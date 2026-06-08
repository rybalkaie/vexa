"""Ф8b — listener-уровневые интеграционные тесты ФОНОВОСТИ перевыпуска (КОД1).

Что закрывает эта фаза (план 2026-06-08-async-protocol-reissue, Фаза 3):
unit'ы claim/finalize/reissue/cleanup живут в `tests.test_phase8_async_reissue`
(Ф1) и `tests.test_phase4_feedback_reissue` (Ф4) — здесь их НЕ дублируем.
Эта фаза гоняет то, что видно только на уровне процесса/потока:

  - **КОД1 / R1 (ядро):** ИМЕННО тредовый путь `_process_reissues_async`
    (drain → claim → executor.submit(reissue_one) → drain/finalize) с реальным
    `ThreadPoolExecutor(max_workers=1)` и `reissue_fn`-стабом, блокирующимся на
    `threading.Event`. Пока стаб «генерирует» — главный поток отзывчив (успевает
    sweep-подобную операцию + heartbeat). Это НЕ синхронная обёртка
    `process_ready_reissues` (она могла бы тихо разойтись с реальным путём).
  - **R6:** «один за раз» (cap=1) — вторая готовая встреча ждёт завершения первой.
  - **R4 / РИСК1:** reclaim оборванного future vs. защита живого через `skip_fids`.
  - **R12:** guard ручных команд на `reissuing`-встрече.
  - **R11:** уведомление о старте при submit (один раз на раунд, best-effort).
  - **R10:** вызов уборки dormant через listener-троттл `_maybe_cleanup_dormant_states`.

Детерминизм фоновости (важно — легко написать flaky-тест):
  - стаб ждёт `started.set()` (тест знает, что future реально стартовал в воркере)
    и блокируется на `release.wait(timeout=…)` — НИКАКИХ голых `sleep` для
    синхронизации;
  - `future.result(timeout=5)` везде — упавший тест не виснет вечно;
  - executor всегда закрывается в `finally` (`shutdown(wait=True)`).

R9 (приватность): фикстуры нейтральные, текст правок/протокола не печатается.

Запуск: python3 -m unittest tests.test_phase8b_async_listener (system python3.9).
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

from notary.lib import feedback_state, feedback_reissue  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402

UTC = timezone.utc

# Запас времени для синхронизации потоков на CI/слабой машине. Тесты НЕ полагаются
# на эти величины для корректности (используют Event'ы), только как предохранитель
# от вечного зависания.
_JOIN_TIMEOUT = 5.0


class _ListenerReissueBase(unittest.TestCase):
    """Изолированный feedback-dir + хелпер на создание ready/reissuing state'ов.

    Переиспользует подход `_ReissueBase`/`_Base` из существующих тестов (tempfile +
    `MEETING_NOTARY_FEEDBACK_DIR`), чтобы не писать в реальный `_feedback_edits/`.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "_feedback_edits"
        self.root.mkdir(parents=True, exist_ok=True)
        # Env-изоляция нужна для путей, которые резолвят root сами (guard читает
        # read_state БЕЗ root=, listener-cleanup зовёт cleanup_dormant_states()).
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_FEEDBACK_DIR": str(self.root),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _ready(self, *, series="coord", date="2026-06-02", chat_id=-1001,
               status="ready_for_reissue", rnd=1, attempts=0, mids=(101, 102)):
        fid = feedback_state.build_feedback_id(series, date, chat_id)
        st = {
            "feedback_id": fid, "series": series, "date": date, "chat_id": chat_id,
            "meta_path": "/x/meta.json", "protocol_message_ids": list(mids),
            "round": rnd, "status": status, "reissue_attempts": attempts,
            "edits": [{"author": "M", "text": "правка", "tg_message_id": 1}],
        }
        feedback_state.write_state(st, root=self.root)
        return fid


