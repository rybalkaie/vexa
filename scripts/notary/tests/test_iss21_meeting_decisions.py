"""Тесты Ф2 / R4 (план `2026-06-24-vtoroy-mozg-ai-klon`): забор решений из протокола
встречи в журнал решений (`lib/meeting_decisions.py`, DORMANT-ядро).

Проверяет:
  • fail-closed по приватности — скраб недоступен / остаточный секрет → ОТКАЗ;
  • в промпт уходит СКРАБЛЕННЫЙ текст (не сырой) — порядок «скраб → промпт»;
  • формат промпта (decision_record: статус черновик, источник встреча);
  • портативное разрешение secret_scrub работает на dev (callable);
  • схема транспорт-записи очереди.

В файле НЕТ настоящих секретов (sentinel-токены синтетические) — `secret-scrub
--check` по нему CLEAN.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss21_meeting_decisions -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.lib import meeting_decisions as md  # noqa: E402

_PROTOCOL = (
    "#протоколвстречи 24.06.2026\n\n"
    "## 1) Закупки\n\n"
    "▪️ Решили заказывать Pulsar 63ML на ИП, а не на ООО — экономия на эквайринге.\n"
)


class TestFailClosed(unittest.TestCase):

    def test_scrub_unavailable_refuses(self):
        with mock.patch.object(md, "_resolve_scrub", return_value=None):
            res = md.prepare_extraction(_PROTOCOL, series="s", date="2026-06-24")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "scrub-unavailable")

    def test_residual_secret_blanks_to_refusal(self):
        # Скраб вернул пусто (бэкстоп остаточного секрета) → отказ, не тихий пропуск.
        res = md.prepare_extraction(_PROTOCOL, series="s", date="d", scrub=lambda t: "")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "residual-secret")

    def test_empty_protocol_refused(self):
        res = md.prepare_extraction("   ", series="s", date="d")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "empty-protocol")

    def test_scrub_exception_is_failclosed(self):
        def boom(_t):
            raise RuntimeError("x")
        res = md.prepare_extraction(_PROTOCOL, series="s", date="d", scrub=boom)
        self.assertFalse(res["ok"])
        self.assertTrue(res["reason"].startswith("scrub-error"))


class TestScrubBeforePrompt(unittest.TestCase):
    """Порядок «скраб → промпт»: в промпт попадает СКРАБЛЕННЫЙ текст, не сырой."""

    def test_scrubbed_text_used_not_raw(self):
        raw = _PROTOCOL + "\nпароль доступа: TASKSENTINEL_TOKEN_XYZ\n"
        # Синтетический скраб режет sentinel → placeholder.
        fake = lambda t: t.replace("TASKSENTINEL_TOKEN_XYZ", "[secret:test]")  # noqa: E731
        res = md.prepare_extraction(raw, series="s", date="2026-06-24", scrub=fake)
        self.assertTrue(res["ok"])
        self.assertNotIn("TASKSENTINEL_TOKEN_XYZ", res["prompt"])  # сырой не утёк
        self.assertIn("[secret:test]", res["scrubbed"])


class TestPromptFormat(unittest.TestCase):

    def test_prompt_has_decision_record_hints(self):
        p = md.build_extraction_prompt("тело протокола", series="anzhee-direktorat", date="2026-06-24")
        self.assertIn("статус: черновик", p)
        self.assertIn("источник: встреча", p)
        self.assertIn("2026-06-24", p)
        self.assertIn("решение не выявлено", p)         # ветка «нет решения»
        self.assertIn("тело протокола", p)              # сам протокол в промпте
        self.assertIn("anzhee-direktorat", p)

    def test_ok_path_with_real_scrub(self):
        """Реальный portable-скраб (dev-фолбэк) на доброкачественном протоколе → ok."""
        res = md.prepare_extraction(_PROTOCOL, series="s", date="2026-06-24")
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertIn("Pulsar 63ML", res["prompt"])


class TestResolveScrub(unittest.TestCase):

    def test_resolve_scrub_callable_on_dev(self):
        fn = md._resolve_scrub()
        self.assertTrue(callable(fn))
        # Доброкачественный текст проходит без изменений по сути.
        self.assertIn("привет", fn("привет, мир"))


class TestQueueRecord(unittest.TestCase):

    def test_record_shape(self):
        rec = md.to_queue_record("draft", series="s", date="2026-06-24", meeting_sid="tm-1")
        self.assertEqual(rec["source"], "meeting-protocol")
        self.assertEqual(rec["status"], "черновик")
        self.assertEqual(rec["draft_md"], "draft")
        self.assertEqual(rec["series"], "s")
        self.assertEqual(rec["meeting_sid"], "tm-1")

    def test_queue_path_default(self):
        p = md.queue_path("/tmp/me-mirror")
        self.assertTrue(str(p).endswith("_inbox/notary-decision-drafts.jsonl"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
