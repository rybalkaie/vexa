"""Ф9 (B1–B6) — питание второго мозга из встреч (дистилляция durable-знания).

Покрывает критерий «сделано» плана `umnyi-protokol-assemblyai`, Фаза 9:
  • B1 — дистилляция durable-кандидатов из ГОТОВОГО протокола; хвост Ф8 не тащим;
         пустая встреча → честное «ничего»; сбой → пусто (не роняет finalize);
  • B2 — маршрутизация ТОЛЬКО через router: личное→me/, компания→outbox→PR, креды→DROP;
         + ОБЯЗАТЕЛЬНЫЙ scope-тест не-протекания компаний (A↛B);
  • B3 — перед COMPANY: G11 + гейт; чувствительное/спорное → воскресная очередь, НЕ молча;
  • B4 — обучение на объяснении владельца (ратчет keep-private/promote + feedback-правило);
  • B5 — воскресный мини-отчёт + источник дашборда;
  • B6 — лог только счётчиками (без текста факта/реплик).

Изоляция: outbox/private/ratchet/brain-queue/weeklog/feedback — в темп через env.
LLM (дистиллятор + G11) ИНЪЕКТИРУЮТСЯ — claude не нужен.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase9_second_brain -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.lib import knowledge_distill as kd  # noqa: E402
from notary.lib import knowledge_writeback as wb  # noqa: E402
from notary.lib import knowledge_router as router  # noqa: E402
from notary.lib import knowledge_ratchet as rt  # noqa: E402
from notary.lib import publication_gate  # noqa: E402


class _IsolatedMixin(unittest.TestCase):
    """Темп-окружение: ни один реальный файл `me/`/`*-context` не трогается."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.base = base
        self._env = {
            "NOTARY_KNOWLEDGE_OUTBOX_DIR": str(base / "outbox"),
            "NOTARY_PRIVATE_KNOWLEDGE_QUEUE": str(base / "private.md"),
            "NOTARY_KNOWLEDGE_RATCHET_PATH": str(base / "ratchet.json"),
            "MEETING_NOTARY_CONTEXT_DIR": str(base / "no-context"),
            "MEETING_NOTARY_ME_DIR": str(base / "me"),
            "NOTARY_BRAIN_QUEUE_PATH": str(base / "me" / "_inbox" / "company-brain-queue.md"),
            "NOTARY_MEMORY_WEEKLOG_PATH": str(base / "me" / "_inbox" / "weeklog.jsonl"),
            "NOTARY_FEEDBACK_DIR": str(base / "me" / "ai-clone" / "feedback"),
            "ENABLE_KNOWLEDGE_DISTILL": "1",
        }
        self._old = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # helpers
    def _outbox_facts(self, company):
        return [r.get("value") for r in wb._read_outbox(company) if r.get("kind") == wb.KIND_INSIGHT]

    def _private_text(self):
        p = Path(self._env["NOTARY_PRIVATE_KNOWLEDGE_QUEUE"])
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def _brain_queue_text(self):
        p = Path(self._env["NOTARY_BRAIN_QUEUE_PATH"])
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def _allow_gate(self, company="anzhee"):
        """Контекст: публикационный гейт РАЗРЕШАЕТ (симулируем размеченную групповую)."""
        dec = publication_gate.PublicationDecision(True, "company", company, "ok-group")
        return mock.patch.object(publication_gate, "decide_for_meeting", return_value=dec)


# ───────────────────────── B1: дистилляция ────────────────────────────────────


