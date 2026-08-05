# -*- coding: utf-8 -*-
"""Ф2 (ISS-51…55): переключение режима открытых вопросов серии командой боту.

Покрывает шесть пунктов Фазы 2 плана:
  1. узкий детерминированный триггер обеих сторон + форма с явным именем серии;
  2. owner-гейт (в группе применяет только владелец);
  3. видимое подтверждение владельцу — дословно;
  4. вопрос «в каком режиме эта серия?»;
  5. строка в чат серии о смене формата к следующей встрече;
  6. обе стороны переключения, отказ не-владельцу, обычная переписка не
     перехватывается.

ТРИГГЕР УЗКИЙ И ЯВНЫЙ (переделка после ревью Ф2). Команда — «режим шпаргалка» /
«режим накопление» / «режим» плюс закрытый список плановых формулировок; всё,
что не совпало с формой ЦЕЛИКОМ, командой не считается. Прошлая версия угадывала
свободную речь морфологией: понимала 9 живых фраз из 23, ШЕСТЬ просьб «выключи
накопление» понимала как «включи», а 8 из 10 бытовых фраз рабочей группы
перехватывала и заставляла бота печатать в групповой чат список внутренних имён
всех серий. Отсюда три инварианта, которые здесь охраняются тестами:
  • форма совпадает с сообщением целиком — иначе не команда;
  • серию не опознали в ГРУППЕ → роут молчит и возвращает False (сообщение
    достаётся роутам ниже, список серий в комнату с людьми не попадает никогда);
  • список внутренних имён серий показывается ТОЛЬКО в личке владельца.

Плюс то, на чём эта зона уже обжигалась: битый реестр кидает `SystemExit` (не
`Exception`) и валил листенер в петлю рестартов; flock утекал при сбое разбора;
`logger.debug` в листенере не виден (уровень INFO); тексты сообщений в журнал
попадать не должны.

Чистые функции (парсер, сеттер реестра) тестируются БЕЗ pyyaml — `watched`
инжектируется словарём.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss51_mode_command -v
"""
from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.cli import registry  # noqa: E402
from notary.lib import mode_command  # noqa: E402
from notary.lib.mode_command import parse_mode_command  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402

OWNER = 12345
GROUP = -100500
OTHER_GROUP = -100777
STRANGER = 999


def _watched(*recs):
    return {"watched": list(recs)}


def _rec(series, **fields):
    rec = {"id": f"{series}-evt", "series": series, "type": "manual",
           "cron": "0 11 * * 2", "tz": "Europe/Moscow"}
    rec.update(fields)
    return rec


def _msg(text, *, chat=GROUP, from_id=OWNER, reply=False):
    m = {"chat": {"id": chat}, "from": {"id": from_id},
         "message_id": 1000, "date": 1718000000, "text": text}
    if reply:
        m["reply_to_message"] = {"message_id": 900, "text": "📋 Открытые вопросы…"}
    return m


@contextlib.contextmanager
def _route_env(watched, *, display_map=None, load_side_effect=None, save_side_effect=None):
    """Реестр и отправка замоканы; `watched` мутируется роутом по-настоящему."""
    load_kw = {"side_effect": load_side_effect} if load_side_effect else {"return_value": watched}
    with mock.patch("notary.cli.registry.load_watched", **load_kw) as lw, \
         mock.patch("notary.cli.registry.save_watched",
                    side_effect=save_side_effect) as sw, \
         mock.patch("notary.cli.registry.release_watched_lock") as rl, \
         mock.patch.object(ml, "_series_display_map", return_value=display_map or {}), \
         mock.patch.object(ml, "send_message", return_value=777) as sm:
        yield SimpleNamespace(load=lw, save=sw, release=rl, send=sm)


def _said(send_mock):
    """Все тексты, отправленные ботом, одной строкой."""
    return "\n".join(str(c.args[2]) for c in send_mock.call_args_list)


