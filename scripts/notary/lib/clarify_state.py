"""Персистенция состояния clarify-flow (`_pending_clarification/`).

Один файл = одна встреча: `<root>/<meeting_id>.json`. Атомарная запись через
`tempfile + os.rename`, чтобы worker не прочитал полузаписанный JSON.

Состояние:
    {
      "meeting_id":      "auto-tm-…",
      "transcript_path": "/abs/path/<series>/<date>.md",
      "meta": {                    # подмножество meta.json (для аудита/правок)
        "series": "...",
        "date":   "YYYY-MM-DD",
        "sessionUid": "..."
      },
      "unclear_clusters": {
        "SPEAKER_02": {
          "name_options": ["Илья Рыбалка", "Михаил Саргин", ...],
          "samples":      ["[00:05] первая реплика...", "[01:23] вторая..."],
          "speaker_label_in_md": "Спикер 3"      # «Спикер N», как написано в .md
        }
      },
      "name_pool":       ["Илья Рыбалка", "Михаил Саргин", ...],
      "chat_id":         359008340,
      "message_id":      12345,
      "sent_at":         "2026-05-28T13:00:00Z",
      "timeout_s":       420,
      "deadline_at":     "2026-05-28T13:07:00Z",
      "status":          "pending" | "resolved" | "timed_out",
      "resolved_via":    "callback" | "text" | null,
      "resolved_at":     "2026-05-28T13:02:30Z" | null,
      "applied_mapping": {"SPEAKER_02": "Дарья Набережная"} | null
    }

Гейт `status` (важно для late-answer):
  pending     — окно ещё открыто, на ответ применяем + переразмечаем + отправляем
                в группу (в Ф6 этой логики ещё нет — пока только перезапись на диске).
  resolved    — Илья уже ответил, повторные callback'и игнорируем.
  timed_out   — таймаут прошёл; поздний ответ всё ещё применяет mapping
                и переписывает транскрипт на диске, **в группу повторно не шлёт**.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


logger = logging.getLogger(__name__)

DEFAULT_LOCAL_ROOT = os.path.expanduser("~/Projects/meeting-notary/_pending_clarification")
DEFAULT_VPS_ROOT = "/opt/meeting-notary/_pending_clarification"

_MEETING_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def resolve_pending_dir(*, override: Optional[str] = None) -> Path:
    """Возвращает корень `_pending_clarification/`.

    Приоритет: явный аргумент > env `MEETING_NOTARY_PENDING_DIR` >
    дефолт (VPS если `/opt/meeting-notary` существует, иначе мак-путь).
    """
    if override:
        return Path(os.path.expanduser(override))
    env = os.environ.get("MEETING_NOTARY_PENDING_DIR")
    if env:
        return Path(os.path.expanduser(env))
    # Если бежим на VPS (LOCAL_FINALIZE=0 в проде), `/opt/meeting-notary` есть.
    if Path("/opt/meeting-notary").is_dir():
        return Path(DEFAULT_VPS_ROOT)
    return Path(DEFAULT_LOCAL_ROOT)


def _validate_meeting_id(meeting_id: str) -> str:
    """meeting_id попадает в имя файла. Защита от `/`, `..`, NUL."""
    if not isinstance(meeting_id, str) or not meeting_id:
        raise ValueError("meeting_id must be a non-empty string")
    if not _MEETING_ID_RE.match(meeting_id):
        raise ValueError(
            "meeting_id contains invalid characters (allowed: A-Z a-z 0-9 . _ -): %r" % meeting_id
        )
    if meeting_id in (".", "..") or "/" in meeting_id or "\\" in meeting_id:
        raise ValueError("meeting_id path traversal blocked: %r" % meeting_id)
    return meeting_id


def path_for(meeting_id: str, *, root: Optional[Path] = None) -> Path:
    """Полный путь к файлу состояния (без создания директории)."""
    root = root or resolve_pending_dir()
    return root / f"{_validate_meeting_id(meeting_id)}.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_state(state: dict, *, root: Optional[Path] = None) -> Path:
    """Атомарно записывает state-файл. Возвращает финальный Path.

    Атомарность: `tempfile.mkstemp(dir=<тот же фс>)` → fsync → `os.rename`.
    `os.rename` на POSIX атомарен в рамках одной файловой системы.
    """
    meeting_id = _validate_meeting_id(state.get("meeting_id", ""))
    root = root or resolve_pending_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{meeting_id}.json"

    fd, tmp = tempfile.mkstemp(prefix=f".{meeting_id}.", suffix=".json.tmp", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, target)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return target


def read_state(meeting_id: str, *, root: Optional[Path] = None) -> Optional[dict]:
    """Читает state или None если файла нет / битый JSON."""
    p = path_for(meeting_id, root=root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[clarify-state] read %s failed: %s", p, e)
        return None


def list_pending(*, root: Optional[Path] = None, status_filter: Optional[Iterable[str]] = None) -> list[dict]:
    """Сканирует папку, возвращает state'ы со статусом из `status_filter`.

    `status_filter=None` → возвращает все валидные state'ы.
    """
    root = root or resolve_pending_dir()
    if not root.exists():
        return []
    out: list[dict] = []
    allowed = set(status_filter) if status_filter else None
    for f in root.glob("*.json"):
        # Скрытые tempfile'ы `.{id}.…tmp` пропускаем.
        if f.name.startswith("."):
            continue
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[clarify-state] skip malformed %s: %s", f.name, e)
            continue
        if not isinstance(state, dict):
            continue
        if allowed is not None and state.get("status") not in allowed:
            continue
        out.append(state)
    return out


def mark_status(
    meeting_id: str,
    new_status: str,
    *,
    root: Optional[Path] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Optional[dict]:
    """Обновляет статус (атомарно). Возвращает новый state или None если файла нет."""
    if new_status not in ("pending", "resolved", "timed_out"):
        raise ValueError(f"unknown status: {new_status!r}")
    state = read_state(meeting_id, root=root)
    if state is None:
        return None
    state["status"] = new_status
    if extra:
        state.update(extra)
    write_state(state, root=root)
    return state


def is_past_deadline(state: dict) -> bool:
    """Проверка таймаута для воркер'а. Сравнение по UTC."""
    deadline = state.get("deadline_at")
    if not deadline:
        return False
    try:
        # Поддерживаем '...Z' формат.
        ts = deadline.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= dt
