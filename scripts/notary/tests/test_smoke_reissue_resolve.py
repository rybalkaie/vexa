"""Ф2 delivery-fixes (ISS-1) — smoke-связки резолва перевыпуска (REQ 1.4 / идея ревью №2).

Покрывает `tools/smoke_reissue_resolve.py`: для свежей delivered-meta в ПРОД-
раскладке НЕС2 (meta в `_tmp/transcripts/<sid>.meta.json`, транскрипт в
`<output_dir>/<series>/…`) резолв `feedback_reissue._resolve_paths` (Ф1) даёт
ЧИТАЕМЫЙ транскрипт — статус «ok», ≠ «transcript missing». И обратное: когда
транскрипт реально отсутствует / папки разъехались — smoke это ловит (FAIL),
что и есть его смысл (поймать НЕС2 ДО следующей правки).

Запуск: python3 -m unittest tests.test_smoke_reissue_resolve (system python3.9, без venv).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_TOOLS = _NOTARY / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import smoke_reissue_resolve as smoke  # noqa: E402


class TestSyntheticSelfTest(unittest.TestCase):
    def test_self_test_passes(self):
        # Прод-раскладка НЕС2 → резолв находит читаемый транскрипт, статус «ok».
        res = smoke.synthetic_self_test()
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "ok")
        self.assertIsInstance(res["bytes"], int)
        self.assertGreater(res["bytes"], 0)

    def test_main_self_test_exit_zero(self):
        self.assertEqual(smoke.main(["--self-test"]), 0)


class TestCheckMeta(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.transcripts = self.base / "_tmp" / "transcripts"   # meta
        self.protocols = self.base / "protocols"                # транскрипт
        self.transcripts.mkdir(parents=True, exist_ok=True)
        self.protocols.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _series_dir(self, series="coord"):
        d = self.protocols / series
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_meta(self, payload, sid="sid1"):
        mp = self.transcripts / f"{sid}.meta.json"
        mp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return mp

    def test_fresh_delivered_meta_resolves_readable(self):
        # REQ 1.4: persisted-путь (Ф1) → читаемый транскрипт в РАЗНОЙ от meta папке.
        d = self._series_dir()
        tpath = d / "2026-06-10-2026-06-10-tm-555.md"
        ppath = d / "2026-06-10-protokol.md"
        tpath.write_text("00:00 Спикер 1: текст\n", encoding="utf-8")
        ppath.write_text("#протоколвстречи\n- пункт\n", encoding="utf-8")
        meta = {"series": "coord", "date": "2026-06-10",
                "transcript_path": str(tpath), "protocol_path": str(ppath),
                "delivered": [{"chat_id": -1001, "message_ids": [1], "at": "x"}]}
        mp = self._write_meta(meta)
        r = smoke.check_meta(mp)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["transcript"], str(tpath))
        self.assertGreater(r["bytes"], 0)

    def test_folder_mismatch_is_caught_as_fail(self):
        # Смысл smoke: транскрипта НЕТ в папке серии (НЕС2 разъезд / удалён) →
        # FAIL «transcript missing», а не ложный PASS.
        d = self._series_dir()
        (d / "2026-06-10-protokol.md").write_text("#протокол\n", encoding="utf-8")
        # transcript_path указывает в НЕсуществующий файл (папки разъехались)
        meta = {"series": "coord", "date": "2026-06-10",
                "transcript_path": str(d / "2026-06-10-2026-06-10-tm-555.md"),
                "delivered": [{"chat_id": -1001, "message_ids": [1], "at": "x"}]}
        mp = self._write_meta(meta)
        r = smoke.check_meta(mp)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["status"], "transcript missing")

    def test_protocol_missing_reported(self):
        # Транскрипт есть, протокола нет → reissue_one упал бы «protocol missing».
        d = self._series_dir()
        tpath = d / "2026-06-10-2026-06-10-tm-555.md"
        tpath.write_text("00:00 Спикер 1: текст\n", encoding="utf-8")
        meta = {"series": "coord", "date": "2026-06-10",
                "transcript_path": str(tpath),
                "delivered": [{"chat_id": -1001, "message_ids": [1], "at": "x"}]}
        mp = self._write_meta(meta)
        r = smoke.check_meta(mp)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["status"], "protocol missing")

    def test_scan_skips_non_delivered_and_flags_failures(self):
        d = self._series_dir()
        # 1) delivered + ok
        tpath = d / "2026-06-10-2026-06-10-tm-1.md"
        tpath.write_text("x\n", encoding="utf-8")
        (d / "2026-06-10-protokol.md").write_text("#p\n", encoding="utf-8")
        self._write_meta({"series": "coord", "date": "2026-06-10",
                          "transcript_path": str(tpath),
                          "protocol_path": str(d / "2026-06-10-protokol.md"),
                          "delivered": [{"chat_id": -1, "message_ids": [1], "at": "x"}]},
                         sid="ok1")
        # 2) delivered + broken: ОТДЕЛЬНАЯ серия без единого транскрипта, чтобы
        #    фолбэк-glob Ф1 не подобрал чужой -tm- файл (он бы — и это правильно).
        d2 = self._series_dir("orphan")
        self._write_meta({"series": "orphan", "date": "2026-06-10",
                          "transcript_path": str(d2 / "missing.md"),
                          "delivered": [{"chat_id": -1, "message_ids": [2], "at": "x"}]},
                         sid="bad1")
        # 3) не-delivered → skipped
        self._write_meta({"series": "coord", "date": "2026-06-10"}, sid="nd1")

        results = smoke.scan(self.transcripts)
        by_status = {r["meta"]: r for r in results}
        self.assertEqual(by_status["ok1.meta.json"]["status"], "ok")
        self.assertTrue(by_status["ok1.meta.json"]["ok"])
        self.assertFalse(by_status["bad1.meta.json"]["ok"])
        self.assertIsNone(by_status["nd1.meta.json"]["ok"])
        self.assertIn("skipped", by_status["nd1.meta.json"]["status"])

    def test_main_scan_returns_nonzero_on_failure(self):
        d = self._series_dir()
        self._write_meta({"series": "coord", "date": "2026-06-10",
                          "transcript_path": str(d / "missing.md"),
                          "delivered": [{"chat_id": -1, "message_ids": [2], "at": "x"}]},
                         sid="bad1")
        self.assertEqual(smoke.main(["--scan", str(self.transcripts)]), 1)

    def test_main_scan_returns_zero_when_all_pass(self):
        d = self._series_dir()
        tpath = d / "2026-06-10-2026-06-10-tm-1.md"
        tpath.write_text("x\n", encoding="utf-8")
        (d / "2026-06-10-protokol.md").write_text("#p\n", encoding="utf-8")
        self._write_meta({"series": "coord", "date": "2026-06-10",
                          "transcript_path": str(tpath),
                          "protocol_path": str(d / "2026-06-10-protokol.md"),
                          "delivered": [{"chat_id": -1, "message_ids": [1], "at": "x"}]},
                         sid="ok1")
        self.assertEqual(smoke.main(["--scan", str(self.transcripts)]), 0)


if __name__ == "__main__":
    unittest.main()
