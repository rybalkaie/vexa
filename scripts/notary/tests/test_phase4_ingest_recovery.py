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

Фаза 5 плана 2026-09-23-telemost-update-resilience (классы `TestReplyFinds*`,
`TestManualFinalizeLock`, `TestModeAndSeams`): правка ответом на восстановленный
протокол находит встречу — и в личке (путь А), и в группе (путь Б); ручная
финализация держит тот же замок, что сборщик и повторы; чужая расшифровка не
перезаписывается. finalize подменён двойником, который доставляет ровно так, как
боевой (`deliver_protocol(..., meta_json_path=<та же meta>)`) — Telegram и PDF
подменены на уровне `telegram_api`/`protocol_to_pdf`.

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


class TestSafeSeriesSegment(unittest.TestCase):
    """Серия — ОДИН безопасный сегмент пути (цикл5 У2): легальные slug'и проходят
    как есть, разделители/traversal нейтрализуются, пустой/`.`/`..` отвергаются."""

    def test_legit_slug_unchanged(self):
        for s in ("series-koord-8399ea", "проект-координация", "weekly_sync.v2"):
            self.assertEqual(ing.safe_series_segment(s), s, s)

    def test_whitespace_trimmed(self):
        self.assertEqual(ing.safe_series_segment("  koord  "), "koord")

    def test_separators_collapsed_to_single_segment(self):
        # Никаких '/' в результате → невозможно выйти за output_dir.
        for s in ("a/b", "проект/координация", "../etc", "a\\b"):
            out = ing.safe_series_segment(s)
            self.assertNotIn("/", out, s)
            self.assertNotIn("\\", out, s)

    def test_dotdot_literal_is_safe_segment(self):
        # 'x..y' — буквальное имя, не parent-ссылка; остаётся как есть.
        self.assertEqual(ing.safe_series_segment("x..y"), "x..y")

    def test_traversal_and_empty_rejected(self):
        for bad in ("", "   ", ".", "..", "/", "//"):
            with self.assertRaises(ValueError, msg=bad):
                ing.safe_series_segment(bad)


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


# ---------------------------------------------------------------------------
# Фаза 5: правки ответом на восстановленный протокол + замок ручной финализации
# ---------------------------------------------------------------------------
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
from unittest import mock  # noqa: E402

from lib import feedback_worker  # noqa: E402
from lib import finalize_lock  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402

# Фиктивные чаты — не настоящие id (правило: id чатов в тесты не кладём).
OWNER = 111
GROUP = -100500
DATE = "2026-09-23"
SECRET = "СЕКРЕТ-РЕПЛИКА-про-зарплату-Иванова"
PROTOCOL = f"# Протокол встречи\n\n## Решения\n- {SECRET}\n"
TRANSCRIPT = f"[00:00:01] Иван Петров: {SECRET}\n"


