#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""meetings_evening_block.py — Ф6: блок «📅 Встречи и запись» для вечернего дайджеста.

Сканирует Google Calendar на 7 дней вперёд по всем календарям, упомянутым
в watched.yaml как `calendar_id` (плюс `primary` если реестр пуст), применяет
эвристику «событие — это встреча» (видеоссылка ИЛИ ключевое слово в названии),
считает дельту против реестра:
  - НОВЫЕ        — событие подходит под эвристику и не отслеживается;
  - ПЕРЕНЕСЁННЫЕ — известный recurring instance изменил start_at;
  - УДАЛЁННЫЕ    — известный recurring instance исчез из календаря или отменён.

Кэш ранее виденных инстансов — ~/.local/state/meetings-known-instances.json:
  { "<event_id>": {"start_at": "<iso>", "calendar_id": "<id>", "series": "<id>" } }

Вывод (stdout JSON):
  {
    "block_text": "📅 Встречи и запись на неделю\n\n...",   # пусто если дельта = 0
    "items": [
      {"n": 1, "kind": "new" | "moved" | "cancelled",
       "calendar_id": "...", "event_id": "...",
       "recurring_event_id": "..." | null,
       "title": "...", "start_at_iso": "...", "url": "..." | null,
       "has_url_in_event": true | false},
      ...
    ],
    "calendars_scanned": [...],
    "new_count": N, "moved_count": M, "cancelled_count": K
  }

Гейт пустого блока — `block_text == ""` ⇒ обёртке нечего слать.
Ошибки лежат в exit-коде: 0 успех (даже с пустым блоком), 2 если упало
загрузить креды Google (вернёт block_text="" и report-flag в логе).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.cli.registry import (  # noqa: E402
    find_room,
    load_rooms,
    load_watched,
)
from notary.lib.gcal import (  # noqa: E402
    GCalAPIError,
    GCalAuthError,
    Credentials,
    event_start_dt,
    extract_meeting_url,
    list_events,
    load_credentials,
)

HORIZON_DAYS = 7

# Эвристика «это встреча»: ключевые слова в названии (case-insensitive, по подстроке).
MEETING_KEYWORDS = [
    "встреча",
    "звонок",
    "созвон",
    "координация",
    "sync",
    "1-на-1",
    "one-on-one",
    "meeting",
    "call",
]

# Локальная TZ для отображения. По проекту фиксирован Asia/Dubai (см. CLAUDE.md).
DISPLAY_TZ = ZoneInfo("Asia/Dubai")

STATE_DIR = Path(os.path.expanduser("~/.local/state"))
KNOWN_CACHE_FILE = STATE_DIR / "meetings-known-instances.json"

LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR", "~/Library/Logs/meeting-notary")
))
LOG_FILE = LOG_DIR / "meetings-evening-block.log"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


logger = logging.getLogger("meetings-evening-block")


def looks_like_meeting(event: dict[str, Any]) -> tuple[bool, str | None]:
    """Эвристика. Возвращает (yes_or_no, meeting_url_or_None)."""
    url = extract_meeting_url(event)
    if url:
        return True, url
    title = (event.get("summary") or "").lower()
    for kw in MEETING_KEYWORDS:
        if kw in title:
            return True, None
    return False, None


