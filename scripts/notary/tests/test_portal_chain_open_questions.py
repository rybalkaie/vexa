"""Режим открытых вопросов едет по ЦЕПОЧКЕ портальных встреч, а не угадывается.

ЧТО СЛУЧИЛОСЬ 29.09.2026. Протокол «Еженедельной координации» снова ушёл в рабочую
группу с разделом «Вопросы с прошлых встреч, по которым не ясен статус» (17 пунктов,
самые старые — из июля), хотя у серии накопление выключено с 05.08.2026.

Починка 18.08 (`test_portal_series_open_questions.py`) научила портальную встречу брать
режим серии САМОЙ СВЕЖЕЙ встречи своей истории. Её тест проверял один шаг:
«календарная серия → первая портальная встреча». Через шесть недель вся история
(3 последние встречи с похожим составом) стала портальной: 18.08, 25.08, 08.09. У
портальной встречи записи в реестре нет → страховка «серии нет в реестре → carry» →
раздел снова напечатан. Здесь проверяем цепочку во времени, а не один шаг.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts
    PYTHONPATH=. python3 -m unittest notary.tests.test_portal_chain_open_questions -v
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

from notary.lib import series_memory as sm  # noqa: E402

CARRY = sm.OPEN_QUESTIONS_MODE_CARRY
LAST_ONLY = sm.OPEN_QUESTIONS_MODE_LAST_ONLY

KOORD = "series-ezhenedelnaya-koordinaciya-8399ea"
DIREKTORAT = "anzhee-direktorat"

# Реестр как на бою 29.09.2026: у координации поля режима нет (→ дефолт `last-only`),
# серия выключена с 18.08 (её ведёт портал); у Директората явный `carry`.
WATCHED = {
    "watched": [
        {"id": KOORD, "series": KOORD, "enabled": False},
        {"id": DIREKTORAT, "series": DIREKTORAT,
         "open_questions_mode": "carry", "enabled": True},
    ],
}


def digest(series: str, date: str, mode: str | None = None) -> dict:
    d = {"series": series, "date": date, "open_tasks": ["Кто-то: что-то"]}
    if mode is not None:
        d[sm.OPEN_QUESTIONS_MODE_KEY] = mode
    return d


def portal(n: int) -> str:
    return f"portal-0000000{n}-aaaa-bbbb-cccc-000000000000"


class _IsolatedProtocolsRoot(unittest.TestCase):
    """Каждый тест — в пустом каталоге протоколов: `portal_series_stored_mode` читает
    выжимки с диска, и живые файлы разработчика влиять на результат не должны."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"MEETING_NOTARY_PROTOCOLS_DIR": str(self.root)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def put_digest(self, series: str, date: str, mode: str | None = None) -> None:
        d = self.root / series
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{date}-memory.json").write_text(
            json.dumps(digest(series, date, mode), ensure_ascii=False), encoding="utf-8")


class TestChainOfPortalWeeks(_IsolatedProtocolsRoot):

    def test_history_of_only_portal_meetings_stays_last_only(self):
        """Ровно боевой случай 29.09: вся история портальная и без записанного режима."""
        history = [digest(portal(1), "2026-08-18"), digest(portal(2), "2026-08-25"),
                   digest(portal(3), "2026-09-08")]
        mode, determined = sm.resolve_open_questions_mode_for_meeting(
            portal(4), digests=history, watched=WATCHED)
        self.assertEqual(mode, LAST_ONLY, "раздел «с прошлых встреч» снова уйдёт в группу")
        self.assertTrue(determined)
        self.assertFalse(sm.carries_open_questions(portal(4), digests=history, watched=WATCHED))

    def test_five_weeks_on_portal_after_the_move(self):
        """Неделя за неделей после переезда: окно истории сползает с календаря на портал."""
        weeks = ["2026-08-18", "2026-08-25", "2026-09-01", "2026-09-08", "2026-09-15"]
        history = [digest(KOORD, "2026-07-28"), digest(KOORD, "2026-08-11")]
        for i, date in enumerate(weeks, start=1):
            window = history[-3:]  # `resolve_memory` отдаёт 3 последние похожие встречи
            mode, determined = sm.resolve_open_questions_mode_for_meeting(
                portal(i), digests=window, watched=WATCHED)
            with self.subTest(week=date, window=[d["series"][:9] for d in window]):
                self.assertEqual(mode, LAST_ONLY)
                self.assertTrue(determined)
            # finalize кладёт ОПРЕДЕЛЁННЫЙ режим в выжимку этой встречи
            history.append(digest(portal(i), date, mode))

    def test_old_digests_without_mode_are_skipped_not_guessed(self):
        """Свежая портальная без режима (файл до 29.09) → смотрим глубже, до календарной."""
        history = [digest(KOORD, "2026-08-11"), digest(portal(1), "2026-08-18")]
        self.assertEqual(
            sm.open_questions_mode_for_meeting(portal(2), digests=history, watched=WATCHED),
            LAST_ONLY)


