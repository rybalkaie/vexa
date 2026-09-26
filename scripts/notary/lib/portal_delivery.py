"""Ф10c: доставка результата встречи в портал — очередь на диске, ACK, удержание исходников.

ЗАЧЕМ. Сегодня цепочка результата такая: finalize собрал протокол → доставил в
Telegram → collector увидел `delivery.status in {sent, skipped}` и **удалил WAV**
(`collector.py`, «WAV удалён после успешной доставки»). Пока получатель один —
Telegram, — это работает. Как только вторым получателем становится портал,
цепочка «протокол ушёл в Telegram → WAV удалён → портал лежал → повтор жил в
памяти процесса → рестарт» теряет результат НЕОБРАТИМО. Класс инцидента уже был
17.07: тихий провал неотличим от «встречи не было».

ЧТО ДЕЛАЕТ ЭТОТ МОДУЛЬ (три вещи, и ровно они):

  1. **Очередь доставки НА ДИСКЕ.** Один файл — один результат. Переживает
     рестарт службы, потому что состояние не в памяти, а в файле, и следующий
     тик читает его с нуля. Потолок попыток и экспоненциальная пауза.
  2. **Удержание исходников (`hold`).** Пока результат не подтверждён порталом,
     WAV и рабочий транскрипт этой встречи НЕ УДАЛЯЕТ НИКТО. Удержание —
     отдельный факт на диске, а не «в очереди что-то лежит»: сломанная очередь
     не имеет права разблокировать удаление.
  3. **Алерт владельцу при исчерпании потолка.** Ровно потому, что тихий провал
     здесь неотличим от «встречи не было».

🔴 ВСЁ ЖИВЁТ ЗА `ENABLE_PORTAL_ORDERS` (дефолт OFF, канон Ватсона). При OFF
каждая функция возвращает «ничего нет» ДО единого обращения к диску — как в
Ф10a/Ф10b, структурной гарантией, а не обещанием. Личные серии владельца сегодня
доставляются в Telegram и обязаны продолжать ровно так же.

🔴 НАПРАВЛЕНИЯ ОШИБОК РАЗНЫЕ, И ЭТО НАРОЧНО:

  * очередь — **fail-open** (не прочиталась → «доставлять нечего», бот работает
    как без портала: сломанная доставка не имеет права остановить запись встреч);
  * удержание — **fail-closed** (не прочиталось → считаем, что держим). Удаление
    необратимо, и цена ошибки здесь несимметрична: лишний WAV на диске чинится
    одной командой, потерянная запись разговора — ничем.

🔴 ПУТИ РЕЗОЛВЯТСЯ ЛЕНИВО (`orders.orders_file()` рядом). Ровно этот класс
дефекта заставлял пробы писать в БОЕВОЙ `.state.json` (приёмка 05.08.2026).
Сторожа — `test_state_path_isolation.py`, `test_gateway_state_isolation.py`.

🔴 ОПАСНАЯ ТРОЙКА (личные данные + чужой контент + отправка наружу) — это ровно
этот модуль: в теле элемента очереди лежит ТЕКСТ протокола и транскрипта живого
разговора. Поэтому:

  * файлы очереди — `0o600`, каталог — `0o700` (как файл окружения с секретом);
  * в журнал уходят только ключи, счётчики и КЛАССЫ ошибок. Ни реплики, ни
    протокол, ни имена участников в лог не попадают — включая ветку ошибки, где
    соблазн «залогировать тело для разбора» максимален;
  * ответ портала в лог тоже не эхуется целиком — только код и класс.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Модуль зовётся и из пакета (`notary.lib.portal_delivery`), и из finalize, где
# на sys.path лежит только `scripts/notary`. Без этой вставки ленивый импорт
# `notary.cli.registry` внутри падал бы там, где его меньше всего ждут — на
# гейте удаления исходников, и падал бы в сторону «удаляй».
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logger = logging.getLogger("notary.portal_delivery")

SCHEMA_VERSION = 1

ROOT_DIRNAME = ".portal-delivery"
ITEMS_DIRNAME = "items"
HOLDS_DIRNAME = "holds"

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_DEAD = "dead"

#: Маркер рядом с WAV — единственное, что понимает метельщик из systemd (там
#: bash, JSON он не разберёт). Имя без точки в начале: `rm -f <sid>.*` в юните
#: сметает и его, поэтому маркер обязан лежать в той же группе имён.
HOLD_MARKER_SUFFIX = ".ack-hold"

# Потолок попыток и пауза. Считано от требования R13.3: «положи портал на 30
# минут → протокол доехал после подъёма БЕЗ ручных действий». Паузы
# 1-2-4-8-15-15-… минут накрывают тридцатиминутный простой пятью попытками, а
# полный бюджет (12 попыток) — примерно два с четвертью часа. Дальше молчать
# нельзя: владельцу уходит алерт, но исходники ОСТАЮТСЯ (см. `record_failure`).
DEFAULT_MAX_ATTEMPTS = 12
DEFAULT_BACKOFF_BASE_S = 60
DEFAULT_BACKOFF_CAP_S = 15 * 60

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class PortalDeliveryError(RuntimeError):
    """Очередь физически не может принять результат (диск, права, форма файла)."""


# ---------------------------------------------------------------------------
# Выключатель
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Аварийный выключатель `ENABLE_PORTAL_ORDERS`. Дефолт OFF.

    Импорт ленивый и защищённый: слой идентичности мог не доехать при выкатке —
    тогда доставка в портал считается выключенной, а не роняет collector.
    """
    try:
        from notary.lib import identity  # noqa: PLC0415

        return identity.is_enabled()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Пути (ЛЕНИВО, см. шапку)
