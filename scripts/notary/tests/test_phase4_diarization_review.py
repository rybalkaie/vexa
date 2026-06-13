"""Тесты Ф4 (план `2026-06-13-umnyi-protokol-assemblyai.md`) — консервативная
LLM-ревизия диаризации поверх встроенного деления AssemblyAI.

Покрывает REQ D2/D3/D4:
  - D2  ревизия ПРАВИТ только однозначные ошибки деления по спикерам
        (ответ под спрашивавшим / диалог двух людей внутри одного спикера) через
        детерминированную re-attribution реплики в транскрипте; на чистом входе —
        0 перестановок. Двунаправленное доказательство:
        слипший вход → правка; чистый (бенч 1123, 3 чётких спикера) → не тронут.
  - D3  сомнительное деление — видимая пометка «⚠️ спикер под вопросом» в ПРОТОКОЛЕ,
        реплики остаются на местах (никакой молчаливой перестановки).
  - D4  приватность: лог ревизии несёт только счётчики (N правок / N пометок),
        без текста реплик и имён; ОДИН claude-вызов (секция в существующем
        review_protocol, не новый проход).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase4_diarization_review -v
"""
from __future__ import annotations

import json
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
# минимальные стабы, если пакет реально отсутствует (приём из других тестов Ф4).
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib import llm_postprocess as lp  # noqa: E402


# Слипший вход: два человека под ОДНИМ «Спикер 1» (он и спрашивает, и отвечает —
# явный диалог двух людей). Ответы должны уехать на «Спикер 2».
TRANSCRIPT_SLIPPED = (
    "#транскрипт 2026-06-13\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "**[00:00] Спикер 1:** Михаил, как у нас с бюджетом на квартал?\n\n"
    "**[00:08] Спикер 1:** Бюджет закрыли на девяносто процентов.\n\n"
    "**[00:15] Спикер 1:** А что по складу осталось?\n\n"
    "**[00:22] Спикер 1:** Палеты вывезем на неделе.\n"
)

# Чистый вход по образцу бенча 1123 — 3 чётких спикера, корректное деление.
TRANSCRIPT_CLEAN = (
    "#транскрипт 2026-06-13\n\n"
    "**Участники:** Илья, Михаил, Ольга\n\n"
    "---\n\n"
    "**[00:00] Илья:** Начнём с финансов, Михаил.\n\n"
    "**[00:09] Михаил:** Оборот за месяц два и восемь.\n\n"
    "**[00:18] Ольга:** По кадрам ищем кладовщика.\n\n"
    "**[00:25] Илья:** Хорошо, фиксируем.\n"
)


# ==========================================================================
# Секция "diarization" в system-prompt (ОДИН проход, расширение checks=)
# ==========================================================================
class TestDiarizationSectionPrompt(unittest.TestCase):

    def test_section_added_when_requested(self):
        sp = lp._build_review_system_prompt(("values", "roles", "memory", "diarization"))
        self.assertIn('"diarization"', sp)
        self.assertIn("деления по спикерам", sp)
        self.assertIn("verdict", sp)
        self.assertIn("speaker_to", sp)
        # Это ОДИН проход: прочие секции на месте (не отдельный вызов).
        self.assertIn("число", sp)      # values
        self.assertIn("смешан", sp)     # roles

    def test_section_absent_without_check(self):
        sp = lp._build_review_system_prompt(("values", "roles"))
        self.assertNotIn("деления по спикерам", sp)
        # verdict/speaker_to не навязываются прочим секциям.
        self.assertNotIn("speaker_to", sp)


# ==========================================================================
# Парсер: verdict (fix|flag, дефолт flag) + speaker_to
# ==========================================================================
class TestParseDiarization(unittest.TestCase):

    def test_fix_carries_verdict_and_speaker_to(self):
        raw = json.dumps({"findings": [{
            "section": "diarization", "quote": "Бюджет закрыли на девяносто",
            "note": "ответ под спрашивавшим", "verdict": "fix", "speaker_to": "Спикер 2",
        }]})
        out = lp._parse_review_response(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["verdict"], "fix")
        self.assertEqual(out[0]["speaker_to"], "Спикер 2")

    def test_missing_verdict_defaults_to_flag(self):
        raw = json.dumps({"findings": [{
            "section": "diarization", "quote": "что-то", "note": "сомнительно",
        }]})
        out = lp._parse_review_response(raw)
        self.assertEqual(out[0]["verdict"], "flag")
        self.assertEqual(out[0]["speaker_to"], "")

    def test_fix_without_speaker_to_downgrades_to_flag(self):
        raw = json.dumps({"findings": [{
            "section": "diarization", "quote": "что-то", "note": "сомнительно",
            "verdict": "fix",  # speaker_to нет → нельзя править → flag
        }]})
        out = lp._parse_review_response(raw)
        self.assertEqual(out[0]["verdict"], "flag")
        self.assertEqual(out[0]["speaker_to"], "")

    def test_other_sections_have_no_verdict(self):
        raw = json.dumps({"findings": [
            {"section": "values", "quote": "28 млн", "note": "сверь"},
        ]})
        out = lp._parse_review_response(raw)
        self.assertNotIn("verdict", out[0])
        self.assertNotIn("speaker_to", out[0])


