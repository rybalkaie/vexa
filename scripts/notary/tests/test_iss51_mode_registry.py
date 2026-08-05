# -*- coding: utf-8 -*-
"""Ф1 (ISS-51…55): режим открытых вопросов в реестре + знание своей серии (ISS-52).

Покрывает:
  • `series_open_questions_mode` — резолв режима серии, дефолт `last-only`,
    мусорное значение → дефолт + предупреждение в лог, нормализация ключа (ISS-7);
  • валидацию поля `open_questions_mode` в записи watched.yaml;
  • `series_for_chat` — обратный резолв «чат → серия» по УНИКАЛЬНЫМ сериям
    (одна серия обычно = несколько записей: наивный список дал бы ложное «несколько»);
  • ISS-52: в групповом чате серии просьба прислать файл срабатывает БЕЗ reply
    на протокол; при двух сериях на чат — переспрос; в личке владельца вопрос
    «какую серию» остаётся законным.

Чистые dict-резолверы тестируются БЕЗ pyyaml (инъекция `watched`).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss51_mode_registry -v
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.cli import registry  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402


def _watched(*recs):
    return {"watched": list(recs)}


def _rec(series, **fields):
    rec = {"id": f"{series}-evt", "series": series, "type": "manual",
           "cron": "0 11 * * 2", "tz": "Europe/Moscow"}
    rec.update(fields)
    return rec


# ==========================================================================
# Резолв режима серии
# ==========================================================================
class TestOpenQuestionsModeResolve(unittest.TestCase):

    def setUp(self):
        # Предупреждение о мусорном режиме дедуплицируется на время процесса —
        # между тестами память сбрасываем, иначе второй тест с тем же мусором
        # проверял бы дедуп, а не то, что он проверяет.
        registry._OQM_WARNED.clear()

    def test_default_is_last_only(self):
        # Решение владельца 3 (29.07): дефолт для новых серий — справочный режим.
        w = _watched(_rec("koord"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_carry_is_read(self):
        w = _watched(_rec("anzhee-direktorat", open_questions_mode="carry"))
        self.assertEqual(
            registry.series_open_questions_mode("anzhee-direktorat", w), "carry")

    def test_last_only_is_read(self):
        w = _watched(_rec("koord", open_questions_mode="last-only"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_garbage_falls_back_to_default_and_warns(self):
        # План Ф1: «неизвестное значение → дефолт last-only + предупреждение в лог».
        w = _watched(_rec("koord", open_questions_mode="накопление"))
        with self.assertLogs("notary.registry", level="WARNING") as cm:
            mode = registry.series_open_questions_mode("koord", w)
        self.assertEqual(mode, "last-only")
        self.assertTrue(any("open_questions_mode" in line for line in cm.output))

    def test_garbage_does_not_leak_carry(self):
        # Мусор НЕ должен случайно включить накопление ни при какой форме.
        for junk in (1, True, [], "carry-on", "last_only", "накопление"):
            w = _watched(_rec("koord", open_questions_mode=junk))
            with self.assertLogs("notary.registry", level="WARNING"):
                mode = registry.series_open_questions_mode("koord", w)
            self.assertEqual(mode, "last-only", f"мусор {junk!r} дал {mode}")

    def test_null_value_is_absence_not_garbage(self):
        # `open_questions_mode:` без значения в YAML даёт None — это ОТСУТСТВИЕ
        # поля, а не опечатка: молчим, берём дефолт, лог не засоряем.
        w = _watched(_rec("koord", open_questions_mode=None))
        with mock.patch.object(registry.logger, "warning") as warn:
            mode = registry.series_open_questions_mode("koord", w)
        self.assertEqual(mode, "last-only")
        warn.assert_not_called()

    def test_garbage_record_does_not_hide_valid_one(self):
        # Опечатка в одной записи серии не должна прятать валидное значение
        # соседней: иначе Директорат тихо стал бы шпаргалкой.
        w = _watched(_rec("anzhee-direktorat", open_questions_mode="накопление"),
                     _rec("anzhee-direktorat", open_questions_mode="carry"))
        with self.assertLogs("notary.registry", level="WARNING"):
            mode = registry.series_open_questions_mode("anzhee-direktorat", w)
        self.assertEqual(mode, "carry")

    def test_case_and_spaces_normalised(self):
        # Значение поля нормализуется: регистр и пробелы — не мусор.
        w = _watched(_rec("koord", open_questions_mode="  Carry  "))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "carry")

    def test_series_key_drift_resolves(self):
        # ISS-7: дрейф ключа серии (регистр/пробелы/кавычки) не должен ронять режим.
        w = _watched(_rec("anzhee-direktorat", open_questions_mode="carry"))
        for ref in ("anzhee-direktorat", "Anzhee-Direktorat", "  anzhee-direktorat  ",
                    "«anzhee-direktorat»"):
            self.assertEqual(registry.series_open_questions_mode(ref, w), "carry", ref)

    def test_exact_match_wins_over_normalised(self):
        w = _watched(_rec("Koord", open_questions_mode="last-only"),
                     _rec("koord", open_questions_mode="carry"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "carry")

    def test_first_exact_match_wins_over_later_exact(self):
        # Правило «первое ТОЧНОЕ совпадение выигрывает» — как у соседей по реестру
        # (`get_telegram_chat_id_for_series` возвращает первый валидный chat_id).
        # Без него режим серии решала бы ПОСЛЕДНЯЯ запись, и резолв разошёлся бы
        # с остальным реестром молча.
        w = _watched(_rec("koord", open_questions_mode="carry"),
                     _rec("koord", open_questions_mode="last-only"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "carry")

    def test_garbage_warning_is_deduped(self):
        # Резолвер зовётся на каждом дайджесте/протоколе; одна опечатка в реестре
        # не должна печатать WARNING в каждом проходе.
        w = _watched(_rec("koord", open_questions_mode="накопление"))
        with self.assertLogs("notary.registry", level="WARNING") as cm:
            registry.series_open_questions_mode("koord", w)
        self.assertEqual(len(cm.output), 1)
        with mock.patch.object(registry.logger, "warning") as warn:
            for _ in range(5):
                registry.series_open_questions_mode("koord", w)
        warn.assert_not_called()

    def test_normalised_empty_series_key_is_default(self):
        # «Нормализованно-пустой» ключ («  », «»») после normalize_series_key
        # становится "" и совпал бы с записью, чья series тоже пуста, — это матч
        # по пустоте, а не по серии.
        w = _watched({"id": "x", "series": "  ", "type": "manual",
                      "open_questions_mode": "carry"})
        for ref in ("«»", "  ", '""'):
            self.assertEqual(registry.series_open_questions_mode(ref, w),
                             "last-only", repr(ref))

    def test_first_non_empty_among_series_records(self):
        # Пер-record поле, резолв на уровень серии: первое непустое (как company).
        w = _watched(_rec("koord"), _rec("koord", open_questions_mode="carry"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "carry")

    def test_foreign_series_not_matched(self):
        w = _watched(_rec("anzhee-direktorat", open_questions_mode="carry"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_empty_series_is_default(self):
        w = _watched(_rec("koord", open_questions_mode="carry"))
        for ref in ("", None, 0, []):
            self.assertEqual(registry.series_open_questions_mode(ref, w), "last-only")

    def test_broken_watched_is_default(self):
        for w in (None, {}, {"watched": None}, {"watched": ["мусор"]}, "строка"):
            self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_carry_series_is_not_disturbed_by_default(self):
        # Т4: серия-накопление остаётся carry, когда рядом живут last-only серии.
        w = _watched(_rec("anzhee-direktorat", open_questions_mode="carry"),
                     _rec("koord"), _rec("marketplaces-tatiana"))
        self.assertEqual(
            registry.series_open_questions_mode("anzhee-direktorat", w), "carry")
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")


# ==========================================================================
# Валидация поля в записи
# ==========================================================================
class TestOpenQuestionsModeValidation(unittest.TestCase):

    ROOMS = {"rooms": []}

    def _errors(self, **fields):
        rec = _rec("koord", **fields)
        return registry.validate_watched_record(rec, self.ROOMS)

    def test_absent_field_is_valid(self):
        self.assertEqual([e for e in self._errors() if "open_questions_mode" in e], [])

    def test_valid_values_pass(self):
        for v in ("carry", "last-only", "CARRY", " last-only "):
            errs = [e for e in self._errors(open_questions_mode=v)
                    if "open_questions_mode" in e]
            self.assertEqual(errs, [], f"{v!r} должно проходить")

    def test_garbage_is_flagged(self):
        for v in ("накопление", "lastonly", 1, True, []):
            errs = [e for e in self._errors(open_questions_mode=v)
                    if "open_questions_mode" in e]
            self.assertTrue(errs, f"{v!r} должно флагаться")

    def test_field_is_editable_from_cli(self):
        """Поле, которого нет в EDITABLE_FIELDS, правится только руками в YAML.

        Фаза 7 плана требует пройти реестр и осознанно проставить режим КАЖДОЙ
        серии; до фазы 2 (команда боту) CLI — единственный путь. Разрыв
        «валидатор знает поле, редактор не знает» уже существовал для `genre`,
        поэтому сторож проверяет оба.
        """
        try:
            from notary.cli import meeting_watch  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001 — click/yaml может не быть в venv
            self.skipTest(f"cli.meeting_watch недоступен: {type(e).__name__}")
        for field in ("open_questions_mode", "genre"):
            self.assertIn(field, meeting_watch.EDITABLE_FIELDS,
                          f"{field} валидируется, но не редактируется из CLI")
            self.assertEqual(meeting_watch.EDITABLE_FIELDS[field], "str")


# ==========================================================================
# Обратный резолв «чат → серия»
# ==========================================================================
class TestSeriesForChat(unittest.TestCase):

    def test_single_series_resolved(self):
        w = _watched(_rec("koord", telegram_chat_id=-100500))
        self.assertEqual(registry.series_for_chat(-100500, w), ["koord"])

    def test_several_records_of_one_series_collapse(self):
        # Одна серия = несколько записей — это норма реестра. Наивный список дал бы
        # ложное «несколько серий» и лишний переспрос у пользователя.
        w = _watched(_rec("koord", telegram_chat_id=-100500),
                     _rec("koord", telegram_chat_id=-100500),
                     _rec("koord", telegram_chat_id=-100500))
        self.assertEqual(registry.series_for_chat(-100500, w), ["koord"])

    def test_key_drift_collapses_too(self):
        w = _watched(_rec("koord", telegram_chat_id=-100500),
                     _rec("Koord", telegram_chat_id=-100500))
        self.assertEqual(len(registry.series_for_chat(-100500, w)), 1)

    def test_two_series_one_chat_both_returned(self):
        # Одна комната обслуживает несколько серий (@t11) — реальный случай.
        w = _watched(_rec("koord", telegram_chat_id=-100500),
                     _rec("vystavka", telegram_chat_id=-100500))
        self.assertEqual(sorted(registry.series_for_chat(-100500, w)),
                         ["koord", "vystavka"])

    def test_foreign_chat_empty(self):
        w = _watched(_rec("koord", telegram_chat_id=-100500))
        self.assertEqual(registry.series_for_chat(-100999, w), [])

    def test_invalid_chat_id_empty(self):
        w = _watched(_rec("koord", telegram_chat_id=-100500))
        for bad in (None, True, False, "‑100500", 0.5, []):
            self.assertEqual(registry.series_for_chat(bad, w), [], repr(bad))
        # Единственный вход, где ранняя проверка типа РЕАЛЬНО решает: в Python
        # `1 == True`, и без неё chat_id=True сматчил бы запись с привязкой 1.
        self.assertEqual(
            registry.series_for_chat(True, _watched(_rec("koord", telegram_chat_id=1))), [])

    def test_disabled_record_is_not_a_series(self):
        # Владелец выключил серию — планировщик её больше не ведёт (scheduler.py),
        # значит и «в чате какой серии я нахожусь» она больше не отвечает.
        w = _watched(_rec("koord-old", telegram_chat_id=-100500, enabled=False))
        self.assertEqual(registry.series_for_chat(-100500, w), [])

    def test_ignored_record_is_not_a_series(self):
        w = _watched(_rec("koord-old", telegram_chat_id=-100500, ignored=True))
        self.assertEqual(registry.series_for_chat(-100500, w), [])

    def test_live_series_survives_dead_neighbour(self):
        # Главный сценарий Р2: в общей комнате рядом с живой серией лежит мёртвая
        # (закрытая выставка / осиротевшая после пересоздания встречи). Одной такой
        # записи хватало, чтобы чат навсегда ушёл в переспрос и ISS-52 в нём умер.
        w = _watched(_rec("koord-old", telegram_chat_id=-100500, enabled=False),
                     _rec("koord", telegram_chat_id=-100500),
                     _rec("vystavka-2025", telegram_chat_id=-100500, ignored=True))
        self.assertEqual(registry.series_for_chat(-100500, w), ["koord"])

    def test_bool_binding_ignored(self):
        # bool — подкласс int: True не должен матчиться на chat_id=1.
        w = _watched(_rec("koord", telegram_chat_id=True))
        self.assertEqual(registry.series_for_chat(1, w), [])

    def test_broken_watched_empty(self):
        for w in (None, {}, {"watched": None}, {"watched": ["мусор"]}, "строка"):
            self.assertEqual(registry.series_for_chat(-100500, w), [])

    def test_record_without_series_skipped(self):
        w = _watched({"id": "x", "type": "manual", "telegram_chat_id": -100500})
        self.assertEqual(registry.series_for_chat(-100500, w), [])


# ==========================================================================
# ISS-52: просьба в групповом чате серии — без reply на протокол
# ==========================================================================
class TestSendArtifactWithoutReply(unittest.TestCase):

    OWNER = 12345
    GROUP = -100500

    def _msg(self, text, *, chat=None, reply=False):
        m = {"chat": {"id": chat if chat is not None else self.GROUP},
             "from": {"id": 42, "first_name": "Мария"},
             "message_id": 1000, "date": 1718000000, "text": text}
        if reply:
            m["reply_to_message"] = {"message_id": 900, "text": "📋 Открытые вопросы…"}
        return m

    def test_group_single_series_works_without_reply(self):
        # Т10 / ISS-52: в чате серии просьба срабатывает без ответа-реплая.
        # Глагол здесь из СЕГОДНЯШНЕГО списка детекта: независимость от
        # формулировки — вторая половина Т10, её закрывает Ф6 (ISS-51).
        with mock.patch.object(ml, "_chat_context_series", return_value=(["koord"], None)), \
             mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
             mock.patch.object(ml, "send_message") as sm:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER,
                self._msg("пришлите актуальный файл с открытыми вопросами"))
        self.assertTrue(claimed)
        rd.assert_called_once()
        self.assertEqual(rd.call_args[0][2], "koord")
        sm.assert_not_called()

    def test_group_two_series_asks_which(self):
        # Несколько серий на чат → переспрашиваем, файл не шлём.
        with mock.patch.object(ml, "_chat_context_series",
                               return_value=(["koord", "vystavka"], None)), \
             mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp, \
             mock.patch.object(ml, "send_message") as sm:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER, self._msg("скинь протокол"))
        self.assertTrue(claimed)
        rd.assert_not_called()
        rp.assert_not_called()
        sm.assert_called_once()
        said = sm.call_args[0][2]
        self.assertIn("koord", said)
        self.assertIn("vystavka", said)

    def test_owner_dm_without_reply_passes_through(self):
        # Личка владельца к серии не привязана. Без реплая роут уступает дальше —
        # ровно как до ISS-52: в личке владелец пользуется командами, и перехват
        # артефакт-роутом отобрал бы у них сообщение.
        with mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
             mock.patch.object(ml, "send_message") as sm:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.OWNER, self.OWNER,
                self._msg("пришли протокол", chat=self.OWNER))
        self.assertFalse(claimed)
        rd.assert_not_called()
        sm.assert_not_called()

    def test_unbound_group_stays_silent(self):
        # Бот сидит в группе, не привязанной ни к одной серии: без реплая молчим,
        # иначе он отвечал бы на каждую фразу с глаголом отправки.
        with mock.patch.object(ml, "_chat_context_series", return_value=([], None)), \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp, \
             mock.patch.object(ml, "send_message") as sm:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER, self._msg("пришли протокол"))
        self.assertFalse(claimed)
        rp.assert_not_called()
        sm.assert_not_called()

    def test_reply_without_context_still_hints(self):
        # Прежний контракт ISS-45 не тронут: на РЕПЛАЙ без контекста серии
        # бот по-прежнему отвечает мягкой подсказкой, а не молчит.
        with mock.patch.object(ml, "_reply_context_series", return_value=None), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value=None), \
             mock.patch.object(ml, "_chat_context_series", return_value=([], None)), \
             mock.patch.object(ml, "send_message") as sm:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER,
                self._msg("пришли протокол", reply=True))
        self.assertTrue(claimed)
        sm.assert_called_once()

    def test_plain_chatter_not_intercepted(self):
        # Обычная переписка в группе не перехватывается (детект узкий).
        for text in ("привет, как дела", "спасибо!", "закрой третью",
                     "завтра встречаемся в 11", "юани — закупили"):
            with mock.patch.object(ml, "_chat_context_series",
                                   return_value=(["koord"], None)), \
                 mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
                 mock.patch.object(ml, "_resend_protocol", return_value=True) as rp, \
                 mock.patch.object(ml, "send_message") as sm:
                claimed = ml.maybe_route_to_send_artifact(
                    "tok", self.GROUP, self.OWNER, self._msg(text))
            self.assertFalse(claimed, text)
            rd.assert_not_called()
            rp.assert_not_called()
            sm.assert_not_called()

    def test_status_reply_still_not_stolen_without_reply(self):
        # Гейт detect_status_intent остаётся: «пришли, что сделано» не крадём.
        with mock.patch.object(ml, "_chat_context_series", return_value=(["koord"], None)), \
             mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
             mock.patch.object(ml, "send_message") as sm:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER,
                self._msg("пришли протокол — по второй задаче всё сделано"))
        self.assertFalse(claimed)
        rd.assert_not_called()
        sm.assert_not_called()

    def test_reply_context_still_wins(self):
        # Прежний путь не сломан: reply на протокол по-прежнему даёт серию.
        with mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value=None), \
             mock.patch.object(ml, "_chat_context_series") as ccs, \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER,
                self._msg("пришли протокол", reply=True))
        self.assertTrue(claimed)
        rp.assert_called_once()
        ccs.assert_not_called()


# ==========================================================================
# Хелпер резолва чата в listener
# ==========================================================================
class TestChatContextSeries(unittest.TestCase):

    OWNER = 12345
    GROUP = -100500

    def test_owner_dm_returns_nothing(self):
        # Личка владельца не привязана к серии — фолбэк по чату не применяем.
        # Мок обязателен: без него тест (а) зелен даже с удалённым гейтом, потому
        # что на машине без реестра load_watched вернёт пустой список, и (б) на
        # боевой машине читает НАСТОЯЩИЙ watched.yaml — где команда «серию X шли
        # в личку» привязывает серию к чату владельца, и тест краснеет мимо кода.
        with mock.patch("notary.cli.registry.load_watched") as lw:
            series, err = ml._chat_context_series(self.OWNER, self.OWNER)
        lw.assert_not_called()
        self.assertEqual(series, [])
        self.assertIsNone(err)

    def test_broken_registry_does_not_kill_listener(self):
        # 🔴 `registry._read_yaml` на не-словаре верхнего уровня кидает SystemExit —
        # потомок BaseException, сквозь `except Exception` он пролетал насквозь и
        # гасил процесс листенера ДО save_offset (петля перезапусков всего бота).
        with mock.patch("notary.cli.registry.load_watched",
                        side_effect=SystemExit("ожидался словарь верхнего уровня")):
            series, err = ml._chat_context_series(self.GROUP, self.OWNER)
        self.assertEqual(series, [])
        self.assertEqual(err, "SystemExit")

    def test_broken_resolve_does_not_kill_listener(self):
        w = _watched(_rec("koord", telegram_chat_id=self.GROUP))
        with mock.patch("notary.cli.registry.load_watched", return_value=w), \
             mock.patch("notary.cli.registry.series_for_chat",
                        side_effect=SystemExit("bang")):
            series, err = ml._chat_context_series(self.GROUP, self.OWNER)
        self.assertEqual(series, [])
        self.assertEqual(err, "SystemExit")

    def test_group_reads_registry(self):
        w = _watched(_rec("koord", telegram_chat_id=self.GROUP))
        with mock.patch("notary.cli.registry.load_watched", return_value=w):
            series, err = ml._chat_context_series(self.GROUP, self.OWNER)
        self.assertEqual(series, ["koord"])
        self.assertIsNone(err)

    def test_registry_failure_is_soft(self):
        # Реестр недоступен → пустой список и причина, роут даёт прежнюю подсказку.
        with mock.patch("notary.cli.registry.load_watched", side_effect=OSError("нет файла")):
            series, err = ml._chat_context_series(self.GROUP, self.OWNER)
        self.assertEqual(series, [])
        self.assertIsNotNone(err)


# ==========================================================================
# 🔴 Битый реестр не валит листенер (SystemExit из _read_yaml)
# ==========================================================================
class TestBrokenRegistryDoesNotKillRoute(unittest.TestCase):

    OWNER = 12345
    GROUP = -100500

    def _msg(self, text):
        return {"chat": {"id": self.GROUP}, "from": {"id": 42},
                "message_id": 1000, "date": 1718000000, "text": text}

    def test_route_survives_systemexit_from_registry(self):
        with mock.patch("notary.cli.registry.load_watched",
                        side_effect=SystemExit("ожидался словарь верхнего уровня")), \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp, \
             mock.patch.object(ml, "send_message") as sm:
            with self.assertLogs("meetings-listener", level="WARNING"):
                claimed = ml.maybe_route_to_send_artifact(
                    "tok", self.GROUP, self.OWNER, self._msg("пришли протокол"))
        self.assertFalse(claimed)
        rp.assert_not_called()
        sm.assert_not_called()

    def test_route_survives_real_broken_watched_yaml(self):
        # Тот же сценарий на НАСТОЯЩЕМ файле: верхний уровень watched.yaml —
        # список вместо словаря (частая ручная правка: снесли ключ `watched:`).
        try:
            import yaml  # noqa: F401,PLC0415
        except ImportError:  # pragma: no cover — системный python без pyyaml
            self.skipTest("pyyaml недоступен")
        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory(prefix="broken-registry-") as td:
            broken = Path(td) / "watched.yaml"
            broken.write_text("- id: koord-evt\n  series: koord\n", encoding="utf-8")
            with mock.patch.object(registry, "WATCHED_FILE", broken), \
                 mock.patch.object(ml, "_resend_protocol", return_value=True) as rp, \
                 mock.patch.object(ml, "send_message") as sm:
                # Сам факт SystemExit из реестра — предпосылка теста.
                with self.assertRaises(SystemExit):
                    registry.load_watched()
                with self.assertLogs("meetings-listener", level="WARNING"):
                    claimed = ml.maybe_route_to_send_artifact(
                        "tok", self.GROUP, self.OWNER, self._msg("пришли протокол"))
        self.assertFalse(claimed)
        rp.assert_not_called()
        sm.assert_not_called()


class TestSetChatCommandBrokenRegistry(unittest.TestCase):
    """Однотипный зазор в соседнем роуте: он читает реестр под flock'ом."""

    OWNER = 12345
    GROUP = -100500

    def test_broken_registry_does_not_kill_set_chat(self):
        msg = {"chat": {"id": self.GROUP}, "from": {"id": self.OWNER},
               "message_id": 1000, "date": 1718000000, "text": "серию koord шли сюда"}
        with mock.patch("notary.cli.registry.load_watched",
                        side_effect=SystemExit("ожидался словарь верхнего уровня")), \
             mock.patch.object(ml, "send_message") as sm:
            with self.assertLogs("meetings-listener", level="WARNING"):
                claimed = ml.maybe_route_to_set_chat_command(
                    "tok", self.GROUP, self.OWNER, msg)
        # Видимая реакция вместо падения процесса (и без утечки flock'а).
        self.assertTrue(claimed)
        sm.assert_called_once()


