"""Ф1 delivery-fixes (ISS-1) — надёжный резолв путей + не-тихий провал + R-REPLY-disarm.

Покрывает бинарные критерии приёмки плана `2026-06-10-delivery-fixes-protocol.md`:
  - REQ 1.1  — persisted `transcript_path` в delivered-marker meta → `_resolve_paths`
               берёт его напрямую (даже когда meta и транскрипт в РАЗНЫХ папках —
               прод-раскладка НЕС2); deliver_protocol его персистит на финализации.
  - REQ 1.1b — старое state БЕЗ поля + транскрипт `…-tm-<id>.md` в папке серии →
               фолбэк-glob находит его (env-дефолт `<date>.md` тут бесполезен).
  - REQ 1.3  — двойная встреча одного дня → различение по `meeting_sid`/
               `nativeMeetingId` из meta (правится она, не пустышка/чужая).
  - REQ 1.5  — исчерпание попыток → терминализация `failed` (см. также
               test_phase4_feedback_reissue.test_attempts_cap_terminalizes_and_notifies_once).
  - R-REPLY  — полу-живая ask-машинерия `delivery_worker` обезврежена (орфаны
               ретайрятся, ответ владельца не глотается); 7.2 — no-binding→личка
               без интерактивного вопроса (нет `*-delivery.json`).

Запуск: python3 -m unittest tests.test_delivery_fixes_iss1 (system python3.9, без venv).
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

from notary.lib import feedback_state, feedback_reissue  # noqa: E402
from notary.lib import delivery_worker  # noqa: E402
from notary.lib import llm_postprocess as lp  # noqa: E402

PROTO = "#протоколвстречи\n\n## Решения\n\n- Пункт\n"
TRANSCRIPT = "00:00 Михаил: текст реплики\n"


class _FakeSendDoc:
    def __init__(self):
        self.docs = []

    def __call__(self, token, chat_id, path, *, caption=None, filename=None, **kw):
        self.docs.append({"chat_id": chat_id})
        return {"message_id": 7000 + len(self.docs)}


class _FakeRenderPdf:
    def __call__(self, markdown, out_path, *, title=None, subtitle=None, **kw):
        Path(out_path).write_text("%PDF-stub", encoding="utf-8")


# ===========================================================================
# REQ 1.1 / 1.1b / 1.3 — _resolve_paths
# ===========================================================================
class TestResolvePaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        # Прод-раскладка НЕС2: meta (delivered-marker) и транскрипт в РАЗНЫХ папках.
        self.transcripts = self.base / "_tmp" / "transcripts"   # <sid>.meta.json
        self.protocols = self.base / "protocols"                # <series>/<date>*.md
        self.transcripts.mkdir(parents=True, exist_ok=True)
        self.protocols.mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_PROTOCOLS_DIR": str(self.protocols),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _series_dir(self, series="coord"):
        d = self.protocols / series
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _meta_file(self, payload: dict, sid="sid1") -> Path:
        p = self.transcripts / f"{sid}.meta.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def _state(self, meta_path, *, series="coord", date="2026-06-02", chat_id=-1001):
        return {
            "feedback_id": feedback_state.build_feedback_id(series, date, chat_id),
            "series": series, "date": date, "chat_id": chat_id,
            "meta_path": str(meta_path), "status": "reissuing", "round": 1,
            "edits": [{"author": "Михаил", "text": "правка", "tg_message_id": 5}],
        }

    def test_req_1_1_persisted_transcript_path_used_directly(self):
        # REQ 1.1: meta несёт transcript_path → резолв берёт его, хотя meta лежит в
        # _tmp/transcripts/, а транскрипт — в protocols/<series>/ (разные папки).
        d = self._series_dir()
        tpath = d / "2026-06-02.md"
        ppath = d / "2026-06-02-protokol.md"
        tpath.write_text(TRANSCRIPT, encoding="utf-8")
        ppath.write_text(PROTO, encoding="utf-8")
        meta = {"series": "coord", "date": "2026-06-02",
                "transcript_path": str(tpath), "protocol_path": str(ppath),
                "delivered": [{"chat_id": -1001, "message_ids": [101], "at": "x"}]}
        meta_path = self._meta_file(meta)
        t, p = feedback_reissue._resolve_paths(self._state(meta_path), meta_path, meta)
        self.assertEqual(t, tpath)
        self.assertEqual(p, ppath)
        self.assertTrue(t.is_file())  # статус ≠ "transcript missing"

    def test_req_1_1b_fallback_glob_finds_tm_transcript(self):
        # REQ 1.1b: старое state БЕЗ transcript_path; survivor назван
        # `<date>-<date>-tm-<id>.md` (collision-rename). env-дефолт `<date>.md` бесполезен.
        d = self._series_dir()
        survivor = d / "2026-06-02-2026-06-02-tm-999.md"
        survivor.write_text(TRANSCRIPT, encoding="utf-8")
        (d / "2026-06-02-protokol.md").write_text(PROTO, encoding="utf-8")
        # НЕТ `<date>.md` — только `-tm-` survivor.
        meta = {"series": "coord", "date": "2026-06-02",
                "delivered": [{"chat_id": -1001, "message_ids": [101], "at": "x"}]}
        meta_path = self._meta_file(meta)
        t, p = feedback_reissue._resolve_paths(self._state(meta_path), meta_path, meta)
        self.assertEqual(t, survivor)
        self.assertTrue(t.is_file())
        self.assertEqual(p, d / "2026-06-02-protokol.md")

    def test_req_1_3_double_meeting_disambiguated_by_session_uid(self):
        # REQ 1.3: две встречи одного дня (две `-tm-` стенограммы) → различаем по
        # sessionUid (`tm-<id>`) из meta; правим ту, к которой относится фидбэк.
        d = self._series_dir()
        t_a = d / "2026-06-02-2026-06-02-tm-111.md"
        t_b = d / "2026-06-02-2026-06-02-tm-222.md"
        t_a.write_text("A: встреча 111\n", encoding="utf-8")
        t_b.write_text("B: встреча 222\n", encoding="utf-8")
        (d / "2026-06-02-protokol.md").write_text(PROTO, encoding="utf-8")
        meta_a = {"series": "coord", "date": "2026-06-02",
                  "sessionUid": "auto-tm-111-20260602T100000Z",
                  "delivered": [{"chat_id": -1001, "message_ids": [101], "at": "x"}]}
        meta_b = {"series": "coord", "date": "2026-06-02",
                  "sessionUid": "auto-tm-222-20260602T140000Z",
                  "delivered": [{"chat_id": -1001, "message_ids": [201], "at": "x"}]}
        mp_a = self._meta_file(meta_a, sid="sidA")
        mp_b = self._meta_file(meta_b, sid="sidB")
        ta, _ = feedback_reissue._resolve_paths(self._state(mp_a), mp_a, meta_a)
        tb, _ = feedback_reissue._resolve_paths(self._state(mp_b), mp_b, meta_b)
        self.assertEqual(ta, t_a)   # фидбэк A → стенограмма 111
        self.assertEqual(tb, t_b)   # фидбэк B → стенограмма 222
        self.assertNotEqual(ta, tb)

    def test_req_1_3_native_meeting_id_disambiguates(self):
        # Альтернативный id: nativeMeetingId (а не sessionUid) тоже разводит.
        d = self._series_dir()
        t_a = d / "2026-06-02-2026-06-02-tm-555.md"
        t_a.write_text("A\n", encoding="utf-8")
        (d / "2026-06-02-2026-06-02-tm-666.md").write_text("B\n", encoding="utf-8")
        (d / "2026-06-02-protokol.md").write_text(PROTO, encoding="utf-8")
        meta = {"series": "coord", "date": "2026-06-02", "nativeMeetingId": "555",
                "delivered": [{"chat_id": -1001, "message_ids": [101], "at": "x"}]}
        mp = self._meta_file(meta)
        t, _ = feedback_reissue._resolve_paths(self._state(mp), mp, meta)
        self.assertEqual(t, t_a)

    def test_double_meeting_no_id_is_ambiguous_not_guessed(self):
        # Без id различить нельзя — НЕ угадываем (None → reissue упадёт явно,
        # «transcript missing», а не правит случайную/пустышку).
        d = self._series_dir()
        (d / "2026-06-02-2026-06-02-tm-111.md").write_text("A\n", encoding="utf-8")
        (d / "2026-06-02-2026-06-02-tm-222.md").write_text("B\n", encoding="utf-8")
        meta = {"series": "coord", "date": "2026-06-02",
                "delivered": [{"chat_id": -1001, "message_ids": [101], "at": "x"}]}
        mp = self._meta_file(meta)
        t, _ = feedback_reissue._resolve_paths(self._state(mp), mp, meta)
        self.assertIsNone(t)

    def test_id_match_overrides_clobbered_persisted_path(self):
        # Двойная встреча: persisted `<date>.md` мог быть перезаписан (clobber)
        # последней встречей дня. id-match даёт ПРАВИЛЬНую стенограмму поверх
        # clobber-prone persisted — иначе REQ 1.3 регрессировал бы.
        d = self._series_dir()
        clobbered = d / "2026-06-02.md"            # «пустышка» от другой встречи
        right = d / "2026-06-02-2026-06-02-tm-777.md"
        clobbered.write_text("ЧУЖАЯ встреча\n", encoding="utf-8")
        right.write_text("МОЯ встреча 777\n", encoding="utf-8")
        (d / "2026-06-02-protokol.md").write_text(PROTO, encoding="utf-8")
        meta = {"series": "coord", "date": "2026-06-02",
                "sessionUid": "auto-tm-777-20260602T140000Z",
                "transcript_path": str(clobbered),  # persisted указывает на пустышку
                "delivered": [{"chat_id": -1001, "message_ids": [101], "at": "x"}]}
        mp = self._meta_file(meta)
        t, _ = feedback_reissue._resolve_paths(self._state(mp), mp, meta)
        self.assertEqual(t, right)  # id победил clobber-prone persisted


# ===========================================================================
# REQ 1.1 — deliver_protocol персистит путь на финализации
# ===========================================================================
class TestPersistOnDelivery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.meta_path = self.dir / "sid.meta.json"
        self.meta_path.write_text(json.dumps({"series": "coord", "date": "2026-06-02"}),
                                  encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {
            "TELEGRAM_NOTARIUS_BOT_TOKEN": "tok",
            "TELEGRAM_NOTARIUS_CHAT_ID": "555",
            "ENABLE_PROTOCOL_DELIVERY": "1",
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_deliver_persists_transcript_path_into_meta(self):
        # REQ 1.1: deliver_protocol кладёт transcript_path/protocol_path в meta
        # верхним уровнем (под flock'ом _update_meta_delivered).
        tpath = self.dir / "coord" / "2026-06-02.md"
        ppath = self.dir / "coord" / "2026-06-02-protokol.md"
        with mock.patch.object(lp.telegram_api, "send_document", _FakeSendDoc()), \
             mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", _FakeRenderPdf()):
            res = lp.deliver_protocol(
                meeting_meta={"series": "coord", "date": "2026-06-02",
                              "transcript_path": str(tpath), "protocol_path": str(ppath)},
                protocol_text=PROTO,
                meta_json_path=self.meta_path,
                meeting_sid="sid",
            )
        self.assertEqual(res["status"], "sent")
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["transcript_path"], str(tpath))
        self.assertEqual(meta["protocol_path"], str(ppath))
        self.assertEqual(meta["transcript_filename"], "2026-06-02.md")

    def test_persist_is_backfilled_even_on_idempotent_skip(self):
        # Бэкфилл (REQ 1.1b закрытие окна): повторная доставка уже-доставленной
        # встречи (idempotent skip) всё равно записывает transcript_path в старую meta.
        self.meta_path.write_text(json.dumps({
            "series": "coord", "date": "2026-06-02",
            "delivered": [{"chat_id": 555, "message_ids": [9], "at": "x", "document": True}],
        }), encoding="utf-8")
        tpath = self.dir / "coord" / "2026-06-02.md"
        with mock.patch.object(lp.telegram_api, "send_document", _FakeSendDoc()) as sd, \
             mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", _FakeRenderPdf()):
            res = lp.deliver_protocol(
                meeting_meta={"series": "coord", "date": "2026-06-02",
                              "transcript_path": str(tpath)},
                protocol_text=PROTO, meta_json_path=self.meta_path,
                target_chat_id=555, meeting_sid="sid",
            )
        self.assertEqual(res["status"], "skipped")     # идемпотентный пропуск
        self.assertEqual(sd.docs, [])                  # PDF повторно не слали
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["transcript_path"], str(tpath))  # но путь бэкфилнут


# ===========================================================================
# REQ 7.2 — нет привязки → личка, БЕЗ интерактивного вопроса (no *-delivery.json)
# ===========================================================================
class TestNoBindingGoesToDmWithoutAsking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.pending = self.dir / "_pending_clarification"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "sid.meta.json"
        self.meta_path.write_text("{}", encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {
            "TELEGRAM_NOTARIUS_BOT_TOKEN": "tok",
            "TELEGRAM_NOTARIUS_CHAT_ID": "555",   # личка-дефолт
            "ENABLE_PROTOCOL_DELIVERY": "1",
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_no_binding_delivers_dm_no_pending_state(self):
        # 7.2 / ВОПР1=A: нет series-привязки и target → авто в личку (555), статус
        # "sent", БЕЗ "asked" и БЕЗ создания `*-delivery.json` (фича вопроса снята).
        doc = _FakeSendDoc()
        with mock.patch.object(lp.telegram_api, "send_document", doc), \
             mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", _FakeRenderPdf()), \
             mock.patch.object(lp, "_load_watched_for_series", return_value=None):
            res = lp.deliver_protocol(
                meeting_meta={"series": "unbound-series", "date": "2026-06-02"},
                protocol_text=PROTO, meta_json_path=self.meta_path, meeting_sid="sid",
            )
        self.assertEqual(res["status"], "sent")
        self.assertNotEqual(res["status"], "asked")
        self.assertEqual(doc.docs[0]["chat_id"], 555)   # ушло в личку
        self.assertEqual(list(self.pending.glob("*-delivery.json")), [])  # вопрос не задан


# ===========================================================================
# R-REPLY — обезвреживание полу-живой ask-машинерии delivery_worker (РИСК5)
# ===========================================================================
class TestDeliveryWorkerDisarm(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pending = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _orphan(self, meeting_id="m1"):
        state = {
            "kind": "delivery", "status": "pending", "meeting_id": meeting_id,
            "chat_id": 555, "meta": {"series": "coord", "date": "2026-06-02"},
            "deadline_at": "2099-01-01T00:00:00Z",
        }
        (self.pending / f"{meeting_id}-delivery.json").write_text(
            json.dumps(state), encoding="utf-8")

    def test_retire_orphan_states(self):
        self._orphan("m1")
        self._orphan("m2")
        self.assertTrue(delivery_worker.has_any_pending_delivery(self.pending))
        n = delivery_worker.retire_orphan_delivery_states(self.pending)
        self.assertEqual(n, 2)
        # После ретайра орфаны больше не «pending» → не перехватят ответ.
        self.assertFalse(delivery_worker.has_any_pending_delivery(self.pending))

    def test_process_text_message_does_not_swallow(self):
        # РИСК5: текстовый ответ владельца при орфане НЕ глотается (return False),
        # орфан ретайрится → сообщение уходит к реальным обработчикам.
        self._orphan("m1")
        msg = {"text": "что-то про доставку", "chat": {"id": 555}, "message_id": 7}
        handled = delivery_worker.process_text_message(msg, self.pending, "tok")
        self.assertFalse(handled)  # НЕ перехватили — «не реагирует» исключён
        self.assertFalse(delivery_worker.has_any_pending_delivery(self.pending))

    def test_process_callback_retires_and_consumes_dead_button(self):
        self._orphan("m1")
        answers = []
        with mock.patch.object(delivery_worker, "telegram_api", create=True):
            pass  # telegram_api импортируется лениво внутри — мок ниже на модуль
        with mock.patch("notary.lib.telegram_api.answer_callback_query",
                        side_effect=lambda *a, **k: answers.append(k.get("text") or a[-1])):
            handled = delivery_worker.process_callback(
                {"data": "cd:abcd1234:dm", "id": "cbq1", "message": {"chat": {"id": 555}}},
                self.pending, "tok",
            )
        self.assertTrue(handled)  # наш `cd:` префикс прожёван (крутилка снята)
        self.assertFalse(delivery_worker.has_any_pending_delivery(self.pending))  # орфан ретайрнут
        self.assertTrue(answers)  # ответили «устарело» (видимая реакция)

    def test_non_cd_callback_passed_through(self):
        # Не наш префикс → False (передаём дальше другим воркерам).
        handled = delivery_worker.process_callback(
            {"data": "other:xxx", "id": "c", "message": {}}, self.pending, "tok")
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
