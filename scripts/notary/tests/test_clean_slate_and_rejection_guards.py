# -*- coding: utf-8 -*-
"""Сторожа на обещания, которые проверка мутациями застала НЕЗАЩИЩЁННЫМИ.

Прогон доводки внёс девять поломок в починенный код и посмотрел, какие из них
заметит гейт зоны. Заметил две. Семь починок можно было откатить, и гейт остался бы
зелёным — то есть следующий заход снял бы их, ничего не узнав. Этот файл закрывает
четыре из тех семи; остальные три живут в `test_iss51_send_artifact_ai.py`.

Что здесь охраняется:
  • «да» отменяет прошлое «нет» ПОЛНОСТЬЮ — включая заглушку отказа другого рода
    (иначе владельцу говорится «записал серию», а бот на встречу не приезжает);
  • обещание «счёт с чистого листа» держится и на второй корзине дайджеста
    («просто напоминаю»), и при подсчёте происхождения по полной истории серии,
    а не по окну памяти;
  • отсечка не может быть невозможной или будущей датой — такая срезала бы хвост
    серии на всех поверхностях навсегда, оставив одну строку в журнале.

Запуск:
    cd scripts/notary/tests && python3 test_clean_slate_and_rejection_guards.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# `croniter` — необязательная зависимость (нужна только развороту cron у записей
# рода `manual`). Заглушка держит модуль импортируемым на голом интерпретаторе.
sys.modules.setdefault("croniter", types.SimpleNamespace(croniter=object))

_TMP_STATE = tempfile.mkdtemp()
for _v in ("MEETING_NOTARY_REGISTRY_DIR", "MEETING_NOTARY_LOG_DIR",
           "MEETING_NOTARY_STATE_DIR"):
    os.environ.setdefault(_v, _TMP_STATE)

from notary.cli import registry as reg  # noqa: E402
from notary.lib import morning_digest as md  # noqa: E402
from notary.lib import series_memory as sm  # noqa: E402
import notary.meetings_apply_reply as mar  # noqa: E402
import notary.scheduler as sched  # noqa: E402

SERIES = "koordinatsiya"


def _watched(**fields):
    rec = {"id": f"{SERIES}-evt", "series": SERIES, "type": "manual",
           "cron": "0 11 * * 2", "tz": "Europe/Moscow"}
    rec.update(fields)
    return {"watched": [rec]}


def _with_carry_since(since):
    return mock.patch.object(
        sm, "load_watched_for_modes",
        return_value=_watched(open_questions_mode="carry",
                              open_questions_carry_since=since))


class _Series:
    """Каталог серии с выжимками на диске."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp()) / SERIES
        self.dir.mkdir(parents=True, exist_ok=True)

    def meeting(self, date, *, tail=(), born=(), minor=()):
        open_tasks = [{"text": t} for t in tail]
        open_tasks += [{"text": t, "minor": True} for t in minor]
        sm.save_digest(self.dir, date, {
            "schema": 1, "date": date, "series": SERIES,
            "participants": ["Илья Рыбалка"], "themes": [], "key_points": [],
            "open_tasks": open_tasks,
            sm.OPEN_TASKS_BORN_HERE_KEY: list(born),
        })

    def digests(self):
        return sm.list_series_digests(self.dir)


