# -*- coding: utf-8 -*-
"""Тесты Ф6 (план `2026-07-16-open-questions-reply-fix.md`, ISS-36 REQ 4.1/4.2):
разовый инструмент `tools/reapply-digest-reply.py` — переприменение УЖЕ НАПИСАННОГО
ответа участника на утренний дайджест через ТОТ ЖЕ путь Ф1–Ф4, что живой digest-роут.

Что проверяем:
  • REQ 4.2 (dry-run) — прогон БЕЗ --apply НЕ пишет в sidecar, но собирает «задача →
    статус» для превью;
  • REQ 4.1 (запись) — прогон с apply=True пишет ровно те же статусы в sidecar Ф2
    (`set_task_status`), идемпотентно по ключу;
  • Q3 (в группу молчим) — `send_message` НИКОГДА не вызывается (ack собирается только
    для сверки владельцем);
  • одиночный fast-path (REQ 2.3), ординал против слепка (Ф4), UPU1 (не разобрал пачку),
    overflow «N из M» (REQ 3.2), «нет статус-маркера»;
  • Опасная тройка — сырой текст ответа НЕ попадает в логи и в возврат инструмента.

ВСЁ на СИНТЕТИКЕ. Реальный текст участника (Михаила) в фикстуры НЕ тащим (опасная
тройка + правило безопасности). Сплиттер пачки — стаб (`mrs._request_split`), реальный
Claude/сеть не зовём (как в Ф2/Ф4/Ф5).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss36_phase6_reapply -v
"""
from __future__ import annotations

import importlib.util
import io
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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

# Инструмент — файл с дефисом в имени: грузим по пути (как хотел бы sys.exit(main())).
_TOOL_PATH = _NOTARY / "tools" / "reapply-digest-reply.py"
_spec = importlib.util.spec_from_file_location("reapply_digest_reply", str(_TOOL_PATH))
rdr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdr)


# Синтетический протокол серии с 5 висяками (те же безопасные фикстуры, что в Ф2/Ф4/Ф5 —
# НЕ реальные задачи/цифры участников).
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

_S_MEDIA = "медиаплан на июль сделали"                # done  → медиаплан
_S_RASCH = "расчёт по складу закрыли"                 # done  → расчёт склад
_S_BUDGET = "бюджет маркетинга снимаем, неактуально"  # cancelled → бюджет
_BATCH3_TEXT = "медиаплан сделали\nрасчёт по складу закрыли\nбюджет снимаем"
_CANNED3 = {"statuses": [_S_MEDIA, _S_RASCH, _S_BUDGET], "edits": [], "unclear": []}


