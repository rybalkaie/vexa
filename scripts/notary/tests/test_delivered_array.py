"""Тесты array-формата `meta.delivered` + smoke `deliver_protocol`.

Покрывает Ф1-доработки 2026-05-29:
  - `_normalize_delivered` — миграция объект→массив (`UPУ2`).
  - `_find_delivery_for_chat` — поиск записи для chat_id.
  - `_update_meta_delivered` — append, replace-for-chat-id логика.
  - `deliver_protocol` end-to-end с mock-Telegram: idempotency срабатывает
    после первой доставки; смена `target_chat_id` отправляет в новый chat
    без дубля в старый.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_delivered_array -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import llm_postprocess as lp  # noqa: E402


SAMPLE_PROTOCOL = """#протоколвстречи 29.05.2026

**Встреча:** Тест доставки.

**Длительность:** 30 мин

**Участники:** Илья Рыбалка

**Транскрипт:** [2026-05-29.md](2026-05-29.md)

---

## 1) Раздел

▪️ Один буллет.

---

## Решения

🔸 Решение.

---

## Задачи

**Илья Рыбалка**

- Сделать что-то.
"""

META = {
    "series": "test-series",
    "date": "2026-05-29",
    "sessionUid": "test-sid",
    "startTs": "2026-05-29T09:00:00Z",
    "endTs": "2026-05-29T09:30:00Z",
    "expectedParticipants": ["Илья Рыбалка"],
    "participants": [],
}


class TestNormalizeDelivered(unittest.TestCase):

    def test_none_returns_empty(self):
        self.assertEqual(lp._normalize_delivered(None), [])

    def test_legacy_object_wrapped_in_list(self):
        """Старый формат `{chat_id, ...}` оборачивается в [{...}] (миграция)."""
        legacy = {"chat_id": 123, "message_ids": [1, 2, 3], "at": "2026-05-29T09:00:00Z"}
        out = lp._normalize_delivered(legacy)
        self.assertEqual(out, [legacy])

    def test_array_passthrough(self):
        arr = [{"chat_id": 1, "message_ids": [10]}, {"chat_id": 2, "message_ids": [20]}]
        self.assertEqual(lp._normalize_delivered(arr), arr)

    def test_non_dict_items_dropped(self):
        arr = [{"chat_id": 1}, "garbage", 42, {"chat_id": 2}]
        out = lp._normalize_delivered(arr)
        self.assertEqual(out, [{"chat_id": 1}, {"chat_id": 2}])

    def test_invalid_type_returns_empty(self):
        self.assertEqual(lp._normalize_delivered("string"), [])
        self.assertEqual(lp._normalize_delivered(42), [])


class TestFindDeliveryForChat(unittest.TestCase):

    def test_returns_last_for_chat_id(self):
        records = [
            {"chat_id": 1, "message_ids": [10]},
            {"chat_id": 2, "message_ids": [20]},
            {"chat_id": 1, "message_ids": [30, 31]},  # последняя запись для chat 1
        ]
        rec = lp._find_delivery_for_chat(records, 1)
        self.assertEqual(rec["message_ids"], [30, 31])

    def test_returns_none_when_not_found(self):
        self.assertIsNone(lp._find_delivery_for_chat([{"chat_id": 1}], 999))


class TestUpdateMetaDelivered(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test-meta-")
        self.meta_path = Path(self.tmpdir) / "test.meta.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read(self):
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def test_first_call_creates_array(self):
        """На пустом meta.json: запись → `delivered` = [{...}]."""
        self.meta_path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        rec = {"chat_id": 100, "message_ids": [1], "at": "now"}
        ok = lp._update_meta_delivered(self.meta_path, rec)
        self.assertTrue(ok)
        meta = self._read()
        self.assertEqual(meta["delivered"], [rec])

    def test_migrates_legacy_object(self):
        """Старый формат-объект автоматически мигрирует в массив."""
        self.meta_path.write_text(json.dumps({
            "delivered": {"chat_id": 100, "message_ids": [1, 2], "at": "old"},
        }), encoding="utf-8")
        # Добавляем запись в ДРУГОЙ chat_id.
        rec = {"chat_id": 200, "message_ids": [3], "at": "new"}
        lp._update_meta_delivered(self.meta_path, rec)
        meta = self._read()
        self.assertEqual(len(meta["delivered"]), 2)
        chat_ids = sorted(r["chat_id"] for r in meta["delivered"])
        self.assertEqual(chat_ids, [100, 200])

    def test_replace_for_chat_id_default(self):
        """Дефолт `replace_for_chat_id=True`: запись для того же chat_id заменяется."""
        self.meta_path.write_text(json.dumps({
            "delivered": [{"chat_id": 100, "message_ids": [1]}],
        }), encoding="utf-8")
        rec = {"chat_id": 100, "message_ids": [1, 2, 3], "at": "later"}
        lp._update_meta_delivered(self.meta_path, rec)
        meta = self._read()
        self.assertEqual(len(meta["delivered"]), 1)
        self.assertEqual(meta["delivered"][0]["message_ids"], [1, 2, 3])

    def test_keep_history_when_replace_false(self):
        self.meta_path.write_text(json.dumps({
            "delivered": [{"chat_id": 100, "message_ids": [1]}],
        }), encoding="utf-8")
        rec = {"chat_id": 100, "message_ids": [2], "at": "later"}
        lp._update_meta_delivered(self.meta_path, rec, replace_for_chat_id=False)
        meta = self._read()
        self.assertEqual(len(meta["delivered"]), 2)


def _fake_render(md_text, out_pdf, *, title, subtitle, **kwargs):
    """Mock PDF-рендера: пишет валидную заглушку (>10 байт `%PDF`), без chrome."""
    p = Path(out_pdf)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n" + b"x" * 64)
    return p


class TestDeliverProtocolIdempotency(unittest.TestCase):
    """Smoke `deliver_protocol` end-to-end по PDF-пути (mock render + sendDocument)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test-deliver-")
        self.meta_path = Path(self.tmpdir) / "test.meta.json"
        self.meta_path.write_text(json.dumps({}), encoding="utf-8")
        # Бот-токен обязателен.
        self._old_token = os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN")
        os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = "fake-token"
        # Гейт включён по дефолту, но удостоверимся.
        self._old_enable = os.environ.get("ENABLE_PROTOCOL_DELIVERY")
        os.environ.pop("ENABLE_PROTOCOL_DELIVERY", None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._old_token is None:
            os.environ.pop("TELEGRAM_NOTARIUS_BOT_TOKEN", None)
        else:
            os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = self._old_token
        if self._old_enable is not None:
            os.environ["ENABLE_PROTOCOL_DELIVERY"] = self._old_enable

    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    @mock.patch.object(lp.telegram_api, "send_document")
    def test_first_call_sends_pdf_and_records(self, mock_send_doc, mock_render):
        """REQ 1.1: один PDF + caption; `delivered` помечен document:true (RISK1)."""
        mock_send_doc.return_value = {"message_id": 42}
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL,
            meta_json_path=self.meta_path,
            target_chat_id=999,
            meeting_sid="test-sid",
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["chat_id"], 999)
        self.assertEqual(result["message_ids"], [42])
        self.assertEqual(result["parts_count"], 1)
        self.assertTrue(result.get("document"))
        # Ровно один документ; caption передан в send_document (REQ 3.1/3.2).
        self.assertEqual(mock_send_doc.call_count, 1)
        _, kwargs = mock_send_doc.call_args
        self.assertIn("caption", kwargs)
        self.assertIn("#протоколвстречи", kwargs["caption"])
        # meta.delivered: новый array-формат + флаг document (RISK1).
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertIsInstance(meta["delivered"], list)
        self.assertEqual(meta["delivered"][0]["chat_id"], 999)
        self.assertEqual(meta["delivered"][0]["message_ids"], [42])
        self.assertTrue(meta["delivered"][0]["document"])

    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    @mock.patch.object(lp.telegram_api, "send_document")
    def test_second_call_idempotent_skip(self, mock_send_doc, mock_render):
        """REQ 1.5: повторная доставка в тот же chat_id → skip без send."""
        mock_send_doc.return_value = {"message_id": 42}
        lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path,
            target_chat_id=999, meeting_sid="test-sid",
        )
        sends_after_first = mock_send_doc.call_count
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path,
            target_chat_id=999, meeting_sid="test-sid",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(mock_send_doc.call_count, sends_after_first)  # без нового send

    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    @mock.patch.object(lp.telegram_api, "send_document")
    def test_change_chat_id_sends_to_new_chat(self, mock_send_doc, mock_render):
        """РИСК5: смена target_chat_id → доставка в новый chat без дубля в старый."""
        mock_send_doc.return_value = {"message_id": 42}
        lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path,
            target_chat_id=999, meeting_sid="test-sid",
        )
        first = mock_send_doc.call_count
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path,
            target_chat_id=888, meeting_sid="test-sid",
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["chat_id"], 888)
        self.assertGreater(mock_send_doc.call_count, first)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        chat_ids = sorted(r["chat_id"] for r in meta["delivered"])
        self.assertEqual(chat_ids, [888, 999])

    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    @mock.patch.object(lp.telegram_api, "send_document")
    def test_legacy_text_delivery_skips_no_pdf_dup(self, mock_send_doc, mock_render):
        """RISK3: встреча, доставленная ТЕКСТОМ до деплоя (message_ids=N чанков,
        без document-флага), при повторном finalize НЕ должна уйти PDF-дублем —
        существующая запись для chat_id трактуется как «уже доставлено» (skip),
        число частей НЕ сверяется."""
        legacy = {
            "delivered": [{
                "chat_id": 999,
                "message_ids": [101, 102, 103],  # 3 текстовых чанка (legacy)
                "at": "2026-06-01T10:00:00Z",
            }],
        }
        self.meta_path.write_text(json.dumps(legacy), encoding="utf-8")
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path,
            target_chat_id=999, meeting_sid="test-sid",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["message_ids"], [101, 102, 103])
        mock_send_doc.assert_not_called()  # PDF-дубль НЕ отправлен
        mock_render.assert_not_called()    # и PDF даже не собирался

    @mock.patch.object(lp, "_alert_owner_pdf_failure")
    @mock.patch.object(lp.telegram_api, "send_document")
    @mock.patch.object(
        lp.protocol_to_pdf, "render_pdf_from_markdown",
        side_effect=lp.protocol_to_pdf.PdfRenderError("chromium boom"),
    )
    def test_pdf_build_failure_alerts_no_text(self, mock_render, mock_send_doc, mock_alert):
        """REQ 1.4: сбой СБОРКИ PDF → status error + алерт Илье; текстом НЕ слать."""
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path,
            target_chat_id=999, meeting_sid="test-sid",
        )
        self.assertEqual(result["status"], "error")
        mock_alert.assert_called_once()
        mock_send_doc.assert_not_called()  # текстом/документом ничего не ушло
        # meta.delivered НЕ записан (доставки не было) → не блокирует ретрай.
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta.get("delivered", []), [])

    @mock.patch.object(lp, "_alert_owner_pdf_failure")
    @mock.patch.object(
        lp.telegram_api, "send_document",
        side_effect=lp.telegram_api.TelegramApiError("sendDocument ok=false"),
    )
    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    def test_pdf_send_failure_alerts_no_text(self, mock_render, mock_send_doc, mock_alert):
        """REQ 1.4: сбой ОТПРАВКИ (send_document) → status error + алерт; не дублим."""
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path,
            target_chat_id=999, meeting_sid="test-sid",
        )
        self.assertEqual(result["status"], "error")
        mock_alert.assert_called_once()
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta.get("delivered", []), [])


if __name__ == "__main__":
    unittest.main()
