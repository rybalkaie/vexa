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
  • «Спутник» записи (`companion_wavs`) — файл встречи, который живёт и удаляется ВМЕСТЕ
    с `files.wav`, но в нём не указан: второе ухо, пока протокол из основной; кусок
    основной, который до фазы 4 стоял бы в `files.wav`, когда протокол из второго уха.
  • Сборщик (`collector.py`) после подтверждённой доставки удаляет спутников по тем же
    правилам, что основную запись (`files.wav`): не раньше доставки, не под удержанием
    портала, не при keep_audio. Иначе второе ухо (или заглушённая основная) копилось
    бы на диске навсегда (час встречи ≈ 115 МБ; свободно на сервере ~25 ГБ) — метельщик
    `meeting-notary-cleanup` куски `*.chunkN.wav` не находит.
  • Фаза 6б (копилка владельца №3, 26.09): там же и тогда же удаляются ВСЕ остальные
    куски основной записи этой встречи (`extra_recording_wavs`: длинная встреча с
    паузами пишется кусками `.chunk1…N.wav`, а в `files.wav` стоит только первый кусок
    с речью). Раньше их не удалял никто — полные голоса участников лежали вечно.
  • Сбой расшифровки (`finalize-meeting._stash_into_failed`): исходник переезжает в
    `_failed/`, meta из `_tmp/` удаляется — спутники переезжают туда же
    (`stash_companions`), иначе сборщик их больше не нашёл бы. В `_failed/` их убирает
    та же уборка `<sid>.*` после удачного повтора, с тем же удержанием для портала.
    Остальные куски основной записи НЕ переезжают: из них повтор заново склеивает звук
    (`wav_concat.resolve_wav_for_stt`), и полупереехавший набор дал бы протокол без
    части встречи. Их удаляет та же уборка после удачного повтора
    (`primary_chunk_wavs`, finalize 5.1) — с тем же удержанием для портала.
  • Удержание для портала (`portal_delivery.session_wav_files`) и сторож зависаний
    сборщика знают файл по имени `*.chunk<цифры>.wav` — отдельного кода не нужно.
  • Самопроверка (`lib/canary.py`) отличает второе ухо от основной записи по суффиксу.

🔴 ОПАСНАЯ ТРОЙКА. Здесь только метаданные: пути, правило, секунды. Текста встречи нет.
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("notary.second_ear")

