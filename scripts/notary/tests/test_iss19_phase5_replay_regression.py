# -*- coding: utf-8 -*-
"""Ф5 (ISS-19, R14/R15): боевой реплей форензики 2026-06-22 + регресс старого обучения.

План 2026-06-22 durable-learning-semantic-edits, Фаза 5. Доказываем НА
РЕПРЕЗЕНТАТИВНЫХ данных встречи `mpervyi-pn-koord-finplan` 2026-06-22, что фича бьёт
в КОРЕНЬ ISS-19 — смысловые правки теперь становятся durable — и НЕ сломала старое
обучение (терм-пары / роли / коннекторы / глобальный маркер).

Реплей-набор (репрезентативный — точного дампа 11 правок встречи на машине нет, он
живёт в эфемерном `_tmp/_feedback_edits/` на VPS; собран из ДОКУМЕНТИРОВАННЫХ правок
форензики, ISS-19 §19 + план §25):
  • 2 чистые ТЕРМ-ПАРЫ, что выучились и раньше: MLAB→MLOVE, ДЭК→СДЭК (детерм. путь).
  • 2 ЦЕННЫЕ СМЫСЛОВЫЕ, что терялись: «не Dream Story, а 23МПКТК» (РАЗЛИЧЕНИЕ),
    «вывод с ИП, чтобы не писал СП» (СМЫСЛ) — теперь durable через LLM-остаток.
  • НЕГАТИВЫ-ОРАКУЛ (разовые): «убери абзац про кофе», «сократи раздел» — остаются
    one-off (мусор не отравляет словарь).

Встреча `mpervyi-pn-koord-finplan` — ГРУППОВАЯ координация (бот + ≥2 человека) →
смысл/различение пишутся на уровне КОМПАНИИ (`company=mpervyi`), по типу встречи (A3/R7).

Реплей моделирует ЖИВОЙ путь `reissue_one` на success (`feedback_reissue.py:1075`/
`:1102`): на одном и том же батче контент-правок вызываются ОБА интейка —
детерминированный `record_learning_from_edits` (терм/коннектор) И LLM-остаток
`classify_remainder_edits` (различение/смысл/guidance). LLM инжектируется через
`classify_fn` (реальный claude НЕ зовётся; confidence — РЕАЛИСТИЧНЫЕ, как боевой Haiku).

Запуск: python3 -m unittest tests.test_iss19_phase5_replay_regression (system python3.9).
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

from notary.lib import feedback_learning as fl  # noqa: E402
from notary.lib import feedback_classify_llm as fcl  # noqa: E402
from notary.lib import feedback_router as fr  # noqa: E402
from notary.lib import feedback_state  # noqa: E402

# Состав ГРУППОВОЙ встречи (бот не в составе → 3 человека → company-уровень).
GROUP = ["Илья Рыбалка", "Мария Михина", "Пётр Сидоров"]
ONE_TO_ONE = ["Илья Рыбалка"]
COMPANY = "mpervyi"


# ── Реалистичные классификации LLM для реплея (боевой Haiku-подобный выход) ──────
# Каждое значение — выход `classify_edit_llm` БЕЗ scope_candidate (его добавляет
# диспетчер из фактических участников, как делает боевой классификатор). Confidence —
# реалистичные: явное различение «разные разделы» ~0.9; объяснение смысла ~0.82
# (документированный боевой ~0.85); разовые правки → durable=false.
_LLM_REPLAY = {
    "не Dream Story, а 23МПКТК": {
        "durable": True, "type": "distinction",
        "subjects": ["Dream Story", "23МПКТК"],
        "rule": "разные разделы, не объединять", "confidence": 0.9,
    },
    "вывод с ИП": {
        "durable": True, "type": "meaning", "subjects": ["вывод"],
        "rule": "писать «с ИП», не «СП»", "confidence": 0.82,
    },
    "убери этот абзац про кофе": {
        "durable": False, "type": "one-off", "subjects": [], "rule": "",
        "confidence": 0.0,
    },
    "сократи раздел про планы": {
        "durable": False, "type": "one-off", "subjects": [], "rule": "",
        "confidence": 0.0,
    },
    "поправь дату встречи на 23 июня": {
        "durable": False, "type": "one-off", "subjects": [], "rule": "",
        "confidence": 0.0,
    },
}


def _dispatch_classification(body: str, participants):
    """Инжектируемый `classify_fn`: маппит тело правки → реалистичную классификацию.

    Возвращает полный dict (как `classify_edit_llm`), со `scope_candidate` из
    ФАКТИЧЕСКИХ участников (групповая → company, 1:1 → series). Неизвестное тело →
    one-off (как graceful degrade), чтобы реплей не молчал на неучтённой правке."""
    for marker, res in _LLM_REPLAY.items():
        if marker in body:
            return {**res, "scope_candidate": fcl.scope_candidate(participants)}
    return {"durable": False, "type": "one-off", "subjects": [], "rule": "",
            "confidence": 0.0, "scope_candidate": fcl.scope_candidate(participants)}


class _Base(unittest.TestCase):
    """Песочница каталога обучения + мок привязки серия→компания (как Ф2/Ф3)."""

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

    # --- живой интейк reissue_one на success (детерм. + LLM-остаток на одном батче) ---
    def _replay_batch(self, series, edits, participants, *, classify_fn=None):
        """Прогон батча контент-правок через ОБА интейка, как `reissue_one` на success.

        `edits` — list of {"author","text"} (контентные правки перевыпуска). Сначала
        детерминированный `record_learning_from_edits` (терм/коннектор), затем
        LLM-остаток `classify_remainder_edits` (различение/смысл) — ровно порядок и
        входы боевого `feedback_reissue.reissue_one`."""
        state = {"series": series, "date": "2026-06-22", "round": 1,
                 "feedback_id": feedback_state.build_feedback_id(series, "2026-06-22", -1)}
        meta = {"expectedParticipants": participants}
        fl.record_learning_from_edits(state, edits, meta=meta, root=self.root)
        fcl.classify_remainder_edits(
            state, edits, meta=meta,
            classify_fn=(classify_fn or _dispatch_classification), root=self.root)
        return state

    # --- утилиты-ассерты ---
    def _has_distinction(self, rules, a, b):
        key = {a.casefold(), b.casefold()}
        return any({(r.get("subject_a") or "").casefold(),
                    (r.get("subject_b") or "").casefold()} == key for r in rules)

    def _has_meaning(self, rules, subject_substr, rule_substr):
        return any(subject_substr.casefold() in (r.get("subject") or "").casefold()
                   and rule_substr.casefold() in (r.get("meaning") or "").casefold()
                   for r in rules)

    def _has_term(self, rules, wrong, right):
        return any((r.get("wrong") or "").casefold() == wrong.casefold()
                   and (r.get("right") or "").casefold() == right.casefold()
                   for r in rules)


# ===========================================================================
# R15 — боевой реплей форензики 2026-06-22: оба ценных смысловых правила durable
# ===========================================================================
class R15ForensicsReplayTest(_Base):
    SERIES = "mpervyi-pn-koord-finplan"

    # Репрезентативный батч 2026-06-22 (групповая координация). Порядок — как мог бы
    # прийти пачкой от владельца (Wispr Flow). 2 терм-пары + 2 смысловых + 3 негатива.
    FORENSICS_EDITS = [
        {"author": "Илья Рыбалка", "text": "замени MLAB на MLOVE"},
        {"author": "Илья Рыбалка", "text": "ДЭК → СДЭК"},
        {"author": "Илья Рыбалка", "text": "не Dream Story, а 23МПКТК, это разные разделы"},
        {"author": "Илья Рыбалка", "text": "вывод с ИП, чтобы не писал СП"},
        {"author": "Илья Рыбалка", "text": "убери этот абзац про кофе"},
        {"author": "Илья Рыбалка", "text": "сократи раздел про планы"},
        {"author": "Илья Рыбалка", "text": "поправь дату встречи на 23 июня"},
    ]

    def test_replay_learns_both_valuable_semantic_rules_binary(self):
        """🎯 R15 (бинарно): durable содержит И различение Dream Story≠23МПКТК,
        И смысл «вывод с ИП» — оба ценных правила, не только 2 терм-пары."""
        self._replay_batch(self.SERIES, self.FORENSICS_EDITS, GROUP)

        # Групповая встреча → смысл/различение на уровне КОМПАНИИ (mpervyi).
        dist = fl.active_company_distinction_rules(COMPANY, root=self.root)
        mean = fl.active_company_meaning_rules(COMPANY, root=self.root)
        self.assertTrue(self._has_distinction(dist, "Dream Story", "23МПКТК"),
                        f"различение Dream Story≠23МПКТК не выучено: {dist}")
        self.assertTrue(self._has_meaning(mean, "вывод", "ИП"),
                        f"смысл «вывод с ИП» не выучен: {mean}")

    def test_replay_term_pairs_still_learned_deterministically(self):
        """2 чистые терм-пары (MLAB→MLOVE, ДЭК→СДЭК) учатся детерминированным путём,
        как и до ISS-19 (per-series терм-правила)."""
        self._replay_batch(self.SERIES, self.FORENSICS_EDITS, GROUP)
        terms = fl.active_term_rules(self.SERIES, root=self.root)
        self.assertTrue(self._has_term(terms, "MLAB", "MLOVE"), f"MLAB→MLOVE: {terms}")
        self.assertTrue(self._has_term(terms, "ДЭК", "СДЭК"), f"ДЭК→СДЭК: {terms}")

    def test_replay_one_off_edits_stay_one_off(self):
        """Негативы-оракул («убери абзац», «сократи», «поправь дату») НЕ становятся
        durable ни в одном сторе (мусор не отравляет словарь)."""
        self._replay_batch(self.SERIES, self.FORENSICS_EDITS, GROUP)
        # Ни в company-, ни в series-сторе нет правил от разовых правок (про кофе/планы/дату).
        all_company = (fl.active_company_meaning_rules(COMPANY, root=self.root)
                       + fl.active_company_distinction_rules(COMPANY, root=self.root)
                       + fl.active_company_guidance_rules(COMPANY, root=self.root))
        all_series = (fl.active_meaning_rules(self.SERIES, root=self.root)
                      + fl.active_distinction_rules(self.SERIES, root=self.root)
                      + fl.active_guidance_rules(self.SERIES, root=self.root))
        blob = repr(all_company + all_series).casefold()
        for junk in ("кофе", "сократи", "дату", "23 июня", "планы"):
            self.assertNotIn(junk.casefold(), blob,
                             f"разовая правка просочилась в durable: {junk}")
        # Ровно одно различение + одно смысловое (ничего лишнего сверх ценных двух).
        self.assertEqual(len(fl.active_company_distinction_rules(COMPANY, root=self.root)), 1)
        self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)

    def test_replay_rules_reach_next_company_meeting_prompt(self):
        """Выученное РЕАЛЬНО влияет на следующую встречу: company-различение и смысл
        со-рендерятся в блок выученного для серии этой компании (R15 «влияет»)."""
        self._replay_batch(self.SERIES, self.FORENSICS_EDITS, GROUP)
        block = fl.format_learned_terms_block(self.SERIES, root=self.root)
        self.assertIn("23МПКТК", block)
        self.assertIn("Dream Story", block)
        self.assertIn("ИП", block)

    def test_replay_on_one_to_one_keeps_semantic_private_to_series(self):
        """Тот же набор на ЛИЧНОЙ 1:1 встрече → смысл/различение остаются на уровне
        СЕРИИ (приватность 1:1 не протекает на компанию). Контроль scope по типу встречи."""
        self._replay_batch("lichnaya-1-1", self.FORENSICS_EDITS, ONE_TO_ONE)
        self.assertEqual(fl.active_company_distinction_rules(COMPANY, root=self.root), [])
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])
        self.assertTrue(self._has_distinction(
            fl.active_distinction_rules("lichnaya-1-1", root=self.root),
            "Dream Story", "23МПКТК"))
        self.assertTrue(self._has_meaning(
            fl.active_meaning_rules("lichnaya-1-1", root=self.root), "вывод", "ИП"))


# ===========================================================================
# РИСК3 — калибровка порога `_DURABLE_MIN_CONFIDENCE` (оракул pass/cut)
# ===========================================================================
class ThresholdCalibrationOracleTest(_Base):
    """Оракул калибровки: «вывод с ИП» ПРОХОДИТ дефолт-порог; «убери абзац»/«сократи»
    НЕ проходят (durable=false); неуверенная durable-классификация режется порогом.
    Итог калибровки: порог = 0.6 (см. handoff §3 + docs)."""
    SERIES = "mpervyi-pn-koord-finplan"

    def _classify_one(self, text, classification):
        cf = lambda b, p: {**classification, "scope_candidate": fcl.scope_candidate(p)}
        self._replay_batch(self.SERIES, [{"author": "Илья", "text": text}], GROUP,
                           classify_fn=cf)

    def test_default_threshold_value_is_calibrated_06(self):
        """Калиброванное значение порога = 0.6 (осторожный дефолт A4, env-override)."""
        self.assertEqual(fl._DURABLE_MIN_CONFIDENCE, 0.6)

    def test_oracle_vyvod_s_ip_passes_default_threshold(self):
        """🎯 Оракул PASS: «вывод с ИП» при реалистичном боевом confidence (0.82)
        проходит дефолт-порог 0.6 → пишется в durable (R15 не зарублен порогом)."""
        self._classify_one("вывод с ИП, чтобы не писал СП", _LLM_REPLAY["вывод с ИП"])
        comp = fl.active_company_meaning_rules(COMPANY, root=self.root)
        self.assertEqual(len(comp), 1)
        self.assertIn("ИП", comp[0].get("meaning", ""))

    def test_oracle_distinction_passes_default_threshold(self):
        """Оракул PASS: различение Dream Story≠23МПКТК (conf 0.9) проходит порог."""
        self._classify_one("не Dream Story, а 23МПКТК",
                           _LLM_REPLAY["не Dream Story, а 23МПКТК"])
        self.assertEqual(len(fl.active_company_distinction_rules(COMPANY, root=self.root)), 1)

    def test_oracle_junk_edits_stay_one_off(self):
        """🎯 Оракул CUT: «убери абзац» / «сократи» классифицируются durable=false →
        не пишутся в durable ни при каком пороге (отсев мусора ДО порога)."""
        for text in ("убери этот абзац про кофе", "сократи раздел про планы"):
            self._classify_one(text, _LLM_REPLAY[text])
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])
        self.assertEqual(fl.active_company_guidance_rules(COMPANY, root=self.root), [])
        self.assertEqual(fl.active_company_distinction_rules(COMPANY, root=self.root), [])

    def test_threshold_actually_gates_uncertain_durable(self):
        """Порог — НЕ декоративный: неуверенная durable-классификация (conf 0.5 < 0.6)
        режется и остаётся one-off. Доказывает, что 0.6 реально отделяет уверенное
        правило от спорного (вторая линия защиты поверх durable=false)."""
        uncertain = {"durable": True, "type": "meaning", "subjects": ["маркетинг"],
                     "rule": "что-то про продвижение", "confidence": 0.5}
        self._classify_one("тут вроде про маркетинг речь шла", uncertain)
        self.assertEqual(fl.active_company_meaning_rules(COMPANY, root=self.root), [])

    def test_calibration_window_holds_both_conditions(self):
        """Окно калибровки: порог ниже реалистичного «вывод с ИП» (0.82) и выше серой
        зоны (≤0.55). 0.6 ∈ (0.55, 0.82] — оба условия оракула выполнимы одновременно.

        Доказываем границы прямым прогоном по сторам, не только сравнением чисел:
        на пороге 0.6 conf=0.82 пишется, conf=0.55 — нет."""
        self.assertGreater(fl._DURABLE_MIN_CONFIDENCE, 0.55)   # выше серой зоны
        self.assertLessEqual(fl._DURABLE_MIN_CONFIDENCE, 0.82)  # ниже боевого «вывод с ИП»
        # conf=0.82 → пишется.
        self._classify_one("вывод с ИП, чтобы не писал СП",
                           {**_LLM_REPLAY["вывод с ИП"], "confidence": 0.82})
        self.assertEqual(len(fl.active_company_meaning_rules(COMPANY, root=self.root)), 1)

    def test_threshold_env_override_recalibratable(self):
        """Порог калибруется БЕЗ передеплоя кода — env `FEEDBACK_LLM_DURABLE_MIN_CONFIDENCE`
        читается в значение модуля при загрузке. Подтверждаем РЕАЛЬНУЮ механику reload'ом,
        но restore делаем ВНЕ patched-env (try/finally) — иначе перетёрли бы дефолт 0.6
        для всего остального сьюта (поллюция глобального состояния)."""
        import importlib
        try:
            with mock.patch.dict(os.environ, {"FEEDBACK_LLM_DURABLE_MIN_CONFIDENCE": "0.75"}):
                reloaded = importlib.reload(fl)
                self.assertEqual(reloaded._DURABLE_MIN_CONFIDENCE, 0.75)
        finally:
            # env уже восстановлен (вышли из with) → reload вернёт дефолт 0.6.
            importlib.reload(fl)
        self.assertEqual(fl._DURABLE_MIN_CONFIDENCE, 0.6)


# ===========================================================================
# R14 — регресс старого обучения (терм-пары / роли / коннекторы / маркер «везде»)
# ===========================================================================
class R14OldLearningRegressionTest(_Base):
    """Аддитивные изменения Ф1-Ф4 НЕ сломали детерминированные пути обучения.
    Эти пути — чистые регекспы, флаг LLM их не трогает; проверяем, что учатся как
    до ISS-19 (терм-пары, роли, 3 коннектора смысла, глобальный маркер «везде»)."""

    def _learn(self, series, text, meta=None):
        state = {"series": series, "date": "2026-06-22", "round": 1,
                 "feedback_id": feedback_state.build_feedback_id(series, "2026-06-22", -1)}
        return fl.record_learning_from_edits(
            state, [{"author": "Илья", "text": text}], meta=meta, root=self.root)

    # --- терм-пары ---
    def test_term_pair_rsya_rec_learned_as_before(self):
        self._learn("coord", "РСЯ → РЕЦ")
        self.assertTrue(self._has_term(
            fl.active_term_rules("coord", root=self.root), "РСЯ", "РЕЦ"))

    def test_term_pair_zameni_form(self):
        self._learn("coord", "замени РСЯ на РЕЦ")
        self.assertTrue(self._has_term(
            fl.active_term_rules("coord", root=self.root), "РСЯ", "РЕЦ"))

    def test_term_pair_renders_to_prompt(self):
        self._learn("coord", "РСЯ → РЕЦ")
        block = fl.format_learned_terms_block("coord", root=self.root)
        self.assertIn("РСЯ", block)
        self.assertIn("РЕЦ", block)

    # --- роли (отдельная подсистема feedback_router, ISS-19 её не трогает) ---
    def test_role_extraction_unbroken(self):
        """Роль распознаётся регекспом `feedback_router` (путь не тронут ISS-19)."""
        self.assertEqual(fr.classify_edit("за сервис отвечает Михаил Саргин"), fr.KIND_ROLE)
        role = fr.extract_role("за сервис отвечает Михаил Саргин")
        self.assertEqual(role, {"name": "Михаил Саргин", "domain": "сервис"})

    def test_role_not_misrouted_to_series_term(self):
        """Роль приоритетнее терм/смысла — не уходит в series-term (порядок роутера)."""
        self.assertEqual(fr.classify_edit("Дарья ведёт HR"), fr.KIND_ROLE)

    # --- 3 определительных коннектора смысла ---
    def test_meaning_connector_dash_eto(self):
        self._learn("coord", "дельта — это разница факта и плана")
        self.assertTrue(self._has_meaning(
            fl.active_meaning_rules("coord", root=self.root), "дельта", "разница"))

    def test_meaning_connector_oznachaet(self):
        self._learn("coord", "ЭДО означает электронный документооборот")
        self.assertTrue(self._has_meaning(
            fl.active_meaning_rules("coord", root=self.root), "ЭДО", "электронный"))

    def test_meaning_connector_pod_ponimaetsya(self):
        self._learn("coord", "под отгрузкой понимается передача товара перевозчику")
        self.assertTrue(self._has_meaning(
            fl.active_meaning_rules("coord", root=self.root), "отгрузк", "передача"))

    def test_three_connectors_extract_directly(self):
        """Все 3 коннектора извлекаются экстрактором (узкий контракт Ф3б неизменен)."""
        self.assertEqual(len(fl.extract_meaning_rules("дельта — это разница")), 1)
        self.assertEqual(len(fl.extract_meaning_rules("ЭДО означает документооборот")), 1)
        self.assertEqual(
            len(fl.extract_meaning_rules("под отгрузкой понимается передача товара")), 1)

    # --- глобальный маркер «везде» (term-like → cross-company global) ---
    def test_global_marker_term_goes_global(self):
        self._learn("coord", "везде Зифренд пишется Zifriend")
        glob = fl.active_global_spellings(root=self.root)
        self.assertTrue(self._has_term(glob, "Зифренд", "Zifriend"),
                        f"глобальное написание по маркеру «везде» не выучено: {glob}")

    def test_global_marker_split_unbroken(self):
        is_global, body = fl._split_global_marker("везде Зифренд пишется Zifriend")
        self.assertTrue(is_global)
        self.assertEqual(body, "Зифренд пишется Zifriend")

    def test_plain_term_without_marker_stays_series(self):
        """Терм БЕЗ маркера остаётся per-series (в global не утекает) — как до ISS-19."""
        self._learn("coord", "РСЯ → РЕЦ")
        self.assertEqual(fl.active_global_spellings(root=self.root), [])
        self.assertEqual(len(fl.active_term_rules("coord", root=self.root)), 1)


# ===========================================================================
# R6 — Опасная тройка на ЖИВОМ пути классификации остатка
# ===========================================================================
class R6DangerTripletLivePathTest(_Base):
    """На живом пути `classify_remainder_edits`→`record_classified_rule` текст
    правки/транскрипта НЕ уходит в логи — только метаданные (счётчики/тип/conf)."""
    SERIES = "mpervyi-pn-koord-finplan"

    def test_classify_remainder_logs_no_edit_text(self):
        secret = "СЕКРЕТНО-вывод с ИП по закрытой сделке на 99 миллионов"
        cf = lambda b, p: {"durable": True, "type": "meaning", "subjects": ["вывод"],
                           "rule": secret, "confidence": 0.85,
                           "scope_candidate": fcl.scope_candidate(p)}
        with self.assertLogs("notary.feedback_classify_llm", level="INFO") as cm:
            self._replay_batch(self.SERIES, [{"author": "Илья", "text": secret}],
                               GROUP, classify_fn=cf)
        joined = "\n".join(cm.output)
        # Метаданные есть, текст правки — нет.
        self.assertIn("остаток=", joined)
        self.assertNotIn("СЕКРЕТНО", joined)
        self.assertNotIn("99 миллион", joined)

    def test_record_path_logs_no_edit_text(self):
        """Низлежащая запись (`record_classified_rule`) тоже не логирует текст правки —
        порог-reject и успешная запись несут только тип/conf/счётчики."""
        secret = "СЕКРЕТ-конфиденциальная формулировка вывода"
        with self.assertLogs("notary.lib.feedback_learning", level="INFO") as cm:
            fl.record_classified_rule(
                {"durable": True, "type": "meaning", "subjects": ["вывод"],
                 "rule": secret, "confidence": 0.1, "scope_candidate": "series"},
                series=self.SERIES, root=self.root)
        joined = "\n".join(cm.output)
        self.assertIn("conf=", joined)
        self.assertNotIn("СЕКРЕТ", joined)

    def test_no_transcript_in_classify_prompt(self):
        """Промпт классификатора несёт ТОЛЬКО правку + имена, без транскрипта/реплик
        (R6 на уровне сборки промпта — живой `build_classify_user_prompt`)."""
        prompt = fcl.build_classify_user_prompt(
            "вывод с ИП", ["Илья Рыбалка", "Мария Михина"])
        self.assertIn("вывод с ИП", prompt)
        self.assertIn("Илья Рыбалка", prompt)
        # Нет маркеров транскрипта/таймкодов/реплик.
        self.assertNotIn("[", prompt)  # таймкоды реплик «[00:12]» не подаются
        self.assertNotIn("SPEAKER", prompt.upper())


if __name__ == "__main__":
    unittest.main()
