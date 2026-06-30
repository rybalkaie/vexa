"""Тесты Ф3 (план bot-eviction-recovery-resilience) R4c + R13-доставка:
гейт доставки протокола в групповой чат серии.

Закрепляют ЧИСТУЮ логику `lib.delivery_gate.evaluate_delivery` (без IO/Telegram/
docker) — «пускать ли финальный протокол в группу или подавить как обрывок/провал».
Раздвоение новых `endReason` Ф2 (`evicted_failure` vs `evicted_normal_end`) +
страховка FU-4 (короткую ПОЛНУЮ встречу, ошибочно помеченную `evicted_failure`,
не теряем — подавляем по СУЩЕСТВЕННОСТИ записи, а не по метке).

Почему важно: регрессия гейта = либо обрывок-мусор уходит в групповой чат команды
(R4c), либо реальный короткий протокол теряется (главный РИСК1 плана). Тест ловит
оба без живого STT-пайплайна. См. plans/2026-06-30-bot-eviction-recovery-resilience.md.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase3_delivery_gate -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, str(_NOTARY / rel))
    mod = importlib.util.module_from_spec(spec)
    # Регистрируем ДО exec_module: dataclass на py3.9 при `from __future__ import
    # annotations` резолвит ClassVar через sys.modules[cls.__module__] — без
    # регистрации падает AttributeError. (Прод-импорт `from lib import …` это делает сам.)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


delivery_gate = _load("delivery_gate_f3", "lib/delivery_gate.py")
evaluate = delivery_gate.evaluate_delivery
MIN = delivery_gate.SUBSTANTIAL_MIN_REPLIES  # порог существенности (дефолт)


class TestNewEvictionReasons(unittest.TestCase):
    """Раздвоение `evicted_failure` vs `evicted_normal_end` (R4c + R13-доставка)."""

    def test_evicted_failure_empty_suppressed(self):
        # Пустой evicted_failure (нет реплик) → подавить обрывок (R4c).
        d = evaluate(end_reason="evicted_failure", turn_count=0, utter_count=0)
        self.assertTrue(d.suppressed)
        self.assertFalse(d.deliver)
        # бот уже алертил в момент детекта → finalize НЕ дублирует сообщение.
        self.assertTrue(d.bot_already_alerted)

    def test_evicted_failure_with_speech_delivered_external(self):
        # FU-4: evicted_failure С РЕЧЬЮ (короткая полная встреча, ошибочно помеченная
        # порогом A7=25 мин) → доставить, НЕ теряем по метке. Внешний STT.
        d = evaluate(end_reason="evicted_failure", turn_count=12, utter_count=30)
        self.assertTrue(d.deliver)
        self.assertFalse(d.suppressed)
        self.assertTrue(d.substantial)

    def test_evicted_failure_short_full_delivered_internal(self):
        # FU-4 на ВНУТРЕННЕМ бэкенде (whisper_pyannote, utter_count=None):
        # существенность мерится по turn_count.
        d = evaluate(end_reason="evicted_failure", turn_count=8, utter_count=None)
        self.assertTrue(d.deliver)
        self.assertTrue(d.substantial)

    def test_evicted_normal_end_delivered(self):
        # R13-доставка: выброс-в-конце ПОЛНОЙ встречи = норма → доставлять как обычно.
        d = evaluate(end_reason="evicted_normal_end", turn_count=120, utter_count=300)
        self.assertTrue(d.deliver)
        self.assertFalse(d.suppressed)

    def test_evicted_normal_end_empty_external_still_suppressed(self):
        # Защита: даже evicted_normal_end с 0 реплик внешнего STT (битый/пустой
        # аудио) → пустой протокол в группу НЕ шлём.
        d = evaluate(end_reason="evicted_normal_end", turn_count=0, utter_count=0)
        self.assertTrue(d.suppressed)
        # это НЕ bot_alerted-причина → finalize пошлёт личный owner-alert.
        self.assertFalse(d.bot_already_alerted)

    def test_eviction_at_end_of_full_meeting_delivered(self):
        # Дословный критерий: «встреча, завершившаяся выбросом-в-конце» → протокол
        # идёт в группу как обычно (endReason помечен normal_end Ф2-стороной).
        d = evaluate(end_reason="evicted_normal_end", turn_count=64, utter_count=140)
        self.assertTrue(d.deliver)


class TestDeafAfterStart(unittest.TestCase):
    """`deaf_after_start` — подавить только если несущественно (на усмотрение Ф3)."""

    def test_deaf_after_start_no_speech_suppressed(self):
        d = evaluate(end_reason="deaf_after_start", turn_count=1, utter_count=1)
        self.assertTrue(d.suppressed)
        self.assertTrue(d.bot_already_alerted)  # бот уже слал ЛИЧНЫЙ deaf-алерт

    def test_deaf_after_start_with_recording_delivered(self):
        # Оглох ПОСЛЕ реальной записи → отдать собранный протокол.
        d = evaluate(end_reason="deaf_after_start", turn_count=40, utter_count=90)
        self.assertTrue(d.deliver)


class TestLegacyRegressions(unittest.TestCase):
    """Регресс: старое поведение гейта (Ф1/ISS-20) не сломано."""

    def test_no_one_joined_suppressed(self):
        d = evaluate(end_reason="no_one_joined", turn_count=0, utter_count=0)
        self.assertTrue(d.suppressed)
        self.assertFalse(d.bot_already_alerted)  # finalize шлёт личный алерт (как раньше)

    def test_no_one_joined_suppressed_even_with_stray_turns(self):
        # Жёсткая причина подавляет ВСЕГДА, без оглядки на объём.
        d = evaluate(end_reason="no_one_joined", turn_count=5, utter_count=7)
        self.assertTrue(d.suppressed)

    def test_deaf_with_participants_suppressed(self):
        d = evaluate(end_reason="deaf_with_participants", turn_count=0, utter_count=0)
        self.assertTrue(d.suppressed)
        self.assertFalse(d.bot_already_alerted)

    def test_empty_external_recording_suppressed_no_reason(self):
        # utter==0 без явной причины (пустая запись внешнего STT) → подавить, личный
        # алерт (как в ISS-20).
        d = evaluate(end_reason="", turn_count=0, utter_count=0)
        self.assertTrue(d.suppressed)
        self.assertFalse(d.bot_already_alerted)

    def test_normal_meeting_delivered(self):
        # Обычная штатная встреча (нет endReason, есть реплики) → доставить.
        d = evaluate(end_reason="", turn_count=50, utter_count=120)
        self.assertTrue(d.deliver)
        self.assertFalse(d.suppressed)

    def test_internal_backend_normal_not_falsely_failed(self):
        # Внутренний whisper_pyannote (utter_count=None) со штатной встречей не
        # помечается провалом ложно (utter==0 не срабатывает на None).
        d = evaluate(end_reason="", turn_count=30, utter_count=None)
        self.assertTrue(d.deliver)


class TestSubstantialityThreshold(unittest.TestCase):
    """Граница порога существенности (FU-4)."""

    def test_below_threshold_evicted_failure_suppressed(self):
        d = evaluate(end_reason="evicted_failure", turn_count=MIN - 1, utter_count=MIN - 1)
        self.assertTrue(d.suppressed)

    def test_at_threshold_evicted_failure_delivered(self):
        # Ровно на пороге → существенно → доставить (биас в пользу доставки, РИСК1).
        d = evaluate(end_reason="evicted_failure", turn_count=MIN, utter_count=MIN)
        self.assertTrue(d.deliver)
        self.assertTrue(d.substantial)

    def test_utter_count_priority_over_turns(self):
        # Монолог: 1 turn, но много реплик внешнего STT → существенно по utter_count.
        d = evaluate(end_reason="evicted_failure", turn_count=1, utter_count=MIN + 5)
        self.assertTrue(d.deliver)

    def test_reason_normalized_whitespace(self):
        # endReason с пробелами нормализуется (strip) — метка матчится.
        d = evaluate(end_reason="  evicted_failure  ", turn_count=0, utter_count=0)
        self.assertTrue(d.suppressed)
        self.assertEqual(d.reason, "evicted_failure")


if __name__ == "__main__":
    unittest.main()
