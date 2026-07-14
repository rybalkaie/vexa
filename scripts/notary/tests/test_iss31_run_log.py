# -*- coding: utf-8 -*-
"""ISS-31 / Ф1: per-meeting durable-лог причин сбоя генерации + опасная тройка.

Что проверяем:
  1. `run_log.append_event` пишет метадата-строку в `<dir>/<sid>.log`; sid с `/`
     санитизируется; None-поля пропускаются; строковые значения режутся по длине.
  2. Ретенция: старые (> N дней) per-meeting логи удаляются лениво при записи.
  3. Сбой генерации (`output-without-header`) через `generate_protocol` пишет
     причину в per-meeting лог, и текст реплик/транскрипта в лог НЕ попадает
     (опасная тройка).
  4. Успешный прогон пишет `event=ok`.
  5. Опасная тройка (AST): вызовы run-лога в `llm_postprocess.py` не передают
     переменную с текстом транскрипта/протокола как значение поля.
  6. `collector._finalize_stderr_warnings`: при rc=0 отбираются WARNING/ERROR/
     `[protocol]`-строки stderr (метаданные), не весь поток.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import run_log  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402
import collector  # noqa: E402


# Секретная фраза-маркер: если попадёт в лог — опасная тройка нарушена.
_SECRET = "SEKRETNAYA-REPLIKA-XYZ-777"
_TRANSCRIPT = f"**[00:01] Иван:** {_SECRET} обсудили финансы и HR очень подробно."


class RunLogAppendTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"MEETING_NOTARY_RUN_LOG_DIR": self._tmp.name}
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        run_log._pruned_this_process = False  # чистый прайм на каждый тест

    def test_writes_metadata_line(self):
        run_log.append_event(
            "sid-abc", "failed", reason="output-without-header",
            model="claude-opus-4-8", prompt_len=1234, segments=7, output_len=42,
        )
        p = Path(self._tmp.name) / "sid-abc.log"
        self.assertTrue(p.exists())
        content = p.read_text(encoding="utf-8")
        self.assertIn("event=failed", content)
        self.assertIn("reason=output-without-header", content)
        self.assertIn("prompt_len=1234", content)
        self.assertIn("segments=7", content)

    def test_sid_with_slash_sanitized(self):
        run_log.append_event("weird/sid\\x", "ok")
        files = list(Path(self._tmp.name).glob("*.log"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("/", files[0].name)

    def test_none_field_skipped(self):
        run_log.append_event("sid1", "ok", stop_reason=None, model="opus")
        content = (Path(self._tmp.name) / "sid1.log").read_text(encoding="utf-8")
        self.assertNotIn("stop_reason", content)
        self.assertIn("model=opus", content)

    def test_string_value_capped_and_single_line(self):
        run_log.append_event("sid2", "ok", reason="A" * 500 + "\nВТОРАЯ СТРОКА")
        content = (Path(self._tmp.name) / "sid2.log").read_text(encoding="utf-8")
        # ровно одна строка-запись (нет инъекции второй строки через \n)
        self.assertEqual(len([ln for ln in content.splitlines() if ln.strip()]), 1)
        self.assertNotIn("ВТОРАЯ СТРОКА", content)

    def test_never_raises_on_bad_dir(self):
        # Каталог под файлом → mkdir упадёт, но append не имеет права бросить.
        clash = Path(self._tmp.name) / "afile"
        clash.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_RUN_LOG_DIR": str(clash)}):
            run_log._pruned_this_process = False
            run_log.append_event("sid3", "ok")  # не бросает

    def test_retention_prunes_old_logs(self):
        old = Path(self._tmp.name) / "old-sid.log"
        old.write_text("stale\n", encoding="utf-8")
        old_ts = time.time() - 40 * 86400  # 40 дней назад > дефолт 30
        os.utime(old, (old_ts, old_ts))
        run_log._pruned_this_process = False
        run_log.append_event("fresh-sid", "ok")  # запись триггерит прун
        self.assertFalse(old.exists(), "старый лог должен быть удалён ретенцией")
        self.assertTrue((Path(self._tmp.name) / "fresh-sid.log").exists())


class GenerateProtocolRunLogTest(unittest.TestCase):
    """Интеграция: сбой/успех генерации пишет причину в per-meeting лог."""

    _META = {
        "series": "test-iss31-series",
        "date": "2026-06-09",
        "duration": 25,
        "expectedParticipants": ["Иван"],
        "participants": ["Иван"],
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"MEETING_NOTARY_RUN_LOG_DIR": self._tmp.name}
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        run_log._pruned_this_process = False

    def test_output_without_header_logs_reason_and_no_transcript_leak(self):
        headerless = "Тут нет шапки протокола, просто текст ответа модели."
        with mock.patch.object(lp, "call_claude_print", return_value=headerless):
            with self.assertRaises(lp.ProtocolGenerationError):
                lp.generate_protocol(
                    _TRANSCRIPT, self._META, method_text="М", meeting_sid="fail-sid",
                )
        log = Path(self._tmp.name) / "fail-sid.log"
        self.assertTrue(log.exists(), "per-meeting лог должен появиться при сбое")
        content = log.read_text(encoding="utf-8")
        self.assertIn("event=failed", content)
        self.assertIn("reason=output-without-header", content)
        self.assertIn("segments=1", content)  # одна реплика в транскрипте
        # ОПАСНАЯ ТРОЙКА: текст реплики НЕ утёк в лог.
        self.assertNotIn(_SECRET, content)
        self.assertNotIn("финансы", content)

    def test_success_logs_ok(self):
        protocol = (
            "#протоколвстречи 09.06.2026\n\n"
            "**Встреча:** Тест.\n\n"
            "**Длительность:** 25 мин\n\n"
            "**Участники:** Иван\n\n"
            "**Транскрипт:** [2026-06-09.md](2026-06-09.md)\n\n"
            "---\n\n"
            "## 1) Тема\n\n▪️ Что-то произошло.\n"
        )
        with mock.patch.object(lp, "call_claude_print", return_value=protocol):
            out = lp.generate_protocol(
                _TRANSCRIPT, self._META, method_text="М", meeting_sid="ok-sid",
            )
        self.assertTrue(out.startswith("#протоколвстречи"))
        content = (Path(self._tmp.name) / "ok-sid.log").read_text(encoding="utf-8")
        self.assertIn("event=ok", content)
        self.assertNotIn(_SECRET, content)


class DangerTripleASTTest(unittest.TestCase):
    """AST-гард: вызовы run-лога не передают текст транскрипта/протокола."""

    # Имена переменных, несущих СЫРОЙ текст (реплики/протокол) — их нельзя
    # передавать как значение поля в per-meeting лог. `len(x)` от них разрешён
    # (это счётчик, не текст) — проверка исключает аргументы len().
    _FORBIDDEN = {
        "transcript_md", "transcript", "text", "raw", "user_prompt",
        "system_prompt", "protocol_text", "content", "body",
    }

    @staticmethod
    def _is_run_log_call(node: ast.Call) -> bool:
        f = node.func
        if isinstance(f, ast.Name) and f.id == "_emit_run_log":
            return True
        if (isinstance(f, ast.Attribute) and f.attr == "append_event"
                and isinstance(f.value, ast.Name) and f.value.id == "run_log"):
            return True
        return False

    def _forbidden_outside_len(self, value: ast.AST) -> list[str]:
        # Узлы-аргументы len(...) — их содержимое безопасно (возвращается int).
        len_arg_ids: set[int] = set()
        for n in ast.walk(value):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "len"):
                for a in n.args:
                    for sub in ast.walk(a):
                        len_arg_ids.add(id(sub))
        bad: list[str] = []
        for n in ast.walk(value):
            if (isinstance(n, ast.Name) and n.id in self._FORBIDDEN
                    and id(n) not in len_arg_ids):
                bad.append(n.id)
        return bad

    def test_llm_postprocess_run_log_calls_carry_no_text(self):
        src = (_NOTARY / "lib" / "llm_postprocess.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and self._is_run_log_call(n)]
        self.assertGreater(len(calls), 0, "run-лог вызовы должны существовать")
        for call in calls:
            values = list(call.args) + [kw.value for kw in call.keywords]
            for v in values:
                bad = self._forbidden_outside_len(v)
                self.assertEqual(
                    bad, [],
                    f"run-лог получает текстовую переменную {bad} "
                    f"(строка {getattr(call, 'lineno', '?')}) — опасная тройка",
                )


class CollectorStderrWarningsTest(unittest.TestCase):
    def test_picks_only_warning_and_protocol_lines(self):
        stderr = (
            "2026-07-14 [INFO] finalize-meeting: старт\n"
            "2026-07-14 [WARNING] llm_postprocess: [protocol] degraded meeting=s reason=timeout\n"
            "обычная болтовня без уровня\n"
            "2026-07-14 [ERROR] finalize-meeting: что-то упало\n"
        )
        out = collector._finalize_stderr_warnings(stderr)
        self.assertEqual(len(out), 2)
        self.assertTrue(any("degraded" in ln for ln in out))
        self.assertTrue(any("[ERROR]" in ln for ln in out))
        self.assertFalse(any("болтовня" in ln for ln in out))

    def test_empty_stderr(self):
        self.assertEqual(collector._finalize_stderr_warnings(""), [])
        self.assertEqual(collector._finalize_stderr_warnings(None), [])

    def test_limit_respected(self):
        stderr = "\n".join(f"[WARNING] строка {i}" for i in range(100))
        out = collector._finalize_stderr_warnings(stderr, limit=5)
        self.assertEqual(len(out), 5)


if __name__ == "__main__":
    unittest.main()
