"""Состояние авто-пополнения vocab: `auto_vocab_state.json` под flock.

Структура файла (Слой 4 плана Ф8):

    {
      "_comment": "...",
      "rejected_terms":    {"<term>": <count>},   # сколько раз владелец отклонил
      "approved_patterns": {"<pattern>": <count>}, # для бустинга confidence (Шаг 8.4)
      "weekly_stats": [
        {"week": "2026-W22", "auto_added": 0, "requested": 0,
         "approved": 0, "rejected": 0, "sources_added": 0, "cost_usd": 0.0}
      ]
    }

Все мутации идут через `update(fn)` под эксклюзивным flock на sidecar-файле
`<state>.lock` — защита от гонки cron-источников (Шаг 8.1) и LLM-proposer'а
(Шаг 8.2), которые могут писать одновременно. flock — POSIX (mac + Linux VPS).

`rejected_terms[term] >= REJECT_THRESHOLD` (по умолчанию 3) → term больше не
предлагается proposer'ом (Шаг 8.2 подмешивает их в промт как «не предлагай»).

Неделя считается по ISO (`%G-W%V`) от системного времени. На VPS таймер
крутится в МСК (см. systemd-юниты), так что граница недели = МСК-воскресенье.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

# Порог отклонений, после которого term навсегда исключается из предложений.
REJECT_THRESHOLD = 3

# Поля недельной статистики — единый источник правды для дайджеста (Шаг 8.4).
_WEEKLY_FIELDS = ("auto_added", "requested", "approved", "rejected", "sources_added")


def _default_state_path() -> Path:
    """Рядом с vocab-конфигом (тот же `config/`), имя `auto_vocab_state.json`.

    Переопределяется env `AUTO_VOCAB_STATE_PATH`. VOCAB_CONFIG_PATH берём из
    того же env, что и speechmatics_client (`SPEECHMATICS_VOCAB_PATH`), чтобы
    state и vocab жили вместе и на маке, и на VPS.
    """
    env = os.environ.get("AUTO_VOCAB_STATE_PATH")
    if env:
        return Path(env).expanduser()
    vocab = os.environ.get(
        "SPEECHMATICS_VOCAB_PATH",
        "/srv/meeting-notary/config/speechmatics-vocab.json",
    )
    return Path(vocab).expanduser().parent / "auto_vocab_state.json"


def state_path() -> Path:
    return _default_state_path()


def _empty_state() -> dict:
    return {
        "_comment": (
            "Состояние авто-пополнения Speechmatics vocab (Фаза 8). "
            "rejected_terms: счётчик отклонений владельцем (>=3 → не предлагать). "
            "approved_patterns: для бустинга confidence. "
            "weekly_stats: агрегат для воскресного TG-дайджеста."
        ),
        "rejected_terms": {},
        "approved_patterns": {},
        "weekly_stats": [],
    }


def current_week_key(now: datetime | None = None) -> str:
    """ISO-неделя `%G-W%V` (год по ISO, чтобы конец декабря не уезжал)."""
    dt = now or datetime.now()
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def load_state() -> dict:
    """Прочитать state без блокировки (для read-only: дайджест, дедуп).

    Битый/отсутствующий файл → пустой state (не роняем пополнение из-за state).
    """
    path = state_path()
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state не объект")
        # Бэкфилл недостающих ключей (миграция со старых версий формата).
        base = _empty_state()
        for k in ("rejected_terms", "approved_patterns"):
            if not isinstance(data.get(k), dict):
                data[k] = base[k]
        if not isinstance(data.get("weekly_stats"), list):
            data["weekly_stats"] = base["weekly_stats"]
        return data
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("auto_vocab state битый (%s: %s) — старт с пустого", type(e).__name__, e)
        return _empty_state()


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".auto_vocab_state.", suffix=".tmp")
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
    """Эксклюзивный flock на sidecar `<state>.lock`. Блокирующий."""
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


def update(fn: Callable[[dict], None]) -> dict:
    """Read-modify-write под flock. `fn` мутирует переданный dict in-place.

    Возвращает записанный state. Любое исключение в `fn` пробрасывается, но
    файл при этом не перезаписывается (мутация была только в памяти).
    """
    with _lock():
        data = load_state()
        fn(data)
        _write_atomic(state_path(), data)
        return data


def _ensure_week(data: dict, week_key: str) -> dict:
    for row in data["weekly_stats"]:
        if row.get("week") == week_key:
            return row
    row = {"week": week_key, **{f: 0 for f in _WEEKLY_FIELDS}, "cost_usd": 0.0}
    data["weekly_stats"].append(row)
    return row


def bump_weekly(field: str, n: int = 1, *, week_key: str | None = None) -> None:
    """Прибавить n к недельному счётчику (`sources_added`, `auto_added`, ...)."""
    if field not in _WEEKLY_FIELDS and field != "cost_usd":
        raise ValueError(f"неизвестное поле weekly_stats: {field}")
    wk = week_key or current_week_key()

    def _mut(data: dict) -> None:
        row = _ensure_week(data, wk)
        row[field] = round((row.get(field, 0) or 0) + n, 6)

    update(_mut)


def add_cost(cost_usd: float, *, week_key: str | None = None) -> None:
    """Прибавить стоимость Claude-вызова к недельной сумме (РИСК6 плана)."""
    if cost_usd <= 0:
        return
    bump_weekly("cost_usd", cost_usd, week_key=week_key)


def add_rejected(term: str) -> int:
    """Инкремент счётчика отклонений term. Возвращает новое значение."""
    key = term.strip()
    new_count = {"v": 0}

    def _mut(data: dict) -> None:
        c = int(data["rejected_terms"].get(key, 0)) + 1
        data["rejected_terms"][key] = c
        new_count["v"] = c

    update(_mut)
    return new_count["v"]


def add_approved_pattern(pattern: str) -> None:
    def _mut(data: dict) -> None:
        data["approved_patterns"][pattern] = int(data["approved_patterns"].get(pattern, 0)) + 1

    update(_mut)


def rejected_blocklist(threshold: int = REJECT_THRESHOLD) -> list[str]:
    """Термины, отклонённые >= threshold раз — их proposer больше не предлагает."""
    data = load_state()
    return [t for t, c in data.get("rejected_terms", {}).items() if int(c) >= threshold]


def classify_term_pattern(term: str) -> str:
    """Грубая классификация термина для бустинга confidence (Шаг 8.4).

    Простой тапл-признак из решения оркестратора: (UPPER-латиница / русское
    имя-фамилия / есть цифра / прочее). Когда паттерн одобрялся >= 3 раз —
    proposer склоняет такие термины к `high` автоматически.
    """
    t = (term or "").strip()
    if not t:
        return "other"
    if any(ch.isdigit() for ch in t):
        return "has_digit"
    import re as _re
    if _re.fullmatch(r"[A-Z]{2,}", t):
        return "upper_latin"
    if _re.fullmatch(r"[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?", t):
        return "cyr_name"
    if _re.fullmatch(r"[A-Z][a-zA-Z]+", t):
        return "cap_latin"
    return "other"


# Человекочитаемые подсказки для промта proposer'а по типу паттерна.
_PATTERN_HINTS = {
    "upper_latin": "термины ЗАГЛАВНОЙ латиницей (бренды вроде ALTRONIX)",
    "cyr_name": "русские имена/фамилии",
    "has_digit": "термины с цифрами (вроде Dealer 360)",
    "cap_latin": "латинские названия с заглавной (бренды/проекты)",
}


def boosted_pattern_hints(threshold: int = REJECT_THRESHOLD) -> list[str]:
    """Подсказки для proposer'а: типы терминов, одобренные >= threshold раз —
    их стоит чаще помечать high."""
    data = load_state()
    hints: list[str] = []
    for pattern, count in data.get("approved_patterns", {}).items():
        if int(count) >= threshold and pattern in _PATTERN_HINTS:
            hints.append(_PATTERN_HINTS[pattern])
    return hints


def week_stats(week_key: str | None = None) -> dict:
    """Срез статистики за неделю (для дайджеста). Нет недели → нули."""
    wk = week_key or current_week_key()
    for row in load_state().get("weekly_stats", []):
        if row.get("week") == wk:
            return dict(row)
    return {"week": wk, **{f: 0 for f in _WEEKLY_FIELDS}, "cost_usd": 0.0}
