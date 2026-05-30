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

    # Конфликт: если в окне старта оказалось несколько встреч — берём первую.
    primary = candidates[0]
    others = candidates[1:]
    for o in others:
        push(
            f"Не записал «{o.get('series')}» (start {o.get('start_at')}): "
            f"конфликт с «{primary.get('series')}». "
            f"Параллельные сессии Vexa пока не поддерживаем.",
            dedupe=True,
        )
        logger.warning(
            "Конфликт: %s пропущен из-за %s",
            o.get("meeting_id"),
            primary.get("meeting_id"),
        )

    # State-файл идемпотентности: атомарный acquire.
    if not state_acquire(primary["event_id"], now=now):
        logger.info(
            "event_id=%s уже стартовал недавно — пропуск (повторный launchd-тик или ручной run)",
            primary["event_id"],
        )
        return 0

    # Проверим, что на VPS не запущен уже бот.
    if _is_vexa_running():
        push(
            f"Не записал «{primary.get('series')}» (start {primary.get('start_at')}): "
            f"на VPS уже идёт активная Vexa-сессия. Проверь docker ps.",
            dedupe=True,
        )
        logger.warning("Vexa уже запущена — primary пропущен")
        return 0

    launched_ok = _launch_bot(primary, now)

    if launched_ok and primary.get("type") == "one-off":
        _auto_disable_one_off(primary["meeting_id"])
    elif not launched_ok and primary.get("type") == "one-off":
        logger.info(
            "auto-disable пропущен: one-off %s не стартовал успешно (rc!=0) — "
            "запись остаётся enabled. ПРЕДУПРЕЖДЕНИЕ: state-файл уже занял "
            "event_id на 12ч TTL — повторный запуск ЭТОГО event_id блокируется. "
            "Чтобы попробовать снова в окне старта: почисти `.state.json` руками.",
            primary["meeting_id"],
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


def _is_vexa_running() -> bool:
    """Проверить, есть ли активный контейнер с vexa-bot:notarius-telemost.

    На маке — через ssh meeting-notary; на VPS (MEETING_NOTARY_LOCAL_DOCKER=1) —
    напрямую через docker ps без префикса ssh.
    """
    if LOCAL_DOCKER:
        cmd = ["docker", "ps", "--filter", f"ancestor={BOT_IMAGE}", "--format", "{{.ID}}"]
    else:
        cmd = ["ssh", SSH_HOST, "docker ps --filter ancestor=" + shlex.quote(BOT_IMAGE) + " --format '{{.ID}}'"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        logger.warning("docker ps — timeout (считаем что бот не запущен)")
        return False
    if out.returncode != 0:
        logger.warning("docker ps rc=%d stderr=%s — считаем что не запущен", out.returncode, out.stderr.strip()[:200])
        return False
    return bool(out.stdout.strip())


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
