"""Тесты Ф3 (bot-notarius-master-plan) — шлюз правок FB1–FB4 + задел FB12.

Закрепляют, что бот:
  • FB1 — реагирует ТОЛЬКО на reply к доставленному протоколу (message_id ∈
    meta.delivered этого чата); обычное сообщение / @-упоминание / reply на
    чужое — drop;
  • FB2 — опознаёт автора (expectedParticipants/people.md → имя, иначе профиль)
    и шлёт ack «✅ Замечание принял, <Имя>. Жду 20 минут…»;
  • FB3 — окно стартует от первой правки, новая правка сбрасывает таймер на
    FEEDBACK_WINDOW_MIN, но не дольше потолка FEEDBACK_MAX_WINDOW_MIN от первой;
  • FB4 — состояние переживает рестарт listener (на диске);
  • FB12 — после сигнала перевыпуска state спит и просыпается новым раундом.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase3_feedback_gate -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import feedback_state, feedback_worker  # noqa: E402

UTC = timezone.utc


def _dt(h, m, s=0):
    return datetime(2026, 6, 2, h, m, s, tzinfo=UTC)


def _meeting(series="coord", date="2026-06-02", chat_id=-1001, mids=(101,), expected=None):
    meta = {
        "series": series,
        "date": date,
        "expectedParticipants": expected if expected is not None else ["Михаил Саргин", "Дарья Набережная"],
    }
    return {
        "series": series,
        "date": date,
        "chat_id": chat_id,
        "meta_path": f"/tmp/{series}/meta.json",
        "message_ids": list(mids),
        "meta": meta,
    }


def _edit(message_id, *, author="Михаил Саргин", text="правка", user_id=777, reply_mid=101):
    return {
        "edit_id": f"e-{message_id}",
        "tg_message_id": message_id,
        "reply_to_message_id": reply_mid,
        "from_user_id": user_id,
        "author": author,
        "text": text,
        "at": feedback_state.now_iso(),
    }


def _msg(message_id, text, *, reply_mid=101, from_user=None, chat_id=-1001, voice=False):
    m = {
        "message_id": message_id,
        "chat": {"id": chat_id},
        "from": from_user or {"id": 777, "first_name": "Михаил"},
    }
    if text is not None:
        m["text"] = text
    if reply_mid is not None:
        m["reply_to_message"] = {"message_id": reply_mid, "text": "📋 Протокол координации…"}
    if voice:
        m["voice"] = {"file_id": "v1", "duration": 3}
    return m


class _FakeSend:
    """Фейковый telegram_api.send_message — копит исходящие (chat_id, text, reply_to)."""

    def __init__(self):
        self.sent = []

    def __call__(self, token, chat_id, text, *, reply_to_message_id=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id})
        return {"message_id": 9000 + len(self.sent)}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "_feedback_edits"
        self.protocols = Path(self._tmp.name) / "protocols"
        self.protocols.mkdir(parents=True, exist_ok=True)
        # env: окно 20/120, фича ON, people.md отключён, delivered-root = temp.
        self._env = {
            "FEEDBACK_WINDOW_MIN": "20",
            "FEEDBACK_MAX_WINDOW_MIN": "120",
            "ENABLE_FEEDBACK_EDITS": "1",
            "MEETING_NOTARY_PEOPLE_MD": "/nonexistent/people.md",
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
            "MEETING_NOTARY_DELIVERED_ROOTS": str(self.protocols),
        }
        self._patch_env = mock.patch.dict(os.environ, self._env)
        self._patch_env.start()
        # сбрасываем TTL-кэш индекса между тестами
        feedback_worker._INDEX_CACHE.update(built_at=0.0, roots=None, index={})

    def tearDown(self):
        self._patch_env.stop()
        self._tmp.cleanup()

    def _write_delivered(self, series="coord", date="2026-06-02", chat_id=-1001, mids=(101,), expected=None):
        d = self.protocols / series
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "series": series,
            "date": date,
            "expectedParticipants": expected if expected is not None else ["Михаил Саргин"],
            "delivered": [{"chat_id": chat_id, "message_ids": list(mids), "at": "2026-06-02T11:00:00Z"}],
        }
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return d / "meta.json"


# ===========================================================================
# FB1 — шлюз: реагируем только на reply к доставленному протоколу
# ===========================================================================
class TestGateFB1(_Base):
    def test_find_delivered_match(self):
        self._write_delivered(mids=(101, 102))
        m = feedback_worker.find_delivered_protocol(-1001, 102, roots=[self.protocols], use_cache=False)
        self.assertIsNotNone(m)
        self.assertEqual(m["series"], "coord")
        self.assertEqual(m["chat_id"], -1001)

    def test_find_delivered_no_match_wrong_mid(self):
        self._write_delivered(mids=(101,))
        self.assertIsNone(
            feedback_worker.find_delivered_protocol(-1001, 999, roots=[self.protocols], use_cache=False)
        )

    def test_find_delivered_no_match_wrong_chat(self):
        self._write_delivered(chat_id=-1001, mids=(101,))
        self.assertIsNone(
            feedback_worker.find_delivered_protocol(-2002, 101, roots=[self.protocols], use_cache=False)
        )

    def test_reply_to_protocol_reacts(self):
        self._write_delivered(mids=(101,))
        send = _FakeSend()
        with mock.patch.object(feedback_worker.telegram_api, "send_message", send):
            handled = feedback_worker.route_feedback_reply(
                "tok", -1001, _msg(555, "131 на доставке", reply_mid=101),
                allowed_chat=42, root=self.root, now=_dt(12, 0),
            )
        self.assertTrue(handled)
        self.assertEqual(len(send.sent), 1)
        self.assertIn("Замечание принял", send.sent[0]["text"])

    def test_plain_message_group_silent_drop(self):
        # обычное сообщение (НЕ reply) в групповом чате → drop, бот молчит
        send = _FakeSend()
        with mock.patch.object(feedback_worker.telegram_api, "send_message", send):
            handled = feedback_worker.route_feedback_reply(
                "tok", -1001, _msg(556, "всем привет", reply_mid=None),
                allowed_chat=42, root=self.root, now=_dt(12, 0),
            )
        self.assertTrue(handled)  # прожёвано (дропнуто) — caller не идёт дальше
        self.assertEqual(send.sent, [])  # бот молчит

    def test_mention_without_reply_silent_drop(self):
        # @-упоминание без reply → drop, молчит
        msg = _msg(557, "@ilya_protocol_meeting_bot поправь", reply_mid=None)
        msg["entities"] = [{"type": "mention", "offset": 0, "length": 26}]
        send = _FakeSend()
        with mock.patch.object(feedback_worker.telegram_api, "send_message", send):
            handled = feedback_worker.route_feedback_reply(
                "tok", -1001, msg, allowed_chat=42, root=self.root, now=_dt(12, 0),
            )
        self.assertTrue(handled)
        self.assertEqual(send.sent, [])

    def test_reply_to_non_protocol_silent_drop(self):
        # reply есть, но на сообщение, которого нет в delivered → drop, молчит
        self._write_delivered(mids=(101,))
        send = _FakeSend()
        with mock.patch.object(feedback_worker.telegram_api, "send_message", send):
            handled = feedback_worker.route_feedback_reply(
                "tok", -1001, _msg(558, "правка", reply_mid=777),
                allowed_chat=42, root=self.root, now=_dt(12, 0),
            )
        self.assertTrue(handled)
        self.assertEqual(send.sent, [])

    def test_dm_non_protocol_returns_false(self):
        # В DM (allowed_chat) НЕ-протокольный reply → False: отдаём старому flow
        send = _FakeSend()
        with mock.patch.object(feedback_worker.telegram_api, "send_message", send):
            handled = feedback_worker.route_feedback_reply(
                "tok", 42, _msg(559, "1 да в @t11", reply_mid=None, chat_id=42),
                allowed_chat=42, root=self.root, now=_dt(12, 0),
            )
        self.assertFalse(handled)
        self.assertEqual(send.sent, [])

    def test_voice_reply_to_protocol_group_drop(self):
        # голосовая правка (FB9 — Ф5): в группе drop с пропуском, без ack
        self._write_delivered(mids=(101,))
        send = _FakeSend()
        with mock.patch.object(feedback_worker.telegram_api, "send_message", send):
            handled = feedback_worker.route_feedback_reply(
                "tok", -1001, _msg(560, None, reply_mid=101, voice=True),
                allowed_chat=42, root=self.root, now=_dt(12, 0),
            )
        self.assertTrue(handled)
        self.assertEqual(send.sent, [])

    def test_feature_disabled_returns_false(self):
        with mock.patch.dict(os.environ, {"ENABLE_FEEDBACK_EDITS": "0"}):
            handled = feedback_worker.route_feedback_reply(
                "tok", -1001, _msg(561, "правка", reply_mid=101),
                allowed_chat=42, root=self.root, now=_dt(12, 0),
            )
        self.assertFalse(handled)


# ===========================================================================
# FB2 — опознание автора + ack
# ===========================================================================
class TestAuthorFB2(_Base):
    def test_author_from_expected_participants_by_firstname(self):
        a = feedback_worker.resolve_author(
            {"id": 1, "first_name": "Михаил"},
            expected_participants=["Михаил Саргин", "Дарья Набережная"],
        )
        self.assertEqual(a, "Михаил Саргин")

    def test_author_namesakes_fall_to_profile(self):
        # два «Михаила» в пуле → не угадываем, профиль
        a = feedback_worker.resolve_author(
            {"id": 1, "first_name": "Михаил", "last_name": "И."},
            expected_participants=["Михаил Саргин", "Михаил Еремеев"],
        )
        self.assertEqual(a, "Михаил И.")

    def test_author_from_people_md_when_not_in_expected(self):
        a = feedback_worker.resolve_author(
            {"id": 1, "first_name": "Дарья"},
            expected_participants=["Михаил Саргин"],
            people_md_names=["Дарья Набережная", "Ольга Новикова"],
        )
        self.assertEqual(a, "Дарья Набережная")

    def test_author_profile_fallback_username(self):
        a = feedback_worker.resolve_author(
            {"id": 1, "username": "ivan_x"}, expected_participants=["Михаил Саргин"]
        )
        self.assertEqual(a, "@ivan_x")

    def test_parse_people_md(self):
        p = self.protocols / "people.md"
        p.write_text("# Люди\n- **Михаил Саргин** — продукт.\n- **Дарья Набережная** — HR.\n", encoding="utf-8")
        names = feedback_worker.parse_people_md(str(p))
        self.assertIn("Михаил Саргин", names)
        self.assertIn("Дарья Набережная", names)

    def test_ack_first_text(self):
        send = _FakeSend()
        st = feedback_worker.handle_feedback_reply(
            "tok", -1001, _msg(555, "131 не под досмотром, а на доставке", reply_mid=101),
            meeting=_meeting(), root=self.root, now=_dt(12, 0), send=send,
        )
        self.assertEqual(st["status"], "collecting")
        self.assertEqual(len(send.sent), 1)
        txt = send.sent[0]["text"]
        self.assertIn("Замечание принял", txt)
        self.assertIn("Михаил Саргин", txt)
        self.assertIn("20 минут", txt)
        self.assertEqual(send.sent[0]["reply_to"], 555)

    def test_ack_subsequent_text(self):
        send = _FakeSend()
        feedback_worker.handle_feedback_reply(
            "tok", -1001, _msg(555, "первая", reply_mid=101),
            meeting=_meeting(), root=self.root, now=_dt(12, 0), send=send,
        )
        feedback_worker.handle_feedback_reply(
            "tok", -1001, _msg(556, "вторая", reply_mid=101),
            meeting=_meeting(), root=self.root, now=_dt(12, 5), send=send,
        )
        self.assertEqual(len(send.sent), 2)
        self.assertTrue(send.sent[1]["text"].startswith("✅ Принял"))


# ===========================================================================
# FB3 — окно / дебаунс / потолок
# ===========================================================================
class TestWindowFB3(_Base):
    def test_first_edit_starts_window(self):
        st, kind = feedback_worker.apply_edit(
            None, edit=_edit(1), meeting=_meeting(), win_min=20, max_min=120, now=_dt(12, 0)
        )
        self.assertEqual(kind, "first")
        self.assertEqual(st["status"], "collecting")
        self.assertEqual(feedback_state._parse_iso(st["deadline_at"]), _dt(12, 20))
        self.assertEqual(feedback_state._parse_iso(st["hard_deadline_at"]), _dt(14, 0))

    def test_second_edit_resets_timer(self):
        st, _ = feedback_worker.apply_edit(
            None, edit=_edit(1), meeting=_meeting(), win_min=20, max_min=120, now=_dt(12, 0)
        )
        st2, kind = feedback_worker.apply_edit(
            st, edit=_edit(2), meeting=_meeting(), win_min=20, max_min=120, now=_dt(12, 5)
        )
        self.assertEqual(kind, "more")
        # окно сброшено: дедлайн = 12:05 + 20 = 12:25; старт окна не двигается
        self.assertEqual(feedback_state._parse_iso(st2["deadline_at"]), _dt(12, 25))
        self.assertEqual(feedback_state._parse_iso(st2["window_started_at"]), _dt(12, 0))
        self.assertEqual(len(st2["edits"]), 2)

    def test_ceiling_caps_deadline(self):
        # правки каждые 19 минут — окно 20 мин никогда не истекает естественно,
        # но потолок 120 мин от первой правки принудительно закрывает
        st, _ = feedback_worker.apply_edit(
            None, edit=_edit(1), meeting=_meeting(), win_min=20, max_min=120, now=_dt(12, 0)
        )
        t = _dt(12, 0)
        for i in range(2, 9):
            t = t + timedelta(minutes=19)
            st, _ = feedback_worker.apply_edit(
                st, edit=_edit(i), meeting=_meeting(), win_min=20, max_min=120, now=t
            )
        hard = _dt(14, 0)  # 12:00 + 120 мин
        self.assertLessEqual(feedback_state._parse_iso(st["deadline_at"]), hard)
        # за потолком окно истекло
        self.assertTrue(feedback_state.is_window_expired(st, now=_dt(14, 1)))

    def test_no_state_before_first_edit(self):
        # до первой правки ждём неограниченно — состояния нет, sweep ничего не делает
        self.assertEqual(feedback_state.list_states(root=self.root), [])
        self.assertEqual(feedback_worker.sweep_timeouts(self.root, now=_dt(13, 0)), 0)

    def test_sweep_closes_only_expired(self):
        st, _ = feedback_worker.apply_edit(
            None, edit=_edit(1), meeting=_meeting(), win_min=20, max_min=120, now=_dt(12, 0)
        )
        feedback_state.write_state(st, root=self.root)
        # до дедлайна — не закрываем
        self.assertEqual(feedback_worker.sweep_timeouts(self.root, now=_dt(12, 10)), 0)
        self.assertEqual(feedback_state.read_state(st["feedback_id"], root=self.root)["status"], "collecting")
        # после дедлайна — закрываем → ready_for_reissue
        self.assertEqual(feedback_worker.sweep_timeouts(self.root, now=_dt(12, 21)), 1)
        self.assertEqual(
            feedback_state.read_state(st["feedback_id"], root=self.root)["status"], "ready_for_reissue"
        )


# ===========================================================================
# FB4 — персистентность (переживает рестарт)
# ===========================================================================
class TestPersistenceFB4(_Base):
    def test_state_survives_restart_and_edit_not_lost(self):
        send = _FakeSend()
        st = feedback_worker.handle_feedback_reply(
            "tok", -1001, _msg(555, "131 на доставке", reply_mid=101),
            meeting=_meeting(), root=self.root, now=_dt(12, 0), send=send,
        )
        fid = st["feedback_id"]
        # «рестарт»: новое чтение состояния с диска (другой объект)
        reloaded = feedback_state.read_state(fid, root=self.root)
        self.assertIsNotNone(reloaded)
        self.assertEqual(len(reloaded["edits"]), 1)
        self.assertEqual(feedback_state._parse_iso(reloaded["deadline_at"]), _dt(12, 20))
        # по истечении окна после рестарта — правка не потеряна, уходит в ready_for_reissue
        self.assertEqual(feedback_worker.sweep_timeouts(self.root, now=_dt(12, 25)), 1)
        final = feedback_state.read_state(fid, root=self.root)
        self.assertEqual(final["status"], "ready_for_reissue")
        self.assertEqual(final["edits"][0]["text"], "131 на доставке")

    def test_dedup_same_message_no_double_ack(self):
        send = _FakeSend()
        feedback_worker.handle_feedback_reply(
            "tok", -1001, _msg(555, "правка", reply_mid=101),
            meeting=_meeting(), root=self.root, now=_dt(12, 0), send=send,
        )
        # тот же tg_message_id (повторная доставка апдейта) → без второго ack
        feedback_worker.handle_feedback_reply(
            "tok", -1001, _msg(555, "правка", reply_mid=101),
            meeting=_meeting(), root=self.root, now=_dt(12, 1), send=send,
        )
        self.assertEqual(len(send.sent), 1)
        st = feedback_state.read_state(
            feedback_state.build_feedback_id("coord", "2026-06-02", -1001), root=self.root
        )
        self.assertEqual(len(st["edits"]), 1)


# ===========================================================================
# FB12 — многораундовость: сон после перевыпуска, пробуждение новым reply
# ===========================================================================
class TestMultiroundFB12(_Base):
    def test_reissue_signal_then_new_round(self):
        # раунд 1 → закрытие окна → ready_for_reissue
        st, _ = feedback_worker.apply_edit(
            None, edit=_edit(1), meeting=_meeting(), win_min=20, max_min=120, now=_dt(12, 0)
        )
        feedback_state.write_state(st, root=self.root)
        feedback_worker.sweep_timeouts(self.root, now=_dt(12, 21))
        closed = feedback_state.read_state(st["feedback_id"], root=self.root)
        self.assertEqual(closed["status"], "ready_for_reissue")
        # новый reply на ту же серию → раунд 2 (state «проснулся»)
        st2, kind = feedback_worker.apply_edit(
            closed, edit=_edit(7), meeting=_meeting(), win_min=20, max_min=120, now=_dt(12, 30)
        )
        self.assertEqual(kind, "first")
        self.assertEqual(st2["round"], 2)
        self.assertEqual(st2["status"], "collecting")
        self.assertEqual(len(st2["edits"]), 1)
        self.assertEqual(feedback_state._parse_iso(st2["window_started_at"]), _dt(12, 30))

    def test_dormant_wakes_to_new_round(self):
        # Ф4 после перевыпуска ставит dormant → новый reply открывает раунд
        st, _ = feedback_worker.apply_edit(
            None, edit=_edit(1), meeting=_meeting(), win_min=20, max_min=120, now=_dt(12, 0)
        )
        st["status"] = "dormant"
        st["round"] = 2
        st2, kind = feedback_worker.apply_edit(
            st, edit=_edit(9), meeting=_meeting(), win_min=20, max_min=120, now=_dt(13, 0)
        )
        self.assertEqual(kind, "first")
        self.assertEqual(st2["round"], 3)
        self.assertEqual(st2["status"], "collecting")


if __name__ == "__main__":
    unittest.main()
