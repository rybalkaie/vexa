"""Парсер Telegram-команды генерации протокола (Ф4).

Распознаёт сообщения вида:
  - «протокол <series> <date>»
  - «сгенерируй протокол <series> <date>»
  - «перегенерируй протокол <series> <date>»
  - «перегенерируй <series> <date>»

Регистронезависимо, гибкие разделители (пробелы), дата в формате YYYY-MM-DD.

Этот модуль импортируется из `meetings_listener.py` (под `venv-cli` без
pyannote/torch), поэтому стдлиб-only и не тянет ничего из llm_postprocess
на верхнем уровне.
"""
from __future__ import annotations

import re
from typing import Optional

_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Глагол-триггер: «сгенерируй», «перегенерируй», «сделай», «обнови».
# Слово «протокол» (опционально с окончанием) — обязательно ИЛИ присутствует
# глагол + слово «протокол» в любом порядке. Series — токен с латиницей/
# дефисами/подчёркиваниями/цифрами (имя папки `~/Projects/me/встречи/<series>/`).
_VERB_RE = re.compile(
    r"(?i)\b(с?генерируй|перегенерируй|пересгенерируй|сделай|обнови|сделать)\b"
)
_PROTOKOL_WORD_RE = re.compile(r"(?i)\bпротокол(?:а|у|ы)?\b")
# Series-токен = имя папки в `~/Projects/me/встречи/`. Имя может начинаться
# с цифры (`2026-05-27-tm-1779869180376` и т.п. — реальные one-off папки до
# Ф7-миграции). Минимум 3 символа, чтобы не цепляться за случайные числа.
_SERIES_TOKEN_RE = re.compile(
    r"\b([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9._-]{1,}[A-Za-zА-Яа-яЁё0-9])\b"
)


def parse_protocol_command(text: str) -> Optional[tuple[str, str]]:
    """Возвращает `(series, date)` если text — команда генерации протокола, иначе None.

    Триггер — хотя бы одно из:
      - есть глагол («сгенерируй»/«перегенерируй»/«сделай»/«обнови»),
      - есть слово «протокол».
    Плюс обязательно: дата YYYY-MM-DD + series-токен ДО даты.

    Series-токен берётся как последний «слово»-токен (латиница/цифры/-) перед датой.

    Примеры что распознаётся:
      «протокол sales-quality 2026-05-27»
      «сгенерируй протокол sales-quality 2026-05-27»
      «перегенерируй sales-quality 2026-05-27»
      «обнови протокол anzhee-direktorat 2026-06-01»

    Что НЕ распознаётся:
      «по протоколу sales-quality 2026-05-27 договорились» — нет глагола
        и слово «протоколу» не точно «протокол» (но это сейчас прошло бы по
        regex; гасим: если в исходной строке после даты ещё есть «не-пробельный»
        текст — это уже не команда, а разговор о протоколе).
    """
    if not text or not text.strip():
        return None
    raw = text.strip()

    # Берём ПОСЛЕДНЮЮ дату YYYY-MM-DD в строке: команда выглядит как
    # «протокол <series> <date>», а series-имя папки само может содержать
    # дату (`2026-05-27-tm-…`). Последняя дата = дата встречи; всё до неё —
    # глагол + series-токен (включая префиксную дату внутри series).
    date_matches = list(_DATE_RE.finditer(raw))
    if not date_matches:
        return None
    date_match = date_matches[-1]
    date_str = date_match.group(1)

    # «date в середине разговорной фразы» — НЕ команда: если после даты ещё
    # есть значимый текст (не пробелы / точка / восклицание), считаем что
    # это не команда.
    tail = raw[date_match.end():].strip(".!?…\n ")
    if tail:
        return None

    has_verb = bool(_VERB_RE.search(raw))
    has_protokol = bool(_PROTOKOL_WORD_RE.search(raw))
    if not (has_verb or has_protokol):
        return None

    # Series — последний токен перед датой.
    before = raw[:date_match.start()]
    tokens = _SERIES_TOKEN_RE.findall(before)
    if not tokens:
        return None
    series = tokens[-1]
    # Защита от ложноположительных: если series — это сам глагол или слово
    # «протокол», команда невалидна.
    if _VERB_RE.fullmatch(series) or _PROTOKOL_WORD_RE.fullmatch(series):
        return None
    return series, date_str