# ==========================================================================
# 1. Парсер: явная команда (основная форма + закрытый список форм плана)
# ==========================================================================
class TestParseModeCommand(unittest.TestCase):

    def test_main_command_both_directions(self):
        # Основная форма — то, что владелец диктует каждый раз одинаково.
        c = parse_mode_command("режим шпаргалка")
        self.assertEqual((c.action, c.mode, c.series_ref), ("set", "last-only", ""))
        c = parse_mode_command("режим накопление")
        self.assertEqual((c.action, c.mode, c.series_ref), ("set", "carry", ""))

    def test_main_command_is_forgiving_to_dictation(self):
        # Wispr Flow ставит заглавную, точку и иногда слэш; падеж слова режима
        # владелец тоже не выверяет.
        for text in ("Режим шпаргалка", "режим Шпаргалка.", "/режим шпаргалка",
                     "режим шпаргалку", "  режим   шпаргалка  ", "режим шпаргалка!"):
            got = parse_mode_command(text)
            self.assertIsNotNone(got, text)
            self.assertEqual((got.action, got.mode), ("set", "last-only"), text)
        for text in ("режим накопления", "РЕЖИМ НАКОПЛЕНИЕ"):
            self.assertEqual(parse_mode_command(text).mode, "carry", text)

    def test_plan_wording_both_directions(self):
        # Дословные формы из плана Фазы 2 — закрытый список, работают наравне.
        c = parse_mode_command("эта серия — накопление")
        self.assertEqual((c.action, c.mode, c.series_ref), ("set", "carry", ""))
        c = parse_mode_command("эта серия — шпаргалка")
        self.assertEqual((c.action, c.mode, c.series_ref), ("set", "last-only", ""))

    def test_explicit_series_name_form(self):
        # Форма для лички владельца: чат там ни к какой серии не привязан.
        for text in ("режим шпаргалка Координация", "серию Координация — шпаргалка",
                     "серию Координация шпаргалка"):
            c = parse_mode_command(text)
            self.assertEqual((c.action, c.mode, c.series_ref),
                             ("set", "last-only", "Координация"), text)
        self.assertEqual(parse_mode_command("режим накопление anzhee-direktorat").series_ref,
                         "anzhee-direktorat")  # внутренний дефис слага цел

    def test_human_series_name_is_taken_whole(self):
        # 🔴 Прошлая версия вычитала из имени служебные слова и не резолвила 2 из
        # 4 человеческих имён боевого реестра — тех самых, которые бот печатает
        # в своих же подтверждениях («Закупки И финпланирование»).
        for text, ref in (
            ("режим шпаргалка Закупки и финпланирование (МПервый)",
             "Закупки и финпланирование (МПервый)"),
            ("режим накопление 1-на-1 с Татьяной Филипповой",
             "1-на-1 с Татьяной Филипповой"),
            ("серию Закупки и финпланирование (МПервый) — шпаргалка",
             "Закупки и финпланирование (МПервый)"),
            ("серию 1-на-1 с Татьяной Филипповой — накопление",
             "1-на-1 с Татьяной Филипповой"),
        ):
            self.assertEqual(parse_mode_command(text).series_ref, ref, text)

    def test_dated_slug_is_a_valid_series_name(self):
        # 🔴 Три серии боевого реестра носят дату прямо в slug'е. Прежний гейт
        # `YYYY-MM-DD` гасил команду целиком — бот на корректную команду не
        # отвечал ВООБЩЕ ничего, и серия была недостижима любым набором текста.
        for slug in ("sales-quality-2026-05-27", "mpervyi-produkt-2026-06-11",
                     "oneoff-sargin-2026-06-18"):
            c = parse_mode_command(f"режим накопление {slug}")
            self.assertEqual((c.action, c.mode, c.series_ref), ("set", "carry", slug))
            self.assertEqual(parse_mode_command(f"серию {slug} — шпаргалка").series_ref, slug)
            self.assertEqual(parse_mode_command(f"режим {slug}").series_ref, slug)

    def test_query_forms(self):
        for text in ("режим", "режим?", "в каком режиме эта серия",
                     "в каком режиме эта серия?"):
            got = parse_mode_command(text)
            self.assertIsNotNone(got, text)
            self.assertEqual((got.action, got.series_ref), ("query", ""), text)
        got = parse_mode_command("режим Координация")
        self.assertEqual((got.action, got.series_ref), ("query", "Координация"))

    def test_set_wins_over_query_on_mode_word(self):
        # «режим шпаргалка» — переключение, а не вопрос про серию по имени
        # «шпаргалка»: форма set проверяется раньше.
        self.assertEqual(parse_mode_command("режим шпаргалка").action, "set")

    def test_invisible_and_control_chars_are_stripped(self):
        # Невидимки не исполняются, но ломали резолв серии и отображение ответа.
        self.assertEqual(parse_mode_command("режим​ шпаргалка\x07").mode, "last-only")
        self.assertEqual(parse_mode_command("режим шпаргалка ‮Координация").series_ref,
                         "Координация")

    def test_overlong_series_ref_is_not_a_command(self):
        # `series_ref` уезжает в ответ бота дословно: на длинном тексте ответ
        # перестаёт влезать в лимит Telegram (4096), sendMessage возвращает
        # ошибку — и человек не видит НИЧЕГО (молчаливый провал, нарушение R-REPLY).
        self.assertIsNone(parse_mode_command("режим шпаргалка " + "я" * 5000))
        self.assertIsNone(parse_mode_command("режим " + "я" * 5000))
        self.assertIsNotNone(parse_mode_command("режим шпаргалка " + "я" * 100))

    def test_mode_values_match_registry(self):
        # Парсер stdlib-only и константы дублирует; расхождение с реестром сделало
        # бы команду записывающей мусор, который резолвер молча заменит дефолтом.
        self.assertIn(mode_command.CARRY, registry.VALID_OPEN_QUESTIONS_MODES)
        self.assertIn(mode_command.LAST_ONLY, registry.VALID_OPEN_QUESTIONS_MODES)

    def test_texts_cover_every_valid_mode(self):
        # Словари текстов ключуются литералами, а индексируются значением из
        # реестра. Появится третий режим — `_OQ_MODE_STATE[mode]` кинет KeyError
        # наружу из роута, прямо в `process_message`.
        for name in ("_OQ_MODE_CONFIRM", "_OQ_MODE_SERIES_NOTICE", "_OQ_MODE_STATE",
                     "_OQ_MODE_ALREADY", "_OQ_MODE_ALREADY_FIXED", "_OQ_MODE_HOWTO"):
            self.assertEqual(set(getattr(ml, name)), set(registry.VALID_OPEN_QUESTIONS_MODES),
                             name)


# ==========================================================================
# 1б. Обычная переписка НЕ перехватывается (пункт 6 плана)
# ==========================================================================
# Формы, которые прошлый парсер принимал за команду. Каждая — из прогона ревью:
# 8 из 10 бытовых фраз рабочей группы перехватывались, и бот отвечал на них
# выкладкой списка всех серий владельца в групповой чат.
PLAIN_CHATTER = (
    "привет, как дела",
    "спасибо!",
    "закрой третью",
    "завтра встречаемся в 11",
    "мы копим вопросы уже месяц",
    "у нас накопление вопросов по этой серии растёт",
    "данные только с прошлой встречи",
    "не копи деньги, вложи в дело",
    "сделал шпаргалку по проекту",
    "нужна копия протокола",
    "у нас в компании накопление долга",
    "вопросы копились с мая",
    "почему ты не копишь вопросы?",
    "эта серия — шпаргалка, а не накопление",
    "по этой серии всё сделано",
    # ↓ якоря узости: слово режима есть, формы нет.
    "нужна шпаргалка",
    "скинь шпаргалку",
    "у нас накопление",
    "сделай шпаргалку по проекту",
    "по этой серии у нас шпаргалка",
    "по этой серии идёт накопление вопросов",
    "эта серия — сплошное накопление",
    # ↓ императивы прошлой версии: чаще всего это обращение людей друг к другу.
    "копи вопросы",
    "не копи",
    "только с прошлой встречи",
    "Ребят, давайте копить вопросы в одном месте, а не в трёх чатах",
    "давайте копить вопросы к следующей встрече",
    "Коллеги, копим вопросы и выносим на директорат",
    "надо копить вопросы, а не терять",
    "прошу копить вопросы в этом чате",
    "не копите вопросы, пишите сразу",
    "будем копить вопросы",
    "давайте вопросы не копить, а закрывать на встрече",
)

# 🔴 Инверсия отрицания: прошлый парсер отдавал на каждой из этих фраз `carry`,
# то есть ВКЛЮЧАЛ накопление там, где просили выключить. Правило теперь: либо
# форма понята точно, либо не понята вовсе — «понял наоборот» невозможно.
NEGATIONS = (
    "Слушай, эту серию давай без накопления",
    "эта серия без накопления",
    "Не надо больше копить вопросы по этой серии",
    "Здесь вопросы копить не надо",
    "не надо тут копить вопросы",
    "давай не будем копить вопросы тут",
    "не надо больше копить",
    "без накопления",
    "хватит копить",
)


