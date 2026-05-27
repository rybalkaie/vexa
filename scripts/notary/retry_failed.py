#!/usr/bin/env python3
"""Retry-очередь для встреч, где упал Speechmatics.

Запускается systemd-timer'ом `meeting-notary-retry-failed.timer` каждые 15 мин.
На каждом тике:

  1. heartbeat `/srv/meeting-notary/state/retry-alive` (mtime — для watchdog).
  2. Проходит по `_failed/*.retry-state.json`.
  3. Для каждого решает по расписанию (см. SCHEDULE_MIN), пора ли пробовать.
  4. На пробе — берёт `flock /srv/meeting-notary/state/finalize-<sid>.lock`
     (защита от двойной оплаты, см. план Ф2 У6), запускает finalize-meeting.py
     с `STT_BACKEND=speechmatics` и meta из `_failed/<sid>.meta.json` (там WAV
     уже переписан на путь в _failed/).
  5. На успехе финализатор сам почистит `_failed/<sid>.*` и пушнет ✅.
  6. На фейле инкрементит `attempts`, обновляет `last_attempt_at`/`last_error`.
  7. На превышении 24ч с first_failed_at — финальный push «нужно ручное решение»
     (один раз, флаг `final_push_sent` в retry-state).

Логи: stdout/stderr идут в journal (через ExecStart systemd-юнита).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import subprocess
import sys
from pathlib import Path


logger = logging.getLogger("retry-failed")


# Расписание из плана Ф2:
#   T+15/30/45/60 мин (4 попытки в первый час),
#   T+2/3/4/5 ч     (4 попытки часы 2-5, шаг 1ч = 60 мин),
#   T+8/11/14/17/20/23 ч (6 попыток с шагом 3ч).
# Итого 14 попыток в окне 24ч. На attempts=N следующая проба должна состояться
# когда now >= first_failed + SCHEDULE_MIN[N] минут.
SCHEDULE_MIN: list[int] = [
    15, 30, 45, 60,
    120, 180, 240, 300,
    480, 660, 840, 1020, 1200, 1380,
]
MAX_AGE_S = 24 * 3600  # после этого — финальный push, ретраи останавливаются

DEFAULT_FAILED_DIR = "/srv/meeting-notary/_failed"
DEFAULT_STATE_DIR = "/srv/meeting-notary/state"
DEFAULT_FINALIZER = "/srv/meeting-notary/vexa/scripts/notary/finalize-meeting.py"
DEFAULT_PYTHON = "/home/dev/meeting-notary/venv/bin/python"
DEFAULT_TG_SEND = "/srv/meeting-notary/bin/tg-send"
DEFAULT_ENV_FILE = "/srv/meeting-notary/.env.notary"


def _parse_iso(s: str) -> dt.datetime:
    """Парс UTC-ISO ("2026-05-27T10:11:12.345Z") в naive UTC datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def _now_utc() -> dt.datetime:
    return dt.datetime.utcnow()


def _heartbeat(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "retry-alive").touch()


