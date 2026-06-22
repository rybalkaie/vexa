# -*- coding: utf-8 -*-
"""Ф4 (ISS-19, R10/R11/R12/R16): контроль владельца — сводное inline-подтверждение
выученного, реальный scope, дайджест новых типов, откат (incl company-стор).

План 2026-06-22 durable-learning-semantic-edits, Фаза 4. Ф1–Ф3 построили классификатор,
хранилище новых типов со scope и защиту от отравления. Ф4 наращивает UX-контроль ПОВЕРХ:

R16 — бот СРАЗУ подтверждает выученное ОДНИМ сводным сообщением на перевыпуск (не N
      подряд); у каждой строки ФАКТИЧЕСКИЙ уровень из записи (РАЗМ1: эта серия / вся
      компания / везде), не хардкод.
R10 — distinction/meaning озвучиваются простым языком (формат Owner-preview).
R11 — откат «забудь/откати» работает для новых типов и ОБХОДИТ company-стор (РИСК1).
R12 — режим = авто-применение + откат (отдельного гейта подтверждения НЕТ).
+ Дедуп: одно сообщение, не дублируется между подтверждением и дайджестом и между
  раундами одной встречи (mark_announced); one-off в озвучку не попадает.
+ Достижимость из реального триггера: проводка в `reissue_one` (гейт ISS-19) + роут
  reply-отката на подтверждение в листенере.

Запуск: python3 -m unittest tests.test_iss19_phase4_owner_control (system python3.9, без venv).
"""
from __future__ import annotations

import json
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

from notary.lib import feedback_learning as fl  # noqa: E402
from notary.lib import feedback_classify_llm as fcl  # noqa: E402
from notary.lib import feedback_state  # noqa: E402
from notary.lib import feedback_reissue  # noqa: E402

# Группа = ≥2 человек → scope_candidate=company; 1:1 = 1 человек → series.
GROUP = ["Илья Рыбалка", "Пётр Сидоров", "Мария"]
ONE_TO_ONE = ["Илья Рыбалка"]
COMPANY = "mpervyi"

DIST = {"durable": True, "type": "distinction",
        "subjects": ["Dream Story", "23МПКТК"], "rule": "разные разделы", "confidence": 0.9}
MEAN = {"durable": True, "type": "meaning", "subjects": ["вывод"],
        "rule": "писать с ИП, не СП", "confidence": 0.85}
ONE_OFF = {"durable": False, "type": "one-off", "subjects": [], "rule": "", "confidence": 0.0}


# ===========================================================================
# Рендер правила простым языком — ТОЧНО формат Owner-preview (строки 122–142)
# ===========================================================================
class TestAckRendererExactOwnerPreview(unittest.TestCase):
    def test_meaning_phrase_matches_owner_preview(self):
        r = {"kind": "meaning", "subject": "вывод", "meaning": "пиши «с ИП», не «СП»",
             "scope": "company", "company": COMPANY}
        self.assertEqual(fl._ack_rule_phrase(r), "«вывод» — пиши «с ИП», не «СП»")

    def test_distinction_phrase_matches_owner_preview(self):
        # Дефолтная нота схлопывается до Owner-preview «не объединять».
        r = {"kind": "distinction", "subject_a": "23МПКТК", "subject_b": "Dream Story",
             "note": fl._DISTINCTION_DEFAULT_NOTE, "scope": "company"}
        self.assertEqual(fl._ack_rule_phrase(r), "«23МПКТК» ≠ «Dream Story», не объединять")

    def test_distinction_keeps_informative_note(self):
        r = {"kind": "distinction", "subject_a": "A", "subject_b": "B", "note": "разные разделы"}
        self.assertEqual(fl._ack_rule_phrase(r), "«A» ≠ «B», разные разделы")

    def test_term_and_guidance_phrase(self):
        self.assertEqual(fl._ack_rule_phrase({"kind": "term", "wrong": "РСЯ", "right": "РЕЦ"}),
                         "«РСЯ» → пиши «РЕЦ»")
        self.assertEqual(fl._ack_rule_phrase({"kind": "guidance", "subject": "суммы",
                                              "rule": "в тысячах рублей"}),
                         "«суммы»: в тысячах рублей")
        self.assertEqual(fl._ack_rule_phrase({"kind": "guidance", "subject": None,
                                              "rule": "короче формулируй"}),
                         "короче формулируй")

    def test_human_scope_from_record_not_hardcoded(self):
        self.assertEqual(fl._human_scope({"scope": "global"}), "везде")
        self.assertEqual(fl._human_scope({"scope": "company"}), "для всей компании")
        self.assertEqual(fl._human_scope({"scope": "series"}), "только эта серия")


