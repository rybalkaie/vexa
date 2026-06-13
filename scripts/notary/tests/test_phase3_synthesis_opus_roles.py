"""Ф3 (umnyi-protokol-assemblyai) — быстрые победы синтеза: Opus + роли + методичка.

Покрывает REQ плана `plans/2026-06-13-umnyi-protokol-assemblyai.md`, Фаза 3:
  - G1  — модель генерации протокола = Opus 4.8 (env-override), уходит в `--model`.
  - РИСК1 — таймаут Opus: дефолт ≥600s; деградация на резервную модель вместо
           молчаливой потери протокола; без фолбэка — понятный отказ (не тихий None).
  - G2  — блок «роли участников» (имя → зона) в user-промпте, отфильтрован к
           присутствующим, company-scoped; нет ростера → нет блока.
  - G3  — методичка доходит до system-промпта ЦЕЛИКОМ (сверка длины, без среза).
  - G10 — приватность: текст транскрипта и имена ролей НЕ логируются (метаданные).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase3_synthesis_opus_roles -v
"""
from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import llm_postprocess as lp  # noqa: E402
from lib import series_roster as sr  # noqa: E402
from lib.claude_cli import ClaudeCliTimeout, ClaudeCliFailed  # noqa: E402


# Минимальный валидный протокол (должен начинаться с `#протоколвстречи`).
_FAKE_PROTOCOL = (
    "#протоколвстречи 09.06.2026\n\n"
    "**Встреча:** Координация.\n\n"
    "**Длительность:** 25 мин\n\n"
    "**Участники:** Мария Михина, Сона Енгибарян\n\n"
    "**Транскрипт:** [2026-06-09.md](2026-06-09.md)\n\n"
    "---\n\n"
    "## 1) Поставки\n\n"
    "▪️ Контейнер на таможне.\n"
)

# Метаданные встречи Anzhee-координации (ростер захардкожен под этот slug).
_SERIES = sr.ANZHEE_COORDINATION_SLUG
_ROSTER_NAMES = ["Мария Михина", "Сона Енгибарян", "Михаил Саргин"]
_META = {
    "series": _SERIES,
    "date": "2026-06-09",
    "duration": 25,
    "expectedParticipants": _ROSTER_NAMES,
    "participants": _ROSTER_NAMES,
}


def _capture_call():
    """side_effect для `call_claude_print`: пишет аргументы вызова и отдаёт протокол."""
    captured: dict = {}

    def _fake(user_prompt, *, system, timeout, model):
        captured["user"] = user_prompt
        captured["system"] = system
        captured["timeout"] = timeout
        captured["model"] = model
        return _FAKE_PROTOCOL

    return _fake, captured


# ---------------------------------------------------------------------------
# G1 — модель генерации = Opus 4.8
# ---------------------------------------------------------------------------
class TestGenModelOpus(unittest.TestCase):

    @unittest.skipIf(
        os.environ.get("PROTOCOL_GEN_MODEL"),
        "PROTOCOL_GEN_MODEL переопределён через env — дефолт не проверить",
    )
    def test_default_model_is_opus(self):
        self.assertEqual(lp.PROTOCOL_GEN_MODEL, "claude-opus-4-8")

    def test_model_passed_to_cli(self):
        fake, cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.generate_protocol("транскрипт", _META, method_text="МЕТОД", meeting_sid="g1")
        self.assertEqual(cap["model"], lp.PROTOCOL_GEN_MODEL)


