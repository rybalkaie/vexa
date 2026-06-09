"""Ф7 D3 + D4 — safety-классификатор слоя + ратчет private→company.

Покрывает:
  • D3 — дефолт «при сомнении → me/ (приватно)»: неклассифицированный/1-на-1/
    неразмеченный/неизвестная компания факт уходит PRIVATE;
  • D4 — ратчет: после команды «переноси в контекст» факт уезжает COMPANY и ВПРЕДЬ
    знание такого рода → COMPANY (запоминание);
  • D5 на входе роутера — креды → DROP (никуда).

Без IO/pyyaml (ратчет-стейт в темп-файле через env) — зелёные на системном python3.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_knowledge_routing -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.lib import knowledge_ratchet as kr  # noqa: E402
from notary.lib import knowledge_router as router  # noqa: E402


class _RatchetTempMixin(unittest.TestCase):
    """Изолирует ратчет-стейт в темп-файл (env override)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("NOTARY_KNOWLEDGE_RATCHET_PATH")
        os.environ["NOTARY_KNOWLEDGE_RATCHET_PATH"] = str(Path(self._tmp.name) / "ratchet.json")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("NOTARY_KNOWLEDGE_RATCHET_PATH", None)
        else:
            os.environ["NOTARY_KNOWLEDGE_RATCHET_PATH"] = self._old
        self._tmp.cleanup()


class TestSafetyDefaultPrivate(_RatchetTempMixin):
    """D3: при сомнении → PRIVATE."""

    def test_unknown_company_is_private(self):
        d = router.classify_destination("оффер", kind="term", company=None)
        self.assertTrue(d.is_private)
        self.assertEqual(d.reason, "default-private-no-company")

    def test_known_company_but_not_published_is_private(self):
        # Компания известна, но публикация НЕ разрешена и ратчета нет → приватно (D3).
        d = router.classify_destination("оффер", kind="term", company="anzhee",
                                        publication_allowed=False)
        self.assertTrue(d.is_private)
        self.assertEqual(d.reason, "default-private")

    def test_publication_allowed_goes_company(self):
        # E1: групповая размеченной компании → публикуемо.
        d = router.classify_destination("оффер", kind="term", company="anzhee",
                                        publication_allowed=True)
        self.assertTrue(d.is_company)
        self.assertEqual(d.company, "anzhee")
        self.assertEqual(d.reason, "publication-allowed")


class TestCredsDropped(_RatchetTempMixin):
    """D5: креды на входе роутера → DROP (никуда), даже если публикация разрешена."""

    def test_credential_drops_over_company(self):
        d = router.classify_destination("ghp_" + "A" * 36, kind="term",
                                        company="anzhee", publication_allowed=True)
        self.assertTrue(d.is_drop)
        self.assertEqual(d.reason, "credential")

    def test_credential_in_role_value(self):
        d = router.classify_destination("токен: ghp_" + "B" * 36, kind="roster-role",
                                        company="anzhee", publication_allowed=True)
        self.assertTrue(d.is_drop)


class TestRatchetRemembers(_RatchetTempMixin):
    """D4: команда «переноси» повышает и ЗАПОМИНАЕТ направление."""

    def test_before_command_private_after_command_company(self):
        # До команды — приватно (D3).
        d0 = router.classify_destination("Сона Енгибарян | коммерция",
                                         kind="roster-role", company="anzhee")
        self.assertTrue(d0.is_private)
        # Владелец командует «переноси в контекст» про роль anzhee.
        rule = kr.note_promote_command("переноси в контекст компании",
                                       kind="roster-role", company="anzhee")
        self.assertIsNotNone(rule)
        # Впредь ТАКИЕ (тот же kind, та же компания) → COMPANY (D4 запоминание).
        d1 = router.classify_destination("Дарья Набережная | резервы",
                                         kind="roster-role", company="anzhee")
        self.assertTrue(d1.is_company)
        self.assertEqual(d1.company, "anzhee")
        self.assertEqual(d1.reason, "ratchet-promoted")

    def test_ratchet_is_company_scoped(self):
        # Повысили роли anzhee — mpfirst-роль это НЕ повышает (другой репо).
        kr.note_promote_command("переноси в контекст", kind="roster-role", company="anzhee")
        d = router.classify_destination("Кто-то | склад", kind="roster-role",
                                        company="mpfirst")
        self.assertTrue(d.is_private)

    def test_ratchet_is_kind_scoped(self):
        # Повысили роли — термин этим правилом НЕ повышается.
        kr.note_promote_command("переноси в контекст", kind="roster-role", company="anzhee")
        d = router.classify_destination("оффер", kind="term", company="anzhee")
        self.assertTrue(d.is_private)

    def test_explicit_company_in_command_wins(self):
        # Команда называет компанию явно — она и запоминается.
        kr.note_promote_command("фиксируй это в контекст mpfirst", kind="term", company=None)
        self.assertTrue(router.classify_destination("Bolong", kind="term", company="mpfirst").is_company)
        self.assertTrue(router.classify_destination("оффер", kind="term", company="anzhee").is_private)


