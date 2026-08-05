"""Сторож: пути состояния планировщика резолвятся ЛЕНИВО и не утекают в боевой каталог.

Заведён на приёмке 05.08.2026 (см. `plans/follow-ups-open-questions-mode-per-series.md`,
ФУ-ПР-1). История дефекта: `lib/state.py` вычислял `STATE_FILE` на ИМПОРТЕ модуля, поэтому
`MEETING_NOTARY_REGISTRY_DIR`, выставленная тестом после первого импорта соседним модулем,
уже не действовала. В общем прогоне `state_acquire()` писала фиктивные `event_id` в БОЕВОЙ
`~/Projects/me/встречи/.state.json`; записи живут 12 часов и роняли следующий полный прогон.

Почему это важнее теста: `state_acquire` — барьер «встреча уже стартовала». Мусор в нём на
сервере означает, что бот молча НЕ придёт на настоящую встречу (зеркальный отказ к ISS-54).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

import notary.lib.state as state  # noqa: E402


class TestStatePathsResolveLazily(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("MEETING_NOTARY_REGISTRY_DIR")
        self.tmp = tempfile.mkdtemp(prefix="state-isolation-")
        os.environ["MEETING_NOTARY_REGISTRY_DIR"] = self.tmp

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_REGISTRY_DIR", None)
        else:
            os.environ["MEETING_NOTARY_REGISTRY_DIR"] = self._saved

    def test_state_path_follows_env_changed_after_import(self) -> None:
        """env, выставленная ПОСЛЕ импорта модуля, обязана действовать."""
        self.assertEqual(state.state_file().parent, Path(self.tmp))
        self.assertEqual(state.queue_file().parent, Path(self.tmp))
        self.assertEqual(state.last_success_file().parent, Path(self.tmp))

    def test_acquire_writes_only_inside_env_dir(self) -> None:
        """Захват встречи пишет в каталог из env и никуда больше."""
        self.assertTrue(state.state_acquire("gcal:isolation-probe"))
        written = Path(self.tmp) / ".state.json"
        self.assertTrue(written.exists(), "состояние не легло в каталог из env")
        self.assertIn("gcal:isolation-probe", written.read_text(encoding="utf-8"))

    def test_acquire_never_touches_real_registry(self) -> None:
        """Прямая проверка того, чем дефект был опасен: боевой файл не трогается.

        Мутация, которая красит этот тест: вернуть в `state_acquire` обращение к
        константе `STATE_FILE` вместо `state_file()`.
        """
        real = Path.home() / "Projects" / "me" / "встречи" / ".state.json"
        before = real.read_text(encoding="utf-8") if real.exists() else None
        state.state_acquire("gcal:isolation-must-not-leak")
        after = real.read_text(encoding="utf-8") if real.exists() else None
        self.assertEqual(before, after, "тест записал в БОЕВОЙ файл состояния встреч")

    def test_second_acquire_within_ttl_is_refused(self) -> None:
        """Барьер работает по существу, а не только по путям (страховка ISS-54)."""
        now = datetime.now(timezone.utc)
        self.assertTrue(state.state_acquire("gcal:isolation-ttl", now=now))
        self.assertFalse(state.state_acquire("gcal:isolation-ttl", now=now + timedelta(hours=1)))
        later = now + timedelta(hours=state.STATE_TTL_HOURS + 1)
        self.assertTrue(state.state_acquire("gcal:isolation-ttl", now=later))


if __name__ == "__main__":
    unittest.main()