class TestDistillB1(_IsolatedMixin):
    def test_strip_carryover_removes_phase8(self):
        proto = "## Решения\nРешили X\n\n## 🔻 С прошлых встреч\n- задача висит\n\n## Задачи\n- сделать Y"
        out = kd.strip_carryover(proto)
        self.assertNotIn("С прошлых встреч", out)
        self.assertNotIn("задача висит", out)
        self.assertIn("Решили X", out)
        self.assertIn("сделать Y", out)

    def test_empty_protocol_yields_nothing(self):
        self.assertEqual(kd.distill_candidates("", _caller=lambda *a, **k: "[]"), [])
        self.assertEqual(kd.distill_candidates("кратко", _caller=lambda *a, **k: "[]"), [])

    def test_parses_candidates_high_low(self):
        raw = ('[{"fact":"Стратегия: фокус на белом канале продаж","kind":"стратегия","confidence":"high"},'
               '{"fact":"Маржа держится около тридцати процентов","kind":"экономика","confidence":"low"}]')
        proto = "x" * 100
        cands = kd.distill_candidates(proto, _caller=lambda *a, **k: raw)
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[0]["confidence"], "high")
        self.assertEqual(cands[0]["insight_kind"], "стратегия")
        self.assertEqual(cands[1]["confidence"], "low")

    def test_failure_is_empty(self):
        def boom(*a, **k):
            raise RuntimeError("claude down")
        self.assertEqual(kd.distill_candidates("x" * 100, _caller=boom), [])

    def test_malformed_json_empty(self):
        self.assertEqual(kd.distill_candidates("x" * 100, _caller=lambda *a, **k: "не json"), [])
        self.assertEqual(kd.distill_candidates("x" * 100, _caller=lambda *a, **k: '{"obj":1}'), [])

    def test_carryover_not_sent_to_distiller(self):
        seen = {}
        def cap(prompt, **k):
            seen["p"] = prompt
            return "[]"
        proto = ("## Решения\nDecisionZ — приняли стратегию выхода на новый сегмент рынка "
                 "и зафиксировали юнит-экономику на ближайший квартал подробно.\n\n"
                 "## 🔻 С прошлых встреч\n- HANGING_TASK_SECRET\n")
        kd.distill_candidates(proto, _caller=cap)
        self.assertIn("DecisionZ", seen["p"])
        self.assertNotIn("HANGING_TASK_SECRET", seen["p"])  # хвост Ф8 не уходит в промпт

    def test_cap_limits(self):
        items = ",".join('{"fact":"durable вывод номер %d тут","kind":"вывод","confidence":"low"}' % i
                         for i in range(20))
        os.environ["KNOWLEDGE_DISTILL_MAX"] = "3"
        try:
            cands = kd.distill_candidates("x" * 100, _caller=lambda *a, **k: "[" + items + "]")
            self.assertEqual(len(cands), 3)
        finally:
            os.environ.pop("KNOWLEDGE_DISTILL_MAX", None)

    def test_invalid_kind_defaults_vyvod(self):
        raw = '[{"fact":"какой-то durable вывод","kind":"мусор","confidence":"bad"}]'
        c = kd.distill_candidates("x" * 100, _caller=lambda *a, **k: raw)[0]
        self.assertEqual(c["insight_kind"], "вывод")
        self.assertEqual(c["confidence"], "low")  # некорректная уверенность → консервативно low


# ───────────────────────── B2: маршрутизация ──────────────────────────────────


class TestRoutingB2(_IsolatedMixin):
    def _distill(self, **kw):
        defaults = dict(
            series="koord-anzhee", present_participants=["Илья", "Мария", "Сона"],
            watched={}, distiller=lambda: [{"fact": "Durable: фокус на прямых продажах",
                                            "insight_kind": "стратегия", "confidence": "high"}],
            classifier=lambda cands: [False] * len(cands),
        )
        defaults.update(kw)
        return kd.distill_and_route("x" * 100, **defaults)

    def test_private_when_no_company(self):
        res = self._distill(company=None, series="unknown-series-xyz")
        self.assertEqual(res["private"], 1)
        self.assertIn("Durable", self._private_text())
        self.assertEqual(self._outbox_facts("anzhee"), [])

    def test_company_clean_to_outbox(self):
        with self._allow_gate("anzhee"):
            res = self._distill(company="anzhee")
        self.assertEqual(res["company"], 1)
        self.assertIn("Durable: фокус на прямых продажах", self._outbox_facts("anzhee"))
        # ничего в воскресной очереди (несекретное durable идёт сразу через outbox→PR)
        self.assertNotIn("Durable", self._brain_queue_text())

    def test_credential_dropped(self):
        res = self._distill(
            company=None,
            distiller=lambda: [{"fact": "ghp_" + "A" * 36, "insight_kind": "вывод", "confidence": "high"}],
        )
        self.assertEqual(res["drop"], 1)
        self.assertEqual(self._private_text(), "")
        self.assertEqual(self._outbox_facts("anzhee"), [])

    def test_disabled_killswitch(self):
        os.environ["ENABLE_KNOWLEDGE_DISTILL"] = "0"
        try:
            res = self._distill(company="anzhee")
            self.assertEqual(res["status"], "disabled")
            self.assertEqual(self._outbox_facts("anzhee"), [])
        finally:
            os.environ["ENABLE_KNOWLEDGE_DISTILL"] = "1"

    def test_empty_candidates_honest_nothing(self):
        res = self._distill(distiller=lambda: [])
        self.assertEqual(res["status"], "empty")
        self.assertEqual(res["candidates"], 0)


