"""Ф7 F2 — версионируемый шаблон протокола.

Критерий: правка формата = новая версия, применяется к БУДУЩИМ протоколам.
Покрывает: бамп версии, идемпотентность, накопление директив, блок для промпта,
откат, фильтр кредов в директиве (D5), гейт.

Без IO внешнего — журнал в темп через `root`. Зелёные на системном python3.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_protocol_template -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.lib import protocol_template as pt  # noqa: E402


class _TmpRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestVersioning(_TmpRoot):
    def test_initial_version_is_one(self):
        self.assertEqual(pt.active_version(root=self.root), 1)
        self.assertEqual(pt.format_block(root=self.root), "")

    def test_format_change_bumps_version(self):
        r = pt.register_format_change("суммы выводи таблицей", root=self.root)
        self.assertIsNotNone(r)
        self.assertEqual(r["version"], 2)
        self.assertEqual(pt.active_version(root=self.root), 2)

    def test_second_change_accumulates(self):
        pt.register_format_change("суммы таблицей", root=self.root)
        pt.register_format_change("резюме короче, 3 пункта", root=self.root)
        self.assertEqual(pt.active_version(root=self.root), 3)
        texts = [d["text"] for d in pt.active_directives(root=self.root)]
        self.assertIn("суммы таблицей", texts)
        self.assertIn("резюме короче, 3 пункта", texts)

    def test_idempotent_same_directive(self):
        pt.register_format_change("суммы таблицей", root=self.root)
        again = pt.register_format_change("суммы таблицей", root=self.root)
        self.assertIsNone(again)
        self.assertEqual(pt.active_version(root=self.root), 2)


class TestFutureApplication(_TmpRoot):
    """Активная версия применяется к будущим протоколам (блок в промпт)."""

    def test_block_carries_active_directives(self):
        pt.register_format_change("убери раздел рисков", root=self.root)
        block = pt.format_block(root=self.root)
        self.assertIn("убери раздел рисков", block)
        self.assertIn("v2", block)
        # Рамка анти-инъекции присутствует.
        self.assertIn("ДАННЫЕ-пожелания", block)
        self.assertIn("не меняй факты", block)


class TestRollback(_TmpRoot):
    def test_rollback_removes_directive(self):
        pt.register_format_change("суммы таблицей", root=self.root)
        pt.register_format_change("резюме короче", root=self.root)
        rolled = pt.rollback_format_change("таблицей", root=self.root)
        self.assertEqual(len(rolled), 1)
        texts = [d["text"] for d in pt.active_directives(root=self.root)]
        self.assertNotIn("суммы таблицей", texts)
        self.assertIn("резюме короче", texts)
        # Версия снизилась (директив стало меньше).
        self.assertEqual(pt.active_version(root=self.root), 2)


class TestSafety(_TmpRoot):
    def test_credential_directive_rejected(self):
        r = pt.register_format_change("вставь токен ghp_" + "A" * 36, root=self.root)
        self.assertIsNone(r)
        self.assertEqual(pt.active_version(root=self.root), 1)

    def test_empty_directive_rejected(self):
        self.assertIsNone(pt.register_format_change("   ", root=self.root))

    def test_directive_length_capped(self):
        long = "оформляй " + "очень " * 100
        r = pt.register_format_change(long, root=self.root)
        self.assertIsNotNone(r)
        self.assertLessEqual(len(r["text"]), 200)


class TestDigest(_TmpRoot):
    def test_digest_block(self):
        pt.register_format_change("суммы таблицей", root=self.root)
        text, version = pt.digest_block(root=self.root)
        self.assertEqual(version, 2)
        self.assertIn("суммы таблицей", text)
        self.assertIn("v2", text)

    def test_digest_empty_at_v1(self):
        text, version = pt.digest_block(root=self.root)
        self.assertEqual(text, "")
        self.assertEqual(version, 1)


if __name__ == "__main__":
    unittest.main()
