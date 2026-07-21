# -*- coding: utf-8 -*-
"""Ф3 (план `2026-07-20-watson-smart-replies-rethink.md`, R24 + R20): ОДНО хранилище
«журнала решений understand-слоя» — память «слово/формулировка → класс статуса» плюс
журнал незнакомых слов.

Зачем (R24, требование владельца 20.07): бот должен умнеть от ответов. Если участник
однажды написал статус нестандартным словом («отработала», «разрулили»), и ИИ-разбор
верно отнёс это к классу (done), то во ВТОРОЙ раз это слово применяется БЫСТРЫМ путём
без ИИ-сомнения — как если бы оно было в словаре. Так однотипный переспрос не повторяется.

Граница (РАЗМ1, решение владельца): запоминается ТОЛЬКО «слово → класс статуса», НЕ
привязка к конкретной задаче. Память понижает ступень уверенности R8 лишь для РАСПОЗНАВАНИЯ
СЛОВА; чужие задачи (R11) и сам факт закрытия конкретной задачи всё равно проходят
страховки understand-слоя. Scope пополнения — ГЛОБАЛЬНЫЙ быстрый путь (слово ведёт себя
одинаково во всех сериях), поэтому хранилище одно на бота, не на серию.

Одно хранилище с журналом R20 (не два, RAZM1): те же события пишут и «выучил слово→класс»
(`learn`), и «встретил незнакомое слово, исход такой-то» (`journal`). R20 (Ф6) читает
этот же файл — новое хранилище под журнал не заводим.

Откат — по аналогии с learning-rollback (`feedback_learning.apply_rollback_reply`):
append-only, откат = НОВОЕ событие `rollback`, старое НЕ мутируется/не удаляется; свёртка
(`_fold`) берёт последнее событие по ключу. Так история решений аудируема (R20).

Опасная тройка (CLAUDE.md проекта): в файл кладём ТОЛЬКО статус-СЛОВО + класс + исход +
дата (без year-точности → просто ISO-дата) — НЕ тексты реплик, НЕ имена задач, НЕ ответы
Claude. Тексты reply в долгоживущее хранилище не попадают.

stdlib-only (исполняется в listener-контексте, системный python3.9 без venv).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("notary.decision_memory")

# Классы статуса — зеркало `pending_status_reply.LABEL_*` (не импортируем его, чтобы
# не тянуть цикл: understand-слой импортирует ОБА). Валидируем вход по этому набору.
STATUS_CLASSES = frozenset({"done", "cancelled", "doubt", "wait", "reopen"})

# Событие журнала пишем под одним процесс-локом (listener однопроцессный; append <4КБ
# на POSIX атомарен, лок страхует от переплетения строк при фоновом воркере).
_WRITE_LOCK = threading.Lock()

_MAX_KEY_LEN = 48          # длина ключа-слова (защита от гигантских полей)
_MIN_KEY_LEN = 4           # короче — не идентифицирующее слово (как токенайзер матчинга)
_MIN_NAME_LEN = 2          # имя короче — не идентифицирующее (У4: журнал вокатив-пар; Оля/Ян)

# Прямое отрицание «не <слово>» — зеркало dict `pending_status_reply._REOPEN_RE`
# (`\bне\s+<verb>`). Класс выученного слова относится к УТВЕРДИТЕЛЬНОЙ форме: «не разрулили»
# ≠ «разрулили». Из отрицательного сегмента память НЕ учит и по памяти НЕ применяет — иначе
# ложное закрытие по отрицанию (класс бага FU-1, который Ф1 закрыла для словаря). Ф3-цикл5.
_NEG_WORD_RE = re.compile(r"\bне\s+([a-zа-я0-9]+)")


def _store_path(explicit: Optional[Path] = None) -> Path:
    """Путь к JSONL-хранилищу. Приоритет: явный аргумент → env
    `MEETING_NOTARY_DECISION_MEMORY_FILE` → дефолт под STATE_DIR (НЕ в PII-дереве
    протоколов). Тесты подставляют tmp-файл через env/аргумент."""
    if explicit is not None:
        return Path(explicit)
    raw = os.environ.get("MEETING_NOTARY_DECISION_MEMORY_FILE")
    if raw:
        return Path(os.path.expanduser(raw))
    state = os.environ.get("MEETING_NOTARY_STATE_DIR") or "~/.local/state"
    return Path(os.path.expanduser(state)) / "meeting-notary-watson" / "decision-memory.jsonl"


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _word_key(word: str) -> Optional[str]:
    """Нормализованный ключ статус-слова. lower, ё→е, срез пробелов/пунктуации по
    краям, потолок длины. Короче `_MIN_KEY_LEN` или пусто → None (не запоминаем)."""
    w = _norm(word).strip().strip(".,;:!?«»\"'()[]")
    w = re.sub(r"\s+", " ", w).strip()
    if len(w) < _MIN_KEY_LEN:
        return None
    return w[:_MAX_KEY_LEN]


def _today() -> str:
    """ISO-дата без времени (журнал R20 — «слово+дата+исход», без секунд/PII-точности)."""
    return time.strftime("%Y-%m-%d", time.localtime())


def _append_event(store_path: Path, event: dict) -> bool:
    """Дописать одно событие в JSONL (append-only). Best-effort: сбой I/O → warning +
    False, обработку не валим (listener не падает из-за журнала)."""
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with _WRITE_LOCK:
            with open(store_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except Exception as e:  # noqa: BLE001 — журнал best-effort, не критичен для обработки
        logger.warning("[decision-memory] запись события не удалась (non-fatal): %s", type(e).__name__)
        return False


def _read_events(store_path: Path) -> list:
    """Все события файла (порядок сохранён). Нет файла/битая строка → пропускаем строку.
    Опасная тройка: события не логируем (в них только слово+класс, но всё равно тихо)."""
    if not store_path.exists():
        return []
    out: list = []
    try:
        with open(store_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except Exception as e:  # noqa: BLE001
        logger.warning("[decision-memory] чтение журнала не удалось (non-fatal): %s", type(e).__name__)
        return []
    return out


def _fold(events: list) -> dict:
    """Свёртка событий `learn`/`rollback` в активную карту `{key: cls}`.

    Последнее событие по ключу побеждает (как `feedback_learning._fold`): повторный
    `learn` обновляет класс, `rollback` — убирает ключ. События `journal` (R20) на
    активную карту НЕ влияют — это аудит, не быстрый путь."""
    active: dict = {}
    for ev in events:
        kind = ev.get("ev")
        key = ev.get("key")
        if not isinstance(key, str) or not key:
            continue
        if kind == "learn":
            cls = ev.get("cls")
            if cls in STATUS_CLASSES:
                active[key] = cls
        elif kind == "rollback":
            active.pop(key, None)
    return active


def known_map(*, store_path: Optional[Path] = None) -> dict:
    """Активная карта `{статус-слово → класс}` (после свёртки). Пусто → {}."""
    return _fold(_read_events(_store_path(store_path)))


def remember_word_class(
    word: str, status_class: str, *, source: str = "reply",
    store_path: Optional[Path] = None,
) -> bool:
    """Запомнить «слово → класс статуса» (R24, событие `learn`). Невалидный класс /
    пустое слово → no-op (False). Идемпотентно по смыслу: повторный learn того же
    ключа обновляет класс. Возвращает True, если событие записано."""
    if status_class not in STATUS_CLASSES:
        return False
    key = _word_key(word)
    if key is None:
        return False
    return _append_event(_store_path(store_path), {
        "ev": "learn", "key": key, "cls": status_class,
        "src": (source or "reply")[:32], "date": _today(),
    })


def forget_word_class(word: str, *, store_path: Optional[Path] = None) -> bool:
    """Откатить выученное слово (событие `rollback`, learning-rollback-аналог). Слово
    не выучено → всё равно пишем rollback (идемпотентно, история аудируема). Пустой
    ключ → False."""
    key = _word_key(word)
    if key is None:
        return False
    return _append_event(_store_path(store_path), {
        "ev": "rollback", "key": key, "date": _today(),
    })


def negated_words(text: str) -> set:
    """Слова СРАЗУ после «не» (прямое отрицание). Зеркалит подход словаря
    (`pending_status_reply._REOPEN_RE`: `\\bне\\s+<verb>`) для памяти: класс выученного
    слова — про УТВЕРДИТЕЛЬНУЮ форму, «не <слово>» его переворачивает.

    Берём только слова ≥`_MIN_KEY_LEN` (как фильтр токенов recall): словарные негации
    статуса все длинные (закры/сдела/готов/…), а короткое «не раз», «не то» статус НЕ
    отрицают — иначе префиксно пере-подавили бы «разрулили» из-за «не раз»."""
    return {m.group(1) for m in _NEG_WORD_RE.finditer(_norm(text))
            if len(m.group(1)) >= _MIN_KEY_LEN}


def word_is_negated(text: str, word: str) -> bool:
    """True, если `word` (или его основа) стоит под прямым отрицанием «не …» в `text`.
    Из такого сегмента память НЕ учит и по нему НЕ применяет (иначе ложное закрытие по
    отрицанию — класс FU-1). Префиксная терпимость к словоизменению, как в матчинге."""
    w = _norm(word).strip()
    if not w:
        return False
    return any(w == n or w.startswith(n) or n.startswith(w) for n in negated_words(text))


def recall_word_class(text: str, *, store_path: Optional[Path] = None) -> Optional[str]:
    """Класс статуса для reply по ВЫУЧЕННОМУ слову, если оно встречается в тексте.

    Токенизируем text (lower, ё→е, ≥`_MIN_KEY_LEN`), для каждого токена ищем точное или
    префиксное совпадение с ключом активной карты (устойчивость к словоизменению, как
    `_tok_match` матчинга висяков). Первое совпадение → его класс. Ничего → None (памяти
    по этому reply нет → understand идёт словарём/ИИ). Детерминированно, БЕЗ сети.

    ОТРИЦАНИЕ: токен под «не …» (`negated_words`) память ПРОПУСКАЕТ — «не разрулили» ≠
    «разрулили» (иначе ложное закрытие по отрицанию, класс FU-1; зеркалит dict `_REOPEN_RE`).
    Тогда understand идёт классом ИИ (он отрицание понимает) или ступенью-3 (переспрос).

    Граница (РАЗМ1): это ТОЛЬКО распознавание слова → класс. Привязку к задаче и
    страховки чужих задач память НЕ обходит (это делает understand-слой отдельно)."""
    active = known_map(store_path=store_path)
    if not active:
        return None
    neg = negated_words(text)
    tokens = [t for t in re.findall(r"[a-zа-я0-9]+", _norm(text)) if len(t) >= _MIN_KEY_LEN]
    if not tokens:
        return None
    for key, cls in active.items():
        for tok in tokens:
            if any(tok == n or tok.startswith(n) or n.startswith(tok) for n in neg):
                continue  # «не <слово>» — отрицание переворачивает класс, память молчит
            if tok == key or (len(key) >= _MIN_KEY_LEN and tok.startswith(key)) \
                    or (len(tok) >= _MIN_KEY_LEN and key.startswith(tok)):
                return cls
    return None


def record_unknown_word(
    word: str, *, outcome: str, store_path: Optional[Path] = None,
) -> bool:
    """Журнал R20: слово, применённое ИИ МИМО словаря, + исход (класс/`unresolved`/…) +
    дата. БЕЗ текстов реплик (опасная тройка) — только слово+исход+дата. То же хранилище,
    что R24 (одно, не два). Пустой ключ → False."""
    key = _word_key(word)
    if key is None:
        return False
    return _append_event(_store_path(store_path), {
        "ev": "journal", "key": key, "outcome": (outcome or "")[:32], "date": _today(),
    })


def _name_key(name: str) -> Optional[str]:
    """Ключ ИМЕНИ для журнала вокатив-пар (У4). Мягче `_word_key`: имена бывают короткими
    (Оля, Ян). lower, ё→е, срез пунктуации по краям, потолок длины. Пусто/<`_MIN_NAME_LEN`
    → None (не журналим)."""
    n = _norm(name).strip().strip(".,;:!?«»\"'()[]")
    n = re.sub(r"\s+", " ", n).strip()
    if len(n) < _MIN_NAME_LEN:
        return None
    return n[:_MAX_KEY_LEN]


def record_vocative_flag(
    name_a: str, name_b: str, *, store_path: Optional[Path] = None,
) -> bool:
    """У4 (Ф6, follow-ups Ф5): журнал того, что пометила вокатив-проверка Ф5 —
    «vocative-пара помечена». В файл — ТОЛЬКО имена пары (нормализованные, отсортированы для
    стабильного ключа) + дата, БЕЗ текста реплик/транскрипта (опасная тройка; У4 разрешает
    имена, но НЕ реплики). То же хранилище, что R20/R24 (одно, не два). Событие `vocative`
    на активную карту слов НЕ влияет (аудит, как `journal` R20 — `_fold` его игнорирует).
    Пустое/короткое имя любой стороны → False."""
    a = _name_key(name_a)
    b = _name_key(name_b)
    if not a or not b:
        return False
    pair = sorted((a, b))
    return _append_event(_store_path(store_path), {
        "ev": "vocative", "pair": pair, "date": _today(),
    })


def journal_entries(*, store_path: Optional[Path] = None) -> list:
    """Все события журнала (learn/rollback/journal/vocative) в порядке записи — для аудита
    R20+У4 (Ф6). Возвращает список dict'ов как записаны."""
    return _read_events(_store_path(store_path))