class TestScopeNoLeakB6(_IsolatedMixin):
    """🔴 ОБЯЗАТЕЛЬНЫЙ scope-тест: durable-факт компании A НЕ попадает в `*-context` B."""

    def test_writeback_addressing_no_cross_leak(self):
        # propose_insight адресует ИМЕННО свою компанию (детерминированно).
        wb.propose_insight("Факт-A: стратегия Anzhee", series="sA", company="anzhee",
                           publication_allowed=True)
        wb.propose_insight("Факт-B: экономика МПервого", series="sB", company="mpfirst",
                           publication_allowed=True)
        a_facts = self._outbox_facts("anzhee")
        b_facts = self._outbox_facts("mpfirst")
        self.assertIn("Факт-A: стратегия Anzhee", a_facts)
        self.assertIn("Факт-B: экономика МПервого", b_facts)
        # ↛ не протекло: A нет в B, B нет в A.
        self.assertNotIn("Факт-A: стратегия Anzhee", b_facts)
        self.assertNotIn("Факт-B: экономика МПервого", a_facts)

    def test_distill_routes_each_to_own_company(self):
        clean = lambda cands: [False] * len(cands)  # noqa: E731
        with self._allow_gate("anzhee"):
            kd.distill_and_route("x" * 100, series="sA", company="anzhee",
                                 present_participants=["Илья", "Мария", "Сона"], watched={},
                                 distiller=lambda: [{"fact": "AAA durable стратегия",
                                                     "insight_kind": "стратегия", "confidence": "high"}],
                                 classifier=clean)
        with self._allow_gate("mpfirst"):
            kd.distill_and_route("x" * 100, series="sB", company="mpfirst",
                                 present_participants=["Илья", "Иван", "Пётр"], watched={},
                                 distiller=lambda: [{"fact": "BBB durable экономика",
                                                     "insight_kind": "экономика", "confidence": "high"}],
                                 classifier=clean)
        self.assertIn("AAA durable стратегия", self._outbox_facts("anzhee"))
        self.assertIn("BBB durable экономика", self._outbox_facts("mpfirst"))
        self.assertNotIn("AAA durable стратегия", self._outbox_facts("mpfirst"))
        self.assertNotIn("BBB durable экономика", self._outbox_facts("anzhee"))


# ───────────────────── B2/B3: чувствительное НЕ в COMPANY ──────────────────────


