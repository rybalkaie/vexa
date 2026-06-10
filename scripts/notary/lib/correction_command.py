"""Парсер Telegram-команды коррекции протокола (Ф6) + смены чата серии (Ф3 ISS-7).

Распознаёт сообщения вида:
  - «поправь протокол <series> <date>: <инструкция>»
  - «удали задачу <N> из <series> <date>»
  - «<series> <date>: задачу X не было»  (структурная — instruction = весь хвост)
  - «серию <X> шли сюда / в личку / в чат <id>»  (kind=set_chat, БЕЗ даты —
    привязка к серии, не к встрече; цель в `target`). REQ 7.4: владелец в любой
    момент переназначает серию→чат. Подробный синтаксис — у `_parse_set_chat`.

Регистронезависимо, гибкие разделители (пробелы / двоеточие), дата YYYY-MM-DD.

Этот модуль импортируется из `meetings_listener.py` (под `venv-cli` без
pyannote/torch), поэтому стдлиб-only и не тянет ничего из llm_postprocess
на верхнем уровне.

Не перекрывается с `protocol_command.parse_protocol_command` (Ф4):
  - Ф4 распознаёт «протокол <series> <date>» (только генерация).
  - Ф6 распознаёт «поправь/удали задачу N» — обязательно с инструкцией ИЛИ
    с упоминанием задачи к удалению. В listener'е Ф6 проверяется ДО Ф4-парсера
    или ПОСЛЕ (см. комментарий в meetings_listener.process_message).
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional


_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_SERIES_TOKEN_RE = re.compile(
    r"\b([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9._-]{1,}[A-Za-zА-Яа-яЁё0-9])\b"
)

# Формы триггеров.
_FIX_PROTOCOL_RE = re.compile(
    r"(?i)^\s*поправь\s+протокол\s+",
)
_REMOVE_TASK_RE = re.compile(
    r"(?i)^\s*удали(?:ть)?\s+задачу\s+(\d+)\s+из\s+",
)
# «<series> <date>: задачу X не было» — третья форма: ничего особенного впереди,
# двоеточие после даты, в инструкции «не было».
_TAIL_NEGATE_RE = re.compile(
    r"(?i)\bзадач(?:и|у|ы|а)\s+.+\s+не\s+было\b",
)


class CorrectionCommand(NamedTuple):
    series: str
    date: str
    instruction: str
    kind: str  # "fix_protocol" | "remove_task" | "tail_negate" | "set_chat"
    target: str = ""  # только для set_chat: "here" | "dm" | "<chat_id>"


# ----- Ф3 (ISS-7/REQ 7.4): команда смены чата серии -----
# Триггер требует ОДНОВРЕМЕННО три компонента (слово «серию» + глагол отправки +
# цель). Это узкое пересечение защищает от ложноположительных на обычной переписке
# и на других командах коррекции (у них нет «серию»+глагол+цель сразу). Дополнительно
# отправителя-владельца проверяет листенер (owner-гейт) перед применением.
_SET_CHAT_SERIES_WORD_RE = re.compile(r"(?i)\bсери[июяйе]\w*")
_SET_CHAT_VERB_RE = re.compile(
    r"(?i)\b(?:шли|шлите|шлёт|шлю|пошли|пошлите|слать|посыла\w+|отправ\w+|"
    r"присыл\w+|направл\w+|перенаправ\w+|кидай\w*|привяж\w+|переключ\w+|закреп\w+)\b"
)
_SET_CHAT_TARGET_ID_RE = re.compile(r"(?i)(?:в\s+чат|чат|в\s+группу|группу)\s+(-?\d{4,})")
_SET_CHAT_TARGET_DM_RE = re.compile(
    r"(?i)(?:в\s+личк\w+|в\s+лс\b|в\s+личн\w+\s+сообщени\w+|в\s+директ\b|мне\s+в\s+личк\w+)"
)
_SET_CHAT_TARGET_HERE_RE = re.compile(
    r"(?i)(?:сюда|сюды|в\s+этот\s+чат|в\s+текущий\s+чат|в\s+эту\s+группу)"
)
# Для извлечения ссылки на серию вычитаем из текста цель/глагол/слово-«серию»/мусор.
_SET_CHAT_TARGET_ANY_RE = re.compile(
    r"(?i)(?:в\s+чат\s+-?\d{4,}|в\s+группу\s+-?\d{4,}|чат\s+-?\d{4,}|"
    r"в\s+личк\w+|в\s+лс\b|в\s+личн\w+\s+сообщени\w+|в\s+директ\b|"
    r"сюда|сюды|в\s+этот\s+чат|в\s+текущий\s+чат|в\s+эту\s+группу)"
)
_SET_CHAT_FILLER_RE = re.compile(
    r"(?i)\b(?:эту|эта|этой|эти|это|этому|этого|данн\w+|текущ\w+|теперь|давай(?:те)?|"
    r"пожалуйста|плиз|please|бот[ау]?|нотариус[ауе]?|нужно|надо|прошу|протокол\w*|"
    r"настро\w+|для|please)\b"
)


def _parse_set_chat(raw: str) -> Optional[tuple[str, str]]:
    """Распознаёт команду смены чата серии. Возвращает `(series_ref, target)` или None.

    Синтаксис (регистронезависимо, порядок слов гибкий — диктовка Wispr Flow):
      «серию <X> шли сюда»            → (X, "here")  — привязать к ТЕКУЩЕМУ чату
      «серию <X> шли в личку»         → (X, "dm")    — слать в личку владельца
      «серию <X> шли в чат <id>»      → (X, "<id>")  — явный chat_id (для DM)
      «эту серию шли сюда» (reply)    → ("", "here") — серия из контекста реплая
      также «шли серию <X> сюда», «отправляй серию <X> в этот чат» и пр.

    `<X>` — slug (`anzhee-direktorat`) ИЛИ отображаемое имя («Директорат»);
    резолв в каноничный slug — на стороне листенера (`resolve_series_ref`).
    Пустой `series_ref` означает «серия из реплая на протокол» (резолвит листенер).

    Требует все три: слово «серию/серия» + глагол отправки + распознанную цель —
    иначе None (не наша команда, не перехватываем).
    """
    if not raw or not raw.strip():
        return None
    # Явные команды коррекции (форма 1 «поправь протокол…» / форма 2 «удали
    # задачу N из…») имеют приоритет — не перехватываем их, даже если в инструкции
    # случайно встретилось «серию … шли … сюда».
    if _FIX_PROTOCOL_RE.match(raw) or _REMOVE_TASK_RE.match(raw):
        return None
    low = raw.lower()
    if not _SET_CHAT_SERIES_WORD_RE.search(low):
        return None
    if not _SET_CHAT_VERB_RE.search(low):
        return None
    # Цель: явный chat_id > личка > текущий чат. Нет цели → не команда смены чата.
    target: Optional[str] = None
    m_id = _SET_CHAT_TARGET_ID_RE.search(raw)
    if m_id:
        target = m_id.group(1)
    elif _SET_CHAT_TARGET_DM_RE.search(low):
        target = "dm"
    elif _SET_CHAT_TARGET_HERE_RE.search(low):
        target = "here"
    else:
        return None
    # Ссылка на серию = текст минус цель, глагол, слово-«серию», демонстративы/мусор.
    work = _SET_CHAT_TARGET_ANY_RE.sub(" ", raw, count=1)
    work = _SET_CHAT_VERB_RE.sub(" ", work, count=1)
    work = _SET_CHAT_SERIES_WORD_RE.sub(" ", work, count=1)
    work = _SET_CHAT_FILLER_RE.sub(" ", work)
    series_ref = re.sub(r"\s+", " ", work).strip(" :«»\"'.,!?—–-")
    return (series_ref, target)


def parse_correction_command(text: str) -> Optional[CorrectionCommand]:
    """Возвращает `CorrectionCommand` или None если не распознано.

    Распознаёт 3 формы:
      1. «поправь протокол <series> <date>: <инструкция>»
         → kind=fix_protocol, instruction=<хвост после двоеточия>
      2. «удали задачу <N> из <series> <date>»
         → kind=remove_task, instruction=<вся строка>
      3. «<series> <date>: задачу X не было / неправильная / лишняя»
         → kind=tail_negate, instruction=<хвост после двоеточия>

    Series-токен в форме 2 берётся как первый kebab/snake-токен после «из».
    В форме 1 — первый токен после «протокол». В форме 3 — последний токен
    перед датой (как в protocol_command).
    """
    if not text or not text.strip():
        return None
    raw = text.strip()

    # Ф3 (ISS-7/REQ 7.4): команда смены чата серии — БЕЗ даты (привязка к серии,
    # а не к конкретной встрече). Проверяем ДО обязательной даты ниже. Узкий
    # триггер (серию+глагол+цель) не пересекается с формами 1–3 коррекции.
    set_chat = _parse_set_chat(raw)
    if set_chat is not None:
        ref, target = set_chat
        return CorrectionCommand(series=ref, date="", instruction="", kind="set_chat", target=target)

    # Дата — обязательна (последнее вхождение в строке).
    date_matches = list(_DATE_RE.finditer(raw))
    if not date_matches:
        return None
    date_match = date_matches[-1]
    date_str = date_match.group(1)

    # Форма 1: «поправь протокол <series> <date>: <инструкция>»
    fix_m = _FIX_PROTOCOL_RE.match(raw)
    if fix_m:
        body = raw[fix_m.end():]  # «<series> <date>: <инструкция>»
        series, tail = _extract_series_and_tail(body, date_str)
        if series and tail.startswith(":"):
            instr = tail.lstrip(":").strip()
            if instr:
                return CorrectionCommand(series, date_str, instr, "fix_protocol")
        elif series:
            # «поправь протокол X 2026-05-28 убери задачу 3» — без двоеточия,
            # тоже принимаем (хвост после даты — инструкция).
            instr = tail.strip()
            if instr:
                return CorrectionCommand(series, date_str, instr, "fix_protocol")
        return None

    # Форма 2: «удали задачу N из <series> <date>»
    rm_m = _REMOVE_TASK_RE.match(raw)
    if rm_m:
        body = raw[rm_m.end():]  # «<series> <date>»
        series, tail = _extract_series_and_tail(body, date_str)
        if series:
            tail_clean = tail.strip(".!?…\n :")
            if not tail_clean:
                return CorrectionCommand(series, date_str, raw, "remove_task")
        return None

    # Форма 3: «<series> <date>: задачу X не было»
    if _TAIL_NEGATE_RE.search(raw):
        # Series — последний токен перед датой.
        before = raw[: date_match.start()]
        tokens = _SERIES_TOKEN_RE.findall(before)
        if not tokens:
            return None
        series = tokens[-1]
        # Защита от ложноположительных: series не может быть «задачу», «удали».
        bad = {"задачу", "задача", "удали", "поправь", "протокол"}
        if series.lower() in bad:
            return None
        tail = raw[date_match.end():].lstrip(":").strip()
        if not tail:
            return None
        return CorrectionCommand(series, date_str, tail, "tail_negate")

    return None


def _extract_series_and_tail(body: str, date_str: str) -> tuple[Optional[str], str]:
    """Из `<series> <date>[: ...]` (тело после триггера) достаёт `(series, tail)`.

    tail — всё после даты (включая ведущее двоеточие если есть).
    Возвращает `(None, body)` если series-токен перед датой не найден.
    """
    date_idx = body.find(date_str)
    if date_idx < 0:
        return (None, body)
    before = body[:date_idx]
    tokens = _SERIES_TOKEN_RE.findall(before)
    if not tokens:
        return (None, body)
    series = tokens[-1]
    bad = {"задачу", "задача", "удали", "поправь", "протокол"}
    if series.lower() in bad:
        return (None, body)
    tail = body[date_idx + len(date_str):]
    return (series, tail)
