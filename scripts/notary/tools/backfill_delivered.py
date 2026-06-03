#!/usr/bin/env python3
"""Бэкфилл `meta.delivered` по уже-обработанному бэклогу встреч (REQ 1.8, УПУ1).

ЗАЧЕМ. После деплоя Ф1 collector на каждом тике перезапускает finalize по
встречам без `meta.delivered`. Если WAV уже почищен (P0-регрессия записи) —
finalize отдаёт rc=3 «WAV not found» → ложный алерт владельцу по КАЖДОЙ старой
встрече. Чтобы первый тик не сыпал rc=3, помечаем «обработанным» бэклог, по
которому протокол УЖЕ собран (есть .md в архиве протоколов).

БЕЗОПАСНОСТЬ (вариант А плана — «помечать, не править»):
  Помечаем ТОЛЬКО встречи, где ОДНОВРЕМЕННО:
    1. WAV отсутствует на диске (иначе finalize отработает штатно — не трогаем,
       пометка delivered ЗАБЛОКИРОВАЛА бы его легитимную финализацию);
    2. `meta.delivered` пустой (нечего перетирать);
    3. протокол УЖЕ существует (.md найден) — т.е. встреча реально обработана.
  Если WAV нет И протокола нет → это РЕАЛЬНАЯ потеря: НЕ помечаем (пусть rc=3
  легитимно дойдёт до владельца, это зона Ф2).

  Маркер пишется как массив с одной записью:
    delivered=[{chat_id:null, message_ids:["backfill-<sid>"], at:<iso>,
                decision:"backfill", note:"…"}]
  `message_ids` непустой + decision≠"partial-failure" → удовлетворяет и
  collector._delivery_done (скип), и finalize._is_success_record (rc=10).
  chat_id=null и decision="backfill" честно говорят «реальной доставки сейчас
  не было, протокол уже в архиве».

ЗАПУСК (на VPS, под dev — там лежат meta.json и /srv/.../protocols):
    # сухой прогон (НИЧЕГО не пишет, печатает план):
    python3 tools/backfill_delivered.py
    # применить:
    python3 tools/backfill_delivered.py --apply

Пути переопределяемы флагами/env для теста на маке.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TRANSCRIPTS = os.environ.get(
    "MEETING_NOTARY_TRANSCRIPTS_DIR", "/home/dev/meeting-notary/_tmp/transcripts"
)
# Где искать протоколы: плоский finalize-output + series-архив (mirror source).
DEFAULT_PROTOCOLS_FLAT = os.environ.get(
    "MEETING_NOTARY_PROTOCOLS_FLAT", "/home/dev/meeting-notary/_tmp/protocols"
)
DEFAULT_PROTOCOLS_ARCHIVE = os.environ.get(
    "MEETING_NOTARY_PROTOCOLS_DIR", "/srv/meeting-notary/protocols"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _delivery_done(meta: dict) -> bool:
    """Та же логика, что collector._delivery_done / finalize._is_success_record."""
    raw = meta.get("delivered")
    if isinstance(raw, dict):
        records = [raw]
    elif isinstance(raw, list):
        records = [r for r in raw if isinstance(r, dict)]
    else:
        return False
    for r in records:
        if (r.get("message_ids") or []) and r.get("decision") != "partial-failure":
            return True
    return False


def _date_from_meta(meta: dict, sid: str) -> str:
    raw = meta.get("date") or meta.get("startTs") or ""
    if isinstance(raw, str) and len(raw) >= 10 and raw[:4].isdigit():
        return raw[:10]
    # fallback: дата из имени сессии `YYYY-MM-DD-tm-…`
    if len(sid) >= 10 and sid[:4].isdigit():
        return sid[:10]
    return ""


def _one_off_id(meta: dict, sid: str) -> str:
    explicit = meta.get("oneOffId")
    if isinstance(explicit, str) and explicit:
        return explicit
    s = meta.get("sessionUid") or sid or ""
    if "tm-" in s:
        tail = s.split("tm-", 1)[1]
        parts = tail.rsplit("-", 1)
        if len(parts) == 2 and "T" in parts[1] and parts[1].endswith("Z"):
            return f"tm-{parts[0]}"
        return f"tm-{tail}"
    return s or "unknown"


def _protocol_paths(meta: dict, sid: str, flat: Path, archive: Path) -> list[Path]:
    """Кандидаты на местоположение протокола (.md / -protokol.md)."""
    date_str = _date_from_meta(meta, sid)
    series = (meta.get("series") or "").strip()
    cands: list[Path] = [flat / f"{sid}.md"]
    if date_str:
        if series:
            cands.append(archive / series / f"{date_str}.md")
            cands.append(archive / series / f"{date_str}-protokol.md")
            cands.append(flat / series / f"{date_str}.md")
        else:
            oid = _one_off_id(meta, sid)
            cands.append(archive / "_one-off" / f"{date_str}-{oid}" / f"{date_str}.md")
            cands.append(flat / f"{date_str}-{sid}.md")
    return cands


def _wav_present(meta: dict, tdir: Path, sid: str) -> bool:
    """WAV физически на диске ХОСТА?

    ВАЖНО: `meta.files.wav` — путь ВНУТРИ docker-контейнера (`/transcripts/<sid>.wav`),
    на хосте его НЕТ (bind-mount монтирует контейнерный /transcripts в host tdir).
    Поэтому `os.path.exists(meta.files.wav)` на хосте всегда False — нельзя по нему
    судить о наличии WAV. Проверяем хостовый путь `<tdir>/<sid>.wav` напрямую.
    """
    host_wav = tdir / f"{sid}.wav"
    if host_wav.exists():
        return True
    # Подстраховка: если meta.files.wav вдруг АБСОЛЮТНЫЙ хостовый путь (не контейнерный
    # /transcripts/...), уважаем и его.
    wav = (meta.get("files") or {}).get("wav")
    return bool(wav) and not str(wav).startswith("/transcripts/") and os.path.exists(wav)


def _atomic_write(path: Path, meta: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Бэкфилл meta.delivered по обработанному бэклогу (REQ 1.8).")
    ap.add_argument("--apply", action="store_true", help="реально записать (по умолчанию — dry-run)")
    ap.add_argument("--transcripts-dir", default=DEFAULT_TRANSCRIPTS)
    ap.add_argument("--protocols-flat", default=DEFAULT_PROTOCOLS_FLAT)
    ap.add_argument("--protocols-archive", default=DEFAULT_PROTOCOLS_ARCHIVE)
    args = ap.parse_args(argv)

    tdir = Path(os.path.expanduser(args.transcripts_dir))
    flat = Path(os.path.expanduser(args.protocols_flat))
    archive = Path(os.path.expanduser(args.protocols_archive))

    if not tdir.is_dir():
        print(f"ОШИБКА: нет каталога transcripts: {tdir}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== backfill_delivered [{mode}] ===")
    print(f"transcripts: {tdir}")
    print(f"protocols:   flat={flat}  archive={archive}\n")

    metas = sorted(tdir.glob("*.meta.json"))
    n_mark = n_skip_done = n_skip_wav = n_skip_noproto = 0
    for mp in metas:
        sid = mp.name[: -len(".meta.json")]
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ⚠️  {sid}: битый meta.json ({e}) — пропуск")
            continue
        if not isinstance(meta, dict):
            print(f"  ⚠️  {sid}: meta не dict — пропуск")
            continue

        if _delivery_done(meta):
            n_skip_done += 1
            continue  # уже помечен
        if _wav_present(meta, tdir, sid):
            n_skip_wav += 1
            print(f"  ⏭  {sid}: WAV на диске — finalize отработает штатно, НЕ трогаем")
            continue
        proto = next((p for p in _protocol_paths(meta, sid, flat, archive) if p.exists()), None)
        if proto is None:
            n_skip_noproto += 1
            print(f"  ❗ {sid}: WAV нет И протокола нет — РЕАЛЬНАЯ потеря, НЕ помечаем (legit rc=3)")
            continue

        n_mark += 1
        print(f"  ✅ {sid}: WAV нет, протокол есть ({proto}) → пометить delivered=backfill")
        if args.apply:
            meta["delivered"] = [{
                "chat_id": None,
                "message_ids": [f"backfill-{sid}"],
                "at": _now_iso(),
                "decision": "backfill",
                "note": "Бэкфилл Ф1 (bot-notarius-full): протокол уже в архиве, WAV почищен, повторная доставка не требуется.",
            }]
            _atomic_write(mp, meta)

    print("\n--- итог ---")
    print(f"  пометить delivered:        {n_mark}")
    print(f"  пропуск (уже delivered):   {n_skip_done}")
    print(f"  пропуск (WAV на диске):    {n_skip_wav}")
    print(f"  пропуск (реальная потеря): {n_skip_noproto}")
    if not args.apply and n_mark:
        print("\nDRY-RUN. Для записи: python3 tools/backfill_delivered.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
