# -*- coding: utf-8 -*-
"""Ф4 (план `2026-07-20-watson-smart-replies-rethink.md`, R13-R16/R26): ЕДИНЫЙ сборщик
ack + нумерованный переспрос + разбор ответа человека на ack/переспрос бота.

Ф3 отдала СТРУКТУРУ решения (`reply_understand.understand_reply` → applied/questions/
edits/config/unclear). Тексты пользовательских ответов рендерит ЭТОТ модуль (граница
ЗАВ1). Один reply раскладывается на честные части (R13):
  • ПРИМЕНИЛ — только реально записанные статусы (ступени 1-2); «принял» без записи не
    существует (структура Ф3 гарантирует: questions ≠ applied);
  • НЕ ПРИВЯЗАЛ — непривязанные перечисляются ПОИМЁННО (R14, эхо сегмента), не голым числом;
  • НЕ ПОНЯЛ / ПЕРЕСПРОС — закрытый нумерованный вопрос (R15): ответ цифрой / «да/нет» /
    «да, кроме 3» (И1) сразу применяется.
Дедуп по задаче в пачке (R16): одна задача не попадает в две статус-группы, последний
статус побеждает.

Канал несогласия (R26/УПУ1): у обещания ступени-2 «поправьте, если не так — молчание =
согласие» есть РЕАЛЬНЫЙ откат. Контекст ack (что применено / о чём спросили) персистится
по `message_id` ([[reply_ack_state]], переживает рестарт — RAZM6); reply человека НА ack
находит его и делает reopen (несогласие) либо применяет ответ на переспрос.

R24 (канонический триггер, Ф3 дала механику): при ПОДТВЕРЖДЁННОМ ответе на переспрос
ступени-3, где человек НАЗВАЛ класс статуса слова (режим `clarify_class`), дёргаем
`decision_memory.remember_word_class` — впредь слово узнаётся без ИИ.

Опасная тройка (CLAUDE.md проекта): в переспрос кладём только распознанные имена задач +
эхо сегментов человека (его же слова), НЕ сырые тексты сверх нужного; тексты reply НЕ
логируем (только счётчики/режимы); ответы Claude не персистим — только структура решения.

stdlib-only (исполняется в listener-контексте, системный python3.9 без venv).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from . import decision_memory as dm
from . import mixed_reply_split as mrs
from . import pending_status_reply as psr
from . import reply_understand as ru
from . import series_memory

logger = logging.getLogger("notary.reply_ack")

_MAX_OPTIONS = 10        # потолок нумерованного списка кандидатов (не дамп)
_SOURCE = "ack-reply"

# Обещание несогласия ступени-2 (R26): молчание = согласие, но канал отката реален.
TIER2_DISAGREE_NOTE = (
    "Часть отметил с небольшим сомнением — если что-то не так, ответьте «не так» или "
    "«верни», поправлю. Если всё верно, можно не отвечать."
)
# Настройка формата серии (R12/И2) — схлопнута в ОДИН ack (Ф3 слал вторым сообщением).
# Формулировку «формат протокола решает он» согласовал владелец (И2), сохраняем дословно.
CONFIG_FORWARDED_LINE = "🔧 Передал Илье — формат протокола решает он."


def _label_human(label: Optional[str]) -> str:
    return mrs._GROUP_META.get(label or "other", mrs._GROUP_META["other"])[1]


def _short(text: str, *, limit: int = 48) -> str:
    return mrs._short_task_name(text, limit=limit)


# ── R16: дедуп применённых по задаче, последний статус побеждает ───────────────
def _dedup_applied(applied: list) -> list:
    """Одна задача — одна запись; при повторе в пачке ПОБЕЖДАЕТ последний статус
    (R16). Порядок — по последнему появлению задачи. Записи без matched/label
    пропускаем (нечего группировать)."""
    by_key: dict = {}
    for a in applied or []:
        if not isinstance(a, dict):
            continue
        matched = a.get("matched")
        label = a.get("label")
        if not matched or not label:
            continue
        key = series_memory._status_key(matched)
        if not key:
            continue
        if key in by_key:
            del by_key[key]          # переставить в конец = позиция последнего появления
        by_key[key] = a
    return list(by_key.values())


def _groups_from_applied(deduped: list) -> list:
    grouped: dict = {}
    for a in deduped:
        grouped.setdefault(a.get("label"), []).append(a.get("matched") or "")
    return [(lbl, names) for lbl, names in grouped.items()]


def _load_items(series_dir, snapshot) -> list:
    """Универсум кандидатов = ровно то, что участник видит (Ф2 R4/R5, снапшот-scoped)."""
    try:
        items = psr.load_series_pending(series_dir, snapshot=snapshot)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ack] резолв висяков не удался (non-fatal): %s", type(e).__name__)
        return []
    return [t for t in items if isinstance(t, str) and t.strip()][:_MAX_OPTIONS]


def _replier_tasks(items: list, replier: Optional[str]) -> list:
    """Задачи, заведённые НА ответившего (для «закрыть все свои», block_close R11).
    Нет опознаваемых своих (безымянные / чужой) → весь список (не молчим, спросим по всем)."""
    owned = []
    rt = ru._name_tokens(replier)
    for it in items:
        owner = ru._owner_of(it)
        if owner and rt and (ru._name_tokens(owner) & rt):
            owned.append(it)
    return owned or list(items)


def _numbered(options: list) -> list:
    return [f"{i}) {_short(o)}" for i, o in enumerate(options, 1)]


# ── Построение единого ack + первичного переспроса ────────────────────────────
def _build_primary_question(questions: list, *, series_dir, snapshot, replier):
    """Выбрать ОДИН закрытый переспрос (минимум действий, R15) из вопросов ступени-3.
    Приоритет: block_close (И1) → чужая (R11) → неясный класс (R24) → к какой задаче.
    Возвращает (qstate|None, lines, consumed_question|None)."""
    block = next((q for q in questions if q.get("kind") == "block_close"), None)
    foreign = next((q for q in questions if q.get("kind") == "foreign"), None)
    unclear = next((q for q in questions if q.get("kind") == "unclear_label"
                    and q.get("matched")), None)
    pick = next((q for q in questions if q.get("kind") in ("no_match", "ambiguous_ordinal")
                 and q.get("label")), None)

    if block is not None:
        options = _replier_tasks(_load_items(series_dir, snapshot), replier)
        if not options:
            return None, [], None
        lines = ["Закрыть все ваши вопросы?", *_numbered(options),
                 "Ответьте «да», «нет» или «да, кроме 3» (номер)."]
        return ({"mode": "confirm_close", "label": "done", "options": options, "word": None},
                lines, block)

    if foreign is not None and foreign.get("matched"):
        label = foreign.get("label") or "done"
        human = _label_human(label)
        lines = [f"«{_short(foreign['matched'])}» — вопрос не ваш (заведён на другого "
                 f"участника). Всё равно отметить как «{human}»?",
                 "Ответьте «да» или «нет»."]
        return ({"mode": "confirm_foreign", "label": label,
                 "matched": foreign["matched"], "word": None}, lines, foreign)

    if unclear is not None:
        matched = unclear["matched"]
        seg = unclear.get("segment") or ""
        word = ru._status_word(seg, matched)
        if word and dm.word_is_negated(seg, word):
            word = None                      # из отрицания класс не учим (Ф3-цикл5)
        lines = [f"Не понял статус по «{_short(matched)}»: сделано, снято, в работе или "
                 "под вопросом? Ответьте словом."]
        return ({"mode": "clarify_class", "label": None, "matched": matched, "word": word},
                lines, unclear)

    if pick is not None:
        options = _load_items(series_dir, snapshot)
        if not options:
            return None, [], None
        label = pick["label"]
        human = _label_human(label)
        seg = pick.get("segment") or ""
        word = ru._status_word(seg, None)
        if word and dm.word_is_negated(seg, word):
            word = None
        lines = [f"Не понял, к какому вопросу относится «{human}»:", *_numbered(options),
                 "Ответьте цифрой (или «мимо», если ни к какому)."]
        return ({"mode": "pick_target", "label": label, "options": options, "word": word},
                lines, pick)

    return None, [], None


def _echo_unattached(questions: list) -> list:
    """R14: непривязанные части — ПОИМЁННО (эхо сегмента), не голым числом."""
    segs = []
    for q in questions:
        if q.get("kind") == "block_close":
            continue                          # у block_close нет сегмента-эха
        s = q.get("segment") or q.get("matched")
        if isinstance(s, str) and s.strip():
            segs.append(s.strip())
    if not segs:
        return []
    lines = ["Не смог привязать к вопросу:"]
    for i, s in enumerate(segs, 1):
        lines.append(f"{i}) «{_short(s, limit=80)}»")
    lines.append("Уточните, по какому вопросу — можно ответить на это сообщение.")
    return lines


def _edits_line(n_edits: int, *, edit_as_hint: bool, edit_queued: bool) -> Optional[str]:
    if not n_edits:
        return None
    if edit_as_hint:
        return (f"✏️ Заметил правки текста ({n_edits}) — их вносите ответом на сам протокол "
                "встречи, не на дайджест.")
    if edit_queued:
        return f"✏️ Правки текста: принял {n_edits}, перевыпущу протокол."
    return (f"⚠️ Правки текста: {n_edits} не смог применить — ответьте на протокол с правкой "
            "ещё раз.")


def build_ack(result: dict, *, series_dir, snapshot=None, replier: Optional[str] = None,
              edit_as_hint: bool = False) -> dict:
    """Единый сборщик ack (R13-R16). Возвращает:
      • `text` — ОДНО сообщение на весь reply (ничего молча: каждая часть отражена);
      • `applied` — `[{text,label}]` применённых (для отката R26 по «не так/верни»);
      • `has_tier2` — был ли хоть один статус ступени-2 (показать обещание несогласия);
      • `question` — контекст закрытого переспроса ступени-3 (persist по message_id) или None.
    """
    deduped = _dedup_applied(result.get("applied") or [])
    groups = _groups_from_applied(deduped)
    parts: list = list(mrs._render_status_section(groups, None))

    if result.get("config_requests"):
        parts.append(CONFIG_FORWARDED_LINE)

    edits_line = _edits_line(
        len(result.get("edits") or []),
        edit_as_hint=edit_as_hint, edit_queued=bool(result.get("_edit_queued")))
    if edits_line:
        parts.append(edits_line)

    has_tier2 = any(a.get("tier") == ru.TIER_MINOR for a in deduped)
    applied_state = [{"text": a.get("matched"), "label": a.get("label")} for a in deduped]

    questions = list(result.get("questions") or [])
    qstate, qlines, consumed = _build_primary_question(
        questions, series_dir=series_dir, snapshot=snapshot, replier=replier)

    if has_tier2:
        parts.append(TIER2_DISAGREE_NOTE)
    if qlines:
        parts.append("\n".join(qlines))

    remaining = [q for q in questions if q is not consumed]
    parts.extend(_echo_unattached(remaining))

    snippet = (result.get("unclear") or "").strip()
    if snippet and not result.get("config_requests"):
        if len(snippet) > 120:
            snippet = snippet[:119].rstrip() + "…"
        parts.append(f"🤔 Эту часть не понял: «{snippet}» — уточните, пожалуйста.")

    if not parts:
        text = "Не нашёл в ответе ни статусов, ни правок — уточните, пожалуйста."
    else:
        text = "\n".join(parts)
    return {"text": text, "applied": applied_state, "has_tier2": has_tier2, "question": qstate}


# ── Разбор ответа человека на ack/переспрос (R15/R26) ─────────────────────────
_ROLLBACK_RE = re.compile(
    r"\b(не\s*так|неверно|не\s*верно|неправильн|верн[иуё][тл]?|верни|отмен[иеть]|откат)",
    re.IGNORECASE)
_YES_RE = re.compile(r"^\s*(да\b|ага\b|угу\b|верно\b|точно\b|подтвержда|ок\b|окей\b|"
                     r"конечно\b|yes\b|y\b|\+)", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*(нет\b|неа\b|мимо\b|ни\s*к\s*как|no\b|n\b)", re.IGNORECASE)
_KROME_RE = re.compile(r"кроме\s+(.+)$", re.IGNORECASE)


def _positions_in(text: str) -> set:
    """Позиции (1..N) из текста: цифры + порядковые/количественные словом («третьей»,
    «третью», «два»). Для «кроме 3 и 5» / «да, кроме третьей» (И1)."""
    t = (text or "").lower().replace("ё", "е")
    pos = {int(x) for x in re.findall(r"\d+", t)}
    for tok in re.findall(r"[а-я]+", t):
        if tok in psr._ORDINAL_WORD_TO_POS:
            pos.add(psr._ORDINAL_WORD_TO_POS[tok])
        elif tok in psr._CARDINAL_WORD_TO_NUM:
            pos.add(psr._CARDINAL_WORD_TO_NUM[tok])
    return pos


def parse_answer(text: str) -> dict:
    """Разобрать ответ человека на ack/переспрос в интент. Опасная тройка: текст не логируем."""
    t = (text or "").strip()
    rollback = bool(_ROLLBACK_RE.search(t))
    yes = bool(_YES_RE.match(t))
    no = bool(_NO_RE.match(t))
    excluded: set = set()
    m = _KROME_RE.search(t)
    if m:
        excluded = _positions_in(m.group(1))
    # Цифры/номера ВНЕ «кроме …» — прямой выбор (pick_target / выборочное закрытие).
    head = _KROME_RE.sub("", t)
    digits = _positions_in(head)
    cls = psr.detect_status_intent(t)          # назвал класс словом (для clarify_class R24)
    return {"rollback": rollback, "yes": yes, "no": no,
            "digits": digits, "excluded": excluded, "cls": cls}


def _apply_status(series_dir, task_text: str, label: str, *, date) -> bool:
    status = psr._STATUS_MAP.get(label, series_memory.STATUS_OPEN)
    reason = None if status == series_memory.STATUS_OPEN else "по ответу"
    try:
        rec = series_memory.set_task_status(
            series_dir, task_text, status, reason=reason, source=_SOURCE, date=date)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ack] запись статуса не удалась (non-fatal): %s", type(e).__name__)
        return False
    return rec is not None


def _reopen(series_dir, task_text: str, *, date) -> bool:
    try:
        rec = series_memory.set_task_status(
            series_dir, task_text, series_memory.STATUS_OPEN,
            source="ack-rollback", date=date)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ack] откат статуса не удался (non-fatal): %s", type(e).__name__)
        return False
    return rec is not None


def _closed_reply(applied_texts: list, label: str) -> str:
    human = _label_human(label)
    if not applied_texts:
        return "Ок, ничего не отметил."
    names = ", ".join(f"«{_short(t)}»" for t in applied_texts)
    return f"Готово — отметил как «{human}»: {names}."


def handle_ack_reply(state: dict, text: str, *, store_path=None) -> dict:
    """Обработать reply человека НА ack/переспрос бота (R15/R26/R24). `state` — запись
    [[reply_ack_state]] по message_id. Возвращает:
      • `text` — ответ человеку («» = отвечать не на что, не молчим ложно);
      • `consume` — можно ли удалить контекст (действие завершено vs ждём внятный ответ);
      • `learned` — сработал ли триггер памяти R24.
    Опасная тройка: тексты reply не логируем."""
    series_dir = Path(state.get("series_dir") or ".")
    date = state.get("date")
    applied = state.get("applied") or []
    question = state.get("question")
    ans = parse_answer(text)

    # 1) Несогласие (R26): «не так / верни» на ступени-2 → РЕАЛЬНЫЙ reopen применённого.
    if ans["rollback"] and applied:
        reopened = [a["text"] for a in applied
                    if a.get("text") and _reopen(series_dir, a["text"], date=date)]
        if reopened:
            names = ", ".join(f"«{_short(t)}»" for t in reopened)
            return {"text": f"↩️ Вернул в работу: {names}. Спасибо, что поправили.",
                    "consume": True, "learned": False}
        return {"text": "Не нашёл, что откатить — уточните, пожалуйста.",
                "consume": False, "learned": False}

    # 2) Ответ на закрытый переспрос (R15).
    if question:
        mode = question.get("mode")
        label = question.get("label")
        options = question.get("options") or []

        if mode == "confirm_close":
            if ans["yes"]:
                targets = [o for i, o in enumerate(options, 1) if i not in ans["excluded"]]
            elif ans["digits"]:
                targets = [options[i - 1] for i in sorted(ans["digits"])
                           if 1 <= i <= len(options)]
            elif ans["no"]:
                return {"text": "Ок, ничего не закрываю.", "consume": True, "learned": False}
            else:
                return {"text": "Ответьте «да», «нет» или «да, кроме 3» (номер), пожалуйста.",
                        "consume": False, "learned": False}
            done = [t for t in targets if _apply_status(series_dir, t, label, date=date)]
            return {"text": _closed_reply(done, label), "consume": True, "learned": False}

        if mode == "confirm_foreign":
            matched = question.get("matched")
            if ans["yes"] and matched:
                ok = _apply_status(series_dir, matched, label, date=date)
                return {"text": _closed_reply([matched] if ok else [], label),
                        "consume": True, "learned": False}
            if ans["no"]:
                return {"text": "Ок, чужой вопрос не трогаю.", "consume": True, "learned": False}
            return {"text": "Ответьте «да» или «нет», пожалуйста.",
                    "consume": False, "learned": False}

        if mode == "pick_target":
            if ans["no"]:
                return {"text": "Понял, ни к какому — оставил как есть.",
                        "consume": True, "learned": False}
            chosen = [options[i - 1] for i in sorted(ans["digits"]) if 1 <= i <= len(options)]
            if not chosen:
                return {"text": "Ответьте цифрой из списка (или «мимо»), пожалуйста.",
                        "consume": False, "learned": False}
            done = [t for t in chosen if _apply_status(series_dir, t, label, date=date)]
            return {"text": _closed_reply(done, label), "consume": True, "learned": False}

        if mode == "clarify_class":
            matched = question.get("matched")
            cls = ans["cls"]
            if cls is None:
                return {"text": "Как отметить — «сделано», «снято», «в работе» или «под "
                        "вопросом»? Ответьте словом.", "consume": False, "learned": False}
            ok = _apply_status(series_dir, matched, cls, date=date) if matched else False
            # R24 канонический триггер: человек НАЗВАЛ верный класс слова → запоминаем,
            # чтобы впредь узнать без ИИ (Ф3 дала механику, Ф4 — триггер).
            learned = False
            word = question.get("word")
            if ok and word:
                learned = dm.remember_word_class(word, cls, source="ack-clarify",
                                                 store_path=store_path)
            return {"text": _closed_reply([matched] if ok else [], cls),
                    "consume": True, "learned": bool(learned)}

    # 3) Reply на ack без вопроса и без «верни» (согласие «ок/спасибо» = молчание) — не
    #    досаждаем повторным ответом; неясное — мягко подсказываем канал несогласия.
    if applied:
        if ans["yes"] or not text.strip():
            return {"text": "", "consume": True, "learned": False}
        return {"text": "Если что-то не так — ответьте «не так» или «верни», поправлю.",
                "consume": False, "learned": False}
    return {"text": "", "consume": True, "learned": False}