# ==========================================================================
# 1. «Да» отменяет «нет» полностью — даже если заглушка отказа другого рода
# ==========================================================================
class TestYesCancelsNoAcrossKinds(unittest.TestCase):
    """Боевой случай: одна встреча приезжает в вечернем блоке ДВУМЯ номерами (она
    видна в личном календаре и в календаре компании). Ответ «первую не надо, вторую
    пиши серией» — обычная реплика.

    Заглушка отказа всегда одноразовая, а согласие «да, серией» искало записи только
    своего рода. Метка отказа оставалась жить, планировщик снимал ровно этот старт —
    и владельцу было сказано «записал серию», а бот на встречу не приезжал. Узнать об
    этом можно было только из строки в журнале, где вдобавок написана неправда («саму
    серию веду дальше»)."""

    URL = "https://telemost.yandex.ru/j/1"
    ICAL = "koord@google.com"

    def _snapshot(self, n, calendar_id, event_id, recurring_id, start):
        return {"n": n, "calendar_id": calendar_id, "event_id": event_id,
                "recurring_event_id": recurring_id, "title": "Координация",
                "start_at": start.isoformat(), "url_in_event": self.URL,
                "has_url_in_event": True, "ical_uid": self.ICAL}

    def _reject_then_accept_series(self):
        start = (datetime.now(timezone.utc) + timedelta(days=5)).replace(
            minute=0, second=0, microsecond=0)
        data = {"watched": []}
        with mock.patch.object(mar, "load_watched", lambda *a, **k: data), \
             mock.patch.object(mar, "save_watched", lambda d: None), \
             mock.patch.object(mar, "load_rooms", lambda *a, **k: {"rooms": []}), \
             mock.patch.object(mar, "release_watched_lock", lambda: None):
            res = mar.apply_decisions(
                [{"n": 1, "action": "reject"}, {"n": 2, "action": "accept_series"}],
                [self._snapshot(1, "primary", "evt-A", "rr-A", start),
                 self._snapshot(2, "company@x", "evt-B", "rr-B", start)])
        queue = [{"meeting_id": "series-koord", "start_at": start.isoformat(),
                  "url": self.URL, "ical_uid": self.ICAL,
                  "calendar_id": "company@x", "event_id": "evt-B"}]
        return res, data["watched"], queue

    def test_the_bot_actually_comes_to_the_meeting_it_agreed_to(self):
        res, watched, queue = self._reject_then_accept_series()
        kept = sched.drop_rejected_occurrences(queue, watched)
        self.assertEqual(len(kept), 1,
                         "планировщик снял старт, на который владелец только что согласился")

    def test_the_owner_is_told_the_truth(self):
        # Страховка от зелёного вхолостую: тест выше что-то значит, только если бот
        # вообще отчитался о согласии.
        res, _watched, _queue = self._reject_then_accept_series()
        joined = " ".join(res["summary_lines"])
        self.assertIn("серию", joined.lower())

    def test_no_rejection_marks_survive_the_agreement(self):
        _res, watched, _queue = self._reject_then_accept_series()
        still = [w.get("id") for w in watched
                 if w.get(mar.REJECTION_OWNER_FIELD) or w.get("rejected_start_at")]
        self.assertEqual(still, [], f"метки отказа пережили согласие: {still}")


# ==========================================================================
# 2. «Чистый лист» — на ОБЕИХ корзинах показа
# ==========================================================================
class TestCleanSlateCoversMinorBucket(unittest.TestCase):
    """Мелочь «просто напоминаю» отсечки не знала вовсе: у неё не было ни каталога
    серии, ни реестра. Сразу после возврата серии в накопление первый же дайджест
    уходил в чат с до-переключательным пунктом — под текстом «старые вопросы не
    поднимаю», который бот отправил туда же. Причём дайджеста не было бы вовсе:
    основной список пуст, и отправляла его ровно эта мелочь."""

    def setUp(self):
        self.fx = _Series()
        self.before = "2026-07-01"
        self.since = "2026-07-15"
        self.fx.meeting(self.before, minor=("Илья: скинуть ссылку",))

    def test_pre_switch_reminder_is_not_raised(self):
        with _with_carry_since(self.since):
            got = sm._resolve_minor_open_tasks_raw(
                self.fx.digests(), series_dir=self.fx.dir)
        self.assertEqual(got, [], "до-переключательная мелочь пробилась через отсечку")

    def test_without_a_cutoff_the_reminder_still_shows(self):
        # Страховка от зелёного вхолостую: без отсечки мелочь обязана быть на месте,
        # иначе тест выше проходил бы просто потому, что мелочь потерялась.
        with mock.patch.object(sm, "load_watched_for_modes",
                               return_value=_watched(open_questions_mode="carry")):
            got = sm._resolve_minor_open_tasks_raw(
                self.fx.digests(), series_dir=self.fx.dir)
        self.assertEqual(got, ["Илья: скинуть ссылку"])

    def test_a_reminder_born_after_the_cutoff_shows(self):
        self.fx.meeting("2026-07-22", minor=("Илья: прислать смету",))
        with _with_carry_since(self.since):
            got = sm._resolve_minor_open_tasks_raw(
                self.fx.digests(), series_dir=self.fx.dir)
        self.assertEqual(got, ["Илья: прислать смету"])