class TestSensitiveToSundayB3(_IsolatedMixin):
    """🔴 Чувствительное (G11) / спорное → воскресная очередь, НЕ молча в `*-context`."""

    def _run(self, *, classifier, confidence="high"):
        with self._allow_gate("anzhee"):
            return kd.distill_and_route(
                "x" * 100, series="koord-anzhee", company="anzhee",
                present_participants=["Илья", "Мария", "Сона"], watched={},
                distiller=lambda: [{"fact": "Чувствительное: увольнение Иванова",
                                    "insight_kind": "вывод", "confidence": confidence}],
                classifier=classifier,
            )

    def test_g11_sensitive_not_to_outbox(self):
        res = self._run(classifier=lambda cands: [True] * len(cands))  # G11: чувствительно
        self.assertEqual(res["sunday"], 1)
        self.assertEqual(res["company"], 0)
        self.assertEqual(self._outbox_facts("anzhee"), [])  # НЕ молча в репо
        self.assertIn("увольнение Иванова", self._brain_queue_text())  # → воскресный разбор

    def test_low_confidence_to_sunday(self):
        res = self._run(classifier=lambda cands: [False] * len(cands), confidence="low")
        self.assertEqual(res["sunday"], 1)
        self.assertEqual(res["company"], 0)
        self.assertEqual(self._outbox_facts("anzhee"), [])
        self.assertIn("увольнение Иванова", self._brain_queue_text())

    def test_g11_failure_is_conservative_to_sunday(self):
        def boom(cands):
            raise RuntimeError("haiku down")
        res = self._run(classifier=boom)  # сбой G11 → всё company трактуем чувствительным
        self.assertEqual(res["company"], 0)
        self.assertEqual(self._outbox_facts("anzhee"), [])
        self.assertEqual(res["sunday"], 1)

    def test_g11_length_mismatch_is_conservative(self):
        res = self._run(classifier=lambda cands: [])  # битый ответ длины → exclude-all
        self.assertEqual(res["company"], 0)
        self.assertEqual(self._outbox_facts("anzhee"), [])

    def test_brain_queue_format_and_dedup(self):
        ok1 = kd.append_brain_queue("Секрет про экономику", company="anzhee",
                                    reason="G11: чувствительное", date="2026-06-13")
        ok2 = kd.append_brain_queue("Секрет про экономику", company="anzhee",
                                    reason="G11: чувствительное", date="2026-06-13")
        self.assertTrue(ok1 and ok2)
        text = self._brain_queue_text()
        self.assertEqual(text.count("Секрет про экономику"), 1)  # дедуп
        self.assertIn("## Очередь", text)
        self.assertIn("- [ ] 2026-06-13 | Anzhee |", text)  # формат строки очереди


# ───────────────────────── B4: обучение на объяснениях ────────────────────────


class TestLearnB4(_IsolatedMixin):
    def test_keep_private_command_parsed(self):
        self.assertIsNotNone(rt.parse_keep_private_command("это приватное, не в контекст"))
        self.assertIsNotNone(rt.parse_keep_private_command("держи в личном"))
        self.assertIsNone(rt.parse_keep_private_command("ок кроме 2"))

    def test_keep_private_overrides_publication(self):
        # До обучения: company + гейт разрешил → COMPANY.
        d0 = router.classify_destination("FactQ", kind=kd.KIND_INSIGHT, company="anzhee",
                                         publication_allowed=True)
        self.assertTrue(d0.is_company)
        # Владелец объяснил «это приватное» (по роду insight, компания anzhee).
        rt.remember_keep_private(kd.KIND_INSIGHT, company="anzhee")
        d1 = router.classify_destination("FactQ", kind=kd.KIND_INSIGHT, company="anzhee",
                                         publication_allowed=True)
        self.assertTrue(d1.is_private)  # keep-private перебивает гейт
        self.assertEqual(d1.reason, "owner-keep-private")

    def test_learn_from_owner_persists_rule_and_ratchet(self):
        out = kd.learn_from_owner("это приватное, потому что кадровое решение", company="anzhee")
        self.assertIsNotNone(out["keep_private"])
        self.assertTrue(rt.should_keep_private(kd.KIND_INSIGHT, company="anzhee"))
        # feedback-правило записано по канону.
        self.assertTrue(out["rule_file"])
        rule_text = Path(out["rule_file"]).read_text(encoding="utf-8")
        self.assertIn("type: feedback", rule_text)
        self.assertIn("**Why:**", rule_text)
        self.assertIn("**How to apply:**", rule_text)
        # INDEX.md дополнен.
        idx = Path(self._env["NOTARY_FEEDBACK_DIR"]) / "INDEX.md"
        self.assertIn(".md)", idx.read_text(encoding="utf-8"))

    def test_learn_promote_direction(self):
        out = kd.learn_from_owner("переноси такие в контекст компании", company="anzhee")
        self.assertIsNotNone(out["promoted"])
        self.assertTrue(rt.should_promote(kd.KIND_INSIGHT, company="anzhee"))

    def test_negated_promote_not_promoted(self):
        # «не переноси в контекст» → fail-closed: НЕ повышаем (ошибочное повышение
        # необратимо для команды). Ничего не запомнено в сторону COMPANY.
        self.assertIsNone(rt.parse_promote_command("не переноси в контекст"))
        out = kd.learn_from_owner("не переноси такие в контекст компании", company="anzhee")
        self.assertIsNone(out["promoted"])
        self.assertFalse(rt.should_promote(kd.KIND_INSIGHT, company="anzhee"))

    def test_distiller_respects_learned_keep_private(self):
        rt.remember_keep_private(kd.KIND_INSIGHT, company="anzhee")
        with self._allow_gate("anzhee"):
            res = kd.distill_and_route(
                "x" * 100, series="koord-anzhee", company="anzhee",
                present_participants=["Илья", "Мария", "Сона"], watched={},
                distiller=lambda: [{"fact": "Durable который владелец велел держать приватным",
                                    "insight_kind": "стратегия", "confidence": "high"}],
                classifier=lambda cands: [False] * len(cands),
            )
        # keep-private → PRIVATE, несмотря на разрешающий гейт и high-confidence.
        self.assertEqual(res["private"], 1)
        self.assertEqual(res["company"], 0)
        self.assertEqual(self._outbox_facts("anzhee"), [])


