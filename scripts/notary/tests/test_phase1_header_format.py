"""Ф1 (доработки «память и знания»): фиксы шапки протокола + стабильный формат.

Покрывает REQ A1, A2.1, A2.2, F1, F3 (детерминированная часть — без LLM):
  - A2.1  фильтр UI-мусора участников (`filter_participant_names`);
  - A1/F1 человеческое имя серии в шапке TG (`_format_header`) и резолве
          (`resolve_series_display_name`) из `series-display.json`;
  - A2.2  PDF не печатает участников дважды (`_strip_leading_heading`);
  - F3    промпт values-ревью содержит правило сверки финансовых сумм.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase1_header_format -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import protocol_to_tg as ptg  # noqa: E402
from lib import protocol_to_pdf as ppdf  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402


WEEKLY_SLUG = "series-ezhenedelnaya-koordinaciya-8399ea"

# Реальный список имён координации (из эталона 09.06) — фильтр НЕ должен их съесть.
REAL_NAMES = [
    "Илья Рыбалка", "Мария Михина", "Сона Енгибарян",
    "Ольга Новикова", "Дарья Набережная", "Михаил Саргин",
]

# Мусор, который панель Телемоста реально подсовывала (жалоба владельца 09.06).
TELEMOST_JUNK = [
    "Скопировать ссылку", "Пригласить", "ДН", "ИР", "И.Р.", "Вы",
    "Участники", "Закрыть",
]


class TestFilterParticipantNames(unittest.TestCase):
    """A2.1 — blocklist UI-строк + отбраковка не-имён."""

    def test_drops_ui_strings_and_monograms(self):
        out = ptg.filter_participant_names(TELEMOST_JUNK)
        self.assertEqual(out, [], f"мусор не вычищен: {out}")

    def test_keeps_real_names(self):
        out = ptg.filter_participant_names(REAL_NAMES)
        self.assertEqual(out, REAL_NAMES)

    def test_mixed_keeps_only_real(self):
        mixed = ["Мария Михина", "Скопировать ссылку", "ДН",
                 "Сона Енгибарян", "Пригласить"]
        self.assertEqual(
            ptg.filter_participant_names(mixed),
            ["Мария Михина", "Сона Енгибарян"],
        )

    def test_short_real_names_survive(self):
        # «Ия»/«Лев» — короткие, но со строчными → это имена, не монограммы.
        self.assertEqual(ptg.filter_participant_names(["Ия", "Лев"]), ["Ия", "Лев"])

    def test_monogram_two_caps_dropped(self):
        # «ДН», «ИР» — аватар-инициалы (обе заглавные) → мусор.
        for mono in ("ДН", "ИР", "И.Р.", "AB"):
            self.assertTrue(ptg._is_ui_or_nonname(mono), mono)

    def test_dedup_preserves_order(self):
        out = ptg.filter_participant_names(
            ["Мария Михина", "Сона Енгибарян", "Мария Михина"]
        )
        self.assertEqual(out, ["Мария Михина", "Сона Енгибарян"])

    def test_non_str_and_empty_ignored(self):
        out = ptg.filter_participant_names([None, "", "   ", 42, "Мария Михина"])
        self.assertEqual(out, ["Мария Михина"])

    def test_resolve_participants_filters_panel_junk(self):
        # Сырьё панели с мусором → в шапке только реальные имена.
        meta = {
            "expectedParticipants": [],
            "participants": ["Мария Михина", "Скопировать ссылку", "ДН"],
        }
        with mock.patch.object(ptg, "_read_people_md", return_value=None):
            out = ptg._resolve_participants(meta)
        self.assertIn("Мария Михина", out)
        self.assertNotIn("Скопировать ссылку", out)
        self.assertNotIn("ДН", out)


class _SeriesDisplayConfigMixin:
    """Подкладывает временный series-display.json через env-override."""

    def _with_config(self, mapping: dict):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(mapping, tmp, ensure_ascii=False)
        tmp.close()
        self.addCleanup(lambda: os.unlink(tmp.name))
        return mock.patch.dict(
            os.environ, {"MEETING_NOTARY_SERIES_DISPLAY_PATH": tmp.name}
        )


class TestSeriesDisplayName(unittest.TestCase, _SeriesDisplayConfigMixin):
    """A1 — заголовок серии человеческим именем из конфига slug→имя."""

    def test_weekly_coord_human_name(self):
        cfg = {WEEKLY_SLUG: "Еженедельная координация"}
        with self._with_config(cfg):
            name = ptg.resolve_series_display_name({"series": WEEKLY_SLUG})
        self.assertEqual(name, "Еженедельная координация")
        self.assertNotIn("ezhenedelnaya", name.lower())

    def test_second_series_human_name(self):
        # ≥1 другая серия — тоже человеческим именем (конфиг перебивает хардкод).
        cfg = {"anzhee-direktorat": "Директорат"}
        with self._with_config(cfg):
            name = ptg.resolve_series_display_name({"series": "anzhee-direktorat"})
        self.assertEqual(name, "Директорат")

    def test_unknown_series_does_not_crash(self):
        with self._with_config({}):
            name = ptg.resolve_series_display_name({"series": "totally-unknown-xyz"})
        self.assertTrue(name)  # humanize-fallback, не падает


class TestTgHeaderSeriesTitle(unittest.TestCase, _SeriesDisplayConfigMixin):
    """A1/F1 — шапка TG показывает имя серии, не generic «ПРОТОКОЛ ВСТРЕЧИ»."""

    def test_header_shows_series_name(self):
        cfg = {WEEKLY_SLUG: "Еженедельная координация"}
        meta = {"series": WEEKLY_SLUG, "date": "2026-06-09",
                "expectedParticipants": REAL_NAMES, "participants": []}
        parsed = {"date": "09.06.2026", "theme": "Тест", "participants": "",
                  "duration": "~31 мин"}
        with self._with_config(cfg), \
                mock.patch.object(ptg, "_read_people_md", return_value=None):
            header = ptg._format_header(parsed, meta)
        self.assertIn("📋 Еженедельная координация — 09.06.2026", header)
        self.assertIn(ptg.HASHTAG, header)


class TestPdfNoDoubleParticipants(unittest.TestCase):
    """A2.2 — поля шапки (участники/длительность/транскрипт) не дублируются в теле PDF."""

    MD = (
        "#протоколвстречи 09.06.2026\n\n"
        "**Встреча:** Еженедельная координация — тест.\n\n"
        "**Длительность:** ~31 мин\n\n"
        "**Участники:** Илья Рыбалка, Мария Михина\n\n"
        "**Транскрипт:** [2026-06-09.md](2026-06-09.md)\n\n"
        "---\n\n"
        "## 1) Поставки\n\n"
        "▪️ Поставка 131 прошла таможню.\n"
    )

    def test_body_drops_duplicated_header_fields(self):
        body = ppdf._strip_leading_heading(self.MD)
        self.assertNotIn("**Участники:**", body)      # участники → только в subtitle
        self.assertNotIn("**Длительность:**", body)   # длительность → subtitle
        self.assertNotIn("**Транскрипт:**", body)     # ссылка в PDF бесполезна
        self.assertIn("**Встреча:**", body)           # тему оставляем
        self.assertIn("## 1) Поставки", body)         # тело не пострадало

    def test_participants_appear_once_overall(self):
        # subtitle печатает участников; тело — больше не должно.
        body = ppdf._strip_leading_heading(self.MD)
        _title, subtitle = ptg.build_pdf_title_subtitle(
            self.MD,
            {"series": "x", "date": "2026-06-09",
             "expectedParticipants": ["Илья Рыбалка", "Мария Михина"],
             "participants": []},
        )
        self.assertIn("Участники:", subtitle)
        self.assertEqual(body.count("Участники"), 0)


class TestValuesReviewFinancialRule(unittest.TestCase):
    """F3 — промпт values-ревью содержит правило сверки финансовых сумм."""

    def test_prompt_mentions_financial_consistency(self):
        sp = lp._build_review_system_prompt(("values",))
        self.assertIn("6 440 000", sp)         # эталонный пример владельца
        self.assertIn("процент", sp.lower())   # план/факт/процент
        # регресс-инвариант существующих тестов остаётся:
        self.assertIn("число", sp)
        self.assertIn("JSON", sp)


@unittest.skipUnless(
    Path("/Users/ilarybalka/Projects/me/встречи/" + WEEKLY_SLUG +
         "/2026-06-09-protokol.md").is_file(),
    "эталон 09.06 недоступен (боевой проход)",
)
class TestRealProtocol0906(unittest.TestCase, _SeriesDisplayConfigMixin):
    """Боевой проход: эталон 09.06 + meta с реальным мусором панели."""

    REAL = Path("/Users/ilarybalka/Projects/me/встречи/" + WEEKLY_SLUG +
                "/2026-06-09-protokol.md")

    def test_render_header_clean_and_human(self):
        md = self.REAL.read_text(encoding="utf-8")
        meta = {
            "series": WEEKLY_SLUG, "date": "2026-06-09",
            "expectedParticipants": REAL_NAMES,
            "participants": ["Скопировать ссылку", "ДН", "Пригласить"],
        }
        cfg = {WEEKLY_SLUG: "Еженедельная координация"}
        with self._with_config(cfg), \
                mock.patch.object(ptg, "_read_people_md", return_value=None):
            tg = ptg.format_protocol_as_tg_text(md, meta)
        self.assertIn("Еженедельная координация", tg)
        for junk in ("Скопировать ссылку", "Пригласить"):
            self.assertNotIn(junk, tg)
        # «ДН» как отдельное слово-участник не появляется.
        self.assertNotIn("👥 ДН", tg)


if __name__ == "__main__":
    unittest.main()
