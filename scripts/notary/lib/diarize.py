"""Speaker diarization через pyannote-audio.

Использует `pyannote/speaker-diarization-3.1` (Hugging Face) — стандарт-де-факто,
лучший open-source DER на conversational audio (~11-19% на VoxConverse).

Требования:
  - HF_TOKEN — токен https://huggingface.co/settings/tokens (read scope).
  - Принятые условия модели: https://huggingface.co/pyannote/speaker-diarization-3.1
    + https://huggingface.co/pyannote/segmentation-3.0 (зависимость).

Если HF_TOKEN не задан — есть **fake mode** для проверки остального pipeline
без получения токена. Активируется env-переменной FAKE_DIARIZATION_PATH —
путь к JSON-файлу со списком [{start, end, speaker}, ...]. Полезно для
e2e-тестирования рендера/маппинга, когда HF-аккаунта ещё нет.

API: вернуть список сегментов [{start, end, speaker}].
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass
class DiarizationSegment:
    start: float
    end: float
    speaker: str  # «SPEAKER_00», «SPEAKER_01», ...


def _load_fake_diarization(path: str) -> list[DiarizationSegment]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out: list[DiarizationSegment] = []
    for d in data:
        out.append(
            DiarizationSegment(
                start=float(d["start"]),
                end=float(d["end"]),
                speaker=str(d["speaker"]),
            )
        )
    logger.warning(
        "FAKE diarization loaded from %s (%d segments). "
        "Это режим тестирования pipeline без HF-токена. Замени на pyannote "
        "когда получишь HF_TOKEN.", path, len(out),
    )
    return out


def diarize_wav(
    wav_path: str,
    hf_token: Optional[str] = None,
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    device: str = "cpu",
) -> list[DiarizationSegment]:
    """Запускает pyannote pipeline на WAV. Возвращает список сегментов с speaker-id."""
    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"WAV not found: {wav_path}")

    # Fake mode — для e2e-теста pipeline без HF-токена.
    fake_path = os.environ.get("FAKE_DIARIZATION_PATH")
    if fake_path:
        return _load_fake_diarization(fake_path)

    token = hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        raise RuntimeError(
            "HF_TOKEN не задан. Получите токен на https://huggingface.co/settings/tokens "
            "и примите условия моделей pyannote/speaker-diarization-3.1 + "
            "pyannote/segmentation-3.0. Положите в .env.notary как HF_TOKEN=hf_..."
        )

    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "pyannote.audio не установлен. Поставьте через requirements.txt: "
            "pip install pyannote.audio==3.3.2"
        ) from e

    logger.info(
        "Loading pyannote/speaker-diarization-3.1 (device=%s, num_speakers=%s, "
        "min=%s, max=%s)", device, num_speakers, min_speakers, max_speakers,
    )
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=token,
    )

    if device == "cuda":
        try:
            import torch  # type: ignore

            pipeline.to(torch.device("cuda"))
            logger.info("Pyannote pipeline moved to CUDA")
        except Exception as e:
            logger.warning("CUDA move failed (%s) — остаёмся на CPU", e)

    pipeline_kwargs: dict = {}
    if num_speakers is not None:
        pipeline_kwargs["num_speakers"] = int(num_speakers)
    else:
        if min_speakers is not None:
            pipeline_kwargs["min_speakers"] = int(min_speakers)
        if max_speakers is not None:
            pipeline_kwargs["max_speakers"] = int(max_speakers)

    logger.info("Running diarization on %s (kwargs=%s)", wav_path, pipeline_kwargs)
    diar = pipeline(wav_path, **pipeline_kwargs)

    segments: list[DiarizationSegment] = []
    for turn, _track, speaker in diar.itertracks(yield_label=True):
        segments.append(
            DiarizationSegment(
                start=float(turn.start),
                end=float(turn.end),
                speaker=str(speaker),
            )
        )

    speakers = sorted({s.speaker for s in segments})
    logger.info(
        "Diarization done — %d segments, %d unique speakers (%s)",
        len(segments), len(speakers), ", ".join(speakers),
    )
    return segments
