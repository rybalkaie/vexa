"""Фаза 5 ISS-60: сторож сдался → порталу «бот не пришёл», разовый наряд закрыт.

Что доказывается:

* `TestGaveUpHook` — крючок `gave_up` зовётся РОВНО ОДИН раз и только при
  окончательном срыве (после повтора `-a2` или когда повтор бессмыслен), после
  сообщения людям; сбой крючка не ломает проход сторожа.
* `TestNoShowToPortal` — событие ложится в ту же очередь, что результаты, и
  доезжает до портала: портал-двойник отвечает ровно по формуле `storeEvent`
  (`notaryIngest.ts`), бот видит своё подтверждение. У личных серий — ни звука.
* `TestPortalContract` — форма события против того, что принимает портал: зеркало
  `notaryEventSchema` всегда + настоящий разбор портала (`parseNotaryIngestBody`
  через tsx), если рядом лежит его репозиторий.
* `TestImmediateOrderClosed` — хвост У9: после срыва кнопка «Подключить сейчас»
  снова заводит наряд и бота; прежний ключ старта держится (устаревшая строка
  очереди третьего бота не поднимет).
* `TestRunnerEndToEnd` — те же тики запускателя, что на бою: два срыва → одно
  сообщение, одно событие порталу, наряд закрыт.

🔴 Изоляция: временный реестр, никакого Telegram и настоящего портала; на боевой
портал тестовое событие не шлётся никогда (колокольчик ушёл бы живому человеку).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_no_show_portal_iss60 -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY.parent))
sys.path.insert(0, str(_NOTARY))

import notary.cli.registry as registry_mod  # noqa: E402
import notary.runner as runner  # noqa: E402
from notary.lib import identity, join_watch as jw  # noqa: E402
from notary.lib import orders as orders_lib  # noqa: E402
from notary.lib import portal_delivery, portal_ingest, portal_queue  # noqa: E402
from notary.lib import state as state_lib  # noqa: E402

ROOM = "https://telemost.yandex.ru/j/33333333333333"
INGEST_URL = "https://portal.example.test/api/meetings/notary/ingest"
NOW = datetime(2026, 9, 28, 7, 57, tzinfo=timezone.utc)
START = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
CONTRACT_PATH = _NOTARY / "docs" / "gateway-contract.md"


# ---------------------------------------------------------------------------
# Зеркало приёма портала (anzhee-b2b-portal/lib/meetings/notaryIngest.ts + route.ts)
# ---------------------------------------------------------------------------
PORTAL_EVENTS = ("bot_joined", "transcribing", "interrupted", "no_show", "sources_purged")
_OFFSET_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$", re.IGNORECASE)
_AUDIO_KEY_RE = re.compile(r"(audio|wav|mp3|m4a|ogg|opus|flac|voice|waveform|media_blob)", re.IGNORECASE)


def portal_event_issues(body: Dict[str, Any], *, now: datetime) -> List[str]:
    """Что портал отверг бы в событии хода (422). Пустой список — принял бы.

    Построчно по `notaryEventSchema` (meeting_id 1..64, event из списка,
    occurred_at — ISO с поясом 20..40 знаков, order_id ≤300, company ≤64,
    delivery_key ≤300), роуту (поле «про звук» — отказ; время не из будущего >5 мин)
    и `processNotaryIngest` (order_id обязан быть `portal:<meeting_id>`)."""
    issues: List[str] = []
    for key in body:
        if _AUDIO_KEY_RE.search(key):
            issues.append("audio_key:%s" % key)
    mid = body.get("meeting_id")
    if not (isinstance(mid, str) and 1 <= len(mid) <= 64):
        issues.append("meeting_id")
    if body.get("event") not in PORTAL_EVENTS:
        issues.append("event")
    stamp = body.get("occurred_at")
    if not (isinstance(stamp, str) and 20 <= len(stamp) <= 40 and _OFFSET_RE.search(stamp.strip())):
        issues.append("occurred_at")
    else:
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if when > now + timedelta(minutes=5):
                issues.append("occurred_at:in_future")
        except ValueError:
            issues.append("occurred_at:not_a_date")
    for key, cap in (("order_id", 300), ("company", 64), ("delivery_key", 300)):
        value = body.get(key)
        if value is not None and not (isinstance(value, str) and len(value) <= cap):
            issues.append(key)
    if body.get("order_id") and body.get("order_id") != "portal:%s" % mid:
        issues.append("order_id_mismatch")
    return issues


def portal_ack(body: Dict[str, Any]) -> Dict[str, Any]:
    """Подтверждение `storeEvent`: ключ — присланный или `portal:<id>#<event>`."""
    return {
        "ok": True,
        "ack": {
            "delivery_key": body.get("delivery_key") or "portal:%s#%s" % (body["meeting_id"], body["event"]),
            "meeting_id": body["meeting_id"],
            "version": None,
            "received_at": NOW.isoformat(),
            "duplicate": False,
            "stored": True,
            "sources_may_be_purged": False,
            "recording_status": "BOT_NO_SHOW",
        },
    }


class _FakePortal:
    """Ручка приёма портала: разбирает событие по зеркалу схемы и отвечает как `storeEvent`."""

    def __init__(self) -> None:
        self.received: List[Dict[str, Any]] = []
        self.rejected: List[List[str]] = []

    def __call__(self, url: str, headers: Dict[str, str], body: bytes, timeout: int) -> Tuple[int, bytes]:
        payload = json.loads(body.decode("utf-8"))
        issues = portal_event_issues(payload, now=datetime.now(timezone.utc))
        if issues:
            self.rejected.append(issues)
            return 422, b'{"ok":false,"error":"invalid_body"}'
        self.received.append(payload)
        return 200, json.dumps(portal_ack(payload)).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. Крючок сторожа
# ---------------------------------------------------------------------------
def _st(phase: str, *, at: datetime, reason: Optional[str] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {"phase": phase, "updated_at": at.isoformat()}
    if reason:
        d["reason"] = reason
    return d


class TestGaveUpHook(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger: Dict[str, Any] = {}
        self.item = {"meeting_id": "portal-mtg-1", "series": "portal-mtg-1", "event_id": "portal:portal:mtg-1",
                     "start_at": START.isoformat(), "url": ROOM, "order_id": "portal:mtg-1", "immediate": True}
        jw.register_launch(self.ledger, "k1", self.item, session_uid="auto-x",
                           container="vexa-notarius-auto-x", attempt=1, now=NOW)
        self.status: Dict[str, Any] = {}
        self.containers: Dict[str, Any] = {}
        self.calls: List[Tuple[str, Any]] = []
        self.persist_fails = False
        self.gave_up_raises = False

    def hooks(self) -> jw.Hooks:
        def gave_up(entry, reason):
            self.calls.append(("gave_up", reason))
            if self.gave_up_raises:
                raise RuntimeError("портал лежит")

        def persist(_ledger):
            if self.persist_fails:
                raise OSError("No space left on device")

        return jw.Hooks(
            inspect=lambda name: self.containers.get(name, jw.ContainerState("missing")),
            read_status=lambda uid: self.status.get(uid),
            stop=lambda name: None,
            plan_retry=lambda entry, n: {"session_uid": f"auto-x-a{n}", "container": f"vexa-notarius-auto-x-a{n}"},
            relaunch=lambda entry, n: True,
            alert=lambda entry, reason, n, kit, note: self.calls.append(("alert", reason)),
            event_alert=lambda entry, et, ev: None,
            note_owner=lambda text: None,
            persist=persist,
            gave_up=gave_up,
        )

    def fail_attempt(self, uid: str, at: datetime, reason: str = "name_input_not_found") -> None:
        self.status[uid] = _st("failed", at=at, reason=reason)
        self.containers[f"vexa-notarius-{uid}"] = jw.ContainerState("exited", 1)

    def tick(self, now: datetime, **kw) -> None:
        jw.tick(self.ledger, now=now, hooks=self.hooks(), **kw)

    def test_only_after_retry_and_only_once(self):
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1 - timedelta(seconds=20))
        self.tick(t1)
        self.assertEqual(self.calls, [], "первый срыв — ещё не «бот не пришёл»: впереди повтор")
        t2 = t1 + timedelta(seconds=60)
        self.fail_attempt("auto-x-a2", t2 - timedelta(seconds=10))
        self.tick(t2)
        self.assertEqual(self.calls, [("alert", "name_input_not_found"), ("gave_up", "name_input_not_found")],
                         "сдался: сначала людям, потом порталу")
        self.tick(t2 + timedelta(seconds=60))
        self.tick(t2 + timedelta(minutes=10))
        self.assertEqual(len([c for c in self.calls if c[0] == "gave_up"]), 1)

    def test_pointless_retry_gives_up_at_once(self):
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1, "meeting_not_found")
        self.tick(t1)
        self.assertIn(("gave_up", "meeting_not_found"), self.calls)

    def test_paused_retries_give_up_on_first_failure(self):
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1)
        self.tick(t1, allow_retry=False)
        self.assertIn(("gave_up", "name_input_not_found"), self.calls)

    def test_success_never_gives_up(self):
        t1 = NOW + timedelta(seconds=90)
        self.status["auto-x"] = _st("in_call", at=NOW + timedelta(seconds=40))
        self.containers["vexa-notarius-auto-x"] = jw.ContainerState("running")
        self.tick(t1)
        self.containers["vexa-notarius-auto-x"] = jw.ContainerState("exited", 0)
        self.tick(t1 + timedelta(minutes=50))
        self.assertEqual(self.calls, [])

    def test_unsaved_journal_blocks_it(self):
        self.persist_fails = True
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1, "meeting_not_found")
        self.tick(t1)
        self.assertEqual(self.calls, [], "без записи в журнал — ни сообщения, ни события порталу")

    def test_hook_failure_does_not_break_tick(self):
        self.gave_up_raises = True
        t1 = NOW + timedelta(seconds=60)
        self.fail_attempt("auto-x", t1, "meeting_not_found")
        self.tick(t1)  # не бросает
        self.assertEqual(self.ledger["k1"]["state"], "alerted")
        self.assertEqual(self.calls[0], ("alert", "meeting_not_found"))


# ---------------------------------------------------------------------------
# Общая песочница портала
# ---------------------------------------------------------------------------
class _PortalEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ph5-noshow-"))
        self.registry = self.tmp / "registry"
        self.registry.mkdir()
        (self.registry / "watched.yaml").write_text("watched: []\n", encoding="utf-8")
        env = {
            "MEETING_NOTARY_REGISTRY_DIR": str(self.registry),
            "MEETING_NOTARY_LOG_DIR": str(self.tmp / "logs"),
            portal_ingest.URL_ENV: INGEST_URL,
            portal_ingest.TOKEN_ENV: "test-token",
            identity.ENABLE_ENV: "1",
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        identity.reset_cache()
        self.pushes: List[str] = []
        self.patches = [
            mock.patch.object(registry_mod, "WATCHED_FILE", self.registry / "watched.yaml"),
            mock.patch.object(registry_mod, "ROOMS_FILE", self.registry / "rooms.yaml"),
            mock.patch.object(registry_mod, "PAUSE_FILE", self.registry / ".pause-until"),
            mock.patch("notary.lib.notify.push", side_effect=lambda text, **kw: self.pushes.append(text) or True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self._env.stop()
        identity.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def order(self, meeting_id: str = "mtg-501", *, kind: str = orders_lib.KIND_IMMEDIATE,
              now: datetime = NOW, company: str = "mpfirst") -> Dict[str, Any]:
        payload = {
            "meeting_id": meeting_id, "company": company, "series_title": "Планёрка отдела",
            "room_url": ROOM, "start_at": (now + timedelta(hours=1)).isoformat(),
            "duration_minutes": 60, "owner": {"portal_user_id": "42"},
        }
        clean, errors = orders_lib.validate_order_payload(payload, kind=kind, now=now)
        self.assertEqual(errors, [], errors)
        if kind == orders_lib.KIND_IMMEDIATE:
            order, _ = orders_lib.join_now(clean, now=now)
        else:
            order, _ = orders_lib.put_order(clean, now=now)
        return order

    def entry_for(self, order: Dict[str, Any], *, now: datetime = NOW) -> Dict[str, Any]:
        item = portal_queue.queue_item(order)
        ledger: Dict[str, Any] = {}
        jw.register_launch(ledger, state_lib.start_key(item), item, session_uid="auto-portal-x",
                           container="vexa-notarius-auto-portal-x", attempt=1, now=now)
        entry = ledger[state_lib.start_key(item)]
        entry["attempts"].append({"attempt": 2, "session_uid": "auto-portal-x-a2",
                                  "container": "vexa-notarius-auto-portal-x-a2", "launched_at": now.isoformat()})
        return entry

    def no_show_item(self, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Элемент «бот не пришёл» ЗАХОДА этого наряда (ключ — наряд + минута старта)."""
        mark = state_lib.normalized_start_key(portal_queue.queue_item(order).get("start_at"))
        return portal_delivery.load_item(portal_ingest.no_show_key(order["id"], mark))


