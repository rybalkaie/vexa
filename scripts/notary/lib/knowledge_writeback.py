"""Ф7 D1: write-back авто-выученного ЗНАНИЯ в `*-context` нужной компании.

Меняет адресата авто-пополнения: раньше auto_vocab дописывал термины в локальный
`speechmatics-vocab-auto.json` (код-смежный файл на VPS). Теперь ПРОИЗВОДНОЕ знание
(термин/роль) адресуется КОНТЕКСТУ КОМПАНИИ — `<company>-context/knowledge/notary/`
— по контракту Ф4 §3.3: **ветка `notary/auto-knowledge` + PR, НЕ прямой push в
`main`**. Команда (Михаил/Татьяна) ревьюит PR — это и есть «бот вносит, раз в
неделю отчитывается, команда корректирует» (D2). ASR-словарь продолжает работать
своим каналом (проекция A из `glossary.yaml` после мёржа) — этот модуль про
ДОЛГОВЕЧНОЕ знание, не про немедленное распознавание.

Слой назначения каждого факта решает `knowledge_router` (D3/D4/D5):
  • DROP    (креды) → не сохраняем НИКУДА;
  • COMPANY → company-outbox → PR в `*-context` (этот модуль);
  • PRIVATE → приватная очередь в `me/` (дефолт D3).

═══ Что в этой фазе РЕАЛЬНО, а что — задел Ф8 ═══
Outbox (адресованная-по-компании очередь предложений) + чистый ПЛАН PR (`plan_pr`,
с зашитыми инвариантами §3.3) + чистый мёрж знания в YAML (`apply_entries_to_glossary`/
`apply_entries_to_org`) — РЕАЛЬНЫ и тестируются на dict/моках/тест-клоне. Боевой
push/PR требует реального write-токена (`NOTARY_CONTEXT_GIT_WRITE_TOKEN`) и клона —
это **провижининг владельца/Ф8** (контракт §3.4). Без токена `flush_company_outbox`
— no-op: предложения копятся в outbox, дайджест D2 их показывает «ожидают PR».

🔴 Безопасность: креды (D5) фильтруются повторно здесь (defense-in-depth). Сырьё
(транскрипт/реплики) сюда НЕ попадает — только производные термины/роли. В лог —
метаданные (kind/company/счётчики), не значения и не тексты.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import cred_filter
from . import context_knowledge
from . import knowledge_router

logger = logging.getLogger(__name__)

KIND_TERM = "term"
KIND_ROLE = "roster-role"
# Ф9 (B1/B2): durable-факт уровня второго мозга (стратегия/позиционирование/
# экономика/проверенный вывод/веха). Едет в `*-context` той же веткой+PR, что
# термины/роли, но в ОТДЕЛЬНЫЙ markdown-файл `insights.md` (читаемый командой; не
# YAML-словарь). Команда на ревью PR переносит в `baza-znaniy/` по своему усмотрению.
KIND_INSIGHT = "insight"

# Стабильное имя bot-ветки (контракт §3.3) — пересоздаётся от свежего origin/main.
WRITEBACK_BRANCH = "notary/auto-knowledge"
# Поддерево, которое бот ПРАВИТ (и только его) — контракт §1.1/§3.3.
GLOSSARY_REL = "knowledge/notary/glossary.yaml"
ORG_REL = "knowledge/notary/org-structure.yaml"
INSIGHTS_REL = "knowledge/notary/insights.md"  # Ф9: durable-факты встреч (markdown)

# Шапка `insights.md` при первом создании (бот владеет файлом целиком).
_INSIGHTS_HEADER = (
    "# Авто-знание нотариуса: durable-факты встреч\n\n"
    "<!-- Бот-нотариус (Ф9) предлагает сюда durable-факты уровня второго мозга\n"
    "(стратегия/позиционирование/экономика/проверенный вывод/веха). Ревью и перенос\n"
    "в baza-znaniy — за командой через PR. Чувствительное сюда НЕ попадает (фильтр\n"
    "G11 + публикационный гейт). Сырьё (транскрипт/реплики) сюда НЕ течёт. -->\n"
)


# ── Пути хранилища ────────────────────────────────────────────────────────────


def _state_dir() -> Path:
    vocab = os.environ.get(
        "SPEECHMATICS_VOCAB_PATH",
        "/srv/meeting-notary/config/speechmatics-vocab.json",
    )
    return Path(vocab).expanduser().parent


def outbox_dir() -> Path:
    env = os.environ.get("NOTARY_KNOWLEDGE_OUTBOX_DIR")
    if env:
        return Path(env).expanduser()
    return _state_dir() / "knowledge_outbox"


def company_outbox_path(company: str) -> Path:
    return outbox_dir() / f"{str(company).strip().lower()}.jsonl"


def private_queue_path() -> Path:
    """Приватная очередь знания (дефолт D3) — человекочитаемый markdown в `me/`.

    Если `me/` недоступна (C4: бот не зависит от папки владельца) — fallback в
    локальный state-dir, чтобы приватный факт не потерялся и не утёк в команду.
    """
    env = os.environ.get("NOTARY_PRIVATE_KNOWLEDGE_QUEUE")
    if env:
        return Path(env).expanduser()
    me = Path(os.environ.get("MEETING_NOTARY_ME_DIR", "~/Projects/me")).expanduser()
    if me.is_dir():
        return me / "_inbox" / "notary-knowledge-queue.md"
    return _state_dir() / "private-knowledge-queue.md"


# ── Outbox: запись предложения, адресованного слою ────────────────────────────


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_outbox(company: str) -> list[dict]:
    p = company_outbox_path(company)
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    except OSError as e:
        logger.warning("[writeback] outbox %s не прочитан (%s)", p, e)
    return out


def _outbox_values(company: str, kind: str) -> set:
    return {
        str(r.get("value") or "").strip().lower()
        for r in _read_outbox(company)
        if r.get("kind") == kind and r.get("status") != "delivered"
    }


def _append_outbox(company: str, record: dict) -> None:
    p = company_outbox_path(company)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_private(value: str, kind: str, source: Optional[dict]) -> bool:
    p = private_queue_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        src = source or {}
        line = (f"- [{_now_iso()}] ({kind}) **{value}** "
                f"— серия `{src.get('series') or '—'}`, {src.get('date') or '—'}\n")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError as e:
        logger.warning("[writeback] приватная очередь %s не записана (%s)", p, e)
        return False


@dataclass(frozen=True)
class EnqueueResult:
    layer: str               # company | private | drop | exists
    company: Optional[str]
    value: str
    reason: str


def _glossary_existing_canon(company: str) -> set:
    """Каноники, УЖЕ лежащие в glossary.yaml компании — чтобы не предлагать дубль."""
    try:
        return {
            str(e.get("canonical") or "").strip().lower()
            for e in context_knowledge.load_glossary(company)
            if e.get("canonical")
        }
    except Exception as e:  # noqa: BLE001
        logger.info("[writeback] load_glossary(%s) failed (%s) — дедуп только по outbox", company, e)
        return set()


def enqueue(
    destination,
    *,
    kind: str,
    value: str,
    payload: dict,
    source: Optional[dict] = None,
) -> EnqueueResult:
    """Положить факт в слой по вердикту `knowledge_router.Destination`.

    DROP (креды) → ничего. PRIVATE → приватная очередь `me/`. COMPANY → company-
    outbox, адресованный `<company>-context/knowledge/notary/*` (D1). Дедуп: против
    outbox + (для термина) против уже лежащего в glossary.yaml. Идемпотентно.
    """
    val = (value or "").strip()
    if not val:
        return EnqueueResult("drop", None, "", "empty")
    # D5 defense-in-depth: даже если роутер пропустил — здесь второй барьер.
    if not cred_filter.is_safe_to_store(val) or destination.is_drop:
        logger.warning("[writeback] факт не сохранён (вид=%s/layer=%s) — D5/router",
                       cred_filter.secret_kind(val), destination.layer)
        return EnqueueResult("drop", None, val, destination.reason)

    if destination.is_private:
        ok = _append_private(val, kind, source)
        return EnqueueResult("private" if ok else "drop", None, val, destination.reason)

    # COMPANY: дедуп и запись в адресованную компании очередь.
    company = destination.company
    if not company:
        # Роутер сказал company, но компании нет — fail-closed в приватное.
        ok = _append_private(val, kind, source)
        return EnqueueResult("private" if ok else "drop", None, val, "company-missing→private")
    key = val.lower()
    if key in _outbox_values(company, kind):
        return EnqueueResult("exists", company, val, "already-queued")
    if kind == KIND_TERM and key in _glossary_existing_canon(company):
        return EnqueueResult("exists", company, val, "already-in-glossary")
    _rel = {KIND_TERM: GLOSSARY_REL, KIND_ROLE: ORG_REL, KIND_INSIGHT: INSIGHTS_REL}.get(kind, ORG_REL)
    target = f"{context_knowledge.repo_for_company(company) or company}/{_rel}"
    record = {
        "kind": kind,
        "company": company,
        "target": target,
        "value": val,
        "payload": payload,
        "source": {k: (source or {}).get(k) for k in ("series", "date", "feedback_id")},
        "at": _now_iso(),
        "status": "queued",
    }
    _append_outbox(company, record)
    # B6/опасная тройка: значение insight — durable-ВЫВОД (целое предложение, текст
    # кандидата), его в INFO-лог НЕ пишем; термин/роль — короткий каноник, допустимо.
    _logval = f"<{len(val)} симв.>" if kind == KIND_INSIGHT else f"«{val}»"
    logger.info("[writeback] предложение в %s: %s %s", target, kind, _logval)
    return EnqueueResult("company", company, val, destination.reason)


def propose_term(
    canonical: str,
    *,
    series: Optional[str],
    company: Optional[str] = None,
    aliases: Optional[list] = None,
    note: Optional[str] = None,
    scope: Optional[str] = None,
    publication_allowed: bool = False,
    present_participants: Optional[list] = None,
    watched: Optional[dict] = None,
    source: Optional[dict] = None,
) -> EnqueueResult:
    """Предложить ТЕРМИН в контекст компании (или приватно по D3). Адресат — роутер.

    Если `company`/`publication_allowed` не переданы — резолвит из серии встречи
    (`route_for_meeting`). aliases чистятся от кредов (D5). Дедуп — в `enqueue`.
    """
    canonical = (canonical or "").strip()
    if not canonical:
        return EnqueueResult("drop", None, "", "empty")
    safe_aliases = cred_filter.filter_safe([str(a).strip() for a in (aliases or []) if str(a).strip()])
    if company is not None:
        dest = knowledge_router.classify_destination(
            canonical, kind=KIND_TERM, company=company, publication_allowed=publication_allowed,
        )
    else:
        dest = knowledge_router.route_for_meeting(
            canonical, kind=KIND_TERM, series=series,
            present_participants=present_participants, watched=watched,
        )
    payload = {
        "canonical": canonical,
        "scope": (scope or (dest.company or "")) or None,
        "aliases": safe_aliases,
    }
    if note:
        payload["note"] = str(note)[:200]
    return enqueue(dest, kind=KIND_TERM, value=canonical, payload=payload,
                   source=dict(source or {}, series=series))


def propose_role(
    name: str,
    domain: str,
    *,
    series: Optional[str],
    company: Optional[str] = None,
    keywords: Optional[list] = None,
    publication_allowed: bool = False,
    present_participants: Optional[list] = None,
    watched: Optional[dict] = None,
    source: Optional[dict] = None,
) -> EnqueueResult:
    """Предложить РОЛЬ (человек→домен) в оргструктуру компании (или приватно по D3)."""
    name = (name or "").strip()
    domain = (domain or "").strip()
    if not name or not domain:
        return EnqueueResult("drop", None, name, "incomplete-role")
    value = f"{name} | {domain}"
    safe_kw = cred_filter.filter_safe([str(k).strip() for k in (keywords or []) if str(k).strip()])
    if company is not None:
        dest = knowledge_router.classify_destination(
            value, kind=KIND_ROLE, company=company, publication_allowed=publication_allowed,
        )
    else:
        dest = knowledge_router.route_for_meeting(
            value, kind=KIND_ROLE, series=series,
            present_participants=present_participants, watched=watched,
        )
    payload = {"slug": series, "role": {"name": name, "domain": domain, "keywords": safe_kw}}
    return enqueue(dest, kind=KIND_ROLE, value=value, payload=payload,
                   source=dict(source or {}, series=series))


def propose_insight(
    fact: str,
    *,
    series: Optional[str],
    company: Optional[str] = None,
    insight_kind: Optional[str] = None,
    confidence: Optional[str] = None,
    publication_allowed: bool = False,
    present_participants: Optional[list] = None,
    watched: Optional[dict] = None,
    source: Optional[dict] = None,
) -> EnqueueResult:
    """Ф9 (B1/B2): предложить durable-ФАКТ уровня второго мозга. Адресат — роутер.

    Тонкая обёртка вокруг `route_for_meeting`/`classify_destination` + `enqueue`,
    симметричная `propose_term`/`propose_role`, но для нового вида знания (insight).
    НЕ заводит параллельную трубу — тот же router/outbox/PR-механизм.

    🔴 Маршрут COMPANY обязан ДОПОЛНИТЕЛЬНО пройти фильтр чувствительного (G11) у
    вызывателя (`knowledge_distill.distill_and_route`) ДО записи; здесь — только
    адресация слоя. Прямой вызов с company+publication_allowed=True предполагает,
    что G11 уже пройден (используется в тестах/после фильтра).
    """
    fact = (fact or "").strip()
    if not fact:
        return EnqueueResult("drop", None, "", "empty")
    if company is not None:
        dest = knowledge_router.classify_destination(
            fact, kind=KIND_INSIGHT, company=company, publication_allowed=publication_allowed,
        )
    else:
        dest = knowledge_router.route_for_meeting(
            fact, kind=KIND_INSIGHT, series=series,
            present_participants=present_participants, watched=watched,
        )
    payload = {"fact": fact, "scope": dest.company or (str(company).strip().lower() if company else None) or None}
    if insight_kind:
        payload["insight_kind"] = str(insight_kind)[:40]
    if confidence:
        payload["confidence"] = str(confidence)[:10]
    return enqueue(dest, kind=KIND_INSIGHT, value=fact, payload=payload,
                   source=dict(source or {}, series=series))


# ── Чистый мёрж предложений в YAML-структуру знания (тестируется на dict) ──────


def apply_entries_to_glossary(glossary_doc: Optional[dict], entries: list[dict]) -> tuple[dict, int]:
    """Влить term-предложения в распарсенный glossary.yaml. Возвращает (doc, added).

    Чистая функция над dict (без pyyaml/IO) — единица тестов мёржа. Дедуп по
    `canonical` (case-insensitive) против уже лежащих. Креды-payload отсекаются (D5).
    """
    doc = dict(glossary_doc or {})
    terms = doc.get("terms")
    if not isinstance(terms, list):
        terms = []
    have = {str(t.get("canonical") or "").strip().lower() for t in terms if isinstance(t, dict)}
    added = 0
    for e in entries:
        if e.get("kind") != KIND_TERM:
            continue
        pl = e.get("payload") or {}
        canonical = str(pl.get("canonical") or "").strip()
        if not canonical or canonical.lower() in have:
            continue
        if not cred_filter.is_safe_to_store(canonical):
            continue
        entry = {"canonical": canonical, "scope": pl.get("scope") or e.get("company")}
        aliases = cred_filter.filter_safe([str(a).strip() for a in (pl.get("aliases") or []) if str(a).strip()])
        if aliases:
            entry["aliases"] = aliases
        if pl.get("note"):
            entry["note"] = pl["note"]
        terms.append(entry)
        have.add(canonical.lower())
        added += 1
    doc.setdefault("version", 1)
    doc["terms"] = terms
    return doc, added


def apply_entries_to_org(org_doc: Optional[dict], entries: list[dict]) -> tuple[dict, int]:
    """Влить role-предложения в распарсенный org-structure.yaml. Возвращает (doc, added).

    Чистая функция над dict. Роль кладётся в `rosters[slug].roles`, дедуп по имени
    внутри серии. Креды-имена/keywords отсекаются (D5).
    """
    doc = dict(org_doc or {})
    rosters = doc.get("rosters")
    if not isinstance(rosters, dict):
        rosters = {}
    added = 0
    for e in entries:
        if e.get("kind") != KIND_ROLE:
            continue
        pl = e.get("payload") or {}
        slug = str(pl.get("slug") or "").strip()
        role = pl.get("role") or {}
        name = str(role.get("name") or "").strip()
        if not slug or not name or not cred_filter.is_safe_to_store(name):
            continue
        spec = rosters.get(slug)
        if not isinstance(spec, dict):
            spec = {"company": e.get("company"), "roles": []}
        roles = spec.get("roles")
        if not isinstance(roles, list):
            roles = []
        if any(str(r.get("name") or "").strip().lower() == name.lower() for r in roles if isinstance(r, dict)):
            continue
        roles.append({
            "name": name,
            "domain": str(role.get("domain") or "").strip(),
            "keywords": cred_filter.filter_safe([str(k).strip() for k in (role.get("keywords") or []) if str(k).strip()]),
        })
        spec["roles"] = roles
        rosters[slug] = spec
        added += 1
    doc.setdefault("version", 1)
    doc["rosters"] = rosters
    return doc, added


def _norm_fact(s: object) -> str:
    """Нормализация факта для дедупа: схлопнуть пробелы, lower, отбросить хвостовую
    пунктуацию. Не идеальная семантика, но ловит точные/почти-точные повторы."""
    t = " ".join(str(s or "").split()).strip().lower()
    return t.rstrip(".!?;: ")


def apply_entries_to_insights(insights_text: Optional[str], entries: list[dict]) -> tuple[str, int]:
    """Ф9: влить insight-предложения в markdown `insights.md`. Возвращает (text, added).

    Чистая функция над ТЕКСТОМ (не YAML — файл человекочитаемый, бот владеет им
    целиком). Дедуп по нормализованному факту против уже лежащих строк. Креды
    отсекаются (D5). Сырьё сюда не попадает — только сжатый факт. Новые строки
    дописываются в конец (минимальный diff для ревью).
    """
    text = insights_text if (insights_text and insights_text.strip()) else _INSIGHTS_HEADER
    # Существующие факты — нормализованный пул для дедупа (всё тело файла, lower).
    have_norm = "\n".join(_norm_fact(ln) for ln in text.splitlines())
    new_lines: list[str] = []
    added = 0
    for e in entries:
        if e.get("kind") != KIND_INSIGHT:
            continue
        pl = e.get("payload") or {}
        fact = str(pl.get("fact") or e.get("value") or "").strip()
        if not fact or not cred_filter.is_safe_to_store(fact):
            continue
        nf = _norm_fact(fact)
        if not nf or nf in have_norm:
            continue
        src = e.get("source") or {}
        series = src.get("series") or pl.get("slug") or pl.get("series") or "—"
        date = src.get("date") or "—"
        kind_tag = pl.get("insight_kind")
        tag = f" _[{kind_tag}]_" if kind_tag else ""
        new_lines.append(f"- [{date}] (серия `{series}`){tag} {fact}")
        have_norm += "\n" + nf
        added += 1
    if not new_lines:
        return text, 0
    if not text.endswith("\n"):
        text += "\n"
    # Пустая строка-разделитель перед первым добавлением в свежий файл/блок.
    if not text.endswith("\n\n"):
        text += "\n"
    text += "\n".join(new_lines) + "\n"
    return text, added


# ── Чистый ПЛАН PR (инварианты Ф4 §3.3 зашиты и тестируются) ──────────────────


@dataclass(frozen=True)
class PRPlan:
    company: str
    repo: str
    branch: str
    base: str
    commit_message: str
    title: str
    body: str
    # argv-команды по порядку (git/gh). Тест проверяет безопасность.
    commands: tuple
    # GitHub compare-URL для ручного открытия PR (deploy-key путь: бот пушит ветку,
    # PR открывает команда по ссылке — `gh pr create` через REST требует PAT).
    compare_url: Optional[str] = None


def compare_url(repo: str, *, branch: str = WRITEBACK_BRANCH, base: str = "main") -> str:
    """GitHub compare-URL для ручного открытия PR команды (`?expand=1` = форма PR).

    Deploy-key путь: бот пушит ветку (SSH read-write deploy key умеет), но НЕ может
    `gh pr create` (REST требует PAT с `pull_requests:write`). Ссылку отдаём в
    еженедельный дайджест — команда открывает PR одним кликом.
    """
    return f"https://github.com/{repo}/compare/{base}...{branch}?expand=1"


def plan_pr(company: str, entries: list[dict], *, repo: Optional[str] = None,
            pr_via: str = "gh") -> PRPlan:
    """Построить ПЛАН ветка+PR для company-outbox (контракт §3.3). Чистая функция.

    🔴 Инварианты (тестируются `test_knowledge_writeback`):
      • ветка `notary/auto-knowledge` создаётся от СВЕЖЕГО `origin/main` (recreate);
      • push идёт в bot-ВЕТКУ, НЕ в `main`; нет bare `--force` в `main`;
      • PR создаётся (`gh pr create --base main`), НЕ мёржится ботом;
      • `git add` трогает ТОЛЬКО `knowledge/notary/*` (своё поддерево).

    `pr_via`: "gh" (дефолт) — финальной командой `gh pr create` (нужен write-PAT);
    "push" — боевой deploy-key путь: команды заканчиваются на push, а PR команда
    открывает по `plan.compare_url` (deploy key не умеет REST `gh pr create`).
    """
    repo = repo or context_knowledge.full_repo_for_company(company)
    if not repo:
        raise ValueError(
            f"не резолвится <owner>/<repo> для компании {company!r} — "
            "нет owner/repo в context_knowledge (write-back невозможен)"
        )
    n_term = sum(1 for e in entries if e.get("kind") == KIND_TERM)
    n_role = sum(1 for e in entries if e.get("kind") == KIND_ROLE)
    n_insight = sum(1 for e in entries if e.get("kind") == KIND_INSIGHT)
    title = f"[notary] авто-знание: +{n_term} терм., +{n_role} ролей, +{n_insight} фактов"
    # Тело PR — производные термины/роли/факты, НЕ сырьё (опасная тройка). Факты —
    # сжатые durable-выводы (Ф9), прошедшие G11+гейт ДО постановки в COMPANY-outbox.
    body_lines = ["Авто-предложение бота-нотариуса (D1/D2/Ф9). Ревью и мёрж — за командой.", ""]
    for e in entries:
        if e.get("kind") == KIND_TERM:
            body_lines.append(f"- термин: `{e.get('value')}`")
        elif e.get("kind") == KIND_ROLE:
            body_lines.append(f"- роль: `{e.get('value')}` (серия {(e.get('source') or {}).get('series') or '—'})")
        elif e.get("kind") == KIND_INSIGHT:
            pl = e.get("payload") or {}
            body_lines.append(f"- факт: {pl.get('fact') or e.get('value')} (серия {(e.get('source') or {}).get('series') or '—'})")
    body = "\n".join(body_lines)
    commit_message = title
    # Conditional `git add` — стейджим ТОЛЬКО файлы под актуальные виды записей.
    # Иначе `git add <несуществующий insights.md>` упал бы на репо без файла и
    # отменил бы всю постановку (git add атомарен по pathspec). Все пути — внутри
    # поддерева бота knowledge/notary/* (контракт §3.3). Term/role-путь неизменен.
    add_paths = []
    if n_term:
        add_paths.append(GLOSSARY_REL)
    if n_role:
        add_paths.append(ORG_REL)
    if n_insight:
        add_paths.append(INSIGHTS_REL)
    if not add_paths:  # подстраховка (entries без распознанного kind) — штатные YAML
        add_paths = [GLOSSARY_REL, ORG_REL]
    commands = [
        ["git", "fetch", "origin"],
        ["git", "checkout", "-B", WRITEBACK_BRANCH, "origin/main"],
        ["git", "add", *add_paths],
        ["git", "commit", "-m", commit_message],
        ["git", "push", "--force-with-lease", "origin", WRITEBACK_BRANCH],
    ]
    if pr_via == "gh":
        commands.append(
            ["gh", "pr", "create", "--repo", repo, "--base", "main",
             "--head", WRITEBACK_BRANCH, "--title", title, "--body", body]
        )
    return PRPlan(company=str(company).strip().lower(), repo=repo, branch=WRITEBACK_BRANCH,
                  base="main", commit_message=commit_message, title=title, body=body,
                  commands=tuple(commands), compare_url=compare_url(repo))


# ── Боевой провижининг (gated; реальный push — Ф8/владелец) ───────────────────


def is_writeback_provisioned() -> bool:
    """Есть ли write-токен для боевого PR (контракт §3.3). В Ф7/headless — нет."""
    tok = (os.environ.get("NOTARY_CONTEXT_GIT_WRITE_TOKEN") or "").strip()
    return bool(tok)


def flush_company_outbox(
    company: str,
    *,
    runner: Optional[Callable] = None,
    clone_path: Optional[Path] = None,
    yaml_writer: Optional[Callable] = None,
    force: bool = False,
) -> dict:
    """Применить company-outbox → PR (контракт §3.3). Боевой путь — gated токеном.

    Без write-токена (Ф7/headless) и `force=False` → no-op `not-provisioned`:
    предложения остаются в outbox, дайджест D2 их покажет. С `runner`/`clone_path`
    (тест-клон или мок) — исполняет `plan_pr` поверх клона, мёржит знание в YAML
    через `apply_entries_to_*`. Не трогает `main`, не мёржит PR (инварианты plan_pr).
    """
    entries = [r for r in _read_outbox(company) if r.get("status") == "queued"]
    if not entries:
        return {"status": "empty", "company": company, "entries": 0}
    if not (is_writeback_provisioned() or force or runner is not None):
        return {"status": "not-provisioned", "company": company, "entries": len(entries)}

    plan = plan_pr(company, entries)
    if runner is None:
        # Боевой раннер не подключаем в этой фазе (реальный gh/git — Ф8/владелец).
        return {"status": "no-runner", "company": company, "entries": len(entries), "plan": plan}

    # Тест-клон / мок: применяем YAML-мёрж + прогоняем команды через инъекц. runner.
    results = []
    if clone_path is not None and yaml_writer is not None:
        try:
            cp = Path(clone_path)
            g_doc, _ = apply_entries_to_glossary(_safe_yaml_read(cp / GLOSSARY_REL), entries)
            o_doc, _ = apply_entries_to_org(_safe_yaml_read(cp / ORG_REL), entries)
            yaml_writer(cp / GLOSSARY_REL, g_doc)
            yaml_writer(cp / ORG_REL, o_doc)
        except Exception as e:  # noqa: BLE001
            logger.warning("[writeback] YAML-мёрж в клон не удался (%s)", e)
    for cmd in plan.commands:
        results.append(runner(cmd, cwd=str(clone_path) if clone_path else None))
    return {"status": "pr-opened", "company": company, "entries": len(entries),
            "branch": plan.branch, "commands": len(plan.commands), "runner_results": results}


def _safe_yaml_read(path: Path) -> Optional[dict]:
    try:
        import yaml  # lazy
    except ImportError:
        return None
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


# ── Боевой раннер write-back (deploy-key push + compare-URL) ───────────────────
#
# Контракт §3.3: бот пушит ветку `notary/auto-knowledge` поверх свежего origin/main,
# правит ТОЛЬКО knowledge/notary/*, НЕ трогает main, НЕ мёржит. На VPS доступ к
# `*-context` — SSH deploy keys (read-write): пушить ветку умеют, `gh pr create`
# (REST) — нет. Поэтому боевой путь = push ветки + compare-URL в дайджест (PR
# открывает команда кликом). Lifecycle статусов записи outbox:
#   queued → pr-pending (в открытой bot-ветке) → merged (подтверждён в origin/main).

STATUS_QUEUED = "queued"
STATUS_PENDING = "pr-pending"   # включён в bot-ветку, ждёт ревью/мёржа командой
STATUS_MERGED = "merged"        # дедуп при flush показал: уже в origin/main


def clone_dir_for_company(company: str) -> Optional[Path]:
    """Путь к локальному/VPS-клону `*-context` компании (корень репо, не поддерево)."""
    repo = context_knowledge.repo_for_company(company)
    if not repo:
        return None
    return context_knowledge.context_root() / repo


def _safe_yaml_write(path: Path, doc: dict) -> None:
    import yaml  # lazy; прод-venv несёт pyyaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _safe_text_read(path: Path) -> Optional[str]:
    """Прочитать markdown-файл `insights.md` (или None, если нет/нечитаем)."""
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _safe_text_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rewrite_outbox(company: str, records: list[dict]) -> None:
    p = company_outbox_path(company)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def _set_status(company: str, targets: set, new_status: str, **extra) -> int:
    """Перезаписать статус записей outbox по набору ключей (kind, value.lower())."""
    if not targets:
        return 0
    recs = _read_outbox(company)
    changed = 0
    for r in recs:
        key = (r.get("kind"), str(r.get("value") or "").strip().lower())
        if key in targets and r.get("status") != new_status:
            r["status"] = new_status
            r.update(extra)
            r["status_at"] = _now_iso()
            changed += 1
    if changed:
        _rewrite_outbox(company, recs)
    return changed


def _subprocess_runner(cmd: list, cwd: Optional[str] = None) -> dict:
    """Боевой раннер git/gh. Лог — только argv[0:2]+rc (значения/тело PR не логируем)."""
    import subprocess  # lazy
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        logger.warning("[writeback] $ %s упал (%s)", " ".join(cmd[:2]), e)
        return {"argv": cmd[:2], "rc": 1, "stdout": "", "stderr": str(e)[:200]}
    logger.info("[writeback] $ %s → rc=%s", " ".join(cmd[:2]), proc.returncode)
    return {"argv": cmd[:2], "rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _format_term_yaml(entry: dict) -> str:
    """Сериализовать ОДИН новый term-блок в стиле файла (2 пробела, `- canonical:`)."""
    lines = [f"  - canonical: {entry['canonical']}"]
    scope = entry.get("scope")
    if scope:
        lines.append(f"    scope: {scope}")
    note = entry.get("note")
    if note:
        if any(c in str(note) for c in ":#\"'") or str(note) != str(note).strip():
            note = '"' + str(note).replace('"', '\\"') + '"'
        lines.append(f"    note: {note}")
    aliases = entry.get("aliases")
    if aliases:
        lines.append(f"    aliases: [{', '.join(aliases)}]")
    return "\n".join(lines)


def _append_terms_textual(path: Path, new_items: list[dict]) -> bool:
    """Дописать новые term-блоки в конец списка `terms:`, СОХРАНЯЯ комментарии/формат
    (минимальный diff для ревью команды). False → структуру не распознали, фолбэк."""
    if not new_items:
        return True
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    ti = next((i for i, ln in enumerate(lines) if ln.rstrip() == "terms:"), None)
    if ti is None:
        return False
    end = len(lines)
    for j in range(ti + 1, len(lines)):
        ln = lines[j]
        if ln and not ln[0].isspace() and not ln.lstrip().startswith("#"):
            end = j
            break
    block = [bl for e in new_items for bl in _format_term_yaml(e).split("\n")]
    merged = lines[:end] + block + lines[end:]
    path.write_text("\n".join(merged) + "\n", encoding="utf-8")
    return True


def run_flush(
    company: str,
    *,
    runner: Optional[Callable] = None,
    clone_path: Optional[Path] = None,
    yaml_writer: Optional[Callable] = None,
    pr_via: str = "push",
) -> dict:
    """Боевой write-back: накопить queued+pr-pending → ветка `notary/auto-knowledge`
    поверх свежего origin/main → push (deploy key) → compare-URL для PR команды.

    Накопительная модель: КАЖДЫЙ запуск пересоздаёт единую bot-ветку из всех ещё-не-
    смёрженных предложений (а не только новых) — иначе recreate от origin/main терял
    бы прошлые pr-pending. Дедуп против свежего main: что уже там → статус `merged`
    (ретайр из дайджеста). Инъекции runner/clone_path/yaml_writer — для теста на
    временном git-репо; прод-дефолты: subprocess + VPS-клон + textual/yaml-writer.
    НЕ трогает main, НЕ мёржит PR (инварианты `plan_pr`)."""
    company = str(company).strip().lower()
    runner = runner or _subprocess_runner
    yaml_writer = yaml_writer or _safe_yaml_write
    cp = Path(clone_path) if clone_path else clone_dir_for_company(company)
    if cp is None or not (cp / ".git").is_dir():
        return {"status": "no-clone", "company": company, "clone": str(cp) if cp else None}

    active = [r for r in _read_outbox(company)
              if r.get("status") in (STATUS_QUEUED, STATUS_PENDING)]
    if not active:
        return {"status": "empty", "company": company, "entries": 0}

    plan = plan_pr(company, active, pr_via=pr_via)
    run = lambda cmd: runner(cmd, cwd=str(cp))  # noqa: E731

    # 1) свежий origin/main → ветка (plan-команды 0..1: fetch, checkout -B)
    if run(plan.commands[0]).get("rc"):
        return {"status": "fetch-failed", "company": company}
    run(plan.commands[1])  # рабочее дерево теперь = свежий origin/main

    # 2) что уже в main (→ merged), что новое (→ в ветку)
    g_doc0 = _safe_yaml_read(cp / GLOSSARY_REL)
    o_doc0 = _safe_yaml_read(cp / ORG_REL)
    insights_text0 = _safe_text_read(cp / INSIGHTS_REL)  # Ф9: markdown, может отсутствовать
    have_terms = {str(t.get("canonical") or "").strip().lower()
                  for t in ((g_doc0 or {}).get("terms") or []) if isinstance(t, dict)}
    have_roles = set()
    for slug, spec in ((o_doc0 or {}).get("rosters") or {}).items():
        if isinstance(spec, dict):
            for role in spec.get("roles") or []:
                if isinstance(role, dict):
                    have_roles.add((str(slug).strip().lower(),
                                    str(role.get("name") or "").strip().lower()))
    # Детект «уже в main» для insight ОБЯЗАН совпадать с дедупом apply_entries_to_insights
    # (там — подстрока нормализованного факта в нормализованном теле файла). Строки
    # insights.md несут префикс «- [дата] (серия …) _[тег]_», поэтому точное членство
    # ГОЛОГО факта в множестве строк не совпало бы НИКОГДА → уже-смерженный факт вечно
    # числился бы pending и не дренировался из outbox. Сверяем как дедуп: подстрока в blob.
    have_insights_norm = "\n".join(_norm_fact(ln) for ln in (insights_text0 or "").splitlines())
    merged_keys, pending_keys = set(), set()
    for r in active:
        kind, val = r.get("kind"), str(r.get("value") or "").strip().lower()
        if kind == KIND_TERM:
            (merged_keys if val in have_terms else pending_keys).add((kind, val))
        elif kind == KIND_ROLE:
            pl = r.get("payload") or {}
            slug = str(pl.get("slug") or "").strip().lower()
            name = str((pl.get("role") or {}).get("name") or "").strip().lower()
            (merged_keys if (slug, name) in have_roles else pending_keys).add((kind, val))
        elif kind == KIND_INSIGHT:
            pl = r.get("payload") or {}
            nf = _norm_fact(pl.get("fact") or r.get("value"))
            (merged_keys if (nf and nf in have_insights_norm) else pending_keys).add((kind, val))

    # 3) влить новое в YAML (glossary — textual append, сохраняя комментарии; org — dump).
    #    NB: apply_entries_to_glossary мутирует список terms на месте — длину «до» снимаем заранее.
    n_orig_terms = len((g_doc0 or {}).get("terms") or [])
    g_doc, n_term = apply_entries_to_glossary(g_doc0, active)
    o_doc, n_role = apply_entries_to_org(o_doc0, active)
    insights_text, n_insight = apply_entries_to_insights(insights_text0, active)  # Ф9
    if merged_keys:
        _set_status(company, merged_keys, STATUS_MERGED)
    if n_term == 0 and n_role == 0 and n_insight == 0:
        return {"status": "already-merged", "company": company,
                "merged": len(merged_keys), "entries": len(active)}
    if n_term:
        new_term_items = g_doc["terms"][n_orig_terms:]
        if not _append_terms_textual(cp / GLOSSARY_REL, new_term_items):
            yaml_writer(cp / GLOSSARY_REL, g_doc)
    if n_role:
        yaml_writer(cp / ORG_REL, o_doc)
    if n_insight:
        _safe_text_write(cp / INSIGHTS_REL, insights_text)  # Ф9: markdown-файл бота

    # 4) add → commit → push (plan-команды 2..4)
    run(plan.commands[2])  # git add knowledge/notary/*
    if run(plan.commands[3]).get("rc"):  # commit; rc!=0 ⇒ нечего коммитить = уже в main
        _set_status(company, pending_keys | merged_keys, STATUS_MERGED)
        return {"status": "nothing-to-commit", "company": company, "entries": len(active)}
    if run(plan.commands[4]).get("rc"):
        return {"status": "push-failed", "company": company}

    # 5) опц. gh pr create (только pr_via=gh + write-PAT); deploy-key путь — пропуск
    pr_results = [run(c) for c in plan.commands[5:]]

    # 6) пушнутые → pr-pending с compare-URL для дайджеста D2
    _set_status(company, pending_keys, STATUS_PENDING,
                compare_url=plan.compare_url, branch=plan.branch)
    return {"status": "pr-pushed", "company": company, "branch": plan.branch,
            "compare_url": plan.compare_url, "pushed": len(pending_keys),
            "merged": len(merged_keys), "n_term": n_term, "n_role": n_role,
            "n_insight": n_insight, "pr_results": pr_results}


def run_flush_all(*, companies: Optional[list] = None, **kw) -> dict:
    """flush по всем известным компаниям (для systemd-таймера). Сводка по компаниям."""
    out: dict = {}
    for c in (companies or context_knowledge.known_companies()):
        try:
            out[c] = run_flush(c, **kw)
        except Exception as e:  # noqa: BLE001
            logger.warning("[writeback] run_flush(%s) упал (%s)", c, e)
            out[c] = {"status": "error", "company": c, "error": str(e)[:120]}
    return out


# ── Сводка outbox для еженедельного дайджеста (D2) ────────────────────────────


def outbox_digest() -> dict:
    """Сводка очередей знания для D2-дайджеста: {company: {terms, roles, pr_url, ...}}.

    Показывает ДВЕ группы (значения — производные термины/роли, не сырьё; безопасно
    для личных сообщений владельцу): `queued` — ждут первого push; `pr-pending` —
    уже в открытой bot-ветке, `pr_url` = compare-ссылка для открытия PR командой.
    `merged`/`delivered` — ретайр, не показываем.
    """
    out: dict = {}
    d = outbox_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.jsonl")):
        company = p.stem
        recs = _read_outbox(company)
        queued = [r for r in recs if r.get("status") == STATUS_QUEUED]
        pending = [r for r in recs if r.get("status") == STATUS_PENDING]
        if not queued and not pending:
            continue
        pr_url = next((r.get("compare_url") for r in pending if r.get("compare_url")), None)
        out[company] = {
            "terms": [r.get("value") for r in queued if r.get("kind") == KIND_TERM],
            "roles": [r.get("value") for r in queued if r.get("kind") == KIND_ROLE],
            "insights": [r.get("value") for r in queued if r.get("kind") == KIND_INSIGHT],
            "pending_terms": [r.get("value") for r in pending if r.get("kind") == KIND_TERM],
            "pending_roles": [r.get("value") for r in pending if r.get("kind") == KIND_ROLE],
            "pending_insights": [r.get("value") for r in pending if r.get("kind") == KIND_INSIGHT],
            "pr_url": pr_url,
            "target_repo": context_knowledge.repo_for_company(company) or company,
        }
    return out


# ── CLI / systemd-таймер: боевой прогон write-back ────────────────────────────


def main(argv: Optional[list] = None) -> int:
    """Боевой прогон flush по всем компаниям. systemd: `python -m notary.lib.knowledge_writeback --flush`.

    Без `--flush` — сухой показ outbox-сводки (что ушло бы в дайджест), ничего не пушит.
    """
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [knowledge_writeback] %(message)s")
    ap = argparse.ArgumentParser(description="Write-back авто-знания в *-context (ветка+PR)")
    ap.add_argument("--flush", action="store_true", help="реально пушить bot-ветки (deploy key)")
    ap.add_argument("--company", default=None, help="только эта компания (по умолчанию все известные)")
    args = ap.parse_args(argv)
    companies = [args.company] if args.company else None
    if not args.flush:
        digest = outbox_digest()
        print(json.dumps({"dry-run": True, "outbox": digest}, ensure_ascii=False, indent=2))
        return 0
    summary = run_flush_all(companies=companies)
    # Печатаем только статусы/счётчики (не значения) — лог-гигиена опасной тройки.
    safe = {c: {k: v for k, v in (r or {}).items()
                if k in ("status", "pushed", "merged", "n_term", "n_role", "n_insight",
                         "branch", "compare_url")}
            for c, r in summary.items()}
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
