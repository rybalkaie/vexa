# -*- coding: utf-8 -*-
"""ISS-54: на одну встречу — один бот, даже если событие видно в двух календарях.

Боевой случай: выставка СТ-1 27.07 лежала и в личном календаре, и в календаре
Anzhee. Google отдал её двумя РАЗНЫМИ `event_id`, реестр завёл две записи
(`oneoff-ef50c3ab` и `anzhee-vystavka-st1-20260727`), и на встречу пришли два
бота. Все барьеры ниже по течению это пропускают: `runner._candidates` ключует
`event_id`, `_concurrency_decision` — `series`, `state_acquire` — `event_id`; у
двух записей все три ключа разные.

Точный ключ «одно и то же событие» у Google — `iCalUID`: у копий события в разных
календарях он совпадает. Ловушка: у всех экземпляров ОДНОЙ рекуррентной серии он
тоже совпадает (различаются только `id`), поэтому ключ всегда парный —
`iCalUID` + время старта. Пара «время старта + URL» — страховка, и на боевом пути
она ГЛАВНАЯ: в снапшот вечернего блока `iCalUID` пока не приезжает.

Правило, принятое по ходу ISS-54: совпали комната и секунда старта — это одна
встреча, схлопываем ВСЕГДА, даже если `iCalUID` у событий разные. Один URL
Telemost в одну секунду — это один разговор; пропустить обоих значит привести в
комнату двух ботов, ровно ту беду, ради которой всё делалось. Разные точные
ключи — не повод пропустить, а повод сказать владельцу: проверь календарь.

Покрывает:
  • scheduler: два календаря → один элемент очереди, и про схлопывание есть лог;
  • scheduler: две РАЗНЫЕ серии в одной комнате в разное время не схлопываются;
  • scheduler: экземпляры одной рекуррентной серии (общий iCalUID) не схлопываются;
  • scheduler: разные `iCalUID` в одной комнате в одну секунду — одна встреча,
    но с предупреждением владельцу;
  • scheduler: победитель схлопывания не меняется между тиками, а если он из
    очереди пропал — выживший наследует его барьер повторного старта;
  • scheduler: сбой дедупа не уносит очередь (fail-open);
  • страховка «время + URL»: отдельно доказано, что без совпавшего времени и без
    совпавшей комнаты она НЕ срабатывает (на обоих путях);
  • запись в реестр: «да» по двум копиям одной встречи заводит ОДНУ запись;
  • запись в реестр: дубль не заводится поверх отказа владельца (ISS-53); «нет» и
    «никогда» по копии гасят запись-близнеца, называют её владельцу и оставляют
    след под ключом самой копии (иначе вечерний блок спросит снова);
  • запись в реестр: разные встречи и разные экземпляры одной серии не схлопываются;
  • одна формула тождества на оба пути (`lib/meeting_identity.py`).

Сети и боевых данных здесь нет: календарь замокан, реестр — во временном каталоге.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary/tests
    python3 test_iss54_meeting_dedup.py
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

# Каталоги реестра/логов/состояния связываются на импорте (registry.py,
# lib/state.py). Уводим их во временный каталог ДО импорта модулей зоны —
# боевой ~/Projects/me/встречи тест трогать не имеет права.
_TMP_ROOT = tempfile.mkdtemp(prefix="iss54-registry-")
os.environ["MEETING_NOTARY_REGISTRY_DIR"] = _TMP_ROOT
os.environ["MEETING_NOTARY_LOG_DIR"] = _TMP_ROOT
os.environ["MEETING_NOTARY_STATE_DIR"] = _TMP_ROOT
atexit.register(shutil.rmtree, _TMP_ROOT, True)

import notary.lib.gcal as gcal  # noqa: E402
import notary.lib.meeting_identity as mid  # noqa: E402
import notary.lib.state as state  # noqa: E402
import notary.meetings_apply_reply as mar  # noqa: E402
import notary.meetings_evening_block as meb  # noqa: E402
import notary.scheduler as sched  # noqa: E402


CAL_MAIN = "primary"
CAL_ANZHEE = "anzhee@example.com"
ROOM_URL = "https://telemost.yandex.ru/j/11111111111111"
OTHER_URL = "https://telemost.yandex.ru/j/22222222222222"
ICAL_VYSTAVKA = "vystavka-st1@google.com"
ROOMS = {"rooms": [{"name": "t11", "url": ROOM_URL}]}


# ==========================================================================
# Хелперы: фейковый календарь, прогон scheduler-а и apply_decisions без сети
# ==========================================================================
def _soon(hours: int = 5, minute: int = 0) -> datetime:
    """Момент внутри 48-часового горизонта планировщика."""
    base = datetime.now(timezone.utc) + timedelta(hours=hours)
    return base.replace(minute=minute, second=0, microsecond=0)


def _event(
    event_id: str,
    start: datetime,
    *,
    ical_uid: str | None = None,
    url: str | None = ROOM_URL,
    title: str = "Выставка СТ-1",
    recurring: str | None = None,
    duration_min: int = 60,
) -> dict:
    """Событие в той форме, в какой его отдаёт Google Calendar API."""
    evt: dict = {
        "id": event_id,
        "status": "confirmed",
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=duration_min)).isoformat()},
    }
    if url:
        evt["conferenceData"] = {"entryPoints": [{"uri": url}]}
    if ical_uid:
        evt["iCalUID"] = ical_uid
    if recurring:
        evt["recurringEventId"] = recurring
    return evt


def _rec_once(rec_id: str, calendar_id: str, event_id: str, **extra) -> dict:
    rec = {
        "id": rec_id,
        "series": rec_id,
        "type": "google-calendar",
        "calendar_id": calendar_id,
        "event_id": event_id,
        "enabled": True,
        "ignored": False,
    }
    rec.update(extra)
    return rec


def _rec_series(rec_id: str, calendar_id: str, recurring_event_id: str, **extra) -> dict:
    rec = {
        "id": rec_id,
        "series": rec_id,
        "type": "google-calendar",
        "calendar_id": calendar_id,
        "recurring_event_id": recurring_event_id,
        "enabled": True,
        "ignored": False,
    }
    rec.update(extra)
    return rec


class _FakeCreds:
    """Заглушка OAuth — планировщик только прокидывает её в календарные вызовы."""


class _LogSink(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.messages.append(record.getMessage())


def _in_minutes(minutes: int) -> datetime:
    """Момент близко к старту — там, где выбор победителя уже заморожен."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(
        second=0, microsecond=0)


def _fresh_winners_file() -> Path:
    """Отдельный файл памяти о схлопывании на каждый прогон.

    Память живёт между тиками НАМЕРЕННО (иначе на встречу приезжает второй бот),
    поэтому по умолчанию тесты друг другу её не наследуют — иначе один прогон
    чинил бы или ломал соседний.
    """
    fd, path = tempfile.mkstemp(prefix="iss54-winners-", suffix=".json", dir=_TMP_ROOT)
    os.close(fd)
    os.unlink(path)
    return Path(path)


def _run_scheduler(
    watched_records, events_by_cal, winners_file=None,
) -> tuple[int, list[dict], list[str], list[str]]:
    """Прогнать scheduler.main() без Google. Возвращает (rc, очередь, логи, push-и)."""
    written: list[list[dict]] = []
    pushes: list[str] = []
    if winners_file is None:
        winners_file = _fresh_winners_file()

    def fake_get_event(creds, calendar_id, event_id):
        for evt in events_by_cal.get(calendar_id, []):
            if evt.get("id") == event_id:
                return evt, creds
        return None, creds

    def fake_list_instances(creds, calendar_id, recurring_event_id, time_min, time_max):
        out = []
        for evt in events_by_cal.get(calendar_id, []):
            if evt.get("recurringEventId") != recurring_event_id:
                continue
            start = gcal.event_start_dt(evt)
            if start and time_min <= start <= time_max:
                out.append(evt)
        return out, creds

    sink = _LogSink()
    prev_level, prev_propagate = sched.logger.level, sched.logger.propagate
    sched.logger.addHandler(sink)
    sched.logger.setLevel(logging.DEBUG)
    sched.logger.propagate = False
    try:
        with mock.patch.object(sched, "setup_logging", lambda: None), \
                mock.patch.object(sched, "DEDUP_WINNERS_FILE", winners_file), \
                mock.patch.object(sched, "pause_until", lambda: None), \
                mock.patch.object(sched, "load_watched", lambda *a, **k: {"watched": list(watched_records)}), \
                mock.patch.object(sched, "load_rooms", lambda *a, **k: {"rooms": []}), \
                mock.patch.object(sched, "load_credentials", lambda: _FakeCreds()), \
                mock.patch.object(sched, "get_event", fake_get_event), \
                mock.patch.object(sched, "list_instances", fake_list_instances), \
                mock.patch.object(sched, "write_queue", lambda items, **k: written.append(list(items))), \
                mock.patch.object(sched, "write_last_success", lambda *a, **k: None), \
                mock.patch.object(sched, "read_last_success", lambda: None), \
                mock.patch.object(sched, "push", lambda msg, **k: pushes.append(msg)):
            rc = sched.main()
    finally:
        sched.logger.removeHandler(sink)
        sched.logger.setLevel(prev_level)
        sched.logger.propagate = prev_propagate
    queue = written[-1] if written else []
    return rc, queue, sink.messages, pushes


