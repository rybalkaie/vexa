"""Тесты Ф4 (план `2026-07-12-watson-company-memory.md`) — утренний дайджест открытых
вопросов в день встречи серии.

Покрывает критерии приёмки R10–R13:
  - R10: дайджест уходит в день встречи в 09:00 МСК, но НЕ позже чем за час до начала
    (встреча в 09:30 → 08:30); группировка по ответственным; в шапке — инструкция
    владельца ДОСЛОВНО.
  - R11: ответ (reply) на сообщение дайджеста обновляет статус висяка ТЕМ ЖЕ каналом,
    что и ответ на протокол (`pending_status_reply` → sidecar Ф2); статус переживает
    следующую финализацию (закрытое не воскресает).
  - R12: мелочь показывается отдельным блоком «просто напоминаю», в ПРОТОКОЛ не едет
    и после встречи снимается сама (в следующую выжимку не переносится).
  - R13: нет встречи сегодня / нет висяков → дайджест не шлём; в лог — только счётчики,
    без текстов задач (опасная тройка).
  - УПУ2: висяки без ответственного не теряются (отдельный бакет); уже накопленные
    выжимки БЕЗ класса значимости трактуются как значимые (дефолт-класс).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_watson_phase4_morning_digest -v
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import morning_digest as md  # noqa: E402
from lib import pending_status_reply as psr  # noqa: E402
from lib import series_memory as sm  # noqa: E402

MSK = md.MSK


def _msk(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=MSK)


def _queue_item(series: str, start: datetime, mid: str = "m1"):
    return {"meeting_id": mid, "event_id": f"local:{mid}", "series": series,
            "start_at": start.isoformat(), "duration_minutes": 60, "url": "https://t/x",
            "type": "manual"}


class _SeriesFixture:
    """Каталог серии с одной выжимкой: значимые висяки + (опц.) мелочь."""

    def __init__(self, tmp: Path, series: str = "koordinatsiya"):
        self.root = tmp
        self.series = series
        self.dir = tmp / series
        self.dir.mkdir(parents=True, exist_ok=True)

    def write_digest(self, date: str, open_tasks: list):
        sm.save_digest(self.dir, date, {
            "schema": 1, "date": date, "series": self.series,
            "participants": ["Илья Рыбалка", "Татьяна Филиппова"],
            "themes": [], "key_points": [], "open_tasks": open_tasks,
        })


# ==========================================================================
# R10 — КОГДА слать: 09:00 МСК, но не позже чем за час до начала
# ==========================================================================
class TestSendTime(unittest.TestCase):

    def test_default_nine_msk(self):
        # Встреча в 12:00 МСК → дайджест в 09:00 МСК.
        self.assertEqual(md.digest_send_at(_msk(2026, 7, 14, 12)), _msk(2026, 7, 14, 9))

    def test_early_meeting_shifts_earlier(self):
        # R10: встреча в 09:30 → НЕ 09:00, а 08:30 (за час до начала).
        self.assertEqual(md.digest_send_at(_msk(2026, 7, 14, 9, 30)), _msk(2026, 7, 14, 8, 30))

    def test_meeting_before_ten_shifts(self):
        # Встреча ровно в 10:00 → 09:00 (граница: и «09:00», и «за час» совпали).
        self.assertEqual(md.digest_send_at(_msk(2026, 7, 14, 10)), _msk(2026, 7, 14, 9))

    def test_tz_independent_of_local_clock(self):
        # РИСК6: вход в UTC (так пишет очередь планировщика) — считаем в МСК.
        utc_start = _msk(2026, 7, 14, 12).astimezone(md.timezone.utc)
        self.assertEqual(md.digest_send_at(utc_start), _msk(2026, 7, 14, 9))


class TestDueMeetings(unittest.TestCase):

    def test_due_when_time_came(self):
        q = [_queue_item("koordinatsiya", _msk(2026, 7, 14, 12))]
        due = md.due_meetings_today(q, _msk(2026, 7, 14, 9, 1))
        self.assertEqual([r["series"] for r in due], ["koordinatsiya"])
        self.assertEqual(due[0]["date"], "2026-07-14")

    def test_not_due_before_send_time(self):
        q = [_queue_item("koordinatsiya", _msk(2026, 7, 14, 12))]
        self.assertEqual(md.due_meetings_today(q, _msk(2026, 7, 14, 8, 30)), [])

    def test_not_due_after_meeting_started(self):
        # Дайджест «к встрече» задним числом не шлём.
        q = [_queue_item("koordinatsiya", _msk(2026, 7, 14, 12))]
        self.assertEqual(md.due_meetings_today(q, _msk(2026, 7, 14, 12, 5)), [])

    def test_no_meeting_today(self):
        # R13: встреча завтра → сегодня молчим.
        q = [_queue_item("koordinatsiya", _msk(2026, 7, 15, 12))]
        self.assertEqual(md.due_meetings_today(q, _msk(2026, 7, 14, 9, 1)), [])

    def test_two_meetings_same_series_one_digest(self):
        q = [_queue_item("koordinatsiya", _msk(2026, 7, 14, 16), "m2"),
             _queue_item("koordinatsiya", _msk(2026, 7, 14, 12), "m1")]
        due = md.due_meetings_today(q, _msk(2026, 7, 14, 9, 1))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["start_at"], _msk(2026, 7, 14, 12))  # самая ранняя


# ==========================================================================
# R10/УПУ2 — ЧТО в дайджесте: группировка, безымянные, шапка владельца
# ==========================================================================
class TestDigestMarkdown(unittest.TestCase):

    def test_header_instruction_verbatim(self):
        # Слова владельца (2026-07-12) — дословно, без переписывания.
        out = md.build_digest_markdown("koordinatsiya", "2026-07-14", ["Илья: подготовить расчёт"], [])
        self.assertIn(md.HEADER_INSTRUCTION, out)
        self.assertIn("бот услышит и пометит сам", out)
        self.assertIn("напишите ответом на это сообщение текущий статус", out)

    def test_grouped_by_owner(self):
        out = md.build_digest_markdown(
            "koordinatsiya", "2026-07-14",
            ["Илья: подготовить расчёт", "Татьяна: согласовать договор", "Илья: решить по найму"],
            [],
        )
        self.assertIn("## Илья", out)
        self.assertIn("## Татьяна", out)
        self.assertIn("- подготовить расчёт", out)
        self.assertIn("- решить по найму", out)

    def test_nameless_bucket_not_swallowed(self):
        # УПУ2: висяк без ответственного не теряется — отдельный бакет.
        out = md.build_digest_markdown(
            "koordinatsiya", "2026-07-14",
            ["определиться со сроком запуска", "Илья: подготовить расчёт"], [],
        )
        self.assertIn("Без ответственного", out)
        self.assertIn("- определиться со сроком запуска", out)

    def test_minor_block_present_and_separate(self):
        # R12: мелочь — отдельным блоком «просто напоминаю», без запроса статуса.
        out = md.build_digest_markdown(
            "koordinatsiya", "2026-07-14", ["Илья: подготовить расчёт"], ["Илья: отправить файл"],
        )
        self.assertIn("Просто напоминаю", out)
        self.assertIn("статус по ним писать не нужно", out)
        i_minor = out.index("Просто напоминаю")
        self.assertLess(out.index("подготовить расчёт"), i_minor)  # значимое выше мелочи

    def test_empty_gives_empty_string(self):
        self.assertEqual(md.build_digest_markdown("s", "2026-07-14", [], []), "")

    def test_caption_carries_instruction_and_fits_telegram(self):
        cap = md.build_caption("koordinatsiya", "2026-07-14", ["Илья: расчёт"], ["Илья: файл"])
        self.assertIn(md.HEADER_INSTRUCTION, cap)  # отвечают реплаем на СООБЩЕНИЕ, не на PDF
        self.assertLessEqual(len(cap), 1024)


# ==========================================================================
# R12/НЕС1 — «tag-and-keep»: мелочь хранится, но в протокол не едет
# ==========================================================================
class TestSignificanceTagAndKeep(unittest.TestCase):

    def _classifier(self, tasks):
        return ["отправить файл" not in t.lower() for t in tasks]

    def test_minor_tagged_not_dropped(self):
        out = sm.tag_open_tasks_significance(
            ["Илья: подготовить расчёт", "Илья: отправить файл"], classifier=self._classifier)
        self.assertEqual(out[0], "Илья: подготовить расчёт")          # значимое — голая строка
        self.assertEqual(out[1], {"text": "Илья: отправить файл", "minor": True})

    def test_gate_off_keeps_everything_plain(self):
        # Гейт дефолт-OFF и нет инъекции → claude НЕ зовётся, ничего не метим.
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ENABLE_OPEN_TASKS_SIGNIFICANCE_FILTER", None)
            out = sm.tag_open_tasks_significance(["a", "b"])
        self.assertEqual(out, ["a", "b"])

    def test_classifier_failure_keeps_all_significant(self):
        def boom(_tasks):
            raise RuntimeError("boom")
        out = sm.tag_open_tasks_significance(["a", "b"], classifier=boom)
        self.assertEqual(out, ["a", "b"])  # консервативно: мелочи нет, ничего не спрятано

    def test_minor_excluded_from_protocol_tail(self):
        # R12: помеченная мелочь в раздел протокола «🔻 С прошлых встреч» НЕ попадает —
        # ровно то поведение, что было у старого «drop».
        digests = [{"date": "2026-07-13", "open_tasks": [
            "Илья: подготовить расчёт", {"text": "Илья: отправить файл", "minor": True}]}]
        self.assertEqual(sm.resolve_open_tasks(digests), ["Илья: подготовить расчёт"])
        self.assertEqual(sm.resolve_minor_open_tasks(digests), ["Илья: отправить файл"])

    def test_legacy_plain_tail_is_significant_by_default(self):
        # УПУ2: выжимки, накопленные ДО гейта значимости (голые строки) → дефолт-класс
        # «значимо»: показываем, а не скрываем.
        digests = [{"date": "2026-07-13", "open_tasks": ["Илья: подготовить расчёт"]}]
        self.assertEqual(sm.resolve_open_tasks(digests), ["Илья: подготовить расчёт"])
        self.assertEqual(sm.resolve_minor_open_tasks(digests), [])

    def test_minor_autodrops_next_meeting(self):
        # R12: мелочь живёт одну встречу. Протокол следующей встречи её не несёт (см.
        # test выше) → новая выжимка (build_digest по этому протоколу) её не содержит.
        proto_next = (
            "#протоколвстречи 14.07.2026\n\n**Участники:** Илья Рыбалка\n\n"
            "## Задачи\n\n**Илья**\n- подготовить расчёт стоимости\n"
        )
        d2 = sm.build_digest(proto_next, {"series": "koordinatsiya", "date": "2026-07-14"})
        joined = " | ".join(sm._open_task_text(t) for t in (d2.get("open_tasks") or []))
        self.assertNotIn("отправить файл", joined)


# ==========================================================================
# R13 — молчим, когда нечего слать; идемпотентность; счётчики в логе
# ==========================================================================
class TestSendGuards(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.fx = _SeriesFixture(self.tmp)
        self.sent: list = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def _sender(self, chat_id, md_text, caption, date):
        self.sent.append({"chat_id": chat_id, "md": md_text, "caption": caption, "date": date})
        return 4242

    def _send(self, **kw):
        return md.send_series_digest(
            self.fx.series, date="2026-07-14", root=self.tmp,
            chat_resolver=lambda _s: -100500, sender=self._sender, **kw)

    def test_no_open_tasks_no_message(self):
        # R13: висяков нет → дайджест не шлём.
        self.fx.write_digest("2026-07-13", [])
        res = self._send()
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["reason"], "empty")
        self.assertEqual(self.sent, [])

    def test_no_digests_at_all_no_message(self):
        res = self._send()
        self.assertEqual(res["reason"], "empty")
        self.assertEqual(self.sent, [])

    def test_sends_and_records_message_id(self):
        self.fx.write_digest("2026-07-13", ["Илья: подготовить расчёт"])
        res = self._send()
        self.assertEqual(res["status"], "sent")
        self.assertEqual(res["message_id"], 4242)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(md.already_sent(self.fx.dir, "2026-07-14"))

    def test_idempotent_second_tick_silent(self):
        # Таймер тикает каждые 15 мин — второй тик того же дня НЕ шлёт дубль.
        self.fx.write_digest("2026-07-13", ["Илья: подготовить расчёт"])
        self._send()
        res2 = self._send()
        self.assertEqual(res2["reason"], "already")
        self.assertEqual(len(self.sent), 1)

    def test_no_chat_binding_no_send(self):
        self.fx.write_digest("2026-07-13", ["Илья: подготовить расчёт"])
        res = md.send_series_digest(
            self.fx.series, date="2026-07-14", root=self.tmp,
            chat_resolver=lambda _s: None, sender=self._sender)
        self.assertEqual(res["reason"], "no-chat")
        self.assertEqual(self.sent, [])

    def test_closed_task_not_reminded(self):
        # Статус из sidecar (reply/сверщик) переживает: закрытое в дайджест не тянем.
        self.fx.write_digest("2026-07-13", ["Илья: подготовить расчёт", "Илья: согласовать договор"])
        sm.set_task_status(self.fx.dir, "Илья: подготовить расчёт", sm.STATUS_DONE,
                           reason="по ответу", source="reply", date="2026-07-13")
        res = self._send()
        self.assertEqual(res["open"], 1)
        self.assertIn("согласовать договор", self.sent[0]["md"])
        self.assertNotIn("подготовить расчёт", self.sent[0]["md"])

    def test_logs_carry_no_task_text(self):
        # Опасная тройка: в лог — счётчики, не тексты задач.
        self.fx.write_digest("2026-07-13", ["Илья: подготовить секретный расчёт по марже"])
        with self.assertLogs("notary.morning_digest", level="INFO") as cm:
            md.run(now=_msk(2026, 7, 14, 9, 1),
                   queue=[_queue_item(self.fx.series, _msk(2026, 7, 14, 12))],
                   root=self.tmp, chat_resolver=lambda _s: -100500, sender=self._sender)
        joined = " ".join(cm.output)
        self.assertNotIn("марже", joined)
        self.assertNotIn("секретный", joined)
        self.assertIn("open=1", joined)

    def test_run_skips_when_no_meeting_today(self):
        self.fx.write_digest("2026-07-13", ["Илья: подготовить расчёт"])
        out = md.run(now=_msk(2026, 7, 14, 9, 1),
                     queue=[_queue_item(self.fx.series, _msk(2026, 7, 15, 12))],
                     root=self.tmp, chat_resolver=lambda _s: -100500, sender=self._sender)
        self.assertEqual(out, [])
        self.assertEqual(self.sent, [])

    def test_kill_switch_off(self):
        self.fx.write_digest("2026-07-13", ["Илья: подготовить расчёт"])
        with mock.patch.dict("os.environ", {"ENABLE_MORNING_DIGEST": "0"}):
            out = md.run(now=_msk(2026, 7, 14, 9, 1),
                         queue=[_queue_item(self.fx.series, _msk(2026, 7, 14, 12))],
                         root=self.tmp, chat_resolver=lambda _s: -100500, sender=self._sender)
        self.assertEqual(out, [])
        self.assertEqual(self.sent, [])

    def test_send_failure_does_not_mark_sent(self):
        # Сбой отправки → день НЕ помечен «слали»: следующий тик попробует снова.
        self.fx.write_digest("2026-07-13", ["Илья: подготовить расчёт"])
        res = md.send_series_digest(
            self.fx.series, date="2026-07-14", root=self.tmp,
            chat_resolver=lambda _s: -100500, sender=lambda *a, **k: None)
        self.assertEqual(res["reason"], "send-failed")
        self.assertFalse(md.already_sent(self.fx.dir, "2026-07-14"))


# ==========================================================================
# R11 — reply на дайджест обновляет статус ТЕМ ЖЕ каналом (РИСК5)
# ==========================================================================
class TestReplyChannel(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.fx = _SeriesFixture(self.tmp)
        self.fx.write_digest("2026-07-13", ["Илья: подготовить расчёт стоимости",
                                            "Татьяна: согласовать договор с юристом"])
        md.send_series_digest(self.fx.series, date="2026-07-14", root=self.tmp,
                              chat_resolver=lambda _s: -100500,
                              sender=lambda *a, **k: 777)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_digest_message_resolves_to_series(self):
        # РИСК5: id дайджеста НЕТ в индексе доставленных протоколов → своя карта.
        self.assertEqual(
            md.digest_series_for_message(-100500, 777, root=self.tmp), self.fx.series)

    def test_foreign_message_id_not_resolved(self):
        self.assertIsNone(md.digest_series_for_message(-100500, 778, root=self.tmp))
        self.assertIsNone(md.digest_series_for_message(-1, 777, root=self.tmp))

    def test_reply_updates_status_same_writer(self):
        # R11: тот же `pending_status_reply` → тот же sidecar Ф2.
        res = psr.apply_status_reply(
            "расчёт стоимости сделали", self.fx.dir, date="2026-07-14", source="reply")
        self.assertIsNotNone(res)
        self.assertEqual(res["status"], sm.STATUS_DONE)
        store = sm.load_task_status(self.fx.dir)
        rec = store[sm._status_key("Илья: подготовить расчёт стоимости")]
        self.assertEqual(rec["status"], sm.STATUS_DONE)

    def test_status_survives_next_finalization(self):
        # Инвариант lifecycle: закрытое ответом на дайджест не воскресает висящим.
        psr.apply_status_reply("расчёт стоимости сделали", self.fx.dir,
                               date="2026-07-14", source="reply")
        digests = sm.list_series_digests(self.fx.dir)
        merged = sm.merge_open_tasks_with_status(
            sm.resolve_open_tasks(digests), sm.load_task_status(self.fx.dir))
        self.assertNotIn("Илья: подготовить расчёт стоимости", merged["open"])
        self.assertIn("Татьяна: согласовать договор с юристом", merged["open"])

    def test_minor_reply_matches_minor_item(self):
        # Мелочь видна участнику в дайджесте → ответ по ней должен попасть В НЕЁ,
        # а не сматчиться на похожий значимый висяк.
        self.fx.write_digest("2026-07-13", [
            "Илья: подготовить расчёт стоимости",
            {"text": "Илья: отправить файл сметы", "minor": True},
        ])
        res = psr.apply_status_reply("файл сметы — сделано", self.fx.dir,
                                     date="2026-07-14", source="reply")
        self.assertIsNotNone(res)
        self.assertEqual(res["matched"], "Илья: отправить файл сметы")


# ==========================================================================
# Достижимость из боевого входа: listener роутит reply на дайджест
# ==========================================================================
class TestListenerWiring(unittest.TestCase):

    def test_listener_calls_digest_resolver(self):
        src = (_NOTARY / "meetings_listener.py").read_text(encoding="utf-8")
        self.assertIn("_reply_context_series_digest", src)
        self.assertIn("morning_digest.digest_series_for_message", src)

    def test_finalize_uses_tag_and_keep(self):
        # НЕС1: боевая финализация обязана ПОМЕЧАТЬ мелочь, а не выбрасывать её —
        # иначе дайджесту нечего показать в блоке «просто напоминаю».
        src = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        self.assertIn("tag_open_tasks_significance", src)
        self.assertNotIn("significance_filter=lambda _tasks: series_memory.significant_open_tasks",
                         src)

    def test_systemd_units_exist(self):
        repo = _NOTARY.parents[2]  # …/meeting-notary/vexa/scripts/notary → …/meeting-notary
        unit = repo / "systemd" / "meeting-notary-morning-digest.service"
        timer = repo / "systemd" / "meeting-notary-morning-digest.timer"
        self.assertTrue(unit.is_file(), f"нет юнита: {unit}")
        self.assertTrue(timer.is_file(), f"нет таймера: {timer}")
        self.assertIn("send_morning_digest.py", unit.read_text(encoding="utf-8"))

    def test_tick_grid_matches_timer(self):
        # `TICK_MINUTES` — то, на что код округляет момент отправки; сетка таймера —
        # то, когда он реально может выстрелить. Разъедутся → «не позже чем за час до
        # начала» поплывёт МОЛЧА. Пусть лучше падает тест.
        repo = _NOTARY.parents[2]
        timer = (repo / "systemd" / "meeting-notary-morning-digest.timer").read_text(encoding="utf-8")
        self.assertIn(f"OnCalendar=*:0/{md.TICK_MINUTES}", timer,
                      f"сетка таймера разошлась с TICK_MINUTES={md.TICK_MINUTES}")


# ==========================================================================
# Сторожа инвариантов, найденных циклом из 5 ходов (ход 1)
# ==========================================================================
class TestCycleGuards(unittest.TestCase):
    """Инварианты, которые дайджест обязан держать наравне с протоколом."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.fx = _SeriesFixture(self.tmp)
        self.sent: list = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def _sender(self, chat_id, md_text, caption, date):
        self.sent.append({"chat_id": chat_id, "date": date})
        return 4242

    def _send(self, **kw):
        return md.send_series_digest(
            self.fx.series, date="2026-07-14", root=self.tmp,
            chat_resolver=lambda _s: -100500, sender=self._sender, **kw)

    # Н1 — кап применяется ПОСЛЕ наложения статусов, как в каноне протокола.
    def test_closed_tasks_do_not_eat_cap_of_live_ones(self):
        # Хвост: 3 закрытые задачи ИДУТ ПЕРВЫМИ, за ними 2 живых. Кап = 3.
        # Капнув ДО статусов (баг), дайджест увидел бы только 3 закрытых → 0 висяков.
        # Канон: раскрутить хвост целиком → наложить статусы → капить уже ВИСЯЩИЕ → 2.
        closed = [f"Илья: закрытая задача {i}" for i in range(1, 4)]
        live = ["Илья: живой висяк A", "Татьяна: живой висяк Б"]
        self.fx.write_digest("2026-07-13", closed + live)
        for t in closed:
            sm.set_task_status(self.fx.dir, t, sm.STATUS_DONE, source="test")
        with mock.patch.dict("os.environ", {"OPEN_TASKS_MAX": "3"}):
            items = md.collect_items(self.fx.dir)
        self.assertEqual(items["open"], live,
                         "закрытые задачи съели слоты капа у живых висяков")

    def test_minor_cap_also_applied_after_status(self):
        # То же для второй корзины: закрытая руками мелочь не должна съедать кап.
        closed_minor = [{"text": f"Илья: мелочь закрытая {i}", "minor": True} for i in range(1, 4)]
        live_minor = [{"text": "Илья: мелочь живая", "minor": True}]
        self.fx.write_digest("2026-07-13", ["Илья: значимый висяк"] + closed_minor + live_minor)
        for t in closed_minor:
            sm.set_task_status(self.fx.dir, t["text"], sm.STATUS_DONE, source="test")
        with mock.patch.dict("os.environ", {"OPEN_TASKS_MAX": "3"}):
            items = md.collect_items(self.fx.dir)
        self.assertEqual(items["minor"], ["Илья: мелочь живая"])

    # Н2 — аварийный рычаг учёта висяков гасит и этот канал.
    def test_open_tasks_kill_switch_silences_digest(self):
        # `ENABLE_OPEN_TASKS_TRACKING=0` — рубильник «поехал трекинг, гаси показ».
        # Он гасит раздел висяков в протоколе и ОБЯЗАН гасить дайджест: иначе владелец
        # продолжит получать PDF с теми же битыми данными и не найдёт, чем их унять.
        self.fx.write_digest("2026-07-13", ["Илья: висяк"])
        with mock.patch.dict("os.environ", {"ENABLE_OPEN_TASKS_TRACKING": "0"}):
            self.assertFalse(md.is_enabled())
            res = self._send()
        self.assertEqual(self.sent, [], "дайджест ушёл при выключенном учёте висяков")
        self.assertEqual(res["reason"], "disabled")

    def test_digest_kill_switch_still_works_independently(self):
        # Свой рубильник дайджеста продолжает работать сам по себе (не регресс).
        with mock.patch.dict("os.environ", {"ENABLE_MORNING_DIGEST": "0"}):
            self.assertFalse(md.is_enabled())

    # Н3 — «не позже чем за час» держится на сетке таймера, а не только на бумаге.
    def test_send_time_never_later_than_one_hour_before(self):
        # Отправка возможна ТОЛЬКО на тике сетки `OnCalendar=*:0/15`. Для каждой минуты
        # старта проверяем: тик, на котором дайджест реально уйдёт, не позже start-1ч.
        for minute in range(0, 60):
            start = _msk(2026, 7, 14, 9, minute)  # утренние встречи — самый жёсткий случай
            send_at = md.digest_send_at(start)
            self.assertEqual(send_at.minute % md.TICK_MINUTES, 0,
                             f"момент отправки не лёг на сетку таймера (старт 09:{minute:02d})")
            self.assertLessEqual(send_at, start - timedelta(minutes=md.LEAD_MINUTES),
                                 f"дайджест уйдёт позже чем за час до начала (старт 09:{minute:02d})")

    def test_nine_msk_not_shifted_by_rounding(self):
        # Округление вниз не должно двигать штатные 09:00 (они кратны сетке).
        self.assertEqual(md.digest_send_at(_msk(2026, 7, 14, 12)), _msk(2026, 7, 14, 9))

    # Н4 — провал записи состояния виден, а не выдаётся за успех.
    def test_state_write_failure_is_reported(self):
        # Дайджест ушёл, а sidecar не записался → следующий тик пошлёт ПОВТОР.
        # Отменить отправку нельзя, но авария обязана быть громкой (reason + ERROR),
        # иначе тик рапортует «успех», а в чат серии капают дубли.
        self.fx.write_digest("2026-07-13", ["Илья: висяк"])
        with mock.patch.object(md, "record_sent", return_value=None):
            with self.assertLogs("notary.morning_digest", level="ERROR"):
                res = self._send()
        self.assertEqual(res["status"], "sent")
        self.assertEqual(res["reason"], "state-write-failed")

    # У3 — отказ доставки владелец УЗНАЁТ (канон протокола: тихих отказов не бывает).
    def test_owner_alerted_when_digest_not_delivered(self):
        self.fx.write_digest("2026-07-13", ["Илья: висяк"])
        alerts: list = []
        with mock.patch.object(md, "_alert_owner", side_effect=lambda *a: alerts.append(a)):
            md.run(now=_msk(2026, 7, 14, 9, 1),
                   queue=[_queue_item(self.fx.series, _msk(2026, 7, 14, 12))],
                   root=self.tmp,
                   chat_resolver=lambda _s: None,          # серия ни к чему не привязана
                   sender=self._sender)
        self.assertEqual(self.sent, [])
        self.assertEqual([a[2] for a in alerts], ["no-chat"])

    def test_no_alert_on_normal_silence(self):
        # Нет висяков → дайджест штатно молчит. Это НЕ отказ — владельца не дёргаем.
        self.fx.write_digest("2026-07-13", [])
        alerts: list = []
        with mock.patch.object(md, "_alert_owner", side_effect=lambda *a: alerts.append(a)):
            md.run(now=_msk(2026, 7, 14, 9, 1),
                   queue=[_queue_item(self.fx.series, _msk(2026, 7, 14, 12))],
                   root=self.tmp, chat_resolver=lambda _s: -100500, sender=self._sender)
        self.assertEqual(alerts, [])


