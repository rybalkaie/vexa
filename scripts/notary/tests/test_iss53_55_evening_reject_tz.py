# -*- coding: utf-8 -*-
"""Ф4 (ISS-53 + ISS-55): «нет» должно запоминаться, время — совпадать.

ISS-53. Единственное, что владелец делает с ботом каждый вечер, — отвечает на блок
«📅 Встречи и запись». На «нет» бот раньше не оставлял НИКАКОГО следа на диске
(`apply_decisions` для `reject` только рисовал строку в summary), и назавтра
предлагал ту же самую встречу снова — выглядело как «он меня не слышит».

ISS-55. Вопрос бота рисовался в `MEETING_NOTARY_DISPLAY_TZ` (на бою Europe/Moscow),
а подтверждение — жёстко в Asia/Dubai: одна и та же встреча в вопросе и в ответе
расходилась на час.

Покрывает:
  • повторный вечерний блок не предлагает отклонённую встречу (сквозной путь:
    блок → snapshot → ответ «нет» → блок ещё раз);
  • различие «нет» (этот экземпляр) vs «не предлагай эту серию» (`ignore_forever`,
    вся серия) не схлопнуто: после «нет» следующий инстанс серии предлагается,
    после `ignore_forever` — не предлагается ни один;
  • реестр сохраняется и когда решений-отказов больше нуля (было `applied>0 or ignored>0`);
  • ретеншн: отказ по встрече старше 30 дней вычищается, свежий — остаётся,
    активные записи и «глушилки» `ignore_forever` не трогаются;
  • время в вопросе и в подтверждении совпадает при любой TZ.

Сети/`claude`/боевых данных здесь нет: календарь, реестр и каталог состояния
подсунуты моками, snapshot пишется во временный каталог самого теста.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary/tests
    python3 test_iss53_55_evening_reject_tz.py
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

import notary.meetings_apply_reply as mar  # noqa: E402
import notary.meetings_evening_block as meb  # noqa: E402


CAL = "primary"          # календарь один — чтобы collect_calendar_ids дал ровно его
SERIES_REC_ID = "rec-koordinaciya"
ROOM_URL = "https://telemost.yandex.ru/j/00000000000000"

# Фиксированный момент для TZ-тестов: 09:00 UTC = 12:00 Москва = 13:00 Дубай.
FIXED_ISO = "2026-08-06T09:00:00+00:00"


# ==========================================================================
# Хелперы: фейковый календарь и прогон обоих скриптов без сети
# ==========================================================================
def _soon(days: int, hour: int = 9) -> datetime:
    """Момент внутри 7-дневного горизонта блока (days ≥ 1 → строго в будущем)."""
    base = datetime.now(timezone.utc) + timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def _event(event_id: str, start: datetime, title: str, recurring: str | None = None) -> dict:
    """Событие в форме, в которой его отдаёт Google Calendar (singleEvents=true)."""
    evt = {
        "id": event_id,
        "status": "confirmed",
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
        "conferenceData": {"entryPoints": [{"uri": ROOM_URL}]},
    }
    if recurring:
        evt["recurringEventId"] = recurring
    return evt


class _FakeCreds:
    """Заглушка вместо OAuth-кредов — блок их только прокидывает в list_events."""


def _run_evening_block(
    watched_records,
    events,
    state_dir: Path,
    display_tz: str | None = None,
    known: dict | None = None,
    commit_known: bool = False,
):
    """Прогнать meetings_evening_block.main() без Google. Возвращает (rc, распарсенный JSON).

    `events` — список (все в календаре CAL) либо словарь {calendar_id: [события]}.
    Календарь у фейка НАСТОЯЩИЙ: он отдаёт только события запрошенного календаря.
    Иначе половина ключа индекса `(calendar_id, event_id)` тестом не проверяется.
    """
    events_by_cal = dict(events) if isinstance(events, dict) else {CAL: list(events)}

    def fake_list_events(creds, calendar_id, time_min, time_max, **_kw):
        return list(events_by_cal.get(calendar_id, [])), creds

    stdout = io.StringIO()
    argv = ["meetings_evening_block.py"] + (["--commit-known"] if commit_known else [])
    patches = [
        mock.patch.object(meb, "setup_logging", lambda: None),
        mock.patch.object(meb, "load_watched", lambda *a, **k: {"watched": list(watched_records)}),
        mock.patch.object(meb, "load_rooms", lambda *a, **k: {"rooms": []}),
        mock.patch.object(meb, "load_known", lambda: json.loads(json.dumps(known or {}))),
        mock.patch.object(meb, "load_credentials", lambda: _FakeCreds()),
        mock.patch.object(meb, "list_events", fake_list_events),
        mock.patch.object(meb, "STATE_DIR", state_dir),
        # KNOWN_CACHE_FILE связан при импорте — без этой заплатки --commit-known
        # пишет в БОЕВОЙ ~/.local/state владельца.
        mock.patch.object(meb, "KNOWN_CACHE_FILE", state_dir / "meetings-known-instances.json"),
        mock.patch.object(sys, "argv", argv),
    ]
    if display_tz:
        patches.append(mock.patch.object(meb, "DISPLAY_TZ", ZoneInfo(display_tz)))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(stdout))
        rc = meb.main()
    return rc, json.loads(stdout.getvalue())


def _read_snapshot(state_dir: Path) -> dict:
    """Прочитать snapshot, который блок ТОЛЬКО ЧТО записал (шов «блок → файл → apply-reply»)."""
    with (state_dir / "meetings-shown.last.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def _apply(decisions, items, watched_records=(), today=None, shown_tz=None, save_raises=None):
    """Прогнать apply_decisions поверх реестра-в-памяти. Возвращает (result, data, saved_calls)."""
    data = {"watched": list(watched_records)}
    saved: list[dict] = []
    kwargs = {}
    if today is not None:
        kwargs["today"] = today
    if shown_tz is not None:
        kwargs["shown_tz"] = shown_tz

    def fake_save(d):
        if save_raises is not None:
            raise save_raises
        saved.append(d)

    with mock.patch.object(mar, "load_watched", lambda *a, **k: data), \
            mock.patch.object(mar, "save_watched", fake_save), \
            mock.patch.object(mar, "load_rooms", lambda *a, **k: {"rooms": []}), \
            mock.patch.object(mar, "release_watched_lock", lambda: None):
        result = mar.apply_decisions(decisions, items, **kwargs)
    return result, data, saved


def _rejection_record(rec_id: str, meeting_day: date, **extra) -> dict:
    """Запись-отказ ровно в том виде, в каком её пишет `_mark_rejected`."""
    rec = {
        "id": rec_id,
        "series": rec_id,
        "type": "google-calendar",
        "calendar_id": CAL,
        "event_id": f"evt-{rec_id}",
        "enabled": False,
        "ignored": False,
        # Метка «эту запись завёл отказ» — без неё ретеншн не имеет права
        # трогать запись: она не его.
        mar.REJECTION_OWNER_FIELD: True,
        "rejected_at": meeting_day.isoformat(),
        "rejected_start_at": f"{meeting_day.isoformat()}T09:00:00+00:00",
    }
    rec.update(extra)
    return rec


class _EveningCase(unittest.TestCase):
    """База: временный STATE_DIR + два инстанса одной серии внутри горизонта."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.start_a = _soon(1)
        self.start_b = _soon(4)
        self.evt_a = _event("inst-a", self.start_a, "Координация по маркетплейсам", SERIES_REC_ID)
        self.evt_b = _event("inst-b", self.start_b, "Координация по маркетплейсам", SERIES_REC_ID)
        self.addCleanup(self._tmp.cleanup)

    def run_block(self, watched_records=(), events=None, display_tz=None,
                  known=None, commit_known=False):
        rc, res = _run_evening_block(
            watched_records, events if events is not None else [self.evt_a, self.evt_b],
            self.state_dir, display_tz=display_tz, known=known, commit_known=commit_known,
        )
        self.assertEqual(rc, 0)
        return res

    def snapshot_items(self):
        """items из ФАЙЛА, который блок только что записал, — как их читает apply-reply."""
        return _read_snapshot(self.state_dir)["items"]


