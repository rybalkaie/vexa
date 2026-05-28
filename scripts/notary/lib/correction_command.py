"""Парсер Telegram-команды коррекции протокола (Ф6).

Распознаёт сообщения вида:
  - «поправь протокол <series> <date>: <инструкция>»
  - «удали задачу <N> из <series> <date>»
  - «<series> <date>: задачу X не было»  (структурная — instruction = весь хвост)

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
    kind: str  # "fix_protocol" | "remove_task" | "tail_negate"


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
