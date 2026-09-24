"""Сообщение о сорванном входе бота — туда же, куда уходит протокол (ISS-60).

ЗАЧЕМ. 21–23.09.2026 бот не зашёл ни на одну встречу, а люди на встречах об этом
не знали и не включили запись сами. Решение владельца (23.09.2026): если бот не
смог подключиться, он пишет туда же, куда обычно присылает протокол (группа
серии или личка), и просит включить запись в интерфейсе Телемоста, чтобы
контекст встречи не потерялся, пока бот чинят.

КОМУ.
* Личная серия владельца → чат серии из реестра; чата нет → личка владельца.
* Наряд портала → туда, куда уйдёт его протокол (`delivery_route.resolve`):
  группа → группа; личка сотрудника → ему и владельцу; «только портал» →
  организатору (если его Телеграм известен) и владельцу («хотя бы организатору
  встречи и мне, если группа не назначена»).
* «Встречи нет / отклонили в зале ожидания» — это не поломка бота, а дело
  ссылки или людей: пишем только владельцу, группу не пугаем.
* Владелец всегда получает технический разбор (или одно общее сообщение, если
  адресат людей — и есть его личка).

Шлёт ботом Ватсона (`TELEGRAM_NOTARIUS_BOT_TOKEN`) — тем же, что доставляет
протоколы: он состоит в группах серий. Токена нет → владельцу через `push`,
группе — никак (и владелец об этом узнаёт).

Тестовый режим: `MEETING_NOTARY_JOIN_ALERT_TO_OWNER=1` — все сообщения уходят
владельцу с пометкой, кому ушло бы. Так проверяется маршрут без писем людям.

R9 («опасная тройка»): в сообщениях нет ни реплик, ни имён участников — только
название встречи, время и причина словами. В лог — только адресаты-режимы.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("notary.join_alert")

try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    _MSK = ZoneInfo("Europe/Moscow")
except Exception:  # noqa: BLE001 — нет базы поясов → фиксированный +3
    from datetime import timedelta

    _MSK = timezone(timedelta(hours=3))

# Кому адресовано сообщение людям.
AUD_GROUP = "group"            # чат серии (личная серия владельца)
AUD_GROUP_PORTAL = "group_portal"  # чат встречи сотрудников с портала
AUD_ORGANIZER = "organizer"    # личка организатора встречи портала
AUD_OWNER = "owner"            # личка владельца — как адресат протокола
AUD_OWNER_TECH = "owner_tech"  # технический разбор владельцу

#: Причины, где дело не в боте: группе не пишем.
MEETING_PROBLEM_REASONS = frozenset({"meeting_not_found", "admission_rejected"})
#: Не впустили из зала ожидания — людям пишем, но без «сбой бота».
WAITING_ROOM_REASONS = frozenset({"waiting_room_not_admitted", "admission_timeout"})

REASON_HUMAN: Dict[str, str] = {
    "name_input_not_found": "не нашёл поле для имени в лобби — похоже, Телемост поменял вход",
    "join_button_not_found": "не нашёл кнопку «Подключиться» — похоже, Телемост поменял вход",
    "still_in_lobby": "нажал «Подключиться», но остался в лобби",
    "navigation_failed": "не открылась страница встречи",
    "meeting_not_found": "Телемост пишет, что такой встречи нет или она уже завершена",
    "admission_rejected": "его отклонили в зале ожидания",
    "waiting_room_not_admitted": "его не впустили из зала ожидания",
    "admission_timeout": "его не впустили из зала ожидания",
    "admission_false_positive": "вход не подтвердился — в звонке бота не оказалось",
    "stuck_joining": "завис на входе",
    "crashed": "упал во время входа",
    "join_error": "ошибка при входе",
    "no_slot_for_retry": "ошибка при входе, а на повтор не нашлось свободного места",
}


def reason_human(reason: str) -> str:
    return REASON_HUMAN.get(reason) or f"ошибка при входе ({reason})"


@dataclass(frozen=True)
class Outgoing:
    chat_id: Optional[int]
    audience: str
    text: str


# ---------------------------------------------------------------------------
# Кто адресат
# ---------------------------------------------------------------------------


def _as_chat(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def owner_chat() -> Optional[int]:
    try:
        from notary.lib import delivery_route  # noqa: PLC0415

        return delivery_route.owner_chat_env()
    except Exception:  # noqa: BLE001
        raw = (os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        try:
            return int(raw) if raw else None
        except ValueError:
            return None


def people_target(entry: Dict[str, Any], *, watched: Any = None) -> tuple[str, Optional[int]]:
    """Куда уйдёт протокол этой встречи → (аудитория, чат).

    Ошибка чтения реестров → («owner», None): лучше написать только владельцу,
    чем угадать чужую группу.
    """
    series = str(entry.get("series") or "")
    order_id = entry.get("order_id")
    if not order_id:
        try:
            from notary.cli import registry  # noqa: PLC0415

            data = watched if isinstance(watched, dict) else registry.load_watched()
            chat = _as_chat(registry.get_telegram_chat_id_for_series(series, data))
        except Exception as e:  # noqa: BLE001
            logger.warning("[join-alert] привязка серии не прочитана (%s) — пишу владельцу", type(e).__name__)
            return (AUD_OWNER, None)
        return (AUD_GROUP, chat) if chat is not None else (AUD_OWNER, None)

    try:
        from notary.lib import delivery_route, orders  # noqa: PLC0415

        order = orders.get_order(str(order_id))
        if not order:
            return (AUD_OWNER, None)
        route = delivery_route.resolve(order, watched=watched)
    except Exception as e:  # noqa: BLE001
        logger.warning("[join-alert] маршрут наряда %s не вычислен (%s)", order_id, type(e).__name__)
        return (AUD_OWNER, None)
    mode = getattr(route, "mode", None)
    if mode == "group" and _as_chat(route.chat_id) is not None:
        return (AUD_GROUP_PORTAL, route.chat_id)
    if mode == "dm" and _as_chat(route.chat_id) is not None:
        return (AUD_ORGANIZER, route.chat_id)
    if mode == "portal_only" and _as_chat(route.owner_user_id) is not None:
        return (AUD_ORGANIZER, route.owner_user_id)
    return (AUD_OWNER, None)


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------


def meeting_name(entry: Dict[str, Any]) -> str:
    """Название встречи, как его видят люди в подписи протокола."""
    series = str(entry.get("series") or "").strip()
    meta: Dict[str, Any] = {"series": series}
    order_title = entry.get("series_title")
    if isinstance(order_title, str) and order_title.strip():
        meta["order"] = {"series_title": order_title.strip()}
    try:
        from notary.lib.protocol_to_tg import resolve_series_display_name  # noqa: PLC0415

        name = resolve_series_display_name(meta)
        if name:
            return name
    except Exception:  # noqa: BLE001 — подпись важнее точности названия
        pass
    return series or "встреча"


def when_text(start_at: Any, now: Optional[datetime] = None) -> str:
    """«сегодня в 14:40 МСК» / «24.09 в 14:40 МСК»."""
    try:
        start = datetime.fromisoformat(str(start_at).replace("Z", "+00:00"))
    except ValueError:
        return "в назначенное время"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    local = start.astimezone(_MSK)
    today = (now or datetime.now(timezone.utc)).astimezone(_MSK).date()
    day = "сегодня" if local.date() == today else local.strftime("%d.%m")
    return f"{day} в {local.strftime('%H:%M')} МСК"


def _tech_line(entry: Dict[str, Any], crash_kit: Optional[str]) -> str:
    container = entry.get("container") or "?"
    if crash_kit:
        kit = str(crash_kit).rsplit("/", 1)[-1]
        return f"Разбор: снимок экрана и кнопок — .crash-kits/{kit} на сервере; лог — docker logs {container}."
    return f"Лог: docker logs {container}."


def build_messages(
    entry: Dict[str, Any],
    reason: str,
    *,
    attempts: int,
    crash_kit: Optional[str] = None,
    watched: Any = None,
    now: Optional[datetime] = None,
) -> List[Outgoing]:
    """Все сообщения по одному сорванному входу, без отправки."""
    name = meeting_name(entry)
    when = when_text(entry.get("start_at"), now)
    why = reason_human(reason)
    tech = _tech_line(entry, crash_kit)
    owner = owner_chat()
    tries = f"Попыток: {attempts}."

    if reason in MEETING_PROBLEM_REASONS:
        return [Outgoing(owner, AUD_OWNER_TECH,
            f"ℹ️ Ватсон не зашёл на «{name}» ({when}): {why}. {tries}\n"
            f"Похоже, дело не в боте, а во встрече или ссылке — людям не писал.\n{tech}")]

    audience, chat = people_target(entry, watched=watched)
    waiting = reason in WAITING_ROOM_REASONS
    head_cause = (
        f"его не впустили из зала ожидания" if waiting
        else "сбой в работе бота-протоколиста, уже разбираемся"
    )
    out: List[Outgoing] = []
    if audience == AUD_OWNER:
        out.append(Outgoing(owner, AUD_OWNER,
            f"⚠️ Ватсон не смог подключиться к встрече «{name}» ({when}): {why}. {tries}\n\n"
            f"Если встреча идёт — включи запись в интерфейсе Яндекс Телемоста; после встречи "
            f"скажи Альфреду «обработай запись» — протокол соберётся как обычно.\n\n🔧 {tech}"))
        return out

    if audience == AUD_GROUP:
        people_text = (
            f"⚠️ Ватсон не смог подключиться к встрече «{name}» ({when}) — {head_cause}.\n\n"
            f"Если встреча идёт, а бота в ней нет, — пожалуйста, включите запись в интерфейсе "
            f"Яндекс Телемоста, а после встречи перешлите запись Илье. Так контекст встречи не "
            f"потеряется: протокол соберём из записи."
        )
        where = "в чат серии"
    elif audience == AUD_GROUP_PORTAL:
        people_text = (
            f"⚠️ Ватсон не смог подключиться к встрече «{name}» ({when}) — {head_cause}.\n\n"
            f"Если встреча идёт, а бота в ней нет, — пожалуйста, включите запись в интерфейсе "
            f"Яндекс Телемоста, а после встречи перешлите запись организатору встречи. Так "
            f"контекст встречи не потеряется."
        )
        where = "в чат встречи (портал)"
    else:  # AUD_ORGANIZER
        people_text = (
            f"⚠️ Ватсон не смог подключиться к вашей встрече «{name}» ({when}) — {head_cause}.\n\n"
            f"Если встреча идёт — пожалуйста, включите запись в интерфейсе Яндекс Телемоста и "
            f"сохраните её после встречи, чтобы контекст не потерялся."
        )
        where = "организатору встречи (портал) в личку"
    out.append(Outgoing(chat, audience, people_text))
    out.append(Outgoing(owner, AUD_OWNER_TECH,
        f"🔧 Ватсон не зашёл на «{name}» ({when}): {why}. {tries}\n"
        f"Людям написал: {where}.\n"
        f"Если ты на встрече — включи запись сам; после встречи скажи Альфреду «обработай запись».\n"
        f"{tech}"))
    return out


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

_AUDIENCE_LABEL = {
    AUD_GROUP: "чат серии",
    AUD_GROUP_PORTAL: "чат встречи портала",
    AUD_ORGANIZER: "личка организатора",
    AUD_OWNER: "твоя личка",
    AUD_OWNER_TECH: "твоя личка (тех.)",
}


def _redirect_to_owner() -> bool:
    return (os.environ.get("MEETING_NOTARY_JOIN_ALERT_TO_OWNER") or "").strip() in {"1", "true", "yes"}


def _default_sender(chat_id: int, text: str) -> bool:
    token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    if not token:
        return False
    from notary.lib import telegram_api  # noqa: PLC0415

    # Тестовый режим — беззвучно: проверка маршрута не должна будить владельца.
    telegram_api.send_message(token, chat_id, text, disable_notification=_redirect_to_owner())
    return True


def send(
    entry: Dict[str, Any],
    reason: str,
    *,
    attempts: int,
    crash_kit: Optional[str] = None,
    watched: Any = None,
    now: Optional[datetime] = None,
    sender: Optional[Callable[[int, str], bool]] = None,
    owner_push: Optional[Callable[[str], bool]] = None,
) -> List[Dict[str, Any]]:
    """Отправить все сообщения. Возвращает отчёт по каждому (без текстов)."""
    sender = sender or _default_sender
    if owner_push is None:
        from notary.lib.notify import push as _push  # noqa: PLC0415

        owner_push = lambda text: _push(text, dedupe=False, silent=_redirect_to_owner())  # noqa: E731
    msgs = build_messages(entry, reason, attempts=attempts, crash_kit=crash_kit, watched=watched, now=now)
    redirect = _redirect_to_owner()
    owner = owner_chat()
    report: List[Dict[str, Any]] = []
    failed_people: List[str] = []
    for m in msgs:
        chat, text = m.chat_id, m.text
        if redirect and m.audience not in (AUD_OWNER, AUD_OWNER_TECH):
            text = f"[ТЕСТ — на бою ушло бы: {_AUDIENCE_LABEL.get(m.audience, m.audience)}]\n{text}"
            chat = owner
        ok = False
        if chat is not None:
            try:
                ok = bool(sender(chat, text))
            except Exception as e:  # noqa: BLE001 — сеть/Телеграм: пробуем резерв ниже
                logger.warning("[join-alert] отправка (%s) упала: %s", m.audience, type(e).__name__)
                ok = False
        if not ok and m.audience in (AUD_OWNER, AUD_OWNER_TECH):
            ok = bool(owner_push(text))
        if not ok and m.audience not in (AUD_OWNER, AUD_OWNER_TECH):
            failed_people.append(_AUDIENCE_LABEL.get(m.audience, m.audience))
        report.append({"audience": m.audience, "sent": ok})
        logger.info("[join-alert] %s → %s", m.audience, "отправлено" if ok else "НЕ отправлено")
    if failed_people:
        owner_push(
            "⚠️ Не смог написать людям о сорванном входе Ватсона ("
            + ", ".join(failed_people)
            + "): Телеграм не принял сообщение. Предупреди участников сам."
        )
    return report
