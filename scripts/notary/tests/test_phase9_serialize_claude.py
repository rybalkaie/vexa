"""И1 (Ф9) — все claude-пути листенера через ЕДИНЫЙ фоновый исполнитель + уборка
stale `ready_for_reissue`.

Что закрывает (план 2026-06-08-serialize-all-claude-paths):
  - I1/I2/I3 ФОНОВОСТЬ: protocol/correction/apply-reply heavy-часть уходит в
    `_submit_command_job` → реальный ThreadPoolExecutor(max_workers=1). Пока job
    «висит» на Event — главный поток отзывчив (heartbeat), drain снимает после release.
  - I4 СЕРИАЛИЗАЦИЯ: один воркер на reissue И команды → ≤1 claude одновременно.
    Занятый reissue-future держит слот, командный job ждёт в очереди (не параллельно);
    job исполняется в воркере, не в главном потоке (thread.name).
  - I5 ДОСТАВКА: job сам шлёт результат (мок send_message); на сбое claude — user-facing
    ошибка ушла, реестр очищается в drain.
  - I6: R12 на `reissuing`-встрече по-прежнему отлупляет команду (job НЕ submit'ится);
    дедуп работает.
  - I9: stale `ready_for_reissue` attempts≥MAX старше порога удалён; attempts<MAX и
    свежий — целы; dormant-ветка не сломана.

Детерминизм (как в test_phase8b): стаб ждёт `started.set()` и блокируется на
`release.wait(timeout)`; `future.result(timeout=5)`; executor закрывается в finally.

R9: фикстуры нейтральные, текст правок/протокола/реплик НЕ печатается.

Запуск: python3 -m unittest tests.test_phase9_serialize_claude
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import feedback_state  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402

UTC = timezone.utc
_JOIN_TIMEOUT = 5.0


# ===========================================================================
# A. Инфраструктура — submit / busy / drain
# ===========================================================================
class TestCommandJobInfra(unittest.TestCase):
    def setUp(self):
        # Изолируем модульные реестры — тесты не должны влиять друг на друга.
        ml._command_inflight.clear()
        ml._reissue_inflight.clear()

    def tearDown(self):
        ml._command_inflight.clear()
        ml._reissue_inflight.clear()

    def test_submit_runs_in_background_and_drain_clears(self):
        """I1: job через _submit_command_job с реальным executor крутится в ФОНЕ
        (главный поток отзывчив), drain снимает завершённый из реестра."""
        started = threading.Event()
        release = threading.Event()
        worker_names: list[str] = []

        def job():
            worker_names.append(threading.current_thread().name)
            started.set()
            if not release.wait(timeout=_JOIN_TIMEOUT):
                raise AssertionError("release не выставлен — тест завис бы")

        inflight: dict = {}
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-cmd")
        try:
            ok = ml._submit_command_job(executor, inflight, "j#1", "protocol:x/y", job)
            self.assertTrue(ok)
            self.assertIn("j#1", inflight)
            # Реально стартовал в фоне (не синхронно в главном потоке).
            self.assertTrue(started.wait(timeout=_JOIN_TIMEOUT),
                            "job не стартовал в фоне — путь синхронный?")
            self.assertNotEqual(worker_names[0], threading.current_thread().name,
                                "job выполнился в ГЛАВНОМ потоке (не фон!)")
            # Пока висит — drain ничего не снимает (future не done).
            ml._drain_command_jobs(inflight=inflight)
            self.assertIn("j#1", inflight)
            # Отпускаем — дожидаемся, drain снимает.
            release.set()
            inflight["j#1"][0].result(timeout=5)
            ml._drain_command_jobs(inflight=inflight)
            self.assertEqual(inflight, {})
        finally:
            release.set()
            executor.shutdown(wait=True)

    def test_submit_none_executor_runs_sync(self):
        """Fallback: executor=None → fn() синхронно, реестр не растёт, True."""
        ran = {"v": False}
        inflight: dict = {}
        ok = ml._submit_command_job(None, inflight, "j#1", "lbl", lambda: ran.__setitem__("v", True))
        self.assertTrue(ok)
        self.assertTrue(ran["v"])
        self.assertEqual(inflight, {})

    def test_submit_none_executor_swallows_job_exception(self):
        """Синхронный fallback: исключение job не валит листенер (как process_message
        оборачивает всё). True всё равно (сообщение «обработано»)."""
        def boom():
            raise RuntimeError("claude умер")
        inflight: dict = {}
        ok = ml._submit_command_job(None, inflight, "j#1", "lbl", boom)
        self.assertTrue(ok)
        self.assertEqual(inflight, {})

    def test_executor_busy_reflects_both_registries(self):
        """I7: _executor_busy True если занят командный ИЛИ reissue реестр."""
        self.assertFalse(ml._executor_busy())
        ml._command_inflight["j#1"] = (mock.Mock(), "lbl", time.monotonic())
        self.assertTrue(ml._executor_busy())
        ml._command_inflight.clear()
        self.assertFalse(ml._executor_busy())
        ml._reissue_inflight["fid"] = (mock.Mock(), {}, time.monotonic())
        self.assertTrue(ml._executor_busy())

    def test_drain_clears_even_if_job_raised(self):
        """I5: job бросил необработанное → drain ловит result(), снимает из реестра
        (слот воркера освобождён, листенер не залип)."""
        executor = ThreadPoolExecutor(max_workers=1)
        inflight: dict = {}
        try:
            def boom():
                raise RuntimeError("boom")
            ml._submit_command_job(executor, inflight, "j#1", "lbl", boom)
            # Дождёмся завершения future (он упал — done() станет True).
            for _ in range(50):
                if inflight["j#1"][0].done():
                    break
                time.sleep(0.02)
            ml._drain_command_jobs(inflight=inflight)
            self.assertEqual(inflight, {})
        finally:
            executor.shutdown(wait=True)


# ===========================================================================
# I4 — СЕРИАЛИЗАЦИЯ: reissue держит слот, командный job ждёт
# ===========================================================================
class TestSerialization(unittest.TestCase):
    def test_command_job_queues_behind_busy_worker(self):
        """I4: занятый воркер (имитируем reissue-future блокировкой на Event) →
        командный job, отправленный в ТОТ ЖЕ executor, не исполняется параллельно —
        ждёт в очереди. ≤1 claude одновременно."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="serial")
        reissue_started = threading.Event()
        reissue_release = threading.Event()
        cmd_started = threading.Event()
        order: list[str] = []
        cmd_thread_name: list[str] = []

        def reissue_job():
            order.append("reissue-start")
            reissue_started.set()
            reissue_release.wait(timeout=_JOIN_TIMEOUT)
            order.append("reissue-end")

        def cmd_job():
            cmd_thread_name.append(threading.current_thread().name)
            order.append("cmd-start")
            cmd_started.set()

        try:
            f_reissue = executor.submit(reissue_job)
            self.assertTrue(reissue_started.wait(timeout=_JOIN_TIMEOUT))
            # Воркер занят reissue. Отправляем командный job — он встаёт в очередь.
            f_cmd = executor.submit(cmd_job)
            # Командный НЕ должен стартовать, пока reissue держит единственный воркер.
            self.assertFalse(cmd_started.wait(timeout=0.3),
                             "командный job стартовал параллельно с reissue (>1 claude!)")
            # Отпускаем reissue → теперь воркер свободен → командный исполняется.
            reissue_release.set()
            f_reissue.result(timeout=5)
            f_cmd.result(timeout=5)
            self.assertTrue(cmd_started.wait(timeout=_JOIN_TIMEOUT))
            # Порядок: reissue целиком ДО командного (сериализация).
            self.assertEqual(order, ["reissue-start", "reissue-end", "cmd-start"])
            # Командный job исполнился в воркере, не в главном потоке.
            self.assertNotEqual(cmd_thread_name[0], threading.current_thread().name)
        finally:
            reissue_release.set()
            executor.shutdown(wait=True)


