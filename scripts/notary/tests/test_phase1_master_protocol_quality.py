"""Ф1 плана bot-notarius-master — качество протокола.

Покрывает доработки, которые сотрудники чистили руками после боевого 02.06:
  • FU-11  — доменный глоссарий: пост-проход замен по сгенерированному протоколу
            + наличие глоссария в промпте генерации.
  • FU-12  — тело протокола (`**Длительность:**`) и подпись/шапка показывают одно
            согласованное чистое время.
  • FU-INBOX4 — перед уточняющим вопросом бот сверяется с people.md /
            expected_participants и не переспрашивает про известного участника
            (регрессия на инцидент 02.06 п.4: «Михаил Саргин есть в базе»).

FU-10 (тёзки в подписи) покрыт в `tests/test_protocol_to_tg.py`
(TestCaptionParticipants).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import glossary  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402
from lib import protocol_to_tg as ptg  # noqa: E402


# ===========================================================================
# FU-11 — доменный глоссарий
# ===========================================================================
class TestGlossaryCorrections(unittest.TestCase):
    """Пост-проход замен (`apply_glossary_corrections`) — точность важнее полноты."""

    def test_garsia_to_rsya(self):
        """«Гарсия» (галлюцинация ASR) → РСЯ, во всех склонениях, любой регистр."""
        self.assertEqual(
            glossary.apply_glossary_corrections("Лиды из Гарсии распределяем вручную."),
            "Лиды из РСЯ распределяем вручную.",
        )
        self.assertEqual(
            glossary.apply_glossary_corrections("гарсия даёт трафик"),
            "РСЯ даёт трафик",
        )
        self.assertEqual(
            glossary.apply_glossary_corrections("бюджет на Гарсию"),
            "бюджет на РСЯ",
        )

    def test_letnee_hranenie_to_paletnoe(self):
        """«летнее хранение» → «палетное хранение» (биграмма, безопасно)."""
        self.assertEqual(
            glossary.apply_glossary_corrections("склад под летнее хранение занят"),
            "склад под палетное хранение занят",
        )
        self.assertEqual(
            glossary.apply_glossary_corrections("тариф летнего хранения вырос"),
            "тариф палетного хранения вырос",
        )

    def test_does_not_touch_legit_words(self):
        """Безопасность: легитимные «следов»/«летнее»/«летнего» без контекста — не трогаем."""
        for legit in (
            "Шли по следам конкурентов.",
            "Запустили летнее меню в кафе.",
            "Ждём летнего отпуска.",
            "Следов взлома не нашли.",
        ):
            self.assertEqual(glossary.apply_glossary_corrections(legit), legit)

    def test_idempotent(self):
        """Повторный прогон ничего не меняет."""
        once = glossary.apply_glossary_corrections("Гарсия и летнее хранение")
        twice = glossary.apply_glossary_corrections(once)
        self.assertEqual(once, twice)
        self.assertEqual(once, "РСЯ и палетное хранение")

    def test_empty_input(self):
        self.assertEqual(glossary.apply_glossary_corrections(""), "")
        self.assertEqual(glossary.apply_glossary_corrections(None), None)

    def test_prompt_block_lists_key_terms_and_rule(self):
        """Глоссарий в промпте содержит ключевые термины и правило «не выдумывай»."""
        block = glossary.PROJECT_GLOSSARY_PROMPT_BLOCK
        for term in ("РСЯ", "палетное", "ВКЛ", "ЭДО", "ковенанта", "лиды"):
            self.assertIn(term, block)
        self.assertIn("ошибка распознавания", block)
        self.assertIn("Не добавляй термины", block)


# ===========================================================================
# FU-12 — длительность в теле == чистое время в шапке/подписи
# ===========================================================================
# Свежая встреча: чистое время речи 42 мин (recording.first/lastSpeechMs).
_META_42 = {
    "series": "anzhee-coordination",
    "date": "2026-06-02",
    "duration": 62,  # календарное wall-time, которое раньше утекало в тело
    "expectedParticipants": ["Илья Рыбалка", "Михаил Еремеев"],
    "participants": [],
    "recording": {"firstSpeechMs": 0, "lastSpeechMs": 42 * 60 * 1000},
}

_PROTOCOL_62 = (
    "#протоколвстречи 02.06.2026\n\n"
    "**Встреча:** Координация — статус.\n\n"
    "**Длительность:** 62 мин\n\n"
    "**Участники:** Илья Рыбалка, Михаил Еремеев\n\n"
    "**Транскрипт:** [2026-06-02.md](2026-06-02.md)\n\n"
    "---\n\n"
    "## 1) Лиды\n\n"
    "▪️ Источник — Гарсия; склад под летнее хранение занят.\n"
)


class TestDurationNormalization(unittest.TestCase):

    def test_body_duration_rewritten_to_clean_time(self):
        """`**Длительность:** 62 мин` (wall) → 42 мин (чистое речевое)."""
        out = lp._normalize_protocol_duration(_PROTOCOL_62, _META_42)
        self.assertIn("**Длительность:** 42 мин", out)
        self.assertNotIn("62 мин", out)

    def test_body_matches_caption_value(self):
        """Главный критерий FU-12: одно согласованное значение в теле и подписи."""
        out = lp._normalize_protocol_duration(_PROTOCOL_62, _META_42)
        clean = ptg.compute_duration_label(_META_42)  # «42 мин»
        # тело
        self.assertIn(f"**Длительность:** {clean}", out)
        # подпись PDF строит ту же величину (с ~)
        caption = ptg.build_pdf_caption(out, _META_42)
        self.assertIn(f"Чистое время обсуждения: ~{clean}", caption)

    def test_unknown_clean_time_keeps_body(self):
        """Без recording.* и без endTs (чистое='—') — тело НЕ затираем на «—»."""
        meta_unknown = {"series": "x", "date": "2026-06-02"}
        out = lp._normalize_protocol_duration(_PROTOCOL_62, meta_unknown)
        self.assertEqual(out, _PROTOCOL_62)  # без изменений

    def test_no_duration_field_no_crash(self):
        """Шапка без `**Длительность:**` — текст не падает и не портится."""
        txt = "#протоколвстречи 02.06.2026\n\n**Встреча:** Без поля.\n"
        self.assertEqual(lp._normalize_protocol_duration(txt, _META_42), txt)

    def test_presence_only_not_written_to_body(self):
        """Регресс цикла5: старая встреча без `recording.*`, но с календарными
        `startTs/endTs` — тело НЕ переписываем на присутствие (wall-time). Иначе
        подпись (чистое время из transcript-json) и тело разойдутся — ровно то
        противоречие «два числа», которое FU-12 призван убрать."""
        meta_presence = {
            "series": "x",
            "date": "2026-06-02",
            "startTs": "2026-06-02T10:00:00Z",
            "endTs": "2026-06-02T11:02:00Z",  # присутствие 62 мин, recording.* нет
        }
        out = lp._normalize_protocol_duration(_PROTOCOL_62, meta_presence)
        self.assertEqual(out, _PROTOCOL_62)  # тело не тронуто


# ===========================================================================
# FU-11 + FU-12 вместе — интеграция через generate_protocol (mock LLM)
# ===========================================================================
class TestGenerateProtocolPostProcessing(unittest.TestCase):

    def test_generate_applies_glossary_and_duration(self):
        """generate_protocol прогоняет глоссарий + нормализацию длительности."""
        with mock.patch.object(lp, "call_claude_print", return_value=_PROTOCOL_62):
            out = lp.generate_protocol(
                "транскрипт", _META_42, method_text="МЕТОД", meeting_sid="t1",
            )
        # FU-11
        self.assertIn("РСЯ", out)
        self.assertIn("палетное хранение", out)
        self.assertNotIn("Гарси", out)
        self.assertNotIn("летнее хранение", out)
        # FU-12
        self.assertIn("**Длительность:** 42 мин", out)
        self.assertNotIn("62 мин", out)

    def test_system_prompt_includes_glossary(self):
        """Глоссарий действительно подмешан в system-prompt генерации."""
        captured = {}

        def _fake(user_prompt, *, system, timeout, model):
            captured["system"] = system
            return _PROTOCOL_62

        with mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            lp.generate_protocol("т", _META_42, method_text="МЕТОД", meeting_sid="t2")
        self.assertIn("Глоссарий проекта", captured["system"])
        self.assertIn("РСЯ", captured["system"])


# ===========================================================================
# FU-INBOX4 — сверка с people.md / expected перед уточняющим вопросом
# ===========================================================================
class TestKnownSpeakerNotReasked(unittest.TestCase):
    """Инцидент 02.06 п.4: «Михаил Саргин есть в people.md — не переспрашивай»."""

    def test_single_known_speaker_auto_resolved_not_asked(self):
        """1 неопознанный кластер + 1 известный кандидат → авто-подстановка, без вопроса."""
        # Илья уже привязан к другому кластеру (occupied); остаётся один
        # неопознанный кластер и один свободный известный кандидат — Михаил Саргин.
        auto = lp.auto_resolve_known_speakers(
            {"SPEAKER_01": {"speaker_label_in_md": "Спикер 2"}},
            resolved_names=["Илья Рыбалка"],
            name_pool=["Илья Рыбалка", "Михаил Саргин"],
            people_names=["Илья Рыбалка", "Михаил Саргин"],
            expected_participants=["Илья Рыбалка", "Михаил Саргин"],
        )
        # бот НЕ переспрашивает — подставил известного сам.
        self.assertEqual(auto, {"SPEAKER_01": "Михаил Саргин"})

    def test_two_namesakes_still_asked_no_wrong_guess(self):
        """Двое «Михаил» с коротким именем — не угадываем (защита от FU-10-ошибки)."""
        auto = lp.auto_resolve_known_speakers(
            {"SPEAKER_00": {}, "SPEAKER_01": {}},
            resolved_names=[],
            name_pool=["Михаил", "Михаил"],
            people_names=["Михаил Еремеев", "Михаил Саргин"],
            expected_participants=["Михаил Еремеев", "Михаил Саргин"],
        )
        self.assertEqual(auto, {})  # отдаём Илье на clarify, не путаем тёзок


if __name__ == "__main__":
    unittest.main()