# ===========================================================================
# R1 / КОД1 — ядро: тредовый путь не морозит листенер
# ===========================================================================
class TestAsyncBackgroundResponsiveness(_ListenerReissueBase):
    def test_listener_responsive_while_reissue_runs_in_background(self):
        """КОД1: гоняем ИМЕННО `_process_reissues_async` с реальным
        ThreadPoolExecutor(max_workers=1). reissue_fn-стаб блокируется на Event,
        имитируя долгую генерацию. Доказываем:
          (1) submit реально ушёл в фоновый поток (стаб сигналит started),
          (2) пока он «работает» — главный поток успевает heartbeat + ещё один
              проход _process_reissues_async (drain-ничего, claim-нет ёмкости),
          (3) после release+drain — finalize отработал, in-flight пуст, dormant.
        """
        fid = self._ready()

        started = threading.Event()   # стаб реально стартовал в воркере
        release = threading.Event()   # тест разрешает стабу завершиться
        worker_thread_names: list[str] = []

        def slow_reissue(state, *, root=None):
            worker_thread_names.append(threading.current_thread().name)
            started.set()
            # Блокируемся как «долгая генерация». timeout — предохранитель.
            if not release.wait(timeout=_JOIN_TIMEOUT):
                raise AssertionError("release не выставлен — тест завис бы")
            return {"status": "sent", "message_ids": [9001, 9002]}

        # heartbeat в temp-файл (HEARTBEAT_FILE — модульная константа из STATE_DIR).
        hb_file = Path(self._tmp.name) / "listener-alive"
        inflight: dict = {}
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-reissue")
        try:
            with mock.patch.object(ml, "HEARTBEAT_FILE", hb_file):
                # --- проход 1: claim + submit (тяжёлое уходит в фон) ---
                ml._process_reissues_async(
                    executor=executor, inflight=inflight,
                    root=self.root, cap=1, reissue_fn=slow_reissue,
                )
                # submit состоялся, future зарегистрирован.
                self.assertIn(fid, inflight)
                # Дожидаемся, что стаб РЕАЛЬНО стартовал в фоновом потоке (а не
                # синхронно в главном) — иначе тест ложно-зелёный.
                self.assertTrue(started.wait(timeout=_JOIN_TIMEOUT),
                                "reissue_fn не стартовал в фоне — путь синхронный?")
                self.assertNotEqual(worker_thread_names[0],
                                    threading.current_thread().name,
                                    "reissue_fn выполнился в ГЛАВНОМ потоке (не фон!)")

                # --- пока генерация «висит»: главный поток ОТЗЫВЧИВ ---
                future_obj = inflight[fid][0]
                self.assertFalse(future_obj.done())  # future ещё крутится в воркере
                # (а) heartbeat обновляется без блокировки.
                ml.heartbeat()
                self.assertTrue(hb_file.exists())
                # (б) ещё один sweep-подобный проход проходит мгновенно (drain ничего
                #     не финализирует — future жив; claim не берёт — нет ёмкости).
                t0 = time.monotonic()
                ml._process_reissues_async(
                    executor=executor, inflight=inflight,
                    root=self.root, cap=1, reissue_fn=slow_reissue,
                )
                self.assertLess(time.monotonic() - t0, _JOIN_TIMEOUT,
                                "повторный проход заблокировался — листенер заморожен")
                # state по-прежнему в работе, ничего не пере-заклеймили.
                self.assertEqual(len(inflight), 1)
                self.assertEqual(
                    feedback_state.read_state(fid, root=self.root)["status"],
                    "reissuing",
                )
                # heartbeat можно дёрнуть ещё раз — листенер жив, не заблокирован.
                ml.heartbeat()
                self.assertTrue(hb_file.exists())

                # --- отпускаем генерацию, дожидаемся future, drain → finalize ---
                release.set()
                # Детерминированно ждём завершения future (предохранитель timeout).
                self.assertEqual(future_obj.result(timeout=5)["status"], "sent")
                ml._process_reissues_async(
                    executor=executor, inflight=inflight,
                    root=self.root, cap=1, reissue_fn=slow_reissue,
                )
                # finalize отработал: in-flight пуст, state dormant, mids обновлены.
                self.assertEqual(inflight, {})
                final = feedback_state.read_state(fid, root=self.root)
                self.assertEqual(final["status"], "dormant")
                self.assertEqual(final["protocol_message_ids"], [9001, 9002])
        finally:
            release.set()  # на случай падения до release — не виснуть на shutdown
            executor.shutdown(wait=True)

    def test_exception_in_background_reverts_to_ready(self):
        """Исключение из reissue_one в фоне → drain ловит, finalize делает
        error-revert (ready_for_reissue, attempts++). Листенер не падает."""
        fid = self._ready(attempts=0)

        def boom(state, *, root=None):
            raise RuntimeError("claude процесс умер")

        inflight: dict = {}
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=boom,
            )
            self.assertIn(fid, inflight)
            # дождаться, что future завершился (с исключением) — детерминированно.
            fut = inflight[fid][0]
            with self.assertRaises(RuntimeError):
                fut.result(timeout=5)
            # drain: исключение → finalize({"status":"error"}) → revert.
            # cap=0 на этом проходе → изолируем drain/finalize, чтобы тот же проход
            # НЕ пере-заклеймил уже-revert'нутый ready (это легитимный ретрай, но он
            # зашумил бы проверку «in-flight снят и revert состоялся»).
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=0, reissue_fn=boom,
            )
        finally:
            executor.shutdown(wait=True)
        self.assertEqual(inflight, {})  # future снят из реестра после drain
        final = feedback_state.read_state(fid, root=self.root)
        self.assertEqual(final["status"], "ready_for_reissue")  # error-revert для ретрая
        self.assertEqual(final["reissue_attempts"], 1)


