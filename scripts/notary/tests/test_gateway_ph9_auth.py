"""Шлюз Ватсона (Ф9): дверь. Секрет, fail-closed, коды 200/401/503, лимит тела.

Требование R7.1 дословно: `GET /health` с верным токеном → 200; без токена → 401;
при незаданном на сервере токене → 503 (fail-closed).

Почему это отдельный файл проб: шлюз — единственная дверь портала к записи чужих
встреч. «Авторизация выключилась молча» здесь означает открытый в интернет пульт,
а не неудобство.
"""
from __future__ import annotations

import inspect
import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.tests.gateway_harness import (  # noqa: E402
    TEST_TOKEN,
    EnvSandbox,
    LiveGateway,
    order_payload,
)

import notary.gateway as gateway  # noqa: E402


def _source_without_docstring(func) -> str:
    """Код функции без docstring: иначе пример из комментария сойдёт за реализацию."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    body = list(getattr(node, "body", []))
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


class TestTokenResolution(unittest.TestCase):
    """Fail-closed: пока секрет не годен, шлюз считает себя ненастроенным."""

    def setUp(self) -> None:
        self.env = EnvSandbox()

    def tearDown(self) -> None:
        self.env.restore()

    def test_env_token_is_taken(self) -> None:
        token, reason = gateway.resolve_token()
        self.assertEqual(token, TEST_TOKEN)
        self.assertIsNone(reason)

    def test_missing_token_is_not_configured(self) -> None:
        os.environ.pop("MEETING_NOTARY_GATEWAY_TOKEN", None)
        token, reason = gateway.resolve_token()
        self.assertIsNone(token)
        self.assertEqual(reason, "token_not_configured")

    def test_blank_token_is_not_configured(self) -> None:
        os.environ["MEETING_NOTARY_GATEWAY_TOKEN"] = "   "
        token, _ = gateway.resolve_token()
        self.assertIsNone(token, "пробельный секрет принят за настроенный")

    def test_short_token_is_refused(self) -> None:
        """Короткий секрет — секрет на один вечер перебора, а не «настроен плохо»."""
        os.environ["MEETING_NOTARY_GATEWAY_TOKEN"] = "x" * (gateway.MIN_TOKEN_LEN - 1)
        token, reason = gateway.resolve_token()
        self.assertIsNone(token)
        self.assertIn("token_too_short", reason or "")

    def test_token_file_must_be_owner_only(self) -> None:
        """Секрет, читаемый группой/миром, — не секрет. Мутация: убрать проверку прав."""
        path = self.env.root / "gateway.token"
        path.write_text(TEST_TOKEN, encoding="utf-8")
        os.chmod(path, 0o644)
        os.environ.pop("MEETING_NOTARY_GATEWAY_TOKEN", None)
        os.environ["MEETING_NOTARY_GATEWAY_TOKEN_FILE"] = str(path)
        token, reason = gateway.resolve_token()
        self.assertIsNone(token)
        self.assertIn("token_file_mode", reason or "")

        os.chmod(path, 0o600)
        token, reason = gateway.resolve_token()
        self.assertEqual(token, TEST_TOKEN)
        self.assertIsNone(reason)

    def test_token_file_missing_is_refused(self) -> None:
        os.environ.pop("MEETING_NOTARY_GATEWAY_TOKEN", None)
        os.environ["MEETING_NOTARY_GATEWAY_TOKEN_FILE"] = str(self.env.root / "нет-такого")
        token, reason = gateway.resolve_token()
        self.assertIsNone(token)
        self.assertIn("token_file_unreadable", reason or "")


class TestBearerComparison(unittest.TestCase):
    def test_only_exact_bearer_passes(self) -> None:
        self.assertTrue(gateway.is_authorized("Bearer %s" % TEST_TOKEN, TEST_TOKEN))
        self.assertFalse(gateway.is_authorized(None, TEST_TOKEN))
        self.assertFalse(gateway.is_authorized("", TEST_TOKEN))
        self.assertFalse(gateway.is_authorized(TEST_TOKEN, TEST_TOKEN), "схема Bearer не обязательна")
        self.assertFalse(gateway.is_authorized("Basic %s" % TEST_TOKEN, TEST_TOKEN))
        self.assertFalse(gateway.is_authorized("Bearer %s" % TEST_TOKEN[:-1], TEST_TOKEN))
        self.assertFalse(gateway.is_authorized("Bearer %sx" % TEST_TOKEN, TEST_TOKEN))
        self.assertFalse(gateway.is_authorized("bearer %s" % TEST_TOKEN, TEST_TOKEN))

    def test_empty_server_token_authorizes_nobody(self) -> None:
        """Пустой серверный токен не должен «совпадать» ни с чем, включая пустоту."""
        self.assertFalse(gateway.is_authorized("Bearer ", ""))
        self.assertFalse(gateway.is_authorized("", ""))

    def test_comparison_is_constant_time(self) -> None:
        """Сравнение секрета — только `hmac.compare_digest`.

        Мутация, которая красит эту пробу: заменить его на `==` (наивное сравнение
        длинного секрета утекает посимвольно по времени ответа). Поведение при
        этом не меняется — поймать можно только так.
        """
        code = _source_without_docstring(gateway.is_authorized)
        self.assertIn("compare_digest", code)
        self.assertNotIn("==", code, "в сравнении секрета появился наивный `==`")
        self.assertNotIn("!=", code, "в сравнении секрета появился наивный `!=`")


class TestLiveDoor(unittest.TestCase):
    """Живой HTTP: ровно те коды, что записаны в критерии R7.1."""

    def setUp(self) -> None:
        self.env = EnvSandbox()
        self.gw = LiveGateway()

    def tearDown(self) -> None:
        self.gw.stop()
        self.env.restore()

    def test_health_with_valid_token_is_200(self) -> None:
        resp = self.gw.request("GET", "/health")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.json()["ok"])

    def test_health_without_token_is_401(self) -> None:
        resp = self.gw.request("GET", "/health", token=None)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.json()["error"], "unauthorized")
        self.assertIn("Bearer", resp.header("WWW-Authenticate") or "")

    def test_health_with_foreign_token_is_401(self) -> None:
        resp = self.gw.request("GET", "/health", token="a" * len(TEST_TOKEN))
        self.assertEqual(resp.status, 401)

    def test_health_without_server_token_is_503(self) -> None:
        """Fail-closed: секрета на сервере нет → 503 всем, а не «пускаем всех».

        Мутация, которая красит: вернуть в `_authorized` ветку «токен не задан →
        считаем запрос разрешённым».
        """
        os.environ.pop("MEETING_NOTARY_GATEWAY_TOKEN", None)
        resp = self.gw.request("GET", "/health")
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.json()["error"], "gateway_token_not_configured")
        # И с верным (бывшим) токеном, и без него — одинаково закрыто.
        self.assertEqual(self.gw.request("GET", "/health", token=None).status, 503)

    def test_short_server_token_closes_the_gateway(self) -> None:
        os.environ["MEETING_NOTARY_GATEWAY_TOKEN"] = "korotkiy"
        resp = self.gw.request("GET", "/health", token="korotkiy")
        self.assertEqual(resp.status, 503)

    def test_every_route_is_behind_the_token(self) -> None:
        """Ни одной ручки мимо двери: список сверяется с маршрутами обработчиков."""
        cases = [
            ("GET", "/health"),
            ("GET", "/health?deep=1"),
            ("GET", "/status"),
            ("GET", "/orders"),
            ("GET", "/orders/portal:mtg-1001"),
            ("GET", "/protocol?meeting_id=mtg-1001"),
            ("POST", "/orders"),
            ("POST", "/orders/join-now"),
            ("POST", "/orders/leave-now"),
            ("PUT", "/orders/portal:mtg-1001"),
            ("DELETE", "/orders/portal:mtg-1001"),
            ("GET", "/no-such-handle"),
        ]
        for method, path in cases:
            with self.subTest(route="%s %s" % (method, path)):
                body = order_payload() if method in ("POST", "PUT") else None
                resp = self.gw.request(method, path, body=body, token=None)
                self.assertEqual(
                    resp.status, 401, "%s %s пустил без токена" % (method, path)
                )

    def test_unknown_route_is_404_not_500(self) -> None:
        self.assertEqual(self.gw.request("GET", "/no-such-handle").status, 404)
        self.assertEqual(self.gw.request("POST", "/no-such-handle", body={}).status, 404)

    def test_change_order_by_post_says_which_method(self) -> None:
        resp = self.gw.request("POST", "/orders/portal:mtg-1001", body=order_payload())
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json()["error"], "method_not_allowed")


class TestBodyLimits(unittest.TestCase):
    """Тело запроса: лимит размера, битый JSON, отсутствующий Content-Length."""

    def setUp(self) -> None:
        self.env = EnvSandbox()
        os.environ["MEETING_NOTARY_GATEWAY_MAX_BODY"] = "2048"
        self.gw = LiveGateway()

    def tearDown(self) -> None:
        self.gw.stop()
        self.env.restore()

    def test_body_over_limit_is_413(self) -> None:
        big = order_payload(expected_participants=["Участник %d" % i for i in range(300)])
        resp = self.gw.request("POST", "/orders", body=big)
        self.assertEqual(resp.status, 413)
        self.assertEqual(resp.json()["error"], "body_too_large")
        self.assertEqual(resp.json()["limit_bytes"], 2048)

    def test_oversized_body_is_not_read_into_memory(self) -> None:
        """Смысл лимита — не прочитать большое тело вовсе.

        Мутация, которая красит: сперва `self.rfile.read(length)`, потом проверка
        размера — тогда 10 МБ окажутся в памяти шлюза до отказа.
        """
        src = inspect.getsource(gateway._Handler._read_json_body)
        head, _, tail = src.partition("rfile.read")
        self.assertIn("body_too_large", head, "проверка лимита стоит ПОСЛЕ чтения тела")
        self.assertNotIn("body_too_large", tail)

    def test_broken_json_is_400_and_body_is_not_echoed(self) -> None:
        raw = b'{"meeting_id": "mtg-1", "company": '
        resp = self.gw.request("POST", "/orders", raw_body=raw)
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.json()["error"], "invalid_json")
        self.assertNotIn("meeting_id", resp.json()["message"])

    def test_empty_body_is_400(self) -> None:
        resp = self.gw.request("POST", "/orders", raw_body=b"")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.json()["error"], "empty_body")

    def test_non_json_content_type_is_415(self) -> None:
        resp = self.gw.request(
            "POST", "/orders", raw_body=b"meeting_id=1", content_type="text/plain"
        )
        self.assertEqual(resp.status, 415)

    def test_json_array_instead_of_object_is_422(self) -> None:
        resp = self.gw.request("POST", "/orders", raw_body=b"[1, 2, 3]")
        self.assertEqual(resp.status, 422)
        self.assertEqual(resp.json()["errors"][0]["code"], "not_an_object")

    def test_missing_content_length_is_411(self) -> None:
        """Chunked-тело шлюз не принимает: иначе лимит размера обходится."""
        code, _ = self.gw.raw_socket_request(
            b"POST /orders HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Authorization: Bearer " + TEST_TOKEN.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        )
        self.assertEqual(code, 411)

    def test_bad_content_length_is_400(self) -> None:
        code, _ = self.gw.raw_socket_request(
            b"POST /orders HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Authorization: Bearer " + TEST_TOKEN.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: -5\r\n\r\n"
        )
        self.assertEqual(code, 400)

    def test_refusal_before_reading_body_closes_connection(self) -> None:
        """Непрочитанное тело + keep-alive = следующий запрос разбирается по мусору.

        У портала клиент с пулом соединений: после первого же 401 «поехал бы» весь
        пул. Мутация, которая красит: убрать `Connection: close` из отказов.
        """
        code, raw = self.gw.raw_socket_request(
            b"POST /orders HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        self.assertEqual(code, 401)
        self.assertIn(b"Connection: close", raw)


if __name__ == "__main__":
    unittest.main()
