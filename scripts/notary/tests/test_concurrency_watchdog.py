"""Тесты Ф3 (bot-notarius-full): concurrency-гейт runner'а + watchdog collector'а.

Закрепляют ЧИСТУЮ логику (без docker/диска), которой принимаются решения:
  • runner._concurrency_decision — пускать ли 2-ю запись (REQ 3.1, 3.3);
  • collector._watchdog_verdicts — какой контейнер зависший (REQ 3.2);
  • collector._parse_docker_time — разбор RFC3339 StartedAt (наносекунды).

«Симуляция» из плана выражена здесь как прогон чистых функций над снимками
состояний: две пересекающиеся встречи = два кандидата над одним снапшотом;
зависший контейнер = контейнер с age/stale за порогом.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_concurrency_watchdog -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))


def _load(mod_name: str, filename: str):
    """Top-level скрипт (collector.py/runner.py) — грузим через importlib без
    выполнения main() (он под `if __name__ == '__main__'`)."""
    spec = importlib.util.spec_from_file_location(mod_name, str(_NOTARY / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Под системным python3 (без PyYAML) импорт runner.py падает на transitive
# `notary.cli.registry` → `import yaml`. Тестируемые здесь функции ЧИСТЫЕ и yaml
# не трогают — если настоящего PyYAML нет (CI/системный python), кладём заглушку,
# чтобы модуль импортировался. Под .venv-cli (yaml 6.x есть) берётся настоящий.
try:
    import yaml  # noqa: F401
except ImportError:
    import types as _types
    sys.modules["yaml"] = _types.ModuleType("yaml")

runner = _load("runner_module", "runner.py")
collector = _load("collector_module_cw", "collector.py")


# ─────────────────────────── runner._concurrency_decision ───────────────────────────

def _empty_snapshot() -> dict:
    return {"by_series": {}, "unlabeled": [], "total": 0}


class TestConcurrencyDecision(unittest.TestCase):
    MAX = 2

    def _decide(self, series, snapshot, launched, count):
        return runner._concurrency_decision(series, snapshot, launched, count, self.MAX)

    def test_launch_when_idle(self):
        action, reason = self._decide("anzhee", _empty_snapshot(), set(), 0)
        self.assertEqual(action, "launch")
        self.assertIsNone(reason)

    def test_second_series_runs_in_parallel(self):
        """REQ 3.1: пока пишется серия X, встреча серии Y СТАРТУЕТ (не блок)."""
        snap = {"by_series": {"tatyana": ["vexa-notarius-auto-1-x"]}, "unlabeled": [], "total": 1}
        action, _ = self._decide("anzhee-direktorat", snap, set(), 1)
        self.assertEqual(action, "launch")

    def test_same_series_already_running_is_skipped(self):
        snap = {"by_series": {"anzhee": ["vexa-notarius-auto-1-x"]}, "unlabeled": [], "total": 1}
        action, reason = self._decide("anzhee", snap, set(), 1)
        self.assertEqual(action, "skip_series")
        self.assertIn("anzhee", reason)

    def test_same_series_launched_this_tick_is_skipped(self):
        """Два кандидата одной серии в одном окне → 2-й конфликт (не дубль)."""
        action, reason = self._decide("dup", _empty_snapshot(), {"dup"}, 1)
        self.assertEqual(action, "skip_series")

    def test_capacity_blocks_third_concurrent(self):
        """REQ 3.1: лимит 2 — 3-я параллельная встреча в очередь с уведомлением."""
        snap = {
            "by_series": {"a": ["c-a"], "b": ["c-b"]},
            "unlabeled": [],
            "total": 2,
        }
        action, reason = self._decide("c-new", snap, set(), 2)
        self.assertEqual(action, "skip_capacity")
        self.assertIn("2/2", reason)

    def test_series_check_precedes_capacity(self):
        """Своя серия уже пишется И лимит достигнут → skip_series (точнее)."""
        snap = {"by_series": {"a": ["c-a"], "b": ["c-b"]}, "unlabeled": [], "total": 2}
        action, _ = self._decide("a", snap, set(), 2)
        self.assertEqual(action, "skip_series")

    def test_unlabeled_legacy_counts_toward_capacity(self):
        """Legacy-бот без label занимает слот лимита, но серию не дедупит."""
        snap = {"by_series": {}, "unlabeled": ["vexa-notarius-legacy"], "total": 1}
        # под лимитом — другая серия стартует
        self.assertEqual(self._decide("x", snap, set(), 1)[0], "launch")
        # на лимите — блок по ёмкости
        self.assertEqual(self._decide("x", snap, set(), 2)[0], "skip_capacity")

    def test_two_overlapping_meetings_both_launch(self):
        """Полная симуляция тика: 2 кандидата РАЗНЫХ серий в одном окне →
        ОБА стартуют параллельно (повторяем логику main-петли)."""
        candidates = [
            {"event_id": "e1", "series": "tatyana"},
            {"event_id": "e2", "series": "anzhee-direktorat"},
        ]
        snapshot = _empty_snapshot()
        running = snapshot["total"]
        launched: set[str] = set()
        results = []
        for cand in candidates:
            series = cand["series"]
            action, _ = self._decide(series, snapshot, launched, running)
            results.append((series, action))
            if action == "launch":
                launched.add(series)
                running += 1
        self.assertEqual(results, [("tatyana", "launch"), ("anzhee-direktorat", "launch")])
        self.assertEqual(running, 2)

    def test_three_overlapping_third_queued(self):
        """3 кандидата разных серий, лимит 2 → 3-я в очередь (skip_capacity)."""
        candidates = ["s1", "s2", "s3"]
        snapshot = _empty_snapshot()
        running = 0
        launched: set[str] = set()
        actions = []
        for series in candidates:
            action, _ = self._decide(series, snapshot, launched, running)
            actions.append(action)
            if action == "launch":
                launched.add(series)
                running += 1
        self.assertEqual(actions, ["launch", "launch", "skip_capacity"])


# ─────────────────────────── collector._watchdog_verdicts ───────────────────────────

class TestWatchdogVerdicts(unittest.TestCase):
    MAX_AGE = 4 * 3600.0   # 4ч
    STALL = 30 * 60.0      # 30 мин

    def _verdicts(self, containers):
        return collector._watchdog_verdicts(containers, max_age_sec=self.MAX_AGE, stall_sec=self.STALL)

    def test_healthy_container_survives(self):
        c = [{"name": "c1", "series": "a", "age_sec": 1800, "stale_sec": 30}]
        self.assertEqual(self._verdicts(c), [])

    def test_age_over_threshold_killed(self):
        c = [{"name": "c1", "series": "a", "age_sec": 5 * 3600, "stale_sec": 10}]
        v = self._verdicts(c)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0][0], "c1")
        self.assertIn("живёт", v[0][2])

    def test_stall_over_threshold_killed(self):
        c = [{"name": "c1", "series": "a", "age_sec": 1800, "stale_sec": 40 * 60}]
        v = self._verdicts(c)
        self.assertEqual(len(v), 1)
        self.assertIn("не растёт", v[0][2])

    def test_both_none_never_killed(self):
        """Невозможно оценить (нет StartedAt И нет файлов) → НЕ трогаем."""
        c = [{"name": "c1", "series": "a", "age_sec": None, "stale_sec": None}]
        self.assertEqual(self._verdicts(c), [])

    def test_age_none_stall_under_survives(self):
        c = [{"name": "c1", "series": "a", "age_sec": None, "stale_sec": 60}]
        self.assertEqual(self._verdicts(c), [])

    def test_age_under_stall_none_survives(self):
        c = [{"name": "c1", "series": "a", "age_sec": 600, "stale_sec": None}]
        self.assertEqual(self._verdicts(c), [])

    def test_age_takes_priority_in_reason(self):
        """age И stall за порогом → причина про age (жёсткий потолок)."""
        c = [{"name": "c1", "series": "a", "age_sec": 5 * 3600, "stale_sec": 40 * 60}]
        v = self._verdicts(c)
        self.assertIn("живёт", v[0][2])

    def test_mixed_fleet_only_stuck_flagged(self):
        c = [
            {"name": "healthy", "series": "x", "age_sec": 1200, "stale_sec": 15},
            {"name": "old", "series": "y", "age_sec": 5 * 3600, "stale_sec": 5},
            {"name": "stalled", "series": "z", "age_sec": 600, "stale_sec": 60 * 60},
        ]
        names = {v[0] for v in self._verdicts(c)}
        self.assertEqual(names, {"old", "stalled"})

    def test_no_name_skipped(self):
        c = [{"name": "", "series": "a", "age_sec": 99 * 3600, "stale_sec": 99 * 60}]
        self.assertEqual(self._verdicts(c), [])

    def test_boundary_exactly_at_threshold_survives(self):
        """Ровно на пороге — не убиваем (строгое >)."""
        c = [{"name": "c1", "series": "a", "age_sec": self.MAX_AGE, "stale_sec": self.STALL}]
        self.assertEqual(self._verdicts(c), [])


# ─────────────────────────── collector._parse_docker_time ───────────────────────────

class TestParseDockerTime(unittest.TestCase):

    def test_nanoseconds_with_z(self):
        dt = collector._parse_docker_time("2026-06-03T11:14:54.243456789Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.minute, 14)
        self.assertEqual(dt.tzinfo, timezone.utc)
        # доля обрезана до микросекунд
        self.assertEqual(dt.microsecond, 243456)

    def test_microseconds(self):
        dt = collector._parse_docker_time("2026-06-03T11:14:54.243456Z")
        self.assertEqual(dt.microsecond, 243456)

    def test_no_fraction(self):
        dt = collector._parse_docker_time("2026-06-03T11:14:54Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.second, 54)

    def test_not_started_sentinel(self):
        self.assertIsNone(collector._parse_docker_time("0001-01-01T00:00:00Z"))

    def test_empty_and_garbage(self):
        self.assertIsNone(collector._parse_docker_time(""))
        self.assertIsNone(collector._parse_docker_time("not a time"))

    def test_age_computation_realistic(self):
        """StartedAt 2.5ч назад → age ≈ 9000с (база для age-триггера)."""
        started = collector._parse_docker_time("2026-06-03T09:00:00Z")
        now = datetime(2026, 6, 3, 11, 30, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual((now - started).total_seconds(), 9000.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
