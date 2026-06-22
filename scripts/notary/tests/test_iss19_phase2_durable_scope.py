# -*- coding: utf-8 -*-
"""Ф2 (ISS-19, R3/R4/R5): новые durable-типы + scope=company + рендер в промпт.

План 2026-06-22 durable-learning-semantic-edits, Фаза 2. Ф1 построила LLM-классификатор
(durable/type/subjects/rule/confidence/scope_candidate). Ф2 учит ХРАНИЛИЩЕ и РЕНДЕР:

R3 — distinction «A ≠ B» хранится со scope ПО ТИПУ ВСТРЕЧИ: групповая → company,
     личная 1:1 → series.
R4 — разговорное объяснение смысла («вывод с ИП») фиксируется как durable БЕЗ
     коннектора «— это»; scope по типу встречи (групповая → company, 1:1 → series).
R5 — новые durable-типы рендерятся в промпт регенерации на встречах ВСЕЙ компании.

+ Аддитивная совместимость: старые jsonl без kind/scope читаются как term/series.
+ НЕС1: старый коннектор-путь `extract_meaning_rules` теперь тоже scope-по-типу-встречи.
+ Конфликт уровней per-series > company > global.
+ Флаг OFF → запись не зовёт реальный claude и ничего не пишет.

Запуск: python3 -m unittest tests.test_iss19_phase2_durable_scope (system python3.9, без venv).
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
from notary.lib import llm_postprocess as lp  # noqa: E402

# Группа = бот + ≥2 человек → scope_candidate=company; 1:1 = бот + 1 человек → series.
GROUP = ["Илья Рыбалка", "Пётр Сидоров", "Мария"]
ONE_TO_ONE = ["Илья Рыбалка"]
COMPANY = "mpervyi"


class _Base(unittest.TestCase):
    """Песочница каталога обучения + мок привязки серия→компания (company_for_series)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "_feedback_edits"
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
            "ENABLE_FEEDBACK_LEARNING": "1",
            "ENABLE_FEEDBACK_LLM_CLASSIFY": "1",
            "TELEGRAM_NOTARIUS_BOT_TOKEN": "test-token",
        })
        self._env.start()
        # Любая серия в этих тестах принадлежит компании mpervyi (мок YAML-привязки).
        self._cfs = mock.patch("notary.lib.context_knowledge.company_for_series",
                               return_value=COMPANY)
        self._cfs.start()

    def tearDown(self):
        self._cfs.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _classify(self, series, text, participants, result):
        """Прогон правки через боевую точку врезки `classify_remainder_edits`.

        `result` — durable-классификация без scope_candidate; добавляем его реальным
        `scope_candidate(participants)` (как делает классификатор), чтобы scope-дисциплина
        Ф2 проверялась по фактическому типу встречи."""
        cf = lambda t, p: {**result, "scope_candidate": fcl.scope_candidate(p)}
        state = {"series": series, "date": "2026-06-22",
                 "feedback_id": feedback_state.build_feedback_id(series, "2026-06-22", -1)}
        return fcl.classify_remainder_edits(
            state, [{"author": "Илья Рыбалка", "text": text}],
            meta={"expectedParticipants": participants}, classify_fn=cf, root=self.root)


