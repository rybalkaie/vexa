"""Слой 3 (minimal) — применение LLM-кандидатов (Шаг 8.3-minimal).

Поштучный выбор («🔧 Выбрать») в этой версии НЕ реализован (решение владельца —
backlog). Только две ветки:
  - **high** → авто-добавление в авто-словарь (`vocab_io.add_to_auto`) + silent
    TG-уведомление владельцу (через `notify.push`, без кнопок);
  - **low** → запрос в TG (`@ilya_protocol_meeting_bot`) с двумя кнопками
    `[✅ Все] / [❌ Никого]`; pending-state кладётся в `pending_root/vocab/<sid>.json`,
    callback обрабатывает `lib/vocab_worker.py`.

Применяется из pipeline finalize ПОСЛЕ proposer'а. Stash proposer'а
(`proposed/<sid>.json`) после обработки удаляется (high применён, low уехал в
pending-state). Всё best-effort: ошибки логируются, pipeline не падает.

Запись в авто-словарь идёт через `vocab_io.add_to_auto` (под flock) — безопасно
при гонке с sources-таймером и callback-применением.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from notary.auto_vocab import llm_proposer, state, vocab_io

logger = logging.getLogger(__name__)

DEFAULT_CHAT_ID = 359008340  # личка Ильи (fallback)


def _notarius_token() -> str | None:
    return os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")


def _notarius_chat_id() -> int:
    raw = os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    try:
        return int(raw) if raw else DEFAULT_CHAT_ID
    except (TypeError, ValueError):
        return DEFAULT_CHAT_ID


def _pending_dir_root() -> Path | None:
    """Корень pending (тот же, что слушает listener) — НЕ подкаталог vocab."""
    try:
        from notary.lib import clarify_state  # noqa: PLC0415
        return clarify_state.resolve_pending_dir()
    except Exception as e:  # noqa: BLE001
        logger.warning("[applier] pending_root недоступен (%s)", e)
        return None


def pending_path(pending_root: Path, session_uid: str) -> Path:
    """State-файл vocab-запроса: <pending_root>/vocab/<sid>.json."""
    return pending_root / "vocab" / f"{session_uid}.json"


# ── применение терминов (используется и applier'ом, и vocab_worker'ом) ──

def commit_terms(candidates: list[dict], *, count_as_approved: bool = True) -> list[str]:
    """Добавить термины в авто-словарь. Возвращает реально добавленные `content`.

    `count_as_approved` (Н1 цикла): True — это РУЧНОЕ одобрение владельца
    (callback `[✅ Все]`) → бампим `approved` + `approved_patterns` (бустинг
    confidence). False — это HIGH-авто-добавление (applier без спроса) → НЕ
    считаем за одобрение: иначе дайджест «ты одобрил K» завышается, а паттерны
    самоусиливаются без человека (runaway к high). High учитывается отдельным
    счётчиком `auto_added` в вызывающем коде.
    """
    if not candidates:
        return []
    # Кандидаты proposer'а используют ключ `term`; vocab_io ждёт `content`.
    entries = [
        {"content": c["term"], "sounds_like": c.get("sounds_like") or []}
        for c in candidates
        if isinstance(c.get("term"), str) and c["term"].strip()
    ]
    added = vocab_io.add_to_auto(entries)
    names = [e["content"] for e in added]
    if names and count_as_approved:
        try:
            state.bump_weekly("approved", len(names))
            for n in names:
                state.add_approved_pattern(state.classify_term_pattern(n))
        except Exception as e:  # noqa: BLE001
            logger.warning("[applier] статистика approved не обновлена (%s)", e)
    return names


def reject_terms(candidates: list[dict]) -> None:
    """Отклонить кандидатов: бамп rejected_terms (порог 3 → не предлагать) +
    недельный счётчик. Используется на `[❌ Никого]` и на timeout sweep."""
    n = 0
    for c in candidates:
        term = c.get("term") or c.get("content")
        if term:
            try:
                state.add_rejected(term)
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[applier] reject %r не записан (%s)", term, e)
    if n:
        try:
            state.bump_weekly("rejected", n)
        except Exception as e:  # noqa: BLE001
            logger.warning("[applier] статистика rejected не обновлена (%s)", e)


# ── основной вход: применить предложение по встрече ──

def apply_proposal(session_uid: str, meta: dict, *, pending_root: Path | None = None) -> dict:
    """Прочитать stash proposer'а и применить: high→авто+silent TG, low→TG-запрос.

    Best-effort, не бросает. Возвращает summary.
    """
    stash = llm_proposer._proposed_stash_path(session_uid)
    if not stash.exists():
        return {"status": "no-stash", "high_added": [], "low_sent": 0}
    try:
        payload = json.loads(stash.read_text(encoding="utf-8"))
        candidates = payload.get("candidates") or []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[applier] stash %s битый (%s) — пропуск", stash, e)
        return {"status": "bad-stash", "high_added": [], "low_sent": 0}

    high = [c for c in candidates if c.get("confidence") == "high"]
    low = [c for c in candidates if c.get("confidence") != "high"]

    high_added: list[str] = []
    if high:
        # count_as_approved=False — high это АВТО без спроса, не «одобрение».
        high_added = commit_terms(high, count_as_approved=False)
        if high_added:
            try:
                state.bump_weekly("auto_added", len(high_added))
            except Exception as e:  # noqa: BLE001
                logger.warning("[applier] auto_added не обновлён (%s)", e)
            _notify_high(high, high_added)
            # D1 (Ф7): ДОЛГОВЕЧНОЕ знание адресуем КОНТЕКСТУ КОМПАНИИ (не коду) —
            # предложение в `*-context/knowledge/notary/glossary.yaml` через PR
            # (контракт §3.3). Локальный ASR-словарь (commit_terms выше) остаётся —
            # это немедленная проекция распознавания; контекст — источник истины
            # знания. Слой решает knowledge_router (company/private/drop). Best-effort.
            _writeback_high_terms(session_uid, meta, high, high_added)

    low_sent = 0
    if low:
        if _send_low_request(session_uid, meta, low, pending_root=pending_root):
            low_sent = len(low)
            try:
                state.bump_weekly("requested", len(low))
            except Exception as e:  # noqa: BLE001
                logger.warning("[applier] requested не обновлён (%s)", e)
        else:
            # Н2 цикла: low-запрос не ушёл (TG down). НЕ оставляем сиротский stash
            # (proposer-guard заблокировал бы повтор). Удаляем stash → на повторном
            # finalize той же встречи proposer честно перезапросит и перешлёт.
            # low «теряются» только для этого прогона — это уточняющие кандидаты,
            # термины повторятся на след. встречах. Логируем явно.
            lost = ", ".join(c.get("term", "?") for c in low)
            logger.warning("[applier] low-запрос не отправлен (TG?) — дропнул low: %s", lost)
            try:
                stash.unlink()
            except OSError:
                pass
            return {"status": "low-send-failed", "high_added": high_added, "low_sent": 0}

    # high применён, low уехал в pending-state (или его не было) → stash больше не нужен
    try:
        stash.unlink()
    except OSError:
        pass
    logger.info("[applier] sid=%s: high+%d low→TG %d", session_uid, len(high_added), low_sent)
    return {"status": "ok", "high_added": high_added, "low_sent": low_sent}


def _meeting_present(meta: dict) -> list:
    """Лучший доступный «кто присутствовал» из meta proposer'а — для
    публикационного гейта (route_for_meeting). Best-effort, без сырья."""
    for k in ("participants", "expectedParticipants", "present_participants"):
        v = meta.get(k)
        if isinstance(v, list) and v:
            return [str(x) for x in v]
    return []


def _writeback_high_terms(session_uid: str, meta: dict, high: list[dict], added: list[str]) -> None:
    """D1: предложить авто-добавленные high-термины в контекст компании (PR, §3.3).

    Слой (company/private) решает `knowledge_router` по серии встречи и
    публикационному гейту Ф6 — креды уже отфильтрованы (D5). Сбой не валит pipeline.
    """
    try:
        from notary.lib import knowledge_writeback  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.info("[applier] knowledge_writeback недоступен (%s) — пропуск write-back", e)
        return
    series = meta.get("series")
    present = _meeting_present(meta)
    source = {"series": series, "date": meta.get("date"), "feedback_id": session_uid}
    addset = {a.lower() for a in added}
    routed = {"company": 0, "private": 0, "drop": 0, "exists": 0}
    for c in high:
        term = (c.get("term") or "").strip()
        if not term or term.lower() not in addset:
            continue
        try:
            res = knowledge_writeback.propose_term(
                term, series=series, aliases=c.get("sounds_like") or [],
                note=(c.get("reason") or None), present_participants=present,
                source=source,
            )
            routed[res.layer] = routed.get(res.layer, 0) + 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[applier] write-back термина не удался (non-fatal): %s", e)
    logger.info("[applier] write-back high: company=%d private=%d drop=%d exists=%d",
                routed.get("company", 0), routed.get("private", 0),
                routed.get("drop", 0), routed.get("exists", 0))


def _notify_high(high: list[dict], added: list[str]) -> None:
    if not added:
        return
    reasons = "; ".join(
        f"{c['term']} — {c.get('reason', '')}".strip(" —")
        for c in high if c["term"] in added
    )[:500]
    msg = f"🟢 Авто-добавил в словарь без подтверждения: {', '.join(added)}.\nПричины: {reasons}"
    try:
        from notary.lib import notify  # noqa: PLC0415
        notify.push(msg, silent=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[applier] silent-уведомление не отправлено (%s)", e)


def _send_low_request(session_uid: str, meta: dict, low: list[dict], *,
                      pending_root: Path | None = None) -> bool:
    token = _notarius_token()
    if not token:
        logger.warning("[applier] нет TELEGRAM_NOTARIUS_BOT_TOKEN — low-запрос не отправить")
        return False
    root = pending_root or _pending_dir_root()
    if root is None:
        return False

    meeting_name = meta.get("name") or meta.get("title") or meta.get("series") or session_uid
    lines = [f"🆕 Кандидаты в словарь после встречи «{meeting_name}»:"]
    for c in low:
        sl = f" (sounds_like: {', '.join(c['sounds_like'])})" if c.get("sounds_like") else ""
        lines.append(f"• {c['term']}{sl}")
    lines.append("\nЧто добавить?")
    text = "\n".join(lines)

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Все", "callback_data": f"vocab:approve_all:{session_uid}"},
            {"text": "❌ Никого", "callback_data": f"vocab:reject_all:{session_uid}"},
        ]]
    }
    chat_id = _notarius_chat_id()

    # Н3 цикла: pending-state пишем ДО отправки (без message_id). Если записать
    # не вышло — НЕ шлём кнопки, которые некому обработать. message_id допишем
    # после успешной отправки (для edit; edit и так best-effort).
    pend = pending_path(root, session_uid)
    payload = {
        "session_uid": session_uid,
        "meeting_name": meeting_name,
        "candidates": low,
        "chat_id": chat_id,
        "message_id": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
    }
    try:
        pend.parent.mkdir(parents=True, exist_ok=True)
        pend.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("[applier] pending-state %s не записан (%s) — кнопки не шлём", pend, e)
        return False

    try:
        from notary.lib import telegram_api  # noqa: PLC0415
        result = telegram_api.send_message(token, chat_id, text, reply_markup=keyboard)
        message_id = result.get("message_id")
    except Exception as e:  # noqa: BLE001
        logger.warning("[applier] sendMessage low-запроса упал (%s) — откатываю pending", e)
        try:
            pend.unlink()
        except OSError:
            pass
        return False

    # дописать message_id (для будущего editMessageText). Не вышло — не критично.
    payload["message_id"] = message_id
    try:
        pend.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("[applier] message_id не дописан в pending (%s) — edit будет пропущен", e)
    return True