def _snapshot_item(
    n: int,
    calendar_id: str,
    event_id: str,
    start: datetime,
    *,
    ical_uid: str | None = None,
    url: str | None = ROOM_URL,
    title: str = "Выставка СТ-1",
    recurring: str | None = None,
) -> dict:
    """Элемент snapshot в том виде, в каком его пишет вечерний блок."""
    item = {
        "n": n,
        "calendar_id": calendar_id,
        "event_id": event_id,
        "recurring_event_id": recurring,
        "title": title,
        "start_at": start.isoformat(),
        "url_in_event": url,
        "has_url_in_event": bool(url),
    }
    if ical_uid:
        # Поле появляется у snapshot, когда его научится класть вечерний блок;
        # apply-reply обязан понимать оба варианта.
        item["ical_uid"] = ical_uid
    return item


def _apply(decisions, items, watched_records=(), today=None, rooms=None):
    """Прогнать apply_decisions поверх реестра-в-памяти. Возвращает (result, data)."""
    data = {"watched": [dict(r) for r in watched_records]}
    rooms = rooms if rooms is not None else {"rooms": []}
    kwargs = {}
    if today is not None:
        kwargs["today"] = today
    with mock.patch.object(mar, "load_watched", lambda *a, **k: data), \
            mock.patch.object(mar, "save_watched", lambda d: None), \
            mock.patch.object(mar, "load_rooms", lambda *a, **k: rooms), \
            mock.patch.object(mar, "release_watched_lock", lambda: None):
        result = mar.apply_decisions(decisions, items, **kwargs)
    return result, data


def _offered_again(data: dict, calendar_id: str, event_id: str, recurring=None) -> bool:
    """Спросит ли вечерний блок про эту встречу завтра ЕЩЁ РАЗ?

    Проверяем его собственным индексом: блок считает встречу известной по паре
    (calendar_id, event_id) — про запись-близнеца под другим event_id он не знает.
    """
    idx = meb.index_watched(data["watched"])
    return meb.find_in_watched(idx, calendar_id, event_id, recurring) is None


def _gcal_records(data: dict) -> list[dict]:
    return [r for r in data["watched"] if r.get("type") == "google-calendar"]


# ==========================================================================
# scheduler: одна встреча — один старт
# ==========================================================================
class TestSchedulerDedup(unittest.TestCase):
    """Очередь стартов: копии одного события схлопываются, разные встречи — нет."""

    def setUp(self):
        self.start = _soon(5)
        # Одно событие, два календаря: разные event_id, ОДИН iCalUID.
        self.evt_main = _event("evt-personal-copy", self.start, ical_uid=ICAL_VYSTAVKA)
        self.evt_anzhee = _event("evt-anzhee-copy", self.start, ical_uid=ICAL_VYSTAVKA)
        self.records = [
            _rec_once("oneoff-ef50c3ab", CAL_MAIN, "evt-personal-copy"),
            _rec_once("anzhee-vystavka-st1-20260727", CAL_ANZHEE, "evt-anzhee-copy"),
        ]
        self.events = {CAL_MAIN: [self.evt_main], CAL_ANZHEE: [self.evt_anzhee]}

    def test_two_calendars_one_queue_item(self):
        rc, queue, _logs, pushes = _run_scheduler(self.records, self.events)
        self.assertEqual(rc, 0)
        self.assertEqual(pushes, [], "наружу ничего не должно улетать")
        self.assertEqual(
            len(queue), 1,
            "ISS-54: одно событие в двух календарях дало %d стартов — придут два бота: %s"
            % (len(queue), [it.get("meeting_id") for it in queue]),
        )

    def test_collapse_is_logged(self):
        _rc, _queue, logs, _pushes = _run_scheduler(self.records, self.events)
        joined = "\n".join(logs)
        self.assertIn("oneoff-ef50c3ab", joined)
        self.assertIn("anzhee-vystavka-st1-20260727", joined)
        self.assertTrue(
            any(
                "oneoff-ef50c3ab" in m and "anzhee-vystavka-st1-20260727" in m
                for m in logs
            ),
            "нет строки лога про схлопывание с обоими id — молча гасить нельзя:\n"
            + joined,
        )

    def test_owner_named_record_wins(self):
        # Ведём встречу по записи, которую вёл владелец: у неё привязка чата,
        # разметка company/visibility и режим открытых вопросов. Автоимя
        # `oneoff-<hash>` от вечернего блока — расходный материал.
        _rc, queue, _logs, _pushes = _run_scheduler(self.records, self.events)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["meeting_id"], "anzhee-vystavka-st1-20260727")

    def test_queue_item_carries_ical_uid_and_calendar_id(self):
        _rc, queue, _logs, _pushes = _run_scheduler(
            [self.records[0]], {CAL_MAIN: [self.evt_main]})
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].get("ical_uid"), ICAL_VYSTAVKA)
        self.assertEqual(queue[0].get("calendar_id"), CAL_MAIN)

    def test_two_series_in_one_room_at_different_times_are_kept(self):
        # `@t11` обслуживает и координацию, и выставку. Разное время — разные
        # встречи. iCalUID тут намеренно НЕ проставлен: проверяем именно
        # страховочный ключ «URL + время», где время — единственное отличие.
        early, late = _soon(5), _soon(9)
        records = [
            _rec_once("koordinaciya-1", CAL_MAIN, "evt-koord"),
            _rec_once("vystavka-1", CAL_MAIN, "evt-vyst"),
        ]
        events = {CAL_MAIN: [
            _event("evt-koord", early, title="Координация"),
            _event("evt-vyst", late, title="Выставка"),
        ]}
        _rc, queue, _logs, _pushes = _run_scheduler(records, events)
        self.assertEqual(
            len(queue), 2,
            "две разные встречи в одной комнате в разное время схлопнулись — "
            "одна из них останется без записи",
        )

    def test_recurring_instances_share_ical_uid_but_are_kept(self):
        # У всех экземпляров одной серии iCalUID ОДИН. Ключ без времени старта
        # выкосил бы всю неделю, оставив один старт.
        rec = _rec_series("koordinaciya", CAL_MAIN, "recur-koord")
        events = {CAL_MAIN: [
            _event("recur-koord_1", _soon(5), ical_uid="recur-koord@google.com",
                   recurring="recur-koord"),
            _event("recur-koord_2", _soon(29), ical_uid="recur-koord@google.com",
                   recurring="recur-koord"),
        ]}
        _rc, queue, _logs, _pushes = _run_scheduler([rec], events)
        self.assertEqual(
            len(queue), 2,
            "экземпляры одной серии схлопнулись по общему iCalUID — "
            "бот придёт только на первую встречу недели",
        )

    def test_same_room_same_time_without_ical_uid_is_collapsed(self):
        # Страховка второго уровня: iCalUID неизвестен (старый реестр / ручная
        # запись), но время и комната совпадают — это одна встреча.
        records = [
            _rec_once("oneoff-aaaaaaaa", CAL_MAIN, "evt-a"),
            _rec_once("vystavka-ruchnaya", CAL_ANZHEE, "evt-b"),
        ]
        events = {
            CAL_MAIN: [_event("evt-a", self.start)],
            CAL_ANZHEE: [_event("evt-b", self.start)],
        }
        _rc, queue, logs, _pushes = _run_scheduler(records, events)
        self.assertEqual(len(queue), 1, "страховка «время + URL» не сработала")
        self.assertTrue(
            any("oneoff-aaaaaaaa" in m and "vystavka-ruchnaya" in m for m in logs),
            "схлопывание по страховке не залогировано",
        )

    def test_different_ical_uids_in_one_room_collapse_with_warning(self):
        # РАЗНЫЕ iCalUID в ОДНОЙ комнате в ОДНУ секунду. Так выглядит событие,
        # которое скопировали между календарями руками, а не пригласили: точные
        # ключи различаются, а разговор один. Раньше здесь стояло вето («две
        # доказуемо разные встречи — веду обе»), и оно приводило в комнату двух
        # ботов — ровно то, ради чего фаза делалась. Схлопываем, но владельцу
        # говорим: в календаре два разных события, проверь.
        records = [
            _rec_once("koordinaciya-1", CAL_MAIN, "evt-koord"),
            _rec_once("vystavka-1", CAL_ANZHEE, "evt-vyst"),
        ]
        events = {
            CAL_MAIN: [_event("evt-koord", self.start, ical_uid="koord@google.com")],
            CAL_ANZHEE: [_event("evt-vyst", self.start, ical_uid="vystavka@google.com")],
        }
        _rc, queue, logs, _pushes = _run_scheduler(records, events)
        self.assertEqual(
            len(queue), 1,
            "разные iCalUID в одной комнате в одну секунду не схлопнулись — "
            "в комнату приедут два бота: %s" % [it.get("meeting_id") for it in queue],
        )
        self.assertTrue(
            any("ДВА РАЗНЫХ события" in m and "koordinaciya-1" in m and "vystavka-1" in m
                for m in logs),
            "владельцу не сказано, что в календаре два разных события в одной "
            "комнате в одну секунду:\n" + "\n".join(logs),
        )