class TestPlainChatterNotIntercepted(unittest.TestCase):

    def test_parser_declines_plain_chatter(self):
        for text in PLAIN_CHATTER:
            self.assertIsNone(parse_mode_command(text), text)

    def test_negations_are_never_understood_backwards(self):
        for text in NEGATIONS:
            got = parse_mode_command(text)
            self.assertIsNone(got, f"{text!r} → {got}")

    def test_route_declines_and_never_touches_registry(self):
        # Реестр не парсится на каждом «привет» в каждой группе: детект стоит
        # перед любым чтением, поэтому площадь отказа реестра = сами команды.
        with mock.patch("notary.cli.registry.load_watched") as lw, \
             mock.patch.object(ml, "send_message") as sm:
            for text in PLAIN_CHATTER + NEGATIONS:
                claimed = ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg(text))
                self.assertFalse(claimed, text)
        lw.assert_not_called()
        sm.assert_not_called()

    def test_form_must_match_the_whole_message(self):
        # Главный якорь узости: команда — это ВСЁ сообщение. Слово режима,
        # спрятанное в рассказе, командой не делает ничего.
        for text in ("давай поменяем режим шпаргалка на что-то другое",
                     "эта серия — шпаргалка и ещё пара слов",
                     "серию Координация — шпаргалка, но не сегодня",
                     "в каком режиме эта серия работает",
                     "напомни, режим накопление это что",
                     "эта серия — шпаргалка\nи ещё пара слов"):
            self.assertIsNone(parse_mode_command(text), text)

    def test_set_chat_command_is_not_stolen(self):
        # Соседняя команда владельца «серию X шли сюда» обязана достаться своему
        # роуту: он стоит выше, и красть её нельзя даже при совпадении слова «серию».
        from notary.lib.correction_command import parse_correction_command  # noqa: PLC0415
        for text in ("серию koord шли сюда", "серию координация шли в личку",
                     "эту серию шли в чат -100500"):
            self.assertIsNone(parse_mode_command(text), text)
            self.assertIsNotNone(parse_correction_command(text), text)

    def test_dated_correction_command_is_not_stolen(self):
        # Правка протокола, где мимоходом сказано «серию … шпаргалка», обязана
        # уехать в свой роут. Гейта по дате больше нет (он гасил серии, у которых
        # дата в slug'е) — узость держится тем, что форма матчит сообщение целиком.
        from notary.lib.correction_command import parse_correction_command  # noqa: PLC0415
        for text in ("поправь протокол koord 2026-05-28: убери задачу 3",
                     "koord 2026-05-28: задачу про юани не было",
                     "поправь протокол koord 2026-05-28: серию переименовали, "
                     "теперь это шпаргалка"):
            self.assertIsNone(parse_mode_command(text), text)
            self.assertIsNotNone(parse_correction_command(text), text)

    def test_protocol_command_is_not_stolen(self):
        from notary.lib.protocol_command import parse_protocol_command  # noqa: PLC0415
        for text in ("протокол sales-quality-2026-05-27 2026-05-27",
                     "перегенерируй koord 2026-05-28"):
            self.assertIsNone(parse_mode_command(text), text)
            self.assertIsNotNone(parse_protocol_command(text), text)

    def test_question_is_not_an_order(self):
        self.assertIsNone(parse_mode_command("почему ты не копишь вопросы?"))
        self.assertIsNone(parse_mode_command("эта серия — шпаргалка?"))


