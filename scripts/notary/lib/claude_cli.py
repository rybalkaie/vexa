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
import shutil
import subprocess
import time
from typing import Optional


logger = logging.getLogger(__name__)


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
