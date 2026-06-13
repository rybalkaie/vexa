"""Ф7 D4: ратчет private→company по команде владельца с ЗАПОМИНАНИЕМ правила.

Дефолт безопасности (D3) — авто-выученное знание уходит ПРИВАТНО (в `me/`). Но
владелец может скомандовать «переноси это в контекст компании» — тогда (а) текущий
факт уезжает в `*-context`, и (б) ВПРЕДЬ знание такого же РОДА (kind) для этой
компании роутится в `*-context` без переспроса. Это и есть ратчет: один раз
разрешил — запомнили направление.

Почему «ратчет» (только вверх, private→company, не наоборот): повышение делает
ТОЛЬКО владелец явной командой; автоматика сама вверх не повышает (fail-closed).
Понижения обратно не запоминаем — если владелец передумал, он удалит правило/файл
(или мы добавим обратную команду отдельно); по умолчанию память лишь РАСШИРЯЕТ
доверие, а не сужает приватность скрытно.

Хранилище: `knowledge_ratchet.json` рядом с auto_vocab-state (тот же `config/` на
VPS), под flock — как `auto_vocab/state` / `feedback_state`. Пер-kind правила
(«впредь такие — туда») + пер-key правила (конкретный факт). Команда владельца —
текстовый реплай, разбирается `parse_promote_command`.

🔴 Секреты сюда НЕ кладём (D5): ратчет хранит kind/company/нормализованный ключ
знания (термин/роль), не сырьё и не креды; вызыватель уже отфильтровал креды.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Виды знания, которыми оперирует ратчет (совпадают с `knowledge_router`).
KIND_TERM = "term"
KIND_ROSTER_ROLE = "roster-role"
# Ф9 (B4): durable-факт уровня второго мозга (стратегия/позиционирование/экономика/
# проверенный вывод/веха). Ратчет/keep-private работают по нему так же, как по
# терминам/ролям — логика kind-агностична, добавлена лишь именованная константа.
KIND_INSIGHT = "insight"

# Спец-значение company в пер-kind правиле: «любая компания» (владелец не уточнил).
COMPANY_ANY = "*"


def _state_dir() -> Path:
    """Каталог состояния нотариуса — тот же, где auto_vocab-state и vocab
    (`config/` на VPS). Берём из `SPEECHMATICS_VOCAB_PATH` (как `auto_vocab/state`)."""
    vocab = os.environ.get(
        "SPEECHMATICS_VOCAB_PATH",
        "/srv/meeting-notary/config/speechmatics-vocab.json",
    )
    return Path(vocab).expanduser().parent


def state_path() -> Path:
    env = os.environ.get("NOTARY_KNOWLEDGE_RATCHET_PATH")
    if env:
        return Path(env).expanduser()
    return _state_dir() / "knowledge_ratchet.json"


def _empty_state() -> dict:
    return {
        "_comment": (
            "Ф7 D4: ратчет private→company. promote_kinds: впредь знание этого рода "
            "для этой компании роутится в *-context. promote_keys: конкретный факт. "
            "Повышает владелец командой «переноси в контекст»; автоматика сама не "
            "повышает (fail-closed). Секреты сюда не попадают (D5). Ф9 B4: "
            "keep_private_* — зеркало вниз (company→private): владелец объяснил «это "
            "приватное» → впредь такое НЕ публикуем (только увеличивает приватность)."
        ),
        "promote_kinds": {},   # "<kind>" -> {"company": <name|*>, "at": iso}
        "promote_keys": {},    # "<kind>:<normkey>" -> {"company": <name|*>, "at": iso}
        # Ф9 (B4): запомненное владельцем «держать приватным». Перебивает повышение
        # (fail-closed: приватность всегда выигрывает). Та же форма ключей.
        "keep_private_kinds": {},
        "keep_private_keys": {},
        "log": [],             # append-only история команд владельца
    }


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ratchet state не объект")
        base = _empty_state()
        for k in ("promote_kinds", "promote_keys",
                  "keep_private_kinds", "keep_private_keys"):
            if not isinstance(data.get(k), dict):
                data[k] = base[k]
        if not isinstance(data.get("log"), list):
            data["log"] = base["log"]
        return data
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("ratchet state битый (%s: %s) — старт с пустого", type(e).__name__, e)
        return _empty_state()


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".knowledge_ratchet.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def _lock() -> Iterator[None]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lf = open(lock_path, "w")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        finally:
            lf.close()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _company_match(rule_company: Optional[str], fact_company: Optional[str]) -> bool:
    """Правило с company=C повышает только факт той же company (или C==`*` — любая)."""
    rc = _norm(rule_company)
    if rc in ("", COMPANY_ANY):
        return True
    return rc == _norm(fact_company)


def should_promote(kind: str, *, company: Optional[str] = None, key: Optional[str] = None) -> bool:
    """Запомнен ли ратчет, повышающий этот факт private→company (D4)?

    Совпадение по конкретному ключу (пер-key) ИЛИ по роду знания (пер-kind),
    при совместимости компании. Нет правила → False (остаётся приватным, D3).
    """
    k = _norm(kind)
    if not k:
        return False
    data = load_state()
    if key:
        rule = data.get("promote_keys", {}).get(f"{k}:{_norm(key)}")
        if isinstance(rule, dict) and _company_match(rule.get("company"), company):
            return True
    rule = data.get("promote_kinds", {}).get(k)
    if isinstance(rule, dict) and _company_match(rule.get("company"), company):
        return True
    return False


def remember_promotion(
    kind: str, *, company: Optional[str] = None, key: Optional[str] = None,
    scope: str = "kind",
) -> Optional[dict]:
    """Запомнить ратчет: впредь знание рода `kind` (scope=kind) или конкретный
    факт `key` (scope=key) для компании `company` роутится в `*-context`.

    Возвращает записанное правило либо None (пустой kind). Идемпотентно
    перезаписывает (обновляет company/at). company=None → `*` (любая компания).
    """
    k = _norm(kind)
    if not k:
        return None
    comp = _norm(company) or COMPANY_ANY
    rule = {"company": comp, "at": _now_iso()}

    def _mut(data: dict) -> None:
        if scope == "key" and key:
            data["promote_keys"][f"{k}:{_norm(key)}"] = rule
        else:
            data["promote_kinds"][k] = rule
        data["log"].append({"op": "promote", "kind": k, "scope": scope,
                            "company": comp, "key": (_norm(key) if key else None),
                            "at": rule["at"]})

    with _lock():
        data = load_state()
        _mut(data)
        _write_atomic(state_path(), data)
    logger.info("[ratchet] запомнено повышение private→company: kind=%s scope=%s company=%s",
                k, scope, comp)
    return {"kind": k, "scope": scope, "company": comp, "key": (_norm(key) if key else None)}


# ── Ф9 (B4): зеркало вниз — «держать приватным» (company→private) ─────────────
# Дефолт безопасности (D3) уже приватный, но публикационный гейт/ратчет могут
# отправить факт в COMPANY. Если владелец объяснил «это приватное» — запоминаем,
# и ВПРЕДЬ такое знание принудительно остаётся приватным, ПЕРЕБИВАЯ повышение.
# Это только УВЕЛИЧИВАЕТ приватность (fail-closed остаётся fail-closed).


def should_keep_private(kind: str, *, company: Optional[str] = None, key: Optional[str] = None) -> bool:
    """Запомнил ли владелец, что такое знание держать приватным (B4)?

    Совпадение по конкретному ключу (пер-key) ИЛИ по роду (пер-kind), при
    совместимости компании — симметрично `should_promote`. Нет правила → False.
    """
    k = _norm(kind)
    if not k:
        return False
    data = load_state()
    if key:
        rule = data.get("keep_private_keys", {}).get(f"{k}:{_norm(key)}")
        if isinstance(rule, dict) and _company_match(rule.get("company"), company):
            return True
    rule = data.get("keep_private_kinds", {}).get(k)
    if isinstance(rule, dict) and _company_match(rule.get("company"), company):
        return True
    return False


def remember_keep_private(
    kind: str, *, company: Optional[str] = None, key: Optional[str] = None,
    scope: str = "kind",
) -> Optional[dict]:
    """Запомнить «держать приватным»: впредь знание рода `kind` (scope=kind) или
    конкретный факт `key` (scope=key) для компании `company` остаётся приватным.

    Зеркало `remember_promotion`. company=None → `*` (любая). Идемпотентно.
    """
    k = _norm(kind)
    if not k:
        return None
    comp = _norm(company) or COMPANY_ANY
    rule = {"company": comp, "at": _now_iso()}

    def _mut(data: dict) -> None:
        if scope == "key" and key:
            data["keep_private_keys"][f"{k}:{_norm(key)}"] = rule
        else:
            data["keep_private_kinds"][k] = rule
        data["log"].append({"op": "keep-private", "kind": k, "scope": scope,
                            "company": comp, "key": (_norm(key) if key else None),
                            "at": rule["at"]})

    with _lock():
        data = load_state()
        _mut(data)
        _write_atomic(state_path(), data)
    logger.info("[ratchet] запомнено «держать приватным» (company→private): kind=%s scope=%s company=%s",
                k, scope, comp)
    return {"kind": k, "scope": scope, "company": comp, "key": (_norm(key) if key else None)}


# ── Разбор команды владельца «переноси в контекст …» ─────────────────────────
# Триггер действия + цель «контекст/общий мозг/командное/общая база».
_PROMOTE_TRIGGER_RE = re.compile(
    r"\b(?:перенос\w*|перенеси|переведи|фиксируй|сохраняй|клади|добавляй|"
    r"запоминай|храни)\b",
    re.IGNORECASE,
)
_PROMOTE_TARGET_RE = re.compile(
    r"\b(?:в\s+контекст(?:\s+компании)?|в\s+общий\s+мозг|в\s+командн\w+|"
    r"в\s+общую\s+базу|в\s+\*?-?context|в\s+базу\s+знаний\s+компании)\b",
    re.IGNORECASE,
)
# Короткая форма без глагола: «это в контекст компании», «в общий мозг».
_PROMOTE_SHORT_RE = re.compile(
    r"^\s*(?:это\s+|давай\s+)?(?:в\s+контекст(?:\s+компании)?|в\s+общий\s+мозг|"
    r"в\s+общую\s+базу)\b",
    re.IGNORECASE,
)

_COMPANY_HINTS = (
    ("anzhee", re.compile(r"\b(?:anzhee|анже\w*|энжи|engie|анзи)\b", re.IGNORECASE)),
    ("mpfirst", re.compile(r"\b(?:mpfirst|мпервый|м-?первый|m1|первый)\b", re.IGNORECASE)),
)

# Ф9: отрицание прямо перед триггером повышения («не переноси в контекст») — это НЕ
# команда повышения, а наоборот (часто = держать приватным). Fail-closed: при
# отрицании повышение НЕ срабатывает (ошибочное повышение необратимо для команды).
_PROMOTE_NEGATION_RE = re.compile(
    r"\b(?:не|нет|никогда|ни\s+в\s+коем)\s+(?:перенос\w*|перенеси|переведи|фиксируй|"
    r"сохраняй|клади|добавляй|запоминай|храни)\b",
    re.IGNORECASE,
)


def parse_promote_command(text: object) -> Optional[dict]:
    """Это команда «переноси в контекст …»? → {company: Optional[str], scope}.

    Чистый разбор (без IO) — основной объект unit-теста. Возвращает None, если
    текст не команда повышения. `company` — если владелец назвал компанию явно,
    иначе None (повышаем в компанию факта). `scope='kind'` по умолчанию (впредь
    такие — туда); слово «только/именно это» → scope='key' (разово).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    # Fail-closed: отрицание перед триггером («не переноси в контекст») → не повышаем.
    if _PROMOTE_NEGATION_RE.search(s):
        return None
    has_target = bool(_PROMOTE_TARGET_RE.search(s))
    is_command = (has_target and bool(_PROMOTE_TRIGGER_RE.search(s))) or bool(_PROMOTE_SHORT_RE.search(s))
    if not is_command:
        return None
    company: Optional[str] = None
    for name, rx in _COMPANY_HINTS:
        if rx.search(s):
            company = name
            break
    # «только это» / «именно этот» / «разово» → разовое повышение, не запоминать род.
    scope = "key" if re.search(r"\b(?:только\s+это|именно\s+эт\w+|разов\w+|один\s+раз)\b", s, re.IGNORECASE) else "kind"
    return {"company": company, "scope": scope}


