"""Ф3: ростер ролей серии (человек → зона ответственности) для доменного маппинга.

На регулярной координации каждый участник стабильно отвечает за свой блок
(поставки, коммерция, сервис, резервы, финансы). Реплики по домену X почти всегда
говорит человек с зоной ответственности X — это надёжный, НЕвероятностный сигнал
авторства, в отличие от vocative/LLM-добивки (РИСК4: LLM-маппинг вероятностен,
ростер — основная опора, якорь авторства вторичен).

Модуль даёт:
  - `get_roster(series_slug)` — ростер серии (список зон с каноничным именем
    ответственного + доменными ключевыми словами);
  - `format_roster_hint(roster)` — текстовый блок-подсказка для LLM-маппинга
    (домен реплики ↔ роль автора), подаётся в `llm_postprocess.map_speaker_names`;
  - `domain_for_name(roster, name)` — обратный индекс имя→домен (для тестов/логов).

Детерминированный матчер «реплики по домену → ответственный» живёт в
`name_mapping.map_from_roster_domain` (использует поле `keywords` отсюда).

⚠️ ЗАВ1 / Ф5 — ЕДИНСТВЕННАЯ ТОЧКА РАСШИРЕНИЯ.
Сейчас ростер ХАРДКОД для Anzhee-координации (разбор владельца 2026-06-09). В Ф5
`get_roster` переключается на чтение оргструктуры из `*-context` нужной компании
(тогда и МПервый-координация получит ростер). Менять надо ТОЛЬКО тело `get_roster`
(источник данных), сигнатуры/контракт остаются — `map_from_roster_domain`,
`format_roster_hint` и тесты от смены источника не зависят. НЕ переключать здесь и
сейчас (иначе МПервый-координация без ростера, а хардкод останется навсегда).

Дисциплина «Опасной тройки» (CLAUDE.md проекта): keywords — доменная лексика, не
ПДн; имена ответственных и так фигурируют в составе участников. Текст реплик сюда
не попадает и не логируется.
"""
from __future__ import annotations

from typing import Optional, TypedDict

from . import context_knowledge


class RosterEntry(TypedDict):
    """Одна зона ответственности серии."""

    name: str           # каноничное имя ответственного («Мария Михина»)
    domain: str         # человекочитаемый ярлык зоны («поставки»)
    keywords: list[str]  # доменные стемы (lowercase, substring-матч по реплике)


# Slug серии еженедельной координации Anzhee (тот же, что папка в me/встречи и
# `series-display.json`). Держим как именованную константу — ЗАВ1/Ф5 заменит
# источник, slug-ключ останется единственным стабильным идентификатором серии.
ANZHEE_COORDINATION_SLUG = "series-ezhenedelnaya-koordinaciya-8399ea"


# Хардкод-ростер Anzhee-координации (разбор владельца 2026-06-09). Раскладка зон ↔
# ответственных — критерий приёмки A4: поставки→Мария Михина, коммерция→Сона
# Енгибарян, сервис→Михаил Саргин, резервы→Дарья Набережная, финансы→Ольга Новикова.
# keywords подобраны по доменной лексике реальных координаций (02.06/09.06): стемы,
# которые в этом домене звучат, а в чужом — почти нет (для высокоточного матча).
_ANZHEE_COORDINATION_ROSTER: list[RosterEntry] = [
    {
        "name": "Мария Михина",
        "domain": "поставки",
        "keywords": [
            "поставк", "контейнер", "ктк", "отгруз", "таможн", "досмотр",
            "декларац", "склад агент", "перевозчик", "логист", "закуп", "груз",
        ],
    },
    {
        "name": "Сона Енгибарян",
        "domain": "коммерция",
        "keywords": [
            "продаж", "выручк", "оффер", "акб", "лид", "клиент", "тендер",
            "контракт", "коммерч", "воронк", "регистрац", "сделк", "оборот",
        ],
    },
    {
        "name": "Михаил Саргин",
        "domain": "сервис",
        "keywords": [
            "сервис", "обслуживан", "ремонт", "гаранти", "ателье", "рекламац",
            "монтаж", "установк", "поддержк", "техник",
        ],
    },
    {
        "name": "Дарья Набережная",
        "domain": "резервы",
        "keywords": [
            "резерв", "остатк", "оборачиваемост", "запас", "неликвид",
            "срез по резерв", "товарный запас",
        ],
    },
    {
        "name": "Ольга Новикова",
        "domain": "финансы",
        "keywords": [
            "финанс", "платёж", "платеж", "оплат", "юан", "кредит", "ндфл",
            "ндс", "ковенант", "бюджет", "тенге", "касс", "задолженност",
        ],
    },
]


# Реестр ростеров по slug серии. ЗАВ1/Ф5: тело `get_roster` подменит этот источник
# на чтение из `*-context`; здесь — стартовый хардкод одной серии Anzhee.
_STATIC_ROSTERS: dict[str, list[RosterEntry]] = {
    ANZHEE_COORDINATION_SLUG: _ANZHEE_COORDINATION_ROSTER,
}


