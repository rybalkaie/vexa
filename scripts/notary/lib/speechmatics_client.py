"""Клиент Speechmatics Batch API для meeting-notary.

ADR (почему Speechmatics, а не Whisper-GPU/Deepgram/AssemblyAI):
    ~/Projects/anzhee-dealer-360/decisions/2026-05-27-stt-speechmatics.md

Контракт `Utterance` (фиксируется этим модулем как внешний контракт фазы Ф1):
    speaker: str     — идентификатор от Speechmatics ("S1", "S2", ...), без переименования
    start:   float   — секунды float от начала аудио
    end:     float
    text:    str     — реплика с пунктуацией (пунктуация приклеена к предыдущему слову
                        без пробела перед знаком — естественная русская типографика)

Парсер `results[]` обрабатывает type=word и type=punctuation:
    - punctuation всегда приклеивается к текущему буферу без пробела,
    - новая реплика открывается только при смене speaker'а на word'е.

Retry (Ф1, синхронный, для smoke и Ф2-интеграции):
    - 5xx/timeout при submit/poll/fetch: 1 быстрый retry через 30 сек, иначе raise.
    - job со статусом `rejected` → сразу raise (конфиг-ошибка, retry не лечит).
    - Полная 24-часовая retry-очередь живёт на уровне finalize-meeting.py в Ф2
      (`_failed/` + systemd-timer), здесь НЕ дублируется.

Adapter для существующего pipeline (используется в Ф2):
    to_aligned_turns(utterances) -> list[AlignedTurn]
        Маппит speaker "S1"→"SPEAKER_00", "S2"→"SPEAKER_01" (стабильно по порядку
        появления) и собирает AlignedTurn с полями start/end/speaker/text/display_name.
        Не правит остальной pipeline (merge_consecutive_same_speaker / name_mapping /
        render_protocol работают на AlignedTurn без изменений).

Smoke CLI:
    python -m notary.lib.speechmatics_client <wav_path>
печатает первые 10 реплик в формате `Speaker S1: текст`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

import httpx


logger = logging.getLogger(__name__)


SPEECHMATICS_BASE_URL = "https://asr.api.speechmatics.com/v2"
LANGUAGE = "ru"
OPERATING_POINT = "enhanced"

POLL_INTERVAL_S = 5.0
POLL_TIMEOUT_S = 60 * 60 * 3  # 3 часа — формальный потолок Batch API ~2 ч, дадим запас
HTTP_TIMEOUT_S = 120.0
RETRY_BACKOFF_S = 30.0

VOCAB_CONFIG_PATH = os.environ.get(
    "SPEECHMATICS_VOCAB_PATH",
    "/srv/meeting-notary/config/speechmatics-vocab.json",
)
_VOCAB_CACHE: list[dict] | None = None  # читаем диск один раз за процесс


class Utterance(NamedTuple):
    speaker: str
    start: float
    end: float
    text: str


class TranscriptionResult(NamedTuple):
    """Расширенный результат submit+poll+fetch одного job'а.

    Используется финализатором (Ф2): помимо utterances ему нужны
    audio_duration_s и detected_language (заменяют возврат старого
    transcribe_wav), а также raw_json для сохранения архивной транскрипции
    в `_transcripts/<date>.json` и job_id для аудит-trail.
    """
    utterances: list["Utterance"]
    audio_duration_s: float        # results[-1].end_time (metadata.audio_duration отсутствует)
    detected_language: str          # metadata.language_pack_info.language_description
    raw_json: dict                  # сырой ответ Speechmatics (json-v2), как пришёл
    job_id: str                     # для аудит-trail (Speechmatics хранит ~7 дней)


class SpeechmaticsError(RuntimeError):
    """Любая ошибка взаимодействия со Speechmatics, не лечащаяся одним retry."""


class SpeechmaticsRejectedError(SpeechmaticsError):
    """Job отвергнут — это конфиг-ошибка (формат/lang/audio), retry бесполезен."""


def _get_api_key() -> str:
    key = os.environ.get("SPEECHMATICS_API_KEY")
    if not key:
        raise SpeechmaticsError(
            "SPEECHMATICS_API_KEY не задан в окружении. "
            "На VPS значение лежит в /srv/meeting-notary/.env.notary."
        )
    return key


def _load_additional_vocab() -> list[dict]:
    """Прочитать словарь терминов для Speechmatics из VOCAB_CONFIG_PATH.

    Лечит галлюцинации на собственных именах/брендах (Битрикс→Беатрикс,
    нейросеть→Евросеть и т.п.). Файл отсутствует / некорректный JSON →
    возвращаем [] (не ронять job из-за словаря).
    """
    global _VOCAB_CACHE
    if _VOCAB_CACHE is not None:
        return _VOCAB_CACHE
    path = Path(VOCAB_CONFIG_PATH)
    if not path.exists():
        logger.info("Speechmatics vocab не найден (%s) — работаем без additional_vocab", path)
        _VOCAB_CACHE = []
        return _VOCAB_CACHE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        vocab = data.get("additional_vocab") or []
        if not isinstance(vocab, list):
            raise ValueError(f"additional_vocab должен быть list, получили {type(vocab).__name__}")
        logger.info("Speechmatics vocab загружен из %s: %d терминов", path, len(vocab))
        _VOCAB_CACHE = _merge_auto_vocab(vocab)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Speechmatics vocab битый (%s: %s) — работаем без него", type(e).__name__, e)
        _VOCAB_CACHE = []
    return _VOCAB_CACHE


def _merge_auto_vocab(main_vocab: list[dict]) -> list[dict]:
    """Дослить авто-словарь Ф8 (`speechmatics-vocab-auto.json`) к главному.

    Главный побеждает при совпадении `content` (case-insensitive). Авто-файл
    отсутствует/битый → возвращаем главный без изменений. Импорт ленивый, чтобы
    клиент не зависел от пакета auto_vocab, если того нет (изоляция Ф8).
    """
    try:
        auto_path = Path(
            os.environ.get(
                "SPEECHMATICS_VOCAB_AUTO_PATH",
                str(Path(VOCAB_CONFIG_PATH).with_name(
                    Path(VOCAB_CONFIG_PATH).stem + "-auto" + Path(VOCAB_CONFIG_PATH).suffix
                )),
            )
        )
        if not auto_path.exists():
            return main_vocab
        auto_data = json.loads(auto_path.read_text(encoding="utf-8"))
        auto_entries = auto_data.get("additional_vocab") or []
        if not isinstance(auto_entries, list):
            return main_vocab
        have = {
            (e.get("content") or "").strip().lower()
            for e in main_vocab
            if isinstance(e, dict)
        }
        added = 0
        merged = list(main_vocab)
        for e in auto_entries:
            if not isinstance(e, dict) or not isinstance(e.get("content"), str):
                continue
            key = e["content"].strip().lower()
            if key and key not in have:
                have.add(key)
                merged.append(e)
                added += 1
        if added:
            logger.info("Speechmatics vocab: +%d авто-терминов из %s (итого %d)",
                        added, auto_path, len(merged))
        return merged
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("auto-vocab не дослит (%s: %s) — только главный", type(e).__name__, e)
        return main_vocab


def _is_retryable_http_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _call_with_retry(fn, *, action: str):
    """Вызвать fn(); на 5xx/timeout сделать ровно 1 retry через 30 сек, иначе raise."""
    try:
        return fn()
    except BaseException as e:
        if not _is_retryable_http_error(e):
            raise
        logger.warning(
            "Speechmatics %s упал (%s: %s), ретрай через %.0f сек",
            action, type(e).__name__, str(e)[:200], RETRY_BACKOFF_S,
        )
        time.sleep(RETRY_BACKOFF_S)
        return fn()


# REQ 6.1 — диапазон «мягкого» нуджа sensitivity. Speechmatics default = 0.5.
# Держим потолок 0.7, чтобы не уехать в переосегментацию (дробление одного
# человека на несколько — обратный баг). См. _build_speaker_diarization_config.
_SENSITIVITY_DEFAULT = 0.5
_SENSITIVITY_CEILING = 0.7
_SENSITIVITY_STEP = 0.05


def _build_speaker_diarization_config(expected_speakers: int | None) -> dict:
    """REQ 6.1: мягкая подсказка диаризации по ожидаемому составу серии.

    Speechmatics Batch v2 НЕ принимает «ожидаемое число спикеров» как
    soft-hint. `speaker_diarization_config` реально умеет:
      - `speaker_sensitivity` (0.0–1.0, дефолт 0.5): выше → детектится больше
        спикеров (меньше склеек разных людей), ниже → меньше (больше склеек);
      - `max_speakers` — ЖЁСТКИЙ потолок числа спикеров.

    `max_speakers=N` ЗАПРЕЩЁН (РИСК3 плана): незапланированный гость был бы
    принудительно склеен с кем-то из ожидаемых — это баг #5 наоборот. Поэтому
    жёсткий потолок НЕ ставим вовсе. Ожидаемый состав используем как
    НЕ-форсирующий ориентир: при известном составе из ≥2 человек поднимаем
    `speaker_sensitivity` выше дефолта, чтобы алгоритм меньше склеивал разных
    ожидаемых участников (наблюдаемый баг #5: Ольга+Дарья ушли в один кластер).
    Это ГЛОБАЛЬНЫЙ мягкий knob, не привязка к конкретным именам — точную
    привязку имён делает claude-постмаппинг (`map_speaker_names`) и ревью
    ролей 6.2. Потолок 0.7 не даёт уехать в дробление одного человека.

    Env:
      - `SPEECHMATICS_DIARIZATION_HINT` (дефолт ON; `0/false/no` → OFF) —
        kill-switch на случай, если Speechmatics начнёт отвергать конфиг
        в проде (отключение без передеплоя).
      - `SPEECHMATICS_SPEAKER_SENSITIVITY` — явное фиксированное значение
        (прод-тюнинг), перебивает авто-нудж.

    Возвращает dict для `transcription_config["speaker_diarization_config"]`
    или `{}` (тогда поле не добавляется — дефолтное поведение Speechmatics).
    """
    raw_flag = (os.environ.get("SPEECHMATICS_DIARIZATION_HINT") or "").strip().lower()
    if raw_flag in ("0", "false", "no"):
        return {}
    env_sens = (os.environ.get("SPEECHMATICS_SPEAKER_SENSITIVITY") or "").strip()
    if env_sens:
        try:
            s = max(0.0, min(1.0, float(env_sens)))
            return {"speaker_sensitivity": round(s, 2)}
        except ValueError:
            logger.warning(
                "SPEECHMATICS_SPEAKER_SENSITIVITY не число (%r) — игнор", env_sens
            )
    if not expected_speakers or expected_speakers < 2:
        # Состав неизвестен / соло-встреча → не вмешиваемся (дефолт API).
        return {}
    sensitivity = min(
        _SENSITIVITY_DEFAULT + _SENSITIVITY_STEP * expected_speakers,
        _SENSITIVITY_CEILING,
    )
    return {"speaker_sensitivity": round(sensitivity, 2)}


def _submit_job(
    client: httpx.Client,
    headers: dict,
    wav_path: Path,
    *,
    expected_speakers: int | None = None,
) -> str:
    transcription_config: dict = {
        "language": LANGUAGE,
        "diarization": "speaker",
        "operating_point": OPERATING_POINT,
    }
    diar_cfg = _build_speaker_diarization_config(expected_speakers)
    if diar_cfg:
        transcription_config["speaker_diarization_config"] = diar_cfg
        # INFO: редкое событие (раз на job), полезно для прод-аудита эффекта 6.1.
        # Числа/имена участников НЕ логируем — только производный sensitivity.
        logger.info(
            "Speechmatics speaker_diarization_config=%s (expected_speakers≈%s)",
            diar_cfg, expected_speakers,
        )
    vocab = _load_additional_vocab()
    if vocab:
        transcription_config["additional_vocab"] = vocab
        # Аудит того, что реально ушло в job-конфиг Speechmatics (Ф2 доработок).
        # DEBUG, не INFO: вызывается на каждый job, не зашумляем боевые логи —
        # факт загрузки словаря уже логируется один раз в _load_additional_vocab.
        logger.debug("transcription_config.additional_vocab size=%d", len(vocab))
    config = {
        "type": "transcription",
        "transcription_config": transcription_config,
    }

    def _do():
        with open(wav_path, "rb") as f:
            files = {
                "data_file": (wav_path.name, f, "audio/wav"),
                "config": (None, json.dumps(config), "application/json"),
            }
            r = client.post(f"{SPEECHMATICS_BASE_URL}/jobs", headers=headers, files=files)
            r.raise_for_status()
            return r.json()["id"]

    return _call_with_retry(_do, action="submit")


def _poll_until_done(client: httpx.Client, headers: dict, job_id: str) -> None:
    t0 = time.time()
    while True:
        def _do():
            r = client.get(f"{SPEECHMATICS_BASE_URL}/jobs/{job_id}", headers=headers)
            r.raise_for_status()
            return r.json()

        data = _call_with_retry(_do, action=f"poll job={job_id}")
        status = (data.get("job") or {}).get("status")
        if status == "done":
            return
        if status == "rejected":
            raise SpeechmaticsRejectedError(
                f"Job {job_id} rejected by Speechmatics: {json.dumps(data, ensure_ascii=False)[:500]}"
            )
        elapsed = time.time() - t0
        if elapsed > POLL_TIMEOUT_S:
            raise SpeechmaticsError(
                f"Job {job_id} не дошёл до done за {POLL_TIMEOUT_S/60:.0f} мин (статус: {status})"
            )
        time.sleep(POLL_INTERVAL_S)


def _fetch_transcript(client: httpx.Client, headers: dict, job_id: str) -> dict:
    def _do():
        r = client.get(
            f"{SPEECHMATICS_BASE_URL}/jobs/{job_id}/transcript",
            headers=headers,
            params={"format": "json-v2"},
        )
        r.raise_for_status()
        return r.json()

    return _call_with_retry(_do, action=f"fetch transcript job={job_id}")


def _parse_results(results: list[dict]) -> list[Utterance]:
    """Сборка Utterance из results[].

    Новая реплика открывается только при смене speaker'а на элементе type=word.
    Пунктуация (type=punctuation) приклеивается к текущему буферу без пробела.
    """
    utterances: list[Utterance] = []
    cur_speaker: str | None = None
    cur_start: float = 0.0
    cur_end: float = 0.0
    buf: str = ""

    def _flush() -> None:
        nonlocal buf
        if cur_speaker is not None and buf.strip():
            utterances.append(Utterance(
                speaker=cur_speaker,
                start=cur_start,
                end=cur_end,
                text=buf.strip(),
            ))
        buf = ""

    for r in results:
        rtype = r.get("type")
        if rtype not in ("word", "punctuation"):
            continue
        alt = (r.get("alternatives") or [{}])[0]
        sp = alt.get("speaker") or "UU"
        content = alt.get("content", "")
        start = float(r.get("start_time", 0.0))
        end = float(r.get("end_time", start))

        if rtype == "word":
            if sp != cur_speaker:
                _flush()
                cur_speaker = sp
                cur_start = start
            if buf and not buf.endswith(" "):
                buf += " "
            buf += content
            cur_end = end
        else:
            buf += content
            if end > cur_end:
                cur_end = end

    _flush()
    return utterances


def _extract_audio_duration_s(raw: dict, results: list[dict]) -> float:
    """Длительность аудио в секундах.

    Speechmatics НЕ кладёт audio_duration в metadata (проверено эмпирически
    на job r667dbbx7y, Ф1, ход 5). Берём `end_time` последнего word/punctuation;
    если results пустой — пробуем metadata.duration (на случай если API
    добавит поле в будущем), иначе 0.0.
    """
    for r in reversed(results):
        if r.get("type") in ("word", "punctuation"):
            try:
                return float(r.get("end_time", 0.0))
            except (TypeError, ValueError):
                return 0.0
    md = raw.get("metadata") or {}
    try:
        return float(md.get("duration") or md.get("audio_duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _extract_detected_language(raw: dict) -> str:
    """Из metadata.language_pack_info.language_description ("Russian" для language=ru).

    Fallback на metadata.transcription_config.language ("ru") если поле отсутствует.
    """
    md = raw.get("metadata") or {}
    lang_pack = (md.get("language_pack_info") or {})
    desc = lang_pack.get("language_description")
    if desc:
        return str(desc)
    cfg = (md.get("transcription_config") or {})
    code = cfg.get("language")
    return str(code or "unknown")


def transcribe_diarize_wav(
    wav_path: str | Path,
    *,
    expected_speakers: int | None = None,
) -> TranscriptionResult:
    """Прогнать WAV через Speechmatics Batch API и вернуть TranscriptionResult.

    Шаги: submit → poll status → fetch json-v2 → parse results.
    На 5xx/timeout — 1 retry через 30 сек на каждом шаге; на `rejected` job → raise.

    `expected_speakers` (REQ 6.1) — ожидаемое число участников серии. Мягкий
    ориентир диаризации (НЕ жёсткий лимит): подмешивается в job-конфиг через
    `_build_speaker_diarization_config`. None / <2 → дефолтное поведение API.

    Возврат — `TranscriptionResult` NamedTuple с полями utterances, audio_duration_s,
    detected_language, raw_json, job_id. `result.utterances` остаётся `list[Utterance]`
    с тем же контрактом что зафиксирован в Ф1.
    """
    p = Path(wav_path)
    if not p.exists():
        raise FileNotFoundError(f"WAV не найден: {p}")

    headers = {"Authorization": f"Bearer {_get_api_key()}"}
    logger.info("Speechmatics submit: %s", p.name)
    t0 = time.time()

    try:
        # stat() внутри try: broken symlink / race с unlink / permission denied
        # дадут OSError, который ниже завернётся в SpeechmaticsError для _failed/.
        size_mb = p.stat().st_size / 1024 / 1024
        logger.info("Speechmatics file size: %.1f MB", size_mb)
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            job_id = _submit_job(client, headers, p, expected_speakers=expected_speakers)
            logger.info("Speechmatics job submitted: %s", job_id)
            _poll_until_done(client, headers, job_id)
            logger.info("Speechmatics job done за %.1f сек, тяну transcript", time.time() - t0)
            raw = _fetch_transcript(client, headers, job_id)
    except SpeechmaticsError:
        raise
    except httpx.HTTPStatusError as e:
        # 4xx (401/403/422) — НЕ retryable, но финализатор должен зачислить в _failed/.
        # body НЕ цитируем больше 80 char — может содержать эхо additional_vocab
        # с именами сотрудников, которые потом попадут в retry-state.json и push.
        snippet = (e.response.text or "").strip()[:80]
        raise SpeechmaticsError(
            f"HTTP {e.response.status_code}: {snippet}"
        ) from e
    except (httpx.RequestError, OSError, ValueError) as e:
        # Сетевые сбои, кривой ответ JSON, не-ASCII в заголовках и т.п.
        raise SpeechmaticsError(f"{type(e).__name__}: {str(e)[:200]}") from e

    results = raw.get("results", [])
    utterances = _parse_results(results)
    audio_duration_s = _extract_audio_duration_s(raw, results)
    detected_language = _extract_detected_language(raw)
    logger.info(
        "Speechmatics: %d results-элементов → %d utterances, %d спикеров, "
        "duration=%.1f сек, lang=%s, общее время %.1f сек",
        len(results),
        len(utterances),
        len({u.speaker for u in utterances}),
        audio_duration_s,
        detected_language,
        time.time() - t0,
    )
    return TranscriptionResult(
        utterances=utterances,
        audio_duration_s=audio_duration_s,
        detected_language=detected_language,
        raw_json=raw,
        job_id=job_id,
    )


def to_aligned_turns(utterances: list[Utterance]):
    """Адаптер для Ф2-интеграции: Utterance → AlignedTurn существующего pipeline.

    Маппинг speaker'а Speechmatics ("S1", "S2", ...) → формат pyannote
    ("SPEAKER_00", "SPEAKER_01", ...) выполняется стабильно по порядку появления.
    Это нужно, чтобы render.py (`_speaker_label` + `cluster_to_index`) и
    name_mapping.py продолжили работать на тех же ключах кластеров.
    """
    from .align import AlignedTurn

    speaker_to_pyannote: dict[str, str] = {}
    out: list[AlignedTurn] = []
    for u in utterances:
        if u.speaker not in speaker_to_pyannote:
            speaker_to_pyannote[u.speaker] = f"SPEAKER_{len(speaker_to_pyannote):02d}"
        out.append(AlignedTurn(
            start=u.start,
            end=u.end,
            speaker=speaker_to_pyannote[u.speaker],
            text=u.text,
            display_name=None,
        ))
    return out


def _smoke_main() -> int:
    """CLI: `python -m notary.lib.speechmatics_client <wav>` — печать первых 10 реплик."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) != 2:
        print("Usage: python -m notary.lib.speechmatics_client <wav_path>", file=sys.stderr)
        return 2

    wav_path = sys.argv[1]
    result = transcribe_diarize_wav(wav_path)
    utterances = result.utterances

    print(f"\n=== Получено utterances: {len(utterances)} ===")
    print(f"Job ID: {result.job_id}")
    print(f"Audio duration: {result.audio_duration_s:.1f}s")
    print(f"Detected language: {result.detected_language}")
    speakers = sorted({u.speaker for u in utterances})
    print(f"Спикеры: {speakers}")
    if utterances:
        print(f"Длительность по тайм-кодам: {utterances[0].start:.1f}s … {utterances[-1].end:.1f}s")
    print("\n=== Первые 10 реплик ===")
    for u in utterances[:10]:
        print(f"[{u.start:7.2f}–{u.end:7.2f}] Speaker {u.speaker}: {u.text}")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_main())
