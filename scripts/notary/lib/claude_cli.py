"""Тонкая обёртка над `claude --print` CLI (подписка владельца).

Зачем отдельный модуль: одинаковый subprocess-паттерн нужен Ф2 (маппинг
имён), Ф4 (генерация протокола) и Ф5 (извлечение задач). Чтобы не плодить
копии вызова с одинаковой обработкой timeout/exit/stdout — выносим сюда.

Дисциплина «Опасной тройки» (CLAUDE.md проекта):
  - НЕ логируем содержимое промта/ответа.
  - Логируем только метаданные (длина промта, exit-код, elapsed).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from typing import Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Причина сбоя из потока `stream-json` (цикл5 фазы 5 ISS-60)
# ---------------------------------------------------------------------------
# 🔴 ЗАЧЕМ. Живой CLI при отказе API (нет входа, лимит подписки, 429, перегрузка)
# выходит с кодом 1 и ПУСТЫМ stderr, а причину пишет в stdout: итоговым событием
# `result` (`is_error: true`, `terminal_reason: api_error`, строка ошибки в
# `result`) и событием `assistant` с `is_api_error_message: true` и кодом `error`
# (проверено на CLI 2.1.283 без входа: `authentication_failed`, «Not logged in ·
# Please run /login»). Раньше stdout при ненулевом коде выбрасывался, и в журнале
# оставалось `exit=1, stderr=''` — так на бою 16.09 две встречи остались без
# протокола, а причину восстановить было нечем.
#
# 🔴 ОПАСНАЯ ТРОЙКА. В потоке лежит и ответ модели — то есть протокол встречи.
# Отсюда берутся ТОЛЬКО: коды (`subtype`, `terminal_reason`, `error` — строго
# `[a-z_]`), число `api_error_status` и строка ошибки — лишь при `is_error: true`,
# только первая строка, не длиннее 160 знаков, с вычищенными похожими на секреты
# кусками. Текст `assistant` не читается никогда; `result` без `is_error` (это
# ответ модели, в том числе обрывок протокола) — только длина.
_STREAM_CODE_RE = re.compile(r"^[a-z_]{1,40}$")
_STREAM_SECRET_RES = (
    re.compile(r"sk-ant-\S+"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"://[^/\s@]+@"),
    re.compile(r"[A-Za-z0-9_\-]{40,}"),
)
_STREAM_TAIL_LINES = 50
_STREAM_MSG_MAX = 160


def _redact_secret_like(text: str) -> str:
    for rx in _STREAM_SECRET_RES:
        text = rx.sub("***", text)
    return text


def stream_error_summary(stdout: Optional[str]) -> Optional[str]:
    """Короткая причина сбоя из вывода `stream-json` — без текста ответа модели.

    Разбирает не больше 50 последних строк: последнее событие `result` и последнее
    `assistant` с `is_api_error_message: true`. Возвращает, например,
    `subtype=success terminal_reason=api_error error=authentication_failed
    msg='Not logged in · Please run /login'`, или None, если разбирать нечего.
    """
    import json as _json  # локально, как у остальных вариантов вызова

    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()][-_STREAM_TAIL_LINES:]
    final = None
    api_error = None
    for ln in reversed(lines):
        try:
            ev = _json.loads(ln)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if final is None and ev.get("type") == "result":
            final = ev
        elif (api_error is None and ev.get("type") == "assistant"
              and ev.get("is_api_error_message") is True):
            api_error = ev
        if final is not None and api_error is not None:
            break
    parts: list[str] = []
    for src in (final, api_error):
        if not isinstance(src, dict):
            continue
        for key in ("subtype", "terminal_reason", "error"):
            val = src.get(key)
            item = f"{key}={val}"
            if isinstance(val, str) and _STREAM_CODE_RE.match(val) and item not in parts:
                parts.append(item)
        status = src.get("api_error_status")
        if isinstance(status, int) and not isinstance(status, bool):
            item = f"api_error_status={status}"
            if item not in parts:
                parts.append(item)
    if isinstance(final, dict):
        res = final.get("result")
        if final.get("is_error") is True and isinstance(res, str) and res.strip():
            first = res.strip().splitlines()[0]
            parts.append("msg=%r" % _redact_secret_like(first)[:_STREAM_MSG_MAX])
        elif isinstance(res, str):
            parts.append(f"result_len={len(res)}")
    return " ".join(parts) or None


class ClaudeCliError(RuntimeError):
    """Базовое исключение для проблем с `claude --print`."""


class ClaudeCliNotInstalled(ClaudeCliError):
    """`claude` бинарник не найден в PATH."""


class ClaudeCliTimeout(ClaudeCliError):
    """Превышен timeout subprocess."""


class ClaudeCliFailed(ClaudeCliError):
    """Ненулевой exit, пустой stdout, прочие неуспехи."""


def call_claude_print(
    prompt: str,
    *,
    system: Optional[str] = None,
    timeout: int = 60,
    model: Optional[str] = None,
) -> str:
    """Запускает `claude --print` через stdin, возвращает stdout (stripped).

    Параметры:
      prompt: пользовательский промт (идёт в stdin целиком).
      system: опциональная системная инструкция; склеивается с prompt через
              два перевода строки (CLI `--print` поддерживает только plain
              stdin без отдельного system-канала).
      timeout: секунды на выполнение subprocess.
      model: опциональный явный выбор модели (например, `claude-sonnet-4-6`
             или `sonnet`/`opus` алиас). None → дефолт CLI (того, кто
             залогинен). Прокидывается в subprocess как `--model <value>`.

    Возвращает: stripped stdout. На любую ошибку — ClaudeCliError-подкласс.

    НЕ ловит ничего тихо — вызывающий код должен решить, как обработать
    исключение (например, в `map_speaker_names` — warning + return {}).
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise ClaudeCliNotInstalled("`claude` не найден в PATH")

    # Глобальный пол таймаута через env: на длинных встречах (45+ мин транскрипт)
    # зашитые в вызовы лимиты (90/180с) не успевают, claude обрывается и протокол
    # уходит в деградированный режим. CLAUDE_MIN_TIMEOUT поднимает пол всех вызовов
    # без правки каждого call-site. 0/unset → поведение прежнее.
    try:
        _floor = int(os.environ.get("CLAUDE_MIN_TIMEOUT", "0") or "0")
    except ValueError:
        _floor = 0
    if _floor > timeout:
        timeout = _floor

    full_prompt = prompt if system is None else f"{system}\n\n{prompt}"

    cmd = [claude_bin, "--print"]
    if model:
        cmd += ["--model", model]

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        raise ClaudeCliTimeout(
            f"claude --print timeout {timeout}s (elapsed={elapsed:.1f}s)"
        ) from e
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        raise ClaudeCliFailed(
            f"claude --print exit={result.returncode}, stderr={stderr_snippet!r}"
        )
    raw = (result.stdout or "").strip()
    if not raw:
        raise ClaudeCliFailed(
            f"claude --print вернул пустой stdout (elapsed={elapsed:.1f}s)"
        )
    logger.debug(
        "claude --print ok: model=%s prompt_len=%d output_len=%d elapsed=%.1fs",
        model or "(default)", len(full_prompt), len(raw), elapsed,
    )
    return raw


