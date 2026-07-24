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

from . import ack_understand as aiu
from . import decision_memory as dm
from . import mixed_reply_split as mrs
from . import pending_status_reply as psr
from . import reply_understand as ru
from . import series_memory

logger = logging.getLogger("notary.reply_ack")

_MAX_OPTIONS = 10        # потолок нумерованного списка кандидатов (не дамп)
# ISS-48: массовое закрытие («закрой всё у всех») законно затрагивает больше 10 задач —
# потолок показа выше, но НЕ бесконечный. Остаток НЕ теряется: он кладётся в `rest` вопроса,
# и после подтверждения бот сам предлагает следующую порцию (цикл5 У1).
_MAX_MASS_OPTIONS = 25
# Бюджет символов на СПИСОК в одном сообщении. Telegram режет на 4096, а `send_message`
# длину не режет (цикл5 У5): пробитое сообщение НЕ уходит вовсе — на просьбу «закрой всё»
# бот бы просто промолчал. 2200 оставляет запас на остальные блоки ack и хвост-подсказку.
_MASS_LIST_BUDGET = 2200
# Подсказка формата ответа: с ISS-49 канал понимает и обычную речь — говорим об этом (У6).
_ANSWER_HINT = ("Ответьте «да», «нет», «да, кроме 3» (номер) — или просто словами: "
                "«оставь только про агента», «закрой ещё у Татьяны».")
_SOURCE = "ack-reply"

# Обещание несогласия ступени-2 (R26): молчание = согласие, но канал отката реален.
TIER2_DISAGREE_NOTE = (
    "Часть отметил с небольшим сомнением — если что-то не так, ответьте «не так» или "
    "«верни», поправлю. Если всё верно, можно не отвечать."
)
# Настройка формата серии (R12/И2) — схлопнута в ОДИН ack (Ф3 слал вторым сообщением).
# Формулировку «формат протокола решает он» согласовал владелец (И2), сохраняем дословно.
CONFIG_FORWARDED_LINE = "🔧 Передал Илье — формат протокола решает он. Как решит, вернусь."


def _label_human(label: Optional[str]) -> str:
    return mrs._GROUP_META.get(label or "other", mrs._GROUP_META["other"])[1]


def _short(text: str, *, limit: int = 10_000) -> str:
    """Имя РАСПОЗНАННОЙ задачи для переспроса/сводки: снимаем префикс исполнителя
    «Имя: », длину по умолчанию НЕ режем (ISS-44.4 + фидбэк владельца 23.07: обрезка
    «…через знакомого по…» мешает понять, что подтверждаешь). Явный `limit` (эхо сырого
    сегмента человека в `_echo_unattached`) — режет как прежде."""
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


def _terminal_keys(series_dir) -> set:
    """Ключи задач, уже стоящих в ТЕРМИНАЛЬНОМ статусе (сделано/снято/авто-закрыто).
    ISS-48 (побочное): список «Закрыть все ваши вопросы?» показывал задачу, закрытую двумя
    минутами ранее — `load_series_pending` намеренно тащит и закрытые (нужны для reopen),
    но в вопросе «что закрыть» им не место. Сбой чтения → пустое множество (не режем)."""
    try:
        store = series_memory.load_task_status(series_dir) or {}
    except Exception as e:  # noqa: BLE001 — чтение статусов best-effort
        logger.warning("[ack] чтение статусов не удалось (non-fatal): %s", type(e).__name__)
        return set()
    out = set()
    for k, rec in store.items():
        st = rec.get("status") if isinstance(rec, dict) else None
        if st in series_memory._TERMINAL_CLOSED:
            out.add(k)
    return out


def _open_only(items: list, series_dir) -> list:
    """Оставить только НЕ закрытые задачи (ISS-48: не предлагаем закрыть уже закрытое)."""
    closed = _terminal_keys(series_dir)
    if not closed:
        return list(items)
    return [t for t in items if series_memory._status_key(t) not in closed]


def _all_open_items(series_dir, snapshot) -> list:
    """Все открытые задачи серии (ЛЮБОГО владельца) — универсум массового закрытия
    (ISS-48: «закрой всё у всех / у Татьяны, Александры, Марии»).

    `snapshot` передан (построение вопроса по отвечаемому документу) → корзина ограничена
    тем, что участник ВИДЕЛ (инв. Ф2 R4). При разборе ОТВЕТА на вопрос бота снапшота нет
    (state его не хранит) → берём живой хвост серии; инвариант «не закрываем невиданное»
    держится другим механизмом: получившийся список бот ПОКАЗЫВАЕТ и ждёт подтверждения."""
    try:
        items = psr.load_series_pending(series_dir, snapshot=snapshot)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ack] резолв висяков не удался (non-fatal): %s", type(e).__name__)
        return []
    items = [t for t in items if isinstance(t, str) and t.strip()]
    return _open_only(items, series_dir)


def _owned_by_names(items: list, names: list) -> list:
    """Задачи, заведённые на кого-то из `names` (ISS-48, охват «у Татьяны, у Марии»).
    Владелец закодирован префиксом «Имя: текст» (карта series_memory)."""
    want: set = set()
    for n in names or []:
        want |= ru._name_tokens(n)
    if not want:
        return []
    out = []
    for it in items:
        owner = ru._owner_of(it)
        if owner and (ru._name_tokens(owner) & want):
            out.append(it)
    return out


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


