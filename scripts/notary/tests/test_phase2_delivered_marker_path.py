"""Ф2 (REQ 3.1/3.2): finalize пишет `delivered` в ТУ ЖЕ meta, что читают
дедуп collector'а И reply-gate правок — без дубля и с матчем reply.

Регресс закрывает РИСК1/НЕС2. Корень бага: маркер доставки писался в
`md_path.parent/"meta.json"` (output-dir протокола) — этот файл создаётся
лениво (`.is_file()` обычно False → `_update_meta_delivered` получал None),
и его НЕ читает дедуп collector'а. Поэтому при recovery/повторном тике
встреча перевыпускалась дублем (а при почищенном WAV — ложный rc=3).

Чинится одним писателем (`_update_meta_delivered`), одним путём, одной формой:
маркер ложится в ИСХОДНУЮ meta встречи (`args.meta_json` =
`<transcripts>/<sid>.meta.json`) — ровно тот файл, который:
  • читает дедуп collector'а: `collector._read_meta_obj` → `_delivery_done`;
  • сканирует reply-gate: `feedback_worker.find_delivered_protocol`
    (transcripts-dir входит в delivered-roots);
  • заполняет `tools/backfill_delivered.py`.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase2_delivered_marker_path -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import llm_postprocess as lp  # noqa: E402
from lib import feedback_worker  # noqa: E402


def _load_collector():
    """collector.py — top-level скрипт; грузим через importlib без side-effects."""
    spec = importlib.util.spec_from_file_location(
        "collector_module_ph2", str(_NOTARY / "collector.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_finalize():
    """finalize-meeting.py (имя с дефисом) с заглушками тяжёлых внешних пакетов.

    В lean-окружении нет requests/torch/pyannote/… — STT-ветки импортируют их
    лениво, но `lib.name_mapping → align → transcribe` тянет `requests` уже на
    import. Подставляем пустышки только для загрузки модуля (логику пути доставки
    они не трогают).
    """
    for name in ("requests", "torch", "torchaudio", "pyannote",
                 "speechmatics", "anthropic", "numpy"):
        sys.modules.setdefault(name, types.ModuleType(name))
    spec = importlib.util.spec_from_file_location(
        "finalize_meeting_module_ph2", str(_NOTARY / "finalize-meeting.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_collector()
finalize = _load_finalize()


SAMPLE_PROTOCOL = """#протоколвстречи 06.06.2026

**Встреча:** Директорат.

**Длительность:** 30 мин

**Участники:** Илья Рыбалка

---

## 1) Раздел

▪️ Один буллет.

---

## Решения

