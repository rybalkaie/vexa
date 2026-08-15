"""Занятость бота в окне времени — один расчёт на шлюз и планировщик (Ф10b).

ЗАЧЕМ. Потолок одновременных записей у бота ФИЗИЧЕСКИЙ: на каждого бота свой
headless-Chrome, машина двухъядерная, сборка протокола уже сериализована в один
поток. Дефолт — 2 (`MEETING_NOTARY_MAX_CONCURRENT_BOTS`, `runner.py`). Три
встречи в 10:00 — третья ждёт слота.

🔴 ПОТОЛОК ОБЩИЙ НА ОБЕ КОМПАНИИ, И ЭТО НЕ УПУЩЕНИЕ, А РЕШЕНИЕ. Ограничение
упирается в ядра одной машины, а не в права компании; «по 2 на компанию» дало бы
четыре headless-Chrome на 2 vCPU — то есть четыре испорченные записи вместо двух
хороших. Компания в ответе показывается ТОЛЬКО как диагностика («кто занял»),
решение принимается по общему числу.

🔴 СЧИТАЕМ ОБА ИСТОЧНИКА. До этой фазы шлюз видел только наряды портала и мог
пообещать порталу свободный слот в час, когда у владельца стоят две личные
встречи из `watched.yaml`. Поэтому здесь складываются наряды портала И очередь
планировщика (в ней личные серии владельца), с защитой от двойного счёта одной и
той же встречи, приехавшей из обоих источников.

Опасная тройка: наружу уходят только числа и коды. Ни ссылок на комнаты, ни имён.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("notary.slots")

DEFAULT_MAX_CONCURRENT_BOTS = 2


def max_concurrent_bots() -> int:
    """Потолок одновременных записей. ОДНА переменная на весь проект.

    Две константы «сколько ботов разом» в двух файлах разъезжаются, и портал
    начинает обещать слот, которого нет.
    """
    raw = os.environ.get("MEETING_NOTARY_MAX_CONCURRENT_BOTS")
    try:
        value = int(raw) if raw and raw.strip() else DEFAULT_MAX_CONCURRENT_BOTS
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT_BOTS
    return value if value > 0 else DEFAULT_MAX_CONCURRENT_BOTS


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


def _slot_key(start: datetime, url: Any) -> str:
    """Одна встреча = один слот, из скольких бы источников она ни приехала."""
    return "%s|%s" % (
        start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M"),
        str(url or "").strip().lower(),
    )


def _overlaps(
    a_start: datetime, a_end: datetime, b_start: datetime, b_minutes: Any
) -> bool:
    try:
        minutes = int(b_minutes or 60)
    except (TypeError, ValueError):
        minutes = 60
    b_end = b_start + timedelta(minutes=max(1, minutes))
    return b_start < a_end and a_start < b_end


def occupancy(
    start: datetime,
    duration_minutes: Any,
    *,
    exclude_order_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Кто занимает бота в окне `[start; start+duration)`.

    Мягкая к сбоям: нечитаемый источник не роняет расчёт, а помечается в
    `errors` — «подсказка о занятости» не повод потерять уже принятый наряд.
    """
    now = now or datetime.now(timezone.utc)
    try:
        minutes = int(duration_minutes or 60)
    except (TypeError, ValueError):
        minutes = 60
    end = start + timedelta(minutes=max(1, minutes))

    limit = max_concurrent_bots()
    seen: Dict[str, str] = {}          # ключ слота → источник
    by_company: Dict[str, int] = {}
    errors: List[str] = []

    # 1. Наряды портала.
    try:
        from notary.lib import orders as orders_lib  # noqa: PLC0415

        for order in orders_lib.active_orders_overlapping(
            start, max(1, minutes), exclude_id=exclude_order_id
        ):
            other_start = _parse(order.get("start_at"))
            if other_start is None:
                continue
            key = _slot_key(other_start, order.get("room_url"))
            seen.setdefault(key, "portal_orders")
            company = str(order.get("company") or "?")
            by_company[company] = by_company.get(company, 0) + 1
    except Exception as exc:  # noqa: BLE001
        errors.append("orders:%s" % type(exc).__name__)

    # 2. Очередь планировщика (в ней личные серии владельца И уже разложенные
    #    наряды портала — отсюда защита от двойного счёта по ключу слота).
    try:
        from notary.lib import state as state_lib  # noqa: PLC0415

        for item in state_lib.read_queue():
            if not isinstance(item, dict):
                continue
            if exclude_order_id and item.get("order_id") == exclude_order_id:
                continue
            other_start = _parse(item.get("start_at"))
            if other_start is None:
                continue
            if not _overlaps(start, end, other_start, item.get("duration_minutes")):
                continue
            key = _slot_key(other_start, item.get("url"))
            if key in seen:
                continue
            seen[key] = "scheduler_queue"
            company = str(item.get("company") or "series")
            by_company[company] = by_company.get(company, 0) + 1
    except Exception as exc:  # noqa: BLE001
        errors.append("queue:%s" % type(exc).__name__)

    busy = len(seen)
    by_source: Dict[str, int] = {}
    for source in seen.values():
        by_source[source] = by_source.get(source, 0) + 1

    out: Dict[str, Any] = {
        "limit": limit,
        # Компания здесь ТОЛЬКО диагностика: решение принимается по общему числу
        # (потолок — про ядра машины, а не про права компании).
        "scope": "all_companies",
        "known": not errors,
        "busy": busy,
        "free_slots": max(0, limit - busy),
        "by_source": by_source,
        "by_company": by_company,
        "warning": (
            None
            if busy < limit
            else "в это время уже занято %d записей при потолке %d — бот зайдёт "
            "только если слот освободится до конца встречи, и опоздание будет "
            "подписано в протоколе" % (busy, limit)
        ),
    }
    if errors:
        out["errors"] = errors
    return out
