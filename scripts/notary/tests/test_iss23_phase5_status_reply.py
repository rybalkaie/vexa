"""Тесты Ф5 (план `2026-06-24-pending-items-lifecycle.md`) — активное обновление
статуса висяка ОТВЕТОМ участника на протокол (REQ R9, R5, переоткрытие A10).

Покрывает критерий «сделано» Ф5:
  - детектор интента: закрыт/снят/жду/под-сомнением/переоткрыть; правка текста и
    уточнение спикера НЕ распознаются как статус (консервативно — A9);
  - матчинг «о каком висяке reply»: по тексту висяков серии, дейктический «этот»
    резолвится лишь при единственном пункте, неоднозначность → None;
  - запись через писатель Ф2 `set_task_status` (sidecar, переживает регенерацию) →
    на СЛЕДУЮЩЕМ протоколе пункт уходит из висящих (R5 cancelled / R9 done) или
    возвращается (A10 reopen);
  - listener-маршрут: reply от НЕ-владельца принят (A4), правка не перехвачена,
    голос не наш путь, ack отправлен (ОЖИД1);
  - LLM-ярус интента: дефолт-OFF (claude не зовётся), vetо может ТОЛЬКО отозвать
    в правки;
  - приватность (опасная тройка): тексты reply/висяков в лог не уходят.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss23_phase5_status_reply -v
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
import notary.meetings_listener as ml  # noqa: E402


# Протокол с блоком «Задачи» (два висяка под `**Имя**`) — даёт непустой хвост.
_PROTO = """#протоколвстречи 03.06.2026

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

_ITEMS = [
    "Татьяна Филиппова: Подготовить медиаплан на июль",
    "Татьяна Филиппова: Прислать расчёт по складу Ozon",
]


# ==========================================================================
# Детектор интента (детерминированный, без сети)
# ==========================================================================
class TestDetectIntent(unittest.TestCase):

    def test_done_variants(self):
        for t in ("этот закрыт", "уже сделали", "готово", "выполнено", "решили", "done"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_DONE, t)

    def test_cancelled_variants(self):
        for t in ("снимаем, неактуально", "отменяем", "передумали", "больше не нужно", "снят"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_CANCELLED, t)

    def test_wait_variants(self):
        for t in ("жду расчёт", "ещё не закрыли", "в работе", "пока не успел"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_WAIT, t)

    def test_reopen_variants(self):
        for t in ("нет, не закрыто", "верни вопрос", "переоткрой", "рано закрыли, верните"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_REOPEN, t)

    def test_doubt_variants(self):
        for t in ("вроде закрыто", "кажется решили", "наверное закрыт", "надо проверить"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_DOUBT, t)

    def test_not_status_returns_none(self):
        # Правки текста / уточнение спикера / удаление задачи — НЕ статус (A9).
        for t in (
            "в пункте 3 не Озон, а Wildberries",
            "закрой кавычку во втором абзаце",
            "убери задачу про медиаплан",
            "Спикер 3 это Дарья",
            "",
            "   ",
        ):
            self.assertIsNone(p.detect_status_intent(t), t)

    def test_doubt_beats_done(self):
        # «вроде закрыто» — буфер doubt (R10), НЕ done.
        self.assertEqual(p.detect_status_intent("вроде закрыто"), p.LABEL_DOUBT)

    def test_reopen_beats_done(self):
        # «не закрыто» — переоткрытие, НЕ done.
        self.assertEqual(p.detect_status_intent("это не закрыто"), p.LABEL_REOPEN)


