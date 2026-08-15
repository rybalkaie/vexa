"""Наряды портала → элементы очереди планировщика (Ф10b).

ЗАЧЕМ. Ф9 научила портал КЛАСТЬ наряд в реестр (`lib/orders.py`), Ф10a — считать
владельца наряда данными (`lib/identity.py`). Исполнения не было ни там, ни там:
бот про наряды не знал. Этот модуль — мост между реестром нарядов и очередью
стартов, и он единственный: **второго писателя очереди в проекте быть не должно**
(наряды раскрывает планировщик, как и личные серии владельца).

🔴 ВСЁ ЖИВЁТ ЗА `ENABLE_PORTAL_ORDERS` (дефолт OFF, канон Ватсона). При OFF
`items_for_queue` возвращает пустой список ДО единого обращения к диску — как в
Ф10a, структурной гарантией, а не обещанием «мы аккуратно правили».

🔴 КЛЮЧ ИДЕМПОТЕНТНОСТИ — ДЕТЕРМИНИРОВАННЫЙ `id` ЗАПИСИ РЕЕСТРА (решение №2
плана): `event_id = "portal:<id наряда>"`, а `id` наряда — `portal:<meeting_id>`
портала. Повторная постановка того же наряда даёт ТУ ЖЕ запись реестра, ту же
строку очереди и тот же ключ старта — второго бота не бывает. Ядро дедупа
(`scheduler.dedupe_queue`, `state.state_acquire`) при этом не переписывается:
наряд въезжает в него на общих правах, элементом очереди.

Опасная тройка: сюда едут идентификаторы, время, ссылка на комнату и имена
ОЖИДАЕМЫХ участников (ровно то, что уже возит очередь для личных серий). Тексты
реплик и протоколов модуль не видит и не логирует.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("notary.portal_queue")

QUEUE_TYPE = "portal"

# «Зайти сейчас» ставится в реестр в ту же секунду, а очередь планировщика
# перестраивается раз в 15 минут — то есть наряд «немедленно» появляется в
# очереди уже с опозданием и в обычное окно старта (5 мин до / 1 мин после) не
# попадает. Поэтому у разового наряда окно шире: он остаётся годным к запуску
# столько минут после своего «сейчас», сколько нужно, чтобы пережить тик
# планировщика с запасом.
IMMEDIATE_DUE_MINUTES = 20


def is_enabled() -> bool:
    """Аварийный выключатель `ENABLE_PORTAL_ORDERS`. Дефолт OFF.

    Импорт ленивый и защищённый: модуль идентичности мог не доехать при выкатке —
    тогда наряды считаются выключенными, а не роняют планировщик.
    """
    try:
        from notary.lib import identity  # noqa: PLC0415

        return identity.is_enabled()
    except Exception:  # noqa: BLE001
        return False


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_meeting_id(order: Dict[str, Any]) -> str:
    """`meeting_id` элемента очереди — обязан быть годным для имени контейнера.

    `runner._safe_session_uid` подставляет его в `docker run --name` как есть, а
    `id` наряда содержит двоеточие (`portal:mtg-1`). Двоеточие в имени контейнера
    docker не принимает — бот не стартовал бы вовсе, и выяснилось бы это на живой
    встрече.
    """
    raw = str(order.get("meeting_id") or order.get("id") or "").strip()
    slug = _SAFE_ID_RE.sub("-", raw).strip("-._")
    return ("portal-%s" % slug)[:96] if slug else "portal-order"


def event_id_for(order: Dict[str, Any]) -> str:
    """Ключ идемпотентности наряда: детерминированный `id` записи реестра."""
    return "portal:%s" % str(order.get("id") or "")


def _parse(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def queue_item(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Один наряд → один элемент очереди. None — наряд негоден к исполнению.

    Негоден = нет комнаты или нет разбираемого времени. Молча пропускать нельзя,
    поэтому caller логирует; но и ронять раскрытие остальной очереди из-за одного
    кривого наряда — нельзя тем более.
    """
    if not isinstance(order, dict):
        return None
    start = _parse(order.get("start_at"))
    url = order.get("room_url")
    if start is None or not isinstance(url, str) or not url.strip():
        return None
    try:
        duration = int(order.get("duration_minutes") or 60)
    except (TypeError, ValueError):
        duration = 60
    participants = order.get("expected_participants")
    item: Dict[str, Any] = {
        "meeting_id": safe_meeting_id(order),
        "event_id": event_id_for(order),
        # Обратная ссылка в реестр: по ней runner берёт чат доставки, а Ф10c
        # найдёт, кому отдавать результат.
        "order_id": order.get("id"),
        "series": str(order.get("series") or safe_meeting_id(order)),
        "series_title": order.get("series_title"),
        "company": order.get("company"),
        "start_at": start.isoformat(),
        "duration_minutes": max(1, duration),
        "url": url.strip(),
        "expected_participants": list(participants) if isinstance(participants, list) else [],
        "keep_audio": bool(order.get("keep_audio", False)),
        "type": QUEUE_TYPE,
        # Диагностика дублей в логе схлопывания: у наряда календаря нет.
        "calendar_id": None,
        "ical_uid": None,
    }
    if order.get("kind") == "immediate":
        item["immediate"] = True
    return item