# ---------------------------------------------------------------------------
# 2. Событие порталу
# ---------------------------------------------------------------------------
class TestNoShowToPortal(_PortalEnv):
    def test_gave_up_queues_no_show_and_portal_acks_it(self):
        order = self.order()
        runner._jw_gave_up(self.entry_for(order), "name_input_not_found")
        item = self.no_show_item(order)
        self.assertIsNotNone(item, "«бот не пришёл» не встал в очередь к порталу")
        payload = item["payload"]
        self.assertEqual(payload["event"], "no_show")
        self.assertEqual(payload["meeting_id"], "mtg-501")
        self.assertEqual(payload["order_id"], order["id"])
        self.assertEqual(payload["company"], "mpfirst")
        self.assertEqual(item["ack_key"], payload["delivery_key"])
        portal = _FakePortal()
        stats = portal_ingest.drain(transport=portal)
        self.assertEqual(portal.rejected, [], "портал отверг бы событие")
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(self.no_show_item(order)["status"], portal_delivery.STATUS_DELIVERED,
                         "портал подтвердил, а бот не узнал своё подтверждение")

    def test_second_give_up_same_meeting_is_one_event(self):
        order = self.order()
        runner._jw_gave_up(self.entry_for(order), "crashed")
        runner._jw_gave_up(self.entry_for(order), "crashed")
        portal = _FakePortal()
        portal_ingest.drain(transport=portal)
        self.assertEqual(len(portal.received), 1)

    def test_next_round_of_same_meeting_reports_again(self):
        """Цикл5: бота по той же встрече позвали снова — портал снял «бот не пришёл»
        (`retractBotNoShow`: снова «наряд размещён», колокольчик удалён). Срыв этого
        захода обязан дойти до портала снова, а не схлопнуться с первым по ключу наряда."""
        order = self.order(now=NOW)
        runner._jw_gave_up(self.entry_for(order, now=NOW), "name_input_not_found")
        portal = _FakePortal()
        portal_ingest.drain(transport=portal)
        later = NOW + timedelta(minutes=9)
        again = self.order(now=later)
        self.assertEqual(again["id"], order["id"], "та же встреча портала — тот же наряд")
        runner._jw_gave_up(self.entry_for(again, now=later), "name_input_not_found")
        portal_ingest.drain(transport=portal)
        keys = [p["delivery_key"] for p in portal.received]
        self.assertEqual(portal.rejected, [])
        self.assertEqual(len(keys), 2, "второй срыв после нового вызова не дошёл до портала: %s" % keys)
        self.assertEqual(len(set(keys)), 2, keys)
        for key in keys:
            self.assertTrue(key.startswith(order["id"] + "@") and key.endswith("#no_show"), key)

    def test_cancelled_order_sends_no_no_show(self):
        """Цикл5: встречу сняли, пока бот пытался войти, — колокольчик «бот не пришёл»
        про отменённую встречу был бы ложным."""
        order = self.order()
        orders_lib.cancel_order(order["id"], reason="отменили")
        runner._jw_gave_up(self.entry_for(order), "crashed")
        self.assertEqual(portal_delivery.list_items(), [])

    def test_owner_series_sends_nothing(self):
        entry = {"item": {"meeting_id": "anzhee-direktorat", "series": "anzhee-direktorat",
                          "start_at": START.isoformat()}, "series": "anzhee-direktorat",
                 "attempts": [{"attempt": 1, "session_uid": "auto-a"}]}
        with mock.patch.object(orders_lib, "get_order", side_effect=AssertionError("реестр нарядов не нужен")):
            runner._jw_gave_up(entry, "crashed")
        self.assertEqual(portal_delivery.list_items(), [])

    def test_flag_off_sends_nothing(self):
        order = self.order()
        os.environ[identity.ENABLE_ENV] = "0"
        identity.reset_cache()
        runner._jw_gave_up(self.entry_for(order), "crashed")
        os.environ[identity.ENABLE_ENV] = "1"
        identity.reset_cache()
        self.assertEqual(portal_delivery.list_items(), [])

    def test_dead_no_show_alerts_owner_with_true_text(self):
        order = self.order()
        runner._jw_gave_up(self.entry_for(order), "crashed")
        portal_ingest.drain(transport=lambda *a: (422, b'{"ok":false}'))
        self.assertEqual(self.no_show_item(order)["status"], portal_delivery.STATUS_DEAD)
        self.assertTrue(any("бот не пришёл" in t for t in self.pushes), self.pushes)
        self.assertFalse(any("Запись и рабочий транскрипт" in t for t in self.pushes),
                         "у «бот не пришёл» записи нет — текст про неё был бы враньём")

    def test_no_show_never_passes_for_a_result_delivery(self):
        """Отчёт об уборке опирается на ключ доставки результата — не на событие."""
        order = self.order()
        runner._jw_gave_up(self.entry_for(order), "crashed")
        portal_ingest.drain(transport=_FakePortal())
        self.assertIsNone(portal_delivery.delivered_key_for_session("auto-portal-x-a2"))