class TestFU1NegationReopen(unittest.TestCase):
    """FU-1 (R1): ответ-отрицание «не <done-слово>» = НЕ сделано → reopen, НЕ done.

    Инцидент-корень: бот принимал «не сделал/не выполнил/не решил» за «сделано» и молча
    закрывал задачу, которую человек назвал НЕзакрытой (худший класс ошибки, инвариант
    R16 «лучше ложно-висит»). Матрица форм глагола × отрицание + регресс «ещё не …» → wait.
    """

    # {основа × форма} — все мужские «-л», средние/женские, «выполн» целиком.
    _REOPEN_FORMS = (
        "не сделал", "не сделали", "не сделала", "не сделано", "не сделан",
        "не решил", "не решили", "не решено", "не решён", "не решен",
        "не выполнил", "не выполнили", "не выполнено", "не выполнена",
        "не закрыл", "не закрыто", "не закрыт",
        "не готов", "не готово",
        "не отработано", "не отработал",       # зеркало _DONE_RE «отработано»
        "так и не сделал",                      # длинная форма
        "не выполнил план до конца",            # длинная форма — не ловит done по «выполн»
    )

    def test_negations_map_to_reopen(self):
        for t in self._REOPEN_FORMS:
            self.assertEqual(p.detect_status_intent(t), p.LABEL_REOPEN, t)

    def test_still_waiting_stays_wait_not_reopen(self):
        # «ещё не …» / «пока не …» — ожидание (wait), НЕ переоткрытие: _WAIT_FIRST раньше.
        for t in ("ещё не сделал", "пока не сделал", "ещё не выполнил", "пока не решил"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_WAIT, t)

    def test_positive_without_negation_unbroken(self):
        # Расширение — только под префиксом «не …»; позитив по-прежнему done.
        for t in ("сделал", "сделали", "выполнил", "выполнено", "решил", "отработано"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_DONE, t)


class TestSplitNegationReopen(unittest.TestCase):
    """ход3/У1 (разорванное отрицание): «не» + идиома НЕуспеха, когда done-слово стоит НЕ
    вплотную к «не» («не получилось закрыть», «не удалось решить», «не было сделано»).
    Раньше проваливались в done → ЛОЖНОЕ закрытие. Теперь → reopen (R16). Регресс: позитив
    остаётся done, «ещё/пока не …» — wait, команда «не удаляй» НЕ уходит в reopen."""

    _REOPEN = (
        "не было сделано", "не было закрыто", "не было решено", "не было выполнено",
        "не получилось закрыть", "не получается сделать", "не получилось решить",
        "не удалось решить", "не удалось закрыть вопрос", "не удалась задача",
        "не смогли выполнить", "не смог закрыть", "не смогла сделать",
        "не вышло закрыть",
    )

    def test_split_negations_map_to_reopen(self):
        for t in self._REOPEN:
            self.assertEqual(p.detect_status_intent(t), p.LABEL_REOPEN, t)

    def test_still_trying_stays_wait(self):
        # «ещё/пока не получилось/удалось» — всё ещё в работе (wait), не reopen: _WAIT_FIRST.
        for t in ("ещё не получилось закрыть", "пока не удалось решить", "ещё не смогли"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_WAIT, t)

    def test_positive_completion_not_reopened(self):
        # Ключевой инвариант фикса: расширение — СТРОГО под префиксом «не», поэтому идиома
        # успеха без «не» НЕ уходит в reopen. (done vs None зависит от инфинитивности глагола —
        # «удалось решить»/«смогли выполнить» дают None: «решить»/«выполнить» инфинитивы, не
        # `_DONE_RE`; это pre-existing и вне scope фикса. Проверяем именно «не reopen».)
        for t in ("получилось закрыть", "удалось решить", "смогли выполнить", "всё сделали"):
            self.assertNotEqual(p.detect_status_intent(t), p.LABEL_REOPEN, t)
        self.assertEqual(p.detect_status_intent("всё сделали"), p.LABEL_DONE)

    def test_delete_command_not_reopen(self):
        # «не удаляй» — команда удаления, НЕ статус-переоткрытие: анкор `удал[оаи]сь` не задет.
        self.assertNotEqual(p.detect_status_intent("не удаляй задачу"), p.LABEL_REOPEN)


# ==========================================================================
# Матчинг «о каком висяке reply»
# ==========================================================================
class TestMatchItem(unittest.TestCase):

    def test_token_overlap(self):
        self.assertEqual(
            p.match_pending_item("снимаем вопрос про расчёт по складу, неактуально", _ITEMS),
            _ITEMS[1],
        )

    def test_other_item(self):
        self.assertEqual(p.match_pending_item("медиаплан готов", _ITEMS), _ITEMS[0])

    def test_inflection_prefix(self):
        # «расчёта» (склонение) матчит «расчёт».
        self.assertEqual(p.match_pending_item("расчёта дождались, закрыт", _ITEMS), _ITEMS[1])

    def test_deictic_single_item(self):
        self.assertEqual(p.match_pending_item("закрыт", ["Один висяк про склад"]),
                         "Один висяк про склад")

    def test_deictic_multi_is_ambiguous(self):
        self.assertIsNone(p.match_pending_item("этот закрыт", _ITEMS))

    def test_no_overlap_is_none(self):
        self.assertIsNone(p.match_pending_item("закрыт вопрос про бюджет рекламы", _ITEMS))

    def test_empty_items(self):
        self.assertIsNone(p.match_pending_item("закрыт", []))


