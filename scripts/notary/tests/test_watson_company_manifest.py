"""Тесты Ф2 плана `watson-company-memory`: per-company манифест знания + бриф компании.

Покрывает критерии приёмки дословно:
  R3 — манифест компании вместо хардкод-каталога; нет/пустой манифест → поведение как
       сегодня (код-фолбэк), без падения.
  R4 — знание Anzhee (сухой прокат) ВХОДИТ в промпт; каталог МПервый по-прежнему входит
       (регресс R6 не сломан).
  R5 — компактная секция «Знание о компании» ОТДЕЛЬНО от глоссария и каталога; бюджет
       применяется к суммарному брифу; превышение → WARNING, не молча.
  R9 — правка манифеста меняет состав знания БЕЗ правки кода; жёсткий бэкстоп живёт в
       коде и правкой манифеста НЕ обходится (A3).
  + видимый сигнал «файл X не подключён, причина Y» (follow-up 1 из Ф1).

🔒 Главный инвариант фазы: манифест — это ДАННЫЕ в чужом репо, их правит команда, НЕ
прогоняя наши тесты. Поэтому манифест не может РАСШИРИТЬ права: фикстура намеренно
«отравлена» (restricted/ + экономика + финмодель + абсолютный путь + `..`), и тесты
доказывают, что вписать их МОЖНО, а прочитать — НЕЛЬЗЯ.

Два дерева фикстур — намеренно:
  • `fixtures/context/`          — БЕЗ манифеста: мир код-фолбэка (поведение как до Ф2).
  • `fixtures/context-manifest/` — С манифестом: мир Ф2. Второе дерево нужно потому, что
    тесты Ф1/ISS-21 патчат `PROTOCOL_CONTEXT_WHITELIST` и ждут, что источник состава —
    именно он; манифест в том же дереве молча перебил бы константу, и те тесты перестали
    бы проверять то, что заявляют.

Тестам не нужен pyyaml (знание — markdown, читается как байты) и не нужен `claude` CLI.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_watson_company_manifest -v
"""
from __future__ import annotations

import logging
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

from notary.lib import context_knowledge as ck  # noqa: E402
from notary.lib import glossary  # noqa: E402
from notary.lib import llm_postprocess  # noqa: E402

_FIXTURE_MANIFEST = _HERE / "fixtures" / "context-manifest"   # мир Ф2 (с манифестом)
_FIXTURE_LEGACY = _HERE / "fixtures" / "context"              # мир до Ф2 (без манифеста)

# Сентинели фикстур.
_CATALOG = "Pulsar 63ML"                    # каталог МПервый
_BRAND = "BRAND_SENTINEL_ALTRONIX_IDENTITY"  # бриф МПервый: бренд
_ROLES = "ROLES_SENTINEL_KOMANDA"            # бриф МПервый: кто за что отвечает
_BAZA_OK = "BAZA_ZNANIY_SAFE_SENTINEL"       # безопасный файл baza-znaniy (в манифесте)
_DRY_RENTAL = "DRY_RENTAL_SENTINEL_ANZHEE"   # бриф Anzhee: сухой прокат
_TEAM_ANZHEE = "TEAM_SENTINEL_ANZHEE"        # бриф Anzhee: команда

# Чувствительное — вписано в манифест-фикстуру, но НЕ должно попасть в промпт.
_SENSITIVE = (
    "SENSITIVE_SENTINEL_SALARIES_MUST_NOT_LEAK",
    "SENSITIVE_SENTINEL_EKONOMIKA_MUST_NOT_LEAK",
    "SENSITIVE_SENTINEL_FINMODEL_MUST_NOT_LEAK",
)
# Транслит-финфайл: лежит рядом с безопасным в baza-znaniy, паттернами НЕ ловится —
# его защищает ТОЛЬКО отсутствие в манифесте (РИСК1).
_TRANSLIT_FIN = "TRANSLIT_FIN_SENTINEL_DDS_MUST_NOT_LEAK"

_METHOD = "# Метод (заглушка для теста)\n"


class _ContextRoot:
    """Указывает MEETING_NOTARY_CONTEXT_DIR на дерево фикстур и восстанавливает после."""

    _ROOT: Path = _FIXTURE_MANIFEST

    def setUp(self):
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(self._ROOT)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved


