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
from . import series_memory  # Ф7: память серии встреч


logger = logging.getLogger(__name__)


# Постоянство offset'а для getUpdates (чтобы рестарт не повторял старые updates).
OFFSET_FILE_NAME = ".clarify_offset"

# Ф4 (REQ 4.3): timed_out, провисевший дольше этого порога, sweep архивирует.
# Дефолт 7 дней — разумно для ночи, обратимо (статус, не удаление файла).
# Переопределяется env `MEETING_NOTARY_CLARIFY_ARCHIVE_DAYS`.
DEFAULT_CLARIFY_ARCHIVE_DAYS = 7


def _archive_days() -> int:
    raw = (os.environ.get("MEETING_NOTARY_CLARIFY_ARCHIVE_DAYS") or "").strip()
    if not raw:
        return DEFAULT_CLARIFY_ARCHIVE_DAYS
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_CLARIFY_ARCHIVE_DAYS
    return v if v > 0 else DEFAULT_CLARIFY_ARCHIVE_DAYS


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


def _ack_suffix(is_late: bool, redelivery_status: Optional[str]) -> str:
    """Текст подтверждения Илье с учётом до-сыла обновлённой версии (5.5/5.6)."""
    if redelivery_status == "sent":
        return "✅ Применил — обновлённую версию дослал в группу"
    if not is_late:
        return "✅ Применил"
    return "⏰ Поздно (после таймаута) — обновил файл"


