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

    STT_BACKEND=assemblyai (Ф1 umnyi-protokol-assemblyai; на бою — новый дефолт):
        То же, что speechmatics, но через `assemblyai_client.transcribe_diarize_wav`
        (upload → create → poll, universal-3-pro, ru, speaker_labels). Тот же
        downstream (архив _transcripts/<date>.{json,txt}, clean-time, bench) — обе
        внешние STT-ветки идут общим путём через `_is_external_stt(backend)`. При
        AssemblyAIError/AssemblyAIRejectedError аудио+meta → `_failed/`, push;
        retry_failed форсит Speechmatics → сбой AAI ретраится на fallback (AA4).
        Speechmatics НЕ удаляется — остаётся fallback/legacy.

Pipeline-инвариант: name_mapping (Claude Haiku) и render_protocol работают
поверх AlignedTurn во всех ветках без изменений.

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
    build_authorship_uncertainty_findings,
    build_cross_memory_block,
    clarify_speakers_via_telegram,
    deliver_protocol,
    extract_tasks,
    generate_alt_draft_for_best_of_2,
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
from lib.protocol_to_tg import filter_participant_names  # noqa: E402  # Ф1 A2.1
from lib.render import render_protocol  # noqa: E402
from lib.wav_concat import resolve_wav_for_stt  # noqa: E402
from lib import context_knowledge  # noqa: E402  # R18: справочник людей компании
from lib import correction_facts  # noqa: E402  # Ф2: company-замок исправлений (R6-R12)
from lib import series_memory  # noqa: E402  # Ф7: память серии встреч
from lib import series_roster  # noqa: E402  # Ф3: ростер ролей серии (домен→роль)
from lib import publication_gate  # noqa: E402  # Ф6: гейтинг публикации знания (E1–E5)


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
    """Читает STT_BACKEND из env. Поддерживается `whisper_pyannote` (default),
    `speechmatics` и `assemblyai` (Ф1 плана umnyi-protokol-assemblyai). На бою
    дефолт переходит на AAI; Speechmatics остаётся fallback (AA4), не удаляем."""
    raw = (os.environ.get("STT_BACKEND") or "").strip().lower()
    if not raw or raw == "whisper_pyannote":
        return "whisper_pyannote"
    if raw == "speechmatics":
        return "speechmatics"
    if raw == "assemblyai":
        return "assemblyai"
    raise SystemExit(
        f"STT_BACKEND={raw!r} — недопустимое значение. "
        "Допустимо: 'whisper_pyannote' (default), 'speechmatics' или 'assemblyai'."
    )


def _is_external_stt(backend: str) -> bool:
    """Внешний облачный STT (один HTTP-job; есть raw_json/job_id/архив транскрипта).

    speechmatics и assemblyai делят общий downstream-путь (архив `_transcripts`,
    clean-time, bench-лог, recovery по id). whisper_pyannote — локальный, без них."""
    return backend in ("speechmatics", "assemblyai")


def _stt_id_meta_key(backend: str) -> str:
    """Ключ в meta для id внешнего job'а (идемпотентность/recovery/CG2).

    Разные ключи у движков, чтобы id одного не подхватился как id другого при
    смене backend между прогонами одной встречи. whisper_pyannote → '' (нет id)."""
    if backend == "speechmatics":
        return "speechmatics_job_id"
    if backend == "assemblyai":
        return "assemblyai_transcript_id"
    return ""


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
# Ветка AssemblyAI (новая, Ф1 плана umnyi-protokol-assemblyai)
# ---------------------------------------------------------------------------

def _persist_transcript_id_to_meta(meta_path: str, meta: dict, transcript_id: str) -> None:
    """AAI-аналог `_persist_job_id_to_meta`: фиксируем `assemblyai_transcript_id`
    в meta СРАЗУ после create, до поллинга. На рестарте main() прочитает id и
    переиспользует существующий транскрипт без повторной оплаты (идемпотентность).
    Мутируем и in-memory `meta`, чтобы дальнейший код в этом процессе видел id."""
    meta["assemblyai_transcript_id"] = transcript_id
    try:
        _atomic_write_json(meta_path, meta)
    except OSError as e:
        logging.getLogger("finalize-meeting").warning(
            "не смог зафиксировать assemblyai_transcript_id в %s: %s", meta_path, e
        )


