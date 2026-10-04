"""Плановый наряд портала у самого старта: runner берёт его из реестра сам.

Бэклог нотариуса 04.10.2026 (решение владельца — вариант А долга №25 ремонта
портала). Плановые наряды runner видел только через очередь планировщика, а она
перестраивается раз в 3 минуты: встреча, заведённая или перенесённая на портале за
минуту-две до начала, попадала в очередь после окна старта, и бот её пропускал.

Что доказывается (тик runner'а гоняется по минутам, планировщик — настоящий):

* `TestNearOrderGetsABot` — заведённый за минуту до старта и перенесённый на
  «через минуту» наряд получает бота РОВНО ОДИН РАЗ, пока планировщик не отработал;
  после того как планировщик положил тот же наряд в очередь, второго запуска нет.
  Наряд, заведённый уже после начала (шлюз терпит 2 минуты), получает поздний
  вход с подписью в протоколе — даже если планировщик успел раньше runner'а.
* `TestNoSecondBot` — главный риск: та же встреча в другом облике (личная серия
  владельца в той же комнате и секунде, память дублей планировщика, «нет»
  владельца) второго бота не даёт; перенос внутри окна даёт одного кандидата, и
  бот идёт по новому времени и в новую комнату.
* `TestWhatIsNotTaken` — снятый наряд не запускается, дальний (через 30 минут)
  этим путём не берётся, обычный старт без поздней правки позднего входа не
  получает, битый реестр нарядов не роняет тик, нечитаемый реестр серий не
  снимает «нет» владельца.

Цикл5 04.10 (после правки):

* `TestOneMeetingOneBotAcrossTicks` — строку встречи runner выбирает сам, а следующий
  тик или планировщик вправе взять другую: серия владельца, вернувшаяся в очередь
  после запуска наряда, пересозданная встреча, старший наряд, перенесённый в слот
  запущенного, — второго бота нет (ключ встречи «комната + секунда»), ложного «пока
  не записал» тоже; разные комнаты в ту же секунду — две встречи.
* `TestRegistryIsFresherThanQueue` — строка очереди снятого, «вышедшего» и
  перенесённого далеко наряда бота не поднимает и до перестройки очереди, ожидание
  слота по ней снимается; строку, в которую схлопнулась серия владельца, отмена
  наряда не гасит.
* `TestLateOrderAlreadyCollapsedWithCalendar`, `TestRememberedEventId` — поздний вход
  у строки, уже склеенной с календарём; память дублей читается без записи и без
  предупреждений от имени планировщика.

Выключатель OFF этот путь не открывает вовсе: счётчик обращений к реестру при OFF
— в `test_orders_ph10b.TestPortalOff.test_orders_registry_never_touched_when_off`
(тот же тик целиком, включая этот путь).
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

import notary.runner as runner  # noqa: E402
import notary.scheduler as scheduler  # noqa: E402
from notary.lib import late_join, orders as orders_lib  # noqa: E402
from notary.lib import portal_queue, state as state_lib, waitlist  # noqa: E402
from notary.lib.meeting_identity import fingerprint  # noqa: E402
from notary.tests.test_orders_ph10b import ROOM, ROOM_2, _Base, _iso  # noqa: E402


def _minute() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


class _Near(_Base):
    """Стенд Ф10b + короткие сценарии «наряд у старта»."""

    def put_late(self, meeting_id: str, start: datetime, *, at: datetime, **kw):
        """Наряд, который портал поставил (или перенёс) в момент `at`."""
        order, _ = self.put_order(meeting_id, start, now=at, **kw)
        return order

    def put_long_ago(self, meeting_id: str, start: datetime, **kw):
        """Наряд, поставленный за два часа: его давно знает планировщик."""
        order, _ = self.put_order(meeting_id, start, now=start - timedelta(hours=2), **kw)
        return order


class TestNearOrderGetsABot(_Near):
    """Встреча, поставленная у самого старта, получает бота — один раз."""

    def test_created_one_minute_before_start(self):
        t0 = _minute()
        order = self.put_late("mtg-n101", t0 + timedelta(minutes=1), at=t0, series="portal-n101")
        self.queue_of([])  # планировщик ещё не отработал

        first = self.tick(t0 + timedelta(minutes=1))
        self.assertEqual(len(first), 1, "наряд у старта обязан получить бота без планировщика")
        self.assertEqual(first[0]["order_id"], order["id"])
        # Минута +2: бот не виден в docker (упал или ещё стартует) — ключ старта
        # всё равно не даёт второго запуска.
        self.assertEqual(self.tick(t0 + timedelta(minutes=2)), [], "второй бот на тот же наряд")

        # Планировщик положил тот же наряд в очередь — ключ старта совпадает.
        self.write_watched([])
        queue = self.run_scheduler()
        self.assertEqual([state_lib.start_key(i) for i in queue], [state_lib.start_key(first[0])])
        self.assertEqual(self.tick(t0 + timedelta(minutes=3), live_series=["portal-n101"]), [])
        self.assertEqual(self.tick(t0 + timedelta(minutes=3)), [], "ключ занят — и без живого бота")

    def test_moved_to_one_minute_from_now(self):
        t0 = _minute()
        self.put_long_ago("mtg-n102", t0 + timedelta(minutes=30), series="portal-n102")
        self.write_watched([])
        old_queue = self.run_scheduler()  # в очереди — старое время, через 30 минут
        self.assertEqual(len(old_queue), 1)

        moved = self.put_late("mtg-n102", t0 + timedelta(minutes=1), at=t0, series="portal-n102")
        self.assertEqual(moved["version"], 2, "перенос — та же запись, не второй наряд")

        first = self.tick(t0 + timedelta(minutes=1))
        self.assertEqual(len(first), 1, "перенесённый на «через минуту» наряд обязан получить бота")
        self.assertEqual(first[0]["start_at"], _iso(t0 + timedelta(minutes=1)), "по новому времени")
        self.assertEqual(self.tick(t0 + timedelta(minutes=2)), [])

        queue = self.run_scheduler()
        self.assertEqual([i["start_at"] for i in queue], [_iso(t0 + timedelta(minutes=1))])
        self.assertEqual(self.tick(t0 + timedelta(minutes=3), live_series=["portal-n102"]), [])
        # Прежнее время не всплывает и в своё окно.
        self.assertEqual(self.tick(t0 + timedelta(minutes=27)), [])

    def test_created_after_start_joins_late_and_says_so(self):
        """Шлюз принимает начало до 2 минут в прошлом — такой наряд не теряется."""
        t0 = _minute()
        self.put_late("mtg-n103", t0, at=t0 + timedelta(seconds=90), series="portal-n103")
        self.queue_of([])

        launched = self.tick(t0 + timedelta(minutes=3))
        self.assertEqual(len(launched), 1, "наряд, заведённый после начала, получает поздний вход")
        entry = late_join.get("portal-n103", _iso(t0)[:10])
        self.assertIsNotNone(entry, "опоздание обязано быть отмечено для протокола")
        self.assertEqual(entry["reason"], late_join.REASON_LATE_ORDER)
        self.assertEqual(entry["late_minutes"], 3)
        signed = late_join.sign("#протоколвстречи\n\n## Итоги\n", "portal-n103", _iso(t0)[:10])
        self.assertIn("на портале перед самым началом", signed)

        self.write_watched([])
        self.run_scheduler()
        self.assertEqual(self.tick(t0 + timedelta(minutes=4)), [], "второго запуска нет")

    def test_late_entry_survives_when_scheduler_was_faster(self):
        """Планировщик успел положить поздний наряд раньше тика runner'а."""
        t0 = _minute()
        self.put_late("mtg-n104", t0, at=t0 + timedelta(seconds=90), series="portal-n104")
        self.write_watched([])
        self.assertEqual(len(self.run_scheduler()), 1)

        launched = self.tick(t0 + timedelta(minutes=3))
        self.assertEqual(len(launched), 1, "строка планировщика обязана получить тот же поздний вход")
        self.assertEqual(late_join.get("portal-n104", _iso(t0)[:10])["reason"], late_join.REASON_LATE_ORDER)


