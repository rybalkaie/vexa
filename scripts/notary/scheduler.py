#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""scheduler.py — формирует очередь стартов на 48 часов вперёд.

Запускается launchd-агентом `com.ilarybalka.meeting-notary.scheduler` раз в 15 минут.

Алгоритм:
  1. Прочитать ~/Projects/me/встречи/watched.yaml и rooms.yaml.
  2. Для каждой записи `enabled=true` и `ignored=false`:
     - google-calendar: запросить instances серии через Calendar API на 48ч.
     - manual: развернуть cron-правило в datetime'ы.
     - one-off: добавить напрямую если в окне.
  3. Для каждого экземпляра разрешить URL (явный room → conferenceData → push).
  4. Записать .queue.json (атомарно).
  5. Если был хоть один успешный календарный запрос (или календарных нет) —
     обновить .scheduler-last-success.
  6. Если .scheduler-last-success старее 1 часа — push в Telegram.

Стартует только сам себя как один процесс, читает реестры под flock через registry.
Все ошибки логируем в ~/Library/Logs/meeting-notary/scheduler.log.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.cli.registry import (  # noqa: E402
    load_rooms,
    load_watched,
    pause_until,
    resolve_room_to_url,
)
from notary.lib.cron_expand import expand_cron  # noqa: E402
from notary.lib.gcal import (  # noqa: E402
    GCalAPIError,
    GCalAuthError,
    Credentials,
    event_start_dt,
    extract_meeting_url,
    get_event,
    list_instances,
    load_credentials,
)
from notary.lib.notify import push  # noqa: E402
from notary.lib.state import (  # noqa: E402
    read_last_success,
    write_last_success,
    write_queue,
)

LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR", "~/Library/Logs/meeting-notary")
))
LOG_FILE = LOG_DIR / "scheduler.log"

HORIZON_HOURS = 48
MCP_DEAD_THRESHOLD_HOURS = 1


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger("scheduler")


def main() -> int:
    setup_logging()
    started_at = datetime.now(timezone.utc)
    logger.info("=== scheduler run start === %s", started_at.isoformat())

    pu = pause_until()
    if pu:
        logger.info("Пауза активна до %s — пишу пустую очередь", pu)
        write_queue([], generated_at=started_at)
        return 0

    watched_data = load_watched()
    rooms_data = load_rooms()
    items_in_yaml = watched_data.get("watched", []) or []
    horizon_end = started_at + timedelta(hours=HORIZON_HOURS)

    queue: list[dict[str, Any]] = []
    gcal_attempted = False
    gcal_succeeded = False

    creds: Credentials | None = None
    creds_load_error: str | None = None
    try:
        creds = load_credentials()
    except GCalAuthError as e:
        creds_load_error = str(e)
        logger.warning("Не удалось загрузить OAuth credentials: %s", e)

    for rec in items_in_yaml:
        if not rec.get("enabled", True):
            continue
        if rec.get("ignored", False):
            continue
        tp = rec.get("type")
        try:
            if tp == "google-calendar":
                gcal_attempted = True
                if creds is None:
                    raise GCalAuthError(creds_load_error or "credentials unavailable")
                new_items, creds = _expand_google_calendar(
                    rec, rooms_data, creds, started_at, horizon_end
                )
                queue.extend(new_items)
                gcal_succeeded = True
            elif tp == "manual":
                queue.extend(_expand_manual(rec, rooms_data, started_at, horizon_end))
            elif tp == "one-off":
                queue.extend(_expand_one_off(rec, rooms_data, started_at, horizon_end))
            else:
                logger.warning("Запись %s: неизвестный type=%r — пропущена", rec.get("id"), tp)
        except GCalAuthError as e:
            logger.error("GCal auth error для %s: %s", rec.get("id"), e)
            # Не падаем — продолжаем с другими записями.
        except GCalAPIError as e:
            logger.error("GCal API error для %s: %s", rec.get("id"), e)
        except Exception as e:
            logger.exception("Не смог раскрыть запись %s: %s", rec.get("id"), e)

    # Сортируем очередь по времени старта.
    queue.sort(key=lambda x: x.get("start_at", ""))

    write_queue(queue, generated_at=started_at)
    logger.info("Очередь записана: %d стартов", len(queue))
    for it in queue[:10]:
        logger.info(
            "  • %s @ %s [%s] url=%s",
            it.get("meeting_id"),
            it.get("start_at"),
            it.get("type"),
            (it.get("url") or "")[:60],
        )

    # Лог-маркер успеха календарного скана.
    if (not gcal_attempted) or gcal_succeeded:
        write_last_success(started_at)
    else:
        # Все google-calendar запросы упали — алерт о смерти MCP/OAuth.
        last = read_last_success()
        if last is None:
            # никогда не было успеха — считаем от старта
            logger.warning("Календарь ни разу не отвечал успешно")
            push(
                "Календарь не отвечает: первый запуск scheduler-а упал. "
                "Нужен повторный логин Google (см. ~/.config/google-calendar-mcp/)."
            )
        else:
            age = started_at - last
            if age > timedelta(hours=MCP_DEAD_THRESHOLD_HOURS):
                logger.warning(
                    "Календарь молчит уже %.1fч — алерт", age.total_seconds() / 3600
                )
                push(
                    f"Календарь не отвечает уже {int(age.total_seconds()/3600)}ч. "
                    "Нужен повторный логин Google. Без него регулярные встречи "
                    "записываться не будут."
                )

    return 0


