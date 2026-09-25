"""ISS-60, фаза 2: самопроверка бота в Т-10, «нюх» версии Телемоста, предупреждение
перед встречами.

Docker, redis, ffmpeg, whisper и Телеграм подменены «игрушечным миром» (`World`):
он по сценарию изображает контейнеры диктора и бота — пишет их файлы-статусы в
папку прогона так же, как это делают настоящие speaker.js и бот.

Запуск:
    cd ~/Projects/meeting-notary/vexa-resilience/scripts/notary
    python3 -m unittest tests.test_canary_iss60 -v
"""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.lib import canary as lib  # noqa: E402

KIT = _HERE.parent / "canary-kit"
FAKE_URL = "https://telemost.yandex.ru/j/00000000000000"
TOKEN = "123:SECRET-TOKEN-VALUE"
NIGHT = datetime(2026, 9, 26, 1, 40, tzinfo=timezone.utc)   # 04:40 МСК
DAY = datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)      # 10:00 МСК


def _words():
    data = json.loads((KIT / "phrases" / "phrases.json").read_text(encoding="utf-8"))
    return {p["file"]: p["word"] for p in data["phrases"]}


# ---------------------------------------------------------------------------
# Игрушечный мир docker
# ---------------------------------------------------------------------------

DEFAULT_RUN = dict(
    speaker_result="alone",        # alone | busy | failed | none
    speaker_reason="name_input_not_found",
    speaker_join_sec=12,
    bot_result="in_call",          # in_call | failed | none
    bot_reason="still_in_lobby",
    bot_join_sec=30,
    spoke_sec=20,
    mic_ui="on",
    busy_after_bot=False,
    wav=True,
    duration=45.0,
    volume=(-32.0, -8.0),
    transcript="Это автоматическая проверка бота-протоколиста. Кодовое слово: {word}. Повторяю: {word}.",
    transcribe_error=False,
    participants=[],
    speaker_start_ok=True,
    bot_start_ok=True,
)


class World:
    def __init__(self, base=NIGHT, runs=None, queue=None, running=None, room_url=FAKE_URL, **common):
        self.base = base
        self.t = 0.0
        self.runs = [dict(DEFAULT_RUN, **common, **r) for r in (runs or [{}])]
        self.queue = queue or []
        self.running = running or []
        self.room_url_value = room_url
        self.containers = {}
        self.docker_cmds = []
        self.published = []
        self.stopped = []
        self.removed = []
        self.pushes = []
        self.sleeps = []
        self.transcribed = 0
        self.words = _words()
        self.run_idx = -1
        self.last_word = "нет"

    # --- время
    def now(self):
        return self.base + timedelta(seconds=self.t)

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        self.step()

    def cfg(self):
        return self.runs[min(self.run_idx, len(self.runs) - 1)]

    # --- docker
    def docker_run(self, cmd):
        self.docker_cmds.append(cmd)
        name = cmd[cmd.index("--name") + 1]
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        run_dir = Path(next(m for m in mounts if m.endswith(":/transcripts")).rsplit(":", 1)[0])
        kind = "speaker" if "-speaker-" in name else "bot"
        if kind == "speaker":
            self.run_idx += 1
        cfg = self.cfg()
        if not cfg[f"{kind}_start_ok"]:
            return False, f"docker: cannot start {FAKE_URL}"
        env = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in cmd if "=" in a and a.split("=", 1)[0].isupper()}
        c = {"kind": kind, "status": "running", "code": None, "started": self.t, "run_dir": run_dir,
             "cfg": cfg, "leave": False}
        if kind == "speaker":
            sc = json.loads(env["SPEAKER_CONFIG"])
            c["word"] = self.last_word = self.words[sc["phraseFile"]]
            c["uid"] = json.loads(env["BOT_CONFIG"])["sessionUid"]
        else:
            bc = json.loads(env["BOT_CONFIG"])
            c["meeting_id"] = bc["meeting_id"]
            c["uid"] = bc["sessionUid"]
        self.containers[name] = c
        self.step()
        return True, ""

    def container_state(self, name):
        c = self.containers.get(name)
        return (c["status"], c["code"]) if c else ("missing", None)

    def running_containers(self):
        return list(self.running)

    def leftovers(self):
        return []

    def stop(self, name):
        self.stopped.append(name)
        c = self.containers.get(name)
        if c and c["status"] == "running":
            c["status"], c["code"] = "exited", 137

    def remove(self, name):
        self.removed.append(name)
        self.containers.pop(name, None)

    def logs(self, name):
        return f'{{"msg": "step=join_start url=\\"{FAKE_URL}\\" name=x"}}\nok\n'

    def publish(self, channel, message):
        self.published.append((channel, message))
        for c in self.containers.values():
            if c["kind"] == "bot" and channel == f"bot_commands:meeting:{c['meeting_id']}":
                c["leave"] = True
                return True
        return False

    # --- звук
    def probe(self, p):
        return self.cfg()["duration"]

    def volume(self, p):
        return self.cfg()["volume"]

    def transcribe(self, p):
        self.transcribed += 1
        cfg = self.cfg()
        if cfg["transcribe_error"]:
            raise RuntimeError("whisper down")
        return cfg["transcript"].format(word=self.last_word)

    def notify(self, text, silent):
        self.pushes.append((text, silent))
        return True

    # --- ход мира
    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def step(self):
        items = sorted(self.containers.items(), key=lambda kv: kv[1]["kind"] != "bot")  # бот первым
        for name, c in items:
            if c["status"] != "running":
                continue
            if c["kind"] == "bot":
                self._bot(c)
            else:
                self._speaker(c)

    def _bot_in_call(self, run_dir):
        for c in self.containers.values():
            if c["kind"] == "bot" and c["run_dir"] == run_dir:
                return self._read(run_dir / ".join-status" / f"{c['uid']}.json").get("phase") == "in_call"
        return False

    def _speaker(self, c):
        cfg, d = c["cfg"], c["run_dir"]
        sp_path = d / ".canary" / "speaker.json"
        sp = self._read(sp_path)
        ph = sp.get("phase")
        cmd = self._read(d / ".canary" / "cmd.json")
        age = self.t - c["started"]
        if cmd.get("action") == "leave" and ph not in ("busy", "failed", "left"):
            self._write(sp_path, {**sp, "phase": "left", "reason": "command"})
            c["status"], c["code"] = "exited", 0
            return
        if ph is None and age >= cfg["speaker_join_sec"]:
            r = cfg["speaker_result"]
            if r == "alone":
                self._write(sp_path, {"phase": "alone", "others": 0, "count_method": "button+tiles"})
            elif r == "busy":
                self._write(sp_path, {"phase": "busy", "reason": "strangers", "others": 1})
                c["status"], c["code"] = "exited", 0
            elif r == "failed":
                kit = f"/transcripts/.crash-kits/{c['uid']}"
                (d / ".crash-kits" / c["uid"]).mkdir(parents=True, exist_ok=True)
                self._write(sp_path, {"phase": "failed", "reason": cfg["speaker_reason"], "crash_kit": kit})
                c["status"], c["code"] = "exited", 1
            return
        if ph == "alone":
            if cfg["busy_after_bot"] and self._bot_in_call(d):
                self._write(sp_path, {**sp, "phase": "busy", "reason": "strangers", "others": 2})
                c["status"], c["code"] = "exited", 0
                return
            if cmd.get("action") == "speak" and cfg.get("speak_fails"):
                self._write(sp_path, {**sp, "phase": "failed", "reason": cfg["speak_fails"], "step": "speak"})
                c["status"], c["code"] = "exited", 1
                return
            if cmd.get("action") == "speak":
                c.setdefault("speak_at", self.t)
                if self.t - c["speak_at"] >= cfg["spoke_sec"]:
                    self._write(sp_path, {**sp, "phase": "spoke", "mic_ui": cfg["mic_ui"], "plays_done": 3})

    def _bot(self, c):
        cfg, d = c["cfg"], c["run_dir"]
        st_path = d / ".join-status" / f"{c['uid']}.json"
        st = self._read(st_path)
        if c["leave"]:
            c["status"], c["code"] = "exited", 0
            return
        if st.get("phase") is None and self.t - c["started"] >= cfg["bot_join_sec"]:
            r = cfg["bot_result"]
            if r == "in_call":
                self._write(st_path, {"phase": "in_call", "history": [
                    {"phase": "joining"}, {"phase": "admission"}, {"phase": "in_call"}]})
                if cfg["wav"]:
                    (d / "2026-09-26-tm-1.chunk1.wav").write_bytes(b"RIFF....WAVE")
                    self._write(d / "2026-09-26-tm-1.meta.json",
                                {"participants": cfg["participants"], "meetingUrl": FAKE_URL})
            elif r == "failed":
                kit = f"/transcripts/.crash-kits/{c['uid']}"
                (d / ".crash-kits" / c["uid"]).mkdir(parents=True, exist_ok=True)
                self._write(st_path, {"phase": "failed", "reason": cfg["bot_reason"], "crash_kit": kit,
                                      "history": [{"phase": "joining"}, {"phase": "failed"}]})
                c["status"], c["code"] = "exited", 1

    # --- набор зависимостей
    def deps(self):
        return lib.Deps(
            docker_run=self.docker_run,
            container_state=self.container_state,
            containers=self.running_containers,
            canary_leftovers=self.leftovers,
            stop=self.stop,
            remove=self.remove,
            logs=self.logs,
            publish=self.publish,
            probe_duration=self.probe,
            volume=self.volume,
            transcribe=self.transcribe,
            read_queue=lambda: list(self.queue),
            room_url=lambda room: self.room_url_value if room == "t10" else None,
            notify=self.notify,
            now=self.now,
            sleep=self.sleep,
            rng=random.Random(7),
        )


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="canary-"))
        self._env = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_CANARY_STATE_DIR": str(self.tmp / "state"),
            "MEETING_NOTARY_CANARY_DIR": str(self.tmp / "canary"),
            "MEETING_NOTARY_CANARY_KIT_DIR": str(KIT),
            "TELEGRAM_NOTARIUS_BOT_TOKEN": TOKEN,
            "TELEGRAM_BOT_TOKEN": TOKEN,
        })
        self._env.start()
        os.environ.pop("MEETING_NOTARY_CANARY_WARN", None)

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cycle(self, world, **kw):
        return lib.run_cycle(deps=world.deps(), image="vexa-bot:resil-test", room="t10",
                             reason=kw.pop("reason", "daily"), **kw)


