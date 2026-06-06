"""Ф3а — проводка дайджеста самообучения «🧠 Ватсон выучил» в листенер (REQ 2.3).

Закрывает код-часть критерия «сделано» Ф3а (живая озвучка таймером — ⏳ деплой):
  1. Роут: reply на дайджест «🧠 …» в `process_message` → `maybe_route_to_learning_rollback`,
     и НЕ перехватывает reply на вечерний блок «📅 …» (apply_reply встреч не сломан).
  2. Откат: «откати <термин>» в ответ на дайджест → правило снято (active_rules пусто),
     событие `rollback` видно в логе серии, владельцу ушло подтверждение.
  3. Подтверждение без отката («спасибо, верно») — реестр не трогаем, ack отправлен.
  4. Smoke-сборка блока «Ватсон выучил» из лога (через тот же путь, что таймер).

Запуск: python3 -m unittest tests.test_phase3a_learning_rollback_route
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

from notary import meetings_listener as ml  # noqa: E402
from notary.lib import feedback_learning as fl  # noqa: E402
from notary.lib import feedback_state  # noqa: E402

TOKEN = "test-token"
CHAT = 359008340
LEARN_PREFIX = "\U0001F9E0"  # 🧠 — должно совпасть с feedback_learning.DIGEST_PREFIX
TRIGGER_PREFIX = "\U0001F4C5"  # 📅 — вечерний блок (apply_reply встреч)


class _LearnBase(unittest.TestCase):
    """Песочница каталога обучения + захват send_message."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "_feedback_edits"
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
            "ENABLE_FEEDBACK_LEARNING": "1",
        })
        self._env.start()
        # send_message → захват (без сети).
        self.sent: list[tuple] = []
        self._sm = mock.patch.object(
            ml, "send_message",
            side_effect=lambda token, chat_id, text, **kw: self.sent.append((chat_id, text, kw)),
        )
        self._sm.start()

    def tearDown(self):
        self._sm.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _learn(self, series, text, *, author="Михаил"):
        state = {"series": series, "date": "2026-06-02",
                 "feedback_id": feedback_state.build_feedback_id(series, "2026-06-02", -1),
                 "round": 1}
        # root НЕ передаём — лезем через env (как живой листенер), чтобы тест ловил
        # реальную точку чтения `resolve_feedback_dir()`.
        return fl.record_learning_from_edits(state, [{"author": author, "text": text}])

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


# ===========================================================================
# 1) Роут в process_message по префиксам (🧠 ловим, 📅 не трогаем)
# ===========================================================================
class TestProcessMessageRouting(_LearnBase):
    def _msg(self, orig_text: str, reply_text: str = "откати Гарсиа") -> dict:
        return {
            "chat": {"id": CHAT},
            "text": reply_text,
            "message_id": 42,
            "date": 1780000000,
            "reply_to_message": {"text": orig_text},
        }

    def test_learning_digest_reply_routed_to_rollback(self):
        spy = mock.MagicMock(return_value=True)
        with mock.patch.object(ml, "maybe_route_to_feedback_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_protocol_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_correction_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_learning_rollback", spy):
            ml.process_message(TOKEN, CHAT, self._msg(f"{LEARN_PREFIX} Ватсон выучил …"))
        spy.assert_called_once()
        # Вызван c (token, chat_id, msg).
        self.assertEqual(spy.call_args.args[0], TOKEN)
        self.assertEqual(spy.call_args.args[1], CHAT)

    def test_evening_block_reply_not_routed_to_rollback(self):
        spy = mock.MagicMock(return_value=True)
        with mock.patch.object(ml, "maybe_route_to_feedback_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_protocol_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_correction_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_learning_rollback", spy), \
             mock.patch.object(ml, "STATE_DIR", self.base), \
             mock.patch.object(ml, "heartbeat", lambda *a, **k: None):
            # 📅-reply идёт в apply_reply встреч; snapshot отсутствует → мягкий выход.
            ml.process_message(TOKEN, CHAT, self._msg(f"{TRIGGER_PREFIX} Встречи и запись"))
        spy.assert_not_called()

    def test_prefix_matches_digest_block_source(self):
        # Анти-дрейф: префикс роута == первый символ реального блока дайджеста.
        self._learn("coord", "Гарсия → Гарсиа")
        text, _ = fl.format_digest_block()
        self.assertTrue(text.startswith(LEARN_PREFIX))
        self.assertEqual(ml._learning_digest_prefix(), LEARN_PREFIX)


