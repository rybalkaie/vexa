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
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)


# SSRF guard: транскрипт = личные данные, не должен уходить на произвольный host.
# Расширить через env NOTARY_ALLOWED_TRANSCRIPTION_HOSTS=h1,h2,... (для будущих
# случаев — например, GPU-pool на отдельной VM).
_DEFAULT_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "172.17.0.1"}


def _allowed_hosts() -> set[str]:
    extra = os.environ.get("NOTARY_ALLOWED_TRANSCRIPTION_HOSTS", "")
    hosts = set(_DEFAULT_ALLOWED_HOSTS)
    if extra:
        hosts.update(h.strip() for h in extra.split(",") if h.strip())
    return hosts


def _validate_service_url(url: str) -> None:
    host = urlparse(url).hostname or ""
    allowed = _allowed_hosts()
    if host not in allowed:
        raise ValueError(
            f"transcription-service host {host!r} не в allowlist {sorted(allowed)}. "
            "Если нужен внешний host — добавь в NOTARY_ALLOWED_TRANSCRIPTION_HOSTS env."
        )


def _load_hallucination_phrases(lang: str) -> set[str]:
    """Загружает список Whisper-галлюцинаций из <lang>.txt.

    Это тот же файл, который использует TS hallucination-filter в стримовом режиме —
    один источник правды между bot и post-processing.

    Порядок поиска:
      1. $NOTARY_HALLUCINATIONS_DIR/<lang>.txt — явный override.
      2. /opt/vexa/services/vexa-bot/core/src/services/hallucinations/<lang>.txt (Docker layout).
      3. parents[3]/services/vexa-bot/... — когда scripts/notary внутри vexa форка.
      4. parents[4]/... — fallback на случай нестандартной вложенности.
      5. локальный lib/hallucinations/<lang>.txt — when scripts deployed без vexa рядом.
    """
    here = Path(__file__).resolve()
    candidates = []
    env_dir = os.environ.get("NOTARY_HALLUCINATIONS_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / f"{lang}.txt")
    candidates.extend([
        here.parents[3] / "services" / "vexa-bot" / "core" / "src" / "services" / "hallucinations" / f"{lang}.txt",
        here.parents[4] / "services" / "vexa-bot" / "core" / "src" / "services" / "hallucinations" / f"{lang}.txt",
        here.parent / "hallucinations" / f"{lang}.txt",
    ])
    for p in candidates:
        try:
            if p.exists():
                phrases: set[str] = set()
                for line in p.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    phrases.add(s.lower())
                logger.info("Loaded %d hallucination phrases from %s", len(phrases), p)
                return phrases
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to read %s: %s", p, e)
    logger.warning(
        "hallucinations/%s.txt not found (tried %d candidates) — фильтр галлюцинаций отключён",
        lang, len(candidates),
    )
    return set()


_HALLUCINATIONS_CACHE: dict[str, set[str]] = {}


def _filter_hallucination(text: str, lang: str) -> bool:
    """True если text — это известная Whisper-галлюцинация."""
    if lang not in _HALLUCINATIONS_CACHE:
        _HALLUCINATIONS_CACHE[lang] = _load_hallucination_phrases(lang)
    phrases = _HALLUCINATIONS_CACHE[lang]
    if not phrases:
        return False
    normalized = text.strip().lower().rstrip(".!?")
    return normalized in phrases or text.strip().lower() in phrases


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

    _validate_service_url(service_url)

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

    # Фильтр Whisper-галлюцинаций — из shared ru.txt (общий с TS hallucination-filter).
    # Стримовый режим Ф2 фильтрует только стрим, на полной транскрипции фильтра не было —
    # пришло «С вами был Игорь Негода» в live-протоколе Ф3.
    before_filter = len(segments)
    segments = [s for s in segments if not _filter_hallucination(s.text, detected_lang)]
    if before_filter != len(segments):
        logger.info(
            "Hallucination filter dropped %d/%d segments", before_filter - len(segments), before_filter,
        )
        # Пересобираем full_text из отфильтрованных сегментов — иначе он останется
        # с галлюцинациями (transcription-service вернул его из всех сегментов сразу).
        full_text = " ".join(s.text.strip() for s in segments).strip()

    # Конфиденциальность: НЕ логируем full_text. Только metadata.
    logger.info(
        "Transcription done — lang=%s, segments=%d, total_chars=%d, duration=%.1fs",
        detected_lang, len(segments), len(full_text),
        segments[-1].end if segments else 0.0,
    )
    return full_text, detected_lang, segments
