"""Тесты Ф3 плана `2026-06-24-selfreview-on-regeneration.md` (ISS-22 пункт в,
супер-идея владельца 2026-06-24): best-of-2 на ПЕРВОЙ публикации.

Наблюдение «второй прогон встречи полнее первого» → рычаг: на finalize-пути
протокол собирается из ДВУХ независимых черновиков; склейщик-критик (тот же движок
«не-лоссовая лучшая склейка» из Ф2) отдаёт лучшее из обоих, ничего не теряя.

Покрывает REQ (реестр плана, стр.62–64):
  - R-c1  на первой публикации генерится второй независимый черновик; пункт,
          пойманный ТОЛЬКО черновиком B, попадает в финал (через инвариант Ф2).
  - R-c2  деградация: сбой/таймаут второго прогона ИЛИ склейки → одинарная версия
          (черновик A + критик), встреча не теряется, finalize не падает.
  - R-c3  утяжеление ТОЛЬКО на первой публикации (finalize); команда «протокол …»
          и CLI остаются на одном прогоне (source-scan).
  + РИСК4 склейка идёт ЧЕРЕЗ файл-обёртку: diarization-fix в транскрипт, дисклеймер
          и ⚠️-авторства тёзок ЖИВЫ после best-of-2 (первая публикация не регрессит).
  + опасная тройка: второй черновик и склейка в логи пишут ТОЛЬКО числа.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss22_best_of_2 -v
"""
from __future__ import annotations

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
# (приём из тестов Ф4/Ф6/Ф7/Ф2-ISS22).
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib import llm_postprocess as lp  # noqa: E402
from lib import series_memory as sm  # noqa: E402

_DISCLAIMER = "> _Авторство реплик распознано автоматически, возможны неточности._"
_METHOD = "МЕТОДИЧКА (тест): пиши протокол по структуре."

# Уникальная задача Y — её держит ТОЛЬКО черновик B (best-of-2 ловит то, что A упустил).
Y_PHRASE = "Согласовать дизайн лендинга с подрядчиком"

META = {
    "series": None,
    "date": "2026-06-24",
    "durationMin": 30,
    "expectedParticipants": ["Илья", "Михаил"],
    "participants": ["Илья", "Михаил"],
    "transcript_filename": "2026-06-24.md",
}

# Сырой ответ модели на ГЕНЕРАЦИЮ черновика B (без дисклеймера — generate_protocol
# его вставит сам). Держит задачу Y под «Илья».
DRAFT_B_RAW = (
    "#протоколвстречи 24.06.2026\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "**Илья**\n"
    "▪️ " + Y_PHRASE + ".\n\n"
    "**Михаил**\n"
    "▪️ Подготовить отчёт по складу.\n\n"
    "## 2) Решения\n\n"
    "🔸 Бюджет урезан на 15 процентов.\n"
)

