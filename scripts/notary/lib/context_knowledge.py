"""Ф5: единый загрузчик YAML-знания компании из VPS-клона `*-context`.

Реализует ИНТЕРФЕЙС, зафиксированный контрактом Ф4 (`docs/notary-context-contract.md`,
§1–§2): бот читает производное ЗНАНИЕ (глоссарий + оргструктуру) из выделенного
поддерева `knowledge/notary/` репозитория контекста нужной компании.

Топология (контракт §3.1):

    <context-root>/<repo>/knowledge/notary/glossary.yaml
    <context-root>/<repo>/knowledge/notary/org-structure.yaml

  - VPS:  context-root = /srv/meeting-notary/context (env MEETING_NOTARY_CONTEXT_DIR,
          задаётся в .env.notary); repo = anzhee-context | mpfirst-context.
  - мак:  context-root = ~/Projects (клоны `anzhee-context`/`mpfirst-context` уже там).
  Env `MEETING_NOTARY_CONTEXT_DIR` переопределяет корень (тесты указывают на фикстуру).

Дисциплина загрузчика (как `auto_vocab/sources.py`, контракт §2): нет клона / нет
файла / нет pyyaml / битый YAML → **пустой результат**, НЕ падаем, НЕ зануляем.
Поведение «как до Ф5» (fallback на встроенные источники) обеспечивают вызыватели
(`glossary.py`, `series_roster.py`) — этот модуль лишь возвращает пусто.

Lazy `import yaml` ВНУТРИ функции: системный python3 юнит-тестов pyyaml не несёт
(контракт §1.2), а прод-venv несёт. Чистые проекции (`glossary.py`) и фильтр
company-scope тестируются на вход-словаре без YAML.

Company-scope (контракт §1.5): для встречи компании X читаются записи `scope == X`
И `scope == cross`. Кросс-термин дублируется в ОБА репо — рассинхрон ловит
`lint_cross_sync()`. Карта `company → repo` живёт здесь (не в `*-context` — иначе
курица-яйцо), переопределяема env.

Опасная тройка (CLAUDE.md проекта): знание — производные термины/роли/доменная
лексика, НЕ сырьё реплик. Тексты транскриптов сюда не попадают и не логируются.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)

# Корень, где лежат клоны репозиториев контекста. На VPS — задаётся env из
# .env.notary; на маке дефолт — ~/Projects (там клоны уже есть). Совпадает с
# логикой `auto_vocab/sources.py::me_dir` (env-override + домашний дефолт).
DEFAULT_CONTEXT_ROOT = "~/Projects"

# Поддерево бот-знания внутри репо контекста (контракт §1.1).
KNOWLEDGE_SUBPATH = "knowledge/notary"
GLOSSARY_FILE = "glossary.yaml"
ORG_STRUCTURE_FILE = "org-structure.yaml"

# Карта компания → имя репозитория контекста (контракт §1.5). Дефолт; каждую
# запись можно переопределить env `NOTARY_CONTEXT_REPO_<COMPANY>` (uppercase).
_DEFAULT_REPO_MAP = {
    "anzhee": "anzhee-context",
    "mpfirst": "mpfirst-context",
}

# Карта компания → GitHub-владелец репо контекста (Ф8). Репо двух компаний лежат
# под РАЗНЫМИ владельцами: anzhee-context — в орге `anzhee-dev`, mpfirst-context —
# в личном аккаунте `rybalkaie`. Поэтому full_repo = `<owner>/<repo>`, а git-токен
# выдаётся per-company (fine-grained PAT не покрывает двух владельцев одним токеном).
# Значения совпадают с Makefile (ANZHEE_CONTEXT_REPO/MPFIRST_CONTEXT_REPO) — там
# источник для read-bootstrap, здесь — для write-back PR. Переопределяемо env
# `NOTARY_CONTEXT_OWNER_<COMPANY>`.
_DEFAULT_OWNER_MAP = {
    "anzhee": "anzhee-dev",
    "mpfirst": "rybalkaie",
}

# Допустимые значения поля scope в glossary.yaml.
SCOPE_CROSS = "cross"


class GlossaryEntry(TypedDict, total=False):
    """Одна запись `glossary.yaml` (контракт §1.3). total=False — note/aliases опц."""

    canonical: str
    scope: str
    note: str
    aliases: list[str]
    asr_sounds_like: bool
    protocol_regex: bool


# Справочник людей компании (R18, план 2026-06-16): статусы участия + канон имени
# + алиасы. Company-level (как rosters), читается из ТОГО ЖЕ org-structure.yaml в
# верхнеуровневой секции `people:`. Нужен, чтобы:
#   • не подставлять автором того, кто больше не ходит на встречи (status=inactive
#     → выпадает из name_pool LLM-добивки и из ожидаемых; A5/R4/R5);
#   • знать каноничное написание имени (будущий замок исправлений Ф2 — R6/R10).
PERSON_STATUS_ACTIVE = "active"
PERSON_STATUS_INACTIVE = "inactive"


class PersonEntry(TypedDict, total=False):
    """Одна запись `people:` (R18). `status`/`aliases` опциональны: запись без
    status трактуется как активный участник (бэкомпат со старыми файлами)."""

    name: str
    status: str
    aliases: list[str]


# --- Резолв путей -------------------------------------------------------------


def context_root() -> Path:
    """Корень клонов `*-context`. Env `MEETING_NOTARY_CONTEXT_DIR` → дефолт ~/Projects."""
    return Path(
        os.environ.get("MEETING_NOTARY_CONTEXT_DIR", DEFAULT_CONTEXT_ROOT)
    ).expanduser()


def known_companies() -> list[str]:
    """Список компаний, для которых известен репозиторий контекста."""
    return list(_repo_map().keys())


def _repo_map() -> dict[str, str]:
    """company → repo-name с env-переопределением (`NOTARY_CONTEXT_REPO_ANZHEE=...`)."""
    out = dict(_DEFAULT_REPO_MAP)
    for company in _DEFAULT_REPO_MAP:
        override = os.environ.get(f"NOTARY_CONTEXT_REPO_{company.upper()}")
        if override and override.strip():
            out[company] = override.strip()
    return out


def repo_for_company(company: Optional[str]) -> Optional[str]:
    """Имя репо контекста для компании. Неизвестная/пустая компания → None."""
    if not company or not str(company).strip():
        return None
    return _repo_map().get(str(company).strip().lower())


def _owner_map() -> dict[str, str]:
    """company → github-owner с env-переопределением (`NOTARY_CONTEXT_OWNER_ANZHEE=...`)."""
    out = dict(_DEFAULT_OWNER_MAP)
    for company in _DEFAULT_OWNER_MAP:
        override = os.environ.get(f"NOTARY_CONTEXT_OWNER_{company.upper()}")
        if override and override.strip():
            out[company] = override.strip()
    return out


def owner_for_company(company: Optional[str]) -> Optional[str]:
    """GitHub-владелец репо контекста (орг/аккаунт). Неизвестная компания → None."""
    if not company or not str(company).strip():
        return None
    return _owner_map().get(str(company).strip().lower())


def full_repo_for_company(company: Optional[str]) -> Optional[str]:
    """`<owner>/<repo>` для компании (для `gh pr --repo` и git-remote). None, если
    неизвестен владелец ИЛИ имя репо (write-back на неизвестную компанию невозможен)."""
    owner = owner_for_company(company)
    repo = repo_for_company(company)
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def git_token_for_company(company: Optional[str], *, write: bool = False) -> Optional[str]:
    """Git-токен per-company (Ф8). fine-grained PAT привязан к одному владельцу,
    поэтому токен ищется по компании, затем — глобальный фолбэк.

    write=True (push/PR):  `NOTARY_CONTEXT_GIT_WRITE_TOKEN_<CO>` → `..._WRITE_TOKEN`.
    write=False (clone/fetch): `NOTARY_CONTEXT_GIT_TOKEN_<CO>` → `..._GIT_TOKEN`, а
      при их отсутствии — write-токен (write implies read, отдельный read-токен
      опционален). Пусто во всех слоях → None (инертно, как до провижининга)."""
    co = (str(company).strip().lower() if company and str(company).strip() else "")
    chain: list[str] = []
    if write:
        if co:
            chain.append(f"NOTARY_CONTEXT_GIT_WRITE_TOKEN_{co.upper()}")
        chain.append("NOTARY_CONTEXT_GIT_WRITE_TOKEN")
    else:
        if co:
            chain.append(f"NOTARY_CONTEXT_GIT_TOKEN_{co.upper()}")
        chain.append("NOTARY_CONTEXT_GIT_TOKEN")
        if co:
            chain.append(f"NOTARY_CONTEXT_GIT_WRITE_TOKEN_{co.upper()}")
        chain.append("NOTARY_CONTEXT_GIT_WRITE_TOKEN")
    for var in chain:
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    return None


def knowledge_dir(company: Optional[str]) -> Optional[Path]:
    """Путь к `<root>/<repo>/knowledge/notary` для компании. None, если репо неизвестно."""
    repo = repo_for_company(company)
    if not repo:
        return None
    return context_root() / repo / KNOWLEDGE_SUBPATH


# --- Чтение YAML (lazy import, graceful) --------------------------------------


def _read_yaml(path: Path) -> Optional[dict]:
    """Прочитать YAML-файл в dict. Нет pyyaml / нет файла / битый YAML → None.

    pyyaml импортируется ЛЕНИВО: системный python3 тестов его не несёт (контракт
    §1.2), и отсутствие модуля — это та же graceful degradation, что отсутствие
    файла (бот падать не должен).
    """
    try:
        import yaml  # lazy — см. docstring
    except ImportError:
        logger.info("pyyaml недоступен — знание из %s не читается (degradation)", path)
        return None
    # Защита от заглушки yaml (часть юнит-тестов делает sys.modules["yaml"]=stub):
    # без реального safe_load трактуем как «pyyaml недоступен» (degradation), а не
    # падаем на AttributeError.
    safe_load = getattr(yaml, "safe_load", None)
    if not callable(safe_load):
        logger.info("yaml без safe_load (заглушка?) — знание из %s не читается", path)
        return None
    if not path.is_file():
        logger.info("файл знания не найден, пропуск: %s", path)
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("не прочитать файл знания %s: %s", path, e)
        return None
    try:
        data = safe_load(text)
    except Exception as e:  # noqa: BLE001 — yaml.YAMLError и любой сбой парсера/заглушки
        logger.warning("битый YAML %s: %s — пропуск (degradation)", path, e)
        return None
    if not isinstance(data, dict):
        logger.warning("корень YAML %s не объект (%s) — пропуск", path, type(data).__name__)
        return None
    return data


# --- Глоссарий ----------------------------------------------------------------


def _scope_of(entry: dict) -> str:
    return str(entry.get("scope") or "").strip().lower()


def filter_glossary_by_company(entries: list[dict], company: Optional[str]) -> list[GlossaryEntry]:
    """Чистый company-scope фильтр (контракт §1.5): оставить `scope == company`
    И `scope == cross`. Записи без canonical отбрасываются.

    Пуст без YAML — тестируется инъекцией списка (Bolong[mpfirst] виден в mpfirst,
    не виден в anzhee). company=None → только cross (нет привязки к компании).
    """
    comp = (str(company).strip().lower() if company else "")
    out: list[GlossaryEntry] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        canonical = str(e.get("canonical") or "").strip()
        if not canonical:
            continue
        scope = _scope_of(e)
        if scope == SCOPE_CROSS or (comp and scope == comp):
            out.append(e)  # type: ignore[arg-type]
    return out


def _read_glossary_terms(company: Optional[str]) -> list[dict]:
    """Сырой список `terms` из glossary.yaml репо компании (без company-фильтра).

    Нужен и для company-scope загрузки, и для lint кросс-дублей (видит все scope).
    """
    kdir = knowledge_dir(company)
    if not kdir:
        return []
    data = _read_yaml(kdir / GLOSSARY_FILE)
    if not data:
        return []
    terms = data.get("terms")
    if not isinstance(terms, list):
        return []
    return [t for t in terms if isinstance(t, dict)]


def load_glossary(company: Optional[str]) -> list[GlossaryEntry]:
    """Записи glossary.yaml компании, отфильтрованные по company-scope (§1.5).

    Читает репо ИМЕННО этой компании (anzhee-context для anzhee) — он содержит
    только `scope: anzhee` + `scope: cross`, поэтому `scope: mpfirst` (напр.
    Bolong) сюда не попадает по построению; фильтр — вторая страховка.
    Graceful: нет клона/файла/yaml/битый → `[]`.
    """
    return filter_glossary_by_company(_read_glossary_terms(company), company)


def load_guidance(company: Optional[str]) -> list[str]:
    """Опциональная doc-level секция `guidance` из glossary.yaml компании (Ф8, У1/У2).

    Свободные cross-cutting правила, которые НЕ выражаются записью term→canonical
    (контекстная дизамбигуация, анти-галлюцинация чисел и т.п.) — команда может
    их вести в YAML рядом с терминами. Схема: `guidance:` — список строк. Любой не-
    список / отсутствие / нестроковые элементы → `[]` (graceful, как load_glossary).

    Это НАДСТРОЙКА над стабильным встроенным базисом `glossary.CROSS_CUTTING_GUIDANCE_BLOCK`
    (который рендерится всегда при активном YAML) — потеря базиса при активации YAML
    исключена даже если секция пустая."""
    kdir = knowledge_dir(company)
    if not kdir:
        return []
    data = _read_yaml(kdir / GLOSSARY_FILE)
    if not data:
        return []
    raw = data.get("guidance")
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


# --- Оргструктура (ростеры серий) ---------------------------------------------


def _roles_to_entries(roles: object) -> list[dict]:
    """`roles[*]` YAML → список dict (1-в-1 с `series_roster.RosterEntry`:
    name/domain/keywords). Невалидные/без имени роли отбрасываются."""
    out: list[dict] = []
    if not isinstance(roles, list):
        return out
    for r in roles:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        kws = r.get("keywords")
        keywords = [str(k).strip() for k in kws if str(k).strip()] if isinstance(kws, list) else []
        out.append({
            "name": name,
            "domain": str(r.get("domain") or "").strip(),
            "keywords": keywords,
        })
    return out


def load_org_structure(company: Optional[str]) -> dict[str, list[dict]]:
    """`{slug: [RosterEntry...]}` из org-structure.yaml репо компании.

    Контракт §1.4: `rosters[slug].roles[*]`. Graceful: нет клона/файла/yaml →
    `{}`. Пустой ростер серии (стаб МПервый) → `{slug: []}` (slug известен, ролей
    нет → у потребителя fallback S1+S2+LLM).
    """
    kdir = knowledge_dir(company)
    if not kdir:
        return {}
    data = _read_yaml(kdir / ORG_STRUCTURE_FILE)
    if not data:
        return {}
    rosters = data.get("rosters")
    if not isinstance(rosters, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for slug, spec in rosters.items():
        if not isinstance(slug, str) or not slug.strip():
            continue
        roles = spec.get("roles") if isinstance(spec, dict) else None
        out[slug.strip()] = _roles_to_entries(roles)
    return out


# --- Справочник людей компании (R18, план 2026-06-16) -------------------------


def parse_people(data: object) -> list[PersonEntry]:
    """`people:` YAML-объект → список `PersonEntry` (чистая, без IO).

    Схема (R18): `people:` — список записей `{name, status?, aliases?}`.
      • `name` обязателен и непуст (иначе запись отбрасывается);
      • `status` нормализуется в lowercase; всё, что не `inactive`, считается
        `active` — бэкомпат: СТАРАЯ запись без `status` = активный участник, и
        неизвестное значение не «выключает» человека по ошибке;
      • `aliases` — список непустых строк (нет/не список → `[]`).

    Принимает как весь dict org-structure.yaml (берёт ключ `people`), так и уже
    извлечённый список — удобно тестам инъекцией без YAML (как
    `filter_glossary_by_company`).
    """
    if isinstance(data, dict):
        raw = data.get("people")
    else:
        raw = data
    if not isinstance(raw, list):
        return []
    out: list[PersonEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        status_raw = str(item.get("status") or "").strip().lower()
        status = PERSON_STATUS_INACTIVE if status_raw == PERSON_STATUS_INACTIVE else PERSON_STATUS_ACTIVE
        aliases_raw = item.get("aliases")
        aliases = (
            [str(a).strip() for a in aliases_raw if str(a).strip()]
            if isinstance(aliases_raw, list)
            else []
        )
        out.append({"name": name, "status": status, "aliases": aliases})
    return out


def load_people(company: Optional[str]) -> list[PersonEntry]:
    """Справочник людей компании из `people:` org-structure.yaml. Graceful: нет
    клона/файла/yaml/секции → `[]` (как `load_org_structure`)."""
    kdir = knowledge_dir(company)
    if not kdir:
        return []
    data = _read_yaml(kdir / ORG_STRUCTURE_FILE)
    if not data:
        return []
    return parse_people(data)


def inactive_person_names(company: Optional[str]) -> set[str]:
    """Множество ИМЁН (канон + алиасы) людей со статусом `inactive` у компании.

    Для сужения `name_pool` LLM-добивки и чистки ожидаемых (R4/R5): такой человек
    больше не ходит на встречи, автором его не подставляем. Алиасы включены, чтобы
    короткое/искажённое написание неактивного тоже отсеялось. Сравнение СТРОГОЕ
    (точное, case-insensitive) — НЕ тёзко-первословное: иначе bare «Михаил» отсёкся
    бы как неактивный Еремеев и затёр присутствующего активного Саргина."""
    out: set[str] = set()
    for p in load_people(company):
        if p.get("status") != PERSON_STATUS_INACTIVE:
            continue
        name = str(p.get("name") or "").strip()
        if name:
            out.add(name.lower())
        for a in p.get("aliases") or []:
            al = str(a).strip()
            if al:
                out.add(al.lower())
    return out


def is_name_inactive(name: Optional[str], company: Optional[str]) -> bool:
    """Имя принадлежит неактивному человеку компании (строгое точное совпадение по
    канону/алиасу). Пустое имя / неизвестная компания → False (инертно)."""
    n = (str(name).strip().lower() if name else "")
    if not n:
        return False
    return n in inactive_person_names(company)


def company_for_series(series_slug: Optional[str]) -> Optional[str]:
    """Компания серии по её slug — поиск slug по оргструктурам всех компаний.

    До Ф6 (поле `company` в разметке серии) это единственный источник привязки
    серии к компании. Slug уникален (папка серии) → первое совпадение корректно.
    Нет совпадения / нет YAML → None (вызыватель уходит в fallback на встроенное).
    """
    if not series_slug or not str(series_slug).strip():
        return None
    slug = str(series_slug).strip()
    for company in known_companies():
        if slug in load_org_structure(company):
            return company
    return None


def roster_for_series(series_slug: Optional[str]) -> list[dict]:
    """Ростер серии из оргструктуры (поиск по всем компаниям). Нет → `[]`.

    Тело `series_roster.get_roster` (ЗАВ1): источник `_STATIC_ROSTERS` → этот
    YAML-поиск; при пустом результате get_roster падает на встроенный хардкод
    (graceful, поведение как до Ф5).
    """
    if not series_slug or not str(series_slug).strip():
        return []
    slug = str(series_slug).strip()
    for company in known_companies():
        os_map = load_org_structure(company)
        if slug in os_map and os_map[slug]:
            return os_map[slug]
    return []


# --- Lint синхронности кросс-дублей (контракт §1.5) ---------------------------


def _cross_signature(entries: list[dict]) -> dict[str, list[str]]:
    """{canonical: sorted(aliases)} для записей scope==cross. Ключ дедупа — для
    сверки, что набор кросс-терминов идентичен в обоих репо."""
    sig: dict[str, list[str]] = {}
    for e in entries:
        if _scope_of(e) != SCOPE_CROSS:
            continue
        canonical = str(e.get("canonical") or "").strip()
        if not canonical:
            continue
        aliases = e.get("aliases")
        al = sorted(str(a).strip() for a in aliases if str(a).strip()) if isinstance(aliases, list) else []
        sig[canonical] = al
    return sig


def lint_cross_sync_signatures(by_company: dict[str, list[dict]]) -> list[str]:
    """Чистый lint (без YAML): набор `cross`-записей во всех компаниях обязан
    совпадать по `canonical`+`aliases`. Возвращает список расхождений (пусто = ОК).

    Принимает {company: [raw terms]} — тестируется инъекцией. Боевую обёртку даёт
    `lint_cross_sync()` (читает YAML каждого репо).
    """
    companies = list(by_company.keys())
    if len(companies) < 2:
        return []
    sigs = {c: _cross_signature(by_company[c]) for c in companies}
    base_company = companies[0]
    base = sigs[base_company]
    problems: list[str] = []
    for other in companies[1:]:
        other_sig = sigs[other]
        for canonical in sorted(set(base) | set(other_sig)):
            in_base = canonical in base
            in_other = canonical in other_sig
            if in_base and not in_other:
                problems.append(f"cross-термин «{canonical}» есть в {base_company}, нет в {other}")
            elif in_other and not in_base:
                problems.append(f"cross-термин «{canonical}» есть в {other}, нет в {base_company}")
            elif base.get(canonical) != other_sig.get(canonical):
                problems.append(
                    f"cross-термин «{canonical}»: aliases расходятся "
                    f"({base_company}={base.get(canonical)} vs {other}={other_sig.get(canonical)})"
                )
    return problems


def lint_cross_sync() -> list[str]:
    """Боевой lint: читает glossary.yaml каждого известного репо и сверяет
    кросс-дубли. Нет YAML у репо → его кросс-набор пуст (рассинхрон всплывёт)."""
    by_company = {c: _read_glossary_terms(c) for c in known_companies()}
    # Репо, у которых файл вовсе не читается (нет клона/pyyaml), из сверки
    # исключаем — иначе degradation выглядит как рассинхрон. Сверяем только те,
    # где термины реально прочитаны.
    present = {c: terms for c, terms in by_company.items() if terms}
    if len(present) < 2:
        return []
    return lint_cross_sync_signatures(present)
