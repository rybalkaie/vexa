"""Строка владельцу, когда протокол встречи собран запасной моделью.

Основная модель протокола (`PROTOCOL_GEN_MODEL`, уровень Opus) не уложилась во время
или недоступна (снята — 404, перегружена — 529), и `generate_protocol` собрал протокол
на запасной (`PROTOCOL_GEN_FALLBACK_MODEL`, уровень Sonnet). Встреча не теряется, но
протокол слабее — владелец хочет об этом знать (слово владельца: пуш при сборке
протокола на запасной модели — «давай»).

Канал — `notify.push`, как у остальных строк владельцу: на сервере `TG_SEND_BIN` —
отправитель Ватсона (`/srv/meeting-notary/bin/tg-send`, его бот, личка владельца).
Класс «ожидаемое» уходит переменной `TG_SEND_CLASS=expected`: общий `tg-send` со
шлюзом тишины без класса оставил бы строку только в журнале. Только владельцу —
участникам встречи ничего не уходит. Дедуп по встрече и причине: повторный прогон
финализации той же встречи не повторяет строку (окно `notify`).
Шлём, только когда отправитель задан явно (`TG_SEND_BIN` — на сервере он в
`.env.notary`): на маке `notify` по умолчанию берёт настоящий `~/.local/bin/tg-send`,
и тесты генерации протокола, проходящие через запасную модель, слали бы владельцу
настоящие строки. Выключатель без выкатки кода — `NOTARY_PROTOCOL_FALLBACK_ALERT=0`.
Опасная тройка: в строке только серия, дата, модели и причина — без текста встречи.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_SWITCH_ENV = "NOTARY_PROTOCOL_FALLBACK_ALERT"

_REASONS = {
    "primary-timeout": "не уложилась во время",
    "primary-unavailable": "недоступна (снята или перегружена)",
}


def alert_owner(
    *,
    meeting_sid: Optional[str],
    meeting_meta: Optional[dict],
    primary: str,
    used: str,
    reason: str,
    answered: Optional[list] = None,
    pusher: Optional[Callable[..., object]] = None,
) -> bool:
    """Шлёт владельцу строку «протокол собран запасной моделью». Никогда не бросает.

    `answered` — версии, которые фактически ответили (из ответа клиента), для строки.
    `pusher` — подмена отправки в тестах; по умолчанию `notify.push` с классом
    `expected`. Возвращает True, если строка ушла.
    """
    if (os.environ.get(_SWITCH_ENV) or "").strip().lower() in ("0", "false", "no", "off"):
        logger.info("[protocol] протокол собран запасной моделью; строка владельцу выключена (%s)",
                    _SWITCH_ENV)
        return False
    try:
        meta = meeting_meta if isinstance(meeting_meta, dict) else {}
        series = str(meta.get("series") or "без серии")
        date = str(meta.get("date") or "")
        version = f", {answered[0]}" if answered else ""
        text = (
            f"⚠️ Ватсон: протокол встречи «{series}» {date} собран запасной моделью "
            f"({used}{version}) — основная ({primary}) {_REASONS.get(reason, reason)}. "
            f"Протокол идёт по обычному пути; перечитайте его внимательнее."
        )
        key = f"protocol-fallback:{meeting_sid or series + ':' + date}:{reason}"
        if pusher is None:
            if not (os.environ.get("TG_SEND_BIN") or "").strip():
                logger.warning("[protocol] протокол собран запасной моделью; TG_SEND_BIN не задан — "
                               "строка владельцу не отправлена")
                return False
            from . import notify  # noqa: PLC0415 — lazy: тестам не нужен tg-send
            return bool(notify.push(text, dedupe_key=key, send_class="expected"))
        return bool(pusher(text, dedupe_key=key, send_class="expected"))
    except Exception as e:  # noqa: BLE001 — сломанная строка владельцу не валит протокол
        logger.warning("[protocol] строка владельцу о запасной модели не ушла: %s", type(e).__name__)
        return False
