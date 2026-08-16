"""Бот → портал: приём кода привязки и привязка группы (Ф12, R8.1–R8.6).

ЧТО ЭТО. Вторая (после `portal_ingest`) ручка портала, к которой ходит бот, и
единственная, у которой ответ ПРЕДНАЗНАЧЕН ЧЕЛОВЕКУ: портал возвращает готовую
фразу, бот печатает её дословно.

🔴 ПОЧЕМУ ТЕКСТЫ ОТВЕТОВ ЖИВУТ НА ПОРТАЛЕ, А НЕ ЗДЕСЬ. Они обязаны совпадать с
тем, что написано на экране «Ватсон» (и «код живёт 15 минут», и «возьмите новый
на портале»), а экран и бот — РАЗНЫЕ репозитории с разными выкатками. Держи
тексты здесь — первая же правка формулировки на портале разошлась бы с ботом
молча, и человек читал бы две разные инструкции. Отсюда правило: бот НЕ решает,
что случилось, и не сочиняет ответ; он только доставляет.

🔴 ЧЕГО ЗДЕСЬ НЕТ: РЕШЕНИЙ О ПРИВЯЗКЕ. Ни срока кода, ни счётчика попыток, ни
«чей это код» — всё это знает и решает портал, потому что там лежат данные.
Бот, который сам судит о коде, — это второй источник правды об идентичности.

═══ ОПАСНАЯ ТРОЙКА ═══
  * КОД В ЖУРНАЛ НЕ ПИШЕТСЯ НИКОГДА. Это пароль на пятнадцать минут; в логах
    листенера (уровень INFO, файл на диске VPS) ему места нет — ни целиком, ни
    «первыми тремя знаками». В журнал уходит только КЛАСС исхода.
  * ЧИСЛОВОЙ ОТПРАВИТЕЛЬ И ЧАТ ТОЖЕ НЕ ПИШУТСЯ. В остальном листенере
    `chat_id`/`from_id` печатаются открыто — это давняя норма проекта, и её
    здесь НЕ наследуем: рядом с исходом привязки эта пара означает «кто именно
    сейчас подключает себе чужие протоколы», то есть след, которого в общем
    логе быть не должно.
  * ТОКЕН НЕ ПИШЕТСЯ И НЕ ВОЗВРАЩАЕТСЯ. Заголовок собирается на месте вызова.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

from notary.lib import portal_ingest

logger = logging.getLogger("notary.portal_link")

#: Адрес ручки привязки. Не задан — выводится из адреса ingest'а (см. `endpoint`).
URL_ENV = "MEETING_NOTARY_PORTAL_LINK_URL"

DEFAULT_TIMEOUT_S = 15

#: Потолок ответа. Оттуда приезжает одна фраза; всё, что больше, — не наш портал.
MAX_RESPONSE_BYTES = 16 * 1024


def endpoint() -> str:
    """Адрес ручки.

    🔴 ВЫВОДИТСЯ ИЗ АДРЕСА INGEST'А, ЕСЛИ СВОЙ НЕ ЗАДАН. Обе ручки живут на
    одном портале и отличаются последним сегментом пути; заставлять владельца
    прописывать вторую переменную ради этого значило бы получить выкатку, где
    ingest настроен, а привязка молча не работает — причём различить это можно
    только по тому, что люди не могут подключиться.

    Вывод ТОЛЬКО из точного хвоста `/ingest`: гадать по произвольному адресу
    нельзя, иначе бот начнёт стучаться в выдуманный URL.
    """
    explicit = (os.environ.get(URL_ENV) or "").strip()
    if explicit:
        return explicit
    ingest = portal_ingest.endpoint()
    trimmed = ingest.rstrip("/")
    if trimmed.endswith("/ingest"):
        return trimmed[: -len("/ingest")] + "/link"
    return ""


def token() -> str:
    """Тот же секрет, что у ingest'а: направление одно (бот → портал)."""
    return portal_ingest.token()


def timeout_s() -> int:
    return DEFAULT_TIMEOUT_S


def config_problem() -> Optional[str]:
    """Почему ходить нельзя. ``None`` — настройка в порядке."""
    url = endpoint()
    if not url:
        return "portal_link_url_not_set"
    if (urlsplit(url).scheme or "").lower() != "https":
        # То же правило, что у соседа: секрет по http не уходит никогда.
        return "portal_link_url_not_https"
    if not token():
        return "portal_token_not_set"
    return None


