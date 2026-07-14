"""ISS-32 (Ф3): развести рабочую папку протоколов и долговечный архив.

Корень бага: `MEETING_NOTARY_PROTOCOLS_DIR` (рабочая `_tmp`) использовалась И как
цель долговечной копии collector'а → копия становилась in-place no-op, `/srv`/мак-
архив замирали. Фикс: отдельная `MEETING_NOTARY_ARCHIVE_DIR` для ЗАПИСИ долговечной
копии (дефолт = рабочая, обратная совместимость).

Тесты закрепляют:
  • `paths.archive_dest_for` — маппинг рабочий→архив, None когда архив не разведён;
  • `collector.MEETINGS_DIR` читает ARCHIVE_DIR (с фолбэком на PROTOCOLS_DIR);
  • `collector._sync_protocol_summary` — протокол-сводка `<date>-protokol.md` в архив
    (finalize→архив, REQ 32.2), включая date-ключ при коллизии и in-place no-op;
  • `feedback_reissue._sync_reissue_to_archive` — протокол+транскрипт+память в архив
    после перевыпуска (reissue→архив, REQ 32.3), no-op при незаданном архиве (32.4).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss32_archive_split -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_NOTARY))

from notary.lib import paths  # noqa: E402
from notary.lib import feedback_reissue  # noqa: E402


def _load_collector_module():
    """`collector.py` — top-level скрипт (не в lib/); свежий инстанс через importlib.

    Каждая загрузка перечитывает env → так проверяем env-зависимый MEETINGS_DIR.
    """
    spec = importlib.util.spec_from_file_location(
        "collector_module_iss32", str(_NOTARY / "collector.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_collector_module()


# ---------------------------------------------------------------------------
# paths.archive_dest_for — резолв рабочий→архив
# ---------------------------------------------------------------------------
class TestArchiveDestFor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.prot = root / "_tmp" / "protocols"
        self.arch = root / "srv" / "protocols"
        self.prot.mkdir(parents=True)
        self.arch.mkdir(parents=True)
        self.working = self.prot / "series-x" / "2026-07-14-protokol.md"

    def tearDown(self):
        self._tmp.cleanup()

    def test_none_when_archive_unset(self):
        env = {"MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot)}
        env.pop("MEETING_NOTARY_ARCHIVE_DIR", None)
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MEETING_NOTARY_ARCHIVE_DIR", None)
            self.assertIsNone(paths.archive_dest_for(self.working))

    def test_none_when_archive_equals_protocols(self):
        with mock.patch.dict(os.environ, {
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot),
            "MEETING_NOTARY_ARCHIVE_DIR": str(self.prot),
        }):
            self.assertIsNone(paths.archive_dest_for(self.working))

    def test_maps_to_archive_when_split(self):
        with mock.patch.dict(os.environ, {
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot),
            "MEETING_NOTARY_ARCHIVE_DIR": str(self.arch),
        }):
            dst = paths.archive_dest_for(self.working)
        self.assertEqual(dst, self.arch / "series-x" / "2026-07-14-protokol.md")

    def test_none_when_file_outside_protocols(self):
        # co-located тест / нестандартный путь вне рабочей папки → не мапим.
        outside = Path(self._tmp.name) / "elsewhere" / "2026-07-14.md"
        with mock.patch.dict(os.environ, {
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot),
            "MEETING_NOTARY_ARCHIVE_DIR": str(self.arch),
        }):
            self.assertIsNone(paths.archive_dest_for(outside))

    def test_roots_helpers(self):
        with mock.patch.dict(os.environ, {
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot),
            "MEETING_NOTARY_ARCHIVE_DIR": str(self.arch),
        }):
            self.assertEqual(paths.protocols_root(), self.prot)
            self.assertEqual(paths.archive_root(), self.arch)
        # archive_root дефолтит на рабочую, когда ARCHIVE_DIR не задан.
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot)}):
            os.environ.pop("MEETING_NOTARY_ARCHIVE_DIR", None)
            self.assertEqual(paths.archive_root(), self.prot)


# ---------------------------------------------------------------------------
# collector.MEETINGS_DIR читает ARCHIVE_DIR (с фолбэком на PROTOCOLS_DIR)
# ---------------------------------------------------------------------------
class TestMeetingsDirFromArchiveEnv(unittest.TestCase):
    def test_meetings_dir_uses_archive_dir_when_set(self):
        with mock.patch.dict(os.environ, {
            "MEETING_NOTARY_PROTOCOLS_DIR": "/tmp/work/_tmp/protocols",
            "MEETING_NOTARY_ARCHIVE_DIR": "/tmp/durable/srv/protocols",
        }):
            mod = _load_collector_module()
        self.assertEqual(str(mod.MEETINGS_DIR), "/tmp/durable/srv/protocols")

    def test_meetings_dir_falls_back_to_protocols_dir(self):
        env = {"MEETING_NOTARY_PROTOCOLS_DIR": "/tmp/work/_tmp/protocols"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MEETING_NOTARY_ARCHIVE_DIR", None)
            mod = _load_collector_module()
        # Обратная совместимость: архив не задан → архив == рабочая папка.
        self.assertEqual(str(mod.MEETINGS_DIR), "/tmp/work/_tmp/protocols")


# ---------------------------------------------------------------------------
# collector._sync_protocol_summary — протокол-сводка → долговечный архив
# ---------------------------------------------------------------------------
class TestSyncProtocolSummaryLocal(unittest.TestCase):
    def setUp(self):
        self._orig_local = collector.LOCAL_FINALIZE
        collector.LOCAL_FINALIZE = True
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.out = root / "_tmp" / "protocols" / "test-series"      # рабочая (output-dir)
        self.durable = root / "archive" / "test-series"            # долговечный архив
        self.out.mkdir(parents=True)
        self.durable.mkdir(parents=True)
        # finalize положил транскрипт + протокол-сводку рядом в output-dir.
        self.src_transcript = self.out / "2026-07-14.md"
        self.src_transcript.write_text("# Транскрипт\nСпикер 1: привет\n", encoding="utf-8")
        self.src_proto = self.out / "2026-07-14-protokol.md"
        self.src_proto.write_text("# Протокол встречи\n## Решения\n- сделать X\n", encoding="utf-8")
        # collector разместил транскрипт в долговечном архиве.
        self.target_md = self.durable / "2026-07-14.md"

    def tearDown(self):
        collector.LOCAL_FINALIZE = self._orig_local
        self._tmp.cleanup()

    def test_copies_protocol_summary_to_durable(self):
        collector._sync_protocol_summary(
            str(self.src_transcript), self.target_md, "2026-07-14", "auto-sid-20260714T100000Z",
        )
        dst = self.durable / "2026-07-14-protokol.md"
        self.assertTrue(dst.exists(), "протокол-сводка должна доехать в долговечный архив")
        self.assertIn("Решения", dst.read_text(encoding="utf-8"))
        self.assertFalse((self.durable / "2026-07-14-protokol.md.part").exists())

    def test_no_source_protocol_is_silent_noop(self):
        self.src_proto.unlink()  # генерация протокола выключена / упала
        try:
            collector._sync_protocol_summary(
                str(self.src_transcript), self.target_md, "2026-07-14", "sid",
            )
        except Exception as e:  # noqa: BLE001
            self.fail(f"синк без файла-источника не должен падать: {e}")
        self.assertFalse((self.durable / "2026-07-14-protokol.md").exists())

    def test_inplace_when_src_equals_dst(self):
        # output-dir == долговечный корень (архив не разведён) → no-op, файл цел.
        target_md = self.out / "2026-07-14.md"
        collector._sync_protocol_summary(
            str(self.src_transcript), target_md, "2026-07-14", "sid",
        )
        self.assertTrue(self.src_proto.exists())
        self.assertIn("сделать X", self.src_proto.read_text(encoding="utf-8"))
        self.assertFalse((self.out / "2026-07-14-protokol.md.part").exists())

    def test_date_keyed_name_even_on_transcript_collision(self):
        # Транскрипт на долговечной стороне переименован `<date>-<sid>.md` (коллизия дат),
        # но протокол кладётся по date-ключу `<date>-protokol.md` (как пишет finalize).
        target_md = self.durable / "2026-07-14-auto-sid.md"
        collector._sync_protocol_summary(
            str(self.src_transcript), target_md, "2026-07-14", "auto-sid",
        )
        self.assertTrue((self.durable / "2026-07-14-protokol.md").exists())
        self.assertFalse((self.durable / "2026-07-14-auto-sid-protokol.md").exists())


# ---------------------------------------------------------------------------
# feedback_reissue._sync_reissue_to_archive — перевыпуск → долговечный архив
# ---------------------------------------------------------------------------
class TestReissueArchiveSync(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.prot = root / "_tmp" / "protocols"
        self.arch = root / "srv" / "protocols"
        self.series_dir = self.prot / "test-series"
        self.series_dir.mkdir(parents=True)
        self.arch.mkdir(parents=True)
        self.date = "2026-07-14"
        # reissue уже переписал рабочие файлы на диск.
        self.transcript = self.series_dir / f"{self.date}.md"
        self.protocol = self.series_dir / f"{self.date}-protokol.md"
        self.memory = self.series_dir / f"{self.date}-memory.json"
        self.transcript.write_text("# Транскрипт (remapped)\n", encoding="utf-8")
        self.protocol.write_text("# Протокол v2\n## Решения\n- новое\n", encoding="utf-8")
        self.memory.write_text(json.dumps({"schema": 1, "date": self.date}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_syncs_all_three_to_archive_when_split(self):
        with mock.patch.dict(os.environ, {
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot),
            "MEETING_NOTARY_ARCHIVE_DIR": str(self.arch),
        }):
            feedback_reissue._sync_reissue_to_archive(self.protocol, self.transcript, self.date)
        adir = self.arch / "test-series"
        self.assertTrue((adir / f"{self.date}-protokol.md").exists(), "протокол → архив")
        self.assertTrue((adir / f"{self.date}.md").exists(), "транскрипт → архив")
        self.assertTrue((adir / f"{self.date}-memory.json").exists(), "память → архив")
        self.assertIn("Протокол v2", (adir / f"{self.date}-protokol.md").read_text(encoding="utf-8"))
        # additive: рабочие файлы на месте, .part не оставлен.
        self.assertTrue(self.protocol.exists())
        self.assertFalse((adir / f"{self.date}-protokol.md.part").exists())

    def test_noop_when_archive_unset(self):
        env = {"MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot)}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MEETING_NOTARY_ARCHIVE_DIR", None)
            feedback_reissue._sync_reissue_to_archive(self.protocol, self.transcript, self.date)
        # Обратная совместимость: архив не разведён → ничего не копируем.
        self.assertFalse((self.arch / "test-series").exists())

    def test_missing_files_skipped(self):
        # Память не писалась (нет remap) → синк протокола+транскрипта, память пропущена.
        self.memory.unlink()
        with mock.patch.dict(os.environ, {
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.prot),
            "MEETING_NOTARY_ARCHIVE_DIR": str(self.arch),
        }):
            feedback_reissue._sync_reissue_to_archive(self.protocol, self.transcript, self.date)
        adir = self.arch / "test-series"
        self.assertTrue((adir / f"{self.date}-protokol.md").exists())
        self.assertTrue((adir / f"{self.date}.md").exists())
        self.assertFalse((adir / f"{self.date}-memory.json").exists())


if __name__ == "__main__":
    unittest.main()
