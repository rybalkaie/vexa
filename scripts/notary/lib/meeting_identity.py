"""ISS-54: одна формула «это одна и та же встреча» — на всю зону одна.

Зачем отдельный модуль. Вопрос «эти два описания — про одну встречу?» задают
ДВОЕ: планировщик (схлопывает очередь стартов, иначе в комнату придут два бота)
и apply-reply (не заводит вторую запись в реестре на то же событие). Пока
формула была скопирована в оба файла, реализации разошлись — и два дедупа стали
давать противоположные ответы на одном и том же случае. Здесь она одна.

Из чего складывается ответ (правило принято по ходу ISS-54):
  • `iCalUID` — точный ключ Google: у копии события в другом календаре он
    совпадает. Ловушка: у всех экземпляров ОДНОЙ рекуррентной серии он тоже
    общий, поэтому в очереди стартов он всегда парный — `iCalUID` + время
    старта. Ключ без времени выкосил бы всю неделю регулярной встречи.
  • Комната + время старта — страховка, и на боевом пути она главная: в снапшот
    вечернего блока `iCalUID` пока не приезжает. Две РАЗНЫЕ встречи не могут
    занимать одну комнату в одну и ту же секунду — один URL Telemost это один
    разговор. Поэтому совпадение (url, start) считаем дублем и схлопываем
    НЕЗАВИСИМО от того, известны ли `iCalUID` и совпали ли они.
  • Различный `iCalUID` при совпавшей комнате и секунде — не повод пропустить
    обоих (иначе придут два бота, ровно та беда, ради которой всё делалось), а
    повод сказать владельцу: в календаре это два разных события, проверь.
    Про это `compare` возвращает второй элемент — `ical_conflict`.

Зависимостей извне у модуля нет — только стандартная библиотека.
"""
from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timezone
from typing import Any

# Чем доказано тождество. Пустая строка — не доказано ничем.
MATCH_NONE = ""
MATCH_ICAL = "ical"      # совпал точный ключ события
MATCH_ROOM = "room"      # совпали комната и секунда старта (страховка)
MATCH_ID = "id"          # это буквально та же запись (совпал id/event_id)

# Нормализованные приметы одной встречи. Ключи считаются по разу на объект —
# на этом же держится линейность дедупа очереди (раньше ISO-строка разбиралась
# заново на каждую пару, и 1000 стартов стоили 2,6 с).
Fingerprint = namedtuple("Fingerprint", "start url ical")


def instant_key(value: Any) -> str | None:
    """Момент старта в UTC как строка. None — если время не разобрать.

    Две копии одного события в разных календарях могут приехать с разными
    офсетами («…T13:00+04:00» и «…T09:00Z») — сравнивать сырые строки нельзя.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def url_key(value: Any) -> str | None:
    """URL комнаты для сравнения: без хвостового слэша и регистра."""
    if not isinstance(value, str):
        return None
    return value.strip().rstrip("/").lower() or None


def ical_key(value: Any) -> str | None:
    """`iCalUID` для сравнения. Мусор и пустая строка — как «неизвестно»."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def fingerprint(start: Any = None, url: Any = None, ical: Any = None) -> Fingerprint:
    """Разобрать приметы один раз (см. комментарий у `Fingerprint`)."""
    return Fingerprint(instant_key(start), url_key(url), ical_key(ical))


def compare(a: Fingerprint, b: Fingerprint, *, series_scope: bool = False) -> tuple:
    """Одна ли это встреча? Возвращает (чем доказано, конфликт точных ключей).

    `series_scope=True` — сравниваем ЗАПИСИ О СЕРИИ (не отдельные старты): у
    серии `iCalUID` общий на все экземпляры, и это как раз то, что нужно —
    копия серии из второго календаря опознаётся, даже если увидели её в другой
    день. В очереди стартов `series_scope` всегда False: там экземпляры одной
    серии обязаны остаться разными встречами.

    Второй элемент ответа — `ical_conflict`: тождество доказано комнатой, но
    точные ключи известны и РАЗНЫЕ. Схлопывать — да (два бота хуже), молчать —
    нет: владельцу говорим, что в календаре два разных события.
    """
    same_start = bool(a.start) and a.start == b.start
    both_ical = bool(a.ical) and bool(b.ical)
    if both_ical and a.ical == b.ical and (series_scope or same_start):
        return (MATCH_ICAL, False)
    if same_start and a.url and a.url == b.url:
        return (MATCH_ROOM, both_ical and a.ical != b.ical)
    return (MATCH_NONE, False)


def proof_ru(proof: str, a: Fingerprint) -> str:
    """Причина схлопывания человеческими словами — для лога и для владельца."""
    if proof == MATCH_ICAL:
        return "одно и то же событие календаря (%s)" % (a.ical or "?")
    if proof == MATCH_ROOM:
        return "одна комната %s в одну и ту же секунду" % (a.url or "?")
    if proof == MATCH_ID:
        return "та же запись реестра"
    return "совпадений нет"
