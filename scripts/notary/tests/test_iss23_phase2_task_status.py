"""Тесты Ф2 (план `2026-06-24-pending-items-lifecycle.md`) — sidecar-хранилище
статусов висяка, переживающее регенерацию протокола.

Покрывает критерий «сделано» Ф2:
  - sidecar `task-status.json` keyed по `_status_key`, который финализация НЕ
    пересобирает (статус ПЕРЕЖИВАЕТ регенерацию);
  - merge-on-render: терминально закрытые (отменён/сделано/авто) НЕ воскресают как
    «висит»; «под сомнением» — отдельная корзина; «показано» (R21) убирает из выдачи;
  - совместимость/миграция: `resolve_open_tasks` терпит объект `{'text': …}`, старые
    `list[str]` проходят без потерь;
  - стабильность ключа: `_status_key` срезает «(срок: …)» → статус матчится при
    переформулировке срока между встречами;
  - приватность (опасная тройка): тексты задач/причин не уходят в лог.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss23_phase2_task_status -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import series_memory as sm  # noqa: E402


# Протокол встречи с блоком «Задачи» (буллеты под `**Имя**`) — даёт непустой хвост.
_PROTO_TASKS = """#протоколвстречи 03.06.2026

**Встреча:** Координация по маркетплейсам.

**Длительность:** 25 мин

**Участники:** Илья Рыбалка, Татьяна Филиппова

**Транскрипт:** [2026-06-03.md](2026-06-03.md)

---

## 1) Маркетплейсы

▪️ Обсудили план на июль.

## Задачи

