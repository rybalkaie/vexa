# -*- coding: utf-8 -*-
"""Ф2 (ISS-16, телеметрия ярусов правки): счётчики `tier` точечной правки протокола.

Зачем (РИСК4 плана). Патч-путь (Ф2) на разговорной диктовке может часто не ложиться
(парафраз якоря) → фолбэк на регенерацию с уведомлением «⚠️ Не смог поправить точечно».
Если success-rate низкий, владелец привыкнет игнорировать ⚠️. Чтобы видеть РЕАЛЬНЫЙ
success-rate за окно боя (а не гадать), на каждом доставленном перевыпуске пишем,
каким ЯРУСОМ он закрылся, и умеем агрегировать счётчики:
  • `patch_applied`   — закрыт патч-путём (tier=patch);
  • `patch_rejected`  — патч пробовали, но он не лёг → ушло в регенерацию;
  • `regen_fallback`  — закрыт регенерацией (вкл. случаи без попытки патча);
  • `deterministic`   — закрыт детерминированным ярусом (Ф1/Ф4);
Низкий `patch_applied/(patch_applied+patch_rejected)` — триггер пересмотра промпта/
валидатора (не слепое «и так сойдёт»).

Опасная тройка (R8). Пишем ТОЛЬКО метаданные: время (ISO), ярус, флаг «пробовали
патч». НИ строки протокола/реплики/правки. Хранилище — append-only JSONL под
feedback-dir (как `feedback_learning`), best-effort: сбой записи НЕ валит перевыпуск.

stdlib-only (системный python3.9 listener без venv).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import feedback_state

logger = logging.getLogger("notary.patch_telemetry")

TELEMETRY_DIRNAME = "_telemetry"
TIERS_LOG_NAME = "patch-tiers.jsonl"

# Допустимые ярусы финального исхода перевыпуска.
TIERS = ("deterministic", "patch", "regen")


def tiers_log_path(*, root: Optional[Path] = None) -> Path:
    """`<feedback_dir>/_telemetry/patch-tiers.jsonl` — один файл на инстанс."""
    root = root or feedback_state.resolve_feedback_dir()
    return Path(root) / TELEMETRY_DIRNAME / TIERS_LOG_NAME


def record_tier(
    tier: str,
    *,
    patch_attempted: bool = False,
    root: Optional[Path] = None,
) -> bool:
    """Дописывает событие исхода `{at, tier, patch_attempted}` (append-only).

    `tier` ∈ {deterministic, patch, regen}. `patch_attempted` — пробовали ли Ф2-патч
    (нужно, чтобы отличить regen-после-отказа-патча от regen-без-попытки). Best-effort:
    любой сбой → warning + False, перевыпуск не падает. R8: текста реплик НЕ пишем.
    """
    if tier not in TIERS:
        return False
    try:
        rec = {
            "at": feedback_state.now_iso(),
            "tier": tier,
            "patch_attempted": bool(patch_attempted),
        }
        p = tiers_log_path(root=root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        logger.warning("[patch-tlm] запись tier не удалась (non-fatal): %s", e)
        return False


def aggregate(*, root: Optional[Path] = None, window_days: Optional[int] = 7) -> dict:
    """Свёртка событий в счётчики за окно (по умолчанию 7 дней; None → всё время).

    Возвращает `{patch_applied, patch_rejected, regen_fallback, deterministic, total,
    patch_success_rate}`:
      • patch_applied   = #(tier==patch)
      • patch_rejected  = #(tier==regen И patch_attempted)
      • regen_fallback  = #(tier==regen)            (вкл. без попытки патча)
      • deterministic   = #(tier==deterministic)
      • patch_success_rate = patch_applied/(patch_applied+patch_rejected) или None.
    Битые строки/нет файла → нули. Best-effort чтение.
    """
    counts = {"patch_applied": 0, "patch_rejected": 0, "regen_fallback": 0,
              "deterministic": 0, "total": 0}
    p = tiers_log_path(root=root)
    if not p.is_file():
        return {**counts, "patch_success_rate": None}
    cutoff = None
    if window_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {**counts, "patch_success_rate": None}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if cutoff is not None:
            at = feedback_state._parse_iso(rec.get("at"))
            if at is not None and at < cutoff:
                continue
        tier = rec.get("tier")
        if tier not in TIERS:
            continue
        counts["total"] += 1
        if tier == "patch":
            counts["patch_applied"] += 1
        elif tier == "deterministic":
            counts["deterministic"] += 1
        elif tier == "regen":
            counts["regen_fallback"] += 1
            if rec.get("patch_attempted"):
                counts["patch_rejected"] += 1
    denom = counts["patch_applied"] + counts["patch_rejected"]
    rate = (counts["patch_applied"] / denom) if denom else None
    return {**counts, "patch_success_rate": rate}
