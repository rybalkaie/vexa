"""Пометки о спорной разметке спикеров в протоколе (находки section="vocative").

Источник находок — детерминированная кросс-проверка обращений (`vocative_check`):
спикер, подписанный именем N, сам зовёт N по имени — разметка, скорее всего,
перепутала людей. Рендер зовёт `llm_postprocess.apply_review_flags` — тот же контур,
что у authorship (значит из ОБОИХ call-site: finalize + clarify_worker). Владелец
УТР1: помечать, не спрашивать.

Два места:
  • плашка в ШАПКЕ — одна строка, называет спорные пары имён;
  • врезка «⚠️ проверьте: тут мог быть Y, а не X» — ТОЛЬКО в блоке «Задачи»
    (подзаголовок исполнителя и строки задач): там ошибка в имени стоит дорого —
    задача уйдёт не тому человеку. В темах и решениях имя из пары — пересказ
    обсуждения, а врезка у каждой такой строки топила смысл в шуме (слово владельца:
    пометки только у задач). Блок задач узнаётся тем же правилом, что разбор задач
    в памяти серии (`series_memory._classify_heading`), — пометка и перенос задач в
    дайджест не разойдутся.

Чистые функции (без IO/claude). PII: имена уже в протоколе, наружу ничего нового.
"""

from __future__ import annotations

import re
from typing import Optional

from . import series_memory
from . import vocative_check

# Маркер пометки — тот же, что `llm_postprocess.REVIEW_FLAG_MARKER` (идемпотентность
# общего контура пометок). Модуль не импортирует llm_postprocess — иначе круг импорта.
REVIEW_FLAG_MARKER = "⚠️"
SPEAKER_PLAQUE_SENTINEL = "Разметка спикеров под вопросом"
# Маркер inline-врезки (для идемпотентности повторного прохода).
_VOCATIVE_INLINE_MARKER = "проверьте: тут мог быть"


def _first_body_section_idx(lines: list[str]) -> Optional[int]:
    """Индекс первой строки-секции `## ` (граница шапка↔тело). None — секций нет."""
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## "):
            return i
    return None


def _format_plaque_pairs(pairs: list[tuple]) -> str:
    """(X,Y) пары → «X и Y; A и B» для текста плашки."""
    return "; ".join(f"{a} и {b}" for a, b in pairs)


def _insert_speaker_plaque(protocol_text: str, pairs: list[tuple]) -> str:
    """Ф5 (R19, место №1): плашка «мог перепутать X и Y — проверьте» в ШАПКЕ протокола.

    Ставится строго ДО первой `## `-секции — как постоянный дисклеймер Ф4а (иначе
    утекла бы в series_memory-дайджест, который собирает только тело `## `-секций).
    Идемпотентно по `SPEAKER_PLAQUE_SENTINEL`: повторный проход дубль не плодит,
    а список пар обновляет (снимаем старую строку и ставим новую с актуальными
    именами). Markdown-цитата курсивом — как дисклеймер (служебная заметка, не контент).
    """
    if not pairs:
        return protocol_text
    text = protocol_text or ""
    lines = text.splitlines()
    if not lines:
        return text
    # Снять прежнюю плашку (если имена изменились между перевыпусками).
    lines = [ln for ln in lines if SPEAKER_PLAQUE_SENTINEL not in ln]
    plaque = (
        f"> ⚠️ _{SPEAKER_PLAQUE_SENTINEL}: мог перепутать "
        f"{_format_plaque_pairs(pairs)} — проверьте, кто где._"
    )
    insert_at = _first_body_section_idx(lines)
    if insert_at is None:
        # Нет секций → после шапки (первый `---`), иначе после первой строки.
        insert_at = next((i + 1 for i, ln in enumerate(lines) if ln.strip() == "---"), 1)
    block = ["", plaque, ""]
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    # Схлопываем прогоны пустых строк в одну — иначе снятие/повторная вставка плашки
    # копит осиротевшие пустые строки (нарушая идемпотентность повторного прохода).
    collapsed: list[str] = []
    for ln in new_lines:
        if ln.strip() == "" and collapsed and collapsed[-1].strip() == "":
            continue
        collapsed.append(ln)
    result = "\n".join(collapsed)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _task_block_lines(lines: list[str], body_start: int) -> list[bool]:
    """Для каждой строки — лежит ли она в блоке «Задачи» (сами заголовки — нет).

    Раздел узнаётся тем же правилом, что в `series_memory.extract_open_tasks`:
    заголовок `##`…`####`, без нумерации «3) » и эмфазы → `_classify_heading`.
    «🔻 С прошлых встреч» — свой раздел (carryover), не задачи этой встречи: владельцы
    там пришли из прошлых протоколов, путаница спикеров этой встречи их не меняет.
    """
    out = [False] * len(lines)
    section: Optional[str] = None
    for i in range(body_start, len(lines)):
        hm = series_memory._HEADING_RE.match(lines[i])
        if hm:
            title = series_memory._THEME_NUM_PREFIX_RE.sub("", hm.group(1).strip()).strip()
            title = re.sub(r"[*_`]{1,2}", "", title).strip()
            section = series_memory._classify_heading(title)
            continue
        out[i] = section == "tasks"
    return out