🔸 Решение.
"""


def _fake_render(md_text, out_pdf, *, title, subtitle, **kwargs):
    """Mock PDF-рендера: валидная заглушка `%PDF`, без chrome."""
    p = Path(out_pdf)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n" + b"x" * 64)
    return p


class TestDeliveryMarkerPathWiring(unittest.TestCase):
    """Лочим САМ выбор пути в finalize: исходная transcripts-meta, не output-dir."""

    def test_marker_path_is_source_meta(self):
        src = "/srv/meeting-notary/_tmp/transcripts/auto-tm-x-20260606T090000Z.meta.json"
        self.assertEqual(finalize._delivery_marker_meta_path(src), Path(src))

    def test_marker_path_is_not_output_dir_meta(self):
        # Старый баг: маркер уходил в md_path.parent/meta.json output-dir'а.
        src = "/srv/meeting-notary/_tmp/transcripts/sid.meta.json"
        output_dir_meta = Path("/srv/meeting-notary/_tmp/protocols/anzhee-direktorat/meta.json")
        self.assertNotEqual(finalize._delivery_marker_meta_path(src), output_dir_meta)

    def test_finalize_source_no_longer_uses_output_dir_meta(self):
        """Source-lock: блок доставки больше не пишет маркер в output-dir."""
        text = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        self.assertNotIn('meta_json_path = md_path.parent / "meta.json"', text)
        self.assertIn("_delivery_marker_meta_path(args.meta_json)", text)


class TestDeliveredMarkerThreeReaders(unittest.TestCase):
    """delivered в ОДНОМ файле читают collector._delivery_done И
    feedback.find_delivered_protocol; повтор → skip без второго sendDocument."""

    def setUp(self):
        # «transcripts»-дир как на VPS: <sid>.meta.json + delivered-root reply-gate.
        self.tdir = Path(tempfile.mkdtemp(prefix="ph2-transcripts-"))
        self.sid = "auto-tm-test-20260606T090000Z"
        self.meta_path = self.tdir / f"{self.sid}.meta.json"
        self.meta_path.write_text(json.dumps({
            "series": "anzhee-direktorat",
            "date": "2026-06-06",
            "startTs": "2026-06-06T09:00:00Z",
            "sessionUid": self.sid,
            "files": {"wav": f"/transcripts/{self.sid}.wav"},
            "expectedParticipants": ["Илья Рыбалка"],
            "participants": [],
        }, ensure_ascii=False), encoding="utf-8")
        self.chat_id = -1001234567
        self._old_token = os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN")
        os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = "fake-token"
        self._old_enable = os.environ.get("ENABLE_PROTOCOL_DELIVERY")
        os.environ.pop("ENABLE_PROTOCOL_DELIVERY", None)
        # Сброс TTL-кэша индекса reply-gate между тестами.
        feedback_worker._INDEX_CACHE.update(built_at=0.0, roots=None, index={})

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tdir, ignore_errors=True)
        if self._old_token is None:
            os.environ.pop("TELEGRAM_NOTARIUS_BOT_TOKEN", None)
        else:
            os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = self._old_token
        if self._old_enable is not None:
            os.environ["ENABLE_PROTOCOL_DELIVERY"] = self._old_enable
        feedback_worker._INDEX_CACHE.update(built_at=0.0, roots=None, index={})

    def _meta(self):
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    @mock.patch.object(lp.telegram_api, "send_document")
    def test_marker_read_by_both_readers_and_no_dup(self, mock_send, mock_render):
        mock_send.return_value = {"message_id": 555}
        meeting_meta = {"series": "anzhee-direktorat", "date": "2026-06-06", "sessionUid": self.sid}

        # 1) Доставка пишет delivered в путь, который выбирает finalize (= args.meta_json).
        marker = finalize._delivery_marker_meta_path(str(self.meta_path))
        self.assertEqual(marker, self.meta_path)  # путь = исходная transcripts-meta
        res1 = lp.deliver_protocol(
            meeting_meta, SAMPLE_PROTOCOL,
            meta_json_path=marker, target_chat_id=self.chat_id, meeting_sid=self.sid,
        )
        self.assertEqual(res1["status"], "sent")
        self.assertEqual(mock_send.call_count, 1)  # ровно один sendDocument

        # 2) delivered лежит ИМЕННО в transcripts-meta.
        meta = self._meta()
        self.assertEqual(meta["delivered"][0]["chat_id"], self.chat_id)
        self.assertEqual(meta["delivered"][0]["message_ids"], [555])

        # 3) Reader #1 — дедуп collector'а читает ТОТ ЖЕ файл и видит доставку.
        self.assertTrue(collector._delivery_done(meta))

        # 4) Reader #2 — reply-gate сканирует transcripts-dir и матчит reply.
        meeting = feedback_worker.find_delivered_protocol(
            self.chat_id, 555, roots=[self.tdir], use_cache=False,
        )
        self.assertIsNotNone(meeting)
        self.assertEqual(
            Path(meeting["meta_path"]).resolve(), self.meta_path.resolve(),
        )  # ОДИН и тот же файл, что у collector'а
        self.assertEqual(meeting["series"], "anzhee-direktorat")

        # 5) Повторный collector-тик (та же встреча) → idempotent skip,
        #    второго sendDocument НЕТ (нет дубля протокола).
        res2 = lp.deliver_protocol(
            meeting_meta, SAMPLE_PROTOCOL,
            meta_json_path=marker, target_chat_id=self.chat_id, meeting_sid=self.sid,
        )
        self.assertEqual(res2["status"], "skipped")
        self.assertEqual(mock_send.call_count, 1)  # по-прежнему один — без дубля

    def test_collector_pretick_skips_after_marker(self):
        """До finalize: collector видит непустой meta.delivered в transcripts-meta
        → `_delivery_done` True → встреча скипается (finalize не зовётся)."""
        # Эмулируем уже-записанный маркер (как после успешной доставки выше).
        ok = lp._update_meta_delivered(self.meta_path, {
            "chat_id": self.chat_id, "message_ids": [555],
            "at": "2026-06-06T09:31:00Z", "decision": "pdf", "document": True,
        })
        self.assertTrue(ok)
        # collector в LOCAL_FINALIZE читает ровно этот файл (см. _read_meta_obj).
        self.assertTrue(collector._delivery_done(self._meta()))

    def test_partial_failure_marker_not_treated_as_done(self):
        """Контроль формы (РИСК1): partial-failure НЕ считается доставкой —
        ни одним из читателей, иначе недосыл бы замаскировался под «доставлено»."""
        lp._update_meta_delivered(self.meta_path, {
            "chat_id": self.chat_id, "message_ids": [1, 2],
            "at": "2026-06-06T09:31:00Z", "decision": "partial-failure",
        })
        meta = self._meta()
        self.assertFalse(collector._delivery_done(meta))


if __name__ == "__main__":
    unittest.main()
