"""Персистенция состояния сбора правок в чате (фича «правки реплаем», Ф3).

Один файл = одна встреча-в-чате: `<root>/<feedback_id>-feedback.json`, где
`feedback_id = fb-<series>-<date>-<chat_id>`. Атомарная запись через
`tempfile.mkstemp + os.rename` (как `clarify_state`), чтобы listener не прочитал
полузаписанный JSON.

Каталог СВОЙ (не `_pending_clarification/`) — env `MEETING_NOTARY_FEEDBACK_DIR`,
дефолт `/opt/meeting-notary/_feedback_edits` (VPS) или мак-путь. Изоляция
намеренная: clarify/delivery sweep'ы глобают `_pending_clarification/` и не должны
видеть наши state'ы (и наоборот).

Состояние:
    {
      "feedback_id":   "fb-coord-2026-06-02--1001234",
      "series":        "coord",
      "date":          "2026-06-02",
      "chat_id":       -1001234,
      "meta_path":     "/abs/path/<series>/meta.json",   # где лежит meta.delivered (для Ф4)
      "protocol_message_ids": [101, 102],   # message_id доставленного протокола, на который ответили
      "round":         1,                   # номер раунда (растёт при перевыпуске → новый сбор, FB12)
      "window_min":    20,                  # FEEDBACK_WINDOW_MIN на момент старта окна
      "max_window_min": 120,                # FEEDBACK_MAX_WINDOW_MIN (потолок)
      "window_started_at": "2026-06-02T12:00:00Z",   # = время ПЕРВОЙ правки раунда
      "last_edit_at":      "2026-06-02T12:05:00Z",
      "deadline_at":       "2026-06-02T12:25:00Z",   # min(last_edit+window, window_started+max)
      "hard_deadline_at":  "2026-06-02T14:00:00Z",   # window_started + max (потолок)
      "edits": [
        {
          "edit_id":            "e-555",
          "tg_message_id":      555,           # message_id самой правки (дедуп повторной доставки)
          "reply_to_message_id": 101,          # на какое сообщение протокола ответили
          "from_user_id":       777,
          "author":             "Михаил Саргин",
          "text":               "131 не под досмотром, а на доставке",
          "at":                 "2026-06-02T12:00:00Z"
        }, ...
      ],
      "status": "collecting" | "ready_for_reissue" | "dormant",
      "created_at": "...",
      "updated_at": "..."
    }

Статусы (контракт Ф3→Ф4):
  collecting        — окно открыто, копим правки. Когда `now >= deadline_at`
                      (естественный дебаунс или потолок) — sweep переводит в
                      `ready_for_reissue`.
  ready_for_reissue — окно закрылось; Ф4 должна забрать `edits`, пересобрать
                      протокол, дописать новый message_id в meta.delivered и
                      перевести state в `dormant`. В Ф3 (без Ф4) state остаётся
                      `ready_for_reissue` до следующего reply (тогда новый раунд).
  reissuing         — Ф4 «забрала» state на перевыпуск (claim, см.
                      `claim_for_reissue`). Промежуточный статус: пока он стоит,
                      конкурентный reply НЕ дозаписывается в съедаемые `edits`, а
                      открывает СЛЕДУЮЩИЙ раунд (Н1, FM-10 — защита от потери
                      правок в окне между закрытием окна и перевыпуском).
  dormant           — спит после перевыпуска; новый reply на любую версию серии
                      открывает новый раунд (FB12, многораундовость).
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

DEFAULT_LOCAL_ROOT = os.path.expanduser("~/Projects/meeting-notary/_feedback_edits")
DEFAULT_VPS_ROOT = "/opt/meeting-notary/_feedback_edits"

STATE_SUFFIX = "-feedback.json"

VALID_STATUSES = ("collecting", "ready_for_reissue", "reissuing", "dormant")

# Н1 (FM-10): потолок попыток перевыпуска одного раунда. После него
# `process_ready_reissues` перестаёт клеймить state (claude/telegram стабильно
# падают) — оставляем `ready_for_reissue` владельцу/Ф9, не крутим claude в холостую.
# Новый reply всё равно откроет свежий раунд (apply_edit: ready_for_reissue → round+1).
MAX_REISSUE_ATTEMPTS = 3

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
_FEEDBACK_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def resolve_feedback_dir(*, override: Optional[str] = None) -> Path:
    """Корень `_feedback_edits/`.

    Приоритет: явный аргумент > env `MEETING_NOTARY_FEEDBACK_DIR` >
    дефолт (VPS если `/opt/meeting-notary` существует, иначе мак-путь).
    """
    if override:
        return Path(os.path.expanduser(override))
    env = os.environ.get("MEETING_NOTARY_FEEDBACK_DIR")
    if env:
        return Path(os.path.expanduser(env))
    if Path("/opt/meeting-notary").is_dir():
        return Path(DEFAULT_VPS_ROOT)
    return Path(DEFAULT_LOCAL_ROOT)


def _sanitize(part: str) -> str:
    """Любой символ вне `[A-Za-z0-9._-]` → `_`. Серия может быть кириллицей/с пробелами."""
    return _SAFE_ID_RE.sub("_", str(part or "").strip()) or "x"


def build_feedback_id(series: Optional[str], date: Optional[str], chat_id: Any) -> str:
    """Детерминированный id встречи-в-чате. Один и тот же для всех раундов (FB12)."""
    return f"fb-{_sanitize(series)}-{_sanitize(date)}-{_sanitize(str(chat_id))}"


def _validate_feedback_id(feedback_id: str) -> str:
    """feedback_id попадает в имя файла. Защита от `/`, `..`, NUL."""
    if not isinstance(feedback_id, str) or not feedback_id:
        raise ValueError("feedback_id must be a non-empty string")
    if not _FEEDBACK_ID_RE.match(feedback_id):
        raise ValueError(
            "feedback_id contains invalid characters (allowed: A-Z a-z 0-9 . _ -): %r" % feedback_id
        )
    if feedback_id in (".", "..") or "/" in feedback_id or "\\" in feedback_id:
        raise ValueError("feedback_id path traversal blocked: %r" % feedback_id)
    return feedback_id


def path_for(feedback_id: str, *, root: Optional[Path] = None) -> Path:
    """Полный путь к файлу состояния (без создания директории)."""
    root = root or resolve_feedback_dir()
    return root / f"{_validate_feedback_id(feedback_id)}{STATE_SUFFIX}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_state(state: dict, *, root: Optional[Path] = None) -> Path:
    """Атомарно записывает state-файл (`tempfile + os.rename`, fsync). Возвращает Path."""
    feedback_id = _validate_feedback_id(state.get("feedback_id", ""))
    root = root or resolve_feedback_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{feedback_id}{STATE_SUFFIX}"
    state["updated_at"] = now_iso()

    fd, tmp = tempfile.mkstemp(prefix=f".{feedback_id}.", suffix=".json.tmp", dir=str(root))
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


def read_state(feedback_id: str, *, root: Optional[Path] = None) -> Optional[dict]:
    """Читает state или None если файла нет / битый JSON."""
    p = path_for(feedback_id, root=root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[feedback-state] read %s failed: %s", p, e)
        return None


def list_states(
    *, root: Optional[Path] = None, status_filter: Optional[Iterable[str]] = None
) -> list[dict]:
    """Сканирует папку, возвращает state'ы со статусом из `status_filter` (None → все)."""
    root = root or resolve_feedback_dir()
    if not root.exists():
        return []
    out: list[dict] = []
    allowed = set(status_filter) if status_filter else None
    for f in root.glob(f"*{STATE_SUFFIX}"):
        if f.name.startswith("."):  # скрытые tempfile'ы
            continue
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[feedback-state] skip malformed %s: %s", f.name, e)
            continue
        if not isinstance(state, dict):
            continue
        if allowed is not None and state.get("status") not in allowed:
            continue
        out.append(state)
    return out


