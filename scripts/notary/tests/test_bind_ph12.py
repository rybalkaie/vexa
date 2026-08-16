"""Ф12 — привязка сотрудника кодом и привязка группы `/привязать` (R8.1–R8.6).

🔴 ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, А ЧТО НЕТ.

Доказывается ПЯТЬ вещей, каждая из которых ломается молча:

  1. РОУТ ДОЕЗЖАЕТ ИЗ ГРУППЫ. `/привязать` приходит из чата, который бот
     сегодня НЕ обслуживает (его и просят привязать), — значит роут обязан
     стоять ДО DM-гейта. Стой он после, команда молча тонула бы ровно там, где
     она нужна, и увидеть это можно было бы только на живом сервере.

  2. OWNER-ГЕЙТА НЕТ. Это прямое требование R8.6 («группу привязывает любой
     сотрудник») и одновременно самое опасное изменение фазы: соседние
     групповые роуты владельцем гейтятся, и «сделать как рядом» здесь было бы
     ошибкой. Проба закрепляет ИМЕННО отсутствие гейта.

  3. БОТ НЕ СУДИТ О КОДЕ. Он не знает ни срока, ни счётчика попыток; он
     передаёт код порталу и печатает ОТВЕТ ПОРТАЛА дословно. Начни бот
     сочинять свои формулировки — у человека появились бы две инструкции.

  4. КОД НЕ ПОПАДАЕТ В ЖУРНАЛ. Лог листенера — файл на диске VPS, уровень INFO;
     код привязки это пароль на пятнадцать минут.

  5. ОБЫЧНАЯ ПЕРЕПИСКА НЕ ПЕРЕХВАТЫВАЕТСЯ. Числа в группе люди пишут
     постоянно; принимай мы их за код — чужими руками выбивали бы человеку
     блокировку по счётчику попыток на портале.

НЕ доказывается (и доказано быть не может без живого сервера): доедет ли
кириллическое `/привязать` через privacy mode. Это свойство Telegram, а не
нашего кода; долг записан в плане фазы.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_bind_ph12 -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import notary.meetings_listener as ml  # noqa: E402
from notary.lib import portal_link  # noqa: E402
from notary.lib.bind_command import parse_bind_command  # noqa: E402

OWNER = 12345
GROUP = -100500
STRANGER = 999
CODE = "482913"


def _msg(
    text,
    *,
    chat=GROUP,
    from_id=OWNER,
    title="Директорат Anzhee",
    username=None,
    chat_type=None,
):
    """Сообщение Telegram.

    `chat_type` живой Telegram проставляет ВСЕГДА (`private` / `group` /
    `supergroup` / `channel`), и именно по нему бот решает, личка ли это. По
    умолчанию выводим его из знака идентификатора, как это делает и сам бот.
    """
    frm = {"id": from_id}
    if username:
        frm["username"] = username
    return {
        "chat": {
            "id": chat,
            "title": title,
            "type": chat_type or ("private" if chat > 0 else "supergroup"),
        },
        "from": frm,
        "message_id": 1000,
        "date": 1718000000,
        "text": text,
    }


class _FakePortal:
    """Шов вместо сети: подменяем `_http_post`, а не urllib.

    Возвращает то, что вернул бы портал: `{ok, outcome, reply}`.
    """

    def __init__(self, *, status=200, reply="Готово", outcome="linked", raise_exc=None):
        self.status = status
        self.reply = reply
        self.outcome = outcome
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        if self.raise_exc:
            raise self.raise_exc
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": json.loads(body.decode("utf-8")),
                "timeout": timeout,
            }
        )
        payload = json.dumps(
            {"ok": True, "outcome": self.outcome, "reply": self.reply}, ensure_ascii=False
        ).encode("utf-8")
        return self.status, payload


class TestParse(unittest.TestCase):
    """Разбор: что считается командой, а что нет."""

    def test_group_command_forms(self):
        for text in ("/привязать", "/bind", "/привязать@AnzheeWatsonBot", "/BIND"):
            with self.subTest(text=text):
                parsed = parse_bind_command(text, direct=False)
                self.assertIsNotNone(parsed, text)
                self.assertEqual(parsed.kind, "group")
                self.assertIsNone(parsed.value)

    def test_group_command_with_company(self):
        parsed = parse_bind_command("/привязать anzhee", direct=False)
        self.assertEqual((parsed.kind, parsed.value), ("group", "anzhee"))
        parsed = parse_bind_command("/bind@AnzheeWatsonBot mpervyy", direct=False)
        self.assertEqual((parsed.kind, parsed.value), ("group", "mpervyy"))

    def test_bare_code_only_in_direct(self):
        # 🔴 В ЛИЧКЕ шесть цифр — код…
        parsed = parse_bind_command(CODE, direct=True)
        self.assertEqual((parsed.kind, parsed.value), ("code", CODE))
        # …А В ГРУППЕ — просто число. Иначе каждое «переносим на 141500» уходило
        # бы в портал промахом и выбивало человеку блокировку.
        self.assertIsNone(parse_bind_command(CODE, direct=False))

    def test_code_with_separators_and_command(self):
        self.assertEqual(parse_bind_command("482 913", direct=True).kind, "code")
        self.assertEqual(parse_bind_command("/привязать 482913", direct=False).kind, "code")

    def test_not_a_command(self):
        for text in (
            "привязать группу",  # свободная речь — не команда
            "перенеси встречу",
            "",
            "   ",
            "48291",  # пять цифр
            "4829134",  # семь цифр
            "серию anzhee шли сюда",  # чужая команда, её ведёт другой роут
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_bind_command(text, direct=True))

    def test_start(self):
        self.assertEqual(parse_bind_command("/start", direct=True).kind, "start")
        self.assertEqual(parse_bind_command("/start@AnzheeWatsonBot", direct=True).kind, "start")


class TestGroupBinding(unittest.TestCase):
    """`/привязать` в группе."""

    def setUp(self):
        self.portal = _FakePortal(reply="Готово — группа появилась на портале.", outcome="bound")
        self.env = mock.patch.dict(
            "os.environ",
            {
                "MEETING_NOTARY_PORTAL_INGEST_URL": "https://portal.example/api/meetings/notary/ingest",
                "MEETING_NOTARY_PORTAL_INGEST_TOKEN": "t" * 40,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.post = mock.patch.object(portal_link, "_http_post", self.portal)
        self.post.start()
        self.addCleanup(self.post.stop)
        self.send = mock.patch.object(ml, "send_message", return_value=777).start()
        self.addCleanup(mock.patch.stopall)

    def _said(self):
        return "\n".join(str(c.args[2]) for c in self.send.call_args_list)

    def test_owner_can_bind_group(self):
        handled = ml.maybe_route_to_bind_command("tok", GROUP, OWNER, _msg("/привязать"))
        self.assertTrue(handled)
        self.assertIn("группа появилась", self._said())
        self.assertEqual(self.portal.calls[0]["payload"]["kind"], "group")

    def test_NON_owner_can_bind_group_too(self):
        """🔴 СЕРДЦЕ ФАЗЫ: owner-гейт снят (R8.6).

        Соседние групповые роуты (`set_chat`, `mode`) не-владельцу отвечают
        `False` и молчат. Здесь наоборот — команда обязана СРАБОТАТЬ, иначе
        подключение группы снова становится задачей владельца бота.
        """
        handled = ml.maybe_route_to_bind_command(
            "tok", GROUP, OWNER, _msg("/привязать", from_id=STRANGER)
        )
        self.assertTrue(handled)
        self.assertEqual(len(self.portal.calls), 1)
        # Портал получил ИМЕННО отправителя — по нему он и решит, сотрудник ли
        # это (замена снятому гейту).
        self.assertEqual(self.portal.calls[0]["payload"]["telegram_user_id"], str(STRANGER))

    def test_company_argument_passed_through(self):
        ml.maybe_route_to_bind_command("tok", GROUP, OWNER, _msg("/привязать anzhee"))
        self.assertEqual(self.portal.calls[0]["payload"]["organization"], "anzhee")

    def test_group_title_goes_to_portal(self):
        ml.maybe_route_to_bind_command("tok", GROUP, OWNER, _msg("/привязать"))
        self.assertEqual(self.portal.calls[0]["payload"]["title"], "Директорат Anzhee")

    def test_ordinary_group_message_not_intercepted(self):
        for text in ("а давайте перенесём", "141500", "серию anzhee шли сюда"):
            with self.subTest(text=text):
                handled = ml.maybe_route_to_bind_command("tok", GROUP, OWNER, _msg(text))
                self.assertFalse(handled)
        self.send.assert_not_called()
        self.assertEqual(self.portal.calls, [])


class TestCodeBinding(unittest.TestCase):
    """Код в личке.

    🔴 АКТЁР ЗДЕСЬ — НЕЗНАКОМЕЦ, А НЕ ВЛАДЕЛЕЦ. Первым заходом фазы весь этот
    класс проходил личку владельца, и на ней дефект «личка считается через
    вопрос о доверии» не проявлялся вовсе (у владельца оба ответа совпадают).
    Живой сценарий R8.1 — сотрудник, которого бот ещё не знает.
    """

    def setUp(self):
        self.portal = _FakePortal(reply="Готово — теперь я знаю, что это вы.", outcome="linked")
        self.env = mock.patch.dict(
            "os.environ",
            {
                "MEETING_NOTARY_PORTAL_INGEST_URL": "https://portal.example/api/meetings/notary/ingest",
                "MEETING_NOTARY_PORTAL_INGEST_TOKEN": "t" * 40,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        mock.patch.object(portal_link, "_http_post", self.portal).start()
        self.send = mock.patch.object(ml, "send_message", return_value=777).start()
        self.addCleanup(mock.patch.stopall)

    def _said(self):
        return "\n".join(str(c.args[2]) for c in self.send.call_args_list)

    def test_code_in_dm_goes_to_portal_and_reply_is_portals(self):
        handled = ml.maybe_route_to_bind_command(
            "tok", STRANGER, OWNER,
            _msg(CODE, chat=STRANGER, from_id=STRANGER, title=None, username="petrov_tg"),
        )
        self.assertTrue(handled)
        payload = self.portal.calls[0]["payload"]
        self.assertEqual(payload["kind"], "code")
        self.assertEqual(payload["code"], CODE)
        self.assertEqual(payload["telegram_username"], "petrov_tg")
        # 🔴 ОТВЕТ — ДОСЛОВНО ОТ ПОРТАЛА, бот его не сочиняет.
        self.assertIn("теперь я знаю", self._said())

    def test_portal_verdict_is_printed_as_is(self):
        """Любой исход — тем же путём: бот не переводит «expired» на свой язык."""
        self.portal.reply = "Код истёк. Возьмите новый на портале."
        self.portal.outcome = "expired"
        ml.maybe_route_to_bind_command(
            "tok", STRANGER, OWNER, _msg(CODE, chat=STRANGER, from_id=STRANGER, title=None)
        )
        self.assertIn("Код истёк", self._said())

    def test_portal_down_gets_honest_answer_not_silence(self):
        mock.patch.object(
            portal_link, "_http_post", _FakePortal(raise_exc=OSError("нет сети"))
        ).start()
        handled = ml.maybe_route_to_bind_command(
            "tok", STRANGER, OWNER, _msg(CODE, chat=STRANGER, from_id=STRANGER, title=None)
        )
        self.assertTrue(handled)
        self.assertIn("не отвечает", self._said())

    def test_start_explains_without_network(self):
        calls_before = len(self.portal.calls)
        handled = ml.maybe_route_to_bind_command(
            "tok", STRANGER, OWNER, _msg("/start", chat=STRANGER, from_id=STRANGER, title=None)
        )
        self.assertTrue(handled)
        self.assertIn("шести цифр", self._said())
        # /start в сеть не ходит: человек ещё ничего не прислал.
        self.assertEqual(len(self.portal.calls), calls_before)

    def test_code_never_appears_in_logs(self):
        """🔴 КОД — ПАРОЛЬ НА 15 МИНУТ, а лог листенера лежит файлом на VPS."""
        with self.assertLogs("meetings-listener", level="INFO") as captured:
            ml.maybe_route_to_bind_command(
                "tok", STRANGER, OWNER, _msg(CODE, chat=STRANGER, from_id=STRANGER, title=None)
            )
        joined = "\n".join(captured.output)
        self.assertNotIn(CODE, joined)
        # И числового отправителя рядом с исходом привязки тоже нет.
        self.assertNotIn(str(STRANGER), joined)


class TestStrangerInDirectChat(unittest.TestCase):
    """🔴 ГЛАВНЫЙ ВХОД ФАЗЫ, КОТОРОГО НЕ БЫЛО НИ В ОДНОЙ ПРОБЕ: НЕЗНАКОМЕЦ В ЛИЧКЕ.

    Все прежние пробы кода проходили личку ВЛАДЕЛЬЦА (`chat == OWNER ==
    allowed_chat`), а юнит-разбор получал признак лички хардкодом. На этой паре
    подстановок дефект не виден вовсе: `_is_direct_chat` отвечает не «личка ли
    это», а «личка ли это ДОВЕРЕННОГО лица», и у владельца оба ответа совпадают.

    А фаза адресована ровно обратному человеку — сотруднику, которого бот ещё НЕ
    знает: он берёт код на портале и шлёт шесть цифр в личку. При прежнем вопросе
    признак лички для него был ЛОЖЬЮ, разбор возвращал «это не про привязку», и в
    портал не уходило НИЧЕГО. Курица и яйцо: чтобы стать доверенным — привяжись,
    чтобы привязаться — будь доверенным.
    """

    def setUp(self):
        self.portal = _FakePortal(reply="Готово — теперь я знаю, что это вы.", outcome="linked")
        self.env = mock.patch.dict(
            "os.environ",
            {
                "MEETING_NOTARY_PORTAL_INGEST_URL": "https://portal.example/api/meetings/notary/ingest",
                "MEETING_NOTARY_PORTAL_INGEST_TOKEN": "t" * 40,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        mock.patch.object(portal_link, "_http_post", self.portal).start()
        self.send = mock.patch.object(ml, "send_message", return_value=777).start()
        self.addCleanup(mock.patch.stopall)

    def _said(self):
        return "\n".join(str(c.args[2]) for c in self.send.call_args_list)

    def test_unknown_employee_code_reaches_portal(self):
        """Сотрудник, которого бот не знает, шлёт шесть цифр в личку → УШЛО В ПОРТАЛ."""
        handled = ml.maybe_route_to_bind_command(
            "tok",
            STRANGER,  # его личка: id чата == id человека
            OWNER,  # …а доверенная личка — чужая, владельца
            _msg(CODE, chat=STRANGER, from_id=STRANGER, title=None, username="petrov"),
        )
        self.assertTrue(handled, "роут обязан взять сообщение незнакомца")
        self.assertEqual(len(self.portal.calls), 1, "в портал не ушло ничего")
        payload = self.portal.calls[0]["payload"]
        self.assertEqual(payload["kind"], "code")
        self.assertEqual(payload["code"], CODE)
        self.assertEqual(payload["telegram_user_id"], str(STRANGER))
        self.assertIn("теперь я знаю", self._said())

    def test_unknown_employee_code_with_spacing(self):
        """Он же, но перепечатал код с разрядкой, как нарисовано на экране портала."""
        ml.maybe_route_to_bind_command(
            "tok", STRANGER, OWNER, _msg("482 913", chat=STRANGER, from_id=STRANGER, title=None)
        )
        self.assertEqual(len(self.portal.calls), 1)
        self.assertEqual(self.portal.calls[0]["payload"]["kind"], "code")

    def test_start_from_unknown_employee_explains(self):
        """`/start` незнакомцу тоже отвечает — это первый экран инструкции."""
        handled = ml.maybe_route_to_bind_command(
            "tok", STRANGER, OWNER, _msg("/start", chat=STRANGER, from_id=STRANGER, title=None)
        )
        self.assertTrue(handled)
        self.assertIn("шести цифр", self._said())

    def test_private_chat_never_becomes_a_group(self):
        """🔴 `/привязать` В ЛИЧКЕ — НЕ ПРИВЯЗКА ГРУППЫ.

        Следствие того же корня: при ложном признаке лички команда без аргумента
        трактовалась как «привяжи ЭТОТ чат», и портал заводил ЛИЧНЫЙ чат
        сотрудника как «группу компании» — с его числовым Telegram открытым
        текстом в колонке адреса доставки.
        """
        for text in ("/привязать", "/bind", "/привязать@AnzheeWatsonBot"):
            with self.subTest(text=text):
                self.send.reset_mock()
                self.portal.calls.clear()
                handled = ml.maybe_route_to_bind_command(
                    "tok", STRANGER, OWNER, _msg(text, chat=STRANGER, from_id=STRANGER, title=None)
                )
                self.assertTrue(handled)
                self.assertEqual(
                    [c["payload"]["kind"] for c in self.portal.calls],
                    [],
                    "личка ушла в портал как группа",
                )
                self.assertIn("шести цифр", self._said())

    def test_group_binding_from_private_chat_is_blocked_at_the_route(self):
        """Второй пояс: даже согласись разбор считать личку группой — роут не пустит.

        Разбор сюда не доводит (в личке команда без аргумента = «объясни»), но
        цена пропуска слишком велика: портал завёл бы ЛИЧНЫЙ чат как «группу
        компании», и числовой Telegram сотрудника лёг бы открытым текстом в
        колонку адреса доставки. Подменяем разбор швом и проверяем сам пояс.
        """
        from notary.lib import bind_command as bc

        with mock.patch.object(
            bc, "parse_bind_command", return_value=bc.BindCommand(kind="group")
        ):
            handled = ml.maybe_route_to_bind_command(
                "tok",
                STRANGER,
                OWNER,
                _msg("/привязать", chat=STRANGER, from_id=STRANGER, title=None),
            )
        self.assertTrue(handled)
        self.assertEqual(self.portal.calls, [], "личка ушла в портал как группа")

    def test_owner_private_chat_is_not_a_group_either(self):
        """И у владельца тоже: его личка — не «группа компании»."""
        ml.maybe_route_to_bind_command("tok", OWNER, OWNER, _msg("/привязать", chat=OWNER))
        self.assertEqual([c["payload"]["kind"] for c in self.portal.calls], [])

    def test_group_still_binds_as_group(self):
        """Сторож на противоход: настоящая группа обязана остаться группой."""
        ml.maybe_route_to_bind_command(
            "tok", GROUP, OWNER, _msg("/привязать", from_id=STRANGER)
        )
        self.assertEqual([c["payload"]["kind"] for c in self.portal.calls], ["group"])

    def test_bare_digits_from_known_person_are_not_a_code(self):
        """🔴 «141500» В ЛИЧКЕ ВЛАДЕЛЬЦА — ЭТО ВРЕМЯ, А НЕ КОД.

        Роут стоит ПЕРВЫМ, поэтому съеденное им сообщение до остальных роутов не
        доходит вовсе: человек получал бы «такого кода не знаю» вместо разбора, а
        пять таких подряд выбивали бы ему пятнадцатиминутную блокировку на
        портале. Бот владельца ЗНАЕТ — значит и код от него не ждёт.
        """
        for text in ("141500", "141 500", "482913"):
            with self.subTest(text=text):
                handled = ml.maybe_route_to_bind_command(
                    "tok", OWNER, OWNER, _msg(text, chat=OWNER)
                )
                self.assertFalse(handled, "сообщение владельца съедено привязкой")
        self.assertEqual(self.portal.calls, [])
        self.send.assert_not_called()

    def test_known_person_can_still_relink_with_explicit_command(self):
        """Переезд на другой аккаунт знакомому оставлен — явной формой с кодом."""
        handled = ml.maybe_route_to_bind_command(
            "tok", OWNER, OWNER, _msg(f"/привязать {CODE}", chat=OWNER)
        )
        self.assertTrue(handled)
        self.assertEqual(self.portal.calls[0]["payload"]["kind"], "code")
        self.assertEqual(self.portal.calls[0]["payload"]["code"], CODE)


class TestRouteOrder(unittest.TestCase):
    """🔴 РОУТ СТОИТ ДО DM-ГЕЙТА — иначе `/привязать` из группы тонет молча."""

    def test_bind_route_runs_first_and_short_circuits(self):
        with mock.patch.object(ml, "maybe_route_to_bind_command", return_value=True) as bind, \
             mock.patch.object(ml, "maybe_route_to_set_chat_command") as set_chat, \
             mock.patch.object(ml, "maybe_route_to_mode_command") as mode:
            ml.process_message("tok", OWNER, _msg("/привязать"))
        bind.assert_called_once()
        set_chat.assert_not_called()
        mode.assert_not_called()

    def test_bind_route_reached_for_unserviced_group(self):
        """Группа, которую бот НЕ обслуживает, — ровно тот случай `/привязать`."""
        with mock.patch.object(ml, "maybe_route_to_bind_command", return_value=False) as bind, \
             mock.patch.object(ml, "maybe_route_to_set_chat_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_mode_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_ack_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_send_artifact", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_digest_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_reply_understand", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_mixed_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_pending_status_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_feedback_reply", return_value=False), \
             mock.patch.object(ml, "send_message", return_value=1):
            ml.process_message("tok", OWNER, _msg("/привязать", chat=-100999))
        # Роут ПОЗВАЛИ, хотя чат не обслуживается: DM-гейт стоит ниже.
        bind.assert_called_once()


class TestPortalLinkConfig(unittest.TestCase):
    """Адрес ручки и отказ ходить по открытому каналу."""

    def test_url_derived_from_ingest(self):
        with mock.patch.dict(
            "os.environ",
            {"MEETING_NOTARY_PORTAL_INGEST_URL": "https://p.example/api/meetings/notary/ingest"},
            clear=False,
        ):
            self.assertEqual(portal_link.endpoint(), "https://p.example/api/meetings/notary/link")

    def test_explicit_url_wins(self):
        with mock.patch.dict(
            "os.environ",
            {
                "MEETING_NOTARY_PORTAL_INGEST_URL": "https://p.example/api/meetings/notary/ingest",
                "MEETING_NOTARY_PORTAL_LINK_URL": "https://other.example/link",
            },
            clear=False,
        ):
            self.assertEqual(portal_link.endpoint(), "https://other.example/link")

    def test_http_is_refused(self):
        """🔴 Секрет по открытому каналу не уходит никогда — как у соседа."""
        with mock.patch.dict(
            "os.environ",
            {
                "MEETING_NOTARY_PORTAL_INGEST_URL": "http://p.example/api/meetings/notary/ingest",
                "MEETING_NOTARY_PORTAL_INGEST_TOKEN": "t" * 40,
                "MEETING_NOTARY_PORTAL_LINK_URL": "",
            },
            clear=False,
        ):
            self.assertEqual(portal_link.config_problem(), "portal_link_url_not_https")

    def test_no_network_when_unconfigured(self):
        """Настройки нет — в сеть не идём вовсе, а не «пробуем и падаем»."""
        called = {"n": 0}

        def _transport(*_args, **_kwargs):
            called["n"] += 1
            raise AssertionError("не должны были ходить в сеть")

        with mock.patch.dict(
            "os.environ",
            {"MEETING_NOTARY_PORTAL_INGEST_URL": "", "MEETING_NOTARY_PORTAL_LINK_URL": ""},
            clear=False,
        ):
            result = portal_link.submit_code(
                telegram_user_id=OWNER, telegram_username=None, code=CODE, transport=_transport
            )
        self.assertFalse(result.ok)
        self.assertEqual(called["n"], 0)

    def test_token_not_logged(self):
        secret = "s" * 40
        portal = _FakePortal()
        with mock.patch.dict(
            "os.environ",
            {
                "MEETING_NOTARY_PORTAL_INGEST_URL": "https://p.example/api/meetings/notary/ingest",
                "MEETING_NOTARY_PORTAL_INGEST_TOKEN": secret,
                "MEETING_NOTARY_PORTAL_LINK_URL": "",
            },
            clear=False,
        ):
            with self.assertLogs("notary.portal_link", level="INFO") as captured:
                portal_link.submit_code(
                    telegram_user_id=OWNER,
                    telegram_username=None,
                    code=CODE,
                    transport=portal,
                )
        joined = "\n".join(captured.output)
        self.assertNotIn(secret, joined)
        self.assertNotIn(CODE, joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