# ==========================================================================
# 🔴 Без реплая узкий детект остаётся узким (голый «файл/версия» не интент)
# ==========================================================================
BARE_FILE_PHRASES = (
    "Оля, пришли файл с ценами",
    "дайте актуальную версию договора",
    "дай ссылку на файл",
    "кинь файл, который вчера обсуждали",
    "отправь, пожалуйста, файл с макетом",
    "покажи версию для печати",
    "повтори, какой файл нужен",
)


class TestBareFileNeedsReply(unittest.TestCase):

    OWNER = 12345
    GROUP = -100500

    def _msg(self, text, *, reply=False):
        m = {"chat": {"id": self.GROUP}, "from": {"id": 42},
             "message_id": 1000, "date": 1718000000, "text": text}
        if reply:
            m["reply_to_message"] = {"message_id": 900, "text": "📋 Открытые вопросы…"}
        return m

    def _route(self, text, **kw):
        with mock.patch.object(ml, "_chat_context_series", return_value=(["koord"], None)), \
             mock.patch.object(ml, "_resend_digest", return_value=True) as rd, \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp, \
             mock.patch.object(ml, "send_message") as sm:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER, self._msg(text, **kw))
        return claimed, rd, rp, sm

    def test_bare_file_phrases_ignored_without_reply(self):
        # Рабочая переписка людей между собой в чате серии: без реплая бот НЕ
        # обязан считать «пришли файл с ценами» просьбой к себе — иначе он
        # вываливает в чат PDF протокола встречи (7 из 10 обычных фраз).
        from notary.lib import send_artifact as sa  # noqa: PLC0415
        for text in BARE_FILE_PHRASES:
            self.assertIsNone(
                sa.detect_artifact_request(text, allow_bare_file=False), text)
            claimed, rd, rp, sm = self._route(text)
            self.assertFalse(claimed, text)
            rd.assert_not_called()
            rp.assert_not_called()
            sm.assert_not_called()

    def test_same_phrases_still_work_in_reply(self):
        # С реплаем контекст есть — прежнее поведение ISS-45 сохранено.
        from notary.lib import send_artifact as sa  # noqa: PLC0415
        self.assertEqual(sa.detect_artifact_request("скинь файл"), "protocol")
        self.assertEqual(
            sa.detect_artifact_request("скинь актуальную версию файла",
                                       reply_surface="digest"), "digest")
        with mock.patch.object(ml, "_reply_context_series", return_value=None), \
             mock.patch.object(ml, "_reply_context_series_digest", return_value="koord"), \
             mock.patch.object(ml, "_resend_digest", return_value=True) as rd:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER,
                self._msg("Ты можешь скинуть актуальную версию файла?", reply=True))
        self.assertTrue(claimed)
        rd.assert_called_once()

    def test_unambiguous_requests_still_work_without_reply(self):
        # ISS-52 не сломан: однозначные формулировки работают без реплая.
        claimed, rd, rp, _ = self._route("скинь открытые вопросы")
        self.assertTrue(claimed)
        rd.assert_called_once()
        claimed, rd, rp, _ = self._route("пришли протокол")
        self.assertTrue(claimed)
        rp.assert_called_once()


