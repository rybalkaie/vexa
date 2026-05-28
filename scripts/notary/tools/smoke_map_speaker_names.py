"""Synthetic smoke для llm_postprocess.map_speaker_names (Ф2).

Цель критерия «сделано» п.7: на встрече с
expectedParticipants=["Илья Рыбалка","Михаил Саргин"], минимум 3 длинные
реплики каждого → 2/2 спикеров получают правильное имя, confidence ≥ 0.85.

Реплики — из реальной встречи sales-quality-2026-05-27 (выжимка длинных
блоков Илья + Михаила Саргина). SPEAKER_00=Михаил Саргин, SPEAKER_01=Илья
Рыбалка. LLM должен это определить по контексту речи.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

THIS = Path(__file__).resolve()
NOTARY_DIR = Path("/Users/ilarybalka/Projects/meeting-notary/vexa/scripts/notary")
sys.path.insert(0, str(NOTARY_DIR))

from lib.align import AlignedTurn  # noqa: E402
from lib.llm_postprocess import map_speaker_names  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# Реальные реплики из встречи sales-quality-2026-05-27 (sales-quality).
# Истинная разметка (для оценки результата) — НЕ передаётся в LLM.
SARGIN_LINES = [
    "в этом приложении. Основная наша проблема заключается в сложности транскребации из-за плохого качества звонков, то есть мы транскрибируем не какие-то там заметки, которые мы наговариваем нормально на микрофон, а звонки с шумом, ветром, хреновым прям откровенно качеством. Bitrix это не вывозит из коробки вообще совсем. Мы протестировали почти 10 различных доступных моделей, в том числе Сберовскую, Яндексовскую. Вот ту модель, которую мы нашли показывает именно с точки зрения распознавания голоса самое высокое качество.",
    "распознает и делит там своя праприетарная это вот сервис соникс у них соответственно своя какая-то собственная модель которые они разрабатывают ну у них это основа бизнеса вот эта вот моделя",
    "Ну вот смотри, с транскрипацией у меня был опыт вот из самого простого. Ты делаешь транскрибацию за период закидываешь в любую нейронку в качестве mdfile'а и просто дальше задаешь вопрос на основании этой базы данных. это вот самый такой примитивный простой способ как посмотреть, что у тебя там менеджеры с кем говорят.",
    "Да нет, идея классная, в принципе реализовать-то ее можно. Тут единственная проблема будет в том, что что мы делаем с Bittrex после этого? Ну потому что по сути дела то, что ты предлагаешь это на 70 процентов заменяет CRM",
]

ILYA_LINES = [
    "использует какая модель вот вы сказали ты сказал что она наиболее хорошо",
    "распознавания я понял понял так давайте получается они делают транскрипацию звонков по ролям дальше что происходит куда это транскрибация идет и что с ней вообще дальше, ну как ее судьба дальнейшая?",
    "Понятно. Так, ну смотрите, идея транскрибации звонков 100% нужна. Классно что нашли сервис, который нормально транскрибирует. Я смотрите на эту как бы идею, смотрю чуть больше. Идея та же самая, но я хотел наверное больше ее автоматизировать. Я хотел бы, что помимо транскрипации у нас еще нейросеть в автоматическом режиме анализировала все звонки и не только звонки, чтобы она анализирала все касания менеджера с клиентам, потому что менеджер может поговорить о чем-то договориться.",
    "У нас остается... Я как бы не хочу CRM пока делать. Пока! На текущий момент я понимаю, что...",
]


def build_turns() -> list[AlignedTurn]:
    """SPEAKER_00 = Михаил Саргин, SPEAKER_01 = Илья Рыбалка."""
    turns: list[AlignedTurn] = []
    t = 0.0
    # Чередуем для реалистичности: M-И-М-И-М-И-М-И.
    for i in range(max(len(SARGIN_LINES), len(ILYA_LINES))):
        if i < len(SARGIN_LINES):
            turns.append(AlignedTurn(start=t, end=t + 30, speaker="SPEAKER_00", text=SARGIN_LINES[i]))
            t += 35
        if i < len(ILYA_LINES):
            turns.append(AlignedTurn(start=t, end=t + 30, speaker="SPEAKER_01", text=ILYA_LINES[i]))
            t += 35
    return turns


def main() -> int:
    turns = build_turns()
    print(f"Smoke: {len(turns)} turns, SPEAKER_00=Михаил Саргин, SPEAKER_01=Илья Рыбалка")
    expected = ["Илья Рыбалка", "Михаил Саргин"]
    panel = []

    result = map_speaker_names(
        turns,
        expected_participants=expected,
        panel_participants=panel,
        meeting_sid="synthetic-smoke-sales-quality",
    )
    print(f"Result: {result}")

    truth = {"SPEAKER_00": "Михаил Саргин", "SPEAKER_01": "Илья Рыбалка"}
    correct = 0
    high_conf = 0
    for cluster, true_name in truth.items():
        got = result.get(cluster)
        if got and got[0] == true_name:
            correct += 1
            if got[1] >= 0.85:
                high_conf += 1
            print(f"  ✅ {cluster}: {got[0]} (conf={got[1]:.2f})")
        else:
            print(f"  ❌ {cluster}: expected {true_name}, got {got}")

    print(f"Correct: {correct}/2, conf≥0.85: {high_conf}/2")
    if correct == 2 and high_conf == 2:
        print("SMOKE OK")
        return 0
    print("SMOKE FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
