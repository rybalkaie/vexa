"""Реестр стейкхолдеров для Ф5 (`route_tasks`).

Источник правды — `~/Projects/me-dashboard/stakeholders.py` (cross-project).
На VPS реестр копируется как `stakeholders.json` в `_methods/` тем же
launchd push-агентом, что синкает методички (`meeting-notary-methods-push.sh`).
Маков прод вот на VPS живёт, но мак-режим (LOCAL_FINALIZE / smoke) тоже
поддерживается: если JSON не найден — fallback на прямой import.py.

Контракт записи:
  {"stakeholders": [
    {"slug": "...", "name": "...", "file_path": "<rel-path-from-me>",
     "company_tag": "..."},
    ...
  ]}
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


# Дефолтные кандидаты для JSON-копии (тот же путь что у методички).
_JSON_CANDIDATES = (
    "/opt/meeting-notary/_methods/stakeholders.json",
    os.path.expanduser("~/Projects/me/methods/stakeholders.json"),
)

# Fallback: прямой import .py (только в локальном dev / на маке).
_PY_FALLBACK = os.path.expanduser("~/Projects/me-dashboard/stakeholders.py")


def _load_from_json(path: str) -> Optional[list[dict]]:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[stakeholders] %s не читается: %s", p, e)
        return None
    if not isinstance(data, dict):
        return None
    stk = data.get("stakeholders")
    if not isinstance(stk, list):
        return None
    return [s for s in stk if isinstance(s, dict) and s.get("slug") and s.get("name")]


def _load_from_py(path: str) -> Optional[list[dict]]:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_stakeholders_fallback", str(p))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        raw = getattr(mod, "STAKEHOLDERS", None)
    except Exception as e:  # noqa: BLE001
        logger.warning("[stakeholders] py-fallback %s failed: %s", p, e)
        return None
    if not isinstance(raw, list):
        return None
    return [
        {
            "slug": s.get("slug"),
            "name": s.get("name"),
            "file_path": s.get("file_path"),
            "company_tag": s.get("company_tag"),
        }
        for s in raw
        if isinstance(s, dict) and s.get("slug") and s.get("name")
    ]


def load_stakeholders(*, override_path: Optional[str] = None) -> list[dict]:
    """Возвращает [{slug, name, file_path, company_tag}, ...].

    Порядок поиска:
      1. `override_path` (для тестов).
      2. env `MEETING_NOTARY_STAKEHOLDERS_JSON` — явный путь к JSON.
      3. JSON-кандидаты (`_JSON_CANDIDATES`).
      4. PY-fallback (`_PY_FALLBACK`) — для локального dev.

    Пустой список если ничего не нашли — caller сам решает что делать
    (вероятнее всего — задачи собеседников пропускать с логом).
    """
    candidates: list[str] = []
    if override_path:
        candidates.append(override_path)
    env_path = (os.environ.get("MEETING_NOTARY_STAKEHOLDERS_JSON") or "").strip()
    if env_path:
        candidates.append(os.path.expanduser(env_path))
    candidates.extend(_JSON_CANDIDATES)

    for c in candidates:
        items = _load_from_json(c)
        if items is not None:
            return items

    py_items = _load_from_py(_PY_FALLBACK)
    if py_items is not None:
        return py_items

    logger.warning(
        "[stakeholders] реестр не найден: tried JSON=%s, py-fallback=%s",
        candidates, _PY_FALLBACK,
    )
    return []


def stakeholder_abs_track_path(stakeholder: dict, *, me_dir: Optional[str] = None) -> Optional[Path]:
    """Возвращает абсолютный путь к треку (md-накопителю) стейкхолдера.

    `file_path` хранится относительно `~/Projects/me/`. На VPS файл доступен
    через mirror (`~/Projects/me/` mirror'ится на VPS под dev'ом). Если
    директории `~/Projects/me/` нет — None (caller пропустит запись).
    """
    rel = stakeholder.get("file_path") or ""
    if not rel:
        return None
    base = me_dir or os.environ.get("ME_DIR") or os.path.expanduser("~/Projects/me")
    return Path(base) / rel


def find_stakeholder_by_name(name: str, stakeholders: list[dict]) -> Optional[dict]:
    """Точное совпадение `name` либо first-word (для «Татьяна» / «Татьяна Филипова»).

    Возвращает первый матч (реестр маленький, гонок не предвидится).
    """
    if not name or not isinstance(name, str):
        return None
    needle = name.strip().lower()
    if not needle:
        return None
    for s in stakeholders:
        nm = (s.get("name") or "").strip().lower()
        if not nm:
            continue
        if nm == needle:
            return s
        # First-word match: реестр пишет «Михаил Еремеев», LLM может вернуть «Михаил».
        fw_registry = nm.split()[0] if nm.split() else ""
        fw_needle = needle.split()[0] if needle.split() else ""
        if fw_registry and fw_needle and fw_registry == fw_needle:
            # Для неоднозначных имён («Михаил» → Еремеев/Саргин) проверим что в
            # реестре только один с таким first-word.
            same = [
                s2 for s2 in stakeholders
                if (s2.get("name") or "").strip().lower().split()[:1] == [fw_registry]
            ]
            if len(same) == 1:
                return s
    return None
