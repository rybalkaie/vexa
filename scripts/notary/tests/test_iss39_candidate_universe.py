#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ф2 (watson-smart-replies-rethink, ISS-39) — ИНВАРИАНТ «напечатано ⇒ находится».

Универсум кандидатов матчера (`pending_status_reply.load_series_pending`) обязан
покрывать ровно то, что участник ВИДИТ в блоке «с прошлых встреч» — на ОБОИХ роутах
(протокол `series_memory.build_open_tasks_block` и дайджест
`morning_digest.build_digest_markdown`). Если пункт напечатан, но выпал из кандидатов —
бот честно ответить по нему не может (ложное «не смог привязать» на видимом), это ядро
ISS-39 (R4).

Покрывает:
  • R4 — каждый пункт блока протокола И дайджеста ∈ кандидаты (в т.ч. регрессия
    «закрытый впереди капа» — merge-vs-raw, из-за которой видимый висяк выпадал);
  • R5 — ключи статусов стабильны: закрытая задача НЕ воскресает после регенерации;
  • РИСК2 — снапшот отвечаемого документа ОГРАНИЧИВАЕТ корзину висящих (рост универсума
    не плодит ложных overlap-матчей у всех потребителей: одиночный/дайджест/mixed/reapply).

Фикстуры — СИНТЕТИЧЕСКИЕ (не реальные задачи/имена участников). Опасная тройка не
затрагивается: тесты офлайн, тексты никуда не логируются.
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
from notary.lib import pending_status_reply as p  # noqa: E402
from notary.lib import morning_digest as md  # noqa: E402


# Синтетический хвост серии: безымянные + два исполнителя + мелочь. НЕ реальные данные.
_NAMELESS = "запустить рассылку по клиентской базе за прошлый квартал"
_SONA_1 = "Сона: подготовить макет лендинга под новый тариф"
_SONA_2 = "Сона: свести отчёт по расходам маркетинга за квартал"
_MIKH_1 = "Михаил: настроить интеграцию оплаты через СБП в кабинете"
_MIKH_2 = "Михаил: описать регламент онбординга новых партнёров"
_MINOR_1 = "проверить орфографию в футере сайта"

_OPEN_TASKS = [
    _NAMELESS,
    _SONA_1,
    _SONA_2,
    _MIKH_1,
    _MIKH_2,
    {"text": _MINOR_1, "minor": True},
]


def _write_series(sd: Path, open_tasks: list, *, date: str = "2026-06-10") -> None:
    """Кладёт один memory.json с заданным хвостом (`save_digest` — низкоуровневый писатель,
    без парсинга протокола: контролируем состав хвоста точно)."""
    digest = {
        "schema": 1,
        "date": date,
        "series": sd.name,
        "participants": ["Сона", "Михаил"],
        "open_tasks": list(open_tasks),
    }
    sm.save_digest(sd, date, digest)


def _candidate_keys(sd: Path, *, snapshot=None) -> set:
    return {sm._status_key(c) for c in p.load_series_pending(sd, snapshot=snapshot)}


