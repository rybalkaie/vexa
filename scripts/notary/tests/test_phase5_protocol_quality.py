"""Тесты Ф5 — качество протокола (контекст / числа / «протокол вперёд»).

Покрывает REQ 5.1–5.6 плана `2026-06-03-dorabotki-bot-notarius-full.md`:
  - 5.1  промт сохраняет субстантивный контекст (баланс с краткостью).
  - 5.2  ревью-проход: парс ответа, ⚠️-пометки, идемпотентность, mock-claude.
  - 5.4  auto_resolve_known_speakers — строгий 1:1, защита от коллизий.
  - 5.5/5.6  redeliver_revised_protocol — revision-маркер, обход идемпотентности,
            content-hash, и сохранность идемпотентности Ф1 (deliver_protocol).
  - grace-окно finalize (5.3): _delivery_grace_sec / _wait_for_clarify_grace.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase5_protocol_quality -v
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
sys.path.insert(0, str(_NOTARY))

from lib import llm_postprocess as lp  # noqa: E402
from lib import clarify_worker as cw  # noqa: E402
from lib import delivery_grace as dg  # noqa: E402


# --------------------------------------------------------------------------
# 5.1 — промт сохраняет субстантивный контекст, сохраняя краткость
# --------------------------------------------------------------------------
class TestPromptContextBalance(unittest.TestCase):

    def test_base_prompt_mentions_substantive_context(self):
        p = lp.GENERATE_PROTOCOL_BASE_PROMPT
        self.assertIn("СУБСТАНТИВНЫЙ контекст", p)
        # Должны быть примеры «основание/канал/состояние».
        self.assertIn("канал", p)
        self.assertIn("основание", p)

    def test_base_prompt_keeps_brevity_rule(self):
        """5.1 — это БАЛАНС, а не отмена сжатия: правило длины и «режь воду» на месте."""
        p = lp.GENERATE_PROTOCOL_BASE_PROMPT
        self.assertIn("компактнее транскрипта в 5–10 раз", p)
        self.assertIn("НЕ отмена сжатия", p)


# --------------------------------------------------------------------------
# 5.2 — ревью-проход: парсинг ответа claude
# --------------------------------------------------------------------------
class TestParseReviewResponse(unittest.TestCase):

    def test_clean_json(self):
        raw = json.dumps({"findings": [
            {"section": "values", "quote": "28 млн", "note": "в речи 2,8 млн"},
        ]})
        out = lp._parse_review_response(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["quote"], "28 млн")

    def test_markdown_fenced_json(self):
        raw = "```json\n" + json.dumps({"findings": [
            {"quote": "склад свободен", "note": "в речи занят"},
        ]}) + "\n```"
        out = lp._parse_review_response(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["section"], "values")  # дефолт секции

    def test_prose_around_json(self):
        raw = 'Вот результат: {"findings": [{"quote": "a", "note": "b"}]} — всё.'
        out = lp._parse_review_response(raw)
        self.assertEqual(len(out), 1)

    def test_empty_findings(self):
        self.assertEqual(lp._parse_review_response('{"findings": []}'), [])

    def test_garbage(self):
        self.assertEqual(lp._parse_review_response("не json вообще"), [])
        self.assertEqual(lp._parse_review_response(""), [])

    def test_drops_items_without_quote_or_note(self):
        raw = json.dumps({"findings": [
            {"quote": "", "note": "x"},
            {"quote": "y", "note": ""},
            {"quote": "z", "note": "ok"},
        ]})
        out = lp._parse_review_response(raw)
        self.assertEqual([f["quote"] for f in out], ["z"])


PROTOCOL_FOR_FLAGS = """#протоколвстречи 02.06.2026

**Встреча:** Тест.

---

## 1) Финансы

▪️ Оборот вырос до **28 млн** за месяц.

▪️ Склад сейчас **свободен**.

---

## 2) Прочее

