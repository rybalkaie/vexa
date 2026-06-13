"""Ф2 (umnyi-protokol-assemblyai, S4): сбор доменного словаря серии для
`keyterms_prompt` AssemblyAI.

Зачем (план `plans/2026-06-13-umnyi-protokol-assemblyai.md`, Фаза 2):
    Speechmatics путал имена участников, бренды и термины ниши (бенч 1123:
    `Sonix`→`Оникс`, `Playwright`→`ProLights`, `Сберовская`→`Кировская`). AAI
    даёт рычаг `keyterms_prompt` — список верных написаний, которым входное
    распознавание биасится к каноничной форме. Этот модуль СОБИРАЕТ такой список
    из уже имеющихся источников проекта; передачу в задание делает
    `assemblyai_client._create_transcript` (поле `keyterms_prompt`).

Источники (переиспользуем, НЕ изобретаем):
    1. Имена участников серии — `series_roster.get_roster(series_slug)` (поле
       `name`). Ростер company-scoped, fallback на встроенный хардкод (Ф3/Ф5).
    2. Доменные бренды/термины компании — `glossary.glossary_keyterms(company)`
       (канонические из YAML-знания `*-context`; fallback — встроенный
       `BUILTIN_KEYTERMS`). Только КАНОНИЧЕСКИЕ формы, НЕ ASR-искажения (`aliases`):
       искажение в keyterms биасило бы движок к ошибке.
    3. Ручные нишевые термины серии — опциональный файл
       `<MEETING_NOTARY_KEYTERMS_DIR>/<series_slug>.txt` (один термин на строку,
       `#`-комментарии и пустые строки игнорируются). Источник предусмотрен, но НЕ
       обязателен: нет файла → просто не добавляем (graceful).

Лимиты AAI (жёсткие, вывод гейта реальности dealer360 2026-06-12, REQ AA1/S4):
    - ≤1000 терминов в `keyterms_prompt` (universal-3-pro);
    - ≤6 слов на фразу — более длинные ОТБРАСЫВАЕМ (обрубок фразы биасил бы ASR к
      бессмысленному фрагменту), а не обрезаем по словам.
    `build_keyterms_prompt` применяет лимиты, нормализует и дедуплицирует
    (case-insensitive, первое вхождение оригинального регистра выигрывает).

Опасная тройка (CLAUDE.md проекта, РИСК2): keyterms содержат ИМЕНА участников —
    сам список НЕ логируется (ни здесь, ни у вызывателя; только число терминов).
    Модуль не работает с сырьём реплик и не пишет термины в долгоживущие файлы.

Чистые функции (`build_keyterms_prompt`, нормализация) тестируются напрямую без
сети и без YAML; сбор (`collect_keyterms_prompt`) best-effort — любой сбой
источника даёт деградацию (меньше терминов), но НЕ роняет расшифровку.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

from . import glossary, series_markup, series_roster

logger = logging.getLogger(__name__)


# Лимиты API AAI для keyterms_prompt (REQ AA1/S4). Совпадают с dealer360
# (`lib/dealer360/dictionary.ts::DICTIONARY_LIMITS`) — тот же провайдер/модель.
MAX_KEYTERMS = 1000           # universal-3-pro: до 1000 терминов
MAX_WORDS_PER_PHRASE = 6      # ≤6 слов/фраза; длиннее — отбрасываем целиком

# Каталог ручных нишевых терминов серии (опционально). На VPS лежит рядом с
# реестром встреч; env переопределяет (тесты указывают на временную папку).
DEFAULT_KEYTERMS_DIR = "/srv/meeting-notary/keyterms"


# --- Нормализация (общий шаг для фильтра и ключа дедупа) -----------------------

def _collapse_ws(s: str) -> str:
    """Схлопнуть любые пробелы/переводы строк и обрезать края. Регистр НЕ трогаем
    (каноника «Anzhee»/«КС-ТРЕЙД» значима для биаса ASR)."""
    return " ".join(str(s).split())


def _has_word_char(s: str) -> bool:
    """Термин обязан нести хотя бы одну букву/цифру — иначе «!!!»/«---» просочились
    бы мусором в keyterms_prompt. isalnum() unicode-aware (кириллица/латиница)."""
    return any(ch.isalnum() for ch in s)


def _word_count(term: str) -> int:
    """Число слов во фразе (по схлопнутым пробелам). Пустая → 0."""
    t = _collapse_ws(term)
    return len(t.split()) if t else 0


# --- Сборка keyterms_prompt с лимитами AAI (порт dealer360 buildKeytermsPrompt) -

def build_keyterms_prompt(raw_terms: Iterable[object]) -> list[str]:
    """Сырые термины → валидный `keyterms_prompt` (REQ S4).

    Шаги (порядок важен): схлопнуть пробелы → выкинуть пустые/без буквы-цифры →
    отбросить фразы >6 слов (целиком, не обрезая) → дедуп по нижнему регистру
    (первое вхождение оригинального регистра выигрывает) → обрезать до ≤1000.

    Чистая функция: вход — любой iterable строк (роли+глоссарий+ручные уже слиты);
    нестроковые элементы приводятся к строке (защитно). Пустой вход → []
    (вызыватель не кладёт поле в тело задания — поведение Ф1 неизменно)."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_terms or []:
        term = _collapse_ws(raw if isinstance(raw, str) else str(raw or ""))
        if not term or not _has_word_char(term):
            continue
        if _word_count(term) > MAX_WORDS_PER_PHRASE:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= MAX_KEYTERMS:
            break
    return out