# ==========================================================================
# lib/gcal: iCalUID протянут от календарного слоя
# ==========================================================================
class TestGcalIcalUid(unittest.TestCase):

    def test_reads_field(self):
        self.assertEqual(
            gcal.event_ical_uid(_event("e", _soon(), ical_uid=ICAL_VYSTAVKA)),
            ICAL_VYSTAVKA,
        )

    def test_missing_or_garbage_is_none(self):
        self.assertIsNone(gcal.event_ical_uid(_event("e", _soon())))
        self.assertIsNone(gcal.event_ical_uid({"iCalUID": ""}))
        self.assertIsNone(gcal.event_ical_uid({"iCalUID": 42}))
        self.assertIsNone(gcal.event_ical_uid(None))


# ==========================================================================
# Запись в реестр: «да» по двум копиям одной встречи = одна запись
# ==========================================================================
class TestRegistryWriteDedup(unittest.TestCase):

    def setUp(self):
        self.start = _soon(20)

    def _two_copies(self, *, ical: str | None) -> list[dict]:
        return [
            _snapshot_item(1, CAL_MAIN, "evt-personal-copy", self.start, ical_uid=ical),
            _snapshot_item(2, CAL_ANZHEE, "evt-anzhee-copy", self.start, ical_uid=ical),
        ]

    def test_two_calendars_one_record_by_ical_uid(self):
        items = self._two_copies(ical=ICAL_VYSTAVKA)
        result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        recs = _gcal_records(data)
        self.assertEqual(
            len(recs), 1,
            "ISS-54: «да» по двум копиям одного события завело %d записей: %s"
            % (len(recs), [r["id"] for r in recs]),
        )
        self.assertTrue(
            any("та же встреча" in line for line in result["summary_lines"]),
            "владелец не увидел, что вторая копия — та же встреча: %s"
            % result["summary_lines"],
        )

    def test_two_calendars_one_record_without_ical_uid(self):
        # Сегодняшний вечерний блок iCalUID в snapshot не кладёт — держит
        # страховка «время старта + URL».
        items = self._two_copies(ical=None)
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        self.assertEqual(len(_gcal_records(data)), 1)

    def test_record_carries_fingerprint(self):
        items = [_snapshot_item(1, CAL_MAIN, "evt-personal-copy", self.start,
                                ical_uid=ICAL_VYSTAVKA)]
        _result, data = _apply([{"n": 1, "action": "accept_once", "room": None}], items)
        rec = _gcal_records(data)[0]
        self.assertEqual(rec.get("ical_uid"), ICAL_VYSTAVKA)
        self.assertEqual(rec.get("event_start_at"), self.start.isoformat())
        self.assertEqual(rec.get("event_url"), ROOM_URL)

    def test_different_meetings_are_not_collapsed(self):
        items = [
            _snapshot_item(1, CAL_MAIN, "evt-koord", _soon(20),
                           ical_uid="koord@google.com", title="Координация"),
            _snapshot_item(2, CAL_MAIN, "evt-vyst", _soon(26),
                           ical_uid="vystavka@google.com", title="Выставка"),
        ]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        self.assertEqual(len(_gcal_records(data)), 2)

    def test_two_different_meetings_at_the_same_time_are_kept(self):
        # Две разные встречи в одно время (в разных комнатах): точные ключи
        # известны и различны — схлопывать нельзя даже при совпавшем старте.
        items = [
            _snapshot_item(1, CAL_MAIN, "evt-koord", self.start,
                           ical_uid="koord@google.com", url=ROOM_URL, title="Координация"),
            _snapshot_item(2, CAL_ANZHEE, "evt-vyst", self.start,
                           ical_uid="vystavka@google.com", url=OTHER_URL, title="Выставка"),
        ]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        self.assertEqual(
            len(_gcal_records(data)), 2,
            "две разные встречи в одно время схлопнулись — одна осталась без записи",
        )

    def test_two_instances_of_one_series_are_kept(self):
        # Разово согласились на вторник и на четверг одной серии: iCalUID общий,
        # старты разные — это две встречи, и записей должно быть две.
        items = [
            _snapshot_item(1, CAL_MAIN, "recur_1", _soon(20),
                           ical_uid="recur@google.com", recurring="recur"),
            _snapshot_item(2, CAL_MAIN, "recur_2", _soon(44),
                           ical_uid="recur@google.com", recurring="recur"),
        ]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        self.assertEqual(
            len(_gcal_records(data)), 2,
            "два разных экземпляра одной серии схлопнулись в одну запись",
        )

    def test_yes_on_twin_of_rejected_meeting_does_not_add_second_record(self):
        # ISS-53: отказ лежит в реестре выключенной записью. «Да» по копии из
        # другого календаря обязано ожить ЕЁ, а не завести вторую.
        rejected = _rec_once(
            "oneoff-ef50c3ab", CAL_MAIN, "evt-personal-copy",
            enabled=False,
            ical_uid=ICAL_VYSTAVKA,
            event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
            rejected_at=date.today().isoformat(),
            rejected_start_at=self.start.isoformat(),
            **{mar.REJECTION_OWNER_FIELD: True},
        )
        items = [_snapshot_item(1, CAL_ANZHEE, "evt-anzhee-copy", self.start,
                                ical_uid=ICAL_VYSTAVKA)]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None}], items,
            watched_records=[rejected],
        )
        recs = _gcal_records(data)
        self.assertEqual(len(recs), 1, "поверх отказа завелась вторая запись")
        self.assertIs(recs[0]["enabled"], True)
        self.assertNotIn("rejected_at", recs[0], "метки отказа не сняты у ожившей записи")

    def test_never_on_twin_mutes_existing_series_record(self):
        # «Не предлагай эту серию» по копии обязано погасить ту запись, по
        # которой бот реально приходит, а не завести вторую «глушилку».
        twin = _rec_series(
            "anzhee-koordinaciya", CAL_ANZHEE, "recur-anzhee",
            ical_uid="recur@google.com",
            event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
        )
        items = [_snapshot_item(1, CAL_MAIN, "recur-main_1", self.start,
                                ical_uid="recur@google.com", recurring="recur-main")]
        result, data = _apply(
            [{"n": 1, "action": "ignore_forever", "room": None}], items,
            watched_records=[twin],
        )
        muted = [r for r in _gcal_records(data) if r["id"] == "anzhee-koordinaciya"][0]
        self.assertIs(muted["ignored"], True, "запись, по которой приходил бот, жива")
        self.assertIs(muted["enabled"], False)
        self.assertEqual(result["ignored_forever"], 1)
        # Ни одна из записанных не осталась включённой — бот не придёт ни по одной.
        self.assertEqual(
            [r["id"] for r in _gcal_records(data) if r.get("enabled", True)], [],
            "после «никогда» осталась включённая запись — бот всё равно придёт",
        )

    def test_once_yes_does_not_hijack_series_record(self):
        # Граница осознанная: запись «вся серия» и запись «только этот
        # экземпляр» — разные обещания владельцу, подменять одну другой нельзя.
        # Двойного бота на пересечении снимает схлопывание очереди в scheduler.
        series_rec = _rec_series(
            "anzhee-koordinaciya", CAL_ANZHEE, "recur-anzhee",
            ical_uid="recur@google.com",
            event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
        )
        items = [_snapshot_item(1, CAL_MAIN, "recur-main_1", self.start,
                                ical_uid="recur@google.com", recurring="recur-main")]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None}], items,
            watched_records=[series_rec],
        )
        kept = [r for r in _gcal_records(data) if r["id"] == "anzhee-koordinaciya"][0]
        self.assertTrue(kept.get("recurring_event_id"), "запись серии подменили разовой")
        self.assertIs(kept["enabled"], True)
        self.assertEqual(len(_gcal_records(data)), 2)

    def test_no_on_twin_disables_existing_record(self):
        # «Нет» по копии из второго календаря должно погасить запись-близнеца,
        # иначе бот всё равно придёт — по первой записи.
        twin = _rec_once(
            "anzhee-vystavka-st1-20260727", CAL_ANZHEE, "evt-anzhee-copy",
            ical_uid=ICAL_VYSTAVKA,
            event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
        )
        items = [_snapshot_item(1, CAL_MAIN, "evt-personal-copy", self.start,
                                ical_uid=ICAL_VYSTAVKA)]
        result, data = _apply(
            [{"n": 1, "action": "reject", "room": None}], items,
            watched_records=[twin],
        )
        twin_after = [r for r in _gcal_records(data)
                      if r["id"] == "anzhee-vystavka-st1-20260727"][0]
        self.assertIs(
            twin_after["enabled"], False,
            "«нет» по копии не погасило запись-близнеца — бот всё равно придёт",
        )
        self.assertEqual(
            [r["id"] for r in _gcal_records(data) if r.get("enabled", True)], [],
            "после «нет» осталась включённая запись — бот всё равно придёт",
        )
        self.assertEqual(result["rejected"], 1)


