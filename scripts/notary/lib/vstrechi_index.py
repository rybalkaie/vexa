"""Автогенерация INDEX.md в корне ~/Projects/me/встречи/.

Без LLM — простой os.walk + sort. Используется:
  * finalize-meeting.py — после доставки протокола (или по завершении финализации),
    чтобы INDEX был свежим;
  * plans/scripts/migrate-vstrechi-structure.py (--update-index) — первый прогон
    после миграции Ф7.

Структура INDEX.md (стабильна, парсится глазами):
    # Встречи — индекс
    _Сгенерирован YYYY-MM-DDTHH:MM:SS_

    ## <series>
    - YYYY-MM-DD — [link]
    - YYYY-MM-DD — [link]
    - YYYY-MM-DD — [link]

    ## <series-2>
    ...

Сортировка series — по дате последней встречи (DESC), внутри — 3 последние даты (DESC).
Под-папки `_one-off/`, `_archive/`, `_test/`, `_config/`, `_versions/` пропускаются.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_ROOT = Path(os.path.expanduser("~/Projects/me/встречи"))
INDEX_FILENAME = "INDEX.md"
SERIES_TOP_N = 3  # сколько последних встреч под каждой series в INDEX'е

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

logger = logging.getLogger(__name__)


def _atomic_write_text(path: Path, text: str) -> None:
    """tempfile.mkstemp в той же директории → fsync → os.replace."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=".tmp-index-", dir=str(parent))
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _date_to_int(date_str: str) -> int:
    """YYYY-MM-DD → int (для сортировки). Пустая/невалидная → -1 — гарантированно
    попадает в конец списка при DESC (РЕГ1 цикла Ф7: 0 ставил пустые даты в
    начало списка, теперь меньшее число → дальше при DESC через `-_date_to_int`).
    """
    if not date_str or not _DATE_RE.match(date_str):
        return -1
    try:
        return int(date_str.replace("-", ""))
    except ValueError:
        return -1


def _collect_series_dates(root: Path) -> dict[str, list[str]]:
    """Top-level пробег по корню, собирает {series: [date, ...]} (DESC).

    Series — любая папка без префикса `_`. Date внутри series — берётся из имён
    файлов `<YYYY-MM-DD>.md` или `<YYYY-MM-DD>-protokol.md`. Иные имена игнорим.
    """
    series: dict[str, set[str]] = {}
    if not root.is_dir():
        return {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("_"):
            continue
        dates: set[str] = set()
        for f in child.iterdir():
            if not f.is_file() or not f.name.endswith(".md"):
                continue
            stem = f.stem
            # accept "<date>" or "<date>-protokol*"
            if _DATE_RE.match(stem):
                dates.add(stem)
                continue
            if "-protokol" in stem:
                head = stem.split("-protokol", 1)[0]
                if _DATE_RE.match(head):
                    dates.add(head)
        if dates:
            series[name] = dates
    return {k: sorted(v, reverse=True) for k, v in series.items()}


def _build_index_text(series_dates: dict[str, list[str]], *, root: Path) -> str:
    """Рендерит INDEX.md как чистый markdown."""
    lines: list[str] = []
    lines.append("# Встречи — индекс")
    lines.append("")
    lines.append(f"_Сгенерирован {datetime.now().isoformat(timespec='seconds')}_")
    lines.append("")
    if not series_dates:
        lines.append("_Пока пусто — нет регулярных series._")
        lines.append("")
        return "\n".join(lines)

    # Сортировка series по последней дате DESC; при равной дате — лексикографически
    # по имени ASC (детерминированный порядок — Н8 цикла Ф7).
    ordered = sorted(
        series_dates.items(),
        key=lambda kv: (kv[1][0] if kv[1] else "", kv[0]),
    )
    ordered.reverse()
    # reverse() переворачивает обе компоненты — для имени получим Z→A, а нужно A→Z
    # при равной дате. Чистый сорт:
    ordered = sorted(
        series_dates.items(),
        key=lambda kv: (-_date_to_int(kv[1][0] if kv[1] else ""), kv[0]),
    )
    for s, dates in ordered:
        lines.append(f"## {s}")
        for d in dates[:SERIES_TOP_N]:
            protocol = root / s / f"{d}-protokol.md"
            transcript = root / s / f"{d}.md"
            target = protocol if protocol.exists() else transcript
            try:
                rel = target.relative_to(root)
            except ValueError:
                rel = target.name
            lines.append(f"- {d} — [{target.name}]({rel})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def update_vstrechi_index(root: Optional[Path | str] = None) -> dict:
    """Обновляет `<root>/INDEX.md` атомарно. Возвращает {series_count, total_bytes, path}.

    Безопасно вызывать многократно — без LLM, без сетевых вызовов.
    """
    root_path = Path(os.path.expanduser(str(root))) if root else DEFAULT_ROOT
    if not root_path.is_dir():
        raise FileNotFoundError(f"vstrechi root does not exist: {root_path}")

    series_dates = _collect_series_dates(root_path)
    text = _build_index_text(series_dates, root=root_path)
    target = root_path / INDEX_FILENAME
    _atomic_write_text(target, text)
    return {
        "series_count": len(series_dates),
        "total_bytes": len(text.encode("utf-8")),
        "path": str(target),
    }


def is_enabled() -> bool:
    """Гейт `ENABLE_VSTRECHI_INDEX` — дефолт ON, выключить можно `=0/no/false`."""
    raw = (os.environ.get("ENABLE_VSTRECHI_INDEX") or "").strip().lower()
    return raw not in ("0", "false", "no")


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Обновить INDEX.md в ~/Projects/me/встречи/")
    p.add_argument("--root", default=None, help="Корень (default: ~/Projects/me/встречи)")
    args = p.parse_args()
    try:
        stats = update_vstrechi_index(args.root)
    except FileNotFoundError as e:
        print(f"[vstrechi-index] ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    print(
        f"[vstrechi-index] updated: {stats['series_count']} series, "
        f"total bytes={stats['total_bytes']} → {stats['path']}"
    )
