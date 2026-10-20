"""Номер версии результата встречи для ленты версий портала.

Одно правило на два пути: перевыпуск по правке (`feedback_reissue._next_portal_version`)
и ручное восстановление (`portal_recovery.next_portal_version`). Вынесено из
`feedback_reissue` в ходе 4 цикла Ф2, чтобы не держать две копии подсчёта.
"""
from __future__ import annotations

from typing import Any, Optional


def is_result_item(item: Any, order_id: str) -> bool:
    """Элемент очереди — результат этого наряда (версия протокола), а не служебное
    событие. «Бот не пришёл» (`#no_show`) лежит в той же очереди с `version=1`, но
    версией протокола не является: посчитать его — значит отдать первую версию
    номером 2 с `revision_of=1`. Отчёт об уборке (`#purged`) — считаем: его номер —
    номер реально доставленной версии (её саму метельщик мог уже убрать)."""
    from . import portal_delivery  # noqa: PLC0415

    if not isinstance(item, dict) or str(item.get("order_id") or "") != order_id:
        return False
    key = str(item.get("key") or "")
    if key.endswith(portal_delivery.PURGE_SUFFIX):
        return True
    payload = item.get("payload")
    event = payload.get("event") if isinstance(payload, dict) else None
    return not key.endswith(portal_delivery._NO_SHOW_SUFFIX) and not event


def next_version(
    order_id: str, *, meta: Optional[dict] = None, order: Optional[dict] = None,
    recovery: Optional[bool] = None,
) -> int:
    """Номер следующей версии результата для портала.

    Считаем по УЖЕ поставленным в очередь ключам `<наряд>#v<N>`: первая доставка
    делает v1 (`collector`), каждый перевыпуск — следующий номер.

    🔴 Очередь — НЕ единственный источник. Доставленные элементы из неё убирают
    (метельщик, ручная чистка), и счёт по одной только очереди после уборки
    начинался бы заново: номер повторялся, а `enqueue` на занятый ключ молча
    отдаёт `created=False` — новая версия исчезала бы без следа. Поэтому high-water
    отметка дублируется в meta встречи (`portal_version`), которая живёт столько
    же, сколько сама встреча. Плюс страховка ниже: занятый ключ пропускаем.

    Ручное восстановление (`recovery`, по умолчанию — `meta.recovery is True`;
    его первая доставка — `portal_recovery.next_portal_version`) считает
    иначе: v1 сборщик ему не ставил, поэтому служебные события («бот не пришёл»)
    версиями не считаются, а если результатов этого наряда ещё нет — первая версия
    (`result_version` наряда): портал отвергает разрывы версий. Встреча бота —
    буква в букву как было.
    """
    from . import portal_delivery  # noqa: PLC0415

    if recovery is None:
        recovery = isinstance(meta, dict) and meta.get("recovery") is True
    highest = 0
    if isinstance(meta, dict):
        try:
            highest = max(highest, int(meta.get("portal_version") or 0))
        except (TypeError, ValueError):
            highest = highest
    for item in portal_delivery.list_items():
        if recovery:
            if not is_result_item(item, order_id):
                continue
        elif not isinstance(item, dict) or str(item.get("order_id") or "") != order_id:
            continue
        try:
            highest = max(highest, int(item.get("version") or 0))
        except (TypeError, ValueError):
            continue
    if highest:
        version = highest + 1
    elif recovery:
        try:
            version = max(1, int((order or {}).get("result_version") or 1))
        except (TypeError, ValueError):
            version = 1
    else:
        version = 2
    # Ключ занят (очередь видит не всё, что лежит на диске) — берём следующий
    # свободный, а не пишем в чужой: `enqueue` вернул бы created=False и версия
    # пропала бы молча.
    while portal_delivery.item_path(portal_delivery.delivery_key(order_id, version)).exists():
        version += 1
    return version
