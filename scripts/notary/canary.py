"""canary.py — самопроверка бота в Т-10 и «нюх» версии Телемоста (ISS-60, фаза 2).

    python -m notary.canary run [--image vexa-bot:notarius-telemost] [--room t10]
                                [--reason daily|version-change|manual] [--force]
    python -m notary.canary sniff [--image …] [--room t10]
    python -m notary.canary status

«Нюх» (`sniff`) — раз в час; смена версии Телемоста проверяется утром, пока в Т-10
никого нет: ежедневной самопроверкой в 04:40 МСК или, если она не закрыла проверку,
внеплановой в 07:00–08:00 МСК (решение владельца 26.09, фаза 6б). Перепроверка красного,
которую не удалось сделать сразу, — тоже только в 07:00–08:00: днём самопроверка в Т-10
не заходит (цикл5 фазы 6б; ручной `run --reason manual` — исключение, он осознанный).

Логика и её объяснение — `lib/canary.py`. Здесь только настоящий внешний мир:
docker, redis-cli (в контейнере redis), ffprobe/ffmpeg, локальный whisper,
главная страница Телемоста и tg-send владельцу.

Работает только на сервере бота (`MEETING_NOTARY_LOCAL_DOCKER=1`): на маке
`run`/`sniff` отказываются — иначе тест или случайный запуск с мака пошёл бы
заводить контейнеры. `status` только читает файлы и работает везде.

Живые проверки кандидата (только с тестовым `--image`, на боевом образе флаги
игнорируются): `MEETING_NOTARY_CANARY_FORCE_FALLBACK=1|no-testid` — бот входит
запасным путём (фаза 3б); `MEETING_NOTARY_CANARY_FORCE_DEAF=1` — основная запись
бота «оглохла», оценивается второе ухо (фаза 4): кодовое слово в его файле
`*.chunk0.wav`, бот сам объявил основную оглохшей (событие `primary_deaf`),
переключил протокол на второе ухо и не ушёл раньше команды самопроверки.

Юниты systemd — `deploy/meeting-notary-canary.{service,timer}` и
`deploy/meeting-notary-telemost-sniff.{service,timer}` (см. deploy/README.md).
"""
from __future__ import annotations

import argparse
import array
import logging
import os
import signal
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path
from typing import List, Optional, Tuple

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.lib import canary as lib  # noqa: E402

logger = logging.getLogger("notary.canary")

REDIS_CONTAINER = os.environ.get("MEETING_NOTARY_REDIS_CONTAINER") or "vexa-redis-1"
LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR") or "~/Library/Logs/meeting-notary"
))
#: Обычный браузерный User-Agent: главная Телемоста ботам может отдавать другое.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


def setup_logging() -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_DIR / "canary.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=handlers)


def _on_signal(signum: int, _frame: object) -> None:
    """SIGTERM/SIGHUP → SystemExit: блоки finally выводят бота и диктора из комнаты.

    Без обработчика питон умирал сразу (systemd по сроку юнита, оборванный ssh при
    ручном прогоне), `finally` в `lib.run_once` не выполнялся — контейнеры оставались
    в Т-10, бот писал комнату до 20 минут тишины, а при людях — дольше (цикл5 фазы 2,
    Н1). Повторный такой же сигнал игнорируем, чтобы не оборвать саму уборку;
    последней мерой остаётся SIGKILL systemd после TimeoutStopSec.
    """
    try:
        signal.signal(signum, signal.SIG_IGN)
    except (OSError, ValueError):
        pass
    logger.warning("[canary] сигнал %s — выхожу, сперва вывожу бота и диктора из комнаты", signum)
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------


