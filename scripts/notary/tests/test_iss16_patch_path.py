# -*- coding: utf-8 -*-
"""Ф2 (ISS-16, R3/R4/R5b/R6/R8/R9/R10/R12): LLM патч-путь точечной правки протокола.

Корень (план 2026-06-19 protocol-patch-edits-not-regen). Детерминированные ярусы
берут лишь механические правки; любая РАЗГОВОРНАЯ (контентная или авторская со
склонением) сегодня уходит в полную регенерацию → проза переписывается целиком.
Ф2: Claude возвращает СТРУКТУРНЫЙ ПАТЧ, код применяет его к существующему протоколу.

R3  — контентная правка → СТРУКТУРНЫЙ ПАТЧ (op+anchor+value), не текст; нетронутое
      побайтно идентично; в логе tier=patch.
R4  — якорь обязан быть точной УНИКАЛЬНОЙ подстрокой; нет/дважды → отказ → фолбэк.
R5b — НОВЫЙ масштаб-валидатор: якорь/⚠️/секции целы; insert/delete РАЗРЕШЕНЫ.
R6  — отказ патча → уведомление «⚠️ Не смог поправить точечно …» + регенерация.
R8  — опасная тройка: meta/лог без текста реплик; сырой ответ Claude не персистим.
R9  — разговорное авторство → минимальный патч (только атрибуция), без регенерации.
R10 — авторский патч исключён из контент-обучения (glossary/роли не пополняются).
R12 — результат течёт через слот targeted_new_text (тот же redeliver/архив; на сбое
      доставки на диске остаётся оригинал).

Запуск: python3 -m unittest tests.test_iss16_patch_path (system python3.9, без venv).
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

from notary.lib import protocol_patch as pp  # noqa: E402
from notary.lib import patch_telemetry as tlm  # noqa: E402


def _protocol() -> str:
    """Протокол с двумя «500к» (проверка уникальности якоря), якорем/⚠️/секциями/атрибуцией."""
    return (
        "#протоколвстречи 20.06.2026\n"
        "\n"
        "**Встреча:** Планёрка.\n"
        "\n"
        "**Участники:** Илья Рыбалка, Пётр Сидоров\n"
        "\n"
        "> ℹ️ _Авторство реплик восстановлено автоматически — если перепутали, поправьте._\n"
        "\n"
        "---\n"
        "\n"
        "## 1) Решения\n"
        "\n"
        "▪️ Пётр Сидоров: бюджет на квартал 500к.\n"
        "\n"
        "## 📌 Задачи\n"
        "\n"
        "**Пётр Сидоров**\n"
        "\n"
        "- Подготовить смету до пятницы.\n"
        "\n"
        "## ⚠️ Проверить\n"
        "\n"
        "⚠️ сумма под вопросом: 500к\n"
    )


_KNOWN = ["Илья Рыбалка", "Пётр Сидоров", "Илья", "Пётр"]


# ===========================================================================
# parse_patch_response (R8: сырой ответ не персистим — только распарсенный патч)
# ===========================================================================
class ParsePatchTest(unittest.TestCase):
    def test_valid_object(self):
        p = pp.parse_patch_response('{"edit_kind":"content","ops":[{"op":"replace","anchor":"a","value":"b"}]}')
        self.assertEqual(p["edit_kind"], "content")
        self.assertEqual(p["ops"], [{"op": "replace", "anchor": "a", "value": "b"}])

    def test_markdown_fence_stripped(self):
        p = pp.parse_patch_response('```json\n{"ops":[{"op":"delete","anchor":"x"}]}\n```')
        self.assertEqual(p["ops"][0]["op"], "delete")

    def test_prose_wrapped_first_object(self):
        p = pp.parse_patch_response('Вот патч: {"ops":[{"op":"delete","anchor":"x"}]} — готово')
        self.assertIsNotNone(p)
        self.assertEqual(p["edit_kind"], "content")  # дефолт

    def test_unknown_op_rejected(self):
        self.assertIsNone(pp.parse_patch_response('{"ops":[{"op":"frobnicate","anchor":"x"}]}'))

    def test_replace_without_value_rejected(self):
        self.assertIsNone(pp.parse_patch_response('{"ops":[{"op":"replace","anchor":"x"}]}'))

    def test_insert_without_value_rejected(self):
        self.assertIsNone(pp.parse_patch_response('{"ops":[{"op":"insert_after","anchor":"x"}]}'))

    def test_empty_anchor_rejected(self):
        self.assertIsNone(pp.parse_patch_response('{"ops":[{"op":"delete","anchor":""}]}'))

    def test_ops_not_list_rejected(self):
        self.assertIsNone(pp.parse_patch_response('{"ops":"nope"}'))

    def test_non_dict_rejected(self):
        self.assertIsNone(pp.parse_patch_response('[1,2,3]'))

    def test_garbage_rejected(self):
        self.assertIsNone(pp.parse_patch_response('не json вовсе'))
        self.assertIsNone(pp.parse_patch_response(''))

    def test_delete_ignores_value(self):
        p = pp.parse_patch_response('{"ops":[{"op":"delete","anchor":"x","value":"ignored"}]}')
        self.assertEqual(p["ops"][0]["value"], "")

    def test_too_many_ops_rejected(self):
        ops = ",".join('{"op":"delete","anchor":"a%d"}' % i for i in range(pp._MAX_OPS + 1))
        self.assertIsNone(pp.parse_patch_response('{"ops":[' + ops + ']}'))

    def test_oversized_anchor_rejected(self):
        big = "z" * (pp._MAX_ANCHOR_LEN + 1)
        self.assertIsNone(pp.parse_patch_response(json.dumps({"ops": [{"op": "delete", "anchor": big}]})))


# ===========================================================================
# R4 — валидатор якорей: точная УНИКАЛЬНАЯ подстрока
# ===========================================================================
class AnchorValidationTest(unittest.TestCase):
    def test_unique_anchor_ok(self):
        self.assertTrue(pp.validate_anchors(_protocol(), [{"op": "replace", "anchor": "квартал 500к", "value": "x"}]))

    def test_absent_anchor_rejected(self):
        self.assertFalse(pp.validate_anchors(_protocol(), [{"op": "replace", "anchor": "НЕТ ТАКОГО", "value": "x"}]))

    def test_duplicate_anchor_rejected(self):
        # «500к» встречается дважды → не уникален → отказ (R4 дословно).
        self.assertFalse(pp.validate_anchors(_protocol(), [{"op": "replace", "anchor": "500к", "value": "x"}]))

    def test_one_bad_among_good_rejects_all(self):
        ops = [{"op": "replace", "anchor": "квартал 500к", "value": "x"},
               {"op": "delete", "anchor": "НЕТ"}]
        self.assertFalse(pp.validate_anchors(_protocol(), ops))


# ===========================================================================
# Применятель: replace/delete/insert_before/insert_after + последовательность
# ===========================================================================
class ApplyPatchTest(unittest.TestCase):
    def test_replace(self):
        new = pp.apply_patch("abc DEF ghi", [{"op": "replace", "anchor": "DEF", "value": "XYZ"}])
        self.assertEqual(new, "abc XYZ ghi")

    def test_delete(self):
        new = pp.apply_patch("abc DEF ghi", [{"op": "delete", "anchor": " DEF"}])
        self.assertEqual(new, "abc ghi")

    def test_insert_after(self):
        new = pp.apply_patch("abc ghi", [{"op": "insert_after", "anchor": "abc", "value": " DEF"}])
        self.assertEqual(new, "abc DEF ghi")

    def test_insert_before(self):
        new = pp.apply_patch("abc ghi", [{"op": "insert_before", "anchor": "ghi", "value": "DEF "}])
        self.assertEqual(new, "abc DEF ghi")

    def test_sequential_ops(self):
        new = pp.apply_patch(
            "one two three",
            [{"op": "replace", "anchor": "one", "value": "1"},
             {"op": "replace", "anchor": "three", "value": "3"}],
        )
        self.assertEqual(new, "1 two 3")

    def test_nonunique_in_current_text_rejected(self):
        # Якорь уникален в исходнике, но первая op делает его неуникальным → отказ.
        new = pp.apply_patch(
            "A B",
            [{"op": "replace", "anchor": "A", "value": "B"},
             {"op": "replace", "anchor": "B", "value": "C"}],
        )
        self.assertIsNone(new)

    def test_noop_returns_none(self):
        self.assertIsNone(pp.apply_patch("abc", [{"op": "replace", "anchor": "abc", "value": "abc"}]))


# ===========================================================================
# R5b — НОВЫЙ масштаб-валидатор (insert/delete разрешены; секция/⚠️ — нет)
# ===========================================================================
class ScaleValidatorTest(unittest.TestCase):
    def test_single_insert_passes(self):
        old = _protocol()
        new = pp.apply_patch(old, [{"op": "insert_after", "anchor": "до пятницы.", "value": "\n\n- Ещё пункт."}])
        ok, reason = pp.patch_scale_ok(old, new)
        self.assertTrue(ok, reason)

    def test_single_delete_passes(self):
        old = _protocol()
        new = pp.apply_patch(old, [{"op": "delete", "anchor": "\n\n- Подготовить смету до пятницы."}])
        ok, reason = pp.patch_scale_ok(old, new)
        self.assertTrue(ok, reason)

    def test_removing_section_rejected(self):
        old = _protocol()
        new = pp.apply_patch(old, [{"op": "delete", "anchor": "## 1) Решения\n\n"}])
        ok, reason = pp.patch_scale_ok(old, new)
        self.assertFalse(ok)
        self.assertIn("секц", reason)

    def test_removing_warn_rejected(self):
        old = _protocol()
        new = pp.apply_patch(old, [{"op": "delete", "anchor": "⚠️ сумма под вопросом: 500к\n"}])
        ok, reason = pp.patch_scale_ok(old, new)
        self.assertFalse(ok)
        self.assertIn("⚠️", reason)

    def test_losing_header_anchor_rejected(self):
        old = _protocol()
        new = pp.apply_patch(old, [{"op": "replace", "anchor": "#протоколвстречи 20.06.2026",
                                     "value": "Протокол от 20.06"}])
        ok, reason = pp.patch_scale_ok(old, new)
        self.assertFalse(ok)
        self.assertIn("якор", reason)

    def test_strict_phase4_invariant_not_reused(self):
        # Гарантия плана: insert меняет число строк — строгий Ф4 invariants_preserved
        # отклонил бы (равенство строк), а R5b-валидатор пропускает.
        from notary.lib import targeted_protocol_edit as _tpe
        old = _protocol()
        new = pp.apply_patch(old, [{"op": "insert_after", "anchor": "до пятницы.", "value": "\n\n- Ещё."}])
        strict_ok, _ = _tpe.invariants_preserved(
            _tpe.protocol_invariants(old), _tpe.protocol_invariants(new))
        self.assertFalse(strict_ok, "строгий Ф4 ДОЛЖЕН отклонять insert (равенство строк)")
        self.assertTrue(pp.patch_scale_ok(old, new)[0], "R5b ДОЛЖЕН пропускать insert")


class BoundedDiffTest(unittest.TestCase):
    def test_small_change_bounded(self):
        old = _protocol()
        new = old.replace("500к.", "700к.")
        self.assertTrue(pp.diff_is_bounded(old, new)[0])

    def test_huge_rewrite_rejected(self):
        old = _protocol()
        new = old.split("\n")[0] + "\n" + "новая строка\n" * 60
        ok, reason = pp.diff_is_bounded(old, new)
        self.assertFalse(ok)

    def test_collapse_rejected(self):
        old = _protocol()
        new = old[:len(old) // 3]
        self.assertFalse(pp.diff_is_bounded(old, new)[0])


# ===========================================================================
# R10 — структурный детектор авторского патча
# ===========================================================================
class AuthorshipDetectorTest(unittest.TestCase):
    def test_name_only_replace_is_authorship(self):
        ops = [{"op": "replace", "anchor": "▪️ Пётр Сидоров: бюджет",
                "value": "▪️ Илья Рыбалка: бюджет"}]
        self.assertTrue(pp.is_authorship_patch(ops, _KNOWN))

    def test_content_replace_not_authorship(self):
        ops = [{"op": "replace", "anchor": "квартал 500к", "value": "квартал 700к"}]
        self.assertFalse(pp.is_authorship_patch(ops, _KNOWN))

    def test_insert_present_not_authorship(self):
        ops = [{"op": "replace", "anchor": "Пётр Сидоров", "value": "Илья Рыбалка"},
               {"op": "insert_after", "anchor": "x", "value": "y"}]
        self.assertFalse(pp.is_authorship_patch(ops, _KNOWN))

    def test_empty_known_names_not_authorship(self):
        ops = [{"op": "replace", "anchor": "Пётр Сидоров", "value": "Илья Рыбалка"}]
        self.assertFalse(pp.is_authorship_patch(ops, []))


# ===========================================================================
# targeted_patch_reissue — высокоуровневый вход (с инъекцией request_fn)
# ===========================================================================
# Патч-путь по дефолту ТЁМНЫЙ (ENABLE_PROTOCOL_PATCH OFF, активирует Ф3) — форсим ON
# ТОЛЬКО на время этих тестов (scoped, не течёт в другие файлы общего discover-процесса).
@mock.patch.dict(os.environ, {"ENABLE_PROTOCOL_PATCH": "1"})
class TargetedPatchReissueTest(unittest.TestCase):
    def test_content_patch_applies(self):
        old = _protocol()
        patch = {"edit_kind": "content",
                 "ops": [{"op": "replace", "anchor": "бюджет на квартал 500к", "value": "бюджет на квартал 700к"}]}
        res = pp.targeted_patch_reissue(old, ["бюджет не 500к, а 700к"], known_names=_KNOWN,
                                        request_fn=lambda o, e: patch)
        self.assertIsNotNone(res)
        new, meta = res
        self.assertIn("квартал 700к", new)
        # Нетронутое побайтно: только этот фрагмент изменён (R3).
        self.assertEqual(old.replace("бюджет на квартал 500к", "бюджет на квартал 700к"), new)
        self.assertIn("⚠️ сумма под вопросом: 500к", new)  # второе «500к» не тронуто
        self.assertFalse(meta["is_authorship"])

    def test_authorship_patch_flagged(self):
        old = _protocol()
        patch = {"edit_kind": "authorship",
                 "ops": [{"op": "replace", "anchor": "▪️ Пётр Сидоров: бюджет на квартал 500к",
                          "value": "▪️ Илья Рыбалка: бюджет на квартал 500к"}]}
        res = pp.targeted_patch_reissue(old, ["реплику про бюджет произнёс Илья"], known_names=_KNOWN,
                                        request_fn=lambda o, e: patch)
        self.assertIsNotNone(res)
        new, meta = res
        self.assertTrue(meta["is_authorship"])
        self.assertIn("▪️ Илья Рыбалка: бюджет", new)

    def test_bad_anchor_falls_back(self):
        patch = {"edit_kind": "content", "ops": [{"op": "replace", "anchor": "НЕТ", "value": "x"}]}
        self.assertIsNone(pp.targeted_patch_reissue(_protocol(), ["x"], request_fn=lambda o, e: patch))

    def test_empty_ops_falls_back(self):
        patch = {"edit_kind": "content", "ops": []}
        self.assertIsNone(pp.targeted_patch_reissue(_protocol(), ["непонятно"], request_fn=lambda o, e: patch))

    def test_request_returns_none_falls_back(self):
        self.assertIsNone(pp.targeted_patch_reissue(_protocol(), ["x"], request_fn=lambda o, e: None))

    def test_meta_carries_no_text(self):
        old = _protocol()
        patch = {"edit_kind": "content",
                 "ops": [{"op": "replace", "anchor": "квартал 500к", "value": "квартал 700к"}]}
        _new, meta = pp.targeted_patch_reissue(old, ["бюджет не тот"], known_names=_KNOWN,
                                               request_fn=lambda o, e: patch)
        blob = json.dumps(meta, ensure_ascii=False)
        self.assertNotIn("500", blob)
        self.assertNotIn("бюджет", blob)

    @mock.patch.dict(os.environ, {"ENABLE_PROTOCOL_PATCH": "0"})
    def test_disabled_gate_returns_none(self):
        # Гейт OFF → путь молчит (None), даже если request_fn вернул бы валидный патч.
        self.assertIsNone(pp.targeted_patch_reissue(
            _protocol(), ["x"], request_fn=lambda o, e: {"ops": [{"op": "delete", "anchor": "x"}]}))


# ===========================================================================
# Телеметрия ярусов (РИСК4)
# ===========================================================================
class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="iss16-tlm-")
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_counts_aggregate(self):
        tlm.record_tier("patch", patch_attempted=True, root=self.tmp)
        tlm.record_tier("patch", patch_attempted=True, root=self.tmp)
        tlm.record_tier("regen", patch_attempted=True, root=self.tmp)   # rejected
        tlm.record_tier("regen", patch_attempted=False, root=self.tmp)  # без попытки
        tlm.record_tier("deterministic", patch_attempted=False, root=self.tmp)
        agg = tlm.aggregate(root=self.tmp, window_days=None)
        self.assertEqual(agg["patch_applied"], 2)
        self.assertEqual(agg["patch_rejected"], 1)
        self.assertEqual(agg["regen_fallback"], 2)
        self.assertEqual(agg["deterministic"], 1)
        self.assertEqual(agg["patch_success_rate"], 2 / 3)

    def test_unknown_tier_ignored(self):
        self.assertFalse(tlm.record_tier("bogus", root=self.tmp))

    def test_empty_aggregate(self):
        agg = tlm.aggregate(root=self.tmp)
        self.assertEqual(agg["total"], 0)
        self.assertIsNone(agg["patch_success_rate"])


# ===========================================================================
# R6 — уведомление о фолбэке (точный текст в чат встречи)
# ===========================================================================
class FallbackNoticeTest(unittest.TestCase):
    def test_notice_text_matches_criterion(self):
        from notary.lib import feedback_reissue
        self.assertEqual(
            feedback_reissue.PATCH_FALLBACK_NOTICE,
            "⚠️ Не смог поправить точечно — пересобрал протокол заново, проверь, пожалуйста",
        )

    def test_default_notify_sends_to_chat(self):
        import os
        from notary.lib import feedback_reissue
        from notary.lib import telegram_api
        sent = {}

        def _fake_send(token, chat_id, text, **k):
            sent["token"] = token
            sent["chat_id"] = chat_id
            sent["text"] = text
            return {"message_id": 5}

        orig = telegram_api.send_message
        orig_tok = os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN")
        telegram_api.send_message = _fake_send
        os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = "T:123"
        try:
            ok = feedback_reissue._default_notify_fallback(777, "fb-1")
        finally:
            telegram_api.send_message = orig
            if orig_tok is None:
                del os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"]
            else:
                os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = orig_tok
        self.assertTrue(ok)
        self.assertEqual(sent["chat_id"], 777)
        self.assertEqual(sent["text"], feedback_reissue.PATCH_FALLBACK_NOTICE)

    def test_no_chat_id_returns_false(self):
        from notary.lib import feedback_reissue
        self.assertFalse(feedback_reissue._default_notify_fallback(None, "fb-1"))


# ===========================================================================
# Интеграция reissue_one — достижимость из боевого триггера (R3/R6/R9/R10/R12)
# ===========================================================================
# Патч-путь активируем (gate ON) ТОЛЬКО на время интеграционных тестов — scoped, как выше.
def _write_meeting(tmp: Path) -> tuple:
    series_dir = tmp / "iss16-patch"
    series_dir.mkdir(parents=True)
    date = "2026-06-20"
    transcript = series_dir / f"{date}.md"
    transcript.write_text(
        "**[00:01] Илья Рыбалка:** Обсудим бюджет на квартал.\n\n"
        "**[00:02] Пётр Сидоров:** Предлагаю 500к.\n",
        encoding="utf-8",
    )
    protocol = series_dir / f"{date}-protokol.md"
    protocol.write_text(_protocol(), encoding="utf-8")
    meta = {
        "series": "iss16-patch", "date": date,
        "transcript_path": str(transcript), "protocol_path": str(protocol),
        "expectedParticipants": ["Илья Рыбалка", "Пётр Сидоров"],
        "participants": ["Илья Рыбалка", "Пётр Сидоров"],
        "delivered": [{
            "chat_id": 777, "message_ids": [42], "at": "2026-06-20T10:00:00+00:00",
            "content_hash": "deadbeef", "revision": 0,
        }],
    }
    meta_path = series_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return transcript, protocol, meta_path


@mock.patch.dict(os.environ, {"ENABLE_PROTOCOL_PATCH": "1"})
class ReissuePatchIntegrationTest(unittest.TestCase):
    def setUp(self):
        from notary.lib import feedback_reissue
        self.fr = feedback_reissue
        self._td = tempfile.TemporaryDirectory(prefix="iss16-patch-")
        self.tmp = Path(self._td.name)
        self.transcript, self.protocol, self.meta_path = _write_meeting(self.tmp)
        self.old_protocol = self.protocol.read_text(encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _state(self, text: str, fid: str = "fb-patch-1") -> dict:
        return {
            "series": "iss16-patch", "date": "2026-06-20",
            "meta_path": str(self.meta_path), "chat_id": 777,
            "feedback_id": fid, "round": 1,
            "edits": [{"author": "Илья", "text": text}],
        }

    def _no_regen(self):
        def _gen(*a, **k):
            raise AssertionError("generate_fn НЕ должна вызываться на патч-пути (R3/R9)")
        return _gen

    def _capture_redeliver(self, captured, status="sent"):
        def _redeliver(meeting_meta, old_text, new_text, **k):
            captured["old"] = old_text
            captured["new"] = new_text
            return {"status": status, "chat_id": 777, "message_ids": [99],
                    "revision": 1, "content_hash": "x"}
        return _redeliver

    # ---- R3: контентная правка → tier=patch, без регенерации, нетронутое идентично ----
    def test_content_patch_skips_regeneration(self):
        captured = {}
        saved = {"n": 0}
        patch = {"edit_kind": "content",
                 "ops": [{"op": "replace", "anchor": "бюджет на квартал 500к",
                          "value": "бюджет на квартал 700к"}]}
        notified = {"n": 0}
        res = self.fr.reissue_one(
            self._state("бюджет был не 500к, а 700к"),
            root=self.tmp,
            generate_fn=self._no_regen(),
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: saved.__setitem__("n", saved["n"] + 1),
            patch_fn=lambda o, e: patch,
            notify_fn=lambda *a, **k: notified.__setitem__("n", notified["n"] + 1),
        )
        self.assertEqual(res.get("status"), "sent")
        new = captured["new"]
        # Только патч-фрагмент изменён, нетронутое побайтно (R3).
        self.assertEqual(self.old_protocol.replace("квартал 500к", "квартал 700к"), new)
        self.assertIn("⚠️ сумма под вопросом: 500к", new)  # второе «500к» цело
        self.assertEqual(saved["n"], 1, "R12: архив версии зван (слот targeted_new_text)")
        self.assertEqual(notified["n"], 0, "успешный патч → уведомления о фолбэке нет")
        # Телеметрия: tier=patch.
        agg = tlm.aggregate(root=self.tmp, window_days=None)
        self.assertEqual(agg["patch_applied"], 1)

    # ---- R9: разговорное авторство (детерминированный путь не разобрал) → патч ----
    def test_conversational_authorship_patch(self):
        captured = {}
        # Формулировка без «не A, а B»/стрелок/«поменяй» → parse_authorship_remap НЕ
        # разбирает (remap пуст) → правка в content_edits → патч-путь (R9).
        edit = "слушай, реплику про бюджет на самом деле произнёс Илья, Петру приписал по ошибке"
        patch = {"edit_kind": "authorship",
                 "ops": [{"op": "replace", "anchor": "▪️ Пётр Сидоров: бюджет на квартал 500к",
                          "value": "▪️ Илья Рыбалка: бюджет на квартал 500к"}]}
        res = self.fr.reissue_one(
            self._state(edit, fid="fb-auth-1"),
            root=self.tmp,
            generate_fn=self._no_regen(),
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=lambda o, e: patch,
        )
        self.assertEqual(res.get("status"), "sent")
        new = captured["new"]
        # Исправлена только атрибуция; прочее побайтно.
        self.assertEqual(
            self.old_protocol.replace("▪️ Пётр Сидоров: бюджет на квартал 500к",
                                      "▪️ Илья Рыбалка: бюджет на квартал 500к"),
            new)
        # R10: авторский патч НЕ выучен в glossary/роли.
        from notary.lib import feedback_learning
        self.assertEqual(feedback_learning.active_term_rules("iss16-patch", root=self.tmp), [])
        self.assertEqual(feedback_learning.active_global_spellings(root=self.tmp), [])

    # ---- R10: контрольный — БЕЗ авторской метки термин-правка УЧИТСЯ (exclusion точечна) ----
    def test_content_term_patch_still_learns(self):
        # «не РСЯ, а РЕЦ» — term-like контент-правка; remap пуст (РСЯ не спикер) → патч-путь.
        # is_authorship=False → правка идёт в обучение как раньше (не регресс).
        captured = {}
        patch = {"edit_kind": "content",
                 "ops": [{"op": "replace", "anchor": "квартал 500к", "value": "квартал 500к"}]}
        # Чтобы патч реально применился (а не noop), меняем чуть-чуть:
        patch["ops"][0]["value"] = "квартал 500 тыс"
        res = self.fr.reissue_one(
            self._state("в протоколе не РСЯ, а РЕЦ", fid="fb-term-1"),
            root=self.tmp,
            generate_fn=self._no_regen(),
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=lambda o, e: patch,
        )
        self.assertEqual(res.get("status"), "sent")
        from notary.lib import feedback_learning
        learned = feedback_learning.active_term_rules("iss16-patch", root=self.tmp)
        pairs = {(r["wrong"], r["right"]) for r in learned}
        self.assertIn(("РСЯ", "РЕЦ"), pairs, "контентная term-правка должна учиться (не регресс)")

    # ---- R6: патч не лёг → уведомление дословно + регенерация ----
    def test_fallback_notifies_and_regenerates(self):
        captured = {}
        gen_called = {"n": 0}
        regen_text = (
            "#протоколвстречи 20.06.2026\n\n**Участники:** Илья Рыбалка, Пётр Сидоров\n\n"
            "---\n\n## Тема\n\n▪️ Пересобрано заново.\n"
        )

        def _gen(path, meta, sid):
            gen_called["n"] += 1
            return regen_text

        notes = []
        res = self.fr.reissue_one(
            self._state("перефразируй вступление поживее"),
            root=self.tmp,
            generate_fn=_gen,
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=lambda o, e: None,  # патч не получился → фолбэк
            notify_fn=lambda chat_id, sid=None: notes.append((chat_id, sid)),
        )
        self.assertEqual(res.get("status"), "sent")
        self.assertEqual(gen_called["n"], 1, "патч не лёг → регенерация (R6)")
        self.assertEqual(captured["new"], regen_text)
        self.assertEqual(len(notes), 1, "патч пробовали и не лёг → одно уведомление (R6)")
        self.assertEqual(notes[0][0], 777, "уведомление в чат встречи")
        # Телеметрия: regen + rejected.
        agg = tlm.aggregate(root=self.tmp, window_days=None)
        self.assertEqual(agg["regen_fallback"], 1)
        self.assertEqual(agg["patch_rejected"], 1)

    # ---- R6: обычная регенерация БЕЗ попытки патча (remap/смесь) — без уведомления ----
    def test_no_notice_when_patch_not_attempted(self):
        # Смешанная (имя+контент): remap есть → патч-путь не трогаем (R7) → регенерация
        # как сегодня, БЕЗ уведомления о фолбэке (патч не пробовали).
        captured = {}
        gen_called = {"n": 0}

        def _gen(path, meta, sid):
            gen_called["n"] += 1
            return ("#протоколвстречи 20.06.2026\n\n**Участники:** Илья Рыбалка\n\n"
                    "---\n\n## Тема\n\n▪️ Регенерация.\n")

        notes = []
        state = {
            "series": "iss16-patch", "date": "2026-06-20",
            "meta_path": str(self.meta_path), "chat_id": 777,
            "feedback_id": "fb-mix-1", "round": 1,
            "edits": [
                {"author": "Илья", "text": "не Пётр, а Илья"},
                {"author": "Илья", "text": "и бюджет на самом деле был 600к по итогу"},
            ],
        }
        res = self.fr.reissue_one(
            state, root=self.tmp, generate_fn=_gen,
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=lambda o, e: (_ for _ in ()).throw(AssertionError("patch_fn НЕ должна зваться при remap")),
            notify_fn=lambda *a, **k: notes.append(a),
        )
        self.assertEqual(res.get("status"), "sent")
        self.assertEqual(gen_called["n"], 1)
        self.assertEqual(len(notes), 0, "патч не пробовали (remap) → нет уведомления о фолбэке")

    # ---- R12: на сбое доставки на диске остаётся ОРИГИНАЛ ----
    def test_delivery_failure_keeps_original_on_disk(self):
        patch = {"edit_kind": "content",
                 "ops": [{"op": "replace", "anchor": "квартал 500к", "value": "квартал 700к"}]}

        def _redeliver_fail(meeting_meta, old_text, new_text, **k):
            return {"status": "error", "error": "telegram down"}

        res = self.fr.reissue_one(
            self._state("бюджет не 500к, а 700к"),
            root=self.tmp,
            generate_fn=self._no_regen(),
            redeliver_fn=_redeliver_fail,
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=lambda o, e: patch,
        )
        self.assertNotEqual(res.get("status"), "sent")
        # Диск не тронут — оригинал цел (R12 ретрай-безопасность).
        self.assertEqual(self.protocol.read_text(encoding="utf-8"), self.old_protocol)


if __name__ == "__main__":
    unittest.main()
