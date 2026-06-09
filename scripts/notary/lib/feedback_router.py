"""Ф7 D6: маршрутизатор фидбэка участников ПО СЛОЯМ (на базе петли перевыпуска).

Когда участник правит протокол реплаем, правка — не только «перевыпустить этот
протокол», но и «куда положить ЗНАНИЕ из правки, чтобы впредь не объяснять дважды».
D6 классифицирует КАЖДУЮ правку по ТИПУ и направляет в СВОЙ слой:

  • РОЛЬ («за поставки отвечает Мария», «Саргин ведёт сервис») → оргструктура
    компании (`*-context`) или приватно — через `knowledge_writeback`/`knowledge_router`
    (слой решает D3/D4/D5);
  • ТЕРМИН/СМЫСЛ СЕРИИ («не РСЯ, а РЕЦ», «X — это Y») → карточка серии — это уже
    делает `feedback_learning.record_learning_from_edits` (вызывается рядом в
    `feedback_reissue`); роутер их НЕ дублирует, лишь относит к слою «series-card»;
  • ИМЯ/ФОРМАТ («заголовок — Координация»; «убери раздел рисков», «суммы таблицей»)
    → конфиг/шаблон: формат → `protocol_template` (новая версия, F2), имя/шапка →
    очередь конфиг-предложений;
  • КОНТЕНТ (разовая правка тела) → НЕ обобщаем (учится только перевыпуск).

🔴 Инварианты (промт Ф7): сырьё НЕ переезжает; delivered-маркеры перевыпуска НЕ
трогаются; креды (D5) отбрасываются ДО любого слоя. Роутер только ЧИТАЕТ
state/edits и пишет в outbox/шаблон — feedback-state и доставку не трогает.

Чистые `classify_edit` / `extract_role` — основной объект тестов; `route_edits` —
best-effort диспетчер, зовётся из `feedback_reissue.reissue_one` рядом с обучением.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import cred_filter

logger = logging.getLogger(__name__)

KIND_ROLE = "role"
KIND_SERIES_TERM = "series-term"
KIND_NAME_FORMAT = "name-format"
KIND_CONTENT = "content"

# ── Классификатор типа правки (чистый) ───────────────────────────────────────

# Роль: глагол/существительное ответственности рядом с доменом/именем.
_ROLE_RE = re.compile(
    r"\b(?:отвеча\w+|ответствен\w+|вед[её]т|кури\w+|занима\w+ся|на\s+н[её]м|на\s+ней|"
    r"зона\s+ответственн\w+|в\s+зоне|закреп\w+\s+за|это\s+зона)\b",
    re.IGNORECASE,
)

# Формат/структура оформления.
_FORMAT_RE = re.compile(
    r"\b(?:формат\w*|шаблон\w*|структур\w*|раздел\w*|секци\w*|порядок\s+раздел\w*|"
    r"оформл\w*|табли[цч]\w*|пункт\w*|блок\w*|поменяй\s+местами|"
    r"(?:сделай|пиши|резюме)\s+(?:коротк\w+|короче|подробн\w+|длинн\w+)|"
    r"слишком\s+(?:длинн\w+|коротк\w+|подробн\w+)|убери\s+(?:раздел|блок|пункт)|"
    r"добав[ьи]\w*\s+(?:раздел|блок|пункт)|не\s+нужен\s+раздел)\b",
    re.IGNORECASE,
)

# Имя/заголовок/шапка/состав.
_NAME_HEADER_RE = re.compile(
    r"\b(?:заголов\w+|назван\w+\s+(?:встреч\w+|сери\w+)|в\s+участник\w+|из\s+участник\w+|"
    r"в\s+шапк\w+|шапк\w+\s+протокол\w*|это\s+зовут|зовут\s+[А-ЯЁ])\b",
    re.IGNORECASE,
)


def classify_edit(text: object) -> str:
    """Тип правки: role | name-format | series-term | content (приоритет в этом
    порядке). Чистая функция — единица тестов маршрутизации.

    series-term определяется делегированием в `feedback_learning` (терм-пара ИЛИ
    смысловое правило); если оно ничего не извлекло и нет других сигналов → content.
    """
    if not isinstance(text, str) or not text.strip():
        return KIND_CONTENT
    s = text

    if _ROLE_RE.search(s) and _extract_role_pure(s) is not None:
        return KIND_ROLE
    if _FORMAT_RE.search(s) or _NAME_HEADER_RE.search(s):
        return KIND_NAME_FORMAT
    # Делегируем определение терм/смысл существующему экстрактору петли перевыпуска.
    try:
        from . import feedback_learning  # noqa: PLC0415 (lazy: избегаем цикла)
        if feedback_learning.extract_learned_terms(s) or feedback_learning.extract_meaning_rules(s):
            return KIND_SERIES_TERM
    except Exception:  # noqa: BLE001
        pass
    return KIND_CONTENT


def is_format_directive(text: object) -> bool:
    """name-format про ФОРМАТ (структуру/длину) vs про ИМЯ/шапку. Формат → шаблон (F2)."""
    return bool(isinstance(text, str) and _FORMAT_RE.search(text))


# Имя: 1–2 слова с заглавной (кириллица/латиница), допускаем дефис.
_NAME_TOKEN = r"[А-ЯЁA-Z][а-яёa-zA-Z]+(?:-[А-ЯЁA-Zа-яёa-z]+)?"
_NAME_GROUP = rf"({_NAME_TOKEN}(?:\s+{_NAME_TOKEN})?)"
# Домен: строчная именная группа 1–3 слова (greedy по словам, backtracking отдаёт
# хвостовой глагол/имя обратно паттерну). Без заглавных в начале (это не имя).
_DOMAIN_GROUP = r"([а-яёa-z][а-яёa-z\-]+(?:\s+[а-яёa-z\-]+){0,2})"

_ROLE_PATTERNS = (
    # «за <домен> отвеча(ет/ют) <Имя>» / «за <домен> — <Имя>»
    re.compile(rf"\bза\s+{_DOMAIN_GROUP}\s+(?:отвеча\w+|вед[её]т|кури\w+|закреп\w+)\s+{_NAME_GROUP}", re.IGNORECASE),
    # «<Имя> отвеча(ет) за <домен>» / «<Имя> ведёт <домен>»
    re.compile(rf"\b{_NAME_GROUP}\s+(?:отвеча\w+|вед[её]т|кури\w+|занима\w+ся)\s+(?:за\s+)?{_DOMAIN_GROUP}", re.IGNORECASE),
    # «<домен> — зона ответственности <Имя>» / «<домен> ведёт <Имя>»
    re.compile(rf"\b{_DOMAIN_GROUP}\s+(?:вед[её]т|кури\w+|—\s*это\s+зона\s+\w+)\s+{_NAME_GROUP}", re.IGNORECASE),
)

# Если «домен» на самом деле — стоп-слово/местоимение, роль не извлекаем.
_DOMAIN_STOP = {"это", "он", "она", "они", "его", "её", "их", "там", "тут", "здесь", "всё", "все", "тебя", "меня"}


def _clean_role_name(raw: str) -> str:
    return (raw or "").strip().strip("«»\"'`.,;:!?()").strip()


def _clean_role_domain(raw: str) -> str:
    d = (raw or "").strip().strip("«»\"'`.,;:!?()").strip()
    # Срезаем хвостовые служебные слова, прилипшие к домену.
    d = re.sub(r"\s+(?:за|на|в|и|по|это|—)$", "", d, flags=re.IGNORECASE).strip()
    return d


def _extract_role_pure(text: str) -> Optional[tuple]:
    """(name, domain) из правки про роль, иначе None. Имя в одной из групп — по
    тому, какая похожа на имя (заглавная); домен — строчная группа."""
    for i, pat in enumerate(_ROLE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        g1, g2 = m.group(1), m.group(2)
        # В паттерне 1 порядок (домен, имя); в 2 (имя, домен); в 3 (домен, имя).
        if i == 1:
            name, domain = g1, g2
        else:
            name, domain = g2, g1
        name = _clean_role_name(name)
        domain = _clean_role_domain(domain)
        if not name or not domain:
            continue
        if domain.casefold() in _DOMAIN_STOP or name.casefold() in _DOMAIN_STOP:
            continue
        # Имя должно начинаться с заглавной (это человек), домен — со строчной.
        if not re.match(r"^[А-ЯЁA-Z]", name):
            continue
        return name, domain
    return None


def extract_role(text: object) -> Optional[dict]:
    """{name, domain} из правки про роль (санитизировано, без кредов) либо None."""
    if not isinstance(text, str) or not text.strip():
        return None
    pair = _extract_role_pure(text)
    if pair is None:
        return None
    name, domain = pair
    if not cred_filter.is_safe_to_store(name) or not cred_filter.is_safe_to_store(domain):
        return None
    return {"name": name, "domain": domain}


# ── Конфиг-очередь для имён/шапки (свой слой, не знание и не шаблон) ──────────


def _config_queue_path() -> Path:
    env = os.environ.get("NOTARY_CONFIG_PROPOSALS_PATH")
    if env:
        return Path(env).expanduser()
    vocab = os.environ.get("SPEECHMATICS_VOCAB_PATH",
                           "/srv/meeting-notary/config/speechmatics-vocab.json")
    return Path(vocab).expanduser().parent / "config_proposals.jsonl"


def _append_config_proposal(text: str, *, series: Optional[str], source: Optional[dict]) -> bool:
    p = _config_queue_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"kind": "name-config", "text": cred_filter.scrub(text)[:240],
               "series": series, "source_feedback_id": (source or {}).get("feedback_id"),
               "at": datetime.now().isoformat(timespec="seconds")}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        logger.warning("[fb-router] config-очередь не записана (%s)", e)
        return False


# ── Диспетчер: маршрутизация правок по слоям ─────────────────────────────────


def route_edits(
    state: dict, edits: Optional[list], *, template_root: Optional[Path] = None,
    watched: Optional[dict] = None,
) -> dict:
    """Разнести КОНТЕНТНЫЕ правки по слоям (D6). Best-effort, не бросает.

    Возвращает сводку {routed: {role,series-term,name-format,content}, dropped_creds,
    details:[...]}. Зовётся из `reissue_one` рядом с `record_learning_from_edits`
    (термины/смысл уже ушли в карточку серии — здесь добавляем РОЛИ и ИМЯ/ФОРМАТ).

    `template_root` — корень журнала версий шаблона (F2); None → дефолтный store-dir
    `protocol_template` (тот же, что читает `generate_protocol.format_block()`).
    Передаётся ТОЛЬКО тестами для изоляции — НЕ feedback-dir (иначе версии формата
    осели бы не там, где их читает генерация).

    🔴 НЕ трогает feedback-state, delivered-маркеры, сырьё — только outbox/шаблон.
    """
    series = (state or {}).get("series")
    present = _present_from_state(state)
    summary = {"routed": {KIND_ROLE: 0, KIND_SERIES_TERM: 0, KIND_NAME_FORMAT: 0, KIND_CONTENT: 0},
               "dropped_creds": 0, "details": []}
    for e in edits or []:
        if not isinstance(e, dict):
            continue
        text = e.get("text") or ""
        # D5: правка с кредом не учится ни в один слой.
        if cred_filter.looks_like_secret(text):
            summary["dropped_creds"] += 1
            summary["details"].append({"kind": "dropped-cred", "secret_kind": cred_filter.secret_kind(text)})
            continue
        kind = classify_edit(text)
        summary["routed"][kind] = summary["routed"].get(kind, 0) + 1
        source = {"series": series, "date": (state or {}).get("date"),
                  "feedback_id": (state or {}).get("feedback_id")}
        try:
            if kind == KIND_ROLE:
                summary["details"].append(_route_role(text, series, present, source, watched))
            elif kind == KIND_NAME_FORMAT:
                summary["details"].append(_route_name_format(text, series, source, template_root))
            else:
                # series-term → уже в карточке серии (feedback_learning); content — разовая.
                summary["details"].append({"kind": kind, "layer":
                                           "series-card" if kind == KIND_SERIES_TERM else "one-off"})
        except Exception as ex:  # noqa: BLE001
            logger.warning("[fb-router] маршрутизация правки не удалась (non-fatal): %s", ex)
    if any(summary["routed"].values()) or summary["dropped_creds"]:
        logger.info("[fb-router] series=%s routed role=%d term=%d name/format=%d content=%d dropped_creds=%d",
                    series or "?", summary["routed"][KIND_ROLE], summary["routed"][KIND_SERIES_TERM],
                    summary["routed"][KIND_NAME_FORMAT], summary["routed"][KIND_CONTENT],
                    summary["dropped_creds"])
    return summary


def _present_from_state(state: dict) -> Optional[list]:
    for k in ("participants", "present_participants", "expectedParticipants"):
        v = (state or {}).get(k)
        if isinstance(v, list) and v:
            return [str(x) for x in v]
    return None


def _route_role(text: str, series, present, source: dict, watched) -> dict:
    role = extract_role(text)
    if not role:
        return {"kind": KIND_ROLE, "layer": "unparsed"}
    try:
        from . import knowledge_writeback  # noqa: PLC0415
        res = knowledge_writeback.propose_role(
            role["name"], role["domain"], series=series,
            present_participants=present, watched=watched, source=source,
        )
        return {"kind": KIND_ROLE, "layer": res.layer, "company": res.company,
                "name": role["name"], "domain": role["domain"]}
    except Exception as e:  # noqa: BLE001
        logger.warning("[fb-router] role write-back не удался (%s)", e)
        return {"kind": KIND_ROLE, "layer": "error"}


def _route_name_format(text: str, series, source: dict, root) -> dict:
    if is_format_directive(text):
        try:
            from . import protocol_template  # noqa: PLC0415
            r = protocol_template.register_format_change(text, series=series, source=source, root=root)
            return {"kind": KIND_NAME_FORMAT, "layer": "template",
                    "version": (r or {}).get("version")}
        except Exception as e:  # noqa: BLE001
            logger.warning("[fb-router] template register не удался (%s)", e)
            return {"kind": KIND_NAME_FORMAT, "layer": "error"}
    ok = _append_config_proposal(text, series=series, source=source)
    return {"kind": KIND_NAME_FORMAT, "layer": "config" if ok else "error"}