class TestNoSecondBot(_Near):
    """Элемент из реестра проходит те же защиты, что элемент планировщика."""

    def test_owner_series_in_the_same_room_and_second(self):
        t0 = _minute()
        start = t0 + timedelta(minutes=2)
        self.queue_of([{
            "meeting_id": "anzhee-direktorat", "event_id": "gcal:dir-1",
            "series": "anzhee-direktorat", "start_at": _iso(start), "duration_minutes": 60,
            "url": ROOM, "type": "google-calendar", "calendar_id": "primary", "ical_uid": None,
        }])
        self.put_late("mtg-n201", start, at=t0, room=ROOM, series="portal-n201")

        launched = self.tick(t0)
        self.assertEqual(len(launched), 1, "одна комната, одна секунда — один бот")
        self.assertEqual(launched[0]["meeting_id"], "anzhee-direktorat")

    def test_dedup_memory_keeps_the_key_of_a_meeting_already_led(self):
        """Личную встречу уже повели и она ушла из очереди (разовая гасит себя)."""
        t0 = _minute()
        start = t0 + timedelta(minutes=1)
        fp = fingerprint(_iso(start), ROOM, None)
        scheduler.save_dedup_winners({
            key: {"event_id": "gcal:owner-1", "meeting_id": "owner-oneoff", "start_at": fp.start}
            for key in scheduler._memory_keys(fp)
        })
        state_lib.write_state({
            "gcal:owner-1@%s" % state_lib.normalized_start_key(start):
                _iso(t0 - timedelta(minutes=4)),
        })
        self.queue_of([])
        self.put_late("mtg-n202", start, at=t0, room=ROOM, series="portal-n202")

        self.assertEqual(self.tick(t0 + timedelta(minutes=1)), [], "бот этой встречи уже в комнате")

    def test_owner_said_no_to_this_meeting(self):
        t0 = _minute()
        start = t0 + timedelta(minutes=1)
        self.write_watched([{
            "id": "oneoff-0a1b2c3d", "series": "oneoff-0a1b2c3d", "type": "one-off",
            "created_by_rejection": True, "enabled": False,
            "event_start_at": _iso(start), "event_url": ROOM,
        }])
        self.queue_of([])
        self.put_late("mtg-n203", start, at=t0, room=ROOM, series="portal-n203")

        self.assertEqual(self.tick(t0 + timedelta(minutes=1)), [], "«нет» владельца действует и здесь")

    def test_moved_inside_the_window_gives_one_candidate(self):
        t0 = _minute()
        self.put_long_ago("mtg-n204", t0 + timedelta(minutes=4), series="portal-n204")
        self.write_watched([])
        self.run_scheduler()  # в очереди — старт через 4 минуты
        self.put_late("mtg-n204", t0 + timedelta(minutes=2), at=t0, series="portal-n204")

        launched = self.tick(t0)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0]["start_at"], _iso(t0 + timedelta(minutes=2)), "по новому времени")
        self.assertEqual(waitlist.load(), {}, "прежний старт не встаёт в ожидание")
        self.assertFalse(any("Пока не записал" in p for p in self.pushes), self.pushes)
        self.assertEqual(self.tick(t0 + timedelta(minutes=1), live_series=["portal-n204"]), [])
        self.assertFalse(any("Пока не записал" in p for p in self.pushes), self.pushes)

    def test_moved_onto_a_meeting_already_in_queue(self):
        """Наряд перенесли ровно на встречу владельца — ведёт она, старая копия молчит."""
        t0 = _minute()
        self.put_long_ago("mtg-n206", t0 + timedelta(minutes=4), room=ROOM, series="portal-n206")
        self.write_watched([])
        queue = self.run_scheduler()
        owner = {
            "meeting_id": "anzhee-direktorat", "event_id": "gcal:dir-3",
            "series": "anzhee-direktorat", "start_at": _iso(t0 + timedelta(minutes=2)),
            "duration_minutes": 60, "url": ROOM_2, "type": "google-calendar",
            "calendar_id": "primary", "ical_uid": None,
        }
        self.queue_of([owner] + queue)
        self.put_late("mtg-n206", t0 + timedelta(minutes=2), at=t0, room=ROOM_2, series="portal-n206")

        launched = self.tick(t0)
        self.assertEqual([i["meeting_id"] for i in launched], ["anzhee-direktorat"],
                         "прежняя копия наряда (старая комната и время) запускаться не должна")

    def test_moved_to_another_room_at_the_same_minute(self):
        t0 = _minute()
        start = t0 + timedelta(minutes=3)
        self.put_long_ago("mtg-n205", start, room=ROOM, series="portal-n205")
        self.write_watched([])
        self.assertEqual(self.run_scheduler()[0]["url"], ROOM)
        self.put_late("mtg-n205", start, at=t0, room=ROOM_2, series="portal-n205")

        launched = self.tick(t0)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0]["url"], ROOM_2, "бот обязан пойти в новую комнату")


