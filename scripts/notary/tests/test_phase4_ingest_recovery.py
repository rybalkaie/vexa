"""Тесты Ф4 (план bot-eviction-recovery-resilience) R7 / R9 / R11:
обёртка восстановления протокола из ручной записи `tools/ingest-recording.py`.

Закрепляют ЧИСТУЮ логику обёртки БЕЗ ffmpeg/STT/LLM/Telegram:
  • R7  — синтетическая meta НЕ несёт failure-причину (иначе гейт Ф3 подавил бы
          восстановленный протокол) + пометка «диаризация не выверена».
  • R9  — развилка транскрипта: метки спикеров есть → роли; меток нет → честная
          пометка «без разбивки» + подсказка (имена не выдумываем).
  • R11 — метаданные транскрипта НЕ содержат сырого текста реплик.

Почему важно: регрессия здесь = либо восстановленный протокол молча теряется
(failure-метка в meta), либо боту приписывают выдуманные роли (ложный детект
меток), либо в логи течёт текст встречи (опасная тройка). Тест ловит всё это
без живого пайплайна. См. plans/2026-06-30-bot-eviction-recovery-resilience.md.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase4_ingest_recovery -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


def _load_ingest():
    """Грузим модуль с дефисом в имени (import как обычно нельзя)."""
    path = _NOTARY / "tools" / "ingest-recording.py"
    spec = importlib.util.spec_from_file_location("ingest_recording", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ingest_recording"] = mod
    spec.loader.exec_module(mod)
    return mod


ing = _load_ingest()

# Набор failure-причин гейта доставки Ф3 (lib/delivery_gate.py) — синтетическая
# meta НЕ должна нести ни одну (кросс-фазный контракт).
FAILURE_REASONS = {"no_one_joined", "deaf_with_participants", "evicted_failure", "deaf_after_start"}


class TestDetectInputKind(unittest.TestCase):
    def test_video_by_ext(self):
        for f in ("a.mp4", "b.MKV", "c.mov", "d.webm", "e.avi"):
            self.assertEqual(ing.detect_input_kind(f), "video", f)

    def test_audio_by_ext(self):
        for f in ("a.wav", "b.MP3", "c.m4a", "d.ogg", "e.opus", "f.flac"):
            self.assertEqual(ing.detect_input_kind(f), "audio", f)

    def test_transcript_by_ext(self):
        for f in ("a.txt", "b.md", "c.vtt", "d.srt"):
            self.assertEqual(ing.detect_input_kind(f), "transcript", f)

    def test_forced_overrides_extension(self):
        # .txt, но --kind transcript → transcript; форс video на .bin → video.
        self.assertEqual(ing.detect_input_kind("x.bin", "video"), "video")
        self.assertEqual(ing.detect_input_kind("x.mp4", "transcript"), "transcript")

    def test_unknown_extension_raises(self):
        with self.assertRaises(ValueError):
            ing.detect_input_kind("mystery.xyz")

    def test_auto_passthrough(self):
        self.assertEqual(ing.detect_input_kind("a.mp4", "auto"), "video")


class TestValidateDate(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(ing.validate_date("2026-06-30"), "2026-06-30")

    def test_bad_format(self):
        for bad in ("2026/06/30", "30-06-2026", "2026-6-3", "июнь", ""):
            with self.assertRaises(ValueError, msg=bad):
                ing.validate_date(bad)

    def test_impossible_date(self):
        with self.assertRaises(ValueError):
            ing.validate_date("2026-13-40")


class TestSafeSessionUid(unittest.TestCase):
    def test_prefix_and_components(self):
        sid = ing.safe_session_uid("series-koord-8399ea", "2026-06-30")
        self.assertTrue(sid.startswith("recovery-"))
        self.assertIn("2026-06-30", sid)

    def test_no_path_separators(self):
        sid = ing.safe_session_uid("a/b c\\d", "2026-06-30")
        self.assertNotIn("/", sid)
        self.assertNotIn("\\", sid)
        self.assertNotIn(" ", sid)


class TestSyntheticMeta(unittest.TestCase):
    def _meta(self):
        return ing.build_synthetic_meta(
            series="series-x", date="2026-06-30", wav_path="/tmp/r.wav",
            participants=["Иван Петров", "Мария Сидорова"], sid="recovery-series-x-2026-06-30",
        )

    def test_required_fields_present(self):
        m = self._meta()
        # finalize-meeting.py main() требует sessionUid + files.wav.
        self.assertTrue(m.get("sessionUid"))
        self.assertTrue((m.get("files") or {}).get("wav"))
        self.assertEqual(m["series"], "series-x")
        self.assertTrue(m["startTs"].startswith("2026-06-30"))
        self.assertEqual(m["language"], "ru")
        self.assertEqual(m["expectedParticipants"], ["Иван Петров", "Мария Сидорова"])

    def test_no_failure_end_reason(self):
        # КОНТРАКТ Ф3: failure-причина подавила бы восстановленный протокол.
        m = self._meta()
        self.assertNotIn(m.get("endReason"), FAILURE_REASONS)
        self.assertEqual(m.get("endReason"), "manual_recovery")
        self.assertTrue(m.get("recovery"))

    def test_gate_does_not_suppress_recovered_protocol(self):
        """Сквозная проверка кросс-фазного контракта: гейт доставки Ф3 на нашей
        синтетической meta + существенной записи НЕ подавляет (доставка возможна)."""
        from lib import delivery_gate
        m = self._meta()
        verdict = delivery_gate.evaluate_delivery(
            end_reason=str(m.get("endReason")), turn_count=12, utter_count=40,
        )
        self.assertFalse(verdict.suppressed, "восстановленный протокол не должен подавляться гейтом")

    def test_gate_suppresses_if_failure_reason_leaked(self):
        """Негативный контроль: если бы meta несла failure-причину — гейт подавил бы.
        Доказывает, что выбор нейтральной причины осмыслен, а не случаен."""
        from lib import delivery_gate
        verdict = delivery_gate.evaluate_delivery(
            end_reason="evicted_failure", turn_count=1, utter_count=1,
        )
        self.assertTrue(verdict.suppressed)


class TestSpeakerLabelDetection(unittest.TestCase):
    def test_explicit_speaker_tags(self):
        t = "Спикер 1: привет всем\nСпикер 2: добрый день\nСпикер 1: начнём"
        self.assertTrue(ing.has_speaker_labels(t))

    def test_named_dialogue(self):
        t = "Иван: открываю встречу\nМария: я по поставкам\nИван: спасибо"
        self.assertTrue(ing.has_speaker_labels(t))

    def test_named_with_timestamps_and_markers(self):
        t = "[00:01] Иван Петров: бюджет согласован\n- Мария Сидорова: уточню по складу"
        self.assertTrue(ing.has_speaker_labels(t))

    def test_speaker_tags_with_timestamps(self):
        t = "(00:12) Speaker 1: ok\n(01:03) Speaker 2: agreed\n(02:00) Speaker 1: next"
        self.assertTrue(ing.has_speaker_labels(t))

    def test_plain_prose_no_labels(self):
        t = ("Сегодня обсудили план поставок на третий квартал и согласовали "
             "бюджет.\nРешили перенести запуск на июль.\nОтдельно отметили риски "
             "по логистике и договорились вернуться к ним позже.")
        self.assertFalse(ing.has_speaker_labels(t))

    def test_single_stray_colon_not_labels(self):
        # Одна «Итого:» в прозе не делает текст размеченным.
        t = ("Обсудили выручку за квартал и динамику воронки продаж.\n"
             "Итого: план выполнен на 80 процентов.\n"
             "Дальше работаем над удержанием клиентов и сервисом.")
        self.assertFalse(ing.has_speaker_labels(t))

    def test_empty(self):
        self.assertFalse(ing.has_speaker_labels(""))
        self.assertFalse(ing.has_speaker_labels("\n\n  \n"))


class TestTranscriptStatsLogDiscipline(unittest.TestCase):
    """R11: метаданные транскрипта НЕ содержат сырого текста реплик."""

    SECRET = "СЕКРЕТ-РЕПЛИКА-про-увольнение-Иванова-и-зарплату"

    def test_stats_keys_are_metadata_only(self):
        t = f"Иван: {self.SECRET}\nМария: тоже секрет про финансы"
        stats = ing.transcript_meta_stats(t)
        # Все значения — числа/булевы (никакого текста).
        for k, v in stats.items():
            self.assertIn(type(v), (int, bool), f"{k}={v!r} должен быть метаданным")
        # Среди значений нет подстроки секрета.
        joined = " ".join(str(v) for v in stats.values())
        self.assertNotIn("СЕКРЕТ", joined)
        self.assertNotIn("Иван", joined)

    def test_speaker_label_stats_no_text(self):
        t = f"Спикер 1: {self.SECRET}\nСпикер 2: ответ"
        st = ing.speaker_label_stats(t)
        joined = " ".join(str(v) for v in st.values())
        self.assertNotIn("СЕКРЕТ", joined)
        self.assertGreaterEqual(st["distinct_labels"], 2)

    def test_stats_counts_sane(self):
        t = "Иван: раз\nМария: два\nИван: три"
        stats = ing.transcript_meta_stats(t)
        self.assertEqual(stats["nonempty_lines"], 3)
        self.assertEqual(stats["distinct_labels"], 2)
        self.assertTrue(stats["has_labels"])
        self.assertEqual(stats["chars"], len(t))


class TestBannerInjection(unittest.TestCase):
    def test_after_heading(self):
        proto = "# Протокол встречи\n\n## Решения\n- сделать X"
        out = ing.inject_banner(proto, ing.DIARIZATION_BANNER)
        lines = out.splitlines()
        self.assertEqual(lines[0], "# Протокол встречи")
        self.assertIn(ing.DIARIZATION_BANNER.splitlines()[0], out)
        # Баннер идёт раньше тела «## Решения».
        self.assertLess(out.index("сверьте роли".lower()) if False else out.index("Диаризация"),
                        out.index("## Решения"))

    def test_no_heading_prepended(self):
        proto = "Решения:\n- сделать X"
        out = ing.inject_banner(proto, ing.NO_SPEAKERS_BANNER)
        self.assertTrue(out.lstrip().startswith(">"))
        self.assertIn("без разбивки", out.lower())

    def test_idempotent(self):
        proto = "# П\n\nтело"
        once = ing.inject_banner(proto, ing.DIARIZATION_BANNER)
        twice = ing.inject_banner(once, ing.DIARIZATION_BANNER)
        self.assertEqual(once, twice)
        self.assertEqual(once.count(ing.DIARIZATION_BANNER.splitlines()[0]), 1)

    def test_r7_exact_phrase(self):
        # R7: дословная пометка «диаризация не выверена — сверьте роли».
        self.assertIn("диаризация не выверена", ing.DIARIZATION_BANNER.lower())
        self.assertIn("сверьте роли", ing.DIARIZATION_BANNER.lower())

    def test_r9_no_speakers_phrase_and_hint(self):
        self.assertIn("без разбивки по спикерам", ing.NO_SPEAKERS_BANNER.lower())
        self.assertIn("видео", ing.NO_SPEAKERS_BANNER.lower())


class TestProtocolPathFor(unittest.TestCase):
    def test_layout_matches_finalize(self):
        p = ing.protocol_path_for("/out", "series-x", "2026-06-30")
        self.assertEqual(p, Path("/out/series-x/2026-06-30-protokol.md"))


if __name__ == "__main__":
    unittest.main()
