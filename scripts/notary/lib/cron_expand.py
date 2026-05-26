"""Разворачивание cron-правил manual-записей в конкретные datetime'ы.

Используется scheduler-ом: для записи типа `manual` с полями
`cron: '0 11 * * 3'` и `tz: 'Europe/Moscow'` нужно получить список
start-time'ов на ближайшие 48 часов в форме datetime с TZ.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter


def expand_cron(
    cron_expr: str,
    tz_name: str,
    horizon_end: datetime,
    *,
    now: datetime | None = None,
) -> list[datetime]:
    """Вернуть start-time'ы cron_expr в зоне tz_name с now до horizon_end.

    horizon_end должен быть с TZ. Возвращаемые datetime — в TZ tz_name,
    aware. Если cron-выражение невалидно — поднимает ValueError.
    """
    tz = ZoneInfo(tz_name)
    if now is None:
        now = datetime.now(timezone.utc)
    base_local = now.astimezone(tz)
    horizon_local = horizon_end.astimezone(tz)
    it = croniter(cron_expr, base_local)
    out: list[datetime] = []
    while True:
        nxt = it.get_next(datetime)
        # croniter возвращает naive в зоне base'a — навешиваем TZ.
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=tz)
        if nxt > horizon_local:
            break
        out.append(nxt)
        if len(out) > 100:  # защита от безумных кронов
            break
    return out
