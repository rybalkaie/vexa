"""Реестр НАРЯДОВ портала — `.orders.json` рядом с реестром серий (Ф9 шлюза).

Наряд — это «портал попросил записать встречу»: что, когда, в какой комнате,
какой КОМПАНИИ и под каким человеческим именем серии. Реестр серий
(`watched.yaml`) остаётся за личными сериями владельца; порталовые встречи
живут ЗДЕСЬ и `watched.yaml` не трогают (решение №2 плана: две правды = двойной
заход бота).

Инварианты, каждый из которых куплен инцидентом:

* 🔴 **Пути резолвятся ЛЕНИВО, на вызове** (`orders_file()`), а не на импорте.
  Ровно этот класс дефекта в `lib/state.py` (приёмка 05.08.2026) заставлял тесты
  писать в БОЕВОЙ `.state.json`, и записи на 12 часов блокировали настоящие
  встречи. Сторож — `tests/test_gateway_state_isolation.py`.
* 🔴 **Пишем только из-под непривилегированного пользователя** (`dev`). Правка
  реестра из-под `root` уже роняла бота 12.08.2026: файл менял владельца, и
  следующая запись из-под `dev` падала на правах. `_mutate` отказывается писать
  при euid == 0 — молча «починить» это нельзя, только запускать юнит с `User=dev`.
* 🔴 **Компания — обязательное поле.** Наряд без компании ОТВЕРГАЕТСЯ, а не
  «считается anzhee по умолчанию»: от компании зависит, в какой чат уйдёт
  протокол и какое знание компании подмешается в сборку.
* 🔴 **Человеческое имя серии — обязательное поле.** Сегодня в шапке PDF имя
  печатается транслитом slug'а («Koordinatsiya»); порталовые серии приехали бы
  туда же, если имя не передать.
* Запись атомарная (tmp + `os.replace`) и под `flock` — одна дверь `_mutate()`.

Что этот модуль НЕ делает: не запускает ботов и не пишет `.queue.json`. Наряд
кладётся в реестр, исполнение (планировщик учитывает наряды наравне с сериями) —
фаза 10b. Второго писателя очереди в проекте быть не должно.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent.parent))  # для notary.cli.registry

from notary.cli.registry import VALID_COMPANIES  # noqa: E402
from notary.lib.meeting_identity import start_sort_key  # noqa: E402
from notary.lib.telemost_url import room_url_key, url_host_in  # noqa: E402

ORDERS_FILENAME = ".orders.json"
SCHEMA_VERSION = 1

STATUS_SCHEDULED = "scheduled"
STATUS_STOPPING = "stopping"
STATUS_CANCELLED = "cancelled"
STATUS_FINISHED = "finished"
# «Живой» наряд: планировщик обязан его видеть, а повторное «зайти сейчас» в ту
# же комнату — вернуть его, а не завести второй.
ACTIVE_STATUSES = frozenset({STATUS_SCHEDULED, STATUS_STOPPING})
VALID_STATUSES = frozenset(
    {STATUS_SCHEDULED, STATUS_STOPPING, STATUS_CANCELLED, STATUS_FINISHED}
)

KIND_SCHEDULED = "scheduled"
KIND_IMMEDIATE = "immediate"

DEFAULT_DURATION_MINUTES = 60
MAX_DURATION_MINUTES = 24 * 60
# Насколько «в прошлое» терпим `start_at`. Часы портала и VPS расходятся на
# секунды, а сеть добавляет задержку — ноль был бы гарантией ложных отказов.
START_PAST_GRACE_MINUTES = 2
# Дальше горизонта наряд принимать бессмысленно: планировщик смотрит на 48 часов,
# а годовой наряд означает опечатку в дате (2027 вместо 2026).
START_HORIZON_DAYS = 400
# Разовый наряд «зайти сейчас» держит комнату занятой не вечно: забытый наряд
# иначе навсегда блокировал бы кнопку «Подключить сейчас» по этой комнате.
IMMEDIATE_TTL_MINUTES = 180

_MEETING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SERIES_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_TITLE_LEN = 200
_MAX_PARTICIPANTS = 100
_MAX_PARTICIPANT_LEN = 200

DEFAULT_ROOM_HOSTS = ("telemost.yandex.ru",)

# 🔴 У ПОРТАЛА И БОТА РАЗНЫЕ СЛОВА ДЛЯ ОДНОЙ КОМПАНИИ. У бота канон — `mpfirst`
# (`notary.cli.registry.VALID_COMPANIES`), у портала код компании это `slug`
# организации, и там она заведена как `m1` (`prisma/seed-organization.ts:1204`).
# Без перевода наряд МПервого отбивался бы как «неизвестная компания» — то есть
# половина компаний просто не смогла бы позвать бота, и выяснилось бы это на
# первой же встрече.
#
# Перевод живёт ЗДЕСЬ, на границе, а не в портале: словарь бота этим не
# трогается, а любой будущий клиент шлюза получает то же поведение бесплатно.
# В реестр всегда ложится КАНОН бота — ниже по течению (доставка протокола,
# подмешивание знания компании) синонимов быть не должно.
COMPANY_ALIASES = {
    "m1": "mpfirst",
    "мпервый": "mpfirst",
}


def canonical_company(raw: str) -> str:
    """Слово портала → канон бота. Незнакомое возвращаем как есть (отобьёт валидатор)."""
    value = (raw or "").strip().lower()
    return COMPANY_ALIASES.get(value, value)


class OrderRegistryError(RuntimeError):
    """Реестр физически не может принять запись (права, диск, root)."""


class OrderConflict(RuntimeError):
    """Оптимистичная блокировка: наряд изменился под руками (версия разошлась)."""

    def __init__(self, message: str, *, current_version: int) -> None:
        super().__init__(message)
        self.current_version = current_version


# ---------- пути (ЛЕНИВО, см. шапку) ----------


def _registry_dir() -> Path:
    from notary.cli.registry import _resolve_registry_dir

    return _resolve_registry_dir()


def orders_file() -> Path:
    """Путь к реестру нарядов НА МОМЕНТ ВЫЗОВА."""
    return _registry_dir() / ORDERS_FILENAME


def _effective_uid() -> int:
    """Отдельной функцией — это шов для теста «отказ писать из-под root»."""
    return os.geteuid()


def allowed_room_hosts() -> Tuple[str, ...]:
    """Хосты, ссылки на которые бот умеет открывать.

    Сегодня бот ходит только в Яндекс.Телемост (`platform: yandex_telemost` в
    `runner._launch_bot`), поэтому ссылка на Zoom/Meet — это не «строгий шлюз», а
    честный отказ вместо бота, который приедет в пустоту. Расширяется env'ом,
    когда у бота появится вторая платформа.
    """
    raw = os.environ.get("MEETING_NOTARY_GATEWAY_ROOM_HOSTS")
    if not raw or not raw.strip():
        return DEFAULT_ROOM_HOSTS
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


# ---------- ввод-вывод ----------


def _empty_state() -> Dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "orders": {}}


def _read_raw() -> Dict[str, Any]:
    path = orders_file()
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Битый файл НЕ «чиним» молча в пустой: писать поверх — значит потерять
        # все живые наряды. Отдаём наверх, шлюз ответит 503 и владелец увидит.
        raise OrderRegistryError(
            "реестр нарядов %s нечитаем (битый JSON или нет доступа) — "
            "чинить руками, перезапись стёрла бы живые наряды" % path
        )
    if not isinstance(data, dict) or not isinstance(data.get("orders"), dict):
        raise OrderRegistryError("реестр нарядов %s имеет неожиданную форму" % path)
    return data


def _atomic_write(state: Dict[str, Any]) -> None:
    path = orders_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        # Наряд несёт имена участников и ссылку на комнату — доступ только
        # владельцу процесса (`dev`), как у файла окружения с секретом.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _mutate(fn: Callable[[Dict[str, Any]], Any]) -> Any:
    """Единственная дверь записи: flock → чтение → изменение → атомарная запись.

    `fn` получает состояние и возвращает результат; писать состояние сама она не
    должна. Если `fn` бросила — файл остаётся нетронутым (мы ещё не писали).
    """
    if _effective_uid() == 0:
        raise OrderRegistryError(
            "запись реестра нарядов из-под root запрещена: файл сменит владельца, "
            "и следующая запись из-под dev упадёт на правах (инцидент 12.08.2026). "
            "Юнит обязан идти с User=dev"
        )
    path = orders_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / (".%s.lock" % path.name)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _read_raw()
        result = fn(state)
        _atomic_write(state)
        return result
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------- ключи ----------


def order_id_for_meeting(meeting_id: str) -> str:
    """Детерминированный id наряда по id встречи портала (решение №2 плана).

    Повторная отправка того же наряда обязана попасть в ТУ ЖЕ запись — иначе
    второй бот. Ключ идемпотентности берём от портала, своего не выдумываем.
    """
    return "portal:%s" % meeting_id


def room_key(room_url: str) -> str:
    """Ключ комнаты для идемпотентности «зайти сейчас».

    Какая это комната, решает общая формула проекта `telemost_url.room_url_key`:
    два одинаковых приглашения, различающихся utm-меткой, — одна и та же комната,
    и второго бота там быть не должно. Здесь от неё берётся хэш: в ключ наряда и в
    метку контейнера идёт он, а не сама ссылка. Для обычной ссылки хэш тот же, что
    до общей формулы, — ключи уже записанных нарядов и живых контейнеров совпадают.
    """
    canon = room_url_key(room_url) or ""
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


# ---------- валидация ----------


def _err(field: str, code: str, message: str) -> Dict[str, str]:
    return {"field": field, "code": code, "message": message}


def _parse_dt(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Время без пояса — источник классической ошибки «бот пришёл на три часа
        # раньше». Портал обязан слать смещение явно.
        return None
    return dt


def validate_order_payload(
    payload: Any,
    *,
    kind: str = KIND_SCHEDULED,
    now: Optional[datetime] = None,
    check_start_in_past: bool = True,
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Проверяет и нормализует тело наряда. Возвращает (наряд, список ошибок).

    Ошибки возвращаются СПИСКОМ (а не первой попавшейся): портал показывает их
    человеку в форме, и «по одной за запрос» — это три круга на одной встрече.

    `check_start_in_past=False` — для правки наряда, где время НЕ меняют: у
    прошедшей встречи `start_at` уже в прошлом, и запрет «времени в прошлом» не
    имеет права мешать поменять, скажем, имя серии задним числом.
    """
    now = now or datetime.now(timezone.utc)
    errors: List[Dict[str, str]] = []
    out: Dict[str, Any] = {}

    if not isinstance(payload, dict):
        return {}, [_err("", "not_an_object", "тело запроса должно быть объектом JSON")]

    # --- id встречи портала ---
    meeting_id = payload.get("meeting_id")
    if not isinstance(meeting_id, str) or not _MEETING_ID_RE.match(meeting_id.strip()):
        errors.append(
            _err(
                "meeting_id",
                "invalid",
                "нужен идентификатор встречи портала (латиница/цифры/._:- до 128 знаков)",
            )
        )
    else:
        out["meeting_id"] = meeting_id.strip()

    # --- компания (ОБЯЗАТЕЛЬНО, дефолта нет by design) ---
    company = payload.get("company")
    if company is None or (isinstance(company, str) and not company.strip()):
        errors.append(
            _err(
                "company",
                "required",
                "компания обязательна: от неё зависит, в какой чат уйдёт протокол. "
                "Допустимо: %s" % ", ".join(sorted(VALID_COMPANIES)),
            )
        )
    elif not isinstance(company, str) or canonical_company(company) not in VALID_COMPANIES:
        errors.append(
            _err(
                "company",
                "unknown",
                "неизвестная компания %r, допустимо: %s (портальное %s тоже принимается)"
                % (
                    company,
                    ", ".join(sorted(VALID_COMPANIES)),
                    ", ".join(sorted(COMPANY_ALIASES)),
                ),
            )
        )
    else:
        # В реестр ложится КАНОН бота, а не то слово, которым назвал портал.
        out["company"] = canonical_company(company)

    # --- человеческое имя серии (ОБЯЗАТЕЛЬНО) ---
    title = payload.get("series_title")
    if not isinstance(title, str) or not title.strip():
        errors.append(
            _err(
                "series_title",
                "required",
                "человеческое имя серии обязательно — иначе в шапке PDF встанет "
                "транслит slug'а («Koordinatsiya»)",
            )
        )
    elif len(title.strip()) > _MAX_TITLE_LEN:
        errors.append(
            _err("series_title", "too_long", "имя серии длиннее %d знаков" % _MAX_TITLE_LEN)
        )
    else:
        out["series_title"] = title.strip()

    # --- slug серии (папка на диске; выводим из meeting_id, если не дали) ---
    series = payload.get("series")
    if series is None or (isinstance(series, str) and not series.strip()):
        if out.get("meeting_id"):
            out["series"] = _slug_from_meeting_id(out["meeting_id"])
    elif not isinstance(series, str) or not _SERIES_SLUG_RE.match(series.strip()):
        errors.append(
            _err("series", "invalid", "slug серии — kebab-case латиницей (a-z0-9_-)")
        )
    else:
        out["series"] = series.strip()

    # --- ссылка на комнату ---
    room_url = payload.get("room_url")
    if not isinstance(room_url, str) or not room_url.strip():
        errors.append(_err("room_url", "required", "ссылка на комнату обязательна"))
    else:
        raw_url = room_url.strip()
        try:
            parts = urlsplit(raw_url)
            host = (parts.hostname or "").lower()
            scheme = parts.scheme
        except ValueError:  # «https://[::1» и подобное — urlsplit не разбирает
            host, scheme = "", ""
        if scheme != "https" or not host:
            errors.append(
                _err("room_url", "not_https", "ссылка на комнату должна быть https://…")
            )
        elif host not in allowed_room_hosts():
            errors.append(
                _err(
                    "room_url",
                    "unsupported_host",
                    "бот умеет заходить только в %s; ссылка на %s означала бы "
                    "бота, приехавшего в пустоту"
                    % (", ".join(allowed_room_hosts()), host),
                )
            )
        elif not url_host_in(raw_url, allowed_room_hosts()):
            # Хвост С10 (фаза 3б): Python и браузер поняли бы адрес по-разному —
            # `https://evil.example\@telemost.yandex.ru/…` для urlsplit ведёт в
            # Телемост, а браузер читает `\` как `/` и открыл бы evil.example. Так же
            # логин перед адресом, чужой порт, пробелы и переводы строки внутри.
            errors.append(
                _err(
                    "room_url",
                    "invalid",
                    "ссылка записана так, что браузер понял бы её иначе (обратная косая "
                    "черта, логин перед адресом, чужой порт, пробелы) — скопируйте "
                    "ссылку из Телемоста заново",
                )
            )
        else:
            out["room_url"] = raw_url

    # --- время старта ---
    if kind == KIND_IMMEDIATE:
        out["start_at"] = now.isoformat()
    else:
        raw_start = payload.get("start_at")
        if raw_start is None or (isinstance(raw_start, str) and not raw_start.strip()):
            errors.append(_err("start_at", "required", "время начала обязательно"))
        else:
            start = _parse_dt(raw_start)
            if start is None:
                errors.append(
                    _err(
                        "start_at",
                        "invalid",
                        "нужен ISO 8601 СО СМЕЩЕНИЕМ пояса (2026-08-20T10:00:00+03:00)",
                    )
                )
            elif check_start_in_past and start < now - timedelta(
                minutes=START_PAST_GRACE_MINUTES
            ):
                errors.append(
                    _err(
                        "start_at",
                        "in_past",
                        "время начала в прошлом (%s) — бот на него не придёт"
                        % start.isoformat(),
                    )
                )
            elif start > now + timedelta(days=START_HORIZON_DAYS):
                errors.append(
                    _err(
                        "start_at",
                        "too_far",
                        "время начала дальше %d дней — похоже на опечатку в дате"
                        % START_HORIZON_DAYS,
                    )
                )
            else:
                out["start_at"] = start.isoformat()

    # --- длительность ---
    duration = payload.get("duration_minutes")
    if duration is None:
        out["duration_minutes"] = DEFAULT_DURATION_MINUTES
    elif isinstance(duration, bool) or not isinstance(duration, int):
        errors.append(_err("duration_minutes", "invalid", "длительность — целое число минут"))
    elif duration < 1 or duration > MAX_DURATION_MINUTES:
        errors.append(
            _err(
                "duration_minutes",
                "out_of_range",
                "длительность от 1 до %d минут" % MAX_DURATION_MINUTES,
            )
        )
    else:
        out["duration_minutes"] = duration

    # --- участники (метаданные для маппинга имён; тексты сюда не кладутся) ---
    participants = payload.get("expected_participants")
    if participants is None:
        out["expected_participants"] = []
    elif not isinstance(participants, list):
        errors.append(
            _err("expected_participants", "invalid", "ожидается список имён участников")
        )
    elif len(participants) > _MAX_PARTICIPANTS:
        errors.append(
            _err(
                "expected_participants",
                "too_many",
                "не больше %d участников" % _MAX_PARTICIPANTS,
            )
        )
    elif any(
        not isinstance(p, str) or not p.strip() or len(p) > _MAX_PARTICIPANT_LEN
        for p in participants
    ):
        errors.append(
            _err("expected_participants", "invalid", "имена участников — непустые строки")
        )
    else:
        out["expected_participants"] = [p.strip() for p in participants]

    # --- куда слать протокол ---
    chat_id = payload.get("telegram_chat_id")
    if chat_id is not None:
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            errors.append(
                _err(
                    "telegram_chat_id",
                    "invalid",
                    "chat_id — целое число (отрицательное для группы)",
                )
            )
        else:
            out["telegram_chat_id"] = chat_id

    # --- маршрут доставки: «кому уходит протокол» (Ф10d, R7.10) ---
    #
    # Поле необязательное: не прислали — бот выбирает сам (группа, если чат
    # привязан, иначе личка владельца наряда, иначе только портал). Прислали
    # мусор — 422, а не «поняли как смогли»: «отправь в личку» и «отправь в
    # группу» отличаются кругом читателей, и угадывать здесь нечего.
    #
    # 🔴 `dm` без привязанного телеграма сотрудника и `group` без чата
    # ОТБИВАТЬ НЕЛЬЗЯ: привязка появляется на экране «Ватсон» (Ф12) уже ПОСЛЕ
    # постановки наряда, и отказ означал бы «встречу не записать, пока не
    # подключишь телеграм». Такой наряд принимается, а на доставке честно
    # деградирует в «только портал» — см. `lib/delivery_route.py`.
    delivery = payload.get("delivery")
    if delivery is not None:
        from notary.lib import delivery_route  # noqa: PLC0415

        if not isinstance(delivery, str) or delivery.strip().lower() not in (
            delivery_route.REQUESTABLE_MODES
        ):
            errors.append(
                _err(
                    "delivery",
                    "unknown",
                    "маршрут доставки — одно из: %s"
                    % ", ".join(delivery_route.REQUESTABLE_MODES),
                )
            )
        else:
            out["delivery"] = delivery.strip().lower()

    # --- владелец наряда: «ЧЕЙ это наряд» (Ф10a, R7.10) ---
    #
    # При `ENABLE_PORTAL_ORDERS=OFF` поле необязательно — ровно как приехало из
    # Ф9: выключатель обязан возвращать вчерашнее поведение целиком, включая
    # строгость валидатора. При ON — обязательно: наряд без владельца
    # маршрутизировать некуда (кому слать протокол, чьи правки принимать), а
    # «считать владельцем Илью по умолчанию» отдало бы чужие встречи ему.
    #
    # Уже лежащие на диске безвладельческие наряды Ф9 это НЕ ломает: строгость
    # стоит на ВХОДЕ (новая постановка), а чтение старых живёт в
    # `lib/identity.order_owner` — оно их читает и просто не наделяет правами.
    owner = payload.get("owner")
    owner_required = _owner_required()
    if owner is None:
        if owner_required:
            errors.append(
                _err(
                    "owner",
                    "required",
                    "владелец наряда обязателен: без него неизвестно, кому слать "
                    "протокол и чьи правки принимать",
                )
            )
    elif not isinstance(owner, dict):
        errors.append(_err("owner", "invalid", "владелец — объект"))
    else:
        clean: Dict[str, Any] = {}
        uid = owner.get("portal_user_id")
        if isinstance(uid, str) and uid.strip():
            clean["portal_user_id"] = uid.strip()[:128]
        elif owner_required:
            errors.append(
                _err(
                    "owner.portal_user_id",
                    "required",
                    "нужен идентификатор сотрудника портала — по имени наряд не "
                    "связать ни с человеком, ни с его телеграм-аккаунтом",
                )
            )
        name = owner.get("name")
        if isinstance(name, str) and name.strip():
            clean["name"] = name.strip()[:_MAX_TITLE_LEN]
        tg = owner.get("telegram_user_id")
        if isinstance(tg, int) and not isinstance(tg, bool):
            clean["telegram_user_id"] = tg
        out["owner"] = clean

    keep_audio = payload.get("keep_audio")
    out["keep_audio"] = bool(keep_audio) if keep_audio is not None else False

    return out, errors


