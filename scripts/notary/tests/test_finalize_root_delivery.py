"""Доставка протокола в группу: два регресса ремонта 18.08.2026.

ЧТО СЛУЧИЛОСЬ. Встреча «Еженедельная координация» прошла через портал, протокол
приехал в карточку — а в рабочую группу не попал: его получил в личку владелец
бота. Причин оказалось две, и вторая маскировала первую.

1. КОРЕНЬ. Финализацию запускают из папки `scripts/notary` (`cd … && python
   finalize-meeting.py`), поэтому её корень пакетов — `lib`, а пакета `notary`
   в `sys.path` нет вовсе. Все внутренние импорты вида `from notary.lib import
   orders` в этом процессе падали с `ModuleNotFoundError`, маршрут «чья это
   встреча» не выводился, и доставка уходила в legacy-фолбэк «нет привязки →
   личка владельца».
2. МАСКИРОВКА. Порталу при этом рапортовали `status: sent, mode: group` —
   `mode` собирал collector, у которого корень `notary` и импорты живые, то
   есть он сообщал НАМЕРЕНИЕ наряда, а не факт отправки.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_finalize_root_delivery -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


def _load_collector_module():
    spec = importlib.util.spec_from_file_location(
        "collector_module_delivery", str(_NOTARY / "collector.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_collector_module()


class TestImportsFromFinalizeRoot(unittest.TestCase):
    """Корень `lib` (процесс финализации) обязан видеть `notary.*`.

    Проверяем ПОДПРОЦЕССОМ в тех же условиях, что и боевой запуск: рабочая папка
    `scripts/notary`, `PYTHONPATH` пуст. В самом тест-прогоне корень `notary`
    доступен, и внутри него этот регресс не воспроизводится в принципе — именно
    поэтому 127 тестов пакета его и не поймали.
    """

    def _run(self, code: str) -> subprocess.CompletedProcess:
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "ENABLE_PORTAL_ORDERS")
        }
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            cwd=str(_NOTARY), env=env, capture_output=True, text=True, timeout=120,
        )

    def test_notary_lib_importable_and_not_duplicated(self):
        proc = self._run(
            """
            import json, sys
            import lib
            from notary.lib import identity, orders, delivery_route
            out = {
                "same_identity": identity is sys.modules["lib.identity"],
                "same_orders": orders is sys.modules["lib.orders"],
                "same_route": delivery_route is sys.modules["lib.delivery_route"],
                # Имена модулей обязаны остаться настоящими: псевдоним не должен
                # переименовывать модуль, иначе в логах и трейсбеках всплывёт
                # второе, несуществующее имя.
                "name_orders": orders.__name__,
                "name_lib": lib.__name__,
            }
            print(json.dumps(out))
            """
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(got["same_identity"], "notary.lib.identity — двойник lib.identity")
        self.assertTrue(got["same_orders"], "notary.lib.orders — двойник lib.orders")
        self.assertTrue(got["same_route"], "notary.lib.delivery_route — двойник")
        self.assertEqual(got["name_orders"], "lib.orders")
        self.assertEqual(got["name_lib"], "lib")

    def test_sibling_packages_importable(self):
        """`notary.cli` и соседи грузятся штатно, без копий кода."""
        proc = self._run(
            """
            import lib
            from notary.cli.registry import VALID_COMPANIES
            assert VALID_COMPANIES, "реестр компаний пуст"
            print("ok")
            """
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])

    def test_route_resolves_instead_of_blocking(self):
        """Маршрут выводится, а не встаёт в `blocked` из-за сбоя импорта.

        `blocked` означает «чья это встреча — НЕ ВЫЯСНЕНО», и вызывающий обязан
        остановить доставку. Именно этим кончался сломанный импорт.
        """
        proc = self._run(
            """
            import json, lib
            from notary.lib import delivery_route
            route = delivery_route.route_for_meeting(
                {"meetingUrl": "https://telemost.yandex.ru/j/000", "startTs": "2026-08-18T08:00:00+00:00"},
                session_uid="test-sid",
            )
            print(json.dumps({"blocked": bool(route.blocked), "mode": route.mode}))
            """
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(got["blocked"], "маршрут встал в blocked — реестр снова не читается")


class TestTelegramDeliveryNoteTellsTheTruth(unittest.TestCase):
    """Рапорт порталу описывает ФАКТ отправки, а не намерение наряда."""

    ORDER = {
        "id": "portal:meeting-1",
        "company": "anzhee",
        "delivery": "group",
        "telegram_chat_id": -1001207402590,
        "meeting_id": "meeting-1",
    }

    def test_group_delivery_reported_as_group(self):
        note = collector._telegram_delivery_note(
            self.ORDER, "sent", {"status": "sent", "chat_id": -1001207402590},
        )
        self.assertEqual(note["status"], "sent")
        self.assertEqual(note["mode"], "group")
        self.assertNotEqual(note.get("reason"), "delivered_elsewhere")

    def test_delivery_to_owner_dm_is_not_reported_as_group(self):
        """Тот самый инцидент: ушло в личку — рапорт обязан сказать «личка»."""
        note = collector._telegram_delivery_note(
            self.ORDER, "sent", {"status": "sent", "chat_id": 359008340},
        )
        self.assertEqual(note["mode"], "dm", "рапорт снова врёт «в группу»")
        self.assertEqual(note["reason"], "delivered_elsewhere")

    def test_no_fact_no_invention(self):
        """Факта нет (старый вызов без объекта доставки) — прежнее поведение."""
        note = collector._telegram_delivery_note(self.ORDER, "skipped")
        self.assertEqual(note["status"], "skipped")
        self.assertNotEqual(note.get("reason"), "delivered_elsewhere")


if __name__ == "__main__":
    unittest.main()
