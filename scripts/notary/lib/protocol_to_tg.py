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

import json
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

# --- Ф4а: постоянный дисклеймер авторства ------------------------------------
# Бот выпускает лучшую ДОГАДКУ авторства без вопроса-стопа (A1); чтобы читатель
# не пугался возможной путаницы, в начале КАЖДОГО протокола — постоянное
# приглашение поправить (A2). Единый текст на все каналы (.md / PDF / TG-текст),
# чтобы не разъезжался. Правка реактивна реплаем (механика обучения — Ф4б).
PROTOCOL_DISCLAIMER_SENTINEL = "Авторство реплик"
_DISCLAIMER_TEXT = (
    "Авторство реплик бот определил автоматически и мог перепутать, кто что "
    "сказал. Если заметили ошибку — ответьте на это сообщение с поправкой, "
    "учту на будущее."
)
# .md / PDF — markdown-цитата (blockquote есть в allowlist PDF-санитайзера;
# курсив = «служебная заметка», а не контент протокола).
PROTOCOL_DISCLAIMER_MD = f"> ℹ️ _{_DISCLAIMER_TEXT}_"
# TG-текст (revision-путь) — плоская строка с эмодзи (без markdown-разметки).
PROTOCOL_DISCLAIMER_TG = f"ℹ️ {_DISCLAIMER_TEXT}"


def insert_protocol_disclaimer(md_text: str) -> str:
    """Вставляет постоянный дисклеймер (Ф4а) в начало ТЕЛА протокола.

    Идемпотентно: если дисклеймер уже присутствует — возвращает текст как есть
    (повторная генерация не плодит дубль). Точка вставки — строго ДО первой
    `## `-секции (это и есть инвариант Ф4а), по приоритету:
      1) перед первой секцией `## ` — ПЕРВИЧНЫЙ якорь: гарантирует, что
         дисклеймер не окажется в теле секции независимо от того, куда модель
         поставила `---` (а значит — не утечёт в series_memory-дайджест);
      2) если секций `## ` нет вовсе — сразу после шапки (первый `---`);
      3) иначе — после первой строки (`#протоколвстречи`).

    Позиция «до первой секции» критична для инвариантов Ф4а:
      - series_memory.build_digest собирает только содержимое `## `-секций и
        строку `**Участники:**` → дисклеймер в дайджест НЕ попадает;
      - `_caption_participants` берёт участников из meta, не из тела → не ломается.
    """
    text = md_text or ""
    if PROTOCOL_DISCLAIMER_SENTINEL in text:
        return text
    lines = text.splitlines()
    if not lines:
        return text
    insert_at: Optional[int] = None
    # Первичный якорь — ДО первой `## `-секции (инвариант Ф4а: дисклеймер не
    # должен попасть в тело секции → в series_memory-дайджест). Привязка к
    # СЕКЦИИ, а не к первому `---`: если модель пропустит шапочный `---`, но
    # вставит `---` где-то в теле, привязка к первому `---` уронила бы дисклеймер
    # ВНУТРЬ секции (и в дайджест). Секция-якорь от этого защищён структурно.
    for idx, ln in enumerate(lines):
        if ln.strip().startswith("## "):
            insert_at = idx
            break
    # Секций `## ` нет вовсе → ставим после шапки (первый `---`), иначе после якоря.
    if insert_at is None:
        for idx, ln in enumerate(lines):
            if ln.strip() == "---":
                insert_at = idx + 1
                break
    if insert_at is None:
        insert_at = 1
    block = ["", PROTOCOL_DISCLAIMER_MD, ""]
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    result = "\n".join(new_lines)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def strip_protocol_disclaimer(md_text: str) -> str:
    """Убирает строку(и) дисклеймера Ф4а из текста протокола.

    Нужно потребителям, которым дисклеймер мешает (например, если в будущем
    протокол с дисклеймером пойдёт в LLM как «контент»). Снимает строку-цитату
    `> …Авторство реплик…` и осиротевшие пустые строки вокруг неё.
    """
    text = md_text or ""
    if PROTOCOL_DISCLAIMER_SENTINEL not in text:
        return text
    out: list[str] = []
    for ln in text.splitlines():
        if PROTOCOL_DISCLAIMER_SENTINEL in ln and ln.lstrip().startswith(">"):
            continue
        out.append(ln)
    # схлопываем возможную двойную пустую строку на месте удаления
    cleaned: list[str] = []
    for ln in out:
        if ln.strip() == "" and cleaned and cleaned[-1].strip() == "":
            continue
        cleaned.append(ln)
    result = "\n".join(cleaned)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result

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

