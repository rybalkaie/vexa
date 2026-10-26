"""Shared YAML registry helpers for meeting-notary CLIs.

Atomic write via tmp + rename; preserves trailing newline on read.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("notary.registry")

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

# 🔴 ФУ-П1 (Ф10d): пути реестра резолвятся ЛЕНИВО, а не на импорте модуля.
#
# ЧТО БЫЛО. `ROOMS_FILE`/`WATCHED_FILE`/`PAUSE_FILE` были константами, вычисленными
# в момент `import`. Любая проба, выставившая `MEETING_NOTARY_REGISTRY_DIR` ПОСЛЕ
# импорта (а он случается транзитивно — `lib.identity` тянет `cli.registry`),
# читала и правила ЖИВОЙ реестр владельца в `~/Projects/me/встречи`. Обходили это
# тремя `mock.patch.object` в каждом наборе проб (`test_orders_ph10b.py:110-112`,
# `test_portal_delivery_ph10c.py:204-206`) — то есть заплатой в КАЖДОЙ пробе, и
# любая новая проба, забывшая заплату, снова била в боевой файл. Тот же класс
# дефекта чинили лениво уже дважды: `.state.json` планировщика (f249582) и память
# дедупа (40a6934). Это третий и последний из известных.
#
# ЧТО СТАЛО. Резолверы `rooms_file()`/`watched_file()`/`pause_file()` спрашивают
# окружение в момент ОБРАЩЕНИЯ. Имена-константы остались доступны снаружи через
# `__getattr__` модуля (PEP 562) — старые вызыватели (`cli/meeting_rooms.py`,
# `cli/meeting_watch.py`, `tools/set-telegram-chat-id.py`) и уже написанные пробы
# с `mock.patch.object` продолжают работать без правок. Заплата пробы по-прежнему
# ВЫИГРЫВАЕТ: резолвер сначала смотрит в `globals()`, где патч и оседает.


def registry_dir() -> Path:
    """Каталог реестра ПРЯМО СЕЙЧАС (env читается на каждом обращении)."""
    return _resolve_registry_dir()


def _resolved(name: str, filename: str) -> Path:
    """Путь `name`: заплата пробы (если модулю её поставили) → ленивый резолв.

    `globals().get(name)` не находит ничего в обычной жизни — константы в
    `__dict__` модуля больше нет. Она появляется там ровно тогда, когда проба
    сделала `mock.patch.object(registry, name, ...)`, и тогда обязана выиграть:
    иначе патч перестал бы действовать и пробы старых фаз ушли бы в боевой файл.
    """
    patched = globals().get(name)
    if patched is not None:
        return Path(patched)
    return registry_dir() / filename


def rooms_file() -> Path:
    return _resolved("ROOMS_FILE", "rooms.yaml")


def watched_file() -> Path:
    return _resolved("WATCHED_FILE", "watched.yaml")


def pause_file() -> Path:
    return _resolved("PAUSE_FILE", ".pause-until")


_LAZY_PATHS = {
    "ROOMS_FILE": rooms_file,
    "WATCHED_FILE": watched_file,
    "PAUSE_FILE": pause_file,
}


def __getattr__(name: str) -> Any:  # PEP 562 — только для отсутствующих имён
    resolver = _LAZY_PATHS.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()


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
#
# Ф1 (ISS-51…55): опциональный режим открытых вопросов серии — что бот считает
# «открытыми вопросами» этой серии:
#   open_questions_mode: carry | last-only
#               carry — серия-накопление: незакрытые вопросы переезжают из
#               встречи во встречу и копятся, пока их не закроют (Директорат:
#               «висяки не должны теряться»). last-only — справочный режим:
#               показываем только вопросы последней встречи.
#   open_questions_carry_since: YYYY-MM-DD
#               отсечка «счёт с чистого листа»: вопросы, копившиеся ДО этой даты,
#               серия больше не поднимает. Ставит сам бот при возврате серии в
#               накопление — руками задавать не нужно. Дата из будущего или
#               несуществующая срежет весь хвост серии, поэтому валидатор их
#               отвергает.
#               ОТСУТСТВИЕ поля → дефолт last-only (решение владельца 29.07);
#               неизвестное значение → тот же дефолт + предупреждение в лог
#               (реестр правят руками, опечатка не должна тихо менять поведение).
"""