# ==========================================================================
# Победитель схлопывания закреплён между тиками
# ==========================================================================
class TestWinnerIsSticky(unittest.TestCase):
    """Схлопывание живёт дольше одного прогона планировщика.

    Барьеры ниже по течению (`runner._candidates`, `state_acquire`) ключуют
    `event_id`. Если в следующем тике очередь отдаст ДРУГОГО победителя (или
    выживет проигравший), барьер не сработает и на встречу приедет второй бот —
    к тому, который уже пишет.
    """

    def setUp(self):
        self.start = _in_minutes(10)   # старт близко: выбор уже заморожен
        self.winners = _fresh_winners_file()
        # `state.state_acquire` пишет в общий `.state.json` — event_id делаем
        # уникальным на тест, иначе барьер соседнего теста ловит этот.
        tag = self._testMethodName[-20:]
        eid_main, eid_anzhee = "evt-%s-p" % tag, "evt-%s-a" % tag
        self.evt_main = _event(eid_main, self.start, ical_uid=ICAL_VYSTAVKA)
        self.evt_anzhee = _event(eid_anzhee, self.start, ical_uid=ICAL_VYSTAVKA)
        self.rec_auto = _rec_once("oneoff-12345678", CAL_MAIN, eid_main)
        self.rec_owner = _rec_once("anzhee-vystavka-tick", CAL_ANZHEE, eid_anzhee)
        self.events = {CAL_MAIN: [self.evt_main], CAL_ANZHEE: [self.evt_anzhee]}

    def test_two_ticks_in_a_row_give_the_same_winner(self):
        recs = [self.rec_auto, self.rec_owner]
        _rc, q1, _l, _p = _run_scheduler(recs, self.events, winners_file=self.winners)
        # Выбор пережил прогон: он на диске, а не в памяти процесса.
        remembered = json.loads(self.winners.read_text(encoding="utf-8"))
        self.assertTrue(remembered, "выбор победителя не пережил тик")
        self.assertEqual(
            {v["event_id"] for v in remembered.values()}, {q1[0]["event_id"]},
            "в памяти о схлопывании не тот победитель: %s" % remembered,
        )
        _rc, q2, _l, _p = _run_scheduler(recs, self.events, winners_file=self.winners)
        self.assertEqual(len(q1), 1)
        self.assertEqual(len(q2), 1)
        self.assertEqual(
            (q1[0]["meeting_id"], q1[0]["event_id"]),
            (q2[0]["meeting_id"], q2[0]["event_id"]),
            "победитель схлопывания сменился между тиками — приедет второй бот",
        )

    def test_lost_winner_keeps_the_barrier_and_second_bot_does_not_start(self):
        # Тик 1: обе копии на месте, ведём по записи владельца.
        _rc, q1, _l, _p = _run_scheduler(
            [self.rec_auto, self.rec_owner], self.events, winners_file=self.winners)
        self.assertEqual(q1[0]["meeting_id"], "anzhee-vystavka-tick")
        # Бот стартовал по победителю — занял барьер.
        self.assertTrue(state.state_acquire(q1[0]["event_id"]))

        # Тик 2: календарь победителя не ответил, в очереди только проигравший.
        _rc, q2, logs, _p = _run_scheduler(
            [self.rec_auto], {CAL_MAIN: [self.evt_main]}, winners_file=self.winners)
        self.assertEqual(len(q2), 1)
        self.assertEqual(
            q2[0]["event_id"], q1[0]["event_id"],
            "выживший не унаследовал барьер победителя — на встречу приедет "
            "второй бот к тому, который уже пишет",
        )
        self.assertFalse(
            state.state_acquire(q2[0]["event_id"]),
            "барьер повторного старта не сработал — стартует второй бот",
        )
        self.assertTrue(any("второй бот" in m for m in logs),
                        "подмена барьера не залогирована:\n" + "\n".join(logs))

    def test_winner_that_disabled_itself_after_start_does_not_hand_over_the_meeting(self):
        # Победитель — разовая запись; после старта её гасит `_auto_disable_one_off`,
        # и в следующем тике из очереди она пропадает.
        _rc, q1, _l, _p = _run_scheduler(
            [self.rec_auto, self.rec_owner], self.events, winners_file=self.winners)
        self.assertTrue(state.state_acquire(q1[0]["event_id"]))
        gone = dict(self.rec_owner)
        gone["enabled"] = False          # именно так гасит себя разовая запись
        _rc, q2, _l, _p = _run_scheduler(
            [self.rec_auto, gone], self.events, winners_file=self.winners)
        self.assertEqual(len(q2), 1)
        self.assertFalse(
            state.state_acquire(q2[0]["event_id"]),
            "погасшая после старта запись отдала встречу второму боту",
        )

    def test_winner_survives_reordering_of_the_registry(self):
        # Обе записи автоимённые — выбирать не по чему, кроме порядка строк в
        # watched.yaml. Владелец переставил строки (или добавил запись выше) —
        # победитель не имеет права смениться: бот уже мог стартовать.
        tag = self._testMethodName[-20:]
        eid_a, eid_b = "evt-%s-x" % tag, "evt-%s-y" % tag
        recs = [
            _rec_once("oneoff-11111111", CAL_MAIN, eid_a),
            _rec_once("oneoff-22222222", CAL_ANZHEE, eid_b),
        ]
        events = {
            CAL_MAIN: [_event(eid_a, self.start, ical_uid=ICAL_VYSTAVKA)],
            CAL_ANZHEE: [_event(eid_b, self.start, ical_uid=ICAL_VYSTAVKA)],
        }
        _rc, q1, _l, _p = _run_scheduler(recs, events, winners_file=self.winners)
        self.assertTrue(state.state_acquire(q1[0]["event_id"]))
        _rc, q2, _l, _p = _run_scheduler(
            list(reversed(recs)), events, winners_file=self.winners)
        self.assertEqual(len(q2), 1)
        self.assertFalse(
            state.state_acquire(q2[0]["event_id"]),
            "перестановка строк в реестре сменила победителя — на встречу "
            "приедет второй бот",
        )

    def test_winner_is_still_reconsidered_long_before_the_start(self):
        # Заморозка нужна только у самого старта. За часы до него бот стартовать
        # не мог, и вернувшийся календарь владельца обязан снова победить —
        # иначе протокол уедет в чат автозаписи с её настройками.
        far = _soon(5)
        events_main = {CAL_MAIN: [_event("evt-far-personal", far, ical_uid="far@google.com")]}
        events_both = {
            CAL_MAIN: [_event("evt-far-personal", far, ical_uid="far@google.com")],
            CAL_ANZHEE: [_event("evt-far-anzhee", far, ical_uid="far@google.com")],
        }
        auto = _rec_once("oneoff-87654321", CAL_MAIN, "evt-far-personal")
        owner = _rec_once("anzhee-vystavka-far", CAL_ANZHEE, "evt-far-anzhee")
        _rc, q1, _l, _p = _run_scheduler([auto], events_main, winners_file=self.winners)
        self.assertEqual(q1[0]["meeting_id"], "oneoff-87654321")
        _rc, q2, _l, _p = _run_scheduler(
            [auto, owner], events_both, winners_file=self.winners)
        self.assertEqual(len(q2), 1)
        self.assertEqual(
            q2[0]["meeting_id"], "anzhee-vystavka-far",
            "за часы до старта победитель не пересмотрен — встречу ведёт "
            "автозапись без привязки чата и режима открытых вопросов",
        )


