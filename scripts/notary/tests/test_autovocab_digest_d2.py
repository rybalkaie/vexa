"""Ф7 D2 — еженедельный отчёт «внёс вот это, есть корректировки?».

Критерий D2: бот вносит сам, раз в неделю отчитывается списком внесённого/
предложенного знания (адресовано контексту компании) + версия шаблона, и даёт
окно на корректировку (не пред-подтверждение).

Изоляция: outbox/template/vocab-state — в темп через env. Без сети (dry_run).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_autovocab_digest_d2 -v
"""
from __future__ import annotations

import os
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

from notary.auto_vocab import digest  # noqa: E402
from notary.lib import knowledge_writeback as wb  # noqa: E402
from notary.lib import protocol_template as pt  # noqa: E402


class _IsolatedMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env = {
            "NOTARY_KNOWLEDGE_OUTBOX_DIR": str(base / "outbox"),
            "NOTARY_PRIVATE_KNOWLEDGE_QUEUE": str(base / "private.md"),
            "NOTARY_KNOWLEDGE_RATCHET_PATH": str(base / "ratchet.json"),
            "NOTARY_TEMPLATE_DIR": str(base / "tmpl"),
            "MEETING_NOTARY_CONTEXT_DIR": str(base / "no-context"),
            # auto_vocab state в темп — чтобы week_stats не читал боевой файл.
            "AUTO_VOCAB_STATE_PATH": str(base / "auto_vocab_state.json"),
            "SPEECHMATICS_VOCAB_PATH": str(base / "config" / "speechmatics-vocab.json"),
        }
        self._old = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class TestKnowledgeSection(_IsolatedMixin):
    def test_empty_when_nothing(self):
        self.assertEqual(digest.format_knowledge_section(), "")

    def test_reports_company_addressed_proposals(self):
        wb.propose_term("оффер", series="s1", company="anzhee", publication_allowed=True)
        wb.propose_role("Мария Михина", "поставки", series="s1", company="anzhee",
                        publication_allowed=True)
        section = digest.format_knowledge_section()
        # Адресовано КОНТЕКСТУ КОМПАНИИ, не коду (критерий D1/D2).
        self.assertIn("контекст компаний", section)
        self.assertIn("anzhee-context", section)
        self.assertIn("оффер", section)
        self.assertIn("Мария Михина | поставки", section)
        # Окно на корректировку (не пред-подтверждение).
        self.assertIn("есть корректировки", section)
        self.assertIn("переноси", section)

    def test_reports_template_version(self):
        pt.register_format_change("суммы выводи таблицей")
        section = digest.format_knowledge_section()
        self.assertIn("Шаблон протокола v2", section)
        self.assertIn("суммы выводи таблицей", section)

    def test_not_provisioned_note(self):
        wb.propose_term("оффер", series="s1", company="anzhee", publication_allowed=True)
        section = digest.format_knowledge_section()
        self.assertIn("провижининге", section)


class TestRunComposition(_IsolatedMixin):
    def test_run_includes_knowledge(self):
        wb.propose_term("Bolong", series="s2", company="mpfirst", publication_allowed=True)
        msg = digest.run(dry_run=True)
        # Словарная часть + секция знания в одном сообщении.
        self.assertIn("Словарь", msg)
        self.assertIn("Bolong", msg)
        self.assertIn("mpfirst-context", msg)

    def test_run_dry_run_no_send(self):
        # Пустая неделя без знания — только словарная часть, без падений.
        msg = digest.run(dry_run=True)
        self.assertTrue(msg)


if __name__ == "__main__":
    unittest.main()
