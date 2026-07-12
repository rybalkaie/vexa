#!/usr/bin/env python3
"""Самопроверка манифеста знания компаний: что бот РЕАЛЬНО видит и что не подключилось.

Зачем (follow-up 1 из Ф1 плана `watson-company-memory`): состав знания ведёт КОМАНДА
текстовым манифестом `knowledge/notary/protocol-knowledge-allowlist.txt` в своём репо
контекста, а жёсткий бэкстоп приватности живёт в КОДЕ бота. Значит возможна тихая
поломка: человек вписал файл — код его отсёк (финансы / `restricted/` / нет файла /
не влез в бюджет) — человек уверен, что бот про файл знает, а бот не знает. Без этой
команды об отказе знал бы только лог на VPS, которого никто не читает.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 tools/check-knowledge-manifest.py            # все известные компании
    python3 tools/check-knowledge-manifest.py mpfirst    # одна компания

Код возврата: 0 — всё подключилось; 1 — что-то из манифеста НЕ вошло в промпт.
(Ненулевой код нужен, чтобы проверку можно было повесить в CI/крон, а не только
запускать глазами.)

Опасная тройка: печатаются ТОЛЬКО пути, размеры и причины — ни байта содержимого
файлов знания (оно уходит во внешний LLM, но не в логи и не в консоль).
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent  # scripts/notary
if str(_NOTARY) not in sys.path:
    sys.path.insert(0, str(_NOTARY))

from lib import context_knowledge as ck  # noqa: E402


def main(argv: list[str]) -> int:
    companies = argv[1:] or ck.known_companies()
    problems = 0
    for company in companies:
        bundle = ck.load_protocol_knowledge(company)
        print("=" * 78)
        print(ck.manifest_report(company, bundle))
        if bundle.rejected:
            problems += len(bundle.rejected)
        mpath = ck.manifest_path(company)
        if mpath and not mpath.is_file():
            print(f"  ℹ️  манифеста нет ({mpath}) — работает код-фолбэк (поведение как до Ф2)")
    print("=" * 78)
    if problems:
        print(
            f"❌ {problems} файл(ов) из манифеста НЕ попадут в протокол (причины выше).\n"
            "   Это НЕ поломка бота: он соберёт протокол без них. Но если файл нужен —\n"
            "   почини строку в манифесте (или убери её, если файл не нужен)."
        )
        return 1
    print("✅ всё, что вписано в манифесты, реально доезжает до промпта бота")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