# ---------------------------------------------------------------------------
# РИСК1 — таймаут Opus: дефолт ≥600 + деградация
# ---------------------------------------------------------------------------
class TestTimeoutAndDegradation(unittest.TestCase):

    def test_default_timeout_at_least_600(self):
        self.assertGreaterEqual(lp.PROTOCOL_GEN_TIMEOUT, 600)

    def test_timeout_passed_to_cli_at_least_600(self):
        fake, cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.generate_protocol("т", _META, method_text="МЕТОД", meeting_sid="r1")
        self.assertGreaterEqual(cap["timeout"], 600)

    def test_explicit_timeout_respected(self):
        fake, cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.generate_protocol("т", _META, method_text="М", timeout=120, meeting_sid="r1b")
        self.assertEqual(cap["timeout"], 120)

    def test_timeout_degrades_to_fallback_model(self):
        """Opus упал по таймауту → один проход на резервной модели, встреча НЕ потеряна."""
        calls = []

        def _fake(user_prompt, *, system, timeout, model):
            calls.append(model)
            if model == lp.PROTOCOL_GEN_MODEL:
                raise ClaudeCliTimeout("timeout 600s")
            return _FAKE_PROTOCOL

        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", "claude-sonnet-4-6"), \
                mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            out = lp.generate_protocol("т", _META, method_text="М", meeting_sid="r2")
        self.assertTrue(out.startswith("#протоколвстречи"))
        # Первая попытка — основная модель, вторая — фолбэк.
        self.assertEqual(calls[0], lp.PROTOCOL_GEN_MODEL)
        self.assertEqual(calls[1], "claude-sonnet-4-6")

    def test_degradation_logged_with_marker(self):
        """Деградация видна в structured-логе (`degraded`), без текста транскрипта."""
        def _fake(user_prompt, *, system, timeout, model):
            if model == lp.PROTOCOL_GEN_MODEL:
                raise ClaudeCliTimeout("timeout")
            return _FAKE_PROTOCOL

        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", "claude-sonnet-4-6"), \
                mock.patch.object(lp, "call_claude_print", side_effect=_fake), \
                self.assertLogs(lp.logger, level="WARNING") as logctx:
            lp.generate_protocol("т", _META, method_text="М", meeting_sid="r3")
        blob = "\n".join(logctx.output)
        self.assertIn("degraded", blob)
        self.assertIn("reason=timeout", blob)

    def test_timeout_without_fallback_raises(self):
        """Фолбэк выключен → понятный отказ (не тихий None), встреча перегенерится позже."""
        def _fake(user_prompt, *, system, timeout, model):
            raise ClaudeCliTimeout("timeout")

        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", ""), \
                mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            with self.assertRaises(lp.ProtocolGenerationError):
                lp.generate_protocol("т", _META, method_text="М", meeting_sid="r4")

    def test_fallback_failure_raises_not_silent(self):
        """Если и фолбэк упал — ProtocolGenerationError, не тихий None."""
        def _fake(user_prompt, *, system, timeout, model):
            if model == lp.PROTOCOL_GEN_MODEL:
                raise ClaudeCliTimeout("timeout")
            raise ClaudeCliFailed("fallback boom")

        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", "claude-sonnet-4-6"), \
                mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            with self.assertRaises(lp.ProtocolGenerationError):
                lp.generate_protocol("т", _META, method_text="М", meeting_sid="r5")

    def test_non_timeout_error_unchanged(self):
        """Не-таймаут-ошибка ведёт себя как до Ф3: отказ без фолбэка."""
        def _fake(user_prompt, *, system, timeout, model):
            raise ClaudeCliFailed("exit 1")

        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", "claude-sonnet-4-6"), \
                mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            with self.assertRaises(lp.ProtocolGenerationError):
                lp.generate_protocol("т", _META, method_text="М", meeting_sid="r6")


# ---------------------------------------------------------------------------
# G2 — блок ролей участников в user-промпте
# ---------------------------------------------------------------------------
class TestRolesBlock(unittest.TestCase):

    def test_roles_block_in_user_prompt(self):
        prompt = lp._format_protocol_user_prompt("транскрипт", _META)
        self.assertIn("Роли участников этой серии", prompt)
        self.assertIn("Мария Михина — поставки", prompt)
        self.assertIn("Сона Енгибарян — коммерция", prompt)

    def test_roles_filtered_to_present(self):
        """Только присутствующие получают роль — отсутствующий владелец домена не в блоке."""
        meta = dict(_META)
        meta["participants"] = ["Мария Михина"]  # из ростера присутствует одна
        meta["expectedParticipants"] = ["Мария Михина"]
        prompt = lp._format_protocol_user_prompt("транскрипт", meta)
        self.assertIn("Мария Михина — поставки", prompt)
        self.assertNotIn("Сона Енгибарян", prompt)
        self.assertNotIn("Ольга Новикова", prompt)

    def test_no_roster_no_block(self):
        """Незнакомая серия → нет ростера → нет блока ролей, промпт валиден."""
        meta = dict(_META)
        meta["series"] = "series-unknown-deadbeef"
        prompt = lp._format_protocol_user_prompt("транскрипт", meta)
        self.assertNotIn("Роли участников этой серии", prompt)
        self.assertIn("Транскрипт:", prompt)

    def test_roles_block_reaches_generate_protocol(self):
        """End-to-end: блок ролей доходит до user-промпта генерации."""
        fake, cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.generate_protocol("транскрипт", _META, method_text="М", meeting_sid="g2")
        self.assertIn("Роли участников этой серии", cap["user"])
        self.assertIn("Мария Михина — поставки", cap["user"])

    def test_format_roles_block_unit(self):
        roster = [
            {"name": "Мария Михина", "domain": "поставки", "keywords": ["поставк", "груз"]},
            {"name": "Сона Енгибарян", "domain": "коммерция", "keywords": ["продаж"]},
        ]
        block = sr.format_roles_block(roster)
        self.assertIn("- Мария Михина — поставки", block)
        self.assertIn("- Сона Енгибарян — коммерция", block)
        # keywords — лексика ASR-маппинга, в блок генерации НЕ кладём (шум).
        # Проверяем по стемам, которых нет в ярлыках доменов («груз»/«продаж»).
        self.assertNotIn("груз", block)
        self.assertNotIn("продаж", block)

    def test_format_roles_block_empty(self):
        self.assertEqual(sr.format_roles_block([]), "")


