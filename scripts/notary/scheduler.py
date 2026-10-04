#!/Users/ilarybalka/Projects/meeting-notary/.venv-cli/bin/python
"""scheduler.py — формирует очередь стартов на 48 часов вперёд.

Запускается на VPS systemd-таймером `meeting-notary-scheduler.timer` раз в 3 минуты
(180 с; launchd-агент на маке раз в 15 минут — архивный путь отката). Плановые
наряды портала со стартом в ближайшие минуты runner добирает из реестра сам, не
дожидаясь этого тика (`runner._with_near_scheduled_orders`).

Алгоритм:
  1. Прочитать ~/Projects/me/встречи/watched.yaml и rooms.yaml.
  2. Для каждой записи `enabled=true` и `ignored=false`:
     - google-calendar: запросить instances серии через Calendar API на 48ч.
     - manual: развернуть cron-правило в datetime'ы.
     - one-off: добавить напрямую если в окне.
  3. Для каждого экземпляра разрешить URL (явный room → conferenceData → push).
  4. Записать .queue.json (атомарно).
  5. Если был хоть один успешный календарный запрос (или календарных нет) —
     обновить .scheduler-last-success.
  6. Если .scheduler-last-success старее 1 часа — push в Telegram.

Стартует только сам себя как один процесс, читает реестры под flock через registry.
Все ошибки логируем в ~/Library/Logs/meeting-notary/scheduler.log.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.cli.registry import (  # noqa: E402
    DEFAULT_REGISTRY_DIR,
    load_rooms,
    load_watched,
    pause_until,
    resolve_room_to_url,
)
from notary.lib.cron_expand import expand_cron  # noqa: E402
from notary.lib.meeting_identity import (  # noqa: E402
    AUTO_RECORD_ID_RE,
    MATCH_NONE,
    compare,
    fingerprint,
    ical_key,
    is_owner_named,
    proof_ru,
)
from notary.lib.gcal import (  # noqa: E402
    GCalAPIError,
    GCalAuthError,
    Credentials,
    event_ical_uid,
    event_start_dt,
    extract_meeting_url,
    get_event,
    list_instances,
    load_credentials,
)
from notary.lib.notify import push  # noqa: E402
from notary.lib import portal_queue  # noqa: E402
from notary.lib.state import (  # noqa: E402
    read_last_success,
    write_last_success,
    write_queue,
)

LOG_DIR = Path(os.path.expanduser(
    os.environ.get("MEETING_NOTARY_LOG_DIR", "~/Library/Logs/meeting-notary")
))
LOG_FILE = LOG_DIR / "scheduler.log"

HORIZON_HOURS = 48
MCP_DEAD_THRESHOLD_HOURS = 1

# ISS-54: id, который бот сгенерировал сам (вечерний блок, `make_record_id`).
# При схлопывании дубля побеждает запись с человеческим именем: её вёл владелец,
# и на ней висят привязка чата, разметка company/visibility и режим открытых
# вопросов. Автоимя — расходный материал. Само правило живёт в
# `lib/meeting_identity` — ОДНО на планировщик и на реестр (apply-reply брал
# среди двух живых копий просто первую в файле и называл владельцу не ту).
AUTO_MEETING_ID_RE = AUTO_RECORD_ID_RE

# ISS-54: кого мы выбрали вести встречу в прошлые тики. Схлопывание жило в
# памяти одного прогона, и это давало ВТОРОГО бота на ровном месте: если в
# следующем тике победитель из очереди пропал (календарь его записи не ответил;
# победитель был разовой записью и погасил себя после старта), выживал
# проигравший — с другим `event_id`, а на `event_id` ключуются все барьеры ниже
# по течению (`runner._candidates`, `state_acquire`). Файл маленький, живёт
# рядом с `.queue.json`, чинится удалением.
# 🔴 Резолв ЛЕНИВЫЙ, на вызове (цикл5 05.08, Р-2 — тот же класс, что чинил f249582
# в lib/state.py, только в новом файле фичи). Константа, вычисленная на импорте, не
# видит подмену каталога реестра тестом и пишет в БОЕВОЙ реестр. Сегодня не стреляло
# только потому, что тест дедупа патчит константу вручную; следующий тест без патча
# записал бы в ~/Projects/me/встречи/.
DEDUP_WINNERS_FILE = DEFAULT_REGISTRY_DIR / ".dedup-winners.json"  # для обратной совместимости


def dedup_winners_file() -> Path:
    """Путь к памяти о победителях схлопывания НА МОМЕНТ ВЫЗОВА."""
    from notary.cli.registry import _resolve_registry_dir

    return _resolve_registry_dir() / ".dedup-winners.json"
# За сколько до старта выбор победителя перестаёт пересматриваться. Раньше
# этого момента бот стартовать не мог (runner берёт кандидатов за 5 минут до
# старта), поэтому смена победителя безопасна и даже полезна — вернулся
# календарь владельца, ведём по его записи. Позже — фиксируем: пересмотр в этот
# момент и означал бы второго бота.
WINNER_FREEZE_MINUTES = 30
# Сколько держим память после старта: разово-погашенный победитель должен ещё
# гаситься и в следующих тиках, но вечно копить ключи незачем.
WINNER_MEMORY_KEEP_HOURS = 6


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


logger = logging.getLogger("scheduler")


def main() -> int:
    setup_logging()
    started_at = datetime.now(timezone.utc)
    logger.info("=== scheduler run start === %s", started_at.isoformat())

    pu = pause_until()
    if pu:
        logger.info("Пауза активна до %s — пишу пустую очередь", pu)
        write_queue([], generated_at=started_at)
        return 0

    watched_data = load_watched()
    rooms_data = load_rooms()
    items_in_yaml = watched_data.get("watched", []) or []
    horizon_end = started_at + timedelta(hours=HORIZON_HOURS)

    queue: list[dict[str, Any]] = []
    gcal_attempted = False
    gcal_succeeded = False

    creds: Credentials | None = None
    creds_load_error: str | None = None
    try:
        creds = load_credentials()
    except GCalAuthError as e:
        creds_load_error = str(e)
        logger.warning("Не удалось загрузить OAuth credentials: %s", e)

    for rec in items_in_yaml:
        if not rec.get("enabled", True):
            continue
        if rec.get("ignored", False):
            continue
        tp = rec.get("type")
        try:
            if tp == "google-calendar":
                gcal_attempted = True
                if creds is None:
                    raise GCalAuthError(creds_load_error or "credentials unavailable")
                new_items, creds = _expand_google_calendar(
                    rec, rooms_data, creds, started_at, horizon_end
                )
                queue.extend(new_items)
                gcal_succeeded = True
            elif tp == "manual":
                queue.extend(_expand_manual(rec, rooms_data, started_at, horizon_end))
            elif tp == "one-off":
                queue.extend(_expand_one_off(rec, rooms_data, started_at, horizon_end))
            else:
                logger.warning("Запись %s: неизвестный type=%r — пропущена", rec.get("id"), tp)
        except GCalAuthError as e:
            logger.error("GCal auth error для %s: %s", rec.get("id"), e)
            # Не падаем — продолжаем с другими записями.
        except GCalAPIError as e:
            logger.error("GCal API error для %s: %s", rec.get("id"), e)
        except Exception as e:
            logger.exception("Не смог раскрыть запись %s: %s", rec.get("id"), e)

    # Ф10b: наряды портала — НАРАВНЕ с реестром серий, в ту же очередь, теми же
    # правилами дедупа. Отдельной очереди у портала нет by design: две правды о
    # том, кого записывать, означают двух ботов в комнате.
    #
    # 🔴 За выключателем `ENABLE_PORTAL_ORDERS` (дефолт OFF): при OFF
    # `items_for_queue` возвращает [] ДО обращения к диску, и очередь владельца
    # получается ровно такой же, какой была до этой фазы.
    #
    # Fail-open: сбой на стороне нарядов не имеет права унести личные серии.
    try:
        portal_items = portal_queue.items_for_queue(started_at, horizon_end)
    except Exception as e:  # noqa: BLE001
        logger.exception("Раскрытие нарядов портала упало (%s) — очередь без них", e)
        portal_items = []
    if portal_items:
        logger.info("Нарядов портала в очереди: %d", len(portal_items))
        queue.extend(portal_items)

    # Сортируем очередь по времени старта. Сортировка СТАБИЛЬНА: при равном
    # старте личная серия владельца остаётся раньше наряда портала, и при
    # схлопывании дубля (одна комната, одна секунда) побеждает она — у неё
    # привязка чата, разметка компании и режим открытых вопросов.
    queue.sort(key=lambda x: x.get("start_at", ""))

    # ISS-53: «нет» по встрече, которую приносит ЖИВАЯ запись серии из второго
    # календаря. Fail-open — сбой не имеет права унести очередь целиком.
    try:
        queue = drop_rejected_occurrences(queue, items_in_yaml)
    except Exception as e:  # noqa: BLE001
        logger.exception("Отсев отказных стартов упал (%s) — очередь как есть", e)

    # ISS-54: одно и то же событие, видное в двух календарях, приезжает сюда
    # двумя элементами с разными meeting_id/event_id — и на встречу приходят два
    # бота. Схлопываем ДО записи очереди: это последний рубеж, где виден весь
    # список (барьеры runner-а ключуют event_id/series — у дублей они разные).
    #
    # Fail-open. Дедуп — улучшение, а не условие работы: если он падает, очередь
    # уходит как есть. Два бота на одной встрече неприятны, «бот не пришёл НИ НА
    # ОДНУ встречу» — катастрофа, и цена ошибки тут несимметрична.
    memory = load_dedup_winners(started_at)
    try:
        queue = dedupe_queue(queue, memory=memory, now=started_at)
    except Exception as e:  # noqa: BLE001
        logger.exception("Дедуп очереди упал (%s) — пишу очередь как есть", e)
        logger.warning(
            "Схлопывание дублей не отработало: если событие видно в двух "
            "календарях, на встречу приедут два бота. Очередь не тронута."
        )
    else:
        try:
            save_dedup_winners(memory)
        except Exception as e:  # noqa: BLE001
            logger.warning("Не смог сохранить память о схлопывании (%s)", e)

    write_queue(queue, generated_at=started_at)
    logger.info("Очередь записана: %d стартов", len(queue))
    for it in queue[:10]:
        logger.info(
            "  • %s @ %s [%s] url=%s",
            it.get("meeting_id"),
            it.get("start_at"),
            it.get("type"),
            (it.get("url") or "")[:60],
        )

    # Лог-маркер успеха календарного скана.
    if (not gcal_attempted) or gcal_succeeded:
        write_last_success(started_at)
    else:
        # Все google-calendar запросы упали — алерт о смерти MCP/OAuth.
        last = read_last_success()
        if last is None:
            # никогда не было успеха — считаем от старта
            logger.warning("Календарь ни разу не отвечал успешно")
            push(
                "Календарь не отвечает: первый запуск scheduler-а упал. "
                "Нужен повторный логин Google (см. ~/.config/google-calendar-mcp/)."
            )
        else:
            age = started_at - last
            if age > timedelta(hours=MCP_DEAD_THRESHOLD_HOURS):
                logger.warning(
                    "Календарь молчит уже %.1fч — алерт", age.total_seconds() / 3600
                )
                push(
                    f"Календарь не отвечает уже {int(age.total_seconds()/3600)}ч. "
                    "Нужен повторный логин Google. Без него регулярные встречи "
                    "записываться не будут."
                )

    return 0


def _is_owner_named(item: dict[str, Any]) -> bool:
    return is_owner_named(item.get("meeting_id"))


# ISS-53 (шов Ф4↔Ф5): «нет» по конкретной встрече оставляет в реестре мёртвую
# запись-отказ с приметами (`created_by_rejection`). Планировщик мёртвые записи
# не раскрывает — и обычно этого хватает. Но если ту же встречу приносит ЖИВАЯ
# запись (та же встреча видна вторым календарём как экземпляр уже ведомой СЕРИИ),
# отказ оставался словами: бот приходил, хотя владельцу сказано «не записываю».
# Гасить саму серию нельзя — «нет» сказано про ОДНУ встречу, а не про серию,
# поэтому пропускаем ровно этот старт.
def _rejected_fingerprints(watched_records: list[dict[str, Any]]) -> list:
    out: list = []
    for rec in watched_records or []:
        if not isinstance(rec, dict):
            continue
        if not rec.get("created_by_rejection"):
            continue
        if rec.get("enabled", True) or rec.get("ignored", False):
            # Отказ сняли («да» после «нет») либо он дорос до «никогда» —
            # запись работает по общим правилам, старты гасить не наше дело.
            continue
        fp = fingerprint(rec.get("event_start_at"), rec.get("event_url"),
                         rec.get("ical_uid"))
        if fp.start and (fp.ical or fp.url):
            out.append((rec.get("id"), fp))
    return out


def drop_rejected_occurrences(
    queue: list[dict[str, Any]], watched_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Выкинуть из очереди старты, по которым владелец сказал «нет».

    Сравнение — общей формулой тождества (`compare`, БЕЗ `series_scope`: отказ
    всегда про один экземпляр, и общий `iCalUID` серии сам по себе старт не
    гасит — нужно совпадение секунды старта).

    Fail-open, как и весь дедуп очереди: любой сбой → очередь как есть. «Бот
    пришёл на встречу, от которой отказались» неприятно, «бот не пришёл ни на
    одну» — катастрофа.
    """
    rejected = _rejected_fingerprints(watched_records)
    if not rejected:
        return queue
    kept: list[dict[str, Any]] = []
    for item in queue:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        fp = fingerprint(item.get("start_at"), item.get("url"), item.get("ical_uid"))
        hit = None
        for rec_id, rfp in rejected:
            proof, _conflict = compare(rfp, fp)
            if proof:
                hit = (rec_id, proof)
                break
        if hit is None:
            kept.append(item)
            continue
        logger.info(
            "Старт «%s» @ %s не ставлю: по этой встрече владелец сказал «нет» "
            "(запись отказа «%s», %s). Саму серию веду дальше — отказ был про "
            "одну встречу",
            item.get("meeting_id"), item.get("start_at"), hit[0],
            proof_ru(hit[1], fp),
        )
    return kept


