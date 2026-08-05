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
     решений по номерам: accept_once / accept_series / reject / ignore_forever /
     no_answer (владелец про этот номер ничего не сказал — следа не оставляем).
  3. Применить через прямой вызов registry.save_watched():
     - accept_once    → запись type=google-calendar с event_id + (room|null)
     - accept_series  → запись type=google-calendar с recurring_event_id + (room|null)
     - reject         → ISS-53: запись type=google-calendar по ЭТОМУ ЭКЗЕМПЛЯРУ
                       (event_id) с enabled=false + датой отказа. Вечерний блок
                       считает известным всё, что лежит в реестре, — значит
                       завтра эту же встречу он уже не предложит. Серию НЕ глушит:
                       следующий инстанс придёт с другим event_id.
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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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

# STATE_DIR читаем из env — ровно как вечерний блок и остальные модули зоны.
# Иначе стенд/второй инстанс пишет snapshot в один каталог, а читает из другого.
STATE_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_STATE_DIR") or "~/.local/state"
))
SNAPSHOT_FILE = STATE_DIR / "meetings-shown.last.json"

LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR", "~/Library/Logs/meeting-notary")
))
LOG_FILE = LOG_DIR / "meetings-apply-reply.log"

CLAUDE_BIN = os.path.expanduser(os.environ.get(
    "CLAUDE_BIN", "~/.npm-global/bin/claude"
))
CLAUDE_TIMEOUT_S = 90

# `no_answer` — владелец про этот номер НЕ СКАЗАЛ НИЧЕГО. Это не решение:
# следа на диске не оставляем, завтра встреча снова попадёт в блок. Заведено,
# потому что после ISS-53 «нет» помнится вечно, и молчание, записанное как
# отказ, тихо убивало бы встречи, про которые владелец просто не ответил.
VALID_ACTIONS = {"accept_once", "accept_series", "reject", "ignore_forever", "no_answer"}

# ISS-55: TZ отображения. Дефолт и имя переменной — ТЕ ЖЕ, что у вечернего блока
# (`meetings_evening_block.DISPLAY_TZ`), иначе бот спрашивает про встречу в одном
# часовом поясе, а подтверждает в другом (на бою: блок в Europe/Moscow, ответ
# жёстко в Asia/Dubai → расхождение в час на одной и той же встрече).
DISPLAY_TZ_ENV = "MEETING_NOTARY_DISPLAY_TZ"
DEFAULT_DISPLAY_TZ = "Asia/Dubai"

# ISS-53: сколько держать отказные записи после даты самой встречи. Отказ —
# разовый факт («в этот четверг не пиши»), и без ретеншна реестр за год
# превращается в свалку из сотен мёртвых записей.
REJECTION_RETENTION_DAYS = 30

# Метка «эту запись завёл отказ» — лицензия ретеншну её удалить. На записи,
# которую вёл владелец (со своей переговоркой, привязкой чата, company/
# visibility/open_questions_mode), эта метка НЕ ставится никогда: чистка не
# имеет права уносить то, чего не создавала.
REJECTION_OWNER_FIELD = "created_by_rejection"


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
        "`reject` (владелец ЯВНО отказался: эту встречу не записываем и больше про неё "
        "не спрашиваем, но следующую встречу той же серии предложим снова), "
        "`ignore_forever` (не предлагать эту серию никогда), "
        "`no_answer` (владелец про этот номер ничего не сказал).\n"
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
        "- **МОЛЧАНИЕ — НЕ ОТКАЗ.** Если про номер в ответе нет ни слова (ни по номеру, "
        "ни общей фразой про все встречи) — ставь `no_answer`. НИКОГДА не ставь `reject` "
        "только потому, что номер не упомянут: `reject` — это записанное решение владельца, "
        "и после него бот про эту встречу больше не спросит. Пример: список из трёх встреч, "
        "ответ «1 да» → [{\"n\":1,\"action\":\"accept_once\"}, {\"n\":2,\"action\":\"no_answer\"}, "
        "{\"n\":3,\"action\":\"no_answer\"}].\n"
        "- ВЕРНИ ТОЛЬКО JSON-массив, без markdown-обёртки, без комментариев.\n\n"
        f"Список переговорок (rooms.yaml):\n{json.dumps(rooms_list, ensure_ascii=False)}\n\n"
        f"Предложенные встречи:\n{json.dumps(items_for_llm, ensure_ascii=False)}\n\n"
        f"Ответ владельца:\n```\n{reply}\n```\n\n"
        "Верни JSON-массив строго в формате:\n"
        '[{"n": 1, "action": "accept_once|accept_series|reject|ignore_forever|no_answer", '
        '"room": "@t11"|"https://..."|null}, ...]\n'
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
        # bool отсекаем явно: в Python True — это int, и `n: true` проезжал как
        # номер 1 (владелец видел подтверждение «True. Координация… — не записываю»).
        # Ровно так же сделано для telegram_chat_id в registry.py:340.
        if isinstance(n, bool) or not isinstance(n, int) or n not in expected_ns:
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


