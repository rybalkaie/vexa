#!/usr/bin/env python3
"""reconcile_pending.py — Ф6 (план `2026-06-24-pending-items-lifecycle.md`): тонкая
точка входа агента-сверщика висяков для systemd-timer.

Запускается `meeting-notary-reconcile-pending.timer` раз в сутки ночью (A5). Вся
логика — в `lib/pending_reconciler` (см. её docstring): кросс-серийное закрытие
висяков по протоколам ДРУГИХ серий, три исхода (close/doubt/keep), консервативный
порог, опасная тройка (тексты не логируем), запись в sidecar Ф2 `task-status.json`.

ГЕЙТ `ENABLE_PENDING_RECONCILER` — ДЕФОЛТ-OFF: без флага таймер инертен (реальный
`claude` не зовётся). Боевую активацию флага + установку юнита на VPS делает ВЛАДЕЛЕЦ
(control-gate). Логи (только счётчики/метаданные) → journal через systemd.

CLI:
  python3 reconcile_pending.py            # боевой прогон (нужен гейт ON)
  python3 reconcile_pending.py --dry-run  # калибровка: считает исходы, не пишет
  python3 reconcile_pending.py --series <папка-серии>
"""
from __future__ import annotations

import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.lib import pending_reconciler  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(pending_reconciler.main())