# --- Ручные нишевые термины серии (опциональный файл) -------------------------

def keyterms_dir() -> Path:
    """Каталог ручных терминов серии (env `MEETING_NOTARY_KEYTERMS_DIR`, иначе VPS-дефолт)."""
    return Path(os.environ.get("MEETING_NOTARY_KEYTERMS_DIR") or DEFAULT_KEYTERMS_DIR).expanduser()


def manual_keyterms_for_series(series_slug: Optional[str]) -> list[str]:
    """Ручные нишевые термины серии из `<dir>/<slug>.txt` (один термин на строку).

    `#`-комментарии и пустые строки игнорируются. Нет каталога/файла/сбой чтения →
    [] (graceful, источник не обязателен — REQ S4 «предусмотреть, но не требовать»).
    Содержимое НЕ логируется (термины ниши, дисциплина приватности)."""
    if not series_slug or not str(series_slug).strip():
        return []
    slug = str(series_slug).strip()
    # Path-traversal guard: slug уходит в ИМЯ файла. Реальные slug'и системные
    # (`series-<...>-<hex>`). Любой разделитель пути / `..` / NUL отвергаем
    # (best-effort → [], как прочие сбои источника): иначе slug увёл бы чтение за
    # каталог keyterms, а абсолютный путь (ведущий `/`) — куда угодно (pathlib
    # `dir / "/abs"` сбрасывает на абсолютный).
    if any(c in slug for c in ("/", "\\", "\x00")) or ".." in slug:
        logger.info("[keyterms] slug серии не filename-safe, ручной файл пропущен")
        return []
    path = keyterms_dir() / f"{slug}.txt"
    try:
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.info("[keyterms] ручной файл серии не прочитан, пропуск: %s", e)
        return []
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


# --- Сбор словаря серии из всех источников ------------------------------------

def gather_series_terms(
    series_slug: Optional[str],
    *,
    company: Optional[str] = None,
    extra_terms: Optional[Iterable[str]] = None,
) -> list[str]:
    """Слить сырые термины серии из всех источников (БЕЗ лимитов/дедупа — их
    применяет `build_keyterms_prompt`). Порядок: имена ростера → глоссарий
    компании → ручные термины серии → явно переданные `extra_terms`.

    `company` можно передать готовым (hot-path/тесты); None → резолвим по серии
    через `series_markup.company_for_series` (разметка `watched.yaml` первична,
    оргструктура — fallback, как у глоссария в `llm_postprocess`). Каждый источник
    best-effort: сбой одного НЕ роняет остальные (деградация, не отказ)."""
    terms: list[str] = []

    # 1. Имена участников серии (ростер ролей).
    try:
        roster = series_roster.get_roster(series_slug)
        terms.extend(
            str(e.get("name") or "").strip()
            for e in roster
            if isinstance(e, dict) and str(e.get("name") or "").strip()
        )
    except Exception as e:  # noqa: BLE001 — best-effort источник
        logger.info("[keyterms] ростер серии недоступен (пропуск): %s", e)

    # Компания серии (для company-scoped глоссария).
    co = company
    if co is None:
        try:
            co = series_markup.company_for_series(series_slug)
        except Exception as e:  # noqa: BLE001
            logger.info("[keyterms] резолв компании серии упал (пропуск): %s", e)
            co = None

    # 2. Канонические бренды/термины компании из глоссария.
    try:
        terms.extend(glossary.glossary_keyterms(co))
    except Exception as e:  # noqa: BLE001
        logger.info("[keyterms] глоссарий компании недоступен (пропуск): %s", e)

    # 3. Ручные нишевые термины серии (опциональный файл).
    try:
        terms.extend(manual_keyterms_for_series(series_slug))
    except Exception as e:  # noqa: BLE001
        logger.info("[keyterms] ручные термины серии недоступны (пропуск): %s", e)

    # 4. Явно переданные термины (расширение для вызывателей/тестов).
    if extra_terms:
        terms.extend(str(t) for t in extra_terms if str(t or "").strip())

    return terms


def collect_keyterms_prompt(
    series_slug: Optional[str],
    *,
    company: Optional[str] = None,
    extra_terms: Optional[Iterable[str]] = None,
) -> list[str]:
    """Готовый `keyterms_prompt` для встречи серии: сбор всех источников + лимиты AAI.

    Точка входа для `finalize-meeting._run_assemblyai`. Best-effort на верхнем
    уровне: любой неожиданный сбой сбора → [] (расшифровка идёт без словаря,
    поведение Ф1). НЕ логирует сам список (РИСК2 — имена участников); число
    терминов логирует вызыватель."""
    try:
        raw = gather_series_terms(series_slug, company=company, extra_terms=extra_terms)
        return build_keyterms_prompt(raw)
    except Exception as e:  # noqa: BLE001 — никогда не роняем расшифровку из-за словаря
        logger.warning("[keyterms] сбор словаря серии упал (degradation): %s", e)
        return []
