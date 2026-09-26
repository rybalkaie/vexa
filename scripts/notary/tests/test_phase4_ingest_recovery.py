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

from lib import delivery_gate  # noqa: E402
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
            # Очередь повторов — песочная: на сервере `/srv/.../_failed` живая.
            "MEETING_NOTARY_FAILED_DIR": str(self.tmp / "failed"),
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

        #: Текст, из которого собран каждый отправленный PDF (что увидели люди).
        self.rendered: list = []

        def _render(md_text, out_pdf, **kw):
            self.rendered.append(md_text)
            return _fake_render(md_text, out_pdf, **kw)

        self.patches = [
            mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_render),
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
                # Ровно блок доставки finalize-meeting.py: пометка «собран из
                # присланной записи» (фаза 6), пути в meta доставки и `delivered` —
                # в ИСХОДНУЮ meta (`_delivery_marker_meta_path`).
                text = delivery_gate.with_recovery_note(PROTOCOL, meta, delivery_enabled=True)
                proto.write_text(text, encoding="utf-8")
                dm = dict(meta)
                dm.update({"date": date, "sessionUid": meta["sessionUid"],
                           "transcript_path": str(md), "protocol_path": str(proto)})
                delivery = lp.deliver_protocol(
                    meeting_meta=dm, protocol_text=text,
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
        # Фаза 6: в группу — только группа у серии И надёжная серия (журнал / календарь).
        for src in ("journal", "calendar"):
            self.assertEqual(ing.resolve_mode("auto", kind="audio", series="grp-series", source=src),
                             "standard")
        for src in (None, "name", "calendar_mismatch"):
            self.assertEqual(ing.resolve_mode("auto", kind="audio", series="grp-series", source=src),
                             "personal", "серия, угаданная по названию, ушла бы в группу")
        self.assertEqual(ing.resolve_mode("auto", kind="video", series="dm-series", source="journal"),
                         "personal")
        self.assertEqual(ing.resolve_mode("auto", kind="audio", series="no-such"), "personal")
        self.assertEqual(ing.resolve_mode("auto", kind="transcript", series="grp-series",
                                          source="journal"), "personal")
        self.assertEqual(ing.resolve_mode("standard", kind="audio", series="grp-series"), "standard",
                         "явный путь Б человека не ужесточается")
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



class TestCycle5Fixes(_RecoveryEnv):
    """Цикл из 5 ходов по фазе 5: что обёртка говорит и делает, когда что-то пошло
    не так, — и чтобы при этом не было второго протокола, второй оплаты расшифровки
    и потерянных записей."""

    # --- двойники finalize: сбой модели и сбой расшифровки ---
    def finalize_without_protocol(self):
        """finalize на живой проверке 26.09: расшифровка записана, протокол моделью не
        составлен (в finalize это не фатально, rc=0), доставки не было."""
        def run(meta_path, output_dir, *, deliver=False):
            self.finalize_calls.append({"meta": str(meta_path), "deliver": deliver})
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            sdir = Path(output_dir) / meta["series"]
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / f"{DATE}.md").write_text(TRANSCRIPT, encoding="utf-8")
            return 0, {"ok": True, "series": meta["series"], "protocol_path": str(sdir / f"{DATE}.md"),
                       "delivery": {"status": "not-run"}}
        return run

    def finalize_stashing(self):
        """Сбой расшифровки — ровно `_stash_into_failed`: копия записи и meta в `_failed/`,
        ИСХОДНИКИ УДАЛЕНЫ, заведено состояние повторов; rc=4."""
        from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415

        failed = Path(os.environ["MEETING_NOTARY_FAILED_DIR"])

        def run(meta_path, output_dir, *, deliver=False):
            self.finalize_calls.append({"meta": str(meta_path), "deliver": deliver})
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            sid = meta["sessionUid"]
            failed.mkdir(parents=True, exist_ok=True)
            wav = Path(meta["files"]["wav"])
            shutil.copy2(wav, failed / f"{sid}.wav")
            (failed / f"{sid}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (failed / f"{sid}.retry-state.json").write_text(json.dumps({
                "session_uid": sid, "attempts": 0, "rejected": False,
                "first_failed_at": _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }), encoding="utf-8")
            wav.unlink()
            Path(meta_path).unlink()
            return 4, None
        return run

    # --- сбой модели: честный итог, расшифровка не теряется ---
    def test_personal_protocol_not_generated_is_reported_and_kept(self):
        rc, out = self.run_main("--series", "dm-series", "--mode", "personal",
                                finalize=self.finalize_without_protocol())
        self.assertEqual(rc, ing.EXIT_FAILED)
        self.assertIsNotNone(out, "итога нет — Альфред скажет «сбой, пришли другой формат»")
        self.assertEqual(out["error"], "protocol_not_generated")
        self.assertIn("протокол dm-series %s" % DATE, out["note"])
        self.assertTrue((self.A / "dm-series" / f"{DATE}.md").is_file(), "расшифровка не попала в архив")
        self.assertEqual(self.sent, [])
        rc2, out2 = self.run_main("--series", "dm-series", "--mode", "personal")
        self.assertEqual((rc2, out2["error"]), (ing.EXIT_INPUT, "target_exists"))
        self.assertIn("только расшифровка", out2["note"])

    def test_standard_protocol_not_generated_note_tells_the_truth(self):
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard",
                                finalize=self.finalize_without_protocol())
        self.assertEqual(rc, ing.EXIT_FAILED)
        self.assertEqual(out["error"], "protocol_not_generated")
        self.assertNotIn("пустую", out["note"])
        self.assertIn("расшифровку возьмёт готовую", out["note"])
        self.assertTrue(out["transcript_path"].endswith(f"{DATE}.md"))
        self.assertTrue(Path(out["transcript_path"]).is_file(), "расшифровка потеряна")
        # В архив её положит сборщик, когда доведёт встречу (иначе там лёг бы дубль).
        self.assertFalse((self.A / "grp-series" / f"{DATE}.md").exists())
        self.assertTrue((self.T / f"{ing.safe_session_uid('grp-series', DATE)}.meta.json").is_file(),
                        "встреча ушла из очереди сборщика — доводить её некому")

    # --- сбой расшифровки: встречу доводит очередь повторов, второго прогона нет ---
    def test_stt_failure_on_personal_path_is_left_to_retry_queue(self):
        rc, out = self.run_main("--series", "dm-series", "--mode", "personal",
                                finalize=self.finalize_stashing())
        self.assertEqual((rc, out["error"]), (ing.EXIT_BUSY, "in_retry_queue"))
        self.assertIn("очередь повторов", out["note"])
        self.assertTrue(self.rec.exists(), "файл человека удалён вместе с исходником прогона")
        calls = len(self.finalize_calls)
        for mode in ("personal", "standard"):
            with self.subTest(mode=mode):
                rc2, out2 = self.run_main("--series", "dm-series", "--mode", mode)
                self.assertEqual((rc2, out2["error"]), (ing.EXIT_BUSY, "in_retry_queue"))
        self.assertEqual(len(self.finalize_calls), calls, "второй прогон запустил вторую финализацию")
        self.assertEqual(self.sent, [])

    def test_retry_that_will_not_happen_does_not_block(self):
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: PLC0415

        sid = ing.safe_session_uid("dm-series", DATE)
        failed = Path(os.environ["MEETING_NOTARY_FAILED_DIR"])
        failed.mkdir(parents=True, exist_ok=True)
        old = (_dt.now(_tz.utc) - _td(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for state in ({"rejected": True, "attempts": 99, "first_failed_at": old},
                      {"rejected": False, "attempts": 3, "first_failed_at": old}):
            with self.subTest(state=state):
                (failed / f"{sid}.retry-state.json").write_text(json.dumps(state), encoding="utf-8")
                self.assertFalse(ing.retry_pending(sid))
        (failed / f"{sid}.retry-state.json").write_text(json.dumps(
            {"rejected": False, "attempts": 3, "first_failed_at": old, "blocked_by_killswitch": True}),
            encoding="utf-8")
        self.assertTrue(ing.retry_pending(sid), "стоп-флаг держит встречу — повтор будет")

    # --- бот ещё доводит свою запись той же серии и даты ---
    def test_bot_still_finishing_same_series_and_date_blocks(self):
        bot_sid = f"{DATE}-tm-1"
        bot_meta = self.T / f"{bot_sid}.meta.json"
        meta = {"sessionUid": bot_sid, "series": "grp-series", "startTs": f"{DATE}T07:00:00Z",
                "files": {"wav": str(self.T / f"{bot_sid}.wav")}}
        bot_meta.write_text(json.dumps(meta), encoding="utf-8")
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual((rc, out["error"]), (ing.EXIT_BUSY, "series_date_busy"))
        self.assertEqual(self.finalize_calls, [])
        # Другая дата той же серии — не мешает.
        meta["startTs"] = "2026-09-22T07:00:00Z"
        bot_meta.write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(ing.pending_same_date_meetings(self.T, "grp-series", DATE, "x"), [])
        # Доставленная встреча бота не мешает (дальше решает target_exists).
        meta["startTs"] = f"{DATE}T07:00:00Z"
        meta["delivered"] = [{"chat_id": GROUP, "message_ids": [5], "decision": "pdf"}]
        bot_meta.write_text(json.dumps(meta), encoding="utf-8")
        rc2, out2 = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc2, 0, out2)

    def test_queue_done_matches_collector(self):
        """Копия критерия «доставлено» сборщика в обёртке — сторож паритета."""
        samples = [
            {}, {"delivered": []}, {"delivered": None},
            {"delivered": {"chat_id": GROUP, "message_ids": [1]}},
            {"delivered": [{"chat_id": 0, "message_ids": [0], "decision": "owner-alert-failed-recording"}]},
            {"delivered": [{"chat_id": GROUP, "message_ids": [3], "decision": "partial-failure"}]},
            {"delivered": [{"chat_id": GROUP, "message_ids": []}, {"chat_id": OWNER, "message_ids": [9]}]},
        ]
        for m in samples:
            with self.subTest(meta=m):
                self.assertEqual(ing._queue_delivery_done(m), collector._delivery_done(m))

    # --- --force: прежние файлы не теряются ---
    def test_force_keeps_copies_of_replaced_files(self):
        sdir = self.W / "grp-series"
        sdir.mkdir(parents=True)
        (sdir / f"{DATE}.md").write_text("частичная запись бота", encoding="utf-8")
        (sdir / f"{DATE}-protokol.md").write_text("протокол бота", encoding="utf-8")
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard", "--force")
        self.assertEqual(rc, 0, out)
        backups = sorted((sdir / "_versions").glob("before-recovery-*/*"))
        self.assertEqual(sorted(p.name for p in backups), [f"{DATE}-protokol.md", f"{DATE}.md"])
        saved = next(p for p in backups if p.name == f"{DATE}.md")
        self.assertEqual(saved.read_text(encoding="utf-8"), "частичная запись бота")
        self.assertEqual(len(out["replaced_backup"]), 2)
        self.assertEqual((sdir / f"{DATE}.md").read_text(encoding="utf-8"), TRANSCRIPT)

    # --- повтор пути А не обещает PDF, который не придёт ---
    def test_rerun_personal_does_not_promise_pdf_again(self):
        with mock.patch("lib.notify.push_via_notarius", return_value=True) as notice:
            self.run_main("--series", "dm-series", "--mode", "personal")
            rc, out = self.run_main("--series", "dm-series", "--mode", "personal", "--force")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["delivery"]["status"], "skipped")
        self.assertEqual(notice.call_count, 1, "второе «PDF следующим сообщением» без PDF")

    # --- правка ответом: «не знаю» — не «не сработает» ---
    def test_replies_unknown_without_listener_roots(self):
        os.environ.pop("MEETING_NOTARY_DELIVERED_ROOTS", None)
        rc, out = self.run_main("--series", "dm-series", "--mode", "personal")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(out["replies_enabled"],
                          "без папок поиска слушателя обёртка не знает — и не должна пугать «не сработает»")

    # --- замок: «не смог завести» — не «уже собирают» ---
    def test_lock_unavailable_is_not_reported_as_busy(self):
        os.environ[finalize_lock.DIRS_ENV] = "/nonexistent-root-dir-cycle5/locks"
        rc, out = self.run_main("--series", "dm-series")
        self.assertEqual(rc, ing.EXIT_FAILED)
        self.assertIsNone(out, "«уже собирает бот» — неправда: не собирает никто")
        self.assertEqual(self.finalize_calls, [])

    # --- итог — последняя строка общего журнала (методичка читает tail -1) ---
    def test_json_is_last_line_of_combined_output(self):
        import subprocess  # noqa: PLC0415

        sid = ing.safe_session_uid("dm-series", DATE)
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        with finalize_lock.hold(sid) as held:
            self.assertTrue(held)
            proc = subprocess.run(
                [sys.executable, str(_NOTARY / "tools" / "ingest-recording.py"), str(self.rec),
                 "--series", "dm-series", "--date", DATE, "--kind", "audio"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120,
            )
        self.assertEqual(proc.returncode, ing.EXIT_BUSY, proc.stdout[-400:])
        last = proc.stdout.strip().splitlines()[-1]
        self.assertEqual(json.loads(last)["error"], "finalize_in_progress")

    # --- общая папка очереди: подложенный ярлык не срабатывает ---
    def test_planted_link_in_queue_is_not_followed(self):
        sid = ing.safe_session_uid("grp-series", DATE)
        victim = self.tmp / "victim.txt"
        victim.write_text("чужой файл хозяина", encoding="utf-8")
        (self.T / f".{sid}.part.wav").symlink_to(victim)
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard")
        self.assertEqual(rc, 0, out)
        self.assertEqual(victim.read_text(encoding="utf-8"), "чужой файл хозяина")
        self.assertEqual([p.name for p in self.T.iterdir() if p.name.endswith(".part.wav")
                          and not p.is_symlink()], [], "недописанная копия осталась в очереди")

    # --- сборщик: дата встречи из имени и уборка под замком ---
    def test_collector_dates_recovered_meeting_by_its_name(self):
        for series in ("grp-series", "проект"):
            with self.subTest(series=series):
                self.assertEqual(collector._date_from_filename_uid(ing.safe_session_uid(series, DATE)), DATE)
        self.assertEqual(collector._date_from_filename_uid(f"{DATE}-tm-1"), DATE)

    def test_collector_does_not_wipe_recording_under_manual_lock(self):
        sid = ing.safe_session_uid("grp-series", DATE)
        wav = self.T / f"{sid}.wav"
        wav.write_bytes(b"RIFFxxxxWAVEfresh")
        meta = {"sessionUid": sid, "files": {"wav": str(wav)}, "keepAudio": False,
                "delivered": [{"chat_id": GROUP, "message_ids": [7], "decision": "pdf"}]}
        with mock.patch.object(collector, "LOCAL_FINALIZE", True), \
                mock.patch.object(collector, "_portal_sources_held", return_value=False):
            with finalize_lock.hold(sid) as held:
                self.assertTrue(held)
                collector._cleanup_delivered_under_lock(meta, sid)
                self.assertTrue(wav.exists(), "сборщик стёр свежую запись посреди ручной финализации")
            collector._cleanup_delivered_under_lock(meta, sid)
        self.assertFalse(wav.exists(), "после ручной финализации уборка идёт как раньше")


class TestAutoModeNeedsReliableSeries(_RecoveryEnv):
    """Фаза 6 (копилка владельца п.5): `--mode auto` шлёт в группу серии, только если
    серию подтверждает журнал сторожа или событие календаря, привязанное к серии.
    23.09.2026 протокол ушёл в группу по серии, угаданной по названию, — и имена в нём
    были перепутаны. Угаданная серия → лично владельцу, с понятной строкой."""

    GROUP_NOTE_MARK = "Протокол собран из присланной записи"

    def write_journal(self, **entries) -> None:
        """Журнал сторожа входа (`lib/join_watch.ledger_path`) — рядом с реестром."""
        data = {}
        for key, (series, start_at, extra) in entries.items():
            data[key] = {"series": series, "start_at": start_at, "state": "alerted",
                         "attempts": [], "created_at": "2026-09-23T07:00:00+00:00", **extra}
        (self.R / ".join-watch.json").write_text(json.dumps(data), encoding="utf-8")

    def bind_event(self, event_id: str) -> None:
        (self.R / "watched.yaml").write_text(
            "watched:\n"
            "  - id: grp-series\n    series: grp-series\n    type: google-calendar\n"
            "    calendar_id: cal-test\n    recurring_event_id: %s\n    telegram_chat_id: %d\n"
            "  - id: dm-series\n    series: dm-series\n" % (event_id, GROUP),
            encoding="utf-8",
        )

    def test_journal_confirms_series_goes_to_group_with_note(self):
        self.write_journal(k1=("grp-series", f"{DATE}T07:00:00Z", {}))
        rc, out = self.run_main("--series", "grp-series", "--mode", "auto")
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["mode"], out["series_source"]), ("standard", "journal"))
        self.assertNotIn("mode_note", out)
        self.assertEqual([c for c, _ in self.sent], [GROUP])
        self.assertIn(self.GROUP_NOTE_MARK, self.rendered[-1],
                      "в группу ушёл протокол из записи без пометки «собран из присланной записи»")
        proto = Path(out["protocol_path"]).read_text(encoding="utf-8")
        self.assertIn(self.GROUP_NOTE_MARK, proto, "пометки нет в файле — перевыпуск её потеряет")

    def test_journal_date_is_moscow_date(self):
        # 22.09 22:30 UTC = 23.09 01:30 МСК — встреча 23-го.
        self.write_journal(k1=("grp-series", "2026-09-22T22:30:00Z", {}))
        self.assertTrue(ing.series_in_join_journal("grp-series", DATE))
        # 23.09 21:30 UTC = 24.09 00:30 МСК — это уже другая встреча.
        self.write_journal(k1=("grp-series", "2026-09-23T21:30:00Z", {}))
        self.assertFalse(ing.series_in_join_journal("grp-series", DATE))

    def test_journal_not_evidence_for_other_date_series_or_portal(self):
        self.write_journal(
            other_date=("grp-series", "2026-09-22T07:00:00Z", {}),
            other_series=("dm-series", f"{DATE}T07:00:00Z", {}),
            portal=("grp-series", f"{DATE}T09:00:00Z", {"order_id": "portal:1"}),
        )
        self.assertFalse(ing.series_in_join_journal("grp-series", DATE))
        rc, out = self.run_main("--series", "grp-series", "--mode", "auto")
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["mode"], out["series_source"]), ("personal", "name"))
        self.assertEqual([c for c, _ in self.sent], [OWNER])

    def test_series_by_name_goes_to_owner_with_plain_line(self):
        with mock.patch("lib.notify.push_via_notarius", return_value=True) as notice:
            rc, out = self.run_main("--series", "grp-series", "--mode", "auto")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["mode"], "personal", "угаданная по названию серия ушла в группу")
        self.assertEqual(out["series_source"], "name")
        self.assertIn("серию определил по названию — в группу не отправляю", out["mode_note"])
        self.assertEqual([c for c, _ in self.sent], [OWNER], "протокол ушёл не только владельцу")
        self.assertEqual([c["deliver"] for c in self.finalize_calls], [False])
        self.assertIn("Серию определил по названию — в группу не отправляю",
                      notice.call_args[0][0], "владелец не узнал, почему не в группу")
        proto = Path(out["protocol_path"]).read_text(encoding="utf-8")
        self.assertIn("Диаризация не выверена", proto)
        self.assertNotIn(self.GROUP_NOTE_MARK, proto, "на пути А пометка была бы двойной")

    def test_calendar_event_bound_to_series_goes_to_group(self):
        self.bind_event("evtgrp123")
        rc, out = self.run_main("--series", "grp-series", "--mode", "auto",
                                "--calendar-event", "evtgrp123_20260923T070000Z")
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["mode"], out["series_source"]), ("standard", "calendar"))
        self.assertEqual([c for c, _ in self.sent], [GROUP])

    def test_calendar_event_of_other_series_is_not_overridden_by_journal(self):
        self.bind_event("evtgrp123")
        self.write_journal(k1=("grp-series", f"{DATE}T07:00:00Z", {}))
        rc, out = self.run_main("--series", "grp-series", "--mode", "auto",
                                "--calendar-event", "evt-another-series")
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["mode"], out["series_source"]), ("personal", "calendar_mismatch"))
        self.assertIn("не привязано к этой серии", out["mode_note"])
        self.assertEqual([c for c, _ in self.sent], [OWNER])
        self.assertFalse(ing.series_bound_to_calendar_event("grp-series", "evtgrp123; rm -rf /"))

    def test_series_without_group_and_explicit_standard_unchanged(self):
        rc, out = self.run_main("--series", "dm-series", "--mode", "auto")
        self.assertEqual((rc, out["mode"]), (0, "personal"))
        self.assertNotIn("mode_note", out, "у серии нет группы — «не отправляю в группу» лишнее")
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard", "--force")
        self.assertEqual((rc, out["mode"]), (0, "standard"))
        self.assertNotIn("series_source", out)


