"""Worker для clarification ответов по задачам (Ф5 meeting-notary-llm).

Аналог `clarify_worker.py` (спикеры) для двух новых state-типов:

  - `<meeting_id>-tasks.json`        — анти-галлюцинация (фильтрация N задач).
    Callback: `tf:<mid_short>:keep`. Текст: «оставить все» / «убрать 3,5,7».
  - `<meeting_id>-deadlines.json`    — clarification дедлайнов задач Ильи.
    Текст: «1=2026-06-05, 2=на этой неделе, 3=без срока». Без callback'ов
    (даты слишком разнообразные для кнопок).

Воркер вызывается из `meetings_listener.py` (один long-poll на токен).
Дисциплина «Опасной тройки»: в лог — только meeting_id + counts.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from . import clarify_state
from . import llm_postprocess
from . import telegram_api


logger = logging.getLogger(__name__)


_TASKS_FILE_SUFFIX = "-tasks.json"
_DEADLINES_FILE_SUFFIX = "-deadlines.json"


def _list_task_states(
    pending_root: Path,
    *,
    kind: str,
    status_filter: Optional[list[str]] = None,
) -> list[dict]:
    """Сканирует pending_root, возвращает state'ы заданного kind ('task_filter' / 'task_deadlines')."""
    if not pending_root.exists():
        return []
    out: list[dict] = []
    suffix = _TASKS_FILE_SUFFIX if kind == "task_filter" else _DEADLINES_FILE_SUFFIX
    allowed = set(status_filter) if status_filter else None
    for f in pending_root.glob(f"*{suffix}"):
        if f.name.startswith("."):
            continue
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[task-clarify] skip malformed %s: %s", f.name, e)
            continue
        if not isinstance(state, dict) or state.get("kind") != kind:
            continue
        if allowed is not None and state.get("status") not in allowed:
            continue
        out.append(state)
    return out


def has_any_pending_task_clarify(pending_root: Optional[Path] = None) -> bool:
    """True если есть хоть один pending/timed_out state по задачам."""
    root = pending_root or clarify_state.resolve_pending_dir()
    if not root.exists():
        return False
    for kind in ("task_filter", "task_deadlines"):
        if _list_task_states(root, kind=kind, status_filter=["pending", "timed_out"]):
            return True
    return False


def _write_state(state: dict, pending_root: Path, *, file_suffix: str) -> None:
    """Atomic запись state'а через `_atomic_write_text` из llm_postprocess."""
    meeting_id = state["meeting_id"]
    safe_id = llm_postprocess._validate_meeting_id_for_task(meeting_id)
    target = pending_root / f"{safe_id}{file_suffix}"
    llm_postprocess._atomic_write_text(target, json.dumps(state, ensure_ascii=False, indent=2))


_RETENTION_DAYS = 7


def sweep_timeouts(pending_root: Path) -> int:
    """Помечает истекшие task_filter/task_deadlines как timed_out + чистит
    старые state'ы (>RETENTION_DAYS, ход 3 У9).

    Поведение по таймауту: «лучше шум, чем потеря» — для task_filter
    оставляем все задачи (filter в tasks.md уже произошёл); для
    task_deadlines дедлайны остаются `до —`.

    Retention: state'ы со status ∈ {resolved, timed_out} старше N дней
    удаляются — иначе папка растёт без ограничений (60+ за месяц на 10
    встречах/неделю).
    """
    n = 0
    now = time.time()
    retention_seconds = _RETENTION_DAYS * 24 * 3600
    for kind, suffix in (
        ("task_filter", _TASKS_FILE_SUFFIX),
        ("task_deadlines", _DEADLINES_FILE_SUFFIX),
    ):
        for state in _list_task_states(pending_root, kind=kind, status_filter=["pending"]):
            if clarify_state.is_past_deadline(state):
                state["status"] = "timed_out"
                state["resolved_at"] = clarify_state.now_iso()
                _write_state(state, pending_root, file_suffix=suffix)
                logger.info("[task-clarify] timed_out meeting=%s kind=%s",
                            state["meeting_id"], kind)
                n += 1
        # Retention: удаляем resolved/timed_out старше RETENTION_DAYS
        # по mtime файла.
        if not pending_root.exists():
            continue
        for f in pending_root.glob(f"*{suffix}"):
            if f.name.startswith("."):
                continue
            try:
                age = now - f.stat().st_mtime
            except OSError:
                continue
            if age < retention_seconds:
                continue
            try:
                state = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (state or {}).get("status") in ("resolved", "timed_out"):
                try:
                    f.unlink()
                    logger.info("[task-clarify] purged old state file=%s age=%.0fd", f.name, age / 86400)
                except OSError:
                    pass
    return n


