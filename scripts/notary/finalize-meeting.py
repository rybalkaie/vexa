#!/usr/bin/env python3
"""Финализирует записанную встречу: meta.json + WAV → Markdown-протокол.

Использование:
    python3 finalize-meeting.py <meta.json>

Pipeline (определяется env STT_BACKEND, default `whisper_pyannote`):

    STT_BACKEND=whisper_pyannote (default):
        1. Прочитать meta.json (path к WAV, participants[], meetingUrl, …).
        2. transcribe_wav через transcription-service (faster-whisper).
        3. diarize_wav через pyannote 3.1 (требует HF_TOKEN).
        4. assign_speakers (alignment) + merge_consecutive_same_speaker.
        5. Маппинг имён (source 1 Telemost / source 2 regex+pymorphy3 /
           source 3 Claude Haiku под флагом).
        6. Рендер markdown и запись в --output-dir.

    STT_BACKEND=speechmatics:
        1. Прочитать meta.json.
        2. speechmatics_client.transcribe_diarize_wav → TranscriptionResult
           (utterances + audio_duration + lang + raw_json + job_id).
        3. to_aligned_turns → AlignedTurn'ы → merge_consecutive_same_speaker.
        4. Маппинг имён (тот же).
        5. Рендер markdown + сохранение _transcripts/<date>.{json,txt} +
           ссылка-строка в шапке .md. Все три артефакта пишутся атомарно:
           сначала .part-файлы, потом os.replace() ТОЛЬКО на успехе всего
           pipeline — либо все три есть, либо ни одного.
        6. При SpeechmaticsError/SpeechmaticsRejectedError аудио + meta
           уходит в `_failed/<sid>.*` для retry-очереди, в Telegram идёт
           push. WAV из _tmp НЕ удаляется до успешной финализации.

Pipeline-инвариант: name_mapping (Claude Haiku) и render_protocol работают
поверх AlignedTurn в обеих ветках без изменений.

Конфиденциальность («Опасная тройка» Ф3):
    - НЕ логируем содержимое транскрипта.
    - НЕ сохраняем сырой ответ Claude в долгоживущие файлы.
    - В meta.json финального протокола НЕ кладём реплики — только пути + sources.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# Подключаем lib (когда запускается из родительской директории).
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from lib.name_mapping import map_all, apply_mapping  # noqa: E402
from lib.llm_postprocess import (  # noqa: E402
    ProtocolGenerationError,
    clarify_speakers_via_telegram,
    deliver_protocol,
    extract_tasks,
    map_speaker_names,
    maybe_clarify_pending_deadlines,
    maybe_clarify_task_count,
    notify_unknown_owners,
    regenerate_protocol_for_meeting,
    review_and_flag_protocol_file,
    route_tasks,
    sync_stakeholder_track,
)
from lib.delivery_grace import wait_for_clarify_grace  # noqa: E402
from lib.render import render_protocol  # noqa: E402
from lib.wav_concat import resolve_wav_for_stt  # noqa: E402
from lib import series_memory  # noqa: E402  # Ф7: память серии встреч


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _is_protocol_enabled() -> bool:
    """Гейт `ENABLE_PROTOCOL_GENERATION` (Ф4). Дефолт ON; `0/false/no` → OFF."""
    raw = (os.environ.get("ENABLE_PROTOCOL_GENERATION") or "").strip().lower()
    return raw not in ("0", "false", "no")


def _stt_backend() -> str:
    """Читает STT_BACKEND из env. Поддерживается `whisper_pyannote` (default) и `speechmatics`."""
    raw = (os.environ.get("STT_BACKEND") or "").strip().lower()
    if not raw or raw == "whisper_pyannote":
        return "whisper_pyannote"
    if raw == "speechmatics":
        return "speechmatics"
    raise SystemExit(
        f"STT_BACKEND={raw!r} — недопустимое значение. "
        "Допустимо: 'whisper_pyannote' (default) или 'speechmatics'."
    )


# ---------------------------------------------------------------------------
# Ветка whisper+pyannote (старая, default до cutover в Ф4)
# ---------------------------------------------------------------------------

def _run_whisper_pyannote(
    args, meta: dict, wav_path: str, language: str, log: logging.Logger
):
    """Старая ветка: transcribe_wav + diarize_wav + assign_speakers + merge.

    Возвращает (turns, extra) — где `extra` это словарь с полями для финального
    JSON-отчёта (whisper_segments / diarization_segments / detected_language).
    """
    # Импорт внутри ветки — чтобы STT_BACKEND=speechmatics не тащил pyannote/torch.
    from lib.transcribe import transcribe_wav  # noqa
    from lib.diarize import diarize_wav  # noqa
    from lib.align import assign_speakers, merge_consecutive_same_speaker  # noqa

    log.info("Step 1/5 — Transcribe full WAV (whisper)")
    api_token = os.environ.get("TRANSCRIPTION_SERVICE_TOKEN")
    _full_text, detected_lang, whisper_segments = transcribe_wav(
        wav_path,
        service_url=args.transcription_service_url,
        model=args.asr_model,
        language=language,
        api_token=api_token,
    )
    if not whisper_segments:
        log.warning("Whisper returned 0 segments — записанный WAV похож на тишину")

    log.info("Step 2/5 — Diarization via pyannote-audio")
    diarization_segments = diarize_wav(
        wav_path,
        num_speakers=args.num_speakers,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        device="cpu",
    )

    log.info("Step 3/5 — Align + merge")
    aligned = assign_speakers(whisper_segments, diarization_segments)
    turns = merge_consecutive_same_speaker(aligned, max_gap_s=1.5)
    extra = {
        "detected_language": detected_lang,
        "whisper_segments": len(whisper_segments),
        "diarization_segments": len(diarization_segments),
        "speakers_detected": len({s.speaker for s in diarization_segments}),
        "stt_label": "whisper+pyannote",
    }
    return turns, extra


# ---------------------------------------------------------------------------
# Ветка Speechmatics (новая, Ф2)
# ---------------------------------------------------------------------------

def _atomic_write_json(path: str, data: dict) -> None:
    """Атомарная запись JSON (tmp + os.replace) — чтобы краш в момент записи
    не оставил полу-записанный meta. Используется для ранней фиксации job_id."""
    import tempfile
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _persist_job_id_to_meta(meta_path: str, meta: dict, job_id: str) -> None:
    """CG3: записать speechmatics_job_id в meta СРАЗУ после сабмита, до поллинга.

    Источник истины переживания краша — сам meta-файл (`<sid>.meta.json`):
    он остаётся на диске и на _tmp/ (collector подберёт заново), и в _failed/
    (retry подберёт). На следующем прогоне main() прочитает job_id и через CG2
    переиспользует живой job вместо повторной оплаты. Мутируем и in-memory
    `meta`, чтобы дальнейший код в этом же процессе видел id.
    """
    meta["speechmatics_job_id"] = job_id
    try:
        _atomic_write_json(meta_path, meta)
    except OSError as e:
        # Не валим расшифровку из-за сбоя записи meta — job уже сабмичен,
        # поллинг продолжится. Худший случай: на краше потеряем дедуп (как было
        # до CG3). Логируем явно, чтобы было видно в journal.
        logging.getLogger("finalize-meeting").warning(
            "CG3: не смог зафиксировать job_id в %s: %s", meta_path, e
        )


def _run_speechmatics(
    wav_path: str,
    log: logging.Logger,
    *,
    expected_speakers: int | None = None,
    existing_job_id: str | None = None,
    on_job_submitted=None,
):
    """Speechmatics-ветка: один HTTP-запрос вместо whisper+pyannote.

    `expected_speakers` (REQ 6.1) — мягкая подсказка состава для диаризации
    (НЕ жёсткий лимит); прокидывается в `transcribe_diarize_wav`.

    `existing_job_id` (CG2) — job из прошлого прогона той же встречи: если ещё
    жив, переиспользуется без повторной оплаты. `on_job_submitted` (CG3) —
    callback(job_id), которым main() фиксирует id в meta СРАЗУ после сабмита.

    Возвращает (turns, extra, sm_result) — `sm_result` это TranscriptionResult
    с raw_json/job_id/duration/lang — используется выше для сохранения в
    _transcripts/<date>.{json,txt}.

    HF_TOKEN явно удаляется из env: Speechmatics его не использует, и нечего
    случайно его таскать в дочерние процессы (типа Claude Haiku подпроцесса,
    хотя там он тоже не нужен — но дисциплина дороже).
    """
    from lib.speechmatics_client import transcribe_diarize_wav, to_aligned_turns
    from lib.align import merge_consecutive_same_speaker  # noqa

    os.environ.pop("HF_TOKEN", None)

    log.info("Step 1/3 — Speechmatics submit + transcribe + diarize (один запрос)")
    sm_result = transcribe_diarize_wav(
        wav_path,
        expected_speakers=expected_speakers,
        existing_job_id=existing_job_id,
        on_job_submitted=on_job_submitted,
    )
    log.info(
        "Speechmatics: %d utterances, %d спикеров, %.1f сек аудио, lang=%s, job=%s",
        len(sm_result.utterances),
        len({u.speaker for u in sm_result.utterances}),
        sm_result.audio_duration_s,
        sm_result.detected_language,
        sm_result.job_id,
    )

    log.info("Step 2/3 — Adapter Utterance → AlignedTurn + merge_consecutive")
    aligned = to_aligned_turns(sm_result.utterances)
    # merge_consecutive_same_speaker оставлен идемпотентным: Speechmatics уже
    # склеивает подряд идущие реплики одного спикера, поэтому на практике
    # шаг no-op (см. эмпирическую проверку в плане Ф2). Не убираем — защита
    # на случай нестандартных стыков (короткое перебивание + продолжение).
    turns = merge_consecutive_same_speaker(aligned, max_gap_s=1.5)

    extra = {
        "detected_language": sm_result.detected_language,
        "audio_duration_s": sm_result.audio_duration_s,
        "speechmatics_job_id": sm_result.job_id,
        "utterances_raw": len(sm_result.utterances),
        "speakers_detected": len({u.speaker for u in sm_result.utterances}),
        "stt_label": "speechmatics-enhanced",
    }
    return turns, extra, sm_result


# ---------------------------------------------------------------------------
# _failed/ — retry-очередь при сбое Speechmatics
# ---------------------------------------------------------------------------

def _failed_dir() -> Path:
    """`_failed/` рядом с output-dir; default `/srv/meeting-notary/_failed/`."""
    return Path(os.environ.get("MEETING_NOTARY_FAILED_DIR") or "/srv/meeting-notary/_failed")


def _push_telegram(message: str, *, silent: bool = False) -> None:
    """Тонкая обёртка над `/srv/meeting-notary/bin/tg-send` (он сам читает .env.notary).

    На маке отсутствует — там используется `~/.local/bin/tg-send`. Финализатор
    штатно работает только на VPS, поэтому ищем сначала VPS-путь.
    """
    log = logging.getLogger("finalize-meeting")
    candidates = [
        "/srv/meeting-notary/bin/tg-send",
        os.path.expanduser("~/.local/bin/tg-send"),
    ]
    tg = next((p for p in candidates if os.access(p, os.X_OK)), None)
    if not tg:
        # logger.error (не warning) — это значит ВЛАДЕЛЕЦ НЕ УЗНАЕТ о сбое
        # Speechmatics / финализации через Telegram. Должен орать в journal,
        # чтобы при разборе очевидно было «alert silenced». См. У6 цикла Ф2.
        log.error("tg-send не найден в %s — push НЕ ОТПРАВЛЕН: %s", candidates, message[:120])
        return
    import subprocess
    cmd = [tg]
    if silent:
        cmd.append("--silent")
    cmd.append(message)
    try:
        subprocess.run(cmd, check=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("tg-send failed: %s — %s", e, message[:80])


def _stash_into_failed(
    sid: str,
    wav_path: str,
    meta_path: str,
    *,
    rejected: bool,
    err_repr: str,
    killswitch: bool = False,
) -> Path:
    """Скопировать WAV+meta в `_failed/<sid>.*` и создать retry-state.

    Если `rejected=True` (конфиг-ошибка Speechmatics: формат/lang/audio) —
    выставляем `attempts=99` чтобы retry-timer не пробовал; ручной разбор
    нужен. Это решение из плана Ф2 (защита от бесполезных retry).

    Если `killswitch=True` (CG7) — встреча отложена не из-за сбоя, а из-за
    взведённого недельного kill-switch. Помечаем `blocked_by_killswitch`,
    чтобы retry_failed не жёг 24ч-бюджет и не слал «нужно ручное решение»,
    а тихо ждал ручного снятия флага (CG8) и считался в напоминании (CG9).
    """
    log = logging.getLogger("finalize-meeting")
    failed = _failed_dir()
    failed.mkdir(parents=True, exist_ok=True)

    wav_dst = failed / f"{sid}.wav"
    meta_dst = failed / f"{sid}.meta.json"
    state_dst = failed / f"{sid}.retry-state.json"

    if not wav_dst.exists() and os.path.exists(wav_path):
        shutil.copy2(wav_path, wav_dst)
    # meta в _failed/ должна указывать на WAV в _failed/, иначе retry попробует
    # обработать исходник из _tmp/, который к этому моменту может быть удалён
    # collector'ом / следующей итерацией финализатора.
    if not meta_dst.exists() and os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta_for_retry = json.load(fh)
        files = (meta_for_retry.get("files") or {}).copy()
        files["wav"] = str(wav_dst)
        meta_for_retry["files"] = files
        meta_dst.write_text(
            json.dumps(meta_for_retry, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # Защита от дубля submit в Speechmatics (У1+У2 из цикла Ф2): после stash
    # удаляем исходные meta/WAV из _tmp/ — иначе listener/collector на следующем
    # тике подберёт meta повторно и оплатит второй submit ($1/час). Источник
    # истины теперь — копия в _failed/.
    if str(Path(wav_path).resolve()) != str(wav_dst.resolve()) and os.path.exists(wav_path):
        try:
            os.unlink(wav_path)
            log.info("Исходный WAV в _tmp/ удалён, источник истины — _failed/%s.wav", sid)
        except OSError as e:
            log.warning("Не смог удалить исходный WAV %s: %s", wav_path, e)
    if str(Path(meta_path).resolve()) != str(meta_dst.resolve()) and os.path.exists(meta_path):
        try:
            os.unlink(meta_path)
            log.info("Исходный meta в _tmp/ удалён, источник истины — _failed/%s.meta.json", sid)
        except OSError as e:
            log.warning("Не смог удалить исходный meta %s: %s", meta_path, e)

    now_iso = datetime.utcnow().isoformat() + "Z"
    state = {
        "session_uid": sid,
        "first_failed_at": now_iso,
        "attempts": 99 if rejected else 0,
        "last_attempt_at": None,
        "rejected": rejected,
        "last_error": err_repr[:500],
        "original_meta_path": meta_path,
        "original_wav_path": wav_path,
    }
    if killswitch:
        state["blocked_by_killswitch"] = True
    state_dst.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Stashed into %s (rejected=%s)", failed, rejected)
    return failed


# ---------------------------------------------------------------------------
# Атомарность артефактов: .part → os.replace()
# ---------------------------------------------------------------------------

class _AtomicBundle:
    """Запись 3 артефактов (JSON / TXT / MD) одним атомарным коммитом.

    Использование:
        bundle = _AtomicBundle()
        bundle.add(json_path, json_str)
        bundle.add(txt_path, txt_str)
        bundle.add(md_path, md_str)
        bundle.commit()   # на этом моменте всё или ничего

    На любой exception между add() и commit() — bundle.abort() убирает .part'ы;
    .commit() атомарно переименовывает все .part в финальные имена через
    os.replace(). Если os.replace одного из них упадёт посередине — частично
    переименованные откатываем по-best-effort, но это редкий edge-case (две
    записи на одной локальной FS обычно либо обе ОК, либо обе падают).
    """

    def __init__(self) -> None:
        self._parts: list[tuple[Path, Path]] = []  # [(final, part), ...]

    def add(self, final_path: Path, content: str | bytes) -> None:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = final_path.with_suffix(final_path.suffix + ".part")
        mode = "wb" if isinstance(content, (bytes, bytearray)) else "w"
        encoding = None if isinstance(content, (bytes, bytearray)) else "utf-8"
        with open(part_path, mode, encoding=encoding) as fh:
            fh.write(content)
        self._parts.append((final_path, part_path))

    def commit(self) -> None:
        renamed: list[Path] = []
        try:
            for final_path, part_path in self._parts:
                os.replace(part_path, final_path)
                renamed.append(final_path)
        except Exception:
            # Откатываем уже переименованные финальные файлы,
            # И отдельно сметаем оставшиеся .part'ы — иначе на FS остаётся
            # мусор после частичного commit'а (Н1 цикла Ф2).
            for f in renamed:
                try:
                    f.unlink()
                except OSError:
                    pass
            for _, part_path in self._parts:
                try:
                    if part_path.exists():
                        part_path.unlink()
                except OSError:
                    pass
            raise

    def abort(self) -> None:
        for _, part_path in self._parts:
            try:
                if part_path.exists():
                    part_path.unlink()
            except OSError:
                pass
        self._parts.clear()


def _format_transcript_txt(utterances) -> str:
    """`[HH:MM:SS] Speaker S1: текст`. По одной реплике на строку."""
    lines = []
    for u in utterances:
        total = int(u.start)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        ts = f"{h:02d}:{m:02d}:{s:02d}"
        lines.append(f"[{ts}] Speaker {u.speaker}: {u.text}")
    return "\n".join(lines) + ("\n" if lines else "")


def _output_dir_for_meta(args, meta: dict) -> tuple[Path, str, str]:
    """Решает, куда писать .md. Возвращает (series_dir, date_part, md_name).

    Серия `<series>` берётся из meta; если её нет — кладём в подпапку с
    sessionUid (legacy-поведение `<date>-<sid>.md`).
    """
    date_part = (meta.get("startTs") or datetime.now().isoformat())[:10]
    series = (meta.get("series") or "").strip()
    sid = meta.get("sessionUid") or "unknown"
    base = Path(args.output_dir)
    if series:
        return base / series, date_part, f"{date_part}.md"
    # legacy fallback (как было): один .md в корне output-dir, без серии.
    return base, date_part, f"{date_part}-{sid}.md"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Финализирует встречу в Markdown-протокол")
    parser.add_argument("meta_json", help="Путь к <sessionUid>.meta.json")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("TELEMOST_PROTOCOL_DIR", "/opt/meeting-notary/_tmp/protocols"),
        help="Куда положить .md (default: $TELEMOST_PROTOCOL_DIR)",
    )
    parser.add_argument(
        "--transcription-service-url",
        default=os.environ.get("TRANSCRIPTION_SERVICE_URL", "http://127.0.0.1:8083/v1/audio/transcriptions"),
    )
    parser.add_argument(
        "--asr-model",
        default=os.environ.get("ASR_MODEL", "Systran/faster-whisper-medium"),
    )
    parser.add_argument(
        "--template",
        default=str(THIS_DIR.parent.parent / "templates" / "meeting-protocol.md"),
        help="Путь к шаблону протокола",
    )
    parser.add_argument("--keep-audio", action="store_true", help="Не удалять WAV после рендера")
    parser.add_argument("--num-speakers", type=int, default=None, help="Точное число спикеров (если знаем)")
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--_test-fail-after-stt",
        action="store_true",
        help="ВНУТРЕННЕЕ: симулировать exception ПОСЛЕ STT для smoke-теста атомарности",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("finalize-meeting")

    t_start = time.time()
    backend = _stt_backend()
    log.info("STT_BACKEND=%s", backend)

    # 1. Читаем meta.
    if not os.path.exists(args.meta_json):
        log.error("meta.json not found: %s", args.meta_json)
        return 2
    if not args.meta_json.endswith(".json"):
        log.error("meta_json должен быть .json файлом, получили: %s", args.meta_json)
        return 2
    with open(args.meta_json, "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    if not isinstance(meta, dict) or not meta.get("sessionUid") or not (meta.get("files") or {}).get("wav"):
        log.error("meta.json не похоже на artifact бота (нужны поля sessionUid + files.wav)")
        return 2

    session_uid = meta.get("sessionUid") or "unknown"
    wav_path = (meta.get("files") or {}).get("wav")
    # Ф2: реальное аудио для STT. Склеивает все meta.recording.chunks[] (Ф5
    # multichunk) в один WAV и чинит placeholder-шапку (data_size=0 у WAV, на
    # котором бот умер до close() — корень P0). None → пригодного PCM нет ни в
    # chunks[], ни в files.wav (тогда ниже отрабатывает rc=10/rc=3, как раньше).
    audio_path, audio_is_temp = resolve_wav_for_stt(meta, log=log)
    if audio_path is None:
        # Ф1-доработки (2026-05-29): если протокол УЖЕ доставлен (есть запись
        # в meta.delivered), WAV был легитимно почищен collector'ом — это
        # «nothing to do», не сбой. Возвращаем rc=10, collector интерпретирует
        # его как «всё ок, не шлём alert».
        delivered_raw = meta.get("delivered")
        has_delivery = False
        # Н8 хода 1: partial-failure НЕ считается успешной доставкой. Если
        # WAV пропал и в meta только partial-failure записи — мы НЕ должны
        # отдавать rc=10 «всё ок», иначе оставшиеся части протокола никогда
        # не дойдут. Принимаем за успех только записи без decision или с
        # decision != "partial-failure".
        def _is_success_record(r):
            return (
                isinstance(r, dict)
                and r.get("message_ids")
                and r.get("decision") != "partial-failure"
            )
        if isinstance(delivered_raw, list):
            has_delivery = any(_is_success_record(r) for r in delivered_raw)
        elif isinstance(delivered_raw, dict):
            has_delivery = _is_success_record(delivered_raw)
        if has_delivery:
            log.info(
                "WAV отсутствует (meta.files.wav=%s) И meta.delivered непустой — "
                "nothing to do, exit rc=10 (no alert)",
                wav_path,
            )
            return 10
        log.error("WAV not found (meta.files.wav=%s)", wav_path)
        return 3
    participants = meta.get("participants") or []
    expected = meta.get("expectedParticipants") or []
    expanded_expected: list[str] = []
    for full in expected:
        if not full:
            continue
        if full not in expanded_expected:
            expanded_expected.append(full)
        first_word = full.split()[0] if full.split() else ""
        if first_word and first_word not in expanded_expected:
            expanded_expected.append(first_word)
    participants_union: list[str] = list(dict.fromkeys(participants + expanded_expected))
    language = meta.get("language") or "ru"

    # Ф7 (7.2/7.3/7-связка): резолвим память серии ОДИН раз — ДО STT, чтобы
    # постоянный состав усилил и диаризацию (6.1), и clarify (5.4), и генерацию
    # (7.3). Резолв дешёвый (json-файлы рядом с протоколами, без claude). Прямая
    # память той же серии (стабильный slug) → fallback по составу участников
    # (нерегулярные 1-на-1, напр. Саргин). Best-effort: сбой → пустая память.
    series_dir, date_part, _md_name_early = _output_dir_for_meta(args, meta)
    series_memory_digests: list[dict] = []
    series_memory_block = ""
    _expected_base = [n for n in expected if n]
    expected_enriched: list[str] = list(dict.fromkeys(_expected_base))
    try:
        if series_memory.is_enabled() and series_memory.has_series_slug(meta.get("series")):
            series_memory_digests = series_memory.resolve_memory(
                series_dir, Path(args.output_dir),
                current_participants=participants_union,
                current_date=date_part,
            )
            for nm in series_memory.permanent_participants(series_memory_digests):
                if nm not in expected_enriched:
                    expected_enriched.append(nm)
            series_memory_block = series_memory.format_memory_block(series_memory_digests)
            log.info(
                "[series-memory] meeting=%s series=%s loaded=%d expected_enrich=+%d",
                session_uid, meta.get("series") or "?", len(series_memory_digests),
                len(expected_enriched) - len(_expected_base),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("[series-memory] resolve failed (non-fatal): %s", e)

    # REQ 6.1: мягкая подсказка диаризации = число РАЗНЫХ ожидаемых участников
    # (по полному имени, без раздутия first-word'ами). Ф7: состав обогащён
    # постоянными участниками серии (expected_enriched) → точнее sensitivity.
    # None → состав неизвестен, Speechmatics работает в дефолте.
    _distinct_expected = [n for n in dict.fromkeys(expected_enriched) if n and isinstance(n, str)]
    expected_speaker_count = len(_distinct_expected) if _distinct_expected else None

    log.info("Session %s — files.wav=%s → stt-audio=%s (temp=%s), "
             "%d participants (panel) + %d expected → %d union, lang=%s",
             session_uid, wav_path, audio_path, audio_is_temp,
             len(participants), len(expected), len(participants_union), language)

    # 2. STT + диаризация (зависит от backend). audio_path может быть временным
    # сконкатенированным/починенным WAV — чистим его в finally после STT.
    sm_result = None  # заполняется только в speechmatics-ветке
    # CG2/CG3 — дедуп платных job'ов Speechmatics.
    #   existing: job из прошлого прогона этой встречи (meta переживает рестарт).
    #   callback: фиксирует job_id в meta СРАЗУ после сабмита, до поллинга.
    existing_job_id = meta.get("speechmatics_job_id") if isinstance(meta, dict) else None

    def _on_job_submitted(job_id: str) -> None:
        _persist_job_id_to_meta(args.meta_json, meta, job_id)

    try:
        if backend == "speechmatics":
            turns, extra, sm_result = _run_speechmatics(
                audio_path, log, expected_speakers=expected_speaker_count,
                existing_job_id=existing_job_id, on_job_submitted=_on_job_submitted,
            )
        else:
            turns, extra = _run_whisper_pyannote(args, meta, audio_path, language, log)
    except Exception as e:
        # Импорт здесь, чтобы whisper_pyannote-ветка не тянула httpx-исключения.
        if backend == "speechmatics":
            from lib.speechmatics_client import (
                SpeechmaticsError,
                SpeechmaticsRejectedError,
                SpeechmaticsKillSwitchError,
            )
            if isinstance(e, SpeechmaticsKillSwitchError):
                # CG7: недельный kill-switch взведён — НЕ платим за сабмит, кладём
                # встречу в retry-очередь (не теряется) и ждём ручного снятия (CG8).
                # rc=5 — отдельный код: collector/retry НЕ считают это сбоем и НЕ
                # жгут 24ч-бюджет ретраев (см. retry_failed: blocked_by_killswitch).
                log.warning(
                    "Speechmatics kill-switch активен — встреча %s отложена в retry "
                    "без сабмита (ждёт ручного снятия флага)", session_uid,
                )
                already_queued = (_failed_dir() / f"{session_uid}.retry-state.json").exists()
                if not already_queued:
                    _stash_into_failed(
                        session_uid, audio_path, args.meta_json,
                        rejected=False, err_repr="kill-switch active", killswitch=True,
                    )
                series_label = meta.get("series") or session_uid
                date_label = (meta.get("startTs") or datetime.now().isoformat())[:10]
                _push_telegram(
                    f"⏸ Расшифровка на паузе: сработал недельный лимит Speechmatics. "
                    f"Встреча «{series_label}» ({date_label}) отложена и НЕ потеряна — "
                    f"обработаю, как только снимешь стоп-флаг (подробности и счётчик "
                    f"ждущих встреч — в дайджесте)."
                )
                return 5
            if isinstance(e, (SpeechmaticsError, SpeechmaticsRejectedError)):
                rejected = isinstance(e, SpeechmaticsRejectedError)
                log.error("Speechmatics %s: %s", "rejected" if rejected else "failed", e)
                # Стэшим именно audio_path (склеенный/починенный) — на retry он же
                # станет files.wav, а исходные chunk-файлы могут быть уже почищены.
                _stash_into_failed(
                    session_uid, audio_path, args.meta_json,
                    rejected=rejected, err_repr=f"{type(e).__name__}: {e}",
                )
                series_label = meta.get("series") or session_uid
                date_label = (meta.get("startTs") or datetime.now().isoformat())[:10]
                if rejected:
                    _push_telegram(
                        f"⛔ Speechmatics отверг встречу `{series_label} {date_label}` "
                        f"(rejected), retry НЕ запущен — нужен ручной разбор. "
                        f"WAV+meta в `_failed/{session_uid}.*`. Причина: {type(e).__name__}: {str(e)[:200]}"
                    )
                else:
                    _push_telegram(
                        f"⚠️ Speechmatics упал на встрече `{series_label} {date_label}`. "
                        f"Аудио в `_failed/{session_uid}.*`, retry-расписание (24ч) запущено. "
                        f"Причина: {type(e).__name__}: {str(e)[:200]}"
                    )
                return 4
        log.exception("STT/диаризация упала: %s", e)
        return 4
    finally:
        # Временный concat/repair WAV больше не нужен (на failure _stash_into_failed
        # уже скопировал его в _failed/ ДО этого finally). Исходные chunk-WAV не
        # трогаем — их чистит collector после доставки / cleanup.service по TTL.
        if audio_is_temp and audio_path and os.path.exists(audio_path):
            try:
                os.unlink(audio_path)
            except OSError as _e:
                log.warning("Не смог удалить временный WAV %s: %s", audio_path, _e)

    # 2.1. Smoke-точка отказа для теста атомарности: симулируем сбой ПОСЛЕ STT.
    if getattr(args, "_test_fail_after_stt", False):
        raise RuntimeError("smoke: симуляция exception после STT (тест атомарности)")

    # 3. Маппинг имён: детерминированные S1+S2, затем LLM-добивка для остатка.
    log.info("Step 4/5 — Name mapping (S1+S2 deterministic, then LLM)")
    mapping_result = map_all(turns, participants_union)
    cluster_to_name: dict[str, str] = dict(mapping_result.cluster_to_name)
    sources_used: list[str] = list(mapping_result.sources_used)
    speaker_confidence: dict[str, float] = {}
    if mapping_result.unresolved_clusters:
        llm_decided = map_speaker_names(
            turns,
            expected_participants=expected_enriched,
            panel_participants=participants,
            already_mapped=cluster_to_name,
            meeting_sid=session_uid,
        )
        if llm_decided:
            for cluster, (name, conf) in llm_decided.items():
                cluster_to_name[cluster] = name
                speaker_confidence[cluster] = conf
            sources_used.append("llm-postprocess")
    turns = apply_mapping(turns, cluster_to_name)
    unresolved_after = [c for c in mapping_result.unresolved_clusters if c not in cluster_to_name]
    log.info("Mapping done — sources=%s, mapped=%d, unresolved=%d",
             sources_used,
             len(cluster_to_name),
             len(unresolved_after))

    # 4. Рендер + атомарная запись.
    log.info("Step 5/5 — Render markdown")
    series_dir, date_part, md_name = _output_dir_for_meta(args, meta)

    transcript_relpath = None
    transcripts_dir = series_dir / "_transcripts"
    transcripts_json_path = transcripts_dir / f"{date_part}.json"
    transcripts_txt_path = transcripts_dir / f"{date_part}.txt"
    if backend == "speechmatics" and sm_result is not None:
        # Относительный путь от папки серии — рендер кладёт его в шапку .md.
        transcript_relpath = f"_transcripts/{date_part}.txt"

    asr_label = "speechmatics-enhanced" if backend == "speechmatics" else args.asr_model
    diar_label = "speechmatics-enhanced" if backend == "speechmatics" else "pyannote/speaker-diarization-3.1"

    markdown = render_protocol(
        template_path=args.template,
        turns=turns,
        meta=meta,
        sources_used=sources_used,
        asr_model=asr_label,
        diarization_model=diar_label,
        transcript_relpath=transcript_relpath,
    )

    md_path = series_dir / md_name
    bundle = _AtomicBundle()
    try:
        # 4a. JSON/TXT — только для speechmatics-ветки.
        if backend == "speechmatics" and sm_result is not None:
            archive_json = {
                "session_uid": session_uid,
                "series": meta.get("series"),
                "date": date_part,
                "speechmatics_job_id": sm_result.job_id,
                "audio_duration_s": sm_result.audio_duration_s,
                "detected_language": sm_result.detected_language,
                "raw_json": sm_result.raw_json,
            }
            bundle.add(
                transcripts_json_path,
                json.dumps(archive_json, ensure_ascii=False, indent=2),
            )
            bundle.add(
                transcripts_txt_path,
                _format_transcript_txt(sm_result.utterances),
            )
        # 4b. .md — всегда.
        bundle.add(md_path, markdown)
        # 4c. Атомарный commit — на этом моменте все три (или один в legacy) — на диске.
        bundle.commit()
    except Exception:
        bundle.abort()
        raise
    log.info("Protocol written → %s", md_path)

    # 4a-bis. Ф1-доработки (2026-06-04): «чистое время обсуждения» (REQ 2.1/2.2).
    # По словам транскрипта (type=word) считаем firstSpeechMs=min(start_time),
    # lastSpeechMs=max(end_time) и кладём в meta.recording. Это превращает
    # «время в звонке» (endTs-startTs = присутствие бота) в реальное время речи
    # от первой до последней реплики. Пишем И в in-memory meta (caption-путь Ф2
    # читает через source 2 compute_duration_label), И обратно в meta.json
    # (REQ 2.2: после finalize meta.json содержит recording.firstSpeechMs/Last).
    if backend == "speechmatics" and sm_result is not None:
        bounds = None
        try:
            from lib.protocol_to_tg import speech_bounds_ms_from_raw_json
            bounds = speech_bounds_ms_from_raw_json(sm_result.raw_json)
        except Exception as e:  # noqa: BLE001
            log.warning("[clean-time] speech-bounds compute failed (non-fatal): %s", e)
        if bounds is not None:
            first_ms, last_ms = bounds
            rec = meta.get("recording")
            if not isinstance(rec, dict):
                rec = {}
                meta["recording"] = rec
            rec["firstSpeechMs"] = first_ms
            rec["lastSpeechMs"] = last_ms
            log.info(
                "[clean-time] recording.firstSpeechMs=%d lastSpeechMs=%d (clean=%d ms)",
                first_ms, last_ms, last_ms - first_ms,
            )
            # Персист обратно в meta.json (REQ 2.2). Атомарно — тем же
            # механизмом, что и артефакты протокола. Best-effort: на сбой
            # записи финализацию не валим (in-memory meta уже обогащён —
            # caption этой доставки всё равно получит чистое время). abort()
            # на сбое чистит висячий `.part` (как протокол-bundle выше).
            meta_bundle = _AtomicBundle()
            try:
                meta_bundle.add(
                    Path(args.meta_json),
                    json.dumps(meta, ensure_ascii=False, indent=2),
                )
                meta_bundle.commit()
            except Exception as e:  # noqa: BLE001
                meta_bundle.abort()
                log.warning("[clean-time] meta.json write-back failed (non-fatal): %s", e)
        else:
            log.warning(
                "[clean-time] no word-level results in transcript — "
                "recording.firstSpeechMs/lastSpeechMs not written"
            )

    # 4.0.1. Ф4: LLM-генерация протокола Sonnet 4.6 по методичке.
    # Порядок: протокол ДО clarify. Если clarify сработает — hook в
    # clarify_worker._apply_resolution перегенерирует протокол на обновлённом
    # транскрипте (так же atomic). Поэтому в Telegram-группу позже (Ф6) уходит
    # уже актуальная версия. До Ф6 — файл просто лежит на диске.
    #
    # Путь: рядом с transcript'ом (`md_path.parent`). `_target_path` сейчас
    # отдал бы НОВУЮ структуру (`<series>/<date>-protokol.md`), а transcript
    # лежит по LEGACY-пути от `_output_dir_for_meta` (`<series>-<date>/...`).
    # Класть протокол в новую папку = разорвать transcript↔protocol. После
    # Ф7 миграции collector синхронизируется на `_target_path`, пути совпадут.
    # Сейчас же — самый надёжный путь «<transcript-dir>/<date>-protokol.md».
    protocol_path = md_path.parent / f"{date_part}-protokol.md"
    if _is_protocol_enabled():
        protocol_meta = dict(meta)
        protocol_meta["date"] = date_part
        protocol_meta["expectedParticipants"] = expected
        protocol_meta["participants"] = participants
        protocol_meta["transcript_filename"] = md_name
        try:
            regenerate_protocol_for_meeting(
                transcript_path=md_path,
                protocol_path=protocol_path,
                meeting_meta=protocol_meta,
                meeting_sid=session_uid,
                series_memory=series_memory_block,  # Ф7 (7.3/7.4): справка серии
            )
            log.info("Protocol generated → %s", protocol_path)
        except ProtocolGenerationError as e:
            # Best-effort: финализация не валится. Файл транскрипта уже на
            # диске; протокол можно перегенерировать через
            # `tools/regenerate-protocol.py <series> <date>` или через
            # Telegram-команду «протокол <series> <date>».
            log.warning(
                "[protocol] generation failed (non-fatal) meeting=%s: %s",
                session_uid, e,
            )
    else:
        log.info("[protocol] disabled by ENABLE_PROTOCOL_GENERATION=0 — skip")

    # 4.0.2. Ф5: извлечение задач + маршрутизация в tasks.md / треки.
    # Делаем ДО clarify спикеров (см. 4.1) и ДО доставки в группу (Ф6).
    # Best-effort: на сбой LLM/IO — warning, finalize не валится.
    tasks_extracted_meta = {
        "ilia": 0,
        "others": 0,
        "pending_deadline": 0,
        "unknown_owner": 0,
    }
    if protocol_path.is_file():
        try:
            protocol_md_text = protocol_path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("[extract_tasks] protocol read failed: %s", e)
            protocol_md_text = ""
        if protocol_md_text.strip():
            task_meta = dict(meta)
            task_meta["date"] = date_part
            task_meta["expectedParticipants"] = expected
            task_meta["participants"] = participants
            # audioDurationS для расчёта порога анти-галлюцинации.
            if sm_result is not None:
                task_meta["audioDurationS"] = sm_result.audio_duration_s
            try:
                tasks = extract_tasks(
                    protocol_md_text,
                    task_meta,
                    meeting_sid=session_uid,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("[extract_tasks] failed (non-fatal): %s", e)
                tasks = []

            if tasks:
                # Анти-галлюцинация (асинхронная, не блокирует запись).
                try:
                    maybe_clarify_task_count(session_uid, tasks, task_meta)
                except Exception as e:  # noqa: BLE001
                    log.warning("[task-clarify] count clarify failed (non-fatal): %s", e)

                try:
                    route_result = route_tasks(
                        tasks,
                        task_meta,
                        meeting_sid=session_uid,
                    )
                    tasks_extracted_meta["ilia"] = route_result.get("ilia", 0)
                    tasks_extracted_meta["others"] = route_result.get("others", 0)
                    tasks_extracted_meta["pending_deadline"] = route_result.get("pending_deadline", 0)
                    tasks_extracted_meta["unknown_owner"] = route_result.get("unknown_owner", 0)
                except Exception as e:  # noqa: BLE001
                    log.warning("[route_tasks] failed (non-fatal): %s", e)
                    route_result = {}

                # Clarification дедлайнов + блок [?] объединяем в ОДНО
                # сообщение Илье (ход 3 У8: не спамим личку 3 сообщениями
                # подряд по одной встрече). Если deadlines==0 и unknown>0 —
                # отдельный notify; если оба 0 — ничего не шлём.
                ilia_no_deadline = [
                    t for t in tasks
                    if (t.get("owner") or "").lower().startswith("илья")
                    and not t.get("deadline")
                ]
                unk = tasks_extracted_meta.get("unknown_owner", 0)
                if ilia_no_deadline:
                    try:
                        maybe_clarify_pending_deadlines(
                            session_uid, ilia_no_deadline, task_meta,
                            unknown_owner_count=unk,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("[task-clarify] deadlines clarify failed (non-fatal): %s", e)
                elif unk > 0:
                    try:
                        notify_unknown_owners(session_uid, unk, task_meta)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[task-clarify] unknown-notify failed (non-fatal): %s", e)

            # Ф3: автосвязка протокол → трек стейкхолдера (закрытие обсуждённых
            # открытых вопросов + добавление новых). За флагом
            # ENABLE_STAKEHOLDER_TRACK_CLOSE (дефолт ON); только 1:1 со
            # стейкхолдером из реестра — гейт внутри. Не зависит от наличия
            # задач (закрытие вопросов идёт по протоколу). Best-effort.
            try:
                sync_stakeholder_track(
                    protocol_md_text,
                    task_meta,
                    meeting_sid=session_uid,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("[track-sync] failed (non-fatal): %s", e)
    else:
        log.info("[extract_tasks] протокол не сгенерирован — пропуск задач")

    # 4.0.2b. Ф5 (5.4 + 5.3): clarify спикеров ДО доставки + grace-окно.
    # Порядок изменён в Ф5: раньше clarify шёл ПОСЛЕ доставки (протокол уходил
    # со «Спикер N», поздний ответ только правил файл). Теперь:
    #   5.4 — clarify_speakers_via_telegram сначала пытается АВТО-подставить
    #         известного участника (people.md / expected) при строгом 1:1 и
    #         перегенерировать протокол; вопрос уходит только про неопознанных.
    #   5.3 — если вопрос ушёл, ждём ~5 мин (PROTOCOL_DELIVERY_GRACE_SEC) шанса,
    #         что Илья ответит и имя попадёт уже в ПЕРВУЮ доставку. Доставка НЕ
    #         блокируется навсегда: по таймауту шлём как есть; поздний ответ
    #         (в окне суток) дошлёт обновлённую версию (5.5/5.6 в clarify_worker).
    # Best-effort: сбой clarify/grace не валит finalize.
    clarify_state_path = None
    try:
        clarify_meta = dict(meta)
        clarify_meta["date"] = date_part
        clarify_state_path = clarify_speakers_via_telegram(
            meeting_id=session_uid,
            turns=turns,
            speaker_confidence=speaker_confidence,
            cluster_to_name=cluster_to_name,
            expected_participants=expected_enriched,
            panel_participants=participants,
            meta=clarify_meta,
            transcript_path=md_path,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("clarify_speakers_via_telegram failed (non-fatal): %s", e)
    if clarify_state_path is not None:
        try:
            wait_for_clarify_grace(session_uid, log)
        except Exception as e:  # noqa: BLE001
            log.warning("[delivery] grace-wait failed (non-fatal): %s", e)

    # 4.0.2c. Ф5 (5.2): LLM-постпроход «подозрительные числа / инверсии».
    # Прогоняем на ФИНАЛЬНОЙ (после авто/grace-доразметки) версии протокола,
    # ДО доставки. ВОПР1 вариант А: НЕ авто-правим, только помечаем ⚠️ «проверь».
    # Best-effort: нет claude в PATH / сбой → 0 пометок, файл не трогаем.
    # Ф6 (6.2): объединено с 5.2 в ОДИН claude-вызов — checks=("values","roles")
    # (НЕ плодим второй проход в hot-path). roles помечает спикеров со
    # смешанными ролями/темами (признак склейки двух людей в один кластер).
    # Ф7 (7.4): добавлена "memory" — ТЕМ ЖЕ одним вызовом (НЕ 3-й проход). Секция
    # ловит факты протокола без опоры на текущий транскрипт (утечку прошлого из
    # справки памяти серии). Сверяет протокол↔транскрипт, доп. данные не нужны.
    if _is_protocol_enabled() and protocol_path.is_file():
        try:
            n_flags = review_and_flag_protocol_file(
                protocol_path=protocol_path,
                transcript_path=md_path,
                checks=("values", "roles", "memory"),
                meeting_sid=session_uid,
            )
            if n_flags:
                log.info("[review] meeting=%s flagged %d suspicious item(s) ⚠️", session_uid, n_flags)
        except Exception as e:  # noqa: BLE001
            log.warning("[review] failed (non-fatal): %s", e)

    # 4.0.2d. Ф7 (7.1): компактная выжимка-память рядом с протоколом.
    # Строим из ФИНАЛЬНОГО протокола ДЕТЕРМИНИРОВАННО (без claude). РИСК4 (ПДн):
    # только производные поля (участники/темы/ключевые пункты), без сырых реплик;
    # текст не логируем; срок хранения прунится отдельным вызовом ниже. На
    # СЛЕДУЮЩЕЙ встрече серии эта выжимка подтянется как справка (7.3). Best-effort.
    if (series_memory.is_enabled() and series_memory.has_series_slug(meta.get("series"))
            and _is_protocol_enabled() and protocol_path.is_file()):
        try:
            _proto_for_digest = protocol_path.read_text(encoding="utf-8")
            if _proto_for_digest.strip():
                _digest_meta = dict(meta)
                _digest_meta["date"] = date_part
                _digest_meta["expectedParticipants"] = expected
                _digest_meta["participants"] = participants
                _digest = series_memory.build_digest(
                    _proto_for_digest, _digest_meta, date=date_part,
                )
                series_memory.save_digest(series_dir, date_part, _digest)
                # РИСК4 (срок хранения): прунинг старых выжимок ПО ТЕКУЩЕЙ серии
                # (свежая, дешёвый скан). Отдельной операцией, а не внутри save —
                # иначе бэкфилл старых протоколов самоудалял бы результат.
                series_memory.prune_old_digests(series_dir, series_memory.retention_days())
        except Exception as e:  # noqa: BLE001
            log.warning("[series-memory] digest save failed (non-fatal): %s", e)

    # 4.0.3. Ф6: доставка протокола в Telegram-группу.
    # Идемпотентность через `meta.delivered` в meta.json. Если привязки
    # series→chat_id в watched.yaml нет — `deliver_protocol` сам спросит
    # Илью через notarius-бота (state в `_pending_clarification/...delivery.json`),
    # listener подберёт ответ. Best-effort: на сбой LLM/IO/Telegram —
    # warning, finalize не валится; протокол на диске уже есть, можно
    # переотправить вручную (через Ф6.x CLI или повторную финализацию).
    delivery_result = {"status": "not-run"}
    if protocol_path.is_file():
        try:
            protocol_text_for_delivery = protocol_path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("[delivery] protocol read failed: %s", e)
            protocol_text_for_delivery = ""
        if protocol_text_for_delivery.strip():
            delivery_meta = dict(meta)
            delivery_meta["date"] = date_part
            delivery_meta["sessionUid"] = session_uid
            # meta.json финализированной встречи — для idempotency. Лежит
            # рядом с транскриптом (collector кладёт meta туда же).
            meta_json_path = md_path.parent / "meta.json"
            try:
                delivery_result = deliver_protocol(
                    meeting_meta=delivery_meta,
                    protocol_text=protocol_text_for_delivery,
                    meta_json_path=meta_json_path if meta_json_path.is_file() else None,
                    meeting_sid=session_uid,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("[delivery] failed (non-fatal): %s", e)
                delivery_result = {"status": "error", "error": str(e)}

    # 4.0.3b. Ф8 LLM-proposer: кандидаты в Speechmatics vocab по стенограмме.
    # Зовём ТОЛЬКО после успешной TG-доставки протокола (status in {sent,
    # skipped}) — иначе нет смысла тратить вызов Claude. Best-effort (РИСК4):
    # любой сбой proposer'а внутри конвертируется в статус, не бросает; здесь
    # дополнительный try на случай ImportError пакета. НЕ блокирует pipeline.
    # Гейт DISABLE_AUTO_VOCAB обрабатывается внутри propose(). До готовности
    # applier'а (Шаг 8.3) кандидаты только логируются и складываются в stash —
    # держи DISABLE_AUTO_VOCAB=1 в проде, пока applier не задеплоен.
    if delivery_result.get("status") in {"sent", "skipped"} and md_path.is_file():
        try:
            from notary.auto_vocab import llm_proposer  # noqa: PLC0415
            proposer_meta = dict(meta)
            proposer_meta["date"] = date_part
            pr = llm_proposer.propose_from_meta(
                proposer_meta, md_path, session_uid=session_uid
            )
            log.info(
                "[auto-vocab] proposer: status=%s candidates=%d cost=$%.4f",
                pr.get("status"), len(pr.get("candidates", [])), pr.get("cost_usd", 0.0),
            )
            # Шаг 8.3: применить кандидатов — high→авто-словарь+silent TG,
            # low→TG-запрос с кнопками. Best-effort, не блокирует.
            if pr.get("status") == "ok" and pr.get("candidates"):
                from notary.auto_vocab import applier  # noqa: PLC0415
                ar = applier.apply_proposal(session_uid, proposer_meta)
                log.info(
                    "[auto-vocab] applier: status=%s high+%d low→TG %d",
                    ar.get("status"), len(ar.get("high_added", [])), ar.get("low_sent", 0),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("[auto-vocab] proposer/applier failed (non-fatal): %s", e)

    # 4.0.4. Ф7: обновление INDEX.md в корне ~/Projects/me/встречи/.
    # Без LLM, простой os.walk. Best-effort: не валим финализацию из-за индекса.
    # Гейт ENABLE_VSTRECHI_INDEX (дефолт ON).
    try:
        from lib.vstrechi_index import update_vstrechi_index, is_enabled as _index_enabled
        if _index_enabled():
            # INDEX живёт там же, где transcript на этой машине — корень MEETINGS_DIR.
            # На VPS (LOCAL_FINALIZE=1) индекс не нужен — данные потом mirror'ятся на мак.
            if os.environ.get("MEETING_NOTARY_LOCAL_FINALIZE") != "1":
                # Корень — DEFAULT_ROOT модуля (~/Projects/me/встречи).
                # md_path.parent.parent даёт _one-off для one-off, не подходит.
                idx_stats = update_vstrechi_index()
                log.info(
                    "[vstrechi-index] updated: %d series, total bytes=%d",
                    idx_stats["series_count"], idx_stats["total_bytes"],
                )
    except Exception as e:  # noqa: BLE001
        log.warning("[vstrechi-index] update failed (non-fatal): %s", e)

    # 4.1. Ф5: clarify спикеров теперь идёт ДО доставки (блок 4.0.2b выше) —
    # с авто-подстановкой известных (5.4) и grace-окном (5.3). Здесь раньше был
    # повторный trigger ПОСЛЕ доставки (дизайн Ф3/Ф4); в Ф5 он перенесён вверх,
    # чтобы имя попадало в первую доставку, а не только в поздний до-сыл.

    if backend == "speechmatics":
        log.info("Transcripts archive → %s, %s", transcripts_json_path, transcripts_txt_path)
        # Append в bench-speechmatics-prod.log — Ф4 семидневный мониторинг.
        # Формат: <iso_ts> <series> <date> wav=<sec> finalize=<sec> job=<id>
        try:
            bench_path = "/srv/meeting-notary/logs/bench-speechmatics-prod.log"
            series_label = meta.get("series") or session_uid
            audio_sec = round(sm_result.audio_duration_s, 1) if sm_result else 0.0
            finalize_sec = round(time.time() - t_start, 1)
            job_id = sm_result.job_id if sm_result else "n/a"
            iso_ts = datetime.utcnow().isoformat() + "Z"
            line = f"{iso_ts} {series_label} {date_part} wav={audio_sec} finalize={finalize_sec} job={job_id}\n"
            with open(bench_path, "a", encoding="utf-8") as bf:
                bf.write(line)
        except Exception as e:
            log.warning("bench-prod log write failed: %s", e)

    # 5. WAV cleanup перенесён в collector (Ф1-доработки 2026-05-29).
    # Раньше: finalize удалял WAV здесь, до подтверждения доставки в TG.
    # Сейчас: WAV живёт до тех пор, пока collector не убедится, что
    #   (а) `.md` лёг в MEETINGS_DIR; (б) TG-доставка прошла (`delivery.status
    #       in {sent, skipped}` в stdout JSON).
    # Это даёт возможность безболезненно перезапустить finalize при сбое
    # доставки. См. _finalize_and_collect в collector.py.
    keep_audio_from_meta = bool(meta.get("keepAudio") or meta.get("keep_audio"))
    log.info(
        "WAV cleanup decision deferred to collector (keep_audio_flag=%s, "
        "delivery_status=%s)",
        bool(args.keep_audio or keep_audio_from_meta),
        delivery_result.get("status"),
    )

    # 5.1. Если этот sid был в _failed/ — успешная финализация значит retry прошёл.
    sid_files = list(_failed_dir().glob(f"{session_uid}.*")) if _failed_dir().exists() else []
    if sid_files:
        attempts_count = None
        state_path = _failed_dir() / f"{session_uid}.retry-state.json"
        if state_path.exists():
            try:
                attempts_count = json.loads(state_path.read_text(encoding="utf-8")).get("attempts")
            except Exception:
                pass
        for f in sid_files:
            try:
                f.unlink()
            except OSError:
                pass
        series_label = meta.get("series") or session_uid
        n = (attempts_count or 0) + 1
        _push_telegram(
            f"✅ Встреча `{series_label} {date_part}` обработана после {n} попыток (Speechmatics)."
        )

    # 6. Краткий результат.
    result_json = {
        "ok": True,
        "session_uid": session_uid,
        "series": meta.get("series"),
        "protocol_path": str(md_path),
        "wav_kept": args.keep_audio or keep_audio_from_meta,
        "stt_backend": backend,
        "sources": {"stt": extra.get("stt_label")},
        "language_detected": extra.get("detected_language"),
        "speakers_detected": extra.get("speakers_detected"),
        "speakers_named": len(cluster_to_name),
        "name_mapping_sources": sources_used,
        "unresolved_clusters": unresolved_after,
        "speaker_confidence": speaker_confidence,
        "tasks_extracted": tasks_extracted_meta,
        "delivery": delivery_result,
    }
    if backend == "speechmatics":
        result_json.update({
            "audio_duration_s": extra.get("audio_duration_s"),
            "speechmatics_job_id": extra.get("speechmatics_job_id"),
            "utterances_raw": extra.get("utterances_raw"),
            "transcripts_archive_json": str(transcripts_json_path),
            "transcripts_archive_txt": str(transcripts_txt_path),
        })
    else:
        result_json.update({
            "whisper_segments": extra.get("whisper_segments"),
            "diarization_segments": extra.get("diarization_segments"),
        })
    print(json.dumps(result_json, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
