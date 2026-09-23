"""ISS-60: сторож входа бота — решения и проход по журналу.

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


def obs(phase=None, *, reason=None, seen=True, status="running", code=None, age=None, kit=None, fb=None):
    return jw.Observation(
        phase=phase, reason=reason, status_seen=seen,
        container=jw.ContainerState(status, code),
        fallback_used=fb, crash_kit=kit, phase_age_sec=age,
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
        # сразу после in_call бот ещё раз проверяет вход — закрывать рано
        self.assertEqual(decide(obs("in_call", age=3)).action, jw.WAIT)

    def test_in_call_then_exited_is_ok(self):
        self.assertEqual(decide(obs("in_call", age=3, status="exited", code=0)).action, jw.OK)

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
        self.assertEqual(decide(obs("joining", age=100), launched_ago=100).action, jw.WAIT)
        d = decide(obs("joining", age=300), launched_ago=jw.STUCK_JOINING_SEC + 10)
        self.assertEqual((d.action, d.reason), (jw.STOP_RETRY, "stuck_joining"))

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
        self.notes: list = []
        self.relaunch_ok = True

    def hooks(self):
        return jw.Hooks(
            inspect=lambda name: self.containers.get(name, jw.ContainerState("missing")),
            read_status=lambda uid: self.status.get(uid),
            stop=lambda name: self.stopped.append(name),
            relaunch=self._relaunch,
            alert=lambda entry, reason, n, kit: self.alerts.append((reason, n, kit)),
            note_owner=lambda text: self.notes.append(text),
        )

    def _relaunch(self, entry, attempt):
        self.relaunched.append(attempt)
        if not self.relaunch_ok:
            return None
        uid = f"auto-x-a{attempt}"
        return {"session_uid": uid, "container": f"vexa-notarius-{uid}"}


def st(phase, *, at, reason=None, kit=None, fb=None):
    d = {"phase": phase, "updated_at": at.isoformat()}
    if reason:
        d["reason"] = reason
    if kit:
        d["crash_kit"] = kit
    if fb:
        d["fallback_used"] = fb
    return d


class TickTest(unittest.TestCase):
    def setUp(self):
        self.ledger: dict = {}
        self.item = {"meeting_id": "anzhee-direktorat", "series": "anzhee-direktorat",
                     "start_at": START.isoformat(), "url": "https://example.invalid/j/1", "event_id": "e1"}
        jw.register_launch(self.ledger, "k1", self.item, session_uid="auto-x",
                           container="vexa-notarius-auto-x", attempt=1, now=NOW)
        self.w = FakeWorld()

    def run_tick(self, now):
        return jw.tick(self.ledger, now=now, hooks=self.w.hooks())

    def test_failed_retry_then_alert_once(self):
        t1 = NOW + timedelta(seconds=60)
        self.w.status["auto-x"] = st("failed", at=t1 - timedelta(seconds=20), reason="name_input_not_found", kit="/transcripts/.crash-kits/auto-x")
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("exited", 1)
        self.assertTrue(self.run_tick(t1))
        self.assertEqual(self.w.relaunched, [2])
        self.assertEqual(self.w.alerts, [])
        self.assertEqual(len(self.ledger["k1"]["attempts"]), 2)

        t2 = t1 + timedelta(seconds=60)
        self.w.status["auto-x-a2"] = st("failed", at=t2 - timedelta(seconds=10), reason="name_input_not_found")
        self.w.containers["vexa-notarius-auto-x-a2"] = jw.ContainerState("exited", 1)
        self.run_tick(t2)
        self.assertEqual(self.w.alerts, [("name_input_not_found", 2, None)])
        self.assertEqual(self.ledger["k1"]["state"], "alerted")

        # следующий тик — повторного сообщения нет
        self.run_tick(t2 + timedelta(seconds=60))
        self.assertEqual(len(self.w.alerts), 1)
        self.assertEqual(self.w.stopped, [])  # вышедших не останавливаем

    def test_no_slot_for_retry_alerts_with_original_reason(self):
        self.w.relaunch_ok = False
        t1 = NOW + timedelta(seconds=60)
        self.w.status["auto-x"] = st("failed", at=t1, reason="still_in_lobby")
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("exited", 1)
        self.run_tick(t1)
        self.assertEqual(self.w.alerts, [("still_in_lobby", 1, None)])

    def test_success_on_second_try_notes_owner_quietly(self):
        t1 = NOW + timedelta(seconds=60)
        self.w.status["auto-x"] = st("failed", at=t1, reason="still_in_lobby")
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("exited", 1)
        self.run_tick(t1)
        t2 = t1 + timedelta(seconds=90)
        self.w.status["auto-x-a2"] = st("in_call", at=t2 - timedelta(seconds=40))
        self.w.containers["vexa-notarius-auto-x-a2"] = jw.ContainerState("running")
        self.run_tick(t2)
        self.assertEqual(self.ledger["k1"]["state"], "ok")
        self.assertEqual(self.w.alerts, [])
        self.assertEqual(len(self.w.notes), 1)
        self.assertIn("второй попытки", self.w.notes[0])

    def test_ok_first_try_is_silent(self):
        t1 = NOW + timedelta(seconds=90)
        self.w.status["auto-x"] = st("in_call", at=NOW + timedelta(seconds=40))
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.run_tick(t1)
        self.assertEqual(self.ledger["k1"]["state"], "ok")
        self.assertEqual((self.w.alerts, self.w.notes), ([], []))

    def test_fallback_join_notes_owner(self):
        t1 = NOW + timedelta(seconds=90)
        self.w.status["auto-x"] = st("in_call", at=NOW + timedelta(seconds=40), fb={"join": "по тексту кнопки"})
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.run_tick(t1)
        self.assertEqual(len(self.w.notes), 1)
        self.assertIn("запасным путём", self.w.notes[0])

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

    def test_hooks_errors_do_not_escape(self):
        hooks = self.w.hooks()
        hooks.inspect = mock.Mock(side_effect=RuntimeError("docker down"))
        jw.tick(self.ledger, now=NOW + timedelta(seconds=30), hooks=hooks)  # не бросает
        self.assertEqual(self.ledger["k1"]["state"], "watching")

    def test_ledger_ttl(self):
        jw.tick(self.ledger, now=NOW + timedelta(hours=jw.LEDGER_TTL_HOURS + 1), hooks=self.w.hooks())
        self.assertNotIn("k1", self.ledger)

    def test_no_retry_when_paused(self):
        t1 = NOW + timedelta(seconds=60)
        self.w.status["auto-x"] = st("failed", at=t1, reason="name_input_not_found")
        self.w.containers["vexa-notarius-auto-x"] = jw.ContainerState("exited", 1)
        jw.tick(self.ledger, now=t1, hooks=self.w.hooks(), allow_retry=False)
        self.assertEqual(self.w.relaunched, [])
        self.assertEqual(len(self.w.alerts), 1)


class LedgerIoTest(unittest.TestCase):
    def test_roundtrip_in_registry_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MEETING_NOTARY_REGISTRY_DIR": tmp}):
            led = jw.load_ledger()
            self.assertEqual(led, {})
            jw.register_launch(led, "k", {"meeting_id": "m", "start_at": START.isoformat(), "url": "u"},
                               session_uid="s", container="c", attempt=1, now=NOW)
            jw.save_ledger(led)
            p = Path(tmp) / ".join-watch.json"
            self.assertTrue(p.exists())
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
            self.assertEqual(jw.load_ledger()["k"]["attempts"][0]["container"], "c")

    def test_broken_ledger_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MEETING_NOTARY_REGISTRY_DIR": tmp}):
            (Path(tmp) / ".join-watch.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(jw.load_ledger(), {})


class PruneTest(unittest.TestCase):
    def test_prunes_old_diagnostics_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / ".join-status").mkdir()
            (base / ".crash-kits" / "old").mkdir(parents=True)
            (base / ".crash-kits" / "new").mkdir(parents=True)
            old_status = base / ".join-status" / "old.json"
            new_status = base / ".join-status" / "new.json"
            meeting_meta = base / "2026-09-01-tm-1.meta.json"
            for p in (old_status, new_status, meeting_meta, base / ".crash-kits" / "old" / "kit.json",
                      base / ".crash-kits" / "new" / "kit.json"):
                p.write_text("{}", encoding="utf-8")
            ancient = 1_000_000.0
            for p in (old_status, meeting_meta, base / ".crash-kits" / "old" / "kit.json", base / ".crash-kits" / "old"):
                os.utime(p, (ancient, ancient))
            now_ts = ancient + 60 * 86400
            for p in (new_status, base / ".crash-kits" / "new" / "kit.json", base / ".crash-kits" / "new"):
                os.utime(p, (now_ts, now_ts))
            jw.prune_diagnostics(base, now_ts=now_ts)
            self.assertFalse(old_status.exists())
            self.assertTrue(new_status.exists())
            self.assertFalse((base / ".crash-kits" / "old").exists())
            self.assertTrue((base / ".crash-kits" / "new").exists())
            self.assertTrue(meeting_meta.exists())  # записи встреч не трогаем никогда


if __name__ == "__main__":
    unittest.main()