def _split_by_budget(items: list, *, with_owner: bool) -> tuple:
    """Порция списка, влезающая в одно сообщение, + ОСТАТОК (цикл5 У1/У5).
    Режем и по количеству (`_MAX_MASS_OPTIONS`), и по символам (`_MASS_LIST_BUDGET`):
    длинные названия пробивают лимит Telegram раньше, чем счётчик. Остаток не выбрасываем —
    его несёт `rest` вопроса, и после подтверждения бот предложит следующую порцию."""
    head, used = [], 0
    for t in items:
        shown = t if with_owner else _short(t)
        cost = len(str(shown)) + 5           # «12) » + перевод строки
        if head and (len(head) >= _MAX_MASS_OPTIONS or used + cost > _MASS_LIST_BUDGET):
            break
        head.append(t)
        used += cost
    return head, list(items[len(head):])


def _numbered(options: list, *, with_owner: bool = False) -> list:
    """Нумерованный список кандидатов. `with_owner=True` — НЕ снимаем префикс «Имя: »
    (ISS-48: в массовом закрытии по нескольким людям без имени владельца не понять, что
    подтверждаешь; в списке «ваши вопросы» имя, наоборот, шум — оно и так ваше)."""
    return [f"{i}) {o if with_owner else _short(o)}" for i, o in enumerate(options, 1)]


# ── Построение единого ack + первичного переспроса ────────────────────────────
def _build_primary_question(questions: list, *, series_dir, snapshot, replier):
    """Выбрать ОДИН закрытый переспрос (минимум действий, R15) из вопросов ступени-3.
    Приоритет: block_close (И1) → чужие (R11) → неясный класс (R24) → к какой задаче.
    Возвращает (qstate|None, lines, consumed_questions:list). `consumed_questions` —
    вопросы, СЪЕДЕННЫЕ этим переспросом (их не эхошит `_echo_unattached`); чужих может
    быть НЕСКОЛЬКО в одном reply — их подтверждаем одним блоком «да/нет» (ISS-44.8)."""
    block = next((q for q in questions if q.get("kind") == "block_close"), None)
    foreigns = [q for q in questions if q.get("kind") == "foreign" and q.get("matched")]
    unclear = next((q for q in questions if q.get("kind") == "unclear_label"
                    and q.get("matched")), None)
    pick = next((q for q in questions if q.get("kind") in ("no_match", "ambiguous_ordinal")
                 and q.get("label")), None)

    if block is not None:
        # ISS-48: «закрой всё» бывает НЕ только про свои задачи — «у всех», «у Татьяны, у
        # Александры». Охват даёт understand-слой (`scope`/`owners`); показываем СРАЗУ тот
        # список, который человек просил, а не молча сужаем до своих.
        scope = (block.get("scope") if isinstance(block, dict) else None) or "mine"
        owners = list((block.get("owners") if isinstance(block, dict) else None) or [])
        pool = _all_open_items(series_dir, snapshot)
        mine = _replier_tasks(pool, replier)
        note = None
        if scope == "everyone":
            options, head = pool, "Закрыть ВСЕ открытые вопросы — у всех участников?"
        elif scope == "named" and owners:
            named = _owned_by_names(pool, owners)
            if named:
                options = named
                head = f"Закрыть все открытые вопросы ({', '.join(owners)})?"
            else:
                options, head = mine, "Закрыть все ваши вопросы?"
                note = (f"Вопросов, заведённых на {', '.join(owners)}, в этой серии не "
                        "нашёл — показываю только ваши.")
        else:
            options, head = mine, "Закрыть все ваши вопросы?"
        if not options:
            # У2 (цикл5): «закрой всё» при нуле висящих — не молчим и не врём «не нашёл
            # статусов»: честно говорим, что закрывать нечего (block_close «съеден»).
            return None, ["Сейчас у вас нет открытых вопросов — закрывать нечего."], [block]
        with_owner = scope != "mine"
        total = len(options)
        options, rest = _split_by_budget(options, with_owner=with_owner)
        lines = [head]
        if note:
            lines.append(note)
        lines.extend(_numbered(options, with_owner=with_owner))
        if rest:
            lines.append(f"(открытых {total} — показываю первые {len(options)}, "
                         f"после ответа предложу оставшиеся {len(rest)})")
        lines.append(_ANSWER_HINT)
        return ({"mode": "confirm_close", "label": "done", "options": options, "word": None,
                 "scope": scope, "owners": owners, "rest": rest},
                lines, [block])

    if foreigns:
        # ISS-44.8: несколько чужих задач в одном reply → ОДИН блок подтверждения «да/нет»
        # (а не «1 спросил + N в „не понял“»). Все привязаны (к чужим задачам) — просто не
        # свои. Дедуп по задаче в пачке: одна задача — один пункт (последний label побеждает).
        options, labels, seen = [], [], {}
        for q in foreigns:
            m = q["matched"]
            key = series_memory._status_key(m)
            lbl = q.get("label") or "done"
            if key and key in seen:
                labels[seen[key]] = lbl      # повтор задачи — обновить статус, не дублить пункт
                continue
            if key:
                seen[key] = len(options)
            options.append(m)
            labels.append(lbl)
        if len(options) == 1:
            human = _label_human(labels[0])
            lines = [f"«{_short(options[0])}» — вопрос не ваш (заведён на другого "
                     f"участника). Всё равно отметить как «{human}»?",
                     "Ответьте «да» или «нет»."]
        else:
            if len(set(labels)) == 1:
                human = _label_human(labels[0])
                head = (f"Эти вопросы не ваши (заведены на другого участника) — всё равно "
                        f"отметить все как «{human}»?")
            else:
                head = ("Эти вопросы не ваши (заведены на другого участника) — всё равно "
                        "отметить?")
            lines = [head, *_numbered(options),
                     "Ответьте «да», «нет» или «да, кроме 2» (номер)."]
        return ({"mode": "confirm_foreign", "label": labels[0], "options": options,
                 "labels": labels, "word": None}, lines, list(foreigns))

    if unclear is not None:
        matched = unclear["matched"]
        seg = unclear.get("segment") or ""
        word = ru._status_word(seg, matched)
        if word and dm.word_is_negated(seg, word):
            word = None                      # из отрицания класс не учим (Ф3-цикл5)
        lines = [f"Не понял статус по «{_short(matched)}»: сделано, снято, в работе или "
                 "под вопросом? Ответьте словом."]
        return ({"mode": "clarify_class", "label": None, "matched": matched, "word": word},
                lines, [unclear])

    if pick is not None:
        options = _load_items(series_dir, snapshot)
        if not options:
            return None, [], []
        label = pick["label"]
        human = _label_human(label)
        seg = pick.get("segment") or ""
        word = ru._status_word(seg, None)
        if word and dm.word_is_negated(seg, word):
            word = None
        lines = [f"Не понял, к какому вопросу относится «{human}»:", *_numbered(options),
                 "Ответьте цифрой (или «мимо», если ни к какому)."]
        return ({"mode": "pick_target", "label": label, "options": options, "word": word},
                lines, [pick])

    return None, [], []


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
    head = ("⚠️ А это не понял — не смог привязать к вопросу протокола:" if len(segs) == 1
            else "⚠️ А эти не понял — не смог привязать к вопросам протокола:")
    lines = [head]
    for i, s in enumerate(segs, 1):
        lines.append(f"{i}) «{_short(s, limit=80)}»")
    lines.append("Подскажите, как они называются в протоколе, — и я сразу закрою "
                 "(можно ответить на это сообщение).")
    return lines


