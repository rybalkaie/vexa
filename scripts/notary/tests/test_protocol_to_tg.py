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
from unittest import mock

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
            ptg._resolve_full_name("Ilya R.", []), "Илья Рыбалка",
        )
        self.assertEqual(
            ptg._resolve_full_name("Ilya", []), "Илья Рыбалка",
        )
        self.assertEqual(
            ptg._resolve_full_name("Илья Р.", []), "Илья Рыбалка",
        )
        self.assertEqual(
            ptg._resolve_full_name("Илья", []),
            "Илья Рыбалка",
        )


class TestCleanSpeechFromRawJson(unittest.TestCase):
    """Хелперы чистого времени по сырому Speechmatics-ответу (Ф1-доработки)."""

    def test_bounds_and_clean_basic(self):
        raw = {"results": [
            {"type": "word", "start_time": 5.0, "end_time": 5.4,
             "alternatives": [{"content": "a"}]},
            {"type": "punctuation", "start_time": 5.4, "end_time": 5.4,
             "alternatives": [{"content": "."}]},
            {"type": "word", "start_time": 60.0, "end_time": 61.0,
             "alternatives": [{"content": "b"}]},
        ]}
        self.assertEqual(ptg.speech_bounds_ms_from_raw_json(raw), (5000, 61000))
        self.assertEqual(ptg.clean_speech_ms_from_raw_json(raw), 56000)

    def test_punctuation_does_not_extend_bounds(self):
        """Пунктуация позже последнего слова НЕ растягивает lastSpeechMs."""
        raw = {"results": [
            {"type": "word", "start_time": 1.0, "end_time": 2.0,
             "alternatives": [{"content": "x"}]},
            {"type": "punctuation", "start_time": 9.0, "end_time": 9.0,
             "alternatives": [{"content": "?"}]},
        ]}
        self.assertEqual(ptg.speech_bounds_ms_from_raw_json(raw), (1000, 2000))

    def test_empty_results_returns_none(self):
        self.assertIsNone(ptg.clean_speech_ms_from_raw_json({"results": []}))
        self.assertIsNone(ptg.speech_bounds_ms_from_raw_json({"results": []}))

    def test_no_words_returns_none(self):
        raw = {"results": [
            {"type": "punctuation", "start_time": 1.0, "end_time": 1.0,
             "alternatives": [{"content": "."}]},
        ]}
        self.assertIsNone(ptg.clean_speech_ms_from_raw_json(raw))

    def test_broken_input_returns_none(self):
        for bad in (None, [], "x", {}, {"results": "nope"}, 42):
            self.assertIsNone(ptg.clean_speech_ms_from_raw_json(bad))
            self.assertIsNone(ptg.speech_bounds_ms_from_raw_json(bad))

    def test_garbage_timings_skipped(self):
        """Нечисловые тайминги пропускаются, функция не падает."""
        raw = {"results": [
            {"type": "word", "start_time": "oops", "end_time": 5.0,
             "alternatives": [{"content": "a"}]},
            {"type": "word", "start_time": 10.0, "end_time": 12.0,
             "alternatives": [{"content": "b"}]},
        ]}
        self.assertEqual(ptg.speech_bounds_ms_from_raw_json(raw), (10000, 12000))

    def test_negative_word_duration_normalized(self):
        """end_time < start_time → end нормализуется к start (нет отриц. речи)."""
        raw = {"results": [
            {"type": "word", "start_time": 10.0, "end_time": 9.0,
             "alternatives": [{"content": "a"}]},
        ]}
        self.assertEqual(ptg.speech_bounds_ms_from_raw_json(raw), (10000, 10000))
        # Один word, end==start → интервал 0 → None.
        self.assertIsNone(ptg.clean_speech_ms_from_raw_json(raw))


_FIXTURE_CLEAN = _HERE / "fixtures" / "transcript_clean_time_sample.json"


