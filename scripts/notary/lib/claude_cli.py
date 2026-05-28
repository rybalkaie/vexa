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