# ==========================================================================
# 2. Реестр: сеттер, записи серии, живость записи
# ==========================================================================
class TestSetOpenQuestionsModeForSeries(unittest.TestCase):

    def test_sets_all_records_of_series(self):
        # Читатель берёт ПЕРВОЕ валидное значение среди записей серии. Записав
        # режим в одну запись, получили бы read-after-write, зависящий от порядка
        # записей: старое значение выше молча перебило бы только что заданное.
        w = _watched(_rec("koord", open_questions_mode="carry"),
                     _rec("koord"), _rec("koord", open_questions_mode="carry"))
        n = registry.set_open_questions_mode_for_series("koord", "last-only", w)
        self.assertEqual(n, 3)
        self.assertTrue(all(r["open_questions_mode"] == "last-only" for r in w["watched"]))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_other_series_untouched(self):
        # Т4 плана: серия-накопление не должна меняться от переключения соседней.
        w = _watched(_rec("koord"), _rec("anzhee-direktorat", open_questions_mode="carry"))
        registry.set_open_questions_mode_for_series("koord", "last-only", w)
        self.assertEqual(w["watched"][1]["open_questions_mode"], "carry")
        self.assertEqual(registry.series_open_questions_mode("anzhee-direktorat", w), "carry")

    def test_key_drift_matches_like_reader(self):
        # ISS-7: запись и чтение матчат ключ одинаково, иначе привязка теряется.
        w = _watched(_rec("Anzhee-Direktorat"))
        n = registry.set_open_questions_mode_for_series("  «anzhee-direktorat»  ", "carry", w)
        self.assertEqual(n, 1)
        self.assertEqual(registry.series_open_questions_mode("Anzhee-Direktorat", w), "carry")

    def test_value_written_is_canonical(self):
        w = _watched(_rec("koord"))
        registry.set_open_questions_mode_for_series("koord", "  Carry  ", w)
        self.assertEqual(w["watched"][0]["open_questions_mode"], "carry")

    def test_invalid_mode_raises(self):
        w = _watched(_rec("koord"))
        for bad in ("накопление", "", "last_only", 1, None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                registry.set_open_questions_mode_for_series("koord", bad, w)
        self.assertNotIn("open_questions_mode", w["watched"][0])

    def test_empty_series_key_matches_nothing(self):
        # Нормализованно-пустой ключ совпал бы с записями, чья series тоже пуста, —
        # это матч по пустоте, а не по серии.
        w = _watched(_rec("koord"), {"id": "x", "series": "", "type": "manual"})
        self.assertEqual(registry.set_open_questions_mode_for_series("«»", "carry", w), 0)
        self.assertEqual(registry.set_open_questions_mode_for_series("", "carry", w), 0)
        self.assertEqual(registry.series_records("«»", w), [])
        self.assertNotIn("open_questions_mode", w["watched"][1])

    def test_non_dict_record_does_not_crash(self):
        w = {"watched": ["мусор", _rec("koord")]}
        self.assertEqual(registry.set_open_questions_mode_for_series("koord", "carry", w), 1)
        self.assertEqual(len(registry.series_records("koord", w)), 1)
        self.assertIsNotNone(registry.resolve_series_ref("koord", w))

    def test_live_record_wins_over_disabled_one(self):
        # 🔴 `series_for_chat` выключенные записи ПРОПУСКАЕТ, а резолвер режима
        # их учитывал — и отказная запись прошлой встречи (их заводит ISS-53)
        # определяла режим ЖИВОЙ серии. Правило одно: режим определяют живые записи.
        w = _watched(_rec("koord", enabled=False, ignored=True, open_questions_mode="carry"),
                     _rec("koord", open_questions_mode="last-only"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")
        self.assertFalse(registry.series_record_is_live(w["watched"][0]))
        self.assertTrue(registry.series_record_is_live(w["watched"][1]))

    def test_live_record_without_field_still_beats_dead_one(self):
        # Живая запись без поля = «дефолт», а не «нет мнения»: мёртвая запись не
        # должна включать серии режим за неё.
        w = _watched(_rec("koord", enabled=False, open_questions_mode="carry"),
                     _rec("koord"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_dead_records_are_still_a_fallback(self):
        # Если живых записей у серии не осталось совсем — честнее назвать
        # записанный режим, чем молча выдать дефолт.
        w = _watched(_rec("koord", enabled=False, open_questions_mode="carry"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "carry")

    def test_writer_covers_dead_records_too(self):
        # Пишем во ВСЕ записи серии, включая выключенные: одно значение на серию,
        # расхождений между записями после команды не остаётся.
        w = _watched(_rec("koord", enabled=False), _rec("koord"))
        self.assertEqual(registry.set_open_questions_mode_for_series("koord", "carry", w), 2)
        self.assertTrue(all(r["open_questions_mode"] == "carry" for r in w["watched"]))


# ==========================================================================
# 3. Обе стороны переключения + видимое подтверждение (пункты 3 и 6 плана)
# ==========================================================================
class TestSwitchBothWays(unittest.TestCase):

    def test_switch_to_last_only(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим шпаргалка"))
        self.assertTrue(claimed)
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")
        env.save.assert_called_once()
        self.assertIn("теперь в режиме шпаргалки", _said(env.send))

    def test_switch_to_carry(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="last-only"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим накопление"))
        self.assertTrue(claimed)
        self.assertEqual(registry.series_open_questions_mode("koord", w), "carry")
        env.save.assert_called_once()
        self.assertIn("теперь в режиме накопления", _said(env.send))

    def test_plan_wording_still_switches(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("эта серия — шпаргалка"))
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")
        env.save.assert_called_once()

    def test_confirmation_is_plan_wording(self):
        # Пункт 3 плана — дословно: «Принято: серия <…> теперь в режиме шпаргалки
        # — с ближайшей встречи вопросы копиться не будут».
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим шпаргалка"))
        said = _said(env.send)
        self.assertIn("Принято: серия «Координация» теперь в режиме шпаргалки — "
                      "с ближайшей встречи вопросы копиться не будут", said)

    def test_carry_confirmation_warns_about_clean_slate(self):
        # Решение владельца 7 и РИСК6 плана: при возврате в накопление счёт идёт
        # с чистого листа. Подтверждение обязано сказать это честно, иначе через
        # месяц владельца ждёт сюрприз «а где мои старые вопросы».
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="last-only"))
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим накопление"))
        said = _said(env.send)
        self.assertIn("с чистого листа", said)
        self.assertIn("не поднимаю", said)

    def test_carry_confirmation_on_a_legacy_series_promises_no_clean_slate(self):
        # 🔴 ЦИКЛ5, обратная сторона починки: у серии поля режима НЕТ (боевое
        # состояние до раскладки). Отсечку мы не ставим — значит и обещать
        # «старое не поднимаю» нельзя: владелец пошёл бы искать пропажу того,
        # что на месте. Текст обязан описывать сделанное, а не задуманное.
        w = _watched(_rec("koord", telegram_chat_id=GROUP))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим накопление"))
        said = _said(env.send)
        self.assertIn("теперь в режиме накопления", said)
        self.assertNotIn("с чистого листа", said)
        self.assertIn("сохранил", said)

    def test_dated_slug_series_is_switchable(self):
        # 🔴 Три серии боевого реестра были недостижимы ни одним набором текста:
        # дата в slug'е гасила команду, и бот молчал.
        w = _watched(_rec("sales-quality-2026-05-27", telegram_chat_id=OTHER_GROUP))
        with _route_env(w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER,
                _msg("режим накопление sales-quality-2026-05-27", chat=OWNER))
        self.assertTrue(claimed)
        self.assertEqual(
            registry.series_open_questions_mode("sales-quality-2026-05-27", w), "carry")
        env.save.assert_called_once()

    def test_human_display_name_resolves(self):
        # Бот сам печатает серию человеческим именем — оно обязано приниматься
        # обратно. Прошлая версия вычитала из имени «и»/«на» и не резолвила.
        w = _watched(_rec("mpervyi-pn-koord-finplan", telegram_chat_id=OTHER_GROUP))
        dm = {"mpervyi-pn-koord-finplan": "Закупки и финпланирование (МПервый)"}
        with _route_env(w, display_map=dm) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER,
                _msg("режим накопление Закупки и финпланирование (МПервый)", chat=OWNER))
        self.assertTrue(claimed)
        self.assertEqual(
            registry.series_open_questions_mode("mpervyi-pn-koord-finplan", w), "carry")


# ==========================================================================
# 3б. Идемпотентность и самолечение реестра
# ==========================================================================
class TestIdempotencyAndSelfHealing(unittest.TestCase):

    def test_repeat_of_same_mode_writes_nothing(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим накопление"))
        self.assertTrue(claimed)
        env.save.assert_not_called()
        env.release.assert_called()  # лок отпущен, иначе листенер встанет
        self.assertIn("и так в режиме накопления", _said(env.send))
        self.assertEqual(env.send.call_count, 1)  # чат серии не шумим

    def test_first_explicit_choice_is_written_even_at_default(self):
        # 🔴 Дефолт — `last-only`, поэтому первая команда «режим шпаргалка» была
        # немым no-op: в реестре не появлялось ничего (на бою поле не проставлено
        # ни у одной из 16 серий), и «выбрано» было не отличить от «никто не
        # выбирал» — смена дефолта однажды молча перевернула бы их все.
        w = _watched(_rec("koord", telegram_chat_id=GROUP))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим шпаргалка"))
        self.assertTrue(claimed)
        env.save.assert_called_once()
        self.assertEqual(w["watched"][0]["open_questions_mode"], "last-only")
        said = _said(env.send)
        self.assertIn("и так в режиме шпаргалки", said)
        self.assertIn("записал это явно", said)
        self.assertEqual(env.send.call_count, 1)  # формат не менялся — людям не пишем

    def test_diverged_records_are_healed(self):
        # 🔴 Короткое замыкание «и так в этом режиме» стояло ДО записи, поэтому
        # расхождение между записями серии не лечилось никогда: владельцу
        # называли режим одной записи, а поведение определяла другая.
        w = _watched(_rec("koord", telegram_chat_id=GROUP, enabled=False, ignored=True,
                          open_questions_mode="carry"),
                     _rec("koord", telegram_chat_id=GROUP, open_questions_mode="last-only"))
        with _route_env(w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим шпаргалка"))
        self.assertTrue(claimed)
        env.save.assert_called_once()
        self.assertEqual([r["open_questions_mode"] for r in w["watched"]],
                         ["last-only", "last-only"])
        # 🔴 ШОВ (сквозная проверка, S4): «какой режим сейчас» обязано быть ОДНИМ
        # ответом на обе ветки команды и совпадать с ФАКТИЧЕСКИМ поведением бота.
        # Мёртвая запись с явным `carry` включает fail-safe боевого резолвера
        # (`_explicit_carry_signal`): дайджест и протокол по этой серии реально
        # шли в накоплении. Значит команда «режим шпаргалка» здесь — НАСТОЯЩЕЕ
        # переключение, и говорить «и так в шпаргалке, формат не меняется»
        # (прежний ответ по букве реестра) означало соврать: формат менялся, а
        # людей в чате никто не предупреждал.
        self.assertIn("теперь в режиме шпаргалки", _said(env.send))

    def test_disabled_record_no_longer_hijacks_live_series(self):
        # Тот же реестр, но команда — в ПРОТИВОПОЛОЖНУЮ сторону: живая запись
        # last-only, мёртвая carry. Реестр после команды лечится (во всех
        # записях `carry`), а формат встреч при этом действительно НЕ меняется:
        # боевой резолвер и до команды считал серию накоплением (fail-safe по
        # явному сигналу мёртвой записи). Ответ обязан говорить именно это —
        # и он тот же самый, что даст ветка «в каком режиме серия».
        w = _watched(_rec("koord", telegram_chat_id=GROUP, enabled=False, ignored=True,
                          open_questions_mode="carry"),
                     _rec("koord", telegram_chat_id=GROUP, open_questions_mode="last-only"))
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим накопление"))
        env.save.assert_called_once()
        self.assertEqual(registry.series_open_questions_mode("koord", w), "carry")
        self.assertEqual([r["open_questions_mode"] for r in w["watched"]],
                         ["carry", "carry"])
        self.assertIn("и так в режиме накопления", _said(env.send))


# ==========================================================================
# 3в. ШОВ Ф1↔Ф2: обе ветки команды отвечают ОДНО и то же (сквозная проверка, S4)
# ==========================================================================
class TestBothBranchesAgreeOnTheCurrentMode(unittest.TestCase):
    """🔴 Опечатка в реестре ломала отчёт команды.

    `open_questions_mode: карри` (ровно то, что рождает ручная раскладка режимов
    при выкатке) резолвер реестра читает как «поле не задано» и отдаёт дефолт
    `last-only`, а боевой путь (дайджест, протокол, сверщик) в этом случае
    ОСТАЁТСЯ в накоплении: потерять хвост необратимо, показать лишний — нет.
    Ветка «вопрос» этот fail-safe спрашивала, ветка «команда» — нет. Итог: на
    «режим шпаргалка» бот рапортовал «и так в шпаргалке, формат встреч не
    меняется, участников не беспокоил», реально переводя серию из накопления в
    справку и никого не предупредив.
    """

    TYPO = "карри"

    def test_query_and_command_call_the_same_resolver(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode=self.TYPO))
        self.assertEqual(ml._oq_effective_mode("koord", w), "carry")
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_typo_makes_the_command_a_real_switch(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode=self.TYPO))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим шпаргалка"))
        said = _said(env.send)
        self.assertIn("теперь в режиме шпаргалки", said)
        self.assertNotIn("формат встреч не меняется", said)
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_participants_are_warned_when_the_format_really_changes(self):
        # Команда из лички: люди в чате серии обязаны узнать о смене формата.
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode=self.TYPO))
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER, _msg("режим шпаргалка koord", chat=OWNER))
        chats = [c.args[1] for c in env.send.call_args_list]
        self.assertIn(GROUP, chats, "чат серии не предупредили: %s" % chats)

    def test_both_branches_say_the_same_words_about_the_typo_registry(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode=self.TYPO))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим"))
        asked = _said(env.send)
        w2 = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode=self.TYPO))
        with _route_env(w2, display_map={"koord": "Координация"}) as env2:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим накопление"))
        commanded = _said(env2.send)
        self.assertIn("в режиме накопления", asked)
        self.assertIn("и так в режиме накопления", commanded)
        # И реестр после команды вылечен: опечатки в нём больше нет.
        self.assertEqual([r["open_questions_mode"] for r in w2["watched"]], ["carry"])


