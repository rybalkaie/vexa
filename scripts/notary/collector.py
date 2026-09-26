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

import contextlib
import fcntl
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
from notary.lib import series_memory  # noqa: E402  # Ф4 (task 5): прод-синк памяти серии

LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR") or "~/Library/Logs/meeting-notary"
))
LOG_FILE = LOG_DIR / "collector.log"

# CG1 — per-session flock-каталог. Защищает от двойной оплаты Speechmatics, когда
# два тика collector'а (раз в 5 мин) пересеклись на одной не-доставленной встрече
# (реальный инцидент 27.05: одна встреча ушла в платный движок 4 раза подряд).
# Лок берётся НЕБЛОКИРУЮЩЕ: занят (finalize этой встречи ещё идёт) → тик скипает
# встречу и попробует на следующем заходе. Каталог настраивается через env
# (тесты подменяют на tmp), дефолт — рядом с логами.
STATE_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_STATE_DIR") or "~/Library/Logs/meeting-notary"
))

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

# Ф1 (ISS-28) R6: куда персистим `docker logs` бота, чтобы они переживали
# `docker rm`. Логи бот-контейнера живут в дефолтном json-драйвере docker и
# умирают при удалении контейнера (weekly cleanup-таймер снимает Exited
# vexa-notarius-* старше 7д). Без дампа причина сбойного прогона (ISS-28:
# close-код WS, причина навигации) теряется. На VPS — host-путь, переживает rm.
BOT_CONTAINER_LOG_DIR = (
    os.environ.get("MEETING_NOTARY_BOT_LOG_DIR") or "/srv/meeting-notary/logs/bot-containers"
)
_SAFE_CONTAINER_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _env_float(name: str, default: float) -> float:
    """Положительный float из env, иначе default (терпим мусор/пусто)."""
    try:
        v = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


# ISS-31/Ф1: строки-предупреждения из stderr finalize-подпроцесса. При rc=0
# finalize мог напечатать WARNING-и генерации (деградация в Sonnet, «нет шапки»,
# ретрай Ф2) — раньше stderr читался ТОЛЬКО в ветке rc≠0, и причина «протокол
# собрался, но со второй попытки / на резервной модели» молча выбрасывалась.
# Фильтруем СТРОГО по уровню логгера finalize (`[WARNING]`/`[ERROR]` из формата
# `%(levelname)s`, см. `finalize-meeting.py:setup_logging`) — только метаданные
# (текст реплик finalize в stderr не печатает; опасная тройка соблюдена на
# стороне генератора). Маркер `[protocol]` НЕ добавляем в regex отдельно: его
# несут и WARNING-строки (уже покрыты уровнем), и INFO-строки об успехе
# (`llm_postprocess.py` — «[protocol] generated meeting=...» на КАЖДОМ успешном
# прогоне, «[protocol] FU-12: ... нормализация пропущена») — ловить его сам по
# себе означало бы репортить «warning» на 100% успешных финализаций (ложный
# сигнал, противоречит цели REQ 31.4 — видеть именно проблемы, не шум).
_FINALIZE_WARN_RE = re.compile(r"\[(?:WARNING|ERROR)\]")


def _finalize_stderr_warnings(stderr: str, *, limit: int = 40) -> list[str]:
    """WARNING/ERROR-строки из stderr finalize (метаданные, без текста реплик)."""
    if not stderr:
        return []
    out: list[str] = []
    for line in stderr.splitlines():
        s = line.strip()
        if s and _FINALIZE_WARN_RE.search(s):
            out.append(s[:300])
            if len(out) >= limit:
                break
    return out


# Ф3 (2026-06-03): watchdog зависшего bot-контейнера. Зависшим считаем контейнер,
# который (а) живёт дольше любой реальной встречи — наблюдаемый максимум ~122 мин
# (Татьяна); бот сам уходит после 20 мин тишины (`endReason=silence_20min`),
# поэтому > 2ч живёт только при зависшем авто-выходе → дефолт-порог 4ч с запасом;
# ЛИБО (б) его запись (WAV/chunk) не растёт дольше STALL-порога. Порог STALL
# заведомо > 20 мин (окно авто-выхода бота по тишине), иначе убьём бота,
# легитимно досиживающего паузу → дефолт 30 мин. `docker kill` БЕЗОПАСЕН для WAV:
# Ф2 пишет валидную шапку на диск каждые ~3с — на диске лежит готовый WAV до
# любого kill. Пороги настраиваемы через env; watchdog можно выключить
# MEETING_NOTARY_WATCHDOG=0.
WATCHDOG_ENABLED = os.environ.get("MEETING_NOTARY_WATCHDOG", "1") != "0"
WATCHDOG_MAX_HOURS = _env_float("MEETING_NOTARY_WATCHDOG_MAX_HOURS", 4.0)
WATCHDOG_STALL_MIN = _env_float("MEETING_NOTARY_WATCHDOG_STALL_MIN", 30.0)


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