# ───────────────────────── B5: воскресный отчёт ────────────────────────────────


class TestWeeklyReportB5(_IsolatedMixin):
    def _seed(self, route, company, fact, date="2026-06-13"):
        kd.record_week_log(route=route, company=company, fact=fact, date=date)

    def test_report_built_grouped(self):
        from datetime import datetime
        self._seed("company", "anzhee", "Компанийский durable вывод")
        self._seed("private", None, "Личный durable вывод")
        self._seed("sunday", "mpfirst", "Спорный кандидат")
        rep = kd.build_weekly_report(now=datetime(2026, 6, 14))
        self.assertIn("Пополнение памяти за неделю", rep)
        self.assertIn("Компанийский durable вывод", rep)
        self.assertIn("Личный durable вывод", rep)
        self.assertIn("подтверждение", rep)  # секция воскресного разбора

    def test_report_empty_none(self):
        self.assertIsNone(kd.build_weekly_report())

    def test_old_entries_excluded(self):
        from datetime import datetime
        self._seed("private", None, "Старый факт", date="2026-05-01")
        rep = kd.build_weekly_report(now=datetime(2026, 6, 14), since_days=7)
        self.assertIsNone(rep)  # вне окна 7 дней

    def test_send_uses_injected_sender(self):
        from datetime import datetime
        self._seed("private", None, "Факт для отправки")
        captured = {}
        res = kd.send_weekly_report(now=datetime(2026, 6, 14),
                                    sender=lambda t: captured.setdefault("t", t) or True)
        self.assertEqual(res["status"], "sent")
        self.assertIn("Факт для отправки", captured["t"])

    def test_send_empty_no_call(self):
        called = {"n": 0}
        res = kd.send_weekly_report(sender=lambda t: called.__setitem__("n", called["n"] + 1) or True)
        self.assertEqual(res["status"], "empty")
        self.assertEqual(called["n"], 0)


# ───────────────────────── B6: приватность логов ──────────────────────────────


class TestPrivacyB6(_IsolatedMixin):
    def test_distill_logs_only_counters(self):
        secret_fact = "СУПЕР-СЕКРЕТНЫЙ-ФАКТ-НЕ-В-ЛОГ"
        with self.assertLogs("notary.lib.knowledge_distill", level="INFO") as cm:
            kd.distill_and_route(
                "x" * 100, series="koord-anzhee", company=None,
                present_participants=["Илья", "Мария", "Сона"], watched={},
                distiller=lambda: [{"fact": secret_fact, "insight_kind": "вывод", "confidence": "high"}],
                classifier=lambda cands: [False] * len(cands),
            )
        joined = "\n".join(cm.output)
        self.assertNotIn(secret_fact, joined)        # текст факта НЕ в логе
        self.assertIn("candidates=", joined)          # счётчики есть
        self.assertIn("private=", joined)

    def test_week_log_stores_result_not_raw(self):
        kd.record_week_log(route="private", company=None, fact="Только факт-результат")
        p = Path(self._env["NOTARY_MEMORY_WEEKLOG_PATH"])
        rec = json.loads(p.read_text(encoding="utf-8").strip())
        self.assertEqual(rec["fact"], "Только факт-результат")
        self.assertNotIn("transcript", rec)  # сырья нет
        self.assertNotIn("replies", rec)