# ==========================================================================
# Страховка «время + URL»: чего она НЕ делает
# ==========================================================================
class TestSafetyNetNegatives(unittest.TestCase):
    """Единственный ключ, который работает на бою, — и его отрицательная сторона.

    `iCalUID` в снапшот вечернего блока пока не приезжает, поэтому решения
    принимает страховка. Если она бьёт шире, чем должна, встреча теряется молча.
    """

    def test_queue_safety_net_does_not_fire_without_the_same_time(self):
        # Одна комната, РАЗНОЕ время, точного ключа нет ни у кого.
        records = [
            _rec_once("koordinaciya-2", CAL_MAIN, "evt-koord-2"),
            _rec_once("vystavka-2", CAL_ANZHEE, "evt-vyst-2"),
        ]
        events = {
            CAL_MAIN: [_event("evt-koord-2", _soon(5), title="Координация")],
            CAL_ANZHEE: [_event("evt-vyst-2", _soon(9), title="Выставка")],
        }
        _rc, queue, _logs, _p = _run_scheduler(records, events)
        self.assertEqual(
            len(queue), 2,
            "страховка сработала без совпадения времени — вторая встреча "
            "выпала из очереди, бот на неё не придёт",
        )

    def test_queue_safety_net_does_not_fire_without_the_same_room(self):
        # Одно время, РАЗНЫЕ комнаты, точного ключа нет ни у кого.
        start = _soon(5)
        records = [
            _rec_once("koordinaciya-3", CAL_MAIN, "evt-koord-3"),
            _rec_once("vystavka-3", CAL_ANZHEE, "evt-vyst-3"),
        ]
        events = {
            CAL_MAIN: [_event("evt-koord-3", start, url=ROOM_URL, title="Координация")],
            CAL_ANZHEE: [_event("evt-vyst-3", start, url=OTHER_URL, title="Выставка")],
        }
        _rc, queue, _logs, _p = _run_scheduler(records, events)
        self.assertEqual(
            len(queue), 2,
            "страховка сработала без совпадения комнаты — две встречи в одну "
            "секунду в РАЗНЫХ комнатах схлопнулись в одну",
        )

    def test_registry_safety_net_does_not_fire_without_the_same_time(self):
        items = [
            _snapshot_item(1, CAL_MAIN, "evt-a1", _soon(20), title="Координация"),
            _snapshot_item(2, CAL_ANZHEE, "evt-b1", _soon(26), title="Выставка"),
        ]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        self.assertEqual(
            len(_gcal_records(data)), 2,
            "в одной комнате в разное время — две РАЗНЫЕ встречи; одна из них "
            "осталась без записи",
        )

    def test_registry_safety_net_does_not_fire_without_the_same_room(self):
        start = _soon(20)
        items = [
            _snapshot_item(1, CAL_MAIN, "evt-a2", start, url=ROOM_URL, title="Координация"),
            _snapshot_item(2, CAL_ANZHEE, "evt-b2", start, url=OTHER_URL, title="Выставка"),
        ]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        self.assertEqual(
            len(_gcal_records(data)), 2,
            "страховка сработала без совпадения комнаты — вторая встреча не "
            "записана, а владельцу сказано «это та же встреча»",
        )

    def test_registry_safety_net_needs_both_halves_of_the_key(self):
        # Ни времени, ни ссылки: сверять нечем — дубль не ищем вовсе.
        item = _snapshot_item(1, CAL_MAIN, "evt-a3", _soon(20), url=None)
        item["start_at"] = ""
        self.assertEqual(
            mar.find_duplicates_watched(
                item,
                {"watched": [_rec_once("x", CAL_ANZHEE, "evt-b3",
                                       event_start_at="", event_url="")]},
                series=False),
            [],
            "дубль «найден» по пустым приметам",
        )


# ==========================================================================
# Одна формула тождества на оба пути
# ==========================================================================
class TestOneFormulaForBothPaths(unittest.TestCase):
    """`lib/meeting_identity` — единственное место, где живёт «та же встреча».

    Пока формула была скопирована в scheduler и apply-reply, реализации
    разошлись и два дедупа давали противоположные ответы на одном случае.
    """

    def test_both_paths_call_the_same_function(self):
        self.assertIs(sched.compare, mid.compare)
        self.assertIs(mar.compare, mid.compare)
        self.assertIs(sched.fingerprint, mid.fingerprint)
        self.assertIs(mar.fingerprint, mid.fingerprint)

    def test_same_instant_in_different_offsets_is_one_meeting_in_queue(self):
        start_utc = _soon(5)
        start_dubai = start_utc.astimezone(timezone(timedelta(hours=4)))
        records = [
            _rec_once("oneoff-aabbccdd", CAL_MAIN, "evt-tz-a"),
            _rec_once("vystavka-tz", CAL_ANZHEE, "evt-tz-b"),
        ]
        events = {
            CAL_MAIN: [_event("evt-tz-a", start_utc)],
            CAL_ANZHEE: [_event("evt-tz-b", start_dubai)],
        }
        _rc, queue, _logs, _p = _run_scheduler(records, events)
        self.assertEqual(
            len(queue), 1,
            "один и тот же момент в разных офсетах («…+04:00» и «…Z») принят за "
            "две встречи — придут два бота",
        )

    def test_same_instant_in_different_offsets_is_one_meeting_in_registry(self):
        start_utc = _soon(20)
        start_dubai = start_utc.astimezone(timezone(timedelta(hours=4)))
        items = [
            _snapshot_item(1, CAL_MAIN, "evt-tz-c", start_utc),
            _snapshot_item(2, CAL_ANZHEE, "evt-tz-d", start_dubai),
        ]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        self.assertEqual(len(_gcal_records(data)), 1)

    def test_room_url_is_compared_without_slash_and_case(self):
        start = _soon(20)
        items = [
            _snapshot_item(1, CAL_MAIN, "evt-url-a", start, url=ROOM_URL),
            _snapshot_item(2, CAL_ANZHEE, "evt-url-b", start,
                           url="https://Telemost.Yandex.ru/j/11111111111111/"),
        ]
        _result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None},
             {"n": 2, "action": "accept_once", "room": None}],
            items,
        )
        self.assertEqual(
            len(_gcal_records(data)), 1,
            "та же комната с хвостовым слэшем/в другом регистре принята за другую",
        )

    def test_room_url_is_compared_without_slash_and_case_in_queue(self):
        start = _soon(5)
        records = [
            _rec_once("oneoff-ddccbbaa", CAL_MAIN, "evt-url-c"),
            _rec_once("vystavka-url", CAL_ANZHEE, "evt-url-d"),
        ]
        events = {
            CAL_MAIN: [_event("evt-url-c", start, url=ROOM_URL)],
            CAL_ANZHEE: [_event("evt-url-d", start,
                                url="https://Telemost.Yandex.ru/j/11111111111111/")],
        }
        _rc, queue, _logs, _p = _run_scheduler(records, events)
        self.assertEqual(len(queue), 1)

    def test_recurring_instances_are_never_collapsed_by_shared_ical(self):
        # Ключ `iCalUID` без времени старта выкосил бы всю неделю регулярной
        # встречи. В очереди сравнение всегда парное.
        a = mid.fingerprint(_soon(5).isoformat(), ROOM_URL, "recur@google.com")
        b = mid.fingerprint(_soon(29).isoformat(), ROOM_URL, "recur@google.com")
        self.assertEqual(mid.compare(a, b)[0], mid.MATCH_NONE)
        # А вот ЗАПИСИ о серии по общему ключу опознаются: копия серии из второго
        # календаря могла быть увидена в другой день.
        self.assertEqual(mid.compare(a, b, series_scope=True)[0], mid.MATCH_ICAL)


