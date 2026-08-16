#!/usr/bin/env python3
"""gateway.py — HTTP-шлюз Ватсона: защищённый канал команд «портал → бот» (Ф9).

Слушает ЛОКАЛЬНЫЙ порт `127.0.0.1:8789`; наружу ведёт только Caddy с
автоматическим TLS (`scripts/notary/deploy/meeting-notary-gateway.caddy`). Голый
порт в интернет не смотрит никогда — секрет ходит по TLS и только по нему.
Юнит, конфигурация Caddy и список шагов, которые делает владелец на VPS, —
`scripts/notary/deploy/README.md`.

Контракт для портала (ручки, формы запроса/ответа, коды ошибок, поведение при
занятости) — `scripts/notary/docs/gateway-contract.md`. Он источник истины для
клиента на стороне портала; менять ручку, не поправив контракт, нельзя.

ПОЧЕМУ ОТДЕЛЬНЫЙ СЕРВИС, а не ручка внутри listener'а: падение шлюза не должно
ронять запись встреч, а рестарт шлюза — обрывать `getUpdates`. Цена — 15-й юнит
на машине, поэтому в юните обязательны потолки памяти и процессора.

ТРИ ПРАВИЛА, НАРУШЕНИЕ КОТОРЫХ УЖЕ СТОИЛО ПРОЕКТУ СУТОК:

1. **Fail-closed.** Токен на сервере не задан (или короче 32 знаков, или лежит в
   файле, читаемом кем попало) — ЛЮБОЙ запрос получает 503, а не «пускаем всех».
   Молчаливое «авторизация выключена» — это открытый в интернет пульт к записи
   чужих встреч.
2. **Глубокий health.** `/health?deep=1` реально проходит путь до планировщика.
   Плоский health уже стоил трёх суток тишины (инцидент 17.07): он отвечал 200,
   пока жив процесс, а служба под ним была мертва.
3. **Ни строчки содержания в логах.** Ни реплик, ни текста протокола, ни тела
   запроса, ни секрета. В лог идут метод, путь, код, id наряда, размер — всё.
   Опасная тройка (личные данные + чужой контент + отправка наружу) сходится
   здесь целиком.

К Claude шлюз НЕ аутентифицируется вовсе: он транспорт. Слот авторизации Claude
на этой машине (годовой `CLAUDE_CODE_OAUTH_TOKEN` в `/srv/meeting-notary/.env.notary`)
шлюз не трогает — один аккаунт, залогиненный в двух домашних папках, выбивает сам
себя за 1–3 дня и молча убивает все службы (инциденты 26.06 и 17.07).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from notary.lib import orders as orders_lib  # noqa: E402

LOG_DIR = Path(
    os.path.expanduser(
        os.environ.get("MEETING_NOTARY_LOG_DIR") or "~/Library/Logs/meeting-notary"
    )
)
LOG_FILE = LOG_DIR / "gateway.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8789
# Наряд — это десяток полей и список участников. 64 КиБ с запасом; всё, что
# больше, — либо ошибка портала, либо попытка забить память шлюза.
DEFAULT_MAX_BODY_BYTES = 64 * 1024
# Короткий секрет = секрет на один вечер перебора. 32 знака — нижняя планка,
# ниже которой шлюз считает себя НЕнастроенным (503), а не «настроенным плохо».
MIN_TOKEN_LEN = 32

GATEWAY_VERSION = "1"

logger = logging.getLogger("notary.gateway")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.insert(0, logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass  # journal остаётся, сервис поднимается
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


# ---------- конфигурация и секрет ----------


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def max_body_bytes() -> int:
    return _env_int("MEETING_NOTARY_GATEWAY_MAX_BODY", DEFAULT_MAX_BODY_BYTES)


def max_concurrent_bots() -> int:
    """Тот же потолок, что у runner'а (`MEETING_NOTARY_MAX_CONCURRENT_BOTS`).

    Ф10b: значение считает ОДНА функция на весь проект (`lib/slots.py`) — две
    константы «сколько ботов разом» в двух файлах разъезжаются, и портал начал
    бы обещать слот, которого нет.
    """
    try:
        from notary.lib import slots  # noqa: PLC0415

        return slots.max_concurrent_bots()
    except Exception:  # noqa: BLE001
        return _env_int("MEETING_NOTARY_MAX_CONCURRENT_BOTS", 2)


def resolve_token() -> Tuple[Optional[str], Optional[str]]:
    """Секрет шлюза → (токен, причина_отказа). Токен None ⇒ шлюз fail-closed.

    Два источника: `MEETING_NOTARY_GATEWAY_TOKEN` (EnvironmentFile юнита, права
    600) и `MEETING_NOTARY_GATEWAY_TOKEN_FILE`. У файла проверяются права: если
    он читаем группой или миром — считаем шлюз ненастроенным. «Секрет, который
    может прочитать кто угодно» — это не секрет, а иллюзия, и лучше 503 в лоб,
    чем тихо работающий открытый пульт.
    """
    raw = os.environ.get("MEETING_NOTARY_GATEWAY_TOKEN")
    if raw is None or not raw.strip():
        path_raw = os.environ.get("MEETING_NOTARY_GATEWAY_TOKEN_FILE")
        if path_raw and path_raw.strip():
            path = Path(os.path.expanduser(path_raw.strip()))
            try:
                mode = path.stat().st_mode
            except OSError as exc:
                return None, "token_file_unreadable: %s" % exc
            if mode & 0o077:
                return None, (
                    "token_file_mode: %s доступен группе/миру (%o) — нужен chmod 600"
                    % (path, mode & 0o777)
                )
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                return None, "token_file_unreadable: %s" % exc
        else:
            return None, "token_not_configured"
    token = raw.strip()
    if not token:
        return None, "token_empty"
    if len(token) < MIN_TOKEN_LEN:
        return None, "token_too_short: нужно ≥%d знаков" % MIN_TOKEN_LEN
    return token, None


def is_authorized(auth_header: Optional[str], token: str) -> bool:
    """Заголовок обязан быть ровно `Bearer <token>`. Сравнение в постоянное время.

    Наивный `==` на длинном секрете утекает посимвольно по времени ответа —
    паттерн повторён с двух соседних шлюзов этой же машины.
    """
    if not token:
        return False
    expected = "Bearer %s" % token
    got = auth_header or ""
    return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


# ---------- глубокая проверка живости ----------


def deep_probe(now: Optional[datetime] = None) -> Tuple[bool, List[Dict[str, Any]]]:
    """Реально проходит путь до планировщика. Возвращает (ок, список проверок).

    Не «отвечает константой»: собирает синтетическую разовую запись и прогоняет
    её через настоящую `scheduler._expand_one_off` → `_resolve_url` →
    `_queue_item`. Сломается любое звено этой цепочки — здесь станет красно, и
    сторож портала разбудит владельца. Сеть НЕ трогается: календарь, Telegram и
    docker сюда не входят, иначе проверка сама станет источником ложных тревог.
    """
    now = now or datetime.now(timezone.utc)
    checks: List[Dict[str, Any]] = []
    ok = True

    def add(name: str, passed: bool, **extra: Any) -> None:
        nonlocal ok
        entry: Dict[str, Any] = {"check": name, "ok": bool(passed)}
        entry.update(extra)
        checks.append(entry)
        if not passed:
            ok = False

    # 1. Реестр нарядов: путь, читаемость, право записи.
    try:
        path = orders_lib.orders_file()
        state = orders_lib.read_orders()
        writable = os.access(str(path.parent), os.W_OK) if path.parent.exists() else False
        add(
            "orders_registry",
            writable,
            path=str(path),
            orders=len(state.get("orders", {})),
            detail=None if writable else "каталог реестра недоступен на запись",
        )
    except Exception as exc:  # noqa: BLE001
        add("orders_registry", False, detail="%s: %s" % (type(exc).__name__, exc))

    # 2. Планировщик: НАСТОЯЩАЯ раскрутка записи в элемент очереди.
    try:
        import notary.scheduler as scheduler  # локальный импорт: сломанный
        # планировщик обязан красить health, а не мешать шлюзу подняться

        probe_start = now + timedelta(minutes=30)
        rec = {
            "id": "gateway-deep-probe",
            "series": "gateway-deep-probe",
            "type": "one-off",
            "datetime": probe_start.isoformat(),
            "room": "https://telemost.yandex.ru/j/00000000000000",
            "duration_minutes": 30,
        }
        items = scheduler._expand_one_off(
            rec, {"rooms": []}, now, now + timedelta(hours=48)
        )
        shape_ok = (
            len(items) == 1
            and items[0].get("event_id", "").startswith("local:")
            and items[0].get("url") == rec["room"]
            and items[0].get("start_at")
            and int(items[0].get("duration_minutes") or 0) > 0
        )
        add(
            "scheduler_expand",
            shape_ok,
            items=len(items),
            detail=None if shape_ok else "планировщик вернул элемент неожиданной формы",
        )
    except Exception as exc:  # noqa: BLE001
        add("scheduler_expand", False, detail="%s: %s" % (type(exc).__name__, exc))

    # 3. Очередь и отметка последнего успешного скана календаря.
    #
    # ЗДЕСЬ И ЖИВЁТ СМЫСЛ ГЛУБОКОЙ ПРОВЕРКИ: процесс планировщика может быть жив,
    # а работать — перестать (инцидент 17.07: разлогиненная подписка, звонки в
    # терминальном FAILED, три дня тишины). Поэтому молчание планировщика дольше
    # порога — КРАСНО, а не строчка в отчёте, которую никто не читает.
    try:
        from notary.lib import state as state_lib

        queue = state_lib.read_queue()
        last = state_lib.read_last_success()
        age_min = None if last is None else int((now - last).total_seconds() // 60)
        limit_min = _env_int("MEETING_NOTARY_GATEWAY_SCHEDULER_STALE_MINUTES", 90)
        if not isinstance(queue, list):
            detail = "очередь планировщика нечитаема"
            passed = False
        elif last is None:
            # На живой машине планировщик отмечается каждые 15 минут. Отсутствие
            # отметки — либо он ни разу не отработал, либо файл потёрли.
            detail = "планировщик ещё ни разу не отчитался об успешном скане"
            passed = False
        elif age_min is not None and age_min > limit_min:
            detail = "планировщик молчит %d мин при пороге %d" % (age_min, limit_min)
            passed = False
        else:
            detail = None
            passed = True
        add(
            "scheduler_queue",
            passed,
            queue_size=len(queue) if isinstance(queue, list) else None,
            last_success_age_minutes=age_min,
            stale_after_minutes=limit_min,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        add("scheduler_queue", False, detail="%s: %s" % (type(exc).__name__, exc))

    return ok, checks


# ---------- статус привязок и активности ----------


def bindings_status() -> Dict[str, Any]:
    """Привязки серий и активность — для будущих экранов портала (Ф12/Ф13a).

    Значения `telegram_chat_id` НАРУЖУ НЕ ОТДАЮТСЯ: порталу нужен факт «привязка
    есть / нет», а id приватного чата — лишняя утечка на ровном месте.
    """
    out: Dict[str, Any] = {"series": [], "source": "watched.yaml"}
    try:
        from notary.cli.registry import (
            _read_yaml,
            _resolve_registry_dir,
            get_company_for_series,
            get_visibility_for_series,
            series_record_is_live,
        )

        # Путь резолвим ЛЕНИВО, а не через `load_watched()`: та ходит в константу
        # `WATCHED_FILE`, вычисленную на ИМПОРТЕ модуля. Ровно этот класс дефекта
        # (приёмка 05.08.2026) заставлял процессы работать с каталогом, который
        # был актуален минуту назад. Для долгоживущей службы это значит «читаем не
        # тот реестр и не замечаем».
        watched = _read_yaml(_resolve_registry_dir() / "watched.yaml", "watched")
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out

    seen: Dict[str, Dict[str, Any]] = {}
    for rec in watched.get("watched", []) or []:
        if not isinstance(rec, dict):
            continue
        series = rec.get("series")
        if not isinstance(series, str) or not series.strip():
            continue
        entry = seen.setdefault(
            series,
            {
                "series": series,
                "telegram_chat_bound": False,
                "company": get_company_for_series(series, watched),
                "visibility": get_visibility_for_series(series, watched),
                "live": False,
            },
        )
        chat = rec.get("telegram_chat_id")
        if isinstance(chat, int) and not isinstance(chat, bool):
            entry["telegram_chat_bound"] = True
        if series_record_is_live(rec):
            entry["live"] = True
    out["series"] = sorted(seen.values(), key=lambda e: e["series"])
    return out


def activity_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    out: Dict[str, Any] = {"max_concurrent_bots": max_concurrent_bots()}
    # Ф10a: положение аварийного выключателя — наружу. Иначе «наряды не
    # исполняются» и «фича выключена» с портала выглядят одинаково, а разбирать
    # это придётся по логам на VPS.
    try:
        from notary.lib import identity  # noqa: PLC0415

        out["portal_orders_enabled"] = identity.is_enabled()
    except Exception:  # noqa: BLE001
        out["portal_orders_enabled"] = False
    try:
        from notary.lib import state as state_lib

        queue = state_lib.read_queue()
        last = state_lib.read_last_success()
        out["queue_size"] = len(queue)
        out["next_start_at"] = queue[0].get("start_at") if queue else None
        out["scheduler_last_success_at"] = None if last is None else last.isoformat()
        out["scheduler_stale"] = (
            True if last is None else (now - last) > timedelta(hours=1)
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        active = orders_lib.list_orders(statuses=orders_lib.ACTIVE_STATUSES)
        out["orders_active"] = len(active)
        out["orders_by_company"] = {}
        for order in active:
            company = str(order.get("company") or "?")
            out["orders_by_company"][company] = out["orders_by_company"].get(company, 0) + 1
    except Exception as exc:  # noqa: BLE001
        out["orders_error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


# ---------- протокол по запросу ----------


def protocol_path_for_order(order: Dict[str, Any], *, kind: str = "protokol"):
    """Путь к протоколу встречи наряда: рабочая папка → долговечный архив."""
    from notary.lib.paths import archive_root, protocols_root, _target_path

    start = str(order.get("start_at") or "")
    meta = {"series": order.get("series"), "date": start[:10], "startTs": start}
    for root in (protocols_root(), archive_root()):
        candidate = _target_path(meta, root=root, kind=kind)
        if candidate.exists():
            return candidate
    return None


# ---------- HTTP ----------


class _Handler(BaseHTTPRequestHandler):
    server_version = "meeting-notary-gateway/" + GATEWAY_VERSION
    sys_version = ""  # не раскрываем версию Python наружу
    protocol_version = "HTTP/1.1"
    # Клиент, открывший соединение и замолчавший, иначе держит поток шлюза
    # навсегда: десяток таких — и шлюз перестал отвечать, не упав (то есть
    # плоский health продолжал бы рапортовать 200).
    timeout = 30

    # Тело запроса прочитано? Если нет, а метод его нёс — соединение придётся
    # закрыть (см. `_send`). Сбрасывается на КАЖДЫЙ запрос, а не на соединение:
    # при keep-alive один обработчик обслуживает их подряд.
    _body_consumed = False

    # ---- служебное ----

    def handle_one_request(self) -> None:
        self._body_consumed = False
        super().handle_one_request()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        """Гасим встроенный лог в stderr: он печатает строку запроса целиком.

        Свою строку пишем сами — метод, путь БЕЗ query, код. Идентификаторы и
        тем более тела запросов в лог не попадают.
        """
        return

    def _log(self, code: int, note: str = "") -> None:
        path = urlsplit(self.path).path
        logger.info(
            "%s %s → %d%s", self.command, path, code, (" (%s)" % note) if note else ""
        )

    def _must_close(self) -> bool:
        """Тело запроса осталось непрочитанным → соединение переиспользовать нельзя.

        HTTP/1.1 держит соединение живым, и непрочитанные байты тела следующий
        разбор примет за строку запроса. Отказ по токену и отказ по размеру тела
        оба отвечают ДО чтения — то есть у клиента с пулом соединений (портал)
        после первого же 401 поехал бы весь пул.
        """
        return self.command in ("POST", "PUT", "PATCH") and not self._body_consumed

    def _send(self, code: int, payload: Dict[str, Any], *, note: str = "") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        close = self._must_close()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self._log(code, note)

    def _error(self, code: int, error: str, message: str, **extra: Any) -> None:
        payload: Dict[str, Any] = {"ok": False, "error": error, "message": message}
        payload.update(extra)
        self._send(code, payload, note=error)

    def _read_json_body(self) -> Tuple[Optional[Any], bool]:
        """(тело, всё_ок). При ошибке ответ уже отправлен."""
        limit = max_body_bytes()
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._error(
                411,
                "length_required",
                "нужен заголовок Content-Length (chunked-тело шлюз не принимает)",
            )
            return None, False
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            self._error(400, "bad_content_length", "Content-Length не число")
            return None, False
        if length < 0:
            self._error(400, "bad_content_length", "Content-Length отрицателен")
            return None, False
        if length > limit:
            # Не читаем: смысл лимита в том, чтобы большое тело не оказалось в
            # памяти шлюза вообще.
            self._error(
                413,
                "body_too_large",
                "тело больше %d байт" % limit,
                limit_bytes=limit,
            )
            return None, False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if length > 0 and ctype and ctype != "application/json":
            self._error(415, "unsupported_media_type", "ожидается application/json")
            return None, False
        raw = self.rfile.read(length) if length else b""
        self._body_consumed = True
        if not raw:
            self._error(400, "empty_body", "пустое тело запроса")
            return None, False
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Само тело в лог и в ответ НЕ попадает — там может быть что угодно.
            self._error(400, "invalid_json", "тело запроса не разбирается как JSON")
            return None, False
        return data, True

    def _authorized(self) -> bool:
        """Fail-closed: сперва «настроен ли шлюз», потом «пустили ли просителя»."""
        token, reason = resolve_token()
        if token is None:
            self._error(
                503,
                "gateway_token_not_configured",
                "шлюз не настроен: секрет не задан или негоден (%s)" % reason,
            )
            return False
        if not is_authorized(self.headers.get("Authorization"), token):
            close = self._must_close()
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="meeting-notary"')
            body = json.dumps(
                {"ok": False, "error": "unauthorized", "message": "нужен верный Bearer-токен"},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self._log(401, "unauthorized")
            return False
        return True

    # ---- маршруты ----

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        if path == "/health":
            return self._health(query)
        if path == "/status":
            return self._status()
        if path == "/orders":
            return self._orders_list(query)
        if path.startswith("/orders/"):
            return self._order_get(path[len("/orders/") :])
        if path == "/protocol":
            return self._protocol(query)
        self._error(404, "not_found", "нет такой ручки")

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/orders":
            return self._order_create()
        if path == "/orders/join-now":
            return self._join_now()
        if path == "/orders/leave-now":
            return self._leave_now()
        if path == "/protocol/edit":
            return self._protocol_edit()
        if path.startswith("/orders/"):
            self._error(
                405, "method_not_allowed", "изменение наряда — PUT, снятие — DELETE"
            )
            return
        self._error(404, "not_found", "нет такой ручки")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path.startswith("/orders/") and path not in (
            "/orders/join-now",
            "/orders/leave-now",
        ):
            return self._order_update(path[len("/orders/") :])
        self._error(404, "not_found", "нет такой ручки")

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path.startswith("/orders/") and path not in (
            "/orders/join-now",
            "/orders/leave-now",
        ):
            return self._order_cancel(path[len("/orders/") :])
        self._error(404, "not_found", "нет такой ручки")

    # ---- реализация ручек ----

    def _health(self, query: Dict[str, List[str]]) -> None:
        deep = (query.get("deep") or ["0"])[0] in ("1", "true", "yes")
        if not deep:
            self._send(200, {"ok": True, "deep": False, "version": GATEWAY_VERSION})
            return
        ok, checks = deep_probe()
        self._send(
            200 if ok else 503,
            {"ok": ok, "deep": True, "version": GATEWAY_VERSION, "checks": checks},
            note="deep",
        )

    def _status(self) -> None:
        self._send(
            200,
            {
                "ok": True,
                "version": GATEWAY_VERSION,
                "bindings": bindings_status(),
                "activity": activity_status(),
            },
        )

    def _orders_list(self, query: Dict[str, List[str]]) -> None:
        status_filter = query.get("status")
        statuses = None
        if status_filter:
            statuses = set()
            for raw in status_filter:
                for part in raw.split(","):
                    if part.strip():
                        statuses.add(part.strip())
            unknown = statuses - set(orders_lib.VALID_STATUSES)
            if unknown:
                self._error(
                    400,
                    "unknown_status",
                    "неизвестный статус: %s" % ", ".join(sorted(unknown)),
                )
                return
        company = (query.get("company") or [None])[0]
        try:
            found = orders_lib.list_orders(statuses=statuses, company=company)
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        self._send(200, {"ok": True, "orders": found, "count": len(found)})

    def _order_get(self, order_id: str) -> None:
        if not order_id:
            self._error(404, "not_found", "не указан идентификатор наряда")
            return
        try:
            order = orders_lib.get_order(_unquote_id(order_id))
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        if order is None:
            self._error(404, "order_not_found", "наряда с таким идентификатором нет")
            return
        self._send(200, {"ok": True, "order": order})

    def _cross_company_errors(self, payload: Dict[str, Any]) -> List[Dict[str, str]]:
        """Ф10a/R12.12: наряд одной компании на серию, размеченную другой.

        Молча принять — значит увести протокол Anzhee в чат МПервого: ровно та
        утечка между компаниями, которую требование и запрещает. Проверка живёт за
        `ENABLE_PORTAL_ORDERS`: при OFF шлюз обязан вести себя ровно как Ф9.
        Неразмеченная серия конфликтом НЕ считается — иначе половина живых серий
        стала бы «конфликтом» на ровном месте.
        """
        try:
            from notary.lib import identity  # noqa: PLC0415

            if not identity.is_enabled():
                return []
            from notary.cli.registry import load_watched  # noqa: PLC0415

            other = identity.order_company_conflict(payload, load_watched())
        except Exception as exc:  # noqa: BLE001 — реестр недоступен → не утверждаем конфликт
            logger.warning("[orders] проверка компании серии не выполнена: %s", type(exc).__name__)
            return []
        if not other:
            return []
        return [
            {
                "field": "company",
                "code": "company_mismatch",
                "message": (
                    "серия %s размечена компанией %s, а наряд пришёл от %s — "
                    "протокол ушёл бы в чужой чат"
                    % (payload.get("series"), other, payload.get("company"))
                ),
            }
        ]

    def _order_create(self) -> None:
        body, ok = self._read_json_body()
        if not ok:
            return
        now = datetime.now(timezone.utc)
        payload, errors = orders_lib.validate_order_payload(body, now=now)
        errors = errors + self._cross_company_errors(payload)
        if errors:
            self._error(422, "validation_failed", "наряд не принят", errors=errors)
            return
        try:
            order, created = orders_lib.put_order(payload, now=now)
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        self._send(
            201 if created else 200,
            {
                "ok": True,
                "order": order,
                "created": created,
                "capacity": self._capacity_hint(order),
            },
            note=order["id"],
        )

    def _order_update(self, order_id: str) -> None:
        order_id = _unquote_id(order_id)
        body, ok = self._read_json_body()
        if not ok:
            return
        if not isinstance(body, dict):
            self._error(400, "not_an_object", "тело запроса должно быть объектом JSON")
            return
        try:
            existing = orders_lib.get_order(order_id)
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        if existing is None:
            self._error(404, "order_not_found", "наряда с таким идентификатором нет")
            return
        expected_version = body.pop("expected_version", None)
        if expected_version is not None and (
            isinstance(expected_version, bool) or not isinstance(expected_version, int)
        ):
            self._error(400, "bad_version", "expected_version — целое число")
            return
        merged_input = {
            key: existing.get(key)
            for key in (
                "meeting_id",
                "company",
                "series",
                "series_title",
                "room_url",
                "start_at",
                "duration_minutes",
                "expected_participants",
                "telegram_chat_id",
                "owner",
                "keep_audio",
            )
            if key in existing
        }
        merged_input.update(body)
        now = datetime.now(timezone.utc)
        payload, errors = orders_lib.validate_order_payload(
            merged_input, now=now, check_start_in_past="start_at" in body
        )
        errors = errors + self._cross_company_errors(payload)
        if errors:
            self._error(422, "validation_failed", "правка наряда не принята", errors=errors)
            return
        if payload.get("meeting_id") != existing.get("meeting_id"):
            self._error(
                409,
                "meeting_id_immutable",
                "идентификатор встречи менять нельзя: на нём держится "
                "идемпотентность (иначе на встречу приедут два бота)",
            )
            return
        try:
            order = orders_lib.update_order(
                order_id, payload, expected_version=expected_version, now=now
            )
        except orders_lib.OrderConflict as exc:
            self._error(
                409,
                "version_conflict",
                str(exc),
                current_version=exc.current_version,
            )
            return
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        if order is None:
            self._error(404, "order_not_found", "наряда с таким идентификатором нет")
            return
        self._send(
            200,
            {"ok": True, "order": order, "capacity": self._capacity_hint(order)},
            note=order_id,
        )

    def _order_cancel(self, order_id: str) -> None:
        order_id = _unquote_id(order_id)
        try:
            order = orders_lib.cancel_order(order_id)
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        if order is None:
            self._error(404, "order_not_found", "наряда с таким идентификатором нет")
            return
        self._send(200, {"ok": True, "order": order}, note=order_id)

    def _join_now(self) -> None:
        body, ok = self._read_json_body()
        if not ok:
            return
        now = datetime.now(timezone.utc)
        payload, errors = orders_lib.validate_order_payload(
            body, kind=orders_lib.KIND_IMMEDIATE, now=now
        )
        errors = errors + self._cross_company_errors(payload)
        if errors:
            self._error(422, "validation_failed", "наряд не принят", errors=errors)
            return
        try:
            order, created = orders_lib.join_now(payload, now=now)
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        self._send(
            201 if created else 200,
            {
                "ok": True,
                "order": order,
                "created": created,
                # Явное слово порталу: второго бота НЕ подняли, вот текущий наряд.
                "already_running": not created,
                "capacity": self._capacity_hint(order),
            },
            note=order["id"],
        )

    def _leave_now(self) -> None:
        body, ok = self._read_json_body()
        if not ok:
            return
        if not isinstance(body, dict):
            self._error(400, "not_an_object", "тело запроса должно быть объектом JSON")
            return
        meeting_id = body.get("meeting_id")
        room_url = body.get("room_url")
        order_id = None
        if isinstance(meeting_id, str) and meeting_id.strip():
            order_id = orders_lib.order_id_for_meeting(meeting_id.strip())
        elif not (isinstance(room_url, str) and room_url.strip()):
            self._error(
                400, "target_required", "нужен meeting_id или room_url — кого выгонять"
            )
            return
        try:
            order = orders_lib.leave_now(order_id=order_id, room_url=room_url)
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        if order is None:
            self._error(
                404, "order_not_found", "живого наряда по этой встрече/комнате нет"
            )
            return
        self._send(200, {"ok": True, "order": order}, note=order["id"])

    #: Потолок текста правки. Больше — почти наверняка вставленный кусок
    #: документа, а не замечание; модели он всё равно урезается до 2000 знаков
    #: (`feedback_reissue.sanitize_edit_text`), так что принимать мегабайт и
    #: молча выбрасывать хвост было бы враньём порталу.
    MAX_EDIT_TEXT = 4000

    def _protocol_edit(self) -> None:
        """Ф10d (R6.2/R6.3): правка протокола С ПОРТАЛА — в ТО ЖЕ окно, что реплай.

        Своего окна у портала нет by design: встреча, которую правят и в чате, и
        на экране, обязана выпустить ОДНУ следующую версию с обеими правками.
        Поэтому ручка ничего не решает сама — она валидирует тело и передаёт
        правку тому же механизму, что принимает реплаи.

        Ответ несёт `deadline_at` и `hard_deadline_at`: портал показывает
        человеку «новая версия выйдет примерно тогда-то», а не «принято» в
        пустоту (критерий Ф8 «поле правки с показом окна перевыпуска»).
        """
        body, ok = self._read_json_body()
        if not ok:
            return
        if not isinstance(body, dict):
            self._error(400, "not_an_object", "тело запроса должно быть объектом JSON")
            return
        meeting_id = body.get("meeting_id")
        if not isinstance(meeting_id, str) or not meeting_id.strip():
            self._error(400, "meeting_id_required", "нужен идентификатор встречи портала")
            return
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._error(400, "text_required", "текст правки обязателен")
            return
        if len(text) > self.MAX_EDIT_TEXT:
            self._error(
                413, "text_too_long",
                "текст правки длиннее %d знаков" % self.MAX_EDIT_TEXT,
            )
            return
        author = body.get("author")
        portal_user_id = body.get("portal_user_id")
        if not isinstance(portal_user_id, str) or not portal_user_id.strip():
            # 🔴 Без автора правку принимать нельзя: от него зависит и подпись в
            # ленте версий, и право расширять цитирование (R13.2). «Неизвестный
            # участник» здесь означал бы «любой участник — организатор».
            self._error(
                400, "portal_user_id_required",
                "нужен идентификатор автора правки на портале",
            )
            return
        try:
            from notary.lib import feedback_worker  # noqa: PLC0415

            summary = feedback_worker.apply_portal_edit(
                meeting_id=meeting_id.strip(),
                text=text,
                author=author if isinstance(author, str) else "",
                portal_user_id=portal_user_id.strip(),
                session_uid=body.get("session_uid")
                if isinstance(body.get("session_uid"), str)
                else None,
                edit_id=body.get("edit_id") if isinstance(body.get("edit_id"), str) else None,
            )
        except Exception as exc:  # noqa: BLE001 — разбираем по типу ниже
            code = getattr(exc, "code", "")
            message = getattr(exc, "message", "") or str(exc)
            if not code:
                logger.warning("[gateway] правка с портала упала (%s)", type(exc).__name__)
                self._error(503, "edit_unavailable", "правку принять не удалось")
                return
            status = {
                "meeting_not_found": 404,
                "empty": 400,
                "window_full": 429,
                "edits_disabled": 503,
            }.get(code, 422)
            self._error(status, code, message)
            return
        self._send(200, {"ok": True, "edit": summary}, note=meeting_id.strip())

    def _capacity_hint(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """«В это время бот занят» — ПРИ ПОСТАНОВКЕ наряда, а не молчание до встречи.

        Подсказка, не запрет: окончательное решение принимает планировщик в
        момент старта, а портал показывает предупреждение в форме сразу.

        🔴 Ф10b: считаются ОБА источника занятости — наряды портала И очередь
        планировщика (личные серии владельца). До этого шлюз видел только
        наряды и обещал порталу свободный слот в час, когда у владельца уже
        стоят две встречи. Потолок ОБЩИЙ на обе компании: он про ядра машины, а
        не про права компании (см. `lib/slots.py`).
        """
        limit = max_concurrent_bots()
        try:
            start = datetime.fromisoformat(str(order.get("start_at")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return {"limit": limit, "known": False}
        try:
            from notary.lib import slots  # noqa: PLC0415

            hint = slots.occupancy(
                start,
                int(order.get("duration_minutes") or orders_lib.DEFAULT_DURATION_MINUTES),
                exclude_order_id=order.get("id"),
            )
        except Exception as exc:  # noqa: BLE001
            # Подсказка о занятости — не повод потерять уже принятый наряд:
            # ответ уходит без неё, а не 500-й со стектрейсом.
            return {"limit": limit, "known": False, "error": type(exc).__name__}
        # `overlapping_orders` оставлен для клиента портала, написанного по Ф9.
        hint["overlapping_orders"] = int(hint.get("busy") or 0)
        return hint

    def _protocol(self, query: Dict[str, List[str]]) -> None:
        meeting_id = (query.get("meeting_id") or [""])[0].strip()
        fmt = (query.get("format") or ["md"])[0].strip().lower()
        if not meeting_id:
            self._error(400, "meeting_id_required", "нужен meeting_id")
            return
        if fmt not in ("md", "pdf"):
            self._error(400, "bad_format", "format=md или format=pdf")
            return
        try:
            order = orders_lib.get_order(orders_lib.order_id_for_meeting(meeting_id))
        except orders_lib.OrderRegistryError as exc:
            self._error(503, "registry_unavailable", str(exc))
            return
        if order is None:
            self._error(404, "order_not_found", "наряда по этой встрече нет")
            return
        path = protocol_path_for_order(order)
        if path is None:
            self._error(
                404,
                "protocol_not_ready",
                "протокол этой встречи ещё не собран",
            )
            return
        try:
            md_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._error(503, "protocol_unreadable", "не смог прочитать протокол: %s" % exc)
            return
        if fmt == "md":
            body = md_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            # В лог — только объём. Текст протокола в лог не попадает никогда.
            self._log(200, "protocol md %d bytes" % len(body))
            return
        self._protocol_pdf(order, md_text)

    def _protocol_pdf(self, order: Dict[str, Any], md_text: str) -> None:
        import tempfile

        try:
            from notary.lib.protocol_to_pdf import PdfRenderError, render_pdf_from_markdown
        except Exception as exc:  # noqa: BLE001
            self._error(503, "pdf_unavailable", "сборка PDF недоступна: %s" % exc)
            return
        title = str(order.get("series_title") or order.get("series") or "Протокол встречи")
        subtitle = str(order.get("start_at") or "")[:10]
        tmpdir = tempfile.mkdtemp(prefix="gateway-pdf-")
        out_pdf = Path(tmpdir) / "protocol.pdf"
        try:
            render_pdf_from_markdown(md_text, str(out_pdf), title=title, subtitle=subtitle)
            body = out_pdf.read_bytes()
        except PdfRenderError as exc:
            self._error(503, "pdf_unavailable", "PDF временно недоступен: %s" % exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._error(503, "pdf_unavailable", "PDF временно недоступен: %s" % exc)
            return
        finally:
            for leftover in Path(tmpdir).glob("*"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self._log(200, "protocol pdf %d bytes" % len(body))


def _unquote_id(raw: str) -> str:
    from urllib.parse import unquote

    return unquote(raw)


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(host: Optional[str] = None, port: Optional[int] = None) -> GatewayServer:
    host = host if host is not None else (
        os.environ.get("MEETING_NOTARY_GATEWAY_HOST") or DEFAULT_HOST
    )
    port = port if port is not None else _env_int(
        "MEETING_NOTARY_GATEWAY_PORT", DEFAULT_PORT
    )
    return GatewayServer((host, port), _Handler)


def main() -> int:
    setup_logging()
    token, reason = resolve_token()
    if token is None:
        # Не падаем: живой процесс, отвечающий 503 с причиной, диагностируется
        # порталом и сторожем. Мёртвый процесс даёт «connection refused», по
        # которому не отличить «шлюз не настроен» от «VPS лежит».
        logger.error(
            "Секрет шлюза не настроен (%s) — шлюз поднят в режиме fail-closed, "
            "все запросы получают 503",
            reason,
        )
    server = build_server()
    host, port = server.server_address[0], server.server_address[1]
    logger.info(
        "Шлюз слушает %s:%s (лимит тела %d байт, потолок ботов %d)",
        host,
        port,
        max_body_bytes(),
        max_concurrent_bots(),
    )
    if host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(
            "🔴 Шлюз слушает НЕ локальный адрес (%s): наружу он обязан ходить "
            "только через Caddy с TLS, иначе Bearer идёт открытым текстом",
            host,
        )

    def _stop(signum: int, _frame: Any) -> None:
        logger.info("Сигнал %s — останавливаюсь", signum)
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        logger.info("Шлюз остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