# ==========================================================================
# ISS-53: «нет» запоминается
# ==========================================================================
class TestRejectIsRemembered(_EveningCase):

    def test_repeat_evening_block_does_not_repropose_rejected(self):
        # 1. Пустой реестр — блок предлагает оба инстанса.
        first = self.run_block()
        self.assertEqual(first["new_count"], 2)
        items = self.snapshot_items()

        # 2. Владелец: «1 нет».
        result, data, _saved = _apply(
            [{"n": 1, "action": "reject", "room": None}], items)
        self.assertEqual(result["rejected"], 1)

        # 3. Вечер следующего дня: тот же календарь, реестр после ответа.
        second = self.run_block(watched_records=data["watched"])
        proposed = {it["event_id"] for it in second["items"]}
        self.assertNotIn(
            "inst-a", proposed,
            "ISS-53: отклонённая встреча снова в блоке — «нет» не запомнено",
        )

    def test_reject_writes_disabled_record_with_date(self):
        first = self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        _result, data, _saved = _apply(
            [{"n": 1, "action": "reject", "room": None}], items)

        recs = [r for r in data["watched"] if r.get("event_id") == "inst-a"]
        self.assertEqual(len(recs), 1, "отказ не оставил следа в реестре")
        rec = recs[0]
        self.assertIs(rec["enabled"], False)
        self.assertTrue(rec.get("rejected_at"), "нет даты отказа")
        # Дата отказа — обычный YYYY-MM-DD, её потом читает ретеншн.
        self.assertRegex(rec["rejected_at"], r"^\d{4}-\d{2}-\d{2}$")

    def test_registry_is_saved_when_only_rejections(self):
        # Раньше условие сохранения было `applied > 0 or ignored > 0` — отказ терялся.
        first = self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        _result, _data, saved = _apply(
            [{"n": 1, "action": "reject", "room": None}], items)
        self.assertEqual(len(saved), 1, "реестр не сохранён при rejected > 0")

    def test_rejected_record_is_inert_for_scheduler(self):
        # Запись-отказ не должна привести к записи встречи: планировщик берёт
        # только enabled=true и ignored=false (scheduler.py:110-112).
        first = self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        _result, data, _saved = _apply(
            [{"n": 1, "action": "reject", "room": None}], items)
        rec = data["watched"][0]
        self.assertFalse(rec.get("enabled", True))
        self.assertFalse(rec.get("ignored", False),
                         "отказ по экземпляру — не «глушилка» серии")

    def test_second_reject_does_not_duplicate_record(self):
        # Повторный «нет» по той же встрече (владелец ответил дважды) не плодит записи.
        first = self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        _r1, data, _s1 = _apply([{"n": 1, "action": "reject", "room": None}], items)
        _r2, data2, _s2 = _apply(
            [{"n": 1, "action": "reject", "room": None}], items,
            watched_records=data["watched"])
        self.assertEqual(len(data2["watched"]), 1)

    def test_owner_changed_mind_reenables_record(self):
        # «нет» вчера, «да» сегодня — запись оживает и следов отказа не остаётся.
        first = self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        _r1, data, _s1 = _apply([{"n": 1, "action": "reject", "room": None}], items)
        result, data2, _s2 = _apply(
            [{"n": 1, "action": "accept_once", "room": None}], items,
            watched_records=data["watched"])
        self.assertEqual(result["applied"], 1)
        rec = data2["watched"][0]
        self.assertIs(rec["enabled"], True)
        self.assertIsNone(rec.get("rejected_at"),
                          "передумал — метка отказа должна уйти, иначе её съест ретеншн")


