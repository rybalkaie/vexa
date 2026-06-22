# -*- coding: utf-8 -*-
"""Ф1 (ISS-16, R1/R2/R5/R7/R8/R12): детерминированная точечная ЗАМЕНА «замени X на Y».

Корень (план 2026-06-19 protocol-patch-edits-not-regen): терсная механическая
правка («замени фронтенд на frontend») сегодня пере-собирает ВЕСЬ протокол через
Claude — проза плывёт, хотя просили одно слово. Ф1 — первый ДЕШЁВЫЙ ярус: парсим
терсную директиву и подменяем X→Y по СУЩЕСТВУЮЩЕМУ протоколу теми же word-boundary/
инвариантами/bounded-diff, что и name-путь Ф4 — без LLM.

R1 — «замени X на Y» точечной заменой без LLM (все X→Y, остальной текст идентичен).
R2 — отказ при over-match/неоднозначности (X — подстрока более широких слов / нет
     самостоятельным токеном) → «не применимо» → следующий ярус.
R5 — строгие инварианты Ф4: якорь/⚠️ целы, число секций И строк РАВНО старому.
R7 — не регресс: правки имён (name-путь) и смешанные (имя+контент) ведут как сегодня.
R8 — ноль LLM на пути; в логах/мете нет текста реплик.
R12 — результат течёт через слот targeted_new_text (тот же redeliver/архив).

Запуск: python3 -m unittest tests.test_iss16_term_replace (system python3.9, без venv).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import targeted_protocol_edit as tpe  # noqa: E402


def _protocol_term() -> str:
    """Протокол с термином «фронтенд» (4 самостоятельных вхождения) + якорь/⚠️/секции."""
    return (
        "#протоколвстречи 20.06.2026\n"
        "\n"
        "**Встреча:** Разработка портала.\n"
        "\n"
        "**Участники:** Илья Рыбалка, Пётр Сидоров\n"
        "\n"
        "> ℹ️ _Авторство реплик восстановлено автоматически — если перепутали, поправьте._\n"
        "\n"
        "---\n"
        "\n"
        "## 1) Архитектура\n"
        "\n"
        "▪️ Решили переписать фронтенд на React — отв. Пётр Сидоров.\n"
        "\n"
        "🔸 Сроки по фронтенд ⚠️ проверь: расходятся с речью.\n"
        "\n"
        "## 📌 Задачи\n"
        "\n"
        "**Пётр Сидоров**\n"
        "\n"
        "- Сверстать фронтенд до пятницы.\n"
        "\n"
        "## ⚠️ Проверить\n"
        "\n"
        "⚠️ термин под вопросом: фронтенд\n"
    )


# ---------------------------------------------------------------------------
# Парсер терсных директив
# ---------------------------------------------------------------------------
class ParseDirectiveTest(unittest.TestCase):
    def test_verb_zameni(self):
        self.assertEqual(tpe._parse_one_directive("замени фронтенд на frontend"),
                         ("фронтенд", "frontend"))

    def test_verb_pomenyay(self):
        self.assertEqual(tpe._parse_one_directive("поменяй сет на сот"), ("сет", "сот"))

    def test_verb_isprav(self):
        self.assertEqual(tpe._parse_one_directive("исправь телемост на Телемост"),
                         ("телемост", "Телемост"))

    def test_arrow_form(self):
        self.assertEqual(tpe._parse_one_directive("фронтенд → frontend"),
                         ("фронтенд", "frontend"))

    def test_arrow_ascii(self):
        self.assertEqual(tpe._parse_one_directive("фронтенд -> frontend"),
                         ("фронтенд", "frontend"))

    def test_quotes_stripped(self):
        self.assertEqual(tpe._parse_one_directive('замени "фронтенд" на «frontend»'),
                         ("фронтенд", "frontend"))

    def test_trailing_punct_stripped(self):
        self.assertEqual(tpe._parse_one_directive("замени фронтенд на frontend."),
                         ("фронтенд", "frontend"))

    def test_polite_prefix(self):
        self.assertEqual(tpe._parse_one_directive("пожалуйста, замени X на Y"),
                         ("X", "Y"))

    def test_non_directive_returns_none(self):
        self.assertIsNone(tpe._parse_one_directive("добавь задачу Ольге до среды"))

    def test_multiline_returns_none(self):
        self.assertIsNone(tpe._parse_one_directive("замени X на Y\nи ещё что-то"))

    def test_identity_returns_none(self):
        self.assertIsNone(tpe._parse_one_directive("замени frontend на frontend"))

    def test_empty_target_returns_none(self):
        self.assertIsNone(tpe._parse_one_directive("замени фронтенд на"))

    def test_too_long_returns_none(self):
        self.assertIsNone(tpe._parse_one_directive("замени " + "а" * 250 + " на б"))

    def test_multiple_na_separators_refused(self):
        # Н1 (цикл5): >1 самостоятельного предлога «на» → точка деления X|Y
        # неоднозначна → отказ (а не мис-парс X=«налоги», Y=«прибыль на НДС»).
        self.assertIsNone(tpe._parse_one_directive("замени налоги на прибыль на НДС"))
        self.assertIsNone(tpe._parse_one_directive("замени Иван на Петрович на встрече"))

    def test_na_inside_word_not_counted(self):
        # «на» внутри слова (Анна/начало) НЕ считается разделителем — кейс жив.
        self.assertEqual(tpe._parse_one_directive("замени Анна на Мария"), ("Анна", "Мария"))

    def test_trailing_prose_comma_refused(self):
        # У1 (цикл5): клаузо-хвост через «, » → Y поглотил бы продолжение фразы → отказ.
        self.assertIsNone(
            tpe._parse_one_directive("замени фронтенд на frontend, и убери последний пункт"))

    def test_trailing_prose_conjunction_refused(self):
        # У1 (цикл5): хвост без запятой, но >4 слов в Y → не термин → отказ.
        self.assertIsNone(
            tpe._parse_one_directive("замени фронтенд на frontend и ещё там дата неправильная"))

    def test_multiword_name_ok(self):
        # Имя из нескольких слов (≤ потолка) — валидный терсный Y, остаётся живым.
        self.assertEqual(
            tpe._parse_one_directive("замени Еремеев на Михаил Сергеевич Саргин"),
            ("Еремеев", "Михаил Сергеевич Саргин"))

    def test_decimal_comma_not_overrejected(self):
        # «1,5» (десятичная запятая без пробела) — не клаузо-хвост, кейс жив.
        self.assertEqual(tpe._parse_one_directive("замени 1,5 на 1.5"), ("1,5", "1.5"))

    def test_arrow_with_na_inside_ok(self):
        # Стрелочная форма: «на» внутри X легитимен (разделитель — стрелка, не «на»).
        self.assertEqual(tpe._parse_one_directive("план на год → бюджет"),
                         ("план на год", "бюджет"))


class ParseDirectivesListTest(unittest.TestCase):
    def test_single(self):
        self.assertEqual(tpe.parse_replace_directives(["замени фронтенд на frontend"]),
                         {"фронтенд": "frontend"})

    def test_multiple_terms(self):
        self.assertEqual(
            tpe.parse_replace_directives(["замени A на B", "замени C на D"]),
            {"A": "B", "C": "D"})

    def test_any_non_directive_aborts_all(self):
        # Есть не-замена среди правок → весь детерминированный путь не применим.
        self.assertIsNone(
            tpe.parse_replace_directives(["замени A на B", "добавь предложение про X"]))

    def test_conflict_returns_none(self):
        self.assertIsNone(tpe.parse_replace_directives(["замени A на B", "замени A на C"]))

    def test_empty_returns_none(self):
        self.assertIsNone(tpe.parse_replace_directives([]))
        self.assertIsNone(tpe.parse_replace_directives([""]))


# ---------------------------------------------------------------------------
# R2 — over-match / неоднозначность
# ---------------------------------------------------------------------------
class OverMatchTest(unittest.TestCase):
    def test_standalone_token_ok(self):
        old = "## Тема\n▪️ Решили про фронтенд сегодня.\n"
        self.assertFalse(tpe._replacement_is_ambiguous(old, "фронтенд"))

    def test_substring_of_wider_word_refused(self):
        # «сет» внутри «сетка» / «сеть» — самостоятельного токена нет → отказ.
        old = "## Тема\n▪️ Купили сетку и провели сеть в офис.\n"
        self.assertTrue(tpe._replacement_is_ambiguous(old, "сет"))

    def test_absent_token_refused(self):
        # «сет» при «интернет/совет» (ни один не содержит «сет») → нечего менять → отказ.
        old = "## Тема\n▪️ Подключили интернет, дали совет команде.\n"
        self.assertTrue(tpe._replacement_is_ambiguous(old, "сет"))

    def test_standalone_plus_substring_refused(self):
        # Самостоятельный «сет» ЕСТЬ, но «сет» ещё и внутри «сетка» → неоднозначно → отказ.
        old = "## Тема\n▪️ Это сет, а вот сетка отдельно.\n"
        self.assertTrue(tpe._replacement_is_ambiguous(old, "сет"))

    def test_apply_refuses_overmatch_whole(self):
        # R2 дословно: «замени сет на сот» при «интернет/совет» → None (НЕ трогает интернет).
        old = (
            "#протоколвстречи 20.06.2026\n\n**Участники:** Илья\n\n"
            "> ℹ️ _Авторство реплик восстановлено._\n\n---\n\n"
            "## Тема\n\n▪️ Подключили интернет, дали совет.\n"
        )
        self.assertIsNone(tpe.targeted_term_reissue(old, ["замени сет на сот"]))
        self.assertIn("интернет", old)  # исходник не тронут (None → без изменений)


# ---------------------------------------------------------------------------
# R1 + R5 — применение + строгие инварианты
# ---------------------------------------------------------------------------
class ApplyTermEditTest(unittest.TestCase):
    def test_all_occurrences_replaced(self):
        old = _protocol_term()
        new = tpe.apply_targeted_term_edit(old, {"фронтенд": "frontend"})
        self.assertIsNotNone(new)
        self.assertNotIn("фронтенд", new)
        self.assertEqual(new.count("frontend"), 4)

    def test_returns_none_when_term_absent(self):
        old = _protocol_term()
        self.assertIsNone(tpe.apply_targeted_term_edit(old, {"backend": "бэкенд"}))

    def test_high_level_r1_rest_identical(self):
        old = _protocol_term()
        res = tpe.targeted_term_reissue(old, ["замени фронтенд на frontend"])
        self.assertIsNotNone(res)
        new, meta = res
        # Все «фронтенд» → «frontend».
        self.assertNotIn("фронтенд", new)
        # Остальной текст идентичен — дифф ровно по этим словам (bounded-diff).
        ok, reason = tpe.diff_is_bounded(old, new, {"фронтенд": "frontend"}, "broad")
        self.assertTrue(ok, reason)
        # R5: число строк и секций РАВНО старому (замена слов длину не меняет).
        oi = tpe.protocol_invariants(old)
        ni = tpe.protocol_invariants(new)
        self.assertEqual(oi["line_count"], ni["line_count"])
        self.assertEqual(oi["heading_count"], ni["heading_count"])
        self.assertEqual(oi["warn_count"], ni["warn_count"])
        self.assertTrue(ni["anchor"])
        self.assertIn("Авторство реплик", new)
        # meta — только счётчики, без текста.
        self.assertEqual(meta["n_terms"], 1)
        self.assertEqual(meta["n_changes"], 4)

    def test_meta_carries_no_protocol_text(self):
        # Опасная тройка: meta не несёт содержимого протокола/правок.
        old = _protocol_term()
        _new, meta = tpe.targeted_term_reissue(old, ["замени фронтенд на frontend"])
        blob = json.dumps(meta, ensure_ascii=False)
        self.assertNotIn("фронтенд", blob)
        self.assertNotIn("frontend", blob)

    def test_rejects_when_replacement_changes_section_count(self):
        # Замена ТЕКСТА заголовка «## Решения» → «Решения» убрала бы секцию (строка
        # перестала начинаться с «## ») → строгий инвариант R5 отклоняет → None.
        old = (
            "#протоколвстречи 20.06.2026\n\n**Участники:** Илья\n\n"
            "> ℹ️ _Авторство реплик восстановлено._\n\n---\n\n"
            "## Решения\n\n▪️ Что-то решили.\n"
        )
        self.assertIsNone(tpe.targeted_term_reissue(old, ["замени ## Решения на Решения"]))

    def test_rejects_when_replacement_drops_warn(self):
        # X содержит ⚠️ → замена убрала бы пометку → строгий инвариант отклоняет.
        old = (
            "#протоколвстречи 20.06.2026\n\n**Участники:** Илья\n\n"
            "> ℹ️ _Авторство реплик восстановлено._\n\n---\n\n"
            "## Тема\n\n▪️ Тут ⚠️зам стоит.\n"
        )
        self.assertIsNone(tpe.targeted_term_reissue(old, ["замени ⚠️зам на зам"]))

    def test_empty_protocol_returns_none(self):
        self.assertIsNone(tpe.targeted_term_reissue("", ["замени A на B"]))

    def test_no_directive_returns_none(self):
        old = _protocol_term()
        self.assertIsNone(tpe.targeted_term_reissue(old, ["добавь раздел про сроки"]))


# ---------------------------------------------------------------------------
# Интеграция: достижимость из реального триггера reissue_one (R1/R2/R7/R8/R12)
# ---------------------------------------------------------------------------
def _write_meeting(tmp: Path) -> tuple:
    series_dir = tmp / "iss16-term"
    series_dir.mkdir(parents=True)
    date = "2026-06-20"
    transcript = series_dir / f"{date}.md"
    transcript.write_text(
        "**[00:01] Илья Рыбалка:** Перепишем фронтенд на React.\n\n"
        "**[00:02] Пётр Сидоров:** Сверстаю до пятницы.\n",
        encoding="utf-8",
    )
    protocol = series_dir / f"{date}-protokol.md"
    protocol.write_text(_protocol_term(), encoding="utf-8")
    meta = {
        "series": "iss16-term",
        "date": date,
        "transcript_path": str(transcript),
        "protocol_path": str(protocol),
        "expectedParticipants": ["Илья Рыбалка", "Пётр Сидоров", "Ольга Сонина"],
        "participants": ["Илья Рыбалка", "Пётр Сидоров"],
        "delivered": [{
            "chat_id": 777, "message_ids": [42], "at": "2026-06-20T10:00:00+00:00",
            "content_hash": "deadbeef", "revision": 0,
        }],
    }
    meta_path = series_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return transcript, protocol, meta_path


class ReissueDeterministicIntegrationTest(unittest.TestCase):
    """reissue_one: терсная «замени X на Y» → tier=deterministic, без регенерации."""

    def setUp(self):
        from notary.lib import feedback_reissue
        self.fr = feedback_reissue
        self._td = tempfile.TemporaryDirectory(prefix="iss16-term-")
        self.tmp = Path(self._td.name)
        self.transcript, self.protocol, self.meta_path = _write_meeting(self.tmp)
        self.old_protocol = self.protocol.read_text(encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _state(self, text: str) -> dict:
        return {
            "series": "iss16-term", "date": "2026-06-20",
            "meta_path": str(self.meta_path), "chat_id": 777,
            "feedback_id": "fb-iss16-1",
            "edits": [{"author": "Илья", "text": text}],
        }

    def test_terse_replace_skips_regeneration(self):
        # R1/R8: deterministic путь — generate_fn (LLM) НЕ вызывается.
        def _gen(*a, **k):
            raise AssertionError("generate_fn НЕ должна вызываться на детерминированном пути")

        captured = {}

        def _redeliver(meeting_meta, old_text, new_text, **k):
            # R12: тот же слот доставки получает old/new текстом-аргументом.
            captured["old"] = old_text
            captured["new"] = new_text
            return {"status": "sent", "chat_id": 777, "message_ids": [99],
                    "revision": 1, "content_hash": "x"}

        saved = {"n": 0}

        res = self.fr.reissue_one(
            self._state("замени фронтенд на frontend"),
            root=self.tmp,
            generate_fn=_gen, redeliver_fn=_redeliver,
            save_version_fn=lambda *_a, **_k: saved.__setitem__("n", saved["n"] + 1),
        )
        self.assertEqual(res.get("status"), "sent")
        new = captured["new"]
        self.assertNotIn("фронтенд", new)
        self.assertEqual(new.count("frontend"), 4)
        # Инварианты целы (R5).
        self.assertTrue(new.lstrip().startswith("#протоколвстречи"))
        self.assertIn("⚠️", new)
        self.assertIn("Авторство реплик", new)
        # Остальной текст стабилен: ровно подмена термина (R1, R12 архив зван).
        ok, reason = tpe.diff_is_bounded(
            self.old_protocol, new, {"фронтенд": "frontend"}, "broad")
        self.assertTrue(ok, reason)
        self.assertEqual(saved["n"], 1, "save_version_fn (архив версии) должен быть зван (R12)")

    def test_overmatch_falls_back_to_regen(self):
        # R2: «замени сет на сот» при «интернет/совет» (нет самостоятельного «сет»)
        # → детерминированный путь не применим → регенерация (generate_fn зван).
        called = {"gen": 0}
        regen_text = (
            "#протоколвстречи 20.06.2026\n\n**Участники:** Илья Рыбалка, Пётр Сидоров\n\n"
            "---\n\n## Тема\n\n▪️ Регенерация.\n"
        )

        def _gen(path, meta, sid):
            called["gen"] += 1
            return regen_text

        def _redeliver(meeting_meta, old_text, new_text, **k):
            return {"status": "sent", "chat_id": 777, "message_ids": [99],
                    "revision": 1, "content_hash": "x"}

        res = self.fr.reissue_one(
            self._state("замени сет на сот"),
            root=self.tmp,
            generate_fn=_gen, redeliver_fn=_redeliver,
            save_version_fn=lambda *_a, **_k: None,
        )
        self.assertEqual(called["gen"], 1, "over-match → должна быть регенерация")
        self.assertEqual(res.get("status"), "sent")

    def test_non_directive_content_falls_back_to_regen(self):
        # R7: обычная контентная правка (не «замени X на Y») → регенерация как сегодня.
        called = {"gen": 0}

        def _gen(path, meta, sid):
            called["gen"] += 1
            return ("#протоколвстречи 20.06.2026\n\n**Участники:** Илья\n\n"
                    "---\n\n## Тема\n\n▪️ Регенерация.\n")

        def _redeliver(meeting_meta, old_text, new_text, **k):
            return {"status": "sent", "chat_id": 777, "message_ids": [99],
                    "revision": 1, "content_hash": "x"}

        res = self.fr.reissue_one(
            self._state("добавь задачу Ольге: проверить склад до среды"),
            root=self.tmp,
            generate_fn=_gen, redeliver_fn=_redeliver,
            save_version_fn=lambda *_a, **_k: None,
        )
        self.assertEqual(called["gen"], 1)
        self.assertEqual(res.get("status"), "sent")

    def test_mixed_name_and_content_applied_pointwise(self):
        # ISS-17 (R13/R14): смешанная (авторство + терсный контент) теперь применяется
        # ТОЧЕЧНО по одному протоколу — name-remap детерминированно + term-замена
        # детерминированно, БЕЗ регенерации. Раньше (R7-вне-скоупа) уходило в regen.
        captured = {}

        def _gen(*a, **k):
            raise AssertionError("смешанный точечный путь лёг → generate_fn НЕ должна зваться")

        def _redeliver(meeting_meta, old_text, new_text, **k):
            captured["new"] = new_text
            return {"status": "sent", "chat_id": 777, "message_ids": [99],
                    "revision": 1, "content_hash": "x"}

        state = {
            "series": "iss16-term", "date": "2026-06-20",
            "meta_path": str(self.meta_path), "chat_id": 777,
            "feedback_id": "fb-iss16-mix",
            # «не Илья, а Ольга» → oneway-remap {Илья Рыбалка→Ольга Сонина} (Ольга в
            # expectedParticipants, не текущий спикер → не своп). Своп ушёл бы в regen.
            "edits": [
                {"author": "Илья", "text": "не Илья, а Ольга"},
                {"author": "Илья", "text": "замени фронтенд на frontend"},
            ],
        }
        res = self.fr.reissue_one(
            state, root=self.tmp,
            generate_fn=_gen, redeliver_fn=_redeliver,
            save_version_fn=lambda *_a, **_k: None,
        )
        self.assertEqual(res.get("status"), "sent")
        new = captured["new"]
        # Контентная часть применена точечно.
        self.assertNotIn("фронтенд", new)
        self.assertEqual(new.count("frontend"), 4)
        # Авторская часть применена (Илья Рыбалка → Ольга Сонина).
        self.assertNotIn("Илья", new)
        self.assertIn("Ольга Сонина", new)
        # Инварианты целы.
        self.assertTrue(new.lstrip().startswith("#протоколвстречи"))
        self.assertIn("⚠️", new)


if __name__ == "__main__":
    unittest.main()