# ===========================================================================
# Парсер манифеста — чистый, без IO
# ===========================================================================
class TestParseManifest(unittest.TestCase):

    def test_skips_comments_and_blank_lines(self):
        catalog, brief = ck.parse_manifest(
            "# комментарий\n\n   \ncompany/a.md\n# ещё коммент\ncompany/b.md\n"
        )
        self.assertEqual(catalog, [])
        self.assertEqual(brief, ["company/a.md", "company/b.md"])

    def test_catalog_prefix_separates_kinds(self):
        catalog, brief = ck.parse_manifest("catalog: knowledge/cat.md\ncompany/a.md\n")
        self.assertEqual(catalog, ["knowledge/cat.md"])
        self.assertEqual(brief, ["company/a.md"])

    def test_order_is_preserved_and_dupes_collapse(self):
        """Порядок = приоритет при бюджете, поэтому он обязан сохраняться."""
        catalog, brief = ck.parse_manifest("company/b.md\ncompany/a.md\ncompany/b.md\n")
        self.assertEqual(brief, ["company/b.md", "company/a.md"])

    def test_inline_comment_and_dot_slash_normalised(self):
        _c, brief = ck.parse_manifest("./company/a.md   # почему взяли\n")
        self.assertEqual(brief, ["company/a.md"])

    def test_empty_text_gives_empty(self):
        self.assertEqual(ck.parse_manifest(""), ([], []))
        self.assertEqual(ck.parse_manifest("# только комментарий\n"), ([], []))

    def test_parser_does_not_judge_safety(self):
        """Разбор НЕ фильтрует — фильтр один и он на чтении (единая точка правды)."""
        _c, brief = ck.parse_manifest("restricted/salaries.md\n")
        self.assertEqual(brief, ["restricted/salaries.md"])


# ===========================================================================
# R3 — манифест определяет состав; нет манифеста → поведение как сегодня
# ===========================================================================
class TestR3ManifestIsTheSource(_ContextRoot, unittest.TestCase):

    def test_bot_reads_exactly_what_manifest_lists(self):
        """R3 дословно: «список её бизнес-файлов; бот читает именно их»."""
        b = ck.load_protocol_knowledge("mpfirst")
        self.assertEqual(b.source, "manifest")
        rels = [r for r, _ in b.catalog] + [r for r, _ in b.brief]
        self.assertEqual(rels, [
            "knowledge/product-catalog-wb.md",              # catalog: → первым
            "company/brands/altronix/identity.md",
            "knowledge/notary/komanda-roli.md",
            "company/baza-znaniy/pozitsionirovanie.md",
        ])

    def test_manifest_brings_knowledge_hardcode_never_had(self):
        """Ядро фазы: бриф несёт то, чего в хардкод-whitelist не было и быть не могло."""
        blob = "\n".join(t for _, t in ck.load_protocol_knowledge("mpfirst").brief)
        self.assertIn(_BRAND, blob)
        self.assertIn(_ROLES, blob)
        self.assertNotIn("knowledge/product-catalog-wb.md", ck.PROTOCOL_CONTEXT_WHITELIST[1:])


class TestR3NoManifestFallsBack(_ContextRoot, unittest.TestCase):
    """R3, вторая половина: «Пустой/нет манифеста → поведение как сегодня, без падения»."""

    _ROOT = _FIXTURE_LEGACY  # дерево БЕЗ манифеста

    def test_no_manifest_uses_code_fallback(self):
        b = ck.load_protocol_knowledge("mpfirst")
        self.assertEqual(b.source, "code-fallback")
        self.assertEqual([r for r, _ in b.catalog], list(ck.PROTOCOL_CONTEXT_WHITELIST))
        self.assertEqual(b.brief, [])

    def test_catalog_still_in_prompt_without_manifest(self):
        """РИСК2: манифест не доехал на VPS → канон артикулов ISS-21 всё равно в промпте."""
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", _METHOD)
        self.assertIn(_CATALOG, sp)
        self.assertIn("услышано", sp)  # правило R7 на месте

    def test_no_brief_section_without_manifest(self):
        self.assertEqual(glossary.company_knowledge_block("mpfirst"), "")


