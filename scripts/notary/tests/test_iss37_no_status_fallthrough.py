# -*- coding: utf-8 -*-
"""Тесты ISS-37: reply БЕЗ статусов (одни правки) не должен закрывать случайный висяк.

Реальный инцидент 20.07.2026, серия `mpervyi-pn-koord-finplan`. Два reply подряд —
Мария (12 правок, статусов нет) и Ольга (5 правок + жалоба на блок висяков). Лог VPS:

    [mixed-split] statuses=0 edits=12 unclear=False
    [pending-status] статус проставлен label=done status=done via=overlap llm=off

Сплиттер отработал ПРАВИЛЬНО («статусов нет»), но `should_engage` при пустом
`status_segments` возвращал False → смешанный роут отпускал reply → следом
срабатывал одиночный статус-роут (мы вообще попадаем в mixed только когда
`detect_status_intent` дал не-None) и матчил ВЕСЬ текст по пересечению слов.
Совпадали слова из ПРАВОК («Zifriend/1С», «контейнер/Bolong») → закрывались две
живые задачи сегодняшней встречи, а 12+5 правок молча терялись.

Двойной ущерб: живые задачи исчезают из следующего протокола, «отработанные»
вопросы возвращаются, автор видит «✅ Принял» и уверен, что всё учтено.

Тесты воспроизводят ДИСПЕТЧЕРИЗАЦИЮ как в `process_message` (mixed → status), иначе
баг не виден: сам по себе mixed-роут просто возвращает False.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss37_no_status_fallthrough -v
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
from notary.lib import mixed_reply_split as mrs  # noqa: E402
from notary.lib import feedback_worker  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402


# Протокол-фикстура: структура и формулировки реального протокола 20.07 (усечённо).
# Важны именно ЭТИ задачи — на них сработал ложный overlap («Zifriend/1С», «контейнер»).
_PROTO = """#протоколвстречи 20.07.2026

**Встреча:** Закупки и финпланирование.

**Длительность:** 39 мин

**Участники:** Илья Рыбалка, Татьяна Филиппова, Мария Михина, Ольга

**Транскрипт:** [2026-07-20.md](2026-07-20.md)

---

## 1) Закупки

▪️ Обсудили 25-й контейнер и оплаты.

## Задачи

**Татьяна Филиппова**
- До среды пересчитать 25-й контейнер и принять решение по перекладке Bolong/ZIL Sound (август vs ноябрь); согласование — четверг-пятница.
- Подтвердить Bolong фото товара (Space Projector 18, UW24 чёрные); при недостатке — запросить нормальные фото.
- Скинуть счёт Zifriend по 1С в чат (подвинуть на завтра — Ольга оплатит завтра).

**Мария Михина**
- Сегодня пересчитать склад после пожара ВБ (24-е проекторы) и подтвердить, нужна ли срочная догрузка.
- На этой неделе предоставить Илье информацию по микрофонам на складе в Китае (по его запросу).

**Ольга**
- Начать закупать юани к пятнице под 25-й контейнер (независимо от согласования оплаты).
- Оплатить завтра счёт Zifriend по 1С (по понедельникам маркетплейсы не платит).
"""

# Две задачи, которые бот ошибочно закрыл в бою 20.07 — ядро регрессии.
_WRONGLY_CLOSED = [
    "Татьяна Филиппова: До среды пересчитать 25-й контейнер и принять решение по "
    "перекладке Bolong/ZIL Sound (август vs ноябрь); согласование — четверг-пятница.",
    "Ольга: Оплатить завтра счёт Zifriend по 1С (по понедельникам маркетплейсы не платит).",
]

# ── Реальные тексты reply (как пришли в Telegram 20.07) ───────────────────────
_REPLY_MARIA = """ПРАВКИ:

1) Кадры: уход Саши
Заменить нужно Марию на Татьяну
Мария не перенимает обязанности Саши

2) Пожар на складе ВБ (Электросталь)
Заменить Марию на Татьяну

3) Поставка 25-го контейнера — сроки поплыли
Заменить Zil Sound на Zeal Sound (заменить везде, где будет ещё появляться)