# ===========================================================================
# R3 — distinction со scope по типу встречи
# ===========================================================================
class R3DistinctionScopeTest(_Base):
    DIST = {"durable": True, "type": "distinction",
            "subjects": ["Dream Story", "23МПКТК"], "rule": "разные разделы",
            "confidence": 0.9}
    TEXT = "раздел не Dream Story, а 23МПКТК, это разные вещи"

    def test_group_meeting_distinction_is_company(self):
        self._classify("seriesA", self.TEXT, GROUP, self.DIST)
        comp = fl.active_company_distinction_rules(COMPANY, root=self.root)
        self.assertEqual(len(comp), 1)
        self.assertEqual(comp[0]["scope"], "company")
        self.assertEqual(comp[0]["company"], COMPANY)
        pair = sorted([comp[0]["subject_a"], comp[0]["subject_b"]])
        self.assertEqual(pair, sorted(["Dream Story", "23МПКТК"]))
        # На уровне серии — пусто (ушло в company).
        self.assertEqual(fl.active_distinction_rules("seriesA", root=self.root), [])

    def test_one_to_one_distinction_is_series(self):
        self._classify("seriesA", self.TEXT, ONE_TO_ONE, self.DIST)
        ser = fl.active_distinction_rules("seriesA", root=self.root)
        self.assertEqual(len(ser), 1)
        self.assertEqual(ser[0]["scope"], "series")
        # В company-сторадж не утекло (приватность 1:1 не протекает).
        self.assertEqual(fl.active_company_distinction_rules(COMPANY, root=self.root), [])

    def test_distinction_idempotent_unordered_pair(self):
        self._classify("seriesA", self.TEXT, GROUP, self.DIST)
        # Та же пара в обратном порядке («не 23МПКТК, а Dream Story») — НЕ дубль.
        flipped = {**self.DIST, "subjects": ["23МПКТК", "Dream Story"]}
        self._classify("seriesA", "не 23МПКТК, а Dream Story, это разное", GROUP, flipped)
        self.assertEqual(len(fl.active_company_distinction_rules(COMPANY, root=self.root)), 1)


# ===========================================================================
# R4 — разговорное объяснение смысла без коннектора, scope по типу встречи
# ===========================================================================
class R4MeaningScopeTest(_Base):
    MEAN = {"durable": True, "type": "meaning", "subjects": ["вывод"],
            "rule": "писать «с ИП», не «СП»", "confidence": 0.85}
    TEXT = "вывод с ИП, чтобы не писал СП"

    def test_group_meeting_meaning_is_company(self):
        self._classify("seriesA", self.TEXT, GROUP, self.MEAN)
        comp = fl.active_company_meaning_rules(COMPANY, root=self.root)
        self.assertEqual(len(comp), 1)
        self.assertEqual(comp[0]["scope"], "company")
        self.assertEqual(comp[0]["subject"], "вывод")
        self.assertIn("с ИП", comp[0]["meaning"])
        self.assertEqual(fl.active_meaning_rules("seriesA", root=self.root), [])

    def test_one_to_one_meaning_is_series(self):
        self._classify("seriesA", self.TEXT, ONE_TO_ONE, self.MEAN)
        ser = fl.active_meaning_rules("seriesA", root=self.root)
        self.assertEqual(len(ser), 1)
        self.assertEqual(ser[0]["scope"], "series")
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])

    def test_guidance_durable_not_lost(self):
        # durable, но не distinction/meaning → kind=guidance (не теряем правку молча).
        g = {"durable": True, "type": "guidance", "subjects": [],
             "rule": "суммы писать в тысячах рублей", "confidence": 0.8}
        self._classify("seriesA", "суммы всегда в тысячах рублей пиши", GROUP, g)
        comp = fl.active_company_guidance_rules(COMPANY, root=self.root)
        self.assertEqual(len(comp), 1)
        self.assertIn("тысячах", comp[0]["rule"])


