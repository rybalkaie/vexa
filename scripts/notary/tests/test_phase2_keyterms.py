"""Тесты Ф2 плана umnyi-protokol-assemblyai — доменный словарь keyterms_prompt.

Закрепляют контракт REQ S4 на фикстурах/моках (боевая сеть AAI не дёргается —
ключа на маке headless нет). Что доказываем без живого ключа:
  • build_keyterms_prompt: нормализация, дедуп (case-insensitive), отброс фраз
    >6 слов, потолок ≤1000, сохранение оригинального регистра/порядка — S4;
  • источники словаря: имена ростера серии + канонические из глоссария компании
    (YAML/встроенный fallback) + ручные нишевые термины серии (файл) — S4;
  • передача `keyterms_prompt` в тело create-запроса; взаимоисключение с `prompt`;
    пусто/None → поле НЕ передаётся (регресс Ф1) — S4;
  • проводка по серии: transcribe_diarize_wav → _create_transcript; _run_assemblyai
    собирает словарь по series_slug — S4 «достижимость из реального триггера»;
  • бенч 1123: Sonix/Playwright/Сбер из словаря серии доходят до keyterms_prompt;
  • приватность (РИСК2): имена/keyterms НЕ попадают в логи (только число терминов).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase2_keyterms -v
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


def _stub_missing(*names):
    for name in names:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)


# yaml НЕ стабим намеренно при наличии: тесты используют пустые временные каталоги
# контекста, поэтому реальный pyyaml всё равно не найдёт файлов → []. Если pyyaml
# отсутствует — _stub_missing подставит заглушку (тот же пустой результат).
_stub_missing("yaml", "requests", "pymorphy3", "torch", "numpy")


def _install_rich_httpx_stub():
    """httpx-заглушка с Client-контекстменеджером и классами-исключениями (как в Ф1)."""
    if "httpx" in sys.modules and getattr(sys.modules["httpx"], "_rich_stub", False):
        return
    if "httpx" in sys.modules and hasattr(sys.modules["httpx"], "Client") \
            and not isinstance(sys.modules["httpx"], types.ModuleType):
        return
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

import lib.assemblyai_client as aai  # noqa: E402
import lib.glossary as glossary_mod  # noqa: E402
import lib.keyterms as kt  # noqa: E402
import lib.series_roster as series_roster  # noqa: E402


def _completed_raw(text_a="реплика один", text_b="реплика два"):
    return {
        "id": "tid-fixture-2",
        "status": "completed",
        "language_code": "ru",
        "audio_duration": 75,
        "text": f"{text_a} {text_b}",
        "utterances": [
            {"speaker": "A", "start": 1000, "end": 4000, "text": text_a},
            {"speaker": "B", "start": 4200, "end": 8000, "text": text_b},
        ],
        "words": [{"text": "реплика", "start": 1000, "end": 1500, "speaker": "A"}],
    }


# ─────────────────────────── S4: build_keyterms_prompt ───────────────────────────

class TestBuildKeytermsPrompt(unittest.TestCase):
    def test_dedup_case_insensitive_keeps_first_case(self):
        out = kt.build_keyterms_prompt(["Sonix", "SONIX", "  sonix ", "Playwright"])
        self.assertEqual(out, ["Sonix", "Playwright"])  # первое вхождение регистра

    def test_drops_empty_and_punctuation_only(self):
        out = kt.build_keyterms_prompt(["", "   ", "!!!", "---", "Anzhee"])
        self.assertEqual(out, ["Anzhee"])

    def test_drops_phrases_over_six_words(self):
        six = "один два три четыре пять шесть"
        seven = "один два три четыре пять шесть семь"
        out = kt.build_keyterms_prompt([six, seven])
        self.assertIn(six, out)
        self.assertNotIn(seven, out)  # >6 слов отброшено целиком, не обрезано

    def test_collapses_internal_whitespace(self):
        out = kt.build_keyterms_prompt(["палетное   хранение", "Space\tProjector"])
        self.assertEqual(out, ["палетное хранение", "Space Projector"])

    def test_caps_at_1000(self):
        terms = [f"термин{i}" for i in range(1100)]
        out = kt.build_keyterms_prompt(terms)
        self.assertEqual(len(out), kt.MAX_KEYTERMS)
        self.assertEqual(out[0], "термин0")     # порядок сохранён
        self.assertEqual(out[-1], "термин999")  # обрезано на 1000-м

    def test_non_string_coerced_or_dropped(self):
        out = kt.build_keyterms_prompt([123, None, 0, "x"])
        self.assertIn("123", out)
        self.assertIn("x", out)
        self.assertNotIn("", out)   # None/0 → "" → выброшены

    def test_empty_input(self):
        self.assertEqual(kt.build_keyterms_prompt([]), [])
        self.assertEqual(kt.build_keyterms_prompt(None), [])


# ─────────────────────── S4: ручные нишевые термины серии ───────────────────────

class TestManualKeyterms(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._saved = os.environ.get("MEETING_NOTARY_KEYTERMS_DIR")
        os.environ["MEETING_NOTARY_KEYTERMS_DIR"] = self._tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_KEYTERMS_DIR", None)
        else:
            os.environ["MEETING_NOTARY_KEYTERMS_DIR"] = self._saved

    def _write(self, slug, content):
        (Path(self._tmp) / f"{slug}.txt").write_text(content, encoding="utf-8")

    def test_reads_lines_skips_comments_and_blanks(self):
        self._write("series-tech-x", "# нишевые термины\nSonix\nPlaywright\n\n  Сбер  \n")
        out = kt.manual_keyterms_for_series("series-tech-x")
        self.assertEqual(out, ["Sonix", "Playwright", "Сбер"])

    def test_missing_file_is_graceful(self):
        self.assertEqual(kt.manual_keyterms_for_series("no-such-series"), [])

    def test_empty_slug(self):
        self.assertEqual(kt.manual_keyterms_for_series(""), [])
        self.assertEqual(kt.manual_keyterms_for_series(None), [])


# ─────────────────────── S4: glossary_keyterms (канон + fallback) ───────────────────────

class TestGlossaryKeyterms(unittest.TestCase):
    def test_canonicals_from_yaml_entries(self):
        saved = glossary_mod.context_knowledge.load_glossary
        glossary_mod.context_knowledge.load_glossary = lambda company: [
            {"canonical": "Sonix"}, {"canonical": "Playwright"},
            {"canonical": "  "}, {"no_canonical": "x"},  # мусор отброшен
        ]
        try:
            out = glossary_mod.glossary_keyterms("anzhee")
        finally:
            glossary_mod.context_knowledge.load_glossary = saved
        self.assertEqual(out, ["Sonix", "Playwright"])

    def test_builtin_fallback_when_no_yaml(self):
        saved = glossary_mod.context_knowledge.load_glossary
        glossary_mod.context_knowledge.load_glossary = lambda company: []
        try:
            out = glossary_mod.glossary_keyterms(None)
        finally:
            glossary_mod.context_knowledge.load_glossary = saved
        self.assertIn("Anzhee", out)
        self.assertIn("Dream Story", out)
        # каждый встроенный термин ≤6 слов (жёсткий лимит AAI)
        for t in glossary_mod.BUILTIN_KEYTERMS:
            self.assertLessEqual(len(t.split()), kt.MAX_WORDS_PER_PHRASE, t)


# ─────────────────────── S4: сбор словаря серии (интеграция) ───────────────────────

class TestCollectSeriesKeyterms(unittest.TestCase):
    """Изолируем от реальных клонов/реестра: пустые временные каталоги контекста и
    реестра → YAML-источники возвращают [] (встроенные fallback'и работают)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in (
            "MEETING_NOTARY_CONTEXT_DIR", "MEETING_NOTARY_KEYTERMS_DIR",
            "MEETING_NOTARY_REGISTRY_DIR")}
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._tmp
        os.environ["MEETING_NOTARY_KEYTERMS_DIR"] = self._tmp
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = self._tmp

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_roster_names_plus_builtin_brands(self):
        # Реальный статический ростер Anzhee-координации (5 имён) + встроенные бренды.
        out = kt.collect_keyterms_prompt(series_roster.ANZHEE_COORDINATION_SLUG)
        self.assertIn("Мария Михина", out)     # имя из ростера серии
        self.assertIn("Сона Енгибарян", out)
        self.assertIn("Anzhee", out)            # бренд из встроенного глоссария
        # лимиты соблюдены
        self.assertLessEqual(len(out), kt.MAX_KEYTERMS)
        for t in out:
            self.assertLessEqual(len(t.split()), kt.MAX_WORDS_PER_PHRASE, t)

    def test_manual_niche_terms_included(self):
        slug = "series-tech-bench-1123"
        (Path(self._tmp) / f"{slug}.txt").write_text(
            "# бенч 1123 — что путал Speechmatics\nSonix\nPlaywright\nСбер\n",
            encoding="utf-8")
        out = kt.collect_keyterms_prompt(slug)
        # Критерий приёмки Ф2: термины, что путал SM, есть в собранном словаре.
        self.assertIn("Sonix", out)
        self.assertIn("Playwright", out)
        self.assertIn("Сбер", out)

    def test_extra_terms_merged_and_deduped(self):
        out = kt.collect_keyterms_prompt(
            None, company=None, extra_terms=["Anzhee", "НовыйТермин"])
        self.assertIn("НовыйТермин", out)
        # Anzhee встроенный + extra → один (дедуп)
        self.assertEqual(out.count("Anzhee"), 1)


