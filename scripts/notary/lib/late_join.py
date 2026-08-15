"""Отметка «бот зашёл с опозданием» и её подпись в протоколе (Ф10b).

ЗАЧЕМ. Потолок одновременных записей (`MEETING_NOTARY_MAX_CONCURRENT_BOTS`,
дефолт 2 под 2 vCPU) означает, что третья встреча в одно время ждёт свободного
слота в очереди ожидания (`lib/waitlist.py`). Дождалась — бот заходит ПОЗЖЕ
начала, и первые минуты разговора в записи отсутствуют физически. Молчать об
этом нельзя: протокол без начала выглядит как протокол встречи, которая так и
началась, и читатель делает выводы по неполной картине.

ЧТО ДЕЛАЕТ. Runner ставит отметку в момент позднего захода; сборка протокола
дописывает в конец файла одну честную строку «бот подключился на N минут позже
начала». Отметка живёт на диске рядом с реестром, поэтому переживает и
перезапуск, и перегенерацию протокола.

Инварианты:

* 🔴 Путь резолвится ЛЕНИВО (тот же класс дефекта, что `f249582` в `lib/state.py`).
* 🔴 Подпись ИДЕМПОТЕНТНА: перегенерация протокола не плодит вторую строку.
* Нет отметки → текст протокола возвращается БАЙТ В БАЙТ. Обычный день владельца
  этот модуль не задевает вовсе.
* Опасная тройка: в отметке только серия, даты и минуты. Ни реплик, ни имён.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("notary.late_join")

LATE_JOINS_FILENAME = ".late-joins.json"
KEEP_DAYS = 30

# Маркер идемпотентности. Ищется в тексте протокола перед подписью — перегенерация
# не имеет права дописать вторую такую же строку.
MARKER = "<!-- notary:late-join -->"


def _registry_dir() -> Path:
    from notary.cli.registry import _resolve_registry_dir  # noqa: PLC0415

    return _resolve_registry_dir()


def late_joins_file() -> Path:
    """Путь к файлу отметок НА МОМЕНТ ВЫЗОВА."""
    return _registry_dir() / LATE_JOINS_FILENAME


def _key(series: Any, date: Any) -> str:
    return "%s|%s" % (str(series or "").strip(), str(date or "").strip()[:10])


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        raw = json.loads(late_joins_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Отметки опозданий нечитаемы (%s) — начинаю с пустых", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}


def _save(entries: Dict[str, Dict[str, Any]]) -> None:
    path = late_joins_file()
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _prune(entries: Dict[str, Dict[str, Any]], now: datetime) -> None:
    edge = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for key in list(entries):
        date_part = key.split("|", 1)[-1]
        if len(date_part) == 10 and date_part < edge:
            entries.pop(key, None)


def mark(
    series: Any,
    start_at: Any,
    *,
    joined_at: datetime,
    late_minutes: int,
    reason: str,
) -> bool:
    """Записать факт позднего захода. True — записали.

    Best-effort: сбой записи не имеет права сорвать уже разрешённый запуск бота —
    лучше запись без подписи, чем встреча без записи.
    """
    date = str(start_at or "")[:10]
    if not series or len(date) != 10 or late_minutes <= 0:
        return False
    try:
        entries = _load()
        _prune(entries, joined_at)
        entries[_key(series, date)] = {
            "series": str(series),
            "scheduled_start_at": str(start_at),
            "joined_at": joined_at.isoformat(),
            "late_minutes": int(late_minutes),
            "reason": str(reason)[:40],
        }
        _save(entries)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Не смог записать отметку опоздания (%s)", type(e).__name__)
        return False


def get(series: Any, date: Any) -> Optional[Dict[str, Any]]:
    """Отметка по серии и дате или None."""
    if not series or not date:
        return None
    try:
        return _load().get(_key(series, date))
    except Exception:  # noqa: BLE001
        return None


def note_text(entry: Dict[str, Any]) -> str:
    """Одна строка для протокола. Без имён и без текста разговора."""
    minutes = int(entry.get("late_minutes") or 0)
    started = str(entry.get("scheduled_start_at") or "")[11:16]
    joined = str(entry.get("joined_at") or "")[11:16]
    why = (
        "в это время шли другие записи и свободного слота у бота не было"
        if entry.get("reason") == "skip_capacity"
        else "встречу перенесли, и бот перезашёл в новое время"
    )
    return (
        "%s\n\n---\n\n> ⏳ **Бот подключился с опозданием на %d мин** "
        "(плановое начало %s, фактический заход %s): %s. "
        "Первые минуты разговора в запись не попали." % (MARKER, minutes, started, joined, why)
    )


def sign(text: str, series: Any, date: Any) -> str:
    """Подписать протокол отметкой опоздания. Нет отметки → текст БАЙТ В БАЙТ.

    Идемпотентно: повторная подпись уже подписанного текста ничего не меняет.
    """
    if not isinstance(text, str) or not text.strip():
        return text
    if MARKER in text:
        return text
    try:
        entry = get(series, date)
    except Exception:  # noqa: BLE001
        return text
    if not entry or int(entry.get("late_minutes") or 0) <= 0:
        return text
    return text.rstrip("\n") + "\n\n" + note_text(entry) + "\n"