def _memory_keys(fp) -> list[str]:
    """Ключи, под которыми группа помнится между тиками (см. DEDUP_WINNERS_FILE).

    Оба уровня сразу: копии могли схлопнуться по точному ключу, а в следующем
    тике встретиться уже по комнате (или наоборот).
    """
    keys: list[str] = []
    if fp.start:
        if fp.ical:
            keys.append("ical\t%s\t%s" % (fp.ical, fp.start))
        if fp.url:
            keys.append("room\t%s\t%s" % (fp.url, fp.start))
    return keys


def load_dedup_winners(now: datetime | None = None) -> dict[str, Any]:
    """Прочитать память о победителях, выкинув всё, что уже отгорело."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        raw = json.loads(dedup_winners_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Память о схлопывании нечитаема (%s) — начинаю с чистой", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    edge = now - timedelta(hours=WINNER_MEMORY_KEEP_HOURS)
    for key, val in raw.items():
        if not isinstance(key, str) or not isinstance(val, dict):
            continue
        try:
            start = datetime.fromisoformat(str(val.get("start_at")))
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if start >= edge:
            out[key] = val
    return out


def save_dedup_winners(memory: dict[str, Any]) -> None:
    """Записать память атомарно. Файл вспомогательный — сбой не роняет прогон."""
    dwf = dedup_winners_file()
    dwf.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dwf.name + ".",
                               dir=str(dwf.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dwf)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _remembered(memory: dict[str, Any], fps: list) -> dict[str, Any] | None:
    for fp in fps:
        for key in _memory_keys(fp):
            val = memory.get(key)
            if isinstance(val, dict) and val.get("event_id"):
                return val
    return None


def _pick_winner(
    group: list[dict[str, Any]],
    remembered: dict[str, Any] | None,
    frozen: bool,
) -> tuple[dict[str, Any], str]:
    """Кого ведём. Возвращает (победитель, почему именно его)."""
    if frozen and remembered:
        for it in group:
            if it.get("event_id") == remembered.get("event_id"):
                return it, "по ней уже вели в прошлый тик"
    for it in group:
        if _is_owner_named(it):
            # У записи с человеческим именем привязка чата, разметка
            # company/visibility и режим открытых вопросов. Автоимя вечернего
            # блока `oneoff-<hash>` — расходный материал.
            return it, "имя записи дал владелец"
    return group[0], "первая в реестре"


def dedupe_queue(
    queue: list[dict[str, Any]],
    *,
    memory: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """ISS-54: схлопнуть элементы очереди, которые описывают ОДНУ встречу.

    Формула тождества — общая с реестром, живёт в `lib/meeting_identity.py`.
    Здесь важны три вещи сверх неё.

    1. Совпали комната и секунда старта — схлопываем ВСЕГДА, даже если у
       событий разные `iCalUID`. Один URL Telemost в одну секунду — это один
       разговор; два элемента очереди на него означают двух ботов в комнате.
       Разные точные ключи — не повод пропустить обоих, а повод сказать
       владельцу: в календаре два разных события, проверь (WARNING в лог).
    2. Молча не гасим никогда: на каждое схлопывание — строка в scheduler.log
       с обоими id, обоими календарями и именем победителя.
    3. Победитель ЗАКРЕПЛЯЕТСЯ между тиками (`memory`). Иначе достаточно, чтобы
       в следующем тике победитель из очереди пропал, — и на встречу приезжает
       второй бот под другим `event_id`. Если победитель пропал, а проигравший
       остался, проигравший наследует `event_id` победителя: на нём ключуются
       все барьеры ниже по течению.

    Элемент очереди не мутируем: работаем с копиями (функцию тестируют как
    чистую, и `.queue.json` пишется из возвращённого списка).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    mem = memory if isinstance(memory, dict) else {}

    kept: list[dict[str, Any]] = []
    kept_fp: list[Any] = []          # приметы выжившего (объединённые по группе)
    groups: list[list[dict[str, Any]]] = []   # все элементы группы
    group_fps: list[list[Any]] = []           # их приметы
    by_start: dict[str, list[int]] = {}       # старт → индексы в kept (линейность)

    for raw in queue:
        if not isinstance(raw, dict):
            # Дедуп — не то место, где можно потерять старт: чужой мусор
            # проносим как есть, разбираться будет тот, кто его положил.
            logger.warning("В очереди не-словарь (%s) — оставляю как есть",
                           type(raw).__name__)
            kept.append(raw)
            kept_fp.append(fingerprint())
            groups.append([])
            group_fps.append([])
            continue

        item = dict(raw)
        fp = fingerprint(item.get("start_at"), item.get("url"), item.get("ical_uid"))

        twin_idx: int | None = None
        proof, conflict = MATCH_NONE, False
        if fp.start is not None:
            for idx in by_start.get(fp.start, ()):
                proof, conflict = compare(kept_fp[idx], fp)
                if proof:
                    twin_idx = idx
                    break

        if twin_idx is None:
            kept.append(item)
            kept_fp.append(fp)
            groups.append([item])
            group_fps.append([fp])
            if fp.start is not None:
                by_start.setdefault(fp.start, []).append(len(kept) - 1)
            continue

        prev = kept[twin_idx]
        groups[twin_idx].append(item)
        group_fps[twin_idx].append(fp)
        if conflict:
            logger.warning(
                "В одной комнате в одну и ту же секунду — ДВА РАЗНЫХ события "
                "календаря: «%s» (%s) и «%s» (%s) @ %s %s. Веду как одну встречу "
                "(иначе в комнату придут два бота и запишут один разговор), но "
                "проверь календарь — похоже, событие продублировано",
                prev.get("meeting_id"), ical_key(prev.get("ical_uid")) or "?",
                item.get("meeting_id"), fp.ical or "?",
                item.get("start_at"), item.get("url"),
            )

        remembered = _remembered(mem, group_fps[twin_idx])
        frozen = _is_frozen(fp.start, now)
        winner, why = _pick_winner(groups[twin_idx], remembered, frozen)
        dropped = [it for it in groups[twin_idx] if it is not winner]
        logger.info(
            "Схлопнул дубль встречи (%s): «%s» [%s] и «%s» [%s] @ %s — веду по "
            "«%s» (%s), второй старт не ставлю (иначе придут два бота)",
            proof_ru(proof, fp),
            prev.get("meeting_id"), prev.get("calendar_id") or "?",
            item.get("meeting_id"), item.get("calendar_id") or "?",
            item.get("start_at"), winner.get("meeting_id"), why,
        )
        # Ключ дубля мог быть известен только у выбывшего — переносим на
        # победителя, иначе следующий тик не узнает встречу по точному ключу.
        if not ical_key(winner.get("ical_uid")):
            for other in dropped:
                other_ical = ical_key(other.get("ical_uid"))
                if other_ical:
                    winner["ical_uid"] = other_ical
                    break
        # Ф10b: ту же встречу могли принести И личная серия владельца, И наряд
        # портала. Победила серия — связь с нарядом обязана переехать на неё,
        # иначе портал никогда не узнает, что его встречу записали (наряд навсегда
        # остался бы «ждущим», а результат ушёл бы только в Telegram владельца).
        if not winner.get("order_id"):
            for other in dropped:
                if other.get("order_id"):
                    winner["order_id"] = other["order_id"]
                    logger.info(
                        "Дубль встречи пришёл и нарядом портала (%s) — веду по "
                        "личной серии «%s», связь с нарядом сохраняю",
                        other.get("order_id"), winner.get("meeting_id"),
                    )
                    break
        kept[twin_idx] = winner
        kept_fp[twin_idx] = fingerprint(
            winner.get("start_at"), winner.get("url"), winner.get("ical_uid"))

    _apply_winner_memory(kept, kept_fp, group_fps, mem, now)
    return kept