# ===========================================================================
# R6 — один за раз (cap=1): вторая встреча ждёт завершения первой
# ===========================================================================
class TestOneAtATime(_ListenerReissueBase):
    def test_second_meeting_waits_for_first_future(self):
        fid_a = self._ready(series="alpha")
        fid_b = self._ready(series="bravo")

        release = threading.Event()
        started = threading.Event()
        submitted_fids: list[str] = []

        def slow_reissue(state, *, root=None):
            submitted_fids.append(state["feedback_id"])
            started.set()
            if not release.wait(timeout=_JOIN_TIMEOUT):
                raise AssertionError("release не выставлен")
            return {"status": "sent", "message_ids": [9001]}

        inflight: dict = {}
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            # проход 1: cap=1 → клеймим РОВНО одну, вторая остаётся ready.
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=slow_reissue,
            )
            self.assertEqual(len(inflight), 1)
            self.assertTrue(started.wait(timeout=_JOIN_TIMEOUT))
            first_fid = next(iter(inflight))
            # вторая встреча НЕ заклеймлена (нет свободной ёмкости).
            statuses = {
                f: feedback_state.read_state(f, root=self.root)["status"]
                for f in (fid_a, fid_b)
            }
            self.assertEqual(statuses[first_fid], "reissuing")
            other_fid = fid_b if first_fid == fid_a else fid_a
            self.assertEqual(statuses[other_fid], "ready_for_reissue")

            # проход 2 пока первый жив: claimed state НЕ реклеймится, вторая НЕ
            # подхватывается (ёмкости нет). in-flight тот же один.
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=slow_reissue,
            )
            self.assertEqual(list(inflight), [first_fid])
            self.assertEqual(submitted_fids, [first_fid])  # второй submit не было

            # отпускаем первый, ждём, drain → finalize первого, освобождает ёмкость.
            release.set()
            inflight[first_fid][0].result(timeout=5)
            started.clear()
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=slow_reissue,
            )
            # теперь в работе ВТОРАЯ встреча, первая — dormant.
            self.assertTrue(started.wait(timeout=_JOIN_TIMEOUT))
            self.assertEqual(list(inflight), [other_fid])
            self.assertEqual(submitted_fids, [first_fid, other_fid])
            self.assertEqual(
                feedback_state.read_state(first_fid, root=self.root)["status"],
                "dormant",
            )
            # дочистим второй future, чтобы shutdown не ждал.
            release.set()
            inflight[other_fid][0].result(timeout=5)
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=slow_reissue,
            )
            self.assertEqual(inflight, {})
        finally:
            release.set()
            executor.shutdown(wait=True)


