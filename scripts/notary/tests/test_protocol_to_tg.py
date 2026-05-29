"""Тесты для `lib.protocol_to_tg` — формат TG-сообщения + smart-split.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_protocol_to_tg -v

Smoke-Вариант А (упрощённый) для Ф1: парсим РЕАЛЬНЫЙ
`~/Projects/me/встречи/oneoff-vstrecha-po-vaibkodingu-e57601/2026-05-29-protokol.md`
и проверяем, что финальный TG-текст соответствует 5 правкам владельца:
  1. Маркер буллетов `•` (правка #1).
  2. Дата DD.MM.YYYY в шапке (правка #2).
  3. Хэштег на 2-й строке (правка #3).
  4. Имя Фамилия в участниках (правка #4 — резолв через people.md).
  5. Длительность присутствует (правка #5, fallback — endTs-startTs / durationLabel).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# tests/ → notary/ — импортируем `lib.protocol_to_tg` как из finalize-meeting.py.
_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import protocol_to_tg as ptg  # noqa: E402


REAL_PROTOCOL = Path(
    "/Users/ilarybalka/Projects/me/встречи/oneoff-vstrecha-po-vaibkodingu-e57601/"
    "2026-05-29-protokol.md"
)


SAMPLE_PROTOCOL = """#протоколвстречи 29.05.2026

**Встреча:** Тестовая встреча — короткое описание.

**Длительность:** 45 мин

**Участники:** Михаил, Илья Рыбалка

**Транскрипт:** [2026-05-29.md](2026-05-29.md)

---

## 1) Первый раздел

▪️ Первый буллет.

▫️ Второй буллет.

---

## 2) Второй раздел

▪️ Ещё буллет.

---

## Решения

🔸 Решение №1.

---

## Задачи

**Илья Рыбалка**

- Задача один к понедельнику.

- Задача два.

**Михаил**

- Задача Михаилу.
"""


META_SAMPLE = {
    "series": "oneoff-vstrecha-po-vaibkodingu-e57601",
    "date": "2026-05-29",
    "startTs": "2026-05-29T09:00:00Z",
    "endTs": "2026-05-29T09:45:00Z",
    "expectedParticipants": ["Илья Рыбалка"],
    "participants": ["Михаил"],
    "sessionUid": "auto-tm-test-20260529T090000Z",
}


class TestFormatProtocolAsTgText(unittest.TestCase):

    def test_header_first_two_lines(self):
        """Шапка: 1-я строка — заголовок с датой, 2-я — хэштег (правка #3)."""
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("📋 ПРОТОКОЛ ВСТРЕЧИ — "))
        self.assertIn("29.05.2026", lines[0])  # правка #2: DD.MM.YYYY
        self.assertEqual(lines[1], "#протоколвстречи")  # правка #3

    def test_bullet_marker_is_dot(self):
        """Все буллеты заменены на `•` (правка #1)."""
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        # Не должно быть исходных маркеров после форматтера.
        for old in ("▪️", "▫️", "🔸", "🟠"):
            self.assertNotIn(old, text, f"исходный маркер {old!r} не заменён")
        # Должен быть `• ` хотя бы один раз.
        self.assertIn("• ", text)

    def test_emoji_section_indicators(self):
        """Секции — 1️⃣2️⃣ + ✅ РЕШЕНИЯ + 📌 ЗАДАЧИ."""
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        self.assertIn("1️⃣ ПЕРВЫЙ РАЗДЕЛ", text)
        self.assertIn("2️⃣ ВТОРОЙ РАЗДЕЛ", text)
        self.assertIn("✅ РЕШЕНИЯ", text)
        self.assertIn("📌 ЗАДАЧИ", text)

    def test_tasks_section_has_name_blocks(self):
        """В «📌 ЗАДАЧИ» имена с двоеточием — отдельной строкой."""
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        self.assertIn("Илья Рыбалка:", text)
        self.assertIn("Михаил:", text)

    def test_duration_label_uses_fallback(self):
        """При наличии startTs/endTs (Ф1 fallback) длительность подставляется."""
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        # 45 мин — что выдаст _format_ms((endTs - startTs).total_seconds() * 1000).
        self.assertIn("45 мин", text)
        self.assertIn("⏱", text)

    def test_participants_resolved_when_possible(self):
        """Участники в шапке `👥 …` — Имя Фамилия (правка #4).

        people.md существует, в нём есть «Михаил Еремеев». Резолв должен
        превратить «Михаил» → «Михаил Еремеев», ЕСЛИ нет коллизии (там есть
        ещё «Михаил Саргин» — должно сработать defensive fallback).
        """
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        self.assertIn("👥 ", text)
        # «Илья Рыбалка» в meta.expectedParticipants — попадает в шапку как есть.
        self.assertIn("Илья Рыбалка", text)
        # Для «Михаил» — в people.md 2 матча, fallback на короткое имя.
        # Главное — никаких ошибок и имя есть.

    def test_no_markdownv2_escape(self):
        """Никаких `\\.` `\\-` `\\(` экранов (правка владельца — плоский текст)."""
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        # Простой sanity-check.
        self.assertNotIn("\\.", text)
        self.assertNotIn("\\-", text)
        self.assertNotIn("\\(", text)

    @unittest.skipUnless(REAL_PROTOCOL.exists(), f"требует {REAL_PROTOCOL}")
    def test_real_protocol_does_not_crash(self):
        """Smoke на реальном `<date>-protokol.md` от 29.05."""
        raw = REAL_PROTOCOL.read_text(encoding="utf-8")
        meta = {
            "series": "oneoff-vstrecha-po-vaibkodingu-e57601",
            "date": "2026-05-29",
            "startTs": "2026-05-29T11:00:00Z",
            "endTs": "2026-05-29T12:05:00Z",
            "expectedParticipants": ["Илья Рыбалка"],
            "participants": ["Михаил"],
            "sessionUid": "real-smoke",
        }
        text = ptg.format_protocol_as_tg_text(raw, meta)
        self.assertTrue(text.startswith("📋 ПРОТОКОЛ ВСТРЕЧИ"))
        self.assertIn("#протоколвстречи", text.splitlines()[1])
        self.assertIn("Дилерский кабинет".upper(), text.upper())
        self.assertIn("✅ РЕШЕНИЯ", text)
        self.assertIn("📌 ЗАДАЧИ", text)
        self.assertIn("• ", text)
        # Длительность fallback из endTs-startTs = 1 ч 05 мин.
        self.assertIn("1 ч 05 мин", text)


