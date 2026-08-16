"""Ф10d: каждый получает свой протокол своим маршрутом, и правки работают.

Что здесь доказывается (и почему именно так):

* `TestTwoEmployeesTwoCompanies` — сценарий критерия. Две встречи, два разных
  сотрудника, две РАЗНЫЕ компании; каждый получает протокол своим маршрутом, и
  ни один не получает чужой. Проверяется не «функция вернула словарь», а КУДА
  ушёл документ: доставка прогоняется целиком, адресаты собираются списком.
* `TestCompanyNeverLost` — компания едет от наряда до сборки. Мутация «потеряй
  компанию в одном звене» обязана красить сьют: именно она отделяет «поле
  есть» от «поле работает».
* `TestNoOwnerFallbackForOrders` — 🔴 главное правило фазы: у наряда НЕТ фолбэка
  в личку владельца портала. Адресата не вывели → только портал, а не «хоть
  кому-нибудь».
* `TestClarifyGoesToPerson` — уточнения по спикерам несут кусок расшифровки и
  уходят в личку владельца НАРЯДА, никогда в группу и никогда Илье по чужой
  встрече.
* `TestEditWindows` — окна 20 мин / 2 ч сохранены и для правки с портала: она
  кладётся в ТО ЖЕ окно, что реплай, а не заводит своё.
* `TestEditAfterMonth` (R6.7) — правка встречи месячной давности проходит, и
  через портал давность не участвует вовсе.
* `TestQuotePolicy` (R13.2) — враждебная формулировка «добавь дословно, кто что
  сказал по этому спору» от не-организатора отбивается С ОБЪЯСНЕНИЕМ, та же
  правка от организатора проходит; обычные правки участников не задеты.
* `TestOneVersionFeed` (R6.3) — правка из Telegram и правка с портала попадают
  в одну ленту: один файл окна, одна следующая версия, источник у каждой
  правки свой.
* `TestInvalidInputs` — невалидные входы каждого перехода списком.
* `TestPortalOff` — 🔴 неизменность поведения владельца: при `OFF` маршрут
  вырождается в сегодняшний, диск нарядов не читается.
* `TestCalendarReschedule` — перенос события Google-календаря (не создание):
  ключ старта нормализован в UTC, перенос даёт НОВЫЙ старт, повторный тик по
  тому же — нет.
* `TestLazyRegistryPaths` (ФУ-П1) — пути реестра резолвятся лениво: проба,
  выставившая каталог ПОСЛЕ импорта, видит свой реестр, а не боевой.
* `TestScale` — реальные объёмы: серия на много встреч, длинный протокол,
  очередь с несколькими нарядами разом.

🔴 Изоляция: временный каталог реестра, временный каталог правок, ни одного
обращения в Telegram и ни одной записи в боевой реестр владельца.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))
sys.path.insert(0, str(_NOTARY.parent))

import notary.cli.registry as registry_mod  # noqa: E402
from notary.lib import delivery_route, identity, orders as orders_lib  # noqa: E402
from notary.lib import feedback_state, feedback_worker, quote_policy  # noqa: E402
from notary.lib import series_markup  # noqa: E402

FLAG = identity.ENABLE_ENV
ROOM_A = "https://telemost.yandex.ru/j/11111111111111"
ROOM_B = "https://telemost.yandex.ru/j/22222222222222"

OWNER_CHAT = 100500          # личка Ильи (TELEGRAM_NOTARIUS_CHAT_ID)
PETROV_TG = 777001           # сотрудник Anzhee
SIDOROV_TG = 777002          # сотрудник МПервого
GROUP_ANZHEE = -1001111111111
GROUP_MPFIRST = -1002222222222

ISOLATED_ENV = (
    "MEETING_NOTARY_REGISTRY_DIR",
    "MEETING_NOTARY_FEEDBACK_DIR",
    "MEETING_NOTARY_DELIVERED_ROOTS",
    "MEETING_NOTARY_LOG_DIR",
    "TELEGRAM_NOTARIUS_CHAT_ID",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_NOTARIUS_BOT_TOKEN",
    "FEEDBACK_WINDOW_MIN",
    "FEEDBACK_MAX_WINDOW_MIN",
    "ENABLE_FEEDBACK_EDITS",
    quote_policy.ENABLE_ENV,
    FLAG,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class _Base(unittest.TestCase):
    """Временный реестр, временные правки, выключатель ON, без Telegram."""

    flag_default: Optional[str] = "1"

    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ISOLATED_ENV}
        self.tmp = Path(tempfile.mkdtemp(prefix="ph10d-"))
        self.registry = self.tmp / "registry"
        self.protocols = self.tmp / "protocols"
        self.feedback = self.tmp / "feedback"
        for d in (self.registry, self.protocols, self.feedback):
            d.mkdir(parents=True, exist_ok=True)
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = str(self.registry)
        os.environ["MEETING_NOTARY_FEEDBACK_DIR"] = str(self.feedback)
        os.environ["MEETING_NOTARY_DELIVERED_ROOTS"] = str(self.protocols)
        os.environ["MEETING_NOTARY_LOG_DIR"] = str(self.tmp / "logs")
        os.environ["TELEGRAM_NOTARIUS_CHAT_ID"] = str(OWNER_CHAT)
        os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = "test-token"
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        os.environ.pop("FEEDBACK_WINDOW_MIN", None)
        os.environ.pop("FEEDBACK_MAX_WINDOW_MIN", None)
        os.environ.pop("ENABLE_FEEDBACK_EDITS", None)
        os.environ.pop(quote_policy.ENABLE_ENV, None)
        self.set_flag(self.flag_default)
        feedback_worker._INDEX_CACHE.update(built_at=0.0, roots=None, index={})

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        identity.reset_cache()
        feedback_worker._INDEX_CACHE.update(built_at=0.0, roots=None, index={})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_flag(self, value: Optional[str]) -> None:
        if value is None:
            os.environ.pop(FLAG, None)
        else:
            os.environ[FLAG] = value
        identity.reset_cache()

    # ---- фикстуры ----

    def put_order(
        self,
        *,
        meeting_id: str,
        company: str,
        room: str,
        owner_tg: Optional[int],
        portal_user_id: str,
        delivery: Optional[str] = None,
        chat_id: Optional[int] = None,
        series: str = "koordinatsiya",
        start: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        start = start or (datetime.now(timezone.utc) + timedelta(hours=1))
        owner: Dict[str, Any] = {"portal_user_id": portal_user_id, "name": "Сотрудник"}
        if owner_tg is not None:
            owner["telegram_user_id"] = owner_tg
        payload: Dict[str, Any] = {
            "meeting_id": meeting_id,
            "company": company,
            "series_title": "Координация",
            "series": series,
            "room_url": room,
            "start_at": _iso(start),
            "owner": owner,
        }
        if delivery is not None:
            payload["delivery"] = delivery
        if chat_id is not None:
            payload["telegram_chat_id"] = chat_id
        clean, errors = orders_lib.validate_order_payload(payload)
        self.assertEqual(errors, [], "наряд не прошёл валидацию: %s" % errors)
        order, _ = orders_lib.put_order(clean)
        return order

    def meta_for(self, order: Dict[str, Any], *, sid: str, date: str = "2026-08-20") -> Dict[str, Any]:
        """Meta встречи ПОСЛЕ штампа (так её видит весь конвейер после Ф10d)."""
        meta = {
            "sessionUid": sid,
            "series": order.get("series"),
            "date": date,
            "meetingUrl": order.get("room_url"),
            "startTs": order.get("start_at"),
            "files": {"wav": str(self.tmp / f"{sid}.wav")},
        }
        delivery_route.stamp_meta(meta, session_uid=sid)
        return meta

    def write_delivered_meta(
        self,
        *,
        series: str,
        date: str,
        chat_id: Optional[int],
        message_id: int = 501,
        sid: str = "sid-1",
        order: Optional[Dict[str, Any]] = None,
        age_days: int = 0,
    ) -> Path:
        """meta.json доставленной встречи — вход для поиска правок."""
        d = self.protocols / series
        d.mkdir(parents=True, exist_ok=True)
        meta: Dict[str, Any] = {
            "series": series,
            "date": date,
            "sessionUid": sid,
            "startTs": f"{date}T10:00:00+03:00",
            "expectedParticipants": ["Пётр Петров", "Ольга Иванова"],
        }
        if chat_id is not None:
            meta["delivered"] = [{"chat_id": chat_id, "message_ids": [message_id]}]
        if order is not None:
            meta["order"] = order
            meta["company"] = order.get("company")
        path = d / "meta.json"
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if age_days:
            old = datetime.now(timezone.utc) - timedelta(days=age_days)
            os.utime(path, (old.timestamp(), old.timestamp()))
        return path


# ---------------------------------------------------------------------------


class TestTwoEmployeesTwoCompanies(_Base):
    """Критерий фазы: два сотрудника, две компании, каждый получает СВОЁ."""

    def _deliver(self, meta: Dict[str, Any], sent: List[tuple]) -> Dict[str, Any]:
        """Прогон настоящей `deliver_protocol` с перехватом отправки."""
        from notary.lib import llm_postprocess as lp

        with mock.patch.object(lp, "_send_protocol_pdf", create=True) as pdf:
            pdf.return_value = {"ok": True, "message_id": 900}
            with mock.patch.object(lp, "telegram_api") as tg:
                tg.send_document.side_effect = lambda token, chat_id, *a, **kw: (
                    sent.append(("doc", chat_id)) or {"message_id": 900}
                )
                tg.send_message.side_effect = lambda token, chat_id, *a, **kw: (
                    sent.append(("msg", chat_id)) or {"message_id": 901}
                )
                return lp.deliver_protocol(
                    meeting_meta=meta,
                    protocol_text="# Протокол\n\nо чём договорились",
                    meeting_sid=meta.get("sessionUid"),
                )

    def test_each_employee_gets_his_own_route(self):
        petrov = self.put_order(
            meeting_id="mtg-a", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", delivery="dm",
        )
        sidorov = self.put_order(
            meeting_id="mtg-b", company="mpfirst", room=ROOM_B,
            owner_tg=SIDOROV_TG, portal_user_id="43", delivery="group",
            chat_id=GROUP_MPFIRST, series="mpfirst-sync",
        )
        route_a = delivery_route.resolve(petrov)
        route_b = delivery_route.resolve(sidorov)

        self.assertEqual(route_a.mode, delivery_route.MODE_DM)
        self.assertEqual(route_a.chat_id, PETROV_TG)
        self.assertEqual(route_a.company, "anzhee")

        self.assertEqual(route_b.mode, delivery_route.MODE_GROUP)
        self.assertEqual(route_b.chat_id, GROUP_MPFIRST)
        self.assertEqual(route_b.company, "mpfirst")

        # Ни один не получил адрес другого и ни один не получил личку Ильи.
        self.assertNotEqual(route_a.chat_id, route_b.chat_id)
        for r in (route_a, route_b):
            self.assertNotEqual(r.chat_id, OWNER_CHAT)

    def test_delivery_actually_sends_to_the_right_chat(self):
        """Не «резолвер вернул число», а КУДА ушёл документ."""
        petrov = self.put_order(
            meeting_id="mtg-a", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", delivery="dm",
        )
        sidorov = self.put_order(
            meeting_id="mtg-b", company="mpfirst", room=ROOM_B,
            owner_tg=SIDOROV_TG, portal_user_id="43", delivery="group",
            chat_id=GROUP_MPFIRST, series="mpfirst-sync",
        )
        sent: List[tuple] = []
        res_a = self._deliver(self.meta_for(petrov, sid="sid-a"), sent)
        res_b = self._deliver(self.meta_for(sidorov, sid="sid-b"), sent)

        self.assertEqual(res_a.get("chat_id"), PETROV_TG, res_a)
        self.assertEqual(res_b.get("chat_id"), GROUP_MPFIRST, res_b)
        addressees = {chat for _, chat in sent}
        self.assertNotIn(OWNER_CHAT, addressees,
                         "протокол сотрудника ушёл владельцу портала")

    def test_owner_personal_series_unchanged(self):
        """Личная серия владельца: наряда нет — поведение прежнее."""
        meta = {"series": "lichnaya", "date": "2026-08-20", "sessionUid": "sid-own"}
        route = delivery_route.route_for_meeting(meta)
        self.assertTrue(route.is_owner_legacy)
        self.assertIsNone(route.chat_id)
        # И уточнения по-прежнему идут в личку из окружения.
        self.assertEqual(delivery_route.clarify_chat_for_meeting(meta), OWNER_CHAT)


class TestCompanyNeverLost(_Base):
    """Компания едет от наряда до сборки протокола и не переугадывается."""

    def test_order_company_beats_slug_guess(self):
        order = self.put_order(
            meeting_id="mtg-b", company="mpfirst", room=ROOM_B,
            owner_tg=SIDOROV_TG, portal_user_id="43", series="koordinatsiya",
        )
        meta = self.meta_for(order, sid="sid-b")
        self.assertEqual(meta.get("company"), "mpfirst")

        # Даже если резолвер по slug'у уверенно говорит «anzhee» — компания
        # наряда сильнее. Ровно этот случай и есть «протокол МПервый собран на
        # знании Anzhee»: slug серии нашёлся в чужой оргструктуре.
        with mock.patch.object(series_markup, "company_for_series", return_value="anzhee"):
            self.assertEqual(series_markup.company_for_meeting(meta), "mpfirst")

    def test_without_order_company_is_resolved_as_before(self):
        meta = {"series": "koordinatsiya", "date": "2026-08-20"}
        with mock.patch.object(series_markup, "company_for_series", return_value="anzhee") as f:
            self.assertEqual(series_markup.company_for_meeting(meta), "anzhee")
            f.assert_called_once()

    def test_garbage_company_in_meta_is_ignored(self):
        """Мусор в поле не имеет права стать путём к чужому каталогу знаний."""
        meta = {"series": "koordinatsiya", "company": "../../etc"}
        with mock.patch.object(series_markup, "company_for_series", return_value="anzhee"):
            self.assertEqual(series_markup.company_for_meeting(meta), "anzhee")

    def test_durable_correction_lock_prefers_the_order_company(self):
        """Замок выученных правок — company-scoped и DURABLE (живёт на всех
        будущих встречах компании). Ошибка компании здесь подмешивает чужой
        канон имён навсегда, поэтому проверяем именно ЭТОТ блок, а не файл
        целиком: один законный вызов резолвера ниже освободил бы весь файл.
        """
        source = (_NOTARY / "lib" / "feedback_reissue.py").read_text(encoding="utf-8")
        anchor = source.find("DURABLE company-замок")
        self.assertNotEqual(anchor, -1, "блок durable-замка не найден")
        block = source[anchor:anchor + 1400]
        meta_at = block.find('(meta or {}).get("company")')
        slug_at = block.find("context_knowledge.company_for_series(series)")
        self.assertNotEqual(meta_at, -1, "замок не смотрит компанию наряда из meta")
        self.assertNotEqual(slug_at, -1, "фолбэк по серии пропал — сломали личные серии")
        self.assertLess(meta_at, slug_at, "компания наряда обязана идти ПЕРВОЙ")

    def test_order_without_company_gets_no_telegram_target(self):
        """Наряд без компании адресата не получает вовсе (fail-closed)."""
        route = delivery_route.resolve(
            {"id": "portal:x", "telegram_chat_id": GROUP_ANZHEE,
             "owner": {"portal_user_id": "42", "telegram_user_id": PETROV_TG}}
        )
        self.assertTrue(route.portal_only)
        self.assertEqual(route.reason, delivery_route.REASON_NO_COMPANY)
        self.assertIsNone(route.chat_id)

    def test_series_chat_of_another_company_is_not_a_target(self):
        """Серия размечена чужой компанией → её групповой чат адресатом не станет."""
        order = self.put_order(
            meeting_id="mtg-b", company="mpfirst", room=ROOM_B,
            owner_tg=None, portal_user_id="43", series="anzhee-koord",
        )
        watched = {"watched": [{
            "series": "anzhee-koord", "company": "anzhee",
            "telegram_chat_id": GROUP_ANZHEE,
        }]}
        route = delivery_route.resolve(order, watched=watched)
        self.assertTrue(route.portal_only, route)
        self.assertNotEqual(route.chat_id, GROUP_ANZHEE)


class TestNoOwnerFallbackForOrders(_Base):
    """🔴 У наряда нет фолбэка в личку владельца портала."""

    def test_no_targets_means_portal_only(self):
        order = self.put_order(
            meeting_id="mtg-c", company="anzhee", room=ROOM_A,
            owner_tg=None, portal_user_id="44",
        )
        route = delivery_route.resolve(order, watched={"watched": []})
        self.assertTrue(route.portal_only)
        self.assertEqual(route.reason, delivery_route.REASON_NO_TARGET)

    def test_dm_requested_without_binding_degrades_not_leaks(self):
        order = self.put_order(
            meeting_id="mtg-d", company="anzhee", room=ROOM_A,
            owner_tg=None, portal_user_id="44", delivery="dm",
        )
        route = delivery_route.resolve(order, watched={"watched": []})
        self.assertTrue(route.portal_only)
        self.assertEqual(route.reason, delivery_route.REASON_DM_UNKNOWN)
        self.assertIsNone(route.chat_id)

    def test_group_requested_without_binding_degrades(self):
        order = self.put_order(
            meeting_id="mtg-e", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="44", delivery="group",
        )
        route = delivery_route.resolve(order, watched={"watched": []})
        self.assertTrue(route.portal_only)
        self.assertEqual(route.reason, delivery_route.REASON_GROUP_UNKNOWN)

    def test_deliver_protocol_returns_portal_only_and_sends_nothing(self):
        order = self.put_order(
            meeting_id="mtg-f", company="anzhee", room=ROOM_A,
            owner_tg=None, portal_user_id="44", delivery="portal_only",
        )
        from notary.lib import llm_postprocess as lp

        sent: List[Any] = []
        with mock.patch.object(lp, "telegram_api") as tg:
            tg.send_document.side_effect = lambda *a, **kw: sent.append(a)
            tg.send_message.side_effect = lambda *a, **kw: sent.append(a)
            res = lp.deliver_protocol(
                meeting_meta=self.meta_for(order, sid="sid-f"),
                protocol_text="# Протокол\n\nтело",
                meeting_sid="sid-f",
            )
        self.assertEqual(res.get("status"), "portal-only", res)
        self.assertIsNone(res.get("chat_id"))
        self.assertEqual(sent, [], "при «только портал» в Telegram ушло сообщение")

    def test_portal_only_is_a_finished_delivery_for_wav_cleanup(self):
        """Читатель статуса: `portal-only` не должен держать WAV вечно."""
        source = (_NOTARY / "collector.py").read_text(encoding="utf-8")
        i = source.find("success_statuses = {")
        self.assertNotEqual(i, -1)
        self.assertIn("portal-only", source[i:i + 200])


class TestClarifyGoesToPerson(_Base):
    """Уточнения по спикерам несут расшифровку — только в личку, только своему."""

    def test_group_route_still_asks_in_dm(self):
        order = self.put_order(
            meeting_id="mtg-g", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", delivery="group",
            chat_id=GROUP_ANZHEE,
        )
        meta = self.meta_for(order, sid="sid-g")
        route = delivery_route.route_for_meeting(meta)
        self.assertEqual(route.chat_id, GROUP_ANZHEE)
        self.assertEqual(route.clarify_chat_id, PETROV_TG)
        self.assertEqual(delivery_route.clarify_chat_for_meeting(meta), PETROV_TG)

    def test_no_dm_means_no_question_not_owner_dm(self):
        order = self.put_order(
            meeting_id="mtg-h", company="anzhee", room=ROOM_A,
            owner_tg=None, portal_user_id="42", delivery="group",
            chat_id=GROUP_ANZHEE,
        )
        meta = self.meta_for(order, sid="sid-h")
        self.assertIsNone(delivery_route.clarify_chat_for_meeting(meta))

    def test_owner_meeting_asks_in_env_chat_as_before(self):
        meta = {"series": "lichnaya", "date": "2026-08-20"}
        self.assertEqual(delivery_route.clarify_chat_for_meeting(meta), OWNER_CHAT)


class TestEditWindows(_Base):
    """Окна 20 мин / 2 ч сохранены; правка с портала кладётся в ТО ЖЕ окно."""

    def setUp(self) -> None:
        super().setUp()
        self.order = self.put_order(
            meeting_id="mtg-w", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", delivery="group",
            chat_id=GROUP_ANZHEE, series="koordinatsiya",
        )
        self.write_delivered_meta(
            series="koordinatsiya", date="2026-08-20", chat_id=GROUP_ANZHEE,
            sid="sid-w", order=self.order,
        )

    def _portal_edit(self, text: str, *, user: str = "42", now=None, edit_id=None):
        return feedback_worker.apply_portal_edit(
            meeting_id="mtg-w", text=text, author="Ольга Иванова",
            portal_user_id=user, session_uid="sid-w",
            edit_id=edit_id, root=self.feedback, now=now,
            roots=[self.protocols],
        )

    def test_default_windows_are_20_and_120(self):
        summary = self._portal_edit("по поставке отвечает Ольга")
        self.assertEqual(summary["window_min"], 20)
        self.assertEqual(summary["max_window_min"], 120)
        self.assertEqual(summary["status"], "collecting")

    def test_second_edit_resets_debounce_but_not_the_ceiling(self):
        t0 = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        self._portal_edit("первая", now=t0, edit_id="p1")
        s2 = self._portal_edit("вторая", now=t0 + timedelta(minutes=15), edit_id="p2")
        # дебаунс сдвинулся на 20 минут от ВТОРОЙ правки
        self.assertEqual(s2["deadline_at"], "2026-08-20T12:35:00Z")
        # потолок по-прежнему от ПЕРВОЙ
        self.assertEqual(s2["hard_deadline_at"], "2026-08-20T14:00:00Z")

    def test_ceiling_wins_over_debounce(self):
        t0 = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        self._portal_edit("первая", now=t0, edit_id="p1")
        s = self._portal_edit("поздняя", now=t0 + timedelta(minutes=115), edit_id="p2")
        self.assertEqual(s["hard_deadline_at"], "2026-08-20T14:00:00Z")
        self.assertEqual(s["deadline_at"], "2026-08-20T14:00:00Z")

    def test_repeat_of_same_edit_id_does_not_move_the_window(self):
        t0 = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        first = self._portal_edit("правка", now=t0, edit_id="p1")
        again = self._portal_edit("правка", now=t0 + timedelta(minutes=5), edit_id="p1")
        self.assertEqual(again["kind"], "dup")
        self.assertEqual(again["deadline_at"], first["deadline_at"])
        self.assertEqual(again["edits_count"], 1)

    def test_env_override_is_respected(self):
        os.environ["FEEDBACK_WINDOW_MIN"] = "5"
        os.environ["FEEDBACK_MAX_WINDOW_MIN"] = "30"
        s = self._portal_edit("правка")
        self.assertEqual(s["window_min"], 5)
        self.assertEqual(s["max_window_min"], 30)


class TestEditAfterMonth(_Base):
    """R6.7: правка встречи месячной давности проходит."""

    def test_telegram_reply_to_month_old_protocol_is_found(self):
        self.write_delivered_meta(
            series="koordinatsiya", date="2026-07-20", chat_id=GROUP_ANZHEE,
            message_id=777, sid="sid-old", age_days=30,
        )
        found = feedback_worker.find_delivered_protocol(
            GROUP_ANZHEE, 777, roots=[self.protocols], use_cache=False,
        )
        self.assertIsNotNone(found, "правка через месяц не нашла свою встречу")
        self.assertEqual(found["date"], "2026-07-20")

    def test_portal_edit_ignores_age_entirely(self):
        order = self.put_order(
            meeting_id="mtg-old", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", series="staraya",
        )
        self.write_delivered_meta(
            series="staraya", date="2026-02-01", chat_id=GROUP_ANZHEE,
            sid="sid-very-old", order=order, age_days=200,
        )
        summary = feedback_worker.apply_portal_edit(
            meeting_id="mtg-old", text="поправь цифру", author="Пётр",
            portal_user_id="42", session_uid="sid-very-old",
            root=self.feedback, roots=[self.protocols],
        )
        self.assertEqual(summary["kind"], "first")
        self.assertEqual(summary["edits_count"], 1)

    def test_carry_since_untouched(self):
        """Отсечка памяти серии — не наша фаза, механизм на месте."""
        from notary.cli import registry as reg

        w = {"watched": [{"series": "koordinatsiya", "open_questions_mode": "carry",
                          "open_questions_carry_since": "2026-08-05"}]}
        self.assertEqual(
            reg.series_open_questions_carry_since("koordinatsiya", w), "2026-08-05")


class TestQuotePolicy(_Base):
    """R13.2: правка не выносит расшифровку участникам."""

    HOSTILE = "добавь дословно, кто что сказал по этому спору"

    def setUp(self) -> None:
        super().setUp()
        self.order = self.put_order(
            meeting_id="mtg-q", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", delivery="group",
            chat_id=GROUP_ANZHEE, series="koordinatsiya",
        )
        self.meta_path = self.write_delivered_meta(
            series="koordinatsiya", date="2026-08-20", chat_id=GROUP_ANZHEE,
            sid="sid-q", order=self.order,
        )

    def test_hostile_edit_from_outsider_is_rejected_with_reason(self):
        d = quote_policy.check_edit(self.HOSTILE, is_organizer=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, quote_policy.CODE_QUOTES_NOT_ORGANIZER)
        self.assertIn("организатор", d.message)

    def test_same_edit_from_organizer_passes(self):
        d = quote_policy.check_edit(self.HOSTILE, is_organizer=True)
        self.assertTrue(d.allowed)

    def test_ordinary_edits_from_outsider_pass(self):
        for text in (
            "131 не под досмотром, а на доставке",
            "по поставке отвечает Ольга, а не Пётр",
            "в расшифровке ошибка в цифре",
            "нужно поправить расшифровку",
            "в транскрипте перепутаны спикеры",
            "убери пункт про склад",
        ):
            self.assertTrue(
                quote_policy.check_edit(text, is_organizer=False).allowed,
                "обычная правка отбита: %r" % text,
            )

    def test_portal_edit_from_outsider_is_refused(self):
        with self.assertRaises(feedback_worker.PortalEditError) as ctx:
            feedback_worker.apply_portal_edit(
                meeting_id="mtg-q", text=self.HOSTILE, author="Ольга",
                portal_user_id="99", session_uid="sid-q",
                root=self.feedback, roots=[self.protocols],
            )
        self.assertEqual(ctx.exception.code, quote_policy.CODE_QUOTES_NOT_ORGANIZER)
        self.assertEqual(feedback_state.list_states(root=self.feedback), [],
                         "отклонённая правка всё равно записалась в окно")

    def test_portal_edit_from_organizer_is_accepted(self):
        summary = feedback_worker.apply_portal_edit(
            meeting_id="mtg-q", text=self.HOSTILE, author="Пётр",
            portal_user_id="42", session_uid="sid-q",
            root=self.feedback, roots=[self.protocols],
        )
        self.assertEqual(summary["edits_count"], 1)

    def test_telegram_reply_from_outsider_is_refused_with_explanation(self):
        sent: List[tuple] = []
        meeting = feedback_worker.find_delivered_protocol(
            GROUP_ANZHEE, 501, roots=[self.protocols], use_cache=False,
        )
        self.assertIsNotNone(meeting)
        msg = {
            "message_id": 9001, "text": self.HOSTILE,
            "from": {"id": 555999, "first_name": "Посторонний"},
            "reply_to_message": {"message_id": 501},
        }
        feedback_worker.handle_feedback_reply(
            "token", GROUP_ANZHEE, msg, meeting=meeting, root=self.feedback,
            send=lambda t, c, text, **kw: sent.append((c, text)),
        )
        self.assertEqual(len(sent), 1)
        self.assertIn("организатор", sent[0][1])
        self.assertEqual(feedback_state.list_states(root=self.feedback), [])

    def test_telegram_reply_from_organizer_is_accepted(self):
        meeting = feedback_worker.find_delivered_protocol(
            GROUP_ANZHEE, 501, roots=[self.protocols], use_cache=False,
        )
        msg = {
            "message_id": 9002, "text": self.HOSTILE,
            "from": {"id": PETROV_TG, "first_name": "Пётр"},
            "reply_to_message": {"message_id": 501},
        }
        state = feedback_worker.handle_feedback_reply(
            "token", GROUP_ANZHEE, msg, meeting=meeting, root=self.feedback,
            send=lambda *a, **kw: None,
        )
        self.assertEqual(len(state.get("edits") or []), 1)
        self.assertTrue(state["edits"][0]["is_organizer"])

    def test_owner_edits_are_never_gated_on_personal_series(self):
        """🔴 Неизменность: у личной серии владельца организатор — он."""
        self.write_delivered_meta(
            series="lichnaya", date="2026-08-20", chat_id=OWNER_CHAT,
            message_id=601, sid="sid-own",
        )
        meeting = feedback_worker.find_delivered_protocol(
            OWNER_CHAT, 601, roots=[self.protocols], use_cache=False,
        )
        msg = {
            "message_id": 9003, "text": self.HOSTILE,
            "from": {"id": OWNER_CHAT, "first_name": "Илья"},
            "reply_to_message": {"message_id": 601},
        }
        state = feedback_worker.handle_feedback_reply(
            "token", OWNER_CHAT, msg, meeting=meeting, root=self.feedback,
            send=lambda *a, **kw: None,
        )
        self.assertEqual(len(state.get("edits") or []), 1)

    def test_kill_switch_restores_previous_behaviour(self):
        os.environ[quote_policy.ENABLE_ENV] = "0"
        self.assertTrue(quote_policy.check_edit(self.HOSTILE, is_organizer=False).allowed)

    def test_assembly_gets_explicit_guard_when_round_has_outsider(self):
        from notary.lib import feedback_reissue

        own = [{"text": "поправь", "author": "Пётр", "is_organizer": True}]
        mixed = own + [{"text": "и это", "author": "Гость", "is_organizer": False}]
        self.assertNotIn(
            quote_policy.OUTSIDER_QUOTE_GUARD,
            feedback_reissue.build_edit_instruction(own))
        self.assertIn(
            quote_policy.OUTSIDER_QUOTE_GUARD,
            feedback_reissue.build_edit_instruction(mixed))

    def test_legacy_edits_without_flag_are_not_retroactively_outsiders(self):
        self.assertFalse(quote_policy.edits_have_outsider(
            [{"text": "старая правка", "author": "Илья"}]))


class TestOneVersionFeed(_Base):
    """R6.3: правка из Telegram и правка с портала — одна лента версий."""

    def setUp(self) -> None:
        super().setUp()
        self.order = self.put_order(
            meeting_id="mtg-v", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", delivery="group",
            chat_id=GROUP_ANZHEE, series="koordinatsiya",
        )
        self.write_delivered_meta(
            series="koordinatsiya", date="2026-08-20", chat_id=GROUP_ANZHEE,
            sid="sid-v", order=self.order,
        )

    def test_both_sources_land_in_one_window(self):
        meeting = feedback_worker.find_delivered_protocol(
            GROUP_ANZHEE, 501, roots=[self.protocols], use_cache=False,
        )
        feedback_worker.handle_feedback_reply(
            "token", GROUP_ANZHEE,
            {"message_id": 11, "text": "цифра не та",
             "from": {"id": PETROV_TG, "first_name": "Пётр"},
             "reply_to_message": {"message_id": 501}},
            meeting=meeting, root=self.feedback, send=lambda *a, **kw: None,
        )
        feedback_worker.apply_portal_edit(
            meeting_id="mtg-v", text="и фамилию поправь", author="Ольга Иванова",
            portal_user_id="42", session_uid="sid-v",
            root=self.feedback, roots=[self.protocols],
        )
        states = feedback_state.list_states(root=self.feedback)
        self.assertEqual(len(states), 1, "две правки развели по разным окнам")
        edits = states[0]["edits"]
        self.assertEqual(len(edits), 2)
        self.assertEqual(
            [feedback_worker.edit_source(e) for e in edits],
            [feedback_worker.SOURCE_TELEGRAM, feedback_worker.SOURCE_PORTAL],
        )
        # Автор реплая канонизируется по реестру участников встречи (FB2),
        # автор с портала приходит именем сотрудника — подписи РАЗНЫЕ.
        self.assertEqual(edits[0]["author"], "Пётр Петров")
        self.assertEqual(edits[1]["author"], "Ольга Иванова")
        self.assertIsNone(edits[0].get("portal_user_id"))
        self.assertEqual(edits[1]["portal_user_id"], "42")

    def test_payload_carries_source_and_author(self):
        from notary.lib import portal_ingest

        payload = portal_ingest.build_payload(
            order_id="portal:mtg-v", meeting_id="mtg-v", version=2,
            company="anzhee", session_uid="sid-v", revision_of=1,
            edits=[
                {"source": "telegram", "author": "Пётр", "at": "2026-08-20T12:00:00Z",
                 "is_organizer": True, "text": "СЕКРЕТНЫЙ ТЕКСТ ПРАВКИ"},
                {"source": "portal", "author": "Ольга", "portal_user_id": "43",
                 "at": "2026-08-20T12:05:00Z", "is_organizer": False},
            ],
        )
        self.assertEqual(payload["revision_of"], 1)
        self.assertEqual([e["source"] for e in payload["edits"]], ["telegram", "portal"])
        self.assertEqual(payload["edits"][1]["portal_user_id"], "43")
        # 🔴 Текст правки в ленту версий не уезжает.
        self.assertNotIn("СЕКРЕТНЫЙ", json.dumps(payload, ensure_ascii=False))

    def test_legacy_edit_without_source_reads_as_telegram(self):
        self.assertEqual(
            feedback_worker.edit_source({"text": "старая"}),
            feedback_worker.SOURCE_TELEGRAM)


class TestInvalidInputs(_Base):
    """Невалидные входы каждого перехода — списком."""

    def test_order_without_company_is_refused_by_gateway_validator(self):
        clean, errors = orders_lib.validate_order_payload({
            "meeting_id": "mtg-x", "series_title": "С", "room_url": ROOM_A,
            "start_at": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
            "owner": {"portal_user_id": "42"},
        })
        self.assertTrue(any(e["field"] == "company" for e in errors), errors)

    def test_order_without_owner_is_refused_when_flag_on(self):
        _, errors = orders_lib.validate_order_payload({
            "meeting_id": "mtg-x", "company": "anzhee", "series_title": "С",
            "room_url": ROOM_A,
            "start_at": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
        })
        self.assertTrue(any(e["field"] == "owner" for e in errors), errors)

    def test_unknown_delivery_mode_is_refused(self):
        _, errors = orders_lib.validate_order_payload({
            "meeting_id": "mtg-x", "company": "anzhee", "series_title": "С",
            "room_url": ROOM_A, "delivery": "почтой",
            "start_at": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
            "owner": {"portal_user_id": "42"},
        })
        self.assertTrue(any(e["field"] == "delivery" for e in errors), errors)

    def test_known_delivery_modes_are_accepted(self):
        for mode in delivery_route.REQUESTABLE_MODES:
            _, errors = orders_lib.validate_order_payload({
                "meeting_id": "mtg-x", "company": "anzhee", "series_title": "С",
                "room_url": ROOM_A, "delivery": mode,
                "start_at": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
                "owner": {"portal_user_id": "42"},
            })
            self.assertEqual([e for e in errors if e["field"] == "delivery"], [])

    def test_portal_edit_empty_text(self):
        with self.assertRaises(feedback_worker.PortalEditError) as ctx:
            feedback_worker.apply_portal_edit(
                meeting_id="mtg-x", text="   ", author="A", portal_user_id="42",
                root=self.feedback, roots=[self.protocols])
        self.assertEqual(ctx.exception.code, "empty")

    def test_portal_edit_unknown_meeting(self):
        with self.assertRaises(feedback_worker.PortalEditError) as ctx:
            feedback_worker.apply_portal_edit(
                meeting_id="mtg-never", text="правка", author="A",
                portal_user_id="42", root=self.feedback, roots=[self.protocols])
        self.assertEqual(ctx.exception.code, "meeting_not_found")

    def test_portal_edit_when_edits_disabled(self):
        os.environ["ENABLE_FEEDBACK_EDITS"] = "0"
        with self.assertRaises(feedback_worker.PortalEditError) as ctx:
            feedback_worker.apply_portal_edit(
                meeting_id="mtg-x", text="правка", author="A",
                portal_user_id="42", root=self.feedback, roots=[self.protocols])
        self.assertEqual(ctx.exception.code, "edits_disabled")

    def test_route_of_broken_order_object(self):
        for junk in (None, "", 0, [], {}, {"id": "x"}):
            route = delivery_route.resolve(junk)
            self.assertIn(route.mode, (delivery_route.MODE_OWNER,
                                       delivery_route.MODE_PORTAL_ONLY))

    def test_deleted_chat_does_not_change_the_route_decision(self):
        """Чат удалён/бот выгнан — это ошибка ОТПРАВКИ, а не выбора адресата.

        Маршрут обязан остаться прежним: иначе следующая встреча тихо уехала бы
        в другое место, и никто не узнал бы, что группа развалилась.
        """
        order = self.put_order(
            meeting_id="mtg-y", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", delivery="group",
            chat_id=GROUP_ANZHEE,
        )
        from notary.lib import llm_postprocess as lp

        with mock.patch.object(lp, "telegram_api") as tg:
            tg.TelegramApiError = RuntimeError
            tg.send_document.side_effect = RuntimeError("chat not found")
            res = lp.deliver_protocol(
                meeting_meta=self.meta_for(order, sid="sid-y"),
                protocol_text="# П\n\nтело", meeting_sid="sid-y",
            )
        self.assertNotEqual(res.get("status"), "portal-only")
        self.assertEqual(delivery_route.resolve(order).chat_id, GROUP_ANZHEE)


class TestPortalOff(_Base):
    """🔴 `ENABLE_PORTAL_ORDERS=OFF` возвращает сегодняшнее поведение целиком."""

    flag_default = None

    def test_route_is_owner_legacy_even_with_stamped_meta(self):
        stamped = {"id": "portal:x", "company": "mpfirst",
                   "owner": {"portal_user_id": "43", "telegram_user_id": SIDOROV_TG}}
        route = delivery_route.route_for_meeting({"order": stamped, "series": "s"})
        self.assertTrue(route.is_owner_legacy)

    def test_no_disk_access_when_off(self):
        calls: List[Any] = []
        with mock.patch.object(orders_lib, "find_order_for_recording",
                               side_effect=lambda **kw: calls.append(kw)):
            delivery_route.route_for_meeting({"series": "s", "sessionUid": "sid"})
            delivery_route.stamp_meta({"series": "s", "sessionUid": "sid"})
        self.assertEqual(calls, [], "при OFF читали реестр нарядов")

    def test_clarify_falls_back_to_env(self):
        self.assertEqual(
            delivery_route.clarify_chat_for_meeting({"series": "s"}), OWNER_CHAT)

    def test_owner_required_is_off_too(self):
        _, errors = orders_lib.validate_order_payload({
            "meeting_id": "mtg-x", "company": "anzhee", "series_title": "С",
            "room_url": ROOM_A,
            "start_at": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
        })
        self.assertEqual([e for e in errors if e["field"] == "owner"], [])


class TestCalendarReschedule(_Base):
    """Перенос события Google-календаря (именно перенос, не создание)."""

    def test_moved_event_gets_a_new_start_key(self):
        from notary.lib import state as state_lib

        before = {"event_id": "gcal:abc_20260820T100000Z", "start_at": "2026-08-20T10:00:00+03:00"}
        after = {"event_id": "gcal:abc_20260820T100000Z", "start_at": "2026-08-20T11:00:00+03:00"}
        self.assertNotEqual(state_lib.start_key(before), state_lib.start_key(after),
                            "перенос не даёт нового старта — бот не придёт в новое время")

    def test_same_start_in_different_timezones_is_one_key(self):
        """Расхождение «на час» — классика: 10:00+03:00 и 07:00Z это ОДИН старт."""
        from notary.lib import state as state_lib

        a = {"event_id": "gcal:abc", "start_at": "2026-08-20T10:00:00+03:00"}
        b = {"event_id": "gcal:abc", "start_at": "2026-08-20T07:00:00Z"}
        self.assertEqual(state_lib.start_key(a), state_lib.start_key(b),
                         "один старт дал два ключа — в комнату придут два бота")

    def test_repeat_tick_of_the_same_start_is_still_deduped(self):
        from notary.lib import state as state_lib

        item = {"event_id": "gcal:abc", "start_at": "2026-08-20T10:00:00+03:00"}
        self.assertEqual(state_lib.start_key(item), state_lib.start_key(dict(item)))

    def test_unparseable_start_does_not_collapse_two_events(self):
        from notary.lib import state as state_lib

        a = {"event_id": "gcal:abc", "start_at": "мусор-1"}
        b = {"event_id": "gcal:abc", "start_at": "мусор-2"}
        self.assertNotEqual(state_lib.start_key(a), state_lib.start_key(b))


class TestLazyRegistryPaths(_Base):
    """ФУ-П1: пути реестра резолвятся лениво, а не на импорте модуля."""

    def test_env_set_after_import_is_honoured(self):
        self.assertEqual(registry_mod.watched_file(), self.registry / "watched.yaml")
        self.assertEqual(registry_mod.WATCHED_FILE, self.registry / "watched.yaml")
        other = self.tmp / "another"
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = str(other)
        self.assertEqual(registry_mod.watched_file(), other / "watched.yaml")
        self.assertEqual(registry_mod.rooms_file(), other / "rooms.yaml")
        self.assertEqual(registry_mod.pause_file(), other / ".pause-until")

    def test_old_style_patch_still_wins(self):
        """Пробы прошлых фаз патчат константу — она обязана продолжать работать."""
        patched = self.tmp / "patched.yaml"
        with mock.patch.object(registry_mod, "WATCHED_FILE", patched):
            self.assertEqual(registry_mod.watched_file(), patched)
        self.assertEqual(registry_mod.watched_file(), self.registry / "watched.yaml")

    def test_live_owner_registry_is_never_the_answer_here(self):
        for p in (registry_mod.watched_file(), registry_mod.rooms_file(),
                  registry_mod.pause_file(), orders_lib.orders_file()):
            self.assertIn(str(self.tmp), str(p), "путь ушёл мимо песочницы: %s" % p)

    def test_unknown_attribute_still_raises(self):
        with self.assertRaises(AttributeError):
            registry_mod.NO_SUCH_FILE  # noqa: B018


class TestScale(_Base):
    """Реальные объёмы, а не фикстура из одной встречи."""

    def test_many_orders_at_once_each_keeps_its_own_route(self):
        routes = {}
        for i in range(30):
            company = "anzhee" if i % 2 == 0 else "mpfirst"
            tg = PETROV_TG + i
            order = self.put_order(
                meeting_id="mtg-%02d" % i, company=company,
                room="https://telemost.yandex.ru/j/%014d" % (10 ** 13 + i),
                owner_tg=tg, portal_user_id=str(1000 + i), delivery="dm",
                series="seriya-%02d" % i,
            )
            routes[order["id"]] = delivery_route.resolve(order)
        self.assertEqual(len(routes), 30)
        self.assertEqual(len({r.chat_id for r in routes.values()}), 30,
                         "два наряда получили один адрес")
        for oid, r in routes.items():
            self.assertNotEqual(r.chat_id, OWNER_CHAT, oid)
        companies = {r.company for r in routes.values()}
        self.assertEqual(companies, {"anzhee", "mpfirst"})

    def test_long_series_many_meetings_finds_the_right_one(self):
        """Серия на много встреч: правка попадает в СВОЮ, а не в соседнюю."""
        for day in range(1, 41):
            d = self.protocols / ("koordinatsiya/%02d" % day)
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps({
                "series": "koordinatsiya",
                "date": "2026-07-%02d" % day,
                "sessionUid": "sid-%02d" % day,
                "delivered": [{"chat_id": GROUP_ANZHEE, "message_ids": [1000 + day]}],
            }, ensure_ascii=False), encoding="utf-8")
        found = feedback_worker.find_delivered_protocol(
            GROUP_ANZHEE, 1017, roots=[self.protocols], use_cache=False)
        self.assertIsNotNone(found)
        self.assertEqual(found["date"], "2026-07-17")

    def test_long_protocol_is_truncated_not_refused(self):
        from notary.lib import portal_ingest

        big = "х" * (portal_ingest.PORTAL_PROTOCOL_MAX + 5000)
        payload = portal_ingest.build_payload(
            order_id="portal:mtg-1", meeting_id="mtg-1", version=2,
            company="anzhee", session_uid="sid", protocol_md=big,
            revision_of=1, edits=[],
        )
        self.assertTrue(payload["protocol_truncated"])
        self.assertEqual(len(payload["protocol_md"]), portal_ingest.PORTAL_PROTOCOL_MAX)

    def test_window_cap_holds_under_flood(self):
        order = self.put_order(
            meeting_id="mtg-flood", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", series="koordinatsiya",
        )
        self.write_delivered_meta(
            series="koordinatsiya", date="2026-08-20", chat_id=GROUP_ANZHEE,
            sid="sid-flood", order=order)
        cap = feedback_worker.max_edits_per_window()
        for i in range(cap):
            feedback_worker.apply_portal_edit(
                meeting_id="mtg-flood", text="правка %d" % i, author="Пётр",
                portal_user_id="42", session_uid="sid-flood", edit_id="p%d" % i,
                root=self.feedback, roots=[self.protocols])
        with self.assertRaises(feedback_worker.PortalEditError) as ctx:
            feedback_worker.apply_portal_edit(
                meeting_id="mtg-flood", text="сверх капа", author="Пётр",
                portal_user_id="42", session_uid="sid-flood", edit_id="p-over",
                root=self.feedback, roots=[self.protocols])
        self.assertEqual(ctx.exception.code, "window_full")


class TestGatewayEditHandle(unittest.TestCase):
    """Ручка `POST /protocol/edit` — живым HTTP, а не вызовом метода класса.

    Коды ответов — это ровно то, что увидит портал; вызовом функции они не
    проверяются. Стенд — общий с пробами Ф9 (`gateway_harness`).
    """

    @classmethod
    def setUpClass(cls) -> None:
        from tests import gateway_harness as gh  # noqa: PLC0415

        cls.gh = gh

    def setUp(self) -> None:
        self.sandbox = self.gh.EnvSandbox()
        os.environ["MEETING_NOTARY_FEEDBACK_DIR"] = str(self.sandbox.root / "feedback")
        os.environ["MEETING_NOTARY_DELIVERED_ROOTS"] = str(self.sandbox.protocols_dir)
        os.environ[FLAG] = "1"
        identity.reset_cache()
        feedback_worker._INDEX_CACHE.update(built_at=0.0, roots=None, index={})
        self.gw = self.gh.LiveGateway()

    def tearDown(self) -> None:
        self.gw.stop()
        os.environ.pop("MEETING_NOTARY_FEEDBACK_DIR", None)
        os.environ.pop("MEETING_NOTARY_DELIVERED_ROOTS", None)
        self.sandbox.restore()
        feedback_worker._INDEX_CACHE.update(built_at=0.0, roots=None, index={})

    def _prepare_meeting(self) -> None:
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        clean, errors = orders_lib.validate_order_payload({
            "meeting_id": "mtg-gw", "company": "anzhee", "series_title": "Координация",
            "series": "koordinatsiya", "room_url": ROOM_A, "start_at": _iso(start),
            "delivery": "group", "telegram_chat_id": GROUP_ANZHEE,
            "owner": {"portal_user_id": "42", "telegram_user_id": PETROV_TG},
        })
        self.assertEqual(errors, [])
        order, _ = orders_lib.put_order(clean)
        d = self.sandbox.protocols_dir / "koordinatsiya"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({
            "series": "koordinatsiya", "date": "2026-08-20", "sessionUid": "sid-gw",
            "order": order, "company": "anzhee",
            "delivered": [{"chat_id": GROUP_ANZHEE, "message_ids": [501]}],
        }, ensure_ascii=False), encoding="utf-8")

    def test_edit_is_accepted_and_returns_the_window(self):
        self._prepare_meeting()
        r = self.gw.request("POST", "/protocol/edit", body={
            "meeting_id": "mtg-gw", "text": "по поставке отвечает Ольга",
            "author": "Пётр Петров", "portal_user_id": "42", "session_uid": "sid-gw",
        })
        self.assertEqual(r.status, 200, r.body)
        edit = r.json()["edit"]
        self.assertEqual(edit["window_min"], 20)
        self.assertEqual(edit["max_window_min"], 120)
        self.assertTrue(edit["deadline_at"])
        self.assertTrue(edit["hard_deadline_at"])

    def test_without_token_is_401(self):
        r = self.gw.request("POST", "/protocol/edit", body={"meeting_id": "x"}, token=None)
        self.assertEqual(r.status, 401)

    def test_missing_fields(self):
        for body, code in (
            ({"text": "правка", "portal_user_id": "42"}, "meeting_id_required"),
            ({"meeting_id": "mtg-gw", "portal_user_id": "42"}, "text_required"),
            ({"meeting_id": "mtg-gw", "text": "правка"}, "portal_user_id_required"),
        ):
            r = self.gw.request("POST", "/protocol/edit", body=body)
            self.assertEqual(r.status, 400, r.body)
            self.assertEqual(r.json().get("error"), code, r.body)

    def test_unknown_meeting_is_404(self):
        r = self.gw.request("POST", "/protocol/edit", body={
            "meeting_id": "mtg-never", "text": "правка", "portal_user_id": "42"})
        self.assertEqual(r.status, 404, r.body)
        self.assertEqual(r.json().get("error"), "meeting_not_found")

    def test_too_long_text_is_413(self):
        self._prepare_meeting()
        r = self.gw.request("POST", "/protocol/edit", body={
            "meeting_id": "mtg-gw", "text": "х" * 5000, "portal_user_id": "42"})
        self.assertEqual(r.status, 413, r.body)

    def test_quote_demand_from_outsider_is_422_with_explanation(self):
        self._prepare_meeting()
        r = self.gw.request("POST", "/protocol/edit", body={
            "meeting_id": "mtg-gw", "text": "добавь дословно, кто что сказал по этому спору",
            "author": "Гость", "portal_user_id": "999", "session_uid": "sid-gw"})
        self.assertEqual(r.status, 422, r.body)
        payload = r.json()
        self.assertEqual(payload.get("error"), quote_policy.CODE_QUOTES_NOT_ORGANIZER)
        self.assertIn("организатор", json.dumps(payload, ensure_ascii=False))

    def test_same_demand_from_organizer_passes(self):
        self._prepare_meeting()
        r = self.gw.request("POST", "/protocol/edit", body={
            "meeting_id": "mtg-gw", "text": "добавь дословно, кто что сказал по этому спору",
            "author": "Пётр", "portal_user_id": "42", "session_uid": "sid-gw"})
        self.assertEqual(r.status, 200, r.body)

    def test_repeat_with_same_edit_id_is_idempotent(self):
        self._prepare_meeting()
        body = {"meeting_id": "mtg-gw", "text": "правка", "author": "Пётр",
                "portal_user_id": "42", "session_uid": "sid-gw", "edit_id": "e-1"}
        first = self.gw.request("POST", "/protocol/edit", body=body)
        again = self.gw.request("POST", "/protocol/edit", body=body)
        self.assertEqual(first.status, 200)
        self.assertEqual(again.status, 200)
        self.assertEqual(again.json()["edit"]["edits_count"], 1)
        self.assertEqual(again.json()["edit"]["kind"], "dup")


class TestNoTextsInLogs(_Base):
    """Опасная тройка: маршрут не логирует ни реплик, ни имён участников."""

    def test_route_module_has_no_text_fields(self):
        source = (_NOTARY / "lib" / "delivery_route.py").read_text(encoding="utf-8")
        for forbidden in ("protocol_md", "transcript_text", "participants"):
            self.assertNotIn(
                "%s" % forbidden, source.split('"""', 2)[2],
                "маршрут трогает текстовое поле %s" % forbidden)

    def test_route_as_dict_carries_no_chat_id(self):
        """Порталу отдаём ФАКТ привязки, а не адрес приватного чата."""
        order = self.put_order(
            meeting_id="mtg-z", company="anzhee", room=ROOM_A,
            owner_tg=PETROV_TG, portal_user_id="42", delivery="group",
            chat_id=GROUP_ANZHEE)
        as_dict = delivery_route.resolve(order).as_dict()
        self.assertTrue(as_dict["chat_bound"])
        self.assertNotIn(str(GROUP_ANZHEE), json.dumps(as_dict))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