# ==========================================================================
# 4. Owner-гейт (пункт 2 плана) + место, где можно назвать имена серий
# ==========================================================================
class TestOwnerGate(unittest.TestCase):

    def test_non_owner_in_group_is_refused(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w) as env:
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                claimed = ml.maybe_route_to_mode_command(
                    "tok", GROUP, OWNER, _msg("режим шпаргалка", from_id=STRANGER))
        self.assertFalse(claimed)  # сообщение уходит обычным роутам ниже
        env.save.assert_not_called()
        env.load.assert_not_called()
        env.send.assert_not_called()
        self.assertEqual(registry.series_open_questions_mode("koord", w), "carry")
        self.assertTrue(any("не владелец" in line for line in cm.output), cm.output)

    def test_owner_in_group_applies(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим шпаргалка", from_id=OWNER))
        self.assertTrue(claimed)
        env.save.assert_called_once()

    def test_owner_dm_applies_with_explicit_series(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER,
                _msg("режим шпаргалка Координация", chat=OWNER, from_id=OWNER))
        self.assertTrue(claimed)
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_stranger_dm_is_refused(self):
        # Чужая личка: chat.id == user.id постороннего, владельцем он не является.
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", STRANGER, OWNER,
                _msg("режим шпаргалка", chat=STRANGER, from_id=STRANGER))
        self.assertFalse(claimed)
        env.save.assert_not_called()

    def test_query_about_own_chat_is_not_owner_gated(self):
        # Вопрос ничего не меняет, а люди в чате — ровно те, кого режим касается.
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим", from_id=STRANGER))
        self.assertTrue(claimed)
        env.save.assert_not_called()
        said = _said(env.send)
        self.assertIn("в режиме накопления", said)
        # …но команде переключения участника не учим: у него она не сработает.
        self.assertNotIn("Переключить", said)

    def test_stranger_cannot_probe_other_series_by_name(self):
        # Посторонний в личке бота спрашивает про ЧУЖУЮ серию по имени: иначе он
        # вытягивал бы режимы, а через «не нашёл серию» — список внутренних имён
        # всех встреч владельца.
        w = _watched(_rec("anzhee-direktorat", telegram_chat_id=GROUP,
                          open_questions_mode="carry"),
                     _rec("marketplaces-tatiana", telegram_chat_id=OTHER_GROUP))
        with _route_env(w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", STRANGER, OWNER,
                _msg("режим anzhee-direktorat", chat=STRANGER, from_id=STRANGER))
        self.assertFalse(claimed)
        env.send.assert_not_called()


