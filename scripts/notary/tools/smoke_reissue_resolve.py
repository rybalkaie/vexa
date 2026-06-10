#!/usr/bin/env python3
"""Smoke-связки резолва перевыпуска (ISS-1, Ф2, REQ 1.4 / идея ревью №2).

Проверяет НЕ «env задан», а что для свежей delivered-meta цепочка
`feedback_reissue._resolve_paths` (починена в Ф1) фактически резолвится в
ЧИТАЕМЫЙ транскрипт — то есть статус перевыпуска был бы ≠ «transcript missing».
Ловит расхождение папок НЕС2 (meta в `_tmp/transcripts/<sid>.meta.json`,
транскрипт в `<output_dir>/<series>/…`) ДО следующей реальной правки.

Два режима:

  --self-test  (дефолт) — синтетически воспроизводит ПРОД-раскладку во временной
               папке (meta отдельно от транскрипта, survivor `…-tm-<id>.md`) и
               ассертит, что резолв вернул читаемый путь. Годен для CI/гейта.

  --scan DIR   — боевой прогон на батч-деплое: сканирует каталог транскриптов
               (`$MEETING_NOTARY_FEEDBACK*`/`_tmp/transcripts`) на `*.meta.json`,
               для каждой delivered-meta запускает резолв и печатает PASS/FAIL.
               Exit≠0, если хоть одна delivered-meta не резолвится в читаемый
               транскрипт. Так батч-деплой ловит НЕС2 на боевых данных.

  --meta FILE  — то же для одного meta-файла.

ПРИВАТНОСТЬ (CLAUDE.md / R9): текст транскрипта НЕ читаем и НЕ логируем — только
метаданные (серия, дата, путь, размер в байтах, флаг читаемости). Размер берём
`os.path.getsize` (stat, без чтения содержимого).

Запуск:
  python3 tools/smoke_reissue_resolve.py                 # self-test
  python3 tools/smoke_reissue_resolve.py --scan /home/dev/meeting-notary/_tmp/transcripts
  python3 tools/smoke_reissue_resolve.py --meta <sid>.meta.json
Тест-обёртка: python3 -m unittest tests.test_smoke_reissue_resolve
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent       # …/scripts/notary/tools
_NOTARY = _HERE.parent                          # …/scripts/notary
_SCRIPTS = _NOTARY.parent                       # …/scripts
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from notary.lib import feedback_reissue  # noqa: E402


# --------------------------------------------------------------------------
# Резолв одной meta → структурированный вердикт (без текста транскрипта)
# --------------------------------------------------------------------------

def _date_from_meta(meta: dict) -> str:
    """Дата встречи: persisted `date` (пишется на доставке, finalize:1187),
    иначе из `startTs` (`YYYY-MM-DD`)."""
    d = (meta.get("date") or "").strip()
    if d:
        return d
    ts = meta.get("startTs") or ""
    return ts[:10]


def _state_from_meta(meta_path: Path, meta: dict) -> dict:
    """Минимальный state для `_resolve_paths` (он читает только series/date)."""
    return {
        "series": meta.get("series"),
        "date": _date_from_meta(meta),
        "meta_path": str(meta_path),
    }


def _is_delivered(meta: dict) -> bool:
    """Meta относится к ДОСТАВЛЕННОМУ протоколу (есть что перевыпускать)."""
    deliv = meta.get("delivered")
    if isinstance(deliv, list) and deliv:
        return True
    # Persisted-путь тоже признак свежей delivered-meta (Ф1 пишет оба сразу).
    return bool(meta.get("transcript_path"))


def check_meta(meta_path: Path) -> dict:
    """Прогоняет meta через `_resolve_paths` и возвращает вердикт-словарь.

    status ∈ {ok | transcript missing | transcript not readable | protocol missing}
    — повторяет ворота `reissue_one` (feedback_reissue.py:648-651), поэтому «ok»
    означает «перевыпуск дошёл бы до генерации, а не упал бы transcript missing».
    Текст транскрипта не читается; `chars`/`bytes` — из stat, не из содержимого.
    """
    meta_path = Path(meta_path)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"meta": meta_path.name, "status": "meta unreadable",
                "ok": False, "error": str(e)}

    state = _state_from_meta(meta_path, meta)
    transcript, protocol = feedback_reissue._resolve_paths(state, meta_path, meta)

    res: dict = {
        "meta": meta_path.name,
        "series": state.get("series"),
        "date": state.get("date"),
        "transcript": str(transcript) if transcript else None,
        "protocol": str(protocol) if protocol else None,
        "bytes": None,
        "ok": False,
        "status": "transcript missing",
    }
    if not transcript or not Path(transcript).is_file():
        return res
    if not os.access(transcript, os.R_OK):
        res["status"] = "transcript not readable"
        return res
    # Размер — метаданные (stat), не читаем содержимое (личные данные).
    try:
        res["bytes"] = os.path.getsize(transcript)
    except OSError:
        res["bytes"] = None
    if not protocol or not Path(protocol).is_file():
        res["status"] = "protocol missing"
        return res
    res["ok"] = True
    res["status"] = "ok"
    return res


# --------------------------------------------------------------------------
# Синтетический self-test — прод-раскладка НЕС2 во временной папке
# --------------------------------------------------------------------------

def synthetic_self_test() -> dict:
    """Строит прод-раскладку (meta ОТДЕЛЬНО от транскрипта) и проверяет резолв.

    Воспроизводит НЕС2: delivered-meta в `_tmp/transcripts/<sid>.meta.json`,
    транскрипт-survivor `<output_dir>/<series>/<date>-<date>-tm-<id>.md`. Ассертит,
    что `_resolve_paths` вернул ЧИТАЕМЫЙ транскрипт и статус «ok».

    Возвращает вердикт `check_meta`; бросает AssertionError при провале.
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        transcripts = base / "_tmp" / "transcripts"           # meta живёт здесь
        protocols = base / "protocols"                        # транскрипт — здесь
        series, date, sid = "smoke-series", "2026-06-10", "tm-987654"
        sdir = protocols / series
        sdir.mkdir(parents=True, exist_ok=True)
        transcripts.mkdir(parents=True, exist_ok=True)

        # survivor двойной/collision-раскладки + протокол рядом
        tpath = sdir / f"{date}-{date}-{sid}.md"
        ppath = sdir / f"{date}-protokol.md"
        tpath.write_text("00:00 Спикер 1: реплика\n", encoding="utf-8")
        ppath.write_text("#протоколвстречи\n\n## Решения\n- пункт\n", encoding="utf-8")

        meta = {
            "series": series,
            "date": date,
            "sessionUid": f"auto-{sid}-20260610T100000Z",
            "transcript_path": str(tpath),   # Ф1 персистит точный путь
            "protocol_path": str(ppath),
            "delivered": [{"chat_id": -100123, "message_ids": [42], "at": "x"}],
        }
        meta_path = transcripts / f"{sid}.meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        res = check_meta(meta_path)
        assert res["ok"], f"резолв провалился: {res}"
        assert res["status"] == "ok", f"статус != ok: {res}"
        assert res["transcript"] == str(tpath), f"не тот транскрипт: {res}"
        # читаемость подтверждена через size>0 (содержимое НЕ читали)
        assert res["bytes"] and res["bytes"] > 0, f"пустой/нечитаемый транскрипт: {res}"
        return res


