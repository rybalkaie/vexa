#!/usr/bin/env python3
"""send_morning_digest.py — Ф4 (план `2026-07-12-watson-company-memory.md`, R10–R13):
тонкая точка входа утреннего дайджеста открытых вопросов для systemd-таймера.

Вся логика — в `lib/morning_digest` (см. её docstring): в день встречи серии, в 09:00
МСК (но не позже чем за час до начала), в чат серии уходит PDF «Открытые вопросы к
встрече», сгруппированный по ответственным; ответ на это сообщение обновляет статус
висяка тем же каналом, что и ответ на протокол.

Таймер `meeting-notary-morning-digest.timer` тикает часто (каждые 15 мин): тик сам
решает, кому сегодня пора (встреча в 09:30 → дайджест в 08:30). Тик дешёвый и
идемпотентный — читает очередь планировщика и выжимки серий с диска, LLM не зовёт,
дважды за день одной серии не шлёт. Нет встреч сегодня / нет висяков → молчит (R13).

CLI:
  python3 send_morning_digest.py             # боевой тик (его зовёт таймер)
  python3 send_morning_digest.py --dry-run   # посчитать, кому бы ушло, не отправляя
  python3 send_morning_digest.py --series <папка-серии>
"""
from __future__ import annotations

import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.lib import morning_digest  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(morning_digest.main())
