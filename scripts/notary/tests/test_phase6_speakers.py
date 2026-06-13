"""Тесты Ф6 — распознавание «кто говорил»: склейка и дробление.

Покрывает REQ 6.1–6.2 плана `2026-06-03-dorabotki-bot-notarius-full.md`:
  - 6.1  мягкая подсказка состава в Speechmatics job-конфиг (НЕ жёсткий лимит):
         _build_speaker_diarization_config, прокидывание в _submit_job,
         незапланированный участник не форсируется (нет max_speakers).
  - 6.2  объединённый ревью-проход checks=("values","roles") — ОДИН claude-вызов,
         roles-секция в system-prompt, флаг на спикера, фильтр по секции.
  - долг Ф5: ⚠️-пометки переживают позднюю ревизию (clarify_worker._apply_resolution).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase6_speakers -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

# speechmatics_client требует httpx (есть в venv VPS). На маке/CI системный
# python его может не иметь — тестируемые здесь функции (`_build_speaker_…`,
# `_submit_job` с мок-клиентом) реальный httpx не используют, поэтому при его
# отсутствии подкладываем минимальный стаб. Если httpx установлен — берём его.
try:  # noqa: SIM105
    import httpx  # noqa: F401
except ModuleNotFoundError:
    _stub = types.ModuleType("httpx")

    class _HttpxError(Exception):
        pass

    class _HTTPStatusError(_HttpxError):
        def __init__(self, *a, response=None, **k):
            super().__init__(*a)
            self.response = response

    _stub.TimeoutException = type("TimeoutException", (_HttpxError,), {})
    _stub.ConnectError = type("ConnectError", (_HttpxError,), {})
    _stub.RemoteProtocolError = type("RemoteProtocolError", (_HttpxError,), {})
    _stub.RequestError = type("RequestError", (_HttpxError,), {})
    _stub.HTTPStatusError = _HTTPStatusError
    _stub.Client = type("Client", (), {})
    sys.modules["httpx"] = _stub

from lib import speechmatics_client as sc  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402
from lib import clarify_worker as cw  # noqa: E402


# ==========================================================================
# 6.1 — мягкая подсказка состава для диаризации (НЕ жёсткий лимит)
# ==========================================================================
class TestBuildSpeakerDiarizationConfig(unittest.TestCase):

    def setUp(self):
        # Изоляция от env, который мог утечь из окружения прогона.
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("SPEECHMATICS_DIARIZATION_HINT", "SPEECHMATICS_SPEAKER_SENSITIVITY")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_none_or_unknown_roster_no_config(self):
        """Состав неизвестен → не вмешиваемся (дефолт API)."""
        self.assertEqual(sc._build_speaker_diarization_config(None), {})
        self.assertEqual(sc._build_speaker_diarization_config(0), {})

    def test_solo_meeting_no_config(self):
        """Соло-встреча (1 ожидаемый) → не форсируем дробление."""
        self.assertEqual(sc._build_speaker_diarization_config(1), {})

    def test_two_speakers_bumps_sensitivity(self):
        """K=2 (кейс Ольга+Дарья) → sensitivity выше дефолта 0.5, чтобы НЕ склеивать."""
        cfg = sc._build_speaker_diarization_config(2)
        self.assertAlmostEqual(cfg["speaker_sensitivity"], 0.6)

    def test_sensitivity_scales_and_clamps(self):
        self.assertAlmostEqual(sc._build_speaker_diarization_config(3)["speaker_sensitivity"], 0.65)
        self.assertAlmostEqual(sc._build_speaker_diarization_config(4)["speaker_sensitivity"], 0.7)
        # Потолок 0.7 — не уезжаем в переосегментацию (дробление одного человека).
        self.assertAlmostEqual(sc._build_speaker_diarization_config(8)["speaker_sensitivity"], 0.7)
        self.assertAlmostEqual(sc._build_speaker_diarization_config(20)["speaker_sensitivity"], 0.7)

    def test_never_emits_hard_cap(self):
        """РИСК3: НИКОГДА не ставим max_speakers — иначе гость склеится принудительно."""
        for k in (None, 1, 2, 3, 5, 10, 50):
            cfg = sc._build_speaker_diarization_config(k)
            self.assertNotIn("max_speakers", cfg, f"max_speakers просочился при K={k}")
            self.assertNotIn("speakers", cfg)

    def test_killswitch_disables(self):
        os.environ["SPEECHMATICS_DIARIZATION_HINT"] = "0"
        self.assertEqual(sc._build_speaker_diarization_config(4), {})

    def test_env_override_fixed_sensitivity(self):
        os.environ["SPEECHMATICS_SPEAKER_SENSITIVITY"] = "0.9"
        cfg = sc._build_speaker_diarization_config(2)
        self.assertAlmostEqual(cfg["speaker_sensitivity"], 0.9)
        self.assertNotIn("max_speakers", cfg)

    def test_env_override_garbage_falls_through(self):
        os.environ["SPEECHMATICS_SPEAKER_SENSITIVITY"] = "не число"
        # Кривой env игнорируется → авто-нудж по K.
        cfg = sc._build_speaker_diarization_config(3)
        self.assertAlmostEqual(cfg["speaker_sensitivity"], 0.65)


class TestHintReachesJobConfig(unittest.TestCase):
    """6.1 — подсказка реально попадает в transcription_config, отправляемый Speechmatics."""

    def _capture_config(self, expected_speakers):
        captured = {}

        def _fake_post(url, headers=None, files=None):
            cfg_field = files["config"]  # (None, json_str, "application/json")
            captured["config"] = json.loads(cfg_field[1])
            resp = mock.MagicMock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"id": "job-test"}
            return resp

        client = mock.MagicMock()
        client.post.side_effect = _fake_post
        with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
            tf.write(b"RIFFxxxxWAVE")
            tf.flush()
            with mock.patch.object(sc, "_load_additional_vocab", return_value=[]):
                job_id = sc._submit_job(
                    client, {"Authorization": "Bearer x"}, Path(tf.name),
                    expected_speakers=expected_speakers,
                )
        self.assertEqual(job_id, "job-test")
        return captured["config"]

    def test_hint_present_for_multi_roster(self):
        cfg = self._capture_config(3)
        tc = cfg["transcription_config"]
        self.assertEqual(tc["diarization"], "speaker")
        self.assertIn("speaker_diarization_config", tc)
        self.assertAlmostEqual(tc["speaker_diarization_config"]["speaker_sensitivity"], 0.65)
        # РИСК3: незапланированный участник НЕ форсируется — нет жёсткого лимита.
        self.assertNotIn("max_speakers", tc["speaker_diarization_config"])

    def test_no_hint_when_roster_unknown(self):
        cfg = self._capture_config(None)
        tc = cfg["transcription_config"]
        self.assertNotIn("speaker_diarization_config", tc)


# ==========================================================================
# 6.2 — объединённый ревью-проход values+roles (ОДИН claude-вызов)
# ==========================================================================
class TestRolesReviewPrompt(unittest.TestCase):

    def test_roles_section_added_when_requested(self):
        sp = lp._build_review_system_prompt(("values", "roles"))
        self.assertIn('"roles"', sp)
        self.assertIn("смешан", sp)          # «смешаны роли/темы»
        self.assertIn("финансы", sp)         # пример зоны
        # values-секция тоже на месте — это ОДИН проход с двумя секциями.
        self.assertIn("число", sp)

    def test_roles_section_absent_without_check(self):
        sp = lp._build_review_system_prompt(("values",))
        self.assertNotIn("смешан", sp)


class TestRolesFlagApplication(unittest.TestCase):
    PROTOCOL = """#протоколвстречи 02.06.2026

