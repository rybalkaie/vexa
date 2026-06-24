"""Ф2 / R4 (план `2026-06-24-vtoroy-mozg-ai-klon`): забор РЕШЕНИЙ из протокола
встречи в журнал решений второго мозга (`ai-clone/decisions/`).

Третий источник журнала (A6/РИСК2): протоколы встреч живут на VPS, локального
SessionEnd там нет → отдельный забор НА ФИНАЛИЗАЦИИ протокола. Этот модуль —
ПОРТАТИВНОЕ ЯДРО забора (как `session-end-collector.py` для сессий Claude Code):
скраб → промпт извлечения. Спавн `claude -p` и транспорт черновика mac←VPS — на
АКТИВАЦИИ (прод-деплой нотариуса, control-gate владельца) — см. DEPLOY-handoff Д1.4.

Состояние: DORMANT — модуль НЕ вызывается из живого пути финализации. Реализован
полностью (скраб fail-closed + промпт + запись очереди), готов к активации.

🔒 Приватность (Опасная тройка + R18): протокол скрабится ОБЩИМ `secret_scrub` ДО
любого LLM-прохода и ДО любой записи; остаточный секрет → ОТКАЗ (fail-closed, как
коллектор). Текст протокола в логи не уходит — только метаданные (серия/дата/итог).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional

# Путь записи журнала на маке (куда транспорт доставит черновик). Здесь — только
# для инструкции в промпте; модуль на VPS напрямую в `me/` НЕ пишет (транспорт-
# очередь, Д1.4 step 3).
DECISIONS_DIR = "~/Projects/me/ai-clone/decisions"
DECISIONS_README = "~/Projects/me/ai-clone/decisions/README.md"
CONVENTION = "~/Projects/me/ai-clone/knowledge-unit-convention.md"

# Транспорт-очередь черновиков встреч (mac←VPS). Существующий синк/зеркало забирает
# (Д1.4). Имя — латиницей, как остальные служебные очереди мозга.
DEFAULT_QUEUE_REL = "_inbox/notary-decision-drafts.jsonl"


# --- Разрешение secret_scrub (портативно: notary/lib после деплоя, hooks на dev) --

def _resolve_scrub() -> Optional[Callable[[str], str]]:
    """Вернуть функцию `scrub(text)->text` или None (fail-closed у вызывателя).

    Порядок: (1) `notary.lib.secret_scrub` — куда модуль КОПИРУЕТСЯ на VPS при
    активации (Д1.4 step 1); (2) dev/test-фолбэк `~/.claude/hooks/lib` (общий
    исходник `secret_scrub.py`). Нет нигде → None (отказ, а не тихий пропуск)."""
    mod = None
    try:
        from . import secret_scrub as mod  # type: ignore  # после деплоя на VPS
    except Exception:  # noqa: BLE001
        mod = None
    if mod is None:
        # dev/test-фолбэк: общий модуль живёт в hooks/lib (источник истины Ф1).
        hooks_lib = os.path.expanduser("~/.claude/hooks/lib")
        if hooks_lib not in sys.path:
            sys.path.insert(0, hooks_lib)
        try:
            import secret_scrub as mod  # type: ignore
        except Exception:  # noqa: BLE001
            return None
    scrub_fn = getattr(mod, "scrub", None)
    contains = getattr(mod, "contains_secret", None)
    if not callable(scrub_fn):
        return None

    def _scrub(text: str) -> str:
        res = scrub_fn(text)
        out = getattr(res, "text", res)  # ScrubResult.text | str
        # Бэкстоп: если после скраба секрет всё ещё детектится — пустая строка,
        # вызыватель трактует как отказ (никогда не отдаём подозрительный текст в LLM).
        if callable(contains) and contains(out):
            return ""
        return out

    return _scrub


# --- Промпт извлечения (зеркало session-end-collector, адаптация под протокол) ---

def build_extraction_prompt(scrubbed_protocol: str, *, series: Optional[str], date: Optional[str]) -> str:
    """Промпт для `claude -p`: извлечь ОДНО осмысленное решение из СКРАБЛЕННОГО
    протокола встречи и выдать ЧЕРНОВИК записи журнала в stdout (НЕ писать файл —
    на VPS папки `me/` нет; транспорт заберёт stdout, Д1.4). Иначе — «решение не
    выявлено». Формат записи — как у коллектора сессий (единый decision_record)."""
    sid = f"{series or '?'} / {date or '?'}"
    return (
        "Ты — фоновый авто-сборщик ЖУРНАЛА РЕШЕНИЙ второго мозга Ильи. Ниже —"
        " СКРАБЛЕННЫЙ протокол встречи (секреты уже вырезаны детерминированным"
        " secret-scrub). Встреча: " + sid + ".\n\n"
        "ФОРМАТ записи и правила (открой на маке при доставке): "
        f"{DECISIONS_README} + {CONVENTION}.\n\n"
        "ЗАДАЧА:\n"
        "1) Прочитай протокол (блоки «Решения»/«Задачи»/темы).\n"
        "2) Реши: было ли на встрече ОСМЫСЛЕННОЕ РЕШЕНИЕ? Осмысленное = любое из:"
        " меняет направление / задействует деньги-людей-ресурсы; захочется вспомнить"
        " ПОЧЕМУ через месяц; раскрывает, КАК Илья выбирает. Протокольная рутина без"
        " причины-для-памяти — НЕ годится.\n"
        "3) ЕСЛИ годное решение есть — выведи В STDOUT ОДИН ЧЕРНОВИК строго по"
        " формату README: frontmatter (тип: решение, scope: клон-когнитивный,"
        " статус: черновик, дата: " + (date or "YYYY-MM-DD") + ", источник: встреча"
        " (протокол), актор: агент, уверенность 0..1 + калибровочные поля"
        " предсказанный-исход/confidence-прогноза/исход:null/review_date) и тело"
        " ## Ситуация / ## Выбор / ## Почему / ## Исход (Исход пустой). Захвати"
        " именно ЛОГИКУ (ПОЧЕМУ так решили). Несколько решений — выбери ОДНО"
        " самое значимое.\n"
        "4) ЕСЛИ годного решения нет — выведи ровно «решение не выявлено».\n\n"
        "БЕЗОПАСНОСТЬ: даже если что-то секрето-подобное проскочило — НЕ переноси"
        " значения ключей/паролей/токенов в запись. Подтверждений не спрашивай,"
        " Telegram не трогай, циклы проверок не запускай.\n\n"
        "=== СКРАБЛЕННЫЙ ПРОТОКОЛ ===\n" + scrubbed_protocol
    )


def prepare_extraction(
    protocol_text: str,
    *,
    series: Optional[str] = None,
    date: Optional[str] = None,
    scrub: Optional[Callable[[str], str]] = None,
) -> dict:
    """Скраб (fail-closed) → промпт извлечения. НЕ спавнит LLM (это активация).

    Возвращает `{ok, reason, prompt?, scrubbed?}`:
      • scrub недоступен → ok=False, reason="scrub-unavailable" (отказ, не тихо);
      • после скраба пусто/остаточный секрет → ok=False, reason="residual-secret";
      • иначе ok=True + prompt (для `claude -p`) + scrubbed.
    """
    if not protocol_text or not protocol_text.strip():
        return {"ok": False, "reason": "empty-protocol"}
    scrub_fn = scrub or _resolve_scrub()
    if scrub_fn is None:
        return {"ok": False, "reason": "scrub-unavailable"}
    try:
        scrubbed = scrub_fn(protocol_text)
    except Exception as e:  # noqa: BLE001 — любой сбой скраба = отказ (fail-closed)
        return {"ok": False, "reason": f"scrub-error:{type(e).__name__}"}
    if not scrubbed or not scrubbed.strip():
        return {"ok": False, "reason": "residual-secret"}
    return {
        "ok": True,
        "reason": "ready",
        "prompt": build_extraction_prompt(scrubbed, series=series, date=date),
        "scrubbed": scrubbed,
    }


def to_queue_record(draft_md: str, *, series: Optional[str], date: Optional[str],
                    meeting_sid: Optional[str] = None) -> dict:
    """Обернуть извлечённый ЧЕРНОВИК в запись транспорт-очереди (mac←VPS, Д1.4).

    Минимальная схема для синка: источник, серия/дата, статус-черновик, тело.
    Реальная запись в `ai-clone/decisions/` создаётся на маке при доставке."""
    return {
        "source": "meeting-protocol",
        "series": series,
        "date": date,
        "meeting_sid": meeting_sid,
        "status": "черновик",
        "draft_md": draft_md,
    }


def queue_path(me_dir: Optional[str] = None) -> Path:
    """Путь транспорт-очереди черновиков встреч. `me_dir` дефолтит на ~/Projects/me
    (на VPS — зеркало, env MEETING_NOTARY_ME_DIR, как vocab-sources)."""
    base = me_dir or os.environ.get("MEETING_NOTARY_ME_DIR") or os.path.expanduser("~/Projects/me")
    return Path(base).expanduser() / DEFAULT_QUEUE_REL
