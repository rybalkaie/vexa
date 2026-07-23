# -*- coding: utf-8 -*-
"""ISS-45: бот присылает протокол / открытые вопросы по просьбе, а не «🤔 не понял».

Покрывает:
  • детект интента (глагол отправки + объект; неоднозначное «файл» по surface; мимо-
    статус/правка → None);
  • роут `maybe_route_to_send_artifact`: диспетчеризация protocol/digest, уступка
    статус-ответу, подсказка при нерезолвнутой серии, не-reply → False;
  • `_resend_digest`: зовёт `send_series_digest(force=True)`, пустой список → честная строка.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python -m unittest tests.test_send_artifact -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import send_artifact as sa  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402


# ==========================================================================
# Детект интента (чистая функция)
# ==========================================================================
class TestDetect(unittest.TestCase):
    def test_protocol_requests_explicit(self):
        # Явное слово «протокол» → protocol независимо от контекста.
        for t in ("пришли протокол", "повтори протокол, пожалуйста", "скинь протокол"):
            self.assertEqual(sa.detect_artifact_request(t), "protocol", t)

    def test_version_file_is_ambiguous_by_surface(self):
        # Фидбэк владельца 23.07: «актуальную версию файла» — НЕ признак протокола.
        # Ответ на дайджест «Открытые вопросы» → digest (слова «протокол» нет).
        self.assertEqual(
            sa.detect_artifact_request("Ты можешь скинуть актуальную версию файла?",
                                       reply_surface="digest"), "digest")
        self.assertEqual(
            sa.detect_artifact_request("скинь версию файла", reply_surface="protocol"), "protocol")
        # без контекста — протокол (главный документ)
        self.assertEqual(sa.detect_artifact_request("скинь актуальную версию файла"), "protocol")

    def test_document_word_not_captured(self):
        # Н1 цикл5: «документ» слишком общее — «пришли документ по клиенту» не про протокол.
        self.assertIsNone(sa.detect_artifact_request("пришли документ по клиенту"))

    def test_digest_requests(self):
        for t in ("пришли открытые вопросы", "скинь дайджест", "отправь список вопросов",
                  "дай открытые задачи"):
            self.assertEqual(sa.detect_artifact_request(t), "digest", t)

    def test_ambiguous_file_by_surface(self):
        self.assertEqual(sa.detect_artifact_request("скинь файл", reply_surface="digest"), "digest")
        self.assertEqual(sa.detect_artifact_request("скинь файл", reply_surface="protocol"), "protocol")
        # без контекста — протокол (главный документ)
        self.assertEqual(sa.detect_artifact_request("скинь файл"), "protocol")

    def test_both_named_use_surface(self):
        # оба явно названы → по контексту ответа
        self.assertEqual(
            sa.detect_artifact_request("пришли протокол и открытые вопросы", reply_surface="digest"),
            "digest")
        # без контекста → протокол
        self.assertEqual(
            sa.detect_artifact_request("пришли протокол и открытые вопросы"), "protocol")

    def test_misses_not_stolen(self):
        # ни глагола отправки, ни/или объекта — не наш интент
        for t in ("закрой третью", "юани — закупили", "спасибо", "да",
                  "скинь ссылку на карточку", "пришли", "готово"):
            self.assertIsNone(sa.detect_artifact_request(t), t)

    def test_yo_normalized(self):
        self.assertEqual(sa.detect_artifact_request("можёшь скинуть протокол"), "protocol")


# ==========================================================================
# Роут в listener
# ==========================================================================
class TestRoute(unittest.TestCase):
    GROUP = 555

    def _msg(self, text, *, reply=True):
        m = {"chat": {"id": self.GROUP}, "from": {"id": 42, "first_name": "Илья"},
             "message_id": 1000, "date": 1718000000, "text": text}
        if reply:
            m["reply_to_message"] = {"message_id": 900, "text": "📋 Открытые вопросы…"}
        return m

    def test_dispatch_digest(self):
        # reply на дайджест + «скинь открытые вопросы» → _resend_digest
        with mock.patch.object(ml, "_reply_context_series", return_value=None), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.GROUP, self._msg("скинь открытые вопросы"))
        self.assertTrue(claimed)
        rd.assert_called_once()
        rp.assert_not_called()

    def test_dispatch_protocol(self):
        # reply на протокол + «пришли протокол» → _resend_protocol
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value=None), \
             mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.GROUP, self._msg("пришли протокол"))
        self.assertTrue(claimed)
        rp.assert_called_once()
        rd.assert_not_called()

    def test_ambiguous_file_uses_reply_surface(self):
        # Кейс владельца 23.07: ответ на ДАЙДЖЕСТ «актуальную версию файла» → digest
        # (surface разрешает неоднозначность; слова «протокол» нет — не шлём протокол).
        with mock.patch.object(ml, "_reply_context_series", return_value=None), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp:
            ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.GROUP,
                self._msg("Ты можешь скинуть актуальную версию файла?"))
        rd.assert_called_once()
        rp.assert_not_called()

    def test_status_reply_not_stolen(self):
        # обычный статус-ответ (не просьба о файле) → роут не перехватывает
        with mock.patch.object(ml, "_reply_context_series", return_value=None), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "send_message") as sm_:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.GROUP, self._msg("закрой третью"))
        self.assertFalse(claimed)
        sm_.assert_not_called()

    def test_no_series_gives_hint(self):
        # интент есть, но серия не резолвится → мягкая подсказка, НЕ «не понял»
        sent = []
        with mock.patch.object(ml, "_reply_context_series", return_value=None), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value=None), \
             mock.patch.object(ml, "send_message", side_effect=lambda *a, **k: sent.append(a[2])):
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.GROUP, self._msg("пришли протокол"))
        self.assertTrue(claimed)
        self.assertTrue(any("ответьте на дайджест" in s.lower() for s in sent))

    def test_not_a_reply_returns_false(self):
        # не reply → нет контекста серии → False (обычный маршрут)
        with mock.patch.object(ml, "send_message") as sm_:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.GROUP, self._msg("пришли протокол", reply=False))
        self.assertFalse(claimed)
        sm_.assert_not_called()


# ==========================================================================
# _resend_digest — переотправка через send_series_digest(force=True)
# ==========================================================================
class TestResendDigest(unittest.TestCase):
    GROUP = 555

    def _msg(self):
        return {"chat": {"id": self.GROUP}, "message_id": 1000, "text": "скинь открытые вопросы",
                "reply_to_message": {"message_id": 900}}

    def test_force_true_passed(self):
        from notary.lib import morning_digest
        with mock.patch.object(morning_digest, "send_series_digest",
                               return_value={"status": "sent"}) as ssd, \
             mock.patch.object(ml, "send_message") as sm_:
            claimed = ml._resend_digest("tok", self.GROUP, "koord", self._msg())
        self.assertTrue(claimed)
        _, kwargs = ssd.call_args
        self.assertTrue(kwargs.get("force"))          # обошли already_sent
        sm_.assert_not_called()                        # сам дайджест уже ушёл документом

    def test_empty_says_no_open(self):
        from notary.lib import morning_digest
        sent = []
        with mock.patch.object(morning_digest, "send_series_digest",
                               return_value={"status": "skipped", "reason": "empty"}), \
             mock.patch.object(ml, "send_message", side_effect=lambda *a, **k: sent.append(a[2])):
            claimed = ml._resend_digest("tok", self.GROUP, "koord", self._msg())
        self.assertTrue(claimed)
        self.assertTrue(any("пуст" in s.lower() for s in sent))


if __name__ == "__main__":
    unittest.main()
