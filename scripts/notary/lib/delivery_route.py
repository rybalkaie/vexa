"""Ф10d: адресная доставка — «кому уходит результат ЭТОЙ встречи».

ЧТО БЫЛО. Адресат был ОДИН и выбирался в самом низу цепочки, уже внутри
доставки: `deliver_protocol` смотрел привязку серии в `watched.yaml`, а не нашёл
— молча падал в `TELEGRAM_NOTARIUS_CHAT_ID`, то есть в личку Ильи
(`llm_postprocess.py`, шаг 2). Пока пользователь один, это правильный ответ.
Как только наряд приходит от ДРУГОГО сотрудника, тот же фолбэк означает буквально
«чужой протокол уехал владельцу портала»: наряд несёт и компанию, и владельца, но
до доставки не доезжало ни одно из этих полей (`orders.telegram_chat_id`
читался ровно дважды — в `runner` для конфига бота и в `identity` для опознания
человека; в `deliver_protocol` — ни разу).

ЧТО СТАЛО. Вопрос «куда» задаётся ОДНОМУ месту — `route_for_meeting()`, — и оно
отвечает `Route`: режим, чат протокола, чат уточнений, компания. Наряда нет
(личная серия владельца) → `MODE_OWNER`, и вызывающий делает ровно то, что делал
вчера: этот модуль в такой ветке не решает НИЧЕГО.

🔴 ГЛАВНОЕ ПРАВИЛО: ДЛЯ НАРЯДА ФОЛБЭКА В ЛИЧКУ ВЛАДЕЛЬЦА ПОРТАЛА НЕТ. Не вывели
адресата — результат уходит ТОЛЬКО в портал (`MODE_PORTAL_ONLY`), а не «ну хоть
куда-нибудь». Протокол на портале виден по правам, в чужой личке — нет. Молчание
здесь дешевле утечки, и это единственная причина, по которой fail-closed выбран
вместо привычного «отправить хоть кому-то».

🔴 КОМПАНИЯ НЕ ТЕРЯЕТСЯ. Бот один на обе компании (решение владельца), поэтому
единственная защита от «протокол МПервый ушёл в чат Anzhee» — поле компании,
которое едет с нарядом до самого конца. Здесь оно (а) обязательно: наряд без
компании адресата не получает вовсе; (б) сверяется с разметкой серии — чат серии
чужой компании адресатом НЕ становится (`identity.order_company_conflict`).

🔴 УТОЧНЕНИЯ ПО СПИКЕРАМ — ТОЛЬКО В ЛИЧКУ. Вопрос «кто говорил во фрагменте…»
несёт кусок расшифровки. Сегодня он уходит в один жёстко заданный чат из
`TELEGRAM_NOTARIUS_CHAT_ID` (`llm_postprocess.clarify_speakers_via_telegram`), и
для порталовой встречи это чужой человек. Правило: уточнение идёт в личку
владельца наряда, и НИКОГДА в групповой чат — даже если протокол туда уходит
(R13.2: расшифровка не выносится участникам). Лички нет → уточнение не задаётся.

R9 («опасная тройка»): модуль не читает и не логирует ни реплик, ни протокола, ни
имён участников — только идентификаторы, компанию и код причины.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("notary.delivery_route")

#: Наряда нет — личная серия владельца. Вызывающий действует ровно как до Ф10d.
MODE_OWNER = "owner"
#: Личка сотрудника, чей это наряд.
MODE_DM = "dm"
#: Групповой чат серии (из наряда или из привязки в `watched.yaml`).
MODE_GROUP = "group"
#: В Telegram не уходит ничего — результат живёт только на портале.
MODE_PORTAL_ONLY = "portal_only"
#: 🔴 НЕ ВЫЯСНИЛИ, ЧЬЯ ЭТО ВСТРЕЧА (реестр нарядов не прочитался). Не «наряда
#: нет»: адресата не знаем вовсе, поэтому не уходит НИКУДА — ни в Telegram, ни в
#: портал (компанию мы тоже не знаем, а портал раскладывает результат по ней).
#: Отдельный режим нужен именно потому, что «нет наряда» и «не смогли прочитать»
#: раньше склеивались в один ответ, и второй исход означал чужой протокол в личке
#: владельца портала.
MODE_BLOCKED = "blocked"

#: Что портал вправе попросить полем `delivery` наряда.
REQUESTABLE_MODES = (MODE_DM, MODE_GROUP, MODE_PORTAL_ONLY)

# Коды причин. Машинные: уезжают порталу в `telegram_delivery.reason` и в журнал,
# человеку их показывает портал своими словами.
REASON_NO_ORDER = "no_order"
REASON_FLAG_OFF = "flag_off"
REASON_REQUESTED = "requested"
REASON_AUTO = "auto"
REASON_NO_COMPANY = "no_company"
REASON_DM_UNKNOWN = "dm_unknown"
REASON_GROUP_UNKNOWN = "group_unknown"
REASON_NO_TARGET = "no_target"
REASON_COMPANY_MISMATCH = "company_mismatch"
REASON_REGISTRY_ERROR = "registry_error"


class RegistryUnavailable(RuntimeError):
    """Реестр нарядов не прочитался — «чей это результат» НЕ ВЫЯСНЕНО.

    Отличается от «наряда нет» (`None`) намеренно: молча свести их к одному
    ответу означает отдать протокол сотрудника чужой компании в личку владельца
    портала, потому что «наряда нет» = личная встреча владельца. Ровно этот
    инцидент фаза и закрывает, а окно для него на VPS реальное: `orders._mutate`
    пишет реестр через `os.replace` под `flock`, читатель идёт без замка, плюс
    права файла уже один раз ломались (инцидент 12.08.2026).
    """


def is_enabled() -> bool:
    """Гейт `ENABLE_PORTAL_ORDERS` — тот же выключатель, что у Ф10a.

    Отдельного тумблера у маршрутизации нет by design: она и существует только
    ради нарядов портала, а те целиком живут за этим флагом.
    """
    try:
        from notary.lib import identity  # noqa: PLC0415

        return identity.is_enabled()
    except Exception:  # noqa: BLE001 — модуль не доехал → считаем OFF (вчерашнее)
        return False


@dataclass(frozen=True)
class Route:
    """Куда уходит результат встречи. Неизменяемый — это снимок решения."""

    mode: str
    chat_id: Optional[int] = None
    clarify_chat_id: Optional[int] = None
    company: Optional[str] = None
    order_id: Optional[str] = None
    owner_user_id: Optional[int] = None
    portal_user_id: Optional[str] = None
    reason: str = REASON_NO_ORDER

    @property
    def is_owner_legacy(self) -> bool:
        """Наряда нет → вызывающий обязан вести себя ровно как до Ф10d."""
        return self.mode == MODE_OWNER

    @property
    def portal_only(self) -> bool:
        return self.mode == MODE_PORTAL_ONLY

    @property
    def blocked(self) -> bool:
        """Чья встреча — не выяснено. Доставлять нельзя НИКУДА, это не «нет наряда»."""
        return self.mode == MODE_BLOCKED

    @property
    def telegram_status(self) -> str:
        """Что сказать порталу в `telegram_delivery.status`, если не отправляли."""
        return "skipped" if self.portal_only else "unknown"

    def as_dict(self) -> Dict[str, Any]:
        """Для панели «Доставка» портала и для журнала. Без текстов и имён."""
        return {
            "mode": self.mode,
            "chat_bound": self.chat_id is not None,
            "company": self.company,
            "order_id": self.order_id,
            "reason": self.reason,
        }


OWNER_ROUTE = Route(mode=MODE_OWNER, reason=REASON_NO_ORDER)
#: Реестр не прочитался. Ни чата, ни компании — доставки нет вовсе.
BLOCKED_ROUTE = Route(mode=MODE_BLOCKED, reason=REASON_REGISTRY_ERROR)


# ---------------------------------------------------------------------------
# Что попросил портал
# ---------------------------------------------------------------------------


def requested_mode(order: Any) -> Optional[str]:
    """Значение поля `delivery` наряда или None (портал не просил ничего).

    Мусор в поле трактуется как «не просил»: валидатор шлюза такой наряд не
    принимает (`orders.validate_order_payload`), а уже лежащий на диске наряд
    ломать чтением нельзя — он старше этой фазы.
    """
    if not isinstance(order, dict):
        return None
    raw = order.get("delivery")
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value if value in REQUESTABLE_MODES else None


# ---------------------------------------------------------------------------
# Кандидаты в адресаты
# ---------------------------------------------------------------------------


def _dm_target(order: Any) -> Optional[int]:
    """Личка владельца наряда. Без `telegram_user_id` её не существует."""
    try:
        from notary.lib import identity  # noqa: PLC0415

        owner = identity.order_owner(order)
    except Exception:  # noqa: BLE001 — модуль не доехал → лички не знаем
        return None
    if owner is None:
        return None
    tg = owner.telegram_user_id
    return tg if isinstance(tg, int) and not isinstance(tg, bool) else None


def _series_chat(order: Dict[str, Any], watched: Any) -> Optional[int]:
    """Чат серии из `watched.yaml` — ТОЛЬКО если серия не чужой компании.

    Разметка серии старше наряда: если серия помечена `mpfirst`, а наряд пришёл
    с `anzhee`, её групповой чат адресатом не становится ни при каких условиях.
    Шлюз такой наряд отбивает на входе (422 `company_mismatch`), но реестр правят
    и руками, а разметку могли поменять уже после постановки наряда.
    """
    series = order.get("series")
    if not isinstance(series, str) or not series.strip():
        return None
    try:
        from notary.cli import registry  # noqa: PLC0415
        from notary.lib import identity  # noqa: PLC0415

        data = watched if isinstance(watched, dict) else registry.load_watched()
        conflict = identity.order_company_conflict(order, data)
        if conflict:
            logger.warning(
                "[route] наряд %s (%s) против серии %r компании %s — групповой чат "
                "серии адресатом НЕ становится",
                order.get("id"), order.get("company"), series.strip(), conflict,
            )
            return None
        chat = registry.get_telegram_chat_id_for_series(series.strip(), data)
    except Exception as e:  # noqa: BLE001 — реестр не читается → привязки нет
        logger.warning("[route] привязка серии не прочитана (%s)", type(e).__name__)
        return None
    return chat if isinstance(chat, int) and not isinstance(chat, bool) else None


def _group_target(order: Dict[str, Any], watched: Any) -> Optional[int]:
    """Групповой чат: явный из наряда, иначе привязка серии той же компании."""
    explicit = order.get("telegram_chat_id")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    return _series_chat(order, watched)


# ---------------------------------------------------------------------------
# Главная дверь
# ---------------------------------------------------------------------------


def resolve(order: Any, *, watched: Any = None) -> Route:
    """Маршрут для наряда. `order is None` → `MODE_OWNER` (сегодняшнее поведение)."""
    if not isinstance(order, dict) or not order:
        return OWNER_ROUTE

    order_id = order.get("id")
    company = order.get("company")
    company = company.strip().lower() if isinstance(company, str) and company.strip() else None
    dm = _dm_target(order)
    portal_user_id = None
    try:
        from notary.lib import identity  # noqa: PLC0415

        owner = identity.order_owner(order)
        portal_user_id = owner.portal_user_id if owner is not None else None
    except Exception:  # noqa: BLE001
        portal_user_id = None

    def _portal_only(reason: str) -> Route:
        return Route(
            mode=MODE_PORTAL_ONLY,
            chat_id=None,
            # Уточнение — вопрос ЧЕЛОВЕКУ, а не «в чат встречи». Даже когда
            # протокол в Telegram не уходит, спросить владельца наряда в личке
            # можно и нужно: без ответа в протоколе останется «Говорящий 2».
            clarify_chat_id=dm,
            company=company,
            order_id=order_id,
            owner_user_id=dm,
            portal_user_id=portal_user_id,
            reason=reason,
        )

    # 🔴 Компания — не украшение, а условие маршрутизации. Наряд без неё
    # адресата не получает: «наверное, Anzhee» здесь стоит ровно одну утечку.
    if not company:
        logger.warning("[route] наряд %s без компании — только портал", order_id)
        return _portal_only(REASON_NO_COMPANY)

    requested = requested_mode(order)
    group = _group_target(order, watched)

    if requested == MODE_PORTAL_ONLY:
        return _portal_only(REASON_REQUESTED)

    if requested == MODE_DM:
        if dm is None:
            logger.warning(
                "[route] наряд %s просит личку, но телеграм сотрудника не привязан "
                "— только портал", order_id,
            )
            return _portal_only(REASON_DM_UNKNOWN)
        return Route(
            mode=MODE_DM, chat_id=dm, clarify_chat_id=dm, company=company,
            order_id=order_id, owner_user_id=dm, portal_user_id=portal_user_id,
            reason=REASON_REQUESTED,
        )

    if requested == MODE_GROUP:
        if group is None:
            logger.warning(
                "[route] наряд %s просит группу, но чат не привязан — только портал",
                order_id,
            )
            return _portal_only(REASON_GROUP_UNKNOWN)
        return Route(
            mode=MODE_GROUP, chat_id=group, clarify_chat_id=dm, company=company,
            order_id=order_id, owner_user_id=dm, portal_user_id=portal_user_id,
            reason=REASON_REQUESTED,
        )

    # Портал не просил — выбираем сами. Группа важнее лички: встречу видели все,
    # кто в чате серии, и протокол там ждут. Лички нет и группы нет — портал.
    if group is not None:
        return Route(
            mode=MODE_GROUP, chat_id=group, clarify_chat_id=dm, company=company,
            order_id=order_id, owner_user_id=dm, portal_user_id=portal_user_id,
            reason=REASON_AUTO,
        )
    if dm is not None:
        return Route(
            mode=MODE_DM, chat_id=dm, clarify_chat_id=dm, company=company,
            order_id=order_id, owner_user_id=dm, portal_user_id=portal_user_id,
            reason=REASON_AUTO,
        )
    logger.info(
        "[route] наряд %s: ни группы, ни лички — результат уходит только на портал",
        order_id,
    )
    return _portal_only(REASON_NO_TARGET)


def order_for_meeting(
    meeting_meta: Any, *, session_uid: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Наряд, которому принадлежит эта запись. None — личная серия владельца.

    Тот же двухшаговый поиск, что у `collector._portal_order_for` (по отметке
    запуска, иначе по комнате и времени) — и та же трактовка None: это ОБЫЧНЫЙ
    исход, а не сбой. При выключателе OFF диск не трогается вовсе.

    🔴 Сбой чтения реестра — НЕ `None`, а `RegistryUnavailable`. «Наряда нет»
    означает «личная встреча владельца, шли ему», и подменять этим ответом
    «не смогли выяснить» нельзя: цена ошибки — чужой протокол в чужой личке.
    """
    if not isinstance(meeting_meta, dict):
        return None
    # Штамп в meta — тот же источник истины, что для `route_for_meeting`: наряд
    # уже выяснен один раз в начале обработки. Идти за ним в реестр повторно —
    # значит зависеть от того, что комната и время всё ещё сходятся, а у старой
    # встречи (правка через месяц) они не сходятся: наряд из реестра уже убран
    # или сдвинут. Именно из-за этого перевыпуск порталовой встречи не находил
    # свой наряд и версия не ставилась в очередь вовсе.
    stamped = meeting_meta.get("order")
    if isinstance(stamped, dict) and stamped:
        return stamped
    try:
        from notary.lib import identity  # noqa: PLC0415

        if not identity.is_enabled():
            return None
        from notary.lib import orders as orders_lib  # noqa: PLC0415

        return orders_lib.find_order_for_recording(
            room_url=meeting_meta.get("meetingUrl"),
            started_at=meeting_meta.get("startTs"),
            session_uid=session_uid or meeting_meta.get("sessionUid"),
        )
    except Exception as e:  # noqa: BLE001 — любой сбой чтения = «не выяснили»
        logger.error(
            "[route] 🔴 РЕЕСТР НАРЯДОВ НЕ ПРОЧИТАН (%s) — чья это встреча, "
            "НЕ ВЫЯСНЕНО. Доставка остановлена: отправить «раз наряда нет, значит "
            "владельцу» здесь означало бы чужой протокол в чужой личке",
            type(e).__name__,
        )
        raise RegistryUnavailable(type(e).__name__) from e


