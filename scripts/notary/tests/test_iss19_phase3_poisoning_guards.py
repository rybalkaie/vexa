# -*- coding: utf-8 -*-
"""Ф3 (ISS-19, R7/R8/R9/R17): защита от отравления словаря.

План 2026-06-22 durable-learning-semantic-edits, Фаза 3. Ф2 научила хранилище писать
новые durable-типы (distinction/meaning/guidance) со scope по типу встречи. Ф3 кладёт
ПОВЕРХ записи гейты, чтобы расширенный захват не засорял durable мусором и не протекал
в глобальный (cross-company) уровень:

R7 — дисциплина уровня: групповая → company, 1:1 → series; cross-company global
     (`spellings-global.jsonl`) ТОЛЬКО по явному маркеру «везде» и ТОЛЬКО для term-like
     написаний; смысл/различение в cross-company global не уходят НИКОГДА (🔴 REQ 2.7).
R8 — порог уверенности: durable ниже порога не пишется (правка остаётся one-off, A4).
R9 — дедуп/конфликт: повтор не плодит дубли; одна и та же замена (один `wrong`) →
     одно актуальное правило, последняя побеждает (в том же сторе).
R17 — cross-kind конфликт: различение «X≠Y» отменяет замену «X→Y» и наоборот
     (кейс-инициатор Dream Story/23МПКТК), в промпт не уходят оба.

Запуск: python3 -m unittest tests.test_iss19_phase3_poisoning_guards (system python3.9).
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

GROUP = ["Илья Рыбалка", "Пётр Сидоров", "Мария"]   # бот не в составе → 3 человека → company
ONE_TO_ONE = ["Илья Рыбалка"]                        # 1 человек → series
COMPANY = "mpervyi"


class _Base(unittest.TestCase):
    """Песочница каталога обучения + мок привязки серия→компания (как Ф2)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "_feedback_edits"
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
            "ENABLE_FEEDBACK_LEARNING": "1",
            "ENABLE_FEEDBACK_LLM_CLASSIFY": "1",
            "TELEGRAM_NOTARIUS_BOT_TOKEN": "test-token",
        })
        self._env.start()
        self._cfs = mock.patch("notary.lib.context_knowledge.company_for_series",
                               return_value=COMPANY)
        self._cfs.start()

    def tearDown(self):
        self._cfs.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _classify(self, series, text, participants, result):
        """Прогон правки через боевую точку врезки `classify_remainder_edits`.

        `result` — durable-классификация без scope_candidate; добавляем фактический
        `scope_candidate(participants)` (как делает классификатор)."""
        cf = lambda t, p: {**result, "scope_candidate": fcl.scope_candidate(p)}
        state = {"series": series, "date": "2026-06-22",
                 "feedback_id": feedback_state.build_feedback_id(series, "2026-06-22", -1)}
        return fcl.classify_remainder_edits(
            state, [{"author": "Илья Рыбалка", "text": text}],
            meta={"expectedParticipants": participants}, classify_fn=cf, root=self.root)

    def _learn(self, series, text, meta=None):
        """Коннектор-путь приёма правок (`record_learning_from_edits`)."""
        state = {"series": series, "date": "2026-06-22", "round": 1,
                 "feedback_id": feedback_state.build_feedback_id(series, "2026-06-22", -1)}
        return fl.record_learning_from_edits(
            state, [{"author": "Илья", "text": text}], meta=meta, root=self.root)


