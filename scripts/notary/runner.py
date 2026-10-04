#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""runner.py — за 5 минут до встречи запускает Vexa-бот на VPS.

Запускается на VPS systemd-таймером `meeting-notary-runner.timer` раз в минуту
(launchd-агент на маке — архивный путь отката).

Алгоритм одного тика:
  1. Прочитать .queue.json (от scheduler-а) и сверить с реестром нарядов портала:
     добрать то, что очередь ещё не знает (разовые «зайти сейчас», плановые наряды
     со стартом в окне), и отбросить строки снятых и перенесённых нарядов.
  2. Прочитать .pause-until (через registry.pause_until() — она сама чистит stale).
  3. Найти кандидатов: start_at − now ∈ [−1; 5] мин (позже — только ждущие слот,
     разовые и поставленные у самого старта), ни ключ старта, ни ключ встречи
     (комната + секунда) не заняты в state-файле.
  4. Если есть активная Vexa-сессия (определяем через ssh + docker ps) — пропускаем.
  5. Конфликт нескольких на одно время → берём первого по порядку в .queue.json
     (а очередь scheduler пишет в порядке появления в watched.yaml), остальным
     шлём push «не записал, конфликт с …».
  6. Атомарно занимаем state-файл (state.state_acquire).
  7. Дёргаем `ssh meeting-notary docker run ...` в background.
  8. Если запись one-off — после успешного старта помечаем enabled: false.