class TestParsePromoteCommand(unittest.TestCase):
    """Чистый разбор команды повышения (без IO)."""

    def test_recognised_forms(self):
        for txt in (
            "переноси в контекст компании",
            "перенеси это в общий мозг",
            "фиксируй в контекст",
            "в контекст компании",
            "это в общую базу",
            "добавляй в командный репозиторий",
        ):
            with self.subTest(txt=txt):
                self.assertIsNotNone(kr.parse_promote_command(txt))

    def test_not_a_command(self):
        for txt in ("спасибо, всё верно", "откати РЕЦ", "поставки ведёт Мария", "", None):
            with self.subTest(txt=txt):
                self.assertIsNone(kr.parse_promote_command(txt))

    def test_company_extracted(self):
        self.assertEqual(kr.parse_promote_command("переноси в контекст anzhee")["company"], "anzhee")
        self.assertEqual(kr.parse_promote_command("фиксируй в контекст мпервый")["company"], "mpfirst")
        self.assertIsNone(kr.parse_promote_command("переноси в контекст")["company"])

    def test_scope_one_time(self):
        self.assertEqual(kr.parse_promote_command("перенеси только это в контекст")["scope"], "key")
        self.assertEqual(kr.parse_promote_command("переноси в контекст")["scope"], "kind")


class TestRatchetKeyScope(_RatchetTempMixin):
    """scope='key' повышает ТОЛЬКО конкретный факт, не род."""

    def test_one_time_promotion_does_not_generalise(self):
        kr.note_promote_command("перенеси только это в контекст", kind="term",
                                key="оффер", company="anzhee")
        # Конкретный факт повышен.
        self.assertTrue(router.classify_destination("оффер", kind="term", company="anzhee").is_company)
        # Другой термин того же рода — НЕ повышен (scope=key, не kind).
        self.assertTrue(router.classify_destination("АКБ", kind="term", company="anzhee").is_private)


class TestListenerPromoteHook(_RatchetTempMixin):
    """D4 end-to-end: reply «переноси в контекст» на дайджесте знания запоминает
    ратчет; reply «откати …» по-прежнему уходит в откат (регрессия не сломана)."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from notary import meetings_listener as ml
        self.ml = ml
        self.sent = []
        self._sm = mock.patch.object(
            ml, "send_message",
            side_effect=lambda token, chat_id, text, **kw: self.sent.append((chat_id, text, kw)),
        )
        self._sm.start()

    def tearDown(self):
        self._sm.stop()
        super().tearDown()

    def test_promote_reply_remembers_ratchet(self):
        handled = self.ml.maybe_route_to_learning_rollback(
            "tok", 1, {"text": "переноси в контекст anzhee", "message_id": 9})
        self.assertTrue(handled)
        # Ратчет запомнен → впредь термины anzhee → company (D4).
        self.assertTrue(router.classify_destination("оффер", kind="term", company="anzhee").is_company)
        self.assertIn("впредь такие термины", self.sent[-1][1])

    def test_rollback_reply_not_hijacked(self):
        # «откати …» НЕ распознаётся как промоут → уходит в откат (возвращает True,
        # ратчет не запомнен).
        handled = self.ml.maybe_route_to_learning_rollback(
            "tok", 1, {"text": "откати РЕЦ", "message_id": 9})
        self.assertTrue(handled)
        self.assertTrue(router.classify_destination("оффер", kind="term", company="anzhee").is_private)

    def test_one_time_promote_does_not_broaden(self):
        # Цикл5 Н1: «переноси ТОЛЬКО ЭТО» на дайджесте не привязано к конкретному
        # термину (per-fact = Ф8). Раньше listener молча писал kind-level правило
        # (remember_promotion scope=key, key=None → вырождение в kind) и повышал
        # ВСЕ будущие термины — вопреки «только это». Теперь правило НЕ пишется.
        handled = self.ml.maybe_route_to_learning_rollback(
            "tok", 1, {"text": "переноси только это в контекст anzhee", "message_id": 9})
        self.assertTrue(handled)
        self.assertTrue(router.classify_destination("оффер", kind="term", company="anzhee").is_private)
        self.assertIn("пока не поддержан", self.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
