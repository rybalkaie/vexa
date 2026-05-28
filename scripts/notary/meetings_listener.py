"""meetings_listener.py — долгоживущий процесс, ловит ответы Ильи на блок 📅
И clarify-callback'и от Ф3 (на том же боте `@ilya_protocol_meeting_bot`).

Запускается systemd-сервисом meeting-notary-listener на VPS. Polling-у через
notarius-бота (token TELEGRAM_NOTARIUS_BOT_TOKEN), принимает в авторизованном
DM-чате (TELEGRAM_NOTARIUS_CHAT_ID):
  - `callback_query` (inline-кнопки от Ф3 clarify) → `lib.clarify_worker.process_callback`;
  - `message` Reply на сообщение, начинающееся с 📅 — старый apply_reply flow;
  - `message` без Reply — если есть pending clarify state, пробуем как clarify;
    иначе — apply_reply.
Reply-привязка остаётся опциональной для будущей мультиплексности нескольких
блоков, но не обязательна (Илья диктует голосом и Reply делает редко).

Защиты:
  - Long-poll timeout 25s (≥ серверного default 30s — TG разрывает реже).
  - Heartbeat: каждый тик пишем int(time.time()) в STATE_DIR/listener-alive.
    Watchdog-таймер (meeting-notary-listener-watchdog) проверяет mtime и
    рестартует процесс, если разрыв > 3 минут.
  - При сбое API — пауза 10s и retry. Чтобы systemd Restart=on-failure не
    стрелял на временных HTTP-500.
  - offset держим в STATE_DIR/listener-offset, чтобы переживать рестарт.
  - Reply на чужой бот игнорируем (chat_id ≠ TELEGRAM_NOTARIUS_CHAT_ID).
  - Heartbeat пишется и при пустом long-poll (timeout), иначе watchdog
    ложно сработает при тишине.
  - sweep_timeouts() для clarify state'ов раз в SWEEP_EVERY_S сек.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

STATE_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_STATE_DIR") or "~/.local/state"
))
LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR", "~/Library/Logs/meeting-notary")
))
LOG_FILE = LOG_DIR / "listener.log"
HEARTBEAT_FILE = STATE_DIR / "listener-alive"
OFFSET_FILE = STATE_DIR / "listener-offset"

POLL_TIMEOUT_S = 25
ERROR_BACKOFF_S = 10
HEARTBEAT_EVERY_S = 30
SWEEP_EVERY_S = 30  # частота проверки таймаутов clarify
TRIGGER_PREFIX = "\U0001F4C5"  # 📅 — вечерний блок (apply_reply flow)


def _clarify_prefix() -> str:
    """Префикс 🎙 берётся из `llm_postprocess.CLARIFY_MSG_PREFIX` — один источник истины.

    Fallback на жёстко вшитый литерал, если модуль не импортируется (mac без pyannote
    в редких сетапах — TYPE_CHECKING закрывает это, но on guard).
    """
    try:
        from notary.lib.llm_postprocess import CLARIFY_MSG_PREFIX  # noqa: PLC0415
        return CLARIFY_MSG_PREFIX
    except Exception:
        return "\U0001F399"

ENV_FILE = os.environ.get("MEETING_NOTARY_ENV_FILE", "/srv/meeting-notary/.env.notary")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger("meetings-listener")


def load_env_file(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        logger.warning("env-файл %s не найден — пробую os.environ", path)
    return env


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.write_text(f"{int(time.time())}\n")
    except OSError as e:
        logger.warning("Не смог обновить heartbeat: %s", e)


def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_offset(offset: int) -> None:
    try:
        tmp = OFFSET_FILE.with_suffix(".tmp")
        tmp.write_text(f"{offset}\n")
        os.replace(tmp, OFFSET_FILE)
    except OSError as e:
        logger.warning("Не смог сохранить offset: %s", e)


def telegram_request(token: str, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send_message(token: str, chat_id: int, text: str, *, reply_to: int | None = None, html: bool = False) -> None:
    params: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    if html:
        params["parse_mode"] = "HTML"
    try:
        resp = telegram_request(token, "sendMessage", params, timeout=15)
        if not resp.get("ok"):
            logger.warning("sendMessage: %s", resp.get("description", "?"))
    except Exception as e:  # noqa: BLE001
        logger.warning("sendMessage упал: %s", e)


def get_updates(token: str, offset: int) -> list[dict[str, Any]]:
    params = {
        "offset": offset,
        "timeout": POLL_TIMEOUT_S,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    try:
        resp = telegram_request(token, "getUpdates", params, timeout=POLL_TIMEOUT_S + 10)
    except urllib.error.URLError as e:
        logger.warning("getUpdates URLError: %s", e)
        return []
    except TimeoutError as e:
        logger.warning("getUpdates timeout: %s", e)
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning("getUpdates exception: %s", e)
        return []
    if not resp.get("ok"):
        logger.warning("getUpdates: %s", resp.get("description", "?"))
        return []
    return resp.get("result", []) or []


def _clarify_pending_root() -> Path | None:
    """Резолвит pending_root для clarify (или None если модуль не импортируется)."""
    try:
        from notary.lib import clarify_state  # noqa: PLC0415
        return clarify_state.resolve_pending_dir()
    except Exception as e:  # noqa: BLE001
        logger.debug("clarify_state import failed (clarify disabled?): %s", e)
        return None


def maybe_route_to_clarify_text(token: str, msg: dict[str, Any]) -> bool:
    """Если есть pending/timed_out clarify-state — передаёт msg в clarify_worker.

    Возвращает True если clarify взял на себя (caller должен выйти и не звать
    apply_reply). False если pending'ов нет — caller продолжает свой flow.
    """
    pending_root = _clarify_pending_root()
    if pending_root is None or not pending_root.exists():
        return False
    try:
        from notary.lib import clarify_worker  # noqa: PLC0415
        if not clarify_worker.has_any_pending_clarify(pending_root):
            return False
        clarify_worker.process_text_message(msg, pending_root, token)
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("clarify text routing failed: %s", e)
        return False


def process_callback_query(token: str, allowed_chat: int, cbq: dict[str, Any]) -> None:
    """Inline-кнопка от Ф3 clarify. Если есть pending — применяем."""
    # Sender check (chat_id для callback внутри сообщения).
    msg = cbq.get("message") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid is not None and cid != allowed_chat:
        logger.info("skip callback: chat_id=%s ≠ allowed=%s", cid, allowed_chat)
        return
    pending_root = _clarify_pending_root()
    if pending_root is None:
        logger.warning("callback received but clarify lib unavailable")
        return
    try:
        from notary.lib import clarify_worker  # noqa: PLC0415
        clarify_worker.process_callback(cbq, pending_root, token)
    except Exception as e:  # noqa: BLE001
        logger.exception("process_callback failed: %s", e)


def sweep_clarify_timeouts() -> None:
    pending_root = _clarify_pending_root()
    if pending_root is None or not pending_root.exists():
        return
    try:
        from notary.lib import clarify_worker  # noqa: PLC0415
        n = clarify_worker.sweep_timeouts(pending_root)
        if n:
            logger.info("clarify sweep: marked %d as timed_out", n)
    except Exception as e:  # noqa: BLE001
        logger.exception("clarify sweep failed: %s", e)


def process_message(token: str, allowed_chat: int, msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid != allowed_chat:
        logger.info("skip: chat_id=%s ≠ allowed=%s", cid, allowed_chat)
        return

    # Reply-привязка опциональна. Reply на сообщение бота:
    #   • начинается с 📅 → блок встреч → apply_reply flow (ниже).
    #   • начинается с 🎙 → clarify-уведомление Ф3 → роутинг в clarify_worker.
    #   • что-то ещё → игнор (Илья ответил на чужую служебку бота).
    # Если Reply нет — Илья просто написал в DM. Если есть pending clarify —
    # передаём ему; иначе старый apply_reply flow к последнему snapshot.
    reply_to = msg.get("reply_to_message")
    if reply_to:
        orig_text = (reply_to.get("text") or "").strip()
        if orig_text.startswith(_clarify_prefix()):
            if maybe_route_to_clarify_text(token, msg):
                return
            logger.info(
                "skip clarify Reply: no pending state (already resolved/expired)"
            )
            return
        if not orig_text.startswith(TRIGGER_PREFIX):
            return
    else:
        if maybe_route_to_clarify_text(token, msg):
            return

    reply_text = (msg.get("text") or "").strip()
    if not reply_text:
        send_message(token, cid, "Пустой reply — нечего применять. Ответь по номерам, например: «1 да в @t11, 2 нет».", reply_to=msg.get("message_id"))
        return

    reply_date = msg.get("date")  # unix-timestamp
    snapshot = STATE_DIR / "meetings-shown.last.json"
    if not snapshot.exists():
        send_message(token, cid, "Не нашёл snapshot предложений (meetings-shown.last.json). Попроси админа перезапустить evening-digest.", reply_to=msg.get("message_id"))
        logger.error("snapshot %s отсутствует", snapshot)
        return

    cmd = [
        sys.executable, "-m", "notary.meetings_apply_reply",
        "--reply", reply_text,
        "--snapshot", str(snapshot),
    ]
    if reply_date:
        cmd += ["--reply-date", str(reply_date)]

    logger.info("apply-reply: reply_len=%d reply_date=%s", len(reply_text), reply_date)
    # Heartbeat ПЕРЕД блокирующим subprocess — apply-reply (claude --print внутри)
    # может занять до 90s. Watchdog-порог 240s (с запасом). Без свежего heartbeat
    # watchdog ложно сорвался бы рестартом посередине apply-reply, оставив flock
    # на watched.yaml до stale-timeout fs.
    heartbeat()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        logger.error("meetings_apply_reply timeout 180s")
        send_message(token, cid, "Ответ не применился — таймаут apply-reply (180s).", reply_to=msg.get("message_id"))
        return

    out = proc.stdout.strip()
    err = proc.stderr.strip()
    logger.info("apply-reply rc=%d stdout=%s stderr=%s", proc.returncode, out[:600], err[:300])

    try:
        result = json.loads(out) if out else {}
    except json.JSONDecodeError:
        result = {"error": "невалидный JSON от apply-reply"}

    summary_lines = result.get("summary_lines") or []
    pending = result.get("pending_room") or []
    skipped = result.get("skipped") or []
    err_msg = result.get("error")
    no_decisions = result.get("no_decisions")

    if err_msg:
        send_message(token, cid, f"Не применил: {err_msg}", reply_to=msg.get("message_id"))
        return

    if no_decisions:
        send_message(
            token, cid,
            result.get("hint") or "В ответе не нашёл решений по номерам — переформулируй: «1 да в @t11, 2 нет».",
            reply_to=msg.get("message_id"),
        )
        return

    parts: list[str] = []
    if summary_lines:
        parts.append("Готово:")
        parts += summary_lines
    if pending:
        parts.append("")
        parts.append("Не хватает переговорки:")
        for p in pending:
            parts.append(f"{p.get('n')}. {p.get('title')} ({p.get('when')}) — назови переговорку (например, «{p.get('n')} в @t11»).")
    if skipped:
        parts.append("")
        parts.append("Не применил:")
        parts += [f"• {s}" for s in skipped]

    text = "\n".join(parts) or "Применил без summary."
    send_message(token, cid, text, reply_to=msg.get("message_id"))


def main() -> int:
    setup_logging()
    env = load_env_file(ENV_FILE)
    token = env.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN")
    chat_raw = env.get("TELEGRAM_NOTARIUS_CHAT_ID") or os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
    if not token or not chat_raw:
        logger.error("TELEGRAM_NOTARIUS_BOT_TOKEN / TELEGRAM_NOTARIUS_CHAT_ID не заданы")
        return 2
    try:
        allowed_chat = int(chat_raw)
    except ValueError:
        logger.error("TELEGRAM_NOTARIUS_CHAT_ID не int: %r", chat_raw)
        return 2

    logger.info("listener старт: chat_id=%s long-poll=%ds", allowed_chat, POLL_TIMEOUT_S)
    heartbeat()

    offset = load_offset()
    last_hb = time.time()
    last_sweep = 0.0

    while True:
        # Периодический sweep clarify-таймаутов (дёшево — только файловые ops).
        now_mono = time.monotonic()
        if now_mono - last_sweep >= SWEEP_EVERY_S:
            sweep_clarify_timeouts()
            last_sweep = now_mono

        updates = get_updates(token, offset)
        now = time.time()
        if updates:
            for u in updates:
                offset = max(offset, u.get("update_id", 0) + 1)
                cbq = u.get("callback_query")
                if cbq:
                    try:
                        process_callback_query(token, allowed_chat, cbq)
                    except Exception as e:  # noqa: BLE001
                        logger.exception("process_callback_query упал: %s", e)
                    continue
                msg = u.get("message")
                if msg:
                    try:
                        process_message(token, allowed_chat, msg)
                    except Exception as e:  # noqa: BLE001
                        logger.exception("process_message упал: %s", e)
            save_offset(offset)
        # heartbeat: либо после каждого тика, либо не реже раз в HEARTBEAT_EVERY_S
        if now - last_hb >= 1 or updates:
            heartbeat()
            last_hb = now
        if not updates:
            # long-poll вернул пустой результат (timeout) — короткая передышка, чтобы
            # не упасть в API rate-limit на сетевых ошибках. Heartbeat обеспечен выше.
            time.sleep(1)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