def _is_frozen(start_key: str | None, now: datetime) -> bool:
    """Пора ли держаться за прошлый выбор: до старта меньше окна заморозки."""
    if not start_key:
        return False
    try:
        start = datetime.fromisoformat(start_key)
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return now >= start - timedelta(minutes=WINNER_FREEZE_MINUTES)


def _apply_winner_memory(
    kept: list[dict[str, Any]],
    kept_fp: list[Any],
    group_fps: list[list[Any]],
    memory: dict[str, Any],
    now: datetime,
) -> None:
    """Закрепить победителя и подобрать «осиротевших» проигравших.

    Осиротевший — это выживший элемент, который в прошлые тики был проигравшим,
    а победитель из очереди исчез (календарь его записи не ответил; победитель
    был разовой записью и погасил себя после старта). Своего `event_id` ему
    хватит, чтобы привести ВТОРОГО бота на встречу, где первый уже пишет.
    Поэтому он наследует `event_id` победителя: на этом ключе стоят барьеры
    `runner._candidates` и `state_acquire`.
    """
    for idx, item in enumerate(kept):
        if not isinstance(item, dict):
            continue
        fp = kept_fp[idx]
        # Помним группу под ключами ВСЕХ её элементов: копии могли схлопнуться
        # по точному ключу, приехав из разных комнат, — тогда в следующем тике
        # выживший узнается по своей комнате.
        keys = list(dict.fromkeys(
            [k for member in group_fps[idx] for k in _memory_keys(member)]
            + _memory_keys(fp)
        ))
        if not keys:
            continue
        remembered = _remembered(memory, [fp])
        collapsed_now = len(group_fps[idx]) > 1
        if (
            remembered
            and not collapsed_now
            and remembered.get("event_id") != item.get("event_id")
            and _is_frozen(fp.start, now)
        ):
            logger.warning(
                "Встречу @ %s в прошлый тик вели по «%s», сейчас её в очереди "
                "нет — веду по «%s», но барьер повторного старта оставляю "
                "прежним (иначе на встречу приедет второй бот)",
                item.get("start_at"), remembered.get("meeting_id"),
                item.get("meeting_id"),
            )
            item["event_id"] = remembered["event_id"]
            continue
        entry = {
            "event_id": item.get("event_id"),
            "meeting_id": item.get("meeting_id"),
            "start_at": fp.start,
        }
        for key in keys:
            memory[key] = entry


