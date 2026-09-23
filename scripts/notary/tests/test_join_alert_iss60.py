"""ISS-60: сообщение о сорванном входе — адресаты «туда же, куда протокол» и тексты.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_join_alert_iss60 -v
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.lib import delivery_route, join_alert as ja  # noqa: E402

NOW = datetime(2026, 9, 28, 11, 37, tzinfo=timezone.utc)  # 14:37 МСК
OWNER = 111
GROUP = -1001
ORG = 222

WATCHED = {"watched": [
    {"id": "anzhee-direktorat", "series": "anzhee-direktorat", "telegram_chat_id": GROUP},
    {"id": "solo", "series": "solo"},
]}


def entry(**kw):
    e = {"series": "anzhee-direktorat", "start_at": "2026-09-28T11:40:00+00:00",
         "container": "vexa-notarius-auto-x-a2",
         "expected_participants": ["Иван Секретов", "Мария Тайнова"]}
    e.update(kw)
    return e


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"TELEGRAM_NOTARIUS_CHAT_ID": str(OWNER)}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("MEETING_NOTARY_JOIN_ALERT_TO_OWNER", None)
        n = mock.patch.object(ja, "meeting_name", side_effect=lambda e: {"anzhee-direktorat": "Директорат"}.get(e.get("series"), e.get("series_title") or e.get("series")))
        n.start()
        self.addCleanup(n.stop)

    def build(self, e, reason="name_input_not_found", **kw):
        return ja.build_messages(e, reason, attempts=2, watched=WATCHED, now=NOW, **kw)

    def assertNoPrivate(self, msgs):
        for m in msgs:
            for secret in ("Секретов", "Тайнова", "example.invalid"):
                self.assertNotIn(secret, m.text)


class OwnerSeriesTest(Base):
    def test_group_series_gets_people_text_and_owner_tech(self):
        msgs = self.build(entry(), crash_kit="/transcripts/.crash-kits/auto-x-a2")
        self.assertEqual([(m.audience, m.chat_id) for m in msgs],
                         [(ja.AUD_GROUP, GROUP), (ja.AUD_OWNER_TECH, OWNER)])
        people, tech = msgs[0].text, msgs[1].text
        self.assertIn("«Директорат»", people)
        self.assertIn("сегодня в 14:40 МСК", people)
        self.assertIn("сбой в работе бота-протоколиста", people)
        self.assertIn("включите запись в интерфейсе Яндекс Телемоста", people)
        self.assertIn("перешлите запись Илье", people)
        self.assertNotIn("name_input_not_found", people)  # никаких кодов людям
        self.assertIn("Людям написал: в чат серии", tech)
        self.assertIn(".crash-kits/auto-x-a2", tech)
        self.assertIn("Попыток: 2", tech)
        self.assertNoPrivate(msgs)

    def test_series_without_group_goes_to_owner_once(self):
        msgs = self.build(entry(series="solo"))
        self.assertEqual([(m.audience, m.chat_id) for m in msgs], [(ja.AUD_OWNER, OWNER)])
        self.assertIn("обработай запись", msgs[0].text)
        self.assertIn("включи запись в интерфейсе Яндекс Телемоста", msgs[0].text)

    def test_meeting_problem_goes_only_to_owner(self):
        for reason in ("meeting_not_found", "admission_rejected"):
            msgs = self.build(entry(), reason)
            self.assertEqual([m.audience for m in msgs], [ja.AUD_OWNER_TECH], reason)
            self.assertIn("людям не писал", msgs[0].text)

    def test_waiting_room_is_not_called_bot_failure(self):
        msgs = self.build(entry(), "waiting_room_not_admitted")
        self.assertIn("не впустили из зала ожидания", msgs[0].text)
        self.assertNotIn("сбой в работе", msgs[0].text)

    def test_registry_error_falls_back_to_owner(self):
        with mock.patch("notary.cli.registry.get_telegram_chat_id_for_series", side_effect=OSError("x")):
            msgs = self.build(entry())
        self.assertEqual([m.audience for m in msgs], [ja.AUD_OWNER])


class PortalTest(Base):
    def route(self, mode, chat=None, owner_user=None):
        return delivery_route.Route(mode=mode, chat_id=chat, owner_user_id=owner_user, company="anzhee")

    def build_portal(self, route, reason="still_in_lobby"):
        with mock.patch("notary.lib.orders.get_order", return_value={"id": "o1", "company": "anzhee"}), \
             mock.patch.object(delivery_route, "resolve", return_value=route):
            return self.build(entry(series="portal-o1", order_id="o1", series_title="Планёрка отдела"), reason)

    def test_portal_group(self):
        msgs = self.build_portal(self.route("group", chat=-2002, owner_user=ORG))
        self.assertEqual([(m.audience, m.chat_id) for m in msgs],
                         [(ja.AUD_GROUP_PORTAL, -2002), (ja.AUD_OWNER_TECH, OWNER)])
        self.assertIn("перешлите запись организатору встречи", msgs[0].text)
        self.assertNotIn("Илье", msgs[0].text)

    def test_portal_dm_goes_to_organizer_and_owner(self):
        msgs = self.build_portal(self.route("dm", chat=ORG, owner_user=ORG))
        self.assertEqual([(m.audience, m.chat_id) for m in msgs],
                         [(ja.AUD_ORGANIZER, ORG), (ja.AUD_OWNER_TECH, OWNER)])
        self.assertIn("вашей встрече", msgs[0].text)

    def test_portal_only_with_known_organizer(self):
        msgs = self.build_portal(self.route("portal_only", owner_user=ORG))
        self.assertEqual([(m.audience, m.chat_id) for m in msgs],
                         [(ja.AUD_ORGANIZER, ORG), (ja.AUD_OWNER_TECH, OWNER)])

    def test_portal_only_unknown_organizer_goes_to_owner(self):
        msgs = self.build_portal(self.route("portal_only"))
        self.assertEqual([m.audience for m in msgs], [ja.AUD_OWNER])

    def test_missing_order_goes_to_owner(self):
        with mock.patch("notary.lib.orders.get_order", return_value=None):
            msgs = self.build(entry(series="portal-o1", order_id="o1"))
        self.assertEqual([m.audience for m in msgs], [ja.AUD_OWNER])


class SendTest(Base):
    def test_sends_each_message_once(self):
        sent = []
        rep = ja.send(entry(), "name_input_not_found", attempts=2, watched=WATCHED, now=NOW,
                      sender=lambda chat, text: sent.append(chat) or True, owner_push=lambda t: True)
        self.assertEqual(sent, [GROUP, OWNER])
        self.assertTrue(all(r["sent"] for r in rep))

    def test_test_mode_redirects_everything_to_owner(self):
        sent = []
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_JOIN_ALERT_TO_OWNER": "1"}):
            ja.send(entry(), "name_input_not_found", attempts=2, watched=WATCHED, now=NOW,
                    sender=lambda chat, text: sent.append((chat, text)) or True, owner_push=lambda t: True)
        self.assertEqual([c for c, _ in sent], [OWNER, OWNER])
        self.assertTrue(sent[0][1].startswith("[ТЕСТ — на бою ушло бы: чат серии]"))

    def test_group_send_failure_tells_owner(self):
        pushed = []

        def sender(chat, text):
            if chat == GROUP:
                raise RuntimeError("chat not found")
            return True

        ja.send(entry(), "name_input_not_found", attempts=2, watched=WATCHED, now=NOW,
                sender=sender, owner_push=lambda t: pushed.append(t) or True)
        self.assertEqual(len(pushed), 1)
        self.assertIn("Не смог написать людям", pushed[0])

    def test_owner_message_falls_back_to_push(self):
        pushed = []
        ja.send(entry(series="solo"), "name_input_not_found", attempts=2, watched=WATCHED, now=NOW,
                sender=lambda chat, text: False, owner_push=lambda t: pushed.append(t) or True)
        self.assertEqual(len(pushed), 1)


class WhenTextTest(unittest.TestCase):
    def test_today_and_other_day(self):
        self.assertEqual(ja.when_text("2026-09-28T11:40:00+00:00", NOW), "сегодня в 14:40 МСК")
        self.assertEqual(ja.when_text("2026-09-29T07:00:00Z", NOW), "29.09 в 10:00 МСК")
        self.assertEqual(ja.when_text("мусор", NOW), "в назначенное время")


if __name__ == "__main__":
    unittest.main()
