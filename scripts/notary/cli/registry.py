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

# pyyaml импортируется ЛЕНИВО внутри `_read_yaml`/`_atomic_write_yaml` (контракт
# Ф4 §1.2): системный python3 юнит-тестов pyyaml не несёт, а чистые резолверы
# разметки (`get_company_for_series`/`get_visibility_for_series`) и валидатор —
# stdlib-only и должны импортироваться/тестироваться без pyyaml. Прод-venv-cli/VPS
# несут pyyaml; чтение/запись YAML без него осознанно падает (ImportError) у
# caller'ов, которые это уже ловят best-effort (series_markup, llm_postprocess).

def _resolve_registry_dir() -> Path:
    """Каталог реестра встреч (rooms.yaml/watched.yaml).

    Приоритет: env `MEETING_NOTARY_REGISTRY_DIR` → канон-путь VPS
    `/srv/meeting-notary/registry` (ТОЛЬКО если существует) → домашний дефолт
    `~/Projects/me/встречи` (мак).

    VPS-фолбэк (фикс бага доставки 2026-06-15): дочерний процесс финализации/
    доставки может потерять env (спавн без EnvironmentFile/source) → без env
    домашний дефолт `~/Projects/me/встречи` на VPS НЕ существует → реестр читается
    пустым → привязка серии к группе не находится → протокол молча уходит в личку
    (`deliver_protocol` fallback `TELEGRAM_NOTARIUS_CHAT_ID`). Фолбэк на
    существующий `/srv/meeting-notary/registry` делает резолв устойчивым к потере
    env, НЕ ломая мак (там `/srv/...` нет → домашний дефолт; env по-прежнему
    главнее всего — фикстуры тестов и явная конфигурация выигрывают)."""
    env = os.environ.get("MEETING_NOTARY_REGISTRY_DIR")
    if env and env.strip():
        return Path(os.path.expanduser(env.strip()))
    vps_canonical = Path("/srv/meeting-notary/registry")
    if vps_canonical.is_dir():
        return vps_canonical
    return Path(os.path.expanduser("~/Projects/me/встречи"))


DEFAULT_REGISTRY_DIR = _resolve_registry_dir()
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
    import yaml  # lazy — см. шапку модуля (контракт §1.2)
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
    import yaml  # lazy — см. шапку модуля (контракт §1.2)
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
#
# Ф6 (E6): опциональная разметка серии (пер-record, резолв на уровень series):
#   company:    anzhee | mpfirst   — какой *-context кормит серию (глоссарий/ростер).
#   visibility: company | private  — можно ли производное ЗНАНИЕ публиковать в
#               общий мозг компании. ОТСУТСТВИЕ visibility = неразмечено → бот
#               трактует серию как private (fail-closed): знание НЕ уходит в
#               *-context (E3). 1-на-1 — всегда private. Сырьё (транскрипт/
#               протокол) этим полям НЕ подчиняется — оно всегда в me/встречи.
#
# Ф5 (G4): опциональный жанр/повестка серии — задаёт ФОКУС протокола:
#   genre: директорат | координация | продукт | 1-на-1 | oneoff
#               директорат — решения/риски/цифры; координация — кто-что-когда;
#               продукт — решения/гипотезы; 1-на-1 — личные договорённости;
#               oneoff — разовая самодостаточная встреча. Проставляй ключевым
#               сериям вручную; ОТСУТСТВИЕ genre → бот берёт мягкий дефолт
#               (координация), НЕ форсируя.
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

# Ф6 (E6): разметка серии поверх watched.yaml — «компания» + «видимость».
# company   — какой `*-context` кормит серию (anzhee → anzhee-context, ...).
# visibility— можно ли производное ЗНАНИЕ публиковать в общий мозг компании:
#             `company` (групповая координация — можно) | `private` (1-на-1/
#             чувствительное — нельзя). ОТСУТСТВИЕ поля = неразмечено → бот
#             трактует как private (fail-closed, см. lib/publication_gate.py).
# Оба поля ОПЦИОНАЛЬНЫ и пер-record (резолв на уровень series — как
# telegram_chat_id: первое непустое среди записей серии). Сырьё (транскрипт/
# протокол) этим полям не подчиняется — оно всегда остаётся в me/встречи (РИСК1).
VALID_COMPANIES = {"anzhee", "mpfirst"}
VALID_VISIBILITY = {"company", "private"}

