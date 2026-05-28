#!/usr/bin/env python3
"""CLI: regenerate-protocol.py <series> <date> — пересоздаёт `<date>-protokol.md`.

Назначение:
  - Backfill (Ф4): прогнать `generate_protocol` на архивных транскриптах.
  - Отладка промта: после правки `~/Projects/me/methods/kak-delat-protokol-vstrechi.md`
    можно сразу проверить эффект, не дожидаясь следующей реальной финализации.
  - Ручной retry: если автогенерация в finalize упала (CLI/timeout) —
    `regenerate-protocol.py <series> <date>` исправит.

<series> — имя ПАПКИ в `~/Projects/me/встречи/` (legacy `<series>-<date>/`
ИЛИ новая `<series>/`). После Ф7-миграции — только новая.
<date> — `YYYY-MM-DD`.

CLI читает `<root>/<series>/<date>.md` (или ищет `<date>.md` в legacy-папке),
прогоняет `regenerate_protocol_for_meeting`, atomic-перезаписывает
`<root>/<series>/<date>-protokol.md`.

Опции:
  --root <dir>          — корень папки встреч (default `~/Projects/me/встречи`).
  --out <file>          — явное имя выходного файла (для backfill sales-quality
                          → `<date>-protokol-auto.md`, чтобы не затереть эталон).
  --duration <int>      — длительность встречи в минутах (для шапки протокола).
  --participants "a,b"  — участники через запятую (для шапки).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
sys.path.insert(0, str(NOTARY_DIR))

from lib.llm_postprocess import (  # noqa: E402
    ProtocolGenerationError,
    regenerate_protocol_for_meeting,
)

DEFAULT_ROOT = os.path.expanduser("~/Projects/me/встречи")


def _resolve_paths(root: Path, series: str, date: str, out_name: str | None) -> tuple[Path, Path]:
    """Возвращает (transcript_path, protocol_path).

    Поиск transcript_path:
      1. `<root>/<series>/<date>.md` (новая структура, после Ф7).
      2. `<root>/<series>-<date>/<date>.md` (legacy collector до Ф7).
      3. `<root>/<series>/<date>.md` — если папка та же, что в (1), но файла нет —
         финальный путь для protocol_path = той же папке.
    """
    new_layout = root / series / f"{date}.md"
    legacy_layout = root / f"{series}-{date}" / f"{date}.md"
    if new_layout.is_file():
        transcript = new_layout
    elif legacy_layout.is_file():
        transcript = legacy_layout
    else:
        raise SystemExit(
            f"transcript не найден ни по {new_layout}, ни по {legacy_layout}\n"
            f"Доступные папки в {root}:\n  "
            + "\n  ".join(sorted(p.name for p in root.iterdir() if p.is_dir()))
        )
    out_file = out_name or f"{date}-protokol.md"
    protocol = transcript.parent / out_file
    return transcript, protocol


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Перегенерация .md-протокола встречи из транскрипта."
    )
    parser.add_argument("series", help="Имя серии (или базовое имя legacy-папки)")
    parser.add_argument("date", help="Дата встречи YYYY-MM-DD")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help=f"Корень папки встреч (default: {DEFAULT_ROOT})")
    parser.add_argument("--out", default=None,
                        help="Имя выходного файла (default: <date>-protokol.md)")
    parser.add_argument("--duration", type=int, default=None,
                        help="Длительность в минутах (для шапки)")
    parser.add_argument("--participants", default=None,
                        help="Участники через запятую (для шапки)")
    args = parser.parse_args(argv)

    root = Path(os.path.expanduser(args.root))
    if not root.is_dir():
        raise SystemExit(f"--root не существует: {root}")

    transcript_path, protocol_path = _resolve_paths(root, args.series, args.date, args.out)

    meta: dict = {
        "series": args.series,
        "date": args.date,
        "transcript_filename": transcript_path.name,
    }
    if args.duration is not None:
        meta["duration"] = args.duration
    if args.participants:
        meta["expectedParticipants"] = [p.strip() for p in args.participants.split(",") if p.strip()]
        meta["participants"] = []

    print(f"[regen] transcript: {transcript_path}", file=sys.stderr)
    print(f"[regen] protocol:   {protocol_path}", file=sys.stderr)
    try:
        regenerate_protocol_for_meeting(
            transcript_path=transcript_path,
            protocol_path=protocol_path,
            meeting_meta=meta,
            meeting_sid=f"cli-{args.series}-{args.date}",
        )
    except ProtocolGenerationError as e:
        print(f"[regen] FAILED: {e}", file=sys.stderr)
        return 1
    print(f"[regen] OK: {protocol_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
