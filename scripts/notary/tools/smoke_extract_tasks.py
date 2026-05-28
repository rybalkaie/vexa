#!/usr/bin/env python3
"""Smoke РАЗМ2 для Ф5 meeting-notary-llm: extract_tasks на sales-quality-2026-05-27.

Эталон (из handoff Ф4 раздел 5.9 + ручной protokol.md):
  Задачи Ильи (2):
    1. Прогнать тестовые звонки Михаила Саргина через свою локальную модель
    2. Оформить видение проекта (документ) и разослать команде
  Задачи Михаила Саргина (2 — стейкхолдером не является, должны быть skip):
    1. Отправить Илье тестовые проблемные звонки
    2. Если вспомнит — прислать название коммерческого "второго пилота"
  Задачи «Команды» (2 — owner != конкретное лицо, ожидаемое поведение — skip):
    1. Заполнить @-никнеймы у всех сотрудников в Bitrix
    2. Накидать Илье голосовухами идеи

Критерий PASS:
  - 2 из 2 задач Ильи извлечены (формулировки совпадают по смыслу, перефразирование OK).
  - 0 ложно-извлечённых (задач, которых НЕТ в эталоне).

Запуск: cd ~/Projects/meeting-notary/vexa/scripts/notary && python3 tools/smoke_extract_tasks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from lib.llm_postprocess import extract_tasks  # noqa: E402

PROTOKOL_PATH = Path(
    "/Users/ilarybalka/Projects/me/встречи/sales-quality-2026-05-27/"
    "2026-05-27-protokol.md"
)

EXPECTED_ILIA = [
    "прогнать тестовые звонки",
    "оформить видение проекта",
]
# Ожидаемые задачи Саргина — для проверки что LLM их различает, но они в
# tasks.md не записываются (Саргин не в реестре стейкхолдеров).
EXPECTED_SARGIN_HINTS = [
    "отправить",
    "название",  # «прислать название коммерческого»
]


def _matches_any(needle_phrase: str, candidates: list[str]) -> bool:
    needle_low = needle_phrase.lower()
    for c in candidates:
        if needle_low in c.lower():
            return True
    return False


def main() -> int:
    if not PROTOKOL_PATH.is_file():
        print(f"FAIL: эталон не найден: {PROTOKOL_PATH}")
        return 2

    protocol = PROTOKOL_PATH.read_text(encoding="utf-8")
    meeting_meta = {
        "series": "sales-quality",
        "date": "2026-05-27",
        "duration": 21,
        "audioDurationS": 1260,
        "expectedParticipants": [
            "Илья Рыбалка",
            "Михаил Саргин",
            "Дарья Набережная",
            "Михаил Еремеев",
        ],
        "participants": [],
    }
    print("→ extract_tasks running (Sonnet 4.6, может занять 15-60 сек)...")
    tasks = extract_tasks(
        protocol,
        meeting_meta,
        meeting_sid="smoke-sales-quality",
    )
    print(f"→ Получено задач: {len(tasks)}")
    print()
    for i, t in enumerate(tasks, start=1):
        owner = t.get("owner")
        text = t.get("text")
        deadline = t.get("deadline") or "—"
        sphere = t.get("sphere") or "—"
        conf = t.get("confidence_sphere")
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
        quote = (t.get("source_quote") or "")[:80]
        print(
            f"  {i}) [{owner}] {text}\n"
            f"     deadline={deadline} sphere={sphere} conf={conf_s}\n"
            f"     quote: {quote!r}"
        )
        print()

    # --- Анализ ---
    ilia_tasks = [t for t in tasks if (t.get("owner") or "").lower().startswith("илья")]
    sargin_tasks = [
        t for t in tasks if "саргин" in (t.get("owner") or "").lower()
    ]
    team_tasks = [
        t for t in tasks
        if (t.get("owner") or "").strip().lower() in ("команда", "команда anzhee", "сотрудники")
    ]
    other_tasks = [
        t for t in tasks
        if t not in ilia_tasks and t not in sargin_tasks and t not in team_tasks
    ]

    ilia_phrases = [t.get("text", "") for t in ilia_tasks]

    print("=" * 60)
    print(f"Итог:")
    print(f"  Илья: {len(ilia_tasks)}")
    print(f"  Михаил Саргин: {len(sargin_tasks)}")
    print(f"  Команда: {len(team_tasks)}")
    print(f"  Прочие owner'ы: {len(other_tasks)}")
    print()

    failures: list[str] = []
    # 1. Илья: 2 из 2 ожидаемых
    for needle in EXPECTED_ILIA:
        if not _matches_any(needle, ilia_phrases):
            failures.append(f"Не найдена задача Ильи: {needle!r}")
    # 2. Никаких лишних задач Ильи
    extra_ilia = [
        t for t in ilia_tasks
        if not any(_matches_any(n, [t.get("text", "")]) for n in EXPECTED_ILIA)
    ]
    if extra_ilia:
        failures.append(
            f"Лишние задачи Ильи (нет в эталоне): "
            + ", ".join(t.get("text", "")[:50] for t in extra_ilia)
        )

    print("=" * 60)
    if failures:
        print("РЕЗУЛЬТАТ: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("РЕЗУЛЬТАТ: PASS")
    print(f"  ✅ 2/2 задач Ильи извлечены")
    print(f"  ✅ 0 ложно-извлечённых задач Ильи")
    return 0


if __name__ == "__main__":
    sys.exit(main())