# Латинские/короткие метки владельца из Telemost UI («Ilya R.», «Ilya», «Илья Р.»)
# — это всегда Илья Рыбалка. Нормализуем (нижний регистр, без точек) и матчим по
# списку, чтобы шапка протокола не показывала «Илья Рыбалка» и «Ilya R.» как ДВУХ
# разных участников (баг, замеченный владельцем 2026-06-06).
_OWNER_CANONICAL = "Илья Рыбалка"
_OWNER_ALIAS_KEYS = {"илья", "илья р", "ilya", "ilya r", "ilya rybalka"}


def _is_owner_alias(name: str) -> bool:
    key = re.sub(r"[.\s]+", " ", (name or "").strip().lower()).strip()
    return key in _OWNER_ALIAS_KEYS


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
    # Метка владельца («Ilya R.», «Илья Р.» и т.п.) → канон «Илья Рыбалка» ДО
    # проверки на пробел (иначе «Ilya R.» вернётся как есть и не схлопнется с
    # курируемым «Илья Рыбалка» — в шапке появятся ДВА участника-владельца).
    if _is_owner_alias(short_name):
        return _OWNER_CANONICAL
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


# --- Фильтр мусора из скрейпа участников Телемоста (Ф1: A2.1) ------------
#
# Панель «Участники» Телемоста (scrape в vexa-bot `participants.ts`) иногда
# отдаёт не имена, а UI-строки кнопок («Скопировать ссылку», «Пригласить») и
# аватар-монограммы («ДН» = Дарья Набережная, «ИР» = Илья Рыбалка). Node-
# эвристика `looksLikeName` их пропускает (дыра найдена владельцем 2026-06-09:
# «скопировать ссылку» не равно стоп-слову «копировать»; «ДН» — валидная по её
# меркам строка). Чистим на стороне Python — единый chokepoint: чинит и УЖЕ
# собранные meta.json прошлых встреч, и не требует пересборки/редеплоя vexa-bot.
# Принцип: лучше выкинуть сомнительное, чем оставить мусор в шапке протокола.

# UI-фразы Телемоста: матч по нормализованной ПОДСТРОКЕ (нижний регистр).
_UI_PHRASE_SUBSTR = (
    "скопировать", "копировать ссылк", "ссылка на встреч", "ссылку на встреч",
    "пригласить", "ожидан", "демонстрац", "поделиться", "показать вс",
    "ещё участ", "еще участ", "copy link", "invite", "share screen",
    "waiting room", "admit",
)

# UI-слова: кандидат, ВСЕ токены которого служебные, — отбраковываем целиком.
_UI_WORDS = frozenset({
    "участник", "участники", "ждать", "ожидание", "выйти", "поиск", "найти",
    "закрыть", "вы", "хост", "ведущий", "микрофон", "камера", "звук", "видео",
    "чат", "сообщение", "ссылка", "ссылку", "копировать", "пригласить",
    "host", "you", "search", "close", "leave", "mute", "unmute",
    "participant", "participants", "share", "more",
})

# Технические символы, которых в человеческом имени не бывает.
_NONNAME_TECH_RE = re.compile(r"[<>{}|\\/\[\]=@]")


def _is_ui_or_nonname(name: str) -> bool:
    """True — строку НЕ берём в участники (UI-кнопка Телемоста / не-имя, A2.1).

    Отбраковываем:
      - UI-фразы кнопок («Скопировать ссылку», «Пригласить») — по подстроке;
      - строки целиком из служебных слов;
      - технические символы (`<>{}|\\/[]=@`);
      - аватар-монограммы / инициалы: один «токен» из ≤3 голых букв, ВСЕ
        заглавные («ДН», «ИР», «И.Р.») — это инициалы аватара, не имя.
    Имя из ≥2 слов или со строчными буквами («Мария», «Сона», «Ия») проходит.
    """
    s = (name or "").strip()
    if not s:
        return True
    low = re.sub(r"\s+", " ", s).lower()
    for sub in _UI_PHRASE_SUBSTR:
        if sub in low:
            return True
    words = low.split()
    if words and all(w in _UI_WORDS for w in words):
        return True
    if _NONNAME_TECH_RE.search(s):
        return True
    # Монограмма/инициалы: ≤3 голых буквы (точки/дефисы/пробелы срезаны) и ни
    # одной строчной → «ДН», «И.Р.». Реальное имя такой длины («Ия», «Лев»)
    # содержит строчные и проходит.
    letters_only = re.sub(r"[.\s\-]", "", s)
    if 1 <= len(letters_only) <= 3 and letters_only.isalpha() \
            and letters_only == letters_only.upper():
        return True
    return False


