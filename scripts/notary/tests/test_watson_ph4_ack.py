# -*- coding: utf-8 -*-
"""Тесты Ф4 (план `2026-07-20-watson-smart-replies-rethink.md`): бот не врёт в ответах —
честный ack + переспрос + канал несогласия.

Покрывает бинарные критерии приёмки Ф4:
  • R13 — ack раскладывается на «применил / не понял / не привязал»; «принял» без записи
    статуса не существует (вопросы ≠ применённое);
  • R14 — непривязанные перечисляются ПОИМЁННО (эхо сегмента), не голым числом;
  • R15 — закрытый нумерованный переспрос: ответ цифрой / «да, кроме 3» (И1) применяется;
  • R16 — дедуп по задаче в пачке: одна задача — одна группа, последний статус побеждает;
  • R26 — reply «не так / верни» на ack ступени-2 РЕАЛЬНО откатывает (reopen);
  • R24-триггер — подтверждённый класс слова на переспросе → remember_word_class;
  • RAZM6 — контекст ack переживает «рестарт» (перечитан из файла);
  • ОЖИД1 — «⏳ Получил, разбираю…» при пачке ДО долгого разбора.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python -m unittest tests.test_watson_ph4_ack -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import series_memory as sm  # noqa: E402
from notary.lib import decision_memory as dm  # noqa: E402
from notary.lib import reply_ack as ra  # noqa: E402
from notary.lib import reply_ack_state as ras  # noqa: E402
from notary.lib import reply_understand as ru  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402


_PROTO = """#протоколвстречи 03.06.2026

**Встреча:** Координация по маркетплейсам.

**Участники:** Илья Рыбалка, Татьяна Филиппова

**Транскрипт:** [2026-06-03.md](2026-06-03.md)

---

## Задачи