class TestWhatIsNotTaken(_Near):
    """Снятое, дальнее и старое этим путём не берётся."""

    def test_cancelled_order_is_not_launched(self):
        t0 = _minute()
        order = self.put_late("mtg-n301", t0 + timedelta(minutes=1), at=t0, series="portal-n301")
        orders_lib.cancel_order(order["id"], reason="встречу отменили", now=t0)
        self.queue_of([])
        self.assertEqual(self.tick(t0 + timedelta(minutes=1)), [], "по снятому наряду бот не приходит")
        self.assertEqual(self.tick(t0 + timedelta(minutes=2)), [])

    def test_far_order_is_left_to_the_scheduler(self):
        t0 = _minute()
        self.put_late("mtg-n302", t0 + timedelta(minutes=30), at=t0, series="portal-n302")
        queue, late_keys = runner._with_near_scheduled_orders([], t0)
        self.assertEqual(queue, [], "наряд через 30 минут — забота планировщика")
        self.assertEqual(late_keys, set())
        self.assertEqual(self.tick(t0), [])

    def test_plain_order_gets_no_late_entry(self):
        """Поздний вход — только наряду, который поставили или изменили у старта."""
        t0 = _minute()
        order = self.put_long_ago("mtg-n303", t0, series="portal-n303")
        self.queue_of([portal_queue.queue_item(order)])
        self.assertEqual(self.tick(t0 + timedelta(minutes=3)), [])

    def test_broken_registry_keeps_the_tick_as_before(self):
        t0 = _minute()
        self.queue_of([{
            "meeting_id": "anzhee-direktorat", "event_id": "gcal:dir-2",
            "series": "anzhee-direktorat", "start_at": _iso(t0 + timedelta(minutes=2)),
            "duration_minutes": 60, "url": ROOM_2, "type": "google-calendar",
        }])
        (self.registry / orders_lib.ORDERS_FILENAME).write_text("{битый", encoding="utf-8")
        launched = self.tick(t0)
        self.assertEqual([i["meeting_id"] for i in launched], ["anzhee-direktorat"])

    def test_unreadable_series_registry_takes_no_near_orders(self):
        """Без отсева «нет» владельца наряд у старта не берётся (цикл5 04.10, ход 5)."""
        t0 = _minute()
        start = t0 + timedelta(minutes=1)
        self.write_watched([{
            "id": "oneoff-0a1b2c3e", "series": "oneoff-0a1b2c3e", "type": "one-off",
            "created_by_rejection": True, "enabled": False,
            "event_start_at": _iso(start), "event_url": ROOM,
        }])
        self.queue_of([])
        self.put_late("mtg-n304", start, at=t0, room=ROOM, series="portal-n304")
        (self.registry / "watched.yaml").write_text("watched: [битый", encoding="utf-8")
        self.assertEqual(self.tick(t0 + timedelta(minutes=1)), [],
                         "реестр серий не прочитан — «нет» владельца молча не слетает")