# ---------------------------------------------------------------------------


def _registry_dir() -> Path:
    from notary.cli.registry import _resolve_registry_dir  # noqa: PLC0415

    return _resolve_registry_dir()


def root_dir() -> Path:
    """Каталог очереди доставки НА МОМЕНТ ВЫЗОВА."""
    return _registry_dir() / ROOT_DIRNAME


def items_dir() -> Path:
    return root_dir() / ITEMS_DIRNAME


def holds_dir() -> Path:
    return root_dir() / HOLDS_DIRNAME


def _ensure_dir(path: Path) -> Path:
    """Создать каталог с правами 0o700. Внутри лежат тексты живых разговоров."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover — чужой владелец каталога
        pass
    return path


def _atomic_write_json(path: Path, data: Any) -> None:
    """Запись «всё или ничего» + права 0o600 ДО того, как файл станет видимым.

    `chmod` на временном файле, а не на конечном: между `replace` и `chmod`
    существовало бы окно, в котором протокол живого разговора читается любым
    пользователем машины.
    """
    _ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Прочитать элемент. Битый/чужой формы файл → None + WARNING, НЕ исключение.

    Битый файл не «чиним» перезаписью: один нечитаемый элемент не имеет права ни
    остановить доставку остальных, ни быть молча затёртым (в нём лежит результат
    встречи).
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning(
            "[portal-delivery] элемент %s нечитаем (%s) — пропущен, файл не тронут",
            path.name, type(e).__name__,
        )
        return None
    if not isinstance(data, dict) or not isinstance(data.get("key"), str):
        logger.warning(
            "[portal-delivery] элемент %s неожиданной формы — пропущен, файл не тронут",
            path.name,
        )
        return None
    return data


# ---------------------------------------------------------------------------
# Ключи
# ---------------------------------------------------------------------------


def delivery_key(order_id: Any, version: Any) -> str:
    """Ключ идемпотентности доставки: наряд + версия результата.

    Ровно он едет в портал (`delivery_key` тела) и ровно его портал обязан
    вернуть в ACK. Наряд без версии — версия 1: «протокол собран впервые».
    """
    try:
        v = int(version)
    except (TypeError, ValueError):
        v = 1
    return "%s#v%d" % (str(order_id or "").strip(), max(1, v))


def _item_filename(key: str) -> str:
    """Имя файла элемента: читаемый огрызок ключа + хвост хеша.

    Хвост обязателен: `portal:a-b` и `portal:a:b` после чистки дают одно и то же
    имя, и без хеша два разных результата затирали бы друг друга.
    """
    slug = _SAFE_RE.sub("-", key).strip("-._")[:80] or "item"
    tail = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    return "%s.%s.json" % (slug, tail)


def item_path(key: str) -> Path:
    return items_dir() / _item_filename(key)


def _hold_filename(session_uid: str) -> str:
    slug = _SAFE_RE.sub("-", str(session_uid or "")).strip("-._")[:80] or "session"
    tail = hashlib.sha256(str(session_uid or "").encode("utf-8")).hexdigest()[:10]
    return "%s.%s.json" % (slug, tail)


def hold_path(session_uid: str) -> Path:
    return holds_dir() / _hold_filename(session_uid)


# ---------------------------------------------------------------------------
# Настройки попыток
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("[portal-delivery] %s=%r не число — беру дефолт %d", name, raw, default)
        return default
    return value if value >= minimum else default


def max_attempts() -> int:
    return _env_int("MEETING_NOTARY_PORTAL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)


def backoff_seconds(attempts: int) -> int:
    """Пауза перед попыткой номер `attempts+1`. Экспонента с потолком.

    Потолок нужен, чтобы после подъёма портала результат не ждал часами:
    экспонента без крышки на десятой попытке даёт сутки, а требование —
    «доехал после подъёма без ручных действий».
    """
    base = _env_int("MEETING_NOTARY_PORTAL_BACKOFF_BASE_S", DEFAULT_BACKOFF_BASE_S)
    cap = _env_int("MEETING_NOTARY_PORTAL_BACKOFF_CAP_S", DEFAULT_BACKOFF_CAP_S)
    n = max(0, int(attempts))
    if n >= 20:  # 2**20 секунд — защита от переполнения на мусорном счётчике
        return cap
    return min(cap, base * (2 ** n))


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Удержание исходников: «ничего не удаляется до ACK»
# ---------------------------------------------------------------------------


def hold(
    session_uid: str,
    *,
    order_id: Optional[str] = None,
    reason: str = "portal-ack-pending",
    now: Optional[datetime] = None,
) -> bool:
    """Поставить удержание на исходники встречи. Идемпотентно.

    Ставится РАНЬШЕ доставки — в момент, когда стало известно, что встреча
    принадлежит наряду портала. Порядок именно такой, потому что первым в
    очереди на удаление стоит не портал, а сам конвейер: `finalize` сносит
    копии из `_failed/` сразу после успешного разбора, а `collector` — WAV
    сразу после Telegram. Поставь удержание после доставки — и удалять будет
    уже нечего.

    Возвращает True, если удержание появилось (или уже стояло). False — только
    если записать не удалось; вызывающий обязан считать это отказом и НЕ удалять
    ничего (гейт `is_held` в этом случае и так ответит «держим»).
    """
    if not is_enabled():
        return False
    sid = str(session_uid or "").strip()
    if not sid:
        return False
    path = hold_path(sid)
    existing = _read_json(path) if path.exists() else None
    if isinstance(existing, dict) and existing.get("released_at") is None:
        return True
    record = {
        "schema": SCHEMA_VERSION,
        "key": sid,
        "session_uid": sid,
        "order_id": order_id,
        "reason": reason,
        "created_at": _iso(_now(now)),
        "released_at": None,
    }
    try:
        _atomic_write_json(path, record)
    except OSError as e:
        # Диск полон / права. Молчать нельзя: без удержания следующий тик снесёт
        # WAV, а результат ещё не у портала.
        logger.error(
            "[portal-delivery] НЕ смог поставить удержание исходников сессии %s (%s) — "
            "исходники остаются на диске (гейт удаления fail-closed)",
            sid, type(e).__name__,
        )
        return False
    logger.info("[portal-delivery] удержание исходников: сессия %s (наряд %s)", sid, order_id)
    return True


def hold_if_portal(
    session_uid: str,
    *,
    room_url: Any = None,
    started_at: Any = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Удержать исходники, ЕСЛИ запись принадлежит наряду портала.

    Одна дверь для двух вызывающих — `collector` (перед финализацией) и
    `finalize` (перед сносом копий из `_failed/`). Разными путями к одному
    решению они бы неминуемо разъехались, а разъезд здесь стоит записи
    разговора.

    Возвращает наряд или None. 🔴 None — обычный исход (личная серия владельца),
    не ошибка: ни лога-предупреждения, ни алерта.
    """
    if not is_enabled():
        return None
    sid = str(session_uid or "").strip()
    if not sid:
        return None
    try:
        from notary.lib import orders as orders_lib  # noqa: PLC0415

        order = orders_lib.find_order_for_recording(
            room_url=room_url, started_at=started_at, session_uid=sid,
        )
    except Exception as e:  # noqa: BLE001 — реестр не читается → работаем как без нарядов
        logger.warning("[portal-delivery] наряд для записи не резолвится: %s", type(e).__name__)
        return None
    if order is None:
        return None
    hold(sid, order_id=str(order.get("id") or ""), now=now)
    return order


