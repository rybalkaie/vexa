"""Шлюз Ватсона (Ф9): наряды. Постановка, правка, снятие, «зайти/выйти сейчас».

Требования: R7.2 (портал ставит/меняет/снимает наряд), R12.6 (повторное «Подключить
сейчас» по той же комнате не заводит второго бота), R12.5 (комната — ссылка, и без
годной ссылки наряда нет).

Каждая проба идёт по ЖИВОМУ HTTP на временном реестре: коды ответов и форма тела —
это и есть контракт для клиента портала (`docs/gateway-contract.md`).
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.tests.gateway_harness import (  # noqa: E402
    OMIT,
    EnvSandbox,
    LiveGateway,
    order_payload,
)

import notary.lib.orders as orders  # noqa: E402

ROOM_A = "https://telemost.yandex.ru/j/11111111111111"
ROOM_B = "https://telemost.yandex.ru/j/22222222222222"


class _GatewayCase(unittest.TestCase):
    def setUp(self) -> None:
        self.env = EnvSandbox()
        self.gw = LiveGateway()

    def tearDown(self) -> None:
        self.gw.stop()
        self.env.restore()

    def create(self, **overrides):
        return self.gw.request("POST", "/orders", body=order_payload(**overrides))

    def assert_field_error(self, resp, field: str, code: str = None) -> None:
        self.assertEqual(resp.status, 422, resp.body[:400])
        payload = resp.json()
        fields = {e["field"]: e["code"] for e in payload["errors"]}
        self.assertIn(field, fields, "ошибки: %s" % fields)
        if code is not None:
            self.assertEqual(fields[field], code)


class TestOrderLifecycle(_GatewayCase):
    """R7.2: поставить → увидеть в реестре → изменить → снять."""

    def test_create_puts_order_into_registry(self) -> None:
        resp = self.create()
        self.assertEqual(resp.status, 201, resp.body[:400])
        order = resp.json()["order"]
        self.assertEqual(order["id"], "portal:mtg-1001")
        self.assertEqual(order["status"], "scheduled")
        self.assertEqual(order["company"], "anzhee")
        self.assertEqual(order["series_title"], "Координация продаж")
        self.assertEqual(order["source"], "portal")
        # Не «шлюз ответил 201», а «наряд лежит на диске» — портал ждёт именно этого.
        on_disk = orders.get_order("portal:mtg-1001")
        self.assertIsNotNone(on_disk)
        self.assertEqual(on_disk["room_url"], ROOM_A)
        self.assertTrue((self.env.registry_dir / ".orders.json").exists())

    def test_repeat_of_the_same_order_does_not_create_second(self) -> None:
        """Портал имеет право повторить запрос после таймаута сети.

        Мутация, которая красит: выдавать новому наряду случайный id вместо
        детерминированного `portal:<meeting_id>` — тогда на встречу приедут два бота.
        """
        payload = order_payload()
        first = self.gw.request("POST", "/orders", body=payload)
        self.assertEqual(first.status, 201)
        again = self.gw.request("POST", "/orders", body=payload)
        self.assertEqual(again.status, 200)
        self.assertFalse(again.json()["created"])
        self.assertEqual(len(orders.list_orders()), 1)
        self.assertEqual(
            again.json()["order"]["version"], first.json()["order"]["version"]
        )

    def test_repost_with_changed_time_is_a_move_not_a_second_bot(self) -> None:
        self.create()
        moved = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        resp = self.create(start_at=moved)
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.json()["created"])
        self.assertEqual(len(orders.list_orders()), 1)
        self.assertEqual(resp.json()["order"]["start_at"], moved)
        self.assertEqual(resp.json()["order"]["version"], 2)

    def test_repost_with_changed_room_moves_room_key_too(self) -> None:
        """Ключ комнаты обязан ехать за ссылкой.

        Мутация, которая красит: не пересчитывать `room_key` в ветке переноса —
        тогда «зайти сейчас» по НОВОЙ комнате завело бы второй наряд, а «выйти»
        по ней же не нашло бы ни одного.
        """
        self.create()
        resp = self.create(room_url=ROOM_B)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["order"]["room_key"], orders.room_key(ROOM_B))

    def test_repost_with_changed_participants_is_applied(self) -> None:
        """Смена списка участников не смеет пропадать «по идемпотентности»."""
        self.create()
        resp = self.create(expected_participants=["Илья Рыбалка", "Новый Участник"])
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            orders.get_order("portal:mtg-1001")["expected_participants"],
            ["Илья Рыбалка", "Новый Участник"],
        )

    def test_get_one_and_list(self) -> None:
        self.create()
        self.create(meeting_id="mtg-1002", company="mpfirst")
        one = self.gw.request("GET", "/orders/portal:mtg-1001")
        self.assertEqual(one.status, 200)
        self.assertEqual(one.json()["order"]["id"], "portal:mtg-1001")

        all_orders = self.gw.request("GET", "/orders")
        self.assertEqual(all_orders.json()["count"], 2)

        by_company = self.gw.request("GET", "/orders?company=mpfirst")
        self.assertEqual(by_company.json()["count"], 1)

        by_status = self.gw.request("GET", "/orders?status=scheduled")
        self.assertEqual(by_status.json()["count"], 2)

    def test_update_changes_time_and_bumps_version(self) -> None:
        created = self.create().json()["order"]
        moved = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        resp = self.gw.request(
            "PUT",
            "/orders/portal:mtg-1001",
            body={"start_at": moved, "expected_version": created["version"]},
        )
        self.assertEqual(resp.status, 200, resp.body[:400])
        order = resp.json()["order"]
        self.assertEqual(order["start_at"], moved)
        self.assertEqual(order["version"], created["version"] + 1)
        # Поля, которых не было в правке, сохранились.
        self.assertEqual(order["series_title"], "Координация продаж")
        self.assertEqual(order["company"], "anzhee")

    def test_update_with_stale_version_is_409(self) -> None:
        self.create()
        resp = self.gw.request(
            "PUT", "/orders/portal:mtg-1001", body={"duration_minutes": 30, "expected_version": 99}
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.json()["error"], "version_conflict")
        self.assertEqual(resp.json()["current_version"], 1)

    def test_update_cannot_change_meeting_id(self) -> None:
        """На `meeting_id` держится идемпотентность — подменить его нельзя."""
        self.create()
        resp = self.gw.request(
            "PUT", "/orders/portal:mtg-1001", body={"meeting_id": "mtg-other"}
        )
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.json()["error"], "meeting_id_immutable")

    def test_update_of_past_meeting_is_allowed_when_time_not_touched(self) -> None:
        """Правка имени серии задним числом не должна отбиваться «время в прошлом»."""
        self.create()
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        orders.update_order("portal:mtg-1001", {"start_at": past})
        resp = self.gw.request(
            "PUT", "/orders/portal:mtg-1001", body={"series_title": "Координация (правка)"}
        )
        self.assertEqual(resp.status, 200, resp.body[:400])
        self.assertEqual(resp.json()["order"]["series_title"], "Координация (правка)")

    def test_update_of_unknown_order_is_404(self) -> None:
        resp = self.gw.request("PUT", "/orders/portal:no-such", body={"duration_minutes": 30})
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json()["error"], "order_not_found")

    def test_cancel_marks_order_cancelled(self) -> None:
        self.create()
        resp = self.gw.request("DELETE", "/orders/portal:mtg-1001")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["order"]["status"], "cancelled")
        self.assertEqual(orders.get_order("portal:mtg-1001")["status"], "cancelled")
        self.assertEqual(len(orders.list_orders(statuses=orders.ACTIVE_STATUSES)), 0)

    def test_cancel_of_unknown_order_is_404(self) -> None:
        resp = self.gw.request("DELETE", "/orders/portal:no-such")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json()["error"], "order_not_found")

    def test_get_unknown_order_is_404(self) -> None:
        self.assertEqual(self.gw.request("GET", "/orders/portal:no-such").status, 404)

    def test_unknown_status_filter_is_400(self) -> None:
        resp = self.gw.request("GET", "/orders?status=vydumannyy")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.json()["error"], "unknown_status")


class TestOrderValidation(_GatewayCase):
    """Невалидные входы постановки наряда — каждый со своей пробой."""

    def test_order_without_company_is_refused(self) -> None:
        """🔴 Компания обязательна и дефолта НЕ имеет.

        Мутация, которая красит: подставить `anzhee` по умолчанию — тогда протокол
        встречи МПервого молча уехал бы в чат Anzhee.
        """
        self.assert_field_error(self.create(company=OMIT), "company", "required")
        self.assert_field_error(self.create(company=""), "company", "required")
        self.assert_field_error(self.create(company=None), "company", "required")
        self.assertEqual(orders.list_orders(), [], "отвергнутый наряд всё-таки лёг в реестр")

    def test_unknown_company_is_refused(self) -> None:
        self.assert_field_error(self.create(company="gazprom"), "company", "unknown")
        self.assert_field_error(self.create(company=42), "company", "unknown")

    def test_known_company_case_and_spaces_are_tolerated(self) -> None:
        resp = self.create(company="  Anzhee ")
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.json()["order"]["company"], "anzhee")

    def test_portal_company_code_m1_is_translated_to_bot_canon(self) -> None:
        """🔴 У портала МПервый называется `m1`, у бота — `mpfirst`.

        Портал шлёт `slug` организации (`prisma/seed-organization.ts:1204` = `m1`),
        бот знает только `mpfirst` (`notary.cli.registry.VALID_COMPANIES`). Без
        перевода на границе наряд МПервого отбивался бы как «неизвестная компания»,
        и половина компаний не смогла бы позвать бота вовсе.

        Мутация, которая красит: убрать `canonical_company` из валидатора — наряд
        `m1` получит 422. Убрать его из записи — в реестр ляжет `m1`, и ниже по
        течению (доставка протокола, знание компании) появится второе слово для
        одной компании.
        """
        for index, portal_code in enumerate(("m1", "M1", "  m1  ", "МПервый")):
            with self.subTest(portal_code):
                resp = self.create(meeting_id="mtg-alias-%d" % index, company=portal_code)
                self.assertEqual(resp.status, 201, resp.body[:400])
                # В реестр лёг КАНОН бота, а не слово портала.
                self.assertEqual(resp.json()["order"]["company"], "mpfirst")

    def test_alias_and_canon_are_the_same_company_for_filters(self) -> None:
        """Фильтр по компании не должен делить МПервый надвое."""
        self.create(meeting_id="mtg-canon", company="mpfirst")
        self.create(meeting_id="mtg-alias", company="m1")
        found = self.gw.request("GET", "/orders?company=mpfirst").json()
        self.assertEqual(found["count"], 2, "псевдоним и канон разъехались в реестре")

    def test_order_without_human_series_title_is_refused(self) -> None:
        """🔴 Без человеческого имени в шапке PDF встанет транслит slug'а."""
        self.assert_field_error(self.create(series_title=OMIT), "series_title", "required")
        self.assert_field_error(self.create(series_title="   "), "series_title", "required")
        self.assert_field_error(self.create(series_title="и" * 201), "series_title", "too_long")

    def test_order_without_room_is_refused(self) -> None:
        """R12.5: без комнаты наряда нет — бот не знает, куда ехать."""
        self.assert_field_error(self.create(room_url=OMIT), "room_url", "required")
        self.assert_field_error(self.create(room_url=""), "room_url", "required")
        self.assert_field_error(self.create(room_url="   "), "room_url", "required")

    def test_plain_http_room_is_refused(self) -> None:
        self.assert_field_error(
            self.create(room_url="http://telemost.yandex.ru/j/1"), "room_url", "not_https"
        )

    def test_room_on_unsupported_platform_is_refused(self) -> None:
        """Бот умеет только Телемост: ссылка на Zoom — это бот, приехавший в пустоту."""
        self.assert_field_error(
            self.create(room_url="https://zoom.us/j/123"), "room_url", "unsupported_host"
        )

    def test_room_host_allowlist_is_extendable_by_env(self) -> None:
        os.environ["MEETING_NOTARY_GATEWAY_ROOM_HOSTS"] = "telemost.yandex.ru,meet.example.com"
        resp = self.create(room_url="https://meet.example.com/room-7")
        self.assertEqual(resp.status, 201, resp.body[:300])

    def test_meeting_id_must_be_sane(self) -> None:
        self.assert_field_error(self.create(meeting_id=OMIT), "meeting_id", "invalid")
        self.assert_field_error(self.create(meeting_id=""), "meeting_id", "invalid")
        self.assert_field_error(self.create(meeting_id="../../etc/passwd"), "meeting_id", "invalid")
        self.assert_field_error(self.create(meeting_id=12345), "meeting_id", "invalid")

    def test_series_slug_must_be_path_safe(self) -> None:
        """Slug уезжает в путь файла протокола — «..» и слэши тут недопустимы."""
        self.assert_field_error(self.create(series="../побег"), "series", "invalid")
        self.assert_field_error(self.create(series="Верхний Регистр"), "series", "invalid")

    def test_series_slug_is_derived_when_missing(self) -> None:
        resp = self.create(series=OMIT)
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.json()["order"]["series"], "portal-mtg-1001")

    def test_start_time_must_be_present_correct_and_zoned(self) -> None:
        self.assert_field_error(self.create(start_at=OMIT), "start_at", "required")
        self.assert_field_error(self.create(start_at="завтра"), "start_at", "invalid")
        self.assert_field_error(self.create(start_at="2026-08-20T10:00:00"), "start_at", "invalid")

    def test_start_time_in_past_is_refused(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assert_field_error(self.create(start_at=past), "start_at", "in_past")

    def test_start_time_beyond_horizon_is_refused(self) -> None:
        far = (datetime.now(timezone.utc) + timedelta(days=500)).isoformat()
        self.assert_field_error(self.create(start_at=far), "start_at", "too_far")

    def test_duration_must_be_a_sane_integer(self) -> None:
        self.assert_field_error(self.create(duration_minutes=0), "duration_minutes", "out_of_range")
        self.assert_field_error(self.create(duration_minutes=-5), "duration_minutes", "out_of_range")
        self.assert_field_error(self.create(duration_minutes=10**6), "duration_minutes", "out_of_range")
        self.assert_field_error(self.create(duration_minutes="45"), "duration_minutes", "invalid")
        self.assert_field_error(self.create(duration_minutes=True), "duration_minutes", "invalid")

    def test_participants_must_be_a_list_of_names(self) -> None:
        self.assert_field_error(self.create(expected_participants="Илья"), "expected_participants", "invalid")
        self.assert_field_error(self.create(expected_participants=[""]), "expected_participants", "invalid")
        self.assert_field_error(
            self.create(expected_participants=["Имя"] * 101), "expected_participants", "too_many"
        )

    def test_telegram_chat_id_must_be_integer(self) -> None:
        self.assert_field_error(self.create(telegram_chat_id="-1001"), "telegram_chat_id", "invalid")
        resp = self.create(telegram_chat_id=-1001234567890)
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.json()["order"]["telegram_chat_id"], -1001234567890)

    def test_all_errors_come_at_once(self) -> None:
        """Портал показывает ошибки формы человеку — «по одной за запрос» это три круга."""
        resp = self.gw.request(
            "POST", "/orders", body={"meeting_id": "mtg-x", "duration_minutes": -1}
        )
        self.assertEqual(resp.status, 422)
        fields = {e["field"] for e in resp.json()["errors"]}
        self.assertTrue({"company", "series_title", "room_url", "start_at"} <= fields, fields)


