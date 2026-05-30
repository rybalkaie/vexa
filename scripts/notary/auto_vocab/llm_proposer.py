"""Слой 2 — LLM-кандидаты в vocab после каждой встречи (Шаг 8.2).

Вызывается из pipeline finalize ПОСЛЕ успешной TG-доставки протокола
(`finalize-meeting.py`, см. интеграцию). Best-effort: на любой сбой —
WARN/ERROR-лог + skip, pipeline НЕ блокируется (РИСК4 плана). Протокол к этому
моменту уже доставлен владельцу.

LLM-канал: `lib/claude_cli.call_claude_print_json` (`claude --print`, подписка
владельца), НЕ Anthropic SDK — устоявшийся паттерн Ф4/Ф5. Стоимость берётся из
`total_cost_usd` ответа CLI (РИСК6), ANTHROPIC_API_KEY не нужен.

Контракт надёжности (РИСК4 плана):
  - timeout вызова = PROPOSER_TIMEOUT_S (дефолт 30 сек);
  - timeout / непустой exit / битый JSON → WARN-лог + skip;
  - `claude` не установлен / авторизация сломана → ERROR-лог + TG-алерт
    владельцу + skip (аналог «401/403» из плана — для CLI это «нет бинарника /
    не залогинен»);
  - провал НЕ помечается как «нужна ручная починка» — следующая встреча
    попробует заново.

Env-флаг `DISABLE_AUTO_VOCAB` (УПУ5): `1`/`true`/`yes` → proposer не зовёт
Claude вообще, сразу возвращает пустой результат. В тестах — `1`, в проде — `0`
(но до готовности applier'а Шага 8.3 держать `1`, иначе Claude зовётся каждую
встречу впустую — кандидаты пока только логируются и складываются в stash).

Шаг 8.2 НЕ применяет кандидатов (это Шаг 8.3 applier). Здесь — только
предложить, посчитать расход и сложить результат в stash
`proposed/<sid>.json`, откуда applier их заберёт.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from notary.auto_vocab import state, vocab_io

logger = logging.getLogger(__name__)

# Дефолт 120с (не 30с из плана): план предполагал быстрый Anthropic SDK, а мы
# ходим через `claude --print` CLI с заметным cold-start + загрузкой модели.
# Замер на synthetic-транскрипте: 45с — timeout, 120с — успех ($0.07/вызов).
# Proposer best-effort и зовётся ПОСЛЕ доставки протокола, так что +латентность
# не вредит пользователю. Переопределяется env PROPOSER_TIMEOUT_S.
DEFAULT_TIMEOUT_S = 120
DEFAULT_MODEL = "sonnet"  # дёшево и достаточно для извлечения терминов
MAX_TRANSCRIPT_CHARS = 60_000  # потолок на размер промта (ограничивает токены/расход)
_PROMPT_FILE = Path(__file__).with_name("proposer_prompt.txt")
_VALID_CONFIDENCE = {"high", "low"}


def is_disabled() -> bool:
    return os.environ.get("DISABLE_AUTO_VOCAB", "0").strip().lower() in {"1", "true", "yes"}


def _proposed_stash_path(session_uid: str) -> Path:
    """Куда складывать предложенных кандидатов для applier'а (Шаг 8.3)."""
    base = state.state_path().parent / "proposed"
    return base / f"{session_uid}.json"


def _load_system_prompt() -> str:
    return _PROMPT_FILE.read_text(encoding="utf-8")