# ===========================================================================
# R8 — порог уверенности: ниже порога durable НЕ пишется (остаётся one-off)
# ===========================================================================
class R8ConfidenceThresholdTest(_Base):
    MEAN = {"durable": True, "type": "meaning", "subjects": ["вывод"],
            "rule": "писать «с ИП», не «СП»"}
    TEXT = "вывод с ИП, чтобы не писал СП"

    def test_low_confidence_meaning_not_written(self):
        self._classify("seriesA", self.TEXT, GROUP, {**self.MEAN, "confidence": 0.3})
        # Спорная правка не появилась НИ в company, НИ в series jsonl.
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])
        self.assertEqual(fl.active_meaning_rules("seriesA", root=self.root), [])

    def test_high_confidence_meaning_written(self):
        self._classify("seriesA", self.TEXT, GROUP, {**self.MEAN, "confidence": 0.85})
        self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)

    def test_threshold_boundary_inclusive(self):
        # Ровно на пороге (>=) — проходит (граница не режет годное).
        with mock.patch.object(fl, "_DURABLE_MIN_CONFIDENCE", 0.6):
            self._classify("seriesA", self.TEXT, GROUP, {**self.MEAN, "confidence": 0.6})
        self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)

    def test_threshold_is_configurable_constant(self):
        # Порог — константа с env-override: ужесточили до 0.9 → 0.85 уже не пишется.
        with mock.patch.object(fl, "_DURABLE_MIN_CONFIDENCE", 0.9):
            self._classify("seriesA", self.TEXT, GROUP, {**self.MEAN, "confidence": 0.85})
            self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])
            self._classify("seriesA", self.TEXT, GROUP, {**self.MEAN, "confidence": 0.95})
            self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)

    def test_default_threshold_is_cautious_float(self):
        # Осторожный дефолт в (0,1): полезное может пройти, мусор < порога режется.
        self.assertGreater(fl._DURABLE_MIN_CONFIDENCE, 0.0)
        self.assertLess(fl._DURABLE_MIN_CONFIDENCE, 1.0)
        # «вывод с ИП» (R15) на дефолте обязан проходить (его боевой confidence ~0.85).
        self.assertLessEqual(fl._DURABLE_MIN_CONFIDENCE, 0.85)


# ===========================================================================
# R7 — дисциплина уровня + cross-company global ТОЛЬКО по маркеру «везде»
# ===========================================================================
class R7ScopeAndGlobalMarkerTest(_Base):
    MEAN = {"durable": True, "type": "meaning", "subjects": ["вывод"],
            "rule": "писать «с ИП», не «СП»", "confidence": 0.85}

    def test_group_meeting_without_marker_is_company(self):
        self._classify("seriesA", "вывод с ИП, чтобы не писал СП", GROUP, self.MEAN)
        comp = fl.active_company_meaning_rules(COMPANY, root=self.root)
        self.assertEqual(len(comp), 1)
        self.assertEqual(comp[0]["scope"], "company")
        self.assertEqual(fl.active_meaning_rules("seriesA", root=self.root), [])

    def test_one_to_one_without_marker_is_series(self):
        self._classify("seriesA", "вывод с ИП, чтобы не писал СП", ONE_TO_ONE, self.MEAN)
        ser = fl.active_meaning_rules("seriesA", root=self.root)
        self.assertEqual(len(ser), 1)
        self.assertEqual(ser[0]["scope"], "series")
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])

    def test_no_semantic_rule_reaches_cross_company_global_without_marker(self):
        # 🔴 Критерий R7: ни одно правило без «везде» не уходит в cross-company global.
        self._classify("seriesA", "вывод с ИП, чтобы не писал СП", GROUP, self.MEAN)
        self.assertEqual(fl.active_global_spellings(root=self.root), [])
        self.assertFalse(fl.global_log_path(root=self.root).exists())

    def test_meaning_with_vezde_marker_still_not_global(self):
        # 🔴 REQ 2.7: даже с маркером «везде» СМЫСЛ не уходит в cross-company global —
        # маркер для смысла не повышает уровень (остаётся company по типу встречи).
        self._classify("seriesA", "везде: вывод с ИП, чтобы не писал СП", GROUP, self.MEAN)
        self.assertEqual(fl.active_global_spellings(root=self.root), [])
        self.assertFalse(fl.global_log_path(root=self.root).exists())
        self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)

    def test_term_with_vezde_marker_goes_cross_company_global(self):
        # Положительный маркер: term-like классификация с «везде» → глобальное написание
        # (term-like — единственное, что допустимо cross-company). Тело после снятия
        # маркера регекспы не ловят → доходит до классификатора-остатка.
        term = {"durable": True, "type": "term", "subjects": ["Зифренд", "Zifriend"],
                "rule": "", "confidence": 0.9}
        self._classify("seriesA", "везде так пишем бренд", GROUP, term)
        glob = fl.active_global_spellings(root=self.root)
        self.assertEqual(len(glob), 1)
        self.assertEqual((glob[0]["wrong"], glob[0]["right"]), ("Зифренд", "Zifriend"))