# Куда collector кладёт ДОЛГОВЕЧНУЮ копию (архив). На маке — scp с VPS в
# ~/Projects/me/встречи/. На VPS (LOCAL_FINALIZE=1) — /srv/meeting-notary/protocols/,
# mirror-агент мака притащит позже.
#
# ISS-32 (Ф3, 2026-07-14): источник — `MEETING_NOTARY_ARCHIVE_DIR` (долговечный
# архив), с фолбэком на рабочую `MEETING_NOTARY_PROTOCOLS_DIR`. Раньше читалась
# ТОЛЬКО рабочая переменная — когда её перевели на `_tmp` (для listener/reissue),
# долговечная копия стала in-place no-op и `/srv`/мак-архив замерли на месяц.
# Разведение: finalize пишет в PROTOCOLS_DIR (`_tmp`), collector копирует в
# ARCHIVE_DIR (`/srv`). Дефолт ARCHIVE_DIR = PROTOCOLS_DIR → архив == рабочая →
# копия in-place, поведение как до Ф3 (обратная совместимость).
MEETINGS_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_ARCHIVE_DIR")
    or os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR")
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

    # Ф3 (REQ 3.2): watchdog зависших bot-контейнеров — ДО finalize-петли, чтобы
    # убитый контейнер освободил серию уже в этом тике. Не должен ронять collector.
    try:
        _run_watchdog()
    except Exception as e:  # noqa: BLE001
        logger.exception("watchdog упал (не фатально для collector): %s", e)

    # Ф10c (R13.3): очередь доставки разбирается ПЕРВЫМ делом и БЕЗУСЛОВНО.
    # Ниже у тика четыре ранних `return 0` (нет meta, ssh не ответил, список
    # пуст) — поставь разбор только в конце, и результат, положенный в очередь
    # перед рестартом, ждал бы СЛЕДУЮЩЕЙ ЗАПИСАННОЙ ВСТРЕЧИ. Ровно так теряются
    # результаты в выходные.
    _portal_delivery_tick()

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
        # Ф10c: удержание исходников ставим ДО финализации — она сама сносит
        # копии из `_failed/` после успешного разбора.
        _portal_note_recording(sid, meta)
        # Idempotency-guard (Ф1-доработки 29.05): если meta.delivered непустой,
        # протокол уже доставлен — finalize запускать НЕ нужно. Также
        # пробуем убрать WAV, если он ещё на диске (cleanup отложенный из
        # прошлой итерации, см. ниже).
        if meta is not None and _delivery_done(meta):
            logger.info("Скип %s — уже доставлен (meta.delivered непустой)", sid)
            _cleanup_delivered_under_lock(meta, sid)
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
    # Ф10c (R13.3): очередь доставки в портал разбирается КАЖДЫЙ тик и
    # независимо от того, была ли в этом тике новая встреча — иначе результат,
    # положенный в очередь перед рестартом, ждал бы следующей записи.
    _portal_delivery_tick()
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
    его имя. С Ф3 (2026-06-03) разные серии пишутся параллельно, поэтому
    блокировать finalize нужно ТОЛЬКО для своей серии (точное совпадение по
    label) — finalize встречи серии X не должен ждать живого бота серии Y. Но
    unlabeled legacy-бот (серию не знаем) по-прежнему трактуем перестраховочно:
    лучше отложить finalize на тик, чем запустить его на возможно пишущемся WAV.
    """
    by_series = running_index.get("by_series") or {}
    unlabeled = running_index.get("unlabeled") or []
    series = meta.get("series") if isinstance(meta, dict) else None
    if isinstance(series, str) and series and by_series.get(series):
        return by_series[series][0]
    if unlabeled:
        return unlabeled[0]
    return None


# ─── Ф3 (2026-06-03): watchdog зависшего bot-контейнера (REQ 3.2) ───

_DOCKER_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?")


def _parse_docker_time(s: str) -> datetime | None:
    """RFC3339 `State.StartedAt` от docker → aware datetime UTC, либо None.

    docker отдаёт наносекунды (`...:54.243456789Z`), а `datetime.fromisoformat`
    ест максимум микросекунды (6 знаков) → обрезаем дробную часть до 6. Пояс
    docker всегда UTC (`Z`). `0001-01-01...` = sentinel «контейнер не стартовал».
    """
    if not s or s.strip().startswith("0001-01-01"):
        return None
    m = _DOCKER_TS_RE.match(s.strip())
    if not m:
        return None
    # Go RFC3339Nano срезает хвостовые нули → дробь может быть 1–9 знаков
    # (`.5`, `.243456789`). `datetime.fromisoformat` на Python <3.11 принимает
    # дробь ТОЛЬКО из 3 или 6 знаков → нормализуем к ровно 6 (паддинг справа
    # нулями + обрезка) для версионной независимости.
    digits = (m.group(2) or "").lstrip(".")
    micro = f".{digits[:6]:0<6}" if digits else ""  # fill '0', left-align, ширина 6
    try:
        dt = datetime.fromisoformat(m.group(1) + micro)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc)


def _docker_started_at(name: str) -> datetime | None:
    """`docker inspect State.StartedAt` контейнера → datetime UTC, либо None."""
    cmd = ("docker inspect --format " + shlex.quote("{{.State.StartedAt}}") +
           " " + shlex.quote(name))
    try:
        proc = ssh_capture(cmd, timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning("watchdog: docker inspect %s — timeout", name)
        return None
    if proc.returncode != 0:
        logger.warning("watchdog: docker inspect %s rc=%d", name, proc.returncode)
        return None
    return _parse_docker_time(proc.stdout or "")


def _newest_recording_activity_sec(now: datetime, started_at: datetime | None) -> float | None:
    """Секунд с последнего роста записи (WAV/chunk) контейнера, либо None.

    Активность = mtime самого свежего `*.wav` в каталоге транскриптов,
    появившегося в окне жизни контейнера (mtime ≥ StartedAt − запас). `*.wav`
    покрывает и одиночный WAV, и `*.chunkN.wav` (Ф5). Исключаем
    `*.finalize-concat.wav` (артефакт finalize, не запись); `.recover-bak`/`.pcm`
    не оканчиваются на `.wav` → и так мимо. Старые WAV прошлых встреч отсекает
    фильтр по mtime. None — подходящих файлов нет (запись ещё не появилась / уже
    почищена) → вызывающий пропустит STALL-триггер (остаётся age-триггер).

    При нескольких параллельных записях окна жизни пересекаются: контейнер видит
    «самый свежий WAV в своём окне», возможно чужой → STALL сработает, лишь когда
    стихли ВСЕ записи. Это осознанно консервативно: НИКОГДА не убить здоровую
    встречу; зависшую при этом добьёт age-триггер.
    """
    if not LOCAL_FINALIZE:
        return None
    # Без StartedAt окно жизни контейнера неизвестно → НЕ оцениваем активность
    # (иначе взяли бы новейший из ВСЕХ *.wav, включая древние с прошлых встреч →
    # ложный STALL → false-kill живого/неизвестного контейнера, Н2 цикла Ф3).
    # Возврат None = STALL-триггер пропущен, остаётся только age-триггер.
    if started_at is None:
        return None
    base = Path(VPS_TRANSCRIPTS)
    if not base.is_dir():
        return None
    floor = started_at.timestamp() - 120
    newest: float | None = None
    try:
        for p in base.glob("*.wav"):
            if p.name.endswith(".finalize-concat.wav"):
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt < floor:
                continue
            if newest is None or mt > newest:
                newest = mt
    except OSError:
        return None
    if newest is None:
        return None
    return max(0.0, now.timestamp() - newest)


def _watchdog_verdicts(
    containers: list[dict[str, Any]], *, max_age_sec: float, stall_sec: float,
) -> list[tuple[str, str | None, str]]:
    """Чистое решение, какие контейнеры зависли (тестируемо без docker/диска).

    containers: list[{"name", "series", "age_sec": float|None, "stale_sec": float|None}]
      age_sec   — сколько живёт контейнер (now − StartedAt);
      stale_sec — сколько НЕ растёт запись (now − newest mtime), None = не оценить.
    Возвращает [(name, series, reason)] для зависших. Консервативно: если оба
    показателя None — НЕ трогаем (на неопределённости не убиваем). age приоритетнее
    stall (жёсткий потолок длительности).
    """
    out: list[tuple[str, str | None, str]] = []
    for c in containers:
        name = c.get("name")
        if not name:
            continue
        age = c.get("age_sec")
        stale = c.get("stale_sec")
        if age is not None and age > max_age_sec:
            out.append((name, c.get("series"),
                        f"живёт {age / 3600:.1f}ч > порога {max_age_sec / 3600:.1f}ч "
                        f"(встреча не идёт так долго — авто-выход бота, видимо, завис)"))
            continue
        if stale is not None and stale > stall_sec:
            out.append((name, c.get("series"),
                        f"запись не растёт {stale / 60:.0f} мин > порога {stall_sec / 60:.0f} мин "
                        f"(бот не ушёл после тишины — запись мертва)"))
            continue
    return out


def _kill_container(name: str) -> bool:
    """`docker kill <name>`. True при rc=0. Безопасно для WAV (Ф2 periodic flush)."""
    cmd = "docker kill " + shlex.quote(name)
    try:
        proc = ssh_capture(cmd, timeout=20)
    except subprocess.TimeoutExpired:
        logger.error("watchdog: docker kill %s — timeout", name)
        return False
    if proc.returncode != 0:
        logger.error("watchdog: docker kill %s rc=%d stderr=%s", name,
                     proc.returncode, (proc.stderr or "").strip()[:200])
        return False
    return True


def _persist_container_log(name: str) -> bool:
    """Ф1 (ISS-28) R6: `docker logs <name>` → долговечный файл на хосте.

    Лог бот-контейнера переживает `docker rm` (weekly cleanup/watchdog). Дамп
    идемпотентен (перезапись). СОДЕРЖИМОЕ В ПИТОН НЕ ЧИТАЕМ (опасная тройка) —
    редирект на диск делает удалённая/локальная шелл-команда. Best-effort и
    тихо: сбой не должен ронять ни watchdog, ни finalize. True при rc=0.

    Путь: BOT_CONTAINER_LOG_DIR/<name>.log на ХОСТЕ (переживает удаление
    контейнера). `docker logs` пишет и stdout, и stderr — забираем оба (2>&1).
    """
    if not name:
        return False
    safe = _SAFE_CONTAINER_RE.sub("_", name)
    log_dir_q = shlex.quote(BOT_CONTAINER_LOG_DIR)
    dst_q = shlex.quote(f"{BOT_CONTAINER_LOG_DIR}/{safe}.log")
    cmd = f"mkdir -p {log_dir_q} && docker logs {shlex.quote(name)} > {dst_q} 2>&1"
    try:
        proc = ssh_capture(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        logger.warning("R6: persist docker logs %s — timeout", name)
        return False
    if proc.returncode != 0:
        logger.warning("R6: persist docker logs %s rc=%d stderr=%s", name,
                       proc.returncode, (proc.stderr or "").strip()[:200])
        return False
    logger.info("R6: лог бот-контейнера сохранён → %s.log", safe)
    return True


def _latest_exited_notarius_for_series(series: str | None) -> str | None:
    """Имя самого свежего ВЫШЕДШЕГО bot-контейнера серии (для R6 persist после finalize).

    Резолв по label `meeting-notary.series` (тот же ключ, что у finalize-гейта).
    `docker ps -a` отдаёт newest-first → берём первую строку. None при
    отсутствии/ошибке/таймауте (best-effort, не критично).
    """
    if not series:
        return None
    fmt = "{{.Names}}"
    cmd = (
        f"docker ps -a --filter name={NOTARIUS_NAME_PREFIX} "
        f"--filter label={SERIES_LABEL}={shlex.quote(series)} "
        f"--filter status=exited --format {shlex.quote(fmt)}"
    )
    try:
        proc = ssh_capture(cmd, timeout=20)
    except subprocess.TimeoutExpired:
        logger.warning("R6: docker ps -a (series=%s) — timeout", series)
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        nm = line.strip()
        if nm:
            return nm
    return None


def _run_watchdog(now: datetime | None = None) -> None:
    """Найти и убить зависшие bot-контейнеры (REQ 3.2). Только на VPS.

    Гоняется в начале тика collector'а ДО finalize-петли: убитый контейнер
    освобождает серию уже в этом тике (finalize гейтится по живому контейнеру),
    и встреча дособерётся, как только meta на диске. Не должен ронять collector —
    вызывается под try/except в main().
    """
    if not (WATCHDOG_ENABLED and LOCAL_FINALIZE):
        return
    if now is None:
        now = datetime.now(timezone.utc)
    index = _running_notarius_index()
    series_of: dict[str, str | None] = {}
    for s, names in (index.get("by_series") or {}).items():
        for nm in names:
            series_of[nm] = s
    for nm in (index.get("unlabeled") or []):
        series_of.setdefault(nm, None)
    if not series_of:
        return
    containers: list[dict[str, Any]] = []
    for nm in series_of:
        started = _docker_started_at(nm)
        age = (now - started).total_seconds() if started else None
        stale = _newest_recording_activity_sec(now, started)
        containers.append({"name": nm, "series": series_of.get(nm),
                           "age_sec": age, "stale_sec": stale})
    verdicts = _watchdog_verdicts(
        containers,
        max_age_sec=WATCHDOG_MAX_HOURS * 3600.0,
        stall_sec=WATCHDOG_STALL_MIN * 60.0,
    )
    for name, series, reason in verdicts:
        # R6 (ISS-28): персист логов ПЕРЕД kill — зависший/сбойный бот это ровно
        # тот прогон, чью причину надо сохранить до того, как weekly cleanup
        # снимет контейнер. Не критично для kill (best-effort, тихо).
        _persist_container_log(name)
        killed = _kill_container(name)
        label = series or name
        push(
            f"⚠️ Watchdog убил зависший бот-контейнер «{label}»: {reason}. "
            f"{'docker kill OK' if killed else 'docker kill НЕ удался — проверь руками'}. "
            f"WAV на диске валиден (Ф2). Если встреча успела закрыть chunk (была пауза) — "
            f"meta.json есть, финализация подберёт на следующем тике. Если шла без пауз — "
            f"meta.json мог не записаться (SIGKILL минует final_exit): проверь "
            f"~/meeting-notary/_tmp/transcripts/ на WAV без .meta.json и восстанови вручную.",
            dedupe_key=f"watchdog-kill:{name}",
        )
        logger.warning("watchdog: контейнер %s (series=%s) — %s; kill=%s",
                       name, series, reason, killed)


def _portal_sources_held(sid: str, meta: dict | None = None) -> bool:
    """Ф10c (R13.4): держим ли исходники этой встречи до подтверждения портала.

    🔴 ЕДИНСТВЕННЫЙ гейт удаления, и он FAIL-CLOSED: сбой чтения удержания
    отвечает «держим». Удаление WAV необратимо, а цена ошибки несимметрична —
    лишний файл на диске чинится одной командой, стёртая запись разговора не
    чинится ничем (класс инцидента 17.07: «протокол ушёл в Telegram → WAV
    удалён → портал лежал → результат потерян»).

    При `ENABLE_PORTAL_ORDERS=OFF` всегда False — поведение владельца на его
    личных сериях остаётся буква в букву сегодняшним.
    """
    try:
        from notary.lib import portal_delivery  # noqa: PLC0415

        if not portal_delivery.is_enabled():
            return False
        held = portal_delivery.is_held(sid)
        # Маркер рядом с WAV — договорённость с метельщиком из systemd (он bash
        # и JSON-удержание прочитать не может). Синхронизируем в ОБЕ стороны:
        # забытый маркер запретил бы метельщику вечно, и `<sid>.txt`/`<sid>.meta.json`
        # остались бы на диске навсегда после удаления самого WAV.
        portal_delivery.sync_wav_marker(((meta or {}).get("files") or {}).get("wav"), sid)
        return held
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Удержание исходников не прочитано (%s) — WAV сохраняю (fail-closed)",
            type(e).__name__,
        )
        return True


def _portal_order_for(sid: str, meta: dict | None) -> dict | None:
    """Наряд портала, которому принадлежит эта запись. None — личная серия.

    🔴 None здесь ОБЫЧНОЕ ДЕЛО: сегодня все встречи владельца — личные серии, и
    ветка «наряда нет» обязана работать ровно как до Ф10c. Ни алерта, ни
    предупреждения тут быть не должно.
    """
    if not isinstance(meta, dict):
        return None
    try:
        from notary.lib import orders as orders_lib  # noqa: PLC0415
        from notary.lib import portal_delivery  # noqa: PLC0415

        if not portal_delivery.is_enabled():
            return None
        return orders_lib.find_order_for_recording(
            room_url=meta.get("meetingUrl"),
            started_at=meta.get("startTs"),
            session_uid=sid,
        )
    except Exception as e:  # noqa: BLE001 — реестр не читается → работаем как без нарядов
        logger.warning("Наряд для записи не резолвится (%s)", type(e).__name__)
        return None


def _portal_note_recording(sid: str, meta: dict | None) -> None:
    """Ф10c: встреча порталовая → удерживаем её исходники ДО подтверждения.

    Удержание ставится РАНЬШЕ финализации, а не после доставки. Причина
    буквальная: первым исходники удаляет не портал и не Telegram, а сам
    конвейер — `finalize` сносит копии из `_failed/` сразу после успешного
    разбора, а этот же collector сносит WAV сразу после Telegram. Поставь
    удержание позже — удерживать будет нечего.
    """
    try:
        from notary.lib import portal_delivery  # noqa: PLC0415

        order = portal_delivery.hold_if_portal(
            sid,
            room_url=(meta or {}).get("meetingUrl"),
            started_at=(meta or {}).get("startTs"),
        )
        if order is None:
            return
        portal_delivery.sync_wav_marker(((meta or {}).get("files") or {}).get("wav"), sid)
    except Exception as e:  # noqa: BLE001
        logger.warning("Удержание исходников не поставлено (%s)", type(e).__name__)


def _telegram_delivery_note(
    order: dict | None,
    delivery_status: str | None,
    delivery: dict | None = None,
) -> dict:
    """Ф10d: что сказать порталу про доставку в Telegram — статус И маршрут.

    Без маршрута панель «Доставка» может показать только «пропущено», и человек
    не поймёт, это поломка или так задумано. С маршрутом она говорит «ушло в
    группу серии» / «только на портал: телеграм сотрудника не привязан».

    🔴 `mode` — ЭТО ФАКТ ОТПРАВКИ, А НЕ НАМЕРЕНИЕ НАРЯДА (ремонт 18.08.2026).
    Раньше здесь стоял только `delivery_route.resolve(order)` — то есть «куда
    ПОЛАГАЛОСЬ отправить». Отправлял при этом другой процесс (финализация), у
    которого в тот день были сломаны импорты: он не сумел вывести маршрут и увёл
    протокол в личку владельца бота по legacy-фолбэку. Порталу же уехало
    `status: sent, mode: group` — панель «Доставка» показала «ушло в группу», а
    в группе не было ничего. Рапорт, который не может отличиться от правды,
    хуже отсутствия рапорта: по нему закрыли бы разбор.

    Поэтому сверяем чат: `delivery["chat_id"]` — куда РЕАЛЬНО ушло (его вернула
    сама отправка), `route.chat_id` — куда собирались. Разошлись → говорим
    порталу правду о фактическом адресате и ставим `reason=delivered_elsewhere`,
    по которому портал печатает человеку строку «ушло не туда».
    """
    note: dict = {"status": delivery_status or "unknown"}
    try:
        from notary.lib import delivery_route  # noqa: PLC0415

        route = delivery_route.resolve(order)
        note["mode"] = route.mode
        note["reason"] = route.reason
        fact_chat = (delivery or {}).get("chat_id")
        if (
            delivery_status == "sent"
            and isinstance(fact_chat, int)
            and not isinstance(fact_chat, bool)
            and isinstance(route.chat_id, int)
            and fact_chat != route.chat_id
        ):
            # Знак chat_id у Telegram и есть тип чата: группы/супергруппы
            # отрицательные, личка — положительная. Это не эвристика «на глаз»,
            # а свойство протокола, и другого признака у нас в этой точке нет.
            note["mode"] = "group" if fact_chat < 0 else "dm"
            note["reason"] = "delivered_elsewhere"
            logger.error(
                "🔴 доставка ушла НЕ по наряду: отправлено в чат %s, наряд %s "
                "требовал чат %s. Порталу отправлен ФАКТ (%s), не намерение",
                fact_chat, (order or {}).get("id"), route.chat_id, note["mode"],
            )
    except Exception as e:  # noqa: BLE001 — маршрут опционален, статус важнее
        logger.warning("Маршрут доставки не резолвится (%s)", type(e).__name__)
    return note


def _portal_enqueue_result(
    session_uid: str,
    meta: dict,
    finalize_result: dict,
    *,
    transcript_path: Path | None,
    protocol_path: Path | None,
    delivery_status: str | None,
    delivery: dict | None = None,
) -> None:
    """Ф10c (R7.5): результат встречи → durable-очередь доставки в портал.

    Тело собирается ЗДЕСЬ, потому что только здесь одновременно известны все
    семь вещей, которые ждёт портал: протокол, версия, задачи, транскрипт,
    участники, факт доставки в Telegram и длительность.

    Сбой записи в очередь — это НЕ «ну и ладно»: результат в этот момент ещё
    нигде, кроме диска бота, и исходники сотрёт следующий тик. Поэтому при
    отказе очереди удержание остаётся стоять, а владельцу уходит алерт.
    """
    order = _portal_order_for(session_uid, meta)
    if order is None:
        return
    try:
        from notary.lib import portal_delivery, portal_ingest  # noqa: PLC0415

        order_id = str(order.get("id") or "")
        # Удержание могло не встать раньше (наряд появился уже после старта
        # обработки) — ставим до записи в очередь, а не после.
        portal_delivery.hold(session_uid, order_id=order_id)

        def _read(path: Path | None) -> str:
            if not path:
                return ""
            try:
                return Path(path).read_text(encoding="utf-8")
            except OSError:
                return ""

        payload = portal_ingest.build_payload(
            order_id=order_id,
            meeting_id=order.get("meeting_id"),
            version=1,
            company=order.get("company"),
            session_uid=session_uid,
            protocol_md=_read(protocol_path),
            transcript_text=_read(transcript_path),
            # 🔴 НЕ `finalize_result["tasks_extracted"]`. Там СЧЁТЧИКИ
            # (`{"ilia": 0, "others": 0, …}`, finalize-meeting.py:1397), а
            # портал ждёт список объектов `{title, assignee, due}` и на словарь
            # ответил бы 422 — то есть отказом НАВСЕГДА и алертом владельцу на
            # каждой порталовой встрече. Машиночитаемого списка задач у бота
            # сегодня нет вовсе: задачи живут разделом внутри протокола, и
            # разбирает их портал из `protocol_md`. Пустой список — честное
            # «структурированных задач не передаю», а не потеря данных.
            tasks=[],
            participants=(meta.get("participants") or []),
            expected_participants=(meta.get("expectedParticipants") or []),
            started_at=meta.get("startTs"),
            ended_at=meta.get("endTs"),
            duration_seconds=meta.get("durationS"),
            # Ф10d: панели «Доставка» мало слова «skipped» — ей нужно сказать
            # человеку ПОЧЕМУ. `mode`/`reason` отвечают на это машинно:
            # «только портал, потому что телеграм сотрудника не привязан».
            telegram_delivery=_telegram_delivery_note(order, delivery_status, delivery),
            order_status=order.get("status"),
        )
        portal_delivery.enqueue(
            order_id=order_id,
            meeting_id=order.get("meeting_id"),
            version=1,
            session_uid=session_uid,
            company=order.get("company"),
            payload=payload,
            sources={
                "wav": (meta.get("files") or {}).get("wav"),
                "transcript_dir": VPS_TRANSCRIPTS,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Результат встречи %s НЕ попал в очередь доставки (%s) — исходники "
            "удерживаются, доставку надо повторить руками", session_uid, type(e).__name__,
        )
        push(
            f"🔴 Результат встречи не попал в очередь доставки на портал "
            f"({type(e).__name__}). Запись и транскрипт НЕ удалены — ничего не потеряно, "
            f"но на портал результат сам не доедет.",
            dedupe_key=f"portal-enqueue-fail:{session_uid}",
        )


def _portal_report_purged(sid: str) -> None:
    """Ф10c: сказать порталу, что исходники этой встречи физически удалены.

    ACK доказывает, что портал ПРИНЯЛ результат, и ничего не говорит о том, что
    бот стёр WAV. Без отдельного отчёта панель «Запись» навсегда застревает на
    «ждём отчёта бота» (R7.9, R5.12). Отчёт идёт через ту же durable-очередь:
    портал может лежать и в эту минуту, а исходников уже нет — восстановить
    состояние «удалено» будет нечем.
    """
    try:
        from notary.lib import portal_delivery  # noqa: PLC0415

        portal_delivery.report_sources_purged(sid)
    except Exception as e:  # noqa: BLE001 — файл уже удалён, ронять тик нельзя
        logger.warning("Отчёт об уборке исходников не поставлен (%s)", type(e).__name__)


def _portal_delivery_tick() -> None:
    """Ф10c (R13.3): разобрать очередь доставки. Зовётся КАЖДЫЙ тик.

    Именно это делает доставку живучей к рестарту: состояние лежит на диске, и
    любой следующий тик — хоть после перезапуска службы, хоть после перезагрузки
    машины — продолжает ровно с того места. Ничего «в памяти процесса» здесь нет
    by design.
    """
    try:
        from notary.lib import portal_delivery, portal_ingest  # noqa: PLC0415

        if not portal_delivery.is_enabled():
            return
        stats = portal_ingest.drain()
        if any(stats.values()):
            logger.info("Очередь доставки в портал: %s", stats)
    except Exception as e:  # noqa: BLE001 — доставка не имеет права ронять сбор встреч
        logger.warning("Тик доставки в портал упал (%s) — записи не тронуты", type(e).__name__)


def _cleanup_delivered_under_lock(meta: dict, sid: str) -> None:
    """Уборка записи доставленной встречи — под тем же замком, что финализация.

    Цикл5 фазы 5 ISS-60: повторный прогон восстановления из присланной записи
    (`tools/ingest-recording.py --force`, путь Б) кладёт в очередь СВЕЖУЮ копию
    записи рядом с прежней отметкой `delivered` и держит замок встречи, пока идёт
    расшифровка. Без замка тик сборщика видел «уже доставлено» и стирал запись
    посреди работы. Замок занят — уборка ждёт следующего тика."""
    with _session_finalize_lock(sid) as locked:
        if not locked:
            logger.info("Уборка %s отложена — встречу сейчас финализируют (замок занят)", sid)
            return
        _maybe_cleanup_wav_after_delivered(meta, sid)


def _maybe_cleanup_wav_after_delivered(meta: dict, sid: str) -> None:
    """Удаляет WAV, если delivery УЖЕ подтверждена И WAV ещё на диске.

    Применяется в двух кейсах:
      1. Тик после доставки: idempotent-guard срабатывает раньше, чем мы
         успели чистить WAV (например, finalize упал в момент очистки).
      2. Перепрогон с другого хоста: meta.delivered есть, но WAV не убрался.

    keepAudio / keep_audio в meta — пропускаем cleanup (как и раньше).

    Ф10c: третье условие — портал подтвердил приём результата (или встреча
    вообще не порталовая). Доставка в Telegram больше не даёт права стирать
    исходники в одиночку.
    """
    if meta.get("keepAudio") or meta.get("keep_audio"):
        logger.info("WAV cleanup skip (sid=%s) — keep_audio=true", sid)
        return
    if _portal_sources_held(sid, meta):
        logger.info(
            "WAV сохранён (sid=%s): результат ещё не подтверждён порталом (R13.4)", sid,
        )
        return
    # Фаза 4 (ISS-60): спутник записи (второе ухо / заглушённая основная) — по тем же
    # гейтам, что основная запись, и даже если основную уже убрали прошлым тиком
    # (иначе он остался бы навсегда).
    _cleanup_second_ear(meta, sid)
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
        _portal_report_purged(sid)
    except OSError as e:
        logger.warning("Не смог удалить WAV %s: %s", wav_path, e)


def _cleanup_second_ear(meta: dict | None, sid: str) -> None:
    """Фаза 4 (ISS-60): удалить спутников записи встречи (`second_ear.companion_wavs`).

    Спутник — второе ухо (`<дата>-<сессия>.chunk0.wav`), пока протокол из основной;
    а когда протокол собран из второго уха (`files.wav` = оно, его удаляет основной
    код) — кусок основной, который до фазы 4 был `files.wav` и удалялся здесь всегда.
    Подчиняется ТЕМ ЖЕ правилам хранения, что основная запись: зовётся только там,
    где сборщик удаляет `files.wav`, и только после тех же гейтов — доставка
    подтверждена, keep_audio нет, портал не держит исходники. Путь — из meta бота,
    со строгой проверкой имени. Только на сервере (LOCAL_FINALIZE): с мака по ssh
    не удаляется никогда.
    """
    if not LOCAL_FINALIZE or not isinstance(meta, dict):
        return
    try:
        from notary.lib import second_ear  # noqa: PLC0415

        second_ear.cleanup_after_delivery(meta, sid, host_path=Path)
    except Exception as e:  # noqa: BLE001 — уборка не должна ронять сбор встреч
        logger.warning("Второе ухо не убрано (sid=%s): %s", sid, type(e).__name__)


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


def _finalize_lock_path(session_uid: str) -> Path:
    """Путь per-session lock-файла (CG1). Имя санитизируется: session_uid
    приходит из meta-файла и теоретически может содержать `/`.

    Имя строит `lib/finalize_lock.lock_filename` — тот же, которым берёт замок
    ручная финализация из присланной записи (фаза 5 ISS-60): разойдутся имена —
    сборщик не увидит ручную финализацию и запустит вторую."""
    from notary.lib import finalize_lock  # noqa: PLC0415

    return STATE_DIR / finalize_lock.lock_filename(session_uid)


@contextlib.contextmanager
def _session_finalize_lock(session_uid: str):
    """CG1: неблокирующий flock на встречу. yield True — лок взят (можно
    финализировать), False — занят другим тиком (надо пропустить).

    Паттерн скопирован с `retry_failed._try_finalize` (тот же finalize-<sid>.lock
    по смыслу, но там per-VPS, тут per-mac у collector'а). Лок держится на время
    всего finalize (до 3ч ssh) — параллельный тик увидит занятость и пропустит,
    не плодя второй платный submit. Освобождается на закрытии fd (выход из with
    ИЛИ смерть процесса collector'а — ядро снимает flock автоматически)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _finalize_lock_path(session_uid)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (BlockingIOError, OSError):
            locked = False
        yield locked
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _finalize_and_collect(session_uid: str) -> None:
    """CG1-обёртка: берёт per-session lock и только под ним финализирует.

    Два пересёкшихся тика по одной встрече → второй видит занятый лок,
    пишет «уже идёт» и пропускает (один платный job вместо двух+)."""
    with _session_finalize_lock(session_uid) as locked:
        if not locked:
            logger.info(
                "Скип %s — finalize по этой встрече уже идёт (lock занят), "
                "жду следующий тик (cost-guard CG1)", session_uid,
            )
            return
        _do_finalize_and_collect(session_uid)


def _sync_sidecar_to_archive(
    src_dir: Path, dst_dir: Path, filename: str, session_uid: str, *, label: str,
) -> None:
    """Донести sidecar-файл `filename` (память серии / протокол-сводка) с рабочей
    стороны (output-dir finalize) в долговечный архив рядом с транскриптом.

    Тем же транспортом, что и протокол:
      • LOCAL_FINALIZE (коллектор на VPS): локальная copy2 `src_dir → dst_dir`;
      • мак (коллектор на маке): scp с VPS.

    Имя файла на обеих сторонах одинаковое (`filename`) — date-ключ совпадает с тем,
    что писал finalize (кладётся по date-ключу даже при коллизии имени транскрипта).

    Защитно к топологии: `src == dst` (output-dir == долговечный корень, архив не
    разведён с рабочей папкой) → no-op, как и копирование `.md`.

    Best-effort и ТИХО: sidecar — вторичный артефакт; нет файла-источника (артефакт
    выключен / пустой протокол) или сбой транспорта → INFO/WARN без алерта — доставка
    протокола уже состоялась. Содержимое файла НЕ читаем и НЕ логируем (опасная тройка).
    """
    dst = Path(dst_dir) / filename
    tmp = dst.with_suffix(dst.suffix + ".part")

    if LOCAL_FINALIZE:
        src = Path(src_dir) / filename
        if not src.exists():
            logger.info(
                "%s: файла-источника %s нет — синк не нужен (артефакт выключен / "
                "пустой протокол; sid=%s)", label, src.name, session_uid,
            )
            return
        try:
            if src.resolve() == dst.resolve():
                logger.info("✓ %s уже на месте (in-place от finalize): %s", label, dst)
                return
        except OSError:
            pass
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            logger.info("✓ %s (архив, local): %s", label, dst)
        except OSError as e:
            logger.warning(
                "%s: local copy %s → %s не удался (не критично): %s", label, src, dst, e,
            )
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
        return

    # Мак-путь: scp `filename` с VPS (рядом с транскриптом-источником на VPS).
    vps_src = f"{Path(src_dir).as_posix()}/{filename}"
    scp_cmd = ["scp", f"{SSH_HOST}:{vps_src}", str(tmp)]
    try:
        scp = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        logger.warning(
            "%s: scp timeout (sid=%s) — пропуск, протокол уже доставлен", label, session_uid,
        )
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        return
    if scp.returncode != 0:
        # Норма, когда артефакта нет (выключен / пустой протокол): файла нет → rc != 0.
        logger.info(
            "%s: нет на VPS или scp rc=%d (sid=%s) — синк пропущен (не критично)",
            label, scp.returncode, session_uid,
        )
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dst)
        logger.info("✓ %s (архив, scp): %s", label, dst)
    except OSError as e:
        logger.warning(
            "%s: os.replace %s → %s не удался (не критично): %s", label, tmp, dst, e,
        )
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _sync_series_memory(finalize_protocol_path: str, target_md: Path, session_uid: str) -> None:
    """Ф4 (task 5): доставить выжимку памяти серии на долговечную сторону рядом с протоколом.

    finalize (`series_memory.save_meeting_digest`) кладёт `<date>-memory.json` рядом с
    протоколом в свой output-dir. Долговечный архив (MEETINGS_DIR) до Ф4 получал ТОЛЬКО
    `.md` — память серии на бою не доезжала в архив владельца (наследие Ф2 §5). Имя файла
    памяти выводим из имени транскрипта-источника (`<date>.md` → `<date>-memory.json`, см.
    `series_memory.MEMORY_FILE_SUFFIX`) — date-ключ совпадает с тем, что писал finalize.
    Кладём по date-ключу даже при коллизии имени транскрипта (`<date>-<sid>.md`): одна
    выжимка на дату серии, перефинализация перетирает свою же выжимку.
    """
    src_protocol = Path(finalize_protocol_path)
    mem_name = src_protocol.stem + series_memory.MEMORY_FILE_SUFFIX  # `<date>.md` → `<date>-memory.json`
    _sync_sidecar_to_archive(
        src_protocol.parent, target_md.parent, mem_name, session_uid, label="Память серии",
    )


def _sync_protocol_summary(
    finalize_protocol_path: str, target_md: Path, date_str: str, session_uid: str,
) -> None:
    """ISS-32 (Ф3): доставить LLM-протокол `<date>-protokol.md` в долговечный архив.

    finalize пишет протокол-сводку рядом с транскриптом (`finalize-meeting.py:1283`,
    `<date>-protokol.md` по date-ключу), а collector до Ф3 копировал в архив ТОЛЬКО
    транскрипт (`<date>.md`) + память — сама сводка (то, что читает владелец) в архив
    не доезжала. Имя — по date-ключу (как пишет finalize для series И one-off), а не
    из stem транскрипта (у one-off транскрипт `<date>-<sid>.md`, а протокол всё равно
    `<date>-protokol.md`). Best-effort: нет файла (генерация выключена/упала) → no-op.
    """
    proto_name = f"{date_str}-protokol.md"
    _sync_sidecar_to_archive(
        Path(finalize_protocol_path).parent, target_md.parent, proto_name, session_uid,
        label="Протокол-сводка",
    )


def _do_finalize_and_collect(session_uid: str) -> None:
    """Запустить finalize-meeting.py на VPS для sessionUid, потом scp .md на мак."""
    # R6 (ISS-28): персист логов бот-контейнера. Делаем ПЕРЕД finalize — контейнер
    # уже вышел (collector гейтит finalize по живому контейнеру в main), а weekly
    # cleanup его снимет; без дампа причина прогона теряется. До finalize, а не
    # после — тогда persist отрабатывает независимо от исхода finalize (вкл.
    # rc!=0/timeout, где логи особенно нужны). Series берём из meta. Best-effort.
    try:
        _m = _read_meta_obj(session_uid) or {}
        _cont = _latest_exited_notarius_for_series(_m.get("series"))
        if _cont:
            _persist_container_log(_cont)
    except Exception as e:  # noqa: BLE001
        logger.warning("R6 persist (finalize-path) не удался (не критично): %s", e)
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
    # rc=5 «kill-switch активен» (CG7): встреча отложена в retry-очередь, НЕ сбой.
    # Не алертим (об этом уже сказал finalize + дайджест) и не копируем — протокола
    # ещё нет. finalize сам пушнул владельцу «на паузе».
    if proc.returncode == 5:
        logger.info(
            "finalize rc=5 «kill-switch активен» для %s — встреча отложена в retry, "
            "ждёт ручного снятия флага. Не сбой, алерт не шлём.", session_uid,
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

    # ISS-31/Ф1: rc=0 НЕ значит «всё гладко». finalize мог собрать протокол со
    # второй попытки / на резервной модели / с warning-ами генерации — раньше эти
    # строки stderr выбрасывались молча (читались только в ветке rc≠0 выше).
    # Логируем их в общий collector.log, чтобы причина «тихой» деградации не
    # терялась. Сам per-meeting лог причин пишет finalize на VPS (lib/run_log.py).
    warn_lines = _finalize_stderr_warnings(proc.stderr or "")
    if warn_lines:
        logger.warning(
            "finalize %s rc=0, но stderr нёс %d warning-строк(и):\n%s",
            session_uid, len(warn_lines), "\n".join(warn_lines),
        )

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
                # ISS-32: src==dst → архив НЕ разведён с рабочей (ARCHIVE_DIR пуст/
                # ==PROTOCOLS_DIR). Копировать нечего, finalize уже на месте.
                logger.info("✓ Протокол (архив == рабочая, in-place): %s", target_md)
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
                logger.info("✓ Протокол (архив, local): %s", target_md)
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
        logger.info("✓ Протокол (архив, scp): %s", target_md)

    # 2.5. Синк sidecar-артефактов рядом с транскриптом на долговечную сторону.
    # finalize пишет их в свой output-dir; collector раньше тащил в архив только
    # транскрипт `.md`. Best-effort внутри — сбой синка не влияет на доставку и не
    # алертит владельца. Гейт по `md_placed` + известному источнику.
    if md_placed and finalize_protocol_path:
        # ISS-32 (Ф3): протокол-сводка `<date>-protokol.md` — то, что читает владелец;
        # до Ф3 в архив не доезжала. Пишется и у series, и у one-off (генерация не
        # завязана на серию) → синкаем без гейта series_from_finalize.
        _sync_protocol_summary(finalize_protocol_path, target_md, date_str, session_uid)
        # Ф4 (task 5): память серии `<date>-memory.json` — только у series-встреч.
        if series_from_finalize:
            _sync_series_memory(finalize_protocol_path, target_md, session_uid)

    # 2.6. Ф10c (R7.5/R13.3): результат — в durable-очередь доставки в портал.
    # СТРОГО перед блоком чистки ниже: удаление исходников гейтится удержанием,
    # которое ставит именно эта запись в очередь. Порядок наоборот означал бы
    # окно, в котором WAV уже стёрт, а результат ещё нигде.
    if md_placed:
        _portal_enqueue_result(
            session_uid,
            _read_meta_obj(session_uid) or {},
            finalize_result,
            transcript_path=target_md,
            protocol_path=(
                Path(finalize_protocol_path).parent / f"{date_str}-protokol.md"
                if finalize_protocol_path else None
            ),
            delivery_status=delivery_status,
            delivery=delivery if isinstance(delivery, dict) else None,
        )

    # 3. WAV cleanup (Ф1-доработки 29.05). Удаляем WAV ТОЛЬКО когда оба
    # условия выполнены:
    #   (а) `.md` лежит в MEETINGS_DIR (`md_placed=True`);
    #   (б) доставка в TG подтверждена (`delivery.status in {sent, skipped}`).
    # «skipped» здесь = idempotent skip (уже доставлено в этот chat_id),
    # это легитимный успех. Для статусов «asked»/«error»/«disabled» WAV
    # остаётся — даст возможность безболезненно перепрогнать finalize после
    # ответа Ильи / починки токена / включения флага.
    # Ф10d: `portal-only` — ЗАКОНЧЕННАЯ доставка, а не сбой. У встречи с
    # маршрутом «только портал» телеграмного адресата нет by design, и
    # держать из-за этого WAV «на шанс перепрогона» означало бы копить записи
    # вечно (свободного места на машине 25 ГБ из 75). Исходники всё равно
    # защищены удержанием до ACK портала — проверка ниже.
    success_statuses = {"sent", "skipped", "portal-only"}
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
    # Ф10c (R13.4): доставка в Telegram больше не единственный получатель. Пока
    # портал не подтвердил приём — исходники не трогаем.
    if _portal_sources_held(session_uid, meta_obj):
        logger.info(
            "WAV сохранён (sid=%s): результат ещё не подтверждён порталом (R13.4)",
            session_uid,
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
            _portal_report_purged(session_uid)
        except OSError as e:
            logger.warning("Не смог удалить WAV %s: %s", wav_path, e)
    # Фаза 4 (ISS-60): спутник записи (второе ухо / заглушённая основная) — те же
    # гейты, та же минута, что у основной записи.
    _cleanup_second_ear(meta_obj, session_uid)


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


_RECOVERY_UID_DATE_RE = re.compile(r"^recovery-(?:.*-)?(\d{4}-\d{2}-\d{2})$")


def _date_from_filename_uid(filename_uid: str) -> str:
    """Имя meta-файла на диске: 2026-05-27-tm-<id>. Первые 10 символов = дата.

    Встреча, восстановленная из присланной записи (путь Б `tools/ingest-recording.py`,
    фаза 5 ISS-60), лежит в очереди как `recovery-<серия>-<дата>` — дата в КОНЦЕ.
    Без этой ветки получалось «сегодня», и сборщик, доводя такую встречу (сорвалась
    доставка или составление протокола), клал её расшифровку в архив и на мак под
    сегодняшним числом — занимая имя настоящей встречи серии за сегодня, — а
    протокол-сводку не находил вовсе."""
    if len(filename_uid) >= 10 and filename_uid[4] == "-" and filename_uid[7] == "-":
        return filename_uid[:10]
    m = _RECOVERY_UID_DATE_RE.match(filename_uid or "")
    if m:
        try:
            datetime.strptime(m.group(1), "%Y-%m-%d")
            return m.group(1)
        except ValueError:
            pass
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
