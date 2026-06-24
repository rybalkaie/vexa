"""Ф7: память серии встреч — компактные выжимки прошлых встреч серии.

На регулярных и повторных встречах бот опирается на выжимки прошлых встреч
той же серии: узнаёт постоянных участников, держит сквозные темы/термины,
видит динамику чисел. Протокол при этом строится в продолжающемся контексте,
НО факты по-прежнему берутся строго из текущей записи (см. `format_memory_block`
и ревью-секцию "memory" в `llm_postprocess`).

Состав модуля:
  - 7.1  `build_digest`        — детерминированная выжимка из готового протокола
                                 (участники, темы, ключевые пункты/числа/решения);
         `save_digest`/`load_digest`/`list_series_digests`/`prune_old_digests`
                                 — хранилище рядом с протоколом (`<date>-memory.json`).
  - 7.2  `resolve_memory`      — резолвер серии: (а) прошлые выжимки той же серии
                                 (стабильный slug из календаря/watched.yaml), (б)
                                 fallback по совпадению состава участников
                                 (нерегулярные 1-на-1).
  - 7-связка `permanent_participants` — постоянный состав из памяти серии для
                                 обогащения `expected_participants` (усиливает 5.4/6.1).
  - 7.3/7.4 `format_memory_block` — справочный блок для промта генерации с явной
                                 дисциплиной «прошлое = справка, не факт».
  - 7.5  `backfill_series`/`backfill_root` — разовый сбор выжимок из уже лежащих
                                 протоколов (детерминированно, без claude).

РИСК4 / «опасная тройка» (решение владельца 2026-05-26). Память серии — новое
хранилище производных от ПДн данных. Дисциплина зашита в код:
  • выжимка = ТОЛЬКО производные поля (участники, темы, ключевые пункты), которые
    уже прошли сжатие в протоколе. НИКОГДА не сырой транскрипт и не сырые реплики;
  • текст выжимки НЕ логируется (логируем только счётчики/серию/дату);
  • срок хранения ограничен (`SERIES_MEMORY_RETENTION_DAYS`, прунинг старых файлов);
  • генерация выжимки детерминированна (без claude) — в Claude уходит только уже
    собранный справочный блок на генерации протокола (тот же путь, что транскрипт).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("meeting_notary.series_memory")

# Версия схемы выжимки — на случай миграции формата.
SCHEMA_VERSION = 1

# Имя файла выжимки рядом с протоколом `<date>-protokol.md`.
MEMORY_FILE_SUFFIX = "-memory.json"
PROTOCOL_FILE_SUFFIX = "-protokol.md"

# Дефолты (переопределяются env, см. ниже).
DEFAULT_DEPTH = 3
DEFAULT_RETENTION_DAYS = 180

# Лимиты состава выжимки (РИСК4: компактность + предсказуемый размер хранилища).
_MAX_PARTICIPANTS = 12
_MAX_THEMES = 14
_MAX_KEY_POINTS = 12
_MAX_POINT_LEN = 200

# Маркеры протокола (см. методичку kak-delat-protokol-vstrechi.md и реальный
# пример `встречи/.../<date>-protokol.md`): темы `## N) Тема`, буллеты
# ▪️/▫️ (тематические), 🔸 (решения), 🟠 (срочные задачи).
_HEADING_RE = re.compile(r"^\s{0,3}#{2,4}\s+(.*\S)\s*$")
# Срезаем нумерацию «1) », «2. » в начале заголовка темы.
_THEME_NUM_PREFIX_RE = re.compile(r"^\d+[.)]\s*")
# Любой буллет-маркер в начале строки (функциональные эмодзи + markdown-дефис).
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|▪️|▫️|🔸|🟠|🔹|◾|◽)\s*")
# ⚠️-пометка ревью (5.2/6.2) — срезаем из выжимки, это не контент встречи.
_REVIEW_FLAG_RE = re.compile(r"\s*⚠️.*$")
_HAS_DIGIT_RE = re.compile(r"\d")

# Ф8 (трекинг открытых задач). Потолок числа открытых задач, оседающих в выжимке
# серии (storage cap, РАЗМ/приватность): не даём хвосту незакрытых разрастаться без
# предела. Подача в промпт капится отдельно env-лимитом `open_tasks_max()` (≤ этого).
_MAX_OPEN_TASKS = 25
# Дефолт подачи в промпт = storage cap (НЕ меньше!). Инвариант G9: всё, что осело
# в выжимке, ОБЯЗАНО попасть в промпт — иначе задача на позиции 16..25 хранится, но
# в «🔻 С прошлых встреч» не выводится → на следующей (кумулятивной) выжимке её уже
# нет → незакрытая задача молча исчезает (ровно то, что G9 предотвращает). Снизить
# подачу можно env'ом OPEN_TASKS_MAX, но это осознанный выбор «показывать меньше».
DEFAULT_OPEN_TASKS_MAX = _MAX_OPEN_TASKS
# Подзаголовок исполнителя внутри блока «Задачи»: `**Имя Фамилия**` (методичка §4).
_OWNER_SUBHEADING_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*$")
# Статус перенесённой задачи в блоке «🔻 С прошлых встреч»: модель пишет
# `- <задача> — <статус>` (см. `format_open_tasks_block`). «— закрыта» (с опц. ✅) в
# хвосте → задачу дальше НЕ несём. Дефис ОБЯЗАТЕЛЕН (а не просто слово «закрыт» в
# конце): иначе висящая задача с формулировкой вида «…проверить, всё ли закрыто»
# ложно схлопнулась бы в «закрыта». Консерватизм правила вопроса 8: ложно-закрыть
# хуже, чем лишний раз показать висящей — поэтому требуем явный разделитель статуса.
_OPEN_TASK_CLOSED_RE = re.compile(
    r"[—–-]\s*(?:✅\s*)?закрыт\w*\s*(?:✅)?[\s.!…)]*$", re.IGNORECASE
)
# Суффикс статуса «— висит» / «— висит, статус?» (с опц. 🔻) — срезаем при переносе,
# чтобы текст задачи не копил статусы из встречи в встречу. Хвостовая пунктуация/
# эмодзи («— висит.», «— закрыта ✅.») терпимы (`[\s.!…)]*$` в обоих регексах):
# модель недетерминирована и дрейфует от точного формата — иначе суффикс не срезался
# бы и статус накапливался в тексте задачи из встречи в встречу.
_OPEN_TASK_STATUS_SUFFIX_RE = re.compile(
    r"\s*[—–-]\s*(?:🔻\s*)?висит(?:,?\s*статус\??)?[\s.!…)]*$", re.IGNORECASE
)

# Ф2 (pending-items): sidecar-статусы висяка. Файл `task-status.json` рядом с
# `<date>-memory.json` в каталоге серии — ОДИН на серию (статус — свойство хвоста
# серии, не отдельной встречи). Финализация его НЕ пишет (build_digest/save_digest
# трогают только `<date>-memory.json`) → статусы переживают регенерацию (ядро Ф2,
# снятие РИСК1). Пишут его reply-канал (Ф5) и сверщик (Ф6); читает рендер хвоста.
TASK_STATUS_FILE = "task-status.json"
TASK_STATUS_SCHEMA = 1

# Набор статусов висяка (план Ф2). Внутренние коды; человекочитаемые ярлыки причин
# («по встрече»/«по чату»/…) живут в поле reason, рендер — Ф3.
STATUS_OPEN = "open"            # висит (дефолт — записи в sidecar может и не быть)
STATUS_DONE = "done"           # закрыто-сделано
STATUS_CANCELLED = "cancelled"  # отменён (снят без выполнения — норма, R5)
STATUS_DOUBT = "doubt"         # под сомнением (буфер, R10/R16 — не закрываем молча)
STATUS_AUTO_CLOSED = "auto_closed"  # закрыто-авто (сверщик/кросс-серийно, R20)
_VALID_STATUSES = frozenset(
    {STATUS_OPEN, STATUS_DONE, STATUS_CANCELLED, STATUS_DOUBT, STATUS_AUTO_CLOSED}
)
# Терминально закрытые: НЕ показываются как «висит» (ядро Ф2 — не воскресают).
# «под сомнением» — НЕ терминальный (буфер): показывается отдельным подблоком (Ф3).
_TERMINAL_CLOSED = frozenset({STATUS_DONE, STATUS_CANCELLED, STATUS_AUTO_CLOSED})

# Служебные заголовки протокола — НЕ темы встречи.
_DECISION_HEADING_KEYS = ("решен", "что внедряем", "договорил")
_TASK_HEADING_KEYS = ("задач",)
_SERVICE_HEADING_KEYS = ("провер", "что изменилось", "🔁", "приложен")
# Ф8: блок трекинга открытых задач серии «🔻 С прошлых встреч». Своя секция, НЕ
# тема и НЕ блок задач текущей встречи: её содержимое — перенесённые задачи со
# статусом (закрыта/висит), их разбирает `extract_open_tasks`, а не темы/key_points.
_CARRYOVER_HEADING_KEYS = ("с прошлых встреч", "🔻")

# Владелец присутствует на каждой встрече — при матчинге серии по составу
# участников его исключаем (иначе любая встреча «похожа» на любую). Можно
# переопределить через env `SERIES_MEMORY_OWNER_NAMES` (через запятую).
_DEFAULT_OWNER_TOKENS = ("илья", "рыбалка", "ilya", "rybalka")

# Порог совпадения состава для fallback-матчинга (Jaccard по не-владельцам).
_PARTICIPANT_MATCH_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# env-конфиг
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    """Kill-switch `ENABLE_SERIES_MEMORY` (дефолт ON; `0/false/no` → OFF).

    Нужен, чтобы при сбое в проде отключить чтение/запись памяти без передеплоя.
    """
    raw = (os.environ.get("ENABLE_SERIES_MEMORY") or "").strip().lower()
    return raw not in ("0", "false", "no")


def has_series_slug(series: Optional[str]) -> bool:
    """True, если у встречи есть осмысленный slug серии (не пусто/пробелы).

    Память серии работает ТОЛЬКО при наличии slug. Встреча без серии (legacy-путь
    `_output_dir_for_meta` → `<date>-<sid>.md` в КОРНЕ протоколов) папки-серии не
    имеет: её `series_dir` схлопывается в общий корень. Без этого гейта несвязанные
    series-less встречи делили бы один пул памяти (ложная «та же серия») — поэтому
    резолв/сохранение памяти на стороне finalize/clarify гейтятся этим предикатом.
    """
    return bool((series or "").strip())


def series_memory_depth() -> int:
    """Глубина контекста `SERIES_MEMORY_DEPTH` (число прошлых выжимок, дефолт 3).

    Невалидное/<=0 → дефолт. Жёсткий потолок 20 — защита от случайного раздутия
    промта генерации.
    """
    raw = (os.environ.get("SERIES_MEMORY_DEPTH") or "").strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DEPTH
    if val <= 0:
        return DEFAULT_DEPTH
    return min(val, 20)


def retention_days() -> int:
    """Срок хранения выжимок `SERIES_MEMORY_RETENTION_DAYS` (дни, дефолт 180).

    `0` / отрицательное → 0 = «не прунить» (хранение без ограничения по времени).
    """
    raw = (os.environ.get("SERIES_MEMORY_RETENTION_DAYS") or "").strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return max(val, 0)


def is_open_tasks_enabled() -> bool:
    """Ф8: kill-switch `ENABLE_OPEN_TASKS_TRACKING` (дефолт ON; `0/false/no` → OFF).

    Отдельный от `ENABLE_SERIES_MEMORY`: при сбое в проде можно выключить ТОЛЬКО
    подачу хвоста открытых задач в промпт (раздел «🔻 С прошлых встреч»), не трогая
    резолв памяти серии (диаризация/постоянный состав/справка). Выключение гасит
    лишь построение блока на call-site; сами выжимки `open_tasks` хранить продолжаем.
    """
    raw = (os.environ.get("ENABLE_OPEN_TASKS_TRACKING") or "").strip().lower()
    return raw not in ("0", "false", "no")


def open_tasks_max() -> int:
    """Ф8: лимит числа открытых задач, подаваемых в промпт `OPEN_TASKS_MAX`.

    Дефолт = `DEFAULT_OPEN_TASKS_MAX` (= storage cap `_MAX_OPEN_TASKS`): по умолчанию
    в промпт едет ВСЁ, что хранится, иначе хвост 16..N молча терялся бы (см. коммент
    у `DEFAULT_OPEN_TASKS_MAX`). Невалидное/<=0 → дефолт. Потолок = `_MAX_OPEN_TASKS`:
    просить в промпт больше, чем хранилище держит, бессмысленно (хранилище режет первым).
    """
    raw = (os.environ.get("OPEN_TASKS_MAX") or "").strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_OPEN_TASKS_MAX
    if val <= 0:
        return DEFAULT_OPEN_TASKS_MAX
    return min(val, _MAX_OPEN_TASKS)


def _owner_tokens() -> tuple[str, ...]:
    raw = (os.environ.get("SERIES_MEMORY_OWNER_NAMES") or "").strip()
    if not raw:
        return _DEFAULT_OWNER_TOKENS
    toks = tuple(t.strip().lower() for t in raw.split(",") if t.strip())
    return toks or _DEFAULT_OWNER_TOKENS


# ---------------------------------------------------------------------------
# 7.1 — построение выжимки (детерминированно, без claude)
# ---------------------------------------------------------------------------
def _clean_bullet_text(line: str) -> str:
    """Текст буллета без маркера, ⚠️-пометки ревью и лишних пробелов, с обрезкой."""
    text = _BULLET_PREFIX_RE.sub("", line)
    text = _REVIEW_FLAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Срезаем markdown-эмфазу — она для отображения, в выжимке шумит при матчинге.
    text = re.sub(r"[*_`]{1,2}", "", text)
    if len(text) > _MAX_POINT_LEN:
        text = text[: _MAX_POINT_LEN - 1].rstrip() + "…"
    return text


# Строка markdown-эмфазы в начале (`*курсив*`, `**жирный**`) — это НЕ буллет списка.
# Маркер-звёздочка списка всегда идёт с пробелом («* пункт»); «*текст*»/«**жирный**»
# (звезда сразу к не-пробелу) — эмфаза, которую `_BULLET_PREFIX_RE` ошибочно ловил как
# буллет. Из-за этого курсивный футер протокола («*Протокол восстановлен из транскрипта…*»)
# утекал в `extract_open_tasks` отдельной ложной задачей. Guard отсекает эмфазу, не трогая
# настоящие буллеты `- `/`▪️`/`🟠`/`* пункт`.
_EMPHASIS_LINE_RE = re.compile(r"^\s*\*{1,2}\S")


def _is_bullet(line: str) -> bool:
    if not _BULLET_PREFIX_RE.match(line):
        return False
    return not _EMPHASIS_LINE_RE.match(line)


def _classify_heading(title: str) -> str:
    """Классифицирует заголовок: carryover | theme | decisions | tasks | service."""
    low = title.lower()
    # Ф8: «🔻 С прошлых встреч» — самый специфичный, проверяем первым (иначе при
    # будущем переименовании секции мог бы случайно уйти в theme и засорить выжимку).
    if any(k in low for k in _CARRYOVER_HEADING_KEYS):
        return "carryover"
    if any(k in low for k in _SERVICE_HEADING_KEYS):
        return "service"
    if any(k in low for k in _DECISION_HEADING_KEYS):
        return "decisions"
    if any(k in low for k in _TASK_HEADING_KEYS):
        return "tasks"
    return "theme"


def _extract_participants_from_header(protocol_text: str) -> list[str]:
    """Парсит строку `**Участники:** Имя, Имя2` из шапки протокола."""
    m = re.search(r"^\s*\*\*Участники:\*\*\s*(.+)$", protocol_text, flags=re.MULTILINE)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw or raw == "—":
        return []
    out: list[str] = []
    for part in raw.split(","):
        nm = re.sub(r"[*_`]{1,2}", "", part).strip()
        if nm and nm not in out:
            out.append(nm)
    return out


def _norm_participants(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names or []:
        if not isinstance(n, str):
            continue
        nm = n.strip()
        if nm and nm not in out:
            out.append(nm)
    return out[:_MAX_PARTICIPANTS]


def extract_protocol_sections(protocol_text: str) -> tuple[list[str], list[str]]:
    """Возвращает (themes, key_points) из markdown-протокола.

    themes      — заголовки тематических блоков `## N) Тема` (без служебных).
    key_points  — решения (🔸 под «## Решения…») + тематические буллеты с числами
                  (для динамики чисел). Каждый — очищен от маркера/⚠️/эмфазы.

    Чистая функция (без IO) — основной объект unit-тестов 7.1.
    """
    themes: list[str] = []
    decisions: list[str] = []
    number_points: list[str] = []
    section = None  # theme|decisions|tasks|service|None (до первого заголовка)
    for raw_line in protocol_text.splitlines():
        hm = _HEADING_RE.match(raw_line)
        if hm:
            title = _THEME_NUM_PREFIX_RE.sub("", hm.group(1).strip()).strip()
            title = re.sub(r"[*_`]{1,2}", "", title).strip()
            section = _classify_heading(title)
            if section == "theme" and title and title not in themes:
                themes.append(title)
            continue
        if not _is_bullet(raw_line):
            continue
        if section == "decisions":
            txt = _clean_bullet_text(raw_line)
            if txt and txt not in decisions:
                decisions.append(txt)
        elif section == "theme":
            txt = _clean_bullet_text(raw_line)
            if txt and _HAS_DIGIT_RE.search(txt) and txt not in number_points:
                number_points.append(txt)
    # Решения приоритетнее — заполняем сначала ими, остаток добиваем числами.
    key_points: list[str] = []
    for p in decisions:
        if len(key_points) >= _MAX_KEY_POINTS:
            break
        key_points.append(p)
    for p in number_points:
        if len(key_points) >= _MAX_KEY_POINTS:
            break
        if p not in key_points:
            key_points.append(p)
    return themes[:_MAX_THEMES], key_points


def _clean_task_text(line: str) -> str:
    """Текст задачи без ведущих маркеров (-, 🟠, 🔻, ▪️…), эмфазы, ⚠️ и лишних
    пробелов, с обрезкой. Маркеры срезаем В ЦИКЛЕ: строка задачи бывает «- 🟠 …»
    (дефис + функциональный эмодзи), один проход `^`-якорного regex снял бы только
    дефис.
    """
    s = line
    prev = None
    while prev != s:
        prev = s
        s = _BULLET_PREFIX_RE.sub("", s).lstrip()
    s = _REVIEW_FLAG_RE.sub("", s)
    s = re.sub(r"[*_`]{1,2}", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > _MAX_POINT_LEN:
        s = s[: _MAX_POINT_LEN - 1].rstrip() + "…"
    return s


def _task_key(text: str) -> str:
    """Ключ дедупликации задачи: lower, без эмфазы, схлопнутые пробелы."""
    s = re.sub(r"[*_`]{1,2}", "", text or "")
    return re.sub(r"\s+", " ", s).strip().lower()


# Ф2 (pending-items): хвостовой «(срок: …)» табличной задачи (Ф1). Срезаем его при
# построении КЛЮЧА СТАТУСА (см. `_status_key`), чтобы переформулировка срока между
# встречами («пятница» → «к 15.06») не сменила ключ и не потеряла проставленный
# статус. Только финальный скобочный срок в конце строки, не середина текста.
_DUE_SUFFIX_RE = re.compile(r"\s*\(\s*срок\s*:[^)]*\)\s*$", re.IGNORECASE)


def _status_key(text: str) -> str:
    """Ф2 (pending-items): ключ задачи в sidecar статусов.

    Это `_task_key` БЕЗ хвостового «(срок: …)». Свой ключ (а не общий `_task_key`)
    специально: общий ключ держит дедуп в `resolve_open_tasks`/`extract_open_tasks`/
    `extract_decisions` — менять его рискованно. Срок-суффикс модель переформулирует
    между встречами, и статус, проставленный по старой формулировке, потерялся бы.
    Срезаем срок → статус матчится по СУТИ задачи (две формулировки срока одной
    задачи дают один ключ — это та же задача с обновлённым дедлайном, схлопывание
    верное). Консерватизм (R16): не совпал ключ → задача покажется висящей (ложно-
    висит < ложно-закрыто), а не молча закрытой.
    """
    return _task_key(_DUE_SUFFIX_RE.sub("", text or ""))


def _open_task_text(item) -> str:
    """Ф2 (pending-items): текст задачи из элемента `open_tasks` — строка ИЛИ объект.

    Совместимость/миграция (Ф1 §5): `open_tasks` сейчас `list[str]` и таким остаётся
    (статусы — в SIDECAR, не здесь). Но если запись окажется объектом `{'text': …}`
    (будущая схема / ручная правка файла), элемент НЕ должен выпасть из фильтра
    `isinstance(t, str)` в `resolve_open_tasks` — извлекаем текст. Старые `list[str]`
    проходят как есть (миграция без потерь). Не-строка/не-объект → "" (пропуск).
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        t = item.get("text") or item.get("task") or ""
        return t.strip() if isinstance(t, str) else ""
    return ""


# Ф1 (ISS-23): на 1-на-1 встречах модель часто рендерит блок задач markdown-ТАБЛИЦЕЙ
# `| Кому | Что | Срок |` («формат Татьяны» — референс методички), а не списком `- `
# под `**Имя**`. Без разбора таблицы хвост таких встреч пуст (R1: «на 1:1 digest почти
# пуст, боту нечего поднимать»). Эти ключи распознают колонки шапки таблицы задач.
_TABLE_OWNER_KEYS = ("кому", "ответствен", "кто", "owner", "исполнит")
_TABLE_TASK_KEYS = ("что", "задач", "действ", "task", "action")
_TABLE_DUE_KEYS = ("срок", "когда", "дедлайн", "дата", "due", "deadline")


def _is_table_row(line: str) -> bool:
    """Строка markdown-таблицы — начинается с `|` (после необязательных пробелов)."""
    return line.lstrip().startswith("|")


def _split_table_row(line: str) -> list[str]:
    """`| a | b | c |` → ['a','b','c'] (внешние пайпы срезаны, ячейки strip'нуты)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_separator(cells: list[str]) -> bool:
    """Строка-разделитель шапки таблицы: все непустые ячейки — только из `-`/`:`."""
    nonempty = [c for c in cells if c.strip()]
    return bool(nonempty) and all(set(c.strip()) <= set("-:") for c in nonempty)


def _table_header_cols(cells: list[str]) -> Optional[dict]:
    """Если строка таблицы — ШАПКА (есть и колонка исполнителя, и колонка задачи),
    вернуть {'owner': i, 'task': j, 'due': k|None}; иначе None (строка данных).

    Требуем найти ОБЕ ключевые колонки (кому+что) в одной строке — иначе строка
    данных, случайно содержащая слово «что», не будет принята за шапку.
    """
    low = [c.lower() for c in cells]
    owner = task = due = None
    for i, c in enumerate(low):
        if owner is None and any(k in c for k in _TABLE_OWNER_KEYS):
            owner = i
        if task is None and any(k in c for k in _TABLE_TASK_KEYS):
            task = i
        if due is None and any(k in c for k in _TABLE_DUE_KEYS):
            due = i
    if owner is not None and task is not None:
        return {"owner": owner, "task": task, "due": due}
    return None


def _clean_table_cell(cell: str) -> str:
    """Текст ячейки таблицы без эмфазы/⚠️-пометки ревью, схлопнутые пробелы, обрезка."""
    s = _REVIEW_FLAG_RE.sub("", cell or "")
    s = re.sub(r"[*_`]{1,2}", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > _MAX_POINT_LEN:
        s = s[: _MAX_POINT_LEN - 1].rstrip() + "…"
    return s


def _task_from_table_cells(cells: list[str], cols: Optional[dict]) -> str:
    """Строит «Имя: задача (срок: X)» из строки-ДАННЫХ таблицы задач.

    `cols` — индексы колонок из шапки; без шапки дефолт Кому|Что|Срок = 0|1|2.
    Срок добавляем только осмысленный (не «—»/пусто). Имя-префикс — как в bullet-пути
    (нужно «за кем следующий шаг»), но не дублируем, если имя уже в тексте задачи.
    Пустая задача → "" (строка пропускается). НЕ кладёт сырые реплики (РИСК4).
    """
    if not cells:
        return ""
    if cols is None:
        owner_i, task_i, due_i = 0, 1, 2
    else:
        owner_i, task_i, due_i = cols.get("owner", 0), cols.get("task", 1), cols.get("due")

    def _cell(i: Optional[int]) -> str:
        if i is None or i < 0 or i >= len(cells):
            return ""
        return _clean_table_cell(cells[i])

    owner = _cell(owner_i)
    task = _cell(task_i)
    if not task:
        # Кривая/одноколоночная таблица: первая непустая ячейка = задача, без owner.
        for c in cells:
            cc = _clean_table_cell(c)
            if cc:
                task = cc
                break
        owner = ""
    if not task:
        return ""
    due = _cell(due_i)
    if due and set(due) - set(" .—–-"):  # есть содержимое помимо тире/точек/пробелов
        task = f"{task} (срок: {due})"
    if owner and owner.lower() not in task.lower():
        task = f"{owner}: {task}"
    return task


def extract_open_tasks(protocol_text: str) -> list[str]:
    """Ф8: открытые (незакрытые) задачи серии ИЗ ГОТОВОГО протокола.

    Два источника в протоколе:
      • блок «Задачи» (section==tasks, методичка §4): задачи, назначенные на ЭТОЙ
        встрече — НОВЫЕ открытые. Канонически сгруппированы под подзаголовком `**Имя**`
        списком `- ` — исполнителя кладём префиксом «Имя: …» (за кем следующий шаг).
        Ф1: на 1-на-1 встречах модель часто рендерит этот блок markdown-ТАБЛИЦЕЙ
        `| Кому | Что | Срок |` — её тоже разбираем (иначе хвост 1:1 пуст, R1/ISS-23).
      • блок «🔻 С прошлых встреч» (section==carryover, Ф8): ПЕРЕНЕСЁННЫЕ задачи со
        статусом. Помеченные «закрыта» — ОТБРАСЫВАЕМ (закрыты на этой встрече);
        «висит»/«висит, статус?» — несём дальше, срезая суффикс статуса. Консерватизм
        (правило вопроса 8): дальше несём всё, что не помечено явно закрытым.

    Возвращает дедуплицированный список «перенесённые-висящие → новые», capped
    `_MAX_OPEN_TASKS`. Порядок (висящие раньше новых) держит «хвост» серии при капе.
    Чистая функция (без IO, без claude) — основной объект unit-тестов Ф8. НЕ кладёт
    сырые реплики (РИСК4): только формулировки задач из уже сжатого протокола.
    """
    carried: list[str] = []
    fresh: list[str] = []
    section = None  # carryover|tasks|theme|decisions|service|None
    current_owner: Optional[str] = None
    # Ф1: разбор таблицы задач. Шапку определяем ПО ПОЗИЦИИ — это строка
    # НЕПОСРЕДСТВЕННО перед разделителем `|---|`. Это надёжнее распознавания по
    # ключевым словам: (а) не зависит от формулировки шапки (синонимы «Участник/
    # Поручение/Задание» вне keyword-списков иначе утекали бы мусорной задачей);
    # (б) строку-ДАННЫХ, где в тексте есть и owner-слово («кто»), и task-слово
    # («что»), по ключам ложно приняли бы за шапку → потеря задачи + порча колонок.
    # Поэтому строку таблицы ПРИДЕРЖИВАЕМ на один шаг (`table_pending`): следующая
    # строка-разделитель ⇒ придержанная была шапкой (роли колонок берём из неё, в
    # задачи НЕ выводим); иначе придержанная — строка-данных.
    table_cols: Optional[dict] = None
    table_pending: Optional[list[str]] = None

    def _flush_pending_task() -> None:
        """Зафиксировать придержанную строку таблицы как задачу (она оказалась
        данными, не шапкой: за ней не последовал разделитель)."""
        nonlocal table_pending
        if table_pending is None:
            return
        cells = table_pending
        table_pending = None
        txt = _task_from_table_cells(cells, table_cols)
        if txt:
            fresh.append(txt)

    for raw_line in (protocol_text or "").splitlines():
        hm = _HEADING_RE.match(raw_line)
        if hm:
            _flush_pending_task()  # незакрытая строка-данных предыдущей таблицы
            title = _THEME_NUM_PREFIX_RE.sub("", hm.group(1).strip()).strip()
            title = re.sub(r"[*_`]{1,2}", "", title).strip()
            section = _classify_heading(title)
            current_owner = None
            table_cols = None
            continue
        if section == "tasks":
            om = _OWNER_SUBHEADING_RE.match(raw_line)
            if om:
                _flush_pending_task()
                current_owner = re.sub(r"\s+", " ", om.group(1)).strip()
                continue
            if _is_table_row(raw_line):  # Ф1: задачи markdown-таблицей (формат 1:1)
                cells = _split_table_row(raw_line)
                if _is_table_separator(cells):
                    # Разделитель: придержанная строка перед ним — ШАПКА. Роли
                    # колонок берём из неё (нет ключей → None → дефолт Кому|Что|Срок
                    # при рендере). Саму шапку в задачи НЕ выводим.
                    if table_pending is not None:
                        table_cols = _table_header_cols(table_pending)
                        table_pending = None
                    continue
                _flush_pending_task()   # предыдущая придержанная была данными
                table_pending = cells   # текущую придержим — вдруг за ней разделитель
                continue
            # не табличная строка — закрыть незавершённую таблицу
            _flush_pending_task()
            if _is_bullet(raw_line):
                txt = _clean_task_text(raw_line)
                if not txt:
                    continue
                if current_owner and current_owner.lower() not in txt.lower():
                    txt = f"{current_owner}: {txt}"
                fresh.append(txt)
        elif section == "carryover":
            # Ф3 (R7): раздел висяков сгруппирован по ответственному подзаголовком
            # `**Имя**` (как блок «Задачи»). Трекаем владельца, чтобы перенос сохранял
            # «кто следующий шаг» (Ф1), когда имя вынесено в подзаголовок, а не в строку.
            om = _OWNER_SUBHEADING_RE.match(raw_line)
            if om:
                current_owner = re.sub(r"\s+", " ", om.group(1)).strip()
                continue
            if not raw_line.strip():
                continue  # пустая строка-разделитель групп — владельца НЕ сбрасывает
            if not _is_bullet(raw_line):
                # Не-буллет не-пустая строка = маркер подраздела Ф3 («🟡 …»/«✅ …»/
                # курсивный ярлык). Сбрасываем владельца: задачи подразделов несут
                # «Имя:» в самой строке (sidecar-ключ по полному тексту) — чужой
                # подзаголовок к ним клеить нельзя.
                current_owner = None
                continue
            txt = _clean_task_text(raw_line)
            if not txt:
                continue
            if _OPEN_TASK_CLOSED_RE.search(txt):
                continue  # закрыта на этой встрече — дальше серия её не несёт
            txt = _OPEN_TASK_STATUS_SUFFIX_RE.sub("", txt).strip()
            if txt:
                # Имя из подзаголовка восстанавливаем (как в блоке «Задачи»), если в
                # строке его ещё нет — round-trip префикса исполнителя при группировке.
                if current_owner and current_owner.lower() not in txt.lower():
                    txt = f"{current_owner}: {txt}"
                carried.append(txt)
    _flush_pending_task()  # хвост: последняя строка-данных таблицы в конце протокола
    out: list[str] = []
    seen: set[str] = set()
    for t in carried + fresh:
        k = _task_key(t)
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out[:_MAX_OPEN_TASKS]


def extract_decisions(protocol_text: str) -> list[str]:
    """Ф2 (инвариант «не теряем»): решения/договорённости ИЗ ГОТОВОГО протокола.

    Буллеты под секцией решений — заголовок классифицируется ТОЙ ЖЕ
    `_classify_heading` (через `_DECISION_HEADING_KEYS`: «Решения» / «Что внедряем»
    / «Договорились»), что и `extract_open_tasks`/`extract_protocol_sections`. Это
    НЕ отдельный парсер (РИСК3): переиспользует классификатор заголовков и
    `_clean_bullet_text`, поэтому не разойдётся с вариантами написания секции и
    не несёт статус-логики (у решений её нет). Чистая функция (без IO, без claude).

    Возвращает дедуплицированный список формулировок решений, capped
    `_MAX_KEY_POINTS`. НЕ кладёт сырые реплики — только сжатые пункты протокола.
    """
    out: list[str] = []
    seen: set[str] = set()
    section = None  # carryover|tasks|theme|decisions|service|None
    for raw_line in (protocol_text or "").splitlines():
        hm = _HEADING_RE.match(raw_line)
        if hm:
            title = _THEME_NUM_PREFIX_RE.sub("", hm.group(1).strip()).strip()
            title = re.sub(r"[*_`]{1,2}", "", title).strip()
            section = _classify_heading(title)
            continue
        if section == "decisions" and _is_bullet(raw_line):
            txt = _clean_bullet_text(raw_line)
            k = _task_key(txt)
            if txt and k not in seen:
                seen.add(k)
                out.append(txt)
    return out[:_MAX_KEY_POINTS]


def extract_items_from_versions(texts: list[str]) -> list[str]:
    """Ф2/Ф3 (инвариант «финал ⊇ источников»): открытые задачи + решения из ОДНОГО
    или НЕСКОЛЬКИХ готовых протоколов (прошлая версия и/или параллельный черновик
    той же встречи), дедуплицированные МЕЖДУ источниками.

    Обобщённый вход (`texts` — список): Ф2 подаёт одну прошлую версию, Ф3 (best-of-2)
    переиспользует ТУ ЖЕ функцию, подавая второй независимый черновик (с прошлой
    версией или без). Объединяет `extract_open_tasks` (несёт статус-логику Ф8/G9 —
    carryover со статусом «закрыта» отфильтрован, поэтому закрытое транскриптом
    НЕ воскрешается: A6/R-b5) + `extract_decisions`. БЕЗ LLM (без egress).

    Дедуп по `_task_key` держит идемпотентность повторного регена (один и тот же
    пункт из двух источников не задваивается). Битый/пустой текст → пропуск
    (мягкая деградация R-b4: пустой список, а не падение). Чистая функция.
    """
    items: list[str] = []
    seen: set[str] = set()
    for text in texts or []:
        if not isinstance(text, str) or not text.strip():
            continue
        for it in extract_open_tasks(text) + extract_decisions(text):
            k = _task_key(it)
            if k and k not in seen:
                seen.add(k)
                items.append(it)
    return items


def _norm_speaker_mapping(speaker_mapping: Optional[dict]) -> dict[str, str]:
    """Ф4б: нормализует cluster→name маппинг для хранения в выжимке.

    Пустые ключи/значения отбрасываем; всё к строкам. Возвращает {} если нечего
    хранить (тогда build_digest не кладёт ключ — поле остаётся ленивым, УПУ3).
    """
    out: dict[str, str] = {}
    if not isinstance(speaker_mapping, dict):
        return out
    for cluster, name in speaker_mapping.items():
        if cluster is None or name is None:
            continue
        c = str(cluster).strip()
        n = str(name).strip()
        if c and n:
            out[c] = n
    return out


def build_digest(
    protocol_text: str,
    meeting_meta: dict,
    *,
    date: Optional[str] = None,
    speaker_mapping: Optional[dict] = None,
    participant_filter: Optional[Callable[[list], list]] = None,
    publication: Optional[dict] = None,
) -> dict:
    """7.1: компактная выжимка-память из готового протокола.

    participants: ПРИОРИТЕТ — шапка протокола «**Участники:**» (её модель пишет по
    факту транскрипта = реально присутствовавшие); fallback на meta
    (participants ∪ expectedParticipants). Так в памяти оседают фактические
    участники, а не приглашённые-но-отсутствовавшие — важно для матчинга серии
    по составу (7.2) и постоянного состава (7-связка). Работает и для бэкфилла,
    где meta нет. themes/key_points — из протокола. НЕ кладём сырые реплики (РИСК4).

    Ф4б (REQ 1.2): `speaker_mapping` — резолвленное на этой встрече сопоставление
    cluster→имя (после правок авторства). Кладём ленивым ключом ТОЛЬКО если непусто
    (нет ключа в старых файлах → resolve_speaker_anchor вернёт {}, миграции/бэкфилл
    не нужны, УПУ3). НЕ ПДн сверх уже хранимого: имена и так есть в `participants`.

    Ф6 (E1–E5): `publication` — вердикт гейта публикации знания (`publication_gate`):
    PII-free `{allowed, visibility, company, reason}`, без сырья. Ленивый ключ:
    кладём ТОЛЬКО если передан (бэкфилл/старые файлы его не несут → Ф7-публикатор
    трактует отсутствие как private, fail-closed). Это НЕ публикует знание (Ф7) —
    лишь фиксирует решение рядом с памятью серии, достижимое из реальной финализации.

    Ф2 (B2 / Ф1 §5): `participant_filter` — отбраковка UI-мусора скрейпа Телемоста
    («ДН»/монограммы/«Скопировать ссылку») и не-имён из РАЗ имён. Нужен бэкфиллу:
    он читает шапку СТАРОГО протокола, куда до Ф1-фильтра мог осесть мусор — без
    фильтра он попал бы в память серии. Применяется ДО нормализации/капа. Инъекция
    зависимости (обычно `protocol_to_tg.filter_participant_names`) держит модуль
    stdlib-only. None → прежнее поведение (фильтр не применяется). Best-effort:
    сбой фильтра не валит выжимку.

    Возвращает dict со схемой v1. `date` — YYYY-MM-DD (из аргумента или meta).
    """
    meta = meeting_meta or {}
    dt = (date or meta.get("date") or (meta.get("startTs") or "")[:10] or "").strip()
    series = (meta.get("series") or "").strip()

    raw_participants = _extract_participants_from_header(protocol_text)
    if not raw_participants:
        expected = meta.get("expectedParticipants") or meta.get("expected_participants") or []
        panel = meta.get("participants") or []
        raw_participants = list(panel) + list(expected)
    if participant_filter is not None:
        try:
            raw_participants = list(participant_filter(raw_participants))
        except Exception:  # noqa: BLE001 — фильтр best-effort, не валим выжимку
            pass
    participants = _norm_participants(raw_participants)

    themes, key_points = extract_protocol_sections(protocol_text or "")
    digest: dict = {
        "schema": SCHEMA_VERSION,
        "date": dt,
        "series": series,
        "participants": participants,
        "themes": themes,
        "key_points": key_points,
    }
    # Ф8 (G9): открытые задачи серии (новые + перенесённые-висящие) — лазивый ключ,
    # кладём ТОЛЬКО если непусто (как speaker_mapping/publication; старые выжимки без
    # ключа → resolve_open_tasks вернёт [], миграции/бэкфилл не нужны). Эта выжимка
    # несёт КУМУЛЯТИВНОЕ состояние хвоста: на следующей встрече серии resolve_open_tasks
    # берёт его из самой свежей выжимки и подаёт в промпт генерации (Вызов 1). РИСК4:
    # только формулировки задач из уже сжатого протокола, не сырые реплики.
    open_tasks = extract_open_tasks(protocol_text or "")
    if open_tasks:
        digest["open_tasks"] = open_tasks
    sm = _norm_speaker_mapping(speaker_mapping)
    if sm:
        digest["speaker_mapping"] = sm
    if isinstance(publication, dict) and publication:
        digest["publication"] = publication
    return digest


# ---------------------------------------------------------------------------
# хранилище
# ---------------------------------------------------------------------------
def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic write через tempfile + fsync + os.replace (как vstrechi_index)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def digest_path(series_dir: Path, date: str) -> Path:
    """Путь файла выжимки рядом с протоколом: `<series_dir>/<date>-memory.json`."""
    return Path(series_dir) / f"{date}{MEMORY_FILE_SUFFIX}"


def save_digest(series_dir: Path, date: str, digest: dict) -> Optional[Path]:
    """Сохраняет выжимку рядом с протоколом (atomic). Возвращает путь или None.

    НЕ логирует содержимое (РИСК4) — только серию/дату/счётчики. Best-effort: на
    сбой IO → None. Прунинг по сроку хранения — ОТДЕЛЬНОЙ операцией
    (`prune_old_digests`), её зовёт finalize по текущей серии; так бэкфилл старых
    протоколов не самоудаляет результат на лету.
    """
    if not date:
        return None
    path = digest_path(series_dir, date)
    try:
        text = json.dumps(digest, ensure_ascii=False, indent=2)
        _atomic_write_text(path, text)
    except (OSError, TypeError, ValueError) as e:
        logger.warning("[series-memory] save failed series=%s date=%s: %s",
                       digest.get("series") if isinstance(digest, dict) else "?", date, e)
        return None
    logger.info("[series-memory] saved digest series=%s date=%s participants=%d themes=%d points=%d open_tasks=%d",
                digest.get("series") or "?", date,
                len(digest.get("participants") or []),
                len(digest.get("themes") or []),
                len(digest.get("key_points") or []),
                len(digest.get("open_tasks") or []))
    return path


def save_meeting_digest(
    series_dir: Path,
    date: str,
    protocol_text: str,
    meeting_meta: dict,
    *,
    speaker_mapping: Optional[dict] = None,
    participant_filter: Optional[Callable[[list], list]] = None,
    publication: Optional[dict] = None,
    prune_days: int = 0,
) -> Optional[Path]:
    """B1 (finalize 4.0.2d): построить выжимку из ГОТОВОГО протокола и сохранить
    рядом, затем (опц.) прунинг старых по сроку хранения.

    Тонкая оркестровка `build_digest`→`save_digest`→`prune_old_digests`: hot-path
    финализации зовёт её ОДНИМ вызовом (вместо инлайна), и она же — тестируемая
    единица для критерия B1 «после финализации в папке серии появляется
    `<date>-memory.json`». Пустой протокол → None (нечего сохранять); прунинг при
    этом НЕ выполняется. Best-effort: сбой save → None, finalize не валим.

    `prune_days` — ОТДЕЛЬНОЙ операцией после save (а не внутри `save_digest`),
    иначе бэкфилл старых протоколов самоудалял бы свежий результат (см. `save_digest`).
    """
    if not protocol_text or not protocol_text.strip():
        return None
    digest = build_digest(
        protocol_text, meeting_meta, date=date,
        speaker_mapping=speaker_mapping, participant_filter=participant_filter,
        publication=publication,
    )
    saved = save_digest(series_dir, date, digest)
    if prune_days and prune_days > 0:
        prune_old_digests(series_dir, prune_days)
    return saved


def load_digest(path: Path) -> Optional[dict]:
    """Читает один файл выжимки. На сбой/мусор → None."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _date_from_memory_filename(name: str) -> Optional[str]:
    """`2026-05-27-memory.json` → `2026-05-27`. Иначе None."""
    if not name.endswith(MEMORY_FILE_SUFFIX):
        return None
    stem = name[: -len(MEMORY_FILE_SUFFIX)]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", stem):
        return stem
    return None


def list_series_digests(
    series_dir: Path,
    *,
    exclude_date: Optional[str] = None,
) -> list[dict]:
    """Все выжимки серии из `series_dir`, отсортированы по дате ВОЗРАСТАНИЮ.

    `exclude_date` — пропустить выжимку текущей встречи (чтобы не подмешивать
    её саму в собственный контекст). Битые/нечитаемые файлы пропускаем молча.
    """
    d = Path(series_dir)
    if not d.is_dir():
        return []
    items: list[tuple[str, dict]] = []
    for entry in d.iterdir():
        if not entry.is_file():
            continue
        date = _date_from_memory_filename(entry.name)
        if not date:
            continue
        if exclude_date and date == exclude_date:
            continue
        dig = load_digest(entry)
        if dig is None:
            continue
        dig.setdefault("date", date)
        items.append((date, dig))
    items.sort(key=lambda x: x[0])
    return [dig for _, dig in items]


def prune_old_digests(series_dir: Path, days: int, *, today: Optional[str] = None) -> int:
    """Удаляет выжимки старше `days` дней. `days<=0` → ничего не делаем.

    `today` (YYYY-MM-DD) — для детерминизма в тестах; в проде вычисляется сам.
    Возвращает число удалённых файлов. Дату сравниваем строкой (ISO сортируем
    лексикографически) — порог = today − days.
    """
    if days <= 0:
        return 0
    d = Path(series_dir)
    if not d.is_dir():
        return 0
    if today is None:
        from datetime import date as _date  # локальный импорт: модуль stdlib-only
        today = _date.today().isoformat()
    try:
        from datetime import date as _date, timedelta as _td
        y, m, dd = (int(x) for x in today.split("-"))
        cutoff = (_date(y, m, dd) - _td(days=days)).isoformat()
    except (ValueError, TypeError):
        return 0
    removed = 0
    for entry in d.iterdir():
        if not entry.is_file():
            continue
        date = _date_from_memory_filename(entry.name)
        if not date:
            continue
        if date < cutoff:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("[series-memory] pruned %d digest(s) older than %s in %s",
                    removed, cutoff, d.name)
    return removed


def prune_root(root: Path, days: int, *, today: Optional[str] = None) -> dict:
    """РИСК4: прунит выжимки старше `days` по ВСЕМ сериям под `root`.

    `prune_old_digests` на финализации чистит только АКТИВНУЮ серию — dormant-
    серия (встреч больше нет) свои старые выжимки иначе не теряет НИКОГДА, и срок
    хранения ПДн-производных по такой серии нарушается. Этот проход — для разовой
    / периодической операции обслуживания (утренний батч владельца): соблюсти
    retention по всему архиву. Служебные `_*`/`.`-папки пропускаем. `days<=0` →
    no-op (хранение без ограничения по времени). `today` (YYYY-MM-DD) — для тестов.

    Возвращает {"series": тронуто_серий, "digests": удалено_выжимок}.
    """
    r = Path(root)
    if days <= 0 or not r.is_dir():
        return {"series": 0, "digests": 0}
    series_touched = 0
    digests_removed = 0
    for entry in sorted(r.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        n = prune_old_digests(entry, days, today=today)
        if n:
            series_touched += 1
            digests_removed += n
    logger.info("[series-memory] prune_root: series=%d digests=%d (>%dd)",
                series_touched, digests_removed, days)
    return {"series": series_touched, "digests": digests_removed}


# ---------------------------------------------------------------------------
# 7.2 — резолвер серии (память той же серии + fallback по составу участников)
# ---------------------------------------------------------------------------
def _participant_norm(name: str) -> str:
    """Нормализует имя для матчинга: lower, схлопнутые пробелы, без эмфазы."""
    s = re.sub(r"[*_`]{1,2}", "", name or "")
    return re.sub(r"\s+", " ", s).strip().lower()


def _non_owner_keys(names: list[str]) -> set[str]:
    """Множество не-владельцев (по first-word нормализованного имени).

    Матчим по первому слову («Саргин Иванов» ↔ «Саргин») — состав на разных
    встречах может записываться полным/коротким именем.
    """
    owners = _owner_tokens()
    keys: set[str] = set()
    for n in names or []:
        norm = _participant_norm(n)
        if not norm:
            continue
        first = norm.split()[0] if norm.split() else norm
        if any(tok in norm for tok in owners):
            continue
        keys.add(first)
    return keys


def _participant_overlap(a: list[str], b: list[str]) -> float:
    """Jaccard по не-владельцам. 0.0 если у текущей встречи нет не-владельцев."""
    ka = _non_owner_keys(a)
    kb = _non_owner_keys(b)
    if not ka or not kb:
        return 0.0
    inter = ka & kb
    union = ka | kb
    return len(inter) / len(union) if union else 0.0


def _match_by_participants(
    root: Path,
    *,
    current_series_dir: Path,
    current_participants: list[str],
    current_date: Optional[str],
    depth: int,
) -> list[dict]:
    """Fallback 7.2(б): ищет выжимки встреч с похожим составом по всем сериям.

    Сканирует папки-серии под `root`, берёт их выжимки, оставляет те, чей состав
    (по не-владельцам) совпадает с текущим выше порога. Собирает все совпавшие,
    сортирует по дате, отдаёт последние `depth`. Текущую папку серии и `_*`
    служебные папки пропускаем.
    """
    r = Path(root)
    if not r.is_dir():
        return []
    cur_keys = _non_owner_keys(current_participants)
    if not cur_keys:
        return []
    try:
        cur_resolved = current_series_dir.resolve()
    except OSError:
        cur_resolved = current_series_dir
    matched: list[tuple[str, dict]] = []
    for entry in r.iterdir():
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        try:
            if entry.resolve() == cur_resolved:
                continue
        except OSError:
            pass
        for dig in list_series_digests(entry, exclude_date=current_date):
            score = _participant_overlap(current_participants, dig.get("participants") or [])
            if score >= _PARTICIPANT_MATCH_THRESHOLD:
                matched.append((dig.get("date") or "", dig))
    matched.sort(key=lambda x: x[0])
    if depth > 0:
        matched = matched[-depth:]
    return [dig for _, dig in matched]


def resolve_memory(
    series_dir: Path,
    root: Path,
    *,
    current_participants: Optional[list[str]] = None,
    current_date: Optional[str] = None,
    depth: Optional[int] = None,
) -> list[dict]:
    """7.2: выжимки последних встреч серии для контекста генерации.

    Стратегия:
      (а) ПРЯМАЯ — прошлые выжимки той же серии (та же папка). Серия стабильна
          по slug из календаря/watched.yaml → регулярная встреча (Татьяна Ср 12:00)
          матчится сразу.
      (б) FALLBACK — если прямых нет (новая/нерегулярная серия, напр. 1-на-1 с
          Саргиным в отдельной папке-одиночке) → ищем по совпадению состава
          участников среди других серий.

    Возвращает список выжимок по дате ВОЗРАСТАНИЮ (старые → новые), не больше
    `depth`. Текущую дату исключаем. Best-effort: на любой сбой → [].
    """
    if depth is None:
        depth = series_memory_depth()
    try:
        same = list_series_digests(series_dir, exclude_date=current_date)
    except Exception as e:  # noqa: BLE001
        logger.warning("[series-memory] list same-series failed (non-fatal): %s", e)
        same = []
    if same:
        return same[-depth:] if depth > 0 else same
    # Прямой памяти нет — пробуем по составу участников.
    try:
        return _match_by_participants(
            root,
            current_series_dir=Path(series_dir),
            current_participants=current_participants or [],
            current_date=current_date,
            depth=depth,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[series-memory] participant-fallback failed (non-fatal): %s", e)
        return []


# ---------------------------------------------------------------------------
# Ф4б — якорь авторства серии (cluster→имя) для диаризации (REQ 1.2)
# ---------------------------------------------------------------------------
def resolve_speaker_anchor(digests: list[dict]) -> dict[str, str]:
    """Ф4б: последнее непустое `speaker_mapping` среди выжимок серии.

    `digests` — как из `resolve_memory` (по дате ВОЗРАСТАНИЮ). Берём самое свежее
    непустое сопоставление cluster→имя (последняя правка авторства бьёт ранние).
    Ленивое поле: нет ключа в старых файлах → {} (бэкфилл/миграция не нужны, УПУ3).

    NB (РИСК-диаризация): метки S1/S2 нестабильны между джобами, поэтому якорь —
    паллиатив; в `map_all` он применяется с валидацией строгим vocative текущей
    встречи (см. `name_mapping._apply_series_anchor`), чтобы не вернуть инверсию.
    """
    anchor: dict[str, str] = {}
    for dig in digests or []:
        sm = dig.get("speaker_mapping") if isinstance(dig, dict) else None
        norm = _norm_speaker_mapping(sm)
        if norm:
            anchor = norm  # ascending → последняя непустая выжимка побеждает
    return anchor


def update_digest_speaker_mapping(
    series_dir: Path, date: str, remap: dict[str, str]
) -> bool:
    """Ф4б (REQ 1.2): применяет name-remap к `speaker_mapping` выжимки встречи.

    Зовётся из перевыпуска (`feedback_reissue`) при правке авторства реплаем —
    чтобы память серии понесла ИСПРАВЛЕННОЕ авторство вперёд. `remap` —
    `{старое_имя_или_метка: новое_имя}` (тот же, что применён к транскрипту);
    применяем к ЗНАЧЕНИЯМ (именам) хранимого cluster→имя. Своп-безопасно:
    каждое значение мапится независимо через `remap.get(v, v)`.

    Ленивое/идемпотентно: нет файла выжимки / нет ключа `speaker_mapping` /
    remap ничего не меняет → no-op, возвращает False. Бэкфилл не нужен.
    """
    if not remap or not date:
        return False
    path = digest_path(series_dir, date)
    dig = load_digest(path)
    if not dig:
        return False
    sm = _norm_speaker_mapping(dig.get("speaker_mapping"))
    if not sm:
        return False
    new_sm = {c: remap.get(n, n) for c, n in sm.items()}
    if new_sm == sm:
        return False
    dig["speaker_mapping"] = new_sm
    saved = save_digest(series_dir, date, dig)
    if saved:
        logger.info("[series-memory] speaker_mapping remapped series=%s date=%s", dig.get("series") or "?", date)
    return bool(saved)


# ---------------------------------------------------------------------------
# 7-связка — постоянный состав из памяти (обогащение expected_participants)
# ---------------------------------------------------------------------------
def permanent_participants(
    digests: list[dict],
    *,
    min_occurrences: int = 2,
) -> list[str]:
    """Имена, встречающиеся в ≥`min_occurrences` выжимках — постоянный состав.

    Усиливает 5.4 (авто-подстановка известного) и 6.1 (подсказка числа спикеров):
    «известный участник» = people.md ∪ постоянный состав серии. Владельца НЕ
    включаем (он и так в expected). Сохраняем наиболее частое написание имени.
    """
    if not digests:
        return []
    owners = _owner_tokens()
    # key (first-word) → {full_name: count}, и общий счётчик по key.
    variants: dict[str, dict[str, int]] = {}
    key_count: dict[str, int] = {}
    for dig in digests:
        seen_keys: set[str] = set()
        for n in dig.get("participants") or []:
            norm = _participant_norm(n)
            if not norm or any(tok in norm for tok in owners):
                continue
            key = norm.split()[0] if norm.split() else norm
            variants.setdefault(key, {})
            variants[key][n.strip()] = variants[key].get(n.strip(), 0) + 1
            if key not in seen_keys:
                key_count[key] = key_count.get(key, 0) + 1
                seen_keys.add(key)
    out: list[str] = []
    for key, cnt in key_count.items():
        if cnt < min_occurrences:
            continue
        # самое частое написание (при равенстве — самое длинное = полнее).
        best = max(variants[key].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        out.append(best)
    return out


# ---------------------------------------------------------------------------
# 7.3/7.4 — справочный блок для промта генерации
# ---------------------------------------------------------------------------
_MEMORY_BLOCK_HEADER = (
    "СПРАВКА — прошлые встречи этой серии (это НЕ источник фактов для протокола!).\n"
    "Ниже краткие выжимки последних встреч серии. Используй их СТРОГО для:\n"
    "- правильного распознавания ИМЁН постоянных участников и устойчивых "
    "ТЕРМИНОВ/проектов;\n"
    "- понимания ДИНАМИКИ чисел (если в текущей записи число другое — это норма, "
    "бери ТЕКУЩЕЕ).\n"
    "ЗАПРЕЩЕНО переносить в протокол темы, решения, задачи или числа, которых НЕТ "
    "в текущем транскрипте. Все факты протокола — только из текущей записи. Если "
    "темы из прошлой встречи в текущей не было — её в протоколе быть НЕ должно."
)


def format_memory_block(digests: list[dict]) -> str:
    """7.3/7.4: справочный блок «прошлые встречи серии» с дисциплиной «справка,
    не факт». Пустой список → "" (блок не добавляется).
    """
    if not digests:
        return ""
    lines = [_MEMORY_BLOCK_HEADER, ""]
    for dig in digests:
        date = dig.get("date") or "—"
        lines.append(f"— Встреча {date}:")
        participants = dig.get("participants") or []
        if participants:
            lines.append(f"  Участники: {', '.join(participants)}")
        themes = dig.get("themes") or []
        if themes:
            lines.append(f"  Темы: {'; '.join(themes)}")
        key_points = dig.get("key_points") or []
        for kp in key_points:
            lines.append(f"  • {kp}")
        lines.append("")
    lines.append("(конец справки — ниже текущая встреча, факты только из неё)")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Ф8 — трекинг открытых задач серии во времени (G9)
# ---------------------------------------------------------------------------
def _resolve_open_tasks_raw(digests: list[dict]) -> list[str]:
    """Хвост открытых задач из САМОЙ СВЕЖЕЙ выжимки, дедуплицированный, БЕЗ капа.

    Выделено из `resolve_open_tasks`, чтобы Ф2-слияние (`build_open_tasks_block`)
    накладывало статусы на ПОЛНЫЙ хвост и капило уже ВИСЯЩИЕ — иначе закрытая задача
    в первых N съела бы слот капа у живого висяка (при сниженном `OPEN_TASKS_MAX`).
    """
    if not digests:
        return []
    latest = digests[-1]
    if not isinstance(latest, dict):
        return []
    raw = latest.get("open_tasks") or []
    out: list[str] = []
    seen: set[str] = set()
    for t in raw:
        # Ф2: `_open_task_text` терпит и строку, и объект `{'text': …}` — расширенная
        # запись НЕ выпадает из фильтра (старый `isinstance(t, str)` молча терял бы её).
        s = _open_task_text(t)
        k = _task_key(s)
        if s and k and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def resolve_open_tasks(digests: list[dict]) -> list[str]:
    """Ф8: открытые задачи серии для подачи в промпт генерации (Вызов 1).

    Берём `open_tasks` из САМОЙ СВЕЖЕЙ выжимки (`digests` по дате ВОЗРАСТАНИЮ → [-1]).
    Каждая выжимка несёт КУМУЛЯТИВНОЕ состояние хвоста: новые задачи той встречи +
    перенесённые-висящие (закрытые на той встрече уже отброшены в `build_digest`).
    Поэтому достаточно последней — она уже учитывает все предыдущие состояния;
    старые выжимки не подмешиваем, иначе воскресили бы давно закрытые задачи.
    Нет выжимок / нет ключа (история до Ф8) / пусто → [] (блок не строится). Capped
    `open_tasks_max()`.
    """
    out = _resolve_open_tasks_raw(digests)
    cap = open_tasks_max()
    return out[:cap] if cap > 0 else out


# Ф3 (pending-items, R3): мягкий заголовок раздела висяков — вариант владельца из
# ISS-23. Подача «помощь, не контроль». ВАЖНО: содержит подстроку «с прошлых встреч»,
# поэтому `_classify_heading` (тут), реордер секций (`llm_postprocess`) и вырезание
# перед дистилляцией (`knowledge_distill.strip_carryover`) узнают секцию как carryover
# по тексту — структурный якорь сохранён без 🔻 (см. их регексы по «с прошлых встреч»).
PENDING_SECTION_HEADING = "Вопросы с прошлых встреч, по которым не ясен статус"

# Ф3 (R7): префикс ответственного в начале задачи («Имя: задача …»). Владельца
# выносим в подзаголовок группы, в строке имя срезаем — extract_open_tasks восстановит
# его из подзаголовка при переносе (round-trip, Ф1 «кто следующий шаг»). Без двоеточия
# / со скобкой в «owner» → задача безымянная (срок «(срок: …)» в конце не считаем).
_PENDING_OWNER_RE = re.compile(r"^\s*([^:()\n]{1,32}?)\s*:\s+(\S.*)$")

# Ф3 (R21/R10): код терминального/сомнительного статуса → человекочитаемый ярлык.
# Причину («по встрече»/«по чату»/«по задаче») кладёт писатель (Ф5/Ф6) в reason —
# тут только дописываем её в скобках. Маппинг — обязанность РЕНДЕРА (план, Ф2 handoff).
_CLOSED_STATUS_LABEL = {
    STATUS_DONE: "сделано",
    STATUS_CANCELLED: "снято",
    STATUS_AUTO_CLOSED: "закрыто автоматически",
}


def _human_closed_label(status: Optional[str], reason: Optional[str]) -> str:
    """Ярлык закрытой задачи: «сделано (по встрече)» / «снято» / «закрыто автоматически»."""
    base = _CLOSED_STATUS_LABEL.get(status or "", "закрыто")
    r = (reason or "").strip()
    return f"{base} ({r})" if r else base


def _group_open_by_owner(tasks: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Ф3 (R7): группировка висящих по ответственному.

    Возвращает `(nameless, owned)`:
      • `nameless` — задачи без префикса «Имя:» (рендерятся БЕЗ подзаголовка, ПЕРВЫМИ —
        чтобы при переносе `extract_open_tasks` не приклеил им чужого владельца);
      • `owned` — `[(owner, [task_без_имени, …]), …]` в порядке первого появления.
    Имя в строке срезаем (оно уходит в подзаголовок **Имя**); перенос восстановит
    «Имя: задача» из подзаголовка. Безымянные несут полный текст.
    """
    nameless: list[str] = []
    order: list[str] = []
    owned: dict[str, list[str]] = {}
    for t in tasks or []:
        m = _PENDING_OWNER_RE.match(t or "")
        if not m:
            s = (t or "").strip()
            if s:
                nameless.append(s)
            continue
        owner = re.sub(r"\s+", " ", m.group(1)).strip()
        rest = m.group(2).strip()
        if owner not in owned:
            owned[owner] = []
            order.append(owner)
        owned[owner].append(rest)
    return nameless, [(o, owned[o]) for o in order]


def _group_closed_by_label(closed: list[dict]) -> list[tuple[str, list[str]]]:
    """Ф3 (R21): закрытые сгруппированы по человекочитаемому ярлыку (статус+причина).

    Ярлык — в маркер-строку, текст задачи в буллете остаётся ДОСЛОВНЫМ (без суффикса):
    иначе при переносе `extract_open_tasks` сдвинул бы `_status_key` и задача
    «воскресла» бы как висящая. Порядок ярлыков — первого появления.
    """
    order: list[str] = []
    groups: dict[str, list[str]] = {}
    for c in closed or []:
        if not isinstance(c, dict):
            continue
        label = _human_closed_label(c.get("status"), c.get("reason"))
        text = (c.get("text") or "").strip()
        if not text:
            continue
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(text)
    return [(lbl, groups[lbl]) for lbl in order]


def _render_pending_subsections(doubt: list[dict], closed: list[dict]) -> str:
    """Ф3 (R10/R21): подразделы «под сомнением» и «закрытые» для копирования ДОСЛОВНО.

    Маркеры — ПЛОСКИЕ строки (НЕ `##`-заголовки и НЕ `**жирный**`): заголовок завёл бы
    лишнюю секцию и сломал бы вырезание дистиллятором; жирная строка спуталась бы с
    подзаголовком ответственного (`_OWNER_SUBHEADING_RE`) и приклеила бы маркер к
    задаче при переносе. Буллеты несут ПОЛНЫЙ текст задачи (с «Имя:») — sidecar-ключ
    по нему; при переносе закрытые исчезнут (shown=True), сомнительные вернутся.
    """
    out: list[str] = []
    if doubt:
        out.append("🟡 Вроде закрыто — подтвердите")
        for d in doubt:
            if not isinstance(d, dict):
                continue
            t = (d.get("text") or "").strip()
            if t:
                out.append(f"- {t}")
        out.append("")
    if closed:
        out.append("✅ Закрыто с прошлых встреч")
        for label, items in _group_closed_by_label(closed):
            out.append(f"_{label}:_")
            for t in items:
                out.append(f"- {t}")
        out.append("")
    return "\n".join(out).rstrip()


_OPEN_TASKS_BLOCK_HEADER = (
    "ВОПРОСЫ С ПРОШЛЫХ ВСТРЕЧ ЭТОЙ СЕРИИ — мягкое напоминание-помощь, НЕ контроль и "
    "НЕ аудит. В протоколе текущей встречи добавь В САМОМ КОНЦЕ (после блока «Задачи») "
    "отдельный раздел с заголовком РОВНО:\n"
    f"## {PENDING_SECTION_HEADING}\n"
    "Тон — дружелюбная сверка, а не спрос: это вопросы, поднятые на прошлых встречах "
    "серии и пока не закрытые явно; если что-то уже решено — участники поправят. БЕЗ "
    "давления, БЕЗ слов «просрочка»/«опоздание»/«срыв»/«почему не сделано»."
)


def format_open_tasks_block(
    open_tasks: list[str],
    *,
    doubt: Optional[list[dict]] = None,
    closed: Optional[list[dict]] = None,
) -> str:
    """Ф8/Ф3: блок-инструкция раздела висяков для промпта генерации (G9).

    Три части под одним мягким заголовком (`PENDING_SECTION_HEADING`):
      • Часть 1 — НЕзакрытые: сгруппированы по ответственному (R7), статус ставит
        модель по ТЕКУЩЕМУ транскрипту (висит/закрыта); срок — только если он уже в
        тексте, без маркеров просрочки (R6/R3);
      • Часть 2 — подразделы «под сомнением» (R10) и «закрытые с прошлых встреч»
        (R21): модель копирует их ДОСЛОВНО, не переоценивая статус.
    Всё пусто → "" (раздела не будет). Дисциплина: это НЕ справка-«не-факт» (как
    память серии), а инструкция перенести/показать — потому идёт ПОСЛЕ блока памяти.
    """
    open_tasks = open_tasks or []
    doubt = doubt or []
    closed = closed or []
    if not (open_tasks or doubt or closed):
        return ""
    lines = [_OPEN_TASKS_BLOCK_HEADER]
    if open_tasks:
        lines += [
            "",
            "ЧАСТЬ 1 — НЕзакрытые, сгруппируй по ответственному (НЕ плоским списком). "
            "Для каждого человека — подзаголовок жирным `**Имя**`, под ним его задачи "
            "строкой «- <текст задачи дословно> — <статус>». Если в задаче есть префикс "
            "исполнителя «Имя: …» — это и есть ответственный (вынеси имя в подзаголовок; "
            "в строке имя можно не повторять). Задачи без явного исполнителя выведи "
            "первыми, без подзаголовка. Статус ставь ТОЛЬКО по ТЕКУЩЕМУ транскрипту: "
            "решена/выполнена на этой встрече → «закрыта»; в транскрипте не всплыла → "
            "«висит»; упомянута, но неясно → «висит, статус?» (НЕ угадывай «закрыта» — "
            "лучше показать висящей лишний раз). Срок показывай ТОЛЬКО если он уже есть "
            "в тексте задачи (в скобках «(срок: …)»); не добавляй сроков и НЕ помечай "
            "просрочку — этого в данных нет.",
            "",
            "Незакрытые задачи по людям:",
        ]
        nameless, owned = _group_open_by_owner(open_tasks)
        for t in nameless:
            lines.append(f"- {t}")
        for owner, items in owned:
            lines.append(f"**{owner}**")
            for it in items:
                lines.append(f"- {it}")
    subsections = _render_pending_subsections(doubt, closed)
    if subsections:
        lines += [
            "",
            "ЧАСТЬ 2 — приведённые НИЖЕ подразделы СКОПИРУЙ В КОНЕЦ ЭТОГО ЖЕ раздела "
            "ДОСЛОВНО, символ в символ: НЕ меняй формулировок, НЕ группируй по людям, "
            "НЕ проставляй им статус по транскрипту (их статус уже подтверждён вне этой "
            "встречи). Маркеры подразделов («🟡 …», «✅ …», курсивные ярлыки) сохрани "
            "как есть.",
            "",
            subsections,
        ]
    lines += [
        "",
        "(конец: у НЕзакрытых проставь статус по транскрипту; подразделы Части 2 — дословно)",
    ]
    return "\n".join(lines).rstrip() + "\n"


def build_open_tasks_block(
    digests: list[dict],
    *,
    series_dir: Optional[Path] = None,
    meeting_sid: Optional[str] = None,
    mark_shown: bool = False,
) -> str:
    """Ф8 (G9): единая точка сборки блока открытых задач для ОБОИХ триггеров
    (finalize И clarify) — как `build_cross_memory_block` для кросс-фона.

    Под kill-switch `is_open_tasks_enabled()`. Best-effort: выключено / нет хвоста /
    любой сбой → "" (трекинг опционален, генерацию не роняет). Приватность (опасная
    тройка): текст задач НЕ логируем — только счётчики (висящих / закрытых / под
    сомнением) и длину блока.

    Ф2 (pending-items): `series_dir` (если передан) → накладываем sidecar-статусы на
    свежесгенерированный хвост (`merge_open_tasks_with_status`): терминально закрытые
    (отменён/закрыто-сделано/закрыто-авто) В ХВОСТ «висит» НЕ попадают — статусы
    reply/сверщика переживают регенерацию и закрытое не воскресает. `series_dir=None`
    → прежнее поведение Ф8.

    Ф3 (pending-items): рендерим ВСЕ три корзины — висящие (по людям, R7) + подразделы
    «под сомнением» (R10) и «закрытые» (R21). `mark_shown=True` (канонический показ из
    finalize) → после включения закрытых в блок зовём `mark_status_shown`: на следующих
    встречах merge их не вернёт (показ один раз, список не копится). clarify передаёт
    `mark_shown=False` — реген уже показанного протокола shown-состояние НЕ двигает.
    """
    if not is_open_tasks_enabled():
        return ""
    try:
        # Раскручиваем хвост БЕЗ капа, накладываем статусы, и капим уже ВИСЯЩИЕ —
        # чтобы закрытая задача не съедала слот капа у живого висяка (Ф2).
        fresh = _resolve_open_tasks_raw(digests)
        store = load_task_status(series_dir) if series_dir is not None else {}
        merged = merge_open_tasks_with_status(fresh, store)
        cap = open_tasks_max()
        hanging = merged["open"][:cap] if cap > 0 else merged["open"]
        doubt = merged["doubt"]
        closed = merged["closed"]
        block = format_open_tasks_block(hanging, doubt=doubt, closed=closed)
        # R21: закрытые показываем ОДИН раз. Помечаем shown ТОЛЬКО на каноническом
        # показе (finalize, mark_shown=True) и только если они реально попали в блок.
        if mark_shown and closed and series_dir is not None and block:
            for c in closed:
                try:
                    mark_status_shown(series_dir, c.get("text", ""))
                except Exception:  # noqa: BLE001 — пометка best-effort, блок уже собран
                    pass
        logger.info(
            "[open-tasks] block meeting=%s carried=%d closed=%d doubt=%d block_len=%d",
            meeting_sid or "?", len(hanging), len(closed), len(doubt), len(block),
        )
        return block
    except Exception as e:  # noqa: BLE001 — трекинг опционален, генерацию не роняем
        logger.warning(
            "[open-tasks] build failed meeting=%s (non-fatal): %s",
            meeting_sid or "?", type(e).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# Ф2 (pending-items) — sidecar статусов висяка + слияние при рендере
# ---------------------------------------------------------------------------
def task_status_path(series_dir: Path) -> Path:
    """Путь sidecar-файла статусов серии: `<series_dir>/task-status.json`."""
    return Path(series_dir) / TASK_STATUS_FILE


def load_task_status(series_dir: Optional[Path]) -> dict:
    """Читает sidecar статусов серии → `{_status_key: record}`.

    Нет каталога/файла/мусор/старая серия без sidecar → `{}` (миграция не нужна:
    отсутствие файла = «статусов нет», все задачи висят). record:
    `{status, reason, source, shown, text, updated}`. Запись с неизвестным `status`
    отбрасывается (битьё не валит рендер). Финализация этот файл НЕ пишет — он живёт
    отдельно от `<date>-memory.json`, поэтому статусы переживают регенерацию (Ф2).
    НЕ логирует тексты (опасная тройка).
    """
    if series_dir is None:
        return {}
    p = task_status_path(series_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    out: dict = {}
    for k, rec in items.items():
        if not isinstance(k, str) or not isinstance(rec, dict):
            continue
        if rec.get("status") not in _VALID_STATUSES:
            continue
        out[k] = rec
    return out


def save_task_status(series_dir: Path, store: dict) -> Optional[Path]:
    """Atomic-запись sidecar статусов серии. Best-effort: сбой → None.

    НЕ логирует тексты задач/причин (опасная тройка) — только счётчик записей.
    """
    p = task_status_path(series_dir)
    payload = {"schema": TASK_STATUS_SCHEMA, "items": store}
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        _atomic_write_text(p, text)
    except (OSError, TypeError, ValueError) as e:
        logger.warning("[task-status] save failed (non-fatal): %s", type(e).__name__)
        return None
    logger.info("[task-status] saved items=%d", len(store))
    return p


def set_task_status(
    series_dir: Path,
    task_text: str,
    status: str,
    *,
    reason: Optional[str] = None,
    source: Optional[str] = None,
    date: Optional[str] = None,
) -> Optional[dict]:
    """Ф2: выставить/обновить статус висяка в sidecar (потребители — Ф5 reply, Ф6 сверщик).

    Ключ = `_status_key(task_text)` (без срок-суффикса — стабилен к переформулировке).
    Возвращает обновлённый record или None (пустой ключ/сбой). Идемпотентно по ключу.
    Семантика флага `shown` (R21): смена статуса СБРАСЫВАЕТ `shown=False` (новое
    закрытие надо показать заново подразделом «закрытые»); повтор того же статуса
    `shown` не трогает. Переоткрытие (A10) — `status=STATUS_OPEN`: задача снова висит,
    причина/показ обнуляются. Текст храним последний виденный (для рендера Ф3).
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"unknown task status: {status!r}")
    key = _status_key(task_text)
    if not key:
        return None
    store = load_task_status(series_dir)
    prev = store.get(key) or {}
    status_changed = status != prev.get("status")
    if status == STATUS_OPEN:
        rec = {
            "status": STATUS_OPEN,
            "reason": None,
            "source": source or prev.get("source"),
            "shown": False,
            "text": (task_text or prev.get("text") or "").strip(),
            "updated": date,
        }
    else:
        rec = {
            "status": status,
            "reason": reason if reason is not None else prev.get("reason"),
            "source": source or prev.get("source"),
            # новое закрытие → показать заново; тот же статус → сохранить shown.
            "shown": False if status_changed else bool(prev.get("shown")),
            "text": (task_text or prev.get("text") or "").strip(),
            "updated": date,
        }
    store[key] = rec
    save_task_status(series_dir, store)
    return rec


def mark_status_shown(
    series_dir: Path, task_text_or_key: str, *, by_key: bool = False
) -> bool:
    """R21: пометить закрытую задачу «показано» (Ф3 после рендера подраздела «закрытые»).

    Идемпотентно. `by_key=True` — аргумент уже `_status_key`. Возвращает True, если
    запись была (и теперь shown). Нет записи → False.
    """
    key = task_text_or_key if by_key else _status_key(task_text_or_key)
    store = load_task_status(series_dir)
    rec = store.get(key)
    if not rec:
        return False
    if rec.get("shown"):
        return True
    rec["shown"] = True
    store[key] = rec
    save_task_status(series_dir, store)
    return True


def merge_open_tasks_with_status(fresh_tail: list[str], status_store: dict) -> dict:
    """Ф2: наложение sidecar-статусов на свежесгенерированный хвост `open_tasks`.

    `fresh_tail` — тексты задач из `resolve_open_tasks` (пересобирается из текста
    протокола на каждом финализе). `status_store` — `{_status_key: record}` из
    `load_task_status`. Возвращает три корзины (порядок хвоста сохранён):
      • `open`   — `list[str]`: висящие (нет записи / `open` / переоткрытые) → идут
                   в раздел «🔻 С прошлых встреч» (LLM проставит статус по транскрипту);
      • `doubt`  — `list[dict]` `{text, reason}`: «под сомнением» (буфер R10) → Ф3
                   рендерит отдельным подблоком; НЕ в `open` (не плоский «висит»);
      • `closed` — `list[dict]` `{text, status, reason}`: терминально закрытые
                   (done/cancelled/auto_closed) с `shown=False` → Ф3 рендерит
                   подразделом «закрытые» ОДИН раз, затем `mark_status_shown`.
    Терминально закрытые НИКОГДА не попадают в `open` → «отменён»/«закрыто-авто» не
    воскресают как «висит» (ядро Ф2). Уже показанные закрытые (`shown=True`) не идут
    ни в одну корзину (исчезли из выдачи — список не копится, R21). Чистая функция.
    """
    open_tasks: list[str] = []
    doubt: list[dict] = []
    closed: list[dict] = []
    for text in fresh_tail or []:
        if not isinstance(text, str) or not text.strip():
            continue
        rec = status_store.get(_status_key(text)) if status_store else None
        status = rec.get("status") if isinstance(rec, dict) else None
        if status in _TERMINAL_CLOSED:
            if not (isinstance(rec, dict) and rec.get("shown")):
                closed.append({
                    "text": text,
                    "status": status,
                    "reason": rec.get("reason") if isinstance(rec, dict) else None,
                })
            # shown=True → задача показана и закрыта: ни в open, ни в closed (R21).
            continue
        if status == STATUS_DOUBT:
            doubt.append({
                "text": text,
                "reason": rec.get("reason") if isinstance(rec, dict) else None,
            })
            continue
        # нет записи / open / переоткрытая → висит
        open_tasks.append(text)
    return {"open": open_tasks, "doubt": doubt, "closed": closed}


# ---------------------------------------------------------------------------
# 7.5 — разовый бэкфилл выжимок из уже лежащих протоколов
# ---------------------------------------------------------------------------
def _date_from_protocol_filename(name: str) -> Optional[str]:
    """`2026-05-27-protokol.md` → `2026-05-27`. Иначе None."""
    if not name.endswith(PROTOCOL_FILE_SUFFIX):
        return None
    stem = name[: -len(PROTOCOL_FILE_SUFFIX)]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", stem):
        return stem
    return None


def backfill_series(
    series_dir: Path,
    *,
    overwrite: bool = False,
    participant_filter: Optional[Callable[[list], list]] = None,
) -> int:
    """7.5: собирает выжимки из готовых протоколов `<date>-protokol.md` серии.

    Для каждого протокола без `<date>-memory.json` (или с `overwrite=True`)
    строит выжимку и сохраняет рядом. Детерминированно, без claude — безопасно
    гонять разово. `series` и `participants` берём из шапки протокола (meta.json
    рядом не парсим — шапка протокола уже несёт состав).

    Ф2 (Ф1 §5): `participant_filter` пробрасывается в `build_digest` — старые
    протоколы (до Ф1) могут нести UI-мусор Телемоста в шапке участников; без
    фильтра он осел бы в памяти серии. Обычно `protocol_to_tg.filter_participant_names`.

    Возвращает число записанных выжимок.
    """
    d = Path(series_dir)
    if not d.is_dir():
        return 0
    series_slug = d.name
    written = 0
    for entry in sorted(d.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        date = _date_from_protocol_filename(entry.name)
        if not date:
            continue
        mem_path = digest_path(d, date)
        if mem_path.exists() and not overwrite:
            continue
        try:
            protocol_text = entry.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("[series-memory] backfill read failed %s: %s", entry, e)
            continue
        if not protocol_text.strip():
            continue
        meta = {
            "series": series_slug,
            "date": date,
            # participants возьмёт из шапки протокола (meta пуст по составу).
        }
        digest = build_digest(protocol_text, meta, date=date,
                              participant_filter=participant_filter)
        if save_digest(d, date, digest):
            written += 1
    if written:
        logger.info("[series-memory] backfill series=%s wrote=%d", series_slug, written)
    return written


def backfill_root(
    root: Path,
    *,
    overwrite: bool = False,
    participant_filter: Optional[Callable[[list], list]] = None,
) -> dict:
    """7.5: бэкфилл по всем сериям под `root`. Служебные `_*`/`.`-папки пропускаем.

    Возвращает {"series": N, "digests": M} — сколько серий тронуто и выжимок
    записано. РЕАЛЬНЫЙ прогон по живым протоколам — операция утреннего деплоя
    (ПДн): ночью только код+unit на синтетике. `participant_filter` — см. `backfill_series`.
    """
    r = Path(root)
    if not r.is_dir():
        return {"series": 0, "digests": 0}
    series_touched = 0
    digests_written = 0
    for entry in sorted(r.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        n = backfill_series(entry, overwrite=overwrite, participant_filter=participant_filter)
        if n:
            series_touched += 1
            digests_written += n
    logger.info("[series-memory] backfill_root: series=%d digests=%d", series_touched, digests_written)
    return {"series": series_touched, "digests": digests_written}


# ===========================================================================
# Ф6 (umnyi-protokol-assemblyai) — КРОСС-встречная память уровня компании
# ===========================================================================
# Отличие от резолва ТОЙ ЖЕ серии (`resolve_memory`): кросс-память собирает ШИРОКИЙ
# фон из ДРУГИХ серий и кладёт в промпт генерации отдельным блоком «ФОН». Приоритет
# своей компании — МЯГКИЙ (A7: жёсткой стены между компаниями нет; кросс-компания
# участвует при релевантности — напр. совместные поставки/процессы). Главный guard
# приватности — фильтр чувствительного G11 (LLM-классификатор, инъекция
# `sensitive_classifier`), плюс ручной маркер серии `visibility=private`
# («приватное — не использовать как фон»). «Опасная тройка» (РИСК4): сюда сходятся
# производные ПДн из РАЗНЫХ встреч + LLM-обработка + egress — поэтому: в промпт идёт
# ТОЛЬКО отобранное и лимитированное (РАЗМ1), текст фона/кандидатов НЕ логируется,
# при сомнении классификатора — исключаем (консервативно).
#
# Этот модуль остаётся stdlib-only: и резолвер компании/видимости, и LLM-классификатор
# ИНЪЕКТИРУЮТСЯ колбэками (как `participant_filter` в `build_digest`) — claude/реестр
# живут у вызывателя (`llm_postprocess.build_cross_memory_block`).

# Веса ранкера. Участники — сильнейший сигнал «та же орбита людей», тема — «тот же
# предмет», свежесть — лёгкий бонус недавнему. Сумма нормировки не требует: score
# нужен для сортировки и отсечки по MIN_SCORE.
_CROSS_W_PARTICIPANTS = 0.5
_CROSS_W_TOPIC = 0.4
_CROSS_W_FRESHNESS = 0.1
# Кросс-компания — МЯГКИЙ приоритет (НЕ стена, A7): её score множится на фактор < 1,
# поэтому для попадания в фон ей нужна более высокая «сырая» релевантность. Применяем
# ТОЛЬКО когда обе компании известны и различны (иначе нейтрально 1.0 — fallback по
# slug, см. УПУ1: без разметки company-приоритет деградирует мягко).
_CROSS_COMPANY_FACTOR = 0.6
# Минимальный финальный score для попадания в фон. Кандидат без пересечения по людям
# И по теме (relevance==0) отсекается раньше — свежесть сама по себе фоном не делает.
_CROSS_MIN_SCORE = 0.12
# Окно свежести (дни): встреча сегодня → 1.0, старше окна → 0.0 (линейно).
_CROSS_FRESHNESS_WINDOW_DAYS = 180
# Потолок кандидатов, уходящих в классификатор — cost-guard на LLM-вызов G11 (один
# батч-вызов на ≤ этого числа топ-релевантных кандидатов, не на весь архив).
_CROSS_POOL_CAP = 8

# Дефолты лимита блока (РАЗМ1). N — сколько выжимок, M — потолок символов.
DEFAULT_CROSS_MAX_DIGESTS = 3
DEFAULT_CROSS_MAX_CHARS = 1800
# Сколько тем/пунктов одной выжимки показывать в блоке ФОНА. Фон — это ХИНТ
# (общие проекты/термины), не полная запись: ограничиваем вклад одной выжимки,
# чтобы богатая прошлая встреча не съедала весь бюджет M в одиночку.
_CROSS_BLOCK_THEMES_PER_DIGEST = 8
_CROSS_BLOCK_POINTS_PER_DIGEST = 5

# Стоп-слова (рус/eng) — выкидываем из тематических токенов, иначе предлоги/союзы
# дают ложное пересечение тем между любыми встречами.
_TOPIC_STOPWORDS = frozenset({
    "и", "в", "во", "на", "по", "для", "от", "до", "из", "за", "о", "об", "с", "со",
    "к", "у", "что", "как", "это", "этот", "эта", "эти", "тот", "та", "те", "не",
    "ни", "да", "но", "или", "the", "a", "an", "of", "to", "in", "on", "for", "and", "or",
})
_TOPIC_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_MIN_TOPIC_TOKEN_LEN = 3


def is_cross_enabled() -> bool:
    """Ф6: kill-switch `ENABLE_CROSS_MEMORY` (дефолт ON; `0/false/no` → OFF).

    Отдельный от `ENABLE_SERIES_MEMORY`: кросс-фон (другие серии) можно выключить в
    проде, оставив резолв памяти ТОЙ ЖЕ серии (diarization/постоянный состав) живым.
    """
    raw = (os.environ.get("ENABLE_CROSS_MEMORY") or "").strip().lower()
    return raw not in ("0", "false", "no")


def cross_memory_max_digests() -> int:
    """Ф6 (G7/РАЗМ1): лимит числа выжимок кросс-фона `CROSS_MEMORY_MAX_DIGESTS` (3).

    Невалидное/<=0 → дефолт. Потолок 10 — кросс-фон справочный, не должен раздувать
    промпт (длинная история — это РАЗМ1, отсекаем).
    """
    raw = (os.environ.get("CROSS_MEMORY_MAX_DIGESTS") or "").strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CROSS_MAX_DIGESTS
    if val <= 0:
        return DEFAULT_CROSS_MAX_DIGESTS
    return min(val, 10)


def cross_memory_max_chars() -> int:
    """Ф6 (G7/РАЗМ1): потолок символов блока кросс-фона `CROSS_MEMORY_MAX_CHARS` (1800).

    Невалидное/<=0 → дефолт. Пол 200 (меньше — блок бессмысленен), потолок 8000
    (защита промпта).
    """
    raw = (os.environ.get("CROSS_MEMORY_MAX_CHARS") or "").strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CROSS_MAX_CHARS
    if val <= 0:
        return DEFAULT_CROSS_MAX_CHARS
    # Пол 700 (а не меньше): заголовок-дисциплина блока ~430 символов — при M ниже
    # этого блок всегда схлопывался бы в "" (одна выжимка не влезает к заголовку).
    return min(max(val, 700), 8000)


def _topic_tokens(texts) -> set:
    """Множество тематических токенов из строк (темы/ключевые пункты).

    Lowercase, выкидываем стоп-слова и токены короче `_MIN_TOPIC_TOKEN_LEN`. Только
    для ранжирования (пересечение тем) — НЕ хранится, НЕ логируется.
    """
    out: set = set()
    for t in texts or []:
        if not isinstance(t, str):
            continue
        for m in _TOPIC_TOKEN_RE.findall(t.lower()):
            if len(m) >= _MIN_TOPIC_TOKEN_LEN and m not in _TOPIC_STOPWORDS:
                out.add(m)
    return out


def build_topic_profile(digests) -> set:
    """Ф6: тематический профиль текущей серии — токены тем/пунктов её выжимок.

    Опора оси «тема» при ранжировании кросс-фона: «эта серия про X» → кросс-серия,
    тоже про X, релевантнее. Пустой список (новая серия без истории) → пустой профиль
    → ось темы = 0 (ранжирование мягко падает на участников+свежесть).
    """
    texts: list = []
    for dig in digests or []:
        if not isinstance(dig, dict):
            continue
        texts.extend(dig.get("themes") or [])
        texts.extend(dig.get("key_points") or [])
    return _topic_tokens(texts)


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _freshness(date_str: Optional[str], today: Optional[str]) -> float:
    """Свежесть выжимки в [0,1]: сегодня → 1.0, старше окна → 0.0 (линейно).

    Битая/пустая дата → 0.0 (без бонуса свежести, не фатально).
    """
    if not date_str or not today:
        return 0.0
    try:
        from datetime import date as _date
        y, m, d = (int(x) for x in str(date_str).split("-"))
        ty, tm, td = (int(x) for x in str(today).split("-"))
        age = (_date(ty, tm, td) - _date(y, m, d)).days
    except (ValueError, TypeError):
        return 0.0
    if age <= 0:
        return 1.0
    if age >= _CROSS_FRESHNESS_WINDOW_DAYS:
        return 0.0
    return 1.0 - age / _CROSS_FRESHNESS_WINDOW_DAYS


def resolve_cross_memory(
    root: Path,
    *,
    current_series_dir: Path,
    current_company: Optional[str] = None,
    current_participants: Optional[list] = None,
    current_topic_tokens: Optional[set] = None,
    current_date: Optional[str] = None,
    markup_resolver: Optional[Callable[[str], tuple]] = None,
    sensitive_classifier: Optional[Callable[[list], list]] = None,
    exclude_keys: Optional[set] = None,
    max_digests: Optional[int] = None,
    today: Optional[str] = None,
) -> list:
    """Ф6 (G6/G7/G11): ШИРОКИЙ кросс-встречный фон из ДРУГИХ серий.

    Пул = выжимки ВСЕХ серий под `root`, КРОМЕ текущей (`current_series_dir`).
    Ранжирование по трём осям (G7): Jaccard-по-участникам (не-владельцы) + Jaccard
    тематических токенов (тема) + свежесть. Компания — МЯГКИЙ приоритет (A7), не стена:
    кросс-компания штрафуется множителем, не отбрасывается. Ручной маркер серии
    `visibility=='private'` (через `markup_resolver`) → серия в пул НЕ берётся вовсе
    («приватное — не использовать как фон»). Топ-`_CROSS_POOL_CAP` кандидатов идут в
    `sensitive_classifier` (G11) ОДНИМ батчем; помеченные чувствительными — выкинуты.
    Возвращает ≤`max_digests` ОТОБРАННЫХ выжимок (по убыванию релевантности).

    Инъекции (stdlib-only): `markup_resolver(slug) -> (company, visibility)` и
    `sensitive_classifier(candidates) -> list[bool]` (True=чувствительно). Без
    классификатора (None) → [] КОНСЕРВАТИВНО: G11 — главный guard, без него фон не
    отдаём. `exclude_keys` — set из `(series, date)`, уже показанных в блоке памяти
    ТОЙ ЖЕ серии (fallback-матч по составу), чтобы не дублировать их в фоне.

    Приватность (опасная тройка): текст кандидатов/фона тут НЕ логируется — только
    счётчики у вызывателя. Best-effort внутри пер-серийного скана; общий best-effort
    держит `build_cross_memory_block`.
    """
    r = Path(root)
    if not r.is_dir():
        return []
    if max_digests is None:
        max_digests = cross_memory_max_digests()
    if today is None:
        from datetime import date as _date  # локальный импорт: модуль stdlib-only
        today = _date.today().isoformat()
    cur_participants = current_participants or []
    cur_topic = current_topic_tokens or set()
    excl = exclude_keys or set()
    try:
        cur_resolved = current_series_dir.resolve()
    except OSError:
        cur_resolved = current_series_dir

    scored: list = []  # (score, digest)
    try:
        entries = list(r.iterdir())
    except OSError as e:
        # Корень нечитаем (напр. неожиданно высокий путь) — тихо без фона, не падаем.
        logger.warning("[cross-memory] scan failed (non-fatal): %s", type(e).__name__)
        return []
    for entry in entries:
        # Весь файловый доступ по входу — под OSError-гардом: неreadable серия
        # (напр. чужая папка при неожиданно высоком корне) пропускается, не валит скан.
        try:
            if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
                continue
            try:
                if entry.resolve() == cur_resolved:
                    continue
            except OSError:
                pass
            # БЕЗ exclude_date: текущая серия уже исключена целиком выше (по resolve),
            # а у ЧУЖОЙ серии встреча в ту же календарную дату — валидный фон, не дубль
            # текущей. Передача current_date сюда раньше молча роняла такие кросс-встречи.
            digs = list_series_digests(entry)
        except OSError:
            continue
        if not digs:
            continue
        slug = (digs[0].get("series") or entry.name) if isinstance(digs[0], dict) else entry.name
        company = None
        visibility = None
        if markup_resolver is not None:
            try:
                company, visibility = markup_resolver(slug)
            except Exception:  # noqa: BLE001 — резолв разметки best-effort
                company, visibility = None, None
        # Ручной маркер «приватное — не использовать как фон»: серия целиком вне пула.
        if isinstance(visibility, str) and visibility.strip().lower() == "private":
            continue
        # Кросс-компания — мягкий штраф (A7), и только когда ОБЕ компании известны.
        if current_company and company and company != current_company:
            company_factor = _CROSS_COMPANY_FACTOR
        else:
            company_factor = 1.0
        for dig in digs:
            if not isinstance(dig, dict):
                continue
            if (dig.get("series"), dig.get("date")) in excl:
                continue
            p_overlap = _participant_overlap(cur_participants, dig.get("participants") or [])
            t_overlap = _jaccard(cur_topic, _topic_tokens((dig.get("themes") or []) + (dig.get("key_points") or [])))
            if p_overlap <= 0.0 and t_overlap <= 0.0:
                continue  # нет пересечения ни по людям, ни по теме → не фон
            fresh = _freshness(dig.get("date"), today)
            score = (
                _CROSS_W_PARTICIPANTS * p_overlap
                + _CROSS_W_TOPIC * t_overlap
                + _CROSS_W_FRESHNESS * fresh
            ) * company_factor
            if score < _CROSS_MIN_SCORE:
                continue
            scored.append((score, dig))
    if not scored:
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = [dig for _, dig in scored[:_CROSS_POOL_CAP]]
    # G11 — главный guard. Без классификатора фон НЕ отдаём (консервативно).
    if sensitive_classifier is None:
        return []
    try:
        verdicts = sensitive_classifier(candidates)
    except Exception:  # noqa: BLE001 — сбой классификатора → консервативно всё скрыть
        return []
    if not isinstance(verdicts, list) or len(verdicts) != len(candidates):
        return []  # несоответствие длины = доверять нельзя → исключить всё
    safe = [dig for dig, bad in zip(candidates, verdicts) if not bad]
    return safe[:max_digests] if max_digests > 0 else safe


# Блок-ДАННЫЕ кросс-фона. Дисциплина ЖЁСТЧЕ, чем у памяти той же серии: факты ДРУГИХ
# встреч к текущей могут не иметь отношения вовсе — перенос любого из них = ошибка.
_CROSS_MEMORY_BLOCK_HEADER = (
    "ФОН — связанные встречи из ДРУГИХ серий (это НЕ источник фактов для протокола!).\n"
    "Ниже короткие выжимки релевантных встреч (возможно, других команд/направлений),\n"
    "приведены ТОЛЬКО как фон: распознать общие проекты/термины/контекст. СТРОГО "
    "ЗАПРЕЩЕНО переносить в протокол ЛЮБЫЕ темы, решения, задачи, числа или имена из "
    "этого фона — к ТЕКУЩЕЙ встрече они могут не относиться. Все факты протокола — "
    "только из ТЕКУЩЕГО транскрипта ниже."
)


def format_cross_memory_block(digests: list, *, max_chars: Optional[int] = None) -> str:
    """Ф6 (G7/РАЗМ1): блок кросс-фона с дисциплиной «фон, не факт» и потолком символов.

    Состав выжимки в блоке — серия + дата + темы + ключевые пункты. Участников ДРУГИХ
    встреч НЕ выводим (лишний egress ПДн без пользы для генерации; имена постоянного
    состава — забота памяти ТОЙ ЖЕ серии). Пустой список → "". Превышение `max_chars`
    → выкидываем ХВОСТОВЫЕ (наименее релевантные) выжимки целиком и пересобираем; не
    влезает даже одна → "" (лучше без фона, чем рваный).
    """
    if not digests:
        return ""
    if max_chars is None:
        max_chars = cross_memory_max_chars()
    selected = list(digests)
    while selected:
        lines = [_CROSS_MEMORY_BLOCK_HEADER, ""]
        for dig in selected:
            date = dig.get("date") or "—"
            series = dig.get("series") or "другая серия"
            lines.append(f"— Серия «{series}», встреча {date}:")
            themes = (dig.get("themes") or [])[:_CROSS_BLOCK_THEMES_PER_DIGEST]
            if themes:
                lines.append(f"  Темы: {'; '.join(themes)}")
            for kp in (dig.get("key_points") or [])[:_CROSS_BLOCK_POINTS_PER_DIGEST]:
                lines.append(f"  • {kp}")
            lines.append("")
        lines.append("(конец фона — ниже текущая встреча, факты только из неё)")
        block = "\n".join(lines).rstrip() + "\n"
        if len(block) <= max_chars:
            return block
        selected = selected[:-1]  # хвост (наименее релевантный) не влез — выкидываем
    return ""