# ===========================================================================
# R4 / РИСК1 — reclaim оборванного future vs. защита живого через skip_fids
# ===========================================================================
class TestReclaimAfterLostFuture(_ListenerReissueBase):
    def test_lost_future_reclaimed_when_registry_empty(self):
        """R4: рестарт листенера → реестр in-flight пуст, но на диске остался
        `reissuing` старше порога. Следующий claim-проход (skip_fids пуст, т.к.
        реестр пуст) реклеймит его в ready_for_reissue, attempts++."""
        fid = self._ready(status="reissuing", attempts=0)
        cur = feedback_state.read_state(fid, root=self.root)
        # claim давно — старше RECLAIM_STALE_REISSUING_SEC (900с).
        old = (datetime.now(UTC) - timedelta(seconds=2000)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur["reissue_claimed_at"] = old
        feedback_state.write_state(cur, root=self.root)

        # имитация рестарта: реестр in-flight ПУСТ (future «потерян»).
        inflight: dict = {}
        # стаб не должен вызываться повторно для проверки самого reclaim — но если
        # claim пере-заклеймит, он уйдёт в submit. Считаем submit'ы.
        submits: list = []

        def noop_reissue(state, *, root=None):
            submits.append(state["feedback_id"])
            return {"status": "sent", "message_ids": [1]}

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=noop_reissue,
            )
            # reclaim вернул в ready и тут же claim заклеймил заново (attempts уже 1).
            self.assertEqual(submits, [fid])
            if fid in inflight:
                inflight[fid][0].result(timeout=5)
        finally:
            executor.shutdown(wait=True)
        # после реклейма attempts инкрементнут (его видно в claimed-снимке → finalize).
        # claim_ready_reissues видит attempts=1 после reclaim, клеймит (1 < MAX=3).
        # Проверяем именно факт реклейма: до reclaim был reissuing, стал обработан.
        self.assertEqual(len(submits), 1)


class TestRisk1LiveFutureNotReclaimed(_ListenerReissueBase):
    def test_live_future_protected_by_skip_fids_but_reclaimed_without(self):
        """РИСК1 (фиксируем РАЗНИЦУ двумя ассертами):
          - с skip_fids (future жив, в inflight) → reclaim НЕ трогает, статус
            остаётся reissuing, двойной доставки нет;
          - тот же state БЕЗ skip_fids → реклеймнулся бы в ready_for_reissue.
        Это ровно тот инвариант, что делает фоновость безопасной (claude
        недетерминирован → пере-claim = двойная доставка уже отправленного)."""
        fid = self._ready(status="reissuing", attempts=0)
        cur = feedback_state.read_state(fid, root=self.root)
        cur["reissue_claimed_at"] = "2026-06-02T10:00:00Z"  # давно (>900с до now ниже)
        feedback_state.write_state(cur, root=self.root)
        now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)  # +2ч → claim старый

        # (A) future ЖИВ → его fid в skip_fids → reclaim его НЕ трогает.
        n_protected = feedback_reissue.reclaim_stale_reissuing(
            root=self.root, now=now, skip_fids={fid},
        )
        self.assertEqual(n_protected, 0)
        self.assertEqual(
            feedback_state.read_state(fid, root=self.root)["status"],
            "reissuing",
            "живой future реклеймнулся — РИСК1 не закрыт, грозит двойная доставка",
        )

        # (B) тот же кейс БЕЗ skip_fids → реклеймнулся бы (контраст-доказательство).
        n_unprotected = feedback_reissue.reclaim_stale_reissuing(
            root=self.root, now=now,  # skip_fids пуст
        )
        self.assertEqual(n_unprotected, 1)
        self.assertEqual(
            feedback_state.read_state(fid, root=self.root)["status"],
            "ready_for_reissue",
        )

    def test_async_path_passes_skip_fids_for_live_inflight(self):
        """Тот же инвариант, но через РЕАЛЬНЫЙ async-путь: пока future жив в
        inflight, повторный _process_reissues_async НЕ должен реклеймить его state,
        даже если claim искусственно «состарен» (>900с). Доказывает, что
        listener-слой пробрасывает skip_fids в reclaim (а не только unit reclaim)."""
        fid = self._ready()
        release = threading.Event()
        started = threading.Event()

        def slow_reissue(state, *, root=None):
            started.set()
            if not release.wait(timeout=_JOIN_TIMEOUT):
                raise AssertionError("release не выставлен")
            return {"status": "sent", "message_ids": [9001]}

        inflight: dict = {}
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=slow_reissue,
            )
            self.assertTrue(started.wait(timeout=_JOIN_TIMEOUT))
            self.assertIn(fid, inflight)
            # искусственно состариваем claim, как будто генерация идёт >900с.
            cur = feedback_state.read_state(fid, root=self.root)
            cur["reissue_claimed_at"] = (
                datetime.now(UTC) - timedelta(seconds=2000)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            feedback_state.write_state(cur, root=self.root)

            # повторный проход: future ещё жив → skip_fids={fid} → НЕ реклеймим.
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=slow_reissue,
            )
            self.assertEqual(
                feedback_state.read_state(fid, root=self.root)["status"],
                "reissuing",
                "async-путь не пробросил skip_fids в reclaim — живая генерация сброшена",
            )
            self.assertIn(fid, inflight)  # всё ещё в работе

            release.set()
            inflight[fid][0].result(timeout=5)
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=slow_reissue,
            )
            self.assertEqual(
                feedback_state.read_state(fid, root=self.root)["status"], "dormant",
            )
        finally:
            release.set()
            executor.shutdown(wait=True)