def index_watched(watched: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Индекс отслеживаемых google-calendar записей.

    Ключ: (calendar_id, recurring_event_id_or_event_id). Значение — сама запись.
    Полезно для быстрого матча события vs реестр.
    """
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in watched:
        if rec.get("type") != "google-calendar":
            continue
        cid = rec.get("calendar_id")
        if not cid:
            continue
        re_id = rec.get("recurring_event_id")
        ev_id = rec.get("event_id")
        if re_id:
            idx[(cid, re_id)] = rec
        if ev_id:
            idx[(cid, ev_id)] = rec
    return idx


def collect_calendar_ids(watched: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for rec in watched:
        if rec.get("type") != "google-calendar":
            continue
        cid = rec.get("calendar_id")
        if cid and cid not in seen:
            seen.append(cid)
    if not seen:
        seen.append("primary")
    return seen


def load_known() -> dict[str, dict[str, Any]]:
    if not KNOWN_CACHE_FILE.exists():
        return {}
    try:
        with KNOWN_CACHE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        logger.warning("Кэш %s повреждён — игнорирую", KNOWN_CACHE_FILE)
    return {}


def save_known(known: dict[str, dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = KNOWN_CACHE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(known, f, ensure_ascii=False, indent=2)
    os.replace(tmp, KNOWN_CACHE_FILE)


def fmt_when(start_iso: str) -> str:
    """Человеко-читаемое 'завтра 13:00' / 'пт 27.05 14:00'."""
    try:
        dt = datetime.fromisoformat(start_iso)
    except ValueError:
        return start_iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(DISPLAY_TZ)
    now = datetime.now(DISPLAY_TZ)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    when_str = local.strftime("%H:%M")
    if local.date() == today:
        prefix = "сегодня"
    elif local.date() == tomorrow:
        prefix = "завтра"
    else:
        wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][local.weekday()]
        prefix = f"{wd} {local.strftime('%d.%m')}"
    return f"{prefix} {when_str}"


def fmt_when_simple(start_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(start_iso)
    except ValueError:
        return start_iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(DISPLAY_TZ)
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][local.weekday()]
    return f"{wd} {local.strftime('%d.%m %H:%M')}"


HTML_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def esc(text: str) -> str:
    """HTML-экранирование для tg-send --html. Незабудь — текст из календаря недоверенный."""
    return "".join(HTML_ESCAPE.get(c, c) for c in text)


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Ф6 вечерний блок встреч.")
    p.add_argument(
        "--commit-known",
        action="store_true",
        help="Сохранить кэш known-instances. Без флага кэш не пишется — "
             "защита от потери move/cancelled-детекта, если обёртка не отправит сообщение.",
    )
    args = p.parse_args()

    setup_logging()
    started_at = datetime.now(timezone.utc)
    horizon_end = started_at + timedelta(days=HORIZON_DAYS)

    watched_data = load_watched()
    rooms_data = load_rooms()
    watched_records = watched_data.get("watched", []) or []

    watched_idx = index_watched(watched_records)
    calendar_ids = collect_calendar_ids(watched_records)
    known = load_known()

    # 1. Сканируем календари.
    try:
        creds = load_credentials()
    except GCalAuthError as e:
        logger.error("OAuth credentials недоступны: %s", e)
        # Тихо «нет блока» = плохая UX: Илья не узнает, что предложения встреч
        # отвалились. Push с дедупликацией (тот же механизм, что у scheduler Ф5).
        try:
            from notary.lib.notify import push  # type: ignore

            push(
                "Блок 📅 «Встречи и запись» не сформировался: Google Calendar не "
                "пускает (OAuth refresh-token отозван). Нужен повторный логин Google "
                "через MCP-сервер.",
                dedupe=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не смог отправить алерт о смерти OAuth: %s", exc)
        print(json.dumps({"block_text": "", "items": [], "calendars_scanned": [], "error": "auth"}))
        return 0

    all_events: list[tuple[str, dict[str, Any]]] = []  # (calendar_id, event)
    calendars_scanned: list[str] = []
    for cid in calendar_ids:
        try:
            events, creds = list_events(creds, cid, started_at, horizon_end)
            calendars_scanned.append(cid)
            for evt in events:
                all_events.append((cid, evt))
        except (GCalAuthError, GCalAPIError) as e:
            logger.warning("Не смог пройти календарь %s: %s", cid, e)

    # 2. Отбираем то, что соответствует эвристике.
    candidates: list[dict[str, Any]] = []
    seen_in_calendar: set[tuple[str, str]] = set()  # (cid, event_id) — что есть в календаре сейчас
    for cid, evt in all_events:
        eid = evt.get("id")
        if not eid:
            continue
        seen_in_calendar.add((cid, eid))
        start = event_start_dt(evt)
        if not start:
            continue
        if evt.get("status") == "cancelled":
            continue
        is_meeting, url = looks_like_meeting(evt)
        if not is_meeting:
            continue
        candidates.append({
            "calendar_id": cid,
            "event": evt,
            "start": start,
            "url_in_event": url,
        })

    # 3. Разделяем кандидатов на «новые» vs «уже отслеживаются» vs «ignored».
    new_items: list[dict[str, Any]] = []
    for c in candidates:
        evt = c["event"]
        eid = evt["id"]
        recurring = evt.get("recurringEventId")
        cid = c["calendar_id"]

        # Проверяем матч с реестром — сначала по recurring_event_id, потом по event_id.
        rec = None
        if recurring:
            rec = watched_idx.get((cid, recurring))
        if rec is None:
            rec = watched_idx.get((cid, eid))

        if rec is not None:
            # уже знаем эту встречу — пропускаем
            continue

        new_items.append({
            "kind": "new",
            "calendar_id": cid,
            "event_id": eid,
            "recurring_event_id": recurring,
            "title": evt.get("summary") or "(без названия)",
            "start_at_iso": c["start"].isoformat(),
            "url": c["url_in_event"],
            "has_url_in_event": bool(c["url_in_event"]),
        })

    # 4. Перенесённые и удалённые — по кэшу `known` против реестра.
    moved_items: list[dict[str, Any]] = []
    cancelled_items: list[dict[str, Any]] = []

    # Карта актуальных стартов для already-tracked instances (для детекта переноса).
    cur_start_by_eid: dict[tuple[str, str], str] = {}
    for cid, evt in all_events:
        eid = evt.get("id")
        if not eid:
            continue
        start = event_start_dt(evt)
        if not start:
            continue
        # Только для тех, что в реестре (нас интересует переезд именно отслеживаемых).
        recurring = evt.get("recurringEventId")
        rec = None
        if recurring:
            rec = watched_idx.get((cid, recurring))
        if rec is None:
            rec = watched_idx.get((cid, eid))
        if rec is None:
            continue
        cur_start_by_eid[(cid, eid)] = start.isoformat()

    for eid_key, info in list(known.items()):
        cid = info.get("calendar_id")
        prev_start = info.get("start_at")
        series = info.get("series")
        recurring = info.get("recurring_event_id")
        if not cid or not prev_start:
            continue
        key = (cid, eid_key)
        if key in cur_start_by_eid:
            # всё ещё видимо — проверим, не сдвинулось ли
            new_start = cur_start_by_eid[key]
            if new_start != prev_start:
                moved_items.append({
                    "kind": "moved",
                    "calendar_id": cid,
                    "event_id": eid_key,
                    "recurring_event_id": recurring,
                    "title": info.get("title", "(без названия)"),
                    "series": series,
                    "prev_start_iso": prev_start,
                    "start_at_iso": new_start,
                })
        else:
            # event_id уже не возвращается календарём в этом окне.
            # Это может быть перенос за пределы окна или удаление инстанса.
            # Считаем «удалённым» только если событие ДОЛЖНО было быть в окне (старт был в окне).
            try:
                prev_dt = datetime.fromisoformat(prev_start)
            except ValueError:
                continue
            if prev_dt.tzinfo is None:
                prev_dt = prev_dt.replace(tzinfo=timezone.utc)
            if started_at <= prev_dt <= horizon_end:
                cancelled_items.append({
                    "kind": "cancelled",
                    "calendar_id": cid,
                    "event_id": eid_key,
                    "recurring_event_id": recurring,
                    "title": info.get("title", "(без названия)"),
                    "series": series,
                    "start_at_iso": prev_start,
                })

    # 5. Обновляем кэш known: только из *отслеживаемых* инстансов (которые сейчас видим).
    new_known: dict[str, dict[str, Any]] = {}
    for cid, evt in all_events:
        eid = evt.get("id")
        if not eid:
            continue
        start = event_start_dt(evt)
        if not start:
            continue
        recurring = evt.get("recurringEventId")
        rec = None
        if recurring:
            rec = watched_idx.get((cid, recurring))
        if rec is None:
            rec = watched_idx.get((cid, eid))
        if rec is None:
            continue
        new_known[eid] = {
            "calendar_id": cid,
            "start_at": start.isoformat(),
            "title": evt.get("summary") or "",
            "series": rec.get("series") or rec.get("id"),
            "recurring_event_id": recurring,
        }
    if args.commit_known:
        save_known(new_known)

    # 6. Сортируем «новые» по start_at; «перенесённые» и «удалённые» — тоже хронологически.
    new_items.sort(key=lambda x: x["start_at_iso"])
    moved_items.sort(key=lambda x: x["start_at_iso"])
    cancelled_items.sort(key=lambda x: x["start_at_iso"])

    # 7. Формируем сообщение — пронумерованный список «новые» (1, 2, ...).
    out_items: list[dict[str, Any]] = []
    lines: list[str] = []
    if new_items or moved_items or cancelled_items:
        lines.append("📅 <b>Встречи и запись на неделю</b>")
        lines.append("")

    if new_items:
        lines.append("🆕 Новые — писать?")
        for i, it in enumerate(new_items, start=1):
            n = i
            it["n"] = n
            out_items.append(it)
            title = esc(it["title"])
            when = fmt_when(it["start_at_iso"])
            if it["url"]:
                link_part = f', <a href="{esc(it["url"])}">ссылка из календаря</a>'
            else:
                link_part = " — ссылки в событии нет, нужна переговорка"
            # «Если да — разово или серию?» — только для recurring
            if it["recurring_event_id"]:
                hint = " (разово или серию?)"
            else:
                hint = ""
            lines.append(f"{n}. {title} — {when}{link_part}{hint}")
        lines.append("")

    if moved_items:
        lines.append("🔄 Перенесённые (запись сдвинул):")
        for it in moved_items:
            title = esc(it["title"])
            old_when = fmt_when_simple(it["prev_start_iso"])
            new_when = fmt_when_simple(it["start_at_iso"])
            lines.append(f"• {title}: {old_when} → {new_when}")
        lines.append("")

    if cancelled_items:
        lines.append("❌ Отменённые (запись снял):")
        for it in cancelled_items:
            title = esc(it["title"])
            when = fmt_when_simple(it["start_at_iso"])
            lines.append(f"• {title} — {when}")
        lines.append("")

    if new_items:
        # Кнопочный мини-help — короткая подсказка по формату reply.
        lines.append("Ответь по номерам: «1 да, 2 нет, 3 не предлагай эту серию», для номера без ссылки можно «1 да в главной».")

    block_text = "\n".join(lines).rstrip()

    result = {
        "block_text": block_text,
        "items": out_items,
        "new_count": len(new_items),
        "moved_count": len(moved_items),
        "cancelled_count": len(cancelled_items),
        "calendars_scanned": calendars_scanned,
        "generated_at": started_at.isoformat(),
    }

    # 8. Сохраняем snapshot для daemon-а (как inbox-shown.last.json).
    if new_items:
        snapshot = {
            "shown_date": started_at.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d"),
            "source": "meetings-evening-block",
            "items": [{
                "n": it["n"],
                "calendar_id": it["calendar_id"],
                "event_id": it["event_id"],
                "recurring_event_id": it["recurring_event_id"],
                "title": it["title"],
                "start_at": it["start_at_iso"],
                "url_in_event": it["url"],
                "has_url_in_event": it["has_url_in_event"],
            } for it in new_items],
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / ".meetings-shown.last.json.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_DIR / "meetings-shown.last.json")

    print(json.dumps(result, ensure_ascii=False))
    logger.info(
        "scan: new=%d moved=%d cancelled=%d calendars=%s",
        len(new_items),
        len(moved_items),
        len(cancelled_items),
        calendars_scanned,
    )
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        logger.exception("meetings_evening_block упал: %s", e)
        # Возвращаем JSON с error — обёртка покажет дайджест без блока.
        print(json.dumps({"block_text": "", "items": [], "error": str(e)[:200]}))
        rc = 0
    sys.exit(rc)
