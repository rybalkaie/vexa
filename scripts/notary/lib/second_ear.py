"""Второе ухо бота на стороне хоста (ISS-60, фаза 4 плана telemost-update-resilience).

ЧТО ЭТО. Бот, кроме основной записи (звук из дорожек звонка внутри страницы), пишет
«второе ухо» — всё, что браузер реально ИГРАЕТ в звуковую систему контейнера
(`services/vexa-bot/core/src/platforms/yandextelemost/second-ear.ts`). Файл лежит
рядом с основной записью: `<дата>-<сессия>.chunk0.wav` (основная нумерует куски с 1).

КТО ЧТО ДЕЛАЕТ.
  • Бот решает сам, оглохла ли основная запись (правило «минута подряд» + итог в конце
    встречи) и пишет это в meta: `recording.audioSource` = "primary" | "second_ear".
    При "second_ear" `recording.chunks[]` и `files.wav` уже указывают на второе ухо —
    финализации для звука ничего делать не нужно, двойного звука нет.
  • Финализация (`finalize-meeting.py`) по "second_ear" пишет владельцу — `notify_owner_if_ear`.
  • Сборщик (`collector.py`) после подтверждённой доставки удаляет второе ухо по тем же
    правилам, что основную запись (`files.wav`): не раньше доставки, не под удержанием
    портала, не при keep_audio. Иначе второе ухо копилось бы на диске навсегда (час
    встречи ≈ 115 МБ; свободно на сервере ~25 ГБ).
  • Удержание для портала (`portal_delivery.session_wav_files`) и сторож зависаний
    сборщика знают файл по имени `*.chunk<цифры>.wav` — отдельного кода не нужно.
  • Самопроверка (`lib/canary.py`) отличает второе ухо от основной записи по суффиксу.

🔴 ОПАСНАЯ ТРОЙКА. Здесь только метаданные: пути, правило, секунды. Текста встречи нет.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("notary.second_ear")

#: Кусок второго уха: номер 0, основная запись нумерует куски с 1 (`recording.ts`).
EAR_CHUNK_SUFFIX = ".chunk0.wav"
AUDIO_SOURCE_EAR = "second_ear"
AUDIO_SOURCE_PRIMARY = "primary"

try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    _MSK = ZoneInfo("Europe/Moscow")
except Exception:  # noqa: BLE001 — нет базы поясов → фиксированный +3
    _MSK = timezone(timedelta(hours=3))


def _dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def is_ear_file(name: Any) -> bool:
    """Имя файла — второе ухо (`…chunk0.wav`)?"""
    return isinstance(name, str) and PurePosixPath(name).name.endswith(EAR_CHUNK_SUFFIX)


def ear_wav(meta: Any) -> Optional[str]:
    """Путь второго уха из meta (`recording.secondEar.wav`) или None.

    Путь проверяется до того, как им воспользуется удаление: только файл с
    суффиксом второго уха и без `..` — meta пишет наш бот, но удаление по пути из
    файла без проверки — плохая привычка.
    """
    rec = _dict(_dict(meta).get("recording"))
    wav = _dict(rec.get("secondEar")).get("wav")
    if not is_ear_file(wav):
        return None
    if ".." in PurePosixPath(str(wav)).parts:
        return None
    return str(wav)


def uses_ear(meta: Any) -> bool:
    """Протокол этой встречи собирается из второго уха (основная оглохла)."""
    return _dict(_dict(meta).get("recording")).get("audioSource") == AUDIO_SOURCE_EAR


def _msk_hm(raw: Any) -> Optional[str]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK).strftime("%H:%M") + " МСК"


def _minutes(sec: Any) -> str:
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return "меньше минуты"
    return f"{round(s / 60)} мин"


def owner_note(meta: Any, *, series: Optional[str] = None, date: Optional[str] = None) -> Optional[str]:
    """Текст владельцу «протокол собран со второго уха» или None (основная в порядке).

    Только метаданные: серия, дата, время, минуты. Ни ссылки на комнату, ни текста.
    """
    if not uses_ear(meta):
        return None
    m = _dict(meta)
    rec = _dict(m.get("recording"))
    deaf = _dict(rec.get("primaryDeaf"))
    series_lbl = series or str(m.get("series") or "встреча")
    date_lbl = date or str(m.get("startTs") or "")[:10] or "?"
    since = _msk_hm(deaf.get("since"))
    since_part = f" с {since}" if since else ""
    if deaf.get("rule") == "sustained":
        why = "основная запись молчала минуту подряд, пока второе ухо слышало речь"
    else:
        why = (
            f"за встречу основная запись услышала {_minutes(deaf.get('primaryLoudS'))} звука, "
            f"второе ухо — {_minutes(deaf.get('earLoudS'))}"
        )
    text = (
        f"🎧 Ватсон: встреча «{series_lbl}» {date_lbl} — основная запись бота не слышала "
        f"звонок{since_part}. Протокол собираю со второго уха (запись всего, что играл "
        f"браузер бота): встреча не потеряна.\n"
        f"Почему так решил: {why}.\n"
        f"Основную запись бота надо чинить — вероятно, Телемост поменял, как отдаёт звук."
    )
    if deaf.get("forced"):
        text += "\n(Это проверка: основная запись заглушена флагом живой проверки.)"
    return text


def notify_owner_if_ear(
    meta: Any,
    *,
    session_uid: str,
    date: Optional[str] = None,
    push: Optional[Callable[..., Any]] = None,
) -> bool:
    """Финализация: протокол из второго уха → сообщение владельцу (раз в 6 ч на встречу).

    Возвращает True, если сообщение ушло. Не бросает: встреча важнее сообщения.
    """
    text = owner_note(meta, date=date)
    if text is None:
        return False
    if _dict(meta).get("delivered"):
        # Перегенерация уже доставленной встречи: владелец об этом уже знает.
        return False
    deaf = _dict(_dict(_dict(meta).get("recording")).get("primaryDeaf"))
    logger.info(
        "[second-ear] протокол из второго уха: sid=%s rule=%s forced=%s",
        session_uid, deaf.get("rule"), bool(deaf.get("forced")),
    )
    try:
        if push is None:
            from notary.lib import notify  # noqa: PLC0415

            push = notify.push
        return bool(push(text, dedupe_key=f"second-ear:{session_uid}"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[second-ear] сообщение владельцу не ушло (%s)", type(e).__name__)
        return False


def cleanup_after_delivery(meta: Any, sid: str, *, host_path: Callable[[str], Path]) -> Optional[str]:
    """Удалить второе ухо после подтверждённой доставки. Гейты — у вызывающего.

    Зовётся ТАМ ЖЕ и ТОГДА ЖЕ, когда сборщик удаляет основную запись (`files.wav`):
    после доставки, не под удержанием портала, не при keep_audio. Если второе ухо и
    есть `files.wav` (основная оглохла), его уже удалил основной код — здесь ничего.
    Возвращает путь удалённого файла или None.
    """
    ear = ear_wav(meta)
    if not ear:
        return None
    main = _dict(_dict(meta).get("files")).get("wav")
    if isinstance(main, str) and PurePosixPath(main).name == PurePosixPath(ear).name:
        return None
    p = host_path(ear)
    try:
        if p.is_symlink() or not p.is_file():
            return None
        p.unlink()
    except OSError as e:
        logger.warning("Второе ухо %s не удалено (%s)", p.name, type(e).__name__)
        return None
    logger.info("Второе ухо удалено после доставки (sid=%s, path=%s)", sid, ear)
    return ear
