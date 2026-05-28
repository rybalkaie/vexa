"""Тесты `lib.paths._target_path` — 3 ключевых кейса плана Ф1 + edge-cases.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_paths -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# tests/ → notary/ (родитель), чтобы импортировать `lib.paths` как из finalize-meeting.py.
_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib.paths import _target_path, DEFAULT_ROOT  # noqa: E402


ROOT = "/tmp/meeting-notary-test-vstrechi"


class TestTargetPath(unittest.TestCase):

    # ------------------------------------------------------------------
    # (a) series + date — регулярная встреча
    # ------------------------------------------------------------------
    def test_series_plus_date_from_startTs(self):
        meta = {
            "series": "sales-quality",
            "startTs": "2026-05-27T08:06:00Z",
            "sessionUid": "auto-sales-quality-20260527T080600Z",
        }
        result = _target_path(meta, root=ROOT)
        self.assertEqual(result, Path(ROOT) / "sales-quality" / "2026-05-27.md")

    def test_series_plus_date_from_date_field(self):
        meta = {"series": "marketplaces-tatiana", "date": "2026-05-28"}
        result = _target_path(meta, root=ROOT)
        self.assertEqual(
            result, Path(ROOT) / "marketplaces-tatiana" / "2026-05-28.md"
        )

    def test_series_protokol_filename(self):
        """kind=protokol → <date>-protokol.md рядом с транскриптом."""
        meta = {"series": "sales-quality", "startTs": "2026-05-27T08:06:00Z"}
        result = _target_path(meta, root=ROOT, kind="protokol")
        self.assertEqual(
            result, Path(ROOT) / "sales-quality" / "2026-05-27-protokol.md"
        )

    def test_series_with_whitespace_stripped(self):
        """series с пробелами по краям — strip()."""
        meta = {"series": "  sales-quality  ", "startTs": "2026-05-27T08:06:00Z"}
        result = _target_path(meta, root=ROOT)
        self.assertEqual(result, Path(ROOT) / "sales-quality" / "2026-05-27.md")

    # ------------------------------------------------------------------
    # (b) one-off — без series
    # ------------------------------------------------------------------
    def test_one_off_from_session_uid(self):
        """series отсутствует, sessionUid=`auto-tm-<id>-<dt>` → _one-off/<date>-tm-<id>/."""
        meta = {
            "series": "",
            "startTs": "2026-05-27T15:49:00Z",
            "sessionUid": "auto-tm-1779869180376-20260527T154900Z",
        }
        result = _target_path(meta, root=ROOT)
        self.assertEqual(
            result,
            Path(ROOT)
            / "_one-off"
            / "2026-05-27-tm-1779869180376"
            / "2026-05-27.md",
        )

    def test_one_off_explicit_id_overrides_session_uid(self):
        """oneOffId перебивает sessionUid."""
        meta = {
            "series": None,
            "startTs": "2026-05-27T15:49:00Z",
            "sessionUid": "auto-something-else-20260527T154900Z",
            "oneOffId": "custom-7",
        }
        result = _target_path(meta, root=ROOT)
        self.assertEqual(
            result,
            Path(ROOT) / "_one-off" / "2026-05-27-custom-7" / "2026-05-27.md",
        )

    def test_one_off_fallback_session_uid_no_tm(self):
        """sessionUid без tm- → используем как есть."""
        meta = {"startTs": "2026-05-27T15:49:00Z", "sessionUid": "manual-asdfgh"}
        result = _target_path(meta, root=ROOT)
        self.assertEqual(
            result,
            Path(ROOT)
            / "_one-off"
            / "2026-05-27-manual-asdfgh"
            / "2026-05-27.md",
        )

    def test_one_off_no_session_uid_unknown(self):
        """Совсем нет sessionUid — "unknown" вместо id."""
        meta = {"startTs": "2026-05-27T15:49:00Z"}
        result = _target_path(meta, root=ROOT)
        self.assertEqual(
            result,
            Path(ROOT) / "_one-off" / "2026-05-27-unknown" / "2026-05-27.md",
        )

    # ------------------------------------------------------------------
    # (c) явный архив
    # ------------------------------------------------------------------
    def test_archive_with_year_int(self):
        meta = {
            "series": "sales-quality",
            "startTs": "2025-09-15T10:00:00Z",
            "archive": 2025,
        }
        result = _target_path(meta, root=ROOT)
        self.assertEqual(
            result,
            Path(ROOT)
            / "_archive"
            / "2025"
            / "sales-quality"
            / "2025-09-15.md",
        )

    def test_archive_with_year_str(self):
        meta = {
            "series": "anzhee-direktorat",
            "startTs": "2024-12-01T10:00:00Z",
            "archive": "2024",
        }
        result = _target_path(meta, root=ROOT)
        self.assertEqual(
            result,
            Path(ROOT)
            / "_archive"
            / "2024"
            / "anzhee-direktorat"
            / "2024-12-01.md",
        )

    def test_archive_requires_series(self):
        """archive без series — ValueError (нельзя в архив без названия серии)."""
        meta = {"startTs": "2025-09-15T10:00:00Z", "archive": 2025}
        with self.assertRaises(ValueError):
            _target_path(meta, root=ROOT)

    # ------------------------------------------------------------------
    # Граничные / negative cases
    # ------------------------------------------------------------------
    def test_no_date_raises(self):
        """Нет ни startTs, ни date — ValueError."""
        meta = {"series": "sales-quality"}
        with self.assertRaises(ValueError):
            _target_path(meta, root=ROOT)

    def test_malformed_date_raises(self):
        """date НЕ в формате YYYY-MM-DD — ValueError (защита от тихой порчи путей, Н1)."""
        for bad in ("tomorrow!!", "2026/05/27", "26-05-2026", "2026-5-27"):
            with self.subTest(date=bad):
                meta = {"series": "sales-quality", "date": bad}
                with self.assertRaises(ValueError):
                    _target_path(meta, root=ROOT)

    def test_malformed_startTs_raises(self):
        """startTs первые 10 символов не YYYY-MM-DD — ValueError."""
        meta = {"series": "x", "startTs": "blah-blah-blah"}
        with self.assertRaises(ValueError):
            _target_path(meta, root=ROOT)

    def test_date_fallback_to_startTs_when_date_malformed(self):
        """Если date невалидный, но startTs валидный — берём startTs."""
        meta = {
            "series": "x",
            "date": "tomorrow!!",
            "startTs": "2026-05-27T08:00:00Z",
        }
        result = _target_path(meta, root=ROOT)
        self.assertEqual(result, Path(ROOT) / "x" / "2026-05-27.md")

    def test_meta_not_dict_raises(self):
        with self.assertRaises(TypeError):
            _target_path("not a dict", root=ROOT)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Path traversal / валидация компонентов (защита из хода 3 цикла Ф1)
    # ------------------------------------------------------------------
    def test_series_with_slash_rejected(self):
        """У1: series с `/` — ValueError (path traversal)."""
        meta = {"series": "../etc/passwd", "startTs": "2026-05-27T00:00:00Z"}
        with self.assertRaises(ValueError):
            _target_path(meta, root=ROOT)

    def test_series_with_backslash_rejected(self):
        meta = {"series": "sales\\quality", "startTs": "2026-05-27T00:00:00Z"}
        with self.assertRaises(ValueError):
            _target_path(meta, root=ROOT)

    def test_series_dotdot_rejected(self):
        meta = {"series": "..", "startTs": "2026-05-27T00:00:00Z"}
        with self.assertRaises(ValueError):
            _target_path(meta, root=ROOT)

    def test_series_whitespace_only_rejected(self):
        """У2: series из одних пробелов — ValueError, не silent one-off fallback."""
        meta = {"series": "   ", "startTs": "2026-05-27T00:00:00Z"}
        with self.assertRaises(ValueError):
            _target_path(meta, root=ROOT)

    def test_one_off_id_with_slash_rejected(self):
        """У3: oneOffId с `/` — ValueError."""
        meta = {
            "startTs": "2026-05-27T00:00:00Z",
            "oneOffId": "../escape",
        }
        with self.assertRaises(ValueError):
            _target_path(meta, root=ROOT)

    def test_archive_must_be_year(self):
        """У6: archive — строго YYYY (4 цифры), иначе ValueError."""
        for bad in ("архив", "true", "2025-05", "25", "20255"):
            with self.subTest(archive=bad):
                meta = {
                    "series": "sales-quality",
                    "startTs": "2025-09-15T00:00:00Z",
                    "archive": bad,
                }
                with self.assertRaises(ValueError):
                    _target_path(meta, root=ROOT)

    def test_archive_valid_string_year_still_works(self):
        """Регрессия после фикса У6: archive='2024' (валидный) продолжает работать."""
        meta = {
            "series": "sales-quality",
            "startTs": "2024-12-01T00:00:00Z",
            "archive": "2024",
        }
        result = _target_path(meta, root=ROOT)
        self.assertEqual(
            result,
            Path(ROOT) / "_archive" / "2024" / "sales-quality" / "2024-12-01.md",
        )

    def test_cyrillic_series_allowed(self):
        """Legacy: кириллица в series (как `встречи/`) допустима."""
        meta = {"series": "встреча-таня", "startTs": "2026-01-01T00:00:00Z"}
        result = _target_path(meta, root=ROOT)
        self.assertEqual(result, Path(ROOT) / "встреча-таня" / "2026-01-01.md")

    def test_series_wrong_type_raises(self):
        """НОВ3: series — число/dict/list → TypeError, не silent one-off."""
        for bad in (42, 3.14, ["a"], {"x": 1}):
            with self.subTest(bad=bad):
                meta = {"series": bad, "startTs": "2026-05-27T00:00:00Z"}
                with self.assertRaises(TypeError):
                    _target_path(meta, root=ROOT)

    def test_archive_falsy_values_treated_as_no_archive(self):
        """archive=None/""/0/False → НЕ архивный путь, обычный series."""
        for archive_val in (None, "", 0, False):
            with self.subTest(archive=archive_val):
                meta = {
                    "series": "sales-quality",
                    "startTs": "2026-05-27T08:00:00Z",
                    "archive": archive_val,
                }
                result = _target_path(meta, root=ROOT)
                self.assertEqual(
                    result,
                    Path(ROOT) / "sales-quality" / "2026-05-27.md",
                )

    # ------------------------------------------------------------------
    # root: явный / env / default
    # ------------------------------------------------------------------
    def test_root_explicit_arg_wins(self):
        meta = {"series": "x", "startTs": "2026-01-01T00:00:00Z"}
        result = _target_path(meta, root="/tmp/explicit")
        self.assertEqual(result, Path("/tmp/explicit") / "x" / "2026-01-01.md")

    def test_root_from_env_when_no_arg(self):
        meta = {"series": "x", "startTs": "2026-01-01T00:00:00Z"}
        old = os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR")
        os.environ["MEETING_NOTARY_PROTOCOLS_DIR"] = "/tmp/from-env"
        try:
            result = _target_path(meta)
            self.assertEqual(
                result, Path("/tmp/from-env") / "x" / "2026-01-01.md"
            )
        finally:
            if old is None:
                os.environ.pop("MEETING_NOTARY_PROTOCOLS_DIR", None)
            else:
                os.environ["MEETING_NOTARY_PROTOCOLS_DIR"] = old

    def test_root_default_when_no_env_no_arg(self):
        meta = {"series": "x", "startTs": "2026-01-01T00:00:00Z"}
        old = os.environ.pop("MEETING_NOTARY_PROTOCOLS_DIR", None)
        try:
            result = _target_path(meta)
            self.assertEqual(result, Path(DEFAULT_ROOT) / "x" / "2026-01-01.md")
        finally:
            if old is not None:
                os.environ["MEETING_NOTARY_PROTOCOLS_DIR"] = old

    def test_root_expands_user_tilde(self):
        meta = {"series": "x", "startTs": "2026-01-01T00:00:00Z"}
        result = _target_path(meta, root="~/some/place")
        self.assertEqual(
            result,
            Path(os.path.expanduser("~/some/place")) / "x" / "2026-01-01.md",
        )


if __name__ == "__main__":
    unittest.main()