# ===========================================================================
# R9 — дедуп (повтор не плодит) + конфликт (одна замена → одно правило, latest)
# ===========================================================================
class R9DedupAndConflictTest(_Base):
    def test_exact_repeat_meaning_idempotent(self):
        mean = {"durable": True, "type": "meaning", "subjects": ["вывод"],
                "rule": "писать «с ИП»", "confidence": 0.85}
        self._classify("seriesA", "вывод с ИП", GROUP, mean)
        self._classify("seriesA", "вывод с ИП", GROUP, mean)   # повтор той же правки
        self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)

    def test_term_same_wrong_latest_wins_in_same_series(self):
        # Две правки об одном `wrong` (РСЯ) с РАЗНЫМ написанием → одно актуальное,
        # последняя побеждает (старое снято supersede, не копится).
        self._learn("coord", "РСЯ → РЕЦ")
        self._learn("coord", "РСЯ → РЕЦБ")
        rules = fl.active_term_rules("coord", root=self.root)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["right"], "РЕЦБ")
        block = fl.format_learned_terms_block("coord", root=self.root)
        self.assertIn("«РСЯ» → пиши «РЕЦБ»", block)
        self.assertNotIn("РЕЦ»", block.replace("РЕЦБ»", ""))  # старого РЕЦ нет

    def test_term_same_wrong_other_series_not_touched(self):
        # Изоляция: конфликт «latest» резолвится в СВОЁМ сторе, чужую серию не трогает.
        self._learn("coord", "РСЯ → РЕЦ")
        self._learn("sales", "РСЯ → РЕЦБ")
        self.assertEqual(len(fl.active_term_rules("coord", root=self.root)), 1)
        self.assertEqual(fl.active_term_rules("coord", root=self.root)[0]["right"], "РЕЦ")
        self.assertEqual(fl.active_term_rules("sales", root=self.root)[0]["right"], "РЕЦБ")


