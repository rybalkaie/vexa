# -*- coding: utf-8 -*-
"""Тесты Ф5 (план `2026-07-14-reliability-and-open-tasks-ux.md`, ISS-33):
расщепление СМЕШАННОГО reply участника на статус-часть + правочную часть.

Корень — реальный кейс Ольги Новиковой: 4 правки + 2 статуса ОДНИМ сообщением, бот
принял статус ОДНОГО вопроса и прожевал всё, правки + второй статус молча пропали.
Тихая потеря — худший режим.

Покрывает критерий «сделано» Ф5 (бинарный):
  - сплиттер: флаг дефолт-OFF (claude не зовётся), стаб НЕ зовёт реальный claude,
    парсинг/нормализация, гранулярность «сегмент на каждый статус» (РИСК4);
  - `should_engage`: берёмся только на смешанном (≥2 статуса / статус+правки /
    статус+непонятое); чистый одиночный статус → False (РИСК5);
  - `apply_status_reply` в цикле по сегментам → ОБА висяка получают статус (РИСК4);
  - `route_feedback_reply(send=…)` — ack правок подавляется, факт приёма виден;
  - объединённый ack: «статусы: N; правки: M; не понял: …» (REQ 33.3/33.4/33.5);
  - listener-роут: смешанный, мульти-статус, статус+мусор, кейс Ольги, флаг OFF =
    текущее поведение, чистый статус + флаг ON → reissue НЕ триггерится (РИСК5);
  - опасная тройка: текст reply/сегментов в лог не уходит.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss33_mixed_reply_split -v
"""
from __future__ import annotations

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
from notary.lib import feedback_worker  # noqa: E402
from notary.lib import feedback_state  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402


# Протокол с двумя висяками под одним исполнителем (даёт непустой хвост).
_PROTO = """#протоколвстречи 03.06.2026

**Встреча:** Координация по маркетплейсам.

**Длительность:** 25 мин

**Участники:** Илья Рыбалка, Татьяна Филиппова

**Транскрипт:** [2026-06-03.md](2026-06-03.md)

---

## 1) Маркетплейсы

▪️ Обсудили план на июль.

## Задачи

**Татьяна Филиппова**
- Подготовить медиаплан на июль
- Прислать расчёт по складу Ozon
"""

_ITEMS = [
    "Татьяна Филиппова: Подготовить медиаплан на июль",
    "Татьяна Филиппова: Прислать расчёт по складу Ozon",
]

# Сегменты, привязывающиеся к конкретным висякам (match_pending_item по токенам).
_SEG_MEDIAPLAN_CANCEL = "медиаплан снимаем, неактуально"        # → item0, CANCELLED
_SEG_RASCHET_DONE = "по расчёту склада всё закрыли"            # → item1, DONE


def _on():
    """Контекст-менеджер: флаг сплиттера ON."""
    return mock.patch.dict(os.environ, {"ENABLE_MIXED_REPLY_SPLIT": "1"})