class TestJoinAndLeaveNow(_GatewayCase):
    """R12.6 + РЗ-4 (транспортная половина): «зайти сейчас» и «выйти сейчас»."""

    def join(self, **overrides):
        payload = order_payload(**overrides)
        payload.pop("start_at", None)
        return self.gw.request("POST", "/orders/join-now", body=payload)

    def test_join_now_creates_immediate_order(self) -> None:
        resp = self.join()
        self.assertEqual(resp.status, 201, resp.body[:400])
        order = resp.json()["order"]
        self.assertEqual(order["kind"], "immediate")
        self.assertEqual(order["status"], "scheduled")
        self.assertFalse(resp.json()["already_running"])
        # Время старта проставляет сам шлюз: «сейчас», а не то, что прислал портал.
        self.assertLessEqual(
            abs(
                (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(order["start_at"])
                ).total_seconds()
            ),
            60,
        )

    def test_second_join_into_the_same_room_returns_the_same_order(self) -> None:
        """🔴 R12.6: второе нажатие не поднимает второго бота.

        Мутация, которая красит: убрать поиск живого наряда по комнате в
        `join_now` — тогда в комнате окажется два бота.
        """
        first = self.join().json()["order"]
        second = self.join()
        self.assertEqual(second.status, 200)
        self.assertFalse(second.json()["created"])
        self.assertTrue(second.json()["already_running"])
        self.assertEqual(second.json()["order"]["id"], first["id"])
        self.assertEqual(len(orders.list_orders()), 1)

    def test_second_join_by_another_person_returns_the_same_order(self) -> None:
        """«И вторым человеком тоже» — критерий R12.6 дословно."""
        first = self.join(meeting_id="mtg-ilya").json()["order"]
        second = self.join(meeting_id="mtg-maria", series_title="Летучка Марии")
        self.assertEqual(second.status, 200)
        self.assertTrue(second.json()["already_running"])
        self.assertEqual(second.json()["order"]["id"], first["id"])
        self.assertEqual(len(orders.list_orders()), 1, "второй человек завёл второй наряд")

    def test_join_into_another_room_is_a_separate_order(self) -> None:
        self.join(meeting_id="mtg-room-a")
        resp = self.join(meeting_id="mtg-room-b", room_url=ROOM_B)
        self.assertEqual(resp.status, 201)
        self.assertEqual(len(orders.list_orders()), 2)

    def test_same_room_with_utm_tail_is_still_the_same_room(self) -> None:
        first = self.join().json()["order"]
        second = self.join(
            meeting_id="mtg-utm", room_url=ROOM_A + "/?utm_source=portal#frag"
        )
        self.assertTrue(second.json()["already_running"])
        self.assertEqual(second.json()["order"]["id"], first["id"])

    def test_stale_immediate_order_does_not_block_the_room_forever(self) -> None:
        """Забытый наряд не имеет права навсегда закрыть кнопку по этой комнате."""
        self.join()
        long_ago = datetime.now(timezone.utc) - timedelta(
            minutes=orders.IMMEDIATE_TTL_MINUTES + 10
        )
        orders.update_order("portal:mtg-1001", {"start_at": long_ago.isoformat()})
        resp = self.join(meeting_id="mtg-new")
        self.assertEqual(resp.status, 201)

    def test_join_without_company_is_refused(self) -> None:
        self.assert_field_error(self.join(company=OMIT), "company", "required")

    def test_join_without_room_is_refused(self) -> None:
        self.assert_field_error(self.join(room_url=""), "room_url", "required")

    def test_leave_now_by_meeting_id_marks_stopping(self) -> None:
        self.join()
        resp = self.gw.request("POST", "/orders/leave-now", body={"meeting_id": "mtg-1001"})
        self.assertEqual(resp.status, 200, resp.body[:400])
        order = resp.json()["order"]
        self.assertEqual(order["status"], "stopping")
        self.assertTrue(order["stop_requested_at"], "нет отметки времени запроса на выход")

    def test_leave_now_by_room_url_marks_stopping(self) -> None:
        self.join()
        resp = self.gw.request("POST", "/orders/leave-now", body={"room_url": ROOM_A})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["order"]["status"], "stopping")

    def test_repeated_leave_now_is_harmless(self) -> None:
        self.join()
        first = self.gw.request("POST", "/orders/leave-now", body={"meeting_id": "mtg-1001"})
        second = self.gw.request("POST", "/orders/leave-now", body={"meeting_id": "mtg-1001"})
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(second.json()["order"]["status"], "stopping")

    def test_leave_now_without_target_is_400(self) -> None:
        resp = self.gw.request("POST", "/orders/leave-now", body={})
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.json()["error"], "target_required")

    def test_leave_now_for_unknown_meeting_is_404(self) -> None:
        resp = self.gw.request("POST", "/orders/leave-now", body={"meeting_id": "mtg-no-such"})
        self.assertEqual(resp.status, 404)

    def test_leave_now_for_unknown_room_is_404(self) -> None:
        resp = self.gw.request("POST", "/orders/leave-now", body={"room_url": ROOM_B})
        self.assertEqual(resp.status, 404)

    def test_leave_now_does_not_resurrect_cancelled_order(self) -> None:
        """«Выйти» из снятого наряда — 404, а не возврат его в живые.

        Мутация, которая красит: убрать проверку статуса в `leave_now` — снятая
        встреча снова стала бы активной (`stopping` входит в живые статусы), и
        планировщик увидел бы её опять.
        """
        self.create()
        self.gw.request("DELETE", "/orders/portal:mtg-1001")
        resp = self.gw.request("POST", "/orders/leave-now", body={"meeting_id": "mtg-1001"})
        self.assertEqual(resp.status, 404)
        self.assertEqual(orders.get_order("portal:mtg-1001")["status"], "cancelled")