def _run_assemblyai(
    wav_path: str,
    log: logging.Logger,
    *,
    existing_transcript_id: str | None = None,
    on_transcript_created=None,
    series_slug: str | None = None,
):
    """AssemblyAI-ветка: upload → create → poll вместо whisper+pyannote.

    Контракт возврата идентичен `_run_speechmatics`: (turns, extra, aai_result).
    `aai_result` — TranscriptionResult с raw_json/job_id/duration/lang (используется
    выше для архива `_transcripts/<date>.{json,txt}` и clean-time, как у Speechmatics).

    `existing_transcript_id` (идемпотентность) — id из прошлого прогона той же
    встречи: если задан, переиспользуем без повторной оплаты. `on_transcript_created`
    — callback(id), которым main() фиксирует id в meta СРАЗУ после create.

    `series_slug` (Ф2, S4) — slug серии встречи: по нему собираем доменный словарь
    (имена участников + бренды + термины ниши) и передаём в `keyterms_prompt`
    задания AAI (биас распознавания к верным написаниям). Нет slug / сбор упал →
    словарь пуст, поле не передаётся (поведение Ф1). РИСК2: список содержит имена —
    логируем только ЧИСЛО терминов, не сам список.

    HF_TOKEN явно удаляется из env (как в Speechmatics-ветке): AAI его не использует,
    нечего таскать в дочерние процессы — дисциплина.
    """
    from lib.assemblyai_client import transcribe_diarize_wav, to_aligned_turns
    from lib.align import merge_consecutive_same_speaker  # noqa
    from lib import keyterms  # Ф2: сбор доменного словаря серии

    os.environ.pop("HF_TOKEN", None)

    # S4: доменный словарь серии → keyterms_prompt. best-effort; сам список НЕ
    # логируем (РИСК2 — имена участников), только метаданные (число терминов).
    keyterms_prompt = keyterms.collect_keyterms_prompt(series_slug)
    log.info("AssemblyAI keyterms: %d терминов словаря серии", len(keyterms_prompt))

    log.info("Step 1/3 — AssemblyAI upload + transcribe + diarize (один job)")
    aai_result = transcribe_diarize_wav(
        wav_path,
        existing_transcript_id=existing_transcript_id,
        on_transcript_created=on_transcript_created,
        keyterms_prompt=keyterms_prompt,
    )
    log.info(
        "AssemblyAI: %d utterances, %d спикеров, %.1f сек аудио, lang=%s, id=%s",
        len(aai_result.utterances),
        len({u.speaker for u in aai_result.utterances}),
        aai_result.audio_duration_s,
        aai_result.detected_language,
        aai_result.job_id,
    )

    log.info("Step 2/3 — Adapter Utterance → AlignedTurn + merge_consecutive")
    aligned = to_aligned_turns(aai_result.utterances)
    # merge_consecutive_same_speaker идемпотентен: AAI уже отдаёт реплики turn-by-turn
    # (utterances[]), поэтому на практике no-op. Не убираем — защита на нестандартных
    # стыках (короткое перебивание + продолжение того же спикера), как в SM-ветке.
    turns = merge_consecutive_same_speaker(aligned, max_gap_s=1.5)

    extra = {
        "detected_language": aai_result.detected_language,
        "audio_duration_s": aai_result.audio_duration_s,
        "assemblyai_transcript_id": aai_result.job_id,
        "utterances_raw": len(aai_result.utterances),
        "speakers_detected": len({u.speaker for u in aai_result.utterances}),
        "stt_label": "assemblyai-universal-3-pro",
    }
    return turns, extra, aai_result


# ---------------------------------------------------------------------------
# _failed/ — retry-очередь при сбое внешнего STT (Speechmatics / AssemblyAI)
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


def _delivery_marker_meta_path(meta_json_arg: str) -> Path:
    """Путь к meta-файлу, в который finalize пишет маркер `delivered`.

    Это ИСХОДНАЯ meta встречи (`args.meta_json` = `<transcripts>/<sid>.meta.json`),
    а НЕ `meta.json` в output-dir рядом с протоколом. Причина (Ф2, REQ 3.1/3.2):
    маркер обязан лежать в ОДНОМ файле, который читают ОБА потребителя —
      • дедуп collector'а: `collector._read_meta_obj` → `_delivery_done` берёт
        ровно `<VPS_TRANSCRIPTS>/<sid>.meta.json` (= `args.meta_json`);
      • reply-gate правок: `feedback_worker.find_delivered_protocol` сканирует
        delivered-roots (transcripts-dir в их числе).
    Туда же пишет `tools/backfill_delivered.py`. Один писатель
    (`_update_meta_delivered`), один путь, одна форма — НЕ плодить второй (РИСК1).

    РАНЬШЕ путь был `md_path.parent / "meta.json"` (output-dir протокола, который
    создаётся лениво и `.is_file()` обычно False) → `_update_meta_delivered`
    получал None, маркер не писался в meta collector'а, и при recovery/повторном
    тике встреча перевыпускалась дублем (а WAV почищен → ложный rc=3).
    """
    return Path(meta_json_arg)


