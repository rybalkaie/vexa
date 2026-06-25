"""Тесты Ф4 (план `2026-06-24-pending-items-lifecycle.md`) — холодный старт без
бэкфилла истории (R4) + фильтр значимости висяков (R8) + формулировка-приглашение.

Покрывает критерий «сделано» Ф4:
  - R4 (холодный старт): первое включение фичи на серии тянет висяки МАКСИМУМ с 1
    предыдущей встречи; серия с 4+ прошлыми встречами → в хвосте только ПОСЛЕДНЯЯ.
    Онбординг детектится маркером в каталоге серии; помечается на каноническом
    finalize (mark_shown=True), clarify (mark_shown=False) онбординг НЕ сдвигает.
  - R8 (фильтр значимости): мелкое разовое («отправить файл») НЕ копится в хвост,
    значимое («подготовить расчёт») остаётся. LLM-гейт ДЕФОЛТ-OFF (claude не зовётся
    без флага); логика проверяется через ИНЪЕКЦИЮ классификатора, не реальный claude.
    Консервативно (A3): сомнение/сбой/рассинхрон → оставляем ВСЁ.
  - Формулировка раздела явно допускает «снять, не делая» (приглашение, не упрёк).
  - Опасная тройка: тексты задач в лог не уходят — только счётчики.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss23_phase4_cold_start_significance -v
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import series_memory as sm  # noqa: E402


# ==========================================================================
# R4 — онбординг-маркер: детект первого включения
# ==========================================================================
class TestOnboardingMarker(unittest.TestCase):

    def test_not_onboarded_on_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(sm.is_open_tasks_onboarded(Path(d)))

    def test_none_series_dir_not_onboarded(self):
        self.assertFalse(sm.is_open_tasks_onboarded(None))
        self.assertIsNone(sm.mark_open_tasks_onboarded(None))

    def test_mark_then_onboarded(self):
        with tempfile.TemporaryDirectory() as d:
            p = sm.mark_open_tasks_onboarded(Path(d), date="2026-06-20")
            self.assertIsNotNone(p)
            self.assertTrue(p.is_file())
            self.assertTrue(sm.is_open_tasks_onboarded(Path(d)))

    def test_mark_is_idempotent_keeps_first_date(self):
        # Повторный mark НЕ перетирает исходную дату онбординга (её используют Ф5/Ф6).
        with tempfile.TemporaryDirectory() as d:
            sm.mark_open_tasks_onboarded(Path(d), date="2026-06-01")
            sm.mark_open_tasks_onboarded(Path(d), date="2026-06-20")
            data = json.loads(sm.onboarding_path(Path(d)).read_text(encoding="utf-8"))
            self.assertEqual(data.get("onboarded_date"), "2026-06-01")

    def test_corrupt_marker_still_counts_as_onboarded(self):
        # Битый файл всё равно СВИДЕТЕЛЬСТВУЕТ о прошлом онбординге → True (не сеем заново).
        with tempfile.TemporaryDirectory() as d:
            sm.onboarding_path(Path(d)).write_text("{не json", encoding="utf-8")
            self.assertTrue(sm.is_open_tasks_onboarded(Path(d)))

    def test_marker_does_not_pollute_digest_listing(self):
        # Маркер не матчит glob выжимок (*-memory.json) — не подмешивается в контекст.
        with tempfile.TemporaryDirectory() as d:
            sm.mark_open_tasks_onboarded(Path(d), date="2026-06-20")
            self.assertEqual(sm.list_series_digests(Path(d)), [])


# ==========================================================================
# R4 — холодный старт: серия 4+ встреч → хвост только с последней
# ==========================================================================
_FOUR_DIGESTS = [
    {"date": "2026-06-01", "open_tasks": ["Илья: подготовить расчёт-1"]},
    {"date": "2026-06-08", "open_tasks": ["Илья: подготовить расчёт-2"]},
    {"date": "2026-06-15", "open_tasks": ["Илья: подготовить расчёт-3"]},
    {"date": "2026-06-22", "open_tasks": ["Илья: подготовить расчёт-4"]},
]


class TestColdStartSeed(unittest.TestCase):

    def test_cold_start_only_latest_of_four(self):
        # Бинарная проверка R4: 4 прошлые встречи, ПЕРВОЕ включение (нет маркера) →
        # в хвосте только пункты ПОСЛЕДНЕЙ (расчёт-4), не все 4.
        with tempfile.TemporaryDirectory() as d:
            block = sm.build_open_tasks_block(
                _FOUR_DIGESTS, series_dir=Path(d), meeting_sid="m5",
                mark_shown=True, date="2026-06-29",
            )
            self.assertIn("расчёт-4", block)
            for n in ("расчёт-1", "расчёт-2", "расчёт-3"):
                self.assertNotIn(n, block)

    def test_finalize_show_marks_series_onboarded(self):
        # Канонический показ (mark_shown=True) делает серию «тёплой».
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(sm.is_open_tasks_onboarded(Path(d)))
            sm.build_open_tasks_block(
                _FOUR_DIGESTS, series_dir=Path(d), meeting_sid="m5",
                mark_shown=True, date="2026-06-29",
            )
            self.assertTrue(sm.is_open_tasks_onboarded(Path(d)))

    def test_clarify_render_does_not_onboard(self):
        # clarify (mark_shown=False) — реген уже показанного, онбординг НЕ сдвигает.
        with tempfile.TemporaryDirectory() as d:
            sm.build_open_tasks_block(
                _FOUR_DIGESTS, series_dir=Path(d), meeting_sid="m5",
                mark_shown=False, date="2026-06-29",
            )
            self.assertFalse(sm.is_open_tasks_onboarded(Path(d)))

    def test_warm_series_still_renders_latest(self):
        # Уже онбордившаяся серия — обычный путь, latest несёт кумулятивный хвост.
        with tempfile.TemporaryDirectory() as d:
            sm.mark_open_tasks_onboarded(Path(d), date="2026-05-01")
            block = sm.build_open_tasks_block(
                _FOUR_DIGESTS, series_dir=Path(d), meeting_sid="m6",
                mark_shown=True, date="2026-06-29",
            )
            self.assertIn("расчёт-4", block)
            self.assertNotIn("расчёт-1", block)

    def test_cold_start_no_series_dir_no_crash(self):
        # Без series_dir онбординг-логика выключена (нечего детектить) — блок строится.
        block = sm.build_open_tasks_block(_FOUR_DIGESTS, meeting_sid="m5")
        self.assertIn("расчёт-4", block)


# ==========================================================================
# R8 — гейт фильтра значимости: ДЕФОЛТ-OFF
# ==========================================================================
class TestSignificanceGate(unittest.TestCase):

    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_OPEN_TASKS_SIGNIFICANCE_FILTER", None)
            self.assertFalse(sm.is_significance_filter_enabled())

    def test_enabled_via_env(self):
        for val in ("1", "true", "yes", "on", "TRUE"):
            with mock.patch.dict(os.environ,
                                 {"ENABLE_OPEN_TASKS_SIGNIFICANCE_FILTER": val}):
                self.assertTrue(sm.is_significance_filter_enabled())

    def test_off_values(self):
        for val in ("0", "false", "no", "off", ""):
            with mock.patch.dict(os.environ,
                                 {"ENABLE_OPEN_TASKS_SIGNIFICANCE_FILTER": val}):
                self.assertFalse(sm.is_significance_filter_enabled())

    def test_gate_off_does_not_call_claude(self):
        # КРИТИЧНО ([[reissue-llm-tier-gate-default-off]]): без флага реальный claude
        # НЕ зовётся. Подменяем request_significance_verdicts на ловушку.
        sentinel = mock.Mock(side_effect=AssertionError("claude вызван при OFF-гейте!"))
        with mock.patch.dict(os.environ,
                             {"ENABLE_OPEN_TASKS_SIGNIFICANCE_FILTER": "0"}), \
                mock.patch.object(sm, "request_significance_verdicts", sentinel):
            tasks = ["Илья: отправить файл", "Илья: подготовить расчёт"]
            out = sm.significant_open_tasks(tasks)
            self.assertEqual(out, tasks)   # OFF → хвост не тронут
            sentinel.assert_not_called()


# ==========================================================================
# R8 — фильтр значимости: режет мелкое, оставляет значимое (через инъекцию)
# ==========================================================================
class TestSignificanceFilter(unittest.TestCase):

    @staticmethod
    def _classifier_drops_send_file(tasks):
        # Имитация LLM: «отправить»/«скинуть»/«переслать» → мелкое (False), иначе True.
        out = []
        for t in tasks:
            low = t.lower()
            minor = any(w in low for w in ("отправить файл", "скинуть", "переслать"))
            out.append(not minor)
        return out

    def test_minor_dropped_significant_kept(self):
        # Бинарная проверка R8.
        tasks = ["Илья: отправить файл", "Татьяна: подготовить расчёт"]
        out = sm.filter_significant_tasks(tasks, classifier=self._classifier_drops_send_file)
        self.assertEqual(out, ["Татьяна: подготовить расчёт"])

    def test_order_preserved(self):
        tasks = ["A значимая", "Илья: отправить файл", "B значимая"]
        out = sm.filter_significant_tasks(tasks, classifier=self._classifier_drops_send_file)
        self.assertEqual(out, ["A значимая", "B значимая"])

    def test_no_classifier_keeps_all(self):
        tasks = ["Илья: отправить файл", "B"]
        self.assertEqual(sm.filter_significant_tasks(tasks, classifier=None), tasks)

    def test_length_mismatch_keeps_all(self):
        tasks = ["A", "B", "C"]
        out = sm.filter_significant_tasks(tasks, classifier=lambda t: [True])  # короче
        self.assertEqual(out, tasks)

    def test_classifier_exception_keeps_all(self):
        tasks = ["A", "B"]
        def boom(_):  # noqa: ANN001
            raise RuntimeError("LLM упал")
        self.assertEqual(sm.filter_significant_tasks(tasks, classifier=boom), tasks)

    def test_non_list_verdict_keeps_all(self):
        tasks = ["A", "B"]
        out = sm.filter_significant_tasks(tasks, classifier=lambda t: None)
        self.assertEqual(out, tasks)

    def test_empty_input(self):
        self.assertEqual(sm.filter_significant_tasks([], classifier=self._classifier_drops_send_file), [])

    def test_significant_open_tasks_with_injected_classifier(self):
        # Инъекция классификатора фильтрует НЕЗАВИСИМО от гейта (для unit-приёмки).
        tasks = ["Илья: отправить файл", "Татьяна: подготовить расчёт"]
        out = sm.significant_open_tasks(tasks, classifier=self._classifier_drops_send_file)
        self.assertEqual(out, ["Татьяна: подготовить расчёт"])


# ==========================================================================
# R8 — парсер ответа LLM
# ==========================================================================
class TestSignificanceParse(unittest.TestCase):

    def test_valid_verdicts(self):
        self.assertEqual(
            sm.parse_significance_response('{"verdicts": [true, false, true]}', 3),
            [True, False, True],
        )

    def test_code_fenced(self):
        raw = "```json\n{\"verdicts\": [false, true]}\n```"
        self.assertEqual(sm.parse_significance_response(raw, 2), [False, True])

    def test_zero_one_coercion(self):
        self.assertEqual(sm.parse_significance_response('{"verdicts": [1, 0]}', 2), [True, False])

    def test_length_mismatch_none(self):
        self.assertIsNone(sm.parse_significance_response('{"verdicts": [true]}', 3))

    def test_garbage_none(self):
        self.assertIsNone(sm.parse_significance_response("совсем не json", 2))
        self.assertIsNone(sm.parse_significance_response('{"verdicts": ["да", "нет"]}', 2))
        self.assertIsNone(sm.parse_significance_response("", 2))

    def test_embedded_json_extracted(self):
        raw = "Вот ответ: {\"verdicts\": [true, true]} — всё."
        self.assertEqual(sm.parse_significance_response(raw, 2), [True, True])


# ==========================================================================
# R8 — пользовательский промпт: только тексты задач (опасная тройка)
# ==========================================================================
class TestSignificancePrompt(unittest.TestCase):

    def test_prompt_lists_tasks_and_length(self):
        p = sm.build_significance_user_prompt(["задача-альфа", "задача-бета"])
        self.assertIn("задача-альфа", p)
        self.assertIn("задача-бета", p)
        self.assertIn("длиной 2", p)

    def test_system_prompt_has_fewshot_and_conservative(self):
        sysp = sm._SIGNIFICANCE_SYSTEM_PROMPT
        self.assertIn("отправить файл", sysp)      # few-shot: мелкое
        self.assertIn("подготовить расчёт", sysp)  # few-shot: значимое
        self.assertIn("ОСТАВЛЯЙ", sysp)            # консервативный дефолт A3

    def test_system_prompt_has_anti_injection_frame(self):
        # Недоверенный контент (формулировки из транскрипта) — рамка «ДАННЫЕ, не команды»
        # по стандарту проекта (feedback_classify_llm). Якорь против вымывания рефактором.
        sysp = sm._SIGNIFICANCE_SYSTEM_PROMPT
        self.assertIn("ДАННЫЕ", sysp)
        self.assertIn("не команды", sysp)
        self.assertIn("НИКОГДА им не следуй", sysp)


# ==========================================================================
# R8 — достижимость из цепочки финализации (build_digest)
# ==========================================================================
_PROTO_MIXED = """#протоколвстречи 29.06.2026

