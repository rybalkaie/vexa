"""Ф2 (план 2026-06-16, ISS-11): COMPANY-level замок исправлений имён/ролей.

Когда владелец поправляет имя/авторство/роль («сервис → Саргин», «не Еремеев, а
Саргин», «Саргин отвечает за сервис»), это должно держаться **навсегда на уровне
КОМПАНИИ**: реприменяться на каждой регенерации текущей встречи (R12) И на будущих
встречах любой серии этой компании (R6), кумулятивно (R7), с last-write-wins (R10).

Архитектура (РИСК3 — это НОВЫЙ путь, не чистый «реюз»):
  • durable-факт пишется в ЛОКАЛЬНЫЙ overlay-слой бота (`_corrections/<company>.json`),
    НЕ в боевой клон `*-context` — тот под launchd-автосинком (auto-memory
    `context-repos-autosync-to-main`): любая правка файла там = авто-пуш в общий main
    БЕЗ PR = нарушение R11. Overlay читается `merge_roster`/`apply_name_canon` ПОВЕРХ
    YAML-ростера (`series_roster.get_roster` / `context_knowledge.*`);
  • в общую базу команды факт уезжает РАЗВЯЗАННО — через существующий PR-механизм
    `knowledge_writeback.propose_role` (company-outbox → ветка + PR, ревью команды).
    Это R11: локально применяется СРАЗУ, в `*-context` — через ревью, не мгновенно
    в main. Team-share за гейтом провижининга Ф8 (нет write-токена → копится в
    outbox, локальное применение этим НЕ блокируется).

Хранилище — по образцу `feedback_state`/`clarify_state` (атомарная запись
mkstemp+fsync+rename, graceful чтение). Last-write-wins резолвится на ЧТЕНИИ по
полю `ts`: append-only лог фактов, при чтении свежий по ключу перекрывает старый.

Дисциплина «Опасной тройки» (CLAUDE.md проекта meeting-notary): НЕ логируем текст
реплик/правок — только счётчики (число фактов, число применённых). В факт кладём
лишь имя/домен/каноничное написание — не сырьё реплик. Имена и так фигурируют в
составе участников.

Модуль stdlib-only (импортируется из listener'а под venv-cli без pyannote/torch);
`knowledge_writeback` тянется ЛЕНИВО внутри team-share (он несёт свои зависимости).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Локальный overlay-слой бота. НЕ боевой клон `*-context` (тот под автосинком).
DEFAULT_LOCAL_ROOT = os.path.expanduser("~/Projects/meeting-notary/_corrections")
DEFAULT_VPS_ROOT = "/opt/meeting-notary/_corrections"

STORE_SUFFIX = "-corrections.json"

KIND_ROLE = "role"   # домен/зона → каноничное имя ответственного (R6 «сервис→Саргин»)
KIND_NAME = "name"   # каноничное переименование: wrong → right (R8 «не A, а B»)

_SAFE_COMPANY_RE = re.compile(r"[^a-z0-9_-]")


# ---------------------------------------------------------------------------
# Резолв путей хранилища (как feedback_state.resolve_feedback_dir)
# ---------------------------------------------------------------------------

def resolve_corrections_dir(*, override: Optional[str] = None) -> Path:
    """Корень `_corrections/`.

    Приоритет: явный аргумент > env `MEETING_NOTARY_CORRECTIONS_DIR` > дефолт
    (VPS если `/opt/meeting-notary` существует, иначе мак-путь). Совпадает с
    конвенцией `feedback_state`/`clarify_state`.
    """
    if override:
        return Path(os.path.expanduser(override))
    env = os.environ.get("MEETING_NOTARY_CORRECTIONS_DIR")
    if env:
        return Path(os.path.expanduser(env))
    if Path("/opt/meeting-notary").is_dir():
        return Path(DEFAULT_VPS_ROOT)
    return Path(DEFAULT_LOCAL_ROOT)


def _safe_company(company: Optional[str]) -> Optional[str]:
    """Нормализованный slug компании для имени файла (`anzhee`). Пусто/мусор → None."""
    if not company or not str(company).strip():
        return None
    slug = _SAFE_COMPANY_RE.sub("", str(company).strip().lower())
    return slug or None


def path_for(company: Optional[str], *, root: Optional[Path] = None) -> Optional[Path]:
    """Путь к файлу overlay компании (`<root>/<company>-corrections.json`). Нет
    валидной компании → None (инертно: ни читать, ни писать некуда)."""
    slug = _safe_company(company)
    if not slug:
        return None
    root = root or resolve_corrections_dir()
    return root / f"{slug}{STORE_SUFFIX}"


# ---------------------------------------------------------------------------
# Чтение / запись overlay (атомарно, graceful)
# ---------------------------------------------------------------------------

def load_correction_facts(company: Optional[str], *, root: Optional[Path] = None) -> list[dict]:
    """Сырой append-only список фактов компании (в порядке записи). Нет файла /
    битый JSON / нет компании → `[]` (graceful, как загрузчики знания)."""
    p = path_for(company, root=root)
    if not p or not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[corrections] read %s failed: %s", p, e)
        return []
    if not isinstance(data, list):
        logger.warning("[corrections] %s root not a list — ignored", p)
        return []
    return [f for f in data if isinstance(f, dict)]


def _atomic_write_facts(company: str, facts: list[dict], *, root: Optional[Path] = None) -> Path:
    """Атомарная перезапись overlay (mkstemp + fsync + os.rename) — как feedback_state."""
    p = path_for(company, root=root)
    if p is None:  # pragma: no cover — defensive
        raise ValueError(f"cannot resolve corrections path for company {company!r}")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.stem}.", suffix=".json.tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(facts, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, p)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return p


def _append_fact(company: str, fact: dict, *, root: Optional[Path] = None) -> bool:
    """Дописать один факт в overlay компании (R7 — кумулятивно, не затирая прежних).

    last-write-wins НЕ применяется на записи: лог append-only, свежий по ключу
    перекрывает старый только на ЧТЕНИИ (`resolved_facts`). Так история правок не
    теряется и порядок ts остаётся восстановимым. Возвращает True при успехе.
    """
    slug = _safe_company(company)
    if not slug:
        return False
    facts = load_correction_facts(company, root=root)
    facts.append(fact)
    try:
        _atomic_write_facts(slug, facts, root=root)
    except OSError as e:
        logger.warning("[corrections] write company=%s failed: %s", slug, e)
        return False
    return True


# ---------------------------------------------------------------------------
# Запись фактов (локально сразу) + развязанный team-share через PR (R11)
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def record_role_fact(
    company: Optional[str],
    name: str,
    domain: str,
    *,
    keywords: Optional[list[str]] = None,
    series: Optional[str] = None,
    date: Optional[str] = None,
    ts: Optional[float] = None,
    root: Optional[Path] = None,
    share: bool = True,
) -> bool:
    """R6/R10: durable company-факт «домен → каноничное имя ответственного».

    Пишется ЛОКАЛЬНО сразу (overlay) — влияет на ближайший перевыпуск и будущие
    встречи. `share=True` дополнительно ставит факт в очередь team-share (PR в
    `*-context`) через `knowledge_writeback.propose_role` — РАЗВЯЗАННО (R11): в
    общую базу через ревью, не мгновенно в main. Graceful: нет write-токена →
    копится в outbox; ошибка share не валит локальную запись.
    """
    name = (name or "").strip()
    domain = (domain or "").strip()
    slug = _safe_company(company)
    if not slug or not name or not domain:
        return False
    kw = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kw:
        kw = _domain_keywords(domain)
    fact = {
        "kind": KIND_ROLE,
        "domain": domain,
        "name": name,
        "keywords": kw,
        "ts": float(ts) if ts is not None else _now(),
    }
    if series:
        fact["series"] = str(series)
    if date:
        fact["date"] = str(date)
    ok = _append_fact(slug, fact, root=root)
    if ok:
        logger.info("[corrections] company=%s recorded role fact (domain set)", slug)
    if ok and share:
        _share_role_via_pr(slug, name, domain, keywords=kw, series=series)
    return ok


def record_name_fact(
    company: Optional[str],
    wrong: str,
    right: str,
    *,
    series: Optional[str] = None,
    date: Optional[str] = None,
    ts: Optional[float] = None,
    root: Optional[Path] = None,
) -> bool:
    """R8/R10/R12: durable company-факт каноничного переименования «wrong → right».

    Применяется как rename поверх любого источника имени (`apply_name_canon`) +
    `wrong` исключается из пула LLM-добивки (`excluded_names`). Локально сразу.
    Team-share имени-канона идёт НЕ ролью, а как термин/инсайт оргструктуры —
    в Ф2 кладём только локально (имя-канон в `people:` боевого YAML — owner-PR,
    FU-1), чтобы не плодить полу-структурный role-PR без домена.
    """
    wrong = (wrong or "").strip()
    right = (right or "").strip()
    slug = _safe_company(company)
    if not slug or not wrong or not right or wrong.lower() == right.lower():
        return False
    fact = {
        "kind": KIND_NAME,
        "wrong": wrong,
        "name": right,
        "ts": float(ts) if ts is not None else _now(),
    }
    if series:
        fact["series"] = str(series)
    if date:
        fact["date"] = str(date)
    ok = _append_fact(slug, fact, root=root)
    if ok:
        logger.info("[corrections] company=%s recorded name canon fact", slug)
    return ok


def _share_role_via_pr(
    company: str, name: str, domain: str, *, keywords: Optional[list[str]], series: Optional[str]
) -> None:
    """R11: развязанный team-share роли через существующий PR-механизм. Лениво
    импортируем `knowledge_writeback` (несёт свои зависимости). Любой сбой — мягкий
    (локальная запись уже сделана; в `*-context` уедет позже)."""
    try:
        from . import knowledge_writeback as kw  # lazy
    except Exception as e:  # noqa: BLE001 — импорт может тянуть отсутствующее окружение
        logger.info("[corrections] team-share skipped (writeback unavailable): %s", e)
        return
    try:
        kw.propose_role(
            name, domain,
            series=series, company=company, keywords=keywords,
            publication_allowed=True,  # company-факт явно одобрен правкой владельца
            source={"origin": "correction-lock", "company": company},
        )
    except Exception as e:  # noqa: BLE001
        logger.info("[corrections] team-share enqueue failed (kept local): %s", e)


# ---------------------------------------------------------------------------
# Резолв last-write-wins (R10) + проекции для маппинга
# ---------------------------------------------------------------------------

def _norm(s: object) -> str:
    return str(s or "").strip().lower()


def _fact_key(fact: dict) -> Optional[tuple[str, str]]:
    """Ключ дедупа last-write-wins. role → (role, домен); name → (name, wrong)."""
    kind = _norm(fact.get("kind"))
    if kind == KIND_ROLE:
        d = _norm(fact.get("domain"))
        return (KIND_ROLE, d) if d else None
    if kind == KIND_NAME:
        w = _norm(fact.get("wrong"))
        return (KIND_NAME, w) if w else None
    return None


def resolved_facts(company: Optional[str], *, root: Optional[Path] = None) -> list[dict]:
    """Факты компании после last-write-wins (R10): по каждому ключу — самый свежий
    по `ts`. Порядок результата стабилен (по ts возрастанию). R7 кумулятивность
    обеспечивается тем, что РАЗНЫЕ ключи сосуществуют."""
    raw = load_correction_facts(company, root=root)
    # Сортируем по ts (отсутствие ts → 0), затем по индексу для стабильности.
    indexed = sorted(
        enumerate(raw),
        key=lambda iv: (float(iv[1].get("ts") or 0.0), iv[0]),
    )
    latest: dict[tuple[str, str], dict] = {}
    for _, fact in indexed:
        key = _fact_key(fact)
        if key is None:
            continue
        latest[key] = fact  # позже по ts перекрывает (R10)
    return [latest[k] for k in sorted(latest, key=lambda k: float(latest[k].get("ts") or 0.0))]


def role_facts(company: Optional[str], *, root: Optional[Path] = None) -> list[dict]:
    return [f for f in resolved_facts(company, root=root) if _norm(f.get("kind")) == KIND_ROLE]


def name_facts(company: Optional[str], *, root: Optional[Path] = None) -> list[dict]:
    return [f for f in resolved_facts(company, root=root) if _norm(f.get("kind")) == KIND_NAME]


def corrections_to_roster_entries(
    company: Optional[str], *, root: Optional[Path] = None
) -> list[dict]:
    """Проекция role-фактов в записи ростера `{name, domain, keywords}` — формат
    `series_roster.RosterEntry`, чтобы лечь в `map_from_roster_domain`."""
    out: list[dict] = []
    for f in role_facts(company, root=root):
        name = str(f.get("name") or "").strip()
        domain = str(f.get("domain") or "").strip()
        if not name or not domain:
            continue
        kws = [str(k).strip() for k in (f.get("keywords") or []) if str(k).strip()]
        out.append({"name": name, "domain": domain, "keywords": kws or _domain_keywords(domain)})
    return out


def merge_roster(
    base_roster: Optional[list[dict]], company: Optional[str], *, root: Optional[Path] = None
) -> list[dict]:
    """R6/R10: наложить company-факты ПОВЕРХ ростера серии.

    Факт по домену D перекрывает запись базового ростера с тем же доменом
    (last-write-wins — выученное «сервис→Саргин» бьёт устаревший YAML «сервис→
    Еремеев»); новый домен — добавляется. Возвращает НОВЫЙ список (вход не мутирует).
    Нет фактов → возвращает базовый ростер как есть (поведение как до Ф2).

    Важно: при перекрытии существующего домена меняем ТОЛЬКО имя ответственного, а
    доменную ЛЕКСИКУ (`keywords`) СОХРАНЯЕМ из базового ростера (объединяя с
    keyword'ами факта). Иначе захваченный из короткой правки факт нёс бы лишь 1
    ключевое слово — НИЖЕ порога `map_from_roster_domain` (=2), и доменный матч
    перестал бы срабатывать. Правка «кто отвечает», не «какая лексика у домена».
    Порядок записей стабилен: базовые домены в их порядке, затем новые.
    """
    base = list(base_roster or [])
    overlay = {_norm(f.get("domain")): f for f in role_facts(company, root=root)
               if _norm(f.get("domain")) and str(f.get("name") or "").strip()}
    if not overlay:
        return base
    by_domain: dict[str, dict] = {}
    order: list[str] = []
    for e in base:
        d = _norm(e.get("domain"))
        if not d:
            continue
        by_domain[d] = dict(e)
        if d not in order:
            order.append(d)
    for d, fact in overlay.items():
        name = str(fact.get("name") or "").strip()
        fact_kw = [str(k).strip() for k in (fact.get("keywords") or []) if str(k).strip()]
        if d in by_domain:
            base_kw = [str(k).strip() for k in (by_domain[d].get("keywords") or []) if str(k).strip()]
            merged_kw = list(dict.fromkeys(base_kw + fact_kw))  # сохранить богатую лексику базы
            by_domain[d] = {
                "name": name,
                "domain": by_domain[d].get("domain") or d,
                "keywords": merged_kw,
            }
        else:
            by_domain[d] = {
                "name": name,
                "domain": fact.get("domain") or d,
                "keywords": fact_kw or _domain_keywords(d),
            }
            order.append(d)
    no_domain = [e for e in base if not _norm(e.get("domain"))]
    return no_domain + [by_domain[d] for d in order]


def name_canon_map(company: Optional[str], *, root: Optional[Path] = None) -> dict[str, str]:
    """{wrong_lower: right} из name-фактов (R8/R10) для rename поверх маппинга."""
    out: dict[str, str] = {}
    for f in name_facts(company, root=root):
        wrong = _norm(f.get("wrong"))
        right = str(f.get("name") or "").strip()
        if wrong and right:
            out[wrong] = right
    return out


def apply_name_canon(
    cluster_to_name: dict[str, str], company: Optional[str], *, root: Optional[Path] = None
) -> dict[str, str]:
    """R12/R6: переименовать значения маппинга `cluster→name` по company name-канону.

    Матч СТРОГИЙ (точное имя, case-insensitive) — НЕ тёзко-первословный: иначе bare
    «Михаил» мог бы переименоваться в канон неверного тёзки и затереть присутствующего
    (тот же инвариант, что `inactive_person_names`). Возвращает НОВЫЙ dict.
    """
    canon = name_canon_map(company, root=root)
    if not canon:
        return dict(cluster_to_name)
    out: dict[str, str] = {}
    for cluster, name in (cluster_to_name or {}).items():
        out[cluster] = canon.get(_norm(name), name)
    return out


def excluded_names(company: Optional[str], *, root: Optional[Path] = None) -> set[str]:
    """Множество (lowercase) «отвергнутых» имён для добавления к `inactive_names`
    (LLM-добивка/дизамбигуация их не предлагают).

    ТАРГЕТИРОВАННО на тёзка-путаницу (корень ISS-11: Еремеев↔Саргин — оба «Михаил»):
    исключаем `wrong` ТОЛЬКО когда он тёзка `right` (общее первое слово). Иначе —
    `wrong` может быть валидным участником, ошибочно названным в одном месте, и
    глушить его на будущих встречах вредно (его всё равно подстрахует rename
    `apply_name_canon`: любой источник, выдавший `wrong`, переименуется в `right`).
    """
    out: set[str] = set()
    for f in name_facts(company, root=root):
        wrong = _norm(f.get("wrong"))
        right = _norm(f.get("name"))
        if not wrong or not right:
            continue
        w_first = wrong.split()[0] if wrong.split() else wrong
        r_first = right.split()[0] if right.split() else right
        if w_first == r_first:  # тёзки (общее имя) → глушим неверного тёзку
            out.add(wrong)
    return out


# ---------------------------------------------------------------------------
# Захват правок из текста владельца (R8) — расширенный
# ---------------------------------------------------------------------------
# Парсер light, stdlib (listener на системном python3 без pymorphy3): склонения
# матчим суффикс-стеммером `_word_stem`, не морфологией. Незнакомую формулировку
# НЕ трогаем (фолбэк на прежний контентный путь — не регресс).

# Имя-токен: слово, начинающееся с буквы (Unicode), допускает дефис (Набережная-2 —
# нет, но «Илья-» — нет). Двусловные имена резолвятся по словам против пула.
_NAME_TOK = r"([^\W\d_][\w-]*)"
# Хвостовые символы, срезаемые стеммером: гласные + мягкий/твёрдый знак + «й»
# (инструменталь «-ой/-ей»: «Ольгой»→«ольг», «Сергей»→«серге»).
_VOWELS_SOFT = set("аеёиоуыэюяьъй")


def _word_stem(word: str) -> str:
    """Грубый стем под склонения без pymorphy3: lowercase + срез хвостовых
    гласных/мягкого знака/«й» (до ≥3 символов). «Еремеева»→«еремеев», «Марию»/
    «Мария»→«мар», «Татьяну»/«Татьяна»→«татьян», «Ольгой»/«Ольга»→«ольг».
    Достаточно для матча падежных форм против каноничных имён участников."""
    w = re.sub(r"[^\w-]", "", str(word or "").strip().lower())
    while len(w) > 3 and w[-1] in _VOWELS_SOFT:
        w = w[:-1]
    return w


def _name_stems(name: str) -> set[str]:
    """Стемы всех слов имени (для тёзко-безопасного матча по словам)."""
    return {_word_stem(p) for p in str(name or "").split() if _word_stem(p)}


def resolve_known_name(token: str, known_names: Optional[list[str]]) -> Optional[str]:
    """Сопоставить склонённый токен («Еремеева») каноничному имени из пула участников.

    Матч по стему слова: токен совпадает, если его стем равен стему какого-то слова
    каноничного имени. Неоднозначность (стем совпал с >1 разными именами) → None
    (не угадываем вслепую — тёзка-безопасность). Пустой пул → None."""
    tok = _word_stem(token)
    if not tok or not known_names:
        return None
    hits: list[str] = []
    for cand in known_names:
        if not cand or not str(cand).strip():
            continue
        if tok in _name_stems(cand):
            if cand not in hits:
                hits.append(cand)
    return hits[0] if len(hits) == 1 else None


def parse_absent_names(text: str, *, known_names: Optional[list[str]] = None) -> list[str]:
    """R9: «кого не было» из текста правки → каноничные имена (по пулу участников).

    Формы (вкл. склонения): «X не было», «X не была/не был», «без X», «X отсутствовал
    /отсутствовала», «X не пришёл/не пришла», «X не участвовал». Возвращает список
    каноничных имён (ТОЛЬКО резолвящихся в пуле — иначе пропуск). НЕ персистится —
    это негативный слой ТЕКУЩЕЙ встречи (решение владельца R9/A3)."""
    if not text or not text.strip():
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        canon = resolve_known_name(tok, known_names)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)

    # «<Имя> не было / не была / не был / не пришёл / не участвовал / отсутствовал…»
    for m in re.finditer(
        r"(?i)" + _NAME_TOK + r"\s+(?:не\s+был[оа]?|не\s+приш[ёе]л[а]?|"
        r"не\s+участвовал[а]?|отсутствовал[а]?)\b", text
    ):
        _add(m.group(1))
    # «без <Имя>» (родительный) — «совещание прошло без Еремеева».
    for m in re.finditer(r"(?i)\bбез\s+" + _NAME_TOK, text):
        _add(m.group(1))
    return out


def parse_correction_facts(
    text: str, *, known_names: Optional[list[str]] = None
) -> list[dict]:
    """R8: расширенный захват durable-правок имени/авторства/роли из текста владельца.

    Возвращает список типизированных корректировок:
      • {kind: "name", wrong, right}     — «не A, а B» / «A это B» / «A → B»
      • {kind: "swap", a, b}             — «перепутал A и B» / «A и B местами»
      • {kind: "role", name, domain}     — «B отвечает за X» / «за X отвечает B» /
                                            «B ведёт X» / «X — зона B»
    Склонения резолвятся `resolve_known_name` против `known_names` (если задан);
    без пула имена берутся surface-form (как написаны). domain — lowercase-слово.
    Незнакомую формулировку не возвращаем (фолбэк на прежний контентный путь).
    """
    if not text or not text.strip():
        return []
    out: list[dict] = []

    def _name(tok: str) -> Optional[str]:
        if known_names:
            r = resolve_known_name(tok, known_names)
            if r:
                return r
            return None  # есть пул, но не резолвится → не durable-имя (тёзка-безопасно)
        t = str(tok or "").strip()
        return t or None

    def _push(fact: dict) -> None:
        if fact not in out:
            out.append(fact)

    # A. Роль: «<Имя> отвеча(ет/л/ла) за <домен>», «<Имя> ведёт/курирует <домен>».
    for m in re.finditer(
        r"(?i)" + _NAME_TOK + r"\s+(?:отвеча\w+\s+за|ведёт|ведет|курир\w+|"
        r"отвеча\w+\s+по)\s+" + _NAME_TOK, text
    ):
        nm = _name(m.group(1))
        dom = str(m.group(2) or "").strip().lower()
        if nm and dom:
            _push({"kind": KIND_ROLE, "name": nm, "domain": dom})
    # A'. Роль инверсная: «за <домен> отвеча(ет) <Имя>».
    for m in re.finditer(
        r"(?i)\bза\s+" + _NAME_TOK + r"\s+отвеча\w+\s+" + _NAME_TOK, text
    ):
        dom = str(m.group(1) or "").strip().lower()
        nm = _name(m.group(2))
        if nm and dom:
            _push({"kind": KIND_ROLE, "name": nm, "domain": dom})

    # B. Имя: «(это) не A, (а|это) B».
    for m in re.finditer(
        r"(?i)\bне\s+" + _NAME_TOK + r"\s*,?\s*(?:а|это)\s+" + _NAME_TOK, text
    ):
        wrong = _name(m.group(1))
        right = _name(m.group(2))
        if wrong and right and _norm(wrong) != _norm(right):
            _push({"kind": KIND_NAME, "wrong": wrong, "right": right})
    # C. Имя стрелкой/равенством: «A -> B» / «A = B» (но не если левый — это «Спикер»).
    for m in re.finditer(
        r"(?i)" + _NAME_TOK + r"\s*(?:=>|->|→|—>|=)\s*" + _NAME_TOK, text
    ):
        if str(m.group(1)).strip().lower() == "спикер":
            continue
        wrong = _name(m.group(1))
        right = _name(m.group(2))
        if wrong and right and _norm(wrong) != _norm(right):
            _push({"kind": KIND_NAME, "wrong": wrong, "right": right})

    # D. Своп: «перепутал(и) A и B» / «A и B местами/наоборот/перепутаны».
    if re.search(r"(?i)перепута|помен[яе]|наоборот|местами", text):
        toks: list[str] = []
        for m in re.finditer(_NAME_TOK, text):
            w = m.group(1)
            # пропускаем служебные слова, что попали под имя-токен
            if w.lower() in (
                "перепутал", "перепутали", "перепутаны", "поменяй", "поменяйте",
                "поменяли", "наоборот", "местами", "и", "а", "это", "ты", "не", "за",
            ):
                continue
            nm = _name(w)
            if nm and nm not in toks:
                toks.append(nm)
            if len(toks) == 2:
                break
        if len(toks) == 2:
            _push({"kind": "swap", "a": toks[0], "b": toks[1]})

    return out


def _domain_keywords(domain: str) -> list[str]:
    """Стем доменного слова под substring-матч `map_from_roster_domain` (keywords
    там — lowercase-стемы). «поставки»→[«поставк»], «сервис»→[«сервис»]. Для
    одно-словного домена даём и стем, и полную форму (страховка точного матча)."""
    d = str(domain or "").strip().lower()
    if not d:
        return []
    stem = _word_stem(d)
    out = [stem]
    if d != stem:
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Высокоуровневая запись из захвата (роутинг durable vs current-meeting)
# ---------------------------------------------------------------------------

def _keywords_from_roster(domain: str, roster: Optional[list[dict]]) -> list[str]:
    """Лексика домена из ростера серии (для обогащения захваченного role-факта —
    короткая правка несёт лишь слово-домен, ростер несёт полные стемы). Нет ростера/
    домена → []."""
    d = _norm(domain)
    if not d or not roster:
        return []
    for e in roster:
        if _norm(e.get("domain")) == d:
            return [str(k).strip() for k in (e.get("keywords") or []) if str(k).strip()]
    return []


def record_facts_from_text(
    company: Optional[str],
    text: str,
    *,
    known_names: Optional[list[str]] = None,
    series: Optional[str] = None,
    date: Optional[str] = None,
    roster: Optional[list[dict]] = None,
    root: Optional[Path] = None,
    share: bool = True,
) -> dict:
    """Распарсить правку владельца и записать DURABLE company-факты (role/name).

    Записываем ТОЛЬКО то, что осмысленно держать на уровне КОМПАНИИ навсегда:
      • role («B отвечает за X») — зона ответственности компании;
      • name-канон («не A, а B») — каноничное написание/замена имени.
    НЕ записываем как durable:
      • swap («перепутал A и B») — это перестановка авторства ИМЕННО ЭТОЙ встречи
        (диаризация местами поменяла два голоса), а не company-канон. Своп держит
        per-meeting механизм `feedback_reissue.parse_authorship_remap` (remap текста
        транскрипта) — переносить его на будущие встречи было бы вредно (R15: масштаб
        правки по тексту). Парсер его всё равно ВОЗВРАЩАЕТ (R8: капча формулировки) —
        не персистим здесь.
      • absent («A не было») — негативный слой ТЕКУЩЕЙ встречи (R9/A3), собирает
        `parse_absent_names`, НЕ персистится вперёд.

    Возвращает счётчики `{role, name}` записанных durable-фактов (лог без текста —
    опасная тройка).
    """
    counts = {"role": 0, "name": 0}
    slug = _safe_company(company)
    if not slug:
        return counts
    for fact in parse_correction_facts(text, known_names=known_names):
        kind = fact.get("kind")
        if kind == KIND_ROLE:
            # Обогащаем лексику домена из ростера серии (захват несёт лишь слово-
            # домен; ростер — полные стемы), чтобы durable-факт прошёл доменный порог.
            kw = _keywords_from_roster(fact["domain"], roster)
            if record_role_fact(
                slug, fact["name"], fact["domain"],
                keywords=kw or None,
                series=series, date=date, root=root, share=share,
            ):
                counts["role"] += 1
        elif kind == KIND_NAME:
            if record_name_fact(
                slug, fact["wrong"], fact["right"],
                series=series, date=date, root=root,
            ):
                counts["name"] += 1
        # kind == "swap" → per-meeting, не durable (см. докстринг).
    return counts
