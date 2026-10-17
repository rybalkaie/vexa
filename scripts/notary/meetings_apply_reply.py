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
    resolve_room_to_url,
    save_watched,
    validate_watched_record,
)
from notary.lib.meeting_identity import (  # noqa: E402
    MATCH_ICAL,
    MATCH_ID,
    MATCH_NONE,
    compare,
    fingerprint,
    is_owner_named,
)
from notary.lib.claude_cli import model_args  # noqa: E402 — модель словом + усилие по уровню

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
# Модель разбора — уровень Sonnet явным словом (без слова клиент взял бы своё умолчание,
# сейчас Opus), усилие по уровню (`claude_cli.model_args`); env-override без передеплоя.
APPLY_REPLY_MODEL = (os.environ.get("APPLY_REPLY_MODEL") or "").strip() or "sonnet"

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
    cmd = [CLAUDE_BIN, "--print"] + model_args(APPLY_REPLY_MODEL)
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
                    errors, muted = _mark_rejected(item, data, rooms, today=today_day)
                    if errors:
                        # Отказ не записан — обещать «больше не спрошу» нельзя,
                        # и счётчик не растим: сохранять нечего.
                        skipped.append(f"{n}: {'; '.join(errors)}")
                        summary_lines.append(
                            f"{n}. {title} ({when}) — не записываю "
                            f"(но запомнить отказ не смог, спрошу ещё раз)"
                        )
                    else:
                        line = (
                            f"{n}. {title} ({when}) — не записываю, "
                            f"больше про эту встречу не спрошу"
                        )
                        if muted:
                            # ISS-54: гасим не только копию, но и запись, по
                            # которой бот реально приходил. Молча выключать
                            # чужую живую запись нельзя — владелец должен
                            # увидеть это в тот же вечер.
                            line += (
                                " (заодно выключил "
                                + _muted_ru(muted)
                                + " — это та же встреча из другого календаря)"
                            )
                        # Та же встреча может приезжать ЭКЗЕМПЛЯРОМ уже ведомой
                        # СЕРИИ (копия из второго календаря). Запись серии не
                        # гасим — «нет» сказано про одну встречу, — но и молчать
                        # нельзя: владелец должен знать, что серия жива.
                        # Сам старт при этом не поставится
                        # (`scheduler.drop_rejected_occurrences`).
                        series_twins = _live_series_twin_ids(item, data, rooms)
                        if series_twins:
                            line += (
                                " (эту встречу приносит ведомая серия — "
                                + _muted_ru(series_twins)
                                + "; серию не трогаю, но на эту встречу не приду)"
                            )
                        summary_lines.append(line)
                        rejected += 1
                    continue

                if action == "ignore_forever":
                    # создаём «глушащую» запись с enabled=false, ignored=true
                    rec = _build_record(item, room=None, kind_series_if_possible=True)
                    rec["enabled"] = False
                    rec["ignored"] = True
                    # Не задваиваем — если уже есть с тем же id, обновим флаги.
                    existing = find_watched(rec["id"], data)
                    # ISS-54: у копии из второго календаря id другой. Глушить надо
                    # И её (иначе вечерний блок предложит копию завтра — он
                    # ключует (calendar_id, event_id) и про близнеца не знает),
                    # И ту запись, по которой бот реально приходит, — иначе
                    # «никогда» останется обещанием на словах.
                    muted = _mute_twins(
                        item, data, rooms, skip=existing,
                        series=bool(rec.get("recurring_event_id")), forever=True,
                    )
                    if existing is not None:
                        existing["enabled"] = False
                        existing["ignored"] = True
                        # Была разовым отказом (у не-recurring встречи id тот же) —
                        # снимаем метки, иначе ретеншн через 30 дней съест «никогда».
                        _clear_rejection_marks(existing)
                        _merge_fingerprint(existing, item, proof=MATCH_ID)
                    else:
                        errors = validate_watched_record(rec, rooms)
                        if errors:
                            skipped.append(f"{n}: {'; '.join(errors)}")
                            continue
                        data["watched"].append(rec)
                    line = f"{n}. {title} ({when}) — больше не предлагаю эту серию"
                    if muted:
                        line += (
                            " (заодно выключил " + _muted_ru(muted)
                            + " — это та же встреча из другого календаря)"
                        )
                    summary_lines.append(line)
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
                # Кого оживляем. Запись под тем же id (повторное «да», след
                # прошлого «нет») — ЛИШЬ ОДИН из кандидатов, а не готовый ответ.
                # Раньше поиск по id стоял ДО дедупа и коротко замыкал его: у
                # отказной заглушки ISS-53 id считается из того же события, что
                # и у новой записи, поэтому по id она находилась ВСЕГДА, а
                # `find_duplicate_watched` (который и знает, что оживлять надо
                # запись с привязкой чата и режимом) не звался вовсе. Оживала
                # заглушка: встреча шла серией без чата (протокол уезжал
                # владельцу в личку) и с дефолтным режимом открытых вопросов, а
                # настроенная запись владельца оставалась выключенной навсегда —
                # и владельцу про это не говорилось ни слова.
                by_id = find_watched(rec["id"], data)
                candidates: list[tuple] = []
                if by_id is not None:
                    candidates.append((by_id, MATCH_ID, False))
                # ISS-54: то же событие из другого календаря приезжает с
                # другим event_id → и id записи другой. Заводить вторую
                # запись нельзя: на встречу придут два бота.
                for twin in find_duplicates_watched(
                        item, data, series=bool(rec.get("recurring_event_id")),
                        rooms=rooms, room=chosen_room):
                    if twin[0] is not by_id:
                        candidates.append(twin)
                existing, proof, conflict = (None, MATCH_ID, False)
                if candidates:
                    existing, proof, conflict = min(
                        candidates, key=lambda m: _dup_rank(m[0]))
                # «Дубль» для строки владельцу — это «оживил НЕ ту запись, что
                # под ключом этой копии»: именно тогда он обязан узнать, какая
                # запись реально поведёт встречу.
                duplicate = existing if (existing is not None and existing is not by_id) else None
                was_rejected_by_id = bool(by_id is not None and (
                    by_id.get(REJECTION_OWNER_FIELD) or by_id.get("rejected_start_at")))
                # «Да» отменяет прошлое «нет» по ЭТОЙ встрече целиком — со всех
                # её записей, а не только с ожившей. Иначе оставшаяся заглушка
                # продолжала бы гасить старт в планировщике (`drop_rejected_
                # occurrences`), и согласие владельца снова осталось бы словами.
                for other, _p, _c in candidates:
                    if other is existing:
                        continue
                    if other.get(REJECTION_OWNER_FIELD) or other.get("rejected_start_at"):
                        _clear_rejection_marks(other)
                        logger.info(
                            "«да» по номеру %s снял отказ с записи «%s» (она "
                            "остаётся выключенной — встречу ведёт «%s»)",
                            n, other.get("id"), existing.get("id") if existing else "?",
                        )
                if existing is not None:
                    was_ignored = bool(existing.get("ignored"))
                    # обновим enabled=True, ignored=False — Илья «передумал»
                    existing["enabled"] = True
                    existing["ignored"] = False
                    # Передумал после «нет» — метки отказа снимаем, иначе живую
                    # запись через 30 дней вынесет ретеншн.
                    _clear_rejection_marks(existing)
                    if chosen_room:
                        existing["room"] = chosen_room
                    _merge_fingerprint(existing, item, proof=proof)
                    if duplicate is not None:
                        logger.info(
                            "ISS-54: номер %s (%s) — та же встреча, что запись «%s» "
                            "(%s; календарь %s vs %s); второй записи не завожу",
                            n, item.get("start_at"), existing.get("id"), proof,
                            existing.get("calendar_id"), item.get("calendar_id"),
                        )
                        line = (
                            f"{n}. {title} ({when}) — это та же встреча, что уже "
                            f"в реестре («{existing.get('id')}»), просто видна в двух "
                            f"календарях: пишу по ней, второй записи не завожу"
                        )
                        if conflict:
                            # Комната и секунда те же, а события календаря разные.
                            # Схлопнуть обязаны (иначе два бота), но владелец
                            # должен знать: возможно, он продублировал событие.
                            line += (
                                "; в календаре это два РАЗНЫХ события в одной "
                                "комнате в одну секунду — проверь, если это "
                                "разные встречи"
                            )
                        if was_ignored:
                            line += "; снял с неё «больше не предлагать»"
                        if was_rejected_by_id:
                            # Владелец сказал «нет», потом передумал. Оживает не
                            # заглушка отказа, а настроенная запись — и он должен
                            # видеть, что она другая (на ней чат серии и режим).
                            line += "; прошлое «нет» по этой встрече снял"
                        summary_lines.append(line)
                    else:
                        line = f"{n}. {title} ({when}) — уже была в реестре, включил"
                        if was_ignored:
                            line += ", снял «больше не предлагать»"
                        summary_lines.append(line)
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