def _load_module(name: str, rel: str):
    for stub in ("requests", "torch", "numpy", "pymorphy3"):
        try:
            __import__(stub)
        except ImportError:
            sys.modules[stub] = types.ModuleType(stub)
    spec = importlib.util.spec_from_file_location(name, str(_NOTARY / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_module("collector_ph5_recovery", "collector.py")
retry_failed = _load_module("retry_failed_ph5_recovery", "retry_failed.py")


def _fake_render(md_text, out_pdf, *, title, subtitle, **kwargs):
    p = Path(out_pdf)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n" + b"x" * 64)
    return p


class _RecoveryEnv(unittest.TestCase):
    """Песочница как на сервере: очередь записей, рабочая папка протоколов,
    архив, реестр серий, папка замков. Telegram, PDF и finalize — двойники."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ph5-recovery-"))
        self.T = self.tmp / "transcripts"
        self.W = self.tmp / "protocols-work"
        self.A = self.tmp / "protocols-archive"
        self.R = self.tmp / "registry"
        self.S = self.tmp / "state"
        for d in (self.T, self.W, self.A, self.R, self.S):
            d.mkdir(parents=True)
        (self.R / "watched.yaml").write_text(
            "watched:\n"
            "  - id: grp-series\n    series: grp-series\n    telegram_chat_id: %d\n"
            "  - id: dm-series\n    series: dm-series\n" % GROUP,
            encoding="utf-8",
        )
        self.rec = self.tmp / "rec.wav"
        self.rec.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake-recording")
        (self.tmp / "people.md").write_text("# люди песочницы\n", encoding="utf-8")
        env = {
            # Подпись PDF читает список людей — берём песочный, не настоящий.
            "MEETING_NOTARY_PEOPLE_PATH": str(self.tmp / "people.md"),
            "MEETING_NOTARY_TRANSCRIPTS_DIR": str(self.T),
            "TELEMOST_PROTOCOL_DIR": str(self.W),
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.W),
            "MEETING_NOTARY_ARCHIVE_DIR": str(self.A),
            "MEETING_NOTARY_DELIVERED_ROOTS": os.pathsep.join(map(str, (self.T, self.W, self.A))),
            "MEETING_NOTARY_REGISTRY_DIR": str(self.R),
            "MEETING_NOTARY_FINALIZE_LOCK_DIRS": str(self.S),
            "MEETING_NOTARY_LOG_DIR": str(self.tmp / "logs"),
            "TELEGRAM_NOTARIUS_BOT_TOKEN": "test-token",
            "TELEGRAM_NOTARIUS_CHAT_ID": str(OWNER),
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        for leak in ("ENABLE_PROTOCOL_DELIVERY", "MEETING_NOTARY_STATE_DIR", "ENABLE_PORTAL_ORDERS"):
            os.environ.pop(leak, None)
        self.sent: list = []
        self.finalize_calls: list = []
        self.next_mid = 700

        def _send_document(token, chat_id, path, *, caption=None, filename=None, **kw):
            self.next_mid += 1
            self.sent.append((chat_id, self.next_mid))
            return {"message_id": self.next_mid}

        self.patches = [
            mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render),
            mock.patch.object(lp.telegram_api, "send_document", side_effect=_send_document),
            mock.patch("lib.notify.push_via_notarius", return_value=True),
            mock.patch("lib.notify.push", return_value=True),
            mock.patch.object(collector, "STATE_DIR", self.S),
        ]
        for p in self.patches:
            p.start()
        self.sid = ing.safe_session_uid("x", DATE)

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- двойник finalize: пишет расшифровку/протокол и доставляет КАК БОЕВОЙ ---
    def fake_finalize(self, *, rc: int = 0, during=None, drop_meta: bool = False):
        def run(meta_path, output_dir, *, deliver=False):
            self.finalize_calls.append({"meta": str(meta_path), "deliver": deliver})
            if during is not None:
                during(meta_path)
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            series, date = meta["series"], meta["startTs"][:10]
            sdir = Path(output_dir) / series
            sdir.mkdir(parents=True, exist_ok=True)
            md = sdir / f"{date}.md"
            proto = sdir / f"{date}-protokol.md"
            md.write_text(TRANSCRIPT, encoding="utf-8")
            proto.write_text(PROTOCOL, encoding="utf-8")
            (sdir / f"{date}-memory.json").write_text("{}", encoding="utf-8")
            if drop_meta:
                Path(meta_path).unlink()  # как `_stash_into_failed` при сбое расшифровки
                return rc, None
            delivery = {"status": "disabled"}
            if deliver:
                # Ровно блок доставки finalize-meeting.py: пути в meta доставки и
                # `delivered` — в ИСХОДНУЮ meta (`_delivery_marker_meta_path`).
                dm = dict(meta)
                dm.update({"date": date, "sessionUid": meta["sessionUid"],
                           "transcript_path": str(md), "protocol_path": str(proto)})
                delivery = lp.deliver_protocol(
                    meeting_meta=dm, protocol_text=PROTOCOL,
                    meta_json_path=Path(meta_path), meeting_sid=meta["sessionUid"],
                )
            return rc, {"ok": rc == 0, "series": series, "protocol_path": str(md), "delivery": delivery}
        return run

    def run_main(self, *extra, finalize=None):
        argv = [str(self.rec), "--date", DATE, "--participants", "Иван Петров,Мария Сидорова", *extra]
        out = io.StringIO()
        # basicConfig внутри main навесил бы обработчик на корневой журнал до конца
        # всего набора — соседние пробы начали бы печатать чужие журналы.
        with mock.patch.object(ing, "run_finalize", side_effect=finalize or self.fake_finalize()), \
                mock.patch.object(ing.logging, "basicConfig"), \
                contextlib.redirect_stdout(out):
            rc = ing.main(argv)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip().startswith("{")]
        return rc, (json.loads(lines[-1]) if lines else None)

    def reissue_paths(self, found):
        from lib import feedback_reissue  # noqa: PLC0415

        state = {"series": found["series"], "date": found["date"], "chat_id": found["chat_id"]}
        return feedback_reissue._resolve_paths(state, Path(found["meta_path"]), found["meta"])


class TestReplyFindsPersonalRecovery(_RecoveryEnv):
    """Корень Б: путь А (лично владельцу) писал доставку без номера сообщения."""

    def test_reply_to_personal_protocol_finds_meeting(self):
        rc, out = self.run_main("--series", "dm-series", "--mode", "personal")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "personal")
        self.assertTrue(out["replies_enabled"], "итог обязан честно сказать, что правки ответом работают")
        self.assertEqual(out["delivery"]["chat_kind"], "owner")
        self.assertEqual([c for c, _ in self.sent], [OWNER], "протокол ушёл НЕ только владельцу")
        mid = self.sent[0][1]
        self.assertEqual(out["delivery"]["message_ids"], [mid])

        found = feedback_worker.find_delivered_protocol(OWNER, mid, use_cache=False)
        self.assertIsNotNone(found, "ответ правкой на восстановленный протокол не находит встречу")
        self.assertEqual((found["series"], found["date"]), ("dm-series", DATE))
        # Тот же номер сообщения в ДРУГОМ чате — не наша встреча (номера по-чатные).
        self.assertIsNone(feedback_worker.find_delivered_protocol(GROUP, mid, use_cache=False))
        # Перевыпуску по правке есть из чего собирать: расшифровка и протокол на месте.
        transcript, protocol = self.reissue_paths(found)
        self.assertTrue(transcript and transcript.is_file())
        self.assertTrue(protocol and protocol.is_file())
        self.assertIn("Диаризация не выверена", protocol.read_text(encoding="utf-8"))

    def test_personal_meta_is_outside_collector_queue(self):
        """meta пути А не лежит в очереди сборщика: иначе он «доставил» бы её в группу."""
        rc, out = self.run_main("--series", "dm-series")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(p.name for p in self.T.glob("*.meta.json")), [],
                         "сборщик увидел бы meta пути А как встречу в очереди")
        meta_path = Path(out["meta_path"])
        self.assertEqual(meta_path.parent, self.T / ing.PERSONAL_META_SUBDIR)
        self.assertIn(meta_path, list(feedback_worker._iter_meta_files(self.T, None)))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta.get("telegram_chat_id"), OWNER,
                         "повтор после сбоя расшифровки ушёл бы мимо владельца")

    def test_transcript_input_reply_also_works(self):
        txt = self.tmp / "t.txt"
        txt.write_text("Иван: раз\nМария: два\n", encoding="utf-8")
        self.rec = txt

        def _gen(*, transcript_text, series, date, participants, out_protocol):
            out_protocol = Path(out_protocol)
            out_protocol.parent.mkdir(parents=True, exist_ok=True)
            (out_protocol.parent / f"{date}.md").write_text(transcript_text, encoding="utf-8")
            out_protocol.write_text(PROTOCOL, encoding="utf-8")
            return out_protocol

        with mock.patch.object(ing, "generate_protocol_from_transcript", side_effect=_gen):
            rc, out = self.run_main("--series", "grp-series", "--mode", "auto")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["mode"], "personal", "текст без звука — только лично")
        found = feedback_worker.find_delivered_protocol(OWNER, self.sent[0][1], use_cache=False)
        self.assertIsNotNone(found)
        self.assertTrue(self.reissue_paths(found)[0].is_file())

    def test_no_owner_chat_falls_back_honestly(self):
        os.environ.pop("TELEGRAM_NOTARIUS_CHAT_ID", None)
        with mock.patch.object(ing, "deliver_personally",
                               return_value={"notice": True, "document": True}) as legacy:
            rc, out = self.run_main("--series", "dm-series")
        self.assertEqual(rc, 0, out)
        legacy.assert_called_once()
        self.assertFalse(out["replies_enabled"], "без номера сообщения правка ответом невозможна")
        self.assertEqual(self.sent, [])


class TestReplyFindsStandardRecovery(_RecoveryEnv):
    """Корень А: путь Б клал meta мимо папок, где слушатель ищет протоколы."""

    def test_reply_in_group_finds_meeting(self):
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"])
        self.assertEqual(self.finalize_calls[0]["deliver"], True, "путь Б обязан доставлять штатно")
        meta_path = Path(out["meta_path"])
        self.assertEqual(meta_path, self.T / f"{ing.safe_session_uid('grp-series', DATE)}.meta.json",
                         "meta пути Б — в очереди бота, рядом с записями")
        self.assertEqual([c for c, _ in self.sent], [GROUP], "протокол серии с группой — в группу")
        mid = self.sent[0][1]
        found = feedback_worker.find_delivered_protocol(GROUP, mid, use_cache=False)
        self.assertIsNotNone(found, "ответ правкой в группе на протокол пути Б не находит встречу")
        self.assertEqual(found["meta_path"], str(meta_path))
        transcript, protocol = self.reissue_paths(found)
        self.assertTrue(transcript.is_file() and protocol.is_file())
        self.assertTrue(out["replies_enabled"])
        self.assertEqual(out["delivery"]["chat_kind"], "group")

    def test_staged_wav_is_own_copy_cleaned_by_collector_policy(self):
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc, 0, out)
        meta = json.loads(Path(out["meta_path"]).read_text(encoding="utf-8"))
        wav = Path(meta["files"]["wav"])
        self.assertEqual(wav.parent, self.T)
        self.assertNotEqual(wav, self.rec, "файл человека не должен стать записью бота")
        self.assertFalse(meta["keepAudio"], "копию штатно уберёт сборщик после доставки")
        self.assertTrue(self.rec.exists(), "исходный файл человека тронут")

    def test_archive_gets_durable_copy(self):
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc, 0, out)
        for name in (f"{DATE}.md", f"{DATE}-protokol.md", f"{DATE}-memory.json"):
            self.assertTrue((self.A / "grp-series" / name).is_file(), name)

    def test_rerun_does_not_send_second_pdf(self):
        self.run_main("--series", "grp-series", "--mode", "standard")
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard", "--force")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.sent), 1, "повторный прогон прислал в группу второй протокол")
        self.assertEqual(out["delivery"]["status"], "skipped")

    def test_failed_conversion_keeps_previous_queue_copy(self):
        """Сорвалась конвертация видео → прежняя копия в очереди (по ней мог повторять
        сборщик) цела, недописанного файла нет, finalize не запускался."""
        sid = ing.safe_session_uid("grp-series", DATE)
        staged = self.T / f"{sid}.wav"
        staged.write_bytes(b"previous-run-copy")
        video = self.tmp / "call.webm"
        video.write_bytes(b"not-really-video")
        self.rec = video

        def _boom(src, out):
            Path(out).write_bytes(b"half")
            raise RuntimeError("ffmpeg rc=1")

        with mock.patch.object(ing, "extract_wav", side_effect=_boom):
            rc, _out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc, 1)
        self.assertEqual(staged.read_bytes(), b"previous-run-copy")
        self.assertEqual([p.name for p in self.T.iterdir() if "part" in p.name], [])
        self.assertEqual(self.finalize_calls, [])

    def test_input_already_in_queue_is_used_as_is(self):
        sid = ing.safe_session_uid("grp-series", DATE)
        staged = self.T / f"{sid}.wav"
        staged.write_bytes(b"RIFFxxxxWAVEqueue")
        self.rec = staged
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc, 0, out)
        self.assertEqual(staged.read_bytes(), b"RIFFxxxxWAVEqueue")

    def test_stt_failure_hands_meeting_to_retry_queue(self):
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard",
                                finalize=self.fake_finalize(rc=1, drop_meta=True))
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("очередь повторов", out["note"])
        self.assertEqual(self.sent, [])

    def test_texts_never_reach_logs_or_stdout(self):
        records: list = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _H(level=logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(h)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
            rc2, out2 = self.run_main("--series", "dm-series", "--mode", "personal")
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        self.assertEqual((rc, rc2), (0, 0))
        blob = "\n".join(records) + json.dumps([out, out2], ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(str(GROUP), json.dumps([out, out2]), "id чата в stdout (его пересказывает Альфред)")


class TestManualFinalizeLock(_RecoveryEnv):
    """Ручная финализация держит тот же `finalize-<sid>.lock`, что сборщик и повторы."""

    def test_collector_skips_meeting_under_manual_lock(self):
        sid = ing.safe_session_uid("grp-series", DATE)
        calls: list = []
        with mock.patch.object(collector, "_do_finalize_and_collect", side_effect=calls.append):
            with finalize_lock.hold(sid) as held:
                self.assertTrue(held)
                collector._finalize_and_collect(sid)  # тик сборщика посреди ручной финализации
                self.assertEqual(calls, [], "сборщик запустил вторую финализацию под ручным замком")
            collector._finalize_and_collect(sid)
        self.assertEqual(calls, [sid], "после ручной финализации сборщик снова работает")

    def test_retry_skips_meeting_under_manual_lock(self):
        sid = ing.safe_session_uid("grp-series", DATE)
        state_path = self.tmp / f"{sid}.retry-state.json"
        state = {"attempts": 0}
        state_path.write_text("{}", encoding="utf-8")
        with finalize_lock.hold(sid) as held, \
                mock.patch.object(retry_failed.subprocess, "run",
                                  side_effect=AssertionError("повтор запустил finalize под замком")):
            self.assertTrue(held)
            retry_failed._try_finalize(
                sid=sid, meta_path=self.tmp / "m.json", state_path=state_path, state=state,
                state_dir=self.S, finalizer=Path("/nonexistent"), python_bin=Path("/nonexistent"),
                env_file=self.tmp / "no.env", dry_run=False,
            )
        self.assertEqual(state["attempts"], 0, "попытка под чужим замком засчитана")

    def test_wrapper_holds_lock_for_whole_finalize(self):
        seen: list = []

        def during(meta_path):
            sid = json.loads(Path(meta_path).read_text(encoding="utf-8"))["sessionUid"]
            with collector._session_finalize_lock(sid) as got:
                seen.append(got)

        for mode, series in (("standard", "grp-series"), ("personal", "dm-series")):
            with self.subTest(mode=mode):
                seen.clear()
                rc, out = self.run_main("--series", series, "--mode", mode,
                                        finalize=self.fake_finalize(during=during))
                self.assertEqual(rc, 0, out)
                self.assertEqual(seen, [False], "сборщик взял замок посреди ручной финализации")

    def test_wrapper_refuses_when_meeting_is_being_finalized(self):
        sid = ing.safe_session_uid("grp-series", DATE)
        with collector._session_finalize_lock(sid) as held:
            self.assertTrue(held)
            rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc, ing.EXIT_BUSY)
        self.assertEqual(out["error"], "finalize_in_progress")
        self.assertEqual(self.finalize_calls, [], "вторая финализация всё-таки запущена")
        self.assertEqual(self.sent, [])

    def test_lock_names_and_dirs_match_collector_and_retry(self):
        sid = "recovery-проект-2026-09-23"
        self.assertEqual(collector._finalize_lock_path(sid).name, finalize_lock.lock_filename(sid))
        names = {p.name for p in finalize_lock.lock_paths(sid, dirs=[self.S])}
        self.assertIn(finalize_lock.lock_filename(sid), names)   # как у сборщика
        self.assertIn(f"finalize-{sid}.lock", names)              # как у повторов
        self.assertEqual(finalize_lock.RETRY_DEFAULT_STATE_DIR, retry_failed.DEFAULT_STATE_DIR)
        src = (_NOTARY / "collector.py").read_text(encoding="utf-8")
        self.assertIn('"MEETING_NOTARY_STATE_DIR") or "%s"' % finalize_lock.COLLECTOR_DEFAULT_STATE_DIR, src)
        os.environ.pop(finalize_lock.DIRS_ENV, None)
        os.environ["MEETING_NOTARY_STATE_DIR"] = str(self.S)
        dirs = finalize_lock.lock_dirs()
        self.assertIn(self.S, dirs)
        self.assertIn(Path(os.path.expanduser(finalize_lock.COLLECTOR_DEFAULT_STATE_DIR)), dirs,
                      "в ручной оболочке может не быть переменной юнита сборщика")


class TestModeAndSeams(_RecoveryEnv):
    def test_auto_mode_by_series_group(self):
        self.assertEqual(ing.resolve_mode("auto", kind="audio", series="grp-series"), "standard")
        self.assertEqual(ing.resolve_mode("auto", kind="video", series="dm-series"), "personal")
        self.assertEqual(ing.resolve_mode("auto", kind="audio", series="no-such"), "personal")
        self.assertEqual(ing.resolve_mode("auto", kind="transcript", series="grp-series"), "personal")
        with self.assertRaises(ValueError):
            ing.resolve_mode("standard", kind="transcript", series="grp-series")

    def test_default_queue_dir_is_bots_and_first_reply_root(self):
        """Шов: путь Б кладёт meta туда, где её ищет слушатель на бою."""
        self.assertEqual(ing.DEFAULT_TRANSCRIPTS_DIR, collector.VPS_TRANSCRIPTS)
        unit = (_NOTARY / "deploy" / "meeting-notary-gateway.service").read_text(encoding="utf-8")
        roots = next(ln for ln in unit.splitlines()
                     if ln.startswith("Environment=MEETING_NOTARY_DELIVERED_ROOTS="))
        self.assertEqual(roots.split("=", 2)[2].split(":")[0], ing.DEFAULT_TRANSCRIPTS_DIR)
        self.assertFalse(ing.PERSONAL_META_SUBDIR.startswith((".", "_")),
                         "такую подпапку поиск доставленных протоколов пропускает")

    def test_finalize_marks_delivery_in_the_meta_it_was_given(self):
        """Двойник finalize в этих пробах доставляет так же, как боевой."""
        src = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        self.assertIn("meta_json_path = _delivery_marker_meta_path(args.meta_json)", src)
        self.assertIn("meta_json_path=meta_json_path", src)
        self.assertIn("return Path(meta_json_arg)", src)

    def test_existing_transcript_is_not_overwritten(self):
        sdir = self.W / "dm-series"
        sdir.mkdir(parents=True)
        (sdir / f"{DATE}.md").write_text("расшифровка бота", encoding="utf-8")
        rc, out = self.run_main("--series", "dm-series")
        self.assertEqual(rc, ing.EXIT_INPUT)
        self.assertEqual(out["error"], "target_exists")
        self.assertEqual(self.finalize_calls, [])
        self.assertEqual((sdir / f"{DATE}.md").read_text(encoding="utf-8"), "расшифровка бота")
        rc, out = self.run_main("--series", "dm-series", "--force")
        self.assertEqual(rc, 0, out)

    def test_archive_copy_also_guarded(self):
        adir = self.A / "grp-series"
        adir.mkdir(parents=True)
        (adir / f"{DATE}-protokol.md").write_text("протокол из архива", encoding="utf-8")
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc, ing.EXIT_INPUT)
        self.assertEqual(self.sent, [])

    def test_run_finalize_env_per_path(self):
        seen: list = []

        def _run(cmd, env=None, **kw):
            seen.append(env)
            return types.SimpleNamespace(returncode=0, stdout='log\n{\n  "delivery": {"status": "sent"}\n}\n')

        with mock.patch.object(ing.subprocess, "run", side_effect=_run), \
                mock.patch.object(ing.sys, "stderr", io.StringIO()):
            rc_a, rep_a = ing.run_finalize("/m.json", "/out", deliver=False)
            rc_b, rep_b = ing.run_finalize("/m.json", "/out", deliver=True)
        self.assertEqual(seen[0].get("ENABLE_PROTOCOL_DELIVERY"), "0")
        self.assertNotIn("ENABLE_PROTOCOL_DELIVERY", seen[1])
        self.assertEqual(rep_b["delivery"]["status"], "sent")
        self.assertEqual((rc_a, rc_b), (0, 0))


if __name__ == "__main__":
    unittest.main()