def _already_line(already: list, *, applied_keys: Optional[set] = None) -> Optional[str]:
    """ISS-46: задачи, уже стоящие в названном статусе, — «уже закрыто», не «отметил
    закрытым» (владелец 23.07: бот перезакрывал старые закрытые и вводил в заблуждение).
    `applied_keys` — задачи, реально применённые в ЭТОМ reply: их из «уже» исключаем
    (та же задача дважды в пачке не должна показаться и «✅ Закрыто», и «ℹ️ уже», Н1 цикл5)."""
    by_key: dict = {}
    for a in already or []:
        m = a.get("matched")
        if not m:
            continue
        k = series_memory._status_key(m)
        if k and not (applied_keys and k in applied_keys):
            by_key[k] = a
    if not by_key:
        return None
    names = ", ".join(f"«{_short(a.get('matched'))}»" for a in by_key.values())
    labels = {a.get("label") for a in by_key.values()}
    if len(labels) == 1:
        human = _label_human(next(iter(labels)))
        return f"ℹ️ Это уже было отмечено как «{human}» раньше — повторно не трогаю: {names}."
    return f"ℹ️ Это уже было закрыто раньше — повторно не трогаю: {names}."


def _edits_line(n_edits: int, *, edit_as_hint: bool, edit_queued: bool) -> Optional[str]:
    if not n_edits:
        return None
    if edit_as_hint:
        return (f"✏️ Заметил правки текста ({n_edits}) — их вносите ответом на сам протокол "
                "встречи, не на дайджест.")
    if edit_queued:
        return f"✏️ И ещё правки к тексту ({n_edits}) — все внёс, сейчас пришлю обновлённый протокол."
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

    applied_keys = {series_memory._status_key(a.get("matched"))
                    for a in deduped if a.get("matched")}
    already_line = _already_line(result.get("already") or [], applied_keys=applied_keys)
    if already_line:
        parts.append(already_line)

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

    consumed_ids = {id(q) for q in (consumed or [])}
    remaining = [q for q in questions if id(q) not in consumed_ids]
    echo = _echo_unattached(remaining)
    if echo:
        parts.append("\n".join(echo))  # блок эха — целиком, пустая строка только ВОКРУГ него

    snippet = (result.get("unclear") or "").strip()
    if snippet and not result.get("config_requests"):
        if len(snippet) > 120:
            snippet = snippet[:119].rstrip() + "…"
        parts.append(f"🤔 Эту часть не понял: «{snippet}». Напишите: название задачи "
                     "(как в протоколе) + что с ней, например «юани — закупили».")

    if not parts:
        text = ("🤔 Не разобрал, про какую задачу речь. Напишите: название задачи "
                "(как в протоколе) + что с ней, например «юани — закупили».")
    else:
        text = "\n\n".join(parts)  # ISS-44.4: пустая строка между блоками (читаемость)
    return {"text": text, "applied": applied_state, "has_tier2": has_tier2, "question": qstate}


