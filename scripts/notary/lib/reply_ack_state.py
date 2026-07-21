# -*- coding: utf-8 -*-
"""Ф4 (план `2026-07-20-watson-smart-replies-rethink.md`, R15/R26/RAZM6): персистентный
КОНТЕКСТ ack/переспроса бота, keyed by `message_id` БОТ-сообщения.

Зачем: обещание ступени-2 «поправьте, если не так — молчание = согласие» (R26) и
нумерованный переспрос ступени-3 (R15) требуют, чтобы reply человека НА ack бота нашёл
исходный контекст (что бот применил / о чём спросил). Reply приходит как ответ на
бот-сообщение, у нас есть только его `message_id` — по нему и ищем state.

Хранилище ГЛОБАЛЬНОЕ (как [[decision_memory]]), не на серию: по одному `message_id` надо
найти state, НЕ зная серию (Telegram отдаёт только одно звено reply-цепочки — сам ack, не
исходный протокол). Поэтому серию кладём ВНУТРЬ записи. Переживает рестарт listener
(RAZM6). TTL короткий — ответы приходят по горячим следам.

Опасная тройка (CLAUDE.md проекта): это ОПЕРАЦИОННЫЙ state (как очередь R9
[[reply_understand_queue]]), НЕ логи. Тексты задач нужны, чтобы применить статус/откат по
ответу — но в ЛОГИ они не идут (пишем только счётчики), сырой reply/ответ Claude НЕ храним,
записи TTL-bounded. Совместимо с принятым владельцем решением по опасной тройке.

stdlib-only (исполняется в listener-контексте, системный python3.9 без venv).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz
from pathlib import Path
from typing import Optional

logger = logging.getLogger("notary.reply_ack_state")

SCHEMA = 1
_WRITE_LOCK = threading.Lock()      # listener однопроцессный; RMW под локом от переплетения


def _store_path(explicit: Optional[Path] = None) -> Path:
    """Путь к JSON-хранилищу state ack. Приоритет: явный аргумент → env
    `MEETING_NOTARY_ACK_STATE_FILE` → дефолт под STATE_DIR (НЕ в PII-дереве протоколов,
    как decision-memory). Тесты подставляют tmp-файл через env/аргумент."""
    if explicit is not None:
        return Path(explicit)
    raw = os.environ.get("MEETING_NOTARY_ACK_STATE_FILE")
    if raw:
        return Path(os.path.expanduser(raw))
    state = os.environ.get("MEETING_NOTARY_STATE_DIR") or "~/.local/state"
    return Path(os.path.expanduser(state)) / "meeting-notary-watson" / "reply-ack-state.json"


def _retention_days() -> int:
    """TTL записей (дни). Дефолт 3 — reply на ack приходит по горячим следам; дальше
    контекст протухает (RAZM6: переживает рестарт, но не копится вечно)."""
    try:
        return max(1, int(os.environ.get("REPLY_ACK_STATE_RETENTION_DAYS", "3") or "3"))
    except (TypeError, ValueError):
        return 3


def _mid_key(message_id) -> Optional[str]:
    try:
        return str(int(message_id))
    except (TypeError, ValueError):
        return None


def _load(store_path: Path) -> dict:
    """`{str(message_id): record}`. Нет файла/мусор → {} (миграции не нужны)."""
    if not store_path.is_file():
        return {}
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def _prune(entries: dict, *, keep_days: int) -> dict:
    """Выкинуть записи старше `keep_days` по полю `created` (дата СОЗДАНИЯ ack, не дата
    отвечаемого сообщения — иначе ответ на старый протокол протух бы мгновенно). Записи
    без валидной даты создания не стареют (консервативно оставляем)."""
    cutoff = (_date.today() - _td(days=keep_days)).isoformat()
    out = {}
    for k, v in entries.items():
        c = v.get("created") if isinstance(v, dict) else None
        if isinstance(c, str) and len(c) == 10 and c < cutoff:
            continue
        out[k] = v
    return out


def _atomic_write(store_path: Path, entries: dict) -> bool:
    payload = {"schema": SCHEMA, "entries": entries}
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = store_path.with_suffix(store_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, store_path)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.warning("[ack-state] запись state не удалась (non-fatal): %s", type(e).__name__)
        return False


def record(
    message_id,
    *,
    series_dir,
    chat_id=None,
    replier: Optional[str] = None,
    applied: Optional[list] = None,
    has_tier2: bool = False,
    question: Optional[dict] = None,
    date: Optional[str] = None,
    store_path: Optional[Path] = None,
) -> bool:
    """Записать контекст ack бота под ключом его `message_id`.

    `applied` — список `{text, label}` применённых статусов (для отката R26 по «не так/
    верни»). `has_tier2` — был ли среди них хоть один ступени-2 (показать ли обещание
    несогласия). `question` — контекст закрытого переспроса ступени-3 (R15), см.
    `reply_ack` (mode/label/word/options/matched). Хотя бы одно из (applied, question)
    должно быть непусто — иначе state не нужен (нет канала ответа). Best-effort.

    Опасная тройка: НЕ логируем тексты — только счётчики."""
    mid = _mid_key(message_id)
    if mid is None or series_dir is None:
        return False
    applied = [a for a in (applied or []) if isinstance(a, dict) and (a.get("text") or "").strip()]
    if not applied and not question:
        return False
    rec: dict = {
        "series_dir": str(series_dir),
        "at": _dt.now(_tz.utc).isoformat(),
        "created": _date.today().isoformat(),   # TTL — по моменту создания ack
        "date": date,                            # дата reply для поля `updated` статуса
        "applied": applied,
        "has_tier2": bool(has_tier2),
    }
    if chat_id is not None:
        try:
            rec["chat_id"] = int(chat_id)
        except (TypeError, ValueError):
            pass
    if replier:
        rec["replier"] = str(replier)[:64]
    if question:
        rec["question"] = question
    sp = _store_path(store_path)
    with _WRITE_LOCK:
        entries = dict(_load(sp))
        entries[mid] = rec
        entries = _prune(entries, keep_days=_retention_days())
        ok = _atomic_write(sp, entries)
    if ok:
        logger.info("[ack-state] сохранён контекст ack applied=%d question=%s",
                    len(applied), (question or {}).get("mode") if question else None)
    return ok


def get(message_id, *, store_path: Optional[Path] = None) -> Optional[dict]:
    """Контекст ack по `message_id` бот-сообщения (на которое пришёл reply). Нет → None."""
    mid = _mid_key(message_id)
    if mid is None:
        return None
    rec = _load(_store_path(store_path)).get(mid)
    return rec if isinstance(rec, dict) else None


def consume(message_id, *, store_path: Optional[Path] = None) -> Optional[dict]:
    """Прочитать И удалить контекст ack (ответ обработан → повторно не применяем).
    Возвращает запись или None. Best-effort: сбой удаления не критичен (TTL добьёт)."""
    mid = _mid_key(message_id)
    if mid is None:
        return None
    sp = _store_path(store_path)
    with _WRITE_LOCK:
        entries = dict(_load(sp))
        rec = entries.pop(mid, None)
        if rec is not None:
            entries = _prune(entries, keep_days=_retention_days())
            _atomic_write(sp, entries)
    return rec if isinstance(rec, dict) else None
