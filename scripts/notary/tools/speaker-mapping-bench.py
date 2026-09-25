#!/usr/bin/env python3
"""Стенд: воспроизводит расстановку имён по говорящим на СОХРАНЁННЫХ расшифровках.

Зачем. Правки маппинга нельзя принимать «на глаз»: одна и та же правка чинит одну
серию и ломает другую. Стенд прогоняет `map_all` офлайн — без сети, без STT, без
LLM и без денег — и показывает, что изменилось.

План: ../../../plans/2026-09-25-speaker-attribution-fix.md, Фаза 1.
Эталон правильных ответов: ../../../plans/speaker-groundtruth.md

Что делает:
  1. читает архивные расшифровки `<протоколы>/<серия>/_transcripts/<дата>.json`
     (там лежит сырой ответ AssemblyAI в `raw_json`);
  2. собирает вход РОВНО как финализатор: `expected_participants` серии из
     watched.yaml + (по умолчанию) добавление первого слова отдельным кандидатом —
     это и есть воспроизводимый дефект;
  3. берёт якорь серии из выжимки ПРЕДЫДУЩЕЙ встречи (`<дата>-memory.json`);
  4. зовёт `map_all` БЕЗ LLM-добивки (детерминированно) и печатает результат;
  5. сверяет с эталоном и считает две метрики.

Опасная тройка (R11): текст реплик НЕ печатается и никуда не пишется — только
кластеры, имена и счётчики.

Запуск (на сервере, где стоит venv нотариуса):
    PYTHONPATH=/srv/meeting-notary/vexa/scripts \\
    /srv/meeting-notary/venv-cli/bin/python tools/speaker-mapping-bench.py \\
        --root /srv/meeting-notary/protocols --registry /srv/meeting-notary/registry/watched.yaml

Полезные ключи:
    --series anzhee-direktorat   только одна серия
    --no-expand-first-word       смоделировать Фазу 2 (без дубля кандидата)
    --json out.json              сохранить результат для сравнения «до/после»
    --compare before.json        показать дифф против прошлого прогона
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
if str(NOTARY_DIR) not in sys.path:
    sys.path.insert(0, str(NOTARY_DIR))

# ---------------------------------------------------------------------------
# Эталон: только случаи, доказанные уликой в тексте (обращение/прощание).
# Источник — plans/speaker-groundtruth.md, колонка «✅ доказано».
# Формат: (серия, дата) -> {кластер: имя}
# ---------------------------------------------------------------------------
GROUND_TRUTH: dict[tuple[str, str], dict[str, str]] = {
    # «Илья Рыбалка: Илья, приветствую» → говорит Михаил; «Илья: Привет, Миш» → Илья
    ("anzhee-direktorat", "2026-07-08"): {"SPEAKER_00": "Илья Рыбалка", "SPEAKER_01": "Михаил Еремеев"},
    ("anzhee-direktorat", "2026-07-15"): {"SPEAKER_00": "Михаил Еремеев", "SPEAKER_01": "Илья Рыбалка"},
    # «Запись просто поставлю», «я ему скину, он обработает» → говорит Илья
    ("anzhee-direktorat", "2026-09-23"): {"SPEAKER_00": "Илья Рыбалка", "SPEAKER_01": "Михаил Еремеев"},
}


def load_registry(path: Path) -> dict[str, list[str]]:
    """series -> expected_participants из watched.yaml."""
    import yaml  # noqa: PLC0415

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as e:
        print(f"! реестр не прочитан ({e}) — иду без expected_participants", file=sys.stderr)
        return {}
    # Боевой watched.yaml — словарь с ключом `watched`; поддерживаем и голый список.
    if isinstance(data, dict):
        data = data.get("watched") or data.get("meetings") or []
    out: dict[str, list[str]] = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        series = entry.get("series") or entry.get("id")
        names = entry.get("expected_participants") or []
        if series and names:
            out[str(series)] = [str(n) for n in names if n]
    return out


def utterances_from_raw(raw: dict) -> list:
    """raw_json AssemblyAI -> list[Utterance]. Пустой список, если формат чужой."""
    from lib.assemblyai_client import Utterance  # noqa: PLC0415

    items = (raw or {}).get("utterances") or []
    out = []
    for u in items:
        spk, text = u.get("speaker"), u.get("text")
        if not spk or not text:
            continue
        out.append(Utterance(
            speaker=str(spk),
            start=float(u.get("start") or 0) / 1000.0,
            end=float(u.get("end") or 0) / 1000.0,
            text=str(text),
        ))
    return out


def expand_expected(expected: list[str], *, expand_first_word: bool) -> list[str]:
    """Пул кандидатов — как его строит finalize-meeting.py:861–870.

    `expand_first_word=True` воспроизводит текущее боевое поведение: первое слово
    имени кладётся ОТДЕЛЬНЫМ кандидатом («Илья Рыбалка» + «Илья»). Именно из-за
    этого на 1:1 кандидатов четверо и защита двух говорящих не включается.
    `False` моделирует Фазу 2."""
    out: list[str] = []
    for full in expected:
        if not full:
            continue
        if full not in out:
            out.append(full)
        if not expand_first_word:
            continue
        first = full.split()[0] if full.split() else ""
        if first and first not in out:
            out.append(first)
    return out


def previous_anchor(series_dir: Path, date: str, *, confirmed_only: bool) -> dict[str, str]:
    """Якорь = speaker_mapping последней подходящей выжимки СТРОГО до `date`.

    Повторяет `series_memory.resolve_speaker_anchor`. `confirmed_only=True` —
    поведение после Фазы 5: берём только разметку с меткой «подтвердил человек»
    (`speaker_mapping_confirmed`). False — как было в бою до правки."""
    anchor: dict[str, str] = {}
    for mem in sorted(series_dir.glob("*-memory.json")):
        mem_date = mem.name[:10]
        if mem_date >= date:
            break
        try:
            doc = json.loads(mem.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            continue
        if confirmed_only and not doc.get("speaker_mapping_confirmed"):
            continue
        sm = doc.get("speaker_mapping")
        if isinstance(sm, dict) and sm:
            anchor = {str(k): str(v) for k, v in sm.items() if k and v}
    return anchor


def run_one(tr_path: Path, expected: list[str], *, expand_first_word: bool,
            confirmed_anchor_only: bool = False) -> dict | None:
    """Один прогон. Возвращает метаданные результата (без текста реплик)."""
    from lib.assemblyai_client import to_aligned_turns  # noqa: PLC0415
    from lib.name_mapping import map_all  # noqa: PLC0415

    try:
        doc = json.loads(tr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (doc.get("stt_backend") or "").lower() not in ("assemblyai", ""):
        return None  # Speechmatics-архивы другого формата — пропускаем
    utts = utterances_from_raw(doc.get("raw_json") or {})
    if not utts:
        return None

    series = str(doc.get("series") or tr_path.parent.parent.name)
    date = str(doc.get("date") or tr_path.stem)
    turns = to_aligned_turns(utts)
    pool = expand_expected(expected, expand_first_word=expand_first_word)
    # Боевое правило (Ф5): строгий режим включается САМ на встречах вдвоём.
    pair = len([n for n in expected if n]) == 2
    anchor = previous_anchor(tr_path.parent.parent, date,
                             confirmed_only=confirmed_anchor_only or pair)

    res = map_all(turns, pool, anchor=anchor, roster=None)
    clusters = sorted({t.speaker for t in turns if t.speaker})
    mapping = dict(res.cluster_to_name)

    names = [n for n in mapping.values() if n]
    firsts = [n.split()[0] for n in names]
    return {
        "series": series,
        "date": date,
        "clusters": len(clusters),
        "people_expected": len(expected),
        "mapping": mapping,
        "sources": list(res.sources_used),
        "unresolved": len(res.unresolved_clusters),
        # инвариант: один человек не может занимать два голоса
        "split_person": len(set(firsts)) < len(firsts),
        # инвариант: голосов не больше, чем людей (+1 на неожиданного участника)
        "too_many_voices": bool(expected) and len(clusters) > len(expected) + 1,
    }


def same_person(a: str | None, b: str | None) -> bool:
    """«Илья» и «Илья Рыбалка» — ОДИН человек, «Михаил Еремеев» и «Михаил Саргин» — РАЗНЫЕ.

    Сравнение по строке врало бы: до Фазы 2 маппер выдаёт короткую форму, эталон
    хранит полную. Правило: совпало первое слово И хотя бы одна из форм короткая
    (то есть это форма того же имени, а не однофамильцы/тёзки с разными фамилиями)."""
    if not a or not b:
        return False
    if a == b:
        return True
    pa, pb = a.split(), b.split()
    if pa[0].lower() != pb[0].lower():
        return False
    return len(pa) == 1 or len(pb) == 1


def verdict_against_truth(rows: list[dict]) -> tuple[int, int, int, list[str]]:
    """(верно, неверно, частично, пояснения) по доказанным случаям.

    «Верно» = каждый кластер эталона размечен тем же человеком. «Частично» =
    ошибок нет, но часть кластеров не размечена (маппер воздержался) — это НЕ
    провал: лучше «Спикер N», чем уверенно неверный автор."""
    ok = bad = partial = 0
    notes: list[str] = []
    for r in rows:
        want = GROUND_TRUTH.get((r["series"], r["date"]))
        if not want:
            continue
        got = r["mapping"]
        wrong = [c for c, n in want.items() if got.get(c) and not same_person(got.get(c), n)]
        missing = [c for c in want if not got.get(c)]
        if wrong:
            bad += 1
            notes.append(f"  ❌ {r['date']} {r['series']}: перепутал {wrong} → {got}")
        elif missing:
            partial += 1
            notes.append(f"  ⚪ {r['date']} {r['series']}: не размечено {missing} (ошибок нет)")
        else:
            ok += 1
    return ok, bad, partial, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Офлайн-стенд расстановки имён по говорящим.")
    ap.add_argument("--root", default="/srv/meeting-notary/protocols", help="корень папок серий")
    ap.add_argument("--registry", default="/srv/meeting-notary/registry/watched.yaml")
    ap.add_argument("--series", default=None, help="только эта серия")
    ap.add_argument("--no-expand-first-word", action="store_true",
                    help="не класть первое слово отдельным кандидатом (модель Фазы 2)")
    ap.add_argument("--confirmed-anchor-only", action="store_true",
                    help="наследовать только подтверждённую человеком разметку (модель Фазы 5)")
    ap.add_argument("--json", default=None, help="сохранить результат в файл")
    ap.add_argument("--compare", default=None, help="сравнить с прошлым прогоном (файл --json)")
    args = ap.parse_args(argv)

    root = Path(args.root)
    registry = load_registry(Path(args.registry))
    rows: list[dict] = []
    for tr in sorted(root.glob("*/_transcripts/*.json")):
        series_dir = tr.parent.parent
        if args.series and series_dir.name != args.series:
            continue
        expected = registry.get(series_dir.name, [])
        r = run_one(tr, expected, expand_first_word=not args.no_expand_first_word,
                    confirmed_anchor_only=args.confirmed_anchor_only)
        if r:
            rows.append(r)

    if not rows:
        print("нет расшифровок для прогона — проверь --root")
        return 2

    split = [r for r in rows if r["split_person"]]
    many = [r for r in rows if r["too_many_voices"]]
    ok, bad, partial, notes = verdict_against_truth(rows)

    print(f"прогнано расшифровок: {len(rows)}")
    print(f"эталон: верно {ok}, НЕВЕРНО {bad}, частично {partial} (из {len(GROUND_TRUTH)})")
    for n in notes:
        print(n)
    print(f"человек разрезан надвое: {len(split)}")
    print(f"голосов больше, чем людей+1: {len(many)}")
    print()
    for r in sorted(rows, key=lambda x: (x["series"], x["date"])):
        want = GROUND_TRUTH.get((r["series"], r["date"]))
        mark = ""
        if want:
            wrong = [c for c, n in want.items() if r["mapping"].get(c) and not same_person(r["mapping"].get(c), n)]
            missing = [c for c in want if not r["mapping"].get(c)]
            mark = " ❌ЭТАЛОН-МИМО" if wrong else (" ⚪ЧАСТИЧНО" if missing else " ✅ЭТАЛОН-ОК")
        flags = (" 🔴разрезан" if r["split_person"] else "") + (" 🟠голосов>людей" if r["too_many_voices"] else "")
        print(f"{r['date']} {r['series'][:34]:34} голосов={r['clusters']} "
              f"людей={r['people_expected']} источники={','.join(r['sources']) or '—'}"
              f"{flags}{mark}")
        print(f"      {r['mapping']}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nрезультат сохранён: {args.json}")

    if args.compare:
        try:
            before = {(r["series"], r["date"]): r for r in json.loads(Path(args.compare).read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError) as e:
            print(f"! не прочитал {args.compare}: {e}")
            return 1
        changed = 0
        print("\n=== ИЗМЕНЕНИЯ против прошлого прогона ===")
        for r in rows:
            b = before.get((r["series"], r["date"]))
            if not b or b["mapping"] == r["mapping"]:
                continue
            changed += 1
            print(f"{r['date']} {r['series']}")
            print(f"   было:  {b['mapping']}")
            print(f"   стало: {r['mapping']}")
        print(f"изменилось встреч: {changed}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
