# -*- coding: utf-8 -*-
"""Ф6 (план `2026-07-20-watson-smart-replies-rethink.md`, R21 — ОФЛАЙН-часть):
golden-набор реальных кейсов ответов сотрудников + офлайн детерминированный стаб ИИ.

Зачем (R21): доказать НА КЕЙСАХ, что understand-слой стал умнее, а не «тесты зелёные».
Каждый кейс — «как ответил живой человек» → «что бот сделал». Прогон детерминированным
СТАБОМ ИИ (`understand_fn`) — БЕЗ сети, БЕЗ реальных ПДн, БЕЗ вызова `claude`. Это
АВТОНОМНАЯ часть R21.

Живой прогон на КОПИЯХ реальных серий с настоящим ИИ (вторая половина R21, критерий
РАЗМ3 «критичные 3/3») — OWNER-GATED: реальные транскрипты = личные данные участников +
вызов claude + отправка наружу («опасная тройка», CLAUDE.md). Прецедент
[[acceptance-realseries-run-blocked-by-privacy-gate]]. Тут он НЕ выполняется — верифицируем
офлайн-стабом на синтетических фикстурах (имена/задачи вымышлены, не ПДн).

Недетерминированность (РАЗМ3): офлайн-стаб ДЕТЕРМИНИРОВАН — один и тот же вход даёт один
и тот же исход, поэтому «критичные 3/3» на офлайне выполняется тождественно (см.
`test_critical_cases_stable_3x`, гоняет критичные кейсы трижды). «3/3 на живом ИИ» —
owner-gated хвост.

Отчёт по КАЖДОМУ кейсу (исходы, не «зелёно»):
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    ../../../.venv-cli/bin/python tests/test_phase6_golden.py --report

Обычный прогон:
    ../../../.venv-cli/bin/python -m unittest tests.test_phase6_golden -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
sys.path.insert(0, str(_SCRIPTS))

from notary.lib import series_memory as sm  # noqa: E402
from notary.lib import pending_status_reply as psr  # noqa: E402
from notary.lib import decision_memory as dm  # noqa: E402
from notary.lib import reply_understand as ru  # noqa: E402
from notary.lib import reply_ack as ack  # noqa: E402


# ── Синтетическая фикстура (ВЫМЫШЛЕННЫЕ имена/задачи — НЕ реальные ПДн) ──────────
# Ольге отведено ровно 5 нумерованных задач — для кейса И1 «да, кроме третьей».
_PROTO = """#протоколвстречи 20.07.2026

**Встреча:** Директорат — синхрон по направлениям.

**Участники:** Ольга Смирнова, Мария Козлова, Михаил Орлов

**Транскрипт:** [2026-07-20.md](2026-07-20.md)

---

## Задачи

**Ольга Смирнова**
- Согласовать бюджет на июль
- Обновить прайс поставщика
- Проверить остатки на складе
- Запустить рассылку по базе
- Подготовить отчёт по продажам

**Мария Козлова**
- Собрать заявки от отдела

