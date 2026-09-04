#!/usr/bin/env python3
"""Сторож класса «запись стирает сама себя».

Два хранилища прунят старьё ПРЯМО НА ЗАПИСИ. Отсечка обязана считаться по моменту
СОЗДАНИЯ записи (`at`), а не по дате документа: иначе документ, датированный давно
(протокол встречи, собранный с опозданием — `finalize-meeting` берёт дату из `startTs`;
дайджест за прошедшую дату), пишет запись и тут же её теряет.

Что ломается без этого, дословно из соседнего кода:
  • нумерация: ответ «1 сделал, 2 в работе» по такому протоколу не резолвится, молча;
  • идемпотентность: `already_sent` вечно False → плановый дайджест уходит повторно
    каждый тик, «и так до начала встречи» (см. аварийную ветку morning_digest).

Тесты НАМЕРЕННО считают даты относительно `date.today()` и потому не протухают от хода
календаря — ровно та ошибка, из-за которой этот дефект вскрывался трижды и трижды
чинился в фикстурах вместо кода (см. test_iss36_phase4_ordinal.py, шапка).

Правило прунинга — как в lib/reply_ack_state.py: записи без валидного `at` НЕ стареют.
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notary.lib import morning_digest as md  # noqa: E402
from notary.lib import series_memory as sm  # noqa: E402

_TASK = "Илья: подготовить расчёт"


def _ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


class TestSnapshotPrune(unittest.TestCase):
    """`record_open_tasks_snapshot` — слепок нумерации висяков."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _write(self, date_str, mid=4242):
        return sm.record_open_tasks_snapshot(
            self.dir, mid, [_TASK], surface="digest", date=date_str)

    def test_old_document_date_survives_its_own_write(self):
        # Протокол встречи двухмесячной давности, собранный сегодня.
        self._write(_ago(sm._KEEP_SNAPSHOT_DAYS + 15))
        self.assertEqual(sm.open_tasks_snapshot_for_message(self.dir, 4242), [_TASK],
                         "слепок стёрт той же записью, что его сделала")

    def test_today_document_date_survives(self):
        self._write(_ago(0))
        self.assertEqual(sm.open_tasks_snapshot_for_message(self.dir, 4242), [_TASK])

    def test_records_created_long_ago_are_still_pruned(self):
        # Ретенция обязана продолжать работать — «починка» не должна быть
        # «выкинули прунинг».
        self._write(_ago(0), mid=1111)
        path = sm.open_tasks_snapshot_path(self.dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["snapshots"]["1111"]["at"] = f"{_ago(sm._KEEP_SNAPSHOT_DAYS + 10)}T09:00:00+00:00"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self._write(_ago(0), mid=2222)  # запись триггерит прун
        self.assertIsNone(sm.open_tasks_snapshot_for_message(self.dir, 1111),
                          "старая по времени СОЗДАНИЯ запись обязана выпруниваться")
        self.assertEqual(sm.open_tasks_snapshot_for_message(self.dir, 2222), [_TASK])

    def test_record_without_creation_stamp_does_not_age(self):
        # Консервативно, как в reply_ack_state: не знаем возраст — не выкидываем.
        self._write(_ago(0), mid=1111)
        path = sm.open_tasks_snapshot_path(self.dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["snapshots"]["1111"].pop("at", None)
        payload["snapshots"]["1111"]["date"] = _ago(sm._KEEP_SNAPSHOT_DAYS + 30)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self._write(_ago(0), mid=2222)
        self.assertEqual(sm.open_tasks_snapshot_for_message(self.dir, 1111), [_TASK])


class TestDigestSentPrune(unittest.TestCase):
    """`record_sent` — идемпотентность дайджеста и якорь reply-канала."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_old_date_send_is_recorded_and_idempotent(self):
        old = _ago(md._KEEP_SENT_DAYS + 15)
        md.record_sent(self.dir, old, chat_id=-100500, message_id=4242, mode="carry")
        self.assertTrue(md.already_sent(self.dir, old),
                        "отметка отправки стёрта → плановый тик пошлёт дайджест снова")

    def test_old_date_send_remembers_its_mode(self):
        old = _ago(md._KEEP_SENT_DAYS + 15)
        md.record_sent(self.dir, old, chat_id=-100500, message_id=4242, mode="carry")
        self.assertEqual(md.digest_mode_for_message(self.dir, -100500, 4242), "carry")

    def test_today_send_is_recorded(self):
        today = _ago(0)
        md.record_sent(self.dir, today, chat_id=-100500, message_id=4242, mode="carry")
        self.assertTrue(md.already_sent(self.dir, today))

    def test_records_created_long_ago_are_still_pruned(self):
        old_date = _ago(3)
        md.record_sent(self.dir, old_date, chat_id=-100500, message_id=1111)
        path = md.digest_state_path(self.dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sent"][old_date]["at"] = f"{_ago(md._KEEP_SENT_DAYS + 10)}T09:00:00+00:00"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        md.record_sent(self.dir, _ago(0), chat_id=-100500, message_id=2222)
        self.assertFalse(md.already_sent(self.dir, old_date),
                         "старая по времени СОЗДАНИЯ отметка обязана выпруниваться")
        self.assertTrue(md.already_sent(self.dir, _ago(0)))

    def test_record_without_creation_stamp_does_not_age(self):
        old_date = _ago(3)
        md.record_sent(self.dir, old_date, chat_id=-100500, message_id=1111)
        path = md.digest_state_path(self.dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sent"][old_date].pop("at", None)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        md.record_sent(self.dir, _ago(0), chat_id=-100500, message_id=2222)
        self.assertTrue(md.already_sent(self.dir, old_date))


if __name__ == "__main__":
    unittest.main(verbosity=1)