**Участники:** Ольга, Дарья

---

## 1) Финансы

▪️ Оборот за месяц **2,8 млн**.

## 2) Кадры

▪️ Ищем кладовщика.
"""

    def test_roles_finding_goes_to_speaker_block(self):
        findings = [{"section": "roles", "quote": "Ольга", "note": "смешаны финансы и кадры"}]
        out = lp.apply_review_flags(self.PROTOCOL, findings)
        self.assertIn("## ⚠️ Проверить", out)
        # Спикер-уровневый формат: «⚠️ <спикер>: <что смешано>».
        self.assertIn("⚠️ Ольга: смешаны финансы и кадры", out)
        # roles НЕ лепится inline на случайную строку с «Ольга» в шапке участников.
        header = [l for l in out.split("\n") if l.startswith("**Участники:**")][0]
        self.assertNotIn("⚠️", header)

    def test_mixed_values_inline_roles_in_block(self):
        findings = [
            {"section": "values", "quote": "2,8 млн", "note": "сверь число"},
            {"section": "roles", "quote": "Дарья", "note": "смешаны продажи и склад"},
        ]
        out = lp.apply_review_flags(self.PROTOCOL, findings)
        # values — inline на строке с числом.
        vline = [l for l in out.split("\n") if "2,8 млн" in l][0]
        self.assertIn("⚠️ проверь: сверь число", vline)
        # roles — в хвостовом блоке.
        self.assertIn("⚠️ Дарья: смешаны продажи и склад", out)

    def test_roles_flag_idempotent(self):
        findings = [{"section": "roles", "quote": "Ольга", "note": "смешаны финансы и кадры"}]
        once = lp.apply_review_flags(self.PROTOCOL, findings)
        twice = lp.apply_review_flags(once, findings)
        self.assertEqual(twice.count("⚠️ Ольга: смешаны финансы и кадры"), 1)
        self.assertEqual(twice.count("## ⚠️ Проверить"), 1)


class TestParseRolesFinding(unittest.TestCase):

    def test_parse_keeps_roles_section(self):
        raw = json.dumps({"findings": [
            {"section": "roles", "quote": "Ольга", "note": "смешаны финансы и кадры"},
        ]})
        out = lp._parse_review_response(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["section"], "roles")
        self.assertEqual(out[0]["quote"], "Ольга")


class TestCombinedReviewSingleClaudeCall(unittest.TestCase):
    """Ключевой инвариант Ф6: values+roles = ОДИН claude-вызов, не два."""

    def test_one_call_two_sections(self):
        fake = json.dumps({"findings": [
            {"section": "values", "quote": "28 млн", "note": "сверь число"},
            {"section": "roles", "quote": "Ольга", "note": "смешаны финансы и кадры"},
        ]})
        with mock.patch.object(lp, "call_claude_print", return_value=fake) as m:
            out = lp.review_protocol(
                "протокол", "транскрипт",
                checks=("values", "roles"), meeting_sid="x",
            )
        self.assertEqual(m.call_count, 1)  # НЕ два прохода
        sections = sorted(f["section"] for f in out)
        self.assertEqual(sections, ["roles", "values"])

    def test_roles_section_in_system_prompt_of_call(self):
        with mock.patch.object(lp, "call_claude_print", return_value='{"findings": []}') as m:
            lp.review_protocol("p", "t", checks=("values", "roles"), meeting_sid="x")
        # system-prompt вызова содержит обе секции.
        system = m.call_args.kwargs.get("system") or (m.call_args.args[1] if len(m.call_args.args) > 1 else "")
        self.assertIn("число", system)
        self.assertIn("смешан", system)


# ==========================================================================
# Долг Ф5: ⚠️-пометки переживают позднюю ревизию
# ==========================================================================
class TestReviewSurvivesRevision(unittest.TestCase):

    def _make_state(self, td: Path):
        transcript = td / "2026-06-02.md"
        transcript.write_text("**[00:01] Ольга:** привет\n", encoding="utf-8")
        protocol = td / "2026-06-02-protokol.md"
        protocol.write_text("#протоколвстречи 02.06.2026\nтело\n", encoding="utf-8")
        (td / "meta.json").write_text("{}", encoding="utf-8")
        state = {
            "meeting_id": "m1",
            "transcript_path": str(transcript),
            "unclear_clusters": {"SPEAKER_00": {"speaker_label_in_md": "Спикер 1"}},
            "name_pool": ["Ольга", "Дарья"],
            "meta": {"series": "s", "date": "2026-06-02", "sessionUid": "u"},
            "status": "pending",
        }
        return state

    def test_review_reruns_on_regen_before_redeliver(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = self._make_state(tdp)
            mapping = {"SPEAKER_00": "Ольга"}
            order: list = []

            def _regen(**k):
                order.append("regen")

            def _review(**k):
                order.append(("review", k.get("checks")))
                return 1

            def _redeliver(*a, **k):
                order.append("redeliver")
                return {"status": "sent"}

            with mock.patch.object(cw.llm_postprocess, "apply_clarify_mapping_to_transcript", return_value=True), \
                 mock.patch.object(cw.llm_postprocess, "_save_protocol_version"), \
                 mock.patch.object(cw.llm_postprocess, "regenerate_protocol_for_meeting", side_effect=_regen), \
                 mock.patch.object(cw.llm_postprocess, "review_and_flag_protocol_file", side_effect=_review) as rev, \
                 mock.patch.object(cw.llm_postprocess, "redeliver_revised_protocol", side_effect=_redeliver), \
                 mock.patch.object(cw.clarify_state, "mark_status"), \
                 mock.patch.object(cw.clarify_state, "now_iso", return_value="2026-06-02T00:00:00Z"):
                status = cw._apply_resolution(state, mapping, via="button", pending_root=tdp)

            self.assertEqual(status, "sent")
            self.assertTrue(rev.called)
            # Объединённый проход на ревизии — те же checks, что в finalize.
            # Ф7 расширил набор: + "memory" (дисциплина «прошлое = справка»); Ф4: +
            # "diarization" (поздний реген не должен терять ⚠️ «спикер под вопросом»).
            # Всё тем же ОДНИМ вызовом (НЕ отдельный проход).
            self.assertEqual(rev.call_args.kwargs.get("checks"), ("values", "roles", "memory", "diarization"))
            # Порядок: регенерация → ревью (вернуло ⚠️) → до-сыл.
            simplified = [c if isinstance(c, str) else c[0] for c in order]
            self.assertEqual(simplified, ["regen", "review", "redeliver"])

    def test_no_review_when_transcript_not_updated(self):
        """Если transcript не изменился — нет регена и нет ревью/до-сыла."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = self._make_state(tdp)
            mapping = {"SPEAKER_00": "Ольга"}
            with mock.patch.object(cw.llm_postprocess, "apply_clarify_mapping_to_transcript", return_value=False), \
                 mock.patch.object(cw.llm_postprocess, "review_and_flag_protocol_file") as rev, \
                 mock.patch.object(cw.llm_postprocess, "redeliver_revised_protocol") as red, \
                 mock.patch.object(cw.clarify_state, "mark_status"), \
                 mock.patch.object(cw.clarify_state, "now_iso", return_value="2026-06-02T00:00:00Z"):
                cw._apply_resolution(state, mapping, via="button", pending_root=tdp)
            rev.assert_not_called()
            red.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
