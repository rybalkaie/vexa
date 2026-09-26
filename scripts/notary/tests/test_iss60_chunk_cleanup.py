"""ISS-60, фаза 6б (копилка владельца №3, 26.09.2026): куски записи 2…N удаляются после доставки.

Длинная встреча с паузами бот пишет кусками `<дата>-<сессия>.chunk1…N.wav`, а в
`meta.files.wav` стоит только первый кусок с речью. Сборщик после доставки удалял
только его; остальные куски (полные голоса участников) и куски основной записи после
сбоя расшифровки не удалял никто. Решение владельца — удалять их так же, как первый:

  • там же и тогда же, что `files.wav`: не при удержании порталом, не при keep_audio,
    не до доставки, только на сервере;
  • после сбоя расшифровки куски остаются источником повтора и удаляются вместе с
    остальными файлами встречи при удачном повторе (finalize 5.1);
  • только `.wav`-куски ЭТОЙ встречи: та же папка, та же сессия, номер куска ≥ 1;
    расшифровки, протоколы и meta не трогаются (правило «транскрипты не удалять»).

Запуск:
    cd ~/Projects/meeting-notary/vexa-resilience/scripts/notary
    python3 -m unittest tests.test_iss60_chunk_cleanup -v
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import second_ear, wav_concat  # noqa: E402

SR = 16000
UID = "tm-1727250000000"
SID = f"2026-09-25-{UID}"          # имя meta у сборщика: <дата>-<сессия>
OTHER = "2026-09-25-tm-1727259999999"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, str(_NOTARY / file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load("collector_chunks_6b", "collector.py")


def _load_finalize():
    """`finalize-meeting.py` с заглушками тяжёлых пакетов (как в test_second_ear_iss60)."""
    for name in ("requests", "torch", "torchaudio", "pyannote",
                 "speechmatics", "anthropic", "numpy", "soundfile"):
        sys.modules.setdefault(name, types.ModuleType(name))
    return _load("finalize_meeting_chunks_6b", "finalize-meeting.py")


def _wav(path: Path, samples: int, value: int = 1000) -> Path:
    pcm = (value.to_bytes(2, "little", signed=True)) * samples
    path.write_bytes(wav_concat.build_wav_header(len(pcm), SR) + pcm)
    return path


class _Meeting(unittest.TestCase):
    """Папка записей бота с длинной встречей из N кусков и соседями, которых трогать нельзя."""

    N = 4

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="chunks6b-"))
        self.rec = self.tmp / "transcripts"
        self.rec.mkdir()
        self.chunks = [_wav(self.rec / f"{SID}.chunk{i}.wav", SR * (i + 1), value=100 * i)
                       for i in range(1, self.N + 1)]
        self.ear = _wav(self.rec / f"{SID}.chunk0.wav", SR * 2, value=7)
        # Соседи: расшифровка-черновик, meta, протокол, чужая встреча, наш маркер.
        self.txt = self.rec / f"{SID}.txt"
        self.txt.write_text("черновик расшифровки", encoding="utf-8")
        self.meta_path = self.rec / f"{SID}.meta.json"
        self.md = self.rec / f"{SID}.md"
        self.md.write_text("расшифровка", encoding="utf-8")
        self.other = _wav(self.rec / f"{OTHER}.chunk2.wav", SR)
        self.keep = [self.txt, self.md, self.other]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def meta(self, *, files_wav: Path | None = None, speech=None, **extra) -> dict:
        speech = set(speech if speech is not None else range(1, self.N + 1))
        chunks = [{"idx": i, "wav": str(p),
                   "firstSpeechMs": (1_000 * i if i in speech else None),
                   "lastSpeechMs": (1_000 * i + 500 if i in speech else None)}
                  for i, p in enumerate(self.chunks, start=1)]
        first = files_wav or next(p for i, p in enumerate(self.chunks, start=1) if i in speech)
        meta = {"sessionUid": UID, "series": "koordinatsiya", "startTs": "2026-09-25T09:00:00Z",
                "recording": {"chunks": chunks, "audioSource": "primary",
                              "secondEar": {"wav": str(self.ear), "status": "stopped"}},
                "files": {"wav": str(first), "draftTxt": str(self.txt), "meta": str(self.meta_path)}}
        meta.update(extra)
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta

    def meeting_chunks(self) -> list[str]:
        return sorted(p.name for p in self.rec.glob(f"{SID}.chunk*.wav"))

    def assert_neighbours_kept(self) -> None:
        for p in self.keep + [self.meta_path]:
            self.assertTrue(p.exists(), f"тронут чужой/не звуковой файл: {p.name}")


class CollectorChunksTest(_Meeting):
    """Сборщик: после доставки на диске нет ни одного куска этой встречи."""

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

    def tearDown(self) -> None:
        for p in reversed(self._p):
            p.stop()
        super().tearDown()

    def test_long_meeting_all_chunks_gone_after_delivery(self):
        collector._maybe_cleanup_wav_after_delivered(self.meta(), SID)
        self.assertEqual(self.meeting_chunks(), [], "куски встречи 2…N остались после доставки")
        self.assert_neighbours_kept()

    def test_keep_audio_keeps_every_chunk(self):
        before = self.meeting_chunks()
        collector._maybe_cleanup_wav_after_delivered(self.meta(keep_audio=True), SID)
        collector._maybe_cleanup_wav_after_delivered(self.meta(keepAudio=True), SID)
        self.assertEqual(self.meeting_chunks(), before)

    def test_portal_hold_keeps_every_chunk(self):
        self.held = True
        before = self.meeting_chunks()
        collector._maybe_cleanup_wav_after_delivered(self.meta(), SID)
        self.assertEqual(self.meeting_chunks(), before)

    def test_never_from_mac(self):
        before = self.meeting_chunks()
        with mock.patch.object(collector, "LOCAL_FINALIZE", False):
            collector._maybe_cleanup_wav_after_delivered(self.meta(), SID)
        self.assertEqual(self.meeting_chunks(), before)

    def test_rest_removed_even_if_first_chunk_gone_last_tick(self):
        meta = self.meta()
        self.chunks[0].unlink()
        collector._maybe_cleanup_wav_after_delivered(meta, SID)
        self.assertEqual(self.meeting_chunks(), [])

    def test_files_wav_is_second_chunk_first_silent_chunk_removed_too(self):
        # chunk1 без речи → files.wav = chunk2; chunk1 — тоже «остальной» кусок.
        collector._maybe_cleanup_wav_after_delivered(self.meta(speech={2, 3, 4}), SID)
        self.assertEqual(self.meeting_chunks(), [])

    def test_post_finalize_site_removes_all_but_files_wav(self):
        # Вторая точка уборки («# 3. WAV cleanup»): files.wav удаляет основной код рядом.
        collector._cleanup_extra_wavs(self.meta(), SID)
        self.assertEqual(self.meeting_chunks(), [self.chunks[0].name])
        self.assert_neighbours_kept()

    def test_meta_of_another_meeting_deletes_nothing(self):
        # Имя meta (sid сборщика) — другой встречи: чужая meta не даёт удалить ничего.
        before = self.meeting_chunks()
        collector._cleanup_extra_wavs(self.meta(), OTHER)
        self.assertEqual(self.meeting_chunks(), before)
        self.assertTrue(self.other.exists())


class StrictPathsTest(_Meeting):
    """Удаление идёт по путям из meta — принимаются только куски этой встречи."""

    def test_only_meeting_chunks_listed(self):
        got = second_ear.extra_recording_wavs(self.meta(), SID)
        self.assertEqual(sorted(Path(p).name for p in got),
                         sorted([self.ear.name] + [p.name for p in self.chunks[1:]]))

    def test_foreign_paths_rejected(self):
        meta = self.meta()
        sub = self.rec / "x"
        sub.mkdir()
        precious = self.tmp / "precious.wav"
        precious.write_bytes(b"RIFF")
        link = self.rec / f"{SID}.chunk9.wav"
        link.symlink_to(precious)
        bad = [
            str(self.other),                                   # другая встреча в той же папке
            str(sub / f"{SID}.chunk5.wav"),                    # та же встреча, другая папка
            f"{self.rec}/../{self.rec.name}/{SID}.chunk6.wav",  # `..`
            str(self.rec / f"{SID}.chunk7.txt"),               # не звук
            str(self.rec / f"{SID}.chunk07.wav"),              # номер с нулём впереди
            str(self.rec / f"{UID}.chunk8.wav"),               # без даты — не имя бота
            "/etc/passwd",
        ]
        for i, p in enumerate(bad):
            meta["recording"]["chunks"].append({"idx": 10 + i, "wav": p})
        meta["recording"]["chunks"].append({"idx": 9, "wav": str(link)})
        (sub / f"{SID}.chunk5.wav").write_bytes(b"RIFF")
        listed = second_ear.extra_recording_wavs(meta, SID)
        for p in bad:
            self.assertNotIn(p, listed, p)
        with mock.patch.object(collector, "LOCAL_FINALIZE", True):
            collector._cleanup_extra_wavs(meta, SID)
        self.assertTrue(precious.exists(), "удаление прошло по ссылке")
        self.assertTrue(link.is_symlink())
        self.assertTrue((sub / f"{SID}.chunk5.wav").exists())
        self.assertTrue(self.other.exists())

    def test_session_forms(self):
        # sid сборщика — `<дата>-<сессия>`, финализации — sessionUid: обе формы — эта встреча.
        meta = self.meta()
        self.assertTrue(second_ear.primary_chunk_wavs(meta, SID))
        self.assertTrue(second_ear.primary_chunk_wavs(meta, UID))
        for wrong in (OTHER, "tm-1", "", None, "../" + UID, "2026-09-25"):
            self.assertEqual(second_ear.primary_chunk_wavs(meta, wrong), [], wrong)

    def test_old_image_without_ear(self):
        meta = self.meta()
        meta["recording"].pop("secondEar")
        self.assertEqual(sorted(Path(p).name for p in second_ear.extra_recording_wavs(meta, SID)),
                         [p.name for p in self.chunks[1:]])


class RetryChunksTest(_Meeting):
    """Сбой расшифровки → куски — источник повтора; удачный повтор (5.1) их удаляет."""

    def setUp(self) -> None:
        super().setUp()
        self.failed = self.tmp / "failed"
        self.failed.mkdir()
        self._env = mock.patch.dict(os.environ, {"MEETING_NOTARY_FAILED_DIR": str(self.failed)})
        self._env.start()
        self.finalize = _load_finalize()
        self.log = logging.getLogger("test-chunks-6b")

    def tearDown(self) -> None:
        self._env.stop()
        super().tearDown()

    def stash(self, meta: dict) -> dict:
        audio, _temp = wav_concat.resolve_wav_for_stt(meta, tmp_dir=self.tmp)
        self.finalize._stash_into_failed(UID, str(audio), str(self.meta_path),
                                         rejected=False, err_repr="SpeechmaticsError: down")
        return json.loads((self.failed / f"{UID}.meta.json").read_text(encoding="utf-8"))

    def test_chunks_stay_as_retry_source_then_go_after_success(self):
        failed_meta = self.stash(self.meta())
        # Склейка и второе ухо — в _failed/, сами куски — на месте: из них склеивает повтор.
        self.assertTrue((self.failed / f"{UID}.wav").exists())
        self.assertTrue((self.failed / f"{UID}.chunk0.wav").exists())
        self.assertEqual(self.meeting_chunks(), [p.name for p in self.chunks])
        path, temp = wav_concat.resolve_wav_for_stt(failed_meta, tmp_dir=self.tmp)
        pcm, _sr = wav_concat.read_pcm(path)
        self.assertEqual(len(pcm) // 2, SR * sum(range(2, self.N + 2)), "повтор склеил не всю встречу")
        if temp:
            Path(path).unlink()
        removed = self.finalize._remove_primary_chunks_after_retry(failed_meta, UID, self.log, hold=False)
        self.assertEqual(len(removed), self.N)
        self.assertEqual(self.meeting_chunks(), [])
        self.assertTrue(self.txt.exists() and self.md.exists() and self.other.exists())
        # _failed/ этот шаг не трогает — его чистит уборка `<sid>.*` рядом.
        self.assertTrue((self.failed / f"{UID}.wav").exists())

    def test_portal_hold_keeps_chunks(self):
        failed_meta = self.stash(self.meta())
        self.assertEqual(self.finalize._remove_primary_chunks_after_retry(failed_meta, UID, self.log, hold=True), [])
        self.assertEqual(self.meeting_chunks(), [p.name for p in self.chunks])

    def test_protocol_from_ear_rest_of_primary_goes_after_retry(self):
        prim = [{"idx": i, "wav": str(p), "firstSpeechMs": (1_000 if i == 2 else None),
                 "lastSpeechMs": (2_000 if i == 2 else None)} for i, p in enumerate(self.chunks, start=1)]
        meta = self.meta()
        meta["recording"].update({"audioSource": "second_ear", "primaryChunks": prim,
                                  "chunks": [{"idx": 0, "wav": str(self.ear)}]})
        meta["files"]["wav"] = str(self.ear)
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        failed_meta = self.stash(meta)
        # Ухо — исходник повтора, первый кусок основной с речью — спутник в _failed/.
        self.assertTrue((self.failed / f"{UID}.wav").exists())
        self.assertTrue((self.failed / f"{UID}.chunk2.wav").exists())
        self.assertEqual(self.meeting_chunks(), sorted(p.name for i, p in enumerate(self.chunks, start=1) if i != 2))
        self.finalize._remove_primary_chunks_after_retry(failed_meta, UID, self.log, hold=False)
        self.assertEqual(self.meeting_chunks(), [])

    def test_cleanup_failure_never_raises(self):
        with mock.patch.object(second_ear, "remove_wavs", side_effect=RuntimeError("boom")):
            self.assertEqual(
                self.finalize._remove_primary_chunks_after_retry(self.meta(), UID, self.log, hold=False), [])

    def test_retry_success_block_wires_the_cleanup_under_portal_gate(self):
        src = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        block = src.split("# 5.1. Если этот sid был в _failed/", 1)[1].split("# 6. Краткий результат", 1)[0]
        i_glob = block.index("for f in sid_files:")
        i_call = block.index("_remove_primary_chunks_after_retry(meta, session_uid, log, hold=_hold_sources)")
        self.assertLess(block.index("_hold_sources = _keep_sources_until_portal_ack"), i_call)
        self.assertLess(i_glob, i_call)


if __name__ == "__main__":
    unittest.main()
