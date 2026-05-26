"""Shared YAML registry helpers for meeting-notary CLIs.

Atomic write via tmp + rename; preserves trailing newline on read.
"""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

DEFAULT_REGISTRY_DIR = Path(os.path.expanduser("~/Projects/me/встречи"))
ROOMS_FILE = DEFAULT_REGISTRY_DIR / "rooms.yaml"
WATCHED_FILE = DEFAULT_REGISTRY_DIR / "watched.yaml"
PAUSE_FILE = DEFAULT_REGISTRY_DIR / ".pause-until"

ROOM_REF_RE = re.compile(r"^@([a-z0-9][a-z0-9_-]*)$")
TELEMOST_HOST = "telemost.yandex.ru"


def _extract_header_comment(path: Path) -> str:
    """Return leading `#`-comment lines + first blank line group as header. Stops at YAML data."""
    if not path.exists():
        return ""
    lines: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.lstrip()
            if stripped.startswith("#") or stripped == "" or stripped == "\n":
                lines.append(raw)
                continue
            break
    return "".join(lines)


_FD_LOCKS: dict[str, Any] = {}


def _acquire_lock(path: Path) -> Any:
    """Acquire exclusive flock on path-specific lock file. Held until release_lock()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / (f".{path.name}.lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    _FD_LOCKS[str(path)] = fd
    return fd


def _release_lock(path: Path) -> None:
    fd = _FD_LOCKS.pop(str(path), None)
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_yaml(path: Path, default_top_key: str) -> dict[str, Any]:
    if not path.exists():
        return {default_top_key: []}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: ожидался словарь верхнего уровня, получено {type(data).__name__}")
    if default_top_key not in data or data[default_top_key] is None:
        data[default_top_key] = []
    return data


def _atomic_write_yaml(path: Path, data: dict[str, Any], default_header: str) -> None:
    """Write YAML atomically. Preserve existing leading comment block; fall back to default.

    Also keeps a single-step backup of the previous file at <path>.bak — protection
    against accidental rm or a malformed save (the YAML files live in ~/Projects/me/,
    which is not a git repo; no other history exists).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_header = _extract_header_comment(path)
    header = existing_header if existing_header.strip() else default_header
    if not header.endswith("\n"):
        header += "\n"
    if not header.endswith("\n\n"):
        header += "\n"

    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass

    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(header)
            yaml.dump(
                data,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=120,
            )
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


ROOMS_HEADER = """# Справочник постоянных переговорок (Telemost-комнат).
#
# Наполнен один раз; редко меняется. Используется в watched.yaml
# через ссылки вида `@<name>`. Редактируй через `meeting-rooms add/remove`.
#
# Поля:
#   name         — короткое имя (латиницей)
#   url          — полный URL Telemost
#   description  — пояснение (опционально)
"""

WATCHED_HEADER = """# Реестр отслеживаемых встреч для бота-нотариуса.
#
# Редактируй через `meeting-watch add/remove/disable/enable`.
# Полная схема полей — в шапке исходного шаблона watched.yaml.
"""


def load_rooms(lock: bool = False) -> dict[str, Any]:
    if lock:
        _acquire_lock(ROOMS_FILE)
    return _read_yaml(ROOMS_FILE, "rooms")


def save_rooms(data: dict[str, Any]) -> None:
    try:
        _atomic_write_yaml(ROOMS_FILE, data, ROOMS_HEADER)
    finally:
        _release_lock(ROOMS_FILE)


def release_rooms_lock() -> None:
    """Release rooms lock explicitly (call in finally when save_rooms wasn't reached)."""
    _release_lock(ROOMS_FILE)


def load_watched(lock: bool = False) -> dict[str, Any]:
    if lock:
        _acquire_lock(WATCHED_FILE)
    return _read_yaml(WATCHED_FILE, "watched")


def save_watched(data: dict[str, Any]) -> None:
    try:
        _atomic_write_yaml(WATCHED_FILE, data, WATCHED_HEADER)
    finally:
        _release_lock(WATCHED_FILE)


def release_watched_lock() -> None:
    """Release watched lock explicitly (call in finally when save_watched wasn't reached)."""
    _release_lock(WATCHED_FILE)


def find_room(name: str, rooms: dict[str, Any]) -> dict[str, Any] | None:
    for r in rooms.get("rooms", []):
        if r.get("name") == name:
            return r
    return None


def resolve_room_to_url(value: str | None, rooms: dict[str, Any]) -> str | None:
    """Resolve `@name` reference or pass through direct URL. Returns None for empty."""
    if not value:
        return None
    value = value.strip()
    m = ROOM_REF_RE.match(value)
    if m:
        room = find_room(m.group(1), rooms)
        if not room:
            raise SystemExit(f"Комната @{m.group(1)} не найдена в rooms.yaml")
        return room.get("url")
    if value.startswith("https://"):
        return value
    raise SystemExit(f"Не распознан room: {value!r} (ожидается @<name> или https://...)")


def find_watched(meeting_id: str, watched: dict[str, Any]) -> dict[str, Any] | None:
    for w in watched.get("watched", []):
        if w.get("id") == meeting_id:
            return w
    return None


def validate_room_record(rec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    name = rec.get("name", "")
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", name):
        errors.append(f"name='{name}': должно быть kebab-case латиницей")
    url = rec.get("url", "")
    if not (url.startswith("https://") and TELEMOST_HOST in url):
        errors.append(f"url='{url}': ожидается https://...{TELEMOST_HOST}/...")
    return errors


VALID_TYPES = {"google-calendar", "manual", "one-off"}


def validate_watched_record(rec: dict[str, Any], rooms: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    mid = rec.get("id", "")
    if not re.match(r"^[a-z0-9][a-z0-9_.-]*$", mid):
        errors.append(f"id='{mid}': должно быть kebab-case латиницей/цифрами")
    series = rec.get("series", "")
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", series):
        errors.append(f"series='{series}': должно быть kebab-case латиницей")
    tp = rec.get("type", "")
    if tp not in VALID_TYPES:
        errors.append(f"type='{tp}': допустимо {sorted(VALID_TYPES)}")
    if tp == "google-calendar":
        if not rec.get("calendar_id"):
            errors.append("google-calendar: пустой calendar_id")
        if not (rec.get("recurring_event_id") or rec.get("event_id")):
            errors.append("google-calendar: нужен recurring_event_id или event_id")
    elif tp == "manual":
        if not rec.get("cron"):
            errors.append("manual: пустой cron")
        if not rec.get("tz"):
            errors.append("manual: пустой tz")
    elif tp == "one-off":
        if not rec.get("datetime"):
            errors.append("one-off: пустой datetime")
    room = rec.get("room")
    if room:
        try:
            resolve_room_to_url(room, rooms)
        except SystemExit as e:
            errors.append(str(e))
    return errors


def pause_until() -> str | None:
    """Return ISO datetime if pause is active and not yet expired; else None (and clean stale file)."""
    if not PAUSE_FILE.exists():
        return None
    raw = PAUSE_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until <= datetime.now(timezone.utc):
        try:
            PAUSE_FILE.unlink()
        except FileNotFoundError:
            pass
        return None
    return raw


def err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def ok(msg: str) -> None:
    sys.stdout.write(msg + "\n")