# ─────────────────── S4: передача keyterms_prompt в _create_transcript ───────────────────

class _RecordingResp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "tid-rec"}


class _RecordingClient:
    def __init__(self):
        self.last_json = None

    def post(self, url, headers=None, json=None):
        self.last_json = json
        return _RecordingResp()


class TestCreateTranscriptBody(unittest.TestCase):
    def test_keyterms_added_to_body(self):
        cli = _RecordingClient()
        aai._create_transcript(cli, {"authorization": "k"}, "https://up",
                               keyterms_prompt=["Anzhee", "Sonix", "Сбер"])
        self.assertEqual(cli.last_json["keyterms_prompt"], ["Anzhee", "Sonix", "Сбер"])
        # S4: keyterms и prompt взаимоисключающи — prompt мы не шлём НИКОГДА.
        self.assertNotIn("prompt", cli.last_json)
        # базовый конфиг Ф1 не сломан
        self.assertEqual(cli.last_json["speech_models"], [aai.SPEECH_MODEL])
        self.assertTrue(cli.last_json["speaker_labels"])

    def test_no_keyterms_field_when_empty_or_none(self):
        for kt_arg in (None, []):
            cli = _RecordingClient()
            aai._create_transcript(cli, {"authorization": "k"}, "https://up",
                                   keyterms_prompt=kt_arg)
            self.assertNotIn("keyterms_prompt", cli.last_json,
                             f"пустой keyterms ({kt_arg!r}) не должен добавлять поле (регресс Ф1)")

    def test_keyterms_body_is_copy_not_alias(self):
        src = ["Anzhee"]
        cli = _RecordingClient()
        aai._create_transcript(cli, {"authorization": "k"}, "https://up", keyterms_prompt=src)
        src.append("mutated")
        self.assertEqual(cli.last_json["keyterms_prompt"], ["Anzhee"])  # тело не зависит от мутации источника


