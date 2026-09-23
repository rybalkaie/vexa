"""ISS-60: запускатель ↔ сторож входа.

Проверяет:
  * токен Ватсона уходит боту через окружение docker-клиента, а не в командной
    строке (в списке процессов секрета нет);
  * повторная попытка — новый контейнер с суффиксом `-a2`;
  * в BOT_CONFIG — кому пересылать ручную запись (Илье / организатору);
  * старт попадает в журнал сторожа; сорванный вход → повтор → сообщение;
  * упавший сторож не мешает запуску; на маке (режим ssh) сторож инертен.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_runner_join_watch_iss60 -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

import notary.cli.registry as registry_mod  # noqa: E402
import notary.runner as runner  # noqa: E402
from notary.lib import join_watch, state as state_lib  # noqa: E402

TOKEN = "123:SECRET-TOKEN-VALUE"
OWNER = "111"


def _item(start: datetime, **kw):
    it = {
        "meeting_id": "anzhee-direktorat", "event_id": "gcal:e1", "series": "anzhee-direktorat",
        "start_at": start.isoformat(), "duration_minutes": 60,
        "url": "https://telemost.yandex.ru/j/00000000000000", "expected_participants": [],
        "type": "recurring",
    }
    it.update(kw)
    return it


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iss60-"))
        (self.tmp / "registry").mkdir()
        env = {
            "MEETING_NOTARY_REGISTRY_DIR": str(self.tmp / "registry"),
            "MEETING_NOTARY_LOG_DIR": str(self.tmp / "logs"),
            "TELEGRAM_NOTARIUS_BOT_TOKEN": TOKEN,
            "TELEGRAM_NOTARIUS_CHAT_ID": OWNER,
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        os.environ.pop("MEETING_NOTARY_JOIN_WATCH", None)
        self.patches = [
            mock.patch.object(runner, "setup_logging", lambda: None),
            mock.patch.object(runner, "push"),
            mock.patch.object(registry_mod, "WATCHED_FILE", self.tmp / "registry" / "watched.yaml"),
            mock.patch.object(registry_mod, "ROOMS_FILE", self.tmp / "registry" / "rooms.yaml"),
            mock.patch.object(registry_mod, "PAUSE_FILE", self.tmp / "registry" / ".pause-until"),
            mock.patch.object(runner, "RUNS_DIR", self.tmp / "logs" / "runs"),
            mock.patch.object(runner, "LOG_DIR", self.tmp / "logs"),
        ]
        for p in self.patches:
            p.start()
        (self.tmp / "logs" / "runs").mkdir(parents=True)
        (self.tmp / "registry" / "watched.yaml").write_text("watched: []\n", encoding="utf-8")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class LaunchTest(_Env):
    def _launch(self, item, *, attempt=1):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return mock.Mock(returncode=0)

        with mock.patch.object(runner, "LOCAL_DOCKER", True), \
             mock.patch.object(runner.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(runner, "_verify_container_alive"):
            ok = runner._launch_bot(item, datetime.now(timezone.utc), seq=0, attempt=attempt)
        self.assertTrue(ok)
        return calls[0]

    def test_token_goes_via_client_env_not_argv(self):
        cmd, kw = self._launch(_item(datetime.now(timezone.utc)))
        joined = " ".join(cmd)
        self.assertNotIn(TOKEN, joined)
        i = cmd.index("TELEGRAM_BOT_TOKEN")
        self.assertEqual(cmd[i - 1], "-e")
        self.assertIn("TELEGRAM_CHAT_ID", cmd)
        self.assertEqual(kw["env"]["TELEGRAM_BOT_TOKEN"], TOKEN)
        self.assertEqual(kw["env"]["TELEGRAM_CHAT_ID"], OWNER)

    def test_no_token_no_flags(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_NOTARIUS_BOT_TOKEN": ""}):
            cmd, kw = self._launch(_item(datetime.now(timezone.utc)))
        self.assertNotIn("TELEGRAM_BOT_TOKEN", cmd)
        self.assertIsNone(kw.get("env"))

    def test_retry_container_name_and_recipient(self):
        start = datetime.now(timezone.utc)
        cmd, _ = self._launch(_item(start), attempt=2)
        name = cmd[cmd.index("--name") + 1]
        self.assertTrue(name.endswith("-a2"), name)
        cfg = json.loads(next(a for a in cmd if a.startswith("BOT_CONFIG="))[len("BOT_CONFIG="):])
        self.assertEqual(cfg["recordingRecipient"], "Илье")
        self.assertTrue(cfg["sessionUid"].endswith("-a2"))

    def test_portal_recipient_is_organizer(self):
        with mock.patch("notary.lib.orders.get_order", return_value=None):
            cmd, _ = self._launch(_item(datetime.now(timezone.utc), order_id="o1"))
        cfg = json.loads(next(a for a in cmd if a.startswith("BOT_CONFIG="))[len("BOT_CONFIG="):])
        self.assertEqual(cfg["recordingRecipient"], "организатору встречи")

    def test_session_uid_suffix(self):
        it = _item(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))
        base = runner._session_uid_for(it, 1)
        self.assertEqual(runner._session_uid_for(it, 2), base + "-a2")


class WatchWiringTest(_Env):
    def test_inert_on_mac_ssh_mode(self):
        now = datetime.now(timezone.utc)
        with mock.patch.object(runner, "LOCAL_DOCKER", False):
            runner._join_watch_register(_item(now), now, attempt=1)
            with mock.patch.object(join_watch, "load_ledger", side_effect=AssertionError("не должен читаться")):
                runner._join_watch_tick(now, allow_retry=True)
        self.assertFalse((self.tmp / "registry" / ".join-watch.json").exists())

    def test_watch_crash_never_escapes(self):
        with mock.patch.object(runner, "LOCAL_DOCKER", True), \
             mock.patch.object(join_watch, "load_ledger", side_effect=RuntimeError("boom")):
            runner._join_watch_tick(datetime.now(timezone.utc), allow_retry=True)  # не бросает

    def test_kill_switch(self):
        now = datetime.now(timezone.utc)
        with mock.patch.object(runner, "LOCAL_DOCKER", True), \
             mock.patch.dict(os.environ, {"MEETING_NOTARY_JOIN_WATCH": "0"}):
            runner._join_watch_register(_item(now), now, attempt=1)
        self.assertFalse((self.tmp / "registry" / ".join-watch.json").exists())

    def test_end_to_end_failed_join_retry_then_alert(self):
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=4)
        state_lib.write_queue([_item(start)])
        t0 = start - timedelta(minutes=4)
        snapshot = {"by_series": {}, "by_room": {}, "unlabeled": [], "total": 0}
        launches = []

        def fake_launch(item, now, seq=0, attempt=1):
            launches.append(attempt)
            return True

        statuses = {}
        containers = {}
        alerts = []
        with mock.patch.object(runner, "LOCAL_DOCKER", True), \
             mock.patch.object(runner, "_running_bots_snapshot", return_value=snapshot), \
             mock.patch.object(runner, "_launch_bot", side_effect=fake_launch), \
             mock.patch.object(runner, "_jw_inspect", side_effect=lambda n: containers.get(n, join_watch.ContainerState("running"))), \
             mock.patch.object(runner, "_jw_read_status", side_effect=lambda uid: statuses.get(uid)), \
             mock.patch.object(runner, "_jw_save_logs"), \
             mock.patch.object(runner, "_jw_stop"), \
             mock.patch("notary.lib.join_alert.send", side_effect=lambda *a, **k: alerts.append((a, k))):
            self.assertEqual(runner.main(now=t0), 0)
            self.assertEqual(launches, [1])
            led = join_watch.load_ledger()
            self.assertEqual(len(led), 1)
            entry = next(iter(led.values()))
            uid1 = entry["attempts"][0]["session_uid"]

            # попытка 1 сорвалась
            t1 = t0 + timedelta(seconds=70)
            statuses[uid1] = {"phase": "failed", "reason": "name_input_not_found", "updated_at": t0.isoformat()}
            containers["vexa-notarius-" + uid1] = join_watch.ContainerState("exited", 1)
            runner.main(now=t1)
            self.assertEqual(launches, [1, 2])
            self.assertEqual(alerts, [])

            # попытка 2 тоже сорвалась → одно сообщение
            uid2 = uid1 + "-a2"
            t2 = t1 + timedelta(seconds=70)
            statuses[uid2] = {"phase": "failed", "reason": "name_input_not_found", "updated_at": t1.isoformat()}
            containers["vexa-notarius-" + uid2] = join_watch.ContainerState("exited", 1)
            runner.main(now=t2)
            runner.main(now=t2 + timedelta(seconds=60))
        self.assertEqual(launches, [1, 2])
        self.assertEqual(len(alerts), 1)
        (payload, reason), kw = alerts[0][0][:2], alerts[0][1]
        self.assertEqual(reason, "name_input_not_found")
        self.assertEqual(kw["attempts"], 2)
        self.assertEqual(payload["series"], "anzhee-direktorat")


if __name__ == "__main__":
    unittest.main()