# ===========================================================================
# Песочница + боевая точка записи (classify_remainder_edits)
# ===========================================================================
class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "_feedback_edits"
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
            "ENABLE_FEEDBACK_LEARNING": "1",
            "ENABLE_FEEDBACK_LLM_CLASSIFY": "1",
        })
        self._env.start()
        self._cfs = mock.patch("notary.lib.context_knowledge.company_for_series",
                               return_value=COMPANY)
        self._cfs.start()

    def tearDown(self):
        self._cfs.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _fid(self, series="seriesA", date="2026-06-22"):
        return feedback_state.build_feedback_id(series, date, -1)

    def _classify(self, series, text, participants, result, *, fid=None, author="Илья Рыбалка"):
        """Прогон правки через боевую точку врезки — пишет durable-правило со scope
        по типу встречи и source_feedback_id из state (как реальный перевыпуск)."""
        cf = lambda t, p: {**result, "scope_candidate": fcl.scope_candidate(p)}
        state = {"series": series, "date": "2026-06-22", "feedback_id": fid or self._fid(series)}
        return fcl.classify_remainder_edits(
            state, [{"author": author, "text": text}],
            meta={"expectedParticipants": participants}, classify_fn=cf, root=self.root)


# ===========================================================================
# R16 / РАЗМ1 — ОДНО сводное подтверждение с ФАКТИЧЕСКИМ scope
# ===========================================================================
class TestReissueAckSummary(_Base):
    def test_one_summary_real_scope_one_off_excluded(self):
        fid = self._fid()
        # Пакет правок одной групповой встречи: различение + смысл (→ company), one-off.
        self._classify("seriesA", "раздел не Dream Story, а 23МПКТК, это разные вещи", GROUP, DIST, fid=fid)
        self._classify("seriesA", "вывод обязательно с ИП", GROUP, MEAN, fid=fid)
        self._classify("seriesA", "убери этот абзац", GROUP, ONE_OFF, fid=fid)

        text, ids = fl.format_reissue_ack(fid, root=self.root)
        # ОДНО сообщение — со списком, а не N подряд (ОЖИД1).
        self.assertTrue(text.startswith(fl.REISSUE_ACK_PREFIX))
        self.assertEqual(len(ids), 2)
        bullets = [ln for ln in text.splitlines() if ln.strip().startswith("•")]
        self.assertEqual(len(bullets), 2, "ровно 2 durable-строки в ОДНОМ сообщении")
        # РАЗМ1: фактический уровень из записи (групповая → вся компания), не хардкод.
        self.assertIn("для всей компании", text)
        self.assertNotIn("только эта серия", text)
        # one-off в озвучку не попал.
        self.assertNotIn("абзац", text)
        # Формат Owner-preview: различение + смысл простым языком.
        self.assertIn("«23МПКТК»", text)
        self.assertIn("Dream Story", text)
        self.assertIn("≠", text)
        self.assertIn("«вывод» — ", text)

    def test_series_scope_label_for_one_to_one(self):
        fid = self._fid()
        self._classify("seriesA", "вывод обязательно с ИП", ONE_TO_ONE, MEAN, fid=fid)
        text, ids = fl.format_reissue_ack(fid, root=self.root)
        self.assertEqual(len(ids), 1)
        # 1:1 → series-scope → «только эта серия» (приватность личного не протекает).
        self.assertIn("только эта серия", text)
        self.assertNotIn("для всей компании", text)

    def test_no_durable_rules_no_message(self):
        fid = self._fid()
        self._classify("seriesA", "убери абзац", GROUP, ONE_OFF, fid=fid)
        # Перевыпуск без durable-правок не пишет вовсе (ОЖИД1).
        self.assertEqual(fl.format_reissue_ack(fid, root=self.root), ("", []))

    def test_empty_feedback_id_no_message(self):
        self.assertEqual(fl.format_reissue_ack(None, root=self.root), ("", []))
        self.assertEqual(fl.format_reissue_ack("", root=self.root), ("", []))

    def test_only_this_meetings_rules_in_ack(self):
        # Дедуп между встречами: подтверждение раунда не зачерпывает чужую встречу
        # из общей очереди не-озвученных.
        fidA, fidB = self._fid("seriesA"), self._fid("seriesB")
        self._classify("seriesA", "вывод обязательно с ИП", ONE_TO_ONE, MEAN, fid=fidA)
        self._classify("seriesB", "сроки это про дедлайны",
                       ONE_TO_ONE, {**MEAN, "subjects": ["сроки"], "rule": "про дедлайны"}, fid=fidB)
        textA, idsA = fl.format_reissue_ack(fidA, root=self.root)
        self.assertEqual(len(idsA), 1)
        self.assertIn("вывод", textA)
        self.assertNotIn("сроки", textA)


