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
from notary.lib.paths import _target_path  # noqa: E402

LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR") or "~/Library/Logs/meeting-notary"
))
LOG_FILE = LOG_DIR / "collector.log"

SSH_HOST = "meeting-notary"
# Абсолютные пути на VPS (user=dev). В LOCAL_FINALIZE=1 collector запускается на VPS
# через `bash -lc`, и значения, попавшие под shlex.quote (например meta_path), берутся
# одинарными кавычками — `~` не разворачивается. Литералом `/home/dev/...` работают
# оба сценария (ssh-shell тоже найдёт). См. handoff-Ф2-perenos-v3.md.
VPS_TRANSCRIPTS = "/home/dev/meeting-notary/_tmp/transcripts"
VPS_PROTOCOLS = "~/meeting-notary/_tmp/protocols"
VPS_VENV = "~/meeting-notary/venv"  # venv с pyannote/torch/whisper (создан в Ф3)

LOCAL_FINALIZE = os.environ.get("MEETING_NOTARY_LOCAL_FINALIZE") == "1"


def _target_md_for_session(series: str, date_str: str, session_uid: str) -> Path:
    """Единая точка вычисления пути к .md (Ф7 синхронизация на _target_path).

    Пустая series → _target_path кладёт в `_one-off/<date>-<id>/<date>.md`,
    непустая → `<root>/<series>/<date>.md`. session_uid нужен только для one-off.

    Fallback (Н6 цикла Ф7): если `date_str` или производное не пройдут
    валидации `_target_path` (regex YYYY-MM-DD, _validate_path_component),
    откатываемся на старую logic `MEETINGS_DIR/<series>/<date>.md` —
    лучше «битый путь» чем умершая orphan-обработка.
    """
    try:
        return _target_path(
            {"series": series, "date": date_str, "sessionUid": session_uid},
            root=MEETINGS_DIR,
            kind="transcript",
        )
    except (ValueError, TypeError) as e:
        logger.warning(
            "_target_path упал на series=%r date=%r sid=%r: %s; "
            "fallback на legacy путь",
            series, date_str, session_uid, e,
        )
        # НОВ2+РЕГ3 цикла Ф7: пустая series раньше клала в корень MEETINGS_DIR/<date>.md.
        # После Ф7 миграции корень содержит только INDEX.md + директории. Кладём
        # one-off fallback в `_one-off/<date>-<sid>/<date>.md` руками — структура
        # та же, что у _target_path успешного пути. Sid может быть «странным» (Н6),
        # но в имени папки нам важна только уникальность.
        if not series:
            safe_sid = (session_uid or "unknown").replace("/", "_").replace("\\", "_")
            return MEETINGS_DIR / "_one-off" / f"{date_str}-{safe_sid}" / f"{date_str}.md"
        return MEETINGS_DIR / series / f"{date_str}.md"

