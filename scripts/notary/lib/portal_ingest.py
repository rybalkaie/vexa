"""Ф10c: разговор с ручкой приёма портала — форма тела, ACK, отправка, разбор очереди.

Пара к `lib/portal_delivery.py`: тот держит очередь на диске, этот знает, ЧТО и
КУДА отправить и что считать подтверждением.

🔴 ТРЕТЬЯ ТОЧКА СТЫКА С ПОРТАЛОМ, И ДВЕ ПРЕДЫДУЩИЕ РАЗЪЕХАЛИСЬ. В этом же прогоне
уже дважды ловили «две половины по отдельности зелёные, вместе не работают»:
портал слал наряд в camelCase, шлюз ждал snake_case (422, ноль нарядов); портал
звал ручку PDF по несуществующему адресу. Поэтому:

  * имена полей — **snake_case**, как во всём контракте шлюза. Единственный
    источник истины — `docs/gateway-contract.md`, раздел «Бот → портал»;
  * форма ACK описана там же дословно, и проба `TestContractShape` сверяет её с
    этим модулем — чтобы расхождение падало у нас, а не на живой встрече.

🔴 ПОДТВЕРЖДЕНИЕ — ЭТО НЕ HTTP 200. Портал обязан вернуть ТЕЛО с `ack`, чей
`delivery_key` совпадает с отправленным. «200 без ACK» и «ACK с чужим ключом»
означают ровно одно: результат не принят, повторяем. На этом стоит удаление
исходников — если считать ACK'ом любой 200, то прокси, отдавший «ok» за портал,
сотрёт запись живого разговора.

🔴 ТОЛЬКО HTTPS. Bearer уходит в теле заголовка открытым текстом; отправить его
по http — значит отдать ключ ко всем протоколам обеих компаний. Схема не https →
не отправляем ВООБЩЕ и не тратим попытку (это ошибка настройки, а не портала).

Опасная тройка: тело несёт протокол и транскрипт живого разговора. В журнал
уходят только ключ, код ответа и класс ошибки — ни строки текста, ни ответа
портала целиком.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from notary.lib import portal_delivery  # noqa: E402

logger = logging.getLogger("notary.portal_ingest")

URL_ENV = "MEETING_NOTARY_PORTAL_INGEST_URL"
TOKEN_ENV = "MEETING_NOTARY_PORTAL_INGEST_TOKEN"
TIMEOUT_ENV = "MEETING_NOTARY_PORTAL_INGEST_TIMEOUT_S"
MAX_BODY_ENV = "MEETING_NOTARY_PORTAL_INGEST_MAX_BODY"

#: Потолок тела на стороне портала. Ручка приёма мозга держит 25 МиБ, и приём
#: встреч сделан по её образцу; берём тот же порядок, но с запасом вниз —
#: превышение здесь стоит не отказа, а урезанного транскрипта (см. `shrink`).
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_S = 30

#: Что за исход попытки. Разделение важно: «повторяем» тратит попытку, «настройка»
#: не тратит (иначе опечатка в адресе за два часа похоронила бы все доставки), а
#: «отказ» хоронит элемент сразу — повторять нечего, тело не пройдёт никогда.
OUTCOME_DELIVERED = "delivered"
OUTCOME_RETRY = "retry"
OUTCOME_PERMANENT = "permanent"
OUTCOME_MISCONFIGURED = "misconfigured"


class Outcome:
    """Исход одной попытки. Без текста ответа — только класс и код."""

    __slots__ = ("kind", "http_status", "error", "ack")

    def __init__(
        self,
        kind: str,
        *,
        http_status: Optional[int] = None,
        error: str = "",
        ack: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.kind = kind
        self.http_status = http_status
        self.error = error
        self.ack = ack

    def __repr__(self) -> str:  # pragma: no cover — диагностика
        return "Outcome(%s, http=%s, error=%r)" % (self.kind, self.http_status, self.error)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


def endpoint() -> str:
    return (os.environ.get(URL_ENV) or "").strip()


def token() -> str:
    return (os.environ.get(TOKEN_ENV) or "").strip()


def timeout_s() -> int:
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def max_body_bytes() -> int:
    raw = (os.environ.get(MAX_BODY_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BODY_BYTES
    return value if value > 0 else DEFAULT_MAX_BODY_BYTES


def config_problem() -> Optional[str]:
    """Почему отправлять нельзя. None — настройка в порядке.

    Возвращает КЛАСС проблемы, а не значение: строка уходит в журнал и в алерт.
    """
    url = endpoint()
    if not url:
        return "portal_url_not_set"
    scheme = (urlsplit(url).scheme or "").lower()
    if scheme != "https":
        # Ровно то же правило, что у клиента шлюза на стороне портала: секрет по
        # http не уходит никогда, даже «на время отладки».
        return "portal_url_not_https"
    if not token():
        return "portal_token_not_set"
    return None


# ---------------------------------------------------------------------------
# Форма тела (контракт)
# ---------------------------------------------------------------------------


def build_payload(
    *,
    order_id: str,
    meeting_id: Optional[str],
    version: int,
    company: Optional[str],
    session_uid: str,
    protocol_md: str = "",
    transcript_text: str = "",
    tasks: Optional[List[Dict[str, Any]]] = None,
    participants: Optional[List[str]] = None,
    expected_participants: Optional[List[str]] = None,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    telegram_delivery: Optional[Dict[str, Any]] = None,
    order_status: Optional[str] = None,
    late_join_minutes: Optional[int] = None,
    generated_at: Optional[str] = None,
    revision_of: Optional[int] = None,
    edits: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Тело `POST /api/meetings/notary/ingest`. Только snake_case.

    Что едет и почему именно это (R7.5): протокол, версия, задачи, транскрипт,
    участники, факт доставки, длительность. PDF НЕ едет — портал забирает его у
    шлюза по запросу (решение №3 плана): гонять мегабайты через очередь ради
    файла, который пересобирается за секунду, незачем.
    """
    protocol = protocol_md or ""
    protocol_cut = len(protocol) > PORTAL_PROTOCOL_MAX
    return {
        "delivery_key": portal_delivery.delivery_key(order_id, version),
        "order_id": order_id,
        "meeting_id": meeting_id,
        "version": int(version),
        "company": company,
        "session_uid": session_uid,
        "order_status": order_status,
        "protocol_md": protocol[:PORTAL_PROTOCOL_MAX],
        "protocol_truncated": protocol_cut,
        "transcript": {
            "text": transcript_text or "",
            "truncated": False,
            "omitted": False,
        },
        # Списки подрезаны под потолки портала: 301-я задача превратила бы всю
        # доставку в отказ навсегда, а не в «одной задачей меньше».
        "tasks": list(tasks or [])[:PORTAL_LIST_MAX],
        "participants": list(participants or [])[:PORTAL_LIST_MAX],
        "expected_participants": list(expected_participants or [])[:PORTAL_LIST_MAX],
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "late_join_minutes": late_join_minutes,
        # Факт доставки в Telegram — чтобы панель «Доставка» на портале говорила
        # «ушло туда-то», а не «неизвестно».
        "telegram_delivery": telegram_delivery or {"status": "unknown"},
        # Ф10d (R6.3): ОТКУДА эта версия. `revision_of` — номер версии, которую
        # она заменяет (у первой доставки None); `edits` — кто и откуда правил.
        # Ровно это портал печатает строкой в истории изменений: «Telegram,
        # Михаил Саргин» против «Портал, Ольга Иванова». Без полей обе ленты
        # слились бы в одну безымянную, и требование «в истории видно источник»
        # выполнить было бы нечем.
        #
        # 🔴 ТЕКСТА ПРАВКИ ЗДЕСЬ НЕТ by design. Содержание правки уже уехало в
        # `protocol_md` — применённым. Дублировать его отдельным полем значит
        # положить в ленту версий сырую реплику участника, которую портал будет
        # хранить и показывать; ленте достаточно «кто, откуда, когда».
        "revision_of": revision_of,
        "edits": [
            {
                "source": e.get("source") or "telegram",
                "author": str(e.get("author") or "")[:120],
                "portal_user_id": e.get("portal_user_id"),
                "at": e.get("at"),
                "is_organizer": e.get("is_organizer"),
            }
            for e in (edits or [])[:PORTAL_LIST_MAX]
            if isinstance(e, dict)
        ],
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
    }