def _owner_manual(start: datetime, room: str) -> dict:
    """Личная серия владельца (manual, cron) ровно на эту минуту — её разложит планировщик."""
    return {
        "id": "anzhee-direktorat", "series": "anzhee-direktorat", "type": "manual",
        "cron": "%d %d * * *" % (start.minute, start.hour), "tz": "UTC",
        "room": room, "enabled": True, "duration_minutes": 60,
    }


class TestOneMeetingOneBotAcrossTicks(_Near):
    """Цикл5 04.10: строку встречи runner выбирает сам, а следующий тик или прогон
    планировщика вправе выбрать ДРУГУЮ строку той же встречи — с другим ключом
    старта. Барьер — ключ встречи «комната + секунда» (`runner._meeting_key`)."""

    def test_owner_series_collapsed_after_near_launch(self):
        """Календарь в прошлом прогоне не ответил: в очереди не было и серии владельца."""
        t0 = _minute()
        start = t0 + timedelta(minutes=3)
        self.put_late("mtg-n401", start, at=t0, room=ROOM, series="portal-n401")
        self.queue_of([])
        self.assertEqual(len(self.tick(t0)), 1)

        self.write_watched([_owner_manual(start, ROOM)])
        queue = self.run_scheduler()
        owner = [i for i in queue if i["meeting_id"] == "anzhee-direktorat"
                 and i["start_at"] == _iso(start)]
        self.assertEqual([i.get("order_id") for i in owner], ["portal:mtg-n401"],
                         "стенд: серия победила в схлопывании и забрала связь с нарядом")
        self.assertEqual(self.tick(t0 + timedelta(minutes=1), live_series=["portal-n401"]), [],
                         "второй бот в ту же комнату на ту же встречу")

    def test_meeting_recreated_on_portal_after_launch(self):
        t0 = _minute()
        start = t0 + timedelta(minutes=3)
        first = self.put_late("mtg-n402", start, at=t0, room=ROOM, series="portal-n402")
        self.queue_of([])
        self.assertEqual(len(self.tick(t0)), 1)
        orders_lib.cancel_order(first["id"], reason="пересоздали", now=t0 + timedelta(seconds=20))
        self.put_late("mtg-n402b", start, at=t0 + timedelta(seconds=30), room=ROOM,
                      series="portal-n402b")
        self.assertEqual(self.tick(t0 + timedelta(minutes=1), live_series=["portal-n402"]), [],
                         "пересозданная встреча — та же комната и секунда, бот уже в ней")

    def test_older_order_moved_into_slot_of_launched_one(self):
        """Старший наряд идёт в реестре первым — без барьера выбор строки «переворачивался»."""
        t0 = _minute()
        start = t0 + timedelta(minutes=3)
        self.put_long_ago("mtg-n403a", t0 + timedelta(minutes=40), room=ROOM, series="portal-n403a")
        self.put_late("mtg-n403b", start, at=t0, room=ROOM, series="portal-n403b")
        self.queue_of([])
        self.assertEqual([i["order_id"] for i in self.tick(t0)], ["portal:mtg-n403b"])
        self.put_late("mtg-n403a", start, at=t0 + timedelta(seconds=30), room=ROOM,
                      series="portal-n403a")
        self.assertEqual(self.tick(t0 + timedelta(minutes=1), live_series=["portal-n403b"]), [])

    def test_no_false_waiting_message_for_the_same_meeting(self):
        """Потолок занят: другая строка той же встречи не встаёт в ожидание с
        «пока не записал» — встречу уже пишут (отсев в `_candidates`)."""
        t0 = _minute()
        start = t0 + timedelta(minutes=3)
        self.put_late("mtg-n405", start, at=t0, room=ROOM, series="portal-n405")
        self.queue_of([])
        self.assertEqual(len(self.tick(t0)), 1)
        self.write_watched([_owner_manual(start, ROOM)])
        self.run_scheduler()
        self.assertEqual(self.tick(t0 + timedelta(minutes=1), live_series=["portal-n405", "busy"]), [])
        self.assertEqual(waitlist.load(), {})
        self.assertFalse(any("Пока не записал" in p for p in self.pushes), self.pushes)

    def test_state_acquire_takes_the_meeting_key_under_the_same_lock(self):
        t0 = _minute()
        self.assertTrue(state_lib.state_acquire("a@1", now=t0, meeting_key="room:x@1"))
        self.assertFalse(state_lib.state_acquire("b@1", now=t0, meeting_key="room:x@1"),
                         "другой ключ старта той же встречи")
        self.assertTrue(state_lib.state_acquire("c@1", now=t0, meeting_key="room:y@1"))
        self.assertTrue(state_lib.state_acquire("d@1", now=t0), "без ключа встречи — как раньше")

    def test_two_rooms_at_the_same_second_both_run(self):
        """Барьер про одну комнату: разные комнаты в ту же секунду — две встречи."""
        t0 = _minute()
        start = t0 + timedelta(minutes=2)
        self.put_late("mtg-n404a", start, at=t0, room=ROOM, series="portal-n404a")
        self.put_late("mtg-n404b", start, at=t0, room=ROOM_2, series="portal-n404b")
        self.queue_of([])
        self.assertEqual(sorted(i["url"] for i in self.tick(t0)), sorted([ROOM, ROOM_2]))