# ---------------------------------------------------------------------------
# Оценка
# ---------------------------------------------------------------------------


def ev(**kw):
    base = dict(bot_phase="in_call", bot_phases_seen=("joining", "in_call"), wav_durations=(40.0,),
                volume=(-30.0, -6.0), code_word_found=True, participants=("Тест-диктор",))
    base.update(kw)
    return lib.Evidence(**base)


class EvaluateTest(unittest.TestCase):
    def test_all_ok_is_green(self):
        r = lib.evaluate(ev())
        self.assertEqual((r["status"], r["reasons"]), (lib.GREEN, []))

    def test_not_joined_is_red(self):
        r = lib.evaluate(ev(bot_phase="failed", bot_phases_seen=("joining", "in_call", "failed")))
        self.assertEqual(r["status"], lib.RED)
        self.assertEqual(r["reasons"][0], "bot_not_joined")

    def test_never_in_call_is_red(self):
        r = lib.evaluate(ev(bot_phase="admission", bot_phases_seen=("joining", "admission")))
        self.assertEqual(r["status"], lib.RED)

    def test_no_wav_is_red(self):
        r = lib.evaluate(ev(wav_durations=(), volume=None, code_word_found=None))
        self.assertEqual(r["status"], lib.RED)
        self.assertIn("no_recording", r["reasons"])

    def test_short_recording_is_red(self):
        r = lib.evaluate(ev(wav_durations=(4.0, 3.0)))
        self.assertEqual(r["status"], lib.RED)
        self.assertEqual(r["reasons"], ["recording_too_short"])
        self.assertEqual(r["details"]["recording_too_short"], 7.0)

    def test_several_chunks_sum_up(self):
        self.assertEqual(lib.evaluate(ev(wav_durations=(6.0, 5.0)))["status"], lib.GREEN)

    def test_digital_silence_is_red(self):
        r = lib.evaluate(ev(volume=(-91.0, -91.0), code_word_found=False))
        self.assertEqual(r["status"], lib.RED)
        self.assertEqual(r["reasons"], ["silence"])  # корень — тишина, не «нет слова»

    def test_click_in_silence_is_red(self):
        # одиночный щелчок: пик громкий, в среднем пусто
        r = lib.evaluate(ev(volume=(-80.0, -12.0)))
        self.assertEqual(r["status"], lib.RED)
        self.assertIn("silence", r["reasons"])

    def test_quiet_peak_is_red(self):
        self.assertEqual(lib.evaluate(ev(volume=(-50.0, -40.0)))["status"], lib.RED)

    def test_sound_thresholds_boundary(self):
        self.assertEqual(lib.evaluate(ev(volume=(lib.SOUND_MEAN_DB, lib.SOUND_MAX_DB)))["status"], lib.GREEN)

    def test_no_code_word_is_red(self):
        r = lib.evaluate(ev(code_word_found=False))
        self.assertEqual((r["status"], r["reasons"]), (lib.RED, ["code_word_missing"]))

    def test_transcribe_failed_is_yellow(self):
        r = lib.evaluate(ev(code_word_found=None))
        self.assertEqual((r["status"], r["reasons"]), (lib.YELLOW, ["transcribe_failed"]))

    def test_names_missing_is_yellow(self):
        r = lib.evaluate(ev(participants=()))
        self.assertEqual((r["status"], r["reasons"]), (lib.YELLOW, ["names_missing"]))

    def test_no_meta_is_yellow(self):
        self.assertEqual(lib.evaluate(ev(participants=None))["status"], lib.YELLOW)

    def test_name_variants(self):
        self.assertTrue(lib.names_contain(["Тест\u2011диктор (вы)"]))
        self.assertTrue(lib.names_contain(["тест диктор"]))
        self.assertFalse(lib.names_contain(["Бот — протокол встречи"]))

    def test_red_beats_yellow(self):
        r = lib.evaluate(ev(code_word_found=False, participants=()))
        self.assertEqual(r["status"], lib.RED)
        self.assertEqual(r["reasons"], ["code_word_missing", "names_missing"])