# ==========================================================================
# apply_status_reply — backend-путь до sidecar + следующий протокол
# ==========================================================================
class _SeriesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-06-03", _PROTO,
                               {"series": "koord", "date": "2026-06-03"})

    def tearDown(self):
        self._tmp.cleanup()

    def _next_block(self):
        digs = sm.resolve_memory(self.sd, self.root, current_date="2026-06-11")
        return sm.build_open_tasks_block(digs, series_dir=self.sd, meeting_sid="next")


class TestApplyBackend(_SeriesBase):

    def test_r5_cancel_removes_from_hanging(self):
        # R5: «снимаем X, неактуально» → cancelled → не висит, показан как «снято».
        r = p.apply_status_reply("снимаем расчёт по складу, неактуально", self.sd,
                                 date="2026-06-10")
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], sm.STATUS_CANCELLED)
        key = sm._status_key("Татьяна Филиппова: Прислать расчёт по складу Ozon")
        self.assertEqual(sm.load_task_status(self.sd)[key]["status"], sm.STATUS_CANCELLED)
        block = self._next_block()
        hanging = block.split("ЧАСТЬ 2")[0]
        self.assertNotIn("ozon", hanging.lower())     # не висит
        self.assertIn("Закрыто", block)               # показан как закрытый
        self.assertIn("медиаплан", hanging.lower())   # второй висяк на месте

    def test_r9_done_single_named(self):
        # R9: «медиаплан готов» → done; на след. протоколе уходит в «закрыто».
        r = p.apply_status_reply("медиаплан готов", self.sd, date="2026-06-10")
        self.assertEqual(r["status"], sm.STATUS_DONE)
        hanging = self._next_block().split("ЧАСТЬ 2")[0]
        self.assertNotIn("медиаплан", hanging.lower())

    def test_wait_keeps_hanging(self):
        r = p.apply_status_reply("жду расчёт по складу", self.sd, date="2026-06-10")
        self.assertEqual(r["status"], sm.STATUS_OPEN)
        key = sm._status_key("Татьяна Филиппова: Прислать расчёт по складу Ozon")
        self.assertEqual(sm.load_task_status(self.sd)[key]["status"], sm.STATUS_OPEN)
        hanging = self._next_block().split("ЧАСТЬ 2")[0]
        self.assertIn("ozon", hanging.lower())        # остался висящим

    def test_a10_reopen_restores_hanging(self):
        # Закрыли, затем «нет, не закрыто, верни» → снова висит (A10).
        p.apply_status_reply("расчёт по складу закрыт", self.sd, date="2026-06-10")
        key = sm._status_key("Татьяна Филиппова: Прислать расчёт по складу Ozon")
        self.assertEqual(sm.load_task_status(self.sd)[key]["status"], sm.STATUS_DONE)
        r = p.apply_status_reply("нет, расчёт по складу не закрыто, верни", self.sd,
                                 date="2026-06-11")
        self.assertEqual(r["label"], p.LABEL_REOPEN)
        rec = sm.load_task_status(self.sd)[key]
        self.assertEqual(rec["status"], sm.STATUS_OPEN)
        self.assertIsNone(rec["reason"])              # переоткрытие чистит причину
        hanging = self._next_block().split("ЧАСТЬ 2")[0]
        self.assertIn("ozon", hanging.lower())        # вернулся в висящие

    def test_doubt_goes_to_buffer(self):
        r = p.apply_status_reply("расчёт по складу вроде закрыто", self.sd, date="2026-06-10")
        self.assertEqual(r["status"], sm.STATUS_DOUBT)
        block = self._next_block()
        self.assertIn("подтвердите", block.lower())   # буфер «вроде закрыто»

    def test_text_edit_not_intercepted(self):
        # Консервативно (A9): правка текста → None, sidecar НЕ тронут.
        r = p.apply_status_reply("в задаче не Ozon, а Wildberries", self.sd, date="2026-06-10")
        self.assertIsNone(r)
        self.assertEqual(sm.load_task_status(self.sd), {})

    def test_ambiguous_deictic_not_applied(self):
        # «этот закрыт» при ДВУХ висяках — неоднозначно → None (не угадываем).
        r = p.apply_status_reply("этот закрыт", self.sd, date="2026-06-10")
        self.assertIsNone(r)
        self.assertEqual(sm.load_task_status(self.sd), {})

    def test_no_pending_no_status(self):
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "empty"
            empty.mkdir()
            self.assertIsNone(p.apply_status_reply("закрыт", empty, date="2026-06-10"))


