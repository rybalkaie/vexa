"""С10 (фаза 3 плана telemost-update-resilience): строгая сверка адреса ссылки.

Раньше хост сверяли подстрокой, и `https://evil.example/telemost.yandex.ru/j/…`
проходила в календаре (`lib/gcal.py`) и в реестре комнат (`cli/registry.py`).
Проверяем общий помощник `lib/telemost_url.py`, оба места и последний рубеж —
запускатель (`runner._launch_bot`), который на чужой ссылке бота не поднимает.

Ссылки в тесте — выдуманные (нули), настоящих комнат здесь нет.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_telemost_url_c10 -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.cli import registry  # noqa: E402
from notary.lib import gcal, telemost_url  # noqa: E402

REAL = "https://telemost.yandex.ru/j/00000000000000"
REAL_360 = "https://telemost.360.yandex.ru/j/00000000000000"

#: Всё, что раньше проходило подстрокой или что браузер понял бы иначе, чем Python.
EVIL = (
    "https://evil.example/telemost.yandex.ru/j/00000000000000",   # подстрока в пути (сам С10)
    "https://evil.example/?next=https://telemost.yandex.ru/j/0",  # подстрока в запросе
    "https://evil.example/#telemost.yandex.ru",                   # подстрока во фрагменте
    "https://telemost.yandex.ru.evil.example/j/0",                # хост лишь начинается так
    "https://xtelemost.yandex.ru/j/0",                            # похожий, но другой хост
    "https://telemost.yandex.ru@evil.example/j/0",                # «логин» перед чужим хостом
    "https://evil.example\\@telemost.yandex.ru/j/0",              # браузер читает \ как /
    "https://telemost.yandex.ru:8443/j/0",                        # чужой порт
    "https://tele most.yandex.ru/j/0",                            # пробел внутри
    "https://telemost.yandex.ru/j/0\nhttps://evil.example/",       # перевод строки внутри
    "https://telemost.yandex\t.ru/j/0",                           # табуляцию urlsplit выкинул бы молча
    "javascript:alert(1)//telemost.yandex.ru",
    "ftp://telemost.yandex.ru/j/0",
    "",
)


class TelemostUrlTest(unittest.TestCase):
    def test_real_links_pass(self):
        for url in (REAL, REAL_360, "https://TELEMOST.YANDEX.RU/j/0", REAL + "?utm=1",
                    "https://telemost.yandex.ru:443/j/0", "  " + REAL + "  ",
                    "https://sub.telemost.yandex.ru/j/0"):
            self.assertTrue(telemost_url.is_telemost_url(url), url)

    def test_evil_links_rejected(self):
        for url in EVIL:
            self.assertFalse(telemost_url.is_telemost_url(url), url)

    def test_http_only_where_allowed(self):
        self.assertFalse(telemost_url.is_telemost_url("http://telemost.yandex.ru/j/0"))
        self.assertTrue(telemost_url.is_telemost_url("http://telemost.yandex.ru/j/0", schemes=("http", "https")))
        self.assertFalse(telemost_url.is_telemost_url("http://telemost.yandex.ru:8080/j/0", schemes=("http", "https")))

    def test_not_a_string(self):
        for v in (None, 123, b"https://telemost.yandex.ru/j/0", ["x"]):
            self.assertFalse(telemost_url.is_telemost_url(v), repr(v))

    def test_host_matches_is_whole_label(self):
        self.assertTrue(telemost_url.host_matches("zoom.us", ["zoom.us"]))
        self.assertTrue(telemost_url.host_matches("us02web.zoom.us", ["zoom.us"]))
        self.assertFalse(telemost_url.host_matches("evilzoom.us", ["zoom.us"]))
        self.assertFalse(telemost_url.host_matches("", ["zoom.us"]))


class GcalTest(unittest.TestCase):
    def test_video_call_url_is_strict(self):
        self.assertTrue(gcal._is_video_call_url(REAL))
        self.assertTrue(gcal._is_video_call_url("https://us02web.zoom.us/j/1"))
        for url in EVIL:
            self.assertFalse(gcal._is_video_call_url(url), url)

    def test_extract_skips_foreign_page_in_description(self):
        ev = {"description": "Подробности: https://evil.example/telemost.yandex.ru/j/1 и сама встреча " + REAL}
        self.assertEqual(gcal.extract_meeting_url(ev), REAL)

    def test_extract_nothing_when_only_foreign(self):
        ev = {"location": "https://evil.example/telemost.yandex.ru/j/1"}
        self.assertIsNone(gcal.extract_meeting_url(ev))


class RegistryRoomTest(unittest.TestCase):
    def test_real_room_valid(self):
        self.assertEqual(registry.validate_room_record({"name": "t-10", "url": REAL}), [])
        self.assertEqual(registry.validate_room_record({"name": "t-11", "url": REAL_360}), [])

    def test_evil_room_rejected(self):
        for url in EVIL:
            errs = registry.validate_room_record({"name": "t-10", "url": url})
            self.assertTrue(errs, url)

    def test_http_room_rejected(self):
        self.assertTrue(registry.validate_room_record({"name": "t-10", "url": "http://telemost.yandex.ru/j/0"}))


class RunnerGateTest(unittest.TestCase):
    """Последний рубеж: запускатель не поднимает бота на чужой ссылке."""

    def _item(self, url):
        return {
            "meeting_id": "c10-series", "event_id": "gcal:e1", "series": "c10-series",
            "start_at": datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc).isoformat(),
            "duration_minutes": 60, "url": url, "expected_participants": [], "type": "recurring",
        }

    def test_foreign_link_not_launched(self):
        import notary.runner as runner  # noqa: PLC0415

        with mock.patch.object(runner, "push") as push, \
             mock.patch.object(runner.subprocess, "run") as run:
            ok = runner._launch_bot(self._item(EVIL[0]), datetime.now(timezone.utc))
        self.assertFalse(ok)
        run.assert_not_called()
        push.assert_called_once()
        text = push.call_args[0][0]
        self.assertIn("не в Яндекс Телемост", text)
        self.assertNotIn("evil.example", text)  # ссылку в сообщение не кладём

    def test_http_telemost_still_launches(self):
        # из описания события может прийти http:// — Телемост сам уводит на https
        import tempfile  # noqa: PLC0415
        import notary.runner as runner  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(runner, "push"), \
                 mock.patch.object(runner, "LOCAL_DOCKER", True), \
                 mock.patch.object(runner, "RUNS_DIR", Path(tmp)), \
                 mock.patch.object(runner, "get_telegram_chat_id_for_series", return_value=None), \
                 mock.patch.object(runner, "load_watched", return_value={"watched": []}), \
                 mock.patch.object(runner, "_verify_container_alive"), \
                 mock.patch.object(runner.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                ok = runner._launch_bot(self._item("http://telemost.yandex.ru/j/00000000000000"),
                                        datetime.now(timezone.utc))
        self.assertTrue(ok)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
