# -*- coding: utf-8 -*-
"""Ф4 (R15/R16): точечная пересборка протокола по правке авторства/имени.

R15 — точечная правка трогает только адресованное; масштаб — по тексту обратной
связи: «эту реплику не тому приписал» → ОДНА реплика меняется, остальное
идентично; «вообще перепутал человека» → ВСЕ места этого человека, остальной
текст идентичен.

R16 — инварианты целы после точечной правки: якорь `#протоколвстречи`,
⚠️-пометки (вкл. дисклеймер авторства R3 / inline / хвостовой блок «## ⚠️
Проверить»), markdown-структура. ⚠️ сохраняются независимо от того, какой
call-site их поставил (finalize-style хвостовой блок + clarify-style пометки).

Интеграция — достижимость из РЕАЛЬНОГО триггера `feedback_reissue.reissue_one`:
чистая правка авторства → регенерация (`generate_fn`) НЕ вызывается, доставляется
точечно-исправленный старый протокол; смешанная (есть контентная правка) →
точечный путь пропускается, идёт регенерация (без регресса).

Запуск: python3 -m unittest tests.test_phase4_targeted_edit (system python3.9, без venv).
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

from notary.lib import targeted_protocol_edit as tpe  # noqa: E402


# ---------------------------------------------------------------------------
# Реалистичный протокол: якорь + дисклеймер (R3/Ф4а) + участники + секции +
# имя в участниках / задачах-владельцах / прозе + inline-⚠️ + хвостовой ⚠️-блок.
# ---------------------------------------------------------------------------
OLD = "Михаил Еремеев"
NEW = "Михаил Саргин"


def _protocol() -> str:
    return (
        "#протоколвстречи 09.06.2026\n"
        "\n"
        "**Встреча:** Координация Anzhee.\n"
        "\n"
        "**Длительность:** 25 мин\n"
        "\n"
        "**Участники:** Илья Рыбалка, Михаил Еремеев, Ольга Сонина\n"
        "\n"
        "> ℹ️ _Авторство реплик восстановлено автоматически по голосам — если "
        "перепутали, поправьте._\n"
        "\n"
        "---\n"
        "\n"
        "## 1) Поставки\n"
        "\n"
        "▪️ Контейнер застрял на таможне — Михаил Еремеев ведёт переговоры с брокером.\n"
        "\n"
        "▪️ Ольга Сонина сверяет документы.\n"
        "\n"
        "## ✅ Решения\n"
        "\n"
        "🔸 Сумма 28 млн ⚠️ проверь: расходится с речью.\n"
        "\n"
        "🔸 Запросить у брокера новую дату — отв. Михаил Еремеев.\n"
        "\n"
        "## 📌 Задачи\n"
        "\n"
        "**Михаил Еремеев**\n"
        "\n"
        "- Получить новую дату растаможки до пятницы.\n"
        "\n"
        "**Илья Рыбалка**\n"
        "\n"
        "- Свести бюджет по логистике.\n"
        "\n"
        "## ⚠️ Проверить\n"
        "\n"
        "⚠️ авторство под вопросом, поправьте: Михаил Еремеев\n"
    )


class ScopeClassifyTest(unittest.TestCase):
    def test_default_is_broad(self):
        self.assertEqual(tpe.classify_edit_scope(["не Илья, а Пётр"]), "broad")

    def test_empty_is_broad(self):
        self.assertEqual(tpe.classify_edit_scope([]), "broad")
        self.assertEqual(tpe.classify_edit_scope(["", None]), "broad")

    def test_narrow_signal(self):
        self.assertEqual(
            tpe.classify_edit_scope(["эту реплику не тому приписал"]), "narrow")
        self.assertEqual(
            tpe.classify_edit_scope(["в этом пункте перепутал, не А а Б"]), "narrow")

    def test_broad_signal(self):
        self.assertEqual(
            tpe.classify_edit_scope(["вообще перепутал человека"]), "broad")
        self.assertEqual(
            tpe.classify_edit_scope(["это везде не тот человек"]), "broad")

    def test_broad_beats_narrow_when_both(self):
        # Явный broad-сигнал перебивает узкий.
        self.assertEqual(
            tpe.classify_edit_scope(["вот эту тоже, но вообще он везде не тот"]),
            "broad")

    def test_whitespace_normalized(self):
        self.assertEqual(
            tpe.classify_edit_scope(["эту   \n  реплику не тому"]), "narrow")


class BroadEditTest(unittest.TestCase):
    """R15 broad: ВСЕ места человека меняются, остальной текст побайтно идентичен."""

    def setUp(self):
        self.old = _protocol()
        self.res = tpe.targeted_name_reissue(
            self.old, {OLD: NEW}, ["вообще перепутал человека"])

    def test_applied(self):
        self.assertIsNotNone(self.res)

    def test_all_name_lines_changed_rest_identical(self):
        new, meta = self.res
        self.assertEqual(meta["scope"], "broad")
        old_lines = self.old.split("\n")
        new_lines = new.split("\n")
        self.assertEqual(len(old_lines), len(new_lines))
        for o, n in zip(old_lines, new_lines):
            if OLD in o:
                self.assertEqual(n, o.replace(OLD, NEW),
                                 f"строка с именем не подменена: {o!r}")
            else:
                self.assertEqual(n, o, f"посторонняя строка изменилась: {o!r}")

    def test_old_name_gone_new_name_everywhere(self):
        new, _ = self.res
        self.assertNotIn(OLD, new)
        # Было 4 вхождения (участники, проза, решение-отв, задача-владелец, ⚠️-flag).
        self.assertEqual(new.count(NEW), self.old.count(OLD))

    def test_participants_line_updated(self):
        new, _ = self.res
        self.assertIn("Илья Рыбалка, Михаил Саргин, Ольга Сонина", new)


class NarrowEditTest(unittest.TestCase):
    """R15 narrow: меняется ОДНА реплика, всё остальное (вкл. участников) идентично."""

    def setUp(self):
        self.old = _protocol()
        self.res = tpe.targeted_name_reissue(
            self.old, {OLD: NEW}, ["эту реплику ты не тому приписал"])

    def test_applied_narrow(self):
        self.assertIsNotNone(self.res)
        _, meta = self.res
        self.assertEqual(meta["scope"], "narrow")
        self.assertEqual(meta["n_changes"], 1)

    def test_exactly_one_line_changed(self):
        new, _ = self.res
        old_lines = self.old.split("\n")
        new_lines = new.split("\n")
        diffs = [(o, n) for o, n in zip(old_lines, new_lines) if o != n]
        self.assertEqual(len(diffs), 1)
        o, n = diffs[0]
        self.assertEqual(n, o.replace(OLD, NEW, 1))
        # Первая КОНТЕНТНАЯ строка с именем — проза «Поставки», не шапка/участники.
        self.assertIn("Контейнер застрял", o)

    def test_participants_untouched_by_narrow(self):
        new, _ = self.res
        # Человек остаётся участником — narrow не трогает строку «Участники».
        self.assertIn("Илья Рыбалка, Михаил Еремеев, Ольга Сонина", new)

    def test_other_mentions_untouched(self):
        new, _ = self.res
        # Имя ещё встречается (задача-владелец, ⚠️-flag и т.д.) — narrow тронул одно.
        self.assertIn(OLD, new)
        self.assertGreater(new.count(OLD), 1)


class InvariantsTest(unittest.TestCase):
    """R16: якорь / ⚠️ / дисклеймер / структура целы после точечной правки."""

    def test_invariants_snapshot(self):
        inv = tpe.protocol_invariants(_protocol())
        self.assertTrue(inv["anchor"])
        self.assertTrue(inv["disclaimer"])
        # inline-⚠️ + «## ⚠️ Проверить» + хвостовой flag = 3.
        self.assertEqual(inv["warn_count"], 3)
        self.assertEqual(inv["heading_count"], 4)

    def test_broad_preserves_all_invariants(self):
        old = _protocol()
        new, _ = tpe.targeted_name_reissue(old, {OLD: NEW}, ["вообще перепутал"])
        oi, ni = tpe.protocol_invariants(old), tpe.protocol_invariants(new)
        ok, reason = tpe.invariants_preserved(oi, ni)
        self.assertTrue(ok, reason)
        # ⚠️ не убыло (хвостовой flag переименовался, но маркер на месте).
        self.assertEqual(ni["warn_count"], oi["warn_count"])
        self.assertTrue(ni["anchor"])
        self.assertTrue(ni["disclaimer"])
        self.assertEqual(ni["heading_count"], oi["heading_count"])

    def test_narrow_preserves_all_invariants(self):
        old = _protocol()
        new, _ = tpe.targeted_name_reissue(old, {OLD: NEW}, ["эту реплику не тому"])
        ok, reason = tpe.invariants_preserved(
            tpe.protocol_invariants(old), tpe.protocol_invariants(new))
        self.assertTrue(ok, reason)

    def test_both_callsite_warn_markers_survive(self):
        # R16 «оба call-site»: ⚠️ из finalize-style (хвостовой блок «## ⚠️ Проверить»
        # + authorship-flag) И clarify-style (inline «спикер под вопросом») — все
        # переживают точечную правку. Собираем протокол с обоими видами пометок.
        old = _protocol().replace(
            "▪️ Ольга Сонина сверяет документы.",
            "▪️ Ольга Сонина сверяет документы.  ⚠️ спикер под вопросом: «...» — проверь",
        )
        before = old.count(tpe.REVIEW_FLAG_MARKER)
        new, _ = tpe.targeted_name_reissue(old, {OLD: NEW}, ["вообще перепутал"])
        self.assertGreaterEqual(new.count(tpe.REVIEW_FLAG_MARKER), before)
        self.assertIn("## ⚠️ Проверить", new)
        self.assertIn("спикер под вопросом", new)
        self.assertIn("авторство под вопросом", new)

    def test_invariants_preserved_rejects_warn_loss(self):
        ok, reason = tpe.invariants_preserved(
            {"anchor": True, "warn_count": 3, "disclaimer": True,
             "heading_count": 4, "line_count": 30},
            {"anchor": True, "warn_count": 2, "disclaimer": True,
             "heading_count": 4, "line_count": 30})
        self.assertFalse(ok)
        self.assertIn("⚠️", reason)

    def test_invariants_preserved_rejects_anchor_loss(self):
        ok, _ = tpe.invariants_preserved(
            {"anchor": True, "warn_count": 0, "disclaimer": False,
             "heading_count": 1, "line_count": 5},
            {"anchor": False, "warn_count": 0, "disclaimer": False,
             "heading_count": 1, "line_count": 5})
        self.assertFalse(ok)

    def test_invariants_preserved_rejects_structure_change(self):
        ok, _ = tpe.invariants_preserved(
            {"anchor": True, "warn_count": 0, "disclaimer": True,
             "heading_count": 4, "line_count": 30},
            {"anchor": True, "warn_count": 0, "disclaimer": True,
             "heading_count": 3, "line_count": 30})
        self.assertFalse(ok)


class FallbackTest(unittest.TestCase):
    """Механизм ОТКАЗЫВАЕТСЯ (None) → caller падает на регенерацию (без регресса)."""

    def test_key_not_in_protocol_returns_none(self):
        # «Спикер 2»-ключ (в синтезированном протоколе таких нет) → None.
        self.assertIsNone(
            tpe.targeted_name_reissue(_protocol(), {"Спикер 2": "Мария"}, ["..."]))

    def test_empty_remap_returns_none(self):
        self.assertIsNone(tpe.targeted_name_reissue(_protocol(), {}, ["..."]))

    def test_identity_remap_returns_none(self):
        self.assertIsNone(
            tpe.targeted_name_reissue(_protocol(), {OLD: OLD}, ["..."]))

    def test_empty_protocol_returns_none(self):
        self.assertIsNone(tpe.targeted_name_reissue("", {OLD: NEW}, ["..."]))

    def test_pure_swap_falls_back_to_regen(self):
        # Своп A↔B (взаимная путаница) — НЕ точечный путь, остаётся Ф4б-регенерации.
        old = _protocol()
        self.assertIsNone(
            tpe.targeted_name_reissue(
                old, {"Илья Рыбалка": "Михаил Еремеев",
                      "Михаил Еремеев": "Илья Рыбалка"}, ["перепутал местами"]))

    def test_mixed_oneway_and_swap_keeps_oneway(self):
        # Смешанный remap: своп-пара отсеивается, одностороннее переименование берётся.
        old = _protocol()
        res = tpe.targeted_name_reissue(
            old,
            {"Илья Рыбалка": "Ольга Сонина", "Ольга Сонина": "Илья Рыбалка",
             "Михаил Еремеев": "Михаил Саргин"},
            ["вообще перепутал"])
        self.assertIsNotNone(res)
        new, _ = res
        # Своп-пара (Илья↔Ольга) НЕ тронута, односторонняя Еремеев→Саргин применена.
        self.assertNotIn("Михаил Еремеев", new)
        self.assertIn("Илья Рыбалка", new)
        self.assertIn("Ольга Сонина", new)


class SwapSafetyTest(unittest.TestCase):
    """Своп A↔B за один проход не схлопывается (как remap_transcript_speakers)."""

    def test_swap_single_pass(self):
        text = (
            "#протоколвстречи 01.01.2026\n\n**Участники:** Илья, Пётр\n\n---\n\n"
            "## Тема\n\n▪️ Илья сказал X, Пётр ответил Y, снова Илья и Пётр.\n"
        )
        new = tpe.apply_targeted_name_edit(text, {"Илья": "Пётр", "Пётр": "Илья"}, "broad")
        self.assertIsNotNone(new)
        # Илья и Пётр поменялись местами во ВСЕХ вхождениях, без схлопывания.
        self.assertIn("▪️ Пётр сказал X, Илья ответил Y, снова Пётр и Илья.", new)

    def test_longer_name_matched_first(self):
        # «Михаил Еремеев» матчится раньше «Михаил» (сортировка по длине).
        text = (
            "#протоколвстречи 01.01.2026\n\n**Участники:** Михаил Еремеев\n\n---\n\n"
            "## Тема\n\n▪️ Михаил Еремеев вёл встречу.\n"
        )
        new = tpe.apply_targeted_name_edit(
            text, {"Михаил Еремеев": "Михаил Саргин"}, "broad")
        self.assertIn("Михаил Саргин вёл встречу", new)
        self.assertNotIn("Еремеев", new)


class WordBoundaryTest(unittest.TestCase):
    """Границы слова: имя не матчит подстроку другого слова."""

    def test_no_substring_match(self):
        text = (
            "#протоколвстречи 01.01.2026\n\n**Участники:** Иван\n\n---\n\n"
            "## Тема\n\n▪️ Иванов и Иван — разные люди; Иванова тоже.\n"
        )
        new = tpe.apply_targeted_name_edit(text, {"Иван": "Пётр"}, "broad")
        # Только отдельный токен «Иван», не «Иванов»/«Иванова».
        self.assertIn("Иванов и Пётр — разные люди; Иванова тоже", new)


class DiffBoundTest(unittest.TestCase):
    """РИСК5: контроль объёма диффом — отвергаем не-точечные изменения."""

    def test_bounded_broad_ok(self):
        old = _protocol()
        new, _ = tpe.targeted_name_reissue(old, {OLD: NEW}, ["вообще"])
        ok, reason = tpe.diff_is_bounded(old, new, {OLD: NEW}, "broad")
        self.assertTrue(ok, reason)

    def test_rejects_non_name_line_change(self):
        old = "#протоколвстречи 01.01.2026\nстрока A\nМихаил Еремеев тут\n"
        # Изменили ПОСТОРОННЮЮ строку (не подменой имени).
        new = "#протоколвстречи 01.01.2026\nстрока Б\nМихаил Саргин тут\n"
        ok, reason = tpe.diff_is_bounded(old, new, {OLD: NEW}, "broad")
        self.assertFalse(ok)

    def test_rejects_line_count_change(self):
        old = "#протоколвстречи 01.01.2026\nМихаил Еремеев\n"
        new = "#протоколвстречи 01.01.2026\nМихаил Саргин\nлишняя строка\n"
        ok, _ = tpe.diff_is_bounded(old, new, {OLD: NEW}, "broad")
        self.assertFalse(ok)

    def test_narrow_rejects_multi_line(self):
        old = "x\nМихаил Еремеев\nМихаил Еремеев\n"
        new = "x\nМихаил Саргин\nМихаил Саргин\n"
        ok, _ = tpe.diff_is_bounded(old, new, {OLD: NEW}, "narrow")
        self.assertFalse(ok)

    def test_reason_carries_no_protocol_text(self):
        # Опасная тройка: reason на провале диффа (он логируется) НЕ содержит
        # содержимого протокола/реплик — только факт выхода за рамки.
        secret = "СЕКРЕТНАЯ_СУММА_42млн_конфиденциально"
        old = f"#протоколвстречи\n{secret}\nМихаил Еремеев тут\n"
        new = f"#протоколвстречи\nдругая строка\nМихаил Саргин тут\n"
        ok, reason = tpe.diff_is_bounded(old, new, {OLD: NEW}, "broad")
        self.assertFalse(ok)
        self.assertNotIn(secret, reason)
        self.assertNotIn("другая строка", reason)


class ConstantsDriftTest(unittest.TestCase):
    """Локальные зеркала инвариантов совпадают с источником (защита от дрейфа)."""

    def test_review_flag_marker_matches(self):
        from notary.lib import llm_postprocess as lp
        self.assertEqual(tpe.REVIEW_FLAG_MARKER, lp.REVIEW_FLAG_MARKER)

    def test_disclaimer_sentinel_matches(self):
        from notary.lib import protocol_to_tg
        self.assertEqual(
            tpe.DISCLAIMER_SENTINEL, protocol_to_tg.PROTOCOL_DISCLAIMER_SENTINEL)

    def test_anchor_matches_generate_protocol(self):
        import inspect
        from notary.lib import llm_postprocess as lp
        # Якорь именованной константы не имеет — сверяем, что он фигурирует в
        # валидации generate_protocol (если переименуют — тест упадёт).
        self.assertIn(tpe.PROTOCOL_ANCHOR, inspect.getsource(lp.generate_protocol))


# ---------------------------------------------------------------------------
# Интеграция: достижимость из реального триггера reissue_one
# ---------------------------------------------------------------------------
def _write_meeting(tmp: Path) -> tuple[Path, Path, Path, dict]:
    """Раскладка встречи: транскрипт + протокол + meta.json (delivered-запись)."""
    series_dir = tmp / "anzhee-koord"
    series_dir.mkdir(parents=True)
    date = "2026-06-09"
    transcript = series_dir / f"{date}.md"
    transcript.write_text(
        "**[00:01] Илья Рыбалка:** Контейнер на таможне, веду брокера.\n\n"
        "**[00:02] Ольга Сонина:** Документы сверила.\n",
        encoding="utf-8",
    )
    protocol = series_dir / f"{date}-protokol.md"
    protocol.write_text(
        "#протоколвстречи 09.06.2026\n\n"
        "**Встреча:** Координация Anzhee.\n\n"
        "**Участники:** Илья Рыбалка, Ольга Сонина\n\n"
        "> ℹ️ _Авторство реплик восстановлено автоматически._\n\n"
        "---\n\n"
        "## 1) Поставки\n\n"
        "▪️ Илья Рыбалка ведёт переговоры с брокером.\n\n"
        "## 📌 Задачи\n\n"
        "**Илья Рыбалка**\n\n"
        "- Получить дату растаможки.\n\n"
        "## ⚠️ Проверить\n\n"
        "⚠️ авторство под вопросом, поправьте: Илья Рыбалка\n",
        encoding="utf-8",
    )
    meta = {
        "series": "anzhee-koord",
        "date": date,
        "transcript_path": str(transcript),
        "protocol_path": str(protocol),
        "expectedParticipants": ["Илья Рыбалка", "Ольга Сонина", "Пётр Сидоров"],
        "participants": ["Илья Рыбалка", "Ольга Сонина"],
        "delivered": [{
            "chat_id": 555, "message_ids": [42], "at": "2026-06-09T10:00:00+00:00",
            "content_hash": "deadbeef", "revision": 0,
        }],
    }
    meta_path = series_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return transcript, protocol, meta_path, meta


class ReissueIntegrationTest(unittest.TestCase):
    """reissue_one: чистая правка авторства → точечно, без регенерации."""

    def setUp(self):
        from notary.lib import feedback_reissue
        self.fr = feedback_reissue
        self._td = tempfile.TemporaryDirectory(prefix="ф4-targeted-")
        tmp = Path(self._td.name)
        self.transcript, self.protocol, self.meta_path, _ = _write_meeting(tmp)
        self.old_protocol = self.protocol.read_text(encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _state(self, text: str) -> dict:
        return {
            "series": "anzhee-koord", "date": "2026-06-09",
            "meta_path": str(self.meta_path), "chat_id": 555,
            "feedback_id": "fb-test-1",
            "edits": [{"author": "Илья", "text": text}],
        }

    def test_pure_authorship_skips_regeneration(self):
        captured = {}

        def _gen(*a, **k):
            raise AssertionError("generate_fn НЕ должна вызываться на точечном пути")

        def _redeliver(meeting_meta, old_text, new_text, **k):
            captured["old"] = old_text
            captured["new"] = new_text
            return {"status": "sent", "chat_id": 555, "message_ids": [99],
                    "revision": 1, "content_hash": "x"}

        res = self.fr.reissue_one(
            self._state("вообще перепутал человека, не Илья, а Пётр"),
            generate_fn=_gen, redeliver_fn=_redeliver,
            save_version_fn=lambda *_a, **_k: None,
        )
        self.assertEqual(res.get("status"), "sent")
        # Точечная правка: Илья Рыбалка → Пётр Сидоров во всём протоколе.
        new = captured["new"]
        self.assertIn("Пётр Сидоров", new)
        self.assertNotIn("Илья Рыбалка", new)
        # Инварианты целы (R16).
        self.assertTrue(new.lstrip().startswith("#протоколвстречи"))
        self.assertIn("⚠️", new)
        self.assertIn("Авторство реплик", new)
        # Остальной текст стабилен: ровно подмена имени, без плавания.
        ok, reason = tpe.diff_is_bounded(
            self.old_protocol, new,
            {"Илья Рыбалка": "Пётр Сидоров"}, "broad")
        self.assertTrue(ok, reason)

    def test_content_edit_falls_back_to_regen(self):
        called = {"gen": 0}
        regen_text = (
            "#протоколвстречи 09.06.2026\n\n**Участники:** Илья Рыбалка, Ольга Сонина\n\n"
            "---\n\n## Тема\n\n▪️ Регенерация.\n"
        )

        def _gen(path, meta, sid):
            called["gen"] += 1
            return regen_text

        def _redeliver(meeting_meta, old_text, new_text, **k):
            return {"status": "sent", "chat_id": 555, "message_ids": [99],
                    "revision": 1, "content_hash": "x"}

        res = self.fr.reissue_one(
            self._state("добавь задачу Ольге: проверить склад до среды"),
            generate_fn=_gen, redeliver_fn=_redeliver,
            save_version_fn=lambda *_a, **_k: None,
        )
        # Контентная правка (нет remap) → точечный путь пропущен, регенерация была.
        self.assertEqual(called["gen"], 1)
        self.assertEqual(res.get("status"), "sent")


if __name__ == "__main__":
    unittest.main()
