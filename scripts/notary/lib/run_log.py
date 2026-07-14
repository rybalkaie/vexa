# -*- coding: utf-8 -*-
"""run_log.py — per-meeting durable-лог прогона финализации (ISS-31, Ф1).

Зачем: причина сбоя/деградации генерации протокола ДО этой фазы не персистилась
нигде — жила только в stderr finalize-подпроцесса, который collector выбрасывал
при rc=0, и в `docker logs` контейнера, умирающих при `docker rm`. Пишем причину
в durable-каталог логов VPS `/srv/meeting-notary/logs/runs/<sid>.log` — рядом с
`logs/bot-containers` (`collector.py:81`), НЕ рядом с недолговечным `_tmp`
(корень ISS-32). Файл переживает удаление контейнера и чистку рабочей папки.

Опасная тройка (правило проекта `CLAUDE.md`): в этот лог уходят ТОЛЬКО метаданные
и счётчики (класс ошибки, ветка деградации, длина промпта, число сегментов
транскрипта, модель, stop_reason когда доступен). НИКОГДА — текст реплик,
транскрипта или протокола. Helper на всякий случай режет строковые значения по
длине и вырезает переводы строк, но несущая дисциплина — на стороне вызова:
caller передаёт только счётчики/литералы.

Всё best-effort: наблюдаемость не имеет права уронить генерацию протокола —
любая ошибка записи глотается (лог-долг, не сбой встречи).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Долговечная конвенция логов VPS: `/srv/meeting-notary/logs/<...>`. env
# `MEETING_NOTARY_RUN_LOG_DIR` переопределяет (тесты подменяют на tmp, на маке
# `/srv` не писабелен — там лог просто отсутствует, это ок: артефакт VPS-стороны).
_RUN_LOG_DIR_DEFAULT = "/srv/meeting-notary/logs/runs"

# Ретенция: тот же порог, что ручная чистка мак-логов в README
# (`find ~/Library/Logs/meeting-notary/runs/ -mtime +30 -delete`). Прунится
# лениво — раз на процесс finalize (ниже), чтобы лог не копился вечно даже без
# внешнего cleanup-таймера. env `MEETING_NOTARY_RUN_LOG_RETENTION_DAYS`.
_RETENTION_DAYS_DEFAULT = 30

# Санитизация sid в имя файла (тот же класс символов, что `_SAFE_CONTAINER_RE`
# в collector) — sid приходит из имени meta-файла, но подстраховываемся от `/`.
_SAFE_SID_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Кап на строковое значение поля — вторая линия защиты опасной тройки: даже если
# caller однажды передаст длинную строку, в лог уйдёт максимум это число символов.
_MAX_FIELD_LEN = 200

# Прунить не чаще раза на процесс (finalize = свежий subprocess на встречу, значит
# фактически «раз на встречу»). Модульный флаг сбрасывается новым процессом.
_pruned_this_process = False

# Цикл5/Ф1 (ход4 РЕГ1): та же причина сбоя записи (обычно системная — нет прав /
# каталог занят) повторяется на КАЖДЫЙ вызов append_event, пока её не починят
# руками. Warning видим (ход3 У1), но не долбим одним и тем же сообщением N раз
# за один процесс — первый раз громко, дальше тем же путём, что ретенция (флаг
# на процесс; в проде процесс = один finalize, повтор means best-of-2 draft A+B).
_warned_this_process = False


def run_log_dir() -> Path:
    """Каталог per-meeting логов. env override → дефолт `/srv/.../logs/runs`."""
    raw = os.environ.get("MEETING_NOTARY_RUN_LOG_DIR")
    if raw:
        return Path(os.path.expanduser(raw))
    return Path(_RUN_LOG_DIR_DEFAULT)


def run_log_path(sid: str) -> Path:
    """Путь к per-meeting логу для sid (без создания)."""
    safe = _SAFE_SID_RE.sub("_", sid or "unknown") or "unknown"
    return run_log_dir() / f"{safe}.log"


def _retention_days() -> int:
    try:
        v = int(os.environ.get("MEETING_NOTARY_RUN_LOG_RETENTION_DAYS", "") or _RETENTION_DAYS_DEFAULT)
    except (TypeError, ValueError):
        return _RETENTION_DAYS_DEFAULT
    return v if v > 0 else _RETENTION_DAYS_DEFAULT


def _coerce_field(value: object) -> str | None:
    """Значение поля → безопасная скалярная строка. None → пропуск (не пишем).

    Опасная тройка: НЕ сериализуем произвольные объекты (мог бы утечь repr текста)
    и режем строки по длине + вырезаем переводы строк (одна запись = одна строка).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        s = value.replace("\n", " ").replace("\r", " ").strip()
        return s[:_MAX_FIELD_LEN]
    # Незнакомый тип — только имя класса, НЕ его содержимое (защита от дампа текста).
    return type(value).__name__


def _prune_old(directory: Path) -> None:
    """Удаляет per-meeting логи старше ретенции. Best-effort, раз на процесс."""
    global _pruned_this_process
    if _pruned_this_process:
        return
    _pruned_this_process = True
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - _retention_days() * 86400
        for p in directory.glob("*.log"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def append_event(sid: str, event: str, **fields: object) -> None:
    """Дописывает одну метадата-строку в per-meeting durable-лог.

    Формат строки: `<ISO-UTC> event=<event> key=value ...`. Пишем ТОЛЬКО
    счётчики/метаданные (см. модуль-docstring, опасная тройка). Best-effort:
    любая ошибка ФС/прав глотается — наблюдаемость не роняет финализацию.

    Args:
      sid: session_uid встречи (идёт в имя файла).
      event: короткий класс события (`ok` / `failed` / `degraded`).
      **fields: метаданные (reason, model, prompt_len, segments, output_len,
        stop_reason, …). Значения — только скаляры; caller НЕ передаёт текст.
    """
    try:
        directory = run_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _prune_old(directory)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts = [ts, f"event={_coerce_field(event) or '?'}"]
        for key, value in fields.items():
            coerced = _coerce_field(value)
            if coerced is None:
                continue
            safe_key = _SAFE_SID_RE.sub("_", str(key))
            parts.append(f"{safe_key}={coerced}")
        line = " ".join(parts) + "\n"
        path = directory / (f"{_SAFE_SID_RE.sub('_', sid or 'unknown') or 'unknown'}.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as e:
        # Цикл5/Ф1 (ход3 У1): `finalize-meeting.py:setup_logging()` дефолтит на
        # INFO (verbose=False) — `logger.debug(...)` физически НЕ долетал бы до
        # stderr/collector.log, и единственный deliverable этой фазы (durable
        # лог причин сбоя) мог бы тихо не существовать НАВСЕГДА, если каталог на
        # VPS вдруг не writable (ошибка прав / путь занят файлом), а мы бы этого
        # не узнали никогда. warning — виден и в проде, и (после ход1 Н1) корректно
        # долетает до `collector.log` через `_finalize_stderr_warnings`, не заваливая
        # его шумом на успехе. Best-effort остаётся: append не бросает исключение.
        #
        # Ход4 РЕГ1: причина обычно системная (нет прав/каталог занят) и повторяется
        # на каждый вызов, пока её не починят руками — первый раз громко (warning),
        # дальше в этом же процессе тише (debug), чтобы не заспамить один и тот же
        # диагноз (напр. best-of-2 зовёт generate_protocol дважды за один finalize).
        global _warned_this_process
        if _warned_this_process:
            logger.debug("run_log append не удался (sid=%s): %s", sid, e)
        else:
            _warned_this_process = True
            logger.warning("run_log append не удался (sid=%s): %s", sid, e)