# ==========================================================================
# D2 — apply_diarization_fixes: слипший → правит; чистый → 0 перестановок
# ==========================================================================
class TestApplyDiarizationFixes(unittest.TestCase):

    def test_slipped_answer_reattributed(self):
        """🔴 D2 (←): ответ под спрашивавшим уезжает на второго спикера."""
        findings = [{
            "section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
            "quote": "Бюджет закрыли на девяносто процентов", "note": "ответ под спрашивавшим",
        }]
        out, n = lp.apply_diarization_fixes(TRANSCRIPT_SLIPPED, findings)
        self.assertEqual(n, 1)
        self.assertIn("**[00:08] Спикер 2:** Бюджет закрыли на девяносто процентов.", out)
        # Реплики-вопросы того же исходного спикера НЕ задеты (реплика-уровневая правка).
        self.assertIn("**[00:00] Спикер 1:** Михаил, как у нас с бюджетом", out)
        self.assertIn("**[00:15] Спикер 1:** А что по складу осталось?", out)

    def test_two_answers_split_to_second_speaker(self):
        """Оба ответа слипшего диалога уходят на Спикер 2 (две реплика-уровневые правки)."""
        findings = [
            {"section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
             "quote": "Бюджет закрыли на девяносто процентов", "note": "ответ"},
            {"section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
             "quote": "Палеты вывезем на неделе", "note": "ответ"},
        ]
        out, n = lp.apply_diarization_fixes(TRANSCRIPT_SLIPPED, findings)
        self.assertEqual(n, 2)
        self.assertIn("**[00:08] Спикер 2:** Бюджет закрыли", out)
        self.assertIn("**[00:22] Спикер 2:** Палеты вывезем на неделе.", out)

    def test_clean_input_zero_reshuffle_no_findings(self):
        """🔴 D2 (→): чистый вход (бенч 1123) без находок → транскрипт не тронут."""
        out, n = lp.apply_diarization_fixes(TRANSCRIPT_CLEAN, [])
        self.assertEqual(n, 0)
        self.assertEqual(out, TRANSCRIPT_CLEAN)

    def test_clean_input_zero_reshuffle_with_nondiar_findings(self):
        """Даже когда ревизор нашёл values/roles на чистом входе — деление не трогается."""
        findings = [
            {"section": "values", "quote": "два и восемь", "note": "сверь число"},
            {"section": "roles", "quote": "Илья", "note": "смешаны зоны"},
        ]
        out, n = lp.apply_diarization_fixes(TRANSCRIPT_CLEAN, findings)
        self.assertEqual(n, 0)
        self.assertEqual(out, TRANSCRIPT_CLEAN)

    def test_flag_verdict_never_reshuffles(self):
        """🔴 D3: сомнительное (flag) НЕ переставляет реплики, даже со speaker_to."""
        findings = [{
            "section": "diarization", "verdict": "flag", "speaker_to": "Спикер 2",
            "quote": "Бюджет закрыли на девяносто процентов", "note": "под вопросом",
        }]
        out, n = lp.apply_diarization_fixes(TRANSCRIPT_SLIPPED, findings)
        self.assertEqual(n, 0)
        self.assertEqual(out, TRANSCRIPT_SLIPPED)

    def test_ambiguous_quote_skipped(self):
        """Quote совпал с ≥2 репликами → неоднозначно → НЕ правим (консервативно)."""
        transcript = (
            "**[00:00] Спикер 1:** Понял.\n\n"
            "**[00:05] Спикер 1:** Хорошо.\n\n"
            "**[00:10] Спикер 1:** Понял.\n"
        )
        findings = [{
            "section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
            "quote": "Понял", "note": "ответ",
        }]
        out, n = lp.apply_diarization_fixes(transcript, findings)
        self.assertEqual(n, 0)
        self.assertEqual(out, transcript)

    def test_quote_not_found_skipped(self):
        findings = [{
            "section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
            "quote": "такой реплики в транскрипте нет", "note": "ответ",
        }]
        out, n = lp.apply_diarization_fixes(TRANSCRIPT_SLIPPED, findings)
        self.assertEqual(n, 0)
        self.assertEqual(out, TRANSCRIPT_SLIPPED)

    def test_idempotent(self):
        """Повторный проход с теми же fix-находками → метка уже та → 0 правок."""
        findings = [{
            "section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
            "quote": "Бюджет закрыли на девяносто процентов", "note": "ответ",
        }]
        once, n1 = lp.apply_diarization_fixes(TRANSCRIPT_SLIPPED, findings)
        twice, n2 = lp.apply_diarization_fixes(once, findings)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)
        self.assertEqual(twice, once)

    def test_swap_safe_two_lines(self):
        """Своп авторства двух РАЗНЫХ строк применяется построчно, не схлопывается."""
        transcript = (
            "**[00:00] Илья:** Готово по складу?\n\n"
            "**[00:05] Михаил:** Да, закрыли.\n"
        )
        findings = [
            {"section": "diarization", "verdict": "fix", "speaker_to": "Михаил",
             "quote": "Готово по складу", "note": "вопрос второго"},
            {"section": "diarization", "verdict": "fix", "speaker_to": "Илья",
             "quote": "Да, закрыли", "note": "ответ первого"},
        ]
        out, n = lp.apply_diarization_fixes(transcript, findings)
        self.assertEqual(n, 2)
        self.assertIn("**[00:00] Михаил:** Готово по складу?", out)
        self.assertIn("**[00:05] Илья:** Да, закрыли.", out)

    def test_header_line_never_touched(self):
        """Строка-шапка `**Участники:**` не реплика → правка её не задевает."""
        findings = [{
            "section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
            "quote": "Бюджет закрыли на девяносто процентов", "note": "ответ",
        }]
        out, _ = lp.apply_diarization_fixes(TRANSCRIPT_SLIPPED, findings)
        self.assertIn("**Участники:** Илья, Михаил", out)

    def test_junk_speaker_to_rejected(self):
        """speaker_to с переносом/markdown/двоеточием/длинный → не вписываем как метку."""
        for junk in ["**Илья**", "Спикер: 2", "x" * 50, "две\nстроки", ""]:
            findings = [{
                "section": "diarization", "verdict": "fix", "speaker_to": junk,
                "quote": "Бюджет закрыли на девяносто процентов", "note": "ответ",
            }]
            out, n = lp.apply_diarization_fixes(TRANSCRIPT_SLIPPED, findings)
            self.assertEqual(n, 0, f"мусорный speaker_to не должен править: {junk!r}")
            self.assertEqual(out, TRANSCRIPT_SLIPPED)

    def test_speaker_to_equals_current_noop(self):
        findings = [{
            "section": "diarization", "verdict": "fix", "speaker_to": "Спикер 1",
            "quote": "Бюджет закрыли на девяносто процентов", "note": "ответ",
        }]
        out, n = lp.apply_diarization_fixes(TRANSCRIPT_SLIPPED, findings)
        self.assertEqual(n, 0)
        self.assertEqual(out, TRANSCRIPT_SLIPPED)