# Ф5 (G4): жанр/повестка серии — задаёт ФОКУС протокола генерации (директорат →
# решения/риски/цифры; координация → кто-что-когда; продукт → решения/гипотезы;
# 1-на-1 → личные договорённости; oneoff → разовая самодостаточная встреча).
# Опционально, пер-record (резолв на уровень series, как company). Владелец
# проставляет ключевым сериям вручную (A4); неразмеченным вызыватель
# (`lib/series_markup.genre_for_series`) даёт мягкий дефолт, НЕ форсируя. Значения
# нормализуются casefold-lower; невалидное в watched.yaml пропускается как
# отсутствие (резолв устойчив к ручному мусору).
VALID_GENRES = {"директорат", "координация", "продукт", "1-на-1", "oneoff"}


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
    # Ф6: telegram_chat_id — опционально, int (отрицательный для группы).
    raw_chat = rec.get("telegram_chat_id")
    if raw_chat is not None:
        if isinstance(raw_chat, bool) or not isinstance(raw_chat, int):
            errors.append(f"telegram_chat_id={raw_chat!r}: должно быть целым числом (отрицательное для группы)")
    # Ф6 (E6): company/visibility — опциональны, валидируем значения если заданы.
    company = rec.get("company")
    if company is not None:
        if not isinstance(company, str) or company.strip().lower() not in VALID_COMPANIES:
            errors.append(f"company={company!r}: допустимо {sorted(VALID_COMPANIES)} или отсутствие")
    visibility = rec.get("visibility")
    if visibility is not None:
        if not isinstance(visibility, str) or visibility.strip().lower() not in VALID_VISIBILITY:
            errors.append(f"visibility={visibility!r}: допустимо {sorted(VALID_VISIBILITY)} или отсутствие")
    # Ф5 (G4): genre — опционален, валидируем значение если задано.
    genre = rec.get("genre")
    if genre is not None:
        if not isinstance(genre, str) or genre.strip().casefold() not in VALID_GENRES:
            errors.append(f"genre={genre!r}: допустимо {sorted(VALID_GENRES)} или отсутствие")
    return errors


# ----- Ф6: helper'ы для telegram_chat_id привязок -----


def normalize_series_key(series: Any) -> str:
    """Каноничный ключ серии для устойчивого сравнения (Ф3 ISS-7, РИСК4).

    Корень ISS-7: и чтение (`get_telegram_chat_id_for_series`), и запись
    (`set_telegram_chat_id_for_series`) матчили серию ТОЧНЫМ `==` по строке.
    Любой дрейф ключа (регистр, пробелы, кавычки вокруг человекочитаемого
    имени) → 0 совпадений → привязка молча теряется → протокол уходит в личку.
    Нормализация: срез пробелов и обрамляющих кавычек + casefold (юникод-aware
    нижний регистр). Разделители slug (`-`/`_`) НЕ схлопываем — это рискованно
    (две разные серии могли бы совпасть); межсловарный случай slug↔имя
    решается отдельно через `resolve_series_ref` + display-маппинг.
    """
    if not isinstance(series, str):
        return ""
    return series.strip().strip("«»\"'").strip().casefold()


def get_telegram_chat_id_for_series(series: str, watched: dict[str, Any]) -> int | None:
    """Возвращает `telegram_chat_id` для series из watched.yaml или None.

    РИСК4 (ISS-7): сначала ТОЧНОЕ совпадение строки (исторический контракт —
    если есть точная запись, берём её), затем — нормализованное (`casefold` +
    срез пробелов/кавычек), чтобы рассинхрон регистра/пробелов в `meta.series`
    vs `watched.yaml` НЕ ронял привязку молча в личку. Если несколько записей
    одной series имеют разные chat_id — берётся первый (аномалия; одна привязка
    на series — инвариант писателя).
    """
    if not series:
        return None
    norm = normalize_series_key(series)
    fallback: int | None = None  # нормализованное совпадение, если точного нет
    for w in watched.get("watched", []):
        cid = w.get("telegram_chat_id")
        if not (isinstance(cid, int) and not isinstance(cid, bool)):
            continue
        ws = w.get("series")
        if ws == series:
            return cid  # точное совпадение приоритетно
        if fallback is None and ws is not None and normalize_series_key(ws) == norm:
            fallback = cid
    return fallback


def find_series_bindings(watched: dict[str, Any]) -> list[tuple[Any, int]]:
    """Все пары `(series, telegram_chat_id)` из watched.yaml, где привязка задана.

    Чистая функция для диагностики silent-fallthrough (ISS-7/REQ 7.3): если
    `get_telegram_chat_id_for_series` вернул None, но привязки в реестре ЕСТЬ —
    значит ключ серии рассинхронен; caller логирует это (метаданные, без текста).
    """
    out: list[tuple[Any, int]] = []
    if not isinstance(watched, dict):
        return out
    for w in watched.get("watched", []):
        cid = w.get("telegram_chat_id")
        if isinstance(cid, int) and not isinstance(cid, bool):
            out.append((w.get("series"), cid))
    return out