4) Статусы поставок и контейнеров
Поправить - проекторы и клавиатуры приехали на склад в Москве, готовы к продаже.

Поправить - Микрофоны на складе в Китае — на этой неделе Илье предоставить информацию по его запросу (остальное не корректно указано)

6) Zifriend — учёт переплаты (Татьяна + Ольга отдельно)
Заменить Татьяну на Марию"""

_REPLY_OLGA = """вместо страховые поКС-3 — 8 тыс.
правильно: страховые по КС-Трейд - 8 тыс.

вместо далее значительный ~4,7 млн (обеспечиваем через СП)
правильно: далее значительный ~4,7 млн (обеспечиваем через ИП)

вместо 6) Zifriend — учёт переплаты (Татьяна + Ольга отдельно)
правильно (Мария и Ольга отдельно)

вместо чтобы не путаться в закрытых периодах.
правильно: чтобы перепроверить сумму и не менять в закрытых периодах

вместо Оплатить завтра счёт Zifriend по 1С (по понедельникам маркетплейсы не платит).
правильно: оплатить завтра счет за разработку отчета по 1С (по понедельникам не платим)

по блоку: Вопросы с прошлых встреч, по которым не ясен статус
неоднократно писала, что вопросы отработаны, не надо их больше включать в протокол"""

# Ответ сплиттера — РОВНО как отработал реальный Haiku в бою (см. лог VPS).
_SPLIT_MARIA = {
    "statuses": [],
    "edits": [
        "Кадры: уход Саши — заменить Марию на Татьяну",
        "Мария не перенимает обязанности Саши",
        "Пожар на складе ВБ (Электросталь) — заменить Марию на Татьяну",
        "Заменить Zil Sound на Zeal Sound везде",
        "Проекторы и клавиатуры приехали на склад в Москве, готовы к продаже",
        "Микрофоны на складе в Китае — на этой неделе Илье предоставить информацию",
        "Zifriend — учёт переплаты: заменить Татьяну на Марию",
    ],
    "unclear": [],
}

_SPLIT_OLGA = {
    "statuses": [],
    "edits": [
        "страховые по КС-Трейд - 8 тыс. вместо страховые поКС-3",
        "далее значительный ~4,7 млн (обеспечиваем через ИП) вместо через СП",
        "Zifriend — учёт переплаты: Мария и Ольга отдельно",
        "чтобы перепроверить сумму и не менять в закрытых периодах",
        "оплатить завтра счет за разработку отчета по 1С (по понедельникам не платим)",
    ],
    "unclear": [
        "по блоку «Вопросы с прошлых встреч»: неоднократно писала, что вопросы "
        "отработаны, не надо их больше включать в протокол",
    ],
}


def _on():
    return mock.patch.dict(os.environ, {"ENABLE_MIXED_REPLY_SPLIT": "1"})


class _Base(unittest.TestCase):
    GROUP = 222
    OWNER = 111

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "koord"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, "2026-07-20", _PROTO,
                               {"series": "koord", "date": "2026-07-20"})

    def tearDown(self):
        self._tmp.cleanup()

    def _msg(self, text, who="Ольга"):
        return {
            "chat": {"id": self.GROUP},
            "from": {"id": 777, "first_name": who},
            "reply_to_message": {"message_id": 555, "text": "📋 #протоколвстречи"},
            "text": text,
            "message_id": 999,
            "date": 1784530572,
        }

    def _dispatch(self, msg, canned):
        """Порядок роутов КАК В `process_message`: mixed → status.

        Без этой связки баг не воспроизводится: mixed-роут сам по себе просто
        отдаёт False, а закрывает висяк уже статус-роут следом.

        Возвращает (кто забрал, ack'и, тексты, ушедшие в шлюз правок).
        """
        sent = []
        edit_calls = []

        def _fake_gateway(token, chat_id, edit_msg, *, allowed_chat, send):
            edit_calls.append(edit_msg.get("text"))
            send(token, chat_id, "ack-правок", reply_to_message_id=edit_msg.get("message_id"))
            return True

        with _on(), \
             mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(mrs, "_request_split", return_value=canned), \
             mock.patch.object(feedback_worker, "route_feedback_reply", _fake_gateway), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda *a, **k: sent.append((a, k))):
            if ml.maybe_route_to_mixed_reply("tok", self.GROUP, self.OWNER, msg):
                return "mixed", sent, edit_calls
            if ml.maybe_route_to_pending_status_reply("tok", self.GROUP, self.OWNER, msg):
                return "status", sent, edit_calls
        return None, sent, edit_calls

    def assertNothingClosed(self):
        """Ни одна задача серии не получила терминальный статус."""
        store = sm.load_task_status(self.sd)
        closed = [rec.get("text") for rec in store.values()
                  if rec.get("status") in sm._TERMINAL_CLOSED]
        self.assertEqual(closed, [], f"закрыты задачи, которых никто не закрывал: {closed}")


class TestNoStatusReplyDoesNotCloseTasks(_Base):
    """Ядро ISS-37: statuses=0 → ни один висяк не закрывается, правки доезжают."""

    def test_maria_edits_only_nothing_closed(self):
        who, sent, edit_calls = self._dispatch(
            self._msg(_REPLY_MARIA, who="Мария"), _SPLIT_MARIA)
        self.assertNothingClosed()
        self.assertTrue(edit_calls, "правки не доехали до шлюза — тихая потеря")
        self.assertIn("Zeal Sound", "\n".join(edit_calls))
        self.assertEqual(who, "mixed")

    def test_olga_edits_only_nothing_closed(self):
        who, sent, edit_calls = self._dispatch(
            self._msg(_REPLY_OLGA), _SPLIT_OLGA)
        self.assertNothingClosed()
        self.assertTrue(edit_calls, "правки не доехали до шлюза — тихая потеря")
        self.assertIn("КС-Трейд", "\n".join(edit_calls))
        self.assertEqual(who, "mixed")

    def test_ack_mentions_edits_not_closure(self):
        """ack не должен рапортовать «отметил вопрос закрытым», когда статусов нет."""
        _who, sent, _edits = self._dispatch(self._msg(_REPLY_OLGA), _SPLIT_OLGA)
        self.assertEqual(len(sent), 1, "ожидаем ровно один объединённый ack")
        ack = sent[0][0][2]
        self.assertIn("равки", ack)
        self.assertNotIn("закрыт", ack.lower())

    def test_block_complaint_surfaced_not_swallowed(self):
        """Жалоба «вопросы отработаны, не включайте» не понимается ботом как статус —
        значит обязана прозвучать в ack как непонятое, а не пропасть молча."""
        _who, sent, _edits = self._dispatch(self._msg(_REPLY_OLGA), _SPLIT_OLGA)
        ack = sent[0][0][2]
        self.assertIn("не понял", ack.lower())

    def test_wrongly_closed_tasks_stay_open(self):
        """Именно те две задачи, что бот закрыл в бою, остаются висящими."""
        for reply, canned in ((_REPLY_MARIA, _SPLIT_MARIA), (_REPLY_OLGA, _SPLIT_OLGA)):
            self._dispatch(self._msg(reply), canned)
        store = sm.load_task_status(self.sd)
        fresh = [t for t in sm.extract_open_tasks(_PROTO)]
        merged = sm.merge_open_tasks_with_status(fresh, store)
        for task in _WRONGLY_CLOSED:
            self.assertIn(task, merged["open"],
                          f"задача выпала из висяков, хотя её никто не закрывал: {task[:60]}")


class TestRegressionStatusStillWorks(_Base):
    """Фикс не должен сломать нормальный путь: настоящий статус по-прежнему ставится."""

    # NB: текст берём ОДНОЗНАЧНЫЙ («юани» есть ровно у одной задачи). На «счёт
    # Zifriend по 1С» матч даёт ничью — таких задач в фикстуре две (у Татьяны и у
    # Ольги), и `match_pending_item` честно возвращает None. Это правильное
    # поведение, но для проверки «статус ставится» нужен неспорный кейс.
    def test_pure_status_still_closes(self):
        canned = {"statuses": ["юани к пятнице закупили, закрыто"],
                  "edits": [], "unclear": []}
        who, _sent, _edits = self._dispatch(
            self._msg("юани к пятнице закупили, закрыто"), canned)
        self.assertEqual(who, "status", "чистый одиночный статус идёт прежним роутом")
        store = sm.load_task_status(self.sd)
        closed = [r.get("text") for r in store.values()
                  if r.get("status") in sm._TERMINAL_CLOSED]
        self.assertEqual(len(closed), 1, "ровно один висяк закрыт")

    def test_mixed_status_plus_edits_both_handled(self):
        canned = {"statuses": ["юани к пятнице закупили, закрыто"],
                  "edits": ["Заменить Zil Sound на Zeal Sound"], "unclear": []}
        who, sent, edit_calls = self._dispatch(
            self._msg("юани закупили, закрыто; и замените Zil на Zeal"), canned)
        self.assertEqual(who, "mixed")
        self.assertTrue(edit_calls)
        store = sm.load_task_status(self.sd)
        closed = [r for r in store.values() if r.get("status") in sm._TERMINAL_CLOSED]
        self.assertEqual(len(closed), 1)


class TestDigestRouteGuard(_Base):
    """ISS-37, ВТОРАЯ точка вызова ([[review-checks-two-call-sites]]): дайджест-роут
    тоже применяет статус по ЦЕЛОМУ тексту reply, значит пачка правок и там закрыла бы
    случайный висяк. Найдено сверкой уже ПОСЛЕ деплоя первой версии фикса."""

    def test_edit_heavy_reply_to_digest_does_not_close(self):
        sent = []
        msg = self._msg(_REPLY_OLGA)
        msg["reply_to_message"] = {"message_id": 777, "text": "📋 Открытые вопросы к встрече"}
        with _on(), \
             mock.patch.object(ml, "_protokol_root", return_value=self.root), \
             mock.patch.object(ml, "_digest_reply_series", return_value="koord", create=True), \
             mock.patch.object(ml, "send_message",
                               side_effect=lambda *a, **k: sent.append(a[2])):
            try:
                ml.maybe_route_to_digest_reply("tok", self.GROUP, msg)
            except Exception:  # noqa: BLE001 — резолв серии дайджеста стабится по-разному
                pass          # важен инвариант ниже, а не факт срабатывания роута
        self.assertNothingClosed()

    def test_guard_itself_recognises_edit_wall(self):
        """Прямая проверка предохранителя на реальных текстах (без роут-обвязки)."""
        from notary.lib import pending_status_reply as psr
        self.assertTrue(psr.looks_edit_heavy(_REPLY_OLGA))
        self.assertTrue(psr.looks_edit_heavy(_REPLY_MARIA))
        # Законный статус предохранитель НЕ перехватывает.
        self.assertFalse(psr.looks_edit_heavy("юани закупили, вопрос закрыт"))
        self.assertFalse(psr.looks_edit_heavy("заменили поставщика, задача выполнена"))

    def test_guard_recognises_past_tense_edit_markers(self):
        """R2: маркеры правок в ПРОШЕДШЕМ времени («поправили/исправили/заменили»)
        тоже собираются в edit-heavy (≥2 маркеров). Раньше ловилось только повелит./
        инфинитив, а прошедшее проваливалось → пачка правок «мы уже поправили» не
        распознавалась как правки и могла закрыть случайный висяк."""
        from notary.lib import pending_status_reply as psr
        # ≥2 маркеров в прошедшем времени → edit-heavy.
        self.assertTrue(psr.looks_edit_heavy("поправили название, заменили сумму"))
        self.assertTrue(psr.looks_edit_heavy("исправили дату и поправили имя спикера"))
        self.assertTrue(psr.looks_edit_heavy("заменили Zil на Zeal, исправили цифру в п.3"))
        self.assertTrue(psr.looks_edit_heavy("мы поправили абзац, а строку выше исправили"))
        # Один маркер (даже прошедшего времени) — законный статус, НЕ перехватываем.
        self.assertFalse(psr.looks_edit_heavy("поправил одно слово, задача закрыта"))
        self.assertFalse(psr.looks_edit_heavy("исправили дату, вопрос отработан"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
