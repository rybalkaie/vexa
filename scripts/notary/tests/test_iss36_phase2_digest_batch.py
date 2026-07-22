# -*- coding: utf-8 -*-
"""Тесты Ф2 (план `2026-07-16-open-questions-reply-fix.md`, ISS-36 случай А):
мульти-статус ответом на ДАЙДЖЕСТ + защита от тихой обрезки пачки + краткий
структурный ack.

Корень (случай А): участник отвечает на утренний дайджест ОДНИМ сообщением сразу по
нескольким висякам («медиаплан — сделали, расчёт — закрыли, бюджет — снимаем»).
Старый digest-роут распознавал статус ОДНОГО вопроса и молча терял остальные. Плюс
пачка из ~25 задач срезалась на лимитах 12/12/1200 без единого слова боту.

Покрывает бинарные критерии приёмки:
  • REQ 2.1 — reply на 5 задач+статусов → 5 записей `set_task_status`;
  • REQ 2.2 — 4 из 5 привязались → ack «принял 4» + «1 не смог привязать»;
  • REQ 2.3 — одиночный чистый статус → fast-path, платный сплиттер НЕ зовётся;
  • REQ 2.4 / RAZM1 / OZHID2 — ≥4 статусов: по СТРОКЕ на статус-группу со счётчиком и
    РАСПОЗНАННЫМИ именами; 1-3 — по строке на задачу; имя = сматченное ботом, не эхо;
  • REQ 3.1 / UPU2 — пачка ~25 задач реальной длины (~2-3К) доезжает до сегментации;
  • REQ 3.2 — объём превышен → «обработал N из M»;
  • UPU1 — сплит икнул на явной пачке → честная подсказка, не молча-один.

Опасная тройка: тексты реплик/сегментов в лог не уходят — только счётчики.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss36_phase2_digest_batch -v
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


def _on():
    """Контекст-менеджер: флаг сплиттера ON (иначе digest-роут идёт одиночным путём)."""
    return mock.patch.dict(os.environ, {"ENABLE_MIXED_REPLY_SPLIT": "1"})


# Протокол серии с 5 висяками под одним исполнителем.
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

# Сегменты, каждый уникально матчащийся на свой висяк (проверено match_pending_item).
_S_MEDIA = "медиаплан на июль сделали"            # done  → медиаплан
_S_RASCH = "расчёт по складу закрыли"             # done  → расчёт склад
_S_BUDGET = "бюджет маркетинга снимаем, неактуально"  # cancelled → бюджет
_S_VAKANS = "вакансию дизайнера закрыли"          # done  → вакансия
_S_PRICE = "прайс на витрине сделали"             # done  → прайс
_S_NOMATCH = "юбилейный отчёт готов"              # done, но НИ на что не матчится → unresolved


# ==========================================================================
# Лимиты подняты (UPU2 / REQ 3.1) — документируем требуемые пороги
# ==========================================================================
class TestLimitsRaised(unittest.TestCase):
    def test_reply_len_at_least_4000(self):
        self.assertGreaterEqual(mrs._MAX_REPLY_LEN, 4000)  # UPU2: 2-3К reply доезжает

    def test_status_segments_at_least_30(self):
        self.assertGreaterEqual(mrs._MAX_STATUS_SEGMENTS, 30)

    def test_items_in_prompt_covers_series(self):
        self.assertGreaterEqual(mrs._MAX_ITEMS_IN_PROMPT, 25)  # NES2: подсказки сплиттеру


# ==========================================================================
# looks_like_multi_status — дешёвый пред-чек «похоже на пачку»
# ==========================================================================
class TestLooksLikeMultiStatus(unittest.TestCase):
    def test_single_one_liner_is_not_multi(self):
        self.assertFalse(mrs.looks_like_multi_status("медиаплан на июль сделали"))

    def test_empty_is_not_multi(self):
        self.assertFalse(mrs.looks_like_multi_status(""))
        self.assertFalse(mrs.looks_like_multi_status("   "))

    def test_multiline_is_multi(self):
        self.assertTrue(mrs.looks_like_multi_status("медиаплан сделали\nрасчёт закрыли"))

    def test_numbered_list_is_multi(self):
        self.assertTrue(mrs.looks_like_multi_status("1) медиаплан сделали 2) расчёт закрыли"))

    def test_semicolons_are_multi(self):
        self.assertTrue(mrs.looks_like_multi_status("медиаплан сделали; расчёт закрыли; бюджет снят"))


# ==========================================================================
# run_status_loop — сбор счётчиков + разбивка по статус-группам
# ==========================================================================
class TestRunStatusLoop(unittest.TestCase):
    def test_groups_and_counts(self):
        canned = {
            _S_MEDIA: {"label": "done", "matched": "Илья: медиаплан", "status": "done", "ack": ""},
            _S_RASCH: {"label": "done", "matched": "Илья: расчёт", "status": "done", "ack": ""},
            _S_BUDGET: {"label": "cancelled", "matched": "Илья: бюджет", "status": "cancelled", "ack": ""},
            _S_NOMATCH: None,  # интент есть, висяк не привязался → unresolved
        }
        loop = mrs.run_status_loop(list(canned), lambda seg: canned[seg])
        self.assertEqual(loop["n_ok"], 3)
        self.assertEqual(loop["n_unresolved"], 1)
        groups = dict(loop["groups"])
        self.assertEqual(groups["done"], ["Илья: медиаплан", "Илья: расчёт"])
        self.assertEqual(groups["cancelled"], ["Илья: бюджет"])

    def test_apply_failure_counts_as_unresolved_not_crash(self):
        def boom(seg):
            raise RuntimeError("bang")
        loop = mrs.run_status_loop(["a", "b"], boom)
        self.assertEqual(loop["n_ok"], 0)
        self.assertEqual(loop["n_unresolved"], 2)  # сбой сегмента не роняет пачку


# ==========================================================================
# build_mixed_ack — краткий структурный ack (REQ 2.4 / RAZM1 / OZHID2)
# ==========================================================================
class TestBuildMixedAckGrouped(unittest.TestCase):
    def test_grouped_when_four_or_more(self):
        # ISS-44.4: группа >1 задачи → заголовок «эмодзи Статус (N):» + буллеты ПОЛНЫМИ
        # названиями (без обрезки, без «+ещё»); блоки разделены пустой строкой.
        groups = [
            ("done", [f"Илья: задача{i}" for i in range(10)]),
            ("wait", ["Илья: бюджет", "Илья: найм"]),
            ("cancelled", ["Илья: рассылка"]),
        ]
        ack = mrs.build_mixed_ack(status_groups=groups)
        blocks = ack.split("\n\n")
        self.assertEqual(len(blocks), 3)                # 3 группы = 3 блока (пустая строка между)
        self.assertIn("(10)", blocks[0])                # счётчик группы
        self.assertIn("(2)", blocks[1])
        self.assertNotIn("+ещё", ack)                   # все имена показаны, не обрезаем
        self.assertEqual(ack.count("• "), 12)           # 10 + 2 буллета (одиночная — инлайн)
        self.assertIn("задача9", ack)                   # последнее имя не срезано

    def test_per_task_when_three_or_fewer(self):
        groups = [("done", ["Илья: отчёт"]), ("cancelled", ["Илья: рассылка"])]
        ack = mrs.build_mixed_ack(status_groups=groups)
        blocks = ack.split("\n\n")
        self.assertEqual(len(blocks), 2)                # по блоку на задачу (одиночные — инлайн)
        self.assertIn("«отчёт»", ack)
        self.assertIn("«рассылка»", ack)

    def test_ozhid2_name_is_recognized_not_owner_prefix(self):
        # OZHID2: в ack — РАСПОЗНАННОЕ имя задачи (без префикса исполнителя-эха).
        groups = [("done", ["Илья Рыбалка: Прислать расчёт по складу Ozon"])]
        ack = mrs.build_mixed_ack(status_groups=groups)
        self.assertIn("Прислать расчёт по складу Ozon", ack)
        self.assertNotIn("Илья Рыбалка:", ack)          # префикс исполнителя срезан

    def test_edit_as_hint_no_gate(self):
        # A3: на дайджесте правки — подсказкой «на сам протокол», НЕ «перевыпущу».
        ack = mrs.build_mixed_ack(status_groups=[("done", ["Илья: отчёт"])],
                                  n_edits=2, edit_as_hint=True)
        self.assertIn("на сам протокол", ack)
        self.assertNotIn("перевыпущу", ack)

    def test_overflow_reported(self):
        ack = mrs.build_mixed_ack(status_groups=[("done", ["a", "b", "c"])], overflow=(3, 5))
        self.assertIn("3 из 5", ack)
        self.assertIn("вторым сообщением", ack)

    def test_unresolved_surfaced(self):
        ack = mrs.build_mixed_ack(status_groups=[("done", ["a"])], n_status_unresolved=2)
        self.assertIn("не смог привязать", ack)
        self.assertIn("2", ack)

    def test_legacy_count_only_still_works(self):
        # Обратная совместимость: старый вызов по счётчику (без groups) не падает.
        ack = mrs.build_mixed_ack(n_status_ok=3, n_edits=1, edit_queued=True)
        self.assertIn("3", ack)
        self.assertIn("равки", ack)


# ==========================================================================
# Digest-роут: сквозные сценарии на реальной серии
# ==========================================================================
class _DigestSeriesBase(unittest.TestCase):
    CHAT = -100500

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-06-03", _PROTO5,
                               {"series": "koord", "date": "2026-06-03"})

    def tearDown(self):
        self._tmp.cleanup()

    def _msg(self, text):
        return {
            "message_id": 999,
            "reply_to_message": {"message_id": 4242, "text": "📋 Открытые вопросы"},
            "text": text,
            "date": 1718000000,
        }

    def _run(self, text, canned):
        """Прогон digest-роута с флагом ON и подставленным сплитом.
        `canned` — dict как из LLM (или None для UPU1). Возвращает (claimed, sent)."""
        sent: list = []
        with _on(), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)):
            claimed = ml.maybe_route_to_digest_reply("tok", self.CHAT, self._msg(text))
        return claimed, sent

    def _store(self):
        return sm.load_task_status(self.sd)


class TestDigestBatchRoute(_DigestSeriesBase):

    def test_req_2_1_five_statuses_five_records(self):
        # REQ 2.1: reply на 5 задач+статусов → 5 записей set_task_status.
        canned = {"statuses": [_S_MEDIA, _S_RASCH, _S_BUDGET, _S_VAKANS, _S_PRICE],
                  "edits": [], "unclear": []}
        claimed, sent = self._run("\n".join(canned["statuses"]), canned)
        self.assertTrue(claimed)
        self.assertEqual(len(self._store()), 5)          # все 5 висяков получили статус
        self.assertEqual(len(sent), 1)                   # один объединённый ack

    def test_req_2_2_partial_match_honest_ack(self):
        # REQ 2.2: 4 из 5 привязались → «принял 4» + «1 не смог привязать».
        canned = {"statuses": [_S_MEDIA, _S_RASCH, _S_VAKANS, _S_PRICE, _S_NOMATCH],
                  "edits": [], "unclear": []}
        claimed, sent = self._run("\n".join(canned["statuses"]), canned)
        self.assertTrue(claimed)
        self.assertEqual(len(self._store()), 4)          # 4 записались, 1 нет
        ack = sent[0]
        self.assertIn("4", ack)                          # счётчик принятых (группа «Закрыто (4)»)
        self.assertIn("не смог привязать", ack)          # непривязанный помечен честно
        self.assertIn("1", ack)                          # «Ещё 1 …»

    def test_req_2_3_single_status_fast_path_no_splitter(self):
        # REQ 2.3: одиночный чистый статус → fast-path, платный сплиттер НЕ зовётся.
        req = mock.Mock()
        sent: list = []
        with _on(), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(mrs, "_request_split", req), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)):
            claimed = ml.maybe_route_to_digest_reply("tok", self.CHAT, self._msg(_S_MEDIA))
        self.assertTrue(claimed)
        req.assert_not_called()                          # сплиттер (платный LLM) не тронут
        self.assertEqual(len(self._store()), 1)          # одиночный apply отработал как раньше
        self.assertEqual(len(sent), 1)

    def test_upu1_split_none_on_batch_honest_hint(self):
        # UPU1: reply похож на пачку, но сплит икнул (None) → честная подсказка, НЕ молча-один.
        text = "медиаплан сделали\nрасчёт закрыли\nбюджет снят\nвакансия закрыта"
        claimed, sent = self._run(text, None)            # canned=None → split_reply вернёт None
        self.assertTrue(claimed)
        self.assertEqual(self._store(), {})              # НИ одного статуса молча не проставлено
        self.assertEqual(len(sent), 1)
        self.assertIn("вторым сообщением", sent[0])       # честная подсказка прислать иначе

    def test_ozhid2_ack_shows_recognized_name_not_echo(self):
        # OZHID2: имя в ack = сматченный ботом висяк (в нём есть «Ozon»), не слова участника.
        canned = {"statuses": [_S_MEDIA, _S_RASCH, _S_VAKANS, _S_PRICE],
                  "edits": [], "unclear": []}
        _claimed, sent = self._run("\n".join(canned["statuses"]), canned)
        self.assertIn("Ozon", sent[0])                   # распознанное имя «…склад Ozon», не эхо

    def test_danger_triple_logs_carry_no_reply_text(self):
        # Опасная тройка: в лог только счётчики, тексты реплик/сегментов не уходят.
        logging.disable(logging.NOTSET)                  # снять возможный глобальный disable
        self.addCleanup(logging.disable, logging.NOTSET)
        secret = "СЕКРЕТНАЯ_МАРЖА_РЕПЛИКА"
        seg_secret = f"{_S_MEDIA} {secret}"
        canned = {"statuses": [seg_secret, _S_RASCH, _S_BUDGET, _S_VAKANS], "edits": [], "unclear": []}
        sent: list = []
        with _on(), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)), \
             self.assertLogs(level="INFO") as cm:
            ml.maybe_route_to_digest_reply("tok", self.CHAT, self._msg("\n".join(canned["statuses"])))
        blob = "\n".join(cm.output)
        self.assertNotIn(secret, blob)                   # секрет из reply в логи не утёк


class TestDigestBatch25(_DigestSeriesBase):
    """REQ 3.1 / UPU2: пачка ~25 задач реальной длины доезжает до сегментации целиком."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        tasks = "\n".join(f"- Досье комплект{i:02d} собрать" for i in range(1, 26))
        proto = (
            "#протоколвстречи 03.06.2026\n\n**Встреча:** Ревизия.\n\n**Длительность:** 40 мин\n\n"
            "**Участники:** Илья Рыбалка\n\n**Транскрипт:** [2026-06-03.md](2026-06-03.md)\n\n---\n\n"
            "## 1) Раздел\n\n▪️ Обсудили.\n\n## Задачи\n\n**Илья Рыбалка**\n" + tasks + "\n"
        )
        sm.save_meeting_digest(self.sd, "2026-06-03", proto,
                               {"series": "koord", "date": "2026-06-03"})

    def test_twenty_five_batch_all_applied_no_truncation(self):
        segments = [f"комплект{i:02d} сделали" for i in range(1, 26)]
        # Реальная длина reply участника (~2-3К) — именно её пропускает поднятый лимит (UPU2).
        text = "\n".join(
            f"По пункту комплект{i:02d} мы всё полностью сделали и закрыли, "
            "вопрос снимаю с повестки, спасибо большой команде за работу." for i in range(1, 26)
        )
        self.assertGreater(len(text), 2000)              # реально длинный reply
        self.assertLess(len(text), 4000)
        self.assertTrue(mrs._looks_splittable(text))     # поднятый _MAX_REPLY_LEN пропускает
        canned = {"statuses": segments, "edits": [], "unclear": []}
        claimed, sent = self._run(text, canned)
        self.assertTrue(claimed)
        self.assertEqual(len(self._store()), 25)         # все 25 доехали до матчинга и записались
        self.assertNotIn("Обработал", sent[0])           # overflow НЕ сработал (25 ≤ cap)

    def _run(self, text, canned):
        sent: list = []
        with _on(), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)):
            claimed = ml.maybe_route_to_digest_reply(
                "tok", -100500,
                {"message_id": 999, "reply_to_message": {"message_id": 4242},
                 "text": text, "date": 1718000000})
        return claimed, sent

    def _store(self):
        return sm.load_task_status(self.sd)


class TestDigestOverflow(_DigestSeriesBase):
    """REQ 3.2: объём превышен (сплиттер вернул больше статусов, чем cap) → «N из M»."""

    def test_overflow_reports_n_of_m(self):
        # Урезаем cap до 3 → 5 статусов дают overflow (3 из 5), бот честно сообщает.
        canned = {"statuses": [_S_MEDIA, _S_RASCH, _S_BUDGET, _S_VAKANS, _S_PRICE],
                  "edits": [], "unclear": []}
        sent: list = []
        with _on(), mock.patch.object(mrs, "_MAX_STATUS_SEGMENTS", 3), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)):
            claimed = ml.maybe_route_to_digest_reply("tok", self.CHAT, self._msg("\n".join(canned["statuses"])))
        self.assertTrue(claimed)
        self.assertIn("3 из 5", sent[0])                 # честно: обработал 3 из 5
        self.assertIn("вторым сообщением", sent[0])


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
