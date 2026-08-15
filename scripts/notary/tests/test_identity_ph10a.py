"""Ф10a: слой идентичности Ватсона — «кто это и можно ли ему».

Главная проба фазы — НЕ «портальный сотрудник получил права», а
`TestLegacyEquivalence`: при `ENABLE_PORTAL_ORDERS=OFF` каждая функция слоя
отвечает БУКВА В БУКВУ так же, как отвечало старое сравнение со скаляром
`allowed_chat`. Ватсон — сервис ежедневного пользования владельца: регресс здесь
замечает он на своей встрече, а не приёмка. Поэтому неизменность доказывается
механически, на полной матрице входов, а не заверением «правил аккуратно».

Второй по важности сторож — `TestNoDiskWhenOff`: при OFF слой не обращается к
реестру нарядов ВООБЩЕ. Это делает «выключено» структурным свойством, а не
обещанием: кода, который мог бы повести себя иначе, при OFF не исполняется.

Третий — `TestReadersAgree`: у вопроса «этому чату можно?» в проекте ЧЕТЫРЕ
читателя (листенер, шлюз правок, два воркера уточнений). Разъедься они в
трактовке — в одном чате правку и приняли бы, и отбили.

🔴 Изоляция: каждая проба, которой нужен реестр, работает во ВРЕМЕННОМ каталоге.
Ни одна не смеет записать в `~/Projects/me/встречи/` — там живые встречи владельца
(сторожа `test_state_path_isolation.py`, `test_gateway_state_isolation.py`).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.lib import clarify_worker as cw  # noqa: E402
from notary.lib import feedback_worker as fw  # noqa: E402
from notary.lib import identity  # noqa: E402
from notary.lib import orders as orders_lib  # noqa: E402
from notary.lib import task_clarify_worker as tcw  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402

FLAG = identity.ENABLE_ENV

# Все формы «выключено», которые встречаются в файлах окружения живьём.
OFF_FORMS = (None, "", "   ", "0", "false", "FALSE", "no", "off", "мусор", "2", "yes ok")
ON_FORMS = ("1", "true", "TRUE", "yes", "on", " on ")

# Матрица входов. `None` — не экзотика: `chat.id` у битого апдейта отсутствует, а
# `allowed_chat` не задан в dev-режиме воркеров уточнений.
ALLOWED = (111, None)
CHATS = (111, 222, -1001234, None)
USERS = (111, 777, None)


# --------------------------------------------------------------------------
# Эталон: как отвечал код ДО Ф10a. Списан с git-состояния 696cd1d.
# --------------------------------------------------------------------------


def legacy_is_direct(cid, allowed):
    """`meetings_listener`: `cid == allowed_chat` (DM-гейт, is_dm, _chat_context_series)."""
    return cid == allowed


def legacy_may_command(cid, from_id, allowed):
    """`meetings_listener`: `is_dm or from_id == allowed_chat` (owner-гейт команд)."""
    return cid == allowed or from_id == allowed


def legacy_dm_target(cid, from_id, allowed):
    """`meetings_listener`: «в личку» = всегда `allowed_chat`."""
    return allowed


def legacy_authorized_sender(from_user, allowed):
    """`clarify_worker` / `task_clarify_worker`: `_is_authorized_sender`."""
    if allowed is None:
        return True
    sender_id = from_user.get("id") if isinstance(from_user, dict) else None
    try:
        return int(sender_id) == int(allowed)
    except (TypeError, ValueError):
        return False


class _FlagBase(unittest.TestCase):
    """Флаг, memo и КАТАЛОГ РЕЕСТРА приводятся в известное состояние до и после пробы.

    Временный каталог здесь, а не только в `_RegistryBase`, сознательно: на
    мутации «выключатель всегда ON» пробы равенства полезли бы читать БОЕВОЙ
    `~/Projects/me/встречи`. Читать чужие живые наряды пробе нечего даже
    случайно — изоляция не должна зависеть от того, включён ли флаг.
    """

    def setUp(self) -> None:
        self._saved_flag = os.environ.get(FLAG)
        self._saved_dir = os.environ.get("MEETING_NOTARY_REGISTRY_DIR")
        self.tmp = tempfile.mkdtemp(prefix="identity-ph10a-")
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = self.tmp
        os.environ.pop(FLAG, None)
        identity.reset_cache()

    def tearDown(self) -> None:
        for name, value in ((FLAG, self._saved_flag),
                            ("MEETING_NOTARY_REGISTRY_DIR", self._saved_dir)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.tmp, ignore_errors=True)
        identity.reset_cache()

    def set_flag(self, value):
        if value is None:
            os.environ.pop(FLAG, None)
        else:
            os.environ[FLAG] = value
        identity.reset_cache()


class _RegistryBase(_FlagBase):
    """То же плюс запись нарядов настоящей дверью `orders.put_order`."""

    def put(self, **over):
        """Живой наряд в ВРЕМЕННОМ реестре через настоящую дверь записи."""
        now = datetime.now(timezone.utc)
        payload = {
            "meeting_id": over.pop("meeting_id", "m-1"),
            "company": "anzhee",
            "series": "portal-m-1",
            "series_title": "Координация",
            "room_url": "https://telemost.yandex.ru/j/abc",
            "start_at": (now + timedelta(hours=1)).isoformat(),
            "duration_minutes": 60,
            "expected_participants": [],
            "keep_audio": False,
        }
        payload.update(over)
        order, _ = orders_lib.put_order(payload, now=now)
        identity.reset_cache()
        return order


# ==========================================================================
# 1. Канон выключателя
# ==========================================================================
class TestFlagCanon(_FlagBase):
    def test_off_forms(self):
        for raw in OFF_FORMS:
            with self.subTest(raw=raw):
                self.set_flag(raw)
                self.assertFalse(identity.is_enabled(), "%r обязано быть OFF" % (raw,))

    def test_on_forms(self):
        for raw in ON_FORMS:
            with self.subTest(raw=raw):
                self.set_flag(raw)
                self.assertTrue(identity.is_enabled(), "%r обязано быть ON" % (raw,))

    def test_default_is_off(self):
        # Дефолт — не «как в окружении», а именно OFF: тёмный запуск по канону
        # Ватсона. Включает владелец, отдельным решением.
        os.environ.pop(FLAG, None)
        self.assertFalse(identity.is_enabled())


# ==========================================================================
# 2. 🔴 ГЛАВНАЯ ПРОБА: при OFF поведение буква в букву прежнее
# ==========================================================================
class TestLegacyEquivalence(_FlagBase):
    """Каждая функция слоя против эталонного старого выражения, на всей матрице."""

    def test_is_direct_chat_matches_legacy(self):
        for raw in OFF_FORMS:
            self.set_flag(raw)
            for allowed in ALLOWED:
                for cid in CHATS:
                    with self.subTest(flag=raw, allowed=allowed, cid=cid):
                        self.assertEqual(
                            identity.is_direct_chat(cid, allowed),
                            legacy_is_direct(cid, allowed),
                        )

    def test_may_command_matches_legacy(self):
        for raw in OFF_FORMS:
            self.set_flag(raw)
            for allowed in ALLOWED:
                for cid in CHATS:
                    for uid in USERS:
                        with self.subTest(flag=raw, allowed=allowed, cid=cid, uid=uid):
                            self.assertEqual(
                                identity.may_command(cid, uid, allowed),
                                legacy_may_command(cid, uid, allowed),
                            )

    def test_dm_target_matches_legacy(self):
        for raw in OFF_FORMS:
            self.set_flag(raw)
            for allowed in ALLOWED:
                for cid in CHATS:
                    for uid in USERS:
                        with self.subTest(flag=raw, allowed=allowed, cid=cid, uid=uid):
                            self.assertEqual(
                                identity.dm_target(cid, uid, allowed),
                                legacy_dm_target(cid, uid, allowed),
                            )

    def test_listener_wrappers_match_legacy(self):
        """Обёртки листенера — тоже эталон: между ними и слоем ничего не потерялось."""
        for raw in OFF_FORMS:
            self.set_flag(raw)
            for allowed in ALLOWED:
                for cid in CHATS:
                    for uid in USERS:
                        with self.subTest(flag=raw, allowed=allowed, cid=cid, uid=uid):
                            self.assertEqual(
                                ml._is_direct_chat(cid, allowed), legacy_is_direct(cid, allowed))
                            self.assertEqual(
                                ml._may_command(cid, uid, allowed),
                                legacy_may_command(cid, uid, allowed))
                            self.assertEqual(
                                ml._dm_target(cid, uid, allowed),
                                legacy_dm_target(cid, uid, allowed))

    def test_feedback_worker_matches_legacy(self):
        for raw in OFF_FORMS:
            self.set_flag(raw)
            for allowed in ALLOWED:
                for cid in CHATS:
                    with self.subTest(flag=raw, allowed=allowed, cid=cid):
                        self.assertEqual(
                            fw._is_direct_chat(cid, allowed), legacy_is_direct(cid, allowed))

    def test_clarify_workers_match_legacy(self):
        senders = ({"id": 111}, {"id": 777}, {"id": "111"}, {"id": None}, {}, None, {"id": "мусор"})
        for raw in OFF_FORMS:
            self.set_flag(raw)
            for allowed in ALLOWED:
                for from_user in senders:
                    with self.subTest(flag=raw, allowed=allowed, from_user=from_user):
                        expected = legacy_authorized_sender(from_user, allowed)
                        self.assertEqual(cw._is_authorized_sender(from_user, allowed), expected)
                        self.assertEqual(tcw._is_authorized_sender(from_user, allowed), expected)


class TestEmptyRegistryOnChangesNothing(_RegistryBase):
    """Включение флага на ПУСТОМ реестре не меняет ничего.

    Это про день выкатки: владелец включает `ENABLE_PORTAL_ORDERS=1` раньше, чем
    портал поставил первый наряд. Если бы в этот момент что-то поехало, дефект
    выглядел бы как «бот сломался от одной переменной».
    """

    def test_on_with_no_orders_equals_legacy(self):
        self.set_flag("1")
        self.assertTrue(identity.is_enabled())
        for allowed in ALLOWED:
            for cid in CHATS:
                for uid in USERS:
                    with self.subTest(allowed=allowed, cid=cid, uid=uid):
                        self.assertEqual(
                            identity.is_direct_chat(cid, allowed), legacy_is_direct(cid, allowed))
                        self.assertEqual(
                            identity.may_command(cid, uid, allowed),
                            legacy_may_command(cid, uid, allowed))


# ==========================================================================
# 3. 🔴 При OFF диск не трогается вовсе
# ==========================================================================
class TestNoDiskWhenOff(_FlagBase):
    """«Выключено» — структурное свойство, а не обещание."""

    def test_orders_registry_never_touched_when_off(self):
        """СЧЁТЧИК, а не исключение.

        Первая версия пробы роняла AssertionError из подменённого `orders_file` —
        и `_active_orders` глотал его своим `except Exception` (AssertionError
        тоже Exception). Проба зеленела на мутации «выключатель всегда ON»:
        обращение к диску было, а сторож молчал. Счётчик соврать не может.
        """
        calls = {"n": 0}

        def _count(*_a, **_k):
            calls["n"] += 1
            return Path(self.tmp) / ".orders.json"

        for raw in OFF_FORMS:
            self.set_flag(raw)
            with mock.patch.object(orders_lib, "orders_file", side_effect=_count), \
                 mock.patch.object(orders_lib, "list_orders", side_effect=_count):
                for cid in CHATS:
                    for uid in USERS:
                        identity.resolve(cid, uid, allowed_chat=111)
                        identity.is_direct_chat(cid, 111)
                        identity.may_command(cid, uid, 111)
                        identity.dm_target(cid, uid, 111)
        self.assertEqual(
            calls["n"], 0,
            "при ENABLE_PORTAL_ORDERS=OFF слой идентичности %d раз пошёл в реестр нарядов"
            % calls["n"],
        )

    def test_counter_probe_actually_counts(self):
        """Сторож самого сторожа: при ON тот же счётчик обязан быть НЕ нулевым.

        Иначе «ноль обращений» ничего не доказывает — счётчик мог просто не быть
        подключён к тому месту, которое ходит на диск.
        """
        calls = {"n": 0}

        def _count(*_a, **_k):
            calls["n"] += 1
            return []

        self.set_flag("1")
        with mock.patch.object(orders_lib, "list_orders", side_effect=_count):
            identity.resolve(222, 777, allowed_chat=111)
        self.assertGreater(calls["n"], 0)

    def test_broken_registry_does_not_break_owner_when_on(self):
        """Даже при ON нечитаемый реестр не отнимает прав у владельца."""
        self.set_flag("1")
        with mock.patch.object(orders_lib, "list_orders", side_effect=OSError("нет доступа")):
            self.assertTrue(identity.may_command(111, 111, 111))
            self.assertTrue(identity.is_direct_chat(111, 111))
            self.assertFalse(identity.may_command(222, 777, 111))


# ==========================================================================
# 4. 🔴 Четыре читателя вопроса «этому можно» не разъезжаются
# ==========================================================================
class TestReadersAgree(_RegistryBase):
    def _readers_direct(self, cid, allowed):
        return {
            "identity": identity.is_direct_chat(cid, allowed),
            "listener": ml._is_direct_chat(cid, allowed),
            "feedback_worker": fw._is_direct_chat(cid, allowed),
        }

    def test_direct_chat_readers_agree_off_and_on(self):
        for flag in (None, "1"):
            self.set_flag(flag)
            for allowed in ALLOWED:
                for cid in CHATS:
                    with self.subTest(flag=flag, allowed=allowed, cid=cid):
                        answers = set(self._readers_direct(cid, allowed).values())
                        self.assertEqual(
                            len(answers), 1,
                            "читатели разошлись: %s" % self._readers_direct(cid, allowed))

    def test_command_readers_agree_for_portal_employee(self):
        """Сотрудник-владелец наряда: листенер и оба воркера уточнений — одно «да»."""
        self.put(owner={"portal_user_id": "u-7", "telegram_user_id": 777, "name": "Пётр"})
        self.set_flag("1")
        for uid, expected in ((777, True), (888, False), (111, True)):
            with self.subTest(uid=uid):
                self.assertEqual(identity.may_command(None, uid, 111), expected)
                self.assertEqual(ml._may_command(None, uid, 111), expected)
                self.assertEqual(cw._is_authorized_sender({"id": uid}, 111), expected)
                self.assertEqual(tcw._is_authorized_sender({"id": uid}, 111), expected)


# ==========================================================================
# 5. Владелец наряда как ДАННЫЕ + невалидные входы
# ==========================================================================
class TestOrderOwnerData(_FlagBase):
    def test_owner_read(self):
        owner = identity.order_owner({
            "id": "portal:m-1", "company": "anzhee",
            "owner": {"portal_user_id": "u-7", "telegram_user_id": 777, "name": "Пётр"},
        })
        self.assertIsNotNone(owner)
        self.assertEqual(owner.portal_user_id, "u-7")
        self.assertEqual(owner.company, "anzhee")
        self.assertEqual(owner.telegram_user_id, 777)

    def test_order_without_owner_is_ownerless_not_crash(self):
        """Наряд Ф9 без владельца: читается, прав не даёт, исключения нет."""
        self.assertIsNone(identity.order_owner({"id": "portal:m-1", "company": "anzhee"}))

    def test_owner_without_portal_user_id_is_ownerless(self):
        # По одному имени наряд не связать ни с сотрудником, ни с аккаунтом.
        self.assertIsNone(identity.order_owner(
            {"company": "anzhee", "owner": {"name": "Пётр"}}))

    def test_unknown_company_gives_no_identity(self):
        self.assertIsNone(identity.order_owner(
            {"company": "acme", "owner": {"portal_user_id": "u-7"}}))
        self.assertIsNone(identity.order_owner(
            {"owner": {"portal_user_id": "u-7"}}))

    def test_garbage_order_shapes(self):
        for junk in (None, [], "наряд", 42, {"owner": "Пётр"}, {"owner": []}):
            with self.subTest(junk=junk):
                self.assertIsNone(identity.order_owner(junk))


class TestPortalIdentityOn(_RegistryBase):
    def test_employee_gets_identity_and_company(self):
        self.put(owner={"portal_user_id": "u-7", "telegram_user_id": 777}, company="mpfirst")
        self.set_flag("1")
        p = identity.resolve(777, 777, allowed_chat=111)
        self.assertEqual(p.kind, identity.KIND_PORTAL)
        self.assertEqual(p.company, "mpfirst")
        self.assertEqual(p.portal_user_id, "u-7")
        self.assertTrue(p.may_command)
        self.assertTrue(p.is_direct)          # его личка обслуживается
        self.assertEqual(p.dm_chat_id, 777)   # «в личку» = ЕГО личка, не Ильи

    def test_scalar_is_no_longer_the_only_answer(self):
        """Критерий фазы: решение принимается по владельцу наряда, а не по скаляру."""
        self.put(owner={"portal_user_id": "u-7", "telegram_user_id": 777})
        # Скаляр говорит «чужой» (777 ≠ 111) — и при OFF это финальный ответ…
        self.assertFalse(identity.may_command(777, 777, 111))
        # …а при ON финальный ответ даёт наряд.
        self.set_flag("1")
        self.assertTrue(identity.may_command(777, 777, 111))
        self.assertEqual(identity.dm_target(777, 777, 111), 777)

    def test_chat_of_nobody_stays_stranger(self):
        self.put(owner={"portal_user_id": "u-7", "telegram_user_id": 777})
        self.set_flag("1")
        p = identity.resolve(-1009999, 888, allowed_chat=111)
        self.assertEqual(p.kind, identity.KIND_STRANGER)
        self.assertFalse(p.may_command)
        self.assertFalse(p.chat_is_serviced)

    def test_ownerless_order_on_disk_grants_nothing(self):
        """Обратная совместимость: наряд Ф9 лежит, читается, прав не раздаёт.

        Чат доставки у наряда ЗАДАН намеренно: это и есть опасный случай. Будь
        разбор владельца снисходительным («нет поля — ну и ладно»), права по
        такому наряду получил бы любой, кто пишет в его чат. Без `telegram_chat_id`
        проба зеленела бы просто потому, что искать было негде — на мутации
        «наряд без владельца проходит» это и вскрылось.
        """
        self.put(meeting_id="m-old", telegram_chat_id=-100777)  # без owner — так писал Ф9
        self.set_flag("1")
        self.assertEqual(len(orders_lib.list_orders()), 1)  # наряд НА МЕСТЕ
        self.assertFalse(identity.may_command(-100777, 555, 111))
        self.assertFalse(identity.is_direct_chat(-100777, 111))
        self.assertEqual(identity.resolve(-100777, 555, allowed_chat=111).kind,
                         identity.KIND_STRANGER)

    def test_cancelled_order_stops_granting_rights(self):
        order = self.put(owner={"portal_user_id": "u-7", "telegram_user_id": 777})
        self.set_flag("1")
        self.assertTrue(identity.may_command(777, 777, 111))
        orders_lib.cancel_order(order["id"])
        identity.reset_cache()
        self.assertFalse(identity.may_command(777, 777, 111))


# ==========================================================================
# 6. Изоляция компаний (R12.12 на стороне бота)
# ==========================================================================
class TestCompanyIsolation(_RegistryBase):
    def test_employee_cannot_touch_other_company_order(self):
        self.set_flag("1")
        mine = {"company": "anzhee", "owner": {"portal_user_id": "u-7"}}
        theirs = {"company": "mpfirst", "owner": {"portal_user_id": "u-9"}}
        p = identity.Principal(kind=identity.KIND_PORTAL, chat_id=777, user_id=777,
                               company="anzhee", portal_user_id="u-7", dm_chat_id=777)
        self.assertTrue(identity.may_touch_order(p, mine))
        self.assertFalse(identity.may_touch_order(p, theirs))

    def test_owner_may_touch_both_companies(self):
        p = identity.Principal(kind=identity.KIND_OWNER, chat_id=111, user_id=111, dm_chat_id=111)
        self.assertTrue(identity.may_touch_order(p, {"company": "anzhee",
                                                     "owner": {"portal_user_id": "u-7"}}))
        self.assertTrue(identity.may_touch_order(p, {"company": "mpfirst",
                                                     "owner": {"portal_user_id": "u-9"}}))

    def test_stranger_touches_nothing(self):
        p = identity.Principal(kind=identity.KIND_STRANGER, chat_id=999, user_id=999)
        self.assertFalse(identity.may_touch_order(p, {"company": "anzhee",
                                                      "owner": {"portal_user_id": "u-7"}}))

    def test_ownerless_order_grants_no_one(self):
        p = identity.Principal(kind=identity.KIND_PORTAL, chat_id=777, user_id=777,
                               company="anzhee", portal_user_id="u-7", dm_chat_id=777)
        self.assertFalse(identity.may_touch_order(p, {"company": "anzhee"}))

    def test_room_of_another_company_is_a_conflict(self):
        # Форма реестра — `{"watched": [...]}` (ключ реальный, не выдуманный: на
        # первой версии этой пробы фикстура была с `records`, и проверка молча не
        # срабатывала ни разу).
        watched = {"watched": [{"series": "koord", "company": "mpfirst"}]}
        conflict = identity.order_company_conflict(
            {"company": "anzhee", "series": "koord"}, watched)
        self.assertEqual(conflict, "mpfirst")

    def test_same_company_and_unmarked_series_are_not_conflicts(self):
        watched = {"watched": [{"series": "koord", "company": "anzhee"},
                               {"series": "bez-razmetki"}]}
        self.assertIsNone(identity.order_company_conflict(
            {"company": "anzhee", "series": "koord"}, watched))
        self.assertIsNone(identity.order_company_conflict(
            {"company": "anzhee", "series": "bez-razmetki"}, watched))
        self.assertIsNone(identity.order_company_conflict(
            {"company": "anzhee", "series": "voobshche-net-v-reestre"}, watched))


# ==========================================================================
# 7. Валидатор наряда: владелец обязателен ТОЛЬКО при ON
# ==========================================================================
class TestOwnerRequiredBehindFlag(_FlagBase):
    def _payload(self, **over):
        now = datetime.now(timezone.utc)
        body = {
            "meeting_id": "m-1",
            "company": "anzhee",
            "series_title": "Координация",
            "room_url": "https://telemost.yandex.ru/j/abc",
            "start_at": (now + timedelta(hours=1)).isoformat(),
        }
        body.update(over)
        return body

    def _codes(self, body):
        _, errors = orders_lib.validate_order_payload(body)
        return {(e["field"], e["code"]) for e in errors}

    def test_off_keeps_owner_optional(self):
        # Поведение Ф9 целиком: выключатель возвращает и строгость валидатора.
        self.assertEqual(self._codes(self._payload()), set())

    def test_on_requires_owner(self):
        self.set_flag("1")
        self.assertIn(("owner", "required"), self._codes(self._payload()))

    def test_on_requires_portal_user_id(self):
        self.set_flag("1")
        codes = self._codes(self._payload(owner={"name": "Пётр"}))
        self.assertIn(("owner.portal_user_id", "required"), codes)

    def test_on_accepts_proper_owner(self):
        self.set_flag("1")
        self.assertEqual(
            self._codes(self._payload(owner={"portal_user_id": "u-7", "telegram_user_id": 777})),
            set(),
        )

    def test_company_still_required_regardless_of_flag(self):
        for flag in (None, "1"):
            self.set_flag(flag)
            body = self._payload(owner={"portal_user_id": "u-7"})
            body.pop("company")
            with self.subTest(flag=flag):
                self.assertIn(("company", "required"), self._codes(body))


# ==========================================================================
# 8. Шлюз: наряд без владельца — по живому HTTP
# ==========================================================================
class TestGatewayOwnerGate(unittest.TestCase):
    """Контракт для портала: что именно он получит, поставив «ничей» наряд."""

    def setUp(self) -> None:
        from notary.tests.gateway_harness import EnvSandbox, LiveGateway  # noqa: PLC0415

        self.env = EnvSandbox()          # песочница сама гасит флаг в OFF
        self.gw = LiveGateway()

    def tearDown(self) -> None:
        self.gw.stop()
        self.env.restore()

    def _order_payload(self, **over):
        from notary.tests.gateway_harness import order_payload  # noqa: PLC0415

        return order_payload(**over)

    def _on(self):
        os.environ[FLAG] = "1"
        identity.reset_cache()

    def test_off_accepts_ownerless_order_as_phase9_did(self):
        resp = self.gw.request("POST", "/orders", body=self._order_payload())
        self.assertEqual(resp.status, 201, resp.body[:400])
        self.assertFalse(resp.json().get("order", {}).get("owner"))

    def test_on_rejects_ownerless_order_with_422(self):
        self._on()
        resp = self.gw.request("POST", "/orders", body=self._order_payload())
        self.assertEqual(resp.status, 422, resp.body[:400])
        fields = {e["field"]: e["code"] for e in resp.json()["errors"]}
        self.assertEqual(fields.get("owner"), "required", fields)

    def test_on_accepts_order_with_owner(self):
        self._on()
        resp = self.gw.request("POST", "/orders", body=self._order_payload(
            owner={"portal_user_id": "u-7", "telegram_user_id": 777, "name": "Пётр"}))
        self.assertEqual(resp.status, 201, resp.body[:400])
        self.assertEqual(resp.json()["order"]["owner"]["portal_user_id"], "u-7")

    def test_flag_state_is_visible_in_status(self):
        # Иначе «наряды не исполняются» и «фича выключена» с портала неразличимы,
        # и разбираться пришлось бы по логам на VPS.
        off = self.gw.request("GET", "/status").json()
        self.assertIs(off["activity"]["portal_orders_enabled"], False)
        self._on()
        on = self.gw.request("GET", "/status").json()
        self.assertIs(on["activity"]["portal_orders_enabled"], True)


# ==========================================================================
# 9. 🔴 Живые роуты листенера при OFF ведут себя как вчера
# ==========================================================================
class TestOwnerRoutesUnchanged(_FlagBase):
    """Не «функция вернула тот же bool», а «роут повёл себя так же».

    Каждый сценарий — из ежедневного обихода владельца: его команда в личке, его
    команда в группе серии, команда постороннего, сообщение в чужом чате, нажатие
    кнопки. Флаг при этом OFF — то есть ровно то состояние, в котором Ватсон
    работает у Ильи сегодня.
    """

    OWNER = 111        # allowed_chat = личка владельца
    GROUP = -1001234   # групповой чат серии
    STRANGER = 777

    def _msg(self, cid, from_id, text):
        return {"chat": {"id": cid}, "from": {"id": from_id},
                "text": text, "message_id": 9001, "date": 1718000000}

    # --- DM-гейт: чужой чат по-прежнему отсекается ---
    def test_foreign_chat_still_skipped(self):
        with mock.patch.object(ml, "maybe_route_to_set_chat_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_mode_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_ack_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_send_artifact", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_digest_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_reply_understand", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_mixed_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_pending_status_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_feedback_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_protocol_command") as after_gate:
            ml.process_message("tok", self.OWNER, self._msg(self.GROUP, self.STRANGER, "привет"))
        after_gate.assert_not_called()  # до команд DM-потока не дошло

    def test_owner_dm_passes_the_gate(self):
        with mock.patch.object(ml, "maybe_route_to_set_chat_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_mode_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_ack_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_send_artifact", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_digest_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_reply_understand", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_mixed_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_pending_status_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_feedback_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_protocol_command", return_value=True) as after:
            ml.process_message("tok", self.OWNER, self._msg(self.OWNER, self.OWNER, "протокол"))
        after.assert_called_once()

    # --- owner-гейт команды смены чата ---
    def test_set_chat_owner_in_group_still_allowed(self):
        parsed = mock.Mock(kind="set_chat", target="here", series="koord")
        with mock.patch("notary.lib.correction_command.parse_correction_command",
                        return_value=parsed), \
             mock.patch.object(ml, "_reply_context_series", return_value=None), \
             mock.patch.object(ml, "_series_display_map", return_value={}), \
             mock.patch.object(ml, "send_message") as send:
            ml.maybe_route_to_set_chat_command(
                "tok", self.GROUP, self.OWNER, self._msg(self.GROUP, self.OWNER, "серию шли сюда"))
        # Владелец в ГРУППЕ прошёл owner-гейт: бот ответил (а не промолчал).
        self.assertTrue(send.called)

    def test_set_chat_stranger_in_group_still_rejected(self):
        parsed = mock.Mock(kind="set_chat", target="here", series="koord")
        with mock.patch("notary.lib.correction_command.parse_correction_command",
                        return_value=parsed), \
             mock.patch.object(ml, "send_message") as send:
            claimed = ml.maybe_route_to_set_chat_command(
                "tok", self.GROUP, self.OWNER,
                self._msg(self.GROUP, self.STRANGER, "серию шли сюда"))
        self.assertFalse(claimed)
        send.assert_not_called()  # нотацию участникам бот не читает

    # --- гейт inline-кнопок ---
    def test_callback_from_foreign_chat_still_skipped(self):
        cbq = {"id": "1", "data": "cl:x", "from": {"id": self.STRANGER},
               "message": {"chat": {"id": self.GROUP}}}
        with mock.patch.object(ml, "_clarify_pending_root") as root:
            ml.process_callback_query("tok", self.OWNER, cbq)
        root.assert_not_called()

    def test_callback_from_owner_dm_still_processed(self):
        cbq = {"id": "1", "data": "cl:x", "from": {"id": self.OWNER},
               "message": {"chat": {"id": self.OWNER}}}
        with mock.patch.object(ml, "_clarify_pending_root", return_value=None) as root:
            ml.process_callback_query("tok", self.OWNER, cbq)
        root.assert_called_once()

    # --- личка владельца не считается чатом серии ---
    def test_owner_dm_is_not_a_series_chat(self):
        self.assertEqual(ml._chat_context_series(self.OWNER, self.OWNER), ([], None))

    def test_group_still_resolves_series_by_binding(self):
        from notary.cli import registry  # noqa: PLC0415

        with mock.patch.object(registry, "load_watched", return_value={"watched": []}), \
             mock.patch.object(registry, "series_for_chat", return_value=["koord"]):
            series, err = ml._chat_context_series(self.GROUP, self.OWNER)
        self.assertEqual((series, err), (["koord"], None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
