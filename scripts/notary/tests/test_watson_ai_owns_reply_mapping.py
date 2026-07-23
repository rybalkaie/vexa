# -*- coding: utf-8 -*-
"""План `2026-07-23-ai-owns-reply-mapping`: САМО сопоставление «часть ответа → задача»
отдано ИИ (позиция `pos` из пронумерованного слепка), детерминизм — только страховка.

Golden-кейсы из РЕАЛЬНЫХ провалов ночи 22→23.07 (ISS-44), синтетика по форме, без ПДн.
Офлайн: стаб ИИ (`understand_fn`) отдаёт `{statuses:[{pos,cls,...}]}` — `claude` НЕ зовётся.

Инвариант корректности: ИИ вернул позицию N → код берёт `snapshot[N-1]` (ровно то, что
участник ВИДЕЛ под номером N). N вне 1..len(snapshot) → НЕ угадываем, честный вопрос."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import reply_understand as ru  # noqa: E402


# 18 своих задач (без чужого префикса) + сквозная нумерация 1..18, как в дайджесте/протоколе.
_SNAP = [f"Пункт номер {i} по проекту" for i in range(1, 19)]


def _canned(statuses, *, edits=None, block_close=False, unclear=""):
    """statuses: список dict с любым подмножеством ключей pos/cls/text/conf.
    Отражает то, что вернул бы `_parse_understand` на реальном ответе ИИ."""
    out = []
    for s in statuses:
        d = {"text": s.get("text", ""), "cls": s.get("cls")}
        if "pos" in s:
            d["pos"] = s["pos"]
        if "conf" in s:
            d["conf"] = s["conf"]
        out.append(d)
    return {"statuses": out, "edits": list(edits or []), "series_config": [],
            "block_close": block_close, "unclear": unclear}


class AiOwnsMappingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sd = Path(self._tmp.name) / "mpervyi-pn-koord-finplan"
        self.sd.mkdir(parents=True)
        self.memf = str(Path(self._tmp.name) / "decision-memory.jsonl")
        self._env = mock.patch.dict(os.environ, {
            "ENABLE_REPLY_UNDERSTAND": "1",
            "MEETING_NOTARY_DECISION_MEMORY_FILE": self.memf,
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, reply_text, statuses, *, snapshot=None, replier="Илья Рыбалка", **kw):
        return ru.understand_reply(
            reply_text, series_dir=self.sd, replier=replier,
            snapshot=list(_SNAP) if snapshot is None else snapshot,
            understand_fn=lambda t, items: _canned(statuses, **kw),
            date="2026-07-23")

    # ── R1/R2: ИИ отдаёт позицию, код берёт snapshot[pos-1] ───────────────────
    def test_ai_positions_map_to_snapshot(self):
        """«закрой 13, 14, 15, 17, 18» — ИИ вернул 5 позиций → РОВНО эти 5 задач done."""
        r = self._run(
            "закрой 13, 14, 15, 17, 18",
            [{"pos": p, "cls": "done"} for p in (13, 14, 15, 17, 18)])
        self.assertEqual(len(r["applied"]), 5)
        self.assertEqual(
            sorted(a["matched"] for a in r["applied"]),
            sorted(_SNAP[p - 1] for p in (13, 14, 15, 17, 18)))
        self.assertEqual(r["questions"], [])
        self.assertTrue(all(a["via"] == "ordinal" for a in r["applied"]))

    def test_ai_position_by_description(self):
        """«которая про ВЭД» — ИИ САМ сопоставил описание с номером (pos=7). Регекс бы не смог."""
        snap = list(_SNAP)
        snap[6] = "Разобраться с оформлением ВЭД по контейнеру"
        r = self._run("закрой ту, которая про ВЭД", [{"pos": 7, "cls": "done", "text": "которая про ВЭД"}],
                      snapshot=snap)
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(r["applied"][0]["matched"], snap[6])

    def test_ai_position_leading_with_description(self):
        """Кейс 00:48: «14 Прислать ссылку по акциям» — ИИ отдаёт pos=14 напрямую."""
        r = self._run("14 Прислать ссылку по акциям",
                      [{"pos": 14, "cls": "done", "text": "Прислать ссылку по акциям"}])
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(r["applied"][0]["matched"], _SNAP[13])

    # ── 44.3: голос, класс единый на все номера ───────────────────────────────
    def test_iss44_3_voice_all_done_one_class(self):
        """«первую, вторую, третью и четвёртую — закрыто» (голос) → ВСЕ 4 = done,
        не разнесены по «в работе/снято» (был корень 44.3)."""
        r = self._run(
            "первую, вторую, третью и четвёртую — закрыто",
            [{"pos": p, "cls": "done"} for p in (1, 2, 3, 4)])
        self.assertEqual(len(r["applied"]), 4)
        self.assertTrue(all(a["label"] == "done" for a in r["applied"]))
        self.assertEqual(r["questions"], [])

    # ── R4/R11: чужие задачи — всегда ступень-3, НЕ «не понял» ─────────────────
    def test_foreign_positions_go_to_confirm_not_lost(self):
        """«Задачи Сони 13, 14, 15 убери» → 3 ЧУЖИЕ (Соня) → каждая в R11-подтверждение,
        НЕ теряются и НЕ закрываются молча."""
        snap = list(_SNAP)
        for i in (13, 14, 15):
            snap[i - 1] = f"Соня: Пункт {i} по маркетплейсам"
        r = self._run("Задачи Сони 13, 14, 15 убери отсюда",
                      [{"pos": p, "cls": "cancelled"} for p in (13, 14, 15)],
                      snapshot=snap, replier="Илья Рыбалка")
        self.assertEqual(r["applied"], [], "чужие НЕ закрываем молча")
        foreign = [q for q in r["questions"] if q.get("kind") == "foreign"]
        self.assertEqual(len(foreign), 3, "3 чужие задачи → 3 подтверждения")

    # ── R3/R5: позиция вне списка → честный вопрос, ничего не закрыто ──────────
    def test_position_out_of_range_asks_not_guesses(self):
        """ИИ вернул pos=99 (список из 18) → НЕ угадываем: matched=None → вопрос, sidecar цел."""
        r = self._run("закрой 99", [{"pos": 99, "cls": "done"}])
        self.assertEqual(r["applied"], [])
        self.assertTrue(any(q.get("kind") == "no_match" for q in r["questions"]))

    def test_position_on_empty_slot_asks_not_closes(self):
        """R5: ИИ вернул pos на ПУСТОЙ слот снимка (участник такой пункт не видел — промпт
        его скрывает) → НЕ закрываем пустую задачу, честный вопрос."""
        snap = list(_SNAP)
        snap[6] = ""  # позиция 7 пуста → в показе её нет
        r = self._run("седьмую закрыл", [{"pos": 7, "cls": "done"}], snapshot=snap)
        self.assertEqual(r["applied"], [])
        self.assertTrue(any(q.get("kind") == "no_match" for q in r["questions"]))

    def test_position_zero_ignored_falls_back(self):
        """pos=0 не валиден (форма) → трактуется как «нет позиции» → фолбэк по тексту.
        Текст «непонятная фраза» ни к чему не привязывается → ничего ложно не закрыто."""
        r = self._run("непонятная фраза без номера", [{"pos": 0, "cls": "done", "text": "непонятная фраза"}])
        self.assertEqual(r["applied"], [])

    # ── R5: уверенность ИИ только ПОНИЖАЕТ ступень ────────────────────────────
    def test_low_conf_downgrades_tier_but_still_applies(self):
        """conf=low → ступень 2 даже когда класс знает СЛОВАРЬ (иначе была бы ступень 1):
        уверенность ИИ лишь ПОНИЖАЕТ (R5). text со словарным словом изолирует эффект conf."""
        r = self._run("третью закрыл", [{"pos": 3, "cls": "done", "conf": "low",
                                         "text": "третью закрыл"}])
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(r["applied"][0]["tier"], ru.TIER_MINOR)

    def test_high_conf_stays_confident(self):
        """conf=high + словарный класс + своя задача в диапазоне → ступень 1 (уверенно).
        Пара к тесту выше: без conf=low та же задача = ступень 1, значит downgrade — от conf."""
        r = self._run("третью закрыл", [{"pos": 3, "cls": "done", "conf": "high",
                                         "text": "третью закрыл"}])
        self.assertEqual(len(r["applied"]), 1)
        self.assertEqual(r["applied"][0]["tier"], ru.TIER_CONFIDENT)

    # ── R6: правки не крадём ──────────────────────────────────────────────────
    def test_edit_origin_not_stolen_into_status(self):
        """Статус-сегмент с маркером правки → в петлю правок, не в закрытие задачи (РИСК1)."""
        r = self._run("Вместо Zil Sound правильно Zeal Sound",
                      [{"pos": 5, "cls": "done", "text": "Вместо Zil Sound правильно Zeal Sound"}])
        self.assertEqual(r["applied"], [])
        self.assertTrue(any("Zeal" in e for e in r["edits"]))

    # ── R9: счёт/сумма без pos → фолбэк не выдумывает ординал ──────────────────
    def test_count_and_amount_without_pos_no_false_close(self):
        """ИИ, поняв «3 задачи закрыл»/«собрать 3–5 млн» как НЕ-номер, отдаёт pos:null →
        фолбэк-регекс тоже НЕ трактует их ординалом → ничего не закрыто."""
        r1 = self._run("3 задачи закрыл", [{"cls": "done", "text": "3 задачи закрыл"}])
        self.assertEqual(r1["applied"], [])
        r2 = self._run("надо собрать 3-5 млн", [{"cls": "wait", "text": "надо собрать 3-5 млн"}])
        self.assertEqual(r2["applied"], [])

    # ── R7: ИИ недоступен → фолбэк R9, ложных закрытий 0 ──────────────────────
    def test_ai_unavailable_defers_no_false_close(self):
        """understand_fn → None (ИИ недоступен): пачка статусов → очередь дозабора, 0 применено."""
        r = ru.understand_reply(
            "закрой 13, 14, 15", series_dir=self.sd, replier="Илья Рыбалка",
            snapshot=list(_SNAP), understand_fn=lambda t, items: None, date="2026-07-23")
        self.assertEqual(r["applied"], [])
        self.assertTrue(r.get("deferred") or not r.get("engaged"))

    # ── Совместимость: статусы БЕЗ pos идут прежним регекс-фолбэком ────────────
    def test_legacy_no_pos_uses_regex_fallback(self):
        """Статус без pos (старый формат / ИИ не дал номер) «закрой 13, 14, 15» → регекс-
        дробитель по-прежнему разворачивает по цифрам (страховка не удалена)."""
        r = self._run("закрой 13, 14, 15", [{"cls": "done", "text": "закрой 13, 14, 15"}])
        self.assertEqual(
            sorted(a["matched"] for a in r["applied"]),
            sorted(_SNAP[p - 1] for p in (13, 14, 15)))

    # ── Детерминированный ФЛОР номеров (инцидент 23.07) ───────────────────────
    def test_floor_recovers_numbers_ai_dropped(self):
        """ГЛАВНОЕ: ИИ уронил числа (вернул 1 pos + огрызки без pos), а в ответе явный
        список «13,14,15» + глагол → флор восстанавливает ВСЕ три; огрызки выкинуты."""
        snap = list(_SNAP)
        for i in (13, 14, 15):
            snap[i - 1] = f"Соня: Задача {i}"
        # стаб имитирует провал: только 13 с pos, 14/15 — безпозиционные огрызки
        stub = [{"pos": 13, "cls": "cancelled", "text": "13"},
                {"cls": "cancelled", "text": "удали, по ошибке"},
                {"cls": "cancelled", "text": "удали, по ошибке"}]
        r = self._run("Удали задачи Соны. 13,14,15. Они в этом протоколе по ошибке",
                      stub, snapshot=snap)
        foreign = [q for q in r["questions"] if q.get("kind") == "foreign"]
        self.assertEqual(len(foreign), 3, "все 3 номера Сони → 3 подтверждения")
        self.assertFalse(any(q.get("kind") == "no_match" for q in r["questions"]),
                         "безпозиционные огрызки ИИ выкинуты, не 'не понял'")

    def test_floor_direct_helper_safety(self):
        """Гарды флора: счёт/сумма/дата/диапазон НЕ дают номеров; чистый список — даёт."""
        self.assertEqual(ru._recover_ordinal_list("Удали задачи Соны. 13,14,15. По ошибке", _SNAP),
                         {13, 14, 15})
        self.assertEqual(ru._recover_ordinal_list("3 задачи закрыл", _SNAP), set())
        self.assertEqual(ru._recover_ordinal_list("надо собрать 3-5 млн", _SNAP), set())
        self.assertEqual(ru._recover_ordinal_list("сделал к 5 июля", _SNAP), set())
        self.assertEqual(ru._recover_ordinal_list("отчёт за 3 квартал", _SNAP), set())
        self.assertEqual(ru._recover_ordinal_list("закрой 17, 18", _SNAP), {17, 18})
        # без глагола действия — не трогаем (просто числа в прозе)
        self.assertEqual(ru._recover_ordinal_list("там было 13, 14, 15 человек", _SNAP), set())
        # число вне диапазона в списке → пусто (не гадаем)
        self.assertEqual(ru._recover_ordinal_list("закрой 3, 99", _SNAP), set())

    # ── Инвариант выравнивания: pos строго = snapshot[pos-1] ──────────────────
    def test_pos_indexes_exact_snapshot_slot(self):
        """Разные позиции не схлопываются и каждая бьётся в СВОЙ слот слепка."""
        r = self._run("1 и 18 закрыл", [{"pos": 1, "cls": "done"}, {"pos": 18, "cls": "done"}])
        got = sorted(a["matched"] for a in r["applied"])
        self.assertEqual(got, sorted([_SNAP[0], _SNAP[17]]))


if __name__ == "__main__":
    unittest.main()