# ==========================================================================
# LLM-ярус интента: дефолт-OFF + vetо только в сторону правок
# ==========================================================================
class TestIntentLLM(_SeriesBase):

    def test_gate_default_off(self):
        # Без флага — claude не зовётся, вердикт «unclear».
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_PENDING_STATUS_INTENT_LLM", None)
            self.assertFalse(p.is_intent_llm_enabled())
            self.assertEqual(p.classify_reply_intent_llm("закрыт", _ITEMS), "unclear")

    def test_llm_off_ignores_classify_fn(self):
        # Гейт OFF → classify_fn НЕ зовётся (детерминированное решение в силе).
        boom = mock.Mock(side_effect=AssertionError("classify_fn не должен зваться при OFF"))
        os.environ.pop("ENABLE_PENDING_STATUS_INTENT_LLM", None)
        r = p.apply_status_reply("медиаплан готов", self.sd, date="2026-06-10",
                                 classify_fn=boom)
        self.assertIsNotNone(r)
        boom.assert_not_called()

    def test_llm_veto_routes_to_edit(self):
        # Гейт ON + вердикт «edit» → vetо: статус НЕ ставим, sidecar чист.
        with mock.patch.dict(os.environ, {"ENABLE_PENDING_STATUS_INTENT_LLM": "1"}):
            r = p.apply_status_reply("медиаплан готов", self.sd, date="2026-06-10",
                                     classify_fn=lambda *a, **k: "edit")
            self.assertIsNone(r)
            self.assertEqual(sm.load_task_status(self.sd), {})

    def test_llm_status_proceeds(self):
        with mock.patch.dict(os.environ, {"ENABLE_PENDING_STATUS_INTENT_LLM": "1"}):
            r = p.apply_status_reply("медиаплан готов", self.sd, date="2026-06-10",
                                     classify_fn=lambda *a, **k: "status")
            self.assertIsNotNone(r)
            self.assertEqual(r["status"], sm.STATUS_DONE)


