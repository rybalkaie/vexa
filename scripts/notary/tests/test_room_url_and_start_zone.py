"""Две находки проверки 04.10.2026 (бэклог нотариуса; решение владельца 06.10 — чинить).

№5 — ссылки комнат сравнивались без отрезания хвоста `?…`/`#…`, и каждое место
резало своё. Теперь какая это комната, решает одна формула
`telemost_url.room_url_key`, и через неё идут все места сравнения: склейка дублей и
«нет» владельца (`meeting_identity.url_key`), память дублей планировщика, ключ наряда
и метка контейнера (`orders.room_key`), счёт слотов (`slots`), сверка очереди с
реестром нарядов (`runner._outdated_portal_row`).

№4 — портал шлёт старт в UTC, календарь — в своём поясе (+03). Очередь сортировалась
по сырой строке: наряд вставал раньше серии владельца с тем же моментом старта и
выигрывал склейку дубля — протокол уходил не в ту серию. Теперь сортировка — по
моменту (`meeting_identity.start_sort_key`: очередь, реестр нарядов, наряды у
старта), а выбор победителя склейки от порядка не зависит (`scheduler._pick_winner`).

Что доказывается (каждая проба проверена порчей: вернуть прежнюю формулу или
сортировку по строке — проба красная):

* `TestOneRoomFormula` — варианты одной ссылки — одна комната во ВСЕХ местах
  сравнения, разные комнаты — разные; хэш комнаты уже записанных нарядов прежний.
* `TestOwnerNoHoldsTailedLink` — ГЛАВНАЯ проба №5: «нет» владельца держит ссылку с
  хвостом — у планировщика (наряд и копия из календаря) и у runner'а у старта.
* `TestTailedTwinIsOneMeeting` — дубль с хвостом склеивается, слот занимает один;
  старт, ждущий слота, не выпадает из ожидания от новой метки в ссылке.
* `TestPortalUtcTwinGoesToOwnerSeries` — ГЛАВНАЯ проба №4: наряд в UTC и серия
  владельца в +03 на одну комнату и секунду — один бот, в серии владельца, связь
  с нарядом цела.
* `TestOrderByMoment` — раньше начинается — раньше стоит; среди нарядов у старта
  раньше получает слот (строки очереди планировщика идут раньше нарядов у старта).
* `TestWinnerIgnoresOrder` — пару «наряд + запись владельца» решает род записи, а не
  её место в группе.

Цикл из 5 ходов по правке (06.10.2026) добавил:

* `TestCrookedDateKeepsTheRegistry` — время, которое не переводится в UTC, уходит в
  конец, а не роняет сортировку реестра нарядов и очереди (раньше сортировали
  строку, и она не падала).
* `TestSeriesYieldsToOrderByMarkup` — серию владельца, размеченную другой
  компанией, чем наряд, или приватную склейка не ведёт: протокол и память серии не
  уходят по маршруту наряда (R12.12, «приватное — не фон»). Своя компания — ведёт серия.
* `TestMergedMeetingKeepsOrderData` — у склеенной встречи участники из карточки
  портала доезжают до бота, а «Скачать PDF» находит протокол в папке серии.

Изоляция — стенд Ф10b (`test_orders_ph10b._Base`): временный реестр, Google подменён.
"""
from __future__ import annotations

import hashlib
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

import notary.runner as runner  # noqa: E402
import notary.scheduler as scheduler  # noqa: E402
from notary.lib import meeting_identity as mid  # noqa: E402
from notary.lib import orders as orders_lib  # noqa: E402
from notary.lib import portal_queue, slots, state as state_lib, telemost_url, waitlist  # noqa: E402
from notary.tests.test_orders_ph10b import ROOM, ROOM_2, _Base, _iso  # noqa: E402

#: Пояс календаря владельца в находке №4.
MSK = timezone(timedelta(hours=3))

#: Личная серия владельца с человеческим именем — из календаря Google.
OWNER_SERIES = {
    "id": "anzhee-direktorat", "series": "anzhee-direktorat", "type": "google-calendar",
    "calendar_id": "primary", "recurring_event_id": "dir-series", "enabled": True,
}


def _minute() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def _gcal_event(event_id: str, start: datetime, url: str) -> Dict[str, Any]:
    """Экземпляр серии в той форме, в какой его отдаёт Google: старт в поясе +03."""
    return {
        "id": event_id,
        "status": "confirmed",
        "summary": "Директорат",
        "recurringEventId": OWNER_SERIES["recurring_event_id"],
        "start": {"dateTime": start.astimezone(MSK).isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=60)).astimezone(MSK).isoformat()},
        "conferenceData": {"entryPoints": [{"uri": url}]},
    }


