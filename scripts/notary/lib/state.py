"""State-файлы планировщика: .queue.json, .state.json, .scheduler-last-success.

.queue.json — текущая очередь стартов от scheduler-а.
.state.json — {event_id: started_at_iso} для идемпотентности.
.scheduler-last-success — timestamp последнего успешного скана календаря
                          (для алерта о смерти MCP/OAuth).

Все три файла лежат в ~/Projects/me/встречи/.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Импортируем DEFAULT_REGISTRY_DIR через cli.registry — там же.
import sys

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent.parent))  # для notary.cli.registry

from notary.cli.registry import DEFAULT_REGISTRY_DIR  # noqa: E402

QUEUE_FILE = DEFAULT_REGISTRY_DIR / ".queue.json"
STATE_FILE = DEFAULT_REGISTRY_DIR / ".state.json"
LAST_SUCCESS_FILE = DEFAULT_REGISTRY_DIR / ".scheduler-last-success"

STATE_TTL_HOURS = 12


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------- Queue ----------

def read_queue() -> list[dict[str, Any]]:
    """Прочитать очередь стартов. [] если файла нет.

    Каждый элемент: {
      'meeting_id': str,
      'event_id': str,                # уникальный ключ старта для state-файла
      'series': str,
      'start_at': iso8601 with offset, # планируемое время начала
      'duration_minutes': int,
      'url': str,                      # уже разрешённый URL Telemost
      'expected_participants': list[str],
      'keep_audio': bool,
      'type': 'google-calendar'|'manual'|'one-off',
    }
    """
    data = _read_json(QUEUE_FILE)
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    return items if isinstance(items, list) else []


def write_queue(items: list[dict[str, Any]], *, generated_at: datetime | None = None) -> None:
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    _atomic_write_json(QUEUE_FILE, {
        "generated_at": generated_at.isoformat(),
        "items": items,
    })


# ---------- State (идемпотентность) ----------

def read_state() -> dict[str, str]:
    data = _read_json(STATE_FILE)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def write_state(state: dict[str, str]) -> None:
    _atomic_write_json(STATE_FILE, state)


def state_acquire(event_id: str, *, now: datetime | None = None) -> bool:
    """Атомарно: если за последние STATE_TTL_HOURS event_id уже стартовал — вернуть False.

    Иначе записать started_at = now и вернуть True. Используется runner-ом
    перед запуском бота.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_FILE.parent / (f".{STATE_FILE.name}.lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = read_state()
        prev = state.get(event_id)
        if prev:
            try:
                prev_dt = datetime.fromisoformat(prev)
            except ValueError:
                prev_dt = None
            if prev_dt:
                if prev_dt.tzinfo is None:
                    prev_dt = prev_dt.replace(tzinfo=timezone.utc)
                age = now - prev_dt
                if age < timedelta(hours=STATE_TTL_HOURS):
                    return False
        # Заодно прибрать stale-записи.
        cleaned: dict[str, str] = {}
        for k, v in state.items():
            try:
                vdt = datetime.fromisoformat(v)
            except ValueError:
                continue
            if vdt.tzinfo is None:
                vdt = vdt.replace(tzinfo=timezone.utc)
            if (now - vdt) < timedelta(hours=STATE_TTL_HOURS * 4):
                # держим в 4× TTL для отладочной видимости
                cleaned[k] = v
        cleaned[event_id] = now.isoformat()
        write_state(cleaned)
        return True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------- Last-success (алерт о смерти MCP/OAuth) ----------

def write_last_success(ts: datetime | None = None) -> None:
    if ts is None:
        ts = datetime.now(timezone.utc)
    LAST_SUCCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_SUCCESS_FILE.write_text(ts.isoformat() + "\n", encoding="utf-8")


def read_last_success() -> datetime | None:
    if not LAST_SUCCESS_FILE.exists():
        return None
    raw = LAST_SUCCESS_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