def load_rooms(lock: bool = False) -> dict[str, Any]:
    if lock:
        _acquire_lock(rooms_file())
    return _read_yaml(rooms_file(), "rooms")


def save_rooms(data: dict[str, Any]) -> None:
    try:
        _atomic_write_yaml(rooms_file(), data, ROOMS_HEADER)
    finally:
        _release_lock(rooms_file())


def release_rooms_lock() -> None:
    """Release rooms lock explicitly (call in finally when save_rooms wasn't reached)."""
    _release_lock(rooms_file())


def load_watched(lock: bool = False) -> dict[str, Any]:
    if lock:
        _acquire_lock(watched_file())
    return _read_yaml(watched_file(), "watched")


def save_watched(data: dict[str, Any]) -> None:
    try:
        _atomic_write_yaml(watched_file(), data, WATCHED_HEADER)
    finally:
        _release_lock(watched_file())


def release_watched_lock() -> None:
    """Release watched lock explicitly (call in finally when save_watched wasn't reached)."""
    _release_lock(watched_file())


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
    # С10 (25.09.2026): хост целиком, а не подстрока — `https://evil.example/telemost.yandex.ru/…`
    # раньше проходила. Проверка общая с планировщиком и запускателем.
    from notary.lib.telemost_url import is_telemost_url  # noqa: PLC0415

    if not is_telemost_url(url):
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

# ISS-51…55 Ф1: режим открытых вопросов серии.
# `carry`     — накопление со статусами (текущее поведение целиком): висяки
#               переносятся из протокола в протокол, утром приходит дайджест со
#               статусами и просьбой отчитаться.
# `last-only` — справочная шпаргалка: к встрече приходит список задач ТОЛЬКО
#               прошедшей встречи, без статусов и без переноса.
# Дефолт — `last-only` (решение владельца 3 от 29.07: накопление включается
# осознанно). Поле ОПЦИОНАЛЬНО и пер-record, резолв на уровень series — как
# telegram_chat_id. Режим переключается в обе стороны в любой момент
# (решение владельца 6), поэтому значение читается на каждом обращении.
VALID_OPEN_QUESTIONS_MODES = {"carry", "last-only"}
DEFAULT_OPEN_QUESTIONS_MODE = "last-only"

# ISS-51…55, решение владельца 7: возврат серии в накопление начинает счёт С
# ЧИСТОГО ЛИСТА — вопросы, копившиеся до переключения, не поднимаются. Дата
# (`YYYY-MM-DD`, пояс выжимок) ставится ТОЛЬКО в момент реального переключения в
# `carry` командой бота. Пока «чистый лист» был лишь обещанием в тексте
# подтверждения, он получался ПОБОЧНО — из того, что протокол в шпаргалке идёт
# без раздела хвоста; стоило переключить туда и обратно, не проведя ни одной
# встречи, и весь до-переключательный ком возвращался целиком.
# Поля нет (реестр разложен руками, серия никогда не переключалась) → отсечки
# нет → прежнее поведение.
OPEN_QUESTIONS_CARRY_SINCE_FIELD = "open_questions_carry_since"
_CARRY_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    # ISS-51…55 Ф1: open_questions_mode — опционален, валидируем значение если задано.
    # bool отсекается явно: он подкласс int и .strip() у него нет.
    oqm = rec.get("open_questions_mode")
    if oqm is not None:
        if not isinstance(oqm, str) or oqm.strip().casefold() not in VALID_OPEN_QUESTIONS_MODES:
            errors.append(
                f"open_questions_mode={oqm!r}: допустимо "
                f"{sorted(VALID_OPEN_QUESTIONS_MODES)} или отсутствие"
            )
    # Решение владельца 7: отсечка «счёт с чистого листа». Дата или отсутствие;
    # мусор ловим здесь, потому что читатель его молча игнорирует (нет отсечки =
    # старое поведение), и без валидации опечатка жила бы в файле незамеченной.
    since = rec.get(OPEN_QUESTIONS_CARRY_SINCE_FIELD)
    if since is not None:
        # 🔴 Форму мало проверить, нужен СМЫСЛ. Регулярка пропускала «2026-13-45» и
        # «2099-01-01», а сравнение дат строковое: такая отсечка старше любой
        # реальной выжимки и срезает хвост серии на ВСЕХ поверхностях навсегда,
        # оставляя один INFO в логе. Поле правят руками (план предписывает
        # разложить режимы до включения), значит опечатка тут — рядовое событие,
        # и цена у неё необратимая.
        bad = None
        if not isinstance(since, str) or not _CARRY_SINCE_RE.match(since.strip()):
            bad = "допустима дата YYYY-MM-DD или отсутствие"
        else:
            from datetime import date as _date  # noqa: PLC0415 — stdlib, локально
            try:
                parsed = _date.fromisoformat(since.strip())
            except ValueError:
                bad = "такой даты не существует"
            else:
                if parsed > _date.today():
                    bad = ("дата из будущего: отсечка «с момента переключения» "
                           "срезала бы весь хвост серии")
        if bad:
            errors.append(f"{OPEN_QUESTIONS_CARRY_SINCE_FIELD}={since!r}: {bad}")
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