def _build_context_block(transcript: str, meta: dict) -> str:
    """Динамическая часть промта: текущий словарь, blocklist, участники, текст."""
    current_terms: list[str] = []
    try:
        _data, entries = vocab_io.load_vocab()
        current_terms = [e["content"] for e in entries if isinstance(e.get("content"), str)]
    except Exception:  # noqa: BLE001
        pass
    try:
        _ad, auto_entries = vocab_io.load_auto()
        current_terms += [e["content"] for e in auto_entries if isinstance(e.get("content"), str)]
    except Exception:  # noqa: BLE001
        pass
    current_terms = sorted(set(current_terms))
    blocklist = state.rejected_blocklist()

    participants = meta.get("participants") or meta.get("expectedParticipants") or []
    if isinstance(participants, list):
        participants_str = ", ".join(str(p) for p in participants) or "(не переданы)"
    else:
        participants_str = str(participants)
    name_mapping = meta.get("name_mapping_resolved") or meta.get("nameMapping") or {}

    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[-MAX_TRANSCRIPT_CHARS:]
        trunc_note = f"(стенограмма обрезана до последних {MAX_TRANSCRIPT_CHARS} символов)\n"
    else:
        trunc_note = ""

    hints = state.boosted_pattern_hints()

    parts = [
        "=== ТЕКУЩИЙ СЛОВАРЬ (не предлагай эти термины заново) ===",
        ", ".join(current_terms) if current_terms else "(пусто)",
        "",
        "=== НЕ ПРЕДЛАГАТЬ (пользователь отклонял) ===",
        ", ".join(blocklist) if blocklist else "(пусто)",
        "",
        "=== ТИПЫ, КОТОРЫЕ ВЛАДЕЛЕЦ ОБЫЧНО ОДОБРЯЕТ (склоняйся к high) ===",
        "; ".join(hints) if hints else "(пока статистики нет)",
        "",
        "=== УЧАСТНИКИ ВСТРЕЧИ ===",
        participants_str,
        "",
        "=== РАЗРЕШЁННЫЕ ИМЕНА (name_mapping) ===",
        json.dumps(name_mapping, ensure_ascii=False) if name_mapping else "(нет)",
        "",
        "=== СТЕНОГРАММА ВСТРЕЧИ ===",
        trunc_note + transcript,
        "",
        "Верни ТОЛЬКО JSON-массив кандидатов по описанному формату.",
    ]
    return "\n".join(parts)


def _extract_json_array(text: str) -> list:
    """Достать JSON-массив из ответа Claude (срезает markdown-обрамление)."""
    s = text.strip()
    # снять ```json ... ``` если есть
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # найти первый '[' и последний ']'
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("в ответе нет JSON-массива")
    arr = json.loads(s[start : end + 1])
    if not isinstance(arr, list):
        raise ValueError("распарсенный JSON не массив")
    return arr