# ==========================================================================
# Модуль mixed_reply_split — гейт, парсинг, нормализация, стаб без claude
# ==========================================================================
class TestSplitModule(unittest.TestCase):

    def test_flag_default_off_returns_none_and_no_llm(self):
        # Дефолт-OFF: split_reply → None и _request_split НЕ зовётся (claude не дёрнут).
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_MIXED_REPLY_SPLIT", None)
            with mock.patch.object(mrs, "_request_split") as req:
                self.assertIsNone(mrs.split_reply("медиаплан закрыли, а имя не то", _ITEMS))
                req.assert_not_called()

    def test_stub_split_fn_used_no_real_claude(self):
        # Стаб split_fn заменяет реальный вызов → call_claude_print не достигается.
        canned = {"statuses": [_SEG_RASCHET_DONE], "edits": ["не Дрим Стори, а 23МПКТК"],
                  "unclear": []}
        stub = mock.Mock(return_value=canned)
        with _on(), mock.patch("lib.claude_cli.subprocess.run",
                               side_effect=AssertionError("реальный claude вызван!")):
            res = mrs.split_reply("текст reply", _ITEMS, split_fn=stub)
        stub.assert_called_once()
        self.assertEqual(res["status_segments"], [_SEG_RASCHET_DONE])
        self.assertEqual(res["edits"], ["не Дрим Стори, а 23МПКТК"])
        self.assertEqual(res["unclear"], "")

    def test_dirty_input_no_llm(self):
        # Пусто / без кириллицы / слишком длинно → None без обращения к LLM.
        with _on():
            for bad in ("", "   ", "abcdef only latin", "я" * 5000):
                with mock.patch.object(mrs, "_request_split") as req:
                    self.assertIsNone(mrs.split_reply(bad, _ITEMS))
                    req.assert_not_called()

    def test_parse_valid_and_fence(self):
        raw = ('```json\n{"statuses":["a","b"],"edits":["c"],"unclear":["d"]}\n```')
        r = mrs.parse_split_response(raw)
        self.assertEqual(r["status_segments"], ["a", "b"])
        self.assertEqual(r["edits"], ["c"])
        self.assertEqual(r["unclear"], "d")

    def test_parse_garbage_none(self):
        for bad in ("", "нет json", "[1,2,3]"):
            self.assertIsNone(mrs.parse_split_response(bad))

    def test_parse_dedup_and_empty_drop(self):
        raw = '{"statuses":["x","x",""],"edits":[],"unclear":[]}'
        r = mrs.parse_split_response(raw)
        self.assertEqual(r["status_segments"], ["x"])   # дубль и пустое отброшены
        self.assertEqual(r["edits"], [])

    # ── should_engage: инвариант вовлечения ────────────────────────────────
    def test_engage_none(self):
        self.assertFalse(mrs.should_engage(None))

    def test_engage_no_status_but_edits_true(self):
        # ISS-37 (было False до 20.07.2026): сплиттер сказал «статусов нет», а правки
        # есть → берём reply СЕБЕ. Отпустить нельзя: следом стоит одиночный статус-роут,
        # он сматчит текст целиком и закроет висяк словами из правок (боевой инцидент
        # 20.07 — закрылись две живые задачи, 12+5 правок потерялись).
        self.assertTrue(mrs.should_engage(
            {"status_segments": [], "edits": ["x"], "unclear": "y"}))

    def test_engage_nothing_at_all_false(self):
        # Сплиттер не нашёл вообще ничего → mixed не берётся (дыру закрывает роут
        # в listener: отдаёт такой reply шлюзу правок, минуя статус-роут).
        self.assertFalse(mrs.should_engage(
            {"status_segments": [], "edits": [], "unclear": ""}))

    def test_engage_single_clean_status_false(self):
        # РИСК5: чистый одиночный статус → статус-роут, шлюз правок не зовём
        self.assertFalse(mrs.should_engage(
            {"status_segments": ["a"], "edits": [], "unclear": ""}))

    def test_engage_multi_status_true(self):
        self.assertTrue(mrs.should_engage(
            {"status_segments": ["a", "b"], "edits": [], "unclear": ""}))

    def test_engage_status_plus_edit_true(self):
        self.assertTrue(mrs.should_engage(
            {"status_segments": ["a"], "edits": ["e"], "unclear": ""}))

    def test_engage_status_plus_unclear_true(self):
        self.assertTrue(mrs.should_engage(
            {"status_segments": ["a"], "edits": [], "unclear": "?"}))

    # ── build_mixed_ack: инвариант «ничего молча» ──────────────────────────
    def test_ack_mentions_statuses_and_edits(self):
        ack = mrs.build_mixed_ack(n_status_ok=2, n_status_unresolved=0,
                                  n_edits=4, edit_queued=True, unclear="")
        self.assertIn("2", ack)
        self.assertIn("4", ack)
        self.assertIn("татус", ack)   # «Статусы»
        self.assertIn("равки", ack)   # «Правки»

    def test_ack_unclear_marked(self):
        ack = mrs.build_mixed_ack(n_status_ok=1, n_status_unresolved=0,
                                  n_edits=0, edit_queued=False, unclear="белиберда")
        self.assertIn("не понял", ack.lower())
        self.assertIn("белиберда", ack)

    def test_ack_unresolved_status_surfaced(self):
        ack = mrs.build_mixed_ack(n_status_ok=0, n_status_unresolved=1,
                                  n_edits=0, edit_queued=False, unclear="")
        self.assertIn("не смог привязать", ack)

    def test_request_split_logs_no_text(self):
        # Опасная тройка: _request_split логирует только форму, без текста reply.
        secret = "СЕКРЕТНАЯ_РЕПЛИКА_ПРО_МАРЖУ"
        raw = '{"statuses":["a"],"edits":[],"unclear":[]}'
        # call_claude_print импортируется лениво `from .claude_cli import` внутри
        # _request_split → при `mrs = notary.lib.mixed_reply_split` резолвится в
        # notary.lib.claude_cli (двойной sys.path: lib.* ≠ notary.lib.* — FU-5).
        # Мокаем ИМЕННО notary.lib.claude_cli, иначе мок промахивается и тест
        # дёргает реальный claude (анти-паттерн reissue-llm-tier-gate-default-off).
        with mock.patch("notary.lib.claude_cli.call_claude_print", return_value=raw), \
             self.assertLogs("notary.mixed_reply_split", level="INFO") as cm:
            mrs._request_split(secret, _ITEMS)
        blob = "\n".join(cm.output)
        self.assertNotIn(secret, blob)


