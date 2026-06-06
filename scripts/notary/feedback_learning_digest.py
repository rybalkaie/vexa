#!/usr/bin/env python3
"""feedback_learning_digest.py — Ф6 (FB10): озвучка самообучения в вечернем дайджесте.

Строит блок «🧠 Ватсон выучил из правок участников …» из обратимого append-only
лога выученных терм-замен (`lib/feedback_learning`) и шлёт владельцу. Образец —
`auto_vocab/digest.py` (отдельный поток обучения: тот учит STT-словарь из заметок,
этот — терм-замены из ПРАВОК участников). Контроль постфактум: владелец отвечает
«откати <термин>» — `--rollback` деактивирует совпавшие правила (прежнее поведение
возвращается), история в логе сохраняется.

После успешной отправки выученные правила помечаются `announced` (append-only) —
повторно в следующих дайджестах не озвучиваются, пока не появятся новые.

CLI:
  python -m notary.feedback_learning_digest [--dry-run]
  python -m notary.feedback_learning_digest --rollback "откати Гарсиа"

Ф9: вшить вызов в сборку вечернего дайджеста (как отдельный блок, по образцу
vocab-digest-таймера) и завести роут ответа «откати …» из дайджест-канала
(ср. `meetings_apply_reply.py`).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.lib import feedback_learning  # noqa: E402

logger = logging.getLogger(__name__)


def run_digest(*, dry_run: bool = False) -> str:
    """Строит и (если не dry-run) шлёт блок «Ватсон выучил …». Возвращает текст блока."""
    text, ids = feedback_learning.format_digest_block()
    if not text:
        logger.info("[fb-learn-digest] нечего озвучивать")
        return ""
    if dry_run:
        logger.info("[fb-learn-digest dry-run] %s", text.replace("\n", " | "))
        return text
    sent = False
    try:
        from notary.lib import notify  # noqa: PLC0415
        sent = notify.push(text, dedupe=False)
    except Exception as e:  # noqa: BLE001
        logger.error("[fb-learn-digest] отправка не удалась (%s)", e)
    if sent:
        # Помечаем озвученными ТОЛЬКО при успешной отправке — иначе при сбое
        # доставки правило промолчит навсегда (как `--commit-known-on-send`).
        feedback_learning.mark_announced(ids)
    return text


def run_rollback(reply: str) -> dict:
    """Применяет ответ «откати <что>». Возвращает summary для дайджест-обёртки."""
    rolled = feedback_learning.apply_rollback_reply(reply)
    return {
        "rolled_back": [
            {"series": r.get("series"), "wrong": r.get("wrong"), "right": r.get("right")}
            for r in rolled
        ],
        "count": len(rolled),
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [fb-learn-digest] %(message)s",
    )
    ap = argparse.ArgumentParser(description="Ф6 (FB10): озвучка самообучения / откат")
    ap.add_argument("--dry-run", action="store_true", help="вывести блок в stdout, не слать в TG")
    ap.add_argument("--rollback", metavar="REPLY", default=None,
                    help="обработать ответ владельца «откати <термин>» и выйти")
    args = ap.parse_args(argv)

    if args.rollback is not None:
        result = run_rollback(args.rollback)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    msg = run_digest(dry_run=args.dry_run)
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
