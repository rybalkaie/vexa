"""Тесты Ф1 плана umnyi-protokol-assemblyai — переезд STT на AssemblyAI.

Закрепляют контракт нового движка (REQ S1/S2/S3/S5/S6/D1) на ФИКСТУРАХ —
боевая сеть AAI не дёргается (ключа на маке headless нет; ПДн-аудио удалены
2026-06-13). Что доказываем без живого ключа:
  • парсер utterances[] → Utterance (мс→сек, метки A/B, защита от мусора) — S2;
  • адаптер to_aligned_turns: A/B/C → SPEAKER_00/01/02 стабильно — S2;
  • orchestration upload→create→poll до completed, on_transcript_created ДО
    поллинга, инъекция sleep — S1;
  • идемпотентность по transcript id (reuse без upload/create) — S5;
  • не-тихий сбой: status=error → AssemblyAIRejectedError; 5xx → 1 ретрай; нет
    id → ошибка; kill-switch блокирует новый сабмит, reuse — нет — S5;
  • приватность: ключ и текст реплик НЕ попадают в логи; тело ошибки [:80] — S6;
  • speech_bounds_ms_from_raw_json понимает форму AAI (words[]/utterances[] в мс),
    SM-форма не сломана;
  • finalize: STT_BACKEND=assemblyai → ветка AAI; _is_external_stt/_stt_id_meta_key.

Боевой прогон на ≥2–3 реальных встречах (нужен ASSEMBLYAI_API_KEY на VPS) —
deploy-остаток за владельцем (РИСК5), синтетикой не подменяется.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase1_assemblyai_stt -v
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


# Под системным python3 (без httpx/requests/...) грузим с заглушками отсутствующих
# third-party — тот же приём, что в test_phase2_cost_guard.
def _stub_missing(*names):
    for name in names:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)


_stub_missing("yaml", "requests", "pymorphy3", "torch", "numpy")


def _install_rich_httpx_stub():
    """httpx-заглушка с Client-контекстменеджером и классами-исключениями,
    на которых assemblyai_client делает isinstance (см. _is_retryable_http_error)."""
    if "httpx" in sys.modules and getattr(sys.modules["httpx"], "_rich_stub", False):
        return
    if "httpx" in sys.modules and hasattr(sys.modules["httpx"], "Client") \
            and not isinstance(sys.modules["httpx"], types.ModuleType):
        return  # настоящий httpx — не трогаем
    fake = types.ModuleType("httpx")
    fake._rich_stub = True

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _HTTPStatusError(Exception):
        def __init__(self, *a, response=None, **k):
            super().__init__(*a)
            self.response = response

    fake.Client = _Client
    fake.TimeoutException = type("TimeoutException", (Exception,), {})
    fake.ConnectError = type("ConnectError", (Exception,), {})
    fake.RemoteProtocolError = type("RemoteProtocolError", (Exception,), {})
    fake.RequestError = type("RequestError", (Exception,), {})
    fake.HTTPStatusError = _HTTPStatusError
    sys.modules["httpx"] = fake


_install_rich_httpx_stub()

# Импортируем как часть пакета `lib` — относительные импорты (.align) резолвятся.
import lib.assemblyai_client as aai  # noqa: E402
import lib.protocol_to_tg as ptg  # noqa: E402


# ─────────────────────────── фикстуры ───────────────────────────

def _completed_raw(text_a="реплика один", text_b="реплика два"):
    """Завершённый transcript-объект AAI (как из GET /transcript при completed)."""
    return {
        "id": "tid-fixture-1",
        "status": "completed",
        "language_code": "ru",
        "audio_duration": 75,
        "text": f"{text_a} {text_b}",
        "utterances": [
            {"speaker": "A", "start": 1000, "end": 4000, "text": text_a},
            {"speaker": "B", "start": 4200, "end": 8000, "text": text_b},
            {"speaker": "A", "start": 8200, "end": 12000, "text": "третья"},
            {"speaker": "B", "start": 12500, "end": 13000, "text": "   "},  # пустая → выброс
        ],
        "words": [
            {"text": "реплика", "start": 1000, "end": 1500, "speaker": "A"},
            {"text": "один", "start": 1500, "end": 2000, "speaker": "A"},
            {"text": "два", "start": 7500, "end": 8000, "speaker": "B"},
        ],
    }


# ─────────────────────────── S2: парсер ───────────────────────────

class TestParseUtterances(unittest.TestCase):
    def test_basic_mapping_and_ms_to_sec(self):
        utts = aai.parse_utterances(_completed_raw())
        # 4 реплики, одна пустая выброшена → 3
        self.assertEqual(len(utts), 3)
        self.assertEqual(utts[0].speaker, "A")
        self.assertEqual(utts[1].speaker, "B")
        # мс → сек
        self.assertAlmostEqual(utts[0].start, 1.0)
        self.assertAlmostEqual(utts[0].end, 4.0)
        self.assertAlmostEqual(utts[1].start, 4.2)
        self.assertEqual(utts[0].text, "реплика один")

    def test_defensive_garbage(self):
        raw = {"utterances": [
            {"speaker": None, "start": "x", "end": None, "text": "ok"},
            {"speaker": "A", "text": ""},          # пустой текст → выброс
            "not-a-dict",                            # мусор → пропуск
            {"speaker": "C", "start": -50, "end": 2000, "text": "neg"},
        ]}
        utts = aai.parse_utterances(raw)
        self.assertEqual(len(utts), 2)
        self.assertEqual(utts[0].speaker, "?")      # None → '?'
        self.assertEqual(utts[0].start, 0.0)        # битый тайминг → 0.0
        self.assertEqual(utts[1].speaker, "C")
        self.assertEqual(utts[1].start, 0.0)        # отрицательный → 0.0

    def test_no_utterances(self):
        self.assertEqual(aai.parse_utterances({"status": "completed"}), [])
        self.assertEqual(aai.parse_utterances({"utterances": "nope"}), [])


# ─────────────────────────── S2: адаптер ───────────────────────────

class TestToAlignedTurns(unittest.TestCase):
    def test_letters_to_speaker_nn_stable(self):
        utts = [
            aai.Utterance("A", 0.0, 1.0, "a1"),
            aai.Utterance("B", 1.0, 2.0, "b1"),
            aai.Utterance("A", 2.0, 3.0, "a2"),
            aai.Utterance("C", 3.0, 4.0, "c1"),
        ]
        turns = aai.to_aligned_turns(utts)
        self.assertEqual([t.speaker for t in turns],
                         ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_02"])
        # секунды сохранены, display_name пуст (заполнит name_mapping)
        self.assertEqual(turns[0].start, 0.0)
        self.assertIsNone(turns[0].display_name)
        self.assertEqual(turns[3].text, "c1")


# ─────────────────────── длительность / язык ───────────────────────

class TestDurationAndLang(unittest.TestCase):
    def test_audio_duration_field(self):
        utts = aai.parse_utterances(_completed_raw())
        self.assertEqual(aai._extract_audio_duration_s(_completed_raw(), utts), 75.0)

    def test_duration_fallback_to_words(self):
        raw = dict(_completed_raw())
        raw.pop("audio_duration")
        utts = aai.parse_utterances(raw)
        # max end слова = 8000 мс → 8.0 сек
        self.assertEqual(aai._extract_audio_duration_s(raw, utts), 8.0)

    def test_language(self):
        self.assertEqual(aai._extract_detected_language(_completed_raw()), "ru")
        self.assertEqual(aai._extract_detected_language({}), "unknown")


# ─────────────────── S1: orchestration upload→create→poll ───────────────────

class _OrchBase(unittest.TestCase):
    def setUp(self):
        os.environ["ASSEMBLYAI_API_KEY"] = "test-secret-KEY-do-not-log"
        self._saved_backoff = aai.RETRY_BACKOFF_S
        aai.RETRY_BACKOFF_S = 0.0  # без реального 30-сек ожидания на ретраях
        self._saved = {n: getattr(aai, n) for n in
                       ("_upload_audio", "_create_transcript", "_get_transcript")}
        # реальный временный WAV (для p.exists()/p.stat() в новом сабмите)
        fd, self._wav = tempfile.mkstemp(suffix=".wav")
        os.write(fd, b"RIFFfakewavdata")
        os.close(fd)
        os.environ.pop("STT_KILLSWITCH_PATH", None)

    def tearDown(self):
        aai.RETRY_BACKOFF_S = self._saved_backoff
        for n, v in self._saved.items():
            setattr(aai, n, v)
        try:
            os.unlink(self._wav)
        except OSError:
            pass
        os.environ.pop("STT_KILLSWITCH_PATH", None)


class TestTranscribeOrchestration(_OrchBase):
    def test_upload_create_poll_until_completed(self):
        events = []
        polls = {"n": 0}

        aai._upload_audio = lambda c, h, p: events.append("upload") or "https://up/abc"
        # double отражает сигнатуру _create_transcript после Ф2 (опц. keyterms_prompt).
        aai._create_transcript = lambda c, h, url, **_k: events.append("create") or "tid-1"

        def fake_get(c, h, tid):
            events.append("poll")
            polls["n"] += 1
            if polls["n"] < 2:
                return {"status": "processing"}
            return _completed_raw()

        aai._get_transcript = fake_get
        created = []
        res = aai.transcribe_diarize_wav(
            self._wav,
            on_transcript_created=lambda t: created.append(t),
            sleep=lambda _s: None,
        )
        # порядок: upload → create → poll(processing) → poll(completed)
        self.assertEqual(events, ["upload", "create", "poll", "poll"])
        self.assertEqual(created, ["tid-1"])  # callback вызван (ДО завершения поллинга)
        self.assertEqual(res.job_id, "tid-1")
        self.assertEqual(len(res.utterances), 3)
        self.assertEqual(res.detected_language, "ru")
        self.assertEqual(res.audio_duration_s, 75.0)
        self.assertIn("utterances", res.raw_json)

    def test_callback_runs_before_polling(self):
        order = []
        aai._upload_audio = lambda c, h, p: "https://up"
        aai._create_transcript = lambda c, h, url, **_k: "tid-x"

        def fake_get(c, h, tid):
            order.append("poll")
            return _completed_raw()

        aai._get_transcript = fake_get
        aai.transcribe_diarize_wav(
            self._wav,
            on_transcript_created=lambda t: order.append("callback"),
            sleep=lambda _s: None,
        )
        self.assertEqual(order[0], "callback")
        self.assertIn("poll", order)


class TestIdempotency(_OrchBase):
    def test_existing_id_skips_upload_and_create(self):
        called = {"upload": 0, "create": 0}

        def boom_upload(*a, **k):
            called["upload"] += 1
            raise AssertionError("upload не должен вызываться при reuse")

        def boom_create(*a, **k):
            called["create"] += 1
            raise AssertionError("create не должен вызываться при reuse")

        aai._upload_audio = boom_upload
        aai._create_transcript = boom_create
        aai._get_transcript = lambda c, h, tid: _completed_raw()

        res = aai.transcribe_diarize_wav(
            "/nonexistent/he.wav",  # WAV отсутствует — но reuse не читает файл
            existing_transcript_id="tid-reuse-9",
            sleep=lambda _s: None,
        )
        self.assertEqual(called, {"upload": 0, "create": 0})
        self.assertEqual(res.job_id, "tid-reuse-9")


class TestFailureNonSilent(_OrchBase):
    def test_status_error_raises_rejected(self):
        aai._upload_audio = lambda c, h, p: "https://up"
        aai._create_transcript = lambda c, h, url, **_k: "tid-err"
        aai._get_transcript = lambda c, h, tid: {
            "status": "error", "error": "audio file is corrupt"
        }
        with self.assertRaises(aai.AssemblyAIRejectedError):
            aai.transcribe_diarize_wav(self._wav, sleep=lambda _s: None)

    def test_create_without_id_raises(self):
        # Реальный _create_transcript на ответе без id → AssemblyAIError.
        import httpx

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"no_id": True}

        class _Cli:
            def post(self, *a, **k):
                return _Resp()

        with self.assertRaises(aai.AssemblyAIError):
            aai._create_transcript(_Cli(), {"authorization": "k"}, "https://up")

    def test_retryable_5xx_retries_once(self):
        import httpx
        calls = {"n": 0}

        class _Resp500:
            status_code = 503
            text = "service unavailable"

        def flaky(c, h, tid):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.HTTPStatusError("boom", response=_Resp500())
            return _completed_raw()

        # Бьём через реальный _call_with_retry-обёрнутый _get_transcript: подменяем
        # сетевой слой внутри — проще проверить _call_with_retry напрямую.
        out = aai._call_with_retry(lambda: flaky(None, None, "t"), action="poll")
        self.assertEqual(calls["n"], 2)  # 1 сбой + 1 ретрай
        self.assertEqual(out["status"], "completed")

    def test_non_retryable_raises_immediately(self):
        import httpx
        calls = {"n": 0}

        class _Resp400:
            status_code = 401
            text = "bad key"

        def fn():
            calls["n"] += 1
            raise httpx.HTTPStatusError("nope", response=_Resp400())

        with self.assertRaises(httpx.HTTPStatusError):
            aai._call_with_retry(fn, action="poll")
        self.assertEqual(calls["n"], 1)  # 401 не ретраится


class TestKillSwitch(_OrchBase):
    def _arm_killswitch(self):
        fd, flag = tempfile.mkstemp(suffix=".flag")
        os.close(fd)
        os.environ["STT_KILLSWITCH_PATH"] = flag
        return flag

    def test_new_submit_blocked_when_armed(self):
        flag = self._arm_killswitch()
        try:
            aai._upload_audio = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("upload не должен вызываться при взведённом kill-switch"))
            with self.assertRaises(aai.AssemblyAIKillSwitchError):
                aai.transcribe_diarize_wav(self._wav, sleep=lambda _s: None)
        finally:
            os.unlink(flag)

    def test_reuse_not_blocked_when_armed(self):
        flag = self._arm_killswitch()
        try:
            aai._get_transcript = lambda c, h, tid: _completed_raw()
            res = aai.transcribe_diarize_wav(
                self._wav, existing_transcript_id="tid-reuse",
                sleep=lambda _s: None,
            )
            self.assertEqual(res.job_id, "tid-reuse")  # reuse прошёл несмотря на флаг
        finally:
            os.unlink(flag)


# ─────────────────────────── S6: приватность ───────────────────────────

class _CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            self.records.append(self.format(record))
        except Exception:
            self.records.append(str(record.msg))


class TestPrivacy(_OrchBase):
    def test_key_and_transcript_text_not_logged(self):
        secret_text = "СВЕРХСЕКРЕТНАЯ-РЕПЛИКА-УВОЛЬНЕНИЕ"
        key_val = os.environ["ASSEMBLYAI_API_KEY"]
        cap = _CaptureLogs()
        cap.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(cap)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            aai._upload_audio = lambda c, h, p: "https://up"
            aai._create_transcript = lambda c, h, url, **_k: "tid-priv"
            aai._get_transcript = lambda c, h, tid: _completed_raw(
                text_a=secret_text, text_b="вторая секретная")
            aai.transcribe_diarize_wav(self._wav, sleep=lambda _s: None)
        finally:
            root.removeHandler(cap)
            root.setLevel(old_level)
        blob = "\n".join(cap.records)
        self.assertNotIn(key_val, blob, "ключ AAI просочился в лог")
        self.assertNotIn(secret_text, blob, "текст реплики просочился в лог")
        self.assertNotIn("вторая секретная", blob)
        # метаданные при этом ЕСТЬ (число спикеров/слов/длительность)
        self.assertTrue(any("utterances" in r for r in cap.records))

    def test_error_body_truncated_to_80(self):
        import httpx
        long_name_body = "X" * 50 + "СЕКРЕТНОЕ-ИМЯ-УЧАСТНИКА" + "Y" * 200

        class _Resp:
            status_code = 422
            text = long_name_body

        class _Cli:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise httpx.HTTPStatusError("bad", response=_Resp())

        # подменяем httpx.Client на клиент, чей upload бросает 422
        saved = aai.httpx.Client
        aai.httpx.Client = _Cli
        try:
            with self.assertRaises(aai.AssemblyAIError) as ctx:
                aai.transcribe_diarize_wav(self._wav, sleep=lambda _s: None)
        finally:
            aai.httpx.Client = saved
        msg = str(ctx.exception)
        # тело обрезано до 80 символов → длинный хвост с именем не весь влез
        self.assertLessEqual(len(msg), 100)  # "HTTP 422: " + 80
        self.assertNotIn("Y" * 200, msg)


# ─────────────── speech_bounds: форма AssemblyAI ───────────────

class TestSpeechBoundsAAI(unittest.TestCase):
    def test_aai_words_shape(self):
        raw = _completed_raw()
        # words: start 1000..end 8000 → (1000, 8000)
        self.assertEqual(ptg.speech_bounds_ms_from_raw_json(raw), (1000, 8000))

    def test_aai_utterances_fallback_when_no_words(self):
        raw = {"utterances": [
            {"speaker": "A", "start": 2000, "end": 5000, "text": "a"},
            {"speaker": "B", "start": 9000, "end": 11000, "text": "b"},
        ]}
        self.assertEqual(ptg.speech_bounds_ms_from_raw_json(raw), (2000, 11000))

    def test_aai_clean_speech(self):
        raw = _completed_raw()
        self.assertEqual(ptg.clean_speech_ms_from_raw_json(raw), 7000)  # 8000-1000

    def test_speechmatics_shape_unbroken(self):
        sm = {"results": [
            {"type": "word", "start_time": 5.0, "end_time": 5.4,
             "alternatives": [{"content": "a"}]},
            {"type": "word", "start_time": 60.0, "end_time": 61.0,
             "alternatives": [{"content": "b"}]},
        ]}
        self.assertEqual(ptg.speech_bounds_ms_from_raw_json(sm), (5000, 61000))

    def test_aai_garbage_returns_none(self):
        self.assertIsNone(ptg.speech_bounds_ms_from_raw_json({"words": "nope"}))
        self.assertIsNone(ptg.speech_bounds_ms_from_raw_json({"words": [{"x": 1}]}))


# ─────────────── finalize: ветка/хелперы (best-effort) ───────────────

def _try_load_finalize():
    """Грузим finalize-meeting.py; тяжёлые транзит-импорты → None (тест скипнется)."""
    spec = importlib.util.spec_from_file_location(
        "finalize_meeting_aai", str(_NOTARY / "finalize-meeting.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_finalize = _try_load_finalize()


@unittest.skipIf(_finalize is None, "finalize-meeting.py не импортируется под маком (тяжёлые deps)")
class TestFinalizeBranch(unittest.TestCase):
    def test_stt_backend_assemblyai(self):
        os.environ["STT_BACKEND"] = "assemblyai"
        try:
            self.assertEqual(_finalize._stt_backend(), "assemblyai")
        finally:
            os.environ.pop("STT_BACKEND", None)

    def test_is_external_stt(self):
        self.assertTrue(_finalize._is_external_stt("speechmatics"))
        self.assertTrue(_finalize._is_external_stt("assemblyai"))
        self.assertFalse(_finalize._is_external_stt("whisper_pyannote"))

    def test_stt_id_meta_key(self):
        self.assertEqual(_finalize._stt_id_meta_key("speechmatics"), "speechmatics_job_id")
        self.assertEqual(_finalize._stt_id_meta_key("assemblyai"), "assemblyai_transcript_id")
        self.assertEqual(_finalize._stt_id_meta_key("whisper_pyannote"), "")

    def test_run_assemblyai_happy_path(self):
        # Подменяем сетевой клиент: _run_assemblyai импортирует из lib.assemblyai_client
        saved = aai.transcribe_diarize_wav
        Result = aai.TranscriptionResult
        aai.transcribe_diarize_wav = lambda wav, **k: Result(
            utterances=[aai.Utterance("A", 0.0, 1.0, "p1"),
                        aai.Utterance("B", 1.0, 2.0, "p2")],
            audio_duration_s=42.0, detected_language="ru",
            raw_json=_completed_raw(), job_id="tid-fin",
        )
        try:
            turns, extra, res = _finalize._run_assemblyai(
                "/tmp/x.wav", logging.getLogger("t"))
        finally:
            aai.transcribe_diarize_wav = saved
        self.assertEqual(extra["stt_label"], "assemblyai-universal-3-pro")
        self.assertEqual(extra["assemblyai_transcript_id"], "tid-fin")
        self.assertEqual(res.job_id, "tid-fin")
        self.assertEqual([t.speaker for t in turns], ["SPEAKER_00", "SPEAKER_01"])


# ─────── РИСК3: сторож недельных трат и боевой движок (ход3 цикла) ───────

class TestWeeklyGuardCoversActiveBackend(unittest.TestCase):
    """РИСК3 закрыт (umnyi-protokol-assemblyai cost-guard follow-up): сторож теперь
    учитывает И speechmatics (jobs API), И assemblyai (локальный леджер). Поэтому при
    боевом STT_BACKEND=assemblyai предупреждения «движок не покрыт» БОЛЬШЕ НЕТ —
    оно осталось бы только для НОВОГО внешнего движка вне `_TALLIED_STT_BACKENDS`."""

    def _run(self, backend):
        import stt_weekly_guard as g  # top-level модуль notary (sys.path уже включает _NOTARY)
        pushes = []

        def pusher(msg, *, dedupe_key=None):
            pushes.append((msg, dedupe_key))

        saved = os.environ.get("STT_BACKEND")
        if backend is None:
            os.environ.pop("STT_BACKEND", None)
        else:
            os.environ["STT_BACKEND"] = backend
        kpath = Path(tempfile.gettempdir()) / "nonexistent-stt-killswitch.flag"
        try:
            res = g.run_guard(
                now=g._now_utc(),
                failed_dir=Path(tempfile.gettempdir()) / "no-such-failed-dir-xyz",
                warn_h=10.0, block_h=15.0,
                killswitch_path=kpath,
                jobs_fetcher=lambda now: [],   # SM ничего не тратил
                pusher=pusher,
                dry_run=True,
            )
        finally:
            if saved is None:
                os.environ.pop("STT_BACKEND", None)
            else:
                os.environ["STT_BACKEND"] = saved
        return res, pushes

    def test_assemblyai_active_no_warning_now_tallied(self):
        # РИСК3 закрыт: assemblyai теперь в _TALLIED_STT_BACKENDS (учитывается через
        # локальный леджер) → предупреждение «движок не покрыт» НЕ шлётся.
        res, pushes = self._run("assemblyai")
        self.assertNotIn("untallied-backend-warning", res["actions"])
        self.assertFalse(any("РИСК3" in m for m, _ in pushes),
                         "после закрытия РИСК3 предупреждение о непокрытом движке не шлём")

    def test_speechmatics_active_warns_now_untallied(self):
        # Speechmatics убран из _TALLIED (решение владельца 2026-06-14, аккаунт общий).
        # Если когда-то переключат STT_BACKEND обратно на speechmatics — сторож НЕ молча
        # предупредит, что его расход не покрыт счётом (движок switchable, но не трекается).
        res, pushes = self._run("speechmatics")
        self.assertIn("untallied-backend-warning", res["actions"])
        self.assertTrue(any("speechmatics" in m for m, _ in pushes))

    def test_whisper_active_no_warning(self):
        res, pushes = self._run("whisper_pyannote")
        self.assertNotIn("untallied-backend-warning", res["actions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