class TestDurationLabelTranscriptJson(unittest.TestCase):
    """Источник «транскрипт-json» в compute_duration_label (REQ 2.1/2.3)."""

    def test_tatyana_case_clean_below_presence(self):
        """REQ 2.1: присутствие 1ч42, первая реплика 5:00 → подпись 1ч37."""
        meta = {
            # Присутствие бота = endTs - startTs = 1 ч 42 мин («грязное» время).
            "startTs": "2026-06-03T10:00:00Z",
            "endTs": "2026-06-03T11:42:00Z",
            # recording.* НЕТ — старая встреча, чистое время берётся из json.
        }
        label = ptg.compute_duration_label(
            meta, transcript_json_path=str(_FIXTURE_CLEAN)
        )
        self.assertEqual(label, "1 ч 37 мин")
        # Чистое время строго меньше присутствия, и это НЕ присутствие.
        self.assertEqual(ptg.compute_duration_label(meta), "1 ч 42 мин")
        self.assertNotEqual(label, ptg.compute_duration_label(meta))

    def test_recording_fields_win_over_transcript_json(self):
        """REQ 2.3: транскрипт-json ПОСЛЕ recording.firstSpeechMs, но ПЕРЕД endTs-startTs."""
        meta_with_rec = {
            "recording": {"firstSpeechMs": 0, "lastSpeechMs": 30 * 60 * 1000},  # 30 мин
            "startTs": "2026-06-03T10:00:00Z",
            "endTs": "2026-06-03T11:42:00Z",
        }
        self.assertEqual(
            ptg.compute_duration_label(
                meta_with_rec, transcript_json_path=str(_FIXTURE_CLEAN)
            ),
            "30 мин",
        )

    def test_missing_json_warns_and_falls_back_to_presence(self):
        """Битый/отсутствующий json → warning + fallback на присутствие (не молча)."""
        meta = {
            "startTs": "2026-06-03T10:00:00Z",
            "endTs": "2026-06-03T11:05:00Z",  # 1 ч 05 мин
        }
        bad_path = str(_HERE / "fixtures" / "does-not-exist.json")
        with self.assertLogs("lib.protocol_to_tg", level="WARNING") as cm:
            label = ptg.compute_duration_label(meta, transcript_json_path=bad_path)
        self.assertEqual(label, "1 ч 05 мин")
        self.assertTrue(
            any("transcript json missing" in m for m in cm.output),
            f"ожидали warning про missing transcript json, получили: {cm.output}",
        )

    def test_no_path_uses_presence_without_warning(self):
        """Без пути — поведение как раньше (присутствие), без лишнего warning."""
        meta = {
            "startTs": "2026-06-03T10:00:00Z",
            "endTs": "2026-06-03T11:05:00Z",
        }
        self.assertEqual(ptg.compute_duration_label(meta), "1 ч 05 мин")


class TestResolveSeriesDisplayName(unittest.TestCase):
    """РАЗМ2: slug серии → человекочитаемое имя для caption."""

    def test_override_marketplaces(self):
        self.assertEqual(
            ptg.resolve_series_display_name({"series": "marketplaces-tatiana"}),
            "Маркетплейсы (Татьяна)",
        )

    def test_override_anzhee(self):
        self.assertEqual(
            ptg.resolve_series_display_name({"series": "anzhee-direktorat"}),
            "Директорат Anzhee",
        )

    def test_explicit_meta_field_wins(self):
        self.assertEqual(
            ptg.resolve_series_display_name(
                {"series": "anzhee-direktorat", "seriesTitle": "Кастомное имя"}
            ),
            "Кастомное имя",
        )

    def test_theme_derived_for_unmapped(self):
        """Неизвестная серия с темой `Имя — расшифровка` → имя до « — »."""
        header = {"theme": "Синхронизация по вайб-кодингу — текущие проекты"}
        self.assertEqual(
            ptg.resolve_series_display_name(
                {"series": "oneoff-vaibkoding-e57601"}, parsed_header=header
            ),
            "Синхронизация по вайб-кодингу",
        )

    def test_humanize_slug_fallback_warns(self):
        """Нет маппинга и нет темы → гуманизированный slug + warning."""
        with self.assertLogs("lib.protocol_to_tg", level="WARNING") as cm:
            name = ptg.resolve_series_display_name({"series": "weekly-sync-abcd12"})
        self.assertEqual(name, "Weekly sync")  # хвост-хэш отрезан, дефисы→пробел
        self.assertTrue(any("display-маппинг" in m for m in cm.output))

    def test_config_overrides_defaults(self, ):
        """`_config/series-display.json` перебивает хардкод-дефолты."""
        with mock.patch.object(
            ptg, "_load_series_display_config",
            return_value={"anzhee-direktorat": "Совет директоров"},
        ):
            self.assertEqual(
                ptg.resolve_series_display_name({"series": "anzhee-direktorat"}),
                "Совет директоров",
            )


