"""Тесты Ф4 (bot-notarius-full): clarify голос + reply-матчинг + авто-архив.

Закрепляют ЧИСТУЮ логику (без сети/Telegram/Whisper/диска кроме tmp pending-dir):
  • REQ 4.1 — голос: ветвление transcribe_audio (smart→groq→None), voice_to_text,
    dispatch listener'а (голос→text) + фолбэк-сообщение при недоступной
    транскрибации. Реальный Whisper/Groq/Telegram замокан.
  • REQ 4.2 — reply-матчинг clarify_worker.process_text_message по message_id
    при НЕСКОЛЬКИХ открытых/истёкших clarify.
  • REQ 4.3 — sweep_timeouts архивирует timed_out старше N дней;
    has_any_pending_clarify не считает archived активными.
  • REQ 4.4 — переходный фолбэк: старый clarify без message_id (== 0) ловится
    count-логикой, поздний ответ не теряется.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_clarify_voice_matching -v
"""
from __future__ import annotations

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

from notary.lib import clarify_state, clarify_worker, voice_input  # noqa: E402
from notary import meetings_listener  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _state(meeting_id: str, *, status: str, message_id: int, **extra) -> dict:
    """Минимальный валидный clarify-state."""
    st = {
        "meeting_id": meeting_id,
        "transcript_path": f"/tmp/{meeting_id}.md",
        "meta": {"series": "anzhee", "date": "2026-06-03"},
        "unclear_clusters": {
            "SPEAKER_03": {
                "name_options": ["Дарья", "Ольга"],
                "samples": [],
                "speaker_label_in_md": "Спикер 3",
            }
        },
        "cluster_keys_ordered": ["SPEAKER_03"],
        "name_pool": ["Дарья", "Ольга"],
        "chat_id": 42,
        "message_id": message_id,
        "sent_at": _iso(datetime.now(timezone.utc)),
        "deadline_at": _iso(datetime.now(timezone.utc) + timedelta(hours=24)),
        "status": status,
    }
    st.update(extra)
    return st


