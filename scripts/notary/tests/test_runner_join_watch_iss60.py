"""ISS-60: запускатель ↔ сторож входа.

Проверяет:
  * токен Ватсона в контейнер бота НЕ передаётся (браузер открывает чужие страницы);
  * повторная попытка — новый контейнер с суффиксом `-a2` и своим каналом команд;
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
from notary.lib import join_watch, late_join, state as state_lib  # noqa: E402

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

    def test_no_telegram_token_in_bot_container(self):
        # цикл5 С1: токен Ватсона в контейнер с браузером не передаётся
        cmd, kw = self._launch(_item(datetime.now(timezone.utc)))
        joined = " ".join(cmd)
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", joined)
        self.assertNotIn("TELEGRAM_CHAT_ID", joined)
        self.assertIsNone(kw.get("env"))

    def test_retry_container_name(self):
        start = datetime.now(timezone.utc)
        cmd, _ = self._launch(_item(start), attempt=2)
        name = cmd[cmd.index("--name") + 1]
        self.assertTrue(name.endswith("-a2"), name)
        cfg = json.loads(next(a for a in cmd if a.startswith("BOT_CONFIG="))[len("BOT_CONFIG="):])
        self.assertTrue(cfg["sessionUid"].endswith("-a2"))
        self.assertNotIn("recordingRecipient", cfg)

    def test_session_uid_suffix(self):
        it = _item(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))
        base = runner._session_uid_for(it, 1)
        self.assertEqual(runner._session_uid_for(it, 2), base + "-a2")


class VerifyAliveTest(_Env):
    """Фаза 3а: после `exec node` в entrypoint.sh docker stop даёт боту выйти
    штатно (код 143) — это внешняя остановка, как 137, а не падение бота."""

    def _alerts_for(self, inspect_line):
        def fake_run(cmd, **kw):
            if "inspect" in cmd:
                return mock.Mock(returncode=0, stdout=inspect_line + "\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        runner.push.reset_mock()
        with mock.patch.object(runner, "LOCAL_DOCKER", True), \
             mock.patch.object(runner.time, "sleep"), \
             mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            runner._verify_container_alive("vexa-notarius-x", self.tmp / "logs" / "run.log",
                                           "anzhee-direktorat", "2026-09-28T08:00:00+00:00")
        return runner.push.call_count

    def test_external_stop_is_silent(self):
        for code in ("137", "143"):
            self.assertEqual(self._alerts_for(f"exited|{code}"), 0, code)

    def test_crash_still_alerts(self):
        self.assertEqual(self._alerts_for("exited|1"), 1)


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

    def test_retry_gets_own_command_channel(self):
        # цикл5 Н3: у повтора свой meeting_id — команда `leave` не уйдёт двум ботам
        seqs = []
        snapshot = {"by_series": {}, "by_room": {}, "unlabeled": [], "total": 0}
        entry = {"item": _item(datetime.now(timezone.utc)), "series": "anzhee-direktorat"}
        with mock.patch.object(runner, "_running_bots_snapshot", return_value=snapshot), \
             mock.patch.object(runner, "_launch_bot", side_effect=lambda item, now, seq=0, attempt=1: seqs.append(seq) or True):
            self.assertTrue(runner._jw_relaunch(entry, 2))
            self.assertTrue(runner._jw_relaunch(entry, 2))
        self.assertEqual(len(set(seqs)), 2)
        self.assertTrue(all(s >= runner._RETRY_SEQ_BASE for s in seqs))

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


class LateJoinOnRetryTest(_Env):
    """Сквозной цикл5 ISS-60: сторож (фаза 1) × подпись опоздания (Ф10b).

    Удачный повтор входа заходит на минуты позже. Пришёлся он на время после
    начала встречи — первых минут в записи нет, и протокол обязан это сказать."""

    START = datetime(2026, 9, 28, 11, 0, tzinfo=timezone.utc)  # 14:00 МСК
    SERIES = "anzhee-direktorat"

    def _entry(self, start_at):
        item = _item(self.START)
        item["start_at"] = start_at
        return {"item": item, "series": self.SERIES, "start_at": start_at}

    def _signed(self):
        return late_join.sign("#протоколвстречи\n\n## Итоги\n", self.SERIES, "2026-09-28")

    def test_retry_after_start_is_signed_late(self):
        runner._jw_joined_on_retry(self._entry(self.START.isoformat()), 2, self.START + timedelta(minutes=6))
        mark = late_join.get(self.SERIES, "2026-09-28")
        self.assertIsNotNone(mark, "удачный повтор после начала встречи обязан оставить отметку")
        self.assertEqual((mark["reason"], mark["late_minutes"]), (late_join.REASON_JOIN_RETRY, 6))
        signed = self._signed()
        self.assertIn("с опозданием на 6 мин", signed)
        self.assertIn("первая попытка войти во встречу сорвалась", signed)
        self.assertIn("плановое начало 14:00 МСК, фактический заход 14:06 МСК", signed)

    def test_calendar_start_in_moscow_offset_shows_same_clock(self):
        # Календарь отдаёт начало с поясом +03:00, заход сторож пишет в UTC — раньше
        # подпись резала строки как есть: «начало 14:00, заход 11:06».
        runner._jw_joined_on_retry(self._entry("2026-09-28T14:00:00+03:00"), 2,
                                   self.START + timedelta(minutes=6))
        self.assertIn("плановое начало 14:00 МСК, фактический заход 14:06 МСК", self._signed())

    def test_retry_before_start_is_not_marked(self):
        runner._jw_joined_on_retry(self._entry(self.START.isoformat()), 2, self.START - timedelta(minutes=1))
        self.assertIsNone(late_join.get(self.SERIES, "2026-09-28"))
        self.assertEqual(self._signed(), "#протоколвстречи\n\n## Итоги\n")

    def test_end_to_end_retry_after_start_marks_protocol(self):
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=4)
        state_lib.write_queue([_item(start)])
        t0 = start - timedelta(minutes=4)
        snapshot = {"by_series": {}, "by_room": {}, "unlabeled": [], "total": 0}
        statuses, containers = {}, {}
        with mock.patch.object(runner, "LOCAL_DOCKER", True), \
             mock.patch.object(runner, "_running_bots_snapshot", return_value=snapshot), \
             mock.patch.object(runner, "_launch_bot", return_value=True), \
             mock.patch.object(runner, "_jw_inspect", side_effect=lambda n: containers.get(n, join_watch.ContainerState("running"))), \
             mock.patch.object(runner, "_jw_read_status", side_effect=lambda uid: statuses.get(uid)), \
             mock.patch.object(runner, "_jw_save_logs"), \
             mock.patch.object(runner, "_jw_stop"), \
             mock.patch.object(runner, "_canary_warn_tick"), \
             mock.patch("notary.lib.join_alert.send"):
            runner.main(now=t0)
            uid1 = next(iter(join_watch.load_ledger().values()))["attempts"][0]["session_uid"]
            # попытка 1 зависла в ожидании впуска — сторож остановил её уже после начала
            t1 = start + timedelta(minutes=3)
            statuses[uid1] = {"phase": "failed", "reason": "admission_unclear", "updated_at": (t0 + timedelta(minutes=5)).isoformat()}
            containers["vexa-notarius-" + uid1] = join_watch.ContainerState("exited", 0)
            runner.main(now=t1)
            joined = start + timedelta(minutes=4, seconds=30)
            statuses[uid1 + "-a2"] = {"phase": "in_call", "updated_at": joined.isoformat(),
                                      "history": [{"phase": "in_call", "at": joined.isoformat()}]}
            runner.main(now=start + timedelta(minutes=5, seconds=10))
        mark = late_join.get(self.SERIES, str(_item(start)["start_at"])[:10])
        self.assertIsNotNone(mark, "сторож и запускатель не связаны: повтор после начала без отметки")
        self.assertEqual((mark["reason"], mark["late_minutes"]), (late_join.REASON_JOIN_RETRY, 4))


if __name__ == "__main__":
    unittest.main()
