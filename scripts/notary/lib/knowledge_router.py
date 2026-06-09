"""Ф7 D3: safety-классификатор слоя назначения для авто-выученного ЗНАНИЯ.

Один вопрос: куда положить факт знания (термин / роль) —
  • `DROP`    — это креды (D5): не сохранять НИКУДА;
  • `COMPANY` — в `*-context` нужной компании (общий мозг команды);
  • `PRIVATE` — в `me/` (приватно владельцу).

Порядок решения (fail-closed, контракт Ф4 §7 + публикационный гейт Ф6):
  1. D5  — креды → DROP (раньше всего, чтобы секрет не утёк даже логикой ниже);
  2. D4  — владелец ЗАПОМНИЛ ратчет «такие — в контекст» → COMPANY;
  3. E1  — публикационный гейт Ф6 явно РАЗРЕШИЛ (групповая размеченной компании,
           знакомый состав) → COMPANY;
  4. D3  — иначе ПРИ СОМНЕНИИ → PRIVATE (`me/`). Это дефолт: неклассифицированный,
           1-на-1, неразмеченная серия, неизвестная компания — всё приватно.

Почему так: ошибка «ложно в COMPANY» = приватное знание необратимо легло в
git-историю команды; ошибка «ложно в PRIVATE» = владелец потом перенесёт командой
(D4). Цена несимметрична → дефолт приватный (D3).

Чистая функция `classify_destination` (без IO) — основной объект тестов. IO-обёртка
`route_for_meeting` подтягивает компанию/публикационный вердикт из реального
контекста встречи. Креды-фильтр и ратчет инъектируются (тесты/изоляция).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import cred_filter
from . import knowledge_ratchet

logger = logging.getLogger(__name__)

LAYER_DROP = "drop"
LAYER_PRIVATE = "private"
LAYER_COMPANY = "company"


@dataclass(frozen=True)
class Destination:
    """Решение роутера. `company` непуст только для LAYER_COMPANY."""

    layer: str
    company: Optional[str]
    reason: str

    @property
    def is_drop(self) -> bool:
        return self.layer == LAYER_DROP

    @property
    def is_company(self) -> bool:
        return self.layer == LAYER_COMPANY

    @property
    def is_private(self) -> bool:
        return self.layer == LAYER_PRIVATE

    def as_metadata(self) -> dict:
        return {"layer": self.layer, "company": self.company, "reason": self.reason}


def _drop(reason: str) -> Destination:
    return Destination(LAYER_DROP, None, reason)


def _private(reason: str) -> Destination:
    return Destination(LAYER_PRIVATE, None, reason)


def _company(company: str, reason: str) -> Destination:
    return Destination(LAYER_COMPANY, company, reason)


def classify_destination(
    value: str,
    *,
    kind: str,
    company: Optional[str] = None,
    publication_allowed: bool = False,
    ratchet=knowledge_ratchet,
    creds=cred_filter,
) -> Destination:
    """Куда положить факт `value` рода `kind` (см. модульный докстринг).

    value — конкретная единица знания (термин или «имя | домен» для роли). kind —
    `term` | `roster-role`. company — компания встречи (или None, если не
    резолвится). publication_allowed — вердикт Ф6-гейта `decide_*().allowed`.
    """
    # 1) D5 — креды → никуда. Раньше всех веток.
    if creds.looks_like_secret(value):
        return _drop("credential")

    comp = (str(company).strip().lower() if company else "")

    # 2) D4 — запомненный ратчет владельца (private→company). Нужна известная компания.
    if comp:
        try:
            if ratchet.should_promote(kind, company=comp, key=value):
                return _company(comp, "ratchet-promoted")
        except Exception as e:  # noqa: BLE001 — ратчет вторичен, сбой не должен ронять
            logger.warning("[router] ratchet check failed (non-fatal): %s", e)

    # 3) E1 — публикационный гейт Ф6 явно разрешил публикацию знания этой встречи.
    if comp and publication_allowed:
        return _company(comp, "publication-allowed")

    # 4) D3 — дефолт при сомнении: приватно (me/). Сюда падает всё неоднозначное.
    if not comp:
        return _private("default-private-no-company")
    return _private("default-private")


def route_for_meeting(
    value: str,
    *,
    kind: str,
    series: Optional[str],
    present_participants: Optional[list] = None,
    watched: Optional[dict] = None,
) -> Destination:
    """IO-обёртка: резолвит компанию серии + публикационный вердикт Ф6 и зовёт
    `classify_destination`. Best-effort: любой сбой загрузки → как «нет данных» →
    дефолт приватный (D3, fail-closed). Зовётся из applier (D1) и feedback-router (D6).
    """
    company: Optional[str] = None
    publication_allowed = False
    try:
        from . import series_markup  # noqa: PLC0415
        company = series_markup.company_for_series(series, watched=watched)
    except Exception as e:  # noqa: BLE001
        logger.info("[router] company resolve failed (%s) → fail-closed private", e)
    if company and present_participants is not None:
        try:
            from . import publication_gate  # noqa: PLC0415
            verdict = publication_gate.decide_for_meeting(
                series, present_participants, watched=watched,
            )
            publication_allowed = bool(verdict.allowed)
        except Exception as e:  # noqa: BLE001
            logger.info("[router] publication gate failed (%s) → not allowed", e)
            publication_allowed = False
    return classify_destination(
        value, kind=kind, company=company, publication_allowed=publication_allowed,
    )