# ==========================================================================
# Порядок: реестр читается только после детекта; журнал нового пути
# ==========================================================================
class TestRegistryReadOrderAndLogging(unittest.TestCase):

    OWNER = 12345
    GROUP = -100500

    def _msg(self, text):
        return {"chat": {"id": self.GROUP}, "from": {"id": 42},
                "message_id": 1000, "date": 1718000000, "text": text}

    def test_chatter_does_not_touch_registry(self):
        # Реестр не парсится на каждом «привет» в каждой группе: чтение стоит
        # ПОСЛЕ детекта, поэтому площадь отказа реестра = просьбы об артефакте.
        with mock.patch("notary.cli.registry.load_watched") as lw:
            for text in ("привет, как дела", "спасибо!", "завтра в 11"):
                ml.maybe_route_to_send_artifact(
                    "tok", self.GROUP, self.OWNER, self._msg(text))
        lw.assert_not_called()

    def test_real_request_does_read_registry(self):
        w = _watched(_rec("koord", telegram_chat_id=self.GROUP))
        with mock.patch("notary.cli.registry.load_watched", return_value=w) as lw, \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER, self._msg("пришли протокол"))
        self.assertTrue(claimed)
        lw.assert_called_once()
        rp.assert_called_once()

    def test_status_reply_does_not_touch_registry(self):
        # Гейт detect_status_intent стоит ДО чтения реестра.
        with mock.patch("notary.cli.registry.load_watched") as lw:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER,
                self._msg("пришли протокол — по второй задаче всё сделано"))
        self.assertFalse(claimed)
        lw.assert_not_called()

    def test_registry_failure_is_logged_as_warning(self):
        # `logger.debug` при жёстком level=INFO в листенере не попадал в лог НИКОГДА:
        # «чат не привязан» и «реестр упал» выглядели одинаково — то есть никак.
        with mock.patch("notary.cli.registry.load_watched", side_effect=OSError("нет файла")):
            with self.assertLogs("meetings-listener", level="WARNING") as cm:
                claimed = ml.maybe_route_to_send_artifact(
                    "tok", self.GROUP, self.OWNER, self._msg("пришли протокол"))
        self.assertFalse(claimed)
        self.assertTrue(any("OSError" in line for line in cm.output), cm.output)

    def test_successful_chat_resolve_is_logged(self):
        # По журналу видно, что ISS-52 жив и каким путём резолвилась серия.
        with mock.patch.object(ml, "_chat_context_series", return_value=(["koord"], None)), \
             mock.patch.object(ml, "_resend_protocol", return_value=True):
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                ml.maybe_route_to_send_artifact(
                    "tok", self.GROUP, self.OWNER, self._msg("пришли протокол"))
        joined = "\n".join(cm.output)
        self.assertIn("koord", joined)
        self.assertIn(str(self.GROUP), joined)

    def test_ambiguous_branch_is_logged(self):
        with mock.patch.object(ml, "_chat_context_series",
                               return_value=(["koord", "vystavka"], None)), \
             mock.patch.object(ml, "send_message"):
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                ml.maybe_route_to_send_artifact(
                    "tok", self.GROUP, self.OWNER, self._msg("пришли протокол"))
        self.assertTrue(any("несколько серий" in line for line in cm.output), cm.output)

    def test_empty_digest_is_logged(self):
        # «Открытых вопросов нет» бот говорит и при дрейфе ключа серии — в журнале
        # это должно быть видно, иначе уверенно ложный ответ не отличить от честного.
        from notary.lib import morning_digest  # noqa: PLC0415
        with mock.patch.object(morning_digest, "send_series_digest",
                               return_value={"status": "skipped", "reason": "empty"}), \
             mock.patch.object(ml, "send_message"):
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                ml._resend_digest("tok", self.GROUP, "koord", self._msg("скинь вопросы"))
        self.assertTrue(any("koord" in line for line in cm.output), cm.output)

    def test_missing_protocol_is_logged(self):
        with mock.patch.object(ml, "_latest_protocol_path", return_value=None), \
             mock.patch.object(ml, "send_message"):
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                ml._resend_protocol("tok", self.GROUP, "koord", self._msg("пришли протокол"))
        self.assertTrue(any("koord" in line for line in cm.output), cm.output)

    def test_logs_carry_no_message_text(self):
        # Опасная тройка: тексты сообщений/задач/встреч в журнал НЕ попадают.
        secret = "юани закупили по 11.4, договор с Петровым"
        with mock.patch.object(ml, "_chat_context_series", return_value=(["koord"], None)), \
             mock.patch.object(ml, "_resend_protocol", return_value=True):
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                ml.maybe_route_to_send_artifact(
                    "tok", self.GROUP, self.OWNER,
                    self._msg(f"пришли протокол — {secret}"))
        joined = "\n".join(cm.output)
        self.assertNotIn("юани", joined)
        self.assertNotIn("Петров", joined)


