#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""meetings_apply_reply.py — Ф6: применяет ответ Ильи на блок 📅 к watched.yaml.

Вход:
  --reply "<текст ответа Ильи>"  (или stdin)
  --snapshot ~/.local/state/meetings-shown.last.json  (опц., по умолчанию это и берётся)

Алгоритм:
  1. Прочитать snapshot — список предложенных встреч (n, calendar_id, event_id,
     recurring_event_id, title, start_at, url_in_event).
  2. Через `claude --print` отдать минимальный JSON-контекст (без сырого
     транскрипта, дисциплина «Опасной тройки») и попросить вернуть JSON-массив
     решений по номерам: accept_once / accept_series / reject / ignore_forever.
  3. Применить через прямой вызов registry.save_watched():
     - accept_once    → запись type=google-calendar с event_id + (room|null)
     - accept_series  → запись type=google-calendar с recurring_event_id + (room|null)
     - reject         → ничего
     - ignore_forever → запись type=google-calendar с enabled=false, ignored=true
                       (по recurring_event_id если есть, иначе event_id)
  4. Если accept_* без room и без url_in_event — action заменяется на
     pending_room (Илья должен указать переговорку — daemon переспросит).
  5. На stdout — JSON с summary_lines, pending_room, applied, rejected.

Дисциплина «Опасной тройки»:
  - В промпт идёт только title + start + has_url_in_event + recurring + text reply.
  - НЕ кладём содержимое прошлых протоколов, транскрипты, личные данные
    участников.
  - Сырой ответ Claude не сохраняем в файлы / логи — только парсим JSON и
    немедленно применяем.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.cli.registry import (  # noqa: E402
    find_room,
    find_watched,
    load_rooms,
    load_watched,
    release_watched_lock,
    save_watched,
    validate_watched_record,
)

STATE_DIR = Path(os.path.expanduser("~/.local/state"))
SNAPSHOT_FILE = STATE_DIR / "meetings-shown.last.json"

LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR", "~/Library/Logs/meeting-notary")
))
LOG_FILE = LOG_DIR / "meetings-apply-reply.log"

CLAUDE_BIN = os.path.expanduser(os.environ.get(
    "CLAUDE_BIN", "~/.npm-global/bin/claude"
))
CLAUDE_TIMEOUT_S = 90

VALID_ACTIONS = {"accept_once", "accept_series", "reject", "ignore_forever"}


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


logger = logging.getLogger("meetings-apply-reply")


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def make_record_id(item: dict[str, Any], kind: str) -> str:
    """Сгенерировать kebab-case id новой записи. Илья может переименовать через CLI."""
    if kind == "series":
        if item.get("recurring_event_id"):
            return f"series-{short_hash(item['recurring_event_id'])}"
        return f"series-{short_hash(item['event_id'])}"
    return f"oneoff-{short_hash(item['event_id'])}"


def build_claude_prompt(items: list[dict[str, Any]], rooms: dict[str, Any], reply: str) -> str:
    """Минимальный промпт. Только метаданные предложений + список комнат + reply."""
    items_for_llm = [
        {
            "n": it["n"],
            "title": it["title"],
            "start_at": it["start_at"],
            "has_url_in_event": it.get("has_url_in_event", False),
            "is_recurring": bool(it.get("recurring_event_id")),
        }
        for it in items
    ]
    rooms_list = [
        {"name": f"@{r.get('name')}", "description": r.get("description", "")}
        for r in rooms.get("rooms", []) or []
    ]
    return (
        "Ты — парсер ответа владельца на вечернюю секцию «📅 Встречи и запись». "
        "Владелец видел нумерованный список встреч и отвечает по номерам.\n\n"
        "Правила:\n"
        "- Каждый номер из списка должен получить одно действие: "
        "`accept_once` (записать только этот экземпляр), "
        "`accept_series` (записывать всю серию), "
        "`reject` (не записывать сейчас, но в следующий раз снова предложить), "
        "`ignore_forever` (не предлагать эту серию никогда).\n"
        "- По дефолту «да, пиши» = accept_once (разово). accept_series — только если "
        "владелец явно сказал «и серию», «постоянно», «каждый раз», «всю серию», «серию тоже». "
        "Если встреча не рекуррентная (is_recurring=false) — accept_series недопустим, "
        "используй accept_once.\n"
        "- «нет», «не надо», «пропусти», «не сейчас» = reject.\n"
        "- «никогда», «больше не предлагай», «не предлагай эту серию» = ignore_forever.\n"
        "- **Общий отказ без номеров** («не сейчас», «ни одной», «все нет», «нет, никакие») = "
        "присвой `action=reject` ВСЕМ номерам из списка предложенных встреч.\n"
        "- **Общее согласие без номеров** («все да», «пиши все», «согласен») = присвой "
        "`action=accept_once` ВСЕМ номерам (НЕ accept_series — серию только при явном «и серию»). "
        "Если у какой-то встречи нет ссылки в событии и Илья не указал переговорку — "
        "оставь `room: null`, обработчик попросит уточнить.\n"
        "- Поле `room`: владелец может назвать переговорку по имени (`@t11`/`главная`/`в продажах`/...) "
        "или дать прямую ссылку (https://...). Имена переговорок из справочника ниже. Слова-синонимы "
        "сопоставляй по полю `description` («главная» → @t11, «продажи»/«запасная» → ..., «маркетплейсы» → @t22, и т.п.). "
        "Если в reply владелец не указал переговорку — `room: null`.\n"
        "- Если в ответе нет упоминания какого-то номера — для него action=reject.\n"
        "- ВЕРНИ ТОЛЬКО JSON-массив, без markdown-обёртки, без комментариев.\n\n"
        f"Список переговорок (rooms.yaml):\n{json.dumps(rooms_list, ensure_ascii=False)}\n\n"
        f"Предложенные встречи:\n{json.dumps(items_for_llm, ensure_ascii=False)}\n\n"
        f"Ответ владельца:\n```\n{reply}\n```\n\n"
        "Верни JSON-массив строго в формате:\n"
        '[{"n": 1, "action": "accept_once|accept_series|reject|ignore_forever", "room": "@t11"|"https://..."|null}, ...]\n'
    )