def note_promote_command(
    text: object, *, kind: str, key: Optional[str] = None, company: Optional[str] = None,
) -> Optional[dict]:
    """Если `text` — команда повышения, запомнить ратчет для факта (kind/key/company).

    Соединяет `parse_promote_command` + `remember_promotion`. company из команды
    приоритетнее переданной (владелец мог назвать другую). Возвращает запомненное
    правило либо None (не команда). Best-effort: ошибку наружу не пускаем.
    """
    parsed = parse_promote_command(text)
    if parsed is None:
        return None
    comp = parsed.get("company") or company
    scope = parsed.get("scope") or "kind"
    try:
        return remember_promotion(kind, company=comp, key=key, scope=scope)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ratchet] запись повышения не удалась (non-fatal): %s", e)
        return None


# Ф9 (B4): команда «это приватное / держи в личном / не в контекст».
_KEEP_PRIVATE_RE = re.compile(
    r"(?:\bэто\s+)?(?:приватн\w+|личн\w+|не\s+(?:для|в)\s+команд\w+|"
    r"не\s+(?:в|для)\s+контекст\w*|не\s+публику\w+|оставь\s+(?:в\s+)?(?:личн\w+|приватн\w+)|"
    r"держи\s+(?:в\s+)?(?:личн\w+|приватн\w+|при\s+себе)|только\s+(?:для\s+)?меня)\b",
    re.IGNORECASE,
)


def parse_keep_private_command(text: object) -> Optional[dict]:
    """Это объяснение «держать приватным»? → {company, scope}. Зеркало
    `parse_promote_command`. None, если текст не про приватность."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    if not _KEEP_PRIVATE_RE.search(s):
        return None
    company: Optional[str] = None
    for name, rx in _COMPANY_HINTS:
        if rx.search(s):
            company = name
            break
    scope = "key" if re.search(r"\b(?:только\s+это|именно\s+эт\w+|разов\w+|один\s+раз)\b", s, re.IGNORECASE) else "kind"
    return {"company": company, "scope": scope}


def note_keep_private_command(
    text: object, *, kind: str, key: Optional[str] = None, company: Optional[str] = None,
) -> Optional[dict]:
    """Если `text` — объяснение приватности, запомнить keep-private для факта.
    Зеркало `note_promote_command`. Best-effort: ошибку наружу не пускаем."""
    parsed = parse_keep_private_command(text)
    if parsed is None:
        return None
    comp = parsed.get("company") or company
    scope = parsed.get("scope") or "kind"
    try:
        return remember_keep_private(kind, company=comp, key=key, scope=scope)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ratchet] запись keep-private не удалась (non-fatal): %s", e)
        return None