def release_hold(session_uid: str, *, now: Optional[datetime] = None) -> bool:
    """Снять удержание — ТОЛЬКО после подтверждения портала.

    Запись не удаляем, а помечаем снятой: след «когда и почему разрешили
    удалять» нужен ровно тогда, когда исходников уже нет и спросить не у кого.
    """
    sid = str(session_uid or "").strip()
    if not sid:
        return False
    path = hold_path(sid)
    record = _read_json(path) if path.exists() else None
    if not isinstance(record, dict):
        return False
    if record.get("released_at") is not None:
        return True
    record["released_at"] = _iso(_now(now))
    try:
        _atomic_write_json(path, record)
    except OSError as e:
        logger.warning(
            "[portal-delivery] не смог снять удержание сессии %s (%s) — "
            "исходники просто останутся на диске", sid, type(e).__name__,
        )
        return False
    logger.info("[portal-delivery] удержание снято (портал подтвердил): сессия %s", sid)
    return True


def is_held(session_uid: str) -> bool:
    """Держим ли исходники этой встречи. 🔴 FAIL-CLOSED.

    Любая неопределённость — «держим»: сбой чтения, чужой формат, нечитаемый
    каталог. Удаление необратимо, и «наверное, можно» здесь недопустимая цена.

    При `ENABLE_PORTAL_ORDERS=OFF` — всегда False, и это НЕ дыра: при OFF
    удержания не ставятся вовсе, а старое поведение владельца обязано остаться
    буква в букву.
    """
    if not is_enabled():
        return False
    sid = str(session_uid or "").strip()
    if not sid:
        return False
    path = hold_path(sid)
    try:
        if not path.exists():
            return False
    except OSError:
        return True  # каталог нечитаем — не рискуем
    record = _read_json(path)
    if not isinstance(record, dict):
        # Файл есть, но разобрать нечем: значит удержание СТАВИЛИ, а прочитать не
        # вышло. Единственный безопасный ответ — «держим».
        logger.warning(
            "[portal-delivery] удержание сессии %s нечитаемо — считаю, что держим", sid,
        )
        return True
    return record.get("released_at") is None


