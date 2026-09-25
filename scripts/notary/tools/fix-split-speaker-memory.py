#!/usr/bin/env python3
"""Одноразовая уборка: чинит выжимки серий, где один человек занял два голоса.

Зачем. `speaker_mapping` в `<дата>-memory.json` наследуется на следующую встречу
(`series_memory.resolve_speaker_anchor`). Пока там лежит «Илья» на одном голосе и
«Илья Рыбалка» на другом, бот тащит эту ошибку вперёд — даже с починенным кодом.
На 25.09.2026 отравлено 17 выжимок из 59.

План: ../../../plans/2026-09-25-speaker-attribution-fix.md, Фаза 3.

Что делает (решение владельца 25.09):
  • пара «короткая форма + полная» одного человека → схлопывается в ОДНУ запись,
    остаётся голос с полным именем, второй голос очищается (он не этот человек,
    а кто именно — неизвестно, пусть решает разбор текущей записи);
  • если правда известна из эталона (`speaker-groundtruth.md`) — ставим её;
  • тёзки («Михаил Еремеев» + «Михаил Саргин») — это РАЗНЫЕ люди, НЕ трогаем.

По умолчанию — сухой прогон: печатает «было → станет» и ничего не пишет.
Запись только с `--apply`, и только после бэкапа (`--backup-dir`).

Опасная тройка (R11): текст реплик не читается и не печатается — работаем только
с именами и метками кластеров.

Запуск:
    python3 tools/fix-split-speaker-memory.py --root /srv/meeting-notary/protocols
    python3 tools/fix-split-speaker-memory.py --root ... --apply --backup-dir /srv/meeting-notary/state/memory-backup-YYYYMMDD
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Правда, установленная вручную по уликам в тексте (обращение/прощание).
# Источник — plans/speaker-groundtruth.md, только строки «✅ доказано».
KNOWN_TRUTH: dict[tuple[str, str], dict[str, str]] = {
    ("anzhee-direktorat", "2026-07-08"): {"SPEAKER_00": "Илья Рыбалка", "SPEAKER_01": "Михаил Еремеев"},
    ("anzhee-direktorat", "2026-07-15"): {"SPEAKER_00": "Михаил Еремеев", "SPEAKER_01": "Илья Рыбалка"},
    ("anzhee-direktorat", "2026-09-23"): {"SPEAKER_00": "Илья Рыбалка", "SPEAKER_01": "Михаил Еремеев"},
}


def split_pairs(mapping: dict[str, str]) -> list[tuple[str, str]]:
    """Пары кластеров, занятые ОДНИМ человеком под разными написаниями.

    Возвращает [(кластер_с_полным_именем, кластер_с_короткой_формой), ...].
    Тёзки с разными фамилиями сюда не попадают: у них обе формы полные."""
    out: list[tuple[str, str]] = []
    items = [(c, n) for c, n in mapping.items() if n]
    for i, (c1, n1) in enumerate(items):
        for c2, n2 in items[i + 1:]:
            p1, p2 = n1.split(), n2.split()
            if p1[0].lower() != p2[0].lower():
                continue
            if len(p1) > 1 and len(p2) == 1:
                out.append((c1, c2))
            elif len(p2) > 1 and len(p1) == 1:
                out.append((c2, c1))
            # обе однословные («Илья»+«Илья») — неразличимо, но это тоже дубль:
            elif len(p1) == 1 and len(p2) == 1:
                out.append((c1, c2))
    return out


def plan_fix(series: str, date: str, mapping: dict[str, str],
             roster: list[str] | None = None) -> tuple[dict[str, str] | None, str]:
    """(новое сопоставление | None, пояснение). None — чинить нечего или нельзя.

    🔴 Ловушка, пойманная на сухом прогоне 25.09: «Михаил» рядом с «Михаил Саргин»
    на планёрке — это НЕ дубль. В составе серии два Михаила (Еремеев и Саргин),
    значит короткая форма принадлежит ВТОРОМУ человеку, и стирать её — потерять
    живого участника. Поэтому: если первым словом в составе серии владеют двое —
    (а) когда второй однозначно вычисляется (он в составе, но не в разметке) —
    дописываем его полное имя; (б) иначе не трогаем и помечаем на ручной разбор."""
    truth = KNOWN_TRUTH.get((series, date))
    if truth:
        return (dict(truth) if truth != mapping else None), "эталон"

    roster = roster or []
    owners: dict[str, list[str]] = {}
    for n in roster:
        if n and len(n.split()) > 1:
            owners.setdefault(n.split()[0].lower(), []).append(n)

    pairs = split_pairs(mapping)
    if not pairs:
        return None, ""
    new = dict(mapping)
    notes: list[str] = []
    for keep, drop in pairs:
        short_name = mapping.get(drop, "")
        first = short_name.split()[0].lower() if short_name else ""
        same_first = owners.get(first, [])
        if len(same_first) > 1:
            # В составе несколько людей с этим именем — короткая форма чья-то ЧУЖАЯ.
            already = {v for v in mapping.values()}
            candidates = [n for n in same_first if n not in already]
            if len(candidates) == 1:
                new[drop] = candidates[0]
                notes.append(f"{drop}: «{short_name}» → «{candidates[0]}» (второй тёзка в составе)")
            else:
                notes.append(f"{drop}: «{short_name}» НЕ трогаю — в составе {len(same_first)} тёзки, кто именно неясен")
            continue
        new.pop(drop, None)
        notes.append(f"{drop}: «{short_name}» убран (дубль к «{mapping.get(keep)}»)")
    return (new if new != mapping else None), "; ".join(notes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Уборка выжимок с разрезанным надвое человеком.")
    ap.add_argument("--root", default="/srv/meeting-notary/protocols")
    ap.add_argument("--registry", default="/srv/meeting-notary/registry/watched.yaml",
                    help="watched.yaml — нужен, чтобы отличить тёзку от дубля")
    ap.add_argument("--apply", action="store_true", help="записать изменения (по умолчанию сухой прогон)")
    ap.add_argument("--backup-dir", default=None, help="куда сложить копии до правки (обязательно с --apply)")
    args = ap.parse_args(argv)

    if args.apply and not args.backup_dir:
        print("! с --apply обязателен --backup-dir", file=sys.stderr)
        return 2

    # Составы серий: без них тёзку («Михаил» при двух Михаилах) не отличить от дубля.
    rosters: dict[str, list[str]] = {}
    try:
        import yaml
        data = yaml.safe_load(Path(args.registry).read_text(encoding="utf-8")) or []
        if isinstance(data, dict):
            data = data.get("watched") or []
        for e in data if isinstance(data, list) else []:
            if isinstance(e, dict) and (e.get("series") or e.get("id")):
                rosters[str(e.get("series") or e.get("id"))] = [str(x) for x in (e.get("expected_participants") or []) if x]
    except Exception as e:  # noqa: BLE001
        print(f"! реестр не прочитан ({e}) — тёзки распознать не смогу, прерываюсь", file=sys.stderr)
        return 2

    root = Path(args.root)
    changed = 0
    for mem in sorted(root.glob("*/[0-9]*-memory.json")):
        series_dir = mem.parent
        date = mem.name[:10]
        try:
            doc = json.loads(mem.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sm = doc.get("speaker_mapping")
        if not isinstance(sm, dict) or not sm:
            continue
        series = str(doc.get("series") or series_dir.name)
        new, note = plan_fix(series, date, {str(k): str(v) for k, v in sm.items()},
                             roster=rosters.get(series))
        if new is None:
            continue
        changed += 1
        print(f"{date} {series}  ({note})")
        print(f"   было:  {sm}")
        print(f"   стало: {new}")
        if args.apply:
            backup = Path(args.backup_dir)
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mem, backup / f"{series}-{mem.name}")
            doc["speaker_mapping"] = new
            mem.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"{'ИСПРАВЛЕНО' if args.apply else 'БУДЕТ ИСПРАВЛЕНО'}: {changed} выжимок")
    if not args.apply:
        print("это сухой прогон — ничего не записано; для записи: --apply --backup-dir <папка>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