def _run(cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def docker_run(cmd: List[str]) -> Tuple[bool, str]:
    try:
        proc = _run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "docker run timeout"
    return proc.returncode == 0, (proc.stderr or "").strip()[-300:]


def container_state(name: str) -> Tuple[str, Optional[int]]:
    try:
        proc = _run(["docker", "inspect", name, "--format", "{{.State.Status}}|{{.State.ExitCode}}"], timeout=15)
    except subprocess.TimeoutExpired:
        return "unknown", None
    if proc.returncode != 0:
        err = (proc.stderr or "").lower()
        return ("missing", None) if ("no such object" in err or "no such container" in err) else ("unknown", None)
    status, _, code = proc.stdout.strip().partition("|")
    try:
        return status or "unknown", int(code)
    except ValueError:
        return status or "unknown", None


def containers() -> Optional[List[Tuple[str, str]]]:
    """Живые контейнеры: (имя, метка роли). None — docker не ответил."""
    fmt = '{{.Names}}\t{{.Label "' + lib.ROLE_LABEL + '"}}'
    try:
        proc = _run(["docker", "ps", "--format", fmt], timeout=20)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    out = []
    for line in (proc.stdout or "").splitlines():
        name, _, role = line.partition("\t")
        if name.strip():
            out.append((name.strip(), role.strip()))
    return out


def canary_leftovers() -> List[str]:
    try:
        proc = _run(["docker", "ps", "-a", "--filter", f"label={lib.ROLE_LABEL}={lib.ROLE_CANARY}",
                     "--format", "{{.Names}}"], timeout=20)
    except subprocess.TimeoutExpired:
        return []
    return [n.strip() for n in (proc.stdout or "").splitlines() if n.strip()] if proc.returncode == 0 else []


def _only_canary(name: str) -> None:
    # Останавливаем и удаляем ТОЛЬКО свои контейнеры — по имени.
    if not name.startswith(lib.CONTAINER_PREFIX):
        raise ValueError("не контейнер самопроверки")


def stop(name: str) -> None:
    _only_canary(name)
    _run(["docker", "stop", "-t", "20", name], timeout=60)


def remove(name: str) -> None:
    _only_canary(name)
    _run(["docker", "rm", "-f", name], timeout=60)


def logs(name: str) -> str:
    proc = _run(["docker", "logs", name], timeout=60)
    return (proc.stdout or "") + (proc.stderr or "")


def publish(channel: str, message: str) -> Optional[bool]:
    """redis PUBLISH через контейнер redis (redis-cli на хосте может не быть)."""
    try:
        proc = _run(["docker", "exec", REDIS_CONTAINER, "redis-cli", "PUBLISH", channel, message], timeout=20)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip().split()[-1]) > 0
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Звук
# ---------------------------------------------------------------------------


def _wav_python(path: Path) -> Tuple[Optional[float], Optional[Tuple[float, float]]]:
    """Запасной путь без ffmpeg: длительность и (mean_db, max_db) для 16-битного PCM."""
    import math  # noqa: PLC0415

    try:
        with wave.open(str(path), "rb") as w:
            rate, n, width = w.getframerate(), w.getnframes(), w.getsampwidth()
            raw = w.readframes(n)
    except (OSError, wave.Error, EOFError):
        return None, None
    dur = n / float(rate) if rate else None
    if width != 2 or not raw:
        return dur, None
    a = array.array("h")
    a.frombytes(raw[: len(raw) - len(raw) % 2])
    if not a:
        return dur, None
    peak = max(abs(x) for x in a) or 0
    rms = math.sqrt(sum(x * x for x in a) / len(a))
    to_db = lambda v: 20 * math.log10(v / 32768.0) if v > 0 else -91.0  # noqa: E731
    return dur, (round(to_db(rms), 1), round(to_db(peak), 1))


def probe_duration(path: Path) -> Optional[float]:
    try:
        proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", str(path)], timeout=60)
        if proc.returncode == 0:
            return round(float(proc.stdout.strip()), 1)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return _wav_python(path)[0]


def volume(path: Path) -> Optional[Tuple[float, float]]:
    try:
        proc = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                     "-af", "volumedetect", "-f", "null", "-"], timeout=180)
        parsed = lib.parse_volumedetect(proc.stderr or "")
        if parsed:
            return parsed
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _wav_python(path)[1]