def _expand_google_calendar(
    rec: dict[str, Any],
    rooms_data: dict[str, Any],
    creds: Credentials,
    horizon_start: datetime,
    horizon_end: datetime,
) -> tuple[list[dict[str, Any]], Credentials]:
    calendar_id = rec["calendar_id"]
    recurring = rec.get("recurring_event_id")
    one_event = rec.get("event_id")

    events: list[dict[str, Any]] = []
    if recurring:
        events, creds = list_instances(
            creds, calendar_id, recurring, horizon_start, horizon_end
        )
    elif one_event:
        evt, creds = get_event(creds, calendar_id, one_event)
        if evt:
            start = event_start_dt(evt)
            if start and horizon_start <= start <= horizon_end:
                events = [evt]
    else:
        logger.warning("google-calendar запись %s без event_id и recurring — пропущена", rec.get("id"))
        return [], creds

    out: list[dict[str, Any]] = []
    for evt in events:
        if evt.get("status") == "cancelled":
            continue
        start = event_start_dt(evt)
        if not start:
            continue
        url = _resolve_url(rec, rooms_data, calendar_event=evt)
        if not url:
            push(
                f"Не знаю куда подключаться для встречи «{rec.get('series')}» "
                f"({start.isoformat()}). Добавь `room: @<name>` в watched.yaml "
                f"или впиши ссылку в Google Calendar."
            )
            logger.warning("URL не разрешён для %s @ %s — встреча пропущена", rec.get("id"), start.isoformat())
            continue
        out.append(_queue_item(rec, evt, start, url))
    return out, creds


def _expand_manual(
    rec: dict[str, Any],
    rooms_data: dict[str, Any],
    horizon_start: datetime,
    horizon_end: datetime,
) -> list[dict[str, Any]]:
    cron_expr = rec["cron"]
    tz_name = rec.get("tz", "Asia/Dubai")
    try:
        starts = expand_cron(cron_expr, tz_name, horizon_end, now=horizon_start)
    except Exception as e:
        logger.error("manual %s: cron %r tz=%s невалиден: %s", rec.get("id"), cron_expr, tz_name, e)
        return []
    out: list[dict[str, Any]] = []
    for start in starts:
        url = _resolve_url(rec, rooms_data, calendar_event=None)
        if not url:
            push(
                f"Не знаю куда подключаться для встречи «{rec.get('series')}» "
                f"({start.isoformat()}). Добавь `room: @<name>` в watched.yaml."
            )
            continue
        out.append(_queue_item(rec, None, start, url))
    return out


def _expand_one_off(
    rec: dict[str, Any],
    rooms_data: dict[str, Any],
    horizon_start: datetime,
    horizon_end: datetime,
) -> list[dict[str, Any]]:
    raw = rec.get("datetime")
    if not raw:
        return []
    if isinstance(raw, datetime):
        start = raw
    else:
        try:
            start = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            logger.error("one-off %s: невалидный datetime %r", rec.get("id"), raw)
            return []
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start < horizon_start or start > horizon_end:
        return []
    url = _resolve_url(rec, rooms_data, calendar_event=None)
    if not url:
        push(
            f"Не знаю куда подключаться для встречи «{rec.get('series')}» "
            f"({start.isoformat()}). Добавь `room: @<name>` в watched.yaml."
        )
        return []
    return [_queue_item(rec, None, start, url)]


def _resolve_url(
    rec: dict[str, Any],
    rooms_data: dict[str, Any],
    *,
    calendar_event: dict[str, Any] | None,
) -> str | None:
    """Приоритеты: явный room (3 уровень из плана) → conferenceData → None.

    Возвращает URL Telemost для подключения. None — если разрешить не удалось.
    """
    explicit = rec.get("room")
    if explicit:
        try:
            url = resolve_room_to_url(explicit, rooms_data)
            if url:
                return url
        except SystemExit as e:
            logger.warning("room %r записи %s невалиден: %s", explicit, rec.get("id"), e)
    if calendar_event:
        url = extract_meeting_url(calendar_event)
        if url:
            return url
    return None


def _queue_item(
    rec: dict[str, Any],
    calendar_event: dict[str, Any] | None,
    start: datetime,
    url: str,
) -> dict[str, Any]:
    duration = rec.get("duration_minutes")
    if not duration and calendar_event:
        # Попробовать высчитать из end - start.
        try:
            end_raw = (calendar_event.get("end") or {}).get("dateTime")
            if end_raw:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                duration = max(1, int((end_dt - start).total_seconds() // 60))
        except (ValueError, TypeError):
            pass
    if not duration:
        duration = 60

    # event_id для state-файла: для google-calendar — gcal id экземпляра;
    # для прочих — meeting_id + start (округлённый до минуты).
    if calendar_event and calendar_event.get("id"):
        event_id = f"gcal:{calendar_event['id']}"
    else:
        event_id = f"local:{rec['id']}:{start.strftime('%Y%m%dT%H%M')}"

    return {
        "meeting_id": rec["id"],
        "event_id": event_id,
        "series": rec.get("series") or rec["id"],
        "start_at": start.isoformat(),
        "duration_minutes": int(duration),
        "url": url,
        "expected_participants": rec.get("expected_participants") or [],
        "keep_audio": bool(rec.get("keep_audio", False)),
        "type": rec.get("type", "?"),
    }


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduler упал с исключением: %s", e)
        push(f"scheduler упал: {e}", dedupe=True)
        rc = 1
    sys.exit(rc)