class CodeWordTest(unittest.TestCase):
    def test_all_kit_words_found_in_own_phrase(self):
        for word in _words().values():
            text = f"Это автоматическая проверка бота-протоколиста. Кодовое слово: {word}. Повторяю: {word}."
            self.assertTrue(lib.code_word_found(text, word), word)

    def test_inflection_and_yo(self):
        self.assertTrue(lib.code_word_found("Про черепахи и самоваре", "черепаха"))
        self.assertTrue(lib.code_word_found("СВЕТОФОРЫ", "светофор"))
        self.assertTrue(lib.code_word_found("ёлка", "елка"))

    def test_other_word_not_found(self):
        self.assertFalse(lib.code_word_found("кодовое слово самовар", "кенгуру"))
        self.assertFalse(lib.code_word_found("", "кенгуру"))

    def test_volumedetect_parse(self):
        err = "[Parsed_volumedetect_0 @ 0x] mean_volume: -31.4 dB\n[Parsed_volumedetect_0 @ 0x] max_volume: -6.0 dB\n"
        self.assertEqual(lib.parse_volumedetect(err), (-31.4, -6.0))
        self.assertIsNone(lib.parse_volumedetect("garbage"))


# ---------------------------------------------------------------------------
# Номера, метки, команды docker
# ---------------------------------------------------------------------------


