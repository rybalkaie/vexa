"""ISS-60: сторож входа и хода встречи — решения и проход по журналу.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_join_watch_iss60 -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.lib import join_watch as jw  # noqa: E402

NOW = datetime(2026, 9, 28, 7, 57, tzinfo=timezone.utc)
START = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)


def obs(phase=None, *, reason=None, seen=True, status="running", code=None, age=None, kit=None, fb=None, events=()):
    return jw.Observation(
        phase=phase, reason=reason, status_seen=seen,
        container=jw.ContainerState(status, code),
        fallback_used=fb, crash_kit=kit, phase_age_sec=age, events=tuple(events),
    )


def decide(o, *, attempt=1, launched_ago=60, now=NOW, start=START, max_attempts=2):
    return jw.decide(
        o, attempt=attempt, max_attempts_=max_attempts,
        launched_at=now - timedelta(seconds=launched_ago), start_at=start, now=now,
    )


class DecideTest(unittest.TestCase):
    def test_in_call_stable_is_ok(self):
        self.assertEqual(decide(obs("in_call", age=30)).action, jw.OK)

    def test_in_call_fresh_waits_for_false_positive_check(self):
        self.assertEqual(decide(obs("in_call", age=3)).action, jw.WAIT)

    def test_in_call_then_normal_exit_is_ok(self):
        self.assertEqual(decide(obs("in_call", age=3, status="exited", code=0)).action, jw.OK)

    def test_in_call_then_crash_soon_is_failure(self):
        # цикл5 Н2: упал с кодом 1 вскоре после входа — встреча не пишется
        d = decide(obs("in_call", age=40, status="exited", code=1))
        self.assertEqual((d.action, d.reason), (jw.RETRY, "crashed_after_join"))

    def test_in_call_docker_unknown_waits(self):
        # цикл5 Н2: docker не ответил — это не «всё хорошо»
        self.assertEqual(decide(obs("in_call", age=3, status="unknown")).action, jw.WAIT)
        self.assertEqual(decide(obs("in_call", age=3, status="paused")).action, jw.WAIT)

    def test_failed_after_in_call_is_failure(self):
        d = decide(obs("failed", reason="admission_false_positive", status="exited", code=0, age=5))
        self.assertEqual((d.action, d.reason), (jw.RETRY, "admission_false_positive"))

    def test_failed_first_attempt_retries(self):
        d = decide(obs("failed", reason="name_input_not_found", status="exited", code=1, age=10))
        self.assertEqual((d.action, d.reason), (jw.RETRY, "name_input_not_found"))

    def test_failed_second_attempt_alerts(self):
        d = decide(obs("failed", reason="name_input_not_found", status="exited", code=1, age=10), attempt=2)
        self.assertEqual((d.action, d.reason), (jw.ALERT, "name_input_not_found"))

    def test_no_retry_for_meeting_problems(self):
        for reason in ("meeting_not_found", "admission_rejected"):
            d = decide(obs("failed", reason=reason, status="exited", code=0, age=10))
            self.assertEqual(d.action, jw.ALERT, reason)

    def test_failed_but_still_running_waits_then_stops(self):
        self.assertEqual(decide(obs("failed", reason="still_in_lobby", age=20)).action, jw.WAIT)
        d = decide(obs("failed", reason="still_in_lobby", age=120))
        self.assertEqual((d.action, d.reason), (jw.STOP_RETRY, "still_in_lobby"))

    def test_status_seen_container_gone_is_crash(self):
        d = decide(obs("joining", status="exited", code=137, age=40))
        self.assertEqual((d.action, d.reason), (jw.RETRY, "crashed"))
        d = decide(obs("joining", status="missing", age=40))
        self.assertEqual((d.action, d.reason), (jw.RETRY, "crashed"))

    def test_legacy_image_exit_code_1_is_failure(self):
        d = decide(obs(None, seen=False, status="exited", code=1))
        self.assertEqual((d.action, d.reason), (jw.RETRY, "join_error"))

    def test_legacy_image_exit_0_is_forgotten(self):
        self.assertEqual(decide(obs(None, seen=False, status="exited", code=0)).action, jw.FORGET)

    def test_legacy_running_is_never_stopped(self):
        self.assertEqual(decide(obs(None, seen=False), launched_ago=600).action, jw.WAIT)
        self.assertEqual(decide(obs(None, seen=False), launched_ago=jw.LEGACY_WATCH_SEC + 1).action, jw.FORGET)

    def test_stuck_joining_is_stopped(self):
        self.assertEqual(decide(obs("joining", age=200), launched_ago=200).action, jw.WAIT)
        d = decide(obs("joining", age=400), launched_ago=jw.STUCK_JOINING_SEC + 10)
        self.assertEqual((d.action, d.reason), (jw.STOP_RETRY, "stuck_joining"))

    def test_admission_wait_is_not_killed_early(self):
        # цикл5 Н1: бот ждёт впуска до 300 с и может войти «методом исключения»
        d = decide(obs("admission", age=299), launched_ago=420)
        self.assertEqual(d.action, jw.WAIT)
        d = decide(obs("admission", age=jw.STUCK_ADMISSION_SEC + 1), launched_ago=600)
        self.assertEqual((d.action, d.reason), (jw.STOP_RETRY, "stuck_admission"))

    def test_waiting_room_is_patient(self):
        self.assertEqual(decide(obs("waiting_room", age=200), launched_ago=300).action, jw.WAIT)
        d = decide(obs("waiting_room", age=500), launched_ago=jw.STUCK_WAITING_SEC + 1)
        self.assertEqual((d.action, d.reason), (jw.STOP_RETRY, "waiting_room_not_admitted"))

    def test_retry_deadline(self):
        late = START + timedelta(minutes=jw.RETRY_DEADLINE_AFTER_START_MIN + 1)
        d = decide(obs("failed", reason="name_input_not_found", status="exited", code=1, age=5), now=late)
        self.assertEqual(d.action, jw.ALERT)

    def test_retries_disabled(self):
        d = decide(obs("failed", reason="name_input_not_found", status="exited", code=1, age=5), max_attempts=1)
        self.assertEqual(d.action, jw.ALERT)

    def test_docker_unknown_waits_then_forgets(self):
        self.assertEqual(decide(obs("joining", status="unknown", age=10)).action, jw.WAIT)
        self.assertEqual(decide(obs("joining", status="unknown"), launched_ago=jw.UNKNOWN_FORGET_SEC + 1).action, jw.FORGET)


class FakeWorld:
    """Поддельный docker + статусы + отправка, чтобы гонять tick без внешнего мира."""

    def __init__(self):
        self.status: dict = {}
        self.containers: dict = {}
        self.stopped: list = []
        self.relaunched: list = []
        self.alerts: list = []
        self.events: list = []
        self.notes: list = []
        self.persisted = 0
        self.persist_fails = False
        self.persist_failed_msgs: list = []
        self.relaunch_ok = True

    def hooks(self):
        return jw.Hooks(
            inspect=lambda name: self.containers.get(name, jw.ContainerState("missing")),
            read_status=lambda uid: self.status.get(uid),
            stop=lambda name: self.stopped.append(name),
            plan_retry=lambda entry, n: {"session_uid": f"auto-x-a{n}", "container": f"vexa-notarius-auto-x-a{n}"},
            relaunch=self._relaunch,
            alert=lambda entry, reason, n, kit, note: self.alerts.append((reason, n, kit, note)),
            event_alert=lambda entry, et, ev: self.events.append(et),
            note_owner=lambda text: self.notes.append(text),
            persist=self._persist,
            persist_failed=lambda text: self.persist_failed_msgs.append(text),
        )

    def _persist(self, ledger):
        if self.persist_fails:
            raise OSError("No space left on device")
        self.persisted += 1

    def _relaunch(self, entry, attempt):
        self.relaunched.append(attempt)
        return self.relaunch_ok


def st(phase, *, at, reason=None, kit=None, fb=None, events=None):
    d = {"phase": phase, "updated_at": at.isoformat()}
    if reason:
        d["reason"] = reason
    if kit:
        d["crash_kit"] = kit
    if fb:
        d["fallback_used"] = fb
    if events:
        d["events"] = events
    return d


class TickTest(unittest.TestCase):
    def setUp(self):
        self.ledger: dict = {}
        self.item = {"meeting_id": "anzhee-direktorat", "series": "anzhee-direktorat",
                     "start_at": START.isoformat(), "url": "https://example.invalid/j/1", "event_id": "e1"}
        jw.register_launch(self.ledger, "k1", self.item, session_uid="auto-x",
                           container="vexa-notarius-auto-x", attempt=1, now=NOW)
        self.w = FakeWorld()

    def run_tick(self, now, **kw):
        return jw.tick(self.ledger, now=now, hooks=self.w.hooks(), **kw)

    def fail_attempt(self, uid, at, reason="name_input_not_found"):
        self.w.status[uid] = st("failed", at=at, reason=reason)
        self.w.containers[f"vexa-notarius-{uid}"] = jw.ContainerState("exited", 1)

    def test_failed_retry_then_alert_once(self):
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1 - timedelta(seconds=20))
        self.assertTrue(self.run_tick(t1))
        self.assertEqual(self.w.relaunched, [2])
        self.assertEqual(self.w.alerts, [])
        self.assertEqual(len(self.ledger["k1"]["attempts"]), 2)

        t2 = t1 + timedelta(seconds=60)
        self.fail_attempt("auto-x-a2", t2 - timedelta(seconds=10))
        self.run_tick(t2)
        self.assertEqual(self.w.alerts, [("name_input_not_found", 2, None, None)])
        self.assertEqual(self.ledger["k1"]["state"], "alerted")
        self.run_tick(t2 + timedelta(seconds=60))
        self.assertEqual(len(self.w.alerts), 1)
        self.assertEqual(self.w.stopped, [])

    def test_no_slot_for_retry_alerts_with_note(self):
        self.w.relaunch_ok = False
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1, "still_in_lobby")
        self.run_tick(t1)
        self.assertEqual(len(self.w.alerts), 1)
        reason, n, _kit, note = self.w.alerts[0]
        self.assertEqual((reason, n), ("still_in_lobby", 1))
        self.assertIn("повтор не запущен", note)  # цикл5 Н11
        self.assertEqual(len(self.ledger["k1"]["attempts"]), 1)

    def test_success_on_second_try_notes_owner_quietly(self):
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1, "still_in_lobby")
        self.run_tick(t1)
        t2 = t1 + timedelta(seconds=90)
        self.w.status["auto-x-a2"] = st("in_call", at=t2 - timedelta(seconds=40))
        self.w.containers["vexa-notarius-auto-x-a2"] = jw.ContainerState("running")
        self.run_tick(t2)
        self.assertEqual(self.ledger["k1"]["state"], "in_call")
        self.assertEqual(self.w.alerts, [])
        self.assertEqual(len(self.w.notes), 1)
        self.assertIn("второй попытки", self.w.notes[0])

    def test_ok_first_try_is_silent_and_watches_to_the_end(self):
        t1 = NOW + timedelta(seconds=90)
        self.w.status["auto-x"] = st("in_call", at=NOW + timedelta(seconds=40))
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.run_tick(t1)
        self.assertEqual(self.ledger["k1"]["state"], "in_call")
        self.assertEqual((self.w.alerts, self.w.notes, self.w.events), ([], [], []))
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("exited", 0)
        self.run_tick(t1 + timedelta(minutes=50))
        self.assertEqual(self.ledger["k1"]["state"], "done")

    def test_meeting_events_delivered_once(self):
        # цикл5 С1/Н4: выброс посреди встречи — сообщение шлёт сторож, один раз
        t1 = NOW + timedelta(seconds=90)
        self.w.status["auto-x"] = st("in_call", at=NOW + timedelta(seconds=40))
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.run_tick(t1)
        ev = {"type": "evicted_failure", "at": (t1 + timedelta(minutes=10)).isoformat()}
        self.w.status["auto-x"] = st("in_call", at=NOW + timedelta(seconds=40), events=[ev])
        self.run_tick(t1 + timedelta(minutes=11))
        self.run_tick(t1 + timedelta(minutes=12))
        self.assertEqual(self.w.events, ["evicted_failure"])
        deaf = {"type": "deaf_after_start", "at": (t1 + timedelta(minutes=20)).isoformat()}
        self.w.status["auto-x"] = st("in_call", at=NOW + timedelta(seconds=40), events=[ev, deaf])
        self.run_tick(t1 + timedelta(minutes=21))
        self.assertEqual(self.w.events, ["evicted_failure", "deaf_after_start"])

    def test_event_already_there_when_join_confirmed(self):
        t1 = NOW + timedelta(seconds=90)
        ev = {"type": "evicted_failure", "at": t1.isoformat()}
        self.w.status["auto-x"] = st("in_call", at=NOW + timedelta(seconds=40), events=[ev])
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("exited", 0)
        self.run_tick(t1)
        self.assertEqual(self.w.events, ["evicted_failure"])
        self.assertEqual(self.ledger["k1"]["state"], "done")

    def test_waiting_room_nudge_once(self):
        # цикл5 У1: людей просят впустить бота, пока он ждёт
        t1 = NOW + timedelta(seconds=100)
        self.w.status["auto-x"] = st("waiting_room", at=t1 - timedelta(seconds=40))
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.run_tick(t1)
        self.run_tick(t1 + timedelta(seconds=60))
        self.assertEqual(self.w.events, ["waiting_room"])

    def test_fallback_join_notes_owner(self):
        t1 = NOW + timedelta(seconds=90)
        self.w.status["auto-x"] = st("in_call", at=NOW + timedelta(seconds=40), fb={"join": "по тексту кнопки"})
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.run_tick(t1)
        self.assertEqual(len(self.w.notes), 1)
        self.assertIn("запасным путём", self.w.notes[0])

    def test_note_failure_does_not_break_tick(self):
        # цикл5 Н5: упавшая заметка не роняет тик и не повторяет чужих сообщений
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1, "still_in_lobby")
        self.run_tick(t1)
        hooks = self.w.hooks()
        hooks.note_owner = mock.Mock(side_effect=OSError("tg-send"))
        t2 = t1 + timedelta(seconds=90)
        self.w.status["auto-x-a2"] = st("in_call", at=t2 - timedelta(seconds=40))
        self.w.containers["vexa-notarius-auto-x-a2"] = jw.ContainerState("running")
        jw.tick(self.ledger, now=t2, hooks=hooks)
        self.assertEqual(self.ledger["k1"]["state"], "in_call")

    def test_stuck_joining_is_stopped_and_retried(self):
        t1 = NOW + timedelta(seconds=jw.STUCK_JOINING_SEC + 5)
        self.w.status["auto-x"] = st("joining", at=NOW + timedelta(seconds=2))
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.run_tick(t1)
        self.assertEqual(self.w.stopped, ["vexa-notarius-auto-x"])
        self.assertEqual(self.w.relaunched, [2])

    def test_legacy_running_never_stopped(self):
        t1 = NOW + timedelta(seconds=jw.LEGACY_WATCH_SEC + 5)
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.run_tick(t1)
        self.assertEqual(self.w.stopped, [])
        self.assertEqual(self.ledger["k1"]["state"], "forgotten")

    def test_persist_failure_blocks_actions(self):
        # цикл5 С3/У10: журнал не записался — ни повтора, ни сообщения людям
        self.w.persist_fails = True
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1)
        for i in range(3):
            self.run_tick(t1 + timedelta(minutes=i))
        self.assertEqual(self.w.relaunched, [])
        self.assertEqual(self.w.alerts, [])
        self.assertTrue(self.w.persist_failed_msgs)
        self.assertEqual(len(self.ledger["k1"]["attempts"]), 1)
        # диск починили — всё продолжается
        self.w.persist_fails = False
        self.run_tick(t1 + timedelta(minutes=4))
        self.assertEqual(self.w.relaunched, [2])

    def test_persist_failure_blocks_alert(self):
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1)
        self.run_tick(t1)
        self.w.persist_fails = True
        t2 = t1 + timedelta(seconds=60)
        self.fail_attempt("auto-x-a2", t2)
        self.run_tick(t2)
        self.run_tick(t2 + timedelta(seconds=60))
        self.assertEqual(self.w.alerts, [])
        self.assertEqual(self.ledger["k1"]["state"], "watching")

    def test_hooks_errors_do_not_escape(self):
        hooks = self.w.hooks()
        hooks.inspect = mock.Mock(side_effect=RuntimeError("docker down"))
        hooks.read_status = mock.Mock(side_effect=RuntimeError("fs"))
        jw.tick(self.ledger, now=NOW + timedelta(seconds=30), hooks=hooks)
        self.assertEqual(self.ledger["k1"]["state"], "watching")

    def test_ledger_ttl(self):
        jw.tick(self.ledger, now=NOW + timedelta(hours=jw.LEDGER_TTL_HOURS + 1), hooks=self.w.hooks())
        self.assertNotIn("k1", self.ledger)

    def test_no_retry_when_paused(self):
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1)
        self.run_tick(t1, allow_retry=False)
        self.assertEqual(self.w.relaunched, [])
        self.assertEqual(len(self.w.alerts), 1)


class LedgerIoTest(unittest.TestCase):
    def test_roundtrip_in_registry_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MEETING_NOTARY_REGISTRY_DIR": tmp}):
            led = jw.load_ledger()
            self.assertEqual(led, {})
            jw.register_launch(led, "k", {"meeting_id": "m", "start_at": START.isoformat(), "url": "u"},
                               session_uid="s", container="c", attempt=1, now=NOW)
            real_open = os.open
            modes = []

            def spy_open(path, flags, mode=0o777, *a, **kw):
                if str(path).endswith(".tmp"):
                    modes.append(mode)
                return real_open(path, flags, mode, *a, **kw)

            with mock.patch.object(jw.os, "open", side_effect=spy_open):
                jw.save_ledger(led)
            self.assertEqual(modes, [0o600])  # цикл5 С6: сразу 0600, без окна 0644
            p = Path(tmp) / ".join-watch.json"
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
            self.assertEqual(jw.load_ledger()["k"]["attempts"][0]["container"], "c")

    def test_broken_ledger_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MEETING_NOTARY_REGISTRY_DIR": tmp}):
            (Path(tmp) / ".join-watch.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(jw.load_ledger(), {})


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "transcripts"
        self.base.mkdir()
        self.ancient = 1_000_000.0
        self.now_ts = self.ancient + 60 * 86400

    def old(self, *paths):
        for p in paths:
            os.utime(p, (self.ancient, self.ancient), follow_symlinks=False)

    def test_prunes_old_diagnostics_only(self):
        base = self.base
        (base / ".join-status").mkdir()
        (base / ".crash-kits" / "old").mkdir(parents=True)
        (base / ".crash-kits" / "new").mkdir(parents=True)
        old_status = base / ".join-status" / "old.json"
        new_status = base / ".join-status" / "new.json"
        meeting_meta = base / "2026-09-01-tm-1.meta.json"
        for p in (old_status, new_status, meeting_meta, base / ".crash-kits" / "old" / "kit.json",
                  base / ".crash-kits" / "new" / "kit.json"):
            p.write_text("{}", encoding="utf-8")
        self.old(old_status, meeting_meta, base / ".crash-kits" / "old" / "kit.json", base / ".crash-kits" / "old")
        for p in (new_status, base / ".crash-kits" / "new" / "kit.json", base / ".crash-kits" / "new"):
            os.utime(p, (self.now_ts, self.now_ts))
        jw.prune_diagnostics(base, now_ts=self.now_ts)
        self.assertFalse(old_status.exists())
        self.assertTrue(new_status.exists())
        self.assertFalse((base / ".crash-kits" / "old").exists())
        self.assertTrue((base / ".crash-kits" / "new").exists())
        self.assertTrue(meeting_meta.exists())

    def test_symlinks_are_never_followed(self):
        # цикл5 С2: ссылка из папки снимков на протоколы не должна их стереть
        protocols = Path(self.tmp.name) / "protocols" / "series-a"
        protocols.mkdir(parents=True)
        victim = protocols / "2026-08-01.md"
        victim.write_text("протокол", encoding="utf-8")
        self.old(victim, protocols)
        (self.base / ".crash-kits").mkdir()
        os.symlink(protocols, self.base / ".crash-kits" / "evil")
        self.old(self.base / ".crash-kits" / "evil")
        (self.base / ".join-status").mkdir()
        os.symlink(victim, self.base / ".join-status" / "x.json")
        jw.prune_diagnostics(self.base, now_ts=self.now_ts)
        self.assertTrue(victim.exists())

    def test_symlinked_dirs_are_skipped(self):
        protocols = Path(self.tmp.name) / "protocols"
        (protocols / "series-a").mkdir(parents=True)
        victim = protocols / "series-a" / "old.md"
        victim.write_text("протокол", encoding="utf-8")
        self.old(victim, protocols / "series-a")
        os.symlink(protocols, self.base / ".crash-kits")
        jw.prune_diagnostics(self.base, now_ts=self.now_ts)
        self.assertTrue(victim.exists())


if __name__ == "__main__":
    unittest.main()
