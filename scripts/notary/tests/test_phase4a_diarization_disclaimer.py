"""Тесты Ф4а — начальная догадка авторства без инверсии + постоянный дисклеймер.

Покрывает REQ 1.1 / 1.3 плана `2026-06-06-dorabotki-notary-pre-live.md`:
  - 1.1  Бот выпускает лучшую ДОГАДКУ авторства без вопроса-стопа (A1). Для ровно
         2 спикеров начальная догадка (`name_mapping` Source 2) НЕ инвертируется:
         регресс директорат 03.06 (Илья↔Михаил) — раньше forward-голос
         переворачивал авторство, теперь решаем по строгим vocative-обращениям.
  - 1.3  Постоянный дисклеймер «перепутал — поправьте, учту» в начале КАЖДОГО
         протокола в .md / PDF / TG; не ломая `_caption_participants`, парсинг
         шапки и series_memory-дайджест (дисклеймер не как контент дайджеста).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase4a_diarization_disclaimer -v
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

# name_mapping → align → diarize/transcribe тянут тяжёлые пакеты (requests/httpx),
# которых нет в системном python мака/CI. Тестируемая логика их не использует —
# подкладываем минимальные стабы, если пакет реально отсутствует.
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib.align import AlignedTurn  # noqa: E402
from lib import name_mapping as nm  # noqa: E402
from lib import protocol_to_tg as ptg  # noqa: E402
from lib import series_memory as sm  # noqa: E402
from lib import protocol_to_pdf as ppdf  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402


def _turn(speaker: str, text: str) -> AlignedTurn:
    return AlignedTurn(start=0.0, end=1.0, speaker=speaker, text=text)


# Базовый протокол БЕЗ дисклеймера. Дисклеймер навешивает insert_protocol_disclaimer —
# ровно как в проде (generate_protocol). Производный «с дисклеймером» используем
# во всех тестах рендера/дайджеста, чтобы тест совпадал с реальным выходом.
PROTOCOL_NO_DISCLAIMER = (
    "#протоколвстречи 03.06.2026\n\n"
    "**Встреча:** Директорат — статус.\n\n"
    "**Длительность:** 45 мин\n\n"
    "**Участники:** Илья Рыбалка, Михаил Еремеев\n\n"
    "**Транскрипт:** [2026-06-03.md](2026-06-03.md)\n\n"
    "---\n\n"
    "## 1) Финансы\n\n"
    "▪️ Оборот за месяц **2,8 млн**.\n\n"
    "## 2) Склад\n\n"
    "▪️ Палетное хранение занято.\n\n"
    "## ✅ Решения\n\n"
    "🔸 Запустить вывоз на неделе.\n"
)
PROTOCOL_WITH_DISCLAIMER = ptg.insert_protocol_disclaimer(PROTOCOL_NO_DISCLAIMER)


# ==========================================================================
# REQ 1.1 — начальная догадка авторства для 2 спикеров без инверсии
# ==========================================================================
class TestTwoSpeakerInitialGuess(unittest.TestCase):
    """Source 2 (`map_from_speech_regex`): 2 спикера — по строгому vocative."""

    PARTS = ["Илья", "Михаил"]  # истина: SPEAKER_00=Илья, SPEAKER_01=Михаил

    def test_directorate_0306_regression_not_inverted(self):
        """🔴 Регресс директорат 03.06: forward-голос инвертировал — теперь верно.

        Илья (S0, ведущий) окликает Михаила строгим vocative один раз; Михаил
        (S1) трижды говорит о себе в третьем лице в начале реплики, после каждой
        отвечает Илья → forward-голос «следующий = Михаил» накапливался на S0 и
        старый greedy выдавал {S0: Михаил} (Илья↔Михаил перепутаны). Строгий
        anti («Илья окликнул Михаила → Илья НЕ Михаил») чинит детерминированно.
        """
        turns = [
            _turn("SPEAKER_00", "Михаил, давай начнём с финансов."),
            _turn("SPEAKER_01", "Михаил посчитал оборот."),
            _turn("SPEAKER_00", "Хорошо."),
            _turn("SPEAKER_01", "Михаил свёл по складу."),
            _turn("SPEAKER_00", "Понял."),
            _turn("SPEAKER_01", "Михаил закроет КТК."),
            _turn("SPEAKER_00", "Отлично, спасибо."),
        ]
        res = nm.map_from_speech_regex(turns, self.PARTS, {})
        self.assertEqual(res.get("SPEAKER_00"), "Илья")
        self.assertEqual(res.get("SPEAKER_01"), "Михаил")

    def test_reciprocal_vocatives_resolves_both(self):
        """Взаимные строгие обращения → оба кластера верно (без unresolved)."""
        turns = [
            _turn("SPEAKER_00", "Михаил, давай по бюджету."),
            _turn("SPEAKER_01", "Илья, готов, смотри цифры."),
            _turn("SPEAKER_00", "Михаил, и по отчёту что?"),
            _turn("SPEAKER_01", "Илья, отчёт почти готов."),
        ]
        res = nm.map_from_speech_regex(turns, self.PARTS, {})
        self.assertEqual(res, {"SPEAKER_00": "Илья", "SPEAKER_01": "Михаил"})

    def test_one_sided_vocative_resolves_both(self):
        """Односторонний строгий vocative (Илья окликает Михаила) → оба верно."""
        turns = [
            _turn("SPEAKER_00", "Михаил, посмотри таблицу."),
            _turn("SPEAKER_01", "Угу."),
            _turn("SPEAKER_00", "Михаил, и ещё момент по складу."),
            _turn("SPEAKER_01", "Понял."),
        ]
        res = nm.map_from_speech_regex(turns, self.PARTS, {})
        self.assertEqual(res, {"SPEAKER_00": "Илья", "SPEAKER_01": "Михаил"})

    def test_no_strict_vocative_defers_to_llm(self):
        """Нет строгого сигнала (только self-ref без запятой) → пусто (defer на LLM).

        Безопаснее «Спикер N» + дисклеймер, чем уверенно неверный автор (старый
        greedy здесь инвертировал на forward-голосах).
        """
        turns = [
            _turn("SPEAKER_01", "Михаил подготовил отчёт сейчас покажу."),
            _turn("SPEAKER_00", "Хорошо."),
            _turn("SPEAKER_01", "Михаил всё свёл по цифрам."),
            _turn("SPEAKER_00", "Илья тут добавит по складу."),
        ]
        res = nm.map_from_speech_regex(turns, self.PARTS, {})
        self.assertEqual(res, {})

    def test_three_speakers_use_greedy_path(self):
        """3 спикера — общий greedy не задет спец-случаем 2×2."""
        parts3 = ["Илья", "Михаил", "Ольга"]
        turns = [
            _turn("SPEAKER_00", "Михаил, твой блок."),
            _turn("SPEAKER_01", "Готов."),
            _turn("SPEAKER_00", "Ольга, а ты?"),
            _turn("SPEAKER_02", "Тоже готова."),
        ]
        res = nm.map_from_speech_regex(turns, parts3, {})
        self.assertEqual(res.get("SPEAKER_01"), "Михаил")
        self.assertEqual(res.get("SPEAKER_02"), "Ольга")

    def test_resolver_unit_strict_anti_decides(self):
        """`_resolve_two_speakers`: меньшее противоречие строгому anti выигрывает."""
        # S0 строго окликнул Михаила (anti=2) → S0 не Михаил → S0=Илья.
        strict = {("SPEAKER_00", "Михаил"): 2.0}
        out = nm._resolve_two_speakers(
            strict, ["SPEAKER_00", "SPEAKER_01"], ["Илья", "Михаил"]
        )
        self.assertEqual(out, {"SPEAKER_00": "Илья", "SPEAKER_01": "Михаил"})

    def test_resolver_unit_no_signal_returns_empty(self):
        """Нет строгого сигнала → {} (равные противоречия = неразличимо)."""
        out = nm._resolve_two_speakers(
            {}, ["SPEAKER_00", "SPEAKER_01"], ["Илья", "Михаил"]
        )
        self.assertEqual(out, {})

    def test_no_stop_question_path_in_finalize(self):
        """A1: путь генерации не содержит стоп-вопроса об авторстве спикеров.

        Source 2 либо возвращает догадку, либо пусто (→ LLM-добивка/«Спикер N»);
        ни одна ветка не запрашивает подтверждение и не блокирует выпуск.
        """
        # Возвращаемое — всегда dict (маппинг), не «вопрос»/исключение.
        self.assertIsInstance(
            nm.map_from_speech_regex(
                [_turn("SPEAKER_00", "Просто текст без имён."),
                 _turn("SPEAKER_01", "И ещё текст.")],
                self.PARTS, {},
            ),
            dict,
        )


# ==========================================================================
# REQ 1.3 — постоянный дисклеймер в .md / PDF / TG
# ==========================================================================
class TestDisclaimerInMarkdown(unittest.TestCase):
    """.md-канал: дисклеймер в теле протокола (через generate_protocol)."""

    def test_insert_puts_disclaimer_before_first_section(self):
        text = PROTOCOL_WITH_DISCLAIMER
        self.assertIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, text)
        disc_idx = text.index(ptg.PROTOCOL_DISCLAIMER_SENTINEL)
        sect_idx = text.index("## 1) Финансы")
        self.assertLess(disc_idx, sect_idx, "дисклеймер должен быть ДО первой секции")
        # И ПОСЛЕ строки-якоря протокола (в начале тела, не до шапки).
        self.assertLess(text.index("#протоколвстречи"), disc_idx)

    def test_insert_is_idempotent(self):
        once = ptg.insert_protocol_disclaimer(PROTOCOL_NO_DISCLAIMER)
        twice = ptg.insert_protocol_disclaimer(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.count(ptg.PROTOCOL_DISCLAIMER_MD), 1)

    def test_insert_fallback_without_separator(self):
        no_sep = "#протоколвстречи 03.06.2026\n\n## 1) Тема\n\n▪️ пункт\n"
        out = ptg.insert_protocol_disclaimer(no_sep)
        self.assertIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, out)
        self.assertLess(out.index(ptg.PROTOCOL_DISCLAIMER_SENTINEL), out.index("## 1) Тема"))

    def test_strip_round_trips(self):
        stripped = ptg.strip_protocol_disclaimer(PROTOCOL_WITH_DISCLAIMER)
        self.assertNotIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, stripped)
        # Содержимое протокола не пострадало.
        self.assertIn("## 1) Финансы", stripped)
        self.assertIn("**Участники:** Илья Рыбалка, Михаил Еремеев", stripped)

    def test_generate_protocol_injects_disclaimer(self):
        """Боевой путь: generate_protocol(...) на выходе содержит дисклеймер."""
        fake = (
            "#протоколвстречи 03.06.2026\n\n"
            "**Участники:** Илья Рыбалка\n\n"
            "---\n\n"
            "## 1) Тема\n\n▪️ пункт\n"
        )
        with mock.patch.object(lp, "call_claude_print", return_value=fake):
            out = lp.generate_protocol(
                "**[00:01] Илья:** привет",
                {"date": "2026-06-03", "participants": ["Илья Рыбалка"],
                 "expectedParticipants": ["Илья Рыбалка"]},
                method_text="（метод-стаб）",
                meeting_sid="t",
            )
        self.assertIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, out)
        self.assertLess(out.index(ptg.PROTOCOL_DISCLAIMER_SENTINEL), out.index("## 1) Тема"))


class TestDisclaimerInPdf(unittest.TestCase):
    """PDF-канал: дисклеймер доезжает до рендерера PDF (тело после шапки)."""

    def test_disclaimer_survives_strip_leading_heading(self):
        # markdown_to_html срезает только первую строку-якорь; дисклеймер остаётся.
        body = ppdf._strip_leading_heading(PROTOCOL_WITH_DISCLAIMER)
        self.assertIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, body)

    def test_disclaimer_in_rendered_html(self):
        try:
            import markdown  # noqa: F401
            import bleach  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("markdown/bleach не установлены в этом окружении")
        html = ppdf.markdown_to_html(
            PROTOCOL_WITH_DISCLAIMER, title="Тест", subtitle="подзаголовок",
        )
        self.assertIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, html)
        # blockquote сохраняется санитайзером (тег в allowlist).
        self.assertIn("<blockquote>", html)


class TestDisclaimerInTg(unittest.TestCase):
    """TG-канал: дисклеймер в тексте протокола (revision-путь)."""

    META = {
        "date": "2026-06-03",
        "expectedParticipants": ["Илья Рыбалка"],
        "participants": ["Михаил"],
    }

    def test_disclaimer_in_tg_text(self):
        text = ptg.format_protocol_as_tg_text(PROTOCOL_WITH_DISCLAIMER, self.META)
        self.assertIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, text)
        # Шапка по-прежнему первые две строки (дисклеймер — отдельным блоком ниже).
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("📋 ПРОТОКОЛ ВСТРЕЧИ"))
        self.assertEqual(lines[1], "#протоколвстречи")

    def test_no_double_disclaimer_in_tg(self):
        """В TG-тексте дисклеймер ровно один раз (не дублируется с тела .md)."""
        text = ptg.format_protocol_as_tg_text(PROTOCOL_WITH_DISCLAIMER, self.META)
        self.assertEqual(text.count(ptg.PROTOCOL_DISCLAIMER_SENTINEL), 1)


# ==========================================================================
# REQ 1.3 — дисклеймер НЕ ломает шапку / caption / series_memory-дайджест
# ==========================================================================
class TestDisclaimerDoesNotBreakHeaderOrDigest(unittest.TestCase):

    META = {
        "series": "anzhee-direktorat",
        "date": "2026-06-03",
        "expectedParticipants": ["Илья Рыбалка", "Михаил Еремеев"],
        "participants": [],
        "recording": {"firstSpeechMs": 0, "lastSpeechMs": 45 * 60 * 1000},
    }

    def test_parse_protocol_header_intact(self):
        parsed = ptg._parse_protocol_md(PROTOCOL_WITH_DISCLAIMER)["header"]
        self.assertEqual(parsed["date"], "03.06.2026")
        self.assertEqual(parsed["participants"], "Илья Рыбалка, Михаил Еремеев")
        self.assertEqual(parsed["theme"], "Директорат — статус.")

    def test_caption_still_four_lines_and_correct(self):
        cap = ptg.build_pdf_caption(PROTOCOL_WITH_DISCLAIMER, self.META)
        self.assertEqual(len(cap.splitlines()), 4, "caption-эталон владельца не сломан")
        self.assertIn("Участники: Илья Рыбалка, Михаил Еремеев", cap)
        # Дисклеймер НЕ просочился в caption (он — в теле PDF/.md).
        self.assertNotIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, cap)

    def test_digest_excludes_disclaimer(self):
        digest = sm.build_digest(PROTOCOL_WITH_DISCLAIMER, self.META, date="2026-06-03")
        # Участники по-прежнему из шапки.
        self.assertIn("Илья Рыбалка", digest["participants"])
        self.assertIn("Михаил Еремеев", digest["participants"])
        # Дисклеймер не попал ни в одно поле дайджеста как контент.
        blob = " ".join(digest["themes"] + digest["key_points"])
        self.assertNotIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, blob)
        self.assertNotIn("поправьте", blob.lower())
        # Содержательные темы/пункты при этом извлеклись.
        self.assertIn("Финансы", digest["themes"])

    def test_extract_sections_skips_disclaimer(self):
        themes, key_points = sm.extract_protocol_sections(PROTOCOL_WITH_DISCLAIMER)
        joined = " ".join(themes + key_points)
        self.assertNotIn(ptg.PROTOCOL_DISCLAIMER_SENTINEL, joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