# ==========================================================================
# Сбой дедупа не должен уносить очередь
# ==========================================================================
class TestDedupFailOpen(unittest.TestCase):

    def test_broken_dedup_keeps_the_queue_as_is(self):
        start = _soon(5)
        records = [
            _rec_once("oneoff-eeeeeeee", CAL_MAIN, "evt-fo-a"),
            _rec_once("vystavka-fo", CAL_ANZHEE, "evt-fo-b"),
        ]
        events = {
            CAL_MAIN: [_event("evt-fo-a", start)],
            CAL_ANZHEE: [_event("evt-fo-b", start)],
        }

        def boom(*_a, **_k):
            raise RuntimeError("дедуп сломался")

        with mock.patch.object(sched, "compare", boom):
            rc, queue, logs, _p = _run_scheduler(records, events)
        self.assertEqual(rc, 0)
        self.assertEqual(
            len(queue), 2,
            "сбой дедупа унёс элементы очереди — бот не придёт НИ НА ОДНУ "
            "встречу, а это хуже, чем два бота",
        )
        self.assertTrue(
            any("не отработало" in m or "упал" in m for m in logs),
            "сбой дедупа не виден в логе:\n" + "\n".join(logs),
        )

    def test_garbage_in_queue_does_not_break_dedup(self):
        out = sched.dedupe_queue([
            {"meeting_id": "a", "event_id": "e-a", "start_at": _soon(5).isoformat(),
             "url": ROOM_URL},
            "мусор",
        ])
        self.assertEqual(len(out), 2)

    def test_dates_are_parsed_once_per_item(self):
        # Дедуп был квадратичен по РАЗБОРАМ дат: 1000 стартов — 2,6 с.
        # Считаем разборы, а не секунды: тест не должен зависеть от нагрузки.
        n = 200
        start = _soon(5)
        queue = [
            {"meeting_id": "m%d" % i, "event_id": "e%d" % i,
             "start_at": (start + timedelta(minutes=i)).isoformat(), "url": ROOM_URL}
            for i in range(n)
        ]
        calls = []
        real = mid.instant_key

        def counting(value):
            calls.append(value)
            return real(value)

        with mock.patch.object(mid, "instant_key", counting):
            out = sched.dedupe_queue(list(queue))
        self.assertEqual(len(out), n)
        self.assertLessEqual(
            len(calls), 2 * n,
            "дату каждого элемента разбирают заново на каждую пару — на боевом "
            "росте это секунды на ровном месте (разборов %d при %d элементах)"
            % (len(calls), n),
        )