**Михаил Орлов**
- Провести аудит договоров
"""
_SERIES_DATE = "2026-07-20"
_APPLY_DATE = "2026-07-22"


def _canned(statuses=None, edits=None, config=None, block=False, unclear=""):
    """Нормализованный результат ИИ-разбора (как из `_parse_understand`) для стаба."""
    return {
        "statuses": [{"text": t, "cls": c} for (t, c) in (statuses or [])],
        "edits": list(edits or []),
        "series_config": list(config or []),
        "block_close": block,
        "unclear": unclear,
    }


# ── Реестр golden-кейсов (route = understand, если не сказано иное) ──────────────
# Каждый understand-кейс: id, title, replier, stub, expect.
#   stub — что «увидел» ИИ (детерминированно); expect — что бот ОБЯЗАН сделать.
#   expect: applied_labels (list[str]) — классы применённых статусов (в порядке);
#           question_kinds (list[str]) — виды переспросов (ступень 3);
#           critical (bool) — критичный кейс (ложное/пропущенное закрытие).
_GOLDEN = [
    {
        "id": "G1", "title": "Ольга 12:10 — свой статус нестандартным словом → закрыто",
        "replier": "Ольга Смирнова",
        "reply": "по бюджету на июль всё согласовали",
        "stub": _canned([("по бюджету на июль всё согласовали", "done")]),
        "expect": {"applied_labels": ["done"], "question_kinds": []},
    },
    {
        "id": "G2", "title": "Ольга 12:15 — смешанный: одно закрыто, другое в работе",
        "replier": "Ольга Смирнова",
        "reply": "рассылку по базе запустила, отчёт по продажам ещё готовлю",
        "stub": _canned([("рассылку по базе запустила", "done"),
                         ("отчёт по продажам ещё готовлю", "wait")]),
        "expect": {"applied_labels": ["done", "wait"], "question_kinds": []},
    },
    {
        "id": "G-R7", "title": "R7 — незнакомое слово доходит до применения БЕЗ словаря + журнал",
        "replier": "Ольга Смирнова",
        "reply": "остатки на складе разрулила",
        "stub": _canned([("остатки на складе разрулила", "done")]),
        "expect": {"applied_labels": ["done"], "question_kinds": [],
                   "assert_ai_only": True, "assert_journaled_word": "разрулила"},
        "critical": True,
    },
    {
        "id": "G3", "title": "Мария 12:07 — снятие своей задачи (cancelled)",
        "replier": "Мария Козлова",
        "reply": "заявки от отдела больше не собираем, снимаем",
        "stub": _canned([("заявки от отдела снимаем", "cancelled")]),
        "expect": {"applied_labels": ["cancelled"], "question_kinds": []},
    },
    {
        "id": "G4-A", "title": "Михаил случай А — закрывает СВОЮ задачу → применяется",
        "replier": "Михаил Орлов",
        "reply": "аудит договоров провёл",
        "stub": _canned([("аудит договоров провёл", "done")]),
        "expect": {"applied_labels": ["done"], "question_kinds": []},
    },
    {
        "id": "G4-B", "title": "Михаил случай Б — трогает ЧУЖУЮ задачу (Ольги) → ступень 3, не пишем (R11)",
        "replier": "Михаил Орлов",
        "reply": "бюджет на июль уже согласовали",
        "stub": _canned([("бюджет на июль согласовали", "done")]),
        "expect": {"applied_labels": [], "question_kinds": ["foreign"]},
        "critical": True,
    },
]


class _GoldenBase(unittest.TestCase):
    """Строит серию-фикстуру, гоняет understand_reply со стабом, изолирует память в tmp."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sd = self.root / "direktorat"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, _SERIES_DATE, _PROTO,
                               {"series": "direktorat", "date": _SERIES_DATE})
        self.memf = str(self.root / "decision-memory.jsonl")
        self._env = mock.patch.dict(os.environ, {
            "ENABLE_REPLY_UNDERSTAND": "1",
            "MEETING_NOTARY_DECISION_MEMORY_FILE": self.memf,
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _status(self):
        return {k: v["status"] for k, v in sm.load_task_status(self.sd).items()}

    def _run(self, case):
        """Прогнать один understand-кейс → нормализованный исход (для теста и отчёта)."""
        stub = (lambda cn: (lambda rt, it, snap=None: cn))(case["stub"])
        r = ru.understand_reply(
            case["reply"], series_dir=self.sd, replier=case["replier"],
            understand_fn=stub, date=_APPLY_DATE, store_path=Path(self.memf))
        return {
            "applied": [(d["label"], d.get("source"), d.get("matched")) for d in r["applied"]],
            "questions": [q["kind"] for q in r["questions"]],
            "status_after": self._status(),
            "raw": r,
        }


class TestGoldenUnderstand(_GoldenBase):
    """Каждый golden-кейс — отдельная проверка исхода."""

    def _check(self, case):
        out = self._run(case)
        exp = case["expect"]
        got_labels = [lbl for (lbl, _src, _m) in out["applied"]]
        self.assertEqual(
            got_labels, exp["applied_labels"],
            msg=f"{case['id']}: применённые классы {got_labels} != ожидаемых {exp['applied_labels']}")
        for kind in exp["question_kinds"]:
            self.assertIn(kind, out["questions"],
                          msg=f"{case['id']}: ждали переспрос '{kind}', получили {out['questions']}")
        if not exp["question_kinds"]:
            self.assertEqual(out["questions"], [],
                             msg=f"{case['id']}: лишние переспросы {out['questions']}")
        if exp.get("assert_ai_only"):
            srcs = [src for (_l, src, _m) in out["applied"]]
            self.assertIn("ai_only", srcs, msg=f"{case['id']}: ждали ai_only, получили {srcs}")
        if exp.get("assert_journaled_word"):
            w = exp["assert_journaled_word"]
            keys = [e.get("key") for e in dm.journal_entries(store_path=Path(self.memf))
                    if e.get("ev") == "journal"]
            self.assertTrue(any(w.startswith(k) or k.startswith(w[:4]) for k in keys),
                            msg=f"{case['id']}: слово '{w}' не попало в журнал R20 (keys={keys})")
        return out

    def test_g1(self):  self._check(_by_id("G1"))
    def test_g2(self):  self._check(_by_id("G2"))
    def test_g_r7(self):  self._check(_by_id("G-R7"))
    def test_g3(self):  self._check(_by_id("G3"))
    def test_g4a(self):  self._check(_by_id("G4-A"))
    def test_g4b(self):  self._check(_by_id("G4-B"))

    def test_critical_cases_stable_3x(self):
        """РАЗМ3 «критичные 3/3»: офлайн-стаб детерминирован → критичный кейс даёт один и
        тот же исход трижды. (Живое «3/3 на реальном ИИ» — owner-gated хвост.)"""
        for case in [c for c in _GOLDEN if c.get("critical")]:
            outs = []
            for _ in range(3):
                # свежая серия на каждый прогон — независимость исходов
                self.tearDown(); self.setUp()
                outs.append(tuple(lbl for (lbl, _s, _m) in self._run(case)["applied"]))
            self.assertEqual(len(set(outs)), 1,
                             msg=f"{case['id']}: недетерминированный исход {outs}")
            self.assertEqual(outs[0], tuple(case["expect"]["applied_labels"]),
                             msg=f"{case['id']}: 3/3 исход {outs[0]} != ожидаемого")


class TestGoldenFU1Matrix(unittest.TestCase):
    """Матрица FU-1: отрицание («не сделал»/«не закрыл»/«пока не готово») НИКОГДА не
    закрывает задачу как done (класс лжи, который план искореняет). Детерминированный
    словарный слой — быстрый путь и фолбэк без ИИ (`pending_status_reply.detect_status_intent`)."""

    _MATRIX = [
        ("сделал", "done"), ("не сделал", "reopen"), ("не сделала", "reopen"),
        ("закрыл", "done"), ("не закрыл", "reopen"),
        ("готово", "done"), ("пока не готово", "wait"), ("не успел", "wait"),
    ]

    def test_fu1_negation_never_done(self):
        for phrase, expected in self._MATRIX:
            got = psr.detect_status_intent(phrase)
            self.assertEqual(got, expected, msg=f"'{phrase}': {got} != {expected}")
            if expected != "done":
                self.assertNotEqual(got, "done", msg=f"'{phrase}' ложно закрылось как done!")


class TestGoldenI1ConfirmExceptThird(unittest.TestCase):
    """И1 «да, кроме третьей» — живой ответ на подтверждение R11 «закрыть все 5?».
    Роут reply_ack (детерминированный, БЕЗ ИИ): закрыть 1,2,4,5, третью ОСТАВИТЬ."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sd = Path(self._tmp.name) / "direktorat"
        self.sd.mkdir()
        sm.save_meeting_digest(self.sd, _SERIES_DATE, _PROTO,
                               {"series": "direktorat", "date": _SERIES_DATE})

    def tearDown(self):
        self._tmp.cleanup()

    def _olga_tasks(self):
        items = psr.load_series_pending(self.sd)
        return [it for it in items if it.lower().startswith("ольга")]

    def test_i1_da_krome_tretey(self):
        options = self._olga_tasks()
        self.assertEqual(len(options), 5, msg=f"фикстура: у Ольги должно быть 5 задач, есть {len(options)}")
        state = {"series_dir": str(self.sd), "date": _APPLY_DATE, "applied": [],
                 "question": {"mode": "confirm_close", "label": "done", "options": options}}
        res = ack.handle_ack_reply(state, "да, кроме третьей", store_path=None)
        self.assertTrue(res["consume"])
        status = {k: v["status"] for k, v in sm.load_task_status(self.sd).items()}
        third_key = sm._status_key(options[2])
        for i, opt in enumerate(options, 1):
            key = sm._status_key(opt)
            if i == 3:
                self.assertNotEqual(status.get(key), sm.STATUS_DONE,
                                    msg="третья задача НЕ должна была закрыться")
            else:
                self.assertEqual(status.get(key), sm.STATUS_DONE,
                                 msg=f"задача №{i} должна быть done")


def _by_id(cid):
    for c in _GOLDEN:
        if c["id"] == cid:
            return c
    raise KeyError(cid)


# ── Отчёт по каждому кейсу (исходы, не «зелёно») ────────────────────────────────
def _report():
    """Печатает исход по КАЖДОМУ golden-кейсу (для пакета сдачи R21)."""
    print("=" * 78)
    print("GOLDEN R21 — офлайн-прогон детерминированным стабом ИИ (без сети/ПДн/claude)")
    print("=" * 78)
    passed = 0
    total = 0

    # understand-кейсы
    for case in _GOLDEN:
        t = _GoldenBase("setUp")
        t.setUp()
        try:
            out = t._run(case)
            exp = case["expect"]
            got_labels = [lbl for (lbl, _s, _m) in out["applied"]]
            ok = got_labels == exp["applied_labels"]
            for k in exp["question_kinds"]:
                ok = ok and (k in out["questions"])
            if not exp["question_kinds"]:
                ok = ok and out["questions"] == []
            if exp.get("assert_ai_only"):
                ok = ok and "ai_only" in [s for (_l, s, _m) in out["applied"]]
            total += 1
            passed += 1 if ok else 0
            verdict = "PASS ✅" if ok else "FAIL ❌"
            print(f"\n[{case['id']}] {case['title']}")
            print(f"   ответил: {case['replier']}  ·  reply(смысл): {case['reply']!r}")
            print(f"   ждали:   applied={exp['applied_labels']} questions={exp['question_kinds']}")
            print(f"   бот:     applied={[ (l,s) for (l,s,_m) in out['applied'] ]} "
                  f"questions={out['questions']}")
            print(f"   вердикт: {verdict}")
        finally:
            t.tearDown()

    # FU-1 матрица
    print("\n[G-FU1] Матрица FU-1 — отрицание не закрывает как done")
    fu = TestGoldenFU1Matrix()
    all_ok = True
    for phrase, expected in TestGoldenFU1Matrix._MATRIX:
        got = psr.detect_status_intent(phrase)
        line_ok = (got == expected)
        all_ok = all_ok and line_ok
        print(f"   {phrase!r:28} → {got!r:12} (ждали {expected!r}) {'✅' if line_ok else '❌'}")
    total += 1; passed += 1 if all_ok else 0
    print(f"   вердикт: {'PASS ✅' if all_ok else 'FAIL ❌'}")

    # И1
    print("\n[G-I1] «да, кроме третьей» — закрыть 1,2,4,5, третью оставить (reply_ack)")
    ti = TestGoldenI1ConfirmExceptThird()
    ti.setUp()
    try:
        options = ti._olga_tasks()
        state = {"series_dir": str(ti.sd), "date": _APPLY_DATE, "applied": [],
                 "question": {"mode": "confirm_close", "label": "done", "options": options}}
        ack.handle_ack_reply(state, "да, кроме третьей", store_path=None)
        status = {k: v["status"] for k, v in sm.load_task_status(ti.sd).items()}
        closed = [i for i, o in enumerate(options, 1)
                  if status.get(sm._status_key(o)) == sm.STATUS_DONE]
        i1_ok = closed == [1, 2, 4, 5]
        total += 1; passed += 1 if i1_ok else 0
        print(f"   закрыто задач №: {closed} (ждали [1, 2, 4, 5])  {'✅' if i1_ok else '❌'}")
        print(f"   вердикт: {'PASS ✅' if i1_ok else 'FAIL ❌'}")
    finally:
        ti.tearDown()

    print("\n" + "=" * 78)
    print(f"ИТОГ GOLDEN: {passed}/{total} кейс-групп PASS")
    print("=" * 78)
    return passed == total


if __name__ == "__main__":
    if "--report" in sys.argv:
        ok = _report()
        sys.exit(0 if ok else 1)
    unittest.main()