def _apply_resolution(
    state: dict,
    mapping: dict[str, str],
    *,
    via: str,
    pending_root: Path,
    is_late: bool = False,
) -> Optional[str]:
    """Применяет mapping к транскрипту на диске, помечает state.

    Возвращает статус до-сыла обновлённой версии в группу (5.5/5.6):
    "sent" / "not-delivered-yet" / "no-change" / "skipped" / None (не пытались).
    Нужен вызывающему для текста ack'а Илье.
    """
    if not mapping:
        logger.info(
            "[clarify] %s meeting=%s via=%s nothing-to-apply",
            "late_answer" if is_late else "resolved",
            state.get("meeting_id"), via,
        )
        return None

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
    # Ф5 (5.5/5.6): если протокол УЖЕ был доставлен — дослыаем обновлённую
    # версию в группу с блоком «🔁 Что изменилось» (revision-маркер).
    protocol_regenerated = False
    redelivery_status: Optional[str] = None
    if file_updated and transcript_path.exists():
        # Имя протокола живёт рядом: `<date>-protokol.md` (тот же паттерн,
        # что в finalize-meeting.py). transcript_path.stem = `<date>`
        # (например `2026-05-27`); если в имени окажется не дата —
        # generate_protocol всё равно возьмёт дату из meta.
        protocol_path = transcript_path.parent / f"{transcript_path.stem}-protokol.md"
        meta_block = state.get("meta") or {}
        # Снимок старой версии ДО перегенерации — для diff «🔁 что изменилось»
        # и для content-hash идемпотентности до-сыла (5.6).
        old_protocol_text = ""
        if protocol_path.is_file():
            try:
                old_protocol_text = protocol_path.read_text(encoding="utf-8")
            except OSError:
                old_protocol_text = ""
            # Сохраняем версию vN в _versions/ для аудита (как correction flow).
            try:
                llm_postprocess._save_protocol_version(protocol_path)
            except Exception:  # noqa: BLE001
                pass
        # Ф7 (7.3/7.4): справку памяти серии подкладываем и в РЕВИЗИЮ — чтобы
        # пере-генерированный протокол держал ту же дисциплину «прошлое = справка»
        # и узнавал постоянный состав, как первичная генерация. transcript лежит
        # в `<root>/<series>/<date>.md` → parent = серия, parent.parent = root.
        # Best-effort: сбой → без справки (ревизия не страдает).
        series_memory_block = ""
        try:
            if series_memory.is_enabled() and series_memory.has_series_slug(meta_block.get("series")):
                _smem = series_memory.resolve_memory(
                    transcript_path.parent, transcript_path.parent.parent,
                    current_participants=state.get("name_pool") or [],
                    current_date=meta_block.get("date") or None,
                )
                series_memory_block = series_memory.format_memory_block(_smem)
                # Ф6 (G6/G7/G11): кросс-встречный фон в РЕВИЗИЮ тоже (как и память
                # серии) — чтобы пере-генерация держала тот же фон, что первичная.
                # transcript лежит в `<root>/<series>/<date>.md` → parent = серия,
                # parent.parent = root. Best-effort внутри build_cross_memory_block → "".
                cross_block = llm_postprocess.build_cross_memory_block(
                    transcript_path.parent.parent, transcript_path.parent, meta_block,
                    current_participants=state.get("name_pool") or [],
                    same_series_digests=_smem,
                    meeting_sid=state.get("meeting_id"),
                )
                series_memory_block = "\n\n".join(
                    p for p in (series_memory_block.strip(), cross_block.strip()) if p
                )
        except Exception:  # noqa: BLE001
            series_memory_block = ""
        try:
            regen_meta = {
                "series": meta_block.get("series") or "",
                "date": meta_block.get("date") or "",
                # sessionUid не критичен для генерации, но передаём для лога.
                "sessionUid": meta_block.get("sessionUid"),
                # expected для шапки берём из name_pool (итоговый pool, что отдавали
                # Илье в clarify — там и резолвленные имена). Ф5#3: participants
                # (панель) РАНЬШЕ зануляли — из-за этого шапка «Участники:» ревизии
                # расходилась с первичной доставкой; теперь берём панель из meta,
                # как первичная генерация (low-risk выравнивание).
                "expectedParticipants": state.get("name_pool", []),
                "participants": meta_block.get("participants") or [],
                "transcript_filename": transcript_path.name,
            }
            llm_postprocess.regenerate_protocol_for_meeting(
                transcript_path=transcript_path,
                protocol_path=protocol_path,
                meeting_meta=regen_meta,
                meeting_sid=state.get("meeting_id"),
                series_memory=series_memory_block,  # Ф7
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

        # Ф6 (долг Ф5): перегенерация выше делает протокол ЗАНОВО, без ⚠️-пометок
        # ревью (5.2 числа + 6.2 роли). Прогоняем тот же объединённый ревью-проход
        # ПОСЛЕ регена и ДО до-сыла — иначе поздняя ревизия теряет пометки, и
        # diff «🔁 что изменилось» ложно показал бы «убрали ⚠️». Best-effort:
        # нет claude / kill-switch → 0 пометок, файл не трогаем.
        # Ф7 (G8): rewrite=True — паритет с finalize. Поздний clarify тоже отдаёт
        # вычитанный второй проходом протокол (иначе доразметка теряла бы редактуру
        # критика). ТЕМ ЖЕ одним вызовом, что diarization — не третий вызов.
        if protocol_regenerated:
            try:
                n_flags = llm_postprocess.review_and_flag_protocol_file(
                    protocol_path=protocol_path,
                    transcript_path=transcript_path,
                    checks=("values", "roles", "memory", "diarization"),  # Ф7+Ф4: те же checks, что в finalize, ОДНИМ вызовом
                    meeting_sid=state.get("meeting_id"),
                    rewrite=True,
                )
                if n_flags:
                    logger.info(
                        "[review] meeting=%s self-review изменил %d пункт(ов)",
                        state.get("meeting_id"), n_flags,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[review] revision re-flag failed (non-fatal) meeting=%s: %s",
                    state.get("meeting_id"), e,
                )

        # Ф5 (5.5/5.6): до-сыл обновлённой версии. meta.json лежит рядом с
        # транскриптом. redeliver сам решает: если ещё не доставляли →
        # not-delivered-yet (первичная доставка подхватит); если контент не
        # менялся → no-change; иначе шлёт ревизию, ОБХОДЯ идемпотентность.
        if protocol_regenerated:
            try:
                new_protocol_text = protocol_path.read_text(encoding="utf-8")
            except OSError:
                new_protocol_text = ""
            if new_protocol_text.strip():
                meta_json_path = transcript_path.parent / "meta.json"
                try:
                    redeliver_meta = {
                        "series": meta_block.get("series") or "",
                        "date": meta_block.get("date") or "",
                        "sessionUid": meta_block.get("sessionUid"),
                        "expectedParticipants": state.get("name_pool", []),
                        "participants": [],
                    }
                    res = llm_postprocess.redeliver_revised_protocol(
                        redeliver_meta,
                        old_protocol_text,
                        new_protocol_text,
                        meta_json_path=meta_json_path if meta_json_path.is_file() else None,
                        meeting_sid=state.get("meeting_id"),
                    )
                    redelivery_status = res.get("status")
                    logger.info(
                        "[revision] meeting=%s redelivery=%s",
                        state.get("meeting_id"), redelivery_status,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[revision] redeliver failed (non-fatal) meeting=%s: %s",
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
        "[clarify] %s meeting=%s via=%s applied=%d file_updated=%s protocol_regen=%s redelivery=%s",
        label, state["meeting_id"], via, len(mapping), file_updated,
        protocol_regenerated, redelivery_status or "n/a",
    )
    return redelivery_status


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
    redelivery_status = _apply_resolution(
        state, mapping, via="callback", pending_root=pending_root, is_late=is_late
    )

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
        ack_suffix = _ack_suffix(is_late, redelivery_status)
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


def _find_state_by_message_id(
    message_id, pending_root: Path,
    *, statuses: tuple[str, ...] = ("pending", "timed_out"),
) -> Optional[dict]:
    """Ф4 (REQ 4.2): находит clarify-state, чей сохранённый `message_id`
    совпадает с message_id отвеченного (reply) сообщения бота.

    По умолчанию смотрим только ОТКРЫТЫЕ pending/timed_out (поздний ответ на них
    ещё применяем). `statuses` можно переопределить на ЗАКРЫТЫЕ
    (resolved/archived) — чтобы отличить «reply на уже закрытый вопрос» от УПУ3
    (старый state без сохранённого id). `message_id == 0` в state = «не сохранён»
    (старый код / send без ответа Telegram) → не матчим (count-фолбэк подхватит).
    """
    try:
        target = int(message_id)
    except (TypeError, ValueError):
        return None
    if target <= 0:
        return None
    for state in clarify_state.list_pending(
        root=pending_root, status_filter=list(statuses)
    ):
        try:
            if int(state.get("message_id") or 0) == target:
                return state
        except (TypeError, ValueError):
            continue
    return None


def process_text_message(
    msg: dict,
    pending_root: Path,
    bot_token: str,
) -> None:
    """Текстовое сообщение Ильи. Приоритет — reply-матчинг по `message_id`
    (REQ 4.2): Reply на конкретный вопрос применяется даже при нескольких
    открытых/истёкших clarify. Если reply нет (или message_id не сохранён —
    УПУ3) — фолбэк «ровно один pending/timed_out», иначе просим кнопку."""
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

    # REQ 4.2 — reply-матчинг по message_id. Срабатывает раньше count-based
    # логики: Илья ответил Reply'ем именно на этот вопрос → точная привязка
    # к meeting'у, сколько бы открытых/истёкших clarify ни было.
    reply_src = msg.get("reply_to_message") or {}
    reply_to_mid = reply_src.get("message_id")
    if reply_to_mid is not None:
        matched = _find_state_by_message_id(reply_to_mid, pending_root)
        if matched is not None:
            is_late = matched.get("status") == "timed_out"
            _try_apply_text_to_state(
                text, matched, bot_token, chat_id, pending_root, is_late=is_late
            )
            return
        # Среди ОТКРЫТЫХ совпадения нет. Если этот message_id принадлежит уже
        # ЗАКРЫТОМУ вопросу (resolved/archived) — Илья реплайнул на закрытый
        # вопрос. НЕ угадываем по count (иначе применим ответ к ЧУЖОЙ открытой
        # встрече — misattribution). Старый clarify без сохранённого id (УПУ3)
        # сюда не попадёт: у него message_id=0 → не найдётся ни среди открытых,
        # ни среди закрытых → корректно падает в count-фолбэк ниже.
        closed = _find_state_by_message_id(
            reply_to_mid, pending_root, statuses=("resolved", "archived")
        )
        if closed is not None:
            why = "уже отвечен" if closed.get("status") == "resolved" else "слишком старый (архивирован)"
            try:
                telegram_api.send_message(
                    bot_token, chat_id,
                    f"↩️ Этот вопрос {why}. Чтобы поправить — ответь Reply'ем на "
                    "активный вопрос или нажми кнопку под ним.",
                )
            except telegram_api.TelegramApiError as e:
                logger.warning("[clarify-worker] reply-to-closed notice failed: %s", e)
            return
        # message_id не совпал ни с одним известным state (старый clarify без
        # сохранённого message_id — УПУ3, либо reply на не-clarify сообщение) →
        # падаем в count-фолбэк ниже, поздний ответ не теряется.

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

    redelivery_status = _apply_resolution(
        state, mapping, via="text", pending_root=pending_root, is_late=is_late
    )
    try:
        ack = _ack_suffix(is_late, redelivery_status)
        names = ", ".join(mapping.values())
        telegram_api.send_message(bot_token, reply_chat_id, f"{ack}: {names}")
    except telegram_api.TelegramApiError:
        pass


# ----- Таймауты -------------------------------------------------------------

def sweep_timeouts(pending_root: Path) -> int:
    """Sweep таймаутов. Возвращает число изменённых state'ов.

    Два шага:
      1) pending → timed_out по дедлайну;
      2) timed_out → archived (REQ 4.3) если провисел дольше N дней
         (`MEETING_NOTARY_CLARIFY_ARCHIVE_DAYS`, дефолт 7) — чтобы stale не
         копились и `has_any_pending_clarify` их не считал активными.
    """
    n = 0
    for state in clarify_state.list_pending(root=pending_root, status_filter=["pending"]):
        if clarify_state.is_past_deadline(state):
            clarify_state.mark_status(
                state["meeting_id"], "timed_out", root=pending_root,
                extra={"resolved_at": clarify_state.now_iso()},
            )
            logger.info("[clarify] timed_out meeting=%s", state["meeting_id"])
            n += 1

    archive_days = _archive_days()
    for state in clarify_state.list_pending(root=pending_root, status_filter=["timed_out"]):
        age = clarify_state.timed_out_age_days(state)
        if age is not None and age >= archive_days:
            clarify_state.mark_status(
                state["meeting_id"], "archived", root=pending_root,
                extra={"archived_at": clarify_state.now_iso()},
            )
            logger.info(
                "[clarify] archived stale timed_out meeting=%s age=%.1fd threshold=%dd",
                state["meeting_id"], age, archive_days,
            )
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
    flow для блока 📅. `archived` (REQ 4.3) НЕ считается активным — sweep
    архивирует stale, и они перестают перехватывать текстовые ответы Ильи.
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
