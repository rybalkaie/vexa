#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""runner.py — за 5 минут до встречи запускает Vexa-бот на VPS.

Запускается launchd-агентом `com.ilarybalka.meeting-notary.runner` каждую минуту.

Алгоритм одного тика:
  1. Прочитать .queue.json (от scheduler-а).
  2. Прочитать .pause-until (через registry.pause_until() — она сама чистит stale).
  3. Найти кандидатов: start_at − now ∈ [0; 5min] и event_id не в state-файле.
  4. Если есть активная Vexa-сессия (определяем через ssh + docker ps) — пропускаем.
  5. Конфликт нескольких на одно время → берём первого по порядку в .queue.json
     (а очередь scheduler пишет в порядке появления в watched.yaml), остальным
     шлём push «не записал, конфликт с …».
  6. Атомарно занимаем state-файл (state.state_acquire).
  7. Дёргаем `ssh meeting-notary docker run ...` в background.
  8. Если запись one-off — после успешного старта помечаем enabled: false.

Не блокируется на длинном docker-run (запускаем через nohup &), runner возвращается
быстро — это важно, иначе launchd будет копить запуски каждую минуту.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.cli.registry import (  # noqa: E402
    DEFAULT_REGISTRY_DIR,
    find_watched,
    load_watched,
    pause_until,
    release_watched_lock,
    save_watched,
)
from notary.lib.notify import push  # noqa: E402
from notary.lib.state import (  # noqa: E402
    read_queue,
    read_state,
    state_acquire,
)

LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR") or "~/Library/Logs/meeting-notary"
))
LOG_FILE = LOG_DIR / "runner.log"
RUNS_DIR = LOG_DIR / "runs"  # per-meeting stdout/stderr

SSH_HOST = "meeting-notary"
BOT_IMAGE = "vexa-bot:notarius-telemost"
DOCKER_NETWORK = "vexa_vexa"
TRANSCRIPTS_VOLUME = "/home/dev/meeting-notary/_tmp/transcripts:/transcripts"
TRANSCRIPTION_URL = "http://172.17.0.1:8083/v1/audio/transcriptions"
# Ф3 (2026-05-29): live-whisper draft. Дефолт "0" — выключен (на CPU-VPS тонет,
# финальный протокол собирается из WAV через Speechmatics). Читается из
# EnvironmentFile=/srv/meeting-notary/.env.notary (см. systemd-юнит runner'а) и
# пробрасывается боту через -e ниже. =1 включить только при наличии GPU.
ENABLE_LIVE_DRAFT = os.environ.get("ENABLE_LIVE_DRAFT", "0")
REDIS_URL = os.environ.get("MEETING_NOTARY_REDIS_URL") or "redis://vexa-redis-1:6379"
TELEGRAM_BOT_TOKEN_FILE = "/home/dev/meeting-notary/vexa/.env.notary"

LOCAL_DOCKER = os.environ.get("MEETING_NOTARY_LOCAL_DOCKER") == "1"

START_WINDOW_MIN = 5  # min до начала; runner запускает если 0 ≤ delta ≤ 5
STARTED_GRACE_MIN = 1  # запускаем и если start_at уже наступил, но не более 1 мин назад

# Ф3 (2026-06-03): concurrency. Матчинг живых ботов — по ИМЕНИ-префиксу (как в
# collector), series тащим из label `meeting-notary.series` (его ставит runner
# при старте — см. _launch_bot). Имя-префикс ловит и legacy-контейнеры без
# label'ов (запущенные до Ф4). Эти константы держим в синхроне с одноимёнными
# в collector.py.
NOTARIUS_NAME_PREFIX = "vexa-notarius-"
SERIES_LABEL = "meeting-notary.series"


def _env_int(name: str, default: int) -> int:
    """Положительный int из env, иначе default (терпим мусор/пусто)."""
    try:
        v = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