class TestCaptionParticipants(unittest.TestCase):
    """Участники caption: expected — как есть, UI — обогащаем (РАЗМ2/эталон)."""

    def test_expected_names_not_enriched(self):
        """«Татьяна» из watched.yaml НЕ обогащается до «Татьяна Филиппова»."""
        out = ptg._caption_participants({
            "expectedParticipants": ["Илья Рыбалка", "Татьяна"],
            "participants": [],
        })
        self.assertEqual(out, ["Илья Рыбалка", "Татьяна"])

    def test_ui_bare_name_enriched(self):
        """Голое имя из Telemost UI обогащается через people.md (single match)."""
        with mock.patch.object(
            ptg, "_read_people_md", return_value="- **Ольга Новикова** — роль"
        ):
            out = ptg._caption_participants({
                "expectedParticipants": ["Илья Рыбалка"],
                "participants": ["Ольга"],
            })
        self.assertEqual(out, ["Илья Рыбалка", "Ольга Новикова"])

    def test_locked_first_name_not_overridden_by_ui(self):
        """Курируемое имя не перетирается одноимённым UI-именем."""
        with mock.patch.object(
            ptg, "_read_people_md", return_value="- **Татьяна Филиппова** — роль"
        ):
            out = ptg._caption_participants({
                "expectedParticipants": ["Татьяна"],
                "participants": ["Татьяна"],
            })
        self.assertEqual(out, ["Татьяна"])

    def test_two_namesakes_in_expected_not_collapsed(self):
        """FU-10 (🔴): двое «Михаил» в expectedParticipants — оба остаются.

        Боевой кейс координации 02.06: «Михаил Еремеев» + «Михаил Саргин»
        раньше схлопывались по first-name в одного. Теперь — оба как есть.
        """
        out = ptg._caption_participants({
            "expectedParticipants": [
                "Илья Рыбалка", "Михаил Еремеев", "Михаил Саргин",
            ],
            "participants": [],
        })
        self.assertEqual(out, ["Илья Рыбалка", "Михаил Еремеев", "Михаил Саргин"])
        # Именно «не потеряли тёзку»: оба Михаила на месте.
        self.assertEqual(sum(1 for n in out if n.startswith("Михаил")), 2)

    def test_exact_duplicate_in_expected_dropped_once(self):
        """Точный дубль строки в курируемом списке схлопывается (но не тёзки)."""
        out = ptg._caption_participants({
            "expectedParticipants": ["Михаил Еремеев", "Михаил Еремеев"],
            "participants": [],
        })
        self.assertEqual(out, ["Михаил Еремеев"])

    def test_namesakes_expected_blocks_bare_ui_namesake(self):
        """Двое курируемых «Михаил*» + bare «Михаил» из UI → UI-тёзка не лезет."""
        with mock.patch.object(
            ptg, "_read_people_md",
            return_value="- **Михаил Еремеев** — роль\n- **Михаил Саргин** — роль",
        ):
            out = ptg._caption_participants({
                "expectedParticipants": ["Михаил Еремеев", "Михаил Саргин"],
                "participants": ["Михаил"],
            })
        # Оба курируемых Михаила; bare-UI «Михаил» (first-name locked) не добавлен.
        self.assertEqual(out, ["Михаил Еремеев", "Михаил Саргин"])

    def test_owner_latin_label_deduped(self):
        """«Ilya R.» из Telemost UI = владелец → НЕ дублируется с «Илья Рыбалка».

        Боевой кейс 03.06 (директорат/Татьяна): meta.participants=[«Ilya R.», …],
        expectedParticipants=[«Илья Рыбалка», …] — раньше в шапке появлялись ОБА
        (латиница не схлопывалась с кириллицей). Замечено владельцем 2026-06-06.
        """
        out = ptg._caption_participants({
            "expectedParticipants": ["Илья Рыбалка", "Михаил Еремеев"],
            "participants": ["Ilya R.", "Михаил"],
        })
        self.assertEqual(out.count("Илья Рыбалка"), 1, "владелец ровно один раз")
        self.assertNotIn("Ilya R.", out, "латинская метка не должна остаться")
        self.assertEqual(out[0], "Илья Рыбалка")


