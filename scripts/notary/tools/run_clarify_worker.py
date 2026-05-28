#!/usr/bin/env python3
"""Entry-point для worker'а clarify-flow (Ф3 meeting-notary-llm).

**Standalone-режим только для локального smoke на маке.** В продакшене на VPS
clarify-хендлеры встроены в `meetings_listener.py` (тот же бот, тот же
getUpdates). Использует `TELEGRAM_NOTARIUS_BOT_TOKEN`.

Реальная логика — `lib.clarify_worker.main()`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
NOTARY_DIR = THIS_FILE.parent.parent  # vexa/scripts/notary/
sys.path.insert(0, str(NOTARY_DIR))

from lib.clarify_worker import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
