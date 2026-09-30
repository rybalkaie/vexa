"""Шлюз Ватсона останавливается по SIGTERM за секунды, а не за 90 с (30.09.2026).

Инцидент: с 01.08 по 30.09 шлюз 14 раз не остановился по SIGTERM. Обработчик
сигнала звал `server.shutdown()` в том же потоке, где крутится
`serve_forever()`: `shutdown()` ждёт выхода цикла, цикл ждёт возврата из
обработчика — взаимная блокировка. systemd ждал 90 с и добивал процесс
SIGKILL; всё это время портал получал отказ.

Пробы злые:
- настоящий процесс `gateway.py` + настоящий SIGTERM (вызов функций в потоке
  этот дефект не видит: он живёт в обработчике сигнала главного потока). На
  прежнем коде проба красная — процесс не выходит за отведённое время;
- открытое простаивающее keep-alive-соединение, как у Caddy: штатное закрытие
  ThreadingMixIn ждало бы его поток до таймаута `_Handler.timeout` (30 с);
- начатый запрос обязан дойти целиком — «быстро остановиться» не значит
  «оборвать ответ»; зависший запрос не держит остановку дольше потолка.
"""
from __future__ import annotations

import http.client
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.tests.gateway_harness import EnvSandbox  # noqa: E402

import notary.gateway as gateway  # noqa: E402

GATEWAY_PY = _HERE.parent / "gateway.py"

