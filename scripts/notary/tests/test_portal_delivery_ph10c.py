"""Ф10c: результат встречи не теряется никогда — очередь на диске, ACK, удержание.

Что здесь доказывается (и почему именно так):

* `TestScenarioPortalDown` (R13.3) — портал лежит 30 минут; тики идут своим
  чередом; после подъёма протокол доезжает САМ. Ни одного ручного действия в
  сценарии нет — это и есть критерий, а не «мы предусмотрели повтор».
* `TestScenarioRestart` (R13.3) — служба перезапущена с непустой очередью.
  «Перезапуск» здесь настоящий: модули выбрасываются из `sys.modules` и
  грузятся заново, то есть память процесса обнуляется. Продолжиться доставка
  может только с диска.
* `TestScenarioSourcesUntilAck` (R13.4) — на диске лежит НАСТОЯЩИЙ файл записи;
  цепочка удаления в `collector` прогоняется как есть и отказывается его
  трогать, пока портал не подтвердил. После ACK — удаляет.
* `TestExhaustionAlert` (R13.4) — потолок попыток исчерпан: владельцу ушёл
  алерт, а исходники ОСТАЛИСЬ. «Устали повторять» не основание стереть запись.
* `TestInvalidInputs` — враждебные входы списком: 500, 502, 401, 404, 200 без
  ACK, ACK с чужим ключом, тело больше лимита, обрыв сети, полный диск, битый
  JSON в очереди, две доставки одного результата, доставка по отменённому
  наряду, доставка при выключателе OFF, результат без наряда.
* `TestPortalOff` — 🔴 неизменность поведения владельца: при OFF ни одного
  обращения к диску (счётчик), гейт удаления отвечает как до Ф10c.
* `TestSweeperContract` — 🔴 метельщик. Он удаляет WAV и рабочие транскрипты по
  сроку и живёт в systemd (bash, JSON не читает). Его условие прогоняется здесь
  НАСТОЯЩИМ bash в песочнице: с маркером удержания — не трогает, без маркера —
  сносит.
* `TestContractShape` — форма тела и ACK сверена с `docs/gateway-contract.md`
  дословно. Третья точка стыка с порталом; две предыдущие в этом же прогоне
  разъехались по camelCase и по адресу ручки.
* `TestNoTextsInLogs` — опасная тройка: ни реплики, ни протокол, ни имена
  участников не попадают ни в журнал, ни в алерт владельцу.
* `TestSeamsWithNeighbours` — соседи по исходникам (collector в двух местах,
  finalize, runner) ходят в ОДИН гейт, а не каждый в свой.

🔴 Изоляция: временный каталог реестра, временный каталог записей, никакого
Telegram и ни одного настоящего WAV владельца. Сторожа того же класса —
`test_state_path_isolation.py`, `test_gateway_state_isolation.py`.
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))
sys.path.insert(0, str(_NOTARY.parent))

import notary.cli.registry as registry_mod  # noqa: E402
from notary.lib import identity, orders as orders_lib  # noqa: E402
from notary.lib import portal_delivery, portal_ingest  # noqa: E402

ROOM = "https://telemost.yandex.ru/j/11111111111111"
ROOM_2 = "https://telemost.yandex.ru/j/22222222222222"
FLAG = identity.ENABLE_ENV

INGEST_URL = "https://portal.example.test/api/meetings/notary/ingest"

ISOLATED_ENV = (
    "MEETING_NOTARY_REGISTRY_DIR",
    "MEETING_NOTARY_LOG_DIR",
    portal_ingest.URL_ENV,
    portal_ingest.TOKEN_ENV,
    portal_ingest.MAX_BODY_ENV,
    "MEETING_NOTARY_PORTAL_MAX_ATTEMPTS",
    "MEETING_NOTARY_PORTAL_BACKOFF_BASE_S",
    "MEETING_NOTARY_PORTAL_BACKOFF_CAP_S",
    FLAG,
)


def _load_collector():
    """`collector.py` — скрипт верхнего уровня, грузим без запуска main()."""
    spec = importlib.util.spec_from_file_location(
        "collector_module_ph10c", str(_NOTARY / "collector.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_finalize():
    """`finalize-meeting.py` с заглушками тяжёлых пакетов (как в соседних пробах)."""
    for name in ("requests", "torch", "torchaudio", "pyannote",
                 "speechmatics", "anthropic", "numpy", "soundfile"):
        sys.modules.setdefault(name, types.ModuleType(name))
    spec = importlib.util.spec_from_file_location(
        "finalize_meeting_module_ph10c", str(_NOTARY / "finalize-meeting.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_collector()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class _FakePortal:
    """Ручка приёма портала в песочнице. Ведёт себя как договорено в контракте.

    Умеет лежать (`down_until`), отвечать любым кодом, «терять» ACK и отдавать
    чужой ключ. Помнит принятое — на этом проверяется идемпотентность.
    """

    def __init__(self) -> None:
        self.down_until: Optional[datetime] = None
        self.clock: datetime = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        self.status: int = 200
        self.received: List[Dict[str, Any]] = []
        self.ack_mode: str = "ok"  # ok | missing | foreign | not_ok | garbage
        self.raise_exc: Optional[Exception] = None
        self.max_body: Optional[int] = None
        self.calls: int = 0
        self.bodies: List[bytes] = []
        self.headers: List[Dict[str, str]] = []

    def __call__(
        self, url: str, headers: Dict[str, str], body: bytes, timeout: int,
    ) -> Tuple[int, bytes]:
        self.calls += 1
        self.bodies.append(body)
        self.headers.append(dict(headers))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.down_until is not None and self.clock < self.down_until:
            return 502, b'{"error":"bad gateway"}'
        if self.max_body is not None and len(body) > self.max_body:
            return 413, b'{"error":"body-too-large"}'
        if self.status != 200:
            return self.status, b'{"error":"nope"}'
        payload = json.loads(body.decode("utf-8"))
        key = payload["delivery_key"]
        # Портал различает диалекты ПО НАЛИЧИЮ `event` — доставка результата и
        # отчёт об уборке приходят с одним `delivery_key`, но это разные
        # сообщения. Дедуп по паре, иначе отчёт «схлопнулся» бы в доставку.
        mark = (key, payload.get("event"))
        duplicate = any((r["delivery_key"], r.get("event")) == mark for r in self.received)
        if not duplicate:
            self.received.append(payload)
        if self.ack_mode == "missing":
            return 200, json.dumps({"ok": True}).encode("utf-8")
        if self.ack_mode == "not_ok":
            return 200, json.dumps({"ok": False, "ack": {"delivery_key": key}}).encode("utf-8")
        if self.ack_mode == "garbage":
            return 200, b"<html>proxy says hi</html>"
        ack_key = "portal:someone-else#v1" if self.ack_mode == "foreign" else key
        return 200, json.dumps({
            "ok": True,
            "ack": {
                "delivery_key": ack_key,
                "meeting_id": payload.get("meeting_id"),
                "version": payload.get("version"),
                "received_at": _iso(self.clock),
                "duplicate": duplicate,
            },
        }).encode("utf-8")


class _Base(unittest.TestCase):
    """Временный реестр, временные записи, выключатель ON, без Telegram."""

    flag_default: Optional[str] = "1"

    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ISOLATED_ENV}
        self.tmp = Path(tempfile.mkdtemp(prefix="ph10c-"))
        self.registry = self.tmp / "registry"
        self.transcripts = self.tmp / "transcripts"
        self.registry.mkdir(parents=True, exist_ok=True)
        self.transcripts.mkdir(parents=True, exist_ok=True)
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = str(self.registry)
        os.environ["MEETING_NOTARY_LOG_DIR"] = str(self.tmp / "logs")
        os.environ[portal_ingest.URL_ENV] = INGEST_URL
        os.environ[portal_ingest.TOKEN_ENV] = "test-token"
        for name in (portal_ingest.MAX_BODY_ENV,
                     "MEETING_NOTARY_PORTAL_MAX_ATTEMPTS",
                     "MEETING_NOTARY_PORTAL_BACKOFF_BASE_S",
                     "MEETING_NOTARY_PORTAL_BACKOFF_CAP_S"):
            os.environ.pop(name, None)
        self.set_flag(self.flag_default)

        self.pushes: List[str] = []
        self.push_keys: List[str] = []
        self._patches = [
            # 🔴 Реестр СЕРИЙ резолвит путь на ИМПОРТЕ (`cli/registry.py:52-54`) —
            # без этих трёх заплат проба читала бы ЖИВОЙ реестр владельца.
            mock.patch.object(registry_mod, "WATCHED_FILE", self.registry / "watched.yaml"),
            mock.patch.object(registry_mod, "ROOMS_FILE", self.registry / "rooms.yaml"),
            mock.patch.object(registry_mod, "PAUSE_FILE", self.registry / ".pause-until"),
            mock.patch.object(collector, "push", side_effect=self._push),
            mock.patch.object(collector, "LOCAL_FINALIZE", True),
        ]
        for p in self._patches:
            p.start()
        # Алерты владельцу — в список, не в Telegram.
        self._notify_patch = mock.patch(
            "notary.lib.notify.push", side_effect=self._push,
        )
        self._notify_patch.start()

    def tearDown(self) -> None:
        self._notify_patch.stop()
        for p in reversed(self._patches):
            p.stop()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        identity.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _push(self, text: str, **kw: Any) -> bool:
        self.pushes.append(text)
        self.push_keys.append(str(kw.get("dedupe_key") or ""))
        return True

    def set_flag(self, value: Optional[str]) -> None:
        if value is None:
            os.environ.pop(FLAG, None)
        else:
            os.environ[FLAG] = value
        identity.reset_cache()

    # ---- фикстуры ----

    def put_order(
        self,
        meeting_id: str = "mtg-1001",
        *,
        start: Optional[datetime] = None,
        room: str = ROOM,
        duration: int = 60,
        company: str = "anzhee",
    ) -> Dict[str, Any]:
        start = start or datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        payload = {
            "meeting_id": meeting_id,
            "company": company,
            "series_title": "Координация продаж",
            "room_url": room,
            "start_at": _iso(start),
            "duration_minutes": duration,
            "owner": {"portal_user_id": "42"},
        }
        clean, errors = orders_lib.validate_order_payload(payload, now=start - timedelta(hours=1))
        self.assertEqual(errors, [], "фикстура наряда невалидна: %s" % errors)
        order, _ = orders_lib.put_order(clean, now=start - timedelta(hours=1))
        return order

    def make_recording(
        self, sid: str = "2026-08-20-tm-777", *, room: str = ROOM,
        start: Optional[datetime] = None, wav: bool = True,
    ) -> Dict[str, Any]:
        """Настоящие файлы записи в песочнице: WAV + рабочий транскрипт + meta."""
        start = start or datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        wav_path = self.transcripts / ("%s.chunk1.wav" % sid)
        txt_path = self.transcripts / ("%s.txt" % sid)
        if wav:
            wav_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake")
        txt_path.write_text("[00:00:01] Speaker S1: тестовая реплика\n", encoding="utf-8")
        return {
            "sessionUid": sid,
            "meetingUrl": room,
            "series": "koordinatsiya",
            "startTs": _iso(start),
            "endTs": _iso(start + timedelta(minutes=47)),
            "durationS": 2812,
            "participants": ["Илья Рыбалка"],
            "expectedParticipants": ["Илья Рыбалка"],
            "files": {"wav": str(wav_path)},
        }

    def enqueue_result(
        self, order: Dict[str, Any], sid: str, meta: Dict[str, Any],
        *, protocol: str = "# Протокол\n\nРешили: тест.\n",
        transcript: str = "[00:00:01] Speaker S1: тестовая реплика\n",
    ) -> str:
        payload = portal_ingest.build_payload(
            order_id=str(order["id"]),
            meeting_id=order.get("meeting_id"),
            version=1,
            company=order.get("company"),
            session_uid=sid,
            protocol_md=protocol,
            transcript_text=transcript,
            participants=meta.get("participants"),
            expected_participants=meta.get("expectedParticipants"),
            started_at=meta.get("startTs"),
            ended_at=meta.get("endTs"),
            duration_seconds=meta.get("durationS"),
            telegram_delivery={"status": "sent"},
            order_status=order.get("status"),
        )
        portal_delivery.hold(sid, order_id=str(order["id"]))
        key, _ = portal_delivery.enqueue(
            order_id=str(order["id"]),
            meeting_id=order.get("meeting_id"),
            version=1,
            session_uid=sid,
            company=order.get("company"),
            payload=payload,
            sources={"wav": (meta.get("files") or {}).get("wav")},
        )
        return key


# ---------------------------------------------------------------------------
# Сценарий 1: портал лежит 30 минут
# ---------------------------------------------------------------------------


class TestScenarioPortalDown(_Base):
    """R13.3: положили портал на 30 минут → протокол доехал БЕЗ ручных действий."""

    def test_portal_down_30_minutes_then_delivers_itself(self):
        t0 = datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)
        order = self.put_order()
        sid = "2026-08-20-tm-777"
        meta = self.make_recording(sid)
        key = self.enqueue_result(order, sid, meta)

        portal = _FakePortal()
        portal.down_until = t0 + timedelta(minutes=30)

        log: List[str] = []
        delivered_at: Optional[datetime] = None
        # Тики collector'а — раз в 5 минут, ровно как на бою.
        for minute in range(0, 65, 5):
            now = t0 + timedelta(minutes=minute)
            portal.clock = now
            stats = portal_ingest.drain(now=now, transport=portal)
            log.append("t+%02d мин → %s" % (minute, stats))
            item = portal_delivery.load_item(key)
            if item and item["status"] == portal_delivery.STATUS_DELIVERED and delivered_at is None:
                delivered_at = now

        print("\n[сценарий 1: портал лежит 30 минут]\n" + "\n".join(log))
        self.assertIsNotNone(delivered_at, "протокол так и не доехал: %s" % log)
        self.assertLessEqual(
            (delivered_at - portal.down_until).total_seconds(), 10 * 60,
            "после подъёма портала результат ждал слишком долго: %s" % log,
        )
        self.assertEqual(len(portal.received), 1, "портал получил не один результат")
        self.assertGreater(portal.calls, 1, "повторов не было — сценарий не воспроизведён")
        # 🔴 Ни одного ручного действия в цикле выше нет — только тики.
        item = portal_delivery.load_item(key)
        self.assertEqual(item["status"], portal_delivery.STATUS_DELIVERED)
        self.assertFalse(portal_delivery.is_held(sid), "удержание не снялось после ACK")

    def test_backoff_never_exceeds_its_cap(self):
        """🔴 Инвариант паузы, а не только сценарий.

        Экспонента без крышки на десятой попытке даёт сутки: портал поднялся, а
        результат ждёт до завтра — формально «доехал», фактически потерян на
        день. Мутационный прогон поймал, что сценарий один этого не ловит.
        """
        cap = portal_delivery.DEFAULT_BACKOFF_CAP_S
        worst = max(portal_delivery.backoff_seconds(n) for n in range(0, 40))
        self.assertLessEqual(worst, cap, "пауза переросла потолок: %d с" % worst)
        self.assertEqual(portal_delivery.backoff_seconds(0), 60)
        self.assertEqual(portal_delivery.backoff_seconds(1), 120)
        # Полный бюджет попыток обязан укладываться в разумные часы, иначе алерт
        # владельцу придёт через сутки — то есть никогда.
        total = sum(portal_delivery.backoff_seconds(n)
                    for n in range(portal_delivery.max_attempts()))
        self.assertLess(total, 4 * 3600, "потолок попыток растянут на полсуток")

    def test_delivery_survives_without_new_meetings(self):
        """Тик разбирает очередь, даже когда новых встреч нет.

        Именно так теряются результаты в выходные: очередь непуста, встреч нет,
        а разбор стоял в конце тика за четырьмя ранними `return`.
        """
        source = (_NOTARY / "collector.py").read_text(encoding="utf-8")
        head = source.split("# 1. Все *.meta.json", 1)[0]
        self.assertIn(
            "_portal_delivery_tick()", head,
            "разбор очереди обязан стоять ДО ранних выходов тика",
        )


# ---------------------------------------------------------------------------
# Сценарий 2: перезапуск службы с непустой очередью
# ---------------------------------------------------------------------------


class TestScenarioRestart(_Base):
    """R13.3: перезапустили сервис с непустой очередью → доставка продолжилась."""

    def test_queue_survives_process_restart(self):
        t0 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
        order = self.put_order()
        sid = "2026-08-20-tm-778"
        meta = self.make_recording(sid)
        key = self.enqueue_result(order, sid, meta)

        portal = _FakePortal()
        portal.clock = t0
        portal.status = 500
        before = portal_ingest.drain(now=t0, transport=portal)
        item_before = portal_delivery.load_item(key)
        self.assertEqual(item_before["status"], portal_delivery.STATUS_PENDING)
        self.assertEqual(item_before["attempts"], 1)

        # 🔴 НАСТОЯЩИЙ перезапуск: выбрасываем модули из памяти процесса. Всё,
        # что жило в переменных, исчезло — продолжиться можно только с диска.
        fresh_delivery, fresh_ingest = self._restart_modules()
        self.assertEqual(
            len(fresh_delivery.list_items()), 1,
            "после перезапуска очередь пуста — состояние жило в памяти",
        )

        portal.status = 200
        after = fresh_ingest.drain(
            now=t0 + timedelta(hours=1), transport=portal,
        )
        item_after = fresh_delivery.load_item(key)
        print("\n[сценарий 2: перезапуск] до=%s после=%s статус=%s"
              % (before, after, item_after["status"]))
        self.assertEqual(item_after["status"], fresh_delivery.STATUS_DELIVERED)
        self.assertEqual(len(portal.received), 1)
        self.assertFalse(fresh_delivery.is_held(sid))

    def test_attempt_counter_survives_restart(self):
        """Счётчик попыток тоже на диске: иначе рестарт обнулял бы его вечно."""
        t0 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
        order = self.put_order()
        sid = "2026-08-20-tm-779"
        key = self.enqueue_result(order, sid, self.make_recording(sid))
        portal = _FakePortal()
        portal.status = 503
        portal_ingest.drain(now=t0, transport=portal)
        portal_ingest.drain(now=t0 + timedelta(minutes=30), transport=portal)
        fresh_delivery, _ = self._restart_modules()
        self.assertEqual(fresh_delivery.load_item(key)["attempts"], 2)

    def _restart_modules(self):
        for name in ("notary.lib.portal_ingest", "notary.lib.portal_delivery"):
            sys.modules.pop(name, None)
        fresh_delivery = importlib.import_module("notary.lib.portal_delivery")
        fresh_ingest = importlib.import_module("notary.lib.portal_ingest")
        self.addCleanup(self._reimport_globals)
        return fresh_delivery, fresh_ingest

    @staticmethod
    def _reimport_globals() -> None:
        globals()["portal_delivery"] = importlib.import_module("notary.lib.portal_delivery")
        globals()["portal_ingest"] = importlib.import_module("notary.lib.portal_ingest")


# ---------------------------------------------------------------------------
# Сценарий 3: исходники живы до ACK
# ---------------------------------------------------------------------------


class TestScenarioSourcesUntilAck(_Base):
    """R13.4: WAV и рабочий транскрипт не удаляются, пока портал не подтвердил."""

    def test_wav_survives_telegram_delivery_and_dies_only_after_ack(self):
        t0 = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
        order = self.put_order()
        sid = "2026-08-20-tm-780"
        meta = self.make_recording(sid)
        wav = Path(meta["files"]["wav"])
        key = self.enqueue_result(order, sid, meta)
        meta["delivered"] = [{"chat_id": -100500, "message_ids": [7]}]

        # Telegram уже доставлен — сегодня этого хватало, чтобы стереть запись.
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertTrue(wav.exists(), "🔴 WAV удалён ДО подтверждения портала")
        marker = wav.with_name(wav.name + portal_delivery.HOLD_MARKER_SUFFIX)
        self.assertTrue(marker.exists(), "маркер удержания для метельщика не выставлен")

        portal = _FakePortal()
        portal.clock = t0
        portal_ingest.drain(now=t0, transport=portal)
        self.assertEqual(
            portal_delivery.load_item(key)["status"], portal_delivery.STATUS_DELIVERED,
        )

        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        print("\n[сценарий 3: исходники] до ACK жив=%s, после ACK жив=%s"
              % (True, wav.exists()))
        self.assertFalse(wav.exists(), "после подтверждения WAV обязан удаляться")
        self.assertFalse(marker.exists(), "маркер удержания не снят после ACK")

    def test_main_cleanup_site_is_gated_too(self):
        """Второе место удаления (после финализации) закрыто тем же гейтом."""
        sid = "2026-08-20-tm-781"
        order = self.put_order()
        meta = self.make_recording(sid)
        self.enqueue_result(order, sid, meta)
        self.assertTrue(collector._portal_sources_held(sid, meta))
        source = (_NOTARY / "collector.py").read_text(encoding="utf-8")
        cleanup_block = source.split("# 3. WAV cleanup", 1)[1].split("_SESSION_RE", 1)[0]
        self.assertIn("_portal_sources_held", cleanup_block)

    def test_hold_is_fail_closed_when_state_unreadable(self):
        """Нечитаемое удержание отвечает «держим». Удаление необратимо."""
        sid = "2026-08-20-tm-782"
        order = self.put_order()
        meta = self.make_recording(sid)
        self.enqueue_result(order, sid, meta)
        portal_delivery.hold_path(sid).write_text("{битый", encoding="utf-8")
        self.assertTrue(portal_delivery.is_held(sid))
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertTrue(Path(meta["files"]["wav"]).exists())

    def test_personal_series_without_order_behaves_exactly_as_before(self):
        """🔴 Результат без наряда — ОБЫЧНЫЙ случай (личная серия владельца)."""
        sid = "2026-08-20-tm-783"
        meta = self.make_recording(sid, room=ROOM_2)
        wav = Path(meta["files"]["wav"])
        collector._portal_note_recording(sid, meta)
        self.assertFalse(portal_delivery.is_held(sid))
        self.assertEqual(portal_delivery.list_items(), [])
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertFalse(wav.exists(), "личная серия обязана вести себя как до Ф10c")
        self.assertEqual(self.pushes, [], "по личной серии владельца не должно быть алертов")

    def test_finalize_keeps_failed_copies_until_ack(self):
        """`_failed/<sid>.wav` — единственный исходник после сбоя STT.

        Оригинал из `_tmp/` снёс стэш, и удалить копию до подтверждения портала
        значит потерять запись насовсем.
        """
        finalize = _load_finalize()
        failed = self.tmp / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        os.environ["MEETING_NOTARY_FAILED_DIR"] = str(failed)
        self.addCleanup(lambda: os.environ.pop("MEETING_NOTARY_FAILED_DIR", None))
        sid = "2026-08-20-tm-784"
        order = self.put_order()
        meta = self.make_recording(sid)
        self.enqueue_result(order, sid, meta)
        self.assertTrue(finalize._keep_sources_until_portal_ack(sid, meta))

        # Ветка «наряда нет» — поведение как до Ф10c.
        meta_personal = self.make_recording("2026-08-20-tm-785", room=ROOM_2)
        self.assertFalse(
            finalize._keep_sources_until_portal_ack("2026-08-20-tm-785", meta_personal),
        )

    def test_no_probe_touches_a_real_recording(self):
        """Сторож самой пробы: все файлы, которыми она играет, — в песочнице."""
        for path in self.transcripts.glob("*"):
            self.assertTrue(str(path).startswith(str(self.tmp)))
        self.assertTrue(str(portal_delivery.root_dir()).startswith(str(self.tmp)))


# ---------------------------------------------------------------------------
# Алерт при исчерпании потолка
# ---------------------------------------------------------------------------


class TestExhaustionAlert(_Base):
    """R13.4: потолок исчерпан → алерт владельцу, исходники ОСТАЛИСЬ."""

    def test_alert_and_sources_kept(self):
        os.environ["MEETING_NOTARY_PORTAL_MAX_ATTEMPTS"] = "3"
        t0 = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
        order = self.put_order()
        sid = "2026-08-20-tm-786"
        meta = self.make_recording(sid)
        wav = Path(meta["files"]["wav"])
        key = self.enqueue_result(order, sid, meta)

        portal = _FakePortal()
        portal.status = 500
        for minute in range(0, 200, 20):
            portal_ingest.drain(now=t0 + timedelta(minutes=minute), transport=portal)

        item = portal_delivery.load_item(key)
        print("\n[алерт] статус=%s попыток=%s пушей=%d"
              % (item["status"], item["attempts"], len(self.pushes)))
        self.assertEqual(item["status"], portal_delivery.STATUS_DEAD)
        self.assertTrue(self.pushes, "алерт владельцу не ушёл")
        self.assertTrue(
            any("НЕ доехал до портала" in text for text in self.pushes),
            "в алерте нет главного: результат не доехал (%s)" % self.pushes,
        )
        self.assertTrue(
            any(k.startswith("portal-delivery-dead:") for k in self.push_keys),
            "у алерта нет ключа глушилки по элементу — шум по одной встрече "
            "съест сообщение по другой",
        )
        # 🔴 «Умер» не значит «можно удалять».
        self.assertTrue(portal_delivery.is_held(sid))
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertTrue(wav.exists(), "исходники стёрты после исчерпания попыток")

    def test_owner_can_release_manually(self):
        """Отпустить исходники может только человек — и это работает."""
        os.environ["MEETING_NOTARY_PORTAL_MAX_ATTEMPTS"] = "1"
        order = self.put_order()
        sid = "2026-08-20-tm-787"
        meta = self.make_recording(sid)
        key = self.enqueue_result(order, sid, meta)
        portal = _FakePortal()
        portal.status = 500
        portal_ingest.drain(transport=portal)
        self.assertTrue(portal_delivery.is_held(sid))
        portal_delivery.abandon(key)
        self.assertFalse(portal_delivery.is_held(sid))
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertFalse(Path(meta["files"]["wav"]).exists())


# ---------------------------------------------------------------------------
# Враждебные входы
# ---------------------------------------------------------------------------


class TestInvalidInputs(_Base):
    """Всё, чем портал и машина могут ответить не по-человечески."""

    def _one(self, sid: str = "2026-08-20-tm-900") -> Tuple[str, Dict[str, Any]]:
        order = self.put_order(meeting_id="mtg-%s" % sid[-3:])
        meta = self.make_recording(sid)
        return self.enqueue_result(order, sid, meta), meta

    def test_http_500_502_401_404_are_retried_not_buried(self):
        for code in (500, 502, 401, 404, 429, 503):
            with self.subTest(code=code):
                sid = "2026-08-20-tm-9%02d" % code
                key, meta = self._one(sid)
                portal = _FakePortal()
                portal.status = code
                portal_ingest.drain(transport=portal)
                item = portal_delivery.load_item(key)
                self.assertEqual(
                    item["status"], portal_delivery.STATUS_PENDING,
                    "код %d не должен хоронить доставку: он чинится на той стороне" % code,
                )
                self.assertIsNotNone(item["next_attempt_at"])
                self.assertTrue(portal_delivery.is_held(sid))

    def test_200_without_ack_is_not_delivery(self):
        key, meta = self._one("2026-08-20-tm-901")
        portal = _FakePortal()
        portal.ack_mode = "missing"
        portal_ingest.drain(transport=portal)
        item = portal_delivery.load_item(key)
        self.assertEqual(item["status"], portal_delivery.STATUS_PENDING)
        self.assertEqual(item["last_error"], "ack_missing")
        self.assertTrue(Path(meta["files"]["wav"]).exists())

    def test_ack_with_foreign_key_is_not_delivery(self):
        key, meta = self._one("2026-08-20-tm-902")
        portal = _FakePortal()
        portal.ack_mode = "foreign"
        portal_ingest.drain(transport=portal)
        item = portal_delivery.load_item(key)
        self.assertEqual(item["status"], portal_delivery.STATUS_PENDING)
        self.assertEqual(item["last_error"], "ack_key_mismatch")
        self.assertTrue(Path(meta["files"]["wav"]).exists())

    def test_ack_not_ok_and_garbage_body(self):
        for mode, expect in (("not_ok", "ack_not_ok"), ("garbage", "ack_not_json")):
            with self.subTest(mode=mode):
                key, _ = self._one("2026-08-20-tm-90%s" % mode[:1])
                portal = _FakePortal()
                portal.ack_mode = mode
                portal_ingest.drain(transport=portal)
                self.assertEqual(portal_delivery.load_item(key)["last_error"], expect)

    def test_body_over_portal_limit_is_shrunk_not_dropped(self):
        os.environ[portal_ingest.MAX_BODY_ENV] = str(64 * 1024)
        order = self.put_order(meeting_id="mtg-big")
        sid = "2026-08-20-tm-903"
        meta = self.make_recording(sid)
        key = self.enqueue_result(
            order, sid, meta,
            protocol="# Протокол\nкороткий\n",
            transcript="реплика " * 40000,
        )
        portal = _FakePortal()
        portal.max_body = 64 * 1024
        portal_ingest.drain(transport=portal)
        item = portal_delivery.load_item(key)
        self.assertEqual(item["status"], portal_delivery.STATUS_DELIVERED)
        got = portal.received[0]
        self.assertTrue(got["transcript"]["truncated"], "транскрипт не помечен урезанным")
        self.assertIn("короткий", got["protocol_md"], "протокол резать нельзя первым")
        # Тело в очереди НЕ испорчено — ужимается только отправляемая копия.
        self.assertFalse(item["payload"]["transcript"]["truncated"])

    def test_network_breaks_mid_send(self):
        key, meta = self._one("2026-08-20-tm-904")
        portal = _FakePortal()
        portal.raise_exc = ConnectionResetError("оборвалось на середине тела")
        portal_ingest.drain(transport=portal)
        item = portal_delivery.load_item(key)
        self.assertEqual(item["status"], portal_delivery.STATUS_PENDING)
        self.assertEqual(item["last_error"], "transport_ConnectionResetError")
        self.assertTrue(Path(meta["files"]["wav"]).exists())

    def test_disk_full_on_enqueue_alerts_and_keeps_sources(self):
        order = self.put_order(meeting_id="mtg-disk")
        sid = "2026-08-20-tm-905"
        meta = self.make_recording(sid)
        portal_delivery.hold(sid, order_id=str(order["id"]))
        with mock.patch.object(
            portal_delivery, "_atomic_write_json",
            side_effect=OSError(28, "No space left on device"),
        ):
            with self.assertRaises(portal_delivery.PortalDeliveryError):
                portal_delivery.enqueue(
                    order_id=str(order["id"]), meeting_id="mtg-disk", version=1,
                    session_uid=sid, payload={"delivery_key": "x"},
                )
        # Удержание уже стоит → следующий тик чистки записи не тронет.
        self.assertTrue(portal_delivery.is_held(sid))
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertTrue(Path(meta["files"]["wav"]).exists())

    def test_disk_full_inside_collector_alerts_owner(self):
        order = self.put_order(meeting_id="mtg-disk2")
        sid = "2026-08-20-tm-906"
        meta = self.make_recording(sid)
        with mock.patch.object(
            portal_delivery, "enqueue",
            side_effect=portal_delivery.PortalDeliveryError("диск"),
        ):
            collector._portal_enqueue_result(
                sid, meta, {}, transcript_path=None, protocol_path=None,
                delivery_status="sent",
            )
        self.assertTrue(
            any("очередь доставки" in t for t in self.pushes),
            "владельцу не сказали, что результат не попал в очередь: %s" % self.pushes,
        )
        self.assertTrue(Path(meta["files"]["wav"]).exists())

    def test_corrupt_queue_file_does_not_stop_the_rest(self):
        key_ok, _ = self._one("2026-08-20-tm-907")
        broken = portal_delivery.items_dir() / "broken.json"
        broken.write_text("{это не json", encoding="utf-8")
        portal = _FakePortal()
        stats = portal_ingest.drain(transport=portal)
        self.assertEqual(stats["sent"], 1, "битый сосед остановил живую доставку")
        self.assertEqual(
            portal_delivery.load_item(key_ok)["status"], portal_delivery.STATUS_DELIVERED,
        )
        # 🔴 Битый файл НЕ перезаписан: в нём мог лежать результат встречи.
        self.assertEqual(broken.read_text(encoding="utf-8"), "{это не json")

    def test_double_delivery_of_same_result(self):
        order = self.put_order(meeting_id="mtg-twice")
        sid = "2026-08-20-tm-908"
        meta = self.make_recording(sid)
        key1 = self.enqueue_result(order, sid, meta)
        key2 = self.enqueue_result(order, sid, meta)
        self.assertEqual(key1, key2, "второй вызов создал второй ключ")
        self.assertEqual(len(portal_delivery.list_items()), 1)
        portal = _FakePortal()
        portal_ingest.drain(transport=portal)
        portal_ingest.drain(transport=portal)
        self.assertEqual(len(portal.received), 1, "портал получил дубль")
        self.assertEqual(portal.calls, 1, "доставленный элемент отправлен повторно")

    def test_enqueue_does_not_reset_attempts_of_existing_item(self):
        order = self.put_order(meeting_id="mtg-reset")
        sid = "2026-08-20-tm-909"
        meta = self.make_recording(sid)
        key = self.enqueue_result(order, sid, meta)
        portal = _FakePortal()
        portal.status = 500
        portal_ingest.drain(transport=portal)
        self.enqueue_result(order, sid, meta)
        self.assertEqual(
            portal_delivery.load_item(key)["attempts"], 1,
            "повторная постановка обнулила счётчик — потолок попыток стал бесконечным",
        )

    def test_cancelled_order_result_is_still_delivered(self):
        """Разговор УЖЕ записан. Отмена встречи постфактум его не отменяет."""
        order = self.put_order(meeting_id="mtg-cancel")
        sid = "2026-08-20-tm-910"
        meta = self.make_recording(sid)
        orders_lib.cancel_order(str(order["id"]))
        found = orders_lib.find_order_for_recording(
            room_url=meta["meetingUrl"], started_at=meta["startTs"], session_uid=sid,
        )
        self.assertIsNotNone(found, "отменённый наряд перестал находиться по записи")
        self.assertEqual(found["status"], orders_lib.STATUS_CANCELLED)
        key = self.enqueue_result(found, sid, meta)
        portal = _FakePortal()
        portal_ingest.drain(transport=portal)
        self.assertEqual(
            portal_delivery.load_item(key)["status"], portal_delivery.STATUS_DELIVERED,
        )
        self.assertEqual(portal.received[0]["order_status"], orders_lib.STATUS_CANCELLED)

    def test_422_and_413_are_permanent_with_alert(self):
        for code, err in ((422, "schema_rejected"), (413, "body_too_large")):
            with self.subTest(code=code):
                sid = "2026-08-20-tm-9%d" % code
                key, meta = self._one(sid)
                portal = _FakePortal()
                portal.status = code
                portal_ingest.drain(transport=portal)
                item = portal_delivery.load_item(key)
                self.assertEqual(item["status"], portal_delivery.STATUS_DEAD)
                self.assertEqual(item["last_error"], err)
                self.assertTrue(portal_delivery.is_held(sid), "исходники отпущены на отказе")

    def test_misconfigured_channel_does_not_burn_attempts(self):
        """Опечатка в адресе не имеет права за два часа похоронить все доставки."""
        key, _ = self._one("2026-08-20-tm-911")
        for value in ("", "http://portal.example.test/api"):
            with self.subTest(url=value or "пусто"):
                os.environ[portal_ingest.URL_ENV] = value
                portal = _FakePortal()
                stats = portal_ingest.drain(transport=portal)
                self.assertEqual(portal.calls, 0, "секрет ушёл по незащищённому каналу")
                self.assertEqual(portal_delivery.load_item(key)["attempts"], 0)
                self.assertGreaterEqual(stats["skipped"], 1)
        self.assertTrue(
            any("не настроен канал приёма" in t for t in self.pushes),
            "владельцу не сказали про сломанную настройку: %s" % self.pushes,
        )

    def test_token_never_leaves_over_plain_http(self):
        os.environ[portal_ingest.URL_ENV] = "http://portal.example.test/api"
        self.assertEqual(portal_ingest.config_problem(), "portal_url_not_https")

    def test_delivery_while_flag_off_is_a_no_op(self):
        key, meta = self._one("2026-08-20-tm-912")
        self.set_flag(None)
        portal = _FakePortal()
        stats = portal_ingest.drain(transport=portal)
        self.assertEqual(portal.calls, 0)
        self.assertEqual(stats, {"sent": 0, "retry": 0, "dead": 0, "skipped": 0})


# ---------------------------------------------------------------------------
# Выключатель
# ---------------------------------------------------------------------------


class TestPortalOff(_Base):
    """🔴 При OFF поведение владельца не меняется — и это доказывается механически."""

    flag_default = None

    OFF_FORMS = (None, "", "   ", "0", "false", "FALSE", "no", "off", "мусор")

    def test_no_disk_access_at_all_when_off(self):
        for form in self.OFF_FORMS:
            with self.subTest(flag=form):
                self.set_flag(form)
                calls: List[str] = []
                with mock.patch.object(
                    portal_delivery, "_registry_dir",
                    side_effect=lambda: calls.append("disk") or self.registry,
                ):
                    self.assertEqual(portal_delivery.list_items(), [])
                    self.assertEqual(portal_delivery.due_items(), [])
                    self.assertEqual(portal_delivery.held_sessions(), [])
                    self.assertFalse(portal_delivery.is_held("sid"))
                    self.assertFalse(portal_delivery.hold("sid"))
                    self.assertIsNone(portal_delivery.load_item("k"))
                    self.assertIsNone(portal_delivery.hold_if_portal("sid", room_url=ROOM))
                    self.assertEqual(portal_delivery.enqueue(
                        order_id="portal:x", meeting_id="x", session_uid="sid",
                        payload={},
                    ), ("", False))
                    self.assertFalse(portal_delivery.summary()["enabled"])
                self.assertEqual(
                    calls, [],
                    "при выключателе OFF модуль полез на диск (%d раз)" % len(calls),
                )

    def test_cleanup_behaves_exactly_as_before_when_off(self):
        self.set_flag(None)
        sid = "2026-08-20-tm-off"
        meta = self.make_recording(sid)
        wav = Path(meta["files"]["wav"])
        self.assertFalse(collector._portal_sources_held(sid, meta))
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertFalse(wav.exists(), "при OFF чистка обязана работать как раньше")

    def test_keep_audio_still_wins_when_on(self):
        """Старая защита `keep_audio` не сломана новой."""
        self.set_flag("1")
        sid = "2026-08-20-tm-keep"
        meta = self.make_recording(sid)
        meta["keepAudio"] = True
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertTrue(Path(meta["files"]["wav"]).exists())


# ---------------------------------------------------------------------------
# Метельщик
# ---------------------------------------------------------------------------


SWEEPER_UNIT = Path(
    "/Users/ilarybalka/Projects/meeting-notary/systemd/meeting-notary-cleanup.service"
)

#: Каталоги, зашитые в юнит литералами (обвязка живёт вне репозитория и env не
#: читает). Подменяем ИХ и только их — проверяемое условие остаётся авторским.
UNIT_TRANSCRIPTS = "/home/dev/meeting-notary/_tmp/transcripts"
UNIT_PROTOCOLS = "/home/dev/meeting-notary/_tmp/protocols"


def _real_sweeper_command(transcripts: Path, protocols: Path) -> Optional[str]:
    """Достать из юнита НАСТОЯЩУЮ строку чистки записей и подготовить к прогону.

    🔴 Почему не копия текста в пробе: копия проверяет копию. Мутационный прогон
    это и поймал — гейт `ack-hold` снимали из юнита, а проба оставалась зелёной,
    потому что слово «ack-hold» встречалось в КОММЕНТАРИИ рядом. Читаем именно
    исполняемую строку и её же исполняем.

    Две объявленные подмены, обе не касаются проверяемого условия:
      * литеральные каталоги → песочница;
      * `stat -c %Y` (GNU, VPS) → `stat -f %m` на macOS, иначе `stat` не отдаст
        время и арифметика юнита развалится на пустой строке.
    """
    if not SWEEPER_UNIT.exists():
        return None
    line = None
    for raw in SWEEPER_UNIT.read_text(encoding="utf-8").splitlines():
        if raw.lstrip().startswith("#"):
            continue  # комментарий — не исполняемая строка
        if raw.startswith("ExecStart=") and "_tmp/transcripts/*.wav" in raw:
            line = raw
            break
    if line is None:
        return None
    body = line.split("=", 1)[1]
    prefix = "/bin/bash -c '"
    assert body.startswith(prefix) and body.endswith("'"), body[:40]
    script = body[len(prefix):-1].replace("%%", "%")
    script = script.replace(UNIT_TRANSCRIPTS, str(transcripts))
    script = script.replace(UNIT_PROTOCOLS, str(protocols))
    if sys.platform == "darwin":
        script = script.replace("stat -c %Y", "stat -f %m")
    return script


class TestSweeperContract(_Base):
    """🔴 Метельщик удаляет WAV и рабочие транскрипты — прямой конфликт с R13.4.

    Договорённость с ним ровно одна: файл-маркер `<wav>.ack-hold`. Проверяем не
    рассуждением и не копией текста, а ИСПОЛНЕНИЕМ строки из самого юнита.
    """

    def _run_sweeper(self, sid: str, *, hold: bool) -> bool:
        wav = self.transcripts / ("%s.wav" % sid)
        wav.write_bytes(b"RIFFfake")
        (self.transcripts / ("%s.txt" % sid)).write_text("рабочий транскрипт", encoding="utf-8")
        protocols = self.tmp / "protocols"
        protocols.mkdir(exist_ok=True)
        (protocols / ("%s.md" % sid)).write_text("# протокол", encoding="utf-8")
        # Состариваем запись на 15 суток — порог юнита ровно 14 (1209600 с).
        old = datetime.now(timezone.utc) - timedelta(days=15)
        os.utime(wav, (old.timestamp(), old.timestamp()))
        if hold:
            wav.with_name(wav.name + portal_delivery.HOLD_MARKER_SUFFIX).write_text("hold")
        script = _real_sweeper_command(self.transcripts, protocols)
        if script is None:  # обвязка живёт вне репозитория
            self.skipTest("юнит метельщика недоступен в этой копии")
        subprocess.run(["/bin/bash", "-c", script], check=True)
        return wav.exists()

    def test_sweeper_skips_held_recording(self):
        self.assertTrue(
            self._run_sweeper("held-session", hold=True),
            "🔴 метельщик снёс запись, не доехавшую до портала",
        )
        self.assertTrue((self.transcripts / "held-session.txt").exists(),
                        "рабочий транскрипт снесён вместе с записью")

    def test_sweeper_still_cleans_unheld_recording(self):
        self.assertFalse(
            self._run_sweeper("free-session", hold=False),
            "без удержания метельщик обязан работать как раньше — иначе диск встанет",
        )

    def test_every_chunk_of_the_recording_is_marked(self):
        """🔴 Запись нарезана на куски, а метельщик судит каждый кусок отдельно.

        В `meta.files.wav` лежит только ПЕРВЫЙ кусок с речью. Пометь один —
        метельщик снесёт остальные, и у «удержанной» встречи пропадёт середина
        разговора.
        """
        sid = "2026-08-20-tm-chunks"
        order = self.put_order(meeting_id="mtg-chunks")
        chunks = []
        for idx in (1, 2, 3):
            path = self.transcripts / ("%s.chunk%d.wav" % (sid, idx))
            path.write_bytes(b"RIFFfake")
            chunks.append(path)
        meta = {
            "sessionUid": sid, "meetingUrl": ROOM,
            "startTs": _iso(datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)),
            "files": {"wav": str(chunks[0])},
        }
        self.enqueue_result(order, sid, meta)
        portal_delivery.sync_wav_marker(str(chunks[0]), sid)
        for path in chunks:
            marker = path.with_name(path.name + portal_delivery.HOLD_MARKER_SUFFIX)
            self.assertTrue(marker.exists(), "кусок %s остался без маркера" % path.name)
        # После подтверждения маркеры снимаются со ВСЕХ кусков.
        portal_delivery.release_hold(sid)
        portal_delivery.sync_wav_marker(str(chunks[0]), sid)
        for path in chunks:
            marker = path.with_name(path.name + portal_delivery.HOLD_MARKER_SUFFIX)
            self.assertFalse(marker.exists(), "маркер на %s не снят" % path.name)

    def test_sweeper_skips_every_chunk_of_a_held_meeting(self):
        """Тот же случай, но проверенный настоящим bash из юнита."""
        sid = "2026-08-20-tm-sweepchunks"
        protocols = self.tmp / "protocols"
        protocols.mkdir(exist_ok=True)
        old = datetime.now(timezone.utc) - timedelta(days=15)
        chunks = []
        for idx in (1, 2):
            path = self.transcripts / ("%s.chunk%d.wav" % (sid, idx))
            path.write_bytes(b"RIFFfake")
            (protocols / ("%s.chunk%d.md" % (sid, idx))).write_text("# протокол")
            os.utime(path, (old.timestamp(), old.timestamp()))
            chunks.append(path)
        order = self.put_order(meeting_id="mtg-sweepchunks")
        meta = {"sessionUid": sid, "meetingUrl": ROOM,
                "startTs": _iso(datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)),
                "files": {"wav": str(chunks[0])}}
        self.enqueue_result(order, sid, meta)
        portal_delivery.sync_wav_marker(str(chunks[0]), sid)
        script = _real_sweeper_command(self.transcripts, protocols)
        if script is None:
            self.skipTest("юнит метельщика недоступен в этой копии")
        subprocess.run(["/bin/bash", "-c", script], check=True)
        for path in chunks:
            self.assertTrue(path.exists(), "метельщик снёс кусок %s" % path.name)

    def test_guard_lives_in_the_executable_line_not_in_a_comment(self):
        """Гейт обязан быть в САМОЙ команде, а не в комментарии рядом.

        Ровно на этом мутация M10 пережила первую версию пробы: слово «ack-hold»
        нашлось в пояснении над `ExecStart`, и грепу этого хватило.
        """
        script = _real_sweeper_command(self.transcripts, self.tmp / "protocols")
        if script is None:
            self.skipTest("юнит метельщика недоступен в этой копии")
        self.assertIn(portal_delivery.HOLD_MARKER_SUFFIX, script)
        self.assertNotIn("#", script.split("ack-hold", 1)[0][-80:])


# ---------------------------------------------------------------------------
# Контракт
# ---------------------------------------------------------------------------


CONTRACT_PATH = _NOTARY / "docs" / "gateway-contract.md"


class TestContractShape(_Base):
    """Третья точка стыка. Две предыдущие разъехались молча — эта не должна."""

    EXPECTED_KEYS = {
        "delivery_key", "order_id", "meeting_id", "version", "company",
        "session_uid", "order_status", "protocol_md", "protocol_truncated",
        "transcript", "tasks",
        "participants", "expected_participants", "started_at", "ended_at",
        "duration_seconds", "late_join_minutes", "telegram_delivery", "generated_at",
        # Ф10d (R6.3): откуда версия и кто правил — лента версий портала.
        "revision_of", "edits",
    }

    def _payload(self) -> Dict[str, Any]:
        return portal_ingest.build_payload(
            order_id="portal:mtg-1001", meeting_id="mtg-1001", version=1,
            company="anzhee", session_uid="2026-08-20-tm-1",
        )

    def test_payload_keys_are_exactly_the_contract(self):
        self.assertEqual(set(self._payload()), self.EXPECTED_KEYS)

    def test_every_key_is_snake_case(self):
        for key in self._payload():
            self.assertEqual(key, key.lower(), "camelCase в теле: %s" % key)
            self.assertNotIn(" ", key)

    def test_contract_document_lists_every_field(self):
        text = CONTRACT_PATH.read_text(encoding="utf-8")
        for key in self.EXPECTED_KEYS:
            self.assertIn('"%s"' % key, text, "поле %s не описано в контракте" % key)

    def test_contract_document_describes_ack(self):
        text = CONTRACT_PATH.read_text(encoding="utf-8")
        for needle in ("ack_missing", "ack_key_mismatch", "duplicate",
                       "/api/meetings/notary/ingest", "X-Notary-Delivery-Key"):
            self.assertIn(needle, text, "в контракте нет: %s" % needle)

    def test_delivery_key_shape(self):
        self.assertEqual(
            portal_delivery.delivery_key("portal:mtg-1001", 1), "portal:mtg-1001#v1",
        )
        self.assertEqual(
            portal_delivery.delivery_key("portal:mtg-1001", 2), "portal:mtg-1001#v2",
        )

    def test_order_id_matches_registry_and_queue(self):
        """Ключ наряда один на все три слоя: реестр, очередь стартов, доставка."""
        from notary.lib import portal_queue  # noqa: PLC0415

        order = self.put_order(meeting_id="mtg-seam")
        self.assertEqual(order["id"], orders_lib.order_id_for_meeting("mtg-seam"))
        item = portal_queue.queue_item(order)
        self.assertEqual(item["order_id"], order["id"])
        self.assertTrue(
            portal_delivery.delivery_key(item["order_id"], 1).startswith(order["id"]),
        )

    def test_ack_validation_matrix(self):
        key = "portal:mtg-1#v1"
        good = {"ok": True, "ack": {"delivery_key": key}}
        self.assertEqual(portal_ingest.validate_ack(good, key)[0], {"delivery_key": key})
        cases = {
            "ack_not_json": "строка вместо объекта",
            "ack_not_ok": {"ok": False, "ack": {"delivery_key": key}},
            "ack_missing": {"ok": True},
            "ack_key_mismatch": {"ok": True, "ack": {"delivery_key": "portal:other#v1"}},
        }
        for reason, body in cases.items():
            with self.subTest(reason=reason):
                ack, got = portal_ingest.validate_ack(body, key)
                self.assertIsNone(ack)
                self.assertEqual(got, reason)

    def test_purge_report_shape_matches_portal_dialect(self):
        """Второй диалект ручки: событие `sources_purged` (R7.9, R5.12).

        Портал принимает его ТОЛЬКО с ключом, который сам выдал в ACK, и эхом
        возвращает тот же ключ. Проверяем всю цепочку: доставили → удалили →
        отчитались → портал подтвердил отчёт.
        """
        order = self.put_order(meeting_id="mtg-purge")
        sid = "2026-08-20-tm-purge"
        meta = self.make_recording(sid)
        base_key = self.enqueue_result(order, sid, meta)
        portal = _FakePortal()
        portal_ingest.drain(transport=portal)
        self.assertEqual(len(portal.received), 1)

        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        self.assertFalse(Path(meta["files"]["wav"]).exists())

        purge_key = base_key + portal_delivery.PURGE_SUFFIX
        item = portal_delivery.load_item(purge_key)
        self.assertIsNotNone(item, "отчёт об уборке не встал в очередь")
        self.assertEqual(item["ack_key"], base_key,
                         "отчёт ждёт подтверждения не по тому ключу")
        payload = item["payload"]
        self.assertEqual(set(payload), {
            "meeting_id", "event", "occurred_at", "order_id", "company", "delivery_key",
        })
        self.assertEqual(payload["event"], "sources_purged")
        self.assertEqual(payload["delivery_key"], base_key)
        self.assertEqual(payload["meeting_id"], "mtg-purge")

        portal_ingest.drain(transport=portal)
        self.assertEqual(
            portal_delivery.load_item(purge_key)["status"],
            portal_delivery.STATUS_DELIVERED,
            "портал подтвердил отчёт, а бот этого не увидел (ключ ACK разошёлся)",
        )
        self.assertEqual(len(portal.received), 2)
        self.assertIn("sources_purged", CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_purge_report_waits_for_the_portal_too(self):
        """Портал лежит в минуту уборки — отчёт не теряется, доедет позже."""
        order = self.put_order(meeting_id="mtg-purge2")
        sid = "2026-08-20-tm-purge2"
        meta = self.make_recording(sid)
        base_key = self.enqueue_result(order, sid, meta)
        portal = _FakePortal()
        portal_ingest.drain(transport=portal)
        collector._maybe_cleanup_wav_after_delivered(meta, sid)
        portal.status = 502
        portal_ingest.drain(transport=portal)
        purge_key = base_key + portal_delivery.PURGE_SUFFIX
        self.assertEqual(
            portal_delivery.load_item(purge_key)["status"], portal_delivery.STATUS_PENDING,
        )
        portal.status = 200
        portal_ingest.drain(now=datetime.now(timezone.utc) + timedelta(hours=1),
                            transport=portal)
        self.assertEqual(
            portal_delivery.load_item(purge_key)["status"], portal_delivery.STATUS_DELIVERED,
        )

    def test_no_purge_report_without_a_confirmed_delivery(self):
        """Нет подтверждённой доставки — предъявить порталу нечего, и не шлём."""
        sid = "2026-08-20-tm-nopurge"
        self.assertIsNone(portal_delivery.report_sources_purged(sid))
        self.assertEqual(portal_delivery.list_items(), [])

    def test_collector_never_sends_counters_as_tasks(self):
        """🔴 `tasks_extracted` у finalize — СЧЁТЧИКИ, а не список задач.

        Отправь их как `tasks`, и портал ответит 422 (`z.array(botTaskSchema)`),
        то есть отказом навсегда и алертом владельцу на КАЖДОЙ порталовой
        встрече. Ровно тот класс, что уже дважды ловили в этом прогоне.
        """
        order = self.put_order(meeting_id="mtg-tasks")
        sid = "2026-08-20-tm-tasks"
        meta = self.make_recording(sid)
        finalize_result = {
            "tasks_extracted": {"ilia": 2, "others": 1, "pending_deadline": 0,
                                "unknown_owner": 0},
        }
        collector._portal_enqueue_result(
            sid, meta, finalize_result,
            transcript_path=None, protocol_path=None, delivery_status="sent",
        )
        item = portal_delivery.load_item(
            portal_delivery.delivery_key(order["id"], 1),
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["payload"]["tasks"], [])
        for task in item["payload"]["tasks"]:
            self.assertIsInstance(task, dict)

    def test_portal_field_ceilings_are_respected(self):
        """Потолки полей портала: 301-я задача не должна хоронить всю доставку."""
        payload = portal_ingest.build_payload(
            order_id="portal:mtg-1", meeting_id="mtg-1", version=1, company="anzhee",
            session_uid="sid",
            protocol_md="п" * (portal_ingest.PORTAL_PROTOCOL_MAX + 10),
            tasks=[{"title": "t%d" % i} for i in range(400)],
            participants=["ч%d" % i for i in range(400)],
        )
        self.assertEqual(len(payload["protocol_md"]), portal_ingest.PORTAL_PROTOCOL_MAX)
        self.assertTrue(payload["protocol_truncated"])
        self.assertEqual(len(payload["tasks"]), portal_ingest.PORTAL_LIST_MAX)
        self.assertEqual(len(payload["participants"]), portal_ingest.PORTAL_LIST_MAX)

    def test_idempotency_header_is_sent(self):
        order = self.put_order(meeting_id="mtg-hdr")
        sid = "2026-08-20-tm-hdr"
        self.enqueue_result(order, sid, self.make_recording(sid))
        portal = _FakePortal()
        portal_ingest.drain(transport=portal)
        self.assertIn("X-Notary-Delivery-Key", portal.headers[0])
        self.assertTrue(portal.headers[0]["Authorization"].startswith("Bearer "))


# ---------------------------------------------------------------------------
# Опасная тройка
# ---------------------------------------------------------------------------


class TestNoTextsInLogs(_Base):
    """Ни реплики, ни протокол, ни имена участников — ни в журнал, ни в алерт."""

    SECRET_PROTOCOL = "СЕКРЕТНАЯ-СТРОКА-ПРОТОКОЛА-777"
    SECRET_TRANSCRIPT = "СЕКРЕТНАЯ-РЕПЛИКА-УЧАСТНИКА-888"

    def test_full_cycle_leaks_nothing(self):
        os.environ["MEETING_NOTARY_PORTAL_MAX_ATTEMPTS"] = "2"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        for name in ("notary.portal_delivery", "notary.portal_ingest", "collector_module_ph10c"):
            log = logging.getLogger(name)
            log.addHandler(handler)
            log.setLevel(logging.DEBUG)
            self.addCleanup(log.removeHandler, handler)

        order = self.put_order(meeting_id="mtg-secret")
        sid = "2026-08-20-tm-secret"
        meta = self.make_recording(sid)
        key = self.enqueue_result(
            order, sid, meta,
            protocol=self.SECRET_PROTOCOL, transcript=self.SECRET_TRANSCRIPT,
        )
        portal = _FakePortal()
        portal.status = 500
        portal_ingest.drain(transport=portal)
        portal_ingest.drain(now=datetime.now(timezone.utc) + timedelta(hours=2), transport=portal)
        portal.status = 200
        portal_delivery.revive(key)
        portal_ingest.drain(transport=portal)

        blob = stream.getvalue() + "\n".join(self.pushes)
        for secret in (self.SECRET_PROTOCOL, self.SECRET_TRANSCRIPT, "Илья Рыбалка"):
            self.assertNotIn(secret, blob, "утечка в журнал/алерт: %s" % secret)

    def test_queue_files_are_not_world_readable(self):
        order = self.put_order(meeting_id="mtg-perm")
        sid = "2026-08-20-tm-perm"
        key = self.enqueue_result(order, sid, self.make_recording(sid))
        path = portal_delivery.item_path(key)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(portal_delivery.items_dir().stat().st_mode & 0o777, 0o700)
        self.assertEqual(portal_delivery.hold_path(sid).stat().st_mode & 0o777, 0o600)

    def test_token_is_not_logged(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        log = logging.getLogger("notary.portal_ingest")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        self.addCleanup(log.removeHandler, handler)
        order = self.put_order(meeting_id="mtg-tok")
        sid = "2026-08-20-tm-tok"
        self.enqueue_result(order, sid, self.make_recording(sid))
        portal = _FakePortal()
        portal.status = 401
        portal_ingest.drain(transport=portal)
        self.assertNotIn("test-token", stream.getvalue() + "\n".join(self.pushes))


# ---------------------------------------------------------------------------
# Швы с соседями
# ---------------------------------------------------------------------------


class TestSeamsWithNeighbours(_Base):
    """Кто ещё читает и пишет то же самое. Разъедься они — потеряется запись."""

    def test_both_collector_cleanup_sites_use_one_gate(self):
        source = (_NOTARY / "collector.py").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("_portal_sources_held("), 3,
            "гейт удаления должен зваться из ДВУХ мест чистки (плюс определение)",
        )

    def test_finalize_failed_cleanup_uses_the_same_gate(self):
        source = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        self.assertIn("_keep_sources_until_portal_ack", source)
        block = source.split("# 5.1. Если этот sid был в _failed/", 1)[1][:1500]
        self.assertIn("_keep_sources_until_portal_ack", block)

    def test_runner_records_the_link_to_the_order(self):
        source = (_NOTARY / "runner.py").read_text(encoding="utf-8")
        self.assertIn("_remember_portal_run(cand, now)", source)
        order = self.put_order(meeting_id="mtg-link")
        orders_lib.attach_run(str(order["id"]), session_uid="auto-portal-mtg-link-x")
        found = orders_lib.find_order_for_recording(
            room_url=None, started_at=None, session_uid="auto-portal-mtg-link-x",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], order["id"])
        self.assertEqual(
            found["version"], order["version"],
            "отметка исполнения подняла версию наряда — портал увидит ложную правку",
        )

    def test_recording_matched_by_room_and_time_window(self):
        start = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        order = self.put_order(meeting_id="mtg-window", start=start, duration=60)
        cases = {
            "за 5 минут до старта": (start - timedelta(minutes=5), True),
            "в минуту старта": (start, True),
            "через час после старта": (start + timedelta(minutes=60), True),
            "через сутки": (start + timedelta(days=1), False),
            "за сутки до": (start - timedelta(days=1), False),
        }
        for label, (moment, expected) in cases.items():
            with self.subTest(label):
                found = orders_lib.find_order_for_recording(
                    room_url=ROOM, started_at=_iso(moment),
                )
                self.assertEqual(found is not None, expected, label)
        self.assertIsNone(
            orders_lib.find_order_for_recording(room_url=ROOM_2, started_at=_iso(start)),
            "чужая комната присвоила себе результат",
        )
        self.assertEqual(order["room_key"], orders_lib.room_key(ROOM))

    def test_two_versions_of_one_meeting_keep_the_hold(self):
        """Перевыпуск: пока вторая версия не подтверждена, исходники держим."""
        order = self.put_order(meeting_id="mtg-2v")
        sid = "2026-08-20-tm-2v"
        meta = self.make_recording(sid)
        key1 = self.enqueue_result(order, sid, meta)
        payload = portal_ingest.build_payload(
            order_id=str(order["id"]), meeting_id="mtg-2v", version=2,
            company="anzhee", session_uid=sid, protocol_md="# v2",
        )
        key2, created = portal_delivery.enqueue(
            order_id=str(order["id"]), meeting_id="mtg-2v", version=2,
            session_uid=sid, payload=payload,
        )
        self.assertTrue(created)
        self.assertNotEqual(key1, key2)
        portal = _FakePortal()
        portal.status = 500
        portal_ingest.drain(transport=portal)  # обе провалились
        portal.status = 200
        # Доставляем только первую — вторая ещё ждёт.
        portal_delivery.record_delivered(key1, {"delivery_key": key1})
        self.assertTrue(
            portal_delivery.is_held(sid),
            "удержание снялось по первой версии, вторая осталась без исходников",
        )
        portal_delivery.record_delivered(key2, {"delivery_key": key2})
        self.assertFalse(portal_delivery.is_held(sid))

    def test_scheduler_and_delivery_do_not_fight_over_the_registry(self):
        """Очередь доставки не трогает ни `.queue.json`, ни реестр нарядов."""
        order = self.put_order(meeting_id="mtg-nofight")
        sid = "2026-08-20-tm-nofight"
        before = json.loads(orders_lib.orders_file().read_text(encoding="utf-8"))
        self.enqueue_result(order, sid, self.make_recording(sid))
        portal_ingest.drain(transport=_FakePortal())
        after = json.loads(orders_lib.orders_file().read_text(encoding="utf-8"))
        self.assertEqual(before, after, "доставка переписала реестр нарядов")
        self.assertFalse((self.registry / ".queue.json").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
