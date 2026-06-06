"""Тесты Ф2 (bot-notarius-master-plan) — cost-guard CG1–CG9.

Закрепляют логику, которой бот не платит дважды за одну встречу и держит
потолок недельных трат:
  • CG1 — per-session flock в collector (два тика → один finalize/job);
  • CG2 — переиспользование живого job в client (нет повторного сабмита);
  • CG3 — ранняя фиксация job_id в meta (переживает краш) + callback ДО поллинга;
  • CG4 — сумма часов за 7 дней из jobs API;
  • CG5 — ≤warn молча / warn..block пуш с разбивкой;
  • CG6 — ≥block взвод kill-switch + пуш;
  • CG7 — kill-switch блокирует НОВЫЙ сабмит, встреча уходит в retry;
  • CG8 — снятие только вручную (монитор/retry не снимают), после снятия — сабмит;
  • CG9 — пока взведён, счётчик ждущих встреч.

«Симуляция» из плана выражена как прогон чистых функций над снимками состояний
и над flock-файлами в tmp (concurrency на flock — реальная, два open() контендят).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase2_cost_guard -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


# Под системным python3 (без yaml/httpx/requests/...) грузим модули с заглушками
# отсутствующих third-party — тот же приём, что в test_concurrency_watchdog (yaml).
def _stub_missing(*names):
    for name in names:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)


_stub_missing("yaml", "requests", "pymorphy3", "torch", "numpy")


# httpx нужен «богатой» заглушкой: speechmatics_client делает isinstance на его
# классах-исключениях и создаёт httpx.Client как контекст-менеджер.
def _install_rich_httpx_stub():
    if "httpx" in sys.modules and getattr(sys.modules["httpx"], "_rich_stub", False):
        return
    if "httpx" in sys.modules and hasattr(sys.modules["httpx"], "Client") \
            and not isinstance(sys.modules["httpx"], types.ModuleType):
        return  # настоящий httpx — не трогаем
    fake = types.ModuleType("httpx")
    fake._rich_stub = True

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _HTTPStatusError(Exception):
        def __init__(self, *a, response=None, **k):
            super().__init__(*a)
            self.response = response

    fake.Client = _Client
    fake.TimeoutException = type("TimeoutException", (Exception,), {})
    fake.ConnectError = type("ConnectError", (Exception,), {})
    fake.RemoteProtocolError = type("RemoteProtocolError", (Exception,), {})
    fake.RequestError = type("RequestError", (Exception,), {})
    fake.HTTPStatusError = _HTTPStatusError
    sys.modules["httpx"] = fake


_install_rich_httpx_stub()


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, str(_NOTARY / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load("collector_cg1", "collector.py")
retry_failed = _load("retry_failed_cg", "retry_failed.py")
swg = _load("stt_weekly_guard_cg", "stt_weekly_guard.py")
sc = _load("speechmatics_client_cg", "lib/speechmatics_client.py")

try:
    finalize = _load("finalize_meeting_cg", "finalize-meeting.py")
except Exception:  # noqa: BLE001 — тяжёлые транзитивные импорты; CG3-meta тест скипнется
    finalize = None


_MIN_RESULTS = {"results": [
    {"type": "word", "start_time": 0.0, "end_time": 1.0,
     "alternatives": [{"speaker": "S1", "content": "привет"}]},
]}


# ─────────────────────────────── CG1 ───────────────────────────────

class TestCG1CollectorFlock(unittest.TestCase):
    """Два пересёкшихся тика по одной встрече → один finalize (один job)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        collector.STATE_DIR = self.tmp  # перенаправляем lock-каталог в tmp

    def test_second_concurrent_tick_sees_lock_busy(self):
        # Два независимых open()/flock на одну sessionUid контендят (как два тика).
        with collector._session_finalize_lock("tm-123") as l1:
            self.assertTrue(l1, "первый тик должен взять лок")
            with collector._session_finalize_lock("tm-123") as l2:
                self.assertFalse(l2, "второй тик по той же встрече — лок занят")
            # другая встреча не блокируется
            with collector._session_finalize_lock("tm-999") as l3:
                self.assertTrue(l3, "другая встреча — лок свободен")
        # после освобождения лок снова берётся
        with collector._session_finalize_lock("tm-123") as l4:
            self.assertTrue(l4, "после release лок снова свободен")

    def test_finalize_and_collect_skips_when_locked(self):
        calls = []
        orig = collector._do_finalize_and_collect
        collector._do_finalize_and_collect = lambda sid: calls.append(sid)
        try:
            # держим лок (имитируем идущий finalize прошлого тика)
            with collector._session_finalize_lock("tm-abc") as held:
                self.assertTrue(held)
                collector._finalize_and_collect("tm-abc")  # параллельный тик
                self.assertEqual(calls, [], "под занятым локом finalize НЕ запускается (CG1)")
            # лок свободен → finalize отрабатывает
            collector._finalize_and_collect("tm-abc")
            self.assertEqual(calls, ["tm-abc"])
        finally:
            collector._do_finalize_and_collect = orig

    def test_lock_path_sanitizes_session_uid(self):
        p = collector._finalize_lock_path("a/b\\c")
        self.assertNotIn("/", p.name)
        self.assertNotIn("\\", p.name)


