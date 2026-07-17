# -*- coding: utf-8 -*-
"""Тесты Ф5 (план `2026-07-16-open-questions-reply-fix.md`, ISS-36 REQ 6.1/6.2/6.3/7.2-голос):
ГОЛОСОВОЙ ответ на утренний дайджест «Открытые вопросы к встрече» → расшифровка → ТОТ ЖЕ
статус-парсер, что и текст (мульти-статус Ф2 + ординал Ф4), а НЕ отбивка «напишите текстом».

Что проверяем:
  • REQ 6.1 — voice-reply с 3 статусами (STT застаблен) → 3 записи `set_task_status` + ack;
  • REQ 7.2 (голос) — voice-reply «первая сделана» → задача 1 = done (ординал по голосу,
    резолв против слепка ДАЙДЖЕСТА, тот же путь Ф4);
  • REQ 6.2 — STT недоступен / вернул пусто → честная подсказка-fallback (не молчим);
  • REQ 6.3 / гейт — флаг `ENABLE_DIGEST_VOICE_REPLY` дефолт-OFF В КОДЕ: при OFF голос
    отбивается `DIGEST_VOICE_HINT` и реальный STT НЕ дёргается (офлайн-тесты зелёные);
  • Опасная тройка — расшифровку/секрет НЕ логируем, только длину/счётчики.

STT в тестах ВСЕГДА застаблен (`transcribe_voice_or_none` мокнут) — реальный Groq/сеть не
зовём. Сплиттер пачки — тоже стаб (`mrs._request_split`), как в Ф2/Ф4.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss36_phase5_voice_digest -v
"""
from __future__ import annotations

import logging
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

from notary.lib import series_memory as sm  # noqa: E402
from notary.lib import pending_status_reply as p  # noqa: E402
from notary.lib import mixed_reply_split as mrs  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402


# Протокол серии с 5 висяками под одним исполнителем (как в Ф2/Ф4).
_PROTO5 = """#протоколвстречи 03.06.2026

**Встреча:** Координация.

**Длительность:** 25 мин

**Участники:** Илья Рыбалка

**Транскрипт:** [2026-06-03.md](2026-06-03.md)

---

## 1) Раздел

▪️ Обсудили.

## Задачи

**Илья Рыбалка**
- Подготовить медиаплан на июль
- Прислать расчёт по складу Ozon
- Согласовать бюджет маркетинга
- Закрыть вакансию дизайнера
- Обновить прайс на витрине
"""

# Сегменты, каждый уникально матчащийся на свой висяк (проверено в Ф2).
_S_MEDIA = "медиаплан на июль сделали"                # done  → медиаплан
_S_RASCH = "расчёт по складу закрыли"                 # done  → расчёт склад
_S_BUDGET = "бюджет маркетинга снимаем, неактуально"  # cancelled → бюджет


# ==========================================================================
# Гейт `ENABLE_DIGEST_VOICE_REPLY` (дефолт-OFF в коде, A11 / Гейт 2 Q2)
# ==========================================================================
class TestVoiceReplyFlag(unittest.TestCase):
    def test_default_off_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_DIGEST_VOICE_REPLY", None)
            self.assertFalse(ml._digest_voice_reply_enabled())

    def test_on_variants(self):
        for val in ("1", "true", "yes", "on", "ON", "Yes", "TRUE"):
            with mock.patch.dict(os.environ, {"ENABLE_DIGEST_VOICE_REPLY": val}):
                self.assertTrue(ml._digest_voice_reply_enabled(), msg=val)

    def test_off_variants(self):
        for val in ("0", "false", "no", "off", "", "  "):
            with mock.patch.dict(os.environ, {"ENABLE_DIGEST_VOICE_REPLY": val}):
                self.assertFalse(ml._digest_voice_reply_enabled(), msg=repr(val))


