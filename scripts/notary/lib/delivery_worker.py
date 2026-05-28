"""Ф6 worker — обработка ответов Ильи на «куда отправить протокол».

Структура зеркалит Ф5 `task_clarify_worker.py`:
  - `process_callback(cbq, pending_root, token)` — обрабатывает callback_query
    с префиксом `cd:`.
  - `process_text_message(msg, pending_root, token)` — текстовый ответ,
    если есть ровно один pending delivery state (иначе бот переспрашивает).
  - `sweep_timeouts(pending_root)` — помечает истёкшие как `timed-out`
    + decision=skip-timeout.
  - `has_any_pending_delivery(pending_root)` — для роутинга в listener.

Импортируется из `meetings_listener.py` (под `venv-cli`). Прямые импорты
LLM-постпроцессинга — ленивые (модуль может тянуть pyannote).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


_DELIVERY_STATE_SUFFIX = "-delivery.json"


def _list_pending_delivery_states(pending_root: Path) -> list[dict]:
    """Возвращает все валидные delivery-state'ы со status=pending."""
    if not pending_root.exists():
        return []
    out: list[dict] = []
    for f in pending_root.glob(f"*{_DELIVERY_STATE_SUFFIX}"):
        if f.name.startswith("."):
            continue
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[delivery] skip malformed %s: %s", f.name, e)
            continue
        if not isinstance(state, dict):
            continue
        if state.get("kind") != "delivery":
            continue
        if state.get("status") != "pending":
            continue
        state["_path"] = str(f)
        out.append(state)
    return out


def has_any_pending_delivery(pending_root: Path) -> bool:
    return bool(_list_pending_delivery_states(pending_root))


def _state_path_for(pending_root: Path, meeting_id: str) -> Path:
    return pending_root / f"{meeting_id}{_DELIVERY_STATE_SUFFIX}"