def expand_orders(
    orders: List[Dict[str, Any]],
    horizon_start: datetime,
    horizon_end: datetime,
) -> List[Dict[str, Any]]:
    """Живые наряды → элементы очереди. Чистая функция, диска не касается.

    Берём только `scheduled`: снятый наряд прихода не даёт (R4.14/R12.10), а
    `stopping` — это «выйти сейчас», ставить ему новый старт означало бы вернуть
    бота, которого только что попросили выйти.

    Окно: наряд живёт в очереди, пока встреча НЕ ЗАКОНЧИЛАСЬ (`start+duration`), а
    не пока не начался старт. Иначе старт, ждущий свободного слота в очереди
    ожидания, исчезал бы из очереди через минуту после начала — и ждать было бы
    уже нечего.
    """
    out: List[Dict[str, Any]] = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        if order.get("status") != "scheduled":
            continue
        item = queue_item(order)
        if item is None:
            logger.warning(
                "Наряд %s негоден к исполнению (нет комнаты или времени) — пропущен",
                (order or {}).get("id"),
            )
            continue
        start = _parse(item["start_at"])
        if start is None:
            continue
        end = start + timedelta(minutes=int(item["duration_minutes"]))
        if end < horizon_start or start > horizon_end:
            continue
        out.append(item)
    return out


def load_active_orders() -> List[Dict[str, Any]]:
    """Живые наряды с диска. Любой сбой → пусто + WARNING (fail-open).

    Наряды портала — добавка к очереди владельца, а не условие её существования:
    «бот не пришёл на порталовую встречу» плохо, «бот не пришёл ни на одну» —
    катастрофа, и цена ошибки здесь несимметрична.
    """
    try:
        from notary.lib import orders as orders_lib  # noqa: PLC0415

        return orders_lib.list_orders(statuses={orders_lib.STATUS_SCHEDULED})
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Реестр нарядов не прочитан (%s) — порталовые встречи в этот тик не "
            "исполняются, личные серии владельца не затронуты",
            type(e).__name__,
        )
        return []


def items_for_queue(
    horizon_start: datetime, horizon_end: datetime
) -> List[Dict[str, Any]]:
    """Элементы очереди из реестра нарядов. При OFF — [] БЕЗ обращения к диску."""
    if not is_enabled():
        return []
    return expand_orders(load_active_orders(), horizon_start, horizon_end)


def due_immediate_items(now: datetime) -> List[Dict[str, Any]]:
    """Разовые наряды «зайти сейчас», годные к запуску прямо на этом тике.

    Читаются runner'ом НАПРЯМУЮ, минуя `.queue.json`: очередь перестраивается раз
    в 15 минут, а кнопка «Подключить сейчас» обязана поднимать бота минутами, а
    не четвертью часа. Второго писателя очереди при этом не появляется — список
    живёт только в памяти тика. Дубля тоже: ключ старта тот же самый, что у
    элемента из очереди (`event_id` детерминированный), поэтому пришедший позже
    элемент очереди упрётся в уже занятый ключ.
    """
    if not is_enabled():
        return []
    out: List[Dict[str, Any]] = []
    for order in load_active_orders():
        if not isinstance(order, dict) or order.get("kind") != "immediate":
            continue
        item = queue_item(order)
        if item is None:
            continue
        start = _parse(item["start_at"])
        if start is None:
            continue
        if start > now + timedelta(minutes=1):
            continue  # ещё не «сейчас»
        if now - start > timedelta(minutes=IMMEDIATE_DUE_MINUTES):
            continue  # протух: кнопку нажали давно, бот уже не нужен
        out.append(item)
    return out
