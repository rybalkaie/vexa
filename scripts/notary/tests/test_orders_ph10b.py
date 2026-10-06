"""Ф10b: планировщик исполняет наряды портала — идемпотентность, перенос, лимит.

Что здесь доказывается (и почему именно так):

* `TestQueueBeforeAfter` — наряды портала уживаются с реестром серий В ОДНОЙ
  очереди, на НАСТОЯЩЕМ прогоне планировщика, а не на вызове чистой функции.
* `TestIdempotency` (R7.3) — повторная отправка одного наряда не приводит ко
  второму боту: та же запись реестра → тот же элемент очереди → тот же ключ
  старта.
* `TestRescheduleISS57` (R12.9, ISS-57) — ГЛАВНАЯ проба фазы. Встречу перенесли,
  когда бот уже сидел в комнате: раньше очередь знала новое время, а повторного
  запуска не было — бот уходил по «никто не пришёл» ровно в минуту нового
  старта. Здесь тик прогоняется по минутам реального инцидента 05.08.2026.
* `TestCancel` (R4.14, R12.10) — отмена снимает приход.
* `TestCapacity` — потолок одновременных записей ведёт себя ОПРЕДЕЛЁННО и
  ВИДИМО: третья встреча ждёт слот, заходит с опозданием, опоздание подписано в
  протоколе; не дождалась — владельцу сказано.
* `TestPortalOff` — 🔴 неизменность поведения владельца при `ENABLE_PORTAL_ORDERS=OFF`
  доказывается МЕХАНИЧЕСКИ (как в Ф10a): очередь совпадает с очередью без
  нарядов на полной матрице «выключено», и реестр нарядов не читается ни разу.
* `TestQueueReaders` — у очереди четыре читателя (runner, шлюз, глубокий health,
  расчёт занятости). Разъедься они — портал обещал бы слот, которого нет.
* `TestInvalidInputs` — враждебные входы списком: тот же наряд дважды, тот же id
  с другим содержимым, наряд в прошлом и через год, отмена несуществующего и
  прошедшего, перенос до и ПОСЛЕ старта, перенос отменённого, три наряда на одно
  время при потолке 2, четвёртый при трёх ждущих, наряд без владельца, наряд при
  выключателе OFF.
* `TestNoTextsInLogs` — опасная тройка: ни имён участников, ни текстов.

🔴 Изоляция: каждая проба работает во ВРЕМЕННОМ каталоге реестра и НЕ пишет ни в
`~/Projects/me/встречи/`, ни в боевые логи. Сторожа того же класса —
`test_state_path_isolation.py`, `test_gateway_state_isolation.py`.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

import notary.cli.registry as registry_mod  # noqa: E402
import notary.runner as runner  # noqa: E402
import notary.scheduler as scheduler  # noqa: E402
from notary.lib import identity, late_join, orders as orders_lib  # noqa: E402
from notary.lib import portal_queue, slots, state as state_lib, waitlist  # noqa: E402
from notary.lib.meeting_identity import instant_key  # noqa: E402

ROOM = "https://telemost.yandex.ru/j/11111111111111"
ROOM_2 = "https://telemost.yandex.ru/j/22222222222222"
ROOM_3 = "https://telemost.yandex.ru/j/33333333333333"

FLAG = identity.ENABLE_ENV
OFF_FORMS = (None, "", "   ", "0", "false", "FALSE", "no", "off", "мусор")

ISOLATED_ENV = (
    "MEETING_NOTARY_REGISTRY_DIR",
    "MEETING_NOTARY_LOG_DIR",
    "MEETING_NOTARY_MAX_CONCURRENT_BOTS",
    "MEETING_NOTARY_START_WAITLIST",
    "MEETING_NOTARY_START_WAITLIST_MAX_MIN",
    FLAG,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class _Base(unittest.TestCase):
    """Временный реестр, выключатель ON, глушёные логи и Telegram."""

    flag_default: Optional[str] = "1"

    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ISOLATED_ENV}
        self.tmp = Path(tempfile.mkdtemp(prefix="ph10b-"))
        self.registry = self.tmp / "registry"
        self.registry.mkdir(parents=True, exist_ok=True)
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = str(self.registry)
        os.environ["MEETING_NOTARY_LOG_DIR"] = str(self.tmp / "logs")
        for name in ("MEETING_NOTARY_MAX_CONCURRENT_BOTS",
                     "MEETING_NOTARY_START_WAITLIST",
                     "MEETING_NOTARY_START_WAITLIST_MAX_MIN"):
            os.environ.pop(name, None)
        self.set_flag(self.flag_default)
        # Ни боевых логов, ни Telegram: сообщения собираем в список.
        self.pushes: List[str] = []
        self.push_keys: List[str] = []
        self._patches = [
            mock.patch.object(runner, "setup_logging", lambda: None),
            mock.patch.object(scheduler, "setup_logging", lambda: None),
            mock.patch.object(runner, "push", side_effect=self._push),
            mock.patch.object(scheduler, "push", side_effect=self._push),
            # 🔴 Реестр СЕРИЙ (в отличие от реестра нарядов и очереди) резолвит
            # путь на ИМПОРТЕ модуля — `cli.registry.WATCHED_FILE`. Подмена
            # `MEETING_NOTARY_REGISTRY_DIR` его уже не двигает, и без этих трёх
            # заплат проба читала бы ЖИВОЙ реестр владельца. Это дефект самого
            # `cli/registry.py` (тот же класс, что чинил f249582 в `lib/state.py`),
            # он записан в отчёт фазы как находка за её пределами.
            mock.patch.object(registry_mod, "WATCHED_FILE", self.registry / "watched.yaml"),
            mock.patch.object(registry_mod, "ROOMS_FILE", self.registry / "rooms.yaml"),
            mock.patch.object(registry_mod, "PAUSE_FILE", self.registry / ".pause-until"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        identity.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _push(self, text: str, **kw: Any) -> None:
        self.pushes.append(text)
        self.push_keys.append(str(kw.get("dedupe_key") or ""))

    def set_flag(self, value: Optional[str]) -> None:
        if value is None:
            os.environ.pop(FLAG, None)
        else:
            os.environ[FLAG] = value
        identity.reset_cache()

    # ---- фикстуры ----

    def put_order(
        self,
        meeting_id: str,
        start: datetime,
        *,
        room: str = ROOM,
        duration: int = 60,
        company: str = "anzhee",
        title: str = "Координация продаж",
        series: Optional[str] = None,
        participants: Optional[List[str]] = None,
        chat_id: Optional[int] = None,
        owner: Optional[Dict[str, Any]] = None,
        kind: str = orders_lib.KIND_SCHEDULED,
        now: Optional[datetime] = None,
    ):
        payload = {
            "meeting_id": meeting_id,
            "company": company,
            "series_title": title,
            "room_url": room,
            "start_at": _iso(start),
            "duration_minutes": duration,
            "expected_participants": participants or [],
            "owner": owner if owner is not None else {"portal_user_id": "42"},
        }
        if series:
            payload["series"] = series
        if chat_id is not None:
            payload["telegram_chat_id"] = chat_id
        clean, errors = orders_lib.validate_order_payload(
            payload, kind=kind, now=now or datetime.now(timezone.utc)
        )
        self.assertEqual(errors, [], "фикстура наряда невалидна: %s" % errors)
        return orders_lib.put_order(clean, kind=kind, now=now)

    def write_watched(self, records: List[Dict[str, Any]]) -> None:
        import yaml  # локально: реестр серий это yaml

        (self.registry / "watched.yaml").write_text(
            yaml.safe_dump({"watched": records}, allow_unicode=True), encoding="utf-8"
        )
        (self.registry / "rooms.yaml").write_text(
            yaml.safe_dump({"rooms": []}, allow_unicode=True), encoding="utf-8"
        )

    def run_scheduler(self) -> List[Dict[str, Any]]:
        """Настоящий прогон планировщика в изолированном каталоге."""
        with mock.patch.object(scheduler, "load_credentials", side_effect=scheduler.GCalAuthError("нет creds")):
            rc = scheduler.main()
        self.assertEqual(rc, 0)
        return state_lib.read_queue()

    def tick(
        self,
        now: datetime,
        *,
        live_series: Optional[List[str]] = None,
        live_rooms: Optional[List[str]] = None,
        launch_ok: bool = True,
    ) -> List[Dict[str, Any]]:
        """Один тик runner'а. Возвращает список ЗАПУЩЕННЫХ элементов очереди."""
        launched: List[Dict[str, Any]] = []
        by_series: Dict[str, List[str]] = {}
        for s in live_series or []:
            by_series.setdefault(s, []).append("vexa-notarius-%s" % s)
        by_room: Dict[str, List[str]] = {}
        for url in live_rooms or []:
            by_room.setdefault(orders_lib.room_key(url), []).append("vexa-notarius-room")
        snapshot = {
            "by_series": by_series,
            "by_room": by_room,
            "unlabeled": [],
            "total": sum(len(v) for v in by_series.values()) or len(by_room),
        }

        def _fake_launch(item, _now, seq=0):
            launched.append(dict(item))
            return launch_ok

        with mock.patch.object(runner, "_running_bots_snapshot", return_value=snapshot), \
             mock.patch.object(runner, "_launch_bot", side_effect=_fake_launch):
            rc = runner.main(now=now)
        self.assertEqual(rc, 0)
        return launched

    def queue_of(self, items: List[Dict[str, Any]]) -> None:
        state_lib.write_queue(items)


