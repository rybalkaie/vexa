#!/usr/bin/env python3
"""CLI-обёртка восстановления протокола из ручной записи.

План: ../../plans/2026-06-30-bot-eviction-recovery-resilience.md, Фаза 4
(Блок B, ядро). Закрывает REQ R7 / R9 / R11.

Нештатный путь: бот встречу НЕ записал (выброс из Телемоста / сбой), участник
записал её вручную. Эта обёртка принимает запись постфактум и собирает протокол
поверх существующего финализатора (`finalize-meeting.py`), чтобы Илья не терял
протокол.

Три входа (тип определяется по расширению, можно форсировать `--kind`):

  • ВИДЕО (`.mp4`/`.mkv`/…) — основной путь (R7):
        ffmpeg извлекает WAV → синтетическая `<sid>.meta.json` (серия/дата/
        участники из реестра) → `finalize-meeting.py` (STT + диаризация +
        генерация протокола) → в протокол добавляется пометка
        «⚠️ диаризация не выверена — сверьте роли» → доставка ЛИЧНО Илье.

  • АУДИО (`.wav`/`.mp3`/…) — тот же путь R7 (ffmpeg при необходимости
        перекодирует в WAV; готовый WAV берём как есть).

  • ТРАНСКРИПТ (`.txt`/`.md`/`.vtt`/`.srt`) — деградированный фолбэк (R9),
        БЕЗ STT/диаризации:
          – есть метки спикеров → протокол с ролями (как есть);
          – меток нет → протокол с честной пометкой «без разбивки по
            спикерам» + подсказка прислать видео/аудио. Имена НЕ выдумываем.

ДВА ПУТИ ДОСТАВКИ (`--mode`, фаза 5 плана 2026-09-23-telemost-update-resilience):

  • `personal` — путь А методички, по умолчанию. Протокол идёт ЛИЧНО Илье на
    проверку (решение владельца A1/Q5), в групповой чат серии он кидает сам.
    finalize идёт с заглушённой доставкой (`ENABLE_PROTOCOL_DELIVERY=0`), затем
    протокол уходит PDF-документом штатной `deliver_protocol` с явным адресатом —
    личкой владельца (`TELEGRAM_NOTARIUS_CHAT_ID`, бот нотариуса). Она же пишет
    отметку `delivered` (чат + номер сообщения) в meta, которая кладётся в
    `<transcripts>/recovered/<sid>.meta.json`: слушатель ищет доставленные
    протоколы именно по этим отметкам, и без неё правка ответом на
    восстановленный протокол молча пропадала (баг 23.09.2026, корень Б).
  • `standard` — путь Б, «как обычно» для серийной встречи (слово владельца
    23.09.2026: «чтобы Михаил видел и чтобы открытые вопросы на следующей неделе
    отработали»). WAV и meta кладутся СРАЗУ в `_tmp/transcripts` — туда же, куда
    их кладёт бот, и это первая папка поиска доставленных протоколов у слушателя
    (`MEETING_NOTARY_DELIVERED_ROOTS`, корень А: meta в `/srv/.../state` делала
    правки невидимыми). finalize идёт с включённой доставкой: PDF в чат серии,
    слепок нумерации открытых вопросов, `delivered` — всё штатно.
  • `auto` — серия привязана к групповому чату → `standard`, иначе `personal`
    (так выбирает Альфред по «обработай запись»).

ЗАМОК. Финализация идёт под замком встречи `finalize-<sid>.lock` — тем же, что
берут сборщик и повторы (`lib/finalize_lock.py`). Без него сборщик раз в 5 минут
видел в очереди ту же встречу без `delivered` и запускал вторую финализацию: два
протокола в группу и двойная оплата расшифровки. Замок занят → код 4.

ЧУЖУЮ РАСШИФРОВКУ НЕ ПЕРЕЗАПИСЫВАЕМ. Протокол и расшифровка ложатся туда же, где
их ищут слушатель и перевыпуск (рабочая папка `TELEMOST_PROTOCOL_DIR`), и
копируются в долговечный архив. Если за эту дату у серии там уже есть файлы — код
2, пока не сказано `--force` (правило владельца: расшифровки не теряются).

R11 / Опасная тройка: текст реплик/транскрипта В ЛОГИ НЕ ПИШЕМ — только
метаданные (длина, число спикеров, длительность, размер). Файл записи
обрабатывается на месте.

Запуск:
    python3 tools/ingest-recording.py <файл> --series <slug> --date YYYY-MM-DD \
        [--mode personal|standard|auto] [--participants "Имя1,Имя2"] \
        [--kind auto|video|audio|transcript] [--output-dir DIR] \
        [--transcripts-dir DIR] [--force] [--no-deliver]

Коды выхода: 0 — готово и доставлено; 1 — сбой сборки или доставки (JSON с
`protocol_path`, если протокол собран; `error: protocol_not_generated` — расшифровка
есть, протокол моделью не составлен); 2 — ошибка входа или «файлы за эту дату уже
есть» (`target_exists`); 4 — запускать сейчас нельзя: эту встречу уже финализирует
кто-то другой (`finalize_in_progress`), её доводит очередь повторов бота
(`in_retry_queue`) или у серии за эту дату бот ещё собирает свою запись
(`series_date_busy`).

Итоговый JSON печатается ПОСЛЕДНЕЙ строкой вывода — после уборки рабочей папки:
методичка читает общий журнал (`2>&1`) по `tail -1`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
# `scripts/` — чтобы работали импорты `notary.lib.*` внутри доставки и реестра
# (на сервере его же даёт PYTHONPATH, тут — без зависимости от окружения).
for _p in (NOTARY_DIR.parent, NOTARY_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

log = logging.getLogger("ingest-recording")

# --- классификация входа по расширению ---------------------------------------
VIDEO_EXT = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".wmv",
    ".mpg", ".mpeg", ".ts", ".3gp", ".ogv",
}
AUDIO_EXT = {
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
    ".wma", ".amr", ".aiff", ".aif",
}
TRANSCRIPT_EXT = {".txt", ".md", ".markdown", ".vtt", ".srt"}

# WAV под STT: 16 кГц / моно / PCM s16le — стандарт whisper, принимают и внешние
# STT (AssemblyAI/Speechmatics). Совпадает с тем, что отдаёт бот.
WAV_SAMPLE_RATE = 16000
WAV_CHANNELS = 1

FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or "ffmpeg"

# --- пути доставки (фаза 5) --------------------------------------------------
MODE_PERSONAL = "personal"   # путь А: лично владельцу
MODE_STANDARD = "standard"   # путь Б: штатная доставка бота (чат серии)
MODE_AUTO = "auto"           # группа у серии → standard, иначе personal
MODES = (MODE_PERSONAL, MODE_STANDARD, MODE_AUTO)

#: Папка записей и meta бота на сервере — ровно `collector.VPS_TRANSCRIPTS` и первая
#: папка `MEETING_NOTARY_DELIVERED_ROOTS` слушателя (сторож — проба шва).
DEFAULT_TRANSCRIPTS_DIR = "/home/dev/meeting-notary/_tmp/transcripts"
TRANSCRIPTS_DIR_ENV = "MEETING_NOTARY_TRANSCRIPTS_DIR"
#: Подпапка для meta пути А. Без точки и подчёркивания в начале: такие папки поиск
#: доставленных протоколов пропускает (`feedback_worker._iter_meta_files`). Не в
#: самой папке записей: там сборщик считал бы её встречей в очереди.
PERSONAL_META_SUBDIR = "recovered"
#: `--output-dir` finalize по умолчанию (`finalize-meeting.py`, argparse).
FINALIZE_DEFAULT_OUTPUT_DIR = "/opt/meeting-notary/_tmp/protocols"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT = 2
EXIT_BUSY = 4

#: Очередь повторов finalize (`finalize-meeting._failed_dir`, `retry_failed`).
FAILED_DIR_ENV = "MEETING_NOTARY_FAILED_DIR"
FAILED_DEFAULT_DIR = "/srv/meeting-notary/_failed"


class HandedToRetry(RuntimeError):
    """Расшифровка сорвалась, и finalize переложил встречу в очередь повторов бота
    (`_failed/<sid>.*`): её доведёт `retry_failed.py` и сам пришлёт протокол.
    Второй прогон обёртки дал бы второй протокол и вторую оплату расшифровки."""


class ProtocolNotGenerated(RuntimeError):
    """Расшифровка на диске, а протокола нет: составить его моделью не удалось.

    В finalize это не фатально (rc=0): он сам пишет владельцу «протокол не
    сгенерировался (транскрипт сохранён)» и советует пересобрать командой
    «протокол <серия> <дата>». Сюда — чтобы итог обёртки сказал то же самое,
    а не «сбой, пришли другой формат» (живая проверка 26.09)."""

    def __init__(self, transcript_path: "str | os.PathLike"):
        self.transcript_path = Path(transcript_path)
        super().__init__(
            "протокол не составлен моделью; расшифровка сохранена: %s" % self.transcript_path
        )

# --- дословные пометки (REQ R7/R9) -------------------------------------------
# R7: автодиаризация на нештатном пути ненадёжна (ревью ОЖИД1) — без пометки Илья
# примет авто-роли за выверенные.
DIARIZATION_BANNER = (
    "> ⚠️ **Диаризация не выверена — сверьте роли.** Протокол восстановлен из "
    "присланной записи постфактум (бот встречу не записал). Разбивка по спикерам "
    "сделана автоматически и может ошибаться — сверьте, кто что сказал, прежде чем "
    "опираться на роли."
)
# R9 (меток нет): диаризацию из текста не сделать — честная пометка + подсказка.
NO_SPEAKERS_BANNER = (
    "> ⚠️ **Без разбивки по спикерам.** Протокол восстановлен из присланного "
    "транскрипта без меток говорящих — кто именно что сказал, из текста "
    "восстановить нельзя, поэтому реплики не привязаны к людям (имена не "
    "выдумывались).\n>\n"
    "> 💡 Будет точнее, если дашь видео/аудио встречи — соберу протокол с ролями."
)
# R9 (метки есть): роли взяты из самого транскрипта (не из автодиаризации) —
# лёгкая пометка о происхождении, без «не выверено».
RECOVERED_FROM_TRANSCRIPT_NOTE = (
    "> ♻️ Протокол восстановлен из присланного транскрипта (бот встречу не "
    "записал). Роли — по меткам говорящих из самого транскрипта."
)


# ---------------------------------------------------------------------------
# Чистые помощники (покрыты unittest'ом — без IO/сети/LLM)
# ---------------------------------------------------------------------------
def detect_input_kind(path: str | os.PathLike, forced: str | None = None) -> str:
    """Тип входа: 'video' | 'audio' | 'transcript'. `forced` (--kind) перебивает.

    По расширению. Незнакомое расширение → ошибка (явно, чтобы не гнать видео
    через STT как транскрипт и наоборот)."""
    if forced and forced != "auto":
        if forced not in ("video", "audio", "transcript"):
            raise ValueError(f"--kind: ожидалось video/audio/transcript/auto, получено {forced!r}")
        return forced
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in TRANSCRIPT_EXT:
        return "transcript"
    raise ValueError(
        f"Не распознан тип файла по расширению {ext!r}. Укажи явно --kind "
        f"video|audio|transcript."
    )


_SLUG_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё._-]+")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_date(date: str) -> str:
    """Проверяет YYYY-MM-DD (и что это реальная дата). Возвращает её же."""
    if not _DATE_RE.match(date or ""):
        raise ValueError(f"--date должна быть в формате YYYY-MM-DD, получено {date!r}")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"--date не похожа на реальную дату: {date!r} ({e})") from e
    return date


def safe_session_uid(series: str, date: str) -> str:
    """Синтетический sessionUid восстановления. Префикс `recovery-` отличает его
    от боевых (`tm-…`/`auto-…`). Без слешей/пробелов (basename-safe в wav_concat)."""
    base = f"recovery-{series}-{date}"
    return _SLUG_RE.sub("-", base).strip("-") or f"recovery-{date}"


def safe_series_segment(series: str) -> str:
    """Серия как ОДИН безопасный сегмент пути — без разделителей и parent-ссылок.

    Серия едет в ТРИ места (ростер, синтетическая meta для finalize, путь
    протокола обёртки) — санитизируем её ОДИН раз на входе, чтобы во всех местах
    был идентичный безопасный вид (иначе рассинхрон путей → «протокол не найден»).
    Легальные slug'и (латиница/кириллица/цифры/`._-`) проходят без изменений;
    `/`, пробелы, спецсимволы схлопываются в `-`. Пустой/`.`/`..` после чистки —
    недопустимый сегмент (traversal/пустой) → ValueError."""
    seg = _SLUG_RE.sub("-", (series or "").strip()).strip("-")
    if seg in ("", ".", ".."):
        raise ValueError(f"--series пуст или даёт недопустимый сегмент пути: {series!r}")
    return seg


def build_synthetic_meta(
    *, series: str, date: str, wav_path: str, participants: list[str], sid: str,
    keep_audio: bool = True, owner_chat_id: Optional[int] = None,
) -> dict:
    """Синтетическая meta для finalize — минимально достаточный набор полей,
    сличённый с боевой meta (runner.py BOT_CONFIG + collector): `sessionUid`,
    `files.wav`, `series`, `startTs` (→ date_part), `language`,
    `expectedParticipants`.

    КОНТРАКТ Ф3: НЕ нести failure-причину (`no_one_joined`/`deaf_*`/
    `evicted_failure`) — иначе гейт доставки подавил бы восстановленный протокол.
    Ставим нейтральный `endReason='manual_recovery'` (для finalize — незнакомая
    причина, при существенной записи гейт доставляет). Флаг `recovery=True`
    помечает синтетику в meta для аудита.

    `keep_audio=False` — только пути Б: там WAV — наша копия в папке записей, и
    после доставки её штатно убирает сборщик, как запись бота. На пути А WAV может
    быть файлом самого человека — его не трогаем никогда.

    `owner_chat_id` — путь А: адресат ЗАПИСАН в meta. Если сборка сорвётся на
    расшифровке, finalize переложит meta в `_failed/`, и повтор соберёт протокол
    уже сам — с этим полем он уйдёт владельцу, а не в группу серии мимо проверки
    (решение A1/Q5)."""
    meta = {
        "sessionUid": sid,
        "series": series,
        "startTs": f"{date}T00:00:00",
        "language": "ru",
        "files": {"wav": str(wav_path)},
        "expectedParticipants": list(participants or []),
        "participants": [],
        "endReason": "manual_recovery",
        "recovery": True,
        "keepAudio": bool(keep_audio),
    }
    if isinstance(owner_chat_id, int) and not isinstance(owner_chat_id, bool):
        meta["telegram_chat_id"] = owner_chat_id
    return meta


# Метка говорящего в начале строки: «Спикер N», «Speaker N», «SPEAKER_00» или
# Имя (1–4 капитализированных слова) — затем разделитель `:` / `—` / ` - `.
_TS_PREFIX_RE = re.compile(r"^\s*[>\-\*•]*\s*(?:\[[^\]]{1,20}\]|\(\d{1,2}:\d{2}(?::\d{2})?\))?\s*")
_SPEAKER_TAG_RE = re.compile(r"^(?:спикер|speaker|участник)[ _]*\d+", re.IGNORECASE)
_NAME_TAG_RE = re.compile(
    r"^[\*_]{0,2}"
    r"([A-ZА-ЯЁ][\wА-Яа-яЁё.\-]*(?:\s+[A-ZА-ЯЁ][\wА-Яа-яЁё.\-]*){0,3})"
    r"[\*_]{0,2}\s*[:：]"
)
_EXPLICIT_TAG_RE = re.compile(r"^[\*_\[]{0,2}(?:спикер|speaker|участник)[ _]*\d+", re.IGNORECASE)


def _line_speaker_label(line: str) -> tuple[str | None, bool]:
    """Из строки достаёт метку говорящего: (label|None, is_explicit_speaker_tag).

    Сначала срезает таймстемп/маркеры списка (`[00:01]`, `- `, `> `). Затем
    пробует «Спикер N» (явная метка) и «Имя:» (именная метка)."""
    core = _TS_PREFIX_RE.sub("", line)
    if not core:
        return None, False
    m_exp = _EXPLICIT_TAG_RE.match(core)
    if m_exp:
        return m_exp.group(0).strip("*_[ ").lower(), True
    m = _NAME_TAG_RE.match(core)
    if m:
        label = m.group(1).strip()
        # Отсекаем явные не-имена-обращения в начале реплики, которые случайно
        # выглядят как «Слово:» (одно короткое служебное слово — не метка).
        if _SPEAKER_TAG_RE.match(label):
            return label.lower(), True
        return label.lower(), False
    return None, False


def speaker_label_stats(text: str) -> dict:
    """Метаданные о метках говорящих в транскрипте — БЕЗ самого текста (R11).

    Возвращает {lines, nonempty_lines, labeled_lines, distinct_labels,
    explicit_tags, coverage}. coverage = labeled/nonempty."""
    lines = (text or "").splitlines()
    nonempty = [ln for ln in lines if ln.strip()]
    labeled = 0
    explicit = 0
    distinct: set[str] = set()
    for ln in nonempty:
        label, is_explicit = _line_speaker_label(ln)
        if label:
            labeled += 1
            distinct.add(label)
            if is_explicit:
                explicit += 1
    cov = (labeled / len(nonempty)) if nonempty else 0.0
    return {
        "lines": len(lines),
        "nonempty_lines": len(nonempty),
        "labeled_lines": labeled,
        "distinct_labels": len(distinct),
        "explicit_tags": explicit,
        "coverage": round(cov, 3),
    }


def has_speaker_labels(text: str) -> bool:
    """Есть ли в транскрипте метки говорящих (R9-развилка).

    True если: ≥2 разных меток при покрытии ≥15% непустых строк, ЛИБО ≥2 явных
    тега «Спикер N»/«Speaker N» (надёжный сигнал даже при низком покрытии).
    Цель — не принять прозу без говорящих за размеченный диалог и не выдумать
    роли там, где их нет."""
    st = speaker_label_stats(text)
    if st["explicit_tags"] >= 2:
        return True
    return st["distinct_labels"] >= 2 and st["coverage"] >= 0.15


def transcript_meta_stats(text: str) -> dict:
    """Метаданные транскрипта для логов (R11) — БЕЗ текста: символы, строки, метки."""
    sl = speaker_label_stats(text)
    return {
        "chars": len(text or ""),
        "lines": sl["lines"],
        "nonempty_lines": sl["nonempty_lines"],
        "distinct_labels": sl["distinct_labels"],
        "has_labels": has_speaker_labels(text),
    }


def inject_banner(protocol_text: str, banner: str) -> str:
    """Вставляет пометку-баннер в протокол.

    Если протокол начинается с заголовка (`# …` или `#протоколвстречи`), баннер
    идёт ПОСЛЕ первой строки-заголовка (чтобы шапка осталась первой). Иначе —
    в самое начало. Идемпотентно: если такой баннер уже есть — не дублируем."""
    text = protocol_text or ""
    banner = banner.rstrip()
    if banner in text:
        return text
    stripped = text.lstrip("\n")
    lines = stripped.split("\n", 1)
    first = lines[0] if lines else ""
    rest = lines[1] if len(lines) > 1 else ""
    if first.startswith("#"):
        body = rest.lstrip("\n")
        return f"{first}\n\n{banner}\n\n{body}" if body else f"{first}\n\n{banner}\n"
    return f"{banner}\n\n{stripped}" if stripped else f"{banner}\n"


# ---------------------------------------------------------------------------
# Реестр участников (тот же источник, что штатный путь — ростер серии)
# ---------------------------------------------------------------------------
def resolve_participants(series: str, explicit: str | None) -> list[str]:
    """Участники для синтетической meta. Явный `--participants` перебивает.
    Иначе — имена из ростера серии (`series_roster.get_roster` → оргструктура
    `*-context`). Нет ростера → []: finalize всё равно дозаполнит постоянный
    состав из памяти серии (`series_memory`), как на штатном пути.

    Best-effort: сбой импорта/чтения реестра не валит восстановление."""
    if explicit:
        return [p.strip() for p in explicit.split(",") if p.strip()]
    try:
        from lib import series_roster  # noqa: PLC0415
        roster = series_roster.get_roster(series)
        names = [e.get("name", "").strip() for e in (roster or []) if e.get("name")]
        return list(dict.fromkeys(n for n in names if n))
    except Exception as e:  # noqa: BLE001
        log.warning("[participants] ростер серии недоступен (не критично): %s", type(e).__name__)
        return []


# ---------------------------------------------------------------------------
# ffmpeg → WAV (R7, видео/аудио). Большие видео = долгая обработка: НЕ ставим
# короткий таймаут, который обрубит длинную запись (та же зона, что nginx-lb
# 7200s — см. CLAUDE.md). R11: логируем только размер/длительность, не контент.
# ---------------------------------------------------------------------------
def _wav_duration_sec(wav_path: str | os.PathLike) -> float | None:
    """Длительность WAV из заголовка (модуль wave, без внешних инструментов)."""
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or WAV_SAMPLE_RATE
            return round(frames / rate, 1) if rate else None
    except Exception:  # noqa: BLE001
        return None


def extract_wav(input_path: str | os.PathLike, out_wav: str | os.PathLike) -> dict:
    """ffmpeg: видео/аудио → WAV 16кГц/моно/s16le. Возвращает метаданные
    (duration_sec, size_bytes) — без контента (R11). Бросает RuntimeError на сбой.

    Таймаута НЕ ставим (None) — длинная запись не должна обрубаться."""
    in_size = None
    try:
        in_size = os.path.getsize(input_path)
    except OSError:
        pass
    cmd = [
        FFMPEG_BIN, "-y", "-nostdin",
        "-i", str(input_path),
        "-vn",                       # без видео-дорожки
        "-ac", str(WAV_CHANNELS),    # моно
        "-ar", str(WAV_SAMPLE_RATE), # 16 кГц
        "-c:a", "pcm_s16le",
        str(out_wav),
    ]
    log.info(
        "[ffmpeg] извлекаю WAV: input_size=%s байт → %s (16кГц/моно)",
        in_size if in_size is not None else "?", Path(out_wav).name,
    )
    # capture_output: stderr ffmpeg — это прогресс/диагностика медиа (без ПДн),
    # но в логи его не льём — наружу отдадим только класс ошибки + хвост при сбое.
    proc = subprocess.run(cmd, capture_output=True, text=True)  # без timeout — см. docstring
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError(
            f"ffmpeg rc={proc.returncode}: " + " | ".join(tail) if tail else f"ffmpeg rc={proc.returncode}"
        )
    out_size = None
    try:
        out_size = os.path.getsize(out_wav)
    except OSError:
        pass
    dur = _wav_duration_sec(out_wav)
    log.info(
        "[ffmpeg] готово: wav_size=%s байт, длительность≈%s сек",
        out_size if out_size is not None else "?", dur if dur is not None else "?",
    )
    if not out_size:
        raise RuntimeError("ffmpeg отработал, но WAV пустой/отсутствует")
    return {"duration_sec": dur, "size_bytes": out_size, "input_size_bytes": in_size}


def _is_wav(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() == ".wav"


# ---------------------------------------------------------------------------
# Вызов finalize (R7). Subprocess; доставка — по пути (А глушит, Б — штатная).
# ---------------------------------------------------------------------------
def parse_finalize_report(stdout: str) -> Optional[dict]:
    """Последний JSON-объект из stdout finalize (он печатает отчёт `indent=2`).

    В отчёте только метаданные (пути, счётчики, статус доставки) — текста
    встречи там нет. None — отчёта нет (finalize упал раньше)."""
    text = stdout or ""
    idx = text.rfind("\n{\n")
    start = idx + 1 if idx >= 0 else (text.find("{") if text.lstrip().startswith("{") else -1)
    if start < 0:
        return None
    try:
        obj = json.loads(text[start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def run_finalize(
    meta_path: str | os.PathLike, output_dir: str | os.PathLike, *, deliver: bool = False,
) -> tuple[int, Optional[dict]]:
    """Запускает `finalize-meeting.py <meta>` поверх существующего пайплайна:
    STT + диаризация + генерация протокола. Возвращает (rc, отчёт finalize).

    `deliver=False` (путь А и сухой прогон) — групповую доставку ГЛУШИМ
    (`ENABLE_PROTOCOL_DELIVERY=0`), восстановленный протокол шлём лично сами.
    `deliver=True` (путь Б) — окружение как есть: finalize доставит протокол тем
    же `deliver_protocol`, что у записи бота, и запишет `delivered` в ЭТУ meta.

    Таймаута НЕ ставим (None) — длинная запись финализируется долго (CPU CCX13 +
    nginx-lb 7200s). stderr наследуется (логи finalize R11-чисты сами по себе)."""
    finalize = str(NOTARY_DIR / "finalize-meeting.py")
    env = dict(os.environ)
    if not deliver:
        env["ENABLE_PROTOCOL_DELIVERY"] = "0"  # никакой групповой/авто-доставки из finalize
    cmd = [sys.executable, finalize, str(meta_path), "--output-dir", str(output_dir)]
    log.info(
        "[finalize] запуск (%s): output-dir=%s",
        "штатная доставка" if deliver else "доставка заглушена", output_dir,
    )
    # finalize печатает СВОЙ JSON-отчёт в stdout; забираем его, чтобы узнать статус
    # доставки, и копируем в НАШ stderr: stdout обёртки несёт ТОЛЬКО её финальный
    # JSON — контракт Ф5 (Альфред парсит stdout). Без timeout — длинную запись
    # нельзя обрубать (docstring).
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, text=True)
    out = proc.stdout or ""
    if out:
        sys.stderr.write(out if out.endswith("\n") else out + "\n")
    log.info("[finalize] rc=%s", proc.returncode)
    return proc.returncode, parse_finalize_report(out)


def protocol_path_for(output_dir: str | os.PathLike, series: str, date: str) -> Path:
    """Где finalize кладёт протокол: <output-dir>/<series>/<date>-protokol.md
    (см. finalize-meeting.py `_output_dir_for_meta` + `protocol_path`)."""
    return Path(output_dir) / series / f"{date}-protokol.md"


# ---------------------------------------------------------------------------
# Транскрипт-фолбэк (R9). Без STT/диаризации — только генерация протокола.
# ---------------------------------------------------------------------------
def generate_protocol_from_transcript(
    *, transcript_text: str, series: str, date: str, participants: list[str],
    out_protocol: str | os.PathLike,
) -> Path:
    """R9: собрать протокол из присланного транскрипта БЕЗ STT/диаризации.

    Пишет транскрипт во временный `<date>.md` и зовёт
    `regenerate_protocol_for_meeting` (тот же генератор, что и штатный путь,
    без ASR). Возвращает путь готового протокола.

    Имена НЕ выдумываются: метки спикеров (если есть) берутся из самого текста;
    их отсутствие caller пометит баннером `NO_SPEAKERS_BANNER`."""
    from lib.llm_postprocess import (  # noqa: PLC0415
        ProtocolGenerationError,
        regenerate_protocol_for_meeting,
    )

    out_protocol = Path(out_protocol)
    out_protocol.parent.mkdir(parents=True, exist_ok=True)
    transcript_path = out_protocol.parent / f"{date}.md"
    transcript_path.write_text(transcript_text, encoding="utf-8")
    meta = {
        "series": series,
        "date": date,
        "transcript_filename": transcript_path.name,
        "expectedParticipants": list(participants or []),
        "participants": [],
    }
    try:
        regenerate_protocol_for_meeting(
            transcript_path=transcript_path,
            protocol_path=out_protocol,
            meeting_meta=meta,
            meeting_sid=safe_session_uid(series, date),
        )
    except ProtocolGenerationError as e:
        # Текст уже лежит в рабочей папке — это та же ситуация, что у звука, когда
        # модель не составила протокол: итог скажет, как пересобрать без потерь.
        raise ProtocolNotGenerated(transcript_path) from e
    return out_protocol


# ---------------------------------------------------------------------------
# Пути, адресаты, meta (фаза 5: правки ответом на восстановленный протокол)
# ---------------------------------------------------------------------------
def transcripts_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Папка записей и meta бота: `--transcripts-dir` → env → серверный дефолт."""
    raw = (
        str(explicit or "").strip()
        or (os.environ.get(TRANSCRIPTS_DIR_ENV) or "").strip()
        or DEFAULT_TRANSCRIPTS_DIR
    )
    return Path(os.path.expanduser(raw))