class _Stand(_Base):
    """Стенд Ф10b + календарь Google и наряд со стартом в заданном поясе."""

    def run_scheduler_with_calendar(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Настоящий прогон планировщика; Google подменён — события как из API."""

        def fake_list_instances(creds, calendar_id, recurring_event_id, time_min, time_max):
            return [e for e in events if e.get("recurringEventId") == recurring_event_id], creds

        def fake_get_event(creds, calendar_id, event_id):
            return next((e for e in events if e.get("id") == event_id), None), creds

        with mock.patch.object(scheduler, "load_credentials", lambda: object()), \
             mock.patch.object(scheduler, "list_instances", fake_list_instances), \
             mock.patch.object(scheduler, "get_event", fake_get_event):
            rc = scheduler.main()
        self.assertEqual(rc, 0)
        return state_lib.read_queue()

    def put_order_in_zone(
        self,
        meeting_id: str,
        start: datetime,
        *,
        zone: timezone,
        room: str = ROOM,
        series: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Наряд, присланный со стартом в поясе `zone`: реестр хранит пояс как прислан."""
        payload = {
            "meeting_id": meeting_id,
            "company": "anzhee",
            "series_title": "Координация продаж",
            "room_url": room,
            "start_at": start.astimezone(zone).isoformat(),
            "duration_minutes": 60,
            "expected_participants": [],
            "owner": {"portal_user_id": "42"},
        }
        if series:
            payload["series"] = series
        clean, errors = orders_lib.validate_order_payload(
            payload, now=now or datetime.now(timezone.utc)
        )
        self.assertEqual(errors, [], "фикстура наряда невалидна: %s" % errors)
        order, _ = orders_lib.put_order(clean, now=now)
        return order


def _rejection(start: datetime, url: str) -> Dict[str, Any]:
    """Запись-отказ: владелец сказал «нет» встрече (ISS-53), приметы — из календаря."""
    return {
        "id": "oneoff-0a1b2c3d", "series": "oneoff-0a1b2c3d", "type": "one-off",
        "created_by_rejection": True, "enabled": False,
        "event_start_at": start.astimezone(MSK).isoformat(), "event_url": url,
    }


# ---------------------------------------------------------------------------
# №5 — одна формула комнаты
# ---------------------------------------------------------------------------


class TestOneRoomFormula(_Stand):
    """Какая это комната — одна формула, и все места сравнения отвечают одинаково."""

    SAME_ROOM = (
        ROOM,
        ROOM + "/",
        ROOM + "?utm_source=portal",
        ROOM + "#join",
        ROOM + "/?utm_source=calendar&utm_medium=mail#x",
        "  " + ROOM + "  ",
        ROOM.replace("https://telemost.yandex.ru", "https://Telemost.Yandex.RU"),
        ROOM.replace("https://", "http://"),
        ROOM.replace("telemost.yandex.ru", "telemost.yandex.ru:443"),
        ROOM.replace("telemost.yandex.ru", "telemost.yandex.ru."),
    )
    OTHER_ROOMS = (
        ROOM_2,
        ROOM.replace("telemost.yandex.ru", "telemost.360.yandex.ru"),
        ROOM[:-1],
        ROOM + "1",
    )

    def test_variants_of_one_link_are_one_room(self):
        for url in self.SAME_ROOM:
            with self.subTest(url=url):
                self.assertEqual(
                    telemost_url.room_url_key(url), "telemost.yandex.ru/j/11111111111111"
                )

    def test_other_rooms_stay_other(self):
        base = telemost_url.room_url_key(ROOM)
        for url in self.OTHER_ROOMS:
            with self.subTest(url=url):
                self.assertNotEqual(telemost_url.room_url_key(url), base)

    def test_nothing_to_compare(self):
        for junk in (None, "", "   ", "?utm=1", "#x", "https://", 42, "https://[::1"):
            with self.subTest(junk=junk):
                self.assertIsNone(telemost_url.room_url_key(junk))

    def test_every_comparison_place_agrees(self):
        """Склейка и «нет» владельца, память дублей, ключ наряда и метка контейнера,
        слоты, сверка очереди с реестром — на одной паре ссылок один ответ."""
        start = _minute() + timedelta(hours=1)
        order, _ = self.put_order("mtg-u001", start, room=ROOM)
        live = portal_queue.queue_item(order)

        def answers(url: str) -> Dict[str, Any]:
            return {
                "url_key": mid.url_key(url),
                "memory": scheduler._memory_keys(mid.fingerprint(_iso(start), url, None)),
                "room_key": orders_lib.room_key(url),
                "label": runner._room_label_value(url),
                "slot": slots._slot_key(start, url),
                "outdated": runner._outdated_portal_row(
                    dict(live, url=url), {str(live["order_id"]): live}
                ),
            }

        base = answers(ROOM)
        self.assertFalse(base["outdated"])
        for url in self.SAME_ROOM:
            with self.subTest(url=url):
                self.assertEqual(answers(url), base)
        for url in self.OTHER_ROOMS:
            got = answers(url)
            for place in ("url_key", "memory", "room_key", "label", "slot"):
                with self.subTest(url=url, place=place):
                    self.assertNotEqual(got[place], base[place])
            with self.subTest(url=url, place="outdated"):
                self.assertTrue(got["outdated"], "другая комната у наряда — это перенос")

    def test_key_of_orders_already_on_disk_is_unchanged(self):
        """Хэш комнаты лежит в реестре нарядов и в метках живых контейнеров."""
        expected = hashlib.sha256(b"telemost.yandex.ru/j/11111111111111").hexdigest()[:16]
        self.assertEqual(orders_lib.room_key(ROOM), expected)
        self.assertEqual(orders_lib.room_key(ROOM + "?utm_source=portal"), expected)


class TestOwnerNoHoldsTailedLink(_Stand):
    """ГЛАВНАЯ проба №5: «нет» владельца держит и ссылку с хвостом."""

    def test_planner_drops_portal_order_with_tail(self):
        start = _minute() + timedelta(hours=3)
        self.write_watched([_rejection(start, ROOM)])
        self.put_order("mtg-n501", start, room=ROOM + "?utm_source=portal#join", series="portal-n501")
        self.assertEqual(
            self.run_scheduler(), [],
            "владелец сказал «нет» этой встрече — метка в ссылке его не обходит",
        )

    def test_planner_drops_when_the_tail_is_in_the_owner_record(self):
        start = _minute() + timedelta(hours=3)
        self.write_watched([_rejection(start, ROOM + "/?from=calendar")])
        self.put_order("mtg-n502", start, room=ROOM, series="portal-n502")
        self.assertEqual(self.run_scheduler(), [])

    def test_runner_at_the_start_drops_it_too(self):
        t0 = _minute()
        start = t0 + timedelta(minutes=1)
        self.write_watched([_rejection(start, ROOM)])
        self.queue_of([])
        self.put_order("mtg-n503", start, room=ROOM + "?utm_source=portal", series="portal-n503", now=t0)
        self.assertEqual(
            self.tick(t0 + timedelta(minutes=1)), [], "«нет» владельца действует и у самого старта",
        )

    def test_calendar_copy_with_tail_is_dropped(self):
        """Та же встреча — экземпляром живой серии из второго календаря (ISS-53)."""
        start = _minute() + timedelta(hours=3)
        self.write_watched([dict(OWNER_SERIES, calendar_id="anzhee@example.com"), _rejection(start, ROOM)])
        queue = self.run_scheduler_with_calendar(
            [_gcal_event("dir-1", start, ROOM + "?utm_source=calendar")]
        )
        self.assertEqual(queue, [], "«нет» сказано про эту встречу — копия с меткой её не возвращает")


class TestTailedTwinIsOneMeeting(_Stand):
    """Дубль с хвостом в ссылке — одна встреча: один бот, один слот."""

    def test_portal_twin_with_tail_collapses_into_owner_series(self):
        start = _minute() + timedelta(hours=3)
        self.write_watched([OWNER_SERIES])
        order, _ = self.put_order("mtg-t601", start, room=ROOM, series="portal-t601")
        queue = self.run_scheduler_with_calendar(
            [_gcal_event("dir-1", start, ROOM + "?utm_source=calendar")]
        )
        self.assertEqual(len(queue), 1, "одна комната, одна секунда — один бот, метка не в счёт")
        self.assertEqual(queue[0]["series"], "anzhee-direktorat")
        self.assertEqual(queue[0]["order_id"], order["id"], "связь с нарядом обязана уцелеть")

    def test_one_meeting_takes_one_slot(self):
        start = _minute() + timedelta(hours=2)
        self.put_order("mtg-t602", start, room=ROOM + "?utm_source=portal")
        self.queue_of([{
            "meeting_id": "anzhee-direktorat", "event_id": "gcal:dir-1",
            "series": "anzhee-direktorat", "start_at": start.astimezone(MSK).isoformat(),
            "duration_minutes": 60, "url": ROOM, "type": "google-calendar",
        }])
        self.assertEqual(slots.occupancy(start, 60)["busy"], 1, "наряд и та же встреча в очереди — один слот")

    def test_waiting_start_survives_a_new_tail_from_the_portal(self):
        """Пока старт ждёт слота, портал переслал ссылку той же комнаты с другой меткой."""
        os.environ["MEETING_NOTARY_MAX_CONCURRENT_BOTS"] = "1"
        t0 = _minute()
        start = t0 + timedelta(minutes=2)
        order, _ = self.put_order(
            "mtg-t603", start, room=ROOM + "?utm_source=a", series="portal-t603",
            now=t0 - timedelta(hours=2),
        )
        self.write_watched([])
        self.run_scheduler()
        self.assertEqual(self.tick(start, live_series=["tatiana"]), [], "слот занят — старт ждёт")
        self.assertEqual(len(waitlist.load()), 1)

        orders_lib.update_order(
            order["id"], {"room_url": ROOM + "?utm_source=b"}, now=start + timedelta(minutes=10)
        )
        self.assertEqual(self.tick(start + timedelta(minutes=10), live_series=["tatiana"]), [])
        self.assertEqual(
            len(waitlist.load()), 1, "та же комната с другой меткой — не перенос, ожидание живо",
        )
        self.run_scheduler()  # очередь перестроена уже с новой ссылкой
        launched = self.tick(start + timedelta(minutes=12))
        self.assertEqual(len(launched), 1, "слот освободился — бот заходит с опозданием")
        self.assertEqual(launched[0]["order_id"], order["id"])


# ---------------------------------------------------------------------------
# №4 — время к одному поясу до сравнения
# ---------------------------------------------------------------------------


class TestPortalUtcTwinGoesToOwnerSeries(_Stand):
    """ГЛАВНАЯ проба №4: наряд в UTC и серия владельца в +03 — одна встреча."""

    def test_one_bot_in_owner_series_and_the_order_link_survives(self):
        t0 = _minute()
        start = t0 + timedelta(minutes=4)
        self.write_watched([OWNER_SERIES])
        order, _ = self.put_order(
            "mtg-z701", start, room=ROOM, series="portal-z701", now=t0 - timedelta(hours=2),
        )
        self.assertTrue(order["start_at"].endswith("+00:00"), "портал присылает UTC")

        queue = self.run_scheduler_with_calendar([_gcal_event("dir-1", start, ROOM)])
        self.assertEqual(len(queue), 1, "одна комната, одна секунда — один бот")
        self.assertTrue(queue[0]["start_at"].endswith("+03:00"), "календарь — в своём поясе")
        self.assertEqual(queue[0]["meeting_id"], "anzhee-direktorat", "встречу ведёт серия владельца")
        self.assertEqual(queue[0]["order_id"], order["id"], "связь с нарядом обязана уцелеть")

        launched = self.tick(t0 + timedelta(minutes=1))
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0]["series"], "anzhee-direktorat", "протокол идёт в серию владельца")
        self.assertEqual(launched[0]["order_id"], order["id"])
        run = (orders_lib.get_order(order["id"]) or {}).get("run") or {}
        self.assertTrue(run.get("session_uid"), "наряд знает, что по нему поднялся бот")
        self.assertEqual(
            self.tick(t0 + timedelta(minutes=2), live_series=["anzhee-direktorat"]), [],
            "второго бота нет",
        )


