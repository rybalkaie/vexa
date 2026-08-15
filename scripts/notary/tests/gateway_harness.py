"""Стенд для проб шлюза Ватсона (Ф9): живой HTTP на эфемерном порту + изоляция путей.

Почему живой сервер, а не вызов функций-обработчиков напрямую: половина
требований фазы — это ПРОТОКОЛ (коды 401/411/413/415, заголовок
`WWW-Authenticate`, закрытие соединения при непрочитанном теле). Вызовом метода
класса это не проверяется, а именно там живут дефекты, которые видит портал.

Изоляция (жёсткое правило проекта, сторож — `test_gateway_state_isolation.py`):
каждая проба поднимает шлюз на СВОЁМ временном каталоге реестра. Ни один тест не
смеет записать в боевой `~/Projects/me/встречи/` — там живые встречи владельца.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

import notary.gateway as gateway  # noqa: E402

# Фикстура, не секрет: 40 знаков, чтобы пройти планку MIN_TOKEN_LEN. Настоящий
# токен живёт в EnvironmentFile юнита с правами 600 и в репозиторий не попадает.
TEST_TOKEN = "test-token-0123456789-0123456789-abcdef"

# Переменные окружения, которые проба обязана подменить, иначе шлюз пойдёт в
# боевые каталоги владельца.
ISOLATED_ENV = (
    "MEETING_NOTARY_REGISTRY_DIR",
    "MEETING_NOTARY_PROTOCOLS_DIR",
    "MEETING_NOTARY_ARCHIVE_DIR",
    "MEETING_NOTARY_LOG_DIR",
    "MEETING_NOTARY_GATEWAY_TOKEN",
    "MEETING_NOTARY_GATEWAY_TOKEN_FILE",
    "MEETING_NOTARY_GATEWAY_MAX_BODY",
    "MEETING_NOTARY_GATEWAY_ROOM_HOSTS",
    "MEETING_NOTARY_MAX_CONCURRENT_BOTS",
)


class EnvSandbox:
    """Снимок env + временные каталоги; всё возвращается на место в teardown."""

    def __init__(self, *, token: Optional[str] = TEST_TOKEN) -> None:
        self._saved: Dict[str, Optional[str]] = {
            name: os.environ.get(name) for name in ISOLATED_ENV
        }
        self.root = Path(tempfile.mkdtemp(prefix="gateway-probe-"))
        self.registry_dir = self.root / "registry"
        self.protocols_dir = self.root / "protocols"
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.protocols_dir.mkdir(parents=True, exist_ok=True)
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = str(self.registry_dir)
        os.environ["MEETING_NOTARY_PROTOCOLS_DIR"] = str(self.protocols_dir)
        os.environ["MEETING_NOTARY_ARCHIVE_DIR"] = str(self.protocols_dir)
        os.environ["MEETING_NOTARY_LOG_DIR"] = str(self.root / "logs")
        os.environ.pop("MEETING_NOTARY_GATEWAY_TOKEN_FILE", None)
        if token is None:
            os.environ.pop("MEETING_NOTARY_GATEWAY_TOKEN", None)
        else:
            os.environ["MEETING_NOTARY_GATEWAY_TOKEN"] = token

    def restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.root, ignore_errors=True)


class Response:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> Optional[str]:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class LiveGateway:
    """Шлюз, поднятый в потоке на 127.0.0.1 со случайным портом."""

    def __init__(self) -> None:
        self.server = gateway.build_server("127.0.0.1", 0)
        self.host, self.port = self.server.server_address[0], self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    # --- запросы ---

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw_body: Optional[bytes] = None,
        token: Optional[str] = TEST_TOKEN,
        headers: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = "application/json",
        keep_alive: bool = False,
    ) -> Response:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=20)
        try:
            return self._do(
                conn, method, path, body, raw_body, token, headers, content_type
            )
        finally:
            if not keep_alive:
                conn.close()

    def _do(
        self,
        conn: http.client.HTTPConnection,
        method: str,
        path: str,
        body: Any,
        raw_body: Optional[bytes],
        token: Optional[str],
        headers: Optional[Dict[str, str]],
        content_type: Optional[str],
    ) -> Response:
        payload = raw_body
        if payload is None and body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs: Dict[str, str] = {}
        if token is not None:
            hdrs["Authorization"] = "Bearer %s" % token
        if payload is not None and content_type:
            hdrs["Content-Type"] = content_type
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return Response(resp.status, dict(resp.getheaders()), data)

    def raw_socket_request(self, chunk: bytes) -> Tuple[int, bytes]:
        """Сырой запрос: нужен там, где http.client сам чинит заголовки.

        Без него не проверить «нет Content-Length» и «Content-Length врёт» — а
        это ровно те входы, на которых сервер, читающий тело наивно, встаёт.
        """
        import socket

        sock = socket.create_connection((self.host, self.port), timeout=20)
        try:
            sock.sendall(chunk)
            out = b""
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                out += part
                if b"\r\n\r\n" in out and b"Content-Length" in out:
                    head, _, rest = out.partition(b"\r\n\r\n")
                    length = 0
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":", 1)[1].strip())
                    if len(rest) >= length:
                        break
        finally:
            sock.close()
        status_line = out.split(b"\r\n", 1)[0]
        try:
            code = int(status_line.split(b" ")[1])
        except (IndexError, ValueError):
            code = 0
        return code, out


class _Omit:
    """Маркер «поля в теле запроса быть не должно» (не то же, что `null`)."""


OMIT = _Omit()


def order_payload(**overrides: Any) -> Dict[str, Any]:
    """Валидный наряд портала. Переопределяй поля точечно в самой пробе."""
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc) + timedelta(hours=3)
    payload: Dict[str, Any] = {
        "meeting_id": "mtg-1001",
        "company": "anzhee",
        "series_title": "Координация продаж",
        "series": "koordinatsiya-prodazh",
        "room_url": "https://telemost.yandex.ru/j/11111111111111",
        "start_at": start.isoformat(),
        "duration_minutes": 45,
        "expected_participants": ["Илья Рыбалка", "Мария Саргина"],
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if not isinstance(v, _Omit)}
