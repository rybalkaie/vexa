"""Ф8 Шаг 8.3 — обработка inline-callback'ов авто-словаря в listener'е.

Подключается в `meetings_listener.process_callback_query` в общую цепочку
(после delivery/task/clarify). Префиксы `vocab:approve_all:<sid>` /
`vocab:reject_all:<sid>` — НЕ пересекаются с `cd:`/`tf:`/`td:`/`cl:`.

Поштучный выбор («🔧 Выбрать») в этой версии НЕ реализован (backlog).

pending-state кладёт `auto_vocab/applier.py` в `<pending_root>/vocab/<sid>.json`.
`sweep_timeouts` отклоняет запросы старше TIMEOUT_H (кандидаты → rejected_terms).

Авторизация отправителя проверяется ВЫШЕ — в `process_callback_query`
(`chat_id != allowed_chat` → отбой до вызова воркеров), поэтому здесь не дублируем.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "vocab:"
TIMEOUT_H = 48  # запрос без ответа старше 48ч → авто-reject


def _pending_file(pending_root: Path, session_uid: str) -> Path:
    return pending_root / "vocab" / f"{session_uid}.json"


def process_callback(cbq: dict[str, Any], pending_root: Path, token: str) -> bool:
    """Обработать vocab-callback. Возвращает True если это наш callback (handled)."""
    data = (cbq.get("data") or "")
    if not data.startswith(PREFIX):
        return False  # не наш — пусть цепочка идёт дальше

    cbq_id = cbq.get("id")
    body = data[len(PREFIX):]  # "approve_all:<sid>" / "reject_all:<sid>"
    try:
        action, session_uid = body.split(":", 1)
    except ValueError:
        logger.warning("[vocab] битый callback_data: %r", data)
        _answer(token, cbq_id, "Непонятная кнопка")
        return True

    pend = _pending_file(pending_root, session_uid)
    if not pend.exists():
        logger.info("[vocab] pending для sid=%s не найден (уже обработан/устарел)", session_uid)
        _answer(token, cbq_id, "Уже обработано")
        return True
    try:
        st = json.loads(pend.read_text(encoding="utf-8"))
        candidates = st.get("candidates") or []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[vocab] pending %s битый (%s)", pend, e)
        _answer(token, cbq_id, "Ошибка состояния")
        return True

    from notary.auto_vocab import applier  # noqa: PLC0415

    if action == "approve_all":
        added = applier.commit_terms(candidates)
        reply = f"✅ Добавил в словарь: {', '.join(added)}" if added else "✅ Готово (все уже были)"
        logger.info("[vocab] approve_all sid=%s → +%d", session_uid, len(added))
    elif action == "reject_all":
        applier.reject_terms(candidates)
        reply = "❌ Ничего не добавил"
        logger.info("[vocab] reject_all sid=%s → отклонено %d", session_uid, len(candidates))
    else:
        logger.warning("[vocab] неизвестное действие: %r", action)
        _answer(token, cbq_id, "Неизвестное действие")
        return True

    _finish(token, st, pend, cbq_id, reply)
    return True


def _finish(token: str, st: dict, pend: Path, cbq_id: str | None, reply: str) -> None:
    """Снять крутилку, перезаписать сообщение (убрать кнопки), удалить pending."""
    _answer(token, cbq_id, reply[:200])
    chat_id, message_id = st.get("chat_id"), st.get("message_id")
    if chat_id and message_id:
        try:
            from notary.lib import telegram_api  # noqa: PLC0415
            name = st.get("meeting_name", "")
            telegram_api.edit_message_text(
                token, chat_id, message_id,
                f"🆕 Кандидаты после «{name}»\n{reply}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[vocab] editMessageText не удался (%s) — не критично", e)
    try:
        pend.unlink()
    except OSError:
        pass


def _answer(token: str, cbq_id: str | None, text: str) -> None:
    if not cbq_id:
        return
    try:
        from notary.lib import telegram_api  # noqa: PLC0415
        telegram_api.answer_callback_query(token, cbq_id, text=text)
    except Exception as e:  # noqa: BLE001
        logger.warning("[vocab] answerCallbackQuery не удался (%s)", e)


def sweep_timeouts(pending_root: Path) -> int:
    """Запросы старше TIMEOUT_H → авто-reject кандидатов + удалить pending.

    Возвращает число обработанных. Best-effort (битые файлы пропускаются)."""
    vocab_dir = pending_root / "vocab"
    if not vocab_dir.exists():
        return 0
    from notary.auto_vocab import applier  # noqa: PLC0415
    now = datetime.now()
    n = 0
    for pend in vocab_dir.glob("*.json"):
        try:
            st = json.loads(pend.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(st.get("created_at"))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            continue
        if (now - created).total_seconds() < TIMEOUT_H * 3600:
            continue
        applier.reject_terms(st.get("candidates") or [])
        token = _resolve_token()
        if token and st.get("chat_id") and st.get("message_id"):
            try:
                from notary.lib import telegram_api  # noqa: PLC0415
                telegram_api.edit_message_text(
                    token, st["chat_id"], st["message_id"],
                    f"⌛️ Кандидаты после «{st.get('meeting_name','')}» — не ответили за {TIMEOUT_H}ч, пропустил.",
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            pend.unlink()
        except OSError:
            pass
        n += 1
    if n:
        logger.info("[vocab] sweep: %d просроченных запросов авто-отклонено", n)
    return n


def _resolve_token() -> str | None:
    import os
    return os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