# Потолок на выход процесса после SIGTERM. Исправленный шлюз выходит за доли
# секунды (цикл приёма опрашивает флаг раз в 0,5 с); прежний не выходил вовсе.
STOP_BUDGET_S = 5.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_serving(port: int, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    """Ждать ОТВЕТА, а не просто открытого порта.

    Порт начинает принимать соединения при создании сервера — раньше, чем
    поставлены обработчики сигналов. Сигнал в этот промежуток убивает процесс
    по умолчанию (код -15), и проба мигала. Ответ приходит только из цикла
    приёма, а он стартует после установки обработчиков.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("шлюз упал на старте, код %s" % proc.returncode)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/health", headers={"Connection": "close"})
            conn.getresponse().read()
            return
        except OSError:
            time.sleep(0.05)
        finally:
            conn.close()
    raise AssertionError("шлюз не начал отвечать за %.0f с" % timeout)


class TestSigtermStopsRealProcess(unittest.TestCase):
    """Настоящий процесс, настоящий сигнал — ровно то, что делает systemd."""

    def setUp(self) -> None:
        # Шлюз без токена: fail-closed, все ответы 503. Для остановки это
        # безразлично, зато проба не трогает ни секрет, ни боевые каталоги.
        self.env = EnvSandbox(token=None)
        self.port = _free_port()
        child_env = dict(os.environ)
        child_env["MEETING_NOTARY_GATEWAY_HOST"] = "127.0.0.1"
        child_env["MEETING_NOTARY_GATEWAY_PORT"] = str(self.port)
        child_env["PYTHONPATH"] = str(_HERE.parent.parent)
        self.proc = subprocess.Popen(
            [sys.executable, str(GATEWAY_PY)],
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._kill)
        _wait_serving(self.port, self.proc)

    def tearDown(self) -> None:
        self.env.restore()

    def _kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def _stop_and_time(self, sig: int) -> float:
        started = time.monotonic()
        self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=STOP_BUDGET_S)
        except subprocess.TimeoutExpired:
            self.fail(
                "шлюз не остановился за %.0f с после сигнала %s — остановка "
                "зависла (прежний дефект: shutdown() из обработчика сигнала)"
                % (STOP_BUDGET_S, sig)
            )
        return time.monotonic() - started

    def test_sigterm_exits_fast_with_code_0(self) -> None:
        elapsed = self._stop_and_time(signal.SIGTERM)
        self.assertEqual(self.proc.returncode, 0)
        self.assertLess(elapsed, 2.0)

    def test_sigterm_with_idle_keepalive_connection_exits_fast(self) -> None:
        """Caddy держит к шлюзу keep-alive-соединения — остановка их не ждёт."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 503)  # fail-closed: токена нет
        self.assertNotEqual(
            (resp.getheader("Connection") or "").lower(),
            "close",
            "проба не создала простаивающее keep-alive-соединение",
        )
        elapsed = self._stop_and_time(signal.SIGTERM)
        self.assertEqual(self.proc.returncode, 0)
        self.assertLess(elapsed, 2.0)

    def test_sigint_stops_too(self) -> None:
        self._stop_and_time(signal.SIGINT)
        self.assertEqual(self.proc.returncode, 0)


class _ServingGateway:
    """Шлюз в соседнем потоке — для проб `close_gracefully` без сигналов."""

    def __init__(self) -> None:
        self.server = gateway.build_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def stop(self, drain_seconds: float) -> Dict[str, Any]:
        started = time.monotonic()
        self.server.shutdown()
        self.thread.join(timeout=5)
        stuck = self.server.close_gracefully(drain_seconds)
        return {"stuck": stuck, "elapsed": time.monotonic() - started}


def _slow_health(delay: float, entered: threading.Event):
    def handler(self: Any, _query: Any) -> None:
        entered.set()
        time.sleep(delay)
        self._send(200, {"ok": True, "slow": True})

    return handler


class TestCloseGracefully(unittest.TestCase):
    def setUp(self) -> None:
        self.env = EnvSandbox()
        self.gw = _ServingGateway()

    def tearDown(self) -> None:
        if not self.gw.server.stopping.is_set():
            self.gw.stop(0)
        self.env.restore()

    def _get(self, results: List[Any], path: str = "/health") -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.gw.port, timeout=10)
        try:
            conn.request(
                "GET", path, headers={"Authorization": "Bearer %s" % os.environ["MEETING_NOTARY_GATEWAY_TOKEN"]}
            )
            resp = conn.getresponse()
            results.append((resp.status, resp.getheader("Connection"), resp.read()))
        except Exception as exc:  # noqa: BLE001 — проба сообщает, что сломалось
            results.append(exc)
        finally:
            conn.close()

    def test_idle_keepalive_does_not_hold_stop(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.gw.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/nope")  # 401 без токена, соединение остаётся живым
        conn.getresponse().read()
        self.assertEqual(self.gw.server.open_connections(), 1)
        out = self.gw.stop(drain_seconds=10)
        self.assertEqual(out["stuck"], 0)
        self.assertLess(out["elapsed"], 2.0, "простаивающий keep-alive задержал остановку")

    def test_request_in_progress_is_answered_in_full(self) -> None:
        entered = threading.Event()
        results: List[Any] = []
        with mock.patch.object(gateway._Handler, "_health", _slow_health(1.0, entered)):
            client = threading.Thread(target=self._get, args=(results,))
            client.start()
            self.assertTrue(entered.wait(5), "запрос не дошёл до обработчика")
            out = self.gw.stop(drain_seconds=10)
            client.join(10)
        self.assertEqual(out["stuck"], 0)
        self.assertGreaterEqual(out["elapsed"], 0.5, "остановка не дождалась начатого запроса")
        self.assertEqual(len(results), 1)
        self.assertNotIsInstance(results[0], Exception, "ответ оборван: %r" % (results[0],))
        status, connection, body = results[0]
        self.assertEqual(status, 200)
        self.assertIn(b'"slow": true', body)
        self.assertEqual((connection or "").lower(), "close")

    def test_hung_request_does_not_hold_stop_beyond_cap(self) -> None:
        entered = threading.Event()
        results: List[Any] = []
        with mock.patch.object(gateway._Handler, "_health", _slow_health(3.0, entered)):
            client = threading.Thread(target=self._get, args=(results,), daemon=True)
            client.start()
            self.assertTrue(entered.wait(5), "запрос не дошёл до обработчика")
            out = self.gw.stop(drain_seconds=0.5)
            self.assertEqual(out["stuck"], 1)
            self.assertLess(out["elapsed"], 2.0, "потолок ожидания не соблюдён")
            client.join(10)  # дать зависшему потоку доработать внутри patch

    def test_new_connections_refused_after_stop(self) -> None:
        self.gw.stop(drain_seconds=1)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.gw.port), timeout=2).close()


if __name__ == "__main__":
    unittest.main()