def series_for_chat(chat_id: Any, watched: dict[str, Any]) -> list[str]:
    """Серии, привязанные к этому `telegram_chat_id`. Уникальные, в порядке появления.

    ISS-52: бот должен понимать, в чате какой серии он находится, когда контекста
    реплая нет.

    Выключенные (`enabled: false`) и заглушенные (`ignored: true`) записи
    пропускаются — тем же правилом, что и в планировщике (`scheduler.py`): чего
    он больше не ведёт, того для бота уже нет. Иначе ОДНОЙ мёртвой записи в чате
    хватало бы, чтобы чат навсегда стал «неоднозначным» и ISS-52 в нём молча
    перестал работать (реальный сценарий: закрыли выставку в общей комнате `@t11`;
    Ф5 дедупа встреч добавляет второй — задвоенная встреча заводит вторую серию).

    Схлопывание по УНИКАЛЬНЫМ сериям: дубли записей одной серии случаются
    (ISS-54), и наивный список дал бы «несколько серий» там, где она одна, — то
    есть лишний переспрос у людей в группе на каждую просьбу. На сегодняшнем
    боевом реестре дублей нет, схлопывание работает на упреждение.

    Обратный инвариант «один чат → одна серия» реестром НЕ гарантирован (одна
    комната может обслуживать несколько серий, `@t11` = координация и выставка),
    поэтому возвращается список, а решение — за вызывателем.

    Ключ схлопывания — `normalize_series_key` (ISS-7): дрейф регистра/пробелов не
    должен размножать одну серию в две. Возвращается РЕАЛЬНЫЙ slug первой записи.
    """
    out: list[str] = []
    if not isinstance(watched, dict):
        return out
    if not (isinstance(chat_id, int) and not isinstance(chat_id, bool)):
        return out
    records = watched.get("watched")
    if not isinstance(records, list):
        return out
    seen: set[str] = set()
    for w in records:
        if not isinstance(w, dict):
            continue
        # Мёртвые записи не дают серию (образец — scheduler.py).
        if not w.get("enabled", True) or w.get("ignored", False):
            continue
        cid = w.get("telegram_chat_id")
        if not (isinstance(cid, int) and not isinstance(cid, bool)):
            continue
        if cid != chat_id:
            continue
        ws = w.get("series")
        if not isinstance(ws, str) or not ws.strip():
            continue
        norm = normalize_series_key(ws)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(ws)
    return out


# Уже отругались на этот мусор в open_questions_mode: (ключ серии, тип, repr).
# Живёт на время процесса — реестр правится руками, и до правки повторять нечего.
_OQM_WARNED: set = set()