# ===========================================================================
# Паритет с sync-обёрткой: упавшая встреча НЕ пере-заклеймливается в ТОМ ЖЕ
# проходе (drained_fids guard в claim). Sync `process_ready_reissues` ведёт
# `seen_fids` → «одна попытка на встречу за sweep»; async drain делает
# finalize(error→ready)+pop ДО claim в том же вызове, поэтому без guard'а только
# что упавшая встреча пере-заклеймилась бы back-to-back, сжигая попытки и держа
# слот cap=1 в обход других ready_for_reissue.
# ===========================================================================
class TestDrainedFidsNotReclaimedSameSweep(_ListenerReissueBase):
    def test_failed_meeting_yields_slot_to_other_ready_same_sweep(self):
        """Находка #2: встреча A падает (error→ready_for_reissue) и в ТОМ ЖЕ
        проходе, где её future дренится, claim НЕ должен пере-заклеймить A —
        свободный слот (cap=1) уходит другой ready-встрече B, A ждёт следующего
        sweep. Паритет с sync-обёрткой process_ready_reissues."""
        fid_a = self._ready(series="alpha", attempts=0)

        release_b = threading.Event()
        started_b = threading.Event()
        submitted: list[str] = []

        def reissue_fn(state, *, root=None):
            submitted.append(state["feedback_id"])
            if state["feedback_id"] == fid_a:
                raise RuntimeError("claude процесс умер")  # A → error-revert
            # B → блокируется как «долгая генерация» (предохранитель по timeout).
            started_b.set()
            if not release_b.wait(timeout=_JOIN_TIMEOUT):
                raise AssertionError("release_b не выставлен")
            return {"status": "sent", "message_ids": [9001]}

        inflight: dict = {}
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            # Проход 1: только A в ready → claim A, submit, future падает с error.
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=reissue_fn,
            )
            self.assertEqual(submitted, [fid_a])
            self.assertIn(fid_a, inflight)
            with self.assertRaises(RuntimeError):
                inflight[fid_a][0].result(timeout=5)  # детерминированно: future done

            # Теперь добавляем B (ready). Проход 2 в ОДНОМ вызове: drain(A)
            # error→ready_for_reissue + pop, затем claim со свободным слотом.
            fid_b = self._ready(series="bravo", attempts=0)
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=reissue_fn,
            )

            # Слот достался B, а НЕ повторному A (drained_fids пропустил A).
            self.assertTrue(started_b.wait(timeout=_JOIN_TIMEOUT))
            self.assertEqual(submitted, [fid_a, fid_b], "A пере-заклеймлена в том же sweep")
            self.assertEqual(list(inflight), [fid_b])
            self.assertEqual(
                feedback_state.read_state(fid_b, root=self.root)["status"], "reissuing",
            )
            # A после error-revert ждёт следующего sweep: ready, attempts инкрементнут.
            cur_a = feedback_state.read_state(fid_a, root=self.root)
            self.assertEqual(cur_a["status"], "ready_for_reissue")
            self.assertEqual(cur_a["reissue_attempts"], 1)

            # Следующий sweep (после освобождения слота B) подхватывает A.
            release_b.set()
            inflight[fid_b][0].result(timeout=5)
            ml._process_reissues_async(
                executor=executor, inflight=inflight,
                root=self.root, cap=1, reissue_fn=reissue_fn,
            )
            self.assertIn(fid_a, inflight)  # A заклеймлена на след. sweep
            # дождаться завершения future A (он снова падает) — submitted растёт лишь
            # ПОСЛЕ реального старта воркера, поэтому сверяем submitted ПОСЛЕ result.
            with self.assertRaises(RuntimeError):
                inflight[fid_a][0].result(timeout=5)
            self.assertEqual(submitted, [fid_a, fid_b, fid_a], "A не подхвачена на след. sweep")
        finally:
            release_b.set()
            executor.shutdown(wait=True)