class TestKnownSeriesListNeverLeaksToGroup(unittest.TestCase):
    """🔴 Список внутренних имён серий — только в личке владельца.

    Гейт `list_known=is_owner` спрашивал «это владелец?» и не спрашивал «а ГДЕ он
    это набрал?». Владелец в любой группе с нерезолвящейся ссылкой вываливал туда
    16 внутренних имён — часть с фамилиями людей и названиями компаний.
    """

    def setUp(self):
        self.w = _watched(_rec("koord", telegram_chat_id=GROUP),
                          _rec("marketplaces-tatiana", telegram_chat_id=OTHER_GROUP),
                          _rec("1-na-1-ivan-petrov"))

    def test_owner_in_group_gets_no_list_and_route_falls_through(self):
        # И вторая половина требования: роут отдаёт False, а не съедает сообщение
        # видимым отказом — не уверены, что это команда → пропускаем дальше.
        for text, writes in (("режим шпаргалка выставка", True),
                             ("режим выставка", False),
                             ("серию выставка — шпаргалка", True)):
            with _route_env(self.w) as env:
                claimed = ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg(text))
            self.assertFalse(claimed, text)
            env.send.assert_not_called()
            env.save.assert_not_called()
            if writes:  # у set-ветки взят flock — он обязан быть отпущен и здесь
                env.release.assert_called()

    def test_participant_in_group_gets_no_list(self):
        with _route_env(self.w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим выставка", from_id=STRANGER))
        self.assertFalse(claimed)
        env.send.assert_not_called()

    def test_owner_dm_does_get_the_list(self):
        # В личке владельца список — то, ради чего он и написан: он там набирает
        # имена серий руками.
        with _route_env(self.w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER, _msg("режим шпаргалка выставка", chat=OWNER))
        self.assertTrue(claimed)
        said = _said(env.send)
        self.assertIn("Не нашёл серию «выставка»", said)
        self.assertIn("marketplaces-tatiana", said)
        self.assertIn("1-na-1-ivan-petrov", said)

    def test_query_path_leaks_nothing_to_group_either(self):
        with _route_env(self.w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим маркетплейсы"))
        self.assertFalse(claimed)
        env.send.assert_not_called()


# ==========================================================================
# 5. Строка в чат серии о смене формата (пункт 5 плана)
# ==========================================================================
class TestSeriesChatNotice(unittest.TestCase):

    def test_notice_goes_to_series_chat_when_command_given_in_dm(self):
        # Команда из лички — люди в группе иначе узнают о смене формата только
        # по исчезнувшим висякам.
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER,
                _msg("режим шпаргалка Координация", chat=OWNER, from_id=OWNER))
        targets = [c.args[1] for c in env.send.call_args_list]
        self.assertEqual(targets, [OWNER, GROUP])
        notice = env.send.call_args_list[1].args[2]
        self.assertIn("Формат к следующей встрече меняется", notice)
        self.assertIn("больше не копятся", notice)

    def test_single_message_when_command_given_in_series_chat(self):
        # Подтверждение уже стоит у людей перед глазами и говорит то же самое —
        # второе сообщение подряд в тот же чат было бы шумом.
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим шпаргалка"))
        self.assertEqual(env.send.call_count, 1)
        self.assertEqual(env.send.call_args_list[0].args[1], GROUP)
        self.assertIn("вопросы копиться не будут", env.send.call_args_list[0].args[2])

    def test_notice_for_carry_direction(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="last-only"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER,
                _msg("режим накопление Координация", chat=OWNER, from_id=OWNER))
        notice = env.send.call_args_list[1].args[2]
        self.assertIn("снова копятся", notice)
        self.assertIn("Счёт начинается заново", notice)

    def test_unbound_series_says_nobody_was_warned(self):
        # Честность вместо тишины: предупредить участников физически некуда.
        w = _watched(_rec("koord", open_questions_mode="carry"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER,
                _msg("режим шпаргалка Координация", chat=OWNER, from_id=OWNER))
        self.assertEqual(env.send.call_count, 1)
        self.assertIn("не привязан", _said(env.send))

    def test_disabled_series_gets_no_notice(self):
        # Встреч по выключенной серии бот не ведёт — обещать людям в её чате
        # новый формат «к следующей встрече» нечестно: следующей встречи нет.
        w = _watched(_rec("koord", telegram_chat_id=GROUP, enabled=False,
                          open_questions_mode="carry"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER,
                _msg("режим шпаргалка Координация", chat=OWNER, from_id=OWNER))
        self.assertEqual(env.send.call_count, 1)
        said = _said(env.send)
        self.assertIn("Серия выключена", said)
        self.assertIn("теперь в режиме шпаргалки", said)  # сама запись сделана

    def test_mode_is_saved_before_anything_is_sent(self):
        # Telegram лежит (`send_message` возвращает None, как в бою) — режим уже
        # записан и не теряется; роут отвечает True и никого не роняет.
        w = _watched(_rec("koord", telegram_chat_id=OTHER_GROUP, open_questions_mode="carry"))
        order = []
        with _route_env(w) as env:
            env.send.side_effect = lambda *a, **k: order.append("send") or None
            env.save.side_effect = lambda *a, **k: order.append("save")
            claimed = ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER, _msg("режим шпаргалка koord", chat=OWNER))
        self.assertTrue(claimed)
        self.assertEqual(order[0], "save")
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")


# ==========================================================================
# 6. Вопрос о режиме (пункт 4 плана)
# ==========================================================================
class TestModeQuery(unittest.TestCase):

    def test_reports_carry(self):
        w = _watched(_rec("anzhee-direktorat", telegram_chat_id=GROUP,
                          open_questions_mode="carry"))
        with _route_env(w, display_map={"anzhee-direktorat": "Директорат"}) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("в каком режиме эта серия?"))
        self.assertTrue(claimed)
        said = _said(env.send)
        self.assertIn("Директорат", said)
        self.assertIn("в режиме накопления", said)
        env.save.assert_not_called()

    def test_reports_last_only_and_default(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP))  # поля нет → дефолт
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим"))
        self.assertIn("в режиме шпаргалки", _said(env.send))

    def test_query_by_explicit_name_from_dm(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w, display_map={"koord": "Координация"}) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER, _msg("режим Координация", chat=OWNER))
        self.assertTrue(claimed)
        self.assertIn("в режиме накопления", _said(env.send))

    def test_query_does_not_lock_the_registry(self):
        # Держать эксклюзивный flock ради ответа «сейчас шпаргалка» незачем:
        # реестр чтением не блокируем.
        w = _watched(_rec("koord", telegram_chat_id=GROUP))
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим"))
        for call in env.load.call_args_list:
            self.assertNotEqual(call.kwargs.get("lock"), True)
            self.assertNotIn(True, call.args)

    def test_query_tells_owner_how_to_switch(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP))
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим"))
        self.assertIn("Переключить: «режим накопление»", _said(env.send))