# ===========================================================================
# R5 — рендер новых типов в промпт регенерации на встречах компании
# ===========================================================================
class R5RenderTest(_Base):
    def test_company_rules_render_on_other_series_of_company(self):
        # Выучили на встрече серии A (групповая → company).
        self._classify("seriesA", "раздел не Dream Story, а 23МПКТК, это разные вещи", GROUP,
                       {"durable": True, "type": "distinction",
                        "subjects": ["Dream Story", "23МПКТК"], "rule": "разные разделы",
                        "confidence": 0.9})
        self._classify("seriesA", "вывод с ИП, чтобы не писал СП", GROUP,
                       {"durable": True, "type": "meaning", "subjects": ["вывод"],
                        "rule": "писать «с ИП», не «СП»", "confidence": 0.85})
        # На ДРУГОЙ серии той же компании блок содержит оба правила.
        block = fl.format_learned_terms_block("seriesB-другая", root=self.root)
        self.assertIn("23МПКТК", block)
        self.assertIn("Dream Story", block)
        self.assertIn("разные", block.lower())
        self.assertIn("вывод", block)
        self.assertIn("с ИП", block)

    def test_company_rules_reach_real_generation_prompt(self):
        self._classify("seriesA", "раздел не Dream Story, а 23МПКТК, это разные вещи", GROUP,
                       {"durable": True, "type": "distinction",
                        "subjects": ["Dream Story", "23МПКТК"], "rule": "разные разделы",
                        "confidence": 0.9})
        self._classify("seriesA", "вывод с ИП, чтобы не писал СП", GROUP,
                       {"durable": True, "type": "meaning", "subjects": ["вывод"],
                        "rule": "писать «с ИП», не «СП»", "confidence": 0.85})
        # РЕАЛЬНАЯ инъекция в промпт генерации след. встречи компании (без мока рендера).
        prompt = lp._format_protocol_user_prompt(
            "00:00 спикер: текст\n", {"series": "seriesB-другая", "date": "2026-06-29"})
        self.assertIn("23МПКТК", prompt)
        self.assertIn("вывод", prompt)
        self.assertIn("РАЗЛИЧЕНИЯ", prompt)

    def test_other_company_does_not_see_rules(self):
        self._classify("seriesA", "вывод с ИП, чтобы не писал СП", GROUP,
                       {"durable": True, "type": "meaning", "subjects": ["вывод"],
                        "rule": "писать «с ИП», не «СП»", "confidence": 0.85})
        # Серия другой компании (мок company_for_series → None) company-правил не видит.
        with mock.patch("notary.lib.context_knowledge.company_for_series", return_value="anzhee"):
            block = fl.format_learned_terms_block("seriesC-anzhee", root=self.root)
        self.assertNotIn("вывод", block)


# ===========================================================================
# Конфликт уровней: per-series > company > global (специфичнее побеждает)
# ===========================================================================
class ConflictLevelsTest(_Base):
    def test_series_meaning_overrides_company(self):
        # company-смысл по субъекту «вывод».
        fl.record_meaning_rule("seriesA", "вывод", "company-версия",
                               scope="company", company=COMPANY, root=self.root)
        # per-series смысл того же субъекта — побеждает.
        fl.record_meaning_rule("seriesA", "вывод", "series-версия",
                               scope="series", root=self.root)
        block = fl.format_learned_terms_block("seriesA", root=self.root)
        self.assertIn("series-версия", block)
        self.assertNotIn("company-версия", block)

    def test_series_spelling_overrides_company_and_global(self):
        fl.record_global_spelling("РСЯ", "GLOBAL", root=self.root)
        fl.record_meaning_rule("seriesA", "noop", "x", root=self.root)  # touch series file
        # company term-правило того же wrong.
        fl._append_event_to_path(
            fl.company_log_path(COMPANY, root=self.root),
            {"op": "learn", "id": "lt-c-1", "kind": "term", "scope": "company",
             "company": COMPANY, "wrong": "РСЯ", "right": "COMPANY", "at": "t"})
        # per-series term-правило того же wrong — побеждает оба.
        fl._append_event("seriesA",
                         {"op": "learn", "id": "lt-s-1", "kind": "term", "scope": "series",
                          "series": "seriesA", "wrong": "РСЯ", "right": "SERIES", "at": "t"},
                         root=self.root)
        block = fl.format_learned_terms_block("seriesA", root=self.root)
        self.assertIn("«РСЯ» → пиши «SERIES»", block)
        self.assertNotIn("COMPANY", block)
        self.assertNotIn("GLOBAL", block)

    def test_two_company_meanings_same_subject_both_render(self):
        # Н1 (цикл5 ход1): два company-уточнения ОДНОГО субъекта, выученные на РАЗНЫХ
        # сериях компании — аддитивны, оба обязаны дойти до промпта. Раньше `_merge_levels`
        # схлопывал их по subject и молча терял более новое (против сверхидеи).
        fl.record_meaning_rule("seriesA", "вывод", "писать с ИП",
                               scope="company", company=COMPANY, root=self.root)
        fl.record_meaning_rule("seriesB", "вывод", "ставить отдельной строкой",
                               scope="company", company=COMPANY, root=self.root)
        block = fl.format_learned_terms_block("seriesC-другая", root=self.root)
        self.assertIn("с ИП", block)
        self.assertIn("отдельной строкой", block)

    def test_two_series_meanings_same_subject_both_render(self):
        # Тот же инвариант на уровне серии: два уточнения одного субъекта из разных
        # раундов рендерятся оба (внутриуровневой аддитивности раньше не было).
        fl.record_meaning_rule("seriesA", "отчёт", "квартальный, не годовой", root=self.root)
        fl.record_meaning_rule("seriesA", "отчёт", "присылать в PDF", root=self.root)
        block = fl.format_learned_terms_block("seriesA", root=self.root)
        self.assertIn("квартальный", block)
        self.assertIn("PDF", block)


