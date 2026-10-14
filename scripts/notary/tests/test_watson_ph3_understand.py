# -*- coding: utf-8 -*-
"""Тесты Ф3 (план `2026-07-20-watson-smart-replies-rethink.md`): understand-слой —
ядро переворота обработки ответов сотрудников.

Покрывает бинарные критерии приёмки Ф3:
  • R7 — ИИ разбирает reply; нестандартная формулировка статуса ДОХОДИТ до применения
    БЕЗ словаря (тест со стабом; проверяем, что словарь её реально не ловит);
  • R8 — три ступени уверенности (уверен/мелкое сомнение/вопрос); ступень 3 НЕ пишет;
  • R9 — ИИ недоступен → очевидное словарём применяется, остальное в ОЧЕРЕДЬ; ложных
    закрытий 0; порядок по времени исходного сообщения (out-of-order guard); дозабор;
  • R10 — флаг `ENABLE_REPLY_UNDERSTAND` дефолт-OFF: без флага claude не зовётся, офлайн;
  • R11 — чужие задачи → всегда ступень 3 (не трогаются без подтверждения);
  • R12 — настройка серии → пересылка владельцу + ответ сотруднику (не молчание);
  • R24 — память «слово→класс»: выученное слово во 2-й раз применяется без ИИ-сомнения;
  • R27 — голос на протокол транскрибируется ПЕРЕД разбором (тот же путь, что текст);
  • НЕС2 — единая точка разбора достижима из реального триггера listener (протокол+дайджест);
  • опасная тройка — текст reply/сегментов в лог не уходит.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python -m unittest tests.test_watson_ph3_understand -v
"""
from __future__ import annotations

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
from notary.lib import pending_status_reply as psr  # noqa: E402
from notary.lib import decision_memory as dm  # noqa: E402
from notary.lib import reply_understand_queue as ruq  # noqa: E402
from notary.lib import reply_understand as ru  # noqa: E402
from notary.lib import mixed_reply_split as mrs  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402


_PROTO = """#протоколвстречи 03.06.2026

**Встреча:** Координация по маркетплейсам.

**Участники:** Илья Рыбалка, Татьяна Филиппова

**Транскрипт:** [2026-06-03.md](2026-06-03.md)

---

## Задачи

**Татьяна Филиппова**
- Подготовить медиаплан на июль
- Прислать расчёт по складу Ozon
"""
_ITEM_MEDIA = "Татьяна Филиппова: Подготовить медиаплан на июль"
_ITEM_RASCH = "Татьяна Филиппова: Прислать расчёт по складу Ozon"


def _canned(statuses=None, edits=None, config=None, block=False, unclear=""):
    """Нормализованный результат ИИ-разбора (как из `_parse_understand`) для стаба
    understand_fn. `statuses` — список (text, cls)."""
    return {
        "statuses": [{"text": t, "cls": c} for (t, c) in (statuses or [])],
        "edits": list(edits or []),
        "series_config": list(config or []),
        "block_close": block,
        "unclear": unclear,
    }


