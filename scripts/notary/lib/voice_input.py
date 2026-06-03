"""Приём голосового ответа на clarify-вопрос (Ф4 REQ 4.1).

Telegram voice → `getFile` → скачать `.oga` → транскрибация → текст, который
listener подставляет как `msg["text"]` и гонит через обычный dispatch (тот же
путь, что у текстового ответа — reply-матчинг, clarify, apply_reply).

Транскрибация переиспользует daemon-стек (как `~/.local/bin/transcribe-smart`):
  1) обёртка `transcribe-smart`, если она на PATH (Groq Whisper Large v3 Turbo +
     локальный mlx-whisper фолбэк) — путь для мака/локального smoke;
  2) прямой вызов Groq REST API ключом из env — путь для VPS, где обёртки нет,
     но Groq доступен по HTTPS;
  3) ничего не вышло → None → listener отвечает «понимаю только текст/кнопки».

Ключ Groq: env `GROQ_API_KEY`, иначе файл `GROQ_API_KEY_FILE`
(дефолт `~/.config/groq/api-key`, как у обёртки). Ключ и токен НЕ логируем.

Дисциплина «Опасной тройки»: текст транскрипта НЕ логируем (только длину).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import telegram_api


logger = logging.getLogger(__name__)


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_MAX_BYTES = 25 * 1024 * 1024  # лимит Groq на размер файла
_TRANSCRIBE_SMART = "transcribe-smart"


def is_voice_message(msg: dict) -> bool:
    return isinstance(msg, dict) and isinstance(msg.get("voice"), dict)


def _groq_api_key() -> Optional[str]:
    """Ключ Groq: env `GROQ_API_KEY` → файл `GROQ_API_KEY_FILE`/дефолт."""
    key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if key:
        return key
    key_file = (os.environ.get("GROQ_API_KEY_FILE") or "").strip() or os.path.expanduser(
        "~/.config/groq/api-key"
    )
    try:
        k = Path(key_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return k or None


def _transcribe_via_smart(audio_path: str) -> Optional[str]:
    """Обёртка `transcribe-smart` (daemon-стек). None если её нет на PATH/упала.

    stdout: текст + последняя строка-метка `[Groq]` / `[локально: …]` /
    `[ошибка: …]`. Метку отбрасываем; `[ошибка…]` → считаем неуспехом.
    """
    exe = shutil.which(_TRANSCRIBE_SMART)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, audio_path], capture_output=True, text=True, timeout=120
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("[voice] transcribe-smart недоступен: %s", type(e).__name__)
        return None
    if proc.returncode != 0:
        logger.warning("[voice] transcribe-smart rc=%d", proc.returncode)
        return None
    lines = (proc.stdout or "").splitlines()
    if lines and lines[-1].strip().startswith("[") and lines[-1].strip().endswith("]"):
        label = lines[-1].strip()
        if label.startswith("[ошибка"):
            logger.warning("[voice] transcribe-smart label=error")
            return None
        body = "\n".join(lines[:-1]).strip()
    else:
        body = (proc.stdout or "").strip()
    return body or None


def _transcribe_via_groq(audio_path: str) -> Optional[str]:
    """Прямой Groq REST (фолбэк для VPS). None если ключа нет / лимит / HTTP-fail."""
    key = _groq_api_key()
    if not key:
        logger.warning("[voice] Groq-ключ не найден — прямой фолбэк недоступен")
        return None
    try:
        size = os.path.getsize(audio_path)
    except OSError:
        return None
    if size > GROQ_MAX_BYTES:
        logger.warning("[voice] аудио %d байт > Groq-лимит 25МБ", size)
        return None
    try:
        import requests  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.warning("[voice] requests недоступен — прямой Groq невозможен")
        return None
    # `.oga` Groq не принимает по расширению — отдаём как `.ogg` (тот же контейнер).
    fname = os.path.basename(audio_path)
    if fname.lower().endswith(".oga"):
        fname = fname[:-4] + ".ogg"
    try:
        with open(audio_path, "rb") as fh:
            resp = requests.post(
                GROQ_ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (fname, fh, "audio/ogg")},
                data={"model": GROQ_MODEL, "response_format": "text", "language": "ru"},
                timeout=60,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[voice] Groq-запрос упал: %s", type(e).__name__)
        return None
    if resp.status_code != 200:
        logger.warning("[voice] Groq HTTP %s", resp.status_code)
        return None
    text = (resp.text or "").strip()
    return text or None


def transcribe_audio(audio_path: str) -> Optional[str]:
    """Daemon-стек: `transcribe-smart` → прямой Groq → None."""
    text = _transcribe_via_smart(audio_path)
    if text:
        return text
    return _transcribe_via_groq(audio_path)


def voice_to_text(token: str, msg: dict, *, work_dir: Optional[str] = None) -> Optional[str]:
    """Голосовое сообщение → распознанный текст. None если скачать/расшифровать
    не вышло (caller ответит фолбэк-сообщением «понимаю только текст/кнопки»)."""
    voice = msg.get("voice") if isinstance(msg, dict) else None
    if not isinstance(voice, dict):
        return None
    file_id = voice.get("file_id")
    if not file_id:
        return None
    try:
        file_path = telegram_api.get_file_path(token, file_id)
    except telegram_api.TelegramApiError as e:
        logger.warning("[voice] getFile failed: %s", e)
        return None
    if not file_path:
        return None

    suffix = os.path.splitext(file_path)[1] or ".oga"
    tmp_dir = work_dir or tempfile.gettempdir()
    fd, tmp_path = tempfile.mkstemp(prefix="clarify-voice-", suffix=suffix, dir=tmp_dir)
    os.close(fd)
    try:
        try:
            telegram_api.download_file(token, file_path, tmp_path)
        except telegram_api.TelegramApiError as e:
            logger.warning("[voice] download failed: %s", e)
            return None
        text = transcribe_audio(tmp_path)
        logger.info("[voice] transcribe ok=%s chars=%d", bool(text), len(text or ""))
        return text
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
