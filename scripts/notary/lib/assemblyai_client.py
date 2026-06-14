"""Клиент AssemblyAI (universal-3-pro) для meeting-notary — Слой 1, Ф1.

Зачем (план `plans/2026-06-13-umnyi-protokol-assemblyai.md`, Фаза 1, REQ S1/S2/S3/S5/S6/D1):
    Speechmatics делает системные ошибки слов (инверсии смысла, выдуманные
    имена, путаница брендов). Бенч 2026-06-13 на встрече 1123 показал, что AAI
    `universal-3-pro` заметно чище. Этот модуль — сетевой слой переезда: тот же
    внешний класс STT, что Speechmatics, новый класс риска ПДн НЕ добавляет (AA3).

Образцы (переиспользуем логику, не изобретаем):
    - `~/Projects/anzhee-b2b-portal/lib/dealer360/assemblyaiClient.ts` +
      `assemblyaiParser.ts` — рабочий клиент upload→create→poll + парсер utterances[].
    - `lib/speechmatics_client.py` — контракт-аналог в этом же проекте (классы
      ошибок, обрезка тела ошибки `[:80]`, не-тихий сбой, идемпотентность,
      kill-switch). `assemblyai_client` строится по тому же контракту.

Поток AAI (3 шага, сверено гейтом реальности dealer360 2026-06-12 с docs AAI):
    1. POST /v2/upload  — сырые байты WAV (octet-stream) → { upload_url }.
    2. POST /v2/transcript — JSON-конфиг с audio_url → { id, status }.
    3. GET  /v2/transcript/{id} — поллинг до status ∈ completed | error.
       Завершённый объект УЖЕ несёт text + utterances[] + words[] (отдельного
       fetch'а транскрипта, как у Speechmatics, нет).

Отличия от Speechmatics (вывод гейта реальности, перепутать = 401/битый парс):
    - Auth: заголовок `authorization: <KEY>` БЕЗ префикса 'Bearer'.
    - Модель: `speech_models: ['universal-3-pro']` (множественный массив;
      сингулярный `speech_model` депрекейтнут). Без авто-фолбэка на universal-2.
    - `speakers_expected` НЕ форсируем (AA2): авто-определение числа голосов.
    - Тайминги utterances/words — в МИЛЛИСЕКУНДАХ (целые). Наш контракт
      `Utterance.start/end` — СЕКУНДЫ float (как у Speechmatics), поэтому мс→сек.
    - Метки спикеров — буквы 'A'/'B'/'C' (Speechmatics давал 'S1'/'S2').

Контракт `Utterance` (тот же, что у Speechmatics — pipeline един для обеих веток):
    speaker: str     — сырая метка AAI ("A", "B", ...), без переименования
    start:   float   — секунды float от начала аудио
    end:     float
    text:    str     — реплика как пришла от AAI (пунктуация уже внутри)

Adapter `to_aligned_turns(utterances)` маппит speaker "A"→"SPEAKER_00",
"B"→"SPEAKER_01" (стабильно по порядку появления) — тот же выход, что у
Speechmatics-адаптера, поэтому render.py / name_mapping.py / merge работают без
правок (S2).

Надёжность (S5): ретраи на 5xx/timeout с жёстким лимитом 1 попытка/вызов; общий
потолок поллинга (wall-clock + max attempts); kill-switch перед новым сабмитом
(тот же глобальный флаг STT, что у Speechmatics CG7 — РИСК3: поминутный STT +
Opus×2 = runaway-риск); идемпотентность по transcript id (existing_transcript_id +
callback фиксирует id ДО поллинга — переживает краш, не платим дважды).

Приватность (S6, «Опасная тройка»): `ASSEMBLYAI_API_KEY` НЕ логируется и НЕ
кладётся в текст ошибок. Тело ошибки провайдера обрезается `[:80]` (РИСК2: может
содержать эхо `keyterms` с именами участников — keyterms заведены в Ф2 (S4),
обрезка их не светит). Само тело create-запроса (с `keyterms_prompt`) НЕ
логируется. Текст транскрипта НЕ логируется — только метаданные
(длительность, число спикеров/слов/реплик, время).

Smoke CLI:
    python -m notary.lib.assemblyai_client <wav_path>
печатает первые 10 реплик в формате `Speaker A: текст`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

import httpx


logger = logging.getLogger(__name__)


ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com/v2"
LANGUAGE_CODE = "ru"
SPEECH_MODEL = "universal-3-pro"

POLL_INTERVAL_S = 5.0
POLL_TIMEOUT_S = 60 * 60 * 3  # 3 часа — формальный потолок, как у Speechmatics
MAX_POLL_ATTEMPTS = 3000      # жёсткий лимит числа опросов (≈ POLL_TIMEOUT_S/interval + запас)
HTTP_TIMEOUT_S = 120.0        # per-request; upload крупнее, но WAV нотариуса небольшие
RETRY_BACKOFF_S = 30.0


class Utterance(NamedTuple):
    speaker: str
    start: float
    end: float
    text: str


class TranscriptionResult(NamedTuple):
    """Результат upload+create+poll одного транскрипта (аналог Speechmatics).

    Поля совпадают по смыслу со Speechmatics-результатом, чтобы финализатор
    обрабатывал обе ветки единым кодом:
      utterances        — list[Utterance] (контракт фазы)
      audio_duration_s  — длительность аудио, секунды
      detected_language — language_code из ответа AAI ("ru")
      raw_json          — сырой завершённый transcript-объект AAI (как пришёл;
                          несёт utterances[]/words[]/text — для архива и clean-time)
      job_id            — id транскрипта AAI (для аудит-trail и идемпотентности).
                          Имя поля `job_id` (а не transcript_id) намеренно: совпадает
                          со Speechmatics-результатом → downstream-код общий.
    """
    utterances: list["Utterance"]
    audio_duration_s: float
    detected_language: str
    raw_json: dict
    job_id: str


class AssemblyAIError(RuntimeError):
    """Любая ошибка взаимодействия с AssemblyAI, не лечащаяся одним retry."""


class AssemblyAIRejectedError(AssemblyAIError):
    """Транскрипт завершился со status=error — терминально (битое аудио/конфиг).

    Аналог `SpeechmaticsRejectedError`: повторять автоматически бесполезно,
    финализатор кладёт в _failed с attempts=99 (ручной разбор)."""


class AssemblyAIKillSwitchError(AssemblyAIError):
    """CG7-аналог: недельный kill-switch STT взведён — новый ПЛАТНЫЙ сабмит запрещён.

    Подкласс AssemblyAIError намеренно (как у Speechmatics): финализатор уже умеет
    ловить базовый класс и класть встречу в retry-очередь (не теряется), но main()
    обрабатывает этот подтип РАНЬШЕ — чтобы не жечь ретраи и слать «на паузе».
    Снимается только вручную (CG8). Флаг общий со Speechmatics — это потолок ВСЕГО
    STT-расхода, не отдельного движка."""


# --- CG7: kill-switch недельного лимита STT (общий флаг-файл) -----------------
# Тот же глобальный флаг, что у Speechmatics (`stt_weekly_guard.py` взводит,
# человек снимает — CG8). Существование файла = «расшифровка остановлена».
# Реализовано локально (а не импортом из speechmatics_client), чтобы не тащить
# зависимость между движками; путь/env идентичны — флаг ровно один на оба STT.
DEFAULT_KILLSWITCH_PATH = "/srv/meeting-notary/state/stt-killswitch.flag"


def killswitch_path() -> Path:
    """Путь флага kill-switch (env STT_KILLSWITCH_PATH, иначе дефолт VPS)."""
    return Path(os.environ.get("STT_KILLSWITCH_PATH") or DEFAULT_KILLSWITCH_PATH)


def killswitch_armed() -> bool:
    """CG7: взведён ли kill-switch (читается на каждый сабмит, не кэшируется)."""
    try:
        return killswitch_path().exists()
    except OSError:
        # Недоступность FS трактуем как «не взведён» — не блокируем расшифровку
        # из-за инфраструктурного сбоя проверки (ложный блок хуже ложного пропуска).
        return False


# --- РИСК3: локальный леджер трат AAI (источник для недельного сторожа) --------
# Аккаунт AssemblyAI ОБЩИЙ с другими проектами (ключ переиспользуется), поэтому
# считать недельный расход нотариуса через list-API аккаунта НЕЛЬЗЯ — он смешает
# чужие транскрипты, и недельный kill-switch может ложно сработать (заблокировать
# нотариус из-за чужого объёма). Вместо этого пишем СВОЙ леджер: одна строка JSONL
# на каждый НОВЫЙ (платный) сабмит. `stt_weekly_guard` читает только его — считает
# исключительно нотариусные часы. Reuse существующего id НЕ пишем (деньги потрачены
# на первом сабмите — повторный учёт был бы завышением).
DEFAULT_SPEND_LEDGER_PATH = "/srv/meeting-notary/state/aai-spend.jsonl"


def spend_ledger_path() -> Path:
    """Путь леджера трат AAI (env AAI_SPEND_LEDGER_PATH, иначе дефолт VPS)."""
    return Path(os.environ.get("AAI_SPEND_LEDGER_PATH") or DEFAULT_SPEND_LEDGER_PATH)


def record_spend(transcript_id: str, audio_duration_s: float, *, now: dt.datetime | None = None) -> None:
    """Best-effort: дописать трату в леджер. НИКОГДА не фатально для финализации.

    Строка JSONL: {"ts": <iso-utc>, "transcript_id": ..., "duration_s": <сек>}.
    Сбой записи (нет каталога/прав/диск) только логируем — расшифровка уже сделана,
    ронять её из-за учёта нельзя (ложный провал финализации хуже недосчёта трат).
    Зовётся ТОЛЬКО на новом сабмите (не на reuse) — иначе двойной учёт."""
    try:
        stamp = (now or dt.datetime.utcnow()).replace(microsecond=0).isoformat() + "Z"
        try:
            dur = float(audio_duration_s)
        except (TypeError, ValueError):
            dur = 0.0
        if dur < 0:
            dur = 0.0
        p = spend_ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": stamp, "transcript_id": str(transcript_id or ""), "duration_s": round(dur, 1)},
            ensure_ascii=False,
        )
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning("AAI spend-ledger: не смог записать трату (не фатально): %s", e)


def _get_api_key() -> str:
    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        # Сообщение НЕ содержит значения ключа (его и нет).
        raise AssemblyAIError(
            "ASSEMBLYAI_API_KEY не задан в окружении. "
            "На VPS значение лежит в /srv/meeting-notary/.env.notary."
        )
    return key


def _auth_headers() -> dict:
    """Auth-заголовок AAI: голый ключ, БЕЗ 'Bearer' (вывод гейта реальности)."""
    return {"authorization": _get_api_key()}


def _is_retryable_http_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _call_with_retry(fn, *, action: str):
    """Вызвать fn(); на 5xx/timeout сделать ровно 1 retry через 30 сек, иначе raise.

    Жёсткий лимит ретраев = 1/вызов (S5): runaway по сетевым ретраям невозможен.
    Текст ошибки в логе обрезаем `[:80]` (РИСК2): тело провайдера может содержать
    эхо keyterms с именами участников (Ф2) — не светим в логи/retry-state."""
    try:
        return fn()
    except BaseException as e:
        if not _is_retryable_http_error(e):
            raise
        logger.warning(
            "AssemblyAI %s упал (%s: %s), ретрай через %.0f сек",
            action, type(e).__name__, str(e)[:80], RETRY_BACKOFF_S,
        )
        time.sleep(RETRY_BACKOFF_S)
        return fn()


# --- HTTP-шаги (вынесены модульными функциями — мокаются в тестах) ------------

def _upload_audio(client: httpx.Client, headers: dict, wav_path: Path) -> str:
    """Шаг 1. POST /v2/upload — сырые байты WAV → upload_url.

    Файл открывается ВНУТРИ _do, чтобы retry перечитал его с начала (позиция
    потока на повторе иначе уже в конце). httpx стримит file-like как тело."""
    def _do():
        with open(wav_path, "rb") as f:
            r = client.post(
                f"{ASSEMBLYAI_BASE_URL}/upload",
                headers={**headers, "content-type": "application/octet-stream"},
                content=f,
            )
            r.raise_for_status()
            data = r.json()
        url = data.get("upload_url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise AssemblyAIError("upload: в ответе нет upload_url")
        return url

    return _call_with_retry(_do, action="upload")


def _create_transcript(
    client: httpx.Client,
    headers: dict,
    audio_url: str,
    *,
    keyterms_prompt: list[str] | None = None,
) -> str:
    """Шаг 2. POST /v2/transcript — создать задание с конфигом → id.

    Конфиг Ф1: universal-3-pro, ru, speaker_labels. speakers_expected НЕ ставим
    (AA2 — авто-определение).

    `keyterms_prompt` (Ф2, S4) — доменный словарь серии (имена участников + бренды
    + термины ниши), уже нормализованный и обрезанный под лимиты AAI вызывателем
    (`keyterms.build_keyterms_prompt`: ≤1000 терминов, ≤6 слов/фраза). Пусто/None →
    поле в тело НЕ кладётся (поведение Ф1 неизменно). РИСК2: список содержит ИМЕНА
    участников — тело задания НЕ логируется (тут и нигде), в текст ошибок не попадает.

    Взаимоисключение `keyterms_prompt` ↔ `prompt` (S4): API запрещает оба сразу.
    Мы `prompt` не используем (форматный контроль — на Claude, Ф3), поэтому в теле
    его и нет; keyterms кладём всегда, когда он непуст. Гард-инвариант ниже: если в
    `body` когда-нибудь окажется непустой `prompt`, keyterms НЕ добавляется (выбор:
    keyterms молча уступает `prompt`), чтобы запрос остался валидным."""
    body = {
        "audio_url": audio_url,
        "speech_models": [SPEECH_MODEL],
        "language_code": LANGUAGE_CODE,
        "speaker_labels": True,
    }
    # S4: keyterms и prompt взаимоисключающи. `prompt` мы не задаём — гард на случай
    # будущей правки конфига, чтобы не отправить невалидную пару (keyterms уступает).
    if keyterms_prompt and not body.get("prompt"):
        body["keyterms_prompt"] = list(keyterms_prompt)

    def _do():
        r = client.post(
            f"{ASSEMBLYAI_BASE_URL}/transcript",
            headers={**headers, "content-type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        tid = data.get("id") if isinstance(data, dict) else None
        if not isinstance(tid, str) or not tid:
            raise AssemblyAIError("create transcript: в ответе нет id")
        return tid

    return _call_with_retry(_do, action="create")


def _get_transcript(client: httpx.Client, headers: dict, transcript_id: str) -> dict:
    """Шаг 3 (одиночный опрос). GET /v2/transcript/{id} → весь transcript-объект."""
    def _do():
        r = client.get(
            f"{ASSEMBLYAI_BASE_URL}/transcript/{transcript_id}",
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise AssemblyAIError("poll transcript: ответ не объект")
        return data

    return _call_with_retry(_do, action=f"poll id={transcript_id}")


def _poll_until_done(
    client: httpx.Client, headers: dict, transcript_id: str, *, sleep=time.sleep
) -> dict:
    """Поллить транскрипт до completed; вернуть завершённый объект.

    status=error → AssemblyAIRejectedError (терминально). Потолок: wall-clock
    POLL_TIMEOUT_S И MAX_POLL_ATTEMPTS (двойная защита от бесконечного цикла)."""
    t0 = time.time()
    for attempt in range(MAX_POLL_ATTEMPTS):
        data = _get_transcript(client, headers, transcript_id)
        status = data.get("status")
        if status == "completed":
            return data
        if status == "error":
            # Поле error AAI — про аудио; обрезаем [:80] на случай эха keyterms (РИСК2).
            reason = str(data.get("error") or "unknown")[:80]
            raise AssemblyAIRejectedError(
                f"transcript {transcript_id} завершился со status=error: {reason}"
            )
        if time.time() - t0 > POLL_TIMEOUT_S:
            raise AssemblyAIError(
                f"transcript {transcript_id} не дошёл до completed за "
                f"{POLL_TIMEOUT_S/60:.0f} мин (статус: {status})"
            )
        sleep(POLL_INTERVAL_S)
    raise AssemblyAIError(
        f"transcript {transcript_id} не завершился за {MAX_POLL_ATTEMPTS} опросов"
    )


# --- Парсер utterances[] → Utterance (порт assemblyaiParser.ts) ---------------

def _to_seconds(value) -> float:
    """Тайминг AAI (мс, целое) → секунды float. Битое/отрицательное → 0.0."""
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return 0.0
    if ms < 0:
        return 0.0
    return round(ms / 1000.0, 3)


def _clean_speaker_label(speaker) -> str:
    """Метка говорящего: 'A'/'B'/… как есть; null/''/не-строка → '?'."""
    if not isinstance(speaker, str):
        return "?"
    trimmed = speaker.strip()
    return trimmed if trimmed else "?"


def parse_utterances(raw: dict) -> list[Utterance]:
    """Сырой transcript-объект AAI → list[Utterance].

    Защитный (как dealer360-парсер): реплики без текста выбрасываем, битые
    тайминги → 0.0. Тайминги мс→сек. Чистая функция — тестируется напрямую."""
    raw_utts = raw.get("utterances") if isinstance(raw, dict) else None
    if not isinstance(raw_utts, list):
        return []
    out: list[Utterance] = []
    for u in raw_utts:
        if not isinstance(u, dict):
            continue
        text = u.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            continue
        out.append(Utterance(
            speaker=_clean_speaker_label(u.get("speaker")),
            start=_to_seconds(u.get("start")),
            end=_to_seconds(u.get("end")),
            text=text,
        ))
    return out


def _extract_audio_duration_s(raw: dict, utterances: list[Utterance]) -> float:
    """Длительность аудио в секундах.

    AAI кладёт `audio_duration` (секунды) в завершённый объект — берём его.
    Фолбэк: max end по words[] (мс→сек), иначе по utterances, иначе 0.0."""
    try:
        dur = float(raw.get("audio_duration"))
        if dur > 0:
            return dur
    except (TypeError, ValueError):
        pass
    words = raw.get("words")
    if isinstance(words, list) and words:
        last = max(
            (_to_seconds(w.get("end")) for w in words if isinstance(w, dict)),
            default=0.0,
        )
        if last > 0:
            return last
    if utterances:
        return max(u.end for u in utterances)
    return 0.0


def _extract_detected_language(raw: dict) -> str:
    """language_code из ответа AAI ("ru" для нашего конфига); 'unknown' если нет."""
    code = raw.get("language_code") if isinstance(raw, dict) else None
    return str(code) if code else "unknown"


# --- Высокоуровневый вызов ----------------------------------------------------

def transcribe_diarize_wav(
    wav_path: str | Path,
    *,
    existing_transcript_id: str | None = None,
    on_transcript_created=None,
    keyterms_prompt: list[str] | None = None,
    sleep=time.sleep,
) -> TranscriptionResult:
    """Прогнать WAV через AssemblyAI и вернуть TranscriptionResult.

    Шаги: (kill-switch) → upload → create → poll → parse. На 5xx/timeout — 1 retry
    через 30 сек на каждом шаге; на status=error → AssemblyAIRejectedError.

    `keyterms_prompt` (Ф2, S4) — доменный словарь серии для биаса распознавания,
    уже под лимитами AAI. Используется ТОЛЬКО при новом сабмите (в create); при
    reuse существующего транскрипта create не делается, словарь игнорируется. Пусто/
    None → поле не передаётся (Ф1-поведение). РИСК2: список с именами НЕ логируется.

    Идемпотентность (S5, «по transcript id»):
      `existing_transcript_id` — id из прошлого прогона той же встречи. Если задан —
        upload/create НЕ делаем (не платим дважды), сразу поллим существующий
        транскрипт. kill-switch при reuse НЕ проверяется (деньги уже потрачены).
      `on_transcript_created(transcript_id)` — callback, вызывается СРАЗУ после
        create, ДО поллинга. Финализатор фиксирует им id в meta, чтобы краш во
        время поллинга не привёл к потере id (и второму платному сабмиту).
      kill-switch (CG7) — перед НОВЫМ сабмитом проверяем `killswitch_armed()`;
        взведён → AssemblyAIKillSwitchError.

    `sleep` инъектируется (тесты передают noop — без реального ожидания).
    """
    p = Path(wav_path)
    # WAV нужен ТОЛЬКО для нового сабмита. При reuse существующего транскрипта
    # (idempotency) файл не читается — протокол восстановим по transcript id уже
    # ПОСЛЕ чистки WAV. Когда reuse'ить нечего — сабмит неизбежен, проверяем файл.
    if not existing_transcript_id and not p.exists():
        raise FileNotFoundError(f"WAV не найден: {p}")

    headers = _auth_headers()
    t0 = time.time()

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            if existing_transcript_id:
                transcript_id = existing_transcript_id
                logger.info(
                    "AssemblyAI: переиспользую существующий transcript %s "
                    "— upload/create НЕ делаю (идемпотентность по id)",
                    transcript_id,
                )
            else:
                # Новый сабмит — здесь и только здесь тратятся деньги, поэтому
                # здесь же гейт kill-switch (CG7).
                if not p.exists():
                    raise FileNotFoundError(f"WAV не найден: {p}")
                size_mb = p.stat().st_size / 1024 / 1024
                logger.info("AssemblyAI submit: %s (%.1f MB)", p.name, size_mb)
                if killswitch_armed():
                    raise AssemblyAIKillSwitchError(
                        "kill-switch недельного лимита STT взведён — "
                        f"новый сабмит запрещён ({killswitch_path()})"
                    )
                upload_url = _upload_audio(client, headers, p)
                transcript_id = _create_transcript(
                    client, headers, upload_url, keyterms_prompt=keyterms_prompt
                )
                logger.info("AssemblyAI transcript created: %s", transcript_id)
                # Отдать id наверх ДО поллинга (переживает краш/рестарт).
                if on_transcript_created is not None:
                    try:
                        on_transcript_created(transcript_id)
                    except Exception as cb_e:  # noqa: BLE001
                        logger.warning(
                            "on_transcript_created callback упал (не фатально): %s",
                            cb_e,
                        )
            raw = _poll_until_done(client, headers, transcript_id, sleep=sleep)
    except AssemblyAIError:
        raise
    except httpx.HTTPStatusError as e:
        # 4xx (401/403/422) — НЕ retryable. Тело обрезаем [:80]: может содержать
        # эхо keyterms с именами (РИСК2), которые иначе попадут в retry-state/push.
        snippet = (e.response.text or "").strip()[:80]
        raise AssemblyAIError(f"HTTP {e.response.status_code}: {snippet}") from e
    except (httpx.RequestError, OSError, ValueError) as e:
        # Сетевые сбои, кривой JSON, не-ASCII в заголовках и т.п. Текст [:80].
        raise AssemblyAIError(f"{type(e).__name__}: {str(e)[:80]}") from e

    utterances = parse_utterances(raw)
    audio_duration_s = _extract_audio_duration_s(raw, utterances)
    if not existing_transcript_id:
        # Новый платный сабмит — фиксируем трату в леджер для недельного сторожа
        # (РИСК3). Reuse сюда не попадает (деньги уже учтены на первом сабмите).
        record_spend(transcript_id, audio_duration_s)
    detected_language = _extract_detected_language(raw)
    words = raw.get("words")
    n_words = len(words) if isinstance(words, list) else 0
    logger.info(
        "AssemblyAI: %d utterances, %d спикеров, %d слов, duration=%.1f сек, "
        "lang=%s, общее время %.1f сек (id=%s)",
        len(utterances),
        len({u.speaker for u in utterances}),
        n_words,
        audio_duration_s,
        detected_language,
        time.time() - t0,
        transcript_id,
    )
    return TranscriptionResult(
        utterances=utterances,
        audio_duration_s=audio_duration_s,
        detected_language=detected_language,
        raw_json=raw,
        job_id=transcript_id,
    )


def to_aligned_turns(utterances: list[Utterance]):
    """Адаптер: Utterance → AlignedTurn существующего pipeline (как у Speechmatics).

    Маппинг speaker'а AAI ("A", "B", ...) → формат pyannote ("SPEAKER_00",
    "SPEAKER_01", ...) стабильно по порядку появления — чтобы render.py
    (`_speaker_label` + `cluster_to_index`) и name_mapping.py работали на тех же
    ключах кластеров без правок (S2)."""
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
    """CLI: `python -m notary.lib.assemblyai_client <wav>` — печать первых 10 реплик."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) != 2:
        print("Usage: python -m notary.lib.assemblyai_client <wav_path>", file=sys.stderr)
        return 2

    result = transcribe_diarize_wav(sys.argv[1])
    utterances = result.utterances

    print(f"\n=== Получено utterances: {len(utterances)} ===")
    print(f"Transcript ID: {result.job_id}")
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
