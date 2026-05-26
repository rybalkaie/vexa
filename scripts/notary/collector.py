#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""collector.py — закрывает цикл post-meeting: finalize на VPS → scp .md на мак.

Запускается launchd-агентом `com.ilarybalka.meeting-notary.collector` раз в 5 минут.

Алгоритм одного тика:
  1. ssh meeting-notary: найти все *.meta.json в ~/meeting-notary/_tmp/transcripts/,
     для которых нет соответствующего *.md в ~/meeting-notary/_tmp/protocols/.
  2. Для каждого: проверить, что бот завершился (нет активного контейнера vexa-bot
     с этим sessionUid в `docker ps`) — иначе встреча ещё идёт.
  3. Если завершился — запустить finalize-meeting.py на VPS.
  4. Скопировать готовый .md с VPS в ~/Projects/me/встречи/<series>/<date>.md.

Дисциплина «Опасной тройки»:
  - Сам collector не читает содержимое транскриптов/протоколов локально.
  - Только пути и факт «новый/нет».
  - Маппинг через `claude` CLI на VPS пока недоступен (нет claude на VPS) —
    это долг Ф6/Ф7. Финализатор Vexa-стороны делает источники 1+2 (Telemost-list,
    regex+pymorphy3) и оставляет «Спикер N» там, где не уверен. Этого достаточно
    для читаемого протокола; LLM-маппинг — улучшение, не блокер.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.lib.notify import push  # noqa: E402

LOG_DIR = Path(os.path.expanduser("~/Library/Logs/meeting-notary"))
LOG_FILE = LOG_DIR / "collector.log"

SSH_HOST = "meeting-notary"
VPS_TRANSCRIPTS = "~/meeting-notary/_tmp/transcripts"
VPS_PROTOCOLS = "~/meeting-notary/_tmp/protocols"
VPS_VENV = "~/meeting-notary/venv"  # venv с pyannote/torch/whisper (создан в Ф3)
VPS_FINALIZE = "~/meeting-notary/vexa/scripts/notary/finalize-meeting.py"

MEETINGS_DIR = Path(os.path.expanduser("~/Projects/me/встречи"))


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger("collector")