class TestRecoveryNoteInFinalize(unittest.TestCase):
    """Пометка «собран из присланной записи» — только у протокола из записи и только
    когда его шлёт finalize (доставка включена): путь А ставит свою пометку сам."""

    META = {"recovery": True, "endReason": "manual_recovery", "series": "s"}

    def test_note_after_header_once(self):
        text = "# Протокол\n\nтело\n"
        once = delivery_gate.with_recovery_note(text, self.META, delivery_enabled=True)
        self.assertTrue(once.startswith("# Протокол\n\n> ♻️ **Протокол собран из присланной записи**"))
        self.assertTrue(once.endswith("тело\n"))
        self.assertEqual(delivery_gate.with_recovery_note(once, self.META, delivery_enabled=True), once)
        no_header = delivery_gate.with_recovery_note("тело", self.META, delivery_enabled=True)
        self.assertTrue(no_header.startswith(delivery_gate.RECOVERED_FROM_RECORDING_NOTE))

    def test_no_note_for_bot_meetings_or_personal_path(self):
        text = "# Протокол\n\nтело\n"
        for meta in ({}, {"endReason": "manual_recovery"}, {"recovery": True, "endReason": "left"},
                     {"recovery": "yes", "endReason": "manual_recovery"}, None):
            with self.subTest(meta=meta):
                self.assertEqual(delivery_gate.with_recovery_note(text, meta, delivery_enabled=True), text)
        self.assertEqual(delivery_gate.with_recovery_note(text, self.META, delivery_enabled=False), text)

    def test_finalize_marks_before_delivery(self):
        """Шов: боевой finalize ставит пометку ДО `deliver_protocol` и по включённой доставке."""
        src = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        hook = src.index("delivery_gate.with_recovery_note(")
        self.assertLess(hook, src.index("delivery_result = deliver_protocol("))
        self.assertIn("delivery_enabled=_is_protocol_delivery_enabled()", src[hook:hook + 300])
        self.assertIn("protocol_path.write_text(_noted", src[hook:hook + 600])
        self.assertTrue(callable(getattr(lp, "_is_protocol_delivery_enabled", None)))
        synthetic = ing.build_synthetic_meta(series="s", date=DATE, wav_path="/w.wav",
                                             participants=[], sid="recovery-s-" + DATE)
        self.assertTrue(delivery_gate.is_recovered_meeting(synthetic),
                        "синтетика обёртки и признак в finalize разошлись")