def _muted_ru(ids: list[str]) -> str:
    """«запись «X»» / «записи «X», «Y»» — для строки владельцу."""
    quoted = ", ".join("«%s»" % i for i in ids)
    return ("запись " if len(ids) == 1 else "записи ") + quoted


def _mute_twins(
    item: dict[str, Any],
    data: dict[str, Any],
    rooms: dict[str, Any],
    *,
    skip: dict[str, Any] | None,
    series: bool,
    forever: bool,
) -> list[str]:
    """Погасить ВСЕ записи-близнецы этой встречи. Возвращает их id.

    Близнец — запись про ту же встречу под другим `event_id` (то же событие
    видно во втором календаре). Если её не погасить, «нет» и «никогда» остаются
    словами: бот всё равно придёт — по ней.

    Гасим все найденные, а не первую: в боевом реестре на одну встречу уже лежат
    две записи. Чужую (не нашу запись отказа) на удаление НЕ помечаем — иначе
    ретеншн через 30 дней унёс бы её вместе с привязкой чата, разметкой
    company/visibility и режимом открытых вопросов.
    """
    muted: list[str] = []
    for twin, proof, _conflict in find_duplicates_watched(
            item, data, series=series, rooms=rooms):
        if twin is skip:
            continue
        was_live = bool(twin.get("enabled", True))
        _merge_fingerprint(twin, item, proof=proof)
        twin["enabled"] = False
        if forever:
            twin["ignored"] = True
        if not twin.get(REJECTION_OWNER_FIELD):
            _clear_rejection_marks(twin)
        if not was_live:
            # Запись и так была выключена — бот по ней не приходил, владельцу
            # сообщать не о чем.
            continue
        muted.append(str(twin.get("id")))
        logger.info(
            "ISS-54: «%s» по копии встречи — выключил запись-близнеца «%s» "
            "(%s; календарь %s vs %s)",
            "никогда" if forever else "нет", twin.get("id"), proof,
            twin.get("calendar_id"), item.get("calendar_id"),
        )
    return muted