# ==========================================================================
# Listener-маршрут: A4 (любой участник), консервативность, ack, голос
# ==========================================================================
class TestListenerRoute(_SeriesBase):

    GROUP = 222          # групповой чат серии (cid != allowed_chat)
    OWNER = 111          # allowed_chat (владелец)
    NON_OWNER = 777      # обычный участник

    def _msg(self, text, *, voice=False):
        m = {
            "chat": {"id": self.GROUP},
            "from": {"id": self.NON_OWNER, "first_name": "Дарья"},
            "reply_to_message": {"message_id": 555, "text": "📅 протокол"},
            "text": text,
            "message_id": 999,
            "date": 1718000000,
        }
        if voice:
            m["voice"] = {"file_id": "x"}
            m.pop("text")
        return m

    def _run(self, msg):
        sent = []
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda *a, **k: sent.append((a, k))):
            claimed = ml.maybe_route_to_pending_status_reply(
                "tok", self.GROUP, self.OWNER, msg)
        return claimed, sent

    def test_non_owner_status_accepted(self):
        # A4: reply от НЕ-владельца принят — статус проставлен + ack отправлен.
        claimed, sent = self._run(self._msg("снимаем расчёт по складу, неактуально"))
        self.assertTrue(claimed)
        self.assertEqual(len(sent), 1)                         # ack один раз
        self.assertEqual(sent[0][1].get("reply_to"), 999)      # ack — reply_to
        key = sm._status_key("Татьяна Филиппова: Прислать расчёт по складу Ozon")
        self.assertEqual(sm.load_task_status(self.sd)[key]["status"], sm.STATUS_CANCELLED)

    def test_text_edit_falls_through(self):
        # Правка текста → маршрут НЕ перехватывает (False), ack нет, sidecar чист.
        claimed, sent = self._run(self._msg("в задаче не Ozon, а Wildberries"))
        self.assertFalse(claimed)
        self.assertEqual(sent, [])
        self.assertEqual(sm.load_task_status(self.sd), {})

    def test_voice_not_our_path(self):
        claimed, sent = self._run(self._msg("", voice=True))
        self.assertFalse(claimed)
        self.assertEqual(sent, [])

    def test_not_reply_falls_through(self):
        msg = self._msg("закрыт")
        msg.pop("reply_to_message")
        claimed, sent = self._run(msg)
        self.assertFalse(claimed)

    def test_unknown_series_falls_through(self):
        # _reply_context_series → None (не наш протокол) → False.
        with mock.patch.object(ml, "_reply_context_series", return_value=None), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ml, "send_message") as sm_send:
            claimed = ml.maybe_route_to_pending_status_reply(
                "tok", self.GROUP, self.OWNER, self._msg("закрыт расчёт по складу"))
        self.assertFalse(claimed)
        sm_send.assert_not_called()


# ==========================================================================
# Приватность (опасная тройка): тексты reply/висяков не уходят в лог
# ==========================================================================
class TestPrivacyLogging(_SeriesBase):

    def test_apply_logs_no_text(self):
        secret = "складу Ozon"
        with self.assertLogs("notary.pending_status_reply", level="INFO") as cm:
            p.apply_status_reply("снимаем расчёт по складу, неактуально", self.sd,
                                 date="2026-06-10")
        blob = "\n".join(cm.output)
        self.assertIn("label=", blob)            # код статуса/счётчики есть
        self.assertNotIn(secret, blob)           # текста висяка/reply нет
        self.assertNotIn("неактуально", blob)


# ==========================================================================
# Регрессии цикла-5 (ход1 Н1/Н2, ход3 У1/У2) — защита фиксов от reintroduction
# ==========================================================================
class TestCycle5Regressions(_SeriesBase):

    def test_future_tense_not_closed(self):
        # Н1: будущее «выполни-М завтра» — НЕ закрытие (было ложно-DONE → ложно-закрыто).
        self.assertNotEqual(p.detect_status_intent("выполним завтра расчёт"), p.LABEL_DONE)
        r = p.apply_status_reply("выполним завтра расчёт по складу", self.sd, date="2026-06-10")
        if r is not None:                                   # если взяли — только как «висит»
            self.assertEqual(r["status"], sm.STATUS_OPEN)
        self.assertNotIn("done", str((sm.load_task_status(self.sd) or {})).lower())

    def test_yo_normalization(self):
        # Н2: формы с ё распознаются наравне с «е» (детектор ё→е, как matcher `_norm`).
        self.assertEqual(p.detect_status_intent("решён"), p.LABEL_DONE)
        self.assertEqual(p.detect_status_intent("не решён"), p.LABEL_REOPEN)

    def test_partial_qualifier_keeps_hanging(self):
        # У1: «почти/наполовину/частично готово» → висит (WAIT), не закрывается молча (R16).
        for t in ("почти готово", "наполовину сделали", "частично закрыли", "не до конца сделали"):
            self.assertEqual(p.detect_status_intent(t), p.LABEL_WAIT, t)
        r = p.apply_status_reply("медиаплан почти готов", self.sd, date="2026-06-10")
        self.assertEqual(r["status"], sm.STATUS_OPEN)       # остался висеть, не closed
        hanging = self._next_block().split("ЧАСТЬ 2")[0]
        self.assertIn("медиаплан", hanging.lower())

    def test_ack_names_matched_item(self):
        # У2: ack называет матчнутый висяк — участник видит, тот ли пункт затронут.
        r = p.apply_status_reply("медиаплан готов", self.sd, date="2026-06-10")
        self.assertIn("медиаплан", r["ack"].lower())