# ── Разбор ответа человека на ack/переспрос (R15/R26) ─────────────────────────
_ROLLBACK_RE = re.compile(
    r"\b(не\s*так|неверно|не\s*верно|неправильн|верн[иуё][тл]?|верни|отмен[иеть]|откат)",
    re.IGNORECASE)
_YES_RE = re.compile(r"^\s*(да\b|ага\b|угу\b|верно\b|точно\b|подтвержда|ок\b|окей\b|"
                     r"конечно\b|yes\b|y\b|\+)", re.IGNORECASE)
# «да нет», «да не надо», «да ну нафиг» — РАЗГОВОРНОЕ ОТРИЦАНИЕ, а не согласие: частица
# «да» здесь усилительная. Проверяется ПЕРЕД `_YES_RE`, иначе «да нет, не надо» закрывало
# бы весь список (цикл5 ISS-49: канал ответа обязан ошибаться в сторону «не тронул»).
_DA_NET_RE = re.compile(r"^\s*да[\s,]+(?:не\b|нет\b|ну\b|ладно\b|погод|подожд)", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*(нет\b|неа\b|мимо\b|ни\s*к\s*как|no\b|n\b)", re.IGNORECASE)
_KROME_RE = re.compile(r"кроме\s+(.+)$", re.IGNORECASE)


def _positions_in(text: str) -> set:
    """Позиции (1..N) из текста: цифры + ПОРЯДКОВЫЕ словом («третьей», «третью»). Для
    «кроме 3 и 5» / «да, кроме третьей» (И1).

    ISS-48: КОЛИЧЕСТВЕННЫЕ словом («один», «два») сюда НЕ входят. Они выглядят как номер,
    а значат количество: «оставь только ОДИН вопрос» → регулярка делала `digits={1}` и
    закрывала пункт 1 — ровно противоположное сказанному (инцидент 24.07, закрыта 1 задача
    из 21). Порядковые однозначны («третью» = пункт 3) и остаются. Количественные разбирает
    ИИ-слой [[ack_understand]] по смыслу фразы."""
    t = (text or "").lower().replace("ё", "е")
    pos = {int(x) for x in re.findall(r"\d+", t)}
    for tok in re.findall(r"[а-я]+", t):
        if tok in psr._ORDINAL_WORD_TO_POS:
            pos.add(psr._ORDINAL_WORD_TO_POS[tok])
    return pos


def parse_answer(text: str) -> dict:
    """Разобрать ответ человека на ack/переспрос в интент. Опасная тройка: текст не логируем."""
    t = (text or "").strip()
    rollback = bool(_ROLLBACK_RE.search(t))
    da_net = bool(_DA_NET_RE.match(t))
    yes = bool(_YES_RE.match(t)) and not da_net
    no = bool(_NO_RE.match(t)) or da_net
    excluded: set = set()
    unresolved = False
    m = _KROME_RE.search(t)
    if m:
        excluded = _positions_in(m.group(1))
        # «кроме караоке» / «кроме двух» — исключение по ОПИСАНИЮ или количеству. Номера из
        # него не достать; принять такой ответ за голое «да» = закрыть в т.ч. то, что просили
        # оставить (инцидент ISS-48). Помечаем — caller честно переспросит, а не угадает.
        unresolved = not excluded
    # Цифры/номера ВНЕ «кроме …» — прямой выбор (pick_target / выборочное закрытие).
    head = _KROME_RE.sub("", t)
    digits = _positions_in(head)
    cls = psr.detect_status_intent(t)          # назвал класс словом (для clarify_class R24)
    return {"rollback": rollback, "yes": yes, "no": no, "digits": digits,
            "excluded": excluded, "excluded_unresolved": unresolved, "cls": cls}


# ── ISS-49: ИИ-первый разбор ответа на ВОПРОС БОТА (регулярки — фолбэк) ───────
_MODE_QUESTION_HINT = {
    "confirm_close": "Бот спросил: закрыть все перечисленные вопросы? (да / нет / да, кроме N)",
    "confirm_foreign": ("Бот спросил: перечисленные вопросы заведены на других участников — "
                        "всё равно отметить? (да / нет)"),
    "pick_target": ("Бот спросил: к какому из перечисленных вопросов относится ваш ответ? "
                    "(номер)"),
    "clarify_class": ("Бот спросил: каким статусом отметить вопрос — сделано / снято / "
                      "в работе / под вопросом?"),
}


def _smart_answer(state: dict, question: Optional[dict], text: str, *, answer_fn=None):
    """ИИ-разбор ответа человека на вопрос бота (ISS-49). Возвращает структуру намерения
    (+ `_candidates` — тот самый пронумерованный список, по которому ИИ давал номера) или
    None → caller идёт регулярками.

    ИИ НЕ зовём, когда ответ и так однозначен («да» / «нет» / «2» / «да, кроме 3») — это
    быстрый путь, как в understand-слое. Для `confirm_close` универсум расширяем до ВСЕХ
    открытых задач серии: человек законно может ответить шире вопроса («закрой и у
    Татьяны»). Расширение НЕ означает тихого закрытия чужого — код ниже показывает
    получившийся список и просит одно подтверждение."""
    if not question or aiu.is_trivial_answer(text):
        return None
    if answer_fn is None and not (ru.is_enabled() and aiu.is_enabled()):
        return None                       # R10: без флага реальный claude не зовём
    options = list(question.get("options") or [])
    if not options and question.get("matched"):
        options = [question["matched"]]
    candidates = list(options)
    if question.get("mode") == "confirm_close":
        seen = {series_memory._status_key(o) for o in options}
        for t in _all_open_items(Path(state.get("series_dir") or "."), None):
            k = series_memory._status_key(t)
            if k and k not in seen:
                seen.add(k)
                candidates.append(t)
    candidates = candidates[: aiu._MAX_ITEMS_IN_PROMPT]
    res = aiu.understand_answer(
        text, candidates, shown_count=len(options),
        question_text=_MODE_QUESTION_HINT.get(question.get("mode")),
        replier=state.get("replier"), answer_fn=answer_fn)
    if not isinstance(res, dict):
        return None
    return {**res, "_candidates": candidates}


def _pick(candidates: list, positions) -> list:
    """Номера от ИИ → тексты задач, ТОЛЬКО в пределах показанного списка (номер вне
    диапазона игнорируем — не угадываем, R5)."""
    return [candidates[i - 1] for i in sorted(positions or []) if 1 <= i <= len(candidates)]


def _scope_targets(smart: dict, *, candidates: list, options: list, replier) -> list:
    """Задачи, которых КАСАЕТСЯ массовое «закрой всё» с учётом охвата (ISS-48):
      • shown    — только то, что бот показал в вопросе;
      • everyone — все открытые задачи серии;
      • named    — задачи названных людей (+ показанные свои, если человек назвал и себя)."""
    scope = smart.get("scope") or "shown"
    if scope == "everyone":
        return list(candidates)
    if scope == "named" and smart.get("owners"):
        named = _owned_by_names(candidates, smart["owners"])
        self_named = bool(ru._name_tokens(replier) & {
            t for n in smart["owners"] for t in ru._name_tokens(n)})
        if self_named:
            keys = {series_memory._status_key(t) for t in named}
            named = named + [o for o in options
                             if series_memory._status_key(o) not in keys]
        return named
    return list(options)


def _scope_human(smart: dict) -> str:
    """Охват человеческим языком (склонять имена не пытаемся — перечисляем через двоеточие)."""
    scope = smart.get("scope") or "shown"
    if scope == "everyone":
        return "по всем участникам"
    if scope == "named" and smart.get("owners"):
        return "по участникам: " + ", ".join(smart["owners"])
    return "по вашим вопросам"


_SMART_HINT = ("Не до конца понял ответ. Скажите проще: «да» — закрыть всё из списка, "
               "«нет» — не трогать, либо назовите номера или названия вопросов.")


def _dedup_texts(items: list, *, skip_keys: Optional[set] = None) -> list:
    out, seen = [], set(skip_keys or ())
    for t in items:
        k = series_memory._status_key(t)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _smart_confirm_close(smart: dict, *, state: dict, question: dict, series_dir, date) -> dict:
    """ISS-48/ISS-49: применить ИИ-разбор ответа на «Закрыть все ваши вопросы?».

    ИНВАРИАНТ ЧЕСТНОСТИ (требование владельца): бот не выполняет молча малую долю просьбы.
    Просьба шире показанного («у всех», «у Татьяны») → собираем ПОЛНЫЙ список намерения,
    показываем его и просим ОДНО подтверждение; ничего чужого не закрываем догадкой."""
    candidates = smart.get("_candidates") or []
    options = list(question.get("options") or [])
    label = smart.get("cls") or question.get("label") or "done"
    intent = smart.get("intent")

    if intent == "decline":
        return {"text": "Ок, ничего не закрываю.", "consume": True, "learned": False}
    if intent not in ("apply", "class"):
        return {"text": _SMART_HINT, "consume": False, "learned": False}

    kept = _pick(candidates, smart.get("keep"))
    keep_keys = {series_memory._status_key(t) for t in kept}
    explicit = _pick(candidates, smart.get("positions"))
    scope = smart.get("scope") or "shown"
    if smart.get("all") or (not explicit and scope != "shown"):
        base = _scope_targets(smart, candidates=candidates, options=options,
                              replier=state.get("replier"))
    else:
        base = explicit
    targets = _dedup_texts(base, skip_keys=keep_keys)
    if not targets:
        if kept:
            return {"text": "Понял: ничего не закрываю, оставляю как есть — "
                            + _names_line(kept) + ".", "consume": True, "learned": False}
        if scope != "shown":
            # Просьбу поняли (охват назван), но задач под него не нашли — частая причина:
            # вопрос записан БЕЗ имени владельца. Не отвечаем «не понял» на понятное
            # (цикл5 У2): называем причину и показываем, что открыто, одним вопросом.
            pool, rest = _split_by_budget(_all_open_items(series_dir, None), with_owner=True)
            if pool:
                lines = [f"Понял охват ({_scope_human(smart)}), но вопросов, записанных на "
                         "этих людей, не нашёл — возможно, они заведены без имени. "
                         "Вот что открыто сейчас:", *_numbered(pool, with_owner=True)]
                if rest:
                    lines.append(f"(показываю {len(pool)}, дальше предложу оставшиеся "
                                 f"{len(rest)})")
                lines.append("Скажите «да» — закрою всё из списка, или назовите номера.")
                return {"text": "\n".join(lines), "consume": True, "learned": False,
                        "question": {"mode": "confirm_close", "label": label,
                                     "options": pool, "word": None, "scope": scope,
                                     "owners": smart.get("owners") or [], "rest": rest}}
            return {"text": "Открытых вопросов сейчас нет — закрывать нечего.",
                    "consume": True, "learned": False}
        return {"text": _SMART_HINT, "consume": False, "learned": False}

    total = len(targets)
    shown_keys = {series_memory._status_key(o) for o in options}
    widened = any(series_memory._status_key(t) not in shown_keys for t in targets)

    if widened:
        # Шире, чем спрашивал бот → НЕ применяем молча: показываем ровно то, что закроем,
        # и берём одно подтверждение (ISS-48 «в»: показать намерение списком).
        targets, rest = _split_by_budget(targets, with_owner=True)
        head = (f"Понял — закрываем шире, чем я спрашивал: {_scope_human(smart)}. "
                f"Отмечу как «{_label_human(label)}» вот эти:")
        # Список чужих задач показываем С ИМЕНЕМ владельца — иначе непонятно, что
        # подтверждаешь («Свести демо-трек» без «Александра Ким:» ни о чём не говорит).
        lines = [head, *_numbered(targets, with_owner=True)]
        if rest:
            lines.append(f"(подходит {total} — в одно сообщение влезло {len(targets)}, "
                         f"оставшиеся {len(rest)} предложу сразу после подтверждения)")
        if kept:
            lines.append(f"Оставляю открытым: {_names_line(kept, with_owner=True)}.")
        lines.append("Подтвердите «да» — закрою весь список. «Нет» — ничего не трогаю.")
        qstate = {"mode": "confirm_close", "label": label, "options": targets,
                  "word": None, "scope": scope, "owners": smart.get("owners") or [],
                  "rest": rest}
        # Аудит массового закрытия ЧУЖИХ задач (ход5): одним «да» затрагиваются задачи
        # других участников — след в логе обязателен. Только счётчики и охват, БЕЗ текстов
        # задач и без текста ответа (опасная тройка).
        foreign_n = sum(1 for t in targets if series_memory._status_key(t) not in shown_keys)
        logger.info("[ack] массовое закрытие: охват=%s предложено=%d чужих=%d остаток=%d",
                    scope, len(targets), foreign_n, len(rest))
        return {"text": "\n".join(lines), "consume": True, "learned": False,
                "question": qstate}

    targets, rest = _split_by_budget(targets, with_owner=False)
    done, already = _apply_many(series_dir, targets, label, date=date)
    parts = [_closed_reply(done, label)]
    already_line = _already_suffix(already)
    if already_line:
        parts.append(already_line)
    if kept:
        parts.append(f"Оставил открытым: {_names_line(kept)}.")
    return _with_rest_or_summary({"text": " ".join(parts), "consume": True, "learned": False},
                                 rest=rest, label=label, question=question,
                                 series_dir=series_dir)


def _apply_many(series_dir, targets: list, label, *, date) -> tuple:
    """Применить статус к пачке → (записанные, уже стоявшие в этом статусе).
    ISS-46 в массовом канале (цикл5 НОВ2): порции живут минутами, задача из остатка могла
    закрыться другим ответом. Перезаписывать и рапортовать «отметил закрытым» повторно
    нельзя — владелец на это прямо жаловался. Смена статуса (reopen) сюда не попадает:
    фильтр только про ТОТ ЖЕ терминальный статус."""
    target_status = psr._STATUS_MAP.get(label)
    try:
        cur = series_memory.load_task_status(series_dir) or {}
    except Exception:  # noqa: BLE001 — чтение best-effort, применение не блокируем
        cur = {}
    done, already = [], []
    for t in targets:
        k = series_memory._status_key(t)
        rec = cur.get(k) if k else None
        if (target_status in series_memory._TERMINAL_CLOSED and isinstance(rec, dict)
                and rec.get("status") == target_status):
            already.append(t)
            continue
        if _apply_status(series_dir, t, label, date=date):
            done.append(t)
    return done, already


def _already_suffix(already: list) -> Optional[str]:
    if not already:
        return None
    return f"ℹ️ Уже было отмечено раньше — повторно не трогал: {_names_line(already)}."


def _open_left_line(series_dir) -> Optional[str]:
    """Сколько вопросов осталось открытыми после массового закрытия (цикл5 У7). Владелец
    жаловался ровно на неопределённость «закрыл — а висит ли ещё что-то»."""
    try:
        left = _all_open_items(series_dir, None)
    except Exception:  # noqa: BLE001 — сводка best-effort, ответ не валим
        return None
    if not left:
        return "Открытых вопросов по серии больше нет — чистый лист."
    return f"Открытыми по серии остаются {len(left)} (в т.ч. у других участников)."


def _with_rest_or_summary(res: dict, *, rest: list, label, question: dict, series_dir) -> dict:
    """После применения порции: остаток есть → сразу предлагаем следующую (обещание «закрою
    остальные» становится РАБОЧИМ, цикл5 У1); остатка нет → говорим, сколько осталось
    открытыми (У7). Новый вопрос caller персистит под своим message_id."""
    if rest:
        chunk, tail = _split_by_budget(rest, with_owner=True)
        lines = [res["text"],
                 f"Осталось ещё {len(rest)} — закрыть и их?",
                 *_numbered(chunk, with_owner=True)]
        if tail:
            lines.append(f"(показываю {len(chunk)}, дальше предложу оставшиеся {len(tail)})")
        lines.append("Ответьте «да», «нет» или «да, кроме 2» (номер).")
        return {**res, "text": "\n".join(lines),
                "question": {"mode": "confirm_close", "label": label, "options": chunk,
                             "word": None, "scope": question.get("scope") or "mine",
                             "owners": question.get("owners") or [], "rest": tail}}
    line = _open_left_line(series_dir)
    return {**res, "text": f"{res['text']} {line}".strip()} if line else res


def _smart_to_ans(smart: Optional[dict], n_options: int) -> Optional[dict]:
    """ISS-49: структура намерения от ИИ → тот же контракт, что у `parse_answer`, для
    режимов БЕЗ расширения охвата (чужие задачи / выбор пункта). None → ИИ не дал внятного
    намерения → caller остаётся на регулярках (и, если и те молчат, честно переспросит).
    Номера вне диапазона показанного отбрасываем — не угадываем (R5)."""
    if not smart:
        return None
    base = {"rollback": False, "yes": False, "no": False,
            "digits": set(), "excluded": set(), "cls": smart.get("cls")}
    intent = smart.get("intent")
    if intent == "decline":
        return {**base, "no": True}
    if intent not in ("apply", "class"):
        return None
    keep = {i for i in (smart.get("keep") or []) if 1 <= i <= n_options}
    pos = {i for i in (smart.get("positions") or []) if 1 <= i <= n_options}
    if smart.get("all") or (not pos and keep):
        return {**base, "yes": True, "excluded": keep}
    if pos:
        return {**base, "digits": pos - keep}
    return None


def _names_line(items: list, *, with_owner: bool = False) -> str:
    return ", ".join(f"«{t if with_owner else _short(t)}»" for t in items)


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


def handle_ack_reply(state: dict, text: str, *, store_path=None, answer_fn=None) -> dict:
    """Обработать reply человека НА ack/переспрос бота (R15/R26/R24). `state` — запись
    [[reply_ack_state]] по message_id. Возвращает:
      • `text` — ответ человеку («» = отвечать не на что, не молчим ложно);
      • `consume` — можно ли удалить контекст (действие завершено vs ждём внятный ответ);
      • `learned` — сработал ли триггер памяти R24;
      • `question` — НОВЫЙ контекст переспроса (ISS-48: просьба оказалась шире вопроса →
        показали список и ждём одно подтверждение). Есть → caller персистит его под
        message_id своего ответа, иначе канал ответа оборвётся.
    `answer_fn` инъектируется тестами вместо реального Claude (ISS-49, стаб не зовёт CLI).
    Опасная тройка: тексты reply не логируем."""
    series_dir = Path(state.get("series_dir") or ".")
    date = state.get("date")
    applied = state.get("applied") or []
    question = state.get("question")
    ans = parse_answer(text)
    # ISS-49: ответ на вопрос бота разбирает ТОТ ЖЕ ИИ-слой, что и ответ на протокол —
    # регулярки остаются быстрым путём для однозначного и фолбэком при недоступном ИИ.
    # На ЯВНОМ откате («не так», «верни») ИИ не зовём вовсе (цикл5 У8): вердикт уже
    # однозначен, а откат — самый нервный момент диалога, лишние секунды там дороже всего.
    plain_rollback = bool(
        ans["rollback"] and applied
        and not (question and question.get("mode") == "clarify_class"))
    smart = None if plain_rollback else _smart_answer(state, question, text,
                                                      answer_fn=answer_fn)
    if smart and smart.get("intent") == "rollback":
        ans = {**ans, "rollback": True}

    # 1) Несогласие (R26): «не так / верни» на ступени-2 → РЕАЛЬНЫЙ reopen применённого.
    #    НО: слова отмены — омонимы. «отмени/отменили» = и «откати», и класс «снято»
    #    (cancelled). Если ждём ответ на clarify_class и человек НАЗВАЛ класс словом — это
    #    ОТВЕТ НА ПЕРЕСПРОС (снять эту задачу), а не откат применённого. Не крадём его в
    #    reopen, иначе вернули бы в работу уже применённое и не сняли спрошенное.
    answering_clarify = bool(
        question and question.get("mode") == "clarify_class"
        and (ans["cls"] or (smart or {}).get("cls")))
    if ans["rollback"] and applied and not answering_clarify:
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
            if smart is not None:
                return _smart_confirm_close(smart, state=state, question=question,
                                            series_dir=series_dir, date=date)
            if ans["yes"] and ans.get("excluded_unresolved"):
                # «да, кроме караоке» без ИИ: что именно оставить — не поняли. Закрыть всё
                # (включая исключённое) нельзя — переспрашиваем номером.
                return {"text": "Понял «да, кроме…», но не понял, что именно оставить. "
                                "Назовите НОМЕР пункта из списка, например «да, кроме 2».",
                        "consume": False, "learned": False}
            if ans["yes"]:
                targets = [o for i, o in enumerate(options, 1) if i not in ans["excluded"]]
            elif ans["digits"]:
                targets = [options[i - 1] for i in sorted(ans["digits"])
                           if 1 <= i <= len(options)]
            elif ans["no"]:
                return {"text": "Ок, ничего не закрываю.", "consume": True, "learned": False}
            else:
                return {"text": _ANSWER_HINT, "consume": False, "learned": False}
            done, already = _apply_many(series_dir, targets, label, date=date)
            text = " ".join(x for x in (_closed_reply(done, label),
                                        _already_suffix(already)) if x)
            # Остаток порции предлагаем ТОЛЬКО когда человек согласился закрыть ВЕСЬ список
            # («да»). При точечном выборе цифрами он выбрал конкретное — не навязываемся
            # (цикл5 НОВ1). Остатка нет → говорим, сколько осталось открытыми (У1/У7).
            rest = list(question.get("rest") or []) if ans["yes"] else []
            return _with_rest_or_summary(
                {"text": text, "consume": True, "learned": False},
                rest=rest, label=label, question=question, series_dir=series_dir)

        if mode == "confirm_foreign":
            # ISS-44.8: чужих задач может быть несколько — один блок «да/нет» на ВСЕ.
            # options/labels — параллельные списки (у каждой чужой задачи свой статус);
            # обратная совместимость со старым одиночным state (`matched`+`label`).
            options = question.get("options") or []
            labels = question.get("labels") or []
            if not options and question.get("matched"):
                options, labels = [question["matched"]], [label]
            multi = len(options) > 1
            ans = _smart_to_ans(smart, len(options)) or ans   # ISS-49: ИИ-первый разбор
            # «нет» проверяем ПЕРВЫМ: ответ-деклайн на ЧУЖУЮ задачу не должен ничего
            # закрывать, даже если в нём мелькнуло число («нет 2») — ложно-закрыть чужое
            # хуже, чем оставить висеть (R5-консерватизм проекта; Н1 цикл5).
            if ans["no"]:
                txt = "Ок, чужие вопросы не трогаю." if multi else "Ок, чужой вопрос не трогаю."
                return {"text": txt, "consume": True, "learned": False}
            if ans["yes"] and ans.get("excluded_unresolved"):
                return {"text": "Понял «да, кроме…», но не понял, что именно оставить. "
                                "Назовите НОМЕР пункта, например «да, кроме 2».",
                        "consume": False, "learned": False}
            if ans["yes"]:
                idxs = [i for i in range(1, len(options) + 1) if i not in ans["excluded"]]
            elif ans["digits"]:
                idxs = [i for i in sorted(ans["digits"]) if 1 <= i <= len(options)]
            else:
                hint = ("Ответьте «да», «нет» или «да, кроме 2» (номер), пожалуйста."
                        if multi else "Ответьте «да» или «нет», пожалуйста.")
                return {"text": hint, "consume": False, "learned": False}
            done, done_label = [], None
            for i in idxs:
                lbl = labels[i - 1] if i - 1 < len(labels) else label
                if _apply_status(series_dir, options[i - 1], lbl, date=date):
                    done.append(options[i - 1])
                    done_label = lbl
            # Сводка: при едином статусе — как «отметил как «X»»; при разных — общий текст.
            uniform = done and len({labels[i - 1] for i in idxs
                                    if i - 1 < len(labels)}) == 1
            if uniform:
                return {"text": _closed_reply(done, done_label),
                        "consume": True, "learned": False}
            if not done:
                return {"text": "Ок, ничего не отметил.", "consume": True, "learned": False}
            names = ", ".join(f"«{_short(t)}»" for t in done)
            return {"text": f"Готово — отметил: {names}.", "consume": True, "learned": False}

        if mode == "pick_target":
            ans = _smart_to_ans(smart, len(options)) or ans   # ISS-49: ИИ-первый разбор
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
            # ISS-49: класс словарём → ИИ (нестандартная формулировка «да всё ок, замяли»).
            cls = ans["cls"] or (smart or {}).get("cls")
            if cls is None:
                return {"text": "Как отметить — «сделано», «снято», «в работе» или «под "
                        "вопросом»? Ответьте словом.", "consume": False, "learned": False}
            ok = _apply_status(series_dir, matched, cls, date=date) if matched else False
            # R24 канонический триггер: человек НАЗВАЛ верный класс слова → запоминаем,
            # чтобы впредь узнать без ИИ (Ф3 дала механику, Ф4 — триггер).
            learned = False
            word = question.get("word")
            # Учим слово ТОЛЬКО когда класс назвал ЧЕЛОВЕК словарным словом (канон R24).
            # Класс, выведенный ИИ по смыслу, в долгую память не кладём — иначе редкая
            # ИИ-догадка навсегда переопределила бы разбор слова без участия человека.
            if ok and word and ans["cls"]:
                learned = dm.remember_word_class(word, cls, source="ack-clarify",
                                                 store_path=store_path)
            return {"text": _closed_reply([matched] if ok else [], cls),
                    "consume": True, "learned": bool(learned)}

    # 3) Reply на ack без вопроса и без «верни» (согласие «ок/спасибо» = молчание) — не
    #    досаждаем повторным ответом; неясное — мягко подсказываем канал несогласия.
    if applied:
        if ans["yes"] or not text.strip():
            return {"text": "", "consume": True, "learned": False}
        # У1 (цикл5): reply на ack, не «да» и не откат — вероятно, добивка нового статуса.
        # Ре-роут в understand вне скоупа Ф4; честно подсказываем оба канала, не молчим.
        return {"text": "Если что-то не так — ответьте «не так» или «верни», поправлю. "
                        "А новый статус по другому вопросу лучше отправить ответом на сам "
                        "протокол встречи.",
                "consume": False, "learned": False}
    return {"text": "", "consume": True, "learned": False}