class TestSplitProtocolSmart(unittest.TestCase):

    def test_short_returns_single_part(self):
        out = ptg.split_protocol_smart("привет мир", max_len=100)
        self.assertEqual(out, ["привет мир"])

    def test_empty_returns_empty(self):
        self.assertEqual(ptg.split_protocol_smart(""), [])

    def test_split_keeps_header_with_first_chunk(self):
        """Шапка `📋` остаётся в первом чанке."""
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        # Принудим к разбиению крошечным max_len.
        out = ptg.split_protocol_smart(text, max_len=200)
        self.assertTrue(out[0].startswith("📋 ПРОТОКОЛ ВСТРЕЧИ"))
        self.assertIn("#протоколвстречи", out[0])

    def test_no_chunk_exceeds_max_len(self):
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        for max_len in (100, 200, 500, 4096):
            chunks = ptg.split_protocol_smart(text, max_len=max_len)
            for ch in chunks:
                self.assertLessEqual(len(ch), max_len, f"chunk > max_len={max_len}")

    def test_section_boundary_not_cut(self):
        """При разделении секция-маркер начинает новый чанк, не разрывается в середине."""
        text = ptg.format_protocol_as_tg_text(SAMPLE_PROTOCOL, META_SAMPLE)
        out = ptg.split_protocol_smart(text, max_len=300)
        # У каждого чанка (кроме первого с шапкой) должна быть секционная граница на старте.
        for i, ch in enumerate(out):
            if i == 0:
                continue
            head = ch.lstrip().splitlines()[0]
            ok = any(
                head.startswith(p)
                for p in (
                    "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "✅", "📌",
                )
            )
            self.assertTrue(
                ok,
                f"чанк #{i} не начинается с секции: {head!r}",
            )

    def test_tasks_section_continuation_header(self):
        """Огромная секция «📌 ЗАДАЧИ» получает «(продолжение)» в следующем чанке."""
        # Сгенерим протокол с большим списком задач, чтобы заведомо превысить max_len.
        many_tasks = "\n\n".join([f"- Задача номер {i} с подробностями." for i in range(40)])
        proto = (
            "#протоколвстречи 29.05.2026\n\n"
            "**Встреча:** Тест разрыва Задач.\n\n"
            "**Длительность:** 30 мин\n\n"
            "**Участники:** Илья Рыбалка\n\n"
            "---\n\n"
            "## Задачи\n\n"
            "**Илья Рыбалка**\n\n"
            f"{many_tasks}\n"
        )
        meta = {
            "date": "2026-05-29",
            "expectedParticipants": ["Илья Рыбалка"],
            "participants": [],
            "startTs": "2026-05-29T09:00:00Z",
            "endTs": "2026-05-29T09:30:00Z",
        }
        text = ptg.format_protocol_as_tg_text(proto, meta)
        out = ptg.split_protocol_smart(text, max_len=400)
        # Должно быть >= 2 чанков, и хотя бы один — с пометкой «продолжение».
        self.assertGreater(len(out), 1)
        has_continuation = any("📌 ЗАДАЧИ (продолжение)" in ch for ch in out)
        self.assertTrue(has_continuation, "должна быть метка «продолжение» в split'е задач")


