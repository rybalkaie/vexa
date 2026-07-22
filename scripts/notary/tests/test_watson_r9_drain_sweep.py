# -*- coding: utf-8 -*-
"""R9 (план `2026-07-20-watson-smart-replies-rethink.md`, Ф6 деплой-блокер): sweep
listener'а РЕАЛЬНО зовёт дренаж очереди дозабора `drain_deferred`, а не просто «функция
существует». Без этой врезки хинт сотруднику «разберу позже» = ложь (reply гниёт в
очереди до TTL 14д). follow-ups-watson-smart-replies-rethink-Ф3.md (FU-Ф3-1).

Офлайн: `_reissue_executor=None` → `_submit_command_job` бежит job СИНХРОННО в главном
потоке (боевой путь тот же, но в фоне); `drain_deferred` замокан — реального claude нет.
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

import notary.meetings_listener as ml  # noqa: E402
from notary.lib import reply_understand  # noqa: E402
from notary.lib import reply_understand_queue as ruq  # noqa: E402


class TestR9DrainSweep(unittest.TestCase):
    def setUp(self):
        ml._command_inflight.clear()
        ml._reissue_inflight.clear()
        ml._last_deferred_drain_mono = None
        self._prev_executor = ml._reissue_executor
        ml._reissue_executor = None  # sync-fallback: job бежит в главном потоке
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.series_dir = self.root / "anzhee-direktorat"
        self.series_dir.mkdir(parents=True)

    def tearDown(self):
        ml._command_inflight.clear()
        ml._reissue_inflight.clear()
        ml._reissue_executor = self._prev_executor
        ml._last_deferred_drain_mono = None
        self._tmp.cleanup()

    def _enqueue_one(self):
        ruq.enqueue(self.series_dir, orig_ts=1_000.0, chat_id=1, msg_id=1,
                    reply_text="по юаням всё разрулил", snapshot=["Юани под 25-й контейнер"])

    def test_sweep_calls_drain_when_flag_on_and_queue_nonempty(self):
        """Флаг ON + непустая очередь → sweep зовёт drain_deferred по этой серии."""
        self._enqueue_one()
        calls: list[Path] = []
        with mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(reply_understand, "is_enabled", return_value=True), \
             mock.patch.object(reply_understand, "drain_deferred",
                               side_effect=lambda sd, **k: calls.append(Path(sd))
                               or {"processed": 1, "failed": 0}):
            ml._sweep_drain_deferred(token="t")
        self.assertEqual(calls, [self.series_dir],
                         "sweep не позвал drain_deferred по серии с непустой очередью")

    def test_flag_off_does_not_drain(self):
        """Флаг OFF → дренаж не запускается (очередь на проде не наполняется)."""
        self._enqueue_one()
        called = {"v": False}
        with mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(reply_understand, "is_enabled", return_value=False), \
             mock.patch.object(reply_understand, "drain_deferred",
                               side_effect=lambda *a, **k: called.__setitem__("v", True)):
            ml._sweep_drain_deferred(token="t")
        self.assertFalse(called["v"], "дренаж не должен зваться при выключенном флаге")

    def test_empty_queue_no_drain(self):
        """Очередь пуста → drain_deferred не зовётся (дёшево в обычном режиме)."""
        called = {"v": False}
        with mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(reply_understand, "is_enabled", return_value=True), \
             mock.patch.object(reply_understand, "drain_deferred",
                               side_effect=lambda *a, **k: called.__setitem__("v", True)):
            ml._sweep_drain_deferred(token="t")
        self.assertFalse(called["v"], "пустая очередь не должна триггерить дренаж")

    def test_throttle_skips_second_immediate_call(self):
        """Троттл: второй вызов сразу после первого не задваивает claude."""
        self._enqueue_one()
        calls: list[Path] = []
        with mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(reply_understand, "is_enabled", return_value=True), \
             mock.patch.object(reply_understand, "drain_deferred",
                               side_effect=lambda sd, **k: calls.append(Path(sd))
                               or {"processed": 0, "failed": 1}):
            ml._sweep_drain_deferred(token="t")
            ml._sweep_drain_deferred(token="t")  # сразу — троттл должен пропустить
        self.assertEqual(len(calls), 1, "троттл не сработал — дренаж запустился дважды подряд")


if __name__ == "__main__":
    unittest.main()
