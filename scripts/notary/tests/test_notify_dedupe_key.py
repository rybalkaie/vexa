"""Тест per-meeting ключа дедупа в lib.notify.push (REQ 1.5, план bot-notarius-full).

Покрывает:
  - dedupe_key даёт дедуп ПО ВСТРЕЧЕ: повтор по той же встрече глушится, но
    НОВАЯ потеря WAV по другой встрече доходит, даже если ТЕКСТ совпал;
  - обратная совместимость: без dedupe_key дедуп по тексту (как раньше).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_notify_dedupe_key -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import notify  # noqa: E402


class NotifyDedupeKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # /bin/echo существует, исполняем, возвращает 0 — push «успешен».
        self._orig_bin = notify.TG_SEND_BIN
        self._orig_dir = notify.DEDUP_DIR
        notify.TG_SEND_BIN = Path("/bin/echo")
        notify.DEDUP_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        notify.TG_SEND_BIN = self._orig_bin
        notify.DEDUP_DIR = self._orig_dir

    def test_per_meeting_key_lets_new_meeting_through(self) -> None:
        msg = "Финализация упала: rc=3"  # идентичный текст во всех трёх
        a1 = notify.push(msg, dedupe_key="finalize-fail:meetingA")
        a2 = notify.push(msg, dedupe_key="finalize-fail:meetingA")
        b1 = notify.push(msg, dedupe_key="finalize-fail:meetingB")
        self.assertTrue(a1, "первый алерт по встрече A должен доставиться")
        self.assertFalse(a2, "повтор по встрече A в окне дедупа должен глушиться")
        self.assertTrue(
            b1,
            "НОВАЯ потеря WAV по встрече B должна доходить, хоть текст совпал с A",
        )

    def test_message_dedup_backward_compatible(self) -> None:
        c1 = notify.push("другой текст X", dedupe=True)
        c2 = notify.push("другой текст X", dedupe=True)
        self.assertTrue(c1)
        self.assertFalse(c2, "без dedupe_key поведение прежнее — дедуп по тексту")


if __name__ == "__main__":
    unittest.main()
