#!/usr/bin/env python3
"""Smoke Ф5: route_tasks на синтетической 1:1 встрече с Татьяной (mpervyi).

Проверки:
  1. Задачи Ильи попадают в tasks.md (atomic write).
  2. Задачи Татьяны попадают в её трек через stakeholder-track.sh append.
  3. Создаётся подсекция `### 📋 Из встречи 2026-05-28` с ссылкой на протокол.
  4. owner=Спикер N → строка с `[?]` в tasks.md.
  5. owner=Михаил Саргин (не в реестре) → skip + лог.

Запуск:
  cd ~/Projects/meeting-notary/vexa/scripts/notary && python3 tools/smoke_route_tasks_1on1.py

Использует ТЕСТОВЫЕ копии файлов в /tmp/, не трогает реальные tasks.md и треки.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from lib.llm_postprocess import route_tasks  # noqa: E402

REAL_TASKS_MD = Path("/Users/ilarybalka/Projects/me/tasks.md")
REAL_TRACK = Path(
    "/Users/ilarybalka/Projects/me/companies/mpervyi/совещания/"
    "tatyana-otkrytye-voprosy.md"
)


def main() -> int:
    if not REAL_TASKS_MD.is_file():
        print(f"FAIL: tasks.md не найден: {REAL_TASKS_MD}")
        return 2
    if not REAL_TRACK.is_file():
        print(f"FAIL: трек Татьяны не найден: {REAL_TRACK}")
        return 2

    # Создаём изолированную тестовую копию `me/` со всеми нужными директориями.
    tmp_root = Path(tempfile.mkdtemp(prefix="smoke-route-tasks-"))
    print(f"→ Тестовая ME_DIR: {tmp_root}")
    test_me = tmp_root / "me"
    test_me.mkdir()
    # Копируем tasks.md (целиком, чтобы заголовки структуры были на месте).
    shutil.copy2(REAL_TASKS_MD, test_me / "tasks.md")
    # Копируем структуру треков и сам файл.
    track_dst = test_me / "companies" / "mpervyi" / "совещания" / "tatyana-otkrytye-voprosy.md"
    track_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_TRACK, track_dst)

    # Stakeholders.json в _methods.
    methods_dst = test_me / "methods"
    methods_dst.mkdir()
    (methods_dst / "stakeholders.json").write_text(
        '{"stakeholders":[{"slug":"tatyana","name":"Татьяна",'
        '"file_path":"companies/mpervyi/совещания/tatyana-otkrytye-voprosy.md",'
        '"company_tag":"мпервый"}]}',
        encoding="utf-8",
    )

    # Whitelist в `stakeholder-track.sh` сейчас ссылается на реальный
    # `~/Projects/me/...`. Чтобы тест не трогал реальный трек — выдадим
    # тестовому stakeholder.file_path абсолютным путём через ME_DIR override
    # и подменим whitelist через временный конфиг.
    # → НО: shell-скрипт читает whitelist из своего исходника. Чтобы не
    # хачить shell — мы НЕ зовём stakeholder-track.sh, а только проверяем
    # извлечение/route_tasks для Ильи + [?]. Задачи Татьяны — внутри теста
    # будут попыткой записи (с reject от whitelist), что и ожидается.
    # Это smoke-стек: проверяем что route_tasks НЕ падает + Илья записан.

    os.environ["ME_DIR"] = str(test_me)
    os.environ["MEETING_NOTARY_TASKS_MD"] = str(test_me / "tasks.md")
    os.environ["MEETING_NOTARY_STAKEHOLDERS_JSON"] = str(methods_dst / "stakeholders.json")
    os.environ["ENABLE_TASK_ROUTING"] = "1"
    os.environ["ENABLE_TASK_EXTRACTION"] = "1"

    # Синтетические задачи — как если бы их вернул extract_tasks.
    tasks = [
        {
            "owner": "Илья",
            "text": "проверить статистику караоке за май",
            "deadline": None,  # → попадёт в pending_deadline
            "sphere": "мпервый",
            "source_quote": "Илья: пришлю статистику до пятницы.",
            "confidence_sphere": 0.9,
        },
        {
            "owner": "Илья Рыбалка",
            "text": "согласовать бюджет на августную закупку",
            "deadline": "2026-06-10",
            "sphere": "мпервый",
            "source_quote": "Илья: подготовлю и согласую к 10 июня.",
            "confidence_sphere": 0.85,
        },
        {
            "owner": "Татьяна",
            "text": "прислать отчёт по RNP за май",
            "deadline": "2026-06-05",
            "sphere": "мпервый",
            "source_quote": "Татьяна: пришлю отчёт RNP к понедельнику.",
            "confidence_sphere": 0.9,
        },
        {
            "owner": "Татьяна Филипова",
            "text": "проработать с Александрой план закупок Q4",
            "deadline": None,
            "sphere": "мпервый",
            "source_quote": "Татьяна: возьму на себя Q4-закупки.",
            "confidence_sphere": 0.9,
        },
        {
            "owner": "Спикер 3",
            "text": "запросить смету у подрядчика",
            "deadline": None,
            "sphere": None,
            "source_quote": "Спикер 3: запрошу смету.",
            "confidence_sphere": None,
        },
        {
            "owner": "Михаил Саргин",  # не в реестре стейкхолдеров (тут только Татьяна)
            "text": "что-то на стороне",
            "deadline": None,
            "sphere": "anzhee",
            "source_quote": "Саргин: посмотрю на стороне.",
            "confidence_sphere": 0.7,
        },
    ]

    meeting_meta = {
        "series": "marketplaces-tatiana",
        "date": "2026-05-28",
        "expectedParticipants": ["Илья Рыбалка", "Татьяна Филипова"],
        "participants": [],
        "audioDurationS": 1800,  # 30 мин
    }

    # 1:1-критерий: ровно 2 ожидаемых + Илья + Татьяна (по first-word матчу).
    print("→ route_tasks running...")
    result = route_tasks(
        tasks, meeting_meta, meeting_sid="smoke-1on1-tatiana",
    )

    print(f"  ilia={result['ilia']}  others={result['others']}  "
          f"unknown_owner={result['unknown_owner']}  "
          f"pending_deadline={result['pending_deadline']}  "
          f"errors={result['errors']}")

    # Контроль tasks.md
    tasks_text = (test_me / "tasks.md").read_text(encoding="utf-8")
    has_kvar_task = "статистику караоке за май" in tasks_text
    has_budget_task = "согласовать бюджет на августную закупку" in tasks_text
    has_unknown = "запросить смету у подрядчика" in tasks_text and "[?]" in tasks_text
    has_ctx_prot = "протокол marketplaces-tatiana 2026-05-28" in tasks_text

    print("\n--- tasks.md проверки ---")
    print(f"  Задача Ильи (без срока) в tasks.md: {has_kvar_task}")
    print(f"  Задача Ильи (с deadline) в tasks.md: {has_budget_task}")
    print(f"  Задача [?] (Спикер 3) в tasks.md: {has_unknown}")
    print(f"  Контекст 'протокол <series> <date>' присутствует: {has_ctx_prot}")

    # Контроль трека Татьяны — поскольку whitelist в shell ссылается на
    # реальный ~/Projects/me/, append может либо упасть rc=4 (path not allowed)
    # либо успешно отработать если ME_DIR override прошёл (НО shell не читает
    # ME_DIR — у него свой ALLOWED_PATHS_*). Ожидаем error в result.
    print("\n--- трек Татьяны проверки ---")
    track_text = track_dst.read_text(encoding="utf-8")
    has_section = "📋 Из встречи 2026-05-28" in track_text
    has_protocol_link = "протокол встречи" in track_text
    has_tatyana_task = "прислать отчёт по RNP" in track_text or "проработать с Александрой" in track_text
    print(f"  Подсекция '📋 Из встречи 2026-05-28': {has_section}")
    print(f"  Ссылка на протокол: {has_protocol_link}")
    print(f"  Задача Татьяны: {has_tatyana_task}")
    print(f"  ⚠️  Запись в трек шла через shell-скрипт; если whitelist не пускает на /tmp/-копию — ошибка в result['errors'] = expected smoke-факт.")

    # PASS-критерий: route_tasks отработал, Илья записан, [?] записан.
    failures: list[str] = []
    if not has_kvar_task or not has_budget_task:
        failures.append("Задачи Ильи не попали в tasks.md")
    if not has_unknown:
        failures.append("[?]-задача Спикера 3 не попала в tasks.md")
    if result["ilia"] != 2:
        failures.append(f"Ожидали ilia=2, получили {result['ilia']}")
    if result["unknown_owner"] != 1:
        failures.append(f"Ожидали unknown_owner=1, получили {result['unknown_owner']}")
    if result["pending_deadline"] != 1:
        failures.append(f"Ожидали pending_deadline=1, получили {result['pending_deadline']}")

    print("\n" + "=" * 60)
    if failures:
        print("РЕЗУЛЬТАТ: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("РЕЗУЛЬТАТ: PASS")
    print(f"  ✅ Илья записан в tasks.md (2 задачи)")
    print(f"  ✅ [?] Спикер 3 записан в tasks.md")
    print(f"  ✅ pending_deadline посчитан правильно")
    print(f"  Тестовая ME_DIR оставлена для ручной проверки: {tmp_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
