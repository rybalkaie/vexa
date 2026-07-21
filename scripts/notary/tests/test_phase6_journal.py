# -*- coding: utf-8 -*-
"""Ф6 (план `2026-07-20-watson-smart-replies-rethink.md`): журнал незнакомых слов и
пометок — R20 (слово, применённое мимо словаря) + У4 (что пометила вокатив-проверка Ф5).

ОДНО хранилище с памятью R24 (`decision_memory`, не два — РАЗМ1). Опасная тройка: в файл
кладём слова/имена/классы/даты/исходы, но НИКОГДА тексты реплик или транскрипта.

  • R20 — journal: слово, применённое ИИ/памятью мимо словаря, + исход + дата (проверка
    сквозной проводки — в golden G-R7; тут ре-подтверждаем механику хранилища);
  • У4 — vocative: «vocative-пара помечена» (имена пары + дата), пишется из РЕАЛЬНОГО
    ревью-контура `review_and_flag_protocol_file` при `ENABLE_VOCATIVE_JOURNAL=1`
    (дефолт-OFF в коде — тесты не пишут в общий с продом файл).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python -m unittest tests.test_phase6_journal -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from notary.lib import decision_memory as dm  # noqa: E402


def _transcript(*lines: str) -> str:
    return "\n".join(lines) + "\n"


_PROTOCOL = (
    "#протоколвстречи 21.07.2026\n"
    "**Встреча:** Синк директоров\n"
    "**Участники:** Татьяна, Мария\n"
    "\n---\n\n"
    "## Задачи\n"
    "**Татьяна:** подготовить отчёт по продажам\n"
    "**Мария:** обновить прайс к пятнице\n"
)


# ==========================================================================
# У4 — decision_memory.record_vocative_flag (единица)
# ==========================================================================
class TestVocativeFlagUnit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = Path(self._tmp.name) / "mem.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_records_pair_sorted(self):
        self.assertTrue(dm.record_vocative_flag("Татьяна", "Мария", store_path=self.p))
        evs = [e for e in dm.journal_entries(store_path=self.p) if e.get("ev") == "vocative"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["pair"], ["мария", "татьяна"])  # отсортированы, ё→е/lower
        self.assertIn("date", evs[0])

    def test_short_name_side_noop(self):
        # пустое/слишком короткое имя любой стороны → не журналим
        self.assertFalse(dm.record_vocative_flag("", "Мария", store_path=self.p))
        self.assertFalse(dm.record_vocative_flag("Я", "Мария", store_path=self.p))

    def test_vocative_not_in_active_map(self):
        # У4-событие — аудит, на активную карту слов R24 НЕ влияет (_fold игнорирует).
        dm.record_vocative_flag("Татьяна", "Мария", store_path=self.p)
        self.assertEqual(dm.known_map(store_path=self.p), {})

    def test_no_reply_text_stored(self):
        # Опасная тройка: только имена+дата, ни текста реплики, ни транскрипта.
        dm.record_vocative_flag("Татьяна", "Мария", store_path=self.p)
        blob = self.p.read_text(encoding="utf-8")
        self.assertNotIn("продаж", blob.lower())
        self.assertNotIn("привет", blob.lower())


# ==========================================================================
# R20 — журнал незнакомых слов в ТОМ ЖЕ хранилище (механика)
# ==========================================================================
class TestUnknownWordJournal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = Path(self._tmp.name) / "mem.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_journal_and_vocative_share_store(self):
        dm.record_unknown_word("разрулила", outcome="done", store_path=self.p)
        dm.record_vocative_flag("Татьяна", "Мария", store_path=self.p)
        kinds = sorted({e.get("ev") for e in dm.journal_entries(store_path=self.p)})
        self.assertEqual(kinds, ["journal", "vocative"])  # одно хранилище, два вида событий


# ==========================================================================
# У4 — журнал из РЕАЛЬНОГО ревью-контура (review_and_flag_protocol_file)
# ==========================================================================
class TestVocativeJournalViaContour(unittest.TestCase):
    def setUp(self):
        from lib import llm_postprocess as lp
        self.lp = lp
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.memf = d / "decision-memory.jsonl"
        self.protocol_path = d / "2026-07-21-protokol.md"
        self.transcript_path = d / "2026-07-21.md"
        self.protocol_path.write_text(_PROTOCOL, encoding="utf-8")
        # своп-конфликт: «Татьяна» зовёт «Тань» (сама себя) — пара помечается
        self.transcript_path.write_text(_transcript(
            "**[00:01] Татьяна:** Привет, Тань, как продажи?",
            "**[00:02] Мария:** Машунь, привет! Норм.",
        ), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, extra_env):
        env = {"ENABLE_PROTOCOL_REVIEW": "0", "ENABLE_VOCATIVE_CHECK": "1",
               "MEETING_NOTARY_DECISION_MEMORY_FILE": str(self.memf)}
        env.update(extra_env)
        with mock.patch.dict(os.environ, env, clear=False):
            self.lp.review_and_flag_protocol_file(
                protocol_path=self.protocol_path,
                transcript_path=self.transcript_path,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="test-sid",
                rewrite=True,  # finalize/clarify зовут с rewrite=True
            )

    def _vocative_events(self):
        if not self.memf.exists():
            return []
        return [e for e in dm.journal_entries(store_path=self.memf) if e.get("ev") == "vocative"]

    def test_journal_on_when_flag_set(self):
        self._run({"ENABLE_VOCATIVE_JOURNAL": "1"})
        evs = self._vocative_events()
        self.assertTrue(evs, "У4: помеченная вокатив-пара не попала в журнал при флаге ON")
        # ровно одна запись на пару (не по 2 finding), имена нормализованы
        self.assertEqual(len(evs), 1)
        self.assertEqual(len(evs[0]["pair"]), 2)
        # опасная тройка: в файле нет текста реплик транскрипта
        blob = self.memf.read_text(encoding="utf-8")
        self.assertNotIn("продаж", blob.lower())

    def test_journal_off_by_default(self):
        # дефолт-OFF: тот же прогон без флага НЕ пишет vocative-событий (общий файл чист).
        self._run({})
        self.assertEqual(self._vocative_events(), [])

    def test_plaque_still_written_without_journal(self):
        # журнал — надстройка: без него плашка/врезка вокатива всё равно ставятся.
        self._run({})
        written = self.protocol_path.read_text(encoding="utf-8")
        self.assertIn(self.lp.SPEAKER_PLAQUE_SENTINEL, written)


if __name__ == "__main__":
    unittest.main()
