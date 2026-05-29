"""Тест-контракт `collector._parse_finalize_result` (У7 хода 2 цикла Ф1).

Цель — закрепить формат stdout JSON финалайза, чтобы будущие правки
`finalize-meeting.py::main` не сломали тихо парсер collector'а и не
вернули рассинхрон 29.05.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_collector_parsing -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


def _load_collector_module():
    """`collector.py` лежит в notary/ как top-level скрипт (не в lib/) —
    подгружаем через importlib без выполнения side-effects.
    """
    spec = importlib.util.spec_from_file_location(
        "collector_module", str(_NOTARY / "collector.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Чтоб не запускать setup_logging() при load — он зовётся только в main().
collector = _load_collector_module()


REALISTIC_STDOUT = """2026-05-29 11:30:01 [INFO] finalize-meeting: STT_BACKEND=speechmatics
2026-05-29 11:30:01 [INFO] finalize-meeting: Session auto-tm-x-20260529T103000Z — wav=/x.wav, ...
2026-05-29 11:30:02 [INFO] finalize-meeting: Step 5/5 — Render markdown
2026-05-29 11:30:03 [INFO] finalize-meeting: Protocol written → /srv/meeting-notary/protocols/test-series/2026-05-29.md
2026-05-29 11:30:04 [INFO] finalize-meeting: [delivery] sent meeting=test chat_id=-1001 parts=1
{
  "ok": true,
  "session_uid": "auto-tm-x-20260529T103000Z",
  "series": "test-series",
  "protocol_path": "/srv/meeting-notary/protocols/test-series/2026-05-29.md",
  "wav_kept": false,
  "stt_backend": "speechmatics",
  "delivery": {
    "status": "sent",
    "chat_id": -1001,
    "message_ids": [42],
    "parts_count": 1
  }
}
"""


class TestParseFinalizeResult(unittest.TestCase):

    def test_extracts_full_result_dict(self):
        """Полный stdout с JSON-блоком после `Protocol written` маркера."""
        result = collector._parse_finalize_result(REALISTIC_STDOUT)
        self.assertIsNotNone(result)
        self.assertEqual(result["series"], "test-series")
        self.assertEqual(
            result["protocol_path"],
            "/srv/meeting-notary/protocols/test-series/2026-05-29.md",
        )
        self.assertEqual(result["delivery"]["status"], "sent")
        self.assertEqual(result["delivery"]["chat_id"], -1001)

    def test_returns_none_on_empty(self):
        self.assertIsNone(collector._parse_finalize_result(""))
        self.assertIsNone(collector._parse_finalize_result(None))

    def test_returns_none_on_no_json(self):
        self.assertIsNone(
            collector._parse_finalize_result("просто лог без JSON"),
        )

    def test_fallback_without_marker(self):
        """JSON без `Protocol written` маркера всё ещё парсится (fallback)."""
        stdout = """какие-то логи
{
  "ok": true,
  "series": "fallback-series",
  "delivery": {"status": "sent"}
}
"""
        result = collector._parse_finalize_result(stdout)
        self.assertEqual(result["series"], "fallback-series")

    def test_traceback_above_json_does_not_break(self):
        """Если выше в stdout есть traceback с `{...}`, маркер anchors на правильный JSON."""
        stdout = (
            "Traceback (most recent call last):\n"
            '  File "foo.py", line 1, in <module>\n'
            "    raise ValueError({'wrong': 'json'})\n"
            "Protocol written → /tmp/x.md\n"
            '{\n  "series": "correct-series",\n  "ok": true\n}\n'
        )
        result = collector._parse_finalize_result(stdout)
        self.assertEqual(result["series"], "correct-series")

    def test_status_disabled_is_extracted(self):
        """`delivery.status=disabled` (ENABLE_PROTOCOL_DELIVERY=0) — корректно достаётся.

        finalize-meeting.py всегда печатает JSON через `json.dumps(..., indent=2)`,
        так что блок multi-line. Парсер опирается на `\\n{\\n` маркер.
        """
        stdout = (
            "Protocol written → /tmp/x.md\n"
            '{\n  "series": "s",\n  "delivery": {\n    "status": "disabled"\n  }\n}\n'
        )
        result = collector._parse_finalize_result(stdout)
        self.assertEqual(result["delivery"]["status"], "disabled")


class TestDeliveryDone(unittest.TestCase):

    def test_empty_meta(self):
        self.assertFalse(collector._delivery_done({}))

    def test_legacy_object_success(self):
        self.assertTrue(collector._delivery_done({
            "delivered": {"chat_id": 1, "message_ids": [10]},
        }))

    def test_partial_failure_not_done(self):
        """`decision=partial-failure` НЕ считается завершённой доставкой."""
        self.assertFalse(collector._delivery_done({
            "delivered": [
                {"chat_id": 1, "message_ids": [10], "decision": "partial-failure"},
            ],
        }))

    def test_array_with_success(self):
        self.assertTrue(collector._delivery_done({
            "delivered": [
                {"chat_id": 1, "message_ids": [10], "decision": "partial-failure"},
                {"chat_id": 2, "message_ids": [20]},  # success
            ],
        }))


if __name__ == "__main__":
    unittest.main()