class _SeriesFx(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        _write_series(self.sd, _OPEN_TASKS)

    def tearDown(self):
        self._tmp.cleanup()

    # — какие пункты РЕАЛЬНО видит участник (через тот же публичный пайплайн, что показ) —
    def _protocol_visible(self):
        digests = sm.list_series_digests(self.sd)
        store = sm.load_task_status(self.sd)
        cap = sm.open_tasks_max()
        merged = sm.merge_open_tasks_with_status(sm._resolve_open_tasks_raw(digests), store)
        hanging = merged["open"][:cap] if cap > 0 else merged["open"]
        doubt = [d["text"] for d in merged["doubt"]]
        closed = [c["text"] for c in merged["closed"]]
        return hanging, doubt, closed

    def _digest_visible(self):
        items = md.collect_items(self.sd)
        return list(items["open"]), list(items["minor"])


class TestR4ProtocolInvariant(_SeriesFx):
    def test_every_printed_protocol_item_is_candidate(self):
        # Помечаем один висяк закрытым, один — под сомнением: блок печатает ОБА подраздела
        # Части 2 (✅ закрыто / 🟡 под сомнением) — они тоже обязаны быть кандидатами.
        sm.set_task_status(self.sd, _MIKH_1, sm.STATUS_DONE, source="test")
        sm.set_task_status(self.sd, _SONA_2, sm.STATUS_DOUBT, source="test")

        block = sm.build_open_tasks_block(
            sm.list_series_digests(self.sd), series_dir=self.sd, date="2026-06-24"
        )
        self.assertTrue(block, "блок должен собраться")
        cand_keys = _candidate_keys(self.sd)
        hanging, doubt, closed = self._protocol_visible()

        # 1) закрытый/сомнительный реально ушли из хвоста в свои корзины
        self.assertIn(_MIKH_1, closed)
        self.assertIn(_SONA_2, doubt)

        # 2) КАЖДЫЙ напечатанный пункт (все три корзины) ∈ кандидаты
        for item in (*hanging, *doubt, *closed):
            self.assertIn(
                sm._status_key(item), cand_keys,
                msg=f"пункт блока не среди кандидатов: {item[:40]!r}",
            )

        # 3) и он реально ВИДЕН в тексте блока (display-текст, у сгруппированных имя срезано)
        for pos in sm.ordered_open_task_positions(hanging):
            self.assertIn(pos["display_text"], block)
        for item in (*doubt, *closed):
            self.assertIn(item, block)  # Часть 2 печатает полный текст дословно

    def test_closed_ahead_of_cap_still_candidate(self):
        # РЕГРЕССИЯ ядра ISS-39: закрытый пункт ВПЕРЕДИ капа. Старый `resolve_open_tasks`
        # капил ДО удаления закрытого → живой висяк, всплывший в показе, выпадал из
        # кандидатов. Новый источник (merge → cap) держит их синхронно.
        tasks = ["альфа задача про склад", "бета задача про логистику",
                 "гамма задача про финансы", "дельта задача про найм"]
        sd2 = self.root / "coord2"
        sd2.mkdir()
        _write_series(sd2, tasks)
        sm.set_task_status(sd2, tasks[0], sm.STATUS_DONE, source="test")  # закрыт ВПЕРЕДИ

        with mock.patch.dict(os.environ, {"OPEN_TASKS_MAX": "3"}):
            digests = sm.list_series_digests(sd2)
            store = sm.load_task_status(sd2)
            merged = sm.merge_open_tasks_with_status(sm._resolve_open_tasks_raw(digests), store)
            hanging = merged["open"][:3]           # что печатает показ: бета/гамма/дельта
            self.assertIn(tasks[3], hanging)       # дельта всплыла из-под капа
            # старый источник (raw→cap) дельту НЕ содержал — фиксируем, что разрыв БЫЛ:
            self.assertNotIn(tasks[3], sm.resolve_open_tasks(digests))
            # новый универсум — содержит (инвариант восстановлен):
            cand_keys = _candidate_keys(sd2)
            for item in hanging:
                self.assertIn(sm._status_key(item), cand_keys)


class TestR4DigestInvariant(_SeriesFx):
    def test_every_printed_digest_item_is_candidate(self):
        open_items, minor = self._digest_visible()
        self.assertIn(_MINOR_1, minor, "мелочь должна быть видна в дайджесте")
        digest_md = md.build_digest_markdown("koord", "2026-06-24", open_items, minor)
        self.assertTrue(digest_md)
        cand_keys = _candidate_keys(self.sd)

        for pos in sm.ordered_open_task_positions(open_items):
            self.assertIn(
                sm._status_key(pos["key_text"]), cand_keys,
                msg=f"висяк дайджеста не среди кандидатов: {pos['key_text'][:40]!r}",
            )
            self.assertIn(pos["display_text"], digest_md)
        for t in minor:
            self.assertIn(sm._status_key(t), cand_keys,
                          msg=f"мелочь дайджеста не среди кандидатов: {t[:40]!r}")
            self.assertIn(t, digest_md)


class TestR5KeyStability(_SeriesFx):
    def test_closed_task_does_not_resurrect_after_regen(self):
        # Закрываем задачу, проходим канонический показ (finalize помечает shown), затем
        # регенерируем — закрытая НЕ возвращается «висит» и НЕ теряет ключ.
        key0 = sm._status_key(_SONA_1)
        sm.set_task_status(self.sd, _SONA_1, sm.STATUS_DONE, source="test")
        digests = sm.list_series_digests(self.sd)

        # ДО показа: закрытая — в корзине «закрыто», НЕ «висит»
        merged0 = sm.merge_open_tasks_with_status(sm._resolve_open_tasks_raw(digests),
                                                  sm.load_task_status(self.sd))
        self.assertNotIn(_SONA_1, merged0["open"])
        self.assertIn(_SONA_1, [c["text"] for c in merged0["closed"]])

        # Канонический показ (finalize, mark_shown=True) помечает закрытую shown
        sm.build_open_tasks_block(digests, series_dir=self.sd, mark_shown=True,
                                  date="2026-06-24")

        # ПОСЛЕ показа + регенерации: shown=True → НЕ печатается и НЕ воскресает «висит»
        merged1 = sm.merge_open_tasks_with_status(sm._resolve_open_tasks_raw(digests),
                                                  sm.load_task_status(self.sd))
        self.assertNotIn(_SONA_1, merged1["open"])
        self.assertEqual([c["text"] for c in merged1["closed"]], [])

        # ключ стабилен: та же задача в новом хвосте маппится в ту же sidecar-запись
        store = sm.load_task_status(self.sd)
        self.assertIn(key0, store)
        self.assertEqual(store[key0]["status"], sm.STATUS_DONE)

    def test_reopen_via_sidecar_key_matches_original(self):
        # A10: закрытый, показанный и выпавший из хвоста пункт всё ещё резолвится по
        # ключу (переоткрытие). Ключ overlap-матчинга == ключ записи статуса.
        sm.set_task_status(self.sd, _MIKH_2, sm.STATUS_DONE, source="test")
        cands = p.load_series_pending(self.sd)
        self.assertIn(sm._status_key(_MIKH_2),
                      {sm._status_key(c) for c in cands})


class TestRisk2SnapshotBounding(_SeriesFx):
    def test_snapshot_bounds_hanging_universe(self):
        # Снапшот отвечаемого документа = подмножество висяков. Универсум висящих
        # ОГРАНИЧЕН им: живой висяк, которого в ТОМ документе не было, — не кандидат.
        snap = [_NAMELESS, _SONA_1]                      # что человек видел в том протоколе
        cand_keys = _candidate_keys(self.sd, snapshot=snap)
        self.assertIn(sm._status_key(_NAMELESS), cand_keys)
        self.assertIn(sm._status_key(_SONA_1), cand_keys)
        # висяки вне снапшота (и без sidecar/мелочь) выпадают — universe ограничен:
        self.assertNotIn(sm._status_key(_SONA_2), cand_keys)
        self.assertNotIn(sm._status_key(_MIKH_1), cand_keys)
        # мелочь снапшот не несёт, но она всегда видна в дайджесте → остаётся кандидатом:
        self.assertIn(sm._status_key(_MINOR_1), cand_keys)

    def test_no_snapshot_uses_full_live_universe(self):
        cand_keys = _candidate_keys(self.sd, snapshot=None)
        for t in (_NAMELESS, _SONA_1, _SONA_2, _MIKH_1, _MIKH_2, _MINOR_1):
            self.assertIn(sm._status_key(t), cand_keys)

    def test_empty_snapshot_means_empty_hanging_basket(self):
        # Ф3 форвард-долг (Ф2-1): пустой слепок (документ показал 0 нумерованных висяков) —
        # это «0 висящих», НЕ live-хвост (иначе overlap мог бы закрыть задачу, которой в
        # отвечаемом документе не было — РИСК2). `snapshot is None` по-прежнему = live.
        # Правка ОСОЗНАННО пересматривает прежнее поведение (было: `[]`→live).
        empty = _candidate_keys(self.sd, snapshot=[])
        live = _candidate_keys(self.sd, snapshot=None)
        # висящая задача БЕЗ слепка — кандидат; с ПУСТЫМ слепком — уже нет:
        self.assertIn(sm._status_key(_SONA_1), live)
        self.assertNotIn(sm._status_key(_SONA_1), empty)
        # мелочь слепок не несёт — она всегда live (остаётся кандидатом и при пустом слепке):
        self.assertIn(sm._status_key(_MINOR_1), empty)


_DISTINCT = [
    "склад", "логистика", "реклама", "найм", "бюджет", "договор", "тара",
    "миграция", "клиенты", "онбординг", "аналитика", "отчётность", "закупка",
    "тендер", "лицензия", "сертификат", "аудит", "инвентаризация", "маршрут",
    "подписка", "интеграция", "вебинар", "витрина", "дистрибуция",
]


class TestGrowthNoFalseOverlap(_SeriesFx):
    def _big_series(self):
        # 24 задачи с ОДНОЙ болванкой и уникальным словом-идентификатором — так рост
        # универсума реалистичен, а верный матч всё же разрешим по различающему слову.
        tasks = [f"проработать направление {w} и закрыть по нему вопрос серии"
                 for w in _DISTINCT]
        sd = self.root / "big"
        sd.mkdir()
        _write_series(sd, tasks)
        return sd, tasks

    def test_incidental_word_does_not_false_close_in_big_universe(self):
        # 24 кандидата. Длинная реплика, делящая с чужими задачами лишь ОДНО болваночное
        # слово, не должна закрыть ни одну (адаптивный порог хотфикса 20.07 — РИСК2).
        sd, tasks = self._big_series()
        reply = "коллеги, по направлению сегодня всё обсудили и договорились двигаться — готово"
        matched = p.match_pending_item(reply, p.load_series_pending(sd))
        self.assertIsNone(matched, "случайное пересечение слов не должно матчить")
        res = p.apply_status_reply(reply, sd, source="test")
        self.assertIsNone(res)
        self.assertEqual(sm.load_task_status(sd), {})   # ни одна задача не закрыта

    def test_genuine_content_matches_right_task_in_big_universe(self):
        # Та же большая база, но реплика реально повторяет слова конкретной задачи →
        # матчится ИМЕННО она (рост универсума не мешает верному матчу).
        sd, tasks = self._big_series()
        target = tasks[7]
        res = p.apply_status_reply(target + " — сделано", sd, source="test")
        self.assertIsNotNone(res)
        self.assertEqual(res["matched"], target)
        self.assertEqual(res["status"], sm.STATUS_DONE)

    def test_snapshot_scoping_blocks_close_of_unshown_task(self):
        # Потребитель одиночного apply: задача ЖИВА в хвосте, но её НЕ было в отвечаемом
        # документе (снапшот). Даже дословный overlap не закрывает её — универсум
        # ограничен снапшотом (РИСК2 на уровне apply, не только матчера).
        target = _MIKH_1
        snap = [_NAMELESS, _SONA_1]                       # target не показан
        res = p.apply_status_reply(target + " — сделано", self.sd,
                                   source="test", snapshot=snap)
        self.assertIsNone(res)
        self.assertNotIn(sm._status_key(target), sm.load_task_status(self.sd))
        # а показанный в снапшоте — закрывается штатно:
        res2 = p.apply_status_reply(_SONA_1 + " — сделано", self.sd,
                                    source="test", snapshot=snap)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["matched"], _SONA_1)


if __name__ == "__main__":
    unittest.main()