class TestRegistryIsFresherThanQueue(_Near):
    """Цикл5 04.10: строка наряда в очереди есть, а в реестре его на этом старте нет.

    Очередь перестраивается раз в 3 минуты; runner реестр читает каждую минуту —
    по снятому, вышедшему или перенесённому наряду бот не идёт и до перестройки.
    """

    def _row_in_queue(self, meeting_id: str, start: datetime, **kw):
        order = self.put_long_ago(meeting_id, start, **kw)
        self.write_watched([])
        self.assertEqual(len(self.run_scheduler()), 1)
        return order

    def test_cancelled_after_queue_rebuild(self):
        t0 = _minute()
        order = self._row_in_queue("mtg-n501", t0 + timedelta(minutes=6), series="portal-n501")
        orders_lib.cancel_order(order["id"], reason="отменили", now=t0)
        self.assertEqual(self.tick(t0 + timedelta(minutes=1)), [],
                         "снятый наряд поднял бота по старой строке очереди")

    def test_leave_now_before_launch(self):
        t0 = _minute()
        order = self._row_in_queue("mtg-n502", t0 + timedelta(minutes=4), series="portal-n502")
        orders_lib.leave_now(order_id=order["id"], now=t0)
        self.assertEqual(self.tick(t0), [], "«выйти сейчас» нового старта не получает")

    def test_moved_far_does_not_launch_at_old_time(self):
        t0 = _minute()
        self._row_in_queue("mtg-n503", t0 + timedelta(minutes=4), series="portal-n503")
        self.put_late("mtg-n503", t0 + timedelta(hours=1), at=t0, series="portal-n503")
        self.assertEqual(self.tick(t0), [], "перенесённый на час наряд поднял бота по старому времени")
        self.run_scheduler()
        moved = self.tick(t0 + timedelta(minutes=58))
        self.assertEqual([i["start_at"] for i in moved], [_iso(t0 + timedelta(hours=1))],
                         "в новое время бот приходит")

    def test_owner_series_row_with_cancelled_order_link_still_runs(self):
        """Строку, в которую схлопнулась личная серия, отмена наряда не гасит."""
        t0 = _minute()
        start = t0 + timedelta(minutes=3)
        order = self.put_long_ago("mtg-n504", start, room=ROOM, series="portal-n504")
        self.write_watched([_owner_manual(start, ROOM)])
        self.run_scheduler()
        orders_lib.cancel_order(order["id"], reason="отменили", now=t0)
        self.assertEqual([i["meeting_id"] for i in self.tick(t0)], ["anzhee-direktorat"])

    def test_waiting_row_of_cancelled_order_leaves_the_waitlist(self):
        t0 = _minute()
        order = self._row_in_queue("mtg-n505", t0 + timedelta(minutes=2), series="portal-n505")
        self.assertEqual(self.tick(t0, live_series=["busy-a", "busy-b"]), [])
        self.assertEqual(len(waitlist.load()), 1, "стенд: старт ждёт свободного слота")
        orders_lib.cancel_order(order["id"], reason="отменили", now=t0 + timedelta(minutes=1))
        self.assertEqual(self.tick(t0 + timedelta(minutes=4)), [], "слот свободен, но наряд снят")
        self.assertEqual(waitlist.load(), {}, "снятая встреча снята и с ожидания")


