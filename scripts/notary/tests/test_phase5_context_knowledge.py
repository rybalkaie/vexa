"""Тесты Ф5 (план `2026-06-09-notary-memory-knowledge-rework`): вынос знаний в
контекст компании, company-scoping, перенос ОБЕИХ проекций глоссария и ростера
из оргструктуры.

Покрывает критерии «сделано» Ф5 + задачи 1–5 (поверх контракта Ф4
`docs/notary-context-contract.md`):
  - термины читаются из контекста компании (company-scope §1.5);
  - «оффер»/«АКБ» исправляются (A3, cross-термины);
  - «Bolong» в Anzhee НЕ подставляется, в МПервый — да (C2);
  - кросс-термин в ОБОИХ контекстах, в me/ нет (C3) + lint синхронности;
  - ЗАВ1: ростер из оргструктуры `*-context` с fallback на хардкод.

Дисциплина тестов (контракт §1.2): чистые проекции и company-scope фильтр —
инъекцией списка записей (БЕЗ pyyaml, всегда зелёные на системном python3).
Чтение реального YAML-фикстура — `skipTest`, если pyyaml недоступен/заглушен
(в прод-venv тесты исполняются полностью). Фикстуры синтетические (доменная
лексика, не сырьё реплик) — опасная тройка не нарушается.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase5_context_knowledge -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent          # scripts/notary
_SCRIPTS = _NOTARY.parent       # scripts  (чтобы `notary.*` импортировался как пакет)
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Импорт через `notary.*` — единый корень: glossary/series_roster/sources видят
# ТОТ ЖЕ объект context_knowledge, что и тест (monkeypatch применяется ко всем).
from notary.lib import context_knowledge as ck  # noqa: E402
from notary.lib import glossary  # noqa: E402
from notary.lib import series_roster  # noqa: E402
from notary.auto_vocab import sources  # noqa: E402

_FIXTURE_CONTEXT = _HERE / "fixtures" / "context"
ANZHEE_SLUG = series_roster.ANZHEE_COORDINATION_SLUG


def _real_yaml():
    """Реальный pyyaml (не заглушка) или None. Проверка В РАНТАЙМЕ — устойчиво к
    тому, что другой тест уже подменил sys.modules['yaml'] на пустышку."""
    try:
        import yaml
    except ImportError:
        return None
    return yaml if callable(getattr(yaml, "safe_load", None)) else None


# Инъекция: «распарсенные» записи glossary.yaml (без YAML). Покрывает все scope.
_ENTRIES = [
    {"canonical": "РСЯ", "scope": "anzhee", "note": "рекламная сеть Яндекса",
     "aliases": ["Гарсия", "Гарсии"], "protocol_regex": True},
    {"canonical": "Bolong", "scope": "mpfirst", "note": "поставщик мини-проекторов",
     "aliases": ["Лонг", "Лонга"], "protocol_regex": True,
     "protocol_regex_case_sensitive": True},
    {"canonical": "ЭДО", "scope": "cross", "note": "электронный документооборот",
     "asr_sounds_like": False},
    {"canonical": "Bravo", "scope": "anzhee", "note": "колонка ALTRONIX",
     "asr_sounds_like": False},
    {"canonical": "лиды", "scope": "anzhee", "note": "НЕ следы",
     "aliases": ["следы", "следов"], "protocol_regex": False},
]


def _apply(reps, text):
    for pat, rep in reps:
        text = pat.sub(rep, text)
    return text


# ===========================================================================
# Company-scope фильтр (контракт §1.5) — инъекция, всегда
# ===========================================================================
class TestCompanyScopeFilter(unittest.TestCase):

    def _canon(self, entries):
        return [e["canonical"] for e in entries]

    def test_anzhee_gets_anzhee_and_cross_not_mpfirst(self):
        out = ck.filter_glossary_by_company(_ENTRIES, "anzhee")
        self.assertIn("РСЯ", self._canon(out))
        self.assertIn("ЭДО", self._canon(out))      # cross
        self.assertNotIn("Bolong", self._canon(out))  # C2: mpfirst не виден в anzhee

    def test_mpfirst_gets_mpfirst_and_cross_not_anzhee(self):
        out = ck.filter_glossary_by_company(_ENTRIES, "mpfirst")
        self.assertIn("Bolong", self._canon(out))
        self.assertIn("ЭДО", self._canon(out))       # cross
        self.assertNotIn("РСЯ", self._canon(out))

    def test_none_company_gets_only_cross(self):
        out = ck.filter_glossary_by_company(_ENTRIES, None)
        self.assertEqual(self._canon(out), ["ЭДО"])

    def test_case_insensitive_company(self):
        self.assertEqual(
            self._canon(ck.filter_glossary_by_company(_ENTRIES, "ANZHEE")),
            self._canon(ck.filter_glossary_by_company(_ENTRIES, "anzhee")),
        )

    def test_entry_without_canonical_dropped(self):
        out = ck.filter_glossary_by_company([{"scope": "anzhee"}, {"scope": "cross"}], "anzhee")
        self.assertEqual(out, [])


# ===========================================================================
# Проекция B (глоссарий протокола) из записей — инъекция, всегда
# ===========================================================================
class TestProjectionBFromEntries(unittest.TestCase):

    def test_prompt_block_company_scoped(self):
        a = ck.filter_glossary_by_company(_ENTRIES, "anzhee")
        m = ck.filter_glossary_by_company(_ENTRIES, "mpfirst")
        self.assertIn("РСЯ", glossary.build_prompt_block(a))
        self.assertNotIn("Bolong", glossary.build_prompt_block(a))
        self.assertIn("Bolong", glossary.build_prompt_block(m))
        self.assertNotIn("РСЯ", glossary.build_prompt_block(m))

    def test_prompt_block_has_framing_and_rules(self):
        block = glossary.build_prompt_block(ck.filter_glossary_by_company(_ENTRIES, "anzhee"))
        self.assertIn("Глоссарий проекта", block)
        self.assertIn("ошибка распознавания", block)
        self.assertIn("Не добавляй термины", block)
        self.assertIn("«Гарсия»", block)  # правило из aliases

    def test_empty_entries_block_is_empty(self):
        self.assertEqual(glossary.build_prompt_block([]), "")

    def test_replacements_company_scoped(self):
        ra = glossary.build_protocol_replacements(ck.filter_glossary_by_company(_ENTRIES, "anzhee"))
        rm = glossary.build_protocol_replacements(ck.filter_glossary_by_company(_ENTRIES, "mpfirst"))
        self.assertEqual(_apply(ra, "лиды из Гарсии"), "лиды из РСЯ")
        self.assertEqual(_apply(ra, "Лонг приедет"), "Лонг приедет")   # C2: не в anzhee
        self.assertEqual(_apply(rm, "Лонг приедет"), "Bolong приедет")

    def test_case_sensitive_proper_noun_not_touch_common_word(self):
        rm = glossary.build_protocol_replacements(ck.filter_glossary_by_company(_ENTRIES, "mpfirst"))
        # «Лонг» case-sensitive: не трогает строчные «лонгслив»/«лонг-рид»
        self.assertEqual(_apply(rm, "купили лонгслив"), "купили лонгслив")
        self.assertEqual(_apply(rm, "это лонг-рид"), "это лонг-рид")

    def test_protocol_regex_false_not_applied(self):
        # «лиды» — protocol_regex:false (легитимные «следы»/«следов» НЕ трогаем)
        ra = glossary.build_protocol_replacements(ck.filter_glossary_by_company(_ENTRIES, "anzhee"))
        self.assertEqual(_apply(ra, "шли по следам"), "шли по следам")
        self.assertEqual(_apply(ra, "следов взлома нет"), "следов взлома нет")

    def test_replacements_idempotent(self):
        rm = glossary.build_protocol_replacements(ck.filter_glossary_by_company(_ENTRIES, "mpfirst"))
        once = _apply(rm, "Лонг и Лонга")
        self.assertEqual(_apply(rm, once), once)


# ===========================================================================
# Ф8 (У1/У2): merge builtin cross-cutting базис + YAML-секция guidance —
# не-терминные правила не теряются при активации YAML. Monkeypatch загрузчиков.
# ===========================================================================
class TestCrossCuttingGuidanceMerge(unittest.TestCase):

    def test_yaml_active_keeps_cross_cutting_base(self):
        from unittest import mock
        anzhee = ck.filter_glossary_by_company(_ENTRIES, "anzhee")
        with mock.patch.object(ck, "load_glossary", return_value=anzhee), \
             mock.patch.object(ck, "load_guidance", return_value=[]):
            block = glossary.glossary_prompt_block("anzhee")
        self.assertIn("РСЯ", block)                       # YAML-термин на месте
        self.assertIn("Ilya R.", block)                   # cross-cutting базис не потерян
        self.assertIn("VPS", block)                       # контекстное правило (VPN→VPS)
        self.assertNotEqual(block, glossary.PROJECT_GLOSSARY_PROMPT_BLOCK)  # это НЕ builtin-ветка

    def test_yaml_guidance_section_rendered(self):
        from unittest import mock
        anzhee = ck.filter_glossary_by_company(_ENTRIES, "anzhee")
        with mock.patch.object(ck, "load_glossary", return_value=anzhee), \
             mock.patch.object(ck, "load_guidance", return_value=["сумма всегда в рублях"]):
            block = glossary.glossary_prompt_block("anzhee")
        self.assertIn("Правила команды", block)
        self.assertIn("сумма всегда в рублях", block)
        self.assertIn("Ilya R.", block)                   # базис всё равно есть

    def test_no_yaml_uses_builtin_unchanged(self):
        from unittest import mock
        with mock.patch.object(ck, "load_glossary", return_value=[]), \
             mock.patch.object(ck, "load_guidance", return_value=["игнор"]):
            block = glossary.glossary_prompt_block("anzhee")
        self.assertEqual(block, glossary.PROJECT_GLOSSARY_PROMPT_BLOCK)

    def test_load_guidance_graceful_non_list(self):
        # load_guidance отдаёт [] при не-списке/мусоре (graceful как load_glossary)
        self.assertEqual(glossary.context_knowledge.load_guidance(None), [])


# ===========================================================================
# Проекция A (ASR-словарь) из записей — инъекция, всегда
# ===========================================================================
class TestProjectionAFromEntries(unittest.TestCase):

    def test_vocab_entries_company_scoped(self):
        va = sources.glossary_vocab_entries_from_entries(ck.filter_glossary_by_company(_ENTRIES, "anzhee"))
        vm = sources.glossary_vocab_entries_from_entries(ck.filter_glossary_by_company(_ENTRIES, "mpfirst"))
        self.assertIn("РСЯ", [e["content"] for e in va])
        self.assertNotIn("Bolong", [e["content"] for e in va])
        self.assertIn("Bolong", [e["content"] for e in vm])

    def test_format_is_content_sounds_like(self):
        va = sources.glossary_vocab_entries_from_entries(ck.filter_glossary_by_company(_ENTRIES, "anzhee"))
        rsya = next(e for e in va if e["content"] == "РСЯ")
        self.assertEqual(rsya["sounds_like"], ["Гарсия", "Гарсии"])

    def test_asr_sounds_like_false_excluded(self):
        # ЭДО (cross, asr_sounds_like:false) и Bravo (anzhee, false) не эмитятся
        all_entries = sources.glossary_vocab_entries_from_entries(_ENTRIES)
        contents = [e["content"] for e in all_entries]
        self.assertNotIn("ЭДО", contents)
        self.assertNotIn("Bravo", contents)

    def test_entry_without_aliases_excluded(self):
        out = sources.glossary_vocab_entries_from_entries(
            [{"canonical": "X", "scope": "anzhee", "asr_sounds_like": True}])
        self.assertEqual(out, [])


# ===========================================================================
# РИСК2 — ОБЕ проекции из ОДНОГО источника синхронно (критерий приёмки Ф5)
# ===========================================================================
class TestProjectionsSynchronized(unittest.TestCase):
    """Из одного списка записей: для mpfirst Bolong виден в проекции A (vocab),
    проекции B (промпт-блок) И проекции B (regex); для anzhee — НИ в одной.
    Это и есть синхронность РИСК2 (нельзя, чтобы LLM чинил по новому, ASR по старому)."""

    def test_bolong_consistent_across_projections_mpfirst(self):
        m = ck.filter_glossary_by_company(_ENTRIES, "mpfirst")
        proj_a = [e["content"] for e in sources.glossary_vocab_entries_from_entries(m)]
        block = glossary.build_prompt_block(m)
        reps = glossary.build_protocol_replacements(m)
        self.assertIn("Bolong", proj_a)                       # A
        self.assertIn("Bolong", block)                        # B-prompt
        self.assertEqual(_apply(reps, "Лонг"), "Bolong")      # B-regex

    def test_bolong_absent_across_projections_anzhee(self):
        a = ck.filter_glossary_by_company(_ENTRIES, "anzhee")
        proj_a = [e["content"] for e in sources.glossary_vocab_entries_from_entries(a)]
        block = glossary.build_prompt_block(a)
        reps = glossary.build_protocol_replacements(a)
        self.assertNotIn("Bolong", proj_a)
        self.assertNotIn("Bolong", block)
        self.assertEqual(_apply(reps, "Лонг"), "Лонг")        # не подставился


# ===========================================================================
# Lint синхронности кросс-дублей (контракт §1.5) — инъекция, всегда
# ===========================================================================
class TestCrossSyncLint(unittest.TestCase):

    def test_matching_cross_sets_ok(self):
        a = [{"canonical": "ЭДО", "scope": "cross", "aliases": []},
             {"canonical": "оффер", "scope": "cross", "aliases": ["эфир"]}]
        m = [{"canonical": "оффер", "scope": "cross", "aliases": ["эфир"]},
             {"canonical": "ЭДО", "scope": "cross"}]
        self.assertEqual(ck.lint_cross_sync_signatures({"anzhee": a, "mpfirst": m}), [])

    def test_missing_in_one_repo_flagged(self):
        a = [{"canonical": "ЭДО", "scope": "cross"},
             {"canonical": "АКБ", "scope": "cross", "aliases": ["АКП"]}]
        m = [{"canonical": "ЭДО", "scope": "cross"}]
        problems = ck.lint_cross_sync_signatures({"anzhee": a, "mpfirst": m})
        self.assertTrue(any("АКБ" in p for p in problems))

    def test_alias_mismatch_flagged(self):
        a = [{"canonical": "оффер", "scope": "cross", "aliases": ["эфир"]}]
        m = [{"canonical": "оффер", "scope": "cross", "aliases": ["эфир", "офер"]}]
        problems = ck.lint_cross_sync_signatures({"anzhee": a, "mpfirst": m})
        self.assertTrue(any("оффер" in p for p in problems))

    def test_non_cross_entries_ignored(self):
        a = [{"canonical": "РСЯ", "scope": "anzhee"}, {"canonical": "ЭДО", "scope": "cross"}]
        m = [{"canonical": "Bolong", "scope": "mpfirst"}, {"canonical": "ЭДО", "scope": "cross"}]
        self.assertEqual(ck.lint_cross_sync_signatures({"anzhee": a, "mpfirst": m}), [])

    def test_single_company_no_lint(self):
        self.assertEqual(ck.lint_cross_sync_signatures({"anzhee": []}), [])


# ===========================================================================
# ЗАВ1 — ростер из оргструктуры с fallback на хардкод (monkeypatch, всегда)
# ===========================================================================
class TestRosterFromOrgStructure(unittest.TestCase):

    def setUp(self):
        self._orig = ck.roster_for_series

    def tearDown(self):
        ck.roster_for_series = self._orig

    def test_get_roster_reads_org_structure_when_present(self):
        fake = [{"name": "Тест Тестов", "domain": "тест", "keywords": ["тест"]}]
        ck.roster_for_series = lambda slug: fake if slug == "any-series" else []
        self.assertEqual(series_roster.get_roster("any-series"), fake)

    def test_get_roster_falls_back_to_hardcode_when_no_yaml(self):
        # Пусто из контекста → fallback на встроенный _STATIC_ROSTERS (как в Ф3).
        ck.roster_for_series = lambda slug: []
        roster = series_roster.get_roster(ANZHEE_SLUG)
        names = [e["name"] for e in roster]
        self.assertIn("Мария Михина", names)
        self.assertIn("Сона Енгибарян", names)
        self.assertEqual(len(roster), 5)

    def test_get_roster_empty_for_blank_slug(self):
        ck.roster_for_series = lambda slug: [{"name": "X", "domain": "y", "keywords": []}]
        self.assertEqual(series_roster.get_roster(None), [])
        self.assertEqual(series_roster.get_roster(""), [])

    def test_unknown_series_no_yaml_no_hardcode_empty(self):
        ck.roster_for_series = lambda slug: []
        self.assertEqual(series_roster.get_roster("unknown-series-xyz"), [])


# ===========================================================================
# Graceful degradation — нет клона/файла/yaml → пусто/fallback, не падаем (всегда)
# ===========================================================================
class TestGracefulDegradation(unittest.TestCase):

    def setUp(self):
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        self._tmp = tempfile.mkdtemp()  # пустой корень — клонов нет
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved

    def test_repo_for_company(self):
        self.assertEqual(ck.repo_for_company("anzhee"), "anzhee-context")
        self.assertEqual(ck.repo_for_company("mpfirst"), "mpfirst-context")
        self.assertIsNone(ck.repo_for_company("zzz"))
        self.assertIsNone(ck.repo_for_company(None))

    def test_loaders_empty_without_clone(self):
        self.assertEqual(ck.load_glossary("anzhee"), [])
        self.assertEqual(ck.load_org_structure("anzhee"), {})
        self.assertEqual(ck.roster_for_series(ANZHEE_SLUG), [])
        self.assertIsNone(ck.company_for_series(ANZHEE_SLUG))

    def test_prompt_block_falls_back_to_builtin(self):
        # company задан, но знания нет → встроенный блок (поведение как до Ф5)
        self.assertEqual(glossary.glossary_prompt_block("anzhee"),
                         glossary.PROJECT_GLOSSARY_PROMPT_BLOCK)

    def test_corrections_fall_back_to_builtin(self):
        # company задан, знания нет → встроенные _PROTOCOL_REPLACEMENTS
        self.assertEqual(glossary.apply_glossary_corrections("Гарсия даёт трафик", "anzhee"),
                         "РСЯ даёт трафик")

    def test_roster_falls_back_to_hardcode(self):
        roster = series_roster.get_roster(ANZHEE_SLUG)
        self.assertEqual(len(roster), 5)  # встроенный _STATIC_ROSTERS

    def test_lint_cross_sync_no_clones_ok(self):
        self.assertEqual(ck.lint_cross_sync(), [])  # <2 прочитанных репо → []


# ===========================================================================
# YAML-фикстуры — company-scope ПРОЯВЛЯЕТСЯ через реальные файлы (skip без yaml)
# ===========================================================================
class TestYamlFixtures(unittest.TestCase):

    def setUp(self):
        if not _real_yaml():
            self.skipTest("pyyaml недоступен/заглушён — фикстурный тест пропущен")
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_FIXTURE_CONTEXT)

    def tearDown(self):
        if getattr(self, "_saved", "__unset__") == "__unset__":
            return
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved

    def _canon(self, company):
        return [e["canonical"] for e in ck.load_glossary(company)]

    def test_bolong_scope_anzhee_absent_mpfirst_present(self):
        """Главный критерий C2: Bolong в Anzhee-загрузке отсутствует, в МПервый — есть."""
        self.assertNotIn("Bolong", self._canon("anzhee"))
        self.assertIn("Bolong", self._canon("mpfirst"))
        self.assertIn("РСЯ", self._canon("anzhee"))
        self.assertNotIn("РСЯ", self._canon("mpfirst"))

    def test_cross_terms_in_both_contexts(self):
        """C3: кросс-термины (ЭДО/оффер/АКБ) в ОБОИХ контекстах."""
        for company in ("anzhee", "mpfirst"):
            canon = self._canon(company)
            for term in ("ЭДО", "оффер", "АКБ"):
                self.assertIn(term, canon, f"{term} отсутствует в {company}")

    def test_cross_sync_lint_clean_on_fixtures(self):
        self.assertEqual(ck.lint_cross_sync(), [])

    def test_offer_akb_corrected(self):
        """A3 (cross, обе компании): «АКП»→«АКБ» — детерминированный regex (АКП без
        легитимного значения в домене). «эфир»→«оффер» — на УРОВНЕ ASR-словаря
        (sounds_like) + промпт-подсказки, НЕ слепым regex по тексту: «эфир» —
        частотное легитимное слово (прямой эфир/выход в эфир, у МПервый — канал
        продаж), слепая замена его затёрла бы (инвариант glossary.py:15-23, как
        «следы»→«лиды»). Демотировано в ходе 1 цикла5 (Н1)."""
        for company in ("anzhee", "mpfirst"):
            # АКП → АКБ: детерминированная замена (домен-безопасно)
            self.assertEqual(
                glossary.apply_glossary_corrections("АКП выросла", company),
                "АКБ выросла")
            # «эфир» как ASR-искажение «оффер» несётся в проекции A (sounds_like)
            # и в промпт-блоке (подсказка LLM)…
            va = {e["content"]: e["sounds_like"] for e in sources.glossary_vocab_entries(company)}
            self.assertIn("эфир", va.get("оффер", []))
            self.assertIn("«эфир»", glossary.glossary_prompt_block(company))
            # …но НЕ слепым regex: легитимный «прямой эфир» пост-проход НЕ трогает.
            self.assertEqual(
                glossary.apply_glossary_corrections("вышли в прямой эфир", company),
                "вышли в прямой эфир")

    def test_bolong_substitution_company_scoped(self):
        # «Лонг» → Bolong только в МПервый; в Anzhee остаётся «Лонг» (C2)
        self.assertEqual(glossary.apply_glossary_corrections("приедет Лонг", "mpfirst"),
                         "приедет Bolong")
        self.assertEqual(glossary.apply_glossary_corrections("приедет Лонг", "anzhee"),
                         "приедет Лонг")

    def test_prompt_block_company_scoped_from_yaml(self):
        self.assertIn("РСЯ", glossary.glossary_prompt_block("anzhee"))
        self.assertNotIn("Bolong", glossary.glossary_prompt_block("anzhee"))
        self.assertIn("Bolong", glossary.glossary_prompt_block("mpfirst"))

    def test_projection_a_company_scoped_from_yaml(self):
        va = [e["content"] for e in sources.glossary_vocab_entries("anzhee")]
        vm = [e["content"] for e in sources.glossary_vocab_entries("mpfirst")]
        self.assertIn("РСЯ", va)
        self.assertNotIn("Bolong", va)
        self.assertIn("Bolong", vm)

    def test_org_structure_anzhee_roster(self):
        org = ck.load_org_structure("anzhee")
        self.assertIn(ANZHEE_SLUG, org)
        names = [r["name"] for r in org[ANZHEE_SLUG]]
        self.assertEqual(len(names), 5)
        for expected in ("Мария Михина", "Сона Енгибарян", "Михаил Саргин",
                         "Дарья Набережная", "Ольга Новикова"):
            self.assertIn(expected, names)

    def test_roster_and_company_for_series_from_yaml(self):
        self.assertEqual(len(ck.roster_for_series(ANZHEE_SLUG)), 5)
        self.assertEqual(ck.company_for_series(ANZHEE_SLUG), "anzhee")
        # МПервый-стаб: rosters пуст → серия не резолвится (НЕ выдумываем роли)
        self.assertEqual(ck.load_org_structure("mpfirst"), {})

    def test_get_roster_uses_yaml_over_hardcode(self):
        # С фикстурой get_roster читает YAML (тот же состав, что хардкод) —
        # источник теперь оргструктура, не _STATIC_ROSTERS.
        roster = series_roster.get_roster(ANZHEE_SLUG)
        self.assertEqual(len(roster), 5)
        self.assertEqual(roster[0]["name"], "Мария Михина")
        self.assertEqual(roster[0]["domain"], "поставки")


if __name__ == "__main__":
    unittest.main()
