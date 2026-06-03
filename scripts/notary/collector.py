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

# Ф4: матчинг «бот ещё в звонке». Контейнеры нотариуса именуются
# `vexa-notarius-*`; runner ставит на них label `meeting-notary.series=<series>`.
NOTARIUS_NAME_PREFIX = "vexa-notarius-"
SERIES_LABEL = "meeting-notary.series"


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

    # 1. Все *.meta.json — потенциальные кандидаты. Ранее `list_cmd` отсеивал
    # уже-финализированные через `[[ -f $VPS_PROTOCOLS/<sid>.md ]]`. Это
    # работало только для legacy-кейса (плоский <sid>.md в корне protocols),
    # а для series-встреч — всегда возвращал false (finalize кладёт в
    # `<protocols>/<series>/<date>.md`, не в корень). Это и есть **рассинхрон
    # 29.05** — collector думал «не финализирована», и запускал finalize
    # повторно после успешной первой итерации.
    # Сейчас (Ф1-доработки 29.05): отсев идёт через idempotency-guard ниже
    # по `meta.delivered`, а наличие .md проверяется по реальному
    # destination-пути в MEETINGS_DIR через `_target_md_for_session`.
    list_cmd = (
        f"set -e; "
        f"cd {VPS_TRANSCRIPTS}; "
        f"shopt -s nullglob; "
        f"for f in *.meta.json; do "
        f"  echo \"${{f%.meta.json}}\"; "
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
        logger.info("Нет meta.json — нечего финализировать")
        return 0
    logger.info("Candidate sessions (%d): %s", len(pending), pending)

    # 2. Для каждого — проверить, что бот завершился (контейнер ушёл).
    # ИСТОРИЯ БАГА 4Г (Ф4-доработки 29.05): раньше тут строилось
    # `cont_name = vexa-notarius-{sid}`, где `sid` — имя meta-файла на диске
    # (`<date>-tm-<ms>`, см. recording.ts:318 `tm-${Date.now()}`). Реальное имя
    # контейнера — `vexa-notarius-auto-<meeting_id>-<start>` (runner._safe_session_uid).
    # Они НИКОГДА не совпадали → гейт «бот ещё в звонке» был мёртв, и при живом
    # боте collector мог запустить finalize на ещё дописываемом WAV (катастрофа
    # для Ф5 chunk-декаплинга, где meta может появиться при активной записи).
    # Сейчас резолвим контейнер по docker label `meeting-notary.series`, который
    # runner ставит при старте (sessionUid в meta `tm-...` ≠ session_uid
    # контейнера `auto-...` — не связаны; series — единственный надёжный общий
    # ключ, runner пишет его и в BOT_CONFIG→meta, и в label). См. handoff Ф4.
    running_index = _running_notarius_index()

    for sid in pending:
        meta = _read_meta_obj(sid)
        running = _find_running_container_for(meta, running_index)
        if running:
            logger.info("Скип %s — бот ещё в звонке (контейнер %s)", sid, running)
            continue
        # Idempotency-guard (Ф1-доработки 29.05): если meta.delivered непустой,
        # протокол уже доставлен — finalize запускать НЕ нужно. Также
        # пробуем убрать WAV, если он ещё на диске (cleanup отложенный из
        # прошлой итерации, см. ниже).
        if meta is not None and _delivery_done(meta):
            logger.info("Скип %s — уже доставлен (meta.delivered непустой)", sid)
            _maybe_cleanup_wav_after_delivered(meta, sid)
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


def _read_meta_obj(filename_uid: str) -> dict | None:
    """Читает meta.json целиком (для LOCAL_FINALIZE и через ssh).

    Возвращает dict или None если файла нет / битый JSON / path traversal.
    """
    if "/" in filename_uid or "\\" in filename_uid or filename_uid.startswith("."):
        return None
    if LOCAL_FINALIZE:
        meta_path = Path(VPS_TRANSCRIPTS) / f"{filename_uid}.meta.json"
        try:
            obj = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    else:
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
    return obj


def _delivery_done(meta: dict) -> bool:
    """`meta.delivered` непустой массив с хотя бы одной успешной записью.

    Принимает новый формат (массив записей) И старый (один объект) —
    в обоих случаях успех = есть `message_ids` и НЕТ `decision=partial-failure`.
    """
    raw = meta.get("delivered")
    if isinstance(raw, dict):
        records = [raw]
    elif isinstance(raw, list):
        records = [r for r in raw if isinstance(r, dict)]
    else:
        return False
    for r in records:
        msgs = r.get("message_ids") or []
        decision = r.get("decision")
        if msgs and decision != "partial-failure":
            return True
    return False


def _running_notarius_index() -> dict[str, Any]:
    """Снимок живых контейнеров нотариуса за один `docker ps`.

    Возвращает {"by_series": {series: [names]}, "unlabeled": [names]}.

    Фильтруем по ИМЕНИ (`vexa-notarius-*`), а не по label — чтобы поймать и
    legacy-контейнеры без наших Ф4-label'ов (запущенные до деплоя Ф4). У каждого
    тащим label `meeting-notary.series` (пусто = legacy/без проводки). При
    timeout / ошибке docker считаем, что активных нет (как и старая логика).
    """
    fmt = '{{.Names}}\t{{.Label "' + SERIES_LABEL + '"}}'
    by_series: dict[str, list[str]] = {}
    unlabeled: list[str] = []
    result = {"by_series": by_series, "unlabeled": unlabeled}
    cmd = f"docker ps --filter name={NOTARIUS_NAME_PREFIX} --format {shlex.quote(fmt)}"
    try:
        ps_proc = ssh_capture(cmd, timeout=20)
    except subprocess.TimeoutExpired:
        logger.warning("docker ps timeout — считаем что нет активных ботов")
        return result
    if ps_proc.returncode != 0:
        logger.warning(
            "docker ps rc=%d stderr=%s — считаем что нет активных",
            ps_proc.returncode, (ps_proc.stderr or "").strip()[:200],
        )
        return result
    for line in (ps_proc.stdout or "").splitlines():
        if not line.strip():
            continue
        name, _, series = line.partition("\t")
        name = name.strip()
        series = series.strip()
        if not name:
            continue
        if series:
            by_series.setdefault(series, []).append(name)
        else:
            unlabeled.append(name)
    return result


def _find_running_container_for(meta: dict | None, running_index: dict[str, Any]) -> str | None:
    """Имя живого контейнера-бота для этой встречи, либо None (Ф4, баг 4Г).

    Резолв через docker label `meeting-notary.series` (ставит runner), а НЕ через
    имя meta-файла. Ключ сопоставления — `series`: meta.sessionUid (`tm-<ms>`,
    генерит бот recording.ts:318) и session_uid контейнера (`auto-<id>-<start>`,
    runner) не связаны; единственный надёжный общий ключ — series (runner пишет
    его и в BOT_CONFIG→meta, и в label). Обоснование выбора series вместо
    sessionUid — handoff Ф4.

    Консервативная страховка: если жив legacy-контейнер без наших label'ов
    (transition-окно до деплоя Ф4) и точечного совпадения по series нет — вернём
    его имя. Инвариант runner: параллельных Vexa-сессий нет (`_is_vexa_running`),
    значит любой живой бот = идёт запись → лучше отложить finalize на тик, чем
    запустить его на пишущемся WAV.
    """
    by_series = running_index.get("by_series") or {}
    unlabeled = running_index.get("unlabeled") or []
    series = meta.get("series") if isinstance(meta, dict) else None
    if isinstance(series, str) and series and by_series.get(series):
        return by_series[series][0]
    if unlabeled:
        return unlabeled[0]
    return None


def _maybe_cleanup_wav_after_delivered(meta: dict, sid: str) -> None:
    """Удаляет WAV, если delivery УЖЕ подтверждена И WAV ещё на диске.

    Применяется в двух кейсах:
      1. Тик после доставки: idempotent-guard срабатывает раньше, чем мы
         успели чистить WAV (например, finalize упал в момент очистки).
      2. Перепрогон с другого хоста: meta.delivered есть, но WAV не убрался.

    keepAudio / keep_audio в meta — пропускаем cleanup (как и раньше).
    """
    if meta.get("keepAudio") or meta.get("keep_audio"):
        logger.info("WAV cleanup skip (sid=%s) — keep_audio=true", sid)
        return
    wav_path = (meta.get("files") or {}).get("wav")
    if not wav_path:
        return
    if not LOCAL_FINALIZE:
        # НОВ2 хода 4: на маке (не-LOCAL_FINALIZE) collector в боевом конвейере
        # НЕ запускается — только дебаг. Не делаем ssh rm против реального
        # VPS из дебаг-сессии — слишком легко случайно убить WAV рабочей
        # встречи. Если действительно нужно — добавить env-флаг
        # MEETING_NOTARY_ALLOW_SSH_CLEANUP=1.
        if os.environ.get("MEETING_NOTARY_ALLOW_SSH_CLEANUP") != "1":
            logger.info(
                "WAV ssh-cleanup пропущен (sid=%s, path=%s) — не LOCAL_FINALIZE; "
                "защита от удаления чужого WAV из дебаг-сессии",
                sid, wav_path,
            )
            return
        try:
            proc = ssh_capture(
                f"rm -f {shlex.quote(wav_path)}",
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            logger.warning("WAV cleanup ssh timeout (sid=%s)", sid)
            return
        if proc.returncode == 0:
            logger.info("WAV удалён через ssh (sid=%s, path=%s)", sid, wav_path)
        return
    p = Path(wav_path)
    if not p.exists():
        return
    try:
        p.unlink()
        logger.info("WAV удалён после доставки (sid=%s, path=%s)", sid, wav_path)
    except OSError as e:
        logger.warning("Не смог удалить WAV %s: %s", wav_path, e)


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
        push(
            f"Финализация «{session_uid}» — timeout 3ч на VPS. Проверь ssh meeting-notary docker ps + логи.",
            dedupe_key=f"finalize-timeout:{session_uid}",
        )
        logger.error("finalize timeout: %s", session_uid)
        return
    # rc=10 «nothing to do» (Ф1-доработки 29.05): WAV почищен, но
    # meta.delivered подтверждает успешную доставку. Не алертим, не копируем.
    if proc.returncode == 10:
        logger.info(
            "finalize rc=10 «nothing to do» для %s — WAV отсутствует, "
            "доставка подтверждена в meta.delivered. Никаких действий не требуется.",
            session_uid,
        )
        return
    if proc.returncode != 0:
        logger.error("finalize rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:400])
        # REQ 1.5: per-meeting ключ дедупа. Потеря WAV (rc=3) и прочие сбои
        # finalize по ОДНОЙ встрече глушатся 6ч-дедупом, но НОВАЯ потеря по
        # другой встрече (другой session_uid) всегда доходит до владельца —
        # P0-алерт не проглатывается из-за совпадения текста сообщения.
        push(
            f"Финализация «{session_uid}» упала: rc={proc.returncode}. Лог: ~/Library/Logs/meeting-notary/collector.log",
            dedupe_key=f"finalize-fail:{session_uid}",
        )
        return

    # 2. Парсим результат finalize'а (один блок JSON с series + delivery + paths).
    finalize_result = _parse_finalize_result(proc.stdout) or {}
    series_from_finalize = finalize_result.get("series") if isinstance(finalize_result.get("series"), str) else None
    delivery = finalize_result.get("delivery") or {}
    delivery_status = delivery.get("status") if isinstance(delivery, dict) else None
    finalize_protocol_path = finalize_result.get("protocol_path")

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

    md_placed = False

    if LOCAL_FINALIZE:
        # На VPS finalize кладёт .md по тому пути, что отдал в stdout
        # JSON (`protocol_path`). Раньше collector брал жёстко
        # `~/meeting-notary/_tmp/protocols/<sid>.md` — это устаревший плоский
        # путь, и для series-встреч он отсутствует (finalize пишет в
        # `<output-dir>/<series>/<date>.md`). Это и был рассинхрон 29.05:
        # collector не находил .md → push «finalize упал» → следующий тик
        # запускал повторную финализацию на (теперь почищенном) WAV.
        # Сейчас (Ф1-доработки): source — `protocol_path` из stdout JSON,
        # fallback на legacy путь — только если JSON не пришёл.
        if finalize_protocol_path and os.path.exists(finalize_protocol_path):
            src_md = Path(finalize_protocol_path)
        else:
            src_md = Path(os.path.expanduser(f"~/meeting-notary/_tmp/protocols/{session_uid}.md"))
        try:
            same_root = src_md.resolve() == target_md.resolve()
        except OSError:
            same_root = False
        if same_root:
            # finalize уже положил файл в финальное место — копировать нечего.
            md_placed = target_md.exists()
            if md_placed:
                logger.info("✓ Протокол (in-place от finalize): %s", target_md)
        else:
            try:
                shutil.copy2(src_md, tmp_md)
                os.replace(tmp_md, target_md)
                md_placed = True
            except OSError as e:
                logger.error("local copy %s → %s упал: %s", src_md, target_md, e)
                push(f"Не смог скопировать .md «{session_uid}» локально: {e}")
                try:
                    tmp_md.unlink()
                except FileNotFoundError:
                    pass
            if md_placed:
                logger.info("✓ Протокол (local): %s", target_md)
    else:
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
            md_placed = True
        except OSError as e:
            logger.error("os.replace %s → %s упал: %s", tmp_md, target_md, e)
            push(f"Не смог переименовать .md «{session_uid}»: {e}")
            return
        logger.info("✓ Протокол: %s", target_md)

    # 3. WAV cleanup (Ф1-доработки 29.05). Удаляем WAV ТОЛЬКО когда оба
    # условия выполнены:
    #   (а) `.md` лежит в MEETINGS_DIR (`md_placed=True`);
    #   (б) доставка в TG подтверждена (`delivery.status in {sent, skipped}`).
    # «skipped» здесь = idempotent skip (уже доставлено в этот chat_id),
    # это легитимный успех. Для статусов «asked»/«error»/«disabled» WAV
    # остаётся — даст возможность безболезненно перепрогнать finalize после
    # ответа Ильи / починки токена / включения флага.
    success_statuses = {"sent", "skipped"}
    meta_obj = _read_meta_obj(session_uid) or {}
    keep_audio_flag = bool(meta_obj.get("keepAudio") or meta_obj.get("keep_audio"))
    if keep_audio_flag:
        logger.info("WAV cleanup пропущен (sid=%s): keep_audio=true в meta", session_uid)
        return
    if not md_placed:
        logger.info(
            "WAV сохранён (sid=%s): .md ещё не на финальном месте — даём шанс перепрогону",
            session_uid,
        )
        return
    if delivery_status not in success_statuses:
        logger.info(
            "WAV сохранён (sid=%s): delivery.status=%r не в %s — даём шанс перепрогону",
            session_uid, delivery_status, sorted(success_statuses),
        )
        return
    wav_path = (meta_obj.get("files") or {}).get("wav")
    if wav_path and Path(wav_path).exists():
        try:
            Path(wav_path).unlink()
            logger.info(
                "WAV удалён после успешной доставки (sid=%s, delivery=%s, path=%s)",
                session_uid, delivery_status, wav_path,
            )
        except OSError as e:
            logger.warning("Не смог удалить WAV %s: %s", wav_path, e)


_SESSION_RE = re.compile(r"^auto-(?P<mid>[^-]+(?:-[^-]+)*?)-(?P<dt>\d{4}\d{2}\d{2}T\d{6}Z)$")


def _parse_finalize_result(stdout: str) -> dict | None:
    """Достаёт **весь** JSON-блок результата из stdout finalize-meeting.py.

    Парсер ищет JSON после маркера `Protocol written`; fallback — последний
    `\\n{\\n`. См. `_parse_series_from_finalize_stdout` для подробностей про
    anchor. Возвращает dict или None.
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
                if isinstance(obj, dict):
                    return obj
            except (json.JSONDecodeError, ValueError):
                pass
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
    return obj if isinstance(obj, dict) else None


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
