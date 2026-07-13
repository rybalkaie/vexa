"""Ф1 плана `2026-07-12-watson-company-memory`: БЭКСТОП ПРИВАТНОСТИ.

Фаза-предохранитель: она идёт ПЕРЕД расширением чтения (Ф2), чтобы никакое
расширение allow-list'а — и никакая ошибка человека в манифесте — не могло слить
финансы / HR-конфиденшел / `restricted/` во ВНЕШНИЙ LLM (Anthropic).

Покрывает:
  • R1 — чувствительный файл, ОШИБОЧНО вписанный в allow-list, не попадает в
    собранный промпт; сборка при этом не падает; его текст не уходит в лог.
  • R2 — чувствительное ловится ПАТТЕРНОМ имени в любом репо/подпапке, а не только
    точным денилистом.
  • R8 (часть) — Опасная тройка: в structured-лог уходят только пути и счётчики,
    ТЕКСТ знания не логируется никогда (иначе утечка логов = утечка коммерческих
    данных). Доказывается двумя способами: поведенчески (перехват логов) и
    статически (AST-грep по исходнику).
  • РИСК1 — доказательство «от противного»: реальные финфайлы `baza-znaniy/`
    названы транслитом и паттернами НЕ ловятся → Ф2 обязана давать явный short-list,
    а НЕ glob папки. См. `TestRisk1TranslitNamesSlipPatterns`.

Фикстуры синтетические (вымышленные цифры/имена, никаких реальных данных компании).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_watson_privacy_backstop -v
"""
from __future__ import annotations

import ast
import contextlib
import logging
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
_CATALOG = "knowledge/product-catalog-wb.md"
_CATALOG_TOKEN = "Pulsar 63ML"  # разрешённое знание — обязано остаться в промпте

# Сентинели чувствительных фикстур: ни один не имеет права появиться ни в промпте,
# ни в логах.
_SENTINELS = {
    "restricted/people-confidential.md": "RESTRICTED_HR_SENTINEL_51515151",
    "knowledge/altronix-ekonomika.md": "EKONOMIKA_SENTINEL_62626262",
    "knowledge/finmodel-orgstruktura.md": "FINMODEL_SENTINEL_73737373",
    "knowledge/accounting-tables-registry.md": "FINSENTINEL_MUST_NOT_LEAK_42424242",
}
# Транслит-финфайлы `baza-znaniy/` (РИСК1): паттерн их НЕ ловит — их закрывает
# только short-list манифеста Ф2.
_TRANSLIT_FIN = {
    "company/baza-znaniy/924-uchet-dds.md": "TRANSLIT_FIN_SENTINEL_84848484",
    "company/baza-znaniy/zifriend-vzaimoraschety-itog.md": "TRANSLIT_FIN_SENTINEL_95959595",
}
_SAFE_KNOWLEDGE = "company/baza-znaniy/pozitsionirovanie.md"


@contextlib.contextmanager
def _capture_logs(*module_loggers: str):
    """Перехватить ВСЁ, что модуль пишет в логи (любого уровня), и отдать список
    ОТФОРМАТИРОВАННЫХ сообщений — именно они уезжают в structured-лог."""
    records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    sink = _Sink()
    sink.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    loggers = [logging.getLogger(name) for name in module_loggers]
    saved = [(lg, lg.level, lg.propagate) for lg in loggers]
    for lg in loggers:
        lg.addHandler(sink)
        lg.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        for lg, level, propagate in saved:
            lg.removeHandler(sink)
            lg.setLevel(level)
            lg.propagate = propagate


class _FixtureContextMixin:
    """MEETING_NOTARY_CONTEXT_DIR → фикстура; восстановление после теста."""

    def setUp(self):
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_FIXTURE_CONTEXT)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved

    def _patch_whitelist(self, value: tuple[str, ...]):
        orig = ck.PROTOCOL_CONTEXT_WHITELIST
        ck.PROTOCOL_CONTEXT_WHITELIST = value
        self.addCleanup(lambda: setattr(ck, "PROTOCOL_CONTEXT_WHITELIST", orig))

    def _patch_budget(self, value: int):
        orig = ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES
        ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES = value
        self.addCleanup(lambda: setattr(ck, "PROTOCOL_CONTEXT_TOTAL_MAX_BYTES", orig))