def held_sessions() -> List[str]:
    """Сессии под удержанием (диагностика/CLI). Пусто при OFF."""
    if not is_enabled():
        return []
    out: List[str] = []
    try:
        entries = sorted(holds_dir().glob("*.json"))
    except OSError:
        return []
    for path in entries:
        record = _read_json(path)
        if isinstance(record, dict) and record.get("released_at") is None:
            out.append(str(record.get("session_uid") or ""))
    return [s for s in out if s]


_CHUNK_RE = re.compile(r"\.chunk\d+\.wav$", re.IGNORECASE)


def session_wav_files(wav_path: Any) -> List[Path]:
    """ВСЕ куски записи этой встречи, а не только тот, что записан в `meta`.

    🔴 Запись бота нарезана на куски: `<дата>-<сессия>.chunk1.wav`,
    `.chunk2.wav`, … (`recording.ts:308`), а в `meta.files.wav` лежит только
    ПЕРВЫЙ кусок С РЕЧЬЮ. Метельщик же ходит по `*.wav` и судит каждый кусок
    отдельно — пометь один, и остальные он снесёт, оставив «удержанную» встречу
    без середины разговора.
    """
    try:
        wav = Path(str(wav_path))
    except (OSError, ValueError):
        return []
    stem = _CHUNK_RE.sub("", wav.name)
    if stem == wav.name:  # не кусок — одиночный файл
        return [wav]
    try:
        found = sorted(wav.parent.glob(stem + ".chunk*.wav"))
    except OSError:
        found = []
    return found or [wav]


def sync_wav_marker(wav_path: Any, session_uid: str) -> Optional[Path]:
    """Маркеры удержания РЯДОМ С ЗАПИСЬЮ — для метельщика из systemd.

    Метельщик (`meeting-notary-cleanup.service`) — это bash, он ходит по
    `_tmp/transcripts/*.wav` и сносит группу `<sid>.*` старше 14 дней. Ни очередь,
    ни JSON-удержание он прочитать не может, поэтому договорённость с ним ровно
    одна: **есть файл `<кусок>.ack-hold` — этот кусок не трогается**.

    Возвращает путь первого маркера, если удержание стоит; None — если снято.
    """
    if not wav_path:
        return None
    files = session_wav_files(wav_path)
    if not files:
        return None
    held = is_held(session_uid)
    first: Optional[Path] = None
    for wav in files:
        marker = wav.with_name(wav.name + HOLD_MARKER_SUFFIX)
        if held:
            try:
                if not marker.exists():
                    marker.write_text("%s\n" % session_uid, encoding="utf-8")
                    os.chmod(marker, 0o600)
                first = first or marker
            except OSError as e:
                logger.warning(
                    "[portal-delivery] маркер удержания рядом с записью не создан (%s) — "
                    "метельщик его не увидит", type(e).__name__,
                )
        else:
            try:
                if marker.exists():
                    marker.unlink()
            except OSError:
                pass
    return first


