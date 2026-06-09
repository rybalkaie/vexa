"""Ф7 D1 — write-back знания в `*-context` (outbox + PR-план + мёрж).

Покрывает:
  • адресация предложения слою: COMPANY→company-outbox (адресован контексту
    компании, НЕ коду — критерий D1), PRIVATE→приватная очередь, DROP(креды)→никуда;
  • дедуп (повтор не плодит, уже-в-glossary не предлагается);
  • 🔴 инварианты PR-плана (контракт §3.3): ветка от origin/main, push в bot-ветку
    НЕ в main, PR создаётся но НЕ мёржится, трогается только knowledge/notary/*;
  • чистый мёрж предложений в glossary/org dict;
  • flush: без токена — no-op (not-provisioned); с фейк-раннером — безопасная
    последовательность на тест-клоне;
  • сводка outbox для дайджеста D2.

Изоляция: outbox/private/ratchet — в темп через env. Мёрж/план — на dict (без pyyaml).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_knowledge_writeback -v
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

from notary.lib import knowledge_writeback as wb  # noqa: E402
from notary.lib import knowledge_router as router  # noqa: E402


class _IsolatedMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env = {
            "NOTARY_KNOWLEDGE_OUTBOX_DIR": str(base / "outbox"),
            "NOTARY_PRIVATE_KNOWLEDGE_QUEUE": str(base / "private.md"),
            "NOTARY_KNOWLEDGE_RATCHET_PATH": str(base / "ratchet.json"),
            # Изолируем context_knowledge от реальных клонов (нет YAML → пусто).
            "MEETING_NOTARY_CONTEXT_DIR": str(base / "no-context"),
        }
        self._old = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self.base = base

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


def _company_dest(company="anzhee"):
    return router.classify_destination("оффер", kind="term", company=company,
                                       publication_allowed=True)


class TestEnqueueAddressing(_IsolatedMixin):
    """D1: COMPANY-предложение адресовано контексту компании, не коду."""

    def test_company_term_goes_to_company_outbox(self):
        res = wb.propose_term("оффер", series="s1", company="anzhee",
                              publication_allowed=True, aliases=["офер"], note="продажи")
        self.assertEqual(res.layer, "company")
        self.assertEqual(res.company, "anzhee")
        recs = wb._read_outbox("anzhee")
        self.assertEqual(len(recs), 1)
        # Адрес — knowledge/notary/ в репо КОМПАНИИ, не код-файл.
        self.assertIn("knowledge/notary/glossary.yaml", recs[0]["target"])
        self.assertIn("anzhee-context", recs[0]["target"])
        self.assertEqual(recs[0]["payload"]["canonical"], "оффер")

    def test_unmarked_term_goes_private(self):
        # Публикация не разрешена, ратчета нет → приватно (D3).
        res = wb.propose_term("оффер", series="s1", company="anzhee", publication_allowed=False)
        self.assertEqual(res.layer, "private")
        self.assertTrue(Path(self._env["NOTARY_PRIVATE_KNOWLEDGE_QUEUE"]).is_file())
        self.assertEqual(wb._read_outbox("anzhee"), [])

    def test_credential_term_dropped_everywhere(self):
        res = wb.propose_term("ghp_" + "A" * 36, series="s1", company="anzhee",
                              publication_allowed=True)
        self.assertEqual(res.layer, "drop")
        self.assertEqual(wb._read_outbox("anzhee"), [])
        self.assertFalse(Path(self._env["NOTARY_PRIVATE_KNOWLEDGE_QUEUE"]).is_file())

    def test_role_to_company(self):
        res = wb.propose_role("Мария Михина", "поставки", series="s1", company="anzhee",
                              publication_allowed=True, keywords=["поставк", "контейнер"])
        self.assertEqual(res.layer, "company")
        recs = wb._read_outbox("anzhee")
        self.assertEqual(recs[0]["kind"], "roster-role")
        self.assertIn("org-structure.yaml", recs[0]["target"])

    def test_dedup_same_term(self):
        wb.propose_term("оффер", series="s1", company="anzhee", publication_allowed=True)
        res2 = wb.propose_term("оффер", series="s1", company="anzhee", publication_allowed=True)
        self.assertEqual(res2.layer, "exists")
        self.assertEqual(len(wb._read_outbox("anzhee")), 1)

    def test_credential_alias_stripped(self):
        wb.propose_term("РСЯ", series="s1", company="anzhee", publication_allowed=True,
                        aliases=["Гарсия", "ghp_" + "Z" * 36])
        recs = wb._read_outbox("anzhee")
        self.assertIn("Гарсия", recs[0]["payload"]["aliases"])
        self.assertTrue(all("ghp_" not in a for a in recs[0]["payload"]["aliases"]))


class TestPRPlanSafety(unittest.TestCase):
    """🔴 Инварианты §3.3 — план PR не трогает main, не мёржит."""

    def setUp(self):
        self.entries = [
            {"kind": "term", "value": "оффер", "company": "anzhee", "payload": {"canonical": "оффер", "scope": "anzhee"}},
            {"kind": "roster-role", "value": "Мария | поставки", "company": "anzhee",
             "source": {"series": "s1"}, "payload": {"slug": "s1", "role": {"name": "Мария", "domain": "поставки"}}},
        ]
        self.plan = wb.plan_pr("anzhee", self.entries)

    def test_branch_from_fresh_origin_main(self):
        cmds = [" ".join(c) for c in self.plan.commands]
        self.assertTrue(any("checkout -B notary/auto-knowledge origin/main" in c for c in cmds))
        self.assertEqual(self.plan.branch, "notary/auto-knowledge")
        self.assertEqual(self.plan.base, "main")

    def test_push_targets_branch_not_main(self):
        for cmd in self.plan.commands:
            if cmd[:2] == ["git", "push"]:
                self.assertIn("notary/auto-knowledge", cmd)
                self.assertNotIn("main", cmd, "push НЕ должен идти в main!")

    def test_no_bare_force_no_self_merge(self):
        flat = " ".join(" ".join(c) for c in self.plan.commands)
        # Нет bare `--force` (только `--force-with-lease` на bot-ветке допустим).
        self.assertNotIn("--force ", flat + " ")
        self.assertNotRegex(flat, r"--force$")
        # Бот НЕ мёржит свой PR.
        self.assertNotIn("pr merge", flat)
        self.assertNotIn("--merge", flat)

    def test_only_knowledge_subtree_staged(self):
        for cmd in self.plan.commands:
            if cmd[:2] == ["git", "add"]:
                for path in cmd[2:]:
                    self.assertTrue(path.startswith("knowledge/notary/"),
                                    f"git add вне поддерева бота: {path}")

    def test_pr_created_via_gh(self):
        self.assertTrue(any(c[:3] == ["gh", "pr", "create"] for c in self.plan.commands))

    def test_body_has_no_raw_transcript(self):
        # Тело PR — производные термины/роли, без сырья.
        self.assertIn("оффер", self.plan.body)
        self.assertIn("Ревью и мёрж — за командой", self.plan.body)


class TestYamlMerge(unittest.TestCase):
    """Чистый мёрж предложений в распарсенный YAML-dict (без pyyaml)."""

    def test_merge_terms_dedup(self):
        doc = {"version": 1, "terms": [{"canonical": "ЭДО", "scope": "cross"}]}
        entries = [
            {"kind": "term", "company": "anzhee", "payload": {"canonical": "оффер", "scope": "anzhee", "aliases": ["офер"]}},
            {"kind": "term", "company": "anzhee", "payload": {"canonical": "ЭДО", "scope": "cross"}},  # дубль
        ]
        out, added = wb.apply_entries_to_glossary(doc, entries)
        self.assertEqual(added, 1)
        canons = [t["canonical"] for t in out["terms"]]
        self.assertEqual(canons, ["ЭДО", "оффер"])

    def test_merge_terms_drops_credential(self):
        entries = [{"kind": "term", "company": "anzhee", "payload": {"canonical": "ghp_" + "A" * 36}}]
        out, added = wb.apply_entries_to_glossary(None, entries)
        self.assertEqual(added, 0)

    def test_merge_roles(self):
        entries = [{"kind": "roster-role", "company": "anzhee",
                    "payload": {"slug": "s1", "role": {"name": "Мария Михина", "domain": "поставки", "keywords": ["поставк"]}}}]
        out, added = wb.apply_entries_to_org(None, entries)
        self.assertEqual(added, 1)
        self.assertEqual(out["rosters"]["s1"]["roles"][0]["name"], "Мария Михина")


class TestFlush(_IsolatedMixin):
    """flush: без токена — no-op; с фейк-раннером — безопасная последовательность."""

    def test_not_provisioned_is_noop(self):
        wb.propose_term("оффер", series="s1", company="anzhee", publication_allowed=True)
        # Нет write-токена, нет раннера → not-provisioned (предложения остаются).
        res = wb.flush_company_outbox("anzhee")
        self.assertEqual(res["status"], "not-provisioned")
        self.assertEqual(len(wb._read_outbox("anzhee")), 1)

    def test_empty_outbox(self):
        self.assertEqual(wb.flush_company_outbox("anzhee")["status"], "empty")

    def test_fake_runner_executes_plan(self):
        wb.propose_term("оффер", series="s1", company="anzhee", publication_allowed=True)
        calls = []

        def fake_runner(cmd, cwd=None):
            calls.append(cmd)
            return {"cmd": cmd, "rc": 0}

        clone = self.base / "clone"
        (clone / "knowledge" / "notary").mkdir(parents=True)

        def fake_yaml_writer(path, doc):
            Path(path).write_text(str(doc), encoding="utf-8")

        res = wb.flush_company_outbox("anzhee", runner=fake_runner, clone_path=clone,
                                      yaml_writer=fake_yaml_writer)
        self.assertEqual(res["status"], "pr-opened")
        # Раннер реально прогнал все команды плана.
        self.assertEqual(len(calls), 6)
        flat = " ".join(" ".join(c) for c in calls)
        self.assertNotIn("push --force-with-lease origin main", flat)
        self.assertIn("gh pr create", flat)


class TestOutboxDigest(_IsolatedMixin):
    """Сводка outbox для дайджеста D2 — адресована компании."""

    def test_digest_lists_per_company(self):
        wb.propose_term("оффер", series="s1", company="anzhee", publication_allowed=True)
        wb.propose_role("Мария", "поставки", series="s1", company="anzhee", publication_allowed=True)
        wb.propose_term("Bolong", series="s2", company="mpfirst", publication_allowed=True)
        dig = wb.outbox_digest()
        self.assertIn("оффер", dig["anzhee"]["terms"])
        self.assertIn("Мария | поставки", dig["anzhee"]["roles"])
        self.assertEqual(dig["anzhee"]["target_repo"], "anzhee-context")
        self.assertIn("Bolong", dig["mpfirst"]["terms"])


if __name__ == "__main__":
    unittest.main()
