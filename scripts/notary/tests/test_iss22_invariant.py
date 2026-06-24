"""Тесты Ф2 плана `2026-06-24-selfreview-on-regeneration.md` (ISS-22 пункт б):
инвариант «задачи и решения не теряются между версиями протокола».

Корень бага ISS-22: полная регенерация протокола — лоссовый пересбор (что-то
ловит, что-то роняет). V2 одной встречи выронил живую задачу, которую держал V1.
Лечение: при пересборке критик получает на вход задачи/решения ПРОШЛОЙ версии и
обязан не дать ни одной пропасть без явной причины (инвариант «финал ⊇ прошлой
версии»). Восстановление — молчаливое (без служебных пометок в тексте).

Покрывает REQ R-b1..R-b5 (реестр плана, стр.57–61):
  - R-b1  промпт критика несёт секцию-инвариант + список прошлых пунктов.
  - R-b2  потерянный сам пункт восстанавливается МОЛЧА (без пометки в тексте).
  - R-b3  регресс: прошлый протокол с задачей X → черновик без X → на диске X.
  - R-b4  мягкая деградация: нет прошлой версии / битая / пустая → обычный
          self-review, без падения, файл не теряется.
  - R-b5  (A6) пункт, закрытый транскриптом (carryover «— закрыта»), инвариант
          назад НЕ возвращает (отфильтрован детерминированно ДО промпта).
  + поведенческий тест порядка «захват прошлой версии → реген → review» на РЕАЛЬНОМ
    CLI (РИСК5: source-scan этого не ловит) — захват ОБЯЗАН быть ДО регенерации.
  + форензик-счётчик «проверено/под угрозой/восстановлено» — числа, без текста.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss22_invariant -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

# llm_postprocess тянет тяжёлые third-party — подкладываем стабы, если их нет
# (приём из тестов Ф4/Ф6/Ф7).
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib import llm_postprocess as lp  # noqa: E402
from lib import series_memory as sm  # noqa: E402

_DISCLAIMER = "> _Авторство реплик распознано автоматически, возможны неточности._"

# Прошлая версия (V1): держит живую задачу X «посмотреть сбой бота» (ISS-22 кейс).
PRIOR = (
    "#протоколвстречи 24.06.2026\n\n"
    + _DISCLAIMER + "\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "**Илья**\n"
    "▪️ Посмотреть сбой бота-протокола после галлюцинации.\n\n"
    "**Михаил**\n"
    "▪️ Подготовить отчёт по складу.\n\n"
    "## 2) Решения\n\n"
    "🔸 Бюджет урезан на 15 процентов.\n"
)

# Черновик пересборки (V2): задачу X ВЫРОНИЛ (лоссовый сэмпл), решение держит.
DRAFT_NO_X = (
    "#протоколвстречи 24.06.2026\n\n"
    + _DISCLAIMER + "\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "**Михаил**\n"
    "▪️ Подготовить отчёт по складу.\n\n"
    "## 2) Решения\n\n"
    "🔸 Бюджет урезан на 15 процентов.\n"
)

# Что вернул бы критик, подчинившись инварианту: X восстановлен МОЛЧА (без пометки).
IMPROVED_WITH_X = (
    "#протоколвстречи 24.06.2026\n\n"
    + _DISCLAIMER + "\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "**Илья**\n"
    "▪️ Посмотреть сбой бота-протокола после галлюцинации.\n\n"
    "**Михаил**\n"
    "▪️ Подготовить отчёт по складу.\n\n"
    "## 2) Решения\n\n"
    "🔸 Бюджет урезан на 15 процентов.\n"
)

TRANSCRIPT = (
    "#транскрипт 2026-06-24\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "**[00:00] Илья:** Бот вчера сбойнул, галлюцинация — я посмотрю в чём дело.\n\n"
    "**[00:10] Михаил:** Отчёт по складу подготовлю.\n\n"
    "**[00:20] Илья:** И бюджет урезаем на пятнадцать процентов.\n"
)

# Опорная фраза задачи X (переживает чистку маркеров/эмфазы в extract_open_tasks).
X_PHRASE = "Посмотреть сбой бота-протокола"


def _envelope(protocol: str, *, findings=None, edits=None) -> str:
    diag = {"findings": findings or [], "edits": edits or {}}
    return (
        lp._REWRITE_DIAG_MARKER + "\n"
        + json.dumps(diag, ensure_ascii=False) + "\n"
        + lp._REWRITE_PROTOCOL_MARKER + "\n"
        + protocol
    )


# ==========================================================================
# A3/РИСК3: извлечение задач+решений переиспользует series_memory (НЕ новый парсер)
# ==========================================================================
class TestExtraction(unittest.TestCase):

    def test_decisions_extracted(self):
        d = sm.extract_decisions(PRIOR)
        self.assertTrue(any("Бюджет урезан на 15" in x for x in d))

    def test_decisions_heading_variants(self):
        """_classify_heading (через _DECISION_HEADING_KEYS) ловит варианты заголовка."""
        for heading in ("## Решения", "## **Решения и договорённости**",
                        "## Что внедряем", "## 3) Договорились"):
            txt = heading + "\n\n🔸 Тестовое решение по складу.\n"
            self.assertTrue(
                any("Тестовое решение" in x for x in sm.extract_decisions(txt)),
                f"заголовок не распознан: {heading}",
            )

    def test_items_from_versions_tasks_and_decisions(self):
        items = sm.extract_items_from_versions([PRIOR])
        self.assertTrue(any(X_PHRASE in x for x in items))          # задача X
        self.assertTrue(any("отчёт по складу" in x for x in items))  # вторая задача
        self.assertTrue(any("Бюджет урезан" in x for x in items))    # решение

    def test_items_dedup_across_versions(self):
        """Идемпотентность: один пункт из ДВУХ источников не задваивается."""
        one = sm.extract_items_from_versions([PRIOR])
        two = sm.extract_items_from_versions([PRIOR, PRIOR])
        self.assertEqual(one, two)

    def test_closed_carryover_dropped_R_b5(self):
        """🔴 R-b5/A6: carryover-задача со статусом «— закрыта» НЕ попадает в пункты
        инварианта (закрыта транскриптом — не воскрешаем). Детерминированно, без LLM."""
        prior_with_closed = (
            "#протоколвстречи 24.06.2026\n\n"
            "## 🔻 С прошлых встреч\n\n"
            "- Татьяна вышлет расчёт — висит\n"
            "- Договор подписать — закрыта ✅\n"
        )
        items = sm.extract_items_from_versions([prior_with_closed])
        self.assertTrue(any("Татьяна вышлет расчёт" in x for x in items))  # висит → несём
        self.assertFalse(any("Договор подписать" in x for x in items))     # закрыта → НЕ несём

    def test_empty_and_broken_degrade_R_b4(self):
        self.assertEqual(sm.extract_items_from_versions([]), [])
        self.assertEqual(sm.extract_items_from_versions(["", None]), [])
        self.assertEqual(sm.extract_items_from_versions(["сырой текст без заголовков"]), [])


# ==========================================================================
# R-b1: секция-инвариант в rewrite-промпте (НЕ в флаг-only) + список в user-prompt
# ==========================================================================
class TestInvariantPrompt(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1"},
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_invariant_in_rewrite_prompt_when_prior_items(self):
        sp = lp._build_rewrite_system_prompt(("values", "roles"), has_prior_items=True)
        self.assertIn("ИНВАРИАНТ", sp)
        self.assertIn("НИЧЕГО НЕ ТЕРЯЕМ", sp)
        self.assertIn("Восстанавливай МОЛЧА", sp)        # R-b2 указание молчать

    def test_invariant_absent_without_prior_items(self):
        """Без прошлых пунктов rewrite-промпт прежний (R-b4: обычный self-review)."""
        sp = lp._build_rewrite_system_prompt(("values", "roles"), has_prior_items=False)
        self.assertNotIn("ИНВАРИАНТ", sp)

    def test_invariant_NOT_in_flag_only_prompt_RISK1(self):
        """🔴 РИСК1: флаг-only промпт (_build_review_system_prompt, rewrite=False) НЕ
        несёт инвариант — регенерация туда не ходит, иначе ISS-22 повторился бы молча."""
        sp = lp._build_review_system_prompt(("values", "roles", "memory"))
        self.assertNotIn("ИНВАРИАНТ «НИЧЕГО НЕ ТЕРЯЕМ»", sp)

    def test_prior_items_listed_in_user_prompt(self):
        """R-b1: список прошлых пунктов уходит критику в user-prompt; system — инвариант."""
        captured = {}

        def fake(user, system="", **kw):
            captured["user"] = user
            captured["system"] = system
            return _envelope(IMPROVED_WITH_X)

        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.review_and_rewrite_protocol(
                DRAFT_NO_X, TRANSCRIPT, checks=("values", "roles"), meeting_sid="sid",
                prior_items=["Илья: " + X_PHRASE + " после галлюцинации.",
                             "Бюджет урезан на 15 процентов."],
            )
        self.assertIn(X_PHRASE, captured["user"])         # пункт в user-prompt
        self.assertIn("ПУНКТЫ ПРОШЛОЙ ВЕРСИИ", captured["user"])
        self.assertIn("ИНВАРИАНТ", captured["system"])     # правило в system-prompt

    def test_no_prior_items_no_invariant_in_call(self):
        """R-b4: prior_items пуст → критик зовётся без инварианта (поведение Ф7)."""
        captured = {}

        def fake(user, system="", **kw):
            captured["system"] = system
            return _envelope(IMPROVED_WITH_X)

        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.review_and_rewrite_protocol(
                DRAFT_NO_X, TRANSCRIPT, checks=("values",), meeting_sid="sid",
                prior_items=None,
            )
        self.assertNotIn("ИНВАРИАНТ", captured["system"])


# ==========================================================================
# R-b3 / R-b2 / форензика: сквозь review_and_flag_protocol_file(prior_sources=…)
# ==========================================================================
class TestInvariantWiring(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1"},
        )
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-06-24.md"
        self.protocol = d / "2026-06-24-protokol.md"
        self.transcript.write_text(TRANSCRIPT, encoding="utf-8")
        self.protocol.write_text(DRAFT_NO_X, encoding="utf-8")  # на диске — черновик без X

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def test_lost_task_restored_on_disk_silently_R_b3_R_b2(self):
        """🔴 R-b3+R-b2: прошлый протокол с X + черновик без X → на диске финал содержит
        X (восстановлен) И без служебной пометки о восстановлении."""
        captured = {}

        def fake(user, system="", **kw):
            captured["user"] = user
            return _envelope(IMPROVED_WITH_X)

        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.review_and_flag_protocol_file(
                self.protocol, self.transcript,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid", rewrite=True, prior_sources=[PRIOR],
            )
        disk = self.protocol.read_text(encoding="utf-8")
        # R-b1: пункт реально дошёл до критика (доказывает захват+извлечение+проброс).
        self.assertIn(X_PHRASE, captured["user"])
        # R-b3: восстановлен на диске.
        self.assertIn(X_PHRASE, disk)
        # R-b2: молча — никаких служебных пометок о восстановлении в тексте.
        for marker in ("восстановлен", "restored", "потеряно", "добавлено критиком",
                       "⚠️ восстанов"):
            self.assertNotIn(marker.lower(), disk.lower())

    def test_forensic_counter_logged_no_reply_text(self):
        """🔴 Опасная тройка: лог несёт ЧИСЛА «проверено/под угрозой/восстановлено»,
        но НЕ текст пунктов/реплик."""
        secret = "урезаем на пятнадцать процентов"  # фраза из транскрипта
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(IMPROVED_WITH_X)):
            with self.assertLogs("lib.llm_postprocess", level="INFO") as cm:
                lp.review_and_flag_protocol_file(
                    self.protocol, self.transcript,
                    checks=("values", "roles", "memory", "diarization"),
                    meeting_sid="sid", rewrite=True, prior_sources=[PRIOR],
                )
        joined = "\n".join(cm.output)
        self.assertIn("prior-items=", joined)             # счётчик проверенных
        self.assertIn("at-risk=", joined)                 # под угрозой потери
        self.assertIn("recovered=", joined)               # восстановлено
        self.assertNotIn(secret, joined)                  # без текста реплик
        self.assertNotIn(X_PHRASE, joined)                # без текста пунктов

    def test_at_risk_counts_lost_task(self):
        """Форензика: задача X есть в прошлой версии, нет в черновике → at-risk≥1,
        а после восстановления критиком → recovered≥1."""
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(IMPROVED_WITH_X)):
            with self.assertLogs("lib.llm_postprocess", level="INFO") as cm:
                lp.review_and_flag_protocol_file(
                    self.protocol, self.transcript,
                    checks=("values",), meeting_sid="sid", rewrite=True,
                    prior_sources=[PRIOR],
                )
        joined = "\n".join(cm.output)
        self.assertNotIn("at-risk=0", joined)   # X под угрозой → ≥1
        self.assertNotIn("recovered=0", joined)  # критик вернул → ≥1

    def test_idempotent_regen_no_at_risk(self):
        """Идемпотентность: если на диске уже полная версия (= прошлой), под угрозой 0."""
        self.protocol.write_text(IMPROVED_WITH_X, encoding="utf-8")  # черновик уже полный
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(IMPROVED_WITH_X)):
            with self.assertLogs("lib.llm_postprocess", level="INFO") as cm:
                lp.review_and_flag_protocol_file(
                    self.protocol, self.transcript,
                    checks=("values",), meeting_sid="sid", rewrite=True,
                    prior_sources=[PRIOR],
                )
        self.assertIn("at-risk=0", "\n".join(cm.output))


# ==========================================================================
# R-b4: мягкая деградация (нет прошлой версии / битая / пустая)
# ==========================================================================
class TestDegradation(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1"},
        )
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-06-24.md"
        self.protocol = d / "2026-06-24-protokol.md"
        self.transcript.write_text(TRANSCRIPT, encoding="utf-8")
        self.protocol.write_text(DRAFT_NO_X, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def _run_capture_system(self, *, prior_sources):
        captured = {}

        def fake(user, system="", **kw):
            captured["system"] = system
            return _envelope(IMPROVED_WITH_X)

        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            n = lp.review_and_flag_protocol_file(
                self.protocol, self.transcript,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid", rewrite=True, prior_sources=prior_sources,
            )
        return n, captured.get("system", "")

    def test_no_prior_version_normal_review(self):
        """🔴 R-b4: prior_sources=None → обычный self-review (без инварианта), работает."""
        n, system = self._run_capture_system(prior_sources=None)
        self.assertNotIn("ИНВАРИАНТ", system)
        self.assertIn(X_PHRASE, self.protocol.read_text(encoding="utf-8"))  # рерайт прошёл
        self.assertGreater(n, 0)

    def test_broken_prior_no_crash_file_kept(self):
        """🔴 R-b4: битая прошлая версия (нет секций) → пунктов 0 → инвариант не
        включается, второй проход идёт, файл не теряется."""
        n, system = self._run_capture_system(prior_sources=["\x00\x01 не протокол вовсе"])
        self.assertNotIn("ИНВАРИАНТ", system)             # пунктов нет → без инварианта
        self.assertTrue(self.protocol.is_file())
        self.assertIn(X_PHRASE, self.protocol.read_text(encoding="utf-8"))

    def test_empty_prior_list(self):
        n, system = self._run_capture_system(prior_sources=[])
        self.assertNotIn("ИНВАРИАНТ", system)
        self.assertTrue(self.protocol.is_file())


# ==========================================================================
# РИСК5: ПОВЕДЕНЧЕСКИЙ порядок «захват прошлой версии → реген → review» на CLI
# ==========================================================================
class TestBehavioralOrderCLI(unittest.TestCase):
    """🔴 РИСК5 (source-scan этого НЕ ловит): захват прошлой версии ОБЯЗАН произойти
    ДО регенерации (atomic-перезапись затирает файл). Гоняем РЕАЛЬНЫЙ CLI main():
    прошлый протокол с X → регенерация даёт черновик без X (перетирает файл) →
    review получает X в промпте (доказывает захват ДО регена) → финал на диске с X.
    Если бы захват был ПОСЛЕ регена — X в промпт бы не попал, тест бы упал."""

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1",
             "NOTARY_REGEN_NO_REVIEW": "0"},
        )
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.series, self.date = "anzhee-directorate", "2026-06-24"
        sdir = root / self.series
        sdir.mkdir(parents=True)
        self.root = root
        (sdir / f"{self.date}.md").write_text(TRANSCRIPT, encoding="utf-8")
        # Прошлая версия протокола на диске — с задачей X.
        (sdir / f"{self.date}-protokol.md").write_text(PRIOR, encoding="utf-8")
        self.protocol_path = sdir / f"{self.date}-protokol.md"
        # Импортируем CLI с дефисом в имени через importlib.
        spec = importlib.util.spec_from_file_location(
            "regen_cli_under_test", _NOTARY / "tools" / "regenerate-protocol.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def test_capture_before_regen_then_restore(self):
        captured = {}

        def fake_regen(*, transcript_path, protocol_path, meeting_meta, meeting_sid, **kw):
            # Симулируем лоссовую регенерацию: перезаписываем файл черновиком БЕЗ X.
            protocol_path.write_text(DRAFT_NO_X, encoding="utf-8")
            return protocol_path

        def fake_claude(user, system="", **kw):
            captured["user"] = user
            captured["system"] = system
            return _envelope(IMPROVED_WITH_X)

        # Мокаем ТОЛЬКО генерацию (в CLI-модуле) и LLM-вызов (в lp). Реальный
        # review_and_flag_protocol_file (он же в CLI) выполняет захват→проброс.
        with mock.patch.object(self.cli, "regenerate_protocol_for_meeting",
                               side_effect=fake_regen), \
                mock.patch.object(lp, "call_claude_print", side_effect=fake_claude):
            rc = self.cli.main([self.series, self.date, "--root", str(self.root)])

        self.assertEqual(rc, 0)
        # Прошлый X дошёл до критика → захват случился ДО регена (иначе была бы версия без X).
        self.assertIn(X_PHRASE, captured.get("user", ""))
        self.assertIn("ИНВАРИАНТ", captured.get("system", ""))
        # И финал на диске содержит X (восстановлен).
        self.assertIn(X_PHRASE, self.protocol_path.read_text(encoding="utf-8"))


# ==========================================================================
# Source-scan: захват прошлой версии ДО регенерации на ВСЕХ 4 call-site
# (дополняет поведенческий CLI-тест — сторожит остальные 3 пути от перестановки)
# ==========================================================================
class TestPriorCaptureBeforeRegen(unittest.TestCase):

    def test_capture_precedes_regen_all_call_sites(self):
        """На каждом боевом пути снимок прошлой версии (read_text в переменную)
        ДОЛЖЕН стоять ДО `regenerate_protocol_for_meeting(` — иначе сравнение
        черновика с самим собой (РИСК2/A2). Поведенческий тест гоняет CLI; этот
        сторожит остальные 3 пути от случайной перестановки строк."""
        cases = [
            ("meetings_listener.py", "prior_protocol_text = protocol_path.read_text"),
            ("tools/regenerate-protocol.py", "prior_protocol_text = protocol_path.read_text"),
            ("finalize-meeting.py", "prior_protocol_text = protocol_path.read_text"),
            ("lib/clarify_worker.py", "old_protocol_text = protocol_path.read_text"),
        ]
        for rel, capture in cases:
            src = (_NOTARY / rel).read_text(encoding="utf-8")
            ci = src.find(capture)
            ri = src.find("regenerate_protocol_for_meeting(")
            self.assertNotEqual(ci, -1, f"{rel}: захват прошлой версии не найден")
            self.assertNotEqual(ri, -1, f"{rel}: вызов регенерации не найден")
            self.assertLess(
                ci, ri, f"{rel}: захват прошлой версии должен быть ДО регенерации (РИСК2)")

    def test_prior_sources_passed_on_all_call_sites(self):
        """Все 4 call-site пробрасывают `prior_sources=` в review_and_flag_protocol_file
        (иначе инвариант не получит прошлую версию — ISS-22 повторится молча)."""
        for rel in ("meetings_listener.py", "tools/regenerate-protocol.py",
                    "finalize-meeting.py", "lib/clarify_worker.py"):
            src = (_NOTARY / rel).read_text(encoding="utf-8")
            i = src.find("review_and_flag_protocol_file(")
            self.assertNotEqual(i, -1, f"{rel}: вызов review не найден")
            # Окно широкое: на finalize call длинный (extra_findings + комментарии),
            # prior_sources= идёт последним аргументом.
            self.assertIn("prior_sources=", src[i:i + 1100],
                          f"{rel}: review_and_flag_protocol_file без prior_sources=")


if __name__ == "__main__":
    unittest.main(verbosity=2)
