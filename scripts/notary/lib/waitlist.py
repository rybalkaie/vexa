"""Очередь ожидания стартов — `.start-waitlist.json` рядом с очередью планировщика (Ф10b).

ЗАЧЕМ. У runner'а есть ровно две причины не запустить бота на старте: живой бот
той же серии (`skip_series`) и потолок одновременных записей
(`skip_capacity`, `MEETING_NOTARY_MAX_CONCURRENT_BOTS`, дефолт 2 — под 2 vCPU
машины). Обе причины ВРЕМЕННЫЕ, а окно старта у runner'а — 5 минут до и 1 минута
после. Итог до этой фазы: слот освобождался через 10 минут, а кандидат к тому
моменту уже выпал из окна навсегда — «третья встреча в 10:00» не записывалась
даже тогда, когда бот освобождался в 10:12.

ЧТО ДЕЛАЕТ. Пропущенный старт кладётся сюда с ДЕДЛАЙНОМ (конец встречи, но не
дальше потолка ожидания). Пока дедлайн не вышел, runner на каждом тике пробует
снова; освободился слот — бот заходит С ОПОЗДАНИЕМ, и опоздание подписывается в
протоколе (`lib/late_join.py`). Не освободился до дедлайна — владельцу уходит
явное «не записал», а не тишина.

Этим же механизмом закрывается ISS-57: перенос встречи, когда бот уже сидит в
комнате, даёт НОВЫЙ ключ старта (`lib/state.start_key`), тот упирается в живого
бота той же серии, ложится сюда — и как только старый бот выходит, новый старт
исполняется. Двух ботов в комнате при этом не бывает ни секунды.

Инварианты:

* 🔴 **Путь резолвится ЛЕНИВО, на вызове.** Тот же класс дефекта, что чинил
  `f249582` в `lib/state.py`: константа, вычисленная на импорте, не видит
  подмену каталога пробой и пишет в БОЕВОЙ реестр владельца.
* 🔴 **Ничего не пишем, пока нечего писать.** Пустая очередь ожидания = ФАЙЛА
  НЕТ. Обычный день владельца (ни одного пропуска) не создаёт файл вовсе —
  «выключено» здесь структурное свойство, а не обещание.
* Решения — чистые функции (`plan`, `defer`, `forget`), диск отдельно.
* Аварийный выключатель `MEETING_NOTARY_START_WAITLIST=0` возвращает поведение
  до Ф10b целиком.

Опасная тройка: сюда ложатся только идентификаторы, время и КОД причины. Ни
текстов реплик, ни протоколов, ни имён участников.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("notary.waitlist")

WAITLIST_FILENAME = ".start-waitlist.json"
ENABLE_ENV = "MEETING_NOTARY_START_WAITLIST"

# Сколько ждать в самом худшем случае, даже если встреча длинная. Бот, заходящий
# через четыре часа после начала, не запись, а недоразумение.
MAX_WAIT_MINUTES_DEFAULT = 240
# Ниже этого опоздание не считается опозданием: runner тикает раз в минуту, и
# «опоздал на 40 секунд» — это не событие, а округление.
LATE_THRESHOLD_MINUTES = 2

REASON_SERIES = "skip_series"
REASON_CAPACITY = "skip_capacity"


def is_enabled() -> bool:
    """Дефолт ON. `0/false/no/off` — вернуть поведение до Ф10b."""
    raw = (os.environ.get(ENABLE_ENV) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def max_wait_minutes() -> int:
    raw = os.environ.get("MEETING_NOTARY_START_WAITLIST_MAX_MIN")
    try:
        value = int(raw) if raw and raw.strip() else MAX_WAIT_MINUTES_DEFAULT
    except (TypeError, ValueError):
        return MAX_WAIT_MINUTES_DEFAULT
    return value if value > 0 else MAX_WAIT_MINUTES_DEFAULT


def _registry_dir() -> Path:
    from notary.cli.registry import _resolve_registry_dir  # noqa: PLC0415

    return _resolve_registry_dir()


def waitlist_file() -> Path:
    """Путь к очереди ожидания НА МОМЕНТ ВЫЗОВА (см. шапку)."""
    return _registry_dir() / WAITLIST_FILENAME


# ---------- ввод-вывод ----------


def load() -> Dict[str, Dict[str, Any]]:
    """Прочитать очередь ожидания. Нет файла / битый файл → пусто.

    Битый файл НЕ повод падать: очередь ожидания — улучшение, а не условие
    работы. Хуже пропущенной поздней записи только «бот не пришёл ни на одну».
    """
    path = waitlist_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Очередь ожидания нечитаема (%s) — начинаю с пустой", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, val in raw.items():
        if isinstance(key, str) and isinstance(val, dict):
            out[key] = val
    return out


def save(entries: Dict[str, Dict[str, Any]]) -> None:
    """Записать атомарно. Пусто + файла нет → НЕ создаём файл (см. шапку)."""
    path = waitlist_file()
    if not entries and not path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not entries:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------- чистые решения ----------


def _parse(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def deadline_for(item: Dict[str, Any], *, now: Optional[datetime] = None) -> Optional[str]:
    """До какого момента ждём слот: конец встречи, но не дальше потолка ожидания."""
    start = _parse(item.get("start_at"))
    if start is None:
        return None
    try:
        duration = int(item.get("duration_minutes") or 60)
    except (TypeError, ValueError):
        duration = 60
    duration = max(1, min(duration, max_wait_minutes()))
    return (start + timedelta(minutes=duration)).isoformat()


def defer(
    entries: Dict[str, Dict[str, Any]],
    key: str,
    item: Dict[str, Any],
    *,
    reason: str,
    now: datetime,
) -> bool:
    """Поставить старт в ожидание (или обновить причину). True — состояние изменилось.

    Идентичность записи — тот же ключ старта, что у идемпотентности
    (`lib/state.start_key`): перенос встречи создаёт НОВУЮ запись ожидания, а не
    правит старую, и старая отваливается сама — её нет в очереди планировщика.
    """
    if not key:
        return False
    deadline = deadline_for(item, now=now)
    if deadline is None:
        return False
    existing = entries.get(key)
    if isinstance(existing, dict):
        if existing.get("reason") == reason and existing.get("deadline_at") == deadline:
            return False
        existing["reason"] = reason
        existing["deadline_at"] = deadline
        existing["last_seen_at"] = now.isoformat()
        return True
    entries[key] = {
        "meeting_id": item.get("meeting_id"),
        "series": item.get("series"),
        "event_id": item.get("event_id"),
        "order_id": item.get("order_id"),
        "start_at": item.get("start_at"),
        "deadline_at": deadline,
        "reason": reason,
        "first_deferred_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
    }
    return True


def forget(entries: Dict[str, Dict[str, Any]], key: str) -> bool:
    """Снять старт с ожидания. True — состояние изменилось."""
    return entries.pop(key, None) is not None


def plan(
    entries: Dict[str, Dict[str, Any]],
    queue_keys: Any,
    now: datetime,
) -> Tuple[List[str], List[Tuple[str, Dict[str, Any]]], List[Tuple[str, Dict[str, Any]]]]:
    """Что делать с очередью ожидания на этом тике. Чистая функция.

    Возвращает `(ждущие, просроченные, отпавшие)`:

    * **ждущие** — ключи, которые всё ещё в очереди планировщика и чей дедлайн не
      вышел: runner обязан считать их кандидатами вне обычного окна старта;
    * **просроченные** — дедлайн вышел: слот так и не освободился, владельцу
      нужно СКАЗАТЬ, а не промолчать;
    * **отпавшие** — старта больше нет в очереди планировщика (встречу отменили
      или перенесли): молча забываем, это штатный ход событий.
    """
    waiting: List[str] = []
    expired: List[Tuple[str, Dict[str, Any]]] = []
    dropped: List[Tuple[str, Dict[str, Any]]] = []
    known = set(queue_keys or ())
    for key, entry in list(entries.items()):
        if key not in known:
            dropped.append((key, entry))
            continue
        deadline = _parse(entry.get("deadline_at"))
        if deadline is None or now >= deadline:
            expired.append((key, entry))
            continue
        waiting.append(key)
    return waiting, expired, dropped


def late_minutes(item: Dict[str, Any], now: datetime) -> int:
    """На сколько минут заход опаздывает против планового начала (0 — не опоздание)."""
    start = _parse(item.get("start_at"))
    if start is None:
        return 0
    delta = (now - start).total_seconds() / 60.0
    return int(delta) if delta >= LATE_THRESHOLD_MINUTES else 0