class _SeriesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-06-03", _PROTO,
                               {"series": "koord", "date": "2026-06-03"})
        self.memf = str(self.root / "decision-memory.jsonl")
        self._env = mock.patch.dict(os.environ, {
            "ENABLE_REPLY_UNDERSTAND": "1",
            "MEETING_NOTARY_DECISION_MEMORY_FILE": self.memf,
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _status(self):
        return {k: v["status"] for k, v in sm.load_task_status(self.sd).items()}


# ==========================================================================
# decision_memory — R24 (слово→класс) + R20 (журнал) — ОДНО хранилище
# ==========================================================================
class TestDecisionMemory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = Path(self._tmp.name) / "mem.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_remember_recall(self):
        self.assertTrue(dm.remember_word_class("отработала", "done", store_path=self.p))
        self.assertEqual(dm.recall_word_class("я по этому уже отработала", store_path=self.p), "done")
        self.assertIsNone(dm.recall_word_class("совсем другое слово тут", store_path=self.p))

    def test_invalid_class_and_short_word_noop(self):
        self.assertFalse(dm.remember_word_class("слово", "неткласс", store_path=self.p))
        self.assertFalse(dm.remember_word_class("ок", "done", store_path=self.p))  # <4 симв.

    def test_rollback_forgets(self):
        dm.remember_word_class("разрулили", "done", store_path=self.p)
        self.assertEqual(dm.recall_word_class("всё разрулили", store_path=self.p), "done")
        self.assertTrue(dm.forget_word_class("разрулили", store_path=self.p))
        self.assertIsNone(dm.recall_word_class("всё разрулили", store_path=self.p))

    def test_last_learn_wins(self):
        dm.remember_word_class("сняли", "done", store_path=self.p)
        dm.remember_word_class("сняли", "cancelled", store_path=self.p)
        self.assertEqual(dm.recall_word_class("сняли вопрос", store_path=self.p), "cancelled")

    def test_journal_r20_same_store(self):
        # R20: журнал незнакомых слов — ТО ЖЕ хранилище (не второе).
        dm.remember_word_class("отработала", "done", store_path=self.p)
        dm.record_unknown_word("отработала", outcome="done", store_path=self.p)
        evs = dm.journal_entries(store_path=self.p)
        kinds = [e.get("ev") for e in evs]
        self.assertIn("learn", kinds)
        self.assertIn("journal", kinds)
        # журнал не влияет на активную карту, но лежит рядом (одно хранилище)
        self.assertEqual(dm.known_map(store_path=self.p), {"отработала": "done"})

    def test_no_reply_text_stored(self):
        # Опасная тройка: в хранилище только слово+класс, НЕ текст реплики.
        dm.remember_word_class("отработала", "done", source="reply", store_path=self.p)
        blob = self.p.read_text(encoding="utf-8")
        self.assertNotIn("реплик", blob.lower())

    def test_negation_not_recalled(self):
        # Ф3-цикл5: «не <слово>» память НЕ распознаёт — иначе ложное закрытие по отрицанию
        # (класс бага FU-1). Утвердительная форма распознаётся, отрицательная — молчит.
        dm.remember_word_class("разрулили", "done", store_path=self.p)
        self.assertEqual(dm.recall_word_class("всё разрулили", store_path=self.p), "done")
        self.assertIsNone(dm.recall_word_class("пока не разрулили", store_path=self.p))
        self.assertTrue(dm.word_is_negated("склад не разрулили", "разрулили"))
        self.assertFalse(dm.word_is_negated("склад разрулили", "разрулили"))
        # анти-пере-подавление (ход4/НОВ1): короткое «не раз» (=многократно, УТВЕРЖДЕНИЕ)
        # НЕ должно гасить «разрулили» из-за префикса «раз».
        self.assertEqual(dm.recall_word_class("не раз склад разрулили", store_path=self.p), "done")
        self.assertFalse(dm.word_is_negated("не раз склад разрулили", "разрулили"))


# ==========================================================================
# reply_understand_queue — R9 механика (порядок, out-of-order, TTL, дозабор)
# ==========================================================================
class TestUnderstandQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sd = Path(self._tmp.name) / "koord"
        self.sd.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_enqueue_survives_reload(self):
        # Очередь — ФАЙЛ (переживает рестарт listener): пишем, читаем свежим load.
        ruq.enqueue(self.sd, orig_ts=100.0, chat_id=1, msg_id=5, reply_text="склад готов")
        self.assertTrue((self.sd / ruq.QUEUE_FILE).exists())
        pend = ruq.list_pending(self.sd)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["reply_text"], "склад готов")

    def test_order_by_orig_ts(self):
        ruq.enqueue(self.sd, orig_ts=200.0, chat_id=1, msg_id=2, reply_text="b")
        ruq.enqueue(self.sd, orig_ts=100.0, chat_id=1, msg_id=1, reply_text="a")
        order = [e["reply_text"] for e in ruq.list_pending(self.sd)]
        self.assertEqual(order, ["a", "b"])  # по времени ИСХОДНОГО сообщения

    def test_dedup_by_msg_id(self):
        ruq.enqueue(self.sd, orig_ts=100.0, chat_id=1, msg_id=5, reply_text="v1")
        ruq.enqueue(self.sd, orig_ts=101.0, chat_id=1, msg_id=5, reply_text="v2")
        self.assertEqual(len(ruq.list_pending(self.sd)), 1)

    def test_out_of_order_guard(self):
        # Поздний ответ (ts=200) тронул задачу → отложенный ранний (ts=100) НЕ применяется.
        ruq.record_applied(self.sd, "k1", 200.0)
        self.assertTrue(ruq.is_superseded(self.sd, "k1", 100.0))
        self.assertFalse(ruq.is_superseded(self.sd, "k1", 300.0))

    def test_record_applied_keeps_max(self):
        ruq.record_applied(self.sd, "k1", 100.0)
        ruq.record_applied(self.sd, "k1", 50.0)   # раньше — не понижаем
        self.assertTrue(ruq.is_superseded(self.sd, "k1", 80.0))

    def test_drain_removes_processed(self):
        ruq.enqueue(self.sd, orig_ts=100.0, chat_id=1, msg_id=1, reply_text="a")
        ruq.enqueue(self.sd, orig_ts=200.0, chat_id=1, msg_id=2, reply_text="b")
        seen = []
        ruq.drain(self.sd, lambda e: seen.append(e["reply_text"]) or True)
        self.assertEqual(seen, ["a", "b"])          # порядок сохранён
        self.assertEqual(ruq.list_pending(self.sd), [])  # разобранные убраны

    def test_ttl_prune(self):
        # Ретеншн по времени ПОСТАНОВКИ в очередь (enqueued_ts). Задаём `now` обоим, чтобы
        # проверка была детерминированной (не зависела от реального wall-clock).
        t0 = 1_000_000.0
        later = t0 + (ruq._RETENTION_DAYS + 1) * 86400
        ruq.enqueue(self.sd, orig_ts=t0, chat_id=1, msg_id=1, reply_text="old", now=t0)
        ruq.enqueue(self.sd, orig_ts=later, chat_id=1, msg_id=2, reply_text="fresh", now=later)
        pend = ruq.list_pending(self.sd)
        self.assertEqual([e["reply_text"] for e in pend], ["fresh"])  # старое (пролежало >TTL) выпрунено


# ==========================================================================
# understand-слой — R10 флаг, R7 без словаря, R8 ступени, R11, R12, edit-origin
# ==========================================================================
class TestUnderstandCore(_SeriesBase):

    def test_r10_flag_off_no_llm(self):
        # R10: без флага understand_fn НЕ зовётся, engaged=False (claude не дёрнут).
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_REPLY_UNDERSTAND", None)
            fn = mock.Mock()
            r = ru.understand_reply("склад готов", series_dir=self.sd, understand_fn=fn,
                                    replier="Татьяна", date="2026-06-10")
        self.assertFalse(r["engaged"])
        fn.assert_not_called()

    def test_r7_nonstandard_word_reaches_apply_without_dict(self):
        # R7: «разрулили» словарь НЕ ловит, ИИ-класс done → статус ПРОСТАВЛЕН (без словаря).
        self.assertIsNone(psr.detect_status_intent("по складу склад разрулили"))
        stub = lambda rt, it, snap=None: _canned([("по складу склад разрулили", "done")])
        r = ru.understand_reply("по складу склад разрулили", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(r["applied"][0]["source"], "ai_only")
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_DONE)

    def test_r8_tier1_dict_strong_own(self):
        # Ступень 1: словарь + strong-матч + своя задача → делает, tier=1.
        stub = lambda rt, it, snap=None: _canned([("по расчёту Ozon всё готово", "done")])
        r = ru.understand_reply("по расчёту Ozon всё готово", series_dir=self.sd,
                                replier="Татьяна Филиппова", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(r["applied"][0]["tier"], ru.TIER_CONFIDENT)
        self.assertEqual(r["applied"][0]["source"], "dict")

    def test_iss46_already_closed_not_reclosed(self):
        # ISS-46 (владелец 23.07): задача УЖЕ done → повторное «закрой» НЕ переписывает и
        # не «отмечает закрытым», а сообщает «уже» (bucket already). Корень бага караоке.
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_DONE, source="earlier", date="2026-06-05")
        stub = lambda rt, it, snap=None: _canned([("по расчёту Ozon всё готово", "done")])
        r = ru.understand_reply("по расчёту Ozon всё готово", series_dir=self.sd,
                                replier="Татьяна Филиппова", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["applied"]), 0)                    # повторно НЕ применили
        self.assertEqual(len(r["already"]), 1)                    # сообщили «уже»
        self.assertEqual(r["already"][0]["matched"], _ITEM_RASCH)
        self.assertTrue(r["engaged"])                             # не молчим

    def test_iss46_reopen_on_closed_still_works(self):
        # Гейт «уже» — ТОЛЬКО про тот же статус. Смена (reopen: done→open) проходит.
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_DONE, source="earlier", date="2026-06-05")
        stub = lambda rt, it, snap=None: _canned([("по расчёту Ozon верните", "reopen")])
        r = ru.understand_reply("по расчёту Ozon верните", series_dir=self.sd,
                                replier="Татьяна Филиппова", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["already"]), 0)                    # не «уже» — статус меняется
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_OPEN)

    def test_iss46_cancelled_then_done_changes_status(self):
        # Терминальный, но ДРУГОЙ статус (cancelled → done) — не «уже», меняем.
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_CANCELLED, source="earlier", date="2026-06-05")
        stub = lambda rt, it, snap=None: _canned([("по расчёту Ozon сделано", "done")])
        r = ru.understand_reply("по расчёту Ozon сделано", series_dir=self.sd,
                                replier="Татьяна Филиппова", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["already"]), 0)
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_DONE)

    def test_r8_tier2_minor_doubt_ai_only(self):
        # Ступень 2: слово знал только ИИ → делает, но tier=2 (мелкое сомнение).
        stub = lambda rt, it, snap=None: _canned([("по складу склад разрулили", "done")])
        r = ru.understand_reply("по складу склад разрулили", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub, date="2026-06-10")
        self.assertEqual(r["applied"][0]["tier"], ru.TIER_MINOR)

    def test_r8_tier3_no_match_not_written(self):
        # Ступень 3: кандидат ∉ универсум → ВОПРОС, статус НЕ пишется.
        stub = lambda rt, it, snap=None: _canned([("по проекту альфа всё сделали", "done")])
        r = ru.understand_reply("по проекту альфа всё сделали", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["applied"]), 0)
        self.assertTrue(any(q["kind"] == "no_match" for q in r["questions"]))
        self.assertEqual(self._status(), {})   # ничего не закрыто

    def test_r11_foreign_task_tier3_not_written(self):
        # R11: та же формулировка, но ответил ЧУЖОЙ (Илья на задачу Татьяны) → ступень 3.
        stub = lambda rt, it, snap=None: _canned([("по расчёту Ozon всё готово", "done")])
        r = ru.understand_reply("по расчёту Ozon всё готово", series_dir=self.sd,
                                replier="Илья Рыбалка", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["applied"]), 0)
        self.assertTrue(any(q["kind"] == "foreign" for q in r["questions"]))
        self.assertEqual(self._status(), {})

    def test_own_task_applies_when_foreign_check_passes(self):
        # Контроль к R11: своя задача (Татьяна) при тех же данных — применяется.
        stub = lambda rt, it, snap=None: _canned([("по расчёту Ozon всё готово", "done")])
        r = ru.understand_reply("по расчёту Ozon всё готово", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["applied"]), 1)

    def test_r12_series_config_forwarded_not_applied(self):
        # R12: настройка серии → в config_requests, статус НЕ трогается.
        stub = lambda rt, it, snap=None: _canned(config=["не включай блок задач в протокол"])
        r = ru.understand_reply("не включай блок задач в протокол", series_dir=self.sd,
                                replier="Илья", understand_fn=stub, date="2026-06-10")
        self.assertEqual(r["config_requests"], ["не включай блок задач в протокол"])
        self.assertEqual(self._status(), {})

    def test_config_deterministic_net_without_ai_flag(self):
        # R12: детерминированный нетто-детектор ловит настройку даже без ИИ-метки.
        stub = lambda rt, it, snap=None: _canned(unclear="не присылай дайджест по этой серии")
        r = ru.understand_reply("не присылай дайджест по этой серии", series_dir=self.sd,
                                replier="Илья", understand_fn=stub, date="2026-06-10")
        self.assertTrue(r["config_requests"])

    def test_edit_marker_status_goes_to_edits(self):
        # ISS-37: статус-сегмент с маркером правки — это правка, не закрытие (в edits).
        stub = lambda rt, it, snap=None: _canned([("вместо Zil правильно Zeal, склад", "done")])
        r = ru.understand_reply("вместо Zil правильно Zeal, склад", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub, date="2026-06-10")
        self.assertEqual(len(r["applied"]), 0)
        self.assertTrue(r["edits"])
        self.assertEqual(self._status(), {})

    def test_pure_edit_does_not_engage(self):
        # РИСК1: чистая правка (без статуса) — работа петли правок, understand НЕ вовлекается
        # (не крадёт правку и её дебаунс-ack), чтобы reply ушёл в шлюз правок как раньше.
        stub = lambda rt, it, snap=None: _canned(edits=["не Дрим Стори, а 23МПКТК"])
        r = ru.understand_reply("не Дрим Стори, а 23МПКТК", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub, date="2026-06-10")
        self.assertFalse(r["engaged"])
        self.assertEqual(len(r["applied"]), 0)

    def test_opasnaya_troika_no_reply_text_in_logs(self):
        # Текст reply в лог understand-слоя не уходит (только счётчики/классы).
        secret = "СЕКРЕТ_ПРО_МАРЖУ_И_ЗАКУПКУ"
        stub = lambda rt, it, snap=None: _canned([("по складу склад разрулили", "done")])
        with self.assertLogs("notary.reply_understand", level="INFO") as cm:
            ru.understand_reply(secret + " по складу склад разрулили", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub, date="2026-06-10")
        self.assertNotIn(secret, "\n".join(cm.output))


