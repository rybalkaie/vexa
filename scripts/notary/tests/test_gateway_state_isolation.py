"""Сторож: реестр нарядов резолвит путь ЛЕНИВО и не утекает в боевой каталог (Ф9).

Родной брат `test_state_path_isolation.py`. История дефекта та же: `lib/state.py`
вычислял путь на ИМПОРТЕ, и общий прогон писал фиктивные записи в БОЕВОЙ
`~/Projects/me/встречи/.state.json`; они живут 12 часов и роняли следующий прогон
(приёмка 05.08.2026). Реестр нарядов опаснее: в нём лежат ЖИВЫЕ наряды портала —
мусор в нём означает бота, который не приедет на настоящую встречу, или второго
бота в комнате.

Здесь же заперты два инварианта записи, каждый купленный инцидентом:
запись только из-под непривилегированного пользователя (12.08.2026) и отказ
«чинить» битый реестр перезаписью.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.tests.gateway_harness import EnvSandbox, order_payload  # noqa: E402

import notary.lib.orders as orders  # noqa: E402

REAL_ORDERS_FILE = Path.home() / "Projects" / "me" / "встречи" / ".orders.json"


class TestOrdersPathsResolveLazily(unittest.TestCase):
    def setUp(self) -> None:
        self.env = EnvSandbox()

    def tearDown(self) -> None:
        self.env.restore()

    def test_orders_path_follows_env_changed_after_import(self) -> None:
        """env, выставленная ПОСЛЕ импорта модуля, обязана действовать.

        Мутация, которая красит: вычислить путь один раз на импорте
        (`ORDERS_FILE = _registry_dir() / ORDERS_FILENAME`) и вернуть его из
        `orders_file()`.
        """
        self.assertEqual(orders.orders_file().parent, self.env.registry_dir)
        moved = self.env.root / "registry-2"
        moved.mkdir()
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = str(moved)
        self.assertEqual(orders.orders_file().parent, moved)

    def test_write_lands_only_inside_env_dir(self) -> None:
        order, created = orders.put_order(_valid_payload())
        self.assertTrue(created)
        written = self.env.registry_dir / ".orders.json"
        self.assertTrue(written.exists(), "реестр не лёг в каталог из env")
        self.assertIn(order["id"], written.read_text(encoding="utf-8"))

    def test_never_touches_real_registry(self) -> None:
        """Прямая проверка того, чем дефект опасен: боевой файл не трогается."""
        before = (
            REAL_ORDERS_FILE.read_bytes() if REAL_ORDERS_FILE.exists() else None
        )
        orders.put_order(_valid_payload(meeting_id="mtg-must-not-leak"))
        after = REAL_ORDERS_FILE.read_bytes() if REAL_ORDERS_FILE.exists() else None
        self.assertEqual(before, after, "проба записала в БОЕВОЙ реестр нарядов")

    def test_registry_file_is_owner_only(self) -> None:
        """В наряде имена участников и ссылка на комнату — доступ только владельцу."""
        orders.put_order(_valid_payload())
        mode = stat.S_IMODE((self.env.registry_dir / ".orders.json").stat().st_mode)
        self.assertEqual(
            mode & 0o077, 0, "реестр нарядов читаем группой/миром (%o)" % mode
        )

    def test_write_from_root_is_refused(self) -> None:
        """Запись из-под root запрещена: 12.08.2026 файл сменил владельца и бот встал.

        Мутация, которая красит: убрать проверку `_effective_uid() == 0` в `_mutate`.
        """
        original = orders._effective_uid
        orders._effective_uid = lambda: 0  # type: ignore[assignment]
        try:
            with self.assertRaises(orders.OrderRegistryError) as ctx:
                orders.put_order(_valid_payload())
        finally:
            orders._effective_uid = original  # type: ignore[assignment]
        self.assertIn("root", str(ctx.exception))
        self.assertFalse(
            (self.env.registry_dir / ".orders.json").exists(),
            "отказ по правам всё-таки создал файл реестра",
        )

    def test_corrupt_registry_is_not_silently_reset(self) -> None:
        """Битый реестр — это 503 владельцу, а не «начнём с чистого листа».

        Перезапись стёрла бы все живые наряды: встречи дня просто не состоялись бы.
        """
        path = self.env.registry_dir / ".orders.json"
        path.write_text("{сломано", encoding="utf-8")
        with self.assertRaises(orders.OrderRegistryError):
            orders.read_orders()
        with self.assertRaises(orders.OrderRegistryError):
            orders.put_order(_valid_payload())
        self.assertEqual(path.read_text(encoding="utf-8"), "{сломано")

    def test_write_is_atomic_no_partial_file_left(self) -> None:
        """Падение внутри мутации не оставляет ни обрезанного файла, ни временного."""
        orders.put_order(_valid_payload())
        path = self.env.registry_dir / ".orders.json"
        good = path.read_text(encoding="utf-8")

        def _boom(_state: dict) -> None:
            raise RuntimeError("падение в середине мутации")

        with self.assertRaises(RuntimeError):
            orders._mutate(_boom)
        self.assertEqual(path.read_text(encoding="utf-8"), good)
        leftovers = [
            p.name
            for p in self.env.registry_dir.iterdir()
            if p.name.startswith(".orders.json.")
        ]
        self.assertEqual(leftovers, [], "остались временные файлы записи: %s" % leftovers)

    def test_two_writers_do_not_lose_each_other(self) -> None:
        """Блокировка + чтение-под-замком: параллельная запись не теряет наряды."""
        import threading

        errors: list = []

        def worker(idx: int) -> None:
            try:
                orders.put_order(_valid_payload(meeting_id="mtg-par-%d" % idx))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        state = json.loads((self.env.registry_dir / ".orders.json").read_text("utf-8"))
        self.assertEqual(
            len(state["orders"]), 8, "часть нарядов затёрлась при параллельной записи"
        )


def _valid_payload(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    payload, errors = orders.validate_order_payload(
        order_payload(start_at=(now + timedelta(hours=2)).isoformat(), **overrides),
        now=now,
    )
    assert not errors, errors
    return payload


if __name__ == "__main__":
    unittest.main()
