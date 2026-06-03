"""Ф5 (5.3): grace-окно перед первой доставкой протокола.

После встречи бот сначала шлёт вопрос-уточнение спикеров, затем ждёт ~5 мин
шанса, что Илья ответит и имя попадёт уже в ПЕРВУЮ доставку. Доставка НЕ
блокируется навсегда: по истечении окна шлём как есть; поздний ответ (в окне
суток) дошлёт обновлённую версию (5.5/5.6, см. `clarify_worker`).

Вынесено отдельным модулем (а не в `finalize-meeting.py`), чтобы было
импортируемо в unit-тестах без тяжёлых зависимостей finalize (requests и т.п.).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from . import clarify_state

logger = logging.getLogger(__name__)


def delivery_grace_sec() -> int:
    """Окно ожидания ответа на clarify ПЕРЕД первой доставкой.

    `PROTOCOL_DELIVERY_GRACE_SEC` (дефолт 300 = ~5 мин). 0 → ждать не нужно
    (тесты/отладка). finalize и так может идти до 3ч (timeout collector'а
    10800s), 5 мин — не угроза таймауту.
    """
    try:
        return max(0, int(os.environ.get("PROTOCOL_DELIVERY_GRACE_SEC", "300")))
    except ValueError:
        return 300


def wait_for_clarify_grace(
    meeting_id: str,
    log: Optional[logging.Logger] = None,
    *,
    poll_sec: int = 10,
) -> str:
    """Ждёт ~5 мин ответа на clarify (5.3), но НЕ блокирует доставку навсегда.

    Поллит clarify-state: как только статус перестал быть `pending` (Илья
    ответил → resolved) — выходим раньше. По истечении окна — выходим как есть
    (доставим с тем, что распозналось; поздний ответ дошлёт ревизию 5.5/5.6).

    Возвращает финальный статус: "skipped" (grace=0) / "resolved"|"timed_out"
    (ранний выход по статусу) / "gone" (state исчез) / "timeout" (окно вышло).
    """
    log = log or logger
    grace = delivery_grace_sec()
    if grace <= 0:
        return "skipped"
    waited = 0
    log.info("[delivery] grace-wait meeting=%s up to %ds for clarify answer", meeting_id, grace)
    while waited < grace:
        state = clarify_state.read_state(meeting_id)
        if state is None:
            return "gone"
        if state.get("status") != "pending":
            log.info(
                "[delivery] grace-wait meeting=%s ended early: status=%s after %ds",
                meeting_id, state.get("status"), waited,
            )
            return state.get("status") or "resolved"
        time.sleep(min(poll_sec, grace - waited))
        waited += poll_sec
    log.info("[delivery] grace-wait meeting=%s elapsed %ds — delivering as-is", meeting_id, grace)
    return "timeout"