def _find_state_by_callback(callback_data: str, pending_root: Path) -> Optional[tuple[dict, str]]:
    """Ищет state по `<prefix><mid_short>:...`. Возвращает (state, prefix) или None."""
    if not isinstance(callback_data, str):
        return None
    if callback_data.startswith(llm_postprocess.CLARIFY_TASK_FILTER_CALLBACK_PREFIX):
        prefix = llm_postprocess.CLARIFY_TASK_FILTER_CALLBACK_PREFIX
        kind = "task_filter"
        suffix = _TASKS_FILE_SUFFIX
    elif callback_data.startswith(llm_postprocess.CLARIFY_TASK_DEADLINES_CALLBACK_PREFIX):
        prefix = llm_postprocess.CLARIFY_TASK_DEADLINES_CALLBACK_PREFIX
        kind = "task_deadlines"
        suffix = _DEADLINES_FILE_SUFFIX
    else:
        return None
    body = callback_data[len(prefix):]
    parts = body.split(":", 1)
    if not parts or not parts[0]:
        return None
    mid_short = parts[0]
    for state in _list_task_states(pending_root, kind=kind):
        if llm_postprocess._short_id(state.get("meeting_id", "")) == mid_short:
            return state, suffix
    return None


def _is_authorized_sender(from_user: dict, allowed_chat_id: Optional[int]) -> bool:
    """Авторизация: только от Ильи (`TELEGRAM_NOTARIUS_CHAT_ID`)."""
    if allowed_chat_id is None:
        return True
    sender_id = from_user.get("id") if isinstance(from_user, dict) else None
    try:
        return int(sender_id) == int(allowed_chat_id)
    except (TypeError, ValueError):
        return False


def _allowed_chat_id() -> Optional[int]:
    raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def process_callback(callback_query: dict, pending_root: Path, bot_token: str) -> bool:
    """Возвращает True если callback относится к task-clarify (обработан).
    False — caller (listener) должен попробовать другой handler (спикеры).
    """
    data = callback_query.get("data") or ""
    if not (
        data.startswith(llm_postprocess.CLARIFY_TASK_FILTER_CALLBACK_PREFIX)
        or data.startswith(llm_postprocess.CLARIFY_TASK_DEADLINES_CALLBACK_PREFIX)
    ):
        return False

    cb_id = callback_query.get("id") or ""
    from_user = callback_query.get("from") or {}
    if not _is_authorized_sender(from_user, _allowed_chat_id()):
        logger.warning(
            "[task-clarify] callback from unauthorized user_id=%s — игнор",
            from_user.get("id"),
        )
        try:
            telegram_api.answer_callback_query(bot_token, cb_id, text="Not authorized.")
        except telegram_api.TelegramApiError:
            pass
        return True

    found = _find_state_by_callback(data, pending_root)
    if found is None:
        try:
            telegram_api.answer_callback_query(bot_token, cb_id, text="(уже обработано)")
        except telegram_api.TelegramApiError:
            pass
        return True
    state, suffix = found

    if state["kind"] == "task_filter":
        # Сейчас единственная кнопка — «keep» (оставить все). Действие: просто
        # пометить state как resolved (фильтр уже состоялся — задачи в tasks.md
        # были записаны после отправки этого сообщения, см. finalize-meeting.py).
        state["status"] = "resolved"
        state["resolved_via"] = "callback_keep"
        state["resolved_at"] = clarify_state.now_iso()
        _write_state(state, pending_root, file_suffix=suffix)
        logger.info(
            "[task-clarify] resolved meeting=%s type=task_filter applied=keep-all",
            state["meeting_id"],
        )
        try:
            telegram_api.answer_callback_query(bot_token, cb_id, text="✅ Оставил все")
        except telegram_api.TelegramApiError:
            pass
        try:
            telegram_api.edit_message_text(
                bot_token,
                chat_id=int(state["chat_id"]),
                message_id=int(state["message_id"]),
                text=(
                    f"✅ Оставил все {len(state.get('tasks', []))} задач. "
                    f"meeting: {state['meeting_id']}"
                ),
            )
        except telegram_api.TelegramApiError:
            pass
        return True

    # task_deadlines: callback'ов сейчас нет (только текст). Если кнопку
    # добавим в будущем — обработка тут.
    try:
        telegram_api.answer_callback_query(bot_token, cb_id, text="Ответь текстом")
    except telegram_api.TelegramApiError:
        pass
    return True