# ==========================================================================
# Переспрос при нескольких сериях: второй путь — назвать серию словами
# ==========================================================================
class TestAmbiguousChatNaming(unittest.TestCase):

    OWNER = 12345
    GROUP = -100500

    def _msg(self, text):
        return {"chat": {"id": self.GROUP}, "from": {"id": 42},
                "message_id": 1000, "date": 1718000000, "text": text}

    def _route(self, text, display_map=None):
        with mock.patch.object(ml, "_chat_context_series",
                               return_value=(["koord", "vystavka"], None)), \
             mock.patch.object(ml, "_series_display_map", return_value=display_map or {}), \
             mock.patch.object(ml, "_resend_protocol", return_value=True) as rp, \
             mock.patch.object(ml, "send_message") as sm:
            claimed = ml.maybe_route_to_send_artifact(
                "tok", self.GROUP, self.OWNER, self._msg(text))
        return claimed, rp, sm

    def test_named_series_resolves_without_reply(self):
        claimed, rp, sm = self._route("пришли протокол vystavka")
        self.assertTrue(claimed)
        rp.assert_called_once()
        self.assertEqual(rp.call_args[0][2], "vystavka")
        sm.assert_not_called()

    def test_named_by_display_name_with_russian_ending(self):
        # Человек пишет по-русски и в падеже, а slug латиницей.
        claimed, rp, sm = self._route(
            "пришли протокол координации",
            display_map={"koord": "Координация", "vystavka": "Выставка"})
        self.assertTrue(claimed)
        rp.assert_called_once()
        self.assertEqual(rp.call_args[0][2], "koord")
        sm.assert_not_called()

    def test_no_name_still_asks_and_offers_both_paths(self):
        claimed, rp, sm = self._route("пришли протокол")
        self.assertTrue(claimed)
        rp.assert_not_called()
        sm.assert_called_once()
        said = sm.call_args[0][2]
        self.assertIn("Назовите серию", said)
        self.assertIn("ответьте на протокол", said.lower())

    def test_both_named_is_not_a_guess(self):
        # Названы обе — не угадываем, переспрашиваем.
        claimed, rp, sm = self._route("пришли протокол koord и vystavka")
        self.assertTrue(claimed)
        rp.assert_not_called()
        sm.assert_called_once()

    def test_display_names_shown_to_people(self):
        _, _, sm = self._route("пришли протокол",
                               display_map={"koord": "Координация", "vystavka": "Выставка"})
        said = sm.call_args[0][2]
        self.assertIn("Координация", said)
        self.assertIn("Выставка", said)


# ==========================================================================
# Схема реестра: поле описано владельцу прямо в watched.yaml
# ==========================================================================
class TestWatchedHeaderDocumentsField(unittest.TestCase):

    def test_open_questions_mode_is_documented(self):
        # WATCHED_HEADER вклеивается в сам watched.yaml при каждом сохранении и
        # является единственной документацией полей для владельца (company/
        # visibility/genre там расписаны). До Ф2 выставить режим можно только
        # руками в YAML — без описания владелец не узнает ни имени поля, ни
        # допустимых значений, ни дефолта.
        header = registry.WATCHED_HEADER
        self.assertIn("open_questions_mode", header)
        for token in sorted(registry.VALID_OPEN_QUESTIONS_MODES):
            self.assertIn(token, header)
        self.assertIn(registry.DEFAULT_OPEN_QUESTIONS_MODE, header)


if __name__ == "__main__":
    unittest.main()
