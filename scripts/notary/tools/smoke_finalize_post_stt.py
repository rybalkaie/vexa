"""Post-STT pipeline smoke: map_all → map_speaker_names → apply_mapping → render_protocol.

WAV-источников локально нет (хранятся на VPS), полный finalize-meeting.py
запустить не на чем. Этот smoke прогоняет post-STT хвост на реальных
репликах sales-quality-2026-05-27 — проверяет что Ф2-интеграция не сломала
дальнейший рендер транскрипта.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

NOTARY_DIR = Path("/Users/ilarybalka/Projects/meeting-notary/vexa/scripts/notary")
sys.path.insert(0, str(NOTARY_DIR))

from lib.align import AlignedTurn  # noqa: E402
from lib.llm_postprocess import map_speaker_names  # noqa: E402
from lib.name_mapping import map_all, apply_mapping  # noqa: E402
from lib.render import render_protocol  # noqa: E402
from lib.paths import _target_path  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


SARGIN_LINES = [
    "в этом приложении. Основная наша проблема заключается в сложности транскребации из-за плохого качества звонков. Мы протестировали почти 10 различных доступных моделей. Сервис Sonix показывает самое высокое качество распознавания.",
    "распознает и делит там своя праприетарная это вот сервис соникс у них соответственно своя какая-то собственная модель которые они разрабатывают",
    "Ну вот смотри, с транскрипацией у меня был опыт. Ты делаешь транскрибацию за период закидываешь в любую нейронку и просто дальше задаешь вопрос на основании этой базы данных.",
    "Да нет, идея классная, в принципе реализовать-то ее можно. Тут единственная проблема будет в том, что что мы делаем с Bittrex после этого?",
]

ILYA_LINES = [
    "использует какая модель вот вы сказали ты сказал что она наиболее хорошо",
    "распознавания я понял понял так давайте получается они делают транскрипацию звонков по ролям дальше что происходит куда это транскрибация идет",
    "Понятно. Так, ну смотрите, идея транскрибации звонков 100% нужна. Я хотел бы, что помимо транскрипации у нас еще нейросеть в автоматическом режиме анализировала все звонки и не только звонки.",
    "У нас остается... Я как бы не хочу CRM пока делать. Пока! На текущий момент я понимаю, что...",
]


def main() -> int:
    # SPEAKER_00=Михаил Саргин, SPEAKER_01=Илья Рыбалка.
    turns: list[AlignedTurn] = []
    t = 0.0
    for i in range(max(len(SARGIN_LINES), len(ILYA_LINES))):
        if i < len(SARGIN_LINES):
            turns.append(AlignedTurn(start=t, end=t + 30, speaker="SPEAKER_00", text=SARGIN_LINES[i]))
            t += 35
        if i < len(ILYA_LINES):
            turns.append(AlignedTurn(start=t, end=t + 30, speaker="SPEAKER_01", text=ILYA_LINES[i]))
            t += 35

    expected = ["Илья Рыбалка", "Михаил Саргин"]
    panel: list[str] = []
    participants_union = list(dict.fromkeys(expected + panel))

    print("=== Step 1: map_all (S1+S2 only) ===")
    mapping_result = map_all(turns, participants_union)
    print(f"sources_used={mapping_result.sources_used}")
    print(f"cluster_to_name={mapping_result.cluster_to_name}")
    print(f"unresolved={mapping_result.unresolved_clusters}")

    cluster_to_name = dict(mapping_result.cluster_to_name)
    sources_used = list(mapping_result.sources_used)

    if mapping_result.unresolved_clusters:
        print("\n=== Step 2: map_speaker_names (LLM for unresolved) ===")
        llm = map_speaker_names(
            turns,
            expected_participants=expected,
            panel_participants=panel,
            already_mapped=cluster_to_name,
            meeting_sid="local-smoke-post-stt",
        )
        print(f"LLM result: {llm}")
        for cluster, (name, _conf) in llm.items():
            cluster_to_name[cluster] = name
        if llm:
            sources_used.append("llm-postprocess")

    print("\n=== Step 3: apply_mapping ===")
    apply_mapping(turns, cluster_to_name)
    sample = [(t.speaker, t.display_name, t.text[:40] + "...") for t in turns[:3]]
    print(f"first 3 turns (speaker, display_name, text): {sample}")

    print("\n=== Step 4: render_protocol ===")
    meta = {
        "sessionUid": "auto-tm-local-smoke",
        "series": "sales-quality",
        "startTs": "2026-05-28T08:00:00Z",
        "participants": panel,
        "expectedParticipants": expected,
        "audioDurationS": 600,
        "meetingUrl": "https://example.invalid/local-smoke",
    }
    md = render_protocol(
        template_path=str(NOTARY_DIR.parent.parent / "templates" / "meeting-protocol.md"),
        turns=turns,
        meta=meta,
        sources_used=sources_used,
        asr_model="speechmatics-enhanced",
        diarization_model="speechmatics-enhanced",
        transcript_relpath=None,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        target = _target_path(meta, root=tmpdir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md, encoding="utf-8")
        print(f"\nwrote {target} ({target.stat().st_size} bytes)")
        print("\n=== first 25 lines of rendered .md ===")
        for line in md.splitlines()[:25]:
            print(line)

    ok = all(cluster_to_name.get(c) == n for c, n in [
        ("SPEAKER_00", "Михаил Саргин"),
        ("SPEAKER_01", "Илья Рыбалка"),
    ])
    print(f"\nResult: {'OK' if ok else 'FAIL'} — sources={sources_used}, mapped={cluster_to_name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