# ===========================================================================
# R12 — guard ручных команд на reissuing-встрече
# ===========================================================================
class TestReissueGuard(_ListenerReissueBase):
    def test_guard_blocks_on_reissuing_state(self):
        """R12: `_reissue_guard_blocks` на встрече со статусом reissuing → True,
        send_message позван с «⏳ …пересобирается…». Read-only — статус не меняется."""
        series, date_str, chat_id = "coord", "2026-06-02", -1001
        self._ready(series=series, date=date_str, chat_id=chat_id, status="reissuing")
        sent: list = []

        def fake_send(token, cid, text, **kw):
            sent.append({"chat_id": cid, "text": text})

        with mock.patch.object(ml, "send_message", fake_send):
            blocked = ml._reissue_guard_blocks(
                "tok", chat_id, series, date_str, {"message_id": 555},
            )
        self.assertTrue(blocked)
        self.assertEqual(len(sent), 1)
        self.assertIn("пересобирается", sent[0]["text"])
        self.assertEqual(sent[0]["chat_id"], chat_id)
        # guard read-only: статус не тронут.
        fid = feedback_state.build_feedback_id(series, date_str, chat_id)
        self.assertEqual(
            feedback_state.read_state(fid, root=self.root)["status"], "reissuing",
        )

    def test_guard_passes_on_non_reissuing_state(self):
        """R12: на не-reissuing встрече (dormant) guard → False, ничего не шлёт
        (команда пойдёт дальше как раньше)."""
        series, date_str, chat_id = "coord", "2026-06-03", -1001
        self._ready(series=series, date=date_str, chat_id=chat_id, status="dormant")
        sent: list = []
        with mock.patch.object(ml, "send_message",
                               lambda *a, **k: sent.append(1)):
            blocked = ml._reissue_guard_blocks(
                "tok", chat_id, series, date_str, {"message_id": 1},
            )
        self.assertFalse(blocked)
        self.assertEqual(sent, [])

    def test_guard_passes_when_no_state(self):
        """R12: state-файла нет вовсе → guard False (best-effort, не блокируем)."""
        with mock.patch.object(ml, "send_message", lambda *a, **k: None):
            blocked = ml._reissue_guard_blocks(
                "tok", -1001, "nope", "2026-06-09", {"message_id": 1},
            )
        self.assertFalse(blocked)

    def test_protocol_command_blocked_does_not_run_generation(self):
        """R12 интеграция через РЕАЛЬНЫЙ командный путь: команда «протокол …» на
        reissuing-встречу → caller вернул True (обработано), генерация НЕ
        запускалась (нет ack «✏️ Генерирую», нет subprocess), участник получил
        отлуп. Доказывает, что guard стоит ДО генерации в реальном пути."""
        series, date_str, chat_id = "sales", "2026-06-02", -1001
        self._ready(series=series, date=date_str, chat_id=chat_id, status="reissuing")
        sent: list = []

        def fake_send(token, cid, text, **kw):
            sent.append(text)

        # subprocess.run НЕ должен быть позван (генерации нет). Если позовётся —
        # это баг guard'а; падаем явно.
        def forbid_subprocess(*a, **k):
            raise AssertionError("subprocess.run позван — guard пропустил генерацию!")

        with mock.patch.object(ml, "send_message", fake_send), \
             mock.patch.object(ml.subprocess, "run", forbid_subprocess):
            handled = ml.maybe_route_to_protocol_command(
                "tok", chat_id, {"message_id": 7, "text": f"протокол {series} {date_str}"},
            )
        self.assertTrue(handled)  # команда «обработана» (отложена)
        self.assertTrue(any("пересобирается" in t for t in sent))
        # никакого ack «Генерирую» — единственный ответ это отлуп guard'а.
        self.assertFalse(any("Генерирую" in t for t in sent))


