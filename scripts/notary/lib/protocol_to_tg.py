"""Форматирование структурного `<date>-protokol.md` под текст Telegram-сообщения.

Образец (зафиксирован владельцем 29.05 по скриншотам
`~/Screenshots/Screenshot 2026-05-29 at 12.44.{05,11}.png` + 5 правок надиктовкой):

    📋 ПРОТОКОЛ ВСТРЕЧИ — 29.05.2026
    #протоколвстречи

    Синхронизация по вайб-кодингу — проекты, инструменты, команда.
    ⏱ 45 мин   👥 Илья Рыбалка, Михаил Еремеев

    1️⃣ ДИЛЕРСКИЙ КАБИНЕТ И ВНУТРЕННИЙ ПОРТАЛ
    • Илья сделал кабинет с двусторонней Битрикс-интеграцией.
    • Внутренний портал — сотрудники видят активность дилеров.

    2️⃣ САЙТ КОМПАНИИ
    • К сайту не приступал, менее приоритетен.

    ✅ РЕШЕНИЯ
    • Двусторонняя интеграция с 1С — отложена.

    📌 ЗАДАЧИ
    Илья Рыбалка:
    • К понедельнику: оформить подписку Claude для команды.
    Михаил:
    • Пройти 10-дневный практикум.

Правки владельца 29.05 (применены ниже):
  1. Маркер буллетов слева — `•` (жирная точка), не тёмные квадраты.
  2. Дата в шапке — после «ПРОТОКОЛ ВСТРЕЧИ» через тире (DD.MM.YYYY).
  3. **Хэштег `#протоколвстречи` — на 2-й строке шапки**, под заголовком
     `📋 ПРОТОКОЛ ВСТРЕЧИ — DD.MM.YYYY`. Не в подвале.
  4. Участники — «Имя Фамилия». Резолв через `_methods/people.md` (на VPS
     синкается из `~/Projects/me/people.md` через rsync).
  5. Длительность — РЕАЛЬНАЯ речь, не время в звонке (см. Ф3, lastSpeechMs).
     Сейчас (Ф1) fallback — `endTs - startTs`. После Ф3 — `lastSpeechMs -
     firstSpeechMs`, после Ф5 — sum по chunks[]. См. compute_duration_label().

TODO (после Ф3): переключить compute_duration_label() с fallback
`endTs - startTs` на `meta.recording.lastSpeechMs - meta.recording.firstSpeechMs`.
TODO (после Ф5): суммировать по `meta.recording.chunks[]`.

Без MarkdownV2-экранирования — плоский текст + эмодзи (владелец явно сказал).
Telegram сам подсветит хэштег и ничего не сломает на спецсимволах в именах.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Маркер буллетов — в одном месте, легко менять при необходимости (правка #1).
BULLET = "•"

# Лимит Telegram на одно сообщение.
TG_MAX_LEN = 4096

# Хэштег — на 2-й строке шапки (правка #3 владельца).
HASHTAG = "#протоколвстречи"

# Эмодзи-индикаторы основных разделов (1️⃣2️⃣…9️⃣). Хранятся как chars из
# unicode-keycap range. Если разделов >9 — fallback на «🔟», далее «🔢» (см. ниже).
_NUMBER_EMOJI = [
    "1️⃣", "2️⃣", "3️⃣", "4️⃣",
    "5️⃣", "6️⃣", "7️⃣", "8️⃣",
    "9️⃣", "\U0001f51f",  # 🔟
]

# Заголовки секций, которые имеют специальный эмодзи (не цифру).
# Сопоставление по lowercase + collapse whitespace для устойчивости к
# мелким разночтениям в шапках протокола.
_SPECIAL_SECTION_EMOJI = (
    # (regex по нормализованному lowercase-заголовку, emoji-префикс)
    (re.compile(r"^(решени[ея]|решения[\s/]+что внедряем|решения и задачи)$"), "✅ "),
    (re.compile(r"^задачи$"), "📌 "),
)

# Маркеры буллетов, которые встречаются в `<date>-protokol.md` — превращаем в BULLET.
# `*` буллет НЕ поддерживаем намеренно — это создаёт коллизию с `**bold**`
# (regex заматчит первую `*` как маркер и оставит `*Имя**` в теле). В наших
# структурных протоколах буллеты только эмодзи + дефис.
_BULLET_MARKERS = ("▪️", "▫️", "🔸", "🟠", "-")

# Регекс одного буллета на старте строки. После маркера обязателен пробел.
# Bold-имена (`**Илья**`) распознаются ДО буллета (через _BOLD_NAME_BLOCK_RE) —
# поэтому одиночный `**` после маркера (`▪️ **что-то**`) спокойно матчится
# как буллет с inline-bold, который потом снимется `re.sub(r"\*\*...\*\*")`.
_BULLET_LINE_RE = re.compile(
    r"^(?:\s*)(?:" + "|".join(re.escape(m) for m in _BULLET_MARKERS) + r")\s+(.+)$"
)

# Bold-имя в строке секции «📌 Задачи»: `**Имя Фамилия**` или `**Имя**`.
# Поддерживаем как блок шапки имени (одна строка `**Имя**`), так и однострочный
# вариант `**Имя:** задача` (старый формат).
_BOLD_NAME_BLOCK_RE = re.compile(r"^\*\*([^*]+?)\*\*\s*:?\s*$")
_BOLD_NAME_INLINE_RE = re.compile(r"^\*\*([^*]+?)\*\*\s*:\s*(.+)$")

# Шапка структурного протокола от Claude Sonnet 4.6 (см. `kak-delat-protokol-vstrechi.md`):
#   `#протоколвстречи DD.MM.YYYY`
#   `**Встреча:** <тема>`
#   `**Длительность:** 1 ч 05 мин`
#   `**Участники:** Михаил, Илья Рыбалка`
#   `**Транскрипт:** [<date>.md](<date>.md)`
_HEADER_FIELD_RE = re.compile(r"^\*\*([^*:]+?):\*\*\s*(.+)$")


# --- Резолв «Имя» → «Имя Фамилия» через people.md (правка #4) -----------


# Дефолтные пути к `people.md`. На VPS deploy через rsync (`/srv/.../_methods/`),
# на маке — оригинал в `~/Projects/me/people.md`.
_PEOPLE_PATHS = (
    "/srv/meeting-notary/_methods/people.md",
    "/opt/meeting-notary/_methods/people.md",
    os.path.expanduser("~/Projects/me/people.md"),
)

# Имя владельца обычно НЕ в people.md (он сам — Илья Рыбалка). Принимаем как
# хардкод: если в meta.expectedParticipants приходит «Илья Рыбалка», ничего
# резолвить не надо. Если приходит просто «Илья» — пытаемся прочитать `about.md`,
# но это редкий путь — обычно meta уже содержит полное имя владельца.
_OWNER_FALLBACK = {"Илья": "Илья Рыбалка"}


def _read_people_md() -> Optional[str]:
    """Читает первый существующий people.md из дефолтных путей или env.

    У6 хода 2: логирует размер найденного файла. Если файл устарел (rsync
    с мака на VPS завис — известный риск из плана 28.05 Ф4), мы тихо
    fallback'немся на короткие имена. Лог поможет диагностировать это
    в боевом журнале.
    """
    env_path = os.environ.get("MEETING_NOTARY_PEOPLE_PATH")
    candidates = [env_path] if env_path else []
    candidates.extend(_PEOPLE_PATHS)
    for p in candidates:
        if not p:
            continue
        try:
            txt = Path(p).read_text(encoding="utf-8")
        except OSError:
            continue
        logger.info("[protocol_to_tg] people.md loaded: %d bytes from %s", len(txt), p)
        return txt
    logger.warning(
        "[protocol_to_tg] people.md not found (tried: %s) — "
        "fallback to short names only",
        [p for p in candidates if p],
    )
    return None


# Bold-имя в строке маркированного списка: `- **Имя Фамилия** — …`,
# `- **Имя Фамилия (девичья)** — …`, и т.п. Допускаем латиницу/кириллицу/дефис/пробел.
_PEOPLE_BOLD_RE = re.compile(r"-\s*\*\*([\wА-Яа-яЁёA-Za-z\s\-()]+?)\*\*")


def _extract_names_from_people(text: str) -> list[str]:
    """Выдаёт список «Имя» или «Имя Фамилия» из bold-блоков people.md.

    Скоуп: только bold-имена в списочных строках (- **...**). Прочие
    упоминания внутри прозы игнорируем, чтобы не подцепить «Алла»
    из текста описания.
    """
    names: list[str] = []
    for m in _PEOPLE_BOLD_RE.finditer(text):
        name = m.group(1).strip()
        # Отрезаем «(девичья)» / «(Михина — текущая)» и подобное.
        name = re.sub(r"\s*\(.+?\)\s*", " ", name).strip()
        # Убираем дублирование пробелов.
        name = re.sub(r"\s+", " ", name)
        if name:
            names.append(name)
    return names


def _resolve_full_name(short_name: str, people_names: list[str]) -> str:
    """Резолв «Имя» → «Имя Фамилия» по списку из people.md.

    Алгоритм (правка #4 + защита РАЗМ2):
      - Если short_name уже содержит пробел (Имя Фамилия) — возвращаем как есть.
      - Если ровно ОДИН full_name начинается с этого имени — возвращаем full.
      - N > 1 совпадений → fallback на короткое имя + WARN-лог (не сохраняем
        в meta — это runtime info, не persistent state).
      - 0 совпадений → fallback _OWNER_FALLBACK (для владельца), потом — само имя.
    """
    if not short_name:
        return short_name
    if " " in short_name.strip():
        return short_name.strip()

    needle = short_name.strip()
    matches = []
    for full in people_names:
        first = full.split()[0] if full else ""
        if first == needle and len(full.split()) > 1:
            matches.append(full)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning(
            "[protocol_to_tg] multiple matches for %r in people.md: %s — "
            "fallback to first-name only",
            needle, matches,
        )
        return needle
    # 0 матчей — пытаемся owner fallback.
    if needle in _OWNER_FALLBACK:
        return _OWNER_FALLBACK[needle]
    return needle


def _resolve_participants(meta: dict) -> list[str]:
    """Собирает список «Имя Фамилия» для шапки TG.

    Берём `meta.participants` (Telemost UI) + `meta.expectedParticipants`
    (watched.yaml). Резолвим имена-без-фамилии через people.md. Сохраняем
    порядок, дедуп — по полному имени.
    """
    raw_participants = meta.get("participants") or []
    raw_expected = meta.get("expectedParticipants") or []

    # Сначала expected (они обычно уже «Имя Фамилия» из watched.yaml),
    # потом participants (могут быть «Имя» из Telemost UI).
    candidates: list[str] = []
    for n in list(raw_expected) + list(raw_participants):
        if isinstance(n, str) and n.strip():
            candidates.append(n.strip())

    people_md = _read_people_md()
    people_names: list[str] = _extract_names_from_people(people_md) if people_md else []

    # НОВ3 хода 4: двухпроходный сбор, чтобы порядок входа не определял
    # «победителя» при коллизии «Михаил» vs «Михаил Еремеев». Сначала
    # резолвим все имена, потом для каждого first-name берём САМЫЙ
    # длинный вариант (`Михаил Еремеев` лучше чем `Михаил`).
    resolved_raw: list[str] = []
    for raw in candidates:
        full = _resolve_full_name(raw, people_names)
        # У4 хода 2: пустая строка после resolve — пропускаем, иначе
        # в шапке будет висящая запятая («👥 , Михаил»).
        if not full or not full.strip():
            continue
        resolved_raw.append(full.strip())

    # Группируем по first-name; выбираем самый «полный» вариант (с фамилией).
    best_by_first: dict[str, str] = {}
    order: list[str] = []  # порядок first-name'ов
    for full in resolved_raw:
        first = full.split()[0] if full.split() else full
        if first not in best_by_first:
            best_by_first[first] = full
            order.append(first)
        else:
            # Уже есть кандидат для этого first. Предпочитаем тот, что
            # содержит больше слов (фамилия > короткое имя).
            if len(full.split()) > len(best_by_first[first].split()):
                best_by_first[first] = full

    # Финальный список + дедуп по полному имени (для случая, когда
    # два разных first-name'а резолвятся в одно полное имя — крайне маловероятно).
    resolved: list[str] = []
    seen: set[str] = set()
    for first in order:
        full = best_by_first[first]
        if full in seen:
            continue
        seen.add(full)
        resolved.append(full)
    return resolved


# --- Длительность речи (правка #5 владельца + НЕС1 приоритет источников) -


def compute_duration_label(meta: dict) -> str:
    """Возвращает «X ч Y мин» / «Y мин» / «<1 мин».

    Приоритет источников (НЕС1):
      1. `meta.recording.chunks[]` (после Ф5) — sum (lastSpeechMs - firstSpeechMs).
      2. `meta.recording.firstSpeechMs/lastSpeechMs` (после Ф3) — single-chunk.
      3. Fallback (Ф1) — `endTs - startTs` (текущая логика).

    TODO(Ф3): после прихода `firstSpeechMs`/`lastSpeechMs` в meta — это станет
    основным путём. TODO(Ф5): chunks[] — суммарная речь, не время в звонке.
    """
    rec = meta.get("recording") or {}

    # (1) chunks[]
    chunks = rec.get("chunks")
    if isinstance(chunks, list) and chunks:
        total_ms = 0
        ok = True
        for ch in chunks:
            if not isinstance(ch, dict):
                ok = False
                break
            fs = ch.get("firstSpeechMs")
            ls = ch.get("lastSpeechMs")
            if not (isinstance(fs, (int, float)) and isinstance(ls, (int, float))):
                ok = False
                break
            total_ms += max(0, int(ls) - int(fs))
        if ok and total_ms > 0:
            return _format_ms(total_ms)

    # (2) single-chunk first/last
    fs = rec.get("firstSpeechMs")
    ls = rec.get("lastSpeechMs")
    if isinstance(fs, (int, float)) and isinstance(ls, (int, float)) and ls > fs:
        return _format_ms(int(ls) - int(fs))

    # (3) fallback endTs - startTs
    start_ts = meta.get("startTs")
    end_ts = meta.get("endTs")
    start_dt = _parse_iso_or_none(start_ts)
    end_dt = _parse_iso_or_none(end_ts)
    if start_dt is not None and end_dt is not None and end_dt > start_dt:
        ms = int((end_dt - start_dt).total_seconds() * 1000)
        return _format_ms(ms)

    # Дополнительный фолбэк: текстовое поле, которое Sonnet кладёт в шапку
    # структурного протокола («Длительность: 1 ч 05 мин»).
    txt = meta.get("durationLabel")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    return "—"


def _parse_iso_or_none(s):
    if not isinstance(s, str) or not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _format_ms(ms: int) -> str:
    total_sec = ms // 1000
    minutes = total_sec // 60
    if minutes < 1:
        return "<1 мин"
    hours, mins = divmod(minutes, 60)
    if hours == 0:
        return f"{mins} мин"
    return f"{hours} ч {mins:02d} мин"


# --- Парсинг структурного `.md` -----------------------------------------


def _normalize_heading_for_emoji(text: str) -> str:
    """Lowercase + collapse internal whitespace для матчинга специальных секций."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _section_emoji_for(idx: int, raw_heading: str) -> str:
    """Возвращает префикс эмодзи для секции (с пробелом)."""
    # Зачищаем «## 1) Заголовок» → «Заголовок».
    text = raw_heading.strip()
    # Отрезаем `<digit>)` / `<digit>.` ведущий нумератор, если есть.
    text = re.sub(r"^\d+\s*[\)\.]\s*", "", text)
    norm = _normalize_heading_for_emoji(text)
    for pat, prefix in _SPECIAL_SECTION_EMOJI:
        if pat.match(norm):
            return prefix
    # Обычная нумерованная секция.
    if 0 <= idx < len(_NUMBER_EMOJI):
        return f"{_NUMBER_EMOJI[idx]} "
    # Фоллбэк — без эмодзи (если >10 секций — крайний случай).
    return ""


def _strip_section_number(raw_heading: str) -> str:
    """Удаляет `<digit>)`/`<digit>.` из начала, оставляет только текст заголовка."""
    return re.sub(r"^\d+\s*[\)\.]\s*", "", raw_heading.strip()).strip()


def _process_bullet_line(text: str) -> Optional[str]:
    """Конвертирует строку буллета `<marker> <text>` → `• <text>`.

    Возвращает None если строка не является буллетом.
    """
    m = _BULLET_LINE_RE.match(text)
    if not m:
        return None
    body = m.group(1).strip()
    # Внутри тела убираем bold (`**`) — TG не подсветит, оставит звёздочки.
    body = re.sub(r"\*\*([^*]+?)\*\*", r"\1", body)
    return f"{BULLET} {body}"


def _parse_protocol_md(text: str) -> dict:
    """Парсит структурный `.md` в dict {header, sections}.

    header: {date, theme, duration, participants, transcript_link}
    sections: [{idx, heading, emoji_prefix, body_lines: [str]}]
              где body_lines уже нормализованы (буллеты с `•`, имена в задачах).

    Для секции «Задачи» поддержан spec-парсинг: блоки `**Имя**` сохраняются
    как отдельные строки `<Имя>:` (формат образца).
    """
    lines = text.splitlines()

    header = {
        "date": "",
        "theme": "",
        "duration": "",
        "participants": "",
        "transcript_link": "",
    }
    sections: list[dict] = []
    cur_section: Optional[dict] = None

    # Парсим шапку: первые строки до первого `## ` (раздела).
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Заголовок секции.
        if stripped.startswith("## "):
            break

        # Хэштег с датой: `#протоколвстречи DD.MM.YYYY` или `#протоколвстречи` + дата отдельно.
        if not header["date"]:
            m = re.match(r"^#\S*\s+(\d{2}\.\d{2}\.\d{4})\s*$", stripped)
            if m:
                header["date"] = m.group(1)
                i += 1
                continue
            # Альтернатива — дата идёт сама по себе.
            m = re.match(r"^(\d{2}\.\d{2}\.\d{4})\s*$", stripped)
            if m and stripped:
                header["date"] = m.group(1)
                i += 1
                continue

        # Поля шапки `**Поле:** значение`.
        fm = _HEADER_FIELD_RE.match(stripped)
        if fm:
            field = fm.group(1).strip().lower()
            value = fm.group(2).strip()
            if "встреч" in field:
                header["theme"] = value
            elif "длитель" in field:
                header["duration"] = value
            elif "участ" in field:
                header["participants"] = value
            elif "транскрип" in field:
                header["transcript_link"] = value
            i += 1
            continue

        # Прочее в шапке (пустые / разделители) — пропускаем.
        i += 1

    # Парсим секции.
    section_idx = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("## "):
            heading_raw = stripped[3:].strip()
            # Закрываем предыдущую секцию.
            if cur_section is not None:
                sections.append(cur_section)
            cur_section = {
                "idx": section_idx,
                "heading_raw": heading_raw,
                "heading_text": _strip_section_number(heading_raw),
                "emoji_prefix": _section_emoji_for(section_idx, heading_raw),
                "body_lines": [],
            }
            # Numeric или специальные → увеличиваем счётчик секций (специальные
            # тоже считаем, чтобы не сбить нумерацию следующих).
            # Но Решения/Задачи не должны «съесть» цифру у обычных секций ниже.
            # На практике в наших протоколах после Решений/Задач секций нет.
            # Проще: счётчик растёт только для НЕ-специальных.
            norm = _normalize_heading_for_emoji(_strip_section_number(heading_raw))
            is_special = any(p.match(norm) for p, _ in _SPECIAL_SECTION_EMOJI)
            if not is_special:
                section_idx += 1
            i += 1
            continue

        # `---` разделитель — пропускаем.
        if stripped == "---":
            i += 1
            continue

        if cur_section is None:
            # До первой `## ` (т.е. подвал шапки) — пропускаем.
            i += 1
            continue

        # Bold-имя в секции «📌 Задачи»: блок `**Илья Рыбалка**` — проверяем
        # ДО буллета, чтобы не поймать `**` как маркер списка.
        bn = _BOLD_NAME_BLOCK_RE.match(stripped)
        if bn:
            # Имя как заголовок-блок: `Имя:`
            name = bn.group(1).strip()
            cur_section["body_lines"].append(f"{name}:")
            i += 1
            continue

        # Inline `**Имя:** задача` → `Имя:` + `• задача`
        bi = _BOLD_NAME_INLINE_RE.match(stripped)
        if bi:
            cur_section["body_lines"].append(f"{bi.group(1).strip()}:")
            cur_section["body_lines"].append(f"{BULLET} {bi.group(2).strip()}")
            i += 1
            continue

        # Проверяем буллет.
        bullet = _process_bullet_line(line)
        if bullet is not None:
            cur_section["body_lines"].append(bullet)
            i += 1
            continue

        # Пустая строка — игнорируем (буллеты сольются, нам так и надо).
        if not stripped:
            i += 1
            continue

        # Любая прочая строка — кладём как есть (без bold-разметки).
        plain = re.sub(r"\*\*([^*]+?)\*\*", r"\1", stripped)
        if plain:
            cur_section["body_lines"].append(plain)
        i += 1

    if cur_section is not None:
        sections.append(cur_section)

    return {"header": header, "sections": sections}


# --- Формирование текста для TG -----------------------------------------


def _format_header(parsed_header: dict, meta: dict) -> str:
    """Шапка TG-сообщения. Правка #3: хэштег на 2-й строке."""
    date_str = parsed_header.get("date") or ""
    if not date_str:
        # Если структурный .md без явной даты — берём из meta.
        ds = meta.get("date") or (meta.get("startTs") or "")[:10]
        if ds and re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
            yyyy, mm, dd = ds.split("-")
            date_str = f"{dd}.{mm}.{yyyy}"
        else:
            date_str = ds or "—"

    theme = (parsed_header.get("theme") or "").strip()

    # Резолв «Имя Фамилия» (правка #4). Берём из meta, не из строкового поля
    # шапки — meta даёт исходный список, а строка шапки могла содержать «Михаил»
    # вместо «Михаил Еремеев», и мы хотим обогатить.
    resolved = _resolve_participants(meta)
    if not resolved and parsed_header.get("participants"):
        # Фолбэк — строка шапки структурного протокола («Михаил, Илья Рыбалка»).
        resolved = [p.strip() for p in parsed_header["participants"].split(",") if p.strip()]
    participants_str = ", ".join(resolved) if resolved else "—"

    duration = compute_duration_label(meta)
    if duration == "—" and parsed_header.get("duration"):
        duration = parsed_header["duration"]

    lines = [
        f"📋 ПРОТОКОЛ ВСТРЕЧИ — {date_str}",
        HASHTAG,
        "",
    ]
    if theme:
        lines.append(theme)
    lines.append(f"⏱ {duration}   👥 {participants_str}")
    return "\n".join(lines)


def _format_section(section: dict) -> str:
    """Превращает разобранную секцию в блок TG-текста."""
    heading = section["heading_text"]
    prefix = section["emoji_prefix"]
    # Заголовок ВЕРХНИМ РЕГИСТРОМ (правка из образца).
    heading_line = f"{prefix}{heading.upper()}"
    out = [heading_line]
    out.extend(section["body_lines"])
    return "\n".join(out)


def format_protocol_as_tg_text(protocol_md: str, meta: dict) -> str:
    """Главная функция: структурный `.md` → текст для Telegram-сообщения.

    Возвращает один большой текст. Split на части — отдельная функция
    `split_protocol_smart`.

    Сигнатура контракта: НЕ принимает `part`-аргумент. Multi-chunk
    (`part_N_of_M` / `final_merged`) отодвинут в Ф5 (РИСК2) — там будет
    другая сигнатура с поддержкой блока «Часть N».
    """
    parsed = _parse_protocol_md(protocol_md or "")
    header = _format_header(parsed["header"], meta or {})
    blocks = [header]
    for section in parsed["sections"]:
        if not section["body_lines"] and not section["heading_text"]:
            continue
        blocks.append(_format_section(section))
    # Между шапкой и секциями + между секциями — одна пустая строка.
    return "\n\n".join(blocks).rstrip() + "\n"


# --- Smart-split ---------------------------------------------------------


# Префиксы, по которым `split_protocol_smart` понимает «начало секции».
# Берём весь Unicode-keycap для цифр + спец-эмодзи.
_SECTION_START_PREFIXES = tuple(_NUMBER_EMOJI) + ("✅", "📌")


def _is_section_start(line: str) -> bool:
    s = line.lstrip()
    return any(s.startswith(p) for p in _SECTION_START_PREFIXES)


def _is_header_start(line: str) -> bool:
    return line.lstrip().startswith("📋")


def _is_tasks_section(block: str) -> bool:
    head = block.split("\n", 1)[0].lstrip()
    return head.startswith("📌")


def _split_block_by_paragraphs(block: str, max_len: int) -> list[str]:
    """Жёсткий fallback: режем огромный блок по `\n\n`, потом `\n`, потом по словам.

    Гарантия: никаких частей > max_len и никогда не рубим посреди слова.
    """
    if len(block) <= max_len:
        return [block]
    parts: list[str] = []
    paragraphs = block.split("\n\n")
    cur = ""
    for p in paragraphs:
        candidate = (cur + ("\n\n" if cur else "") + p)
        if len(candidate) <= max_len:
            cur = candidate
        else:
            if cur:
                parts.append(cur)
            if len(p) <= max_len:
                cur = p
            else:
                # Параграф сам по себе слишком длинный — режем по `\n`.
                cur = ""
                line_parts: list[str] = []
                line_cur = ""
                for ln in p.split("\n"):
                    cand = (line_cur + ("\n" if line_cur else "") + ln)
                    if len(cand) <= max_len:
                        line_cur = cand
                    else:
                        if line_cur:
                            line_parts.append(line_cur)
                        if len(ln) <= max_len:
                            line_cur = ln
                        else:
                            # Строка > max_len — режем по словам.
                            line_cur = ""
                            word_cur = ""
                            for word in ln.split(" "):
                                wc = (word_cur + (" " if word_cur else "") + word)
                                if len(wc) <= max_len:
                                    word_cur = wc
                                else:
                                    if word_cur:
                                        line_parts.append(word_cur)
                                    word_cur = word[:max_len]
                            if word_cur:
                                line_parts.append(word_cur)
                if line_cur:
                    line_parts.append(line_cur)
                parts.extend(line_parts)
    if cur:
        parts.append(cur)
    return parts


def _split_tasks_block(block: str, max_len: int) -> list[str]:
    """Особый split для «📌 ЗАДАЧИ»: границы — на `<Имя>:` строках.

    При разрыве в продолжении дописывает заголовок «📌 ЗАДАЧИ (продолжение)»,
    чтобы было видно, что это та же секция.
    """
    lines = block.split("\n")
    # Первая строка — заголовок. Остальное — тело.
    if not lines:
        return [block]
    header_line = lines[0]
    body = lines[1:]

    # Группируем тело по «Имя:» — каждый блок начинается со строки, оканчивающейся ":".
    groups: list[list[str]] = []
    current: list[str] = []
    for ln in body:
        if ln.endswith(":") and not ln.startswith(BULLET):
            if current:
                groups.append(current)
            current = [ln]
        else:
            current.append(ln)
    if current:
        groups.append(current)

    parts: list[str] = []
    cur_text = header_line
    cont_header = "📌 ЗАДАЧИ (продолжение)"
    for gr in groups:
        gr_text = "\n".join(gr)
        candidate = cur_text + "\n" + gr_text
        if len(candidate) <= max_len:
            cur_text = candidate
        else:
            if cur_text and cur_text != header_line:
                parts.append(cur_text)
            new_start = f"{cont_header}\n{gr_text}"
            if len(new_start) <= max_len:
                cur_text = new_start
            else:
                # Н7 хода 1: группа сама по себе слишком длинная — fallback
                # на абзацы. Раньше cont_header добавлялся только в первый
                # кусок результата, последующие шли без заголовка → читатель
                # видел оторванные буллеты. Добавляем заголовок в КАЖДЫЙ
                # кусок результата (кроме первого, в котором он уже встроен).
                cont_parts = _split_block_by_paragraphs(new_start, max_len)
                # Первый кусок уже содержит cont_header (см. new_start).
                # Остальные — переоборачиваем с шапкой.
                fixed_parts = [cont_parts[0]]
                for p in cont_parts[1:]:
                    candidate_p = f"{cont_header}\n{p}"
                    if len(candidate_p) <= max_len:
                        fixed_parts.append(candidate_p)
                    else:
                        # Заголовок не влезает — режем тело ещё короче.
                        sub_max = max_len - len(cont_header) - 1
                        sub = _split_block_by_paragraphs(p, max(1, sub_max))
                        for s in sub:
                            fixed_parts.append(f"{cont_header}\n{s}")
                parts.extend(fixed_parts[:-1])
                cur_text = fixed_parts[-1]
    if cur_text and (cur_text != header_line):
        parts.append(cur_text)
    elif not parts:
        # Заголовок без тела — единственный кусок.
        parts.append(header_line)
    return parts


def split_protocol_smart(text: str, max_len: int = TG_MAX_LEN) -> list[str]:
    """Разбивает отформатированный текст протокола по логическим границам.

    Алгоритм:
      1. Идём по тексту, накапливая блоки. Блок начинается с шапки `📋`
         или с секции (`1️⃣`/`✅`/`📌`).
      2. Если следующий блок целиком не помещается в текущий чанк — закрываем
         текущий чанк, начинаем новый с этого блока.
      3. Шапка всегда едет с первым чанком (по построению она — первый блок).
      4. Секция «📌 ЗАДАЧИ» при превышении max_len разрывается на границах
         `<Имя>:` (см. `_split_tasks_block`).
      5. Если одна секция > max_len — fallback на параграфы/строки/слова
         (`_split_block_by_paragraphs`). Никогда — посреди слова.
    """
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    # Шаг 1. Разбиваем на блоки по границам секций.
    lines = text.split("\n")
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if _is_header_start(line) or _is_section_start(line):
            if current:
                blocks.append("\n".join(current).rstrip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).rstrip())

    # Шаг 2. Складываем блоки в чанки.
    chunks: list[str] = []
    cur = ""
    for block in blocks:
        if len(block) > max_len:
            # Сначала закрываем то, что накопили.
            if cur:
                chunks.append(cur)
                cur = ""
            if _is_tasks_section(block):
                expanded = _split_tasks_block(block, max_len)
            else:
                expanded = _split_block_by_paragraphs(block, max_len)
            # Последний кусок expanded — попробуем продолжить в нём.
            if len(expanded) > 1:
                chunks.extend(expanded[:-1])
                cur = expanded[-1]
            else:
                cur = expanded[0]
            continue
        candidate = cur + ("\n\n" if cur else "") + block
        if len(candidate) <= max_len:
            cur = candidate
        else:
            if cur:
                chunks.append(cur)
            cur = block
    if cur:
        chunks.append(cur)

    return chunks