class _ReapplyBase(unittest.TestCase):
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
        # Слепок ДАЙДЖЕСТА по message_id (как send_series_digest пишет в Ф3) — против него
        # резолвится ординал (Ф4, тот же читатель open_tasks_snapshot_for_message).
        sm.record_open_tasks_snapshot(self.sd, self.DIGEST_MID, self.kt,
                                      surface="digest", date="2026-06-03")

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self):
        return sm.load_task_status(self.sd)

    def _status_of(self, key_text):
        rec = self._store().get(sm._status_key(key_text))
        return rec.get("status") if rec else None

    def _reapply(self, text, *, canned=_CANNED3, apply=False, mixed_on=True,
                 reply_mid=None):
        """Прогон инструмента со стаб-сплиттером и изолированным env. Возвращает
        результат `rdr.reapply`. Реальный Claude/сеть НЕ зовём."""
        env = {"ENABLE_MIXED_REPLY_SPLIT": "1" if mixed_on else "0"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(mrs, "_request_split", return_value=canned):
            return rdr.reapply(
                "koord", "2026-06-03", text,
                root=str(self.root), reply_to_message_id=reply_mid, apply=apply,
            )


# ==========================================================================
# REQ 4.2 — dry-run НЕ пишет, но собирает превью
# ==========================================================================
class TestDryRun(_ReapplyBase):
    def test_dry_run_does_not_write_sidecar(self):
        res = self._reapply(_BATCH3_TEXT, apply=False)
        self.assertEqual(len(self._store()), 0)          # sidecar пуст — ничего не записано
        self.assertFalse(res["wrote"])
        self.assertTrue(res["batch"])                    # батч-путь Ф2 сработал
        self.assertEqual(len(res["applied"]), 3)         # но превью показывает 3 задачи

    def test_dry_run_preview_maps_task_to_status(self):
        res = self._reapply(_BATCH3_TEXT, apply=False)
        by_key = {sm._status_key(t): s for (t, s, _r) in res["applied"]}
        self.assertEqual(by_key.get(sm._status_key(self.kt[0])), sm.STATUS_DONE)   # медиаплан
        self.assertEqual(by_key.get(sm._status_key(self.kt[1])), sm.STATUS_DONE)   # расчёт
        self.assertEqual(by_key.get(sm._status_key(self.kt[2])),
                         sm.STATUS_CANCELLED)                                       # бюджет


# ==========================================================================
# REQ 4.1 — реальная запись в sidecar Ф2, идемпотентно
# ==========================================================================
class TestApplyWrites(_ReapplyBase):
    def test_apply_writes_statuses(self):
        res = self._reapply(_BATCH3_TEXT, apply=True)
        self.assertTrue(res["wrote"])
        self.assertEqual(len(self._store()), 3)          # 3 висяка получили статус
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)
        self.assertEqual(self._status_of(self.kt[1]), sm.STATUS_DONE)
        self.assertEqual(self._status_of(self.kt[2]), sm.STATUS_CANCELLED)

    def test_apply_is_idempotent(self):
        self._reapply(_BATCH3_TEXT, apply=True)
        first = dict(self._store())
        self._reapply(_BATCH3_TEXT, apply=True)          # второй прогон — тот же результат
        second = self._store()
        self.assertEqual(len(second), 3)
        self.assertEqual({k: v.get("status") for k, v in second.items()},
                         {k: v.get("status") for k, v in first.items()})

    def test_apply_writes_answer_date_not_today(self):
        # updated в sidecar = дата ОТВЕТА (2026-06-03), а не «сегодня».
        self._reapply(_BATCH3_TEXT, apply=True)
        rec = self._store().get(sm._status_key(self.kt[0]))
        self.assertEqual(rec.get("updated"), "2026-06-03")


# ==========================================================================
# Q3 — в групповой чат НЕ постим НИКОГДА
# ==========================================================================
class TestNoGroupPost(_ReapplyBase):
    def test_send_message_never_called(self):
        with mock.patch.object(ml, "send_message") as spy:
            res = self._reapply(_BATCH3_TEXT, apply=True)
        spy.assert_not_called()                          # ничего не ушло в Telegram
        self.assertTrue(res["ack"])                      # ack сформирован — но для сверки

    def test_send_message_never_called_dry_run(self):
        with mock.patch.object(ml, "send_message") as spy:
            self._reapply(_BATCH3_TEXT, apply=False)
        spy.assert_not_called()


