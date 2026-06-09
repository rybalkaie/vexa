"""Безопасное чтение/слияние/запись Speechmatics vocab.

Два файла (решение оркестратора 2026-05-29, вариант «б»):
  - **главный** `speechmatics-vocab.json` — handmaintained, деплоится с мака
    через `make deploy-vocab`. Ручные правки + 7 добавок Ф2.
  - **авто** `speechmatics-vocab-auto.json` — копится автоматикой Ф8 (sources +
    applier high/approve). Живёт ТОЛЬКО на VPS, с мака не деплоится.

`speechmatics_client._load_additional_vocab` читает ОБА и сливает (главный
побеждает при совпадении `content`). Зачем так: `make deploy-vocab` (mac→VPS)
трогает только главный файл и НИКОГДА не затирает авто-добавки — конфликт
двунаправленного синка из чекпойнта §5 устранён архитектурно.

Контракт с Ф2:
  - дедуп по `content` (trim+lower); главный файл — источник истины при
    коллизии, поэтому в авто кандидат с тем же `content` просто не добавляется;
  - служебные ключи (`_*`) и порядок записей сохраняются (см. `_dumps_vocab`);
  - запись атомарная (`os.replace`) — параллельный читатель не видит полуфайл.

Конкурентная запись авто-файла (sources-таймер ↔ applier high ↔ callback
approve) сериализуется через `flock_vocab()` (flock на `<auto>.lock`).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from notary.lib import cred_filter as _cred_filter

logger = logging.getLogger(__name__)


def vocab_path() -> Path:
    """Главный (handmaintained) файл — тот же путь, что у speechmatics_client."""
    return Path(
        os.environ.get(
            "SPEECHMATICS_VOCAB_PATH",
            "/srv/meeting-notary/config/speechmatics-vocab.json",
        )
    ).expanduser()


def auto_vocab_path() -> Path:
    """Авто-файл (копится Ф8). По умолчанию рядом с главным, суффикс `-auto`."""
    env = os.environ.get("SPEECHMATICS_VOCAB_AUTO_PATH")
    if env:
        return Path(env).expanduser()
    main = vocab_path()
    return main.with_name(main.stem + "-auto" + main.suffix)


def normalize(content: str) -> str:
    """Ключ дедупа: trim + lower. «1С» и «1с» — один термин."""
    return (content or "").strip().lower()


def _empty_auto() -> dict:
    return {
        "_comment": (
            "АВТО-словарь Speechmatics (Фаза 8). Копится автоматикой: sources.py "
            "(термины из ~/Projects/me/*) + applier.py (LLM-кандидаты high/approved). "
            "Сливается с главным speechmatics-vocab.json при чтении (главный "
            "побеждает). С мака НЕ деплоится — `make deploy-vocab` его не трогает. "
            "Править руками можно, но обычно не нужно."
        ),
        "_phase": "Ф8 доработок-бот-нотариус 2026-05-29",
        "additional_vocab": [],
    }


def load_vocab(path: Path | None = None) -> tuple[dict, list[dict]]:
    """(полный объект, список additional_vocab) главного файла.

    Бросает при проблеме чтения — вызывающий не должен перезаписывать на основе
    нечитаемой базы (защита Ф2-добавок).
    """
    p = path or vocab_path()
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"vocab root должен быть объектом, получили {type(data).__name__}")
    entries = data.get("additional_vocab")
    if not isinstance(entries, list):
        raise ValueError("additional_vocab отсутствует или не список")
    return data, entries


def load_auto() -> tuple[dict, list[dict]]:
    """(объект, записи) авто-файла. Отсутствует/битый → пустой скелет (НЕ бросает).

    Авто-файл легитимно может не существовать (первый запуск) — это не ошибка.
    """
    p = auto_vocab_path()
    if not p.exists():
        d = _empty_auto()
        return d, d["additional_vocab"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("additional_vocab")
        if not isinstance(data, dict) or not isinstance(entries, list):
            raise ValueError("битая структура авто-файла")
        return data, entries
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("auto-vocab битый (%s: %s) — старт с пустого", type(e).__name__, e)
        d = _empty_auto()
        return d, d["additional_vocab"]


def existing_keys(entries: list[dict]) -> set[str]:
    keys: set[str] = set()
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("content"), str):
            keys.add(normalize(e["content"]))
    return keys


def combined_existing_keys() -> set[str]:
    """Нормализованные `content` из главного + авто файлов — для дедупа
    кандидатов (sources / proposer / applier), чтобы не предлагать/не добавлять
    то, что уже есть в любом из двух."""
    keys: set[str] = set()
    try:
        _d, main_entries = load_vocab()
        keys |= existing_keys(main_entries)
    except Exception as e:  # noqa: BLE001
        logger.warning("combined_existing_keys: главный vocab не прочитан (%s)", e)
    _ad, auto_entries = load_auto()
    keys |= existing_keys(auto_entries)
    return keys


def merge_new(data: dict, candidates: list[dict], *, extra_existing: set[str] | None = None
              ) -> tuple[dict, list[dict]]:
    """Дописать в конец `additional_vocab` только новые (по нормализованному
    `content`) записи. Дедуп против записей самого data + `extra_existing`
    (например, ключей главного файла). Возвращает (data, добавленные)."""
    entries: list[dict] = data["additional_vocab"]
    seen = existing_keys(entries)
    if extra_existing:
        seen |= extra_existing
    added: list[dict] = []
    for cand in candidates:
        if not isinstance(cand, dict) or not isinstance(cand.get("content"), str):
            continue
        # 🔴 D5 (Ф7): жёсткий фильтр кредов — секрет НЕ попадает ни в один слой, в
        # т.ч. в ASR-словарь. Это единственный write-чокпоинт авто-vocab (sources +
        # proposer + applier + glossary-проекция льются сюда) → одна проверка
        # закрывает весь слой. Логируем ТОЛЬКО вид, не значение (опасная тройка).
        content = cand["content"]
        sl = cand.get("sounds_like")
        if not _cred_filter.is_safe_to_store(content) or (
            isinstance(sl, list) and any(not _cred_filter.is_safe_to_store(x) for x in sl)
        ):
            logger.warning("vocab: кандидат отброшен фильтром кредов (вид=%s) — не сохраняем",
                           _cred_filter.secret_kind(content)
                           or _cred_filter.secret_kind(" ".join(str(x) for x in (sl or []))))
            continue
        key = normalize(content)
        if not key or key in seen:
            continue
        seen.add(key)
        entry: dict = {"content": content.strip()}
        if isinstance(sl, list) and sl:
            cleaned = [str(x).strip() for x in sl if str(x).strip()]
            if cleaned:
                entry["sounds_like"] = cleaned
        entries.append(entry)
        added.append(entry)
    return data, added


def _dumps_vocab(data: dict) -> str:
    """Сериализовать так, чтобы КАЖДАЯ запись additional_vocab была на одной
    строке (компактный handmaintained-вид Ф2), служебные ключи — сверху."""
    entries = data.get("additional_vocab", [])
    head = {k: v for k, v in data.items() if k != "additional_vocab"}
    lines: list[str] = ["{"]
    for k, v in head.items():
        lines.append(f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)},")
    lines.append('  "additional_vocab": [')
    for i, entry in enumerate(entries):
        comma = "," if i < len(entries) - 1 else ""
        lines.append(f"    {json.dumps(entry, ensure_ascii=False)}{comma}")
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_vocab_atomic(data: dict, path: Path | None = None) -> None:
    p = path or vocab_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".speechmatics-vocab.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_dumps_vocab(data))
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def flock_vocab() -> Iterator[None]:
    """Эксклюзивный flock на `<auto>.lock` — сериализует писателей авто-файла
    (sources-таймер ↔ applier high ↔ callback approve)."""
    p = auto_vocab_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")
    lf = open(lock_path, "w")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        finally:
            lf.close()


def add_to_auto(candidates: list[dict]) -> list[dict]:
    """Добавить новые термины в авто-файл под flock (дедуп против главного+авто).

    Это единственная точка записи авто-файла для sources/applier. Возвращает
    список реально добавленных записей.
    """
    with flock_vocab():
        try:
            _d, main_entries = load_vocab()
            main_keys = existing_keys(main_entries)
        except Exception as e:  # noqa: BLE001
            logger.warning("add_to_auto: главный vocab не прочитан (%s) — дедуп только по авто", e)
            main_keys = set()
        auto_data, _auto_entries = load_auto()
        auto_data, added = merge_new(auto_data, candidates, extra_existing=main_keys)
        if added:
            write_vocab_atomic(auto_data, auto_vocab_path())
    return added
