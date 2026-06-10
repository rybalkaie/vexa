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


# RISK5 (Ф1 delivery-fixes / R-REPLY): интерактивная фича «бот сам спрашивает,
# куда слать протокол» СНЯТА — `ask_delivery_destination` больше не зовётся
# (inline-кнопки листенером не обрабатывались, протокол зависал; `deliver_protocol`
# теперь дефолтит в личку без вопроса, REQ 7.2). Новые `*-delivery.json` не
# создаются. Но обработчики ответа (`process_text_message`/`process_callback`)
# ещё в wiring'е листенера — это «полу-живая ask-машинерия», кандидат №1 в
# «спросил, а на ответ не реагирует»: ОРФАН-state от снятой фичи мог бы
# перехватить НЕ-reply сообщение владельца (распознать как «куда слать») и
# проглотить его. Поэтому ниже обработчики НЕ глотают, а ретайрят орфаны и
# пропускают сообщение дальше — к реальным обработчикам (правки/clarify).
_ORPHAN_RETIRE_DECISION = "ask-feature-retired"


def retire_orphan_delivery_states(pending_root: Path) -> int:
    """RISK5: терминализует осиротевшие pending delivery-state'ы (фича снята).

    Возвращает число ретайрнутых. Идемпотентно (после ретайра status != pending
    → больше не listится). Диагностика: логируем только число (R9) — без текста/имён.
    """
    if not pending_root or not pending_root.exists():
        return 0
    n = 0
    for state in _list_pending_delivery_states(pending_root):
        sp = Path(state.pop("_path"))
        _finalize_state(sp, state, status="retired", decision=_ORPHAN_RETIRE_DECISION)
        n += 1
    if n:
        logger.warning(
            "[delivery] RISK5: ретайрнул %d осиротевших delivery-ask state(s) — фича "
            "«бот спрашивает куда слать» снята; орфаны больше не перехватывают ответы владельца",
            n,
        )
    return n


def process_callback(cbq: dict[str, Any], pending_root: Path, token: str) -> bool:
    """RISK5: `cd:`-callback от СНЯТОЙ фичи «куда слать». Кнопки осиротели —
    снимаем крутилку, ретайрим орфаны, НЕ доставляем. True = наш префикс прожёван.

    Иначе (не `cd:`) — False (вызывающий передаёт дальше в clarify-worker).
    """
    data = (cbq.get("data") or "").strip()
    if not data.startswith("cd:"):
        return False
    retire_orphan_delivery_states(pending_root)
    logger.warning(
        "[delivery] RISK5: callback `cd:` при снятой фиче «куда слать» — кнопка "
        "устарела, ретайрил орфаны (не доставляю)",
    )
    try:
        from . import telegram_api  # noqa: PLC0415
        telegram_api.answer_callback_query(
            token, cbq.get("id") or "", text="Кнопка устарела — пропустил.",
        )
    except Exception:  # noqa: BLE001
        pass
    return True


def process_text_message(msg: dict[str, Any], pending_root: Path, token: str) -> bool:
    """RISK5: текстовый ответ при снятой фиче «куда слать».

    Возвращает ВСЕГДА False — НЕ перехватываем сообщение владельца (иначе «спросил
    и не реагирует»): любой pending тут — орфан снятой фичи, ретайрим его и
    пропускаем сообщение дальше к реальным обработчикам (правки/clarify/task).
    """
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    pendings = _list_pending_delivery_states(pending_root)
    if not pendings:
        return False
    # Орфаны снятой фичи: ретайрим + диагностика, но НЕ глотаем (return False).
    retire_orphan_delivery_states(pending_root)
    logger.warning(
        "[delivery] RISK5: текстовый ответ при %d орфан-delivery-state(s) снятой фичи "
        "«куда слать» — не перехватываю, ретайрил орфаны, передаю дальше", len(pendings),
    )
    return False


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