def remembered_event_id(
    item: dict[str, Any], memory: dict[str, Any], now: datetime
) -> str | None:
    """Под каким `event_id` эту встречу уже вели (память дублей), если старт заморожен.

    Для того, кто смотрит на ОДИН элемент вне прогона планировщика (runner с
    нарядом у самого старта): тот же выбор, что `_apply_winner_memory` делает для
    выжившего элемента без пары, но без записи в память — писать её дело
    планировщика — и без предупреждения в журнал: иначе runner каждую минуту
    писал бы от имени планировщика «встречи в очереди нет».
    """
    fp = fingerprint(item.get("start_at"), item.get("url"), item.get("ical_uid"))
    if not _memory_keys(fp):
        return None
    remembered = _remembered(memory, [fp])
    if (
        remembered
        and remembered.get("event_id") != item.get("event_id")
        and _is_frozen(fp.start, now)
    ):
        return str(remembered["event_id"])
    return None


def _expand_google_calendar(
    rec: dict[str, Any],
    rooms_data: dict[str, Any],
    creds: Credentials,
    horizon_start: datetime,
    horizon_end: datetime,
) -> tuple[list[dict[str, Any]], Credentials]:
    calendar_id = rec["calendar_id"]
    recurring = rec.get("recurring_event_id")
    one_event = rec.get("event_id")

    events: list[dict[str, Any]] = []
    if recurring:
        events, creds = list_instances(
            creds, calendar_id, recurring, horizon_start, horizon_end
        )
    elif one_event:
        evt, creds = get_event(creds, calendar_id, one_event)
        if evt:
            start = event_start_dt(evt)
            if start and horizon_start <= start <= horizon_end:
                events = [evt]
    else:
        logger.warning("google-calendar запись %s без event_id и recurring — пропущена", rec.get("id"))
        return [], creds

    out: list[dict[str, Any]] = []
    for evt in events:
        if evt.get("status") == "cancelled":
            continue
        start = event_start_dt(evt)
        if not start:
            continue
        url = _resolve_url(rec, rooms_data, calendar_event=evt)
        if not url:
            push(
                f"Не знаю куда подключаться для встречи «{rec.get('series')}» "
                f"({start.isoformat()}). Добавь `room: @<name>` в watched.yaml "
                f"или впиши ссылку в Google Calendar."
            )
            logger.warning("URL не разрешён для %s @ %s — встреча пропущена", rec.get("id"), start.isoformat())
            continue
        out.append(_queue_item(rec, evt, start, url))
    return out, creds


