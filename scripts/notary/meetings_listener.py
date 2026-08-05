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

import atexit
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

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
# R9 (watson Ф6): троттл дренажа очереди дозабора understand-слоя. Дозабор не срочный
# («разберу позже, когда ИИ оживёт») — не хаммерим claude каждые 30с при затяжной
# недоступности ИИ. Переопределяемо env'ом.
DRAIN_DEFERRED_EVERY_S = int(os.environ.get("REPLY_UNDERSTAND_DRAIN_EVERY_S", "120") or "120")
_last_deferred_drain_mono: Optional[float] = None  # None = ещё не дренажили → первый проход не троттлится
TRIGGER_PREFIX = "\U0001F4C5"  # 📅 — вечерний блок (apply_reply flow)

# ── Ф2: фоновый перевыпуск протокола ───────────────────────────────────────
# Перевыпуск (claude-генерация + доставка PDF, 200–600с на слабом CPU) больше не
# блокирует главный цикл. Тяжёлая часть (`reissue_one`) уходит в фоновый поток на
# ОДИН воркер (A2/A4: один claude на CPU за раз; несколько встреч — по очереди).
# claim и finalize остаются в главном потоке (R3: мутации feedback-state в одном
# потоке; R2/FM-10: claim в том же проходе sweep, что и feedback-sweep).
_reissue_executor: Optional[ThreadPoolExecutor] = None
# fid → (future, claimed_state, submitted_monotonic). claimed нужен finalize'у
# (base round/attempts), submitted_ts — для лога длительности (метаданные, R9).
_reissue_inflight: dict[str, tuple[Future, dict, float]] = {}
# cap=1 (A2): не более одной reissue-генерации одновременно. claim берёт встречу
# только когда воркер свободен → claimed-but-queued не возникает, state не висит в
# `reissuing` дольше одной генерации (RECLAIM_STALE_REISSUING_SEC корректен).
_REISSUE_CAP = 1
# ── И1: командные claude-пути через ТОТ ЖЕ исполнитель ───────────────────────
# protocol-команда / correction-команда / apply-reply: heavy-часть (claude +
# доставка результата) уходит в `_reissue_executor` (max_workers=1) — тот же воркер,
# что и reissue. Так физически ≤1 claude одновременно (I4/A2: РИСК3 закрыт
# структурно — гонки за protocol-файл между командой и перевыпуском нет). Каждый job
# самодостаточен: делает claude-работу + сам шлёт результат + сам шлёт user-facing
# ошибку при сбое (как раньше синхронный код). Главный поток после ack не блокируется.
# job_id → (future, краткая-метка, submitted_monotonic). Только главный поток мутирует
# реестр (submit из process_message, drain из sweep — оба в главном потоке). label и
# submitted_ts — для лога метаданных (R9: тип операции + длительность, без текста).
_command_inflight: dict[str, tuple[Future, str, float]] = {}
# Монотонный счётчик для уникального job_id (метка операции + порядковый номер). Не
# несёт смысла кроме уникальности ключа реестра; растёт за жизнь процесса.
_command_job_seq = 0
# R10: троттл уборки dormant-state'ов — не чаще раза в сутки (модульный timestamp,
# а не глоб папки каждые 30с). monotonic, None = ещё не запускали в этой жизни.
_last_dormant_cleanup_mono: Optional[float] = None
_DORMANT_CLEANUP_EVERY_S = 24 * 3600
# R11: фиксированный текст уведомления о старте перевыпуска. Без вставок из правок
# (R9) — только метаданные не нужны участнику, текст один и тот же.
_REISSUE_NOTICE_TEXT = (
    "\U0001F527 Учёл правки, пересобираю протокол — пришлю обновлённую версию "
    "через пару минут"
)


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


def send_message(
    token: str, chat_id: int, text: str, *, reply_to: int | None = None, html: bool = False
) -> int | None:
    """Отправить сообщение. Возвращает `message_id` отправленного (нужен Ф4 для state
    ack по message_id — R15/R26) или None при сбое. Прежние вызывающие возврат
    игнорируют — обратной несовместимости нет."""
    params: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    if html:
        params["parse_mode"] = "HTML"
    try:
        resp = telegram_request(token, "sendMessage", params, timeout=15)
        if not resp.get("ok"):
            logger.warning("sendMessage: %s", resp.get("description", "?"))
            return None
        return (resp.get("result") or {}).get("message_id")
    except Exception as e:  # noqa: BLE001
        logger.warning("sendMessage упал: %s", e)
        return None


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


# Дедуп Telegram-команд «протокол <series> <date>». Илья диктует через
# Wispr Flow, повторы реальны (CLAUDE.md прямо предупреждает). Без дедупа:
# 2 sendMessage + 2 subprocess'а claude за одну команду. С дедупом — окно
# 30 сек по ключу (series, date); повтор в окне → короткий «уже запустил».
_protocol_command_recent: dict[tuple[str, str], float] = {}
_PROTOCOL_COMMAND_DEDUPE_WINDOW_S = 30.0


def _protocol_command_is_dupe(series: str, date_str: str) -> bool:
    """True если такая команда уже запускалась < 30 сек назад. Side-effect:
    при False — отмечает запуск, при True — оставляет старый timestamp.
    Чистим устаревшие записи лениво (нет cron, словарь живёт в RAM)."""
    now = time.monotonic()
    key = (series, date_str)
    # Лениво вычищаем старые записи (не разрастаемся).
    stale = [k for k, t in _protocol_command_recent.items()
             if now - t > _PROTOCOL_COMMAND_DEDUPE_WINDOW_S]
    for k in stale:
        _protocol_command_recent.pop(k, None)
    last = _protocol_command_recent.get(key)
    if last is not None and now - last < _PROTOCOL_COMMAND_DEDUPE_WINDOW_S:
        return True
    _protocol_command_recent[key] = now
    return False


def _protokol_root() -> Path:
    """Корень папки встреч. На VPS можно переопределить env'ом, дефолт — мак-путь.

    На VPS финализация уже пишет в `~/Projects/me/встречи/` через mirror;
    listener живёт там же. Если структура иная — `MEETING_NOTARY_PROTOCOLS_DIR`
    в .env.notary переопределит.
    """
    raw = os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR") or "~/Projects/me/встречи"
    return Path(os.path.expanduser(raw))


def _reissue_guard_blocks(token: str, chat_id: int, series: str, date_str: str, msg: dict[str, Any]) -> bool:
    """R12 (РИСК3): если встреча сейчас в фоновом перевыпуске (`reissuing`) —
    блокируем ручную команду, чтобы параллельная генерация не затёрла protocol-файл.

    Read-only проверка статуса (без claude, без рефактора путей). True → команда
    заблокирована (caller отвечает «обработано» и выходит); False → можно выполнять.
    best-effort: сбой чтения state НЕ блокирует команду (пропускаем guard, команда
    идёт как раньше — отказ от guard безопаснее ложной блокировки легитимной команды).
    """
    try:
        from notary.lib import feedback_state  # noqa: PLC0415
        fid = feedback_state.build_feedback_id(series, date_str, chat_id)
        st = feedback_state.read_state(fid)
    except Exception as e:  # noqa: BLE001
        logger.debug("[reissue] R12 guard read_state упал (пропускаю guard): %s", e)
        return False
    if st is not None and st.get("status") == "reissuing":
        logger.info("[reissue] R12 guard: команда на reissuing-встречу fid=%s — отложена", fid)
        send_message(
            token, chat_id,
            "⏳ Эта встреча сейчас пересобирается по правкам — повтори через пару минут",
            reply_to=msg.get("message_id"),
        )
        return True
    return False


def _executor_busy() -> bool:
    """I7: True если фоновый воркер сейчас чем-то занят (reissue ИЛИ командный job
    in-flight) → queue-aware ack добавит «в очереди». max_workers=1, поэтому любой
    непустой реестр означает, что новый submit встанет в очередь за текущей работой.

    Главный поток читает оба реестра (мутируют тоже только из главного потока:
    submit/drain командных — здесь; reissue — в `_process_reissues_async`), гонки нет.
    """
    return bool(_command_inflight) or bool(_reissue_inflight)


def _submit_command_job(
    executor: Optional[ThreadPoolExecutor],
    inflight: dict,
    job_id: str,
    label: str,
    fn: Callable[[], Any],
) -> bool:
    """И1: отправить самодостаточный командный job в фоновый воркер. Кладёт future в
    `inflight[job_id] = (future, label, submitted_monotonic)`. True если submit удался.

    Fallback (executor is None — тесты / совместимость): зовём `fn()` СИНХРОННО в
    главном потоке и возвращаем True. Так старые тесты, не поднимающие executor, видят
    прежнее блокирующее поведение; боевой `main` всегда создаёт executor.

    R9: логируем только метаданные (label, job_id) — НЕ содержимое job.
    """
    if executor is None:
        # Синхронный fallback: исключение job-функции не должно валить листенер
        # (боевой путь его и не увидел бы — job сам ловит и шлёт user-facing ошибку,
        # но на всякий случай страхуемся, как `process_message` оборачивает всё).
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            logger.exception("[cmd] синхронный job %s упал: %s", label, e)
        return True
    try:
        future = executor.submit(fn)
    except Exception as e:  # noqa: BLE001
        # Submit упал (executor уже shutdown и т.п.) — НЕ оставляем команду без следа.
        logger.exception("[cmd] submit job %s упал: %s", label, e)
        return False
    inflight[job_id] = (future, label, time.monotonic())
    logger.info("[cmd] submit job=%s label=%s → фон", job_id, label)
    return True


def _next_command_job_id(label: str) -> str:
    """Уникальный job_id из метки + монотонного счётчика (главный поток, без гонок)."""
    global _command_job_seq
    _command_job_seq += 1
    return f"{label}#{_command_job_seq}"


def _command_label_inflight(label: str) -> bool:
    """У1 (цикл5/ход3): True если job с такой меткой уже в очереди/в работе.

    Дополняет 30-секундный time-дедуп команд: с И1 командный job может стоять в
    очереди минуты (за длинным reissue), а time-окно (30с) уже протухло → передиктовка
    Wispr Flow дала бы ВТОРОЙ идентичный job (двойная claude-генерация + двойная
    доставка). Проверка по живому реестру ловит дубль на всё время выполнения, а не
    только 30с. (Метка = `protocol:series/date` / `correction:series/date` — одна на
    встречу-операцию.) Главный поток читает реестр, гонки нет.
    """
    return any(lbl == label for (_f, lbl, _ts) in _command_inflight.values())


def _drain_command_jobs(*, inflight: dict) -> None:
    """И1: снять завершённые командные future из реестра (главный поток, рядом с
    `_process_reissues_async` в sweep). Сам результат/ошибку job уже доставил
    пользователю изнутри (самодостаточность I5) — здесь только освобождаем слот и
    логируем метаданные (label, длительность, было ли исключение).

    `future.result()` зовём, чтобы поднять и залогировать необработанное исключение
    job-функции (не должно случаться — job ловит всё внутри, но не теряем сигнал, как
    делает drain reissue). R9: логируем только метаданные — без текста реплик/протокола.
    """
    for job_id in list(inflight.keys()):
        future, label, submitted_ts = inflight[job_id]
        if not future.done():
            continue
        err: Optional[BaseException] = None
        try:
            future.result()
        except Exception as e:  # noqa: BLE001
            err = e
            logger.exception("[cmd] job=%s label=%s бросил необработанное: %s",
                             job_id, label, e)
        inflight.pop(job_id, None)
        dur = time.monotonic() - submitted_ts
        logger.info("[cmd] drain job=%s label=%s dur=%.1fs ok=%s",
                    job_id, label, dur, err is None)


