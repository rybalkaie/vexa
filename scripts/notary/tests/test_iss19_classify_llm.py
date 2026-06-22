# -*- coding: utf-8 -*-
"""Ф1 (ISS-19, R1/R2/R6/R13): LLM-классификатор «правка-на-будущее vs разовая».

Корень (план 2026-06-22 durable-learning-semantic-edits). Регекспы ловят durable
лишь узкими шаблонами; смысловые правки (различение сущностей, разговорное
объяснение смысла) теряются в `content`. Ф1: на ОСТАТКЕ регекспов LLM (Haiku)
решает durable/one-off и извлекает структурированное правило.

R1  — content-правка, не пойманная регекспами, проходит через LLM-классификатор:
      «вывод с ИП…» → durable=true; «убери абзац про кофе» → durable=false.
R2  — извлекается структура: тип + субъект(ы) + кандидат-уровень. «не Dream Story,
      а 23МПКТК» → type=distinction, оба субъекта зафиксированы.
R6  — опасная тройка: в лог только метаданные (длина/тип/confidence); текст правки
      НЕ логируется; в промпт — только правка + участники; сырой ответ не персистим.
R13 — фиче-флаг дефолт OFF: без флага реальный claude НЕ вызывается; на остатке []
      без обращения к сети.

Запуск: python3 -m unittest tests.test_iss19_classify_llm (system python3.9, без venv).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import feedback_classify_llm as fcl  # noqa: E402


_PARTICIPANTS_GROUP = ["Бот — протокол встречи", "Илья Рыбалка", "Пётр Сидоров", "Мария"]
_PARTICIPANTS_1TO1 = ["Бот — протокол встречи", "Илья Рыбалка"]


# ===========================================================================
# parse_classification_response (R6: сырой ответ не персистим — лишь разобранное)
# ===========================================================================
class ParseTest(unittest.TestCase):
    def test_durable_distinction(self):
        p = fcl.parse_classification_response(
            '{"durable":true,"type":"distinction",'
            '"subjects":["Dream Story","23МПКТК"],"rule":"разные разделы","confidence":0.9}')
        self.assertTrue(p["durable"])
        self.assertEqual(p["type"], "distinction")
        self.assertEqual(p["subjects"], ["Dream Story", "23МПКТК"])
        self.assertEqual(p["confidence"], 0.9)

    def test_one_off_clears_structure(self):
        p = fcl.parse_classification_response(
            '{"durable":false,"type":"meaning","subjects":["x"],"rule":"y","confidence":0.3}')
        self.assertFalse(p["durable"])
        self.assertEqual(p["type"], "one-off")
        self.assertEqual(p["subjects"], [])
        self.assertEqual(p["rule"], "")

    def test_markdown_fence_stripped(self):
        p = fcl.parse_classification_response('```json\n{"durable":false,"confidence":0.1}\n```')
        self.assertIsNotNone(p)
        self.assertFalse(p["durable"])

    def test_prose_wrapped_first_object(self):
        p = fcl.parse_classification_response('Вот ответ: {"durable":true,"type":"meaning","subjects":["вывод"]} ок')
        self.assertIsNotNone(p)
        self.assertEqual(p["type"], "meaning")

    def test_unknown_durable_type_becomes_guidance(self):
        p = fcl.parse_classification_response('{"durable":true,"type":"whatever","subjects":["a"]}')
        self.assertEqual(p["type"], "guidance")

    def test_confidence_clamped_and_defaulted(self):
        self.assertEqual(fcl.parse_classification_response('{"durable":true,"type":"term","confidence":1.5}')["confidence"], 1.0)
        self.assertEqual(fcl.parse_classification_response('{"durable":true,"type":"term","confidence":-2}')["confidence"], 0.0)
        self.assertEqual(fcl.parse_classification_response('{"durable":true,"type":"term","confidence":"x"}')["confidence"], 0.0)

    def test_subjects_normalized_dedup_capped(self):
        p = fcl.parse_classification_response(
            '{"durable":true,"type":"distinction","subjects":["A","A","","B","C","D","E"]}')
        self.assertEqual(p["subjects"], ["A", "B", "C", "D"])  # дедуп + срез до _MAX_SUBJECTS

    def test_garbage_returns_none(self):
        self.assertIsNone(fcl.parse_classification_response("no json here"))
        self.assertIsNone(fcl.parse_classification_response(""))
        self.assertIsNone(fcl.parse_classification_response("[1,2,3]"))


# ===========================================================================
# R1 — durable vs one-off (LLM замокан через request_fn)
# ===========================================================================
@mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "1"})
class R1DurableTest(unittest.TestCase):
    def test_meaning_edit_is_durable(self):
        # «вывод с ИП, чтобы не писал СП» → durable
        fn = lambda t, p: {"durable": True, "type": "meaning",
                           "subjects": ["вывод"], "rule": "писать «с ИП», не «СП»",
                           "confidence": 0.85}
        res = fcl.classify_edit_llm("вывод с ИП, чтобы не писал СП",
                                    participants=_PARTICIPANTS_GROUP, request_fn=fn)
        self.assertTrue(res["durable"])
        self.assertEqual(res["type"], "meaning")

    def test_one_off_edit_stays_one_off(self):
        # «убери абзац про кофе» → одноразовая
        fn = lambda t, p: {"durable": False, "type": "one-off",
                           "subjects": [], "rule": "", "confidence": 0.1}
        res = fcl.classify_edit_llm("убери абзац про кофе",
                                    participants=_PARTICIPANTS_GROUP, request_fn=fn)
        self.assertFalse(res["durable"])
        self.assertEqual(res["type"], "one-off")


# ===========================================================================
# R2 — структура правила (тип + субъекты + кандидат-уровень)
# ===========================================================================
@mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "1"})
class R2StructureTest(unittest.TestCase):
    def test_distinction_two_subjects(self):
        fn = lambda t, p: {"durable": True, "type": "distinction",
                           "subjects": ["Dream Story", "23МПКТК"],
                           "rule": "разные разделы, не объединять", "confidence": 0.9}
        res = fcl.classify_edit_llm("не Dream Story, а 23МПКТК",
                                    participants=_PARTICIPANTS_GROUP, request_fn=fn)
        self.assertEqual(res["type"], "distinction")
        self.assertIn("Dream Story", res["subjects"])
        self.assertIn("23МПКТК", res["subjects"])
        self.assertEqual(len(res["subjects"]), 2)

    def test_scope_candidate_in_output(self):
        fn = lambda t, p: {"durable": True, "type": "meaning", "subjects": ["вывод"],
                           "rule": "с ИП", "confidence": 0.8}
        res = fcl.classify_edit_llm("вывод с ИП", participants=_PARTICIPANTS_GROUP, request_fn=fn)
        self.assertIn("scope_candidate", res)


# ===========================================================================
# Кандидат-уровень по типу встречи (A3/R7 хинт; дисциплину вводит Ф3)
# ===========================================================================
class ScopeCandidateTest(unittest.TestCase):
    def test_group_meeting_company(self):
        self.assertEqual(fcl.scope_candidate(_PARTICIPANTS_GROUP), "company")

    def test_one_to_one_series(self):
        self.assertEqual(fcl.scope_candidate(_PARTICIPANTS_1TO1), "series")

    def test_unknown_participants_series(self):
        self.assertEqual(fcl.scope_candidate([]), "series")
        self.assertEqual(fcl.scope_candidate(None), "series")


# ===========================================================================
# R6 — опасная тройка: лог/промпт без лишнего, текст правки не утекает
# ===========================================================================
class R6DangerTripletTest(unittest.TestCase):
    def test_prompt_has_edit_and_names_only(self):
        prompt = fcl.build_classify_user_prompt("вывод с ИП", _PARTICIPANTS_GROUP)
        self.assertIn("вывод с ИП", prompt)
        self.assertIn("Илья Рыбалка", prompt)
        self.assertIn("ДАННЫЕ", prompt)             # anti-injection рамка
        self.assertNotIn("ТРАНСКРИПТ", prompt.upper())  # транскрипт не подаём

    @mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "1"})
    def test_log_carries_no_edit_text(self):
        secret = "СЕКРЕТНАЯТЕМАГ7"
        # Замокан реальный claude — возвращает валидный JSON, текста правки нет в логе.
        with mock.patch("notary.lib.claude_cli.call_claude_print",
                        return_value='{"durable":true,"type":"meaning","subjects":["x"],"rule":"y","confidence":0.7}'):
            with self.assertLogs("notary.feedback_classify_llm", level="INFO") as cm:
                fcl.request_classification(f"вывод {secret} с ИП", _PARTICIPANTS_GROUP,
                                           meeting_sid="m1")
        joined = "\n".join(cm.output)
        self.assertNotIn(secret, joined)            # текст правки НЕ в логах
        self.assertIn("durable=True", joined)       # метаданные — есть

    @mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "1"})
    def test_result_exposes_no_raw(self):
        fn = lambda t, p: {"durable": True, "type": "meaning", "subjects": ["x"],
                           "rule": "y", "confidence": 0.7}
        res = fcl.classify_edit_llm("вывод с ИП", participants=_PARTICIPANTS_GROUP, request_fn=fn)
        self.assertEqual(set(res.keys()),
                         {"durable", "type", "subjects", "rule", "confidence", "scope_candidate"})


# ===========================================================================
# R13 — фиче-флаг дефолт OFF: реальный claude НЕ вызывается
# ===========================================================================
class R13FlagGateTest(unittest.TestCase):
    @mock.patch.dict(os.environ, {}, clear=False)
    def test_default_off(self):
        os.environ.pop("ENABLE_FEEDBACK_LLM_CLASSIFY", None)
        self.assertFalse(fcl.is_enabled())

    @mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "1"})
    def test_on_values(self):
        self.assertTrue(fcl.is_enabled())

    @mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "0"})
    def test_off_does_not_call_request_fn(self):
        def boom(t, p):
            raise AssertionError("request_fn не должен вызываться при флаге OFF")
        res = fcl.classify_edit_llm("вывод с ИП", participants=_PARTICIPANTS_GROUP, request_fn=boom)
        self.assertFalse(res["durable"])
        self.assertEqual(res["type"], "one-off")

    @mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "0"})
    def test_remainder_off_returns_empty_no_claude(self):
        with mock.patch("notary.lib.claude_cli.call_claude_print",
                        side_effect=AssertionError("claude не должен вызываться при флаге OFF")):
            out = fcl.classify_remainder_edits(
                {"series": "s", "feedback_id": "f"},
                [{"text": "вывод с ИП, чтобы не писал СП"}],
                meta={"participants": _PARTICIPANTS_GROUP})
        self.assertEqual(out, [])


# ===========================================================================
# Graceful degrade — грязный вход / сбой LLM → one-off, не падает
# ===========================================================================
@mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "1"})
class GracefulDegradeTest(unittest.TestCase):
    def _boom(self, t, p):
        raise RuntimeError("LLM упал")

    def test_empty_input_one_off_no_llm(self):
        res = fcl.classify_edit_llm("   ", participants=_PARTICIPANTS_GROUP, request_fn=self._boom)
        self.assertFalse(res["durable"])

    def test_too_long_input_one_off_no_llm(self):
        long_text = "вывод " * 500
        res = fcl.classify_edit_llm(long_text, participants=_PARTICIPANTS_GROUP, request_fn=self._boom)
        self.assertFalse(res["durable"])

    def test_non_russian_one_off_no_llm(self):
        res = fcl.classify_edit_llm("delete this paragraph", participants=_PARTICIPANTS_GROUP, request_fn=self._boom)
        self.assertFalse(res["durable"])

    def test_llm_raises_returns_one_off(self):
        res = fcl.classify_edit_llm("вывод с ИП", participants=_PARTICIPANTS_GROUP, request_fn=self._boom)
        self.assertFalse(res["durable"])
        self.assertEqual(res["type"], "one-off")

    def test_llm_returns_garbage_one_off(self):
        res = fcl.classify_edit_llm("вывод с ИП", participants=_PARTICIPANTS_GROUP,
                                    request_fn=lambda t, p: "not a dict")
        self.assertFalse(res["durable"])

    def test_request_classification_cli_error_returns_none(self):
        from notary.lib.claude_cli import ClaudeCliTimeout
        with mock.patch("notary.lib.claude_cli.call_claude_print",
                        side_effect=ClaudeCliTimeout("timeout")):
            self.assertIsNone(fcl.request_classification("вывод с ИП", _PARTICIPANTS_GROUP))


# ===========================================================================
# Остаток (РИСК4) — LLM зовётся ТОЛЬКО на том, что регекспы не распознали
# ===========================================================================
@mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "1"})
class RemainderGatingTest(unittest.TestCase):
    def test_regex_caught_edit_skipped(self):
        # «не РСЯ, а РЕЦ» ловит extract_learned_terms → LLM НЕ зовём.
        calls = []
        cf = lambda t, p: (calls.append(t), {"durable": False, "type": "one-off",
                                             "subjects": [], "rule": "", "confidence": 0.0})[1]
        out = fcl.classify_remainder_edits(
            {"series": "s", "feedback_id": "f"},
            [{"text": "не РСЯ, а РЕЦ"}],
            meta={"participants": _PARTICIPANTS_GROUP}, classify_fn=cf)
        self.assertEqual(calls, [])                 # регекс поймал → классификатор не звали
        self.assertEqual(out, [])

    def test_meaning_connector_caught_skipped(self):
        # «июльские проекты — это вывоз» ловит extract_meaning_rules → LLM НЕ зовём.
        calls = []
        cf = lambda t, p: (calls.append(t), {"durable": False, "type": "one-off",
                                             "subjects": [], "rule": "", "confidence": 0.0})[1]
        fcl.classify_remainder_edits(
            {"series": "s", "feedback_id": "f"},
            [{"text": "июльские проекты — это вывоз Space Projector"}],
            meta={"participants": _PARTICIPANTS_GROUP}, classify_fn=cf)
        self.assertEqual(calls, [])

    def test_remainder_edit_classified(self):
        # «не Dream Story, а 23МПКТК» — оба конца НЕ term-like (Dream Story = два
        # слова, проходит; но различение здесь регекс отдаёт?) проверяем фактический
        # остаток: классификатор зовётся на правке, которую регекспы не выучили.
        calls = []
        cf = lambda t, p: (calls.append(t),
                           {"durable": True, "type": "distinction",
                            "subjects": ["Dream Story", "23МПКТК"],
                            "rule": "разные разделы", "confidence": 0.9})[1]
        out = fcl.classify_remainder_edits(
            {"series": "s", "feedback_id": "f"},
            [{"text": "раздел не Dream Story, а 23МПКТК, это разные вещи"}],
            meta={"participants": _PARTICIPANTS_GROUP}, classify_fn=cf)
        self.assertEqual(len(calls), 1)             # остаток → классификатор позван
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["durable"])


if __name__ == "__main__":
    unittest.main()
