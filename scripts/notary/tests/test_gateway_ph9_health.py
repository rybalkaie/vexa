"""Шлюз Ватсона (Ф9): глубокая проверка живости. Она обязана ХОДИТЬ, а не отвечать.

Инцидент 17.07.2026: плоский `/health` отвечал 200, пока жив процесс, — а служба
под ним была мертва, и проект три дня не знал, что записи не идут. Отсюда правило
фазы: `/health?deep=1` реально проходит путь до планировщика.

Проба, которая это доказывает, обязана быть ЗЛОЙ: она ломает планировщик и требует
красноты. Проба «дёрнул health, пришло 200» не отличает живой шлюз от константы.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from notary.tests.gateway_harness import EnvSandbox, LiveGateway  # noqa: E402

import notary.gateway as gateway  # noqa: E402
import notary.lib.state as state_lib  # noqa: E402
import notary.scheduler as scheduler  # noqa: E402


def _checks_by_name(checks):
    return {c["check"]: c for c in checks}


class TestDeepProbeReallyWalksTheScheduler(unittest.TestCase):
    def setUp(self) -> None:
        self.env = EnvSandbox()
        # Планировщик «жив»: свежая отметка успешного скана.
        state_lib.write_last_success(datetime.now(timezone.utc))

    def tearDown(self) -> None:
        self.env.restore()

    def test_healthy_state_is_green_and_names_its_checks(self) -> None:
        ok, checks = gateway.deep_probe()
        self.assertTrue(ok, checks)
        names = set(_checks_by_name(checks))
        self.assertEqual(names, {"orders_registry", "scheduler_expand", "scheduler_queue"})

    def test_broken_scheduler_turns_probe_red(self) -> None:
        """🔴 Главная проба фазы: сломался планировщик — здесь красно.

        Мутация, которая красит её: сделать deep-health константой
        (`return True, [{"check": "scheduler", "ok": True}]`). Тогда шлюз снова
        начнёт рапортовать «жив» о мёртвой службе — ровно как 17.07.
        """
        with mock.patch.object(
            scheduler, "_expand_one_off", side_effect=RuntimeError("планировщик упал")
        ):
            ok, checks = gateway.deep_probe()
        self.assertFalse(ok, "шлюз назвал упавший планировщик здоровым")
        expand = _checks_by_name(checks)["scheduler_expand"]
        self.assertFalse(expand["ok"])
        self.assertIn("RuntimeError", expand["detail"])

    def test_scheduler_that_returns_nothing_is_red(self) -> None:
        """Тихий отказ (вернул пусто вместо элемента очереди) — тоже красно."""
        with mock.patch.object(scheduler, "_expand_one_off", return_value=[]):
            ok, checks = gateway.deep_probe()
        self.assertFalse(ok)
        self.assertFalse(_checks_by_name(checks)["scheduler_expand"]["ok"])

    def test_scheduler_that_returns_wrong_shape_is_red(self) -> None:
        with mock.patch.object(
            scheduler, "_expand_one_off", return_value=[{"event_id": "gcal:чужой"}]
        ):
            ok, checks = gateway.deep_probe()
        self.assertFalse(ok)

    def test_probe_uses_the_real_expander_with_a_real_record(self) -> None:
        """Проверка не обходит планировщик «своей копией логики»: зовём настоящую."""
        seen = {}
        real = scheduler._expand_one_off

        def spy(rec, rooms, start, end):
            seen["rec"] = rec
            return real(rec, rooms, start, end)

        with mock.patch.object(scheduler, "_expand_one_off", side_effect=spy):
            ok, _ = gateway.deep_probe()
        self.assertTrue(ok)
        self.assertEqual(seen["rec"]["type"], "one-off")
        self.assertTrue(seen["rec"]["room"].startswith("https://"))

    def test_silent_scheduler_turns_probe_red(self) -> None:
        """Планировщик молчит дольше порога — красно.

        Это и есть урок 17.07: процесс жив, отметок нет, а health зелёный.
        Мутация, которая красит: снова только печатать возраст отметки, не судя о нём.
        """
        state_lib.write_last_success(datetime.now(timezone.utc) - timedelta(hours=6))
        ok, checks = gateway.deep_probe()
        self.assertFalse(ok)
        queue_check = _checks_by_name(checks)["scheduler_queue"]
        self.assertFalse(queue_check["ok"])
        self.assertIn("молчит", queue_check["detail"])

    def test_scheduler_that_never_reported_is_red(self) -> None:
        state_lib.last_success_file().unlink()
        ok, checks = gateway.deep_probe()
        self.assertFalse(ok)
        self.assertIn("ни разу", _checks_by_name(checks)["scheduler_queue"]["detail"])

    def test_stale_threshold_is_configurable(self) -> None:
        state_lib.write_last_success(datetime.now(timezone.utc) - timedelta(hours=6))
        os.environ["MEETING_NOTARY_GATEWAY_SCHEDULER_STALE_MINUTES"] = "1000"
        try:
            ok, _ = gateway.deep_probe()
        finally:
            os.environ.pop("MEETING_NOTARY_GATEWAY_SCHEDULER_STALE_MINUTES", None)
        self.assertTrue(ok)

    def test_corrupt_orders_registry_turns_probe_red(self) -> None:
        (self.env.registry_dir / ".orders.json").write_text("{битый", encoding="utf-8")
        ok, checks = gateway.deep_probe()
        self.assertFalse(ok)
        self.assertFalse(_checks_by_name(checks)["orders_registry"]["ok"])

    def test_probe_never_touches_telegram(self) -> None:
        """Проверка живости не имеет права писать владельцу в Telegram.

        Иначе сторож, дёргающий её каждые полчаса, сам станет источником шума —
        и его отключат, как отключают ложные тревоги.
        """
        with mock.patch.object(
            scheduler, "push", side_effect=AssertionError("deep-health полез в Telegram")
        ) as pushed:
            ok, _ = gateway.deep_probe()
        self.assertTrue(ok)
        pushed.assert_not_called()

    def test_probe_does_not_write_anything_into_the_registry(self) -> None:
        """Синтетическая запись проверки не смеет попасть ни в очередь, ни в реестр."""
        before = sorted(p.name for p in self.env.registry_dir.iterdir())
        gateway.deep_probe()
        after = sorted(p.name for p in self.env.registry_dir.iterdir())
        self.assertEqual(before, after, "глубокая проверка что-то записала в реестр")

    def test_probe_does_not_leave_a_probe_order(self) -> None:
        import notary.lib.orders as orders

        gateway.deep_probe()
        self.assertEqual(orders.list_orders(), [])


class TestHealthOverHttp(unittest.TestCase):
    def setUp(self) -> None:
        self.env = EnvSandbox()
        state_lib.write_last_success(datetime.now(timezone.utc))
        self.gw = LiveGateway()

    def tearDown(self) -> None:
        self.gw.stop()
        self.env.restore()

    def test_flat_health_is_200(self) -> None:
        resp = self.gw.request("GET", "/health")
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.json()["deep"])

    def test_deep_health_is_200_and_carries_checks(self) -> None:
        resp = self.gw.request("GET", "/health?deep=1")
        self.assertEqual(resp.status, 200, resp.body[:400])
        payload = resp.json()
        self.assertTrue(payload["deep"])
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["checks"]), 3)

    def test_deep_health_is_503_when_the_path_is_broken(self) -> None:
        """Сторож портала будит владельца по КОДУ ответа, а не по телу."""
        with mock.patch.object(
            scheduler, "_expand_one_off", side_effect=RuntimeError("планировщик упал")
        ):
            deep = self.gw.request("GET", "/health?deep=1")
            flat = self.gw.request("GET", "/health")
        self.assertEqual(deep.status, 503)
        self.assertFalse(deep.json()["ok"])
        # Плоский health при этом остаётся 200 — именно поэтому сторож обязан
        # ходить с `?deep=1`, и это записано в инструкции деплоя.
        self.assertEqual(flat.status, 200)

    def test_deep_flag_accepts_the_usual_spellings(self) -> None:
        for value in ("1", "true", "yes"):
            with self.subTest(value=value):
                resp = self.gw.request("GET", "/health?deep=%s" % value)
                self.assertTrue(resp.json()["deep"])
        for value in ("0", "false", ""):
            with self.subTest(value=value):
                resp = self.gw.request("GET", "/health?deep=%s" % value)
                self.assertFalse(resp.json()["deep"])


class TestListensLocallyOnly(unittest.TestCase):
    """Голый порт наружу не смотрит: снаружи только Caddy с TLS."""

    def setUp(self) -> None:
        self.env = EnvSandbox()

    def tearDown(self) -> None:
        self.env.restore()

    def test_defaults_are_local_port_8789(self) -> None:
        self.assertEqual(gateway.DEFAULT_HOST, "127.0.0.1")
        self.assertEqual(gateway.DEFAULT_PORT, 8789)

    def test_server_binds_loopback_by_default(self) -> None:
        os.environ["MEETING_NOTARY_GATEWAY_PORT"] = "0"
        server = gateway.build_server()
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()
            os.environ.pop("MEETING_NOTARY_GATEWAY_PORT", None)


if __name__ == "__main__":
    unittest.main()
