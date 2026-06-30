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

Доставка — ЛИЧНО Илье (решение владельца A1/Q5: восстановленный протокол идёт
ему на проверку, в групповой чат серии он кидает сам). Поэтому групповую
`deliver_protocol` на этом пути НЕ зовём; finalize-доставку глушим
`ENABLE_PROTOCOL_DELIVERY=0`, протокол шлём личным каналом нотариуса (tg-send
`--bot notarius`, как owner-alert finalize).

R11 / Опасная тройка: текст реплик/транскрипта В ЛОГИ НЕ ПИШЕМ — только
метаданные (длина, число спикеров, длительность, размер). Файл записи
обрабатывается на месте.

Запуск:
    python3 tools/ingest-recording.py <файл> --series <slug> --date YYYY-MM-DD \
        [--participants "Имя1,Имя2"] [--kind auto|video|audio|transcript] \
        [--output-dir DIR] [--no-deliver]
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
from datetime import datetime
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
if str(NOTARY_DIR) not in sys.path:
    sys.path.insert(0, str(NOTARY_DIR))

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
    *, series: str, date: str, wav_path: str, participants: list[str], sid: str
) -> dict:
    """Синтетическая meta для finalize — минимально достаточный набор полей,
    сличённый с боевой meta (runner.py BOT_CONFIG + collector): `sessionUid`,
    `files.wav`, `series`, `startTs` (→ date_part), `language`,
    `expectedParticipants`.

    КОНТРАКТ Ф3: НЕ нести failure-причину (`no_one_joined`/`deaf_*`/
    `evicted_failure`) — иначе гейт доставки подавил бы восстановленный протокол.
    Ставим нейтральный `endReason='manual_recovery'` (для finalize — незнакомая
    причина, при существенной записи гейт доставляет; доставку мы и так глушим).
    Флаг `recovery=True` помечает синтетику в meta для аудита."""
    return {
        "sessionUid": sid,
        "series": series,
        "startTs": f"{date}T00:00:00",
        "language": "ru",
        "files": {"wav": str(wav_path)},
        "expectedParticipants": list(participants or []),
        "participants": [],
        "endReason": "manual_recovery",
        "recovery": True,
        "keepAudio": True,
    }


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
# Вызов finalize (R7). Subprocess с заглушённой доставкой.
# ---------------------------------------------------------------------------
def run_finalize(meta_path: str | os.PathLike, output_dir: str | os.PathLike) -> int:
    """Запускает `finalize-meeting.py <meta>` поверх существующего пайплайна:
    STT + диаризация + генерация протокола. Групповую доставку ГЛУШИМ
    (`ENABLE_PROTOCOL_DELIVERY=0`) — восстановленный протокол шлём лично сами.

    Таймаута НЕ ставим (None) — длинная запись финализируется долго (CPU CCX13 +
    nginx-lb 7200s). Возвращает rc finalize. stderr/stdout наследуются (логи
    finalize R11-чисты сами по себе)."""
    finalize = str(NOTARY_DIR / "finalize-meeting.py")
    env = dict(os.environ)
    env["ENABLE_PROTOCOL_DELIVERY"] = "0"  # никакой групповой/авто-доставки из finalize
    cmd = [sys.executable, finalize, str(meta_path), "--output-dir", str(output_dir)]
    log.info("[finalize] запуск (доставка заглушена): output-dir=%s", output_dir)
    # finalize печатает СВОЙ JSON-отчёт в stdout (finalize-meeting.py); уводим его
    # в НАШ stderr, чтобы stdout обёртки нёс ТОЛЬКО её финальный JSON — контракт Ф5
    # (Альфред парсит stdout). Без timeout — длинную запись нельзя обрубать (docstring).
    proc = subprocess.run(cmd, env=env, stdout=sys.stderr)
    log.info("[finalize] rc=%s", proc.returncode)
    return proc.returncode


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
    from lib.llm_postprocess import regenerate_protocol_for_meeting  # noqa: PLC0415

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
    regenerate_protocol_for_meeting(
        transcript_path=transcript_path,
        protocol_path=out_protocol,
        meeting_meta=meta,
        meeting_sid=safe_session_uid(series, date),
    )
    return out_protocol