# ---------------------------------------------------------------------------
# Очередь доставки
# ---------------------------------------------------------------------------


def enqueue(
    *,
    order_id: str,
    meeting_id: Optional[str],
    version: int = 1,
    session_uid: str,
    company: Optional[str] = None,
    payload: Dict[str, Any],
    sources: Optional[Dict[str, Any]] = None,
    key: Optional[str] = None,
    ack_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[str, bool]:
    """Положить результат в очередь. Возвращает (ключ, создан ли новый элемент).

    🔴 ИДЕМПОТЕНТНО. Повторный вызов с тем же нарядом и версией НЕ создаёт второй
    элемент, НЕ обнуляет счётчик попыток и НЕ воскрешает уже доставленный
    результат. Иначе тик collector'а, встретив ту же встречу второй раз (а он
    встречает — цикл раз в 5 минут), слал бы портал дубль за дублем.

    Бросает `PortalDeliveryError`, если записать физически не удалось (диск,
    права). Вызывающий обязан НЕ удалять исходники и сказать владельцу.
    """
    if not is_enabled():
        return "", False
    key = key or delivery_key(order_id, version)
    path = item_path(key)
    existing = _read_json(path) if path.exists() else None
    if isinstance(existing, dict):
        return key, False
    ts = _iso(_now(now))
    item = {
        "schema": SCHEMA_VERSION,
        "key": key,
        # Чем портал подтвердит ЭТО сообщение. Обычно тот же ключ; у отчёта об
        # уборке — ключ исходной доставки (портал эхом отдаёт именно его).
        "ack_key": ack_key or key,
        "order_id": order_id,
        "meeting_id": meeting_id,
        "version": int(version),
        "session_uid": session_uid,
        "company": company,
        "status": STATUS_PENDING,
        "attempts": 0,
        "created_at": ts,
        "updated_at": ts,
        "next_attempt_at": ts,
        "last_error": None,
        "last_http_status": None,
        "delivered_at": None,
        "ack": None,
        # Что удерживаем до ACK — чтобы «кто держит этот WAV» отвечалось файлом,
        # а не раскопками по логам.
        "sources": sources or {},
        "payload": payload,
    }
    try:
        _atomic_write_json(path, item)
    except OSError as e:
        raise PortalDeliveryError(
            "результат встречи не записан в очередь доставки (%s): %s"
            % (type(e).__name__, path)
        ) from e
    logger.info(
        "[portal-delivery] в очередь: %s (наряд %s, сессия %s)", key, order_id, session_uid,
    )
    return key, True


def load_item(key: str) -> Optional[Dict[str, Any]]:
    if not is_enabled():
        return None
    return _read_json(item_path(key))


def list_items(statuses: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Элементы очереди. Fail-open: нечитаемый каталог → пусто + WARNING."""
    if not is_enabled():
        return []
    try:
        entries = sorted(items_dir().glob("*.json"))
    except OSError as e:
        logger.warning(
            "[portal-delivery] каталог очереди не прочитан (%s) — в этот тик доставки нет",
            type(e).__name__,
        )
        return []
    out: List[Dict[str, Any]] = []
    for path in entries:
        item = _read_json(path)
        if item is None:
            continue
        if statuses is not None and item.get("status") not in statuses:
            continue
        out.append(item)
    return out


def due_items(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Что пора отправлять прямо сейчас, в порядке созревания."""
    moment = _now(now)
    ready: List[Tuple[datetime, Dict[str, Any]]] = []
    for item in list_items(statuses={STATUS_PENDING}):
        due = _parse_iso(item.get("next_attempt_at")) or moment
        if due <= moment:
            ready.append((due, item))
    ready.sort(key=lambda pair: (pair[0], str(pair[1].get("key"))))
    return [item for _, item in ready]


def _save(item: Dict[str, Any]) -> bool:
    try:
        _atomic_write_json(item_path(str(item.get("key"))), item)
        return True
    except OSError as e:
        logger.error(
            "[portal-delivery] состояние элемента %s не сохранено (%s) — "
            "попытка повторится, исходники остаются на месте",
            item.get("key"), type(e).__name__,
        )
        return False


def record_delivered(
    key: str, ack: Dict[str, Any], *, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Портал подтвердил приём. Снимает удержание исходников.

    Порядок важен: сначала помечаем элемент доставленным, потом снимаем
    удержание. Обратный порядок дал бы окно, в котором удержания уже нет, а
    факта доставки ещё нет — и падение ровно в это окно разрешило бы удалить
    непереданный результат.
    """
    item = load_item(key)
    if item is None:
        return None
    if item.get("status") == STATUS_DELIVERED:
        return item
    ts = _iso(_now(now))
    item["status"] = STATUS_DELIVERED
    item["delivered_at"] = ts
    item["updated_at"] = ts
    item["ack"] = ack
    item["last_error"] = None
    if not _save(item):
        return item
    session_uid = str(item.get("session_uid") or "")
    if session_uid and not _session_has_pending(session_uid, exclude_key=key):
        release_hold(session_uid, now=now)
    logger.info("[portal-delivery] портал подтвердил приём: %s", key)
    return item


def _session_has_pending(session_uid: str, *, exclude_key: str = "") -> bool:
    """Есть ли по этой сессии ещё неподтверждённые доставки.

    Удержание снимается не по элементу, а по СЕССИИ: у одной встречи может быть
    несколько версий результата (перевыпуск протокола), и снять удержание по
    первой из них означало бы удалить исходники под ногами у второй.
    """
    for item in list_items(statuses={STATUS_PENDING, STATUS_DEAD}):
        if str(item.get("session_uid") or "") != session_uid:
            continue
        if str(item.get("key") or "") == exclude_key:
            continue
        return True
    return False


def record_failure(
    key: str,
    *,
    error: str,
    http_status: Optional[int] = None,
    permanent: bool = False,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Попытка не удалась: счётчик, пауза, а на исчерпании — алерт владельцу.

    🔴 «Умер» НЕ значит «можно удалять». Элемент со статусом `dead` продолжает
    держать исходники: результат встречи не подтверждён порталом, и стереть
    запись живого разговора потому, что мы устали повторять, — ровно тот тихий
    провал, ради которого фаза существует. Разрешить удаление может только
    человек, командой снятия.

    `error` — КЛАСС ошибки (имя исключения, код ответа). Тела ответов и текстов
    протокола здесь быть не должно: строка уезжает в журнал.
    """
    item = load_item(key)
    if item is None:
        return None
    if item.get("status") == STATUS_DELIVERED:
        return item
    ts = _now(now)
    attempts = int(item.get("attempts") or 0) + 1
    item["attempts"] = attempts
    item["updated_at"] = _iso(ts)
    item["last_error"] = str(error)[:200]
    item["last_http_status"] = http_status
    cap = max_attempts()
    exhausted = permanent or attempts >= cap
    if exhausted:
        item["status"] = STATUS_DEAD
        item["next_attempt_at"] = None
    else:
        item["next_attempt_at"] = _iso(ts + timedelta(seconds=backoff_seconds(attempts)))
    _save(item)
    if exhausted:
        _alert_exhausted(item, permanent=permanent)
    else:
        logger.warning(
            "[portal-delivery] попытка %d/%d по %s не прошла (%s, http=%s) — повтор в %s",
            attempts, cap, key, item["last_error"], http_status, item["next_attempt_at"],
        )
    return item


def _alert_exhausted(item: Dict[str, Any], *, permanent: bool) -> None:
    """Алерт владельцу. Тихий провал здесь неотличим от «встречи не было».

    В сообщении — только «что» и «где смотреть»: ни протокола, ни реплик, ни
    имён участников. Ключ глушилки — по элементу, чтобы шум по одной встрече не
    съел сообщение по другой.
    """
    key = str(item.get("key") or "?")
    reason = "портал отверг результат" if permanent else "портал не ответил"
    if key.endswith(PURGE_SUFFIX):
        # Отчёт об уборке — служебное сообщение: САМ результат у портала уже
        # есть, потерялась только строка на панели «Запись». Будить владельца
        # ночью ради неё — ложная тревога, и она обесценивает настоящую.
        logger.warning(
            "[portal-delivery] отчёт об уборке %s не доехал (%s) — результат у портала есть",
            key, reason,
        )
        return
    if key.endswith(_NO_SHOW_SUFFIX):
        # «Бот не пришёл» (фаза 5): записи нет вовсе, удерживать нечего — текст
        # про «запись не удалил» здесь был бы враньём. Но и молчать нельзя: у
        # организатора без привязанного Телеграма колокольчик портала — единственный
        # способ узнать, что бота не было.
        try:
            from notary.lib import notify  # noqa: PLC0415

            notify.push(
                "⚠️ Портал не принял событие «бот не пришёл» (%s, попыток %s, последняя "
                "ошибка: %s). Колокольчика у организатора не будет — предупреди его сам. "
                "Наряд: %s." % (reason, item.get("attempts"), item.get("last_error"),
                                item.get("order_id")),
                dedupe_key="portal-delivery-dead:%s" % key,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[portal-delivery] алерт владельцу не ушёл: %s", type(e).__name__)
        logger.error("[portal-delivery] «бот не пришёл» %s не доехал (%s)", key, reason)
        return
    try:
        from notary.lib import notify  # noqa: PLC0415

        notify.push(
            "🔴 Результат встречи НЕ доехал до портала: %s (попыток %s, последняя ошибка: %s). "
            "Запись и рабочий транскрипт я НЕ удалил — они на месте, доставку можно повторить "
            "командой. Наряд: %s."
            % (reason, item.get("attempts"), item.get("last_error"), item.get("order_id")),
            dedupe_key="portal-delivery-dead:%s" % key,
        )
    except Exception as e:  # noqa: BLE001 — алерт не имеет права ронять конвейер
        logger.warning("[portal-delivery] алерт владельцу не ушёл: %s", type(e).__name__)
    logger.error(
        "[portal-delivery] 🔴 доставка %s исчерпана (%s, попыток %s) — исходники УДЕРЖАНЫ",
        key, reason, item.get("attempts"),
    )


def revive(key: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Вернуть «умерший» элемент в строй (владелец починил портал/токен)."""
    item = load_item(key)
    if item is None or item.get("status") != STATUS_DEAD:
        return item
    ts = _iso(_now(now))
    item["status"] = STATUS_PENDING
    item["attempts"] = 0
    item["next_attempt_at"] = ts
    item["updated_at"] = ts
    _save(item)
    logger.info("[portal-delivery] элемент %s возвращён в очередь вручную", key)
    return item


def abandon(key: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Осознанный отказ от доставки: снимает удержание исходников.

    Единственная дверь, через которую исходники становятся удаляемыми БЕЗ ACK, и
    она ручная by design — автоматического «сдаюсь» в этой фазе нет.
    """
    item = load_item(key)
    if item is None:
        return None
    ts = _iso(_now(now))
    item["status"] = STATUS_DEAD
    item["abandoned_at"] = ts
    item["updated_at"] = ts
    item["next_attempt_at"] = None
    _save(item)
    session_uid = str(item.get("session_uid") or "")
    if session_uid and not _session_has_pending(session_uid, exclude_key=key):
        release_hold(session_uid, now=now)
    logger.warning(
        "[portal-delivery] доставка %s снята вручную — удержание исходников отпущено", key,
    )
    return item


PURGE_SUFFIX = "#purged"
#: Хвост ключа «бот не пришёл» — копия `portal_ingest.NO_SHOW_SUFFIX` (тот модуль
#: импортирует этот, обратный импорт на уровне модуля дал бы цикл).
_NO_SHOW_SUFFIX = "#no_show"


def delivered_key_for_session(session_uid: str) -> Optional[str]:
    """Ключ ПОДТВЕРЖДЁННОЙ доставки этой встречи (самой свежей версии).

    Нужен отчёту об уборке: портал принимает его только с ключом, который сам же
    и выдал в ACK, — так он отличает отчёт по этой встрече от отчёта по соседней.
    """
    best: Optional[Tuple[str, str]] = None
    for item in list_items(statuses={STATUS_DELIVERED}):
        if str(item.get("session_uid") or "") != str(session_uid or ""):
            continue
        if str(item.get("key") or "").endswith((PURGE_SUFFIX, _NO_SHOW_SUFFIX)):
            # Служебные события — не доставка результата: их ключ портал не
            # примет как «ключ исходной доставки» для отчёта об уборке.
            continue
        stamp = str(item.get("delivered_at") or "")
        if best is None or stamp > best[0]:
            best = (stamp, str(item.get("key")))
    return best[1] if best else None


def report_sources_purged(
    session_uid: str, *, now: Optional[datetime] = None,
) -> Optional[str]:
    """Сказать порталу, что исходники этой встречи физически удалены.

    🔴 ACK доказывает, что ПОРТАЛ ПРИНЯЛ результат, и ничего не говорит о том,
    что бот стёр WAV. Панель «Запись» различает эти состояния словами и без
    отчёта навсегда застревает на «ждём отчёта бота» (R7.9, R5.12).

    Отчёт кладётся В ТУ ЖЕ ОЧЕРЕДЬ, а не шлётся напрямую: портал может лежать и
    в этот момент тоже, а состояние «удалено» без отчёта не восстановить —
    исходников уже нет.
    """
    if not is_enabled():
        return None
    base_key = delivered_key_for_session(session_uid)
    if not base_key:
        # Ничего подтверждённого по этой встрече не было — отчитываться не о чем
        # (и предъявить порталу нечего: ключ он выдаёт только в ACK).
        return None
    source = load_item(base_key) or {}
    try:
        from notary.lib import portal_ingest  # noqa: PLC0415

        payload = portal_ingest.build_purge_event(
            meeting_id=str(source.get("meeting_id") or ""),
            delivery_key=base_key,
            order_id=source.get("order_id"),
            company=source.get("company"),
            occurred_at=_iso(_now(now)),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[portal-delivery] отчёт об уборке не собран: %s", type(e).__name__)
        return None
    try:
        key, _ = enqueue(
            order_id=str(source.get("order_id") or ""),
            meeting_id=source.get("meeting_id"),
            version=int(source.get("version") or 1),
            session_uid=str(session_uid),
            company=source.get("company"),
            payload=payload,
            key=base_key + PURGE_SUFFIX,
            ack_key=base_key,
            now=now,
        )
    except PortalDeliveryError as e:
        # Не трагедия: результат уже у портала, потеряется только строка на
        # панели «Запись». Алерт владельцу тут был бы шумом.
        logger.warning("[portal-delivery] отчёт об уборке не записан: %s", e)
        return None
    return key


def report_no_show(
    order_id: str,
    *,
    meeting_id: Optional[str],
    session_uid: str,
    company: Optional[str] = None,
    round_mark: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Сказать порталу «бот не пришёл» (фаза 5 ISS-60). Возвращает ключ элемента.

    Зовёт запускатель, когда сторож входа СДАЛСЯ окончательно (после повтора).
    Кладётся в ТУ ЖЕ очередь, что результаты: портал может лежать в эту минуту, а
    разбирает очередь сборщик каждые 5 минут. Идемпотентно по ЗАХОДУ бота
    (`round_mark` — минута его старта): второй «не пришёл» того же захода не
    встаёт, а срыв следующего захода по той же встрече — встаёт (см.
    `portal_ingest.no_show_key`).

    Исходников у этой встречи нет, поэтому удержание не ставится и не снимается.
    Без `meeting_id` (портал ищет встречу по нему) не шлём вовсе — 422 навсегда.
    """
    if not is_enabled():
        return None
    order = str(order_id or "").strip()
    mid = str(meeting_id or "").strip()
    if not order or not mid:
        logger.warning(
            "[portal-delivery] «бот не пришёл» не собран: нет наряда или id встречи (%s)",
            order or "?",
        )
        return None
    try:
        from notary.lib import portal_ingest  # noqa: PLC0415

        payload = portal_ingest.build_no_show_event(
            meeting_id=mid, order_id=order, company=company, occurred_at=_iso(_now(now)),
            round_mark=round_mark,
        )
        key = portal_ingest.no_show_key(order, round_mark)
    except Exception as e:  # noqa: BLE001
        logger.warning("[portal-delivery] «бот не пришёл» не собран: %s", type(e).__name__)
        return None
    key, created = enqueue(
        order_id=order,
        meeting_id=mid,
        version=1,
        session_uid=str(session_uid or order),
        company=company,
        payload=payload,
        key=key,
        ack_key=key,
        now=now,
    )
    if created:
        logger.info("[portal-delivery] «бот не пришёл» в очереди: %s", key)
    return key


# ---------------------------------------------------------------------------
# Сводка (CLI, глубокий health, диагностика)
# ---------------------------------------------------------------------------


def summary() -> Dict[str, Any]:
    """Счётчики очереди. Ни одного текста — только числа и ключи."""
    if not is_enabled():
        return {"enabled": False, "pending": 0, "dead": 0, "delivered": 0, "held": 0}
    items = list_items()
    by_status: Dict[str, int] = {STATUS_PENDING: 0, STATUS_DELIVERED: 0, STATUS_DEAD: 0}
    oldest_pending: Optional[str] = None
    for item in items:
        status = str(item.get("status") or "")
        by_status[status] = by_status.get(status, 0) + 1
        if status == STATUS_PENDING:
            created = str(item.get("created_at") or "")
            if oldest_pending is None or created < oldest_pending:
                oldest_pending = created
    return {
        "enabled": True,
        "pending": by_status.get(STATUS_PENDING, 0),
        "delivered": by_status.get(STATUS_DELIVERED, 0),
        "dead": by_status.get(STATUS_DEAD, 0),
        "held": len(held_sessions()),
        "oldest_pending_at": oldest_pending,
    }