**Татьяна Филиппова**
- Подготовить медиаплан на июль
- Прислать расчёт по складу Ozon
"""


# ==========================================================================
# Ключ статуса: срез «(срок: …)» для стабильности к переформулировке срока
# ==========================================================================
class TestStatusKey(unittest.TestCase):

    def test_strips_due_suffix(self):
        a = sm._status_key("Татьяна: Ozon-доставка (срок: пятница)")
        b = sm._status_key("Татьяна: Ozon-доставка (срок: к 15.06)")
        c = sm._status_key("Татьяна: Ozon-доставка")
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_equals_task_key_without_due(self):
        self.assertEqual(sm._status_key("Сделать X"), sm._task_key("Сделать X"))

    def test_normalizes_case_and_emphasis(self):
        self.assertEqual(
            sm._status_key("**Сделать X**"), sm._status_key("сделать x")
        )

    def test_due_only_at_tail(self):
        # «срок:» в СЕРЕДИНЕ текста не срезается (это часть формулировки).
        k = sm._status_key("обсудить (срок: вчера) и закрыть")
        self.assertIn("закрыть", k)


# ==========================================================================
# Sidecar: load/save/set — roundtrip и устойчивость к мусору
# ==========================================================================
class TestSidecarStore(unittest.TestCase):

    def test_load_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(sm.load_task_status(Path(td)), {})

    def test_load_none_series_dir(self):
        self.assertEqual(sm.load_task_status(None), {})

    def test_load_garbage_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            sm.task_status_path(Path(td)).write_text("{ not json", encoding="utf-8")
            self.assertEqual(sm.load_task_status(Path(td)), {})

    def test_load_drops_invalid_status(self):
        with tempfile.TemporaryDirectory() as td:
            payload = {"schema": 1, "items": {
                "k1": {"status": "done"},
                "k2": {"status": "ЧУШЬ"},        # неизвестный статус → отброшен
                "k3": "не объект",               # не dict → отброшен
            }}
            sm.task_status_path(Path(td)).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            store = sm.load_task_status(Path(td))
            self.assertIn("k1", store)
            self.assertNotIn("k2", store)
            self.assertNotIn("k3", store)

    def test_set_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            rec = sm.set_task_status(sd, "Татьяна: прислать расчёт", sm.STATUS_DONE,
                                     reason="по встрече", source="reply", date="2026-06-10")
            self.assertEqual(rec["status"], sm.STATUS_DONE)
            self.assertFalse(rec["shown"])
            store = sm.load_task_status(sd)
            key = sm._status_key("Татьяна: прислать расчёт")
            self.assertIn(key, store)
            self.assertEqual(store[key]["reason"], "по встрече")
            self.assertEqual(store[key]["source"], "reply")
            self.assertEqual(store[key]["text"], "Татьяна: прислать расчёт")

    def test_set_unknown_status_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                sm.set_task_status(Path(td), "X", "не-статус")

    def test_set_empty_task_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(sm.set_task_status(Path(td), "   ", sm.STATUS_DONE))

    def test_status_change_resets_shown(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            sm.set_task_status(sd, "задача Z", sm.STATUS_DONE)
            self.assertTrue(sm.mark_status_shown(sd, "задача Z"))
            self.assertTrue(sm.load_task_status(sd)[sm._status_key("задача Z")]["shown"])
            # смена статуса (auto_closed) → shown снова False (надо показать заново)
            sm.set_task_status(sd, "задача Z", sm.STATUS_AUTO_CLOSED)
            self.assertFalse(sm.load_task_status(sd)[sm._status_key("задача Z")]["shown"])

    def test_same_status_keeps_shown(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            sm.set_task_status(sd, "задача Z", sm.STATUS_DONE)
            sm.mark_status_shown(sd, "задача Z")
            sm.set_task_status(sd, "задача Z", sm.STATUS_DONE)  # тот же статус
            self.assertTrue(sm.load_task_status(sd)[sm._status_key("задача Z")]["shown"])

    def test_reopen_clears_reason_and_shown(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            sm.set_task_status(sd, "вопрос Q", sm.STATUS_AUTO_CLOSED, reason="по чату")
            sm.mark_status_shown(sd, "вопрос Q")
            rec = sm.set_task_status(sd, "вопрос Q", sm.STATUS_OPEN)  # переоткрыли (A10)
            self.assertEqual(rec["status"], sm.STATUS_OPEN)
            self.assertIsNone(rec["reason"])
            self.assertFalse(rec["shown"])


# ==========================================================================
# mark_status_shown (R21)
# ==========================================================================
class TestMarkShown(unittest.TestCase):

    def test_mark_missing_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(sm.mark_status_shown(Path(td), "нет такой"))

    def test_mark_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            sm.set_task_status(sd, "T", sm.STATUS_CANCELLED)
            self.assertTrue(sm.mark_status_shown(sd, "T"))
            self.assertTrue(sm.mark_status_shown(sd, "T"))  # повтор → True, всё ещё shown


# ==========================================================================
# merge_open_tasks_with_status — ядро Ф2: закрытое не воскресает
# ==========================================================================
class TestMerge(unittest.TestCase):

    def test_no_store_all_open(self):
        fresh = ["A", "B", "C"]
        m = sm.merge_open_tasks_with_status(fresh, {})
        self.assertEqual(m["open"], ["A", "B", "C"])
        self.assertEqual(m["closed"], [])
        self.assertEqual(m["doubt"], [])

    def test_terminal_closed_not_in_open(self):
        fresh = ["A", "B", "C"]
        store = {
            sm._status_key("B"): {"status": sm.STATUS_CANCELLED, "reason": "снят", "shown": False},
        }
        m = sm.merge_open_tasks_with_status(fresh, store)
        self.assertEqual(m["open"], ["A", "C"])           # B не висит
        self.assertEqual([c["text"] for c in m["closed"]], ["B"])
        self.assertEqual(m["closed"][0]["status"], sm.STATUS_CANCELLED)

    def test_all_terminal_statuses_excluded(self):
        fresh = ["done-task", "cancel-task", "auto-task"]
        store = {
            sm._status_key("done-task"): {"status": sm.STATUS_DONE, "shown": False},
            sm._status_key("cancel-task"): {"status": sm.STATUS_CANCELLED, "shown": False},
            sm._status_key("auto-task"): {"status": sm.STATUS_AUTO_CLOSED, "shown": False},
        }
        m = sm.merge_open_tasks_with_status(fresh, store)
        self.assertEqual(m["open"], [])
        self.assertEqual(len(m["closed"]), 3)

    def test_doubt_bucket_not_open(self):
        fresh = ["A", "B"]
        store = {sm._status_key("A"): {"status": sm.STATUS_DOUBT, "reason": "вроде закрыто", "shown": False}}
        m = sm.merge_open_tasks_with_status(fresh, store)
        self.assertEqual(m["open"], ["B"])
        self.assertEqual([d["text"] for d in m["doubt"]], ["A"])

    def test_shown_closed_disappears(self):
        # R21: закрытая и уже показанная задача — НИ в open, НИ в closed (список не копится).
        fresh = ["A"]
        store = {sm._status_key("A"): {"status": sm.STATUS_DONE, "shown": True}}
        m = sm.merge_open_tasks_with_status(fresh, store)
        self.assertEqual(m["open"], [])
        self.assertEqual(m["closed"], [])
        self.assertEqual(m["doubt"], [])

    def test_open_status_stays_hanging(self):
        # Переоткрытая (status=open) остаётся висящей.
        fresh = ["A"]
        store = {sm._status_key("A"): {"status": sm.STATUS_OPEN, "shown": False}}
        m = sm.merge_open_tasks_with_status(fresh, store)
        self.assertEqual(m["open"], ["A"])

    def test_due_reformulation_still_matches(self):
        # Стабильность ключа: статус по «(срок: понедельник)» закрывает «(срок: вторник)».
        fresh = ["задача X (срок: вторник)"]
        store = {sm._status_key("задача X (срок: понедельник)"): {"status": sm.STATUS_DONE, "shown": False}}
        m = sm.merge_open_tasks_with_status(fresh, store)
        self.assertEqual(m["open"], [])
        self.assertEqual(len(m["closed"]), 1)

    def test_order_preserved(self):
        fresh = ["A", "B", "C", "D"]
        store = {sm._status_key("B"): {"status": sm.STATUS_DONE, "shown": False}}
        m = sm.merge_open_tasks_with_status(fresh, store)
        self.assertEqual(m["open"], ["A", "C", "D"])


# ==========================================================================
# Совместимость/миграция: resolve_open_tasks терпит объект
# ==========================================================================
class TestCompatMigration(unittest.TestCase):

    def test_resolve_tolerates_dict_items(self):
        digests = [{"date": "2026-06-08", "open_tasks": [
            "строковая A",
            {"text": "объектная B"},   # расширенная запись — НЕ выпадает из фильтра
            {"task": "объектная C"},   # альтернативное поле
            {"nope": 1},               # без текста → пропуск
        ]}]
        out = sm.resolve_open_tasks(digests)
        self.assertIn("строковая A", out)
        self.assertIn("объектная B", out)
        self.assertIn("объектная C", out)
        self.assertEqual(len(out), 3)

    def test_resolve_old_list_str_unchanged(self):
        digests = [{"date": "2026-06-08", "open_tasks": ["A", "B"]}]
        self.assertEqual(sm.resolve_open_tasks(digests), ["A", "B"])


# ==========================================================================
# Статус ПЕРЕЖИВАЕТ регенерацию протокола (финализация sidecar не трогает)
# ==========================================================================
class TestStatusSurvivesRegeneration(unittest.TestCase):

    def test_finalize_does_not_clobber_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koord"
            sd.mkdir()
            # Между встречами проставили статус (как сделал бы reply Ф5 / сверщик Ф6).
            sm.set_task_status(sd, "Татьяна Филиппова: Прислать расчёт по складу Ozon",
                               sm.STATUS_DONE, reason="по чату")
            before = sm.load_task_status(sd)
            # Регенерация протокола = build_digest → save_digest (как на финализе).
            d = sm.build_digest(_PROTO_TASKS, {"series": "koord", "date": "2026-06-03"})
            sm.save_digest(sd, "2026-06-03", d)
            # ...и полный путь финализации save_meeting_digest тоже.
            sm.save_meeting_digest(sd, "2026-06-03", _PROTO_TASKS,
                                   {"series": "koord", "date": "2026-06-03"})
            after = sm.load_task_status(sd)
            self.assertEqual(before, after)  # sidecar НЕ затёрт регенерацией
            self.assertEqual(
                after[sm._status_key("Татьяна Филиппова: Прислать расчёт по складу Ozon")]["status"],
                sm.STATUS_DONE,
            )

    def test_save_digest_writes_only_memory_file(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koord"
            sd.mkdir()
            sm.set_task_status(sd, "задача", sm.STATUS_CANCELLED)
            d = sm.build_digest(_PROTO_TASKS, {"series": "koord", "date": "2026-06-03"})
            sm.save_digest(sd, "2026-06-03", d)
            # sidecar по-прежнему на месте и отдельным файлом
            self.assertTrue(sm.task_status_path(sd).is_file())
            self.assertTrue((sd / "2026-06-03-memory.json").is_file())


# ==========================================================================
# build_open_tasks_block с sidecar + обратная совместимость + приватность
# ==========================================================================
class TestBuildBlockWithSidecar(unittest.TestCase):

    def _digests(self):
        d = sm.build_digest(_PROTO_TASKS, {"series": "koord", "date": "2026-06-03"})
        return [d]

    def test_closed_excluded_from_block(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koord"
            sd.mkdir()
            sm.set_task_status(sd, "Татьяна Филиппова: Прислать расчёт по складу Ozon",
                               sm.STATUS_AUTO_CLOSED, reason="по чату")
            block = sm.build_open_tasks_block(self._digests(), series_dir=sd, meeting_sid="ph2")
            self.assertIn("медиаплан", block)          # висящая на месте
            self.assertNotIn("складу Ozon", block)      # авто-закрытая не воскресла

    def test_no_series_dir_is_legacy_behavior(self):
        # Обратная совместимость: без series_dir блок = весь свежий хвост (как Ф8).
        block = sm.build_open_tasks_block(self._digests(), meeting_sid="legacy")
        self.assertIn("медиаплан", block)
        self.assertIn("складу Ozon", block)

    def test_closed_does_not_eat_cap_slot(self):
        # При сниженном OPEN_TASKS_MAX закрытая задача в первых N НЕ должна вытеснять
        # живой висяк за капом: капим уже ВИСЯЩИЕ, не сырой хвост.
        digests = [{"date": "2026-06-08", "open_tasks": ["X-closed", "A", "B", "C"]}]
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            sm.set_task_status(sd, "X-closed", sm.STATUS_DONE)
            with mock.patch.dict(os.environ, {"OPEN_TASKS_MAX": "3"}):
                block = sm.build_open_tasks_block(digests, series_dir=sd)
            # 3 висящих (A,B,C) показаны; закрытая X не заняла слот.
            for t in ("A", "B", "C"):
                self.assertIn(t, block)
            self.assertNotIn("X-closed", block)

    def test_log_only_counters_no_text(self):
        secret = "складу Ozon"
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koord"
            sd.mkdir()
            sm.set_task_status(sd, "Татьяна Филиппова: Прислать расчёт по складу Ozon",
                               sm.STATUS_DONE)
            with self.assertLogs("meeting_notary.series_memory", level="INFO") as cm:
                sm.build_open_tasks_block(self._digests(), series_dir=sd, meeting_sid="logp")
            blob = "\n".join(cm.output)
            self.assertIn("closed=1", blob)       # счётчик закрытых есть
            self.assertIn("carried=", blob)
            self.assertNotIn(secret, blob)         # текста задачи нет


# ==========================================================================
# Боевая проверка: реплей серии из 2 встреч со статусом между ними
# ==========================================================================
class TestReplayTwoMeetings(unittest.TestCase):

    def test_status_between_meetings_survives_into_second(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sd = root / "koord"
            sd.mkdir()
            # Встреча 1: финализирована, хвост накоплен.
            sm.save_meeting_digest(sd, "2026-06-03", _PROTO_TASKS,
                                   {"series": "koord", "date": "2026-06-03"})
            # Между встречами: участник ответом снял один вопрос (Ф5 напишет так же).
            sm.set_task_status(sd, "Татьяна Филиппова: Прислать расчёт по складу Ozon",
                               sm.STATUS_CANCELLED, reason="неактуально", source="reply")
            # Встреча 2: резолвим память серии → строим хвост.
            digests = sm.resolve_memory(sd, root, current_date="2026-06-10")
            self.assertTrue(digests)
            block = sm.build_open_tasks_block(digests, series_dir=sd, meeting_sid="m2")
            self.assertIn("медиаплан", block)        # живой висяк показан
            self.assertNotIn("складу Ozon", block)    # отменённый НЕ воскрес как «висит»
            # Ещё одна регенерация (повторный финализ той же даты) — статус не сброшен.
            sm.save_meeting_digest(sd, "2026-06-03", _PROTO_TASKS,
                                   {"series": "koord", "date": "2026-06-03"})
            digests2 = sm.resolve_memory(sd, root, current_date="2026-06-10")
            block2 = sm.build_open_tasks_block(digests2, series_dir=sd, meeting_sid="m2b")
            self.assertNotIn("складу Ozon", block2)   # всё ещё закрыт после регенерации
            # merge напрямую: отменённый в корзине closed, не в open.
            fresh = sm.resolve_open_tasks(digests2)
            merged = sm.merge_open_tasks_with_status(fresh, sm.load_task_status(sd))
            self.assertTrue(any("складу Ozon" in c["text"] for c in merged["closed"]))
            self.assertFalse(any("складу Ozon" in t for t in merged["open"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
