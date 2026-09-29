"""Порталовая встреча не тащит в протокол хвост «вопросы с прошлых встреч».

ЧТО СЛУЧИЛОСЬ 18.08.2026. «Еженедельная координация» переехала на портал, и её
протокол приехал в рабочую группу с разделом «Вопросы с прошлых встреч, по
которым не ясен статус» на 18 пунктов — хотя у этой серии перенос ВЫКЛЮЧЕН
(в реестре режим не задан → дефолт `last-only`), а висяки владелец разбирает
отдельным утренним дайджестом.

Корень: порталовая серия зовётся `portal-<id встречи>` и в `watched.yaml` её нет
и быть не может (slug свой у каждой встречи). А `open_questions_mode` на «серии
нет в реестре» отвечает `carry` — сознательный fail-safe, чьё обоснование
(«встреча, дошедшая до протокола, в реестре есть по построению») для порталовой
встречи неверно целиком. Память при этом приехала фолбэком по составу участников
из СТАРОЙ серии — вместе с её хвостом.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_portal_series_open_questions -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from notary.lib import series_memory  # noqa: E402

import os  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

# Ремонт 29.09.2026: портальная серия без истории теперь читает режим из СВОЕЙ выжимки
# на диске (`portal_series_stored_mode` → `protocols_root()`). PORTAL_SERIES ниже —
# живой id встречи 18.08, его папка есть в боевом зеркале. Без изоляции тест читал бы
# живые файлы разработчика: пустой временный каталог протоколов на весь модуль.
_TMP_ROOT = None
_ENV = None


def setUpModule():  # noqa: N802 — имя задаёт unittest
    global _TMP_ROOT, _ENV
    _TMP_ROOT = tempfile.TemporaryDirectory()
    _ENV = mock.patch.dict(os.environ, {"MEETING_NOTARY_PROTOCOLS_DIR": _TMP_ROOT.name})
    _ENV.start()


def tearDownModule():  # noqa: N802
    _ENV.stop()
    _TMP_ROOT.cleanup()

# Реестр в форме, которую отдаёт `load_watched_for_modes`: серия координации без
# поля режима (→ дефолт `last-only`) и Директорат с явным `carry`.
WATCHED = {
    "watched": [
        {"id": "series-ezhenedelnaya-koordinaciya-8399ea",
         "series": "series-ezhenedelnaya-koordinaciya-8399ea",
         "enabled": False},
        {"id": "anzhee-direktorat", "series": "anzhee-direktorat",
         "open_questions_mode": "carry", "enabled": True},
    ],
}

PORTAL_SERIES = "portal-0ebc0725-24d1-4ee6-8e9d-7076601264e8"


def digest(series: str, date: str = "2026-08-11") -> dict:
    return {"series": series, "date": date, "open_tasks": ["Кто-то: что-то"]}


class TestPortalSeriesInheritsMode(unittest.TestCase):
    def test_inherits_last_only_from_source_series(self):
        """Хвост приехал из серии координации — её правило и действует."""
        mode = series_memory.open_questions_mode_for_meeting(
            PORTAL_SERIES,
            digests=[digest("series-ezhenedelnaya-koordinaciya-8399ea")],
            watched=WATCHED,
        )
        self.assertEqual(mode, "last-only")
        self.assertFalse(series_memory.carries_open_questions(
            PORTAL_SERIES,
            digests=[digest("series-ezhenedelnaya-koordinaciya-8399ea")],
            watched=WATCHED,
        ), "раздел «с прошлых встреч» снова печатается в протокол")

    def test_inherits_carry_when_source_series_carries(self):
        """Обратное тоже верно: Директорат остаётся серией-накоплением."""
        self.assertTrue(series_memory.carries_open_questions(
            PORTAL_SERIES, digests=[digest("anzhee-direktorat")], watched=WATCHED,
        ), "перевод Директората на портал потерял бы его висяки")

    def test_no_memory_at_all_uses_registry_default(self):
        """Прошлых встреч нет — дефолт реестра (справка), а не fail-safe carry."""
        self.assertEqual(
            series_memory.open_questions_mode_for_meeting(
                PORTAL_SERIES, digests=[], watched=WATCHED),
            "last-only",
        )

    def test_own_digests_do_not_count_as_source(self):
        """Своя же выжимка источником не считается — иначе наследовать не у кого."""
        self.assertEqual(
            series_memory.open_questions_mode_for_meeting(
                PORTAL_SERIES, digests=[digest(PORTAL_SERIES)], watched=WATCHED),
            "last-only",
        )

    def test_non_portal_series_behaviour_unchanged(self):
        """Обычные серии резолвятся как раньше — реестром."""
        self.assertTrue(series_memory.carries_open_questions(
            "anzhee-direktorat", watched=WATCHED))
        self.assertFalse(series_memory.carries_open_questions(
            "series-ezhenedelnaya-koordinaciya-8399ea", watched=WATCHED))
        # Серии нет в реестре вовсе → прежний fail-safe `carry` (не порталовая).
        self.assertTrue(series_memory.carries_open_questions(
            "kakaya-to-drugaya-seriya", watched=WATCHED))


if __name__ == "__main__":
    unittest.main()