def route_for_meeting(
    meeting_meta: Any,
    *,
    session_uid: Optional[str] = None,
    watched: Any = None,
) -> Route:
    """Маршрут по метаданным встречи. Наряда нет → `MODE_OWNER`.

    Метаданные могут уже НЕСТИ маршрут: `finalize` штампует `order` в meta один
    раз в начале обработки, чтобы весь конвейер (сборка, доставка, уточнения)
    отвечал на вопрос «чья это встреча» одинаково и не перерешал его четырежды.
    """
    if not is_enabled():
        # ───────────────────────────────────────────────────────────────────
        # 🔴 ВЫКЛЮЧАТЕЛЬ ВОЗВРАЩАЕТ ВЧЕРАШНЕЕ ПОВЕДЕНИЕ — НО ТОЛЬКО ТАМ, ГДЕ
        # ВЧЕРАШНЕЕ ПОВЕДЕНИЕ СУЩЕСТВУЕТ (ремонт ШОВ-С9).
        #
        # ЧТО ЛОМАЛОСЬ. Здесь стоял безусловный `OWNER_ROUTE`, и он применялся
        # в том числе к УЖЕ ОТШТАМПОВАННОМУ порталовому наряду. `MODE_OWNER`
        # означает «личная серия владельца бота» — то есть вызывающий шлёт
        # результат в `TELEGRAM_NOTARIUS_CHAT_ID`, личный чат владельца. Итог:
        # аварийный выключатель уводил протоколы ЧУЖИХ КОМПАНИЙ в личку одного
        # человека — ровно та утечка, ради запрета которой этот модуль и
        # заведён («для наряда фолбэка в личку владельца портала НЕТ»).
        #
        # У порталового наряда вчерашнего поведения не существует: вчера его
        # не было вовсе. Значит деградировать он обязан в «только портал» —
        # молчание в Telegram, результат виден на портале по правам.
        #
        # Личные серии владельца этим не задеты: у них штампа нет, ветка ниже
        # отдаёт им прежний `OWNER_ROUTE` целиком.
        # ───────────────────────────────────────────────────────────────────
        if isinstance(meeting_meta, dict):
            stamped = meeting_meta.get("order")
            if isinstance(stamped, dict) and stamped:
                company = stamped.get("company")
                logger.warning(
                    "[route] выключатель OFF при отштампованном наряде %s — "
                    "результат уходит ТОЛЬКО на портал (в личку владельца бота "
                    "чужой протокол не уезжает)",
                    stamped.get("id"),
                )
                return Route(
                    mode=MODE_PORTAL_ONLY,
                    chat_id=None,
                    # Уточнения тоже не задаются: выключатель — аварийный, и
                    # писать людям в Telegram при нём мы не вправе.
                    clarify_chat_id=None,
                    company=(
                        company.strip().lower()
                        if isinstance(company, str) and company.strip()
                        else None
                    ),
                    order_id=stamped.get("id"),
                    reason=REASON_FLAG_OFF,
                )
        return OWNER_ROUTE
    if isinstance(meeting_meta, dict):
        stamped = meeting_meta.get("order")
        if isinstance(stamped, dict) and stamped:
            return resolve(stamped, watched=watched)
    try:
        order = order_for_meeting(meeting_meta, session_uid=session_uid)
    except RegistryUnavailable:
        # 🔴 Fail-closed. Реестр не прочитан → адресат не выведен → результат не
        # уходит никуда, а вызывающий обязан НЕ считать это законченной
        # доставкой (статус не терминальный, следующий проход повторит).
        return BLOCKED_ROUTE
    if order is None:
        return OWNER_ROUTE
    return resolve(order, watched=watched)