# Черновик A на диске (regenerate выше его записал): задачу Y ВЫРОНИЛ (лоссовый сэмпл).
DRAFT_A = (
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

# Что вернул бы критик-склейщик, подчинившись инварианту: Y восстановлен МОЛЧА.
IMPROVED_WITH_Y = (
    "#протоколвстречи 24.06.2026\n\n"
    + _DISCLAIMER + "\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "**Илья**\n"
    "▪️ " + Y_PHRASE + ".\n\n"
    "**Михаил**\n"
    "▪️ Подготовить отчёт по складу.\n\n"
    "## 2) Решения\n\n"
    "🔸 Бюджет урезан на 15 процентов.\n"
)

TRANSCRIPT = (
    "#транскрипт 2026-06-24\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "**[00:00] Илья:** Я согласую дизайн лендинга с подрядчиком на неделе.\n\n"
    "**[00:10] Михаил:** Отчёт по складу подготовлю.\n\n"
    "**[00:20] Илья:** Бюджет урезаем на пятнадцать процентов.\n"
)


def _envelope(protocol: str, *, findings=None, edits=None) -> str:
    """Envelope-ответ второго прохода (как его вернул бы claude) — см. Ф7-тесты."""
    diag = {"findings": findings or [], "edits": edits or {}}
    return (
        lp._REWRITE_DIAG_MARKER + "\n"
        + json.dumps(diag, ensure_ascii=False) + "\n"
        + lp._REWRITE_PROTOCOL_MARKER + "\n"
        + protocol
    )


# ==========================================================================
# Юнит: генератор второго черновика (НЕС1 — в памяти, без atomic-записи)
# ==========================================================================
class TestSecondDraftGeneration(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self._tmp.name) / "2026-06-24.md"
        self.transcript.write_text(TRANSCRIPT, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_draft_text_same_inputs(self):
        """Хелпер зовёт generate_protocol с ТЕМ ЖЕ входом, что черновик A
        (series_memory + open_tasks), и возвращает текст (best-of-2: 2 независимых
        прогона одной встречи)."""
        with mock.patch.object(lp, "generate_protocol", return_value=DRAFT_B_RAW) as gp:
            out = lp.generate_alt_draft_for_best_of_2(
                self.transcript, META, meeting_sid="sid",
                series_memory="SM-BLOCK", open_tasks="OT-BLOCK",
            )
        self.assertEqual(out, DRAFT_B_RAW)
        gp.assert_called_once()
        _, kwargs = gp.call_args
        self.assertEqual(kwargs.get("series_memory"), "SM-BLOCK")
        self.assertEqual(kwargs.get("open_tasks"), "OT-BLOCK")
        self.assertEqual(kwargs.get("meeting_sid"), "sid")

    def test_no_atomic_write_NES1(self):
        """🔴 НЕС1: второй черновик НЕ пишется на диск (никаких файлов в папке
        транскрипта, кроме самого транскрипта) — иначе затёр бы черновик A."""
        before = {p.name for p in self.transcript.parent.iterdir()}
        with mock.patch.object(lp, "generate_protocol", return_value=DRAFT_B_RAW):
            lp.generate_alt_draft_for_best_of_2(self.transcript, META, meeting_sid="sid")
        after = {p.name for p in self.transcript.parent.iterdir()}
        self.assertEqual(before, after)  # ни одного нового файла

    def test_disabled_gate_returns_none_no_generation(self):
        """Kill-switch ENABLE_FINALIZE_BEST_OF_2=0 → None, generate_protocol НЕ зовётся."""
        with mock.patch.dict(os.environ, {"ENABLE_FINALIZE_BEST_OF_2": "0"}):
            with mock.patch.object(lp, "generate_protocol") as gp:
                out = lp.generate_alt_draft_for_best_of_2(self.transcript, META)
        self.assertIsNone(out)
        gp.assert_not_called()

    def test_generation_error_returns_none_no_raise_R_c2(self):
        """🔴 R-c2: сбой генерации второго черновика (ProtocolGenerationError) →
        None, БЕЗ исключения (caller деградирует на одинарную версию)."""
        with mock.patch.object(lp, "generate_protocol",
                               side_effect=lp.ProtocolGenerationError("timeout")):
            out = lp.generate_alt_draft_for_best_of_2(self.transcript, META, meeting_sid="sid")
        self.assertIsNone(out)

    def test_unexpected_error_returns_none_no_raise_R_c2(self):
        """🔴 R-c2: даже НЕОЖИДАННОЕ исключение генерации не валит finalize (ловим
        широко — деградация важнее точности класса)."""
        with mock.patch.object(lp, "generate_protocol", side_effect=RuntimeError("boom")):
            out = lp.generate_alt_draft_for_best_of_2(self.transcript, META, meeting_sid="sid")
        self.assertIsNone(out)

    def test_empty_transcript_returns_none(self):
        self.transcript.write_text("   \n", encoding="utf-8")
        with mock.patch.object(lp, "generate_protocol") as gp:
            out = lp.generate_alt_draft_for_best_of_2(self.transcript, META)
        self.assertIsNone(out)
        gp.assert_not_called()

    def test_missing_transcript_returns_none(self):
        missing = self.transcript.parent / "nope.md"
        out = lp.generate_alt_draft_for_best_of_2(missing, META, meeting_sid="sid")
        self.assertIsNone(out)

    def test_log_only_counts_no_text(self):
        """🔴 Опасная тройка: лог второго черновика несёт ЧИСЛА (out_len), но НЕ
        текст транскрипта/протокола."""
        secret = "урезаем на пятнадцать процентов"  # фраза из транскрипта
        with mock.patch.object(lp, "call_claude_print", return_value=DRAFT_B_RAW):
            with self.assertLogs("lib.llm_postprocess", level="INFO") as cm:
                out = lp.generate_alt_draft_for_best_of_2(
                    self.transcript, META, meeting_sid="sid", method_text=_METHOD)
        self.assertIsNotNone(out)
        joined = "\n".join(cm.output)
        self.assertIn("out_len=", joined)        # счётчик длины есть
        self.assertNotIn(secret, joined)         # текста транскрипта нет
        self.assertNotIn(Y_PHRASE, joined)       # текста пунктов нет


# ==========================================================================
# R-c1: end-to-end — черновик B ловит Y, склейка доносит Y в финал
# ==========================================================================
class TestBestOf2Merge(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1",
             "ENABLE_FINALIZE_BEST_OF_2": "1"},
        )
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-06-24.md"
        self.protocol = d / "2026-06-24-protokol.md"
        self.transcript.write_text(TRANSCRIPT, encoding="utf-8")
        self.protocol.write_text(DRAFT_A, encoding="utf-8")  # на диске — черновик A без Y

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def test_draft_b_extractable(self):
        """Линк best-of-2: задача Y из (реально сгенерённого) черновика B извлекается
        детерминированно (series_memory) → дойдёт до инварианта склейки."""
        with mock.patch.object(lp, "call_claude_print", return_value=DRAFT_B_RAW):
            draft_b = lp.generate_alt_draft_for_best_of_2(
                self.transcript, META, meeting_sid="sid", method_text=_METHOD)
        self.assertIsNotNone(draft_b)
        items = sm.extract_items_from_versions([draft_b])
        self.assertTrue(any(Y_PHRASE in x for x in items))

    def test_draft_b_only_task_reaches_final_R_c1(self):
        """🔴 R-c1 end-to-end: реальный generate_alt_draft (real generate_protocol,
        мок только CLI) делает черновик B с задачей Y; склейка через файл-обёртку
        доносит Y в финал, хотя черновик A на диске её НЕ содержит. Ровно ОДИН
        доп. прогон генерации (best-of-2 = N=2, не больше)."""
        captured = {}
        calls = {"gen": 0}

        def fake_claude(user, system="", **kw):
            if user.startswith("ЧЕРНОВИК протокола"):  # склейка-критик
                captured["critic_user"] = user
                captured["critic_system"] = system
                return _envelope(IMPROVED_WITH_Y)
            calls["gen"] += 1                            # генерация черновика B
            return DRAFT_B_RAW

        with mock.patch.object(lp, "call_claude_print", side_effect=fake_claude):
            draft_b = lp.generate_alt_draft_for_best_of_2(
                self.transcript, META, meeting_sid="sid", method_text=_METHOD)
            self.assertIsNotNone(draft_b)
            self.assertIn(Y_PHRASE, draft_b)             # Y дожила пост-обработку генерации
            # Реплика finalize-блока: на первой публикации прошлой версии нет → только B.
            merge_sources = [s for s in (None, draft_b) if s]
            lp.review_and_flag_protocol_file(
                self.protocol, self.transcript,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid", rewrite=True, prior_sources=merge_sources or None,
            )

        self.assertEqual(calls["gen"], 1)                # ровно один доп. прогон (N=2)
        self.assertIn(Y_PHRASE, captured["critic_user"])      # Y дошла до критика из B
        self.assertIn("ИНВАРИАНТ", captured["critic_system"])  # инвариант «не теряем» активен
        self.assertIn(Y_PHRASE, self.protocol.read_text(encoding="utf-8"))  # Y в финале на диске

    def test_refinalization_carries_prior_and_draft_b(self):
        """Перефинализация: prior_sources = [прошлая версия, черновик B] — пункты
        ОБОИХ источников уходят инварианту (Ф2 prior + Ф3 best-of-2 в одной склейке)."""
        captured = {}

        def fake_claude(user, system="", **kw):
            if user.startswith("ЧЕРНОВИК протокола"):
                captured["critic_user"] = user
                return _envelope(IMPROVED_WITH_Y)
            return DRAFT_B_RAW

        prior_version = (
            "#протоколвстречи 24.06.2026\n\n## 1) Задачи\n\n**Татьяна**\n"
            "▪️ Выслать расчёт по аренде.\n"
        )
        with mock.patch.object(lp, "call_claude_print", side_effect=fake_claude):
            draft_b = lp.generate_alt_draft_for_best_of_2(
                self.transcript, META, meeting_sid="sid", method_text=_METHOD)
            merge_sources = [s for s in (prior_version, draft_b) if s]
            lp.review_and_flag_protocol_file(
                self.protocol, self.transcript,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid", rewrite=True, prior_sources=merge_sources or None,
            )
        # пункт из прошлой версии И пункт из черновика B — оба у критика.
        self.assertIn("Выслать расчёт по аренде", captured["critic_user"])
        self.assertIn(Y_PHRASE, captured["critic_user"])


# ==========================================================================
# R-c2: деградация (сбой второго прогона ИЛИ склейки)
# ==========================================================================
class TestBestOf2Degradation(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1",
             "ENABLE_FINALIZE_BEST_OF_2": "1"},
        )
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-06-24.md"
        self.protocol = d / "2026-06-24-protokol.md"
        self.transcript.write_text(TRANSCRIPT, encoding="utf-8")
        self.protocol.write_text(DRAFT_A, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def test_draft_b_failure_yields_single_review_R_c2(self):
        """🔴 R-c2 (второй прогон): генерация черновика B падает → helper=None →
        merge_sources пуст → ОДИНАРНЫЙ self-review (без инварианта), финал есть, без
        падения, встреча не теряется."""
        with mock.patch.object(lp, "generate_protocol",
                               side_effect=lp.ProtocolGenerationError("timeout")):
            draft_b = lp.generate_alt_draft_for_best_of_2(
                self.transcript, META, meeting_sid="sid")
        self.assertIsNone(draft_b)
        merge_sources = [s for s in (None, draft_b) if s]
        self.assertEqual(merge_sources, [])  # нечего склеивать

        captured = {}

        def fake_claude(user, system="", **kw):
            captured["system"] = system
            return _envelope(IMPROVED_WITH_Y)

        with mock.patch.object(lp, "call_claude_print", side_effect=fake_claude):
            n = lp.review_and_flag_protocol_file(
                self.protocol, self.transcript,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid", rewrite=True, prior_sources=merge_sources or None,
            )
        self.assertNotIn("ИНВАРИАНТ", captured["system"])  # одинарный self-review (R-b4)
        self.assertTrue(self.protocol.is_file())           # файл не потерян
        self.assertGreater(n, 0)                           # критик отработал

    def test_merge_critic_timeout_keeps_draft_a_R_c2(self):
        """🔴 R-c2 (склейка): критик-склейка таймаутит ПРИ best-of-2 (prior_sources=[B])
        → на диске остаётся черновик A (деградация, не потеря, не падение)."""
        with mock.patch.object(lp, "call_claude_print",
                               side_effect=lp.ClaudeCliTimeout("slow")):
            n = lp.review_and_flag_protocol_file(
                self.protocol, self.transcript,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid", rewrite=True, prior_sources=[DRAFT_B_RAW],
            )
        self.assertEqual(self.protocol.read_text(encoding="utf-8"), DRAFT_A)  # A уцелел
        self.assertEqual(n, 0)


# ==========================================================================
# РИСК4: сайд-эффекты finalize живы после best-of-2 склейки
# ==========================================================================
class TestSideEffectsPreservedRISK4(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1",
             "ENABLE_FINALIZE_BEST_OF_2": "1"},
        )
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-06-24.md"
        self.protocol = d / "2026-06-24-protokol.md"
        # Транскрипт с «Спикер N» — критик пометит fix деления по спикерам.
        self.transcript.write_text(
            "#транскрипт 2026-06-24\n\n**Участники:** Илья, Михаил\n\n---\n\n"
            "**[00:00] Спикер 1:** Михаил, бюджет закрыли?\n\n"
            "**[00:08] Спикер 1:** Да, закрыли на девяносто.\n",
            encoding="utf-8",
        )
        self.protocol.write_text(DRAFT_A, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def test_diar_fix_disclaimer_authorship_survive_merge(self):
        """🔴 РИСК4: склейка best-of-2 идёт ЧЕРЕЗ файл-обёртку с полным checks-tuple →
        после склейки (prior_sources=[B]) сайд-эффекты finalize НА МЕСТЕ:
          (1) diarization-fix re-attribution в ТРАНСКРИПТЕ,
          (2) дисклеймер авторства в протоколе (даже если критик его выкинул),
          (3) ⚠️ авторства тёзок (extra_findings) в протоколе."""
        # Критик возвращает улучшенный протокол БЕЗ дисклеймера + diarization-fix finding.
        improved_no_disclaimer = (
            "#протоколвстречи 24.06.2026\n\n"
            "**Участники:** Илья, Михаил\n\n---\n\n"
            "## 1) Задачи\n\n**Илья**\n▪️ " + Y_PHRASE + ".\n\n"
            "**Михаил**\n▪️ Подготовить отчёт по складу.\n"
        )
        findings = [
            {"section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
             "quote": "Да, закрыли на девяносто", "note": "ответ под спрашивавшим"},
        ]
        extra = lp.build_authorship_uncertainty_findings(["Михаил Еремеев"])
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(improved_no_disclaimer, findings=findings)):
            lp.review_and_flag_protocol_file(
                self.protocol, self.transcript,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid", rewrite=True,
                extra_findings=extra, prior_sources=[DRAFT_B_RAW],
            )
        tr = self.transcript.read_text(encoding="utf-8")
        pr = self.protocol.read_text(encoding="utf-8")
        # (1) diarization-fix → транскрипт (re-attribution реплики на Спикер 2).
        self.assertIn("**[00:08] Спикер 2:** Да, закрыли на девяносто", tr)
        # (2) дисклеймер восстановлен в протоколе (Ф4а идемпотентная вставка).
        self.assertIn("Авторство реплик", pr)
        # (3) ⚠️ авторства тёзок (extra_findings) в протоколе.
        self.assertIn("авторство под вопросом", pr)
        self.assertIn("Михаил Еремеев", pr)

    def test_forensic_counter_only_numbers_after_merge(self):
        """🔴 Опасная тройка: лог склейки best-of-2 несёт ЧИСЛА (prior-items/at-risk/
        recovered), но НЕ текст пунктов/реплик."""
        secret = "закрыли на девяносто"
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(IMPROVED_WITH_Y)):
            with self.assertLogs("lib.llm_postprocess", level="INFO") as cm:
                lp.review_and_flag_protocol_file(
                    self.protocol, self.transcript,
                    checks=("values", "roles", "memory", "diarization"),
                    meeting_sid="sid", rewrite=True, prior_sources=[DRAFT_B_RAW],
                )
        joined = "\n".join(cm.output)
        self.assertIn("prior-items=", joined)
        self.assertIn("at-risk=", joined)
        self.assertIn("recovered=", joined)
        self.assertNotIn(secret, joined)       # без текста реплик
        self.assertNotIn(Y_PHRASE, joined)     # без текста пунктов


