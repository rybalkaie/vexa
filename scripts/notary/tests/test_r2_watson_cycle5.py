"""Второй круг моделей служб, Ф4 (Ватсон) — правки после цикла из 5 ходов.

План: `~/Projects/me/plans/2026-10-18-server-models-round2.md`, Фаза 4.

Покрывает:
  - Вывод клиента не конверт в чистом виде: конверт среди посторонних строк, в несколько
    строк, список событий (`verbose` в настройках) — ответ берётся из итогового `result`;
    события без `result` — отказ, а не «ответ модели» (сводка правок ушла бы людям сырым
    JSON). Ответ модели, который сам JSON-массив, по-прежнему проходит как есть.
  - Отказ «модель без инструментов позвала инструмент» (`malformed_tool_use_exhausted`)
    узнаётся (`is_tool_use_refusal`), и сборка протокола делает один проход на запасной
    модели (строка владельцу с честной причиной), а не роняет протокол.
  - Рабочая папка клиента по выключателю `NOTARY_CLAUDE_CWD=<папка>` — своя пустая, а не
    дерево кода с `CLAUDE.md` исходного Vexa (во всех 4 местах вызова); по умолчанию и при
    `inherit` — папка процесса, как раньше.
  - Пометки у задач: блок «Задачи» закрывают `---` и `# …` — приклеенная после задач шапка
    расшифровки (`**Участники:** …`) пометку не получает.
  - Второй черновик best-of-2 строку владельцу не шлёт (людям уходит черновик A).
  - Журнал вызовов: отказы без конверта и отказы разбора ответа на вечерний блок тоже
    пишутся; вход — вместе с кэшем.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python -m unittest tests.test_r2_watson_cycle5 -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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
from lib import protocol_fallback_alert as pfa  # noqa: E402
from lib import vocative_flags as vf  # noqa: E402
from lib.claude_cli import ClaudeCliFailed, ClaudeCliStreamResult  # noqa: E402

_SWITCH_ENV = (
    "NOTARY_CLAUDE_TOOLS", "NOTARY_CLAUDE_NO_SESSION_PERSISTENCE", "NOTARY_CLAUDE_CALLS_LOG",
    "MEETING_NOTARY_LOG_DIR", "CLAUDE_MIN_TIMEOUT", "NOTARY_CLAUDE_CWD",
    "NOTARY_CLAUDE_EFFORT_OPUS", "NOTARY_CLAUDE_EFFORT_SONNET", "NOTARY_CLAUDE_EFFORT_HAIKU",
)


def _clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SWITCH_ENV}
    env.update(extra)
    return env


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr="")


def _envelope_obj(result: str = "ок", *, is_error: bool = False, **extra) -> dict:
    env = {"type": "result", "subtype": "success", "is_error": is_error, "result": result,
           "session_id": "s-1",
           "usage": {"input_tokens": 3, "cache_creation_input_tokens": 9000,
                     "cache_read_input_tokens": 2000, "output_tokens": 50},
           "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 3, "outputTokens": 50}}}
    env.update(extra)
    return env


# Отказ без инструментов по живой форме CLI 2.1.301 (как в test_r2_watson_safety).
_MALFORMED = json.dumps({
    "type": "result", "subtype": "success", "is_error": True, "num_turns": 2,
    "terminal_reason": "malformed_tool_use_exhausted", "stop_reason": "stop_sequence",
    "result": "The model's tool call could not be parsed (retry also failed).",
    "modelUsage": {"claude-opus-5-5": {"inputTokens": 3, "outputTokens": 40}},
})
_MALFORMED_STREAM = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": [],
                "session_id": "s-2"}),
    _MALFORMED,
])


class _CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd_dir = os.path.join(self._tmp.name, "claude-cwd")
        self.log_path = os.path.join(self._tmp.name, "claude-calls.log")
        for p in (mock.patch.object(claude_cli.shutil, "which", return_value="/usr/bin/claude"),
                  mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_CWD=self.cwd_dir,
                                                         NOTARY_CLAUDE_CALLS_LOG=self.log_path),
                                  clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def _log(self) -> str:
        return Path(self.log_path).read_text(encoding="utf-8") if os.path.exists(self.log_path) else ""

    def _print(self, stdout: str, returncode: int = 0) -> str:
        with mock.patch.object(claude_cli.subprocess, "run", return_value=_completed(stdout, returncode)):
            return claude_cli.call_claude_print("prompt", model="sonnet")


# ---------------------------------------------------------------------------
# Вывод клиента: конверт найти, события без итога не отдавать как ответ
# ---------------------------------------------------------------------------
class TestEnvelopeRobust(_CliCase):

    def test_envelope_then_trailing_line(self):
        """Строка после конверта (например, «Update available») — ответ всё равно поле result."""
        out = self._print(json.dumps(_envelope_obj("ответ модели")) + "\nUpdate available: 2.1.302\n")
        self.assertEqual(out, "ответ модели")

    def test_warning_then_pretty_envelope(self):
        out = self._print("Warning: something\n" + json.dumps(_envelope_obj("ответ"), indent=2))
        self.assertEqual(out, "ответ")

    def test_verbose_event_list(self):
        """`json` при `verbose` в настройках — список событий; ответ — из итогового result."""
        events = [{"type": "system", "subtype": "init", "session_id": "s", "model": "claude-sonnet-5-5"},
                  {"type": "assistant", "session_id": "s", "message": {"content": []}},
                  _envelope_obj("из списка")]
        self.assertEqual(self._print(json.dumps(events)), "из списка")
        self.assertIn("answered=claude-sonnet-5-5", self._log())

    def test_events_without_result_fail_closed(self):
        events = [{"type": "system", "subtype": "init", "session_id": "s"},
                  {"type": "assistant", "session_id": "s", "message": {"content": []}}]
        with self.assertRaises(ClaudeCliFailed):
            self._print(json.dumps(events))
        self.assertIn("outcome=error", self._log())

    def test_model_json_answer_still_passes(self):
        """Ответ модели — сам JSON-массив (старый клиент / подставной вывод) — как раньше."""
        ans = '[{"cluster": "SPEAKER_00", "name": null, "confidence": 0.0}]'
        self.assertEqual(self._print(ans), ans)

    def test_print_json_accepts_event_list(self):
        events = [{"type": "system", "subtype": "init", "session_id": "s"}, _envelope_obj("джсон")]
        with mock.patch.object(claude_cli.subprocess, "run", return_value=_completed(json.dumps(events))):
            res = claude_cli.call_claude_print_json("prompt", model="haiku")
        self.assertEqual(res.text, "джсон")

    def test_log_input_counts_cache(self):
        self._print(json.dumps(_envelope_obj("ок")))
        self.assertIn(" in=11003 ", self._log())


# ---------------------------------------------------------------------------
# Отказ «позвала инструмент, которого нет»
# ---------------------------------------------------------------------------
class TestToolUseRefusal(_CliCase):

    def test_print_refusal_recognised(self):
        with self.assertRaises(ClaudeCliFailed) as ctx:
            self._print(_MALFORMED, returncode=1)
        self.assertEqual(ctx.exception.terminal_reason, "malformed_tool_use_exhausted")
        self.assertTrue(claude_cli.is_tool_use_refusal(ctx.exception))
        self.assertFalse(claude_cli.is_model_unavailable(ctx.exception))

    def test_print_refusal_exit_zero(self):
        with self.assertRaises(ClaudeCliFailed) as ctx:
            self._print(_MALFORMED, returncode=0)
        self.assertTrue(claude_cli.is_tool_use_refusal(ctx.exception))

    def test_stream_refusal_recognised(self):
        with mock.patch.object(claude_cli.subprocess, "run", return_value=_completed(_MALFORMED_STREAM, 1)):
            with self.assertRaises(ClaudeCliFailed) as ctx:
                claude_cli.call_claude_print_stream_json("prompt", model="opus")
        self.assertTrue(claude_cli.is_tool_use_refusal(ctx.exception))

    def test_other_failures_are_not_refusal(self):
        self.assertFalse(claude_cli.is_tool_use_refusal(
            ClaudeCliFailed("x", api_error_status=404, error_code="model_not_found")))
        self.assertFalse(claude_cli.is_tool_use_refusal(RuntimeError("malformed_tool_use_exhausted")))


_FAKE_PROTOCOL = (
    "#протоколвстречи 14.10.2026\n\n**Встреча:** Координация.\n\n"
    "**Участники:** Анна Иванова, Пётр Петров\n\n---\n\n## 1) Поставки\n\n▪️ Контейнер на таможне.\n"
)
_META = {"series": "r2-test-series", "date": "2026-10-14", "duration": 25,
         "expectedParticipants": ["Анна Иванова", "Пётр Петров"],
         "participants": ["Анна Иванова", "Пётр Петров"]}


def _refusal():
    return ClaudeCliFailed("claude --print (stream-json) exit=1 [terminal_reason=malformed_tool_use_exhausted]",
                           terminal_reason="malformed_tool_use_exhausted")


class TestProtocolFallbackOnRefusal(unittest.TestCase):

    def setUp(self):
        for p in (mock.patch.object(lp.run_log, "append_event"),
                  mock.patch.object(lp.protocol_fallback_alert, "alert_owner")):
            m = p.start()
            self.addCleanup(p.stop)
            if p.attribute == "append_event":
                self.run_log = m
            else:
                self.alert = m

    def _generate(self, primary_exc, *, fallback="sonnet", **kw):
        calls = []

        def _fake(user_prompt, *, system, timeout, model, max_thinking_tokens=None):
            calls.append(model)
            if model == "opus" and primary_exc is not None:
                raise primary_exc
            version = "claude-opus-5-5" if model == "opus" else "claude-sonnet-5-5"
            return ClaudeCliStreamResult(_FAKE_PROTOCOL, "end_turn", 0.0, 0, 0, {}, [version], "high")

        with mock.patch.object(lp, "PROTOCOL_GEN_MODEL", "opus"), \
                mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", fallback), \
                mock.patch.object(lp, "call_claude_print_stream_json", side_effect=_fake):
            text = lp.generate_protocol("т", _META, method_text="М", meeting_sid="r2-sid", **kw)
        return text, calls

    def test_refusal_goes_to_fallback_once(self):
        text, calls = self._generate(_refusal())
        self.assertIn("#протоколвстречи", text)
        self.assertEqual(calls, ["opus", "sonnet"])
        self.alert.assert_called_once()
        self.assertEqual(self.alert.call_args.kwargs["reason"], "primary-tool-refusal")
        reasons = [c.kwargs.get("reason") for c in self.run_log.call_args_list]
        self.assertIn("primary-tool-refusal", reasons)

    def test_refusal_without_fallback_still_fails(self):
        with self.assertRaises(lp.ProtocolGenerationError):
            self._generate(_refusal(), fallback="")

    def test_refusal_on_fallback_too_fails_with_reason(self):
        def _fake(user_prompt, *, system, timeout, model, max_thinking_tokens=None):
            raise _refusal()

        with mock.patch.object(lp, "PROTOCOL_GEN_MODEL", "opus"), \
                mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", "sonnet"), \
                mock.patch.object(lp, "call_claude_print_stream_json", side_effect=_fake):
            with self.assertRaises(lp.ProtocolGenerationError) as ctx:
                lp.generate_protocol("т", _META, method_text="М", meeting_sid="r2-sid")
        self.assertIn("инструмент", str(ctx.exception))
        reasons = [c.kwargs.get("reason") for c in self.run_log.call_args_list]
        self.assertIn("tool-refusal-fallback-failed", reasons)

    def test_draft_b_does_not_alert(self):
        self._generate(_refusal(), alert_on_fallback=False)
        self.alert.assert_not_called()

    def test_best_of_2_helper_passes_no_alert(self):
        with tempfile.TemporaryDirectory() as d:
            tr = Path(d) / "2026-10-14.md"
            tr.write_text("транскрипт", encoding="utf-8")
            with mock.patch.object(lp, "_is_finalize_best_of_2_enabled", return_value=True), \
                    mock.patch.object(lp, "generate_protocol", return_value="B") as gp:
                self.assertEqual(lp.generate_alt_draft_for_best_of_2(tr, _META, meeting_sid="s"), "B")
        self.assertIs(gp.call_args.kwargs.get("alert_on_fallback"), False)



class TestAlertText(unittest.TestCase):
    """Текст строки владельцу (без подмены `alert_owner`)."""

    def test_alert_text_reason_and_no_false_claim(self):
        pusher = mock.Mock(return_value=True)
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_FALLBACK_ALERT": "1"}):
            pfa.alert_owner(meeting_sid="cli-x", meeting_meta=_META, primary="opus", used="sonnet",
                            reason="primary-tool-refusal", pusher=pusher)
        text = pusher.call_args.args[0]
        self.assertIn("инструмент", text)
        self.assertNotIn("обычному пути", text, "у ручной пересборки в файл это неправда")


# ---------------------------------------------------------------------------
# Рабочая папка клиента
# ---------------------------------------------------------------------------
class TestClaudeCwd(_CliCase):

    def _cwd_of(self, fn, stdout, **kw):
        captured = {}

        def _fake_run(cmd, **k):
            captured.update(k)
            return _completed(stdout)

        with mock.patch.object(claude_cli.subprocess, "run", side_effect=_fake_run):
            fn("prompt", **kw)
        return captured.get("cwd", "НЕ ПЕРЕДАН")

    def test_all_wrapper_calls_use_own_empty_dir(self):
        stream_ok = "\n".join([json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5"}),
                               json.dumps(_envelope_obj("протокол", stop_reason="end_turn"))])
        for fn, out in ((claude_cli.call_claude_print, json.dumps(_envelope_obj())),
                        (claude_cli.call_claude_print_json, json.dumps(_envelope_obj())),
                        (claude_cli.call_claude_print_stream_json, stream_ok)):
            cwd = self._cwd_of(fn, out, model="opus")
            self.assertEqual(cwd, self.cwd_dir, fn.__name__)
        self.assertTrue(os.path.isdir(self.cwd_dir), "папка создаётся сама")
        self.assertEqual(os.listdir(self.cwd_dir), [], "в ней ничего нет — ни CLAUDE.md, ни git")

    def test_apply_reply_uses_own_dir(self):
        import notary.meetings_apply_reply as mar  # noqa: PLC0415
        captured = {}

        def _fake_run(cmd, **k):
            captured.update(k)
            return _completed(json.dumps(_envelope_obj('[{"n": 1, "action": "accept_once"}]')))

        with mock.patch.object(mar.subprocess, "run", side_effect=_fake_run):
            mar.call_claude("prompt")
        self.assertEqual(captured.get("cwd"), self.cwd_dir)

    def test_inherit_keeps_old_behaviour(self):
        with mock.patch.dict(os.environ, {"NOTARY_CLAUDE_CWD": "inherit"}):
            self.assertIsNone(claude_cli.claude_cwd())
            self.assertIsNone(self._cwd_of(claude_cli.call_claude_print, json.dumps(_envelope_obj()),
                                           model="sonnet"))

    def test_default_is_process_dir_as_before(self):
        """Без выключателя — как до Ф4: папка процесса (решение о пустой папке — за владельцем)."""
        env = {k: v for k, v in os.environ.items() if k != "NOTARY_CLAUDE_CWD"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(claude_cli.claude_cwd())
            self.assertIsNone(self._cwd_of(claude_cli.call_claude_print, json.dumps(_envelope_obj()),
                                           model="sonnet"))

    def test_unusable_dir_falls_back_to_process_dir(self):
        blocker = os.path.join(self._tmp.name, "file")
        Path(blocker).write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"NOTARY_CLAUDE_CWD": os.path.join(blocker, "sub")}):
            self.assertIsNone(claude_cli.claude_cwd())


# ---------------------------------------------------------------------------
# Журнал: отказы разбора ответа на вечерний блок и прочие ветки
# ---------------------------------------------------------------------------
class TestFailureJournal(_CliCase):

    def test_apply_reply_nonzero_exit_logged_with_cause(self):
        import notary.meetings_apply_reply as mar  # noqa: PLC0415
        with mock.patch.object(mar.subprocess, "run", return_value=_completed(_MALFORMED, 1)):
            with self.assertRaises(RuntimeError) as ctx:
                mar.call_claude("prompt")
        self.assertIn("malformed_tool_use_exhausted", str(ctx.exception), "причина из stdout, не пустой stderr")
        log = self._log()
        self.assertIn("kind=apply-reply", log)
        self.assertIn("outcome=error", log)
        self.assertIn("client_model=claude-opus-5-5", log)

    def test_apply_reply_timeout_logged(self):
        import notary.meetings_apply_reply as mar  # noqa: PLC0415
        with mock.patch.object(mar.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=90)):
            with self.assertRaises(RuntimeError):
                mar.call_claude("prompt")
        self.assertIn("outcome=timeout", self._log())

    def test_apply_reply_ok_has_elapsed(self):
        import notary.meetings_apply_reply as mar  # noqa: PLC0415
        with mock.patch.object(mar.subprocess, "run",
                               return_value=_completed(json.dumps(_envelope_obj("[]")))):
            mar.call_claude("prompt")
        self.assertNotIn("elapsed=?", self._log())

    def test_empty_stdout_logged(self):
        with self.assertRaises(ClaudeCliFailed):
            self._print("   ")
        self.assertIn("outcome=error", self._log())

    def test_stream_not_result_logged(self):
        bad = json.dumps({"type": "assistant", "session_id": "s", "message": {"content": []}})
        with mock.patch.object(claude_cli.subprocess, "run", return_value=_completed(bad)):
            with self.assertRaises(ClaudeCliFailed):
                claude_cli.call_claude_print_stream_json("prompt", model="opus")
        self.assertIn("kind=stream", self._log())
        self.assertIn("outcome=error", self._log())


class TestToolsSwitchWords(unittest.TestCase):

    def test_off_words_mean_no_tools(self):
        for word in ("none", "None", "off", "0", "false", " no "):
            with mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_TOOLS=word), clear=True):
                self.assertEqual(claude_cli.safety_args()[:2], ["--tools", ""], word)

    def test_default_and_list_pass_through(self):
        with mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_TOOLS="default"), clear=True):
            self.assertEqual(claude_cli.tools_value(), "default")
        with mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_TOOLS="Read,Grep"), clear=True):
            self.assertEqual(claude_cli.tools_value(), "Read,Grep")


# ---------------------------------------------------------------------------
# Пометки у задач: приклеенная после задач шапка расшифровки — не задачи
# ---------------------------------------------------------------------------
_PROTOCOL_WITH_TAIL = (
    "#протоколвстречи 12.10.2026\n"
    "**Участники:** Мария Михина, Ольга Новикова\n\n---\n\n"
    "## 1) Финплан\n\n▪️ Мария рассказала про план.\n\n"
    "## Задачи\n\n"
    "**Мария Михина**\n"
    "- собрать финплан к пятнице\n\n"
    "**Ольга Новикова**\n"
    "- проверить остатки\n\n"
    "---\n\n"
    "# Встреча Telemost — —\n\n"
    "**Участники:** Мария Михина, Ольга Новикова\n\n"
    "---\n\n"
    "## Ключевые моменты\n\n- Мария: про финплан\n"
)
_FINDINGS = [
    {"section": "vocative", "quote": "Мария Михина", "note": "тут мог быть Ольга Новикова, а не Мария Михина",
     "pair": ["Мария Михина", "Ольга Новикова"]},
]
_MARK = "проверьте: тут мог быть"


class TestTaskBlockEnds(unittest.TestCase):

    def test_transcript_header_after_tasks_not_flagged(self):
        out = vf.apply_vocative_flags(_PROTOCOL_WITH_TAIL, _FINDINGS)
        tail = out.split("# Встреча Telemost", 1)[1]
        self.assertNotIn(_MARK, tail)
        tasks = out.split("## Задачи", 1)[1].split("# Встреча Telemost", 1)[0]
        self.assertGreaterEqual(tasks.count(_MARK), 1, "у задач пометка осталась")

    def test_tasks_with_separator_between_owners_kept(self):
        """Задач после `---` внутри блока в живых протоколах нет; если бы были — без пометки,
        но плашка в шапке остаётся (проверка, что правило не ломает остальное)."""
        out = vf.apply_vocative_flags(_PROTOCOL_WITH_TAIL, _FINDINGS)
        self.assertIn(vf.SPEAKER_PLAQUE_SENTINEL, out.split("## 1) Финплан", 1)[0])


if __name__ == "__main__":
    unittest.main()