# ===========================================================================
# Бэкстоп как чистая функция — единая точка правды (её же зовёт манифест Ф2)
# ===========================================================================
class TestContextPathBlockReason(unittest.TestCase):

    def test_safe_paths_pass(self):
        # ⚠️ `knowledge/dry-rental.md` СТОЯЛ здесь как «безопасный» — и это было ошибкой
        # посылки, а не кода: план считал файл каталогом оборудования Anzhee, но его никто
        # не открывал. Ф2 открыла: внутри дилерские цены, маржа прокатчика и ссылка на
        # закрытый прайс. Файл ушёл в денилист (цикл из 5 ходов, Н2); наружу вместо него
        # идёт безопасная выжимка `knowledge/notary/dry-rental-brief.md` — она и стоит здесь.
        for rel in (
            _CATALOG,
            "company/about.md",
            _SAFE_KNOWLEDGE,
            "knowledge/notary/dry-rental-brief.md",
        ):
            self.assertIsNone(ck.context_path_block_reason(rel), rel)

    def test_denylist_by_basename(self):
        """Денилист ловит файл, даже если его ПЕРЕНЕСЛИ из restricted/ в knowledge/."""
        for rel in (
            "knowledge/accounting-tables-registry.md",
            "knowledge/payment-planning-tables-map.md",
            "knowledge/google-drive-map.md",
            "knowledge/altronix-ekonomika.md",
            "knowledge/finmodel-orgstruktura.md",
            "company/people-confidential.md",
            "company/marketplace-affiliation-safety.md",
        ):
            self.assertIsNotNone(ck.context_path_block_reason(rel), rel)

    def test_baza_znaniy_finfiles_blocked_by_name(self):
        """Смена курса владельца 2026-07-13 (раздельное хранение вместо распознавания денег):
        9 конфиденциальных финфайлов baza-znaniy МПервый с транслитными именами закрыты
        ПОИМЁННО — даже вписанные в манифест, режутся наглухо (паттерны их не ловят)."""
        for rel in (
            "company/baza-znaniy/793-kak-uchityvat-reinvestirovanie-protsentov-po-zaymam.md",
            "company/baza-znaniy/825-sistema-ucheta-na-google-tablitsakh.md",
            "company/baza-znaniy/837-planirovanie-oplat-zakazov-tovara.md",
            "company/baza-znaniy/839-kakuyu-tsenu-postavit-unit-ekonomika-prodazh.md",
            "company/baza-znaniy/867-nalichnye-kassy-sverka-ostatkov-po-kassam.md",
            "company/baza-znaniy/922-vedenie-ucheta-vneshnikh-reklamnykh-integratsiy.md",
            "company/baza-znaniy/924-uchet-dds.md",
            "company/baza-znaniy/925-predostavlenie-otcheta-po-sebestoimosti-postavki.md",
            "company/baza-znaniy/zifriend-vzaimoraschety-itog.md",
        ):
            self.assertIsNotNone(ck.context_path_block_reason(rel), rel)

    def test_open_data_with_numbers_still_passes(self):
        """Решение владельца: не всякая цифра секрет. Открытые данные (метрики, публичные
        цены, каталог с артикулами) проходят — их прятать не нужно."""
        for rel in (_CATALOG, "company/team.md", "company/values.md"):
            self.assertIsNone(ck.context_path_block_reason(rel), rel)

    def test_restricted_dir_anywhere_in_tree(self):
        """R1: всё под `restricted/` закрыто — на любой глубине дерева."""
        for rel in (
            "restricted/anything.md",
            "company/restricted/deep/notes.md",
            "RESTRICTED/upper.md",          # регистр не спасает
        ):
            self.assertIsNotNone(ck.context_path_block_reason(rel), rel)

    def test_restricted_as_last_segment_blocked(self):
        """Путь-ДИРЕКТОРИЯ `company/restricted` тоже закрыт.

        На рантайме такой путь отсекает `is_file()`, но фильтр манифеста Ф2 зовёт эту
        функцию как ЕДИНУЮ точку правды — без файловой проверки. Пропусти мы его, и
        запись-папка в манифесте открыла бы всю `restricted/`."""
        for rel in ("restricted", "company/restricted", "COMPANY/Restricted"):
            self.assertIsNotNone(ck.context_path_block_reason(rel), rel)

    def test_structural_escapes_blocked(self):
        for rel in ("/etc/hosts", "~/secrets.md", "knowledge/../../../etc/hosts", ""):
            self.assertIsNotNone(ck.context_path_block_reason(rel), rel)