class Result(NamedTuple):
    """Чем кончился поход.

    ``reply`` — готовая фраза ОТ ПОРТАЛА, её и печатаем человеку. ``None``
    означает «портал не ответил» — тогда фразу берёт вызывающий из своих
    локальных (`UNAVAILABLE_REPLY`): молчание боту не годится, человек стоит и
    ждёт.
    """

    ok: bool
    reply: Optional[str]
    outcome: Optional[str]
    error: Optional[str]


#: Что сказать, когда портал недоступен. ЕДИНСТВЕННЫЙ текст на стороне бота, и
#: он намеренно ничего не утверждает о коде: мы не знаем, верен он или нет.
UNAVAILABLE_REPLY = (
    "Портал сейчас не отвечает — не могу проверить. Попробуйте ещё раз через пару минут."
)


def _http_post(url: str, headers: Dict[str, str], body: bytes, timeout: int) -> Tuple[int, bytes]:
    """Шов для проб: настоящий POST. Пробы подменяют ЭТУ функцию, не сеть."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return int(response.status or 0), response.read(MAX_RESPONSE_BYTES + 1)


def _post(
    payload: Dict[str, Any],
    *,
    transport: Optional[Callable[[str, Dict[str, str], bytes, int], Tuple[int, bytes]]] = None,
) -> Result:
    problem = config_problem()
    if problem:
        # Настройки нет — в сеть не идём вовсе. В журнал КЛАСС проблемы, не URL
        # и не токен.
        logger.warning("[bind] портал не настроен: %s", problem)
        return Result(ok=False, reply=None, outcome=None, error=problem)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer %s" % token(),
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    send = transport or _http_post
    try:
        status, raw = send(endpoint(), headers, body, timeout_s())
    except urllib.error.HTTPError as exc:  # noqa: PERF203 — разные исходы, разные ветки
        status, raw = int(exc.code or 0), b""
    except Exception as exc:  # сеть, TLS, DNS
        logger.warning("[bind] портал недоступен: %s", type(exc).__name__)
        return Result(ok=False, reply=None, outcome=None, error="unreachable")

    if status != 200:
        # 🔴 ТЕЛО ОТВЕТА В ЖУРНАЛ НЕ ПИШЕМ — только код. То же правило, что у
        # ingest'а: там едут дословные реплики живых людей, здесь — коды.
        logger.warning("[bind] портал ответил %s", status)
        return Result(ok=False, reply=None, outcome=None, error="http_%s" % status)

    if len(raw) > MAX_RESPONSE_BYTES:
        logger.warning("[bind] ответ портала больше потолка")
        return Result(ok=False, reply=None, outcome=None, error="response_too_large")

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        logger.warning("[bind] ответ портала не разобрался")
        return Result(ok=False, reply=None, outcome=None, error="bad_response")

    if not isinstance(parsed, dict):
        return Result(ok=False, reply=None, outcome=None, error="bad_response")
    reply = parsed.get("reply")
    outcome = parsed.get("outcome")
    if not isinstance(reply, str) or not reply.strip():
        return Result(ok=False, reply=None, outcome=None, error="no_reply")
    logger.info("[bind] исход: %s", outcome if isinstance(outcome, str) else "?")
    return Result(
        ok=True,
        reply=reply.strip(),
        outcome=outcome if isinstance(outcome, str) else None,
        error=None,
    )


def submit_code(
    *,
    telegram_user_id: int,
    telegram_username: Optional[str],
    code: str,
    transport: Optional[Callable[[str, Dict[str, str], bytes, int], Tuple[int, bytes]]] = None,
) -> Result:
    """Отдать порталу код, который прислал человек."""
    payload: Dict[str, Any] = {
        "kind": "code",
        # snake_case — как во всём контракте со шлюзом. Проект уже платил за
        # расхождение регистров нулём доставленных нарядов.
        "telegram_user_id": str(telegram_user_id),
        "code": code,
    }
    if telegram_username:
        payload["telegram_username"] = telegram_username
    return _post(payload, transport=transport)


def submit_group(
    *,
    telegram_chat_id: int,
    title: str,
    member_count: Optional[int],
    telegram_user_id: int,
    organization: Optional[str],
    transport: Optional[Callable[[str, Dict[str, str], bytes, int], Tuple[int, bytes]]] = None,
) -> Result:
    """Попросить портал завести эту группу компании отправителя."""
    payload: Dict[str, Any] = {
        "kind": "group",
        "telegram_chat_id": str(telegram_chat_id),
        "telegram_user_id": str(telegram_user_id),
        "title": title,
    }
    if isinstance(member_count, int) and member_count >= 0:
        payload["member_count"] = member_count
    if organization:
        payload["organization"] = organization
    return _post(payload, transport=transport)