class TestPersonalRecoveryOutOfSeriesMemory(_RecoveryEnv):
    """Фаза 6б (копилка владельца №6, 26.09): встреча, собранная «лично мне» (путь А) по
    серии с групповым чатом, не идёт в память серии — следующие протоколы группы её не
    видят. Путь Б — идёт, как обычная встреча; серия без группы — как обычно.

    Двойник finalize пишет выжимку ровно как боевой блок 4.0.2d: из готового протокола,
    с meta встречи целиком (`_digest_meta = dict(meta)`)."""

    PREV = "2026-09-16"
    NEXT = "2026-09-30"
    PEOPLE = ["Иван Петров", "Мария Сидорова"]

    def finalize_with_memory(self, seen: list | None = None):
        base = self.fake_finalize(during=(lambda mp: seen.append(json.loads(Path(mp).read_text(
            encoding="utf-8")))) if seen is not None else None)

        def run(meta_path, output_dir, *, deliver=False):
            rc, report = base(meta_path, output_dir, deliver=deliver)
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            date = meta["startTs"][:10]
            digest_meta = dict(meta)
            digest_meta["date"] = date
            digest_meta["participants"] = list(self.PEOPLE)
            series_memory.save_meeting_digest(Path(output_dir) / meta["series"], date, PROTOCOL, digest_meta)
            return rc, report
        return run

    def prev_group_meeting(self, series: str) -> Path:
        sdir = self.W / series
        series_memory.save_digest(sdir, self.PREV, {
            "schema": 1, "date": self.PREV, "series": series, "participants": list(self.PEOPLE),
            "themes": ["бюджет"], "key_points": [], "open_tasks": ["Иван Петров: подготовить отчёт"]})
        return sdir

    def memory_dates(self, sdir: Path) -> list:
        return [d["date"] for d in series_memory.list_series_digests(sdir)]

    def next_protocol_memory(self, sdir: Path) -> list:
        return [d["date"] for d in series_memory.resolve_memory(
            sdir, self.W, current_participants=list(self.PEOPLE), current_date=self.NEXT)]

    def test_personal_by_group_series_stays_out_of_series_memory(self):
        sdir = self.prev_group_meeting("grp-series")
        seen: list = []
        rc, out = self.run_main("--series", "grp-series", "--mode", "personal",
                                finalize=self.finalize_with_memory(seen))
        self.assertEqual((rc, out["mode"]), (0, "personal"), out)
        self.assertIs(seen[0].get(ing.SERIES_MEMORY_EXCLUDE_KEY), True,
                      "meta для finalize без пометки — повтор после сбоя расшифровки её потеряет")
        self.assertTrue((sdir / f"{DATE}-memory.json").is_file(), "выжимка нужна перевыпуску этой встречи")
        self.assertTrue(series_memory.is_excluded_digest(
            series_memory.load_digest(sdir / f"{DATE}-memory.json")))
        self.assertEqual(self.memory_dates(sdir), [self.PREV], "память серии взяла встречу «лично мне»")
        self.assertEqual(self.next_protocol_memory(sdir), [self.PREV],
                         "следующий протокол группы опирается на встречу «лично мне»")
        meta = json.loads(Path(out["meta_path"]).read_text(encoding="utf-8"))
        self.assertIs(meta.get(ing.SERIES_MEMORY_EXCLUDE_KEY), True)

    def test_standard_by_group_series_goes_into_series_memory(self):
        sdir = self.prev_group_meeting("grp-series")
        rc, out = self.run_main("--series", "grp-series", "--mode", "standard",
                                finalize=self.finalize_with_memory())
        self.assertEqual((rc, out["mode"]), (0, "standard"), out)
        self.assertEqual(self.memory_dates(sdir), [self.PREV, DATE])
        self.assertEqual(self.next_protocol_memory(sdir), [self.PREV, DATE])

    def test_auto_withheld_from_group_stays_out_of_series_memory(self):
        sdir = self.prev_group_meeting("grp-series")
        rc, out = self.run_main("--series", "grp-series", "--mode", "auto",
                                finalize=self.finalize_with_memory())
        self.assertEqual((rc, out["mode"]), (0, "personal"), out)
        self.assertIn("mode_note", out)
        self.assertEqual(self.memory_dates(sdir), [self.PREV])

    def test_personal_by_series_without_group_is_ordinary_memory(self):
        sdir = self.prev_group_meeting("dm-series")
        rc, out = self.run_main("--series", "dm-series", "--mode", "personal",
                                finalize=self.finalize_with_memory())
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.memory_dates(sdir), [self.PREV, DATE])

    def test_unreadable_registry_excludes(self):
        self.assertTrue(ing.personal_excluded_from_series_memory("grp-series"))
        self.assertFalse(ing.personal_excluded_from_series_memory("dm-series"))
        with mock.patch("notary.cli.registry.load_watched", side_effect=OSError("disk")):
            self.assertTrue(ing.personal_excluded_from_series_memory("dm-series"))
            self.assertIsNone(ing.series_group_chat("grp-series"), "в группу без реестра не шлём")

    def test_personal_by_series_with_someone_elses_dm_is_excluded(self):
        # Цикл5 фазы 6б (Н4): получатель серии — не только группа, но и личка другого
        # человека; личка владельца (или нет привязки) — встреча остаётся в памяти.
        (self.R / "watched.yaml").write_text(
            "watched:\n"
            "  - id: emp-series\n    series: emp-series\n    telegram_chat_id: 222\n"
            "  - id: own-series\n    series: own-series\n    telegram_chat_id: %d\n"
            "  - id: dm-series\n    series: dm-series\n" % OWNER, encoding="utf-8")
        self.assertTrue(ing.personal_excluded_from_series_memory("emp-series"))
        self.assertFalse(ing.personal_excluded_from_series_memory("own-series"))
        self.assertFalse(ing.personal_excluded_from_series_memory("dm-series"))
        self.assertFalse(ing.personal_excluded_from_series_memory("unknown-series"))

    def test_broken_registry_is_unreadable_not_a_crash(self):
        # Цикл5 фазы 6б (Н7): битый watched.yaml реестр сообщает SystemExit — обёртка не
        # падает посреди прогона без итоговой строки, а считает реестр непрочитанным.
        (self.R / "watched.yaml").write_text("- not: a mapping\n", encoding="utf-8")
        self.assertTrue(ing.personal_excluded_from_series_memory("dm-series"))
        self.assertIsNone(ing.series_group_chat("grp-series"))

    def test_personal_from_transcript_is_marked_out_of_memory(self):
        # Цикл5 фазы 6б (Н5, Н6): протокол из присланного текста выжимку не строит — дата
        # всё равно помечена, её не берут ни память, ни «пришлите протокол» из группы.
        sdir = self.prev_group_meeting("grp-series")
        (sdir / f"{self.PREV}-protokol.md").write_text(PROTOCOL, encoding="utf-8")
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
            rc, out = self.run_main("--series", "grp-series", "--mode", "personal")
        self.assertEqual(rc, 0, out)
        self.assertTrue(series_memory.is_excluded_digest(
            series_memory.load_digest(sdir / f"{DATE}-memory.json")))
        self.assertEqual(self.memory_dates(sdir), [self.PREV])
        import notary.meetings_listener as ml  # noqa: PLC0415

        self.assertEqual(ml._latest_protocol_path("grp-series", skip_personal=True)[1], self.PREV)
        self.assertEqual(ml._latest_protocol_path("grp-series")[1], DATE)

    def test_personal_protocol_not_generated_still_marked(self):
        # Цикл5 фазы 6б: расшифровка есть, протокол Claude не составил (лимит, ISS-61). Его
        # соберут позже «протокол <серия> <дата>» — дата уже помечена «лично мне».
        sdir = self.prev_group_meeting("grp-series")

        def no_protocol(meta_path, output_dir, *, deliver=False):
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            d = Path(output_dir) / meta["series"]
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{meta['startTs'][:10]}.md").write_text(TRANSCRIPT, encoding="utf-8")
            return 0, {"ok": True, "series": meta["series"], "delivery": {"status": "disabled"}}

        rc, out = self.run_main("--series", "grp-series", "--mode", "personal", finalize=no_protocol)
        self.assertEqual(out.get("error"), "protocol_not_generated", out)
        self.assertTrue(series_memory.is_excluded_digest(
            series_memory.load_digest(sdir / f"{DATE}-memory.json")))
        # Серия без получателей кроме владельца — пометки нет.
        dsdir = self.prev_group_meeting("dm-series")
        self.run_main("--series", "dm-series", "--mode", "personal", finalize=no_protocol)
        self.assertFalse((dsdir / f"{DATE}-memory.json").exists())

    def test_meta_key_matches_series_memory(self):
        self.assertEqual(ing.SERIES_MEMORY_EXCLUDE_KEY, series_memory.META_EXCLUDE_KEY)
        plain = ing.build_synthetic_meta(series="s", date=DATE, wav_path="/w.wav", participants=[], sid="x")
        self.assertNotIn(ing.SERIES_MEMORY_EXCLUDE_KEY, plain)


from lib import series_memory  # noqa: E402


if __name__ == "__main__":
    unittest.main()