# ==========================================================================
# РИСК4 — apply_status_reply В ЦИКЛЕ по сегментам → ОБА висяка получают статус
# ==========================================================================
class _SeriesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-06-03", _PROTO,
                               {"series": "koord", "date": "2026-06-03"})

    def tearDown(self):
        self._tmp.cleanup()


class TestApplyStatusLoop(_SeriesBase):

    def test_two_segments_both_resolved(self):
        # РИСК4/REQ 33.2: два статус-сегмента → ОБА висяка получают статус (не один).
        for seg in (_SEG_RASCHET_DONE, _SEG_MEDIAPLAN_CANCEL):
            r = p.apply_status_reply(seg, self.sd, date="2026-06-10", source="mixed-reply")
            self.assertIsNotNone(r, seg)
        store = sm.load_task_status(self.sd)
        k_media = sm._status_key(_ITEMS[0])
        k_rasch = sm._status_key(_ITEMS[1])
        self.assertEqual(store[k_media]["status"], sm.STATUS_CANCELLED)
        self.assertEqual(store[k_rasch]["status"], sm.STATUS_DONE)


# ==========================================================================
# route_feedback_reply(send=…) — ack правок подавляется, факт приёма виден
# ==========================================================================
class TestSendForwarding(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fbroot = Path(self._tmp.name) / "_feedback_edits"
        self.protocols = Path(self._tmp.name) / "protocols"
        self.protocols.mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {
            "FEEDBACK_WINDOW_MIN": "20", "FEEDBACK_MAX_WINDOW_MIN": "120",
            "ENABLE_FEEDBACK_EDITS": "1",
            "MEETING_NOTARY_PEOPLE_MD": "/nonexistent/people.md",
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.fbroot),
            "MEETING_NOTARY_DELIVERED_ROOTS": str(self.protocols),
        })
        self._env.start()
        feedback_worker._INDEX_CACHE.update(built_at=0.0, roots=None, index={})
        d = self.protocols / "coord"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(
            '{"series":"coord","date":"2026-06-02","expectedParticipants":["Михаил Саргин"],'
            '"delivered":[{"chat_id":-1001,"message_ids":[101],"at":"2026-06-02T11:00:00Z"}]}',
            encoding="utf-8")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _msg(self):
        return {
            "message_id": 555, "chat": {"id": -1001},
            "from": {"id": 777, "first_name": "Михаил"},
            "reply_to_message": {"message_id": 101, "text": "📋 Протокол…"},
            "text": "не Дрим Стори, а 23МПКТК",
        }

    def test_injected_send_captures_ack_telegram_silent(self):
        # send= инъекция: ack уходит в наш capture, telegram_api.send_message молчит.
        captured = []
        real_send = mock.Mock()
        with mock.patch.object(feedback_worker.telegram_api, "send_message", real_send):
            handled = feedback_worker.route_feedback_reply(
                "tok", -1001, self._msg(), allowed_chat=42, root=self.fbroot,
                send=lambda *a, **k: captured.append((a, k)),
            )
        self.assertTrue(handled)
        self.assertEqual(len(captured), 1)          # ack ушёл в наш send
        real_send.assert_not_called()               # отдельного ack правок НЕ было
        # правка записана в state (перевыпуск состоится)
        self.assertTrue(feedback_state.list_states(root=self.fbroot))


