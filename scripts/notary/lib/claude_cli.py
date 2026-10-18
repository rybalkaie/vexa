"""Тонкая обёртка над `claude --print` CLI (подписка владельца).

Зачем отдельный модуль: одинаковый subprocess-паттерн нужен Ф2 (маппинг
имён), Ф4 (генерация протокола) и Ф5 (извлечение задач). Чтобы не плодить
копии вызова с одинаковой обработкой timeout/exit/stdout — выносим сюда.

Дисциплина «Опасной тройки» (CLAUDE.md проекта):
  - НЕ логируем содержимое промта/ответа.
  - Логируем только метаданные (длина промта, exit-код, elapsed, модель).
  - Модель без инструментов (`safety_args`): в подсказке чужой текст.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Optional

from .claude_output import is_events_without_result as _is_events_without_result
from .claude_output import json_values as _json_values
from .claude_output import result_envelope as _result_envelope
from .claude_output import total_input_tokens as _total_input_tokens
from .claude_workdir import claude_cwd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Без инструментов и (по выключателю) без копии разговора на диске
# ---------------------------------------------------------------------------
# В подсказке — чужой текст: расшифровка встречи, ответы участников. С включёнными
# инструментами модель в `--print` сама выполняла на сервере команды и читала файлы
# (журналы сессий за месяц: grep/cat/find/ls и чтение кода — в 7 вызовах из 198, у
# разметки имён спикеров — в 3 из 16). Ни одному вызову Ватсона инструменты не нужны:
# всё, что нужно модели, лежит в подсказке. Поэтому в каждом вызове:
#   • `--tools ""` — ни одного встроенного инструмента (CLI 2.1.301: список
#     инструментов в запросе к API пуст; настоящие подсказки Ватсона с ним отвечают
#     как раньше, одним ходом);
#   • `--strict-mcp-config` без `--mcp-config` — MCP-серверы не подключаются, даже если
#     их заведут пользователю `dev` для других служб (`--tools` MCP не касается).
# Откат без выкатки кода: `NOTARY_CLAUDE_TOOLS=default` (все встроенные, как раньше)
# или список имён через запятую.
#
# `--no-session-persistence` — клиент не пишет разговор в журнал сессии на диске
# (`~/.claude/projects/…`: там копии расшифровок и протоколов). Включается
# `NOTARY_CLAUDE_NO_SESSION_PERSISTENCE=1`; по умолчанию выключен (решает владелец).
# Без этих копий ответившую модель видно в журнале вызовов Ватсона (`_record_call`).
_TOOLS_ENV = "NOTARY_CLAUDE_TOOLS"
_NO_PERSIST_ENV = "NOTARY_CLAUDE_NO_SESSION_PERSISTENCE"
_TRUE_VALUES = ("1", "true", "yes", "on")


_TOOLS_OFF_WORDS = ("none", "off", "no", "false", "0")


def tools_value() -> str:
    """Значение `--tools`: пусто — без инструментов (по умолчанию), иначе как в переменной.

    `none`/`off`/`0` — тоже «без инструментов»: клиенту ушло бы `--tools none` (имя
    несуществующего инструмента), а в журнале вызовов это выглядело бы как запрет.
    """
    raw = os.environ.get(_TOOLS_ENV)
    if raw is None or raw.strip().lower() in _TOOLS_OFF_WORDS:
        return ""
    return raw.strip()


def session_persistence_off() -> bool:
    """Включён ли выключатель копий разговора (`--no-session-persistence`)."""
    return (os.environ.get(_NO_PERSIST_ENV) or "").strip().lower() in _TRUE_VALUES


def safety_args() -> list[str]:
    """Флаги каждого вызова: инструменты, MCP, копия разговора на диске."""
    args = ["--tools", tools_value(), "--strict-mcp-config"]
    if session_persistence_off():
        args.append("--no-session-persistence")
    return args


# ---------------------------------------------------------------------------
# Журнал вызовов: какая модель ответила
# ---------------------------------------------------------------------------
# Слово `opus`/`sonnet`/`haiku` клиент сам переводит в версию, поэтому пишем ОТВЕТ
# клиента, а не запрос: `modelUsage` (версии, потратившие токены; первой — та, что
# написала больше всего) или `model` из события `system/init`. Усилие — переданное
# флагом `--effort`: клиент его выполняет (`output_config.effort` в запросе к API), а в
# ответе его нет. Строка на вызов — в журнал службы и в файл `claude-calls.log` рядом с
# журналами Ватсона (`MEETING_NOTARY_LOG_DIR`, путь можно задать
# `NOTARY_CLAUDE_CALLS_LOG`): из финализатора сборщик переносит к себе только
# предупреждения, строки INFO там теряются. Только метаданные — ни подсказки, ни ответа.
_CALLS_LOG_ENV = "NOTARY_CLAUDE_CALLS_LOG"


def _calls_log_path() -> Optional[str]:
    explicit = (os.environ.get(_CALLS_LOG_ENV) or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    log_dir = (os.environ.get("MEETING_NOTARY_LOG_DIR") or "").strip()
    return os.path.join(os.path.expanduser(log_dir), "claude-calls.log") if log_dir else None


def answered_models(envelope: Optional[dict]) -> list[str]:
    """Версии моделей из `modelUsage` ответа клиента; первой — с наибольшим выводом."""
    usage = (envelope or {}).get("modelUsage") if isinstance(envelope, dict) else None
    if not isinstance(usage, dict):
        return []

    def _out(item: tuple) -> int:
        val = item[1].get("outputTokens") if isinstance(item[1], dict) else 0
        return val if isinstance(val, int) else 0

    return [name for name, _ in sorted(usage.items(), key=_out, reverse=True) if isinstance(name, str)]


def _record_call(
    kind: str,
    *,
    model: Optional[str],
    effort: Optional[str],
    answered: list[str],
    outcome: str,
    elapsed: Optional[float],
    in_tok: int = 0,
    out_tok: int = 0,
) -> None:
    """Строка о вызове — в журнал службы и в файл журнала вызовов. Никогда не бросает."""
    fields = [
        ("kind", kind),
        ("requested", model or "default"),
        ("effort", effort or "default"),
        # При отказе клиент называет модель, но она не ответила — другое имя поля.
        ("answered" if outcome == "ok" else "client_model", "+".join(answered) if answered else "unknown"),
        ("outcome", outcome),
        ("elapsed", f"{elapsed:.1f}s" if elapsed is not None else "?"),
        ("in", in_tok),
        ("out", out_tok),
        ("tools", tools_value() or "none"),
        ("persist", "off" if session_persistence_off() else "on"),
    ]
    line = " ".join(f"{k}={v}" for k, v in fields)
    logger.log(logging.INFO if outcome == "ok" else logging.WARNING, "[claude-cli] %s", line)
    path = _calls_log_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except OSError as e:
        logger.warning("[claude-cli] журнал вызовов не записан (%s): %s", path, type(e).__name__)


def _usage_tokens(envelope: Optional[dict]) -> tuple[int, int]:
    """(вход, выход) для журнала вызовов. Вход — вместе с кэшем: клиент кладёт почти всю
    подсказку в кэш, и `input_tokens` без него показывает 2–3 токена."""
    usage = (envelope or {}).get("usage") if isinstance(envelope, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    return _total_input_tokens(usage), int(usage.get("output_tokens", 0) or 0)


def record_failed_call(
    kind: str,
    *,
    model: Optional[str],
    outcome: str,
    elapsed: Optional[float],
    effort: Optional[str] = None,
    stdout: Optional[str] = None,
) -> None:
    """Строка о неудачном вызове (таймаут, ненулевой код) — для вызовов со своим
    subprocess (разбор ответа на вечерний блок). Никогда не бросает."""
    try:
        answered = answered_models(_result_envelope(stdout))
    except Exception:  # noqa: BLE001 — журнал не валит вызов
        answered = []
    _record_call(kind, model=model, effort=resolve_effort(model, effort), answered=answered,
                 outcome=outcome, elapsed=elapsed)


def answer_from_print_json(
    stdout: Optional[str],
    *,
    kind: str,
    model: Optional[str],
    effort: Optional[str] = None,
    elapsed: Optional[float] = None,
) -> str:
    """Текст ответа из вывода `--print --output-format json` + строка в журнал вызовов.

    Для вызовов со своим subprocess (разбор ответа на вечерний блок). Конверт с
    `is_error` → `ClaudeCliFailed` (наследник `RuntimeError`). События клиента без
    итогового `result` → `ClaudeCliFailed` (не отдаём их потребителю как ответ). Не
    конверт и не события — вывод как есть (старый клиент / подставной вывод в тестах).
    `effort=None` → по уровню модели.
    """
    eff = resolve_effort(model, effort)
    envelope = _result_envelope(stdout)
    if envelope is None:
        text = (stdout or "").strip()
        if not text:
            _record_call(kind, model=model, effort=eff, answered=[], outcome="error", elapsed=elapsed)
            raise ClaudeCliFailed(f"claude --print ({kind}) пустой stdout")
        if _is_events_without_result(text):
            _record_call(kind, model=model, effort=eff, answered=[], outcome="error", elapsed=elapsed)
            raise ClaudeCliFailed(f"claude --print ({kind}) события клиента без итогового result")
        _record_call(kind, model=model, effort=eff, answered=[], outcome="ok", elapsed=elapsed)
        return text
    in_tok, out_tok = _usage_tokens(envelope)
    answered = answered_models(envelope)
    if envelope.get("is_error"):
        _record_call(kind, model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed, in_tok=in_tok, out_tok=out_tok)
        status, code = stream_error_codes(stdout)
        raise ClaudeCliFailed(
            "claude --print (%s) is_error: [%s]"
            % (kind, stream_error_summary(stdout) or "причина не разобрана"),
            api_error_status=status, error_code=code,
            terminal_reason=stream_terminal_reason(stdout),
        )
    text = envelope.get("result")
    if not isinstance(text, str) or not text.strip():
        _record_call(kind, model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed, in_tok=in_tok, out_tok=out_tok)
        raise ClaudeCliFailed(f"claude --print ({kind}) пустое поле result")
    _record_call(kind, model=model, effort=eff, answered=answered, outcome="ok",
                 elapsed=elapsed, in_tok=in_tok, out_tok=out_tok)
    return text.strip()


def answer_from_completed(
    proc: subprocess.CompletedProcess, *, kind: str, model: Optional[str],
    elapsed: Optional[float] = None,
) -> str:
    """Ответ из завершённого своего `subprocess.run` (вывод json): код ≠ 0 → строка в
    журнал вызовов и `ClaudeCliFailed` («claude CLI rc=N [причина]: stderr»; причина — из
    stdout: при отказе API stderr пуст); иначе — `answer_from_print_json`."""
    if proc.returncode != 0:
        record_failed_call(kind, model=model, outcome="error", elapsed=elapsed, stdout=proc.stdout)
        cause = stream_error_summary(proc.stdout)
        raise ClaudeCliFailed(
            f"claude CLI rc={proc.returncode}" + (f" [{cause}]" if cause else "")
            + f": {(proc.stderr or '')[:200]}",
            terminal_reason=stream_terminal_reason(proc.stdout),
        )
    return answer_from_print_json(proc.stdout, kind=kind, model=model, elapsed=elapsed)


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


def _stream_error_events(stdout: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
    """Последнее событие `result` и последнее `assistant` с `is_api_error_message`.

    Разбирает не больше 50 последних строк потока. Общая часть для
    `stream_error_summary` (строка в журнал) и `stream_error_codes` (коды для кода).
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
    if final is None:
        # Вывод `json` при включённом `verbose` — один список событий, а не строки потока.
        items: list = []
        for obj in _json_values((stdout or "").strip()) if (stdout or "").strip() else []:
            items.extend(obj if isinstance(obj, list) else [obj])
        for ev in reversed(items[-_STREAM_TAIL_LINES:]):
            if not isinstance(ev, dict):
                continue
            if final is None and ev.get("type") == "result":
                final = ev
            elif (api_error is None and ev.get("type") == "assistant"
                  and ev.get("is_api_error_message") is True):
                api_error = ev
    return final, api_error