def parse_claude_output(raw: str, expected_ns: set[int]) -> list[dict[str, Any]]:
    """Достать JSON-массив из ответа claude. Сырой raw НЕ сохраняем в файлы."""
    # claude может обернуть в markdown ```json ... ``` — отлепим
    text = raw.strip()
    # Сначала пробуем найти первый '[' и последний ']' — самый устойчивый способ.
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        raise ValueError("claude не вернул JSON-массив")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"невалидный JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError("ожидался JSON-массив")
    out: list[dict[str, Any]] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        n = d.get("n")
        if not isinstance(n, int) or n not in expected_ns:
            continue
        action = d.get("action")
        if action not in VALID_ACTIONS:
            continue
        room = d.get("room")
        if room is not None and not isinstance(room, str):
            room = None
        out.append({"n": n, "action": action, "room": room})
    return out


def call_claude(prompt: str) -> str:
    """Запустить claude --print с промптом на stdin. Возвращает stdout."""
    cmd = [CLAUDE_BIN, "--print"]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_S,
            env={**os.environ, "PATH": os.environ.get("PATH", "") + ":" + os.path.dirname(CLAUDE_BIN)},
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"claude CLI не найден: {CLAUDE_BIN}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude CLI timeout {CLAUDE_TIMEOUT_S}s") from e
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI rc={proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout


def fmt_when_short(start_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(start_iso)
    except ValueError:
        return start_iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        local = dt.astimezone(ZoneInfo("Asia/Dubai"))
    except Exception:
        local = dt
    return local.strftime("%d.%m %H:%M")


def apply_decisions(
    decisions: list[dict[str, Any]],
    snapshot_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Применить решения к watched.yaml. Возвращает структуру для daemon-а."""
    snap_by_n = {it["n"]: it for it in snapshot_items}
    rooms = load_rooms()

    # Однократный lock на всё применение.
    data = load_watched(lock=True)
    saved_ok = False
    try:
        summary_lines: list[str] = []
        pending_room: list[dict[str, Any]] = []
        applied = 0
        rejected = 0
        ignored = 0
        skipped: list[str] = []

        for dec in decisions:
            n = dec["n"]
            action = dec["action"]
            room = dec.get("room")
            item = snap_by_n.get(n)
            if item is None:
                continue

            title = item["title"]
            when = fmt_when_short(item["start_at"])

            if action == "reject":
                summary_lines.append(f"{n}. {title} ({when}) — не записываю")
                rejected += 1
                continue

            if action == "ignore_forever":
                # создаём «глушащую» запись с enabled=false, ignored=true
                rec = _build_record(item, room=None, kind_series_if_possible=True)
                rec["enabled"] = False
                rec["ignored"] = True
                # Не задваиваем — если уже есть с тем же id, обновим флаги.
                existing = find_watched(rec["id"], data)
                if existing is not None:
                    existing["enabled"] = False
                    existing["ignored"] = True
                else:
                    errors = validate_watched_record(rec, rooms)
                    if errors:
                        skipped.append(f"{n}: {'; '.join(errors)}")
                        continue
                    data["watched"].append(rec)
                summary_lines.append(f"{n}. {title} ({when}) — больше не предлагаю эту серию")
                ignored += 1
                continue

            # accept_once / accept_series
            is_series = action == "accept_series"
            if is_series and not item.get("recurring_event_id"):
                # не recurring → накатываем accept_once
                is_series = False

            # Проверим переговорку
            chosen_room = _resolve_room_input(room, rooms)
            if chosen_room is None and not item.get("has_url_in_event"):
                # ни room в ответе, ни ссылка в календаре → pending
                pending_room.append({
                    "n": n,
                    "title": title,
                    "when": when,
                    "kind": "series" if is_series else "once",
                })
                continue

            rec = _build_record(item, room=chosen_room, kind_series_if_possible=is_series)
            # Если такой id уже есть (например, повторное подтверждение) — пропускаем.
            existing = find_watched(rec["id"], data)
            if existing is not None:
                # обновим enabled=True, ignored=False — Илья «передумал»
                existing["enabled"] = True
                existing["ignored"] = False
                if chosen_room:
                    existing["room"] = chosen_room
                summary_lines.append(
                    f"{n}. {title} ({when}) — уже была в реестре, включил"
                )
                applied += 1
                continue

            errors = validate_watched_record(rec, rooms)
            if errors:
                skipped.append(f"{n}: {'; '.join(errors)}")
                continue
            data["watched"].append(rec)
            kind_label = "серию" if is_series else "только этот экземпляр"
            room_label = (
                f"в {chosen_room}" if chosen_room else "ссылка из календаря"
            )
            summary_lines.append(
                f"{n}. {title} ({when}) — записал {kind_label}, {room_label}"
            )
            applied += 1

        if applied > 0 or ignored > 0:
            save_watched(data)
            saved_ok = True

        return {
            "summary_lines": summary_lines,
            "pending_room": pending_room,
            "applied": applied,
            "rejected": rejected,
            "ignored_forever": ignored,
            "skipped": skipped,
        }
    finally:
        if not saved_ok:
            # save_watched сам отпускает lock; иначе явный release.
            try:
                release_watched_lock()
            except Exception:  # noqa: BLE001
                pass


def _resolve_room_input(room: str | None, rooms: dict[str, Any]) -> str | None:
    """Нормализуем room из ответа claude. Возвращаем @name или прямую ссылку, или None."""
    if not room:
        return None
    room = room.strip()
    if not room:
        return None
    if room.startswith("@"):
        # проверим что такая комната есть
        name = room[1:]
        if find_room(name, rooms) is None:
            return None
        return room
    if room.startswith("https://"):
        return room
    return None


def _build_record(
    item: dict[str, Any],
    *,
    room: str | None,
    kind_series_if_possible: bool,
) -> dict[str, Any]:
    """Сформировать запись для watched.yaml."""
    use_series = kind_series_if_possible and bool(item.get("recurring_event_id"))
    rid = make_record_id(item, "series" if use_series else "once")
    rec: dict[str, Any] = {
        "id": rid,
        "series": rid,
        "type": "google-calendar",
        "calendar_id": item["calendar_id"],
    }
    if use_series:
        rec["recurring_event_id"] = item["recurring_event_id"]
    else:
        rec["event_id"] = item["event_id"]
    if room:
        rec["room"] = room
    rec["enabled"] = True
    rec["ignored"] = False
    rec["keep_audio"] = False
    rec["notify"] = []
    return rec


def main() -> int:
    setup_logging()

    p = argparse.ArgumentParser(description="Apply Ilya's reply to 📅 meetings block.")
    p.add_argument("--reply", help="Текст ответа Ильи (если не указано — читаем stdin).")
    p.add_argument(
        "--snapshot",
        default=str(SNAPSHOT_FILE),
        help=f"Файл-snapshot предложений (default: {SNAPSHOT_FILE}).",
    )
    p.add_argument(
        "--reply-date",
        type=int,
        help="Unix-timestamp поля reply_to_message.date (для hard-проверки "
             "свежести snapshot — daemon prompt описывает soft-проверку, "
             "эта опция — последний рубеж).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Не писать в watched.yaml, только вернуть план.",
    )
    args = p.parse_args()

    reply_text = args.reply
    if reply_text is None:
        reply_text = sys.stdin.read()
    reply_text = (reply_text or "").strip()
    if not reply_text:
        print(json.dumps({"error": "пустой reply", "summary_lines": [], "pending_room": []}))
        return 1

    snap_path = Path(os.path.expanduser(args.snapshot))
    if not snap_path.exists():
        print(json.dumps({"error": f"snapshot не найден: {snap_path}", "summary_lines": [], "pending_room": []}))
        return 1

    with snap_path.open("r", encoding="utf-8") as f:
        snapshot = json.load(f)
    items = snapshot.get("items", []) or []
    if not items:
        print(json.dumps({"error": "snapshot пуст", "summary_lines": [], "pending_room": []}))
        return 1

    # Hard-проверка свежести: если reply пришёл сильно позже shown_date —
    # snapshot уже мог быть переписан более новым вечером, номера не те.
    # Допуск ±1 день — для кейса «дайджест в 23:50, ответ в 00:05»:
    # snapshot за 26.05, reply за 27.05 — валиден; reply за 28.05 — нет.
    if args.reply_date and snapshot.get("shown_date"):
        try:
            from zoneinfo import ZoneInfo

            reply_dt = datetime.fromtimestamp(args.reply_date, tz=ZoneInfo("Asia/Dubai"))
            reply_day = reply_dt.date()
            shown_day = datetime.strptime(snapshot["shown_date"], "%Y-%m-%d").date()
            delta_days = abs((reply_day - shown_day).days)
        except Exception:
            reply_day = None
            delta_days = None
        if delta_days is not None and delta_days > 1:
            print(json.dumps({
                "error": f"snapshot устарел: shown_date={snapshot['shown_date']}, "
                         f"reply_date={reply_day.isoformat()} (расхождение {delta_days} дн). "
                         f"Илья отвечает на чужой день — реестр НЕ трогаю. "
                         f"Переспроси: «перешли reply на свежее 📅-сообщение или назови встречи словами».",
                "summary_lines": [],
                "pending_room": [],
                "stale_snapshot": True,
            }, ensure_ascii=False))
            return 1

    rooms = load_rooms()
    expected_ns = {it["n"] for it in items}
    prompt = build_claude_prompt(items, rooms, reply_text)

    logger.info(
        "apply-reply start: items=%d, reply_len=%d, ns=%s",
        len(items),
        len(reply_text),
        sorted(expected_ns),
    )

    try:
        raw = call_claude(prompt)
    except RuntimeError as e:
        logger.error("claude CLI: %s", e)
        print(json.dumps({"error": f"claude CLI: {e}", "summary_lines": [], "pending_room": []}))
        return 2

    try:
        decisions = parse_claude_output(raw, expected_ns)
    except ValueError as e:
        logger.error("парсинг ответа: %s", e)
        # raw намеренно не логируем — недоверенный текст (Опасная тройка).
        print(json.dumps({"error": f"парсинг ответа: {e}", "summary_lines": [], "pending_room": []}))
        return 2

    logger.info(
        "decisions parsed: %s",
        [{"n": d["n"], "action": d["action"], "has_room": bool(d.get("room"))} for d in decisions],
    )

    if not decisions:
        # Илья ответил «ок» / «угу» / эмодзи — claude не извлёк решений по номерам.
        # Возвращаем явный сигнал, чтобы daemon переспросил, а не висел в неопределённости.
        logger.warning(
            "no_decisions: claude вернул пустой массив или decisions отфильтрованы "
            "по expected_ns. reply_len=%d, items=%d",
            len(reply_text), len(items),
        )
        print(json.dumps({
            "summary_lines": [],
            "pending_room": [],
            "applied": 0, "rejected": 0, "ignored_forever": 0,
            "no_decisions": True,
            "hint": "В ответе нет понятных решений по номерам. Спроси Илью прямо: "
                    "«По каким встречам и что делаем? Напр.: 1 да в главной, 2 нет, "
                    "3 не предлагай эту серию».",
        }, ensure_ascii=False))
        return 0

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "decisions": decisions,
            "summary_lines": [
                f"DRY: n={d['n']} → {d['action']} room={d.get('room')}"
                for d in decisions
            ],
            "pending_room": [],
        }, ensure_ascii=False))
        return 0

    result = apply_decisions(decisions, items)
    print(json.dumps(result, ensure_ascii=False))
    logger.info(
        "applied=%d rejected=%d ignored_forever=%d pending_room=%d",
        result.get("applied", 0),
        result.get("rejected", 0),
        result.get("ignored_forever", 0),
        len(result.get("pending_room", [])),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