# --------------------------------------------------------------------------
# Боевой скан каталога транскриптов
# --------------------------------------------------------------------------

def scan(transcripts_dir: Path) -> list[dict]:
    """Прогоняет все delivered `*.meta.json` в каталоге. Не-delivered — `skipped`."""
    out: list[dict] = []
    for mp in sorted(Path(transcripts_dir).glob("*.meta.json")):
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            out.append({"meta": mp.name, "status": "meta unreadable", "ok": False})
            continue
        if not _is_delivered(meta):
            out.append({"meta": mp.name, "status": "skipped (not delivered)", "ok": None})
            continue
        out.append(check_meta(mp))
    return out


def _fmt(r: dict) -> str:
    mark = "PASS" if r.get("ok") else ("SKIP" if r.get("ok") is None else "FAIL")
    size = r.get("bytes")
    size_s = f"{size}B" if isinstance(size, int) else "-"
    return (f"[{mark}] {r.get('meta')}  series={r.get('series')!r} "
            f"date={r.get('date')!r} status={r.get('status')!r} bytes={size_s}")


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Smoke-связки резолва перевыпуска (ISS-1)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--self-test", action="store_true", help="синтетический прод-кейс (дефолт)")
    g.add_argument("--scan", metavar="DIR", help="скан каталога транскриптов на delivered-meta")
    g.add_argument("--meta", metavar="FILE", help="проверить один meta-файл")
    args = p.parse_args(argv)

    if args.scan:
        results = scan(Path(args.scan))
        for r in results:
            print(_fmt(r))
        checked = [r for r in results if r.get("ok") is not None]
        failed = [r for r in checked if not r.get("ok")]
        print(f"\nИтог: {len(checked)} delivered-meta проверено, "
              f"{len(checked) - len(failed)} PASS, {len(failed)} FAIL, "
              f"{len(results) - len(checked)} SKIP.")
        return 1 if failed else 0

    if args.meta:
        r = check_meta(Path(args.meta))
        print(_fmt(r))
        return 0 if r.get("ok") else 1

    # дефолт — self-test
    try:
        r = synthetic_self_test()
    except AssertionError as e:
        print(f"[FAIL] self-test: {e}")
        return 1
    print(_fmt(r))
    print("[OK] self-test: прод-раскладка НЕС2 резолвится в читаемый транскрипт.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