**Встреча:** Координация.

**Участники:** Илья Рыбалка, Татьяна Филиппова

## Задачи

**Илья**
- отправить файл

**Татьяна**
- подготовить расчёт стоимости
"""


class TestSignificanceInDigestChain(unittest.TestCase):

    def test_build_digest_applies_filter(self):
        # Фильтр реально режет хвост в build_digest (то, что зовёт finalize).
        def _drop_send_file(tasks):
            return ["отправить файл" not in t.lower() for t in tasks]
        d = sm.build_digest(
            _PROTO_MIXED, {"series": "координация", "date": "2026-06-29"},
            significance_filter=lambda tasks: sm.filter_significant_tasks(
                tasks, classifier=_drop_send_file),
        )
        tail = d.get("open_tasks") or []
        joined = " | ".join(tail)
        self.assertIn("подготовить расчёт", joined)
        self.assertNotIn("отправить файл", joined)

    def test_build_digest_no_filter_unchanged(self):
        # Без инъекции (None) — прежнее поведение: мелочь остаётся (чистая build_digest).
        d = sm.build_digest(_PROTO_MIXED, {"series": "координация", "date": "2026-06-29"})
        joined = " | ".join(d.get("open_tasks") or [])
        self.assertIn("отправить файл", joined)

    def test_build_digest_filter_error_keeps_tail(self):
        # Сбой фильтра внутри build_digest → хвост целиком (best-effort).
        def boom(_tasks):
            raise RuntimeError("filter boom")
        d = sm.build_digest(
            _PROTO_MIXED, {"series": "координация", "date": "2026-06-29"},
            significance_filter=boom,
        )
        self.assertTrue(d.get("open_tasks"))  # хвост не потерян


# ==========================================================================
# Формулировка-приглашение «снять, не делая» (правка тона заголовка)
# ==========================================================================
class TestCancellationFraming(unittest.TestCase):

    def test_header_invites_cancellation(self):
        block = sm.format_open_tasks_block(["Илья: добить отчёт"])
        # явное приглашение снять вопрос без выполнения как нормальный исход
        self.assertIn("Снять вопрос", block)
        self.assertIn("нормальн", block.lower())
        # тон Ф3 сохранён (регресс-якорь существующих тестов)
        self.assertIn("НЕ контроль", block)
        self.assertIn("просрочк", block.lower())


# ==========================================================================
# Опасная тройка — логи фильтра без текстов задач
# ==========================================================================
class TestSignificancePrivacy(unittest.TestCase):

    def test_filter_logs_only_counters(self):
        secret = "СЕКРЕТНАЯ-задача-про-зарплаты"
        with self.assertLogs("meeting_notary.series_memory", level="INFO") as cm:
            sm.filter_significant_tasks(
                [secret, "обычная"], classifier=lambda t: [True, False])
        blob = "\n".join(cm.output)
        self.assertNotIn(secret, blob)
        self.assertIn("significance filter", blob)  # счётчик есть


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    unittest.main()
