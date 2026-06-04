#!/usr/bin/env python3
"""CLI: backfill-series-memory.py — разовый сбор выжимок-памяти (Ф7 7.5).

Назначение: память серии работает с ПЕРВОЙ следующей встречи (а не через 3),
если рядом с уже лежащими протоколами `<date>-protokol.md` собрать выжимки
`<date>-memory.json`. Делается ОДИН раз после деплоя Ф7.

Детерминированно, без claude (build_digest — чистая выжимка из markdown).
Поэтому безопасно для повторного запуска: существующие выжимки по умолчанию
не перезаписываются (нужен `--overwrite`).

РИСК4 / ПДн: выжимка содержит производные поля встречи (участники/темы/ключевые
пункты). Это новое хранилище производных от ПДн данных. РЕАЛЬНЫЙ прогон по живым
протоколам `~/Projects/me/встречи/` — операция утреннего деплоя владельца (как и
весь батч плана), НЕ ночной автопрогон. `--dry-run` показывает, что будет собрано,
ничего не записывая.

Опции:
  --root <dir>     — корень папки встреч (default `~/Projects/me/встречи`,
                     или env `MEETING_NOTARY_PROTOCOLS_DIR`).
  --series <name>  — собрать только одну серию (имя ПАПКИ). Без него — все серии.
  --overwrite      — пересобрать выжимки, даже если файл уже есть.
  --dry-run        — только показать план (сколько протоколов без выжимки), без записи.

Примеры:
  python3 tools/backfill-series-memory.py --dry-run
  python3 tools/backfill-series-memory.py --series marketplaces-tatiana
  python3 tools/backfill-series-memory.py            # все серии, реальная запись
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
sys.path.insert(0, str(NOTARY_DIR))

from lib import series_memory  # noqa: E402

DEFAULT_ROOT = os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR") or os.path.expanduser(
    "~/Projects/me/встречи"
)


def _plan_series(series_dir: Path, *, overwrite: bool) -> tuple[int, int]:
    """Возвращает (всего_протоколов, будет_собрано) без записи — для --dry-run."""
    total = 0
    todo = 0
    for entry in sorted(series_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        date = series_memory._date_from_protocol_filename(entry.name)
        if not date:
            continue
        total += 1
        mem = series_memory.digest_path(series_dir, date)
        if overwrite or not mem.exists():
            todo += 1
    return total, todo


def main() -> int:
    ap = argparse.ArgumentParser(description="Ф7 7.5: бэкфилл выжимок-памяти серий")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="корень папки встреч")
    ap.add_argument("--series", default=None, help="имя одной серии (папки); без него — все")
    ap.add_argument("--overwrite", action="store_true", help="пересобрать существующие выжимки")
    ap.add_argument("--dry-run", action="store_true", help="только показать план, без записи")
    ap.add_argument("--prune-days", type=int, default=0,
                    help="РИСК4: удалить выжимки старше N дней по ВСЕМ сериям (0=не прунить), "
                         "включая dormant. Прун идёт ПОСЛЕ бэкфилла; в --dry-run не выполняется.")
    args = ap.parse_args()

    root = Path(os.path.expanduser(args.root))
    if not root.is_dir():
        print(f"❌ корень не найден: {root}", file=sys.stderr)
        return 2

    # Какие серии обрабатываем.
    if args.series:
        series_dirs = [root / args.series]
        if not series_dirs[0].is_dir():
            print(f"❌ серия не найдена: {series_dirs[0]}", file=sys.stderr)
            return 2
    else:
        series_dirs = [
            d for d in sorted(root.iterdir(), key=lambda p: p.name)
            if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
        ]

    if args.dry_run:
        grand_total = 0
        grand_todo = 0
        for sd in series_dirs:
            total, todo = _plan_series(sd, overwrite=args.overwrite)
            if total:
                print(f"  {sd.name}: протоколов {total}, соберём выжимок {todo}")
            grand_total += total
            grand_todo += todo
        print(f"\n[dry-run] серий: {len(series_dirs)}, протоколов: {grand_total}, "
              f"к сбору: {grand_todo} (запись НЕ выполнена)")
        if args.prune_days and args.prune_days > 0:
            print(f"[dry-run] прунинг старше {args.prune_days}д НЕ выполняется в dry-run "
                  f"(запусти без --dry-run для реального удаления).")
        return 0

    if not series_memory.is_enabled():
        print("⚠️  ENABLE_SERIES_MEMORY=0 — память отключена. Бэкфилл всё равно "
              "соберёт файлы (kill-switch влияет на чтение/запись в пайплайне).")

    total_series = 0
    total_digests = 0
    if args.series:
        n = series_memory.backfill_series(series_dirs[0], overwrite=args.overwrite)
        if n:
            total_series += 1
            total_digests += n
        print(f"  {series_dirs[0].name}: записано выжимок {n}")
    else:
        res = series_memory.backfill_root(root, overwrite=args.overwrite)
        total_series = res["series"]
        total_digests = res["digests"]

    if args.prune_days and args.prune_days > 0:
        if args.series:
            removed = series_memory.prune_old_digests(series_dirs[0], args.prune_days)
            print(f"  🧹 {series_dirs[0].name}: удалено старых выжимок {removed} (>{args.prune_days}д)")
        else:
            pres = series_memory.prune_root(root, args.prune_days)
            print(f"  🧹 прунинг: серий {pres['series']}, выжимок удалено {pres['digests']} (>{args.prune_days}д)")

    print(f"\n✅ бэкфилл готов: серий тронуто {total_series}, выжимок записано {total_digests}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