# ---------------------------------------------------------------------------
# G3 — методичка доходит до system-промпта целиком
# ---------------------------------------------------------------------------
class TestMethodTextFull(unittest.TestCase):

    def test_method_text_full_in_system_prompt(self):
        """Сверка длины: весь method_text — подстрока system-промпта (без среза)."""
        # Длинная методичка с уникальными краевыми маркерами (поймает усечение
        # с любого конца).
        method = (
            "НАЧАЛО-МЕТОДИЧКИ-уникальный-маркер\n"
            + "\n".join(f"Правило {i}: соблюдай стандарт оформления протокола." for i in range(400))
            + "\nКОНЕЦ-МЕТОДИЧКИ-уникальный-маркер"
        )
        fake, cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.generate_protocol("т", _META, method_text=method, meeting_sid="g3")
        # Целиком: и начало, и конец, и полная подстрока.
        self.assertIn("НАЧАЛО-МЕТОДИЧКИ-уникальный-маркер", cap["system"])
        self.assertIn("КОНЕЦ-МЕТОДИЧКИ-уникальный-маркер", cap["system"])
        self.assertIn(method, cap["system"])

    def test_real_method_file_loads_full(self):
        """`_load_method_text` отдаёт непустой файл целиком (если найден на этой машине)."""
        try:
            text = lp._load_method_text()
        except lp.ProtocolGenerationError:
            self.skipTest("метод-файл не найден на этой машине (headless/VPS без rsync)")
        self.assertGreater(len(text), 200)
        fake, cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.generate_protocol("т", _META, method_text=text, meeting_sid="g3b")
        self.assertIn(text, cap["system"])


# ---------------------------------------------------------------------------
# G10 — приватность: текст транскрипта и имена ролей не логируются
# ---------------------------------------------------------------------------
class TestPrivacyNoLeak(unittest.TestCase):

    def test_transcript_and_roles_not_logged(self):
        secret_transcript = "СЕКРЕТ-РЕПЛИКА-не-должна-утечь-в-лог"
        fake, _cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake), \
                self.assertLogs(lp.logger, level="DEBUG") as logctx:
            lp.generate_protocol(secret_transcript, _META, method_text="М", meeting_sid="g10")
        blob = "\n".join(logctx.output)
        # Текст транскрипта не в логах.
        self.assertNotIn(secret_transcript, blob)
        # Имена ролей (из блока ролей) не в логах.
        for name in _ROSTER_NAMES:
            self.assertNotIn(name, blob)
        # Но метаданные (модель) есть — лог не пустой и полезный.
        self.assertIn("model=", blob)

    def test_degraded_path_does_not_leak(self):
        """Путь деградации тоже не логирует транскрипт/имена ролей."""
        secret = "СЕКРЕТ-РЕПЛИКА-деградация"

        def _fake(user_prompt, *, system, timeout, model):
            if model == lp.PROTOCOL_GEN_MODEL:
                raise ClaudeCliTimeout("timeout")
            return _FAKE_PROTOCOL

        with mock.patch.object(lp, "PROTOCOL_GEN_FALLBACK_MODEL", "claude-sonnet-4-6"), \
                mock.patch.object(lp, "call_claude_print", side_effect=_fake), \
                self.assertLogs(lp.logger, level="DEBUG") as logctx:
            lp.generate_protocol(secret, _META, method_text="М", meeting_sid="g10b")
        blob = "\n".join(logctx.output)
        self.assertNotIn(secret, blob)
        for name in _ROSTER_NAMES:
            self.assertNotIn(name, blob)


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