class TestDirektoratKeepsCarryOnPortal(_IsolatedProtocolsRoot):
    """Обратная сторона: если Директорат переедет на портал, его висяки не теряются."""

    def test_carry_travels_down_the_chain(self):
        first, determined = sm.resolve_open_questions_mode_for_meeting(
            portal(1), digests=[digest(DIREKTORAT, "2026-09-02")], watched=WATCHED)
        self.assertEqual(first, CARRY)
        self.assertTrue(determined)
        history = [digest(DIREKTORAT, "2026-09-02"), digest(portal(1), "2026-09-09", first)]
        second = sm.open_questions_mode_for_meeting(portal(2), digests=history, watched=WATCHED)
        self.assertEqual(second, CARRY)
        # Календарной встречи в окне истории уже нет — режим держится записанным.
        history = [digest(portal(1), "2026-09-09", CARRY), digest(portal(2), "2026-09-16", CARRY),
                   digest(portal(3), "2026-09-23", CARRY)]
        self.assertTrue(sm.carries_open_questions(portal(4), digests=history, watched=WATCHED),
                        "Директорат на портале потерял бы висяки через три недели")

    def test_fresh_registry_series_wins_over_older_stored_mode(self):
        """Серия, известная реестру, — её режим СЕЙЧАС: смена режима владельцем действует."""
        history = [digest(portal(1), "2026-09-09", CARRY), digest(KOORD, "2026-09-16")]
        self.assertEqual(
            sm.open_questions_mode_for_meeting(portal(2), digests=history, watched=WATCHED),
            LAST_ONLY)
        history = [digest(KOORD, "2026-09-09"), digest(portal(1), "2026-09-16", CARRY)]
        self.assertEqual(
            sm.open_questions_mode_for_meeting(portal(2), digests=history, watched=WATCHED),
            CARRY)


class TestReadersWithoutHistory(_IsolatedProtocolsRoot):
    """Слушатель, сверщик и утренний дайджест знают только имя серии."""

    def test_portal_series_without_record_is_not_failsafe_carry(self):
        self.assertEqual(sm.open_questions_mode(portal(1), watched=WATCHED), LAST_ONLY)
        self.assertFalse(sm.carries_open_questions(portal(1), watched=WATCHED))

    def test_portal_series_reads_mode_from_its_own_digest(self):
        self.put_digest(portal(1), "2026-09-29", CARRY)
        self.assertEqual(sm.open_questions_mode(portal(1), watched=WATCHED), CARRY)
        self.assertTrue(sm.carries_open_questions(portal(1), watched=WATCHED))
        self.put_digest(portal(2), "2026-09-29", LAST_ONLY)
        self.assertEqual(sm.open_questions_mode(portal(2), watched=WATCHED), LAST_ONLY)

    def test_unknown_non_portal_series_keeps_failsafe_carry(self):
        """Страховку Директората не трогали: НЕпортальной серии нет в реестре → carry."""
        mode, determined = sm._open_questions_mode_ex("kakaya-to-seriya", watched=WATCHED)
        self.assertEqual(mode, CARRY)
        self.assertFalse(determined, "страховка не должна записываться в выжимку")


class TestUndeterminedIsNotStored(_IsolatedProtocolsRoot):

    def test_unreadable_registry_gives_carry_but_not_determined(self):
        history = [digest(KOORD, "2026-08-11")]
        with mock.patch.object(sm, "load_watched_for_modes", return_value=None):
            mode, determined = sm.resolve_open_questions_mode_for_meeting(
                portal(1), digests=history)
        self.assertEqual(mode, CARRY)
        self.assertFalse(determined)

    def test_unreadable_registry_still_uses_stored_modes(self):
        history = [digest(portal(1), "2026-09-22", LAST_ONLY)]
        with mock.patch.object(sm, "load_watched_for_modes", return_value=None):
            mode, determined = sm.resolve_open_questions_mode_for_meeting(
                portal(2), digests=history)
        self.assertEqual(mode, LAST_ONLY)
        self.assertTrue(determined)


class TestModeIsStoredInDigest(_IsolatedProtocolsRoot):

    PROTO = "# Протокол\n\n**Участники:** Ольга, Сона\n\n## Задачи\n\n- Ольга: закрыть месяц\n"

    def _saved(self, series: str, date: str) -> dict:
        return json.loads((self.root / series / f"{date}-memory.json").read_text(encoding="utf-8"))

    def test_mode_is_written_and_survives_reissue(self):
        sdir = self.root / portal(1)
        sdir.mkdir(parents=True)
        meta = {"series": portal(1), "date": "2026-09-29"}
        sm.save_meeting_digest(sdir, "2026-09-29", self.PROTO, meta, open_questions_mode=LAST_ONLY)
        self.assertEqual(self._saved(portal(1), "2026-09-29")[sm.OPEN_QUESTIONS_MODE_KEY], LAST_ONLY)
        # Перевыпуск без режима (ретрай, заливка записи) не стирает записанный.
        sm.save_meeting_digest(sdir, "2026-09-29", self.PROTO, meta)
        self.assertEqual(self._saved(portal(1), "2026-09-29")[sm.OPEN_QUESTIONS_MODE_KEY], LAST_ONLY)

    def test_garbage_mode_is_not_written(self):
        sdir = self.root / portal(2)
        sdir.mkdir(parents=True)
        meta = {"series": portal(2), "date": "2026-09-29"}
        sm.save_meeting_digest(sdir, "2026-09-29", self.PROTO, meta, open_questions_mode="карри")
        self.assertNotIn(sm.OPEN_QUESTIONS_MODE_KEY, self._saved(portal(2), "2026-09-29"))


if __name__ == "__main__":
    unittest.main()