# ==========================================================================
# ISS-53: «нет» (экземпляр) ≠ «не предлагай эту серию» (вся серия)
# ==========================================================================
class TestRejectVsIgnoreForever(_EveningCase):

    def test_reject_keeps_next_instance_of_series(self):
        first = self.run_block()
        items = self.snapshot_items()
        _result, data, _saved = _apply(
            [{"n": 1, "action": "reject", "room": None}], items)

        second = self.run_block(watched_records=data["watched"])
        proposed = {it["event_id"] for it in second["items"]}
        self.assertEqual(
            proposed, {"inst-b"},
            "«нет» — это про ОДИН экземпляр: следующая встреча серии обязана остаться",
        )

    def test_ignore_forever_silences_whole_series(self):
        first = self.run_block()
        items = self.snapshot_items()
        result, data, saved = _apply(
            [{"n": 1, "action": "ignore_forever", "room": None}], items)
        self.assertEqual(result["ignored_forever"], 1)
        self.assertEqual(len(saved), 1)

        second = self.run_block(watched_records=data["watched"])
        self.assertEqual(
            second["new_count"], 0,
            "«не предлагай эту серию» обязано гасить все инстансы серии",
        )

    def test_ignore_forever_record_differs_from_rejection(self):
        first = self.run_block()
        items = self.snapshot_items()
        _r_ign, data_ign, _s1 = _apply(
            [{"n": 1, "action": "ignore_forever", "room": None}], items)
        _r_rej, data_rej, _s2 = _apply(
            [{"n": 1, "action": "reject", "room": None}], items)

        ign = data_ign["watched"][0]
        rej = data_rej["watched"][0]
        self.assertIs(ign["ignored"], True)
        self.assertEqual(ign.get("recurring_event_id"), SERIES_REC_ID,
                         "глушилка серии обязана ключеваться по серии")
        self.assertFalse(rej.get("ignored", False))
        self.assertEqual(rej.get("event_id"), "inst-a",
                         "отказ обязан ключеваться по экземпляру, а не по серии")
        self.assertIsNone(ign.get("rejected_at"),
                          "«никогда» — не разовый отказ, ретеншн его чистить не должен")


# ==========================================================================
# ISS-53: ретеншн отказных записей (30 дней после даты встречи)
# ==========================================================================
class TestRejectionRetention(unittest.TestCase):

    _rejection = staticmethod(_rejection_record)

    def test_rejection_older_than_30_days_is_pruned(self):
        today = date(2026, 8, 5)
        data = {"watched": [self._rejection("oneoff-old", today - timedelta(days=31))]}
        removed = mar.prune_stale_rejections(data, today=today)
        self.assertEqual(removed, 1)
        self.assertEqual(data["watched"], [])

    def test_rejection_within_30_days_is_kept(self):
        today = date(2026, 8, 5)
        data = {"watched": [
            self._rejection("oneoff-fresh", today - timedelta(days=30)),
            self._rejection("oneoff-yesterday", today - timedelta(days=1)),
            self._rejection("oneoff-future", today + timedelta(days=3)),
        ]}
        removed = mar.prune_stale_rejections(data, today=today)
        self.assertEqual(removed, 0)
        self.assertEqual(len(data["watched"]), 3)

    def test_prune_never_touches_active_or_ignored_records(self):
        today = date(2026, 8, 5)
        active = {"id": "anzhee-direktorat", "series": "anzhee-direktorat",
                  "type": "google-calendar", "calendar_id": CAL,
                  "recurring_event_id": SERIES_REC_ID, "enabled": True, "ignored": False}
        forever = {"id": "series-dead", "series": "series-dead",
                   "type": "google-calendar", "calendar_id": CAL,
                   "recurring_event_id": "rec-dead", "enabled": False, "ignored": True}
        # Древняя запись без метки отказа (например, отработанная one-off) — не наша.
        legacy = {"id": "oneoff-legacy", "series": "oneoff-legacy",
                  "type": "google-calendar", "calendar_id": CAL,
                  "event_id": "evt-legacy", "enabled": False, "ignored": False}
        data = {"watched": [active, forever, legacy,
                            self._rejection("oneoff-old", today - timedelta(days=90))]}
        removed = mar.prune_stale_rejections(data, today=today)
        self.assertEqual(removed, 1)
        self.assertEqual([r["id"] for r in data["watched"]],
                         ["anzhee-direktorat", "series-dead", "oneoff-legacy"])

    def test_unparseable_dates_are_kept(self):
        # Мусор в дате — повод оставить запись, а не удалить вслепую.
        today = date(2026, 8, 5)
        broken = self._rejection("oneoff-broken", today - timedelta(days=90))
        broken["rejected_start_at"] = "позавчера"
        broken["rejected_at"] = "давно"
        data = {"watched": [broken]}
        with self.assertLogs(mar.logger, level="WARNING"):
            removed = mar.prune_stale_rejections(data, today=today)
        self.assertEqual(removed, 0)
        self.assertEqual(len(data["watched"]), 1)

    def test_rejected_at_alone_is_enough_for_retention(self):
        # Если старт встречи не записался — считаем по дате отказа, запись не бессмертна.
        today = date(2026, 8, 5)
        rec = self._rejection("oneoff-nostart", today - timedelta(days=60))
        rec.pop("rejected_start_at")
        data = {"watched": [rec]}
        self.assertEqual(mar.prune_stale_rejections(data, today=today), 1)

    def test_apply_decisions_prunes_and_saves(self):
        # Ретеншн едет тем же ходом, что и применение ответа: лок уже взят.
        today = date(2026, 8, 5)
        stale = self._rejection("oneoff-old", today - timedelta(days=45))
        items = [{
            "n": 1, "calendar_id": CAL, "event_id": "inst-a",
            "recurring_event_id": SERIES_REC_ID, "title": "Координация",
            "start_at": FIXED_ISO, "url_in_event": ROOM_URL, "has_url_in_event": True,
        }]
        _result, data, saved = _apply(
            [{"n": 1, "action": "reject", "room": None}], items,
            watched_records=[stale], today=today)
        self.assertEqual(len(saved), 1)
        self.assertNotIn("oneoff-old", [r["id"] for r in data["watched"]])