class ClaudeCliResult:
    """Результат `claude --print --output-format json`.

    Поля:
      text       — ответ ассистента (поле `result` JSON-обёртки CLI).
      cost_usd   — `total_cost_usd` из ответа CLI (0.0, если CLI не отдал).
      input_tokens / output_tokens — из `usage` (0, если нет).
      raw        — распарсенная JSON-обёртка целиком (для аудита).
    """

    __slots__ = ("text", "cost_usd", "input_tokens", "output_tokens", "raw")

    def __init__(self, text: str, cost_usd: float, input_tokens: int, output_tokens: int, raw: dict):
        self.text = text
        self.cost_usd = cost_usd
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raw = raw


def call_claude_print_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    timeout: int = 60,
    model: Optional[str] = None,
) -> "ClaudeCliResult":
    """Как `call_claude_print`, но через `--output-format json` — даёт стоимость.

    Зачем: Ф8 LLM-proposer должен учитывать расходы Claude API (РИСК6 плана).
    Подписочный CLI отдаёт `total_cost_usd` и `usage` в JSON-обёртке, поэтому
    SDK/ANTHROPIC_API_KEY не нужны — берём цифры отсюда.

    На любую ошибку (бинарник, timeout, ненулевой exit, невалидный JSON) —
    ClaudeCliError-подкласс (как у text-варианта). Вызывающий решает сам.
    """
    import json as _json  # локально: модуль использует subprocess, json нужен лишь здесь

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise ClaudeCliNotInstalled("`claude` не найден в PATH")

    # Глобальный пол таймаута через env: на длинных встречах (45+ мин транскрипт)
    # зашитые в вызовы лимиты (90/180с) не успевают, claude обрывается и протокол
    # уходит в деградированный режим. CLAUDE_MIN_TIMEOUT поднимает пол всех вызовов
    # без правки каждого call-site. 0/unset → поведение прежнее.
    try:
        _floor = int(os.environ.get("CLAUDE_MIN_TIMEOUT", "0") or "0")
    except ValueError:
        _floor = 0
    if _floor > timeout:
        timeout = _floor

    full_prompt = prompt if system is None else f"{system}\n\n{prompt}"
    cmd = [claude_bin, "--print", "--output-format", "json"]
    if model:
        cmd += ["--model", model]

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        raise ClaudeCliTimeout(
            f"claude --print --output-format json timeout {timeout}s (elapsed={elapsed:.1f}s)"
        ) from e
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        raise ClaudeCliFailed(
            f"claude --print (json) exit={result.returncode}, stderr={stderr_snippet!r}"
        )
    raw_out = (result.stdout or "").strip()
    if not raw_out:
        raise ClaudeCliFailed(f"claude --print (json) пустой stdout (elapsed={elapsed:.1f}s)")
    try:
        envelope = _json.loads(raw_out)
    except _json.JSONDecodeError as e:
        raise ClaudeCliFailed(f"claude --print (json) невалидный JSON-конверт: {e}") from e
    if not isinstance(envelope, dict):
        raise ClaudeCliFailed("claude --print (json) конверт не объект")

    text = envelope.get("result") or ""
    if not isinstance(text, str) or not text.strip():
        raise ClaudeCliFailed("claude --print (json) пустое поле result")
    usage = envelope.get("usage") or {}
    cost = envelope.get("total_cost_usd")
    cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    logger.debug(
        "claude --print(json) ok: model=%s prompt_len=%d out_len=%d cost=$%.4f in=%d out=%d elapsed=%.1fs",
        model or "(default)", len(full_prompt), len(text), cost_usd, in_tok, out_tok, elapsed,
    )
    return ClaudeCliResult(text.strip(), cost_usd, in_tok, out_tok, envelope)