# ==========================================================================
# Приметы: дописываем недостающее, чужого не трогаем
# ==========================================================================
class TestMergeFingerprint(unittest.TestCase):

    def setUp(self):
        self.start = _soon(20)
        self.item = _snapshot_item(1, CAL_MAIN, "evt-m", self.start,
                                   ical_uid=ICAL_VYSTAVKA)

    def test_fills_only_empty_fields(self):
        rec = _rec_once("x", CAL_ANZHEE, "evt-other")
        mar._merge_fingerprint(rec, self.item, proof=mid.MATCH_ID)
        self.assertEqual(rec["ical_uid"], ICAL_VYSTAVKA)
        self.assertEqual(rec["event_start_at"], self.start.isoformat())
        self.assertEqual(rec["event_url"], ROOM_URL)

    def test_never_overwrites_what_is_already_there(self):
        rec = _rec_once("x", CAL_ANZHEE, "evt-other",
                        ical_uid="ego@google.com",
                        event_start_at="2020-01-01T00:00:00+00:00",
                        event_url=OTHER_URL)
        mar._merge_fingerprint(rec, self.item, proof=mid.MATCH_ID)
        self.assertEqual(rec["ical_uid"], "ego@google.com", "перетёрли чужую примету")
        self.assertEqual(rec["event_start_at"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(rec["event_url"], OTHER_URL)

    def test_weak_match_does_not_write_the_exact_key(self):
        # Тождество доказано только комнатой и секундой. Вписать в запись точный
        # ключ ЧУЖОГО события нельзя: дальше он считался бы доказательством, а
        # откатить нечем — в YAML лежит правдоподобное значение.
        rec = _rec_once("x", CAL_ANZHEE, "evt-other")
        mar._merge_fingerprint(rec, self.item, proof=mid.MATCH_ROOM)
        self.assertIsNone(rec.get("ical_uid"))
        self.assertEqual(rec["event_start_at"], self.start.isoformat())

    def test_no_heals_the_record_it_touched(self):
        # Отчёт фазы обещает «реестр лечится сам, когда владелец касается
        # записи». Для «нет» это должно быть правдой, а не только для «да».
        rec = _rec_once("oneoff-ef50c3ab", CAL_MAIN, "evt-heal")
        item = _snapshot_item(1, CAL_MAIN, "evt-heal", self.start,
                              ical_uid=ICAL_VYSTAVKA)
        with mock.patch.object(mar, "make_record_id", lambda *a, **k: "oneoff-ef50c3ab"):
            _result, data = _apply(
                [{"n": 1, "action": "reject", "room": None}], [item],
                watched_records=[rec],
            )
        healed = _gcal_records(data)[0]
        self.assertEqual(healed.get("ical_uid"), ICAL_VYSTAVKA)
        self.assertEqual(healed.get("event_url"), ROOM_URL)


# ==========================================================================
# Что владелец видит и о чём бот его не спросит второй раз
# ==========================================================================
class TestOwnerSeesWhatWasTouched(unittest.TestCase):
    """«Нет» и «никогда» по копии гасят ЧУЖУЮ живую запись. Молча — нельзя."""

    def setUp(self):
        self.start = _soon(20)
        self.twin = _rec_once(
            "anzhee-vystavka-st1", CAL_ANZHEE, "evt-anzhee-copy",
            ical_uid=ICAL_VYSTAVKA,
            event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
            telegram_chat_id=-5589869859,
        )
        self.copy = _snapshot_item(1, CAL_MAIN, "evt-personal-copy", self.start,
                                   ical_uid=ICAL_VYSTAVKA)

    def test_no_names_the_record_it_disabled(self):
        result, _data = _apply(
            [{"n": 1, "action": "reject", "room": None}], [self.copy],
            watched_records=[self.twin],
        )
        self.assertTrue(
            any("anzhee-vystavka-st1" in line for line in result["summary_lines"]),
            "бот выключил живую запись владельца и не сказал какую: %s"
            % result["summary_lines"],
        )

    def test_never_names_the_record_it_disabled(self):
        result, _data = _apply(
            [{"n": 1, "action": "ignore_forever", "room": None}], [self.copy],
            watched_records=[self.twin],
        )
        self.assertTrue(
            any("anzhee-vystavka-st1" in line for line in result["summary_lines"]),
            "бот заглушил живую запись владельца и не сказал какую: %s"
            % result["summary_lines"],
        )

    def test_no_on_a_copy_is_not_asked_again_tomorrow(self):
        # Обещание фазы 4: «больше про эту встречу не спрошу». Вечерний блок
        # ключует (calendar_id, event_id) — про запись-близнеца он не знает,
        # поэтому след нужен под ключом САМОЙ копии.
        _result, data = _apply(
            [{"n": 1, "action": "reject", "room": None}], [self.copy],
            watched_records=[self.twin],
        )
        self.assertFalse(
            _offered_again(data, CAL_MAIN, "evt-personal-copy"),
            "после «нет» по копии вечерний блок предложит ту же встречу снова — "
            "сразу после обещания «больше про эту встречу не спрошу»",
        )

    def test_never_on_a_copy_is_not_asked_again_tomorrow(self):
        _result, data = _apply(
            [{"n": 1, "action": "ignore_forever", "room": None}], [self.copy],
            watched_records=[self.twin],
        )
        self.assertFalse(
            _offered_again(data, CAL_MAIN, "evt-personal-copy"),
            "после «никогда» по копии вечерний блок предложит её снова",
        )

    def test_disabled_owner_record_is_not_marked_for_deletion(self):
        # Гасим чужую запись — но метку смерти на неё не ставим: через 30 дней
        # ретеншн унёс бы её вместе с привязкой чата и режимом открытых вопросов.
        _result, data = _apply(
            [{"n": 1, "action": "reject", "room": None}], [self.copy],
            watched_records=[self.twin],
        )
        twin_after = [r for r in _gcal_records(data)
                      if r["id"] == "anzhee-vystavka-st1"][0]
        self.assertNotIn(mar.REJECTION_OWNER_FIELD, twin_after)
        self.assertEqual(twin_after.get("telegram_chat_id"), -5589869859)

    def test_no_disables_every_twin_not_just_the_first(self):
        # В боевом реестре на одну встречу лежат ДВА дубля. Отказ, погасивший
        # один, исполнен наполовину: бот придёт по второму.
        second = _rec_once(
            "oneoff-ef50c3ab", CAL_MAIN, "evt-third-copy",
            ical_uid=ICAL_VYSTAVKA,
            event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
        )
        _result, data = _apply(
            [{"n": 1, "action": "reject", "room": None}], [self.copy],
            watched_records=[self.twin, second],
        )
        self.assertEqual(
            [r["id"] for r in _gcal_records(data) if r.get("enabled", True)], [],
            "после «нет» осталась включённая запись-дубль — бот всё равно придёт",
        )

    def test_yes_on_a_copy_warns_when_the_calendar_shows_two_events(self):
        # Комната и секунда те же, точные ключи РАЗНЫЕ. Схлопнуть обязаны
        # (иначе два бота), но владелец должен узнать, что в календаре два
        # разных события: возможно, он продублировал его руками.
        other = _rec_once(
            "anzhee-koordinaciya", CAL_ANZHEE, "evt-koord-copy",
            ical_uid="koord@google.com",
            event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
        )
        result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": None}], [self.copy],
            watched_records=[other],
        )
        self.assertEqual(len(_gcal_records(data)), 1)
        joined = " | ".join(result["summary_lines"])
        self.assertIn("anzhee-koordinaciya", joined)
        self.assertIn("РАЗНЫХ события", joined)


# ==========================================================================
# Встреча без ссылки в событии: комнату назвал владелец
# ==========================================================================
class TestRoomNamedInWords(unittest.TestCase):

    def test_meeting_without_link_in_event_is_still_deduplicated(self):
        # В событии ссылки нет, владелец называет комнату словами («да, @t11»).
        # Раньше страховка молчала (URL пуст) — и на встречу заводилась вторая
        # запись. Комнату разрешаем в URL и сверяем по нему.
        start = _soon(20)
        twin = _rec_once(
            "anzhee-vystavka-room", CAL_ANZHEE, "evt-anzhee-nolink",
            room="@t11",
            event_start_at=start.isoformat(),
        )
        item = _snapshot_item(1, CAL_MAIN, "evt-personal-nolink", start, url=None)
        result, data = _apply(
            [{"n": 1, "action": "accept_once", "room": "@t11"}], [item],
            watched_records=[twin], rooms=ROOMS,
        )
        self.assertEqual(
            len(_gcal_records(data)), 1,
            "встреча без ссылки в событии завела вторую запись: %s"
            % [r["id"] for r in _gcal_records(data)],
        )
        self.assertTrue(any("та же встреча" in line for line in result["summary_lines"]))

    def test_different_rooms_named_in_words_are_not_collapsed(self):
        start = _soon(20)
        rooms = {"rooms": [{"name": "t11", "url": ROOM_URL},
                           {"name": "t22", "url": OTHER_URL}]}
        twin = _rec_once(
            "anzhee-vystavka-room2", CAL_ANZHEE, "evt-anzhee-nolink2",
            room="@t22",
            event_start_at=start.isoformat(),
        )
        item = _snapshot_item(2, CAL_MAIN, "evt-personal-nolink2", start, url=None)
        _result, data = _apply(
            [{"n": 2, "action": "accept_once", "room": "@t11"}], [item],
            watched_records=[twin], rooms=rooms,
        )
        self.assertEqual(len(_gcal_records(data)), 2)


# ==========================================================================
# ШВЫ Ф4↔Ф5 (сквозная проверка): «нет»→«да», одно правило победителя,
# «нет» по экземпляру ведомой серии
# ==========================================================================
class TestNoThenYesRevivesTheConfiguredRecord(unittest.TestCase):
    """🔴 Владелец сказал «нет», через пять минут передумал и сказал «да».

    Поиск записи по id стоял ДО дедупа, а у отказной заглушки id считается из
    ТОГО ЖЕ события, что и у новой записи, — значит по id она находилась всегда
    и дедуп (который один и знает, что оживлять надо запись с привязкой чата и
    режимом) не звался вовсе. Оживала заглушка: встреча шла серией без чата
    (протокол уезжал владельцу в личку) и с дефолтным режимом открытых вопросов,
    настроенная запись владельца оставалась выключенной навсегда, а владельцу
    говорилось только «уже была в реестре, включил».
    """

    def setUp(self):
        self.start = _soon(20)
        self.owner_rec = _rec_once(
            "anzhee-vystavka-st1", CAL_ANZHEE, "evt-anzhee-copy",
            ical_uid=ICAL_VYSTAVKA,
            event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
            telegram_chat_id=-5589869859,
            open_questions_mode="carry",
        )
        self.copy = _snapshot_item(1, CAL_MAIN, "evt-personal-copy", self.start,
                                   ical_uid=ICAL_VYSTAVKA)

    def _no_then_yes(self):
        _res, data = _apply([{"n": 1, "action": "reject", "room": None}], [self.copy],
                            watched_records=[self.owner_rec])
        res2, data2 = _apply([{"n": 1, "action": "accept_once", "room": None}],
                             [self.copy], watched_records=data["watched"])
        return res2, data2

    def _live(self, data):
        return [r for r in _gcal_records(data) if r.get("enabled", True)]

    def test_the_record_that_wakes_up_is_the_owners(self):
        _res, data = self._no_then_yes()
        live = self._live(data)
        self.assertEqual([r["id"] for r in live], ["anzhee-vystavka-st1"],
                         "ожила не та запись: %s" % [r["id"] for r in _gcal_records(data)])

    def test_the_meeting_keeps_its_chat_and_mode(self):
        # Ради чего всё и делалось: на записи владельца висят привязка чата
        # (протокол уходит в чат серии, а не в личку) и режим открытых вопросов.
        _res, data = self._no_then_yes()
        live = self._live(data)[0]
        self.assertEqual(live.get("telegram_chat_id"), -5589869859)
        self.assertEqual(live.get("open_questions_mode"), "carry")

    def test_owner_is_told_which_record_woke_up(self):
        res, _data = self._no_then_yes()
        said = " ".join(res["summary_lines"])
        self.assertIn("anzhee-vystavka-st1", said, said)
        self.assertIn("прошлое «нет»", said, said)

    def test_the_rejection_stub_stays_dead_and_stops_being_a_rejection(self):
        # Заглушка остаётся в реестре (иначе вечерний блок спросит про ту же
        # копию завтра), но отказом быть перестаёт — иначе она продолжала бы
        # гасить старт в планировщике.
        _res, data = self._no_then_yes()
        stub = [r for r in _gcal_records(data) if r["id"] != "anzhee-vystavka-st1"]
        self.assertEqual(len(stub), 1)
        self.assertFalse(stub[0].get("enabled", True))
        self.assertFalse(stub[0].get(mar.REJECTION_OWNER_FIELD))
        self.assertIsNone(stub[0].get("rejected_start_at"))
        self.assertFalse(_offered_again(data, CAL_MAIN, "evt-personal-copy"))

    def test_bot_really_comes_after_the_owner_changed_his_mind(self):
        # Сквозной рубеж: после «нет»→«да» старт обязан появиться в очереди
        # (след отказа больше не гасит встречу).
        _res, data = self._no_then_yes()
        events = {CAL_ANZHEE: [_event("evt-anzhee-copy", self.start,
                                      ical_uid=ICAL_VYSTAVKA)]}
        _rc, queue, _logs, _pushes = _run_scheduler(data["watched"], events)
        self.assertEqual(len(queue), 1, queue)
        self.assertEqual(queue[0]["meeting_id"], "anzhee-vystavka-st1")

    def test_plain_yes_after_no_without_any_twin_is_unchanged(self):
        # Обычный случай (близнеца нет) не тронут: оживает та же запись, что и
        # раньше, и строка владельцу прежняя.
        res, data = _apply([{"n": 1, "action": "reject", "room": None}], [self.copy])
        res2, data2 = _apply([{"n": 1, "action": "accept_once", "room": None}],
                             [self.copy], watched_records=data["watched"])
        self.assertEqual(len(_gcal_records(data2)), 1)
        self.assertTrue(_gcal_records(data2)[0].get("enabled"))
        self.assertTrue(any("уже была в реестре, включил" in line
                            for line in res2["summary_lines"]), res2["summary_lines"])


class TestOneRuleForTheWinner(unittest.TestCase):
    """🟡 «Кто победил в дубле» считался ДВУМЯ правилами.

    Планировщик брал запись с человеческим именем (на ней чат, разметка и режим),
    а apply-reply среди двух ЖИВЫХ копий — просто первую в файле. Владельцу
    называлась одна запись, встречу вёл бот по другой, приметы дописывались не
    туда. Правило теперь одно — `meeting_identity.is_owner_named`.
    """

    def setUp(self):
        self.start = _soon(20)
        self.bare = _rec_once(
            "oneoff-1a2b3c4d", CAL_MAIN, "evt-bare",
            ical_uid=ICAL_VYSTAVKA, event_start_at=self.start.isoformat(),
            event_url=ROOM_URL,
        )
        self.owner_rec = _rec_once(
            "anzhee-vystavka-st1", CAL_ANZHEE, "evt-anzhee-copy",
            ical_uid=ICAL_VYSTAVKA, event_start_at=self.start.isoformat(),
            event_url=ROOM_URL, telegram_chat_id=-5589869859,
            open_questions_mode="carry",
        )
        # Голая автозапись стоит в файле ПЕРВОЙ — прежнее правило брало её.
        self.watched = [self.bare, self.owner_rec]
        self.item = _snapshot_item(1, "third@example.com", "evt-third", self.start,
                                   ical_uid=ICAL_VYSTAVKA)

    def test_registry_and_scheduler_name_the_same_record(self):
        res, data = _apply([{"n": 1, "action": "accept_once", "room": None}],
                           [self.item], watched_records=self.watched)
        said = " ".join(res["summary_lines"])
        self.assertIn("anzhee-vystavka-st1", said, said)
        self.assertNotIn("oneoff-1a2b3c4d", said, said)
        # И планировщик ведёт встречу по ней же.
        events = {
            CAL_MAIN: [_event("evt-bare", self.start, ical_uid=ICAL_VYSTAVKA)],
            CAL_ANZHEE: [_event("evt-anzhee-copy", self.start, ical_uid=ICAL_VYSTAVKA)],
        }
        _rc, queue, _logs, _pushes = _run_scheduler(data["watched"], events)
        self.assertEqual(len(queue), 1, queue)
        self.assertEqual(queue[0]["meeting_id"], "anzhee-vystavka-st1")

    def test_fingerprints_are_healed_on_the_record_that_leads(self):
        # Приметы третьего календаря дописываются в запись, которая реально
        # ведёт встречу, а не в проигравшую автозапись.
        item = _snapshot_item(1, "third@example.com", "evt-third", self.start,
                              ical_uid=ICAL_VYSTAVKA, url=OTHER_URL)
        bare = dict(self.bare); bare.pop("event_url")
        owner = dict(self.owner_rec); owner.pop("event_url")
        _res, data = _apply([{"n": 1, "action": "accept_once", "room": None}], [item],
                            watched_records=[bare, owner])
        by_id = {r["id"]: r for r in _gcal_records(data)}
        self.assertEqual(by_id["anzhee-vystavka-st1"].get("event_url"), OTHER_URL)
        self.assertIsNone(by_id["oneoff-1a2b3c4d"].get("event_url"))

    def test_the_rule_itself_is_shared(self):
        self.assertTrue(mid.is_owner_named("anzhee-vystavka-st1"))
        self.assertFalse(mid.is_owner_named("oneoff-22a5d597"))
        self.assertFalse(mid.is_owner_named("series-22a5d597"))
        self.assertIs(sched.AUTO_MEETING_ID_RE, mid.AUTO_RECORD_ID_RE)
        self.assertTrue(sched._is_owner_named({"meeting_id": "anzhee-vystavka-st1"}))

    def test_a_live_record_still_beats_a_dead_one(self):
        # Порядок ступеней не перевернулся: живая запись важнее имени.
        dead_named = dict(self.owner_rec); dead_named["enabled"] = False
        _res, data = _apply([{"n": 1, "action": "accept_once", "room": None}],
                            [self.item], watched_records=[dead_named, self.bare])
        live = [r["id"] for r in _gcal_records(data) if r.get("enabled", True)]
        self.assertEqual(live, ["oneoff-1a2b3c4d"], live)


class TestNoOnAnInstanceOfAWatchedSeries(unittest.TestCase):
    """🟡 «Нет» по копии встречи, которую приносит ВЕДОМАЯ СЕРИЯ.

    Отказ ищет близнецов только среди разовых записей: «серия» и «экземпляр» —
    разные обещания владельцу, и гасить всю серию из-за одного «нет» нельзя. Но
    и обещание «не записываю, больше про эту встречу не спрошу» надо исполнить:
    раньше бот всё равно приходил — по записи серии.
    """

    def setUp(self):
        self.start = _soon(5)
        self.other_start = _soon(29)
        self.series_rec = _rec_series(
            "anzhee-direktorat", CAL_ANZHEE, "rec-direktorat",
            ical_uid=ICAL_VYSTAVKA, event_start_at=self.start.isoformat(),
            event_url=ROOM_URL, telegram_chat_id=-5589869859,
        )
        self.copy = _snapshot_item(1, CAL_MAIN, "evt-personal-copy", self.start,
                                   ical_uid=ICAL_VYSTAVKA, recurring=None)

    def _reject(self):
        return _apply([{"n": 1, "action": "reject", "room": None}], [self.copy],
                      watched_records=[self.series_rec])

    def test_the_series_itself_is_not_switched_off(self):
        _res, data = self._reject()
        series = [r for r in _gcal_records(data) if r["id"] == "anzhee-direktorat"][0]
        self.assertTrue(series.get("enabled", True))
        self.assertFalse(series.get("ignored", False))

    def test_the_bot_does_not_come_to_that_meeting(self):
        # 🔴 Ядро находки: обещание «не записываю» обязано исполниться делом.
        _res, data = self._reject()
        events = {CAL_ANZHEE: [
            _event("inst-1", self.start, ical_uid=ICAL_VYSTAVKA,
                   recurring="rec-direktorat"),
            _event("inst-2", self.other_start, ical_uid=ICAL_VYSTAVKA,
                   recurring="rec-direktorat"),
        ]}
        _rc, queue, logs, _pushes = _run_scheduler(data["watched"], events)
        starts = [it["start_at"] for it in queue]
        self.assertEqual(len(queue), 1, starts)
        self.assertEqual(queue[0]["start_at"], self.other_start.isoformat(),
                         "погашен не тот старт: %s" % starts)
        self.assertTrue(any("сказал «нет»" in m for m in logs), logs[-5:])

    def test_owner_is_told_the_series_stays_on(self):
        res, _data = self._reject()
        said = " ".join(res["summary_lines"])
        self.assertIn("anzhee-direktorat", said, said)
        self.assertIn("не приду", said, said)

    def test_a_series_without_this_meeting_is_never_skipped(self):
        # Сторож обратной стороны: отказ гасит РОВНО свой старт. Другая серия в
        # той же комнате в другое время идёт как шла.
        _res, data = self._reject()
        other = _rec_series("mpervyi-koord", CAL_ANZHEE, "rec-other")
        events = {CAL_ANZHEE: [
            _event("inst-other", self.other_start, ical_uid="other@google.com",
                   recurring="rec-other"),
        ]}
        _rc, queue, _logs, _pushes = _run_scheduler(
            data["watched"] + [other], events)
        self.assertEqual([it["meeting_id"] for it in queue], ["mpervyi-koord"])

    def test_a_broken_rejection_record_does_not_eat_the_queue(self):
        # Fail-open: мусор в приметах отказа не имеет права оставить бота без
        # стартов («бот не пришёл ни на одну встречу» — катастрофа).
        broken = {"id": "oneoff-deadbeef", "series": "oneoff-deadbeef",
                  "type": "google-calendar", "calendar_id": CAL_MAIN,
                  "event_id": "evt-x", "enabled": False,
                  "created_by_rejection": True, "event_start_at": "не дата"}
        events = {CAL_ANZHEE: [
            _event("inst-1", self.start, ical_uid=ICAL_VYSTAVKA,
                   recurring="rec-direktorat"),
        ]}
        _rc, queue, _logs, _pushes = _run_scheduler([self.series_rec, broken], events)
        self.assertEqual(len(queue), 1, queue)


if __name__ == "__main__":
    unittest.main(verbosity=2)