def _expand_manual(
    rec: dict[str, Any],
    rooms_data: dict[str, Any],
    horizon_start: datetime,
    horizon_end: datetime,
) -> list[dict[str, Any]]:
    cron_expr = rec["cron"]
    tz_name = rec.get("tz", "Asia/Dubai")
    try:
        starts = expand_cron(cron_expr, tz_name, horizon_end, now=horizon_start)
    except Exception as e:
        logger.error("manual %s: cron %r tz=%s невалиден: %s", rec.get("id"), cron_expr, tz_name, e)
        return []
    out: list[dict[str, Any]] = []
    for start in starts:
        url = _resolve_url(rec, rooms_data, calendar_event=None)
        if not url:
            push(
                f"Не знаю куда подключаться для встречи «{rec.get('series')}» "
                f"({start.isoformat()}). Добавь `room: @<name>` в watched.yaml."
            )
            continue
        out.append(_queue_item(rec, None, start, url))
    return out


def _expand_one_off(
    rec: dict[str, Any],
    rooms_data: dict[str, Any],
    horizon_start: datetime,
    horizon_end: datetime,
) -> list[dict[str, Any]]:
    raw = rec.get("datetime")
    if not raw:
        return []
    if isinstance(raw, datetime):
        start = raw
    else:
        try:
            start = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            logger.error("one-off %s: невалидный datetime %r", rec.get("id"), raw)
            return []
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start < horizon_start or start > horizon_end:
        return []
    url = _resolve_url(rec, rooms_data, calendar_event=None)
    if not url:
        push(
            f"Не знаю куда подключаться для встречи «{rec.get('series')}» "
            f"({start.isoformat()}). Добавь `room: @<name>` в watched.yaml."
        )
        return []
    return [_queue_item(rec, None, start, url)]