CAPTION_PROTO = (
    "#протоколвстречи 03.06.2026\n\n"
    "**Встреча:** Маркетплейсы — статус кабинетов.\n\n"
    "---\n\n"
    "## 1) Раздел\n\n"
    "▪️ Буллет.\n"
)


class TestBuildPdfCaption(unittest.TestCase):
    """REQ 3.1: 4-строчная подпись под PDF, совпадает с эталоном владельца."""

    META = {
        "series": "marketplaces-tatiana",
        "date": "2026-06-03",
        "expectedParticipants": ["Илья Рыбалка", "Татьяна"],
        "participants": [],
        # чистое время 1ч37 — single-chunk recording (Ф1 источник 2).
        "recording": {"firstSpeechMs": 0, "lastSpeechMs": 97 * 60 * 1000},
    }

    def test_caption_matches_owner_etalon(self):
        cap = ptg.build_pdf_caption(CAPTION_PROTO, self.META)
        etalon = (
            "📋 #протоколвстречи\n"
            "Маркетплейсы (Татьяна) — 03.06.2026\n"
            "Участники: Илья Рыбалка, Татьяна\n"
            "Чистое время обсуждения: ~1 ч 37 мин"
        )
        self.assertEqual(cap, etalon)

    def test_caption_is_exactly_four_lines(self):
        cap = ptg.build_pdf_caption(CAPTION_PROTO, self.META)
        self.assertEqual(len(cap.splitlines()), 4)

    def test_hashtag_merged_on_first_line(self):
        cap = ptg.build_pdf_caption(CAPTION_PROTO, self.META)
        self.assertEqual(cap.splitlines()[0], "📋 #протоколвстречи")
        self.assertNotIn("#протокол_встречи", cap)  # слитно, не с подчёркиванием

    def test_unknown_duration_no_tilde(self):
        """Без источника времени — «: —» без «~—»."""
        cap = ptg.build_pdf_caption(CAPTION_PROTO, {
            "series": "marketplaces-tatiana", "date": "2026-06-03",
            "expectedParticipants": ["Илья Рыбалка"], "participants": [],
        })
        self.assertIn("Чистое время обсуждения: —", cap)
        self.assertNotIn("~—", cap)

    def test_title_subtitle(self):
        title, subtitle = ptg.build_pdf_title_subtitle(CAPTION_PROTO, self.META)
        self.assertEqual(title, "Маркетплейсы (Татьяна) — 03.06.2026")
        self.assertIn("Участники: Илья Рыбалка, Татьяна", subtitle)
        self.assertIn("Чистое время обсуждения: ~1 ч 37 мин", subtitle)


if __name__ == "__main__":
    unittest.main()
