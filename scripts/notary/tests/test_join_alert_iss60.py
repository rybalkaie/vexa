"""ISS-60: сообщения о сбоях бота — адресаты «туда же, куда протокол» и тексты.

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
    {"id": "to-owner", "series": "to-owner", "telegram_chat_id": OWNER},
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
        names = {"anzhee-direktorat": "Директорат"}
        n = mock.patch.object(ja, "meeting_name",
                              side_effect=lambda e: names.get(e.get("series"), e.get("series_title") or e.get("series")))
        n.start()
        self.addCleanup(n.stop)

    def run_send(self, e, reason="name_input_not_found", *, fail_chats=(), **kw):
        sent = []

        def sender(chat, text):
            if chat in fail_chats:
                raise RuntimeError("chat not found")
            sent.append((chat, text))
            return True

        pushed = []
        rep = ja.send(e, reason, watched=WATCHED, now=NOW, sender=sender,
                      owner_push=lambda t: pushed.append(t) or True, **kw)
        return sent, pushed, rep

    def assertNoPrivate(self, texts):
        for t in texts:
            for secret in ("Секретов", "Тайнова", "example.invalid", "telemost.yandex.ru/j/"):
                self.assertNotIn(secret, t)


class OwnerSeriesTest(Base):
    def test_group_series_people_then_owner_tech(self):
        sent, _p, _r = self.run_send(entry(), attempts=2, crash_kit="/transcripts/.crash-kits/auto-x-a2")
        self.assertEqual([c for c, _ in sent], [GROUP, OWNER])
        people, tech = sent[0][1], sent[1][1]
        self.assertIn("«Директорат»", people)
        self.assertIn("сегодня в 14:40 МСК", people)
        self.assertIn("сбой в работе бота-протоколиста", people)
        self.assertIn("включите запись в интерфейсе Яндекс Телемоста", people)
        self.assertIn("перешлите запись Илье", people)
        self.assertNotIn("name_input_not_found", people)
        self.assertIn("Людям написал: в чат серии.", tech)
        self.assertIn(".crash-kits/auto-x-a2", tech)
        self.assertIn("Попыток: 2", tech)
        self.assertNoPrivate([t for _, t in sent])

    def test_series_without_group_single_owner_message(self):
        sent, _p, _r = self.run_send(entry(series="solo"))
        self.assertEqual([c for c, _ in sent], [OWNER])
        self.assertIn("обработай запись", sent[0][1])

    def test_series_bound_to_owner_dm_single_message(self):
        # цикл5 У4: адресат протокола = личка владельца → одно сообщение, без «Людям написал»
        sent, _p, _r = self.run_send(entry(series="to-owner"))
        self.assertEqual([c for c, _ in sent], [OWNER])
        self.assertNotIn("Людям написал", sent[0][1])
        self.assertNotIn("перешлите запись Илье", sent[0][1])

    def test_meeting_problem_only_owner(self):
        for reason in ("meeting_not_found", "admission_rejected", "meeting_url_rejected"):
            sent, _p, _r = self.run_send(entry(), reason)
            self.assertEqual([c for c, _ in sent], [OWNER], reason)
            self.assertIn("людям не писал", sent[0][1])

    def test_phase3a_reasons_have_human_text(self):
        # Фаза 3а: новые причины бота — человеческим текстом, не кодом.
        self.assertIn("не узнал экран звонка", ja.reason_human("admission_unclear"))
        self.assertIn("не в Яндекс Телемост", ja.reason_human("meeting_url_rejected"))
        sent, _p, _r = self.run_send(entry(), "admission_unclear")
        self.assertEqual([c for c, _ in sent], [GROUP, OWNER])  # сбой бота — людям тоже
        self.assertIn("сбой в работе бота-протоколиста", sent[0][1])
        self.assertIn("не узнал экран звонка", sent[1][1])
        self.assertNotIn("admission_unclear", sent[1][1])

    def test_waiting_room_failure_not_called_bot_failure(self):
        sent, _p, _r = self.run_send(entry(), "waiting_room_not_admitted")
        self.assertIn("не впустили из зала ожидания", sent[0][1])
        self.assertNotIn("сбой в работе", sent[0][1])

    def test_group_failure_reported_to_owner_with_meeting_name(self):
        # цикл5 У5: владелец видит, какую именно группу не удалось предупредить
        sent, _p, _r = self.run_send(entry(), fail_chats=(GROUP,))
        self.assertEqual([c for c, _ in sent], [OWNER])
        self.assertIn("«Директорат»", sent[0][1])
        self.assertIn("НЕ ДОШЛО, предупреди людей сам", sent[0][1])

    def test_registry_error_falls_back_to_owner(self):
        with mock.patch("notary.cli.registry.get_telegram_chat_id_for_series", side_effect=OSError("x")):
            sent, _p, _r = self.run_send(entry())
        self.assertEqual([c for c, _ in sent], [OWNER])


class EventsTest(Base):
    def test_evicted_goes_to_group_and_owner(self):
        sent, _p, _r = self.run_send(entry(), "evicted_failure", kind=ja.KIND_EVICTED)
        self.assertEqual([c for c, _ in sent], [GROUP, OWNER])
        self.assertIn("выкинуло из Телемоста", sent[0][1])
        self.assertIn("перешлите запись Илье", sent[0][1])

    def test_deaf_goes_to_group_and_owner(self):
        sent, _p, _r = self.run_send(entry(), "deaf_after_start", kind=ja.KIND_DEAF)
        self.assertEqual([c for c, _ in sent], [GROUP, OWNER])
        self.assertIn("перестал слышать", sent[0][1])

    def test_waiting_nudge_only_people(self):
        sent, _p, _r = self.run_send(entry(), "waiting_room", kind=ja.KIND_WAITING)
        self.assertEqual([c for c, _ in sent], [GROUP])
        self.assertIn("Впустите", sent[0][1])

    def test_waiting_nudge_owner_series(self):
        sent, _p, _r = self.run_send(entry(series="solo"), "waiting_room", kind=ja.KIND_WAITING)
        self.assertEqual([c for c, _ in sent], [OWNER])


class PortalTest(Base):
    def route(self, mode, chat=None, owner_user=None):
        return delivery_route.Route(mode=mode, chat_id=chat, owner_user_id=owner_user, company="anzhee")

    def run_portal(self, route, reason="still_in_lobby", order=True, **kw):
        with mock.patch("notary.lib.orders.get_order", return_value={"id": "o1", "company": "anzhee"} if order else None), \
             mock.patch.object(delivery_route, "resolve", return_value=route):
            return self.run_send(entry(series="portal-o1", order_id="o1", series_title="Планёрка отдела"), reason, **kw)

    def test_portal_group(self):
        sent, _p, _r = self.run_portal(self.route("group", chat=-2002, owner_user=ORG))
        self.assertEqual([c for c, _ in sent], [-2002, OWNER])
        self.assertIn("перешлите запись организатору встречи", sent[0][1])
        self.assertNotIn("Илье", sent[0][1])

    def test_portal_dm_goes_to_organizer_and_owner(self):
        sent, _p, _r = self.run_portal(self.route("dm", chat=ORG, owner_user=ORG))
        self.assertEqual([c for c, _ in sent], [ORG, OWNER])
        self.assertIn("вашей встрече", sent[0][1])

    def test_portal_only_with_known_organizer(self):
        sent, _p, _r = self.run_portal(self.route("portal_only", owner_user=ORG))
        self.assertEqual([c for c, _ in sent], [ORG, OWNER])

    def test_portal_unknown_organizer_owner_without_alfred_advice(self):
        # цикл5 У2: протокол чужой компании не должен уйти владельцу через «обработай запись»
        sent, _p, _r = self.run_portal(self.route("portal_only"))
        self.assertEqual([c for c, _ in sent], [OWNER])
        self.assertIn("Встреча сотрудников", sent[0][1])
        self.assertIn("Организатору написать не смог", sent[0][1])
        self.assertNotIn("обработай запись", sent[0][1])

    def test_missing_order_goes_to_owner(self):
        sent, _p, _r = self.run_portal(None, order=False)
        self.assertEqual([c for c, _ in sent], [OWNER])
        self.assertNotIn("обработай запись", sent[0][1])

    def test_portal_meeting_not_found_tells_organizer(self):
        # цикл5 У3: ссылку вводил организатор — ему и сообщаем
        sent, _p, _r = self.run_portal(self.route("group", chat=-2002, owner_user=ORG), "meeting_not_found")
        self.assertEqual([c for c, _ in sent], [ORG, OWNER])
        self.assertIn("Проверьте, пожалуйста, ссылку", sent[0][1])
        self.assertIn("организатору в личку", sent[1][1])


class SendTest(Base):
    def test_test_mode_redirects_with_chat_id(self):
        sent = []
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_JOIN_ALERT_TO_OWNER": "1"}):
            ja.send(entry(), "name_input_not_found", watched=WATCHED, now=NOW,
                    sender=lambda chat, text: sent.append((chat, text)) or True, owner_push=lambda t: True)
        self.assertEqual([c for c, _ in sent], [OWNER, OWNER])
        # цикл5 У7: видно не только тип адресата, но и конкретный чат
        self.assertTrue(sent[0][1].startswith(f"[ТЕСТ — на бою ушло бы: чат серии {GROUP}]"))

    def test_owner_message_falls_back_to_push(self):
        pushed = []
        ja.send(entry(series="solo"), "name_input_not_found", watched=WATCHED, now=NOW,
                sender=lambda chat, text: False, owner_push=lambda t: pushed.append(t) or True)
        self.assertEqual(len(pushed), 1)

    def test_owner_note_in_tech(self):
        sent, _p, _r = self.run_send(entry(), owner_note="повтор не запущен: не нашлось свободного места")
        self.assertIn("Повтор не запущен", sent[1][1])


class WhenTextTest(unittest.TestCase):
    def test_today_and_other_day(self):
        self.assertEqual(ja.when_text("2026-09-28T11:40:00+00:00", NOW), "сегодня в 14:40 МСК")
        self.assertEqual(ja.when_text("2026-09-29T07:00:00Z", NOW), "29.09 в 10:00 МСК")
        self.assertEqual(ja.when_text("мусор", NOW), "в назначенное время")


if __name__ == "__main__":
    unittest.main()