def resolve_series_ref(
    ref: str, watched: dict[str, Any], *, display_map: dict[str, str] | None = None
) -> str | None:
    """Резолвит человекочитаемую ссылку владельца на серию → каноничный slug.

    Для команды смены чата (REQ 7.4): владелец называет серию свободно — slug
    (`anzhee-direktorat`), отображаемым именем («Директорат») или с дрейфом
    регистра/кавычек. Возвращаем РЕАЛЬНЫЙ slug, существующий в watched.yaml
    (привязка цепляется к существующей записи), или None если не нашли.

    Приоритет: точный slug → нормализованный slug → обратный display-маппинг
    (имя→slug, только если slug есть в watched). None → caller сообщает владельцу
    «не нашёл серию» (видимая реакция, НЕ молчаливый провал — R-REPLY).
    """
    if not ref or not isinstance(ref, str):
        return None
    ref_s = ref.strip().strip("«»\"'").strip()
    if not ref_s:
        return None
    series_list = [
        w.get("series")
        for w in watched.get("watched", [])
        if isinstance(w.get("series"), str) and w.get("series")
    ]
    # 1. Точный slug.
    for s in series_list:
        if s == ref_s:
            return s
    # 2. Нормализованный slug (регистр/пробелы/кавычки).
    nref = normalize_series_key(ref_s)
    for s in series_list:
        if normalize_series_key(s) == nref:
            return s
    # 3. Обратный display-маппинг: имя → slug (если slug есть в watched).
    if display_map:
        for slug, disp in display_map.items():
            if isinstance(disp, str) and normalize_series_key(disp) == nref and slug in series_list:
                return slug
    return None


def get_company_for_series(series: str, watched: dict[str, Any]) -> str | None:
    """Ф6 (E6): первая непустая `company` среди записей серии (нормализована lower).

    Пер-record поле, резолвится на уровень series как `telegram_chat_id`.
    Невалидное/неизвестное значение пропускается (как будто не задано) — резолв
    устойчив к ручному мусору в watched.yaml. Нет разметки → None (вызыватель
    уйдёт на переходный резолв по оргструктуре, см. lib/series_markup.py).
    """
    if not series:
        return None
    for w in watched.get("watched", []):
        if w.get("series") != series:
            continue
        c = w.get("company")
        if isinstance(c, str) and c.strip().lower() in VALID_COMPANIES:
            return c.strip().lower()
    return None


def get_visibility_for_series(series: str, watched: dict[str, Any]) -> str | None:
    """Ф6 (E6): первая непустая `visibility` среди записей серии (lower).

    Нет разметки / невалидное значение → None. Вызыватель (publication_gate)
    трактует None как НЕразмечено → private (fail-closed): неразмеченная серия
    НЕ публикует знание в общий мозг (E3).
    """
    if not series:
        return None
    for w in watched.get("watched", []):
        if w.get("series") != series:
            continue
        v = w.get("visibility")
        if isinstance(v, str) and v.strip().lower() in VALID_VISIBILITY:
            return v.strip().lower()
    return None


def get_genre_for_series(series: str, watched: dict[str, Any]) -> str | None:
    """Ф5 (G4): первая непустая `genre` среди записей серии (нормализована casefold).

    Зеркало `get_company_for_series` — пер-record поле, резолвится на уровень
    series. Невалидное/неизвестное значение пропускается (как будто не задано) —
    резолв устойчив к ручному мусору в watched.yaml. Нет разметки → None
    (вызыватель `lib/series_markup.genre_for_series` подставит мягкий дефолт).
    """
    if not series:
        return None
    for w in watched.get("watched", []):
        if w.get("series") != series:
            continue
        g = w.get("genre")
        if isinstance(g, str) and g.strip().casefold() in VALID_GENRES:
            return g.strip().casefold()
    return None


def set_telegram_chat_id_for_series(series: str, chat_id: int, watched: dict[str, Any]) -> int:
    """Проставляет `telegram_chat_id=<chat_id>` всем записям с этой series.

    Возвращает число обновлённых записей. Caller обязан после этого вызвать
    `save_watched(watched)` (atomic + flock уже встроены в save_watched).

    РИСК4 (ISS-7): матч симметричен чтению — точное `==` ИЛИ нормализованное
    (`casefold`/пробелы/кавычки), чтобы запись и чтение привязки не
    рассинхронились по дрейфу ключа. Команда смены чата (REQ 7.4) до вызова
    резолвит ссылку владельца в каноничный slug (`resolve_series_ref`), так что
    сюда обычно приходит точный slug; нормализация страхует CLI/ручной путь.
    """
    if not series:
        return 0
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise ValueError(f"chat_id must be int, got {type(chat_id).__name__}")
    n = 0
    nseries = normalize_series_key(series)
    for w in watched.get("watched", []):
        ws = w.get("series")
        if ws == series or (ws is not None and normalize_series_key(ws) == nseries):
            w["telegram_chat_id"] = chat_id
            n += 1
    return n


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
