#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISS-58: вернуть исполнителей в УЖЕ СОБРАННЫЕ выжимки серий.

Зачем. Врезка ревью «⚠️ проверьте: тут мог быть …», приклеенная к строке-
подзаголовку исполнителя (`**Илья Рыбалка**`), ломала разбор раздела «Задачи»:
все задачи встречи уезжали в память серии без префикса «Имя: ». Разбор починен
(`lib/series_memory._OWNER_SUBHEADING_RE`), но УЖЕ ЛЕЖАЩИЕ выжимки сами не
чинятся, а хвост висяков переезжает из выжимки в выжимку — без починки следующая
встреча серии снова получит безымянный ком, и дайджест снова скажет «без
ответственного».

🔴 ПОЧЕМУ НЕ `backfill-series-memory.py --overwrite`. Тот пересобирает выжимку
ЦЕЛИКОМ из протокола, а `speaker_mapping` и `publication` в протоколе не лежат —
их кладёт финализация. Перезапись стёрла бы разметку спикеров (по ней держатся
правки имён) и решение о публикуемости. Здесь трогаются РОВНО два ключа:
`open_tasks` и `open_tasks_born_here`.

🔴 ТОЛЬКО ДОБАВЛЯЕМ ИМЯ, НИКОГДА НЕ МЕНЯЕМ. Каждая задача выжимки обязана либо
совпасть с задачей протокола дословно, либо совпасть с её хвостом после «Имя: ».
Если в выжимке уже стоит исполнитель, а протокол называет другого — это не ремонт
разбора, а переназначение (протокол перевыпустили, статусы проставлены под старым
именем), и файл пропускается целиком. Так же пропускается выжимка, собранная из
другой версии протокола: хотя бы одна задача не сошлась → не трогаем ничего.

🔴 СТАТУСЫ ПЕРЕЕЗЖАЮТ ВМЕСТЕ С ТЕКСТОМ. Sidecar статусов (`task-status.json`)
ключуется по ПОЛНОМУ тексту задачи, а он меняется («задача» → «Имя: задача»).
Найденный статус переносится под новый ключ, иначе проставленное «сделано»
потерялось бы и закрытая задача воскресла бы висящей.

По умолчанию — DRY-RUN: печатает, что изменится, и ничего не пишет. Реальная
запись — только по явному `--apply`.

Примеры:
    python3 tools/repair-open-task-owners.py                      # весь корень, dry-run
    python3 tools/repair-open-task-owners.py --series marketplaces-tatiana
    python3 tools/repair-open-task-owners.py --series marketplaces-tatiana --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
sys.path.insert(0, str(NOTARY_DIR))

from lib import series_memory as sm  # noqa: E402

DEFAULT_ROOT = os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR") or os.path.expanduser(
    "~/Projects/me/встречи"
)


def _text_of(item) -> str:
    """Элемент `open_tasks` → текст. Мелочь лежит объектом `{text, minor}`."""
    if isinstance(item, dict):
        return str(item.get("text") or "")
    return str(item or "")


def _with_text(item, text: str):
    """Тот же элемент с новым текстом — форма (строка / объект-мелочь) сохраняется."""
    if isinstance(item, dict):
        out = dict(item)
        out["text"] = text
        return out
    return text


def _match_key(text: str) -> str:
    """Ключ задачи для сверки «та же задача» — тот же, что у sidecar статусов."""
    return sm._status_key(text or "")


def _is_same_task_with_owner(stored: str, parsed: str) -> bool:
    """`parsed` — это `stored`, которому вернули исполнителя («Имя: <тот же текст>»)?

    🔴 ТОЛЬКО ДОБАВЛЕНИЕ ИМЕНИ, НЕ ЗАМЕНА. Если в выжимке уже стоит исполнитель, а
    протокол называет другого, это НЕ ремонт нашего разбора, а переназначение —
    протокол мог быть перевыпущен, а статусы в sidecar проставлены под старым
    именем. Такое чинить нельзя, и оно сюда не попадает: `stored` обязан целиком
    совпасть с ХВОСТОМ `parsed` после «Имя: ».

    Срезать префикс у `stored` и сравнивать «сути» тоже нельзя: задача
    «Организовать самовыкупы: ~10 отзывов…» несёт двоеточие в тексте, и разбор
    принял бы «Организовать самовыкупы» за имя (боевой случай 12.08.2026).
    """
    m = sm._PENDING_OWNER_RE.match(parsed or "")
    if not m:
        return False
    return _match_key(m.group(2)) == _match_key(stored)