def stream_terminal_reason(stdout: Optional[str]) -> Optional[str]:
    """`terminal_reason` итогового события (только код `[a-z_]`), иначе None."""
    final, _ = _stream_error_events(stdout)
    val = final.get("terminal_reason") if isinstance(final, dict) else None
    return val if isinstance(val, str) and _STREAM_CODE_RE.match(val) else None


def stream_error_codes(stdout: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Коды отказа API из вывода `stream-json`: (`api_error_status`, `error`).

    Только коды — число и строка строго `[a-z_]` (как в `stream_error_summary`), без
    текста ответа модели. По ним генерация протокола отличает «модель недоступна»
    (CLI 2.1.300: снятая модель — `model_not_found` + 404; перегрузка после своих
    повторов — `server_error` + 529) от прочих сбоев. Нечего разобрать → (None, None).
    """
    final, api_error = _stream_error_events(stdout)
    status: Optional[int] = None
    code: Optional[str] = None
    for src in (api_error, final):
        if not isinstance(src, dict):
            continue
        st = src.get("api_error_status")
        if status is None and isinstance(st, int) and not isinstance(st, bool):
            status = st
        val = src.get("error")
        if code is None and isinstance(val, str) and _STREAM_CODE_RE.match(val):
            code = val
    return status, code


def stream_error_summary(stdout: Optional[str]) -> Optional[str]:
    """Короткая причина сбоя из вывода `stream-json` — без текста ответа модели.

    Разбирает не больше 50 последних строк: последнее событие `result` и последнее
    `assistant` с `is_api_error_message: true`. Возвращает, например,
    `subtype=success terminal_reason=api_error error=authentication_failed
    msg='Not logged in · Please run /login'`, или None, если разбирать нечего.
    """
    final, api_error = _stream_error_events(stdout)
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
    """Ненулевой exit, пустой stdout, прочие неуспехи.

    `api_error_status` / `error_code` — коды отказа API из потока `stream-json`, если
    их удалось разобрать (иначе None). По ним `is_model_unavailable` узнаёт «модель
    недоступна».
    """

    def __init__(
        self,
        message: str = "",
        *,
        api_error_status: Optional[int] = None,
        error_code: Optional[str] = None,
        terminal_reason: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.api_error_status = api_error_status
        self.error_code = error_code
        # Код причины остановки клиента (`malformed_tool_use_exhausted`, `api_error`, …).
        self.terminal_reason = terminal_reason


# ---------------------------------------------------------------------------
# Модель словом уровня и явное усилие
# ---------------------------------------------------------------------------
# Модель зовём СЛОВОМ уровня (`opus` / `sonnet` / `haiku`): клиент Claude Code сам
# подставляет последнюю версию уровня — номер не устаревает, а снятый номер клиент
# отвергает (404 `model_not_found`), и служба бы встала.
#
# Усилие (`--effort`) передаём ЯВНО, по уровню модели: у каждой версии своё умолчание,
# и без флага усилие менялось бы вместе с версией (проверено на CLI 2.1.300):
#   • opus → high: Opus 5.5 без флага думает на `medium`, протоколу нужен `high`;
#   • sonnet → high: умолчание Sonnet и так `high` — флаг его закрепляет;
#   • haiku → low: Haiku 5.5 без флага думает на `medium`; классификаторам хватает
#     `low` — это ближе всего к Haiku без размышлений и не дороже.
# Переопределение без выкатки кода: `NOTARY_CLAUDE_EFFORT_OPUS|SONNET|HAIKU`
# (`low`…`max`; `none` — флаг не передавать, решает клиент). Явный `effort=` у вызова
# сильнее всего. Своё имя переменной, не `CLAUDE_EFFORT`: ту читает сам клиент.
# Уровень узнаётся и по номеру (`claude-opus-4-8` → opus); модели без поддержки усилия
# (Haiku 4.5) клиент флаг молча отбрасывает — откат номером безопасен.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
TIER_DEFAULT_EFFORT = {"opus": "high", "sonnet": "high", "haiku": "low"}
_EFFORT_OFF = ("none", "off", "default")
_TIER_RE = re.compile(r"^(?:claude-)?(opus|sonnet|haiku)(?:$|[-\[])")

# «Модель недоступна»: снята/нет доступа (404, код `model_not_found`) или перегружена после
# собственных повторов клиента (529). Проверено пробами на CLI 2.1.300: при 529
# клиент делает 3 запроса за ~3 с и отдаёт `error=server_error` + `api_error_status=529`
# (код `overloaded` есть только в промежуточных `system/api_retry`), поэтому перегрузку
# узнаём по ЧИСЛУ; код `overloaded` оставлен на случай других версий клиента.
_MODEL_UNAVAILABLE_CODES = frozenset({"model_not_found", "overloaded"})
_MODEL_UNAVAILABLE_STATUSES = frozenset({404, 529})


def model_tier(model: Optional[str]) -> Optional[str]:
    """Уровень модели: `opus` / `sonnet` / `haiku` — по слову или по номеру; иначе None."""
    if not model:
        return None
    m = _TIER_RE.match(str(model).strip().lower())
    return m.group(1) if m else None


def resolve_effort(model: Optional[str], effort: Optional[str] = None) -> Optional[str]:
    """Какое `--effort` передать клиенту: явное → переменная уровня → умолчание уровня.

    None — флаг не передаётся (модель не задана / уровень не узнан / `none`).
    """
    if effort is not None and str(effort).strip():
        val = str(effort).strip().lower()
        if val in EFFORT_LEVELS:
            return val
        if val in _EFFORT_OFF:
            return None
        logger.warning("[claude-cli] усилие %r не распознано — беру по уровню модели", effort)
    tier = model_tier(model)
    if tier is None:
        return None
    env_name = f"NOTARY_CLAUDE_EFFORT_{tier.upper()}"
    raw = (os.environ.get(env_name) or "").strip().lower()
    if raw:
        if raw in EFFORT_LEVELS:
            return raw
        if raw in _EFFORT_OFF:
            return None
        logger.warning(
            "[claude-cli] %s=%r не распознано — беру %s", env_name, raw, TIER_DEFAULT_EFFORT[tier],
        )
    return TIER_DEFAULT_EFFORT[tier]


def model_args(model: Optional[str], effort: Optional[str] = None) -> list[str]:
    """`--model <m>` и `--effort <e>` для командной строки клиента (усилие — `resolve_effort`)."""
    args: list[str] = []
    if model:
        args += ["--model", model]
    eff = resolve_effort(model, effort)
    if eff:
        args += ["--effort", eff]
    return args


def is_model_unavailable(exc: BaseException) -> bool:
    """Отказ из-за самой модели (снята / нет доступа / перегружена), а не прочий сбой."""
    if not isinstance(exc, ClaudeCliFailed):
        return False
    if exc.error_code in _MODEL_UNAVAILABLE_CODES:
        return True
    return exc.api_error_status in _MODEL_UNAVAILABLE_STATUSES


# Модель без инструментов (`--tools ""`) всё же попыталась вызвать инструмент: клиент
# дважды просит повторить и сдаётся (CLI 2.1.301, проба Ф4 второго круга). Такой отказ
# лечится другой моделью/повтором, а не ожиданием — генерация протокола делает один
# проход на запасной, как при недоступной основной.
_TOOL_USE_REFUSAL_REASONS = frozenset({"malformed_tool_use_exhausted"})


def is_tool_use_refusal(exc: BaseException) -> bool:
    """Отказ «модель позвала инструмент, которого у неё нет» (`malformed_tool_use_exhausted`)."""
    if not isinstance(exc, ClaudeCliFailed):
        return False
    if getattr(exc, "terminal_reason", None) in _TOOL_USE_REFUSAL_REASONS:
        return True
    return any(r in str(exc) for r in _TOOL_USE_REFUSAL_REASONS)


def fallback_reason(exc: BaseException) -> Optional[str]:
    """Причина собрать протокол на запасной модели — или None (запасная не лечит).

    `primary-unavailable` — основная снята/нет доступа/перегружена (404/529);
    `primary-tool-refusal` — без инструментов всё же позвала инструмент.
    """
    if is_model_unavailable(exc):
        return "primary-unavailable"
    if is_tool_use_refusal(exc):
        return "primary-tool-refusal"
    return None


def call_claude_print(
    prompt: str,
    *,
    system: Optional[str] = None,
    timeout: int = 60,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> str:
    """Запускает `claude --print` через stdin, возвращает stdout (stripped).

    Параметры:
      prompt: пользовательский промт (идёт в stdin целиком).
      system: опциональная системная инструкция; склеивается с prompt через
              два перевода строки (CLI `--print` поддерживает только plain
              stdin без отдельного system-канала).
      timeout: секунды на выполнение subprocess.
      model: модель — словом уровня (`opus` / `sonnet` / `haiku`, клиент берёт
             последнюю версию) или номером. None → дефолт CLI (того, кто
             залогинен). Прокидывается в subprocess как `--model <value>`.
      effort: усилие (`low`…`max`); None → по уровню модели (`resolve_effort`).
              Прокидывается как `--effort <value>`.

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

    # Вывод JSON — ради `modelUsage` (какая модель ответила); текст ответа — поле
    # `result`, то же, что печатает текстовый вывод.
    cmd = [claude_bin, "--print", "--output-format", "json"] + safety_args()
    cmd += model_args(model, effort)
    eff = resolve_effort(model, effort)

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=claude_cwd(),
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        _record_call("print", model=model, effort=eff, answered=[], outcome="timeout", elapsed=elapsed)
        raise ClaudeCliTimeout(
            f"claude --print timeout {timeout}s (elapsed={elapsed:.1f}s)"
        ) from e
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        # Причина — из конверта в stdout (при отказе API stderr пуст), как у stream-json.
        cause = stream_error_summary(result.stdout)
        status, code = stream_error_codes(result.stdout)
        _record_call("print", model=model, effort=eff, outcome="error", elapsed=elapsed,
                     answered=answered_models(_result_envelope(result.stdout)))
        raise ClaudeCliFailed(
            f"claude --print exit={result.returncode}"
            + (f" [{cause}]" if cause else "")
            + f", stderr={stderr_snippet!r}",
            api_error_status=status, error_code=code,
            terminal_reason=stream_terminal_reason(result.stdout),
        )
    if not (result.stdout or "").strip():
        _record_call("print", model=model, effort=eff, answered=[], outcome="error", elapsed=elapsed)
        raise ClaudeCliFailed(
            f"claude --print вернул пустой stdout (elapsed={elapsed:.1f}s)"
        )
    raw = answer_from_print_json(result.stdout, kind="print", model=model, effort=effort,
                                 elapsed=elapsed)
    logger.debug(
        "claude --print ok: model=%s effort=%s prompt_len=%d output_len=%d elapsed=%.1fs",
        model or "(default)", resolve_effort(model, effort) or "(default)",
        len(full_prompt), len(raw), elapsed,
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

    __slots__ = ("text", "cost_usd", "input_tokens", "output_tokens", "raw", "answered_models")

    def __init__(self, text: str, cost_usd: float, input_tokens: int, output_tokens: int, raw: dict,
                 answered_models: Optional[list] = None):
        self.text = text
        self.cost_usd = cost_usd
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raw = raw
        self.answered_models = list(answered_models or [])


def call_claude_print_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    timeout: int = 60,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> "ClaudeCliResult":
    """Как `call_claude_print`, но через `--output-format json` — даёт стоимость.

    Зачем: Ф8 LLM-proposer должен учитывать расходы Claude API (РИСК6 плана).
    Подписочный CLI отдаёт `total_cost_usd` и `usage` в JSON-обёртке, поэтому
    SDK/ANTHROPIC_API_KEY не нужны — берём цифры отсюда. `model`/`effort` — как у
    `call_claude_print` (усилие по уровню модели, если не задано явно).

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
    cmd = [claude_bin, "--print", "--output-format", "json"] + safety_args()
    cmd += model_args(model, effort)
    eff = resolve_effort(model, effort)

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True, timeout=timeout,
            cwd=claude_cwd(),
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        _record_call("json", model=model, effort=eff, answered=[], outcome="timeout", elapsed=elapsed)
        raise ClaudeCliTimeout(
            f"claude --print --output-format json timeout {timeout}s (elapsed={elapsed:.1f}s)"
        ) from e
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        cause = stream_error_summary(result.stdout)
        status, code = stream_error_codes(result.stdout)
        _record_call("json", model=model, effort=eff, outcome="error", elapsed=elapsed,
                     answered=answered_models(_result_envelope(result.stdout)))
        raise ClaudeCliFailed(
            f"claude --print (json) exit={result.returncode}"
            + (f" [{cause}]" if cause else "")
            + f", stderr={stderr_snippet!r}",
            api_error_status=status, error_code=code,
            terminal_reason=stream_terminal_reason(result.stdout),
        )
    raw_out = (result.stdout or "").strip()
    if not raw_out:
        _record_call("json", model=model, effort=eff, answered=[], outcome="error", elapsed=elapsed)
        raise ClaudeCliFailed(f"claude --print (json) пустой stdout (elapsed={elapsed:.1f}s)")
    # Конверт — и когда клиент напечатал что-то до/после него или выдал список событий
    # (`verbose` в настройках); нет итогового `result` — отказ, как раньше.
    envelope = _result_envelope(raw_out)
    if envelope is None:
        try:
            envelope = _json.loads(raw_out)
        except _json.JSONDecodeError as e:
            _record_call("json", model=model, effort=eff, answered=[], outcome="error", elapsed=elapsed)
            raise ClaudeCliFailed(f"claude --print (json) невалидный JSON-конверт: {e}") from e
        if not isinstance(envelope, dict):
            _record_call("json", model=model, effort=eff, answered=[], outcome="error", elapsed=elapsed)
            raise ClaudeCliFailed("claude --print (json) конверт не объект")

    usage = envelope.get("usage") or {}
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    log_in = _total_input_tokens(usage)
    answered = answered_models(envelope)
    if envelope.get("is_error"):
        # Отказ с кодом 0 (так бывает у ошибки API): строка ошибки — не ответ модели.
        _record_call("json", model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed, in_tok=log_in, out_tok=out_tok)
        status, code = stream_error_codes(result.stdout)
        raise ClaudeCliFailed(
            "claude --print (json) is_error: [%s]"
            % (stream_error_summary(result.stdout) or "причина не разобрана"),
            api_error_status=status, error_code=code,
            terminal_reason=stream_terminal_reason(result.stdout),
        )
    text = envelope.get("result") or ""
    if not isinstance(text, str) or not text.strip():
        _record_call("json", model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed, in_tok=log_in, out_tok=out_tok)
        raise ClaudeCliFailed("claude --print (json) пустое поле result")
    cost = envelope.get("total_cost_usd")
    cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0
    _record_call("json", model=model, effort=eff, answered=answered, outcome="ok",
                 elapsed=elapsed, in_tok=log_in, out_tok=out_tok)
    logger.debug(
        "claude --print(json) ok: model=%s effort=%s prompt_len=%d out_len=%d cost=$%.4f "
        "in=%d out=%d elapsed=%.1fs",
        model or "(default)", eff or "(default)",
        len(full_prompt), len(text), cost_usd, in_tok, out_tok, elapsed,
    )
    return ClaudeCliResult(text.strip(), cost_usd, in_tok, out_tok, envelope, answered)


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
      answered_models — версии, которые ответили (`modelUsage`, иначе `model` из init).
      effort — усилие, переданное флагом (`None` — флаг не передавался).
    """

    __slots__ = ("text", "stop_reason", "cost_usd", "input_tokens", "output_tokens", "raw",
                 "answered_models", "effort")

    def __init__(
        self, text: str, stop_reason: Optional[str], cost_usd: float,
        input_tokens: int, output_tokens: int, raw: dict,
        answered_models: Optional[list] = None, effort: Optional[str] = None,
    ):
        self.text = text
        self.stop_reason = stop_reason
        self.cost_usd = cost_usd
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raw = raw
        self.answered_models = list(answered_models or [])
        self.effort = effort


def call_claude_print_stream_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    timeout: int = 60,
    model: Optional[str] = None,
    max_thinking_tokens: Optional[int] = None,
    effort: Optional[str] = None,
) -> "ClaudeCliStreamResult":
    """Как `call_claude_print`, но через `--output-format stream-json --verbose`.

    `model`/`effort` — как у `call_claude_print` (усилие по уровню модели, если не
    задано явно). Отказ API (ненулевой код или `is_error`) несёт коды в
    `ClaudeCliFailed.api_error_status` / `.error_code` — по ним генерация протокола
    узнаёт «модель недоступна» (`is_model_unavailable`).

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
    к API — подтверждено статическим разбором CLI, ветка `qe===0`). Моделям 5.5,
    у которых размышление не выключается (API ответил бы 400), клиент его не
    шлёт: на CLI 2.1.300 `opus`/`sonnet` с `MAX_THINKING_TOKENS=0` отвечают без
    ошибки.
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
    cmd += safety_args() + model_args(model, effort)
    eff = resolve_effort(model, effort)

    # env копируем целиком (subprocess.run(env=...) ЗАМЕНЯЕТ окружение, не
    # мёржит) — иначе claude потеряет PATH/авторизацию/остальной контекст.
    run_env = os.environ.copy()
    if max_thinking_tokens is not None:
        run_env["MAX_THINKING_TOKENS"] = str(max_thinking_tokens)

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True,
            timeout=timeout, env=run_env, cwd=claude_cwd(),
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        _record_call("stream", model=model, effort=eff, answered=[], outcome="timeout", elapsed=elapsed)
        raise ClaudeCliTimeout(
            f"claude --print (stream-json) timeout {timeout}s (elapsed={elapsed:.1f}s)"
        ) from e
    elapsed = time.monotonic() - started
    answered = _stream_answered_models(result.stdout)

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        # Причину — ДО stderr: генерация протокола режет сообщение до 200 знаков,
        # а при отказе API stderr пуст и вся суть лежит в потоке (см. выше).
        cause = stream_error_summary(result.stdout)
        status, code = stream_error_codes(result.stdout)
        _record_call("stream", model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed)
        raise ClaudeCliFailed(
            f"claude --print (stream-json) exit={result.returncode}"
            + (f" [{cause}]" if cause else "")
            + f", stderr={stderr_snippet!r}",
            api_error_status=status, error_code=code,
            terminal_reason=stream_terminal_reason(result.stdout),
        )
    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        _record_call("stream", model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed)
        raise ClaudeCliFailed(
            f"claude --print (stream-json) пустой stdout (elapsed={elapsed:.1f}s)"
        )
    try:
        final = _json.loads(lines[-1])
    except _json.JSONDecodeError as e:
        _record_call("stream", model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed)
        raise ClaudeCliFailed(
            f"claude --print (stream-json) невалидная терминальная строка: {e}"
        ) from e
    if not isinstance(final, dict) or final.get("type") != "result":
        got = final.get("type") if isinstance(final, dict) else type(final).__name__
        _record_call("stream", model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed)
        raise ClaudeCliFailed(
            f"claude --print (stream-json) терминальная строка не result (type={got})"
        )
    if final.get("is_error"):
        # Раньше сюда шли первые 200 знаков `result` как есть; теперь — тем же
        # разбором, что при ненулевом коде: одна строка ошибки, без секретов.
        status, code = stream_error_codes(result.stdout)
        _record_call("stream", model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed)
        raise ClaudeCliFailed(
            "claude --print (stream-json) is_error: [%s]"
            % (stream_error_summary(result.stdout) or "причина не разобрана"),
            api_error_status=status, error_code=code,
            terminal_reason=stream_terminal_reason(result.stdout),
        )
    text = final.get("result") or ""
    if not isinstance(text, str) or not text.strip():
        _record_call("stream", model=model, effort=eff, answered=answered, outcome="error",
                     elapsed=elapsed)
        raise ClaudeCliFailed("claude --print (stream-json) пустое поле result")
    stop_reason = final.get("stop_reason")
    usage = final.get("usage") or {}
    cost = final.get("total_cost_usd")
    cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    _record_call("stream", model=model, effort=eff, answered=answered, outcome="ok",
                 elapsed=elapsed, in_tok=_total_input_tokens(usage), out_tok=out_tok)
    logger.debug(
        "claude --print(stream-json) ok: model=%s effort=%s prompt_len=%d out_len=%d "
        "stop_reason=%s cost=$%.4f in=%d out=%d elapsed=%.1fs",
        model or "(default)", eff or "(default)",
        len(full_prompt), len(text), stop_reason, cost_usd, in_tok, out_tok, elapsed,
    )
    return ClaudeCliStreamResult(text.strip(), stop_reason, cost_usd, in_tok, out_tok, final,
                                 answered, eff)


def _stream_answered_models(stdout: Optional[str]) -> list[str]:
    """Кто ответил в выводе `stream-json`: `modelUsage` итогового события, иначе
    `model` из `system/init`. Читает только эти поля — текст ответа не трогает."""
    final, _ = _stream_error_events(stdout)
    models = answered_models(final)
    if models:
        return models
    for ln in (stdout or "").splitlines()[:5]:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
            m = ev.get("model")
            return [m] if isinstance(m, str) and m else []
    return []