# ===========================================================================
# R2 — чувствительное ловится ПАТТЕРНОМ имени, а не только точным списком
# ===========================================================================
class TestR2SensitivePatterns(unittest.TestCase):
    """R2 на синтетике имён: файл в ЛЮБОМ репо/подпапке с чувствительным именем."""

    _SENSITIVE_NAMES = (
        "company/altronix-ekonomika.md",             # *ekonomik*
        "docs/unit-ekonomiki-2026.md",               # падеж — тоже ловим
        "deep/nested/marketplace-economics.md",      # *economic*
        "knowledge/finmodel-2027.md",                # *finmodel*
        "company/salary-confidential.md",            # *confidential*
        "company/konfidencialno-hr.md",              # *konfiden*
        "finmodel/summary.md",                       # чувствительна ПАПКА, не файл
    )

    def test_patterns_block_by_name(self):
        for rel in self._SENSITIVE_NAMES:
            reason = ck.context_path_block_reason(rel)
            self.assertIsNotNone(reason, f"должен быть заблокирован: {rel}")

    def test_load_whitelisted_context_drops_them(self):
        """R2 дословно: `load_whitelisted_context` отбрасывает такой файл, даже если
        он РЕАЛЬНО существует и вписан в allow-list."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "mpfirst-context"
            for rel in self._SENSITIVE_NAMES:
                p = repo / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("SYNTHETIC_SENSITIVE_BODY_11223344\n", encoding="utf-8")
            safe = repo / "knowledge" / "ok.md"
            safe.parent.mkdir(parents=True, exist_ok=True)
            safe.write_text("безопасное знание\n", encoding="utf-8")

            saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = tmp
            orig_wl = ck.PROTOCOL_CONTEXT_WHITELIST
            ck.PROTOCOL_CONTEXT_WHITELIST = self._SENSITIVE_NAMES + ("knowledge/ok.md",)
            try:
                files = ck.load_whitelisted_context("mpfirst")
            finally:
                ck.PROTOCOL_CONTEXT_WHITELIST = orig_wl
                if saved is None:
                    os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
                else:
                    os.environ["MEETING_NOTARY_CONTEXT_DIR"] = saved

        rels = [r for r, _ in files]
        self.assertEqual(rels, ["knowledge/ok.md"], "прошло только безопасное")
        self.assertNotIn("SYNTHETIC_SENSITIVE_BODY_11223344", "\n".join(t for _, t in files))


# ===========================================================================
# R1 — чувствительное в allow-list не доходит до промпта, сборка не падает
# ===========================================================================
class TestR1SensitiveInAllowlistNeverReachesPrompt(_FixtureContextMixin, unittest.TestCase):
    """Дословный критерий R1: впиши `altronix-ekonomika.md` / файл под `restricted/` /
    `finmodel-orgstruktura.md` в allow-list → промпт их контента НЕ содержит, сборка
    не падает, текст в лог не ушёл."""

    _METHOD = "# Метод (заглушка для теста)\n"
    _POISONED_WHITELIST = (
        _CATALOG,                              # разрешённое — обязано остаться
        "knowledge/altronix-ekonomika.md",     # ошибка человека №1
        "restricted/people-confidential.md",   # ошибка человека №2
        "knowledge/finmodel-orgstruktura.md",  # ошибка человека №3
        "knowledge/accounting-tables-registry.md",  # ошибка человека №4 (финкарта)
    )

    def setUp(self):
        super().setUp()
        self._patch_whitelist(self._POISONED_WHITELIST)

    def test_fixtures_really_exist(self):
        """Тест бессмыслен, если чувствительных файлов нет на диске — проверяем."""
        for rel, sentinel in _SENTINELS.items():
            p = _FIXTURE_CONTEXT / "mpfirst-context" / rel
            self.assertTrue(p.is_file(), f"фикстура должна существовать: {rel}")
            self.assertIn(sentinel, p.read_text(encoding="utf-8"))

    def test_loader_drops_all_sensitive_keeps_catalog(self):
        files = ck.load_whitelisted_context("mpfirst")
        self.assertEqual([r for r, _ in files], [_CATALOG], "прошёл только каталог")
        blob = "\n".join(t for _, t in files)
        for sentinel in _SENTINELS.values():
            self.assertNotIn(sentinel, blob)

    def test_assembled_prompt_has_no_sensitive_and_does_not_crash(self):
        """Собираем ИМЕННО тот system-prompt, что уйдёт во внешний LLM."""
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", self._METHOD)
        for rel, sentinel in _SENTINELS.items():
            self.assertNotIn(sentinel, sp, f"утечка в промпт: {rel}")
        self.assertIn(_CATALOG_TOKEN, sp, "разрешённый каталог не потерян (регресс ISS-21)")
        self.assertIn(self._METHOD.strip(), sp, "сборка цела — методичка на месте")

    def test_sensitive_text_never_enters_logs(self):
        """R8 (часть): отбрасывая чувствительный файл, логгер пишет ПРИЧИНУ и ПУТЬ,
        но НЕ текст. Утечка логов не должна становиться утечкой данных."""
        with _capture_logs("notary.lib.context_knowledge", "notary.lib.glossary") as records:
            glossary.catalog_prompt_block("mpfirst")
        joined = "\n".join(records)
        self.assertTrue(records, "логи вообще писались (иначе тест ничего не проверяет)")
        for rel, sentinel in _SENTINELS.items():
            self.assertNotIn(sentinel, joined, f"текст {rel} утёк в лог")
        # …и даже РАЗРЕШЁННОЕ знание не логируется текстом (в лог — только счётчики).
        self.assertNotIn(_CATALOG_TOKEN, joined)


# ===========================================================================
# РИСК1 — почему Ф2 обязана давать short-list, а НЕ glob по baza-znaniy/
# ===========================================================================
class TestRisk1TranslitNamesSlipPatterns(_FixtureContextMixin, unittest.TestCase):
    """⚠️ ЭТО НЕ БАГ-ТЕСТ, А ДОКАЗАТЕЛЬСТВО ГРАНИЦЫ (РИСК1 плана).

    Реальные финфайлы МПервый в `company/baza-znaniy/` названы транслитом без корней
    `ekonomika`/`finmodel`/`confidential` — паттерны ПО ИМЕНИ их НЕ ловят. Это по-прежнему
    так, и потому первичная защита `baza-znaniy` — ЯВНЫЙ short-list в манифесте, а glob
    папки запрещён: имя файла не доказывает его содержимое.

    Что изменилось в Ф1 плана `knowledge-content-barrier`: раньше на этом защита и
    заканчивалась (файл, вписанный в манифест, уезжал во внешнюю модель), теперь такой файл
    ловит барьер по СОДЕРЖИМОМУ. Тесты ниже фиксируют оба факта: имя молчит — содержимое
    останавливает."""

    def test_translit_fin_names_now_blocked_by_denylist(self):
        """СМЕНА КУРСА владельца 2026-07-13 (раздельное хранение вместо распознавания денег):
        транслитные финфайлы `baza-znaniy`, которые ПАТТЕРНЫ по имени не ловят, теперь закрыты
        ЯВНЫМ denylist поимённо — режутся наглухо даже вписанные в манифест. Раньше этот тест
        фиксировал обратное (что они проходят) как обоснование контент-барьера; после смены
        курса главная защита известного конфиденциального — точный список имён, не распознавание."""
        for rel in _TRANSLIT_FIN:
            self.assertIsNotNone(
                ck.context_path_block_reason(rel),
                f"финфайл {rel} должен быть закрыт denylist поимённо (смена курса 2026-07-13)",
            )
        for rel in (
            "company/baza-znaniy/867-nalichnye-kassy-sverka-ostatkov-po-kassam.md",
            "company/baza-znaniy/925-predostavlenie-otcheta-po-sebestoimosti-postavki.md",
            "company/baza-znaniy/kak-uchityvat-kursy-valyut-pri-zakupkakh.md",
        ):
            self.assertIsNotNone(ck.context_path_block_reason(rel), rel)

    def test_glob_style_include_is_stopped_by_content_barrier(self):
        """Симуляция glob'а папки. РАНЬШЕ (до Ф1 плана `knowledge-content-barrier`) финфайлы
        прошли бы в промпт — тест это и фиксировал. ТЕПЕРЬ их ловит барьер по СОДЕРЖИМОМУ:
        имя по-прежнему ничего не гарантирует (тест выше), а внутрь мы наконец смотрим.

        Закон «baza-znaniy — только поимённо, НИКОГДА glob папки» при этом ОСТАЁТСЯ в силе:
        контент-барьер ловит ДЕНЬГИ, а не «всё чувствительное» (файл со стратегией сделки он
        пропустит). Он последняя линия, а не разрешение глобить."""
        self._patch_whitelist(tuple(_TRANSLIT_FIN) + (_SAFE_KNOWLEDGE,))
        files = ck.load_whitelisted_context("mpfirst")
        rels = [r for r, _ in files]
        blob = "\n".join(t for _, t in files)
        for rel, sentinel in _TRANSLIT_FIN.items():
            self.assertNotIn(rel, rels, f"финфайл {rel} прошёл барьер по содержимому")
            self.assertNotIn(sentinel, blob, f"текст {rel} уехал бы во внешнюю модель")
        self.assertIn(_SAFE_KNOWLEDGE, rels, "безопасный сосед по папке остался (не ловим всё подряд)")

    def test_explicit_shortlist_keeps_finance_out(self):
        """А вот КАК надо (контракт для Ф2): манифест перечисляет файлы поимённо —
        безопасный файл входит, финансовые соседи по папке не входят."""
        self._patch_whitelist((_SAFE_KNOWLEDGE,))
        files = ck.load_whitelisted_context("mpfirst")
        self.assertEqual([r for r, _ in files], [_SAFE_KNOWLEDGE])
        blob = "\n".join(t for _, t in files)
        for sentinel in _TRANSLIT_FIN.values():
            self.assertNotIn(sentinel, blob)


# ===========================================================================
# Бюджет суммарного размера — WARNING + обрезка по границе файлов, не молча
# ===========================================================================
class TestTotalBudget(unittest.TestCase):

    def test_fits_budget_untouched_and_quiet(self):
        files = [("a.md", "x" * 100), ("b.md", "y" * 100)]
        with _capture_logs("notary.lib.context_knowledge") as records:
            out = ck.apply_total_budget(files, budget=10_000)
        self.assertEqual(out, files)
        self.assertEqual([r for r in records if r.startswith("WARNING")], [])

    def test_overflow_truncates_at_file_boundary_with_warning(self):
        """Обрезка ПО ГРАНИЦЕ ФАЙЛОВ: `a` влез целиком, `b` и хвост отброшены целиком
        (без дырок и без обрезки посреди файла), и это не молча."""
        files = [("a.md", "x" * 20_000), ("b.md", "y" * 40_000), ("c.md", "z" * 10)]
        with _capture_logs("notary.lib.context_knowledge") as records:
            out = ck.apply_total_budget(files, budget=50_000)
        self.assertEqual([r for r, _ in out], ["a.md"])
        self.assertEqual(out[0][1], "x" * 20_000, "вошедший файл не порезан")
        warnings = [r for r in records if r.startswith("WARNING")]
        self.assertTrue(warnings, "превышение бюджета обязано быть громким")
        self.assertIn("b.md", "\n".join(warnings))
        self.assertIn("c.md", "\n".join(warnings))

    def test_single_oversized_file_is_trimmed_not_dropped(self):
        """ISS-21-защита: единственный файл больше бюджета режется по бюджету, но НЕ
        выбрасывается целиком — иначе канон артикулов пропал бы из промпта."""
        files = [("big.md", "x" * 60_000)]
        with _capture_logs("notary.lib.context_knowledge") as records:
            out = ck.apply_total_budget(files, budget=50_000)
        self.assertEqual(len(out), 1)
        self.assertLessEqual(len(out[0][1].encode("utf-8")), 50_000)
        self.assertGreater(len(out[0][1]), 0, "бриф не остался пустым")
        self.assertTrue([r for r in records if r.startswith("WARNING")])

    def test_budget_never_logs_file_text(self):
        """R8: в WARNING про бюджет — имена и байты, не содержимое."""
        secret = "BUDGET_TEXT_SENTINEL_31313131"
        files = [("a.md", "x" * 40_000), ("b.md", secret + "y" * 40_000)]
        with _capture_logs("notary.lib.context_knowledge") as records:
            ck.apply_total_budget(files, budget=50_000)
        self.assertNotIn(secret, "\n".join(records))

    def test_budget_applied_in_loader(self):
        """Бюджет реально включён в боевом пути `load_whitelisted_context` (а не
        живёт отдельной неподключённой функцией)."""
        saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_FIXTURE_CONTEXT)
        orig = ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES
        ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES = 200  # искусственно крошечный бюджет
        try:
            files = ck.load_whitelisted_context("mpfirst")
        finally:
            ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES = orig
            if saved is None:
                os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
            else:
                os.environ["MEETING_NOTARY_CONTEXT_DIR"] = saved
        self.assertTrue(files, "каталог не пропал целиком (ISS-21)")
        self.assertLessEqual(len(files[0][1].encode("utf-8")), 200)

    def test_default_budget_in_plan_range(self):
        """Бюджет по умолчанию — в согласованном планом коридоре 40–60 КБ."""
        self.assertGreaterEqual(ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES, 40 * 1024)
        self.assertLessEqual(ck.PROTOCOL_CONTEXT_TOTAL_MAX_BYTES, 60 * 1024)

    def test_non_first_oversized_file_is_dropped_WHOLE(self):
        """⚠️ ГРАНИЦА ЗАЩИТЫ, а не баг — читай перед правкой манифеста в Ф2.

        Спасение «oversized файл режется, а не выбрасывается» действует ТОЛЬКО для
        ПЕРВОГО файла (там иначе бриф стал бы пустым). Файл, стоящий НЕ первым, при
        исчерпанном бюджете выбрасывается ЦЕЛИКОМ. Отсюда закон Ф2: каталог артикулов
        обязан стоять в манифесте ПЕРВЫМ — иначе он молча (нет, громко: WARNING)
        вылетит из промпта, и бот вернётся к фонетике ASR (регресс ISS-21)."""
        files = [("small.md", "y" * 10), ("catalog.md", "x" * 200)]
        out = ck.apply_total_budget(files, budget=50)
        self.assertEqual([r for r, _ in out], ["small.md"], "каталог выброшен ЦЕЛИКОМ, не урезан")

    def test_catalog_is_first_in_whitelist(self):
        """ISS-21-инвариант, вытекающий из теста выше: раз порядок = приоритет, а
        не-первый oversized файл выбрасывается целиком — каталог артикулов обязан быть
        ПЕРВЫМ в allow-list/манифесте. Ф2 добавляет файлы ПОСЛЕ него, не перед."""
        self.assertTrue(ck.PROTOCOL_CONTEXT_WHITELIST, "allow-list не пуст")
        self.assertEqual(
            ck.PROTOCOL_CONTEXT_WHITELIST[0], _CATALOG,
            "каталог артикулов сдвинут с первого места — при переполнении бюджета "
            "он вылетит целиком (ISS-21). Ставь новые файлы ПОСЛЕ него.",
        )


# ===========================================================================
# Симлинк-обход и контрактный self-check allow-list
# ===========================================================================
class TestSymlinkAndAssertSafe(_FixtureContextMixin, unittest.TestCase):

    def test_symlink_into_restricted_is_blocked(self):
        """Чистое имя в allow-list + симлинк на `restricted/` → всё равно не читаем."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "mpfirst-context"
            (repo / "restricted").mkdir(parents=True)
            (repo / "knowledge").mkdir(parents=True)
            secret = repo / "restricted" / "salaries.md"
            secret.write_text("SYMLINK_SECRET_SENTINEL_77779999\n", encoding="utf-8")
            link = repo / "knowledge" / "ok-catalog.md"
            link.symlink_to(Path("..") / "restricted" / "salaries.md")

            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = tmp
            self._patch_whitelist(("knowledge/ok-catalog.md",))
            with _capture_logs("notary.lib.context_knowledge") as records:
                files = ck.load_whitelisted_context("mpfirst")

        self.assertEqual(files, [], "симлинк в restricted/ не пробивает бэкстоп")
        self.assertNotIn("SYMLINK_SECRET_SENTINEL_77779999", "\n".join(records))

    def test_hardlink_into_restricted_is_blocked(self):
        """ЖЁСТКАЯ ссылка — второй обход, симметричный симлинку.

        У симлинка есть цель, и `resolve()` её раскрывает. У hardlink'а цели нет: это
        второе ИМЯ того же инода, `resolve()` возвращает сам путь, и проверки «по пути»
        видят чистое `knowledge/ok-catalog.md`. Без отдельного барьера
        `ln restricted/salaries.md knowledge/ok-catalog.md` утащил бы зарплаты в промпт."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "mpfirst-context"
            (repo / "restricted").mkdir(parents=True)
            (repo / "knowledge").mkdir(parents=True)
            secret = repo / "restricted" / "salaries.md"
            secret.write_text("HARDLINK_SECRET_SENTINEL_31337000\n", encoding="utf-8")
            os.link(secret, repo / "knowledge" / "ok-catalog.md")  # НЕ symlink

            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = tmp
            self._patch_whitelist(("knowledge/ok-catalog.md",))
            with _capture_logs("notary.lib.context_knowledge") as records:
                files = ck.load_whitelisted_context("mpfirst")

        self.assertEqual(files, [], "hardlink в restricted/ не пробивает бэкстоп")
        self.assertNotIn("HARDLINK_SECRET_SENTINEL_31337000", "\n".join(records))

    def test_normal_file_is_not_mistaken_for_hardlink(self):
        """Обратная сторона: обычный файл (nlink=1) читается как ни в чём не бывало —
        fail-closed не должен стать fail-always. Реальные `*-context` приезжают через
        git (`context_pull.sh`), где nlink всегда 1."""
        files = ck.load_whitelisted_context("mpfirst")
        self.assertEqual([r for r, _ in files], [_CATALOG], "боевой каталог по-прежнему читается")

    def test_assert_whitelist_safe_passes_on_real_whitelist(self):
        ck.assert_whitelist_safe()  # боевой allow-list чист — не бросает

    def test_assert_whitelist_safe_raises_on_sensitive(self):
        for bad in (
            "knowledge/altronix-ekonomika.md",
            "restricted/people-confidential.md",
            "knowledge/finmodel-orgstruktura.md",
            "/etc/hosts",
        ):
            self._patch_whitelist((_CATALOG, bad))
            with self.assertRaises(AssertionError, msg=bad):
                ck.assert_whitelist_safe()


# ===========================================================================
# Анти-инъекция: знание компании подаётся как ДАННЫЕ, а не как команды
# ===========================================================================
class TestKnowledgeIsFramedAsData(_FixtureContextMixin, unittest.TestCase):
    """Конвенция плана: «знание в промпте обёрнуто как ДАННЫЕ (анти-инъекция)».

    Знание — недоверенный вход: файлы правит вся команда, текст приезжает копипастой
    из Platrum/Google Drive, а попадает оно в SYSTEM-промпт (самое привилегированное
    место). В проекте этот приём уже применён к правкам участников, свидетельствам
    висяков и пожеланиям формата — знание было единственным входом без рамки."""

    _METHOD = "# Метод (заглушка)\n"

    def test_catalog_block_is_wrapped_in_data_frame(self):
        block = glossary.catalog_prompt_block("mpfirst")
        self.assertIn(glossary.KNOWLEDGE_DATA_FRAME_HEADER, block, "рамка-ДАННЫЕ открыта")
        self.assertIn(glossary.KNOWLEDGE_DATA_FRAME_END, block, "рамка-ДАННЫЕ закрыта")

    def test_knowledge_text_sits_INSIDE_the_frame(self):
        """Текст знания — между открытием и закрытием рамки, а не рядом с ней."""
        block = glossary.catalog_prompt_block("mpfirst")
        start = block.index(glossary.KNOWLEDGE_DATA_FRAME_HEADER)
        end = block.index(glossary.KNOWLEDGE_DATA_FRAME_END)
        catalog_at = block.index(_CATALOG_TOKEN)
        self.assertTrue(start < catalog_at < end, "знание внутри рамки-ДАННЫХ")

    def test_our_own_rule_stays_OUTSIDE_the_frame(self):
        """Правило R7 — наша инструкция модели. Внутри блока «это не команды» она
        обесценилась бы сама собой, поэтому обязана быть ПОСЛЕ закрытия рамки."""
        block = glossary.catalog_prompt_block("mpfirst")
        end = block.index(glossary.KNOWLEDGE_DATA_FRAME_END)
        rule_at = block.index("ЛУЧШУЮ ДОГАДКУ")
        self.assertGreater(rule_at, end, "правило R7 вне блока данных")

    def test_frame_reaches_the_real_system_prompt(self):
        """Рамка доезжает до промпта, который реально уходит во внешний LLM."""
        sp = llm_postprocess._build_protocol_system_prompt("mpfirst", self._METHOD)
        self.assertIn(glossary.KNOWLEDGE_DATA_FRAME_HEADER, sp)
        self.assertIn(_CATALOG_TOKEN, sp, "каталог на месте (ISS-21 не сломан)")


# ===========================================================================
# R8 (часть) — статический грep: текст знания в logger не передаётся
# ===========================================================================
class TestNoKnowledgeTextInLoggerCalls(unittest.TestCase):
    """Машинный грep по AST (то самое «grep по коду» из критерия R8): ни один вызов
    `logger.*` в модулях, которые держат текст знания, не принимает переменную с
    содержимым файла. Логируем пути и счётчики — не данные."""

    _MODULES = (
        _NOTARY / "lib" / "context_knowledge.py",
        _NOTARY / "lib" / "glossary.py",
        # ПОСЛЕДНИЙ РУБЕЖ: именно этот модуль отправляет собранный промпт (со знанием
        # внутри) во внешний LLM. Читать знание бесполезно запрещать, если его выведет
        # в лог тот, кто его отсылает. Сегодня там только счётчики (`prompt_len=%d`) —
        # тест фиксирует это как КОНТРАКТ, а не как везение.
        _NOTARY / "lib" / "claude_cli.py",
    )
    # Имена переменных, в которых в этих модулях лежит СОДЕРЖИМОЕ файла знания
    # (или собранный промпт, внутри которого это содержимое).
    _FORBIDDEN = {
        "text", "data", "head", "blob", "content", "body",
        "full_prompt", "prompt", "system", "system_prompt", "user_prompt",
    }
    # …но обёрнутые в счётчик/метаданные они безвредны и разрешены: `len(data)` —
    # это байты, `type(data).__name__` — имя типа. Утечкой было бы САМО значение.
    _COUNTER_WRAPPERS = {"len", "sum", "bool", "type", "id"}

    def _leaking_names(self, node: ast.AST) -> list[str]:
        """Имена из `_FORBIDDEN`, чьё ЗНАЧЕНИЕ реально дойдёт до строки лога
        (внутрь счётчиков-обёрток не спускаемся)."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in self._COUNTER_WRAPPERS:
            return []
        found: list[str] = []
        if isinstance(node, ast.Name) and node.id in self._FORBIDDEN:
            found.append(node.id)
        for child in ast.iter_child_nodes(node):
            found.extend(self._leaking_names(child))
        return found

    def test_logger_calls_never_take_file_text(self):
        offenders: list[str] = []
        for module in self._MODULES:
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "logger"):
                    continue
                for arg in node.args + [kw.value for kw in node.keywords]:
                    for name in self._leaking_names(arg):
                        offenders.append(
                            f"{module.name}:{node.lineno} logger.{func.attr}(… {name} …)"
                        )
        self.assertEqual(
            offenders, [],
            "текст знания уходит в logger — Опасная тройка нарушена:\n" + "\n".join(offenders),
        )

    def test_ast_check_actually_catches_a_leak(self):
        """Мета-тест: проверка выше не бутафория — на синтетической утечке она ловит."""
        leak = ast.parse('logger.warning("знание: %s", text)').body[0].value
        self.assertEqual(self._leaking_names(leak.args[1]), ["text"])
        safe = ast.parse('logger.warning("байт: %d", len(text))').body[0].value
        self.assertEqual(self._leaking_names(safe.args[1]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