# ===========================================================================
# R11 — уведомление о старте при submit (один раз на раунд, best-effort)
# ===========================================================================
class TestReissueStartNotice(_ListenerReissueBase):
    def test_notice_sent_once_on_submit(self):
        """R11: submit через _process_reissues_async с token → «🔧 …пересобираю…»
        уходит один раз; флаг reissue_notice_sent_round выставлен."""
        fid = self._ready(rnd=1, mids=(101, 102))
        sent: list = []

        def fake_send(token, cid, text, *, reply_to=None, **kw):
            sent.append({"chat_id": cid, "text": text, "reply_to": reply_to})

        release = threading.Event()

        def quick_reissue(state, *, root=None):
            if not release.wait(timeout=_JOIN_TIMEOUT):
                raise AssertionError("release не выставлен")
            return {"status": "sent", "message_ids": [9001]}

        inflight: dict = {}
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with mock.patch.object(ml, "send_message", fake_send):
                ml._process_reissues_async(
                    executor=executor, inflight=inflight, token="tok",
                    root=self.root, cap=1, reissue_fn=quick_reissue,
                )
            # уведомление ушло (R11), reply на последнее сообщение протокола.
            notices = [s for s in sent if "пересобираю" in s["text"]]
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]["chat_id"], -1001)
            self.assertEqual(notices[0]["reply_to"], 102)
            # флаг round выставлен в state (повтор того же раунда не пошлёт второе).
            cur = feedback_state.read_state(fid, root=self.root)
            self.assertEqual(cur.get("reissue_notice_sent_round"), 1)
        finally:
            release.set()
            if fid in inflight:
                inflight[fid][0].result(timeout=5)
            executor.shutdown(wait=True)

    def test_notice_not_resent_same_round(self):
        """R11: повторный claim того же раунда (флаг уже стоит) → второе НЕ шлём.
        Имитируем повторный submit того же раунда напрямую через
        _send_reissue_start_notice (как при reclaim того же раунда)."""
        fid = self._ready(rnd=1)
        # первый notice ставит флаг.
        claimed = feedback_state.read_state(fid, root=self.root)
        sent: list = []
        with mock.patch.object(ml, "send_message",
                               lambda *a, **k: sent.append(1)):
            ml._send_reissue_start_notice("tok", claimed, root=self.root)
            self.assertEqual(len(sent), 1)
            # тот же раунд снова → флаг уже стоит → НЕ шлём.
            claimed2 = feedback_state.read_state(fid, root=self.root)
            ml._send_reissue_start_notice("tok", claimed2, root=self.root)
        self.assertEqual(len(sent), 1)  # всё ещё один

    def test_notice_send_failure_does_not_break_submit(self):
        """R11/УПУ2: send_message бросает исключение → submit перевыпуска НЕ
        падает (future всё равно создан), state не сломан."""
        fid = self._ready(rnd=1)

        def boom_send(*a, **k):
            raise RuntimeError("telegram 500")

        release = threading.Event()

        def quick_reissue(state, *, root=None):
            if not release.wait(timeout=_JOIN_TIMEOUT):
                raise AssertionError("release не выставлен")
            return {"status": "sent", "message_ids": [9001]}

        inflight: dict = {}
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with mock.patch.object(ml, "send_message", boom_send):
                # не должно бросить наружу — submit важнее уведомления.
                ml._process_reissues_async(
                    executor=executor, inflight=inflight, token="tok",
                    root=self.root, cap=1, reissue_fn=quick_reissue,
                )
            # submit состоялся несмотря на сбой send.
            self.assertIn(fid, inflight)
            self.assertEqual(
                feedback_state.read_state(fid, root=self.root)["status"], "reissuing",
            )
        finally:
            release.set()
            if fid in inflight:
                inflight[fid][0].result(timeout=5)
            executor.shutdown(wait=True)