class TestOrderByMoment(_Stand):
    """Раньше начинается — раньше стоит: очередь, реестр нарядов, наряды у старта."""

    def test_queue_is_ordered_by_moment_not_by_string(self):
        start = _minute() + timedelta(hours=3)
        self.write_watched([OWNER_SERIES])
        self.put_order("mtg-z702", start + timedelta(minutes=30), room=ROOM_2, series="portal-z702")
        queue = self.run_scheduler_with_calendar([_gcal_event("dir-1", start, ROOM)])
        self.assertEqual(
            [i["meeting_id"] for i in queue], ["anzhee-direktorat", "portal-mtg-z702"],
            "серия владельца начинается на полчаса раньше наряда",
        )

    def test_orders_registry_lists_by_moment(self):
        start = _minute() + timedelta(hours=3)
        self.put_order_in_zone("mtg-z703", start, zone=MSK, room=ROOM)
        self.put_order("mtg-z704", start + timedelta(minutes=30), room=ROOM_2)
        self.assertEqual(
            [o["meeting_id"] for o in orders_lib.list_orders()], ["mtg-z703", "mtg-z704"],
        )

    def test_earlier_near_order_gets_the_only_slot(self):
        """Потолок 1, два наряда у старта в разных поясах: слот — тому, кто раньше."""
        os.environ["MEETING_NOTARY_MAX_CONCURRENT_BOTS"] = "1"
        t0 = _minute()
        early = self.put_order_in_zone(
            "mtg-z705", t0 + timedelta(minutes=1), zone=MSK, room=ROOM, series="portal-z705", now=t0,
        )
        self.put_order_in_zone(
            "mtg-z706", t0 + timedelta(minutes=2), zone=timezone.utc, room=ROOM_2,
            series="portal-z706", now=t0,
        )
        self.queue_of([])
        launched = self.tick(t0 + timedelta(minutes=1))
        self.assertEqual([i["order_id"] for i in launched], [early["id"]])