# ---------------------------------------------------------------------------


class TestQueueBeforeAfter(_Base):
    """След 1: что было в очереди до и после — на настоящем прогоне."""

    def test_portal_orders_live_next_to_series(self):
        now = datetime.now(timezone.utc)
        self.write_watched([
            {
                "id": "anzhee-direktorat",
                "series": "anzhee-direktorat",
                "type": "manual",
                "cron": "0 10 * * *",
                "tz": "Asia/Dubai",
                "room": ROOM_2,
                "enabled": True,
                "duration_minutes": 60,
            }
        ])

        self.set_flag("0")
        before = self.run_scheduler()
        self.assertTrue(before, "личная серия владельца должна разложиться в очередь")
        self.assertTrue(all(i["type"] == "manual" for i in before))

        self.put_order("mtg-1001", now + timedelta(hours=3), room=ROOM)
        self.put_order("mtg-1002", now + timedelta(hours=5), room=ROOM_3, company="m1")

        self.set_flag("1")
        after = self.run_scheduler()

        portal = [i for i in after if i.get("type") == "portal"]
        series = [i for i in after if i.get("type") == "manual"]
        self.assertEqual(len(series), len(before), "личные серии не должны пострадать")
        self.assertEqual(len(portal), 2, "оба наряда обязаны стоять в очереди")
        # По МОМЕНТУ старта: у серии пояс Дубая, у наряда UTC, и по строке «верный»
        # порядок был бы неверным по времени (находка №4 проверки 04.10.2026).
        moments = [instant_key(i["start_at"]) for i in after]
        self.assertEqual(
            moments, sorted(moments), "очередь обязана остаться отсортированной по старту",
        )
        one = next(i for i in portal if i["order_id"] == "portal:mtg-1001")
        self.assertEqual(one["url"], ROOM)
        self.assertEqual(one["company"], "anzhee")
        self.assertEqual(one["event_id"], "portal:portal:mtg-1001")
        self.assertEqual(one["series_title"], "Координация продаж")
        two = next(i for i in portal if i["order_id"] == "portal:mtg-1002")
        self.assertEqual(two["company"], "mpfirst", "портальное m1 обязано лечь каноном бота")

    def test_container_name_stays_valid_for_portal_meeting(self):
        """`meeting_id` наряда содержит двоеточие — docker такое имя не примет."""
        order, _ = self.put_order("mtg:1001:v2", datetime.now(timezone.utc) + timedelta(hours=2))
        item = portal_queue.queue_item(order)
        uid = runner._safe_session_uid(item["meeting_id"], item["start_at"])
        self.assertNotIn(":", uid)
        self.assertRegex("vexa-notarius-" + uid, r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class TestIdempotency(_Base):
    """R7.3: один наряд = один бот."""

    def test_same_order_twice_gives_one_queue_item(self):
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=2)
        first, created_1 = self.put_order("mtg-2001", start)
        second, created_2 = self.put_order("mtg-2001", start)
        self.assertTrue(created_1)
        self.assertFalse(created_2, "повтор той же постановки не создаёт второй наряд")
        self.assertEqual(first["version"], second["version"], "повтор не двигает версию")

        self.write_watched([])
        queue = self.run_scheduler()
        self.assertEqual(len(queue), 1)

    def test_second_tick_does_not_launch_second_bot(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = now + timedelta(minutes=3)
        order, _ = self.put_order("mtg-2002", start)
        self.queue_of([portal_queue.queue_item(order)])

        launched = self.tick(now)
        self.assertEqual(len(launched), 1)
        # Второй тик через минуту: бот уже живёт, ключ старта занят.
        again = self.tick(now + timedelta(minutes=1), live_series=[launched[0]["series"]])
        self.assertEqual(again, [], "второй бот на тот же наряд — запрещено")

    def test_key_is_the_registry_record_id(self):
        """Ключ идемпотентности — детерминированный id записи реестра (решение №2)."""
        order, _ = self.put_order("mtg-2003", datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(order["id"], "portal:mtg-2003")
        self.assertEqual(portal_queue.event_id_for(order), "portal:portal:mtg-2003")
        item = portal_queue.queue_item(order)
        self.assertTrue(state_lib.start_key(item).startswith("portal:portal:mtg-2003@"))

    def test_same_id_other_content_is_one_order_not_two(self):
        now = datetime.now(timezone.utc)
        self.put_order("mtg-2004", now + timedelta(hours=2))
        moved, created = self.put_order("mtg-2004", now + timedelta(hours=3), room=ROOM_2)
        self.assertFalse(created, "тот же id с другим содержимым — перенос, не вторая встреча")
        self.assertEqual(moved["version"], 2)
        self.write_watched([])
        queue = self.run_scheduler()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["url"], ROOM_2)


class TestRescheduleISS57(_Base):
    """ISS-57 / R12.9: перенос встречи, когда бот уже сидит в комнате.

    Минуты взяты из инцидента 05.08.2026 (серия `anzhee-direktorat`): бот зашёл
    в 15:10 на старт 15:15, в 15:12 встречу передвинули на 15:30, планировщик
    очередь переписал, а повторного запуска не сделал — бот вышел по «никто не
    пришёл» в 15:30:34, реальная встреча началась в 15:40.
    """

    def _timeline(self, hh: int, mm: int) -> datetime:
        """Минуты инцидента, но ЗАВТРА: наряд с временем в прошлом реестр не примет."""
        base = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        return base + timedelta(hours=hh - 12, minutes=mm)

    def test_moved_meeting_relaunches_at_new_time(self):
        t1510 = self._timeline(15, 10)
        order, _ = self.put_order("mtg-3001", self._timeline(15, 15), duration=60)
        self.queue_of([portal_queue.queue_item(order)])

        first = self.tick(t1510)
        self.assertEqual(len(first), 1, "бот обязан зайти по старому времени")
        series = first[0]["series"]

        # 15:12 — встречу передвинули на 15:30; 15:15 — планировщик переписал очередь.
        moved, created = self.put_order("mtg-3001", self._timeline(15, 30), duration=60)
        self.assertFalse(created)
        self.queue_of([portal_queue.queue_item(moved)])

        # 15:26 — новый старт в окне, но бот старого запуска ещё в комнате.
        waiting = self.tick(self._timeline(15, 26), live_series=[series])
        self.assertEqual(waiting, [], "двух ботов в комнате быть не должно")
        self.assertTrue(
            any("Жду свободный слот" in p for p in self.pushes),
            "владельцу должно быть сказано, что старт ждёт: %s" % self.pushes,
        )

        # 15:33 — старый бот вышел («никто не пришёл»). Обычное окно старта уже
        # прошло; спасает только очередь ожидания.
        relaunch = self.tick(self._timeline(15, 33))
        self.assertEqual(len(relaunch), 1, "перенос обязан привести к повторному запуску")
        self.assertEqual(relaunch[0]["start_at"], _iso(self._timeline(15, 30)))

        marked = late_join.get(series, _iso(self._timeline(15, 30))[:10])
        self.assertIsNotNone(marked, "поздний заход обязан быть отмечен")
        self.assertGreaterEqual(marked["late_minutes"], 3)

    def test_old_key_scheme_would_have_blocked_the_relaunch(self):
        """Доказательство, что чинил именно ключ старта, а не совпадение.

        Тот же перенос с ГОЛЫМ `event_id` в качестве ключа (как было до ISS-57)
        повторного запуска не даёт.
        """
        order, _ = self.put_order("mtg-3002", self._timeline(15, 15))
        item = portal_queue.queue_item(order)
        self.assertTrue(state_lib.state_acquire(item["event_id"]))  # старт по-старому
        moved, _ = self.put_order("mtg-3002", self._timeline(15, 30))
        moved_item = portal_queue.queue_item(moved)
        self.assertFalse(
            state_lib.state_acquire(moved_item["event_id"]),
            "голый event_id не меняется при переносе — в этом и был ISS-57",
        )
        self.assertTrue(
            state_lib.state_acquire(state_lib.start_key(moved_item)),
            "ключ со временем старта обязан пропустить перенесённый старт",
        )

    def test_legacy_state_entry_shields_from_double_bot_after_rollout(self):
        """Переходный щит: запись старого формата не даёт второго бота на выкатке."""
        order, _ = self.put_order("mtg-3003", self._timeline(15, 15))
        item = portal_queue.queue_item(order)
        state_lib.write_state({item["event_id"]: _iso(self._timeline(15, 10))})
        self.assertFalse(
            state_lib.state_acquire(
                state_lib.start_key(item), legacy_key=item["event_id"]
            ),
            "старт, отработанный до выкатки, не имеет права запуститься второй раз",
        )

    def test_skip_message_about_the_new_start_is_not_swallowed(self):
        """Глушилка повторов ключуется СТАРТОМ, иначе перенос теряет уведомление.

        `push(dedupe_key=…)` глушит повтор на 6 часов. Ключ по `event_id` у
        перенесённой встречи тот же самый — сообщение «жду слот» про НОВОЕ время
        утонуло бы в окне, поставленном сообщением про старое, и владелец не
        узнал бы, что бот ещё не зашёл.
        """
        order, _ = self.put_order("mtg-3006", self._timeline(15, 15), series="portal-move")
        self.queue_of([portal_queue.queue_item(order)])
        self.tick(self._timeline(15, 12), live_series=["portal-move"])
        first_keys = list(self.push_keys)

        moved, _ = self.put_order("mtg-3006", self._timeline(15, 40), series="portal-move")
        self.queue_of([portal_queue.queue_item(moved)])
        self.push_keys.clear()
        self.tick(self._timeline(15, 37), live_series=["portal-move"])

        self.assertTrue(self.push_keys, "пропуск обязан быть озвучен")
        self.assertNotIn(
            self.push_keys[-1], first_keys,
            "ключ глушилки не изменился при переносе — сообщение утонет: %s" % self.push_keys,
        )
        self.assertIn(
            _iso(self._timeline(15, 40))[:16].replace("-", "").replace(":", ""),
            self.push_keys[-1].replace("T", "T"),
            "в ключе глушилки обязана быть минута НОВОГО старта: %s" % self.push_keys[-1],
        )

    def test_reschedule_one_minute_before_start(self):
        order, _ = self.put_order("mtg-3004", self._timeline(15, 15))
        moved, _ = self.put_order("mtg-3004", self._timeline(15, 45))
        self.queue_of([portal_queue.queue_item(moved)])
        # 15:14 — старого старта в очереди уже нет, нового ещё нет в окне.
        self.assertEqual(self.tick(self._timeline(15, 14)), [])
        launched = self.tick(self._timeline(15, 42))
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0]["start_at"], _iso(self._timeline(15, 45)))

    def test_reschedule_backwards_does_not_double_book(self):
        """Перенос НАЗАД: бот уже зашёл — второго не поднимаем."""
        order, _ = self.put_order("mtg-3005", self._timeline(15, 15))
        self.queue_of([portal_queue.queue_item(order)])
        first = self.tick(self._timeline(15, 12))
        self.assertEqual(len(first), 1)
        moved, _ = self.put_order("mtg-3005", self._timeline(15, 13))
        self.queue_of([portal_queue.queue_item(moved)])
        again = self.tick(self._timeline(15, 14), live_series=[first[0]["series"]])
        self.assertEqual(again, [], "живой бот той же серии — второго не заводим")