# ---------------------------------------------------------------------------
# Личная доставка Илье (A1/Q5). НЕ групповая deliver_protocol.
# ---------------------------------------------------------------------------
def deliver_personally(
    *, protocol_path: str | os.PathLike, series: str, date: str, degraded_note: str | None = None
) -> dict:
    """Шлёт восстановленный протокол ЛИЧНО Илье — личным каналом нотариуса
    (tg-send `--bot notarius`, как owner-alert finalize). Сначала короткое
    уведомление, затем сам протокол вложением-документом. В групповой чат серии
    НЕ шлём (решение владельца A1/Q5).

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
def _process_media(args, kind: str, workdir: Path) -> Path:
    """Видео/аудио → WAV → синтетическая meta → finalize → протокол + баннер R7."""
    src = Path(args.input)
    if kind == "audio" and _is_wav(src):
        wav_path = src  # готовый WAV — берём как есть (без лишней перекодировки)
        log.info("[media] вход уже WAV — STT возьмёт его напрямую")
    else:
        wav_path = workdir / "recording.wav"
        extract_wav(src, wav_path)

    participants = resolve_participants(args.series, args.participants)
    sid = safe_session_uid(args.series, args.date)
    meta = build_synthetic_meta(
        series=args.series, date=args.date, wav_path=str(wav_path),
        participants=participants, sid=sid,
    )
    meta_path = workdir / f"{sid}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(
        "[media] синтетическая meta: series=%s date=%s participants=%d sid=%s",
        args.series, args.date, len(participants), sid,
    )

    output_dir = Path(args.output_dir) if args.output_dir else (workdir / "protocols")
    output_dir.mkdir(parents=True, exist_ok=True)
    rc = run_finalize(meta_path, output_dir)
    if rc != 0:
        raise RuntimeError(f"finalize вернул rc={rc} — протокол не собран (см. логи finalize)")

    protocol_path = protocol_path_for(output_dir, args.series, args.date)
    if not protocol_path.is_file():
        raise RuntimeError(f"finalize отработал (rc=0), но протокол не найден: {protocol_path}")

    # R7: пометка «диаризация не выверена — сверьте роли».
    text = protocol_path.read_text(encoding="utf-8")
    protocol_path.write_text(inject_banner(text, DIARIZATION_BANNER), encoding="utf-8")
    log.info("[media] протокол собран, пометка «роли не выверены» добавлена")
    return protocol_path


def _process_transcript(args, workdir: Path) -> tuple[Path, str | None]:
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
    output_dir = Path(args.output_dir) if args.output_dir else (workdir / "protocols")
    protocol_path = output_dir / args.series / f"{args.date}-protokol.md"
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


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--output-dir", default=None,
                        help="Куда положить протокол (default — временная папка прогона)")
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
        return 2
    # Санитизируем --series ДО использования: один безопасный сегмент пути во
    # всех местах (ростер/meta/путь протокола). finalize стрипает серию внутри
    # себя (_output_dir_for_meta) и использует как сегмент — без согласованной
    # чистки пробельная/со-слешем серия даёт рассинхрон путей и «протокол не
    # найден» на rc=0, либо запись за пределы output_dir.
    try:
        args.series = safe_series_segment(args.series)
    except ValueError as e:
        log.error("%s", e)
        return 2
    if not os.path.exists(args.input):
        log.error("файл не найден: %s", args.input)
        return 2

    try:
        kind = detect_input_kind(args.input, args.kind)
    except ValueError as e:
        log.error("%s", e)
        return 2
    log.info("[ingest] вход=%s тип=%s серия=%s дата=%s", args.input, kind, args.series, args.date)

    # Рабочая папка прогона. tempfile — на месте, ничего лишнего не тащим (R11).
    workdir = Path(tempfile.mkdtemp(prefix="ingest-recording-"))
    degraded_note: str | None = None
    delivery_failed = False  # протокол собран, но НЕ дошёл до Ильи → не чистим копию
    try:
        if kind in ("video", "audio"):
            protocol_path = _process_media(args, kind, workdir)
        else:
            protocol_path, degraded_note = _process_transcript(args, workdir)

        log.info("[ingest] протокол готов: %s", protocol_path)
        if args.no_deliver:
            log.info("[ingest] --no-deliver: доставку пропустил. Протокол на диске: %s", protocol_path)
            print(json.dumps({"ok": True, "protocol_path": str(protocol_path),
                              "delivered": False, "kind": kind}, ensure_ascii=False))
            return 0

        delivery = deliver_personally(
            protocol_path=protocol_path, series=args.series, date=args.date,
            degraded_note=degraded_note,
        )
        # Документ дошёл до Ильи? Уведомление — best-effort, а ВЛОЖЕНИЕ (сам
        # протокол) — это и есть доставка. Не дошло → НЕ рапортуем успех молча:
        # суть фичи в том, чтобы встреча не терялась.
        delivered_ok = bool(delivery.get("document"))
        print(json.dumps(
            {"ok": delivered_ok, "protocol_path": str(protocol_path), "kind": kind,
             "delivered_personally": delivery},
            ensure_ascii=False,
        ))
        if not delivered_ok:
            # Протокол СОБРАН, но не доставлен. Сохраняем копию на диске (флаг
            # ниже не даст finally её удалить) и отдаём rc=1, чтобы Альфред сказал
            # Илье класс проблемы и где лежит файл — а не отрапортовал «готово».
            delivery_failed = True
            log.error(
                "[ingest] протокол СОБРАН, но документ не доставлен Илье — "
                "копия сохранена: %s", protocol_path,
            )
            return 1
        return 0
    except Exception as e:  # noqa: BLE001
        # Класс ошибки + сообщение БЕЗ текста реплик (R11/опасная тройка).
        log.error("[ingest] восстановление не удалось: %s: %s", type(e).__name__, e)
        return 1
    finally:
        # Опасная тройка: чистим рабочую папку прогона ЦЕЛИКОМ — в ней лежат
        # извлечённое аудио, синтетическая meta и (для транскрипта) рабочая копия
        # текста встречи. mkdtemp сам не чистится → без этого каждый прогон копит в
        # /tmp полный протокол/транскрипт встречи (чувствительное). Готовый WAV
        # пользователя лежит ВНЕ workdir и не трогается. Папку оставляем ТОЛЬКО в
        # сухом прогоне без --output-dir, где протокол лежит внутри неё и мы про
        # него отчитались («на диске»); там хотя бы извлечённое аудио убираем.
        # Сбой доставки (delivery_failed) при дефолтном output-dir → тоже держим:
        # протокол лежит внутри workdir и это единственная копия для Ильи.
        keep_workdir = not args.output_dir and (args.no_deliver or delivery_failed)
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