# ==========================================================================
# ISS-55: время в вопросе и в подтверждении
# ==========================================================================
class TestDisplayTimezone(unittest.TestCase):

    ALL_TZ = ("Europe/Moscow", "Asia/Dubai", "UTC", "America/New_York")

    def test_fmt_when_short_follows_display_tz_env(self):
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": "Europe/Moscow"}):
            self.assertEqual(mar.fmt_when_short(FIXED_ISO), "06.08 12:00")

    def test_fmt_when_short_default_is_dubai(self):
        # Дефолт тот же, что у вечернего блока: env не задан → Asia/Dubai.
        env = {k: v for k, v in os.environ.items() if k != "MEETING_NOTARY_DISPLAY_TZ"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(mar.fmt_when_short(FIXED_ISO), "06.08 13:00")

    def test_question_and_confirmation_agree_in_any_tz(self):
        for tz in self.ALL_TZ:
            with self.subTest(tz=tz), \
                    mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": tz}), \
                    mock.patch.object(meb, "DISPLAY_TZ", ZoneInfo(tz)):
                question = meb.fmt_when(FIXED_ISO)            # строка блока: «чт 06.08 13:00»
                confirmation = mar.fmt_when_short(FIXED_ISO)  # ответ бота: «06.08 13:00»
                self.assertEqual(
                    question.split()[-1], confirmation.split()[-1],
                    f"ISS-55: в {tz} вопрос «{question}» и подтверждение «{confirmation}» "
                    f"показывают разное время",
                )
                # Полная дата тоже обязана совпасть — сравниваем с «пт 06.08 13:00».
                self.assertTrue(meb.fmt_when_simple(FIXED_ISO).endswith(confirmation))

    def test_evening_block_reads_the_same_env_var(self):
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": "Europe/Moscow"}):
            importlib.reload(meb)
            self.assertEqual(str(meb.DISPLAY_TZ), "Europe/Moscow")
        importlib.reload(meb)  # вернуть модуль к окружению прогона

    def test_naive_iso_and_garbage_survive(self):
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": "Europe/Moscow"}):
            # Без офсета трактуем как UTC — так же, как вечерний блок.
            self.assertEqual(mar.fmt_when_short("2026-08-06T09:00:00"), "06.08 12:00")
            self.assertEqual(mar.fmt_when_short("не дата"), "не дата")

    def test_unknown_tz_falls_back_to_dubai(self):
        # Опечатка в env не должна ронять применение ответа владельца.
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": "Mars/Olympus"}), \
                self.assertLogs(mar.logger, level="WARNING"):
            self.assertEqual(mar.fmt_when_short(FIXED_ISO), "06.08 13:00")


class TestTimeMatchesEndToEnd(_EveningCase):
    """Сквозной ISS-55: час в блоке и час в подтверждении на один и тот же экземпляр."""

    def _hhmm(self, text: str) -> str:
        m = re.search(r"\b(\d{2}:\d{2})\b", text)
        self.assertIsNotNone(m, f"не нашёл время в строке: {text!r}")
        return m.group(1)

    def test_same_hour_in_block_and_in_reply(self):
        for tz in ("Europe/Moscow", "Asia/Dubai", "America/New_York"):
            with self.subTest(tz=tz):
                block = self.run_block(events=[self.evt_a], display_tz=tz)
                question_line = [ln for ln in block["block_text"].split("\n")
                                 if ln.startswith("1. ")][0]
                items = self.snapshot_items()
                with mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": tz}):
                    result, _data, _saved = _apply(
                        [{"n": 1, "action": "reject", "room": None}], items)
                confirmation_line = result["summary_lines"][0]
                self.assertEqual(
                    self._hhmm(question_line), self._hhmm(confirmation_line),
                    f"ISS-55 ({tz}): «{question_line}» vs «{confirmation_line}»",
                )

    def test_snapshot_carries_the_tz_of_the_question(self):
        # Вопрос рисует один процесс (юнит вечернего дайджеста), ответ применяет
        # другой (subprocess листенера). Правда о времени обязана ехать вместе с
        # вопросом, а не через окружение, которое у двух юнитов может разъехаться.
        self.run_block(events=[self.evt_a], display_tz="Europe/Moscow")
        snap = _read_snapshot(self.state_dir)
        self.assertEqual(snap["shown_tz"], "Europe/Moscow")

    def test_confirmation_follows_question_tz_even_if_env_diverged(self):
        block = self.run_block(events=[self.evt_a], display_tz="Europe/Moscow")
        question_line = [ln for ln in block["block_text"].split("\n")
                         if ln.startswith("1. ")][0]
        snap = _read_snapshot(self.state_dir)
        # У apply-reply в окружении ДРУГАЯ зона — юниты разъехались. Расхождение
        # обязано быть поймано (warning), а не проглочено молча.
        with mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": "Asia/Dubai"}), \
                self.assertLogs(mar.logger, level="WARNING"):
            result, _data, _saved = _apply(
                [{"n": 1, "action": "reject", "room": None}], snap["items"],
                shown_tz=snap.get("shown_tz"))
        self.assertEqual(
            self._hhmm(question_line), self._hhmm(result["summary_lines"][0]),
            "подтверждение показало не тот час, что вопрос: TZ вопроса не доехала",
        )


# ==========================================================================
# Ремонт 1: молчание владельца — НЕ отказ
# ==========================================================================
class TestSilenceIsNotRejection(_EveningCase):
    """Владелец ответил про одну встречу из трёх — две другие не должны умереть.

    Пока «нет» ничего не писало на диск, правило промпта «номер не упомянут →
    reject» было безобидным: встреча возвращалась завтра. С персистентным
    отказом то же правило хоронит встречи, про которые владелец просто ничего
    не сказал, и отменить это в Telegram нечем.
    """

    def _three_meetings(self):
        evt_c = _event("inst-c", _soon(2), "Созвон с подрядчиком")
        first = self.run_block(events=[self.evt_a, self.evt_b, evt_c])
        self.assertEqual(first["new_count"], 3)
        return evt_c, self.snapshot_items()

    def test_answer_about_one_meeting_does_not_kill_the_others(self):
        evt_c, items = self._three_meetings()
        # Так по новому промпту модель разбирает ответ «1 да»: про 2 и 3
        # владелец не сказал НИЧЕГО.
        decisions = [
            {"n": 1, "action": "accept_once", "room": None},
            {"n": 2, "action": "no_answer", "room": None},
            {"n": 3, "action": "no_answer", "room": None},
        ]
        result, data, _saved = _apply(decisions, items)
        self.assertEqual(result["rejected"], 0, "молчание записано как отказ")
        self.assertEqual(
            len(data["watched"]), 1,
            "ответ «1 да» на три встречи обязан оставить ровно одну запись",
        )

        second = self.run_block(
            watched_records=data["watched"], events=[self.evt_a, self.evt_b, evt_c])
        proposed = {it["event_id"] for it in second["items"]}
        self.assertEqual(
            proposed, {"inst-b", "inst-c"},
            "встречи, про которые владелец промолчал, обязаны прийти снова",
        )

    def test_unanswered_numbers_are_reported_to_owner(self):
        _evt_c, items = self._three_meetings()
        result, _data, _saved = _apply([
            {"n": 1, "action": "accept_once", "room": None},
            {"n": 2, "action": "no_answer", "room": None},
            {"n": 3, "action": "no_answer", "room": None},
        ], items)
        self.assertEqual(result["no_answer"], [2, 3])
        self.assertTrue(
            any("решения не было" in ln for ln in result["summary_lines"]),
            f"владельцу не сказали про неотвеченные номера: {result['summary_lines']}",
        )

    def test_no_answer_alone_writes_nothing(self):
        _evt_c, items = self._three_meetings()
        result, data, saved = _apply(
            [{"n": 1, "action": "no_answer", "room": None}], items)
        self.assertEqual(data["watched"], [])
        self.assertEqual(saved, [], "молчание не должно даже трогать реестр")
        self.assertEqual(result["rejected"], 0)

    def test_prompt_forbids_turning_silence_into_reject(self):
        _evt_c, items = self._three_meetings()
        prompt = mar.build_claude_prompt(items, {"rooms": []}, "1 да")
        self.assertIn("no_answer", mar.VALID_ACTIONS)
        self.assertIn("МОЛЧАНИЕ — НЕ ОТКАЗ", prompt)
        self.assertIn("no_answer", prompt)
        self.assertNotIn(
            "нет упоминания какого-то номера — для него action=reject", prompt,
            "промпт всё ещё велит модели превращать молчание в отказ",
        )

    def test_explicit_no_still_persists(self):
        # Контроль: явное «нет» обязано оставаться записанным (иначе «починили»
        # ISS-53 обратно в баг).
        _evt_c, items = self._three_meetings()
        _result, data, saved = _apply(
            [{"n": 1, "action": "reject", "room": None}], items)
        self.assertEqual(len(data["watched"]), 1)
        self.assertEqual(len(saved), 1)


# ==========================================================================
# Ремонт 2: бот не рапортует «запись сдвинул/снял» про отклонённую встречу
# ==========================================================================
class TestRejectedIsNotReportedAsRecorded(_EveningCase):

    def _known(self, event_id: str, start: datetime, series: str, recurring=SERIES_REC_ID):
        return {event_id: {
            "calendar_id": CAL,
            "start_at": start.isoformat(),
            "title": "Координация по маркетплейсам",
            "series": series,
            "recurring_event_id": recurring,
        }}

    def _reject_inst_a(self):
        self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        _r, data, _s = _apply([{"n": 1, "action": "reject", "room": None}], items)
        return data["watched"]

    def test_moved_section_skips_rejected(self):
        watched = self._reject_inst_a()
        moved_evt = _event("inst-a", self.start_a + timedelta(hours=5),
                           "Координация по маркетплейсам", SERIES_REC_ID)
        res = self.run_block(
            watched_records=watched, events=[moved_evt],
            known=self._known("inst-a", self.start_a, watched[0]["id"]),
        )
        self.assertEqual(
            res["moved_count"], 0,
            "бот рапортует «запись сдвинул» про встречу, которую запретили писать",
        )
        self.assertNotIn("Перенесённые", res["block_text"])

    def test_cancelled_section_skips_rejected(self):
        watched = self._reject_inst_a()
        res = self.run_block(
            watched_records=watched, events=[],
            known=self._known("inst-a", self.start_a, watched[0]["id"]),
        )
        self.assertEqual(
            res["cancelled_count"], 0,
            "бот рапортует «запись снял» про встречу, которую запретили писать",
        )
        self.assertNotIn("Отменённые", res["block_text"])

    def test_recorded_meeting_still_reports_moved_and_cancelled(self):
        # Положительный контроль: фильтр не должен выключить сами разделы.
        live = {"id": "koordinaciya", "series": "koordinaciya",
                "type": "google-calendar", "calendar_id": CAL,
                "recurring_event_id": SERIES_REC_ID, "room": "@t11",
                "enabled": True, "ignored": False}
        moved_evt = _event("inst-a", self.start_a + timedelta(hours=5),
                           "Координация по маркетплейсам", SERIES_REC_ID)
        moved = self.run_block(
            watched_records=[live], events=[moved_evt],
            known=self._known("inst-a", self.start_a, "koordinaciya"),
        )
        self.assertEqual(moved["moved_count"], 1)
        cancelled = self.run_block(
            watched_records=[live], events=[],
            known=self._known("inst-a", self.start_a, "koordinaciya"),
        )
        self.assertEqual(cancelled["cancelled_count"], 1)

    def test_rejected_instance_never_enters_known_cache(self):
        watched = self._reject_inst_a()
        self.run_block(watched_records=watched, events=[self.evt_a], commit_known=True)
        cache_file = self.state_dir / "meetings-known-instances.json"
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(
            cache, {},
            "отклонённая встреча попала в кэш «мы это пишем» — из него растут "
            "разделы «Перенесённые»/«Отменённые»",
        )

    def test_ignore_forever_also_stays_out_of_reports(self):
        # Тот же класс, пред-существующий: «не предлагай эту серию».
        self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        _r, data, _s = _apply([{"n": 1, "action": "ignore_forever", "room": None}], items)
        res = self.run_block(
            watched_records=data["watched"], events=[],
            known=self._known("inst-a", self.start_a, data["watched"][0]["id"]),
        )
        self.assertEqual(res["cancelled_count"], 0)


# ==========================================================================
# Ремонт 3: сбой записи реестра — честный отказ, а не «Применил»
# ==========================================================================
class TestRegistryWriteFailure(_EveningCase):
    """`save_watched` падает (ENOSPC / read-only ФС / слетели права).

    Раньше исключение вылетало наружу, stdout оставался пустым, и листенер
    (`meetings_listener.py:2940`) отвечал владельцу «Применил без summary.» —
    то есть бот подтверждал «нет», которое потерял.
    """

    def test_save_failure_is_reported_as_failure(self):
        self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        with self.assertLogs(mar.logger, level="ERROR"):
            result, _data, saved = _apply(
                [{"n": 1, "action": "reject", "room": None}], items,
                save_raises=OSError(28, "No space left on device"),
            )
        self.assertEqual(saved, [])
        # Ключ `error` листенер печатает владельцу как «Не применил: …».
        self.assertTrue(result.get("error"), "сбой записи выдан за успех")
        self.assertIn("не смог сохранить", result["error"])
        self.assertTrue(result.get("save_failed"))
        self.assertEqual(
            result["summary_lines"], [],
            "нельзя показывать подтверждения по решениям, которых нет на диске",
        )
        self.assertEqual(result["rejected"], 0)

    def test_successful_save_has_no_error_key(self):
        self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        result, _data, saved = _apply([{"n": 1, "action": "reject", "room": None}], items)
        self.assertEqual(len(saved), 1)
        self.assertIsNone(result.get("error"))


# ==========================================================================
# Ремонт 4: ретеншн трогает ТОЛЬКО свои записи (сторожа M8/M9/M18)
# ==========================================================================
class TestRetentionGuards(_EveningCase):

    def _solo_items(self):
        """Не-recurring встреча: id отказа и id «глушилки» совпадают."""
        evt = _event("inst-solo", _soon(1), "Разовый созвон с подрядчиком")
        self.run_block(events=[evt])
        return self.snapshot_items()

    def test_no_then_never_leaves_one_immortal_record(self):
        # (а) M18 + M8: «нет», потом «не предлагай серию» по не-recurring встрече.
        items = self._solo_items()
        _r1, data, _s1 = _apply([{"n": 1, "action": "reject", "room": None}], items)
        _r2, data2, _s2 = _apply(
            [{"n": 1, "action": "ignore_forever", "room": None}], items,
            watched_records=data["watched"])
        self.assertEqual(len(data2["watched"]), 1, "id совпадают — запись должна быть одна")
        rec = data2["watched"][0]
        self.assertIs(rec["ignored"], True)
        self.assertIsNone(rec.get("rejected_at"), "на «никогда» осталась метка отказа")
        self.assertNotIn(mar.REJECTION_OWNER_FIELD, rec)
        self.assertEqual(
            mar.prune_stale_rejections({"watched": [rec]}, today=date(2030, 1, 1)), 0,
            "ретеншн через 30 дней съел владельческое «никогда»",
        )

    def test_enabled_record_with_rejection_marks_survives(self):
        # (б) M9: `meeting-watch enable` ставит enabled=true и метки НЕ снимает.
        rec = _rejection_record("oneoff-paused", date(2026, 7, 1))
        rec["enabled"] = True
        data = {"watched": [rec]}
        self.assertEqual(mar.prune_stale_rejections(data, today=date(2026, 8, 5)), 0)
        self.assertEqual(len(data["watched"]), 1,
                         "включённую запись ретеншн удалять не имеет права")

    def test_no_after_never_does_not_downgrade_silence(self):
        # (в) «нет» после «никогда» не превращает вечную глушилку в 30-дневную.
        items = self._solo_items()
        _r1, data, _s1 = _apply([{"n": 1, "action": "ignore_forever", "room": None}], items)
        _r2, data2, _s2 = _apply(
            [{"n": 1, "action": "reject", "room": None}], items,
            watched_records=data["watched"])
        rec = data2["watched"][0]
        self.assertIs(rec["ignored"], True, "«нет» понизило вечное «никогда» до отказа")
        self.assertIsNone(rec.get("rejected_at"))
        self.assertEqual(
            mar.prune_stale_rejections({"watched": [rec]}, today=date(2030, 1, 1)), 0)

    def test_reject_does_not_mark_owners_record_for_deletion(self):
        # Владелец сказал «да» в понедельник (запись обросла привязкой чата и
        # разметкой), в среду проскроллил вверх и ответил «1 нет» на то же
        # сообщение. Ретеншн не имеет права унести ЧУЖУЮ запись.
        items = self._solo_items()
        rid = mar.make_record_id(items[0], "once")
        owner_rec = {
            "id": rid, "series": rid, "type": "google-calendar",
            "calendar_id": CAL, "event_id": items[0]["event_id"],
            "room": "@t11", "telegram_chat_id": -100777,
            "company": "anzhee", "visibility": "company",
            "open_questions_mode": "carry",
            "enabled": True, "ignored": False,
        }
        _r, data, _s = _apply(
            [{"n": 1, "action": "reject", "room": None}], items,
            watched_records=[owner_rec])
        rec = data["watched"][0]
        self.assertEqual(len(data["watched"]), 1)
        self.assertIs(rec["enabled"], False, "«нет» обязано выключить запись")
        self.assertIsNone(rec.get("rejected_at"),
                          "метка смерти поставлена на запись, которую вёл владелец")
        self.assertNotIn(mar.REJECTION_OWNER_FIELD, rec)
        self.assertEqual(rec["telegram_chat_id"], -100777)
        self.assertEqual(
            mar.prune_stale_rejections({"watched": [rec]}, today=date(2030, 1, 1)), 0,
            "ретеншн унёс запись владельца вместе с привязкой чата и разметкой",
        )

    def test_reject_never_downgrades_a_silencer_that_carries_our_marks(self):
        # Прямой сторож в `_mark_rejected`. Смешанное состояние (правили реестр
        # руками / данные прошлой версии): запись — и «никогда», и с меткой
        # отказа. «Нет» по ней не имеет права снять «никогда».
        items = self._solo_items()
        rid = mar.make_record_id(items[0], "once")
        mixed = _rejection_record(rid, date(2026, 7, 1),
                                  event_id=items[0]["event_id"], ignored=True)
        _r, data, _s = _apply([{"n": 1, "action": "reject", "room": None}], items,
                              watched_records=[mixed])
        rec = data["watched"][0]
        self.assertIs(rec["ignored"], True, "«нет» понизило вечное «никогда» до отказа")

    def test_silencer_with_rejection_marks_is_never_pruned(self):
        # Прямой сторож `ignored` в ретеншне (M8): «глушилка» серии вечная, даже
        # если на ней почему-то остались метки отказа.
        rec = _rejection_record("series-dead", date(2026, 6, 1), ignored=True)
        data = {"watched": [rec]}
        self.assertEqual(mar.prune_stale_rejections(data, today=date(2026, 8, 5)), 0)
        self.assertEqual(len(data["watched"]), 1,
                         "ретеншн унёс владельческое «не предлагай эту серию»")

    def test_legacy_marks_without_owner_flag_are_not_pruned(self):
        # Запись с метками, но без «её завёл отказ» (данные до этой правки).
        rec = _rejection_record("oneoff-legacy", date(2026, 6, 1))
        rec.pop(mar.REJECTION_OWNER_FIELD)
        data = {"watched": [rec]}
        self.assertEqual(mar.prune_stale_rejections(data, today=date(2026, 8, 5)), 0)

    def test_broken_record_does_not_crash_retention(self):
        # Битая строка в watched.yaml не должна ронять ответ владельца.
        data = {"watched": ["просто строка",
                            _rejection_record("oneoff-old", date(2026, 6, 1))]}
        self.assertEqual(mar.prune_stale_rejections(data, today=date(2026, 8, 5)), 1)
        self.assertEqual(data["watched"], ["просто строка"])


# ==========================================================================
# Ремонт 5: кривой элемент снапшота не хоронит весь ответ
# ==========================================================================
class TestBrokenSnapshotItems(unittest.TestCase):

    def test_broken_elements_are_dropped_one_by_one(self):
        raw = [
            {"n": 1, "calendar_id": CAL, "event_id": "inst-ok", "title": "Норм",
             "start_at": FIXED_ISO},
            {"n": 2, "calendar_id": CAL, "title": "Без event_id", "start_at": FIXED_ISO},
            {"n": 3, "event_id": "inst-x", "title": "Без календаря", "start_at": FIXED_ISO},
            {"n": True, "calendar_id": CAL, "event_id": "inst-b", "title": "Булево"},
            "просто строка",
            {"n": 5, "calendar_id": CAL, "event_id": "inst-t", "title": "Число вместо старта",
             "start_at": 1754470800},
        ]
        good, dropped = mar.sanitize_snapshot_items(raw)
        self.assertEqual([it["n"] for it in good], [1, 5],
                         "битые элементы не отсеяны или отсеяны исправные")
        self.assertTrue(any("event_id" in d for d in dropped))
        self.assertTrue(any("calendar_id" in d for d in dropped))
        self.assertTrue(any("start_at" in d for d in dropped))
        self.assertEqual(good[1]["start_at"], "", "нестроковый старт уехал дальше как есть")

    def test_missing_title_gets_placeholder(self):
        good, _dropped = mar.sanitize_snapshot_items(
            [{"n": 1, "calendar_id": CAL, "event_id": "inst-a", "start_at": FIXED_ISO}])
        self.assertEqual(good[0]["title"], "(без названия)")

    def test_apply_survives_broken_element_and_applies_the_rest(self):
        items = [
            {"n": 1, "calendar_id": CAL, "event_id": "inst-a", "recurring_event_id": None,
             "title": "Исправная", "start_at": FIXED_ISO, "has_url_in_event": True},
            {"n": 2, "calendar_id": CAL, "title": "Без event_id",
             "start_at": FIXED_ISO, "has_url_in_event": True},
        ]
        # «Сегодня» закреплено вечером накануне встречи FIXED_ISO: без него ретеншн считал
        # от настоящей даты и с 06.09.2026 стирал только что записанный отказ (встреча
        # старше 30 дней) — тест краснел от календаря, а не от кода.
        # Оба порядка решений: при одном «исправное до битого» обрыв цикла на битом
        # (`continue` → `break`) проходил незамеченным — исправное успевало примениться раньше
        # (мутация выжила на проверке возобновления 05.10).
        for order in ((1, 2), (2, 1)):
            with self.subTest(порядок=order):
                result, data, saved = _apply(
                    [{"n": n, "action": "reject", "room": None} for n in order], items,
                    today=date(2026, 8, 5))
                self.assertEqual(result["rejected"], 1, "исправное решение владельца потеряно")
                self.assertEqual(len(data["watched"]), 1)
                self.assertEqual(len(saved), 1)
                self.assertTrue(any(s.startswith("2:") for s in result["skipped"]))

    def test_garbage_start_at_is_not_written_into_registry(self):
        items = [{"n": 1, "calendar_id": CAL, "event_id": "inst-a",
                  "recurring_event_id": None, "title": "Кривая дата",
                  "start_at": "06.08.2026 13:00", "has_url_in_event": True}]
        _result, data, _saved = _apply([{"n": 1, "action": "reject", "room": None}], items)
        rec = data["watched"][0]
        self.assertNotIn("rejected_start_at", rec,
                         "нечитаемая дата уехала в реестр — ретеншн её не прочитает")
        self.assertTrue(rec["rejected_at"])

    def test_number_missing_from_snapshot_is_reported(self):
        items = [{"n": 1, "calendar_id": CAL, "event_id": "inst-a",
                  "recurring_event_id": None, "title": "Одна",
                  "start_at": FIXED_ISO, "has_url_in_event": True}]
        result, _data, _saved = _apply([{"n": 9, "action": "reject", "room": None}], items)
        self.assertTrue(any(s.startswith("9:") for s in result["skipped"]))


# ==========================================================================
# Ремонт 6 и 7: дубли номеров и `n: true`
# ==========================================================================
class TestDuplicateNumbers(_EveningCase):

    def test_last_decision_wins_and_owner_is_told(self):
        self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        result, data, _saved = _apply([
            {"n": 1, "action": "reject", "room": None},
            {"n": 1, "action": "accept_once", "room": None},
        ], items)
        self.assertEqual(len(data["watched"]), 1)
        self.assertIs(data["watched"][0]["enabled"], True, "победило не последнее решение")
        per_number = [ln for ln in result["summary_lines"] if ln.startswith("1. ")]
        self.assertEqual(
            len(per_number), 1,
            f"владельцу ушли взаимоисключающие строки про одну встречу: {per_number}",
        )
        self.assertTrue(
            any("несколько разных решений" in ln for ln in result["summary_lines"]),
            "владельцу не сказали, что в ответе было два решения по одному номеру",
        )

    def test_identical_duplicates_are_collapsed_silently(self):
        self.run_block(events=[self.evt_a])
        items = self.snapshot_items()
        result, data, _saved = _apply([
            {"n": 1, "action": "reject", "room": None},
            {"n": 1, "action": "reject", "room": None},
        ], items)
        self.assertEqual(len(data["watched"]), 1)
        self.assertEqual(len(result["summary_lines"]), 1)
        self.assertEqual(result["rejected"], 1)


class TestParseGuards(unittest.TestCase):

    def test_bool_is_not_a_number(self):
        out = mar.parse_claude_output('[{"n": true, "action": "ignore_forever"}]', {1, 2})
        self.assertEqual(out, [], "n=true проехал как номер 1")

    def test_normal_decision_still_parses(self):
        out = mar.parse_claude_output('[{"n": 1, "action": "reject", "room": null}]', {1, 2})
        self.assertEqual(out, [{"n": 1, "action": "reject", "room": None}])

    def test_no_answer_is_a_valid_action(self):
        out = mar.parse_claude_output('[{"n": 2, "action": "no_answer"}]', {1, 2})
        self.assertEqual(out, [{"n": 2, "action": "no_answer", "room": None}])


# ==========================================================================
# Ремонт 8: обе двери читают TZ одинаково и одинаково переживают мусор
# ==========================================================================
class TestDisplayTzSymmetry(unittest.TestCase):

    def test_both_doors_agree_on_any_env_value(self):
        for value in ("", "   ", "Mars/Olympus", "Europe/Moscow", "UTC"):
            with self.subTest(tz=value), \
                    mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": value}):
                self.assertEqual(
                    str(mar.display_tz()), str(meb.resolve_display_tz()),
                    f"apply-reply и вечерний блок разошлись на TZ={value!r}",
                )

    def test_defaults_and_env_name_match(self):
        self.assertEqual(mar.DEFAULT_DISPLAY_TZ, meb.DEFAULT_DISPLAY_TZ)
        self.assertEqual(mar.DISPLAY_TZ_ENV, meb.DISPLAY_TZ_ENV)

    def test_evening_block_survives_garbage_and_empty_env(self):
        # Раньше мусор в переменной ронял ВЕСЬ вечерний блок на импорте:
        # владелец не получал ни списка встреч, ни ошибки.
        try:
            for value in ("Mars/Olympus", "", "  "):
                with self.subTest(tz=value), \
                        mock.patch.dict(os.environ, {"MEETING_NOTARY_DISPLAY_TZ": value}):
                    importlib.reload(meb)
                    self.assertEqual(str(meb.DISPLAY_TZ), "Asia/Dubai")
        finally:
            importlib.reload(meb)

    def test_state_dir_follows_env_like_neighbours(self):
        try:
            with mock.patch.dict(os.environ, {"MEETING_NOTARY_STATE_DIR": "/tmp/notary-state-test"}):
                importlib.reload(mar)
                self.assertEqual(str(mar.STATE_DIR), "/tmp/notary-state-test")
        finally:
            importlib.reload(mar)


# ==========================================================================
# Ремонт 11: календарь — половина ключа индекса
# ==========================================================================
class TestCalendarIdIsHalfTheKey(_EveningCase):

    def test_record_from_another_calendar_does_not_suppress_meeting(self):
        anchor = {"id": "anchor", "series": "anchor", "type": "google-calendar",
                  "calendar_id": CAL, "event_id": "some-other-event",
                  "enabled": True, "ignored": False}
        alien = _rejection_record("oneoff-alien", date.today(),
                                  calendar_id="other-calendar", event_id="inst-a")
        res = self.run_block(watched_records=[anchor, alien], events={CAL: [self.evt_a]})
        proposed = {it["event_id"] for it in res["items"]}
        self.assertIn(
            "inst-a", proposed,
            "запись из ЧУЖОГО календаря погасила встречу — календарь выпал из ключа индекса",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
