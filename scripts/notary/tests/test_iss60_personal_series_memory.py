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

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    # --- цикл5 фазы 6б (Н6): путь А без выжимки (протокол из присланного текста)

    def test_mark_without_digest_leaves_stub_that_everyone_skips(self):
        self.save(self.grp, "2026-09-16")
        self.assertIsNotNone(sm.mark_excluded_from_series_memory(self.grp, "2026-09-23", series="grp"))
        self.assertTrue(sm.is_excluded_digest(sm.load_digest(sm.digest_path(self.grp, "2026-09-23"))))
        self.assertEqual([d["date"] for d in sm.list_series_digests(self.grp)], ["2026-09-16"])
        # Ручная пересборка бэкфиллом не возвращает встречу в память.
        (self.grp / "2026-09-16-protokol.md").write_text(PROTOCOL, encoding="utf-8")
        (self.grp / "2026-09-23-protokol.md").write_text(PROTOCOL, encoding="utf-8")
        sm.backfill_series(self.grp, overwrite=True)
        self.assertEqual([d["date"] for d in sm.list_series_digests(self.grp)], ["2026-09-16"])

    def test_mark_keeps_existing_digest_content(self):
        self.save(self.grp, "2026-09-23", personal=True)
        before = sm.load_digest(sm.digest_path(self.grp, "2026-09-23"))
        sm.mark_excluded_from_series_memory(self.grp, "2026-09-23")
        self.assertEqual(sm.load_digest(sm.digest_path(self.grp, "2026-09-23")), before)


class ResendToRecipientsTest(_Dirs):
    """Цикл5 фазы 6б (Н5): «пришлите протокол» не из лички владельца не отдаёт протокол
    «лично мне» — получатели серии его не получали. Владельцу — отдаёт."""

    GROUP, OWNER = -100500, 111

    def setUp(self) -> None:
        super().setUp()
        sys.path.insert(0, str(_HERE.parent.parent))
        import notary.meetings_listener as ml  # noqa: PLC0415

        self.ml = ml
        self._env = mock.patch.dict(os.environ, {"MEETING_NOTARY_PROTOCOLS_DIR": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.save(self.grp, "2026-09-16")
        (self.grp / "2026-09-16-protokol.md").write_text(PROTOCOL, encoding="utf-8")
        self.save(self.grp, "2026-09-23", personal=True)
        (self.grp / "2026-09-23-protokol.md").write_text(PROTOCOL, encoding="utf-8")

    def test_latest_protocol_skips_personal_for_recipients(self):
        self.assertEqual(self.ml._latest_protocol_path("grp")[1], "2026-09-23")
        self.assertEqual(self.ml._latest_protocol_path("grp", skip_personal=True)[1], "2026-09-16")
        # Протокол из присланного текста — выжимки нет, только пометка-заглушка.
        (self.grp / "2026-09-30-protokol.md").write_text(PROTOCOL, encoding="utf-8")
        sm.mark_excluded_from_series_memory(self.grp, "2026-09-30")
        self.assertEqual(self.ml._latest_protocol_path("grp", skip_personal=True)[1], "2026-09-16")

    def test_only_personal_protocols_gives_nothing_to_recipients(self):
        (self.grp / "2026-09-16-protokol.md").unlink()
        self.assertIsNone(self.ml._latest_protocol_path("grp", skip_personal=True))

    def test_resend_passes_whose_chat(self):
        for owner_chat, skip in ((False, True), (True, False)):
            with mock.patch.object(self.ml, "_latest_protocol_path", return_value=None) as lp, \
                    mock.patch.object(self.ml, "send_message"):
                self.ml._resend_protocol("tok", self.GROUP, "grp", {"message_id": 1}, owner_chat=owner_chat)
            self.assertEqual(lp.call_args.kwargs, {"skip_personal": skip}, owner_chat)

    def test_route_marks_owner_chat_only_for_owner(self):
        msg = {"from": {"id": 42, "first_name": "Илья"}, "message_id": 1000, "date": 1718000000,
               "text": "пришли протокол", "reply_to_message": {"message_id": 900, "text": "протокол"}}
        for cid, owner_chat in ((self.GROUP, False), (self.OWNER, True)):
            with mock.patch.object(self.ml, "_reply_context_series", return_value="grp"), \
                    mock.patch.object(self.ml, "_reply_context_series_digest", return_value=None), \
                    mock.patch.object(self.ml, "_resend_protocol", return_value=True) as rp:
                self.assertTrue(self.ml.maybe_route_to_send_artifact(
                    "tok", cid, self.OWNER, dict(msg, chat={"id": cid})))
            self.assertEqual(rp.call_args.kwargs, {"owner_chat": owner_chat}, cid)


if __name__ == "__main__":
    unittest.main()