class DockerCommandTest(unittest.TestCase):
    def plan(self, run_id="canary-20260926T014000Z"):
        return lib.RunPlan(run_id=run_id, run_dir=Path("/tmp/c") / run_id, image="img:tag", room="t10",
                           meeting_url=FAKE_URL, phrase_file="samovar.wav", code_word="самовар",
                           meeting_id=lib.canary_meeting_id(NIGHT), kit=KIT)

    def test_meeting_id_range_never_overlaps_runner(self):
        import notary.runner as runner

        for now in (NIGHT, datetime(2030, 1, 1, tzinfo=timezone.utc), datetime(2099, 12, 31, tzinfo=timezone.utc)):
            ts = int(now.timestamp())
            runner_ids = {ts + seq for seq in range(0, runner._RETRY_SEQ_BASE + 500)}
            cid = lib.canary_meeting_id(now)
            self.assertNotIn(cid, runner_ids)
            self.assertGreater(cid, max(runner_ids))
            self.assertLess(cid, 2 ** 53)  # JS хранит точно

    def test_meeting_id_differs_between_runs(self):
        a = lib.canary_meeting_id(NIGHT)
        b = lib.canary_meeting_id(NIGHT + timedelta(minutes=4))
        self.assertNotEqual(a, b)

    def test_constants_match_runner(self):
        import notary.runner as runner

        self.assertEqual(lib.DOCKER_NETWORK, runner.DOCKER_NETWORK)
        self.assertEqual(lib.TRANSCRIPTION_URL, runner.TRANSCRIPTION_URL)
        self.assertEqual(lib.DEFAULT_IMAGE, runner.BOT_IMAGE)
        self.assertEqual(lib.NOTARIUS_NAME_PREFIX, runner.NOTARIUS_NAME_PREFIX)

    def test_both_containers_labeled_canary_and_no_telegram_token(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_NOTARIUS_BOT_TOKEN": TOKEN, "TELEGRAM_BOT_TOKEN": TOKEN}):
            for cmd in (lib.speaker_docker_cmd(self.plan()), lib.bot_docker_cmd(self.plan())):
                joined = " ".join(cmd)
                self.assertIn("meeting-notary.role=canary", joined)
                self.assertNotIn("meeting-notary.role=notarius", joined)
                self.assertNotIn(TOKEN, joined)
                self.assertNotIn("TELEGRAM", joined)
                name = cmd[cmd.index("--name") + 1]
                self.assertTrue(name.startswith("vexa-canary-"))
                # запускатель считает живых ботов фильтром name=vexa-notarius- (подстрока)
                self.assertNotIn("vexa-notarius-", name)
                self.assertIn("/tmp/c/canary-20260926T014000Z:/transcripts", cmd)
                self.assertNotIn("/home/dev/meeting-notary/_tmp/transcripts:/transcripts", cmd)

    def test_bot_config_like_runner(self):
        cmd = lib.bot_docker_cmd(self.plan())
        cfg = json.loads(next(a for a in cmd if a.startswith("BOT_CONFIG="))[len("BOT_CONFIG="):])
        self.assertEqual(cfg["botName"], "Бот — протокол встречи")
        self.assertEqual(cfg["series"], "canary")
        self.assertEqual(cfg["platform"], "yandex_telemost")
        self.assertIsInstance(cfg["meeting_id"], int)
        self.assertIn("ENABLE_LIVE_DRAFT=0", cmd)
        self.assertEqual(cmd[-1], "img:tag")

    def test_speaker_cmd(self):
        cmd = lib.speaker_docker_cmd(self.plan())
        self.assertEqual(cmd[-2:], ["img:tag", "/canary/speaker.sh"])
        self.assertIn(f"{KIT}:/canary:ro", cmd)
        sc = json.loads(next(a for a in cmd if a.startswith("SPEAKER_CONFIG="))[len("SPEAKER_CONFIG="):])
        self.assertEqual(sc["phraseFile"], "samovar.wav")
        self.assertGreaterEqual(sc["maxSeconds"], lib.SPEAKER_ALONE_TIMEOUT_SEC + lib.BOT_IN_CALL_TIMEOUT_SEC)
        bc = json.loads(next(a for a in cmd if a.startswith("BOT_CONFIG="))[len("BOT_CONFIG="):])
        self.assertEqual(bc["sessionUid"], "canary-20260926T014000Z-speaker")

    def test_room_label_and_redact(self):
        self.assertEqual(lib.room_label("t10"), "Т-10")
        text = f'url="{FAKE_URL}" и ещё https://telemost.360.yandex.ru/j/123?x=1 конец'
        red = lib.redact(text, FAKE_URL)
        self.assertNotIn("telemost", red)
        self.assertIn("<комната>", red)


# ---------------------------------------------------------------------------
# Полный цикл
# ---------------------------------------------------------------------------


class CycleTest(_Env):
    def run_files(self):
        return sorted((self.tmp / "state" / "runs").glob("*.json"))

    def test_green_run(self):
        w = World(runs=[{"participants": ["Тест-диктор"]}])
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.GREEN, res)
        self.assertEqual(w.pushes, [])
        h = lib.load_health()
        self.assertEqual(h["status"], lib.GREEN)
        self.assertTrue(h["last_green_at"])
        # записи прогона НЕ удаляются
        run_dir = Path(res["run_dir"])
        self.assertTrue((run_dir / "2026-09-26-tm-1.chunk1.wav").exists())
        self.assertTrue((run_dir / "logs" / "bot.log").exists())
        # бот ушёл по команде leave в свой канал, оба контейнера убраны
        self.assertEqual(w.published, [(f"bot_commands:meeting:{res['meeting_id']}", '{"action": "leave"}')])
        self.assertEqual(len(w.removed), 2)
        self.assertEqual(w.containers, {})
        self.assertEqual(len(self.run_files()), 1)
        self.assertEqual(res["checks"]["code_word"], True)

    def test_empty_participants_is_yellow_first_time_quiet(self):
        w = World()  # participants=[] — как в Телемосте 3.0 сейчас
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.YELLOW)
        self.assertEqual(len(w.pushes), 1)
        text, silent = w.pushes[0]
        self.assertTrue(text.startswith("🟡"))
        self.assertTrue(silent)
        w2 = World(base=NIGHT + timedelta(days=1))
        self.cycle(w2)
        self.assertEqual(w2.pushes, [])  # жёлтый повторно — без сообщений

    def test_red_red_alerts_once_after_retry(self):
        bad = {"bot_result": "failed", "bot_reason": "name_input_not_found"}
        w = World(runs=[bad, bad])
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.RED)
        self.assertIn(lib.RETRY_PAUSE_SEC, w.sleeps)
        self.assertEqual(len(self.run_files()), 2)
        self.assertEqual(len(w.pushes), 1)
        text, silent = w.pushes[0]
        self.assertTrue(text.startswith("🔴 Самопроверка бота в Т-10 не прошла (дважды)"), text)
        self.assertIn("не нашёл поле для имени", text)
        self.assertIn("bot_join_failed: name_input_not_found", text)
        self.assertIn("Снимок: ", text)
        self.assertIn(".crash-kits/", text)
        self.assertTrue(silent)  # 04:40 МСК — ночь
        self.assertEqual(lib.load_health()["status"], lib.RED)

    def test_red_then_green_no_alert_flake_logged(self):
        bad = {"bot_result": "failed", "bot_reason": "still_in_lobby"}
        w = World(runs=[bad, {"participants": ["Тест-диктор"]}])
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.GREEN)
        self.assertEqual(w.pushes, [])
        h = lib.load_health()
        self.assertEqual(h["status"], lib.GREEN)
        self.assertEqual(h["last_flake"]["reasons"], ["bot_join_failed"])
        journal = (self.tmp / "state" / "journal.jsonl").read_text(encoding="utf-8")
        self.assertIn("разовый сбой", journal)

    def test_daytime_red_is_loud(self):
        bad = {"speaker_result": "failed", "speaker_reason": "join_button_not_found"}
        w = World(base=DAY, runs=[bad, bad])
        self.cycle(w, force=True)
        self.assertEqual(len(w.pushes), 1)
        text, silent = w.pushes[0]
        self.assertFalse(silent)
        self.assertIn("диктор не вошёл кодом бота", text)
        self.assertIn("не нашёл кнопку «Подключиться»", text)

    def test_red_dedupe_per_reason_for_a_day(self):
        bad = {"bot_result": "failed", "bot_reason": "name_input_not_found"}
        w1 = World(runs=[bad, bad])
        self.cycle(w1)
        w2 = World(base=NIGHT + timedelta(hours=5), runs=[bad, bad])
        self.cycle(w2)
        self.assertEqual(w2.pushes, [])
        w3 = World(base=NIGHT + timedelta(hours=25), runs=[bad, bad])
        self.cycle(w3)
        self.assertEqual(len(w3.pushes), 1)

    def test_recovery_message(self):
        bad = {"bot_result": "failed"}
        self.cycle(World(runs=[bad, bad]))
        w = World(base=NIGHT + timedelta(days=1), runs=[{"participants": ["Тест-диктор"]}])
        self.cycle(w)
        self.assertEqual(len(w.pushes), 1)
        self.assertTrue(w.pushes[0][0].startswith("🟢 Самопроверка бота в Т-10 снова проходит"))

    def test_speaker_join_failed_is_red_with_reason(self):
        bad = {"speaker_result": "failed", "speaker_reason": "name_input_not_found"}
        w = World(runs=[bad, bad])
        res = self.cycle(w)
        self.assertEqual(res["reasons"], ["speaker_join_failed"])
        self.assertEqual(res["details"], {"speaker_join_failed": "name_input_not_found"})
        self.assertIn("-speaker", res["snapshot"])
        # бот даже не запускался
        self.assertFalse(any("-bot-" in c[c.index("--name") + 1] for c in w.docker_cmds))

    def test_speaker_setup_problem_is_not_red(self):
        bad = {"speaker_result": "failed", "speaker_reason": "pulse_unavailable"}
        w = World(runs=[bad])
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.ERROR)
        self.assertNotEqual(lib.load_health().get("status"), lib.RED)
        self.assertEqual(len(w.pushes), 1)
        self.assertTrue(w.pushes[0][0].startswith("⚙️"))
        self.assertTrue(w.pushes[0][1])

    def test_speaker_sound_failure_while_speaking_is_error(self):
        w = World(runs=[{"speak_fails": "pulse_unavailable"}])
        res = self.cycle(w)
        self.assertEqual((res["status"], res["reasons"]), (lib.ERROR, ["speaker_setup_failed"]))
        self.assertEqual(len(w.published), 1)  # бот всё равно ушёл штатно
        self.assertNotEqual(lib.load_health().get("status"), lib.RED)

    def test_bot_stuck_is_asked_to_leave_before_stop(self):
        w = World(runs=[{"bot_result": "none"}, {"bot_result": "none"}])
        res = self.cycle(w)
        self.assertEqual(res["reasons"], ["bot_join_timeout"])
        self.assertEqual(len(w.published), 2)  # по leave на каждый из двух прогонов

    def test_silence_run_is_red(self):
        silent = {"volume": (-91.0, -91.0), "transcript": ""}
        w = World(runs=[silent, silent])
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.RED)
        self.assertEqual(res["reasons"][0], "silence")

    def test_transcribe_failure_is_yellow_not_red(self):
        w = World(runs=[{"transcribe_error": True, "participants": ["Тест-диктор"]}])
        res = self.cycle(w)
        self.assertEqual((res["status"], res["reasons"]), (lib.YELLOW, ["transcribe_failed"]))

    def test_mic_off_named_as_root_cause(self):
        bad = {"mic_ui": "off", "transcript": "тишина и ничего"}
        w = World(runs=[bad, bad])
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.RED)
        self.assertEqual(res["reasons"][0], "speaker_mic_failed")
        self.assertIn("code_word_missing", res["reasons"])

    def test_retry_deferred_when_budget_exhausted(self):
        bad = {"bot_result": "failed"}
        w = World(runs=[bad, bad])
        with mock.patch.object(lib, "CYCLE_BUDGET_SEC", 60):
            res = self.cycle(w)
        self.assertEqual(res["status"], lib.RED_UNCONFIRMED)
        self.assertEqual(w.pushes, [])
        self.assertTrue(lib.load_health()["pending_recheck"])
        self.assertNotIn(lib.RETRY_PAUSE_SEC, w.sleeps)

    def test_retry_blocked_by_guard_is_unconfirmed(self):
        bad = {"bot_result": "failed"}
        w = World(runs=[bad, bad])
        orig_sleep = w.sleep

        def sleep(s):
            orig_sleep(s)
            if s == lib.RETRY_PAUSE_SEC:  # за время паузы стартовал настоящий бот
                w.running.append(("vexa-notarius-auto-x", "notarius"))

        w.sleep = sleep
        res = self.cycle(w)
        self.assertEqual((res["status"], res["skip_reason"]), (lib.RED_UNCONFIRMED, "bot_running"))
        self.assertEqual(w.pushes, [])
        self.assertTrue(lib.load_health()["pending_recheck"])

    # --- посторонние в комнате

    def test_room_busy_after_bot_joined_deletes_audio_and_stops_everything(self):
        w = World(runs=[{"busy_after_bot": True}])
        res = self.cycle(w)
        self.assertEqual((res["status"], res["skip_reason"]), (lib.SKIPPED, "room_busy"))
        self.assertEqual(w.pushes, [])
        self.assertTrue(res["recordings_deleted"])
        self.assertIsNone(res["run_dir"])
        # ни одной записи самопроверки на диске не осталось
        self.assertEqual(list((self.tmp / "canary").rglob("*.wav")), [])
        self.assertEqual(list((self.tmp / "canary").iterdir()), [])
        names = [c[c.index("--name") + 1] for c in w.docker_cmds]
        self.assertEqual(len(names), 2)
        for n in names:
            self.assertIn(n, w.stopped)
            self.assertIn(n, w.removed)
        self.assertEqual(len(w.published), 1)  # бот ушёл по команде
        saved = json.loads(self.run_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["skip_reason"], "room_busy")
        self.assertNotIn(".wav", json.dumps(saved))
        self.assertNotEqual(lib.load_health().get("status"), lib.RED)

    def test_room_busy_on_entry_bot_never_started(self):
        w = World(runs=[{"speaker_result": "busy"}])
        res = self.cycle(w)
        self.assertEqual(res["skip_reason"], "room_busy")
        self.assertEqual(len(w.docker_cmds), 1)
        self.assertEqual(w.pushes, [])

    def test_three_skips_in_a_row_quiet_note(self):
        for i in range(3):
            w = World(base=NIGHT + timedelta(days=i), runs=[{"speaker_result": "busy"}])
            self.cycle(w)
        self.assertEqual(len(w.pushes), 1)
        self.assertIn("3 раза подряд", w.pushes[0][0])
        self.assertTrue(w.pushes[0][1])

    # --- защиты

    def test_guard_bot_running_even_with_force(self):
        for running in ([("vexa-notarius-auto-x-20260926", "")], [("renamed", "notarius")]):
            w = World(running=running)
            res = self.cycle(w, force=True)
            self.assertEqual((res["status"], res["skip_reason"]), (lib.SKIPPED, "bot_running"))
            self.assertEqual(w.docker_cmds, [])
            self.assertEqual(w.pushes, [])

    def test_guard_meeting_in_30_minutes(self):
        q = [{"event_id": "e1", "series": "s", "start_at": (NIGHT + timedelta(minutes=30)).isoformat()}]
        w = World(queue=q)
        res = self.cycle(w)
        self.assertEqual(res["skip_reason"], "meeting_soon")
        self.assertEqual(w.docker_cmds, [])
        w2 = World(queue=q, runs=[{"participants": ["Тест-диктор"]}])
        res2 = self.cycle(w2, force=True)  # --force снимает только очередь
        self.assertEqual(res2["status"], lib.GREEN)

    def test_guard_meeting_far_away_is_ok(self):
        q = [{"event_id": "e1", "series": "s", "start_at": (NIGHT + timedelta(minutes=50)).isoformat()}]
        w = World(queue=q, runs=[{"participants": ["Тест-диктор"]}])
        self.assertEqual(self.cycle(w)["status"], lib.GREEN)

    def test_guard_docker_unavailable(self):
        w = World()
        w.running_containers = lambda: None
        self.assertEqual(self.cycle(w)["skip_reason"], "docker_unavailable")
        self.assertEqual(w.docker_cmds, [])

    def test_lock_held_by_other_run(self):
        fd = lib.try_lock()
        try:
            w = World()
            res = self.cycle(w)
        finally:
            lib.unlock(fd)
        self.assertEqual(res["skip_reason"], "lock_busy")
        self.assertEqual(w.docker_cmds, [])
        self.assertFalse(lib.health_path().exists())

    def test_room_missing_is_error_not_red(self):
        w = World(room_url=None)
        res = self.cycle(w)
        self.assertEqual((res["status"], res["reasons"]), (lib.ERROR, ["room_not_found"]))
        self.assertEqual(w.docker_cmds, [])

    def test_non_telemost_room_url_refused(self):
        w = World(room_url="https://evil.example.com/j/1")
        res = self.cycle(w)
        self.assertEqual(res["reasons"], ["room_url_invalid"])
        self.assertEqual(w.docker_cmds, [])

    # --- ссылка комнаты никуда не утекает

    def test_room_url_never_leaks(self):
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = H(level=logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        try:
            bad = {"bot_result": "failed", "bot_start_ok": True}
            w = World(runs=[bad, {"bot_start_ok": False}])
            self.cycle(w)
            w2 = World(base=NIGHT + timedelta(days=1), runs=[{"busy_after_bot": True}])
            self.cycle(w2)
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        self.assertTrue(records)
        for msg in records:
            self.assertNotIn(FAKE_URL, msg)
        for text, _silent in w.pushes + w2.pushes:
            self.assertNotIn("telemost.yandex", text)
        for p in (self.tmp / "state").rglob("*"):
            if p.is_file():
                self.assertNotIn("telemost.yandex", p.read_text(encoding="utf-8", errors="replace"), p)
        logs = list((self.tmp / "canary").rglob("logs/*.log"))
        self.assertTrue(logs)
        for p in logs:
            body = p.read_text(encoding="utf-8")
            self.assertNotIn(FAKE_URL, body)
            self.assertIn("<комната>", body)


class CliTest(_Env):
    def test_refuses_on_mac(self):
        import notary.canary as cli

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEETING_NOTARY_LOCAL_DOCKER", None)
            with mock.patch.object(cli.subprocess, "run", side_effect=AssertionError("docker на маке")), \
                 mock.patch("sys.stderr"):
                self.assertEqual(cli.main(["run"]), 2)
                self.assertEqual(cli.main(["sniff"]), 2)

    def test_status_works_anywhere(self):
        import notary.canary as cli

        with mock.patch("builtins.print") as pr:
            self.assertEqual(cli.main(["status"]), 0)
        self.assertIn("Самопроверка", pr.call_args[0][0])

    def test_stop_refuses_foreign_container(self):
        import notary.canary as cli

        with mock.patch.object(cli.subprocess, "run", side_effect=AssertionError("не должен звать docker")):
            with self.assertRaises(ValueError):
                cli.stop("vexa-notarius-auto-x")
            with self.assertRaises(ValueError):
                cli.remove("vexa-redis-1")


# ---------------------------------------------------------------------------
# Переходы состояния (чисто)
# ---------------------------------------------------------------------------


class ApplyFinalTest(unittest.TestCase):
    def final(self, status, reasons=(), **kw):
        return {"run_id": "canary-x", "room": "t10", "status": status, "reasons": list(reasons), **kw}

    def test_first_green_silent(self):
        h, msgs = lib.apply_final({}, self.final(lib.GREEN), DAY)
        self.assertEqual((h["status"], msgs), (lib.GREEN, []))

    def test_since_kept_while_status_same(self):
        h, _ = lib.apply_final({}, self.final(lib.RED, ["silence"]), DAY)
        h2, msgs = lib.apply_final(h, self.final(lib.RED, ["silence"]), DAY + timedelta(hours=2))
        self.assertEqual(h2["since"], h["since"])
        self.assertEqual(msgs, [])  # тот же ключ — дедуп на сутки

    def test_other_reason_alerts_again(self):
        h, _ = lib.apply_final({}, self.final(lib.RED, ["silence"]), DAY)
        _h2, msgs = lib.apply_final(h, self.final(lib.RED, ["bot_join_failed"]), DAY + timedelta(hours=2))
        self.assertEqual(len(msgs), 1)

    def test_night_window(self):
        self.assertTrue(lib.is_night(datetime(2026, 9, 26, 19, 0, tzinfo=timezone.utc)))   # 22:00 МСК
        self.assertTrue(lib.is_night(datetime(2026, 9, 26, 4, 59, tzinfo=timezone.utc)))   # 07:59 МСК
        self.assertFalse(lib.is_night(datetime(2026, 9, 26, 5, 0, tzinfo=timezone.utc)))    # 08:00 МСК


# ---------------------------------------------------------------------------
# «Нюх»
# ---------------------------------------------------------------------------

HTML = ('<html><head><script src="https://yastatic.net/s3/telemost/telemessenger/_/211.3.0/web/app.js">'
        '</script></head></html>')


class SniffTest(_Env):
    def deps(self, *, html=HTML, code=200, error=None, now=DAY):
        w = World(base=now)
        d = w.deps()

        def get(url):
            if error:
                raise error
            return code, html

        d.http_get = get
        return w, d

    def fake_run(self, results):
        calls = []

        def run(**kw):
            calls.append(kw)
            return results.pop(0) if results else {"status": lib.GREEN, "run_id": "r"}

        return run, calls

    def test_parse_version(self):
        self.assertEqual(lib.parse_version(HTML), "211.3.0")
        self.assertEqual(lib.parse_version('src="/telemessenger/v2/_/212.0.1/web/main.js"'), "212.0.1")
        self.assertEqual(lib.parse_version('"/_/213.1.0/static/telemessenger-app.js"'), "213.1.0")
        self.assertIsNone(lib.parse_version("<html>nothing</html>"))

    def test_first_observation_only_recorded(self):
        w, d = self.deps()
        run, calls = self.fake_run([])
        out = lib.sniff(deps=d, run=run)
        self.assertEqual(out["version"], "211.3.0")
        self.assertEqual(calls, [])
        self.assertEqual(w.pushes, [])
        self.assertEqual(lib.load_version_state()["version"], "211.3.0")

    def test_change_triggers_run(self):
        w, d = self.deps()
        lib.sniff(deps=d, run=self.fake_run([])[0])
        w2, d2 = self.deps(html=HTML.replace("211.3.0", "212.0.0"), now=DAY + timedelta(hours=1))
        run, calls = self.fake_run([{"status": lib.GREEN, "run_id": "r1"}])
        out = lib.sniff(deps=d2, run=run)
        self.assertTrue(out["changed"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["reason"], "version-change")
        self.assertEqual(w2.pushes, [("Телемост обновился: 211.3.0 → 212.0.0. Запускаю внеплановую самопроверку.", True)])
        st = lib.load_version_state()
        self.assertFalse(st["pending_check"])
        self.assertEqual([h["version"] for h in st["history"]], ["211.3.0", "212.0.0"])

    def test_guards_keep_pending_then_next_sniff_runs(self):
        lib.sniff(deps=self.deps()[1], run=self.fake_run([])[0])
        _w, d = self.deps(html=HTML.replace("211.3.0", "212.0.0"), now=DAY + timedelta(hours=1))
        run, calls = self.fake_run([{"status": lib.SKIPPED, "skip_reason": "meeting_soon", "run_id": "r"}])
        lib.sniff(deps=d, run=run)
        self.assertTrue(lib.load_version_state()["pending_check"])
        w3, d3 = self.deps(html=HTML.replace("211.3.0", "212.0.0"), now=DAY + timedelta(hours=2))
        run3, calls3 = self.fake_run([{"status": lib.GREEN, "run_id": "r2"}])
        out = lib.sniff(deps=d3, run=run3)
        self.assertFalse(out["changed"])
        self.assertEqual(len(calls3), 1)
        self.assertEqual(w3.pushes, [])  # о смене уже сказали
        self.assertFalse(lib.load_version_state()["pending_check"])

    def test_room_busy_backs_off_two_hours(self):
        lib.sniff(deps=self.deps()[1], run=self.fake_run([])[0])
        _w, d = self.deps(html=HTML.replace("211.3.0", "212.0.0"), now=DAY + timedelta(hours=1))
        lib.sniff(deps=d, run=self.fake_run([{"status": lib.SKIPPED, "skip_reason": "room_busy", "run_id": "r"}])[0])
        _w, d2 = self.deps(html=HTML.replace("211.3.0", "212.0.0"), now=DAY + timedelta(hours=2))
        run2, calls2 = self.fake_run([])
        lib.sniff(deps=d2, run=run2)
        self.assertEqual(calls2, [])
        _w, d3 = self.deps(html=HTML.replace("211.3.0", "212.0.0"), now=DAY + timedelta(hours=3, minutes=1))
        run3, calls3 = self.fake_run([])
        lib.sniff(deps=d3, run=run3)
        self.assertEqual(len(calls3), 1)

    def test_pending_recheck_from_health_runs(self):
        lib.sniff(deps=self.deps()[1], run=self.fake_run([])[0])
        lib.save_health({"status": lib.GREEN, "pending_recheck": True})
        run, calls = self.fake_run([])
        lib.sniff(deps=self.deps(now=DAY + timedelta(hours=1))[1], run=run)
        self.assertEqual([c["reason"] for c in calls], ["recheck"])

    def test_six_errors_one_message_then_reset(self):
        pushes = []
        for i in range(7):
            w, d = self.deps(error=OSError("net down"), now=DAY + timedelta(hours=i))
            lib.sniff(deps=d, run=self.fake_run([])[0])
            pushes += w.pushes
        self.assertEqual(len(pushes), 1)
        self.assertIn("6 раз подряд", pushes[0][0])
        self.assertTrue(pushes[0][1])
        lib.sniff(deps=self.deps(now=DAY + timedelta(hours=8))[1], run=self.fake_run([])[0])
        self.assertEqual(lib.load_version_state()["consecutive_errors"], 0)

    def test_http_error_counts(self):
        w, d = self.deps(code=503)
        lib.sniff(deps=d, run=self.fake_run([])[0])
        self.assertEqual(lib.load_version_state()["consecutive_errors"], 1)
        self.assertIsNone(lib.load_version_state().get("version"))


# ---------------------------------------------------------------------------
# Предупреждение перед встречами
# ---------------------------------------------------------------------------


class WarnTest(_Env):
    NOW = datetime(2026, 9, 26, 6, 50, tzinfo=timezone.utc)

    def item(self, minutes, **kw):
        it = {"event_id": "gcal:e1", "meeting_id": "anzhee-direktorat", "series": "anzhee-direktorat",
              "start_at": (self.NOW + timedelta(minutes=minutes)).isoformat(), "url": FAKE_URL}
        it.update(kw)
        return it

    def red(self):
        lib.save_health({"status": lib.RED, "since": "2026-09-26T01:40:00+00:00",
                         "reasons": ["bot_join_failed"],
                         "reasons_human": "бот не вошёл в звонок: не нашёл поле для имени (bot_join_failed)"})

    def test_warns_once_in_window_only_when_red(self):
        self.red()
        sent = []
        n = lib.warn_tick([self.item(10), self.item(5, event_id="e2"), self.item(15, event_id="e3")],
                          self.NOW, notify=sent.append)
        self.assertEqual(n, 1)
        text = sent[0]
        self.assertTrue(text.startswith("⚠️ Самопроверка бота красная с 26.09 04:40 МСК"), text)
        self.assertIn("10:00 МСК", text)
        self.assertIn("бот может не зайти", text)
        self.assertIn("включи запись в Телемосте", text)
        self.assertNotIn("telemost.yandex", text)
        # следующая минута — то же окно, но уже предупреждён
        again = lib.warn_tick([self.item(10)], self.NOW + timedelta(minutes=1), notify=sent.append)
        self.assertEqual((again, len(sent)), (0, 1))

    def test_window_edges(self):
        self.red()
        sent = []
        lib.warn_tick([self.item(8, event_id="a"), self.item(12, event_id="b"),
                       self.item(7.5, event_id="c"), self.item(12.5, event_id="d")], self.NOW, notify=sent.append)
        self.assertEqual(len(sent), 2)

    def test_green_or_yellow_no_warning(self):
        for st in (lib.GREEN, lib.YELLOW, None):
            lib.save_health({"status": st})
            sent = []
            self.assertEqual(lib.warn_tick([self.item(10)], self.NOW, notify=sent.append), 0)
            self.assertEqual(sent, [])

    def test_portal_meeting_note(self):
        self.red()
        sent = []
        lib.warn_tick([self.item(10, order_id="o1", series_title="Планёрка")], self.NOW, notify=sent.append)
        self.assertIn("портала", sent[0])

    def test_old_marks_pruned(self):
        self.red()
        lib.write_json(lib.warned_path(), {"old@x": "2026-09-20T00:00:00+00:00"})
        lib.warn_tick([self.item(10)], self.NOW, notify=lambda t: None)
        self.assertNotIn("old@x", json.loads(lib.warned_path().read_text(encoding="utf-8")))

    def test_switch(self):
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_CANARY_WARN": "0"}):
            self.assertFalse(lib.warn_enabled())
        self.assertTrue(lib.warn_enabled())


class RunnerWarnWiringTest(_Env):
    def setUp(self):
        super().setUp()
        import notary.cli.registry as registry_mod
        import notary.runner as runner

        self.runner = runner
        (self.tmp / "registry").mkdir()
        (self.tmp / "registry" / "watched.yaml").write_text("watched: []\n", encoding="utf-8")
        (self.tmp / "logs" / "runs").mkdir(parents=True)
        self._env2 = mock.patch.dict(os.environ, {
            "MEETING_NOTARY_REGISTRY_DIR": str(self.tmp / "registry"),
            "MEETING_NOTARY_LOG_DIR": str(self.tmp / "logs"),
        })
        self._env2.start()
        self.launches = []
        self.patches = [
            mock.patch.object(runner, "setup_logging", lambda: None),
            mock.patch.object(runner, "push"),
            mock.patch.object(registry_mod, "WATCHED_FILE", self.tmp / "registry" / "watched.yaml"),
            mock.patch.object(registry_mod, "ROOMS_FILE", self.tmp / "registry" / "rooms.yaml"),
            mock.patch.object(registry_mod, "PAUSE_FILE", self.tmp / "registry" / ".pause-until"),
            mock.patch.object(runner, "RUNS_DIR", self.tmp / "logs" / "runs"),
            mock.patch.object(runner, "LOG_DIR", self.tmp / "logs"),
            mock.patch.object(runner, "_join_watch_tick", lambda now, allow_retry: None),
            mock.patch.object(runner, "_join_watch_register", lambda *a, **k: None),
            mock.patch.object(runner, "_running_bots_snapshot",
                              return_value={"by_series": {}, "by_room": {}, "unlabeled": [], "total": 0}),
            mock.patch.object(runner, "_launch_bot",
                              side_effect=lambda item, now, seq=0, attempt=1: self.launches.append(item) or True),
        ]
        for p in self.patches:
            p.start()
        from notary.lib import state as state_lib

        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        state_lib.write_queue([{
            "meeting_id": "anzhee-direktorat", "event_id": "gcal:e1", "series": "anzhee-direktorat",
            "start_at": (self.now + timedelta(minutes=4)).isoformat(), "duration_minutes": 60,
            "url": FAKE_URL, "expected_participants": [], "type": "recurring",
        }])

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._env2.stop()
        super().tearDown()

    def test_warn_failure_never_breaks_tick(self):
        with mock.patch.object(self.runner, "LOCAL_DOCKER", True), \
             mock.patch.object(lib, "warn_tick", side_effect=RuntimeError("boom")) as wt:
            self.assertEqual(self.runner.main(now=self.now), 0)
        self.assertTrue(wt.called)
        self.assertEqual(len(self.launches), 1)  # бот всё равно запущен

    def test_warn_gets_queue(self):
        with mock.patch.object(self.runner, "LOCAL_DOCKER", True), \
             mock.patch.object(lib, "warn_tick") as wt:
            self.runner.main(now=self.now)
        queue, now = wt.call_args[0]
        self.assertEqual(queue[0]["event_id"], "gcal:e1")
        self.assertEqual(now, self.now)

    def test_switch_off_and_mac(self):
        with mock.patch.object(self.runner, "LOCAL_DOCKER", True), \
             mock.patch.dict(os.environ, {"MEETING_NOTARY_CANARY_WARN": "0"}), \
             mock.patch.object(lib, "warn_tick") as wt:
            self.runner.main(now=self.now)
        self.assertFalse(wt.called)
        with mock.patch.object(self.runner, "LOCAL_DOCKER", False), \
             mock.patch.object(lib, "warn_tick") as wt2:
            self.runner.main(now=self.now)
        self.assertFalse(wt2.called)

    def test_end_to_end_red_health_warns_owner(self):
        self_now = self.now
        lib.save_health({"status": lib.RED, "since": iso_(self_now - timedelta(hours=3)), "reasons": ["silence"]})
        from notary.lib import state as state_lib

        state_lib.write_queue([{
            "meeting_id": "anzhee-direktorat", "event_id": "gcal:e1", "series": "anzhee-direktorat",
            "start_at": (self_now + timedelta(minutes=10)).isoformat(), "duration_minutes": 60,
            "url": FAKE_URL, "expected_participants": [], "type": "recurring",
        }])
        with mock.patch.object(self.runner, "LOCAL_DOCKER", True), \
             mock.patch("notary.lib.notify.push") as push:
            self.runner.main(now=self_now)
            self.runner.main(now=self_now + timedelta(minutes=1))
        self.assertEqual(push.call_count, 1)
        self.assertIn("бот может не зайти", push.call_args[0][0])


def iso_(dt):
    return dt.isoformat()


if __name__ == "__main__":
    unittest.main()