▪️ Обычный пункт без проблем.
"""


class TestApplyReviewFlags(unittest.TestCase):

    def test_inline_flag_on_matching_line(self):
        findings = [{"section": "values", "quote": "28 млн", "note": "в речи 2,8 млн"}]
        out = lp.apply_review_flags(PROTOCOL_FOR_FLAGS, findings)
        # Пометка стоит на строке с числом.
        line = [l for l in out.split("\n") if "28 млн" in l][0]
        self.assertIn("⚠️ проверь: в речи 2,8 млн", line)
        # Другие строки не тронуты.
        self.assertIn("Обычный пункт без проблем.", out)
        self.assertNotIn("Обычный пункт без проблем. ⚠️", out)

    def test_normalization_whitespace_case(self):
        """Матч устойчив к регистру/пробелам в quote."""
        findings = [{"quote": "СКЛАД   сейчас свободен", "note": "в речи занят"}]
        out = lp.apply_review_flags(PROTOCOL_FOR_FLAGS, findings)
        line = [l for l in out.split("\n") if "свободен" in l][0]
        self.assertIn("⚠️", line)

    def test_unmatched_goes_to_block(self):
        findings = [{"quote": "этого нет в протоколе", "note": "сверь вручную"}]
        out = lp.apply_review_flags(PROTOCOL_FOR_FLAGS, findings)
        self.assertIn("## ⚠️ Проверить", out)
        self.assertIn("сверь вручную", out)

    def test_idempotent_no_double_flag(self):
        findings = [{"quote": "28 млн", "note": "в речи 2,8 млн"}]
        once = lp.apply_review_flags(PROTOCOL_FOR_FLAGS, findings)
        twice = lp.apply_review_flags(once, findings)
        self.assertEqual(once.count("⚠️ проверь: в речи 2,8 млн"), 1)
        self.assertEqual(twice.count("⚠️ проверь: в речи 2,8 млн"), 1)

    def test_no_findings_returns_unchanged(self):
        self.assertEqual(lp.apply_review_flags(PROTOCOL_FOR_FLAGS, []), PROTOCOL_FOR_FLAGS)


class TestBuildReviewPrompt(unittest.TestCase):

    def test_values_section_present(self):
        sp = lp._build_review_system_prompt(("values",))
        self.assertIn("values", sp)
        self.assertIn("число", sp)
        self.assertIn("JSON", sp)

    def test_unknown_check_no_crash(self):
        # Ф6/Ф7 ещё не подключены — неизвестный check не ломает сборку.
        sp = lp._build_review_system_prompt(("roles",))
        self.assertIn("JSON", sp)


class TestReviewProtocolMockClaude(unittest.TestCase):

    def test_review_protocol_calls_claude_and_parses(self):
        fake = json.dumps({"findings": [{"quote": "28 млн", "note": "сверь"}]})
        with mock.patch.object(lp, "call_claude_print", return_value=fake) as m:
            out = lp.review_protocol(PROTOCOL_FOR_FLAGS, "транскрипт текст", meeting_sid="x")
        self.assertEqual(len(out), 1)
        self.assertTrue(m.called)

    def test_review_protocol_no_claude_returns_empty(self):
        with mock.patch.object(
            lp, "call_claude_print", side_effect=lp.ClaudeCliNotInstalled("no claude")
        ):
            out = lp.review_protocol(PROTOCOL_FOR_FLAGS, "т", meeting_sid="x")
        self.assertEqual(out, [])

    def test_review_protocol_empty_inputs(self):
        self.assertEqual(lp.review_protocol("", "t"), [])
        self.assertEqual(lp.review_protocol("p", ""), [])

    def test_review_disabled_by_killswitch(self):
        os.environ["ENABLE_PROTOCOL_REVIEW"] = "0"
        try:
            with mock.patch.object(lp, "call_claude_print") as m:
                out = lp.review_protocol(PROTOCOL_FOR_FLAGS, "транскрипт", meeting_sid="x")
            self.assertEqual(out, [])
            m.assert_not_called()
        finally:
            os.environ.pop("ENABLE_PROTOCOL_REVIEW", None)


# --------------------------------------------------------------------------
# 5.4 — авто-подстановка известных спикеров (строгий 1:1)
# --------------------------------------------------------------------------
class TestAutoResolveKnownSpeakers(unittest.TestCase):

    def _unclear(self, *keys):
        return {k: {"speaker_label_in_md": f"Спикер {i+1}"} for i, k in enumerate(keys)}

    def test_singleton_one_candidate_known_in_expected(self):
        """1 кластер + 1 известный (из expected) → авто-подстановка."""
        m = lp.auto_resolve_known_speakers(
            self._unclear("SPEAKER_01"),
            resolved_names=["Илья Рыбалка"],
            name_pool=["Илья Рыбалка", "Михаил Саргин"],
            people_names=[],
            expected_participants=["Илья Рыбалка", "Михаил Саргин"],
        )
        self.assertEqual(m, {"SPEAKER_01": "Михаил Саргин"})

    def test_singleton_known_in_people_md(self):
        """Кандидат не в expected, но есть в people.md → известный (критерий 5.4)."""
        m = lp.auto_resolve_known_speakers(
            self._unclear("SPEAKER_01"),
            resolved_names=["Илья Рыбалка"],
            name_pool=["Илья Рыбалка", "Михаил Саргин"],
            people_names=["Илья Рыбалка", "Михаил Саргин", "Дарья Набережная"],
            expected_participants=["Илья Рыбалка"],  # Саргина в expected нет
        )
        self.assertEqual(m, {"SPEAKER_01": "Михаил Саргин"})

    def test_two_clusters_no_autoresolve(self):
        """2 неразмеченных кластера → неоднозначно → не угадываем."""
        m = lp.auto_resolve_known_speakers(
            self._unclear("SPEAKER_01", "SPEAKER_02"),
            resolved_names=[],
            name_pool=["Михаил Саргин", "Дарья Набережная"],
            people_names=["Михаил Саргин", "Дарья Набережная"],
            expected_participants=["Михаил Саргин", "Дарья Набережная"],
        )
        self.assertEqual(m, {})

    def test_two_candidates_no_autoresolve(self):
        """1 кластер, но 2 не-занятых известных кандидата → не угадываем."""
        m = lp.auto_resolve_known_speakers(
            self._unclear("SPEAKER_01"),
            resolved_names=[],
            name_pool=["Михаил Саргин", "Дарья Набережная"],
            people_names=["Михаил Саргин", "Дарья Набережная"],
            expected_participants=["Михаил Саргин", "Дарья Набережная"],
        )
        self.assertEqual(m, {})

    def test_collision_two_mikhail_no_guess(self):
        """«2 Михаила» в people.md: короткое «Михаил» не резолвится → не известный."""
        m = lp.auto_resolve_known_speakers(
            self._unclear("SPEAKER_01"),
            resolved_names=["Илья Рыбалка"],
            name_pool=["Илья Рыбалка", "Михаил"],
            people_names=["Илья Рыбалка", "Михаил Еремеев", "Михаил Саргин"],
            expected_participants=["Илья Рыбалка"],
        )
        self.assertEqual(m, {})

    def test_unknown_candidate_no_autoresolve(self):
        """Единственный кандидат не известен (нет ни в expected, ни в people) → ask."""
        m = lp.auto_resolve_known_speakers(
            self._unclear("SPEAKER_01"),
            resolved_names=["Илья Рыбалка"],
            name_pool=["Илья Рыбалка", "Случайный Гость"],
            people_names=["Илья Рыбалка"],
            expected_participants=["Илья Рыбалка"],
        )
        self.assertEqual(m, {})

    def test_occupied_candidate_excluded(self):
        """Известный, но уже привязанный к другому кластеру — не кандидат."""
        m = lp.auto_resolve_known_speakers(
            self._unclear("SPEAKER_01"),
            resolved_names=["Михаил Саргин"],  # уже занят
            name_pool=["Михаил Саргин"],
            people_names=["Михаил Саргин"],
            expected_participants=["Михаил Саргин"],
        )
        self.assertEqual(m, {})

    def test_is_known_person(self):
        self.assertTrue(lp._is_known_person("Михаил Саргин", ["Михаил Саргин"], set()))
        self.assertTrue(lp._is_known_person("Икс", [], {"Икс"}))
        # Короткое имя при уникальном people.md-резолве → известный.
        self.assertTrue(lp._is_known_person("Дарья", ["Дарья Набережная"], set()))
        self.assertFalse(lp._is_known_person("Незнакомец", ["Дарья Набережная"], set()))
        self.assertFalse(lp._is_known_person("", [], set()))


# --------------------------------------------------------------------------
# 5.5 / 5.6 — revision до-сыл, обход идемпотентности, content-hash
# --------------------------------------------------------------------------
SAMPLE_OLD = """#протоколвстречи 02.06.2026