# ──────────────────────────── CG2 / CG3 / CG7 / CG8 (client) ────────────────────────────

class TestClientDedupKillswitch(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wav = self.tmp / "x.wav"
        self.wav.write_bytes(b"RIFF0000WAVEfmt ")
        os.environ["SPEECHMATICS_API_KEY"] = "test-key"
        # kill-switch по умолчанию ВЫКЛ — путь на несуществующий файл
        os.environ["STT_KILLSWITCH_PATH"] = str(self.tmp / "no-such.flag")
        # дефолтные безопасные заглушки внутренностей (каждый тест переопределяет нужное)
        self._submit_calls = []
        sc._submit_job = lambda *a, **k: (self._submit_calls.append(1), "NEWJOB")[1]
        sc._poll_until_done = lambda *a, **k: None
        sc._fetch_transcript = lambda *a, **k: dict(_MIN_RESULTS)

    def tearDown(self):
        os.environ.pop("STT_KILLSWITCH_PATH", None)

    # --- CG2: чистая классификация решения по существующему job ---
    def test_classify_existing_job(self):
        self.assertEqual(sc._classify_existing_job("done"), "reuse")
        self.assertEqual(sc._classify_existing_job("running"), "reuse")
        self.assertEqual(sc._classify_existing_job("rejected"), "rejected")
        self.assertEqual(sc._classify_existing_job(None), "resubmit")
        self.assertEqual(sc._classify_existing_job("expired"), "resubmit")

    # --- CG2: живой job → НЕ сабмитим заново ---
    def test_reuse_alive_job_does_not_resubmit(self):
        sc._get_job_status = lambda *a, **k: "done"
        res = sc.transcribe_diarize_wav(str(self.wav), existing_job_id="OLDJOB")
        self.assertEqual(self._submit_calls, [], "CG2: повторного сабмита быть не должно")
        self.assertEqual(res.job_id, "OLDJOB", "переиспользован старый job_id")

    def test_reuse_running_job(self):
        sc._get_job_status = lambda *a, **k: "running"
        res = sc.transcribe_diarize_wav(str(self.wav), existing_job_id="RUN1")
        self.assertEqual(self._submit_calls, [])
        self.assertEqual(res.job_id, "RUN1")

    # --- CG2: rejected job → raise, не сабмитим ---
    def test_reuse_rejected_raises(self):
        sc._get_job_status = lambda *a, **k: "rejected"
        with self.assertRaises(sc.SpeechmaticsRejectedError):
            sc.transcribe_diarize_wav(str(self.wav), existing_job_id="BAD")
        self.assertEqual(self._submit_calls, [])

    # --- CG2: истёкший/неизвестный job → сабмитим заново ---
    def test_reuse_expired_resubmits(self):
        sc._get_job_status = lambda *a, **k: None  # 404 / истёк ретеншн
        res = sc.transcribe_diarize_wav(str(self.wav), existing_job_id="GONE")
        self.assertEqual(self._submit_calls, [1], "истёкший job → новый сабмит")
        self.assertEqual(res.job_id, "NEWJOB")

    # --- Recovery: WAV почищен, но job жив → переиспользуем БЕЗ файла на диске ---
    def test_reuse_alive_job_without_wav_on_disk(self):
        sc._get_job_status = lambda *a, **k: "done"
        gone = self.tmp / "already-cleaned.wav"  # файла НЕТ на диске
        self.assertFalse(gone.exists())
        res = sc.transcribe_diarize_wav(str(gone), existing_job_id="ALIVE")
        self.assertEqual(res.job_id, "ALIVE", "переиспользован job без чтения WAV")
        self.assertEqual(self._submit_calls, [], "сабмита нет — файл не нужен")

    # --- Семантика сохранена: нет job для переиспользования + нет WAV → FileNotFoundError ---
    def test_submit_without_wav_raises_filenotfound(self):
        gone = self.tmp / "missing.wav"
        self.assertFalse(gone.exists())
        with self.assertRaises(FileNotFoundError):
            sc.transcribe_diarize_wav(str(gone))  # свежая встреча, сабмит неизбежен
        self.assertEqual(self._submit_calls, [], "до сабмита не дошли — файла нет")

    # --- Кромка: job истёк И WAV почищен → восстановить нельзя, чистый SpeechmaticsError ---
    def test_reuse_expired_without_wav_raises_speechmatics_error(self):
        sc._get_job_status = lambda *a, **k: None  # истёк ретеншн
        gone = self.tmp / "missing2.wav"
        with self.assertRaises(sc.SpeechmaticsError):
            sc.transcribe_diarize_wav(str(gone), existing_job_id="GONE")
        self.assertEqual(self._submit_calls, [], "сабмит не вызван — файла нет")

    # --- CG3: callback вызывается СРАЗУ после сабмита, ДО поллинга ---
    def test_on_job_submitted_fires_before_poll(self):
        order = []
        sc._submit_job = lambda *a, **k: (order.append("submit"), "J1")[1]
        sc._poll_until_done = lambda *a, **k: order.append("poll")
        sc._fetch_transcript = lambda *a, **k: (order.append("fetch"), dict(_MIN_RESULTS))[1]
        got = []

        def cb(job_id):
            order.append(f"cb:{job_id}")
            got.append(job_id)

        res = sc.transcribe_diarize_wav(str(self.wav), on_job_submitted=cb)
        self.assertEqual(order, ["submit", "cb:J1", "poll", "fetch"],
                         "CG3: job_id фиксируется до поллинга")
        self.assertEqual(got, ["J1"])
        self.assertEqual(res.job_id, "J1")

    def test_callback_failure_is_non_fatal(self):
        # сбой записи job_id не должен ронять расшифровку (job уже сабмичен)
        sc._submit_job = lambda *a, **k: "J2"

        def bad_cb(job_id):
            raise OSError("disk full")

        res = sc.transcribe_diarize_wav(str(self.wav), on_job_submitted=bad_cb)
        self.assertEqual(res.job_id, "J2")

    # --- CG7: kill-switch взведён → новый сабмит НЕ делается ---
    def test_killswitch_blocks_new_submit(self):
        flag = self.tmp / "ks.flag"
        flag.write_text("stop")
        os.environ["STT_KILLSWITCH_PATH"] = str(flag)
        self.assertTrue(sc.killswitch_armed())
        with self.assertRaises(sc.SpeechmaticsKillSwitchError):
            sc.transcribe_diarize_wav(str(self.wav))  # свежая встреча
        self.assertEqual(self._submit_calls, [], "CG7: при взведённом флаге сабмита нет")

    # --- CG7: переиспользование живого job НЕ блокируется (деньги уже потрачены) ---
    def test_killswitch_does_not_block_reuse(self):
        flag = self.tmp / "ks.flag"
        flag.write_text("stop")
        os.environ["STT_KILLSWITCH_PATH"] = str(flag)
        sc._get_job_status = lambda *a, **k: "done"
        res = sc.transcribe_diarize_wav(str(self.wav), existing_job_id="ALIVE")
        self.assertEqual(res.job_id, "ALIVE")
        self.assertEqual(self._submit_calls, [])

    # --- CG8: после ручного снятия флага сабмит проходит ---
    def test_killswitch_removed_allows_submit(self):
        flag = self.tmp / "ks.flag"
        flag.write_text("stop")
        os.environ["STT_KILLSWITCH_PATH"] = str(flag)
        self.assertTrue(sc.killswitch_armed())
        flag.unlink()  # ручное снятие
        self.assertFalse(sc.killswitch_armed())
        sc._submit_job = lambda *a, **k: "AFTER"
        res = sc.transcribe_diarize_wav(str(self.wav))
        self.assertEqual(res.job_id, "AFTER", "CG8: после снятия флага расшифровка идёт")


# ──────────────────────────── CG3 (finalize meta) ────────────────────────────

@unittest.skipIf(finalize is None, "finalize-meeting.py не загрузился под system python3")
class TestCG3FinalizeMetaPersist(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_persist_job_id_to_meta_roundtrip(self):
        mp = str(self.tmp / "tm-1.meta.json")
        meta = {"sessionUid": "tm-1", "files": {"wav": "/x.wav"}}
        finalize._atomic_write_json(mp, meta)
        finalize._persist_job_id_to_meta(mp, meta, "JOB-XYZ")
        on_disk = json.loads(Path(mp).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["speechmatics_job_id"], "JOB-XYZ",
                         "CG3: job_id на диске — переживёт краш/рестарт")
        self.assertEqual(meta["speechmatics_job_id"], "JOB-XYZ",
                         "in-memory meta тоже несёт id для текущего процесса")

    def test_persist_is_atomic_no_partial(self):
        # запись через tmp+replace — после успешного вызова файл — валидный JSON
        mp = str(self.tmp / "tm-2.meta.json")
        meta = {"sessionUid": "tm-2", "files": {"wav": "/x.wav"}, "big": "x" * 5000}
        finalize._persist_job_id_to_meta(mp, meta, "JOB-2")
        json.loads(Path(mp).read_text(encoding="utf-8"))  # не кидает → не «полуфайл»

    def test_crash_to_reuse_handshake(self):
        # CG3→CG2: meta с job_id (как после краша) скармливаем клиенту как
        # existing_job_id → переиспользование без повторного сабмита.
        mp = str(self.tmp / "tm-3.meta.json")
        meta = {"sessionUid": "tm-3", "files": {"wav": "/x.wav"}}
        finalize._persist_job_id_to_meta(mp, meta, "SURVIVED")
        reloaded = json.loads(Path(mp).read_text(encoding="utf-8"))
        self.assertEqual(reloaded.get("speechmatics_job_id"), "SURVIVED")


# ──────────────────────────── retry_failed CG7/CG8 ────────────────────────────

class TestRetryKillswitchScheduling(unittest.TestCase):
    """blocked_by_killswitch не выжигает 24ч-бюджет и не шлёт «сдались»."""

    def setUp(self):
        self.now = dt.datetime(2026, 6, 6, 12, 0, 0)
        self.old = (self.now - dt.timedelta(hours=30)).isoformat() + "Z"

    def test_blocked_meeting_always_due_no_giveup(self):
        st = {"first_failed_at": self.old, "attempts": 99, "blocked_by_killswitch": True}
        # 30ч прошло, attempts заведомо за потолком — но из-за флага всё равно due,
        # и финальный «сдались»-push НЕ шлётся (ждём ручного снятия, CG8).
        self.assertTrue(retry_failed._due_to_attempt(st, self.now))
        self.assertFalse(retry_failed._final_push_due(st, self.now))

    def test_rejected_still_wins_over_killswitch(self):
        st = {"first_failed_at": self.old, "attempts": 1,
              "blocked_by_killswitch": True, "rejected": True}
        self.assertFalse(retry_failed._due_to_attempt(st, self.now))

    def test_normal_meeting_unaffected(self):
        # без флага старая логика цела: 30ч + attempts>14 → не due, final push due
        st = {"first_failed_at": self.old, "attempts": 99}
        self.assertFalse(retry_failed._due_to_attempt(st, self.now))
        self.assertTrue(retry_failed._final_push_due(st, self.now))


# ──────────────────────────── stt_weekly_guard CG4–CG9 ────────────────────────────

class _Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, message, *, dedupe_key=None):
        self.calls.append((message, dedupe_key))
        return True


class TestWeeklyGuard(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.failed = self.tmp / "_failed"
        self.failed.mkdir()
        self.ks = self.tmp / "ks.flag"
        self.now = dt.datetime(2026, 6, 6, 12, 0, 0)

    def _jobs_for_hours(self, hours):
        # один job на N часов, созданный вчера (в окне)
        return [{"created_at": (self.now - dt.timedelta(days=1)).isoformat() + "Z",
                 "duration": int(hours * 3600)}]

    # --- CG4 ---
    def test_compute_weekly_summary_window_and_grouping(self):
        jobs = [
            {"created_at": "2026-06-05T10:00:00.000Z", "duration": 3600},
            {"created_at": "2026-06-05T14:00:00.000Z", "duration": 1800},
            {"created_at": "2026-06-01T09:00:00.000Z", "duration": 7200},
            {"created_at": "2026-05-20T09:00:00.000Z", "duration": 9999},   # старый — вне окна
            {"created_at": "broken", "duration": 100},                       # мусор — пропуск
        ]
        s = swg.compute_weekly_summary(jobs, self.now)
        self.assertAlmostEqual(s.total_hours, 3.5, places=6)
        self.assertEqual(s.job_count, 3)
        self.assertAlmostEqual(s.by_day["2026-06-05"], 1.5, places=6)
        self.assertAlmostEqual(s.by_day["2026-06-01"], 2.0, places=6)

    # --- CG5: ≤warn молча ---
    def test_below_warn_is_silent(self):
        rec = _Rec()
        res = swg.run_guard(now=self.now, failed_dir=self.failed, warn_h=10, block_h=15,
                            killswitch_path=self.ks, jobs_fetcher=lambda n: self._jobs_for_hours(5),
                            pusher=rec)
        self.assertEqual(res["actions"], ["quiet"])
        self.assertEqual(rec.calls, [], "≤10 ч — никаких пушей (CG5)")
        self.assertFalse(self.ks.exists())

    # --- CG5: warn..block пуш с разбивкой ---
    def test_warn_band_pushes_with_breakdown(self):
        rec = _Rec()
        res = swg.run_guard(now=self.now, failed_dir=self.failed, warn_h=10, block_h=15,
                            killswitch_path=self.ks, jobs_fetcher=lambda n: self._jobs_for_hours(12),
                            pusher=rec)
        self.assertEqual(res["actions"], ["warn"])
        self.assertEqual(len(rec.calls), 1)
        msg, key = rec.calls[0]
        self.assertIn("12.0 ч", msg)
        self.assertIn("По дням", msg)
        self.assertTrue(key.startswith("stt-weekly-warn"))
        self.assertFalse(self.ks.exists(), "warn НЕ взводит kill-switch")

    # --- CG6: ≥block взвод + пуш ---
    def test_block_arms_killswitch_and_pushes(self):
        rec = _Rec()
        res = swg.run_guard(now=self.now, failed_dir=self.failed, warn_h=10, block_h=15,
                            killswitch_path=self.ks, jobs_fetcher=lambda n: self._jobs_for_hours(16),
                            pusher=rec)
        self.assertEqual(res["actions"], ["armed"])
        self.assertTrue(self.ks.exists(), "CG6: kill-switch-флаг создан")
        self.assertTrue(res["armed"])
        self.assertIn("ОСТАНОВЛЕНА", rec.calls[0][0])
        self.assertIn("16.0 ч", rec.calls[0][0])

    def test_block_dry_run_does_not_arm(self):
        rec = _Rec()
        res = swg.run_guard(now=self.now, failed_dir=self.failed, warn_h=10, block_h=15,
                            killswitch_path=self.ks, jobs_fetcher=lambda n: self._jobs_for_hours(16),
                            pusher=rec, dry_run=True)
        self.assertEqual(res["actions"], ["armed"])
        self.assertFalse(self.ks.exists(), "dry-run не трогает реальный флаг")

    # --- CG9: пока взведён — напоминание с числом ждущих ---
    def test_reminder_while_armed_counts_waiting(self):
        # предварительно взводим флаг (как будто это сделал прошлый прогон)
        swg.arm_killswitch(self.ks, hours=16.0, now=self.now)
        # 3 встречи отложены kill-switch'ем + 1 упавшая по другой причине
        for i in range(3):
            (self.failed / f"m{i}.retry-state.json").write_text(
                json.dumps({"session_uid": f"m{i}", "blocked_by_killswitch": True}))
        (self.failed / "other.retry-state.json").write_text(
            json.dumps({"session_uid": "other", "rejected": True}))
        rec = _Rec()
        res = swg.run_guard(now=self.now, failed_dir=self.failed, warn_h=10, block_h=15,
                            killswitch_path=self.ks, jobs_fetcher=lambda n: [], pusher=rec)
        self.assertEqual(res["actions"], ["reminder"])
        self.assertEqual(res["waiting"], 3, "считаем только blocked_by_killswitch (CG9)")
        msg, key = rec.calls[0]
        self.assertIn("3", msg)
        self.assertTrue(key.startswith("stt-killswitch-reminder"))

    # --- CG8: монитор НЕ снимает флаг сам ---
    def test_guard_never_removes_flag(self):
        swg.arm_killswitch(self.ks, hours=20.0, now=self.now)
        rec = _Rec()
        # даже если часов теперь мало — флаг остаётся (снятие только вручную)
        swg.run_guard(now=self.now, failed_dir=self.failed, warn_h=10, block_h=15,
                      killswitch_path=self.ks, jobs_fetcher=lambda n: self._jobs_for_hours(1),
                      pusher=rec)
        self.assertTrue(self.ks.exists(), "CG8: kill-switch снимается только вручную")

    # --- CG4: реальная пагинация + дедуп через подставной httpx-клиент ---
    def test_fetch_paginates_dedups_and_stops_at_window(self):
        now = self.now
        in_win = (now - dt.timedelta(days=1)).isoformat() + "Z"
        in_win2 = (now - dt.timedelta(days=2)).isoformat() + "Z"
        old = (now - dt.timedelta(days=30)).isoformat() + "Z"
        page1 = [{"id": f"j{i}", "created_at": in_win, "duration": 3600} for i in range(100)]
        # 2-я страница: повтор j0 (дедуп), один свежий, один старый (→ стоп по окну)
        page2 = [{"id": "j0", "created_at": in_win, "duration": 3600},
                 {"id": "j100", "created_at": in_win2, "duration": 3600},
                 {"id": "jOLD", "created_at": old, "duration": 9999}]
        pages = [page1, page2]
        calls = {"n": 0}

        class _Resp:
            def __init__(self, jobs):
                self._jobs = jobs

            def raise_for_status(self):
                pass

            def json(self):
                return {"jobs": self._jobs}

        class _Cli:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None, params=None):
                i = calls["n"]
                calls["n"] += 1
                return _Resp(pages[i] if i < len(pages) else [])

        httpx_mod = sys.modules["httpx"]
        orig_client = httpx_mod.Client
        httpx_mod.Client = _Cli
        os.environ["SPEECHMATICS_API_KEY"] = "k"
        try:
            jobs = swg.fetch_jobs_speechmatics(now, page_limit=100, max_pages=5)
        finally:
            httpx_mod.Client = orig_client
        # 100 (page1) + 1 свежий (page2); повтор j0 не задвоен; jOLD вне окна; стоп после 2 страниц
        self.assertEqual(len(jobs), 101)
        self.assertEqual(calls["n"], 2, "на 2-й странице встретили старый job → стоп пагинации")
        self.assertAlmostEqual(swg.compute_weekly_summary(jobs, now).total_hours, 101.0, places=3)

    def test_count_waiting_meetings(self):
        (self.failed / "a.retry-state.json").write_text(json.dumps({"blocked_by_killswitch": True}))
        (self.failed / "b.retry-state.json").write_text(json.dumps({"blocked_by_killswitch": True}))
        (self.failed / "c.retry-state.json").write_text(json.dumps({"rejected": True}))
        (self.failed / "d.retry-state.json").write_text("{bad json")
        self.assertEqual(swg.count_waiting_meetings(self.failed), 2)
        self.assertEqual(swg.count_waiting_meetings(self.tmp / "nope"), 0)


if __name__ == "__main__":
    unittest.main()
