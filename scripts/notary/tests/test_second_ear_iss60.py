"""ISS-60, фаза 4: второе ухо бота на стороне хоста.

Бот пишет, кроме основной записи, «второе ухо» — всё, что играл браузер
(`<дата>-<сессия>.chunk0.wav`), и сам решает, оглохла ли основная
(`meta.recording.audioSource`). Здесь проверяется, что хост с этим обращается так
же, как с основной записью, и не подмешивает второе ухо, пока оно не нужно:

  • финализация: пока основная слышит — звук только из неё (двойного звука нет);
    основная оглохла — звук из второго уха, владельцу одна строка без текста встречи;
  • удержание для портала знает файл второго уха (маркеры рядом с записью);
  • сборщик удаляет второе ухо ровно там и тогда, где удаляет основную запись.

Запуск:
    cd ~/Projects/meeting-notary/vexa-resilience/scripts/notary
    python3 -m unittest tests.test_second_ear_iss60 -v
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import portal_delivery, protocol_to_tg, second_ear, wav_concat  # noqa: E402

SR = 16000
SID = "2026-09-25-tm-1727250000000"


def _load_collector():
    spec = importlib.util.spec_from_file_location("collector_second_ear", str(_NOTARY / "collector.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_collector()


def _wav(path: Path, samples: int, value: int = 1000) -> Path:
    pcm = (value.to_bytes(2, "little", signed=True)) * samples
    path.write_bytes(wav_concat.build_wav_header(len(pcm), SR) + pcm)
    return path


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="ear-"))
        self.main1 = self.dir / f"{SID}.chunk1.wav"
        self.main2 = self.dir / f"{SID}.chunk2.wav"
        self.ear = self.dir / f"{SID}.chunk0.wav"

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def meta(self, *, source: str = "primary", chunks=None, files_wav=None, **rec):
        chunks = chunks if chunks is not None else [{"idx": 1, "wav": str(self.main1)}]
        recording = {"chunks": chunks, "audioSource": source,
                     "secondEar": {"wav": str(self.ear), "status": "stopped"}, **rec}
        return {"sessionUid": "tm-1727250000000", "series": "koordinatsiya",
                "startTs": "2026-09-25T09:00:00Z", "meetingUrl": "https://telemost.yandex.ru/j/00000000000000",
                "recording": recording, "files": {"wav": str(files_wav or self.main1)}}


class EarPathTest(_Tmp):
    def test_file_names(self):
        self.assertTrue(second_ear.is_ear_file(f"/transcripts/{SID}.chunk0.wav"))
        for name in (f"/transcripts/{SID}.chunk1.wav", f"/transcripts/{SID}.chunk10.wav", "x.wav", None, 5):
            self.assertFalse(second_ear.is_ear_file(name), name)

    def test_ear_path_from_meta_is_checked(self):
        self.assertEqual(second_ear.ear_wav(self.meta()), str(self.ear))
        bad = self.meta()
        bad["recording"]["secondEar"]["wav"] = "/transcripts/../../etc/x.chunk0.wav"
        self.assertIsNone(second_ear.ear_wav(bad))
        bad["recording"]["secondEar"]["wav"] = "/etc/passwd"
        self.assertIsNone(second_ear.ear_wav(bad))
        self.assertIsNone(second_ear.ear_wav({"recording": {}}))
        self.assertIsNone(second_ear.ear_wav(None))

    def test_uses_ear(self):
        self.assertFalse(second_ear.uses_ear(self.meta()))
        self.assertTrue(second_ear.uses_ear(self.meta(source="second_ear")))
        self.assertFalse(second_ear.uses_ear({}))

    def test_name_matches_bot_chunk_naming(self):
        # Бот: `${stamp}-${sessionUid}.chunk${idx}.wav`, второе ухо — idx 0.
        self.assertRegex(self.ear.name, r"^\d{4}-\d{2}-\d{2}-.+\.chunk0\.wav$")
        self.assertTrue(re.search(r"\.chunk\d+\.wav$", self.ear.name))


class OwnerNoteTest(_Tmp):
    def deaf_meta(self, **deaf):
        base = {"rule": "sustained", "since": "2026-09-25T09:12:00Z", "primaryLoudS": 0, "earLoudS": 600,
                "forced": False}
        base.update(deaf)
        return self.meta(source="second_ear", primaryDeaf=base)

    def test_no_note_while_primary_hears(self):
        self.assertIsNone(second_ear.owner_note(self.meta()))

    def test_sustained_note_plain_and_private(self):
        text = second_ear.owner_note(self.deaf_meta())
        self.assertIn("koordinatsiya", text)
        self.assertIn("2026-09-25", text)
        self.assertIn("с 12:12 МСК", text)
        self.assertIn("минуту подряд", text)
        self.assertIn("второго уха", text)
        # Ссылку на комнату и что-либо из встречи не пишем.
        self.assertNotIn("telemost", text)
        self.assertNotIn("http", text)

    def test_ear_heard_more_note_has_minutes(self):
        text = second_ear.owner_note(self.deaf_meta(rule="ear_heard_more", primaryLoudS=60, earLoudS=1800))
        self.assertIn("1 мин", text)
        self.assertIn("30 мин", text)

    def test_forced_note_says_it_is_a_check(self):
        self.assertIn("проверка", second_ear.owner_note(self.deaf_meta(forced=True)))


class NotifyTest(_Tmp):
    def test_push_once_per_meeting(self):
        calls = []
        meta = self.meta(source="second_ear", primaryDeaf={"rule": "sustained"})
        ok = second_ear.notify_owner_if_ear(meta, session_uid="tm-1", push=lambda t, **kw: calls.append(kw) or True)
        self.assertTrue(ok)
        self.assertEqual(calls, [{"dedupe_key": "second-ear:tm-1"}])

    def test_no_repeat_on_regeneration_after_delivery(self):
        calls = []
        meta = self.meta(source="second_ear", primaryDeaf={"rule": "sustained"})
        meta["delivered"] = [{"chat_id": 1, "message_ids": [5]}]
        self.assertFalse(second_ear.notify_owner_if_ear(meta, session_uid="tm-1",
                                                        push=lambda t, **kw: calls.append(t)))
        self.assertEqual(calls, [])

    def test_no_push_when_primary(self):
        calls = []
        self.assertFalse(second_ear.notify_owner_if_ear(self.meta(), session_uid="tm-1",
                                                        push=lambda t, **kw: calls.append(t)))
        self.assertEqual(calls, [])

    def test_push_failure_never_raises(self):
        def boom(_t, **_kw):
            raise RuntimeError("tg down")

        meta = self.meta(source="second_ear", primaryDeaf={"rule": "sustained"})
        self.assertFalse(second_ear.notify_owner_if_ear(meta, session_uid="tm-1", push=boom))


class FinalizeAudioTest(_Tmp):
    """Звук для расшифровки: пока основная слышит — только основная (без двойного звука)."""

    def test_single_primary_chunk_ignores_ear_on_disk(self):
        _wav(self.main1, SR * 3)
        _wav(self.ear, SR * 5, value=7)
        path, temp = wav_concat.resolve_wav_for_stt(self.meta())
        self.assertEqual((path, temp), (str(self.main1), False))

    def test_multichunk_concat_excludes_ear(self):
        _wav(self.main1, SR * 3)
        _wav(self.main2, SR * 2)
        _wav(self.ear, SR * 7, value=7)
        meta = self.meta(chunks=[{"idx": 1, "wav": str(self.main1)}, {"idx": 2, "wav": str(self.main2)}])
        path, temp = wav_concat.resolve_wav_for_stt(meta, tmp_dir=self.dir)
        self.assertTrue(temp)
        pcm, _sr = wav_concat.read_pcm(path)
        self.assertEqual(len(pcm) // 2, SR * 5)  # 3 + 2 с основной, второго уха нет

    def test_swapped_meta_takes_ear_only(self):
        _wav(self.main1, SR * 3, value=0)
        _wav(self.ear, SR * 5)
        meta = self.meta(source="second_ear", chunks=[{"idx": 0, "wav": str(self.ear)}], files_wav=self.ear,
                         primaryChunks=[{"idx": 1, "wav": str(self.main1)}])
        path, temp = wav_concat.resolve_wav_for_stt(meta)
        self.assertEqual((path, temp), (str(self.ear), False))

    def test_duration_label_from_ear_chunk(self):
        meta = self.meta(source="second_ear", chunks=[
            {"idx": 0, "wav": str(self.ear), "firstSpeechMs": 1_000, "lastSpeechMs": 1_000 + 25 * 60_000}])
        self.assertEqual(protocol_to_tg.compute_duration_label(meta), "25 мин")


class RetentionTest(_Tmp):
    """Удержание для портала: маркеры ставятся на ВСЕ куски встречи, второе ухо — тоже."""

    def test_session_files_include_ear_from_primary(self):
        for p in (self.main1, self.main2, self.ear):
            p.write_bytes(b"RIFF")
        names = [p.name for p in portal_delivery.session_wav_files(str(self.main1))]
        self.assertEqual(names, [self.ear.name, self.main1.name, self.main2.name])

    def test_session_files_from_ear_include_primary(self):
        for p in (self.main1, self.ear):
            p.write_bytes(b"RIFF")
        names = [p.name for p in portal_delivery.session_wav_files(str(self.ear))]
        self.assertEqual(names, [self.ear.name, self.main1.name])


class CollectorCleanupTest(_Tmp):
    """Сборщик удаляет второе ухо там же и тогда же, где основную запись."""

    def setUp(self) -> None:
        super().setUp()
        self.held = False
        self._p = [
            mock.patch.object(collector, "LOCAL_FINALIZE", True),
            mock.patch.object(collector, "_portal_sources_held", side_effect=lambda *_a, **_k: self.held),
            mock.patch.object(collector, "_portal_report_purged", lambda sid: None),
        ]
        for p in self._p:
            p.start()
        for p in (self.main1, self.ear):
            p.write_bytes(b"RIFF")

    def tearDown(self) -> None:
        for p in reversed(self._p):
            p.stop()
        super().tearDown()

    def test_both_deleted_after_delivery(self):
        collector._maybe_cleanup_wav_after_delivered(self.meta(), SID)
        self.assertFalse(self.main1.exists())
        self.assertFalse(self.ear.exists())

    def test_ear_deleted_even_if_primary_already_gone(self):
        self.main1.unlink()
        collector._maybe_cleanup_wav_after_delivered(self.meta(), SID)
        self.assertFalse(self.ear.exists())

    def test_keep_audio_keeps_both(self):
        meta = self.meta()
        meta["keep_audio"] = True
        collector._maybe_cleanup_wav_after_delivered(meta, SID)
        self.assertTrue(self.main1.exists() and self.ear.exists())

    def test_portal_hold_keeps_both(self):
        self.held = True
        collector._maybe_cleanup_wav_after_delivered(self.meta(), SID)
        self.assertTrue(self.main1.exists() and self.ear.exists())

    def test_never_from_mac(self):
        with mock.patch.object(collector, "LOCAL_FINALIZE", False):
            collector._maybe_cleanup_wav_after_delivered(self.meta(), SID)
        self.assertTrue(self.ear.exists())

    def test_ear_as_protocol_source_deleted_once_primary_chunks_kept(self):
        meta = self.meta(source="second_ear", chunks=[{"idx": 0, "wav": str(self.ear)}], files_wav=self.ear)
        collector._maybe_cleanup_wav_after_delivered(meta, SID)
        self.assertFalse(self.ear.exists())
        self.assertTrue(self.main1.exists())  # куски основной — как незапрошенные куски сегодня

    def test_symlink_not_followed(self):
        self.ear.unlink()
        target = self.dir / "precious.wav"
        target.write_bytes(b"RIFF")
        self.ear.symlink_to(target)
        second_ear.cleanup_after_delivery(self.meta(), SID, host_path=Path)
        self.assertTrue(target.exists())

    def test_both_cleanup_sites_call_ear_cleanup(self):
        # Сборщик удаляет основную запись в двух местах — второе ухо обязано быть в обоих.
        src = (_NOTARY / "collector.py").read_text(encoding="utf-8")
        fn = src.split("def _maybe_cleanup_wav_after_delivered", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_cleanup_second_ear(meta, sid)", fn)
        tail = src.split("# 3. WAV cleanup", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_cleanup_second_ear(meta_obj, session_uid)", tail)


class FinalizeWiringTest(unittest.TestCase):
    def test_owner_note_after_audio_choice(self):
        src = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        i_audio = src.index("resolve_wav_for_stt(meta, log=log)")
        i_note = src.index("notify_owner_if_ear(meta, session_uid=session_uid)")
        i_names = src.index("participants = filter_participant_names(")
        self.assertLess(i_audio, i_note)
        self.assertLess(i_note, i_names)


if __name__ == "__main__":
    unittest.main()