**Встреча:** Тест.

**Участники:** Илья Рыбалка

**Транскрипт:** [2026-06-02.md](2026-06-02.md)

---

## 1) Тема

▪️ Сказал **Спикер 3**.
"""

SAMPLE_NEW = SAMPLE_OLD.replace("Спикер 3", "Дарья Набережная")

REDELIVER_META = {"series": "test-series", "date": "2026-06-02", "sessionUid": "sid"}


class TestRedeliverRevisedProtocol(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test-revision-")
        self.meta_path = Path(self.tmpdir) / "meta.json"
        self._old_token = os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN")
        os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = "fake-token"
        self._old_enable = os.environ.get("ENABLE_PROTOCOL_DELIVERY")
        os.environ.pop("ENABLE_PROTOCOL_DELIVERY", None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._old_token is None:
            os.environ.pop("TELEGRAM_NOTARIUS_BOT_TOKEN", None)
        else:
            os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = self._old_token
        if self._old_enable is not None:
            os.environ["ENABLE_PROTOCOL_DELIVERY"] = self._old_enable

    def _write_delivered(self, record):
        self.meta_path.write_text(
            json.dumps({"delivered": [record]}, ensure_ascii=False), encoding="utf-8"
        )

    def test_not_delivered_yet_when_no_prior_delivery(self):
        """Нет доставки (пустой meta) → not-delivered-yet, ничего не шлём."""
        self.meta_path.write_text(json.dumps({}), encoding="utf-8")
        with mock.patch.object(lp.telegram_api, "send_message") as m:
            res = lp.redeliver_revised_protocol(
                REDELIVER_META, SAMPLE_OLD, SAMPLE_NEW,
                meta_json_path=self.meta_path, meeting_sid="sid",
            )
        self.assertEqual(res["status"], "not-delivered-yet")
        m.assert_not_called()

    def test_no_change_when_identical(self):
        self._write_delivered({"chat_id": 999, "message_ids": [1], "at": "old"})
        with mock.patch.object(lp.telegram_api, "send_message") as m:
            res = lp.redeliver_revised_protocol(
                REDELIVER_META, SAMPLE_OLD, SAMPLE_OLD,
                meta_json_path=self.meta_path, meeting_sid="sid",
            )
        self.assertEqual(res["status"], "no-change")
        m.assert_not_called()

    def test_sent_bypasses_idempotency(self):
        """Доставлено + контент изменился → шлём ревизию (обход идемпотентности)."""
        self._write_delivered({"chat_id": 999, "message_ids": [1], "at": "old"})
        with mock.patch.object(lp.telegram_api, "send_message", return_value={"message_id": 42}) as m, \
                mock.patch.object(lp, "_compose_revision_summary", return_value="🔁 changed"):
            res = lp.redeliver_revised_protocol(
                REDELIVER_META, SAMPLE_OLD, SAMPLE_NEW,
                meta_json_path=self.meta_path, meeting_sid="sid",
            )
        self.assertEqual(res["status"], "sent")
        self.assertEqual(res["revision"], 1)
        self.assertTrue(m.called)
        # meta.delivered обновлён ревизией + content_hash.
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        last = meta["delivered"][-1]
        self.assertEqual(last["decision"], "revision")
        self.assertEqual(last["revision"], 1)
        self.assertEqual(last["content_hash"], lp._protocol_content_hash(SAMPLE_NEW))

    def test_summary_sent_first(self):
        """5.5 — блок «🔁 Что изменилось» уходит ПЕРВЫМ сообщением."""
        self._write_delivered({"chat_id": 999, "message_ids": [1], "at": "old"})
        sent_texts = []

        def _capture(token, chat_id, text, **kw):
            sent_texts.append(text)
            return {"message_id": len(sent_texts)}

        with mock.patch.object(lp.telegram_api, "send_message", side_effect=_capture), \
                mock.patch.object(lp, "_compose_revision_summary", return_value="🔁 Что изменилось: имя"):
            lp.redeliver_revised_protocol(
                REDELIVER_META, SAMPLE_OLD, SAMPLE_NEW,
                meta_json_path=self.meta_path, meeting_sid="sid",
            )
        self.assertTrue(sent_texts[0].startswith("🔁"))

    def test_idempotent_revision_skipped_when_hash_matches(self):
        """Повтор того же ревизионного контента → skipped (не задваиваем)."""
        new_hash = lp._protocol_content_hash(SAMPLE_NEW)
        self._write_delivered({
            "chat_id": 999, "message_ids": [5], "at": "old",
            "decision": "revision", "revision": 1, "content_hash": new_hash,
        })
        with mock.patch.object(lp.telegram_api, "send_message") as m:
            res = lp.redeliver_revised_protocol(
                REDELIVER_META, SAMPLE_OLD, SAMPLE_NEW,
                meta_json_path=self.meta_path, meeting_sid="sid",
            )
        self.assertEqual(res["status"], "skipped")
        m.assert_not_called()

    def test_disabled_gate(self):
        os.environ["ENABLE_PROTOCOL_DELIVERY"] = "0"
        try:
            res = lp.redeliver_revised_protocol(
                REDELIVER_META, SAMPLE_OLD, SAMPLE_NEW,
                meta_json_path=self.meta_path, meeting_sid="sid",
            )
            self.assertEqual(res["status"], "disabled")
        finally:
            os.environ.pop("ENABLE_PROTOCOL_DELIVERY", None)

    def test_revision_number_increments(self):
        new2 = SAMPLE_NEW.replace("Дарья Набережная", "Дарья Иванова")
        self._write_delivered({
            "chat_id": 999, "message_ids": [5], "at": "old",
            "decision": "revision", "revision": 2,
            "content_hash": lp._protocol_content_hash(SAMPLE_NEW),
        })
        with mock.patch.object(lp.telegram_api, "send_message", return_value={"message_id": 7}), \
                mock.patch.object(lp, "_compose_revision_summary", return_value="🔁"):
            res = lp.redeliver_revised_protocol(
                REDELIVER_META, SAMPLE_OLD, new2,
                meta_json_path=self.meta_path, meeting_sid="sid",
            )
        self.assertEqual(res["revision"], 3)


class TestF1IdempotencyIntact(unittest.TestCase):
    """5.6 РИСК: revision НЕ должен сломать идемпотентность Ф1.

    После ревизии meta.delivered содержит свежие message_ids → обычный
    повторный deliver_protocol по тому же контенту даёт skipped (collector
    получит rc=10), а НЕ повторную отправку.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test-f1-")
        self.meta_path = Path(self.tmpdir) / "meta.json"
        self.meta_path.write_text(json.dumps({}), encoding="utf-8")
        self._old_token = os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN")
        os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = "fake-token"
        os.environ.pop("ENABLE_PROTOCOL_DELIVERY", None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._old_token is None:
            os.environ.pop("TELEGRAM_NOTARIUS_BOT_TOKEN", None)
        else:
            os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = self._old_token

    def test_deliver_then_revision_then_repeat_deliver_skips(self):
        meta = {"series": "s", "date": "2026-06-02", "sessionUid": "sid",
                "expectedParticipants": ["Илья Рыбалка"], "participants": []}
        with mock.patch.object(lp.telegram_api, "send_message", return_value={"message_id": 1}), \
                mock.patch.object(lp, "_compose_revision_summary", return_value="🔁"):
            # 1) Первичная доставка SAMPLE_OLD.
            d1 = lp.deliver_protocol(
                meta, SAMPLE_OLD, meta_json_path=self.meta_path,
                target_chat_id=999, meeting_sid="sid",
            )
            self.assertEqual(d1["status"], "sent")
            # 2) Ревизия (SAMPLE_NEW) — обход идемпотентности.
            r = lp.redeliver_revised_protocol(
                meta, SAMPLE_OLD, SAMPLE_NEW,
                meta_json_path=self.meta_path, meeting_sid="sid",
            )
            self.assertEqual(r["status"], "sent")
            # 3) Повторный deliver_protocol тем же (ревизионным) контентом →
            #    идемпотентный skip (Ф1 цела), без новой отправки.
            d2 = lp.deliver_protocol(
                meta, SAMPLE_NEW, meta_json_path=self.meta_path,
                target_chat_id=999, meeting_sid="sid",
            )
            self.assertEqual(d2["status"], "skipped")


class TestContentHash(unittest.TestCase):

    def test_trailing_whitespace_ignored(self):
        a = "строка\nдве  \n"
        b = "строка\nдве\n"
        self.assertEqual(lp._protocol_content_hash(a), lp._protocol_content_hash(b))

    def test_real_change_differs(self):
        self.assertNotEqual(
            lp._protocol_content_hash(SAMPLE_OLD),
            lp._protocol_content_hash(SAMPLE_NEW),
        )


# --------------------------------------------------------------------------
# clarify_worker — ack-текст учитывает до-сыл
# --------------------------------------------------------------------------
class TestAckSuffix(unittest.TestCase):

    def test_sent(self):
        self.assertIn("дослал", cw._ack_suffix(True, "sent"))
        self.assertIn("дослал", cw._ack_suffix(False, "sent"))

    def test_not_late(self):
        self.assertEqual(cw._ack_suffix(False, "not-delivered-yet"), "✅ Применил")

    def test_late_no_redeliver(self):
        self.assertIn("Поздно", cw._ack_suffix(True, "not-delivered-yet"))


# --------------------------------------------------------------------------
# 5.3 — grace-окно перед первой доставкой (lib.delivery_grace)
# --------------------------------------------------------------------------
class TestDeliveryGrace(unittest.TestCase):

    def setUp(self):
        self._old = os.environ.get("PROTOCOL_DELIVERY_GRACE_SEC")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PROTOCOL_DELIVERY_GRACE_SEC", None)
        else:
            os.environ["PROTOCOL_DELIVERY_GRACE_SEC"] = self._old

    def test_default_grace(self):
        os.environ.pop("PROTOCOL_DELIVERY_GRACE_SEC", None)
        self.assertEqual(dg.delivery_grace_sec(), 300)

    def test_env_override(self):
        os.environ["PROTOCOL_DELIVERY_GRACE_SEC"] = "60"
        self.assertEqual(dg.delivery_grace_sec(), 60)

    def test_zero_and_invalid(self):
        os.environ["PROTOCOL_DELIVERY_GRACE_SEC"] = "0"
        self.assertEqual(dg.delivery_grace_sec(), 0)
        os.environ["PROTOCOL_DELIVERY_GRACE_SEC"] = "не число"
        self.assertEqual(dg.delivery_grace_sec(), 300)

    def test_wait_returns_skipped_when_grace_zero(self):
        os.environ["PROTOCOL_DELIVERY_GRACE_SEC"] = "0"
        self.assertEqual(dg.wait_for_clarify_grace("meeting-x"), "skipped")

    def test_wait_returns_early_when_resolved(self):
        os.environ["PROTOCOL_DELIVERY_GRACE_SEC"] = "300"
        with mock.patch.object(
            dg.clarify_state, "read_state", return_value={"status": "resolved"},
        ):
            self.assertEqual(dg.wait_for_clarify_grace("meeting-x"), "resolved")

    def test_wait_returns_gone_when_state_missing(self):
        os.environ["PROTOCOL_DELIVERY_GRACE_SEC"] = "300"
        with mock.patch.object(dg.clarify_state, "read_state", return_value=None):
            self.assertEqual(dg.wait_for_clarify_grace("meeting-x"), "gone")


if __name__ == "__main__":
    unittest.main()