class TestR3EmptyManifestFallsBack(unittest.TestCase):
    """Манифест есть, но пуст по сути (одни комментарии) → код-фолбэк, не пустой промпт."""

    def setUp(self):
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        self._tmp = tempfile.mkdtemp()
        repo = Path(self._tmp) / "mpfirst-context"
        (repo / "knowledge" / "notary").mkdir(parents=True)
        (repo / "knowledge" / "product-catalog-wb.md").write_text(
            f"# каталог\n{_CATALOG}\n", encoding="utf-8"
        )
        (repo / "knowledge" / "notary" / ck.PROTOCOL_KNOWLEDGE_MANIFEST_FILE).write_text(
            "# всё закомментировано\n#company/a.md\n", encoding="utf-8"
        )
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved

    def test_empty_manifest_falls_back_to_code(self):
        b = ck.load_protocol_knowledge("mpfirst")
        self.assertEqual(b.source, "code-fallback")
        self.assertIn(_CATALOG, "\n".join(t for _, t in b.catalog))


# ===========================================================================
# R4 — знание Anzhee стало видимым; каталог МПервый не потерян (регресс R6)
# ===========================================================================
class TestR4AnzheeBecomesVisible(_ContextRoot, unittest.TestCase):

    def test_anzhee_knowledge_enters_prompt(self):
        """R4 дословно: знание сухого проката ВХОДИТ в промпт (раньше не входило вовсе)."""
        sp = llm_postprocess._build_protocol_system_prompt("anzhee", _METHOD)
        self.assertIn(_DRY_RENTAL, sp)
        self.assertIn(_TEAM_ANZHEE, sp)
        self.assertIn("Знание о компании", sp)

    def test_mpfirst_catalog_still_enters_prompt(self):
        """R4, вторая половина: регресс R6 не сломан — WB-каталог по-прежнему в промпте."""
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", _METHOD)
        self.assertIn(_CATALOG, sp)
        self.assertIn("услышано", sp)

    def test_anzhee_has_no_catalog_section(self):
        """Честно: каталога товаров у Anzhee НЕТ ни одного файла → секции каталога нет.
        Знание входит брифом. Заведут каталог — секция появится сама, кода не трогать."""
        self.assertEqual(glossary.catalog_prompt_block("anzhee"), "")
        self.assertNotEqual(glossary.company_knowledge_block("anzhee"), "")

    def test_company_isolation_holds(self):
        """Знание МПервый не утекает в промпт Anzhee и наоборот."""
        sp_anzhee = llm_postprocess._build_protocol_system_prompt("anzhee", _METHOD)
        self.assertNotIn(_CATALOG, sp_anzhee)
        self.assertNotIn(_BRAND, sp_anzhee)
        sp_mp = llm_postprocess._build_protocol_system_prompt("mpfirst", _METHOD)
        self.assertNotIn(_DRY_RENTAL, sp_mp)


# ===========================================================================
# R5 — секция «Знание о компании»: отдельная, как ДАННЫЕ, в бюджете
# ===========================================================================
class TestR5BriefSection(_ContextRoot, unittest.TestCase):

    def test_section_exists_and_is_named(self):
        block = glossary.company_knowledge_block("mpfirst")
        self.assertIn("Знание о компании", block)
        self.assertIn(_BRAND, block)
        self.assertIn(_ROLES, block)

    def test_section_is_separate_from_glossary_and_catalog(self):
        """R5 дословно: бриф ОТДЕЛЬНО от глоссария (soft-ASR-bias не разбавлен) и каталога.
        Проверяем фактическим ПОРЯДКОМ в собранном промпте: глоссарий → каталог → бриф."""
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", _METHOD)
        i_catalog = sp.index(_CATALOG)
        i_brief = sp.index(_BRAND)
        i_rule = sp.index(glossary.CATALOG_UNKNOWN_TOKEN_RULE[:40])
        self.assertLess(i_catalog, i_brief, "бриф обязан идти ПОСЛЕ каталога")
        self.assertLess(i_rule, i_brief, "правило каталога закрывается ДО начала брифа")
        # Секции разделены явным разделителем — это не одна слипшаяся простыня.
        self.assertIn("---", sp[i_catalog:i_brief])

    def test_brief_is_wrapped_as_data_anti_injection(self):
        """Знание — ДАННЫЕ, не приказы (конвенция плана): бриф ВНУТРИ рамки."""
        block = glossary.company_knowledge_block("mpfirst")
        self.assertIn(glossary.KNOWLEDGE_DATA_FRAME_HEADER, block)
        self.assertIn(glossary.KNOWLEDGE_DATA_FRAME_END, block)
        i_head = block.index(glossary.KNOWLEDGE_DATA_FRAME_HEADER)
        i_data = block.index(_BRAND)
        i_end = block.index(glossary.KNOWLEDGE_DATA_FRAME_END)
        self.assertLess(i_head, i_data)
        self.assertLess(i_data, i_end)

    def test_our_own_rule_stays_OUTSIDE_the_frame(self):
        """Наша инструкция модели — СНАРУЖИ рамки, иначе сами себе её обесценим
        (тот же инвариант, что сторожит правило каталога в Ф1)."""
        block = glossary.company_knowledge_block("mpfirst")
        i_end = block.index(glossary.KNOWLEDGE_DATA_FRAME_END)
        i_rule = block.index(glossary.COMPANY_KNOWLEDGE_USAGE_RULE[:40])
        self.assertGreater(i_rule, i_end)

    def test_brief_names_its_source_file(self):
        """У каждого куска брифа виден источник — иначе при плохом протоколе не понять,
        какой файл знания сбил модель."""
        block = glossary.company_knowledge_block("mpfirst")
        self.assertIn("company/brands/altronix/identity.md", block)

    def test_empty_brief_renders_nothing(self):
        self.assertEqual(glossary.company_knowledge_block(None), "")