def _read_state_by_meeting_id(pending_root: Path, meeting_id: str) -> Optional[dict]:
    p = _state_path_for(pending_root, meeting_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write_state(target_path: Path, state: dict) -> None:
    """Локальная замена `_atomic_write_text` (без import цикла на llm_postprocess)."""
    import tempfile
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=str(target_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, target_path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _short_id(meeting_id: str) -> str:
    import hashlib
    return hashlib.sha1(meeting_id.encode("utf-8")).hexdigest()[:8]


def _find_state_by_short_id(pending_root: Path, mid_short: str) -> Optional[dict]:
    """Найти state по 8-байтному хешу meeting_id (callback). Берёт первый матч."""
    for state in _list_pending_delivery_states(pending_root):
        mid = state.get("meeting_id") or ""
        if _short_id(mid) == mid_short:
            return state
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _deliver_now(
    state: dict,
    token: str,
    chat_id_target: int,
    *,
    persist_binding: bool,
) -> bool:
    """Шлёт протокол в chat_id_target. Если `persist_binding=True` — пишет
    привязку в watched.yaml.

    Возвращает True на успех.
    """
    series = (state.get("meta") or {}).get("series") or ""
    date = (state.get("meta") or {}).get("date") or ""
    sid = (state.get("meta") or {}).get("sessionUid") or state.get("meeting_id") or ""

    # Резолвим путь к протоколу.
    try:
        from .llm_postprocess import (  # noqa: PLC0415
            _resolve_protocol_paths,
            deliver_protocol,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[delivery] llm_postprocess import failed: %s", e)
        return False

    transcript_path, protocol_path, meta_json_path = _resolve_protocol_paths(series, date)
    if not protocol_path or not protocol_path.is_file():
        logger.warning(
            "[delivery] protocol not found series=%s date=%s — cannot deliver after clarify",
            series, date,
        )
        return False
    try:
        protocol_text = protocol_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[delivery] read protocol failed: %s", e)
        return False

    # Если нужно — пишем в watched.yaml ДО send'а (если send упал, привязку всё
    # равно оставляем — Илья хочет её для будущих встреч).
    if persist_binding and series:
        try:
            from .llm_postprocess import _persist_telegram_chat_id  # noqa: PLC0415
            saved = _persist_telegram_chat_id(series, chat_id_target)
            logger.info(
                "[delivery] persisted chat_id=%s for series=%s saved=%s",
                chat_id_target, series, saved,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[delivery] persist watched.yaml failed: %s", e)

    result = deliver_protocol(
        meeting_meta={
            "series": series,
            "date": date,
            "sessionUid": sid,
            "telegram_chat_id": chat_id_target,
        },
        protocol_text=protocol_text,
        meta_json_path=meta_json_path,
        target_chat_id=chat_id_target,
        meeting_sid=sid,
    )
    return result.get("status") in ("sent", "skipped")


def _finalize_state(
    state_path: Path,
    state: dict,
    *,
    status: str,
    decision: Optional[str] = None,
    chat_id: Optional[int] = None,
) -> None:
    """Маркирует state как resolved/timed_out + decision (skip/dm/chat/timeout)."""
    state["status"] = status
    state["resolved_at"] = _now_iso()
    if decision is not None:
        state["decision"] = decision
    if chat_id is not None:
        state["resolved_chat_id"] = chat_id
    try:
        _atomic_write_state(state_path, state)
    except OSError as e:
        logger.warning("[delivery] finalize_state write failed: %s", e)


def process_callback(cbq: dict[str, Any], pending_root: Path, token: str) -> bool:
    """Если callback_query — наш `cd:`, обрабатывает и возвращает True.

    Иначе False (вызывающий передаёт дальше в clarify-worker).
    """
    data = (cbq.get("data") or "").strip()
    if not data.startswith("cd:"):
        return False
    body = data[3:]
    parts = body.split(":")
    if len(parts) != 2:
        return True  # наш префикс — но битый формат, не передаём дальше
    mid_short, action = parts
    state = _find_state_by_short_id(pending_root, mid_short)
    if not state:
        logger.info("[delivery] callback for unknown/expired mid_short=%s", mid_short)
        # snimaем крутилку.
        msg = cbq.get("message") or {}
        chat_id_msg = (msg.get("chat") or {}).get("id")
        try:
            from . import telegram_api  # noqa: PLC0415
            telegram_api.answer_callback_query(
                token, cbq.get("id") or "",
                text="Запрос устарел — пропустил.",
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    meeting_id = state.get("meeting_id") or ""
    state_path = _state_path_for(pending_root, meeting_id)
    series = (state.get("meta") or {}).get("series") or "—"
    date = (state.get("meta") or {}).get("date") or "—"

    if action == "skip":
        _finalize_state(state_path, state, status="resolved", decision="skip")
        logger.info("[delivery] skipped meeting=%s reason=user-skip", meeting_id)
        try:
            from . import telegram_api  # noqa: PLC0415
            telegram_api.answer_callback_query(token, cbq.get("id") or "", text="Принято — не отправляю.")
            telegram_api.send_message(
                token, state.get("chat_id") or 0,
                f"🚫 Не отправил протокол «{series}» {date} (по запросу).",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[delivery] post-skip notify failed: %s", e)
        return True

    if action == "dm":
        dm_chat = state.get("chat_id") or 0
        ok = _deliver_now(state, token, dm_chat, persist_binding=False)
        if ok:
            _finalize_state(state_path, state, status="resolved", decision="dm", chat_id=dm_chat)
            logger.info("[delivery] dm-only meeting=%s", meeting_id)
        else:
            _finalize_state(state_path, state, status="resolved", decision="dm-failed")
        try:
            from . import telegram_api  # noqa: PLC0415
            telegram_api.answer_callback_query(
                token, cbq.get("id") or "",
                text="Отправил в личку." if ok else "Ошибка отправки.",
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    # Неожиданный action — игнор, но True (наш префикс).
    return True


def process_text_message(msg: dict[str, Any], pending_root: Path, token: str) -> bool:
    """Текстовый ответ. Возвращает True если обработали (вызывающий выходит).

    Работает только если есть РОВНО ОДИН pending delivery; при 2+ просим
    пользователя нажать кнопку (избегаем неоднозначности).
    """
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    pendings = _list_pending_delivery_states(pending_root)
    if not pendings:
        return False
    if len(pendings) > 1:
        # Неоднозначно — просим использовать кнопку (на каждом сообщении свой mid_short).
        try:
            from . import telegram_api  # noqa: PLC0415
            telegram_api.send_message(
                token, msg.get("chat", {}).get("id") or 0,
                "⚠️ Несколько висящих вопросов о доставке протокола — нажми кнопку под нужным.",
                reply_to_message_id=msg.get("message_id"),
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    state = pendings[0]
    state_path = Path(state.pop("_path"))
    meeting_id = state.get("meeting_id") or ""
    series = (state.get("meta") or {}).get("series") or "—"
    date = (state.get("meta") or {}).get("date") or "—"
    chat_id_user = msg.get("chat", {}).get("id") or 0

    try:
        from .llm_postprocess import parse_chat_destination_answer  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.exception("[delivery] llm_postprocess import failed (parse): %s", e)
        return False

    kind, parsed_chat = parse_chat_destination_answer(text)

    if kind == "skip":
        _finalize_state(state_path, state, status="resolved", decision="skip")
        try:
            from . import telegram_api  # noqa: PLC0415
            telegram_api.send_message(
                token, chat_id_user,
                f"🚫 Не отправил протокол «{series}» {date}.",
                reply_to_message_id=msg.get("message_id"),
            )
        except Exception:  # noqa: BLE001
            pass
        logger.info("[delivery] skipped meeting=%s reason=user-skip-text", meeting_id)
        return True

    if kind == "dm":
        ok = _deliver_now(state, token, chat_id_user, persist_binding=False)
        if ok:
            _finalize_state(state_path, state, status="resolved", decision="dm", chat_id=chat_id_user)
        else:
            _finalize_state(state_path, state, status="resolved", decision="dm-failed")
        try:
            from . import telegram_api  # noqa: PLC0415
            telegram_api.send_message(
                token, chat_id_user,
                "✅ Отправил в личку." if ok else "❌ Не смог отправить.",
                reply_to_message_id=msg.get("message_id"),
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    if kind == "chat" and parsed_chat is not None:
        ok = _deliver_now(state, token, parsed_chat, persist_binding=True)
        if ok:
            _finalize_state(state_path, state, status="resolved", decision="chat", chat_id=parsed_chat)
        else:
            _finalize_state(state_path, state, status="resolved", decision="chat-failed", chat_id=parsed_chat)
        try:
            from . import telegram_api  # noqa: PLC0415
            telegram_api.send_message(
                token, chat_id_user,
                (
                    f"✅ Отправил в chat_id={parsed_chat}, привязку «{series}» запомнил."
                    if ok else
                    f"❌ Не смог отправить в chat_id={parsed_chat}. Проверь, что бот добавлен в группу."
                ),
                reply_to_message_id=msg.get("message_id"),
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    # invalid → подсказка.
    try:
        from . import telegram_api  # noqa: PLC0415
        telegram_api.send_message(
            token, chat_id_user,
            "❓ Не распознал ответ. Жду: chat_id (число), ссылку https://t.me/c/.../, "
            "«в личку», «никуда».",
            reply_to_message_id=msg.get("message_id"),
        )
    except Exception:  # noqa: BLE001
        pass
    return True


def sweep_timeouts(pending_root: Path) -> int:
    """Помечает истёкшие state'ы как `timed_out` (decision=skip-timeout).

    Возвращает число пометок. Идемпотентно — повторный вызов не дублирует.
    """
    if not pending_root.exists():
        return 0
    n = 0
    now = datetime.now(timezone.utc)
    for f in pending_root.glob(f"*{_DELIVERY_STATE_SUFFIX}"):
        if f.name.startswith("."):
            continue
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        if state.get("kind") != "delivery":
            continue
        if state.get("status") != "pending":
            continue
        deadline = state.get("deadline_at")
        if not isinstance(deadline, str):
            continue
        try:
            dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now < dt:
            continue
        state["status"] = "timed_out"
        state["decision"] = "skip-timeout"
        state["resolved_at"] = _now_iso()
        try:
            _atomic_write_state(f, state)
            n += 1
        except OSError as e:
            logger.warning("[delivery] sweep write failed %s: %s", f, e)
    return n