# Ф3 (2026-06-03): потолок одновременных записей. Раньше любой живой
# bot-контейнер блокировал старт 2-й встречи (`_is_vexa_running` по
# `ancestor=образ`) — из-за этого 03.06 пропустился директорат, пока шла встреча
# Татьяны (122 мин). Теперь разные серии пишутся параллельно, но в пределах
# лимита: CCX13 = 2 vCPU + headless-Chrome у каждого бота тяжёл → дефолт 2.
# Тонкая настройка через env (на случай разовой тройной накладки встреч).
MAX_CONCURRENT_BOTS = _env_int("MEETING_NOTARY_MAX_CONCURRENT_BOTS", 2)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger("runner")


def main() -> int:
    setup_logging()
    now = datetime.now(timezone.utc)
    pu = pause_until()
    if pu:
        logger.debug("Пауза до %s — тик пропущен", pu)
        return 0

    queue = read_queue()
    if not queue:
        logger.info("tick: очередь пуста")
        return 0

    candidates = _candidates(queue, now)
    if not candidates:
        logger.info("tick: %d в очереди, ни одного в окне старта", len(queue))
        return 0
    logger.info("Кандидатов на старт: %d", len(candidates))

    # Ф3 (2026-06-03): concurrency. Один снимок живых ботов на тик; решаем по
    # КАЖДОМУ кандидату отдельно (раньше — жёсткое «primary + others-в-отказ» +
    # глобальный гейт `_is_vexa_running` «любой контейнер образа = занято», что и
    # блокировало 2-ю встречу 03.06). Теперь:
    #   • разные серии пишутся ПАРАЛЛЕЛЬНО, в пределах MAX_CONCURRENT_BOTS;
    #   • вторая запись ТОЙ ЖЕ серии и переполнение лимита → пропуск С
    #     УВЕДОМЛЕНИЕМ владельца с причиной (REQ 3.3 — не молчаливый пропуск).
    # Финализацию 2-й встречи в очередь ставить тут не нужно: collector
    # (Type=oneshot, OnUnitActiveSec=300s) не перекрывает свои тики, а finalize
    # внутри тика идёт последовательным for-циклом — два Speechmatics-finalize
    # разом на 2 vCPU не запустятся by design.
    snapshot = _running_bots_snapshot()
    running_count = int(snapshot.get("total") or 0)
    launched_series: set[str] = set()

    for cand in candidates:
        series = str(cand.get("series") or cand["meeting_id"])
        start_at = cand.get("start_at")
        action, reason = _concurrency_decision(
            series, snapshot, launched_series, running_count, MAX_CONCURRENT_BOTS,
        )
        if action != "launch":
            # REQ 3.3 — пропуск старта с ПРИЧИНОЙ. dedupe_key по событию (а не по
            # тексту): повтор того же пропуска в окне старта (runner раз в минуту)
            # глушится на 6ч, но пропуск ДРУГОЙ встречи всегда доходит. State НЕ
            # занимаем — если лимит освободится в окне старта, следующий тик
            # попробует снова.
            push(
                f"Не записал «{series}» (start {start_at}): {reason}.",
                dedupe_key=f"start-skip:{action}:{cand.get('event_id')}",
            )
            logger.warning("Старт %s пропущен (%s): %s", cand.get("meeting_id"), action, reason)
            continue

        # State-файл идемпотентности: атомарный acquire ТОЛЬКО перед реальным
        # запуском (пропуски по занятости/лимиту state не занимают).
        if not state_acquire(cand["event_id"], now=now):
            logger.info(
                "event_id=%s уже стартовал недавно — пропуск (повторный тик или ручной run)",
                cand["event_id"],
            )
            continue

        launched_ok = _launch_bot(cand, now)
        if launched_ok:
            launched_series.add(series)
            running_count += 1  # учитываем в лимите для следующих кандидатов тика
            if cand.get("type") == "one-off":
                _auto_disable_one_off(cand["meeting_id"])
        elif cand.get("type") == "one-off":
            logger.info(
                "auto-disable пропущен: one-off %s не стартовал успешно (rc!=0) — "
                "запись остаётся enabled. ПРЕДУПРЕЖДЕНИЕ: state-файл уже занял "
                "event_id на 12ч TTL — повторный запуск ЭТОГО event_id блокируется. "
                "Чтобы попробовать снова в окне старта: почисти `.state.json` руками.",
                cand["meeting_id"],
            )
    return 0