# ==========================================================================
# Сквозные сценарии голос-reply на дайджест
# ==========================================================================
class _VoiceDigestBase(unittest.TestCase):
    CHAT = -100500
    DIGEST_MID = 4242

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-06-03", _PROTO5,
                               {"series": "koord", "date": "2026-06-03"})
        self.kt = [pos["key_text"] for pos in sm.ordered_open_tasks(self.sd)]
        self.assertEqual(len(self.kt), 5)
        # Слепок ДАЙДЖЕСТА по его message_id (как send_series_digest пишет в Ф3) —
        # против него резолвится ординал по голосу (Ф4, тот же читатель).
        sm.record_open_tasks_snapshot(self.sd, self.DIGEST_MID, self.kt,
                                      surface="digest", date="2026-06-03")

    def tearDown(self):
        self._tmp.cleanup()

    def _voice_msg(self, *, audio=False, reply_mid=None):
        key = "audio" if audio else "voice"
        return {
            "message_id": 999,
            "reply_to_message": {"message_id": reply_mid or self.DIGEST_MID,
                                 "text": "📋 Открытые вопросы"},
            key: {"file_id": "AwACAgIfake", "duration": 4},
            "date": 1718000000,
        }

    def _status_of(self, key_text):
        store = sm.load_task_status(self.sd)
        rec = store.get(sm._status_key(key_text))
        return rec.get("status") if rec else None

    def _run(self, transcript, *, canned=None, flag_on=True, mixed_on=True,
             audio=False, reply_mid=None):
        """Прогон digest-роута на ГОЛОСОВОМ сообщении со стаб-STT.
        `transcript` — что «расшифровал» STT (None/'' → сбой STT).
        `canned` — стаб LLM-сплита (для пачки); None → сплит вернёт None."""
        env = {
            "ENABLE_DIGEST_VOICE_REPLY": "1" if flag_on else "0",
            "ENABLE_MIXED_REPLY_SPLIT": "1" if mixed_on else "0",
        }
        sent: list = []
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ml, "transcribe_voice_or_none", return_value=transcript) as tr, \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)):
            claimed = ml.maybe_route_to_digest_reply(
                "tok", self.CHAT, self._voice_msg(audio=audio, reply_mid=reply_mid))
        return claimed, sent, tr


class TestVoiceDigestBatch(_VoiceDigestBase):
    def test_req_6_1_three_statuses_by_voice(self):
        # REQ 6.1: голос с 3 статусами → 3 записи set_task_status + один ack.
        transcript = "медиаплан сделали\nрасчёт по складу закрыли\nбюджет снимаем"
        canned = {"statuses": [_S_MEDIA, _S_RASCH, _S_BUDGET], "edits": [], "unclear": []}
        claimed, sent, tr = self._run(transcript, canned=canned)
        self.assertTrue(claimed)
        tr.assert_called_once()                          # STT дёрнут (флаг ON)
        self.assertEqual(len(sm.load_task_status(self.sd)), 3)   # 3 висяка получили статус
        self.assertEqual(len(sent), 1)                   # один объединённый ack, не отбивка
        self.assertNotIn("напишите", sent[0].lower())    # это ack, а не «напишите текстом»

    def test_audio_message_also_transcribed(self):
        # msg["audio"] (не только "voice") тоже заходит в голос-ветку и расшифровывается.
        transcript = "медиаплан сделали\nрасчёт по складу закрыли\nбюджет снимаем"
        canned = {"statuses": [_S_MEDIA, _S_RASCH, _S_BUDGET], "edits": [], "unclear": []}
        claimed, sent, tr = self._run(transcript, canned=canned, audio=True)
        self.assertTrue(claimed)
        tr.assert_called_once()
        self.assertEqual(len(sm.load_task_status(self.sd)), 3)


class TestVoiceDigestOrdinal(_VoiceDigestBase):
    def test_req_7_2_single_ordinal_by_voice(self):
        # REQ 7.2 (голос): «первую сделал» голосом → задача 1 слепка дайджеста = done.
        # Одиночный ординал → fast-path (не пачка), резолв против снапшота (тот же Ф4-путь).
        claimed, sent, tr = self._run("первую сделал")
        self.assertTrue(claimed)
        tr.assert_called_once()
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)   # задача 1
        self.assertEqual(len(sent), 1)                                  # ack

    def test_multi_ordinal_batch_by_voice(self):
        # Голос «первая — сделана, вторая — в работе» → пачка (≥2 ординала) → задача 1 =
        # done, задача 2 = wait(→open). Резолв каждого сегмента против слепка дайджеста.
        canned = {"statuses": ["первая — сделана", "вторая — в работе"],
                  "edits": [], "unclear": []}
        claimed, sent, tr = self._run("первая — сделана, вторая — в работе", canned=canned)
        self.assertTrue(claimed)
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)   # задача 1
        self.assertEqual(self._status_of(self.kt[1]), sm.STATUS_OPEN)   # задача 2 (wait→open)


