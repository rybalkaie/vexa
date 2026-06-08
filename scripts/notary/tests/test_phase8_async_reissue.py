"""Ф8 — декомпозиция claim/heavy/finalize для фоновости перевыпуска.

Unit-тесты на ОТДЕЛЬНЫЕ чистые операции, на которые разложен монолит
`process_ready_reissues` (см. план 2026-06-08-async-protocol-reissue, Фаза 1):
  - `claim_ready_reissues` — синхронный claim-этап (reclaim + claim-цикл + бюджет
    max_n + skip_fids), без вызова claude;
  - `finalize_reissue` — синхронный перевод статуса (conditional dormant/revert,
    still_reissuing-проверка, инкремент attempts на ошибке);
  - `reclaim_stale_reissuing(skip_fids=…)` — РИСК1: живой future не реклеймится,
    сколько бы генерация ни шла (защита от двойной доставки при фоновости);
  - `feedback_state.cleanup_dormant_states` — R10, уборка старых dormant-state'ов.

Поведение `process_ready_reissues` (back-compat-обёртка) проверяется в
tests.test_phase4_feedback_reissue — здесь его не дублируем.

Запуск: python3 -m unittest tests.test_phase8_async_reissue (system python3.9, без venv).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import feedback_state, feedback_reissue  # noqa: E402

UTC = timezone.utc


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "_feedback_edits"
        self.root.mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _state(self, *, series="coord", date="2026-06-02", chat_id=-1001,
               status="ready_for_reissue", attempts=0, mids=(101, 102), rnd=1):
        fid = feedback_state.build_feedback_id(series, date, chat_id)
        st = {
            "feedback_id": fid, "series": series, "date": date, "chat_id": chat_id,
            "meta_path": "/x/meta.json", "protocol_message_ids": list(mids),
            "round": rnd, "status": status, "reissue_attempts": attempts,
            "edits": [{"author": "M", "text": "правка", "tg_message_id": 1}],
        }
        feedback_state.write_state(st, root=self.root)
        return st


# ===========================================================================
# claim_ready_reissues — синхронный claim-этап (без claude)
# ===========================================================================
class TestClaimReadyReissues(_Base):
    def test_claims_ready_and_marks_reissuing(self):
        st = self._state(status="ready_for_reissue")
        claimed = feedback_reissue.claim_ready_reissues(root=self.root, max_n=5)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["status"], "reissuing")
        self.assertIn("reissue_claimed_at", claimed[0])
        # На диске тоже reissuing.
        on_disk = feedback_state.read_state(st["feedback_id"], root=self.root)
        self.assertEqual(on_disk["status"], "reissuing")

    def test_respects_max_n(self):
        for i in range(3):
            self._state(series=f"s{i}")
        claimed = feedback_reissue.claim_ready_reissues(root=self.root, max_n=2)
        self.assertEqual(len(claimed), 2)
        # Ровно 2 в reissuing, одна осталась ready.
        reissuing = feedback_state.list_states(root=self.root, status_filter=["reissuing"])
        ready = feedback_state.list_states(root=self.root, status_filter=["ready_for_reissue"])
        self.assertEqual(len(reissuing), 2)
        self.assertEqual(len(ready), 1)

    def test_skip_fids_not_claimed(self):
        st = self._state(series="a")
        self._state(series="b")
        claimed = feedback_reissue.claim_ready_reissues(
            root=self.root, max_n=5, skip_fids={st["feedback_id"]},
        )
        fids = {c["feedback_id"] for c in claimed}
        self.assertNotIn(st["feedback_id"], fids)
        self.assertEqual(len(claimed), 1)
        # Пропущенный остался ready (не заклеймлен).
        self.assertEqual(
            feedback_state.read_state(st["feedback_id"], root=self.root)["status"],
            "ready_for_reissue",
        )

    def test_attempts_cap_skipped_not_claimed(self):
        st = self._state(attempts=feedback_state.MAX_REISSUE_ATTEMPTS)
        claimed = feedback_reissue.claim_ready_reissues(root=self.root, max_n=5)
        self.assertEqual(claimed, [])
        self.assertEqual(
            feedback_state.read_state(st["feedback_id"], root=self.root)["status"],
            "ready_for_reissue",
        )

    def test_reclaims_stale_before_claiming(self):
        # reissuing старше потолка → reclaim вернёт в ready → его же заклеймим заново.
        st = self._state(status="reissuing", attempts=0)
        cur = feedback_state.read_state(st["feedback_id"], root=self.root)
        # claim_ready_reissues зовёт reclaim с реальным now(); чтобы детерминированно
        # реклеймнуть — сдвигаем claimed на >900с назад относительно реального now.
        old = (datetime.now(UTC) - timedelta(seconds=2000)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur["reissue_claimed_at"] = old
        feedback_state.write_state(cur, root=self.root)
        claimed = feedback_reissue.claim_ready_reissues(root=self.root, max_n=5)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["status"], "reissuing")
        self.assertEqual(claimed[0]["reissue_attempts"], 1)  # reclaim инкрементнул


# ===========================================================================
# finalize_reissue — синхронный перевод статуса (без claude)
# ===========================================================================
class TestFinalizeReissue(_Base):
    def test_sent_to_dormant_with_mids(self):
        self._state(status="reissuing")
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        claimed = feedback_state.read_state(fid, root=self.root)
        feedback_reissue.finalize_reissue(
            fid, claimed, {"status": "sent", "message_ids": [9001, 9002]}, root=self.root,
        )
        final = feedback_state.read_state(fid, root=self.root)
        self.assertEqual(final["status"], "dormant")
        self.assertEqual(final["protocol_message_ids"], [9001, 9002])
        self.assertEqual(final["reissue_attempts"], 0)
        self.assertEqual(final["last_reissue_status"], "sent")

    def test_dormant_falls_back_to_claimed_mids(self):
        # res без message_ids → берём protocol_message_ids из claimed-state.
        self._state(status="reissuing", mids=(101, 102))
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        claimed = feedback_state.read_state(fid, root=self.root)
        feedback_reissue.finalize_reissue(fid, claimed, {"status": "sent"}, root=self.root)
        final = feedback_state.read_state(fid, root=self.root)
        self.assertEqual(final["protocol_message_ids"], [101, 102])

    def test_error_reverts_to_ready_increments_attempts(self):
        self._state(status="reissuing", attempts=0)
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        claimed = feedback_state.read_state(fid, root=self.root)
        feedback_reissue.finalize_reissue(
            fid, claimed, {"status": "error", "error": "claude down"}, root=self.root,
        )
        final = feedback_state.read_state(fid, root=self.root)
        self.assertEqual(final["status"], "ready_for_reissue")
        self.assertEqual(final["reissue_attempts"], 1)
        self.assertIn("claude down", final["last_reissue_error"])

    def test_error_increments_from_base_attempts(self):
        # base_attempts из claimed-state (а не из текущего файла).
        self._state(status="reissuing", attempts=2)
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        claimed = feedback_state.read_state(fid, root=self.root)
        feedback_reissue.finalize_reissue(
            fid, claimed, {"status": "error", "error": "boom"}, root=self.root,
        )
        final = feedback_state.read_state(fid, root=self.root)
        self.assertEqual(final["reissue_attempts"], 3)

    def test_not_still_reissuing_left_untouched(self):
        # Конкурентный reply открыл новый раунд (collecting) — finalize не трогает.
        self._state(status="collecting", rnd=2)
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        # claimed-state — снимок ДО конкурентного раунда (был reissuing).
        claimed = {"feedback_id": fid, "round": 1, "reissue_attempts": 0,
                   "protocol_message_ids": [101]}
        feedback_reissue.finalize_reissue(
            fid, claimed, {"status": "sent", "message_ids": [9001]}, root=self.root,
        )
        final = feedback_state.read_state(fid, root=self.root)
        self.assertEqual(final["status"], "collecting")  # новый раунд не затёрт
        self.assertEqual(final["round"], 2)

    def test_no_state_file_no_crash(self):
        # finalize на исчезнувший state — без исключения (still_reissuing=False).
        claimed = {"feedback_id": "fb-x-y-z", "round": 1, "reissue_attempts": 0}
        res = feedback_reissue.finalize_reissue(
            "fb-x-y-z", claimed, {"status": "sent"}, root=self.root,
        )
        self.assertEqual(res, "sent")


# ===========================================================================
# РИСК1 — reclaim не трогает живой future (skip_fids)
# ===========================================================================
class TestReclaimSkipFids(_Base):
    def test_live_future_not_reclaimed_even_when_stale(self):
        # reissuing старше 900с, но fid в skip_fids (future жив) → НЕ реклеймим.
        st = self._state(status="reissuing", attempts=0)
        cur = feedback_state.read_state(st["feedback_id"], root=self.root)
        cur["reissue_claimed_at"] = "2026-06-02T10:00:00Z"  # давно
        feedback_state.write_state(cur, root=self.root)
        n = feedback_reissue.reclaim_stale_reissuing(
            root=self.root,
            now=datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC),
            skip_fids={st["feedback_id"]},
        )
        self.assertEqual(n, 0)
        self.assertEqual(
            feedback_state.read_state(st["feedback_id"], root=self.root)["status"],
            "reissuing",  # остался в работе
        )

    def test_same_state_reclaimed_without_skip(self):
        # Тот же кейс без skip_fids — реклеймится (фиксируем разницу).
        st = self._state(status="reissuing", attempts=0)
        cur = feedback_state.read_state(st["feedback_id"], root=self.root)
        cur["reissue_claimed_at"] = "2026-06-02T10:00:00Z"
        feedback_state.write_state(cur, root=self.root)
        n = feedback_reissue.reclaim_stale_reissuing(
            root=self.root, now=datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(n, 1)
        self.assertEqual(
            feedback_state.read_state(st["feedback_id"], root=self.root)["status"],
            "ready_for_reissue",
        )


# ===========================================================================
# R10 — cleanup_dormant_states
# ===========================================================================
class TestCleanupDormantStates(_Base):
    def _write_dormant(self, *, series, updated_at):
        fid = feedback_state.build_feedback_id(series, "2026-06-02", -1001)
        st = {
            "feedback_id": fid, "series": series, "date": "2026-06-02",
            "chat_id": -1001, "status": "dormant", "round": 1, "edits": [],
        }
        feedback_state.write_state(st, root=self.root)
        # Подменяем updated_at напрямую в файле (write_state ставит now).
        p = feedback_state.path_for(fid, root=self.root)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["updated_at"] = updated_at
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return fid

    def test_old_dormant_removed(self):
        old = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fid = self._write_dormant(series="old", updated_at=old)
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 1)
        self.assertFalse(feedback_state.path_for(fid, root=self.root).exists())

    def test_fresh_dormant_kept(self):
        fresh = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fid = self._write_dormant(series="fresh", updated_at=fresh)
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 0)
        self.assertTrue(feedback_state.path_for(fid, root=self.root).exists())

    def test_non_dormant_kept_even_if_old(self):
        # Старый, но не dormant (collecting) — не трогаем.
        st = self._state(status="collecting")
        p = feedback_state.path_for(st["feedback_id"], root=self.root)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["updated_at"] = (datetime.now(UTC) - timedelta(days=99)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 0)
        self.assertTrue(p.exists())

    def test_dormant_without_updated_at_kept(self):
        # Битая/отсутствующая метка времени — консервативно НЕ удаляем.
        fid = feedback_state.build_feedback_id("noupd", "2026-06-02", -1001)
        st = {"feedback_id": fid, "series": "noupd", "date": "2026-06-02",
              "chat_id": -1001, "status": "dormant", "round": 1}
        p = feedback_state.path_for(fid, root=self.root)
        p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")  # без updated_at
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 0)
        self.assertTrue(p.exists())

    def test_hidden_tempfiles_ignored(self):
        # Скрытый tempfile с суффиксом state не должен ни читаться, ни удаляться.
        hidden = self.root / f".half{feedback_state.STATE_SUFFIX}"
        hidden.write_text("{not json", encoding="utf-8")
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 0)
        self.assertTrue(hidden.exists())

    def test_only_state_suffix_files_touched(self):
        # Посторонний файл без STATE_SUFFIX в папке — недостижим для cleanup.
        other = self.root / "protocol.md"
        other.write_text("важный файл", encoding="utf-8")
        old = (datetime.now(UTC) - timedelta(days=99)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_dormant(series="old", updated_at=old)
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 1)  # удалён только dormant-state
        self.assertTrue(other.exists())  # посторонний файл цел

    def test_missing_root_returns_zero(self):
        removed = feedback_state.cleanup_dormant_states(
            root=Path(self._tmp.name) / "nonexistent", max_age_days=30,
        )
        self.assertEqual(removed, 0)


# ===========================================================================
# У1 (цикл5/ход3): спасение правок упавшего раунда при конкурентном новом раунде
# ===========================================================================
class TestRescueUnappliedEditsOnFailure(_Base):
    """Async-регрессия: пока фоновый перевыпуск round1 ПАДАЛ, пришёл reply →
    открыл round2 (collecting, edits=[e3]); правки round1 [e1,e2] выпали из state.
    finalize(error) при still_reissuing=False обязан СПАСТИ их в текущий раунд,
    иначе тихая потеря коррекций участника."""

    def _claimed_round1(self, fid):
        return {
            "feedback_id": fid, "series": "coord", "date": "2026-06-02",
            "chat_id": -1001, "meta_path": "/x/meta.json", "round": 1,
            "reissue_attempts": 0,
            "edits": [
                {"author": "M", "text": "правка-1", "tg_message_id": 11, "edit_id": "e1"},
                {"author": "M", "text": "правка-2", "tg_message_id": 12, "edit_id": "e2"},
            ],
        }

    def test_failed_round_edits_rescued_into_new_round(self):
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        claimed = self._claimed_round1(fid)
        # Конкурентный reply открыл round2 (collecting) с одной новой правкой.
        feedback_state.write_state({
            "feedback_id": fid, "series": "coord", "date": "2026-06-02",
            "chat_id": -1001, "round": 2, "status": "collecting",
            "edits": [{"author": "K", "text": "правка-3", "tg_message_id": 13, "edit_id": "e3"}],
        }, root=self.root)
        # round1 перевыпуск провалился.
        feedback_reissue.finalize_reissue(
            fid, claimed, {"status": "error", "error": "claude timeout"}, root=self.root,
        )
        final = feedback_state.read_state(fid, root=self.root)
        # round2 жив (не затёрт), статус не менялся.
        self.assertEqual(final["status"], "collecting")
        self.assertEqual(final["round"], 2)
        # Все три правки на месте, старые ВПЕРЁД (раньше по времени).
        tmids = [e["tg_message_id"] for e in final["edits"]]
        self.assertEqual(tmids, [11, 12, 13])

    def test_rescue_dedup_by_tg_message_id(self):
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        claimed = self._claimed_round1(fid)
        # round2 уже содержит одну из правок round1 (e2/12) — не дублировать.
        feedback_state.write_state({
            "feedback_id": fid, "series": "coord", "date": "2026-06-02",
            "chat_id": -1001, "round": 2, "status": "ready_for_reissue",
            "edits": [{"author": "M", "text": "правка-2", "tg_message_id": 12, "edit_id": "e2"}],
        }, root=self.root)
        feedback_reissue.finalize_reissue(
            fid, claimed, {"status": "error", "error": "boom"}, root=self.root,
        )
        final = feedback_state.read_state(fid, root=self.root)
        tmids = sorted(e["tg_message_id"] for e in final["edits"])
        self.assertEqual(tmids, [11, 12])  # 12 не задвоился

    def test_no_rescue_when_round_succeeded(self):
        # terminal-OK + новый раунд → НЕ спасаем (правки уже применены/доставлены).
        fid = feedback_state.build_feedback_id("coord", "2026-06-02", -1001)
        claimed = self._claimed_round1(fid)
        feedback_state.write_state({
            "feedback_id": fid, "series": "coord", "date": "2026-06-02",
            "chat_id": -1001, "round": 2, "status": "collecting",
            "edits": [{"author": "K", "text": "правка-3", "tg_message_id": 13, "edit_id": "e3"}],
        }, root=self.root)
        feedback_reissue.finalize_reissue(
            fid, claimed, {"status": "sent", "message_ids": [9001]}, root=self.root,
        )
        final = feedback_state.read_state(fid, root=self.root)
        # round2 не тронут — только своя правка (никакого re-apply round1).
        tmids = [e["tg_message_id"] for e in final["edits"]]
        self.assertEqual(tmids, [13])

    def test_still_reissuing_failure_unchanged_no_rescue(self):
        # Контроль: статус всё ещё reissuing (нет конкурентного раунда) →
        # обычный revert в ready, attempts++; rescue не вмешивается.
        st = self._state(status="reissuing")
        fid = st["feedback_id"]
        claimed = feedback_state.read_state(fid, root=self.root)
        feedback_reissue.finalize_reissue(
            fid, claimed, {"status": "error", "error": "down"}, root=self.root,
        )
        final = feedback_state.read_state(fid, root=self.root)
        self.assertEqual(final["status"], "ready_for_reissue")
        self.assertEqual(final["reissue_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