# ───────────────────────── B2: writeback insight (юнит) ───────────────────────


class TestWritebackInsightB2(_IsolatedMixin):
    def test_insight_company_to_outbox(self):
        res = wb.propose_insight("Стратегический вывод", series="s1", company="anzhee",
                                 publication_allowed=True, insight_kind="стратегия")
        self.assertEqual(res.layer, "company")
        recs = wb._read_outbox("anzhee")
        self.assertEqual(recs[0]["kind"], "insight")
        self.assertIn("insights.md", recs[0]["target"])
        self.assertEqual(recs[0]["payload"]["fact"], "Стратегический вывод")

    def test_insight_private_default(self):
        res = wb.propose_insight("Вывод без публикации", series="s1", company="anzhee",
                                 publication_allowed=False)
        self.assertEqual(res.layer, "private")
        self.assertEqual(wb._read_outbox("anzhee"), [])

    def test_insight_dedup(self):
        wb.propose_insight("Тот же вывод", series="s1", company="anzhee", publication_allowed=True)
        r2 = wb.propose_insight("Тот же вывод", series="s1", company="anzhee", publication_allowed=True)
        self.assertEqual(r2.layer, "exists")

    def test_apply_entries_to_insights_dedup_and_creds(self):
        entries = [
            {"kind": "insight", "value": "Вывод раз", "payload": {"fact": "Вывод раз"}, "source": {"series": "s1", "date": "2026-06-13"}},
            {"kind": "insight", "value": "Вывод раз", "payload": {"fact": "Вывод раз."}},  # дубль (норм.)
            {"kind": "insight", "value": "ghp_" + "A" * 36, "payload": {"fact": "ghp_" + "A" * 36}},  # кред
        ]
        text, added = wb.apply_entries_to_insights(None, entries)
        self.assertEqual(added, 1)
        self.assertIn("Вывод раз", text)
        self.assertNotIn("ghp_", text)

    def test_plan_pr_includes_insights_subtree(self):
        entries = [{"kind": "insight", "value": "F", "payload": {"fact": "F"}, "source": {"series": "s1"}}]
        plan = wb.plan_pr("anzhee", entries)
        # git add только в поддереве бота; insights.md присутствует.
        for cmd in plan.commands:
            if cmd[:2] == ["git", "add"]:
                self.assertTrue(all(p.startswith("knowledge/notary/") for p in cmd[2:]))
                self.assertIn("knowledge/notary/insights.md", cmd[2:])
        self.assertIn("факт: F", plan.body)