def _resolve_absent_for_meeting(meta: dict, pool: list[str]) -> set[str]:
    """Ф2 (R9): негативный слой «кого не было» ТЕКУЩЕЙ встречи → множество каноничных
    имён (по составу `pool`).

    Источник — `meta.absentNames` (список имён, ставит листенер из правки владельца
    «X не было»; может быть в склонённой форме). Резолвим каждое к каноничному имени
    состава через `correction_facts.resolve_known_name` (склонения без pymorphy3);
    нерезолвящееся берём surface-form. Только ЭТА встреча — НЕ персистится вперёд
    (решение владельца R9/A3), поэтому читаем из meta встречи, не из company-overlay.
    Нет поля / не список → пустое множество (поведение как до Ф2)."""
    raw = meta.get("absentNames") if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        tok = str(item or "").strip()
        if not tok:
            continue
        canon = correction_facts.resolve_known_name(tok, pool) or tok
        out.add(canon)
    return out


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
    # Защита от орфана series=null (2026-06-08): если series пуст, протокол уйдёт
    # в legacy-fallback (`<date>-<sid>.md` без серии) → не подхватит telegram_chat_id
    # серии → доставится в личку вместо группы. Корень обычно в боте (Zod-strip
    # series из BOT_CONFIG — фикс в recording.ts). Громкий WARNING тут делает
    # будущий орфан видимым в логах finalize, а не тихой потерей серии.
    if not (meta.get("series") or "").strip():
        log.warning(
            "series ПУСТ в meta (sessionUid=%s) — протокол осиротеет (legacy-путь "
            "без серии, доставка в личку). Проверь, что бот прокидывает series из "
            "BOT_CONFIG в meta (recording.ts).",
            session_uid,
        )
    wav_path = (meta.get("files") or {}).get("wav")
    # Ф2: реальное аудио для STT. Склеивает все meta.recording.chunks[] (Ф5
    # multichunk) в один WAV и чинит placeholder-шапку (data_size=0 у WAV, на
    # котором бот умер до close() — корень P0). None → пригодного PCM нет ни в
    # chunks[], ни в files.wav (тогда ниже отрабатывает rc=10/rc=3, как раньше).
    audio_path, audio_is_temp = resolve_wav_for_stt(meta, log=log)
    if audio_path is None:
        # Recovery: WAV почищен, но в meta есть id внешнего job'а прошлого прогона
        # (speechmatics_job_id / assemblyai_transcript_id — по backend). Если job/
        # транскрипт ещё жив — reuse переиспользует его без повторного STT и без
        # файла на диске (transcribe_diarize_wav в reuse-ветке WAV не открывает).
        # Длительность/clean-time берутся из транскрипта + meta.recording, а не из
        # WAV. Id истёк → audio_path так и останется None, ниже отработает rc=3/rc=10.
        _id_key = _stt_id_meta_key(backend)
        _reuse_job_id = meta.get(_id_key) if (_id_key and isinstance(meta, dict)) else None
        if _reuse_job_id:
            log.info(
                "WAV отсутствует, но есть %s=%s — recovery-режим: переиспользую "
                "существующий job/transcript без STT/WAV (files.wav=%s)",
                _id_key, _reuse_job_id, wav_path,
            )
            audio_path = str(wav_path)  # путь-декларация; в reuse-ветке не читается
            audio_is_temp = False
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
    # Ф1 A2.1: единый chokepoint — чистим панель Телемоста от UI-мусора
    # («Скопировать ссылку», монограммы «ДН») ДО union/маппинга/протокола/
    # выжимки. Иначе мусор течёт в пул имён map_all и в шапку протокола.
    participants = filter_participant_names(meta.get("participants") or [])
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
    # Ф8 (G9): хвост незакрытых задач серии → раздел «🔻 С прошлых встреч» в протоколе.
    # Готовый блок-инструкция, едет в ТОТ ЖЕ Вызов 1 генерации (НЕ отдельный вызов).
    open_tasks_block = ""
    # Ф4б (REQ 1.2): закреплённое человеком сопоставление спикер→имя из памяти серии
    # (правка авторства реплаем на прошлой встрече). Подаётся якорем в map_all ДО
    # догадки Ф4а. {} если нет/старые файлы без ключа (ленивое поле, УПУ3).
    series_speaker_anchor: dict[str, str] = {}
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
            series_speaker_anchor = series_memory.resolve_speaker_anchor(series_memory_digests)
            # Ф6 (G6/G7/G11): ШИРОКИЙ кросс-встречный фон из ДРУГИХ серий (приоритет
            # своей компании, кросс-компания при релевантности — A7, не стена).
            # Главный guard — фильтр чувствительного G11 (LLM) + ручной маркер серии
            # `visibility=private`. Кладём В ТОТ ЖЕ memory-блок ПЕРЕД транскриптом,
            # отдельной секцией «ФОН». Best-effort внутри build_cross_memory_block → "".
            cross_block = build_cross_memory_block(
                Path(args.output_dir), series_dir, meta,
                current_participants=participants_union,
                same_series_digests=series_memory_digests,
                meeting_sid=session_uid,
            )
            series_memory_block = "\n\n".join(
                p for p in (series_memory_block.strip(), cross_block.strip()) if p
            )
            # Ф8 (G9): хвост открытых задач серии из тех же выжимок (latest несёт
            # кумулятивное состояние). Под своим kill-switch внутри. Отдельным от
            # series_memory каналом — у него обратная дисциплина (перенести + статус).
            open_tasks_block = series_memory.build_open_tasks_block(
                series_memory_digests, meeting_sid=session_uid,
            )
            log.info(
                "[series-memory] meeting=%s series=%s loaded=%d expected_enrich=+%d cross_len=%d",
                session_uid, meta.get("series") or "?", len(series_memory_digests),
                len(expected_enriched) - len(_expected_base), len(cross_block),
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
    sm_result = None   # заполняется только в speechmatics-ветке
    aai_result = None  # заполняется только в assemblyai-ветке
    # Дедуп платных job'ов внешнего STT (id переживает рестарт встречи в meta).
    #   existing: id из прошлого прогона этой встречи (meta переживает рестарт).
    #   callback: фиксирует id в meta СРАЗУ после сабмита/create, до поллинга
    #   (CG2/CG3 у Speechmatics; идемпотентность по transcript id у AssemblyAI).
    existing_job_id = meta.get("speechmatics_job_id") if isinstance(meta, dict) else None
    existing_transcript_id = meta.get("assemblyai_transcript_id") if isinstance(meta, dict) else None

    def _on_job_submitted(job_id: str) -> None:
        _persist_job_id_to_meta(args.meta_json, meta, job_id)

    def _on_transcript_created(transcript_id: str) -> None:
        _persist_transcript_id_to_meta(args.meta_json, meta, transcript_id)

    try:
        if backend == "speechmatics":
            turns, extra, sm_result = _run_speechmatics(
                audio_path, log, expected_speakers=expected_speaker_count,
                existing_job_id=existing_job_id, on_job_submitted=_on_job_submitted,
            )
        elif backend == "assemblyai":
            turns, extra, aai_result = _run_assemblyai(
                audio_path, log,
                existing_transcript_id=existing_transcript_id,
                on_transcript_created=_on_transcript_created,
                series_slug=(meta.get("series") if isinstance(meta, dict) else None),
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
                    # Пуш «на паузе» — ТОЛЬКО при первом откладывании встречи (когда
                    # реально кладём её в очередь). На повторных retry-тиках (каждые
                    # 15 мин; _due_to_attempt при blocked_by_killswitch всегда due)
                    # finalize снова вернёт rc=5 с already_queued=True — второй раз НЕ
                    # пушим, иначе один и тот же «на паузе» долбит владельца ~раз в 6 ч
                    # (окно дедупа notify) на каждую ждущую встречу всё время, пока
                    # флаг взведён. Регулярное напоминание — консолидированное, из
                    # недельного монитора (CG9), раз в сутки.
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
                        f"⛔ Не удалось расшифровать встречу «{series_label}» ({date_label}): "
                        f"сервис распознавания речи отклонил запрос и повторять не станет — "
                        f"нужно разобраться вручную. Аудио сохранено, не потеряно. "
                        f"Что пошло не так: {type(e).__name__}: {str(e)[:200]} "
                        f"(файлы для разбора: _failed/{session_uid}.*)"
                    )
                else:
                    _push_telegram(
                        f"⚠️ Пока не получилось расшифровать встречу «{series_label}» ({date_label}): "
                        f"сервис распознавания речи временно недоступен. Аудио сохранено — "
                        f"автоматически попробую ещё раз примерно через сутки, от тебя ничего не нужно. "
                        f"Что пошло не так: {type(e).__name__}: {str(e)[:200]} "
                        f"(файлы: _failed/{session_uid}.*)"
                    )
                return 4
        elif backend == "assemblyai":
            from lib.assemblyai_client import (
                AssemblyAIError,
                AssemblyAIRejectedError,
                AssemblyAIKillSwitchError,
            )
            if isinstance(e, AssemblyAIKillSwitchError):
                # CG7-аналог: kill-switch взведён — НЕ платим за сабмит, кладём
                # встречу в retry-очередь (не теряется), ждём ручного снятия (CG8).
                # rc=5 — отдельный код: retry НЕ жжёт 24ч-бюджет (blocked_by_killswitch).
                log.warning(
                    "AssemblyAI kill-switch активен — встреча %s отложена в retry "
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
                        f"⏸ Расшифровка на паузе: сработал недельный лимит распознавания. "
                        f"Встреча «{series_label}» ({date_label}) отложена и НЕ потеряна — "
                        f"обработаю, как только снимешь стоп-флаг (подробности и счётчик "
                        f"ждущих встреч — в дайджесте)."
                    )
                return 5
            if isinstance(e, (AssemblyAIError, AssemblyAIRejectedError)):
                rejected = isinstance(e, AssemblyAIRejectedError)
                log.error("AssemblyAI %s: %s", "rejected" if rejected else "failed", e)
                # Стэшим audio_path (склеенный/починенный) — на retry он станет
                # files.wav; исходные chunk-файлы могут быть уже почищены. retry_failed
                # форсит STT_BACKEND=speechmatics → сбой AAI ретраится на fallback (AA4).
                _stash_into_failed(
                    session_uid, audio_path, args.meta_json,
                    rejected=rejected, err_repr=f"{type(e).__name__}: {e}",
                )
                series_label = meta.get("series") or session_uid
                date_label = (meta.get("startTs") or datetime.now().isoformat())[:10]
                if rejected:
                    _push_telegram(
                        f"⛔ Не удалось расшифровать встречу «{series_label}» ({date_label}): "
                        f"сервис распознавания речи отклонил запрос и повторять не станет — "
                        f"нужно разобраться вручную. Аудио сохранено, не потеряно. "
                        f"Что пошло не так: {type(e).__name__}: {str(e)[:200]} "
                        f"(файлы для разбора: _failed/{session_uid}.*)"
                    )
                else:
                    _push_telegram(
                        f"⚠️ Пока не получилось расшифровать встречу «{series_label}» ({date_label}): "
                        f"сервис распознавания речи временно недоступен. Аудио сохранено — "
                        f"автоматически попробую ещё раз примерно через сутки, от тебя ничего не нужно. "
                        f"Что пошло не так: {type(e).__name__}: {str(e)[:200]} "
                        f"(файлы: _failed/{session_uid}.*)"
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

    # Унифицированный результат внешнего STT (Speechmatics ИЛИ AssemblyAI), None
    # для whisper_pyannote. Downstream (архив `_transcripts`, clean-time, bench,
    # result_json) работает на нём единообразно через `_is_external_stt(backend)` —
    # ровно один из sm_result/aai_result непуст, когда backend внешний.
    ext_result = sm_result if sm_result is not None else aai_result

    # 3. Маппинг имён: Ф4б якорь серии → S1 → Ф3 ростер-домен → S2 → LLM-добивка.
    # Ф3 (A4/B3): ростер ролей серии (домен реплики ↔ ответственный). Хардкод
    # Anzhee-координации; ЗАВ1/Ф5 переключит источник на `*-context`. Незнакомая
    # серия → [] (доменного маппинга нет, поведение как до Ф3).
    series_roster_entries = series_roster.get_roster(meta.get("series"))
    # R18/R4: справочник людей компании серии — неактивные имена не подставляем
    # автором (LLM-добивка) и не путаем тёзку (дизамбигуация). Graceful: нет
    # company/YAML → пусто (поведение как до R18).
    _company = context_knowledge.company_for_series(meta.get("series"))
    inactive_names = context_knowledge.inactive_person_names(_company)
    # Ф2 (R6/R10): durable company-факты исправлений ПОВЕРХ ростера серии. Выученная
    # правка «сервис→Саргин» перекрывает устаревший YAML-ростер и держится на ВСЕХ
    # будущих встречах ЭТОЙ компании (любой серии) — реприменяется на КАЖДОЙ
    # регенерации, т.к. читается здесь. last-write-wins резолвится на чтении.
    # Graceful: нет company / нет overlay → ростер как есть (поведение как до Ф2).
    series_roster_entries = correction_facts.merge_roster(series_roster_entries, _company)
    # Ф2 (R8): отвергнутые правкой имена («не Еремеев, а Саргин» → Еремеев) — в тот же
    # negative-фильтр, что inactive: LLM-добивка/дизамбигуация их больше не предлагают.
    inactive_names = set(inactive_names) | correction_facts.excluded_names(_company)
    # Ф2 (R9): негативный слой ТЕКУЩЕЙ встречи — «кого не было». Только эта встреча,
    # вперёд НЕ запоминается (решение владельца R9/A3): источник — meta.absentNames
    # (ставит листенер из правки «X не было»), резолв склонений по составу. Механизм
    # ОТДЕЛЁН от R4-сужения пула (НЕС1): тут не сужаем пул людей компании, а исключаем
    # конкретно отсутствовавших на ЭТОЙ встрече.
    absent_names = _resolve_absent_for_meeting(meta, participants_union)
    present_effective = [p for p in participants if p not in absent_names]
    expected_effective = [n for n in expected_enriched if n not in absent_names]
    log.info("Step 4/5 — Name mapping (R19: edit>roster>anchor>S1>S2 deterministic, then LLM)")
    mapping_result = map_all(
        turns, [p for p in participants_union if p not in absent_names],
        anchor=series_speaker_anchor, roster=series_roster_entries,
        present=present_effective,  # Ф3 A5: присутствие для доменного маппинга — по
        # реальной панели Телемоста, не по union (expected-отпускник из
        # watched.yaml не делает отсутствующего владельца кандидатом). Ф2 R9:
        # минус помеченные «не было» на этой встрече.
        inactive=inactive_names,  # R3: неактивного тёзку не подставляем.
    )
    cluster_to_name: dict[str, str] = dict(mapping_result.cluster_to_name)
    sources_used: list[str] = list(mapping_result.sources_used)
    speaker_confidence: dict[str, float] = {}
    # R3/РИСК4: кластеры, авто-резолвленные дизамбигуацией тёзок, — НЕуверенные.
    # Занижаем confidence (не «решено молча») и собираем имена под ⚠️-пометку.
    uncertain_names: list[str] = []
    for _uc in mapping_result.uncertain_clusters:
        nm = cluster_to_name.get(_uc)
        if nm:
            speaker_confidence[_uc] = 0.4  # < CLARIFY_THRESHOLD (0.7)
            uncertain_names.append(nm)
    if mapping_result.unresolved_clusters:
        llm_decided = map_speaker_names(
            turns,
            expected_participants=expected_effective,
            panel_participants=present_effective,
            already_mapped=cluster_to_name,
            meeting_sid=session_uid,
            roster=series_roster_entries,
            inactive_names=inactive_names,  # R4/A5: стухший ожидаемый не в авторы.
            absent_names=absent_names,  # Ф2 R9: помеченные «не было» — не в пул.
        )
        if llm_decided:
            for cluster, (name, conf) in llm_decided.items():
                cluster_to_name[cluster] = name
                speaker_confidence[cluster] = conf
            sources_used.append("llm-postprocess")
    # Ф2 (R12/R6): company name-канон ПОВЕРХ итогового маппинга — выученное «Еремеев→
    # Саргин» переименует любой источник (детерминированный или LLM), поэтому правка
    # имени держится между перевыпусками и на будущих встречах. Строгий точный матч
    # (тёзка-безопасно). Нет фактов → no-op.
    if _company:
        cluster_to_name = correction_facts.apply_name_canon(cluster_to_name, _company)
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
    if _is_external_stt(backend) and ext_result is not None:
        # Относительный путь от папки серии — рендер кладёт его в шапку .md.
        transcript_relpath = f"_transcripts/{date_part}.txt"

    # Метки движков для шапки протокола (asr/диаризация).
    _STT_LABELS = {
        "speechmatics": ("speechmatics-enhanced", "speechmatics-enhanced"),
        "assemblyai": ("assemblyai-universal-3-pro", "assemblyai-universal-3-pro"),
    }
    asr_label, diar_label = _STT_LABELS.get(
        backend, (args.asr_model, "pyannote/speaker-diarization-3.1")
    )

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
        # 4a. JSON/TXT — для любой внешней STT-ветки (speechmatics/assemblyai).
        if _is_external_stt(backend) and ext_result is not None:
            archive_json = {
                "session_uid": session_uid,
                "series": meta.get("series"),
                "date": date_part,
                "stt_backend": backend,
                _stt_id_meta_key(backend): ext_result.job_id,
                "audio_duration_s": ext_result.audio_duration_s,
                "detected_language": ext_result.detected_language,
                "raw_json": ext_result.raw_json,
            }
            bundle.add(
                transcripts_json_path,
                json.dumps(archive_json, ensure_ascii=False, indent=2),
            )
            bundle.add(
                transcripts_txt_path,
                _format_transcript_txt(ext_result.utterances),
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
    if _is_external_stt(backend) and ext_result is not None:
        bounds = None
        try:
            from lib.protocol_to_tg import speech_bounds_ms_from_raw_json
            bounds = speech_bounds_ms_from_raw_json(ext_result.raw_json)
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
    # Ф2 (ISS-22 б): снимок ПРОШЛОЙ версии ДО регенерации (atomic-перезапись её
    # затрёт — РИСК2/A2). Обычная первая публикация → файла ещё нет → None → обычный
    # self-review (R-b4). Перефинализация уже существующей встречи → прошлая версия
    # есть → инвариант «не теряем». Для Ф3 (best-of-2) этот же `prior_sources`
    # понесёт ВТОРОЙ независимый черновик (обобщённый вход переиспользуется).
    prior_protocol_text = None
    if _is_protocol_enabled():
        if protocol_path.is_file():
            try:
                prior_protocol_text = protocol_path.read_text(encoding="utf-8")
            except OSError:
                prior_protocol_text = None
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
                open_tasks=open_tasks_block,  # Ф8 (G9): хвост открытых задач серии
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
            # Ф3 (РИСК1 «не молчаливый провал», цикл5/У2): таймаут Opus уже
            # закрыт деградацией на фолбэк (протокол доставляется), но НЕ-таймаут
            # отказ генерации (Opus недоступен на VPS / опечатка id / нет шапки)
            # иначе уходил бы только в journal — владелец узнавал бы об отсутствии
            # протокола по факту его неприхода. Активный push делает провал
            # видимым и actionable. Приватность (опасная тройка/egress): в
            # сообщение — только slug серии + дата + КЛАСС ошибки, без `str(e)`
            # (тело может нести stderr-фрагмент) и без реплик/имён. Сам push
            # best-effort (`_push_telegram` не валит finalize).
            _series = str(meta.get("series") or "—")
            _push_telegram(
                "⚠️ Протокол встречи не сгенерировался (транскрипт сохранён).\n"
                f"Серия: {_series}\n"
                f"Дата: {date_part}\n"
                f"Причина: {type(e).__name__}\n"
                f"Перегенерировать: «протокол {_series} {date_part}» "
                "или tools/regenerate-protocol.py"
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
            if ext_result is not None:
                task_meta["audioDurationS"] = ext_result.audio_duration_s
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
            # R3: имена тёзка-подстановок — чтобы поздний clarify_worker после
            # перегенерации заново поставил ⚠️ «авторство под вопросом» (паритет).
            authorship_uncertain_names=uncertain_names,
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
    # Ф4 (D2/D3): добавлена "diarization" — ТЕМ ЖЕ одним вызовом. Однозначные ошибки
    # деления по спикерам правятся в транскрипте (re-attribution), сомнительные —
    # помечаются «⚠️ спикер под вопросом» в протоколе (реплики на местах).
    # Ф7 (G8): rewrite=True — второй проход «редактор-критик» ТЕМ ЖЕ одним вызовом
    # ПЕРЕПИСЫВАЕТ черновик по чек-листу (пропущенное / задачи без владельца /
    # смешение ролей / плоское / числа-инверсии), владельцу не надо править руками.
    # 2 тяжёлых вызова суммарно (генерация + этот), НЕ третий. РИСК1: таймаут/сбой
    # второго прохода → черновик отдаётся как финал (деградация, не потеря встречи).
    if _is_protocol_enabled() and protocol_path.is_file():
        # Ф3 (ISS-22 в, best-of-2): на ПЕРВОЙ публикации (finalize) протокол
        # собирается из ДВУХ независимых черновиков. Черновик A уже на диске
        # (regenerate_protocol_for_meeting выше); генерим черновик B ТОЙ ЖЕ встречи
        # В ПАМЯТИ — без второй atomic-записи (НЕС1: она затёрла бы A) — и склеиваем
        # лучшее из обоих ТЕМ ЖЕ движком-критиком (Ф2): B уходит в prior_sources,
        # инвариант «не теряем» доносит до финала пункт, пойманный любым прогоном
        # (R-c1). Сбой/гейт-OFF второго прогона → None → одинарная версия
        # (деградация R-c2). Утяжеление ТОЛЬКО здесь — команда «протокол …» и CLI
        # на одном прогоне (R-c3). Egress — той же встречи (опасная тройка).
        draft_b = generate_alt_draft_for_best_of_2(
            transcript_path=md_path,
            meeting_meta=protocol_meta,
            meeting_sid=session_uid,
            series_memory=series_memory_block,  # тот же вход, что у черновика A
            open_tasks=open_tasks_block,
        )
        # Источники склейки: прошлая версия (если перефинализация, Ф2) + второй
        # черновик (Ф3). Обычная первая публикация → prior_protocol_text=None →
        # только B. Оба None → обычный одинарный self-review (R-b4/R-c2).
        merge_sources = [s for s in (prior_protocol_text, draft_b) if s]
        try:
            n_flags = review_and_flag_protocol_file(
                protocol_path=protocol_path,
                transcript_path=md_path,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid=session_uid,
                rewrite=True,
                # R3: ⚠️ «авторство под вопросом, поправьте» для тёзка-подстановок
                # (system-applied, входит в content-hash). Паритет в clarify_worker.
                extra_findings=build_authorship_uncertainty_findings(uncertain_names),
                # Ф2 (ISS-22 б) + Ф3 (best-of-2): прошлая версия и/или второй черновик
                # → инвариант «не теряем задачи/решения». РИСК4: склейка идёт ЧЕРЕЗ
                # ЭТУ файл-обёртку (полный checks-tuple + сайд-эффекты finalize:
                # diarization-fix в транскрипт, дисклеймер, ⚠️ авторства), НЕ мимо неё.
                prior_sources=merge_sources or None,
            )
            if n_flags:
                log.info("[review] meeting=%s self-review изменил %d пункт(ов)", session_uid, n_flags)
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
            _digest_meta = dict(meta)
            _digest_meta["date"] = date_part
            _digest_meta["expectedParticipants"] = expected
            _digest_meta["participants"] = participants
            # Ф6 (E1–E5): вердикт гейта публикации знания — по РЕАЛЬНОМУ составу
            # (panel `participants`, A5: присутствие, не приглашённые) + разметке
            # серии (watched.yaml) + ростеру. Оседает в памяти серии как метаданные
            # (PII-free, без сырья) — это достижимость гейта из реальной
            # финализации; саму публикацию делает Ф7, читая этот вердикт.
            # Fail-closed: любой сбой → private (знание НЕ утекает).
            _publication = {"allowed": False, "visibility": "private",
                            "company": None, "reason": "gate-error"}
            try:
                # A5: гейт судит по тому же составу, что и шапка протокола —
                # панель Телемоста ∪ реально говорившие (имена кластеров голоса,
                # cluster_to_name), НЕ только панель. Иначе озвучившийся-но-не-в-
                # панели аутсайдер минул бы предохранитель круга E5 (fail-open).
                _voiced_for_gate = [n for n in (cluster_to_name or {}).values()
                                    if isinstance(n, str) and n.strip()]
                _present_for_gate = publication_gate.present_participants_for_gate(
                    expected, participants, _voiced_for_gate)
                _decision = publication_gate.decide_for_meeting(meta.get("series"), _present_for_gate)
                _publication = _decision.as_metadata()
                # Только метаданные (опасная тройка): серия/видимость/причина/счётчик.
                log.info("[publication] meeting=%s series=%s visibility=%s reason=%s present=%d",
                         session_uid, meta.get("series"), _decision.visibility,
                         _decision.reason, len(_present_for_gate or []))
            except Exception as _e:  # noqa: BLE001
                log.warning("[publication] gate failed (non-fatal, fail-closed private): %s", _e)
            # B1: build→save→prune одним вызовом (тестируемая единица, см.
            # series_memory.save_meeting_digest). Прунинг старых выжимок ПО ТЕКУЩЕЙ
            # серии — ОТДЕЛЬНОЙ операцией внутри (не в save_digest), иначе бэкфилл
            # самоудалял бы свежий результат. participant_filter — defense-in-depth:
            # шапка тут уже чистая (Ф1 :623), но фильтр держит инвариант «UI-мусор
            # в память серии не течёт» единообразно с бэкфиллом (Ф1 §5).
            series_memory.save_meeting_digest(
                series_dir, date_part, _proto_for_digest, _digest_meta,
                speaker_mapping=cluster_to_name,  # Ф4б (REQ 1.2): несём авторство в память серии
                participant_filter=filter_participant_names,
                publication=_publication,  # Ф6: вердикт гейта (PII-free) в память серии
                prune_days=series_memory.retention_days(),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("[series-memory] digest save failed (non-fatal): %s", e)

    # 4.0.2e. Ф9 (B1–B6): дистилляция durable-знания из ГОТОВОГО протокола в мозг.
    # Идёт ПОСЛЕ self-review (Ф7) и публикационного гейта — по ФИНАЛЬНОМУ протоколу
    # (не по сырому транскрипту). Маршрут ТОЛЬКО через knowledge_router; COMPANY
    # проходит G11+гейт ДО записи; чувствительное/спорное → воскресная очередь, НЕ
    # молча в *-context. В headless flush gated токеном → COMPANY лишь копится в
    # outbox (PR делает провижининг владельца). Best-effort: сбой → finalize не
    # валится (протокол на диске и доставляется ниже). Достижимо из ОБОИХ триггеров
    # (finalize + clarify) — память review-checks-two-call-sites.
    if _is_protocol_enabled() and protocol_path.is_file():
        try:
            from lib import knowledge_distill  # noqa: PLC0415
            _voiced_distill = [n for n in (cluster_to_name or {}).values()
                               if isinstance(n, str) and n.strip()]
            _present_distill = publication_gate.present_participants_for_gate(
                expected, participants, _voiced_distill)
            _distill_res = knowledge_distill.distill_and_route(
                protocol_path.read_text(encoding="utf-8"),
                series=meta.get("series"),
                present_participants=_present_distill,
                date=date_part,
                meeting_sid=session_uid,
            )
            # B6: только счётчики (без текста факта/реплик).
            log.info("[distill] meeting=%s status=%s cand=%d company=%d private=%d sunday=%d",
                     session_uid, _distill_res.get("status"), _distill_res.get("candidates", 0),
                     _distill_res.get("company", 0), _distill_res.get("private", 0),
                     _distill_res.get("sunday", 0))
        except Exception as e:  # noqa: BLE001
            log.warning("[distill] failed (non-fatal): %s", e)

    # 4.0.3. Ф6: доставка протокола в Telegram-группу.
    # Идемпотентность через `meta.delivered` в meta.json. Если привязки
    # series→chat_id в watched.yaml нет — `deliver_protocol` сам спросит
    # Илью через notarius-бота (state в `_pending_clarification/...delivery.json`),
    # listener подберёт ответ. Best-effort: на сбой LLM/IO/Telegram —
    # warning, finalize не валится; протокол на диске уже есть, можно
    # переотправить вручную (через Ф6.x CLI или повторную финализацию).
    delivery_result = {"status": "not-run"}
    # ISS-20 #4/#5 (инцидент 2026-06-23): пустую/провальную запись НЕ шлём в
    # групповой чат серии (бот не должен позориться плашкой «не состоялась» перед
    # командой). Вместо этого — ЛИЧНЫЙ алерт владельцу, чтобы он узнал о провале
    # от бота, а не от участников встречи, и мог зайти руками.
    _end_reason = str(meta.get("endReason") or meta.get("end_reason") or "").strip()
    # None (а не 0) для внутреннего whisper_pyannote: пустоту по utterances
    # детектим только у внешнего STT (прод = AssemblyAI), иначе ext_result=None
    # ложно пометил бы каждую внутреннюю встречу как провал. endReason работает
    # на любом бэкенде.
    _utter_count = len(ext_result.utterances) if ext_result is not None else None
    _recording_failed = (
        _end_reason in {"no_one_joined", "deaf_with_participants"}
        or _utter_count == 0
    )
    if _recording_failed:
        _series_lbl = meta.get("series") or "?"
        if _end_reason == "deaf_with_participants":
            _why = "бот был в комнате и видел людей, но не получил звук (вероятный сбой захвата аудио)"
        elif _end_reason == "no_one_joined":
            _why = "за время ожидания в комнате так никто и не появился"
        else:
            _why = "запись получилась пустой — ни одной распознанной реплики"
        _room_url = str(meta.get("meetingUrl") or "").strip()
        _room_line = f"\nКомната: {_room_url}" if _room_url else ""
        try:
            from lib import notify  # noqa: PLC0415
            notify.push_via_notarius(
                f"⚠️ Ватсон: встреча «{_series_lbl}» {date_part} НЕ записана.\n"
                f"Причина: {_why}.{_room_line}\n"
                f"Зайти руками сейчас — скажи Альфреду: «подключи Ватсона к этой встрече»."
            )
        except Exception as _e:  # noqa: BLE001
            log.warning("[delivery] owner-alert failed (non-fatal): %s", _e)
        # ISS-20 ход1: пометить встречу ОБРАБОТАННОЙ в meta.delivered. Иначе
        # collector (скип только по непустому delivered — collector.py
        # `_delivery_done`) КАЖДЫЙ тик (5 мин) пере-финализирует пустую запись и
        # шлёт алерт повторно (спам владельцу + лишний прогон STT). Пишем через
        # канонический единый писатель meta (РИСК1) синтетический маркер:
        # message_ids=[0] непустой → `_delivery_done`=True; decision помечает суть.
        try:
            from lib.llm_postprocess import _update_meta_delivered  # noqa: PLC0415
            _update_meta_delivered(
                _delivery_marker_meta_path(args.meta_json),
                {
                    "chat_id": 0,
                    "message_ids": [0],
                    "at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "decision": "owner-alert-failed-recording",
                },
            )
        except Exception as _e:  # noqa: BLE001
            log.warning("[delivery] failed-recording marker write failed (non-fatal): %s", _e)
        delivery_result = {"status": "skipped-failed-recording", "endReason": _end_reason}
        log.info(
            "[delivery] провальная запись (endReason=%s, utter=%d) → личный алерт владельцу; "
            "в групповой чат НЕ шлём", _end_reason or "—",
            (_utter_count if _utter_count is not None else 0),
        )
    elif protocol_path.is_file():
        try:
            protocol_text_for_delivery = protocol_path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("[delivery] protocol read failed: %s", e)
            protocol_text_for_delivery = ""
        if protocol_text_for_delivery.strip():
            delivery_meta = dict(meta)
            delivery_meta["date"] = date_part
            delivery_meta["sessionUid"] = session_uid
            # REQ 1.1 (ISS-1): даём deliver_protocol точные пути транскрипта и
            # протокола — он персистит их в delivered-marker meta, чтобы reissue
            # нашёл транскрипт напрямую (не угадывал `<date>.md`, который эфемерен).
            delivery_meta["transcript_path"] = str(md_path)
            delivery_meta["protocol_path"] = str(protocol_path)
            # Идемпотентность доставки: `delivered` пишем в ИСХОДНУЮ meta встречи
            # (`args.meta_json`), а не в output-dir рядом с протоколом. Это ровно
            # тот файл, который читают дедуп collector'а и reply-gate правок —
            # см. `_delivery_marker_meta_path` (Ф2, REQ 3.1/3.2, РИСК1).
            meta_json_path = _delivery_marker_meta_path(args.meta_json)
            try:
                delivery_result = deliver_protocol(
                    meeting_meta=delivery_meta,
                    protocol_text=protocol_text_for_delivery,
                    meta_json_path=meta_json_path,
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

    if _is_external_stt(backend) and ext_result is not None:
        log.info("Transcripts archive → %s, %s", transcripts_json_path, transcripts_txt_path)
        # Append в bench-<backend>-prod.log — семидневный мониторинг (как у SM, Ф4).
        # Формат: <iso_ts> <series> <date> wav=<sec> finalize=<sec> job=<id>
        try:
            bench_path = f"/srv/meeting-notary/logs/bench-{backend}-prod.log"
            series_label = meta.get("series") or session_uid
            audio_sec = round(ext_result.audio_duration_s, 1)
            finalize_sec = round(time.time() - t_start, 1)
            job_id = ext_result.job_id
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
        _stt_friendly = {
            "speechmatics": "Speechmatics",
            "assemblyai": "AssemblyAI",
            "whisper_pyannote": "Whisper",
        }.get(backend, backend)
        _push_telegram(
            f"✅ Встреча `{series_label} {date_part}` обработана после {n} попыток ({_stt_friendly})."
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
    if _is_external_stt(backend):
        result_json.update({
            "audio_duration_s": extra.get("audio_duration_s"),
            # id внешнего job'а под backend-специфичным ключом (speechmatics_job_id
            # / assemblyai_transcript_id) — чтобы аудит-trail совпадал с meta/архивом.
            _stt_id_meta_key(backend): extra.get(_stt_id_meta_key(backend)),
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
