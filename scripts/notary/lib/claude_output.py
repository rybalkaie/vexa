"""Разбор вывода клиента Claude (`claude --print --output-format json|stream-json`).

Чистые функции, только stdlib. Зовёт обёртка `claude_cli`: по ним она находит итоговое
событие `result` (ответ модели — его поле `result`, какая модель ответила — `modelUsage`)
и отличает поток событий клиента от ответа модели, который сам JSON.

Зачем отдельно: с выводом JSON (Ф4 второго круга моделей, 18.10.2026) ответ перестал быть
«stdout как есть» — клиент может напечатать конверт среди посторонних строк, в несколько
строк или списком событий (`verbose` в его настройках). Отдать такой вывод потребителю как
ответ нельзя: сводка правок ушла бы людям сырым JSON, разметка имён молча обнулилась бы.

Опасная тройка: функции текст ответа не печатают и не логируют.
"""

from __future__ import annotations

import json
from typing import Optional

# Типы событий клиента (`system/init`, `assistant`, `user`, `result`, `stream_event`).
EVENT_TYPES = frozenset({"system", "assistant", "user", "result", "stream_event"})


def json_values(raw: str) -> list:
    """JSON-значения вывода клиента: весь вывод целиком, иначе — каждое значение,
    которое начинается с начала строки (строки до и после конверта — не JSON: «Update
    available», предупреждения; конверт может быть и в несколько строк)."""
    try:
        return [json.loads(raw)]
    except ValueError:
        pass
    dec = json.JSONDecoder()
    out: list = []
    pos = 0
    for ln in raw.splitlines(keepends=True):
        start, pos = pos, pos + len(ln)
        stripped = ln.lstrip()
        if not stripped.startswith(("{", "[")):
            continue
        at = start + (len(ln) - len(stripped))
        if out and at < out[-1][0]:
            continue  # строка внутри уже разобранного значения
        try:
            obj, end = dec.raw_decode(raw, at)
        except ValueError:
            continue
        out.append((end, obj))
    return [obj for _, obj in out]


def result_envelope(stdout: Optional[str]) -> Optional[dict]:
    """Конверт `--output-format json` (`type == "result"`) или None, если вывод — не он.

    Ищет итоговое событие во всём выводе: один конверт; список событий (так клиент
    печатает `json` при включённом `verbose` в настройках) — последнее `result` в нём;
    конверт среди посторонних строк до и после.
    """
    raw = (stdout or "").strip()
    if not raw:
        return None
    found = None
    for obj in json_values(raw):
        items = obj if isinstance(obj, list) else [obj]
        for item in items:
            if isinstance(item, dict) and item.get("type") == "result":
                found = item
    return found


def is_events_without_result(stdout: Optional[str]) -> bool:
    """Вывод — события клиента (одно, список или построчно), а итогового `result` нет.

    Так выглядит обрыв или чужой режим вывода. Ответ модели, который сам JSON (массив
    сопоставления имён, объект разбора), сюда не попадает: у событий клиента есть `type`
    из его словаря и `session_id`/`message`/`subtype`.
    """
    raw = (stdout or "").strip()
    if not raw.startswith(("{", "[")):
        return False
    items: list = []
    for obj in json_values(raw):
        items.extend(obj if isinstance(obj, list) else [obj])
    return bool(items) and all(
        isinstance(x, dict) and x.get("type") in EVENT_TYPES
        and any(k in x for k in ("session_id", "message", "subtype", "uuid"))
        for x in items
    )


def total_input_tokens(usage: Optional[dict]) -> int:
    """Вход запроса вместе с кэшем: клиент кладёт почти всю подсказку в кэш, и
    `input_tokens` без него показывает 2–3 токена."""
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = usage.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            total += val
    return total
