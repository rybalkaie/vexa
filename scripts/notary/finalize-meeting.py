#!/usr/bin/env python3
"""Финализирует записанную встречу: meta.json + WAV → Markdown-протокол.

Использование:
    python3 finalize-meeting.py <meta.json>

Пример:
    python3 finalize-meeting.py /transcripts/2026-05-26-test-1.meta.json

Pipeline:
    1. Прочитать meta.json (path к WAV, participants[], meetingUrl, …).
    2. Транскрибировать ВЕСЬ WAV через transcription-service (faster-whisper).
    3. Диарезация через pyannote-audio (требует HF_TOKEN).
    4. Alignment Whisper-сегментов с pyannote-кластерами.
    5. Merge подряд идущих реплик одного спикера.
    6. Маппинг имён: source 1 (Telemost) + source 2 (regex+pymorphy3) +
       source 3 (Claude Haiku, под флагом ENABLE_CLAUDE_NAME_MAPPING).
    7. Рендер markdown по templates/meeting-protocol.md.
    8. Записать в --output-dir (default: $TELEMOST_PROTOCOL_DIR или
       /opt/meeting-notary/_tmp/protocols/).
    9. Если keep_audio в meta != true и --keep-audio не передан — удалить WAV.

Конфиденциальность («Опасная тройка» Ф3):
    - НЕ логируем содержимое транскрипта.
    - НЕ сохраняем сырой ответ Claude в долгоживущие файлы.
    - В meta.json финального протокола НЕ кладём реплики — только пути + sources.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Подключаем lib (когда запускается из родительской директории).
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from lib.transcribe import transcribe_wav  # noqa: E402
from lib.diarize import diarize_wav  # noqa: E402
from lib.align import assign_speakers, merge_consecutive_same_speaker  # noqa: E402
from lib.name_mapping import map_all, apply_mapping  # noqa: E402
from lib.render import render_protocol  # noqa: E402


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Финализирует встречу в Markdown-протокол")
    parser.add_argument("meta_json", help="Путь к <sessionUid>.meta.json")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("TELEMOST_PROTOCOL_DIR", "/opt/meeting-notary/_tmp/protocols"),
        help="Куда положить .md (default: $TELEMOST_PROTOCOL_DIR)",
    )
    parser.add_argument(
        "--transcription-service-url",
        default=os.environ.get("TRANSCRIPTION_SERVICE_URL", "http://127.0.0.1:8083/v1/audio/transcriptions"),
    )
    parser.add_argument(
        "--asr-model",
        default=os.environ.get("ASR_MODEL", "Systran/faster-whisper-medium"),
    )
    parser.add_argument(
        "--template",
        default=str(THIS_DIR.parent.parent / "templates" / "meeting-protocol.md"),
        help="Путь к шаблону протокола",
    )
    parser.add_argument("--keep-audio", action="store_true", help="Не удалять WAV после рендера")
    parser.add_argument("--num-speakers", type=int, default=None, help="Точное число спикеров (если знаем)")
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("finalize-meeting")

    # 1. Читаем meta.
    if not os.path.exists(args.meta_json):
        log.error("meta.json not found: %s", args.meta_json)
        return 2
    # Минимальная валидация: имя файла должно оканчиваться .meta.json или .json,
    # иначе пользователь скорее всего ошибся (например, передал .txt или .wav).
    if not args.meta_json.endswith(".json"):
        log.error("meta_json должен быть .json файлом, получили: %s", args.meta_json)
        return 2
    with open(args.meta_json, "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    # Минимальная sanity на содержимое: должен быть dict с sessionUid и files.wav.
    if not isinstance(meta, dict) or not meta.get("sessionUid") or not (meta.get("files") or {}).get("wav"):
        log.error("meta.json не похоже на artifact бота (нужны поля sessionUid + files.wav)")
        return 2

    session_uid = meta.get("sessionUid") or "unknown"
    wav_path = (meta.get("files") or {}).get("wav")
    if not wav_path or not os.path.exists(wav_path):
        log.error("WAV not found (meta.files.wav=%s)", wav_path)
        return 3
    participants = meta.get("participants") or []
    language = meta.get("language") or "ru"

    log.info("Session %s — wav=%s, %d participants, lang=%s",
             session_uid, wav_path, len(participants), language)

    # 2. Транскрипция полного WAV.
    log.info("Step 1/5 — Transcribe full WAV")
    api_token = os.environ.get("TRANSCRIPTION_SERVICE_TOKEN")
    try:
        _full_text, detected_lang, whisper_segments = transcribe_wav(
            wav_path,
            service_url=args.transcription_service_url,
            model=args.asr_model,
            language=language,
            api_token=api_token,
        )
    except Exception as e:
        log.error("Transcription failed: %s", e)
        return 4

    if not whisper_segments:
        log.warning("Whisper returned 0 segments — записанный WAV похож на тишину")

    # 3. Диарезация.
    log.info("Step 2/5 — Diarization via pyannote-audio")
    try:
        diarization_segments = diarize_wav(
            wav_path,
            num_speakers=args.num_speakers,
            min_speakers=args.min_speakers,
            max_speakers=args.max_speakers,
            device="cpu",
        )
    except Exception as e:
        log.error("Diarization failed: %s", e)
        log.error("Если ошибка про HF_TOKEN — получите токен на huggingface.co/settings/tokens "
                  "и примите условия pyannote/speaker-diarization-3.1.")
        return 5

    # 4. Alignment + merge.
    log.info("Step 3/5 — Align + merge")
    aligned = assign_speakers(whisper_segments, diarization_segments)
    turns = merge_consecutive_same_speaker(aligned, max_gap_s=1.5)

    # 5. Маппинг имён.
    log.info("Step 4/5 — Name mapping (3 sources)")
    mapping_result = map_all(turns, participants)
    turns = apply_mapping(turns, mapping_result.cluster_to_name)
    log.info("Mapping done — sources=%s, mapped=%d, unresolved=%d",
             mapping_result.sources_used,
             len(mapping_result.cluster_to_name),
             len(mapping_result.unresolved_clusters))

    # 6. Рендер.
    log.info("Step 5/5 — Render markdown")
    markdown = render_protocol(
        template_path=args.template,
        turns=turns,
        meta=meta,
        sources_used=mapping_result.sources_used,
        asr_model=args.asr_model,
    )

    # 7. Записываем в output-dir.
    os.makedirs(args.output_dir, exist_ok=True)
    date_part = (meta.get("startTs") or datetime.now().isoformat())[:10]
    out_filename = f"{date_part}-{session_uid}.md"
    out_path = os.path.join(args.output_dir, out_filename)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    log.info("Protocol written → %s", out_path)

    # 8. keep_audio.
    keep_audio_from_meta = bool(meta.get("keepAudio") or meta.get("keep_audio"))
    if args.keep_audio or keep_audio_from_meta:
        log.info("keep_audio=true — WAV сохраняется")
    else:
        try:
            os.unlink(wav_path)
            log.info("WAV удалён (default-удаление аудио после транскрипции)")
        except Exception as e:
            log.warning("Не удалось удалить WAV %s: %s", wav_path, e)

    # 9. Печатаем краткий результат.
    print(json.dumps({
        "ok": True,
        "session_uid": session_uid,
        "protocol_path": out_path,
        "wav_kept": args.keep_audio or keep_audio_from_meta,
        "language_detected": detected_lang,
        "whisper_segments": len(whisper_segments),
        "diarization_segments": len(diarization_segments),
        "speakers_detected": len({s.speaker for s in diarization_segments}),
        "speakers_named": len(mapping_result.cluster_to_name),
        "name_mapping_sources": mapping_result.sources_used,
        "unresolved_clusters": mapping_result.unresolved_clusters,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
