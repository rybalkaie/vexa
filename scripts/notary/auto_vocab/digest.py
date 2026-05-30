"""Слой 4 — еженедельный TG-дайджест авто-словаря (Шаг 8.4).

Запускается systemd-таймером `meeting-notary-vocab-digest.timer` (вс 19:00 МСК).
Читает `state.week_stats` за текущую ISO-неделю (в вс вечером она и завершается)
и шлёт владельцу сводку через `notify.push`.

Шаблон (план, строка 379):
  «За неделю авто-добавлено N, спросил M, ты одобрил K, отклонил L. Доля
  автоматики: auto_added/(auto_added+requested) = X%. Источники: +S терминов.
  Расходы Claude API: $C.»

Пустая неделя (ни встреч, ни источников) → «копим данные» (не молчим — владелец
должен видеть, что система жива).

CLI:
  python -m notary.auto_vocab.digest [--dry-run] [--week 2026-W22]
"""

from __future__ import annotations

import argparse
import logging
import sys

from notary.auto_vocab import state

logger = logging.getLogger(__name__)


def format_digest(stats: dict) -> str:
    auto = int(stats.get("auto_added", 0) or 0)
    req = int(stats.get("requested", 0) or 0)
    approved = int(stats.get("approved", 0) or 0)
    rejected = int(stats.get("rejected", 0) or 0)
    sources = int(stats.get("sources_added", 0) or 0)
    cost = float(stats.get("cost_usd", 0.0) or 0.0)
    week = stats.get("week", "")

    if auto + req + sources == 0:
        return (
            f"📚 Словарь распознавания ({week}): за неделю ничего нового — "
            f"встреч с новыми терминами не было, источники без изменений. Копим данные."
        )

    total = auto + req
    ratio = f"{round(100 * auto / total)}%" if total else "—"
    lines = [
        f"📚 Авто-словарь за неделю ({week}):",
        f"• Авто-добавлено без спроса: {auto}",
        f"• Спросил у тебя: {req} (одобрил {approved}, отклонил {rejected})",
        f"• Доля автоматики: {ratio} (auto/{total or '0'})",
        f"• Из твоих заметок (people/companies/projects): +{sources}",
        f"• Расходы Claude API: ${cost:.2f}",
    ]
    return "\n".join(lines)


def run(*, dry_run: bool = False, week: str | None = None) -> str:
    stats = state.week_stats(week)
    msg = format_digest(stats)
    if dry_run:
        logger.info("[digest dry-run] %s", msg.replace("\n", " | "))
        return msg
    try:
        from notary.lib import notify  # noqa: PLC0415
        # dedupe=False: дайджест уникален раз в неделю, дедуп не нужен и мог бы
        # съесть две недели с одинаковыми нулями.
        notify.push(msg, dedupe=False)
    except Exception as e:  # noqa: BLE001
        logger.error("[digest] отправка не удалась (%s)", e)
    return msg


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [auto_vocab.digest] %(message)s")
    ap = argparse.ArgumentParser(description="Еженедельный TG-дайджест авто-словаря")
    ap.add_argument("--dry-run", action="store_true", help="вывести в stdout, не слать в TG")
    ap.add_argument("--week", default=None, help="ISO-неделя, напр. 2026-W22 (по умолчанию текущая)")
    args = ap.parse_args(argv)
    msg = run(dry_run=args.dry_run, week=args.week)
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
