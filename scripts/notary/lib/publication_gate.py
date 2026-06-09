"""Ф6 (E1–E5): гейтинг публикации производного ЗНАНИЯ в общий мозг компании.

Решает ОДИН вопрос: можно ли авто-выученное знание этой встречи опубликовать в
`*-context` (читают Михаил/Татьяна), или оно должно остаться приватным (`me/`).
Сама публикация — Ф7; Ф6 даёт ГЕЙТ (детерминированное решение) + проводку в
реальный путь финализации (вердикт оседает в памяти серии как метаданные, без
сырья — `series_memory`). Сырьё (транскрипт/протокол) в `*-context` НЕ течёт
никогда (РИСК1) — гейт только про ЗНАНИЕ.

═══ ПРИНЦИП: FAIL-CLOSED ═══
Гейт — это защита приватности. Ошибка «ложно-разрешил» = приватное знание утекло
в общий мозг (необратимо: ляжет в git-историю команды). Ошибка «ложно-запретил»
= знание осталось приватным (владелец потом переносит вручную, D4/Ф7). Поэтому
ПРИ ЛЮБОМ СОМНЕНИИ → private. Все ветки, кроме явной «групповая координация
размеченной компании со знакомым составом», возвращают private.

═══ ПРАВИЛА (порядок — для диагностики; исход любой не-ok ветки = private) ═══
E4  владелец + РОВНО один участник → private «one-on-one» (ПЕРЕБИВАЕТ разметку:
    «независимо от разметки», критерий E4). 1-на-1 знание — приватно (E2).
—   состав не похож на группу (нет владельца И <2 не-владельцев, либо вообще
    <2 не-владельцев) → private «no-group». Группа = ≥2 не-владельца.
E2/E3 видимость серии != `company` (private / НЕразмечено) → private.
E6  компания серии не распознана (нет репо-цели) → private «no-company».
E5  ПРЕДОХРАНИТЕЛЬ «осторожно добавлять»: каждый присутствовавший должен быть
    внутри КРУГА, которому это знание и так доступно. Кто-то снаружи → private
    «outside-circle» («состав шире круга читателей»).
ok  размеченная-`company` групповая встреча знакомого состава → ПУБЛИКУЕМА (E1).

═══ E5: круг и почему он шире буквальных {Михаил, Татьяна} ═══
Буквально «круг читателей `*-context`» = Михаил + Татьяна (ВОПР1, инвариант — мы
НЕ заводим других ЧИТАТЕЛЕЙ контекста). Но E1 требует, чтобы знание ГРУППОВОЙ
координации (Мария/Сона/Саргин/… — НЕ Михаил/Татьяна) МОГЛО публиковаться, а
владелец это прямо санкционировал (Доп.3: «Михаил/Татьяна видят все цифры
координаций»). Значит E5 НЕ может означать «каждый участник обязан быть
Михаилом/Татьяной» — иначе E1 мёртв.

Примирение E1∧E5: пометив серию `company`, владелец АВТОРИЗОВАЛ её штатный состав
(ростер) на публикацию в общий мозг компании. Поэтому «круг, которому знание
доступно» для встречи = владелец ∪ читатели(Михаил/Татьяна) ∪ РОСТЕР этой серии
(оргструктура `*-context`). E5 — предохранитель на НЕОЖИДАННОГО участника СВЕРХ
этого круга (внешний гость/контрагент): его присутствие → «состав шире круга» →
private. Это и есть «осторожно добавлять».

  • Ростер пуст (стаб МПервый — данных нет) → круг = {владелец, читатели}, и любая
    реальная групповая встреча с не-владельцами → private (fail-closed). Это
    НАМЕРЕННО: нет данных о круге → не публикуем (правило репо «не выдумывай
    факты»). Появится `org-structure.yaml` с ростером — публикация откроется сама.
  • Это НЕ расширяет круг ЧИТАТЕЛЕЙ контекста (он = Михаил/Татьяна, инвариант) —
    ростер расширяет лишь «кого ждём в санкционированной встрече».

Сопоставление имён круга — тёзко-безопасное (как `series_roster`/`name_mapping`):
полное совпадение ИЛИ совпадение по первому слову, только когда одна из сторон
записана одним словом (двух разных полных тёзок не путаем). Остаточный риск: бэйр
«Михаил» в круге матчит любого «Михаил X» — узкий хвост, такой X почти всегда тоже
внутренний; точные личности читателей владелец фиксирует на control-чек-поинте Ф6.

═══ Опасная тройка (CLAUDE.md проекта) ═══
Гейт оперирует ТОЛЬКО метаданными: имена участников (уже в составе встречи) +
флаги. Сырьё реплик сюда не попадает. Вердикт (`as_metadata`) — компактный, без
ПДн сверх уже хранимого в памяти серии (имена там и так есть). Тексты не логируем.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Владелец присутствует на каждой встрече (тот же дефолт и env, что в
# series_memory — конфиг один раз). Детект владельца — по подстроке (надёжно
# поймать 1-на-1: лучше лишний раз private).
_DEFAULT_OWNER_TOKENS = ("илья", "рыбалка", "ilya", "rybalka")

# Читатели `*-context` (ВОПР1). Точные личности (people.md показывает
# неоднозначность: какой Михаил/какая Татьяна) владелец фиксирует на
# control-чек-поинте Ф6 / провижининге; здесь — безопасный дефолт, переопределяем
# env `NOTARY_CONTEXT_READERS` (через запятую).
_DEFAULT_READERS = ("Михаил", "Татьяна")

VISIBILITY_COMPANY = "company"
VISIBILITY_PRIVATE = "private"


@dataclass(frozen=True)
class PublicationDecision:
    """Вердикт гейта. `allowed` — можно ли знание в `*-context`."""

    allowed: bool
    visibility: str            # company | private (итоговая, не разметка)
    company: Optional[str]     # репо-цель, если allowed; иначе None
    reason: str                # машинный код причины (для логов/метаданных/тестов)

    def as_metadata(self) -> dict:
        """Компактный PII-free вердикт для памяти серии / логов (без сырья)."""
        return {
            "allowed": self.allowed,
            "visibility": self.visibility,
            "company": self.company,
            "reason": self.reason,
        }


def _private(reason: str) -> PublicationDecision:
    return PublicationDecision(False, VISIBILITY_PRIVATE, None, reason)


def _owner_tokens() -> tuple[str, ...]:
    raw = (os.environ.get("SERIES_MEMORY_OWNER_NAMES") or "").strip()
    if not raw:
        return _DEFAULT_OWNER_TOKENS
    toks = tuple(t.strip().lower() for t in raw.split(",") if t.strip())
    return toks or _DEFAULT_OWNER_TOKENS


def _reader_names() -> list[str]:
    raw = (os.environ.get("NOTARY_CONTEXT_READERS") or "").strip()
    if not raw:
        return list(_DEFAULT_READERS)
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or list(_DEFAULT_READERS)


def _is_owner(name: str, owner_tokens: tuple[str, ...]) -> bool:
    norm = (name or "").strip().lower()
    return bool(norm) and any(tok in norm for tok in owner_tokens)


def _name_matches_any(name: str, circle: list[str]) -> bool:
    """Тёзко-безопасное присутствие имени в круге (та же логика, что
    `series_roster._name_in_names` / `name_mapping._roster_name_in_pool`): полное
    совпадение ИЛИ совпадение по первому слову, только если одна из сторон записана
    одним словом. Два разных полных тёзки — разные люди (не матчим).
    """
    target = (name or "").strip().lower()
    if not target:
        return False
    t_toks = target.split()
    target_first = t_toks[0] if t_toks else target
    for c in circle:
        cn = (c or "").strip().lower()
        if not cn:
            continue
        c_toks = cn.split()
        cn_first = c_toks[0] if c_toks else cn
        if cn == target:
            return True
        if cn_first == target_first and (len(t_toks) <= 1 or len(c_toks) <= 1):
            return True
    return False


def _known_companies() -> set[str]:
    """Множество известных компаний (репо-цели). Best-effort из context_knowledge,
    с жёстким дефолтом — гейт не должен падать из-за импорта."""
    try:
        from . import context_knowledge  # noqa: PLC0415
        comps = {c.strip().lower() for c in context_knowledge.known_companies() if c}
        if comps:
            return comps
    except Exception as e:  # noqa: BLE001
        logger.info("[publication] known_companies fallback (%s)", e)
    return {"anzhee", "mpfirst"}


def decide_publication(
    series: Optional[str],
    present_participants: Optional[list[str]],
    *,
    visibility: Optional[str],
    company: Optional[str],
    roster_names: Optional[list[str]] = None,
    reader_names: Optional[list[str]] = None,
    owner_tokens: Optional[tuple[str, ...]] = None,
) -> PublicationDecision:
    """Чистое решение гейта (без IO) — основной объект unit-тестов.

    Все входы явные → детерминированно и полностью тестируемо на наборе серий
    (групповая / 1-на-1 / неразмеченная / внешний-гость). IO-обёртка, грузящая
    разметку/ростер, — `decide_for_meeting`.

    present_participants — РЕАЛЬНО присутствовавшие (панель Телемоста ∪ голоса,
    `resolve_present_participants`/шапка), НЕ приглашённые (A5). Это критично для
    E4/E5: гейт судит по факту присутствия.
    """
    owners = owner_tokens if owner_tokens is not None else _owner_tokens()
    readers = reader_names if reader_names is not None else _reader_names()
    present = [str(p).strip() for p in (present_participants or []) if str(p).strip()]

    # Владельца засчитываем РОВНО ОДИН раз: первое совпадение по owner-токенам —
    # это владелец, ПОСЛЕДУЮЩИЕ совпадения (другой человек с тем же именем — ещё
    # один «Илья», однофамилец-аутсайдер) остаются НЕ-владельцами и проходят
    # предохранитель круга E5. Иначе тёзка-аутсайдер владельца минул бы E5 (его
    # подстрочный owner-матч исключил бы его из non_owner) → fail-OPEN в fail-closed
    # гейте. Владелец на встрече физически один — этот инвариант здесь и кодируем.
    owner_present = False
    non_owner: list[str] = []
    for p in present:
        if not owner_present and _is_owner(p, owners):
            owner_present = True
            continue
        non_owner.append(p)

    # E4: владелец + РОВНО один → 1-на-1, принудительно private, ПЕРЕБИВАЕТ разметку.
    if owner_present and len(non_owner) == 1:
        return _private("one-on-one")
    # Не группа: <2 не-владельцев (соло / владелец-один-без-других / один не-владелец).
    # Группа требует ≥2 не-владельцев (групповая координация). Fail-closed.
    if len(non_owner) < 2:
        return _private("no-group")

    # E2/E3: видимость. Только явная `company` проходит. private / НЕразмечено → private.
    vis = (visibility or "").strip().lower()
    if vis != VISIBILITY_COMPANY:
        return _private("private-markup" if vis == VISIBILITY_PRIVATE else "unmarked")

    # E6: компания должна резолвиться в репо-цель.
    comp = (company or "").strip().lower()
    if comp not in _known_companies():
        return _private("no-company")

    # E5: предохранитель круга. Круг = владелец ∪ читатели ∪ ростер серии.
    # Кто-то из присутствовавших вне круга → «состав шире круга» → private.
    circle = [r for r in (roster_names or []) if r and str(r).strip()] + list(readers)
    outside = [p for p in non_owner if not _name_matches_any(p, circle)]
    if outside:
        # НЕ логируем имена (опасная тройка) — только счётчик.
        logger.info("[publication] series=%s outside-circle present=%d outside=%d → private",
                    series or "?", len(present), len(outside))
        return _private("outside-circle")

    # E1: размеченная-company групповая встреча знакомого состава → ПУБЛИКУЕМА.
    return PublicationDecision(True, VISIBILITY_COMPANY, comp, "ok-group")


def decide_for_meeting(
    series: Optional[str],
    present_participants: Optional[list[str]],
    *,
    watched: Optional[dict] = None,
) -> PublicationDecision:
    """IO-обёртка: грузит разметку (company/visibility) + ростер серии и зовёт
    `decide_publication`. Зовётся из реального пути финализации (finalize-meeting).

    Best-effort: любой сбой загрузки разметки/ростера → fail-closed (исключение
    наружу не пускаем — caller тоже подстрахован, но гейт сам default'ит в private).
    """
    try:
        from . import series_markup, series_roster  # noqa: PLC0415
        company, visibility = series_markup.markup_for_series(series, watched=watched)
        roster = series_roster.get_roster(series)
        roster_names = [str(e.get("name")).strip() for e in (roster or []) if e.get("name")]
    except Exception as e:  # noqa: BLE001
        logger.warning("[publication] markup/roster load failed (%s) → fail-closed private", e)
        return _private("gate-error")
    return decide_publication(
        series, present_participants,
        visibility=visibility, company=company, roster_names=roster_names,
    )


def present_participants_for_gate(
    expected: Optional[list[str]],
    panel: Optional[list[str]],
    voiced: Optional[list[str]],
) -> list[str]:
    """A5: состав, по которому СУДИТ гейт = панель Телемоста ∪ реально говорившие
    (тот же набор, что и шапка протокола — `resolve_present_participants`), а НЕ
    только панель.

    Иначе озвучившийся, но не попавший в панель участник (аудио-only / промах
    скрейпа панели / две персоны на одном коннекте) виден в ПРОТОКОЛЕ, но НЕВИДИМ
    предохранителю круга E5 → если он внешний, знание встречи опубликуется →
    fail-OPEN в fail-closed гейте. Контракт «панель ∪ голоса» зафиксирован во
    входной строке `decide_publication`; эта обёртка — ЕДИНЫЙ источник истины «кто
    присутствовал» вместе с шапкой протокола.

    Деградация (сбой импорта `protocol_to_tg`): отдаём хотя бы панель ∪ голоса без
    тёзко-дедупа — гейт сам тёзко-safe (`_name_matches_any`), дубли безвредны.
    """
    try:
        from .protocol_to_tg import resolve_present_participants  # noqa: PLC0415
        return resolve_present_participants(expected or [], panel or [], voiced or [])
    except Exception as e:  # noqa: BLE001
        logger.info("[publication] present-resolve fallback (%s)", e)
        out = [str(p).strip() for p in (panel or []) if str(p).strip()]
        out.extend(str(v).strip() for v in (voiced or []) if str(v).strip())
        return out
