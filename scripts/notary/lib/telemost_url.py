"""Строгая сверка адреса ссылки на встречу (хвост С10, фаза 3 плана
`plans/2026-09-23-telemost-update-resilience.md`).

ЗАЧЕМ. До 25.09.2026 хост сверяли подстрокой: `"telemost.yandex.ru" in url`.
Такой проверке всё равно, ГДЕ стоит подстрока: `https://evil.example/telemost.yandex.ru/j/1`
проходила, и бот открыл бы чужую страницу. Для личных серий ссылка приходит из
календаря — её может вписать внешний организатор. А запасной путь входа
(фаза 3б) нажимает кнопки по тексту: на чужой странице это опасно.

КАК. Разбираем адрес и сравниваем ИМЯ ХОСТА целиком — точное совпадение или
поддомен. Отвергаем всё, что браузер мог бы понять иначе, чем Python:
обратную косую черту (браузер читает её как «/», и хостом стал бы кусок до неё),
пробелы и управляющие символы внутри, логин/пароль перед хостом
(`https://telemost.yandex.ru@evil.example/`), нестандартный порт.

Модуль без зависимостей — его импортируют планировщик (`lib/gcal.py`), реестр
(`cli/registry.py`), запускатель, наряды портала (`lib/orders.py`, с фазы 3б) и
самопроверка (`lib/canary.valid_room_url`, с фазы 3б).
Та же проверка в боте — `services/vexa-bot/core/src/platforms/yandextelemost/screen.ts`
(`isTelemostMeetingUrl`); в дикторе самопроверки — своя копия (`canary-kit/speaker.js`).

Здесь же — `room_url_key`: ОДНА формула «две ссылки ведут в одну комнату» на весь
проект (склейка дублей и «нет» владельца — `meeting_identity.url_key`, ключ наряда —
`orders.room_key`, счёт слотов — `slots`, сверка очереди с реестром нарядов — `runner`).
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple
from urllib.parse import urlsplit

#: Хосты Яндекс Телемоста (обычный и Яндекс 360 для бизнеса) — с поддоменами.
TELEMOST_HOSTS: Tuple[str, ...] = ("telemost.yandex.ru", "telemost.360.yandex.ru")

_DEFAULT_PORTS = {"https": 443, "http": 80}


def host_matches(host: str, allowed: Iterable[str]) -> bool:
    """Хост совпадает с одним из разрешённых целиком или является его поддоменом."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    for a in allowed:
        a = (a or "").strip().lower().rstrip(".")
        if a and (h == a or h.endswith("." + a)):
            return True
    return False


def url_host_in(url: object, allowed: Iterable[str], *, schemes: Tuple[str, ...] = ("https",)) -> bool:
    """Ссылка ведёт на один из разрешённых хостов (строго, см. шапку модуля)."""
    if not isinstance(url, str):
        return False
    s = url.strip()
    if not s or "\\" in s:
        return False
    # Пробелы, табуляции, переводы строк и прочие управляющие символы внутри адреса
    # Python и браузер разбирают по-разному (urlsplit молча выкидывает \t и \n).
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s):
        return False
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme not in tuple(x.lower() for x in schemes):
        return False
    netloc = parts.netloc or ""
    if not netloc or "@" in netloc:
        return False
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        return False
    return host_matches(parts.hostname or "", allowed)


def is_telemost_url(url: object, *, schemes: Tuple[str, ...] = ("https",)) -> bool:
    """Ссылка ведёт в Яндекс Телемост (хост целиком, не подстрока)."""
    return url_host_in(url, TELEMOST_HOSTS, schemes=schemes)


def room_url_key(url: object) -> Optional[str]:
    """Какая это комната: `хост/путь` в нижнем регистре. None — сравнивать нечего.

    ЗАЧЕМ (находка №5 проверки 04.10.2026, решение владельца 06.10). Ссылки комнат
    сравнивали в четырёх местах, и каждое отрезало своё: склейка дублей и «нет»
    владельца — регистр и конечную «/», ключ наряда — ещё и хвост `?…`/`#…`, счёт
    слотов — только регистр, сверка очереди с реестром нарядов — ничего. Ссылка с
    utm-меткой для одних мест была той же комнатой, для других — чужой: «нет»
    владельца не гасило встречу, дубль не склеивался, в комнату шёл второй бот.

    КАК. Комната — хост и путь. Отбрасываем то, что её не меняет: хвост `?…` и
    `#…`, схему, порт и логин перед хостом, конечные «/», точку в конце имени
    хоста, регистр. Ссылка без схемы даёт ту же строку, что и со схемой.

    Это сравнение, а не проверка: годится ли ссылка боту, решает `is_telemost_url`.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").rstrip(".")
    except ValueError:  # «https://[::1» и подобное — urlsplit не разбирает
        return None
    canon = (host + (parts.path or "").rstrip("/")).lower()
    return canon or None