class TestRunFlushInsightLive(_IsolatedMixin):
    """Боевой git: insight едет в `insights.md` на bot-ВЕТКЕ, НЕ в main (B2 + РИСК Ф9)."""

    def setUp(self):
        super().setUp()
        import subprocess
        self.sub = subprocess
        self.ctx = self.base / "ctx"
        self.ctx.mkdir(parents=True)
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(self.ctx)
        self.bare = self.base / "anzhee-context.git"
        self.clone = self.ctx / "anzhee-context"
        seed = self.base / "seed"
        kn = seed / "knowledge" / "notary"
        kn.mkdir(parents=True)
        (kn / "glossary.yaml").write_text("version: 1\nterms: []\nguidance: []\n", encoding="utf-8")
        (kn / "org-structure.yaml").write_text("version: 1\nrosters: {}\n", encoding="utf-8")
        self._git(seed, "init", "-q", "-b", "main")
        self._git(seed, "config", "user.email", "t@t"); self._git(seed, "config", "user.name", "T")
        self._git(seed, "add", "-A"); self._git(seed, "commit", "-q", "-m", "seed")
        self._git(self.base, "clone", "-q", "--bare", str(seed), str(self.bare))
        self._git(self.base, "clone", "-q", str(self.bare), str(self.clone))
        self._git(self.clone, "config", "user.email", "t@t"); self._git(self.clone, "config", "user.name", "T")

    def _git(self, cwd, *args):
        r = self.sub.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def _bare_branches(self):
        return self._git(self.bare, "branch", "--format=%(refname:short)").stdout.split()

    def _show(self, ref, path):
        return self._git(self.bare, "show", f"{ref}:{path}").stdout

    def test_insight_pushed_to_branch_not_main(self):
        wb.propose_insight("Durable факт для PR", series="s1", company="anzhee", publication_allowed=True)
        res = wb.run_flush("anzhee", clone_path=self.clone)
        self.assertEqual(res["status"], "pr-pushed")
        self.assertEqual(res["n_insight"], 1)
        self.assertIn("notary/auto-knowledge", self._bare_branches())
        # 🔴 факт в bot-ветке, main НЕ тронут (insights.md в main нет вообще).
        self.assertIn("Durable факт для PR", self._show("notary/auto-knowledge", wb.INSIGHTS_REL))
        with self.assertRaises(AssertionError):
            self._show("main", wb.INSIGHTS_REL)  # файла в main нет → git show падает

    def test_already_merged_insight_marked_not_repushed(self):
        # У1/Н1 (цикл5): после мёржа PR командой факт уже в main → следующий flush
        # ДОЛЖЕН распознать его как merged (дренировать из outbox), а НЕ держать вечно
        # pending. Регрессия на рассинхрон детекта merge (полные строки vs голый факт).
        wb.propose_insight("Durable факт уже в main", series="s1", company="anzhee",
                           publication_allowed=True)
        r1 = wb.run_flush("anzhee", clone_path=self.clone)
        self.assertEqual(r1["status"], "pr-pushed")
        # команда приняла PR: мёржим bot-ветку в main и пушим.
        self._git(self.clone, "fetch", "-q", "origin")
        self._git(self.clone, "checkout", "-q", "main")
        self._git(self.clone, "merge", "-q", "--no-edit", "origin/notary/auto-knowledge")
        self._git(self.clone, "push", "-q", "origin", "main")
        # второй flush: факт уже в origin/main → merged, ничего нового не вливаем.
        r2 = wb.run_flush("anzhee", clone_path=self.clone)
        self.assertIn(r2["status"], ("already-merged", "nothing-to-commit"))
        self.assertEqual(r2.get("n_insight", 0), 0)
        # запись больше НЕ висит queued/pending — помечена merged, ушла из активных.
        active = [r for r in wb._read_outbox("anzhee")
                  if r.get("status") in (wb.STATUS_QUEUED, wb.STATUS_PENDING)]
        self.assertEqual(active, [])


class TestCLI(_IsolatedMixin):
    """CLI обёртка (воскресный крон/скил): --learn и dry-run отчёта без claude/tg."""

    def test_learn_cli_persists(self):
        rc = kd.main(["--learn", "это приватное, потому что кадровое", "--company", "anzhee"])
        self.assertEqual(rc, 0)
        self.assertTrue(rt.should_keep_private(kd.KIND_INSIGHT, company="anzhee"))

    def test_dry_run_report_no_send(self):
        # main([]) — сухой показ (build_weekly_report), tg-send НЕ зовётся.
        kd.record_week_log(route="private", company=None, fact="Факт недели")
        rc = kd.main([])
        self.assertEqual(rc, 0)

    def test_real_distiller_no_caller_is_safe(self):
        # Без инъекции _caller: если claude недоступен — ClaudeCliNotInstalled внутри
        # → []. Если доступен — реальный вызов (не делаем в тесте). Мокаем call.
        with mock.patch("notary.lib.claude_cli.call_claude_print",
                        side_effect=RuntimeError("no claude in test")):
            self.assertEqual(kd.distill_candidates("x" * 120, series="s1"), [])


if __name__ == "__main__":
    unittest.main()