class TestWinnerIgnoresOrder(_Stand):
    """Кого вести при склейке, не зависит от порядка элементов (`_pick_winner`)."""

    def _pair(self, owner_id: str):
        start = _minute() + timedelta(hours=1)
        order, _ = self.put_order("mtg-w801", start, room=ROOM)
        owner_item = {
            "meeting_id": owner_id, "event_id": "gcal:w-1", "series": owner_id,
            "start_at": start.astimezone(MSK).isoformat(), "duration_minutes": 60,
            "url": ROOM, "type": "google-calendar", "calendar_id": "primary", "ical_uid": None,
        }
        return start, order, portal_queue.queue_item(order), owner_item

    def test_owner_named_series_wins_even_when_the_order_comes_first(self):
        start, order, portal_item, owner_item = self._pair("anzhee-direktorat")
        out = scheduler.dedupe_queue(
            [portal_item, owner_item], memory={}, now=start - timedelta(hours=1)
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["meeting_id"], "anzhee-direktorat", "серия владельца ведёт встречу")
        self.assertEqual(out[0]["order_id"], order["id"], "связь с нарядом обязана уцелеть")

    def test_order_beats_auto_named_record_in_any_order(self):
        """Так было и до правки: автоимя вечернего блока — расходный материал."""
        start, _order, portal_item, owner_item = self._pair("oneoff-0a1b2c3d")
        for group in ([owner_item, portal_item], [portal_item, owner_item]):
            out = scheduler.dedupe_queue(
                [dict(i) for i in group], memory={}, now=start - timedelta(hours=1)
            )
            with self.subTest(first=group[0]["meeting_id"]):
                self.assertEqual([i["meeting_id"] for i in out], [portal_item["meeting_id"]])