# ===========================================================================
# 2-3) Поведение maybe_route_to_learning_rollback
# ===========================================================================
class TestRollbackHelper(_LearnBase):
    def _reply(self, text: str) -> dict:
        return {"text": text, "message_id": 7}

    def test_rollback_deactivates_rule_and_logs(self):
        self._learn("coord", "Гарсия → Гарсиа")
        self.assertEqual(len(fl.active_rules("coord")), 1)

        handled = ml.maybe_route_to_learning_rollback(TOKEN, CHAT, self._reply("откати Гарсиа"))
        self.assertTrue(handled)
        # Правило снято.
        self.assertEqual(fl.active_rules("coord"), [])
        # Событие rollback дописано в лог серии (история обратима, видно).
        path = fl.series_log_path("coord")
        ops = [json.loads(l)["op"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertIn("learn", ops)
        self.assertIn("rollback", ops)
        # Владельцу ушло подтверждение отката.
        self.assertIn("Откатил", self.last_text)
        self.assertIn("Гарсиа", self.last_text)

    def test_confirmation_without_trigger_keeps_rule(self):
        self._learn("coord", "Гарсия → Гарсиа")
        handled = ml.maybe_route_to_learning_rollback(TOKEN, CHAT, self._reply("спасибо, всё верно"))
        self.assertTrue(handled)
        # Нет триггера отката → реестр не тронут.
        self.assertEqual(len(fl.active_rules("coord")), 1)
        self.assertIn("оставляю как выучил", self.last_text)

    def test_empty_reply_hints(self):
        handled = ml.maybe_route_to_learning_rollback(TOKEN, CHAT, self._reply(""))
        self.assertTrue(handled)
        self.assertIn("откати", self.last_text)


# ===========================================================================
# 4) Дайджест уходит ботом notarius (тем же, что ловит откат) — анти-дрейф
# ===========================================================================
class TestDigestSentViaNotariusBot(_LearnBase):
    """Дайджест 🧠 обязан уходить ботом `notarius` — ТЕМ ЖЕ, что поллит listener
    с роутом отката. Если слать дефолтным ботом tg-send (бот «main», как делает
    notify.push), reply владельца «откати …» уйдёт другому боту, listener его не
    увидит и откат (ЗАВ1/REQ 2.5) не сработает. Зеркалит meetings_evening_block
    --send-tg notarius. Тест ловит регресс «отправили не тем ботом»."""

    def test_run_digest_sends_with_bot_notarius(self):
        from notary import feedback_learning_digest as fld  # noqa: PLC0415
        self._learn("coord", "Гарсия → Гарсиа")

        calls: list[list] = []

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        def _fake_run(cmd, **kw):
            calls.append(cmd)
            return _Proc()

        with mock.patch.object(fld.shutil, "which", return_value="/usr/bin/tg-send"), \
             mock.patch.object(fld.subprocess, "run", side_effect=_fake_run):
            out = fld.run_digest()

        self.assertTrue(out.startswith(LEARN_PREFIX))
        self.assertEqual(len(calls), 1, "дайджест должен уйти ровно одним вызовом tg-send")
        cmd = calls[0]
        self.assertIn("--bot", cmd)
        self.assertEqual(cmd[cmd.index("--bot") + 1], "notarius")
        self.assertTrue(cmd[-1].startswith(LEARN_PREFIX),
                        "текст блока — последний позиционный аргумент tg-send")


if __name__ == "__main__":
    unittest.main()