# ─────────────────── S4: проводка через transcribe_diarize_wav ───────────────────

class _OrchBase(unittest.TestCase):
    def setUp(self):
        os.environ["ASSEMBLYAI_API_KEY"] = "test-secret-KEY-do-not-log"
        self._saved_backoff = aai.RETRY_BACKOFF_S
        aai.RETRY_BACKOFF_S = 0.0
        self._saved = {n: getattr(aai, n) for n in
                       ("_upload_audio", "_create_transcript", "_get_transcript")}
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


class TestTranscribePassesKeyterms(_OrchBase):
    def test_keyterms_flow_to_create_on_new_submit(self):
        captured = {}
        aai._upload_audio = lambda c, h, p: "https://up"

        def fake_create(c, h, url, *, keyterms_prompt=None):
            captured["kt"] = keyterms_prompt
            return "tid-1"

        aai._create_transcript = fake_create
        aai._get_transcript = lambda c, h, tid: _completed_raw()
        aai.transcribe_diarize_wav(
            self._wav, keyterms_prompt=["Anzhee", "Сбер"], sleep=lambda _s: None)
        self.assertEqual(captured["kt"], ["Anzhee", "Сбер"])

    def test_reuse_ignores_keyterms_no_create(self):
        def boom_create(*a, **k):
            raise AssertionError("create не должен вызываться при reuse")

        aai._create_transcript = boom_create
        aai._get_transcript = lambda c, h, tid: _completed_raw()
        res = aai.transcribe_diarize_wav(
            self._wav, existing_transcript_id="tid-reuse",
            keyterms_prompt=["Anzhee"], sleep=lambda _s: None)
        self.assertEqual(res.job_id, "tid-reuse")  # reuse прошёл, keyterms не помешали


