# -*- coding: utf-8 -*-
"""План `ai-owns-reply-mapping` (ISS-44 добор, боевой инцидент 23.07): reply на
ПЕРЕОТПРАВЛЕННУЮ копию дайджеста/протокола — у ЭТОГО message_id своего слепка нет (слепок
пишется под id первой отправки), участник ссылается на номера «13,14,15» → раньше слепок
не резолвился (None), промпт уходил БЕЗ нумерации, ИИ ронял числа в правку/no_match, бот
молчал/«не понял». Фолбэк: узнаём отвечаемое сообщение как нумерованную поверхность серии
и берём самый свежий слепок ТОЙ ЖЕ даты.

Офлайн, без ИИ и без сети — проверяем ТОЛЬКО резолвер слепка."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import series_memory as sm  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402


_POS = [f"Пункт {i}" for i in range(1, 19)]


class SnapshotFallbackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sd = Path(self._tmp.name) / "anzhee-direktorat"
        self.sd.mkdir(parents=True)
        # Слепок сохранён под message_id ПЕРВОЙ отправки (409), дата 2026-07-22.
        sm.record_open_tasks_snapshot(self.sd, 409, list(_POS), surface="digest",
                                      date="2026-07-22", chat_id=222)

    def tearDown(self):
        self._tmp.cleanup()

    def _msg(self, reply_to_id, caption):
        return {"chat": {"id": 222}, "message_id": 900,
                "reply_to_message": {"message_id": reply_to_id, "caption": caption}}

    def test_exact_message_id_resolves(self):
        """Прямой путь: reply на message_id со слепком → тот же слепок."""
        r = ml._reply_open_tasks_snapshot(self.sd, self._msg(409, "неважно"))
        self.assertEqual(r, _POS)

    def test_resent_copy_falls_back_to_latest_same_date(self):
        """ГЛАВНОЕ: reply на КОПИЮ дайджеста (id 777, слепка нет), но подпись узнаётся как
        открытые вопросы этой даты → фолбэк на свежий слепок 2026-07-22."""
        cap = "Открытые вопросы к встрече — Anzhee direktorat, 2026-07-22 Открытых вопросов: 18"
        r = ml._reply_open_tasks_snapshot(self.sd, self._msg(777, cap))
        self.assertEqual(r, _POS, "должен подтянуть свежий слепок серии на ту же дату")

    def test_fallback_ignores_when_not_open_tasks_surface(self):
        """Reply на ПРОИЗВОЛЬНОЕ сообщение без маркера поверхности → НЕ подставляем слепок
        (иначе номера из чужого сообщения ложно замапились бы)."""
        r = ml._reply_open_tasks_snapshot(self.sd, self._msg(777, "Привет, как дела? 2026-07-22"))
        self.assertIsNone(r)

    def test_fallback_requires_date_in_caption(self):
        """R5: узнаваемая поверхность, но БЕЗ даты в подписи → НЕ гадаем (honest-остаток).
        Без даты нельзя гарантировать, что свежий слепок = тот список, что человек видел."""
        cap = "Открытые вопросы к встрече — Anzhee direktorat, Открытых вопросов: 18"
        r = ml._reply_open_tasks_snapshot(self.sd, self._msg(777, cap))
        self.assertIsNone(r)

    def test_fallback_date_mismatch_no_wrong_snapshot(self):
        """Безопасность нумерации: подпись другой даты (нет слепка на неё) → None, а не
        слепок другого дня (номер не должен биться в чужой список)."""
        cap = "Открытые вопросы к встрече — Anzhee direktorat, 2026-07-25 Открытых вопросов: 5"
        r = ml._reply_open_tasks_snapshot(self.sd, self._msg(777, cap))
        self.assertIsNone(r)

    def test_protocol_surface_recognized(self):
        """Протокол встречи — тоже нумерованная поверхность → фолбэк работает при дате в подписи."""
        r = ml._reply_open_tasks_snapshot(self.sd, self._msg(777, "Протокол встречи Anzhee 2026-07-22"))
        self.assertEqual(r, _POS)

    def test_latest_snapshot_date_filter(self):
        """`latest_open_tasks_snapshot` фильтрует по дате: другой день → None."""
        self.assertEqual(sm.latest_open_tasks_snapshot(self.sd, date="2026-07-22"), _POS)
        self.assertIsNone(sm.latest_open_tasks_snapshot(self.sd, date="2026-07-25"))

    def test_latest_snapshot_picks_newest(self):
        """Два слепка одной даты → берётся самый свежий по `at`."""
        newer = [f"Новый {i}" for i in range(1, 4)]
        sm.record_open_tasks_snapshot(self.sd, 500, list(newer), surface="digest",
                                      date="2026-07-22", chat_id=222,
                                      at="2999-01-01T00:00:00+00:00")
        self.assertEqual(sm.latest_open_tasks_snapshot(self.sd, date="2026-07-22"), newer)


if __name__ == "__main__":
    unittest.main()
