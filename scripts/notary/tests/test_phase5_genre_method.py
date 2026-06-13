"""Ф5 (umnyi-protokol-assemblyai) — жанр/цель серии + усиление методички.

Покрывает REQ плана `plans/2026-06-13-umnyi-protokol-assemblyai.md`, Фаза 5:
  - G4 — жанр/повестка серии: поле `genre` в watched.yaml + резолвер
         (`registry.get_genre_for_series`, зеркало company) + мягкий дефолт
         (`series_markup.genre_for_series`); блок жанра в user-промпте генерации;
         директорат ≠ координация по фокусу (доказано на сборке промпта).
  - G5 — методичка точечно усилена без поломки структуры: ключевые секции на
         месте, усиление присутствует, методичка доходит до system-промпта целиком.

Чистые dict-резолверы/валидатор тестируются БЕЗ pyyaml (инъекция `watched`).
G4-блок жанра идёт в ТОТ ЖЕ единственный вызов генерации (Вызов 1, ГРАН1/НЕС1).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase5_genre_method -v
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cli import registry  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402
from lib import series_markup  # noqa: E402  (тот же объект, что использует lp)
from lib import series_roster as sr  # noqa: E402

SLUG = sr.ANZHEE_COORDINATION_SLUG
_ALL_GENRES = ("директорат", "координация", "продукт", "1-на-1", "oneoff")

# Минимальный валидный протокол (должен начинаться с `#протоколвстречи`).
_FAKE_PROTOCOL = (
    "#протоколвстречи 09.06.2026\n\n"
    "**Встреча:** Координация.\n\n"
    "**Длительность:** 25 мин\n\n"
    "**Участники:** Мария Михина\n\n"
    "**Транскрипт:** [2026-06-09.md](2026-06-09.md)\n\n"
    "---\n\n"
    "## 1) Поставки\n\n"
    "▪️ Контейнер на таможне.\n"
)

_ROSTER_NAMES = ["Мария Михина", "Сона Енгибарян", "Михаил Саргин"]
_META = {
    "series": SLUG,
    "date": "2026-06-09",
    "duration": 25,
    "expectedParticipants": _ROSTER_NAMES,
    "participants": _ROSTER_NAMES,
}


def _watched(*recs):
    return {"watched": list(recs)}


def _rec(series, **fields):
    rec = {"id": f"{series}-evt", "series": series, "type": "manual",
           "cron": "0 11 * * 2", "tz": "Europe/Moscow"}
    rec.update(fields)
    return rec


def _capture_call():
    """side_effect для `call_claude_print`: пишет аргументы вызова и отдаёт протокол."""
    captured: dict = {}

    def _fake(user_prompt, *, system, timeout, model):
        captured["user"] = user_prompt
        captured["system"] = system
        captured["timeout"] = timeout
        captured["model"] = model
        return _FAKE_PROTOCOL

    return _fake, captured


# ---------------------------------------------------------------------------
# G4 — резолвер жанра в реестре (зеркало get_company_for_series)
# ---------------------------------------------------------------------------
class TestGenreRegistryResolver(unittest.TestCase):

    def test_genre_resolved(self):
        w = _watched(_rec(SLUG, genre="директорат"))
        self.assertEqual(registry.get_genre_for_series(SLUG, w), "директорат")

    def test_missing_genre_is_none(self):
        self.assertIsNone(registry.get_genre_for_series(SLUG, _watched(_rec(SLUG))))

    def test_case_insensitive_and_trimmed(self):
        w = _watched(_rec(SLUG, genre=" Директорат "))
        self.assertEqual(registry.get_genre_for_series(SLUG, w), "директорат")

    def test_invalid_genre_ignored(self):
        # Мусорное значение пропускается, как будто не задано (устойчивость).
        self.assertIsNone(registry.get_genre_for_series(SLUG, _watched(_rec(SLUG, genre="планёрка"))))

    def test_first_nonempty_among_series_records(self):
        w = _watched(_rec(SLUG, id="a"), _rec(SLUG, id="b", genre="продукт"))
        self.assertEqual(registry.get_genre_for_series(SLUG, w), "продукт")

    def test_other_series_not_matched(self):
        self.assertIsNone(registry.get_genre_for_series(SLUG, _watched(_rec("other", genre="директорат"))))

    def test_all_valid_genres_resolve(self):
        for g in _ALL_GENRES:
            self.assertEqual(registry.get_genre_for_series(SLUG, _watched(_rec(SLUG, genre=g))), g)


# ---------------------------------------------------------------------------
# G4 — валидация поля genre в записи watched.yaml
# ---------------------------------------------------------------------------
class TestGenreValidation(unittest.TestCase):

    def _rooms(self):
        return {"rooms": []}

    def test_valid_genre_no_errors(self):
        self.assertEqual(registry.validate_watched_record(_rec(SLUG, genre="директорат"), self._rooms()), [])

    def test_absent_genre_no_errors(self):
        self.assertEqual(registry.validate_watched_record(_rec(SLUG), self._rooms()), [])

    def test_invalid_genre_flagged(self):
        errs = registry.validate_watched_record(_rec(SLUG, genre="планёрка"), self._rooms())
        self.assertTrue(any("genre" in e for e in errs))

    def test_every_valid_genre_accepted(self):
        for g in _ALL_GENRES:
            self.assertEqual(registry.validate_watched_record(_rec(SLUG, genre=g), self._rooms()), [])


# ---------------------------------------------------------------------------
# G4 — обёртка series_markup + мягкий дефолт (A4: не форсируем, не падаем)
# ---------------------------------------------------------------------------
class TestGenreMarkupWrapper(unittest.TestCase):

    def test_genre_from_markup(self):
        w = _watched(_rec(SLUG, genre="директорат"))
        self.assertEqual(series_markup.genre_for_series(SLUG, watched=w), "директорат")

    def test_unmarked_series_soft_default(self):
        self.assertEqual(series_markup.genre_for_series(SLUG, watched=_watched(_rec(SLUG))),
                         series_markup.DEFAULT_GENRE)

    def test_empty_series_soft_default(self):
        self.assertEqual(series_markup.genre_for_series("", watched=_watched()), series_markup.DEFAULT_GENRE)
        self.assertEqual(series_markup.genre_for_series(None, watched=_watched()), series_markup.DEFAULT_GENRE)

    def test_default_genre_has_focus(self):
        # Дефолт обязан иметь инструкцию фокуса (иначе блок молча исчезнет).
        self.assertIn(series_markup.DEFAULT_GENRE, series_markup.GENRE_FOCUS)
        self.assertTrue(series_markup.format_genre_block(series_markup.DEFAULT_GENRE).strip())

    def test_broken_registry_degrades_to_default(self):
        # Битый реестр (watched не той формы) → мягкий дефолт, НЕ падение (graceful A4).
        self.assertEqual(series_markup.genre_for_series(SLUG, watched={"watched": "broken"}),
                         series_markup.DEFAULT_GENRE)


# ---------------------------------------------------------------------------
# G4 — format_genre_block: директорат ≠ координация, мягкие инструкции фокуса
# ---------------------------------------------------------------------------
class TestFormatGenreBlock(unittest.TestCase):

    def test_directorate_distinct_from_coordination(self):
        d = series_markup.format_genre_block("директорат")
        c = series_markup.format_genre_block("координация")
        self.assertTrue(d.strip())
        self.assertTrue(c.strip())
        self.assertNotEqual(d, c)
        # Директорат — решения/риски/цифры; координация — кто-что-когда.
        self.assertIn("РЕШЕНИЯ", d)
        self.assertIn("кто-что-когда", c)
        self.assertNotIn("кто-что-когда", d)

    def test_all_genres_nonempty_and_distinct(self):
        blocks = {g: series_markup.format_genre_block(g) for g in _ALL_GENRES}
        for g, b in blocks.items():
            self.assertTrue(b.strip(), f"пустой блок для жанра {g}")
        self.assertEqual(len(set(blocks.values())), len(_ALL_GENRES), "блоки жанров не уникальны")

    def test_unknown_or_empty_genre_no_block(self):
        self.assertEqual(series_markup.format_genre_block("несуществующий"), "")
        self.assertEqual(series_markup.format_genre_block(""), "")
        self.assertEqual(series_markup.format_genre_block(None), "")

    def test_block_mentions_genre_label(self):
        self.assertIn("директорат", series_markup.format_genre_block("директорат"))


# ---------------------------------------------------------------------------
# G4 — блок жанра доходит до user-промпта генерации (Вызов 1, единственный)
# ---------------------------------------------------------------------------
class TestGenreBlockInPrompt(unittest.TestCase):

    def test_genre_block_in_user_prompt_by_default(self):
        # На этой машине нет watched.yaml → мягкий дефолт (координация) → блок есть.
        prompt = lp._format_protocol_user_prompt("транскрипт", _META)
        self.assertIn("Тип (повестка) этой встречи", prompt)

    def test_directorate_focus_differs_from_coordination(self):
        with mock.patch.object(lp.series_markup, "genre_for_series", return_value="директорат"):
            p_dir = lp._format_protocol_user_prompt("т", dict(_META))
        with mock.patch.object(lp.series_markup, "genre_for_series", return_value="координация"):
            p_coord = lp._format_protocol_user_prompt("т", dict(_META))
        self.assertNotEqual(p_dir, p_coord)
        self.assertIn("РЕШЕНИЯ", p_dir)
        self.assertIn("кто-что-когда", p_coord)
        self.assertNotIn("кто-что-когда", p_dir)

    def test_genre_block_reaches_generate_protocol(self):
        """End-to-end: блок жанра доходит до user-промпта генерации (один вызов)."""
        fake, cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.generate_protocol("транскрипт", _META, method_text="М", meeting_sid="g4")
        self.assertIn("Тип (повестка) этой встречи", cap["user"])

    def test_single_generation_call(self):
        """ГРАН1/НЕС1: жанр НЕ плодит отдельный Opus-вызов — генерация это один вызов."""
        calls = []

        def _fake(user_prompt, *, system, timeout, model):
            calls.append(model)
            return _FAKE_PROTOCOL

        with mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            lp.generate_protocol("транскрипт", _META, method_text="М", meeting_sid="g4b")
        self.assertEqual(len(calls), 1)

    def test_genre_failure_does_not_break_prompt(self):
        """Сбой резолва жанра → промпт собран без блока, генерация не падает."""
        with mock.patch.object(lp.series_markup, "genre_for_series", side_effect=RuntimeError("boom")):
            prompt = lp._format_protocol_user_prompt("транскрипт", _META)
        self.assertIn("Транскрипт:", prompt)
        self.assertNotIn("Тип (повестка) этой встречи", prompt)

    def test_genre_block_not_logged(self):
        """G10/опасная тройка: ни транскрипт, ни блок жанра не утекают в лог."""
        secret = "СЕКРЕТ-РЕПЛИКА-Ф5"
        fake, _cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake), \
                self.assertLogs(lp.logger, level="DEBUG") as logctx:
            lp.generate_protocol(secret, _META, method_text="М", meeting_sid="g4c")
        blob = "\n".join(logctx.output)
        self.assertNotIn(secret, blob)
        self.assertNotIn("Тип (повестка) этой встречи", blob)
        self.assertIn("model=", blob)  # метаданные есть — лог полезный


# ---------------------------------------------------------------------------
# G5 — методичка точечно усилена, структура цела, доходит до system-промпта
# ---------------------------------------------------------------------------
class TestMethodichkaReinforced(unittest.TestCase):

    # Эталонные секции методички, которые усиление НЕ должно удалить/переименовать.
    _KEY_SECTIONS = [
        "#протоколвстречи",
        "## Структура файла",
        "### 1. Шапка",
        "### 2. Тематические блоки",
        "### 3. Блок «Решения",
        "### 4. Блок «Задачи»",
        "## Стилевые правила",
        "## Маппинг имён спикеров",
        "## Доставка",
        "## Эталон",
        "## Лог применений",
    ]
    # Маркеры точечного усиления Ф5 (G5).
    _NEW_MARKERS = [
        "Полнота и фокус протокола",
        "не сел дописывать",
        "У каждой задачи есть владелец",
    ]

    def _load(self) -> str:
        try:
            return lp._load_method_text()
        except lp.ProtocolGenerationError:
            self.skipTest("метод-файл не найден на этой машине (headless/VPS без rsync)")

    def test_key_sections_intact(self):
        text = self._load()
        for marker in self._KEY_SECTIONS:
            self.assertIn(marker, text, f"методичка потеряла эталонную секцию: {marker}")

    def test_reinforcement_present(self):
        text = self._load()
        for marker in self._NEW_MARKERS:
            self.assertIn(marker, text, f"усиление Ф5 не найдено в методичке: {marker}")

    def test_method_full_reaches_system_prompt(self):
        text = self._load()
        self.assertGreater(len(text), 200)
        fake, cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake):
            lp.generate_protocol("т", _META, method_text=text, meeting_sid="g5")
        # Методичка целиком — подстрока system-промпта (без среза).
        self.assertIn(text, cap["system"])
        # Усиление тоже доходит целиком.
        self.assertIn("Полнота и фокус протокола", cap["system"])


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