class TestCleanSlateUsesFullHistory(unittest.TestCase):
    """Полоса происхождения считалась по ТОМУ списку выжимок, который передали. У
    дайджеста и сверщика это вся история, у раздела протокола — окно памяти (по
    умолчанию три встречи). Стоило окну целиком уехать за отсечку, происхождение
    старого пункта «омолаживалось» до края окна, и один и тот же ком дайджест считал
    снятым, а протокол печатал. Состояние самоподдерживающееся: попав в него, серия
    из него не выходит, а на диске всё выглядит нормально."""

    OLD = "Илья: расчёт по марже"

    def setUp(self):
        self.fx = _Series()
        # Пункт рождён ДО отсечки и с тех пор переезжает из встречи во встречу.
        self.fx.meeting("2026-06-03", tail=(self.OLD,), born=(self.OLD,))
        for d in ("2026-06-10", "2026-06-17", "2026-06-24", "2026-07-01"):
            self.fx.meeting(d, tail=(self.OLD,))
        self.since = "2026-06-10"

    def test_window_of_three_still_drops_the_pre_switch_item(self):
        window = self.fx.digests()[-3:]      # ровно то, что видит раздел протокола
        self.assertNotIn("2026-06-03", [d["date"] for d in window])
        with _with_carry_since(self.since):
            got = sm._resolve_open_tasks_raw(window, series_dir=self.fx.dir)
        self.assertEqual(got, [], "окно памяти «омолодило» до-переключательный пункт")

    def test_full_history_and_window_agree(self):
        full = self.fx.digests()
        with _with_carry_since(self.since):
            by_full = sm._resolve_open_tasks_raw(full, series_dir=self.fx.dir)
            by_window = sm._resolve_open_tasks_raw(full[-3:], series_dir=self.fx.dir)
        self.assertEqual(by_full, by_window,
                         "дайджест и протокол расходятся в том, что считать снятым")

    def test_without_a_cutoff_nothing_is_dropped(self):
        # Страховка от зелёного вхолостую.
        with mock.patch.object(sm, "load_watched_for_modes",
                               return_value=_watched(open_questions_mode="carry")):
            got = sm._resolve_open_tasks_raw(self.fx.digests()[-3:],
                                             series_dir=self.fx.dir)
        self.assertEqual(got, [self.OLD])


# ==========================================================================
# 3. Отсечка не может быть невозможной или будущей датой
# ==========================================================================
class TestCarrySinceValidation(unittest.TestCase):
    """Проверялась только ФОРМА. «2026-13-45» и «2099-01-01» проходили валидатор,
    писатель и читателя, а сравнение дат строковое — такая отсечка старше любой
    реальной выжимки и срезает хвост серии на всех поверхностях НАВСЕГДА, оставляя
    одну строку в журнале. Поле правят руками: план предписывает разложить режимы
    серий в реестре до включения."""

    FIELD = reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD

    def _errors(self, value):
        rec = {"id": "x", "series": SERIES, "type": "google-calendar",
               "url": "https://telemost.yandex.ru/j/1"}
        if value is not None:
            rec[self.FIELD] = value
        return [e for e in reg.validate_watched_record(rec, {"rooms": []})
                if self.FIELD in e]

    def test_impossible_dates_are_rejected(self):
        for bad in ("2026-13-45", "2026-02-30", "2026-00-10", "2026-01-32"):
            self.assertTrue(self._errors(bad), f"валидатор пропустил {bad}")

    def test_future_dates_are_rejected(self):
        future = (_date.today() + timedelta(days=1)).isoformat()
        self.assertTrue(self._errors(future), "валидатор пропустил дату из будущего")
        self.assertTrue(self._errors("2099-01-01"))

    def test_real_past_dates_and_absence_are_fine(self):
        self.assertEqual(self._errors(_date.today().isoformat()), [])
        self.assertEqual(self._errors("2026-02-28"), [])
        self.assertEqual(self._errors(None), [])

    def test_garbage_is_still_rejected(self):
        for bad in ("вчера", "2026/07/15", "15.07.2026", 20260715, ""):
            self.assertTrue(self._errors(bad), f"валидатор пропустил {bad!r}")

    def test_the_field_is_documented_where_people_edit_the_file(self):
        # Человек правит реестр руками и о поле узнавал, только увидев его в чужой
        # записи. Шапка обязана его называть.
        self.assertIn(self.FIELD, reg.WATCHED_HEADER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
