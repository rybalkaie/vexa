"""wav_concat.py — multichunk-склейка + починка «битых» WAV для финализации (Ф2).

Корень P0 (подтверждён на проде 2026-06-03): бот пишет PCM в WAV стримом
(`fs.writeSync` → данные сразу на bind-mount хоста), НО заголовок WAV
финализируется только в `WavStreamWriter.close()` из `finally`-teardown'а
recording.ts. `entrypoint.sh` запускает node ребёнком bash (PID 1), поэтому
`docker stop` / рестарт systemd / OOM убивают node SIGKILL'ом → `finally` не
выполняется → заголовок остаётся placeholder'ом из `open()` (`data size = 0`,
RIFF = 36). Любой ридер, уважающий заголовок (ffmpeg, Speechmatics, `wave`),
видит «0 байт аудио» → finalize отдаёт rc=3 «WAV not found»-эквивалент, хотя
на диске лежат десятки МБ реального PCM (см. 06-01 .recover-bak: 86 МБ данных,
data_size=0 в шапке).

Этот модуль — серверная страховка, которая чинит ЛЮБОЙ такой случай:
читает PCM по ФАКТИЧЕСКОМУ размеру файла, а не по полю data_size в шапке.
Плюс склеивает все `meta.recording.chunks[]` (Ф5 chunk-декаплинг) в один WAV
перед STT.

RISK2 (диаризация на стыках кусков): склейка PCM в ОДИН WAV + ОДИН запрос в
Speechmatics сохраняет speaker-continuity лучше, чем по-кусочная диаризация со
сшивкой (один job = единые ID спикеров). Единственный артефакт — вырезанные
паузы тишины между кусками сжимают абсолютные таймкоды; протокол от этого не
страдает (он строится на порядке/спикерах реплик, не на wall-clock). Поэтому
выбран вариант «склеить → один STT-job», а не «диаризовать по кускам».

Только stdlib (struct/os) — модуль тестируется без Speechmatics/torch.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

# Канонический PCM-WAV-заголовок, который пишет бот (float32ToWavBuffer):
# RIFF(4) size(4) WAVE(4) "fmt "(4) 16(4) PCM(2) ch(2) rate(4) byterate(4)
# blockalign(2) bits(2) "data"(4) datasize(4) = 44 байта. PCM начинается с 44.
_CANONICAL_HEADER_LEN = 44
_DEFAULT_SAMPLE_RATE = 16000
_BYTES_PER_SAMPLE = 2  # 16-bit mono


def _find_data_offset(head: bytes) -> int:
    """Сместиться к началу PCM-данных.

    Бот всегда пишет 44-байтовый канонический заголовок, но на случай чужих
    WAV ищем маркер `data` в первых байтах. data-данные начинаются через 8 байт
    после маркера (`data` + 4 байта размера). Fallback — 44.
    """
    idx = head.find(b"data")
    if idx >= 0:
        return idx + 8
    return _CANONICAL_HEADER_LEN


def _read_fmt(head: bytes) -> tuple[int, int, int]:
    """Достать (sample_rate, channels, bits) из fmt-субчанка. Дефолты — 16k/1/16."""
    try:
        # fmt-поля в каноническом заголовке: channels@22, rate@24, bits@34.
        channels = struct.unpack_from("<H", head, 22)[0] or 1
        sample_rate = struct.unpack_from("<I", head, 24)[0] or _DEFAULT_SAMPLE_RATE
        bits = struct.unpack_from("<H", head, 34)[0] or 16
        return sample_rate, channels, bits
    except struct.error:
        return _DEFAULT_SAMPLE_RATE, 1, 16


def read_pcm(path: str | os.PathLike) -> tuple[bytes, int]:
    """Прочитать PCM из WAV, ИГНОРИРУЯ поле data_size в шапке.

    Возвращает (pcm_bytes, sample_rate). Длина PCM = реальный размер файла
    минус заголовок — так чинится placeholder-шапка (data_size=0) у WAV,
    у которого бот не успел выполнить close() (корень P0).
    """
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as fh:
        head = fh.read(min(size, 4096))
        sample_rate, _channels, _bits = _read_fmt(head)
        data_off = _find_data_offset(head)
        if data_off >= size:
            return b"", sample_rate
        fh.seek(data_off)
        pcm = fh.read()
    # Выравниваем на границу сэмпла (16-bit): хвост в 1 байт (оборванный
    # последний writeSync) отбрасываем, иначе sample-границы поедут.
    if len(pcm) % _BYTES_PER_SAMPLE:
        pcm = pcm[: len(pcm) - (len(pcm) % _BYTES_PER_SAMPLE)]
    return pcm, sample_rate


def build_wav_header(pcm_len: int, sample_rate: int = _DEFAULT_SAMPLE_RATE,
                     channels: int = 1, bits: int = 16) -> bytes:
    """Собрать валидный 44-байтовый PCM-WAV-заголовок под данные длины pcm_len."""
    byte_rate = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    return b"".join([
        b"RIFF",
        struct.pack("<I", 36 + pcm_len),
        b"WAVE",
        b"fmt ",
        struct.pack("<I", 16),
        struct.pack("<H", 1),  # PCM
        struct.pack("<H", channels),
        struct.pack("<I", sample_rate),
        struct.pack("<I", byte_rate),
        struct.pack("<H", block_align),
        struct.pack("<H", bits),
        b"data",
        struct.pack("<I", pcm_len),
    ])


def write_wav(out_path: str | os.PathLike, pcm: bytes,
              sample_rate: int = _DEFAULT_SAMPLE_RATE) -> int:
    """Записать валидный WAV (правильная шапка + PCM). Возвращает число сэмплов."""
    header = build_wav_header(len(pcm), sample_rate)
    with open(out_path, "wb") as fh:
        fh.write(header)
        fh.write(pcm)
    return len(pcm) // _BYTES_PER_SAMPLE


def _remap_host_path(path: str) -> str:
    """Контейнерный путь `/transcripts/...` → хостовой, если симлинка нет.

    На проде `/transcripts` — симлинк на `_tmp/transcripts`, поэтому обычно
    путь резолвится как есть. Но если симлинка нет (другой хост/тест),
    подменяем префикс на MEETING_NOTARY_TRANSCRIPTS_DIR. Не трогаем уже-хостовые
    и относительные пути.
    """
    if not path or os.path.exists(path) or not path.startswith("/transcripts/"):
        return path
    base = os.environ.get(
        "MEETING_NOTARY_TRANSCRIPTS_DIR",
        "/home/dev/meeting-notary/_tmp/transcripts",
    )
    return os.path.join(base, os.path.basename(path))


def _existing_chunk_paths(meta: dict) -> list[str]:
    """Пути chunk-WAV из meta.recording.chunks[], отфильтрованные по «есть на
    диске И размер > заголовка» (size>44 = есть реальный PCM).

    ВАЖНО: фильтруем по РАЗМЕРУ ФАЙЛА, а не по meta.chunk.samples/durationS —
    у куска, на котором бот умер до close(), meta.samples=0 (статистика
    проставляется в closeChunk), но на диске PCM есть. Полагаться на meta тут
    нельзя — это и есть корневой кейс.
    """
    rec = meta.get("recording") or {}
    chunks = rec.get("chunks")
    if not isinstance(chunks, list):
        return []
    ordered = sorted(
        (c for c in chunks if isinstance(c, dict) and c.get("wav")),
        key=lambda c: c.get("idx") or 0,
    )
    out: list[str] = []
    for c in ordered:
        path = _remap_host_path(str(c["wav"]))
        try:
            if os.path.exists(path) and os.path.getsize(path) > _CANONICAL_HEADER_LEN:
                out.append(path)
        except OSError:
            continue
    return out


def _pcm_extent(path: str | os.PathLike) -> tuple[int, int, int]:
    """(data_offset, pcm_len, sample_rate) по РЕАЛЬНОМУ размеру файла.

    pcm_len выровнен на границу сэмпла (16-bit). Заголовок не доверяем —
    длину берём из размера файла (чинит placeholder data_size=0).
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(min(size, 4096))
    sr, _ch, _bits = _read_fmt(head)
    off = _find_data_offset(head)
    n = max(0, size - off)
    n -= n % _BYTES_PER_SAMPLE
    return off, n, sr