def stamp_meta(meeting_meta: Any, *, session_uid: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Вписать наряд и компанию в meta встречи ОДИН раз, в начале обработки.

    Зачем штамп, а не резолв по месту: компанию сегодня переугадывают по slug'у
    серии в четырёх независимых местах (`llm_postprocess.generate_protocol`,
    критик, кросс-фон, `finalize`), причём ДВУМЯ разными резолверами. Для
    порталовой встречи это означает, что знание чужой компании может попасть в
    сборку, хотя правильный ответ приехал в наряде. Штамп делает наряд
    единственным источником: `meta["company"]` перебивает угадывание.

    Возвращает наряд (или None). Meta правится на месте.
    """
    if not isinstance(meeting_meta, dict):
        return None
    order = order_for_meeting(meeting_meta, session_uid=session_uid)
    if order is None:
        return None
    meeting_meta["order"] = order
    company = order.get("company")
    if isinstance(company, str) and company.strip():
        meeting_meta["company"] = company.strip().lower()
    return order


# ---------------------------------------------------------------------------
# Адресат уточнений по спикерам
# ---------------------------------------------------------------------------


def owner_chat_env() -> Optional[int]:
    """`TELEGRAM_NOTARIUS_CHAT_ID` (личка владельца портала). Не задан → None."""
    raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def clarify_chat_for_meeting(
    meeting_meta: Any, *, session_uid: Optional[str] = None
) -> Optional[int]:
    """Куда спрашивать «кто говорил» по ЭТОЙ встрече.

    * Личная серия владельца → `TELEGRAM_NOTARIUS_CHAT_ID`, буква в букву как до
      Ф10d (сегодня это единственный живой случай).
    * Наряд портала → личка владельца наряда. Не привязана → None: вопрос не
      задаётся вовсе. Спросить «в общий чат» нельзя (кусок расшифровки), спросить
      Илью — тоже (чужая встреча, и ответить он всё равно не сможет).
    * Реестр не прочитался (`MODE_BLOCKED`) → None по той же причине: кому
      принадлежит кусок расшифровки, мы не знаем, а «спросим владельца портала»
      здесь означало бы выдать ему фрагмент чужого разговора.
    """
    route = route_for_meeting(meeting_meta, session_uid=session_uid)
    if route.is_owner_legacy:
        return owner_chat_env()
    return route.clarify_chat_id