def _live_series_twin_ids(
    item: dict[str, Any], data: dict[str, Any], rooms: dict[str, Any],
) -> list[str]:
    """ЖИВЫЕ записи СЕРИИ, которые приносят эту же встречу. Их id, best-effort.

    `_mute_twins` ищет близнецов только своего рода (`series=False` у отказа):
    «серия» и «экземпляр» — разные обещания владельцу, и гасить всю серию из-за
    одного «нет» нельзя. Но и промолчать нельзя — раньше владелец слышал «не
    записываю, больше про эту встречу не спрошу», а бот всё равно приходил по
    записи серии. Сам старт теперь снимает планировщик по следу отказа; здесь —
    только честная строка владельцу.
    """
    out: list[str] = []
    try:
        for twin, _proof, _conflict in find_duplicates_watched(
                item, data, series=True, rooms=rooms):
            if not twin.get("enabled", True) or twin.get("ignored", False):
                continue
            out.append(str(twin.get("id")))
    except Exception as e:  # noqa: BLE001 — строка владельцу опциональна, отказ уже записан
        logger.warning("не смог проверить записи серии для отказа: %s", type(e).__name__)
    return out


def _mark_rejected(
    item: dict[str, Any],
    data: dict[str, Any],
    rooms: dict[str, Any],
    *,
    today: date,
) -> tuple:
    """ISS-53: записать отказ по КОНКРЕТНОМУ экземпляру.

    Возвращает (ошибки валидации, id погашенных записей-близнецов).

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
    # ISS-54: «нет» пришло по копии события из второго календаря. Гасить надо
    # запись-близнеца — иначе бот всё равно придёт по ней, и отказ владельца
    # окажется услышанным только на словах.
    muted = _mute_twins(item, data, rooms, skip=existing, series=False, forever=False)

    if existing is not None:
        existing["enabled"] = False
        _merge_fingerprint(existing, item, proof=MATCH_ID)
        if existing.get("ignored"):
            # «Нет» пришло по записи-глушилке («не предлагай эту серию»). У
            # не-recurring встречи id отказа и id глушилки совпадают, и снятие
            # ignored понизило бы вечное «никогда» до 30-дневного отказа.
            logger.info("отказ %s: запись уже «никогда» — оставляю как есть", rec["id"])
            return ([], muted)
        if existing.get(REJECTION_OWNER_FIELD):
            # Повторное «нет» по той же встрече — не задваиваем, обновляем метки.
            existing["ignored"] = False
            existing["rejected_at"] = rec["rejected_at"]
            if "rejected_start_at" in rec:
                existing["rejected_start_at"] = rec["rejected_start_at"]
            return ([], muted)
        # Запись вёл владелец — гасим, но не помечаем на удаление.
        _clear_rejection_marks(existing)
        logger.info(
            "отказ %s: запись владельца — выключил, метки отказа не ставлю", rec["id"]
        )
        return ([], muted)

    # ISS-54/ISS-53: след под ключом САМОЙ копии нужен, даже когда близнеца уже
    # погасили. Вечерний блок ключует (calendar_id, event_id) и про близнеца не
    # знает — без этой записи он завтра предложит ту же встречу снова, сразу
    # после обещания «больше про эту встречу не спрошу».
    errors = validate_watched_record(rec, rooms)
    if errors:
        return (errors, muted)
    data["watched"].append(rec)
    return ([], muted)


# ---------------------------------------------------------------------------
# ISS-54: одна встреча — одна запись, даже если событие видно в двух календарях
# ---------------------------------------------------------------------------
# Боевой случай: выставка СТ-1 27.07 лежала и в личном календаре, и в календаре
# Anzhee. Google отдал её двумя разными `event_id` → в реестре появились две
# записи (`oneoff-ef50c3ab` и `anzhee-vystavka-st1-20260727`) → на встречу
# пришли два бота. `event_id`/`recurring_event_id` тут не помогают: у копий они
# РАЗНЫЕ. Точный ключ — `iCalUID` события: у копий он совпадает.
#
# Приметы пишем прямо в запись, потому что сверять их потом не с чем: в
# watched.yaml нет ни времени встречи, ни ссылки, а лезть за ними в календарь на
# каждое «да» — лишний сетевой поход.
#   • `ical_uid`        — точный ключ (когда календарный слой его дал);
#   • `event_start_at`  — старт того экземпляра, по которому завели запись.
#     Обязателен рядом с `iCalUID`: у всех экземпляров одной рекуррентной серии
#     `iCalUID` ОБЩИЙ, и без времени «вторник» и «четверг» одной серии
#     схлопнулись бы в одну запись;
#   • `event_url`       — ссылка из события; вторая половина страховочного ключа
#     «время старта + URL». На боевом пути этот ключ — ГЛАВНЫЙ: `iCalUID` в
#     снапшот вечернего блока пока не приезжает.
#
# Сама формула тождества — общая с планировщиком, живёт в
# `lib/meeting_identity.py`. Раньше она была скопирована в оба файла и успела
# разойтись: два дедупа давали противоположные ответы на одном случае.
FINGERPRINT_FIELDS = ("ical_uid", "event_start_at", "event_url")


def _clean_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def meeting_fingerprint(item: dict[str, Any]) -> dict[str, Any]:
    """Приметы встречи из элемента snapshot (см. блок выше).

    `ical_uid` в snapshot появляется, когда его начнёт класть вечерний блок;
    пока его нет — работает страховка «время старта + URL». Оба варианта
    поддерживаем одновременно, чтобы включение шло без миграции реестра.
    """
    return {
        "ical_uid": _clean_str(item.get("ical_uid")),
        "event_start_at": _clean_str(item.get("start_at")),
        "event_url": _clean_str(item.get("url_in_event")),
    }


def _room_url(value: Any, rooms: dict[str, Any] | None) -> str | None:
    """`@name` из реестра/ответа → URL комнаты. Не разрешилось — None.

    Нужен для встреч БЕЗ ссылки в событии: владелец называет комнату словами
    («да, @t11»), в событии календаря ссылки нет — и страховочный ключ
    «время + URL» без этого шага не срабатывал, давая вторую запись.
    """
    if not value or not rooms:
        return None
    try:
        return resolve_room_to_url(value, rooms)
    except SystemExit:
        # Комната исчезла из rooms.yaml — сверять нечем, но падать тут нельзя.
        return None
    except Exception:  # noqa: BLE001
        return None


def _record_fp(rec: dict[str, Any], rooms: dict[str, Any] | None):
    return fingerprint(
        rec.get("event_start_at"),
        rec.get("event_url") or _room_url(rec.get("room"), rooms),
        rec.get("ical_uid"),
    )


def _item_fp(fp: dict[str, Any], room: str | None, rooms: dict[str, Any] | None):
    return fingerprint(
        fp.get("event_start_at"),
        fp.get("event_url") or _room_url(room, rooms),
        fp.get("ical_uid"),
    )


def find_duplicates_watched(
    item: dict[str, Any],
    data: dict[str, Any],
    *,
    series: bool,
    rooms: dict[str, Any] | None = None,
    room: str | None = None,
) -> list[tuple]:
    """ВСЕ записи реестра про ту же встречу. Список (запись, чем доказано, конфликт).

    Ищем и среди выключенных записей: отказ владельца (ISS-53, `enabled=false`)
    — это тоже «встреча известна». Завести поверх отказа новую запись значило бы
    вернуть владельцу встречу, от которой он отказался.

    Не «первую попавшуюся», а все: в боевом реестре прямо сейчас лежат ДВА
    дубля одной встречи, и «нет», погасившее один из них, было бы исполнено
    наполовину — бот всё равно пришёл бы по второму.

    Записи без примет (старый реестр, ручные записи) сюда не попадают — их
    подстраховывает схлопывание очереди в scheduler по «URL + время старта».
    """
    cand = _item_fp(meeting_fingerprint(item), room, rooms)
    if not cand.ical and not (cand.start and cand.url):
        return []
    out: list[tuple] = []
    for rec in data.get("watched", []) or []:
        if not isinstance(rec, dict) or rec.get("type") != "google-calendar":
            continue
        # Запись другого рода подменой не считаем: «серия» и «только этот
        # экземпляр» — разные обещания владельцу. Двойного бота на их
        # пересечении подстрахует схлопывание очереди в scheduler.
        if bool(rec.get("recurring_event_id")) != series:
            continue
        proof, conflict = compare(_record_fp(rec, rooms), cand, series_scope=series)
        if proof:
            out.append((rec, proof, conflict))
    return out


def _dup_rank(rec: dict[str, Any]) -> tuple:
    """Кого из найденных копий брать первой: живую настроенную запись владельца.

    Три ступени, в порядке убывания важности:
      1. живая раньше выключенной — по живой бот ходит прямо сейчас;
      2. не-отказная раньше отказной заглушки — надгробие прошлого «нет» не
         несёт ни привязки чата, ни режима открытых вопросов;
      3. имя дал владелец раньше автоимени `oneoff-<hash>` — ЭТО ТО ЖЕ ПРАВИЛО,
         по которому планировщик выбирает, по какой записи вести встречу
         (`scheduler._pick_winner` → `meeting_identity.is_owner_named`).

    Третья ступень появилась после сквозной проверки: без неё среди двух ЖИВЫХ
    копий решал порядок записей в файле, и на встречу приезжал бот по записи
    владельца, а владельцу называлась (и приметами лечилась) голая автозапись —
    вместе с её дефолтным режимом открытых вопросов и без привязки чата.
    """
    return (
        not rec.get("enabled", True),
        bool(rec.get(REJECTION_OWNER_FIELD)),
        not is_owner_named(rec.get("id")),
    )


def find_duplicate_watched(
    item: dict[str, Any],
    data: dict[str, Any],
    *,
    series: bool,
    rooms: dict[str, Any] | None = None,
    room: str | None = None,
) -> tuple:
    """Лучшая копия: (запись | None, чем доказано, конфликт точных ключей).

    Лучшая — живая настроенная запись владельца, а не надгробие прошлого отказа:
    оживлять надо ту, на которой висят привязка чата, разметка и режим открытых
    вопросов.
    """
    matches = find_duplicates_watched(
        item, data, series=series, rooms=rooms, room=room)
    if not matches:
        return (None, MATCH_NONE, False)
    return min(matches, key=lambda m: _dup_rank(m[0]))


def _merge_fingerprint(
    rec: dict[str, Any],
    item: dict[str, Any],
    *,
    proof: str = MATCH_ID,
) -> None:
    """Дописать недостающие приметы в существующую запись.

    Только пустые поля: чужое значение не перетираем (запись мог вести владелец).
    Так реестр лечится сам — по мере того, как владелец касается своих записей
    (любым ответом: «да», «нет», «никогда»).

    `ical_uid` дописываем ТОЛЬКО когда это буквально та же запись или тождество
    доказано точным ключом. Иначе одно срабатывание страховки «комната + секунда»
    вписало бы в запись точный ключ ЧУЖОГО события — и дальше он считался бы
    доказательством, а откатить его нечем: в YAML лежит правдоподобное значение.
    """
    fp = meeting_fingerprint(item)
    for field in FINGERPRINT_FIELDS:
        if field == "ical_uid" and proof not in (MATCH_ID, MATCH_ICAL):
            continue
        if fp.get(field) and not _clean_str(rec.get(field)):
            rec[field] = fp[field]


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
    # ISS-54: приметы для опознания той же встречи из другого календаря.
    for field, value in meeting_fingerprint(item).items():
        if value:
            rec[field] = value
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
