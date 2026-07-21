# -*- coding: utf-8 -*-
"""Тесты Ф4 (план `2026-07-16-open-questions-reply-fix.md`, ISS-36 REQ 7.1/7.3):
матчинг висяка по НОМЕРУ/ПОРЯДКУ (ординал) в ТЕКСТЕ — на обеих reply-поверхностях
(дайджест И протокол), резолв против СЛЕПКА ИМЕННО отвечаемого документа (Гейт 2 Q1).

Что проверяем:
  • REQ 7.1 — детерминированный ordinal-детектор (RISK7 — косвенные падежи, число словом,
    цифры «задача N»/«п.N»/лидирующий «N.», нормализация «3»↔«три»);
  • Гейт 2 Q1 — резолв против слепка ОТВЕЧАЕМОГО документа (дайджест ИЛИ конкретная
    версия протокола по reply_to message_id), НЕ против live-порядка;
  • REQ 7.3 — ординал приоритетнее overlap; несколько чисел / вне диапазона → unresolved,
    честный остаток; нет слепка на message_id → ординал недоступен → overlap как до Ф4;
  • ординал НЕ ломает существующий overlap-матчинг;
  • реалистичные ASR-строки Whisper (косвенные падежи, число словом).

Опасная тройка: детект детерминированный, БЕЗ сети; тексты реплик в лог не уходят.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss36_phase4_ordinal -v
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import series_memory as sm  # noqa: E402
from notary.lib import pending_status_reply as p  # noqa: E402
from notary.lib import mixed_reply_split as mrs  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402

from datetime import date as _date  # noqa: E402

# Ф2/ISS-39 (fix follow-up Ф1): дата слепка нумерации должна быть СВЕЖЕЙ — иначе
# `record_open_tasks_snapshot` прунит запись старше `_KEEP_SNAPSHOT_DAYS` (=45) прямо на
# записи (cutoff = сегодня−45), слепок исчезает и ординал недоступен. Жёсткая «2026-06-03»
# протухла с 2026-07-19. Берём «сегодня» (relative), как и пишет прод по горячим следам
# (A14) → тест снова защитный и immune к ходу календаря.
_RECENT_SNAP_DATE = _date.today().isoformat()


_PROTO5 = """#протоколвстречи 03.06.2026

**Встреча:** Координация.

**Длительность:** 25 мин

**Участники:** Илья Рыбалка

**Транскрипт:** [2026-06-03.md](2026-06-03.md)

---

## 1) Раздел

▪️ Обсудили.

## Задачи