# ==========================================================================
# Одиночный fast-path, ординал, UPU1, overflow, нет-маркера
# ==========================================================================
class TestSingleAndEdgeCases(_ReapplyBase):
    def test_single_status_fast_path(self):
        # Одиночный статус (не пачка) → батч не engage, идёт одиночный apply (REQ 2.3).
        res = self._reapply("медиаплан сделали", canned=None, apply=True)
        self.assertFalse(res["batch"])
        self.assertEqual(len(res["applied"]), 1)
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)

    def test_ordinal_single_via_snapshot(self):
        # «первую сделал» + reply_to слепка дайджеста → задача 1 = done (ординал Ф4).
        res = self._reapply("первую сделал", canned=None, apply=True,
                            reply_mid=self.DIGEST_MID)
        self.assertTrue(res["snapshot_used"])
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)

    def test_ordinal_multi_batch_via_snapshot(self):
        # Пачка ординалов «первая — сделана, вторая — в работе» против слепка дайджеста.
        canned = {"statuses": ["первая — сделана", "вторая — в работе"],
                  "edits": [], "unclear": []}
        self._reapply("первая — сделана, вторая — в работе", canned=canned,
                      apply=True, reply_mid=self.DIGEST_MID)
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)   # задача 1
        self.assertEqual(self._status_of(self.kt[1]), sm.STATUS_OPEN)   # задача 2 (wait→open)

    def test_ordinal_unavailable_without_snapshot(self):
        # Нет reply_to → слепка нет → ординал недоступен, идёт overlap (как у живого бота
        # на старых сообщениях). «первую сделал» тогда overlap'ом не привяжется.
        res = self._reapply("первую сделал", canned=None, apply=True, reply_mid=None)
        self.assertFalse(res["snapshot_used"])
        self.assertEqual(len(self._store()), 0)          # честный остаток, ничего не закрыли

    def test_unparsed_batch_hint(self):
        # Похоже на пачку, но сплит вернул None (LLM икнул) → UPU1, ничего не применяем.
        res = self._reapply(_BATCH3_TEXT, canned=None, apply=True)
        self.assertTrue(res["unparsed_batch"])
        self.assertEqual(len(res["applied"]), 0)
        self.assertEqual(len(self._store()), 0)
        self.assertIn("несколько задач", res["ack"].lower())

    def test_overflow_reported(self):
        # REQ 3.2: сплиттер вернул больше статусов, чем реально сегментов → «N из M».
        canned = {"statuses": [_S_MEDIA, _S_RASCH, _S_BUDGET],
                  "edits": [], "unclear": [], "n_status_raw": 99}
        res = self._reapply(_BATCH3_TEXT, canned=canned, apply=False)
        self.assertEqual(res["overflow"], (3, 99))
        self.assertIn("из 99", res["ack"])

    def test_no_status_markers(self):
        # Реакция без статус-маркера («спасибо, ок») → применять нечего, sidecar пуст.
        res = self._reapply("спасибо, ок 👍", canned=None, apply=True)
        self.assertTrue(res["no_status_markers"])
        self.assertEqual(len(res["applied"]), 0)
        self.assertEqual(len(self._store()), 0)

    def test_flag_off_no_batch(self):
        # ENABLE_MIXED_REPLY_SPLIT=OFF → батч-путь не включается (совместимость).
        res = self._reapply("медиаплан сделали", canned=None, apply=True, mixed_on=False)
        self.assertFalse(res["batch"])


# ==========================================================================
# Опасная тройка — сырой текст ответа не в логи и не в возврат
# ==========================================================================
class TestDangerTriple(_ReapplyBase):
    SENTINEL = "СЕКРЕТ-МАРЖА-9876543"   # синтетический «финансовый» маркер

    def test_reply_text_not_in_logs(self):
        text = f"{_BATCH3_TEXT}\nвыручка {self.SENTINEL} рублей"
        with self.assertLogs("notary", level="DEBUG") as cm:
            logging.getLogger("notary").debug("anchor")  # гарантируем непустой лог-контекст
            self._reapply(text, apply=True)
        blob = "\n".join(cm.output)
        self.assertNotIn(self.SENTINEL, blob)            # финансовый маркер не в логах
        self.assertNotIn("медиаплан на июль сделали", blob)  # и сегменты reply не в логах

    def test_reply_text_not_in_return(self):
        text = f"{_BATCH3_TEXT}\nвыручка {self.SENTINEL} рублей"
        res = self._reapply(text, apply=False)
        # applied содержит только РАСПОЗНАННЫЕ ключи задач (из канона серии), не текст reply.
        for task_text, _s, _r in res["applied"]:
            self.assertNotIn(self.SENTINEL, task_text)
        self.assertNotIn(self.SENTINEL, res["ack"])      # unclear пуст → ack без сырого текста


# ==========================================================================
# CLI main() — dry-run по умолчанию, машинная сводка SUMMARY
# ==========================================================================
class TestCliMain(_ReapplyBase):
    def test_main_dry_run_default_no_write(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(mrs, "_request_split", return_value=_CANNED3), \
             redirect_stdout(buf):
            os.environ.pop("ENABLE_MIXED_REPLY_SPLIT", None)  # main сам выставит setdefault
            rc = rdr.main(["koord", "2026-06-03", "--text", _BATCH3_TEXT,
                           "--root", str(self.root)])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("SUMMARY:", out)
        self.assertIn("mode=dry-run", out)
        self.assertIn("recognized=3", out)
        self.assertEqual(len(self._store()), 0)          # дефолт — dry-run, записи нет

    def test_main_apply_and_dry_run_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            rdr.main(["koord", "2026-06-03", "--text", "x", "--apply", "--dry-run"])

    def test_main_missing_series_errors(self):
        with mock.patch.object(mrs, "_request_split", return_value=_CANNED3), \
             self.assertRaises(SystemExit):
            rdr.main(["no-such-series", "2026-06-03", "--text", _BATCH3_TEXT,
                      "--root", str(self.root)])


if __name__ == "__main__":
    unittest.main()