class TestDigestReplyNeverSilent(unittest.TestCase):
    """У1/У2: канал, который сам просит ответить, не имеет права молчать.

    Дайджеста нет в индексе доставленных протоколов → шлюз правок его не узнаёт и в
    ГРУППОВОМ чате (куда дайджест и приходит) молча дропает reply. Поэтому reply на
    дайджест обрабатывает свой роут и ВСЕГДА отвечает.
    """

    def _msg(self, **kw):
        m = {"message_id": 777, "reply_to_message": {"message_id": 4242}}
        m.update(kw)
        return m

    def _listener(self):
        import importlib
        return importlib.import_module("meetings_listener")

    def _psr(self):
        # listener тянет статус-модуль как `notary.lib.pending_status_reply` — патчить
        # надо ИМЕННО этот объект (`lib.pending_status_reply` в sys.modules — другой).
        import importlib
        return importlib.import_module("notary.lib.pending_status_reply")

    def test_voice_reply_gets_hint_not_silence(self):
        ml = self._listener()
        replies: list = []
        with mock.patch.object(ml, "_reply_context_series_digest", return_value="koordinatsiya"), \
             mock.patch.object(ml, "send_message", side_effect=lambda t, c, txt, **k: replies.append(txt)):
            claimed = ml.maybe_route_to_digest_reply("tok", -100500, self._msg(voice={"file_id": "x"}))
        self.assertTrue(claimed, "reply на дайджест обязан быть прожёван, а не уйти в шлюз правок")
        self.assertEqual(len(replies), 1)
        self.assertIn("текстом", replies[0])

    def test_unresolved_status_reply_gets_hint_not_silence(self):
        # Человек ЯВНО дал статус («сделано»), но бот не понял, о какой задаче речь.
        # Вот это молчанием оставлять нельзя: он уверен, что вопрос закрыт, а в учёте пусто.
        ml = self._listener()
        replies: list = []
        with mock.patch.object(ml, "_reply_context_series_digest", return_value="koordinatsiya"), \
             mock.patch.object(ml, "send_message", side_effect=lambda t, c, txt, **k: replies.append(txt)), \
             mock.patch.object(self._psr(), "apply_status_reply", return_value=None):
            claimed = ml.maybe_route_to_digest_reply("tok", -100500, self._msg(text="сделано"))
        self.assertTrue(claimed)
        self.assertEqual(len(replies), 1)
        self.assertIn("Не понял", replies[0])

    def test_small_talk_reply_is_silent_not_noisy(self):
        # А вот реакция без статус-маркера («ок», «спасибо») — не попытка учёта.
        # Бот в рабочем чате её не комментирует: иначе дайджест станет источником шума.
        ml = self._listener()
        replies: list = []
        with mock.patch.object(ml, "_reply_context_series_digest", return_value="koordinatsiya"), \
             mock.patch.object(ml, "send_message", side_effect=lambda t, c, txt, **k: replies.append(txt)):
            for chatter in ("ок", "спасибо!", "+"):
                claimed = ml.maybe_route_to_digest_reply("tok", -100500, self._msg(text=chatter))
                self.assertTrue(claimed, "reply на дайджест не должен уходить в шлюз правок")
        self.assertEqual(replies, [], "бот зашумил рабочий чат ответами на болтовню")

    def test_status_reply_still_acked(self):
        ml = self._listener()
        replies: list = []
        ack = {"label": "done", "status": "done", "matched": "Илья: отчёт", "ack": "✅ Принял: отчёт — сделано"}
        with mock.patch.object(ml, "_reply_context_series_digest", return_value="koordinatsiya"), \
             mock.patch.object(ml, "send_message", side_effect=lambda t, c, txt, **k: replies.append(txt)), \
             mock.patch.object(self._psr(), "apply_status_reply", return_value=ack):
            claimed = ml.maybe_route_to_digest_reply("tok", -100500, self._msg(text="отчёт сделал"))
        self.assertTrue(claimed)
        self.assertEqual(replies, ["✅ Принял: отчёт — сделано"])

    def test_not_a_digest_reply_passes_through(self):
        # Не reply на дайджест → роут не вмешивается (протоколы идут прежним путём).
        ml = self._listener()
        with mock.patch.object(ml, "_reply_context_series_digest", return_value=None):
            self.assertFalse(ml.maybe_route_to_digest_reply("tok", -100500, self._msg(text="сделано")))

    def test_digest_route_runs_before_feedback_gate(self):
        # Порядок в handle_message: дайджест-роут ДО шлюза правок, иначе тот дропнет.
        src = (_NOTARY / "meetings_listener.py").read_text(encoding="utf-8")
        i_digest = src.index("if maybe_route_to_digest_reply(")
        i_feedback = src.index("if maybe_route_to_feedback_reply(")
        self.assertLess(i_digest, i_feedback,
                        "роут дайджеста обязан стоять ДО шлюза правок")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
