"""Alignment Whisper-сегментов с диарезацией pyannote.

Для каждого Whisper-сегмента (start, end, text) находим тот pyannote-кластер,
который покрывает наибольшую часть его длительности, и присваиваем speaker_id.

Если ни один кластер не покрывает сегмент (например, оба VAD не сошлись) —
ставим speaker = None. На выходе аналитик решает: оставить «Спикер ?» или
сжать с соседом.

Также делаем простой merge: подряд идущие сегменты одного спикера склеиваем
в одну реплику (полезно для читаемости протокола).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .diarize import DiarizationSegment
from .transcribe import WhisperSegment


logger = logging.getLogger(__name__)


@dataclass
class AlignedTurn:
    start: float
    end: float
    speaker: Optional[str]  # «SPEAKER_00» / «SPEAKER_01» / None
    text: str
    # display-имя (заполняется потом name_mapping'ом); если None — рендерим как «Спикер N»
    display_name: Optional[str] = None


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers(
    whisper_segments: list[WhisperSegment],
    diarization_segments: list[DiarizationSegment],
) -> list[AlignedTurn]:
    """Для каждого Whisper-сегмента находит speaker по максимальному overlap.

    Если whisper_segments пустые — возвращает только диарезацию без текста.
    """
    if not whisper_segments:
        return [
            AlignedTurn(start=d.start, end=d.end, speaker=d.speaker, text="")
            for d in diarization_segments
        ]

    aligned: list[AlignedTurn] = []
    for w in whisper_segments:
        best_speaker: Optional[str] = None
        best_overlap = 0.0
        for d in diarization_segments:
            ov = _overlap(w.start, w.end, d.start, d.end)
            if ov > best_overlap:
                best_overlap = ov
                best_speaker = d.speaker
        # Сегменты с очень коротким overlap (< 10% длины) считаем «непривязанными».
        wlen = max(0.001, w.end - w.start)
        if best_speaker is None or best_overlap / wlen < 0.1:
            best_speaker = None
        aligned.append(
            AlignedTurn(
                start=w.start,
                end=w.end,
                speaker=best_speaker,
                text=w.text,
            )
        )
    return aligned


def merge_consecutive_same_speaker(
    turns: list[AlignedTurn], max_gap_s: float = 1.5
) -> list[AlignedTurn]:
    """Склеивает подряд идущие реплики одного спикера в одну.

    max_gap_s — допустимая пауза между сегментами, чтобы всё ещё считать
    их одной репликой (1.5s — стандартная пауза «вдох + продолжение»).
    """
    if not turns:
        return []

    merged: list[AlignedTurn] = []
    cur = AlignedTurn(
        start=turns[0].start, end=turns[0].end,
        speaker=turns[0].speaker, text=turns[0].text,
        display_name=turns[0].display_name,
    )
    for nxt in turns[1:]:
        gap = nxt.start - cur.end
        if nxt.speaker == cur.speaker and gap <= max_gap_s:
            sep = " " if cur.text and nxt.text else ""
            cur.text = (cur.text + sep + nxt.text).strip()
            cur.end = nxt.end
        else:
            if cur.text.strip() or cur.speaker:
                merged.append(cur)
            cur = AlignedTurn(
                start=nxt.start, end=nxt.end,
                speaker=nxt.speaker, text=nxt.text,
                display_name=nxt.display_name,
            )
    if cur.text.strip() or cur.speaker:
        merged.append(cur)

    logger.info("Merged %d turns → %d (gap_s=%.1f)", len(turns), len(merged), max_gap_s)
    return merged