# ===========================================================================
# R17 — cross-kind конфликт: различение ↔ замена взаимоисключающи (Dream Story кейс)
# ===========================================================================
class R17CrossKindConflictTest(_Base):
    DIST = {"durable": True, "type": "distinction",
            "subjects": ["Dream Story", "23МПКТК"], "rule": "разные разделы",
            "confidence": 0.9}

    def _seed_series_term(self, series, wrong, right):
        """Кладёт ранее выученную замену напрямую (23МПКТК не term-like для регекспа —
        воспроизводим состояние «бот уже выучил замену», как было в форензике)."""
        rid = fl.rule_id(series, wrong, right)
        fl._append_event(series, {"op": "learn", "id": rid, "kind": "term",
                                  "scope": "series", "series": series,
                                  "wrong": wrong, "right": right, "at": "t"}, root=self.root)
        return rid

    def test_group_distinction_supersedes_series_term_pair(self):
        # Кейс-инициатор: замена Dream Story→23МПКТК выучена per-series; приходит
        # различение (групповая → company) → старая замена снимается, в промпт не идёт.
        self._seed_series_term("seriesA", "Dream Story", "23МПКТК")
        self.assertEqual(len(fl.active_term_rules("seriesA", root=self.root)), 1)
        self._classify("seriesA", "не Dream Story, а 23МПКТК, это разное", GROUP, self.DIST)
        self.assertEqual(fl.active_term_rules("seriesA", root=self.root), [])
        self.assertEqual(len(fl.active_company_distinction_rules(COMPANY, root=self.root)), 1)
        block = fl.format_learned_terms_block("seriesA", root=self.root)
        self.assertNotIn("пиши «23МПКТК»", block)
        self.assertIn("разные", block.lower())

    def test_one_to_one_distinction_supersedes_same_series_term(self):
        self._seed_series_term("seriesA", "Dream Story", "23МПКТК")
        self._classify("seriesA", "не Dream Story, а 23МПКТК", ONE_TO_ONE, self.DIST)
        self.assertEqual(fl.active_term_rules("seriesA", root=self.root), [])
        self.assertEqual(len(fl.active_distinction_rules("seriesA", root=self.root)), 1)

    def test_term_pair_supersedes_prior_distinction(self):
        # Обратное направление: сначала различение, потом замена тех же сущностей →
        # различение снимается (последнее правило побеждает).
        self._classify("coord", "Зифренд и Zifriend разные сущности", ONE_TO_ONE,
                       {"durable": True, "type": "distinction",
                        "subjects": ["Зифренд", "Zifriend"], "rule": "", "confidence": 0.9})
        self.assertEqual(len(fl.active_distinction_rules("coord", root=self.root)), 1)
        self._learn("coord", "Зифренд → Zifriend")
        self.assertEqual(fl.active_distinction_rules("coord", root=self.root), [])
        self.assertEqual(len(fl.active_term_rules("coord", root=self.root)), 1)

    def test_global_term_supersedes_distinction_everywhere(self):
        # Глобальная замena отменяет различение тех же сущностей в любой серии.
        self._classify("seriesA", "Зифренд и Zifriend разные сущности", ONE_TO_ONE,
                       {"durable": True, "type": "distinction",
                        "subjects": ["Зифренд", "Zifriend"], "rule": "", "confidence": 0.9})
        self.assertEqual(len(fl.active_distinction_rules("seriesA", root=self.root)), 1)
        fl.record_global_spelling("Зифренд", "Zifriend", root=self.root)
        self.assertEqual(fl.active_distinction_rules("seriesA", root=self.root), [])

    def test_distinction_supersedes_global_term(self):
        # И наоборот: различение снимает уже выученную ГЛОБАЛЬНУЮ замену (global
        # со-рендерится в каждой серии → конфликт настоящий).
        fl.record_global_spelling("Зифренд", "Zifriend", root=self.root)
        self.assertEqual(len(fl.active_global_spellings(root=self.root)), 1)
        self._classify("seriesA", "Зифренд и Zifriend разные сущности", ONE_TO_ONE,
                       {"durable": True, "type": "distinction",
                        "subjects": ["Зифренд", "Zifriend"], "rule": "", "confidence": 0.9})
        self.assertEqual(fl.active_global_spellings(root=self.root), [])

    def test_distinction_does_not_touch_term_in_other_series(self):
        # Изоляция: различение в seriesB (1:1, series-scope) не снимает замену в seriesA
        # (разные серии — в одном промпте не встречаются, чужое правило не трогаем).
        self._seed_series_term("seriesA", "Зифренд", "Zifriend")
        self._classify("seriesB", "Зифренд и Zifriend разные сущности", ONE_TO_ONE,
                       {"durable": True, "type": "distinction",
                        "subjects": ["Зифренд", "Zifriend"], "rule": "", "confidence": 0.9})
        self.assertEqual(len(fl.active_term_rules("seriesA", root=self.root)), 1)


# ===========================================================================
# Лимиты — защита от лавины (cap числа правил в сторе)
# ===========================================================================
class StoreCapacityTest(_Base):
    def test_capacity_blocks_avalanche(self):
        with mock.patch.object(fl, "_MAX_ACTIVE_RULES_PER_STORE", 2):
            self.assertIsNotNone(fl.record_meaning_rule("seriesA", "альфа", "значение 1", root=self.root))
            self.assertIsNotNone(fl.record_meaning_rule("seriesA", "бета", "значение 2", root=self.root))
            # Третье в тот же стор — отсечено (лавина не раздувает словарь).
            self.assertIsNone(fl.record_meaning_rule("seriesA", "гамма", "значение 3", root=self.root))
        self.assertEqual(len(fl.active_meaning_rules("seriesA", root=self.root)), 2)


# ===========================================================================
# Опасная тройка (R6): supersede/гейты не логируют текст правки
# ===========================================================================
class DangerTripletDisciplineTest(_Base):
    def test_threshold_reject_logs_no_edit_text(self):
        secret = "СЕКРЕТ-вывод с ИП конфиденциально"
        with self.assertLogs("notary.lib.feedback_learning", level="INFO") as cm:
            fl.record_classified_rule(
                {"durable": True, "type": "meaning", "subjects": ["вывод"],
                 "rule": secret, "confidence": 0.1, "scope_candidate": "series"},
                series="seriesA", root=self.root)
        joined = "\n".join(cm.output)
        self.assertIn("conf=", joined)               # метаданные — есть
        self.assertNotIn("СЕКРЕТ", joined)           # текст правки — нет


if __name__ == "__main__":
    unittest.main()