class TestSpeakerToSane(unittest.TestCase):
    def test_accepts_labels_and_names(self):
        for ok in ["Спикер 2", "Илья", "Михаил Еремеев", "Дарья Набережная"]:
            self.assertTrue(lp._speaker_to_is_sane(ok), ok)

    def test_rejects_junk(self):
        for bad in ["", "  ", "**жирный**", "имя: с двоеточием", "a" * 41, "пере\nнос"]:
            self.assertFalse(lp._speaker_to_is_sane(bad), bad)


# ==========================================================================
# D3 — diarization flag рендерится в ПРОТОКОЛ; fix туда НЕ идёт
# ==========================================================================
class TestDiarizationFlagInProtocol(unittest.TestCase):
    PROTOCOL = (
        "#протоколвстречи 13.06.2026\n\n"
        "**Участники:** Илья, Михаил\n\n"
        "---\n\n"
        "## 1) Бюджет\n\n▪️ Закрыли на 90%.\n"
    )

    def test_flag_goes_to_tail_block(self):
        findings = [{
            "section": "diarization", "verdict": "flag",
            "quote": "Палеты вывезем на неделе", "note": "под вопросом деление",
        }]
        out = lp.apply_review_flags(self.PROTOCOL, findings)
        self.assertIn("## ⚠️ Проверить", out)
        self.assertIn("⚠️ спикер под вопросом", out)
        self.assertIn("Палеты вывезем на неделе", out)
        # Тело протокола (буллет) не тронуто — реплики на местах (D3).
        self.assertIn("▪️ Закрыли на 90%.", out)

    def test_fix_not_rendered_in_protocol(self):
        """fix-находки уходят в транскрипт, в протоколе их быть не должно."""
        findings = [{
            "section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
            "quote": "Палеты вывезем на неделе", "note": "ответ под спрашивавшим",
        }]
        out = lp.apply_review_flags(self.PROTOCOL, findings)
        self.assertEqual(out, self.PROTOCOL)  # ничего не вписано
        self.assertNotIn("⚠️", out)

    def test_flag_idempotent(self):
        findings = [{
            "section": "diarization", "verdict": "flag",
            "quote": "Палеты вывезем", "note": "под вопросом",
        }]
        once = lp.apply_review_flags(self.PROTOCOL, findings)
        twice = lp.apply_review_flags(once, findings)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("## ⚠️ Проверить"), 1)