# ===========================================================================
# R16-дедуп — между подтверждением, дайджестом и раундами
# ===========================================================================
class TestAckDedup(_Base):
    def test_announced_blocks_digest_and_next_round(self):
        fid = self._fid()
        self._classify("seriesA", "вывод обязательно с ИП", GROUP, MEAN, fid=fid)
        text, ids = fl.format_reissue_ack(fid, root=self.root)
        self.assertEqual(len(ids), 1)
        # Подтверждение озвучено → помечаем (как делает reissue_one на success-отправке).
        fl.mark_announced(ids, root=self.root)
        # Повтор перевыпуска той же встречи — не озвучивает повторно (между раундами).
        self.assertEqual(fl.format_reissue_ack(fid, root=self.root), ("", []))
        # Вечерний дайджест не дублирует уже подтверждённое (между ним и подтверждением).
        self.assertEqual(fl.format_digest_block(root=self.root), ("", []))


# ===========================================================================
# R11/R12/РИСК1 — откат новых типов, обходя company-стор; пишет rollback
# ===========================================================================
class TestRollbackNewTypes(_Base):
    def _company_rollback_events(self):
        ops = []
        for f in fl._iter_company_files(root=self.root):
            ops += [e.get("op") for e in fl._read_events_from_file(f)]
        return ops

    def test_rollback_company_distinction_removed_from_prompt(self):
        # Групповая → company-стор. Откат ОБЯЗАН обойти company-стор (РИСК1), не только серию.
        self._classify("seriesA", "раздел не Dream Story, а 23МПКТК", GROUP, DIST)
        self.assertEqual(len(fl.active_company_distinction_rules(COMPANY, root=self.root)), 1)
        # До отката — различение видно в промпте серии этой компании.
        self.assertIn("23МПКТК", fl.format_learned_terms_block("seriesA", root=self.root))
        # R12: применение было авто; откат — по сущности.
        rolled = fl.apply_rollback_reply("откати 23МПКТК", root=self.root)
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0]["kind"], "distinction")
        # rollback-событие дописано в company-журнал → правило снято и из стора, и из промпта.
        self.assertIn("rollback", self._company_rollback_events())
        self.assertEqual(fl.active_company_distinction_rules(COMPANY, root=self.root), [])
        self.assertNotIn("23МПКТК", fl.format_learned_terms_block("seriesA", root=self.root))

    def test_rollback_company_meaning_by_forget(self):
        self._classify("seriesA", "вывод обязательно с ИП", GROUP, MEAN)
        self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)
        rolled = fl.apply_rollback_reply("забудь про вывод", root=self.root)
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0]["kind"], "meaning")
        self.assertIn("rollback", self._company_rollback_events())
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])


# ===========================================================================
# Достижимость из реального триггера — reissue_one шлёт ОДНО подтверждение
# ===========================================================================
PROTO_OLD = "# Протокол\n\n- старый текст\n"
PROTO_NEW = "# Протокол\n\n- РЕЦ вместо РСЯ\n"


