"""Ф6 (E6): разметка серии «компания» + «видимость» поверх `watched.yaml`.

План `2026-06-09-notary-memory-knowledge-rework`, Фаза 6. Покрывает:
  - валидацию полей company/visibility в записи watched.yaml (cli/registry);
  - пер-record резолв на уровень series (get_company/visibility_for_series);
  - обёртку lib/series_markup (разметка ПЕРВИЧНА, оргструктура — fallback);
  - дефолт неразмеченной серии → visibility None (→ private у гейта, E3).

Чистые dict-резолверы и валидатор тестируются БЕЗ pyyaml (инъекция `watched`).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_series_markup -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.cli import registry  # noqa: E402
from notary.lib import series_markup  # noqa: E402

SLUG = "series-ezhenedelnaya-koordinaciya-8399ea"


def _watched(*recs):
    return {"watched": list(recs)}


def _rec(series, **fields):
    rec = {"id": f"{series}-evt", "series": series, "type": "manual", "cron": "0 11 * * 2", "tz": "Europe/Moscow"}
    rec.update(fields)
    return rec


class TestRegistryResolvers(unittest.TestCase):
    """Пер-record резолв company/visibility на уровень series (как chat_id)."""

    def test_company_resolved(self):
        w = _watched(_rec(SLUG, company="anzhee", visibility="company"))
        self.assertEqual(registry.get_company_for_series(SLUG, w), "anzhee")

    def test_visibility_resolved(self):
        w = _watched(_rec(SLUG, company="anzhee", visibility="company"))
        self.assertEqual(registry.get_visibility_for_series(SLUG, w), "company")

    def test_missing_markup_is_none(self):
        w = _watched(_rec(SLUG))
        self.assertIsNone(registry.get_company_for_series(SLUG, w))
        self.assertIsNone(registry.get_visibility_for_series(SLUG, w))

    def test_case_insensitive_and_trimmed(self):
        w = _watched(_rec(SLUG, company=" Anzhee ", visibility="PRIVATE"))
        self.assertEqual(registry.get_company_for_series(SLUG, w), "anzhee")
        self.assertEqual(registry.get_visibility_for_series(SLUG, w), "private")

    def test_invalid_value_ignored(self):
        # Мусорное значение пропускается, как будто не задано (устойчивость).
        w = _watched(_rec(SLUG, company="acme", visibility="public"))
        self.assertIsNone(registry.get_company_for_series(SLUG, w))
        self.assertIsNone(registry.get_visibility_for_series(SLUG, w))

    def test_first_nonempty_among_series_records(self):
        w = _watched(
            _rec(SLUG, id="a"),                       # без разметки
            _rec(SLUG, id="b", company="anzhee", visibility="company"),
        )
        self.assertEqual(registry.get_company_for_series(SLUG, w), "anzhee")
        self.assertEqual(registry.get_visibility_for_series(SLUG, w), "company")

    def test_other_series_not_matched(self):
        w = _watched(_rec("other-series", company="mpfirst", visibility="company"))
        self.assertIsNone(registry.get_company_for_series(SLUG, w))


class TestWatchedRecordValidation(unittest.TestCase):
    """Валидация company/visibility в validate_watched_record."""

    def _rooms(self):
        return {"rooms": []}

    def test_valid_markup_no_errors(self):
        errs = registry.validate_watched_record(
            _rec(SLUG, company="anzhee", visibility="company"), self._rooms())
        self.assertEqual(errs, [])

    def test_absent_markup_no_errors(self):
        errs = registry.validate_watched_record(_rec(SLUG), self._rooms())
        self.assertEqual(errs, [])

    def test_invalid_company_flagged(self):
        errs = registry.validate_watched_record(
            _rec(SLUG, company="acme"), self._rooms())
        self.assertTrue(any("company" in e for e in errs))

    def test_invalid_visibility_flagged(self):
        errs = registry.validate_watched_record(
            _rec(SLUG, visibility="public"), self._rooms())
        self.assertTrue(any("visibility" in e for e in errs))


class TestSeriesMarkupWrapper(unittest.TestCase):
    """lib/series_markup: разметка ПЕРВИЧНА; нет разметки → visibility None."""

    def test_company_from_markup(self):
        w = _watched(_rec(SLUG, company="anzhee", visibility="company"))
        self.assertEqual(series_markup.company_for_series(SLUG, watched=w), "anzhee")

    def test_visibility_from_markup(self):
        w = _watched(_rec(SLUG, company="anzhee", visibility="company"))
        self.assertEqual(series_markup.visibility_for_series(SLUG, watched=w), "company")

    def test_visibility_none_when_unmarked(self):
        # E3: неразмеченная серия → visibility None (гейт трактует как private).
        w = _watched(_rec(SLUG))
        self.assertIsNone(series_markup.visibility_for_series(SLUG, watched=w))

    def test_markup_for_series_pair(self):
        w = _watched(_rec(SLUG, company="anzhee", visibility="company"))
        company, visibility = series_markup.markup_for_series(SLUG, watched=w)
        self.assertEqual(company, "anzhee")
        self.assertEqual(visibility, "company")

    def test_empty_series_is_none(self):
        self.assertIsNone(series_markup.company_for_series("", watched=_watched()))
        self.assertIsNone(series_markup.visibility_for_series(None, watched=_watched()))


if __name__ == "__main__":
    unittest.main()