def mark_status(
    feedback_id: str,
    new_status: str,
    *,
    root: Optional[Path] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Optional[dict]:
    """Обновляет статус (атомарно). Возвращает новый state или None если файла нет."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {new_status!r}")
    state = read_state(feedback_id, root=root)
    if state is None:
        return None
    state["status"] = new_status
    if extra:
        state.update(extra)
    write_state(state, root=root)
    return state


def claim_for_reissue(feedback_id: str, *, root: Optional[Path] = None) -> Optional[dict]:
    """Н1 (FM-10): атомарно «забирает» state на перевыпуск ДО чтения `edits`.

    `ready_for_reissue` → `reissuing` одной атомарной записью (mkstemp+rename).
    Возвращает claimed-state (со статусом `reissuing`) если claim удался, иначе
    None (статус уже не `ready_for_reissue` — кто-то перевёл его раньше, либо
    конкурентный reply открыл новый раунд).

    Зачем claim ПЕРЕД чтением edits: пока стоит `reissuing`, `apply_edit`
    трактует входящий reply как НОВЫЙ раунд (а не дозапись в съедаемые edits) —
    правки текущего раунда уходят в перевыпуск, правки конкурентного reply'я — в
    следующий раунд. Без claim reply в окне между `ready_for_reissue` и
    перевыпуском затирал бы несъеденные edits (`_new_round_state`).

    Модель конкуренции: единственный писатель — listener (один процесс); атомарность
    обеспечивает rename в `write_state` (как и весь остальной feedback_state).
    """
    state = read_state(feedback_id, root=root)
    if state is None or state.get("status") != "ready_for_reissue":
        return None
    state["status"] = "reissuing"
    state["reissue_claimed_at"] = now_iso()
    write_state(state, root=root)
    return state


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """ISO-метка (в т.ч. '...Z') → tz-aware datetime (UTC); None если пусто/битое."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cleanup_dormant_states(
    *,
    root: Optional[Path] = None,
    max_age_days: int = 30,
    now: Optional[datetime] = None,
) -> int:
    """R10 (Ф8): удаляет служебные state-файлы `*-feedback.json` в статусе
    `dormant` старше `max_age_days` (по `updated_at`). Возвращает число удалённых.

    Скоуп жёстко ограничен: путь — ТОЛЬКО `resolve_feedback_dir()` (или переданный
    `root`), удаляем ТОЛЬКО файлы с суффиксом `STATE_SUFFIX`. Транскрипты и протоколы
    в `~/Projects/me/встречи/` по построению недостижимы. Не-`dormant` и свежие
    (моложе порога, либо без/битым `updated_at`) — не трогаем. Скрытые tempfile'ы
    (имя на `.`) пропускаем.

    R9: логируем только число удалённых — без имён участников/текста.
    """
    root = root or resolve_feedback_dir()
    if not root.exists():
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff_sec = max(0, int(max_age_days)) * 86400
    removed = 0
    for f in root.glob(f"*{STATE_SUFFIX}"):
        if f.name.startswith("."):  # скрытые tempfile'ы — не трогаем
            continue
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[feedback-state] cleanup skip malformed %s: %s", f.name, e)
            continue
        if not isinstance(state, dict) or state.get("status") != "dormant":
            continue
        updated = _parse_iso(state.get("updated_at"))
        if updated is None:  # без валидной метки — консервативно не удаляем
            continue
        if (now - updated).total_seconds() < cutoff_sec:
            continue  # свежий — оставляем
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            logger.warning("[feedback-state] cleanup unlink failed %s: %s", f.name, e)
    if removed:
        logger.info("[feedback-state] cleanup_dormant_states: удалено %d (старше %dд)",
                    removed, max_age_days)
    return removed


def is_window_expired(state: dict, *, now: Optional[datetime] = None) -> bool:
    """True если окно сбора закрылось (`now >= deadline_at`). `now` инъектируется в тестах."""
    deadline = _parse_iso(state.get("deadline_at"))
    if deadline is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= deadline