# ==========================================================================
# R-c3: утяжеление ТОЛЬКО на первой публикации (source-scan)
# ==========================================================================
class TestBestOf2OnlyInFinalize(unittest.TestCase):

    def test_second_draft_only_in_finalize_R_c3(self):
        """🔴 R-c3: второй прогон генерации (best-of-2) есть ТОЛЬКО в finalize-пути;
        команда «протокол …», CLI и clarify-hook остаются на ОДНОМ прогоне."""
        fin = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        self.assertIn("generate_alt_draft_for_best_of_2(", fin,
                      "finalize не генерит второй черновик — best-of-2 не подключён")
        for rel in ("meetings_listener.py", "tools/regenerate-protocol.py",
                    "lib/clarify_worker.py"):
            src = (_NOTARY / rel).read_text(encoding="utf-8")
            self.assertNotIn("generate_alt_draft_for_best_of_2", src,
                             f"{rel}: best-of-2 утяжелил редкий путь (R-c3 нарушен)")

    def test_finalize_draft_b_before_merge_and_in_prior_sources(self):
        """R-c1 wiring (достижимость): в finalize черновик B генерится ДО склейки и
        уходит в prior_sources склейщика-критика (иначе best-of-2 — мёртвый код)."""
        fin = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        gi = fin.find("generate_alt_draft_for_best_of_2(")
        ri = fin.find("review_and_flag_protocol_file(", gi)
        self.assertNotEqual(gi, -1, "генерация черновика B не найдена")
        self.assertNotEqual(ri, -1, "вызов склейки после генерации B не найден")
        self.assertLess(gi, ri, "черновик B должен генериться ДО review-склейки")
        # draft_b участвует в merge_sources, merge_sources уходит в prior_sources склейки.
        self.assertIn("draft_b", fin[gi:ri + 400])
        self.assertIn("prior_sources=merge_sources", fin[ri:ri + 1300])

    def test_best_of_2_through_file_wrapper_not_bare_RISK4(self):
        """🔴 РИСК4 (source-scan): finalize гонит склейку ЧЕРЕЗ файл-обёртку
        review_and_flag_protocol_file (rewrite=True + полный checks-tuple), НЕ зовёт
        голый review_and_rewrite_protocol мимо неё (иначе потеря сайд-эффектов)."""
        fin = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        self.assertNotIn("review_and_rewrite_protocol(", fin,
                         "finalize зовёт голый критик мимо файл-обёртки (РИСК4)")
        i = fin.find("review_and_flag_protocol_file(")
        call_chunk = fin[i:i + 1300]
        self.assertIn("rewrite=True", call_chunk)
        for chk in ("values", "roles", "memory", "diarization"):
            self.assertIn(f'"{chk}"', call_chunk, f"checks-tuple неполон: нет {chk}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