class TestCapacityHint(_GatewayCase):
    """Лимит одновременных записей виден ПРИ ПОСТАНОВКЕ, а не молчанием до встречи."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["MEETING_NOTARY_MAX_CONCURRENT_BOTS"] = "2"

    def test_free_slots_are_reported(self) -> None:
        start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        first = self.create(meeting_id="mtg-a", start_at=start)
        self.assertEqual(first.json()["capacity"]["free_slots"], 2)
        self.assertIsNone(first.json()["capacity"]["warning"])

    def test_third_overlapping_order_gets_a_warning(self) -> None:
        start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.create(meeting_id="mtg-a", start_at=start, room_url=ROOM_A)
        self.create(meeting_id="mtg-b", start_at=start, room_url=ROOM_B)
        third = self.create(
            meeting_id="mtg-c", start_at=start, room_url="https://telemost.yandex.ru/j/33333333333333"
        )
        capacity = third.json()["capacity"]
        self.assertEqual(capacity["overlapping_orders"], 2)
        self.assertEqual(capacity["free_slots"], 0)
        self.assertIn("занято", capacity["warning"].lower().replace("запланировано", "занято"))
        self.assertEqual(third.status, 201, "предупреждение не должно превращаться в отказ")

    def test_non_overlapping_orders_do_not_warn(self) -> None:
        start = datetime.now(timezone.utc) + timedelta(hours=2)
        self.create(meeting_id="mtg-a", start_at=start.isoformat(), duration_minutes=30)
        later = (start + timedelta(hours=3)).isoformat()
        resp = self.create(meeting_id="mtg-b", start_at=later, room_url=ROOM_B)
        self.assertIsNone(resp.json()["capacity"]["warning"])

    def test_cancelled_order_frees_the_slot(self) -> None:
        start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.create(meeting_id="mtg-a", start_at=start)
        self.gw.request("DELETE", "/orders/portal:mtg-a")
        resp = self.create(meeting_id="mtg-b", start_at=start, room_url=ROOM_B)
        self.assertEqual(resp.json()["capacity"]["overlapping_orders"], 0)


class TestProtocolAndStatus(_GatewayCase):
    """Отдать протокол по запросу + статус привязок и активности (Ф12/Ф13a)."""

    def test_protocol_requires_meeting_id(self) -> None:
        resp = self.gw.request("GET", "/protocol")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.json()["error"], "meeting_id_required")

    def test_protocol_of_unknown_meeting_is_404(self) -> None:
        resp = self.gw.request("GET", "/protocol?meeting_id=mtg-no-such")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json()["error"], "order_not_found")

    def test_protocol_not_ready_is_404_with_reason(self) -> None:
        self.create()
        resp = self.gw.request("GET", "/protocol?meeting_id=mtg-1001")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json()["error"], "protocol_not_ready")

    def test_protocol_is_returned_as_markdown(self) -> None:
        order = self.create().json()["order"]
        day = order["start_at"][:10]
        folder = self.env.protocols_dir / order["series"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ("%s-protokol.md" % day)).write_text(
            "# Протокол\n\nРешили: выкатить шлюз.\n", encoding="utf-8"
        )
        resp = self.gw.request("GET", "/protocol?meeting_id=mtg-1001")
        self.assertEqual(resp.status, 200, resp.body[:300])
        self.assertIn("text/markdown", resp.header("Content-Type") or "")
        self.assertIn("Решили", resp.body.decode("utf-8"))

    def test_unknown_protocol_format_is_400(self) -> None:
        self.create()
        resp = self.gw.request("GET", "/protocol?meeting_id=mtg-1001&format=docx")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.json()["error"], "bad_format")

    def test_status_reports_activity_and_orders(self) -> None:
        self.create()
        resp = self.gw.request("GET", "/status")
        self.assertEqual(resp.status, 200, resp.body[:300])
        payload = resp.json()
        self.assertIn("bindings", payload)
        self.assertIn("activity", payload)
        self.assertEqual(payload["activity"]["orders_active"], 1)
        self.assertEqual(payload["activity"]["orders_by_company"], {"anzhee": 1})
        self.assertEqual(payload["activity"]["max_concurrent_bots"], 2)

    def test_status_never_leaks_telegram_chat_ids(self) -> None:
        """Порталу нужен факт «привязка есть», а не id приватного чата.

        Мутация, которая красит: отдать `telegram_chat_id` из `watched.yaml`
        наружу — это утечка адресов личных чатов на ровном месте.
        """
        (self.env.registry_dir / "watched.yaml").write_text(
            "watched:\n"
            "  - id: seriya-1\n"
            "    series: seriya-1\n"
            "    type: weekly\n"
            "    telegram_chat_id: -1009999999999\n",
            encoding="utf-8",
        )
        resp = self.gw.request("GET", "/status")
        self.assertNotIn("1009999999999", resp.body.decode("utf-8"))
        series = resp.json()["bindings"]["series"]
        self.assertEqual(len(series), 1)
        self.assertTrue(series[0]["telegram_chat_bound"])


if __name__ == "__main__":
    unittest.main()
