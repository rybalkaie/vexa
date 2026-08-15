#!/usr/bin/env python3
"""CLI очереди доставки результатов в портал (Ф10c).

Зачем отдельная команда: когда владельцу приходит «результат встречи НЕ доехал
до портала», ему нужно не читать логи, а увидеть очередь и решить — повторить
или отпустить. Ровно три вопроса, ровно три ответа.

    python3 tools/portal-delivery.py status            # что в очереди и что удерживается
    python3 tools/portal-delivery.py drain             # отправить всё созревшее прямо сейчас
    python3 tools/portal-delivery.py revive <ключ>     # вернуть «умерший» элемент в строй
    python3 tools/portal-delivery.py release <ключ>    # 🔴 отказаться от доставки и
                                                       #    РАЗРЕШИТЬ удаление исходников

🔴 `release` — единственная дверь, за которой исходники становятся удаляемыми
без подтверждения портала, и она ручная by design: автоматического «сдаюсь» в
этой фазе нет. Тексты протоколов команда не печатает никогда — только ключи,
статусы и счётчики (опасная тройка).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
sys.path.insert(0, str(NOTARY_DIR.parent))

from notary.lib import portal_delivery, portal_ingest  # noqa: E402


def _row(item: dict) -> str:
    return "  %-40s %-9s попыток=%-3s след=%s%s" % (
        item.get("key"),
        item.get("status"),
        item.get("attempts"),
        item.get("next_attempt_at") or "—",
        (" ошибка=%s" % item["last_error"]) if item.get("last_error") else "",
    )


def cmd_status(_args: argparse.Namespace) -> int:
    summary = portal_delivery.summary()
    if not summary.get("enabled"):
        print("Доставка в портал ВЫКЛЮЧЕНА (ENABLE_PORTAL_ORDERS не задан).")
        return 0
    print("Очередь доставки: %s" % json.dumps(summary, ensure_ascii=False))
    problem = portal_ingest.config_problem()
    print("Канал приёма: %s" % (problem or "настроен"))
    items = portal_delivery.list_items()
    if items:
        print("Элементы:")
        for item in items:
            print(_row(item))
    held = portal_delivery.held_sessions()
    if held:
        print("Исходники удерживаются по сессиям (%d):" % len(held))
        for sid in held:
            print("  %s" % sid)
    return 0


def cmd_drain(_args: argparse.Namespace) -> int:
    stats = portal_ingest.drain()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_revive(args: argparse.Namespace) -> int:
    item = portal_delivery.revive(args.key)
    if item is None:
        print("Нет такого элемента: %s" % args.key)
        return 1
    print(_row(item))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    item = portal_delivery.abandon(args.key)
    if item is None:
        print("Нет такого элемента: %s" % args.key)
        return 1
    print("Доставка снята, удержание исходников отпущено:")
    print(_row(item))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("drain").set_defaults(fn=cmd_drain)
    p_revive = sub.add_parser("revive")
    p_revive.add_argument("key")
    p_revive.set_defaults(fn=cmd_revive)
    p_release = sub.add_parser("release")
    p_release.add_argument("key")
    p_release.set_defaults(fn=cmd_release)
    args = parser.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