class TestR5BudgetOnTotalBrief(unittest.TestCase):
    """Бюджет Ф1 применяется к СУММЕ каталог+бриф; каталог первым и потому переживает."""

    def setUp(self):
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        self._tmp = tempfile.mkdtemp()
        self._repo = Path(self._tmp) / "mpfirst-context"
        (self._repo / "knowledge" / "notary").mkdir(parents=True)
        (self._repo / "company").mkdir(parents=True)
        (self._repo / "knowledge" / "product-catalog-wb.md").write_text(
            f"# каталог\n{_CATALOG}\n" + "к" * 500, encoding="utf-8"
        )
        (self._repo / "company" / "big.md").write_text("Б" * 5000, encoding="utf-8")
        (self._repo / "company" / "tail.md").write_text("ХВОСТ_НЕ_ВЛЕЗ", encoding="utf-8")
        (self._repo / "knowledge" / "notary" / ck.PROTOCOL_KNOWLEDGE_MANIFEST_FILE).write_text(
            "catalog: knowledge/product-catalog-wb.md\ncompany/big.md\ncompany/tail.md\n",
            encoding="utf-8",
        )
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._tmp
        self._orig_budget = ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES
        ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES = 1200  # каталог влезает, big.md — нет
        self.addCleanup(
            lambda: setattr(ck, "PROTOCOL_CONTEXT_TOTAL_MAX_BYTES", self._orig_budget)
        )

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved

    def test_catalog_survives_budget_pressure(self):
        """ISS-21: при переполнении бюджета выпадает хвост брифа, но НЕ канон артикулов."""
        b = ck.load_protocol_knowledge("mpfirst")
        self.assertIn(_CATALOG, "\n".join(t for _, t in b.catalog))

    def test_overflow_is_loud_not_silent(self):
        """R5 дословно: «превышение → WARNING не молча»."""
        with self.assertLogs("notary.lib.context_knowledge", level=logging.WARNING) as cm:
            b = ck.load_protocol_knowledge("mpfirst")
        self.assertTrue(any("бюджет знания исчерпан" in m for m in cm.output))
        # …и выпавшее видно человеку, а не только логу VPS.
        dropped = [rel for rel, _r in b.rejected]
        self.assertIn("company/big.md", dropped)
        self.assertIn("company/tail.md", dropped)

    def test_dropped_file_text_never_reaches_prompt(self):
        blob = glossary.company_knowledge_block("mpfirst")
        self.assertNotIn("ХВОСТ_НЕ_ВЛЕЗ", blob)