def _validate_and_dedup(raw_candidates: list, *, existing_keys: set[str], blocklist: set[str]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        term = item.get("term")
        if not isinstance(term, str) or not term.strip():
            continue
        term = term.strip()
        key = vocab_io.normalize(term)
        if key in existing_keys or key in seen or key in blocklist:
            continue
        conf = item.get("confidence")
        if conf not in _VALID_CONFIDENCE:
            conf = "low"  # перестраховка: неизвестный confidence → ручное подтверждение
        sounds = item.get("sounds_like")
        sounds_list = (
            [str(x).strip() for x in sounds if isinstance(x, (str, int)) and str(x).strip()]
            if isinstance(sounds, list)
            else []
        )
        reason = item.get("reason") if isinstance(item.get("reason"), str) else ""
        seen.add(key)
        out.append(
            {
                "term": term,
                "sounds_like": sounds_list,
                "confidence": conf,
                "reason": reason.strip(),
            }
        )
    return out


def propose(transcript: str, meta: dict, *, session_uid: str, timeout: int | None = None,
            model: str | None = None) -> dict:
    """Предложить кандидатов в vocab по стенограмме встречи.

    Возвращает словарь:
      {status: "disabled"|"ok"|"empty"|"error"|"timeout"|"no-cli",
       candidates: [{term, sounds_like, confidence, reason}], cost_usd: float}
    Никогда не бросает — все сбои конвертируются в статус (РИСК4).
    """
    if is_disabled():
        logger.info("[proposer] auto-vocab disabled via env (DISABLE_AUTO_VOCAB)")
        return {"status": "disabled", "candidates": [], "cost_usd": 0.0}

    if not (transcript or "").strip():
        logger.info("[proposer] пустая стенограмма — пропуск")
        return {"status": "empty", "candidates": [], "cost_usd": 0.0}

    # Idempotency (Н1 цикла): finalize может прогоняться повторно (retry-очередь,
    # ручной regenerate, второй тик). Если по этому sid уже предлагали — НЕ зовём
    # Claude заново (иначе повторный расход за тот же транскрипт).
    if _proposed_stash_path(session_uid).exists():
        logger.info("[proposer] sid=%s уже обработан (stash есть) — пропуск повторного вызова", session_uid)
        return {"status": "already-proposed", "candidates": [], "cost_usd": 0.0}

    timeout = timeout or int(os.environ.get("PROPOSER_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    model = model or os.environ.get("PROPOSER_MODEL", DEFAULT_MODEL)

    try:
        system_prompt = _load_system_prompt()
    except OSError as e:
        logger.error("[proposer] не прочитать proposer_prompt.txt: %s — skip", e)
        return {"status": "error", "candidates": [], "cost_usd": 0.0}

    context = _build_context_block(transcript, meta)

    # импорт здесь, чтобы модуль грузился даже без lib в sys.path (тесты sources)
    from notary.lib import claude_cli  # noqa: PLC0415

    try:
        result = claude_cli.call_claude_print_json(
            context, system=system_prompt, timeout=timeout, model=model
        )
    except claude_cli.ClaudeCliNotInstalled as e:
        logger.error("[proposer] claude CLI недоступен (%s) — TG-алерт + skip", e)
        _alert_owner("LLM-proposer: `claude` CLI недоступен на VPS (не установлен / не залогинен). "
                     "Авто-пополнение vocab остановлено до починки.")
        return {"status": "no-cli", "candidates": [], "cost_usd": 0.0}
    except claude_cli.ClaudeCliTimeout as e:
        logger.warning("[proposer] timeout %ss (%s) — skip, следующая встреча повторит", timeout, e)
        return {"status": "timeout", "candidates": [], "cost_usd": 0.0}
    except claude_cli.ClaudeCliError as e:
        logger.warning("[proposer] claude вызов упал (%s) — skip", e)
        return {"status": "error", "candidates": [], "cost_usd": 0.0}

    # учёт расхода — даже если кандидатов 0, вызов стоил денег (РИСК6)
    cost = result.cost_usd or 0.0
    if cost > 0:
        try:
            state.add_cost(cost)
        except Exception as e:  # noqa: BLE001
            logger.warning("[proposer] не записать cost_usd (%s) — не критично", e)

    try:
        raw = _extract_json_array(result.text)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("[proposer] не распарсить JSON-ответ (%s) — skip", e)
        return {"status": "error", "candidates": [], "cost_usd": cost}

    try:
        existing = vocab_io.combined_existing_keys()
    except Exception:  # noqa: BLE001
        existing = set()
    blocklist = {vocab_io.normalize(t) for t in state.rejected_blocklist()}
    candidates = _validate_and_dedup(raw, existing_keys=existing, blocklist=blocklist)

    n_high = sum(1 for c in candidates if c["confidence"] == "high")
    n_low = len(candidates) - n_high
    logger.info("[proposer] кандидатов: %d (high=%d low=%d), cost=$%.4f",
                len(candidates), n_high, n_low, cost)

    if candidates:
        _stash_proposed(session_uid, candidates, meta, cost)

    return {
        "status": "ok" if candidates else "empty",
        "candidates": candidates,
        "cost_usd": cost,
        "high": n_high,
        "low": n_low,
    }


def _stash_proposed(session_uid: str, candidates: list[dict], meta: dict, cost: float) -> None:
    """Сложить предложенных кандидатов для applier'а (Шаг 8.3 заберёт отсюда).

    До готовности applier'а это единственное, что происходит с low/high —
    они НЕ применяются автоматически в Шаге 8.2.
    """
    path = _proposed_stash_path(session_uid)
    payload = {
        "session_uid": session_uid,
        "series": meta.get("series"),
        "meeting_name": meta.get("name") or meta.get("title"),
        "date": meta.get("date"),
        "proposed_at": datetime.now().isoformat(timespec="seconds"),
        "cost_usd": cost,
        "candidates": candidates,
        "applied": False,  # applier (8.3) выставит True
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("[proposer] кандидаты сложены в stash: %s", path)
    except OSError as e:
        logger.warning("[proposer] не записать stash %s (%s) — не критично", path, e)


def _alert_owner(message: str) -> None:
    try:
        from notary.lib import notify  # noqa: PLC0415
        notify.push(f"⚠️ {message}")
    except Exception as e:  # noqa: BLE001
        logger.warning("[proposer] TG-алерт не отправлен (%s)", e)


def propose_from_meta(meta: dict, transcript_path: str | Path, *, session_uid: str) -> dict:
    """Удобная обёртка для pipeline: читает стенограмму из файла и зовёт propose.

    Best-effort: ошибка чтения файла → status=error, не бросает.
    """
    try:
        text = Path(transcript_path).read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[proposer] не прочитать стенограмму %s (%s) — skip", transcript_path, e)
        return {"status": "error", "candidates": [], "cost_usd": 0.0}
    return propose(text, meta, session_uid=session_uid)