def _owner_required() -> bool:
    """Обязателен ли владелец у НОВОГО наряда — вопрос к аварийному выключателю.

    Импорт ленивый и защищённый: `lib.identity` читает `lib.orders` обратно (для
    живых нарядов), и жёсткий импорт на уровне модуля свёл бы их в цикл. Модуль
    не доехал при выкатке → считаем OFF, то есть поведение Ф9: реестр продолжает
    принимать наряды, а не встаёт целиком.
    """
    try:
        from notary.lib import identity  # noqa: PLC0415

        return identity.is_enabled()
    except Exception:  # noqa: BLE001
        return False


def _slug_from_meeting_id(meeting_id: str) -> str:
    """Slug папки серии для наряда, у которого портал имени папки не дал.

    Только `[a-z0-9_-]` — значение уезжает в путь файла протокола
    (`lib/paths._target_path` валидирует компонент пути и упал бы на мусоре).
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", meeting_id.lower()).strip("-")
    return ("portal-%s" % slug)[:64] if slug else "portal-meeting"


# ---------- операции ----------


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def read_orders() -> Dict[str, Any]:
    """Всё состояние реестра (для диагностики и статуса)."""
    return _read_raw()


def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    orders = _read_raw().get("orders", {})
    got = orders.get(order_id)
    return dict(got) if isinstance(got, dict) else None


def list_orders(
    *, statuses: Optional[Any] = None, company: Optional[str] = None
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for order in _read_raw().get("orders", {}).values():
        if not isinstance(order, dict):
            continue
        if statuses is not None and order.get("status") not in statuses:
            continue
        if company is not None and order.get("company") != company:
            continue
        out.append(dict(order))
    # По моменту, а не по строке: наряд хранит старт в том поясе, в каком его
    # прислал портал, и строки с разными поясами сортируются не по времени.
    out.sort(key=lambda o: start_sort_key(o.get("start_at")))
    return out


def put_order(
    payload: Dict[str, Any], *, kind: str = KIND_SCHEDULED, now: Optional[datetime] = None
) -> Tuple[Dict[str, Any], bool]:
    """Поставить наряд. Возвращает (наряд, создан_ли_новый).

    Идемпотентность по `meeting_id`: повтор той же постановки НЕ заводит второй
    наряд и НЕ трогает версию — портал имеет право повторить запрос после
    таймаута сети, и это не должно означать второго бота.
    """
    stamp = _now_iso(now)
    order_id = order_id_for_meeting(payload["meeting_id"])

    def _apply(state: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        orders = state.setdefault("orders", {})
        existing = orders.get(order_id)
        if isinstance(existing, dict) and existing.get("status") in ACTIVE_STATUSES:
            # Сравниваем ВСЕ поля наряда, а не пяток «главных»: иначе смена списка
            # участников или чата доставки молча не доезжала бы до реестра —
            # запрос отвечал 200, а бот приходил со старыми данными.
            same = all(existing.get(k) == v for k, v in payload.items())
            if same:
                return dict(existing), False
            # Тот же id, но другое содержимое — это ПЕРЕНОС, а не новый наряд
            # (иначе на встречу приехало бы два бота, старый и новый).
            merged = dict(existing)
            merged.update(payload)
            merged["version"] = int(existing.get("version", 1)) + 1
            merged["updated_at"] = stamp
            merged["status"] = STATUS_SCHEDULED
            if "room_url" in payload:
                # Ключ комнаты обязан ехать за ссылкой. Иначе перенос встречи в
                # другую переговорку оставлял бы `room_key` от старой: «зайти
                # сейчас» по новой комнате завело бы ВТОРОЙ наряд, а «выйти» по
                # ней же не нашло бы ни одного.
                merged["room_key"] = room_key(payload["room_url"])
            orders[order_id] = merged
            return dict(merged), False
        order = dict(payload)
        order.update(
            {
                "id": order_id,
                "kind": kind,
                "status": STATUS_SCHEDULED,
                "source": "portal",
                "version": 1,
                "created_at": stamp,
                "updated_at": stamp,
                "room_key": room_key(payload.get("room_url", "")),
            }
        )
        orders[order_id] = order
        return dict(order), True

    return _mutate(_apply)


def update_order(
    order_id: str,
    patch: Dict[str, Any],
    *,
    expected_version: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Изменить наряд (перенос, смена комнаты). None — наряда нет.

    `expected_version` — оптимистичная блокировка: расхождение → `OrderConflict`,
    портал показывает «встречу изменил кто-то ещё», а не затирает чужую правку.
    """
    stamp = _now_iso(now)

    def _apply(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        orders = state.setdefault("orders", {})
        existing = orders.get(order_id)
        if not isinstance(existing, dict):
            return None
        current_version = int(existing.get("version", 1))
        if expected_version is not None and expected_version != current_version:
            raise OrderConflict(
                "наряд изменился: у вас версия %s, в реестре %s"
                % (expected_version, current_version),
                current_version=current_version,
            )
        merged = dict(existing)
        merged.update(patch)
        merged["id"] = order_id
        merged["version"] = current_version + 1
        merged["updated_at"] = stamp
        if "room_url" in patch:
            merged["room_key"] = room_key(patch["room_url"])
        # Правка снятого наряда возвращает его в строй: на портале «отменил и
        # перенёс» — обычный сценарий, и второй наряд тут был бы лишним.
        if merged.get("status") == STATUS_CANCELLED:
            merged["status"] = STATUS_SCHEDULED
            merged.pop("cancelled_at", None)
            merged.pop("cancel_reason", None)
        orders[order_id] = merged
        return dict(merged)

    return _mutate(_apply)


def cancel_order(
    order_id: str, *, reason: Optional[str] = None, now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """Снять наряд. None — такого наряда нет (отмена несуществующего = 404)."""
    stamp = _now_iso(now)

    def _apply(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        orders = state.setdefault("orders", {})
        existing = orders.get(order_id)
        if not isinstance(existing, dict):
            return None
        merged = dict(existing)
        merged["status"] = STATUS_CANCELLED
        merged["version"] = int(existing.get("version", 1)) + 1
        merged["updated_at"] = stamp
        merged["cancelled_at"] = stamp
        if reason:
            merged["cancel_reason"] = str(reason)[:200]
        orders[order_id] = merged
        return dict(merged)

    return _mutate(_apply)


def _immediate_is_stale(order: Dict[str, Any], now: datetime) -> bool:
    started = _parse_dt(order.get("start_at"))
    if started is None:
        return True
    return (now - started) > timedelta(minutes=IMMEDIATE_TTL_MINUTES)


def find_active_order_for_room(
    room_url: str, *, now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """Живой наряд «прямо сейчас» по этой комнате (или None).

    R12.6: второе нажатие «Подключить сейчас» — по той же комнате, тем же или
    ДРУГИМ человеком — обязано вернуть текущий наряд, а не поднять второго бота.
    """
    now = now or datetime.now(timezone.utc)
    if not (room_url or "").strip():
        # Пустая ссылка не «совпадает» ни с чем: иначе `room_key("")` матчился бы
        # с нарядом, у которого комнаты почему-то нет, и «выйти сейчас» без
        # адреса выгоняло бы случайного бота.
        return None
    key = room_key(room_url)
    for order in _read_raw().get("orders", {}).values():
        if not isinstance(order, dict):
            continue
        if order.get("kind") != KIND_IMMEDIATE:
            continue
        if order.get("status") not in ACTIVE_STATUSES:
            continue
        if order.get("room_key") != key:
            continue
        if _immediate_is_stale(order, now):
            continue
        return dict(order)
    return None


def join_now(
    payload: Dict[str, Any], *, now: Optional[datetime] = None
) -> Tuple[Dict[str, Any], bool]:
    """«Зайти сейчас». Возвращает (наряд, создан_ли_новый).

    Идемпотентность двухслойная: по комнате (второй человек жмёт кнопку по той же
    переговорке) и по `meeting_id` (тот же человек жмёт дважды).
    """
    now = now or datetime.now(timezone.utc)
    existing = find_active_order_for_room(payload.get("room_url", ""), now=now)
    if existing is not None:
        return existing, False
    return put_order(payload, kind=KIND_IMMEDIATE, now=now)


def leave_now(
    *,
    order_id: Optional[str] = None,
    room_url: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """«Выйти сейчас»: помечаем наряд `stopping` + ставим отметку запроса.

    Штатное завершение записи (чтобы файл ушёл в обработку, а не потерялся)
    выполняет исполняющий слой — он читает `stop_requested_at`. Здесь только
    транспорт: наряд помечен, повторный запрос ничего не ломает.
    """
    now = now or datetime.now(timezone.utc)
    if not order_id:
        found = find_active_order_for_room(room_url or "", now=now)
        if found is None:
            return None
        order_id = found["id"]
    stamp = _now_iso(now)
    target_id = order_id

    def _apply(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        orders = state.setdefault("orders", {})
        existing = orders.get(target_id)
        if not isinstance(existing, dict):
            return None
        if existing.get("status") not in ACTIVE_STATUSES:
            # «Выйти» из снятого или уже завершённого наряда — не команда, а 404.
            # Иначе `stopping` воскрешал бы отменённую встречу в живые (statuses
            # активные включают stopping), и планировщик снова увидел бы её.
            return None
        merged = dict(existing)
        merged["status"] = STATUS_STOPPING
        merged["stop_requested_at"] = stamp
        merged["version"] = int(existing.get("version", 1)) + 1
        merged["updated_at"] = stamp
        orders[target_id] = merged
        return dict(merged)

    return _mutate(_apply)


#: Почему разовый наряд закрыт машиной (не человеком): сторож входа сдался.
CLOSE_REASON_JOIN_FAILED = "join_failed"


def close_immediate_after_join_failure(
    order_id: str,
    *,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Закрыть разовый наряд «Подключить сейчас», когда бот так и не вошёл (У9).

    ЧТО БЫЛО. Наряд «зайти сейчас» живёт 180 минут (`IMMEDIATE_TTL_MINUTES`) и всё
    это время держит комнату: повторное нажатие кнопки на портале находило его же
    (`find_active_order_for_room`) и отвечало «бот уже идёт» — хотя сторож входа
    давно сдался. Кнопка молчала до трёх часов.

    СТАЛО. Сторож сдался (после повтора) → наряд `finished` с причиной. Комната
    свободна: новое нажатие заводит свежий наряд со своим временем старта, а значит
    и со своим ключом старта (`<event>@<минута нажатия>`) — прежний ключ в
    `.state.json` ему не мешает.

    🔴 Прежний ключ старта НАМЕРЕННО НЕ ОСВОБОЖДАЕТСЯ. Строка этого наряда может
    ещё жить в `.queue.json` до следующей перестройки очереди планировщиком, а
    разовый наряд годен к запуску 20 минут после нажатия
    (`portal_queue.IMMEDIATE_DUE_MINUTES`): освобождённый ключ поднял бы по
    устаревшей строке ТРЕТЬЕГО бота — и третий круг сообщений людям.

    Трогаем только разовый и ещё живой наряд: плановый наряд — это встреча в
    календаре портала, её судьбу решает человек; снятый/закрытый — не воскрешаем.
    Версию не поднимаем — это отметка исполнения, а не правка встречи (как
    `attach_run`). Возвращает наряд после правки или None, если трогать нечего.
    """
    stamp = _now_iso(now)
    target_id = str(order_id or "")
    if not target_id:
        return None

    def _apply(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        orders = state.setdefault("orders", {})
        existing = orders.get(target_id)
        if not isinstance(existing, dict):
            return None
        if existing.get("kind") != KIND_IMMEDIATE:
            return None
        if existing.get("status") not in ACTIVE_STATUSES:
            return None
        merged = dict(existing)
        merged["status"] = STATUS_FINISHED
        merged["finished_at"] = stamp
        merged["finish_reason"] = CLOSE_REASON_JOIN_FAILED
        if reason:
            merged["join_failure"] = str(reason)[:120]
        merged["updated_at"] = stamp
        orders[target_id] = merged
        return dict(merged)

    return _mutate(_apply)


def attach_run(
    order_id: str,
    *,
    session_uid: str,
    launched_at: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Ф10c: записать в наряд, что по нему поднялся бот (`run`).

    Зачем отдельным полем, а не выводом из имён файлов: у бота и у планировщика
    РАЗНЫЕ идентификаторы сессии и они не связаны — `runner._safe_session_uid`
    даёт `auto-<meeting>-<start>`, а сам бот пишет файлы под
    `<дата>-<connectionId|tm-…>` (`recording.ts:649`). Связать результат с
    нарядом «по имени файла» физически нельзя, поэтому связь ведётся здесь.

    Версию наряда НЕ трогаем: это отметка исполнения, а не правка встречи, и
    поднимать `version` значило бы отдать порталу ложный «кто-то изменил
    встречу» на ровном месте (оптимистичная блокировка `expected_version`).
    """
    stamp = _now_iso(launched_at)
    target_id = str(order_id)
    sid = str(session_uid or "").strip()
    if not sid:
        return None

    def _apply(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        orders = state.setdefault("orders", {})
        existing = orders.get(target_id)
        if not isinstance(existing, dict):
            return None
        merged = dict(existing)
        merged["run"] = {"session_uid": sid, "launched_at": stamp}
        merged["updated_at"] = stamp
        orders[target_id] = merged
        return dict(merged)

    return _mutate(_apply)


#: Окно, в котором запись считается «той самой встречей» наряда. Слева щедро
#: (бот заходит заранее, а перенос на пару минут — рядовое дело), справа —
#: длительность встречи плюс запас на овертайм: совещания заканчиваются позже
#: плана чаще, чем раньше.
MATCH_BEFORE_MINUTES = 30
MATCH_AFTER_MINUTES = 120


def find_order_for_recording(
    *,
    room_url: Optional[str],
    started_at: Any,
    session_uid: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Чей это результат: наряд портала или личная серия владельца (None).

    🔴 `None` — ОБЫЧНЫЙ ИСХОД, а не ошибка: личные серии владельца нарядов не
    имеют и в портал не едут. Ветка «наряда нет» обязана вести себя ровно как
    сегодня, поэтому здесь нет ни предупреждений, ни алертов.

    Сопоставление в два шага:

      1. **По отметке запуска** (`run.session_uid`, `attach_run`) — точный ключ,
         если бот поднимал планировщик и наряд об этом знает.
      2. **По комнате и времени** — запасной путь для случаев, когда отметки нет
         (наряд поставили, пока бот уже сидел; выкатка посреди встречи; ручной
         прогон). Комната сравнивается ХЕШЕМ хоста и пути (`room_key`), то есть
         строго; время — окном вокруг старта наряда, иначе вчерашняя встреча в
         той же переговорке присвоила бы себе сегодняшний результат.

    Отменённые и завершённые наряды тоже участвуют: разговор УЖЕ записан, и
    терять результат из-за того, что встречу отменили постфактум, нельзя.
    """
    sid = str(session_uid or "").strip()
    started = _parse_dt(started_at)
    key = room_key(room_url) if (room_url or "").strip() else ""
    best: Optional[Dict[str, Any]] = None
    best_delta: Optional[timedelta] = None
    for order in _read_raw().get("orders", {}).values():
        if not isinstance(order, dict):
            continue
        run = order.get("run")
        if sid and isinstance(run, dict) and str(run.get("session_uid") or "") == sid:
            return dict(order)
        if not key or started is None:
            continue
        if order.get("room_key") != key:
            continue
        order_start = _parse_dt(order.get("start_at"))
        if order_start is None:
            continue
        duration = max(1, int(order.get("duration_minutes") or DEFAULT_DURATION_MINUTES))
        if started < order_start - timedelta(minutes=MATCH_BEFORE_MINUTES):
            continue
        if started > order_start + timedelta(minutes=duration + MATCH_AFTER_MINUTES):
            continue
        delta = abs(started - order_start)
        if best_delta is None or delta < best_delta:
            best, best_delta = dict(order), delta
    return best


def active_orders_overlapping(
    start: datetime, duration_minutes: int, *, exclude_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Живые наряды, чьё окно пересекается с [start; start+duration].

    Нужно, чтобы шлюз мог честно сказать «в это время бот занят» ПРИ ПОСТАНОВКЕ
    наряда, а не молчать до самой встречи (лимит `MAX_CONCURRENT_BOTS`).
    """
    end = start + timedelta(minutes=max(1, duration_minutes))
    out: List[Dict[str, Any]] = []
    for order in _read_raw().get("orders", {}).values():
        if not isinstance(order, dict) or order.get("status") not in ACTIVE_STATUSES:
            continue
        if exclude_id is not None and order.get("id") == exclude_id:
            continue
        other_start = _parse_dt(order.get("start_at"))
        if other_start is None:
            continue
        other_end = other_start + timedelta(
            minutes=max(1, int(order.get("duration_minutes") or DEFAULT_DURATION_MINUTES))
        )
        if other_start < end and start < other_end:
            out.append(dict(order))
    return out
