#!/usr/bin/env python3
"""CLI: set-telegram-chat-id.py <series> <chat_id> [--create-test-series].

Атомарная правка `watched.yaml` через готовые helper'ы из Ф6
(`cli.registry.set_telegram_chat_id_for_series` + `save_watched`).

Где запускать:
  - На маке: пишет в `~/Projects/me/встречи/watched.yaml`.
  - На VPS: пишет в `/srv/meeting-notary/registry/watched.yaml` (по
    `MEETING_NOTARY_REGISTRY_DIR` из `.env.notary`).
  - Явно: `--registry-dir <path>`.

Опции:
  --create-test-series   — создать manual-series `<series>` (для smoke), если её
                           ещё нет в watched.yaml. cron в субботу 12:00,
                           enabled=false (никогда не сработает реальная финализация
                           по расписанию), expected_participants=[Илья Рыбалка].
                           Нужна для Ф8 boevoy smoke с `test-delivery-smoke`.
  --registry-dir <path>  — переопределить директорию реестра.
  --dry-run              — показать что было бы записано, не трогая файл.

Пример (Ф8 boevoy smoke):
    python3 tools/set-telegram-chat-id.py test-delivery-smoke -1001177080361 \\
        --create-test-series
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
sys.path.insert(0, str(NOTARY_DIR))

from cli import registry  # noqa: E402


TEST_SERIES_TEMPLATE = {
    "type": "manual",
    "cron": "0 12 * * 6",
    "tz": "Asia/Dubai",
    "room": "@t11",
    "enabled": False,
    "ignored": False,
    "keep_audio": False,
    "notify": [],
    "expected_participants": ["Илья Рыбалка"],
}


def _ensure_test_series(watched: dict, series: str) -> bool:
    """Создать manual-series если ещё нет. Возвращает True если создана."""
    for w in watched.get("watched", []):
        if w.get("series") == series:
            return False
    rec = {"id": series, "series": series, **TEST_SERIES_TEMPLATE}
    watched.setdefault("watched", []).append(rec)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("series", help="series-имя (kebab-case латиницей)")
    parser.add_argument("chat_id", type=int, help="chat_id Telegram-группы (отрицательный для супергруппы)")
    parser.add_argument("--create-test-series", action="store_true",
                        help="создать manual-series если её нет (для Ф8 smoke)")
    parser.add_argument("--registry-dir", default=None,
                        help="переопределить директорию реестра (по умолчанию — MEETING_NOTARY_REGISTRY_DIR или ~/Projects/me/встречи)")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать изменения, не трогая файл")
    args = parser.parse_args()

    if args.registry_dir:
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = args.registry_dir
        # Перечитать модуль чтобы подхватить env.
        import importlib
        importlib.reload(registry)

    print(f"[set-telegram-chat-id] registry: {registry.WATCHED_FILE}")

    watched = registry.load_watched(lock=not args.dry_run)
    try:
        created = False
        if args.create_test_series:
            created = _ensure_test_series(watched, args.series)
            if created:
                print(f"[set-telegram-chat-id] created test series: {args.series}")
            else:
                print(f"[set-telegram-chat-id] test series уже есть: {args.series}")

        n = registry.set_telegram_chat_id_for_series(args.series, args.chat_id, watched)
        if n == 0 and not args.create_test_series:
            print(f"[set-telegram-chat-id] ERROR: series '{args.series}' не найдена в watched.yaml. "
                  f"Используй --create-test-series для smoke или добавь series вручную.", file=sys.stderr)
            registry.release_watched_lock()
            return 2

        if args.dry_run:
            print(f"[set-telegram-chat-id] dry-run: would set telegram_chat_id={args.chat_id} "
                  f"для {n} запис(и/ей) series='{args.series}'")
            registry.release_watched_lock()
            return 0

        registry.save_watched(watched)
        print(f"[set-telegram-chat-id] OK: telegram_chat_id={args.chat_id} → {n} запис(ей) "
              f"series='{args.series}' (created={created})")
        return 0
    except Exception:
        registry.release_watched_lock()
        raise


if __name__ == "__main__":
    sys.exit(main())
