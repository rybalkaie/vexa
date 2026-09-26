"""Замок финализации встречи `finalize-<sid>.lock` — один на всех, кто её финализирует.

ЗАЧЕМ (план 2026-09-23-telemost-update-resilience, фаза 5). Финализацию одной и той
же встречи могут запустить трое:

* сборщик (`collector._session_finalize_lock`) — раз в 5 минут подбирает каждую
  `<transcripts>/<sid>.meta.json` без отметки `delivered`;
* повторы (`retry_failed._try_finalize`) — по `_failed/<sid>.retry-state.json`;
* человек или Альфред руками — восстановление протокола из присланной записи
  (`tools/ingest-recording.py`), ~25 минут на запись.

Первые двое давно берут неблокирующий `flock` на `finalize-<sid>.lock`. Ручной путь
его не брал: пока он шёл, сборщик видел в очереди ту же встречу без `delivered` и
запускал вторую финализацию — два протокола в группу и двойная оплата расшифровки.

🔴 ПАПКА ЗАМКА У СБОРЩИКА И У ПОВТОРОВ РАЗНАЯ, И ВЕРНО ЕЁ ЗНАЕТ ТОЛЬКО СЛУЖБА.
Сборщик берёт `MEETING_NOTARY_STATE_DIR` (переменная из юнита systemd, её может не
быть в ручной оболочке) или свой дефолт `~/Library/Logs/meeting-notary`; повторы —
`--state-dir`, по умолчанию `/srv/meeting-notary/state`. Поэтому ручной путь держит
замок ВО ВСЕХ этих местах сразу: каким бы ни был юнит сборщика, он увидит «занято».
Точную папку можно задать явно — `MEETING_NOTARY_FINALIZE_LOCK_DIRS` (через `:`).

🔴 ИМЯ ФАЙЛА — ДВА ВАРИАНТА. Сборщик чистит имя сессии (`[^A-Za-z0-9._-]` → `_`),
повторы берут его как есть. Для обычных имён это один файл; для серии с кириллицей в
имени — два. Ручной путь берёт оба.

Замок неблокирующий: занят → «эту встречу уже кто-то финализирует», второй не
запускается. Отпускается закрытием файла — и при штатном выходе, и при смерти
процесса (ядро снимает `flock` само).
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
from pathlib import Path
from typing import Iterator, List, Optional

logger = logging.getLogger("notary.finalize_lock")

#: Папка замков сборщика по умолчанию — ровно выражение `collector.STATE_DIR`.
COLLECTOR_DEFAULT_STATE_DIR = "~/Library/Logs/meeting-notary"
#: Папка замков повторов по умолчанию — `retry_failed.DEFAULT_STATE_DIR`.
RETRY_DEFAULT_STATE_DIR = "/srv/meeting-notary/state"
#: Явный список папок (через `:`) — перебивает всё остальное.
DIRS_ENV = "MEETING_NOTARY_FINALIZE_LOCK_DIRS"
STATE_DIR_ENV = "MEETING_NOTARY_STATE_DIR"

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def lock_filename(session_uid: str) -> str:
    """Имя файла замка, как его строит сборщик (единый источник)."""
    safe = _UNSAFE_RE.sub("_", session_uid or "unknown")
    return f"finalize-{safe}.lock"


def _raw_lock_filename(session_uid: str) -> Optional[str]:
    """Имя, как его строит `retry_failed` (без чистки). None — если небезопасно."""
    sid = str(session_uid or "")
    if not sid or "/" in sid or "\\" in sid or sid.startswith(".") or "\x00" in sid:
        return None
    return f"finalize-{sid}.lock"


def lock_dirs() -> List[Path]:
    """Папки, где замок этой встречи может искать другой финализатор.

    Порядок: явный список `MEETING_NOTARY_FINALIZE_LOCK_DIRS` (тогда только он) →
    `MEETING_NOTARY_STATE_DIR` → дефолт сборщика → папка повторов (только если она
    существует: на маке и в пробах `/srv` нет, создавать её там нельзя).
    """
    explicit = (os.environ.get(DIRS_ENV) or "").strip()
    if explicit:
        raw = [p for p in explicit.split(os.pathsep) if p.strip()]
    else:
        raw = []
        env_dir = (os.environ.get(STATE_DIR_ENV) or "").strip()
        if env_dir:
            raw.append(env_dir)
        raw.append(COLLECTOR_DEFAULT_STATE_DIR)
        if Path(RETRY_DEFAULT_STATE_DIR).is_dir():
            raw.append(RETRY_DEFAULT_STATE_DIR)
    out: List[Path] = []
    for item in raw:
        p = Path(os.path.expanduser(item.strip()))
        if p not in out:
            out.append(p)
    return out


def lock_paths(session_uid: str, *, dirs: Optional[List[Path]] = None) -> List[Path]:
    """Все файлы-замки встречи: каждая папка × оба варианта имени, без повторов."""
    names = [lock_filename(session_uid)]
    raw = _raw_lock_filename(session_uid)
    if raw and raw not in names:
        names.append(raw)
    out: List[Path] = []
    for d in (dirs if dirs is not None else lock_dirs()):
        for name in names:
            p = Path(d) / name
            if p not in out:
                out.append(p)
    return out


@contextlib.contextmanager
def hold(session_uid: str, *, dirs: Optional[List[Path]] = None) -> Iterator[bool]:
    """Взять ВСЕ замки встречи неблокирующе. yield True — взяты все; False — хоть
    один занят (тогда отпускаем уже взятые и ничего не делаем).

    Папку, которую создать нельзя (права, нет родителя), пропускаем с
    предупреждением: там замок никто и не возьмёт. Но если не взялся НИ ОДИН
    замок — это False: без замка ручная финализация столкнулась бы со сборщиком.
    """
    fds: List[int] = []
    acquired = True
    try:
        for path in lock_paths(session_uid, dirs=dirs):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644)
            except OSError as e:
                logger.warning(
                    "[finalize-lock] папка замка недоступна (%s) — пропускаю: %s",
                    type(e).__name__, path.parent,
                )
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                os.close(fd)
                acquired = False
                logger.info("[finalize-lock] занято: %s", path.name)
                break
            fds.append(fd)
        if acquired and not fds:
            logger.error("[finalize-lock] ни одного замка взять не удалось — финализацию не начинаю")
            acquired = False
        yield acquired
    finally:
        for fd in fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