def get_roster(series_slug: Optional[str]) -> list[RosterEntry]:
    """Ростер ролей серии по её slug. Нет ростера для серии → `[]`.

    ⚠️ ЗАВ1/Ф5 ВЫПОЛНЕНО — изменено ТОЛЬКО тело (источник данных). Сигнатура и
    потребители (`map_from_roster_domain`, `format_roster_hint`, finalize-проводка,
    тесты) не тронуты.

    Источник теперь — оргструктура компании из `*-context`
    (`context_knowledge.roster_for_series`, поиск slug по оргструктурам всех
    компаний — теперь и МПервый-координация получает ростер, как только её
    `org-structure.yaml` появится). Нет YAML-знания (клон `*-context` не
    забутстраплен — control/Ф8) → **fallback на встроенный `_STATIC_ROSTERS`**
    (graceful degradation, поведение как в Ф3). Anzhee-ростер вынесен в
    `anzhee-context/.../org-structure.yaml`; `_STATIC_ROSTERS` остаётся как
    переходный fallback, не как источник истины.

    Пустой/None slug или незнакомая серия → `[]` (доменного маппинга не будет,
    остаются якорь+S1+S2+LLM — поведение как до Ф3).
    """
    if not series_slug or not str(series_slug).strip():
        return []
    slug = str(series_slug).strip()
    from_context = context_knowledge.roster_for_series(slug)
    if from_context:
        return from_context  # type: ignore[return-value]
    return _STATIC_ROSTERS.get(slug, [])


def domain_for_name(roster: list[RosterEntry], name: str) -> Optional[str]:
    """Обратный индекс: каноничное имя → его домен. Нет в ростере → None.

    Матч по полному имени и по первому слову (состав может записываться полным/
    коротким именем), но первое слово мирит тёзок ТОЛЬКО когда кто-то записан одним
    словом — два разных полных тёзки доменом не путаем. Для тестов и structured-логов.
    """
    if not roster or not name:
        return None
    target = name.strip().lower()
    t_toks = target.split()
    target_first = t_toks[0] if t_toks else target
    for entry in roster:
        cand = entry.get("name", "").strip().lower()
        if not cand:
            continue
        c_toks = cand.split()
        cand_first = c_toks[0] if c_toks else cand
        if target == cand:
            return entry.get("domain")
        if target_first == cand_first and (len(t_toks) <= 1 or len(c_toks) <= 1):
            return entry.get("domain")
    return None


def _name_in_names(name: str, names: Optional[list[str]]) -> bool:
    """Тёзко-безопасное присутствие имени в списке (та же логика, что
    `name_mapping._roster_name_in_pool`): первое слово мирит тёзок ТОЛЬКО когда
    кто-то записан одним словом; два разных полных тёзки — разные люди.
    """
    target = (name or "").strip().lower()
    if not target:
        return False
    t_toks = target.split()
    target_first = t_toks[0] if t_toks else target
    for p in names or []:
        pn = (p or "").strip().lower()
        if not pn:
            continue
        p_toks = pn.split()
        pn_first = p_toks[0] if p_toks else pn
        if pn == target:
            return True
        if pn_first == target_first and (len(t_toks) <= 1 or len(p_toks) <= 1):
            return True
    return False


def filter_roster_to_present(
    roster: list[RosterEntry], present_names: Optional[list[str]]
) -> list[RosterEntry]:
    """A5: оставить записи ростера, чей владелец РЕАЛЬНО присутствует.

    Нужна для LLM-подсказки: подавать в промпт домен→владелец только для
    присутствующих, иначе хинт подталкивает Claude подставить отсутствующего
    владельца домена (обход «нет голоса — нет имени»). `present_names` пуст/None →
    `[]` (нет сигнала присутствия → не подсказываем владельцев вовсе).
    """
    if not roster or not present_names:
        return []
    return [e for e in roster if _name_in_names(str(e.get("name") or ""), present_names)]


def format_roles_block(roster: list[RosterEntry]) -> str:
    """Ф3 (G2): блок «роли участников» для ПРОМПТА ГЕНЕРАЦИИ протокола.

    В отличие от `format_roster_hint` (подсказка авторства для маппинга спикеров,
    с доменными keywords) — это компактный список «имя → зона ответственности».
    Цель в синтезе протокола: модель привязывает задачи/решения к верным людям и
    не смешивает роли. keywords сюда НЕ кладём — это лексика ASR-маппинга, в
    синтезе протокола она шум. Пустой ростер → "" (блок не добавляется, поведение
    генерации не меняется).

    Без сырого текста реплик (опасная тройка) — только имена и ярлык зоны; сам
    блок не логируется (G10), кладётся только в промпт генерации.
    """
    if not roster:
        return ""
    lines = [
        "Роли участников этой серии (кто за какую зону отвечает). Привязывай "
        "задачи и решения к верным людям по зоне ответственности — не смешивай "
        "роли и не приписывай задачу не тому участнику:",
    ]
    for entry in roster:
        name = entry.get("name", "").strip()
        domain = entry.get("domain", "").strip()
        if not name or not domain:
            continue
        lines.append(f"- {name} — {domain}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def format_roster_hint(roster: list[RosterEntry]) -> str:
    """Блок-подсказка для LLM-маппинга: зона ответственности → ответственный.

    Подаётся в промпт `map_speaker_names` как ДОМЕННАЯ ПРОВЕРКА (B3): реплики по
    теме X почти всегда говорит ответственный за X. Пустой ростер → "" (блок не
    добавляется, поведение промпта не меняется).

    Без сырого текста реплик (опасная тройка) — только роли и доменная лексика.
    """
    if not roster:
        return ""
    lines = [
        "Ростер ролей этой серии (зона ответственности → кто за неё отвечает). "
        "Реплики по теме почти всегда говорит ответственный за эту тему — "
        "используй это как сильную подсказку авторства:",
    ]
    for entry in roster:
        name = entry.get("name", "").strip()
        domain = entry.get("domain", "").strip()
        kws = [k for k in (entry.get("keywords") or []) if k][:6]
        if not name or not domain:
            continue
        tail = f" (по словам: {', '.join(kws)})" if kws else ""
        lines.append(f"- {domain} → {name}{tail}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