# ===========================================================================
# R10 — listener-троттл уборки dormant
# ===========================================================================
class TestListenerDormantCleanup(_ListenerReissueBase):
    def setUp(self):
        super().setUp()
        # Троттл — модульный global; mock.patch его восстанавливает при выходе из
        # `with` (а функция делает `global ... = now`), поэтому управляем им
        # вручную: сохраняем и сбрасываем в None (как «ещё не запускали»).
        self._saved_throttle = ml._last_dormant_cleanup_mono
        ml._last_dormant_cleanup_mono = None

    def tearDown(self):
        ml._last_dormant_cleanup_mono = self._saved_throttle
        super().tearDown()

    def _write_dormant(self, *, series, age_days):
        import json
        fid = feedback_state.build_feedback_id(series, "2026-06-02", -1001)
        st = {"feedback_id": fid, "series": series, "date": "2026-06-02",
              "chat_id": -1001, "status": "dormant", "round": 1, "edits": []}
        feedback_state.write_state(st, root=self.root)
        p = feedback_state.path_for(fid, root=self.root)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["updated_at"] = (
            datetime.now(UTC) - timedelta(days=age_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return fid

    def test_listener_cleanup_removes_old_keeps_fresh_and_non_dormant(self):
        """R10: `_maybe_cleanup_dormant_states` (listener-троттл) зовёт
        cleanup_dormant_states() через env-резолв root. Старый dormant удалён,
        свежий dormant и не-dormant (reissuing) остаются."""
        old_fid = self._write_dormant(series="old", age_days=40)
        fresh_fid = self._write_dormant(series="fresh", age_days=2)
        keep_fid = self._ready(series="active", status="reissuing")

        # троттл сброшен в setUp (None) → вызов реально выполнится.
        ml._maybe_cleanup_dormant_states()

        self.assertFalse(feedback_state.path_for(old_fid, root=self.root).exists())
        self.assertTrue(feedback_state.path_for(fresh_fid, root=self.root).exists())
        self.assertTrue(feedback_state.path_for(keep_fid, root=self.root).exists())

    def test_listener_cleanup_throttled_within_window(self):
        """R10: повторный вызов в окне троттла (раз/сутки) НЕ глобит папку снова —
        второй старый dormant, созданный ПОСЛЕ первого прохода, доживает до
        следующего окна."""
        old1 = self._write_dormant(series="old1", age_days=40)
        # первый проход (троттл сброшен в setUp) удаляет old1 и ВЗВОДИТ троттл.
        ml._maybe_cleanup_dormant_states()
        self.assertFalse(feedback_state.path_for(old1, root=self.root).exists())
        # теперь троттл взведён. Второй старый dormant создаём ПОСЛЕ первого прохода.
        old2 = self._write_dormant(series="old2", age_days=40)
        # повторный вызов в окне троттла — должен быть проигнорирован (не глобит).
        ml._maybe_cleanup_dormant_states()
        self.assertTrue(feedback_state.path_for(old2, root=self.root).exists(),
                        "троттл не сработал — cleanup глобит папку каждый sweep")


if __name__ == "__main__":
    unittest.main()