# ─────────────────────── РИСК2: приватность (имена в keyterms) ───────────────────────

class _CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            self.records.append(self.format(record))
        except Exception:
            self.records.append(str(record.msg))


class TestKeytermsPrivacy(_OrchBase):
    def test_keyterm_names_not_logged_by_client(self):
        secret_name = "Сверхсекретный Участник Увольнение"
        cap = _CaptureLogs()
        cap.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(cap)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            aai._upload_audio = lambda c, h, p: "https://up"
            aai._create_transcript = lambda c, h, url, *, keyterms_prompt=None: "tid-priv"
            aai._get_transcript = lambda c, h, tid: _completed_raw()
            aai.transcribe_diarize_wav(
                self._wav, keyterms_prompt=[secret_name, "Anzhee"],
                sleep=lambda _s: None)
        finally:
            root.removeHandler(cap)
            root.setLevel(old_level)
        blob = "\n".join(cap.records)
        self.assertNotIn(secret_name, blob, "имя из keyterms просочилось в лог клиента")


# ─────────────── finalize: _run_assemblyai собирает словарь по серии ───────────────

def _try_load_finalize():
    spec = importlib.util.spec_from_file_location(
        "finalize_meeting_kt", str(_NOTARY / "finalize-meeting.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_finalize = _try_load_finalize()


@unittest.skipIf(_finalize is None, "finalize-meeting.py не импортируется под маком (тяжёлые deps)")
class TestRunAssemblyaiKeyterms(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in (
            "MEETING_NOTARY_CONTEXT_DIR", "MEETING_NOTARY_KEYTERMS_DIR",
            "MEETING_NOTARY_REGISTRY_DIR")}
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._tmp
        os.environ["MEETING_NOTARY_KEYTERMS_DIR"] = self._tmp
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = self._tmp

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_collects_keyterms_by_series_and_passes_to_transcribe(self):
        slug = "series-tech-bench-1123"
        (Path(self._tmp) / f"{slug}.txt").write_text("Sonix\nPlaywright\nСбер\n", encoding="utf-8")

        captured = {}
        saved = aai.transcribe_diarize_wav
        Result = aai.TranscriptionResult

        def fake_transcribe(wav, **k):
            captured["kt"] = k.get("keyterms_prompt")
            return Result(
                utterances=[aai.Utterance("A", 0.0, 1.0, "p1")],
                audio_duration_s=10.0, detected_language="ru",
                raw_json=_completed_raw(), job_id="tid-fin")

        aai.transcribe_diarize_wav = fake_transcribe
        cap = _CaptureLogs()
        cap.setFormatter(logging.Formatter("%(message)s"))
        log = logging.getLogger("test-run-aai")
        log.addHandler(cap)
        log.setLevel(logging.DEBUG)
        try:
            _finalize._run_assemblyai("/tmp/x.wav", log, series_slug=slug)
        finally:
            aai.transcribe_diarize_wav = saved
            log.removeHandler(cap)

        # Бенч-термины серии дошли до transcribe → keyterms_prompt.
        self.assertIsNotNone(captured["kt"])
        self.assertIn("Sonix", captured["kt"])
        self.assertIn("Playwright", captured["kt"])
        self.assertIn("Сбер", captured["kt"])
        # РИСК2: в логе только ЧИСЛО терминов, не сами термины.
        blob = "\n".join(cap.records)
        self.assertTrue(any("терминов" in r for r in cap.records), "нет метаданных о числе терминов")
        self.assertNotIn("Sonix", blob, "термин словаря просочился в лог _run_assemblyai")

    def test_no_series_slug_graceful_empty(self):
        captured = {}
        saved = aai.transcribe_diarize_wav
        Result = aai.TranscriptionResult

        def fake_transcribe(wav, **k):
            captured["kt"] = k.get("keyterms_prompt")
            return Result(utterances=[], audio_duration_s=0.0,
                          detected_language="ru", raw_json={}, job_id="tid-x")

        aai.transcribe_diarize_wav = fake_transcribe
        try:
            _finalize._run_assemblyai("/tmp/x.wav", logging.getLogger("t"), series_slug=None)
        finally:
            aai.transcribe_diarize_wav = saved
        # Нет серии → словарь = только встроенные бренды (company=None → builtin),
        # поле всё равно непусто, но без падения; главное — вызов прошёл.
        self.assertIsInstance(captured["kt"], list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
