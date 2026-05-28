"""Единый источник истины для путей записи протоколов meeting-notary.

Все, кто пишет в `~/Projects/me/встречи/` (collector.py, llm_postprocess.py,
будущие генераторы протоколов / задач), обязаны идти через `_target_path()` —
иначе миграция файловой структуры (Ф7 текущего плана) сломает callers.

Стандарт пути зафиксирован в методичке:
  ~/Projects/me/methods/kak-delat-protokol-vstrechi.md
  (раздел «Файловая система хранения»).

Целевая структура (после Ф7-миграции):
    встречи/
        <series>/                       # регулярные серии без даты в имени
            YYYY-MM-DD.md
            YYYY-MM-DD-protokol.md
        _one-off/
            YYYY-MM-DD-<id>/
                YYYY-MM-DD.md
                YYYY-MM-DD-protokol.md
        _archive/
            YYYY/<series>/
                YYYY-MM-DD.md
        _test/
        _config/
        INDEX.md

Сейчас (до Ф7) файловая структура другая (`<series>-<date>/`), и collector.py
продолжает писать по-старому. `_target_path` возвращает уже **целевые** пути по
стандарту — подключать его в новых модулях Ф2–Ф6 безопасно (они пишут только
новые артефакты протокола, не пересекаясь с легаси collector). Ф7 синхронизирует
collector + физически перенесёт исторические папки.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_ROOT = os.path.expanduser("~/Projects/me/встречи")
ONE_OFF_DIR = "_one-off"
ARCHIVE_DIR = "_archive"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")
# Path-component: латиница (+ кириллица для legacy кейсов вроде `встречи`),
# цифры, подчёркивание, дефис, точка. Запрещены `/`, `\`, NUL, и компоненты `.` / `..`.
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.\-Ѐ-ӿ]+$")


def _validate_path_component(value: str, *, field: str) -> str:
    """Защита от path traversal и мусорных символов в имени папки/файла.

    Допустимо: латиница/цифры/`_`/`-`/`.`/кириллица. Запрещено: `/`, `\\`,
    управляющие символы, имена `.` и `..`, пустая строка. Закрывает У1/У3.
    """
    if not value or value in (".", ".."):
        raise ValueError("meta.%s must be a non-empty valid path component, got %r" % (field, value))
    if not _PATH_COMPONENT_RE.match(value):
        raise ValueError(
            "meta.%s contains invalid characters (allowed: A-Z a-z 0-9 . _ - кириллица), got %r"
            % (field, value)
        )
    return value


def _extract_date(meta: dict) -> str:
    """Достаёт YYYY-MM-DD из meta. Приоритет: `date`, потом `startTs[:10]`.

    Валидируется регексом — пути не должны содержать «странных» строк
    вместо даты (тихая порча данных, см. Н1 цикла Ф1).
    """
    candidates = []
    raw_date = meta.get("date")
    if isinstance(raw_date, str) and len(raw_date) >= 10:
        candidates.append(raw_date[:10])
    ts = meta.get("startTs")
    if isinstance(ts, str) and len(ts) >= 10:
        candidates.append(ts[:10])
    for c in candidates:
        if _DATE_RE.match(c):
            return c
    if not candidates:
        raise ValueError("meta must contain 'date' (YYYY-MM-DD) or 'startTs' (ISO) field")
    raise ValueError(
        "meta.date / meta.startTs first 10 chars must match YYYY-MM-DD, "
        "got candidates: %r" % candidates
    )


def _extract_one_off_id(meta: dict) -> str:
    """Достаёт id для one-off папки. Приоритет:
        1. `oneOffId` — явное переопределение (для тестов / ручных запусков),
        2. `tm-<id>` — извлекаем из `sessionUid` (формат runner.py:
           `auto-tm-<id>-<YYYYMMDDTHHMMSSZ>` или `manual-tm-<id>-<YYYYMMDDTHHMMSSZ>`),
        3. fallback — целиком `sessionUid` (или строка "unknown").
    """
    explicit = meta.get("oneOffId")
    if isinstance(explicit, str) and explicit:
        return explicit
    sid = meta.get("sessionUid") or ""
    if not isinstance(sid, str):
        return "unknown"
    if "tm-" in sid:
        tail = sid.split("tm-", 1)[1]
        # Отрезаем хвост `-<YYYYMMDDTHHMMSSZ>`, если он есть.
        parts = tail.rsplit("-", 1)
        if len(parts) == 2 and "T" in parts[1] and parts[1].endswith("Z"):
            return f"tm-{parts[0]}"
        return f"tm-{tail}"
    return sid or "unknown"


def _target_path(meta, *, root=None, kind: str = "transcript"):
    """Возвращает Path для протокола/транскрипта в `~/Projects/me/встречи/`.

    Три кейса по `meta`:

    a) **series + date** (регулярная встреча):
       `<root>/<series>/<date>.md`

    b) **one-off** (без series — разовая встреча, например через Telemost):
       `<root>/_one-off/<date>-<id>/<date>.md`

    c) **явный архив** (когда нужно положить в исторический срез):
       `<root>/_archive/<year>/<series>/<date>.md`
       Требует `meta.archive=<year>` И непустой `meta.series`.

    Поля `meta` (dict):
      - `series` (str, опц.) — название series; пустая строка / None / отсутствует = one-off.
      - `startTs` (str) — ISO timestamp; первые 10 символов = YYYY-MM-DD.
      - `date` (str, опц.) — переопределение даты; если задано — используется вместо startTs.
      - `sessionUid` (str, опц.) — id сессии runner.py, используется для one-off id.
      - `oneOffId` (str, опц.) — явное переопределение one-off id.
      - `archive` (str|int, опц.) — год архива; включает кейс (c).

    Параметры:
      - `root` (str|Path|None) — корень папки встречи. None →
        `$MEETING_NOTARY_PROTOCOLS_DIR` env, иначе `DEFAULT_ROOT`.
      - `kind` (str) — суффикс для имени файла. `"transcript"` → `<date>.md`,
        `"protokol"` → `<date>-protokol.md`. Любое другое значение → `<date>-<kind>.md`.

    Возвращает: `pathlib.Path` (НЕ создаёт директорию, НЕ резолвит симлинки).
    """
    if not isinstance(meta, dict):
        raise TypeError("meta must be dict, got %s" % type(meta).__name__)

    if root is None:
        root_env = os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR")
        root = root_env if root_env else DEFAULT_ROOT
    base = Path(os.path.expanduser(str(root)))

    date_str = _extract_date(meta)

    if kind == "transcript":
        filename = f"{date_str}.md"
    elif kind == "protokol":
        filename = f"{date_str}-protokol.md"
    else:
        filename = f"{date_str}-{kind}.md"

    series_raw = meta.get("series")
    if series_raw is None:
        series = ""
    elif isinstance(series_raw, str):
        stripped = series_raw.strip()
        # У2: если исходно непустой, а после strip пустой — это явный ввод
        # «  » → не молчим, а валим. Если изначально пусто — ok, one-off.
        if series_raw and not stripped:
            raise ValueError(
                "meta.series is whitespace-only %r — must be either a real "
                "series name or omitted (None/empty)" % series_raw
            )
        if stripped:
            _validate_path_component(stripped, field="series")
        series = stripped
    else:
        # НОВ3: явный TypeError вместо silent one-off fallback при числе/dict/list.
        raise TypeError(
            "meta.series must be str or None, got %s" % type(series_raw).__name__
        )

    archive = meta.get("archive")
    if archive not in (None, "", 0, False):
        year = str(archive).strip()
        # У6: archive — только год вида YYYY (предотвращает _archive/<мусор>/).
        if not _YEAR_RE.match(year):
            raise ValueError(
                "meta.archive must be a 4-digit year (e.g. 2025 or '2025'), got %r" % archive
            )
        if not series:
            raise ValueError("meta.series required when meta.archive is set")
        return base / ARCHIVE_DIR / year / series / filename

    if series:
        return base / series / filename

    one_off_id = _extract_one_off_id(meta)
    # У3: id может прийти из meta.oneOffId (ручной ввод в Ф6) или из sessionUid.
    # Защита от `/`, `\\`, `..` в имени папки.
    _validate_path_component(one_off_id, field="oneOffId/sessionUid-derived")
    folder = f"{date_str}-{one_off_id}"
    return base / ONE_OFF_DIR / folder / filename