def transcribe(path: Path) -> str:
    """Только локальный whisper (внешние сервисы самопроверке не нужны)."""
    from notary.lib.transcribe import transcribe_wav  # noqa: PLC0415

    model = (os.environ.get("MEETING_NOTARY_CANARY_ASR_MODEL") or os.environ.get("ASR_MODEL")
             or "Systran/faster-whisper-medium")
    text, _lang, _segments = transcribe_wav(
        str(path), lib.TRANSCRIPTION_URL, model=model, language="ru", timeout_s=lib.TRANSCRIBE_TIMEOUT_SEC,
    )
    return text


# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------


def http_get(url: str) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "ru-RU,ru;q=0.9"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — адрес зашит (TELEMOST_HOME)
        return resp.status, resp.read(3 * 1024 * 1024).decode("utf-8", "replace")


def notify(text: str, silent: bool) -> bool:
    from notary.lib.notify import push  # noqa: PLC0415

    return push(text, dedupe=False, silent=silent)


def read_queue() -> list:
    from notary.lib.state import read_queue as _rq  # noqa: PLC0415

    return _rq()


def build_deps() -> lib.Deps:
    return lib.Deps(
        docker_run=docker_run,
        container_state=container_state,
        containers=containers,
        canary_leftovers=canary_leftovers,
        stop=stop,
        remove=remove,
        logs=logs,
        publish=publish,
        probe_duration=probe_duration,
        volume=volume,
        transcribe=transcribe,
        read_queue=read_queue,
        room_url=lib.resolve_room_url,
        notify=notify,
        http_get=http_get,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="notary.canary", description="Самопроверка бота в Т-10")
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_image = os.environ.get("MEETING_NOTARY_CANARY_IMAGE") or lib.DEFAULT_IMAGE
    default_room = os.environ.get("MEETING_NOTARY_CANARY_ROOM") or lib.DEFAULT_ROOM
    p_run = sub.add_parser("run", help="прогон самопроверки (с повтором перед тревогой)")
    p_run.add_argument("--image", default=default_image)
    p_run.add_argument("--room", default=default_room)
    p_run.add_argument("--reason", default="manual", choices=["daily", "version-change", "manual"])
    p_run.add_argument("--force", action="store_true", help="не смотреть на очередь встреч (идущий бот — всё равно стоп)")
    p_sniff = sub.add_parser("sniff", help="проверить версию Телемоста; сменилась — прогон утром "
                                           "07:00–08:00 МСК, если его не закрыла ежедневная в 04:40")
    p_sniff.add_argument("--image", default=default_image)
    p_sniff.add_argument("--room", default=default_room)
    sub.add_parser("status", help="состояние самопроверки")
    args = ap.parse_args(argv)

    if args.cmd == "status":
        print(lib.status_text())
        return 0
    if not lib.is_local_docker():
        print(
            "Самопроверка работает только на сервере бота (MEETING_NOTARY_LOCAL_DOCKER=1): она заводит "
            "контейнеры бота и диктора в локальном docker. На маке не запускается — это не ошибка.",
            file=sys.stderr,
        )
        return 2
    setup_logging()
    install_signal_handlers()
    deps = build_deps()
    if args.cmd == "run":
        result = lib.run_cycle(deps=deps, image=args.image, room=args.room, reason=args.reason, force=args.force)
        print(lib.summary_line(result)
              + (" [второе ухо: основная запись заглушена флагом]" if result.get("force_deaf") else "")
              + ("" if lib.is_production_image(args.image)
                 else f" [тестовый образ {args.image}: состояние боевого бота и сообщения не трогал]"))
        return 0
    out = lib.sniff(deps=deps, image=args.image, room=args.room)
    print(f"Телемост: версия {out.get('version') or '?'}"
          + (f", ошибка {out['error']}" if out.get("error") else "")
          + (", сменилась" if out.get("changed") else "")
          + (f"; прогон: {out['run']}" if out.get("run") else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        logger.exception("canary упал: %s", type(e).__name__)
        sys.exit(1)
