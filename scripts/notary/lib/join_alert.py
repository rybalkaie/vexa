"""Сообщения о сбоях бота — туда же, куда уходит протокол (ISS-60, 23–24.09.2026).

ЗАЧЕМ. 21–23.09.2026 бот не зашёл ни на одну встречу, а люди на встречах об этом
не знали и не включили запись сами. Решение владельца (23.09.2026): если бот не
смог подключиться (или сломался посреди встречи), он пишет туда же, куда обычно
присылает протокол (группа серии или личка), и просит включить запись в
интерфейсе Телемоста, чтобы контекст встречи не потерялся, пока бота чинят.

КОМУ.
* Личная серия владельца → чат серии из реестра; чата нет → личка владельца.
* Наряд портала → туда, куда уйдёт его протокол (`delivery_route.resolve`):
  группа → группа; личка сотрудника → ему; «только портал» → организатору,
  если его Телеграм известен. Владелец получает технический разбор всегда
  («хотя бы организатору встречи и мне, если группа не назначена»).
* «Встречи нет / отклонили» — не поломка бота, а дело ссылки или людей: по
  личной серии — только владельцу; по наряду — организатору (ссылку вводил он)
  и владельцу. Группу не пугаем.
* Адресат людей совпал с личкой владельца → одно общее сообщение, не два.

КТО ШЛЁТ. Только сторож на хосте (`join_watch`), ботом Ватсона
(`TELEGRAM_NOTARIUS_BOT_TOKEN`) — тем же, что доставляет протоколы. В контейнер с
браузером токен не передаётся намеренно.

Тестовый режим: `MEETING_NOTARY_JOIN_ALERT_TO_OWNER=1` — все сообщения уходят
владельцу беззвучно, с пометкой, кому и в какой чат ушло бы.

R9 («опасная тройка»): в сообщениях нет ни реплик, ни имён участников, ни ссылки
на комнату — только название встречи, время и причина словами. В лог — только
аудитории и результат.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("notary.join_alert")

try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    _MSK = ZoneInfo("Europe/Moscow")
except Exception:  # noqa: BLE001 — нет базы поясов → фиксированный +3
    from datetime import timedelta

    _MSK = timezone(timedelta(hours=3))

# Кому адресовано сообщение людям.
AUD_GROUP = "group"                # чат серии (личная серия владельца)
AUD_GROUP_PORTAL = "group_portal"  # чат встречи сотрудников с портала
AUD_ORGANIZER = "organizer"        # личка организатора встречи портала
AUD_OWNER = "owner"                # личка владельца — как адресат протокола
AUD_OWNER_PORTAL = "owner_portal"  # встреча портала, до организатора не достучаться
AUD_OWNER_TECH = "owner_tech"      # технический разбор владельцу

_PEOPLE = (AUD_GROUP, AUD_GROUP_PORTAL, AUD_ORGANIZER)
_OWNER = (AUD_OWNER, AUD_OWNER_PORTAL, AUD_OWNER_TECH)

#: Причины, где дело не в боте: группе не пишем. `meeting_url_rejected` (фаза 3а,
#: хвост С10) — ссылка ведёт не в Телемост: бот её не открывает, людям о «сбое
#: бота» писать нечего, проверить ссылку — владельцу или организатору.
MEETING_PROBLEM_REASONS = frozenset({"meeting_not_found", "admission_rejected", "meeting_url_rejected"})
#: Не впустили из зала ожидания — людям пишем, но без «сбой бота».
WAITING_ROOM_REASONS = frozenset({"waiting_room_not_admitted", "admission_timeout"})

# Виды сообщений.
KIND_JOIN_FAILED = "join_failed"
KIND_EVICTED = "evicted_failure"
KIND_DEAF = "deaf_after_start"
KIND_WAITING = "waiting_room"

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
    "admission_unclear": "нажал «Подключиться», но не узнал экран звонка — похоже, Телемост поменял экран",
    "meeting_url_rejected": "ссылка встречи ведёт не в Яндекс Телемост — бот открывает только его",
    "stuck_joining": "завис на входе",
    "stuck_admission": "завис, ожидая впуска во встречу",
    "crashed": "упал во время входа",
    "crashed_after_join": "упал сразу после входа",
    "join_error": "ошибка при входе",
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


def _portal_route(entry: Dict[str, Any], watched: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """(Route, наряд) или (None, None), если наряд не прочитался."""
    try:
        from notary.lib import delivery_route, orders  # noqa: PLC0415

        order = orders.get_order(str(entry.get("order_id")))
        if not order:
            return None, None
        return delivery_route.resolve(order, watched=watched), order
    except Exception as e:  # noqa: BLE001
        logger.warning("[join-alert] маршрут наряда не вычислен (%s)", type(e).__name__)
        return None, None


def people_target(entry: Dict[str, Any], *, watched: Any = None) -> Tuple[str, Optional[int]]:
    """Куда уйдёт протокол этой встречи → (аудитория, чат).

    Ошибка чтения реестров → личка владельца (для наряда — «владелец за
    организатора»): лучше написать только владельцу, чем угадать чужую группу.
    """
    owner = owner_chat()
    if not entry.get("order_id"):
        series = str(entry.get("series") or "")
        try:
            from notary.cli import registry  # noqa: PLC0415

            data = watched if isinstance(watched, dict) else registry.load_watched()
            chat = _as_chat(registry.get_telegram_chat_id_for_series(series, data))
        except Exception as e:  # noqa: BLE001
            logger.warning("[join-alert] привязка серии не прочитана (%s) — пишу владельцу", type(e).__name__)
            return (AUD_OWNER, owner)
        if chat is None or chat == owner:
            return (AUD_OWNER, owner)
        return (AUD_GROUP, chat)

    route, _order = _portal_route(entry, watched)
    mode = getattr(route, "mode", None)
    chat: Optional[int] = None
    audience = AUD_OWNER_PORTAL
    if mode == "group" and _as_chat(route.chat_id) is not None:
        audience, chat = AUD_GROUP_PORTAL, route.chat_id
    elif mode == "dm" and _as_chat(route.chat_id) is not None:
        audience, chat = AUD_ORGANIZER, route.chat_id
    elif mode == "portal_only" and _as_chat(route.owner_user_id) is not None:
        audience, chat = AUD_ORGANIZER, route.owner_user_id
    if chat is not None and chat == owner:
        # Организатор встречи портала — сам владелец: одно сообщение ему.
        return (AUD_OWNER, owner)
    if chat is None:
        return (AUD_OWNER_PORTAL, owner)
    return (audience, chat)


def organizer_chat(entry: Dict[str, Any], *, watched: Any = None) -> Optional[int]:
    """Личка организатора наряда (для «ссылка не открывается»), если известна."""
    if not entry.get("order_id"):
        return None
    route, _order = _portal_route(entry, watched)
    tg = getattr(route, "owner_user_id", None) if route is not None else None
    return _as_chat(tg)


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


def _people_text(kind: str, audience: str, name: str, when: str, reason: str) -> str:
    """Текст людям (группе / организатору / владельцу как адресату протокола)."""
    to_whom = "организатору встречи" if audience in (AUD_GROUP_PORTAL,) else "Илье"
    your = "вашей " if audience == AUD_ORGANIZER else ""
    if kind == KIND_WAITING:
        return (
            f"🟡 Ватсон ждёт в зале ожидания {your}встречи «{name}» ({when}). "
            f"Впустите, пожалуйста, участника «Бот — протокол встречи» — иначе встреча не запишется."
        )
    if kind in (KIND_EVICTED, KIND_DEAF):
        what = (
            "его выкинуло из Телемоста, вернуться не получилось" if kind == KIND_EVICTED
            else "он перестал слышать звук встречи"
        )
        head = f"⚠️ Ватсон больше не записывает {your}встречу «{name}» ({when}) — {what}."
    else:
        cause = (
            "его не впустили из зала ожидания" if reason in WAITING_ROOM_REASONS
            else "сбой в работе бота-протоколиста, уже разбираемся"
        )
        head = f"⚠️ Ватсон не смог подключиться к {your}встрече «{name}» ({when}) — {cause}."
    if audience == AUD_ORGANIZER:
        tail = (
            "Если встреча идёт — пожалуйста, включите запись в интерфейсе Яндекс Телемоста и "
            "сохраните её после встречи, чтобы контекст не потерялся."
        )
    elif audience == AUD_GROUP_PORTAL:
        tail = (
            "Если встреча идёт, а бота в ней нет, — пожалуйста, включите запись в интерфейсе "
            "Яндекс Телемоста, а после встречи перешлите запись организатору встречи. Так "
            "контекст встречи не потеряется."
        )
    else:
        tail = (
            "Если встреча идёт, а бота в ней нет, — пожалуйста, включите запись в интерфейсе "
            f"Яндекс Телемоста, а после встречи перешлите запись {to_whom}. Так контекст встречи "
            "не потеряется: протокол соберём из записи."
        )
    return f"{head}\n\n{tail}"


def build_messages(
    entry: Dict[str, Any],
    reason: str,
    *,
    attempts: int = 1,
    kind: str = KIND_JOIN_FAILED,
    crash_kit: Optional[str] = None,
    owner_note: Optional[str] = None,
    watched: Any = None,
    now: Optional[datetime] = None,
) -> List[Outgoing]:
    """Сообщения людям (без технического разбора — его собирает `send` по факту доставки)."""
    name = meeting_name(entry)
    when = when_text(entry.get("start_at"), now)
    owner = owner_chat()

    if kind == KIND_JOIN_FAILED and reason in MEETING_PROBLEM_REASONS:
        org = organizer_chat(entry, watched=watched)
        if org is not None and org != owner:
            return [Outgoing(org, AUD_ORGANIZER,
                f"ℹ️ Ватсон не смог зайти на вашу встречу «{name}» ({when}): {reason_human(reason)}. "
                f"Проверьте, пожалуйста, ссылку на встречу в её карточке на портале.")]
        return []

    audience, chat = people_target(entry, watched=watched)
    if audience == AUD_OWNER:
        if kind == KIND_WAITING:
            return [Outgoing(owner, AUD_OWNER, _people_text(kind, AUD_OWNER, name, when, reason))]
        # Протокол этой серии и так уходит владельцу — одно сообщение с разбором.
        return []
    if audience == AUD_OWNER_PORTAL:
        return []  # владельцу — в техническом сообщении, с пометкой «организатору не написал»
    return [Outgoing(chat, audience, _people_text(kind, audience, name, when, reason))]


def build_owner_text(
    entry: Dict[str, Any],
    reason: str,
    *,
    kind: str,
    attempts: int,
    crash_kit: Optional[str],
    owner_note: Optional[str],
    delivered: List[Tuple[str, Optional[int], bool]],
    watched: Any = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Сообщение владельцу — по факту того, что реально ушло людям."""
    if kind == KIND_WAITING:
        return None  # просьба впустить — людям; владельцу отдельно не нужно
    name = meeting_name(entry)
    when = when_text(entry.get("start_at"), now)
    tech = _tech_line(entry, crash_kit)
    audience, _chat = people_target(entry, watched=watched)
    portal = bool(entry.get("order_id"))
    note = f"\n{owner_note[:1].upper()}{owner_note[1:]}." if owner_note else ""

    def where() -> str:
        if not delivered:
            return ""
        parts = []
        for aud, chat, ok in delivered:
            label = {AUD_GROUP: "в чат серии", AUD_GROUP_PORTAL: "в чат встречи (портал)",
                     AUD_ORGANIZER: "организатору в личку"}.get(aud, aud)
            parts.append(label if ok else f"{label} — НЕ ДОШЛО, предупреди людей сам")
        return "\nЛюдям написал: " + "; ".join(parts) + "."

    if kind == KIND_JOIN_FAILED and reason in MEETING_PROBLEM_REASONS:
        if portal:
            told = where() or "\nОрганизатору написать не смог — его Телеграм не привязан к боту; предупреди его, если можешь."
            return (f"ℹ️ Ватсон не зашёл на встречу сотрудников «{name}» ({when}): {reason_human(reason)}. "
                    f"Похоже, дело не в боте, а в ссылке.{told}\n{tech}")
        return (f"ℹ️ Ватсон не зашёл на «{name}» ({when}): {reason_human(reason)}. Попыток: {attempts}.\n"
                f"Похоже, дело не в боте, а во встрече или ссылке — людям не писал.\n{tech}")

    if kind in (KIND_EVICTED, KIND_DEAF):
        what = "его выкинуло из Телемоста, вернуться не получилось" if kind == KIND_EVICTED else "он перестал слышать звук встречи"
        headline = f"Ватсон больше не записывает «{name}» ({when}) — {what}."
    else:
        headline = f"Ватсон не смог подключиться к «{name}» ({when}): {reason_human(reason)}. Попыток: {attempts}."

    if audience == AUD_OWNER:
        return (f"⚠️ {headline}{note}\n\nЕсли встреча идёт — включи запись в интерфейсе Яндекс Телемоста; "
                f"после встречи скажи Альфреду «обработай запись» — протокол соберётся как обычно.\n\n🔧 {tech}")
    if audience == AUD_OWNER_PORTAL:
        # 🔴 Совета «обработай запись» здесь нет намеренно: протокол чужой
        # компании ушёл бы владельцу в личку в обход `delivery_route` (цикл5 У2).
        return (f"⚠️ Встреча сотрудников: {headline}{note}\nОрганизатору написать не смог — его Телеграм "
                f"не привязан к боту. Если можешь — предупреди его: пусть включит запись в Телемосте и "
                f"сохранит её у себя.\n🔧 {tech}")
    return (f"🔧 {headline}{note}{where()}\nЕсли ты на встрече — включи запись сам; после встречи скажи "
            f"Альфреду «обработай запись».\n{tech}")


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