class _FakeTelegram:
    """Фейк telegram_api: ловит send_message, остальное — no-op."""

    class TelegramApiError(RuntimeError):
        pass

    def __init__(self):
        self.sent: list[tuple] = []

    def send_message(self, token, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return {"message_id": 1}

    def edit_message_text(self, *a, **kw):
        return {}

    def answer_callback_query(self, *a, **kw):
        return None


# ─────────────────────── REQ 4.2 — reply-матчинг по message_id ───────────────────────

class TestReplyMatching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Три открытых clarify: 2 pending + 1 timed_out.
        clarify_state.write_state(_state("auto-tm-100", status="pending", message_id=100), root=self.root)
        clarify_state.write_state(_state("auto-tm-200", status="pending", message_id=200), root=self.root)
        clarify_state.write_state(_state("auto-tm-300", status="timed_out", message_id=300), root=self.root)
        self.captured: list[tuple] = []

        def _capture(text, state, bot_token, reply_chat_id, pending_root, *, is_late):
            self.captured.append((state["meeting_id"], is_late))

        self.apply_patch = mock.patch.object(clarify_worker, "_try_apply_text_to_state", _capture)
        self.fake_tg = _FakeTelegram()
        self.tg_patch = mock.patch.object(clarify_worker, "telegram_api", self.fake_tg)
        self.apply_patch.start()
        self.tg_patch.start()

    def tearDown(self):
        self.apply_patch.stop()
        self.tg_patch.stop()
        self.tmp.cleanup()

    def _msg(self, reply_mid):
        return {
            "text": "Спикер 3 = Дарья",
            "from": {"id": 42},
            "chat": {"id": 42},
            "reply_to_message": {"message_id": reply_mid},
        }

    def test_reply_to_pending_matches_exact_meeting(self):
        """REQ 4.2: reply на 2-й pending применяется именно к нему, не к 1-му."""
        clarify_worker.process_text_message(self._msg(200), self.root, "tok")
        self.assertEqual(self.captured, [("auto-tm-200", False)])

    def test_reply_to_timed_out_applies_as_late(self):
        """REQ 4.2: reply на истёкший вопрос → применяется как late_answer."""
        clarify_worker.process_text_message(self._msg(300), self.root, "tok")
        self.assertEqual(self.captured, [("auto-tm-300", True)])

    def test_reply_unmatched_falls_to_count_logic(self):
        """reply на чужой message_id + 2 pending → НЕ угадываем, просим кнопку."""
        clarify_worker.process_text_message(self._msg(999), self.root, "tok")
        self.assertEqual(self.captured, [])
        self.assertTrue(self.fake_tg.sent, "должен прийти ответ «нажми кнопку»")
        self.assertIn("открытых уточнений", self.fake_tg.sent[-1][1])


# ─────────────────────── REQ 4.4 — переходный фолбэк (УПУ3) ───────────────────────

class TestTransitionalFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.captured: list[tuple] = []

        def _capture(text, state, bot_token, reply_chat_id, pending_root, *, is_late):
            self.captured.append((state["meeting_id"], is_late))

        self.apply_patch = mock.patch.object(clarify_worker, "_try_apply_text_to_state", _capture)
        self.fake_tg = _FakeTelegram()
        self.tg_patch = mock.patch.object(clarify_worker, "telegram_api", self.fake_tg)
        self.apply_patch.start()
        self.tg_patch.start()

    def tearDown(self):
        self.apply_patch.stop()
        self.tg_patch.stop()
        self.tmp.cleanup()

    def test_old_state_without_message_id_caught_by_count(self):
        """REQ 4.4: единственный старый clarify (message_id=0) + reply → не
        матчится по id (0 пропущен), но count-фолбэк применяет (поздний ответ
        не теряется)."""
        clarify_state.write_state(_state("auto-tm-1", status="pending", message_id=0), root=self.root)
        msg = {
            "text": "Спикер 3 = Дарья",
            "from": {"id": 42},
            "chat": {"id": 42},
            "reply_to_message": {"message_id": 555},
        }
        clarify_worker.process_text_message(msg, self.root, "tok")
        self.assertEqual(self.captured, [("auto-tm-1", False)])

    def test_two_old_states_without_id_ask_for_button(self):
        """2 старых clarify без message_id + reply → безопасно не угадываем."""
        clarify_state.write_state(_state("auto-tm-1", status="pending", message_id=0), root=self.root)
        clarify_state.write_state(_state("auto-tm-2", status="pending", message_id=0), root=self.root)
        msg = {
            "text": "Спикер 3 = Дарья",
            "from": {"id": 42},
            "chat": {"id": 42},
            "reply_to_message": {"message_id": 555},
        }
        clarify_worker.process_text_message(msg, self.root, "tok")
        self.assertEqual(self.captured, [])
        self.assertTrue(self.fake_tg.sent)


# ─────────────────────── REQ 4.3 — авто-архив stale timed_out ───────────────────────

class TestSweepArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _timed_out(self, mid: str, age_days: float):
        ts = _iso(datetime.now(timezone.utc) - timedelta(days=age_days))
        clarify_state.write_state(
            _state(mid, status="timed_out", message_id=1, resolved_at=ts), root=self.root
        )

    def test_archives_over_threshold_keeps_under(self):
        """REQ 4.3: 8 дней → archived; 2 дня → остаётся timed_out (дефолт 7)."""
        self._timed_out("auto-tm-old", 8)
        self._timed_out("auto-tm-new", 2)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MEETING_NOTARY_CLARIFY_ARCHIVE_DAYS", None)
            clarify_worker.sweep_timeouts(self.root)
        self.assertEqual(clarify_state.read_state("auto-tm-old", root=self.root)["status"], "archived")
        self.assertEqual(clarify_state.read_state("auto-tm-new", root=self.root)["status"], "timed_out")

    def test_env_override_threshold(self):
        """REQ 4.3: MEETING_NOTARY_CLARIFY_ARCHIVE_DAYS=3 → 4 дня архивит, 2 нет."""
        self._timed_out("auto-tm-a", 4)
        self._timed_out("auto-tm-b", 2)
        with mock.patch.dict("os.environ", {"MEETING_NOTARY_CLARIFY_ARCHIVE_DAYS": "3"}):
            clarify_worker.sweep_timeouts(self.root)
        self.assertEqual(clarify_state.read_state("auto-tm-a", root=self.root)["status"], "archived")
        self.assertEqual(clarify_state.read_state("auto-tm-b", root=self.root)["status"], "timed_out")

    def test_timed_out_without_timestamp_not_archived(self):
        """Нет ни одной валидной метки → НЕ архивируем (не теряем поздний ответ)."""
        st = _state("auto-tm-x", status="timed_out", message_id=1)
        st.pop("resolved_at", None)
        st.pop("deadline_at", None)
        st.pop("sent_at", None)
        clarify_state.write_state(st, root=self.root)
        clarify_worker.sweep_timeouts(self.root)
        self.assertEqual(clarify_state.read_state("auto-tm-x", root=self.root)["status"], "timed_out")

    def test_has_any_pending_ignores_archived(self):
        """REQ 4.3: archived не считается активным; timed_out/pending — считаются."""
        self._timed_out("auto-tm-old", 30)
        clarify_worker.sweep_timeouts(self.root)  # → archived
        self.assertFalse(clarify_worker.has_any_pending_clarify(self.root))
        # Добавим живой timed_out (2 дня) — снова активно.
        self._timed_out("auto-tm-live", 2)
        self.assertTrue(clarify_worker.has_any_pending_clarify(self.root))

    def test_reply_match_skips_archived(self):
        """Архивный state не перехватывается reply-матчингом по message_id."""
        ts = _iso(datetime.now(timezone.utc) - timedelta(days=30))
        clarify_state.write_state(
            _state("auto-tm-arch", status="archived", message_id=777, archived_at=ts),
            root=self.root,
        )
        self.assertIsNone(clarify_worker._find_state_by_message_id(777, self.root))


# ─────────────────────── REQ 4.1 — голос (транскрибация замокана) ───────────────────────

class TestTranscribeBranching(unittest.TestCase):
    def test_smart_success_wins(self):
        with mock.patch.object(voice_input, "_transcribe_via_smart", lambda p: "из smart"), \
             mock.patch.object(voice_input, "_transcribe_via_groq", lambda p: "из groq"):
            self.assertEqual(voice_input.transcribe_audio("/tmp/x.oga"), "из smart")

    def test_groq_fallback_when_smart_none(self):
        with mock.patch.object(voice_input, "_transcribe_via_smart", lambda p: None), \
             mock.patch.object(voice_input, "_transcribe_via_groq", lambda p: "из groq"):
            self.assertEqual(voice_input.transcribe_audio("/tmp/x.oga"), "из groq")

    def test_none_when_both_fail(self):
        with mock.patch.object(voice_input, "_transcribe_via_smart", lambda p: None), \
             mock.patch.object(voice_input, "_transcribe_via_groq", lambda p: None):
            self.assertIsNone(voice_input.transcribe_audio("/tmp/x.oga"))


class TestVoiceToText(unittest.TestCase):
    def test_non_voice_returns_none(self):
        self.assertIsNone(voice_input.voice_to_text("tok", {"text": "hi"}))

    def test_voice_transcribed(self):
        msg = {"voice": {"file_id": "AgAD123"}}
        with mock.patch.object(voice_input.telegram_api, "get_file_path", lambda t, f: "voice/file_1.oga"), \
             mock.patch.object(voice_input.telegram_api, "download_file", lambda t, fp, dest, **kw: None), \
             mock.patch.object(voice_input, "transcribe_audio", lambda p: "Спикер 3 это Дарья"):
            self.assertEqual(voice_input.voice_to_text("tok", msg), "Спикер 3 это Дарья")

    def test_transcription_failure_returns_none(self):
        msg = {"voice": {"file_id": "AgAD123"}}
        with mock.patch.object(voice_input.telegram_api, "get_file_path", lambda t, f: "voice/file_1.oga"), \
             mock.patch.object(voice_input.telegram_api, "download_file", lambda t, fp, dest, **kw: None), \
             mock.patch.object(voice_input, "transcribe_audio", lambda p: None):
            self.assertIsNone(voice_input.voice_to_text("tok", msg))

    def test_no_file_path_returns_none(self):
        msg = {"voice": {"file_id": "AgAD123"}}
        with mock.patch.object(voice_input.telegram_api, "get_file_path", lambda t, f: None):
            self.assertIsNone(voice_input.voice_to_text("tok", msg))


class TestListenerVoiceDispatch(unittest.TestCase):
    """Dispatch listener'а: голос → text впрыскивается и идёт в обычный роутинг;
    при провале транскрибации — фолбэк-сообщение, не молчим."""

    ALLOWED = 42

    def _voice_msg(self, *, reply=False):
        m = {"voice": {"file_id": "AgAD"}, "chat": {"id": self.ALLOWED}, "message_id": 7}
        if reply:
            m["reply_to_message"] = {"message_id": 200, "text": "🎙 кто это?"}
        return m

    def test_transcription_unavailable_sends_fallback(self):
        """REQ 4.1: транскрибация недоступна → отправлен VOICE_FALLBACK_MSG."""
        sent: list[tuple] = []
        with mock.patch.object(meetings_listener, "transcribe_voice_or_none", lambda t, m: None), \
             mock.patch.object(meetings_listener, "send_message",
                               lambda token, cid, text, **kw: sent.append((cid, text))), \
             mock.patch.object(meetings_listener, "maybe_route_to_protocol_command",
                               lambda *a, **k: (_ for _ in ()).throw(AssertionError("routing не должен сработать"))):
            meetings_listener.process_message("tok", self.ALLOWED, self._voice_msg())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], meetings_listener.VOICE_FALLBACK_MSG)

    def test_voice_text_injected_into_dispatch(self):
        """REQ 4.1: распознанный голос подставляется как text и доходит до
        clarify-роутинга (тот же путь, что текстовый ответ)."""
        seen: dict = {}

        def _capture_clarify(token, msg):
            seen["text"] = msg.get("text")
            seen["voice"] = msg.get("voice")
            return True

        with mock.patch.object(meetings_listener, "transcribe_voice_or_none",
                               lambda t, m: "Спикер 3 = Дарья"), \
             mock.patch.object(meetings_listener, "send_message", lambda *a, **k: None), \
             mock.patch.object(meetings_listener, "maybe_route_to_protocol_command", lambda *a, **k: False), \
             mock.patch.object(meetings_listener, "maybe_route_to_correction_command", lambda *a, **k: False), \
             mock.patch.object(meetings_listener, "maybe_route_to_delivery_text", lambda *a, **k: False), \
             mock.patch.object(meetings_listener, "maybe_route_to_task_clarify_text", lambda *a, **k: False), \
             mock.patch.object(meetings_listener, "maybe_route_to_clarify_text", _capture_clarify):
            meetings_listener.process_message("tok", self.ALLOWED, self._voice_msg())
        self.assertEqual(seen.get("text"), "Спикер 3 = Дарья")
        self.assertIsNone(seen.get("voice"), "voice должен быть убран после подстановки")


if __name__ == "__main__":
    unittest.main()
