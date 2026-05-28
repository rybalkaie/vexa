"""Worker для приёма ответов на clarify-вопросы (Ф3 meeting-notary-llm).

Архитектурное решение (2026-05-28, fix-промт Ф3): clarify-обработчики
встроены в уже работающий `meetings_listener.py` daemon, который 24/7
long-poll'ит `TELEGRAM_NOTARIUS_BOT_TOKEN` (`@ilya_protocol_meeting_bot`).
Telegram отдаёт getUpdates только одному потребителю на токен —
поэтому отдельный процесс на том же боте запустить нельзя.

Этот модуль теперь — **библиотека хендлеров**:
  - `process_callback(callback_query, pending_root, token)` — обработка inline-кнопки;
  - `process_text_message(msg, pending_root, token)` — обработка текстового ответа;
  - `sweep_timeouts(pending_root)` — помечает просроченные state'ы как `timed_out`.

Listener импортирует эти три функции и зовёт их в своём long-poll цикле.

Standalone-режим (`run_forever` / `main`) остался для **локального smoke на маке**
или для случая когда захочется отдельного процесса/бота. В продакшене на VPS
он НЕ используется.

Дисциплина «Опасной тройки»:
  - Логируем metadata (meeting_id, cluster, имя), но НЕ тексты ответа.
  - Токен не пишем в лог.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import clarify_state
from . import telegram_api
from . import llm_postprocess


logger = logging.getLogger(__name__)


# Постоянство offset'а для getUpdates (чтобы рестарт не повторял старые updates).
OFFSET_FILE_NAME = ".clarify_offset"


def _offset_path(pending_root: Path) -> Path:
    return pending_root / OFFSET_FILE_NAME


def load_offset(pending_root: Path) -> int:
    p = _offset_path(pending_root)
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def save_offset(pending_root: Path, offset: int) -> None:
    p = _offset_path(pending_root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(offset), encoding="utf-8")
    except OSError as e:
        logger.warning("[clarify-worker] offset save failed: %s", e)


# ----- Обработка одного pending state'а -------------------------------------

def _find_pending_state_by_callback(
    callback_data: str,
    pending_root: Path,
) -> Optional[dict]:
    """Ищет тот state, у которого hash(meeting_id)[:8] совпадает с префиксом в
    callback_data. Это закрывает race condition между параллельными clarify-flow'ами.
    """
    # Формат: `cl:<mid_short>:<cluster_idx>:<name_idx_or_'o'>`.
    if not callback_data.startswith("cl:"):
        return None
    body = callback_data[3:]
    parts = body.split(":", 1)
    if not parts:
        return None
    mid_short = parts[0]
    if mid_short == "h":  # заголовочная кнопка-разделитель
        return None
    for state in clarify_state.list_pending(root=pending_root):
        if llm_postprocess._short_id(state.get("meeting_id", "")) == mid_short:
            return state
    return None


def _apply_resolution(
    state: dict,
    mapping: dict[str, str],
    *,
    via: str,
    pending_root: Path,
    is_late: bool = False,
) -> None:
    """Применяет mapping к транскрипту на диске, помечает state."""
    if not mapping:
        logger.info(
            "[clarify] %s meeting=%s via=%s nothing-to-apply",
            "late_answer" if is_late else "resolved",
            state.get("meeting_id"), via,
        )
        return

    transcript_path = Path(state.get("transcript_path", ""))
    label_to_name: dict[str, str] = {}
    unclear = state.get("unclear_clusters", {})
    # Н1.Н6 (ход 1): предупреждение если Илья переразмечает имя, которое
    # Ф2 уже угадал для другого cluster'а (current_guess). Не блокируем —
    # это сознательно (см. handoff отступление #5), просто видимо в логе.
    other_guesses = {
        v.get("current_guess"): k
        for k, v in unclear.items()
        if v.get("current_guess")
    }
    for cluster_key, name in mapping.items():
        info = unclear.get(cluster_key, {})
        speaker_label = info.get("speaker_label_in_md")
        if speaker_label:
            label_to_name[speaker_label] = name
        if name in other_guesses and other_guesses[name] != cluster_key:
            logger.warning(
                "[clarify] meeting=%s name=%r переразметка: был %s (current_guess), стал %s",
                state.get("meeting_id"), name, other_guesses[name], cluster_key,
            )

    file_updated = llm_postprocess.apply_clarify_mapping_to_transcript(
        transcript_path, label_to_name,
    )

    # Ф4 re-trigger: после atomic-перезаписи transcript'а перегенерируем
    # `<date>-protokol.md` рядом с ним. Делаем и для resolved (Илья нажал
    # кнопку в окне таймаута), и для late_answer (нажал после таймаута) —
    # план фиксирует, что поздний ответ обновляет файл на диске.
    # В Telegram-группу повторно не шлём (этим займётся / откажется Ф6).
    protocol_regenerated = False
    if file_updated and transcript_path.exists():
        # Имя протокола живёт рядом: `<date>-protokol.md` (тот же паттерн,
        # что в finalize-meeting.py). transcript_path.stem = `<date>`
        # (например `2026-05-27`); если в имени окажется не дата —
        # generate_protocol всё равно возьмёт дату из meta.
        try:
            protocol_path = transcript_path.parent / f"{transcript_path.stem}-protokol.md"
            meta_block = state.get("meta") or {}
            regen_meta = {
                "series": meta_block.get("series") or "",
                "date": meta_block.get("date") or "",
                # sessionUid не критичен для генерации, но передаём для лога.
                "sessionUid": meta_block.get("sessionUid"),
                # expected/participants для шапки берём из name_pool
                # (state хранит итоговый pool, передаваемый Илье в clarify).
                "expectedParticipants": state.get("name_pool", []),
                "participants": [],
                "transcript_filename": transcript_path.name,
            }
            llm_postprocess.regenerate_protocol_for_meeting(
                transcript_path=transcript_path,
                protocol_path=protocol_path,
                meeting_meta=regen_meta,
                meeting_sid=state.get("meeting_id"),
            )
            protocol_regenerated = True
            logger.info(
                "[protocol] regenerated meeting=%s via=%s",
                state.get("meeting_id"),
                "clarify_late" if is_late else "clarify_resolved",
            )
        except llm_postprocess.ProtocolGenerationError as e:
            # Не валим clarify: transcript уже обновлён, протокол просто
            # остался устаревший до следующего ручного `regenerate-protocol.py`.
            logger.warning(
                "[protocol] regen failed (non-fatal) meeting=%s: %s",
                state.get("meeting_id"), e,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "[protocol] regen unexpected error meeting=%s: %s",
                state.get("meeting_id"), e,
            )

    new_status = state.get("status")
    extra = {
        "resolved_via": via,
        "resolved_at": clarify_state.now_iso(),
        "applied_mapping": mapping,
    }
    if not is_late and new_status == "pending":
        extra["status"] = "resolved"
        new_status = "resolved"
    # Если был timed_out — поздний ответ оставляет статус timed_out (но
    # дописывает applied_mapping + resolved_via для аудита).
    clarify_state.mark_status(
        state["meeting_id"],
        new_status or "resolved",
        root=pending_root,
        extra=extra,
    )
    label = "late_answer" if is_late else "resolved"
    logger.info(
        "[clarify] %s meeting=%s via=%s applied=%d file_updated=%s protocol_regen=%s delivered_unchanged=%s",
        label, state["meeting_id"], via, len(mapping), file_updated,
        protocol_regenerated,
        # late_answer не отправляет в группу повторно (см. план «Поздний ответ»).
        "true" if is_late else "n/a",
    )


def _is_authorized_sender(from_user: dict, allowed_chat_id: Optional[int]) -> bool:
    """Ход 5 (security): сверяем `from.id` с `TELEGRAM_CHAT_ID` Ильи.

    Защита от случая, когда кто-то узнал username нового clarify-бота
    и стучится к нему. Если `TELEGRAM_CHAT_ID` не задан — пропускаем
    (legacy / dev-mode), но это сценарий «недо-конфигурирован».
    """
    if allowed_chat_id is None:
        return True
    sender_id = from_user.get("id") if isinstance(from_user, dict) else None
    try:
        return int(sender_id) == int(allowed_chat_id)
    except (TypeError, ValueError):
        return False


def _allowed_chat_id() -> Optional[int]:
    raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def process_callback(
    callback_query: dict,
    pending_root: Path,
    bot_token: str,
) -> None:
    """Один callback_query → попытка применить mapping. Идемпотентно."""
    cb_id = callback_query.get("id") or ""
    data = callback_query.get("data") or ""

    # Ход 5 security: только от авторизованного отправителя.
    from_user = callback_query.get("from") or {}
    if not _is_authorized_sender(from_user, _allowed_chat_id()):
        logger.warning(
            "[clarify-worker] callback from unauthorized user_id=%s — отклоняем",
            from_user.get("id"),
        )
        try:
            telegram_api.answer_callback_query(bot_token, cb_id, text="Not authorized.")
        except telegram_api.TelegramApiError:
            pass
        return
    state = _find_pending_state_by_callback(data, pending_root)

    if state is None:
        # Чужой callback / уже resolved / уже timed_out (state мог быть удалён).
        # Подтверждаем чтобы у Ильи спиннер пропал.
        # Н1.Н4 (ход 1): если data парсится как наш формат (cl:<8hex>:...),
        # но state не найден — логируем как orphaned, чтобы при диагностике
        # отличить «битый state» от «чужой бот стучится».
        if data.startswith("cl:") and len(data) >= 4 and data[3:].split(":", 1)[0] not in ("h", ""):
            logger.warning("[clarify-worker] orphaned callback (state lost?): data=%r", data)
        try:
            telegram_api.answer_callback_query(bot_token, cb_id, text="(уже обработано)")
        except telegram_api.TelegramApiError as e:
            logger.debug("[clarify-worker] answerCb noop: %s", e)
        return

    parsed = llm_postprocess.parse_clarify_callback_data(
        data,
        meeting_id=state["meeting_id"],
        cluster_keys=state.get("cluster_keys_ordered", []),
        name_options_per_cluster={
            k: v.get("name_options", [])
            for k, v in (state.get("unclear_clusters") or {}).items()
        },
    )
    if parsed is None:
        try:
            telegram_api.answer_callback_query(bot_token, cb_id, text="не распознал кнопку")
        except telegram_api.TelegramApiError:
            pass
        return

    cluster_key, name_or_none = parsed
    if name_or_none is None:
        # Кнопка «Другое» — просим текстом.
        try:
            telegram_api.answer_callback_query(
                bot_token, cb_id,
                text="Ок, напиши текстом: «Спикер 3 = Дарья»",
            )
        except telegram_api.TelegramApiError:
            pass
        return

    mapping = {cluster_key: name_or_none}
    is_late = state.get("status") == "timed_out"
    _apply_resolution(state, mapping, via="callback", pending_root=pending_root, is_late=is_late)

    # Снимаем «крутилку» + подменяем текст сообщения, чтобы кнопки исчезли.
    try:
        telegram_api.answer_callback_query(bot_token, cb_id, text=f"✓ {name_or_none}")
    except telegram_api.TelegramApiError:
        pass
    try:
        speaker_label = (
            state.get("unclear_clusters", {}).get(cluster_key, {}).get("speaker_label_in_md")
            or cluster_key
        )
        ack_suffix = (
            "✅ Применил" if not is_late
            else "⏰ Поздно (после таймаута) — обновил файл, в группу повторно не шлю"
        )
        telegram_api.edit_message_text(
            bot_token,
            chat_id=int(state["chat_id"]),
            message_id=int(state["message_id"]),
            text=(
                f"{ack_suffix}: {speaker_label} = {name_or_none}\n"
                f"meeting: {state.get('meeting_id', '')}"
            ),
        )
    except telegram_api.TelegramApiError as e:
        logger.debug("[clarify-worker] edit_message after callback failed: %s", e)


def process_text_message(
    msg: dict,
    pending_root: Path,
    bot_token: str,
) -> None:
    """Текстовое сообщение Ильи. Race protection: применяем только если
    ровно один pending state. Если 2+ — отвечаем «уточни meeting_id»."""
    text = msg.get("text") or ""
    if not text.strip():
        return
    # Ход 5 security: только от авторизованного отправителя.
    from_user = msg.get("from") or {}
    if not _is_authorized_sender(from_user, _allowed_chat_id()):
        logger.warning(
            "[clarify-worker] text msg from unauthorized user_id=%s — игнор",
            from_user.get("id"),
        )
        return
    chat = msg.get("chat") or {}
    chat_id = int(chat.get("id") or 0)

    pendings = clarify_state.list_pending(root=pending_root, status_filter=["pending"])
    # Поздний ответ через текст? — допустимо: смотрим timed_out тоже, если
    # ровно один state такой (иначе — игнор).
    if not pendings:
        timed_outs = clarify_state.list_pending(root=pending_root, status_filter=["timed_out"])
        if len(timed_outs) == 1:
            _try_apply_text_to_state(text, timed_outs[0], bot_token, chat_id, pending_root, is_late=True)
            return
        if len(timed_outs) > 1:
            # Н1.Н3 (ход 1): 2+ истекших — текстовый ответ применить
            # безопасно нельзя, иначе угадаем не тот meeting. Отвечаем явно.
            try:
                telegram_api.send_message(
                    bot_token, chat_id,
                    f"⏰ У меня {len(timed_outs)} истёкших уточнений. "
                    "Нажми кнопку под нужным сообщением — там встроен meeting_id.",
                )
            except telegram_api.TelegramApiError as e:
                logger.warning("[clarify-worker] reply for multi-timed_out failed: %s", e)
        return

    if len(pendings) > 1:
        try:
            telegram_api.send_message(
                bot_token, chat_id,
                f"⚠️ У меня сейчас {len(pendings)} открытых уточнений. "
                "Нажми кнопку под нужным сообщением (там встроен meeting_id), "
                "текстовый ответ применить безопасно не могу.",
            )
        except telegram_api.TelegramApiError as e:
            logger.warning("[clarify-worker] reply for multi-pending failed: %s", e)
        return

    _try_apply_text_to_state(text, pendings[0], bot_token, chat_id, pending_root, is_late=False)


def _try_apply_text_to_state(
    text: str,
    state: dict,
    bot_token: str,
    reply_chat_id: int,
    pending_root: Path,
    *,
    is_late: bool,
) -> None:
    name_pool = state.get("name_pool", []) or []
    cluster_keys_ordered = state.get("cluster_keys_ordered", []) or []
    unclear = state.get("unclear_clusters") or {}
    cluster_label_to_key: dict[str, str] = {}
    for cluster_key in cluster_keys_ordered:
        label = unclear.get(cluster_key, {}).get("speaker_label_in_md")
        if label:
            cluster_label_to_key[label] = cluster_key

    mapping = llm_postprocess.parse_clarify_text_answer(
        text,
        cluster_keys_ordered=cluster_keys_ordered,
        cluster_label_to_key=cluster_label_to_key,
        name_pool=name_pool,
        meeting_sid=state.get("meeting_id"),
    )

    if not mapping:
        # Распарсить не получилось. Не пытаемся «угадать» — даём подсказку.
        try:
            telegram_api.send_message(
                bot_token, reply_chat_id,
                "Не распознал. Формат: «Спикер 3 = Дарья» или «не знаю».",
            )
        except telegram_api.TelegramApiError:
            pass
        return

    _apply_resolution(state, mapping, via="text", pending_root=pending_root, is_late=is_late)
    try:
        ack = "✅ Применил" if not is_late else "⏰ Поздно — обновил файл, в группу не шлю"
        names = ", ".join(mapping.values())
        telegram_api.send_message(bot_token, reply_chat_id, f"{ack}: {names}")
    except telegram_api.TelegramApiError:
        pass


# ----- Таймауты -------------------------------------------------------------

def sweep_timeouts(pending_root: Path) -> int:
    """Помечает просроченные pending'и как timed_out. Возвращает число изменений."""
    n = 0
    for state in clarify_state.list_pending(root=pending_root, status_filter=["pending"]):
        if clarify_state.is_past_deadline(state):
            clarify_state.mark_status(
                state["meeting_id"], "timed_out", root=pending_root,
                extra={"resolved_at": clarify_state.now_iso()},
            )
            logger.info("[clarify] timed_out meeting=%s", state["meeting_id"])
            n += 1
    return n