def _header_is_valid(path: str | os.PathLike) -> bool:
    """True, если data_size в шапке > 0 И совпадает с (file_size - data_offset),
    т.е. close() отработал и заголовок финализирован. Тогда WAV пригоден как
    есть — читать/перезаписывать его не нужно (типовой кейс одиночного chunk).
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(64)
        off = _find_data_offset(head)
        declared = struct.unpack_from("<I", head, 40)[0]
        return declared > 0 and declared == (size - off)
    except (struct.error, OSError):
        return False


def _concat_to_file(candidates: list[str], out_path: str | os.PathLike) -> tuple[int, int, int]:
    """Стримовая склейка PCM кандидатов в валидный WAV out_path.

    Память не растёт с длиной аудио: считаем суммарную длину по размерам файлов,
    пишем заголовок, затем дописываем PCM каждого куска блоками по 1 МБ.
    Возвращает (samples, sample_rate, used_chunks).
    """
    extents: list[tuple[str, int, int]] = []
    sample_rate = _DEFAULT_SAMPLE_RATE
    total = 0
    for cp in candidates:
        try:
            off, n, sr = _pcm_extent(cp)
        except OSError:
            continue
        if n <= 0:
            continue
        if not extents:
            sample_rate = sr
        extents.append((cp, off, n))
        total += n
    if total == 0:
        return 0, sample_rate, 0

    written = 0
    with open(out_path, "wb") as out:
        out.write(build_wav_header(total, sample_rate))  # предварительная шапка
        for cp, off, n in extents:
            with open(cp, "rb") as fh:
                fh.seek(off)
                remaining = n
                while remaining > 0:
                    block = fh.read(min(1 << 20, remaining))
                    if not block:
                        break  # файл усёкся на лету — добьём шапку ниже
                    out.write(block)
                    written += len(block)
                    remaining -= len(block)
        if written != total:
            # Кусок(и) усеклись между stat и чтением (крайне маловероятно —
            # finalize гейтится по живому контейнеру). Переписываем шапку под
            # реально записанное, чтобы data_size не врал.
            written -= written % _BYTES_PER_SAMPLE
            out.seek(0)
            out.write(build_wav_header(written, sample_rate))
    return written // _BYTES_PER_SAMPLE, sample_rate, len(extents)


def resolve_wav_for_stt(meta: dict, *, tmp_dir: str | os.PathLike | None = None,
                        log=None) -> tuple[str | None, bool]:
    """Вернуть (audio_path, is_temp) — аудио, готовое к STT.

    Логика:
      • multichunk (Ф5): склеить все существующие `recording.chunks[]` в один
        починенный WAV (temp) → один STT-job (RISK2: сохраняет диаризацию).
      • single/legacy: использовать `files.wav`. Если шапка битая (placeholder)
        — починить в temp; если валидная — отдать путь как есть (без копии).
      • нечего собрать (всё отсутствует) → (None, False); вызывающий отдаст
        rc=3/rc=10 по своей логике (meta.delivered и т.п.).

    is_temp=True → вызывающий обязан удалить файл после STT.
    """
    def _log(msg: str) -> None:
        if log is not None:
            log.info(msg)

    files_wav_raw = (meta.get("files") or {}).get("wav")
    files_wav = _remap_host_path(str(files_wav_raw)) if files_wav_raw else None

    chunk_paths = _existing_chunk_paths(meta)

    # Кандидаты: куски, если есть; иначе — единичный files.wav.
    if chunk_paths:
        candidates = chunk_paths
    elif files_wav and os.path.exists(files_wav) and os.path.getsize(files_wav) > _CANONICAL_HEADER_LEN:
        candidates = [files_wav]
    else:
        # Ничего пригодного на диске. Отдаём None — пусть вызывающий разрулит
        # rc=3 / rc=10 (delivered) по files.wav, как раньше.
        return None, False

    # Fast-path: единственный кусок с УЖЕ валидной шапкой → отдаём как есть, БЕЗ
    # чтения PCM. Типовой кейс — встреча без 5-мин пауз = один большой chunk,
    # close() отработал штатно. Читать 100+ МБ в RAM только ради сверки шапки —
    # расточительно (Н1 цикла Ф2).
    if len(candidates) == 1 and _header_is_valid(candidates[0]):
        return candidates[0], False

    # basename: meta.sessionUid пишет наш бот (tm-<ms>/auto-...), но защищаемся
    # от path-traversal через имя temp-файла на случай битой/чужой meta.
    session_uid = os.path.basename(str(meta.get("sessionUid") or "session")) or "session"
    out_dir = Path(tmp_dir) if tmp_dir else Path(candidates[0]).parent
    out_path = out_dir / f"{session_uid}.finalize-concat.wav"

    # Стримовая склейка/починка — без удержания всего аудио в памяти.
    samples, sample_rate, used = _concat_to_file(candidates, out_path)
    if samples == 0:
        # Файлы есть, но PCM пуст (только заголовки) — нечего финализировать.
        _log(f"wav_concat: кандидатов {len(candidates)}, но PCM пуст — нечего собирать")
        try:
            Path(out_path).unlink()
        except OSError:
            pass
        return None, False

    _log(
        f"wav_concat: собрано {used} кусок(ов) → {Path(out_path).name} "
        f"({samples} сэмплов, ~{samples // sample_rate}s, sr={sample_rate})"
    )
    return str(out_path), True
