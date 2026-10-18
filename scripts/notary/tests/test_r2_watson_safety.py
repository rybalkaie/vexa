"""Второй круг моделей служб, Ф4 (Ватсон): без инструментов, журнал модели, пометки у задач.

План: `~/Projects/me/plans/2026-10-18-server-models-round2.md`, Фаза 4.

Покрывает:
  - `claude_cli.safety_args`: `--tools ""` + `--strict-mcp-config` в КАЖДОМ вызове Claude
    (три функции обёртки и свой вызов разбора ответа на вечерний блок); откат
    `NOTARY_CLAUDE_TOOLS=default`; выключатель копий `--no-session-persistence`
    (`NOTARY_CLAUDE_NO_SESSION_PERSISTENCE`, по умолчанию выключен).
  - `call_claude_print` теперь читает конверт `--output-format json`: ответ — поле
    `result`, `is_error` — отказ, не конверт — вывод как есть; причина отказа из stdout.
  - Журнал вызовов `claude-calls.log`: какая модель ответила (`modelUsage` / init),
    усилие, исход; только при заданном каталоге журналов.
  - `generate_protocol`: протокол собран запасной моделью → строка владельцу (класс
    `expected`, только владельцу, дедуп по встрече); основной моделью — нет; в журнал
    прогона встречи — ответившая версия и усилие.
  - `notify.push(send_class=…)`: класс уходит переменной `TG_SEND_CLASS`, не флагом.
  - Пометки «⚠️ проверьте: тут мог быть» — только в блоке «Задачи» (и строка таблицы —
    внутри последней ячейки); плашка в шапке остаётся.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python -m unittest tests.test_r2_watson_safety -v
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
from lib import notify  # noqa: E402
from lib import protocol_fallback_alert as pfa  # noqa: E402
from lib import series_memory  # noqa: E402
from lib import vocative_flags as vf  # noqa: E402
from lib.claude_cli import ClaudeCliFailed, ClaudeCliStreamResult, ClaudeCliTimeout  # noqa: E402

_SWITCH_ENV = (
    "NOTARY_CLAUDE_TOOLS", "NOTARY_CLAUDE_NO_SESSION_PERSISTENCE", "NOTARY_CLAUDE_CALLS_LOG",
    "MEETING_NOTARY_LOG_DIR", "CLAUDE_MIN_TIMEOUT",
    "NOTARY_CLAUDE_EFFORT_OPUS", "NOTARY_CLAUDE_EFFORT_SONNET", "NOTARY_CLAUDE_EFFORT_HAIKU",
)


def _clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SWITCH_ENV}
    env.update(extra)
    return env


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr="")


def _envelope(result: str = "ответ", *, model: str = "claude-sonnet-5-5", is_error: bool = False,
              **extra) -> str:
    env = {"type": "result", "subtype": "success", "is_error": is_error, "result": result,
           "usage": {"input_tokens": 10, "output_tokens": 5},
           "modelUsage": {model: {"inputTokens": 10, "outputTokens": 5}}}
    env.update(extra)
    return json.dumps(env, ensure_ascii=False)


# Отказ без инструментов по живой форме CLI 2.1.301 (проба на сервере: модель, которую
# просят выполнить команду, пишет вызов текстом; клиент дважды просит повторить и сдаётся).
_MALFORMED_TOOL_USE = json.dumps({
    "type": "result", "subtype": "success", "is_error": True, "num_turns": 2,
    "terminal_reason": "malformed_tool_use_exhausted", "stop_reason": "stop_sequence",
    "result": "The model's tool call could not be parsed (retry also failed).",
    "modelUsage": {"claude-haiku-5-5": {"inputTokens": 3, "outputTokens": 40}},
})
_STREAM_OK = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": []}),
    json.dumps({"type": "result", "subtype": "success", "result": "протокол", "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "modelUsage": {"claude-opus-5-5": {"inputTokens": 100, "outputTokens": 50}}}),
])


# ---------------------------------------------------------------------------
# Флаги безопасности
# ---------------------------------------------------------------------------
class TestSafetyArgs(unittest.TestCase):

    def test_default_no_tools_no_mcp_persist_on(self):
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            self.assertEqual(claude_cli.safety_args(), ["--tools", "", "--strict-mcp-config"])
            self.assertFalse(claude_cli.session_persistence_off())

    def test_rollback_tools_default(self):
        with mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_TOOLS="default"), clear=True):
            self.assertEqual(claude_cli.safety_args(), ["--tools", "default", "--strict-mcp-config"])

    def test_empty_value_means_no_tools(self):
        with mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_TOOLS=""), clear=True):
            self.assertEqual(claude_cli.safety_args()[:2], ["--tools", ""])

    def test_no_session_persistence_switch(self):
        for val, on in (("1", True), ("true", True), ("on", True), ("0", False), ("", False)):
            with mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_NO_SESSION_PERSISTENCE=val),
                                 clear=True):
                self.assertEqual("--no-session-persistence" in claude_cli.safety_args(), on, val)


class TestEveryCallHasSafetyFlags(unittest.TestCase):
    """Все места, где Ватсон запускает `claude`, несут запрет инструментов и MCP."""

    def setUp(self):
        p_which = mock.patch.object(claude_cli.shutil, "which", return_value="/usr/bin/claude")
        p_which.start()
        self.addCleanup(p_which.stop)

    def _cmd(self, fn, stdout, env=None, **kw):
        captured = {}

        def _fake_run(cmd, **k):
            captured["cmd"] = list(cmd)
            return _completed(stdout)

        with mock.patch.dict(os.environ, env or _clean_env(), clear=True), \
                mock.patch.object(claude_cli.subprocess, "run", side_effect=_fake_run):
            fn("prompt", **kw)
        return captured["cmd"]

    def _assert_safe(self, cmd, *, persist_off=False):
        i = cmd.index("--tools")
        self.assertEqual(cmd[i + 1], "", "после --tools — пустое значение: ни одного инструмента")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual("--no-session-persistence" in cmd, persist_off)

    def test_print(self):
        cmd = self._cmd(claude_cli.call_claude_print, _envelope(), model="sonnet")
        self._assert_safe(cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")

    def test_print_json(self):
        self._assert_safe(self._cmd(claude_cli.call_claude_print_json, _envelope(), model="haiku"))

    def test_print_stream_json(self):
        self._assert_safe(self._cmd(claude_cli.call_claude_print_stream_json, _STREAM_OK, model="opus"))

    def test_persist_switch_reaches_all_three(self):
        env = _clean_env(NOTARY_CLAUDE_NO_SESSION_PERSISTENCE="1")
        for fn, out in ((claude_cli.call_claude_print, _envelope()),
                        (claude_cli.call_claude_print_json, _envelope()),
                        (claude_cli.call_claude_print_stream_json, _STREAM_OK)):
            self._assert_safe(self._cmd(fn, out, env=env, model="opus"), persist_off=True)

    def test_apply_reply_own_subprocess(self):
        import notary.meetings_apply_reply as mar  # noqa: PLC0415
        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = list(cmd)
            return _completed(_envelope('[{"n": 1, "action": "accept_once"}]'))

        with mock.patch.dict(os.environ, _clean_env(), clear=True), \
                mock.patch.object(mar.subprocess, "run", side_effect=_fake_run):
            out = mar.call_claude("prompt")
        self._assert_safe(captured["cmd"])
        self.assertEqual(captured["cmd"][captured["cmd"].index("--output-format") + 1], "json")
        self.assertEqual(out, '[{"n": 1, "action": "accept_once"}]', "в разбор идёт поле result")

    def test_apply_reply_error_envelope_is_runtime_error(self):
        import notary.meetings_apply_reply as mar  # noqa: PLC0415
        with mock.patch.dict(os.environ, _clean_env(), clear=True), \
                mock.patch.object(mar.subprocess, "run", return_value=_completed(_MALFORMED_TOOL_USE)):
            with self.assertRaises(RuntimeError):  # вызывающий ловит RuntimeError — как раньше
                mar.call_claude("prompt")


# ---------------------------------------------------------------------------
# call_claude_print: конверт JSON
# ---------------------------------------------------------------------------
class TestPrintEnvelope(unittest.TestCase):

    def setUp(self):
        for p in (mock.patch.object(claude_cli.shutil, "which", return_value="/usr/bin/claude"),
                  mock.patch.dict(os.environ, _clean_env(), clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def _call(self, stdout, returncode=0):
        with mock.patch.object(claude_cli.subprocess, "run", return_value=_completed(stdout, returncode)):
            return claude_cli.call_claude_print("prompt", model="sonnet")

    def test_result_field_is_the_answer(self):
        self.assertEqual(self._call(_envelope("  {\"a\": 1}\n")), '{"a": 1}')

    def test_plain_text_passthrough(self):
        """Не конверт (старый клиент / подставной вывод) — ответ как есть, как раньше."""
        self.assertEqual(self._call("просто текст\n"), "просто текст")
        self.assertEqual(self._call('[{"cluster": "S0", "name": null}]'),
                         '[{"cluster": "S0", "name": null}]')

    def test_is_error_with_exit_zero_fails(self):
        with self.assertRaises(ClaudeCliFailed):
            self._call(_envelope("Not logged in", is_error=True))

    def test_nonzero_exit_carries_cause_from_stdout(self):
        with self.assertRaises(ClaudeCliFailed) as ctx:
            self._call(_MALFORMED_TOOL_USE, returncode=1)
        self.assertIn("malformed_tool_use_exhausted", str(ctx.exception))
        self.assertFalse(claude_cli.is_model_unavailable(ctx.exception))

    def test_empty_result_fails(self):
        with self.assertRaises(ClaudeCliFailed):
            self._call(_envelope("   "))

    def test_timeout_still_timeout(self):
        with mock.patch.object(claude_cli.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
            with self.assertRaises(ClaudeCliTimeout):
                claude_cli.call_claude_print("prompt", model="sonnet", timeout=1)


# ---------------------------------------------------------------------------
# Журнал вызовов: какая модель ответила
# ---------------------------------------------------------------------------
class TestCallsJournal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log = Path(self._tmp.name) / "claude-calls.log"
        for p in (mock.patch.object(claude_cli.shutil, "which", return_value="/usr/bin/claude"),
                  mock.patch.dict(os.environ, _clean_env(MEETING_NOTARY_LOG_DIR=self._tmp.name),
                                  clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, fn, stdout, returncode=0, **kw):
        with mock.patch.object(claude_cli.subprocess, "run", return_value=_completed(stdout, returncode)):
            return fn("prompt", **kw)

    def _lines(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def test_print_records_answered_model_and_effort(self):
        self._run(claude_cli.call_claude_print, _envelope(model="claude-sonnet-5-5"), model="sonnet")
        (line,) = self._lines()
        for part in ("kind=print", "requested=sonnet", "effort=high", "answered=claude-sonnet-5-5",
                     "outcome=ok", "tools=none", "persist=on"):
            self.assertIn(part, line)
        self.assertNotIn("prompt", line)  # ни подсказки, ни ответа

    def test_stream_records_and_returns_answered(self):
        res = self._run(claude_cli.call_claude_print_stream_json, _STREAM_OK, model="opus")
        self.assertEqual(res.answered_models, ["claude-opus-5-5"])
        self.assertEqual(res.effort, "high")
        self.assertIn("answered=claude-opus-5-5", self._lines()[0])

    def test_stream_falls_back_to_init_model(self):
        no_usage = "\n".join([
            json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5"}),
            json.dumps({"type": "result", "subtype": "success", "result": "ок", "usage": {}}),
        ])
        res = self._run(claude_cli.call_claude_print_stream_json, no_usage, model="opus")
        self.assertEqual(res.answered_models, ["claude-opus-5-5"])

    def test_json_records(self):
        res = self._run(claude_cli.call_claude_print_json, _envelope(model="claude-haiku-5-5"),
                        model="haiku")
        self.assertEqual(res.answered_models, ["claude-haiku-5-5"])
        self.assertIn("effort=low", self._lines()[0])

    def test_error_recorded(self):
        with self.assertRaises(ClaudeCliFailed):
            self._run(claude_cli.call_claude_print, _MALFORMED_TOOL_USE, returncode=1, model="haiku")
        line = self._lines()[0]
        self.assertIn("outcome=error", line)
        self.assertIn("answered=claude-haiku-5-5", line)

    def test_answered_order_by_output(self):
        env = {"modelUsage": {"claude-haiku-5-5": {"outputTokens": 3},
                              "claude-opus-5-5": {"outputTokens": 900}}}
        self.assertEqual(claude_cli.answered_models(env), ["claude-opus-5-5", "claude-haiku-5-5"])
        self.assertEqual(claude_cli.answered_models(None), [])

    def test_no_log_dir_no_file(self):
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            self.assertIsNone(claude_cli._calls_log_path())
        with mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_CALLS_LOG="/x/y.log"), clear=True):
            self.assertEqual(claude_cli._calls_log_path(), "/x/y.log")

    def test_unwritable_log_never_breaks_call(self):
        with mock.patch.dict(os.environ, _clean_env(NOTARY_CLAUDE_CALLS_LOG="/proc/нет/claude.log"),
                             clear=True):
            out = self._run(claude_cli.call_claude_print, _envelope("ок"), model="sonnet")
        self.assertEqual(out, "ок")


# ---------------------------------------------------------------------------
# Протокол на запасной модели → строка владельцу
# ---------------------------------------------------------------------------
_FAKE_PROTOCOL = (
    "#протоколвстречи 14.10.2026\n\n"
    "**Встреча:** Координация.\n\n"
    "**Участники:** Анна Иванова, Пётр Петров\n\n"
    "---\n\n"
    "## 1) Поставки\n\n"
    "▪️ Контейнер на таможне.\n"
)
_META = {"series": "r2-test-series", "date": "2026-10-14", "duration": 25,
         "expectedParticipants": ["Анна Иванова", "Пётр Петров"],
         "participants": ["Анна Иванова", "Пётр Петров"]}


def _unavailable(status=404, code="model_not_found"):
    return ClaudeCliFailed("claude --print (stream-json) exit=1", api_error_status=status, error_code=code)


class TestFallbackAlert(unittest.TestCase):

    def setUp(self):
        for p in (mock.patch.object(lp.run_log, "append_event"),
                  mock.patch.object(lp.protocol_fallback_alert, "alert_owner")):
            m = p.start()
            self.addCleanup(p.stop)
            if p.attribute == "append_event":
                self.run_log = m
            else:
                self.alert = m

    def _generate(self, primary_exc=None, *, fallback="sonnet"):
        def _fake(user_prompt, *, system, timeout, model, max_thinking_tokens=None):
            if model == lp.PROTOCOL_GEN_MODEL and primary_exc is not None:
                raise primary_exc
            version = "claude-opus-5-5" if model == "opus" else "claude-sonnet-5-5"
            return ClaudeCliStreamResult(_FAKE_PROTOCOL, "end_turn", 0.0, 0, 0, {}, [version], "high")

        with mock.patch.object(lp, "PROTOCOL_GEN_MODEL", "opus"), \
                mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", fallback), \
                mock.patch.object(lp, "call_claude_print_stream_json", side_effect=_fake):
            return lp.generate_protocol("т", _META, method_text="М", meeting_sid="r2-sid")

    def test_unavailable_primary_alerts_owner_once(self):
        self._generate(_unavailable())
        self.alert.assert_called_once()
        kw = self.alert.call_args.kwargs
        self.assertEqual((kw["primary"], kw["used"], kw["reason"]), ("opus", "sonnet", "primary-unavailable"))
        self.assertEqual(kw["meeting_sid"], "r2-sid")
        self.assertEqual(kw["answered"], ["claude-sonnet-5-5"])

    def test_timeout_retry_on_fallback_alerts(self):
        self._generate(ClaudeCliTimeout("timeout 600s"))
        self.alert.assert_called_once()
        self.assertEqual(self.alert.call_args.kwargs["reason"], "primary-timeout")

    def test_primary_ok_no_alert(self):
        self._generate()
        self.alert.assert_not_called()

    def test_timeout_retry_on_same_model_no_alert(self):
        """Запасной нет (пусто) — повтор той же моделью: протокол собран основной, строки нет."""
        calls = []

        def _fake(user_prompt, *, system, timeout, model, max_thinking_tokens=None):
            calls.append(model)
            if len(calls) == 1:
                raise ClaudeCliTimeout("timeout")
            return ClaudeCliStreamResult(_FAKE_PROTOCOL, "end_turn", 0.0, 0, 0, {}, ["claude-opus-5-5"], "high")

        with mock.patch.object(lp, "PROTOCOL_GEN_MODEL", "opus"), \
                mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", ""), \
                mock.patch.object(lp, "call_claude_print_stream_json", side_effect=_fake):
            lp.generate_protocol("т", _META, method_text="М", meeting_sid="r2-sid")
        self.assertEqual(calls, ["opus", "opus"])
        self.alert.assert_not_called()

    def test_run_log_carries_answered_version_and_effort(self):
        self._generate()
        last = self.run_log.call_args_list[-1]
        self.assertEqual(last.args[1], "ok")
        self.assertEqual(last.kwargs.get("answered"), "claude-opus-5-5")
        self.assertEqual(last.kwargs.get("effort"), "high")


class TestAlertOwnerItself(unittest.TestCase):

    def test_text_class_and_key(self):
        pusher = mock.Mock(return_value=True)
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_FALLBACK_ALERT": "1"}):
            ok = pfa.alert_owner(meeting_sid="tm-1", meeting_meta=_META, primary="opus", used="sonnet",
                                 reason="primary-unavailable", answered=["claude-sonnet-5-5"], pusher=pusher)
        self.assertTrue(ok)
        text = pusher.call_args.args[0]
        self.assertIn("r2-test-series", text)
        self.assertIn("2026-10-14", text)
        self.assertIn("запасной", text)
        self.assertIn("claude-sonnet-5-5", text)
        self.assertEqual(pusher.call_args.kwargs["send_class"], "expected")
        self.assertEqual(pusher.call_args.kwargs["dedupe_key"], "protocol-fallback:tm-1:primary-unavailable")

    def test_switch_off(self):
        pusher = mock.Mock(return_value=True)
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_FALLBACK_ALERT": "0"}):
            self.assertFalse(pfa.alert_owner(meeting_sid="tm-1", meeting_meta=_META, primary="opus",
                                             used="sonnet", reason="primary-timeout", pusher=pusher))
        pusher.assert_not_called()

    def test_never_raises(self):
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_FALLBACK_ALERT": "1"}):
            self.assertFalse(pfa.alert_owner(meeting_sid=None, meeting_meta=None, primary="opus",
                                             used="sonnet", reason="x",
                                             pusher=mock.Mock(side_effect=OSError("сеть"))))

    def test_default_channel_is_notify_push_expected(self):
        """Без подмены — `notify.push`; класс уходит переменной `TG_SEND_CLASS`, не флагом."""
        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"], captured["kw"] = list(cmd), kw
            return _completed("")

        with tempfile.TemporaryDirectory() as d:
            fake_bin = Path(d) / "tg-send"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_FALLBACK_ALERT": "1",
                                              "TG_SEND_BIN": str(fake_bin)}), \
                    mock.patch.object(notify, "TG_SEND_BIN", fake_bin), \
                    mock.patch.object(notify, "DEDUP_DIR", Path(d)), \
                    mock.patch.object(notify.subprocess, "run", side_effect=_fake_run):
                ok = pfa.alert_owner(meeting_sid="tm-2", meeting_meta=_META, primary="opus",
                                     used="sonnet", reason="primary-unavailable")
        self.assertTrue(ok)
        self.assertEqual(captured["kw"]["env"]["TG_SEND_CLASS"], "expected")
        self.assertFalse(any(a.startswith("--expected") for a in captured["cmd"]),
                         "флаг класса отправитель Ватсона принял бы за текст")

    def test_no_explicit_sender_no_send(self):
        """Отправитель не задан явно (мак, тесты) — настоящий `tg-send` не зовётся."""
        env = {k: v for k, v in os.environ.items() if k != "TG_SEND_BIN"}
        env["NOTARY_PROTOCOL_FALLBACK_ALERT"] = "1"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(notify, "push") as push:
            self.assertFalse(pfa.alert_owner(meeting_sid="tm-3", meeting_meta=_META, primary="opus",
                                             used="sonnet", reason="primary-unavailable"))
        push.assert_not_called()

    def test_push_without_class_keeps_old_call(self):
        captured = {}

        def _fake_run(cmd, **kw):
            captured["kw"] = kw
            return _completed("")

        with tempfile.TemporaryDirectory() as d:
            fake_bin = Path(d) / "tg-send"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.object(notify, "TG_SEND_BIN", fake_bin), \
                    mock.patch.object(notify, "DEDUP_DIR", Path(d)), \
                    mock.patch.object(notify.subprocess, "run", side_effect=_fake_run):
                notify.push("строка", dedupe=False)
        self.assertNotIn("env", captured["kw"])


# ---------------------------------------------------------------------------
# Пометки «⚠️ проверьте: тут мог быть» — только у задач
# ---------------------------------------------------------------------------
_PROTOCOL = (
    "#протоколвстречи 14.10.2026\n"
    "**Участники:** Татьяна, Мария\n"
    "\n---\n\n"
    "## 1) Продажи\n\n"
    "▪️ Татьяна рассказала про продажи за неделю.\n\n"
    "## Решения / что внедряем\n\n"
    "🔸 Мария и Татьяна согласовали бюджет.\n\n"
    "## 4) Задачи\n\n"
    "**Татьяна**\n"
    "- подготовить отчёт по продажам\n\n"
    "**Мария**\n"
    "- обновить прайс к пятнице\n"
    "- обсудить бюджет (Татьяна в копии)\n\n"
    "## 🔻 С прошлых встреч\n\n"
    "- Татьяна: сверить остатки — висит\n"
)
_FINDINGS = [
    {"section": "vocative", "quote": "Татьяна", "note": "тут мог быть Мария, а не Татьяна",
     "pair": ["Татьяна", "Мария"]},
    {"section": "vocative", "quote": "Мария", "note": "тут мог быть Татьяна, а не Мария",
     "pair": ["Татьяна", "Мария"]},
]
_MARK = "проверьте: тут мог быть"


def _section(text: str, heading: str) -> str:
    rest = text.split(heading, 1)[1]
    return rest.split("\n## ", 1)[0]


class TestVocativeOnlyAtTasks(unittest.TestCase):

    def setUp(self):
        self.out = vf.apply_vocative_flags(_PROTOCOL, _FINDINGS)

    def test_flags_only_in_tasks(self):
        tasks = _section(self.out, "## 4) Задачи")
        # подзаголовки обоих исполнителей + задача Марии с именем Татьяны
        self.assertEqual(tasks.count(_MARK), 3)
        self.assertEqual(self.out.count(_MARK), 3)
        for heading in ("## 1) Продажи", "## Решения / что внедряем", "## 🔻 С прошлых встреч"):
            self.assertNotIn(_MARK, _section(self.out, heading), heading)

    def test_plaque_in_header_stays(self):
        head = self.out.split("## 1) Продажи", 1)[0]
        self.assertIn(vf.SPEAKER_PLAQUE_SENTINEL, head)
        self.assertIn("Мария и Татьяна", head)

    def test_idempotent(self):
        self.assertEqual(vf.apply_vocative_flags(self.out, _FINDINGS), self.out)

    def test_task_owners_survive_for_series_memory(self):
        tasks = series_memory.extract_open_tasks(self.out)
        self.assertIn("Татьяна: подготовить отчёт по продажам", tasks)
        self.assertIn("Мария: обновить прайс к пятнице", tasks)
        self.assertFalse(any("⚠️" in t for t in tasks))

    def test_table_row_flag_inside_last_cell(self):
        protocol = (
            "#протоколвстречи 14.10.2026\n\n---\n\n"
            "## Задачи\n\n"
            "| Кому | Что | Срок |\n"
            "|---|---|---|\n"
            "| Татьяна | подготовить отчёт | пятница |\n"
        )
        out = vf.apply_vocative_flags(protocol, _FINDINGS)
        row = [ln for ln in out.splitlines() if ln.startswith("| Татьяна")][0]
        self.assertTrue(row.endswith("|"), row)
        self.assertIn(_MARK, row)
        self.assertEqual(row.count("|"), 4, "колонок не прибавилось")
        tasks = series_memory.extract_open_tasks(out)
        self.assertIn("Татьяна: подготовить отчёт (срок: пятница)", tasks)

    def test_review_contour_uses_new_rule(self):
        """Контур ревью (`apply_review_flags`) зовёт именно это правило."""
        out = lp.apply_review_flags(_PROTOCOL, _FINDINGS)
        self.assertEqual(out, self.out)


if __name__ == "__main__":
    unittest.main()
