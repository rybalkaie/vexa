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

# 🔴 Пути резолвятся ЛЕНИВО, на вызове, а не на импорте (приёмка 05.08.2026).
# Было: константы вычислялись при импорте модуля, поэтому `MEETING_NOTARY_REGISTRY_DIR`,
# выставленная тестом ПОСЛЕ первого импорта `lib.state` соседним модулем, уже не
# действовала — и `state_acquire()` в тестах писала фиктивные `event_id` в БОЕВОЙ
# `.state.json`. Записи живут 12 часов (STATE_TTL_HOURS) и роняли следующий полный прогон;
# на сервере тот же механизм заблокировал бы НАСТОЯЩУЮ встречу — барьер «встреча уже
# стартовала» сработал бы на мусоре, и бот молча не пришёл бы (зеркальный отказ к ISS-54).
# Константы ниже оставлены для обратной совместимости (значение на момент импорта), но
# внутри модуля НЕ используются — все обращения идут через функции.
QUEUE_FILE = DEFAULT_REGISTRY_DIR / ".queue.json"
STATE_FILE = DEFAULT_REGISTRY_DIR / ".state.json"
LAST_SUCCESS_FILE = DEFAULT_REGISTRY_DIR / ".scheduler-last-success"


def _registry_dir() -> Path:
    """Каталог реестра НА МОМЕНТ ВЫЗОВА (env главнее всего, как и в cli.registry)."""
    from notary.cli.registry import _resolve_registry_dir

    return _resolve_registry_dir()


def queue_file() -> Path:
    return _registry_dir() / ".queue.json"


def state_file() -> Path:
    return _registry_dir() / ".state.json"


def last_success_file() -> Path:
    return _registry_dir() / ".scheduler-last-success"


STATE_TTL_HOURS = 12


# ---------- ключ идемпотентности старта (ISS-57) ----------
#
# 🔴 БЫЛО: ключом «этот старт уже отработан» был ГОЛЫЙ `event_id`. У экземпляра
# рекуррентного события Google Calendar `event_id` завязан на `originalStartTime`
# и при ПЕРЕНОСЕ встречи НЕ МЕНЯЕТСЯ. Итог (инцидент 05.08.2026, ISS-57): очередь
# знала новое время, а повторный запуск был заблокирован дедупом — бот сидел в
# комнате по старому времени, вышел по «никто не пришёл» ровно в минуту нового
# старта, встречу не записал.
#
# СТАЛО: ключ = `event_id` + МИНУТА старта в UTC. Перенос = другой ключ = новый
# старт разрешён. Повторный тик по ТОМУ ЖЕ старту по-прежнему отбивается (минута
# та же). Нормализация в UTC обязательна: `10:00+03:00` и `07:00Z` — один и тот
# же старт, и разные ключи означали бы ДВУХ ботов на одну встречу.
def normalized_start_key(raw: Any) -> str:
    """Минута старта в UTC (`YYYYMMDDTHHMM`) или `''`, если разобрать нечем."""
    if isinstance(raw, datetime):
        dt: datetime | None = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            dt = None
    else:
        dt = None
    if dt is None:
        # Неразбираемое время НЕ схлопываем в общий ключ: два разных мусорных
        # старта одного события иначе слились бы в один и второй молча пропал.
        return ("raw:" + str(raw).strip())[:64] if raw else ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")


def start_key(item: Any) -> str:
    """Ключ идемпотентности элемента очереди: `<event_id>@<минута старта UTC>`.

    Без `start_at` (битый элемент) вырождается в голый `event_id` — то есть в
    прежнее поведение, а не в «ключ пустой, пускай стартует что угодно».
    """
    if not isinstance(item, dict):
        return ""
    event_id = str(item.get("event_id") or "")
    minute = normalized_start_key(item.get("start_at"))
    return "%s@%s" % (event_id, minute) if minute else event_id


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
    data = _read_json(queue_file())
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    return items if isinstance(items, list) else []


def write_queue(items: list[dict[str, Any]], *, generated_at: datetime | None = None) -> None:
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    _atomic_write_json(queue_file(), {
        "generated_at": generated_at.isoformat(),
        "items": items,
    })


# ---------- State (идемпотентность) ----------

def read_state() -> dict[str, str]:
    data = _read_json(state_file())
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def write_state(state: dict[str, str]) -> None:
    _atomic_write_json(state_file(), state)


def _is_fresh(state: dict[str, str], key: str, now: datetime) -> bool:
    """Ключ занят и ещё не отгорел по TTL."""
    prev = state.get(key)
    if not prev:
        return False
    try:
        prev_dt = datetime.fromisoformat(prev)
    except ValueError:
        return False
    if prev_dt.tzinfo is None:
        prev_dt = prev_dt.replace(tzinfo=timezone.utc)
    return (now - prev_dt) < timedelta(hours=STATE_TTL_HOURS)


def state_acquire(
    event_id: str,
    *,
    now: datetime | None = None,
    legacy_key: str | None = None,
    meeting_key: str | None = None,
) -> bool:
    """Атомарно: если за последние STATE_TTL_HOURS ключ уже стартовал — вернуть False.

    Иначе записать started_at = now и вернуть True. Используется runner-ом
    перед запуском бота.

    `legacy_key` — ПЕРЕХОДНЫЙ щит выкатки ISS-57. До неё ключом был голый
    `event_id`; на живой машине в `.state.json` такие записи лежат до 12 часов.
    Без проверки старого ключа первый же тик после выкатки счёл бы уже
    стартовавшую встречу «новым» ключом и привёл в комнату ВТОРОГО бота. Старый
    ключ только ЧИТАЕТСЯ и никогда не пишется: новые записи всегда композитные,
    и через TTL щит перестаёт что-либо значить сам собой.

    `meeting_key` — второй ключ ТОЙ ЖЕ встречи, независимый от того, какая строка
    очереди её описывает (у runner'а — комната + секунда старта). Занят — старт
    не проходит; при успехе занимается вместе с ключом старта, под тем же замком.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    sf = state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sf.parent / (f".{sf.name}.lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = read_state()
        if _is_fresh(state, event_id, now):
            return False
        if legacy_key and legacy_key != event_id and _is_fresh(state, legacy_key, now):
            return False
        if meeting_key and meeting_key != event_id and _is_fresh(state, meeting_key, now):
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
        if meeting_key:
            cleaned[meeting_key] = now.isoformat()
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
    lsf = last_success_file()
    lsf.parent.mkdir(parents=True, exist_ok=True)
    lsf.write_text(ts.isoformat() + "\n", encoding="utf-8")


def read_last_success() -> datetime | None:
    lsf = last_success_file()
    if not lsf.exists():
        return None
    raw = lsf.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