def default_output_dir() -> Path:
    """Куда finalize кладёт расшифровку и протокол, если `--output-dir` не задан.

    РАБОЧАЯ папка — та же, что у записи бота (`TELEMOST_PROTOCOL_DIR` =
    `--output-dir` finalize). Из неё читают слушатель («протокол <серия> <дата>»),
    перевыпуск по правке и сборка следующей встречи серии (память, открытые
    вопросы). Раньше по умолчанию была временная папка прогона: расшифровка
    удалялась сразу после доставки — перевыпускать по правке было не из чего.
    """
    raw = (
        (os.environ.get("TELEMOST_PROTOCOL_DIR") or "").strip()
        or (os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR") or "").strip()
        or FINALIZE_DEFAULT_OUTPUT_DIR
    )
    return Path(os.path.expanduser(raw))


def owner_chat_id() -> Optional[int]:
    """Личка владельца с ботом нотариуса — `TELEGRAM_NOTARIUS_CHAT_ID`.

    Именно этот чат слушатель считает личкой владельца (`allowed_chat`), и в нём
    же слушает ответы: протокол, отправленный в другой чат или другим ботом,
    ответом не поправить."""
    raw = (os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID") or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def series_group_chat(series: str) -> Optional[int]:
    """Групповой чат серии из реестра (`watched.yaml`) или None.

    Тот же резолв, что у доставки протокола (`get_telegram_chat_id_for_series`):
    отрицательный id — группа. Сбой чтения реестра → None, то есть путь А —
    лично владельцу: в группу без уверенности не шлём."""
    try:
        from notary.cli.registry import (  # noqa: PLC0415
            get_telegram_chat_id_for_series, load_watched,
        )

        chat = get_telegram_chat_id_for_series(series, load_watched())
    except Exception as e:  # noqa: BLE001
        log.warning("[mode] реестр серий не прочитан (%s) — считаю, что группы нет", type(e).__name__)
        return None
    if isinstance(chat, int) and not isinstance(chat, bool) and chat < 0:
        return chat
    return None


def resolve_mode(requested: str, *, kind: str, series: str) -> str:
    """Путь доставки. `auto`: у серии есть групповой чат → standard, иначе personal.

    Транскрипт (R9) — только лично: путь Б — это finalize по звуку, а у текста
    звука нет. Явный `standard` для транскрипта — ошибка входа."""
    if requested not in MODES:
        raise ValueError(f"--mode: ожидалось {'/'.join(MODES)}, получено {requested!r}")
    if kind == "transcript":
        if requested == MODE_STANDARD:
            raise ValueError(
                "--mode standard — только для видео/аудио: штатный путь собирает протокол по звуку"
            )
        return MODE_PERSONAL
    if requested == MODE_AUTO:
        return MODE_STANDARD if series_group_chat(series) is not None else MODE_PERSONAL
    return requested


def standard_meta_path(tdir: str | os.PathLike, sid: str) -> Path:
    """Путь Б: meta — в очереди бота, рядом с записями (`<transcripts>/<sid>.meta.json`)."""
    return Path(tdir) / f"{sid}.meta.json"


def personal_meta_path(tdir: str | os.PathLike, sid: str) -> Path:
    """Путь А: meta — в подпапке, которую поиск протоколов видит, а сборщик нет."""
    return Path(tdir) / PERSONAL_META_SUBDIR / f"{sid}.meta.json"


def _read_json_file(path: str | os.PathLike) -> Optional[dict]:
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def write_meta_atomic(path: str | os.PathLike, meta: dict) -> None:
    """Атомарно (tmp + rename). Имя временного файла не кончается на `.meta.json`:
    сборщик и поиск протоколов не примут недописанный файл за встречу."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def merge_previous_delivery(meta: dict, previous: Optional[dict]) -> dict:
    """Повторный прогон той же встречи: отметки `delivered` прошлого прогона
    сохраняем. Иначе второй PDF ушёл бы в тот же чат (идемпотентность доставки
    живёт в них), а ответ на прежнее сообщение перестал бы находить протокол."""
    out = dict(meta)
    if isinstance(previous, dict) and previous.get("delivered"):
        out["delivered"] = previous["delivered"]
    return out


def target_collisions(output_dir: str | os.PathLike, series: str, date: str) -> list[Path]:
    """Файлы расшифровки и протокола этой даты серии, которые перезаписал бы прогон:
    в рабочей папке и их долговечные копии в архиве."""
    work = [Path(output_dir) / series / f"{date}.md", protocol_path_for(output_dir, series, date)]
    found = [p for p in work if p.exists()]
    try:
        from lib import paths as paths_lib  # noqa: PLC0415

        for p in work:
            dst = paths_lib.archive_dest_for(p)
            if dst is not None and dst.exists() and dst not in found:
                found.append(dst)
    except Exception as e:  # noqa: BLE001
        log.warning("[preflight] архив не проверен (%s)", type(e).__name__)
    return found


def only_transcript_exists(output_dir: str | os.PathLike, series: str, date: str) -> bool:
    """За дату у серии есть расшифровка, а протокола нет — модель его не составила.

    Тогда перезапуск с `--force` заново оплатит расшифровку, а пересобрать протокол
    можно по готовой: командой «протокол <серия> <дата>» боту-нотариусу."""
    return (Path(output_dir) / series / f"{date}.md").is_file() and not protocol_path_for(
        output_dir, series, date).is_file()


def backup_before_force(paths: list[Path]) -> list[str]:
    """Копии файлов, которые `--force` перезапишет, — в `_versions/before-recovery-<время>/`.

    Правило владельца: расшифровки встреч не теряются без явной команды. «Да» на
    вопрос «протокол за эту дату уже есть — заменить?» — согласие заменить протокол,
    а не стереть расшифровку: за этой датой может лежать частичная запись бота с
    тем, чего нет в присланной записи. Копируем (не переносим) рядом с каждым
    файлом — и в рабочей папке, и в архиве. Подпапку с `_` в начале пропускают поиск
    протоколов, индекс встреч и память серии. Не скопировалось — не перезаписываем:
    исключение останавливает прогон."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    saved: list[str] = []
    for src in paths:
        src = Path(src)
        if not src.is_file():
            continue
        dst = src.parent / "_versions" / f"before-recovery-{stamp}" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        saved.append(str(dst))
    if saved:
        log.info("[preflight] прежние файлы за дату сохранены перед --force: %d", len(saved))
    return saved


def failed_dir() -> Path:
    """Очередь повторов — ровно `finalize-meeting._failed_dir()`."""
    return Path(os.environ.get(FAILED_DIR_ENV) or FAILED_DEFAULT_DIR)


def retry_pending(sid: str, *, now: Optional[datetime] = None) -> bool:
    """Встречу ещё доведёт очередь повторов бота (`retry_failed.py`)?

    Её заводит finalize, когда сорвалась расшифровка: копия записи и meta — в
    `_failed/`, повторы по расписанию в течение суток, протокол потом уходит сам
    (на пути А — владельцу: адресат записан в meta). Пока так — второй прогон
    обёртки дал бы второй протокол и вторую оплату расшифровки. Повтор, которого
    уже не будет (отклонён сервисом, попытки или сутки кончились), не мешает."""
    state = _read_json_file(failed_dir() / f"{sid}.retry-state.json")
    if state is None:
        return False
    if state.get("rejected"):
        return False
    if state.get("blocked_by_killswitch"):
        return True
    try:
        import retry_failed as rf  # noqa: PLC0415

        if int(state.get("attempts") or 0) >= len(rf.SCHEDULE_MIN):
            return False
        first = rf._parse_iso(str(state.get("first_failed_at") or ""))
        limit = rf.MAX_AGE_S
    except Exception:  # noqa: BLE001 — не разобрали расписание: считаем, что повтор будет
        return True
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    return (moment - first).total_seconds() <= limit


def _queue_delivery_done(meta: dict) -> bool:
    """Ровно `collector._delivery_done`: сборщик эту meta больше не финализирует.

    Копия, а не импорт сборщика в обёртку (сторож паритета — в пробах)."""
    raw = meta.get("delivered")
    if isinstance(raw, dict):
        records = [raw]
    elif isinstance(raw, list):
        records = [r for r in raw if isinstance(r, dict)]
    else:
        return False
    return any((r.get("message_ids") or []) and r.get("decision") != "partial-failure"
               for r in records)


def pending_same_date_meetings(
    tdir: str | os.PathLike, series: str, date: str, own_sid: str,
) -> list[str]:
    """Другие встречи этой серии за эту дату, которые бот ещё доведёт сам.

    Замок `finalize-<sid>.lock` у каждой встречи свой, а расшифровка и протокол
    одни на серию и дату (`<серия>/<дата>.md`, `finalize._output_dir_for_meta`).
    Частичную запись бота (его выбросило посреди встречи) сборщик финализирует
    ~25 минут, а сорванную расшифровку доводит очередь повторов; прогон обёртки в
    это окно писал бы в те же файлы, а в группу ушло бы два протокола. Смотрим
    очередь сборщика (meta без `delivered`) и очередь повторов (живой повтор)."""
    out: list[str] = []
    for folder, in_retry in ((Path(tdir), False), (failed_dir(), True)):
        if not folder.is_dir():
            continue
        for mp in sorted(folder.glob("*.meta.json")):
            other = mp.name[: -len(".meta.json")]
            if other == own_sid or other in out:
                continue
            meta = _read_json_file(mp)
            if not meta or str(meta.get("series") or "").strip() != series:
                continue
            if str(meta.get("startTs") or "")[:10] != date or _queue_delivery_done(meta):
                continue
            if in_retry and not retry_pending(other):
                continue
            out.append(other)
    return out


def _replies_flag(ok: bool, meta_path: str | os.PathLike) -> Optional[bool]:
    """Поле `replies_enabled`: False — доставки нет; иначе `replies_reachable`
    (None — «не знаю»: папок поиска слушателя в окружении нет)."""
    return replies_reachable(meta_path) if ok else False


def replies_reachable(meta_path: str | os.PathLike) -> Optional[bool]:
    """Найдёт ли слушатель эту meta по ответу на протокол.

    Проверяем то же, что делает поиск (`feedback_worker.find_delivered_protocol`):
    meta лежит под одной из папок `MEETING_NOTARY_DELIVERED_ROOTS` и не внутри
    подпапки с `.`/`_` в начале — такие поиск пропускает.

    None — «не знаю»: в окружении обёртки нет `MEETING_NOTARY_DELIVERED_ROOTS`.
    Папки поиска знает окружение СЛУШАТЕЛЯ (его юнит), а обёртку запускают из
    ручной оболочки; в умолчаниях `_delivered_roots()` нет папки записей, и без этой
    оговорки итог сказал бы владельцу «правка ответом не сработает» про протокол,
    который слушатель найдёт."""
    if not (os.environ.get("MEETING_NOTARY_DELIVERED_ROOTS") or "").strip():
        log.info("[replies] папки поиска протоколов в окружении не заданы — проверить не могу")
        return None
    try:
        from lib import feedback_worker  # noqa: PLC0415

        roots = feedback_worker._delivered_roots()
    except Exception as e:  # noqa: BLE001
        log.warning("[replies] папки поиска протоколов не прочитаны (%s)", type(e).__name__)
        return False
    mp = Path(os.path.abspath(os.path.expanduser(str(meta_path))))
    for root in roots:
        try:
            rel = mp.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith((".", "_")) for part in rel.parts[:-1]):
            continue
        return True
    return False


def sync_to_archive(
    protocol_path: str | os.PathLike, transcript_path: str | os.PathLike, date: str,
) -> list[str]:
    """Долговечная копия расшифровки, протокола и памяти серии — как у сборщика и
    перевыпуска (`paths.archive_dest_for`: тот же относительный путь под архивом;
    архив не разведён с рабочей папкой → копировать нечего). Отсюда её забирает
    зеркало мака. Дописывает, не удаляет; содержимое не читаем."""
    synced: list[str] = []
    try:
        from lib import paths as paths_lib  # noqa: PLC0415
        from lib.series_memory import MEMORY_FILE_SUFFIX  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        log.warning("[archive] модуль путей не загрузился (%s) — в архив не копирую", type(e).__name__)
        return synced
    memory_path = Path(transcript_path).parent / f"{date}{MEMORY_FILE_SUFFIX}"
    for src in (Path(protocol_path), Path(transcript_path), memory_path):
        if not src.is_file():
            continue
        dst = paths_lib.archive_dest_for(src)
        if dst is None:
            continue
        tmp = dst.with_suffix(dst.suffix + ".part")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            synced.append(str(dst))
        except OSError as e:
            log.warning("[archive] %s не скопирован (%s)", src.name, type(e).__name__)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    if synced:
        log.info("[archive] долговечная копия: %d файл(а)", len(synced))
    return synced


_DELIVERED_OK = frozenset({"sent", "skipped"})


def delivery_summary(result: Optional[dict], *, owner: Optional[int] = None) -> dict:
    """Итог доставки для stdout: статус, вид чата, номера сообщений.

    Номер чата наружу не отдаём: stdout читает Альфред и пересказывает владельцу,
    а сам чат виден в meta на сервере (`meta_path`)."""
    r = result if isinstance(result, dict) else {}
    chat = r.get("chat_id")
    chat_kind = None
    if isinstance(chat, int) and not isinstance(chat, bool) and chat != 0:
        if chat < 0:
            chat_kind = "group"
        elif owner is not None and chat == owner:
            chat_kind = "owner"
        else:
            chat_kind = "private"
    mids: list[int] = []
    for m in r.get("message_ids") or []:
        try:
            mids.append(int(m))
        except (TypeError, ValueError):
            continue
    return {"status": str(r.get("status") or "not-run"), "chat_kind": chat_kind, "message_ids": mids}


def has_real_delivery(meta: Optional[dict]) -> bool:
    """В meta есть настоящая отметка доставки: чат и номер сообщения (не заглушка
    `chat_id=0`, которой finalize помечает подавленную пустую запись)."""
    raw = (meta or {}).get("delivered")
    if isinstance(raw, dict):
        recs = [raw]
    elif isinstance(raw, list):
        recs = [r for r in raw if isinstance(r, dict)]
    else:
        recs = []
    for r in recs:
        mids = [m for m in (r.get("message_ids") or []) if m]
        if r.get("chat_id") not in (None, 0) and mids and r.get("decision") != "partial-failure":
            return True
    return False


def _delivered_to_chat(meta: Optional[dict], chat: Optional[int]) -> bool:
    """В meta уже есть доставка в этот чат так, как её видит `deliver_protocol`
    (`_find_delivery_for_chat`: последняя запись чата с непустыми номерами) —
    значит, повторная доставка будет пропущена."""
    if chat is None:
        return False
    raw = (meta or {}).get("delivered")
    records = [raw] if isinstance(raw, dict) else (
        [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else [])
    for rec in reversed(records):
        if rec.get("chat_id") == chat:
            return bool(rec.get("message_ids"))
    return False


# ---------------------------------------------------------------------------
# Личная доставка Илье (A1/Q5)
# ---------------------------------------------------------------------------
def _notice_text(series: str, date: str, degraded_note: Optional[str]) -> str:
    return (
        f"♻️ Восстановил протокол встречи «{series}» {date} из присланной записи.\n"
        + (degraded_note + "\n" if degraded_note else "")
        + "Протокол — PDF следующим сообщением. В групповой чат серии НЕ отправлял — "
        "проверь и перешли сам, если ок. Правку можно прислать ответом на протокол."
    )


def deliver_personal_tracked(
    *,
    protocol_path: str | os.PathLike,
    transcript_path: str | os.PathLike,
    meta: dict,
    meta_path: str | os.PathLike,
    series: str,
    date: str,
    sid: str,
    degraded_note: Optional[str] = None,
) -> Optional[dict]:
    """Путь А: протокол ЛИЧНО владельцу — штатной `deliver_protocol` с явным
    адресатом (его личка с ботом нотариуса). Она собирает PDF, шлёт ботом,
    которого слушает `meetings_listener`, и пишет в `meta_path` отметку
    `delivered` (чат + номер сообщения) и пути расшифровки/протокола — без них
    правка ответом на протокол не находила встречу.

    В групповой чат серии НЕ шлём (решение владельца A1/Q5): адресат передан
    явно, маршрут серии не участвует. None — нет лички владельца или токена бота
    (тогда вызывающий шлёт по-старому, без правок ответом).
    Возвращает {"notice": bool, "result": ответ deliver_protocol}."""
    owner = owner_chat_id()
    token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    if owner is None or not token:
        log.warning("[deliver] нет лички владельца или токена бота нотариуса — шлю по-старому")
        return None
    notice_ok = False
    if _delivered_to_chat(meta, owner):
        # Повторный прогон той же встречи: `deliver_protocol` пропустит доставку
        # (идемпотентность по `delivered`), и «PDF следующим сообщением» было бы
        # неправдой — PDF не придёт.
        log.info("[deliver] владельцу этот протокол уже доставлялся — уведомление не шлю")
    else:
        try:
            from lib import notify  # noqa: PLC0415

            notice_ok = bool(notify.push_via_notarius(_notice_text(series, date, degraded_note)))
        except Exception as e:  # noqa: BLE001
            log.warning("[deliver] личное уведомление не ушло (не критично): %s", type(e).__name__)
    meeting_meta = dict(meta)
    meeting_meta.update({
        "series": series,
        "date": date,
        "sessionUid": sid,
        "transcript_path": str(transcript_path),
        "protocol_path": str(protocol_path),
    })
    try:
        from lib.llm_postprocess import deliver_protocol  # noqa: PLC0415

        result = deliver_protocol(
            meeting_meta=meeting_meta,
            protocol_text=Path(protocol_path).read_text(encoding="utf-8"),
            meta_json_path=Path(meta_path),
            target_chat_id=owner,
            meeting_sid=sid,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[deliver] доставка упала: %s", type(e).__name__)
        result = {"status": "error", "error": type(e).__name__}
    log.info("[deliver] лично владельцу: статус %s", (result or {}).get("status"))
    return {"notice": notice_ok, "result": result if isinstance(result, dict) else {}}


def deliver_personally(
    *, protocol_path: str | os.PathLike, series: str, date: str, degraded_note: str | None = None
) -> dict:
    """Запасной путь (нет лички владельца или токена бота в окружении): протокол
    `.md`-документом через tg-send `--bot notarius`, как было до фазы 5. Номер
    сообщения отсюда не узнать — правка ответом на такой протокол не сработает,
    итоговый JSON честно скажет `replies_enabled: false`.

    R11: в лог — только факт/статус, без текста протокола. Best-effort: сбой
    доставки не валит сборку (протокол уже на диске)."""
    protocol_path = Path(protocol_path)
    notice = (
        f"♻️ Восстановил протокол встречи «{series}» {date} из присланной записи.\n"
        + (degraded_note + "\n" if degraded_note else "")
        + "Протокол — во вложении ниже. В групповой чат серии НЕ отправлял — "
        "проверь и перешли сам, если ок."
    )
    result = {"notice": False, "document": False}
    try:
        from lib import notify  # noqa: PLC0415
        result["notice"] = bool(notify.push_via_notarius(notice))
    except Exception as e:  # noqa: BLE001
        log.warning("[deliver] личное уведомление не ушло (не критично): %s", type(e).__name__)

    tg_send = Path(os.path.expanduser(os.environ.get("TG_SEND_BIN") or "~/.local/bin/tg-send"))
    if not tg_send.exists():
        log.warning("[deliver] tg-send не найден (%s) — документ не отправлен", tg_send)
        return result
    caption = f"Протокол: {series} {date} (восстановлен из записи)"
    try:
        proc = subprocess.run(
            [str(tg_send), "--bot", "notarius", "--document", str(protocol_path), caption],
            capture_output=True, text=True, timeout=120,
        )
        result["document"] = proc.returncode == 0
        if proc.returncode != 0:
            log.warning("[deliver] tg-send документ rc=%s", proc.returncode)
        else:
            log.info("[deliver] протокол доставлен лично Илье (документ)")
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("[deliver] отправка документа упала (не критично): %s", type(e).__name__)
    return result


# ---------------------------------------------------------------------------
# Оркестрация
# ---------------------------------------------------------------------------
def _prepare_wav(
    src: Path, kind: str, dest: Path, *, scratch: Optional[Path] = None,
) -> tuple[Path, bool]:
    """WAV для расшифровки — ВСЕГДА своя копия в `dest`. Возвращает (путь, создали ли).

    Путь А кладёт копию в личную папку прогона, путь Б — в очередь бота (её после
    доставки штатно убирает сборщик). Файл человека finalize не отдаётся никогда:
    при сбое расшифровки finalize переносит запись в `_failed/` и УДАЛЯЕТ исходник
    (`_stash_into_failed`), а после удачного повтора чистит и `_failed/` — файла
    человека не осталось бы нигде (правило владельца: записи сами не удаляются).

    Пишем во временное имя и подменяем атомарно: сорвётся конвертация — копия
    прошлого прогона (по ней, может быть, повторяет сборщик) останется целой. Имя с
    точкой в начале: его не видит уборщик записей (`*.wav`) и сборщик.

    🔴 Очередь бота — ОБЩАЯ папка: на запись в неё смонтирован и контейнер бота
    (root, браузер на чужих страницах). Предсказуемое временное имя там —
    приглашение подложить ярлык: копия или `ffmpeg -y` пошли бы по нему и
    перезаписали чужой файл хозяина. Поэтому конвертация идёт в личной папке
    `scratch`, временный файл заводит `mkstemp` (новое случайное имя, O_EXCL — по
    ярлыку не пойдёт), пишем в свой дескриптор, подменяем `os.replace` (он меняет
    саму ссылку, а не её цель)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src.resolve() == dest.resolve():
            log.info("[media] вход уже лежит на месте копии — беру его как есть")
            return dest, False
    except OSError:
        pass
    work = Path(scratch) if scratch is not None else dest.parent
    converted: Optional[Path] = None
    try:
        if kind == "audio" and _is_wav(src):
            source = src
            log.info("[media] вход уже WAV — своя копия: %s", dest.name)
        else:
            converted = work / f".{dest.stem}.convert.wav"
            extract_wav(src, converted)
            source = converted
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest.stem}.", suffix=".part.wav", dir=str(dest.parent))
        part = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, open(source, "rb") as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(part, dest)
        except BaseException:
            try:
                part.unlink()
            except FileNotFoundError:
                pass
            raise
    finally:
        if converted is not None:
            try:
                converted.unlink()
            except FileNotFoundError:
                pass
    return dest, True


def _build_personal_media(args, kind: str, workdir: Path, output_dir: Path, sid: str) -> tuple[Path, Path, dict]:
    """Путь А и сухой прогон: видео/аудио → WAV → meta во временной папке →
    finalize БЕЗ доставки → протокол + баннер R7. Возвращает (протокол,
    расшифровка, meta после finalize — в ней могут появиться id расшифровки)."""
    wav_path, _ = _prepare_wav(Path(args.input), kind, workdir / "recording.wav")
    participants = resolve_participants(args.series, args.participants)
    meta = build_synthetic_meta(
        series=args.series, date=args.date, wav_path=str(wav_path),
        participants=participants, sid=sid, keep_audio=True, owner_chat_id=owner_chat_id(),
    )
    meta_path = workdir / f"{sid}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(
        "[media] синтетическая meta: series=%s date=%s participants=%d sid=%s",
        args.series, args.date, len(participants), sid,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    rc, _report = run_finalize(meta_path, output_dir, deliver=False)
    if rc != 0:
        if retry_pending(sid):
            # Сорвалась расшифровка: finalize переложил встречу в очередь повторов,
            # повтор соберёт и пришлёт протокол владельцу сам (адресат — в meta).
            raise HandedToRetry(f"finalize вернул rc={rc}: встреча в очереди повторов")
        raise RuntimeError(f"finalize вернул rc={rc} — протокол не собран (см. логи finalize)")

    protocol_path = protocol_path_for(output_dir, args.series, args.date)
    if not protocol_path.is_file():
        transcript_path = Path(output_dir) / args.series / f"{args.date}.md"
        if transcript_path.is_file():
            raise ProtocolNotGenerated(transcript_path)
        raise RuntimeError(f"finalize отработал (rc=0), но протокол не найден: {protocol_path}")

    # R7: пометка «диаризация не выверена — сверьте роли».
    text = protocol_path.read_text(encoding="utf-8")
    protocol_path.write_text(inject_banner(text, DIARIZATION_BANNER), encoding="utf-8")
    log.info("[media] протокол собран, пометка «роли не выверены» добавлена")
    after = _read_json_file(meta_path) or meta
    return protocol_path, Path(output_dir) / args.series / f"{args.date}.md", after


def _process_transcript(args, workdir: Path, output_dir: Optional[Path] = None) -> tuple[Path, str | None]:
    """Транскрипт → протокол (R9). Возвращает (protocol_path, degraded_note)."""
    text = Path(args.input).read_text(encoding="utf-8", errors="replace")
    stats = transcript_meta_stats(text)
    # R11: только метаданные, без текста реплик.
    log.info(
        "[transcript] получен: chars=%d nonempty_lines=%d distinct_labels=%d has_labels=%s",
        stats["chars"], stats["nonempty_lines"], stats["distinct_labels"], stats["has_labels"],
    )
    if not text.strip():
        raise RuntimeError("транскрипт пустой — нечего собирать")

    participants = resolve_participants(args.series, args.participants)
    if output_dir is None:
        output_dir = Path(args.output_dir) if args.output_dir else (workdir / "protocols")
    protocol_path = Path(output_dir) / args.series / f"{args.date}-protokol.md"
    generate_protocol_from_transcript(
        transcript_text=text, series=args.series, date=args.date,
        participants=participants, out_protocol=protocol_path,
    )

    proto_text = protocol_path.read_text(encoding="utf-8")
    if stats["has_labels"]:
        # Метки есть → роли из транскрипта. Лёгкая пометка происхождения.
        protocol_path.write_text(inject_banner(proto_text, RECOVERED_FROM_TRANSCRIPT_NOTE), encoding="utf-8")
        log.info("[transcript] метки спикеров есть → протокол с ролями")
        return protocol_path, None
    # Меток нет → честная пометка «без разбивки» + подсказка прислать видео.
    protocol_path.write_text(inject_banner(proto_text, NO_SPEAKERS_BANNER), encoding="utf-8")
    log.info("[transcript] меток нет → протокол без разбивки по спикерам (+ подсказка)")
    return protocol_path, "⚠️ Без разбивки по спикерам — пришли видео/аудио, соберу с ролями."


def _run_standard(args, kind: str, output_dir: Path, sid: str, tdir: Path, workdir: Path) -> dict:
    """Путь Б: WAV и meta — в очередь бота, finalize со штатной доставкой.

    Всё, что дальше делает бот, — его обычный механизм: PDF в чат серии, слепок
    нумерации открытых вопросов, отметка `delivered` в ЭТОЙ meta. Сорвалась
    расшифровка — finalize сам переложит встречу в очередь повторов; сорвалась
    доставка или составление протокола моделью — meta без `delivered` осталась в
    очереди, сборщик повторит сам (WAV для этого лежит рядом, id расшифровки — в
    meta, второй оплаты нет). Возвращает поля итогового JSON."""
    meta_path = standard_meta_path(tdir, sid)
    staged = Path(tdir) / f"{sid}.wav"
    previous = _read_json_file(meta_path)
    # Недописанную копию `_prepare_wav` убирает сам; файл человека не трогается.
    wav_path, _created = _prepare_wav(Path(args.input), kind, staged, scratch=workdir)
    participants = resolve_participants(args.series, args.participants)
    meta = merge_previous_delivery(
        build_synthetic_meta(
            series=args.series, date=args.date, wav_path=str(wav_path),
            participants=participants, sid=sid, keep_audio=False,
        ),
        previous,
    )
    write_meta_atomic(meta_path, meta)
    log.info(
        "[standard] meta в очереди бота: %s (series=%s date=%s participants=%d)",
        meta_path.name, args.series, args.date, len(participants),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    rc, report = run_finalize(meta_path, output_dir, deliver=True)
    after = _read_json_file(meta_path)
    delivery = (report or {}).get("delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    summary = delivery_summary(delivery, owner=owner_chat_id())
    protocol_path = protocol_path_for(output_dir, args.series, args.date)
    transcript_path = Path(output_dir) / args.series / f"{args.date}.md"
    ok = rc == 0 and summary["status"] in _DELIVERED_OK and has_real_delivery(after)
    note = None
    error = None
    if rc != 0 and after is None:
        note = ("расшифровка сорвалась — встреча переложена в очередь повторов бота, "
                "он доведёт её и доставит сам")
    elif rc != 0:
        note = "finalize упал — встреча осталась в очереди сборщика, он повторит сам"
    elif not protocol_path.is_file() and transcript_path.is_file():
        # Живая проверка 26.09: расшифровка есть, а модель протокол не составила.
        error = "protocol_not_generated"
        note = ("протокол не собрался: составить его моделью не удалось (бот отдельно "
                "написал об этом владельцу), расшифровка сохранена — встреча осталась в "
                "очереди сборщика: он повторит сборку и доставку сам, расшифровку возьмёт "
                "готовую")
    elif summary["status"] == "skipped-failed-recording":
        note = ("запись оказалась пустой или сорванной — такие протоколы бот не шлёт, "
                "владельцу он написал сам")
    elif not ok:
        note = (f"доставка не прошла (статус {summary['status']}) — встреча осталась в "
                "очереди сборщика, он повторит сам")
    # Без протокола в архив не копируем: встречу доведёт сборщик и сам положит
    # расшифровку и протокол в архив (дата — из имени `recovery-…-<дата>`); наша
    # копия заняла бы там `<дата>.md`, и его досборка легла бы дублем `<дата>-<sid>.md`.
    archive = sync_to_archive(protocol_path, transcript_path, args.date) if (
        rc == 0 and protocol_path.is_file()) else []
    if ok:
        log.info("[standard] протокол доставлен штатно: %s", summary["status"])
    else:
        log.error("[standard] протокол НЕ доставлен: rc=%s статус=%s", rc, summary["status"])
    return {
        "ok": ok,
        "protocol_path": str(protocol_path) if protocol_path.is_file() else None,
        **({"transcript_path": str(transcript_path)} if transcript_path.is_file() else {}),
        "delivery": summary,
        "meta_path": str(meta_path),
        "replies_enabled": _replies_flag(ok, meta_path),
        "archive_synced": archive,
        "finalize_rc": rc,
        **({"error": error} if error else {}),
        **({"note": note} if note else {}),
    }


def _run_personal(args, kind: str, workdir: Path, output_dir: Path, sid: str, tdir: Path) -> dict:
    """Путь А: собрать без доставки, затем лично владельцу с отметкой `delivered`."""
    degraded_note: Optional[str] = None
    if kind in ("video", "audio"):
        protocol_path, transcript_path, meta = _build_personal_media(args, kind, workdir, output_dir, sid)
    else:
        protocol_path, degraded_note = _process_transcript(args, workdir, output_dir)
        transcript_path = Path(output_dir) / args.series / f"{args.date}.md"
        meta = build_synthetic_meta(
            series=args.series, date=args.date, wav_path="",
            participants=resolve_participants(args.series, args.participants),
            sid=sid, keep_audio=True, owner_chat_id=owner_chat_id(),
        )
        meta.pop("files", None)
    log.info("[ingest] протокол готов: %s", protocol_path)

    final_meta_path = personal_meta_path(tdir, sid)
    final_meta = merge_previous_delivery(meta, _read_json_file(final_meta_path))
    final_meta.update({
        "date": args.date,
        "transcript_path": str(transcript_path),
        "transcript_filename": Path(transcript_path).name,
        "protocol_path": str(protocol_path),
    })
    tracked = None
    try:
        write_meta_atomic(final_meta_path, final_meta)
    except OSError as e:
        log.warning("[deliver] meta для ответов не записана (%s) — шлю без неё", type(e).__name__)
    else:
        tracked = deliver_personal_tracked(
            protocol_path=protocol_path, transcript_path=transcript_path, meta=final_meta,
            meta_path=final_meta_path, series=args.series, date=args.date, sid=sid,
            degraded_note=degraded_note,
        )
    if tracked is None:
        legacy = deliver_personally(
            protocol_path=protocol_path, series=args.series, date=args.date,
            degraded_note=degraded_note,
        )
        delivered_ok = bool(legacy.get("document"))
        out = {
            "ok": delivered_ok,
            "delivered_personally": legacy,
            "delivery": {"status": "sent" if delivered_ok else "error", "chat_kind": "owner",
                         "message_ids": []},
            "meta_path": None,
            "replies_enabled": False,
        }
    else:
        result = tracked["result"]
        summary = delivery_summary(result, owner=owner_chat_id())
        delivered_ok = summary["status"] in _DELIVERED_OK
        out = {
            "ok": delivered_ok,
            "delivered_personally": {"notice": tracked["notice"], "document": delivered_ok},
            "delivery": summary,
            "meta_path": str(final_meta_path),
            "replies_enabled": _replies_flag(delivered_ok, final_meta_path),
        }
    out["protocol_path"] = str(protocol_path)
    # Расшифровка — исходные данные встречи: в долговечный архив в любом случае.
    out["archive_synced"] = sync_to_archive(protocol_path, transcript_path, args.date)
    if not delivered_ok:
        log.error(
            "[ingest] протокол СОБРАН, но документ не доставлен Илье — копия на диске: %s",
            protocol_path,
        )
    return out


#: Итог прогона ждёт здесь и печатается ПОСЛЕДНИМ — после уборки рабочей папки.
_PENDING_OUTPUT: list[dict] = []


def _emit(payload: dict) -> None:
    """Итоговый JSON — одной строкой в stdout (его парсит Альфред).

    Печатается не сразу, а в самом конце `main`: уборка рабочей папки пишет строку
    в журнал уже ПОСЛЕ ответа, и в общем журнале (`2>&1`, как запускает методичка:
    итог — `tail -1`) последней строкой оказывалась она, стоило выводу быть
    небуферизованным (`PYTHONUNBUFFERED`, `python -u`)."""
    _PENDING_OUTPUT[:] = [payload]


def _flush_emit() -> None:
    if not _PENDING_OUTPUT:
        return
    payload = _PENDING_OUTPUT.pop()
    _PENDING_OUTPUT.clear()
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    """Точка входа: прогон + итоговый JSON последней строкой вывода."""
    _PENDING_OUTPUT.clear()
    try:
        return _main(argv)
    finally:
        _flush_emit()


#: Что сказать, когда встречу доводит очередь повторов бота (пути А и Б).
RETRY_QUEUE_NOTE = (
    "расшифровка этой встречи сорвалась, и её доводит очередь повторов бота: он сам "
    "соберёт протокол и пришлёт его (на пути А — лично владельцу, без пометки «роли не "
    "выверены»). Запускать заново не нужно — будет второй протокол и вторая оплата "
    "расшифровки"
)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Восстановление протокола встречи из ручной записи (видео/аудио/транскрипт).",
    )
    parser.add_argument("input", help="Путь к файлу записи: видео, аудио или транскрипт")
    parser.add_argument("--series", required=True, help="Slug серии (папка в ~/Projects/me/встречи)")
    parser.add_argument("--date", required=True, help="Дата встречи YYYY-MM-DD")
    parser.add_argument("--participants", default=None,
                        help="Участники через запятую (перебивает ростер серии)")
    parser.add_argument("--kind", default="auto", choices=["auto", "video", "audio", "transcript"],
                        help="Тип входа (default auto — по расширению)")
    parser.add_argument("--mode", default=MODE_PERSONAL, choices=list(MODES),
                        help="personal — лично владельцу (путь А, по умолчанию); standard — "
                             "штатная доставка бота в чат серии (путь Б); auto — группа у серии → "
                             "standard, иначе personal")
    parser.add_argument("--output-dir", default=None,
                        help="Куда положить расшифровку и протокол (по умолчанию — рабочая папка "
                             "протоколов бота, при --no-deliver — временная папка прогона)")
    parser.add_argument("--transcripts-dir", default=None,
                        help="Папка записей и meta бота (по умолчанию $MEETING_NOTARY_TRANSCRIPTS_DIR "
                             "или серверная _tmp/transcripts)")
    parser.add_argument("--force", action="store_true",
                        help="Перезаписать расшифровку/протокол этой даты серии, если они уже есть")
    parser.add_argument("--no-deliver", action="store_true",
                        help="Не слать протокол в Telegram (для отладки/сухого прогона)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        args.date = validate_date(args.date)
    except ValueError as e:
        log.error("%s", e)
        return EXIT_INPUT
    # Санитизируем --series ДО использования: один безопасный сегмент пути во
    # всех местах (ростер/meta/путь протокола). finalize стрипает серию внутри
    # себя (_output_dir_for_meta) и использует как сегмент — без согласованной
    # чистки пробельная/со-слешем серия даёт рассинхрон путей и «протокол не
    # найден» на rc=0, либо запись за пределы output_dir.
    try:
        args.series = safe_series_segment(args.series)
    except ValueError as e:
        log.error("%s", e)
        return EXIT_INPUT
    if not os.path.exists(args.input):
        log.error("файл не найден: %s", args.input)
        return EXIT_INPUT

    try:
        kind = detect_input_kind(args.input, args.kind)
    except ValueError as e:
        log.error("%s", e)
        return EXIT_INPUT
    try:
        mode = resolve_mode(args.mode, kind=kind, series=args.series)
    except ValueError as e:
        log.error("%s", e)
        _emit({"ok": False, "error": "mode_invalid", "kind": kind})
        return EXIT_INPUT

    deliver = not args.no_deliver
    sid = safe_session_uid(args.series, args.date)
    tdir = transcripts_dir(args.transcripts_dir)
    log.info(
        "[ingest] вход=%s тип=%s серия=%s дата=%s путь=%s%s",
        args.input, kind, args.series, args.date, mode, "" if deliver else " (сухой прогон)",
    )

    # Рабочая папка прогона. tempfile — на месте, ничего лишнего не тащим (R11).
    workdir = Path(tempfile.mkdtemp(prefix="ingest-recording-"))
    # Сухой прогон без --output-dir — песочница прогона (как раньше). С доставкой —
    # рабочая папка бота: расшифровка обязана пережить прогон (перевыпуск по правке).
    durable_output = bool(args.output_dir) or deliver
    output_dir = (
        Path(args.output_dir) if args.output_dir
        else (default_output_dir() if deliver else workdir / "protocols")
    )
    try:
        from lib import finalize_lock  # noqa: PLC0415

        with finalize_lock.hold(sid) as locked:
            if not locked:
                log.error(
                    "[ingest] встречу %s уже финализирует кто-то другой (сборщик, повтор или "
                    "второй прогон) — второй не запускаю", sid,
                )
                _emit({"ok": False, "error": "finalize_in_progress", "mode": mode, "kind": kind})
                return EXIT_BUSY
            if retry_pending(sid):
                log.error("[ingest] встречу %s доводит очередь повторов бота — второй прогон не "
                          "запускаю", sid)
                _emit({"ok": False, "error": "in_retry_queue", "mode": mode, "kind": kind,
                       "note": RETRY_QUEUE_NOTE})
                return EXIT_BUSY
            if durable_output:
                busy = pending_same_date_meetings(tdir, args.series, args.date, sid)
                if busy:
                    log.error(
                        "[preflight] у серии %s за %s бот ещё доводит свою запись (%d) — пишет в те "
                        "же файлы; не запускаю", args.series, args.date, len(busy),
                    )
                    _emit({"ok": False, "error": "series_date_busy", "mode": mode, "kind": kind,
                           "pending": len(busy),
                           "note": "у серии за эту дату бот ещё собирает свою запись встречи — "
                                   "дождись его протокола; если потом нужно заменить его "
                                   "собранным из присланной записи — запуск с --force"})
                    return EXIT_BUSY
            backup: list[str] = []
            clashes = target_collisions(output_dir, args.series, args.date) if durable_output else []
            if clashes and not args.force:
                log.error(
                    "[preflight] у серии %s за %s уже есть расшифровка/протокол (%d файл(ов)) — "
                    "не перезаписываю; если это та же встреча и так и надо — запусти с --force",
                    args.series, args.date, len(clashes),
                )
                refusal = {"ok": False, "error": "target_exists", "mode": mode, "kind": kind,
                           "existing": [str(p) for p in clashes]}
                if only_transcript_exists(output_dir, args.series, args.date):
                    refusal["note"] = (
                        "за эту дату есть только расшифровка, протокол по ней не собрался — "
                        "пересобрать без новой оплаты расшифровки: «протокол %s %s» боту-нотариусу; "
                        "--force заново оплатит расшифровку" % (args.series, args.date))
                _emit(refusal)
                return EXIT_INPUT
            if clashes:
                backup = backup_before_force(clashes)

            if not deliver:
                if kind in ("video", "audio"):
                    protocol_path, _t, _m = _build_personal_media(args, kind, workdir, output_dir, sid)
                else:
                    protocol_path, _note = _process_transcript(args, workdir, output_dir)
                log.info("[ingest] --no-deliver: доставку пропустил. Протокол на диске: %s", protocol_path)
                _emit({"ok": True, "protocol_path": str(protocol_path), "delivered": False,
                       "kind": kind, "mode": mode})
                return EXIT_OK

            if mode == MODE_STANDARD:
                out = _run_standard(args, kind, output_dir, sid, tdir, workdir)
            else:
                out = _run_personal(args, kind, workdir, output_dir, sid, tdir)
            out.update({"kind": kind, "mode": mode})
            if backup:
                out["replaced_backup"] = backup
            _emit(out)
            return EXIT_OK if out.get("ok") else EXIT_FAILED
    except HandedToRetry as e:
        log.error("[ingest] %s", e)
        _emit({"ok": False, "error": "in_retry_queue", "mode": mode, "kind": kind,
               "note": RETRY_QUEUE_NOTE})
        return EXIT_BUSY
    except ProtocolNotGenerated as e:
        log.error("[ingest] %s", e)
        archived = sync_to_archive(
            protocol_path_for(output_dir, args.series, args.date), e.transcript_path, args.date,
        ) if durable_output else []
        # Про звук finalize сам пишет владельцу «протокол не сгенерировался»; текст
        # (R9) идёт мимо finalize — там сказать некому, кроме этого итога.
        told = " (бот отдельно написал об этом владельцу)" if kind != "transcript" else ""
        force = "заново оплатит расшифровку" if kind != "transcript" else "повторит сборку целиком"
        _emit({"ok": False, "error": "protocol_not_generated", "mode": mode, "kind": kind,
               "transcript_path": str(e.transcript_path), "archive_synced": archived,
               "note": "расшифровка готова и сохранена, а протокол не собрался — составить его "
                       "моделью не удалось%s. Пересобрать по готовой расшифровке: «протокол %s %s» "
                       "боту-нотариусу; перезапуск обёртки упрётся в target_exists, а с --force %s"
                       % (told, args.series, args.date, force)})
        return EXIT_FAILED
    except Exception as e:  # noqa: BLE001
        # Класс ошибки + сообщение БЕЗ текста реплик (R11/опасная тройка).
        log.error("[ingest] восстановление не удалось: %s: %s", type(e).__name__, e)
        return EXIT_FAILED
    finally:
        # Опасная тройка: чистим рабочую папку прогона ЦЕЛИКОМ — в ней лежат
        # извлечённое аудио и синтетическая meta. mkdtemp сам не чистится. Готовый
        # WAV пользователя лежит ВНЕ workdir и не трогается. Протокол и расшифровка
        # с доставкой лежат в рабочей папке бота, не здесь. Папку оставляем ТОЛЬКО в
        # сухом прогоне без --output-dir, где протокол лежит внутри неё и мы про
        # него отчитались («на диске»); там хотя бы извлечённое аудио убираем.
        keep_workdir = not args.output_dir and not deliver
        if keep_workdir:
            extracted = workdir / "recording.wav"
            try:
                if extracted.is_file():
                    extracted.unlink()
                    log.info("[ingest] извлечённое аудио удалено (опасная тройка)")
            except OSError:
                pass
        else:
            shutil.rmtree(workdir, ignore_errors=True)
            log.info("[ingest] рабочая папка прогона удалена (опасная тройка)")


if __name__ == "__main__":
    raise SystemExit(main())
