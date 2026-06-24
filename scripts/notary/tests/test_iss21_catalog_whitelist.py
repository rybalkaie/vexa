"""Тесты Ф2 (план `2026-06-24-vtoroy-mozg-ai-klon`, ISS-21 Gap-2): потребитель
читает нужный `knowledge/` — БЕЛЫЙ СПИСОК (каталог товаров), НЕ финансовые карты.

Покрывает критерии «сделано» Ф2:
  - R6 — при сборке протокола в контексте бота есть каталог товаров (whitelist),
    НЕТ финансовых карт (денилист/allow-list); фикстурная финкарта не утекает.
  - R7 — правило «незнакомый товаро-подобный токен → догадка + (услышано …)»
    присутствует в контексте сборки (не голая фонетика).

🔒 Приватность — главный инвариант фазы: источник каталога — ТОЛЬКО явный allow-list
`context_knowledge.PROTOCOL_CONTEXT_WHITELIST`; финкарта в фикстуре лежит рядом, но
в контекст не попадает (fail-closed). Тесты не требуют pyyaml (каталог — markdown,
читается как байты); фикстуры синтетические (без реплик) — Опасная тройка цела.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss21_catalog_whitelist -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent          # scripts/notary
_SCRIPTS = _NOTARY.parent       # scripts (чтобы `notary.*` импортировался как пакет)
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.lib import context_knowledge as ck  # noqa: E402
from notary.lib import glossary  # noqa: E402
from notary.lib import llm_postprocess  # noqa: E402

_FIXTURE_CONTEXT = _HERE / "fixtures" / "context"

# Сентинели фикстур.
_CATALOG_TOKEN = "Pulsar 63ML"                       # реальный товар ENVONIX в каталоге
_FIN_SENTINEL = "FINSENTINEL_MUST_NOT_LEAK_42424242"  # строка из финкарты — НЕ должна утечь
_FIN_FILE = "accounting-tables-registry.md"


class _FixtureContextMixin:
    """Указывает MEETING_NOTARY_CONTEXT_DIR на фикстуру и восстанавливает после."""

    def setUp(self):
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_FIXTURE_CONTEXT)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved


# ===========================================================================
# Инварианты whitelist (контрактный self-check) — без IO, всегда
# ===========================================================================
class TestWhitelistInvariants(unittest.TestCase):

    def test_whitelist_safe(self):
        """allow-list ∩ денилист = ∅, нет абсолютных/`..` путей."""
        ck.assert_whitelist_safe()  # не должно бросить

    def test_denylist_has_financial_maps(self):
        """Финкарты явно в денилисте-бэкстопе (грепаемое доказательство R6)."""
        self.assertIn("accounting-tables-registry.md", ck.PROTOCOL_CONTEXT_DENYLIST)
        self.assertIn("payment-planning-tables-map.md", ck.PROTOCOL_CONTEXT_DENYLIST)

    def test_whitelist_has_catalog_not_financial(self):
        self.assertIn("knowledge/product-catalog-wb.md", ck.PROTOCOL_CONTEXT_WHITELIST)
        for w in ck.PROTOCOL_CONTEXT_WHITELIST:
            self.assertNotIn(Path(w).name, ck.PROTOCOL_CONTEXT_DENYLIST)


# ===========================================================================
# R6 — load_whitelisted_context: каталог ДА, финкарта НЕТ
# ===========================================================================
class TestLoadWhitelistedContext(_FixtureContextMixin, unittest.TestCase):

    def test_mpfirst_loads_catalog(self):
        files = ck.load_whitelisted_context("mpfirst")
        rels = [r for r, _ in files]
        self.assertIn("knowledge/product-catalog-wb.md", rels)
        blob = "\n".join(t for _, t in files)
        self.assertIn(_CATALOG_TOKEN, blob)

    def test_mpfirst_context_has_no_financial_sentinel(self):
        """R6-ядро: финкарта лежит в фикстуре рядом, но в контекст НЕ попадает."""
        # Финкарта реально существует на диске — иначе тест бессмыслен.
        fin = _FIXTURE_CONTEXT / "mpfirst-context" / "knowledge" / _FIN_FILE
        self.assertTrue(fin.is_file(), "фикстура финкарты должна существовать")
        self.assertIn(_FIN_SENTINEL, fin.read_text(encoding="utf-8"))
        # …но в whitelist-контексте её сентинеля нет.
        blob = "\n".join(t for _, t in ck.load_whitelisted_context("mpfirst"))
        self.assertNotIn(_FIN_SENTINEL, blob)
        self.assertNotIn(_FIN_FILE, [r for r, _ in ck.load_whitelisted_context("mpfirst")])

    def test_anzhee_no_catalog_graceful(self):
        """У anzhee каталога нет → пусто (graceful, не падаем)."""
        self.assertEqual(ck.load_whitelisted_context("anzhee"), [])

    def test_none_company_empty(self):
        self.assertEqual(ck.load_whitelisted_context(None), [])


# ===========================================================================
# Денилист-бэкстоп и path-traversal (порча whitelist не пробивает приватность)
# ===========================================================================
class TestWhitelistBackstops(_FixtureContextMixin, unittest.TestCase):

    def _patch_whitelist(self, value):
        self._orig_wl = ck.PROTOCOL_CONTEXT_WHITELIST
        ck.PROTOCOL_CONTEXT_WHITELIST = value
        self.addCleanup(lambda: setattr(ck, "PROTOCOL_CONTEXT_WHITELIST", self._orig_wl))

    def test_financial_in_whitelist_still_blocked_by_denylist(self):
        """Даже если финкарту ОШИБОЧНО внесли в whitelist — денилист её режет."""
        self._patch_whitelist((
            "knowledge/product-catalog-wb.md",
            "knowledge/accounting-tables-registry.md",  # ошибочно добавлена
        ))
        files = ck.load_whitelisted_context("mpfirst")
        rels = [r for r, _ in files]
        self.assertIn("knowledge/product-catalog-wb.md", rels)        # каталог прошёл
        self.assertNotIn("knowledge/accounting-tables-registry.md", rels)  # финкарта — нет
        self.assertNotIn(_FIN_SENTINEL, "\n".join(t for _, t in files))

    def test_path_traversal_blocked(self):
        """`..`-побег из репо отбрасывается (не читаем чужие файлы)."""
        self._patch_whitelist(("knowledge/../../../../../../etc/hosts",))
        self.assertEqual(ck.load_whitelisted_context("mpfirst"), [])

    def test_absolute_path_blocked(self):
        self._patch_whitelist(("/etc/hosts",))
        self.assertEqual(ck.load_whitelisted_context("mpfirst"), [])

    def test_size_cap_truncates_not_silent(self):
        """Потолок размера обрезает (не молча — логирует), но текст возвращает."""
        self._orig_cap = ck.PROTOCOL_CONTEXT_FILE_MAX_BYTES
        ck.PROTOCOL_CONTEXT_FILE_MAX_BYTES = 50  # искусственно крошечный
        self.addCleanup(lambda: setattr(ck, "PROTOCOL_CONTEXT_FILE_MAX_BYTES", self._orig_cap))
        files = ck.load_whitelisted_context("mpfirst")
        self.assertTrue(files)
        # Усечено по границе потолка (декод replace может дать чуть больше символов,
        # но точно не весь файл).
        self.assertLessEqual(len(files[0][1].encode("utf-8")), 60)


# ===========================================================================
# R6/R7 — catalog_prompt_block: каталог + правило догадки в контексте
# ===========================================================================
class TestCatalogPromptBlock(_FixtureContextMixin, unittest.TestCase):

    def test_mpfirst_block_has_catalog_and_rule(self):
        block = glossary.catalog_prompt_block("mpfirst")
        self.assertIn(_CATALOG_TOKEN, block)            # R6: товар в контексте
        self.assertIn("услышано", block)                # R7: правило догадки
        self.assertIn("ЛУЧШУЮ ДОГАДКУ", block)

    def test_mpfirst_block_no_financial(self):
        block = glossary.catalog_prompt_block("mpfirst")
        self.assertNotIn(_FIN_SENTINEL, block)

    def test_anzhee_block_empty(self):
        """Нет каталога → секции нет (поведение как до Ф2)."""
        self.assertEqual(glossary.catalog_prompt_block("anzhee"), "")

    def test_r7_rule_shows_heard_example(self):
        """R7 на примере токена: правило показывает формат «канон (услышано «…»)»."""
        self.assertIn("(услышано «Пульсар Ви»)", glossary.CATALOG_UNKNOWN_TOKEN_RULE)


# ===========================================================================
# R6 интеграция — «контекст, который видит бот» при сборке протокола
# ===========================================================================
class TestProtocolSystemPrompt(_FixtureContextMixin, unittest.TestCase):
    """Собираем ИМЕННО тот system-prompt, что уйдёт боту (`_build_protocol_system_prompt`),
    на фикстуре компании — без живого `claude` CLI."""

    _METHOD = "# Метод (заглушка для теста)\n"

    def test_mpfirst_prompt_has_catalog_no_financial(self):
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", self._METHOD)
        self.assertIn(_CATALOG_TOKEN, sp)        # R6: каталог ВХОДИТ
        self.assertNotIn(_FIN_SENTINEL, sp)      # R6: финкарта НЕ входит
        self.assertIn("услышано", sp)            # R7: правило в контексте
        self.assertIn(self._METHOD.strip(), sp)  # методичка на месте (не сломали сборку)

    def test_anzhee_prompt_has_no_catalog_section(self):
        sp = llm_postprocess._build_protocol_system_prompt("anzhee", self._METHOD)
        # У anzhee каталога нет — товаро-сентинеля mpfirst тут быть не должно.
        self.assertNotIn(_CATALOG_TOKEN, sp)
        self.assertNotIn(_FIN_SENTINEL, sp)


# ===========================================================================
# Graceful degradation — нет клона/корня → пусто, не падаем (всегда)
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

    def test_no_clone_empty_context(self):
        self.assertEqual(ck.load_whitelisted_context("mpfirst"), [])

    def test_no_clone_block_empty(self):
        self.assertEqual(glossary.catalog_prompt_block("mpfirst"), "")

    def test_no_clone_prompt_builds_without_catalog(self):
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", "# m\n")
        self.assertIn("# m", sp)  # сборка не падает, методичка на месте


if __name__ == "__main__":
    unittest.main(verbosity=2)
