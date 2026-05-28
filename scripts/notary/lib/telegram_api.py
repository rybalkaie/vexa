"""Прямой Telegram Bot API через httpx.

Минимум, нужный Ф3 (clarify-flow) и Ф6 (доставка в группу + correction flow):
  - send_message(...) с поддержкой `reply_markup` (InlineKeyboardMarkup).
  - edit_message_text(...) — для обновления плашки после ответа Ильи.
  - get_updates(offset, allowed_updates) — long-poll для worker'а.
  - answer_callback_query(...) — снять «крутилку» у нажатой кнопки.
  - delete_message(...) — для correction flow Ф6 (в 48-часовом окне).

Намеренно НЕ используем aiogram / python-telegram-bot — план зафиксировал
«50–80 строк голого httpx без framework». Здесь sync httpx — long-poll worker'у
сложности async не нужны, а вызовы из finalize-meeting.py — одиночные.

Дисциплина «Опасной тройки»:
  - Передаваемые тексты НЕ логируем (только len и `chat_id`).
  - Токен бота НЕ логируется ни при каких условиях.
"""

from __future__ import annotations

import logging
from typing import Any, Optional


logger = logging.getLogger(__name__)


class TelegramApiError(RuntimeError):
    """Ненулевой `ok` от Bot API, network-fail, или невалидный токен."""


_API_TIMEOUT_SEC = 30.0


def _bot_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _load_httpx():
    """Ленивый импорт httpx — чтобы smoke парсеров проходил без сетевой обвязки.

    Прод-окружение (VPS / мак с полным venv) всегда имеет httpx
    (`requirements.txt` пиннует `httpx==0.28.1` для speechmatics_client.py).
    """
    import httpx  # noqa: PLC0415  ленивый импорт намеренно
    return httpx


def _post(token: str, method: str, payload: dict[str, Any], *, timeout: float = _API_TIMEOUT_SEC) -> dict:
    """POST + проверка `ok`. Возвращает `result`-секцию."""
    httpx = _load_httpx()
    url = _bot_url(token, method)
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        raise TelegramApiError(f"{method} network error: {type(e).__name__}: {e}") from e
    try:
        data = resp.json()
    except ValueError as e:
        raise TelegramApiError(f"{method} returned non-JSON: HTTP {resp.status_code}") from e
    if not isinstance(data, dict) or not data.get("ok"):
        desc = (data or {}).get("description", "")
        raise TelegramApiError(f"{method} ok=false: HTTP={resp.status_code} desc={desc!r}")
    return data.get("result") or {}


def send_message(
    token: str,
    chat_id: int,
    text: str,
    *,
    reply_markup: Optional[dict] = None,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
    reply_to_message_id: Optional[int] = None,
) -> dict:
    """Отправляет сообщение. Возвращает `result` (включая `message_id`).

    Логи: только chat_id + len(text). Содержимое — нет.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    result = _post(token, "sendMessage", payload)
    logger.info(
        "[tg-api] sendMessage ok chat=%s msg_id=%s len=%d kb=%s",
        chat_id, result.get("message_id"), len(text), reply_markup is not None,
    )
    return result


def edit_message_text(
    token: str,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: Optional[dict] = None,
    parse_mode: Optional[str] = None,
) -> dict:
    """Перезаписывает сообщение бота (без `reply_markup` — кнопки исчезнут)."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = _post(token, "editMessageText", payload)
    logger.info("[tg-api] editMessageText ok chat=%s msg_id=%s len=%d", chat_id, message_id, len(text))
    return result


def answer_callback_query(
    token: str,
    callback_query_id: str,
    *,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> None:
    """Снимает «крутилку» у кнопки. Telegram требует это в течение 30 сек."""
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text[:200]
    if show_alert:
        payload["show_alert"] = True
    _post(token, "answerCallbackQuery", payload)


def delete_message(token: str, chat_id: int, message_id: int) -> bool:
    """Удаляет сообщение бота. Telegram: только в 48-часовом окне. False = не-fatal fail."""
    try:
        _post(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except TelegramApiError as e:
        logger.info("[tg-api] deleteMessage failed chat=%s msg_id=%s: %s", chat_id, message_id, e)
        return False
    logger.info("[tg-api] deleteMessage ok chat=%s msg_id=%s", chat_id, message_id)
    return True


def get_updates(
    token: str,
    *,
    offset: int = 0,
    timeout: int = 25,
    allowed_updates: Optional[list[str]] = None,
) -> list[dict]:
    """Long-poll `getUpdates`. ОДИН процесс на токен (Telegram ограничение).

    Параметры:
      offset — `update_id + 1` последнего обработанного.
      timeout — long-poll окно, рекомендация Telegram ≤ 50 сек.
      allowed_updates — какие типы апдейтов хотим
        (`["message", "callback_query"]` для Ф3).

    Возвращает: список raw-update'ов (dict'ов). Пустой при таймауте без событий.
    """
    payload: dict[str, Any] = {"offset": offset, "timeout": timeout}
    if allowed_updates is not None:
        payload["allowed_updates"] = allowed_updates
    # HTTP timeout = long-poll timeout + запас на сетевую дорогу.
    result = _post(token, "getUpdates", payload, timeout=float(timeout) + 10.0)
    # getUpdates возвращает массив, _post распакует его в result (если он список).
    if isinstance(result, list):
        return result
    # На случай, если Bot API однажды вернёт dict вместо list — не падаем.
    return []


def build_inline_keyboard(rows: list[list[dict]]) -> dict:
    """Конструктор `reply_markup` для `InlineKeyboardMarkup`.

    Каждая ячейка — dict вида `{"text": "...", "callback_data": "..."}`.
    `callback_data` — ASCII, максимум 64 байта (ограничение Telegram).

    Пример:
        build_inline_keyboard([
            [{"text": "Илья",   "callback_data": "clarify:sid:SPEAKER_00:Илья"}],
            [{"text": "Дарья",  "callback_data": "clarify:sid:SPEAKER_00:Дарья"}],
            [{"text": "Другое (текстом)", "callback_data": "clarify:sid:SPEAKER_00:__other__"}],
        ])
    """
    return {"inline_keyboard": rows}