# ---------------------------------------------------------------------------
# Цикл из 5 ходов по правке (06.10.2026)
# ---------------------------------------------------------------------------


class TestCrookedDateKeepsTheRegistry(_Stand):
    """Время, которое не переводится в UTC, — в конец, а не исключение на весь реестр."""

    CROOKED = "0001-01-01T00:30:00+01:00"

    def test_sort_key_of_untranslatable_time_goes_last(self):
        self.assertIsNone(mid.instant_key(self.CROOKED))
        self.assertEqual(mid.start_sort_key(self.CROOKED), (1, ""))

    def test_one_crooked_order_does_not_hide_the_others(self):
        import json

        start = _minute() + timedelta(hours=2)
        good, _ = self.put_order("mtg-c901", start, room=ROOM)
        path = orders_lib.orders_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["orders"]["portal:mtg-c902"] = dict(
            data["orders"][good["id"]], id="portal:mtg-c902", meeting_id="mtg-c902",
            start_at=self.CROOKED,
        )
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        listed = [o["id"] for o in orders_lib.list_orders()]
        self.assertEqual(listed, [good["id"], "portal:mtg-c902"], "кривое время — в конец")
        horizon = datetime.now(timezone.utc)
        items = portal_queue.items_for_queue(horizon, horizon + timedelta(hours=48))
        self.assertEqual([i["order_id"] for i in items], [good["id"]], "хороший наряд на месте")