def _candidates(queue: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    state = read_state()
    out: list[dict[str, Any]] = []
    for item in queue:
        raw = item.get("start_at")
        if not raw:
            continue
        try:
            start = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        delta_min = (start - now).total_seconds() / 60.0
        if delta_min > START_WINDOW_MIN:
            continue
        if delta_min < -STARTED_GRACE_MIN:
            continue
        if item.get("event_id") in state:
            # Если в state, но за более чем 12 часов до — state_acquire отпустит
            # (TTL); здесь мы только грубо отсеиваем, окончательное решение —
            # внутри state_acquire(). Но для лога сразу логирнем.
            logger.debug("event_id=%s уже есть в state — кандидатом не считаем", item.get("event_id"))
            continue
        # Не одобряем папку серии: создаст runner после успешного start.
        out.append(item)
    return out


def _running_bots_snapshot() -> dict[str, Any]:
    """Снимок живых bot-контейнеров нотариуса за один `docker ps` (Ф3).

    Возвращает {"by_series": {series: [names]}, "unlabeled": [names], "total": int}.
    series — из label `meeting-notary.series` (ставит runner); имя-префикс
    `vexa-notarius-*` ловит и legacy-контейнеры без label'ов (до Ф4) — они
    попадают в `unlabeled` и считаются в `total` (занимают лимит), но точечно
    по серии не дедупятся. На маке — через ssh; на VPS (LOCAL_DOCKER=1) —
    локально. При timeout/ошибке docker — пустой снимок (как старая
    `_is_vexa_running`: «считаем, что не запущен»): лучше дать старт, чем
    заблокировать запись из-за слепого docker.

    Сменил прежний фильтр `ancestor=<образ>` на `name=vexa-notarius-` (как в
    collector): ancestor ловил и ручные/dev-боты того же образа, а главное —
    был БУЛЕВЫМ глобальным гейтом без разбивки по сериям (корень бага 03.06).
    """
    fmt = '{{.Names}}\t{{.Label "' + SERIES_LABEL + '"}}'
    by_series: dict[str, list[str]] = {}
    unlabeled: list[str] = []
    snap: dict[str, Any] = {"by_series": by_series, "unlabeled": unlabeled, "total": 0}
    if LOCAL_DOCKER:
        cmd = ["docker", "ps", "--filter", f"name={NOTARIUS_NAME_PREFIX}", "--format", fmt]
    else:
        cmd = ["ssh", SSH_HOST,
               "docker ps --filter name=" + shlex.quote(NOTARIUS_NAME_PREFIX) +
               " --format " + shlex.quote(fmt)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        logger.warning("docker ps — timeout (считаем, что активных ботов нет)")
        return snap
    if out.returncode != 0:
        logger.warning("docker ps rc=%d stderr=%s — считаем, что активных нет",
                       out.returncode, (out.stderr or "").strip()[:200])
        return snap
    total = 0
    for line in (out.stdout or "").splitlines():
        if not line.strip():
            continue
        name, _, series = line.partition("\t")
        name = name.strip()
        series = series.strip()
        if not name:
            continue
        total += 1
        if series:
            by_series.setdefault(series, []).append(name)
        else:
            unlabeled.append(name)
    snap["total"] = total
    return snap


def _concurrency_decision(
    series: str,
    snapshot: dict[str, Any],
    launched_series: set[str],
    running_count: int,
    max_bots: int,
) -> tuple[str, str | None]:
    """Чистое решение по одному кандидату (тестируемо без docker).

    Возвращает (action, reason):
      • ("launch", None)        — можно стартовать;
      • ("skip_series", text)   — бот этой серии уже пишет (живой контейнер) ИЛИ
                                  стартовал в этом же тике → не дублируем серию;
      • ("skip_capacity", text) — достигнут лимит одновременных записей.

    Re-entrancy именно ПО СЕРИИ (а не «любой бот = занято») — две РАЗНЫЕ серии
    пишутся параллельно (REQ 3.1). Лимит — потолок ресурсов (CCX13 2 vCPU).
    """
    by_series = snapshot.get("by_series") or {}
    if series in launched_series or by_series.get(series):
        return ("skip_series",
                f"уже идёт активная запись серии «{series}» — "
                f"вторую запись той же серии не запускаю (проверь docker ps)")
    if running_count >= max_bots:
        return ("skip_capacity",
                f"достигнут лимит одновременных записей ({running_count}/{max_bots}) — "
                f"освободится по завершении активных встреч")
    return ("launch", None)


def _verify_container_alive(name: str, log_file: Path, series: str, start_at: str) -> None:
    """Через 3с после `docker run -d` проверить что контейнер не Exited.

    docker run -d возвращает rc=0 сразу как контейнер создан — ZodError / image-bug
    может убить процесс через 200мс, rc остаётся 0 на стороне runner. Этот hook
    ловит ранние exit'ы и пушит алерт + docker logs в run-лог для debug.
    Лучше дать ложноположительный алерт, чем молча пропустить мёртвого бота.
    """
    time.sleep(3)
    if LOCAL_DOCKER:
        inspect_cmd = ["docker", "inspect", name, "--format",
                       "{{.State.Status}}|{{.State.ExitCode}}"]
    else:
        inspect_cmd = ["ssh", SSH_HOST,
                       "docker inspect " + shlex.quote(name) +
                       " --format '{{.State.Status}}|{{.State.ExitCode}}'"]
    try:
        proc = subprocess.run(inspect_cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning("docker inspect %s — timeout, считаем что жив", name)
        return
    if proc.returncode != 0:
        logger.warning("docker inspect %s rc=%d, не могу проверить состояние", name, proc.returncode)
        return
    status, _, exit_code = (proc.stdout.strip().partition("|"))
    # exited — упал; created/restarting через 3с = stuck на bootstrap (image pull,
    # network init, OCI runtime delay). exit_code=137 (SIGKILL) — внешний docker stop
    # (например ручной smoke), не наш баг — молча игнор.
    if status not in {"exited", "created", "restarting"}:
        return
    if status == "exited" and exit_code == "137":
        logger.info("Container %s остановлен внешне (SIGKILL exit_code=137) — не алертим", name)
        return
    # Контейнер уже Exited / stuck — снимаем логи и алертим
    logs_cmd = (["docker", "logs", "--tail", "40", name] if LOCAL_DOCKER
                else ["ssh", SSH_HOST, "docker logs --tail 40 " + shlex.quote(name)])
    try:
        logs_proc = subprocess.run(logs_cmd, capture_output=True, text=True, timeout=15)
        try:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"\n# --- docker inspect: status={status} exit_code={exit_code} ---\n")
                f.write(f"# --- docker logs --tail 40 {name} ---\n")
                f.write(logs_proc.stdout or "")
                f.write(logs_proc.stderr or "")
        except OSError:
            pass
    except subprocess.TimeoutExpired:
        logger.warning("docker logs %s — timeout", name)
    push(
        f"Бот «{series}» (start {start_at}) не живёт через 3с после старта "
        f"(status={status} exit_code={exit_code}). Лог: {log_file.name}",
        dedupe=False,
    )
    logger.error("Container %s не запустился штатно (status=%s exit_code=%s)", name, status, exit_code)


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_session_uid(meeting_id: str, start_at: str) -> str:
    raw_start = start_at.replace(":", "").replace("-", "").replace("+", "p").replace("T", "T")
    return f"auto-{meeting_id}-{_SAFE_ID_RE.sub('_', raw_start)}"


def _launch_bot(item: dict[str, Any], now: datetime) -> bool:
    """Дёрнуть docker run на VPS. Не блокируется — запускаем в фоне.

    Возвращает True если ssh+docker run отработали успешно (rc=0).
    """
    url = item["url"]
    # str(): series в норме slug-строка из watched.yaml, но fallback на
    # item["meeting_id"] теоретически может быть числом. Нормализуем в источнике,
    # чтобы строкой ушло ВЕЗДЕ: в bot_config["series"] → meta.series у бота, в
    # docker label, в путь папки серии. Иначе meta.series (JSON-число) и label
    # (всегда строка) разъехались бы по типу и матч «бот в звонке» в collector'е
    # (isinstance str) тихо не сработал бы (Н1 цикла Ф4).
    series = str(item.get("series") or item["meeting_id"])
    start_at = item["start_at"]
    session_uid = _safe_session_uid(item["meeting_id"], start_at)

    # Создаём целевую папку серии. На маке — мак-путь, на VPS (LOCAL_DOCKER=1)
    # папка не нужна: collector кладёт .md в /srv/meeting-notary/protocols/<series>,
    # а на мак притаскивает mirror через rsync.
    if not LOCAL_DOCKER:
        series_dir = Path(os.path.expanduser(f"~/Projects/me/встречи/{series}"))
        try:
            series_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Не смог создать папку серии %s: %s", series_dir, e)
    else:
        series_dir = Path("/srv/meeting-notary/protocols") / series

    bot_config = {
        "platform": "yandex_telemost",
        "meetingUrl": url,
        "botName": "Бот — протокол встречи",
        "sessionUid": session_uid,
        "language": "ru",
        "task": "transcribe",
        # Обязательные поля для vexa-bot Zod-схемы (build 2026-05-26+):
        # redisUrl — Zod-required; meeting_id — docker.js делает required-check после Zod.
        "redisUrl": REDIS_URL,
        "meeting_id": int(now.timestamp()),
        # series + expectedParticipants — для маппинга имён в финализаторе.
        # camelCase ключи в bot_config, чтобы vexa-bot (TS) читал из BotConfig без конверсии.
        "series": series,
        "expectedParticipants": item.get("expected_participants") or [],
    }

    # Ф4 (2026-05-29): docker label'ы для матчинга «бот ещё в звонке» в collector'е.
    # Раньше collector строил имя контейнера из имени meta-файла
    # (`vexa-notarius-<date>-tm-<ms>`) и сравнивал с реальным
    # `vexa-notarius-auto-<meeting_id>-<start>` — никогда не совпадало (баг 4Г),
    # гейт был мёртв. Теперь collector резолвит контейнер по label
    # `meeting-notary.series` (надёжный общий ключ meta↔контейнер: sessionUid в meta
    # = `tm-<ms>` от бота, а session_uid контейнера = `auto-...` от runner — не
    # связаны; series runner кладёт и в BOT_CONFIG→meta, и сюда в label).
    # session_uid дублируем в label для дебага. Namespace `meeting-notary.*`.
    # series уже нормализован к str выше (Н1 цикла Ф4) → meta.series и label
    # одного типа, матч в collector'е надёжен.
    labels = {
        "meeting-notary.role": "notarius",
        "meeting-notary.series": series,
        "meeting-notary.sessionUid": session_uid,
    }

    if LOCAL_DOCKER:
        label_args: list[str] = []
        for k, v in labels.items():
            label_args += ["--label", f"{k}={v}"]
        docker_cmd = [
            "docker", "run", "-d",
            "--name", f"vexa-notarius-{session_uid}",
            "--network", DOCKER_NETWORK,
            "-v", TRANSCRIPTS_VOLUME,
            *label_args,
            "-e", f"BOT_CONFIG={json.dumps(bot_config, ensure_ascii=False)}",
            "-e", f"TRANSCRIPTION_SERVICE_URL={TRANSCRIPTION_URL}",
            "-e", f"ENABLE_LIVE_DRAFT={ENABLE_LIVE_DRAFT}",
            BOT_IMAGE,
        ]
    else:
        label_flags = " ".join(
            f"--label {shlex.quote(f'{k}={v}')}" for k, v in labels.items()
        )
        docker_inner = (
            f"docker run -d "
            f"--name vexa-notarius-{shlex.quote(session_uid)} "
            f"--network {shlex.quote(DOCKER_NETWORK)} "
            f"-v {shlex.quote(TRANSCRIPTS_VOLUME)} "
            f"{label_flags} "
            f"-e BOT_CONFIG={shlex.quote(json.dumps(bot_config, ensure_ascii=False))} "
            f"-e TRANSCRIPTION_SERVICE_URL={shlex.quote(TRANSCRIPTION_URL)} "
            f"-e ENABLE_LIVE_DRAFT={shlex.quote(ENABLE_LIVE_DRAFT)} "
            f"{shlex.quote(BOT_IMAGE)}"
        )
        docker_cmd = ["ssh", SSH_HOST, docker_inner]

    log_file = RUNS_DIR / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{item['meeting_id']}.log"
    mode_hint = "local docker" if LOCAL_DOCKER else "ssh docker run"
    logger.info(
        "Запуск (%s): meeting=%s event_id=%s start=%s url=%s session=%s log=%s",
        mode_hint, item["meeting_id"], item["event_id"], start_at, url, session_uid, log_file,
    )
    try:
        with log_file.open("w", encoding="utf-8") as f:
            f.write(f"# Запуск бота {now.isoformat()}\n")
            f.write(f"# mode={mode_hint}\n")
            f.write(f"# meeting_id={item['meeting_id']}\n")
            f.write(f"# event_id={item['event_id']}\n")
            f.write(f"# start_at={start_at}\n")
            f.write(f"# url={url}\n")
            f.write(f"# session_uid={session_uid}\n")
            f.write(f"# series_dir={series_dir}\n")
            f.write(f"# --- {mode_hint} stdout/stderr ниже ---\n\n")
            f.flush()
            proc = subprocess.run(
                docker_cmd,
                stdout=f, stderr=subprocess.STDOUT, timeout=60,
            )
        if proc.returncode != 0:
            push(
                f"Не смог запустить бот «{series}» (start {start_at}): "
                f"{mode_hint} rc={proc.returncode}. Лог: {log_file.name}",
                dedupe=False,
            )
            logger.error("%s rc=%d, см. %s", mode_hint, proc.returncode, log_file)
            return False
        logger.info("%s OK", mode_hint)
        # docker run -d возвращает rc=0 как только контейнер создан, НЕ когда он жив.
        # Через 3с проверяем State.Status — если уже Exited, бот упал на bootstrap
        # (ZodError, image-not-found, network-init) и tg-send алертит с docker logs.
        # На маке (SSH-режим) тоже работает — `ssh meeting-notary docker inspect ...`.
        _verify_container_alive(f"vexa-notarius-{session_uid}", log_file, series, start_at)
        return True
    except subprocess.TimeoutExpired:
        push(
            f"Не смог запустить бот «{series}» (start {start_at}): "
            f"{mode_hint} timeout 60s.",
            dedupe=False,
        )
        logger.error("%s timeout для %s", mode_hint, item["meeting_id"])
        return False
    except Exception as e:  # noqa: BLE001
        push(
            f"Не смог запустить бот «{series}» (start {start_at}): {e}",
            dedupe=False,
        )
        logger.exception("Запуск упал: %s", e)
        return False


def _auto_disable_one_off(meeting_id: str) -> None:
    """Поставить enabled: false для one-off-записи после запуска."""
    data = load_watched(lock=True)
    saved = False
    try:
        rec = find_watched(meeting_id, data)
        if not rec:
            logger.warning("auto-disable: запись %s исчезла из watched.yaml", meeting_id)
            return
        if rec.get("type") != "one-off":
            logger.warning("auto-disable: %s не one-off, type=%s — пропуск", meeting_id, rec.get("type"))
            return
        rec["enabled"] = False
        save_watched(data)
        saved = True
        logger.info("auto-disable: one-off %s помечен enabled=false", meeting_id)
    finally:
        if not saved:
            release_watched_lock()


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        logger.exception("runner упал: %s", e)
        push(f"runner упал: {e}", dedupe=True)
        rc = 1
    sys.exit(rc)