class TestVoiceSttFallback(_VoiceDigestBase):
    def test_req_6_2_empty_transcript_fallback_hint(self):
        # REQ 6.2: STT вернул пусто → честная подсказка-fallback (не молчим, не теряем).
        claimed, sent, tr = self._run("")
        self.assertTrue(claimed)
        tr.assert_called_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], ml.DIGEST_VOICE_STT_HINT)  # именно STT-фолбэк
        self.assertEqual(sm.load_task_status(self.sd), {})   # ничего не записано

    def test_none_transcript_fallback_hint(self):
        # STT недоступен целиком (None) → тот же честный фолбэк.
        claimed, sent, tr = self._run(None)
        self.assertTrue(claimed)
        self.assertEqual(sent[0], ml.DIGEST_VOICE_STT_HINT)

    def test_whitespace_only_transcript_fallback(self):
        # Расшифровка из одних пробелов = пусто → фолбэк, не пустой прогон парсера.
        claimed, sent, tr = self._run("   \n  ")
        self.assertTrue(claimed)
        self.assertEqual(sent[0], ml.DIGEST_VOICE_STT_HINT)

    def test_transcribed_no_status_marker_stays_silent(self):
        # Расшифровали, но статус-маркера НЕТ («спасибо, всем хорошего дня») → молчим,
        # как и на текстовом пути (не шумим в рабочем чате). Ничего не теряем: терялся бы
        # только НЕПОНЯТЫЙ статус, а его тут нет.
        claimed, sent, tr = self._run("спасибо большое всем хорошего дня")
        self.assertTrue(claimed)
        tr.assert_called_once()
        self.assertEqual(sent, [])                           # ни ack, ни подсказки
        self.assertEqual(sm.load_task_status(self.sd), {})


class TestVoiceFlagOff(_VoiceDigestBase):
    def test_flag_off_keeps_current_hint_and_skips_stt(self):
        # Гейт OFF (тестовый дефолт): голос отбивается DIGEST_VOICE_HINT, реальный STT
        # НЕ дёргается (офлайн-тесты не зовут Groq — защита из [[reissue-llm-tier-gate-default-off]]).
        claimed, sent, tr = self._run("медиаплан сделали\nрасчёт закрыли", flag_on=False)
        self.assertTrue(claimed)
        tr.assert_not_called()                               # STT не тронут при OFF
        self.assertEqual(sent, [ml.DIGEST_VOICE_HINT])       # текущая отбивка
        self.assertEqual(sm.load_task_status(self.sd), {})   # ничего не применено


class TestVoiceDangerTriple(_VoiceDigestBase):
    def test_transcript_secret_not_in_logs(self):
        # Опасная тройка (REQ 6.3): расшифровка НЕ уходит в лог — только длина/счётчики.
        logging.disable(logging.NOTSET)                      # снять возможный глоб. disable
        self.addCleanup(logging.disable, logging.NOTSET)
        secret = "СЕКРЕТНАЯ_МАРЖА_ИЗ_ГОЛОСА"
        transcript = f"медиаплан сделали {secret}\nрасчёт по складу закрыли"
        canned = {"statuses": [f"{_S_MEDIA} {secret}", _S_RASCH], "edits": [], "unclear": []}
        env = {"ENABLE_DIGEST_VOICE_REPLY": "1", "ENABLE_MIXED_REPLY_SPLIT": "1"}
        sent: list = []
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ml, "transcribe_voice_or_none", return_value=transcript), \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)), \
             self.assertLogs(level="INFO") as cm:
            ml.maybe_route_to_digest_reply("tok", self.CHAT, self._voice_msg())
        blob = "\n".join(cm.output)
        self.assertNotIn(secret, blob)                       # секрет из расшифровки не в логах
        self.assertIn("chars=", blob)                        # но длина расшифровки залогирована


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
