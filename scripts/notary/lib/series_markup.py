"""Ф6 (E6): разметка серии — «компания» + «видимость» поверх `watched.yaml`.

До Ф6 компания серии резолвилась ПЕРЕХОДНО — поиском slug по оргструктурам всех
компаний (`context_knowledge.company_for_series`). Ф6 заводит ЯВНУЮ разметку в
реестре встреч (`watched.yaml`, поля `company`/`visibility`, см. `cli/registry.py`
и контракт Ф4 §1.5) и делает её ПЕРВИЧНЫМ источником, оставляя оргструктурный
поиск как fallback переходного периода (пока боевой `watched.yaml` не размечен).

Этот модуль — тонкая обёртка: грузит реестр best-effort и резолвит поля на уровень
series. Регистр (`watched.yaml`) — это КОНФИГ (на VPS лежит в
`/srv/meeting-notary/registry/`, env `MEETING_NOTARY_REGISTRY_DIR`), НЕ папка
владельца `me/` и НЕ знание `*-context`. Поэтому чтение разметки не возвращает
зависимость знания от `me/` (C4): знание (глоссарий/оргструктура) по-прежнему
течёт из `*-context`, разметка — из реестра, сырьё — из `me/встречи`.

Дисциплина (как `context_knowledge`, контракт §2): нет реестра / сбой импорта /
битый YAML → graceful (None), НЕ падаем. `watched` можно передать готовым
(инъекция для тестов и чтобы не грузить реестр дважды в hot-path).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import context_knowledge

logger = logging.getLogger(__name__)


def _load_watched_safe(watched: Optional[dict] = None) -> Optional[dict]:
    """Готовый `watched` (инъекция) или загрузка из реестра best-effort.

    Импорт `cli.registry` ленивый (как `llm_postprocess._load_watched_for_series`):
    прод-venv-cli/VPS несут PyYAML; при любом сбое → None (резолв уйдёт в fallback).
    """
    if watched is not None:
        return watched
    try:
        from notary.cli.registry import load_watched  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.info("[series-markup] cli.registry import failed (degradation): %s", e)
        return None
    try:
        return load_watched()
    except Exception as e:  # noqa: BLE001
        logger.info("[series-markup] load_watched failed (degradation): %s", e)
        return None


def company_for_series(series: Optional[str], *, watched: Optional[dict] = None) -> Optional[str]:
    """Компания серии: разметка `watched.yaml` ПЕРВИЧНА, оргструктура — fallback.

    1) поле `company` из разметки серии (`cli.registry.get_company_for_series`);
    2) переходный fallback — поиск slug по оргструктурам `*-context`
       (`context_knowledge.company_for_series`), пока боевой реестр не размечен;
    3) None → вызыватель (`glossary`) падает на встроенный блок (как до Ф5).
    """
    if not series or not str(series).strip():
        return None
    slug = str(series).strip()
    w = _load_watched_safe(watched)
    if w is not None:
        try:
            from notary.cli.registry import get_company_for_series as _reg_company  # noqa: PLC0415
            marked = _reg_company(slug, w)
            if marked:
                return marked
        except Exception as e:  # noqa: BLE001
            logger.info("[series-markup] company markup read failed (fallback): %s", e)
    # Переходный резолв по оргструктуре (Ф5). Развязка от него — когда боевой
    # watched.yaml размечен (follow-up Ф6-деплой/Ф8).
    return context_knowledge.company_for_series(slug)


def visibility_for_series(series: Optional[str], *, watched: Optional[dict] = None) -> Optional[str]:
    """Видимость серии из разметки `watched.yaml`. Нет разметки → None.

    None трактуется вызывателем (`publication_gate`) как НЕразмечено → private
    (fail-closed, E3). Оргструктурного fallback тут НЕТ намеренно: видимость —
    это РЕШЕНИЕ владельца о публикации, его нельзя «угадать» по составу. Нет явной
    пометки `company` → знание приватно.
    """
    if not series or not str(series).strip():
        return None
    w = _load_watched_safe(watched)
    if w is None:
        return None
    try:
        from notary.cli.registry import get_visibility_for_series as _reg_visibility  # noqa: PLC0415
        return _reg_visibility(str(series).strip(), w)
    except Exception as e:  # noqa: BLE001
        logger.info("[series-markup] visibility markup read failed (degradation): %s", e)
        return None


# ----- Ф5 (G4): жанр/повестка серии → фокус протокола генерации -----

# Мягкий дефолт для неразмеченной серии (A4: ключевые серии владелец метит
# вручную, остальным НЕ форсируем — даём наиболее частый рабочий случай).
# Большинство боевых серий — регулярные координации; «кто-что-когда» — безопасный
# нейтральный акцент, не ломающий ни директорат, ни продукт (инструкция мягкая).
DEFAULT_GENRE = "координация"

# Жанр → короткая инструкция ФОКУСА протокола. Формулировки нарочно МЯГКИЕ
# («на первый план», «сворачивай плотнее») — они смещают акцент, НЕ заменяют
# структуру/стандарт из методички (методичка едет в system-промпт целиком, жанр —
# дополнительный блок-данные в user-промпте). keys = `registry.VALID_GENRES`.
GENRE_FOCUS: dict[str, str] = {
    "директорат": (
        "Это встреча директората/правления. На первый план в протоколе выводи "
        "ПРИНЯТЫЕ РЕШЕНИЯ, РИСКИ и ЦИФРЫ (план/факт, отклонения, сроки, ответственных "
        "за риск). Решения и их основания фиксируй точно; обсуждение без решения "
        "сворачивай плотнее."
    ),
    "координация": (
        "Это рабочая координация. На первый план выводи «кто-что-когда»: ЗАДАЧИ с "
        "владельцем и сроком, статусы по зонам, что висит и что закрыто. Каждая "
        "прозвучавшая договорённость должна стать пунктом с ответственным."
    ),
    "продукт": (
        "Это продуктовая встреча. На первый план выводи ПРОДУКТОВЫЕ РЕШЕНИЯ и "
        "ГИПОТЕЗЫ (что проверяем и как поймём результат), приоритеты и зачем мы это "
        "делаем. Технические детали давай кратко, по сути."
    ),
    "1-на-1": (
        "Это встреча один-на-один. На первый план выводи договорённости и следующие "
        "шаги конкретного человека, ожидания и сроки. Это приватный разговор — без "
        "лишних обобщений на всю команду."
    ),
    "oneoff": (
        "Это разовая встреча вне регулярной серии. На первый план выводи, о чём "
        "договорились и что делаем дальше, без опоры на историю серии. Зафиксируй "
        "контекст так, чтобы протокол был понятен сам по себе."
    ),
}

# Жанр (ключ/токен watched.yaml) → человекочитаемый ярлык для ПРОМПТА. Токены
# латиницей («oneoff») удобны владельцу в YAML, но в русскоязычном промпте режут
# глаз и расходятся с методичкой («разовая»). Маппим только не-русские ключи; для
# остальных ярлык = сам ключ (цикл5/ход3, У3).
_GENRE_DISPLAY: dict[str, str] = {"oneoff": "разовая"}


def genre_for_series(series: Optional[str], *, watched: Optional[dict] = None) -> str:
    """Жанр серии: разметка `watched.yaml` ПЕРВИЧНА, иначе — мягкий дефолт.

    Зеркало `company_for_series`, но БЕЗ оргструктурного fallback и БЕЗ None на
    выходе: жанр всегда задаёт какой-то фокус, поэтому неразмеченная серия / битый
    реестр / сбой импорта → `DEFAULT_GENRE` (graceful, НЕ падаем — A4). Возвращаемое
    значение нормализовано (casefold-lower) и гарантированно есть в `GENRE_FOCUS`.
    """
    if not series or not str(series).strip():
        return DEFAULT_GENRE
    slug = str(series).strip()
    w = _load_watched_safe(watched)
    if w is not None:
        try:
            from notary.cli.registry import get_genre_for_series as _reg_genre  # noqa: PLC0415
            marked = _reg_genre(slug, w)
            if marked:
                return marked
        except Exception as e:  # noqa: BLE001
            logger.info("[series-markup] genre markup read failed (default): %s", e)
    return DEFAULT_GENRE


def format_genre_block(genre: Optional[str]) -> str:
    """Ф5 (G4): блок «тип/повестка встречи» для ПРОМПТА ГЕНЕРАЦИИ протокола.

    Короткая инструкция фокуса под жанр серии (см. `GENRE_FOCUS`). Неизвестный/
    пустой жанр → "" (блок не добавляется, поведение генерации не меняется). Текст
    не персональный (опасная тройка) — это статичная инструкция фокуса, не данные
    встречи; в лог не идёт (G10).
    """
    key = (genre or "").strip().casefold()
    focus = GENRE_FOCUS.get(key)
    if not focus:
        return ""
    label = _GENRE_DISPLAY.get(key, key)
    return f"Тип (повестка) этой встречи: {label}.\n{focus}"


def markup_for_series(
    series: Optional[str], *, watched: Optional[dict] = None
) -> tuple[Optional[str], Optional[str]]:
    """`(company, visibility)` одним чтением реестра (для hot-path: один load_watched).

    Грузит `watched` один раз и резолвит оба поля — экономит повторную загрузку
    реестра при вызове из `publication_gate.decide_for_meeting`.
    """
    w = _load_watched_safe(watched)
    company = company_for_series(series, watched=w) if series else None
    visibility = visibility_for_series(series, watched=w) if series else None
    return company, visibility
