"""Pure-Python запись в накопитель стейкхолдера (трек) — без shell-обёртки.

Ф6 закрывает архитектурный долг Ф5: `_append_to_stakeholder_track` в
`llm_postprocess.py` зовёт `~/.local/bin/stakeholder-track.sh` через subprocess,
но shell-скрипт живёт только на маке. На VPS финализация 1:1 встречи
получала `errors=["track-append-failed:..."]` и трек не обновлялся.

Тут — единая Python-реализация (`fcntl.flock` + `tempfile + os.rename`),
работает одинаково на маке и на VPS. Whitelist берётся из реестра
стейкхолдеров (`stakeholders.load_stakeholders()`) — одна копия allowed-paths
в Python вместо синхронных копий в shell+helper-py.

Логика парсинга markdown идентична `~/.local/bin/stakeholder-track-helper.py`
(ветка `append`): найти `## 🟢 Открыто*` → подсекцию `### <SECTION_TITLE>` →
дописать; нет подсекции → создать в конце «Открыто», перед `## ✅ Закрытые`.

Shell-скрипт `~/.local/bin/stakeholder-track.sh` ОСТАЁТСЯ на маке — он
обслуживает CLI close/restore из me-dashboard. Ф6 его не трогает.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from . import stakeholders as _stk


logger = logging.getLogger(__name__)


_STK_OPEN_RE = re.compile(r"^##\s+🟢\s+Открыто\b")
_STK_CLOSED_RE = re.compile(r"^##\s+✅\s+Закрыт")  # «## ✅ Закрытые»
_STK_H3_RE = re.compile(r"^###\s+(.+)$")

# Префикс буллета: `- [ ] ` / `- [x] ` (чекбокс) или просто `- `.
_CHECKBOX_PREFIX_RE = re.compile(r"^-\s+\[[ xX]\]\s+")
_PLAIN_BULLET_PREFIX_RE = re.compile(r"^-\s+")


def _strip_bullet_prefix(text: str) -> str:
    """Снимает `- [ ] ` / `- [x] ` / `- ` префикс буллета → контент пункта.

    Используется и для сравнения (exact-match при закрытии), и для извлечения
    «тела» пункта при переносе в «Закрытые». Если строка не буллет — возвращаем
    как есть (после strip)."""
    s = text.strip()
    m = _CHECKBOX_PREFIX_RE.match(s)
    if m:
        return s[m.end():].strip()
    m = _PLAIN_BULLET_PREFIX_RE.match(s)
    if m:
        return s[m.end():].strip()
    return s


def _load_whitelist(*, me_dir: Optional[str] = None) -> set[Path]:
    """Allowed-paths из реестра стейкхолдеров. Одна копия логики на Python.

    Каждый трек резолвится в абсолютный Path через `stakeholder_abs_track_path`.
    На VPS базовый `~/Projects/me/` — это mirror; на маке — оригинал.
    """
    items = _stk.load_stakeholders()
    out: set[Path] = set()
    for s in items:
        p = _stk.stakeholder_abs_track_path(s, me_dir=me_dir)
        if p is None:
            continue
        try:
            out.add(p.resolve())
        except OSError:
            out.add(p)
    return out


def _is_in_whitelist(target: Path, whitelist: set[Path]) -> bool:
    """target должен совпадать с одним из разрешённых файлов (после resolve)."""
    try:
        rt = target.resolve()
    except OSError:
        rt = target
    return rt in whitelist


def _acquire_lock(file_path: Path) -> Optional[int]:
    """Exclusive flock на отдельном `.lock`-файле рядом с треком.

    Возвращает fd (для release) или None если не смогли создать lock-файл.
    """
    lock_path = file_path.parent / f".{file_path.name}.lock"
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        logger.warning("[track] lock open failed %s: %s", lock_path, e)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        logger.warning("[track] flock failed %s: %s", lock_path, e)
        os.close(fd)
        return None
    return fd


def _release_lock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _atomic_write(path: Path, content: str) -> None:
    """tempfile в той же директории + fsync + os.rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _find_open_block(lines: list[str]) -> tuple[Optional[int], Optional[int]]:
    """Возвращает (open_h2_idx, next_h2_idx). next_h2_idx может быть len(lines)."""
    open_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and _STK_OPEN_RE.match(line):
            open_idx = i
            break
    if open_idx is None:
        return (None, None)
    next_idx = len(lines)
    for j in range(open_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            next_idx = j
            break
    return (open_idx, next_idx)


def _find_subsection(
    lines: list[str],
    open_idx: int,
    next_idx: int,
    section_title: str,
) -> tuple[Optional[int], Optional[int]]:
    """Ищет `### <section_title>` внутри [open_idx+1, next_idx). Возвращает (sub_idx, sub_end)."""
    needle = section_title.strip()
    sub_idx: Optional[int] = None
    for j in range(open_idx + 1, next_idx):
        line = lines[j]
        if not line.startswith("### "):
            continue
        m = _STK_H3_RE.match(line)
        if m and m.group(1).strip() == needle:
            sub_idx = j
            break
    if sub_idx is None:
        return (None, None)
    sub_end = next_idx
    for k in range(sub_idx + 1, next_idx):
        if lines[k].startswith("## ") or lines[k].startswith("### "):
            sub_end = k
            break
    return (sub_idx, sub_end)


def _find_closed_block(lines: list[str]) -> tuple[Optional[int], Optional[int]]:
    """Возвращает (closed_h2_idx, next_h2_idx) для блока «## ✅ Закрытые».
    next_h2_idx может быть len(lines). (None, None) если секции нет."""
    closed_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and _STK_CLOSED_RE.match(line):
            closed_idx = i
            break
    if closed_idx is None:
        return (None, None)
    next_idx = len(lines)
    for j in range(closed_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            next_idx = j
            break
    return (closed_idx, next_idx)


def append_to_open_subsection(
    file_path: Path,
    section_title: str,
    bullet_block: str,
    *,
    me_dir: Optional[str] = None,
    skip_whitelist: bool = False,
) -> bool:
    """Дописывает `bullet_block` в подсекцию `### <section_title>` внутри
    блока `## 🟢 Открыто*`. Если подсекции нет — создаёт её в конце блока
    (перед следующим `## ...` или EOF). Если блока «Открыто» нет — создаёт
    его в конце файла.

    Защиты:
      - Whitelist через реестр стейкхолдеров (одна копия в Python).
      - `fcntl.flock` (exclusive) — защита от параллельного writer'а.
      - Atomic write через `tempfile + fsync + os.rename`.

    Возвращает True на успех. False на: пустой block / файл не существует /
    путь не в whitelist / lock fail / write fail. Логирует причину.

    `skip_whitelist=True` — только для тестов и smoke (пишем в /tmp-копию).
    """
    if not bullet_block or not bullet_block.strip():
        logger.warning("[track] empty bullet_block — skip")
        return False
    if not file_path.is_file():
        logger.warning("[track] target file missing: %s", file_path)
        return False
    if not skip_whitelist:
        whitelist = _load_whitelist(me_dir=me_dir)
        if not whitelist:
            logger.warning("[track] whitelist пустой — реестр стейкхолдеров не загружен")
            return False
        if not _is_in_whitelist(file_path, whitelist):
            logger.warning(
                "[track] %s не в whitelist (whitelist size=%d) — отказ",
                file_path, len(whitelist),
            )
            return False

    fd = _acquire_lock(file_path)
    try:
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("[track] read failed %s: %s", file_path, e)
            return False
        trailing_nl = raw.endswith("\n")
        lines = raw.split("\n")
        if trailing_nl and lines and lines[-1] == "":
            lines = lines[:-1]

        block_lines = bullet_block.rstrip("\n").split("\n")

        open_idx, next_idx = _find_open_block(lines)
        if open_idx is None:
            # Нет «🟢 Открыто» — создаём весь блок в конце файла.
            new_lines = list(lines)
            if new_lines and new_lines[-1].strip() != "":
                new_lines.append("")
            new_lines.append("## 🟢 Открыто — добавлено ботом")
            new_lines.append("")
            new_lines.append("### " + section_title)
            new_lines.append("")
            new_lines.extend(block_lines)
            out = "\n".join(new_lines)
            if trailing_nl:
                out += "\n"
            try:
                _atomic_write(file_path, out)
            except OSError as e:
                logger.warning("[track] write failed %s: %s", file_path, e)
                return False
            return True

        sub_idx, sub_end = _find_subsection(lines, open_idx, next_idx, section_title)
        new_lines = list(lines)
        if sub_idx is not None and sub_end is not None:
            # Подсекция есть — append в её конец, срезая хвостовые пустые строки.
            insert_at = sub_end
            while insert_at > sub_idx + 1 and new_lines[insert_at - 1].strip() == "":
                insert_at -= 1
            inject: list[str] = []
            if insert_at > 0 and new_lines[insert_at - 1].strip() != "":
                inject.append("")
            inject.extend(block_lines)
            new_lines[insert_at:insert_at] = inject
        else:
            # Подсекции нет — создаём в конце блока «Открыто».
            insert_at = next_idx
            while insert_at > open_idx + 1 and new_lines[insert_at - 1].strip() == "":
                insert_at -= 1
            inject = []
            if insert_at > 0 and new_lines[insert_at - 1].strip() != "":
                inject.append("")
            inject.append("### " + section_title)
            inject.append("")
            inject.extend(block_lines)
            inject.append("")
            new_lines[insert_at:insert_at] = inject

        out = "\n".join(new_lines)
        if trailing_nl:
            out += "\n"
        try:
            _atomic_write(file_path, out)
        except OSError as e:
            logger.warning("[track] write failed %s: %s", file_path, e)
            return False
        return True
    finally:
        _release_lock(fd)


def close_open_item(
    file_path: Path,
    item_match: str,
    *,
    closed_date: str,
    note: Optional[str] = None,
    me_dir: Optional[str] = None,
    skip_whitelist: bool = False,
) -> bool:
    """Переносит пункт из «## 🟢 Открыто» в «## ✅ Закрытые» (close/move, Ф3).

    Обратимость (REQ 4.5): пункт НЕ удаляется, а ПЕРЕМЕЩАЕТСЯ с пометкой и
    датой — Илья может вернуть руками.

    `item_match` — дословный текст открытого пункта (с `- [ ]`/`- [x]`/`- `
    префиксом или без — чекбокс снимаем при сравнении). Перенос ТОЛЬКО при
    EXACT-match (РАЗМ3): не нашли точного совпадения в «Открыто» → no-op +
    `warning` (не закрываем «похожий» — страховка от ложного закрытия).

    `closed_date` — дата встречи (`YYYY-MM-DD`). `note` — пометка в скобках;
    дефолт «закрыто ботом по встрече <closed_date>». Итоговая строка:
    `- ✅ Закрыто <date> (<note>): <тело пункта>` (как в существующих треках).

    Идемпотентность: если тело пункта уже присутствует в «✅ Закрытые» —
    no-op (не дублируем). Если секции «✅ Закрытые» нет — создаём в конце файла.

    Защиты те же, что у `append_to_open_subsection`: whitelist из реестра
    стейкхолдеров, `fcntl.flock` (exclusive), atomic write
    (`tempfile + fsync + os.rename`).

    Возвращает True если пункт перенесён; False на no-op (пустой ввод /
    не найден / уже закрыт / путь вне whitelist / lock|write fail).
    Логирует причину.

    `skip_whitelist=True` — только для тестов и smoke (пишем в /tmp-копию).
    """
    if not item_match or not item_match.strip():
        logger.warning("[track] close: пустой item_match — skip")
        return False
    if not closed_date or not str(closed_date).strip():
        logger.warning("[track] close: пустой closed_date — skip")
        return False
    if not file_path.is_file():
        logger.warning("[track] close: target file missing: %s", file_path)
        return False
    if not skip_whitelist:
        whitelist = _load_whitelist(me_dir=me_dir)
        if not whitelist:
            logger.warning("[track] close: whitelist пустой — реестр стейкхолдеров не загружен")
            return False
        if not _is_in_whitelist(file_path, whitelist):
            logger.warning(
                "[track] close: %s не в whitelist (size=%d) — отказ",
                file_path, len(whitelist),
            )
            return False

    closed_date = str(closed_date).strip()
    note_txt = (note or f"закрыто ботом по встрече {closed_date}").strip()
    needle_full = item_match.strip()
    needle_content = _strip_bullet_prefix(item_match)

    fd = _acquire_lock(file_path)
    try:
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("[track] close: read failed %s: %s", file_path, e)
            return False
        trailing_nl = raw.endswith("\n")
        lines = raw.split("\n")
        if trailing_nl and lines and lines[-1] == "":
            lines = lines[:-1]

        open_idx, next_idx = _find_open_block(lines)

        # 1. Точное совпадение открытого пункта (по полной строке ИЛИ по телу
        #    без чекбокса). Первое совпадение выигрывает.
        match_idx: Optional[int] = None
        body: Optional[str] = None
        if open_idx is not None:
            for i in range(open_idx + 1, next_idx):
                ln = lines[i]
                if not ln.lstrip().startswith("-"):
                    continue
                content = _strip_bullet_prefix(ln)
                if needle_full == ln.strip() or needle_content == content:
                    match_idx = i
                    body = content
                    break

        # 2. Идемпотентность: тело уже в «✅ Закрытые»? → no-op (не дублируем).
        check_body = body if body is not None else needle_content
        closed_idx, closed_next = _find_closed_block(lines)
        if closed_idx is not None and check_body:
            for j in range(closed_idx + 1, closed_next):
                if check_body in lines[j]:
                    logger.info(
                        "[track] close: пункт уже в «Закрытые» — no-op: %r",
                        check_body[:60],
                    )
                    return False

        # 3. Точного совпадения в «Открыто» нет → НЕ закрываем похожий (РАЗМ3).
        if match_idx is None:
            logger.warning(
                "[track] close: точный пункт не найден в «🟢 Открыто» — no-op (РАЗМ3): %r",
                needle_full[:80],
            )
            return False

        # 4. Перенос: удаляем строку из «Открыто», добавляем в «Закрытые».
        closed_line = f"- ✅ Закрыто {closed_date} ({note_txt}): {body}"
        new_lines = list(lines)
        del new_lines[match_idx]

        c_idx, _c_next = _find_closed_block(new_lines)
        if c_idx is None:
            # Секции «Закрытые» нет — создаём в конце файла.
            if new_lines and new_lines[-1].strip() != "":
                new_lines.append("")
            new_lines.append("## ✅ Закрытые")
            new_lines.append("")
            new_lines.append(closed_line)
        else:
            # Вставляем первым (newest-first, как в существующих треках):
            # сразу после заголовка, пропустив одну пустую строку-разделитель.
            insert_at = c_idx + 1
            if insert_at < len(new_lines) and new_lines[insert_at].strip() == "":
                insert_at += 1
            new_lines[insert_at:insert_at] = [closed_line]

        out = "\n".join(new_lines)
        if trailing_nl:
            out += "\n"
        try:
            _atomic_write(file_path, out)
        except OSError as e:
            logger.warning("[track] close: write failed %s: %s", file_path, e)
            return False
        logger.info(
            "[track] close: пункт перенесён в «Закрытые» (%s): %r",
            closed_date, (body or "")[:60],
        )
        return True
    finally:
        _release_lock(fd)