def _resolve_url(
    rec: dict[str, Any],
    rooms_data: dict[str, Any],
    *,
    calendar_event: dict[str, Any] | None,
) -> str | None:
    """Приоритеты: явный room (3 уровень из плана) → conferenceData → None.

    Возвращает URL Telemost для подключения. None — если разрешить не удалось.
    """
    explicit = rec.get("room")
    if explicit:
        try:
            url = resolve_room_to_url(explicit, rooms_data)
            if url:
                return url
        except SystemExit as e:
            logger.warning("room %r записи %s невалиден: %s", explicit, rec.get("id"), e)
    if calendar_event:
        url = extract_meeting_url(calendar_event)
        if url:
            return url
    return None


def _queue_item(
    rec: dict[str, Any],
    calendar_event: dict[str, Any] | None,
    start: datetime,
    url: str,
) -> dict[str, Any]:
    duration = rec.get("duration_minutes")
    if not duration and calendar_event:
        # Попробовать высчитать из end - start.
        try:
            end_raw = (calendar_event.get("end") or {}).get("dateTime")
            if end_raw:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                duration = max(1, int((end_dt - start).total_seconds() // 60))
        except (ValueError, TypeError):
            pass
    if not duration:
        duration = 60

    # event_id для state-файла: для google-calendar — gcal id экземпляра;
    # для прочих — meeting_id + start (округлённый до минуты).
    if calendar_event and calendar_event.get("id"):
        event_id = f"gcal:{calendar_event['id']}"
    else:
        event_id = f"local:{rec['id']}:{start.strftime('%Y%m%dT%H%M')}"

    # ISS-54: точный ключ «одно и то же событие» через все календари. Берём из
    # самого события; если календарь поля не дал — из записи реестра (его туда
    # кладёт apply-reply при заведении встречи).
    ical_uid = event_ical_uid(calendar_event) if calendar_event else None
    if not ical_uid:
        raw = rec.get("ical_uid")
        ical_uid = raw.strip() if isinstance(raw, str) and raw.strip() else None

    return {
        "meeting_id": rec["id"],
        "event_id": event_id,
        "series": rec.get("series") or rec["id"],
        "start_at": start.isoformat(),
        "duration_minutes": int(duration),
        "url": url,
        "expected_participants": rec.get("expected_participants") or [],
        "keep_audio": bool(rec.get("keep_audio", False)),
        "type": rec.get("type", "?"),
        # Диагностика дублей: без календаря в элементе лог о схлопывании не
        # говорит владельцу, ИЗ КАКИХ двух календарей приехала одна встреча.
        "calendar_id": rec.get("calendar_id"),
        "ical_uid": ical_uid,
    }


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduler упал с исключением: %s", e)
        push(f"scheduler упал: {e}", dedupe=True)
        rc = 1
    sys.exit(rc)