# Где собирать готовый .md. На маке — тащим scp с VPS в ~/Projects/me/встречи/.
# На VPS (LOCAL_FINALIZE=1) — кладём в /srv/meeting-notary/protocols/<series>/,
# mirror-агент мака притащит позже.
MEETINGS_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR")
    or ("/srv/meeting-notary/protocols" if LOCAL_FINALIZE else "~/Projects/me/встречи")
))


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
    """Запустить shell-команду на удалённом хосте (или локально, если LOCAL_FINALIZE=1).

    На VPS строки путей VPS_TRANSCRIPTS/VPS_PROTOCOLS остаются те же (`~/meeting-notary/...`),
    т.к. оба варианта запускаются от пользователя dev — `~` разрешается корректно.
    """
    if LOCAL_FINALIZE:
        argv = ["bash", "-lc", cmd]
    else:
        argv = ["ssh", SSH_HOST, cmd]
    return subprocess.run(
        argv,
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

    # 3. Orphan pickup (LOCAL_FINALIZE only): .md в VPS_PROTOCOLS уже есть,
    # но копии в MEETINGS_DIR/<series>/ нет — finalize в прошлом тике записал
    # .md, но дошёл до конца таймаута / умер до копирования. Подбираем без
    # повторной финализации (она дорогая — 60-90 мин CPU). Для не-LOCAL_FINALIZE
    # ветки эта проблема не воспроизводится (там scp идёт сразу после finalize
    # в одном subprocess).
    if LOCAL_FINALIZE:
        _pickup_orphans()
    return 0


def _pickup_orphans() -> None:
    """Найти .md, для которых уже есть meta.json, но нет копии в MEETINGS_DIR.

    Защита: MEETINGS_DIR должен существовать (на VPS — /srv/meeting-notary/protocols
    через MEETING_NOTARY_PROTOCOLS_DIR env). Если env не выставлен и дефолт
    ~/Projects/me/встречи на VPS не существует — не запускаемся, иначе
    создадим мусорные папки серий на VPS.
    """
    if not MEETINGS_DIR.exists():
        logger.warning(
            "orphan pickup пропущен: MEETINGS_DIR %s не существует (env MEETING_NOTARY_PROTOCOLS_DIR?)",
            MEETINGS_DIR,
        )
        return
    # mtime фильтр: только meta.json моложе 7 дней — старые архивные f3-test-*
    # уже жили до cutover, mirror их не должен тащить (они не в текущем boevом
    # потоке). 7 дней с запасом покрывают runtime collector тика 5 мин.
    orphan_cmd = (
        f"set -e; "
        f"cd {VPS_TRANSCRIPTS}; "
        f"shopt -s nullglob; "
        f"for f in *.meta.json; do "
        f"  sid=\"${{f%.meta.json}}\"; "
        f"  if [[ -f {VPS_PROTOCOLS}/${{sid}}.md ]] && "
        f"     [[ $(find \"$f\" -mtime -7 -print) ]]; then "
        f"    echo \"$sid\"; "
        f"  fi; "
        f"done"
    )
    try:
        proc = ssh_capture(f"bash -c {shlex.quote(orphan_cmd)}", timeout=30)
    except subprocess.TimeoutExpired:
        logger.warning("orphan list timeout — пропуск")
        return
    if proc.returncode != 0:
        return
    candidates = [s.strip() for s in proc.stdout.strip().splitlines() if s.strip()]
    for sid in candidates:
        # Сначала читаем series из meta.json (новый contract; при LOCAL_FINALIZE=1
        # meta лежит на той же машине). Fallback — старая логика парсинга sid.
        series_from_meta = _read_series_from_meta(sid)
        if series_from_meta:
            series, date_str = series_from_meta, _date_from_filename_uid(sid)
        else:
            series, date_str = _series_and_date(sid)
        target_md = _target_md_for_session(series, date_str, sid)
        if target_md.exists() or target_md.with_name(f"{date_str}-{sid}.md").exists():
            continue
        logger.info("Orphan pickup: %s → %s", sid, target_md)
        _copy_only(sid, series, date_str)


def _read_series_from_meta(filename_uid: str) -> str | None:
    """Прочитать series из meta.json (через ssh для не-LOCAL_FINALIZE, локально на VPS).

    filename_uid — имя без .meta.json (= то, что collector извлекает из ls).
    Возвращает series или None если поля нет / meta не доступен / парсинг упал.
    """
    # Защита от path traversal: filename_uid приходит из bash-парса имени файла
    # (`${f%.meta.json}`). Если кто-то положит файл с именем `../../../etc/passwd.meta.json`,
    # Path-конкатенация может уйти за пределы _tmp/transcripts/.
    if "/" in filename_uid or "\\" in filename_uid or filename_uid.startswith("."):
        return None
    if LOCAL_FINALIZE:
        meta_path = Path(VPS_TRANSCRIPTS) / f"{filename_uid}.meta.json"
        try:
            obj = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    else:
        # На маке: ssh cat. Меньше частный случай — на маке collector сейчас
        # не запускается, но архитектурно держим.
        cmd = f"cat {shlex.quote(f'{VPS_TRANSCRIPTS}/{filename_uid}.meta.json')}"
        try:
            proc = ssh_capture(cmd, timeout=10)
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            obj = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    val = obj.get("series")
    return val if isinstance(val, str) and val else None


def _copy_only(session_uid: str, series: str, date_str: str) -> None:
    """Только копирование готового .md, без finalize. Аналог хвоста _finalize_and_collect."""
    target_md = _target_md_for_session(series, date_str, session_uid)
    try:
        target_md.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("mkdir %s упал: %s", target_md.parent, e)
        return
    if target_md.exists():
        target_md = target_md.with_name(f"{date_str}-{session_uid}.md")
    tmp_md = target_md.with_suffix(target_md.suffix + ".part")
    src_md = Path(os.path.expanduser(f"~/meeting-notary/_tmp/protocols/{session_uid}.md"))
    try:
        shutil.copy2(src_md, tmp_md)
        os.replace(tmp_md, target_md)
    except OSError as e:
        logger.error("orphan copy %s → %s упал: %s", src_md, target_md, e)
        try:
            tmp_md.unlink()
        except FileNotFoundError:
            pass
        return
    logger.info("✓ Orphan pickup OK: %s", target_md)


def _finalize_and_collect(session_uid: str) -> None:
    """Запустить finalize-meeting.py на VPS для sessionUid, потом scp .md на мак."""
    # 1. Финализация на VPS (без claude — на VPS его нет, источник 3 пропустится).
    meta_path = f"{VPS_TRANSCRIPTS}/{session_uid}.meta.json"
    md_path = f"{VPS_PROTOCOLS}/{session_uid}.md"

    finalize_cmd = (
        f"set -e; "
        # Канонический путь — `/srv/meeting-notary/vexa/scripts/notary/` (куда rsync кладёт
        # `make deploy-notary`). Раньше было `~/meeting-notary/...` (host vexa-bot dir),
        # из-за чего finalize-meeting.py использовал устаревшую версию после deploy
        # (Ф2-обнаружение: цикл-проверки У3).
        f"cd /srv/meeting-notary/vexa/scripts/notary; "
        # Загружаем .env.notary. С Ф4 (2026-05-28) источник истины — /srv/meeting-notary/.env.notary
        # (там SPEECHMATICS_API_KEY, STT_BACKEND и рабочие ключи). Сначала source'им legacy
        # (~/meeting-notary/vexa/.env.notary) — оттуда нужны HF_TOKEN, TRANSCRIPTION_SERVICE_URL
        # для LEGACY-ветки STT_BACKEND=whisper_pyannote, и ENABLE_LLM_NAME_MAPPING. Затем
        # source /srv/.env.notary перетирает дубли — новый файл побеждает.
        f"set -a; "
        # `{{ ...; }} || true` — group command (не subshell), env пробрасывается
        # в родительский shell. Защищает от битого legacy-файла (синтаксис в .env.notary):
        # не валим всю финализацию из-за legacy, источник истины ниже всё равно загрузится.
        f"[ -f ~/meeting-notary/vexa/.env.notary ] && {{ source ~/meeting-notary/vexa/.env.notary; }} || true; "
        f"source /srv/meeting-notary/.env.notary; "
        f"set +a; "
        # ENABLE_LLM_NAME_MAPPING оставляем включённым (дефолт on): claude CLI
        # установлен и залогинен на VPS (Ф2 блок 2.4, 2026-05-27). LLM-маппинг
        # имён (lib/llm_postprocess.map_speaker_names) работает прямо здесь.
        f"unset FAKE_DIARIZATION_PATH; "  # HF-токен есть, real pyannote
        f"{VPS_VENV}/bin/python finalize-meeting.py {shlex.quote(meta_path)}"
    )
    logger.info("Финализирую %s на VPS …", session_uid)
    try:
        # 3 часа: CPU CCX13 + faster-whisper-medium даёт ~3x real-time на pyannote
        # и ~0.5x на whisper. На 21-мин аудио = 60-90 мин финализации. Запас 3 часа
        # покрывает 1-часовую встречу + claude-маппинг + scp. Параллельные тики
        # collector'а (раз в 5 мин) ловят активный finalize через docker-фильтр
        # vexa-notarius-* (контейнер ещё Up или Exited <STARTED_GRACE_MIN).
        proc = ssh_capture(f"bash -lc {shlex.quote(finalize_cmd)}", timeout=10800)
    except subprocess.TimeoutExpired:
        push(f"Финализация «{session_uid}» — timeout 3ч на VPS. Проверь ssh meeting-notary docker ps + логи.")
        logger.error("finalize timeout: %s", session_uid)
        return
    if proc.returncode != 0:
        logger.error("finalize rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:400])
        push(f"Финализация «{session_uid}» упала: rc={proc.returncode}. Лог: ~/Library/Logs/meeting-notary/collector.log")
        return

    # 2. Тянем meta.json + .md на мак (или копируем локально на VPS).
    series_from_finalize = _parse_series_from_finalize_stdout(proc.stdout)
    if series_from_finalize:
        series = series_from_finalize
        date_str = _date_from_filename_uid(session_uid)
        logger.info("Series из finalize JSON: %s (date %s)", series, date_str)
    else:
        series, date_str = _series_and_date(session_uid)
        logger.info("Series из имени файла (fallback): %s (date %s)", series, date_str)
    target_md = _target_md_for_session(series, date_str, session_uid)
    try:
        target_md.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("mkdir %s упал: %s", target_md.parent, e)
        return

    if target_md.exists():
        target_md = target_md.with_name(f"{date_str}-{session_uid}.md")

    tmp_md = target_md.with_suffix(target_md.suffix + ".part")

    if LOCAL_FINALIZE:
        # На VPS finalize уже положил .md в ~/meeting-notary/_tmp/protocols/<sid>.md —
        # копируем его в /srv/meeting-notary/protocols/<series>/<date>.md атомарно.
        src_md = Path(os.path.expanduser(f"~/meeting-notary/_tmp/protocols/{session_uid}.md"))
        try:
            shutil.copy2(src_md, tmp_md)
            os.replace(tmp_md, target_md)
        except OSError as e:
            logger.error("local copy %s → %s упал: %s", src_md, target_md, e)
            push(f"Не смог скопировать .md «{session_uid}» локально: {e}")
            try:
                tmp_md.unlink()
            except FileNotFoundError:
                pass
            return
        logger.info("✓ Протокол (local): %s", target_md)
        return

    # Мак-путь: scp с VPS через tmp + rename.
    scp_cmd = [
        "scp",
        f"{SSH_HOST}:{md_path}",
        str(tmp_md),
    ]
    try:
        scp = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        push(f"scp .md «{session_uid}» — timeout. Файл на VPS: {md_path}")
        logger.error("scp timeout: %s", session_uid)
        try:
            tmp_md.unlink()
        except FileNotFoundError:
            pass
        return
    if scp.returncode != 0:
        logger.error("scp rc=%d stderr=%s", scp.returncode, scp.stderr.strip()[:400])
        push(f"scp «{session_uid}» упал: rc={scp.returncode}. Лог в collector.log")
        try:
            tmp_md.unlink()
        except FileNotFoundError:
            pass
        return
    try:
        os.replace(tmp_md, target_md)
    except OSError as e:
        logger.error("os.replace %s → %s упал: %s", tmp_md, target_md, e)
        push(f"Не смог переименовать .md «{session_uid}»: {e}")
        return
    logger.info("✓ Протокол: %s", target_md)


_SESSION_RE = re.compile(r"^auto-(?P<mid>[^-]+(?:-[^-]+)*?)-(?P<dt>\d{4}\d{2}\d{2}T\d{6}Z)$")


def _parse_series_from_finalize_stdout(stdout: str) -> str | None:
    """Извлечь series из JSON-блока в конце stdout finalize-meeting.py.

    finalize-meeting.py печатает JSON через json.dumps(..., indent=2) сразу после
    лога `Protocol written → ...`. Сначала ищем JSON после этого маркера (надёжный
    anchor — отрезает любые traceback'и с фигурными скобками выше), потом
    fallback на rfind последнего «\\n{\\n» (на случай если маркер переименуют
    или вывод обрезан). Возвращает series или None, если поля нет / JSON битый.
    """
    if not stdout:
        return None
    marker = "Protocol written"
    marker_idx = stdout.rfind(marker)
    if marker_idx >= 0:
        tail = stdout[marker_idx:]
        idx_rel = tail.find("\n{\n")
        if idx_rel >= 0:
            try:
                obj = json.loads(tail[idx_rel + 1:])
            except (json.JSONDecodeError, ValueError):
                obj = None
            if isinstance(obj, dict):
                val = obj.get("series")
                if isinstance(val, str) and val:
                    return val
    idx = stdout.rfind("\n{\n")
    if idx >= 0:
        idx += 1
    elif stdout.lstrip().startswith("{"):
        idx = stdout.find("{")
    else:
        return None
    try:
        obj = json.loads(stdout[idx:])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    val = obj.get("series")
    return val if isinstance(val, str) and val else None


def _date_from_filename_uid(filename_uid: str) -> str:
    """Имя meta-файла на диске: 2026-05-27-tm-<id>. Первые 10 символов = дата."""
    if len(filename_uid) >= 10 and filename_uid[4] == "-" and filename_uid[7] == "-":
        return filename_uid[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
