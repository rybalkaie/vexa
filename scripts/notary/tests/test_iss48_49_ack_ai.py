# -*- coding: utf-8 -*-
"""ISS-49 (класс) / ISS-48 (симптом): ответ на ВОПРОС САМОГО БОТА разбирает ИИ, а не
регулярки; массовое закрытие «у всех / у Татьяны, у Марии» не сужается молча.

Инцидент 24.07 (серия marketplaces-tatiana): бот спросил «Закрыть все ваши вопросы?
1) караоке 2) Cloud Code». Владелец: «надо закрыть все вопросы, которые есть у меня, у
Татьяны, у Александры, у Марии. Единственное, оставь только один — доделать агента на
Cloud Code. Все остальные закрывай». Бот закрыл РОВНО ОДНУ задачу (караоке) из ~21 и
промолчал про остальное: слово «один» регулярка приняла за НОМЕР пункта 1, а охват «у
всех» не поддерживался вовсе.

Покрывает:
  • количественные слова больше НЕ становятся номером пункта (ISS-48 «а»);
  • ИИ-первый разбор ответа на вопрос бота, регулярки — фолбэк (ISS-49);
  • охват «у всех / поимённо» в массовом закрытии (ISS-48 «б»);
  • исключение по ОПИСАНИЮ, а не только по номеру (ISS-48 «в»);
  • инвариант честности: шире вопроса → показать список и взять ОДНО подтверждение,
    ничего молча (ISS-48 «г» / ISS-49 «б»);
  • уже закрытые задачи не попадают в список «что закрыть» (побочное ISS-48);
  • ИИ недоступен → честный переспрос, а НЕ закрытие не той задачи.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python -m unittest tests.test_iss48_49_ack_ai -v
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

from notary.lib import ack_understand as aiu  # noqa: E402
from notary.lib import reply_ack as ra  # noqa: E402
from notary.lib import reply_understand as ru  # noqa: E402
from notary.lib import series_memory as sm  # noqa: E402


_PROTO = """#протоколвстречи 03.06.2026

**Встреча:** Координация по маркетплейсам.

**Участники:** Илья Рыбалка, Татьяна Филиппова, Александра Ким, Мария Ли

**Транскрипт:** [2026-06-03.md](2026-06-03.md)

---

## Задачи

**Илья Рыбалка**
- Отслушать сэмплы караоке
- Доделать агента на Claude Code и выдать Татьяне

**Татьяна Филиппова**
- Подготовить медиаплан на июль
- Прислать расчёт по складу Ozon

**Александра Ким**
- Свести демо-трек