#: Потолки ОТДЕЛЬНЫХ полей на стороне портала (`lib/meetings/notaryIngest.ts`).
#: Их превышение — это 422, то есть отказ НАВСЕГДА и алерт владельцу. Дешевле
#: подрезать здесь и честно пометить, чем узнать об этом на живой встрече.
PORTAL_PROTOCOL_MAX = 1024 * 1024
PORTAL_LIST_MAX = 300


def build_purge_event(
    *,
    meeting_id: str,
    delivery_key: str,
    order_id: Optional[str] = None,
    company: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Отчёт «исходники у бота снесены» (диалект событий портала).

    🔴 ЗАЧЕМ ОТДЕЛЬНЫМ СООБЩЕНИЕМ, ЕСЛИ ЕСТЬ ACK. ACK доказывает, что ПОРТАЛ
    ПРИНЯЛ результат, и ничего не говорит о том, что бот действительно стёр WAV.
    Панель «Запись» различает эти два состояния словами, и без отчёта она
    навсегда застревает на «ждём отчёта бота» (R7.9, R5.12). Предъявляем тот же
    `delivery_key`, который портал вернул в ACK, — он же доказательство, что
    отчёт про ЭТУ встречу, а не про соседнюю.
    """
    return {
        "meeting_id": meeting_id,
        "event": "sources_purged",
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
        "order_id": order_id,
        "company": company,
        "delivery_key": delivery_key,
    }


#: Хвост ключа события «бот не пришёл». Ровно этот ключ портал вернул бы в ACK и
#: сам, если его не прислать (`notaryIngest.ts`: `portal:${meeting.id}#${event}`),
#: но шлём явно — сверка ACK не должна зависеть от того, как портал его синтезирует.
NO_SHOW_SUFFIX = "#no_show"


def no_show_key(order_id: str) -> str:
    """`portal:<meeting_id>#no_show` — один на наряд: второй «не пришёл» по той же
    встрече порталу ничего не добавит (статус записи назад не ходит)."""
    return "%s%s" % (str(order_id or "").strip(), NO_SHOW_SUFFIX)


def build_no_show_event(
    *,
    meeting_id: str,
    order_id: str,
    company: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Событие «бот не пришёл» (диалект событий портала, `event: no_show`).

    Шлётся ТОЛЬКО после окончательного срыва входа — когда сторож
    (`lib/join_watch.py`) сдался после повтора: раньше это было бы ложное «бот не
    пришёл» при сорвавшейся первой попытке, которую повтор ещё мог спасти. Портал
    ставит встрече `BOT_NO_SHOW`, звонит организатору колокольчиком и открывает
    загрузку записи вручную (`notaryIngest.ts`, `recordingUpload.ts`).

    Поля — ровно схема портала `notaryEventSchema`; `delivery_key` портал
    возвращает эхом в ACK (`validate_ack` сверяет дословно).
    """
    return {
        "meeting_id": meeting_id,
        "event": "no_show",
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
        "order_id": order_id,
        "company": company,
        "delivery_key": no_show_key(order_id),
    }


def encode(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def shrink(payload: Dict[str, Any], limit: int) -> Tuple[Dict[str, Any], bool]:
    """Ужать тело под потолок портала. Возвращает (тело, ужимали ли).

    Порядок жертв продуман: сначала режется ТРАНСКРИПТ (портал всё равно может
    запросить его отдельно и он есть у бота на диске), протокол — в последнюю
    очередь. Полный отказ «тело великовато» здесь был бы худшим исходом: встреча
    трёхчасового совещания не долетела бы никогда, а именно её и жалко.
    """
    body = encode(payload)
    if len(body) <= limit:
        return payload, False
    shrunk = json.loads(json.dumps(payload))  # копия, исходник не портим
    transcript = shrunk.get("transcript")
    if isinstance(transcript, dict) and transcript.get("text"):
        overflow = len(body) - limit
        text = str(transcript["text"])
        # Режем с запасом: JSON-экранирование и кириллица в utf-8 дают больше
        # байт, чем знаков, — «ровно на overflow» промахнулось бы.
        keep = max(0, len(text) - (overflow * 2 + 4096))
        transcript["text"] = text[:keep]
        transcript["truncated"] = True
        if keep == 0:
            transcript["omitted"] = True
        shrunk["transcript"] = transcript
        body = encode(shrunk)
    if len(body) > limit:
        # Не влез даже без транскрипта — режем протокол, но говорим об этом
        # флагом, а не молча.
        protocol = str(shrunk.get("protocol_md") or "")
        if protocol:
            overflow = len(body) - limit
            shrunk["protocol_md"] = protocol[: max(0, len(protocol) - (overflow * 2 + 4096))]
            shrunk["protocol_truncated"] = True
    return shrunk, True


# ---------------------------------------------------------------------------
# ACK
# ---------------------------------------------------------------------------


def validate_ack(body: Any, expected_key: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Подтверждение портала. Возвращает (ack, причина отказа).

    Принимается ТОЛЬКО объект вида
    `{"ok": true, "ack": {"delivery_key": "<тот же>", "received_at": "...", …}}`.

    Всё остальное — не подтверждение:
      * тело не разобралось               → `ack_not_json`
      * `ok` не true                      → `ack_not_ok`
      * нет объекта `ack`                 → `ack_missing` («200 без ACK»)
      * `delivery_key` чужой или пустой   → `ack_key_mismatch`

    Ни одна из этих веток не разрешает удалять исходники — в этом весь смысл
    отдельного подтверждения.
    """
    if not isinstance(body, dict):
        return None, "ack_not_json"
    if body.get("ok") is not True:
        return None, "ack_not_ok"
    ack = body.get("ack")
    if not isinstance(ack, dict):
        return None, "ack_missing"
    got = ack.get("delivery_key")
    if not isinstance(got, str) or got != expected_key:
        return None, "ack_key_mismatch"
    return ack, ""


# ---------------------------------------------------------------------------
# Транспорт
# ---------------------------------------------------------------------------


def _http_post(url: str, headers: Dict[str, str], body: bytes, timeout: int) -> Tuple[int, bytes]:
    """Шов для проб: настоящий POST. Пробы подменяют ЭТУ функцию, не сеть.

    `urllib` из стандартной библиотеки, не `requests`: модуль зовётся из
    collector'а на VPS, а лишняя зависимость в этом месте — лишний способ
    сломать доставку при выкатке.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return int(response.status or 0), response.read()


def send(
    item: Dict[str, Any],
    *,
    transport: Optional[Callable[[str, Dict[str, str], bytes, int], Tuple[int, bytes]]] = None,
) -> Outcome:
    """Одна попытка доставки одного элемента очереди."""
    problem = config_problem()
    if problem:
        return Outcome(OUTCOME_MISCONFIGURED, error=problem)

    key = str(item.get("key") or "")
    # Ключ, который портал вернёт в ACK, не всегда равен ключу ЭЛЕМЕНТА очереди:
    # отчёт «исходники снесены» предъявляет ключ ИСХОДНОЙ доставки, и портал
    # эхом отдаёт именно его. Свой ключ элемента очереди при этом нужен, чтобы
    # два сообщения по одной встрече не затирали друг друга на диске.
    ack_key = str(item.get("ack_key") or key)
    payload = item.get("payload")
    if not isinstance(payload, dict):
        # Элемент есть, тела нет — отправлять нечего и никогда не будет.
        return Outcome(OUTCOME_PERMANENT, error="payload_missing")

    limit = max_body_bytes()
    payload, shrunk = shrink(payload, limit)
    if shrunk:
        logger.warning(
            "[portal-ingest] тело %s не влезало в потолок портала (%d Б) — "
            "транскрипт урезан, протокол и задачи отправлены целиком", key, limit,
        )
    body = encode(payload)
    if len(body) > limit:
        return Outcome(OUTCOME_PERMANENT, error="body_too_large_after_shrink")

    headers = {
        "Authorization": "Bearer %s" % token(),
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        # Ключ идемпотентности дублируется заголовком: портал вправе отбить
        # повтор, не разбирая тело.
        "X-Notary-Delivery-Key": ack_key,
    }
    post = transport or _http_post
    try:
        status, raw = post(endpoint(), headers, body, timeout_s())
    except urllib.error.HTTPError as e:  # 4xx/5xx приходят исключением
        status = int(e.code or 0)
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as e:  # noqa: BLE001 — сеть рвётся, dns не резолвится, таймаут
        return Outcome(OUTCOME_RETRY, error="transport_%s" % type(e).__name__)

    if status == 422:
        # Портал разобрал тело и сказал «не приму». Повторять бессмысленно: то же
        # тело даст тот же ответ. Владельцу — алерт, исходники — держим.
        return Outcome(OUTCOME_PERMANENT, http_status=status, error="schema_rejected")
    if status == 413:
        return Outcome(OUTCOME_PERMANENT, http_status=status, error="body_too_large")
    if status != 200:
        # 401 (чужой токен), 404 (не та ручка), 500/502 (портал лежит), 429 —
        # всё это чинится на той стороне, и все они повторяемы. Никакого
        # «навсегда» по коду ответа: 401 после ротации секрета — обычное дело.
        return Outcome(OUTCOME_RETRY, http_status=status, error="http_%d" % status)

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return Outcome(OUTCOME_RETRY, http_status=status, error="ack_not_json")
    ack, reason = validate_ack(parsed, ack_key)
    if ack is None:
        return Outcome(OUTCOME_RETRY, http_status=status, error=reason)
    return Outcome(OUTCOME_DELIVERED, http_status=status, ack=ack)


# ---------------------------------------------------------------------------
# Разбор очереди
# ---------------------------------------------------------------------------


def drain(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    transport: Optional[Callable[[str, Dict[str, str], bytes, int], Tuple[int, bytes]]] = None,
) -> Dict[str, int]:
    """Отправить всё созревшее. Зовётся тиком collector'а и вручную из CLI.

    Возвращает счётчики (без единого текста). Fail-open по своей природе: любой
    сбой одного элемента не имеет права остановить остальные и тем более —
    остановить конвейер записи встреч.
    """
    stats = {"sent": 0, "retry": 0, "dead": 0, "skipped": 0}
    if not portal_delivery.is_enabled():
        return stats
    problem = config_problem()
    if problem:
        pending = portal_delivery.summary().get("pending") or 0
        if pending:
            # Настройка сломана, а результаты копятся — это молчание худшего
            # сорта. Говорим владельцу, но попытку НЕ тратим: иначе опечатка в
            # адресе за два часа похоронила бы все доставки разом.
            logger.error(
                "[portal-ingest] доставка в портал не настроена (%s), в очереди %s — "
                "исходники удерживаются", problem, pending,
            )
            try:
                from notary.lib import notify  # noqa: PLC0415

                notify.push(
                    "🔴 Ватсон не может отдать результаты встреч порталу: не настроен канал "
                    "приёма (%s). В очереди %s. Записи не удалены — доедут, как только "
                    "починим." % (problem, pending),
                    dedupe_key="portal-ingest-misconfigured:%s" % problem,
                )
            except Exception:  # noqa: BLE001
                pass
        stats["skipped"] = int(pending)
        return stats

    for item in portal_delivery.due_items(now)[: max(1, limit)]:
        key = str(item.get("key") or "")
        outcome = send(item, transport=transport)
        if outcome.kind == OUTCOME_DELIVERED:
            portal_delivery.record_delivered(key, outcome.ack or {}, now=now)
            stats["sent"] += 1
            continue
        if outcome.kind == OUTCOME_MISCONFIGURED:
            stats["skipped"] += 1
            continue
        updated = portal_delivery.record_failure(
            key,
            error=outcome.error,
            http_status=outcome.http_status,
            permanent=outcome.kind == OUTCOME_PERMANENT,
            now=now,
        )
        if updated is not None and updated.get("status") == portal_delivery.STATUS_DEAD:
            stats["dead"] += 1
        else:
            stats["retry"] += 1
    return stats
