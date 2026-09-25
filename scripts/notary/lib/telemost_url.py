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
"""
from __future__ import annotations

from typing import Iterable, Tuple
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
