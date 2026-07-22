# -*- coding: utf-8 -*-
"""ISS-44.1: «закрой 13, 14, 15, 17, 18» ОДНОЙ фразой (как человек отвечает по
нумерованному дайджесту). Раньше `_resolve_ordinal_target` при нескольких номерах отдавал
`unresolved` → бот честно, но зря переспрашивал. Теперь understand дробит статус-сегмент
с несколькими ординалами на под-сегмент на каждый номер и применяет каждый.

Офлайн: стаб ИИ (`understand_fn`), claude не зовётся. Снапшот = нумерованный список задач
отвечаемого документа (`snapshot[N-1]`)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import reply_understand as ru  # noqa: E402
from notary.lib import series_memory as sm  # noqa: E402


# 18 задач без владельца-префикса (не чужие → применяются), сквозная нумерация 1..18.
_SNAP = [f"Пункт номер {i} по проекту" for i in range(1, 19)]


def _canned(statuses):
    return {"statuses": [{"text": t, "cls": c} for (t, c) in statuses],
            "edits": [], "series_config": [], "block_close": False, "unclear": ""}


class TestMultiOrdinal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sd = Path(self._tmp.name) / "anzhee-direktorat"
        self.sd.mkdir(parents=True)
        self.memf = str(Path(self._tmp.name) / "decision-memory.jsonl")
        self._env = mock.patch.dict(os.environ, {
            "ENABLE_REPLY_UNDERSTAND": "1",
            "MEETING_NOTARY_DECISION_MEMORY_FILE": self.memf,
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, reply_text, statuses):
        return ru.understand_reply(
            reply_text, series_dir=self.sd, replier="Илья Рыбалка",
            snapshot=list(_SNAP), understand_fn=lambda t, items: _canned(statuses),
            date="2026-07-22")

    def test_multi_ordinal_one_phrase_applies_each(self):
        """«закрой 13, 14, 15, 17, 18» → применены РОВНО эти 5 задач, ни одного вопроса."""
        r = self._run("закрой 13, 14, 15, 17, 18",
                      [("закрой 13, 14, 15, 17, 18", "done")])
        matched = sorted(a["matched"] for a in r["applied"])
        self.assertEqual(len(r["applied"]), 5, f"ждали 5 применённых, got {len(r['applied'])}")
        self.assertEqual(matched, sorted(_SNAP[i - 1] for i in (13, 14, 15, 17, 18)))
        self.assertEqual(r["questions"], [], "не должно быть переспроса — все номера в диапазоне")
        self.assertTrue(all(a["via"] == "ordinal" for a in r["applied"]))

    def test_single_ordinal_still_works(self):
        """Регресс: один ординал по-прежнему резолвится (дробление его не ломает)."""
        r = self._run("18 сделал", [("18 сделал", "done")])
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(r["applied"][0]["matched"], _SNAP[17])

    def test_out_of_range_in_bare_list_does_not_false_apply(self):
        """Безопасность (R5): в голом списке есть номер вне диапазона (99) → НЕ угадываем,
        ничего ложно не закрываем (лучше честно переспросить, чем закрыть не ту задачу)."""
        r = self._run("закрой 2, 3, 99", [("закрой 2, 3, 99", "done")])
        self.assertEqual(len(r["applied"]), 0,
                         "смешанный in/out-range голый список не должен ничего закрывать")

    def test_ordinal_suffix_hyphen_resolved(self):
        """Цикл5: русский порядковый суффикс «18-е»/«17-й» резолвится (не теряется остатком)."""
        r = self._run("17-е и 18-е закрывай", [("17-е и 18-е закрывай", "done")])
        self.assertEqual(sorted(a["matched"] for a in r["applied"]),
                         sorted([_SNAP[16], _SNAP[17]]))

    def test_range_and_amount_not_ordinals(self):
        """Безопасность: диапазон «3-5» и сумма «3 млн» НЕ трактуются как ординалы."""
        self.assertEqual(ru._bare_number_list("собрать 3-5 млн", 18), set())
        self.assertEqual(ru._bare_number_list("нужно 3 млн к 5 числу", 18), set())

    def test_no_snapshot_falls_back_no_crash(self):
        """Без слепка (snapshot=None) множественные ординалы не дробятся — прежнее поведение."""
        r = ru.understand_reply(
            "закрой 13, 14, 15", series_dir=self.sd, replier="Илья Рыбалка",
            snapshot=None, understand_fn=lambda t, items: _canned([("закрой 13, 14, 15", "done")]),
            date="2026-07-22")
        # Без слепка ординал недоступен → overlap-путь (0 применённых на синтетике), но НЕ падение.
        self.assertIsInstance(r["applied"], list)


if __name__ == "__main__":
    unittest.main()
