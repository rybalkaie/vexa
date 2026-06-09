"""Тесты Ф7 — память серии встреч: контекст прошлых встреч.

Покрывает REQ 7.1–7.5 плана `2026-06-03-dorabotki-bot-notarius-full.md`:
  - 7.1  выжимка-память: детерминированная (без claude), сохраняется рядом с
         протоколом, состав ограничен (без сырых реплик), ⚠️-пометки срезаны.
  - 7.2  резолвер серии: прямая память той же серии (стабильный slug календаря)
         И fallback по совпадению состава участников (нерегулярные 1-на-1).
  - 7.3  проброс N последних выжимок; `SERIES_MEMORY_DEPTH` управляет числом;
         memory-секция уходит в ОДИН claude-вызов ревью (НЕ 3-й проход).
  - 7.4  дисциплина «прошлое = справка»: блок справки несёт запрет переноса
         фактов; memory-секция ревью ловит факт без опоры на транскрипт; имя
         постоянного участника подставляется (7-связка → 5.4).
  - 7.5  бэкфилл выжимок из готовых протоколов (на синтетике).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase7_series_memory -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import series_memory as sm  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402


# Реалистичный протокол (структура из методички + реального примера).
_PROTOCOL = """#протоколвстречи 27.05.2026

**Встреча:** Координация по маркетплейсам.

**Длительность:** 30 мин

**Участники:** Илья Рыбалка, Татьяна Филиппова

**Транскрипт:** [2026-05-27.md](2026-05-27.md)

---

## 1) Бюджет на июнь

▪️ Бюджет июня — **2,8 млн ₽**, согласовали.  ⚠️ проверь: сверь число

▫️ Тематический пункт без числа.

## 2) Конверсия

▪️ Конверсия выросла до 4% за неделю.

## Решения / что внедряем

🔸 Запустить рекламу WB с 1 июня.

🔸 Перераспределить 500 тыс ₽ на Ozon.

## Задачи

**Илья Рыбалка**