def process_text_message(msg: dict, pending_root: Path, bot_token: str) -> bool:
    """Если есть pending task_filter / task_deadlines — применяем.

    Возвращает True если text взят на себя task-clarify (listener выходит).
    False если не наш case (listener должен попробовать другой handler).
    """
    text = (msg.get("text") or "").strip()
    if not text:
        return False

    from_user = msg.get("from") or {}
    if not _is_authorized_sender(from_user, _allowed_chat_id()):
        return False  # listener сам разберётся с unauthorized
    chat = msg.get("chat") or {}
    chat_id = int(chat.get("id") or 0)

    # task_deadlines имеет приоритет: формат «N=...» уникален.
    deadline_states = _list_task_states(
        pending_root, kind="task_deadlines", status_filter=["pending", "timed_out"]
    )
    if deadline_states and re.search(r"\d+\s*[=:]\s*\S", text):
        if len(deadline_states) > 1:
            try:
                telegram_api.send_message(
                    bot_token, chat_id,
                    f"⚠️ У меня {len(deadline_states)} pending уточнений по дедлайнам. "
                    "Перечисли встречу одним сообщением — пока обработаю первое.",
                )
            except telegram_api.TelegramApiError:
                pass
        state = deadline_states[0]
        _apply_deadlines_resolution(state, text, pending_root, bot_token, chat_id)
        return True

    # task_filter: «оставить все» / «убрать N,M»
    filter_states = _list_task_states(
        pending_root, kind="task_filter", status_filter=["pending", "timed_out"]
    )
    if filter_states and re.search(
        r"(оставить\s+все|оставить\s+всё|убрать|удалить|выкинь|вычеркни|^\s*[\d,\s;]+\s*$)",
        text.lower(),
    ):
        if len(filter_states) > 1:
            try:
                telegram_api.send_message(
                    bot_token, chat_id,
                    f"⚠️ У меня {len(filter_states)} pending уточнений по фильтру задач — обработаю первое.",
                )
            except telegram_api.TelegramApiError:
                pass
        state = filter_states[0]
        _apply_filter_resolution(state, text, pending_root, bot_token, chat_id)
        return True

    return False