# ==========================================================================
# Listener-роут maybe_route_to_mixed_reply — сквозной сценарий
# ==========================================================================
class TestMixedRoute(_SeriesBase):

    GROUP = 222
    OWNER = 111

    def _msg(self, text):
        return {
            "chat": {"id": self.GROUP},
            "from": {"id": 777, "first_name": "Ольга"},
            "reply_to_message": {"message_id": 555, "text": "📅 протокол"},
            "text": text,
            "message_id": 999,
            "date": 1718000000,
        }

    def _run(self, msg, canned, *, edit_accepts=True):
        """Прогон роута с подставленным сплитом и застабленным шлюзом правок.

        `canned` — dict как из LLM. `edit_accepts` — принял ли шлюз правку (зовёт send).
        Возвращает (claimed, sent_acks, edit_calls)."""
        sent = []
        edit_calls = []

        def _fake_gateway(token, chat_id, edit_msg, *, allowed_chat, send):
            edit_calls.append(edit_msg.get("text"))
            if edit_accepts:
                send(token, chat_id, "ack-правок", reply_to_message_id=edit_msg.get("message_id"))
            return True

        with _on(), \
             mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(feedback_worker, "route_feedback_reply", _fake_gateway), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda *a, **k: sent.append((a, k))):
            claimed = ml.maybe_route_to_mixed_reply("tok", self.GROUP, self.OWNER, msg)
        return claimed, sent, edit_calls

    def test_mixed_status_and_edit(self):
        # REQ 33.1/33.3: статус проставлен И правка ушла в шлюз; ОДИН ack про обе части.
        canned = {"statuses": [_SEG_RASCHET_DONE], "edits": ["не Дрим Стори, а 23МПКТК"],
                  "unclear": []}
        claimed, sent, edit_calls = self._run(self._msg("по складу закрыли, и вместо X правильно Y"), canned)
        self.assertTrue(claimed)
        self.assertEqual(len(sent), 1)                        # один объединённый ack
        ack = sent[0][0][2]
        # Ф2/RISK8: ack протокола теперь СТРУКТУРНЫЙ — статус-часть по РАСПОЗНАННОМУ
        # ярлыку («Закрыто»), правочная часть — «Правки текста».
        self.assertIn("Закрыто", ack)
        self.assertIn("равки", ack)
        self.assertEqual(len(edit_calls), 1)                  # шлюз правок вызван
        self.assertIn("23МПКТК", edit_calls[0])
        # статус реально записан
        store = sm.load_task_status(self.sd)
        self.assertEqual(store[sm._status_key(_ITEMS[1])]["status"], sm.STATUS_DONE)

    def test_multi_status_both_applied(self):
        # РИСК4/REQ 33.2: два статуса → ОБА висяка получают статус (не один).
        canned = {"statuses": [_SEG_RASCHET_DONE, _SEG_MEDIAPLAN_CANCEL], "edits": [],
                  "unclear": []}
        claimed, sent, edit_calls = self._run(self._msg("склад закрыли, медиаплан снимаем"), canned)
        self.assertTrue(claimed)
        self.assertEqual(edit_calls, [])                      # правок нет → шлюз не зван
        store = sm.load_task_status(self.sd)
        self.assertEqual(store[sm._status_key(_ITEMS[0])]["status"], sm.STATUS_CANCELLED)
        self.assertEqual(store[sm._status_key(_ITEMS[1])]["status"], sm.STATUS_DONE)
        ack = sent[0][0][2]                                   # Ф2: структурный ack по группам
        self.assertIn("Снято", ack)                          # медиаплан → cancelled
        self.assertIn("Закрыто", ack)                        # склад → done

    def test_status_plus_garbage(self):
        # REQ 33.4: статус + мусор → ack содержит и статус, и «эту часть не понял».
        canned = {"statuses": [_SEG_RASCHET_DONE], "edits": [],
                  "unclear": ["абырвалг непонятное"]}
        claimed, sent, edit_calls = self._run(self._msg("склад закрыли, абырвалг"), canned)
        self.assertTrue(claimed)
        self.assertEqual(edit_calls, [])
        ack = sent[0][0][2]
        self.assertIn("Закрыто", ack)                        # Ф2: статус-часть по ярлыку
        self.assertIn("не понял", ack.lower())
        self.assertIn("абырвалг", ack)

    def test_risk5_pure_status_no_reissue(self):
        # РИСК5: чистый одиночный статус + флаг ON → роут НЕ берётся (False), шлюз
        # правок НЕ зван (лишний reissue не триггерится), mixed-ack не шлётся.
        canned = {"statuses": [_SEG_RASCHET_DONE], "edits": [], "unclear": []}
        claimed, sent, edit_calls = self._run(self._msg("просто закрыли"), canned)
        self.assertFalse(claimed)
        self.assertEqual(edit_calls, [])
        self.assertEqual(sent, [])

    def test_olga_case_all_parts_accounted(self):
        # Реальный кейс Ольги: 4 правки + 2 статуса → оба статуса, все правки, один ack.
        canned = {
            "statuses": [_SEG_RASCHET_DONE, _SEG_MEDIAPLAN_CANCEL],
            "edits": ["не Дрим Стори, а 23МПКТК", "имя не Петров, а Петухов",
                      "убери последний абзац", "сумма не 100, а 150"],
            "unclear": [],
        }
        claimed, sent, edit_calls = self._run(self._msg("склад закрыли, медиаплан снимаем, и правки"), canned)
        self.assertTrue(claimed)
        store = sm.load_task_status(self.sd)
        self.assertEqual(store[sm._status_key(_ITEMS[0])]["status"], sm.STATUS_CANCELLED)
        self.assertEqual(store[sm._status_key(_ITEMS[1])]["status"], sm.STATUS_DONE)
        self.assertEqual(len(edit_calls), 1)                  # 4 правки одним блобом
        ack = sent[0][0][2]
        self.assertIn("Снято", ack)                           # статусы: медиаплан снят
        self.assertIn("Закрыто", ack)                         # склад закрыт
        self.assertIn("4", ack)                               # правки: 4

    def test_flag_off_is_current_behavior(self):
        # Флаг OFF → роут сразу False, ничего не сплитит/не шлёт (обр. совместимость).
        req = mock.Mock()
        sent = []
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_MIXED_REPLY_SPLIT", None)
            with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
                 mock.patch.object(ml, "_protokol_root", return_value=self.root), \
                 mock.patch.object(mrs, "_request_split", req), \
                 mock.patch.object(ml, "send_message",
                                   side_effect=lambda *a, **k: sent.append(a)):
                claimed = ml.maybe_route_to_mixed_reply(
                    "tok", self.GROUP, self.OWNER, self._msg("медиаплан закрыли, имя не то"))
        self.assertFalse(claimed)
        req.assert_not_called()
        self.assertEqual(sent, [])
        self.assertEqual(sm.load_task_status(self.sd), {})

    def test_not_reply_falls_through(self):
        msg = self._msg("медиаплан закрыли, а имя не то")
        msg.pop("reply_to_message")
        with _on():
            self.assertFalse(ml.maybe_route_to_mixed_reply("tok", self.GROUP, self.OWNER, msg))

    def test_voice_not_our_path(self):
        msg = self._msg("x")
        msg.pop("text")
        msg["voice"] = {"file_id": "v"}
        with _on():
            self.assertFalse(ml.maybe_route_to_mixed_reply("tok", self.GROUP, self.OWNER, msg))


if __name__ == "__main__":
    unittest.main()