# ==========================================================================
# ISS-36 случай Б (Ф1): статус по ФИНАЛЬНОЙ клаузе, а не по первому статус-слову
# эхо-текста задачи. Развёрнутый ответ «задача — <эхо-статус>, статус? — <вердикт>»
# теперь помечается вердиктом из ПОСЛЕДНЕЙ клаузы.
# ==========================================================================
class TestFinalClauseIntent(unittest.TestCase):

    def test_echo_wait_then_final_done(self):
        # RISK1: реальный WAIT-токен («в работе») в ЭХО-тексте задачи ПЕРЕД финальным
        # «сделано». Старый фикс-приоритет WAIT>DONE дал бы wait (корень кейса Б);
        # предпочтение финальной клаузы даёт done. Пример несёт НАСТОЯЩИЙ WAIT-паттерн
        # («в работе» матчит `\bв\s+работ`) — не плоское «висит», которое ни один
        # WAIT-регекс не ловит (иначе на старом коде уже был бы done → ложно-зелёный).
        self.assertEqual(
            p.detect_status_intent("Отслушать сэмплы — в работе, статус? — сделано"),
            p.LABEL_DONE,
        )

    def test_multi_intent_last_clause_wins(self):
        # RISK5 (СОЗНАТЕЛЬНАЯ смена приоритета, A4): «жду…, но…сделал» → done —
        # побеждает ПОСЛЕДНЯЯ клауза, а не фикс-приоритет WAIT>DONE, как было.
        self.assertEqual(
            p.detect_status_intent("жду расчёт, но задачу сделал"), p.LABEL_DONE)

    def test_status_not_in_final_clause_falls_back(self):
        # Статус в ПЕРВОЙ клаузе, финальная — без статуса (приписка/ссылка): идём с
        # конца и находим done в более ранней клаузе → распознавание НЕ теряется.
        self.assertEqual(
            p.detect_status_intent("медиаплан сделал — вот ссылка на файл"),
            p.LABEL_DONE,
        )

    def test_no_status_anywhere_is_none(self):
        # Ни в одной клаузе статуса нет → None (предпочтение финальной клаузы не
        # расширяет распознаваемость ложно — правки текста остаются не нашим путём).
        self.assertIsNone(
            p.detect_status_intent("в пункте 3 не Озон — а Wildberries, посмотрите"))

    def test_single_intents_regress_with_separator(self):
        # Критерий Ф1: одиночные интенты не сломаны, в т.ч. с разделителем «—».
        cases = {
            "не закрыто": p.LABEL_REOPEN,
            "почти готово": p.LABEL_WAIT,
            "задача — снимаем": p.LABEL_CANCELLED,
            "задача — сделано": p.LABEL_DONE,
        }
        for text, want in cases.items():
            self.assertEqual(p.detect_status_intent(text), want, text)


class TestFinalClauseApply(_SeriesBase):
    """Кейс Б и мульти-интент ДОХОДЯТ до sidecar на протокольном пути (source=reply/
    mixed-reply) — фикс единый, зовётся из `apply_status_reply` (общий вход)."""

    def test_case_b_echo_wait_then_done_writes_done(self):
        # Кейс Б целиком: эхо «в работе» + финальное «сделано» → sidecar DONE, НЕ OPEN.
        r = p.apply_status_reply(
            "Прислать расчёт по складу — в работе, статус? — сделано", self.sd,
            date="2026-06-10", source="reply")
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], sm.STATUS_DONE)
        key = sm._status_key("Татьяна Филиппова: Прислать расчёт по складу Ozon")
        self.assertEqual(sm.load_task_status(self.sd)[key]["status"], sm.STATUS_DONE)

    def test_multi_intent_apply_writes_done(self):
        # RISK5 на протокольном пути (source=mixed-reply): «жду… но задачу сделал» → DONE.
        r = p.apply_status_reply(
            "жду расчёт по складу, но задачу сделал", self.sd,
            date="2026-06-10", source="mixed-reply")
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], sm.STATUS_DONE)
        key = sm._status_key("Татьяна Филиппова: Прислать расчёт по складу Ozon")
        self.assertEqual(sm.load_task_status(self.sd)[key]["status"], sm.STATUS_DONE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
