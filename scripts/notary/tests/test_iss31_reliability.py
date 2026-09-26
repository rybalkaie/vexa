# -*- coding: utf-8 -*-
"""ISS-31 / Ф2: надёжность генерации протокола — ретрай при обрыве + обуздание
thinking-бюджета (план `plans/2026-07-14-reliability-and-open-tasks-ux.md`).

Что проверяем (по REQ реестра плана):
  1. REQ 31.1 — усечённый/безголовый ответ на 1-й попытке → 1 повтор (A6, с
     выключенным thinking) → полный протокол публикуется В ТОЙ ЖЕ финализации,
     без опоры на следующий тик collector'а. Больше 1 повтора не продавливаем.
  2. REQ 31.2/A9 — шапка есть, но `stop_reason == "max_tokens"` (усечение
     ПОСЛЕ шапки) классифицируется «неполный» и тоже триггерит ретрай — не
     публикуется молча (старый детект по одному лишь «нет шапки» это пропускал).
  3. Гейт «стаб генерации не зовёт реальный claude» (память проекта
     `reissue-llm-tier-gate-default-off`, применена к транспорту генерации
     протокола): даже двойной вызов (попытка + ретрай) не должен трогать
     `subprocess.run` в `lib.claude_cli`.
  4. REQ 31.3 — `NOTARY_PROTOCOL_MAX_THINKING_TOKENS` из конфигурации (не
     хардкод), дефолт 6000, клампится к API-минимуму 1024; `generate_protocol`
     пробрасывает бюджет в 1-ю попытку и принудительный 0 — в ретрай.
  5. `call_claude_print_stream_json` (lib.claude_cli, новый транспорт Ф2):
     парсит ПОСЛЕДНЮЮ строку stdout как терминальный `result`-объект, отдаёт
     `stop_reason` (недоступен в плоском `--output-format json`), пробрасывает
     `MAX_THINKING_TOKENS` в env subprocess'а, не теряя остальное окружение;
     `--verbose` идёт вместе с `--output-format stream-json`; невалидный JSON /
     не-`result` терминальная строка / `is_error` → `ClaudeCliFailed`.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss31_reliability -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import run_log  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402
from lib import claude_cli  # noqa: E402
from lib.claude_cli import ClaudeCliStreamResult, ClaudeCliFailed, ClaudeCliTimeout  # noqa: E402


_META = {
    "series": "test-iss31-ph2-series",
    "date": "2026-07-14",
    "duration": 20,
    "expectedParticipants": ["Иван"],
    "participants": ["Иван"],
}
_TRANSCRIPT = "**[00:01] Иван:** обсудили план на неделю."

_FULL_PROTOCOL = (
    "#протоколвстречи 14.07.2026\n\n"
    "**Встреча:** Тест.\n\n"
    "**Длительность:** 20 мин\n\n"
    "**Участники:** Иван\n\n"
    "**Транскрипт:** [2026-07-14.md](2026-07-14.md)\n\n"
    "---\n\n"
    "## 1) Тема\n\n▪️ Что-то произошло.\n"
)


def _stream(text: str, stop_reason: str = "end_turn") -> ClaudeCliStreamResult:
    return ClaudeCliStreamResult(text, stop_reason, 0.01, 100, 200, {"type": "result"})


class _TmpRunLogMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"MEETING_NOTARY_RUN_LOG_DIR": self._tmp.name}
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        run_log._pruned_this_process = False

    def _log_content(self, sid: str) -> str:
        p = Path(self._tmp.name) / f"{sid}.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""


# ─────────────────────────── REQ 31.1: ретрай ───────────────────────────


class RetryOnTruncationTest(_TmpRunLogMixin, unittest.TestCase):
    def test_headerless_first_attempt_then_full_on_retry(self):
        headerless = "Модель начала рассуждать и не успела написать протокол."
        calls = [_stream(headerless), _stream(_FULL_PROTOCOL)]
        with mock.patch.object(
            lp, "call_claude_print_stream_json", side_effect=calls,
        ) as mocked:
            out = lp.generate_protocol(
                _TRANSCRIPT, _META, method_text="М", meeting_sid="retry-ok-sid",
            )
        self.assertEqual(mocked.call_count, 2, "1-я попытка + 1 ретрай, не больше")
        self.assertTrue(out.startswith("#протоколвстречи"))
        content = self._log_content("retry-ok-sid")
        self.assertIn("event=retrying", content)
        self.assertIn("reason=output-without-header", content)
        self.assertIn("event=ok-after-retry", content)

    def test_retry_calls_same_model_with_thinking_disabled(self):
        # A6 «урезанным thinking-бюджетом» = 0 на ретрае, независимо от
        # дефолтного бюджета 1-й попытки; модель на ретрае — ТА ЖЕ, что дала
        # неполный ответ (retry ловит вероятностный обрыв, не меняя модель).
        calls = [_stream("без шапки"), _stream(_FULL_PROTOCOL)]
        with mock.patch.object(
            lp, "call_claude_print_stream_json", side_effect=calls,
        ) as mocked:
            lp.generate_protocol(
                _TRANSCRIPT, _META, method_text="М", meeting_sid="retry-budget-sid",
            )
        first_kwargs = mocked.call_args_list[0].kwargs
        retry_kwargs = mocked.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["max_thinking_tokens"], 6000)
        self.assertEqual(retry_kwargs["max_thinking_tokens"], 0)
        self.assertEqual(first_kwargs["model"], retry_kwargs["model"])

    def test_retry_also_incomplete_raises_after_exactly_one_retry(self):
        calls = [_stream("без шапки раз"), _stream("без шапки два")]
        with mock.patch.object(
            lp, "call_claude_print_stream_json", side_effect=calls,
        ) as mocked:
            with self.assertRaises(lp.ProtocolGenerationError):
                lp.generate_protocol(
                    _TRANSCRIPT, _META, method_text="М", meeting_sid="retry-fail-sid",
                )
        self.assertEqual(mocked.call_count, 2, "ровно 1 повтор — не больше (A6, «Что НЕ делать»)")
        content = self._log_content("retry-fail-sid")
        self.assertIn("event=retrying", content)
        self.assertIn("event=failed", content)
        self.assertIn("reason=retry-output-without-header", content)
        # Опасная тройка: никакого текста транскрипта/ответа в логе.
        self.assertNotIn("план на неделю", content)

    def test_timeout_degrades_to_fallback_then_retries_on_fallback(self):
        # Цикл5/Ф2 (У1): путь ТАЙМАУТ основной → деградация на фолбэк → фолбэк
        # вернул НЕПОЛНЫЙ → ретрай идёт по ФОЛБЭК-модели (той, что дала неполный
        # ответ), а НЕ по упавшей по таймауту основной. Ретрай — с thinking=0.
        # Раньше эта комбинация (деградация + ретрай в одной генерации) не была
        # закрыта ни одним тестом — покрываем несущую опору фазы на её же стыке.
        if not lp.PROTOCOL_GEN_FALLBACK_MODEL or (
            lp.PROTOCOL_GEN_FALLBACK_MODEL == lp.PROTOCOL_GEN_MODEL
        ):
            self.skipTest("фолбэк-модель не сконфигурирована / совпадает с основной")
        calls = [
            ClaudeCliTimeout("основная модель таймаутнула"),  # 1-я попытка (primary)
            _stream("без шапки"),                              # фолбэк вернул неполный
            _stream(_FULL_PROTOCOL),                           # ретрай на фолбэке — полный
        ]
        with mock.patch.object(
            lp, "call_claude_print_stream_json", side_effect=calls,
        ) as mocked:
            out = lp.generate_protocol(
                _TRANSCRIPT, _META, method_text="М", meeting_sid="degrade-retry-sid",
            )
        self.assertEqual(mocked.call_count, 3, "primary(timeout) + fallback + fallback-retry")
        primary_call, fallback_call, retry_call = mocked.call_args_list
        self.assertEqual(primary_call.kwargs["model"], lp.PROTOCOL_GEN_MODEL)
        self.assertEqual(fallback_call.kwargs["model"], lp.PROTOCOL_GEN_FALLBACK_MODEL)
        # Ключевой инвариант: ретрай — по ФОЛБЭК-модели (не по упавшей основной).
        self.assertEqual(retry_call.kwargs["model"], lp.PROTOCOL_GEN_FALLBACK_MODEL)
        self.assertEqual(retry_call.kwargs["max_thinking_tokens"], 0)
        self.assertTrue(out.startswith("#протоколвстречи"))
        content = self._log_content("degrade-retry-sid")
        self.assertIn("event=degraded", content)       # деградация зафиксирована
        self.assertIn("reason=primary-timeout", content)
        self.assertIn("event=retrying", content)        # ретрай зафиксирован
        self.assertIn("event=ok-after-retry", content)  # ретрай спас встречу


# ───────────── R25 / ISS-42: авто-ретрай генерации при ТАЙМАУТЕ с бОльшим лимитом ─────────────


class TimeoutRetryBiggerLimitTest(_TmpRunLogMixin, unittest.TestCase):
    """R25 (ISS-42): таймаут сборки протокола НЕ роняет протокол целиком. ОДИН авто-ретрай
    с ЯВНО увеличенным лимитом ДО алерта владельцу; алерт (проброс ProtocolGenerationError
    в finalize) — только после ПОВТОРНОГО провала. Единый владелец ретрая — generate_protocol
    (РАЗМ5): collector/finalize второй авто-ретрай не добавляют."""

    def test_retry_timeout_default_is_one_and_half_x(self):
        # REQ: «45 мин от базовых 30» = 1.5×.
        self.assertEqual(lp._protocol_retry_timeout(1800), 2700)
        self.assertEqual(lp._protocol_retry_timeout(600), 900)

    def test_retry_timeout_env_override_absolute(self):
        with mock.patch.dict(os.environ, {"PROTOCOL_GEN_RETRY_TIMEOUT": "3600"}):
            self.assertEqual(lp._protocol_retry_timeout(1800), 3600)

    def test_retry_timeout_floor_is_base_and_garbage_defaults(self):
        # Ретрай не короче исходной попытки даже при заниженном env.
        with mock.patch.dict(os.environ, {"PROTOCOL_GEN_RETRY_TIMEOUT": "100"}):
            self.assertEqual(lp._protocol_retry_timeout(600), 600)
        for bad in ("", "abc", "0", "-5"):
            with mock.patch.dict(os.environ, {"PROTOCOL_GEN_RETRY_TIMEOUT": bad}):
                self.assertEqual(lp._protocol_retry_timeout(600), 900, bad)

    def test_timeout_retries_with_bigger_limit_and_recovers(self):
        # 1-я попытка таймаутит → авто-ретрай с УВЕЛИЧЕННЫМ лимитом собирает протокол.
        # Алерта нет (не бросили), встреча спасена.
        calls = [ClaudeCliTimeout("основная упёрлась в лимит"), _stream(_FULL_PROTOCOL)]
        with mock.patch.object(
            lp, "call_claude_print_stream_json", side_effect=calls,
        ) as mocked:
            out = lp.generate_protocol(
                _TRANSCRIPT, _META, method_text="М", meeting_sid="r25-ok-sid",
            )
        self.assertEqual(mocked.call_count, 2, "таймаут + РОВНО один авто-ретрай")
        first_call, retry_call = mocked.call_args_list
        base_timeout = first_call.kwargs["timeout"]
        self.assertEqual(
            retry_call.kwargs["timeout"], lp._protocol_retry_timeout(base_timeout),
            "ретрай идёт с ЯВНО увеличенным лимитом",
        )
        self.assertGreater(retry_call.kwargs["timeout"], base_timeout)
        self.assertTrue(out.startswith("#протоколвстречи"))
        content = self._log_content("r25-ok-sid")
        self.assertIn("reason=primary-timeout", content)

    def test_timeout_retry_also_fails_raises_for_alert(self):
        # И ретрай упёрся в (больший) лимит → проброс ошибки → finalize шлёт алерт.
        calls = [ClaudeCliTimeout("основная"), ClaudeCliTimeout("и ретрай тоже")]
        with mock.patch.object(
            lp, "call_claude_print_stream_json", side_effect=calls,
        ) as mocked:
            with self.assertRaises(lp.ProtocolGenerationError):
                lp.generate_protocol(
                    _TRANSCRIPT, _META, method_text="М", meeting_sid="r25-fail-sid",
                )
        self.assertEqual(mocked.call_count, 2, "ровно один авто-ретрай, затем честный отказ")
        content = self._log_content("r25-fail-sid")
        self.assertIn("reason=timeout-retry-failed", content)

    def test_retry_holds_even_without_fallback_model(self):
        # Фолбэк выключен → авто-ретрай всё равно есть (та же модель, бОльший лимит):
        # ISS-42 — проблема во ВРЕМЕНИ, не в модели. R25 держится независимо от фолбэка.
        calls = [ClaudeCliTimeout("primary"), _stream(_FULL_PROTOCOL)]
        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", ""), \
             mock.patch.object(
                 lp, "call_claude_print_stream_json", side_effect=calls) as mocked:
            out = lp.generate_protocol(
                _TRANSCRIPT, _META, method_text="М", meeting_sid="r25-nofb-sid",
            )
        self.assertEqual(mocked.call_count, 2)
        first_call, retry_call = mocked.call_args_list
        self.assertEqual(first_call.kwargs["model"], retry_call.kwargs["model"])  # та же модель
        self.assertGreater(retry_call.kwargs["timeout"], first_call.kwargs["timeout"])
        self.assertTrue(out.startswith("#протоколвстречи"))


# ──────────────────── REQ 31.2 / A9: stop_reason=max_tokens ────────────────────


class StopReasonMaxTokensTest(_TmpRunLogMixin, unittest.TestCase):
    def test_header_present_but_max_tokens_triggers_retry(self):
        truncated_after_header = (
            "#протоколвстречи 14.07.2026\n\n**Встреча:** Тест.\n\n"
            "**Длительность:** 20 мин\n\n**Участники:** Иван\n\n"
            "**Транскрипт:** [2026-07-14.md](2026-07-14.md)\n\n---\n\n"
            "## 1) Тема\n\n▪️ Оборвалось прямо здесь"
        )
        calls = [
            _stream(truncated_after_header, stop_reason="max_tokens"),
            _stream(_FULL_PROTOCOL, stop_reason="end_turn"),
        ]
        with mock.patch.object(
            lp, "call_claude_print_stream_json", side_effect=calls,
        ) as mocked:
            out = lp.generate_protocol(
                _TRANSCRIPT, _META, method_text="М", meeting_sid="maxtok-sid",
            )
        self.assertEqual(mocked.call_count, 2, "шапка формально есть, но max_tokens — тоже ретрай")
        self.assertTrue(out.startswith("#протоколвстречи"))
        content = self._log_content("maxtok-sid")
        self.assertIn("reason=max-tokens-truncated", content)
        self.assertIn("stop_reason=max_tokens", content)

    def test_max_tokens_without_header_reports_header_reason(self):
        # Приоритет детекта в _extract_protocol_text: «нет шапки вообще»
        # информативнее «max_tokens» — если шапки нет, репортим её отсутствие,
        # а не косвенный стоп-сигнал.
        calls = [_stream("никакой шапки тут нет", stop_reason="max_tokens")] * 2
        with mock.patch.object(lp, "call_claude_print_stream_json", side_effect=calls):
            with self.assertRaises(lp.ProtocolGenerationError):
                lp.generate_protocol(
                    _TRANSCRIPT, _META, method_text="М",
                    meeting_sid="maxtok-noheader-sid",
                )
        content = self._log_content("maxtok-noheader-sid")
        self.assertIn("reason=output-without-header", content)

    def test_end_turn_with_header_is_complete_no_retry(self):
        with mock.patch.object(
            lp, "call_claude_print_stream_json",
            return_value=_stream(_FULL_PROTOCOL, stop_reason="end_turn"),
        ) as mocked:
            lp.generate_protocol(
                _TRANSCRIPT, _META, method_text="М", meeting_sid="normal-sid",
            )
        self.assertEqual(mocked.call_count, 1)


# ───────────── гейт: стаб не зовёт реальный claude (двойной вызов) ─────────────


class NoRealClaudeCallTest(_TmpRunLogMixin, unittest.TestCase):
    """И 1-я попытка, и ретрай — через мок; реальный subprocess не трогается."""

    def test_generate_protocol_never_shells_out_even_with_retry(self):
        calls = [_stream("без шапки"), _stream(_FULL_PROTOCOL)]
        with mock.patch.object(lp, "call_claude_print_stream_json", side_effect=calls):
            with mock.patch("lib.claude_cli.subprocess.run") as real_subprocess:
                lp.generate_protocol(
                    _TRANSCRIPT, _META, method_text="М", meeting_sid="no-shell-sid",
                )
                real_subprocess.assert_not_called()


# ───────────────────── REQ 31.3: конфигурация thinking-бюджета ─────────────────────


class ThinkingBudgetConfigTest(unittest.TestCase):
    def test_default_is_6000(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTARY_PROTOCOL_MAX_THINKING_TOKENS", None)
            self.assertEqual(lp._protocol_thinking_budget(), 6000)

    def test_env_override_above_floor(self):
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_MAX_THINKING_TOKENS": "2000"}):
            self.assertEqual(lp._protocol_thinking_budget(), 2000)

    def test_zero_disables_thinking_not_clamped(self):
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_MAX_THINKING_TOKENS": "0"}):
            self.assertEqual(lp._protocol_thinking_budget(), 0)

    def test_low_positive_value_clamped_to_api_minimum(self):
        # Спайк Ф2: budget_tokens требует >=1024 у моделей, где параметр ещё
        # принимается — значения 1..1023 не отклоняем, поднимаем до пола.
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_MAX_THINKING_TOKENS": "100"}):
            self.assertEqual(lp._protocol_thinking_budget(), 1024)

    def test_garbage_value_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_MAX_THINKING_TOKENS": "не число"}):
            self.assertEqual(lp._protocol_thinking_budget(), 6000)

    def test_generate_protocol_passes_configured_budget_to_first_call(self):
        with mock.patch.dict(os.environ, {"NOTARY_PROTOCOL_MAX_THINKING_TOKENS": "3333"}):
            with mock.patch.object(
                lp, "call_claude_print_stream_json",
                return_value=_stream(_FULL_PROTOCOL),
            ) as mocked:
                lp.generate_protocol(
                    _TRANSCRIPT, _META, method_text="М", meeting_sid="cfg-sid",
                )
        self.assertEqual(mocked.call_args.kwargs["max_thinking_tokens"], 3333)


# ───────────── lib.claude_cli.call_claude_print_stream_json (транспорт) ─────────────


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _result_line(**overrides) -> str:
    payload = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "готовый текст", "stop_reason": "end_turn",
        "total_cost_usd": 0.05, "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class CallClaudePrintStreamJsonTest(unittest.TestCase):
    def setUp(self):
        self._which = mock.patch("lib.claude_cli.shutil.which", return_value="/usr/bin/claude")
        self._which.start()
        self.addCleanup(self._which.stop)

    def test_parses_terminal_result_line_only(self):
        stdout = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": []}}),
            _result_line(),
        ])
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout=stdout),
        ):
            res = claude_cli.call_claude_print_stream_json("prompt", model="sonnet")
        self.assertEqual(res.text, "готовый текст")
        self.assertEqual(res.stop_reason, "end_turn")
        self.assertEqual(res.cost_usd, 0.05)
        self.assertEqual(res.input_tokens, 10)
        self.assertEqual(res.output_tokens, 20)

    def test_stop_reason_max_tokens_surfaced(self):
        stdout = _result_line(stop_reason="max_tokens", result="усечённый текст")
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout=stdout),
        ):
            res = claude_cli.call_claude_print_stream_json("prompt")
        self.assertEqual(res.stop_reason, "max_tokens")

    def test_cmd_includes_verbose_and_stream_json_and_thinking_env(self):
        captured = {}

        def _fake_run(cmd, *, input, capture_output, text, timeout, env):
            captured["cmd"] = cmd
            captured["env"] = env
            return _FakeCompletedProcess(stdout=_result_line())

        base_env = {"PATH": "/usr/bin", "HOME": "/home/dev"}
        with mock.patch("lib.claude_cli.subprocess.run", side_effect=_fake_run):
            with mock.patch.dict(os.environ, base_env, clear=True):
                claude_cli.call_claude_print_stream_json(
                    "prompt", model="opus", max_thinking_tokens=1500,
                )
        self.assertIn("--output-format", captured["cmd"])
        self.assertIn("stream-json", captured["cmd"])
        self.assertIn("--verbose", captured["cmd"])
        self.assertEqual(captured["env"]["MAX_THINKING_TOKENS"], "1500")
        # Остальной env не потерян (subprocess.run(env=...) заменяет целиком).
        self.assertEqual(captured["env"]["PATH"], "/usr/bin")
        self.assertEqual(captured["env"]["HOME"], "/home/dev")

    def test_no_thinking_budget_means_env_untouched(self):
        captured = {}

        def _fake_run(cmd, *, input, capture_output, text, timeout, env):
            captured["env"] = env
            return _FakeCompletedProcess(stdout=_result_line())

        with mock.patch("lib.claude_cli.subprocess.run", side_effect=_fake_run):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MAX_THINKING_TOKENS", None)
                claude_cli.call_claude_print_stream_json("prompt")
        self.assertNotIn("MAX_THINKING_TOKENS", captured["env"])

    def test_is_error_raises_claude_cli_failed(self):
        stdout = _result_line(is_error=True, result="упало")
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout=stdout),
        ):
            with self.assertRaises(ClaudeCliFailed):
                claude_cli.call_claude_print_stream_json("prompt")

    def test_non_result_terminal_line_raises(self):
        stdout = json.dumps({"type": "assistant", "message": {}})
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout=stdout),
        ):
            with self.assertRaises(ClaudeCliFailed):
                claude_cli.call_claude_print_stream_json("prompt")

    def test_invalid_json_raises(self):
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout="{не json"),
        ):
            with self.assertRaises(ClaudeCliFailed):
                claude_cli.call_claude_print_stream_json("prompt")

    def test_empty_stdout_raises(self):
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout=""),
        ):
            with self.assertRaises(ClaudeCliFailed):
                claude_cli.call_claude_print_stream_json("prompt")

    def test_nonzero_exit_raises(self):
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout="", stderr="boom", returncode=1),
        ):
            with self.assertRaises(ClaudeCliFailed):
                claude_cli.call_claude_print_stream_json("prompt")

    # ── Цикл5 фазы 5 ISS-60: причина сбоя CLI лежит в stdout, не в stderr ──────
    # Живой CLI при отказе API (нет входа, лимит подписки, 429) выходит с кодом 1 и
    # ПУСТЫМ stderr, а причину пишет итоговым событием `result` (is_error) и
    # событием `assistant` с `is_api_error_message`. Раньше stdout при ненулевом
    # коде выбрасывался — в журнале оставалось `exit=1, stderr=''`, и два сбоя
    # составления протокола на бою (16.09, 26.09) нечем было объяснить.
    def _api_error_stdout(self, result: str = "Not logged in · Please run /login") -> str:
        return "\n".join([
            json.dumps({"type": "system", "subtype": "init", "model": "opus"}),
            json.dumps({"type": "assistant", "is_api_error_message": True,
                        "error": "authentication_failed",
                        "message": {"content": [{"type": "text", "text": result}]}}),
            _result_line(is_error=True, subtype="success", result=result,
                         terminal_reason="api_error"),
        ])

    def test_nonzero_exit_surfaces_api_error_from_stream(self):
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout=self._api_error_stdout(), returncode=1),
        ):
            with self.assertRaises(ClaudeCliFailed) as ctx:
                claude_cli.call_claude_print_stream_json("prompt")
        msg = str(ctx.exception)
        self.assertIn("exit=1", msg)
        self.assertIn("error=authentication_failed", msg)
        self.assertIn("Not logged in", msg)
        # Причина обязана пережить обрезку сообщения в генерации протокола (`[:200]`).
        self.assertIn("error=authentication_failed", msg[:200])

    def test_stream_error_never_carries_model_text(self):
        """Текст ответа модели (кусок протокола) в сообщение не попадает НИКОГДА:
        ни из `result` без is_error, ни многострочным, ни из событий `assistant`."""
        secret = "СЕКРЕТ-РЕПЛИКА-ВСТРЕЧИ"
        cases = [
            ("\n".join([
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": secret}]}}),
                _result_line(is_error=False, result=secret + " продолжение", stop_reason="max_tokens"),
            ]), 1),
            (_result_line(is_error=True, result="Ошибка API\n" + secret), 1),
            # Ветка is_error при коде 0: раньше туда шли 200 знаков `result` как есть.
            (_result_line(is_error=True, result="Ошибка API\n" + secret), 0),
        ]
        for stdout, rc in cases:
            with self.subTest(stdout=stdout[:40], rc=rc):
                with mock.patch(
                    "lib.claude_cli.subprocess.run",
                    return_value=_FakeCompletedProcess(stdout=stdout, returncode=rc),
                ):
                    with self.assertRaises(ClaudeCliFailed) as ctx:
                        claude_cli.call_claude_print_stream_json("prompt")
                self.assertNotIn(secret, str(ctx.exception))
                self.assertIn(("Ошибка API" if "Ошибка API" in stdout else "result_len="),
                              str(ctx.exception))

    def test_stream_error_text_is_redacted(self):
        # Похожая на токен строка собирается на лету: в исходниках публичного форка
        # не должно быть ничего, что сканер секретов примет за настоящий токен.
        prefix = "-".join(("sk", "ant", "oat01"))
        leak = prefix + "-" + "A" * 50
        stdout = _result_line(is_error=True, result="Invalid bearer %s rejected" % leak)
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout=stdout, returncode=1),
        ):
            with self.assertRaises(ClaudeCliFailed) as ctx:
                claude_cli.call_claude_print_stream_json("prompt")
        self.assertNotIn("AAAAAAAAAA", str(ctx.exception))
        self.assertNotIn(prefix + "-A", str(ctx.exception))

    def test_nonzero_exit_with_garbage_stdout_keeps_old_message(self):
        with mock.patch(
            "lib.claude_cli.subprocess.run",
            return_value=_FakeCompletedProcess(stdout="{не json", stderr="", returncode=1),
        ):
            with self.assertRaises(ClaudeCliFailed) as ctx:
                claude_cli.call_claude_print_stream_json("prompt")
        self.assertIn("exit=1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
