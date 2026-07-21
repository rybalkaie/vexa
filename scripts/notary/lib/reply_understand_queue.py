# -*- coding: utf-8 -*-
"""Ф3 (план `2026-07-20-watson-smart-replies-rethink.md`, R9 / РИСК4): очередь
ДОЗАБОРА для understand-слоя, когда ИИ недоступен/таймаут.

Зачем (R9, решение владельца 20.07): при недоступном ИИ бот НЕ гадает и НЕ теряет.
Очевидное по словарю применяется сразу (детерминированно, без сети). Остальное —
честное «разберу позже»: reply кладётся в очередь и разбирается ПОЗЖЕ, когда ИИ оживёт.
Ложных закрытий 0.

Инварианты (РИСК4, обязательны):
  • ПОРЯДОК — по времени ИСХОДНОГО сообщения (`orig_ts`), а не по времени постановки в
    очередь: если старый reply разобрался позже свежего, он не должен откатить более
    позднее решение человека. Дренаж идёт отсортировано по `orig_ts`.
  • ЗАПРЕТ ПЕРЕТИРАНИЯ более поздней записи sidecar: карта `applied` хранит по ключу
    задачи максимальный `orig_ts`, которым её уже трогали (живой путь ИЛИ дозабор). Перед
    применением отложенного решения по задаче T сверяем: если `applied[T] > orig_ts`
    записи — НЕ применяем (out-of-order guard). Тест: поздний ответ человека не
    откатывается старым отложенным.
  • ПЕРЕЖИВАЕТ РЕСТАРТ listener — это файл-sidecar в каталоге серии (как
    `task-status.json` / `open-tasks-snapshots.json`), не память процесса.
  • TTL/ретеншн (опасная тройка): записи и applied-отметки старше `_RETENTION_DAYS`
    прунятся. Тексты reply в очереди НУЖНЫ для повторного разбора (это операционная
    очередь, как `feedback_state`), но в ЛОГИ они НЕ уходят (логируем только счётчики).

Схема файла `reply-understand-queue.json` (в каталоге серии):
    { "schema": 1,
      "pending":  [ {id, orig_ts, chat_id, msg_id, reply_text, snapshot, enqueued_ts} ],
      "applied":  { task_key: orig_ts } }

stdlib-only (исполняется в listener-контексте, системный python3.9 без venv).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("notary.reply_understand_queue")

QUEUE_FILE = "reply-understand-queue.json"
_SCHEMA = 1
_RETENTION_DAYS = int(os.environ.get("REPLY_UNDERSTAND_QUEUE_RETENTION_DAYS", "14") or "14")
_MAX_PENDING = int(os.environ.get("REPLY_UNDERSTAND_QUEUE_MAX", "200") or "200")
_MAX_TEXT_LEN = 4000       # потолок хранимого reply (как _MAX_REPLY_LEN сплиттера)

# Один процесс-лок на read-modify-write (listener однопроцессный; фоновый воркер +
# главный поток пишут — лок страхует от гонки, как `_status_store_lock` series_memory).
_LOCK = threading.Lock()


def _queue_path(series_dir) -> Path:
    return Path(series_dir) / QUEUE_FILE


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else float(now)


def _load(path: Path) -> dict:
    """Прочитать sidecar. Нет файла / битьё → пустой каркас (best-effort)."""
    if not path.exists():
        return {"schema": _SCHEMA, "pending": [], "applied": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[understand-queue] чтение не удалось (non-fatal): %s", type(e).__name__)
        return {"schema": _SCHEMA, "pending": [], "applied": {}}
    if not isinstance(data, dict):
        return {"schema": _SCHEMA, "pending": [], "applied": {}}
    pending = data.get("pending")
    applied = data.get("applied")
    return {
        "schema": _SCHEMA,
        "pending": pending if isinstance(pending, list) else [],
        "applied": applied if isinstance(applied, dict) else {},
    }


def _save(path: Path, data: dict) -> None:
    """Атомарная запись (tmp + os.replace) — не оставляем полуфайл при сбое."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _prune(data: dict, now: float) -> dict:
    """Выкинуть pending-записи и applied-отметки старше `_RETENTION_DAYS` (по orig_ts)."""
    cutoff = now - _RETENTION_DAYS * 86400
    data["pending"] = [
        e for e in data["pending"]
        if isinstance(e, dict) and float(e.get("orig_ts") or 0) >= cutoff
    ][-_MAX_PENDING:]
    data["applied"] = {
        k: v for k, v in data["applied"].items()
        if isinstance(v, (int, float)) and float(v) >= cutoff
    }
    return data