def ssh_capture(cmd: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", SSH_HOST, cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def main() -> int:
    setup_logging()
    logger.info("=== collector run ===")

    # 1. Список pending meta.json: есть .meta.json, нет .md в protocols.
    list_cmd = (
        f"set -e; "
        f"cd {VPS_TRANSCRIPTS}; "
        f"shopt -s nullglob; "
        f"for f in *.meta.json; do "
        f"  sid=\"${{f%.meta.json}}\"; "
        f"  if [[ ! -f {VPS_PROTOCOLS}/${{sid}}.md ]]; then "
        f"    echo \"$sid\"; "
        f"  fi; "
        f"done"
    )
    try:
        proc = ssh_capture(f"bash -c {shlex.quote(list_cmd)}", timeout=30)
    except subprocess.TimeoutExpired:
        logger.warning("ssh list timeout — пропуск")
        return 0
    if proc.returncode != 0:
        logger.warning("ssh list rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:200])
        return 0
    pending = [s.strip() for s in proc.stdout.strip().splitlines() if s.strip()]
    if not pending:
        logger.info("Нет pending meta.json — нечего финализировать")
        return 0
    logger.info("Pending sessions (%d): %s", len(pending), pending)

    # 2. Для каждого — проверить, что бот завершился (контейнер ушёл).
    try:
        ps_proc = ssh_capture(
            f"docker ps --format '{{{{.Names}}}}' --filter 'name=vexa-notarius-'",
            timeout=20,
        )
        active = set(s.strip() for s in (ps_proc.stdout or "").splitlines() if s.strip())
    except subprocess.TimeoutExpired:
        logger.warning("docker ps timeout — считаем что нет активных")
        active = set()

    for sid in pending:
        cont_name = f"vexa-notarius-{sid}"
        if cont_name in active:
            logger.info("Скип %s — контейнер ещё активен", sid)
            continue
        _finalize_and_collect(sid)
    return 0


def _finalize_and_collect(session_uid: str) -> None:
    """Запустить finalize-meeting.py на VPS для sessionUid, потом scp .md на мак."""
    # 1. Финализация на VPS (без claude — на VPS его нет, источник 3 пропустится).
    meta_path = f"{VPS_TRANSCRIPTS}/{session_uid}.meta.json"
    md_path = f"{VPS_PROTOCOLS}/{session_uid}.md"

    finalize_cmd = (
        f"set -e; "
        f"cd ~/meeting-notary/vexa/scripts/notary; "
        # Загружаем .env.notary — там HF_TOKEN для pyannote, TRANSCRIPTION_SERVICE_URL и т.п.
        f"set -a; source ~/meeting-notary/vexa/.env.notary; set +a; "
        f"unset ENABLE_CLAUDE_NAME_MAPPING; "  # claude CLI нет на VPS
        f"unset FAKE_DIARIZATION_PATH; "  # HF-токен есть, real pyannote
        f"{VPS_VENV}/bin/python finalize-meeting.py {shlex.quote(meta_path)}"
    )
    logger.info("Финализирую %s на VPS …", session_uid)
    try:
        proc = ssh_capture(f"bash -lc {shlex.quote(finalize_cmd)}", timeout=600)
    except subprocess.TimeoutExpired:
        push(f"Финализация «{session_uid}» — timeout 10мин на VPS. Проверь ssh meeting-notary docker ps + логи.")
        logger.error("finalize timeout: %s", session_uid)
        return
    if proc.returncode != 0:
        logger.error("finalize rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:400])
        push(f"Финализация «{session_uid}» упала: rc={proc.returncode}. Лог: ~/Library/Logs/meeting-notary/collector.log")
        return

    # 2. Тянем meta.json + .md на мак.
    series, date_str = _series_and_date(session_uid)
    target_dir = MEETINGS_DIR / series
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("mkdir %s упал: %s", target_dir, e)
        return

    target_md = target_dir / f"{date_str}.md"
    # Если уже существует — добавим суффикс с session_uid, не перезатираем.
    if target_md.exists():
        target_md = target_dir / f"{date_str}-{session_uid}.md"

    scp_cmd = [
        "scp",
        f"{SSH_HOST}:{md_path}",
        str(target_md),
    ]
    try:
        scp = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        push(f"scp .md «{session_uid}» — timeout. Файл на VPS: {md_path}")
        logger.error("scp timeout: %s", session_uid)
        return
    if scp.returncode != 0:
        logger.error("scp rc=%d stderr=%s", scp.returncode, scp.stderr.strip()[:400])
        push(f"scp «{session_uid}» упал: rc={scp.returncode}. Лог в collector.log")
        return
    logger.info("✓ Протокол: %s", target_md)


_SESSION_RE = re.compile(r"^auto-(?P<mid>[^-]+(?:-[^-]+)*?)-(?P<dt>\d{4}\d{2}\d{2}T\d{6}Z)$")


def _series_and_date(session_uid: str) -> tuple[str, str]:
    """Вытащить серию и дату YYYY-MM-DD из sessionUid.

    Формат session_uid из runner.py: auto-<meeting_id>-<YYYYMMDDTHHMMSSZ>
    Для ручных запусков из meeting-watch run: manual-<meeting_id>-<YYYYMMDDTHHMMSSZ>
    На крайний случай: возвращаем (session_uid, today).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    m = _SESSION_RE.match(session_uid)
    if not m:
        # Попробуем общий формат: <prefix>-<meeting_id>-<dt>
        parts = session_uid.split("-")
        if len(parts) >= 3:
            mid = "-".join(parts[1:-1])
            dt_raw = parts[-1]
            try:
                dt = datetime.strptime(dt_raw, "%Y%m%dT%H%M%SZ")
                return mid, dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        return session_uid, today

    mid = m.group("mid")
    try:
        dt = datetime.strptime(m.group("dt"), "%Y%m%dT%H%M%SZ")
        return mid, dt.strftime("%Y-%m-%d")
    except ValueError:
        return mid, today


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        logger.exception("collector упал: %s", e)
        push(f"collector упал: {e}", dedupe=True)
        rc = 1
    sys.exit(rc)