- 🟠 Прислать финальный бюджет до пятницы.
"""


def _write_protocol(series_dir: Path, date: str, text: str | None = None) -> Path:
    series_dir.mkdir(parents=True, exist_ok=True)
    p = series_dir / f"{date}-protokol.md"
    body = text if text is not None else _PROTOCOL.replace("2026-05-27", date)
    p.write_text(body, encoding="utf-8")
    return p


def _digest(date: str, series: str, participants: list[str],
            themes: list[str] | None = None, key_points: list[str] | None = None) -> dict:
    return {
        "schema": sm.SCHEMA_VERSION,
        "date": date,
        "series": series,
        "participants": participants,
        "themes": themes if themes is not None else ["Тема"],
        "key_points": key_points if key_points is not None else [],
    }


# ==========================================================================
# 7.1 — построение выжимки (детерминированно, без claude)
# ==========================================================================
class TestBuildDigest(unittest.TestCase):

    def test_extracts_themes_excluding_service_sections(self):
        themes, _ = sm.extract_protocol_sections(_PROTOCOL)
        self.assertIn("Бюджет на июнь", themes)
        self.assertIn("Конверсия", themes)
        # Служебные заголовки НЕ темы.
        self.assertNotIn("Решения / что внедряем", themes)
        for t in themes:
            self.assertNotIn("Задач", t)
            self.assertNotIn("Решен", t)

    def test_key_points_decisions_first_then_numbers(self):
        _, kp = sm.extract_protocol_sections(_PROTOCOL)
        joined = " | ".join(kp)
        # решения присутствуют
        self.assertIn("Запустить рекламу WB", joined)
        self.assertIn("Перераспределить 500 тыс", joined)
        # числовой тематический пункт присутствует (динамика чисел)
        self.assertTrue(any("2,8 млн" in x for x in kp))
        # пункт БЕЗ числа из темы НЕ попал
        self.assertFalse(any("без числа" in x.lower() for x in kp))

    def test_review_flag_stripped_from_points(self):
        _, kp = sm.extract_protocol_sections(_PROTOCOL)
        for x in kp:
            self.assertNotIn("⚠️", x)
            self.assertNotIn("проверь", x)

    def test_no_raw_replies_in_digest(self):
        # Сырых таймкодов-реплик `**[ts] Имя:**` в выжимке быть не должно (РИСК4).
        d = sm.build_digest(_PROTOCOL, {"series": "s", "date": "2026-05-27"})
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("[00:", blob)
        self.assertNotRegex(blob, r"\*\*\[\d")

    def test_participants_header_first(self):
        # Шапка протокола приоритетна над meta (реальные присутствовавшие).
        meta = {"series": "s", "date": "2026-05-27",
                "expectedParticipants": ["Кто-То Другой"], "participants": []}
        d = sm.build_digest(_PROTOCOL, meta)
        self.assertIn("Илья Рыбалка", d["participants"])
        self.assertIn("Татьяна Филиппова", d["participants"])
        self.assertNotIn("Кто-То Другой", d["participants"])

    def test_participants_meta_fallback_when_no_header(self):
        text = "#протоколвстречи 01.06.2026\n\nтело без шапки участников\n"
        meta = {"series": "s", "date": "2026-06-01",
                "participants": ["Панель Один"], "expectedParticipants": ["Жданный Два"]}
        d = sm.build_digest(text, meta)
        self.assertIn("Панель Один", d["participants"])

    def test_caps_enforced(self):
        many_themes = "\n".join(f"## {i}) Тема{i}\n▪️ пункт{i}" for i in range(40))
        text = "#протоколвстречи 01.06.2026\n**Участники:** " + \
               ", ".join(f"Чел{i}" for i in range(30)) + "\n---\n" + many_themes
        d = sm.build_digest(text, {"series": "s", "date": "2026-06-01"})
        self.assertLessEqual(len(d["participants"]), sm._MAX_PARTICIPANTS)
        self.assertLessEqual(len(d["themes"]), sm._MAX_THEMES)
        self.assertLessEqual(len(d["key_points"]), sm._MAX_KEY_POINTS)


# ==========================================================================
# 7.1 — хранилище рядом с протоколом
# ==========================================================================
class TestDigestStorage(unittest.TestCase):

    def test_save_next_to_protocol_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "tatyana-sr"
            sdir.mkdir()
            d = sm.build_digest(_PROTOCOL, {"series": "tatyana-sr", "date": "2026-05-27"})
            path = sm.save_digest(sdir, "2026-05-27", d)
            self.assertIsNotNone(path)
            # Файл рядом с протоколом: <date>-memory.json
            self.assertEqual(path.name, "2026-05-27-memory.json")
            self.assertTrue(path.is_file())
            loaded = sm.load_digest(path)
            self.assertEqual(loaded["participants"], d["participants"])

    def test_list_sorted_and_exclude_current(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            sdir.mkdir()
            for dt in ("2026-05-01", "2026-05-20", "2026-06-03"):
                sm.save_digest(sdir, dt, _digest(dt, "s", ["Илья", "Татьяна"]))
            got = sm.list_series_digests(sdir, exclude_date="2026-05-20")
            dates = [d["date"] for d in got]
            self.assertEqual(dates, ["2026-05-01", "2026-06-03"])  # отсортировано asc, 20-е исключено

    def test_load_garbage_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "2026-05-01-memory.json"
            p.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(sm.load_digest(p))


# ==========================================================================
# 7.2 — резолвер серии: прямая + fallback по составу
# ==========================================================================
class TestResolveMemory(unittest.TestCase):

    def test_direct_same_series_calendar(self):
        # Регулярная серия (Татьяна Ср 12:00): прошлые выжимки в той же папке.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / "marketplaces-tatiana"
            sdir.mkdir()
            for dt in ("2026-05-13", "2026-05-20", "2026-05-27"):
                sm.save_digest(sdir, dt, _digest(dt, "marketplaces-tatiana", ["Илья", "Татьяна"]))
            got = sm.resolve_memory(sdir, root, current_participants=["Илья", "Татьяна"],
                                    current_date="2026-06-03", depth=3)
            self.assertEqual([d["date"] for d in got],
                             ["2026-05-13", "2026-05-20", "2026-05-27"])

    def test_depth_limits_to_last_n(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / "s"
            sdir.mkdir()
            for dt in ("2026-05-01", "2026-05-08", "2026-05-15", "2026-05-22", "2026-05-29"):
                sm.save_digest(sdir, dt, _digest(dt, "s", ["Илья", "Татьяна"]))
            got = sm.resolve_memory(sdir, root, current_participants=["Илья"],
                                    current_date="2026-06-05", depth=2)
            self.assertEqual([d["date"] for d in got], ["2026-05-22", "2026-05-29"])

    def test_fallback_by_participants_for_irregular(self):
        # Нерегулярная 1-на-1 с Саргиным: каждая встреча в своей one-off папке →
        # прямой памяти в текущей папке нет → матч по составу среди других серий.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "oneoff-sargin-2026-06-03-zzz"
            cur.mkdir()  # текущая — пустая
            for i, dt in enumerate(("2026-04-10", "2026-05-12")):
                other = root / f"oneoff-sargin-{dt}-id{i}"
                other.mkdir()
                sm.save_digest(other, dt, _digest(dt, other.name, ["Илья Рыбалка", "Саргин"]))
            # шумная серия с другим составом — НЕ должна попасть
            noise = root / "anzhee-direktorat"
            noise.mkdir()
            sm.save_digest(noise, "2026-05-30", _digest("2026-05-30", "anzhee-direktorat",
                                                        ["Илья", "Михаил Еремеев"]))
            got = sm.resolve_memory(cur, root,
                                    current_participants=["Илья Рыбалка", "Саргин Петров"],
                                    current_date="2026-06-03", depth=3)
            dates = sorted(d["date"] for d in got)
            self.assertEqual(dates, ["2026-04-10", "2026-05-12"])
            for d in got:
                self.assertIn("Саргин", " ".join(d["participants"]))

    def test_direct_takes_priority_over_fallback(self):
        # Если в текущей папке есть прямая память — fallback не используется.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "my-series"
            cur.mkdir()
            sm.save_digest(cur, "2026-05-01", _digest("2026-05-01", "my-series", ["Илья", "Татьяна"]))
            sibling = root / "other"
            sibling.mkdir()
            sm.save_digest(sibling, "2026-05-02", _digest("2026-05-02", "other", ["Илья", "Татьяна"]))
            got = sm.resolve_memory(cur, root, current_participants=["Илья", "Татьяна"],
                                    current_date="2026-06-01", depth=3)
            self.assertEqual([d["date"] for d in got], ["2026-05-01"])  # только из своей папки

    def test_empty_when_no_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "fresh"
            cur.mkdir()
            self.assertEqual(
                sm.resolve_memory(cur, root, current_participants=["Илья", "Новичок"],
                                  current_date="2026-06-01", depth=3),
                [],
            )


# ==========================================================================
# 7-связка — постоянный состав из памяти (обогащение expected → 5.4/6.1)
# ==========================================================================
class TestPermanentParticipants(unittest.TestCase):

    def test_recurring_name_detected_owner_excluded(self):
        digests = [
            _digest("2026-05-01", "s", ["Илья Рыбалка", "Татьяна Филиппова"]),
            _digest("2026-05-08", "s", ["Илья", "Татьяна"]),
            _digest("2026-05-15", "s", ["Илья Рыбалка", "Гость Разовый"]),
        ]
        perm = sm.permanent_participants(digests, min_occurrences=2)
        # Татьяна в 2+ встречах (по first-word ключу) → постоянная.
        self.assertTrue(any("Татьяна" in p for p in perm))
        # Илья (владелец) исключён.
        self.assertFalse(any("Илья" in p for p in perm))
        # Разовый гость (1 встреча) не постоянный.
        self.assertFalse(any("Гость" in p for p in perm))

    def test_enrichment_makes_known_for_5_4(self):
        # 7-связка → 5.4: постоянный участник из памяти делает спикера «известным».
        digests = [
            _digest("2026-05-01", "s", ["Илья", "Саргин Иванов"]),
            _digest("2026-05-08", "s", ["Илья", "Саргин Иванов"]),
        ]
        perm = sm.permanent_participants(digests)
        expected_enriched = set(["Илья"]) | set(perm)
        # _is_known_person из llm_postprocess (5.4) теперь признаёт Саргина.
        self.assertTrue(lp._is_known_person("Саргин Иванов", [], expected_enriched))


# ==========================================================================
# 7.3 / 7.4 — справочный блок генерации (дисциплина «справка, не факт»)
# ==========================================================================
class TestMemoryBlock(unittest.TestCase):

    def test_block_has_discipline_and_data(self):
        block = sm.format_memory_block([
            _digest("2026-05-27", "s", ["Илья", "Татьяна"],
                    themes=["Бюджет"], key_points=["Бюджет 2,8 млн ₽"]),
        ])
        self.assertIn("СПРАВКА", block)
        self.assertIn("ЗАПРЕЩЕНО", block)  # запрет переноса фактов (7.4)
        self.assertIn("Татьяна", block)
        self.assertIn("Бюджет", block)

    def test_empty_digests_empty_block(self):
        self.assertEqual(sm.format_memory_block([]), "")

    def test_generation_prompt_carries_memory_before_transcript(self):
        block = sm.format_memory_block([_digest("2026-05-27", "s", ["Илья", "Татьяна"])])
        up = lp._format_protocol_user_prompt(
            "**[00:00:01] Илья:** привет", {"series": "s", "date": "2026-06-03"},
            series_memory=block,
        )
        self.assertIn("СПРАВКА", up)
        self.assertLess(up.index("СПРАВКА"), up.index("Транскрипт"))

    def test_generation_prompt_without_memory_unchanged(self):
        up = lp._format_protocol_user_prompt("x", {"series": "s"})
        self.assertNotIn("СПРАВКА", up)

    def test_memory_threads_through_regenerate_api(self):
        # 7.3: series_memory доходит до claude-промта через публичный API
        # regenerate_protocol_for_meeting (как зовёт finalize/clarify).
        block = sm.format_memory_block([_digest("2026-05-27", "s", ["Илья", "Татьяна"])])
        captured = {}

        def _fake(user_prompt, **kwargs):
            captured["user"] = user_prompt
            return "#протоколвстречи 03.06.2026\n\nтело\n"

        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td) / "2026-06-03.md"
            tpath.write_text("**[00:00:01] Илья:** привет\n", encoding="utf-8")
            ppath = Path(td) / "2026-06-03-protokol.md"
            with mock.patch.object(lp, "call_claude_print", side_effect=_fake), \
                 mock.patch.object(lp, "_load_method_text", return_value="МЕТОД"):
                lp.regenerate_protocol_for_meeting(
                    transcript_path=tpath, protocol_path=ppath,
                    meeting_meta={"series": "s", "date": "2026-06-03"},
                    series_memory=block,
                )
        self.assertIn("СПРАВКА", captured["user"])
        self.assertLess(captured["user"].index("СПРАВКА"), captured["user"].index("Транскрипт"))


# ==========================================================================
# 7.3 / 7.4 — memory-секция в ОДНОМ claude-вызове ревью + парс/применение
# ==========================================================================
class TestReviewMemorySection(unittest.TestCase):

    def test_one_call_three_sections(self):
        fake = json.dumps({"findings": [
            {"section": "values", "quote": "28 млн", "note": "сверь число"},
            {"section": "roles", "quote": "Ольга", "note": "смешаны зоны"},
            {"section": "memory", "quote": "тема про склад", "note": "нет в записи"},
        ]})
        with mock.patch.object(lp, "call_claude_print", return_value=fake) as m:
            out = lp.review_protocol("протокол", "транскрипт",
                                     checks=("values", "roles", "memory"), meeting_sid="x")
        self.assertEqual(m.call_count, 1)  # НЕ три прохода
        self.assertEqual(sorted(f["section"] for f in out), ["memory", "roles", "values"])

    def test_memory_section_in_system_prompt(self):
        sp = lp._build_review_system_prompt(("values", "roles", "memory"))
        self.assertIn("memory", sp)
        self.assertIn("втягивать прошлое", sp)
        # enum секций в схеме ответа динамический
        self.assertIn("values|roles|memory", sp)

    def test_memory_absent_when_not_in_checks(self):
        sp = lp._build_review_system_prompt(("values", "roles"))
        self.assertNotIn("втягивать прошлое", sp)

    def test_memory_finding_applied_inline(self):
        proto = "## 1) Тема\n\n▪️ обсудили склад и логистику подробно\n"
        findings = [{"section": "memory", "quote": "обсудили склад", "note": "нет в записи"}]
        out = lp.apply_review_flags(proto, findings)
        self.assertIn("⚠️ проверь: нет в записи", out)


# ==========================================================================
# env-конфиг: SERIES_MEMORY_DEPTH, ENABLE_SERIES_MEMORY, retention
# ==========================================================================
class TestEnvConfig(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in (
            "SERIES_MEMORY_DEPTH", "ENABLE_SERIES_MEMORY", "SERIES_MEMORY_RETENTION_DAYS",
        )}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_depth_default_and_override(self):
        self.assertEqual(sm.series_memory_depth(), sm.DEFAULT_DEPTH)
        os.environ["SERIES_MEMORY_DEPTH"] = "5"
        self.assertEqual(sm.series_memory_depth(), 5)
        os.environ["SERIES_MEMORY_DEPTH"] = "0"  # невалид → дефолт
        self.assertEqual(sm.series_memory_depth(), sm.DEFAULT_DEPTH)
        os.environ["SERIES_MEMORY_DEPTH"] = "garbage"
        self.assertEqual(sm.series_memory_depth(), sm.DEFAULT_DEPTH)

    def test_depth_controls_resolved_count(self):
        # REQ 7.3: смена SERIES_MEMORY_DEPTH меняет число подтянутых выжимок.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / "s"
            sdir.mkdir()
            for dt in ("2026-05-01", "2026-05-08", "2026-05-15", "2026-05-22"):
                sm.save_digest(sdir, dt, _digest(dt, "s", ["Илья", "Татьяна"]))
            os.environ["SERIES_MEMORY_DEPTH"] = "2"
            got2 = sm.resolve_memory(sdir, root, current_participants=["Илья"],
                                     current_date="2026-06-01")  # depth из env
            self.assertEqual(len(got2), 2)
            os.environ["SERIES_MEMORY_DEPTH"] = "4"
            got4 = sm.resolve_memory(sdir, root, current_participants=["Илья"],
                                     current_date="2026-06-01")
            self.assertEqual(len(got4), 4)

    def test_enabled_killswitch(self):
        self.assertTrue(sm.is_enabled())  # дефолт ON
        os.environ["ENABLE_SERIES_MEMORY"] = "0"
        self.assertFalse(sm.is_enabled())
        os.environ["ENABLE_SERIES_MEMORY"] = "false"
        self.assertFalse(sm.is_enabled())
        os.environ["ENABLE_SERIES_MEMORY"] = "1"
        self.assertTrue(sm.is_enabled())

    def test_retention_default(self):
        self.assertEqual(sm.retention_days(), sm.DEFAULT_RETENTION_DAYS)
        os.environ["SERIES_MEMORY_RETENTION_DAYS"] = "30"
        self.assertEqual(sm.retention_days(), 30)

    def test_has_series_slug(self):
        # Гейт против перекрёстного загрязнения series-less встреч (общий корень).
        self.assertTrue(sm.has_series_slug("marketplaces-tatiana"))
        self.assertFalse(sm.has_series_slug(""))
        self.assertFalse(sm.has_series_slug("   "))
        self.assertFalse(sm.has_series_slug(None))


# ==========================================================================
# срок хранения — прунинг старых выжимок (РИСК4)
# ==========================================================================
class TestPrune(unittest.TestCase):

    def test_prune_removes_old_keeps_recent(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            sdir.mkdir()
            for dt in ("2025-01-01", "2026-05-01", "2026-06-01"):
                sm.save_digest(sdir, dt, _digest(dt, "s", ["Илья"]))
            # today=2026-06-04, срок 90 дней → 2025-01-01 и 2026-05-01? cutoff=2026-03-06
            removed = sm.prune_old_digests(sdir, 90, today="2026-06-04")
            self.assertEqual(removed, 1)  # только 2025-01-01 старше cutoff
            left = sorted(p.name for p in sdir.glob("*-memory.json"))
            self.assertEqual(left, ["2026-05-01-memory.json", "2026-06-01-memory.json"])

    def test_prune_zero_days_noop(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            sdir.mkdir()
            sm.save_digest(sdir, "2020-01-01", _digest("2020-01-01", "s", ["Илья"]))
            self.assertEqual(sm.prune_old_digests(sdir, 0, today="2026-06-04"), 0)

    def test_prune_root_across_series_skips_service(self):
        # РИСК4: глобальный прун соблюдает retention по dormant-сериям тоже.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for s in ("series-a", "series-b", "_archive"):
                (root / s).mkdir()
            sm.save_digest(root / "series-a", "2020-01-01", _digest("2020-01-01", "series-a", ["Татьяна"]))
            sm.save_digest(root / "series-a", "2026-06-01", _digest("2026-06-01", "series-a", ["Татьяна"]))
            sm.save_digest(root / "series-b", "2019-05-05", _digest("2019-05-05", "series-b", ["Саргин"]))
            sm.save_digest(root / "_archive", "2018-01-01", _digest("2018-01-01", "_archive", ["X"]))
            res = sm.prune_root(root, 180, today="2026-06-04")
            self.assertEqual(res["series"], 2)    # a и b тронуты, служебный _archive пропущен
            self.assertEqual(res["digests"], 2)   # удалены 2020-01-01 и 2019-05-05
            self.assertTrue((root / "series-a" / "2026-06-01-memory.json").exists())   # свежая жива
            self.assertFalse((root / "series-a" / "2020-01-01-memory.json").exists())  # старая удалена
            self.assertTrue((root / "_archive" / "2018-01-01-memory.json").exists())   # служебная не тронута

    def test_prune_root_zero_days_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "s").mkdir()
            sm.save_digest(root / "s", "2010-01-01", _digest("2010-01-01", "s", ["Илья"]))
            self.assertEqual(sm.prune_root(root, 0, today="2026-06-04"), {"series": 0, "digests": 0})


# ==========================================================================
# 7.5 — бэкфилл выжимок из готовых протоколов (синтетика)
# ==========================================================================
class TestBackfill(unittest.TestCase):

    def test_backfill_series_creates_digests(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "tatyana-sr"
            _write_protocol(sdir, "2026-05-20")
            _write_protocol(sdir, "2026-05-27")
            n = sm.backfill_series(sdir)
            self.assertEqual(n, 2)
            self.assertTrue((sdir / "2026-05-20-memory.json").is_file())
            self.assertTrue((sdir / "2026-05-27-memory.json").is_file())
            # память непуста с ПЕРВОЙ следующей встречи
            got = sm.resolve_memory(sdir, Path(td), current_participants=["Илья"],
                                    current_date="2026-06-03", depth=3)
            self.assertEqual(len(got), 2)

    def test_backfill_idempotent_and_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            _write_protocol(sdir, "2026-05-20")
            self.assertEqual(sm.backfill_series(sdir), 1)
            self.assertEqual(sm.backfill_series(sdir), 0)  # уже есть → 0
            self.assertEqual(sm.backfill_series(sdir, overwrite=True), 1)

    def test_backfill_root_skips_service_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_protocol(root / "real-series", "2026-05-20")
            _write_protocol(root / "_archive", "2026-01-01")  # служебная — пропустить
            _write_protocol(root / ".hidden", "2026-02-02")
            res = sm.backfill_root(root)
            self.assertEqual(res["series"], 1)
            self.assertEqual(res["digests"], 1)
            self.assertFalse((root / "_archive" / "2026-01-01-memory.json").exists())

    def test_backfill_digest_content_from_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            _write_protocol(sdir, "2026-05-27")
            sm.backfill_series(sdir)
            d = sm.load_digest(sdir / "2026-05-27-memory.json")
            self.assertIn("Татьяна Филиппова", d["participants"])
            self.assertIn("Бюджет на июнь", d["themes"])
            # бэкфилл детерминирован — без сырых реплик
            self.assertNotIn("[00:", json.dumps(d, ensure_ascii=False))


# ==========================================================================
# Ф2 B1 — save_meeting_digest: после «финализации» memory.json рядом с протоколом
# ==========================================================================
class TestSaveMeetingDigestB1(unittest.TestCase):
    """REQ B1: критерий «после финализации в папке серии появляется
    <date>-memory.json». save_meeting_digest — тестируемая единица того инлайна
    finalize, который раньше нельзя было прогнать без STT/WAV.
    """

    def test_save_creates_memory_next_to_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "series-ezhenedelnaya-koordinaciya-8399ea"
            sdir.mkdir()
            path = sm.save_meeting_digest(
                sdir, "2026-06-09", _PROTOCOL, {"series": sdir.name},
            )
            self.assertIsNotNone(path)
            self.assertEqual(path.name, "2026-06-09-memory.json")
            self.assertTrue((sdir / "2026-06-09-memory.json").is_file())
            d = sm.load_digest(path)
            self.assertIn("Татьяна Филиппова", d["participants"])

    def test_empty_protocol_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            sdir.mkdir()
            self.assertIsNone(sm.save_meeting_digest(sdir, "2026-06-09", "   ", {"series": "s"}))
            self.assertEqual(list(sdir.glob("*-memory.json")), [])

    def test_speaker_mapping_persisted_for_anchor(self):
        # B-связка: cluster→имя из текущей встречи оседает в память → на следующей
        # resolve_speaker_anchor его поднимет (Ф4б).
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            sdir.mkdir()
            sm.save_meeting_digest(
                sdir, "2026-06-09", _PROTOCOL, {"series": "s"},
                speaker_mapping={"SPEAKER_00": "Татьяна Филиппова"},
            )
            d = sm.load_digest(sdir / "2026-06-09-memory.json")
            self.assertEqual(d["speaker_mapping"], {"SPEAKER_00": "Татьяна Филиппова"})
            anchor = sm.resolve_speaker_anchor([d])
            self.assertEqual(anchor, {"SPEAKER_00": "Татьяна Филиппова"})

    def test_prune_runs_after_save(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            sdir.mkdir()
            # старая выжимка, которую прунинг должен снести при сохранении свежей
            sm.save_digest(sdir, "2020-01-01", _digest("2020-01-01", "s", ["Илья"]))
            sm.save_meeting_digest(
                sdir, "2026-06-09", _PROTOCOL, {"series": "s"}, prune_days=180,
            )
            self.assertTrue((sdir / "2026-06-09-memory.json").is_file())
            self.assertFalse((sdir / "2020-01-01-memory.json").is_file())  # старая снесена


# ==========================================================================
# Ф2 (Ф1 §5) — participant_filter: бэкфилл старых протоколов не тащит UI-мусор
# ==========================================================================
class TestParticipantFilterInDigest(unittest.TestCase):
    """Бэкфилл читает шапку СТАРОГО протокола, куда до Ф1-фильтра мог осесть
    UI-мусор Телемоста («ДН», «Скопировать ссылку»). participant_filter должен
    отбраковать его до записи в память серии.
    """

    @staticmethod
    def _filter(names):
        from lib.protocol_to_tg import filter_participant_names
        return filter_participant_names(names)

    def test_build_digest_filters_ui_garbage_from_header(self):
        text = ("#протоколвстречи 02.06.2026\n\n"
                "**Участники:** Мария Михина, ДН, Скопировать ссылку, Ольга Новикова\n\n"
                "---\n\n## 1) Тема\n\n▪️ пункт\n")
        d = sm.build_digest(text, {"series": "s"}, participant_filter=self._filter)
        self.assertIn("Мария Михина", d["participants"])
        self.assertIn("Ольга Новикова", d["participants"])
        self.assertNotIn("ДН", d["participants"])
        self.assertFalse(any("Скопировать" in p for p in d["participants"]))

    def test_no_filter_keeps_legacy_behavior(self):
        # Без participant_filter поведение прежнее — мусор НЕ отфильтрован (обратная
        # совместимость: 654 старых теста не должны измениться).
        text = "#протоколвстречи 02.06.2026\n\n**Участники:** Мария, ДН\n\n---\n\n## 1) Т\n\n▪️ x\n"
        d = sm.build_digest(text, {"series": "s"})
        self.assertIn("ДН", d["participants"])

    def test_backfill_series_applies_filter(self):
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "s"
            sdir.mkdir()
            (sdir / "2026-06-02-protokol.md").write_text(
                "#протоколвстречи 02.06.2026\n\n"
                "**Участники:** Сона Енгибарян, ДН, ИР, Дарья Набережная\n\n"
                "---\n\n## 1) Тема\n\n▪️ пункт\n",
                encoding="utf-8",
            )
            n = sm.backfill_series(sdir, participant_filter=self._filter)
            self.assertEqual(n, 1)
            d = sm.load_digest(sdir / "2026-06-02-memory.json")
            self.assertIn("Сона Енгибарян", d["participants"])
            self.assertIn("Дарья Набережная", d["participants"])
            self.assertNotIn("ДН", d["participants"])
            self.assertNotIn("ИР", d["participants"])


if __name__ == "__main__":
    unittest.main()