def _with_flag(line: str, flag: str) -> str:
    """Дописать врезку в строку. Строку таблицы задач (`| … |`) — внутрь последней
    ячейки: хвост после закрывающей `|` стал бы лишней колонкой в PDF и Telegram
    (память серии ⚠️-хвост в ячейке всё равно срезает — `_clean_table_cell`)."""
    body = line.rstrip()
    stripped = body.strip()
    if len(stripped) > 1 and stripped.startswith("|") and stripped.endswith("|"):
        return body[:-1].rstrip() + f"  {flag} |"
    return body + f"  {flag}"


def apply_vocative_flags(protocol_text: str, vocative_findings: list[dict]) -> str:
    """Рендер находок section="vocative": плашка в шапке + врезка у задач.

    Место №1 — плашка в шапке (называет спорные имена X/Y).
    Место №2 (RAZM2) — врезка «⚠️ проверьте: тут мог быть Y, а не X» у строк блока
    «Задачи», где фигурирует спорное имя: подзаголовок исполнителя `**Имя**` и строки
    задач (дословных реплик в саммари нет, поэтому привязываемся к именам). Темы,
    решения, шапка и «С прошлых встреч» врезку не получают — спорную пару там несёт
    плашка. Идемпотентно.
    """
    if not vocative_findings:
        return protocol_text
    # Пары для плашки (дедуп, стабильный порядок).
    pairs: list[tuple] = []
    seen_pairs: set[tuple] = set()
    for f in vocative_findings:
        pr = f.get("pair") or []
        if len(pr) == 2:
            key = tuple(sorted(pr))
            if key not in seen_pairs:
                seen_pairs.add(key)
                pairs.append(key)
    text = _insert_speaker_plaque(protocol_text, pairs)

    # Врезки в блоке «Задачи». Перечень (имя → note) для inline.
    lines = text.split("\n")
    body_start = _first_body_section_idx(lines)
    if body_start is None:
        return text
    in_tasks = _task_block_lines(lines, body_start)
    for f in vocative_findings:
        name = (f.get("quote") or "").strip()
        note = (f.get("note") or "").strip()
        if not name or not note:
            continue
        # Тело протокола почти всегда подписано КОРОТКИМ именем («Илья»), а метка
        # спикера в транскрипте — часто полная двухсловная («Илья Рыбалка»). Привязка
        # по первому токену покрывает оба варианта; иначе врезка (место №2, RAZM2) не
        # встала бы на большинстве реальных встреч (≈60% меток двухсловные — замер
        # ход3 цикла5). Over-match тёзок невозможен: при неоднозначном первом имени
        # конфликт вообще не рождается (`vocative_check.addressed_names` → ambiguous).
        match_name = vocative_check._first_name_token(name) or name
        name_re = re.compile(
            r"(?<![а-яёА-ЯЁ])" + re.escape(match_name) + r"(?![а-яёА-ЯЁ])", re.IGNORECASE
        )
        flag = f"{REVIEW_FLAG_MARKER} проверьте: {note}"
        for i in range(body_start, len(lines)):
            if not in_tasks[i]:
                continue
            ln = lines[i]
            stripped = ln.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(">"):
                continue
            if not name_re.search(ln):
                continue
            # Идемпотентность: на строке уже стоит вокатив-врезка — не дублируем.
            if _VOCATIVE_INLINE_MARKER in ln:
                continue
            lines[i] = _with_flag(ln, flag)
    return "\n".join(lines)
