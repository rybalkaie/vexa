"""Тесты Ф4б — детерминированный remap авторства реплаем + память серии.

Покрывает REQ 1.2 / 1.4 плана `2026-06-06-dorabotki-notary-pre-live.md`:
  - 1.4  Правка авторства реплаем → перевыпуск с верным авторством через
         ДЕТЕРМИНИРОВАННЫЙ remap спикер→имя ДО регенерации (НЕ LLM-правкой-данными).
         Своп-безопасно (Илья↔Михаил). Авторская правка не уходит в LLM-блок и не
         отравляет term/meaning-обучение. Ретрай-безопасно (remap коммитится на диск
         только при доставке).
  - 1.2  Исправленное сопоставление спикер→имя живёт на серию: `speaker_mapping` в
         `build_digest` → `resolve_speaker_anchor` → якорь в `map_all` через
         валидацию строгим vocative (человеческое закрепление бьёт догадку Ф4а;
         Ф4а — фолбэк). Ленивое поле (нет ключа → пусто, миграции нет, УПУ3).
  - регресс Ф4а: `map_all` БЕЗ якоря — догадка `_resolve_two_speakers` для 2
         спикеров не инвертируется (директорат 03.06).

⚠️ Палиатив (РИСК-диаризация): метки S1/S2 нестабильны между джобами. Тесты
переноса на серию используют согласованные метки в фикстуре — это доказывает
ПРОВОДКУ; в бою без голос. отпечатков перенос остаётся паллиативом, и якорь
применяется с защитой строгим vocative (не возвращает уверенную инверсию).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase4b_diarization_remap -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

# name_mapping → align → diarize/transcribe тянут тяжёлые пакеты (requests/httpx/…),
# которых нет в системном python мака/CI. Тестируемая логика их не использует —
# подкладываем минимальные стабы, если пакет реально отсутствует.
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib.align import AlignedTurn  # noqa: E402
from lib import name_mapping as nm  # noqa: E402
from lib import series_memory as sm  # noqa: E402
from lib import feedback_reissue as fr  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402


def _turn(speaker: str, text: str) -> AlignedTurn:
    return AlignedTurn(start=0.0, end=1.0, speaker=speaker, text=text)


# Транскрипт с ИНВЕРТИРОВАННОЙ догадкой авторства (как после ошибки Ф4а): человек
# видит протокол, отвечает «это не Илья, а Михаил». Формат тела — ровно render.py.
TRANSCRIPT_INVERTED = (
    "#транскрипт 2026-06-03\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "**[00:00] Илья:** Начнём со склада.\n\n"
    "**[00:12] Михаил:** Палетное хранение занято на треть.\n\n"
    "**[00:20] Илья:** Тогда вывозим на неделе.\n"
)


# ==========================================================================
# REQ 1.4 — своп-безопасный remap транскрипта (чистая функция)
# ==========================================================================
class TestRemapTranscriptSpeakers(unittest.TestCase):
    def test_swap_is_atomic_not_collapsed(self):
        """Своп Илья↔Михаил применяется одним проходом — не схлопывается в одно имя."""
        out = lp.remap_transcript_speakers(
            TRANSCRIPT_INVERTED, {"Илья": "Михаил", "Михаил": "Илья"}
        )
        self.assertIn("**[00:00] Михаил:** Начнём со склада.", out)
        self.assertIn("**[00:12] Илья:** Палетное хранение занято на треть.", out)
        self.assertIn("**[00:20] Михаил:** Тогда вывозим на неделе.", out)
        # Шапка «**Участники:** Илья, Михаил» НЕ трогается (не строка реплики).
        self.assertIn("**Участники:** Илья, Михаил", out)

    def test_label_assignment(self):
        text = "**[00:00] Спикер 2:** Реплика.\n"
        out = lp.remap_transcript_speakers(text, {"Спикер 2": "Дарья Набережная"})
        self.assertIn("**[00:00] Дарья Набережная:** Реплика.", out)

    def test_swap_handles_hms_timecode(self):
        """Часовая встреча (директорат >1ч): таймкод HH:MM:SS тоже своп-безопасно
        ремапится — регекс рендера `\\[\\d{2}:\\d{2}(?::\\d{2})?\\]` покрывает оба формата."""
        text = (
            "**[00:00] Илья:** Старт.\n\n"
            "**[01:05:30] Михаил:** Через час с лишним.\n"
        )
        out = lp.remap_transcript_speakers(text, {"Илья": "Михаил", "Михаил": "Илья"})
        self.assertIn("**[00:00] Михаил:** Старт.", out)
        self.assertIn("**[01:05:30] Илья:** Через час с лишним.", out)

    def test_unmatched_label_untouched(self):
        out = lp.remap_transcript_speakers(TRANSCRIPT_INVERTED, {"Ольга": "Дарья"})
        self.assertEqual(out, TRANSCRIPT_INVERTED)

    def test_empty_remap_noop(self):
        self.assertEqual(lp.remap_transcript_speakers(TRANSCRIPT_INVERTED, {}), TRANSCRIPT_INVERTED)


# ==========================================================================
# REQ 1.4 — парсер правок авторства (детект + remap)
# ==========================================================================
class TestParseAuthorshipRemap(unittest.TestCase):
    CURRENT = ["Илья", "Михаил"]
    POOL = ["Илья Рыбалка", "Михаил Еремеев"]

    def _edit(self, text):
        return [{"author": "Илья", "text": text}]

    def test_negation_infers_swap_for_two_speakers(self):
        """«это не Илья, а Михаил» при двух отображаемых → своп их меток."""
        remap, idx = fr.parse_authorship_remap(
            self._edit("это не Илья, а Михаил"), self.CURRENT, self.POOL
        )
        self.assertEqual(remap, {"Илья": "Михаил", "Михаил": "Илья"})
        self.assertEqual(idx, {0})

    def test_negation_without_comma(self):
        remap, _ = fr.parse_authorship_remap(
            self._edit("не Илья а Михаил"), self.CURRENT, self.POOL
        )
        self.assertEqual(remap, {"Илья": "Михаил", "Михаил": "Илья"})

    def test_label_assignment_new_name(self):
        """«Спикер 2 = Дарья» — присвоение нового имени (не своп)."""
        remap, idx = fr.parse_authorship_remap(
            self._edit("Спикер 2 = Дарья"),
            ["Илья", "Спикер 2"], ["Илья Рыбалка", "Дарья Набережная"],
        )
        self.assertEqual(remap, {"Спикер 2": "Дарья Набережная"})
        self.assertEqual(idx, {0})

    def test_swap_keyword(self):
        remap, _ = fr.parse_authorship_remap(
            self._edit("перепутаны Илья и Михаил"), self.CURRENT, self.POOL
        )
        self.assertEqual(remap, {"Илья": "Михаил", "Михаил": "Илья"})

    def test_arrow_form(self):
        remap, _ = fr.parse_authorship_remap(
            self._edit("Илья -> Михаил"), self.CURRENT, self.POOL
        )
        self.assertEqual(remap, {"Илья": "Михаил", "Михаил": "Илья"})

    def test_content_edit_not_authorship(self):
        """Контентная правка («131 на доставке») НЕ распознаётся как авторская."""
        remap, idx = fr.parse_authorship_remap(
            self._edit("оборот был 131 на доставке, а не 3"), self.CURRENT, self.POOL
        )
        self.assertEqual(remap, {})
        self.assertEqual(idx, set())

    def test_unresolvable_name_falls_through(self):
        """Имя не из состава встречи → не авторская правка (уходит в LLM)."""
        remap, idx = fr.parse_authorship_remap(
            self._edit("это не Анжи, а Анжела"), self.CURRENT, self.POOL
        )
        self.assertEqual(remap, {})
        self.assertEqual(idx, set())

    def test_no_current_speakers_no_remap(self):
        remap, idx = fr.parse_authorship_remap(self._edit("не Илья, а Михаил"), [], self.POOL)
        self.assertEqual((remap, idx), ({}, set()))

    def test_mixed_edits_split(self):
        """Авторская + контентная в одном раунде: авторская в remap, контентная — нет."""
        edits = [
            {"author": "Илья", "text": "это не Илья, а Михаил"},
            {"author": "Илья", "text": "добавь пункт про КТК"},
        ]
        remap, idx = fr.parse_authorship_remap(edits, self.CURRENT, self.POOL)
        self.assertEqual(remap, {"Илья": "Михаил", "Михаил": "Илья"})
        self.assertEqual(idx, {0})

    def test_extract_current_speakers(self):
        got = fr.extract_current_speakers(TRANSCRIPT_INVERTED)
        self.assertEqual(got, ["Илья", "Михаил"])


# ==========================================================================
# REQ 1.2 — память серии: speaker_mapping (build/resolve/update), ленивость
# ==========================================================================
class TestSeriesSpeakerMapping(unittest.TestCase):
    PROTO = (
        "#протоколвстречи 03.06.2026\n\n"
        "**Участники:** Илья, Михаил\n\n"
        "## 1) Склад\n\n▪️ Палетное занято.\n"
    )

    def test_build_digest_stores_mapping(self):
        d = sm.build_digest(
            self.PROTO, {"series": "dir", "date": "2026-06-03"},
            speaker_mapping={"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"},
        )
        self.assertEqual(d["speaker_mapping"], {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"})

    def test_build_digest_lazy_when_absent(self):
        """Нет speaker_mapping → ключа в выжимке НЕТ (ленивое поле, УПУ3)."""
        d = sm.build_digest(self.PROTO, {"series": "dir", "date": "2026-06-03"})
        self.assertNotIn("speaker_mapping", d)
        d2 = sm.build_digest(self.PROTO, {"series": "dir", "date": "2026-06-03"}, speaker_mapping={})
        self.assertNotIn("speaker_mapping", d2)

    def test_resolve_speaker_anchor_latest_nonempty(self):
        digests = [
            {"date": "2026-06-01", "speaker_mapping": {"SPEAKER_00": "Илья"}},
            {"date": "2026-06-02"},  # без ключа — пропускаем
            {"date": "2026-06-03", "speaker_mapping": {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"}},
        ]
        self.assertEqual(
            sm.resolve_speaker_anchor(digests),
            {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"},
        )

    def test_resolve_speaker_anchor_empty_when_none(self):
        self.assertEqual(sm.resolve_speaker_anchor([]), {})
        self.assertEqual(sm.resolve_speaker_anchor([{"date": "2026-06-01"}]), {})

    def test_update_digest_speaker_mapping_applies_remap(self):
        with tempfile.TemporaryDirectory() as td:
            series_dir = Path(td)
            d = sm.build_digest(
                self.PROTO, {"series": "dir", "date": "2026-06-03"},
                speaker_mapping={"SPEAKER_00": "Илья", "SPEAKER_01": "Михаил"},
            )
            sm.save_digest(series_dir, "2026-06-03", d)
            changed = sm.update_digest_speaker_mapping(
                series_dir, "2026-06-03", {"Илья": "Михаил", "Михаил": "Илья"}
            )
            self.assertTrue(changed)
            reloaded = sm.load_digest(sm.digest_path(series_dir, "2026-06-03"))
            self.assertEqual(
                reloaded["speaker_mapping"], {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"}
            )

    def test_update_digest_lazy_noop_without_key(self):
        """Старая выжимка без speaker_mapping → no-op, без падения, миграции нет."""
        with tempfile.TemporaryDirectory() as td:
            series_dir = Path(td)
            d = sm.build_digest(self.PROTO, {"series": "dir", "date": "2026-06-03"})
            sm.save_digest(series_dir, "2026-06-03", d)
            changed = sm.update_digest_speaker_mapping(
                series_dir, "2026-06-03", {"Илья": "Михаил"}
            )
            self.assertFalse(changed)
            reloaded = sm.load_digest(sm.digest_path(series_dir, "2026-06-03"))
            self.assertNotIn("speaker_mapping", reloaded)

    def test_update_digest_noop_when_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(
                sm.update_digest_speaker_mapping(Path(td), "2026-06-03", {"Илья": "Михаил"})
            )


# ==========================================================================
# REQ 1.2 — якорь серии в map_all (бьёт догадку, валидируется vocative)
# ==========================================================================
class TestMapAllSeriesAnchor(unittest.TestCase):
    PARTS = ["Илья", "Михаил"]

    def _silent_turns(self):
        # Реплики БЕЗ обращений по имени → Source 2 деферит (как у Ф4а на 2 спикерах
        # без строгого vocative). Именно тут якорь серии помогает.
        return [
            _turn("SPEAKER_00", "Начнём со склада."),
            _turn("SPEAKER_01", "Палетное хранение занято."),
            _turn("SPEAKER_00", "Вывозим на неделе."),
        ]

    def test_anchor_fills_silent_two_speaker(self):
        """Нет своего сигнала → якорь серии заполняет авторство (бьёт пустую догадку)."""
        turns = self._silent_turns()
        # Без якоря — догадка деферит (всё unresolved).
        base = nm.map_all(turns, self.PARTS)
        self.assertEqual(base.cluster_to_name, {})
        # С якорем — применяется.
        anchor = {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"}
        res = nm.map_all(turns, self.PARTS, anchor=anchor)
        self.assertEqual(res.cluster_to_name, {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"})
        self.assertIn("series_anchor", res.sources_used)

    def test_anchor_dropped_when_contradicted_by_strict_vocative(self):
        """🔴 Защита от инверсии: строгий vocative текущей встречи опровергает якорь.

        SPEAKER_00 сам окликает «Михаил, …» → SPEAKER_00 точно НЕ Михаил. Якорь
        SPEAKER_00→Михаил ОТБРАСЫВАЕТСЯ, текущая улика побеждает (Ф4а-фолбэк даёт
        верное SPEAKER_00=Илья). Перенос не возвращает уверенную инверсию.
        """
        turns = [
            _turn("SPEAKER_00", "Михаил, давай по складу."),
            _turn("SPEAKER_01", "Палетное занято."),
            _turn("SPEAKER_00", "Хорошо."),
            _turn("SPEAKER_01", "Закрою КТК."),
        ]
        anchor = {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"}  # противоречит улике
        res = nm.map_all(turns, self.PARTS, anchor=anchor)
        self.assertNotEqual(res.cluster_to_name.get("SPEAKER_00"), "Михаил")
        # Текущая улика чинит: окликнувший Михаила (S0) — это Илья.
        self.assertEqual(res.cluster_to_name.get("SPEAKER_00"), "Илья")

    def test_anchor_name_not_in_participants_ignored(self):
        turns = self._silent_turns()
        res = nm.map_all(turns, self.PARTS, anchor={"SPEAKER_00": "Дарья"})
        self.assertNotIn("Дарья", res.cluster_to_name.values())

    def test_partial_anchor_plus_source1_fallback(self):
        """Якорь закрепил один cluster → второй свободный добивается Source 1 (1↔1)."""
        turns = self._silent_turns()
        res = nm.map_all(turns, self.PARTS, anchor={"SPEAKER_00": "Илья"})
        self.assertEqual(res.cluster_to_name.get("SPEAKER_00"), "Илья")
        self.assertEqual(res.cluster_to_name.get("SPEAKER_01"), "Михаил")

    def test_carry_forward_chain_end_to_end(self):
        """REQ 1.2 проводка: выжимка с правленым авторством → якорь → следующая верна сама.

        (Метки согласованы в фикстуре — палиатив без голос. отпечатков, см. шапку.)
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            series_dir = root / "dir"
            series_dir.mkdir()
            proto = "#протоколвстречи\n\n**Участники:** Илья, Михаил\n\n## 1) Тема\n\n▪️ Пункт.\n"
            dig = sm.build_digest(
                proto, {"series": "dir", "date": "2026-06-03"},
                speaker_mapping={"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"},
            )
            sm.save_digest(series_dir, "2026-06-03", dig)
            # Следующая встреча серии резолвит память → якорь.
            digests = sm.resolve_memory(series_dir, root, current_participants=self.PARTS,
                                        current_date="2026-06-10")
            anchor = sm.resolve_speaker_anchor(digests)
            self.assertEqual(anchor, {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"})
            res = nm.map_all(self._silent_turns(), self.PARTS, anchor=anchor)
            self.assertEqual(res.cluster_to_name, {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"})


# ==========================================================================
# Регресс Ф4а — догадка 2 спикеров без якоря не инвертируется
# ==========================================================================
class TestPhase4aRegressionViaMapAll(unittest.TestCase):
    PARTS = ["Илья", "Михаил"]

    def test_directorate_not_inverted_without_anchor(self):
        """map_all БЕЗ якоря: директорат 03.06 → SPEAKER_00=Илья (не инверсия)."""
        turns = [
            _turn("SPEAKER_00", "Михаил, давай начнём с финансов."),
            _turn("SPEAKER_01", "Михаил посчитал оборот."),
            _turn("SPEAKER_00", "Хорошо."),
            _turn("SPEAKER_01", "Михаил свёл по складу."),
            _turn("SPEAKER_00", "Понял."),
            _turn("SPEAKER_01", "Михаил закроет КТК."),
            _turn("SPEAKER_00", "Отлично, спасибо."),
        ]
        res = nm.map_all(turns, self.PARTS)
        self.assertEqual(res.cluster_to_name.get("SPEAKER_00"), "Илья")
        self.assertEqual(res.cluster_to_name.get("SPEAKER_01"), "Михаил")
        self.assertNotIn("series_anchor", res.sources_used)

    def test_no_anchor_is_phase4a_behavior(self):
        """anchor=None ≡ поведение Ф4а (тот же результат, что прямой Source 2)."""
        turns = [
            _turn("SPEAKER_00", "Михаил, посмотри цифры."),
            _turn("SPEAKER_01", "Сейчас."),
        ]
        a = nm.map_all(turns, self.PARTS).cluster_to_name
        b = nm.map_all(turns, self.PARTS, anchor=None).cluster_to_name
        self.assertEqual(a, b)


# ==========================================================================
# REQ 1.4 — интеграция: перевыпуск чинит авторство детерминированно
# ==========================================================================
class TestReissueAuthorshipRemap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.series_dir = self.root / "protocols" / "dir"
        self.series_dir.mkdir(parents=True)
        self.date = "2026-06-03"
        self.transcript_path = self.series_dir / f"{self.date}.md"
        self.protocol_path = self.series_dir / f"{self.date}-protokol.md"
        self.meta_path = self.series_dir / "meta.json"
        self.transcript_path.write_text(TRANSCRIPT_INVERTED, encoding="utf-8")
        self.protocol_path.write_text(
            "#протоколвстречи 03.06.2026\n\n**Участники:** Илья, Михаил\n\n## 1) Склад\n\n▪️ Старое.\n",
            encoding="utf-8",
        )
        meta = {
            "series": "dir", "date": self.date,
            "expectedParticipants": ["Илья Рыбалка", "Михаил Еремеев"],
            "participants": ["Илья", "Михаил"],
            "delivered": [{"chat_id": -1001, "message_ids": [101], "at": "2026-06-03T11:00:00Z"}],
        }
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        # Выжимка серии с догадкой (инвертированной) — её Ф4б должен поправить.
        dig = sm.build_digest(
            self.protocol_path.read_text(encoding="utf-8"),
            {"series": "dir", "date": self.date},
            speaker_mapping={"SPEAKER_00": "Илья", "SPEAKER_01": "Михаил"},
        )
        sm.save_digest(self.series_dir, self.date, dig)

    def tearDown(self):
        self._tmp.cleanup()

    def _state(self, edits):
        return {
            "feedback_id": "fb-dir-2026-06-03--1001",
            "series": "dir", "date": self.date, "chat_id": -1001,
            "meta_path": str(self.meta_path), "protocol_message_ids": [101],
            "round": 1, "status": "reissuing", "reissue_attempts": 0,
            "edits": edits,
        }

    def test_authorship_reply_deterministic_remap(self):
        """«это не Илья, а Михаил» → генерация видит remapped транскрипт; своп на диске."""
        captured = {}

        def fake_gen(gen_path, meeting_meta, sid):
            captured["transcript"] = Path(gen_path).read_text(encoding="utf-8")
            captured["meta"] = meeting_meta
            return "#протоколвстречи 03.06.2026\n\n**Участники:** Илья, Михаил\n\n## 1) Склад\n\n▪️ Новое.\n"

        res = fr.reissue_one(
            self._state([{"author": "Илья", "text": "это не Илья, а Михаил"}]),
            root=self.root,
            generate_fn=fake_gen,
            redeliver_fn=lambda *a, **k: {"status": "sent", "message_ids": [9001]},
            save_version_fn=lambda p: p,
        )
        self.assertEqual(res["status"], "sent")
        # Генерация получила СВОПнутый транскрипт (детерминированно, до LLM).
        self.assertIn("**[00:00] Михаил:** Начнём со склада.", captured["transcript"])
        self.assertIn("**[00:12] Илья:** Палетное хранение занято на треть.", captured["transcript"])
        self.assertNotIn("**[00:00] Илья:**", captured["transcript"])
        # Авторская правка НЕ ушла в LLM-блок (контентных правок нет).
        self.assertEqual(captured["meta"].get("feedback_edits_block", ""), "")
        # На диск закоммичен remapped транскрипт (переживёт будущие перевыпуски).
        on_disk = self.transcript_path.read_text(encoding="utf-8")
        self.assertIn("**[00:00] Михаил:** Начнём со склада.", on_disk)
        self.assertIn("**[00:12] Илья:** Палетное хранение занято на треть.", on_disk)
        # Память серии понесла исправленное авторство вперёд (REQ 1.2).
        reloaded = sm.load_digest(sm.digest_path(self.series_dir, self.date))
        self.assertEqual(
            reloaded["speaker_mapping"], {"SPEAKER_00": "Михаил", "SPEAKER_01": "Илья"}
        )
        # Авторская правка НЕ попала в term/meaning learning-лог (не отравила словарь).
        log_p = fr.learning_log_path(root=self.root)
        self.assertFalse(log_p.is_file())

    def test_authorship_edit_not_in_learning_but_content_is(self):
        """Смешанный раунд: авторская — remap+digest; контентная — в LLM-блок и learning."""
        captured = {}

        def fake_gen(gen_path, meeting_meta, sid):
            captured["transcript"] = Path(gen_path).read_text(encoding="utf-8")
            captured["meta"] = meeting_meta
            return "#протоколвстречи\n\n**Участники:** Илья\n\n## 1) Склад\n\n▪️ 131 на доставке.\n"

        res = fr.reissue_one(
            self._state([
                {"author": "Илья", "text": "это не Илья, а Михаил"},
                {"author": "Илья", "text": "131 на доставке"},
            ]),
            root=self.root,
            generate_fn=fake_gen,
            redeliver_fn=lambda *a, **k: {"status": "sent", "message_ids": [9001]},
            save_version_fn=lambda p: p,
        )
        self.assertEqual(res["status"], "sent")
        # Контентная правка дошла в LLM-блок, авторская — нет.
        block = captured["meta"].get("feedback_edits_block", "")
        self.assertIn("131 на доставке", block)
        self.assertNotIn("это не Илья", block)
        # learning-лог содержит контентную, но не авторскую.
        log_p = fr.learning_log_path(root=self.root)
        self.assertTrue(log_p.is_file())
        rec = json.loads(log_p.read_text(encoding="utf-8").strip().splitlines()[-1])
        texts = " ".join(e["text"] for e in rec["edits"])
        self.assertIn("131 на доставке", texts)
        self.assertNotIn("это не Илья", texts)

    def test_retry_safe_no_double_swap_on_delivery_failure(self):
        """Доставка упала → транскрипт НЕ тронут; ретрай применяет своп РОВНО раз."""
        gen_seen = []

        def fake_gen(gen_path, meeting_meta, sid):
            gen_seen.append(Path(gen_path).read_text(encoding="utf-8"))
            return "#протоколвстречи\n\n**Участники:** Илья, Михаил\n\n## 1) Т\n\n▪️ Новое.\n"

        st = self._state([{"author": "Илья", "text": "это не Илья, а Михаил"}])
        # 1-й проход: доставка не прошла (not-delivered-yet) → диск не трогаем.
        res1 = fr.reissue_one(
            st, root=self.root, generate_fn=fake_gen,
            redeliver_fn=lambda *a, **k: {"status": "not-delivered-yet"},
            save_version_fn=lambda p: p,
        )
        self.assertEqual(res1["status"], "not-delivered-yet")
        self.assertEqual(self.transcript_path.read_text(encoding="utf-8"), TRANSCRIPT_INVERTED)
        # 2-й проход (ретрай): доставка прошла → своп применён ОДИН раз (из оригинала).
        res2 = fr.reissue_one(
            st, root=self.root, generate_fn=fake_gen,
            redeliver_fn=lambda *a, **k: {"status": "sent", "message_ids": [1]},
            save_version_fn=lambda p: p,
        )
        self.assertEqual(res2["status"], "sent")
        on_disk = self.transcript_path.read_text(encoding="utf-8")
        self.assertIn("**[00:00] Михаил:** Начнём со склада.", on_disk)
        self.assertIn("**[00:12] Илья:** Палетное хранение занято на треть.", on_disk)
        # Оба генератора видели один и тот же свопнутый вход (детерминизм, не двойной своп).
        self.assertEqual(gen_seen[0], gen_seen[1])

    def test_no_remap_tmp_left_behind(self):
        """Временный remap-файл не остаётся в папке серии после перевыпуска."""
        fr.reissue_one(
            self._state([{"author": "Илья", "text": "это не Илья, а Михаил"}]),
            root=self.root,
            generate_fn=lambda gp, mm, sid: "#протоколвстречи\n\n## 1) Т\n\n▪️ Новое.\n",
            redeliver_fn=lambda *a, **k: {"status": "sent", "message_ids": [1]},
            save_version_fn=lambda p: p,
        )
        leftovers = [p.name for p in self.series_dir.iterdir() if ".remap." in p.name]
        self.assertEqual(leftovers, [])

    def test_no_tmp_leak_on_write_failure(self):
        """Цикл5 Н1: запись remapped-транскрипта упала (диск/IO) → temp-файл НЕ
        остаётся в папке серии (ранний return минует finally — чистим в except)."""
        import os as _os

        orig_fdopen = fr.os.fdopen

        def boom_fdopen(fd, *a, **k):
            _os.close(fd)  # не течём реальным fd из mkstemp

            class _F:
                def __enter__(self):
                    return self

                def __exit__(self, *e):
                    return False

                def write(self, _s):
                    raise OSError("smoke: disk full на записи remap")

            return _F()

        fr.os.fdopen = boom_fdopen
        try:
            res = fr.reissue_one(
                self._state([{"author": "Илья", "text": "это не Илья, а Михаил"}]),
                root=self.root,
                generate_fn=lambda *a, **k: "x",
                redeliver_fn=lambda *a, **k: {"status": "sent", "message_ids": [1]},
                save_version_fn=lambda p: p,
            )
        finally:
            fr.os.fdopen = orig_fdopen
        self.assertEqual(res["status"], "error")
        self.assertIn("remap tmp", res["error"])
        leftovers = [p.name for p in self.series_dir.iterdir() if ".remap." in p.name]
        self.assertEqual(leftovers, [])
        # Исходный транскрипт не тронут (доставки не было).
        self.assertEqual(self.transcript_path.read_text(encoding="utf-8"), TRANSCRIPT_INVERTED)


if __name__ == "__main__":
    unittest.main()