class TestReissueOneInlineAck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "_feedback_edits"
        self.protocols = self.base / "protocols"
        self.protocols.mkdir(parents=True, exist_ok=True)
        self._envvars = {
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.protocols),
            "TELEGRAM_NOTARIUS_BOT_TOKEN": "test-token",
            "ENABLE_FEEDBACK_LEARNING": "1",
        }

    def tearDown(self):
        self._tmp.cleanup()

    def _meeting_and_state(self, *, edit_text):
        series, date, chat_id = "coord", "2026-06-02", -1001
        d = self.protocols / series
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{date}.md").write_text("00:00 Михаил: текст\n", encoding="utf-8")
        (d / f"{date}-protokol.md").write_text(PROTO_OLD, encoding="utf-8")
        meta = {"series": series, "date": date, "expectedParticipants": ["Михаил Саргин"],
                "delivered": [{"chat_id": chat_id, "message_ids": [101, 102],
                               "at": "2026-06-02T11:00:00Z"}]}
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        fid = feedback_state.build_feedback_id(series, date, chat_id)
        st = {"feedback_id": fid, "series": series, "date": date, "chat_id": chat_id,
              "meta_path": str(d / "meta.json"), "protocol_message_ids": [101, 102],
              "round": 1, "status": "reissuing", "reissue_attempts": 0,
              "edits": [{"author": "Михаил Саргин", "text": edit_text, "tg_message_id": 555}]}
        return st

    def _run(self, *, edit_text, classify_flag):
        env = dict(self._envvars)
        if classify_flag:
            env["ENABLE_FEEDBACK_LLM_CLASSIFY"] = "1"
        else:
            env["ENABLE_FEEDBACK_LLM_CLASSIFY"] = "0"
        acks = []
        with mock.patch.dict(os.environ, env):
            st = self._meeting_and_state(edit_text=edit_text)
            res = feedback_reissue.reissue_one(
                st, root=self.root,
                generate_fn=lambda tp, mm, sid: PROTO_NEW,
                redeliver_fn=lambda *a, **k: {"status": "sent", "message_ids": [1], "chat_id": -1001},
                save_version_fn=lambda p: p,
                ack_send_fn=lambda text: (acks.append(text), True)[1],
            )
        return res, acks

    def test_term_edit_emits_one_inline_ack_and_marks_announced(self):
        res, acks = self._run(edit_text="не РСЯ, а РЕЦ", classify_flag=True)
        self.assertEqual(res["status"], "sent")
        # ОДНО сводное подтверждение ушло (R16).
        self.assertEqual(len(acks), 1)
        self.assertTrue(acks[0].startswith(fl.REISSUE_ACK_PREFIX))
        self.assertIn("«РСЯ» → пиши «РЕЦ»", acks[0])
        self.assertIn("только эта серия", acks[0])  # term-замена серии
        # Помечено озвученным → вечерний дайджест не дублирует.
        with mock.patch.dict(os.environ, self._envvars):
            self.assertEqual(fl.format_digest_block(root=self.root), ("", []))

    def test_no_durable_learning_no_ack(self):
        # Правка без выучиваемого правила → подтверждения нет (ОЖИД1).
        res, acks = self._run(edit_text="131 на доставке", classify_flag=True)
        self.assertEqual(res["status"], "sent")
        self.assertEqual(acks, [])

    def test_gate_off_no_ack_even_if_learned(self):
        # Мастер-флаг ISS-19 OFF → перевыпуск учит как раньше, но inline-подтверждения нет
        # (поведение перевыпуска без фичи неизменно; реальный tg-send не дёргается).
        res, acks = self._run(edit_text="не РСЯ, а РЕЦ", classify_flag=False)
        self.assertEqual(res["status"], "sent")
        self.assertEqual(acks, [])


# ===========================================================================
# Роут reply-отката на подтверждение перевыпуска (как на дайджест)
# ===========================================================================
class TestListenerRoutesAckReply(unittest.TestCase):
    def setUp(self):
        from notary import meetings_listener as ml  # noqa: PLC0415
        self.ml = ml

    def _msg(self, orig_text, reply_text="откати вывод"):
        return {"chat": {"id": 359008340}, "text": reply_text, "message_id": 42,
                "date": 1780000000, "reply_to_message": {"text": orig_text}}

    def test_reply_to_ack_routed_to_rollback(self):
        ml = self.ml
        spy = mock.MagicMock(return_value=True)
        ack_text = f"{fl.REISSUE_ACK_PREFIX} (применяю сразу — подтверди или откати):\n  • «вывод» — пиши с ИП (для всей компании)"
        with mock.patch.object(ml, "maybe_route_to_feedback_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_protocol_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_correction_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_learning_rollback", spy):
            ml.process_message("test-token", 359008340, self._msg(ack_text))
        spy.assert_called_once()

    def test_ack_prefix_matches_source(self):
        # Анти-дрейф: префикс роута == константа подтверждения.
        self.assertEqual(self.ml._reissue_ack_prefix(), fl.REISSUE_ACK_PREFIX)


if __name__ == "__main__":
    unittest.main()