class ClaudeCliStreamResult:
    """Результат `claude --print --output-format stream-json --verbose`.

    Поля:
      text        — ответ ассистента (как у `ClaudeCliResult`).
      stop_reason — `stop_reason` терминальной строки CLI (`end_turn`,
                    `max_tokens`, …), `None` если CLI его не прислал.
                    НЕДОСТУПНО в плоском `--output-format json` (проверено
                    Ф2 ISS-31 РИСК1: `grep stop_reason` по кодовой базе = 0) —
                    единственный способ его получить — этот режим.
      cost_usd / input_tokens / output_tokens — как у `ClaudeCliResult`.
      raw — терминальная JSON-строка (`type=="result"`) целиком, для аудита.
    """

    __slots__ = ("text", "stop_reason", "cost_usd", "input_tokens", "output_tokens", "raw")

    def __init__(
        self, text: str, stop_reason: Optional[str], cost_usd: float,
        input_tokens: int, output_tokens: int, raw: dict,
    ):
        self.text = text
        self.stop_reason = stop_reason
        self.cost_usd = cost_usd
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raw = raw


def call_claude_print_stream_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    timeout: int = 60,
    model: Optional[str] = None,
    max_thinking_tokens: Optional[int] = None,
) -> "ClaudeCliStreamResult":
    """Как `call_claude_print`, но через `--output-format stream-json --verbose`.

    Зачем отдельный режим (Ф2 ISS-31, РИСК1): это единственный вывод CLI, где
    приходит `stop_reason` — нужен, чтобы отличить усечение по лимиту вывода
    (`stop_reason == "max_tokens"`) от нормального завершения, даже если шапка
    протокола формально успела попасть в текст (детект «нет шапки» такое не
    ловит). `--verbose` ОБЯЗАТЕЛЕН вместе с `--output-format stream-json` в
    `--print`-режиме — без него CLI падает мгновенно с понятной ошибкой ДО
    обращения к API (проверено спайком Ф2, cli exit=1 "requires --verbose").

    max_thinking_tokens: если задан — пробрасывается в subprocess как env
    `MAX_THINKING_TOKENS` (тот же механизм, что у интерактивного Claude Code).
    `0` — принудительно выключает thinking (`type: "disabled"` в теле запроса
    к API — подтверждено статическим разбором CLI, ветка `qe===0`).
    `>0` — запрошенный бюджет; спайк Ф2 (реальные вызовы на `claude-sonnet-5`
    + справочные строки самого CLI-бинаря про `budget_tokens`) показал: рычаг
    реально переключает наличие thinking (on/off), но как ЧИСЛЕННЫЙ потолок
    надёжно работает только на моделях, где API ещё принимает явный
    `budget_tokens` (Sonnet 4.6 и старше — deprecated, но не отклоняется).
    На новых моделях (Opus 4.8/4.7, Fable 5 — семейство `PROTOCOL_GEN_MODEL`)
    `budget_tokens` вовсе убран из API (сам CLI шлёт `type:"adaptive"` вместо
    него, минуя число) — численный потолок там реализовать нечем через этот
    транспорт; `None` → рычаг не трогаем (текущее поведение CLI/модели).

    Разбирает ТОЛЬКО последнюю непустую строку stdout (`type == "result"` —
    несёт `result`/`usage`/`total_cost_usd`, как у `ClaudeCliResult`, ПЛЮС
    `stop_reason`); промежуточные события потока (system/assistant/…) не
    парсятся — вызывающему коду они не нужны.

    На любую ошибку (бинарник, timeout, ненулевой exit, невалидный JSON,
    `is_error`) — ClaudeCliError-подкласс, как у остальных вариантов.
    """
    import json as _json  # локально, как у call_claude_print_json

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise ClaudeCliNotInstalled("`claude` не найден в PATH")

    try:
        _floor = int(os.environ.get("CLAUDE_MIN_TIMEOUT", "0") or "0")
    except ValueError:
        _floor = 0
    if _floor > timeout:
        timeout = _floor

    full_prompt = prompt if system is None else f"{system}\n\n{prompt}"
    cmd = [claude_bin, "--print", "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]

    # env копируем целиком (subprocess.run(env=...) ЗАМЕНЯЕТ окружение, не
    # мёржит) — иначе claude потеряет PATH/авторизацию/остальной контекст.
    run_env = os.environ.copy()
    if max_thinking_tokens is not None:
        run_env["MAX_THINKING_TOKENS"] = str(max_thinking_tokens)

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True,
            timeout=timeout, env=run_env,
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        raise ClaudeCliTimeout(
            f"claude --print (stream-json) timeout {timeout}s (elapsed={elapsed:.1f}s)"
        ) from e
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        # Причину — ДО stderr: генерация протокола режет сообщение до 200 знаков,
        # а при отказе API stderr пуст и вся суть лежит в потоке (см. выше).
        cause = stream_error_summary(result.stdout)
        raise ClaudeCliFailed(
            f"claude --print (stream-json) exit={result.returncode}"
            + (f" [{cause}]" if cause else "")
            + f", stderr={stderr_snippet!r}"
        )
    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise ClaudeCliFailed(
            f"claude --print (stream-json) пустой stdout (elapsed={elapsed:.1f}s)"
        )
    try:
        final = _json.loads(lines[-1])
    except _json.JSONDecodeError as e:
        raise ClaudeCliFailed(
            f"claude --print (stream-json) невалидная терминальная строка: {e}"
        ) from e
    if not isinstance(final, dict) or final.get("type") != "result":
        got = final.get("type") if isinstance(final, dict) else type(final).__name__
        raise ClaudeCliFailed(
            f"claude --print (stream-json) терминальная строка не result (type={got})"
        )
    if final.get("is_error"):
        # Раньше сюда шли первые 200 знаков `result` как есть; теперь — тем же
        # разбором, что при ненулевом коде: одна строка ошибки, без секретов.
        raise ClaudeCliFailed(
            "claude --print (stream-json) is_error: [%s]"
            % (stream_error_summary(result.stdout) or "причина не разобрана")
        )
    text = final.get("result") or ""
    if not isinstance(text, str) or not text.strip():
        raise ClaudeCliFailed("claude --print (stream-json) пустое поле result")
    stop_reason = final.get("stop_reason")
    usage = final.get("usage") or {}
    cost = final.get("total_cost_usd")
    cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    logger.debug(
        "claude --print(stream-json) ok: model=%s prompt_len=%d out_len=%d "
        "stop_reason=%s cost=$%.4f in=%d out=%d elapsed=%.1fs",
        model or "(default)", len(full_prompt), len(text), stop_reason,
        cost_usd, in_tok, out_tok, elapsed,
    )
    return ClaudeCliStreamResult(text.strip(), stop_reason, cost_usd, in_tok, out_tok, final)