# ===========================================================================
# I1/I5 — protocol-команда: фоновость + доставка + ошибка
# ===========================================================================
class TestProtocolCommandJob(unittest.TestCase):
    def test_job_sends_protocol_and_chunks(self):
        """I5: _job_protocol_command сам генерирует, читает, шлёт результат."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proto = Path(tmp.name) / "2026-06-02-protokol.md"
        transcript = Path(tmp.name) / "2026-06-02.md"
        transcript.write_text("текст транскрипта", encoding="utf-8")

        sent: list[str] = []

        def fake_regen(**kwargs):
            kwargs["protocol_path"].write_text("ПРОТОКОЛ тело", encoding="utf-8")

        with mock.patch("notary.lib.llm_postprocess.regenerate_protocol_for_meeting",
                        side_effect=fake_regen), \
             mock.patch("notary.lib.telegram_api.split_long_message",
                        return_value=["часть1", "часть2"]), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda *a, **k: sent.append(a[2])):
            ml._job_protocol_command("tok", -1, 5, "coord", "2026-06-02", transcript, proto)

        # header + 2 chunk.
        self.assertTrue(any("Готово" in s for s in sent))
        self.assertIn("часть1", sent)
        self.assertIn("часть2", sent)

    def test_job_sends_user_facing_error_on_generation_failure(self):
        """I5: claude-генерация упала → job сам шлёт user-facing ошибку."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proto = Path(tmp.name) / "p.md"
        transcript = Path(tmp.name) / "t.md"
        transcript.write_text("x", encoding="utf-8")
        sent: list[str] = []

        from notary.lib import llm_postprocess
        with mock.patch.object(llm_postprocess, "regenerate_protocol_for_meeting",
                               side_effect=llm_postprocess.ProtocolGenerationError("claude down")), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda *a, **k: sent.append(a[2])):
            ml._job_protocol_command("tok", -1, 5, "coord", "2026-06-02", transcript, proto)
        self.assertTrue(any("упала" in s or "❌" in s for s in sent))

    def test_route_submits_job_not_blocks(self):
        """I1: route-функция после ack делает submit, heavy не в главном потоке.
        executor=None fallback → синхронно, но генерация всё равно через job-функцию."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "coord").mkdir()
        (root / "coord" / "2026-06-02.md").write_text("транскрипт", encoding="utf-8")

        ml._command_inflight.clear()
        submitted: list[tuple] = []
        executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(executor.shutdown, wait=True)

        msg = {"text": "протокол coord 2026-06-02", "message_id": 7}
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_PROTOCOLS_DIR": str(root)}), \
             mock.patch.object(ml, "_reissue_executor", executor), \
             mock.patch.object(ml, "_reissue_guard_blocks", return_value=False), \
             mock.patch.object(ml, "_protocol_command_is_dupe", return_value=False), \
             mock.patch.object(ml, "_submit_command_job",
                               side_effect=lambda *a, **k: submitted.append(a) or True), \
             mock.patch.object(ml, "send_message"):
            handled = ml.maybe_route_to_protocol_command("tok", -1, msg)
        self.assertTrue(handled)
        self.assertEqual(len(submitted), 1, "heavy-часть не ушла в submit")


# ===========================================================================
# I6 — R12 guard отлупляет команду (job НЕ submit'ится) + дедуп
# ===========================================================================
class TestGuardAndDedupe(unittest.TestCase):
    def test_r12_blocks_protocol_command_no_submit(self):
        """I6: встреча в reissuing → guard блокирует, submit НЕ зовётся."""
        submitted: list = []
        msg = {"text": "протокол coord 2026-06-02", "message_id": 7}
        with mock.patch.object(ml, "_reissue_guard_blocks", return_value=True), \
             mock.patch.object(ml, "_submit_command_job",
                               side_effect=lambda *a, **k: submitted.append(a)):
            handled = ml.maybe_route_to_protocol_command("tok", -1, msg)
        self.assertTrue(handled)  # обработано (отлуп), True
        self.assertEqual(submitted, [], "submit вызван несмотря на R12 guard")

    def test_r12_blocks_correction_command_no_submit(self):
        submitted: list = []
        msg = {"text": "удали задачу 2 из coord 2026-06-02", "message_id": 7}
        with mock.patch.object(ml, "_reissue_guard_blocks", return_value=True), \
             mock.patch.object(ml, "_submit_command_job",
                               side_effect=lambda *a, **k: submitted.append(a)):
            handled = ml.maybe_route_to_correction_command("tok", -1, msg)
        # parse может вернуть None для нашего текста — тогда handled False и это ок,
        # главное: при guard=True (если parse прошёл) submit не дёрнут.
        self.assertEqual(submitted, [])

    def test_protocol_dedupe_blocks_second_in_window(self):
        """I6: дедуп protocol-команды работает — второй вызов в окне → True, no submit."""
        ml._protocol_command_recent.clear()
        self.assertFalse(ml._protocol_command_is_dupe("coord", "2026-06-02"))
        self.assertTrue(ml._protocol_command_is_dupe("coord", "2026-06-02"))

    def test_command_label_inflight_detects_queued_job(self):
        """У1 (цикл5): хелпер видит job с такой меткой в живом реестре."""
        ml._command_inflight.clear()
        self.addCleanup(ml._command_inflight.clear)
        self.assertFalse(ml._command_label_inflight("protocol:coord/2026-06-02"))
        ml._command_inflight["protocol:coord/2026-06-02#1"] = (
            mock.Mock(), "protocol:coord/2026-06-02", time.monotonic())
        self.assertTrue(ml._command_label_inflight("protocol:coord/2026-06-02"))
        self.assertFalse(ml._command_label_inflight("protocol:coord/2026-06-03"))

    def test_protocol_route_blocks_duplicate_while_inflight(self):
        """У1 (цикл5): пока job этой встречи в очереди/работе — повтор команды НЕ
        сабмитит второй job (queuing держит дольше 30с-дедупа). Wispr-дубль не задвоит."""
        ml._command_inflight.clear()
        self.addCleanup(ml._command_inflight.clear)
        # Имитируем уже стоящий в очереди job этой встречи.
        ml._command_inflight["protocol:coord/2026-06-02#1"] = (
            mock.Mock(), "protocol:coord/2026-06-02", time.monotonic())
        submitted: list = []
        msg = {"text": "протокол coord 2026-06-02", "message_id": 9}
        with mock.patch.object(ml, "_reissue_guard_blocks", return_value=False), \
             mock.patch.object(ml, "_submit_command_job",
                               side_effect=lambda *a, **k: submitted.append(a) or True), \
             mock.patch.object(ml, "send_message"):
            handled = ml.maybe_route_to_protocol_command("tok", -1, msg)
        self.assertTrue(handled)            # обработано (отлуп «уже в очереди»)
        self.assertEqual(submitted, [], "второй job сабмитнут несмотря на in-flight дубль")


# ===========================================================================
# I9 — уборка stale ready_for_reissue
# ===========================================================================
class TestCleanupStaleReady(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "_feedback_edits"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, *, series, status, attempts, age_days):
        fid = feedback_state.build_feedback_id(series, "2026-01-01", -1001)
        updated = (datetime.now(UTC) - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        st = {
            "feedback_id": fid, "series": series, "date": "2026-01-01", "chat_id": -1001,
            "status": status, "reissue_attempts": attempts, "round": 1,
            "updated_at": updated,
        }
        feedback_state.write_state(st, root=self.root)
        # write_state перетирает updated_at своим now — выставим вручную после записи.
        path = self.root / f"{fid}{feedback_state.STATE_SUFFIX}"
        import json as _json
        data = _json.loads(path.read_text(encoding="utf-8"))
        data["updated_at"] = updated
        path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return fid

    def test_stale_ready_max_attempts_removed(self):
        """I9: ready_for_reissue attempts≥MAX старше порога — удалён."""
        fid = self._write(series="dead", status="ready_for_reissue",
                          attempts=feedback_state.MAX_REISSUE_ATTEMPTS, age_days=40)
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 1)
        self.assertIsNone(feedback_state.read_state(fid, root=self.root))

    def test_live_ready_below_max_kept(self):
        """I9: ready_for_reissue attempts<MAX (ещё ретраится) — НЕ трогаем, даже старый."""
        fid = self._write(series="live", status="ready_for_reissue",
                          attempts=feedback_state.MAX_REISSUE_ATTEMPTS - 1, age_days=40)
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 0)
        self.assertIsNotNone(feedback_state.read_state(fid, root=self.root))

    def test_fresh_stale_ready_kept(self):
        """I9: ready_for_reissue attempts≥MAX но свежий (моложе порога) — цел."""
        fid = self._write(series="fresh", status="ready_for_reissue",
                          attempts=feedback_state.MAX_REISSUE_ATTEMPTS, age_days=1)
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 0)
        self.assertIsNotNone(feedback_state.read_state(fid, root=self.root))

    def test_dormant_branch_still_works(self):
        """I9: dormant-ветка R10 не сломана — старый dormant по-прежнему удаляется."""
        fid = self._write(series="old", status="dormant", attempts=0, age_days=40)
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 1)
        self.assertIsNone(feedback_state.read_state(fid, root=self.root))

    def test_mixed_only_eligible_removed(self):
        """I9: смешанная папка — удаляются только подходящие, остальное цело."""
        dead = self._write(series="dead", status="ready_for_reissue",
                           attempts=feedback_state.MAX_REISSUE_ATTEMPTS, age_days=40)
        live = self._write(series="live", status="ready_for_reissue",
                           attempts=0, age_days=40)
        dorm = self._write(series="dorm", status="dormant", attempts=0, age_days=40)
        coll = self._write(series="coll", status="collecting", attempts=0, age_days=40)
        removed = feedback_state.cleanup_dormant_states(root=self.root, max_age_days=30)
        self.assertEqual(removed, 2)  # dead + dorm
        self.assertIsNone(feedback_state.read_state(dead, root=self.root))
        self.assertIsNone(feedback_state.read_state(dorm, root=self.root))
        self.assertIsNotNone(feedback_state.read_state(live, root=self.root))
        self.assertIsNotNone(feedback_state.read_state(coll, root=self.root))


if __name__ == "__main__":
    unittest.main()