# ==========================================================================
# R9 — ИИ недоступен: очевидное словарём, остальное в очередь, out-of-order
# ==========================================================================
class TestUnderstandFallbackR9(_SeriesBase):

    def test_ai_down_single_obvious_applied_no_queue(self):
        # Одиночный очевидный статус словарём применяется сразу, очередь пуста.
        r = ru.understand_reply("расчёт по складу Ozon закрыли", series_dir=self.sd,
                                replier="Татьяна", understand_fn=lambda rt, it, snap=None: None,
                                date="2026-06-10", orig_ts=1000.0)
        self.assertTrue(r["deferred"] is False or r["applied"])
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(ru.ruq.list_pending(self.sd), [])
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_DONE)

    def test_ai_down_multi_no_false_close_queued(self):
        # Пачка при недоступном ИИ: НЕ гадаем (0 закрытий), reply в очередь (deferred).
        r = ru.understand_reply("склад готов\nмедиаплан снят", series_dir=self.sd,
                                replier="Татьяна", understand_fn=lambda rt, it, snap=None: None,
                                date="2026-06-10", orig_ts=1000.0)
        self.assertTrue(r["deferred"])
        self.assertEqual(self._status(), {})              # ложных закрытий 0
        self.assertEqual(len(ru.ruq.list_pending(self.sd)), 1)

    def test_ai_down_pure_edit_steps_aside(self):
        # ИИ-down + чистая пачка правок (нет статус-маркера) → understand НЕ берёт
        # (engaged=False), НЕ копит: обычный шлюз правок отработает как сегодня.
        r = ru.understand_reply("вместо Zil правильно Zeal, замените склад на склад-2",
                                series_dir=self.sd, replier="Татьяна",
                                understand_fn=lambda rt, it, snap=None: None, date="2026-06-10", orig_ts=1000.0)
        self.assertFalse(r["engaged"])
        self.assertEqual(ru.ruq.list_pending(self.sd), [])
        self.assertEqual(self._status(), {})

    def test_out_of_order_deferred_does_not_overwrite(self):
        # Поздний живой ответ закрыл задачу (ts=2000); ранний отложенный (ts=1000) при
        # применении НЕ откатывает — out-of-order guard.
        # 1) поздний живой: reopen расчёта (OPEN) в ts=2000
        stub_reopen = lambda rt, it, snap=None: _canned([("по расчёту Ozon верните, не закрыто", "reopen")])
        ru.understand_reply("по расчёту Ozon верните, не закрыто", series_dir=self.sd,
                            replier="Татьяна", understand_fn=stub_reopen, date="2026-06-10",
                            orig_ts=2000.0)
        self.assertEqual(self._status().get(sm._status_key(_ITEM_RASCH)), sm.STATUS_OPEN)
        # 2) ранний отложенный: done расчёта в ts=1000 — НЕ должен перетереть reopen
        stub_done = lambda rt, it, snap=None: _canned([("по расчёту Ozon всё готово", "done")])
        r = ru.understand_reply("по расчёту Ozon всё готово", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub_done, date="2026-06-10",
                                orig_ts=1000.0)
        self.assertEqual(len(r["applied"]), 0)            # применение подавлено
        self.assertEqual(self._status().get(sm._status_key(_ITEM_RASCH)), sm.STATUS_OPEN)

    def test_drain_deferred_applies_when_ai_back(self):
        # Очередь дозабора: положили при ИИ-down, дренаж стабом-ИИ применяет.
        ru.understand_reply("склад готов\nмедиаплан снят", series_dir=self.sd,
                            replier="Татьяна", understand_fn=lambda rt, it, snap=None: None,
                            date="2026-06-10", orig_ts=1000.0)
        self.assertEqual(len(ru.ruq.list_pending(self.sd)), 1)
        stub = lambda rt, it, snap=None: _canned([("расчёт по складу Ozon закрыли", "done")])
        out = ru.drain_deferred(self.sd, understand_fn=stub)
        self.assertEqual(out["processed"], 1)
        self.assertEqual(ru.ruq.list_pending(self.sd), [])
        self.assertEqual(self._status().get(sm._status_key(_ITEM_RASCH)), sm.STATUS_DONE)