def _push(tg_send: Path, message: str) -> None:
    if not os.access(tg_send, os.X_OK):
        logger.warning("tg-send не найден по %s — push пропущен: %s", tg_send, message[:80])
        return
    try:
        subprocess.run([str(tg_send), message], check=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning("tg-send failed: %s — %s", e, message[:80])


def _load_env_file(env_file: Path) -> dict[str, str]:
    """Минимальный парсер .env (KEY=VALUE), без shell-фич. Для финализатора."""
    out: dict[str, str] = {}
    if not env_file.exists():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        elif v.startswith("'") and v.endswith("'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _due_to_attempt(state: dict, now: dt.datetime) -> bool:
    """Пора ли запускать ретрай для этого состояния?"""
    if state.get("rejected"):
        return False
    attempts = int(state.get("attempts") or 0)
    if attempts >= len(SCHEDULE_MIN):
        return False
    first_iso = state.get("first_failed_at")
    if not first_iso:
        return False
    try:
        first = _parse_iso(first_iso)
    except Exception:
        return False
    if (now - first).total_seconds() > MAX_AGE_S:
        return False
    due_at = first + dt.timedelta(minutes=SCHEDULE_MIN[attempts])
    return now >= due_at


def _final_push_due(state: dict, now: dt.datetime) -> bool:
    """24ч прошло, финальный push ещё не отправлен и встреча не rejected."""
    if state.get("rejected"):
        return False
    if state.get("final_push_sent"):
        return False
    first_iso = state.get("first_failed_at")
    if not first_iso:
        return False
    try:
        first = _parse_iso(first_iso)
    except Exception:
        return False
    return (now - first).total_seconds() > MAX_AGE_S


def _try_finalize(
    *,
    sid: str,
    meta_path: Path,
    state_path: Path,
    state: dict,
    state_dir: Path,
    finalizer: Path,
    python_bin: Path,
    env_file: Path,
    dry_run: bool,
) -> None:
    """Один раунд попытки. На успехе финализатор сам уберёт _failed/<sid>.*"""
    lock_path = state_dir / f"finalize-{sid}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["last_attempt_at"] = _now_utc().isoformat() + "Z"

    if dry_run:
        logger.info("[dry-run] sid=%s — пропущу запуск финализатора (attempts=%s)",
                    sid, state["attempts"])
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    env = os.environ.copy()
    env.update(_load_env_file(env_file))
    env["STT_BACKEND"] = "speechmatics"

    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            logger.info("sid=%s lock занят (параллельный финализатор) — пропускаю до след. тика", sid)
            # attempts откатываем — попытка не состоялась.
            state["attempts"] -= 1
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            return

        logger.info("sid=%s retry attempt=%s, meta=%s", sid, state["attempts"], meta_path)
        cmd = [str(python_bin), str(finalizer), str(meta_path)]
        try:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=4 * 3600,
            )
        except subprocess.TimeoutExpired as e:
            state["last_error"] = f"timeout: {e}"
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.error("sid=%s timeout", sid)
            return

        if proc.returncode == 0:
            # Успех — финализатор сам грохнул _failed/<sid>.*, нам делать ничего.
            logger.info("sid=%s SUCCESS на попытке %s", sid, state["attempts"])
            return

        # last_error — короткое резюме без сырого stderr-tail:
        # (a) stderr/stdout финализатора может содержать фрагменты транскрипта
        #     (детали ошибок pyannote/whisper), которые мы по плану «опасной
        #     тройки» в долгоживущие файлы и в Telegram не кладём;
        # (b) сюда же может попасть HTTP-ответ Speechmatics с токеном в заголовках.
        # Поэтому фиксируем тип ошибки/код, а не сырой хвост.
        state["last_error"] = f"finalizer rc={proc.returncode}"
        if state_path.exists():
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        # Для post-mortem stderr/stdout НЕ теряем — пишем в journal через
        # logger (НЕ в state-файл и НЕ в TG). Через journalctl Илья видит,
        # state остаётся чистым от транскрипта/секретов.
        logger.warning(
            "sid=%s retry rc=%s, attempts=%s — stderr/stdout см. в DEBUG ниже",
            sid, proc.returncode, state["attempts"],
        )
        if proc.stderr:
            logger.debug("sid=%s finalizer stderr: %s", sid, proc.stderr[-1000:])
        if proc.stdout:
            logger.debug("sid=%s finalizer stdout: %s", sid, proc.stdout[-1000:])
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def tick(
    *,
    failed_dir: Path,
    state_dir: Path,
    finalizer: Path,
    python_bin: Path,
    env_file: Path,
    tg_send: Path,
    dry_run: bool,
) -> int:
    """Один проход retry-очереди. Возвращает число попыток финализации."""
    _heartbeat(state_dir)
    if not failed_dir.exists():
        logger.info("failed_dir не существует (%s) — нечего ретраить", failed_dir)
        return 0

    now = _now_utc()
    attempts_done = 0
    state_files = sorted(failed_dir.glob("*.retry-state.json"))
    logger.info("retry tick: found %d state files в %s", len(state_files), failed_dir)

    for state_path in state_files:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Не смог прочитать %s: %s", state_path, e)
            continue

        sid = state.get("session_uid") or state_path.stem.replace(".retry-state", "")
        meta_path = failed_dir / f"{sid}.meta.json"

        # Финальный push (24ч прошло).
        if _final_push_due(state, now):
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    series_label = (json.load(fh).get("series") or sid)
            except Exception:
                series_label = sid
            _push(tg_send, (
                f"🕐 Сутки прошло, встреча `{series_label}` не обработана. "
                f"Speechmatics не отвечает, файлы в `_failed/{sid}.*` — нужно ручное решение."
            ))
            state["final_push_sent"] = True
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        if not _due_to_attempt(state, now):
            continue

        if not meta_path.exists():
            logger.warning("meta для sid=%s не найдена (%s) — пропускаю", sid, meta_path)
            continue

        _try_finalize(
            sid=sid,
            meta_path=meta_path,
            state_path=state_path,
            state=state,
            state_dir=state_dir,
            finalizer=finalizer,
            python_bin=python_bin,
            env_file=env_file,
            dry_run=dry_run,
        )
        attempts_done += 1

    logger.info("retry tick done: %d попыток финализации", attempts_done)
    return attempts_done


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry-очередь Speechmatics для meeting-notary")
    parser.add_argument("--failed-dir", default=DEFAULT_FAILED_DIR)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--finalizer", default=DEFAULT_FINALIZER)
    parser.add_argument("--python", default=DEFAULT_PYTHON)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--tg-send", default=DEFAULT_TG_SEND)
    parser.add_argument("--dry-run", action="store_true", help="Не запускать финализатор, только лог")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    tick(
        failed_dir=Path(args.failed_dir),
        state_dir=Path(args.state_dir),
        finalizer=Path(args.finalizer),
        python_bin=Path(args.python),
        env_file=Path(args.env_file),
        tg_send=Path(args.tg_send),
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