class TestSeriesYieldsToOrderByMarkup(_Stand):
    """Серия другой компании, чем наряд, или приватная встречу у наряда не забирает."""

    def _merge(self, series_company: str, visibility: Optional[str] = None):
        start = _minute() + timedelta(hours=3)
        record = dict(OWNER_SERIES, company=series_company, expected_participants=["Пётр Серийный"])
        if visibility:
            record["visibility"] = visibility
        self.write_watched([record])
        order, _ = self.put_order(
            "mtg-f101", start, room=ROOM + "?utm_source=portal", series="portal-f101",
            company="anzhee", participants=["Иван Дилеров"],
        )
        queue = self.run_scheduler_with_calendar([_gcal_event("dir-1", start, ROOM)])
        self.assertEqual(len(queue), 1, "одна комната, одна секунда — один бот")
        return order, queue[0]

    def test_foreign_company_series_does_not_take_the_order(self):
        order, row = self._merge("mpfirst")
        self.assertEqual(row["meeting_id"], "portal-mtg-f101", "встречу ведёт наряд, как до 06.10")
        self.assertEqual(row["order_id"], order["id"])
        self.assertNotEqual(row.get("series"), "anzhee-direktorat",
                            "память серии МПервого в протокол наряда Anzhee не попадает")
        self.assertEqual(row["expected_participants"], ["Иван Дилеров"],
                         "и состав серии чужой компании в наряд не переезжает")

    def test_same_company_series_takes_the_meeting(self):
        order, row = self._merge("anzhee", visibility="company")
        self.assertEqual(row["meeting_id"], "anzhee-direktorat")
        self.assertEqual(row["order_id"], order["id"], "связь с нарядом обязана уцелеть")

    def test_private_series_does_not_take_the_order(self):
        order, row = self._merge("anzhee", visibility="private")
        self.assertEqual(row["meeting_id"], "portal-mtg-f101",
                         "память приватной серии по маршруту наряда не уходит")
        self.assertEqual(row["order_id"], order["id"])


class TestMergedMeetingKeepsOrderData(_Stand):
    """Склеенная встреча (серия владельца + наряд) не теряет того, что знал наряд."""

    def test_participants_from_the_portal_card_reach_the_bot(self):
        start = _minute() + timedelta(hours=3)
        self.write_watched([dict(OWNER_SERIES, expected_participants=["Илья Рыбалка"])])
        self.put_order(
            "mtg-p201", start, room=ROOM, series="portal-p201",
            participants=["Иван Дилеров", "Илья Рыбалка"],
        )
        queue = self.run_scheduler_with_calendar([_gcal_event("dir-1", start, ROOM)])
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["meeting_id"], "anzhee-direktorat")
        self.assertEqual(
            queue[0]["expected_participants"], ["Илья Рыбалка", "Иван Дилеров"],
            "свои — первыми, имена из карточки портала — следом, без повторов",
        )

    def test_portal_downloads_the_protocol_from_the_series_folder(self):
        import json

        from notary import gateway

        start = _minute() + timedelta(hours=3)
        order, _ = self.put_order("mtg-p202", start, room=ROOM, series="portal-p202")
        protocols = self.tmp / "protocols"
        day = start.strftime("%Y-%m-%d")
        folder = protocols / "anzhee-direktorat"
        folder.mkdir(parents=True)
        (folder / ("%s-protokol.md" % day)).write_text("# Протокол\n", encoding="utf-8")
        (folder / "meta.json").write_text(json.dumps({
            "series": "anzhee-direktorat", "date": day, "sessionUid": "tm-1",
            "startTs": start.isoformat(), "order": order,
        }, ensure_ascii=False), encoding="utf-8")
        env = {
            "MEETING_NOTARY_PROTOCOLS_DIR": str(protocols),
            "MEETING_NOTARY_DELIVERED_ROOTS": str(protocols),
        }
        with mock.patch.dict(os.environ, env):
            os.environ.pop("MEETING_NOTARY_ARCHIVE_DIR", None)
            found = gateway.protocol_path_for_order(orders_lib.get_order(order["id"]))
        self.assertEqual(found, folder / ("%s-protokol.md" % day),
                         "протокол склеенной встречи лежит в папке серии владельца")


if __name__ == "__main__":
    unittest.main()