def display_tz(name: str | None = None) -> ZoneInfo | timezone:
    """TZ отображения — та же, что у вечернего блока (ISS-55).

    `name` — зона, приехавшая вместе с вопросом (поле `shown_tz` в snapshot).
    Она главнее окружения: вечерний блок и apply-reply — это ДВА процесса с
    разными юнитами, и если их env разъедется, правду о времени надо брать из
    того же места, где нарисован сам вопрос.

    Без `name` читаем env на каждый вызов, а не при импорте: apply-reply живёт
    один ответ, но так значение не залипает, если демону поменяли env на ходу.
    Мусор в env не роняет применение ответа владельца — откат на дефолт + warning
    (то же поведение, что у `meetings_evening_block.resolve_display_tz`).
    """
    raw = name if name is not None else os.environ.get(DISPLAY_TZ_ENV)
    tz_name = (raw or "").strip() or DEFAULT_DISPLAY_TZ
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        if tz_name != DEFAULT_DISPLAY_TZ:
            logger.warning("%s=%r не распознан — беру %s", DISPLAY_TZ_ENV, tz_name, DEFAULT_DISPLAY_TZ)
        try:
            return ZoneInfo(DEFAULT_DISPLAY_TZ)
        except Exception:  # noqa: BLE001
            # tzdata нет вовсе (голый контейнер) — молчать нельзя: время
            # владельцу будет показано в UTC, а это и есть ISS-55.
            logger.error(
                "не найдена ни одна таймзона (%s и дефолт %s) — показываю время в UTC",
                tz_name, DEFAULT_DISPLAY_TZ,
            )
            return timezone.utc


def fmt_when_short(start_iso: Any, tz: ZoneInfo | timezone | None = None) -> str:
    """«06.08 13:00» в TZ отображения. Ровно то же время, что в вопросе блока."""
    try:
        dt = datetime.fromisoformat(start_iso)
    except (ValueError, TypeError):
        # Мусор в snapshot не должен ронять применение ответа: показываем как есть.
        return start_iso if isinstance(start_iso, str) else ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz or display_tz()).strftime("%d.%m %H:%M")