def _plan_file(mem_path: Path, proto_path: Path) -> dict:
    """Что делать с одной выжимкой: `action` = repair | skip (+ `why` у пропуска)."""
    try:
        digest = json.loads(mem_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"action": "skip", "why": f"выжимка не читается ({type(e).__name__})"}
    stored = digest.get("open_tasks")
    if not isinstance(stored, list) or not stored:
        return {"action": "skip", "why": "открытых задач в выжимке нет"}
    try:
        protocol_text = proto_path.read_text(encoding="utf-8")
    except OSError as e:
        return {"action": "skip", "why": f"протокол не читается ({type(e).__name__})"}

    parsed = sm.extract_open_tasks(protocol_text)
    if not parsed:
        return {"action": "skip", "why": "в протоколе задач не найдено"}

    # Сопоставление «задача выжимки → задача протокола»: либо тот же текст (уже
    # в порядке), либо тот же текст с вернувшимся исполнителем. Всё остальное
    # означает, что выжимка собрана из другой версии протокола, — не наш случай.
    exact = {_match_key(t): t for t in parsed}
    new_items = []
    renames: list = []
    for item in stored:
        old_text = _text_of(item)
        if _match_key(old_text) in exact:
            new_items.append(item)
            continue
        hits = [t for t in parsed if _is_same_task_with_owner(old_text, t)]
        if len(hits) != 1:
            return {
                "action": "skip",
                "why": (f"задача выжимки не сходится с протоколом однозначно "
                        f"(совпадений {len(hits)}) — выжимка собрана из другой версии"),
            }
        renames.append((old_text, hits[0]))
        new_items.append(_with_text(item, hits[0]))
    if not renames:
        return {"action": "skip", "why": "исполнители уже на месте"}

    # `open_tasks_born_here` — подмножество «родилось на этой встрече»; оставить его
    # безымянным значило бы разойтись с `open_tasks` (шпаргалочный режим серии читает
    # именно его). Пересобираем из того же разбора; разошлось по размеру — не трогаем.
    _carried, fresh = sm._split_open_tasks_by_section(protocol_text)
    born = digest.get(sm.OPEN_TASKS_BORN_HERE_KEY)
    new_born = None
    if isinstance(born, list):
        renamed = {_match_key(o): n for o, n in renames}
        born_keys = {renamed.get(_match_key(_text_of(x)), _text_of(x)) for x in born}
        born_keys = {_match_key(t) for t in born_keys}
        candidate = [t for t in fresh if _match_key(t) in born_keys]
        if len(candidate) == len(born):
            new_born = candidate

    return {
        "action": "repair",
        "digest": digest,
        "new_items": new_items,
        "new_born": new_born,
        "renames": renames,
    }


def _migrate_statuses(series_dir: Path, renames: list, *, apply: bool) -> int:
    """Перенести статусы sidecar со старых ключей на новые. Возвращает число переносов."""
    path = series_dir / sm.TASK_STATUS_FILE
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    items = data.get("items")
    if not isinstance(items, dict):
        return 0
    moved = 0
    for old_text, new_text in renames:
        old_key = sm._status_key(old_text)
        new_key = sm._status_key(new_text)
        # Занятый новый ключ не перезаписываем: там уже стоит осознанный статус.
        if old_key == new_key or old_key not in items or new_key in items:
            continue
        rec = dict(items[old_key])
        rec["text"] = new_text
        items[new_key] = rec
        del items[old_key]
        moved += 1
    if moved and apply:
        data["items"] = items
        _write_json(path, data)
    return moved


def _write_json(path: Path, payload: dict) -> None:
    """Атомарная запись с правами 600 — как у самого бота (файлы серии приватны)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="ISS-58: вернуть исполнителей в выжимки серий")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="корень папки встреч")
    ap.add_argument("--series", default=None, help="имя одной серии (папки); без него — все")
    ap.add_argument("--apply", action="store_true", help="реально записать (без него — dry-run)")
    args = ap.parse_args()

    root = Path(os.path.expanduser(args.root))
    if not root.is_dir():
        print(f"❌ корень не найден: {root}", file=sys.stderr)
        return 2
    if args.series:
        series_dirs = [root / args.series]
        if not series_dirs[0].is_dir():
            print(f"❌ серия не найдена: {series_dirs[0]}", file=sys.stderr)
            return 2
    else:
        series_dirs = [
            d for d in sorted(root.iterdir(), key=lambda p: p.name)
            if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
        ]

    repaired = 0
    statuses = 0
    for series_dir in series_dirs:
        for entry in sorted(series_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_file():
                continue
            date = sm._date_from_protocol_filename(entry.name)
            if not date:
                continue
            mem_path = sm.digest_path(series_dir, date)
            if not mem_path.exists():
                continue
            plan = _plan_file(mem_path, entry)
            if plan["action"] != "repair":
                continue
            renames = plan["renames"]
            print(f"\n{series_dir.name}/{date}: вернём исполнителя {len(renames)} задачам")
            for old, new in renames[:3]:
                print(f"    было:  {old[:70]}")
                print(f"    стало: {new[:70]}")
            if len(renames) > 3:
                print(f"    … и ещё {len(renames) - 3}")
            moved = _migrate_statuses(series_dir, renames, apply=args.apply)
            if moved:
                print(f"    статусов переносим в sidecar: {moved}")
            statuses += moved
            if args.apply:
                digest = plan["digest"]
                digest["open_tasks"] = plan["new_items"]
                if plan["new_born"] is not None:
                    digest[sm.OPEN_TASKS_BORN_HERE_KEY] = plan["new_born"]
                _write_json(mem_path, digest)
            repaired += 1

    tail = "" if args.apply else "  (DRY-RUN, ничего не записано — повтори с --apply)"
    print(f"\n{'✅' if args.apply else 'ℹ️'} выжимок к починке: {repaired}, "
          f"статусов перенесено: {statuses}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