# ----- Главный цикл ---------------------------------------------------------

_running = True


def _handle_signal(_signum, _frame):
    global _running
    _running = False
    logger.info("[clarify-worker] signal received, shutting down")


def has_any_pending_clarify(pending_root: Optional[Path] = None) -> bool:
    """True если есть хоть один state со статусом pending или timed_out.

    Используется listener'ом чтобы решить: применить текстовое сообщение Ильи
    как clarify-ответ (`process_text_message`) или передать в старый apply_reply
    flow для блока 📅.
    """
    root = pending_root or clarify_state.resolve_pending_dir()
    if not root.exists():
        return False
    for status in ("pending", "timed_out"):
        if clarify_state.list_pending(root=root, status_filter=[status]):
            return True
    return False


def run_forever(bot_token: str, *, long_poll_timeout: int = 25) -> None:
    """Бесконечный long-poll цикл (standalone-mode для локального smoke).

    В продакшене на VPS вместо этого вызываются `process_callback`,
    `process_text_message`, `sweep_timeouts` из meetings_listener.py,
    который владеет единственным getUpdates на токен.
    """
    if not bot_token:
        raise SystemExit("TELEGRAM_NOTARIUS_BOT_TOKEN не задан — нечего слушать.")
    pending_root = clarify_state.resolve_pending_dir()
    pending_root.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    offset = load_offset(pending_root)
    logger.info(
        "[clarify-worker] start pending_root=%s offset=%d timeout=%ds",
        pending_root, offset, long_poll_timeout,
    )

    last_sweep = 0.0
    sweep_interval_s = 30.0  # частота проверки таймаутов

    while _running:
        # Сначала — sweep таймаутов (дешёво, файловые операции).
        now = time.monotonic()
        if now - last_sweep >= sweep_interval_s:
            sweep_timeouts(pending_root)
            last_sweep = now

        # long-poll. Сам call длится до `long_poll_timeout` сек, не сжигает CPU.
        try:
            updates = telegram_api.get_updates(
                bot_token,
                offset=offset,
                timeout=long_poll_timeout,
                allowed_updates=["callback_query", "message"],
            )
        except telegram_api.TelegramApiError as e:
            logger.warning("[clarify-worker] getUpdates error (sleep 5s): %s", e)
            time.sleep(5)
            continue

        for upd in updates:
            try:
                update_id = int(upd.get("update_id") or 0)
                offset = max(offset, update_id + 1)
                if "callback_query" in upd:
                    process_callback(upd["callback_query"], pending_root, bot_token)
                elif "message" in upd:
                    process_text_message(upd["message"], pending_root, bot_token)
            except Exception as e:
                # Не валим воркера на одной ошибке — это сервис.
                logger.exception("[clarify-worker] update %s processing failed: %s",
                                 upd.get("update_id"), e)

        save_offset(pending_root, offset)

    logger.info("[clarify-worker] exited cleanly, offset=%d", offset)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    if not token:
        sys.stderr.write(
            "TELEGRAM_NOTARIUS_BOT_TOKEN не задан. На VPS он уже есть в "
            "/srv/meeting-notary/.env.notary; в проде слушает meetings_listener.\n"
            "Standalone-mode (этот скрипт) — только для локального smoke на маке.\n"
        )
        return 2
    try:
        long_poll = int(os.environ.get("CLARIFY_LONG_POLL_TIMEOUT", "25"))
    except ValueError:
        long_poll = 25
    run_forever(token, long_poll_timeout=long_poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
