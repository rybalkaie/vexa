"""Ф5 (watson-smart-replies-rethink) — бот не путает людей (ISS-38).

Покрывает REQ:
  - R17: обращение по имени ≠ имя говорящего. Промпт маппера получил вокатив-правило;
         детерминированно: спикер, подписанный N, в своих репликах зовущий N («Привет,
         Тань» от «Татьяны») → это НЕ Татьяна (говорящий зовёт Таню).
  - R18: взаимные приветствия = кросс-проверка. A зовёт B ⇒ A≠B; искусственно
         перепутанная (своп) разметка ловится, корректная — чистая. Диминутивы
         Тань→Татьяна, Машунь→Мария; неоднозначные (Саш→Александр/Александра) → НЕ
         угадываются.
  - R19: неуверенная разметка видна честно в ДВУХ местах — плашка в ШАПКЕ протокола
         (называет конкретные имена X/Y) + врезка в ТЕЛЕ у пунктов со спорными именами.

Всё офлайн (без claude/сети): кросс-проверка детерминированная.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase5_vocative_speakers -v
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
if str(_NOTARY) not in sys.path:
    sys.path.insert(0, str(_NOTARY))

from lib import vocative_check as vc  # noqa: E402


def _transcript(*lines: str) -> str:
    return "\n".join(lines) + "\n"


# ==========================================================================
# R17 — вокатив-правило: обращение по имени ≠ имя говорящего
# ==========================================================================
class TestR17VocativeRule(unittest.TestCase):
    def test_prompt_has_vocative_direction_rule(self):
        """Промпт маппера явно объясняет направление вокатива и запрет самоименования."""
        from lib import llm_postprocess as lp
        prompt = lp.MAP_SPEAKER_NAMES_SYSTEM_PROMPT
        # Направление: произносящий обращение — НЕ адресат.
        self.assertIn("Привет, Тань", prompt)
        self.assertIn("НЕ Татьяна", prompt)
        # Запрет подставлять кластеру имя, к которому он сам обращается.
        self.assertIn("САМ обращается", prompt)
        # Уменьшительные упомянуты (иначе LLM не свяжет «Тань» с Татьяной).
        self.assertIn("уменьшительн", prompt.lower())

    def test_speaker_greeting_tanya_is_not_tatyana(self):
        """«Привет, Тань» от спикера, подписанного Татьяной → детект: говорящий НЕ Татьяна."""
        md = _transcript(
            "**[00:01] Татьяна:** Привет, Тань, как дела?",
            "**[00:02] Мария:** Нормально, спасибо.",
        )
        conflicts = vc.find_conflicts(md)
        self.assertTrue(conflicts, "само-обращение Татьяны должно давать конфликт")
        names = set()
        for c in conflicts:
            names |= {c.name_a, c.name_b}
        self.assertIn("Татьяна", names)


# ==========================================================================
# R18 — резолв уменьшительных и неоднозначность
# ==========================================================================
class TestR18Diminutives(unittest.TestCase):
    def test_tan_resolves_to_tatyana(self):
        self.assertEqual(vc.resolve_address("Тань", ["Татьяна", "Мария"]), {"Татьяна"})
        self.assertEqual(vc.resolve_address("танюш", ["Татьяна"]), {"Татьяна"})

    def test_mashun_resolves_to_maria(self):
        self.assertEqual(vc.resolve_address("Машунь", ["Татьяна", "Мария"]), {"Мария"})

    def test_sash_is_ambiguous_not_guessed(self):
        """«Саш» = Александр ИЛИ Александра — не угадываем (РИСК5)."""
        # Оба присутствуют → однозначно не резолвится.
        self.assertEqual(vc.resolve_address("Саш", ["Александр", "Александра"]), set())
        # Даже с одним присутствующим — форма помечается неоднозначной.
        self.assertTrue(vc.resolve_address_ambiguous("Саш", ["Александр"]))
        self.assertTrue(vc.resolve_address_ambiguous("Саш", ["Александра"]))

    def test_nominative_full_name_resolves(self):
        self.assertEqual(vc.resolve_address("Михаил", ["Михаил", "Ольга"]), {"Михаил"})

    def test_full_display_name_first_token(self):
        """«Илюш» адресует «Илья Рыбалка» по первому токену."""
        self.assertEqual(vc.resolve_address("Илюш", ["Илья Рыбалка", "Ольга"]), {"Илья Рыбалка"})

    def test_yo_normalization(self):
        self.assertEqual(vc.resolve_address("Лёш", ["Алексей"]), {"Алексей"})


class TestR18AddressedNames(unittest.TestCase):
    def test_greeting_prefix(self):
        names, amb = vc.addressed_names("Слушай, Оль, вопрос к тебе", ["Ольга", "Иван"])
        self.assertEqual(names, {"Ольга"})
        self.assertFalse(amb)

    def test_narration_list_not_greeting_is_ignored(self):
        """Номинатив в середине без звательного контекста (перечисление) не считается
        обращением — иначе кошмарили бы корректную разметку."""
        # «обсудили с Михаилом и Ольгой» — падежи, не обращение; наш детект их не берёт.
        names, _amb = vc.addressed_names("Мы обсудили это с Иваном подробно", ["Иван", "Ольга"])
        self.assertEqual(names, set())

    def test_ambiguous_flagged(self):
        names, amb = vc.addressed_names("Саш, глянь", ["Александр"])
        self.assertEqual(names, set())
        self.assertTrue(amb)

    def test_greeting_then_subject_not_address(self):
        """«Смотри, Иван уже сделал» — Иван ПОДЛЕЖАЩЕЕ, не адресат (не ложим обращение)."""
        names, _amb = vc.addressed_names("Смотри, Иван уже всё сделал", ["Иван", "Ольга"])
        self.assertEqual(names, set())

    def test_greeting_then_name_with_punct_is_address(self):
        """«Слушай, Иван, вопрос» — Иван обособлен → обращение."""
        names, _amb = vc.addressed_names("Слушай, Иван, есть вопрос", ["Иван", "Ольга"])
        self.assertEqual(names, {"Иван"})


# ==========================================================================
# R18 — кросс-проверка «A зовёт B ⇒ A≠B»
# ==========================================================================
class TestR18CrossCheck(unittest.TestCase):
    def test_swapped_markup_is_caught(self):
        """Искусственно перепутанная (своп) разметка ловится."""
        md = _transcript(
            "**[00:01] Татьяна:** Привет, Тань, как продажи?",
            "**[00:02] Мария:** Машунь, привет! Норм.",
            "**[00:03] Татьяна:** Тань, глянь ещё отчёт.",
        )
        conflicts = vc.find_conflicts(md)
        self.assertEqual(len(conflicts), 1)
        c = conflicts[0]
        self.assertEqual({c.name_a, c.name_b}, {"Татьяна", "Мария"})
        self.assertEqual(c.kind, "swap")

    def test_correct_markup_is_clean(self):
        """Корректная разметка (каждый зовёт ДРУГОГО) — без конфликтов."""
        md = _transcript(
            "**[00:01] Мария:** Привет, Тань, как продажи?",
            "**[00:02] Татьяна:** Машунь, привет! Норм.",
            "**[00:03] Мария:** Тань, глянь ещё отчёт.",
        )
        self.assertEqual(vc.find_conflicts(md), [])

    def test_lone_self_address_two_speakers_pairs(self):
        """Один самозванец при двух названных спикерах → пара с другим."""
        md = _transcript(
            "**[00:01] Ольга:** Оль, ну ты чего.",  # Ольга зовёт Олю (сама себя)
            "**[00:02] Михаил:** Да понял я.",
        )
        conflicts = vc.find_conflicts(md)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual({conflicts[0].name_a, conflicts[0].name_b}, {"Ольга", "Михаил"})

    def test_single_named_speaker_no_conflict(self):
        """Меньше двух названных спикеров → проверять нечего."""
        md = _transcript("**[00:01] Татьяна:** Привет, Тань.")
        self.assertEqual(vc.find_conflicts(md), [])

    def test_placeholder_speakers_ignored(self):
        """«Спикер N» — не имя, авторского конфликта по нему нет."""
        md = _transcript(
            "**[00:01] Спикер 1:** Привет, Тань.",
            "**[00:02] Спикер 2:** Здорово.",
        )
        self.assertEqual(vc.find_conflicts(md), [])
        self.assertTrue(vc.is_placeholder_label("Спикер 3"))
        self.assertFalse(vc.is_placeholder_label("Татьяна"))

    def test_ambiguous_diminutive_does_not_false_flag(self):
        """Неоднозначная форма («Саш») не порождает ложный конфликт."""
        md = _transcript(
            "**[00:01] Александр:** Саш, подвинься.",  # неоднозначно → не конфликт
            "**[00:02] Ольга:** Ок.",
        )
        self.assertEqual(vc.find_conflicts(md), [])

    def test_nominative_self_address_caught(self):
        md = _transcript(
            "**[00:01] Михаил:** Михаил, посмотри задачу.",
            "**[00:02] Ольга:** Ольга, спасибо тебе.",
        )
        conflicts = vc.find_conflicts(md)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual({conflicts[0].name_a, conflicts[0].name_b}, {"Михаил", "Ольга"})


class TestR18Findings(unittest.TestCase):
    def test_findings_carry_pair_and_notes(self):
        md = _transcript(
            "**[00:01] Татьяна:** Привет, Тань.",
            "**[00:02] Мария:** Машунь, привет.",
        )
        findings = vc.find_vocative_findings(md)
        self.assertEqual(len(findings), 2)  # по одному на имя пары
        self.assertTrue(all(f["section"] == vc.VOCATIVE_SECTION for f in findings))
        self.assertTrue(all(sorted(f["pair"]) == ["Мария", "Татьяна"] for f in findings))
        quotes = {f["quote"] for f in findings}
        self.assertEqual(quotes, {"Татьяна", "Мария"})
        for f in findings:
            self.assertTrue(f["quote"] and f["note"])  # проходят фильтр extra_findings


# ==========================================================================
# R19 — плашка в шапке + врезка в теле (рендер через apply_review_flags)
# ==========================================================================
_PROTOCOL = (
    "#протоколвстречи 21.07.2026\n"
    "**Встреча:** Синк директоров\n"
    "**Участники:** Татьяна, Мария\n"
    "\n"
    "---\n"
    "\n"
    "## Задачи\n"
    "**Татьяна:** подготовить отчёт по продажам\n"
    "**Мария:** обновить прайс к пятнице\n"
    "\n"
    "## Решения\n"
    "- Мария и Татьяна согласовали бюджет\n"
)


def _swap_findings():
    md = _transcript(
        "**[00:01] Татьяна:** Привет, Тань, как продажи?",
        "**[00:02] Мария:** Машунь, привет! Норм.",
    )
    return vc.find_vocative_findings(md)


class TestR19PlaqueAndInline(unittest.TestCase):
    def setUp(self):
        from lib import llm_postprocess as lp
        self.lp = lp
        self.findings = _swap_findings()
        self.out = lp.apply_review_flags(_PROTOCOL, self.findings)

    def test_plaque_in_header_names_concrete_names(self):
        """Место №1: плашка в ШАПКЕ (до первой ## секции) называет конкретные имена."""
        self.assertIn(self.lp.SPEAKER_PLAQUE_SENTINEL, self.out)
        head = self.out.split("## Задачи")[0]
        self.assertIn(self.lp.SPEAKER_PLAQUE_SENTINEL, head)  # именно в шапке
        self.assertTrue(
            ("Татьяна и Мария" in head) or ("Мария и Татьяна" in head),
            "плашка обязана называть обе спорные фамилии",
        )

    def test_inline_note_in_body_at_disputed_items(self):
        """Место №2 (RAZM2): врезка ПРЯМО у пунктов тела со спорными именами."""
        body = self.out.split("## Задачи", 1)[1]
        # У задачи Татьяны и задачи Марии, и у решения с обоими именами.
        self.assertIn("проверьте: тут мог быть", body)
        self.assertEqual(self.out.count("проверьте: тут мог быть"), 3)

    def test_inline_not_in_header(self):
        """Врезка не течёт в шапку (там имена участников — их несёт плашка)."""
        head = self.out.split("## Задачи")[0]
        self.assertNotIn("проверьте: тут мог быть", head)

    def test_idempotent(self):
        out2 = self.lp.apply_review_flags(self.out, self.findings)
        out3 = self.lp.apply_review_flags(out2, self.findings)
        self.assertEqual(self.out, out2)
        self.assertEqual(out2, out3)

    def test_no_findings_no_change(self):
        self.assertEqual(self.lp.apply_review_flags(_PROTOCOL, []), _PROTOCOL)

    def test_non_vocative_findings_untouched(self):
        """Смешанные секции: values-находка рендерится по-старому, vocative — особо."""
        findings = self.findings + [
            {"section": "values", "quote": "обновить прайс к пятнице", "note": "проверь срок"}
        ]
        out = self.lp.apply_review_flags(_PROTOCOL, findings)
        self.assertIn(self.lp.SPEAKER_PLAQUE_SENTINEL, out)
        self.assertIn("⚠️ проверь: проверь срок", out)  # старый контур values жив


# ==========================================================================
# Достижимость из реального ревью-контура (review_and_flag_protocol_file):
# та же функция зовётся из finalize И clarify_worker.
# ==========================================================================
class TestReviewContourIntegration(unittest.TestCase):
    def setUp(self):
        from lib import llm_postprocess as lp
        self.lp = lp
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.protocol_path = d / "2026-07-21-protokol.md"
        self.transcript_path = d / "2026-07-21.md"
        self.protocol_path.write_text(_PROTOCOL, encoding="utf-8")
        self.transcript_path.write_text(_transcript(
            "**[00:01] Татьяна:** Привет, Тань, как продажи?",
            "**[00:02] Мария:** Машунь, привет! Норм.",
        ), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_review_contour_writes_plaque_and_inline(self):
        """flag-only путь (review OFF → без claude): vocative-страховка всё равно
        детерминированно ставит плашку+врезку в файл протокола."""
        env = {"ENABLE_PROTOCOL_REVIEW": "0", "ENABLE_VOCATIVE_CHECK": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            n = self.lp.review_and_flag_protocol_file(
                protocol_path=self.protocol_path,
                transcript_path=self.transcript_path,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="test-sid",
                rewrite=True,  # finalize/clarify зовут с rewrite=True
            )
        self.assertGreater(n, 0)
        written = self.protocol_path.read_text(encoding="utf-8")
        self.assertIn(self.lp.SPEAKER_PLAQUE_SENTINEL, written)
        self.assertIn("проверьте: тут мог быть", written)

    def test_kill_switch_disables_vocative(self):
        env = {"ENABLE_PROTOCOL_REVIEW": "0", "ENABLE_VOCATIVE_CHECK": "0"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.lp.review_and_flag_protocol_file(
                protocol_path=self.protocol_path,
                transcript_path=self.transcript_path,
                checks=("values",),
                meeting_sid="test-sid",
                rewrite=True,
            )
        written = self.protocol_path.read_text(encoding="utf-8")
        self.assertNotIn(self.lp.SPEAKER_PLAQUE_SENTINEL, written)

    def test_correct_markup_no_plaque_via_contour(self):
        """Корректная разметка через контур не порождает плашку (нет ложных срабатываний)."""
        self.transcript_path.write_text(_transcript(
            "**[00:01] Мария:** Привет, Тань, как продажи?",
            "**[00:02] Татьяна:** Машунь, привет! Норм.",
        ), encoding="utf-8")
        env = {"ENABLE_PROTOCOL_REVIEW": "0", "ENABLE_VOCATIVE_CHECK": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.lp.review_and_flag_protocol_file(
                protocol_path=self.protocol_path,
                transcript_path=self.transcript_path,
                checks=("values",),
                meeting_sid="test-sid",
                rewrite=True,
            )
        written = self.protocol_path.read_text(encoding="utf-8")
        self.assertNotIn(self.lp.SPEAKER_PLAQUE_SENTINEL, written)


if __name__ == "__main__":
    unittest.main()
