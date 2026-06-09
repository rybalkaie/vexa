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

# Стабильное имя bot-ветки (контракт §3.3) — пересоздаётся от свежего origin/main.
WRITEBACK_BRANCH = "notary/auto-knowledge"
# Поддерево, которое бот ПРАВИТ (и только его) — контракт §1.1/§3.3.
GLOSSARY_REL = "knowledge/notary/glossary.yaml"
ORG_REL = "knowledge/notary/org-structure.yaml"


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
    target = f"{context_knowledge.repo_for_company(company) or company}/{GLOSSARY_REL if kind == KIND_TERM else ORG_REL}"
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
    logger.info("[writeback] предложение в %s: %s «%s»", target, kind, val)
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


def plan_pr(company: str, entries: list[dict], *, repo: Optional[str] = None) -> PRPlan:
    """Построить ПЛАН ветка+PR для company-outbox (контракт §3.3). Чистая функция.

    🔴 Инварианты (тестируются `test_knowledge_writeback`):
      • ветка `notary/auto-knowledge` создаётся от СВЕЖЕГО `origin/main` (recreate);
      • push идёт в bot-ВЕТКУ, НЕ в `main`; нет bare `--force` в `main`;
      • PR создаётся (`gh pr create --base main`), НЕ мёржится ботом;
      • `git add` трогает ТОЛЬКО `knowledge/notary/*` (своё поддерево).
    """
    repo = repo or f"<org>/{context_knowledge.repo_for_company(company) or company}"
    n_term = sum(1 for e in entries if e.get("kind") == KIND_TERM)
    n_role = sum(1 for e in entries if e.get("kind") == KIND_ROLE)
    title = f"[notary] авто-знание: +{n_term} терм., +{n_role} ролей"
    # Тело PR — производные термины/роли, НЕ сырьё (опасная тройка).
    body_lines = ["Авто-предложение бота-нотариуса (D1/D2). Ревью и мёрж — за командой.", ""]
    for e in entries:
        if e.get("kind") == KIND_TERM:
            body_lines.append(f"- термин: `{e.get('value')}`")
        elif e.get("kind") == KIND_ROLE:
            body_lines.append(f"- роль: `{e.get('value')}` (серия {(e.get('source') or {}).get('series') or '—'})")
    body = "\n".join(body_lines)
    commit_message = title
    commands = (
        ["git", "fetch", "origin"],
        ["git", "checkout", "-B", WRITEBACK_BRANCH, "origin/main"],
        ["git", "add", GLOSSARY_REL, ORG_REL],
        ["git", "commit", "-m", commit_message],
        ["git", "push", "--force-with-lease", "origin", WRITEBACK_BRANCH],
        ["gh", "pr", "create", "--repo", repo, "--base", "main",
         "--head", WRITEBACK_BRANCH, "--title", title, "--body", body],
    )
    return PRPlan(company=str(company).strip().lower(), repo=repo, branch=WRITEBACK_BRANCH,
                  base="main", commit_message=commit_message, title=title, body=body,
                  commands=commands)


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


# ── Сводка outbox для еженедельного дайджеста (D2) ────────────────────────────


def outbox_digest() -> dict:
    """Сводка очередей знания для D2-дайджеста: {company: {terms, roles, values}}.

    Только queued (не delivered). Значения — производные термины/роли (не сырьё),
    безопасны для показа владельцу в личке.
    """
    out: dict = {}
    d = outbox_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.jsonl")):
        company = p.stem
        recs = [r for r in _read_outbox(company) if r.get("status") == "queued"]
        if not recs:
            continue
        out[company] = {
            "terms": [r.get("value") for r in recs if r.get("kind") == KIND_TERM],
            "roles": [r.get("value") for r in recs if r.get("kind") == KIND_ROLE],
            "target_repo": context_knowledge.repo_for_company(company) or company,
        }
    return out
