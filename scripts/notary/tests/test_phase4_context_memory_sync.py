"""Ф4 (task 5): прод-синк памяти серии в коллекторе — `_sync_series_memory`.

finalize (`series_memory.save_meeting_digest`) пишет `<date>-memory.json` рядом с
протоколом в свой output-dir; коллектор обязан донести её на долговечную сторону рядом
с `.md` (наследие Ф2 §5: иначе на бою память серии не накапливается в архиве владельца).

Тест закрепляет LOCAL_FINALIZE-путь (без ssh):
  • копирование sidecar-файла памяти в долговечный архив рядом с протоколом;
  • date-ключ имени (`<date>-memory.json`) даже при коллизии имени протокола;
  • no-op, когда output-dir совпадает с долговечным корнем (src == dst);
  • тихий пропуск без файла-источника (память выключена / пустой протокол).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase4_context_memory_sync -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


def _load_collector_module():
    """`collector.py` — top-level скрипт (не в lib/); грузим через importlib без main()."""
    spec = importlib.util.spec_from_file_location(
        "collector_module_p4", str(_NOTARY / "collector.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_collector_module()


_MEMORY_JSON = {
    "schema": 1,
    "date": "2026-06-09",
    "series": "test-series",
    "participants": ["Мария Михина", "Ольга Новикова"],
    "themes": ["Поставки", "Финансы"],
    "key_points": ["49 млн оборот"],
}


class TestSyncSeriesMemoryLocal(unittest.TestCase):
    def setUp(self):
        # Помощник читает модульный LOCAL_FINALIZE — гоним локальную ветку (без ssh).
        self._orig_local = collector.LOCAL_FINALIZE
        collector.LOCAL_FINALIZE = True
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # output-dir (эфемерный, как _tmp на VPS) и долговечный архив — РАЗНЫЕ корни.
        self.out = root / "_tmp" / "protocols" / "test-series"
        self.durable = root / "archive" / "test-series"
        self.out.mkdir(parents=True)
        self.durable.mkdir(parents=True)
        # finalize положил протокол + память серии рядом в output-dir.
        self.src_protocol = self.out / "2026-06-09.md"
        self.src_protocol.write_text(
            "# Протокол\n**Участники:** Мария Михина\n", encoding="utf-8",
        )
        self.src_mem = self.out / "2026-06-09-memory.json"
        self.src_mem.write_text(json.dumps(_MEMORY_JSON, ensure_ascii=False), encoding="utf-8")
        # коллектор разместил .md в долговечном архиве.
        self.target_md = self.durable / "2026-06-09.md"

    def tearDown(self):
        collector.LOCAL_FINALIZE = self._orig_local
        self._tmp.cleanup()

    def test_copies_sidecar_to_durable(self):
        collector._sync_series_memory(
            str(self.src_protocol), self.target_md, "auto-sid-20260609T100000Z",
        )
        dst = self.durable / "2026-06-09-memory.json"
        self.assertTrue(dst.exists(), "память серии должна доехать в долговечный архив рядом с .md")
        self.assertEqual(
            json.loads(dst.read_text(encoding="utf-8"))["participants"],
            _MEMORY_JSON["participants"],
        )
        # временный .part не оставлен.
        self.assertFalse((self.durable / "2026-06-09-memory.json.part").exists())

    def test_no_source_memory_is_silent_noop(self):
        self.src_mem.unlink()  # память не писалась (выключена / пустой протокол)
        try:
            collector._sync_series_memory(str(self.src_protocol), self.target_md, "sid")
        except Exception as e:  # noqa: BLE001
            self.fail(f"синк без файла-источника не должен падать: {e}")
        self.assertFalse((self.durable / "2026-06-09-memory.json").exists())

    def test_inplace_when_src_equals_dst(self):
        # output-dir == долговечный корень: память уже на месте → no-op, файл цел.
        target_md = self.out / "2026-06-09.md"  # та же папка, что и источник
        collector._sync_series_memory(str(self.src_protocol), target_md, "sid")
        self.assertTrue(self.src_mem.exists())
        self.assertEqual(json.loads(self.src_mem.read_text(encoding="utf-8"))["schema"], 1)
        self.assertFalse((self.out / "2026-06-09-memory.json.part").exists())

    def test_date_keyed_name_even_on_protocol_collision(self):
        # Протокол на долговечной стороне переименован в `<date>-<sid>.md` (коллизия дат),
        # но память кладётся по date-ключу `<date>-memory.json` (из stem источника) —
        # одна выжимка на дату серии, перефинализация перетирает свою же.
        target_md = self.durable / "2026-06-09-auto-sid.md"
        collector._sync_series_memory(str(self.src_protocol), target_md, "auto-sid")
        self.assertTrue((self.durable / "2026-06-09-memory.json").exists())
        self.assertFalse((self.durable / "2026-06-09-auto-sid-memory.json").exists())


if __name__ == "__main__":
    unittest.main()
