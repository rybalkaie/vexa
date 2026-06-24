"""Тесты Ф1 плана `2026-06-24-pending-items-lifecycle.md` (ISS-23 #1) — корень,
из-за которого раздел висяков «🔻 С прошлых встреч» не появлялся ни в одном
реальном протоколе.

Диагноз (handoff §3): хвост `open_tasks` приходил пустым на 1-на-1 встречах,
потому что их блок задач модель рендерит markdown-ТАБЛИЦЕЙ `| Кому | Что | Срок |`
(«формат Татьяны»), а `extract_open_tasks` читал ТОЛЬКО буллеты `- ` под `**Имя**`.
Плюс курсивный футер протокола («*Протокол восстановлен…*») ложно ловился как буллет
и оседал единственной мусорной «задачей».

Покрывает:
  - R1: `extract_open_tasks` разбирает таблицу задач (исполнитель-префикс + срок);
    `build_digest` кладёт непустой `open_tasks` для табличной 1:1 встречи.
  - ложный буллет из эмфазы (`*курсив*`/`**жирно**`) больше НЕ извлекается.
  - R2 end-to-end: синтетическая серия из 2 встреч (1-я — табличная) → раздел
    «🔻 С прошлых встреч» с перенесёнными пунктами доезжает до промпта 2-й встречи.
  - регресс канонического буллет-формата (группа) — без изменений.
  - опасная тройка: текст задач из таблицы не утекает в лог (только счётчики).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss23_phase1_pending_items -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import series_memory as sm  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402


# 1-на-1 протокол с блоком задач в виде markdown-ТАБЛИЦЫ (реальный формат
# marketplaces-tatiana) + курсивный футер, который раньше ловился ложным буллетом.
_PROTO_TABLE_1TO1 = """#протоколвстречи 03.06.2026

**Встреча:** Маркетплейсы — Татьяна.

**Длительность:** 60 мин

**Участники:** Илья Рыбалка, Татьяна

---

## 1. Кабинеты маркетплейсов

▪️ Обсудили статус кабинетов.

## ✅ Задачи по итогам

| Кому | Что | Срок |
|------|-----|------|
| **Татьяна** | Подключить Ozon-доставку и рекламу на сайте | старт сейчас |
| **Татьяна** | Прислать аналитику по конкурентам-проекторам | сегодня |
| **Илья** | Оплатить смену юр.адреса (12 500 ₽) | — |
| **Мария** | Расчёт по выводу проектов | до конца след. недели |

---

*Протокол восстановлен из транскрипта 04.06 (вчера доставка не прошла — бот был сломан).*
"""

# Канонический буллет-формат (группа) — для проверки отсутствия регресса.
_PROTO_BULLETS = """#протоколвстречи 09.06.2026

**Участники:** Илья Рыбалка, Михаил Саргин

---

## 1) Поставки

▪️ Обсудили логистику.

## Задачи

**Михаил Саргин**

- 🟠 Оформить сертификат СТ-1.