# ===========================================================================
# R9 — состав правится без деплоя; бэкстоп в КОДЕ и манифестом не обходится
# ===========================================================================
class TestR9ManifestCannotOpenFinances(_ContextRoot, unittest.TestCase):
    """A3: «редактирование манифеста не должно уметь открыть финансы»."""

    def test_sensitive_paths_in_manifest_never_reach_prompt(self):
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", _METHOD)
        for sentinel in _SENSITIVE:
            self.assertNotIn(sentinel, sp, f"чувствительное утекло в промпт: {sentinel}")

    def test_sensitive_paths_are_rejected_with_reason(self):
        b = ck.load_protocol_knowledge("mpfirst")
        by_path = dict(b.rejected)
        self.assertIn("restricted/salaries.md", by_path)
        self.assertIn("restricted", by_path["restricted/salaries.md"])
        self.assertIn("денилист", by_path["knowledge/altronix-ekonomika.md"])
        self.assertIn("паттерн", by_path["knowledge/finmodel-2027.md"])
        self.assertIn("/etc/hosts", by_path)
        self.assertIn("knowledge/../../../../etc/passwd", by_path)

    def test_missing_file_is_rejected_not_crashed(self):
        """Сборка не падает от кривой строки в манифесте — просто честно отчитывается."""
        by_path = dict(ck.load_protocol_knowledge("mpfirst").rejected)
        self.assertIn("company/net-takogo-fayla.md", by_path)
        self.assertIn("нет", by_path["company/net-takogo-fayla.md"])

    def test_translit_fin_neighbour_not_listed_stays_out(self):
        """РИСК1 наглядно: транслит-финфайл лежит В ТОЙ ЖЕ папке, что разрешённый, и
        паттернами НЕ ловится. Его держит снаружи ровно одно — он не вписан в манифест.
        Это и есть закон «baza-znaniy — только поимённо, НИКОГДА glob папки»."""
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", _METHOD)
        self.assertIn(_BAZA_OK, sp)          # сосед из той же папки — вошёл (он в манифесте)
        self.assertNotIn(_TRANSLIT_FIN, sp)  # финфайл — нет (его в манифесте нет)
        # Докажем, что дело именно в манифесте, а не в бэкстопе: бэкстоп его НЕ блокирует.
        self.assertIsNone(ck.context_path_block_reason("company/baza-znaniy/924-uchet-dds.md"))

    def test_manifest_filter_uses_the_single_backstop(self):
        """Инвариант Ф1: фильтр манифеста ОБЯЗАН звать `context_path_block_reason`, а не
        заводить второй фильтр. Подменяем точку правды — и она обязана быть вызвана."""
        seen: list[str] = []
        orig = ck.context_path_block_reason

        def spy(rel):
            seen.append(str(rel))
            return orig(rel)

        ck.context_path_block_reason = spy
        self.addCleanup(lambda: setattr(ck, "context_path_block_reason", orig))
        ck.load_protocol_knowledge("mpfirst")
        self.assertIn("restricted/salaries.md", seen)
        self.assertIn("company/brands/altronix/identity.md", seen)


class TestR9EditWithoutDeploy(unittest.TestCase):
    """R9 дословно: «правка манифеста меняет состав знания без выкатки бота»."""

    def setUp(self):
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        self._tmp = tempfile.mkdtemp()
        self._repo = Path(self._tmp) / "anzhee-context"
        (self._repo / "knowledge" / "notary").mkdir(parents=True)
        (self._repo / "company").mkdir(parents=True)
        (self._repo / "company" / "a.md").write_text("ФАЙЛ_А", encoding="utf-8")
        (self._repo / "company" / "b.md").write_text("ФАЙЛ_Б", encoding="utf-8")
        self._manifest = self._repo / "knowledge" / "notary" / ck.PROTOCOL_KNOWLEDGE_MANIFEST_FILE
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved

    def test_editing_manifest_changes_prompt_without_touching_code(self):
        self._manifest.write_text("company/a.md\n", encoding="utf-8")
        sp1 = llm_postprocess._build_protocol_system_prompt("anzhee", _METHOD)
        self.assertIn("ФАЙЛ_А", sp1)
        self.assertNotIn("ФАЙЛ_Б", sp1)

        # Единственное, что изменилось между сборками, — ТЕКСТОВЫЙ ФАЙЛ в репо знаний.
        self._manifest.write_text("company/b.md\n", encoding="utf-8")
        sp2 = llm_postprocess._build_protocol_system_prompt("anzhee", _METHOD)
        self.assertIn("ФАЙЛ_Б", sp2)
        self.assertNotIn("ФАЙЛ_А", sp2)


