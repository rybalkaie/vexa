# -*- coding: utf-8 -*-
"""Ф4 (ISS-17, R13/R14): СМЕШАННЫЙ / МНОГОПРАВОЧНЫЙ пакет правок → точечный перевыпуск.

Корень (план 2026-06-19 protocol-patch-edits-not-regen, фаза ISS-17). Пакет, где
намешаны правка авторства (распознана `parse_authorship_remap` → remap НЕпуст) И
контентные правки (content_edits НЕпуст), раньше выпадал из ВСЕХ ярусов: Ф4 name
требует `not content_edits`, Ф1 term и Ф2 patch требуют `not remap`. Итог — полная
LLM-регенерация всего протокола («плавание» текста). На бою смешанное — ЧАСТЫЙ случай
(встреча 06-22: 11 правок одним пакетом → 128/132 строк переписаны).

Фикс: новый смешанный ярус применяет правки ПОСЛЕДОВАТЕЛЬНО по одному протоколу —
(1) детерминированный name-remap (Ф4-движок, без LLM), затем (2) контент по
промежуточному протоколу: детерминированный term (Ф1) → не вышло → патч (Ф2).

R13 — смешанный пакет применяется точечно (имя + контент по одному протоколу), без
      полной регенерации; в логе `tier=mixed`.
R14 — ВСЁ-ИЛИ-РЕГЕНЕРАЦИЯ: если хоть один под-шаг не лёг — откат всего промежуточного
      результата и честная регенерация (regen применяет remap транскрипта + контент-
      инструкции, ничего не теряется).

Запуск: python3 -m unittest tests.test_iss17_mixed_batch (system python3.9, без venv).
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

from notary.lib import patch_telemetry as tlm  # noqa: E402


def _protocol() -> str:
    """Протокол с именем для remap (Илья Рыбалка), термином «фронтенд» и контент-якорем."""
    return (
        "#протоколвстречи 22.06.2026\n"
        "\n"
        "**Встреча:** Координация.\n"
        "\n"
        "**Участники:** Илья Рыбалка, Пётр Сидоров\n"
        "\n"
        "> ℹ️ _Авторство реплик восстановлено автоматически — если перепутали, поправьте._\n"
        "\n"
        "---\n"
        "\n"
        "## 1) Решения\n"
        "\n"
        "▪️ Решили переписать фронтенд на React.\n"
        "\n"
        "▪️ Пётр Сидоров: бюджет на квартал 500к.\n"
        "\n"
        "🔸 Сроки по фронтенд ⚠️ проверь.\n"
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


def _write_meeting(tmp: Path) -> tuple:
    series_dir = tmp / "iss17-mixed"
    series_dir.mkdir(parents=True)
    date = "2026-06-22"
    transcript = series_dir / f"{date}.md"
    transcript.write_text(
        "**[00:01] Илья Рыбалка:** Перепишем фронтенд.\n\n"
        "**[00:02] Пётр Сидоров:** Бюджет 500к.\n",
        encoding="utf-8",
    )
    protocol = series_dir / f"{date}-protokol.md"
    protocol.write_text(_protocol(), encoding="utf-8")
    meta = {
        "series": "iss17-mixed", "date": date,
        "transcript_path": str(transcript), "protocol_path": str(protocol),
        # Ольга Сонина — ожидаемый участник, НЕ текущий спикер → remap на неё oneway (не своп).
        "expectedParticipants": ["Илья Рыбалка", "Пётр Сидоров", "Ольга Сонина"],
        "participants": ["Илья Рыбалка", "Пётр Сидоров"],
        "delivered": [{
            "chat_id": 777, "message_ids": [42], "at": "2026-06-22T10:00:00+00:00",
            "content_hash": "deadbeef", "revision": 0,
        }],
    }
    meta_path = series_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return transcript, protocol, meta_path


@mock.patch.dict(os.environ, {"ENABLE_PROTOCOL_PATCH": "1"})
class MixedBatchTest(unittest.TestCase):
    def setUp(self):
        from notary.lib import feedback_reissue
        self.fr = feedback_reissue
        self._td = tempfile.TemporaryDirectory(prefix="iss17-mixed-")
        self.tmp = Path(self._td.name)
        self.transcript, self.protocol, self.meta_path = _write_meeting(self.tmp)
        self.old_protocol = self.protocol.read_text(encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _state(self, edits: list, fid: str = "fb-mix") -> dict:
        return {
            "series": "iss17-mixed", "date": "2026-06-22",
            "meta_path": str(self.meta_path), "chat_id": 777,
            "feedback_id": fid, "round": 1, "edits": edits,
        }

    def _no_regen(self):
        def _gen(*a, **k):
            raise AssertionError("смешанный точечный путь лёг → generate_fn НЕ должна зваться")
        return _gen

    def _capture_redeliver(self, captured, status="sent"):
        def _redeliver(meeting_meta, old_text, new_text, **k):
            captured["old"] = old_text
            captured["new"] = new_text
            return {"status": status, "chat_id": 777, "message_ids": [99],
                    "revision": 1, "content_hash": "x"}
        return _redeliver

    # ---- R13: имя + ДЕТЕРМИНИРОВАННЫЙ контент («замени X на Y») по одному протоколу ----
    def test_mixed_name_and_deterministic_content(self):
        captured = {}
        res = self.fr.reissue_one(
            self._state([
                {"author": "Илья", "text": "не Илья, а Ольга"},
                {"author": "Илья", "text": "замени фронтенд на frontend"},
            ], fid="fb-det"),
            root=self.tmp,
            generate_fn=self._no_regen(),
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=lambda o, e: (_ for _ in ()).throw(
                AssertionError("терсный контент → детерминированный term, патч не нужен")),
        )
        self.assertEqual(res.get("status"), "sent")
        new = captured["new"]
        # Обе части применены точечно по одному протоколу.
        self.assertNotIn("фронтенд", new)
        self.assertIn("frontend", new)
        self.assertNotIn("Илья", new)
        self.assertIn("Ольга Сонина", new)
        # Инварианты целы.
        self.assertTrue(new.lstrip().startswith("#протоколвстречи"))
        self.assertIn("⚠️", new)
        # Телеметрия: смешанный с детерминированным контентом → tier=deterministic.
        agg = tlm.aggregate(root=self.tmp, window_days=None)
        self.assertEqual(agg["deterministic"], 1)
        self.assertEqual(agg["patch_applied"], 0)

    # ---- R13: имя + РАЗГОВОРНЫЙ контент через патч по одному протоколу ----
    def test_mixed_name_and_patch_content(self):
        captured = {}
        # Контент-якорь «бюджет на квартал 500к» не содержит имени → name-remap (Илья→Ольга)
        # его не сдвинет, патч ляжет на промежуточный протокол.
        patch = {"edit_kind": "content",
                 "ops": [{"op": "replace", "anchor": "бюджет на квартал 500к",
                          "value": "бюджет на квартал 700к"}]}
        seen = {"mid": None}

        def _patch_fn(old, edits):
            seen["mid"] = old  # реально получаем ПРОМЕЖУТОЧНЫЙ (после name-remap) протокол
            return patch

        res = self.fr.reissue_one(
            self._state([
                {"author": "Илья", "text": "не Илья, а Ольга"},
                {"author": "Илья", "text": "бюджет был не 500к, а 700к по итогу обсуждения"},
            ], fid="fb-patch"),
            root=self.tmp,
            generate_fn=self._no_regen(),
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=_patch_fn,
        )
        self.assertEqual(res.get("status"), "sent")
        new = captured["new"]
        # Контентный патч лёг.
        self.assertIn("бюджет на квартал 700к", new)
        self.assertNotIn("бюджет на квартал 500к", new)
        # Имя переназначено (на промежуточном протоколе, который ушёл в патч).
        self.assertIn("Ольга Сонина", seen["mid"])
        self.assertNotIn("Илья", new)
        # Телеметрия: контент-патч в смеси → честно tier=patch (success-rate РИСК4).
        agg = tlm.aggregate(root=self.tmp, window_days=None)
        self.assertEqual(agg["patch_applied"], 1)

    # ---- R14: имя лёг, контент-патч НЕ лёг → ВСЁ-ИЛИ-РЕГЕНЕРАЦИЯ + уведомление ----
    def test_content_fails_falls_back_to_regen_with_notice(self):
        captured = {}
        gen_called = {"n": 0}
        regen_text = (
            "#протоколвстречи 22.06.2026\n\n**Участники:** Ольга Сонина, Пётр Сидоров\n\n"
            "---\n\n## Тема\n\n▪️ Пересобрано.\n"
        )

        def _gen(path, meta, sid):
            gen_called["n"] += 1
            return regen_text

        notes = []
        res = self.fr.reissue_one(
            self._state([
                {"author": "Илья", "text": "не Илья, а Ольга"},
                {"author": "Илья", "text": "перефразируй вступление поживее"},
            ], fid="fb-allor"),
            root=self.tmp,
            generate_fn=_gen,
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=lambda o, e: None,  # патч не лёг → весь смешанный пакет в регенерацию
            notify_fn=lambda chat_id, sid=None: notes.append((chat_id, sid)),
        )
        self.assertEqual(res.get("status"), "sent")
        self.assertEqual(gen_called["n"], 1, "контент не лёг → ВСЁ-ИЛИ-РЕГЕНЕРАЦИЯ (R14)")
        self.assertEqual(captured["new"], regen_text)
        # Патч пробовали (gate ON) и не лёг → одно уведомление (R6).
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], 777)
        agg = tlm.aggregate(root=self.tmp, window_days=None)
        self.assertEqual(agg["patch_rejected"], 1)

    # ---- R14: имя — СВОП (детерминированный name-путь отклоняет) → регенерация БЕЗ патча ----
    def test_swap_name_falls_back_before_patch(self):
        # «не Илья, а Пётр» (оба — спикеры) → parse_authorship_remap даёт СВОП; точечный
        # name-путь свопы не берёт (уходят в regen с remap транскрипта). В смешанном —
        # бэйлим ДО контент-шага: патч не пробуем → регенерация БЕЗ уведомления.
        gen_called = {"n": 0}

        def _gen(path, meta, sid):
            gen_called["n"] += 1
            return ("#протоколвстречи 22.06.2026\n\n**Участники:** Пётр Сидоров\n\n"
                    "---\n\n## Тема\n\n▪️ Пересобрано.\n")

        captured = {}
        notes = []
        res = self.fr.reissue_one(
            self._state([
                {"author": "Илья", "text": "не Илья, а Пётр"},
                {"author": "Илья", "text": "замени фронтенд на frontend"},
            ], fid="fb-swap"),
            root=self.tmp,
            generate_fn=_gen,
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
            patch_fn=lambda o, e: (_ for _ in ()).throw(
                AssertionError("name-своп → бэйл ДО контента, патч не пробуем")),
            notify_fn=lambda *a, **k: notes.append(a),
        )
        self.assertEqual(res.get("status"), "sent")
        self.assertEqual(gen_called["n"], 1, "своп имени → регенерация (R14)")
        self.assertEqual(len(notes), 0, "патч не пробовали → нет уведомления")

    # ---- Многоправочный смешанный пакет: имя + НЕСКОЛЬКО терсных контент-правок ----
    def test_multi_content_mixed(self):
        captured = {}
        res = self.fr.reissue_one(
            self._state([
                {"author": "Илья", "text": "не Илья, а Ольга"},
                {"author": "Илья", "text": "замени фронтенд на frontend"},
                {"author": "Илья", "text": "поменяй React на Vue"},
            ], fid="fb-multi"),
            root=self.tmp,
            generate_fn=self._no_regen(),
            redeliver_fn=self._capture_redeliver(captured),
            save_version_fn=lambda *_a, **_k: None,
        )
        self.assertEqual(res.get("status"), "sent")
        new = captured["new"]
        self.assertNotIn("фронтенд", new)
        self.assertIn("frontend", new)
        self.assertNotIn("React", new)
        self.assertIn("Vue", new)
        self.assertIn("Ольга Сонина", new)

    # ---- R12: на сбое доставки на диске остаётся ОРИГИНАЛ ----
    def test_delivery_failure_keeps_original(self):
        res = self.fr.reissue_one(
            self._state([
                {"author": "Илья", "text": "не Илья, а Ольга"},
                {"author": "Илья", "text": "замени фронтенд на frontend"},
            ], fid="fb-fail"),
            root=self.tmp,
            generate_fn=self._no_regen(),
            redeliver_fn=lambda m, o, n, **k: {"status": "error", "error": "telegram down"},
            save_version_fn=lambda *_a, **_k: None,
        )
        self.assertNotEqual(res.get("status"), "sent")
        # Диск не тронут — оригинал цел (R12).
        self.assertEqual(self.protocol.read_text(encoding="utf-8"), self.old_protocol)
        # Транскрипт тоже не закоммичен remapped (ретрай-безопасность).
        self.assertIn("Илья Рыбалка", self.transcript.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