- Пополнить рекламный кабинет до четверга.
"""

_META = {"series": "marketplaces-tatiana", "date": "2026-06-10",
         "participants": ["Илья Рыбалка"], "expectedParticipants": ["Илья Рыбалка"]}


# ==========================================================================
# R1 — таблица задач разбирается (1:1 хвост перестаёт быть пустым)
# ==========================================================================
class TestTableTasksExtraction(unittest.TestCase):

    def test_all_table_rows_extracted(self):
        tasks = sm.extract_open_tasks(_PROTO_TABLE_1TO1)
        # 4 строки-данных таблицы (шапка + разделитель отброшены).
        self.assertEqual(len(tasks), 4)

    def test_owner_prefix_from_first_column(self):
        tasks = sm.extract_open_tasks(_PROTO_TABLE_1TO1)
        self.assertTrue(any(t.startswith("Татьяна:") and "Ozon" in t for t in tasks))
        self.assertTrue(any(t.startswith("Илья:") and "юр.адреса" in t for t in tasks))
        self.assertTrue(any(t.startswith("Мария:") and "выводу проектов" in t for t in tasks))

    def test_due_included_when_meaningful(self):
        tasks = sm.extract_open_tasks(_PROTO_TABLE_1TO1)
        joined = " | ".join(tasks)
        self.assertIn("(срок: сегодня)", joined)
        self.assertIn("(срок: старт сейчас)", joined)
        self.assertIn("(срок: до конца след. недели)", joined)

    def test_empty_due_dash_not_emitted(self):
        # Ячейка срока «—» не должна давать «(срок: —)».
        tasks = sm.extract_open_tasks(_PROTO_TABLE_1TO1)
        ur_adres = next(t for t in tasks if "юр.адреса" in t)
        self.assertNotIn("срок:", ur_adres)

    def test_header_and_separator_not_tasks(self):
        tasks = sm.extract_open_tasks(_PROTO_TABLE_1TO1)
        for t in tasks:
            # шапка «Кому/Что/Срок» и разделитель «---» не превратились в задачи
            self.assertNotEqual(t.strip().strip("-:| "), "")
            self.assertNotIn("Кому", t)
            self.assertFalse(set(t) <= set("-:| "))

    def test_emphasis_footer_not_extracted(self):
        # Корень-2: курсивный футер «*Протокол восстановлен…*» больше НЕ буллет.
        tasks = sm.extract_open_tasks(_PROTO_TABLE_1TO1)
        self.assertFalse(any("восстановлен из транскрипта" in t for t in tasks))

    def test_no_raw_replies(self):
        for t in sm.extract_open_tasks(_PROTO_TABLE_1TO1):
            self.assertNotRegex(t, r"\*\*\[\d")

    def test_digest_open_tasks_nonempty_for_table_1to1(self):
        # R1: после финализации 1:1 встречи выжимка несёт непустой open_tasks.
        d = sm.build_digest(_PROTO_TABLE_1TO1, {"series": "marketplaces-tatiana",
                                                "date": "2026-06-03"})
        self.assertIn("open_tasks", d)
        self.assertEqual(len(d["open_tasks"]), 4)


# ==========================================================================
# Разбор таблицы — крайние случаи (шапка/без шапки/ложная шапка)
# ==========================================================================
class TestTableParsingEdgeCases(unittest.TestCase):

    def test_headerless_table_uses_default_columns(self):
        # Таблица без строки-шапки → дефолт Кому|Что|Срок (0|1|2).
        proto = (
            "#протоколвстречи 01.06.2026\n\n**Участники:** Илья\n\n---\n\n"
            "## Задачи\n\n"
            "| **Илья** | Сделать отчёт по складу | завтра |\n"
        )
        tasks = sm.extract_open_tasks(proto)
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].startswith("Илья:"))
        self.assertIn("отчёт по складу", tasks[0])
        self.assertIn("(срок: завтра)", tasks[0])

    def test_data_row_with_word_chto_not_treated_as_header(self):
        # Строка-данных, где в тексте задачи встречается «что» — не должна быть
        # принята за шапку (требуем И исполнителя, И задачу в одной строке).
        proto = (
            "#протоколвстречи 01.06.2026\n\n**Участники:** Илья\n\n---\n\n"
            "## Задачи\n\n"
            "| Кому | Что | Срок |\n"
            "|---|---|---|\n"
            "| **Илья** | Решить, что делать с остатками | сегодня |\n"
        )
        tasks = sm.extract_open_tasks(proto)
        self.assertEqual(len(tasks), 1)
        self.assertIn("что делать с остатками", tasks[0])

    def test_alt_header_order_columns_resolved(self):
        # Перестановка колонок (Срок | Что | Ответственный) — индексы из шапки.
        proto = (
            "#протоколвстречи 01.06.2026\n\n**Участники:** Илья\n\n---\n\n"
            "## Задачи\n\n"
            "| Срок | Что | Ответственный |\n"
            "|---|---|---|\n"
            "| пятница | Прислать смету | **Татьяна** |\n"
        )
        tasks = sm.extract_open_tasks(proto)
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].startswith("Татьяна:"))
        self.assertIn("Прислать смету", tasks[0])
        self.assertIn("(срок: пятница)", tasks[0])


# ==========================================================================
# Регресс канонического буллет-формата (не сломали группу)
# ==========================================================================
class TestBulletFormatNoRegression(unittest.TestCase):

    def test_bullets_still_extracted_with_owner(self):
        tasks = sm.extract_open_tasks(_PROTO_BULLETS)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t.startswith("Михаил Саргин:") for t in tasks))
        self.assertTrue(any("СТ-1" in t for t in tasks))
        for t in tasks:
            self.assertNotIn("🟠", t)


# ==========================================================================
# R2 — end-to-end: серия из 2 встреч (1-я табличная) → раздел во 2-й
# ==========================================================================
class TestSyntheticSeriesPendingSection(unittest.TestCase):

    def test_table_meeting_feeds_carryover_into_next_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            series_dir = root / "marketplaces-tatiana"
            series_dir.mkdir()
            # Встреча 1 (табличный 1:1): выжимка с непустым хвостом.
            d1 = sm.build_digest(_PROTO_TABLE_1TO1,
                                 {"series": "marketplaces-tatiana", "date": "2026-06-03"})
            sm.save_digest(series_dir, "2026-06-03", d1)
            self.assertTrue(d1.get("open_tasks"))
            # Встреча 2: резолвим память серии → строим блок висяков → в промпт.
            digests = sm.resolve_memory(series_dir, root, current_date="2026-06-10")
            self.assertTrue(digests)
            block = sm.build_open_tasks_block(digests, meeting_sid="iss23-e2e")
            self.assertIn("## 🔻 С прошлых встреч", block)
            self.assertIn("Ozon-доставку", block)        # перенесённый пункт из таблицы
            prompt = lp._format_protocol_user_prompt("транскрипт", _META, open_tasks=block)
            self.assertIn("## 🔻 С прошлых встреч", prompt)
            self.assertIn("Ozon-доставку", prompt)
            # инструкция-раздел идёт ПЕРЕД транскриптом
            self.assertLess(prompt.index("Ozon-доставку"), prompt.index("Транскрипт:"))

    def test_log_only_counters_for_table_tasks(self):
        # Опасная тройка: текст табличной задачи не уходит в лог (только счётчик).
        secret = "Ozon-доставку"
        d1 = sm.build_digest(_PROTO_TABLE_1TO1,
                             {"series": "marketplaces-tatiana", "date": "2026-06-03"})
        digests = [d1]
        with self.assertLogs("meeting_notary.series_memory", level="INFO") as cm:
            sm.build_open_tasks_block(digests, meeting_sid="iss23-log")
        blob = "\n".join(cm.output)
        self.assertIn("carried=", blob)
        self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