class TestLateOrderAlreadyCollapsedWithCalendar(_Near):
    """Цикл5 04.10 (ход 1, Н5): планировщик успел схлопнуть поздний наряд со строкой
    календаря владельца (строка несёт `ical_uid`) — поздний вход положен и ей."""

    def test_late_entry_survives_collapse(self):
        t0 = _minute()
        order = self.put_late("mtg-n601", t0, at=t0 + timedelta(seconds=90), series="portal-n601")
        row = portal_queue.queue_item(order)
        row["ical_uid"] = "owner-event-1@google.com"
        self.queue_of([row])
        launched = self.tick(t0 + timedelta(minutes=3))
        self.assertEqual(len(launched), 1, "поздний наряд, уже склеенный с календарём, потерял вход")
        self.assertEqual(late_join.get("portal-n601", _iso(t0)[:10])["reason"],
                         late_join.REASON_LATE_ORDER)


class TestRememberedEventId(unittest.TestCase):
    """`scheduler.remembered_event_id` — тот же выбор, что у планировщика, без записи и шума."""

    def test_same_choice_as_planner_without_write_or_warning(self):
        import copy
        import json
        import logging

        now = _minute()
        start = now + timedelta(minutes=2)
        fp = fingerprint(_iso(start), ROOM, None)
        memory = {key: {"event_id": "gcal:o-1", "meeting_id": "owner", "start_at": fp.start}
                  for key in scheduler._memory_keys(fp)}
        before = json.dumps(memory, sort_keys=True)
        item = {"event_id": "portal:portal:mtg-n701", "start_at": _iso(start), "url": ROOM}

        records: list = []
        handler = logging.Handler(level=logging.WARNING)
        handler.emit = records.append  # type: ignore[assignment]
        log = logging.getLogger("scheduler")
        log.addHandler(handler)
        try:
            got = scheduler.remembered_event_id(item, memory, now)
        finally:
            log.removeHandler(handler)
        self.assertEqual(got, "gcal:o-1")
        self.assertEqual(records, [], "runner не пишет в журнал от имени планировщика")
        self.assertEqual(json.dumps(memory, sort_keys=True), before, "память дублей не пишется")

        planned = copy.deepcopy(item)
        scheduler._apply_winner_memory([planned], [fp], [[fp]], copy.deepcopy(memory), now)
        self.assertEqual(planned["event_id"], got, "выбор совпадает с планировщиком")
        # До заморозки (за 30+ минут) выбор не закреплён — ключ свой.
        self.assertIsNone(scheduler.remembered_event_id(item, memory, start - timedelta(hours=1)))


if __name__ == "__main__":
    unittest.main()