class TestComputeDurationLabel(unittest.TestCase):

    def test_priority_chunks(self):
        """chunks[] имеет приоритет над firstSpeechMs/lastSpeechMs (Ф5)."""
        meta = {
            "recording": {
                "chunks": [
                    {"firstSpeechMs": 1000, "lastSpeechMs": 61000},  # 60s
                    {"firstSpeechMs": 0, "lastSpeechMs": 120000},  # 120s = 2 мин
                ],
                # Эти поля должны игнорироваться при наличии chunks.
                "firstSpeechMs": 10000,
                "lastSpeechMs": 70000,
            },
        }
        # 60s + 120s = 180s = 3 мин.
        self.assertEqual(ptg.compute_duration_label(meta), "3 мин")

    def test_priority_first_last(self):
        """firstSpeechMs/lastSpeechMs — следующий приоритет (Ф3)."""
        meta = {
            "recording": {
                "firstSpeechMs": 0,
                "lastSpeechMs": 45 * 60 * 1000,  # 45 мин
            },
            # Должно игнорироваться при наличии recording.firstSpeechMs.
            "startTs": "2026-05-29T09:00:00Z",
            "endTs": "2026-05-29T11:00:00Z",  # 2 часа в звонке
        }
        self.assertEqual(ptg.compute_duration_label(meta), "45 мин")

    def test_fallback_endts_startts(self):
        """endTs - startTs — fallback Ф1."""
        meta = {
            "startTs": "2026-05-29T09:00:00Z",
            "endTs": "2026-05-29T10:05:00Z",
        }
        self.assertEqual(ptg.compute_duration_label(meta), "1 ч 05 мин")

    def test_unknown_returns_dash(self):
        self.assertEqual(ptg.compute_duration_label({}), "—")


class TestResolveFullName(unittest.TestCase):

    def test_already_full_name_passthrough(self):
        self.assertEqual(
            ptg._resolve_full_name("Илья Рыбалка", ["Илья Рыбалка", "Михаил Еремеев"]),
            "Илья Рыбалка",
        )

    def test_single_match_resolves(self):
        self.assertEqual(
            ptg._resolve_full_name("Ольга", ["Ольга Новикова", "Михаил Еремеев"]),
            "Ольга Новикова",
        )

    def test_multiple_matches_fallback_to_first(self):
        """N>1 совпадений по имени → fallback на короткое имя (РАЗМ2)."""
        self.assertEqual(
            ptg._resolve_full_name("Михаил", ["Михаил Еремеев", "Михаил Саргин"]),
            "Михаил",
        )

    def test_no_match_returns_input(self):
        self.assertEqual(
            ptg._resolve_full_name("Незнакомец", ["Михаил Еремеев"]),
            "Незнакомец",
        )

    def test_owner_fallback_ilia(self):
        """Илья → Илья Рыбалка через _OWNER_FALLBACK, если не в people.md."""
        self.assertEqual(
            ptg._resolve_full_name("Илья", []),
            "Илья Рыбалка",
        )


if __name__ == "__main__":
    unittest.main()
