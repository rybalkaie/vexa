"""Слой идентичности Ватсона (Ф10a): «кто это и можно ли ему».

ЧТО БЫЛО. У бота была ровно ОДНА правда о человеке напротив — скаляр
`allowed_chat` (= `TELEGRAM_NOTARIUS_CHAT_ID`, личка владельца). Он же в одиночку
отвечал на ТРИ РАЗНЫХ вопроса, слипшихся в одно сравнение:

  1. «этому ЧАТУ вообще можно?»        → `cid != allowed_chat` → молча выйти
  2. «этот ЧЕЛОВЕК — владелец?»        → `from_id == allowed_chat`
  3. «это личка или ГРУППА серии?»     → `cid == allowed_chat`

Пока пользователь один, все три ответа случайно совпадают, и разница не видна.
Как только наряд приходит от ДРУГОГО сотрудника, они расходятся — а сравнение со
скаляром продолжает отвечать «нет» на все три. Многопользовательности при этом
не бывает: бот физически не умеет сказать «это наряд Петрова».

ЧТО СТАЛО. Вопрос задаётся ОДНОМУ месту — `resolve()`, — и оно возвращает
`Principal`: кто это, какой компании, что ему можно и куда ему в личку. Скаляр
остаётся ОДНИМ ИЗ источников (владелец портала — по-прежнему он), но перестаёт
быть единственным ответом.

🔴 АВАРИЙНЫЙ ВЫКЛЮЧАТЕЛЬ `ENABLE_PORTAL_ORDERS`, ДЕФОЛТ **OFF**. Вся ветка
«наряды портала» рождается за ним — по канону самого Ватсона
(`ENABLE_PROTOCOL_PATCH`, `ENABLE_REPLY_UNDERSTAND`, `ENABLE_MIXED_REPLY_SPLIT`:
тёмный запуск, активация отдельным решением владельца). При OFF каждая функция
этого модуля возвращает РОВНО то, что вернуло бы старое сравнение со скаляром, —
и возвращает его ДО единого обращения к диску. Это не «стараемся не сломать», это
структурная гарантия: ветки, которая могла бы вести себя иначе, при OFF просто не
исполняется. Сторожа — `tests/test_identity_ph10a.py`: `TestLegacyEquivalence`
сверяет КАЖДУЮ функцию с эталонным старым выражением на полной матрице входов, а
`TestNoDiskWhenOff` считает обращения к реестру и требует ровно ноль.

Почему это важнее обычного: Ватсон — сервис ЕЖЕДНЕВНОГО пользования владельца.
Регресс здесь замечает он на своей встрече, а не приёмка.

🔴 ПУТИ РЕЗОЛВЯТСЯ ЛЕНИВО (через `lib/orders.orders_file()`), никакого пути на
импорте. Ровно этот класс дефекта (`lib/state.py`, приёмка 05.08.2026) заставлял
пробы писать в БОЕВОЙ `.state.json`. Сторожа — `test_state_path_isolation.py`,
`test_gateway_state_isolation.py`.

R9 («опасная тройка»): тексты реплик, протоколов и имена участников этот модуль
не читает и не логирует — только идентификаторы и компанию.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("notary.identity")

ENABLE_ENV = "ENABLE_PORTAL_ORDERS"

# Канон флагов Ватсона: ON только от явного слова-«да». Всё остальное — OFF, и
# это НЕ придирка: `ENABLE_PORTAL_ORDERS=false` в файле окружения обязан значить
# «выключено», а не «строка непустая, значит включено» (классический способ
# включить тёмную фичу на бою опечаткой).
_TRUTHY = ("1", "true", "yes", "on")

KIND_OWNER = "owner"
"""Владелец портала (Илья): его личка ИЛИ он сам в групповом чате серии."""

KIND_PORTAL = "portal"
"""Сотрудник, чей наряд ведёт бот. Появляется ТОЛЬКО при `ENABLE_PORTAL_ORDERS=ON`."""

KIND_STRANGER = "stranger"
"""Бот этого чата/человека не знает — законный исход, а не ошибка."""


@dataclass(frozen=True)
class Principal:
    """Ответ на вопрос «кто это и можно ли ему». Неизменяемый — это снимок факта.

    `dm_chat_id` — личка ЭТОГО лица (у владельца = `allowed_chat`, у сотрудника =
    его `telegram_user_id` из наряда). Без него «шли в личку» означало бы личку
    Ильи для любого сотрудника — то есть чужой протокол в чужой личке.
    """

    kind: str
    chat_id: Optional[int]
    user_id: Optional[int] = None
    company: Optional[str] = None
    order_id: Optional[str] = None
    portal_user_id: Optional[str] = None
    dm_chat_id: Optional[int] = None

    # ---- три вопроса, которые раньше задавались одному скаляру ----

    @property
    def may_command(self) -> bool:
        """Вопрос 2: этот человек вправе командовать сериями здесь?

        Эталон (было): `cid == allowed_chat or from_id == allowed_chat`.
        """
        return self.kind in (KIND_OWNER, KIND_PORTAL)

    @property
    def is_direct(self) -> bool:
        """Вопрос 3: это ЛИЧКА доверенного лица (а не групповой чат серии)?

        Эталон (было): `cid == allowed_chat`. Владелец, пишущий В ГРУППЕ, сюда НЕ
        попадает — вопрос про ЧАТ, не про человека, и путать эти два уже стоило
        проекту разбирательств.
        """
        return self.kind in (KIND_OWNER, KIND_PORTAL) and self.chat_id == self.dm_chat_id

    @property
    def chat_is_serviced(self) -> bool:
        """Вопрос 1: DM-поток команд обслуживает этот чат?

        Эталон (было): `cid == allowed_chat`. Сегодня совпадает с `is_direct`;
        разными они станут, когда у сотрудника появится свой групповой чат.
        """
        return self.is_direct

    @property
    def is_owner(self) -> bool:
        """Именно владелец портала (Илья), а не «кто-то доверенный»."""
        return self.kind == KIND_OWNER


def is_enabled() -> bool:
    """Гейт `ENABLE_PORTAL_ORDERS` — ДЕФОЛТ OFF (тёмный запуск).

    Не задан вовсе / мусор / `false` / `0` / `no` / пустая строка → OFF.
    """
    raw = (os.environ.get(ENABLE_ENV) or "").strip().lower()
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Владелец наряда как ДАННЫЕ
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderOwner:
    """Чей наряд. `portal_user_id` — обязателен, остальное по наличию."""

    portal_user_id: str
    company: str
    telegram_user_id: Optional[int] = None
    name: Optional[str] = None


def order_owner(order: Any) -> Optional[OrderOwner]:
    """Владелец наряда или None, если наряд «ничей».

    None — законный, а не аварийный исход, и вот почему НЕ бросаем:

    * **Обратная совместимость.** В `.orders.json` на диске уже лежат наряды,
      поставленные Ф9, где `owner` необязателен. Падать на них означало бы, что
      выкатка Ф10a роняет живой реестр — и бот перестаёт видеть УЖЕ ПОСТАВЛЕННЫЕ
      встречи. Старый наряд читается как обычно, просто личности не даёт.
    * **Fail-closed.** «Ничей» наряд не наделяет ПРАВАМИ никого. Обратное
      («ничей = владельца») отдало бы права по чужому наряду первому встречному.

    Запрет на НОВЫЕ безвладельческие наряды стоит на входе — в валидаторе шлюза
    (`orders.validate_order_payload`, при флаге ON поле обязательно). Здесь —
    чтение уже лежащего.
    """
    if not isinstance(order, dict):
        return None
    raw = order.get("owner")
    if not isinstance(raw, dict):
        return None
    uid = raw.get("portal_user_id")
    if not isinstance(uid, str) or not uid.strip():
        # Владелец без идентификатора — не владелец: по имени «Илья» наряд не
        # свяжешь ни с сотрудником портала, ни с телеграм-аккаунтом.
        return None
    company = order.get("company")
    if not isinstance(company, str) or not company.strip():
        return None
    company = company.strip().lower()
    if company not in valid_companies():
        # Неизвестная компания = наряд не маршрутизируем: от компании зависит, в
        # какой чат уйдёт протокол. Угадывать «наверное anzhee» здесь нельзя.
        logger.warning(
            "[identity] наряд %s с неизвестной компанией %r — личности не даёт",
            order.get("id"), company,
        )
        return None
    tg = raw.get("telegram_user_id")
    if isinstance(tg, bool) or not isinstance(tg, int):
        tg = None
    name = raw.get("name")
    return OrderOwner(
        portal_user_id=uid.strip(),
        company=company,
        telegram_user_id=tg,
        name=name if isinstance(name, str) and name.strip() else None,
    )


def valid_companies() -> frozenset:
    """Канон компаний бота. Импорт ленивый: `cli.registry` тянет sys.path-магию."""
    try:
        from notary.cli.registry import VALID_COMPANIES  # noqa: PLC0415

        return frozenset(VALID_COMPANIES)
    except Exception:  # noqa: BLE001 — реестр недоступен → компании неизвестны
        return frozenset()


# ---------------------------------------------------------------------------
# Чтение реестра нарядов (только при ON; при OFF сюда не доходит ни один вызов)
# ---------------------------------------------------------------------------

# Реестр читается на КАЖДОЕ сообщение Telegram, а машина двухъядерная. Memo по
# (путь, mtime_ns, размер) снимает лишние чтения, не пряча правок: писатель —
# `orders._mutate` — делает `os.replace`, то есть mtime меняется всегда.
# Ключ включает ПУТЬ, потому что путь резолвится ЛЕНИВО: проба, подменившая
# `MEETING_NOTARY_REGISTRY_DIR`, обязана увидеть свой реестр, а не чужой кэш.
_cache: Dict[str, Any] = {"key": None, "orders": []}


def reset_cache() -> None:
    """Сбросить memo. Пробам — в setUp; бою не нужен (mtime справляется сам)."""
    _cache["key"] = None
    _cache["orders"] = []


def _active_orders() -> List[Dict[str, Any]]:
    """Живые наряды из реестра. Любой сбой → пустой список, бот работает как без нарядов."""
    try:
        from notary.lib import orders as orders_lib  # noqa: PLC0415

        path = orders_lib.orders_file()
        try:
            st = path.stat()
            key = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            key = (str(path), None, None)
        if _cache["key"] == key:
            return _cache["orders"]
        found = orders_lib.list_orders(statuses=orders_lib.ACTIVE_STATUSES)
        _cache["key"] = key
        _cache["orders"] = found
        return found
    except Exception as e:  # noqa: BLE001 — реестр не читается → бот не падает
        # WARNING, не debug: нечитаемый реестр означает, что порталовые наряды
        # молча не исполняются, и без строки в журнале это выглядит как «бот
        # проигнорировал встречу».
        logger.warning("[identity] реестр нарядов не прочитан: %s", type(e).__name__)
        return []


def _owner_by_telegram_id(user_id: Optional[int]) -> Optional[Tuple[OrderOwner, Dict[str, Any]]]:
    """Первый живой наряд, чей владелец = этот телеграм-аккаунт."""
    if user_id is None or isinstance(user_id, bool):
        return None
    for order in _active_orders():
        owner = order_owner(order)
        if owner is not None and owner.telegram_user_id == user_id:
            return owner, order
    return None


def _order_for_chat(chat_id: Optional[int]) -> Optional[Tuple[OrderOwner, Dict[str, Any]]]:
    """Первый живой наряд, чей чат доставки = этот чат."""
    if chat_id is None or isinstance(chat_id, bool):
        return None
    for order in _active_orders():
        if order.get("telegram_chat_id") != chat_id:
            continue
        owner = order_owner(order)
        if owner is not None:
            return owner, order
    return None


# ---------------------------------------------------------------------------
# Главная дверь
# ---------------------------------------------------------------------------


def resolve(
    chat_id: Optional[int], user_id: Optional[int], *, allowed_chat: Optional[int]
) -> Principal:
    """Кто это и можно ли ему. ЕДИНСТВЕННАЯ дверь — не сравнивать со скаляром мимо неё.

    При `ENABLE_PORTAL_ORDERS=OFF` возвращает ровно то, что дало бы старое
    сравнение: владелец (личка или он же в группе) либо «не знаю». Диск при этом
    не трогается вовсе.
    """
    owner_principal = _owner_principal(chat_id, user_id, allowed_chat)
    if owner_principal is not None:
        return owner_principal

    if not is_enabled():
        # 🔴 Единственная точка, где ветка нарядов могла бы включиться. Ниже —
        # код, которого при OFF на бою не существует.
        return Principal(kind=KIND_STRANGER, chat_id=chat_id, user_id=user_id)

    # Личность сотрудника: сначала по человеку (он пишет откуда угодно), потом по
    # чату доставки наряда (в групповом чате встречи он может писать не сам).
    found = _owner_by_telegram_id(user_id)
    if found is None:
        found = _order_for_chat(chat_id)
    if found is None:
        return Principal(kind=KIND_STRANGER, chat_id=chat_id, user_id=user_id)
    owner, order = found
    return Principal(
        kind=KIND_PORTAL,
        chat_id=chat_id,
        user_id=user_id,
        company=owner.company,
        order_id=order.get("id"),
        portal_user_id=owner.portal_user_id,
        dm_chat_id=owner.telegram_user_id,
    )


def _owner_principal(
    chat_id: Optional[int], user_id: Optional[int], allowed_chat: Optional[int]
) -> Optional[Principal]:
    """Владелец портала. Работает одинаково при любом положении флага.

    Сравнение — ГОЛЫЙ `==`, буква в букву как в старом коде, включая угол
    «`allowed_chat` не задан И в сообщении нет чата» (`None == None` → владелец).
    Соблазн «заодно закрыть» его здесь отвергнут сознательно: цена этой фазы —
    доказуемая неизменность поведения владельца, а не попутные улучшения. Дыра
    описана в отчёте фазы как находка за её пределами; на бою `allowed_chat`
    всегда int (`meetings_listener.main` валидирует и выходит с кодом 2).
    """
    is_dm = chat_id == allowed_chat
    is_him = user_id == allowed_chat
    if not (is_dm or is_him):
        return None
    return Principal(
        kind=KIND_OWNER,
        chat_id=chat_id,
        user_id=user_id,
        order_id=None,
        dm_chat_id=allowed_chat,
    )


# ---------------------------------------------------------------------------
# Удобные обёртки под конкретные call-site'ы (чтобы не плодить трактовок)
# ---------------------------------------------------------------------------


def is_direct_chat(chat_id: Optional[int], allowed_chat: Optional[int]) -> bool:
    """«Это личка доверенного лица?» — эталон: `chat_id == allowed_chat`.

    Отдельная функция без `user_id`, потому что вопрос про ЧАТ: шлюз правок
    (`feedback_worker`) знает чат, но не обязан знать, кто написал.
    """
    return resolve(chat_id, None, allowed_chat=allowed_chat).is_direct


def may_command(
    chat_id: Optional[int], user_id: Optional[int], allowed_chat: Optional[int]
) -> bool:
    """«Этому можно командовать сериями?» — эталон: `is_dm or from_id == allowed_chat`."""
    return resolve(chat_id, user_id, allowed_chat=allowed_chat).may_command


def dm_target(
    chat_id: Optional[int], user_id: Optional[int], allowed_chat: Optional[int]
) -> Optional[int]:
    """Куда значит «в личку» ДЛЯ ЭТОГО человека — эталон: `allowed_chat`.

    У сотрудника это его личка, а не Ильи: иначе «шли в личку» отправляло бы
    чужой протокол владельцу портала.
    """
    principal = resolve(chat_id, user_id, allowed_chat=allowed_chat)
    return principal.dm_chat_id if principal.dm_chat_id is not None else allowed_chat


# ---------------------------------------------------------------------------
# Изоляция компаний (R12.12 на стороне бота)
# ---------------------------------------------------------------------------


def may_touch_order(principal: Principal, order: Any) -> bool:
    """Вправе ли это лицо трогать ЭТОТ наряд.

    Владелец портала — да (он администратор обеих компаний). Сотрудник — только
    свою компанию: наряд чужой компании для него не существует, как встреча чужой
    компании на экране портала (R12.12).
    """
    if principal.kind == KIND_OWNER:
        return True
    if principal.kind != KIND_PORTAL:
        return False
    owner = order_owner(order)
    if owner is None:
        return False  # ничей наряд правами не наделяет никого
    return owner.company == principal.company


def order_company_conflict(order: Any, watched: Any) -> Optional[str]:
    """Компания наряда против компании серии в `watched.yaml`. None — конфликта нет.

    Ловит «наряд владельца одной компании с комнатой другой»: серия уже размечена
    компанией, а наряд приехал с другой. Молча принять — значит увести протокол
    Anzhee в чат МПервого, то есть ровно та утечка, которую запрещает R12.12.
    Серия без разметки конфликтом НЕ считается (переходный резолв по оргструктуре
    ещё жив) — иначе половина живых серий стала бы «конфликтом».
    """
    if not isinstance(order, dict):
        return None
    company = order.get("company")
    series = order.get("series")
    if not isinstance(company, str) or not isinstance(series, str) or not series.strip():
        return None
    try:
        from notary.cli.registry import get_company_for_series  # noqa: PLC0415

        series_company = get_company_for_series(series.strip(), watched or {})
    except Exception:  # noqa: BLE001 — реестр недоступен → конфликта не утверждаем
        return None
    if not series_company:
        return None
    if series_company == company.strip().lower():
        return None
    return series_company