_AUDIENCE_LABEL = {
    AUD_GROUP: "чат серии",
    AUD_GROUP_PORTAL: "чат встречи портала",
    AUD_ORGANIZER: "личка организатора",
    AUD_OWNER: "твоя личка",
    AUD_OWNER_PORTAL: "твоя личка (встреча портала)",
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
    attempts: int = 1,
    kind: str = KIND_JOIN_FAILED,
    crash_kit: Optional[str] = None,
    owner_note: Optional[str] = None,
    watched: Any = None,
    now: Optional[datetime] = None,
    sender: Optional[Callable[[int, str], bool]] = None,
    owner_push: Optional[Callable[[str], bool]] = None,
) -> List[Dict[str, Any]]:
    """Отправить сообщения людям, затем владельцу — по факту доставки. Отчёт без текстов."""
    sender = sender or _default_sender
    redirect = _redirect_to_owner()
    if owner_push is None:
        from notary.lib.notify import push as _push  # noqa: PLC0415

        owner_push = lambda text: _push(text, dedupe=False, silent=redirect)  # noqa: E731
    owner = owner_chat()
    report: List[Dict[str, Any]] = []

    def _send(chat: Optional[int], text: str) -> bool:
        if chat is None:
            return False
        try:
            return bool(sender(chat, text))
        except Exception as e:  # noqa: BLE001 — сеть/Телеграм: покажем владельцу
            logger.warning("[join-alert] отправка упала: %s", type(e).__name__)
            return False

    delivered: List[Tuple[str, Optional[int], bool]] = []
    for m in build_messages(entry, reason, attempts=attempts, kind=kind, crash_kit=crash_kit,
                            owner_note=owner_note, watched=watched, now=now):
        chat, text = m.chat_id, m.text
        if redirect and m.audience in _PEOPLE:
            text = (f"[ТЕСТ — на бою ушло бы: {_AUDIENCE_LABEL.get(m.audience, m.audience)} "
                    f"{m.chat_id}]\n{text}")
            chat = owner
        ok = _send(chat, text)
        if not ok and m.audience in _OWNER:
            ok = bool(owner_push(text))
        if m.audience in _PEOPLE:
            delivered.append((m.audience, m.chat_id, ok))
        report.append({"audience": m.audience, "sent": ok})
        logger.info("[join-alert] %s/%s → %s", kind, m.audience, "отправлено" if ok else "НЕ отправлено")

    owner_text = build_owner_text(entry, reason, kind=kind, attempts=attempts, crash_kit=crash_kit,
                                  owner_note=owner_note, delivered=delivered, watched=watched, now=now)
    if owner_text:
        ok = _send(owner, owner_text) or bool(owner_push(owner_text))
        report.append({"audience": AUD_OWNER_TECH, "sent": ok})
        logger.info("[join-alert] %s/владелец → %s", kind, "отправлено" if ok else "НЕ отправлено")
    return report