#: Кусок второго уха: номер 0, основная запись нумерует куски с 1 (`recording.ts`).
EAR_CHUNK_SUFFIX = ".chunk0.wav"
AUDIO_SOURCE_EAR = "second_ear"
AUDIO_SOURCE_PRIMARY = "primary"
#: Кусок ОСНОВНОЙ записи: номер с 1 (`recording.ts`, openChunk).
_PRIMARY_CHUNK_RE = re.compile(r"\.chunk([1-9]\d*)\.wav$")
#: Хвост имени любого куска — с ним спутник переезжает в `_failed/<sid>.<хвост>`.
_CHUNK_TAIL_RE = re.compile(r"\.(chunk\d+\.wav)$")
#: Полное имя куска записи бота: `<ГГГГ-ММ-ДД>-<сессия>.chunk<N>.wav` (`recording.ts`,
#: chunkWavPath). N = 0 — второе ухо, N ≥ 1 — основная запись.
_CHUNK_NAME_RE = re.compile(r"^(?P<stem>\d{4}-\d{2}-\d{2}-.+?)\.chunk(?P<n>0|[1-9]\d*)\.wav$")
#: Основа имени куска: дата встречи + sessionUid бота.
_DATED_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(?P<uid>.+)$")

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
    if deaf.get("rule") == "sustained":
        since = _msk_hm(deaf.get("since"))
        heard = "не слышала звонок" + (f" с {since}" if since else "")
        why = "основная запись молчала минуту подряд, пока второе ухо слышало речь"
    else:
        # ear_heard_more: основная что-то слышала — «не слышала с ЧЧ:ММ» было бы неправдой.
        heard = "слышала звонок лишь частично"
        why = (
            f"за встречу основная запись услышала {_minutes(deaf.get('primaryLoudS'))} звука, "
            f"второе ухо — {_minutes(deaf.get('earLoudS'))}"
        )
    text = (
        f"🎧 Ватсон: встреча «{series_lbl}» {date_lbl} — основная запись бота {heard}. "
        f"Протокол собираю со второго уха (запись всего, что играл "
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


def primary_legacy_wav(meta: Any) -> Optional[str]:
    """Кусок ОСНОВНОЙ записи, который до фазы 4 стоял бы в `files.wav`, или None.

    Только когда протокол собран из второго уха (`audioSource = second_ear`): тогда бот
    ставит в `files.wav` второе ухо, а куски основной уходят в `recording.primaryChunks`.
    Правило выбора — как у бота (`recording.ts`, buildMeta): первый кусок с речью, иначе
    первый. Сборщик удалял его после доставки всегда; без этого заглушённая основная
    (при глухоте — вся встреча, при частичной — и живые голоса до поломки) лежала бы
    на диске навсегда.

    Путь проверяется строго — удаление идёт по пути из файла: кусок основной
    (`.chunk<N≥1>.wav`), та же папка и та же сессия, что у второго уха, без `..`.
    """
    if not uses_ear(meta):
        return None
    ear = ear_wav(meta)
    if not ear:
        return None
    rec = _dict(_dict(meta).get("recording"))
    raw = rec.get("primaryChunks")
    chunks = [c for c in (raw if isinstance(raw, list) else [])
              if isinstance(c, dict) and isinstance(c.get("wav"), str) and c.get("wav")]
    if not chunks:
        return None
    with_speech = [c for c in chunks
                   if c.get("firstSpeechMs") is not None and c.get("lastSpeechMs") is not None]
    wav = str((with_speech or chunks)[0]["wav"])
    p, e = PurePosixPath(wav), PurePosixPath(ear)
    if ".." in p.parts or p.parent != e.parent:
        return None
    m = _PRIMARY_CHUNK_RE.search(p.name)
    if not m or p.name[: m.start()] != e.name[: -len(EAR_CHUNK_SUFFIX)]:
        return None
    return wav


def companion_wavs(meta: Any) -> List[str]:
    """Спутники записи: файлы встречи, которые живут и удаляются вместе с `files.wav`.

    Пока протокол из основной — второе ухо. Когда из второго уха (`files.wav` = оно) —
    кусок основной, который раньше был `files.wav` (`primary_legacy_wav`). Сам
    `files.wav` сюда не входит: его убирает основной код сборщика.
    """
    main = _dict(_dict(meta).get("files")).get("wav")
    main_name = PurePosixPath(main).name if isinstance(main, str) and main else None
    out: List[str] = []
    for wav in (ear_wav(meta), primary_legacy_wav(meta)):
        if wav and PurePosixPath(wav).name != main_name and wav not in out:
            out.append(wav)
    return out


def _chunk_parts(wav: Any) -> Optional[Tuple[PurePosixPath, str, int]]:
    """(папка, основа `<дата>-<сессия>`, номер куска) для пути куска записи бота, иначе None."""
    if not isinstance(wav, str) or not wav:
        return None
    p = PurePosixPath(wav)
    if ".." in p.parts:
        return None
    m = _CHUNK_NAME_RE.match(p.name)
    if not m:
        return None
    return p.parent, m.group("stem"), int(m.group("n"))


def _session_matches(stem: str, sid: Any) -> bool:
    """Основа имени куска принадлежит ЭТОЙ встрече.

    `sid` сборщика — имя meta-файла (`<дата>-<сессия>`), `sid` финализации — sessionUid
    без даты: принимаем ровно эти две формы.
    """
    s = str(sid or "")
    if not s or "/" in s or s in (".", ".."):
        return False
    if stem == s:
        return True
    m = _DATED_STEM_RE.match(stem)
    return bool(m) and m.group("uid") == s


def _chunk_entries(meta: Any) -> List[Any]:
    """Пути из `recording.chunks[]` и `recording.primaryChunks[]` (как есть, без проверки)."""
    rec = _dict(_dict(meta).get("recording"))
    out: List[Any] = []
    for key in ("chunks", "primaryChunks"):
        raw = rec.get(key)
        for c in raw if isinstance(raw, list) else []:
            if isinstance(c, dict):
                out.append(c.get("wav"))
    return out


def _meeting_anchor(meta: Any, sid: Any) -> Optional[Tuple[PurePosixPath, str]]:
    """Папка и основа имени записи ЭТОЙ встречи — с ними сверяется каждый кусок.

    Берётся из второго уха, иначе из `files.wav` (если это кусок), иначе из первого куска
    в meta. Основа обязана принадлежать встрече `sid`, иначе None: meta не той встречи
    не даёт удалить ничего.
    """
    m = _dict(meta)
    for wav in (ear_wav(m), _dict(m.get("files")).get("wav"), *_chunk_entries(m)):
        parts = _chunk_parts(wav)
        if parts is None:
            continue
        folder, stem, _n = parts
        return (folder, stem) if _session_matches(stem, sid) else None
    return None


def primary_chunk_wavs(meta: Any, sid: Any) -> List[str]:
    """Все куски ОСНОВНОЙ записи встречи `sid`, известные из meta (номер куска ≥ 1).

    Источники: `files.wav`, `recording.chunks[]`, `recording.primaryChunks[]` (последнее —
    когда протокол собран из второго уха). Путь проверяется строго, потому что удаление
    идёт по пути из файла: имя `<дата>-<сессия>.chunk<N≥1>.wav` этой встречи, та же папка,
    что у остальной её записи, без `..`. Что не прошло проверку — пропускается молча.
    """
    anchor = _meeting_anchor(meta, sid)
    if anchor is None:
        return []
    m = _dict(meta)
    out: List[str] = []
    for wav in (_dict(m.get("files")).get("wav"), *_chunk_entries(m)):
        parts = _chunk_parts(wav)
        if parts is None or parts[2] < 1 or (parts[0], parts[1]) != anchor:
            continue
        if wav not in out:
            out.append(str(wav))
    return out


def extra_recording_wavs(meta: Any, sid: Any) -> List[str]:
    """Весь звук встречи `sid`, кроме `files.wav`, — удаляется вместе с ним после доставки.

    Спутники (`companion_wavs`: второе ухо / заглушённая основная) и, с фазы 6б
    (копилка владельца №3, 26.09), все остальные куски основной записи
    (`primary_chunk_wavs`). Сам `files.wav` убирает основной код сборщика. Каждый путь —
    кусок ЭТОЙ встречи в её папке (`_meeting_anchor`); meta не той встречи → пусто.
    """
    anchor = _meeting_anchor(meta, sid)
    if anchor is None:
        return []
    main = _dict(_dict(meta).get("files")).get("wav")
    main_name = PurePosixPath(main).name if isinstance(main, str) and main else None
    out: List[str] = []
    for wav in (*companion_wavs(meta), *primary_chunk_wavs(meta, sid)):
        parts = _chunk_parts(wav)
        if parts is None or (parts[0], parts[1]) != anchor:
            continue
        if PurePosixPath(wav).name != main_name and wav not in out:
            out.append(wav)
    return out


def remove_wavs(paths: List[str], sid: str, *, host_path: Callable[[str], Path], why: str) -> List[str]:
    """Удалить файлы звука по списку. Ссылкам не следует, только обычные файлы. Не бросает."""
    removed: List[str] = []
    for wav in paths:
        p = host_path(wav)
        try:
            if p.is_symlink() or not p.is_file():
                continue
            p.unlink()
        except OSError as e:
            logger.warning("Кусок записи %s не удалён (%s)", p.name, type(e).__name__)
            continue
        logger.info("Кусок записи удалён %s (sid=%s, path=%s)", why, sid, wav)
        removed.append(wav)
    return removed


def cleanup_after_delivery(meta: Any, sid: str, *, host_path: Callable[[str], Path]) -> List[str]:
    """Удалить весь звук встречи, кроме `files.wav`, после подтверждённой доставки.

    Гейты — у вызывающего. Зовётся ТАМ ЖЕ и ТОГДА ЖЕ, когда сборщик удаляет основную
    запись (`files.wav`): после доставки, не под удержанием портала, не при keep_audio.
    Список — `extra_recording_wavs`: спутники и куски основной записи 2…N. Ссылкам не
    следует. Возвращает пути удалённых файлов.
    """
    return remove_wavs(extra_recording_wavs(meta, sid), sid, host_path=host_path, why="после доставки")


def stash_companions(meta: Any, sid: str, failed_dir: Path, *,
                     host_path: Callable[[str], Path]) -> List[str]:
    """Сбой расшифровки: перенести спутников записи в `_failed/<sid>.chunkN.wav`.

    `_stash_into_failed` переносит исходник в `_failed/` и удаляет meta из `_tmp/` —
    сборщик эту встречу больше не увидит, и спутник в `_tmp/` остался бы навсегда.
    В `_failed/` его уберёт та же уборка `<sid>.*` после удачного повтора (с тем же
    удержанием для портала), а при ручном разборе всё лежит рядом. Перенос = копия,
    затем удаление оригинала, только если копия целая. Не бросает (OSError — в журнал).
    Возвращает пути перенесённых файлов.

    Остальные куски основной записи сюда НЕ входят и остаются на месте (фаза 6б): из
    них повтор заново склеивает звук, а набор, переехавший наполовину (кончилось место),
    дал бы протокол без части встречи. Их удаляет finalize после удачного повтора
    (`primary_chunk_wavs`).
    """
    safe_sid = PurePosixPath(str(sid)).name
    moved: List[str] = []
    for wav in companion_wavs(meta):
        m = _CHUNK_TAIL_RE.search(PurePosixPath(wav).name)
        if not m or not safe_sid:
            continue
        src = host_path(wav)
        dst = Path(failed_dir) / f"{safe_sid}.{m.group(1)}"
        try:
            if src.is_symlink() or not src.is_file():
                continue
            if not dst.exists():
                shutil.copy2(src, dst)
            if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                src.unlink()
                moved.append(wav)
                logger.info("Спутник записи перенесён в _failed (sid=%s, %s)", safe_sid, dst.name)
        except OSError as e:
            logger.warning("Спутник записи %s не перенесён (%s)", PurePosixPath(wav).name, type(e).__name__)
    return moved