**Мария Ли**
- Собрать отзывы по бренд-зоне
"""

_KARAOKE = "Илья Рыбалка: Отслушать сэмплы караоке"
_AGENT = "Илья Рыбалка: Доделать агента на Claude Code и выдать Татьяне"
_MEDIA = "Татьяна Филиппова: Подготовить медиаплан на июль"
_RASCH = "Татьяна Филиппова: Прислать расчёт по складу Ozon"
_DEMO = "Александра Ким: Свести демо-трек"
_OTZYV = "Мария Ли: Собрать отзывы по бренд-зоне"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "marketplaces"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-06-03", _PROTO,
                               {"series": "marketplaces", "date": "2026-06-03"})
        self._env = mock.patch.dict(os.environ, {
            "ENABLE_REPLY_UNDERSTAND": "1",
            "MEETING_NOTARY_DECISION_MEMORY_FILE": str(self.root / "dm.jsonl"),
            "MEETING_NOTARY_ACK_STATE_FILE": str(self.root / "ack.json"),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _status(self):
        return {k: v["status"] for k, v in sm.load_task_status(self.sd).items()}

    def _closed_texts(self):
        return {v["text"] for v in sm.load_task_status(self.sd).values()
                if v["status"] in sm._TERMINAL_CLOSED}

    def _state(self, question, **over):
        st = {"series_dir": str(self.sd), "date": "2026-06-04", "applied": [],
              "replier": "Илья Рыбалка", "question": question}
        st.update(over)
        return st

    def _confirm_close_q(self, options=None):
        """Тот самый вопрос бота: «Закрыть все ВАШИ вопросы?» — показаны только свои."""
        return {"mode": "confirm_close", "label": "done",
                "options": list(options or [_KARAOKE, _AGENT]), "word": None,
                "scope": "mine", "owners": []}


# ==========================================================================
# ISS-48 «а»: количественное слово — НЕ номер пункта
# ==========================================================================
class TestCardinalIsNotPosition(_Base):
    def test_odin_is_not_position_one(self):
        # «оставь только ОДИН» = количество, а не «пункт 1» (корень инцидента 24.07).
        a = ra.parse_answer("оставь только один вопрос, остальные закрывай")
        self.assertEqual(a["digits"], set())

    def test_ordinal_still_works(self):
        a = ra.parse_answer("да, кроме третьей")
        self.assertEqual(a["excluded"], {3})

    def test_digits_still_work(self):
        self.assertEqual(ra.parse_answer("2")["digits"], {2})

    def test_no_ai_no_silent_wrong_close(self):
        # ИИ недоступен (гейт OFF) + непростой ответ → честный переспрос, НИЧЕГО не закрыто.
        # Раньше эта же фраза закрывала пункт 1 (караоке) и молчала про остальное.
        res = ra.handle_ack_reply(
            self._state(self._confirm_close_q()),
            "надо закрыть все вопросы у меня, у Татьяны, у Александры и у Марии, "
            "оставь только один — доделать агента на Claude Code")
        self.assertEqual(self._status(), {})
        self.assertFalse(res["consume"])
        self.assertIn("Ответьте", res["text"])


# ==========================================================================
# ISS-49: быстрый путь vs ИИ
# ==========================================================================
class TestTrivialFastPath(_Base):
    def test_trivial_answers(self):
        for t in ("да", "Да!", "нет", "2", "13, 14 и 15", "да, кроме 3", "да, кроме третьей"):
            self.assertTrue(aiu.is_trivial_answer(t), t)

    def test_non_trivial_answers(self):
        for t in ("оставь только один — агента на Claude Code, остальные закрывай",
                  "закрой всё у всех", "у Татьяны и Марии всё сделано"):
            self.assertFalse(aiu.is_trivial_answer(t), t)

    def test_trivial_never_calls_ai(self):
        calls = []

        def _fn(text, cands):
            calls.append(text)
            return {"intent": "apply", "all": True, "positions": [], "keep": [],
                    "scope": "shown", "owners": [], "cls": None, "unclear": ""}

        res = ra.handle_ack_reply(self._state(self._confirm_close_q()), "да", answer_fn=_fn)
        self.assertEqual(calls, [])                       # ИИ не дёргали — регулярки хватило
        self.assertEqual(self._closed_texts(), {_KARAOKE, _AGENT})
        self.assertTrue(res["consume"])

    def test_ai_gate_off_by_default(self):
        # [[reissue-llm-tier-gate-default-off]]: без ENABLE_ACK_ANSWER_AI реальный claude
        # не зовётся (иначе офлайн-тесты жгли бы квоту и висли на таймауте).
        self.assertFalse(aiu.is_enabled())
        with mock.patch.dict(os.environ, {"ENABLE_ACK_ANSWER_AI": "1"}):
            self.assertTrue(aiu.is_enabled())


# ==========================================================================
# ISS-48: массовое закрытие с охватом + исключение по описанию
# ==========================================================================
class TestMassCloseScope(_Base):
    def _ai(self, **over):
        base = {"intent": "apply", "all": True, "positions": [], "keep": [],
                "scope": "shown", "owners": [], "cls": None, "unclear": ""}
        base.update(over)
        return lambda text, cands: dict(base)

    def _keep_pos(self, cands, text):
        return [i for i, c in enumerate(cands, 1) if c == text][0]

    def test_case_of_owner_24_07(self):
        """Дословный кейс владельца: закрыть у 4 человек, кроме «агента на Claude Code»."""
        seen = {}

        def _fn(text, cands):
            seen["cands"] = list(cands)
            return {"intent": "apply", "all": True, "positions": [],
                    "keep": [self._keep_pos(cands, _AGENT)], "scope": "named",
                    "owners": ["Илья Рыбалка", "Татьяна Филиппова", "Александра Ким",
                               "Мария Ли"],
                    "cls": "done", "unclear": ""}

        res = ra.handle_ack_reply(
            self._state(self._confirm_close_q()),
            "закрой все вопросы у меня, у Татьяны, у Александры, у Марии; "
            "оставь только один — доделать агента на Claude Code",
            answer_fn=_fn)
        # 1) Молча НИЧЕГО не применили — просьба шире вопроса, нужен один подтверждающий шаг.
        self.assertEqual(self._status(), {})
        # 2) Показан ПОЛНЫЙ список намерения, включая ЧУЖИЕ задачи (их бот не показывал).
        self.assertIn(_MEDIA, res["text"])
        self.assertIn(_DEMO, res["text"])
        self.assertIn(_OTZYV, res["text"])
        self.assertIn(_KARAOKE, res["text"])
        # 3) Исключение по ОПИСАНИЮ сработало и названо вслух.
        self.assertIn("Оставляю открытым", res["text"])
        # 4) Сужение НЕ молчаливое — бот прямо говорит, что берёт шире.
        self.assertIn("шире", res["text"])
        # 5) Есть новый контекст переспроса — «да» на него будет куда применить.
        q = res.get("question")
        self.assertIsNotNone(q)
        self.assertEqual(q["mode"], "confirm_close")
        self.assertNotIn(_AGENT, q["options"])
        self.assertEqual(len(q["options"]), 5)          # 6 открытых минус исключённая
        # 6) Подтверждение «да» закрывает ВЕСЬ список, кроме оставленной.
        res2 = ra.handle_ack_reply(self._state(q), "да")
        self.assertTrue(res2["consume"])
        self.assertEqual(self._closed_texts(),
                         {_KARAOKE, _MEDIA, _RASCH, _DEMO, _OTZYV})
        self.assertNotIn(_AGENT, self._closed_texts())

    def test_scope_everyone(self):
        res = ra.handle_ack_reply(
            self._state(self._confirm_close_q()), "закрывай вообще всё у всех, чистый лист",
            answer_fn=self._ai(scope="everyone"))
        self.assertEqual(self._status(), {})            # сперва подтверждение
        self.assertIn("по всем участникам", res["text"])
        self.assertEqual(len(res["question"]["options"]), 6)

    def test_scope_shown_applies_immediately(self):
        # Ответ НЕ выходит за показанное → подтверждать нечего, применяем сразу.
        res = ra.handle_ack_reply(
            self._state(self._confirm_close_q()), "да, закрывай все мои, кроме караоке",
            answer_fn=self._ai(scope="shown", keep=[1]))
        self.assertEqual(self._closed_texts(), {_AGENT})
        self.assertIsNone(res.get("question"))
        self.assertIn("Оставил открытым", res["text"])

    def test_decline_via_ai(self):
        res = ra.handle_ack_reply(
            self._state(self._confirm_close_q()), "не, погоди, ничего пока не трогай",
            answer_fn=self._ai(intent="decline"))
        self.assertEqual(self._status(), {})
        self.assertTrue(res["consume"])
        self.assertIn("ничего не закрываю", res["text"])

    def test_unclear_via_ai_asks_again(self):
        res = ra.handle_ack_reply(
            self._state(self._confirm_close_q()), "ну там как-то так, посмотри сам",
            answer_fn=self._ai(intent="unclear", all=False))
        self.assertEqual(self._status(), {})
        self.assertFalse(res["consume"])                # ждём внятный ответ, не гадаем

    def test_ai_positions_out_of_range_ignored(self):
        # Номер вне списка — не угадываем (R5): применяем только валидные.
        res = ra.handle_ack_reply(
            self._state(self._confirm_close_q()), "закрой первую и сорок вторую",
            answer_fn=self._ai(all=False, positions=[1, 42]))
        self.assertEqual(self._closed_texts(), {_KARAOKE})
        self.assertIsNone(res.get("question"))

    def test_broken_ai_json_falls_back_to_regex(self):
        # Структурный сбой разбора → регулярки (фолбэк), а не тишина.
        res = ra.handle_ack_reply(
            self._state(self._confirm_close_q()), "оставь только один, остальное закрывай",
            answer_fn=lambda t, c: None)
        self.assertEqual(self._status(), {})
        self.assertFalse(res["consume"])


# ==========================================================================
# ISS-48 (побочное): уже закрытые не попадают в список «что закрыть»
# ==========================================================================
class TestClosedNotOffered(_Base):
    def test_terminal_filtered_from_block_close(self):
        sm.set_task_status(self.sd, _KARAOKE, sm.STATUS_DONE, source="test", date="2026-06-04")
        result = {"applied": [], "questions": [
            {"kind": "block_close", "tier": 3, "segment": None, "matched": None,
             "label": None, "scope": "mine", "owners": []}], "edits": [],
            "config_requests": [], "unclear": "", "already": []}
        built = ra.build_ack(result, series_dir=self.sd, snapshot=None,
                             replier="Илья Рыбалка")
        self.assertNotIn("караоке", built["text"].lower())
        self.assertIn("Доделать агента на Claude Code", built["text"])
        self.assertEqual(built["question"]["options"], [_AGENT])

    def test_block_close_scope_everyone_lists_all_owners(self):
        result = {"applied": [], "questions": [
            {"kind": "block_close", "tier": 3, "segment": None, "matched": None,
             "label": None, "scope": "everyone", "owners": []}], "edits": [],
            "config_requests": [], "unclear": "", "already": []}
        built = ra.build_ack(result, series_dir=self.sd, snapshot=None,
                             replier="Илья Рыбалка")
        self.assertIn("у всех участников", built["text"])
        self.assertIn(_MEDIA, built["text"])
        self.assertEqual(len(built["question"]["options"]), 6)

    def test_block_close_scope_named(self):
        result = {"applied": [], "questions": [
            {"kind": "block_close", "tier": 3, "segment": None, "matched": None,
             "label": None, "scope": "named",
             "owners": ["Татьяна Филиппова"]}], "edits": [],
            "config_requests": [], "unclear": "", "already": []}
        built = ra.build_ack(result, series_dir=self.sd, snapshot=None,
                             replier="Илья Рыбалка")
        self.assertIn("Татьяна Филиппова", built["text"])
        self.assertEqual(set(built["question"]["options"]), {_MEDIA, _RASCH})

    def test_block_close_named_unknown_says_so(self):
        result = {"applied": [], "questions": [
            {"kind": "block_close", "tier": 3, "segment": None, "matched": None,
             "label": None, "scope": "named", "owners": ["Пётр Неизвестный"]}], "edits": [],
            "config_requests": [], "unclear": "", "already": []}
        built = ra.build_ack(result, series_dir=self.sd, snapshot=None,
                             replier="Илья Рыбалка")
        self.assertIn("не нашёл", built["text"])         # не молчим о сужении
        self.assertEqual(set(built["question"]["options"]), {_KARAOKE, _AGENT})


# ==========================================================================
# ISS-49: разбор JSON от ИИ (нормализация/защита)
# ==========================================================================
class TestAnswerJsonParsing(unittest.TestCase):
    def test_plain(self):
        r = aiu.parse_answer_json(
            '{"intent":"apply","all":true,"positions":[1,2],"keep":[3],'
            '"scope":"everyone","owners":["Татьяна"],"cls":"done","unclear":""}')
        self.assertEqual(r["intent"], "apply")
        self.assertEqual(r["positions"], [1, 2])
        self.assertEqual(r["keep"], [3])
        self.assertEqual(r["scope"], "everyone")

    def test_fenced_and_noise(self):
        r = aiu.parse_answer_json('```json\n{"intent":"decline"}\n```')
        self.assertEqual(r["intent"], "decline")
        self.assertEqual(r["positions"], [])

    def test_garbage(self):
        self.assertIsNone(aiu.parse_answer_json("не json вообще"))
        self.assertIsNone(aiu.parse_answer_json(""))

    def test_unknown_values_normalized(self):
        r = aiu.parse_answer_json(
            '{"intent":"нечто","scope":"вселенная","cls":"хз","positions":["2","x",0,-1]}')
        self.assertEqual(r["intent"], "unclear")
        self.assertEqual(r["scope"], "shown")
        self.assertIsNone(r["cls"])
        self.assertEqual(r["positions"], [2])

    def test_named_without_owners_downgrades(self):
        r = aiu.parse_answer_json('{"intent":"apply","scope":"named","owners":[]}')
        self.assertEqual(r["scope"], "shown")

    def test_prompt_marks_shown_and_carries_reply_as_data(self):
        p = aiu.build_user_prompt("закрой всё", [_KARAOKE, _MEDIA], shown_count=1)
        self.assertIn("[показан в вопросе]", p.split("2.")[0])
        self.assertIn("<<<ОТВЕТ", p)


# ==========================================================================
# ISS-49: прочие режимы переспроса — тот же ИИ-слой
# ==========================================================================
class TestOtherModesUseAi(_Base):
    def test_confirm_foreign_by_description(self):
        q = {"mode": "confirm_foreign", "label": "done", "options": [_MEDIA, _RASCH],
             "labels": ["done", "done"], "word": None}
        res = ra.handle_ack_reply(
            self._state(q), "только тот, что про медиаплан, второй не трогай",
            answer_fn=lambda t, c: {"intent": "apply", "all": False, "positions": [1],
                                    "keep": [2], "scope": "shown", "owners": [],
                                    "cls": None, "unclear": ""})
        self.assertEqual(self._closed_texts(), {_MEDIA})
        self.assertTrue(res["consume"])

    def test_clarify_class_from_ai(self):
        q = {"mode": "clarify_class", "label": None, "matched": _KARAOKE, "word": "замяли"}
        res = ra.handle_ack_reply(
            self._state(q), "да там оно само рассосалось",
            answer_fn=lambda t, c: {"intent": "class", "all": False, "positions": [],
                                    "keep": [], "scope": "shown", "owners": [],
                                    "cls": "cancelled", "unclear": ""})
        self.assertEqual(self._status()[sm._status_key(_KARAOKE)], sm.STATUS_CANCELLED)
        # Слово в долгую память из ИИ-догадки НЕ уходит (канон R24 — класс называет человек).
        self.assertFalse(res["learned"])

    def test_pick_target_by_description(self):
        q = {"mode": "pick_target", "label": "done", "options": [_KARAOKE, _AGENT],
             "word": None}
        ra.handle_ack_reply(
            self._state(q), "это про агента, конечно",
            answer_fn=lambda t, c: {"intent": "apply", "all": False, "positions": [2],
                                    "keep": [], "scope": "shown", "owners": [],
                                    "cls": None, "unclear": ""})
        self.assertEqual(self._closed_texts(), {_AGENT})


# ==========================================================================
# ISS-48: охват доезжает из understand-слоя в вопрос
# ==========================================================================
class TestUnderstandCarriesScope(unittest.TestCase):
    def test_parse_block_scope(self):
        r = ru._parse_understand(
            '{"statuses":[],"edits":[],"series_config":[],"block_close":true,'
            '"block_scope":"named","block_owners":["Татьяна","Мария"],"unclear":[]}')
        self.assertTrue(r["block_close"])
        self.assertEqual(r["block_scope"], "named")
        self.assertEqual(r["block_owners"], ["Татьяна", "Мария"])

    def test_scope_defaults_to_mine(self):
        r = ru._parse_understand('{"statuses":[],"block_close":true}')
        self.assertEqual(r["block_scope"], "mine")
        self.assertEqual(r["block_owners"], [])

    def test_named_without_owners_downgrades(self):
        r = ru._parse_understand(
            '{"statuses":[],"block_close":true,"block_scope":"named","block_owners":[]}')
        self.assertEqual(r["block_scope"], "mine")


if __name__ == "__main__":
    unittest.main()