# ---------------------------------------------------------------------------
# 3. Контракт с порталом
# ---------------------------------------------------------------------------
def _portal_repo() -> Optional[Path]:
    raw = os.environ.get("ANZHEE_PORTAL_REPO") or os.path.expanduser("~/Projects/anzhee-b2b-portal")
    p = Path(raw)
    return p if (p / "lib" / "meetings" / "notaryIngest.ts").is_file() else None


_TSX_CHECK = r"""
import { parseNotaryIngestBody, normalizeIngestKeys, findAudioFields } from '@/lib/meetings/notaryIngest';
const raw = JSON.parse(process.argv[2]);
const norm = normalizeIngestKeys(raw);
const parsed = parseNotaryIngestBody(norm.value);
console.log(JSON.stringify({ audio: findAudioFields(raw), renamed: norm.renamed, ok: parsed.ok,
  kind: parsed.ok ? parsed.body.kind : null, issues: parsed.ok ? [] : parsed.issues }));
"""


class TestPortalContract(unittest.TestCase):
    def event(self, **kw) -> Dict[str, Any]:
        base = dict(meeting_id="cmabc123", order_id="portal:cmabc123", company="anzhee",
                    occurred_at=datetime.now(timezone.utc).isoformat())
        base.update(kw)
        return portal_ingest.build_no_show_event(**base)

    def test_shape_is_exactly_the_portal_event_dialect(self):
        ev = self.event()
        self.assertEqual(set(ev), {"meeting_id", "event", "occurred_at", "order_id", "company", "delivery_key"})
        self.assertEqual(portal_event_issues(ev, now=datetime.now(timezone.utc)), [])
        self.assertEqual(ev["delivery_key"], "portal:cmabc123#no_show",
                         "без времени захода — ключ, который портал синтезирует сам")
        # Цикл5: ключ одного ЗАХОДА бота — наряд + минута старта; портал его не
        # разбирает, а возвращает эхом, так что форма свободна в пределах 300 знаков.
        ev2 = self.event(round_mark="20260928T0800")
        self.assertEqual(ev2["delivery_key"], "portal:cmabc123@20260928T0800#no_show")
        self.assertEqual(portal_event_issues(ev2, now=datetime.now(timezone.utc)), [])
        self.assertEqual(self.event(round_mark="raw:мусор")["delivery_key"], "portal:cmabc123#no_show",
                         "неразобранное время старта — прежний ключ, не мусор в ключе")
        for key in ev:
            self.assertEqual(key, key.lower(), "camelCase в теле: %s" % key)

    def test_mirror_catches_what_portal_rejects(self):
        """Зеркало не пустышка: ломаем по полю — ловит."""
        now = datetime.now(timezone.utc)
        bad = [
            self.event(meeting_id="x" * 65),
            dict(self.event(), event="bot_left"),
            dict(self.event(), occurred_at="2026-09-26T10:00:00"),
            self.event(order_id="portal:someone-else"),
            dict(self.event(), audio_url="x"),
            self.event(occurred_at=(now + timedelta(hours=1)).isoformat()),
        ]
        for body in bad:
            with self.subTest(body=sorted(body)):
                self.assertNotEqual(portal_event_issues(body, now=now), [])

    def test_contract_document_describes_no_show(self):
        text = CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertIn("no_show", text)
        self.assertIn("#no_show", text)

    def test_mirror_is_in_sync_with_portal_source(self):
        repo = _portal_repo()
        if repo is None:
            self.skipTest("репозитория портала рядом нет (сервер/CI)")
        src = (repo / "lib" / "meetings" / "notaryIngest.ts").read_text(encoding="utf-8")
        for needle in (
            "'no_show',",
            "meeting_id: z.string().min(1).max(64),",
            "event: z.enum(NOTARY_INGEST_EVENTS),",
            "occurred_at: isoWithOffset,",
            "order_id: z.string().max(300).nullish(),",
            "company: z.string().max(64).nullish(),",
            "delivery_key: z.string().max(300).nullish(),",
            "delivery_key: body.delivery_key ?? `portal:${meeting.id}#${body.event}`",
            "body.data.order_id !== `portal:${meeting.id}`",
        ):
            self.assertIn(needle, src, "портал поменял приём событий — сверь зеркало: %s" % needle)
        for event in PORTAL_EVENTS:
            self.assertIn("'%s'" % event, src)

    def test_real_portal_parser_accepts_no_show(self):
        """Настоящий разбор портала (`parseNotaryIngestBody`) на нашем событии."""
        repo = _portal_repo()
        tsx = repo / "node_modules" / ".bin" / "tsx" if repo else None
        if repo is None or not tsx.exists() or os.environ.get("NOTARY_SKIP_PORTAL_TSX") == "1":
            self.skipTest("нет репозитория портала или tsx")
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "check.ts"
            script.write_text(_TSX_CHECK, encoding="utf-8")

            def parse(body):
                env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
                proc = subprocess.run([str(tsx), str(script), json.dumps(body, ensure_ascii=False)],
                                      cwd=str(repo), capture_output=True, text=True, timeout=120, env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
                return json.loads(proc.stdout.strip().splitlines()[-1])

            got = parse(self.event())
            self.assertEqual((got["ok"], got["kind"], got["audio"], got["renamed"]),
                             (True, "event", [], []), got)
            got_round = parse(self.event(round_mark="20260928T0800"))
            self.assertEqual((got_round["ok"], got_round["kind"]), (True, "event"), got_round)
            broken = parse(dict(self.event(), event="bot_left"))
            self.assertFalse(broken["ok"], "контроль: сломанное событие портал обязан отвергнуть")


# ---------------------------------------------------------------------------
# 4. Хвост У9: разовый наряд закрыт — кнопка снова работает
# ---------------------------------------------------------------------------
class TestImmediateOrderClosed(_PortalEnv):
    def test_button_works_again_after_give_up(self):
        order = self.order(now=NOW)
        old_item = portal_queue.queue_item(order)
        old_key = state_lib.start_key(old_item)
        self.assertTrue(state_lib.state_acquire(old_key, now=NOW))   # первый бот поднялся
        runner._jw_gave_up(self.entry_for(order), "name_input_not_found")

        closed = orders_lib.get_order(order["id"])
        self.assertEqual(closed["status"], orders_lib.STATUS_FINISHED)
        self.assertEqual(closed.get("finish_reason"), orders_lib.CLOSE_REASON_JOIN_FAILED)
        self.assertIsNone(orders_lib.find_active_order_for_room(ROOM, now=NOW + timedelta(minutes=8)),
                          "комната всё ещё занята сорвавшимся нарядом")

        later = NOW + timedelta(minutes=9)
        again = self.order(now=later)                      # второе нажатие той же встречи
        self.assertEqual(again["status"], orders_lib.STATUS_SCHEDULED)
        due = portal_queue.due_immediate_items(later)
        self.assertEqual([it["order_id"] for it in due], [again["id"]])
        new_key = state_lib.start_key(due[0])
        self.assertNotEqual(new_key, old_key)
        self.assertTrue(state_lib.state_acquire(new_key, now=later), "новый бот упёрся в прежний ключ")
        self.assertFalse(state_lib.state_acquire(old_key, now=later),
                         "прежний ключ освобождён — устаревшая строка очереди подняла бы третьего бота")

    def test_scheduled_order_is_not_closed(self):
        order = self.order("mtg-777", kind=orders_lib.KIND_SCHEDULED)
        runner._jw_gave_up(self.entry_for(order), "crashed")
        self.assertEqual(orders_lib.get_order(order["id"])["status"], orders_lib.STATUS_SCHEDULED)
        self.assertIsNotNone(self.no_show_item(order), "у плановой встречи «не пришёл» тоже шлётся")

    def test_cancelled_order_stays_cancelled(self):
        order = self.order()
        orders_lib.cancel_order(order["id"], reason="отменили")
        runner._jw_gave_up(self.entry_for(order), "crashed")
        self.assertEqual(orders_lib.get_order(order["id"])["status"], orders_lib.STATUS_CANCELLED)

    def test_registry_failure_does_not_stop_portal_event(self):
        order = self.order()
        with mock.patch.object(orders_lib, "close_immediate_after_join_failure",
                               side_effect=orders_lib.OrderRegistryError("диск")):
            runner._jw_gave_up(self.entry_for(order), "crashed")
        self.assertIsNotNone(self.no_show_item(order))


# ---------------------------------------------------------------------------
# 5. Сквозной тик запускателя
# ---------------------------------------------------------------------------
class TestRunnerEndToEnd(_PortalEnv):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "logs" / "runs").mkdir(parents=True)
        self.more = [
            mock.patch.object(runner, "setup_logging", lambda: None),
            mock.patch.object(runner, "push"),
            mock.patch.object(runner, "RUNS_DIR", self.tmp / "logs" / "runs"),
            mock.patch.object(runner, "LOG_DIR", self.tmp / "logs"),
            mock.patch.object(runner, "LOCAL_DOCKER", True),
            mock.patch.object(runner, "_canary_warn_tick"),
        ]
        for p in self.more:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.more):
            p.stop()
        super().tearDown()

    def test_two_failures_one_message_one_portal_event_order_closed(self):
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        order = self.order(now=t0)
        state_lib.write_queue([])
        snapshot = {"by_series": {}, "by_room": {}, "unlabeled": [], "total": 0}
        launches: List[int] = []
        statuses: Dict[str, Any] = {}
        containers: Dict[str, Any] = {}
        alerts: List[Any] = []
        with mock.patch.object(runner, "_running_bots_snapshot", return_value=snapshot), \
                mock.patch.object(runner, "_launch_bot",
                                  side_effect=lambda item, now, seq=0, attempt=1: launches.append(attempt) or True), \
                mock.patch.object(runner, "_jw_inspect",
                                  side_effect=lambda n: containers.get(n, jw.ContainerState("running"))), \
                mock.patch.object(runner, "_jw_read_status", side_effect=lambda uid: statuses.get(uid)), \
                mock.patch.object(runner, "_jw_save_logs"), mock.patch.object(runner, "_jw_stop"), \
                mock.patch("notary.lib.join_alert.send", side_effect=lambda *a, **k: alerts.append(a)):
            runner.main(now=t0)
            self.assertEqual(launches, [1], "разовый наряд не поднял бота")
            entry = next(iter(jw.load_ledger().values()))
            uid1 = entry["attempts"][0]["session_uid"]
            t1 = t0 + timedelta(seconds=70)
            statuses[uid1] = {"phase": "failed", "reason": "name_input_not_found", "updated_at": t0.isoformat()}
            containers["vexa-notarius-" + uid1] = jw.ContainerState("exited", 1)
            runner.main(now=t1)
            self.assertEqual(launches, [1, 2])
            self.assertIsNone(self.no_show_item(order), "«не пришёл» ушёл до повтора")
            t2 = t1 + timedelta(seconds=70)
            statuses[uid1 + "-a2"] = {"phase": "failed", "reason": "name_input_not_found",
                                      "updated_at": t1.isoformat()}
            containers["vexa-notarius-" + uid1 + "-a2"] = jw.ContainerState("exited", 1)
            runner.main(now=t2)
            runner.main(now=t2 + timedelta(seconds=60))
        self.assertEqual(launches, [1, 2], "после срыва наряд поднял бота ещё раз")
        self.assertEqual(len(alerts), 1)
        self.assertIsNotNone(self.no_show_item(order), "порталу не ушло «бот не пришёл»")
        self.assertEqual(orders_lib.get_order(order["id"])["status"], orders_lib.STATUS_FINISHED)


if __name__ == "__main__":
    unittest.main()