def _parse_day(raw: Any) -> date | None:
    """Достать дату из ISO-даты или ISO-момента. None — если это не дата."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def rejection_meeting_day(rec: dict[str, Any]) -> date | None:
    """Дата встречи, по которой владелец сказал «нет». Фолбэк — дата самого отказа."""
    for raw in (rec.get("rejected_start_at"), rec.get("rejected_at")):
        day = _parse_day(raw)
        if day is not None:
            return day
    return None


def is_stale_rejection(rec: dict[str, Any], today: date) -> bool:
    """Отказная запись, чья встреча прошла больше REJECTION_RETENTION_DAYS назад.

    Четыре сторожа, и каждый закрывает свой способ снести чужие данные:
      1. `created_by_rejection` — ретеншн уносит ТОЛЬКО те записи, которые он же
         и завёл. Запись владельца (с привязкой чата, company/visibility,
         open_questions_mode) не создавал никто из нас — значит и удалять её мы
         не вправе, даже если по ней сказали «нет»;
      2. `enabled` — «передумал» / `meeting-watch enable` включает запись, метки
         при этом не снимаются: включённое не чистим;
      3. `ignored` — «не предлагай эту серию» вечно, это не разовый отказ;
      4. читаемая дата — мусор в дате оставляем, удалять вслепую хуже.
    """
    if not isinstance(rec, dict):
        # Битая строка в watched.yaml не должна ронять весь ответ владельца.
        return False
    if not rec.get(REJECTION_OWNER_FIELD):
        return False
    if not rec.get("rejected_at") and not rec.get("rejected_start_at"):
        return False
    if rec.get("enabled", True):
        return False
    if rec.get("ignored", False):
        return False
    day = rejection_meeting_day(rec)
    if day is None:
        # Мусор в дате — оставляем. Удалять вслепую хуже, чем держать лишнюю строку.
        logger.warning("ретеншн отказов: у %s не читается дата — оставляю", rec.get("id"))
        return False
    return (today - day).days > REJECTION_RETENTION_DAYS


def prune_stale_rejections(data: dict[str, Any], *, today: date | None = None) -> int:
    """Вычистить протухшие отказы из реестра. Возвращает, сколько записей убрал.

    `today` инъектируется (тесты, ретро-прогоны); по умолчанию — сегодня в TZ
    отображения. Вызывается там же, где применяется ответ владельца: лок на
    watched.yaml уже взят, отдельный проход по реестру не нужен.
    """
    if today is None:
        today = datetime.now(display_tz()).date()
    kept: list[dict[str, Any]] = []
    removed = 0
    for rec in list(data.get("watched", []) or []):
        if is_stale_rejection(rec, today):
            removed += 1
            logger.info(
                "ретеншн отказов: убрал %s (встреча %s была больше %d дней назад)",
                rec.get("id"), rejection_meeting_day(rec), REJECTION_RETENTION_DAYS,
            )
            continue
        kept.append(rec)
    if removed:
        data["watched"] = kept
    return removed


def collapse_decisions(
    decisions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[int]]:
    """Свернуть решения по номеру. Возвращает (решения, номера-с-конфликтом).

    Модель законно возвращает два решения по одному номеру на фразу вида
    «первую не надо… хотя ладно, пиши». Раньше применялись оба: владелец получал
    и «записал», и «не записываю» про одну встречу и не знал, что победило.
    Побеждает последнее (оно ближе к концу фразы владельца), а про сам факт
    разночтения ему говорим прямым текстом.
    """
    by_n: dict[int, dict[str, Any]] = {}
    conflicts: list[int] = []
    for dec in decisions:
        n = dec.get("n")
        prev = by_n.get(n)
        if prev is not None and (
            prev.get("action") != dec.get("action") or prev.get("room") != dec.get("room")
        ) and n not in conflicts:
            conflicts.append(n)
        by_n[n] = dec
    return list(by_n.values()), conflicts


def sanitize_snapshot_items(
    items: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Отсеять битые элементы snapshot. Возвращает (годные, описания выброшенных).

    Кривой элемент (нет `event_id`, `calendar_id` не строка, `n` булево…) раньше
    ронял `main()` целиком — вместе с согласиями владельца по ИСПРАВНЫМ встречам,
    и на stdout не оставалось ничего. Поэлементно: битое пропускаем и пишем в лог,
    остальное применяем.
    """
    good: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen_ns: set[int] = set()
    for idx, it in enumerate(items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            dropped.append(f"элемент #{idx}: не объект")
            continue
        n = it.get("n")
        if isinstance(n, bool) or not isinstance(n, int):
            dropped.append(f"элемент #{idx}: номер {n!r} — не целое число")
            continue
        if n in seen_ns:
            dropped.append(f"элемент #{idx}: номер {n} уже был в snapshot")
            continue
        bad_field = None
        for field in ("calendar_id", "event_id"):
            value = it.get(field)
            if not isinstance(value, str) or not value.strip():
                bad_field = field
                break
        if bad_field:
            dropped.append(f"элемент #{idx} (n={n}): пустой или нестроковый {bad_field}")
            continue
        clean = dict(it)
        clean["n"] = n
        title = clean.get("title")
        clean["title"] = title if isinstance(title, str) and title.strip() else "(без названия)"
        start_at = clean.get("start_at")
        if not isinstance(start_at, str):
            if start_at is not None:
                dropped.append(f"элемент #{idx} (n={n}): start_at={start_at!r} — не строка, время не покажу")
            clean["start_at"] = ""
        for field in ("recurring_event_id",):
            if not isinstance(clean.get(field), str) or not clean.get(field):
                clean[field] = None
        seen_ns.add(n)
        good.append(clean)
    return good, dropped


def apply_decisions(
    decisions: list[dict[str, Any]],
    snapshot_items: list[dict[str, Any]],
    *,
    today: date | None = None,
    shown_tz: str | None = None,
) -> dict[str, Any]:
    """Применить решения к watched.yaml. Возвращает структуру для daemon-а.

    `today` — «сегодня» для даты отказа и ретеншна; инъектируется тестами,
    по умолчанию берётся в TZ отображения.
    `shown_tz` — таймзона, в которой владельцу нарисовали сам вопрос (поле
    `shown_tz` snapshot). Приезжает вместе с вопросом, поэтому подтверждение
    показывает то же время даже если у двух юнитов разъехалось окружение.
    """
    tz = display_tz(shown_tz) if shown_tz else display_tz()
    if shown_tz and str(tz) != (os.environ.get(DISPLAY_TZ_ENV) or DEFAULT_DISPLAY_TZ).strip():
        logger.warning(
            "TZ вопроса (%s из snapshot) не совпадает с TZ окружения (%s) — "
            "показываю время так, как оно было в вопросе",
            shown_tz, os.environ.get(DISPLAY_TZ_ENV) or DEFAULT_DISPLAY_TZ,
        )
    snap_by_n = {
        it["n"]: it
        for it in (snapshot_items or [])
        if isinstance(it, dict) and isinstance(it.get("n"), int) and not isinstance(it.get("n"), bool)
    }
    rooms = load_rooms()
    today_day = today or datetime.now(tz).date()
    decisions, conflicts = collapse_decisions(decisions)

    # Однократный lock на всё применение.
    data = load_watched(lock=True)
    saved_ok = False
    try:
        summary_lines: list[str] = []
        pending_room: list[dict[str, Any]] = []
        applied = 0
        rejected = 0
        ignored = 0
        unanswered: list[int] = []
        skipped: list[str] = []

        for dec in decisions:
            n = dec["n"]
            action = dec["action"]
            room = dec.get("room")
            item = snap_by_n.get(n)
            if item is None:
                skipped.append(f"{n}: такого номера не было в списке — пропустил")
                continue

            title = item.get("title") or "(без названия)"
            when = fmt_when_short(item.get("start_at", ""), tz)

            if action == "no_answer":
                # Владелец про этот номер ничего не сказал. Ничего не пишем:
                # завтра встреча снова попадёт в блок и он ответит, если захочет.
                unanswered.append(n)
                continue

            # Кривой элемент snapshot (нет event_id, мусор в полях) не должен
            # хоронить согласия владельца по ИСПРАВНЫМ встречам: падаем
            # поэлементно, в лог и в `skipped`.
            try:
                if action == "reject":
                    # ISS-53. Раньше «нет» не оставляло следа на диске — и назавтра
                    # вечерний блок предлагал ту же встречу снова, как будто не слышал.
                    # Пишем отказ ПО ЭТОМУ ЭКЗЕМПЛЯРУ (event_id): блок считает
                    # известным всё, что есть в реестре, планировщик disabled-записи
                    # не берёт. Серия при этом жива — следующая встреча придёт с другим
                    # event_id и будет предложена (в этом и разница с ignore_forever).
                    errors = _mark_rejected(item, data, rooms, today=today_day)
                    if errors:
                        # Отказ не записан — обещать «больше не спрошу» нельзя,
                        # и счётчик не растим: сохранять нечего.
                        skipped.append(f"{n}: {'; '.join(errors)}")
                        summary_lines.append(
                            f"{n}. {title} ({when}) — не записываю "
                            f"(но запомнить отказ не смог, спрошу ещё раз)"
                        )
                    else:
                        summary_lines.append(
                            f"{n}. {title} ({when}) — не записываю, "
                            f"больше про эту встречу не спрошу"
                        )
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
                        # Была разовым отказом (у не-recurring встречи id тот же) —
                        # снимаем метки, иначе ретеншн через 30 дней съест «никогда».
                        _clear_rejection_marks(existing)
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
                    # Передумал после «нет» — метки отказа снимаем, иначе живую
                    # запись через 30 дней вынесет ретеншн.
                    _clear_rejection_marks(existing)
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
            except Exception as e:  # noqa: BLE001
                logger.exception("номер %s (%s): не смог применить: %s", n, action, e)
                skipped.append(f"{n}: не смог применить ({type(e).__name__})")
                continue

        # Ретеншн едет тем же ходом: лок на watched.yaml уже наш, отдельный
        # проход по реестру заводить незачем (ответ на блок — почти ежедневный).
        pruned = prune_stale_rejections(data, today=today_day)

        if conflicts:
            # Владелец должен понимать, что бот увидел два решения по одной
            # встрече, а не молча выбрать одно из них.
            summary_lines.insert(0, (
                "По номерам " + ", ".join(str(x) for x in sorted(conflicts)) +
                " в ответе несколько разных решений — понял последнее."
            ))
        if unanswered:
            # Не отказ и не согласие: завтра спросим снова (см. `no_answer`).
            summary_lines.append(
                "По номерам " + ", ".join(str(x) for x in sorted(unanswered)) +
                " решения не было — спрошу ещё раз завтра."
            )

        # ISS-53: сохраняем и при одних только отказах — раньше условие было
        # `applied > 0 or ignored > 0`, и запись отказа терялась вместе с блокировкой.
        if applied > 0 or ignored > 0 or rejected > 0 or pruned > 0:
            try:
                save_watched(data)
            except Exception as e:  # noqa: BLE001
                # Fail-closed. Диск кончился / ФС только на чтение / слетели права:
                # решения НЕ на диске. Молчание тут = листенер отвечает владельцу
                # «Применил без summary.», то есть бот врёт про «нет», которое
                # потерял. Возвращаем ошибку — листенер скажет «Не применил: …».
                logger.exception("save_watched упал — ответ владельца НЕ применён: %s", e)
                return {
                    "error": (
                        f"не смог сохранить реестр встреч ({type(e).__name__}). "
                        "Ни одно решение не записано — ни «да», ни «нет». "
                        "Ответь ещё раз позже."
                    ),
                    "save_failed": True,
                    "summary_lines": [],
                    "pending_room": [],
                    "applied": 0,
                    "rejected": 0,
                    "ignored_forever": 0,
                    "pruned_rejections": 0,
                    "skipped": skipped,
                }
            saved_ok = True

        return {
            "summary_lines": summary_lines,
            "pending_room": pending_room,
            "applied": applied,
            "rejected": rejected,
            "ignored_forever": ignored,
            "no_answer": sorted(unanswered),
            # Диагностика чистки: в Telegram не показываем (владельцу нечего
            # решать), но в JSON и в логе видно, что реестр подмели.
            "pruned_rejections": pruned,
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


def _clear_rejection_marks(rec: dict[str, Any]) -> None:
    """Снять метки разового отказа (запись ожила или стала «глушилкой» серии).

    Вместе с метками снимается и `created_by_rejection`: запись, которую владелец
    включил обратно или превратил в «никогда», ретеншну больше не принадлежит.
    """
    rec.pop("rejected_at", None)
    rec.pop("rejected_start_at", None)
    rec.pop(REJECTION_OWNER_FIELD, None)


def _mark_rejected(
    item: dict[str, Any],
    data: dict[str, Any],
    rooms: dict[str, Any],
    *,
    today: date,
) -> list[str]:
    """ISS-53: записать отказ по КОНКРЕТНОМУ экземпляру. Возвращает ошибки валидации.

    Формат намеренно не новый: это обычная запись watched с `enabled=false`
    (её уже понимают и вечерний блок, и планировщик, и CLI) плюс метки —
    `rejected_at` (когда сказали «нет»), `rejected_start_at` (старт той самой
    встречи, по нему считает ретеншн) и `created_by_rejection` (эту запись завёл
    отказ — только такие ретеншн и удаляет).

    Три разных случая, и разница между ними — про сохранность чужих данных:
      • записи нет         → заводим свою, с метками;
      • запись наша        → обновляем метки (повторное «нет» не задваивает);
      • запись владельца   → только гасим (`enabled=false`). Метку смерти на
        чужую запись не ставим, а если она там уже была — снимаем: иначе через
        30 дней ретеншн унёс бы её вместе с привязкой чата, company/visibility
        и open_questions_mode.
    """
    rec = _build_record(item, room=None, kind_series_if_possible=False)
    rec["enabled"] = False
    rec["ignored"] = False
    rec[REJECTION_OWNER_FIELD] = True
    rec["rejected_at"] = today.isoformat()
    # Старт приезжает из snapshot — валидируем, иначе в реестр уедет «06.08.2026
    # 13:00» и ретеншн потом не сможет прочитать собственную дату.
    start_at = item.get("start_at")
    if isinstance(start_at, str) and _parse_day(start_at) is not None:
        rec["rejected_start_at"] = start_at
    elif start_at:
        logger.warning(
            "отказ %s: start_at=%r не читается как дата — пишу только rejected_at",
            rec["id"], start_at,
        )

    existing = find_watched(rec["id"], data)
    if existing is not None:
        existing["enabled"] = False
        if existing.get("ignored"):
            # «Нет» пришло по записи-глушилке («не предлагай эту серию»). У
            # не-recurring встречи id отказа и id глушилки совпадают, и снятие
            # ignored понизило бы вечное «никогда» до 30-дневного отказа.
            logger.info("отказ %s: запись уже «никогда» — оставляю как есть", rec["id"])
            return []
        if existing.get(REJECTION_OWNER_FIELD):
            # Повторное «нет» по той же встрече — не задваиваем, обновляем метки.
            existing["ignored"] = False
            existing["rejected_at"] = rec["rejected_at"]
            if "rejected_start_at" in rec:
                existing["rejected_start_at"] = rec["rejected_start_at"]
            return []
        # Запись вёл владелец — гасим, но не помечаем на удаление.
        _clear_rejection_marks(existing)
        logger.info(
            "отказ %s: запись владельца — выключил, метки отказа не ставлю", rec["id"]
        )
        return []

    errors = validate_watched_record(rec, rooms)
    if errors:
        return errors
    data["watched"].append(rec)
    return []


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

    try:
        with snap_path.open("r", encoding="utf-8") as f:
            snapshot = json.load(f)
        if not isinstance(snapshot, dict):
            raise ValueError("верхний уровень snapshot — не объект")
    except (OSError, ValueError) as e:
        logger.error("snapshot %s не читается: %s", snap_path, e)
        print(json.dumps({
            "error": "снимок вечернего списка не читается — перешли reply на свежее 📅-сообщение",
            "summary_lines": [], "pending_room": [],
        }, ensure_ascii=False))
        return 1

    # Битый элемент выкидываем поштучно: остальные решения владельца должны
    # доехать. Раньше KeyError на event_id/calendar_id ронял main() целиком —
    # на stdout не оставалось ничего, и листенер отвечал «Применил без summary.».
    items, dropped_items = sanitize_snapshot_items(snapshot.get("items"))
    for note in dropped_items:
        logger.warning("snapshot %s: %s — пропускаю", snap_path.name, note)
    if not items:
        print(json.dumps({"error": "snapshot пуст", "summary_lines": [], "pending_room": []}))
        return 1

    # Hard-проверка свежести: если reply пришёл сильно позже shown_date —
    # snapshot уже мог быть переписан более новым вечером, номера не те.
    # Допуск ±1 день — для кейса «дайджест в 23:50, ответ в 00:05»:
    # snapshot за 26.05, reply за 27.05 — валиден; reply за 28.05 — нет.
    shown_tz = snapshot.get("shown_tz") if isinstance(snapshot.get("shown_tz"), str) else None
    if args.reply_date and snapshot.get("shown_date"):
        try:
            # ISS-55: shown_date вечерний блок пишет в TZ отображения — считаем
            # день ответа в ней же (и берём её из самого snapshot, если она там
            # есть), иначе сравниваем разные календари.
            reply_dt = datetime.fromtimestamp(args.reply_date, tz=display_tz(shown_tz))
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

    if not decisions or all(d["action"] == "no_answer" for d in decisions):
        # Илья ответил «ок» / «угу» / эмодзи — claude не извлёк решений по номерам
        # (или разметил все номера как «ничего про них не сказано»).
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

    result = apply_decisions(decisions, items, shown_tz=shown_tz)
    print(json.dumps(result, ensure_ascii=False))
    logger.info(
        "applied=%d rejected=%d ignored_forever=%d no_answer=%d "
        "pruned_rejections=%d pending_room=%d save_failed=%s",
        result.get("applied", 0),
        result.get("rejected", 0),
        result.get("ignored_forever", 0),
        len(result.get("no_answer", [])),
        result.get("pruned_rejections", 0),
        len(result.get("pending_room", [])),
        bool(result.get("save_failed")),
    )
    return 2 if result.get("error") else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        # Пустой stdout листенер трактует как «Применил без summary.» — то есть
        # бот бодро рапортует об успехе там, где упал. Что бы ни сломалось,
        # владелец должен увидеть отказ, а не подтверждение.
        logger.exception("meetings_apply_reply упал: %s", e)
        print(json.dumps({
            "error": f"сбой применения ответа ({type(e).__name__}). Ничего не записал.",
            "summary_lines": [], "pending_room": [],
        }, ensure_ascii=False))
        sys.exit(2)