Не блокируется на длинном docker-run (`docker run -d`), runner возвращается
быстро — это важно, иначе таймер будет копить запуски каждую минуту.
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
    get_telegram_chat_id_for_series,
    load_watched,
    pause_until,
    release_watched_lock,
    save_watched,
)
from notary.lib.notify import push  # noqa: E402
from notary.lib import late_join, portal_queue, slots, waitlist  # noqa: E402
from notary.lib import join_watch  # noqa: E402
from notary.lib import telemost_url  # noqa: E402
from notary.lib.meeting_identity import instant_key  # noqa: E402
from notary.lib.state import (  # noqa: E402
    normalized_start_key,
    read_queue,
    read_state,
    start_key,
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
# Ф10b: ключ комнаты (хэш, не ссылка — в метке контейнера приглашению не место).
# Нужен, чтобы «зайти сейчас» в комнату, где уже идёт запись другой серии, не
# приводило второго бота на тот же разговор.
ROOM_LABEL = "meeting-notary.room"


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

# Ф10b: то же число, но НА МОМЕНТ ВЫЗОВА и из одного места на весь проект
# (`lib/slots.py`). Константа выше вычисляется на импорте и остаётся для
# обратной совместимости; шлюз считает свободные слоты той же функцией, иначе
# портал обещал бы слот, которого у runner'а нет.
def max_concurrent_bots() -> int:
    return slots.max_concurrent_bots()


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


def main(now: datetime | None = None) -> int:
    # `now` параметром — чтобы тик можно было прогнать в пробе по минутам
    # (перенос встречи, освобождение слота). На бою вызывается без аргумента.
    setup_logging()
    now = now or datetime.now(timezone.utc)
    pu = pause_until()
    # ISS-60: сторож входа — ДО всего остального и под try. Упадёт — боты
    # запускаются как раньше. На паузе повторов не делает, только сообщает.
    _join_watch_tick(now, allow_retry=not pu)
    if pu:
        logger.debug("Пауза до %s — тик пропущен", pu)
        return 0

    queue = read_queue()
    # Ф10b: разовые наряды «зайти сейчас» читаются НАПРЯМУЮ из реестра — очередь
    # перестраивается раз в 3 минуты, а кнопка обязана поднимать бота на ближайшем
    # тике runner'а.
    # При `ENABLE_PORTAL_ORDERS=OFF` возвращает очередь как есть, диска не касаясь.
    queue = _with_immediate_orders(queue, now)
    # Бэклог 04.10.2026: так же — плановые наряды портала у самого старта. Встреча,
    # поставленная или перенесённая за минуту-две до начала, иначе попадала в
    # очередь уже после окна старта, и бот её пропускал. Тем же чтением реестра
    # строки снятых и перенесённых нарядов выбывают из очереди тика. При OFF — как выше.
    queue, late_keys = _with_near_scheduled_orders(queue, now)
    # ISS-60, фаза 2: пока самопроверка красная — предупредить владельца за ~10
    # минут до встречи. Под try: упадёт — запуск ботов идёт как раньше.
    _canary_warn_tick(queue, now)

    # Ф10b: очередь ожидания. Старт, пропущенный по занятости или лимиту, не
    # выпадает из окна навсегда — он ждёт свободного слота до конца встречи.
    # Этим же закрывается ISS-57: перенос даёт НОВЫЙ ключ старта, тот упирается в
    # живого бота той же серии, ложится в ожидание и исполняется, как только
    # старый бот вышел.
    #
    # 🔴 Разбор ожидания идёт ДО выхода по пустой очереди. Иначе отмена
    # ЕДИНСТВЕННОЙ встречи оставляла бы ждущий старт в файле навсегда — и через
    # час владелец получал бы «не записал» про встречу, которую сам же отменил.
    wl, wl_changed, waiting_keys = _waitlist_tick(queue, now)

    if not queue:
        logger.info("tick: очередь пуста")
        if wl_changed:
            _waitlist_save(wl)
        return 0

    candidates = _candidates(queue, now, waiting=waiting_keys, late_ok=late_keys)
    if not candidates:
        logger.info("tick: %d в очереди, ни одного в окне старта", len(queue))
        if wl_changed:
            _waitlist_save(wl)
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
    limit = max_concurrent_bots()

    for cand in candidates:
        series = str(cand.get("series") or cand["meeting_id"])
        start_at = cand.get("start_at")
        skey = start_key(cand)
        if _room_busy(cand, snapshot):
            # «Зайти сейчас» в комнату, где уже пишет бот: не второй бот, а
            # сообщение о том, что запись уже идёт. В ожидание НЕ ставим —
            # ждать тут нечего, разговор уже записывается.
            logger.info(
                "Разовый наряд %s не поднимаю: в этой комнате уже идёт запись",
                cand.get("order_id") or cand.get("meeting_id"),
            )
            push(
                f"Бот в этой комнате уже пишет — второго не поднимаю "
                f"(«{series}», запрос {start_at}).",
                dedupe_key=f"join-now-busy:{skey}",
            )
            continue
        action, reason = _concurrency_decision(
            series, snapshot, launched_series, running_count, limit,
        )
        if action != "launch":
            # REQ 3.3 — пропуск старта с ПРИЧИНОЙ. dedupe_key по событию (а не по
            # тексту): повтор того же пропуска в окне старта (runner раз в минуту)
            # глушится на 6ч, но пропуск ДРУГОЙ встречи всегда доходит. State НЕ
            # занимаем — если лимит освободится в окне старта, следующий тик
            # попробует снова.
            #
            # Ф10b: и не только «в окне старта». Пропущенный старт кладётся в
            # очередь ожидания до конца встречи; освободился слот — бот заходит с
            # опозданием, и опоздание подписывается в протоколе.
            if waitlist.is_enabled():
                wl_changed = waitlist.defer(wl, skey, cand, reason=action, now=now) or wl_changed
            waiting_entry = wl.get(skey) if waitlist.is_enabled() else None
            if waiting_entry:
                until = str(waiting_entry.get("deadline_at") or "")[11:16]
                text = (
                    f"Пока не записал «{series}» (start {start_at}): {reason}. "
                    f"Жду свободный слот до {until} — освободится, зайду с "
                    f"опозданием (это будет написано в протоколе)."
                )
            else:
                text = f"Не записал «{series}» (start {start_at}): {reason}."
            # 🔴 Ключ глушилки — по СТАРТУ, а не по событию: у перенесённой
            # встречи `event_id` тот же, и сообщение про НОВОЕ время утонуло бы в
            # шестичасовом окне, поставленном сообщением про старое (тот же класс
            # дефекта, что чинил ISS-57 в ключе идемпотентности).
            push(text, dedupe_key=f"start-skip:{action}:{skey}")
            logger.warning("Старт %s пропущен (%s): %s", cand.get("meeting_id"), action, reason)
            continue

        # State-файл идемпотентности: атомарный acquire ТОЛЬКО перед реальным
        # запуском (пропуски по занятости/лимиту state не занимают).
        #
        # 🔴 Ключ — `event_id` + МИНУТА старта (ISS-57): перенос встречи обязан
        # дать НОВЫЙ старт, а повторный тик по тому же старту — не дать. Голый
        # `event_id` проверяется как переходный щит выкатки (см. `state_acquire`).
        # Вторым ключом занимается сама встреча — комната + секунда старта
        # (`_meeting_key`): другая строка той же встречи второго бота не поднимет.
        if not state_acquire(
            skey, legacy_key=cand.get("event_id"), now=now, meeting_key=_meeting_key(cand),
        ):
            logger.info(
                "старт %s уже отработан недавно — пропуск (повторный тик, ручной run "
                "или другая строка той же встречи)", skey,
            )
            continue

        # seq = сколько уже запустили В ЭТОМ ТИКЕ → уникальный meeting_id на бота
        # (общий now не даёт коллизии redis-канала команд, У2 цикла Ф3).
        launched_ok = _launch_bot(cand, now, seq=len(launched_series))
        if launched_ok:
            _remember_portal_run(cand, now)
            _join_watch_register(cand, now, attempt=1)
            launched_series.add(series)
            running_count += 1  # учитываем в лимите для следующих кандидатов тика
            waited_reason = str((wl.get(skey) or {}).get("reason") or "")
            if not waited_reason and skey in late_keys:
                # Наряд поставили или перенесли перед самым началом — подпись в
                # протоколе говорит именно это, а не «встречу перенесли».
                waited_reason = late_join.REASON_LATE_ORDER
            wl_changed = waitlist.forget(wl, skey) or wl_changed
            _mark_if_late(cand, now, reason=waited_reason)
            if cand.get("type") == "one-off":
                _auto_disable_one_off(cand["meeting_id"])
        elif cand.get("type") == "one-off":
            logger.info(
                "auto-disable пропущен: one-off %s не стартовал успешно (rc!=0) — "
                "запись остаётся enabled. ПРЕДУПРЕЖДЕНИЕ: state-файл уже занял "
                "ЭТОТ СТАРТ (event_id + минута старта) на 12ч TTL — повторный "
                "запуск того же старта блокируется; перенос встречи даст новый ключ. "
                "Чтобы попробовать снова в окне старта: почисти `.state.json` руками.",
                cand["meeting_id"],
            )
    if wl_changed:
        _waitlist_save(wl)
    return 0


def _with_immediate_orders(
    queue: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Добавить в ПАМЯТИ тика разовые наряды «зайти сейчас» (Ф10b).

    Очередь на диске не трогаем: второй писатель `.queue.json` в проекте
    запрещён. Дубля не даёт ключ старта — он тот же, что у элемента очереди,
    который позже положит планировщик.
    """
    try:
        extra = portal_queue.due_immediate_items(now)
    except Exception as e:  # noqa: BLE001
        logger.warning("Разовые наряды не прочитаны (%s) — тик без них", type(e).__name__)
        return queue
    if not extra:
        return queue
    known = {start_key(it) for it in queue if isinstance(it, dict)}
    merged = list(queue)
    for item in extra:
        if start_key(item) in known:
            continue
        logger.info(
            "Разовый наряд «зайти сейчас» %s взят напрямую из реестра (start %s)",
            item.get("order_id"), item.get("start_at"),
        )
        merged.append(item)
    return merged


def _with_near_scheduled_orders(
    queue: list[dict[str, Any]], now: datetime
) -> tuple[list[dict[str, Any]], set[str]]:
    """Сверить очередь тика с реестром нарядов портала (бэклог 04.10.2026).

    ЗАЧЕМ. Плановый наряд runner видел только через очередь планировщика, а она
    перестраивается раз в 3 минуты. Встреча, поставленная или перенесённая на
    портале за минуту-две до начала, попадала в очередь уже после окна старта —
    и бот её пропускал. Теперь runner каждую минуту сам берёт из реестра плановые
    наряды со стартом в своём окне — так же, как разовые «зайти сейчас», — и по
    тому же чтению реестра отбрасывает устаревшие строки очереди (наряд снят,
    перенесён, сменил комнату): иначе по ним бот шёл бы до перестройки очереди.

    Возвращает `(очередь тика, ключи с правом позднего входа)`. Очередь на диске
    не трогаем (второй писатель `.queue.json` запрещён), память дублей
    планировщика не пишем. При OFF и при сбое чтения реестра нарядов — тик ровно
    такой, каким был без этой сверки.
    """
    if not portal_queue.is_enabled():
        return queue, set()
    try:
        orders = portal_queue.read_scheduled_orders()
        near = portal_queue.near_scheduled_items(
            now, ahead_minutes=START_WINDOW_MIN, orders=orders,
        )
        current = portal_queue.scheduled_items_by_order(orders)
    except Exception as e:  # noqa: BLE001 — реестр не прочитан: тик как без сверки
        logger.warning(
            "Реестр нарядов не прочитан (%s) — тик без сверки с ним", type(e).__name__,
        )
        return queue, set()
    try:
        return _merge_near_orders(queue, near, current, now)
    except Exception as e:  # noqa: BLE001 — сведение упало: тик как без сверки
        logger.warning(
            "Наряды портала не сведены с очередью (%s) — тик без сверки с реестром",
            type(e).__name__,
        )
        return queue, set()


def _outdated_portal_row(row: Any, current: dict[str, dict[str, Any]]) -> bool:
    """Строка наряда в очереди устарела против реестра (снят, перенесён, сменил комнату).

    Строку с `ical_uid` не трогаем: так планировщик помечает строку, в которую
    схлопнулась и встреча из календаря владельца, — она отвечает не только за
    наряд. Разовые «зайти сейчас» — у `_with_immediate_orders`.
    """
    if not isinstance(row, dict) or row.get("type") != portal_queue.QUEUE_TYPE:
        return False
    if row.get("immediate") or row.get("ical_uid") or not row.get("order_id"):
        return False
    live = current.get(str(row["order_id"]))
    if live is None:
        return True  # среди плановых нет: снят, «выйти сейчас», удалён или негоден
    return (
        normalized_start_key(row.get("start_at")) != normalized_start_key(live.get("start_at"))
        or row.get("url") != live.get("url")
    )


def _merge_near_orders(
    queue: list[dict[str, Any]],
    near: list[tuple[dict[str, Any], bool]],
    current: dict[str, dict[str, Any]],
    now: datetime,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Свести очередь тика с реестром нарядов. Второго бота не бывает.

    1. Реестр свежее очереди. Строка наряда, которого среди плановых в реестре уже
       нет (снят, «выйти сейчас», удалён) или который там в другое время или в
       другой комнате (перенесён), из очереди ТИКА выбывает: бот по ней не идёт,
       ожидание слота по ней снимается (`_outdated_portal_row`).
    2. Наряд у старта, которого очередь ещё не знает, добавляется, пройдя ТЕ ЖЕ
       защиты, что строка планировщика:
       * тот же ключ старта — элемент собран той же `portal_queue.queue_item`;
       * «нет» владельца по этой встрече (ISS-53) — `scheduler.drop_rejected_occurrences`.
         Реестр серий не прочитан — наряды у старта на этом тике не берём вовсе:
         без отсева бот пришёл бы на встречу, от которой владелец отказался;
       * память дублей (ISS-54): эту комнату в эту секунду уже вели под другим
         ключом — элемент наследует его (`scheduler.remembered_event_id`; писать
         память — дело планировщика);
       * та же встреча в другом облике уже стоит в очереди (личная серия владельца
         в той же комнате и секунде, другой наряд на ту же комнату) — элемент не
         добавляется (`meeting_identity.compare`, формула дедупа).
       Строка с тем же ключом старта уже в очереди — свой элемент не нужен, а
       право позднего входа переходит к ней.
    3. Дальше общий путь: состояние (ключ старта и ключ встречи «комната +
       секунда», `_meeting_key`), очередь ожидания, одна запись серии, потолок.
    """
    merged: list[dict[str, Any]] = []
    for row in queue:
        if _outdated_portal_row(row, current):
            logger.info(
                "Строка наряда %s (start %s) не запускается: в реестре его на этом "
                "старте уже нет — снят, перенесён или сменил комнату",
                row.get("order_id"), row.get("start_at"),
            )
            continue
        merged.append(row)
    if not near:
        return merged, set()

    from notary import scheduler as scheduler_mod  # noqa: PLC0415 — правила дедупа живут у планировщика
    from notary.lib.meeting_identity import compare, fingerprint  # noqa: PLC0415

    try:
        records = (load_watched() or {}).get("watched") or []
    except (Exception, SystemExit) as e:  # noqa: BLE001 — без отсева «нет» новых не берём
        logger.warning(
            "Реестр серий не прочитан (%s) — наряды у старта на этом тике не беру: "
            "«нет» владельца к ним не проверить", type(e).__name__,
        )
        return merged, set()
    memory = scheduler_mod.load_dedup_winners(now)
    late_keys: set[str] = set()

    for raw, late in sorted(near, key=lambda pair: str(pair[0].get("start_at") or "")):
        if not scheduler_mod.drop_rejected_occurrences([raw], records):
            continue
        item = dict(raw)
        inherited = scheduler_mod.remembered_event_id(item, memory, now)
        if inherited:
            item["event_id"] = inherited
        key = start_key(item)
        fp = fingerprint(item.get("start_at"), item.get("url"), item.get("ical_uid"))
        known = twin = None
        for other in merged:
            if not isinstance(other, dict):
                continue
            if start_key(other) == key:
                known = other
                break
            if twin is None and compare(
                fingerprint(other.get("start_at"), other.get("url"), other.get("ical_uid")), fp,
            )[0]:
                twin = other
        if known is not None:
            # Этот старт очередь уже знает: строка планировщика или строка той же
            # встречи, унаследовавшая ключ. Поздний вход положен и ей.
            if late:
                late_keys.add(key)
            continue
        if twin is not None:
            logger.info(
                "Наряд %s у старта не беру: эта встреча уже стоит в очереди как «%s»",
                item.get("order_id"), twin.get("meeting_id"),
            )
            continue
        merged.append(item)
        if late:
            late_keys.add(key)
        logger.info(
            "Плановый наряд %s взят напрямую из реестра (start %s%s)",
            item.get("order_id"), item.get("start_at"),
            ", поставлен у самого старта" if late else "",
        )
    return merged, late_keys


def _meeting_key(item: dict[str, Any]) -> str:
    """Ключ ВСТРЕЧИ для `.state.json`: комната + секунда старта (формула тождества ISS-54).

    Ключей старта (`event_id@минута`) у одной встречи бывает несколько: личная
    серия владельца и наряд портала в той же комнате, два наряда на одну комнату,
    наряд, перенесённый в слот уже запущенного, встреча, пересозданная на портале.
    Пока строку выбирал только планировщик (его память дублей), этого хватало; с
    нарядами у самого старта выбирает и runner, и следующий тик или прогон
    планировщика вправе взять другую строку той же встречи — с другим ключом
    старта. Ключ встречи у всех её строк один: второй запуск не пройдёт.

    Комната — хэш хоста и пути (`orders.room_key`, как у метки контейнера): сама
    ссылка в файл состояния не попадает. Посчитать нечем — пусто, барьера нет.
    """
    room = _room_label_value(item.get("url"))
    instant = instant_key(item.get("start_at"))
    return "room:%s@%s" % (room, instant) if room and instant else ""


def _waitlist_tick(
    queue: list[dict[str, Any]], now: datetime
) -> tuple[dict[str, Any], bool, set]:
    """Разобрать очередь ожидания на этом тике: (состояние, изменилось, ждущие).

    Fail-open: сбой очереди ожидания не имеет права остановить запуск ботов.
    """
    if not waitlist.is_enabled():
        return {}, False, set()
    try:
        wl = waitlist.load()
    except Exception as e:  # noqa: BLE001
        logger.warning("Очередь ожидания не прочитана (%s) — тик без неё", type(e).__name__)
        return {}, False, set()
    if not wl:
        return wl, False, set()
    changed = False
    queue_keys = {start_key(it) for it in queue if isinstance(it, dict)}
    waiting, expired, dropped = waitlist.plan(wl, queue_keys, now)
    for key, entry in expired:
        # Тихо забыть — значит соврать: владелец решил, что встречу записывают.
        push(
            "Не записал «%s» (start %s): свободного слота у бота так и не "
            "появилось до конца встречи." % (entry.get("series"), entry.get("start_at")),
            dedupe_key="waitlist-expired:%s" % key,
        )
        logger.warning(
            "Старт %s снят с ожидания: дедлайн %s прошёл, слот не освободился",
            key, entry.get("deadline_at"),
        )
        wl.pop(key, None)
        changed = True
    for key, entry in dropped:
        logger.info(
            "Старт %s снят с ожидания: его больше нет в очереди (отмена или перенос)",
            key,
        )
        wl.pop(key, None)
        changed = True
    return wl, changed, set(waiting)


def _waitlist_save(wl: dict[str, Any]) -> None:
    try:
        waitlist.save(wl)
    except Exception as e:  # noqa: BLE001
        logger.warning("Очередь ожидания не сохранена (%s)", type(e).__name__)


def _mark_if_late(item: dict[str, Any], now: datetime, *, reason: str) -> None:
    """Отметить поздний заход, чтобы он был ПОДПИСАН в протоколе (Ф10b).

    Опоздание бывает только у старта, который ждал слот, перезашёл после переноса
    или поставлен на портале у самого старта (`late_join.REASON_LATE_ORDER`): в
    обычном окне (5 мин до / 1 мин после) минут опоздания нет.
    """
    late = waitlist.late_minutes(item, now)
    if late <= 0:
        return
    series = str(item.get("series") or item.get("meeting_id") or "")
    late_join.mark(
        series,
        item.get("start_at"),
        joined_at=now,
        late_minutes=late,
        reason=reason or "late_start",
    )
    logger.warning(
        "Бот зашёл на «%s» с опозданием на %d мин (плановый старт %s, причина %s) — "
        "отметка уйдёт в протокол",
        series, late, item.get("start_at"), reason or "late_start",
    )


def _immediate_due(item: dict[str, Any], delta_min: float) -> bool:
    """Разовый наряд «зайти сейчас» годен к запуску шире обычного окна.

    Причина — цена тика планировщика: наряд появляется в `.queue.json` до 3
    минут спустя (в эпоху launchd — до 15) и в окно «1 минута после старта» мог
    не попасть. Runner теперь читает его прямо из реестра, окно оставлено запасом.
    """
    if not item.get("immediate"):
        return False
    return -portal_queue.IMMEDIATE_DUE_MINUTES <= delta_min <= START_WINDOW_MIN


def _late_order_due(skey: str, delta_min: float, late_ok: set) -> bool:
    """Плановый наряд, поставленный или перенесённый у самого старта, годен к позднему входу.

    Только такой: обычный старт, проехавший окно и никого не ждущий, по-прежнему
    не поднимается (бот пришёл бы к середине встречи без всякой причины).
    """
    return skey in late_ok and delta_min >= -portal_queue.LATE_ORDER_DUE_MINUTES


def _candidates(
    queue: list[dict[str, Any]],
    now: datetime,
    *,
    waiting: Any = (),
    late_ok: Any = (),
) -> list[dict[str, Any]]:
    state = read_state()
    waiting = set(waiting or ())
    late_ok = set(late_ok or ())
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
        skey = start_key(item)
        if delta_min < -STARTED_GRACE_MIN:
            # Ф10b: старт, который ЖДЁТ свободного слота, из окна не выпадает —
            # ради него весь механизм и заведён. Остальные опоздавшие — как было,
            # кроме наряда, поставленного или перенесённого у самого старта.
            if (
                skey not in waiting
                and not _immediate_due(item, delta_min)
                and not _late_order_due(skey, delta_min, late_ok)
            ):
                continue
        if skey in state or item.get("event_id") in state:
            # Если в state, но за более чем 12 часов до — state_acquire отпустит
            # (TTL); здесь мы только грубо отсеиваем, окончательное решение —
            # внутри state_acquire(). Но для лога сразу логирнем.
            # Голый `event_id` проверяется как переходный щит выкатки ISS-57:
            # записи старого формата живут в `.state.json` до 12 часов.
            logger.debug("старт %s уже есть в state — кандидатом не считаем", skey)
            continue
        mkey = _meeting_key(item)
        if mkey and mkey in state:
            # Эту встречу (та же комната и секунда старта) бот уже ведёт по другой
            # строке. Отсев здесь, а не только в state_acquire: иначе строка ушла бы
            # в ожидание слота, и владелец получил бы ложное «пока не записал».
            logger.info(
                "старт %s: эту встречу бот уже ведёт по другой строке (та же комната "
                "и секунда старта) — второго не запускаю", skey,
            )
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
    fmt = ('{{.Names}}\t{{.Label "' + SERIES_LABEL + '"}}'
           '\t{{.Label "' + ROOM_LABEL + '"}}')
    by_series: dict[str, list[str]] = {}
    by_room: dict[str, list[str]] = {}
    unlabeled: list[str] = []
    snap: dict[str, Any] = {
        "by_series": by_series, "by_room": by_room, "unlabeled": unlabeled, "total": 0,
    }
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
        parts = line.split("\t")
        name = parts[0].strip() if parts else ""
        series = parts[1].strip() if len(parts) > 1 else ""
        room = parts[2].strip() if len(parts) > 2 else ""
        if not name:
            continue
        total += 1
        if series:
            by_series.setdefault(series, []).append(name)
        else:
            unlabeled.append(name)
        # Ф10b: комната отдельным ключом — «зайти сейчас» в комнату, где уже
        # пишет бот ДРУГОЙ серии, не имеет права привести второго. Legacy-
        # контейнеры (до Ф10b) метки не несут и сюда просто не попадают.
        if room:
            by_room.setdefault(room, []).append(name)
    snap["total"] = total
    return snap


def _room_label_value(url: Any) -> str:
    """Ключ комнаты для метки контейнера. Пусто — если посчитать нечем."""
    try:
        from notary.lib.orders import room_key  # noqa: PLC0415

        return room_key(str(url or "")) if url else ""
    except Exception:  # noqa: BLE001 — метка вспомогательная, запуск важнее
        return ""


def _room_busy(item: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """В этой комнате уже пишет бот (любой серии)?

    Спрашивается ТОЛЬКО про разовое «зайти сейчас». Для расписания это было бы
    вредно: серия каждый день ходит в одну и ту же переговорку, и зависший бот
    вчерашней встречи блокировал бы сегодняшнюю. А вот кнопка «подключить
    сейчас», нажатая по комнате идущей встречи, — это ровно второй бот на один
    разговор.
    """
    if not item.get("immediate"):
        return False
    key = _room_label_value(item.get("url"))
    return bool(key) and bool((snapshot.get("by_room") or {}).get(key))


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
    # (например ручной smoke), не наш баг — молча игнор. С 25.09.2026 (фаза 3а,
    # entrypoint.sh: `exec node`) docker stop доходит до бота SIGTERM'ом, бот
    # выходит штатно с кодом 143 — это тот же внешний стоп.
    if status not in {"exited", "created", "restarting"}:
        return
    if status == "exited" and exit_code in ("137", "143"):
        logger.info("Container %s остановлен внешне (exit_code=%s) — не алертим", name, exit_code)
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


def _remember_portal_run(item: dict[str, Any], now: datetime) -> None:
    """Ф10c: пометить в наряде, что по нему поднялся бот.

    Без этой отметки результат встречи невозможно связать с нарядом надёжно:
    имя сессии у планировщика (`auto-…`) и у самого бота (`<дата>-tm-…`) —
    РАЗНЫЕ и ничем не связаны (`recording.ts:649`). Отметка — единственный
    точный ключ; всё остальное сопоставление идёт эвристикой «комната + время».

    Best-effort и только для нарядов: сбой реестра не имеет права уронить
    запуск бота, который уже состоялся. Личные серии владельца сюда не
    доходят (`order_id` у них нет) — их поведение не меняется ни на шаг.
    """
    order_id = item.get("order_id")
    if not order_id:
        return
    try:
        from notary.lib import orders as orders_lib  # noqa: PLC0415
        from notary.lib import portal_delivery  # noqa: PLC0415

        if not portal_delivery.is_enabled():
            return
        session_uid = _safe_session_uid(item["meeting_id"], item["start_at"])
        orders_lib.attach_run(str(order_id), session_uid=session_uid, launched_at=now)
    except Exception as e:  # noqa: BLE001 — бот уже запущен, ронять тик нельзя
        logger.warning(
            "Отметка исполнения наряда %s не записана (%s) — результат свяжется "
            "по комнате и времени", order_id, type(e).__name__,
        )


def _launch_bot(item: dict[str, Any], now: datetime, seq: int = 0, attempt: int = 1) -> bool:
    """Дёрнуть docker run на VPS. Не блокируется — запускаем в фоне.

    `seq` — порядковый номер запуска В ЭТОМ ТИКЕ (0-based). Нужен для
    уникальности `bot_config["meeting_id"]`: бот подписывается на redis-канал
    `bot_commands:meeting:<meeting_id>` (vexa-bot index.ts), а `now` у всех
    запусков одного тика общий → без `seq` два ПАРАЛЛЕЛЬНЫХ бота получили бы
    один meeting_id и один канал команд → stop/reconfigure одной встречи рвал бы
    запись другой (У2 цикла Ф3). seq ≤ MAX_CONCURRENT_BOTS ≪ 60с между тиками →
    коллизий между тиками нет.

    `attempt` — номер попытки входа (ISS-60): повтор идёт НОВЫМ контейнером с
    суффиксом `-a<N>` в имени сессии — упавший остаётся для разбора.

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
    session_uid = _session_uid_for(item, attempt)

    # С10 (25.09.2026): бот ходит только в Телемост — хост ссылки сверяется
    # ЦЕЛИКОМ, не подстрокой. Сюда сходятся все пути (календарь, реестр, наряды
    # портала, повтор сторожа), поэтому это последний рубеж перед браузером на
    # чужой странице (для личных серий ссылку может вписать внешний организатор).
    # Саму ссылку в сообщение не кладём.
    if not telemost_url.is_telemost_url(url, schemes=("http", "https")):
        push(
            f"Не запустил бот «{series}» (start {start_at}): ссылка встречи ведёт не в "
            f"Яндекс Телемост — бот открывает только его. Проверь ссылку в приглашении.",
            dedupe=False,
        )
        logger.error("Запуск %s отменён: ссылка встречи не на Телемост", item.get("meeting_id"))
        return False

    # Ф1 (ISS-28): chat_id группового чата серии в BOT_CONFIG — задел под Ф2
    # (уведомление «в момент детекта» R4b шлёт сам бот из recording.ts, ему нужен
    # chat_id, т.к. из finalize «в момент» физически нельзя — НЕС1 ревью). Резолв
    # тот же, что у доставки протокола: telegram_chat_id серии из watched.yaml
    # (отрицательный int = группа). Нет привязки → None, в Ф2 бот молча скипнет
    # групповое уведомление (личный алерт R4a всё равно уйдёт, A8). Best-effort:
    # сбой чтения реестра не должен ронять запуск бота.
    series_chat_id = None
    try:
        series_chat_id = get_telegram_chat_id_for_series(series, load_watched())
    except Exception as e:  # noqa: BLE001
        logger.warning("Не смог резолвить chat_id серии %s (не критично): %s", series, e)
    # Ф10b: у порталовой встречи серии в `watched.yaml` нет — чат приезжает с
    # нарядом. В `.queue.json` его НЕ дублируем: очередь это производный артефакт,
    # который переписывается раз в 3 минуты, и копия приватного идентификатора в
    # ней жила бы своей жизнью. Источник истины по доставке один — реестр нарядов.
    if series_chat_id is None and item.get("order_id"):
        try:
            from notary.lib import orders as orders_lib  # noqa: PLC0415

            order = orders_lib.get_order(str(item["order_id"]))
            chat = (order or {}).get("telegram_chat_id")
            if isinstance(chat, int) and not isinstance(chat, bool):
                series_chat_id = chat
        except Exception as e:  # noqa: BLE001
            logger.warning("Чат наряда не резолвится (не критично): %s", type(e).__name__)

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
        # meeting_id = unix-сек + seq: уникален среди ПАРАЛЛЕЛЬНЫХ ботов одного
        # тика (общий now), чтобы redis-канал команд не пересекался (У2 Ф3).
        "redisUrl": REDIS_URL,
        "meeting_id": int(now.timestamp()) + seq,
        # series + expectedParticipants — для маппинга имён в финализаторе.
        # camelCase ключи в bot_config, чтобы vexa-bot (TS) читал из BotConfig без конверсии.
        "series": series,
        "expectedParticipants": item.get("expected_participants") or [],
        # Ф1 (ISS-28): chat_id группового чата серии — задел под Ф2-уведомления
        # «в момент детекта» (recording.ts читает seriesChatId из BOT_CONFIG).
        # None не кладём — пусть отсутствует, чтобы не путать с «привязка = 0».
        **({"seriesChatId": series_chat_id} if series_chat_id is not None else {}),
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
    # Ф10b: ключ комнаты в метку (хэш хоста+пути, не сама ссылка). По нему
    # следующий тик видит «в этой комнате уже пишут» — иначе кнопка «подключить
    # сейчас» по комнате идущей встречи привела бы ВТОРОГО бота на тот же разговор.
    room_hash = _room_label_value(url)
    if room_hash:
        labels[ROOM_LABEL] = room_hash

    if LOCAL_DOCKER:
        label_args: list[str] = []
        for k, v in labels.items():
            label_args += ["--label", f"{k}={v}"]
        # 24.09.2026 (цикл5 фазы 1, С1): токен Телеграма боту в контейнер НЕ
        # передаётся намеренно — браузер внутри открывает чужие страницы. Свои
        # тревоги (зал ожидания, выброс, «оглох») бот пишет событием в статус
        # входа, а отправляет их сторож на хосте (lib/join_watch.py).
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

    attempt_suffix = f"-a{int(attempt)}" if attempt > 1 else ""
    log_file = RUNS_DIR / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{item['meeting_id']}{attempt_suffix}.log"
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


# ---------------------------------------------------------------------------
# ISS-60: сторож входа (lib/join_watch.py) — связка с запускателем
# ---------------------------------------------------------------------------

#: Каталог записей на хосте и статусов входа в нём (бот пишет в
#: /transcripts/.join-status/ — это та же папка через том контейнера).
TRANSCRIPTS_HOST_DIR = Path(TRANSCRIPTS_VOLUME.split(":", 1)[0])
JOIN_STATUS_HOST_DIR = TRANSCRIPTS_HOST_DIR / ".join-status"


def _session_uid_for(item: dict[str, Any], attempt: int = 1) -> str:
    base = _safe_session_uid(item["meeting_id"], item["start_at"])
    return base if attempt <= 1 else f"{base}-a{int(attempt)}"


def _docker_cmd(inner: list[str]) -> list[str]:
    if LOCAL_DOCKER:
        return inner
    return ["ssh", SSH_HOST, " ".join(shlex.quote(a) for a in inner)]


def _jw_inspect(name: str) -> "join_watch.ContainerState":
    if not name:
        return join_watch.ContainerState("missing")
    try:
        proc = subprocess.run(
            _docker_cmd(["docker", "inspect", name, "--format",
                         "{{.State.Status}}|{{.State.ExitCode}}"]),
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return join_watch.ContainerState("unknown")
    if proc.returncode != 0:
        err = (proc.stderr or "").lower()
        if "no such object" in err or "no such container" in err:
            return join_watch.ContainerState("missing")
        return join_watch.ContainerState("unknown")
    status, _, code = proc.stdout.strip().partition("|")
    try:
        exit_code: int | None = int(code)
    except ValueError:
        exit_code = None
    return join_watch.ContainerState(status or "unknown", exit_code)


def _jw_read_status(session_uid: str) -> dict[str, Any] | None:
    if LOCAL_DOCKER:
        return join_watch.read_status(JOIN_STATUS_HOST_DIR, session_uid)
    try:
        proc = subprocess.run(
            ["ssh", SSH_HOST, "cat " + shlex.quote(str(JOIN_STATUS_HOST_DIR / f"{session_uid}.json"))],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _jw_stop(name: str) -> None:
    subprocess.run(_docker_cmd(["docker", "stop", "-t", "20", name]),
                   capture_output=True, text=True, timeout=60)


def _jw_save_logs(name: str) -> None:
    """Логи сорвавшегося контейнера — в постоянный файл (контейнеры раз в неделю чистятся)."""
    if not name:
        return
    dest_dir = LOG_DIR / "bot-containers"
    dest_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(_docker_cmd(["docker", "logs", name]),
                          capture_output=True, text=True, timeout=60)
    (dest_dir / f"{name}.log").write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")


#: Номер запуска для повторов: у каждого бота свой `meeting_id` = свой
#: redis-канал команд. Основной цикл даёт seq 0..N-1 в пределах тика; повтор
#: идёт ДО него в том же тике и с тем же временем — без смещения совпал бы с
#: новым ботом (команда `leave` ушла бы обоим, цикл5 Н3).
_RETRY_SEQ_BASE = 100
_retry_seq_counter = 0


def _jw_plan_retry(entry: dict[str, Any], attempt: int) -> dict[str, str]:
    """Имя сессии и контейнера попытки N — без побочных эффектов."""
    item = dict(entry.get("item") or {})
    uid = _session_uid_for(item, attempt)
    return {"session_uid": uid, "container": f"vexa-notarius-{uid}"}


def _jw_relaunch(entry: dict[str, Any], attempt: int) -> bool:
    """Повторная попытка входа. Нет места под второго бота → False (сторож сообщит)."""
    global _retry_seq_counter
    item = dict(entry.get("item") or {})
    if not item.get("url") or not item.get("meeting_id") or not item.get("start_at"):
        return False
    snapshot = _running_bots_snapshot()
    if int(snapshot.get("total") or 0) >= max_concurrent_bots():
        logger.warning("Повтор входа %s: нет свободного слота", entry.get("series"))
        return False
    _retry_seq_counter += 1
    return _launch_bot(
        item, datetime.now(timezone.utc),
        seq=_RETRY_SEQ_BASE + _retry_seq_counter, attempt=attempt,
    )


def _jw_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = dict(entry.get("item") or {})
    payload.update({
        "series": entry.get("series"),
        "start_at": entry.get("start_at"),
        "order_id": entry.get("order_id"),
        "series_title": entry.get("series_title"),
        "container": entry.get("container"),
    })
    return payload


def _jw_alert(
    entry: dict[str, Any], reason: str, attempts: int, crash_kit: str | None, owner_note: str | None
) -> None:
    from notary.lib import join_alert  # noqa: PLC0415

    join_alert.send(_jw_payload(entry), reason, attempts=attempts, crash_kit=crash_kit, owner_note=owner_note)


def _jw_event_alert(entry: dict[str, Any], event_type: str, event: dict[str, Any]) -> None:
    """Событие встречи (выкинуло / оглох / ждёт в зале ожидания) — людям и владельцу."""
    from notary.lib import join_alert  # noqa: PLC0415

    join_alert.send(_jw_payload(entry), event_type, kind=event_type)


def _jw_gave_up(entry: dict[str, Any], reason: str) -> None:
    """Сторож сдался окончательно (фаза 5 ISS-60) — только для встреч портала.

    1. Порталу — событие «бот не пришёл» (`no_show`): встреча получает статус
       «бот не пришёл», организатору — колокольчик, на карточке открывается
       загрузка записи. Тем же каналом, что результаты (очередь на диске, её
       разбирает сборщик каждые 5 минут).
    2. Разовый наряд «Подключить сейчас» закрывается — иначе повторное нажатие
       кнопки до трёх часов отвечало бы «бот уже идёт» (хвост У9).

    У личных серий владельца наряда нет — ничего не делаем. Каждое действие под
    своим try: сбой портала не мешает закрыть наряд и наоборот.
    """
    item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
    order_id = str(entry.get("order_id") or item.get("order_id") or "").strip()
    if not order_id:
        return
    from notary.lib import orders as orders_lib  # noqa: PLC0415

    order: dict[str, Any] | None = None
    try:
        order = orders_lib.get_order(order_id)
    except Exception as e:  # noqa: BLE001 — реестр нарядов не читается
        logger.warning("Сторож сдался по наряду %s, реестр не прочитан (%s)", order_id, type(e).__name__)
    attempts = entry.get("attempts") if isinstance(entry.get("attempts"), list) else []
    last = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    session_uid = str(last.get("session_uid") or "")
    meeting_id = str((order or {}).get("meeting_id") or "").strip()
    if not meeting_id and order_id.startswith("portal:"):
        meeting_id = order_id[len("portal:"):]
    if (order or {}).get("status") == orders_lib.STATUS_CANCELLED:
        # Встречу сняли, пока бот пытался войти: «бот не пришёл» было бы ложным —
        # портал позвонил бы организатору про встречу, которую тот сам отменил.
        logger.info("Сторож сдался по наряду %s, но наряд уже снят — порталу не пишу", order_id)
    else:
        try:
            from notary.lib import portal_delivery  # noqa: PLC0415
            from notary.lib.state import normalized_start_key  # noqa: PLC0415

            key = portal_delivery.report_no_show(
                order_id, meeting_id=meeting_id, session_uid=session_uid,
                company=(order or {}).get("company") or item.get("company"),
                # Заход бота — минута его старта (как ключ журнала сторожа): срыв
                # следующего захода по той же встрече — новое событие порталу.
                round_mark=normalized_start_key(entry.get("start_at") or item.get("start_at")),
            )
            if key:
                logger.warning("Наряд %s: «бот не пришёл» в очереди к порталу (%s)", order_id, reason)
        except Exception as e:  # noqa: BLE001
            logger.warning("«Бот не пришёл» по наряду %s не поставлен в очередь (%s)", order_id, type(e).__name__)
    if item.get("immediate") or (order or {}).get("kind") == orders_lib.KIND_IMMEDIATE:
        try:
            closed = orders_lib.close_immediate_after_join_failure(order_id, reason=reason)
            if closed:
                logger.warning(
                    "Разовый наряд %s закрыт: бот не вошёл — кнопка «Подключить сейчас» "
                    "снова заводит новый наряд", order_id,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Разовый наряд %s не закрыт (%s)", order_id, type(e).__name__)


def _jw_joined_on_retry(entry: dict[str, Any], attempt: int, joined_at: datetime) -> None:
    """Вход подтверждён не с первой попытки — пометить опоздание для протокола.

    Повтор заходит на минуты позже первой попытки (сорванный вход, остановка
    зависшего бота, минута тика). Пришлось это на время после начала встречи
    (порог `waitlist.LATE_THRESHOLD_MINUTES`) — первых минут разговора в записи
    нет, и протокол обязан это сказать, как после захода из очереди ожидания
    (`late_join`, Ф10b: «протокол без начала выглядит как протокол встречи,
    которая так и началась»). Зашёл раньше начала — отметки нет.

    Сквозной цикл5 ISS-60: сторож (фаза 1) и подпись опоздания (Ф10b) не были
    связаны — протокол после удачного повтора молча терял начало встречи.
    """
    item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
    if not item.get("start_at"):
        return
    _mark_if_late(item, joined_at, reason=late_join.REASON_JOIN_RETRY)


def _jw_note_owner(text: str) -> None:
    push(text, dedupe=False, silent=True)


def _jw_persist_failed(text: str) -> None:
    push(text, dedupe_key="join-watch-ledger-write-failed")


def _join_watch_active() -> bool:
    """Сторож работает только там, где живут боты (VPS, docker локально).

    Режим «мак + ssh» — архивный путь отката (README, runbook): там сторож не
    нужен, а заодно тесты, которые гоняют `runner.main()` на маке, не ходят по
    ssh на боевой сервер.
    """
    return LOCAL_DOCKER and join_watch.is_enabled()


def _join_watch_register(item: dict[str, Any], now: datetime, *, attempt: int) -> None:
    """Занести старт в журнал сторожа. Best-effort: бот уже запущен."""
    if not _join_watch_active():
        return
    try:
        ledger = join_watch.load_ledger()
        uid = _session_uid_for(item, attempt)
        join_watch.register_launch(
            ledger, start_key(item), item,
            session_uid=uid, container=f"vexa-notarius-{uid}",
            attempt=attempt, now=now,
        )
        join_watch.save_ledger(ledger)
    except Exception as e:  # noqa: BLE001
        logger.warning("Сторож входа: старт не записан (%s)", type(e).__name__)


def _join_watch_tick(now: datetime, *, allow_retry: bool) -> None:
    """Проход сторожа входа. НИКОГДА не бросает — запуск ботов важнее."""
    if not _join_watch_active():
        return
    try:
        ledger = join_watch.load_ledger()
        if ledger:
            hooks = join_watch.Hooks(
                inspect=_jw_inspect,
                read_status=_jw_read_status,
                stop=_jw_stop,
                plan_retry=_jw_plan_retry,
                relaunch=_jw_relaunch,
                alert=_jw_alert,
                event_alert=_jw_event_alert,
                note_owner=_jw_note_owner,
                persist=join_watch.save_ledger,
                persist_failed=_jw_persist_failed,
                save_logs=_jw_save_logs,
                gave_up=_jw_gave_up,
                joined_on_retry=_jw_joined_on_retry,
            )
            if join_watch.tick(ledger, now=now, hooks=hooks, allow_retry=allow_retry):
                join_watch.save_ledger(ledger)
        if now.minute == 0:
            join_watch.prune_diagnostics(TRANSCRIPTS_HOST_DIR)
    except Exception as e:  # noqa: BLE001
        logger.warning("Сторож входа упал (%s) — запуск ботов продолжается", type(e).__name__)


def _canary_warn_tick(queue: list[dict[str, Any]], now: datetime) -> None:
    """ISS-60, фаза 2: самопроверка красная → предупредить владельца перед встречами.

    Только там, где живут боты (VPS); выключатель `MEETING_NOTARY_CANARY_WARN=0`.
    НИКОГДА не бросает — запуск ботов важнее.
    """
    if not LOCAL_DOCKER:
        return
    try:
        from notary.lib import canary  # noqa: PLC0415

        if not canary.warn_enabled():
            return
        canary.warn_tick(queue, now)
    except Exception as e:  # noqa: BLE001
        logger.warning("Предупреждение самопроверки упало (%s) — запуск ботов продолжается", type(e).__name__)


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
