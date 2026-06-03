"""Тест multichunk-склейки + починки WAV для финализации (Ф2, plan bot-notarius-full).

Закрепляет корневой P0-фикс: finalize должен собрать ВСЮ речь из
`meta.recording.chunks[]` и НЕ падать «WAV not found», даже если:
  • заголовок WAV битый (placeholder data_size=0 — бот убит до close());
  • chunk_1 пустой (тишина/только заголовок), а речь — в chunk_2.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_wav_concat -v
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import wav_concat  # noqa: E402

SR = 16000


def _pcm(seconds: float, value: int = 1000) -> bytes:
    """seconds аудио 16k mono 16-bit, заполненное константой (не тишина)."""
    n = int(SR * seconds)
    return struct.pack("<%dh" % n, *([value] * n))


def _write_valid_wav(path: Path, pcm: bytes) -> None:
    path.write_bytes(wav_concat.build_wav_header(len(pcm), SR) + pcm)


def _write_broken_wav(path: Path, pcm: bytes) -> None:
    """Валидный заголовок, но data_size=0 и RIFF=36 (placeholder из open()) —
    PCM дописан после. Воспроизводит WAV, на котором бот умер до close().
    """
    header = wav_concat.build_wav_header(0, SR)  # data_size=0, RIFF=36
    assert struct.unpack_from("<I", header, 40)[0] == 0  # placeholder
    path.write_bytes(header + pcm)


def _header_only_wav(path: Path) -> None:
    path.write_bytes(wav_concat.build_wav_header(0, SR))  # 44 байта, нет PCM


class ReadPcmRepairTest(unittest.TestCase):
    def test_repairs_placeholder_header(self):
        """PCM читается по реальному размеру файла, игнорируя битый data_size."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.wav"
            pcm = _pcm(2.0)
            _write_broken_wav(p, pcm)
            got, sr = wav_concat.read_pcm(p)
            self.assertEqual(sr, SR)
            self.assertEqual(len(got), len(pcm), "должен вернуть ВЕСЬ PCM, не 0")

    def test_valid_wav_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ok.wav"
            pcm = _pcm(1.0)
            _write_valid_wav(p, pcm)
            got, _ = wav_concat.read_pcm(p)
            self.assertEqual(got, pcm)

    def test_odd_byte_tail_trimmed(self):
        """Оборванный последний writeSync (нечётный хвост) выравнивается."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "odd.wav"
            _write_broken_wav(p, _pcm(0.5) + b"\x01")  # +1 байт
            got, _ = wav_concat.read_pcm(p)
            self.assertEqual(len(got) % 2, 0)


class HeaderValidityTest(unittest.TestCase):
    def test_valid_header_recognized(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ok.wav"; _write_valid_wav(p, _pcm(1.0))
            self.assertTrue(wav_concat._header_is_valid(p))

    def test_placeholder_header_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.wav"; _write_broken_wav(p, _pcm(1.0))
            self.assertFalse(wav_concat._header_is_valid(p))

    def test_header_only_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.wav"; _header_only_wav(p)
            self.assertFalse(wav_concat._header_is_valid(p))  # declared=0


class ResolveMultichunkTest(unittest.TestCase):
    def _meta(self, d, chunk_files, files_wav=None):
        chunks = [
            {"idx": i + 1, "wav": str(p), "firstSpeechMs": None, "lastSpeechMs": None}
            for i, p in enumerate(chunk_files)
        ]
        return {
            "sessionUid": "tm-test",
            "recording": {"chunks": chunks},
            "files": {"wav": files_wav if files_wav else (str(chunk_files[0]) if chunk_files else None)},
        }

    def test_concat_all_chunks(self):
        """3 куска (один с битой шапкой) → собран весь PCM, всё в один temp."""
        with tempfile.TemporaryDirectory() as d:
            c1 = Path(d) / "s.chunk1.wav"; _write_valid_wav(c1, _pcm(1.0))
            c2 = Path(d) / "s.chunk2.wav"; _write_broken_wav(c2, _pcm(2.0))  # killed before close
            c3 = Path(d) / "s.chunk3.wav"; _write_valid_wav(c3, _pcm(0.5))
            meta = self._meta(d, [c1, c2, c3])
            path, is_temp = wav_concat.resolve_wav_for_stt(meta, tmp_dir=d)
            self.assertTrue(is_temp)
            self.assertTrue(os.path.exists(path))
            pcm, sr = wav_concat.read_pcm(path)
            expected = len(_pcm(1.0)) + len(_pcm(2.0)) + len(_pcm(0.5))
            self.assertEqual(len(pcm), expected, "должна склеиться вся речь со всех кусков")
            # И сам temp — валидный WAV (правильный data_size в шапке).
            head = Path(path).read_bytes()[:44]
            self.assertEqual(struct.unpack_from("<I", head, 40)[0], expected)

    def test_empty_chunk1_speech_in_chunk2(self):
        """chunk_1 — только заголовок (тишина/фора), речь в chunk_2: НЕ падаем,
        собираем речь chunk_2 (корневой кейс «WAV not found при пустом chunk_1»).
        """
        with tempfile.TemporaryDirectory() as d:
            c1 = Path(d) / "s.chunk1.wav"; _header_only_wav(c1)  # size==44 → отфильтруется
            c2 = Path(d) / "s.chunk2.wav"; _write_broken_wav(c2, _pcm(3.0))
            meta = self._meta(d, [c1, c2])
            path, is_temp = wav_concat.resolve_wav_for_stt(meta, tmp_dir=d)
            self.assertIsNotNone(path, "не должно быть None — речь есть в chunk_2")
            pcm, _ = wav_concat.read_pcm(path)
            self.assertEqual(len(pcm), len(_pcm(3.0)))

    def test_single_valid_wav_used_inplace(self):
        """Один кусок с валидной шапкой → отдаём как есть, без temp-копии."""
        with tempfile.TemporaryDirectory() as d:
            c1 = Path(d) / "s.chunk1.wav"; _write_valid_wav(c1, _pcm(1.0))
            meta = self._meta(d, [c1])
            path, is_temp = wav_concat.resolve_wav_for_stt(meta, tmp_dir=d)
            self.assertFalse(is_temp, "валидный одиночный WAV не нужно перезаписывать")
            self.assertEqual(Path(path).resolve(), c1.resolve())

    def test_single_broken_wav_repaired(self):
        """Один кусок с битой шапкой → починенный temp."""
        with tempfile.TemporaryDirectory() as d:
            c1 = Path(d) / "s.chunk1.wav"; _write_broken_wav(c1, _pcm(2.0))
            meta = self._meta(d, [c1])
            path, is_temp = wav_concat.resolve_wav_for_stt(meta, tmp_dir=d)
            self.assertTrue(is_temp)
            head = Path(path).read_bytes()[:44]
            self.assertEqual(struct.unpack_from("<I", head, 40)[0], len(_pcm(2.0)))

    def test_legacy_single_files_wav_no_chunks(self):
        """Старый формат meta без recording.chunks[] → используем files.wav."""
        with tempfile.TemporaryDirectory() as d:
            w = Path(d) / "legacy.wav"; _write_valid_wav(w, _pcm(1.0))
            meta = {"sessionUid": "tm-legacy", "files": {"wav": str(w)}, "recording": {}}
            path, is_temp = wav_concat.resolve_wav_for_stt(meta, tmp_dir=d)
            self.assertFalse(is_temp)
            self.assertEqual(Path(path).resolve(), w.resolve())

    def test_nothing_on_disk_returns_none(self):
        """Ни chunks, ни files.wav на диске нет → (None, False) (rc=3/rc=10 выше)."""
        with tempfile.TemporaryDirectory() as d:
            meta = {
                "sessionUid": "tm-gone",
                "recording": {"chunks": [{"idx": 1, "wav": str(Path(d) / "missing.chunk1.wav")}]},
                "files": {"wav": str(Path(d) / "missing.wav")},
            }
            path, is_temp = wav_concat.resolve_wav_for_stt(meta, tmp_dir=d)
            self.assertIsNone(path)
            self.assertFalse(is_temp)


if __name__ == "__main__":
    unittest.main()