def enqueue(
    series_dir, *, orig_ts: float, chat_id: Optional[int], msg_id: Optional[int],
    reply_text: str, snapshot: Optional[list] = None, now: Optional[float] = None,
) -> bool:
    """Поставить reply в очередь дозабора. Дедуп по `msg_id` (повтор того же сообщения
    не задваивается). Прунит TTL. Возвращает True, если записано.

    Опасная тройка: в лог только серия/счётчик, НЕ текст reply."""
    path = _queue_path(series_dir)
    now = _now(now)
    text = (reply_text or "")[:_MAX_TEXT_LEN]
    with _LOCK:
        data = _prune(_load(path), now)
        if msg_id is not None:
            data["pending"] = [e for e in data["pending"] if e.get("msg_id") != msg_id]
        entry = {
            "id": f"{msg_id if msg_id is not None else 'na'}-{int(orig_ts)}",
            "orig_ts": float(orig_ts), "chat_id": chat_id, "msg_id": msg_id,
            "reply_text": text,
            "snapshot": [s for s in snapshot if isinstance(s, str)] if isinstance(snapshot, list) else None,
            "enqueued_ts": now,
        }
        data["pending"].append(entry)
        _save(path, data)
    logger.info("[understand-queue] поставлено в очередь series=%s pending=%d",
                Path(series_dir).name, len(data["pending"]))
    return True


def record_applied(series_dir, task_key: str, orig_ts: float, *, now: Optional[float] = None) -> None:
    """Отметить, что задача `task_key` тронута решением с временем исходного сообщения
    `orig_ts` (живой путь ИЛИ дозабор). Храним МАКСИМУМ — чтобы более раннее отложенное
    решение не перетёрло более позднее (out-of-order guard R9). Пустой ключ → no-op."""
    if not task_key:
        return
    path = _queue_path(series_dir)
    with _LOCK:
        data = _prune(_load(path), _now(now))
        prev = data["applied"].get(task_key)
        if not isinstance(prev, (int, float)) or float(orig_ts) > float(prev):
            data["applied"][task_key] = float(orig_ts)
            _save(path, data)


def is_superseded(series_dir, task_key: str, orig_ts: float) -> bool:
    """True, если задачу `task_key` уже трогали решением ПОЗЖЕ, чем `orig_ts` (значит
    применять это отложенное решение нельзя — оно откатило бы более свежий статус). R9
    out-of-order guard."""
    if not task_key:
        return False
    data = _load(_queue_path(series_dir))
    prev = data["applied"].get(task_key)
    return isinstance(prev, (int, float)) and float(prev) > float(orig_ts)


def list_pending(series_dir) -> list:
    """Отложенные записи, отсортированные по времени ИСХОДНОГО сообщения (`orig_ts`) —
    порядок дозабора (R9/РИСК4)."""
    data = _load(_queue_path(series_dir))
    return sorted(
        (e for e in data["pending"] if isinstance(e, dict)),
        key=lambda e: float(e.get("orig_ts") or 0),
    )


def remove(series_dir, entry_id: str, *, now: Optional[float] = None) -> None:
    """Убрать разобранную запись из очереди по `id`."""
    path = _queue_path(series_dir)
    with _LOCK:
        data = _prune(_load(path), _now(now))
        before = len(data["pending"])
        data["pending"] = [e for e in data["pending"] if e.get("id") != entry_id]
        if len(data["pending"]) != before:
            _save(path, data)


def drain(series_dir, process_one, *, now: Optional[float] = None) -> dict:
    """Разобрать очередь: по каждой записи (в порядке `orig_ts`) вызвать
    `process_one(entry) -> bool`. True → запись разобрана, убираем из очереди. Сбой
    одной записи не роняет остальные (best-effort). Возвращает счётчики.

    `process_one` — колбэк вызывающего (понимает, как повторно прогнать understand на
    записи); очередь остаётся чистой транспортом, без циклического импорта understand-слоя.
    Out-of-order guard (`is_superseded`) применяет САМ understand при записи — очередь лишь
    отдаёт записи в правильном порядке и хранит applied-карту."""
    processed = 0
    failed = 0
    for entry in list_pending(series_dir):
        try:
            ok = bool(process_one(entry))
        except Exception as e:  # noqa: BLE001 — сбой одной записи не роняет дренаж
            logger.warning("[understand-queue] запись не разобралась (non-fatal): %s", type(e).__name__)
            ok = False
        if ok:
            remove(series_dir, entry.get("id"), now=now)
            processed += 1
        else:
            failed += 1
    if processed or failed:
        logger.info("[understand-queue] дренаж series=%s разобрано=%d осталось=%d",
                    Path(series_dir).name, processed, failed)
    return {"processed": processed, "failed": failed}
