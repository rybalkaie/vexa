"""Тесты Ф3 (план `2026-06-24-pending-items-lifecycle.md`) — мягкое оформление
раздела висяков: тон, группировка по людям, мягкие сроки, подблок «под сомнением»,
подраздел «закрытые с прошлых встреч» (показ один раз).

Покрывает дословные критерии:
  - R3 — мягкий заголовок (вариант владельца) + подача-помощь, без давления;
  - R6 — срок только если он уже в тексте задачи, без маркеров просрочки;
  - R7 — группировка по ответственному (подзаголовок **Имя**), не плоский список;
  - R10 — «под сомнением» в ОТДЕЛЬНОМ подблоке, визуально отделён, не смешан с висящими;
  - R21 — подраздел «закрытые с прошлых встреч»: показ ОДИН раз с причиной, после
    показа `mark_status_shown` → повторно не выводится (список не копится).

Плюс round-trip-безопасность рендера (главная техническая тонкость Ф3): подразделы
«закрытые»/«под сомнением» при ПЕРЕНОСЕ через сгенерированный протокол не воскресают
как висящие (sidecar-ключ совпадает; группировка по людям сохраняет «Имя:»).

Реплей/рендер read-only, без сети/LLM, без боевого `.env.notary`.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss23_phase3_render -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import series_memory as sm  # noqa: E402
from lib import knowledge_distill as kd  # noqa: E402


def _hanging_part(block: str) -> str:
    """Часть 1 (висящие) — всё ДО инструкции Части 2 (подразделы)."""
    return block.split("ЧАСТЬ 2")[0]


# ==========================================================================
# R3 — мягкий заголовок и подача
# ==========================================================================
class TestSoftToneR3(unittest.TestCase):

    def test_soft_heading_no_alarm_emoji(self):
        block = sm.format_open_tasks_block(["Илья: добить отчёт"])
        self.assertIn(f"## {sm.PENDING_SECTION_HEADING}", block)
        # вариант владельца из ISS-23
        self.assertEqual(sm.PENDING_SECTION_HEADING,
                         "Вопросы с прошлых встреч, по которым не ясен статус")
        # старого «алармового» 🔻 в заголовке больше нет
        self.assertNotIn("🔻", block)

    def test_helper_framing_not_control(self):
        block = sm.format_open_tasks_block(["Илья: добить отчёт"])
        self.assertIn("помощь", block.lower())
        self.assertIn("НЕ контроль", block)
        # явный запрет давящих формулировок в инструкции
        self.assertIn("просрочк", block.lower())   # «БЕЗ … просрочка» — запрет, не маркер

    def test_empty_is_blank(self):
        self.assertEqual(sm.format_open_tasks_block([]), "")
        self.assertEqual(sm.format_open_tasks_block([], doubt=[], closed=[]), "")


# ==========================================================================
# R7 — группировка по ответственному
# ==========================================================================
class TestGroupByOwnerR7(unittest.TestCase):

    def test_grouped_subheadings_present(self):
        block = sm.format_open_tasks_block(
            ["Татьяна: A-задача", "Илья: B-задача", "Татьяна: C-задача"])
        self.assertIn("**Татьяна**", block)
        self.assertIn("**Илья**", block)
        # имя в строке не повторяется (срезано в подзаголовок)
        self.assertIn("- A-задача", block)
        self.assertIn("- C-задача", block)
        self.assertNotIn("- Татьяна: A-задача", block)

    def test_owner_grouping_collapses_same_person(self):
        nameless, owned = sm._group_open_by_owner(
            ["Татьяна: A", "Илья: B", "Татьяна: C"])
        self.assertEqual(nameless, [])
        self.assertEqual([o for o, _ in owned], ["Татьяна", "Илья"])
        tat = dict(owned)["Татьяна"]
        self.assertEqual(tat, ["A", "C"])

    def test_nameless_first_without_subheading(self):
        block = sm.format_open_tasks_block(["сделать общий аудит", "Илья: B-задача"])
        nameless, owned = sm._group_open_by_owner(["сделать общий аудит", "Илья: B-задача"])
        self.assertEqual(nameless, ["сделать общий аудит"])
        # безымянная идёт строкой без **подзаголовка**, ПЕРЕД группой Ильи
        self.assertLess(block.index("сделать общий аудит"), block.index("**Илья**"))

    def test_not_flat_list_when_owners(self):
        block = sm.format_open_tasks_block(["Татьяна: A", "Илья: B"])
        # «не плоский список» = есть подзаголовки людей
        self.assertGreaterEqual(block.count("**"), 4)  # **Татьяна** + **Илья**


# ==========================================================================
# R6 — мягкие сроки
# ==========================================================================
class TestSoftDeadlinesR6(unittest.TestCase):

    def test_existing_deadline_kept(self):
        block = sm.format_open_tasks_block(["Илья: отчёт (срок: 15.06)"])
        self.assertIn("(срок: 15.06)", block)

    def test_no_deadline_not_invented(self):
        block = sm.format_open_tasks_block(["Илья: отчёт без срока"])
        # билдер не дописывает срок к задаче без него
        self.assertNotIn("(срок:", _hanging_part(block).split("Незакрытые задачи по людям:")[1])

    def test_no_overdue_markers_on_tasks(self):
        block = sm.format_open_tasks_block(["Илья: отчёт (срок: 01.01)"])
        # на самой задаче нет маркеров просрочки (есть только запрет в инструкции)
        task_area = block.split("Незакрытые задачи по людям:")[1]
        for bad in ("ПРОСРОЧКА", "просрочено", "опоздание", "🔴"):
            self.assertNotIn(bad, task_area)


# ==========================================================================
# R10 — «под сомнением» в отдельном подблоке
# ==========================================================================
class TestDoubtSubblockR10(unittest.TestCase):

    def test_doubt_subblock_separate(self):
        block = sm.format_open_tasks_block(
            ["Илья: висячая"],
            doubt=[{"text": "Татьяна: договор с типографией", "reason": "по чату"}])
        self.assertIn("🟡 Вроде закрыто — подтвердите", block)
        self.assertIn("- Татьяна: договор с типографией", block)
        # подблок «под сомнением» отделён от висящих (идёт в Части 2)
        self.assertNotIn("договор с типографией", _hanging_part(block))

    def test_doubt_not_silently_closed(self):
        # «под сомнением» НЕ в подразделе «закрытые» — это буфер, не закрытие
        block = sm.format_open_tasks_block(
            [], doubt=[{"text": "X-сомнение", "reason": None}])
        self.assertIn("🟡 Вроде закрыто — подтвердите", block)
        self.assertNotIn("✅ Закрыто", block)


# ==========================================================================
# R21 — подраздел «закрытые», показ один раз + причина
# ==========================================================================
class TestClosedSubsectionR21(unittest.TestCase):

    def test_closed_with_reason_label(self):
        block = sm.format_open_tasks_block(
            [], closed=[{"text": "Илья: отчёт", "status": sm.STATUS_DONE,
                         "reason": "по встрече"}])
        self.assertIn("✅ Закрыто с прошлых встреч", block)
        self.assertIn("сделано (по встрече)", block)
        self.assertIn("- Илья: отчёт", block)

    def test_status_label_mapping(self):
        self.assertEqual(sm._human_closed_label(sm.STATUS_DONE, "по встрече"),
                         "сделано (по встрече)")
        self.assertEqual(sm._human_closed_label(sm.STATUS_CANCELLED, None), "снято")
        self.assertEqual(sm._human_closed_label(sm.STATUS_AUTO_CLOSED, "по задаче"),
                         "закрыто автоматически (по задаче)")

    def test_closed_grouped_by_label(self):
        block = sm.format_open_tasks_block(
            [], closed=[
                {"text": "A", "status": sm.STATUS_DONE, "reason": "по встрече"},
                {"text": "B", "status": sm.STATUS_DONE, "reason": "по встрече"},
                {"text": "C", "status": sm.STATUS_CANCELLED, "reason": None},
            ])
        self.assertIn("_сделано (по встрече):_", block)
        self.assertIn("_снято:_", block)

    def test_shown_once_marks_and_disappears(self):
        digests = [{"date": "2026-06-08", "open_tasks": ["Илья: отчёт", "B-висит"]}]
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            sm.set_task_status(sd, "Илья: отчёт", sm.STATUS_DONE, reason="по встрече")
            # finalize-показ: mark_shown=True → закрытая показана и помечена.
            b1 = sm.build_open_tasks_block(digests, series_dir=sd, mark_shown=True)
            self.assertIn("✅ Закрыто с прошлых встреч", b1)
            self.assertIn("Илья: отчёт", b1)
            # следующий показ: merge перечитает sidecar → закрытой уже нет (показ один раз).
            b2 = sm.build_open_tasks_block(digests, series_dir=sd, mark_shown=True)
            self.assertNotIn("✅ Закрыто с прошлых встреч", b2)
            self.assertNotIn("Илья: отчёт", b2)
            self.assertIn("B-висит", b2)   # живой висяк остаётся

    def test_clarify_does_not_mark_shown(self):
        digests = [{"date": "2026-06-08", "open_tasks": ["Илья: отчёт"]}]
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            sm.set_task_status(sd, "Илья: отчёт", sm.STATUS_DONE)
            # clarify-реген: mark_shown=False (дефолт) → показывает, но НЕ помечает.
            b1 = sm.build_open_tasks_block(digests, series_dir=sd, mark_shown=False)
            self.assertIn("Илья: отчёт", b1)
            # статус всё ещё shown=False → следующий показ снова покажет (не потеряли).
            b2 = sm.build_open_tasks_block(digests, series_dir=sd, mark_shown=False)
            self.assertIn("Илья: отчёт", b2)


# ==========================================================================
# R10 — «под сомнением» показывается КАЖДЫЙ раз (не гейтится shown)
# ==========================================================================
class TestDoubtShownEveryTime(unittest.TestCase):

    def test_doubt_persists_across_builds(self):
        digests = [{"date": "2026-06-08", "open_tasks": ["Татьяна: смета"]}]
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            sm.set_task_status(sd, "Татьяна: смета", sm.STATUS_DOUBT, reason="по чату")
            b1 = sm.build_open_tasks_block(digests, series_dir=sd, mark_shown=True)
            b2 = sm.build_open_tasks_block(digests, series_dir=sd, mark_shown=True)
            for b in (b1, b2):
                self.assertIn("🟡 Вроде закрыто — подтвердите", b)
                self.assertIn("Татьяна: смета", b)
                # и НЕ как висящая
                self.assertNotIn("Татьяна: смета", _hanging_part(b))


# ==========================================================================
# Round-trip — рендер подразделов не воскрешает задачи при ПЕРЕНОСЕ
# ==========================================================================
class TestRoundTripSafety(unittest.TestCase):
    """Симулируем сгенерированный протокол (как его собрал бы LLM по инструкции
    билдера) и проверяем `extract_open_tasks` → `merge`: закрытые/сомнительные не
    воскресают как висящие, владелец из подзаголовка восстанавливается."""

    def test_owner_subheading_roundtrip(self):
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "**Татьяна**\n"
            "- согласовать смету (срок: 15.06) — висит\n"
            "- прислать макет — висит\n"
            "**Илья**\n"
            "- добить отчёт — висит\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertIn("Татьяна: согласовать смету (срок: 15.06)", carried)
        self.assertIn("Татьяна: прислать макет", carried)
        self.assertIn("Илья: добить отчёт", carried)

    def test_marker_resets_owner_no_cross_contamination(self):
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "**Татьяна**\n"
            "- согласовать смету — висит\n"
            "🟡 Вроде закрыто — подтвердите\n"
            "- Мария: бриф для дизайнера\n"
            "✅ Закрыто с прошлых встреч\n"
            "_сделано (по встрече):_\n"
            "- Илья: отчёт по выручке\n"
        )
        carried = sm.extract_open_tasks(proto)
        # владелец из подзаголовка применён только к своей задаче
        self.assertIn("Татьяна: согласовать смету", carried)
        # подразделы несут «Имя:» в строке — чужой подзаголовок к ним НЕ приклеен
        self.assertIn("Мария: бриф для дизайнера", carried)
        self.assertIn("Илья: отчёт по выручке", carried)
        self.assertFalse(any("Татьяна: Мария" in t for t in carried))
        self.assertFalse(any("Татьяна: Илья" in t for t in carried))

    def test_closed_not_resurrected_after_shown(self):
        # Закрытая задача, попавшая в подраздел «закрытые» и помеченная shown, при
        # ПЕРЕНОСЕ (она ещё в хвосте) НЕ воскресает: merge видит shown=True → ни в
        # open, ни в closed.
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            sm.set_task_status(sd, "Илья: отчёт по выручке", sm.STATUS_DONE,
                               reason="по встрече")
            sm.mark_status_shown(sd, "Илья: отчёт по выручке")  # как после finalize-показа
            # Сгенерированный протокол перенёс закрытую дословно (бэйр-буллет).
            proto = (
                f"## {sm.PENDING_SECTION_HEADING}\n"
                "**Татьяна**\n- смета — висит\n"
                "✅ Закрыто с прошлых встреч\n_сделано (по встрече):_\n"
                "- Илья: отчёт по выручке\n"
            )
            fresh = sm.extract_open_tasks(proto)
            # текст закрытой сохранён ДОСЛОВНО (ключ совпадёт со sidecar)
            self.assertIn("Илья: отчёт по выручке", fresh)
            merged = sm.merge_open_tasks_with_status(fresh, sm.load_task_status(sd))
            # shown=True закрытая исчезла из ВСЕХ корзин — не воскресла как висящая
            self.assertFalse(any("отчёт по выручке" in t for t in merged["open"]))
            self.assertFalse(any("отчёт по выручке" in c["text"] for c in merged["closed"]))
            self.assertIn("Татьяна: смета", merged["open"])

    def test_doubt_reclassified_on_roundtrip(self):
        # Сомнительная переносится дословно и при следующем merge снова попадает в
        # корзину doubt (а не в open).
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            sm.set_task_status(sd, "Мария: бриф для дизайнера", sm.STATUS_DOUBT,
                               reason="по чату")
            proto = (
                f"## {sm.PENDING_SECTION_HEADING}\n"
                "🟡 Вроде закрыто — подтвердите\n"
                "- Мария: бриф для дизайнера\n"
            )
            fresh = sm.extract_open_tasks(proto)
            self.assertIn("Мария: бриф для дизайнера", fresh)
            merged = sm.merge_open_tasks_with_status(fresh, sm.load_task_status(sd))
            self.assertTrue(any("бриф для дизайнера" in d["text"] for d in merged["doubt"]))
            self.assertFalse(any("бриф для дизайнера" in t for t in merged["open"]))

    def test_short_owner_name_substring_carryover_preserved(self):
        # Регресс Н2 (цикл5): короткое имя владельца лежит ВНУТРИ слова задачи
        # («Аня» ⊂ «з-аня-ть»). Старый подстрочный guard терял бы владельца на
        # round-trip carryover → рушил R7 и сдвигал sidecar-ключ. Префикс «Имя:»
        # обязан сохраниться.
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "**Аня**\n"
            "- занять очередь в налоговой — висит\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertIn("Аня: занять очередь в налоговой", carried)
        self.assertNotIn("занять очередь в налоговой", carried)  # без префикса = баг

    def test_short_owner_name_substring_tasks_block_preserved(self):
        # Тот же фикс на ОСНОВНОМ (прод-активном) пути — блок «Задачи» → open_tasks
        # на каждом финализе. «Лев» ⊂ «с-лев-а».
        proto = (
            "## Задачи\n\n"
            "**Лев**\n"
            "- проверить колонку слева в отчёте\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertIn("Лев: проверить колонку слева в отчёте", carried)

    def test_owner_prefix_not_doubled_when_already_inline(self):
        # Фикс не должен дублировать префикс, если модель оставила «Имя:» в строке.
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "**Аня**\n"
            "- Аня: занять очередь — висит\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertIn("Аня: занять очередь", carried)
        self.assertFalse(any("Аня: Аня" in t for t in carried))

    def test_owner_prefix_not_doubled_for_superstring_name(self):
        # НОВ1 (цикл5 ход4): строка уже начинается с БОЛЕЕ длинного имени, начинающегося
        # на владельца подзаголовка («Иван» ⊂ начала «Иванов:»). Префикс не задваиваем.
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "**Иван**\n"
            "- Иванов: подготовить отчёт — висит\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertFalse(any("Иван: Иванов" in t for t in carried))
        self.assertIn("Иванов: подготовить отчёт", carried)


# ==========================================================================
# Структурные якоря заголовка переехали на мягкий вариант
# ==========================================================================
class TestHeadingAnchors(unittest.TestCase):

    def test_classify_heading_carryover(self):
        self.assertEqual(sm._classify_heading(sm.PENDING_SECTION_HEADING), "carryover")

    def test_strip_carryover_new_heading(self):
        proto = (
            "## Решения\nРешили X\n\n"
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "**Илья**\n- HANGING_TASK_SECRET — висит\n\n"
            "## ⚠️ Проверить\n- свериться по цифрам\n"
        )
        out = kd.strip_carryover(proto)
        self.assertNotIn("HANGING_TASK_SECRET", out)
        self.assertNotIn(sm.PENDING_SECTION_HEADING, out)
        self.assertIn("Решили X", out)
        self.assertIn("свериться по цифрам", out)   # следующая секция уцелела

    def test_strip_carryover_legacy_heading_still_works(self):
        # обратная совместимость: старый 🔻-заголовок всё ещё вырезается
        proto = "## Решения\nРешили X\n\n## 🔻 С прошлых встреч\n- задача — висит\n"
        out = kd.strip_carryover(proto)
        self.assertNotIn("задача", out.split("Решения")[1] if "Решения" in out else out)
        self.assertNotIn("🔻", out)


# ==========================================================================
# Приватность лога (опасная тройка) — счётчики, не тексты
# ==========================================================================
class TestLogPrivacy(unittest.TestCase):

    def test_doubt_closed_text_not_logged(self):
        secret_d = "СЕКРЕТ-СОМНЕНИЕ"
        secret_c = "СЕКРЕТ-ЗАКРЫТО"
        digests = [{"date": "2026-06-08", "open_tasks":
                    [f"Илья: {secret_d}", f"Татьяна: {secret_c}"]}]
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            sm.set_task_status(sd, f"Илья: {secret_d}", sm.STATUS_DOUBT)
            sm.set_task_status(sd, f"Татьяна: {secret_c}", sm.STATUS_DONE)
            with self.assertLogs("meeting_notary.series_memory", level="INFO") as cm:
                sm.build_open_tasks_block(digests, series_dir=sd, meeting_sid="p3log",
                                          mark_shown=True)
            blob = "\n".join(cm.output)
            self.assertIn("closed=1", blob)
            self.assertIn("doubt=1", blob)
            self.assertNotIn(secret_d, blob)
            self.assertNotIn(secret_c, blob)


if __name__ == "__main__":
    unittest.main()