def filter_participant_names(names) -> list[str]:
    """Чистит список участников от UI-мусора скрейпа Телемоста (A2.1) + дедуп.

    Принимает любой iterable; не-строки и пустые отбрасывает. Сохраняет порядок,
    точные дубли убирает. НЕ резолвит имена (это `_resolve_participants` /
    `_caption_participants`) — только выкидывает заведомый мусор. Единый
    chokepoint фильтрации сырых имён панели Телемоста.
    """
    out: list[str] = []
    seen: set[str] = set()
    for n in (names or []):
        if not isinstance(n, str):
            continue
        s = n.strip()
        if not s or _is_ui_or_nonname(s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# Строка реплики транскрипта (формат render.py): «**[MM:SS] Имя:** текст» или
# «**[HH:MM:SS] Имя:** текст». Группа 1 — отображаемое имя/метка спикера.
_TRANSCRIPT_VOICE_LINE_RE = re.compile(
    r"^\*\*\[\d{1,2}:\d{2}(?::\d{2})?\]\s+(.+?):\*\*", re.MULTILINE
)
# Метка «Спикер N» / «Спикер ?» — НЕ имя (нераспознанный кластер).
_SPEAKER_PLACEHOLDER_RE = re.compile(r"^Спикер\s", re.IGNORECASE)


def voiced_speaker_names_from_transcript(transcript_md: str) -> list[str]:
    """Ф3 (A5): отображаемые ИМЕНА спикеров, реально звучавшие в транскрипте.

    Парсит строки реплик «**[ts] Имя:**» рендера и собирает уникальные имена,
    у которых есть кластер голоса. «Спикер N»/«Спикер ?» (нераспознанный кластер)
    — НЕ имя, отбрасываем: для правила «нет голоса — нет имени» важны именно
    распознанные авторы. Порядок появления сохраняем, дубли убираем.

    Опасная тройка: берём только МЕТКУ спикера (имя), сам текст реплик не трогаем
    и не логируем.
    """
    if not transcript_md:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _TRANSCRIPT_VOICE_LINE_RE.finditer(transcript_md):
        label = (m.group(1) or "").strip()
        if not label or _SPEAKER_PLACEHOLDER_RE.match(label):
            continue
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out


def _first_word_key(name: str) -> str:
    """Ключ дедупа по первому слову (lower). «Мария Михина» и «Мария» → один ключ."""
    s = re.sub(r"[*_`]{1,2}", "", name or "").strip().lower()
    parts = s.split()
    return parts[0] if parts else s


def resolve_present_participants(
    expected, panel, voiced,
) -> list[str]:
    """Ф3 (A5): «нет голоса — нет имени». Состав встречи = кто реально был.

    Участник = присутствовавший в комнате (`panel` — список Телемоста) ИЛИ реально
    говоривший (`voiced` — имена из кластеров голоса транскрипта). Приглашённый по
    `expected` (watched.yaml), которого НЕТ ни в панели, ни среди голосов
    (отпускник «Еремеев»), в состав НЕ попадает — устраняет «подставил отсутствующего».

    `expected` сюда подаётся ТОЛЬКО для деградационного фолбэка: если ни панели, ни
    голосов нет (нет сигнала присутствия) — возвращаем `expected ∪ panel` (прежнее
    поведение, лучше показать ожидаемых, чем пустой состав). При наличии любого
    сигнала expected-only отбрасывается.

    Дедуп по первому слову, предпочитаем более полное написание («Мария Михина»
    важнее «Мария»). Порядок: сначала панель, затем добавленные голоса.
    """
    panel_clean = filter_participant_names(panel or [])
    voiced_clean = [v.strip() for v in (voiced or []) if isinstance(v, str) and v.strip()]
    expected_clean = [e.strip() for e in (expected or []) if isinstance(e, str) and e.strip()]

    has_signal = bool(panel_clean) or bool(voiced_clean)
    if not has_signal:
        # Деградация: сигнала присутствия нет → прежнее поведение (expected ∪ panel).
        merged_src = expected_clean + panel_clean
    else:
        # A5: только присутствовавшие (панель) ∪ реально говорившие (голоса).
        merged_src = panel_clean + voiced_clean

    # Дедуп по первому слову с выбором самого полного написания.
    best_by_key: dict[str, str] = {}
    order: list[str] = []
    for name in merged_src:
        key = _first_word_key(name)
        if not key:
            continue
        if key not in best_by_key:
            best_by_key[key] = name
            order.append(key)
        else:
            # Предпочитаем более длинное (более полное) написание имени.
            if len(name) > len(best_by_key[key]):
                best_by_key[key] = name
    return [best_by_key[k] for k in order]


def _resolve_participants(meta: dict) -> list[str]:
    """Собирает список «Имя Фамилия» для шапки TG.

    Берём `meta.participants` (Telemost UI) + `meta.expectedParticipants`
    (watched.yaml). Резолвим имена-без-фамилии через people.md. Сохраняем
    порядок, дедуп — по полному имени.
    """
    # A2.1: чистим сырьё панели Телемоста от UI-мусора («Скопировать ссылку»,
    # монограмм «ДН») ДО резолва — иначе мусор попадёт в шапку TG.
    raw_participants = filter_participant_names(meta.get("participants") or [])
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


def speech_bounds_ms_from_raw_json(raw_json) -> Optional[tuple[int, int]]:
    """Границы реальной речи по сырому ответу Speechmatics (json-v2).

    Возвращает `(firstSpeechMs, lastSpeechMs)` — min(start_time) и max(end_time)
    по элементам `results` с `type == "word"` (пунктуацию игнорируем), в
    миллисекундах. `None` — если `raw_json` битый/пустой или нет ни одного слова.

    Защита (РАЗМ1, риск «firstSpeechMs из results»): пропускаем нечисловые
    тайминги; `end_time < start_time` нормализуем к `start_time` (не доверяем
    отрицательной длительности слова).
    """
    if not isinstance(raw_json, dict):
        return None
    results = raw_json.get("results")
    if not isinstance(results, list) or not results:
        return None

    first_s: Optional[float] = None
    last_s: Optional[float] = None
    for r in results:
        if not isinstance(r, dict) or r.get("type") != "word":
            continue
        try:
            st = float(r.get("start_time"))
            en = float(r.get("end_time"))
        except (TypeError, ValueError):
            continue
        if en < st:
            en = st
        if first_s is None or st < first_s:
            first_s = st
        if last_s is None or en > last_s:
            last_s = en

    if first_s is None or last_s is None:
        return None
    return (int(round(first_s * 1000)), int(round(last_s * 1000)))


def clean_speech_ms_from_raw_json(raw_json) -> Optional[int]:
    """Чистое время речи (мс) = `lastSpeechMs - firstSpeechMs` по `raw_json`.

    `None` — если границы не вычислились (битый/пустой `results`, нет слов) или
    интервал не положительный. Никогда не бросает — защита от мусорного STT.
    """
    bounds = speech_bounds_ms_from_raw_json(raw_json)
    if bounds is None:
        return None
    first_ms, last_ms = bounds
    if last_ms <= first_ms:
        return None
    return last_ms - first_ms


def _clean_speech_ms_from_transcript_json(path) -> Optional[int]:
    """Читает архив `_transcripts/<date>.json` и считает чистое время речи (мс).

    Архив (см. finalize-meeting.py): `{..., "raw_json": {"results": [...]}}`.
    Допускаем и «голый» `raw_json` (с `results` на верхнем уровне) — на случай
    если кто-то передаст путь к самому ответу Speechmatics.

    Любая ошибка (нет файла, битый json, нет слов) → `None`, без исключения.
    Сам warning о fallback логирует вызывающий `compute_duration_label`.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    rj = data.get("raw_json")
    if not isinstance(rj, dict):
        # Возможно, передали сам raw_json (results на верхнем уровне).
        rj = data if isinstance(data.get("results"), list) else None
    if rj is None:
        return None
    return clean_speech_ms_from_raw_json(rj)


def compute_duration_label(
    meta: dict, transcript_json_path=None, *, presence_fallback: bool = True
) -> str:
    """Возвращает «X ч Y мин» / «Y мин» / «<1 мин».

    Приоритет источников (НЕС1 + Ф1-доработки 2026-06-04):
      1. `meta.recording.chunks[]` (после Ф5) — sum (lastSpeechMs - firstSpeechMs).
      2. `meta.recording.firstSpeechMs/lastSpeechMs` — single-chunk. Эти поля
         пишет finalize-meeting.py по словам транскрипта (REQ 2.2) — для новых
         встреч это основной путь «чистого времени».
      3. **Транскрипт-json** (REQ 2.3) — если `transcript_json_path` передан явно
         (путь к `_transcripts/<date>.json`), считаем чистое время прямо по
         словам. Нужен для СТАРЫХ встреч без `recording.*` в meta.
      4. Fallback — `endTs - startTs` (присутствие бота, «грязное» время).

    РАЗМ1: путь к транскрипту передаётся ЯВНЫМ аргументом, не угадывается с
    диска (функция остаётся чистой относительно meta). `audio_duration_s` из
    архива как длительность встречи НЕ используется (это длина аудио, не речь).

    `presence_fallback=False` (FU-12, нормализация тела протокола): если чистое
    время недоступно из источников 1–3, вернуть «—» вместо «грязного» присутствия
    (4) / durationLabel (5). Нужно телу протокола, чтобы оно не уехало на wall-time,
    пока подпись для старой встречи берёт чистое время из transcript-json.

    Если источники 1–2 пусты И `transcript_json_path` передан, но файла нет /
    он битый / нет слов — логируем `warning` и только потом падаем на (4):
    «грязное» время не должно проходить незаметно (критерий Ф1).
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

    # (3) транскрипт-json (между single-chunk и присутствием) — REQ 2.3.
    if transcript_json_path is not None:
        clean_ms = _clean_speech_ms_from_transcript_json(transcript_json_path)
        if clean_ms is not None and clean_ms > 0:
            return _format_ms(clean_ms)
        # Путь передан, но непригоден — НЕ молчим, иначе «грязное» присутствие
        # уедет в подпись незаметно (критерий Ф1).
        logger.warning(
            "[protocol_to_tg] clean-time: transcript json missing, "
            "fallback to presence (path=%s)",
            transcript_json_path,
        )

    # FU-12 (цикл5): для тела протокола чистое время либо есть (источники 1–3),
    # либо его нет. Присутствие (4) / durationLabel (5) — это НЕ чистое время;
    # пускать его в тело нельзя, иначе тело разойдётся с подписью, которая для
    # старых встреч берёт чистое время из transcript-json. presence_fallback=False
    # → честное «—», и вызывающий (`_normalize_protocol_duration`) оставит тело.
    if not presence_fallback:
        return "—"

    # (4) fallback endTs - startTs
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

    # A1/F1: заголовок — человекочитаемое имя серии (как в PDF-шапке), а не
    # generic «ПРОТОКОЛ ВСТРЕЧИ». Серия резолвится из `series-display.json` /
    # хардкод-оверрайдов / темы; fallback на generic, если имя не вычислилось.
    series_name = resolve_series_display_name(meta, parsed_header=parsed_header)
    title = series_name if series_name and series_name != "—" else "ПРОТОКОЛ ВСТРЕЧИ"
    lines = [
        f"📋 {title} — {date_str}",
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
    # Ф4а: постоянный дисклеймер авторства сразу после шапки. _parse_protocol_md
    # отбрасывает текст до первой секции, поэтому в TG-тексте дисклеймер из .md
    # не виден — добавляем его явно (единый текст, дубля не будет).
    blocks = [header, PROTOCOL_DISCLAIMER_TG]
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


# --- PDF caption (Ф2 доработок protocol-pdf-telegram) --------------------
#
# Протокол уходит в Telegram PDF-вложением (`protocol_to_pdf` + sendDocument);
# к нему — короткая подпись из 4 строк (REQ 3.1, эталон владельца 2026-06-04):
#
#     📋 #протоколвстречи
#     <Серия> — DD.MM.YYYY
#     Участники: <имена>
#     Чистое время обсуждения: ~<Xч YYмин>
#
# Тело протокола в чат текстом НЕ дублируется (REQ 3.2) — только PDF + caption.


# Человекочитаемые имена серий для шапки caption (РАЗМ2).
#
# Проверено на реальных встречах 2026-06-04: `meta.series` — это SLUG папки
# серии (`marketplaces-tatiana`, `anzhee-direktorat`, `oneoff-…-e57601`), он же
# ключ привязки chat_id в watched.yaml. Человекочитаемого поля в watched.yaml
# НЕТ. Поэтому slug → отображаемое имя резолвим маппингом. Дефолты ниже —
# эталон владельца; расширяется без правки кода через `_config/series-display.json`.
# Ф1 (A1): реестр регулярных встреч владельца 2026-06-09 (источник правды —
# план notary-memory-knowledge-rework). Хардкод-дефолты держим в синхроне с
# `_config/series-display.json`, чтобы человеческое имя серии работало на VPS
# ДАЖЕ если json-конфиг ещё не выкачен (деплой кода ≠ деплой данных). Конфиг
# остаётся слоем override поверх этих дефолтов.
_SERIES_DISPLAY_OVERRIDES = {
    "mpervyi-pn-koord-finplan": "Закупки и финпланирование (МПервый)",
    "series-ezhenedelnaya-koordinaciya-8399ea": "Еженедельная координация",
    "marketplaces-tatiana": "1-на-1 с Татьяной Филипповой",
    "anzhee-direktorat": "Директорат",
}

# Разделитель «короткое имя — расшифровка» в теме протокола (em/en-dash/дефис
# с пробелами). Для tier-3 резолва имени серии из `**Встреча:**`.
_THEME_SEP_RE = re.compile(r"\s+[—–-]\s+")

# Хвост-суффикс slug'а: `-<hex≥4>` (как `-e57601`/`-8399ea`) или `-YYYY-MM-DD`.
_SLUG_HASH_SUFFIX_RE = re.compile(r"-(?:[0-9a-f]{4,}|\d{4}-\d{2}-\d{2})$")


def _series_display_config_paths() -> list[str]:
    """Пути к опциональному JSON-конфигу `slug → display`.

    Env `MEETING_NOTARY_SERIES_DISPLAY_PATH` → `<registry>/_config/...` →
    дефолт в `~/Projects/me/встречи/_config/...`. Файла обычно нет — это ок
    (вернётся пустой маппинг, работают хардкод-дефолты).
    """
    paths: list[str] = []
    env = os.environ.get("MEETING_NOTARY_SERIES_DISPLAY_PATH")
    if env:
        paths.append(env)
    reg = os.environ.get("MEETING_NOTARY_REGISTRY_DIR")
    if reg:
        paths.append(os.path.join(reg, "_config", "series-display.json"))
    paths.append(os.path.expanduser("~/Projects/me/встречи/_config/series-display.json"))
    return paths


def _load_series_display_config() -> dict:
    """Best-effort загрузка JSON-маппинга `slug → имя`. Stdlib `json`, без yaml.

    Нет файла → `{}` (тихо, это норма). Битый JSON / не-dict → `{}` + warning
    (чтобы повреждённый конфиг не глушился незаметно).
    """
    for p in _series_display_config_paths():
        if not p:
            continue
        try:
            raw = Path(p).read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("[protocol_to_tg] series-display config битый JSON: %s", p)
            return {}
        if not isinstance(data, dict):
            logger.warning("[protocol_to_tg] series-display config не объект: %s", p)
            return {}
        # Только строковые пары slug→имя.
        return {k: v for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()}
    return {}


def _short_from_theme(theme: str) -> Optional[str]:
    """Короткое имя серии из темы `**Встреча:** Имя — расшифровка` (до « — »).

    Возвращает None, если темы нет или «голова» подозрительно длинная (это уже
    не имя серии, а целое предложение — лучше fallback на slug)."""
    theme = (theme or "").strip()
    if not theme:
        return None
    head = _THEME_SEP_RE.split(theme, maxsplit=1)[0].strip()
    if head and len(head) <= 48:
        return head
    return None


def _humanize_slug(series: str) -> str:
    """Последний резерв: `marketplaces-tatiana` → «Marketplaces tatiana».

    Срезает хвост-хэш/дату, меняет `-`/`_` на пробел, капитализирует первую
    букву. Качество для транслит-slug'ов слабое — поэтому caller логирует
    warning, чтобы владелец добавил запись в маппинг."""
    s = _SLUG_HASH_SUFFIX_RE.sub("", (series or "").strip())
    s = re.sub(r"[-_]+", " ", s).strip()
    if not s:
        s = (series or "").strip()
    return (s[:1].upper() + s[1:]) if s else (series or "—")


def resolve_series_display_name(
    meeting_meta: dict,
    *,
    protocol_text: Optional[str] = None,
    parsed_header: Optional[dict] = None,
) -> str:
    """Человекочитаемое имя серии для 1-й части caption (РАЗМ2).

    Приоритет:
      1. Явное поле meta (`seriesTitle`/`series_display`) — future-proof.
      2. Маппинг slug → имя: `_config/series-display.json` перебивает
         хардкод-дефолты `_SERIES_DISPLAY_OVERRIDES`.
      3. Тема `**Встреча:** Имя — …` из шапки протокола (до « — »).
      4. Гуманизированный slug + warning (видно в логе → владелец добавит маппинг).
    """
    meta = meeting_meta or {}
    for key in ("seriesTitle", "series_display", "seriesDisplayName"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    series = (meta.get("series") or "").strip()
    if series:
        overrides = dict(_SERIES_DISPLAY_OVERRIDES)
        overrides.update(_load_series_display_config())
        if series in overrides:
            return overrides[series]

    if parsed_header is None and protocol_text is not None:
        parsed_header = _parse_protocol_md(protocol_text).get("header") or {}
    theme = (parsed_header or {}).get("theme") or ""
    short = _short_from_theme(theme)
    if short:
        return short

    if series:
        human = _humanize_slug(series)
        logger.warning(
            "[protocol_to_tg] series %r нет в display-маппинге — caption берёт %r "
            "(добавь в _config/series-display.json или _SERIES_DISPLAY_OVERRIDES)",
            series, human,
        )
        return human
    return "—"


def _caption_date_from_parsed(meta: dict, parsed_header: dict) -> str:
    """DD.MM.YYYY: из шапки протокола, иначе из `meta.date`/`startTs`."""
    d = (parsed_header or {}).get("date") or ""
    if d:
        return d
    ds = (meta or {}).get("date") or ((meta or {}).get("startTs") or "")[:10]
    if ds and re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
        yyyy, mm, dd = ds.split("-")
        return f"{dd}.{mm}.{yyyy}"
    return ds or "—"


def _clean_time_caption_line(meta: dict, transcript_json_path) -> str:
    """Строка «Чистое время обсуждения: ~…» (Ф1 источник чистого времени).

    Если время неизвестно (`—`) — без тильды, чтобы не было «~—»."""
    duration = compute_duration_label(meta, transcript_json_path=transcript_json_path)
    if duration and duration != "—":
        return f"Чистое время обсуждения: ~{duration}"
    return "Чистое время обсуждения: —"


def _caption_participants(meta: dict) -> list[str]:
    """Участники для caption/шапки PDF — эталон владельца «Илья Рыбалка, Татьяна».

    Отличие от `_resolve_participants` (заголовок TG-текста): имена из
    `expectedParticipants` (курируются в watched.yaml) берём КАК ЕСТЬ и НЕ
    обогащаем через people.md. Причина: people.md-обогащение по первому имени
    подставляет не того человека — marketplaces «Татьяна» ≠ «Татьяна Филиппова»
    (управляющая МПервого, единственная «Татьяна» в people.md). Курируемый
    список — источник истины.

    FU-10 (🔴, боевая 02.06): дедуп по first-name применяем ТОЛЬКО к обогащению
    bare-имён из `participants` (Telemost UI). Курируемые `expectedParticipants`
    берём ВСЕ как есть, без коллапса по first-name — иначе двое «Михаил»
    (Еремеев + Саргин) на координации схлопывались в одного, второй молча
    выпадал из подписи. Точные дубликаты (одна и та же полная строка) убираем.

    Имена из `participants` (Telemost UI, часто «голое» имя) обогащаем через
    people.md, как раньше — это исходное назначение правки #4 (bare «Михаил»
    → «Михаил Еремеев»). first-name, уже занятый курируемым именем, UI не
    перетирает (страховка от того же ложного обогащения).
    """
    raw_expected = [n.strip() for n in (meta.get("expectedParticipants") or [])
                    if isinstance(n, str) and n.strip()]
    # A2.1: панель Телемоста чистим от UI-мусора/монограмм ДО обогащения.
    raw_ui = filter_participant_names(meta.get("participants") or [])
    people_md = _read_people_md()
    people_names = _extract_names_from_people(people_md) if people_md else []

    def _first(name: str) -> str:
        parts = name.split()
        return parts[0] if parts else name

    result: list[str] = []
    seen_full: set = set()
    # first-name'ы курируемого состава — UI-обогащение не должно добавлять
    # ещё одного «Михаила» поверх курируемых (но МЕЖДУ собой курируемые
    # тёзки НЕ схлопываем — FU-10).
    locked: set = set()

    # Курируемые expected — ВСЕ как есть, в исходном порядке (только точный
    # дубль-строку отбрасываем). Тёзки сохраняются оба.
    for name in raw_expected:
        locked.add(_first(name))
        if name not in seen_full:
            seen_full.add(name)
            result.append(name)

    # Telemost UI — обогащаем через people.md; курируемые first-name не
    # трогаем. Среди самих UI-имён одинаковый first-name схлопываем в самый
    # полный вариант (исходная защита от дублей bare-имени).
    ui_best_by_first: dict[str, str] = {}
    ui_order: list[str] = []
    for raw in raw_ui:
        full = _resolve_full_name(raw, people_names)
        if not full or not full.strip():
            continue
        full = full.strip()
        f = _first(full)
        if f in locked:
            continue
        if f not in ui_best_by_first:
            ui_best_by_first[f] = full
            ui_order.append(f)
        elif len(full.split()) > len(ui_best_by_first[f].split()):
            ui_best_by_first[f] = full
    for f in ui_order:
        full = ui_best_by_first[f]
        if full in seen_full:
            continue
        seen_full.add(full)
        result.append(full)

    return result


def build_pdf_caption(
    protocol_text: str,
    meeting_meta: dict,
    *,
    transcript_json_path=None,
) -> str:
    """4-строчная подпись под PDF-вложение протокола (REQ 3.1).

    `transcript_json_path` (для СТАРЫХ встреч без `recording.*`) передаётся
    дальше в `compute_duration_label` (FORWARD-зависимость Ф1 / FU-2): путь к
    `series_dir/_transcripts/<date>.json`, иначе подпись тихо уедет на присутствие.
    Передавать только когда архив реально есть.
    """
    meta = meeting_meta or {}
    parsed_header = _parse_protocol_md(protocol_text or "").get("header") or {}
    date_str = _caption_date_from_parsed(meta, parsed_header)
    series_name = resolve_series_display_name(meta, parsed_header=parsed_header)
    resolved = _caption_participants(meta)
    participants_str = ", ".join(resolved) if resolved else "—"
    lines = [
        f"📋 {HASHTAG}",
        f"{series_name} — {date_str}",
        f"Участники: {participants_str}",
        _clean_time_caption_line(meta, transcript_json_path),
    ]
    return "\n".join(lines)


def build_pdf_title_subtitle(
    protocol_text: str,
    meeting_meta: dict,
    *,
    transcript_json_path=None,
) -> tuple[str, str]:
    """(title, subtitle) для шапки самого PDF (рисует `protocol_to_pdf`).

    title    = «<Серия> — DD.MM.YYYY»
    subtitle = «Участники: … · Чистое время обсуждения: ~…»
    """
    meta = meeting_meta or {}
    parsed_header = _parse_protocol_md(protocol_text or "").get("header") or {}
    date_str = _caption_date_from_parsed(meta, parsed_header)
    series_name = resolve_series_display_name(meta, parsed_header=parsed_header)
    resolved = _caption_participants(meta)
    participants_str = ", ".join(resolved) if resolved else "—"
    duration = compute_duration_label(meta, transcript_json_path=transcript_json_path)
    title = f"{series_name} — {date_str}"
    if duration and duration != "—":
        subtitle = f"Участники: {participants_str}  ·  Чистое время обсуждения: ~{duration}"
    else:
        subtitle = f"Участники: {participants_str}"
    return title, subtitle