# ==========================================================================
# 7. Резолв серии: реплай → чат → явное имя → переспрос
# ==========================================================================
class TestSeriesResolution(unittest.TestCase):

    def test_explicit_name_wins_over_chat_binding(self):
        # Владелец в чате одной серии переключает ДРУГУЮ по имени — угадывать
        # за него нельзя.
        w = _watched(_rec("koord", telegram_chat_id=GROUP),
                     _rec("vystavka", telegram_chat_id=OTHER_GROUP))
        with _route_env(w) as env:
            ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим накопление vystavka"))
        self.assertEqual(registry.series_open_questions_mode("vystavka", w), "carry")
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")

    def test_ambiguous_chat_asks_instead_of_guessing(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP),
                     _rec("vystavka", telegram_chat_id=GROUP))
        with _route_env(w, display_map={"koord": "Координация",
                                        "vystavka": "Выставка"}) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим шпаргалка"))
        self.assertTrue(claimed)
        env.save.assert_not_called()
        said = _said(env.send)
        self.assertIn("несколько серий", said)
        self.assertIn("Координация", said)
        self.assertIn("Выставка", said)

    def test_dm_without_series_asks(self):
        # Личка владельца ни к какой серии не привязана — вопрос законный.
        # Отвечаем видимо (команда однозначна), но имён серий не называем.
        w = _watched(_rec("koord", telegram_chat_id=GROUP))
        with _route_env(w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", OWNER, OWNER, _msg("режим шпаргалка", chat=OWNER))
        self.assertTrue(claimed)
        env.save.assert_not_called()
        said = _said(env.send)
        self.assertIn("Не понял, о какой серии речь", said)
        self.assertIn("режим шпаргалка <имя серии>", said)
        self.assertNotIn("koord", said)

    def test_reply_context_gives_series(self):
        w = _watched(_rec("koord", telegram_chat_id=OTHER_GROUP, open_questions_mode="carry"))
        with _route_env(w) as env, \
             mock.patch.object(ml, "_reply_context_series", return_value="koord"), \
             mock.patch.object(ml, "_chat_context_series") as ccs:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим шпаргалка", reply=True))
        self.assertTrue(claimed)
        ccs.assert_not_called()
        self.assertEqual(registry.series_open_questions_mode("koord", w), "last-only")


# ==========================================================================
# 8. Битый реестр не валит листенер; flock не утекает
# ==========================================================================
class TestBrokenRegistry(unittest.TestCase):

    def test_systemexit_on_locked_load_is_survived(self):
        # 🔴 `registry._read_yaml` кидает SystemExit (потомок BaseException):
        # сквозь `except Exception` он пролетал насквозь и гасил процесс ДО
        # save_offset → Telegram отдавал тот же апдейт → петля рестартов бота.
        with _route_env(None, load_side_effect=SystemExit("ожидался словарь")) as env:
            with self.assertLogs("meetings-listener", level="WARNING"):
                claimed = ml.maybe_route_to_mode_command(
                    "tok", OWNER, OWNER, _msg("режим шпаргалка koord", chat=OWNER))
        self.assertTrue(claimed)  # видимая реакция вместо падения
        env.release.assert_called()  # лок захвачен ДО разбора yaml — отпускаем явно
        said = _said(env.send)
        self.assertIn("Не смог прочитать реестр серий", said)
        # Диагноз честный: прежний «Реестр серий занят» звал ретраить, когда на
        # деле файл был сломан руками.
        self.assertIn("watched.yaml", said)

    def test_systemexit_in_chat_context_is_survived(self):
        with _route_env(None, load_side_effect=SystemExit("ожидался словарь")) as env:
            with self.assertLogs("meetings-listener", level="WARNING") as cm:
                claimed = ml.maybe_route_to_mode_command(
                    "tok", GROUP, OWNER, _msg("режим шпаргалка"))
        self.assertTrue(claimed)
        self.assertTrue(any("SystemExit" in line for line in cm.output), cm.output)

    def test_real_broken_watched_yaml(self):
        # Тот же сценарий на НАСТОЯЩЕМ файле: снесли ключ `watched:` руками.
        try:
            import yaml  # noqa: F401,PLC0415
        except ImportError:  # pragma: no cover
            self.skipTest("pyyaml недоступен")
        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory(prefix="broken-registry-") as td:
            broken = Path(td) / "watched.yaml"
            broken.write_text("- id: koord-evt\n  series: koord\n", encoding="utf-8")
            with mock.patch.object(registry, "WATCHED_FILE", broken), \
                 mock.patch.object(ml, "_series_display_map", return_value={}), \
                 mock.patch.object(ml, "send_message") as sm:
                with self.assertRaises(SystemExit):
                    registry.load_watched()
                with self.assertLogs("meetings-listener", level="WARNING"):
                    claimed = ml.maybe_route_to_mode_command(
                        "tok", OWNER, OWNER, _msg("режим шпаргалка koord", chat=OWNER))
        self.assertTrue(claimed)
        sm.assert_called_once()

    def test_non_dict_record_does_not_break_the_query_path(self):
        # 🔴 `resolve_series_ref` падал `AttributeError` на записи-не-словаре, а в
        # query-ветке этот вызов стоял ВНЕ try: исключение улетало из роута,
        # человек не получал ответа, и сообщение не доходило до роутов ниже.
        # Достижимо любым участником группы — вопрос owner-гейта не имеет.
        w = {"watched": ["строка-вместо-записи",
                         _rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry")]}
        with _route_env(w) as env:
            claimed = ml.maybe_route_to_mode_command(
                "tok", GROUP, OWNER, _msg("режим", from_id=STRANGER))
        self.assertTrue(claimed)
        self.assertIn("в режиме накопления", _said(env.send))

    def test_query_answers_even_if_registry_parsing_explodes(self):
        # Симметрично set-ветке: что бы ни случилось на разборе — человек
        # получает ответ, а не тишину и оборванную обработку сообщения.
        w = _watched(_rec("koord", telegram_chat_id=GROUP))
        with _route_env(w) as env, \
             mock.patch.object(registry, "series_open_questions_mode",
                               side_effect=RuntimeError("бум")):
            with self.assertLogs("meetings-listener", level="WARNING"):
                claimed = ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим"))
        self.assertTrue(claimed)
        self.assertIn("Не смог прочитать реестр серий", _said(env.send))

    def test_save_failure_is_visible_and_releases_lock(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w, save_side_effect=OSError("диск полон")) as env:
            with self.assertLogs("meetings-listener", level="WARNING"):
                claimed = ml.maybe_route_to_mode_command(
                    "tok", GROUP, OWNER, _msg("режим шпаргалка"))
        self.assertTrue(claimed)
        env.release.assert_called()
        self.assertIn("Не смог сохранить режим", _said(env.send))

    def test_import_failure_of_parser_is_soft_but_loud(self):
        # Уровень листенера — INFO: на `logger.debug` пропажа нового модуля
        # (самый вероятный сбой деплоя) не оставляла в журнале ничего, и фича
        # была молча мертва.
        with mock.patch.dict(sys.modules, {"notary.lib.mode_command": None}), \
             mock.patch.object(ml, "send_message") as sm:
            with self.assertLogs("meetings-listener", level="WARNING") as cm:
                claimed = ml.maybe_route_to_mode_command(
                    "tok", GROUP, OWNER, _msg("режим шпаргалка"))
        self.assertFalse(claimed)
        sm.assert_not_called()
        self.assertTrue(any("mode_command" in line for line in cm.output), cm.output)


# ==========================================================================
# 9. Журнал: видно переключение, не видно текстов
# ==========================================================================
class TestLogging(unittest.TestCase):

    def test_switch_is_logged_with_old_and_new_mode(self):
        # `logger.debug` в этом файле не виден (уровень INFO) — успешное
        # переключение обязано быть на INFO, иначе разбирать боевой инцидент нечем.
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w):
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим шпаргалка"))
        joined = "\n".join(cm.output)
        self.assertIn("koord", joined)
        self.assertIn("carry", joined)
        self.assertIn("last-only", joined)
        self.assertIn(str(OWNER), joined)  # кто переключил — в аудит-строке

    def test_logs_carry_no_message_text(self):
        # Опасная тройка: тексты сообщений/задач/встреч в журнал НЕ попадают.
        # Здесь хвост команды уезжает в «ссылку на серию» и печатается ЧЕЛОВЕКУ
        # («не нашёл серию …») — в журнал он попасть всё равно не должен.
        secret = "юани закупили по 11.4, договор с Петровым"
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w) as env:
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                ml.maybe_route_to_mode_command(
                    "tok", OWNER, OWNER, _msg(f"режим шпаргалка {secret}", chat=OWNER))
        self.assertIn("юани", _said(env.send))  # человеку — видно
        joined = "\n".join(cm.output)
        self.assertNotIn("юани", joined)
        self.assertNotIn("Петров", joined)

    def test_clean_switch_log_is_metadata_only(self):
        w = _watched(_rec("koord", telegram_chat_id=GROUP, open_questions_mode="carry"))
        with _route_env(w):
            with self.assertLogs("meetings-listener", level="INFO") as cm:
                ml.maybe_route_to_mode_command("tok", GROUP, OWNER, _msg("режим шпаргалка"))
        joined = "\n".join(cm.output)
        for word in ("шпаргалка", "режим шпаргалка"):
            self.assertNotIn(word, joined)


# ==========================================================================
# 10. Место роута в конвейере: рядом со сменой чата, чужого не крадёт
# ==========================================================================
class TestRouteOrder(unittest.TestCase):

    def test_mode_route_runs_after_set_chat_and_before_ack(self):
        with mock.patch.object(ml, "maybe_route_to_set_chat_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_mode_command", return_value=True) as mc, \
             mock.patch.object(ml, "maybe_route_to_ack_reply") as ack, \
             mock.patch.object(ml, "maybe_route_to_send_artifact") as sa:
            ml.process_message("tok", OWNER, _msg("режим шпаргалка"))
        mc.assert_called_once()
        ack.assert_not_called()
        sa.assert_not_called()

    def test_set_chat_command_still_wins(self):
        with mock.patch.object(ml, "maybe_route_to_set_chat_command", return_value=True), \
             mock.patch.object(ml, "maybe_route_to_mode_command") as mc:
            ml.process_message("tok", OWNER, _msg("серию koord шли сюда"))
        mc.assert_not_called()

    def test_chatter_falls_through_to_lower_routes(self):
        with mock.patch.object(ml, "maybe_route_to_set_chat_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_ack_reply", return_value=True) as ack, \
             mock.patch("notary.cli.registry.load_watched") as lw:
            ml.process_message("tok", OWNER, _msg("привет, как дела"))
        ack.assert_called_once()
        lw.assert_not_called()

    def test_unrecognised_series_in_group_reaches_lower_routes(self):
        # Сквозная проверка обещания «не уверены — пропускаем дальше»: роут
        # прочитал реестр, серию не опознал и всё равно уступил конвейеру.
        w = _watched(_rec("koord", telegram_chat_id=OTHER_GROUP))
        with mock.patch.object(ml, "maybe_route_to_set_chat_command", return_value=False), \
             mock.patch.object(ml, "maybe_route_to_ack_reply", return_value=True) as ack, \
             _route_env(w) as env:
            ml.process_message("tok", OWNER, _msg("режим выставка", chat=GROUP))
        ack.assert_called_once()
        env.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