def series_open_questions_mode(series: Any, watched: dict[str, Any]) -> str:
    """Режим открытых вопросов серии: `carry` | `last-only`. Дефолт — `last-only`.

    Резолв тот же, что у `get_telegram_chat_id_for_series` (ISS-7, РИСК4): сначала
    ТОЧНОЕ совпадение ключа серии, затем нормализованное. Иначе дрейф регистра или
    кавычек в `meta.series` молча вернул бы серию-накопление в справочный режим, и
    висяки Директората перестали бы копиться незаметно.

    Пер-record поле, резолв на уровень серии — берётся первое ВАЛИДНОЕ значение
    среди записей серии (как company/visibility/genre).

    Неизвестное значение → дефолт + предупреждение в лог: реестр правят руками, и
    опечатка не должна тихо менять поведение серии (образец — scheduler.py про
    неизвестный `type`). Предупреждение об одном и том же мусоре печатается ОДИН
    раз на процесс (`_OQM_WARNED`) — иначе одна опечатка сыпалась бы в лог на
    каждом чтении. Логируются только имя серии и само значение — тексты задач и
    содержимое встреч сюда не попадают.
    """
    if not isinstance(watched, dict):
        return DEFAULT_OPEN_QUESTIONS_MODE
    norm = normalize_series_key(series)
    if not norm:
        # Не-строка, пустая строка И «нормализованно-пустой» ключ («  », «»»):
        # после нормализации такой ключ совпал бы с записями, чья series тоже
        # пуста, — это матч по пустоте, а не по серии. `series_for_chat` от того
        # же класса ошибок защищён явно, здесь симметрично.
        return DEFAULT_OPEN_QUESTIONS_MODE
    records = watched.get("watched")
    if not isinstance(records, list):
        return DEFAULT_OPEN_QUESTIONS_MODE

    # Слоты приоритета: (живая запись, точный ключ). Живые записи главнее
    # мёртвых — как в `series_for_chat` и `scheduler.py`, где `enabled: false` /
    # `ignored` серию не дают вовсе. Иначе отказная запись прошлой встречи
    # (их заводит ISS-53) определяла бы режим ЖИВОЙ серии, а владелец в ответ на
    # «эта серия — накопление» слышал бы «и так в накоплении», пока живая запись
    # остаётся шпаргалкой. Мёртвые слоты — фолбэк: если живых записей у серии не
    # осталось совсем, честнее назвать записанный режим, чем дефолт.
    slots: dict[tuple[bool, bool], str] = {}
    has_live = False
    for w in records:
        if not isinstance(w, dict):
            continue
        ws = w.get("series")
        exact = ws == series
        if not exact and not (ws is not None and normalize_series_key(ws) == norm):
            continue
        live = series_record_is_live(w)
        has_live = has_live or live
        slot = (live, exact)
        if slot in slots:
            continue  # уже нашли первое валидное значение этого слота
        raw = w.get("open_questions_mode")
        if raw is None:
            continue  # поле не задано у этой записи — смотрим следующую
        mode: str | None = None
        if isinstance(raw, str):  # bool не подкласс str — отдельная проверка не нужна
            cand = raw.strip().casefold()
            if cand in VALID_OPEN_QUESTIONS_MODES:
                mode = cand
        if mode is None:
            # Дедуп: резолвер зовётся на КАЖДОМ дайджесте/протоколе, а опечатка в
            # реестре живёт до правки руками — без дедупа одна опечатка засоряла бы
            # лог ровно тогда, когда его сели читать. Ключ по значению, а не по
            # объекту: `raw` бывает нехешируемым (список из YAML).
            warn_key = (norm, type(raw).__name__, repr(raw))
            if warn_key not in _OQM_WARNED:
                _OQM_WARNED.add(warn_key)
                logger.warning(
                    "Серия %s: неизвестное open_questions_mode=%r — беру дефолт %s",
                    ws, raw, DEFAULT_OPEN_QUESTIONS_MODE,
                )
            continue
        slots[slot] = mode
    # Есть хоть одна живая запись → решают ТОЛЬКО живые: мёртвая запись не
    # определяет поведение серии даже тогда, когда у живой поле не проставлено
    # (иначе отказная запись прошлой встречи молча включала бы серии режим).
    # Живых нет совсем → мёртвые как фолбэк: честнее назвать записанный режим.
    order = ((True, True), (True, False)) if has_live else ((False, True), (False, False))
    for slot in order:
        if slot in slots:
            return slots[slot]
    return DEFAULT_OPEN_QUESTIONS_MODE


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
    # `isinstance(w, dict)` обязателен: реестр правят руками, и одна строка
    # вместо записи роняла резолв `AttributeError`'ом. В query-ветке команды
    # режима он летел мимо `try` — человек не получал ответа, а обработка
    # сообщения обрывалась, и до роутов ниже оно не доходило.
    series_list = [
        w.get("series")
        for w in (watched.get("watched", []) if isinstance(watched, dict) else [])
        if isinstance(w, dict) and isinstance(w.get("series"), str) and w.get("series")
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


def set_open_questions_mode_for_series(
    series: str, mode: str, watched: dict[str, Any], *, carry_since: str | None = None
) -> int:
    """Ф2 (ISS-51…55): проставляет `open_questions_mode=<mode>` ВСЕМ записям серии.

    Возвращает число обновлённых записей. Caller обязан после этого вызвать
    `save_watched(watched)` (atomic + flock уже внутри `save_watched`).

    `carry_since` (дата `YYYY-MM-DD`) — отсечка «счёт с чистого листа» решения
    владельца 7. Ставится ТОЛЬКО вместе с `mode='carry'` и ТОЛЬКО когда серия
    реально переезжает из шпаргалки в накопление. Повтор команды «режим
    накопление» по серии, которая и так в накоплении, отсечку НЕ трогает —
    иначе одна лишняя команда обнуляла бы накопленный хвост (а он живёт только
    в разделе протокола, и восстановить его нечем).

    КОНВЕНЦИЯ ПОЛЯ (одна на всех, Ф2): единица владения `open_questions_mode` —
    СЕРИЯ, не запись. Пишем во ВСЕ записи серии (включая выключенные — чтобы
    записи не разошлись); читаем по ЖИВЫМ записям (`series_record_is_live`), а
    мёртвые берём только если живых не осталось совсем. Команда бота доводит
    реестр до этого состояния при каждом вызове, даже если режим «тот же».
    Исключение — `meeting-watch edit --id`, который правит ОДНУ запись: это
    ручная дверь для разбора аномалий, и после неё реестр может быть
    несогласован до следующей команды бота (долг: привести CLI к той же
    конвенции — файл вне зоны Ф2).

    Почему ВСЕМ записям серии, а не одной: читатель
    (`series_open_questions_mode`) берёт ПЕРВОЕ валидное значение среди записей
    серии. Одна серия обычно = несколько записей (несколько встреч/календарей,
    ISS-54 добавляет дубли). Записав режим в одну запись, мы получили бы
    read-after-write, зависящий от ПОРЯДКА записей в файле: устаревшее значение
    в записи выше молча перебило бы только что заданное — владелец увидел бы
    «✅ принял», а поведение не изменилось. Тот же выбор, что у соседа
    `set_telegram_chat_id_for_series` (одна привязка на серию — инвариант
    писателя).

    Матч ключа симметричен чтению (ISS-7, РИСК4): точное `==` ИЛИ нормализованное
    (`casefold`/пробелы/кавычки). Нормализованно-пустой ключ (`""`, `«»`) не
    матчит ничего: иначе он совпал бы с записями, чья `series` тоже пуста, — это
    матч по пустоте, а не по серии.

    `mode` вне `VALID_OPEN_QUESTIONS_MODES` → `ValueError` (в реестр записывается
    только каноничное значение; мусор в файл не попадает даже при ошибке caller'а).
    """
    if not series:
        return 0
    if not isinstance(mode, str):
        raise ValueError(f"mode must be str, got {type(mode).__name__}")
    canon = mode.strip().casefold()
    if canon not in VALID_OPEN_QUESTIONS_MODES:
        raise ValueError(
            f"mode={mode!r}: допустимо {sorted(VALID_OPEN_QUESTIONS_MODES)}"
        )
    if carry_since is not None:
        if canon != "carry":
            raise ValueError("carry_since имеет смысл только при mode='carry'")
        if not isinstance(carry_since, str) or not _CARRY_SINCE_RE.match(carry_since.strip()):
            raise ValueError(f"carry_since={carry_since!r}: нужна дата YYYY-MM-DD")
    n = 0
    for w in series_records(series, watched):
        w["open_questions_mode"] = canon
        if carry_since is not None:
            w[OPEN_QUESTIONS_CARRY_SINCE_FIELD] = carry_since.strip()
        n += 1
    return n


def series_open_questions_carry_since(series: Any, watched: Any) -> str | None:
    """Отсечка «счёт с чистого листа» серии (`YYYY-MM-DD`) или None.

    Берём САМУЮ ПОЗДНЮЮ дату среди записей серии, а не первую валидную (как
    company/visibility/genre): отсечку ставит команда бота сразу во ВСЕ записи,
    и если одна из них отстала (её добавили позже, реестр правили руками),
    старая дата не имеет права ослабить последнее решение владельца.

    Мусор и нечитаемую дату игнорируем — «отсечки нет» значит «веду себя как
    раньше». Это безопасная сторона: лишний хвост обратим, потерянный — нет.
    """
    latest: str | None = None
    for w in series_records(series, watched):
        raw = w.get(OPEN_QUESTIONS_CARRY_SINCE_FIELD)
        if not isinstance(raw, str):
            continue
        val = raw.strip()
        if not _CARRY_SINCE_RE.match(val):
            continue
        if latest is None or val > latest:
            latest = val
    return latest


def series_records(series: Any, watched: Any) -> list[dict[str, Any]]:
    """Все записи серии — тем же матчем ключа, каким читается и пишется поле.

    Ф2: команда режима спрашивает у реестра не только «какой режим сейчас», но и
    «согласованы ли записи между собой» (самолечение расхождений) и «жива ли
    серия вообще» (стоит ли писать людям в её чат). Матч ключа обязан быть ОДИН
    на чтение, запись и эти проверки, иначе они разойдутся молча — корень ISS-7.

    Матч: точное `==` ИЛИ нормализованное (`casefold`/пробелы/кавычки).
    Нормализованно-пустой ключ (`""`, `«»`, `«  »`) не матчит НИЧЕГО: иначе он
    совпал бы с записями, чья `series` тоже пуста, — это матч по пустоте.
    Записи-не-словари (руками правленный YAML) пропускаются.
    """
    if not isinstance(watched, dict):
        return []
    nseries = normalize_series_key(series)
    if not nseries:
        return []
    records = watched.get("watched")
    if not isinstance(records, list):
        return []
    out: list[dict[str, Any]] = []
    for w in records:
        if not isinstance(w, dict):
            continue
        ws = w.get("series")
        if ws == series or (ws is not None and normalize_series_key(ws) == nseries):
            out.append(w)
    return out


def series_record_is_live(rec: Any) -> bool:
    """Запись «живая» = бот по ней ходит: не `enabled: false` и не `ignored`.

    Тот же признак, что у `series_for_chat` и `scheduler.py` («мёртвые записи не
    дают серию»). Вынесен, чтобы у режима и у остального реестра он был один.
    """
    if not isinstance(rec, dict):
        return False
    return bool(rec.get("enabled", True)) and not rec.get("ignored", False)


def pause_until() -> str | None:
    """Return ISO datetime if pause is active and not yet expired; else None (and clean stale file)."""
    if not pause_file().exists():
        return None
    raw = pause_file().read_text(encoding="utf-8").strip()
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
            pause_file().unlink()
        except FileNotFoundError:
            pass
        return None
    return raw


def err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def ok(msg: str) -> None:
    sys.stdout.write(msg + "\n")