# ==========================================================================
# D4 + достижимость из реального триггера (finalize → review_and_flag_…)
# ==========================================================================
class TestReviewWiringAndPrivacy(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-06-13.md"
        self.protocol = d / "2026-06-13-protokol.md"
        self.transcript.write_text(TRANSCRIPT_SLIPPED, encoding="utf-8")
        self.protocol.write_text(
            "#протоколвстречи 13.06.2026\n\n**Участники:** Илья, Михаил\n\n"
            "---\n\n## 1) Бюджет\n\n▪️ Закрыли на 90%.\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_fix_written_to_transcript_flag_to_protocol(self):
        """Реальный путь обёртки: fix → диск транскрипта; flag → протокол."""
        findings = [
            {"section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
             "quote": "Бюджет закрыли на девяносто процентов", "note": "ответ"},
            {"section": "diarization", "verdict": "flag",
             "quote": "Палеты вывезем на неделе", "note": "под вопросом"},
        ]
        with mock.patch.object(lp, "review_protocol", return_value=findings):
            n = lp.review_and_flag_protocol_file(
                self.protocol, self.transcript, meeting_sid="sid",
                checks=("values", "roles", "memory", "diarization"),
            )
        self.assertEqual(n, 2)  # 1 правка транскрипта + 1 пометка протокола
        on_disk_tr = self.transcript.read_text(encoding="utf-8")
        self.assertIn("**[00:08] Спикер 2:** Бюджет закрыли", on_disk_tr)
        on_disk_pr = self.protocol.read_text(encoding="utf-8")
        self.assertIn("⚠️ спикер под вопросом", on_disk_pr)
        self.assertIn("Палеты вывезем на неделе", on_disk_pr)

    def test_clean_input_no_writes(self):
        """Нет находок → ни транскрипт, ни протокол не переписаны."""
        self.transcript.write_text(TRANSCRIPT_CLEAN, encoding="utf-8")
        tr_before = self.transcript.read_text(encoding="utf-8")
        pr_before = self.protocol.read_text(encoding="utf-8")
        with mock.patch.object(lp, "review_protocol", return_value=[]):
            n = lp.review_and_flag_protocol_file(
                self.protocol, self.transcript, meeting_sid="sid",
                checks=("values", "roles", "memory", "diarization"),
            )
        self.assertEqual(n, 0)
        self.assertEqual(self.transcript.read_text(encoding="utf-8"), tr_before)
        self.assertEqual(self.protocol.read_text(encoding="utf-8"), pr_before)

    def test_log_carries_only_counters_no_replica_text(self):
        """🔴 D4: лог ревизии — только счётчики, без текста реплик/имён."""
        secret_reply = "Бюджет закрыли на девяносто процентов"
        findings = [
            {"section": "diarization", "verdict": "fix", "speaker_to": "Спикер 2",
             "quote": secret_reply, "note": "ответ"},
            {"section": "diarization", "verdict": "flag",
             "quote": "Палеты вывезем на неделе", "note": "под вопросом"},
        ]
        with mock.patch.object(lp, "review_protocol", return_value=findings):
            with self.assertLogs("lib.llm_postprocess", level="INFO") as cm:
                lp.review_and_flag_protocol_file(
                    self.protocol, self.transcript, meeting_sid="sid",
                    checks=("values", "roles", "memory", "diarization"),
                )
        joined = "\n".join(cm.output)
        # Счётчики есть…
        self.assertIn("diarization-fixes=1", joined)
        self.assertIn("protocol-flags=1", joined)
        # …а текста реплик/имён-кандидатов в логе НЕТ.
        self.assertNotIn(secret_reply, joined)
        self.assertNotIn("Палеты вывезем", joined)
        self.assertNotIn("Спикер 2", joined)

    def test_single_claude_call_includes_diarization(self):
        """Один claude-вызов, в checks которого есть diarization (не новый проход)."""
        fake = json.dumps({"findings": []})
        with mock.patch.object(lp, "call_claude_print", return_value=fake) as m:
            out = lp.review_protocol(
                "протокол", TRANSCRIPT_CLEAN,
                checks=("values", "roles", "memory", "diarization"), meeting_sid="x",
            )
        self.assertEqual(m.call_count, 1)
        self.assertEqual(out, [])
        system = m.call_args.kwargs.get("system", "")
        self.assertIn("деления по спикерам", system)


if __name__ == "__main__":
    unittest.main(verbosity=2)