def _apply_filter_resolution(
    state: dict,
    text: str,
    pending_root: Path,
    bot_token: str,
    reply_chat_id: int,
) -> None:
    """Применяет «убрать N,M» к tasks.md (удаляет соответствующие строки)."""
    tasks = state.get("tasks") or []
    n = len(tasks)
    to_remove = llm_postprocess.parse_task_filter_answer(text, n)
    state["status"] = "resolved"
    state["resolved_via"] = "text"
    state["resolved_at"] = clarify_state.now_iso()
    state["applied_filter"] = to_remove

    if to_remove is None or len(to_remove) == 0:
        _write_state(state, pending_root, file_suffix=_TASKS_FILE_SUFFIX)
        try:
            telegram_api.send_message(bot_token, reply_chat_id, "✅ Оставил все.")
        except telegram_api.TelegramApiError:
            pass
        logger.info(
            "[task-clarify] resolved meeting=%s type=task_filter applied=keep-all",
            state["meeting_id"],
        )
        return

    # Удаляем строки этих задач из tasks.md (matching по тексту + контексту).
    tasks_md_path = _resolve_tasks_md_path()
    if not tasks_md_path.is_file():
        logger.warning("[task-clarify] tasks.md не найден для удаления: %s", tasks_md_path)
        _write_state(state, pending_root, file_suffix=_TASKS_FILE_SUFFIX)
        return

    series = (state.get("meta") or {}).get("series", "—")
    date = (state.get("meta") or {}).get("date", "—")
    ctx_needle = f"протокол {series} {date}".lower()

    # Под flock — те же гарантии, что у `_append_to_tasks_md` (ход 1 Н7).
    lock_path = tasks_md_path.with_suffix(tasks_md_path.suffix + ".lock")
    try:
        lock_fh = open(lock_path, "a")
    except OSError:
        lock_fh = None
    locked = False
    if lock_fh is not None:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError:
            pass
    try:
        try:
            raw = tasks_md_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("[task-clarify] read tasks.md failed: %s", e)
            _write_state(state, pending_root, file_suffix=_TASKS_FILE_SUFFIX)
            return

        lines = raw.split("\n")
        removed = 0
        for idx_1 in to_remove:
            if idx_1 < 1 or idx_1 > n:
                continue
            text_needle = (tasks[idx_1 - 1].get("text") or "")[:30].lower()
            for i, ln in enumerate(lines):
                ln_low = ln.lower()
                if text_needle and text_needle in ln_low and ctx_needle in ln_low:
                    lines[i] = None  # type: ignore[assignment]
                    removed += 1
                    break

        lines = [ln for ln in lines if ln is not None]
        llm_postprocess._atomic_write_text(tasks_md_path, "\n".join(lines))
        _write_state(state, pending_root, file_suffix=_TASKS_FILE_SUFFIX)
    finally:
        if lock_fh is not None:
            try:
                if locked:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                try:
                    lock_fh.close()
                except OSError:
                    pass
    logger.info(
        "[task-clarify] resolved meeting=%s type=task_filter removed=%d/%d",
        state["meeting_id"], removed, len(to_remove),
    )
    try:
        telegram_api.send_message(
            bot_token, reply_chat_id,
            f"✅ Убрал {removed} из {len(to_remove)} запрошенных задач из tasks.md.",
        )
    except telegram_api.TelegramApiError:
        pass


def _apply_deadlines_resolution(
    state: dict,
    text: str,
    pending_root: Path,
    bot_token: str,
    reply_chat_id: int,
) -> None:
    tasks = state.get("tasks") or []
    meeting_date = (state.get("meta") or {}).get("date") or "1970-01-01"
    deadlines = llm_postprocess.parse_task_deadlines_answer(
        text, meeting_date=meeting_date, n_tasks=len(tasks),
    )
    state["status"] = "resolved"
    state["resolved_via"] = "text"
    state["resolved_at"] = clarify_state.now_iso()
    state["applied_deadlines"] = {str(k): v for k, v in deadlines.items()}
    _write_state(state, pending_root, file_suffix=_DEADLINES_FILE_SUFFIX)

    if not deadlines:
        try:
            telegram_api.send_message(
                bot_token, reply_chat_id,
                "Не распознал. Формат: «1=2026-06-05, 2=на этой неделе, 3=без срока».",
            )
        except telegram_api.TelegramApiError:
            pass
        logger.info(
            "[task-clarify] resolved meeting=%s type=task_deadlines applied=0 (parse-empty)",
            state["meeting_id"],
        )
        return

    tasks_md_path = _resolve_tasks_md_path()
    task_texts = [(t.get("text") or "") for t in tasks]
    applied = llm_postprocess.apply_deadlines_to_tasks_md(
        tasks_md_path,
        meeting_meta=state.get("meta") or {},
        task_texts=task_texts,
        deadlines=deadlines,
    )
    logger.info(
        "[task-clarify] resolved meeting=%s type=task_deadlines applied=%d/%d",
        state["meeting_id"], applied, len(deadlines),
    )
    try:
        telegram_api.send_message(
            bot_token, reply_chat_id,
            f"✅ Применил {applied} из {len(deadlines)} дат в tasks.md.",
        )
    except telegram_api.TelegramApiError:
        pass


def _resolve_tasks_md_path() -> Path:
    env = os.environ.get("MEETING_NOTARY_TASKS_MD")
    if env:
        return Path(os.path.expanduser(env))
    me_dir = os.environ.get("ME_DIR") or os.path.expanduser("~/Projects/me")
    return Path(me_dir) / "tasks.md"
