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
    try:
        return feedback_worker.route_feedback_reply(token, chat_id, msg, allowed_chat=allowed_chat)
    except Exception as e:  # noqa: BLE001
        logger.exception("feedback routing failed: %s", e)
        return False


def _learning_digest_prefix() -> str:
    """Префикс 🧠 дайджеста самообучения — из `feedback_learning.DIGEST_PREFIX`
    (один источник истины). Fallback на литерал, если модуль не импортируется."""
    try:
        from notary.lib.feedback_learning import DIGEST_PREFIX  # noqa: PLC0415
        return DIGEST_PREFIX
    except Exception:
        return "\U0001F9E0"


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
        try:
            knowledge_ratchet.remember_promotion(
                "term", company=promo.get("company"), scope=promo.get("scope") or "kind")
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
    try:
        from notary.lib import delivery_worker  # noqa: PLC0415
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
        # ДО проверки 📅, чтобы не утечь в apply_reply встреч (разные каналы).
        if orig_text.startswith(_learning_digest_prefix()):
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