# ===========================================================================
# Видимый сигнал: «твой файл не подключён, причина Y» (follow-up 1 из Ф1)
# ===========================================================================
class TestVisibleSignal(_ContextRoot, unittest.TestCase):

    def test_report_lists_what_entered_and_what_did_not(self):
        report = ck.manifest_report("mpfirst")
        self.assertIn("knowledge/product-catalog-wb.md", report)   # вошло
        self.assertIn("restricted/salaries.md", report)            # не вошло
        self.assertIn("❌", report)
        self.assertIn("денилист", report)

    def test_report_never_contains_file_text(self):
        """Опасная тройка: отчёт — про пути и причины, НЕ про содержимое."""
        report = ck.manifest_report("mpfirst")
        for sentinel in _SENSITIVE + (_CATALOG, _BRAND, _TRANSLIT_FIN):
            self.assertNotIn(sentinel, report)

    def test_alert_is_silent_when_manifest_is_healthy(self):
        """Никакого спама: здоровый манифест → push не зовётся вовсе."""
        calls: list[str] = []
        healthy = ck.KnowledgeBundle(catalog=[], brief=[], rejected=[], source="manifest")
        sent = ck.alert_manifest_problems(
            "mpfirst", healthy, pusher=lambda msg, **kw: calls.append(msg) or True
        )
        self.assertFalse(sent)
        self.assertEqual(calls, [])

    def test_alert_fires_with_path_and_reason(self):
        calls: list[tuple[str, dict]] = []
        bundle = ck.load_protocol_knowledge("mpfirst")
        sent = ck.alert_manifest_problems(
            "mpfirst", bundle,
            pusher=lambda msg, **kw: (calls.append((msg, kw)), True)[1],
        )
        self.assertTrue(sent)
        msg, kw = calls[0]
        self.assertIn("restricted/salaries.md", msg)
        self.assertIn("НЕ подключены", msg)
        self.assertIn("dedupe_key", kw)  # дедуп по набору проблем — не спамим каждой встречей

    def test_alert_never_leaks_file_text(self):
        calls: list[str] = []
        bundle = ck.load_protocol_knowledge("mpfirst")
        ck.alert_manifest_problems(
            "mpfirst", bundle, pusher=lambda msg, **kw: (calls.append(msg), True)[1]
        )
        for sentinel in _SENSITIVE + (_CATALOG, _BRAND):
            self.assertNotIn(sentinel, calls[0])

    def test_broken_pusher_does_not_break_protocol(self):
        """Алерт — сервис, а не хозяин: его падение не имеет права уронить сборку."""
        def boom(msg, **kw):
            raise RuntimeError("telegram down")

        bundle = ck.load_protocol_knowledge("mpfirst")
        self.assertFalse(ck.alert_manifest_problems("mpfirst", bundle, pusher=boom))


class TestIss21CatalogMissingFromManifest(unittest.TestCase):
    """Каталог физически есть, но его забыли вписать в манифест → громкий WARNING.
    Молча дописывать его НЕ имеем права: R3 требует «бот читает ИМЕННО их»."""

    def setUp(self):
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        self._tmp = tempfile.mkdtemp()
        repo = Path(self._tmp) / "mpfirst-context"
        (repo / "knowledge" / "notary").mkdir(parents=True)
        (repo / "company").mkdir(parents=True)
        (repo / "knowledge" / "product-catalog-wb.md").write_text(_CATALOG, encoding="utf-8")
        (repo / "company" / "a.md").write_text("ФАЙЛ_А", encoding="utf-8")
        (repo / "knowledge" / "notary" / ck.PROTOCOL_KNOWLEDGE_MANIFEST_FILE).write_text(
            "company/a.md\n", encoding="utf-8"  # каталог забыли
        )
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved

    def test_forgotten_catalog_is_loud(self):
        with self.assertLogs("notary.lib.context_knowledge", level=logging.WARNING) as cm:
            b = ck.load_protocol_knowledge("mpfirst")
        self.assertTrue(any("канон артикулов" in m for m in cm.output))
        self.assertEqual(b.catalog, [])  # но волю команды не подменяем


if __name__ == "__main__":
    unittest.main(verbosity=2)
