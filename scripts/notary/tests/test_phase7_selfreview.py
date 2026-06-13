"""Тесты Ф7 (план `2026-06-13-umnyi-protokol-assemblyai.md`) — двухпроходная
генерация (self-review): второй проход «редактор-критик» ПЕРЕПИСЫВАЕТ черновик по
чек-листу, а не только флажит.

Покрывает REQ G8 (+ ГРАН1/НЕС1, РИСК1, G10):
  - G8   после черновика — проход-критик (пропущенные договорённости / задачи без
         владельца / смешение ролей / плоские формулировки / числа-инверсии) →
         финал чище черновика. Синтетический тест улучшения: черновик с задачей
         без владельца и пропущенной договорённостью → на диске улучшенный текст.
  - ГРАН1/НЕС1  второй проход — ТОТ ЖЕ единственный вызов, что несёт ревизию
         диаризации (Ф4); НЕ третий Opus-вызов (инвариант call_count==1).
  - РИСК1 таймаут/сбой/битый ответ второго прохода → черновик отдаётся как финал
         (деградация с маркером), встреча НЕ теряется, файл зря не трогаем.
  - G10  диагностика критика — в метаданных-счётчиках, без текста транскрипта/реплик.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase7_selfreview -v
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

# llm_postprocess тянет тяжёлые third-party через соседние модули, которых на
# маке/CI может не быть. Тестируемая логика их не использует — подкладываем
# минимальные стабы, если пакет реально отсутствует (приём из тестов Ф4/Ф6).
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib import llm_postprocess as lp  # noqa: E402

_DISCLAIMER = "> _Авторство реплик распознано автоматически, возможны неточности._"

# Черновик: задача БЕЗ владельца + плоская формулировка + пропущенная договорённость.
DRAFT = (
    "#протоколвстречи 13.06.2026\n\n"
    + _DISCLAIMER + "\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "▪️ Подготовить отчёт по складу.\n"
    "▪️ Обсудили вопрос с бюджетом.\n"
)

# Транскрипт — источник истины: владелец задачи виден, договорённость по бюджету есть.
TRANSCRIPT = (
    "#транскрипт 2026-06-13\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "**[00:00] Илья:** Михаил, подготовь отчёт по складу к пятнице.\n\n"
    "**[00:10] Михаил:** Сделаю к пятнице.\n\n"
    "**[00:20] Илья:** Зафиксируем: бюджет урезаем на пятнадцать процентов.\n\n"
    "**[00:25] Михаил:** Согласен, минус пятнадцать.\n"
)

# Что вернул бы критик (вычитанный финал): владелец проставлен, договорённость добавлена,
# плоская формулировка убрана.
IMPROVED = (
    "#протоколвстречи 13.06.2026\n\n"
    + _DISCLAIMER + "\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "▪️ Подготовить отчёт по складу к пятнице — Михаил.\n\n"
    "## 2) Решения\n\n"
    "▪️ Бюджет урезан на 15%.\n"
)


def _envelope(protocol: str, *, findings=None, edits=None) -> str:
    """Собирает envelope-ответ второго прохода как его вернул бы claude."""
    diag = {"findings": findings or [], "edits": edits or {}}
    return (
        lp._REWRITE_DIAG_MARKER + "\n"
        + json.dumps(diag, ensure_ascii=False) + "\n"
        + lp._REWRITE_PROTOCOL_MARKER + "\n"
        + protocol
    )


# ==========================================================================
# system-prompt второго прохода: чек-лист + envelope + (опц.) diarization
# ==========================================================================
class TestRewriteSystemPrompt(unittest.TestCase):

    def test_checklist_and_envelope_present(self):
        sp = lp._build_rewrite_system_prompt(("values", "roles", "memory", "diarization"))
        # Все пять пунктов чек-листа критика.
        self.assertIn("ПРОПУЩЕННЫЕ ДОГОВОРЁННОСТИ", sp)
        self.assertIn("ЗАДАЧИ БЕЗ ВЛАДЕЛЬЦА", sp)
        self.assertIn("СМЕШЕНИЕ РОЛЕЙ", sp)
        self.assertIn("ПЛОСКИЕ ФОРМУЛИРОВКИ", sp)
        self.assertIn("ЧИСЛА И ИНВЕРСИИ", sp)
        # Envelope-маркеры.
        self.assertIn(lp._REWRITE_DIAG_MARKER, sp)
        self.assertIn(lp._REWRITE_PROTOCOL_MARKER, sp)
        # Сохрани формат/структуру — это редактура, не пересборка.
        self.assertIn("#протоколвстречи", sp)

    def test_diarization_folded_into_same_call(self):
        """ГРАН1/НЕС1: при checks с diarization секция Ф4 — в ТОМ ЖЕ промпте."""
        sp = lp._build_rewrite_system_prompt(("values", "diarization"))
        self.assertIn("деления по спикерам", sp)
        self.assertIn("verdict", sp)
        self.assertIn("speaker_to", sp)

    def test_diarization_absent_without_check(self):
        sp = lp._build_rewrite_system_prompt(("values",))
        self.assertNotIn("деления по спикерам", sp)
        self.assertNotIn("speaker_to", sp)
        # Контракт findings явно помечен пустым.
        self.assertIn("оставь пустым", sp)


# ==========================================================================
# Парсер envelope: протокол сырым markdown'ом, DIAG — маленький JSON
# ==========================================================================
class TestParseRewriteResponse(unittest.TestCase):

    def test_well_formed(self):
        edits = {"added_agreements": 1, "owners_assigned": 1, "roles_separated": 0,
                 "sharpened": 1, "numbers_fixed": 0}
        f, p, e = lp._parse_rewrite_response(_envelope(IMPROVED, edits=edits))
        self.assertIsNotNone(p)
        self.assertTrue(p.startswith("#протоколвстречи"))
        self.assertIn("— Михаил", p)
        self.assertEqual(e["added_agreements"], 1)
        self.assertEqual(e["owners_assigned"], 1)
        self.assertEqual(f, [])

    def test_protocol_without_header_degrades_to_none(self):
        """РИСК1: ответ без шапки протокола → None (caller отдаст черновик)."""
        raw = lp._REWRITE_PROTOCOL_MARKER + "\nпросто проза без шапки протокола"
        f, p, e = lp._parse_rewrite_response(raw)
        self.assertIsNone(p)

    def test_missing_protocol_marker_degrades_to_none(self):
        f, p, e = lp._parse_rewrite_response('{"findings": [], "edits": {}}')
        self.assertIsNone(p)

    def test_garbled_diag_but_good_protocol(self):
        """Битый DIAG-JSON не должен терять валидный протокол (робастность envelope)."""
        raw = (lp._REWRITE_DIAG_MARKER + "\n{это не json,,,}\n"
               + lp._REWRITE_PROTOCOL_MARKER + "\n" + IMPROVED)
        f, p, e = lp._parse_rewrite_response(raw)
        self.assertIsNotNone(p)
        self.assertTrue(p.startswith("#протоколвстречи"))
        self.assertEqual(f, [])
        self.assertEqual(e, {})

    def test_fenced_protocol_unwrapped(self):
        raw = (lp._REWRITE_DIAG_MARKER + '\n{"findings":[],"edits":{}}\n'
               + lp._REWRITE_PROTOCOL_MARKER + "\n```markdown\n" + IMPROVED + "\n```")
        f, p, e = lp._parse_rewrite_response(raw)
        self.assertIsNotNone(p)
        self.assertTrue(p.startswith("#протоколвстречи"))
        self.assertNotIn("```", p)

    def test_diarization_findings_parsed(self):
        findings = [{"section": "diarization", "quote": "Палеты вывезем",
                     "note": "под вопросом", "verdict": "flag"}]
        f, p, e = lp._parse_rewrite_response(_envelope(IMPROVED, findings=findings))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["section"], "diarization")
        self.assertEqual(f[0]["verdict"], "flag")

    def test_edits_clamped_and_unknown_keys_dropped(self):
        edits = {"added_agreements": -5, "owners_assigned": 99999,
                 "sharpened": "3", "made_up_key": 7}
        f, p, e = lp._parse_rewrite_response(_envelope(IMPROVED, edits=edits))
        self.assertEqual(e["added_agreements"], 0)   # отрицательное → 0
        self.assertEqual(e["owners_assigned"], 0)    # за потолком → 0
        self.assertEqual(e["sharpened"], 3)          # "3" → 3
        self.assertNotIn("made_up_key", e)           # неизвестный ключ отброшен

    def test_empty_response(self):
        f, p, e = lp._parse_rewrite_response("")
        self.assertEqual((f, p, e), ([], None, {}))


class TestSafeCount(unittest.TestCase):
    def test_clamps(self):
        self.assertEqual(lp._safe_count(3), 3)
        self.assertEqual(lp._safe_count("5"), 5)
        self.assertEqual(lp._safe_count(-1), 0)
        self.assertEqual(lp._safe_count(10**9), 0)
        self.assertEqual(lp._safe_count(None), 0)
        self.assertEqual(lp._safe_count("abc"), 0)


# ==========================================================================
# review_and_rewrite_protocol: ОДИН вызов + деградация РИСК1
# ==========================================================================
class TestReviewAndRewriteProtocol(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1"},
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_success_single_call_includes_diarization(self):
        """🔴 НЕС1: РОВНО один claude-вызов; его system несёт diarization-секцию."""
        with mock.patch.object(lp, "call_claude_print", return_value=_envelope(IMPROVED)) as m:
            res = lp.review_and_rewrite_protocol(
                DRAFT, TRANSCRIPT, checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid",
            )
        self.assertEqual(m.call_count, 1)
        self.assertEqual(res.degraded, "no")
        self.assertIsNotNone(res.protocol)
        self.assertIn("— Михаил", res.protocol)
        system = m.call_args.kwargs.get("system", "")
        self.assertIn("деления по спикерам", system)
        # Модель второго прохода — Opus (env-override по умолчанию).
        self.assertEqual(m.call_args.kwargs.get("model"), lp.PROTOCOL_REWRITE_MODEL)

    def test_timeout_degrades_to_draft(self):
        """🔴 РИСК1: таймаут второго прохода → protocol=None (черновик как финал)."""
        with mock.patch.object(lp, "call_claude_print",
                               side_effect=lp.ClaudeCliTimeout("slow")) as m:
            res = lp.review_and_rewrite_protocol(
                DRAFT, TRANSCRIPT, checks=("values", "diarization"), meeting_sid="sid",
            )
        self.assertEqual(m.call_count, 1)
        self.assertIsNone(res.protocol)
        self.assertEqual(res.degraded, "rewrite-timeout")

    def test_no_cli_degrades(self):
        with mock.patch.object(lp, "call_claude_print",
                               side_effect=lp.ClaudeCliNotInstalled("no claude")):
            res = lp.review_and_rewrite_protocol(DRAFT, TRANSCRIPT, meeting_sid="sid")
        self.assertIsNone(res.protocol)
        self.assertEqual(res.degraded, "no-cli")

    def test_generic_error_degrades(self):
        with mock.patch.object(lp, "call_claude_print",
                               side_effect=lp.ClaudeCliError("boom")):
            res = lp.review_and_rewrite_protocol(DRAFT, TRANSCRIPT, meeting_sid="sid")
        self.assertIsNone(res.protocol)
        self.assertEqual(res.degraded, "rewrite-error")

    def test_malformed_response_degrades(self):
        """Ответ есть, но без шапки протокола → деградация malformed (черновик)."""
        bad = lp._REWRITE_PROTOCOL_MARKER + "\nничего похожего на протокол"
        with mock.patch.object(lp, "call_claude_print", return_value=bad):
            res = lp.review_and_rewrite_protocol(DRAFT, TRANSCRIPT, meeting_sid="sid")
        self.assertIsNone(res.protocol)
        self.assertEqual(res.degraded, "rewrite-malformed")

    def test_disabled_makes_no_call(self):
        """Kill-switch ENABLE_PROTOCOL_SELFREVIEW=0 → вызова нет, degraded=disabled."""
        with mock.patch.dict(os.environ, {"ENABLE_PROTOCOL_SELFREVIEW": "0"}):
            with mock.patch.object(lp, "call_claude_print") as m:
                res = lp.review_and_rewrite_protocol(DRAFT, TRANSCRIPT, meeting_sid="sid")
        self.assertEqual(m.call_count, 0)
        self.assertEqual(res.degraded, "disabled")


# ==========================================================================
# Достижимость из реального триггера (review_and_flag_protocol_file rewrite=True)
# + G8 синтетическое улучшение на диске
# ==========================================================================
class TestSelfreviewWiring(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1"},
        )
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-06-13.md"
        self.protocol = d / "2026-06-13-protokol.md"
        self.transcript.write_text(TRANSCRIPT, encoding="utf-8")
        self.protocol.write_text(DRAFT, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def _run(self, *, checks=("values", "roles", "memory", "diarization")):
        return lp.review_and_flag_protocol_file(
            self.protocol, self.transcript, checks=checks,
            meeting_sid="sid", rewrite=True,
        )

    def test_improved_protocol_reaches_disk(self):
        """🔴 G8: финал на диске чище черновика — владелец задачи проставлен,
        пропущенная договорённость добавлена, плоская формулировка убрана."""
        with mock.patch.object(lp, "call_claude_print", return_value=_envelope(
                IMPROVED, edits={"added_agreements": 1, "owners_assigned": 1, "sharpened": 1})):
            n = self._run()
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertIn("— Михаил", on_disk)               # задача обрела владельца
        self.assertIn("Бюджет урезан на 15%", on_disk)   # договорённость добавлена
        self.assertNotIn("Обсудили вопрос с бюджетом", on_disk)  # плоское убрано
        self.assertIn("Авторство реплик", on_disk)       # дисклеймер выжил редактуру
        self.assertGreater(n, 0)

    def test_degradation_keeps_draft_on_disk(self):
        """🔴 РИСК1: таймаут второго прохода → на диске остаётся ЧЕРНОВИК (не потеря)."""
        with mock.patch.object(lp, "call_claude_print",
                               side_effect=lp.ClaudeCliTimeout("slow")):
            n = self._run()
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertEqual(on_disk, DRAFT)  # файл не тронут — черновик уцелел
        self.assertEqual(n, 0)

    def test_malformed_keeps_draft_on_disk(self):
        bad = lp._REWRITE_PROTOCOL_MARKER + "\nмусор"
        with mock.patch.object(lp, "call_claude_print", return_value=bad):
            n = self._run()
        self.assertEqual(self.protocol.read_text(encoding="utf-8"), DRAFT)

    def test_diarization_fix_to_transcript_flag_to_protocol(self):
        """Ф4 сохранён в режиме rewrite: fix→транскрипт, flag→улучшенный протокол."""
        self.transcript.write_text(
            "#транскрипт 2026-06-13\n\n**Участники:** Илья, Михаил\n\n---\n\n"
            "**[00:00] Спикер 1:** Михаил, бюджет закрыли?\n\n"
            "**[00:08] Спикер 1:** Да, закрыли на девяносто.\n",
            encoding="utf-8",
        )
        findings = [
            {"section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
             "quote": "Да, закрыли на девяносто", "note": "ответ под спрашивавшим"},
            {"section": "diarization", "verdict": "flag",
             "quote": "Михаил, бюджет закрыли", "note": "под вопросом деление"},
        ]
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(IMPROVED, findings=findings)):
            self._run()
        tr = self.transcript.read_text(encoding="utf-8")
        self.assertIn("**[00:08] Спикер 2:** Да, закрыли на девяносто", tr)  # fix → транскрипт
        pr = self.protocol.read_text(encoding="utf-8")
        self.assertIn("⚠️ спикер под вопросом", pr)   # flag → протокол
        self.assertIn("— Михаил", pr)                  # и улучшенный текст на месте

    def test_content_findings_not_flagged_in_rewrite_mode(self):
        """A5: в режиме rewrite content-находки (values/roles) исправлены в тексте,
        ⚠️-пометок для них НЕ ставим — владелец видит чистый финал."""
        findings = [
            {"section": "values", "quote": "что-то", "note": "сверь число"},
            {"section": "roles", "quote": "Илья", "note": "смешаны зоны"},
        ]
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(IMPROVED, findings=findings)):
            self._run()
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertNotIn("## ⚠️ Проверить", on_disk)
        self.assertNotIn("⚠️ проверь", on_disk)

    def test_single_claude_call_not_two(self):
        """🔴 ГРАН1: rewrite-путь делает РОВНО один claude-вызов (не флаг+rewrite)."""
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(IMPROVED)) as m:
            self._run()
        self.assertEqual(m.call_count, 1)

    def test_log_only_counters_no_transcript_text(self):
        """🔴 G10: лог второго прохода — только счётчики/метаданные, без текста реплик."""
        secret = "бюджет урезаем на пятнадцать процентов"  # есть в транскрипте
        with mock.patch.object(lp, "call_claude_print", return_value=_envelope(
                IMPROVED, edits={"added_agreements": 1, "owners_assigned": 1})):
            with self.assertLogs("lib.llm_postprocess", level="INFO") as cm:
                self._run()
        joined = "\n".join(cm.output)
        self.assertIn("added=1", joined)        # счётчики есть
        self.assertIn("owners=1", joined)
        self.assertNotIn(secret, joined)        # текста транскрипта нет
        self.assertNotIn("Михаил, подготовь отчёт", joined)


# ==========================================================================
# Флаг-only путь (rewrite=False) НЕ затронут — регресс Ф4–Ф6
# ==========================================================================
class TestFlagPathPreserved(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-06-13.md"
        self.protocol = d / "2026-06-13-protokol.md"
        self.transcript.write_text(TRANSCRIPT, encoding="utf-8")
        self.protocol.write_text(DRAFT, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_rewrite_false_uses_flag_path(self):
        """rewrite по умолчанию False → старый путь review_protocol (флажит, не
        переписывает). Доказательство: вызывается review_protocol, НЕ rewrite."""
        finding = [{"section": "values", "quote": "Обсудили вопрос с бюджетом",
                    "note": "пусто по сути"}]
        with mock.patch.object(lp, "review_protocol", return_value=finding) as rp, \
                mock.patch.object(lp, "review_and_rewrite_protocol") as rr:
            n = lp.review_and_flag_protocol_file(
                self.protocol, self.transcript,
                checks=("values", "roles", "memory", "diarization"),
                meeting_sid="sid",  # rewrite НЕ передан → False
            )
        rp.assert_called_once()
        rr.assert_not_called()
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertIn("⚠️ проверь", on_disk)  # флаг-поведение Ф5 сохранено
        self.assertEqual(on_disk.count("Подготовить отчёт по складу."), 1)  # текст НЕ переписан


# ==========================================================================
# Паритет боевых call-site (footgun review-checks-two-call-sites)
# ==========================================================================
class TestCallSiteParity(unittest.TestCase):

    def test_both_call_sites_pass_rewrite_true(self):
        """finalize И clarify должны звать review_and_flag_protocol_file(rewrite=True)
        — иначе поздний clarify терял бы редактуру второго прохода."""
        for rel in ("finalize-meeting.py", "lib/clarify_worker.py"):
            src = (_NOTARY / rel).read_text(encoding="utf-8")
            i = src.find("review_and_flag_protocol_file(")
            self.assertNotEqual(i, -1, f"{rel}: вызов не найден")
            # rewrite=True в пределах того же вызова (до закрывающей скобки call'а).
            call_chunk = src[i:i + 600]
            self.assertIn("rewrite=True", call_chunk,
                          f"{rel}: review_and_flag_protocol_file без rewrite=True")


if __name__ == "__main__":
    unittest.main(verbosity=2)