# ===========================================================================
# НЕС1 — старый коннектор-путь тоже scope-по-типу-встречи
# ===========================================================================
class Nes1ConnectorScopeTest(_Base):
    def _learn(self, series, text, meta):
        state = {"series": series, "date": "2026-06-22",
                 "feedback_id": feedback_state.build_feedback_id(series, "2026-06-22", -1)}
        return fl.record_learning_from_edits(
            state, [{"author": "Илья", "text": text}], meta=meta, root=self.root)

    def test_connector_group_is_company(self):
        # «X — это Y» на групповой встрече → company-смысл (раньше всегда был series).
        self._learn("seriesA", "июльские проекты — это вывоз Space Projector",
                    {"expectedParticipants": GROUP})
        self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)
        self.assertEqual(fl.active_meaning_rules("seriesA", root=self.root), [])

    def test_connector_one_to_one_is_series(self):
        self._learn("seriesA", "июльские проекты — это вывоз Space Projector",
                    {"expectedParticipants": ONE_TO_ONE})
        self.assertEqual(len(fl.active_meaning_rules("seriesA", root=self.root)), 1)
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])

    def test_connector_no_meta_is_series(self):
        # Без meta (старый контракт Ф6) — по-прежнему series, ничего не сломалось.
        self._learn("seriesA", "июльские проекты — это вывоз Space Projector", None)
        self.assertEqual(len(fl.active_meaning_rules("seriesA", root=self.root)), 1)
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])

    def test_term_pair_stays_per_series_regardless_of_meeting_type(self):
        # Термы «слово=слово» — как раньше (per-series), тип встречи их НЕ повышает.
        self._learn("seriesA", "Гарсия → Гарсиа", {"expectedParticipants": GROUP})
        self.assertEqual(len(fl.active_term_rules("seriesA", root=self.root)), 1)
        self.assertEqual(fl.active_company_term_rules(COMPANY, root=self.root), [])


# ===========================================================================
# Аддитивная совместимость: старые jsonl без kind/scope
# ===========================================================================
class BackwardCompatTest(_Base):
    def test_legacy_record_without_kind_scope_reads_as_term_series(self):
        # Старая строка Ф6 — без kind/scope/company.
        fl._append_event("seriesA",
                         {"op": "learn", "id": "lt-legacy", "series": "seriesA",
                          "wrong": "РСЯ", "right": "РЕЦ", "at": "t"}, root=self.root)
        rules = fl.active_rules("seriesA", root=self.root)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["kind"], "term")     # дефолт kind
        self.assertEqual(rules[0]["scope"], "series")  # дефолт scope
        # И рендерится как написание.
        self.assertIn("«РСЯ» → пиши «РЕЦ»", fl.format_learned_terms_block("seriesA", root=self.root))


# ===========================================================================
# Флаг OFF — запись не происходит, реальный claude не зовётся
# ===========================================================================
class FlagOffTest(_Base):
    def test_classify_off_writes_nothing_no_claude(self):
        with mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LLM_CLASSIFY": "0"}), \
             mock.patch("notary.lib.claude_cli.call_claude_print",
                        side_effect=AssertionError("claude не должен вызываться при флаге OFF")):
            out = fcl.classify_remainder_edits(
                {"series": "seriesA", "feedback_id": "f"},
                [{"text": "вывод с ИП, чтобы не писал СП"}],
                meta={"expectedParticipants": GROUP}, root=self.root)
        self.assertEqual(out, [])
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])

    def test_learning_gate_off_blocks_company_write(self):
        # Гейт обучения OFF → company-запись тоже не происходит.
        with mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LEARNING": "0"}):
            self.assertIsNone(fl.record_distinction_rule(
                "A", "B", series="seriesA", company=COMPANY, scope="company", root=self.root))
        self.assertEqual(fl.active_company_distinction_rules(COMPANY, root=self.root), [])


if __name__ == "__main__":
    unittest.main()
