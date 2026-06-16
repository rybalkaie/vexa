# -*- coding: utf-8 -*-
"""Ф3 (R13/R14): спокойная генерация + честный changelog.

R13 — детерминированный ПОСТ-ПРОХОД (`stabilize_protocol_text`): стабильная
сортировка участников + канонический порядок секций. НЕ температура/сэмплинг
(`claude --print` без флага — отвергнуто, РИСК1/A5). Бинарный критерий: две
регенерации одного транскрипта без правок → список участников и порядок секций
идентичны за счёт пост-прохода.

R14 — честный блок «🔁 Что изменилось»: перестановка участников/секций НЕ выдаётся
за замену/добавление человека (стабилизируем обе версии перед diff + явная дельта
участников по множеству).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import llm_postprocess as lp  # noqa: E402


# ---------------------------------------------------------------------------
# Хелперы сборки протокола из общих фрагментов (одинаковые ТЕЛА секций — чтобы
# тест проверял именно ПОРЯДОК, а не различия в содержимом).
# ---------------------------------------------------------------------------
def _header(participants: str) -> str:
    return (
        "#протоколвстречи 09.06.2026\n\n"
        "**Встреча:** Координация Anzhee.\n\n"
        "**Длительность:** 25 мин\n\n"
        f"**Участники:** {participants}\n\n"
        "**Транскрипт:** [2026-06-09.md](2026-06-09.md)\n\n"
        "---\n"
    )


_POSTAVKI = "▪️ Контейнер застрял на таможне, ждём документы."
_SKLAD = "▪️ Палетное хранение занято, ищем альтернативу."
_DECISIONS = "🔸 Переходим на еженедельный отчёт по поставкам."
_TASKS = "**Мария Михина**\n\n- Прислать документы по контейнеру до пятницы."


def _theme(num: int, title: str, body: str) -> str:
    return f"\n## {num}) {title}\n\n{body}\n"


def _svc(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body}\n"


# ===========================================================================
# R13 — стабильная сортировка участников
# ===========================================================================
class TestParticipantsStableSort(unittest.TestCase):
    def test_participants_sorted_and_deduped(self):
        txt = _header("Сона Енгибарян, Мария Михина, сона енгибарян") + _theme(
            1, "Поставки", _POSTAVKI
        )
        out = lp.stabilize_protocol_text(txt)
        # casefold-сортировка + дедуп (повтор «сона …» отброшен).
        self.assertIn("**Участники:** Мария Михина, Сона Енгибарян", out)
        self.assertNotIn("сона енгибарян", out)

    def test_no_participants_line_is_noop_for_header(self):
        txt = "#протоколвстречи 01.01.2026\n\n## 1) Тема\n\n▪️ Пункт.\n"
        # Нет шапки участников — пост-проход не должен падать/портить.
        self.assertEqual(lp.stabilize_protocol_text(txt), txt)


# ===========================================================================
# R13 — канонический порядок секций
# ===========================================================================
class TestSectionCanonicalOrder(unittest.TestCase):
    def test_service_sections_go_to_canonical_tail(self):
        txt = (
            _header("Илья Рыбалка")
            + _svc("Задачи", _TASKS)
            + _theme(1, "Поставки", _POSTAVKI)
            + _svc("✅ Решения", _DECISIONS)
        )
        out = lp.stabilize_protocol_text(txt)
        i_theme = out.index("## 1) Поставки")
        i_dec = out.index("## ✅ Решения")
        i_task = out.index("## Задачи")
        # Тематическая → Решения → Задачи (как в методичке), несмотря на вход.
        self.assertLess(i_theme, i_dec)
        self.assertLess(i_dec, i_task)

    def test_thematic_sorted_by_number_and_renumbered(self):
        txt = (
            _header("Илья Рыбалка")
            + _theme(2, "Склад", _SKLAD)
            + _theme(1, "Поставки", _POSTAVKI)
        )
        out = lp.stabilize_protocol_text(txt)
        self.assertLess(out.index("## 1) Поставки"), out.index("## 2) Склад"))

    def test_review_block_stays_last(self):
        txt = (
            _header("Илья Рыбалка")
            + _theme(1, "Поставки", _POSTAVKI)
            + _svc("⚠️ Проверить", "▪️ проверь: сумма не сходится.")
            + _svc("Задачи", _TASKS)
        )
        out = lp.stabilize_protocol_text(txt)
        self.assertLess(out.index("## Задачи"), out.index("## ⚠️ Проверить"))


# ===========================================================================
# R13 — БИНАРНЫЙ критерий: две «регенерации» → идентичный состав/порядок
# ===========================================================================
class TestTwoRegenerationsIdentical(unittest.TestCase):
    def test_permuted_outputs_become_identical(self):
        # Два LLM-выхода одного транскрипта: то же содержимое, но участники и
        # секции в РАЗНОМ порядке (именно это «плавание» убирает пост-проход).
        run_a = (
            _header("Сона Енгибарян, Мария Михина")
            + _theme(2, "Склад", _SKLAD)
            + _theme(1, "Поставки", _POSTAVKI)
            + _svc("Задачи", _TASKS)
            + _svc("Решения", _DECISIONS)
        )
        run_b = (
            _header("Мария Михина, Сона Енгибарян")
            + _theme(1, "Поставки", _POSTAVKI)
            + _theme(2, "Склад", _SKLAD)
            + _svc("Решения", _DECISIONS)
            + _svc("Задачи", _TASKS)
        )
        self.assertNotEqual(run_a, run_b)  # вход реально различается
        self.assertEqual(
            lp.stabilize_protocol_text(run_a),
            lp.stabilize_protocol_text(run_b),
        )

    def test_idempotent(self):
        txt = (
            _header("Сона Енгибарян, Мария Михина")
            + _theme(2, "Склад", _SKLAD)
            + _svc("Задачи", _TASKS)
            + _theme(1, "Поставки", _POSTAVKI)
        )
        once = lp.stabilize_protocol_text(txt)
        self.assertEqual(once, lp.stabilize_protocol_text(once))

    def test_header_and_disclaimer_preserved_verbatim(self):
        body = _theme(1, "Поставки", _POSTAVKI) + _svc("Задачи", _TASKS)
        with_disc = lp.protocol_to_tg.insert_protocol_disclaimer(
            _header("Илья Рыбалка") + body
        )
        out = lp.stabilize_protocol_text(with_disc)
        # Всё ДО первой `## ` (шапка + дисклеймер) — на месте дословно.
        head_in = with_disc.split("## ", 1)[0]
        head_out = out.split("## ", 1)[0]
        self.assertEqual(head_in, head_out)
        self.assertIn(lp.protocol_to_tg.PROTOCOL_DISCLAIMER_SENTINEL, out)


# ===========================================================================
# R13 — интеграция через generate_protocol (пост-проход реально подключён)
# ===========================================================================
_META = {
    "series": "ezhenedelnaya-koordinaciya-8399ea",
    "date": "2026-06-09",
    "duration": 25,
    "expectedParticipants": ["Мария Михина", "Сона Енгибарян"],
    "participants": ["Мария Михина", "Сона Енгибарян"],
}


class TestGenerateProtocolStabilizes(unittest.TestCase):
    def test_two_regenerations_identical_via_generate_protocol(self):
        run_a = (
            _header("Сона Енгибарян, Мария Михина")
            + _theme(2, "Склад", _SKLAD)
            + _theme(1, "Поставки", _POSTAVKI)
            + _svc("Задачи", _TASKS)
            + _svc("Решения", _DECISIONS)
        )
        run_b = (
            _header("Мария Михина, Сона Енгибарян")
            + _theme(1, "Поставки", _POSTAVKI)
            + _theme(2, "Склад", _SKLAD)
            + _svc("Решения", _DECISIONS)
            + _svc("Задачи", _TASKS)
        )
        with mock.patch.object(lp, "call_claude_print", side_effect=[run_a, run_b]):
            out1 = lp.generate_protocol("т", _META, method_text="М", meeting_sid="s1")
            out2 = lp.generate_protocol("т", _META, method_text="М", meeting_sid="s2")
        # Бинарный критерий R13: два прогона → идентичный список участников и
        # порядок секций (за счёт пост-прохода, не сэмплинга).
        self.assertEqual(out1, out2)
        self.assertIn("**Участники:** Мария Михина, Сона Енгибарян", out1)


# ===========================================================================
# R14 — дельта участников по множеству
# ===========================================================================
class TestParticipantDelta(unittest.TestCase):
    def test_pure_reorder_is_empty_delta(self):
        old = _header("Сона Енгибарян, Мария Михина")
        new = _header("Мария Михина, Сона Енгибарян")
        self.assertEqual(lp._participant_delta(old, new), ([], []))

    def test_real_rename_shows_added_removed(self):
        old = _header("Михаил Еремеев, Илья Рыбалка")
        new = _header("Михаил Саргин, Илья Рыбалка")
        added, removed = lp._participant_delta(old, new)
        self.assertEqual(added, ["Михаил Саргин"])
        self.assertEqual(removed, ["Михаил Еремеев"])


# ===========================================================================
# R14 — честный changelog: перестановка ≠ замена
# ===========================================================================
class TestHonestChangelog(unittest.TestCase):
    def test_reorder_only_yields_factory_fallback_without_calling_llm(self):
        # Старая и новая версии различаются ТОЛЬКО порядком участников/секций.
        old = (
            _header("Сона Енгибарян, Мария Михина")
            + _svc("Задачи", _TASKS)
            + _theme(1, "Поставки", _POSTAVKI)
        )
        new = (
            _header("Мария Михина, Сона Енгибарян")
            + _theme(1, "Поставки", _POSTAVKI)
            + _svc("Задачи", _TASKS)
        )
        called = []

        def _boom(*a, **k):
            called.append(1)
            return "🔁 ЗАМЕНИЛИ ЧЕЛОВЕКА"  # модель НЕ должна быть вызвана

        with mock.patch.object(lp, "call_claude_print", side_effect=_boom):
            summary = lp._compose_revision_summary(
                old, new, {"series": "coord", "date": "2026-06-09"}
            )
        # Перестановка → пустой diff после стабилизации → заводская строка,
        # модель не дёргали, ложного «заменили человека» нет.
        self.assertEqual(called, [])
        self.assertNotIn("ЗАМЕНИЛИ", summary)
        self.assertTrue(summary.startswith("🔁 Обновил протокол"))

    def test_real_change_passes_delta_note_to_llm(self):
        old = _header("Михаил Еремеев") + _theme(1, "Поставки", "▪️ Старый пункт.")
        new = _header("Михаил Саргин") + _theme(1, "Поставки", "▪️ Новый пункт после правки.")
        captured = {}

        def _fake(user_prompt, *, system, timeout, model):
            captured["user"] = user_prompt
            captured["system"] = system
            return "🔁 Имя автора уточнено."

        with mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            summary = lp._compose_revision_summary(
                old, new, {"series": "coord", "date": "2026-06-09"}
            )
        self.assertEqual(summary, "🔁 Имя автора уточнено.")
        # Явная дельта участников по множеству ушла в промпт.
        self.assertIn("добавлены: Михаил Саргин", captured["user"])
        self.assertIn("убраны: Михаил Еремеев", captured["user"])
        # И инструкция «перестановка ≠ изменение» в системном промпте.
        self.assertIn("Перестановка", captured["system"])


# ===========================================================================
# R14 — redeliver: чистая перестановка → no-change (ложного до-сыла нет)
# ===========================================================================
class TestRedeliverNoChangeOnReorder(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"ENABLE_PROTOCOL_DELIVERY": "1"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_permutation_is_no_change(self):
        old = (
            _header("Сона Енгибарян, Мария Михина")
            + _svc("Задачи", _TASKS)
            + _theme(1, "Поставки", _POSTAVKI)
        )
        new = (
            _header("Мария Михина, Сона Енгибарян")
            + _theme(1, "Поставки", _POSTAVKI)
            + _svc("Задачи", _TASKS)
        )
        sent = []
        with mock.patch.object(lp.telegram_api, "send_message",
                               lambda *a, **k: sent.append(1)):
            res = lp.redeliver_revised_protocol(
                {"series": "coord", "date": "2026-06-09"},
                old, new, meta_json_path=None, meeting_sid="x",
            )
        self.assertEqual(res["status"], "no-change")
        self.assertEqual(sent, [])  # в чат ничего не ушло


# ===========================================================================
# Ф3-сопровождение (цикл5): классификатор секций пост-прохода НЕ должен
# разойтись с разбором заголовков в series_memory. Оба модуля независимо хранят
# ключи служебных секций (решения/задачи/перенос/служебка); добавят ключ в один,
# забудут в другой → пост-проход поставит секцию НЕ туда, где её ждёт память
# серии. Тест ловит дрейф (класс бага auto-memory `review-checks-two-call-sites`).
# ===========================================================================
class TestClassifierAlignedWithSeriesMemory(unittest.TestCase):
    def test_section_keys_match_series_memory(self):
        from lib import series_memory as sm

        # Каждый служебный ключ series_memory обязан классифицироваться
        # пост-проходом в соответствующий неттематический ранг.
        cases = [
            (sm._DECISION_HEADING_KEYS, 2),
            (sm._TASK_HEADING_KEYS, 3),
            (sm._CARRYOVER_HEADING_KEYS, 4),
            (sm._SERVICE_HEADING_KEYS, 5),
        ]
        for keys, expected_rank in cases:
            for k in keys:
                rank, _ = lp._classify_protocol_section(f"## {k}")
                self.assertEqual(
                    rank, expected_rank,
                    f"ключ {k!r} из series_memory → ранг {rank} в пост-проходе, "
                    f"ожидался {expected_rank}: ключи служебных секций разошлись "
                    f"между _classify_protocol_section и series_memory._classify_heading",
                )


if __name__ == "__main__":
    unittest.main()