def _job_protocol_command(
    token: str,
    chat_id: int,
    msg_id: Optional[int],
    series: str,
    date_str: str,
    transcript_path: Path,
    protocol_path: Path,
) -> None:
    """И1 (I1): heavy-часть protocol-команды — В ФОНОВОМ ВОРКЕРЕ (после ack).

    Самодостаточен (I5): генерация (claude) → read → split → send результата, со
    ВСЕМИ user-facing ошибками (как раньше синхронный хвост route-функции). Никаких
    замыканий на нестабильное состояние — всё нужное приходит аргументами. Реестр
    `_command_inflight` НЕ мутирует (это только главный поток в submit/drain).

    R9: текст протокола/транскрипта НЕ логируем — только метаданные через вызовы ниже.
    """
    def _err(text: str) -> None:
        send_message(token, chat_id, text, reply_to=msg_id)

    try:
        from notary.lib.llm_postprocess import (  # noqa: PLC0415
            ProtocolGenerationError,
            regenerate_protocol_for_meeting,
            review_and_flag_protocol_file,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("import llm_postprocess failed: %s", e)
        _err(f"❌ Не смог загрузить генератор протоколов: {type(e).__name__}")
        return

    meta = {
        "series": series,
        "date": date_str,
        "transcript_filename": transcript_path.name,
    }
    # Ф2 (ISS-22 б): снимок ПРОШЛОЙ версии протокола ДО регенерации — atomic-
    # перезапись ниже её затрёт (РИСК2/A2). Команда не сохраняет _versions/ (это
    # делает только clarify), поэтому хватаем здесь и пробрасываем в review как
    # страховку инварианта «не потеряй задачи/решения прошлой версии». Best-effort:
    # нет файла (первая сборка) / не прочёлся → None → обычный self-review (R-b4).
    prior_protocol_text = None
    if protocol_path.is_file():
        try:
            prior_protocol_text = protocol_path.read_text(encoding="utf-8")
        except OSError:
            prior_protocol_text = None
    try:
        regenerate_protocol_for_meeting(
            transcript_path=transcript_path,
            protocol_path=protocol_path,
            meeting_meta=meta,
            meeting_sid=f"tg-cmd-{series}-{date_str}",
        )
    except ProtocolGenerationError as e:
        _err(f"❌ Генерация упала: {str(e)[:300]}\nФайл: `{protocol_path}` не обновлён.")
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("protocol generation unexpected error: %s", e)
        _err(f"❌ Неожиданная ошибка: {type(e).__name__}: {str(e)[:200]}")
        return

    # Ф1 (ISS-22 а): ручная команда «протокол …» регенерит протокол ЗАНОВО, как и
    # finalize/clarify. Закрываем дыру — прогоняем ТОТ ЖЕ второй проход-критик
    # (rewrite=True) с теми же checks, что в finalize. Один claude-вызов (он же
    # несёт ревизию диаризации Ф4), не третий. Транскрипт уже на руках — переиспользуем.
    # Best-effort/паритет с clarify: нет claude / kill-switch выключен → 0 пометок,
    # файл не трогаем, ошибка НЕ валит команду. R9: в лог только счётчики/метаданные.
    # Ф3 (watson-company-memory): + check "knowledge" и `series` — ручная команда тоже
    # сверяет черновик со СПРАВКОЙ компании (паритет всех 4 боевых call-site).
    try:
        n_flags = review_and_flag_protocol_file(
            protocol_path=protocol_path,
            transcript_path=transcript_path,
            checks=("values", "roles", "memory", "diarization", "knowledge"),
            meeting_sid=f"tg-cmd-{series}-{date_str}",
            rewrite=True,
            series=series,  # Ф3 (УПУ1): → компания → справка компании критику
            # Ф2 (ISS-22 б): прошлая версия → инвариант «не теряем задачи/решения».
            prior_sources=[prior_protocol_text] if prior_protocol_text else None,
        )
        if n_flags:
            logger.info(
                "[review] meeting=tg-cmd-%s-%s self-review изменил %d пункт(ов)",
                series, date_str, n_flags,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[review] protocol command self-review failed (non-fatal) series=%s date=%s: %s",
            series, date_str, e,
        )

    # Файл готов — пушим результат текстом. Если > лимита — split на части.
    try:
        body = protocol_path.read_text(encoding="utf-8")
    except OSError as e:
        _err(f"✅ Файл сгенерирован → `{protocol_path}`\n⚠️ Прочесть для отправки не смог: {e}")
        return

    try:
        from notary.lib.telegram_api import split_long_message  # noqa: PLC0415
        chunks = split_long_message(body, max_len=3500)
    except Exception as e:  # noqa: BLE001
        # Если split-helper упал (циклический импорт / неожиданная ошибка) —
        # не молчим: лог + отправляем сообщение Илье, чтобы он узнал что
        # протокол на диске, но в Telegram не дошёл. НЕ режем `[:max_len]`
        # незаметно — это была бы тихая потеря данных (РИСК2-стиль).
        logger.exception("split_long_message failed: %s", e)
        _err(
            f"✅ Файл сгенерирован → `{protocol_path}`\n"
            f"⚠️ Не смог разбить длинный текст для отправки в Telegram: {type(e).__name__}. "
            f"Открой файл на диске."
        )
        return

    header = f"✅ Готово: `{series} {date_str}` → `{protocol_path}`"
    send_message(token, chat_id, header, reply_to=msg_id)
    for chunk in chunks:
        send_message(token, chat_id, chunk)


def maybe_route_to_protocol_command(token: str, chat_id: int, msg: dict[str, Any]) -> bool:
    """Если сообщение Ильи — команда «протокол <series> <date>», запускаем
    регенерацию и шлём результат текстом обратно.

    Возвращает True если команда распарсилась и обработана (caller должен
    выйти из process_message без вызова apply_reply). False иначе.

    И1: parse / R12 guard / дедуп / валидация transcript / ack — в ГЛАВНОМ потоке
    (быстро). Heavy-часть (генерация → read → split → send) — в фоновом воркере через
    `_job_protocol_command`. Если parse OK, но запустить не удалось — Илье уходит
    сообщение об ошибке (из job либо синхронно), всё равно True (сообщение «обработано»).
    """
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    try:
        from notary.lib.protocol_command import parse_protocol_command  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.debug("protocol_command import failed: %s", e)
        return False
    parsed = parse_protocol_command(text)
    if parsed is None:
        return False
    series, date_str = parsed

    # R12 (РИСК3): встреча в фоновом перевыпуске → откладываем команду (иначе
    # параллельная генерация молча затрёт protocol-файл). Read-only, до ack/генерации.
    if _reissue_guard_blocks(token, chat_id, series, date_str, msg):
        return True

    # У1 (цикл5): дубль команды, пока прошлая ещё в очереди/в работе (queuing может
    # держать job минуты — дольше 30с-окна ниже). Ловим по живому реестру.
    if _command_label_inflight(f"protocol:{series}/{date_str}"):
        send_message(
            token, chat_id,
            f"⏳ Протокол `{series} {date_str}` уже в очереди/собирается — пришлю, как будет готов.",
            reply_to=msg.get("message_id"),
        )
        return True

    # Дедуп: повтор той же команды в окне 30 сек (Wispr Flow диктовка).
    if _protocol_command_is_dupe(series, date_str):
        send_message(
            token, chat_id,
            f"⏳ Уже запустил `{series} {date_str}` меньше 30 сек назад — жду.",
            reply_to=msg.get("message_id"),
        )
        return True

    # Сначала валидируем что transcript есть — иначе ack «✏️ Генерирую…»
    # запутает Илью (получит «работаю» и сразу следом «не нашёл», думая что
    # бот сошёл с ума). Один осмысленный ответ за раз.
    root = _protokol_root()
    if not root.is_dir():
        send_message(
            token, chat_id,
            f"❌ Папка встреч `{root}` не найдена. Проверь MEETING_NOTARY_PROTOCOLS_DIR.",
            reply_to=msg.get("message_id"),
        )
        return True

    # Резолвим transcript: сначала новая структура, потом legacy.
    new_layout = root / series / f"{date_str}.md"
    legacy_layout = root / f"{series}-{date_str}" / f"{date_str}.md"
    if new_layout.is_file():
        transcript_path = new_layout
    elif legacy_layout.is_file():
        transcript_path = legacy_layout
    else:
        send_message(
            token, chat_id,
            f"❌ Транскрипт не найден ни по `{series}/{date_str}.md`, "
            f"ни по `{series}-{date_str}/{date_str}.md`. "
            f"Проверь имя series.",
            reply_to=msg.get("message_id"),
        )
        return True
    protocol_path = transcript_path.parent / f"{date_str}-protokol.md"

    # Транскрипт найден — теперь ack. Sonnet может думать 15-60s, без ack
    # пользователь не понимает, что бот вообще услышал. I7: если воркер занят
    # (reissue/другая команда) — предупреждаем, что встанем в очередь.
    queued = " (в очереди за текущей задачей — пришлю, как освобожусь)" if _executor_busy() else ""
    send_message(
        token, chat_id,
        f"✏️ Генерирую протокол `{series}` `{date_str}`… Sonnet 4.6, обычно 15-60 сек.{queued}",
        reply_to=msg.get("message_id"),
    )

    # Heavy-часть → фоновый воркер (тот же `_reissue_executor`, max_workers=1 → ≤1
    # claude одновременно, I4). job самодостаточен: claude + доставка + ошибки.
    msg_id = msg.get("message_id")
    job_id = _next_command_job_id(f"protocol:{series}/{date_str}")
    _submit_command_job(
        _reissue_executor, _command_inflight, job_id, f"protocol:{series}/{date_str}",
        lambda: _job_protocol_command(
            token, chat_id, msg_id, series, date_str, transcript_path, protocol_path,
        ),
    )
    return True


def maybe_route_to_task_clarify_text(token: str, msg: dict[str, Any]) -> bool:
    """Ф5: если есть pending task_filter / task_deadlines — передаёт msg туда.

    Возвращает True если task_clarify_worker обработал сообщение (caller выходит).
    False — caller продолжает (clarify спикеров / apply_reply).
    """
    pending_root = _clarify_pending_root()
    if pending_root is None or not pending_root.exists():
        return False
    try:
        from notary.lib import task_clarify_worker  # noqa: PLC0415
        if not task_clarify_worker.has_any_pending_task_clarify(pending_root):
            return False
        return task_clarify_worker.process_text_message(msg, pending_root, token)
    except Exception as e:  # noqa: BLE001
        logger.exception("task_clarify text routing failed: %s", e)
        return False


def maybe_route_to_delivery_text(token: str, msg: dict[str, Any]) -> bool:
    """Ф6: текстовый ответ Ильи на «куда отправить протокол».

    Возвращает True если delivery_worker обработал сообщение.
    """
    pending_root = _clarify_pending_root()
    if pending_root is None or not pending_root.exists():
        return False
    try:
        from notary.lib import delivery_worker  # noqa: PLC0415
        if not delivery_worker.has_any_pending_delivery(pending_root):
            return False
        return delivery_worker.process_text_message(msg, pending_root, token)
    except Exception as e:  # noqa: BLE001
        logger.exception("delivery text routing failed: %s", e)
        return False


# Дедуп correction-команд (тот же паттерн что у protocol_command):
# 30-секундное окно по (series, date, instruction-hash). Wispr Flow повторы
# реальны, повторный вызов = повторный subprocess + второй раунд delete/send.
_correction_command_recent: dict[tuple[str, str, str], float] = {}
_CORRECTION_COMMAND_DEDUPE_WINDOW_S = 30.0


def _correction_command_is_dupe(series: str, date_str: str, instruction: str) -> bool:
    """Н7 (цикл5/ход1): окно дедупа сокращено до 5 сек для команд «удали задачу
    N» — после успешного удаления нумерация сдвигается, и Илья может законно
    хотеть удалить «новую задачу 1» сразу. Для structural правок остаётся 30 сек
    (там настоящий duplicate-risk от Wispr Flow повторов).
    """
    import re as _re
    now = time.monotonic()
    is_remove_task = bool(_re.search(r"(?i)\bудали(?:ть)?\s+задачу\s+\d+", instruction))
    window = 5.0 if is_remove_task else _CORRECTION_COMMAND_DEDUPE_WINDOW_S
    key = (series, date_str, instruction[:120])
    stale = [k for k, t in _correction_command_recent.items()
             if now - t > _CORRECTION_COMMAND_DEDUPE_WINDOW_S]
    for k in stale:
        _correction_command_recent.pop(k, None)
    last = _correction_command_recent.get(key)
    if last is not None and now - last < window:
        return True
    _correction_command_recent[key] = now
    return False


def _job_correction_command(
    token: str,
    chat_id: int,
    msg_id: Optional[int],
    series: str,
    date_str: str,
    instruction: str,
    kind: str,
) -> None:
    """И1 (I2): heavy-часть correction-команды — В ФОНОВОМ ВОРКЕРЕ (после ack).

    Самодостаточен (I5): `apply_correction` (claude) → разбор результата → send
    статуса/ошибки (как раньше синхронный хвост route-функции). Реестр не мутирует.

    R9: текст инструкции коррекции/протокола НЕ логируем — только метаданные.
    """
    def _err(text: str) -> None:
        send_message(token, chat_id, text, reply_to=msg_id)

    try:
        from notary.lib.llm_postprocess import apply_correction  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.exception("apply_correction import failed: %s", e)
        _err(f"❌ Не загрузил correction-модуль: {type(e).__name__}")
        return

    try:
        result = apply_correction(
            series=series,
            date=date_str,
            instruction=instruction,
            in_group=True,
            meeting_sid=f"corr-{series}-{date_str}",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("apply_correction failed: %s", e)
        _err(f"❌ Коррекция упала: {type(e).__name__}: {str(e)[:200]}")
        return

    if result.get("status") == "applied":
        # У9 (цикл5/ход3): различаем «применил полностью» vs «применил на
        # диске, в группу пушнуть не смог» — Илье важно понимать что произошло.
        ig = result.get("in_group_action") or "none"
        err = result.get("error")
        version_name = Path(result.get('version_path') or '').name
        if err:
            _err(
                f"⚠️ Применил на диске (version={version_name}, kind={result.get('kind')}), "
                f"в группу пушнуть не смог: {err}. Проверь права бота / переотправь вручную."
            )
        elif ig in ("deleted-old+sent-new", "sent-new-with-warning"):
            _err(
                f"✅ Применил полностью. kind={result.get('kind')} version={version_name} "
                f"group={ig} summary_sent={result.get('summary_sent')}"
            )
        elif ig in ("none-no-binding", "none"):
            _err(
                f"✅ Применил на диске (version={version_name}, kind={result.get('kind')}). "
                f"В группу не пушил (group={ig})."
            )
        else:
            _err(f"✅ Применил. kind={result.get('kind')} version={version_name} group={ig}")
    elif result.get("status") == "file-only":
        _err(f"✅ Применил (file-only). version={Path(result.get('version_path') or '').name}")
    else:
        _err(f"❌ Коррекция не применена: {result.get('error') or 'unknown error'}")


def maybe_route_to_correction_command(token: str, chat_id: int, msg: dict[str, Any]) -> bool:
    """Ф6: команда коррекции протокола от Ильи в DM.

    Должна проверяться ПОСЛЕ `maybe_route_to_protocol_command` (Ф4 имеет
    наивысший приоритет — это явная команда «протокол <series> <date>»).
    Если parse OK — запускаем `apply_correction` (best-effort через
    subprocess чтобы не блокировать listener'а; correction может занять
    минуту-полторы при структурной правке).

    Возвращает True если parse удался — caller выходит из process_message
    без дальнейших попыток роутинга.
    """
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    try:
        from notary.lib.correction_command import parse_correction_command  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.debug("correction_command import failed: %s", e)
        return False
    parsed = parse_correction_command(text)
    if parsed is None:
        return False
    series, date_str, instruction, kind = parsed.series, parsed.date, parsed.instruction, parsed.kind

    # R12 (РИСК3): встреча в фоновом перевыпуске → откладываем коррекцию (иначе
    # apply_correction молча затрёт protocol-файл параллельно с reissue). Read-only.
    if _reissue_guard_blocks(token, chat_id, series, date_str, msg):
        return True

    # У1 (цикл5): дубль коррекции, пока прошлая ещё в очереди/в работе (queuing).
    if _command_label_inflight(f"correction:{series}/{date_str}"):
        send_message(
            token, chat_id,
            f"⏳ Коррекция `{series} {date_str}` уже в очереди/применяется — пришлю результат.",
            reply_to=msg.get("message_id"),
        )
        return True

    if _correction_command_is_dupe(series, date_str, instruction):
        send_message(
            token, chat_id,
            f"⏳ Уже выполняю коррекцию `{series} {date_str}` — жду.",
            reply_to=msg.get("message_id"),
        )
        return True

    # I7: queue-aware ack — если воркер занят (reissue/другая команда), предупреждаем.
    queued = " (в очереди за текущей задачей — пришлю, как освобожусь)" if _executor_busy() else ""
    send_message(
        token, chat_id,
        f"✏️ Применяю коррекцию `{series}` `{date_str}` (kind={kind})…{queued}",
        reply_to=msg.get("message_id"),
    )

    # Heavy-часть (apply_correction → claude → доставка) → фоновый воркер (тот же
    # `_reissue_executor`, ≤1 claude одновременно, I4). job самодостаточен.
    msg_id = msg.get("message_id")
    job_id = _next_command_job_id(f"correction:{series}/{date_str}")
    _submit_command_job(
        _reissue_executor, _command_inflight, job_id, f"correction:{series}/{date_str}",
        lambda: _job_correction_command(
            token, chat_id, msg_id, series, date_str, instruction, kind,
        ),
    )
    return True


# ----- Ф3 (ISS-7/REQ 7.4): команда смены чата серии -----


def _series_display_map() -> dict[str, str]:
    """slug→отображаемое имя из `protocol_to_tg` (хардкод + JSON-конфиг).

    Best-effort: для резолва человекочитаемой ссылки владельца («Директорат» →
    `anzhee-direktorat`) и для подтверждения. Сбой импорта → пустой маппинг
    (резолв по slug всё равно работает)."""
    try:
        from notary.lib import protocol_to_tg  # noqa: PLC0415
        dm = dict(getattr(protocol_to_tg, "_SERIES_DISPLAY_OVERRIDES", {}) or {})
        try:
            dm.update(protocol_to_tg._load_series_display_config() or {})
        except Exception:  # noqa: BLE001
            pass
        return dm
    except Exception:  # noqa: BLE001
        return {}


def _reply_context_series(cid: int, msg: dict[str, Any]) -> Optional[str]:
    """Серия из реплая на доставленный протокол (REQ 7.4, форма «эту серию шли сюда»).

    None, если сообщение не reply на наш протокол или резолв не удался."""
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return None
    try:
        from notary.lib.feedback_worker import find_delivered_protocol  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        meeting = find_delivered_protocol(cid, reply_to.get("message_id"))
    except Exception as e:  # noqa: BLE001
        logger.debug("[set-chat] find_delivered_protocol failed: %s", e)
        return None
    if isinstance(meeting, dict):
        s = meeting.get("series")
        if isinstance(s, str) and s:
            return s
    return None


def _reply_context_series_digest(cid: int, msg: dict[str, Any]) -> Optional[str]:
    """Ф4 (watson, R11/РИСК5): серия из реплая на УТРЕННИЙ ДАЙДЖЕСТ открытых вопросов.

    Штатный `_reply_context_series` знает только сообщения ПРОТОКОЛОВ (индекс
    `meta.delivered`) — id дайджеста там нет, поэтому «сделано» ответом на дайджест
    без этой ветки уехало бы в шлюз правок вместо учёта висяков. Ищем id в sidecar
    отправок серии (`morning-digest.json`, пишет `lib/morning_digest`). Дальше — тот
    же `pending_status_reply`, тот же sidecar статусов Ф2: второго канала статусов нет.
    """
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return None
    try:
        from notary.lib import morning_digest  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — модуль не грузится → фича просто выключена
        logger.debug("[morning-digest] import failed (feature off?): %s", e)
        return None
    try:
        return morning_digest.digest_series_for_message(
            cid, reply_to.get("message_id"), root=_protokol_root(),
        )
    except Exception as e:  # noqa: BLE001 — резолв best-effort, listener не валим
        logger.debug("[morning-digest] резолв серии по reply не удался: %s", type(e).__name__)
        return None


def _series_named_in_text(
    text: str, candidates: list[str], display_map: dict[str, str]
) -> Optional[str]:
    """Серия, названная СЛОВАМИ прямо в просьбе, — ровно одна из `candidates`.

    ISS-52: когда чат обслуживает несколько серий, единственным выходом был
    ответ-реплай на протокол — ровно то, от чего фичу и уводили. Второй путь:
    «пришлите протокол Координация». Сверяем и по slug'у, и по отображаемому
    имени (slug бывает латиницей, человек пишет по-русски). От русских окончаний
    защищаемся срезом хвоста: «координация» → «координац» матчит «координации».

    Совпало больше одной серии (или ни одной) → None, вызыватель переспросит:
    лишний вопрос дешевле, чем чужой протокол в чат. Кандидаты приходят только
    из привязок ЭТОГО чата, так что выбор ограничен сериями самого чата."""
    t = (text or "").casefold().replace("ё", "е")
    hits: list[str] = []
    for s in candidates:
        labels = [s]
        disp = display_map.get(s)
        if isinstance(disp, str) and disp.strip():
            labels.append(disp)
        for label in labels:
            stems = [
                tok[:max(5, len(tok) - 2)]
                for tok in re.split(r"[^0-9a-zа-я]+", label.casefold().replace("ё", "е"))
                if len(tok) >= 5
            ]
            if any(stem in t for stem in stems):
                hits.append(s)
                break
    return hits[0] if len(hits) == 1 else None


def _chat_context_series(cid: int, allowed_chat: int) -> tuple[list[str], Optional[str]]:
    """ISS-52: серии, привязанные к ТЕКУЩЕМУ чату. Фолбэк, когда реплая нет.

    Симптом без этого: человек в группе серии просит «вышли файл с открытыми
    вопросами», не отвечая на протокол, — и бот не знает, в чате какой серии он
    находится, хотя привязка чата к серии лежит в реестре.

    Возвращает `(серии, причина_сбоя)`. Список пуст в трёх случаях:
      • это личка владельца — она не привязана к серии, и вопрос «какую серию»
        там остаётся законным (иначе бот угадывал бы вместо переспроса);
      • чат не привязан ни к одной серии;
      • реестр не прочитался — тогда вторым элементом едет ИМЯ класса ошибки,
        и вызыватель пишет его в лог (иначе «чат без серии» и «реестр упал»
        выглядят в журнале одинаково — то есть никак).

    Несколько серий на один чат — законная ситуация (одна комната обслуживает
    и координацию, и выставку), поэтому список, а решение — за вызывателем.

    Ловим `(Exception, SystemExit)`, а не голый `Exception`: `registry._read_yaml`
    на не-словаре верхнего уровня делает `raise SystemExit` (CLI-происхождение), а
    `SystemExit` — потомок `BaseException` и сквозь `except Exception` пролетает
    насквозь: из роута, из `process_message`, из главного цикла — и гасит процесс
    ДО `save_offset`. Telegram отдал бы тот же апдейт после рестарта → петля
    перезапусков всего бота из-за одной кривой правки watched.yaml руками.
    `BaseException` брать нельзя — проглотили бы KeyboardInterrupt и SIGTERM.
    """
    if cid is None or cid == allowed_chat:
        return [], None
    try:
        from notary.cli import registry  # noqa: PLC0415
        watched = registry.load_watched()
    except (Exception, SystemExit) as e:  # noqa: BLE001 — реестр best-effort, listener не валим
        return [], type(e).__name__
    try:
        return registry.series_for_chat(cid, watched), None
    except (Exception, SystemExit) as e:  # noqa: BLE001
        return [], type(e).__name__


# ── ISS-45: переотправка артефакта (протокол / дайджест) по просьбе участника ──
def _latest_protocol_path(series: str) -> Optional[tuple]:
    """Последний `<date>-protokol.md` серии → (Path, дата). Имена ISO-датой →
    лексикографический max == хронологический. None, если протоколов нет."""
    try:
        from notary.lib import series_memory  # noqa: PLC0415
        series_dir = _protokol_root() / series
        best = None
        for p in series_dir.glob(f"*{series_memory.PROTOCOL_FILE_SUFFIX}"):
            d = series_memory._date_from_protocol_filename(p.name)
            if d and (best is None or d > best[1]):
                best = (p, d)
        return best
    except Exception as e:  # noqa: BLE001
        logger.debug("[send-artifact] поиск протокола не удался: %s", type(e).__name__)
        return None


def _resend_protocol(token: str, cid: int, series: str, msg: dict[str, Any]) -> bool:
    """Переотправить ПОСЛЕДНИЙ протокол серии PDF-вложением. Идемпотентный
    `deliver_protocol` обходим (не трогаем bookkeeping доставки) — читаем md с диска,
    рендерим PDF, шлём напрямую. Опасная тройка: тексты НЕ логируем."""
    reply_to_id = msg.get("message_id")
    found = _latest_protocol_path(series)
    if not found:
        # Протокола ещё нет: встреча прошла, но сборка не завершилась (или это первая
        # встреча серии). Не пугаем «потерялся» — честно про «ещё не готов».
        # Логируем: этот же ответ приходит и при ДРЕЙФЕ ключа серии (реестр говорит
        # «Koord», папка на диске «koord») — тогда бот уверенно врёт, что протокола
        # нет, и без строки в журнале это неотличимо от честного «ещё не собрался».
        logger.info("[send-artifact] протокол серии %s не найден на диске (чат %s)", series, cid)
        send_message(token, cid, "Протокол этой встречи пока не готов — пришлю, как соберётся.",
                     reply_to=reply_to_id)
        return True
    md_path, date = found
    try:
        protocol_text = md_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[send-artifact] чтение протокола не удалось: %s", type(e).__name__)
        send_message(token, cid, "Не смог прочитать файл протокола — гляну, что не так.",
                     reply_to=reply_to_id)
        return True
    try:
        import tempfile  # noqa: PLC0415
        from notary.lib import protocol_to_pdf, protocol_to_tg, telegram_api  # noqa: PLC0415
        meta = {"series": series, "date": date}
        caption = protocol_to_tg.build_pdf_caption(protocol_text, meta)
        title, subtitle = protocol_to_tg.build_pdf_title_subtitle(protocol_text, meta)
        safe_date = re.sub(r"[^0-9A-Za-z._-]", "-", str(date)) or "protokol"
        filename = f"protokol-{safe_date}.pdf"
        with tempfile.TemporaryDirectory(prefix="resend-protokol-") as td:
            pdf_path = Path(td) / filename
            protocol_to_pdf.render_pdf_from_markdown(
                protocol_text, str(pdf_path), title=title, subtitle=subtitle)
            telegram_api.send_document(token, cid, str(pdf_path),
                                       caption=caption, filename=filename)
    except Exception as e:  # noqa: BLE001 — сбой рендера/отправки не валит listener
        logger.error("[send-artifact] переотправка протокола серии %s не удалась: %s",
                     series, type(e).__name__)
        send_message(token, cid, "Не смог собрать/отправить протокол сейчас — гляну, что не так.",
                     reply_to=reply_to_id)
        return True
    logger.info("[send-artifact] протокол серии %s (%s) переотправлен по просьбе", series, date)
    return True


def _resend_digest(token: str, cid: int, series: str, msg: dict[str, Any]) -> bool:
    """Переотправить дайджест открытых вопросов серии по просьбе. Через
    `send_series_digest(force=True)` — обходит только `already_sent`, слепок нумерации
    пишется (ordinal-ответы на переотправленный дайджест работают). Тексты НЕ логируем."""
    reply_to_id = msg.get("message_id")
    try:
        from notary.lib import morning_digest  # noqa: PLC0415
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        res = morning_digest.send_series_digest(series, date=today, force=True)
    except Exception as e:  # noqa: BLE001
        logger.error("[send-artifact] переотправка дайджеста серии %s не удалась: %s",
                     series, type(e).__name__)
        send_message(token, cid, "Не смог собрать открытые вопросы сейчас — гляну, что не так.",
                     reply_to=reply_to_id)
        return True
    status, reason = res.get("status"), res.get("reason")
    if status == "sent":
        logger.info("[send-artifact] дайджест серии %s переотправлен по просьбе", series)
        # Дайджест уходит в ПРИВЯЗАННЫЙ чат серии (штатный канал). Если просьбу дали в
        # другом чате («серию X шли сюда» развёл источник и канал) — не молчим в источнике.
        sent_chat = res.get("chat_id")
        try:
            if sent_chat is not None and int(sent_chat) != int(cid):
                send_message(token, cid, "Прислал открытые вопросы в чат этой серии.",
                             reply_to=reply_to_id)
        except (TypeError, ValueError):
            pass
        return True
    if reason == "empty":
        # Логируем: ровно этот ответ бот даёт и при ДРЕЙФЕ ключа серии (реестр
        # разошёлся с именем папки — корень ISS-7). Тогда людям в группе уверенно
        # сообщается «вопросов нет», хотя они есть; без строки в журнале отличить
        # это от честной пустоты нельзя.
        logger.info("[send-artifact] открытых вопросов по серии %s нет (чат %s)", series, cid)
        send_message(token, cid, "Открытых вопросов по этой встрече сейчас нет — список пуст.",
                     reply_to=reply_to_id)
        return True
    # disabled / no-chat / send-failed / state-write-failed — не молчим ложно.
    logger.warning("[send-artifact] дайджест серии %s не ушёл: reason=%s", series, reason)
    send_message(token, cid, "Не смог прислать открытые вопросы сейчас — гляну, что не так.",
                 reply_to=reply_to_id)
    return True


def maybe_route_to_send_artifact(
    token: str, cid: Optional[int], allowed_chat: int, msg: dict[str, Any]
) -> bool:
    """ISS-45: просьба «пришли протокол / открытые вопросы» → переотправить нужный
    артефакт (вместо глухого «🤔 не понял»). Стоит ПОСЛЕ ack-роута и ДО digest-роута —
    иначе digest-роут проглотил бы reply на дайджест первым.

    ISS-51: понимание просьбы больше не упирается в список глаголов («вышли» боту не
    понимал, «пришли» понимал) — разбор внутри `send_artifact` стал ИИ-первым под
    флагом `ENABLE_SEND_ARTIFACT_AI` (дефолт-OFF, регулярка — быстрый путь и офлайн-
    фолбэк). Защита от перехвата от этого не ослабла, а усилилась: статус-разговор и
    правки отсекаются ДО разбора (модель на них не зовётся), позиция роута в цепочке
    прежняя (после ack-, до digest-роута; закреплена тестом позиции).

    РЕМОНТ Ф6. Гейт «не крадём чужое» перестал быть безусловным. Он спрашивает не
    только «похоже ли это на статус/правку», но и «ЕСТЬ ЛИ КОМУ уступить»: все
    роуты ниже (digest / mixed / status / шлюз правок) требуют ответа-реплая, а без
    реплая уступка означает не «сообщение обработает кто-то другой», а «сообщение
    исчезло молча». Прогон ревью-2: 6 из 10 живых просьб («вышли открытые вопросы,
    а то я не помню, что там закрыто») гасились именно так — для владельца это
    неотличимо от исходной жалобы ISS-51.

    Правило теперь:
      • ЛЮБАЯ правка текста (даже одиночная — «поправь фамилию в протоколе») →
        уступаем всегда: потерянная правка дороже неприсланного файла (ревью-1 🔴-2,
        класс инцидента 20.07);
      • статус-разговор (тем же предикатом `_looks_like_status_talk`, что у Ф3 —
        шире `detect_status_intent`, ловит и «закрой вопрос по смете») → уступаем,
        если выполнено ЛЮБОЕ из трёх: человек не просит файл; это реплай (там
        статус-роут ниже реально возьмётся, и запись статуса важнее переприсылки);
        статус ДОЛОЖЕН, а не упомянут попутно («пришли протокол — по второй задаче
        всё сделано» — отчёт, его терять нельзя);
      • просьба с ПОПУТНЫМ упоминанием статуса и БЕЗ реплая → это просьба
        («пришли протокол, я не помню, что мы решили»).
    Каждый отказ пишет строку в журнал (раньше сообщение исчезало без следа)."""
    if cid is None:
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    # Серия + surface по тому, на ЧТО ответили (протокол приоритетнее — точный индекс).
    is_reply = isinstance(msg.get("reply_to_message"), dict)
    series = _reply_context_series(cid, msg) if is_reply else None
    surface = "protocol" if series else None
    if not series and is_reply:
        series = _reply_context_series_digest(cid, msg)
        surface = "digest" if series else None
    # ГЕЙТ «не крадём чужое». Всё детерминированно: без сети и без чтения диска.
    # ISS-51 (риск 4 плана) поднял его ВЫШЕ разбора — разбор стал ИИ-первым и по
    # определению шире регулярки, а самый надёжный способ не дать модели украсть
    # статус или правку — не звать её на них вовсе (заодно ноль лишних вызовов).
    try:
        from notary.lib import pending_status_reply, send_artifact  # noqa: PLC0415
        asks = send_artifact.looks_like_request(text)
        if (send_artifact.looks_like_edit_request(text)
                or pending_status_reply.looks_edit_heavy(text)):
            logger.info("[send-artifact] уступаю шлюзу правок (reply=%s): правка текста",
                        is_reply)
            return False
        if _looks_like_status_talk(text) and (
                is_reply or not asks
                or not send_artifact.status_mention_is_incidental(text)):
            logger.info("[send-artifact] уступаю статус-роутам "
                        "(reply=%s, просьба=%s, статус-попутно=%s)",
                        is_reply, asks,
                        send_artifact.status_mention_is_incidental(text))
            return False
    except Exception:  # noqa: BLE001
        pass
    # Дешёвый пред-чек ДО чтения реестра и ДО разбора: только регулярки. Пропустил —
    # значит фраза в принципе может закончиться отправкой, и дальше не жалко ни YAML,
    # ни (возможного) вызова модели.
    try:
        from notary.lib import send_artifact  # noqa: PLC0415
        # `allow_bare_file=is_reply`: голый объект «файл/версия/документ» — интент
        # ТОЛЬКО в ответе на наше сообщение. Без реплая (ISS-52 берёт серию из
        # привязки чата) такие фразы — обычная переписка людей между собой
        # («пришли файл с ценами», «дайте актуальную версию договора»), и бот
        # вываливал бы им в чат PDF протокола встречи.
        if not send_artifact.may_be_request(
                text, reply_surface=surface, allow_bare_file=is_reply):
            return False
    except Exception as e:  # noqa: BLE001
        logger.debug("[send-artifact] пред-чек не удался: %s", type(e).__name__)
        return False
    # ISS-52: реплая нет или он ничего не дал → определяем серию по САМОМУ ЧАТУ.
    # Реестр читаем ЗДЕСЬ — после дешёвого пред-чека и ДО дорогого разбора. Так YAML
    # не парсится на каждом «привет» (площадь отказа реестра не равна групповому
    # трафику), и одновременно вызов модели не тратится в чате, к которому не
    # привязана ни одна серия: там исход предрешён кодом (ревью-2 🟡-3).
    # Surface остаётся None: без реплая нечем разрешить неоднозначное «пришли
    # файл», и пред-чек такую фразу уже отсёк (`allow_bare_file`).
    ambiguous_series: list[str] = []
    if not series:
        chat_series, err = _chat_context_series(cid, allowed_chat)
        if err:
            # Единственный сигнал, что фича отвалилась не по-настоящему, а из-за
            # реестра: без него «чат не привязан» и «реестр упал» неотличимы.
            logger.warning("[chat-series] реестр не прочитан (chat=%s): %s", cid, err)
        if len(chat_series) == 1:
            series = chat_series[0]
            logger.info("[chat-series] серия %s определена по привязке чата %s (кандидатов %d)",
                        series, cid, len(chat_series))
        elif len(chat_series) > 1:
            ambiguous_series = chat_series
    if not series and not ambiguous_series and not is_reply:
        # Реплая нет и чат ни с одной серией не связан (в том числе личка владельца):
        # ответить всё равно нечем — молчим и уступаем роутам ниже, ровно как до
        # ISS-52. Разбор (и возможный вызов модели) тут уже не нужен: исход False
        # при любом его результате.
        return False
    # РАЗБОР: быстрый путь (регулярка ISS-45) + ИИ-ярус под флагом
    # `ENABLE_SEND_ARTIFACT_AI` (дефолт-OFF) + верификация артефакта кодом.
    try:
        from notary.lib import send_artifact  # noqa: PLC0415
        kind = send_artifact.detect_artifact_request(
            text, reply_surface=surface, allow_bare_file=is_reply)
    except Exception as e:  # noqa: BLE001
        logger.debug("[send-artifact] детект не удался: %s", type(e).__name__)
        return False
    if not kind:
        return False
    if not series and ambiguous_series:
        # ISS-52: чат обслуживает несколько серий. Сначала пробуем понять по словам
        # самой просьбы («пришлите протокол Координация»), и только потом
        # переспрашиваем — реплай тут не единственный выход.
        dm = _series_display_map()
        series = _series_named_in_text(text, ambiguous_series, dm)
        if series:
            logger.info("[chat-series] серия %s названа словами в просьбе (чат %s, кандидатов %d)",
                        series, cid, len(ambiguous_series))
        else:
            logger.info("[chat-series] в чате %s несколько серий (%d) — переспрашиваю: %s",
                        cid, len(ambiguous_series), ", ".join(ambiguous_series))
            names = ", ".join(f"«{dm.get(s, s)}»" for s in ambiguous_series)
            example = dm.get(ambiguous_series[0], ambiguous_series[0])
            send_message(token, cid,
                         f"В этом чате у меня несколько серий: {names}. "
                         f"Назовите серию прямо в просьбе — например «пришлите протокол "
                         f"{example}» — или ответьте на протокол либо дайджест нужной встречи.",
                         reply_to=msg.get("message_id"))
            return True
    if not series:
        if not is_reply:
            # Страховка: случай «нет реплая и нет серии» отсечён выше, ДО разбора
            # (там же объяснено почему). Сюда попасть можно только если резолв
            # серии выше изменят — тогда поведение остаётся прежним: молчим.
            return False
        send_message(token, cid,
                     "Чтобы прислать файл, ответьте на дайджест или протокол нужной встречи "
                     "— по нему я пойму, о какой серии речь.",
                     reply_to=msg.get("message_id"))
        return True
    if kind == "digest":
        return _resend_digest(token, cid, series, msg)
    return _resend_protocol(token, cid, series, msg)


def maybe_route_to_set_chat_command(
    token: str, cid: Optional[int], allowed_chat: int, msg: dict[str, Any]
) -> bool:
    """ISS-7/REQ 7.4: команда владельца «серию X шли сюда / в личку / в чат N».

    Переназначает серию→чат в watched.yaml; применяется к будущим протоколам;
    повторная команда перезаписывает. Вызывается ДО DM-гейта (работает и в
    группе, и в личке) — поэтому owner-гейт внутри: в группе применяет ТОЛЬКО
    если отправитель = владелец (его user_id == allowed_chat; в приватном чате
    chat.id == user.id). Узкий триггер парсера (серию+глагол+цель) + owner-гейт
    → обычную переписку не перехватывает.

    True = распознано и дана ВИДИМАЯ реакция (R-REPLY: «✅ принял» / «не нашёл») —
    caller выходит. False = не наша команда / не владелец → обычный dispatch.

    R9 («Опасная тройка»): текст команды/протокола НЕ логируем — только
    метаданные (серия / chat_id).
    """
    if cid is None:
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    try:
        from notary.lib.correction_command import parse_correction_command  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.debug("[set-chat] correction_command import failed: %s", e)
        return False
    parsed = parse_correction_command(text)
    if parsed is None or parsed.kind != "set_chat":
        return False

    # Owner-гейт: в группе — только владелец (его user_id == allowed_chat).
    from_id = (msg.get("from") or {}).get("id")
    is_dm = cid == allowed_chat
    if not is_dm and from_id != allowed_chat:
        logger.info(
            "[set-chat] не владелец (from=%s chat=%s) — команда смены чата пропущена",
            from_id, cid,
        )
        return False

    msg_id = msg.get("message_id")

    # Целевой chat_id: «сюда»→текущий чат, «в личку»→владелец, иначе явный id.
    if parsed.target == "here":
        target_chat: Optional[int] = cid
    elif parsed.target == "dm":
        target_chat = allowed_chat
    else:
        try:
            target_chat = int(parsed.target)
        except (ValueError, TypeError):
            send_message(
                token, cid,
                "🤔 Не понял, в какой чат слать. Скажи «…шли сюда», «…в личку» или «…в чат <id>».",
                reply_to=msg_id,
            )
            return True

    # Серия: явная ссылка ИЛИ из реплая на доставленный протокол.
    series_ref = (parsed.series or "").strip()
    reply_slug: Optional[str] = None
    if not series_ref:
        reply_slug = _reply_context_series(cid, msg)
        if reply_slug is None:
            send_message(
                token, cid,
                "🤔 Не понял, какую серию переназначить. Скажи «серию <имя> шли сюда» "
                "или ответь этой командой на доставленный протокол серии.",
                reply_to=msg_id,
            )
            return True

    try:
        from notary.cli.registry import (  # noqa: PLC0415
            load_watched, save_watched, set_telegram_chat_id_for_series,
            release_watched_lock, resolve_series_ref,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[set-chat] registry import failed: %s", e)
        send_message(
            token, cid,
            "⚠️ Не смог открыть реестр серий — привязку не сохранил. Попробуй ещё раз.",
            reply_to=msg_id,
        )
        return True

    display_map = _series_display_map()
    try:
        watched = load_watched(lock=True)
    except (Exception, SystemExit) as e:  # noqa: BLE001
        # ВАЖНО: load_watched(lock=True) захватывает flock ДО чтения yaml; если
        # сам разбор реестра упал (битый/руками-правленный watched.yaml), лок уже
        # держится и без явного release утечёт — следующая команда смены чата
        # повиснет на flock(LOCK_EX) и встанет весь листенер. Освобождаем явно.
        # `SystemExit` ловим наравне с Exception: `_read_yaml` на не-словаре
        # верхнего уровня кидает именно его (CLI-происхождение), а он потомок
        # BaseException — иначе пролетел бы мимо, унёс листенер И оставил лок
        # захваченным навсегда. BaseException нельзя: SIGTERM/KeyboardInterrupt.
        try:
            release_watched_lock()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[set-chat] load_watched lock failed: %s", e)
        send_message(
            token, cid,
            "⚠️ Реестр серий занят — не сохранил привязку. Попробуй ещё раз.",
            reply_to=msg_id,
        )
        return True

    saved = False
    try:
        ref_for_resolve = reply_slug if reply_slug is not None else series_ref
        slug = resolve_series_ref(ref_for_resolve, watched, display_map=display_map)
        if slug is None and reply_slug is not None:
            # Серия из реплая может быть one-off вне watched — попробуем как есть.
            slug = reply_slug if any(
                w.get("series") == reply_slug for w in watched.get("watched", [])
            ) else None
        if slug is None:
            known = sorted({
                w.get("series") for w in watched.get("watched", [])
                if isinstance(w.get("series"), str) and w.get("series")
            })
            known_str = ", ".join(known) if known else "—"
            send_message(
                token, cid,
                f"🤔 Не нашёл серию «{ref_for_resolve}» в реестре. Известные серии: "
                f"{known_str}. Уточни slug или имя.",
                reply_to=msg_id,
            )
            logger.info("[set-chat] серия не найдена в реестре (chat=%s)", cid)
            return True
        n = set_telegram_chat_id_for_series(slug, target_chat, watched)
        if n == 0:
            send_message(
                token, cid,
                f"🤔 Серия «{slug}» есть, но без активной записи для привязки. "
                "Проверь watched.yaml.",
                reply_to=msg_id,
            )
            logger.warning("[set-chat] set вернул 0 для slug=%s (chat=%s)", slug, cid)
            return True
        save_watched(watched)
        saved = True
    except Exception as e:  # noqa: BLE001
        logger.warning("[set-chat] запись привязки упала: %s", e)
        send_message(
            token, cid,
            "⚠️ Не смог сохранить привязку серии. Попробуй ещё раз.",
            reply_to=msg_id,
        )
        return True
    finally:
        if not saved:
            try:
                release_watched_lock()
            except Exception:  # noqa: BLE001
                pass

    display = display_map.get(slug, slug)
    if parsed.target == "dm" or target_chat == allowed_chat:
        where = "в личку"
    elif parsed.target == "here" or target_chat == cid:
        where = "в этот чат"
    else:
        where = f"в чат {target_chat}"
    send_message(
        token, cid,
        f"✅ Принял. Серия «{display}» теперь идёт {where}. Будущие протоколы — туда.",
        reply_to=msg_id,
    )
    logger.info(
        "[set-chat] привязка обновлена: series=%s → chat=%s (записей=%d, by_owner)",
        slug, target_chat, n,
    )
    return True


# ----- Ф2 (ISS-51…55): переключение режима открытых вопросов серии -----

# Тексты вынесены константами: их читает владелец и люди в группе, и они же
# проверяются тестами дословно. Формулировки — из плана (пункты 3 и 5 Фазы 2) плюс
# решение владельца 7 (возврат в накопление начинает счёт с чистого листа) —
# подтверждение обязано об этом честно сказать, иначе через месяц владельца ждёт
# сюрприз «а где мои старые вопросы» (РИСК6 плана).
_OQ_MODE_CONFIRM = {
    "last-only": (
        "✅ Принято: серия «{display}» теперь в режиме шпаргалки — с ближайшей "
        "встречи вопросы копиться не будут. Записанное никуда не делось, я просто "
        "перестаю его показывать."
    ),
    "carry": (
        "✅ Принято: серия «{display}» теперь в режиме накопления — с ближайшей "
        "встречи вопросы снова копятся и переносятся из встречи во встречу. Счёт "
        "идёт с чистого листа: то, что копилось до переключения, я не поднимаю."
    ),
}

# Строка в чат самой серии: там сидят люди, у которых иначе висяки молча
# исчезнут из вида (или наоборот — внезапно вернутся). Пункт 5 плана.
_OQ_MODE_SERIES_NOTICE = {
    "last-only": (
        "⚙️ Формат к следующей встрече меняется: открытые вопросы по этой серии "
        "больше не копятся — перед встречей пришлю справку по прошлой встрече, "
        "отвечать на неё не нужно."
    ),
    "carry": (
        "⚙️ Формат к следующей встрече меняется: открытые вопросы по этой серии "
        "снова копятся и переносятся из встречи во встречу — незакрытые буду "
        "показывать, пока их не закроют. Счёт начинается заново, старые вопросы "
        "не поднимаю."
    ),
}

_OQ_MODE_STATE = {
    "carry": (
        "Серия «{display}» сейчас в режиме накопления: незакрытые вопросы "
        "переносятся из встречи во встречу, утром прихожу с дайджестом и прошу "
        "статусы."
    ),
    "last-only": (
        "Серия «{display}» сейчас в режиме шпаргалки: перед встречей присылаю "
        "только то, что зафиксировали на прошлой, без статусов и без переноса."
    ),
}

# Подсказка «как переключить» дописывается ТОЛЬКО владельцу: участнику группы
# переключать нечего (owner-гейт молча его проигнорирует), и бот, обучающий
# команде, которая для человека не работает, выглядит сломанным.
_OQ_MODE_HOWTO = {
    "carry": " Переключить: «режим шпаргалка».",
    "last-only": " Переключить: «режим накопление».",
}

_OQ_MODE_ALREADY = {
    "carry": "Серия «{display}» и так в режиме накопления — ничего не менял.",
    "last-only": "Серия «{display}» и так в режиме шпаргалки — ничего не менял.",
}

# Тот же режим, но в реестре он не был записан явно (или записи серии разошлись
# между собой) — команду мы всё равно исполняем: записываем режим во все записи.
# Иначе «выбрано шпаргалкой» и «никто не выбирал» в файле неразличимы, и смена
# дефолта однажды молча перевернула бы все такие серии.
_OQ_MODE_ALREADY_FIXED = {
    "carry": ("Серия «{display}» и так в режиме накопления — записал это явно в "
              "реестр, формат встреч не меняется, участников не беспокоил."),
    "last-only": ("Серия «{display}» и так в режиме шпаргалки — записал это явно в "
                  "реестр, формат встреч не меняется, участников не беспокоил."),
}

_OQ_MODE_ASK_SERIES = (
    "🤔 Не понял, о какой серии речь. Напиши «режим шпаргалка <имя серии>» "
    "(или «режим накопление <имя серии>»), либо дай команду в чате нужной серии."
)

# Один текст на все беды с файлом реестра. Прежний «Реестр серий занят» врал:
# он выдаётся не только на занятый лок, но и на битый руками YAML — и владелец
# уходил «пробовать ещё раз» вместо того, чтобы починить файл.
_OQ_MODE_REGISTRY_UNREADABLE = (
    "⚠️ Не смог прочитать реестр серий — режим не поменял. Либо файл сейчас "
    "занят (попробуй ещё раз), либо watched.yaml повреждён — загляни в него."
)


def _oq_carry_since() -> str:
    """Дата отсечки «счёт с чистого листа» — сегодня в поясе выжимок серии (МСК).

    Сравнивается с датами выжимок (`<YYYY-MM-DD>-memory.json`), а те датируются
    встречей по МСК (`morning_digest.MSK`). Считать «сегодня» по UTC или по
    локальному поясу машины значило бы промахиваться на день в обе стороны —
    на границе суток отсечка либо срезала бы лишнюю встречу, либо пропускала
    ком, который обещали не поднимать.
    """
    from datetime import timedelta  # noqa: PLC0415 — точечно, как соседи

    return datetime.now(timezone(timedelta(hours=3))).date().isoformat()


def _oq_effective_mode(slug: str, watched: dict[str, Any]) -> str:
    """Режим, в котором бот РЕАЛЬНО себя ведёт по этой серии (ISS-51).

    С буквальным резолвом реестра расходится в двух случаях: поле задано, но не
    читается (опечатка «карри» — ровно то, что порождает ручная раскладка
    режимов), либо другая запись серии явно просит накопление. Боевой резолвер
    (`series_memory.open_questions_mode`) в обоих остаётся в `carry`, чтобы не
    потерять хвост: он живёт только в разделе протокола, и один протокол без
    раздела обнуляет его навсегда.

    ОДНА точка на ОБЕ ветки команды. Пока fail-safe висел только на ветке
    «вопрос», ветка «команда» считала текущий режим по букве реестра: на реестре
    с опечаткой «режим шпаргалка» рапортовала «и так в режиме шпаргалки — формат
    встреч не меняется, участников не беспокоил», хотя серия при этом реально
    переезжала из накопления в справку, а люди в чате оставались не предупреждены.

    Исключения НЕ глотаем: сбой разбора реестра обязан долететь до `except`
    вызывающей ветки — человек получает ответ, а не тишину.
    """
    from notary.cli.registry import (  # noqa: PLC0415
        series_open_questions_mode, series_records,
    )
    from notary.lib import series_memory  # noqa: PLC0415

    mode = series_open_questions_mode(slug, watched)
    if mode == series_memory.OPEN_QUESTIONS_MODE_LAST_ONLY and \
            series_memory._explicit_carry_signal(series_records(slug, watched)):
        return series_memory.OPEN_QUESTIONS_MODE_CARRY
    return mode


def _oq_mode_series_hint(
    token: str, cid: int, allowed_chat: int, text: str, msg: dict[str, Any],
    display_map: dict[str, str],
) -> tuple[Optional[str], bool]:
    """Какую серию имел в виду владелец: реплай → привязка чата → переспрос.

    Возвращает `(ссылка_на_серию, ответил_ли_уже)`. Второй флаг = True означает,
    что человеку уже отправлен переспрос и роут должен выйти с True (видимая
    реакция вместо молчания — R-REPLY).

    Явная ссылка в самой команде разбирается ДО этого хелпера и сюда не доходит:
    названная серия всегда главнее контекста (владелец в личке переключает чужую
    серию по имени, и угадывать вместо него нельзя).
    """
    series = _reply_context_series(cid, msg) or _reply_context_series_digest(cid, msg)
    if series:
        return series, False
    chat_series, err = _chat_context_series(cid, allowed_chat)
    if err:
        # Единственный сигнал, что серия не нашлась не по-настоящему, а из-за
        # реестра: иначе «чат не привязан» и «реестр упал» в журнале одинаковы.
        logger.warning("[oq-mode] реестр не прочитан (chat=%s): %s", cid, err)
    if len(chat_series) == 1:
        logger.info("[oq-mode] серия %s определена по привязке чата %s",
                    chat_series[0], cid)
        return chat_series[0], False
    if len(chat_series) > 1:
        named = _series_named_in_text(text, chat_series, display_map)
        if named:
            return named, False
        logger.info("[oq-mode] в чате %s несколько серий (%d) — переспрашиваю",
                    cid, len(chat_series))
        names = ", ".join(f"«{display_map.get(s, s)}»" for s in chat_series)
        example = display_map.get(chat_series[0], chat_series[0])
        send_message(
            token, cid,
            f"В этом чате у меня несколько серий: {names}. Назовите серию прямо в "
            f"команде — например «режим шпаргалка {example}».",
            reply_to=msg.get("message_id"),
        )
        return None, True
    return None, False


def maybe_route_to_mode_command(
    token: str, cid: Optional[int], allowed_chat: int, msg: dict[str, Any]
) -> bool:
    """Ф2 (ISS-51…55): «режим шпаргалка / режим накопление» и «режим» (показать).

    Стоит СРАЗУ ПОСЛЕ команды смены чата и по тем же причинам: это настройка
    СЕРИИ, её дают прямо в групповом чате серии (cid != allowed_chat), а шлюз
    правок ниже прожевал бы любое не-reply сообщение в группе. Порядок роутов
    ниже не меняется — новый роут только вставлен между двумя существующими.

    Взаимного перехвата с командой смены чата нет: у той обязательна ЦЕЛЬ
    («сюда» / «в личку» / «в чат N»), у этой сообщение обязано СОВПАСТЬ ЦЕЛИКОМ
    с одной из форм закрытого списка (`lib/mode_command`).

    Owner-гейт как у `maybe_route_to_set_chat_command`: в группе режим меняет
    только владелец (его user_id == allowed_chat; в личке chat.id == user.id).
    Не владелец → False (лог + обычный dispatch), а НЕ отказ-сообщение: бот,
    читающий нотацию участникам группы, хуже молчания. Вопрос о режиме («режим»)
    гейта не имеет: он ничего не меняет, а люди в чате — ровно те, кого режим
    касается.

    Серия не опозналась → в ГРУППЕ роут возвращает False и молчит (сообщение
    достаётся роутам ниже, список внутренних имён серий в комнату с участниками
    встреч не попадает НИКОГДА). Видимый отказ со списком известных серий —
    только в личке владельца, где он этот список и набирает.

    True = распознано и дана ВИДИМАЯ реакция (R-REPLY: подтверждение / переспрос /
    «не нашёл») — caller выходит. False = не наша команда / не владелец / серия
    не опознана в групповом чате.

    R9 («Опасная тройка»): тексты сообщений и содержимое встреч НЕ логируем —
    только серия, режимы и chat_id.
    """
    if cid is None:
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    try:
        from notary.lib.mode_command import parse_mode_command  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        # WARNING, не debug: уровень листенера — INFO, и на `debug` пропажа
        # нового модуля (самый вероятный сбой деплоя) осталась бы вообще без
        # следа в журнале — фича молча мертва.
        logger.warning("[oq-mode] mode_command import failed: %s", e)
        return False
    parsed = parse_mode_command(text)
    if parsed is None:
        return False

    from_id = (msg.get("from") or {}).get("id")
    is_dm = cid == allowed_chat
    is_owner = is_dm or from_id == allowed_chat
    # Владелец И в личке — единственное место, где бот вправе назвать внутренние
    # имена серий (см. `_oq_mode_unresolved`).
    owner_dm = is_owner and is_dm
    series_ref = (parsed.series_ref or "").strip()
    if not is_owner and (parsed.action == "set" or series_ref):
        # Менять режим может только владелец. Спрашивать про ЧУЖУЮ серию по имени —
        # тоже: иначе любой посторонний, написавший боту в личку, вытягивал бы
        # режимы и (через «не нашёл серию») список внутренних имён всех серий.
        # Вопрос про серию СВОЕГО чата участнику оставлен — режим виден ему и так,
        # по формату встреч.
        logger.info(
            "[oq-mode] не владелец (from=%s chat=%s, action=%s) — команда не применена",
            from_id, cid, parsed.action,
        )
        return False

    msg_id = msg.get("message_id")
    display_map = _series_display_map()
    if not series_ref:
        series_ref, answered = _oq_mode_series_hint(
            token, cid, allowed_chat, text, msg, display_map)
        if answered:
            return True
        if not series_ref:
            send_message(token, cid, _OQ_MODE_ASK_SERIES, reply_to=msg_id)
            return True

    try:
        from notary.cli.registry import (  # noqa: PLC0415
            get_telegram_chat_id_for_series, load_watched, release_watched_lock,
            resolve_series_ref, save_watched, series_record_is_live, series_records,
            set_open_questions_mode_for_series,
        )
        # Текущий режим обе ветки спрашивают у `_oq_effective_mode` (он и импортирует
        # резолверы) — одним ответом на «вопрос» и на «команду».
    except Exception as e:  # noqa: BLE001
        logger.warning("[oq-mode] registry import failed: %s", e)
        send_message(token, cid, "⚠️ Не смог открыть реестр серий — режим не тронул. "
                                 "Попробуй ещё раз.", reply_to=msg_id)
        return True

    # Вопрос о режиме ничего не пишет — читаем БЕЗ flock'а: держать
    # эксклюзивный лок реестра ради ответа «сейчас шпаргалка» незачем.
    if parsed.action == "query":
        try:
            watched = load_watched()
        except (Exception, SystemExit) as e:  # noqa: BLE001 — SystemExit из _read_yaml
            logger.warning("[oq-mode] load_watched (query) упал: %s", type(e).__name__)
            send_message(token, cid, _OQ_MODE_REGISTRY_UNREADABLE, reply_to=msg_id)
            return True
        # Разбор реестра — под тем же `try`, что и загрузка (симметрично
        # set-ветке): кривая запись в watched.yaml роняла резолв, исключение
        # улетало из роута, человек не получал НИЧЕГО, а сообщение не доходило
        # до роутов ниже. Достижимо любым участником группы — query без гейта.
        try:
            slug = resolve_series_ref(series_ref, watched, display_map=display_map)
            if slug is None:
                return _oq_mode_unresolved(token, cid, msg_id, series_ref, watched,
                                           owner_dm=owner_dm, action="query")
            # ISS-51 (Ф3): отвечаем тем режимом, в котором бот РЕАЛЬНО себя ведёт
            # (fail-safe при опечатке в реестре — см. `_oq_effective_mode`). Тот же
            # вызов стоит в ветке «команда»: два разных ответа одной команды —
            # ровно тот шов, на котором она врала про смену формата.
            mode = _oq_effective_mode(slug, watched)
            answer = _OQ_MODE_STATE[mode].format(display=display_map.get(slug, slug))
            if is_owner:
                answer += _OQ_MODE_HOWTO[mode]
        except Exception as e:  # noqa: BLE001
            logger.warning("[oq-mode] разбор реестра (query) упал: %s", type(e).__name__)
            send_message(token, cid, _OQ_MODE_REGISTRY_UNREADABLE, reply_to=msg_id)
            return True
        send_message(token, cid, answer, reply_to=msg_id)
        logger.info("[oq-mode] режим серии %s запрошен: %s (chat=%s)", slug, mode, cid)
        return True

    # Смена режима — под flock'ом. `load_watched(lock=True)` берёт лок ДО разбора
    # YAML: если разбор упал (битый реестр правили руками), лок уже держится и без
    # явного release утечёт — следующая команда повиснет на flock(LOCK_EX) и встанет
    # весь листенер. `SystemExit` ловим наравне с Exception: `_read_yaml` кидает
    # именно его, а он потомок BaseException и сквозь `except Exception` пролетел
    # бы насквозь — унёс процесс ДО save_offset (петля перезапусков бота).
    try:
        watched = load_watched(lock=True)
    except (Exception, SystemExit) as e:  # noqa: BLE001
        try:
            release_watched_lock()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[oq-mode] load_watched(lock) упал: %s", type(e).__name__)
        send_message(token, cid, _OQ_MODE_REGISTRY_UNREADABLE, reply_to=msg_id)
        return True

    saved = False
    try:
        slug = resolve_series_ref(series_ref, watched, display_map=display_map)
        recs = series_records(slug, watched) if slug else []
        if not recs:
            # Пустой `recs` при найденном slug по построению недостижим
            # (`resolve_series_ref` возвращает только slug, физически лежащий в
            # watched, а матч ключа у обоих один и тот же — `series_records`).
            # Обрабатываем как «серию не опознал»: подтвердить запись, которой
            # не было, хуже, чем переспросить.
            return _oq_mode_unresolved(token, cid, msg_id, series_ref, watched,
                                       owner_dm=owner_dm, action="set")
        display = display_map.get(slug, slug)
        # Тем же путём, что и ветка «вопрос»: «какой режим сейчас» обязано быть
        # одним ответом на обе ветки команды, иначе бот рапортует «формат не
        # меняется», реально переводя серию из накопления в справку.
        old_mode = _oq_effective_mode(slug, watched)
        same = old_mode == parsed.mode
        values = [r.get("open_questions_mode") for r in recs]
        if same and all(v == parsed.mode for v in values):
            # Только здесь ничего не пишем: реестр уже говорит ровно то, что
            # просят, во ВСЕХ записях серии.
            send_message(token, cid, _OQ_MODE_ALREADY[old_mode].format(display=display),
                         reply_to=msg_id)
            logger.info("[oq-mode] серия %s уже в режиме %s во всех записях (%d) — "
                        "записи нет", slug, old_mode, len(recs))
            return True
        # Иначе пишем ВСЕГДА — даже когда режим «тот же». Причины две:
        #  • первая команда «режим шпаргалка» при дефолте last-only иначе была бы
        #    немым no-op: в файле не появлялось ничего, и «выбрано» было бы не
        #    отличить от «никто не выбирал» (на бою поле не проставлено ни у
        #    одной из 16 серий — смена дефолта молча перевернула бы их все);
        #  • записи серии могли разойтись между собой, и короткое замыкание
        #    «и так в этом режиме» до записи означало, что расхождение не
        #    лечится никогда.
        # Решение владельца 7: возврат в накопление начинает счёт С ЧИСТОГО
        # ЛИСТА — ровно это подтверждение обещает владельцу, а строка в чат
        # серии обещает всем участникам. Обещание было только текстом: «чистый
        # лист» получался побочно (протокол шпаргалки идёт без раздела хвоста) и
        # разваливался, если между переключениями не было ни одной встречи.
        # Отсечку ставим ТОЛЬКО на реальном переезде шпаргалка → накопление:
        # повтор команды по серии, которая и так в накоплении, хвост не трогает.
        carry_since = _oq_carry_since() if (not same and parsed.mode == "carry") else None
        n = set_open_questions_mode_for_series(
            slug, parsed.mode, watched, carry_since=carry_since)
        series_chat = get_telegram_chat_id_for_series(slug, watched)
        series_live = any(series_record_is_live(r) for r in recs)
        save_watched(watched)
        saved = True
    except Exception as e:  # noqa: BLE001
        logger.warning("[oq-mode] запись режима упала: %s", type(e).__name__)
        send_message(token, cid, "⚠️ Не смог сохранить режим серии. Попробуй ещё раз.",
                     reply_to=msg_id)
        return True
    finally:
        if not saved:
            try:
                release_watched_lock()
            except Exception:  # noqa: BLE001
                pass

    if same:
        # Режим не изменился — формат встреч прежний, людям сообщать не о чем.
        send_message(token, cid,
                     _OQ_MODE_ALREADY_FIXED[parsed.mode].format(display=display),
                     reply_to=msg_id)
        logger.info(
            "[oq-mode] режим серии %s зафиксирован явно: %s (записей=%d, from=%s, chat=%s)",
            slug, parsed.mode, n, from_id, cid,
        )
        return True

    confirm = _OQ_MODE_CONFIRM[parsed.mode].format(display=display)
    if series_chat is None:
        # Честно: предупредить участников физически некуда.
        confirm += " Чат этой серии не привязан — участников не предупредил."
    elif not series_live:
        # Все записи серии выключены (`enabled: false` / `ignored`): встреч по
        # ней бот не ведёт, и обещать людям в её чате новый формат «к следующей
        # встрече» нечестно — следующей встречи нет.
        confirm += (" Серия выключена — встреч по ней я не веду, поэтому людям в её "
                    "чате ничего не писал.")
    send_message(token, cid, confirm, reply_to=msg_id)
    # Строка людям в чат серии — только если это ДРУГОЙ чат: когда команду дали
    # прямо в чате серии, подтверждение выше уже стоит у них перед глазами и
    # говорит то же самое, а второе сообщение подряд — просто шум.
    if series_chat is not None and series_chat != cid and series_live:
        send_message(token, series_chat, _OQ_MODE_SERIES_NOTICE[parsed.mode])
    logger.info(
        "[oq-mode] режим серии %s: %s → %s (записей=%d, from=%s, chat=%s, серия-чат=%s)",
        slug, old_mode, parsed.mode, n, from_id, cid, series_chat,
    )
    return True


def _oq_mode_unresolved(
    token: str, cid: int, msg_id: Any, ref: str, watched: dict[str, Any],
    *, owner_dm: bool, action: str,
) -> bool:
    """Серию не опознали. `True` = ответили человеку, `False` = молча пропускаем.

    Единственное место, где решается, показывать ли список внутренних имён серий.
    Условие — «владелец И в личке», а не «владелец»: гейт по человеку без гейта
    по МЕСТУ вываливал 16 внутренних имён (часть с фамилиями людей и названиями
    компаний) в групповой чат с участниками встреч — достаточно было любой
    фразы, которую старый парсер принимал за команду.

    В группе не отвечаем вовсе и возвращаем False: если мы не уверены, что это
    команда, сообщение обязано достаться роутам ниже (правки, статусы), а не
    съедаться видимым отказом.
    """
    logger.info("[oq-mode] серия не опознана (%s, chat=%s, ответили=%s)",
                action, cid, "да" if owner_dm else "нет")
    if not owner_dm:
        return False
    known = sorted({
        w.get("series") for w in watched.get("watched", [])
        if isinstance(w, dict) and isinstance(w.get("series"), str) and w.get("series")
    })
    known_str = ", ".join(known) if known else "—"
    send_message(
        token, cid,
        f"🤔 Не нашёл серию «{ref}» в реестре. Известные серии: {known_str}. "
        "Уточни slug или имя.",
        reply_to=msg_id,
    )
    return True


# Ф4 (REQ 4.1): фолбэк, когда голос распознать не удалось (транскрибация
# недоступна / упала). Не молчим — явно говорим, что понимаем текст/кнопки.
VOICE_FALLBACK_MSG = (
    "🎙 Голос пока не расшифровал (транскрибация недоступна). "
    "Ответь, пожалуйста, текстом — например «Спикер 3 = Дарья» — или нажми "
    "кнопку под вопросом."
)


def transcribe_voice_or_none(token: str, msg: dict[str, Any]) -> str | None:
    """Ф4: голосовое `.oga` → текст через `voice_input` (daemon-стек). None при
    любой неудаче (нет модуля / не скачалось / не расшифровалось) — caller
    отправит `VOICE_FALLBACK_MSG`. Текст транскрипта не логируем."""
    try:
        from notary.lib import voice_input  # noqa: PLC0415
        return voice_input.voice_to_text(token, msg)
    except Exception as e:  # noqa: BLE001
        logger.exception("voice transcription crashed: %s", e)
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


def maybe_route_to_feedback_reply(token: str, chat_id: int, allowed_chat: int, msg: dict[str, Any]) -> bool:
    """Ф3 шлюз правок (FB1): reply на доставленный протокол бота в чате серии.

    True → сообщение «прожёвано» фичей правок (приложили правку ИЛИ осознанно
    дропнули в групповом чате) — caller выходит из process_message. False → не
    наша забота, caller продолжает свой dispatch (DM-flow). Импорт защищён: если
    модуль не грузится — фича просто выключена, listener работает как раньше.
    """
    try:
        from notary.lib import feedback_worker  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.debug("feedback_worker import failed (feature off?): %s", e)
        return False
    # R-REPLY диагностика (Ф1 delivery-fixes): точка приёма правок-реплаем — ОБЩАЯ
    # для лички владельца (chat_id == allowed_chat) и группы участников (≠). Логируем
    # МЕТАДАННЫЕ приёма (R9: без текста реплая) — чтобы «спросил, а не реагирует» был
    # виден в логе: видно, что reply дошёл и забрал ли его шлюз правок.
    reply_to_mid = (msg.get("reply_to_message") or {}).get("message_id")
    is_dm = chat_id == allowed_chat
    try:
        claimed = feedback_worker.route_feedback_reply(token, chat_id, msg, allowed_chat=allowed_chat)
    except Exception as e:  # noqa: BLE001
        logger.exception("feedback routing failed: %s", e)
        return False
    logger.info(
        "[r-reply] приём reply chat=%s dm=%s reply_to=%s → правки-шлюз claimed=%s",
        chat_id, is_dm, reply_to_mid, claimed,
    )
    return claimed


def _reply_date_str(msg: dict[str, Any]) -> Optional[str]:
    """`YYYY-MM-DD` из unix-`date` сообщения (для поля `updated` sidecar). None при
    отсутствии/мусоре — статус всё равно проставится (дата метаданная)."""
    ts = msg.get("date")
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return None


# Ф4 (watson, R11): ответы на утренний дайджест. Дайджест САМ просит ответить, поэтому
# молчание в этом канале недопустимо — а молчание было бы ровно тем, что происходит по
# умолчанию: дайджеста нет в индексе доставленных протоколов, значит шлюз правок его не
# узнаёт и в ГРУППОВОМ чате (а дайджест по решению владельца идёт именно туда) молча
# дропает такой reply. Поэтому reply на дайджест разбираем отдельным роутом и ВСЕГДА
# отвечаем: статус — подтверждением, всё остальное — подсказкой, как ответить.
DIGEST_VOICE_HINT = (
    "Голосом статус по дайджесту пока не понимаю 🙏 Напишите ответом на это сообщение "
    "текстом — например: «сделано», «не успел, жду расчёт» или «снимаем, неактуально»."
)
# Ф5 (REQ 6.2): голос НА дайджест под ON-флагом, но STT недоступен/вернул пусто —
# не молчим и не теряем: честно просим прислать статус текстом. ОТДЕЛЬНО от
# DIGEST_VOICE_HINT (та — для OFF-флага, «голос пока не понимаю»); здесь голос мы
# понимаем, но конкретно эту запись расшифровать не удалось (сеть/тишина/шум).
DIGEST_VOICE_STT_HINT = (
    "Не смог расшифровать голосовое 🙏 Пришлите, пожалуйста, статус ответом на это "
    "сообщение текстом — например: «сделано», «не успел, жду расчёт» или «снимаем, "
    "неактуально»."
)
DIGEST_UNPARSED_HINT = (
    "Не понял, по какому вопросу статус 🙏 Ответьте на это сообщение, назвав задачу и "
    "статус — например: «отчёт по марже — сделано» или «презентация — снимаем, неактуально». "
    "Если вопрос будете обсуждать на встрече — отвечать не нужно, бот услышит и пометит сам."
)
# Ф2 (UPU1): reply ПОХОЖ на пачку статусов, но сплиттер её не разобрал (LLM икнул/
# таймаут/мусор). Молча взять один статус и потерять остальные нельзя — честно просим
# прислать по-другому, чтобы ни один статус не пропал в тишине.
DIGEST_BATCH_UNPARSED_HINT = (
    "Похоже, тут несколько задач сразу, но я не смог их надёжно разобрать 🙏 Пришлите, "
    "пожалуйста, вторым сообщением — по одной задаче на строку, с задачей и статусом "
    "(«медиаплан — сделано», «расчёт склада — снимаем»)."
)
# ISS-51 (Ф3): человек прислал статус в серии-ШПАРГАЛКЕ. Учёта висяков в ней нет —
# честно говорим это ОДНОЙ строкой и НИЧЕГО не пишем в статус-стор. Молчать нельзя:
# человек потратил силы на ответ и вправе знать, что отчитываться тут не надо.
LAST_ONLY_STATUS_REPLY = (
    "Спасибо 🙏 В этой серии я вопросы не веду — тот список был справкой к встрече, "
    "статусы по нему не нужны."
)
# ISS-51: тот же случай, но человек отвечает на документ, который САМ просил статус
# (ушёл до переключения серии в справочный режим). Отвечать ему прежней фразой —
# значит на голубом глазу отречься от собственной просьбы. Признаём это прямо.
LAST_ONLY_STATUS_REPLY_AFTER_SWITCH = (
    "Спасибо 🙏 Тот список я присылал ещё со статусами, но с тех пор серию перевели в "
    "справочный режим — учёт вопросов по ней я больше не веду, отвечать не нужно."
)


# ISS-51 (Ф3): ПОВЕЛИТЕЛЬНЫЙ разговор про учёт вопросов — «закрой первую», «сними все»,
# «верни задачу 3», «переоткрой». `detect_status_intent` такое НЕ ловит (он ищет
# УТВЕРЖДЕНИЕ о статусе: «сделано», «неактуально»), поэтому в шпаргалочной серии эти
# фразы проскакивали мимо гейта в ИИ-разбор — и человек слышал «Не разобрал, про какую
# задачу речь» вместо обещанной тишины. Регулярка нужна ТОЛЬКО чтобы решить «это про
# статусы» — статусы всё равно не пишутся (гейт у писателя).
_STATUS_TALK_RE = re.compile(
    r"\b(закрой|закройте|закрыть|закрываем|сними|снимите|снять|снимаем|"
    r"верни|верните|вернуть|откати|откатите|откатить|переоткрой|переоткрыть|"
    r"отметь|отметьте|пометь|пометьте)\b",
    re.IGNORECASE,
)


def _looks_like_status_talk(text: str) -> bool:
    """Похоже ли, что человек говорит про СТАТУСЫ висяков (шире `detect_status_intent`).

    Правки текста протокола сюда НЕ попадают (`looks_edit_heavy` проверяется первым):
    в шпаргалочной серии правки обязаны работать как раньше, иначе «поправьте фамилию»
    получило бы в ответ «я тут вопросы не веду»."""
    if not text:
        return False
    try:
        from notary.lib import pending_status_reply  # noqa: PLC0415
        if pending_status_reply.looks_edit_heavy(text):
            return False
        if pending_status_reply.detect_status_intent(text) is not None:
            return True
    except Exception as e:  # noqa: BLE001 — детект best-effort
        logger.debug("[open-tasks] детект статуса упал: %s", type(e).__name__)
    return _STATUS_TALK_RE.search(text) is not None


def _last_only_digest_reply_text(series: str, msg: dict[str, Any]) -> str:
    """Какой справочной фразой ответить на статус в шпаргалочной серии.

    Если ОТВЕЧАЕМЫЙ документ ушёл ещё в режиме накопления (метка `mode` в состоянии
    отправок), человек не «не понял правила» — он выполнил ровно то, о чём его
    попросил бот. Признаём переключение вслух, иначе бот выглядит как отказавшийся
    от собственной просьбы. Нет метки (старая отправка) → обычная фраза."""
    try:
        from notary.lib import morning_digest, series_memory  # noqa: PLC0415
        reply_to = msg.get("reply_to_message") or {}
        prev = morning_digest.digest_mode_for_message(
            _protokol_root() / series, (msg.get("chat") or {}).get("id"),
            reply_to.get("message_id"))
        if prev == series_memory.OPEN_QUESTIONS_MODE_CARRY:
            return LAST_ONLY_STATUS_REPLY_AFTER_SWITCH
    except Exception as e:  # noqa: BLE001 — формулировка best-effort, не валим ответ
        logger.debug("[morning-digest] режим отправки не определён: %s", type(e).__name__)
    return LAST_ONLY_STATUS_REPLY


def _series_open_questions_mode(series: Optional[str]) -> str:
    """Режим открытых вопросов серии (`carry` | `last-only`) — best-effort.

    Единая точка чтения для статус-роутов listener'а. Сбой чтения реестра →
    `carry` (внутри `series_memory`): молчать про статус, который на самом деле
    учитывается, хуже, чем ответить справочной фразой лишний раз.
    """
    try:
        from notary.lib import series_memory  # noqa: PLC0415
        return series_memory.open_questions_mode(series)
    except Exception as e:  # noqa: BLE001 — режим best-effort, роут не валим
        logger.debug("[oq-mode] режим серии %s не прочитан: %s", series, type(e).__name__)
        return "carry"


def _is_last_only_series(series: Optional[str]) -> bool:
    """Серия-шпаргалка: статус-машина её не трогает (плана Ф3, п. «тишина»)."""
    return _series_open_questions_mode(series) == "last-only"


_OPEN_TASKS_SURFACE_RE = re.compile(
    r"открыт\w*\s+вопрос|вопрос\w*\s+с\s+прошл|протокол", re.IGNORECASE)


def _looks_numbered_open_tasks_surface(text: str) -> bool:
    """Отвечаемое сообщение узнаётся как НУМЕРОВАННАЯ поверхность серии (дайджест открытых
    вопросов / протокол встречи) — по маркеру в тексте/подписи. Нужен для фолбэка слепка
    (ниже): включать добор свежего слепка ТОЛЬКО когда reply реально на такой документ, а
    не на произвольное сообщение."""
    return bool(text and _OPEN_TASKS_SURFACE_RE.search(text))


def _reply_open_tasks_snapshot(series_dir, msg: dict[str, Any]) -> Optional[list]:
    """Ф4 (RISK4/A15): нумерованный СЛЕПОК висяков ОТВЕЧАЕМОГО документа по его
    message_id (`reply_to`). Ординал/pos резолвится против слепка ИМЕННО того сообщения, на
    которое reply (дайджест ИЛИ конкретная версия протокола) — читатель
    `series_memory.open_tasks_snapshot_for_message`.

    Фолбэк (план `ai-owns-reply-mapping`, ISS-44 добор): у ЭТОГО message_id слепка нет
    (участник ответил на ПЕРЕОТПРАВЛЕННУЮ копию дайджеста/протокола — слепок пишется под id
    ПЕРВОЙ отправки), НО отвечаемое сообщение узнаётся как нумерованная поверхность серии →
    берём самый свежий слепок ТОЙ ЖЕ даты (номер бьётся в тот список, что человек видел).
    Без фолбэка бот терял «13,14,15» на копии дайджеста (промпт уходил без нумерации → ИИ
    ронял числа в правку/no_match — боевой инцидент 23.07). Best-effort — сбой → None.
    """
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return None
    mid = reply_to.get("message_id")
    try:
        from notary.lib import series_memory  # noqa: PLC0415
        direct = (series_memory.open_tasks_snapshot_for_message(series_dir, mid)
                  if mid is not None else None)
        if direct is not None:
            return direct
        text = str(reply_to.get("text") or reply_to.get("caption") or "")
        m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        surface = _looks_numbered_open_tasks_surface(text)
        # Фолбэк только при ЯВНОЙ дате в подписи (R5: номер бьём в слепок ИМЕННО той даты,
        # что человек видел; без даты не гадаем — honest-остаток, FU-4).
        snap = (series_memory.latest_open_tasks_snapshot(series_dir, date=m.group(1))
                if (m and surface) else None)
        # Диагностика (безопасно: только метаданные ПОДПИСИ бота, не текст reply участника):
        # почему слепок не резолвнулся — самый частый корень «бот молчит на номера».
        logger.info(
            "[ordinal] snapshot resolve: mid=%s direct=miss date=%s surface=%s cap_len=%d fallback=%s",
            mid, (m.group(1) if m else None), surface, len(text),
            (len(snap) if snap else "none"))
        return snap
    except Exception as e:  # noqa: BLE001 — резолв слепка best-effort, ординал не активен
        logger.debug("[ordinal] snapshot lookup failed (non-fatal): %s", type(e).__name__)
        return None


def _apply_digest_status_batch(
    token: str, chat_id: int, series: str, text: str, msg: dict[str, Any],
    *, snapshot: Optional[list] = None,
) -> bool:
    """Ф2 (ISS-36 случай А): ПАЧКА статусов ответом на дайджест — применяет ВСЕ,
    честно перечисляет остаток кратким структурным ack. Общий status-loop
    `mixed_reply_split.run_status_loop` — тот же, что у mixed-роута протокола (KODE1,
    [[review-checks-two-call-sites]]).

    Возврат True = пачка обработана (ack отправлен) → caller выходит. False = это НЕ
    пачка (флаг OFF / одиночный статус — REQ 2.3 fast-path / сплит оказался не
    смешанным) → caller идёт обычным одиночным `apply`.

    A3: на дайджесте протокола НЕТ → сегменты-«правки» уходят в ПОДСКАЗКУ (edit_as_hint),
    НЕ в шлюз правок. UPU1: сплит икнул на явной пачке → честная подсказка, не молча-один.

    Опасная тройка: в лог только счётчики (сегментов/ok/unresolved/правок), не тексты.
    """
    try:
        from notary.lib import mixed_reply_split  # noqa: PLC0415
        from notary.lib import pending_status_reply  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — модуль не грузится → фича просто выключена
        logger.debug("[morning-digest] mixed-split import failed (feature off?): %s", e)
        return False
    if not mixed_reply_split.is_enabled():
        return False  # A1: дефолт-OFF → одиночный путь как раньше (совместимость)
    if not mixed_reply_split.looks_like_multi_status(text):
        return False  # REQ 2.3: одиночный статус → fast-path, платный сплиттер не зовём
    series_dir = _protokol_root() / series
    try:
        # ISS-39/РИСК2: универсум сплиттера = снапшот отвечаемого дайджеста (что видел
        # человек), тот же, что кормит одиночный apply ниже — консистентно по всем
        # потребителям. Нет снапшота → live-хвост (A14).
        items = pending_status_reply.load_series_pending(series_dir, snapshot=snapshot)
    except Exception as e:  # noqa: BLE001
        logger.debug("[morning-digest] резолв висяков не удался (feature off?): %s", e)
        return False
    try:
        split = mixed_reply_split.split_reply(text, items)
    except Exception as e:  # noqa: BLE001 — сплит не должен валить listener
        # Опасная тройка: логируем только ТИП сбоя, без текста reply и без traceback
        # (как run_status_loop и сам split_reply). split_reply глотает свои исключения —
        # этот catch защитный, тип класса достаточен для диагностики границы сплита.
        logger.warning("[morning-digest] сплит пачки упал (non-fatal): %s", type(e).__name__)
        split = None
    if split is None:
        # UPU1: reply похож на пачку, но сплит не удался → честная подсказка (не молча-один).
        send_message(token, chat_id, DIGEST_BATCH_UNPARSED_HINT, reply_to=msg.get("message_id"))
        logger.info("[morning-digest] пачка не разобрана (UPU1) series=%s → подсказка", series)
        return True
    if not mixed_reply_split.should_engage(split):
        return False  # оказался одиночный/чистый статус → обычный одиночный apply
    status_segments = split["status_segments"]
    n_status_raw = split.get("n_status_raw") or len(status_segments)

    def _apply_one(seg):
        return pending_status_reply.apply_status_reply(
            seg, series_dir, date=_reply_date_str(msg), source="digest-reply",
            snapshot=snapshot,
        )

    loop = mixed_reply_split.run_status_loop(status_segments, _apply_one)
    n_edits = len(split.get("edits") or [])
    # REQ 3.2: объём превышен (сплиттер вернул больше статусов, чем cap) → «N из M».
    overflow = (len(status_segments), n_status_raw) if n_status_raw > len(status_segments) else None
    ack = mixed_reply_split.build_mixed_ack(
        status_groups=loop["groups"],
        n_status_unresolved=loop["n_unresolved"],
        n_edits=n_edits, edit_queued=False, edit_as_hint=True,
        unclear=split.get("unclear") or "", overflow=overflow,
    )
    send_message(token, chat_id, ack, reply_to=msg.get("message_id"))
    logger.info(
        "[morning-digest] пачка статусов series=%s сегментов=%d ok=%d unresolved=%d "
        "правок=%d overflow=%s",
        series, len(status_segments), loop["n_ok"], loop["n_unresolved"], n_edits, bool(overflow),
    )
    return True


def _apply_digest_understand(
    token: str, chat_id: int, series: str, text: str, msg: dict[str, Any],
    *, snapshot: Optional[list] = None,
) -> bool:
    """Ф3 (watson, НЕС2): understand-слой на ДАЙДЖЕСТ-роуте — та же единая точка разбора,
    что у протокола. Гейт `ENABLE_REPLY_UNDERSTAND` дефолт-OFF → OFF → False (прежний
    batch/apply). На дайджесте правок протокола нет (A3): сегменты-правки НЕ уходят в шлюз,
    а честной подсказкой (`edit_as_hint`). Возврат True = understand забрал reply."""
    try:
        from notary.lib import reply_understand  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.debug("[understand-digest] import failed (feature off?): %s", e)
        return False
    if not reply_understand.is_enabled():
        return False
    series_dir = _protokol_root() / series
    if snapshot is None:
        snapshot = _reply_open_tasks_snapshot(series_dir, msg)
    replier = _msg_display_name(msg)
    # ОЖИД1: пачка → лёгкая реакция ДО долгого ИИ-разбора, чтобы тишина не читалась плохо.
    from notary.lib import mixed_reply_split  # noqa: PLC0415
    if mixed_reply_split.looks_like_multi_status(text):
        send_message(token, chat_id, UNDERSTAND_PROGRESS_HINT, reply_to=msg.get("message_id"))
    try:
        result = reply_understand.understand_reply(
            text, series_dir=series_dir, replier=replier,
            snapshot=snapshot, orig_ts=msg.get("date"), date=_reply_date_str(msg),
            source="understand-digest", msg_id=msg.get("message_id"), chat_id=chat_id,
        )
    except Exception as e:  # noqa: BLE001 — understand не должен валить listener
        logger.exception("[understand-digest] разбор упал: %s", e)
        return False
    if not result.get("engaged"):
        return False
    # Настройка серии (R12): владельцу форвард; ответ сотруднику схлопнут в ЕДИНЫЙ ack.
    if result.get("config_requests"):
        _forward_config_to_owner(series, result["config_requests"])
    if result.get("deferred"):
        if result.get("status_groups"):
            _send_understand_ack(token, chat_id, msg, result, series_dir=series_dir,
                                 snapshot=snapshot, replier=replier, edit_as_hint=True)
        send_message(token, chat_id, UNDERSTAND_DEFERRED_HINT, reply_to=msg.get("message_id"))
        return True
    # Ф4 (R13-R16/R26): ЕДИНЫЙ ack (на дайджесте правки — подсказкой, `edit_as_hint=True`).
    _send_understand_ack(token, chat_id, msg, result, series_dir=series_dir,
                         snapshot=snapshot, replier=replier, edit_as_hint=True)
    logger.info(
        "[understand-digest] reply разобран series=%s applied=%d questions=%d config=%d",
        series, len(result.get("applied") or []), len(result.get("questions") or []),
        len(result.get("config_requests") or []),
    )
    return True


def _digest_voice_reply_enabled() -> bool:
    """Ф5-гейт `ENABLE_DIGEST_VOICE_REPLY` — ДЕФОЛТ OFF. ON ← `1/true/yes/on`.

    Дефолт-OFF В КОДЕ — ПО ПРИЧИНЕ ТЕСТОВ (A11, память
    [[reissue-llm-tier-gate-default-off]]): без флага голос-ветка на дайджесте НЕ зовёт
    реальный Groq STT → офлайн-тесты остаются зелёными и квоту не жгут. На боевом деплое
    флаг ВЫСТАВЛЯЕТСЯ В 1 СРАЗУ (как `ENABLE_MIXED_REPLY_SPLIT`, Гейт 2 Q2) — это защита
    тестов + аварийный kill-switch, НЕ «ждём smoke». При OFF голос-reply на дайджест ведёт
    себя как сегодня — отбивка `DIGEST_VOICE_HINT` «напишите текстом».
    """
    raw = (os.environ.get("ENABLE_DIGEST_VOICE_REPLY") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _digest_voice_to_text_or_hint(
    token: str, chat_id: int, series: str, msg: dict[str, Any]
) -> Optional[str]:
    """Ф5 (REQ 6.1-6.3): голосовой reply на дайджест → РАСШИФРОВАННЫЙ текст для того же
    статус-парсера, что и текст (мульти-статус Ф2 + ординал Ф4). НЕ переписываем парсер —
    только подаём распознанный текст в существующий вход.

    Возврат:
      • str — расшифровка (caller гонит её общим путём вместе с текстовыми reply);
      • None — подсказка УЖЕ отправлена (флаг OFF ИЛИ STT пусто/недоступен), caller выходит
        (`return True`): ни одного статуса молча не теряем (6.2 «не молчим»).

    Транскрипция — существующий Groq-путь `transcribe_voice_or_none` → `voice_input.
    voice_to_text` (тот же daemon/Groq-стек, что «в других местах», A11).

    Опасная тройка: расшифровку и ключ Groq/токен НЕ логируем — только длину/исход.
    """
    if not _digest_voice_reply_enabled():
        # OFF (тестовый дефолт): текущая отбивка «голос пока не понимаю, напишите текстом».
        send_message(token, chat_id, DIGEST_VOICE_HINT, reply_to=msg.get("message_id"))
        logger.info("[morning-digest] голос-reply series=%s (флаг OFF) → подсказка", series)
        return None
    voice_text = transcribe_voice_or_none(token, msg)
    if not voice_text or not voice_text.strip():
        # 6.2: STT недоступен/пусто → честная подсказка (не молчим, не теряем).
        send_message(token, chat_id, DIGEST_VOICE_STT_HINT, reply_to=msg.get("message_id"))
        logger.info("[morning-digest] голос-reply series=%s STT пусто → фолбэк", series)
        return None
    text = voice_text.strip()
    # Опасная тройка: только ДЛИНА расшифровки, НЕ сам текст.
    logger.info("[morning-digest] голос-reply series=%s расшифрован chars=%d", series, len(text))
    return text


def maybe_route_to_digest_reply(
    token: str, chat_id: Optional[int], msg: dict[str, Any]
) -> bool:
    """Ф4 (watson, R11): ЛЮБОЙ reply на утренний дайджест «Открытые вопросы к встрече».

    Стоит ДО `maybe_route_to_pending_status_reply` и ДО шлюза правок, и — в отличие от
    них — прожёвывает reply на дайджест ЦЕЛИКОМ (всегда True, если это реплай на
    дайджест). Причина: дайджест — НЕ протокол. Шлюзу правок в нём нечего править, а в
    групповом чате он такие reply молча дропает — участник, которого шапка дайджеста
    попросила ответить, получал бы тишину и думал, что статус учтён.

    Разбор:
      • текст с однозначным статусом → `pending_status_reply` (тот же sidecar Ф2, второго
        канала статусов нет) → ack;
      • голос/аудио под флагом `ENABLE_DIGEST_VOICE_REPLY` (Ф5) → транскрибируем и гоним
        расшифровку тем же путём, что текст (мульти-статус Ф2 + ординал Ф4); OFF / STT
        пусто → подсказка (не молчим — 6.2);
      • текст без статуса / не резолвится конкретный висяк → подсказка, как ответить.

    Опасная тройка: НЕ логируем текст reply/висяков — только серию, код статуса, исход.
    """
    if chat_id is None:
        return False
    series = _reply_context_series_digest(chat_id, msg)
    if not series:
        return False  # не reply на дайджест — не наш путь
    if msg.get("voice") or msg.get("audio"):
        # Ф5 (REQ 6.1-6.3): под флагом ENABLE_DIGEST_VOICE_REPLY (дефолт-OFF ради
        # офлайн-тестов, на проде =1 сразу — Гейт 2 Q2) голос ТРАНСКРИБИРУЕТСЯ и идёт
        # ДАЛЬШЕ тем же путём, что текст (мульти-статус Ф2 + ординал Ф4). OFF / STT
        # пусто → подсказка уже отправлена внутри, выходим (не молчим — 6.2).
        text = _digest_voice_to_text_or_hint(token, chat_id, series, msg)
        if text is None:
            return True
    else:
        text = (msg.get("text") or "").strip()
        if not text:
            return True  # стикер/фото на дайджест — молча жуём, отвечать не на что
    # ISS-51 (Ф3): серия-шпаргалка — статус-машину не будим ВООБЩЕ. Гейт стоит ВЫШЕ
    # understand-слоя: тот сам пишет статусы в sidecar, и «сначала разобрать ИИ, потом
    # решить, что не пишем» означало бы платный вызов ради выброшенного результата.
    # Статус-маркер есть → отвечаем справочной фразой (человек написал не в пустоту),
    # реакция без маркера («ок», «спасибо») — молчим, как и в carry.
    if _is_last_only_series(series):
        try:
            from notary.lib import pending_status_reply  # noqa: PLC0415
            has_status = pending_status_reply.detect_status_intent(text) is not None
        except Exception as e:  # noqa: BLE001 — детект best-effort
            logger.debug("[morning-digest] детект статуса упал: %s", type(e).__name__)
            has_status = False
        if has_status:
            send_message(token, chat_id, _last_only_digest_reply_text(series, msg),
                         reply_to=msg.get("message_id"))
            logger.info("[morning-digest] серия %s в режиме last-only — статус НЕ пишу "
                        "(ответил справкой)", series)
            return True
        # Статус-маркера нет — реакция («ок», «спасибо»). Молчим и жуём, ТОЧНО как в
        # накоплении (там ниже такая же ветка). Осознанный остаток: просьбу настройки
        # («уберите меня из рассылки») в carry ловит understand-слой, здесь он НЕ
        # запускается — платный разбор в серии, где статусы всё равно заблокированы у
        # писателя, вернул бы «не разобрал, про какую задачу речь» вместо тишины.
        logger.info("[morning-digest] серия %s в режиме last-only, reply без статус-"
                    "маркера → молчим (как в carry)", series)
        return True
    # Ф3 (watson, НЕС2): understand-слой ДО словарного precheck ниже — иначе настройка
    # серии (R12) / нестандартный статус (R7) молча отсеклись бы `detect_status_intent
    # is None`. Флаг ENABLE_REPLY_UNDERSTAND дефолт-OFF → OFF → прежний путь (совместимость).
    if _apply_digest_understand(token, chat_id, series, text, msg):
        return True
    try:
        from notary.lib import pending_status_reply  # noqa: PLC0415
        # Подсказку даём, только если человек ПЫТАЛСЯ дать статус (есть статус-маркер).
        # Реакция без маркера («ок», «спасибо», «+», вопрос в чат) — не попытка учёта:
        # бот в рабочем чате реплики не комментирует, иначе дайджест превращается в
        # источник шума, который вдобавок легко раскачать повторами. Молчание здесь
        # ничего не теряет — терялся бы только НЕПОНЯТЫЙ статус, а его мы ловим ниже.
        if pending_status_reply.detect_status_intent(text) is None:
            logger.info("[morning-digest] reply без статус-маркера series=%s → молчим", series)
            return True
        # Ф4: слепок нумерации ОТВЕЧАЕМОГО дайджеста (по reply_to message_id) — против
        # него резолвится ординал («первая», «задача 3»). Нет слепка → None (overlap).
        series_dir = _protokol_root() / series
        snapshot = _reply_open_tasks_snapshot(series_dir, msg)
        # Ф2 (ISS-36 случай А): ПАЧКА статусов одним reply под флагом
        # ENABLE_MIXED_REPLY_SPLIT. Одиночный статус сюда НЕ заходит (fast-path
        # внутри, REQ 2.3) → идём обычным одиночным apply ниже. Пачку — обработали и
        # вышли (True). OFF → тоже вниз, одиночным (обратная совместимость).
        if _apply_digest_status_batch(token, chat_id, series, text, msg, snapshot=snapshot):
            return True
        # ISS-37, ВТОРАЯ точка вызова ([[review-checks-two-call-sites]]): пачка правок
        # («вместо … правильно …», «заменить», «поправить») не должна закрывать висяк по
        # случайному пересечению слов — здесь `apply_status_reply` идёт по ЦЕЛОМУ тексту,
        # как в протокольном роуте. Правки на дайджест и так не применяются (их место —
        # ответ на сам протокол), поэтому честно подсказываем, а не молчим и не гадаем.
        if pending_status_reply.looks_edit_heavy(text):
            send_message(
                token, chat_id,
                "✏️ Похоже на правки текста протокола, а не на статусы вопросов. "
                "Их вносите, пожалуйста, ответом на сам протокол встречи — "
                "тогда перевыпущу его с исправлениями.",
                reply_to=msg.get("message_id"),
            )
            logger.info(
                "[morning-digest] reply похож на пачку правок series=%s → статус не "
                "трогаем, дана подсказка", series,
            )
            return True
        result = pending_status_reply.apply_status_reply(
            text, series_dir, date=_reply_date_str(msg), source="digest-reply",
            snapshot=snapshot,
        )
    except Exception as e:  # noqa: BLE001 — обработка статуса не должна валить listener
        logger.exception("[morning-digest] обработка reply на дайджест упала: %s", e)
        return True  # сообщение всё равно наше — в шлюз правок его отдавать нельзя
    if result:
        send_message(token, chat_id, result["ack"], reply_to=msg.get("message_id"))
        logger.info(
            "[morning-digest] статус с дайджеста принят series=%s label=%s status=%s",
            series, result.get("label"), result.get("status"),
        )
        return True
    send_message(token, chat_id, DIGEST_UNPARSED_HINT, reply_to=msg.get("message_id"))
    logger.info("[morning-digest] reply на дайджест не распознан series=%s → подсказка", series)
    return True


def maybe_route_to_pending_status_reply(
    token: str, chat_id: Optional[int], allowed_chat: int, msg: dict[str, Any]
) -> bool:
    """Ф5 (REQ R9/R5/A10): reply на протокол со СТАТУСОМ висяка («этот закрыт»,
    «снимаем, неактуально», «жду расчёт», «нет, не закрыто, верни X»).

    Проверяется ДО шлюза правок (`maybe_route_to_feedback_reply`): иначе шлюз
    «прожевал» бы reply как правку текста. Перехватываем КОНСЕРВАТИВНО — только
    однозначный статус-reply на доставленный протокол серии; всё неоднозначное →
    False → шлюз правок обрабатывает как раньше (A9). Любой участник чата (A4 —
    без сверки личности): сам факт reply на протокол серии = гейт.

    True = распознан статус-reply, статус проставлен в sidecar Ф2 + дан ack →
    caller выходит. False = не наш случай → обычный dispatch.

    Опасная тройка: НЕ логируем текст reply/висяков — только метаданные (серия,
    код статуса, исход). Голос/аудио сюда НЕ заводим (Ф5 — текст): голос-reply
    падает в шлюз правок как раньше (там своя транскрипция).
    """
    if chat_id is None:
        return False
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return False
    text = (msg.get("text") or "").strip()
    if not text or msg.get("voice") or msg.get("audio"):
        return False  # пусто / голос-аудио — не наш путь (текст-only, Ф5)
    try:
        from notary.lib import pending_status_reply  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — модуль не грузится → фича просто выключена
        logger.debug("[pending-status] import failed (feature off?): %s", e)
        return False
    # Дешёвый регекс-предчек ДО резолва доставленного протокола: подавляющее
    # большинство reply'ев — правки текста (нет статус-маркера) → выходим до
    # delivered-index lookup, отдаём их шлюзу правок без накладных.
    if pending_status_reply.detect_status_intent(text) is None:
        return False
    # ISS-37: reply — ПАЧКА ПРАВОК («вместо … правильно …», «заменить», «поправить»).
    # Матч висяка идёт по пересечению слов со ВСЕМ текстом, поэтому слова из правок
    # закрывают случайную живую задачу (боевой инцидент 20.07). Смешанный роут выше
    # это ловит, но он СЕТЕВОЙ (Haiku) — при таймауте/выключенном флаге reply доходит
    # сюда живым. Детерминированный чек — последний рубеж: отдаём такой reply шлюзу
    # правок (он ниже по dispatch), статус НЕ трогаем.
    if pending_status_reply.looks_edit_heavy(text):
        logger.info(
            "[pending-status] reply похож на пачку правок → статус не трогаем, "
            "отдаём шлюзу правок chat=%s", chat_id,
        )
        return False
    series = _reply_context_series(chat_id, msg)
    if not series:
        return False  # не reply на доставленный протокол серии (дайджест — свой роут)
    # ISS-51 (Ф3): в серии-шпаргалке статусов нет. Сюда доходит только ЧИСТЫЙ статус
    # (выше отсеяны и «без статус-маркера», и пачки правок), поэтому забираем reply
    # себе и отвечаем справочной фразой — отдать его шлюзу правок значило бы
    # перевыпустить протокол из-за слова «сделано».
    if _is_last_only_series(series):
        send_message(token, chat_id, LAST_ONLY_STATUS_REPLY, reply_to=msg.get("message_id"))
        # ISS-51 + класс ISS-33: «расчёт сделан, и в протоколе поправь Иванова на
        # Петрова» — ОДНО сообщение с двумя намерениями. Статус в шпаргалке никому не
        # нужен, а правка — нужна, и проглотить reply целиком значит потерять её молча
        # (сплиттер сюда не помогает: в этом режиме его сознательно не зовём). Признак
        # правки в тексте → отдаём сообщение шлюзу правок, как это делает накопление с
        # любым не-статусным reply. Справочную фразу при этом уже сказали: человек
        # знает, что статусную половину я не веду.
        try:
            from notary.lib import pending_status_reply as _psr  # noqa: PLC0415
            has_edit = bool(_psr._EDIT_MARKER_RE.search(text))
        except Exception as e:  # noqa: BLE001 — признак best-effort
            logger.debug("[pending-status] детект правки упал: %s", type(e).__name__)
            has_edit = False
        logger.info("[pending-status] серия %s в режиме last-only — статус НЕ пишу, "
                    "ответил справкой (правка в тексте=%s, chat=%s)",
                    series, has_edit, chat_id)
        return not has_edit
    series_dir = _protokol_root() / series
    # Ф4: слепок нумерации ИМЕННО той версии протокола, на которую reply (по message_id) —
    # ординал резолвится против него (Гейт 2 Q1). Нет слепка → None (overlap как до Ф4).
    snapshot = _reply_open_tasks_snapshot(series_dir, msg)
    try:
        result = pending_status_reply.apply_status_reply(
            text, series_dir, date=_reply_date_str(msg), source="reply",
            snapshot=snapshot,
        )
    except Exception as e:  # noqa: BLE001 — обработка статуса не должна валить listener
        logger.exception("[pending-status] обработка reply упала: %s", e)
        return False
    if not result:
        return False  # не однозначный статус-reply → отдаём шлюзу правок (A9)
    send_message(token, chat_id, result["ack"], reply_to=msg.get("message_id"))
    logger.info(
        "[pending-status] reply-статус принят chat=%s series=%s label=%s status=%s",
        chat_id, series, result.get("label"), result.get("status"),
    )
    return True


def maybe_route_to_mixed_reply(
    token: str, chat_id: Optional[int], allowed_chat: int, msg: dict[str, Any]
) -> bool:
    """Ф5 (ISS-33): расщепление СМЕШАННОГО reply (статусы + правки) на части.

    Стоит ДО статус-роута и шлюза правок. Корень: reply Ольги Новиковой (4 правки +
    2 статуса одним сообщением) — статус-роут распознал ОДИН статус, вернул True и
    прожевал всё, 4 правки и второй статус молча пропали. Тихая потеря — худший режим.

    Гейт `ENABLE_MIXED_REPLY_SPLIT` ДЕФОЛТ-OFF (A1): при OFF — сразу False, поведение
    = ТЕКУЩЕЕ (один reply — один роут, обратная совместимость). При ON и СМЕШАННОМ
    reply (≥2 статуса, ИЛИ статус+правки, ИЛИ статус+непонятое) — обрабатываем ОБЕ
    части и шлём ОДИН объединённый ack → True. Чистый одиночный статус / чистая
    правка / мусор → False → обычные роуты ниже (РИСК5: чистый статус в reissue НЕ
    уходит — шлюз правок при пустом правочном остатке не зовём вовсе).

    Опасная тройка: НЕ логируем текст reply/сегментов — только счётчики.
    """
    if chat_id is None:
        return False
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return False
    text = (msg.get("text") or "").strip()
    if not text or msg.get("voice") or msg.get("audio"):
        return False  # текст-only (голос/аудио — как раньше, в шлюз правок)
    try:
        from notary.lib import mixed_reply_split  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — модуль не грузится → фича просто выключена
        logger.debug("[mixed-reply] import failed (feature off?): %s", e)
        return False
    if not mixed_reply_split.is_enabled():
        return False  # A1: дефолт-OFF → текущее поведение
    try:
        from notary.lib import pending_status_reply  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — модуль не грузится → фича выключена
        logger.debug("[mixed-reply] import pending_status_reply failed: %s", e)
        return False
    # Дешёвый регекс-предчек ДО платного LLM-сплита: смешанный reply ВСЕГДА несёт
    # статус-маркер (статус-часть). Нет маркера → это чистая правка/переписка →
    # не наш путь (в шлюз правок), сплиттер не дёргаем (зеркалит статус-роут :1253).
    if pending_status_reply.detect_status_intent(text) is None:
        return False
    series = _reply_context_series(chat_id, msg)
    if not series:
        return False  # не reply на доставленный протокол серии (дайджест — свой роут)
    # ISS-51 (Ф3): серия-шпаргалка — сплиттер не зовём вовсе (платный LLM ради
    # статусов, которые всё равно не применим). Отдаём reply ВНИЗ: одиночный статус
    # заберёт статус-роут и ответит справочной фразой, а reply с правками уйдёт в
    # шлюз правок, как и в carry, — правка текста протокола от режима не зависит.
    if _is_last_only_series(series):
        logger.info("[mixed-reply] серия %s в режиме last-only — сплит не зову "
                    "(chat=%s)", series, chat_id)
        return False
    series_dir = _protokol_root() / series
    # Ф4/ISS-39: слепок нумерации отвечаемой версии протокола (по message_id) — ординал
    # резолвится против него (A15), И универсум сплиттера/overlap ограничен им (РИСК2:
    # что человек видел в ТОМ протоколе, а не вся глубина хвоста). Нет слепка → None
    # (overlap/live как до Ф4).
    snapshot = _reply_open_tasks_snapshot(series_dir, msg)
    try:
        items = pending_status_reply.load_series_pending(series_dir, snapshot=snapshot)
    except Exception as e:  # noqa: BLE001
        logger.debug("[mixed-reply] резолв висяков не удался (feature off?): %s", e)
        return False
    try:
        split = mixed_reply_split.split_reply(text, items)
    except Exception as e:  # noqa: BLE001 — сплит не должен валить listener
        logger.exception("[mixed-reply] сплит упал (non-fatal): %s", e)
        return False
    if not mixed_reply_split.should_engage(split):
        # ISS-37: сплиттер ОТРАБОТАЛ и не нашёл ни одного статус-сегмента — значит это
        # не статус-reply, чем бы он ни был. Отпустить его нельзя: следующим стоит
        # одиночный статус-роут (:2117), а мы сюда попали только потому, что
        # `detect_status_intent` дал не-None → он возьмётся за тот же текст и закроет
        # висяк по случайному пересечению слов. Поэтому отдаём reply СРАЗУ шлюзу
        # правок, минуя статус-роут (ack шлюза — обычный, это штатный путь правки).
        if split is not None and not (split.get("status_segments") or []):
            try:
                from notary.lib import feedback_worker  # noqa: PLC0415
                handled = feedback_worker.route_feedback_reply(
                    token, chat_id, msg, allowed_chat=allowed_chat,
                )
            except Exception as e:  # noqa: BLE001 — сбой шлюза не валит listener
                logger.exception("[mixed-reply] шлюз правок упал (non-fatal): %s", e)
                return False
            logger.info(
                "[mixed-reply] статус-сегментов нет → отдал шлюзу правок "
                "chat=%s series=%s handled=%s", chat_id, series, bool(handled),
            )
            return bool(handled)
        return False  # не смешанный → обычные роуты (чистый статус/правка) — РИСК5
    status_segments = split["status_segments"]
    n_status_raw = split.get("n_status_raw") or len(status_segments)
    edit_remainder = "\n".join(split["edits"])
    n_edits = len(split["edits"])
    unclear = split["unclear"]

    # 1) Статусы — общий status-loop (РИСК4): один вызов резолвит ОДИН висяк, поэтому
    #    сегмент-на-статус + цикл = все статусы. Тот же хелпер, что у digest-роута (KODE1).
    def _apply_one(seg):
        return pending_status_reply.apply_status_reply(
            seg, series_dir, date=_reply_date_str(msg), source="mixed-reply",
            snapshot=snapshot,
        )

    loop = mixed_reply_split.run_status_loop(status_segments, _apply_one)
    n_status_ok = loop["n_ok"]
    n_status_unresolved = loop["n_unresolved"]

    # 2) Правки — шлюз правок с ПОДАВЛЕННЫМ ack (РИСК5: только при непустом остатке).
    edit_queued = False
    if edit_remainder.strip():
        acked = {"v": False}

        def _capture_send(*_a, **_k):
            acked["v"] = True  # ack правок подавлен, факт приёма зафиксирован

        edit_msg = {**msg, "text": edit_remainder}
        edit_msg.pop("voice", None)
        edit_msg.pop("audio", None)
        try:
            from notary.lib import feedback_worker  # noqa: PLC0415
            feedback_worker.route_feedback_reply(
                token, chat_id, edit_msg, allowed_chat=allowed_chat, send=_capture_send,
            )
        except Exception as e:  # noqa: BLE001 — сбой правок не роняет обработку статусов
            logger.exception("[mixed-reply] шлюз правок упал (non-fatal): %s", e)
        edit_queued = acked["v"]

    # 3) Объединённый ack — ОДИН ответ на весь reply. Краткий СТРУКТУРНЫЙ по статус-
    #    группам (REQ 2.4/OZHID2). REQ 3.2: cap статусов превышен → «N из M» (не тихо).
    overflow = (len(status_segments), n_status_raw) if n_status_raw > len(status_segments) else None
    ack = mixed_reply_split.build_mixed_ack(
        status_groups=loop["groups"], n_status_unresolved=n_status_unresolved,
        n_edits=n_edits, edit_queued=edit_queued, unclear=unclear, overflow=overflow,
    )
    send_message(token, chat_id, ack, reply_to=msg.get("message_id"))
    logger.info(
        "[mixed-reply] chat=%s series=%s статусов=%d(ok=%d,unresolved=%d) правок=%d "
        "queued=%s непонятое=%s overflow=%s",
        chat_id, series, len(status_segments), n_status_ok, n_status_unresolved,
        n_edits, edit_queued, bool(unclear), bool(overflow),
    )
    return True


# ── Ф3 understand-слой (watson, R7-R12/R24/R27): единая точка разбора reply ────
# Флаг ENABLE_REPLY_UNDERSTAND дефолт-OFF (R10) — при OFF роут сразу False, поведение
# бота = ТЕКУЩЕЕ (mixed/status/digest как сегодня). Голос на протокол под отдельным
# ENABLE_PROTOCOL_VOICE_REPLY (R27) транскрибируется ПЕРЕД разбором.
CONFIG_FORWARDED_REPLY = (
    "Передал Илье — формат протокола решает он. Как решит, вернусь 🙌"
)
UNDERSTAND_DEFERRED_HINT = (
    "⏳ Сейчас не смог разобрать сообщение (техническая заминка). Ничего не потерял — "
    "разберу и отвечу позже."
)
# Ф4 (ОЖИД1): при уходе в длинный сплит/ИИ-разбор ПАЧКИ (десятки секунд) — сразу лёгкая
# реакция, чтобы минута тишины не читалась как «опять не работает» (требование «не бесил»).
UNDERSTAND_PROGRESS_HINT = "⏳ Получил, разбираю…"


def _msg_display_name(msg: dict[str, Any]) -> Optional[str]:
    """Имя ответившего для проверки «своя/чужая задача» (R11). Из `from`: имя+фамилия
    либо username. Нет данных → None (R11 тогда не утверждает «чужая»)."""
    frm = msg.get("from")
    if not isinstance(frm, dict):
        return None
    parts = [frm.get("first_name"), frm.get("last_name")]
    name = " ".join(p for p in parts if isinstance(p, str) and p.strip()).strip()
    if name:
        return name
    uname = frm.get("username")
    return uname if isinstance(uname, str) and uname.strip() else None


def _protocol_voice_reply_enabled() -> bool:
    """R27-гейт `ENABLE_PROTOCOL_VOICE_REPLY` — ДЕФОЛТ OFF (ради офлайн-тестов, как
    `ENABLE_DIGEST_VOICE_REPLY`). На проде =1 в деплой-чек-листе Ф6."""
    raw = (os.environ.get("ENABLE_PROTOCOL_VOICE_REPLY") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _maybe_transcribe_protocol_voice(
    token: str, cid: Optional[int], msg: dict[str, Any]
) -> dict[str, Any]:
    """R27: голосовой reply на ПРОТОКОЛ серии → расшифровка ПЕРЕД разбором, чтобы
    голос шёл тем же путём понимания, что текст (решение владельца 21.07). Раньше
    voice на протокол мимо статус-роутов уходил в шлюз правок как «правка».

    Действуем КОНСЕРВАТИВНО: только под флагом, только если это reply на распознанный
    протокол (дайджест — свой роут выше), только голос/аудио. Успех → подставляем
    `text`, чистим `voice`/`audio` (дальше обычный dispatch). Пусто/сбой → msg без
    изменений (падёт в шлюз правок с его голос-фолбэком, как раньше). Опасная тройка:
    расшифровку не логируем — только длину/исход."""
    if not _protocol_voice_reply_enabled():
        return msg
    if not (msg.get("voice") or msg.get("audio")):
        return msg
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return msg
    if not _reply_context_series(cid, msg):
        return msg  # не reply на протокол серии — не наш путь (дайджест обработан выше)
    voice_text = transcribe_voice_or_none(token, msg)
    if not voice_text or not voice_text.strip():
        logger.info("[understand] голос на протокол: STT пусто → шлюз правок (как раньше)")
        return msg
    out = dict(msg)
    out["text"] = voice_text.strip()
    out.pop("voice", None)
    out.pop("audio", None)
    logger.info("[understand] голос на протокол расшифрован chars=%d → текст-путь", len(voice_text))
    return out


def _forward_config_to_owner(series: str, requests: list) -> None:
    """R12: настройку формата серии пересылаем ВЛАДЕЛЬЦУ (бот сам формат не меняет).
    Best-effort через `notify` (tg-send). Опасная тройка: шлём короткую пометку с серией
    и числом просьб, БЕЗ полного текста участника наружу сверх необходимого."""
    try:
        from notary.lib import notify  # noqa: PLC0415
        n = len(requests)
        notify.push(
            f"🔧 Просьба про формат серии «{series}» ({n}) — участник просит поменять "
            f"вид протокола/дайджеста. Формат решаешь ты. Первая: {requests[0][:160]}",
            dedupe_key=f"series-config:{series}",
        )
    except Exception as e:  # noqa: BLE001 — форвард best-effort, listener не валим
        logger.warning("[understand] форвард настройки владельцу упал (non-fatal): %s", type(e).__name__)


def _forward_understand_edits(
    token: str, chat_id: int, allowed_chat: int, msg: dict[str, Any], edits: list
) -> bool:
    """РИСК1: правки understand-слоя ОБЯЗАНЫ утечь в СУЩЕСТВУЮЩУЮ петлю правок (дебаунс
    20 мин / durable-learning / anti-injection / сводный ack / rollback-роут), а не
    потеряться. Тот же путь, что у mixed-роута (:1608): `route_feedback_reply` с
    ПОДАВЛЕННЫМ ack. Возвращает, приняла ли петля правку."""
    remainder = "\n".join(e for e in edits if isinstance(e, str) and e.strip()).strip()
    if not remainder:
        return False
    acked = {"v": False}
    edit_msg = {**msg, "text": remainder}
    edit_msg.pop("voice", None)
    edit_msg.pop("audio", None)
    try:
        from notary.lib import feedback_worker  # noqa: PLC0415
        feedback_worker.route_feedback_reply(
            token, chat_id, edit_msg, allowed_chat=allowed_chat,
            send=lambda *_a, **_k: acked.__setitem__("v", True),
        )
    except Exception as e:  # noqa: BLE001 — сбой правок не роняет обработку статусов
        logger.exception("[understand] шлюз правок упал (non-fatal): %s", e)
    return acked["v"]


def _send_understand_ack(
    token: str, chat_id: int, msg: dict[str, Any], result: dict, *,
    series_dir, snapshot, replier: Optional[str], edit_as_hint: bool
) -> None:
    """Ф4 (R13-R16/R26): ЕДИНЫЙ сборщик ack — «применил / не понял / не привязал» +
    нумерованный переспрос + канал несогласия. Заменяет временный ack Ф3 и схлопывает
    бывший двойной ack config+status в ОДНО сообщение.

    После отправки персистит контекст ack по его `message_id` ([[reply_ack_state]],
    RAZM6: переживает рестарт) — по нему reply человека «не так/верни» (R26) или ответ
    цифрой на переспрос (R15) находит исходное решение. Персист только когда есть что
    откатывать/уточнять (applied/question)."""
    from notary.lib import reply_ack, reply_ack_state  # noqa: PLC0415
    built = reply_ack.build_ack(
        result, series_dir=series_dir, snapshot=snapshot, replier=replier,
        edit_as_hint=edit_as_hint)
    mid = send_message(token, chat_id, built["text"], reply_to=msg.get("message_id"))
    if mid is not None and (built.get("applied") or built.get("question")):
        reply_ack_state.record(
            mid, series_dir=series_dir, chat_id=chat_id, replier=replier,
            applied=built.get("applied"), has_tier2=built.get("has_tier2"),
            question=built.get("question"), date=_reply_date_str(msg))


def maybe_route_to_reply_understand(
    token: str, chat_id: Optional[int], allowed_chat: int, msg: dict[str, Any]
) -> bool:
    """Ф3 (watson, R7-R12/R24): ЕДИНАЯ точка разбора reply на ПРОТОКОЛ серии (НЕС2).

    Стоит ПЕРВЫМ из статус-роутов протокола (до mixed/pending/шлюза правок). Гейт
    `ENABLE_REPLY_UNDERSTAND` дефолт-OFF (R10): OFF → сразу False, роуты ниже работают
    как сегодня. ON → ИИ-разбор understand-слоя: применяет статусы ступеней 1-2, чужие/
    неоднозначные — вопросом (ступени 3), правки → в петлю правок (РИСК1), настройку
    серии → владельцу (R12), при недоступном ИИ — очевидное словарём + очередь (R9).

    True = understand взял reply (что-то распознал/применил/спросил) → caller выходит.
    False = не наш случай (флаг OFF / не reply на протокол / understand ничего не нашёл)
    → обычные роуты ниже.

    Опасная тройка: НЕ логируем текст reply/сегментов — только счётчики/классы."""
    if chat_id is None:
        return False
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return False
    text = (msg.get("text") or "").strip()
    if not text or msg.get("voice") or msg.get("audio"):
        return False  # голос уже подставлен текстом в _maybe_transcribe_protocol_voice (R27)
    try:
        from notary.lib import reply_understand  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — модуль не грузится → фича выключена
        logger.debug("[understand] import failed (feature off?): %s", e)
        return False
    if not reply_understand.is_enabled():
        return False  # R10: дефолт-OFF → обычные роуты (обратная совместимость)
    series = _reply_context_series(chat_id, msg)
    if not series:
        return False  # не reply на доставленный протокол серии (дайджест — свой роут)
    # ISS-51 (Ф3): в серии-шпаргалке СТАТУСНЫЙ reply мимо ИИ — вниз, к детерминированному
    # роуту: он ответит «вопросы тут не веду». Гейт УЗКИЙ (только статус-маркер): правки
    # текста и свежие инструкции от режима не зависят и по-прежнему идут через understand.
    if _is_last_only_series(series):
        if _looks_like_status_talk(text):
            logger.info("[understand] серия %s в режиме last-only — статус-reply "
                        "отдаю детерминированному роуту (chat=%s)", series, chat_id)
            return False
    series_dir = _protokol_root() / series
    snapshot = _reply_open_tasks_snapshot(series_dir, msg)
    replier = _msg_display_name(msg)
    return _understand_and_ack(
        token, chat_id, allowed_chat, msg, series=series, series_dir=series_dir,
        snapshot=snapshot, replier=replier, text=text)


def _understand_and_ack(
    token: str, chat_id: int, allowed_chat: int, msg: dict[str, Any], *,
    series: str, series_dir, snapshot, replier: Optional[str], text: str,
) -> bool:
    """Общий движок «понять reply → применить → единый ack» (Ф3+Ф4). Вынесен из
    `maybe_route_to_reply_understand`, чтобы ТОТ ЖЕ разбор мог запускать и ack-роут
    (ISS-49: reply на любое сообщение бота — не только на его вопрос — понимается как
    свежая инструкция, а не молча падает в шлюз правок). `series_dir` передан явно
    (ack-роут берёт его из ack-state, а не резолвит по reply_to)."""
    from notary.lib import reply_understand  # noqa: PLC0415
    from notary.lib import mixed_reply_split  # noqa: PLC0415
    # ОЖИД1: пачка → лёгкая реакция ДО долгого ИИ-разбора (десятки секунд), не молчим.
    if mixed_reply_split.looks_like_multi_status(text):
        send_message(token, chat_id, UNDERSTAND_PROGRESS_HINT, reply_to=msg.get("message_id"))
    try:
        result = reply_understand.understand_reply(
            text, series_dir=series_dir, replier=replier,
            snapshot=snapshot, orig_ts=msg.get("date"), date=_reply_date_str(msg),
            source="understand", msg_id=msg.get("message_id"), chat_id=chat_id,
        )
    except Exception as e:  # noqa: BLE001 — understand не должен валить listener
        logger.exception("[understand] разбор reply упал: %s", e)
        return False
    if not result.get("engaged"):
        return False  # understand ничего не нашёл → обычные роуты ниже
    # 1) Настройка серии (R12/И2): владельцу форвард; ответ сотруднику схлопнут в ЕДИНЫЙ
    #    ack ниже (Ф4: больше не второе сообщение CONFIG_FORWARDED_REPLY).
    if result.get("config_requests"):
        _forward_config_to_owner(series, result["config_requests"])
    # 2) Правки (РИСК1): в существующую петлю правок с подавленным ack.
    result["_edit_queued"] = _forward_understand_edits(
        token, chat_id, allowed_chat, msg, result.get("edits") or [])
    # 3) ИИ недоступен (R9): очевидное уже применено, остальное в очереди — честная отбивка.
    if result.get("deferred"):
        if result.get("status_groups"):
            _send_understand_ack(token, chat_id, msg, result, series_dir=series_dir,
                                 snapshot=snapshot, replier=replier, edit_as_hint=False)
        send_message(token, chat_id, UNDERSTAND_DEFERRED_HINT, reply_to=msg.get("message_id"))
        logger.info("[understand] reply отложен (ИИ недоступен) series=%s", series)
        return True
    # 4) Ф4 (R13-R16/R26): ЕДИНЫЙ ack — «применил / не понял / не привязал» + переспрос +
    #    канал несогласия. engaged=True гарантирует ≥1 часть, ничего молча.
    _send_understand_ack(token, chat_id, msg, result, series_dir=series_dir,
                         snapshot=snapshot, replier=replier, edit_as_hint=False)
    logger.info(
        "[understand] reply разобран series=%s applied=%d questions=%d edits=%d config=%d",
        series, len(result.get("applied") or []), len(result.get("questions") or []),
        len(result.get("edits") or []), len(result.get("config_requests") or []),
    )
    return True


def maybe_route_to_ack_reply(
    token: str, chat_id: Optional[int], msg: dict[str, Any],
    allowed_chat: Optional[int] = None,
) -> bool:
    """Ф4 (R26/УПУ1 + R15): reply человека НА ack/переспрос бота.

    Раньше reply на бот-сообщение с неизвестным префиксом уходил в игнор
    (`process_message`) — обещание ступени-2 «поправьте, если не так» было ЛОЖНЫМ (некуда
    поправить). Теперь по `message_id` бот-сообщения ищем персистентный контекст
    ([[reply_ack_state]], переживает рестарт — RAZM6):
      • «не так / верни» на применённое ступени-2 → РЕАЛЬНЫЙ reopen (R26);
      • ответ цифрой / «да, кроме 3» / класс словом на переспрос → применяется (R15);
      • подтверждённый класс слова → память `remember_word_class` (R24-триггер).

    Стоит ПЕРВЫМ из reply-роутов (до дайджеста/understand): ack бота — не протокол и не
    дайджест, те роуты его серию не нашли бы, а fallthrough увёл бы «верни» в игнор. Гейт
    `ENABLE_REPLY_UNDERSTAND` дефолт-OFF: OFF → False (state и не пишется). Нет записи по
    этому message_id → False → обычные роуты (не наш ack). Опасная тройка: текст не логируем."""
    if chat_id is None:
        return False
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return False
    mid = reply_to.get("message_id")
    if mid is None:
        return False
    try:
        from notary.lib import reply_understand, reply_ack, reply_ack_state  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — модули не грузятся → фича выключена
        logger.debug("[ack-reply] import failed (feature off?): %s", e)
        return False
    if not reply_understand.is_enabled():
        return False  # R10: дефолт-OFF → state не пишется, обычные роуты
    state = reply_ack_state.get(mid)
    if not state:
        return False  # не reply на НАШ ack → обычные роуты (не перехватываем чужое)
    # Кросс-чатовая защита: Telegram нумерует message_id ПО-ЧАТНО, а state глобальный.
    # Бот живёт в личке владельца + группах серий (cid ≠ allowed_chat), их message_id
    # свободно совпадают. Без сверки chat_id reply в чате B на сообщение с тем же id, что
    # у нашего ack в чате A, увёл бы «верни»/«да» в ЧУЖУЮ серию (порча статусов). Сверяем.
    st_chat = state.get("chat_id")
    if st_chat is not None and st_chat != chat_id:
        return False  # тот же message_id, но другой чат → не наш ack → обычные роуты
    # Текст ответа. ISS-47: голосовой ответ на ВОПРОС бота (переспрос) — РАСШИФРОВЫВАЕМ,
    # а не теряем. Раньше voice на ack молча падал сквозь все роуты («да» на «Закрыть все
    # вопросы?» игнорировался). Транскрипция — тот же Groq-путь и флаг, что у дайджест-
    # голоса; делаем ПОСЛЕ подтверждения «это наш ack» — чужой голос не транскрибируем.
    text = (msg.get("text") or "").strip()
    if not text and (msg.get("voice") or msg.get("audio")):
        if _digest_voice_reply_enabled():
            text = (transcribe_voice_or_none(token, msg) or "").strip()
        if not text:
            # Наш ack, но голос не расшифровался / флаг OFF → не молчим (мы обещали ответ).
            send_message(token, chat_id,
                         "Не расслышал голос — ответьте, пожалуйста, текстом: «да», «нет» "
                         "или цифрой.", reply_to=msg.get("message_id"))
            return True
    if not text:
        return False  # стикер/фото на наш ack — не канал закрытого ответа

    # ISS-51 (Ф3): ack-роут стоит ПЕРВЫМ из reply-роутов, и до Ф3 гейта режима у него не
    # было вовсе — в шпаргалочной серии человек слышал «Не нашёл, что откатить —
    # уточните» и «Ок, ничего не отметил» вместо обещанной справочной фразы. Гейтим
    # ЗДЕСЬ, а не в `reply_ack`: серия известна из ack-state (`series_dir`), и это
    # единственная точка, через которую проходят оба ack-сценария (ответ на вопрос
    # бота и свежая инструкция). Не-статусный ответ («да» на «перевыпустить протокол?»,
    # правка текста) режимом НЕ трогается — он не про учёт вопросов.
    _ack_series_dir = state.get("series_dir")
    _ack_series = Path(_ack_series_dir).name if _ack_series_dir else None
    if _ack_series and _is_last_only_series(_ack_series) and _looks_like_status_talk(text):
        send_message(token, chat_id, LAST_ONLY_STATUS_REPLY, reply_to=msg.get("message_id"))
        logger.info("[ack-reply] серия %s в режиме last-only — статус НЕ трогаю, "
                    "ответил справкой (chat=%s)", _ack_series, chat_id)
        return True

    # ISS-49 (реальный кейс 24.07): под нашим сообщением может ждать ЗАКРЫТЫЙ вопрос
    # («Закрыть все ваши вопросы?») — тогда это ОТВЕТ на него. А может НЕ ждать (reply на
    # финальное «Готово — отметил…») — тогда это СВЕЖАЯ инструкция («закрой всё у всех,
    # кроме агента»). Раньше свежая инструкция под «Готово» вообще не ловилась (у финала
    # нет ack-state) и молча тонула в шлюзе правок. Теперь: нет ждущего вопроса и это не
    # откат/не «ок» → гоним тем же understand-движком, что reply на протокол.
    ans = reply_ack.parse_answer(text)
    fresh_instruction = (
        not state.get("question")
        and not ans["rollback"]
        and not ans["yes"]
        and not ans["no"]
        and not ans["digits"])
    if fresh_instruction:
        series_dir = state.get("series_dir")
        series = Path(series_dir).name if series_dir else None
        if series:
            handled = _understand_and_ack(
                token, chat_id, allowed_chat, msg, series=series,
                series_dir=Path(series_dir), snapshot=None,
                replier=state.get("replier") or _msg_display_name(msg), text=text)
            if handled:
                # Свежая инструкция под старым ack — прежний контекст больше не нужен
                # (понимание перешло на новое сообщение бота, оно и записало свой state).
                reply_ack_state.consume(mid)
                logger.info("[ack-reply] reply на ack понят как свежая инструкция (understand)")
                return True
        # understand ничего не нашёл / серия не резолвится → падаем в обычный handle ниже
        # (мягкая подсказка, не молчание).

    try:
        res = reply_ack.handle_ack_reply(state, text)
    except Exception as e:  # noqa: BLE001 — обработка ответа не должна валить listener
        logger.exception("[ack-reply] обработка ответа упала: %s", e)
        return False
    if res.get("consume"):
        reply_ack_state.consume(mid)
    out = res.get("text") or ""
    if out:
        out_mid = send_message(token, chat_id, out, reply_to=msg.get("message_id"))
        # Персистим контекст под НАШИМ ответом, чтобы reply на него не осиротел:
        #  • есть новый вопрос (ISS-48: ответ шире вопроса → показали список, ждём «да»);
        #  • ЛИБО просто применили статусы (ISS-49, кейс 24.07): reply на финальное
        #    «Готово — отметил…» должен ловиться — иначе свежая инструкция под ним молча
        #    тонет в шлюзе правок. applied → и откат «верни» работает, и re-understand.
        applied_texts = res.get("applied_texts") or []
        applied_recs = [{"text": t, "label": None} for t in applied_texts if t]
        if out_mid is not None and (res.get("question") or applied_recs):
            reply_ack_state.record(
                out_mid, series_dir=state.get("series_dir"), chat_id=chat_id,
                replier=state.get("replier"), applied=applied_recs or None,
                has_tier2=False, question=res.get("question"), date=state.get("date"))
    logger.info("[ack-reply] обработан ответ на ack mid=%s consume=%s learned=%s",
                mid, res.get("consume"), res.get("learned"))
    return True


def _learning_digest_prefix() -> str:
    """Префикс 🧠 дайджеста самообучения — из `feedback_learning.DIGEST_PREFIX`
    (один источник истины). Fallback на литерал, если модуль не импортируется."""
    try:
        from notary.lib.feedback_learning import DIGEST_PREFIX  # noqa: PLC0415
        return DIGEST_PREFIX
    except Exception:
        return "\U0001F9E0"


def _reissue_ack_prefix() -> str:
    """Ф4 (R16): префикс сводного inline-подтверждения «✅ Применил правки …» —
    из `feedback_learning.REISSUE_ACK_PREFIX` (один источник истины). Reply владельца
    на это сообщение роутится в тот же откат, что и reply на дайджест. Fallback на
    литерал, если модуль не импортируется."""
    try:
        from notary.lib.feedback_learning import REISSUE_ACK_PREFIX  # noqa: PLC0415
        return REISSUE_ACK_PREFIX
    except Exception:
        return "✅ Применил правки. Заодно понял на будущее"


def maybe_route_to_learning_rollback(token: str, chat_id: int, msg: dict[str, Any]) -> bool:
    """Ф3а (REQ 2.3): reply на дайджест самообучения «🧠 Ватсон выучил …» → откат.

    Reply именно на этот дайджест обрабатывает ТОЛЬКО самообучение, не apply_reply
    встреч (📅): «откати <термин>» снимает совпавшие правила (`feedback_learning.
    apply_rollback_reply` — append-only `rollback`-событие, видно в логе), любой
    другой ответ («да»/«ок») — подтверждение, реестр не трогаем. Всегда True
    (сообщение прожёвано — caller выходит), кроме недоступного модуля → False
    (фича off, прежнее поведение). Импорт защищён, как у остальных роутов."""
    reply_text = (msg.get("text") or "").strip()
    if not reply_text:
        send_message(token, chat_id,
                     "Пустой ответ. Чтобы откатить выученное — «откати <термин>».",
                     reply_to=msg.get("message_id"))
        return True
    # Ф7 D4: команда «переноси … в контекст» на дайджесте знания — запоминаем
    # ратчет private→company (kind=term: дайджест самообучения про написания/
    # термины). Впредь такое знание роутится в `*-context` (через PR §3.3).
    # Распознаём ДО отката — триггеры не пересекаются («переноси/в контекст» ≠
    # «откати/забудь»). Best-effort: сбой не валит обработку реплая.
    try:
        from notary.lib import knowledge_ratchet  # noqa: PLC0415
        promo = knowledge_ratchet.parse_promote_command(reply_text)
    except Exception as e:  # noqa: BLE001
        logger.debug("knowledge_ratchet import/parse failed (feature off?): %s", e)
        promo = None
    if promo is not None:
        scope = promo.get("scope") or "kind"
        if scope == "key":
            # «только это»: разовое повышение КОНКРЕТНОГО факта — это per-fact
            # ре-роутинг, задел Ф8 (handoff §3.3 п.3). У реплая на дайджест нет
            # привязки к одному термину, поэтому key здесь неизвестен, а
            # remember_promotion(scope="key", key=None) молча выродился бы в
            # kind-level и повысил ВСЕ будущие термины — ровно вопреки «только
            # это» (fail-open). Fail-closed: правило НЕ пишем, объясняем владельцу.
            send_message(token, chat_id,
                         "Разовый перенос одного термина пока не поддержан. Чтобы "
                         "ВПРЕДЬ такие термины уходили в контекст — «переноси в контекст».",
                         reply_to=msg.get("message_id"))
            return True
        try:
            knowledge_ratchet.remember_promotion(
                "term", company=promo.get("company"), scope="kind")
        except Exception as e:  # noqa: BLE001
            logger.warning("ratchet promote failed (non-fatal): %s", e)
        where = f" ({promo['company']})" if promo.get("company") else ""
        send_message(token, chat_id,
                     f"Принял: впредь такие термины — в контекст компании{where} (через PR). "
                     "Если что-то выучено неверно — «откати <термин>».",
                     reply_to=msg.get("message_id"))
        return True
    try:
        from notary.lib import feedback_learning  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.debug("feedback_learning import failed (feature off?): %s", e)
        return False
    try:
        rolled = feedback_learning.apply_rollback_reply(reply_text)
    except Exception as e:  # noqa: BLE001
        logger.exception("learning rollback failed: %s", e)
        send_message(token, chat_id, "Не смог обработать откат — посмотри логи.",
                     reply_to=msg.get("message_id"))
        return True
    if rolled:
        # describe_rule — kind/scope-aware (терм серии / [везде] глобальное / смысл),
        # иначе смысловое/глобальное правило отрисовалось бы как ««None» → «None»».
        items = "; ".join(feedback_learning.describe_rule(r) for r in rolled)
        logger.info("learning rollback: снято правил=%d (%s)", len(rolled), items)
        send_message(token, chat_id,
                     f"Откатил ({len(rolled)}): {items}. Вернул прежнее состояние.",
                     reply_to=msg.get("message_id"))
    else:
        logger.info("learning digest reply без отката (подтверждение / нет совпадения)")
        send_message(token, chat_id,
                     "Принял, оставляю как выучил. Если что-то неверно — «откати <термин>».",
                     reply_to=msg.get("message_id"))
    return True


def process_callback_query(token: str, allowed_chat: int, cbq: dict[str, Any]) -> None:
    """Inline-кнопка от Ф3 (clarify спикеров), Ф5 (task_filter) или Ф6 (delivery).

    Порядок: Ф6 delivery (`cd:`) → Ф5 task_clarify (`tf:`/`td:`) → Ф3 clarify (`cl:`).
    Префиксы не пересекаются — порядок задаёт лишь чисто организационный.
    """
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

    # Ф6 delivery (cd:)
    try:
        from notary.lib import delivery_worker  # noqa: PLC0415
        handled = delivery_worker.process_callback(cbq, pending_root, token)
        if handled:
            return
    except Exception as e:  # noqa: BLE001
        logger.exception("delivery_worker.process_callback failed: %s", e)

    # Ф5 task_clarify (task_filter/task_deadlines)
    try:
        from notary.lib import task_clarify_worker  # noqa: PLC0415
        handled = task_clarify_worker.process_callback(cbq, pending_root, token)
        if handled:
            return
    except Exception as e:  # noqa: BLE001
        logger.exception("task_clarify_worker.process_callback failed: %s", e)

    # Ф8 vocab (vocab:approve_all/reject_all) — авто-словарь
    try:
        from notary.lib import vocab_worker  # noqa: PLC0415
        handled = vocab_worker.process_callback(cbq, pending_root, token)
        if handled:
            return
    except Exception as e:  # noqa: BLE001
        logger.exception("vocab_worker.process_callback failed: %s", e)

    # Ф3 clarify спикеров
    try:
        from notary.lib import clarify_worker  # noqa: PLC0415
        clarify_worker.process_callback(cbq, pending_root, token)
    except Exception as e:  # noqa: BLE001
        logger.exception("process_callback failed: %s", e)


def _warm_reissue_imports() -> None:
    """УПУ1: прогрев ленивых импортов, достижимых из `reissue_one` в фоновом потоке.

    `reissue_one` лениво импортирует `llm_postprocess`, `series_memory`
    (feedback_reissue.py:~630) и `feedback_learning` (~645). Первый импорт модуля
    из НЕ-главного потока несёт риск import-lock дедлока (CPython держит блокировку
    на время выполнения тела модуля; если главный поток в это время держит другой
    замок — взаимоблокировка). Принудительно импортируем их В ГЛАВНОМ ПОТОКЕ на
    старте, до создания исполнителя. Best-effort: сбой логируем, не валим старт
    (фоновый путь тогда импортирует сам — риск ниже, чем не подняться вовсе).
    """
    for modname in (
        "notary.lib.llm_postprocess",
        "notary.lib.series_memory",
        "notary.lib.feedback_learning",
        "notary.lib.feedback_reissue",
    ):
        try:
            __import__(modname)
        except Exception as e:  # noqa: BLE001
            logger.warning("[reissue] прогрев импорта %s не удался (non-fatal): %s", modname, e)


def _send_reissue_start_notice(token: Optional[str], claimed: dict, *, root: Optional[Path] = None) -> None:
    """R11: уведомление участникам о старте перевыпуска (главный поток, при submit).

    Best-effort (УПУ2): сбой/таймаут send НЕ валит submit перевыпуска. Текст
    фиксированный (R9 — без вставок из правок). Один раз на раунд: флаг
    `reissue_notice_sent_round` в state выставляем ПОСЛЕ попытки send; повторный
    claim того же раунда (обычный поток) второе не шлёт. При reclaim после краша
    (новый attempt того же раунда — флаг уже стоит, повтор не шлём; новый раунд от
    конкурентного reply — round вырос, флаг устарел, «пересобираю» уйдёт снова и
    это желательно).

    R9: логируем только метаданные (fid, round) — без текста.
    """
    if not token:
        return  # тесты/совместимость — токен не передан, R11 не шлём
    from notary.lib import feedback_state  # noqa: PLC0415
    fid = claimed.get("feedback_id")
    rnd = claimed.get("round")
    chat_id = claimed.get("chat_id")
    if chat_id is None:
        return
    # Проверка «слать ли»: свежий state мог уже пометить этот раунд (повторный
    # claim того же раунда в обычном потоке). Читаем СВЕЖИЙ, не claimed-снимок.
    try:
        cur = feedback_state.read_state(fid, root=root) if fid else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[reissue] R11 read_state упал fid=%s (non-fatal): %s", fid, e)
        cur = None
    if cur is not None and cur.get("reissue_notice_sent_round") == rnd:
        logger.info("[reissue] R11 уже отправлено fid=%s round=%s — пропуск", fid, rnd)
        return
    # reply на сообщение протокола, если оно известно (последнее — самое свежее).
    mids = claimed.get("protocol_message_ids") or []
    reply_to = mids[-1] if mids else None
    try:
        send_message(token, int(chat_id), _REISSUE_NOTICE_TEXT, reply_to=reply_to)
    except Exception as e:  # noqa: BLE001
        logger.warning("[reissue] R11 send упал fid=%s (non-fatal, submit продолжаем): %s", fid, e)
    # Флаг ставим ПОСЛЕ попытки send (один раз на раунд). Статус НЕ меняем —
    # state уже `reissuing` (claim прошёл), пишем только extra-поле через
    # mark_status на тот же валидный статус (R3 — мутация в главном потоке).
    if not fid:
        return
    try:
        feedback_state.mark_status(
            fid, "reissuing", root=root,
            extra={"reissue_notice_sent_round": rnd},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[reissue] R11 флаг не записан fid=%s (non-fatal): %s", fid, e)


def _process_reissues_async(
    *,
    executor: Optional[ThreadPoolExecutor],
    inflight: dict,
    token: Optional[str] = None,
    root: Optional[Path] = None,
    cap: int = _REISSUE_CAP,
    reissue_fn: Optional[Callable] = None,
) -> None:
    """Ф2: неблокирующая обработка перевыпусков. drain → claim → submit.

    Вызывается из `sweep_clarify_timeouts` СРАЗУ после `feedback_worker.sweep_timeouts()`
    (инвариант FM-10/R2: claim в том же проходе, без обработки сообщений между sweep и
    claim). Тяжёлый `reissue_one` уходит в `executor` (один воркер); claim и finalize —
    в главном потоке (R3).

    Параметризовано для тестов Ф3: `executor`/`inflight`/`reissue_fn` инъектируются;
    sweep зовёт с модульными глобалами и боевым `reissue_one`.

    Реестр `inflight`: fid → (future, claimed_state, submitted_monotonic).
    R9: логируем только метаданные (fid, status, round, длительность).
    """
    from notary.lib import feedback_reissue  # noqa: PLC0415

    # 1) DRAIN: завершённые future → finalize (главный поток), снять из реестра.
    # drained_fids — встречи, прошедшие drain/finalize в ЭТОМ проходе. Передаём их
    # в claim как skip (паритет с sync-обёрткой process_ready_reissues: «одна
    # попытка на встречу за sweep»). Иначе только что упавшая встреча (error→ready)
    # пере-заклеймится в том же проходе и сожжёт попытку back-to-back, держа слот
    # cap=1 и обделяя другие ready_for_reissue.
    drained_fids: set = set()
    for fid in list(inflight.keys()):
        future, claimed, submitted_ts = inflight[fid]
        if not future.done():
            continue
        try:
            res = future.result()
        except Exception as e:  # noqa: BLE001
            # Исключение из reissue_one — как в синхронной обёртке: error-revert.
            logger.exception("[reissue] фоновый reissue_one упал fid=%s: %s", fid, e)
            res = {"status": "error", "error": str(e)}
        try:
            status = feedback_reissue.finalize_reissue(fid, claimed, res, root=root)
        except Exception as e:  # noqa: BLE001
            logger.exception("[reissue] finalize fid=%s упал: %s", fid, e)
            status = None
        inflight.pop(fid, None)
        drained_fids.add(fid)
        dur = time.monotonic() - submitted_ts
        logger.info(
            "[reissue] drain fid=%s status=%s round=%s dur=%.1fs",
            fid, status, claimed.get("round"), dur,
        )

    # Без исполнителя (на всякий случай) — claim/submit не делаем: тяжёлую часть
    # некуда отправить, а синхронно звать нельзя (заблокирует листенер).
    if executor is None:
        return

    # 2) CLAIM: только если есть свободная ёмкость. skip_fids = живые in-flight
    # (РИСК1 — ОБЯЗАТЕЛЬНО: reclaim внутри claim_ready_reissues не должен сбросить
    # живую генерацию дольше 900с → иначе пере-claim и двойная доставка).
    free = max(0, cap - len(inflight))
    if free <= 0:
        return
    # skip = живые in-flight | прошедшие drain в этом проходе (паритет с sync:
    # упавшая встреча ждёт следующий sweep, не пере-заклеймливается тут же).
    skip_fids = set(inflight.keys()) | drained_fids
    try:
        claimed_list = feedback_reissue.claim_ready_reissues(
            root=root, max_n=free, skip_fids=skip_fids,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[reissue] claim упал: %s", e)
        return

    # 3) SUBMIT: каждая claimed-встреча → фоновый reissue_one; R11 уведомление.
    fn = reissue_fn or feedback_reissue.reissue_one
    for claimed in claimed_list:
        fid = claimed.get("feedback_id")
        if not fid:
            continue
        # R11 уведомление о старте (best-effort, до submit — участник видит реакцию
        # сразу). Сбой не валит submit (УПУ2).
        _send_reissue_start_notice(token, claimed, root=root)
        try:
            future = executor.submit(fn, claimed, root=root)
        except Exception as e:  # noqa: BLE001
            # Submit упал (например, executor уже shutdown) — НЕ оставляем state в
            # `reissuing` навсегда: reclaim (≤900с) вернёт в очередь. Логируем и идём.
            logger.exception("[reissue] submit fid=%s упал: %s", fid, e)
            continue
        inflight[fid] = (future, claimed, time.monotonic())
        logger.info("[reissue] submit fid=%s round=%s → фон", fid, claimed.get("round"))


def _maybe_cleanup_dormant_states() -> None:
    """R10: дешёвый троттл-вызов уборки старых dormant-state'ов (не чаще раза в сутки).

    best-effort: сбой не валит sweep. Логируем число удалённых (без имён, R9).
    """
    global _last_dormant_cleanup_mono
    now_mono = time.monotonic()
    if _last_dormant_cleanup_mono is not None and \
            now_mono - _last_dormant_cleanup_mono < _DORMANT_CLEANUP_EVERY_S:
        return
    _last_dormant_cleanup_mono = now_mono
    try:
        from notary.lib import feedback_state as _fs  # noqa: PLC0415
        removed = _fs.cleanup_dormant_states()
        if removed:
            logger.info("[reissue] dormant cleanup: удалено %d старых state-файлов", removed)
    except Exception as e:  # noqa: BLE001
        logger.exception("[reissue] dormant cleanup упал (non-fatal): %s", e)


def sweep_clarify_timeouts(token: Optional[str] = None) -> None:
    pending_root = _clarify_pending_root()
    if pending_root is None or not pending_root.exists():
        return
    try:
        from notary.lib import clarify_worker  # noqa: PLC0415
        n = clarify_worker.sweep_timeouts(pending_root)
        if n:
            logger.info("clarify sweep: %d изменений (pending→timed_out и timed_out→archived)", n)
    except Exception as e:  # noqa: BLE001
        logger.exception("clarify sweep failed: %s", e)
    # Ф5 task_clarify sweep — отдельный модуль, отдельные state-файлы.
    try:
        from notary.lib import task_clarify_worker  # noqa: PLC0415
        n2 = task_clarify_worker.sweep_timeouts(pending_root)
        if n2:
            logger.info("task-clarify sweep: marked %d as timed_out", n2)
    except Exception as e:  # noqa: BLE001
        logger.exception("task_clarify sweep failed: %s", e)
    # Ф6 delivery sweep — отдельный модуль, отдельные state-файлы (`-delivery.json`).
    # RISK5 (Ф1 delivery-fixes): фича «бот спрашивает куда слать» снята; проактивно
    # ретайрим осиротевшие delivery-state'ы, чтобы они не перехватывали ответы владельца.
    try:
        from notary.lib import delivery_worker  # noqa: PLC0415
        n_orphan = delivery_worker.retire_orphan_delivery_states(pending_root)
        if n_orphan:
            logger.warning("delivery sweep: ретайрнул %d орфан-ask state(s) (фича снята)", n_orphan)
        n3 = delivery_worker.sweep_timeouts(pending_root)
        if n3:
            logger.info("delivery sweep: marked %d as timed_out", n3)
    except Exception as e:  # noqa: BLE001
        logger.exception("delivery sweep failed: %s", e)
    # Ф8 vocab sweep — запросы старше 48ч авто-отклоняются (свои state в vocab/).
    try:
        from notary.lib import vocab_worker  # noqa: PLC0415
        n4 = vocab_worker.sweep_timeouts(pending_root)
        if n4:
            logger.info("vocab sweep: %d просроченных авто-отклонено", n4)
    except Exception as e:  # noqa: BLE001
        logger.exception("vocab sweep failed: %s", e)
    # Ф3 feedback sweep — окно сбора правок закрылось (дебаунс/потолок) →
    # ready_for_reissue (свой каталог `_feedback_edits/`, не пересекается с clarify).
    try:
        from notary.lib import feedback_worker  # noqa: PLC0415
        n5 = feedback_worker.sweep_timeouts()
        if n5:
            logger.info("feedback sweep: %d окон закрыто → ready_for_reissue", n5)
    except Exception as e:  # noqa: BLE001
        logger.exception("feedback sweep failed: %s", e)
    # Ф2 feedback reissue — НЕБЛОКИРУЮЩИЙ путь: drain завершённых → finalize
    # (главный поток), claim ready_for_reissue (СРАЗУ после feedback-sweep в этом
    # же проходе — инвариант FM-10/R2, без обработки сообщений между sweep и claim),
    # submit тяжёлого `reissue_one` в фоновый воркер. Генерация claude (200–600с)
    # больше НЕ блокирует листенер — он остаётся отзывчивым (R1). РИСК1: skip_fids
    # живых future пробрасывается в claim/reclaim внутри _process_reissues_async.
    try:
        _process_reissues_async(
            executor=_reissue_executor,
            inflight=_reissue_inflight,
            token=token,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("feedback reissue async failed: %s", e)
    # И1 — drain командных claude-job'ов (protocol/correction/apply-reply): снимаем
    # завершённые из реестра (результат job доставил сам, I5), освобождаем слот воркера.
    # В ТОМ ЖЕ проходе sweep, в главном потоке (R3 — реестр мутирует только главный поток).
    try:
        _drain_command_jobs(inflight=_command_inflight)
    except Exception as e:  # noqa: BLE001
        logger.exception("command jobs drain failed: %s", e)
    # R10 — уборка старых dormant-state'ов (дешёвый троттл, не чаще раза в сутки).
    _maybe_cleanup_dormant_states()


def _series_with_deferred_queue() -> list[Path]:
    """R9: серии с НЕПУСТОЙ очередью дозабора understand (`reply-understand-queue.json`).

    Дёшево: один glob по каталогу встреч + быстрая проверка pending. В обычном режиме
    (ИИ доступен) очередь пуста → пустой список, дренаж не запускается. Очередь копится
    лишь после недоступности claude на статус-reply."""
    from notary.lib import reply_understand_queue as ruq  # noqa: PLC0415
    root = _protokol_root()
    if not root.exists():
        return []
    out: list[Path] = []
    try:
        for qf in sorted(root.glob("*/" + ruq.QUEUE_FILE)):
            try:
                if ruq.list_pending(qf.parent):
                    out.append(qf.parent)
            except Exception:  # noqa: BLE001 — битый sidecar одной серии не роняет обход
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("[understand] обход очередей дозабора не удался (non-fatal): %s",
                       type(e).__name__)
    return out


def _sweep_drain_deferred(token: Optional[str] = None) -> None:
    """R9 (Ф6 деплой-блокер, follow-ups-Ф3-1): подключение дренажа очереди дозабора к
    sweep-циклу. Без него хинт сотруднику «разберу позже» = ложь — reply гниёт в очереди
    до TTL 14д и молча пропадает.

    • Гейт `ENABLE_REPLY_UNDERSTAND` дефолт-OFF → OFF → выход (очередь не наполняется).
    • Разбор отложенного зовёт claude (до `_split_timeout`) → уводим в ФОНОВЫЙ воркер
      через `_submit_command_job`: один claude за раз (общий `_reissue_executor`),
      главный поток и heartbeat НЕ блокируются (иначе watchdog перезапустит листенер).
    • Троттл `DRAIN_DEFERRED_EVERY_S` + дедуп по метке — не задваиваем и не хаммерим ИИ.
    • Опасная тройка: в лог только счётчики/серии, НЕ тексты отложенных reply."""
    global _last_deferred_drain_mono
    try:
        from notary.lib import reply_understand  # noqa: PLC0415
        if not reply_understand.is_enabled():
            return
        now_mono = time.monotonic()
        if _last_deferred_drain_mono is not None and \
                now_mono - _last_deferred_drain_mono < DRAIN_DEFERRED_EVERY_S:
            return
        if _command_label_inflight("understand-drain"):
            return  # предыдущий дренаж ещё в работе — не задваиваем claude
        series_dirs = _series_with_deferred_queue()
        if not series_dirs:
            return
        _last_deferred_drain_mono = now_mono

        def _job() -> None:
            processed = failed = 0
            for sd in series_dirs:
                try:
                    r = reply_understand.drain_deferred(sd)
                except Exception as e:  # noqa: BLE001 — сбой одной серии не роняет дренаж
                    logger.warning("[understand] дренаж серии не удался (non-fatal): %s",
                                   type(e).__name__)
                    continue
                processed += int(r.get("processed", 0))
                failed += int(r.get("failed", 0))
            if processed or failed:
                logger.info("[understand] дренаж очереди дозабора: разобрано=%d осталось=%d серий=%d",
                            processed, failed, len(series_dirs))

        _submit_command_job(
            _reissue_executor, _command_inflight,
            _next_command_job_id("understand-drain"), "understand-drain", _job)
    except Exception as e:  # noqa: BLE001 — sweep-врезка не роняет главный цикл
        logger.exception("understand drain sweep failed: %s", e)


def _job_apply_reply(
    token: str,
    chat_id: int,
    msg_id: Optional[int],
    reply_text: str,
    reply_date: Any,
    snapshot_path: Path,
) -> None:
    """И1 (I3): heavy-часть apply-reply — В ФОНОВОМ ВОРКЕРЕ (после проверки snapshot).

    Самодостаточен (I5): subprocess `meetings_apply_reply` (claude --print внутри,
    до 180с) → parse JSON → send сводки/ошибки (как раньше синхронный хвост
    process_message). Реестр не мутирует.

    Heartbeat здесь НЕ нужен: главный поток больше не блокируется этим subprocess'ом
    (он в воркере), он продолжает крутить цикл и бить heartbeat сам — watchdog спокоен.

    R9: НЕ логируем reply_text/сводку. Pre-existing лог `apply-reply rc=… stdout=out[:600]`
    перенесён КАК ЕСТЬ (он был и до И1 — это не новый код).
    """
    cmd = [
        sys.executable, "-m", "notary.meetings_apply_reply",
        "--reply", reply_text,
        "--snapshot", str(snapshot_path),
    ]
    if reply_date:
        cmd += ["--reply-date", str(reply_date)]

    logger.info("apply-reply: reply_len=%d reply_date=%s", len(reply_text), reply_date)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        logger.error("meetings_apply_reply timeout 180s")
        send_message(token, chat_id, "Ответ не применился — таймаут apply-reply (180s).", reply_to=msg_id)
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("meetings_apply_reply subprocess упал: %s", e)
        send_message(token, chat_id, f"Ответ не применился — сбой apply-reply: {type(e).__name__}.", reply_to=msg_id)
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
        send_message(token, chat_id, f"Не применил: {err_msg}", reply_to=msg_id)
        return

    if no_decisions:
        send_message(
            token, chat_id,
            result.get("hint") or "В ответе не нашёл решений по номерам — переформулируй: «1 да в @t11, 2 нет».",
            reply_to=msg_id,
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
    send_message(token, chat_id, text, reply_to=msg_id)


def process_message(token: str, allowed_chat: int, msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    cid = chat.get("id")

    # ISS-7/REQ 7.4: команда смены чата серии «серию X шли сюда/в личку/в чат N».
    # ДО шлюза правок и DM-гейта — команду владелец может дать прямо в целевой
    # группе (cid != allowed_chat), а шлюз правок ниже «прожевал» бы любое не-reply
    # сообщение в группе. Owner-гейт и узкий триггер — внутри (обычную переписку и
    # команды коррекции не перехватывает).
    if maybe_route_to_set_chat_command(token, cid, allowed_chat, msg):
        return

    # Ф2 (ISS-51…55): «эта серия — шпаргалка / накопление», «в каком режиме серия?».
    # СРАЗУ ПОСЛЕ смены чата и по тем же причинам: это настройка СЕРИИ, её дают
    # прямо в групповом чате (cid != allowed_chat), а шлюз правок ниже прожевал бы
    # такое сообщение. Порядок остальных роутов не тронут. Команду смены чата этот
    # роут не крадёт (там обязательна цель «сюда/в личку/в чат N», здесь — слово
    # режима на месте приказа); owner-гейт и узкий триггер — внутри.
    if maybe_route_to_mode_command(token, cid, allowed_chat, msg):
        return

    # Ф4 (watson, R26/R15): reply человека НА ack/переспрос бота. ПЕРВЫМ из reply-роутов —
    # ack бота не протокол и не дайджест, иначе «не так/верни»/«ответ цифрой» утекли бы в
    # игнор. Ищет контекст ack по message_id (переживает рестарт). Гейт ENABLE_REPLY_UNDERSTAND
    # дефолт-OFF: OFF / не наш ack → False → обычные роуты ниже (обратная совместимость).
    if maybe_route_to_ack_reply(token, cid, msg, allowed_chat):
        return

    # ISS-45: reply-просьба «пришли протокол / открытые вопросы» → переотправить артефакт.
    # ПОСЛЕ ack-роута и ДО digest-роута: иначе digest-роут проглотил бы reply на дайджест
    # первым. Узкий триггер (глагол отправки + объект); статус-ответ уступает роутам ниже.
    if maybe_route_to_send_artifact(token, cid, allowed_chat, msg):
        return

    # Ф4 (watson, R11): reply на УТРЕННИЙ ДАЙДЖЕСТ «Открытые вопросы к встрече».
    # Стоит ПЕРВЫМ из reply-роутов и прожёвывает такой ответ целиком: дайджест — не
    # протокол, шлюзу правок в нём нечего править, и в групповом чате (куда дайджест и
    # приходит) он молча дропнул бы ответ участника. Статус → в тот же sidecar Ф2;
    # голос / нераспознанное → подсказка, но НЕ молчание: канал сам просил ответить.
    if maybe_route_to_digest_reply(token, cid, msg):
        return

    # R27 (watson, решение владельца 21.07): голосовой reply на ПРОТОКОЛ серии →
    # расшифровка ПЕРЕД разбором (под флагом `ENABLE_PROTOCOL_VOICE_REPLY`, дефолт-OFF),
    # чтобы голос шёл тем же путём понимания, что текст. OFF / не-голос / не-протокол →
    # msg без изменений (обратная совместимость: голос уходит в шлюз правок как раньше).
    msg = _maybe_transcribe_protocol_voice(token, cid, msg)

    # Ф3 (watson, R7-R12/R24): ЕДИНАЯ точка разбора reply на протокол (understand-слой).
    # Гейт `ENABLE_REPLY_UNDERSTAND` дефолт-OFF (R10): OFF → сразу False, mixed/status/шлюз
    # правок ниже работают как сегодня. ON → ИИ понимает reply, детерминированные проверки
    # страхуют, при расхождении бот переспрашивает (структуру отдаёт, рендер вопроса — Ф4).
    if maybe_route_to_reply_understand(token, cid, allowed_chat, msg):
        return

    # Ф5 (ISS-33): СМЕШАННЫЙ reply «правки текста + статусы задач» одним сообщением.
    # ДО статус-роута и шлюза правок — иначе статус-роут распознал бы ОДИН статус,
    # вернул True и прожевал бы всё сообщение (правки и второй статус молча пропали
    # бы — корень ISS-33, кейс Ольги). Гейт `ENABLE_MIXED_REPLY_SPLIT` дефолт-OFF:
    # OFF → сразу False (текущее поведение); ON и reply смешанный → обрабатывает обе
    # части + один объединённый ack → True. Чистый статус/правка → False → роуты ниже.
    if maybe_route_to_mixed_reply(token, cid, allowed_chat, msg):
        return

    # Ф5 (REQ R9/R5/A10): reply со СТАТУСОМ висяка («этот закрыт» / «снимаем» /
    # «жду» / «не закрыто, верни X»). ДО шлюза правок — иначе шлюз прожевал бы его
    # как правку текста. Перехватывает консервативно (однозначный статус-reply на
    # протокол серии); неоднозначное → False → шлюз правок ниже (A9). Любой участник
    # чата (A4). Так же, как шлюз правок, работает и в группе (cid != allowed_chat).
    if maybe_route_to_pending_status_reply(token, cid, allowed_chat, msg):
        return

    # Ф3 шлюз правок (FB1): reply на доставленный протокол в ЛЮБОМ чате серии.
    # Проверяем ДО DM-гейта — правки приходят в групповые чаты (cid != allowed_chat),
    # которые ниже отсекаются. Шлюз сам логирует «не reply на протокол — пропуск».
    if maybe_route_to_feedback_reply(token, cid, allowed_chat, msg):
        return

    if cid != allowed_chat:
        logger.info("skip: chat_id=%s ≠ allowed=%s", cid, allowed_chat)
        return

    # Ф4 (REQ 4.1): голосовой ответ. Скачиваем `.oga`, транскрибируем и
    # подставляем как `text` — дальше идёт обычный dispatch (reply-матчинг по
    # message_id для clarify тоже работает, т.к. `reply_to_message` сохраняется).
    # Транскрибация недоступна → отвечаем (не молчим) и выходим.
    if msg.get("voice"):
        voice_text = transcribe_voice_or_none(token, msg)
        if not voice_text or not voice_text.strip():
            send_message(token, cid, VOICE_FALLBACK_MSG, reply_to=msg.get("message_id"))
            logger.info("voice: транскрибация недоступна → отправлен фолбэк")
            return
        msg = dict(msg)
        msg["text"] = voice_text
        msg.pop("voice", None)
        logger.info("voice → text: chars=%d", len(voice_text))

    # Сначала — Ф4 команда «протокол <series> <date>». Это явная команда
    # с фиксированным синтаксисом, проверяется до clarify/apply_reply.
    # Защита от ложноположительных встроена в parse_protocol_command:
    # нужны и series, и дата, и глагол/слово-маркер, и пустой хвост.
    if maybe_route_to_protocol_command(token, cid, msg):
        return

    # Ф6 команды коррекции: «удали задачу N из …» / «поправь протокол …»/
    # «<series> <date>: задачу X не было». ПОСЛЕ Ф4 — у parse_protocol_command
    # тот же синтаксис «протокол <series> <date>», и у Ф4 приоритет.
    if maybe_route_to_correction_command(token, cid, msg):
        return

    # Reply-привязка опциональна. Reply на сообщение бота:
    #   • начинается с 📅 → блок встреч → apply_reply flow (ниже).
    #   • начинается с 🎙 → clarify-уведомление Ф3 → роутинг в clarify_worker.
    #   • что-то ещё → игнор (Илья ответил на чужую служебку бота).
    # Если Reply нет — Илья просто написал в DM. Сначала пробуем Ф5 task-clarify
    # (формат «1=2026-06-05» или «убрать 3,5,7»), потом Ф3 clarify спикеров,
    # иначе apply_reply flow к последнему snapshot.
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
        # Ф3а: reply на дайджест самообучения «🧠 Ватсон выучил …» → откат/подтверждение.
        # Ф4 (R16): reply на сводное подтверждение перевыпуска «✅ Применил правки …»
        # — ТОТ ЖЕ откат (владелец отвечает «откати …» прямо на подтверждение). ДО
        # проверки 📅, чтобы не утечь в apply_reply встреч (разные каналы).
        if (orig_text.startswith(_learning_digest_prefix())
                or orig_text.startswith(_reissue_ack_prefix())):
            maybe_route_to_learning_rollback(token, cid, msg)
            return
        if not orig_text.startswith(TRIGGER_PREFIX):
            return
    else:
        # Ф6 delivery: текстовый ответ «chat_id / ссылка / личка / никуда».
        # Имеет приоритет — формат уникальный (число / t.me/c-ссылка), не
        # пересекается ни с Ф5 task-clarify («N=…» / «убрать N»), ни с Ф3.
        if maybe_route_to_delivery_text(token, msg):
            return
        # Ф5 task-clarify имеет приоритет: формат «N=...» / «убрать N» уникальные.
        if maybe_route_to_task_clarify_text(token, msg):
            return
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

    # И1 (I3): heavy-часть apply-reply (subprocess meetings_apply_reply, claude --print
    # внутри, до 180с) → фоновый воркер (тот же `_reissue_executor`, ≤1 claude одновременно,
    # I4). Главный поток больше НЕ блокируется — heartbeat бьётся из цикла сам, watchdog
    # спокоен (раньше тут был heartbeat() перед блокирующим subprocess — больше не нужен).
    # I7: queue-aware ack, если воркер занят. На пустой очереди ack короткий, чтобы не
    # шуметь на быстром happy-path (раньше ack тут вообще не было — сводка приходила
    # сразу). При занятом воркере — предупреждаем, иначе пользователь ждёт молча.
    msg_id = msg.get("message_id")
    if _executor_busy():
        send_message(
            token, cid,
            "✏️ Принял — применяю ответ. В очереди за текущей задачей, пришлю сводку, как освобожусь.",
            reply_to=msg_id,
        )
    job_id = _next_command_job_id("apply-reply")
    _submit_command_job(
        _reissue_executor, _command_inflight, job_id, "apply-reply",
        lambda: _job_apply_reply(token, cid, msg_id, reply_text, reply_date, snapshot),
    )


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

    # Ф2: фоновый исполнитель перевыпуска. УПУ1 — прогрев ленивых импортов В
    # ГЛАВНОМ ПОТОКЕ ДО создания исполнителя (снимает риск import-lock дедлока при
    # первом импорте из фонового потока). Один воркер (A2/A4: один claude за раз).
    global _reissue_executor
    _warm_reissue_imports()
    _reissue_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reissue")

    def _on_term(signum, frame):  # noqa: ANN001, ARG001
        # R8/РИСК2: shutdown — best-effort/наблюдаемость, НЕ гарантия. Реальную
        # целостность даёт reclaim_stale_reissuing (≤900с) — оборванный `reissuing`
        # вернётся в `ready_for_reissue` на следующем старте. Здесь только лог числа
        # оборванных in-flight (метаданные, R9).
        logger.warning("[reissue] SIGTERM: оборвано in-flight перевыпусков=%d "
                       "командных job=%d (reissue: reclaim вернёт ≤900с; командные — "
                       "Илья переотправит, состояние не повреждено)",
                       len(_reissue_inflight), len(_command_inflight))
        if _reissue_executor is not None:
            _reissue_executor.shutdown(wait=False)
        # os._exit, а НЕ sys.exit: concurrent.futures.thread регистрирует
        # interpreter-level atexit `_python_exit`, который БЕЗУСЛОВНО join()-ит
        # воркер-потоки без таймаута. Обычный exit → atexit → процесс зависнет до
        # конца reissue_one (claude до 600с) или до SIGKILL по grace-периоду
        # systemd. os._exit минует atexit-join: оборванный future мгновенно
        # становится reclaim-кейсом (R8: целостность держит reclaim ≤900с, state
        # уже атомарно на диске, буферить нечего).
        os._exit(143)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError) as e:  # не главный поток / нет сигналов
        logger.debug("[reissue] SIGTERM handler не установлен: %s", e)

    def _shutdown_executor() -> None:
        if _reissue_executor is not None:
            _reissue_executor.shutdown(wait=False)
    atexit.register(_shutdown_executor)

    offset = load_offset()
    last_hb = time.time()
    last_sweep = 0.0

    while True:
        # Периодический sweep clarify-таймаутов (дёшево — только файловые ops).
        now_mono = time.monotonic()
        if now_mono - last_sweep >= SWEEP_EVERY_S:
            sweep_clarify_timeouts(token=token)
            _sweep_drain_deferred(token=token)  # R9 (watson Ф6): дренаж очереди дозабора understand
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
