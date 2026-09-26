"""ISS-60, фаза 6б (копилка владельца №6, 26.09.2026): встреча «лично мне» — не в памяти серии.

Протокол, собранный из присланной записи лично владельцу (путь А обёртки
`tools/ingest-recording.py`) по серии с групповым чатом, группа не получала. Раньше его
выжимка всё равно ложилась в память серии, и следующие протоколы группы на неё
опирались (участники, темы, хвост открытых задач). Теперь:

  • обёртка ставит в meta пометку `excludeFromSeriesMemory`;
  • `build_digest` переносит её в выжимку (`excluded_from_series_memory`);
  • `list_series_digests` — единственная дверь чтения прошлых встреч — такие выжимки
    пропускает: их не видят ни память серии, ни подбор по составу из других серий, ни
    кросс-фон, ни хвост открытых задач, ни утренний дайджест;
  • пометка переживает правку авторства и пересборку выжимок бэкфиллом.

Сквозная проверка через обёртку (путь А — не берёт, путь Б — берёт) —
`test_phase4_ingest_recovery.TestPersonalRecoveryOutOfSeriesMemory`.

Запуск:
    cd ~/Projects/meeting-notary/vexa-resilience/scripts/notary
    python3 -m unittest tests.test_iss60_personal_series_memory -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from lib import series_memory as sm  # noqa: E402

PEOPLE = ["Иван Петров", "Мария Сидорова"]
PROTOCOL = (
    "# Протокол встречи\n\n**Участники:** Иван Петров, Мария Сидорова\n\n"
    "## 1) Бюджет\n▪️ обсудили бюджет квартала\n\n"
    "## Задачи\n**Мария Сидорова**\n- согласовать бюджет с поставщиком\n"
)


def _meta(date: str, *, personal: bool = False) -> dict:
    meta = {"series": "grp", "date": date, "participants": list(PEOPLE)}
    if personal:
        meta[sm.META_EXCLUDE_KEY] = True
    return meta


class _Dirs(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="personal-mem-"))
        self.grp = self.root / "grp"
        self.other = self.root / "one-on-one"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def save(self, sdir: Path, date: str, *, personal: bool = False) -> Path:
        return sm.save_meeting_digest(sdir, date, PROTOCOL, _meta(date, personal=personal))


class DigestMarkTest(_Dirs):
    def test_mark_goes_from_meta_into_digest(self):
        self.assertIs(sm.build_digest(PROTOCOL, _meta("2026-09-23", personal=True))[sm.EXCLUDED_KEY], True)
        self.assertNotIn(sm.EXCLUDED_KEY, sm.build_digest(PROTOCOL, _meta("2026-09-23")))
        # Только настоящее True: строка или 1 — не наша пометка.
        for junk in ("true", 1, "yes"):
            meta = _meta("2026-09-23")
            meta[sm.META_EXCLUDE_KEY] = junk
            self.assertNotIn(sm.EXCLUDED_KEY, sm.build_digest(PROTOCOL, meta), junk)

    def test_series_memory_skips_personal_meeting(self):
        self.save(self.grp, "2026-09-16")
        path = self.save(self.grp, "2026-09-23", personal=True)
        self.assertTrue(path and path.is_file(), "файл выжимки нужен перевыпуску этой встречи")
        self.assertEqual([d["date"] for d in sm.list_series_digests(self.grp)], ["2026-09-16"])
        got = sm.resolve_memory(self.grp, self.root, current_participants=PEOPLE, current_date="2026-09-30")
        self.assertEqual([d["date"] for d in got], ["2026-09-16"])

    def test_open_tasks_tail_does_not_carry_personal_tasks(self):
        self.save(self.grp, "2026-09-23", personal=True)
        self.assertEqual(sm.resolve_open_tasks(sm.list_series_digests(self.grp)), [])
        self.save(self.grp, "2026-09-24")
        tail = sm.resolve_open_tasks(sm.list_series_digests(self.grp))
        self.assertTrue(any("поставщиком" in t for t in tail), tail)

    def test_other_series_do_not_see_it_by_participants(self):
        # Подбор по составу (новая серия без своей памяти) и кросс-фон читают чужие серии.
        self.save(self.grp, "2026-09-23", personal=True)
        self.other.mkdir()

        def cross() -> list:
            # Классификатор «ничего чувствительного»: фон отбрасывается только пометкой.
            return [d["date"] for d in sm.resolve_cross_memory(
                self.root, current_series_dir=self.other, current_participants=PEOPLE,
                sensitive_classifier=lambda items: [False] * len(items), today="2026-09-30")]

        got = sm.resolve_memory(self.other, self.root, current_participants=PEOPLE, current_date="2026-09-30")
        self.assertEqual(got, [])
        self.assertEqual(cross(), [])
        self.save(self.grp, "2026-09-24")
        got = sm.resolve_memory(self.other, self.root, current_participants=PEOPLE, current_date="2026-09-30")
        self.assertEqual([d["date"] for d in got], ["2026-09-24"])
        self.assertEqual(cross(), ["2026-09-24"])

    def test_mark_survives_author_fix_and_backfill_rebuild(self):
        sm.save_meeting_digest(self.grp, "2026-09-23", PROTOCOL, _meta("2026-09-23", personal=True),
                               speaker_mapping={"S1": "Иван Петров"})
        self.assertTrue(sm.update_digest_speaker_mapping(self.grp, "2026-09-23", {"Иван Петров": "Мария Сидорова"}))
        self.assertTrue(sm.is_excluded_digest(sm.load_digest(sm.digest_path(self.grp, "2026-09-23"))))
        (self.grp / "2026-09-23-protokol.md").write_text(PROTOCOL, encoding="utf-8")
        (self.grp / "2026-09-24-protokol.md").write_text(PROTOCOL, encoding="utf-8")
        self.assertEqual(sm.backfill_series(self.grp, overwrite=True), 2)
        self.assertTrue(sm.is_excluded_digest(sm.load_digest(sm.digest_path(self.grp, "2026-09-23"))))
        self.assertFalse(sm.is_excluded_digest(sm.load_digest(sm.digest_path(self.grp, "2026-09-24"))))
        self.assertEqual([d["date"] for d in sm.list_series_digests(self.grp)], ["2026-09-24"])


if __name__ == "__main__":
    unittest.main()