**Илья Рыбалка**
- Подготовить медиаплан на июль
- Прислать расчёт по складу Ozon
- Согласовать бюджет маркетинга
- Закрыть вакансию дизайнера
- Обновить прайс на витрине
"""


def _on():
    """Флаг сплиттера ON (для батч-пути мульти-ординала на дайджесте)."""
    return mock.patch.dict(os.environ, {"ENABLE_MIXED_REPLY_SPLIT": "1"})


# ==========================================================================
# REQ 7.1 — детектор ординалов (детерминированный, RISK7)
# ==========================================================================
class TestDetectOrdinals(unittest.TestCase):
    def _eq(self, text, expected):
        self.assertEqual(p.detect_ordinals(text), expected, msg=repr(text))

    def test_ordinal_words_nominative(self):
        self._eq("первая сделана", {1})
        self._eq("вторая в работе", {2})
        self._eq("десятая закрыта", {10})

    def test_ordinal_words_oblique_cases_risk7(self):
        # RISK7: косвенные падежи — «закрой третью», «сними первую», «по второй».
        self._eq("закрой третью", {3})
        self._eq("сними первую", {1})
        self._eq("по второй уже готово", {2})
        self._eq("восьмую снимаем", {8})
        self._eq("седьмой пункт готов", {7})

    def test_digits_with_keyword(self):
        self._eq("задача 3 снимаем", {3})
        self._eq("п.1 готово", {1})
        self._eq("п 1 готово", {1})
        self._eq("№3 закрыто", {3})
        self._eq("вопрос 4 сделан", {4})

    def test_cardinal_word_after_keyword_risk7(self):
        # Число СЛОВОМ после ключа — нормализация «3»↔«три» = одна позиция.
        self._eq("задачу три сделал", {3})
        self._eq("пункт пять готов", {5})
        self.assertEqual(p.detect_ordinals("задачу три сделал"),
                         p.detect_ordinals("задача 3 сделал"))

    def test_leading_number(self):
        self._eq("3. отчёт готов", {3})
        self._eq("1) медиаплан сделали", {1})

    def test_multiple_distinct_numbers(self):
        self._eq("первая и вторая сделаны", {1, 2})
        self._eq("задачи 3 и 5 сделаны", {3, 5})
        self._eq("пункты 1, 2 готовы", {1, 2})

    def test_out_of_range_number_is_detected_as_number(self):
        # Детектор возвращает номер как есть; диапазон проверяет резолвер.
        self._eq("задача 99 сделано", {99})

    def test_not_ordinal_negatives(self):
        # Не ординалы: даты, кварталы, проценты, идиомы, обычные висяки.
        self._eq("медиаплан сделали", set())
        self._eq("отчёт за 3 квартал готов", set())
        self._eq("готово к 5 числу", set())
        self._eq("на три дня перенесли", set())
        self._eq("15.07 отчёт сдан", set())          # дата (после точки цифра)
        self._eq("в первых числах сделаю", set())     # идиома, мн.ч. не в словаре
        self._eq("бюджет на 80 процентов готов", set())
        self._eq("с 3 и 5 числа", set())              # числа без ключа

    def test_ordinal_word_adjectival_not_reference_r16(self):
        # Цикл5 Ход1 (R16): порядковое СЛОВО как ПРИЛАГАТЕЛЬНОЕ при хвостовом
        # существительном (дата/мера/место/идиома) — НЕ ссылка на пункт. Иначе
        # «первым делом сделал X» ложно закрыло бы задачу №1 (ложно-закрыто хуже
        # ложно-висит). Цифровые формы keyword-anchored уже не страдали; дыра была
        # именно в СЛОВАХ (whole-word по всему тексту).
        self._eq("первым делом сделал медиаплан", set())   # идиома «первым делом»
        self._eq("на первом этаже вопрос решён", set())    # этаж
        self._eq("на втором этаже готово", set())
        self._eq("за третий квартал готово", set())        # квартал СЛОВОМ (тест-дыра)
        self._eq("третьего числа отчёт готов", set())      # дата словом
        self._eq("первого числа сделаю", set())
        self._eq("от первого отдела сделали", set())       # отдел
        self._eq("в первую очередь сделал", set())         # идиома «в первую очередь»
        self._eq("в первый раз получилось", set())         # идиома «в первый раз»
        self._eq("на втором месте", set())                 # место
        self._eq("вторым рейсом отправил", set())          # рейс
        self._eq("в третьем чтении принято", set())        # чтение
        self._eq("на первый год продлили", set())          # год
        self._eq("в первом ряду сидел", set())             # ряд
        self._eq("в первую неделю сделал", set())          # неделя (Ход2 У5)
        self._eq("первую половину закрыл", set())          # половина (Ход2 У5)

    def test_ordinal_word_before_verb_stays_reference(self):
        # Точность guard'а: хвост — НЕ существительное-дата/мера (глагол/местоимение/
        # ключ-слово задачи) → порядковое ОСТАЁТСЯ ссылкой. Страхует от над-подавления
        # (напр. «первую делаю» не должно погибнуть из-за близости к «делом»).
        self._eq("первую делаю", {1})                      # глагол делать ≠ идиома «делом»
        self._eq("первую сделал", {1})                     # «сделал»→«сдел», не «дел»
        self._eq("седьмой пункт готов", {7})               # ключ-слово задачи — законно
        self._eq("первую задачу закрыл", {1})              # ключ-слово задачи — законно
        self._eq("закрой третью", {3})                     # порядковое в конце — законно

    def test_realistic_asr_whisper_strings(self):
        # Строки в стиле расшифровки Whisper (без пунктуации, косвенные падежи).
        self._eq("первую я сделал уже всё закрыто", {1})
        self._eq("вторую пока в работе жду расчёт", {2})
        self._eq("задачу три можно снимать неактуально", {3})

    def test_empty_and_none(self):
        self._eq("", set())
        self.assertEqual(p.detect_ordinals(None), set())


# ==========================================================================
# REQ 7.3 — резолвер против слепка
# ==========================================================================
class TestResolveOrdinalTarget(unittest.TestCase):
    SNAP = ["Илья: A", "Илья: B", "Илья: C", "Илья: D", "Илья: E"]

    def test_no_ordinal_is_none_verdict(self):
        self.assertEqual(p._resolve_ordinal_target("медиаплан сделали", self.SNAP),
                         ("none", None))

    def test_no_snapshot_falls_to_overlap(self):
        # Слепка нет → ('none') → overlap как до Ф4 (аддитивность).
        self.assertEqual(p._resolve_ordinal_target("первая сделана", None), ("none", None))
        self.assertEqual(p._resolve_ordinal_target("первая сделана", "not-a-list"),
                         ("none", None))

    def test_unambiguous_ordinal_matches_keytext(self):
        self.assertEqual(p._resolve_ordinal_target("первая сделана", self.SNAP),
                         ("match", "Илья: A"))
        self.assertEqual(p._resolve_ordinal_target("задача 5 закрыта", self.SNAP),
                         ("match", "Илья: E"))

    def test_multiple_numbers_unresolved(self):
        self.assertEqual(p._resolve_ordinal_target("первая и третья сделаны", self.SNAP),
                         ("unresolved", None))

    def test_out_of_range_unresolved(self):
        self.assertEqual(p._resolve_ordinal_target("задача 99 сделано", self.SNAP),
                         ("unresolved", None))
        self.assertEqual(p._resolve_ordinal_target("задача 0 сделано", self.SNAP),
                         ("none", None))     # 0 не ловится детектором (n>0) → нет ординала

    def test_empty_snapshot_any_ordinal_unresolved(self):
        self.assertEqual(p._resolve_ordinal_target("первая сделана", []),
                         ("unresolved", None))


# ==========================================================================
# apply_status_reply c ординалом (backend-путь до sidecar)
# ==========================================================================
class _SeriesFx(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-06-03", _PROTO5,
                               {"series": "koord", "date": "2026-06-03"})
        self.kt = [pos["key_text"] for pos in sm.ordered_open_tasks(self.sd)]
        self.assertEqual(len(self.kt), 5)

    def tearDown(self):
        self._tmp.cleanup()

    def _status_of(self, key_text):
        store = sm.load_task_status(self.sd)
        rec = store.get(sm._status_key(key_text))
        return rec.get("status") if rec else None


class TestApplyStatusReplyOrdinal(_SeriesFx):
    def test_ordinal_resolves_against_snapshot(self):
        # «первая — сделана» + слепок → задача 1 слепка = done.
        r = p.apply_status_reply("первая — сделана", self.sd, snapshot=self.kt)
        self.assertIsNotNone(r)
        self.assertEqual(r["matched"], self.kt[0])
        self.assertEqual(r["status"], sm.STATUS_DONE)
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)

    def test_ordinal_out_of_range_honest_remainder(self):
        # «задача 99» при 5 висяках → None (honest-остаток), ничего не записано.
        r = p.apply_status_reply("задача 99 — сделано", self.sd, snapshot=self.kt)
        self.assertIsNone(r)
        self.assertEqual(sm.load_task_status(self.sd), {})

    def test_multiple_numbers_unresolved(self):
        r = p.apply_status_reply("задачи 3 и 5 сделаны", self.sd, snapshot=self.kt)
        self.assertIsNone(r)
        self.assertEqual(sm.load_task_status(self.sd), {})

    def test_no_snapshot_falls_to_overlap(self):
        # Слепка нет: «расчёт по складу закрыт» матчится overlap'ом как до Ф4.
        r = p.apply_status_reply("расчёт по складу закрыт", self.sd, snapshot=None)
        self.assertIsNotNone(r)
        self.assertEqual(r["matched"], self.kt[1])   # позиция 2 = расчёт склад
        self.assertEqual(self._status_of(self.kt[1]), sm.STATUS_DONE)

    def test_pure_ordinal_without_snapshot_honest_remainder(self):
        # Нет слепка + чистый ординал (нет содержательного overlap) → honest-остаток.
        r = p.apply_status_reply("первая — сделана", self.sd, snapshot=None)
        self.assertIsNone(r)
        self.assertEqual(sm.load_task_status(self.sd), {})

    def test_ordinal_priority_over_overlap(self):
        # «прайс первая сделали»: overlap звал бы позицию 5 (прайс), ординал → позиция 1.
        r = p.apply_status_reply("прайс первая сделали", self.sd, snapshot=self.kt)
        self.assertEqual(r["matched"], self.kt[0])        # ординал победил
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)
        self.assertIsNone(self._status_of(self.kt[4]))    # прайс НЕ тронут

    def test_same_reply_without_ordinal_uses_overlap(self):
        # Контроль к предыдущему: без ординала «прайс сделали» → позиция 5 (overlap).
        r = p.apply_status_reply("прайс сделали", self.sd, snapshot=self.kt)
        self.assertEqual(r["matched"], self.kt[4])
        self.assertEqual(self._status_of(self.kt[4]), sm.STATUS_DONE)

    def test_ordinal_wait_maps_to_open(self):
        r = p.apply_status_reply("вторую пока в работе", self.sd, snapshot=self.kt)
        self.assertEqual(r["matched"], self.kt[1])
        self.assertEqual(self._status_of(self.kt[1]), sm.STATUS_OPEN)

    def test_ordinal_cancel(self):
        r = p.apply_status_reply("третью снимаем, неактуально", self.sd, snapshot=self.kt)
        self.assertEqual(r["matched"], self.kt[2])
        self.assertEqual(self._status_of(self.kt[2]), sm.STATUS_CANCELLED)


# ==========================================================================
# Роут дайджеста: ординал против слепка ДАЙДЖЕСТА (Гейт 2 Q1)
# ==========================================================================
class _DigestRouteBase(_SeriesFx):
    CHAT = -100500
    DIGEST_MID = 4242
    NOSNAP_MID = 7777

    def setUp(self):
        super().setUp()
        # Слепок ДАЙДЖЕСТА по его message_id (как пишет send_series_digest в Ф3).
        sm.record_open_tasks_snapshot(self.sd, self.DIGEST_MID, self.kt,
                                      surface="digest", date=_RECENT_SNAP_DATE)

    def _msg(self, text, reply_mid=None):
        return {
            "message_id": 999,
            "reply_to_message": {"message_id": reply_mid or self.DIGEST_MID,
                                 "text": "📋 Открытые вопросы"},
            "text": text,
            "date": 1718000000,
        }

    def _run(self, text, canned=None, reply_mid=None):
        sent: list = []
        with _on(), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)):
            claimed = ml.maybe_route_to_digest_reply("tok", self.CHAT,
                                                     self._msg(text, reply_mid))
        return claimed, sent


class TestDigestRouteOrdinal(_DigestRouteBase):
    def test_multi_ordinal_batch_against_digest_snapshot(self):
        # Критерий: «первая — сделана, вторая — в работе» → задача 1 = done, задача 2 = wait.
        canned = {"statuses": ["первая — сделана", "вторая — в работе"],
                  "edits": [], "unclear": []}
        claimed, sent = self._run("первая — сделана, вторая — в работе", canned)
        self.assertTrue(claimed)
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)   # задача 1
        self.assertEqual(self._status_of(self.kt[1]), sm.STATUS_OPEN)   # задача 2 (wait→open)
        self.assertEqual(len(sent), 1)                                  # один объединённый ack

    def test_single_ordinal_fast_path_against_snapshot(self):
        # Одиночный ординал (не пачка) → single-apply путь, резолв против слепка.
        claimed, sent = self._run("первую сделал")
        self.assertTrue(claimed)
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)

    def test_out_of_range_honest_remainder(self):
        # «задача 99» → вне диапазона слепка → honest-остаток (подсказка, не запись).
        claimed, sent = self._run("задача 99 — сделано")
        self.assertTrue(claimed)
        self.assertEqual(sm.load_task_status(self.sd), {})
        self.assertIn("Не понял", sent[0])                             # DIGEST_UNPARSED_HINT

    def test_no_snapshot_on_message_honest_remainder(self):
        # Reply на message БЕЗ слепка (старое сообщение / reissue) + чистый ординал →
        # ординал недоступен, overlap не матчит → honest-остаток (FU-4 граница).
        claimed, sent = self._run("первая — сделана", reply_mid=self.NOSNAP_MID)
        self.assertTrue(claimed)
        self.assertEqual(sm.load_task_status(self.sd), {})
        self.assertIn("Не понял", sent[0])

    def test_ordinal_does_not_break_overlap(self):
        # Не-ординальный reply при живом слепке → overlap работает как раньше.
        claimed, sent = self._run("расчёт по складу — сделано")
        self.assertTrue(claimed)
        self.assertEqual(self._status_of(self.kt[1]), sm.STATUS_DONE)   # позиция 2 (overlap)


# ==========================================================================
# Роут протокола: ординал против слепка ПРОТОКОЛА (Гейт 2 Q1)
# ==========================================================================
class TestProtocolRouteOrdinal(_SeriesFx):
    CHAT = -100777
    PROTO_MID = 5150

    def setUp(self):
        super().setUp()
        # Слепок ПРОТОКОЛА по его message_id (как пишет finalize-meeting в Ф3).
        sm.record_open_tasks_snapshot(self.sd, self.PROTO_MID, self.kt,
                                      surface="protocol", date=_RECENT_SNAP_DATE)

    def _msg(self, text, reply_mid=None):
        return {
            "message_id": 888,
            "reply_to_message": {"message_id": reply_mid or self.PROTO_MID,
                                 "text": "#протоколвстречи"},
            "text": text,
            "date": 1718000000,
        }

    def _run(self, text, reply_mid=None):
        sent: list = []
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda t, c, txt, **k: sent.append(txt)):
            claimed = ml.maybe_route_to_pending_status_reply(
                "tok", self.CHAT, self.CHAT, self._msg(text, reply_mid))
        return claimed, sent

    def test_protocol_reply_ordinal_against_protocol_snapshot(self):
        # Критерий: reply на ПРОТОКОЛ «первая — сделана» → задача 1 слепка протокола = done.
        claimed, sent = self._run("первая — сделана")
        self.assertTrue(claimed)
        self.assertEqual(self._status_of(self.kt[0]), sm.STATUS_DONE)

    def test_protocol_reply_out_of_range_falls_through(self):
        # «задача 99» вне диапазона → apply=None → роут отдаёт шлюзу правок (False).
        claimed, sent = self._run("задача 99 — сделано")
        self.assertFalse(claimed)
        self.assertEqual(sm.load_task_status(self.sd), {})

    def test_protocol_reply_ordinal_no_snapshot_overlap(self):
        # Нет слепка на message → overlap как до Ф4: не-ординальный reply матчится.
        claimed, sent = self._run("расчёт по складу закрыт", reply_mid=9999)
        self.assertTrue(claimed)
        self.assertEqual(self._status_of(self.kt[1]), sm.STATUS_DONE)


# ==========================================================================
# Опасная тройка: детект без сети, тексты реплик не в лог
# ==========================================================================
class TestDangerTriple(_SeriesFx):
    def test_apply_logs_carry_no_reply_text(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.NOTSET)
        secret = "СЕКРЕТНАЯ_МАРЖА_СПИКЕРА"
        with self.assertLogs("notary.pending_status_reply", level="INFO") as cm:
            p.apply_status_reply(f"первая — сделана {secret}", self.sd, snapshot=self.kt)
        blob = "\n".join(cm.output)
        self.assertNotIn(secret, blob)

    def test_unresolved_ordinal_logs_carry_no_reply_text(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.NOTSET)
        secret = "СЕКРЕТНАЯ_ЦИФРА_БЮДЖЕТА"
        with self.assertLogs("notary.pending_status_reply", level="INFO") as cm:
            p.apply_status_reply(f"задача 99 {secret} сделано", self.sd, snapshot=self.kt)
        blob = "\n".join(cm.output)
        self.assertNotIn(secret, blob)

    def test_detect_ordinals_is_offline_pure(self):
        # Детектор не должен трогать сеть/CLI — чистая функция от строки.
        # (Косвенно: никаких импортов claude_cli на пути detect_ordinals.)
        self.assertEqual(p.detect_ordinals("первая сделана"), {1})


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
