"""Ф6 плана «службы на последние модели» (17.10.2026): модели словом уровня + явное усилие.

План: `~/Projects/me/plans/2026-10-16-server-services-latest-models.md`, Фаза 6 (Ватсон).

Покрывает:
  - `claude_cli.resolve_effort` / `model_args`: opus→high, sonnet→high, haiku→low;
    переменная `NOTARY_CLAUDE_EFFORT_<УРОВЕНЬ>`; явный `effort`; `none`; мусор →
    умолчание уровня; уровень узнаётся и по номеру; неизвестная модель → без флага.
  - Все три функции вызова кладут `--model` и `--effort` в командную строку клиента.
  - Отказ «модель недоступна» (404 `model_not_found` / 529 `overloaded`) несёт коды в
    `ClaudeCliFailed`; `is_model_unavailable` отличает его от прочих сбоев.
  - `generate_protocol`: основная модель недоступна → один проход на резервной;
    прочий отказ — как до Ф6, без резервной; резервная тоже упала — понятная ошибка.
  - Места вызова: умолчания — слова прежнего уровня; бывшие вызовы без модели
    (сопоставление имён, разбор уточнения спикеров, патч, разбор ответа на вечерний
    блок) передают `sonnet` — уровень, который они фактически получали до 17.10.
  - Сторож: в коде нотариуса нет номеров моделей Claude строковыми значениями.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python -m unittest tests.test_f6_models_by_tier -v
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_NOTARY))
sys.path.insert(0, str(_SCRIPTS))

from lib import claude_cli  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402
from lib.align import AlignedTurn  # noqa: E402
from lib.claude_cli import (  # noqa: E402
    ClaudeCliFailed,
    ClaudeCliStreamResult,
    ClaudeCliTimeout,
)

_EFFORT_ENV = ("NOTARY_CLAUDE_EFFORT_OPUS", "NOTARY_CLAUDE_EFFORT_SONNET", "NOTARY_CLAUDE_EFFORT_HAIKU")


def _env_without(*names: str) -> dict:
    """Копия окружения без указанных переменных (для `mock.patch.dict(..., clear=True)`)."""
    return {k: v for k, v in os.environ.items() if k not in names}


# Отказ клиента при недоступной модели — по форме настоящего ответа CLI 2.1.300
# (проба 17.10.2026 на сервере: `--model claude-opus-9-9`, режим stream-json).
_STREAM_404 = "\n".join([
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps({"type": "assistant", "error": "model_not_found", "is_api_error_message": True,
                "api_error_status": 404, "message": {"content": [{"type": "text", "text": "x"}]}}),
    json.dumps({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
                "api_error_status": 404,
                "result": "There's an issue with the selected model (claude-opus-9-9)."}),
])
_STREAM_529 = "\n".join([
    json.dumps({"type": "assistant", "error": "overloaded", "is_api_error_message": True,
                "api_error_status": 529}),
    json.dumps({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
                "api_error_status": 529, "result": "Overloaded"}),
])
_STREAM_AUTH = "\n".join([
    json.dumps({"type": "assistant", "error": "authentication_failed", "is_api_error_message": True}),
    json.dumps({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
                "result": "Not logged in · Please run /login"}),
])
_STREAM_OK = json.dumps({"type": "result", "subtype": "success", "result": "ок", "stop_reason": "end_turn",
                         "usage": {"input_tokens": 1, "output_tokens": 1}})


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# Усилие по уровню модели
# ---------------------------------------------------------------------------
class TestResolveEffort(unittest.TestCase):

    def setUp(self):
        p = mock.patch.dict(os.environ, _env_without(*_EFFORT_ENV), clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_defaults_by_tier(self):
        self.assertEqual(claude_cli.resolve_effort("opus"), "high")
        self.assertEqual(claude_cli.resolve_effort("sonnet"), "high")
        self.assertEqual(claude_cli.resolve_effort("haiku"), "low")

    def test_tier_recognised_by_number(self):
        self.assertEqual(claude_cli.resolve_effort("claude-opus-4-8"), "high")
        self.assertEqual(claude_cli.resolve_effort("claude-sonnet-5-5"), "high")
        self.assertEqual(claude_cli.resolve_effort("claude-haiku-4-5-20251001"), "low")
        self.assertEqual(claude_cli.resolve_effort("opus[1m]"), "high")
        self.assertEqual(claude_cli.resolve_effort(" Sonnet "), "high")

    def test_unknown_model_no_flag(self):
        for m in (None, "", "opusplan", "claude-fable-5", "default", "claude-3-opus-20240229"):
            self.assertIsNone(claude_cli.resolve_effort(m), m)

    def test_env_override_per_tier(self):
        with mock.patch.dict(os.environ, {"NOTARY_CLAUDE_EFFORT_OPUS": "xhigh"}):
            self.assertEqual(claude_cli.resolve_effort("opus"), "xhigh")
            self.assertEqual(claude_cli.resolve_effort("sonnet"), "high")  # чужой уровень не тронут
        with mock.patch.dict(os.environ, {"NOTARY_CLAUDE_EFFORT_HAIKU": "none"}):
            self.assertIsNone(claude_cli.resolve_effort("haiku"))

    def test_env_garbage_falls_back_to_tier_default(self):
        with mock.patch.dict(os.environ, {"NOTARY_CLAUDE_EFFORT_SONNET": "очень"}), \
                self.assertLogs(claude_cli.logger, level="WARNING"):
            self.assertEqual(claude_cli.resolve_effort("sonnet"), "high")

    def test_explicit_effort_wins(self):
        with mock.patch.dict(os.environ, {"NOTARY_CLAUDE_EFFORT_OPUS": "low"}):
            self.assertEqual(claude_cli.resolve_effort("opus", "max"), "max")
        self.assertIsNone(claude_cli.resolve_effort("opus", "none"))
        self.assertEqual(claude_cli.resolve_effort("haiku", ""), "low")  # пусто = по уровню
        with self.assertLogs(claude_cli.logger, level="WARNING"):
            self.assertEqual(claude_cli.resolve_effort("opus", "bogus"), "high")

    def test_model_args(self):
        self.assertEqual(claude_cli.model_args("opus"), ["--model", "opus", "--effort", "high"])
        self.assertEqual(claude_cli.model_args("haiku", "medium"),
                         ["--model", "haiku", "--effort", "medium"])
        self.assertEqual(claude_cli.model_args(None), [])
        self.assertEqual(claude_cli.model_args("opusplan"), ["--model", "opusplan"])


# ---------------------------------------------------------------------------
# Командная строка клиента у всех трёх функций вызова
# ---------------------------------------------------------------------------
class TestCommandLine(unittest.TestCase):

    def setUp(self):
        p_env = mock.patch.dict(os.environ, _env_without(*_EFFORT_ENV, "CLAUDE_MIN_TIMEOUT"), clear=True)
        p_env.start()
        self.addCleanup(p_env.stop)
        p_which = mock.patch.object(claude_cli.shutil, "which", return_value="/usr/bin/claude")
        p_which.start()
        self.addCleanup(p_which.stop)

    def _run(self, fn, stdout, **kwargs):
        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = list(cmd)
            return _completed(stdout)

        with mock.patch.object(claude_cli.subprocess, "run", side_effect=_fake_run):
            fn("prompt", **kwargs)
        return captured["cmd"]

    def _assert_pair(self, cmd, flag, value):
        self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index(flag) + 1], value)

    def test_print_text(self):
        cmd = self._run(claude_cli.call_claude_print, "ok", model="sonnet")
        self._assert_pair(cmd, "--model", "sonnet")
        self._assert_pair(cmd, "--effort", "high")

    def test_print_json(self):
        cmd = self._run(claude_cli.call_claude_print_json, json.dumps({"result": "ok", "usage": {}}),
                        model="haiku")
        self._assert_pair(cmd, "--model", "haiku")
        self._assert_pair(cmd, "--effort", "low")

    def test_print_stream_json(self):
        cmd = self._run(claude_cli.call_claude_print_stream_json, _STREAM_OK, model="opus")
        self._assert_pair(cmd, "--model", "opus")
        self._assert_pair(cmd, "--effort", "high")
        self.assertIn("--verbose", cmd)

    def test_env_and_explicit_effort_reach_cli(self):
        with mock.patch.dict(os.environ, {"NOTARY_CLAUDE_EFFORT_OPUS": "medium"}):
            cmd = self._run(claude_cli.call_claude_print_stream_json, _STREAM_OK, model="opus")
        self._assert_pair(cmd, "--effort", "medium")
        cmd = self._run(claude_cli.call_claude_print, "ok", model="opus", effort="low")
        self._assert_pair(cmd, "--effort", "low")

    def test_no_model_no_flags(self):
        cmd = self._run(claude_cli.call_claude_print, "ok")
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--effort", cmd)


# ---------------------------------------------------------------------------
# «Модель недоступна» — коды отказа в исключении
# ---------------------------------------------------------------------------
class TestModelUnavailable(unittest.TestCase):

    def setUp(self):
        p_which = mock.patch.object(claude_cli.shutil, "which", return_value="/usr/bin/claude")
        p_which.start()
        self.addCleanup(p_which.stop)

    def _fail(self, stdout, returncode):
        with mock.patch.object(claude_cli.subprocess, "run", return_value=_completed(stdout, returncode)):
            with self.assertRaises(ClaudeCliFailed) as ctx:
                claude_cli.call_claude_print_stream_json("prompt", model="opus")
        return ctx.exception

    def test_404_nonzero_exit(self):
        e = self._fail(_STREAM_404, 1)
        self.assertEqual(e.api_error_status, 404)
        self.assertEqual(e.error_code, "model_not_found")
        self.assertTrue(claude_cli.is_model_unavailable(e))
        self.assertIn("model_not_found", str(e))  # строка в журнал — как раньше

    def test_404_is_error_exit_zero(self):
        e = self._fail(_STREAM_404, 0)
        self.assertEqual(e.api_error_status, 404)
        self.assertTrue(claude_cli.is_model_unavailable(e))

    def test_529_overloaded(self):
        e = self._fail(_STREAM_529, 1)
        self.assertEqual(e.error_code, "overloaded")
        self.assertTrue(claude_cli.is_model_unavailable(e))

    def test_other_failures_are_not_unavailable(self):
        e = self._fail(_STREAM_AUTH, 1)
        self.assertEqual(e.error_code, "authentication_failed")
        self.assertFalse(claude_cli.is_model_unavailable(e))
        self.assertFalse(claude_cli.is_model_unavailable(ClaudeCliFailed("exit 1")))
        self.assertFalse(claude_cli.is_model_unavailable(ClaudeCliTimeout("timeout")))
        self.assertFalse(claude_cli.is_model_unavailable(RuntimeError("x")))

    def test_stream_error_codes_empty(self):
        self.assertEqual(claude_cli.stream_error_codes(""), (None, None))
        self.assertEqual(claude_cli.stream_error_codes("не json"), (None, None))


# ---------------------------------------------------------------------------
# Протокол: основная модель недоступна → резервная
# ---------------------------------------------------------------------------
_FAKE_PROTOCOL = (
    "#протоколвстречи 14.10.2026\n\n"
    "**Встреча:** Координация.\n\n"
    "**Длительность:** 25 мин\n\n"
    "**Участники:** Анна Иванова, Пётр Петров\n\n"
    "**Транскрипт:** [2026-10-14.md](2026-10-14.md)\n\n"
    "---\n\n"
    "## 1) Поставки\n\n"
    "▪️ Контейнер на таможне.\n"
)
_META = {"series": "f6-test-series", "date": "2026-10-14", "duration": 25,
         "expectedParticipants": ["Анна Иванова", "Пётр Петров"],
         "participants": ["Анна Иванова", "Пётр Петров"]}


def _unavailable(status=404, code="model_not_found"):
    return ClaudeCliFailed("claude --print (stream-json) exit=1", api_error_status=status, error_code=code)


class TestProtocolFallbackOnUnavailable(unittest.TestCase):

    def setUp(self):
        p_log = mock.patch.object(lp.run_log, "append_event")
        self.run_log = p_log.start()
        self.addCleanup(p_log.stop)

    def _generate(self, primary_exc, *, fallback="sonnet", fallback_exc=None):
        calls = []

        def _fake(user_prompt, *, system, timeout, model, max_thinking_tokens=None):
            calls.append(model)
            if model == lp.PROTOCOL_GEN_MODEL:
                raise primary_exc
            if fallback_exc is not None:
                raise fallback_exc
            return ClaudeCliStreamResult(_FAKE_PROTOCOL, "end_turn", 0.0, 0, 0, {})

        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", fallback), \
                mock.patch.object(lp, "call_claude_print_stream_json", side_effect=_fake):
            out = lp.generate_protocol("т", _META, method_text="М", meeting_sid="f6")
        return out, calls

    def test_not_found_degrades_to_fallback(self):
        with self.assertLogs(lp.logger, level="WARNING") as logctx:
            out, calls = self._generate(_unavailable())
        self.assertTrue(out.startswith("#протоколвстречи"))
        self.assertEqual(calls, [lp.PROTOCOL_GEN_MODEL, "sonnet"])
        self.assertIn("reason=model-unavailable", "\n".join(logctx.output))
        events = [(c.args[1], c.kwargs.get("reason")) for c in self.run_log.call_args_list]
        self.assertIn(("degraded", "primary-unavailable"), events)
        # итоговое событие прогона — тоже «degraded» с той же причиной, модель — резервная
        last = self.run_log.call_args_list[-1]
        self.assertEqual((last.args[1], last.kwargs.get("reason"), last.kwargs.get("model")),
                         ("degraded", "primary-unavailable", "sonnet"))

    def test_overloaded_degrades_to_fallback(self):
        out, calls = self._generate(_unavailable(529, "overloaded"))
        self.assertTrue(out.startswith("#протоколвстречи"))
        self.assertEqual(calls, [lp.PROTOCOL_GEN_MODEL, "sonnet"])

    def test_other_failure_no_fallback(self):
        with self.assertRaises(lp.ProtocolGenerationError):
            self._generate(ClaudeCliFailed("exit=1", error_code="authentication_failed"))

    def test_other_failure_does_not_call_fallback(self):
        calls = []

        def _fake(user_prompt, *, system, timeout, model, max_thinking_tokens=None):
            calls.append(model)
            raise ClaudeCliFailed("exit 1")

        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", "sonnet"), \
                mock.patch.object(lp, "call_claude_print_stream_json", side_effect=_fake):
            with self.assertRaises(lp.ProtocolGenerationError):
                lp.generate_protocol("т", _META, method_text="М", meeting_sid="f6b")
        self.assertEqual(calls, [lp.PROTOCOL_GEN_MODEL])

    def test_unavailable_without_fallback_raises(self):
        with self.assertRaises(lp.ProtocolGenerationError):
            self._generate(_unavailable(), fallback="")

    def test_unavailable_fallback_same_as_primary_raises(self):
        with self.assertRaises(lp.ProtocolGenerationError):
            self._generate(_unavailable(), fallback=lp.PROTOCOL_GEN_MODEL)

    def test_fallback_also_fails_raises_with_both_reasons(self):
        with self.assertRaises(lp.ProtocolGenerationError) as ctx:
            self._generate(_unavailable(), fallback_exc=ClaudeCliFailed("fallback boom"))
        self.assertIn("недоступна", str(ctx.exception))
        self.assertIn("fallback boom", str(ctx.exception))


# ---------------------------------------------------------------------------
# Места вызова: слова прежнего уровня
# ---------------------------------------------------------------------------
class TestCallSiteDefaults(unittest.TestCase):

    def _skip_if_env(self, name):
        if (os.environ.get(name) or "").strip():
            self.skipTest(f"{name} задан в окружении — умолчание не проверить")

    def test_llm_postprocess_constants(self):
        for env, value, expected in (
            ("PROTOCOL_GEN_MODEL", lp.PROTOCOL_GEN_MODEL, "opus"),
            ("PROTOCOL_GEN_FALLBACK_MODEL", lp.PROTOCOL_GEN_FALLBACK_MODEL, "sonnet"),
            ("PROTOCOL_REWRITE_MODEL", lp.PROTOCOL_REWRITE_MODEL, "opus"),
        ):
            with self.subTest(env=env):
                if (os.environ.get(env) or "").strip():
                    continue
                self.assertEqual(value, expected)
        self.assertEqual(lp.PROTOCOL_REVIEW_MODEL, "sonnet")
        self.assertEqual(lp.TASK_EXTRACTION_MODEL, "sonnet")
        self.assertEqual(lp.TASK_PARSE_FALLBACK_MODEL, "haiku")
        self.assertEqual(lp.STAKEHOLDER_TRACK_SYNC_MODEL, "sonnet")
        self.assertEqual(lp.CORRECTION_SUMMARY_MODEL, "haiku")
        self.assertEqual(lp.SPEAKER_MAP_MODEL, "sonnet")
        with mock.patch.dict(os.environ, _env_without("CROSS_MEMORY_CLASSIFIER_MODEL"), clear=True):
            self.assertEqual(lp._cross_classifier_model(), "haiku")

    def test_haiku_tier_modules(self):
        from lib import feedback_classify_llm, knowledge_distill, mixed_reply_split  # noqa: PLC0415
        from lib import pending_reconciler, pending_status_reply, series_memory  # noqa: PLC0415
        for env, value in (
            ("FEEDBACK_LLM_CLASSIFY_MODEL", feedback_classify_llm._CLASSIFY_MODEL),
            ("PENDING_RECONCILER_MODEL", pending_reconciler._RECONCILER_MODEL),
            ("OPEN_TASKS_SIGNIFICANCE_MODEL", series_memory._SIGNIFICANCE_MODEL),
            ("MIXED_REPLY_SPLIT_MODEL", mixed_reply_split._SPLIT_MODEL),
            ("PENDING_STATUS_INTENT_MODEL", pending_status_reply._INTENT_MODEL),
        ):
            with self.subTest(env=env):
                if (os.environ.get(env) or "").strip():
                    continue
                self.assertEqual(value, "haiku")
        with mock.patch.dict(os.environ, _env_without("KNOWLEDGE_DISTILL_MODEL"), clear=True):
            self.assertEqual(knowledge_distill._distill_model(), "haiku")

    def test_speaker_map_passes_sonnet(self):
        turns = [
            AlignedTurn(0.0, 5.0, "SPEAKER_00", "Добрый день, коллеги, начинаем планёрку по поставкам."),
            AlignedTurn(5.0, 9.0, "SPEAKER_01", "Да, у меня по контейнеру есть новости с таможни."),
        ]
        names = ["Анна Иванова", "Пётр Петров"]
        with mock.patch.dict(os.environ, _env_without("ENABLE_LLM_NAME_MAPPING"), clear=True), \
                mock.patch.object(lp, "call_claude_print", return_value="{}") as m:
            lp.map_speaker_names(turns, names, names, meeting_sid="f6-map")
        self.assertTrue(m.called)
        self.assertEqual(m.call_args.kwargs.get("model"), "sonnet")

    def test_clarify_text_parse_passes_sonnet(self):
        with mock.patch.object(lp, "call_claude_print", return_value="{}") as m:
            lp._llm_parse_text_answer(
                "первый — это Анна", ["SPEAKER_00"], {"Спикер 1": "SPEAKER_00"},
                ["Анна Иванова"], meeting_sid="f6-clar",
            )
        self.assertTrue(m.called)
        self.assertEqual(m.call_args.kwargs.get("model"), "sonnet")

    def test_protocol_patch_default_sonnet(self):
        self._skip_if_env("PROTOCOL_PATCH_MODEL")
        from lib import protocol_patch as pp  # noqa: PLC0415
        self.assertEqual(pp._PATCH_MODEL, "sonnet")
        with mock.patch.object(pp, "call_claude_print", return_value='{"ops": []}') as m:
            pp.request_patch("#протоколвстречи\n\nтекст", ["поправь срок"], meeting_sid="f6-patch")
        self.assertEqual(m.call_args.kwargs.get("model"), "sonnet")

    def test_apply_reply_cli_has_model_and_effort(self):
        self._skip_if_env("APPLY_REPLY_MODEL")
        import notary.meetings_apply_reply as mar  # noqa: PLC0415
        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = list(cmd)
            return _completed("[]")

        env = _env_without(*_EFFORT_ENV)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(mar.subprocess, "run", side_effect=_fake_run):
            mar.call_claude("prompt")
        cmd = captured["cmd"]
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_decision_drafts_pass_model(self):
        """finalize-meeting.py — скрипт (имя с дефисом), импортом не проверить: сверка кода."""
        src = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        block = src[src.index("_draft = _ccp("):]
        block = block[:block.index(")\n") + 2]
        self.assertIn('MEETING_DECISION_MODEL', block)
        self.assertIn('or "sonnet"', block)


# ---------------------------------------------------------------------------
# Сторож: номера моделей Claude в коде не возвращаются
# ---------------------------------------------------------------------------
_MODEL_NUMBER_RE = re.compile(r"claude-(?:opus|sonnet|haiku)-[0-9][\w.\-]*")


def _docstring_nodes(tree: ast.AST) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _model_number_literals(path: Path) -> list:
    """Строковые значения, которые ЦЕЛИКОМ — номер модели Claude (не текст про него)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docs = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs and _MODEL_NUMBER_RE.fullmatch(node.value.strip())):
            found.append(f"{path.name}:{node.lineno}: {node.value!r}")
    return found


class TestNoModelNumbersInCode(unittest.TestCase):

    def test_no_model_numbers(self):
        files = sorted(
            [p for p in _NOTARY.glob("*.py")]
            + [p for p in (_NOTARY / "lib").glob("*.py")]
            + [p for p in (_NOTARY / "auto_vocab").glob("*.py")]
            + [p for p in (_NOTARY / "cli").glob("*.py")]
        )
        self.assertGreater(len(files), 50)
        found = []
        for p in files:
            found += _model_number_literals(p)
        self.assertEqual(found, [], "номер модели в коде — пиши словом уровня (opus/sonnet/haiku)")

    def test_guard_catches_number(self):
        """Проверка самого сторожа: номер значением ловится, номер в тексте — нет."""
        import tempfile  # noqa: PLC0415
        src = (
            '"""Док: раньше был claude-opus-4-8."""\n'
            'A = "claude-opus-4-8"\n'
            'B = os.environ.get("X") or "claude-haiku-4-5-20251001"\n'
            'C = "текст про claude-sonnet-4-6 в строке"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "probe.py"
            p.write_text(src, encoding="utf-8")
            found = _model_number_literals(p)
        self.assertEqual(len(found), 2, found)


if __name__ == "__main__":
    unittest.main()
