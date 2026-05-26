"""Финальная транскрипция полного WAV через локальный transcription-service.

Стримовая транскрипция Ф2 (3-сек чанки) даёт черновик в draft.txt — этот модуль
прогоняет ВЕСЬ WAV-файл одним запросом, потому что:
  - faster-whisper VAD умеет резать паузы и держит контекст между сегментами;
  - condition_on_previous_text работает только в рамках одного запроса;
  - длинные сегменты дают лучше пунктуацию.

API: вернуть список сегментов вида {start, end, text, no_speech_prob, ...}.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import requests


logger = logging.getLogger(__name__)


@dataclass
class WhisperSegment:
    start: float
    end: float
    text: str
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0
    compression_ratio: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "WhisperSegment":
        return cls(
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            text=(d.get("text") or "").strip(),
            no_speech_prob=float(d.get("no_speech_prob", 0.0)),
            avg_logprob=float(d.get("avg_logprob", 0.0)),
            compression_ratio=float(d.get("compression_ratio", 0.0)),
        )


def transcribe_wav(
    wav_path: str,
    service_url: str,
    model: str = "Systran/faster-whisper-medium",
    language: str = "ru",
    api_token: Optional[str] = None,
    timeout_s: int = 1800,
) -> tuple[str, str, list[WhisperSegment]]:
    """Прогоняет WAV через transcription-service. Возвращает (full_text, detected_lang, segments)."""
    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"WAV not found: {wav_path}")

    file_size = os.path.getsize(wav_path)
    logger.info(
        "Transcribing %s (size=%.1f MB, model=%s, lang=%s) via %s",
        wav_path, file_size / 1024 / 1024, model, language, service_url,
    )

    headers: dict[str, str] = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    with open(wav_path, "rb") as fh:
        files = {"file": (os.path.basename(wav_path), fh, "audio/wav")}
        data = {
            "model": model,
            "language": language,
            "response_format": "verbose_json",
            "timestamp_granularities": "segment",
        }
        # ВАЖНО: long timeout — для часовой встречи на CPU faster-whisper medium
        # может думать 15-25 минут.
        resp = requests.post(service_url, files=files, data=data, headers=headers, timeout=timeout_s)

    if resp.status_code != 200:
        raise RuntimeError(
            f"transcription-service returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    full_text = (body.get("text") or "").strip()
    detected_lang = body.get("language") or language
    segments_raw = body.get("segments") or []
    segments = [WhisperSegment.from_dict(s) for s in segments_raw]

    # Конфиденциальность: НЕ логируем full_text. Только metadata.
    logger.info(
        "Transcription done — lang=%s, segments=%d, total_chars=%d, duration=%.1fs",
        detected_lang, len(segments), len(full_text),
        segments[-1].end if segments else 0.0,
    )
    return full_text, detected_lang, segments