**Татьяна Филиппова**
- Подготовить медиаплан на июль
- Прислать расчёт по складу Ozon
"""
_ITEM_MEDIA = "Татьяна Филиппова: Подготовить медиаплан на июль"
_ITEM_RASCH = "Татьяна Филиппова: Прислать расчёт по складу Ozon"


def _applied(matched, label, *, tier=ru.TIER_CONFIDENT):
    from notary.lib import pending_status_reply as psr
    return {"kind": "applied", "tier": tier, "segment": None, "matched": matched,
            "label": label, "status": psr._STATUS_MAP[label], "source": "dict", "via": "overlap"}


def _question(kind, *, tier=3, segment=None, matched=None, label=None):
    return {"kind": kind, "tier": tier, "segment": segment, "matched": matched, "label": label}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-06-03", _PROTO,
                               {"series": "koord", "date": "2026-06-03"})
        self.memf = str(self.root / "decision-memory.jsonl")
        self.ackf = str(self.root / "reply-ack-state.json")
        self._env = mock.patch.dict(os.environ, {
            "ENABLE_REPLY_UNDERSTAND": "1",
            "MEETING_NOTARY_DECISION_MEMORY_FILE": self.memf,
            "MEETING_NOTARY_ACK_STATE_FILE": self.ackf,
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _status(self):
        return {k: v["status"] for k, v in sm.load_task_status(self.sd).items()}

    def _blank(self, **over):
        r = {"engaged": True, "applied": [], "questions": [], "edits": [],
             "config_requests": [], "unclear": "", "deferred": False,
             "n_status_raw": 0, "status_groups": []}
        r.update(over)
        return r


# ==========================================================================
# reply_ack_state — персистентность контекста ack (RAZM6)
# ==========================================================================
class TestAckState(_Base):
    def test_record_get_roundtrip(self):
        ok = ras.record(101, series_dir=self.sd, chat_id=7, replier="Татьяна",
                        applied=[{"text": _ITEM_RASCH, "label": "done"}], has_tier2=True)
        self.assertTrue(ok)
        rec = ras.get(101)
        self.assertEqual(rec["applied"][0]["text"], _ITEM_RASCH)
        self.assertTrue(rec["has_tier2"])

    def test_no_state_when_nothing_to_track(self):
        # Нет ни applied, ни question → нет смысла в state (нет канала ответа).
        self.assertFalse(ras.record(102, series_dir=self.sd, applied=[], question=None))
        self.assertIsNone(ras.get(102))

    def test_consume_removes(self):
        ras.record(103, series_dir=self.sd, applied=[{"text": _ITEM_RASCH, "label": "done"}])
        self.assertIsNotNone(ras.consume(103))
        self.assertIsNone(ras.get(103))              # второй ответ уже не применится дважды

    def test_survives_restart(self):
        # RAZM6: контекст читается из ФАЙЛА — «рестарт» listener его не теряет.
        ras.record(104, series_dir=self.sd, question={"mode": "pick_target", "label": "done",
                                                      "options": [_ITEM_RASCH], "word": None})
        self.assertTrue(Path(self.ackf).is_file())
        rec = ras.get(104, store_path=Path(self.ackf))   # свежее чтение из файла
        self.assertEqual(rec["question"]["mode"], "pick_target")

    def test_ttl_prune(self):
        # Устаревшую по дате СОЗДАНИЯ запись свежий record прунит.
        import json
        Path(self.ackf).write_text(json.dumps({"schema": 1, "entries": {
            "105": {"series_dir": str(self.sd), "created": "2000-01-01",
                    "applied": [{"text": _ITEM_RASCH, "label": "done"}], "has_tier2": False},
        }}, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(os.environ, {"REPLY_ACK_STATE_RETENTION_DAYS": "3"}):
            ras.record(106, series_dir=self.sd, applied=[{"text": _ITEM_MEDIA, "label": "done"}])
        self.assertIsNone(ras.get(105))              # протухшая выпала
        self.assertIsNotNone(ras.get(106))


# ==========================================================================
# build_ack — честный ack (R13/R14/R16) + схлопывание config
# ==========================================================================
class TestBuildAck(_Base):
    def test_r13_applied_named_no_fake_accept_for_question(self):
        # R13: применённое перечислено; вопрос НЕ выдаётся за «принял».
        res = self._blank(applied=[_applied(_ITEM_RASCH, "done")],
                          questions=[_question("no_match", segment="по неведомой задаче готово")])
        built = ra.build_ack(res, series_dir=self.sd)
        self.assertIn("Закрыто", built["text"])                 # применённое отражено
        self.assertIn("склад", built["text"].lower())           # именем
        self.assertIn("не смог привязать", built["text"].lower())  # вопрос — как непонятое
        self.assertNotIn("принял", built["text"].lower())       # не ложное «принял»
        self.assertEqual([a["text"] for a in built["applied"]], [_ITEM_RASCH])

    def test_r14_unattached_listed_by_name(self):
        # R14: непривязанные — поимённо (эхо сегмента), не голым числом «ещё N».
        res = self._blank(questions=[
            _question("no_match", segment="по рекламе всё"),
            _question("no_match", segment="по отгрузкам ок"),
        ])
        built = ra.build_ack(res, series_dir=self.sd)
        self.assertIn("по рекламе всё", built["text"])
        self.assertIn("по отгрузкам ок", built["text"])
        self.assertNotIn("Ещё 2", built["text"])                # не голый счётчик

    def test_r16_dedup_last_status_wins(self):
        # R16: одна задача в пачке дважды (done → reopen) — одна группа, побеждает reopen.
        res = self._blank(applied=[
            _applied(_ITEM_RASCH, "done"),
            _applied(_ITEM_RASCH, "reopen"),
        ])
        built = ra.build_ack(res, series_dir=self.sd)
        self.assertEqual(len(built["applied"]), 1)
        self.assertEqual(built["applied"][0]["label"], "reopen")
        self.assertNotIn("Закрыто", built["text"])              # старый (done) не показан
        self.assertIn("Вернул в работу", built["text"])

    def test_config_folded_single_message(self):
        # Схлопывание: настройка серии — строкой В ТОМ ЖЕ ack, не вторым сообщением.
        res = self._blank(config_requests=["не включай блок задач"])
        built = ra.build_ack(res, series_dir=self.sd)
        self.assertIn("формат протокола решает он", built["text"])

    def test_tier2_disagree_note_present(self):
        # R26 setup: ступень-2 → обещание «поправьте если не так / молчание = согласие».
        res = self._blank(applied=[_applied(_ITEM_RASCH, "done", tier=ru.TIER_MINOR)])
        built = ra.build_ack(res, series_dir=self.sd)
        self.assertTrue(built["has_tier2"])
        self.assertIn("не так", built["text"].lower())

    def test_block_close_no_pending_says_nothing_to_close(self):
        # У2 (цикл5): «закрой всё» при нуле висящих — честная строка, не молчание и не
        # вводящее «Не нашёл статусов»; block_close не теряется.
        empty = self.root / "empty-series"
        empty.mkdir()
        res = self._blank(questions=[_question("block_close")])
        built = ra.build_ack(res, series_dir=empty, replier="Татьяна")
        self.assertIn("закрывать нечего", built["text"])
        self.assertNotIn("Не нашёл", built["text"])
        self.assertIsNone(built["question"])


# ==========================================================================
# parse_answer — цифры / «да, кроме третьей» (И1) / «верни» / класс словом
# ==========================================================================
class TestParseAnswer(unittest.TestCase):
    def test_yes_except_ordinal_word(self):
        a = ra.parse_answer("да, кроме третьей")
        self.assertTrue(a["yes"])
        self.assertEqual(a["excluded"], {3})

    def test_digit_selection(self):
        a = ra.parse_answer("2")
        self.assertEqual(a["digits"], {2})

    def test_rollback_words(self):
        self.assertTrue(ra.parse_answer("не так")["rollback"])
        self.assertTrue(ra.parse_answer("верни")["rollback"])
        self.assertTrue(ra.parse_answer("отмени, пожалуйста")["rollback"])

    def test_class_word(self):
        self.assertEqual(ra.parse_answer("сделано")["cls"], "done")


# ==========================================================================
# handle_ack_reply — R15 / R26 / R24
# ==========================================================================
class TestHandleAckReply(_Base):
    def _state(self, **over):
        s = {"series_dir": str(self.sd), "date": "2026-06-10", "applied": [], "question": None}
        s.update(over)
        return s

    def test_r26_verni_reopens(self):
        # R26: применили done ступенью-2, человек ответил «верни» → РЕАЛЬНЫЙ reopen.
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_DONE, source="x", date="2026-06-10")
        st = self._state(applied=[{"text": _ITEM_RASCH, "label": "done"}])
        res = ra.handle_ack_reply(st, "не так, верни")
        self.assertTrue(res["consume"])
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_OPEN)
        self.assertIn("Вернул", res["text"])

    def test_r15_pick_target_digit_closes_right_task(self):
        # R15: «к какому вопросу?» 1) медиаплан 2) расчёт → ответ «2» закрывает расчёт.
        st = self._state(question={"mode": "pick_target", "label": "done",
                                   "options": [_ITEM_MEDIA, _ITEM_RASCH], "word": None})
        res = ra.handle_ack_reply(st, "2")
        self.assertTrue(res["consume"])
        self.assertEqual(self._status().get(sm._status_key(_ITEM_RASCH)), sm.STATUS_DONE)
        self.assertNotIn(sm._status_key(_ITEM_MEDIA), self._status())   # первую не тронул

    def test_r15_confirm_close_except_third_i1(self):
        # И1: «да, кроме третьей» закрывает 1 и 2, не трогает 3.
        t1, t2, t3 = "Иван: задача альфа", "Иван: задача бета", "Иван: задача гамма"
        st = self._state(question={"mode": "confirm_close", "label": "done",
                                   "options": [t1, t2, t3], "word": None})
        res = ra.handle_ack_reply(st, "да, кроме третьей")
        self.assertTrue(res["consume"])
        s = self._status()
        self.assertEqual(s.get(sm._status_key(t1)), sm.STATUS_DONE)
        self.assertEqual(s.get(sm._status_key(t2)), sm.STATUS_DONE)
        self.assertNotIn(sm._status_key(t3), s)

    def test_confirm_foreign_yes_no(self):
        st_no = self._state(question={"mode": "confirm_foreign", "label": "done",
                                      "matched": _ITEM_RASCH, "word": None})
        ra.handle_ack_reply(st_no, "нет")
        self.assertNotIn(sm._status_key(_ITEM_RASCH), self._status())   # «нет» — не трогаем
        st_yes = self._state(question={"mode": "confirm_foreign", "label": "done",
                                       "matched": _ITEM_RASCH, "word": None})
        ra.handle_ack_reply(st_yes, "да")
        self.assertEqual(self._status().get(sm._status_key(_ITEM_RASCH)), sm.STATUS_DONE)

    def test_r24_clarify_class_learns_word(self):
        # R24-триггер: класс неизвестен, человек назвал «сделано» → применяем + ЗАПОМИНАЕМ.
        st = self._state(question={"mode": "clarify_class", "label": None,
                                   "matched": _ITEM_RASCH, "word": "разрулили"})
        res = ra.handle_ack_reply(st, "сделано")
        self.assertTrue(res["learned"])
        self.assertEqual(self._status().get(sm._status_key(_ITEM_RASCH)), sm.STATUS_DONE)
        self.assertEqual(dm.recall_word_class("всё разрулили", store_path=Path(self.memf)), "done")

    def test_unparseable_keeps_context(self):
        # Невнятный ответ на переспрос → не применяем и НЕ теряем контекст (спросим снова).
        st = self._state(question={"mode": "pick_target", "label": "done",
                                   "options": [_ITEM_MEDIA, _ITEM_RASCH], "word": None})
        res = ra.handle_ack_reply(st, "ммм не знаю")
        self.assertFalse(res["consume"])
        self.assertEqual(self._status(), {})

    def test_rollback_word_answers_clarify_not_reopen(self):
        # Цикл5 Н2: слово отмены — омоним. При clarify_class + УЖЕ применённом «отменили»
        # = ответ на переспрос (снять спрошенную задачу), НЕ откат применённого. Иначе
        # вернули бы в работу применённое и не сняли спрошенное.
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_DONE, source="x", date="2026-06-10")
        st = self._state(
            applied=[{"text": _ITEM_RASCH, "label": "done"}],
            question={"mode": "clarify_class", "label": None,
                      "matched": _ITEM_MEDIA, "word": "разрулили"})
        res = ra.handle_ack_reply(st, "отменили")
        s = self._status()
        self.assertEqual(s.get(sm._status_key(_ITEM_RASCH)), sm.STATUS_DONE)       # не откатили
        self.assertEqual(s.get(sm._status_key(_ITEM_MEDIA)), sm.STATUS_CANCELLED)  # сняли спрошенное
        self.assertTrue(res["consume"])

    def test_reply_to_ack_new_update_guides_to_protocol(self):
        # У1 (цикл5): reply на ack не-«да»/не-откат (добивка нового статуса) → подсказка
        # про канал протокола, контекст НЕ гасим (вдруг всё же захочет «верни»).
        st = self._state(applied=[{"text": _ITEM_RASCH, "label": "done"}])
        res = ra.handle_ack_reply(st, "а медиаплан тоже готов")
        self.assertFalse(res["consume"])
        self.assertIn("протокол", res["text"].lower())


# ==========================================================================
# Проводка в listener — роут ответа на ack (R26/R15) + persist + ОЖИД1
# ==========================================================================
class TestAckReplyRoute(_Base):
    GROUP = 222
    OWNER = 111

    def _reply_msg(self, text, ack_mid):
        return {
            "chat": {"id": self.GROUP},
            "from": {"id": 777, "first_name": "Татьяна"},
            "reply_to_message": {"message_id": ack_mid, "text": "✅ Закрыто (1): …"},
            "message_id": 999, "date": 1718000000, "text": text,
        }

    def test_route_reply_to_ack_rolls_back(self):
        # R26 сквозь listener-роут: ack записан по message_id → «верни» откатывает.
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_DONE, source="x", date="2026-06-10")
        ras.record(888, series_dir=self.sd, chat_id=self.GROUP,
                   applied=[{"text": _ITEM_RASCH, "label": "done"}], has_tier2=True)
        sent = []
        with mock.patch.object(ml, "send_message", side_effect=lambda *a, **k: sent.append(a[2])):
            claimed = ml.maybe_route_to_ack_reply("tok", self.GROUP, self._reply_msg("верни", 888))
        self.assertTrue(claimed)
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_OPEN)
        self.assertIsNone(ras.get(888))                          # контекст погашен
        self.assertTrue(any("Вернул" in s for s in sent))

    def test_route_not_our_ack_returns_false(self):
        # Reply на бот-сообщение, которого НЕТ в ack-state → не наш → False (обычные роуты).
        with mock.patch.object(ml, "send_message"):
            claimed = ml.maybe_route_to_ack_reply("tok", self.GROUP, self._reply_msg("верни", 777))
        self.assertFalse(claimed)

    def test_route_cross_chat_collision_ignored(self):
        # Цикл5 Н1: message_id уникален ПО-ЧАТНО. Ack записан для ДРУГОГО чата (999), reply
        # с тем же mid пришёл в наш GROUP → chat_id не совпал → не наш ack (False), чужую
        # серию не трогаем и её контекст не гасим.
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_DONE, source="x", date="2026-06-10")
        ras.record(700, series_dir=self.sd, chat_id=999,
                   applied=[{"text": _ITEM_RASCH, "label": "done"}], has_tier2=True)
        with mock.patch.object(ml, "send_message"):
            claimed = ml.maybe_route_to_ack_reply("tok", self.GROUP, self._reply_msg("верни", 700))
        self.assertFalse(claimed)
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_DONE)  # не откатили
        self.assertIsNotNone(ras.get(700))                              # контекст не погашен

    def test_route_off_flag_returns_false(self):
        ras.record(890, series_dir=self.sd, applied=[{"text": _ITEM_RASCH, "label": "done"}])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_REPLY_UNDERSTAND", None)
            claimed = ml.maybe_route_to_ack_reply("tok", self.GROUP, self._reply_msg("верни", 890))
        self.assertFalse(claimed)

    def test_process_message_dispatches_ack_reply(self):
        # Достижимость из РЕАЛЬНОГО триггера process_message.
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_DONE, source="x", date="2026-06-10")
        ras.record(891, series_dir=self.sd, chat_id=self.GROUP,
                   applied=[{"text": _ITEM_RASCH, "label": "done"}], has_tier2=True)
        with mock.patch.object(ml, "send_message"):
            ml.process_message("tok", self.OWNER, self._reply_msg("не так", 891))
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_OPEN)

    def test_send_understand_ack_persists_state(self):
        # Цикл ack→state замкнут: единый сборщик записал контекст по message_id ответа бота.
        res = self._blank(applied=[_applied(_ITEM_RASCH, "done", tier=ru.TIER_MINOR)])
        msg = {"message_id": 999, "date": 1718000000}
        with mock.patch.object(ml, "send_message", return_value=1234):
            ml._send_understand_ack("tok", self.GROUP, msg, res, series_dir=self.sd,
                                    snapshot=None, replier="Татьяна", edit_as_hint=False)
        rec = ras.get(1234)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["applied"][0]["text"], _ITEM_RASCH)
        self.assertTrue(rec["has_tier2"])

    def test_ozhid1_progress_hint_on_batch(self):
        # ОЖИД1: пачка → «⏳ Получил, разбираю…» ДО долгого разбора (первым сообщением).
        batch = "по складу разрулили\nмедиаплан ещё в работе"      # 2 строки = пачка
        stub = lambda rt, it: {  # noqa: E731
            "statuses": [{"text": "по складу склад разрулили", "cls": "done"}],
            "edits": [], "series_config": [], "block_close": False, "unclear": "",
        }
        sent = []
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ru, "_request_understand", stub), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda *a, **k: sent.append(a[2]) or 1):
            msg = {"chat": {"id": self.GROUP}, "from": {"id": 777, "first_name": "Татьяна"},
                   "reply_to_message": {"message_id": 555, "text": "📅 протокол"},
                   "message_id": 999, "date": 1718000000, "text": batch}
            claimed = ml.maybe_route_to_reply_understand("tok", self.GROUP, self.OWNER, msg)
        self.assertTrue(claimed)
        self.assertEqual(sent[0], ml.UNDERSTAND_PROGRESS_HINT)     # реакция ПЕРВОЙ


if __name__ == "__main__":
    unittest.main()
