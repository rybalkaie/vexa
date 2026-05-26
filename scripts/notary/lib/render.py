"""Рендер markdown-протокола из набора AlignedTurn + meta.

Источник шаблона: vexa/templates/meeting-protocol.md (placeholders в Jinja-стиле,
но без зависимости от Jinja — простая str.replace).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Optional

from .align import AlignedTurn


logger = logging.getLogger(__name__)


def _fmt_timecode(seconds: float) -> str:
    """Из 73.5 → «01:13»."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_duration_human(seconds: float) -> str:
    """Из 3725 → «1 ч 02 мин»."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    if h:
        return f"{h} ч {m:02d} мин"
    if m:
        return f"{m} мин"
    return f"{total} сек"


def _speaker_label(turn: AlignedTurn, cluster_to_index: dict[str, int]) -> str:
    """Возвращает «Илья», «Спикер 1», «Спикер ?» для одного turn."""
    if turn.display_name:
        return turn.display_name
    if turn.speaker is None:
        return "Спикер ?"
    idx = cluster_to_index.get(turn.speaker, 0)
    return f"Спикер {idx + 1}"


def render_protocol(
    template_path: str,
    turns: list[AlignedTurn],
    meta: dict,
    sources_used: list[str],
    asr_model: str,
    diarization_model: str = "pyannote/speaker-diarization-3.1",
) -> str:
    """Возвращает готовый markdown-протокол как строку."""
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template not found: {template_path}")

    with open(template_path, "r", encoding="utf-8") as fh:
        template = fh.read()

    # Стабильная нумерация: SPEAKER_00 → Спикер 1, SPEAKER_01 → Спикер 2, ...
    unique_clusters: list[str] = []
    for t in turns:
        if t.speaker and t.speaker not in unique_clusters:
            unique_clusters.append(t.speaker)
    cluster_to_index = {c: i for i, c in enumerate(unique_clusters)}

    # Тело транскрипта.
    body_lines: list[str] = []
    for t in turns:
        if not t.text.strip():
            continue
        label = _speaker_label(t, cluster_to_index)
        ts = _fmt_timecode(t.start)
        # Эскейпим html-чувствительные символы в репликах
        clean = t.text.strip()
        body_lines.append(f"**[{ts}] {label}:** {clean}")
    transcript_body = "\n\n".join(body_lines) if body_lines else "_Транскрипт пустой._"

    # Список участников: реальные имена сначала, потом «Спикер N» — для тех, кого
    # не привязали.
    participants_from_meta = meta.get("participants", []) or []
    speaker_labels_in_text: list[str] = []
    seen_labels: set[str] = set()
    for t in turns:
        lbl = _speaker_label(t, cluster_to_index)
        if lbl not in seen_labels:
            seen_labels.add(lbl)
            speaker_labels_in_text.append(lbl)

    participants_list = ", ".join(speaker_labels_in_text) if speaker_labels_in_text else "—"

    # Заголовок.
    raw_url = meta.get("meetingUrl") or "—"
    native_id = meta.get("nativeMeetingId") or "—"
    meeting_title = f"Встреча Telemost — {native_id}"

    # Дата — берём startTs.
    start_iso = meta.get("startTs")
    if start_iso:
        try:
            dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_str = start_iso
    else:
        date_str = "—"

    duration_s = float(meta.get("audioDurationS") or meta.get("durationS") or 0.0)
    duration_human = _fmt_duration_human(duration_s)

    audio_path = meta.get("files", {}).get("wav") or "—"

    placeholders = {
        "{{ meeting_title }}": meeting_title,
        "{{ date }}": date_str,
        "{{ duration_human }}": duration_human,
        "{{ participants_list }}": participants_list,
        "{{ meeting_url }}": raw_url,
        "{{ audio_path }}": audio_path,
        "{{ transcript_body }}": transcript_body,
        "{{ asr_model }}": asr_model,
        "{{ diarization_model }}": diarization_model,
        "{{ name_mapping_sources }}": ", ".join(sources_used) if sources_used else "—",
        "{{ generated_at }}": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "{{ session_uid }}": meta.get("sessionUid") or "—",
    }
    out = template
    for k, v in placeholders.items():
        out = out.replace(k, str(v))
    return out