# ==========================================================================
# R24 — память: выученное слово во 2-й раз применяется без ИИ-класса
# ==========================================================================
class TestUnderstandMemoryR24(_SeriesBase):

    def test_learned_word_applies_second_time_without_ai_class(self):
        # 1-й раз: ИИ классифицировал «разрулили»→done → применено И запомнено.
        stub1 = lambda rt, it, snap=None: _canned([("по складу склад разрулили", "done")])
        ru.understand_reply("по складу склад разрулили", series_dir=self.sd,
                            replier="Татьяна", understand_fn=stub1, date="2026-06-10")
        self.assertEqual(dm.recall_word_class("разрулили", store_path=Path(self.memf)), "done")
        # сбросим статус, чтобы проверить повторное применение начисто
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_OPEN)
        # 2-й раз: ИИ НЕ дал класс (cls=None) — память узнаёт слово → применяет БЕЗ вопроса.
        stub2 = lambda rt, it, snap=None: _canned([("по складу склад разрулили", None)])
        r = ru.understand_reply("по складу склад разрулили", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub2, date="2026-06-11")
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(r["applied"][0]["source"], "memory")
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_DONE)

    def test_negated_learned_word_not_false_closed(self):
        # Ф3-цикл5: выучили «разрулили»→done; ОТРИЦАНИЕ «не разрулили» НЕ закрывает задачу
        # (класс бага FU-1). ИИ без класса → память на отрицании молчит → ступень-3, не пишем.
        stub1 = lambda rt, it, snap=None: _canned([("по складу склад разрулили", "done")])
        ru.understand_reply("по складу склад разрулили", series_dir=self.sd,
                            replier="Татьяна", understand_fn=stub1, date="2026-06-10")
        sm.set_task_status(self.sd, _ITEM_RASCH, sm.STATUS_OPEN)
        stub2 = lambda rt, it, snap=None: _canned([("по складу склад не разрулили", None)])
        r = ru.understand_reply("по складу склад не разрулили", series_dir=self.sd,
                                replier="Татьяна", understand_fn=stub2, date="2026-06-11")
        self.assertEqual(len(r["applied"]), 0)                       # НЕ применено
        self.assertEqual(self._status().get(sm._status_key(_ITEM_RASCH)), sm.STATUS_OPEN)

    def test_no_poison_learn_from_negated_segment(self):
        # Ф3-цикл5: «не раскидали»→reopen (ИИ) НЕ должно выучить «раскидали»→reopen —
        # утвердительное «раскидали» = сделал. Из отрицательного сегмента слово не учим.
        stub = lambda rt, it, snap=None: _canned([("по складу склад не раскидали", "reopen")])
        ru.understand_reply("по складу склад не раскидали", series_dir=self.sd,
                            replier="Татьяна", understand_fn=stub, date="2026-06-10")
        self.assertIsNone(dm.recall_word_class("раскидали", store_path=Path(self.memf)))


