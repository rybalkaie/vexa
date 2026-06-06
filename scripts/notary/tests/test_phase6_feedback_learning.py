"""Ф6 — самообучение из правок участников (FB10).

Покрывает критерий «сделано» (РАЗМ1 / FB10):
  1. Терм-правка X→Y в серии S → активное правило → подаётся в промпт генерации
     СЛЕДУЮЩЕГО протокола серии S без повторной правки (`_format_protocol_user_prompt`).
  2. Вечерний дайджест: строка «Ватсон выучил: …» из лога; ответ «откати <что>» →
     запись неактивна (видно в логе) → следующая генерация прежнее правило НЕ применяет.
  3. Append-only лог НА СЕРИЮ — обратимый: только аппенд (learn/rollback/announced),
     прежние строки не правятся; откат переживает «рестарт» (перечтение с диска).
  + Боевой путь: правка применена в `reissue_one` (status=sent) → лог выучен.
  + Негатив: контент-правка тела одной встречи правилом НЕ становится.
  + Гейт `ENABLE_FEEDBACK_LEARNING=0` → ничего не учится/не подаётся.

Запуск: python3 -m unittest tests.test_phase6_feedback_learning (system python3.9, без venv).
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
from notary.lib import feedback_state, feedback_reissue  # noqa: E402
from notary.lib import llm_postprocess as lp  # noqa: E402


PROTO_OLD = "#протоколвстречи\n\n**Участники:** Михаил\n\n## 1) Тема\n▪️ старое\n"
PROTO_NEW = "#протоколвстречи\n\n**Участники:** Михаил\n\n## 1) Тема\n▪️ новое\n"


# ===========================================================================
# Извлечение правила: терм-замена vs контент-правка
# ===========================================================================
class TestExtract(unittest.TestCase):
    def test_arrow_term_pair(self):
        out = fl.extract_learned_terms("Гарсия → Гарсиа")
        self.assertEqual(out, [{"wrong": "Гарсия", "right": "Гарсиа"}])

    def test_arrow_ascii(self):
        out = fl.extract_learned_terms("Энвитон -> ENVYTON")
        self.assertEqual(out, [{"wrong": "Энвитон", "right": "ENVYTON"}])

    def test_ne_x_a_y_abbrev(self):
        out = fl.extract_learned_terms("это не РСЯ, а РЕЦ")
        self.assertEqual(out, [{"wrong": "РСЯ", "right": "РЕЦ"}])

    def test_zameni_x_na_y(self):
        out = fl.extract_learned_terms("замени Алтроникс на ALTRONIX")
        self.assertEqual(out, [{"wrong": "Алтроникс", "right": "ALTRONIX"}])

    def test_pishetsya(self):
        out = fl.extract_learned_terms("Саргин пишется Саргинов")
        self.assertEqual(out, [{"wrong": "Саргин", "right": "Саргинов"}])

    def test_content_edit_not_a_rule(self):
        # «131 не под досмотром, а на доставке» — контент про пункт 131, не терм.
        self.assertEqual(fl.extract_learned_terms("131 не под досмотром, а на доставке"), [])

    def test_addition_not_a_rule(self):
        self.assertEqual(
            fl.extract_learned_terms("забыли добавить, что обучение было на прошлой неделе"), [])

    def test_plain_text_not_a_rule(self):
        self.assertEqual(fl.extract_learned_terms("спасибо, всё верно"), [])

    def test_same_term_not_a_rule(self):
        self.assertEqual(fl.extract_learned_terms("РСЯ → РСЯ"), [])

    def test_is_term_like(self):
        self.assertTrue(fl._is_term_like("РСЯ"))
        self.assertTrue(fl._is_term_like("Гарсиа"))
        self.assertTrue(fl._is_term_like("ENVYTON"))
        self.assertFalse(fl._is_term_like("под досмотром"))
        self.assertFalse(fl._is_term_like("131"))
        self.assertFalse(fl._is_term_like("на доставке"))


# ===========================================================================
# База: песочница каталога обучения
# ===========================================================================
class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "_feedback_edits"
        self.protocols = self.base / "protocols"
        self.protocols.mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.protocols),
            "ENABLE_FEEDBACK_LEARNING": "1",
            "TELEGRAM_NOTARIUS_BOT_TOKEN": "test-token",
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _learn(self, series, text, *, author="Михаил", fid=None):
        state = {"series": series, "date": "2026-06-02",
                 "feedback_id": fid or feedback_state.build_feedback_id(series, "2026-06-02", -1),
                 "round": 1}
        return fl.record_learning_from_edits(state, [{"author": author, "text": text}], root=self.root)


# ===========================================================================
# Критерий 1 — выученный терм попадает в промпт генерации следующего протокола
# ===========================================================================
class TestReachesGeneration(_Base):
    def test_learned_term_in_next_generation_prompt(self):
        learned = self._learn("coord", "это не РСЯ, а РЕЦ")
        self.assertEqual(len(learned), 1)
        # Блок выученного непустой и содержит правую часть.
        block = fl.format_learned_terms_block("coord", root=self.root)
        self.assertIn("РЕЦ", block)
        self.assertIn("выученные исправления терминов", block.lower())
        # РЕАЛЬНАЯ инъекция в промпт генерации (без мока): env-root = self.root.
        prompt = lp._format_protocol_user_prompt("00:00 спикер: текст\n", {"series": "coord", "date": "2026-06-09"})
        self.assertIn("РЕЦ", prompt)
        self.assertIn("«РСЯ» → пиши «РЕЦ»", prompt)

    def test_other_series_not_affected(self):
        self._learn("coord", "Гарсия → Гарсиа")
        # Другая серия — блок пуст, в промпт ничего не подмешано.
        self.assertEqual(fl.format_learned_terms_block("sales", root=self.root), "")
        prompt = lp._format_protocol_user_prompt("x\n", {"series": "sales"})
        self.assertNotIn("Гарсиа", prompt)

    def test_no_series_no_block(self):
        self.assertEqual(fl.format_learned_terms_block(None, root=self.root), "")
        self.assertEqual(fl.format_learned_terms_block("", root=self.root), "")


# ===========================================================================
# Критерий 2 — дайджест «Ватсон выучил …» + откат «откати <что>»
# ===========================================================================
class TestDigestAndRollback(_Base):
    def test_digest_line_and_rollback_reverts(self):
        self._learn("coord", "это не РСЯ, а РЕЦ")
        # Дайджест содержит строку «Ватсон выучил …» и термин.
        text, ids = fl.format_digest_block(root=self.root)
        self.assertIn("Ватсон выучил", text)
        self.assertIn("РЕЦ", text)
        self.assertEqual(len(ids), 1)
        # Откат «откати РЕЦ» → правило неактивно, прежнее поведение возвращается.
        rolled = fl.apply_rollback_reply("откати РЕЦ", root=self.root)
        self.assertEqual(len(rolled), 1)
        self.assertEqual(fl.active_rules("coord", root=self.root), [])
        # Следующая генерация прежнее правило НЕ применяет.
        self.assertEqual(fl.format_learned_terms_block("coord", root=self.root), "")
        prompt = lp._format_protocol_user_prompt("x\n", {"series": "coord"})
        self.assertNotIn("РЕЦ", prompt)

    def test_rollback_only_with_trigger(self):
        self._learn("coord", "Гарсия → Гарсиа")
        # Текст без триггера отката не трогает реестр, даже если упомянут термин.
        self.assertEqual(fl.apply_rollback_reply("Гарсиа верно, спасибо", root=self.root), [])
        self.assertEqual(len(fl.active_rules("coord", root=self.root)), 1)

    def test_announced_not_repeated(self):
        self._learn("coord", "это не РСЯ, а РЕЦ")
        text, ids = fl.format_digest_block(root=self.root)
        self.assertTrue(text)
        fl.mark_announced(ids, root=self.root)
        # Повторный дайджест уже не озвучивает то же правило.
        text2, ids2 = fl.format_digest_block(root=self.root)
        self.assertEqual(text2, "")
        self.assertEqual(ids2, [])

    def test_rollback_visible_in_log_history(self):
        self._learn("coord", "это не РСЯ, а РЕЦ")
        fl.apply_rollback_reply("откати РЕЦ", root=self.root)
        path = fl.series_log_path("coord", root=self.root)
        ops = [json.loads(l)["op"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        # Прежняя learn-строка не удалена; добавлена rollback (история обратима).
        self.assertIn("learn", ops)
        self.assertIn("rollback", ops)


# ===========================================================================
# Критерий 3 — append-only лог на серию, обратимый, переживает рестарт
# ===========================================================================
class TestAppendOnlyReversible(_Base):
    def test_append_only_and_survives_restart(self):
        self._learn("coord", "Гарсия → Гарсиа")
        path = fl.series_log_path("coord", root=self.root)
        self.assertTrue(path.is_file())
        first = path.read_text(encoding="utf-8")
        self.assertEqual(first.count("\n"), 1)  # одна learn-строка

        # Откат — НОВАЯ строка, прежняя learn байт-в-байт на месте (append-only).
        fl.apply_rollback_reply("откати Гарсиа", root=self.root)
        after = path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(first))  # старое содержимое нетронуто
        self.assertEqual(after.count("\n"), 2)

        # «Рестарт» = перечтение с диска свежим вызовом → откат сохранён.
        self.assertEqual(fl.active_rules("coord", root=self.root), [])

    def test_relearn_after_rollback_reactivates(self):
        self._learn("coord", "Гарсия → Гарсиа")
        fl.apply_rollback_reply("откати Гарсиа", root=self.root)
        self.assertEqual(fl.active_rules("coord", root=self.root), [])
        # Повторное обучение тем же термом → правило снова активно (тот же id).
        self._learn("coord", "Гарсия → Гарсиа")
        active = fl.active_rules("coord", root=self.root)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["right"], "Гарсиа")

    def test_idempotent_no_duplicate_while_active(self):
        self._learn("coord", "Гарсия → Гарсиа")
        # Повторная та же правка пока правило активно → не плодим learn-событие.
        out = self._learn("coord", "Гарсия → Гарсиа")
        self.assertEqual(out, [])
        path = fl.series_log_path("coord", root=self.root)
        learns = [l for l in path.read_text(encoding="utf-8").splitlines()
                  if l.strip() and json.loads(l)["op"] == "learn"]
        self.assertEqual(len(learns), 1)


# ===========================================================================
# Боевой путь: применённая правка (reissue_one, status=sent) → лог выучен
# ===========================================================================
class TestLearnFromReissue(_Base):
    def _write_meeting(self, series="coord", date="2026-06-02", chat_id=-1001, mids=(101, 102)):
        d = self.protocols / series
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{date}.md").write_text("00:00 Михаил: текст\n", encoding="utf-8")
        (d / f"{date}-protokol.md").write_text(PROTO_OLD, encoding="utf-8")
        meta = {"series": series, "date": date, "expectedParticipants": ["Михаил"],
                "delivered": [{"chat_id": chat_id, "message_ids": list(mids), "at": "2026-06-02T11:00:00Z"}]}
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return d / "meta.json"

    def test_applied_term_edit_is_learned(self):
        meta_path = self._write_meeting()
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        st = {"feedback_id": fid, "series": "coord", "date": "2026-06-02", "chat_id": -1001,
              "meta_path": str(meta_path), "protocol_message_ids": [101, 102], "round": 1,
              "status": "reissuing", "reissue_attempts": 0,
              "edits": [{"author": "Михаил Саргин", "text": "это не РСЯ, а РЕЦ", "tg_message_id": 555}]}
        res = feedback_reissue.reissue_one(
            st, root=self.root,
            generate_fn=lambda *a, **k: PROTO_NEW,
            redeliver_fn=lambda *a, **k: {"status": "sent", "message_ids": [9001], "revision": 1},
            save_version_fn=lambda p: p,
        )
        self.assertEqual(res["status"], "sent")
        # Боевой триггер: применённая терм-правка осела в обучаемом логе серии.
        active = fl.active_rules("coord", root=self.root)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["wrong"], "РСЯ")
        self.assertEqual(active[0]["right"], "РЕЦ")

    def test_no_change_does_not_learn(self):
        # Если перевыпуск не доставлен (no-change), обучения нет (учим только sent).
        meta_path = self._write_meeting()
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        st = {"feedback_id": fid, "series": "coord", "date": "2026-06-02", "chat_id": -1001,
              "meta_path": str(meta_path), "protocol_message_ids": [101, 102], "round": 1,
              "status": "reissuing", "reissue_attempts": 0,
              "edits": [{"author": "Михаил", "text": "Гарсия → Гарсиа", "tg_message_id": 7}]}
        res = feedback_reissue.reissue_one(
            st, root=self.root,
            generate_fn=lambda *a, **k: PROTO_OLD,  # == old → no-change, доставки нет
            redeliver_fn=lambda *a, **k: {"status": "sent"},
            save_version_fn=lambda p: p,
        )
        self.assertEqual(res["status"], "no-change")
        self.assertEqual(fl.active_rules("coord", root=self.root), [])


# ===========================================================================
# Гейт фичи
# ===========================================================================
class TestGate(_Base):
    def test_disabled_learns_nothing(self):
        with mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_LEARNING": "0"}):
            out = self._learn("coord", "это не РСЯ, а РЕЦ")
            self.assertEqual(out, [])
            self.assertFalse(fl.series_log_path("coord", root=self.root).exists())
            self.assertEqual(fl.format_learned_terms_block("coord", root=self.root), "")

    def test_default_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_FEEDBACK_LEARNING", None)
            self.assertTrue(fl.is_enabled())


if __name__ == "__main__":
    unittest.main()