class TestCancel(_Base):
    """R4.14 / R12.10: отмена снимает приход."""

    def test_cancelled_order_leaves_the_queue(self):
        now = datetime.now(timezone.utc)
        self.put_order("mtg-4001", now + timedelta(hours=2))
        self.write_watched([])
        self.assertEqual(len(self.run_scheduler()), 1)

        cancelled = orders_lib.cancel_order("portal:mtg-4001", reason="встречу отменили")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.run_scheduler(), [], "снятый наряд в очередь не ставится")

    def test_cancelled_order_does_not_start_a_bot(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        order, _ = self.put_order("mtg-4002", now + timedelta(minutes=3))
        self.queue_of([portal_queue.queue_item(order)])
        orders_lib.cancel_order(order["id"])
        self.write_watched([])
        self.run_scheduler()  # очередь перестроилась — наряда там нет
        self.assertEqual(self.tick(now), [], "по снятому наряду бот не приходит")

    def test_cancel_of_unknown_order_is_not_found(self):
        self.assertIsNone(orders_lib.cancel_order("portal:нет-такого"))

    def test_cancel_of_past_order_is_accepted(self):
        now = datetime.now(timezone.utc)
        clean, errors = orders_lib.validate_order_payload(
            {
                "meeting_id": "mtg-4003",
                "company": "anzhee",
                "series_title": "Прошедшая",
                "room_url": ROOM,
                "start_at": _iso(now - timedelta(hours=5)),
                "owner": {"portal_user_id": "42"},
            },
            now=now,
            check_start_in_past=False,
        )
        self.assertEqual(errors, [])
        orders_lib.put_order(clean)
        cancelled = orders_lib.cancel_order("portal:mtg-4003")
        self.assertEqual(cancelled["status"], "cancelled")

    def test_stopping_order_gets_no_new_start(self):
        """«Выйти сейчас» не имеет права вернуть бота новым стартом."""
        now = datetime.now(timezone.utc)
        self.put_order("mtg-4004", now + timedelta(hours=1))
        orders_lib.leave_now(order_id="portal:mtg-4004")
        self.write_watched([])
        self.assertEqual(self.run_scheduler(), [])

    def test_expand_skips_every_non_scheduled_status(self):
        """Вторая линия обороны, а не только фильтр чтения реестра.

        Список нарядов приходит в раскрытие извне (диагностика, будущий вызов из
        другого места) — если фильтр статуса живёт ТОЛЬКО в чтении реестра, то
        одна невнимательная правка возвращает бота на отменённую встречу.
        """
        start = datetime.now(timezone.utc) + timedelta(hours=2)
        base = {
            "id": "portal:mtg-4100", "meeting_id": "mtg-4100", "company": "anzhee",
            "series": "portal-x", "series_title": "Т", "room_url": ROOM,
            "start_at": _iso(start), "duration_minutes": 60,
        }
        horizon = (start - timedelta(hours=1), start + timedelta(hours=10))
        self.assertEqual(
            len(portal_queue.expand_orders([dict(base, status="scheduled")], *horizon)), 1
        )
        for status in ("cancelled", "stopping", "finished", "", None):
            self.assertEqual(
                portal_queue.expand_orders([dict(base, status=status)], *horizon), [],
                "наряд со статусом %r не имеет права попасть в очередь" % status,
            )

    def test_waitlisted_start_is_forgotten_when_meeting_cancelled(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        order, _ = self.put_order("mtg-4005", now + timedelta(minutes=2))
        item = portal_queue.queue_item(order)
        self.queue_of([item])
        self.tick(now, live_series=[item["series"]])  # skip_series → в ожидание
        self.assertIn(state_lib.start_key(item), waitlist.load())
        self.queue_of([])  # встречу сняли — очередь пуста
        self.tick(now + timedelta(minutes=1))
        self.assertEqual(waitlist.load(), {}, "снятый старт не должен ждать вечно")


class TestCapacity(_Base):
    """Потолок одновременных записей: определённо и видимо."""

    def _three_at_ten(self, now: datetime):
        start = now + timedelta(minutes=3)
        items = []
        for idx, room in enumerate((ROOM, ROOM_2, ROOM_3), start=1):
            order, _ = self.put_order(
                "mtg-50%02d" % idx, start, room=room, series="portal-s%d" % idx, duration=60
            )
            items.append(portal_queue.queue_item(order))
        self.queue_of(items)
        return start, items

    def test_third_meeting_waits_and_joins_late(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start, items = self._three_at_ten(now)

        launched = self.tick(now)
        self.assertEqual(len(launched), 2, "при потолке 2 стартуют ровно два бота")
        third_key = state_lib.start_key(items[2])
        self.assertIn(third_key, waitlist.load(), "третий обязан ждать, а не пропасть")
        self.assertTrue(any("Жду свободный слот" in p for p in self.pushes))

        # Через 35 минут одна из записей закончилась — слот освободился.
        late_now = start + timedelta(minutes=35)
        third = self.tick(late_now, live_series=[items[0]["series"]])
        self.assertEqual(len(third), 1, "освободился слот — бот заходит с опозданием")
        self.assertEqual(third[0]["order_id"], items[2]["order_id"])
        self.assertNotIn(third_key, waitlist.load())

        entry = late_join.get(items[2]["series"], str(items[2]["start_at"])[:10])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["reason"], "skip_capacity")
        self.assertGreaterEqual(entry["late_minutes"], 30)

    def test_late_join_is_signed_in_the_protocol(self):
        series = "portal-s9"
        start = datetime.now(timezone.utc).replace(microsecond=0)
        late_join.mark(
            series, _iso(start), joined_at=start + timedelta(minutes=17),
            late_minutes=17, reason="skip_capacity",
        )
        text = "#протоколвстречи\n\n## Итоги\n\n- решение\n"
        signed = late_join.sign(text, series, _iso(start)[:10])
        self.assertIn("с опозданием на 17 мин", signed)
        self.assertTrue(signed.startswith("#протоколвстречи"), "шапка обязана остаться первой")
        self.assertEqual(
            signed, late_join.sign(signed, series, _iso(start)[:10]),
            "перегенерация не имеет права дописать вторую отметку",
        )

    def test_protocol_without_marker_is_byte_identical(self):
        text = "#протоколвстречи\n\n## Итоги\n"
        self.assertEqual(late_join.sign(text, "нет-такой-серии", "2026-08-15"), text)

    def test_fourth_order_when_three_are_waiting(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start, items = self._three_at_ten(now)
        fourth, _ = self.put_order("mtg-5004", start, room="https://telemost.yandex.ru/j/44444444444444",
                                   series="portal-s4")
        items.append(portal_queue.queue_item(fourth))
        self.queue_of(items)

        launched = self.tick(now)
        self.assertEqual(len(launched), 2)
        wl = waitlist.load()
        self.assertEqual(len(wl), 2, "оба лишних старта ждут, ни один не потерян")

        hint = slots.occupancy(start, 60, exclude_order_id=fourth["id"])
        self.assertEqual(hint["free_slots"], 0)
        self.assertIsNotNone(hint["warning"], "порталу обязано быть сказано при постановке")

    def test_waiting_start_expires_with_an_honest_message(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start, items = self._three_at_ten(now)
        self.tick(now)
        self.pushes.clear()
        # Встреча закончилась (60 мин), слот так и не освободился.
        self.tick(start + timedelta(minutes=61), live_series=[items[0]["series"], items[1]["series"]])
        self.assertEqual(waitlist.load(), {})
        self.assertTrue(
            any("свободного слота у бота так и не появилось" in p for p in self.pushes),
            "молчать про незаписанную встречу нельзя: %s" % self.pushes,
        )

    def test_limit_is_shared_between_companies_on_purpose(self):
        """Потолок общий: он про ядра машины, а не про права компании."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = now + timedelta(minutes=3)
        a, _ = self.put_order("mtg-5101", start, room=ROOM, company="anzhee", series="a1")
        b, _ = self.put_order("mtg-5102", start, room=ROOM_2, company="m1", series="b1")
        hint = slots.occupancy(start, 60)
        self.assertEqual(hint["scope"], "all_companies")
        self.assertEqual(hint["busy"], 2)
        self.assertEqual(hint["free_slots"], 0)
        self.assertEqual(set(hint["by_company"]), {"anzhee", "mpfirst"})

    def test_capacity_counts_owner_series_from_the_queue(self):
        """Шлюз не имеет права обещать слот, занятый личной встречей владельца."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = now + timedelta(hours=2)
        self.queue_of([
            {"meeting_id": "anzhee-direktorat", "event_id": "gcal:x", "series": "anzhee-direktorat",
             "start_at": _iso(start), "duration_minutes": 60, "url": ROOM_2, "type": "google-calendar"},
            {"meeting_id": "tatiana", "event_id": "gcal:y", "series": "tatiana",
             "start_at": _iso(start), "duration_minutes": 60, "url": ROOM_3, "type": "google-calendar"},
        ])
        hint = slots.occupancy(start, 60)
        self.assertEqual(hint["busy"], 2)
        self.assertEqual(hint["by_source"], {"scheduler_queue": 2})
        self.assertIsNotNone(hint["warning"])

    def test_same_meeting_from_both_sources_counts_once(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = now + timedelta(hours=2)
        order, _ = self.put_order("mtg-5201", start, room=ROOM)
        self.queue_of([portal_queue.queue_item(order)])
        hint = slots.occupancy(start, 60)
        self.assertEqual(hint["busy"], 1, "наряд и его же строка очереди — одна встреча")

    def test_limit_is_configurable_and_defaults_to_two(self):
        self.assertEqual(slots.max_concurrent_bots(), 2)
        os.environ["MEETING_NOTARY_MAX_CONCURRENT_BOTS"] = "3"
        self.assertEqual(slots.max_concurrent_bots(), 3)
        self.assertEqual(runner.max_concurrent_bots(), 3, "runner и шлюз читают одно число")
        os.environ["MEETING_NOTARY_MAX_CONCURRENT_BOTS"] = "мусор"
        self.assertEqual(slots.max_concurrent_bots(), 2, "мусор в настройке не роняет потолок")

    def test_three_at_once_actually_launch_when_limit_raised(self):
        os.environ["MEETING_NOTARY_MAX_CONCURRENT_BOTS"] = "3"
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self._three_at_ten(now)
        self.assertEqual(len(self.tick(now)), 3)


class TestPortalOff(_Base):
    """🔴 Неизменность поведения владельца при выключателе OFF — механически."""

    flag_default = None

    def test_queue_identical_to_a_run_without_orders(self):
        now = datetime.now(timezone.utc)
        self.write_watched([
            {"id": "anzhee-direktorat", "series": "anzhee-direktorat", "type": "manual",
             "cron": "0 10 * * *", "tz": "Asia/Dubai", "room": ROOM_2, "enabled": True},
        ])
        self.put_order("mtg-6001", now + timedelta(hours=2))
        self.put_order("mtg-6002", now + timedelta(hours=4))
        for raw in OFF_FORMS:
            self.set_flag(raw)
            queue = self.run_scheduler()
            self.assertTrue(
                all(i.get("type") != "portal" for i in queue),
                "при ENABLE_PORTAL_ORDERS=%r наряд попал в очередь владельца" % raw,
            )
            self.assertTrue(queue, "личные серии обязаны остаться")

    def test_orders_registry_never_touched_when_off(self):
        """СЧЁТЧИК, а не исключение (урок Ф10a: `except Exception` съел бы assert)."""
        calls = {"n": 0}

        def _count(*_a, **_kw):
            calls["n"] += 1
            return []

        self.write_watched([])
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.queue_of([
            {"meeting_id": "anzhee-direktorat", "event_id": "gcal:z", "series": "anzhee-direktorat",
             "start_at": _iso(now + timedelta(minutes=2)), "duration_minutes": 60,
             "url": ROOM_2, "type": "google-calendar"},
        ])
        for raw in OFF_FORMS:
            self.set_flag(raw)
            with mock.patch.object(orders_lib, "list_orders", side_effect=_count), \
                 mock.patch.object(orders_lib, "orders_file", side_effect=_count):
                self.run_scheduler()
                self.tick(now)
        self.assertEqual(
            calls["n"], 0,
            "при OFF планировщик и runner %d раз пошли в реестр нарядов" % calls["n"],
        )

    def test_counter_probe_actually_counts(self):
        """Сторож сторожа: при ON тот же счётчик обязан быть ненулевым."""
        calls = {"n": 0}

        def _count(*_a, **_kw):
            calls["n"] += 1
            return []

        self.set_flag("1")
        self.write_watched([])
        with mock.patch.object(orders_lib, "list_orders", side_effect=_count):
            self.run_scheduler()
        self.assertGreater(calls["n"], 0)

    def test_owner_start_behaves_exactly_as_before_when_nothing_is_skipped(self):
        """Обычный день владельца: тот же запуск и НИ ОДНОГО нового файла на диске."""
        self.set_flag("0")
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.queue_of([
            {"meeting_id": "anzhee-direktorat", "event_id": "gcal:q", "series": "anzhee-direktorat",
             "start_at": _iso(now + timedelta(minutes=2)), "duration_minutes": 60,
             "url": ROOM_2, "type": "google-calendar"},
        ])
        before = {p.name for p in self.registry.iterdir()}
        launched = self.tick(now)
        self.assertEqual(len(launched), 1)
        after = {p.name for p in self.registry.iterdir()}
        new_files = after - before
        self.assertNotIn(waitlist.WAITLIST_FILENAME, new_files,
                         "очередь ожидания не имеет права появляться на пустом месте")
        self.assertNotIn(late_join.LATE_JOINS_FILENAME, new_files)

    def test_broken_orders_registry_does_not_kill_the_owner_queue(self):
        """Fail-open: битый реестр нарядов не отменяет личные серии."""
        self.set_flag("1")
        self.write_watched([
            {"id": "anzhee-direktorat", "series": "anzhee-direktorat", "type": "manual",
             "cron": "0 10 * * *", "tz": "Asia/Dubai", "room": ROOM_2, "enabled": True},
        ])
        (self.registry / orders_lib.ORDERS_FILENAME).write_text("{битый", encoding="utf-8")
        queue = self.run_scheduler()
        self.assertTrue(queue, "личные серии владельца обязаны доехать до очереди")

    def test_waitlist_kill_switch_returns_old_behaviour(self):
        self.set_flag("1")
        os.environ["MEETING_NOTARY_START_WAITLIST"] = "0"
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        order, _ = self.put_order("mtg-6003", now + timedelta(minutes=2), series="portal-x")
        item = portal_queue.queue_item(order)
        self.queue_of([item])
        self.tick(now, live_series=[item["series"]])
        self.assertFalse((self.registry / waitlist.WAITLIST_FILENAME).exists())
        self.assertTrue(any(p.startswith("Не записал") for p in self.pushes))


class TestQueueReaders(_Base):
    """След 5: очередь читают четверо — они не должны разъехаться."""

    def test_deep_health_stays_green_with_portal_items(self):
        import notary.gateway as gateway

        now = datetime.now(timezone.utc)
        order, _ = self.put_order("mtg-7001", now + timedelta(hours=1))
        self.queue_of([portal_queue.queue_item(order)])
        state_lib.write_last_success(now)
        ok, checks = gateway.deep_probe(now)
        self.assertTrue(ok, checks)

    def test_activity_status_sees_portal_orders(self):
        import notary.gateway as gateway

        now = datetime.now(timezone.utc)
        self.put_order("mtg-7002", now + timedelta(hours=1), company="anzhee")
        self.put_order("mtg-7003", now + timedelta(hours=2), company="m1")
        activity = gateway.activity_status(now)
        self.assertEqual(activity["orders_active"], 2)
        self.assertEqual(activity["orders_by_company"], {"anzhee": 1, "mpfirst": 1})
        self.assertTrue(activity["portal_orders_enabled"])

    def test_dedup_collapses_a_portal_twin_and_keeps_the_link(self):
        """Одна встреча из двух источников = один бот, связь с нарядом жива."""
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=1)
        series_item = {
            "meeting_id": "anzhee-direktorat", "event_id": "gcal:abc",
            "series": "anzhee-direktorat", "start_at": _iso(start),
            "duration_minutes": 60, "url": ROOM, "type": "google-calendar",
            "calendar_id": "primary", "ical_uid": None,
        }
        order, _ = self.put_order("mtg-7004", start, room=ROOM)
        portal_item = portal_queue.queue_item(order)
        out = scheduler.dedupe_queue([series_item, portal_item], memory={}, now=start - timedelta(minutes=30))
        self.assertEqual(len(out), 1, "два элемента на одну комнату и секунду = один бот")
        self.assertEqual(out[0]["meeting_id"], "anzhee-direktorat", "личная серия ведёт встречу")
        self.assertEqual(out[0]["order_id"], order["id"], "связь с нарядом обязана уцелеть")

    def test_gateway_capacity_hint_matches_runner_limit(self):
        import notary.gateway as gateway

        os.environ["MEETING_NOTARY_MAX_CONCURRENT_BOTS"] = "4"
        self.assertEqual(gateway.max_concurrent_bots(), runner.max_concurrent_bots())

    def test_immediate_order_is_picked_up_without_waiting_for_scheduler(self):
        """«Зайти сейчас» не имеет права ждать тика планировщика (раз в 3 минуты)."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        order, _ = self.put_order(
            "mtg-7005", now, kind=orders_lib.KIND_IMMEDIATE, now=now, series="portal-now"
        )
        self.assertEqual(order["kind"], "immediate")
        self.queue_of([])  # планировщик ещё не отработал
        launched = self.tick(now + timedelta(minutes=7))
        self.assertEqual(len(launched), 1, "кнопка «подключить сейчас» обязана поднять бота")
        self.assertEqual(launched[0]["order_id"], order["id"])
        # Повтор через минуту — второго бота нет.
        self.assertEqual(self.tick(now + timedelta(minutes=8), live_series=["portal-now"]), [])


class TestSecondBotInOneRoom(_Base):
    """Второго бота на один разговор не бывает — даже кнопкой «подключить сейчас»."""

    def test_join_now_into_a_room_that_is_already_being_recorded(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        order, _ = self.put_order(
            "mtg-9101", now, kind=orders_lib.KIND_IMMEDIATE, now=now,
            room=ROOM, series="portal-join",
        )
        self.queue_of([])
        launched = self.tick(now + timedelta(minutes=1), live_rooms=[ROOM])
        self.assertEqual(launched, [], "в комнате уже пишут — второй бот запрещён")
        self.assertTrue(any("уже пишет" in p for p in self.pushes), self.pushes)
        self.assertEqual(waitlist.load(), {}, "ждать тут нечего — разговор уже пишется")
        # Комната освободилась — бот заходит.
        again = self.tick(now + timedelta(minutes=2))
        self.assertEqual(len(again), 1)

    def test_room_query_string_does_not_fool_the_check(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.put_order(
            "mtg-9102", now, kind=orders_lib.KIND_IMMEDIATE, now=now,
            room=ROOM + "?utm=portal", series="portal-join2",
        )
        self.queue_of([])
        self.assertEqual(self.tick(now + timedelta(minutes=1), live_rooms=[ROOM]), [])

    def test_scheduled_meeting_is_not_blocked_by_a_busy_room(self):
        """Расписание по комнате НЕ гейтим: зависший бот иначе съел бы серию."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        order, _ = self.put_order("mtg-9103", now + timedelta(minutes=2), room=ROOM,
                                  series="portal-sched")
        self.queue_of([portal_queue.queue_item(order)])
        self.assertEqual(len(self.tick(now, live_rooms=[ROOM])), 1)

    def test_launch_puts_the_room_label_on_the_container(self):
        self.assertEqual(
            runner._room_label_value(ROOM + "?utm=x"), orders_lib.room_key(ROOM)
        )
        self.assertEqual(runner._room_label_value(""), "")


class TestInvalidInputs(_Base):
    """След 2: враждебные входы. Каждый обязан вести себя ОПРЕДЕЛЁННО."""

    def test_order_in_the_past_is_rejected(self):
        now = datetime.now(timezone.utc)
        _, errors = orders_lib.validate_order_payload(
            {"meeting_id": "m", "company": "anzhee", "series_title": "t", "room_url": ROOM,
             "start_at": _iso(now - timedelta(hours=1)), "owner": {"portal_user_id": "1"}},
            now=now,
        )
        self.assertEqual([e["code"] for e in errors], ["in_past"])

    def test_order_a_year_away_is_rejected(self):
        now = datetime.now(timezone.utc)
        _, errors = orders_lib.validate_order_payload(
            {"meeting_id": "m", "company": "anzhee", "series_title": "t", "room_url": ROOM,
             "start_at": _iso(now + timedelta(days=500)), "owner": {"portal_user_id": "1"}},
            now=now,
        )
        self.assertEqual([e["code"] for e in errors], ["too_far"])

    def test_order_without_owner_is_rejected_when_on(self):
        now = datetime.now(timezone.utc)
        _, errors = orders_lib.validate_order_payload(
            {"meeting_id": "m", "company": "anzhee", "series_title": "t", "room_url": ROOM,
             "start_at": _iso(now + timedelta(hours=1))},
            now=now,
        )
        self.assertIn("owner", [e["field"] for e in errors])

    def test_order_far_beyond_horizon_is_not_queued(self):
        now = datetime.now(timezone.utc)
        self.put_order("mtg-8001", now + timedelta(days=10))
        self.write_watched([])
        self.assertEqual(self.run_scheduler(), [], "за горизонтом 48ч наряд в очередь не идёт")

    def test_order_with_broken_time_is_skipped_not_crashing(self):
        self.assertIsNone(portal_queue.queue_item({"id": "portal:x", "start_at": "вчера", "room_url": ROOM}))
        self.assertIsNone(portal_queue.queue_item({"id": "portal:x", "start_at": None, "room_url": ROOM}))
        self.assertIsNone(portal_queue.queue_item({"id": "portal:x", "start_at": "2026-08-20T10:00:00+03:00"}))
        self.assertIsNone(portal_queue.queue_item("не словарь"))

    def test_one_broken_order_does_not_lose_the_others(self):
        now = datetime.now(timezone.utc)
        good, _ = self.put_order("mtg-8002", now + timedelta(hours=2))
        broken = {"id": "portal:bad", "status": "scheduled", "start_at": "вчера", "room_url": ROOM}
        items = portal_queue.expand_orders([broken, good], now, now + timedelta(hours=48))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["order_id"], good["id"])

    def test_plain_start_in_the_past_is_not_launched_late(self):
        """Широкое окно — привилегия РАЗОВОГО наряда, а не всех подряд.

        Обычный старт, проехавший окно и никого не ждущий, поднимать нельзя:
        бот пришёл бы к середине встречи без всякой причины (и без подписи об
        опоздании — её ставит только очередь ожидания).
        """
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        order, _ = self.put_order("mtg-8100", now + timedelta(minutes=2), series="portal-old")
        self.queue_of([portal_queue.queue_item(order)])
        self.assertEqual(self.tick(now + timedelta(minutes=12)), [])
        self.assertEqual(waitlist.load(), {}, "никто не ждал — ждать и не должен")

    def test_immediate_start_in_the_past_is_launched_within_its_window(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.put_order("mtg-8101", now, kind=orders_lib.KIND_IMMEDIATE, now=now,
                       series="portal-imm")
        self.queue_of([])
        self.assertEqual(len(self.tick(now + timedelta(minutes=12))), 1)
        # А через 25 минут кнопка уже протухла.
        state_lib.write_state({})
        self.assertEqual(self.tick(now + timedelta(minutes=25)), [])

    def test_queue_item_without_start_is_ignored_by_runner(self):
        now = datetime.now(timezone.utc)
        self.queue_of([{"meeting_id": "x", "event_id": "e", "series": "s", "url": ROOM}])
        self.assertEqual(self.tick(now), [])

    def test_running_meeting_stays_in_queue_until_it_ends(self):
        """Ждущий старт не имеет права исчезнуть из очереди через минуту после начала."""
        now = datetime.now(timezone.utc)
        order, _ = self.put_order("mtg-8003", now + timedelta(minutes=1), duration=60)
        later = now + timedelta(minutes=30)
        items = portal_queue.expand_orders([order], later, later + timedelta(hours=48))
        self.assertEqual(len(items), 1, "встреча ещё идёт — старт обязан остаться видимым")
        after_end = now + timedelta(minutes=90)
        self.assertEqual(portal_queue.expand_orders([order], after_end, after_end + timedelta(hours=48)), [])

    def test_timezone_forms_of_one_start_give_one_key(self):
        a = {"event_id": "e", "start_at": "2026-08-20T10:00:00+03:00"}
        b = {"event_id": "e", "start_at": "2026-08-20T07:00:00Z"}
        self.assertEqual(state_lib.start_key(a), state_lib.start_key(b))

    def test_start_key_degrades_to_event_id_without_start(self):
        self.assertEqual(state_lib.start_key({"event_id": "e"}), "e")


class TestNoTextsInLogs(_Base):
    """Опасная тройка: в журнал уходят идентификаторы и минуты, не люди."""

    def test_participant_names_never_reach_the_log(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        secret_name = "Михаил Еремеев"
        order, _ = self.put_order(
            "mtg-9001", now + timedelta(minutes=2), participants=[secret_name],
            series="portal-log", title="Разбор рекламации",
        )
        item = portal_queue.queue_item(order)
        self.queue_of([item])

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(handler)
        prev_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            self.tick(now, live_series=[item["series"]])          # уходит в ожидание
            self.tick(now + timedelta(minutes=90))                # протухает
        finally:
            root.removeHandler(handler)
            root.setLevel(prev_level)
        written = buf.getvalue()
        self.assertNotIn(secret_name, written)
        self.assertNotIn("Разбор рекламации", written)


if __name__ == "__main__":
    unittest.main()
