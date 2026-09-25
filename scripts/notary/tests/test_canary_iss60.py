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
    bot_fallback=None,             # dict → `fallback_used` в статусе входа бота
    # Фаза 4 — второе ухо: бот после фазы 4 пишет `*.chunk0.wav` и сведения в meta.
    ear=False,                     # писать файл второго уха и сведения о нём в meta
    ear_duration=None,             # None → как у основной записи
    ear_volume=None,               # None → как у основной записи
    ear_transcript=None,           # None → как у основной записи
    ear_status="stopped",
    audio_source="primary",        # откуда финализация взяла бы звук
    end_reason="page_closed",      # page_closed — бот ушёл по команде leave
    deaf_event_sec=None,           # через сколько с после входа бот объявит «основная оглохла»
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
        self.transcribed_files = []
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
        # `docker run` не дождался ответа (таймаут CLI), а контейнер всё равно поднялся.
        timed_out = bool(cfg.get(f"{kind}_start_timeout_but_running"))
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
        if timed_out:
            return False, "docker run timeout"
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

    # --- звук (второе ухо `*.chunk0.wav` может звучать иначе, чем основная запись)
    @staticmethod
    def _is_ear(p):
        return str(p).endswith(".chunk0.wav")

    def _per_file(self, p, key):
        cfg = self.cfg()
        if self._is_ear(p) and cfg.get(f"ear_{key}") is not None:
            return cfg[f"ear_{key}"]
        return cfg[key]

    def probe(self, p):
        return self._per_file(p, "duration")

    def volume(self, p):
        return self._per_file(p, "volume")

    def transcribe(self, p):
        self.transcribed += 1
        self.transcribed_files.append(Path(p).name)
        cfg = self.cfg()
        if cfg["transcribe_error"]:
            raise RuntimeError("whisper down")
        return self._per_file(p, "transcript").format(word=self.last_word)

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
        # Фаза 4: бот сам объявляет «основная оглохла» (правило «минута подряд»).
        if (st.get("phase") == "in_call" and cfg["deaf_event_sec"] is not None and not c.get("deaf_written")
                and self.t - c.get("in_call_at", self.t) >= cfg["deaf_event_sec"]):
            c["deaf_written"] = True
            self._write(st_path, {**st, "events": [{"type": "primary_deaf", "rule": "sustained", "at": "x"}]})
        if st.get("phase") is None and self.t - c["started"] >= cfg["bot_join_sec"]:
            r = cfg["bot_result"]
            if r == "in_call":
                c["in_call_at"] = self.t
                self._write(st_path, {"phase": "in_call", "history": [
                    {"phase": "joining"}, {"phase": "admission"}, {"phase": "in_call"}],
                    **({"fallback_used": cfg["bot_fallback"]} if cfg.get("bot_fallback") else {})})
                if cfg["wav"]:
                    (d / "2026-09-26-tm-1.chunk1.wav").write_bytes(b"RIFF....WAVE")
                    meta = {"participants": cfg["participants"], "meetingUrl": FAKE_URL}
                    if cfg["ear"]:
                        (d / "2026-09-26-tm-1.chunk0.wav").write_bytes(b"RIFF....WAVE-ear")
                        meta["endReason"] = cfg["end_reason"]
                        meta["recording"] = {
                            "audioSource": cfg["audio_source"],
                            "secondEar": {"status": cfg["ear_status"],
                                          "wav": "/transcripts/2026-09-26-tm-1.chunk0.wav"},
                        }
                    self._write(d / "2026-09-26-tm-1.meta.json", meta)
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
        # По умолчанию — боевой образ: только его прогон меняет «бот здоров/сломан»
        # и пишет владельцу (is_production_image). Кандидаты — `image=...` явно.
        return lib.run_cycle(deps=world.deps(), image=kw.pop("image", lib.DEFAULT_IMAGE), room="t10",
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

    def test_fallback_join_is_yellow(self):
        # Цикл5 фазы 3 (У5): основной вход сломан, бот вошёл запасным → не «всё хорошо».
        s = "основной путь не нашёл поле имени; запасной: имя — по подписи, «Подключиться» — по тексту"
        r = lib.evaluate(ev(bot_fallback=s))
        self.assertEqual((r["status"], r["reasons"]), (lib.YELLOW, ["fallback_join"]))
        self.assertEqual(r["details"]["fallback_join"], s)
        self.assertIn("ЗАПАСНЫМ", lib.human_reasons(r))

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

    @staticmethod
    def _bot_cfg(plan):
        cmd = lib.bot_docker_cmd(plan)
        return json.loads(next(a for a in cmd if a.startswith("BOT_CONFIG="))[len("BOT_CONFIG="):])

    def test_force_fallback_off_by_default(self):
        # Фаза 3б: принудительный запасной путь — только по явному флагу живой проверки.
        env = {k: v for k, v in os.environ.items() if k != lib.FORCE_FALLBACK_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotIn("telemostForceFallback", self._bot_cfg(self.plan()))
        with mock.patch.dict(os.environ, {lib.FORCE_FALLBACK_ENV: "0"}):
            self.assertNotIn("telemostForceFallback", self._bot_cfg(self.plan()))

    def test_force_fallback_only_test_image_and_only_bot(self):
        import dataclasses  # noqa: PLC0415

        with mock.patch.dict(os.environ, {lib.FORCE_FALLBACK_ENV: "1"}):
            self.assertIs(self._bot_cfg(self.plan())["telemostForceFallback"], True)
            prod = dataclasses.replace(self.plan(), image=lib.DEFAULT_IMAGE)
            self.assertNotIn("telemostForceFallback", self._bot_cfg(prod))   # боевой образ — никогда
            self.assertNotIn("telemostForceFallback", " ".join(lib.speaker_docker_cmd(self.plan())))  # диктор — основным путём
        with mock.patch.dict(os.environ, {lib.FORCE_FALLBACK_ENV: "no-testid"}):
            self.assertEqual(self._bot_cfg(self.plan())["telemostForceFallback"], "no-testid")

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

    def test_fallback_join_is_yellow_forced_is_not(self):
        # Цикл5 фазы 3 (У5): вошёл запасным сам (основной сломан) → 🟡 и сообщение;
        # по флагу живой проверки (forced) — это не поломка, зелёный.
        fb = {"primary_reason": "name_input_not_found", "forced": None, "steps": "name:label,join:text",
              "summary": "основной путь не нашёл поле имени; запасной: имя — по подписи, «Подключиться» — по тексту"}
        w = World(runs=[{"participants": ["Тест-диктор"], "bot_fallback": fb}])
        res = self.cycle(w)
        self.assertEqual((res["status"], res["reasons"]), (lib.YELLOW, ["fallback_join"]), res)
        self.assertEqual(len(w.pushes), 1)
        self.assertIn("ЗАПАСНЫМ", w.pushes[0][0])
        self.assertIn("не нашёл поле имени", w.pushes[0][0])
        w2 = World(base=NIGHT + timedelta(days=1),
                   runs=[{"participants": ["Тест-диктор"], "bot_fallback": dict(fb, forced="all")}])
        self.assertEqual(self.cycle(w2, image="vexa-bot:resil-test")["status"], lib.GREEN)

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

    # --- цикл5 фазы 2: боевой образ и кандидаты (Н2)

    def test_candidate_image_never_touches_production_state(self):
        bad = {"bot_result": "failed", "bot_reason": "name_input_not_found"}
        w = World(runs=[bad, bad])
        res = self.cycle(w, image="vexa-bot:resil-candidate", reason="manual")
        self.assertEqual(res["status"], lib.RED)
        self.assertEqual(w.pushes, [])                  # владельца не пугаем
        self.assertFalse(lib.health_path().exists())    # боевое состояние не тронуто
        self.assertEqual(len(self.run_files()), 2)      # итог прогонов — на месте
        journal = [json.loads(x) for x in
                   (self.tmp / "state" / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual((journal[-1]["image"], journal[-1]["production"]), ("vexa-bot:resil-candidate", False))

    def test_green_candidate_does_not_mask_red_production(self):
        bad = {"bot_result": "failed"}
        self.cycle(World(runs=[bad, bad]))              # боевой образ сломан → красный
        self.assertEqual(lib.load_health()["status"], lib.RED)
        w = World(base=NIGHT + timedelta(hours=2), runs=[{"participants": ["Тест-диктор"]}])
        res = self.cycle(w, image="vexa-bot:resil-fix", reason="manual")
        self.assertEqual(res["status"], lib.GREEN)
        self.assertEqual(lib.load_health()["status"], lib.RED)  # предупреждения перед встречами идут дальше
        self.assertEqual(w.pushes, [])

    # --- цикл5 фазы 2: бот не остаётся в комнате без присмотра (Н1, Н1б)

    def test_bot_up_despite_docker_run_timeout_is_led_out(self):
        w = World(runs=[{"bot_start_timeout_but_running": True}, {"bot_start_timeout_but_running": True}])
        res = self.cycle(w)
        self.assertEqual(res["reasons"], ["bot_start_failed"])
        bots = [c[c.index("--name") + 1] for c in w.docker_cmds if "-bot-" in c[c.index("--name") + 1]]
        self.assertEqual(len(bots), 2)
        for name in bots:
            self.assertIn(name, w.removed)
        self.assertEqual(w.containers, {})              # в комнате никого не осталось

    def test_signal_mid_run_leads_everyone_out_and_releases_lock(self):
        w = World()
        orig_sleep = w.sleep

        def sleep(s):  # SIGTERM от systemd приходит, когда бот уже в звонке
            orig_sleep(s)
            if any(c["kind"] == "bot" and c["status"] == "running" and w.t - c["started"] > 40
                   for c in w.containers.values()):
                raise SystemExit(143)

        w.sleep = sleep
        with self.assertRaises(SystemExit):
            self.cycle(w)
        names = [c[c.index("--name") + 1] for c in w.docker_cmds]
        self.assertEqual(len(names), 2)
        for n in names:
            self.assertIn(n, w.removed)
        self.assertEqual(w.containers, {})
        self.assertEqual(len(w.published), 1)           # бот попрошен уйти штатно
        fd = lib.try_lock()                             # замок отпущен
        self.assertIsNotNone(fd)
        lib.unlock(fd)

    # --- цикл5 фазы 2: самопроверка не выключается молча (У2) и отвечает на «запускаю» (У3)

    def test_skip_note_repeats_every_week_of_skips(self):
        pushes = []
        for i in range(11):
            w = World(base=NIGHT + timedelta(days=i), runs=[{"speaker_result": "busy"}])
            self.cycle(w)
            pushes += w.pushes
        self.assertEqual(len(pushes), 2, pushes)
        self.assertIn("уже 3 раза подряд", pushes[0][0])
        self.assertIn("уже 10 раз подряд", pushes[1][0])
        self.assertTrue(all(silent for _t, silent in pushes))

    def test_version_change_run_reports_result_when_ok(self):
        lib.save_health({"status": lib.YELLOW, "since": iso_(NIGHT - timedelta(days=1)),
                         "reasons": ["names_missing"]})
        lib.write_json(lib.version_path(), {"version": "212.2.0"})
        w = World(base=DAY)
        res = self.cycle(w, reason="version-change", force=True)
        self.assertEqual(res["status"], lib.YELLOW)
        self.assertEqual(len(w.pushes), 1, w.pushes)
        text, silent = w.pushes[0]
        self.assertTrue(text.startswith("✅ Самопроверка после обновления Телемоста до 212.2.0 прошла"), text)
        self.assertIn("Замечание", text)
        self.assertTrue(silent)

    def test_version_change_first_yellow_not_doubled(self):
        w = World(base=DAY)
        self.cycle(w, reason="version-change", force=True)
        self.assertEqual(len(w.pushes), 1)
        self.assertTrue(w.pushes[0][0].startswith("🟡"))

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

    def test_sigterm_and_sighup_become_system_exit(self):
        """systemd по сроку юнита и оборванный ssh → SystemExit, finally успевает убрать (Н1)."""
        import signal

        import notary.canary as cli

        saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        try:
            cli.install_signal_handlers()
            for s in (signal.SIGTERM, signal.SIGHUP):
                handler = signal.getsignal(s)
                self.assertTrue(callable(handler))
                with self.assertRaises(SystemExit) as cm:
                    handler(s, None)
                self.assertEqual(cm.exception.code, 128 + s)
                # повторный такой же сигнал не рвёт уборку
                self.assertEqual(signal.getsignal(s), signal.SIG_IGN)
        finally:
            for s, h in saved.items():
                signal.signal(s, h)

    def test_units_signal_only_python(self):
        """KillMode=mixed: SIGTERM — питону (он уводит бота из комнаты), docker-команды не рвутся."""
        deploy = _HERE.parent / "deploy"
        for unit in ("meeting-notary-canary.service", "meeting-notary-telemost-sniff.service"):
            text = (deploy / unit).read_text(encoding="utf-8")
            self.assertIn("\nKillMode=mixed\n", text, unit)
            self.assertIn("\nTimeoutStopSec=", text, unit)


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

    def test_daily_red_reminder_survives_run_time_jitter(self):
        """Завтрашний прогон кончился на 20 с раньше — напоминание о красном всё равно уходит (Н3)."""
        h, msgs = lib.apply_final({}, self.final(lib.RED, ["silence"]), NIGHT + timedelta(minutes=12, seconds=10))
        self.assertEqual(len(msgs), 1)
        _h2, msgs2 = lib.apply_final(h, self.final(lib.RED, ["silence"]),
                                     NIGHT + timedelta(days=1, minutes=11, seconds=50))
        self.assertEqual(len(msgs2), 1)

    def test_plural(self):
        self.assertEqual([lib._times(n) for n in (3, 10, 17, 22, 24, 12, 31)],
                         ["раза", "раз", "раз", "раза", "раза", "раз", "раз"])


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

    def test_candidate_sniff_does_not_consume_version_check(self):
        lib.sniff(deps=self.deps()[1], run=self.fake_run([])[0])
        _w, d = self.deps(html=HTML.replace("211.3.0", "212.0.0"), now=DAY + timedelta(hours=1))
        run, calls = self.fake_run([{"status": lib.GREEN, "run_id": "r"}])
        lib.sniff(deps=d, image="vexa-bot:resil-candidate", run=run)
        self.assertEqual(len(calls), 1)
        self.assertTrue(lib.load_version_state()["pending_check"])   # боевой образ ещё должен пройти
        _w, d2 = self.deps(html=HTML.replace("211.3.0", "212.0.0"), now=DAY + timedelta(hours=2))
        run2, calls2 = self.fake_run([{"status": lib.GREEN, "run_id": "r2"}])
        lib.sniff(deps=d2, run=run2)
        self.assertEqual(calls2[0]["image"], lib.DEFAULT_IMAGE)
        self.assertFalse(lib.load_version_state()["pending_check"])

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

    # --- цикл5 фазы 2: самопроверка не умирает молча (У1)

    def test_stale_canary_quiet_note_once_a_day(self):
        lib.save_health({"status": lib.GREEN, "last_run_at": iso_(self.NOW - timedelta(hours=31))})
        loud, quiet = [], []
        self.assertEqual(lib.warn_tick([self.item(10)], self.NOW, notify=loud.append, notify_quiet=quiet.append), 0)
        self.assertEqual(loud, [])                       # зелёный — о встречах не предупреждаем
        self.assertEqual(len(quiet), 1)
        self.assertIn("не доходила до итога с 25.09 02:50 МСК", quiet[0])
        self.assertIn("meeting-notary-canary", quiet[0])
        lib.warn_tick([], self.NOW + timedelta(hours=3), notify=loud.append, notify_quiet=quiet.append)
        self.assertEqual(len(quiet), 1)                  # в тот же день — один раз
        lib.warn_tick([], self.NOW + timedelta(days=1), notify=loud.append, notify_quiet=quiet.append)
        self.assertEqual(len(quiet), 2)                  # назавтра — снова

    def test_fresh_or_absent_health_no_stale_note(self):
        quiet = []
        lib.warn_tick([], self.NOW, notify=lambda t: None, notify_quiet=quiet.append)   # health.json нет
        lib.save_health({"status": lib.GREEN, "last_run_at": iso_(self.NOW - timedelta(hours=20))})
        lib.warn_tick([], self.NOW, notify=lambda t: None, notify_quiet=quiet.append)
        self.assertEqual(quiet, [])
        self.assertFalse(lib.warned_path().exists())     # без повода warned.json не пишем

    def test_stale_and_red_both_go(self):
        lib.save_health({"status": lib.RED, "since": "2026-09-24T01:40:00+00:00", "reasons": ["silence"],
                         "last_run_at": iso_(self.NOW - timedelta(hours=40))})
        loud, quiet = [], []
        self.assertEqual(lib.warn_tick([self.item(10)], self.NOW, notify=loud.append, notify_quiet=quiet.append), 1)
        self.assertEqual((len(loud), len(quiet)), (1, 1))


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


@unittest.skipUnless(shutil.which("node"), "нет node — решения диктора не проверить")
class SpeakerIdleVerdictTest(unittest.TestCase):
    """canary-kit/speaker.js::idleVerdict — пересчёт участников в ожидании (цикл5 фазы 2, Н5)."""

    def verdicts(self, samples, strict=True):
        import subprocess

        script = (
            "const s = require('./speaker.js');"
            "const cfg = {strictNames: %s}; let st = {}; const out = [];"
            "for (const [count, foreign] of %s) {"
            "  st = s.idleVerdict(cfg, {count, method: 'x', diag: {button: count, tiles: count, foreign_names: foreign}}, st);"
            "  out.push(st.busy);"
            "}"
            "console.log(JSON.stringify(out));"
        ) % ("true" if strict else "false", json.dumps(samples))
        proc = subprocess.run(["node", "-e", script], cwd=str(KIT), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_stranger_by_name_before_bot_leaves_after_two_checks(self):
        # число 2 (диктор + «кто-то»): по числу это мог быть бот, по имени — нет
        self.assertEqual(self.verdicts([[2, 1], [2, 1]]), [None, "foreign_name"])

    def test_flickering_tile_does_not_evict(self):
        self.assertEqual(self.verdicts([[2, 1], [2, 0], [2, 1], [2, 0]]), [None, None, None, None])

    def test_bot_alone_with_speaker_is_fine(self):
        self.assertEqual(self.verdicts([[2, 0], [2, 0], [1, 0]]), [None, None, None])

    def test_count_rules_kept(self):
        self.assertEqual(self.verdicts([[3, 0]]), ["strangers"])
        self.assertEqual(self.verdicts([[None, None], [None, None], [None, None]]), [None, None, "count_unknown"])

    def test_strict_names_switch(self):
        self.assertEqual(self.verdicts([[2, 1], [2, 1], [2, 1]], strict=False), [None, None, None])


# ---------------------------------------------------------------------------
# Фаза 4 — второе ухо бота (`*.chunk0.wav`, всё, что играл браузер)
# ---------------------------------------------------------------------------

QUIET = (-91.0, -91.0)   # цифровая тишина
#: Второе ухо слышит диктора (основная при этом молчит — whisper по тишине даёт пусто).
EAR_TEXT = DEFAULT_RUN["transcript"]


def ear_ev(**kw):
    """Новый бот: пишет второе ухо; основная и второе ухо слышат диктора."""
    base = dict(ear_present=True, ear_duration=45.0, ear_volume=(-30.0, -6.0), ear_code_word_found=None,
                ear_status="stopped", audio_source="primary", end_reason="page_closed")
    base.update(kw)
    return ev(**base)


class EvaluateSecondEarTest(unittest.TestCase):
    # --- обычный прогон: второе ухо не маскирует оглохшую основную
    def test_rescued_by_ear_is_yellow_not_red(self):
        r = lib.evaluate(ear_ev(volume=QUIET, code_word_found=None, audio_source="second_ear",
                                ear_code_word_found=True))
        self.assertEqual(r["status"], lib.YELLOW)
        self.assertIn("primary_deaf", r["reasons"])
        self.assertNotIn("silence", r["reasons"])
        self.assertIn("silence", r["details"]["primary_deaf"])
        self.assertTrue(r["checks"]["second_ear"]["protocol_from_ear"])

    def test_ear_heard_but_bot_did_not_switch_is_red(self):
        # Второе ухо слышит, но протокол ушёл бы из основной — встреча потерялась бы.
        r = lib.evaluate(ear_ev(volume=QUIET, code_word_found=None, ear_code_word_found=True))
        self.assertEqual(r["status"], lib.RED)
        self.assertIn("silence", r["reasons"])

    def test_switched_but_ear_has_no_code_word_is_red(self):
        r = lib.evaluate(ear_ev(volume=QUIET, code_word_found=None, audio_source="second_ear",
                                ear_code_word_found=False))
        self.assertEqual(r["status"], lib.RED)

    def test_rescue_needs_bot_in_call(self):
        r = lib.evaluate(ear_ev(bot_phase="failed", bot_phases_seen=("joining", "failed"), volume=QUIET,
                                code_word_found=None, audio_source="second_ear", ear_code_word_found=True))
        self.assertEqual(r["status"], lib.RED)
        self.assertNotIn("primary_deaf", r["reasons"])

    def test_ear_failed_with_live_primary_is_yellow(self):
        r = lib.evaluate(ear_ev(ear_present=False, ear_duration=None, ear_volume=None, ear_status="failed"))
        self.assertEqual(r["status"], lib.YELLOW)
        self.assertEqual(r["details"]["second_ear_problem"], "не запустилось")

    def test_silent_ear_with_live_primary_is_yellow(self):
        r = lib.evaluate(ear_ev(ear_volume=QUIET))
        self.assertEqual((r["status"], r["reasons"]), (lib.YELLOW, ["second_ear_problem"]))

    def test_needless_switch_is_yellow(self):
        r = lib.evaluate(ear_ev(audio_source="second_ear"))
        self.assertEqual((r["status"], r["reasons"]), (lib.YELLOW, ["second_ear_problem"]))

    def test_old_image_without_ear_stays_green(self):
        self.assertEqual(lib.evaluate(ev())["status"], lib.GREEN)
        self.assertEqual(lib.evaluate(ear_ev())["status"], lib.GREEN)

    # --- живая проверка «основная оглохла» (флаг)
    def deaf(self, **kw):
        base = dict(force_deaf=True, volume=QUIET, code_word_found=None, audio_source="second_ear",
                    ear_code_word_found=True, deaf_event_live=True)
        base.update(kw)
        return lib.evaluate(ear_ev(**base))

    def test_forced_deaf_all_good_is_green(self):
        r = self.deaf()
        self.assertEqual((r["status"], r["reasons"]), (lib.GREEN, []))
        self.assertTrue(r["checks"]["stayed_until_leave"])

    def test_forced_deaf_without_ear_file_is_red(self):
        r = self.deaf(ear_present=False, ear_duration=None, ear_volume=None, ear_code_word_found=None)
        self.assertEqual((r["status"], r["reasons"][0]), (lib.RED, "second_ear_missing"))

    def test_forced_deaf_short_or_silent_ear_is_red(self):
        self.assertEqual(self.deaf(ear_duration=4.0)["reasons"][0], "second_ear_too_short")
        self.assertEqual(self.deaf(ear_volume=QUIET, ear_code_word_found=None)["reasons"][0], "second_ear_silence")

    def test_forced_deaf_no_code_word_in_ear_is_red(self):
        r = self.deaf(ear_code_word_found=False)
        self.assertEqual((r["status"], r["reasons"]), (lib.RED, ["second_ear_code_word_missing"]))

    def test_forced_deaf_not_switched_is_red(self):
        r = self.deaf(audio_source="primary")
        self.assertEqual((r["status"], r["reasons"]), (lib.RED, ["second_ear_not_used"]))

    def test_forced_deaf_bot_left_by_itself_is_red(self):
        r = self.deaf(end_reason="deaf_with_participants")
        self.assertEqual((r["status"], r["reasons"]), (lib.RED, ["bot_left_early"]))
        self.assertIn("deaf_with_participants", lib.human_reasons({**r, "status": lib.RED}))
        self.assertFalse(r["checks"]["stayed_until_leave"])

    def test_forced_deaf_rule_not_live_is_yellow(self):
        r = self.deaf(deaf_event_live=False)
        self.assertEqual((r["status"], r["reasons"]), (lib.YELLOW, ["deaf_rule_not_live"]))

    def test_forced_deaf_ear_transcribe_failed_is_yellow(self):
        r = self.deaf(ear_code_word_found=None)
        self.assertEqual((r["status"], r["reasons"]), (lib.YELLOW, ["transcribe_failed"]))


class SecondEarCycleTest(_Env):
    def bot_cfg(self, w):
        cmd = next(c for c in w.docker_cmds if "--name" in c and "-bot-" in c[c.index("--name") + 1])
        return json.loads(next(a for a in cmd if a.startswith("BOT_CONFIG="))[len("BOT_CONFIG="):])

    def speaker_cfg(self, w):
        cmd = next(c for c in w.docker_cmds if "-speaker-" in c[c.index("--name") + 1])
        return json.loads(next(a for a in cmd if a.startswith("SPEAKER_CONFIG="))[len("SPEAKER_CONFIG="):])

    def test_force_deaf_live_check_green(self):
        w = World(runs=[dict(ear=True, volume=QUIET, transcript="", ear_transcript=EAR_TEXT,
                             audio_source="second_ear", deaf_event_sec=60,
                             ear_volume=(-28.0, -5.0), participants=["Тест-диктор"])])
        with mock.patch.dict(os.environ, {lib.FORCE_DEAF_ENV: "1"}):
            res = self.cycle(w, image="vexa-bot:resil-p4", reason="manual")
        self.assertEqual(res["status"], lib.GREEN, res.get("reasons"))
        self.assertTrue(res["force_deaf"])
        self.assertTrue(self.bot_cfg(w)["telemostForceDeaf"])
        self.assertEqual(self.speaker_cfg(w)["plays"], lib.SPEAK_PLAYS_DEAF)
        # Основная заглушена — расшифровывается только второе ухо.
        self.assertEqual(w.transcribed_files, ["2026-09-26-tm-1.chunk0.wav"])
        self.assertTrue(res["checks"]["deaf_rule_live"])
        self.assertTrue(res["checks"]["second_ear"]["code_word"])
        # Тестовый образ — боевое состояние не тронуто.
        self.assertEqual(lib.load_health(), {})

    def test_force_deaf_waits_for_bot_to_declare_then_leaves(self):
        w = World(runs=[dict(ear=True, volume=QUIET, transcript="", ear_transcript=EAR_TEXT,
                             ear_volume=(-28.0, -5.0), audio_source="second_ear", deaf_event_sec=None,
                             participants=["Тест-диктор"])])
        with mock.patch.dict(os.environ, {lib.FORCE_DEAF_ENV: "1"}):
            res = self.cycle(w, image="vexa-bot:resil-p4", reason="manual")
        self.assertEqual(res["status"], lib.YELLOW)
        self.assertEqual(res["reasons"], ["deaf_rule_not_live"])
        self.assertIn("deaf_event_timeout", res["notes"])
        self.assertTrue(w.published, "бот выведен командой leave")

    def test_force_deaf_ignored_on_production_image(self):
        w = World(runs=[dict(ear=True, participants=["Тест-диктор"])])
        with mock.patch.dict(os.environ, {lib.FORCE_DEAF_ENV: "1"}):
            res = self.cycle(w)
        self.assertNotIn("telemostForceDeaf", self.bot_cfg(w))
        self.assertEqual(self.speaker_cfg(w)["plays"], lib.SPEAK_PLAYS)
        self.assertEqual(res["status"], lib.GREEN)
        self.assertNotIn("force_deaf", res)

    def test_force_deaf_off_by_default(self):
        w = World(runs=[dict(ear=True, participants=["Тест-диктор"])])
        os.environ.pop(lib.FORCE_DEAF_ENV, None)
        self.cycle(w, image="vexa-bot:resil-p4")
        self.assertNotIn("telemostForceDeaf", self.bot_cfg(w))

    def test_primary_deaf_rescued_by_ear_is_yellow_on_production(self):
        # Настоящая поломка основной на боевом образе: бот сам переключился на второе ухо.
        w = World(runs=[dict(ear=True, volume=QUIET, transcript="", ear_transcript=EAR_TEXT,
                             audio_source="second_ear", ear_volume=(-28.0, -5.0),
                             participants=["Тест-диктор"])])
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.YELLOW)
        self.assertIn("primary_deaf", res["reasons"])
        self.assertEqual(lib.load_health()["status"], lib.YELLOW)
        self.assertEqual(len(w.pushes), 1)
        self.assertIn("втором ухе", w.pushes[0][0])
        self.assertIn("2026-09-26-tm-1.chunk0.wav", w.transcribed_files)

    def test_ear_does_not_mask_deaf_primary(self):
        # Второе ухо слышит диктора, а бот протокол не переключил — это красный, как раньше.
        w = World(runs=[dict(ear=True, volume=QUIET, transcript="", ear_transcript=EAR_TEXT,
                             ear_volume=(-28.0, -5.0), participants=["Тест-диктор"])] * 2)
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.RED)
        self.assertEqual(res["reasons"][0], "silence")

    def test_healthy_run_does_not_transcribe_ear(self):
        w = World(runs=[dict(ear=True, participants=["Тест-диктор"])])
        res = self.cycle(w)
        self.assertEqual(res["status"], lib.GREEN)
        self.assertEqual(w.transcribed_files, ["2026-09-26-tm-1.chunk1.wav"])
        self.assertEqual(res["metrics"]["ear_recording_sec"], 45.0)


def iso_(dt):
    return dt.isoformat()


if __name__ == "__main__":
    unittest.main()
