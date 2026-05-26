"""Тонкая обёртка над ~/.local/bin/tg-send для алертов планировщика/runner-а.

Только ошибки. Успешные встречи не уведомляем — спам.

Дедупликация: одинаковый message в одном файле логирования за окно 6 часов
отправится один раз. Используется чтобы scheduler, который запускается
каждые 15 минут, не слал одно и то же сообщение 24 раза подряд.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TG_SEND_BIN = Path(os.path.expanduser("~/.local/bin/tg-send"))
DEDUP_DIR = Path(os.path.expanduser("~/Library/Logs/meeting-notary"))
DEDUP_WINDOW_SEC = 6 * 3600


def _dedupe_key(message: str) -> Path:
    h = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return DEDUP_DIR / f".tg-dedupe-{h}"


def _gc_old_markers(now: float) -> None:
    """Удалить dedupe-маркеры старше 24ч (дольше DEDUP_WINDOW_SEC=6ч).

    Без этого папка обрастает мусором — каждое уникальное сообщение
    оставляет файл-маркер, через год может набежать десятки тысяч.
    """
    cutoff = 24 * 3600
    try:
        for p in DEDUP_DIR.glob(".tg-dedupe-*"):
            try:
                if (now - p.stat().st_mtime) > cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def push(message: str, *, dedupe: bool = True, silent: bool = False) -> bool:
    """Отправить push в Telegram через tg-send. Возвращает True если отправили.

    dedupe=True — не повторять то же сообщение в течение DEDUP_WINDOW_SEC.
    """
    if not TG_SEND_BIN.exists():
        logger.warning("tg-send не найден по пути %s — push пропущен", TG_SEND_BIN)
        return False

    if dedupe:
        DEDUP_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        _gc_old_markers(now)
        marker = _dedupe_key(message)
        if marker.exists():
            age = now - marker.stat().st_mtime
            if age < DEDUP_WINDOW_SEC:
                logger.info("tg-send skipped (deduped, age=%.0fs): %s", age, message[:60])
                return False
        try:
            marker.touch()
        except OSError:
            pass

    cmd = [str(TG_SEND_BIN)]
    if silent:
        cmd.append("--silent")
    cmd.append(message)
    try:
        subprocess.run(cmd, check=True, timeout=15)
        logger.info("tg-send OK: %s", message[:80])
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning("tg-send failed: %s — %s", e, message[:80])
        return False