# ==========================================================================
# НЕС2 / R27 — достижимость из реального триггера listener (протокол + дайджест + голос)
# ==========================================================================
class TestUnderstandListenerRoute(_SeriesBase):
    GROUP = 222
    OWNER = 111

    def _msg(self, text, *, first_name="Татьяна", voice=False):
        m = {
            "chat": {"id": self.GROUP},
            "from": {"id": 777, "first_name": first_name},
            "reply_to_message": {"message_id": 555, "text": "📅 протокол"},
            "message_id": 999, "date": 1718000000,
        }
        if voice:
            m["voice"] = {"file_id": "v"}
        else:
            m["text"] = text
        return m

    def test_route_off_flag_returns_false(self):
        # R10 на уровне listener: флаг OFF → роут False, _request_understand не зван.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_REPLY_UNDERSTAND", None)
            with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
                 mock.patch.object(ml, "_protokol_root", return_value=self.root), \
                 mock.patch.object(ru, "_request_understand") as req:
                claimed = ml.maybe_route_to_reply_understand(
                    "tok", self.GROUP, self.OWNER, self._msg("склад готов"))
        self.assertFalse(claimed)
        req.assert_not_called()

    def test_protocol_route_applies_and_acks(self):
        # НЕС2: understand достижим из maybe_route_to_reply_understand; статус проставлен, ack.
        sent = []
        stub = lambda rt, it, snap=None: _canned([("по складу склад разрулили", "done")])
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ru, "_request_understand", stub), \
             mock.patch.object(ml, "send_message", side_effect=lambda *a, **k: sent.append(a)):
            claimed = ml.maybe_route_to_reply_understand(
                "tok", self.GROUP, self.OWNER, self._msg("по складу склад разрулили"))
        self.assertTrue(claimed)
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_DONE)
        self.assertEqual(len(sent), 1)                       # один ack

    def test_immediate_correction_reaches_feedback_gateway(self):
        from notary.lib import feedback_worker
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_understand_and_ack") as understand, \
             mock.patch.object(feedback_worker, "wants_immediate_reissue", return_value=True), \
             mock.patch.object(ml, "maybe_route_to_mixed_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_pending_status_reply", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_feedback_reply", return_value=True) as gateway:
            ml.process_message(
                "tok", self.OWNER, self._msg("Перепутал Илью и Михаила местами. Делай сразу"))
        gateway.assert_called_once()
        understand.assert_not_called()

    def test_vague_protocol_complaint_asks_about_error(self):
        sent = []
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_understand_and_ack") as understand, \
             mock.patch.object(ml, "send_message", side_effect=lambda *a, **k: sent.append(a[2])):
            claimed = ml.maybe_route_to_reply_understand(
                "tok", self.GROUP, self.OWNER, self._msg("Ох, ты все напутал )"))
        self.assertTrue(claimed)
        self.assertIn("чьи слова", sent[0])
        understand.assert_not_called()

    def test_protocol_route_edits_go_to_gateway(self):
        # РИСК1: правочный сегмент уходит в СУЩЕСТВУЮЩИЙ шлюз правок (не теряется).
        edit_calls = []

        def _fake_gateway(token, chat_id, edit_msg, *, allowed_chat, send=None):
            edit_calls.append(edit_msg.get("text"))
            if send:
                send(token, chat_id, "ack")
            return True

        stub = lambda rt, it, snap=None: _canned([("расчёт по складу Ozon закрыли", "done")],
                                      edits=["не Дрим Стори, а 23МПКТК"])
        from notary.lib import feedback_worker
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ru, "_request_understand", stub), \
             mock.patch.object(feedback_worker, "route_feedback_reply", _fake_gateway), \
             mock.patch.object(ml, "send_message"):
            claimed = ml.maybe_route_to_reply_understand(
                "tok", self.GROUP, self.OWNER, self._msg("склад закрыли и правка"))
        self.assertTrue(claimed)
        self.assertEqual(len(edit_calls), 1)
        self.assertIn("23МПКТК", edit_calls[0])

    def test_protocol_route_config_forwards_to_owner(self):
        # R12 на уровне listener: настройка → форвард владельцу + ответ сотруднику.
        sent = []
        pushed = []
        stub = lambda rt, it, snap=None: _canned(config=["не включай блок задач в протокол"])
        from notary.lib import notify
        # autospec=True → мок ЭНФОРСИТ реальную сигнатуру push (ловит неверный kwarg типа
        # `identity=` — в проде он бы уронил форвард в try/except молча).
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ru, "_request_understand", stub), \
             mock.patch.object(notify, "push", autospec=True,
                               side_effect=lambda *a, **k: pushed.append((a, k)) or True), \
             mock.patch.object(ml, "send_message", side_effect=lambda *a, **k: sent.append(a[2])):
            claimed = ml.maybe_route_to_reply_understand(
                "tok", self.GROUP, self.OWNER, self._msg("не включай блок задач в протокол"))
        self.assertTrue(claimed)
        self.assertEqual(len(pushed), 1)                     # владельцу переслали
        self.assertTrue(any("формат протокола решает он" in s for s in sent))  # сотруднику — не молчание
        self.assertEqual(self._status(), {})                 # формат сам не поменяли

    def test_r27_voice_transcribed_before_parse(self):
        # R27: голос на протокол под флагом → расшифровка ПЕРЕД разбором (text подставлен).
        with mock.patch.dict(os.environ, {"ENABLE_PROTOCOL_VOICE_REPLY": "1"}), \
             mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "transcribe_voice_or_none", return_value="по складу склад разрулили"):
            out = ml._maybe_transcribe_protocol_voice("tok", self.GROUP, self._msg("x", voice=True))
        self.assertEqual(out.get("text"), "по складу склад разрулили")
        self.assertNotIn("voice", out)

    def test_r27_voice_off_flag_untouched(self):
        # Флаг OFF → голос не трогаем (обратная совместимость: уходит в шлюз правок).
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_PROTOCOL_VOICE_REPLY", None)
            m = self._msg("x", voice=True)
            out = ml._maybe_transcribe_protocol_voice("tok", self.GROUP, m)
        self.assertIn("voice", out)
        self.assertNotIn("text", out)

    def test_process_message_dispatches_to_understand(self):
        # Достижимость из РЕАЛЬНОГО триггера: process_message → maybe_route_to_reply_understand
        # (не изолированный вызов роута). Статус реально проставлен через полный dispatch.
        stub = lambda rt, it, snap=None: _canned([("по складу склад разрулили", "done")])
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ru, "_request_understand", stub), \
             mock.patch.object(ml, "send_message"):
            ml.process_message("tok", self.OWNER, self._msg("по складу склад разрулили"))
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_DONE)

    def test_digest_route_understand_reachable(self):
        # НЕС2: understand достижим и на дайджест-роуте (_apply_digest_understand).
        sent = []
        stub = lambda rt, it, snap=None: _canned([("по складу склад разрулили", "done")])
        with mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ru, "_request_understand", stub), \
             mock.patch.object(ml, "send_message", side_effect=lambda *a, **k: sent.append(a)):
            claimed = ml._apply_digest_understand(
                "tok", self.GROUP, "koord", "по складу склад разрулили", self._msg("по складу склад разрулили"))
        self.assertTrue(claimed)
        self.assertEqual(self._status()[sm._status_key(_ITEM_RASCH)], sm.STATUS_DONE)


if __name__ == "__main__":
    unittest.main()
