"""Тесты Ф1 (ISS-28) R6: персист `docker logs` бот-контейнера в долговечный файл.

Закрепляют ЧИСТУЮ логику двух новых хелперов collectorّа (без docker/ssh/диска —
`ssh_capture` мокается), которой R6 решает «что и как дампить»:
  • collector._persist_container_log — форма shell-команды + injection-безопасность
    (shlex-quoting имени; санитайз имени файла) + best-effort (rc!=0/timeout → False);
  • collector._latest_exited_notarius_for_series — резолв имени контейнера серии
    (None-guard, парсинг newest-first, rc!=0/пусто → None).

Почему важно: команда уходит на ХОСТ через ssh — регрессия в quoting = инъекция/
поломка. Тест ловит это без живого docker. См. plans/notary-issues-log.md ISS-28.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss28_r6_persist -v
"""
from __future__ import annotations

import importlib.util
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, str(_NOTARY / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# collector.py транзитивно тянет notary.cli.registry → import yaml. Тестируемые
# функции yaml не трогают; под системным python3 (без PyYAML) кладём заглушку,
# чтобы модуль импортировался (как в test_concurrency_watchdog).
try:
    import yaml  # noqa: F401
except ImportError:
    import types as _types
    sys.modules["yaml"] = _types.ModuleType("yaml")

collector = _load("collector_module_r6", "collector.py")


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr)


class _SshSpy:
    """Записывает последнюю команду, отдаёт заранее заданный результат/исключение."""

    def __init__(self, result=None, raise_exc: BaseException | None = None):
        self.result = result if result is not None else _cp(0)
        self.raise_exc = raise_exc
        self.calls: list[str] = []

    def __call__(self, cmd: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


class TestPersistContainerLog(unittest.TestCase):
    def setUp(self):
        self._orig = collector.ssh_capture

    def tearDown(self):
        collector.ssh_capture = self._orig

    def test_empty_name_no_ssh(self):
        spy = _SshSpy(_cp(0))
        collector.ssh_capture = spy
        self.assertFalse(collector._persist_container_log(""))
        self.assertEqual(spy.calls, [])  # guard ДО ssh — наружу не ходим

    def test_ok_command_shape(self):
        spy = _SshSpy(_cp(0))
        collector.ssh_capture = spy
        name = "vexa-notarius-coord-20260707"
        self.assertTrue(collector._persist_container_log(name))
        self.assertEqual(len(spy.calls), 1)
        cmd = spy.calls[0]
        # mkdir -p <dir> && docker logs <name> > <dst> 2>&1
        self.assertIn("mkdir -p", cmd)
        self.assertIn("docker logs", cmd)
        self.assertIn(shlex.quote(name), cmd)         # имя в команде — заквочено
        self.assertIn("> ", cmd)
        self.assertTrue(cmd.rstrip().endswith("2>&1"))
        self.assertIn(f"{name}.log", cmd)             # обычное имя в путь попадает как есть

    def test_injection_name_is_quoted_and_sanitized(self):
        spy = _SshSpy(_cp(0))
        collector.ssh_capture = spy
        evil = "x; rm -rf / #"
        collector._persist_container_log(evil)
        cmd = spy.calls[0]
        # `docker logs` получает СЫРОЕ имя, но строго заквоченным (инъекции нет).
        self.assertIn(shlex.quote(evil), cmd)
        # путь файла — из САНИТИЗИРОВАННОГО имени: ни пробела, ни ;/#/слэша в basename.
        safe = collector._SAFE_CONTAINER_RE.sub("_", evil)
        self.assertNotIn(";", safe)
        self.assertNotIn(" ", safe)
        self.assertNotIn("/", safe)
        self.assertIn(f"{safe}.log", cmd)

    def test_rc_nonzero_returns_false(self):
        collector.ssh_capture = _SshSpy(_cp(1, stderr="boom"))
        self.assertFalse(collector._persist_container_log("vexa-notarius-x"))

    def test_timeout_returns_false(self):
        collector.ssh_capture = _SshSpy(raise_exc=subprocess.TimeoutExpired("cmd", 60))
        self.assertFalse(collector._persist_container_log("vexa-notarius-x"))


class TestLatestExitedNotariusForSeries(unittest.TestCase):
    def setUp(self):
        self._orig = collector.ssh_capture

    def tearDown(self):
        collector.ssh_capture = self._orig

    def test_none_series_no_ssh(self):
        spy = _SshSpy(_cp(0, stdout="anything\n"))
        collector.ssh_capture = spy
        self.assertIsNone(collector._latest_exited_notarius_for_series(None))
        self.assertEqual(spy.calls, [])

    def test_returns_first_nonempty_line(self):
        spy = _SshSpy(_cp(0, stdout="\nvexa-notarius-coord-new\nvexa-notarius-coord-old\n"))
        collector.ssh_capture = spy
        self.assertEqual(
            collector._latest_exited_notarius_for_series("coord"),
            "vexa-notarius-coord-new",
        )
        # серия уходит в фильтр заквоченной
        self.assertIn(shlex.quote("coord"), spy.calls[0])
        self.assertIn("status=exited", spy.calls[0])

    def test_rc_nonzero_returns_none(self):
        collector.ssh_capture = _SshSpy(_cp(1))
        self.assertIsNone(collector._latest_exited_notarius_for_series("coord"))

    def test_empty_output_returns_none(self):
        collector.ssh_capture = _SshSpy(_cp(0, stdout="\n  \n"))
        self.assertIsNone(collector._latest_exited_notarius_for_series("coord"))

    def test_timeout_returns_none(self):
        collector.ssh_capture = _SshSpy(raise_exc=subprocess.TimeoutExpired("cmd", 20))
        self.assertIsNone(collector._latest_exited_notarius_for_series("coord"))


if __name__ == "__main__":
    unittest.main()
