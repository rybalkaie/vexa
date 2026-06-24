"""Тесты Ф8 (план `2026-06-13-umnyi-protokol-assemblyai.md`) — трекинг открытых
задач серии во времени (REQ G9).

Покрывает:
  - extract_open_tasks: новые задачи (блок «Задачи», с префиксом исполнителя) +
    перенесённые (блок «🔻 С прошлых встреч»): «закрыта» отброшена, «висит» несётся
    дальше (суффикс статуса срезан), «висит, статус?» сохранена; дедуп; срез маркеров.
  - классификация заголовка carryover: секция «🔻 С прошлых встреч» НЕ тема и её
    буллеты НЕ уходят в key_points выжимки.
  - build_digest: open_tasks — лазивый ключ (нет при пустом, есть при непустом),
    без сырых реплик.
  - resolve_open_tasks: берёт хвост из САМОЙ СВЕЖЕЙ выжимки; пусто/нет ключа → [];
    env-cap соблюдается.
  - format_open_tasks_block / build_open_tasks_block: блок с разделом «🔻 С прошлых
    встреч»; kill-switch; лог только счётчики (без текста задач — опасная тройка).
  - достижимость в Вызов 1: блок доходит до user-промпта generate_protocol, РОВНО
    один claude-вызов (ГРАН1/НЕС1 — не третий Opus-вызов).
  - паритет двух боевых call-site (finalize + clarify): оба зовут
    build_open_tasks_block и передают open_tasks= (footgun review-checks-two-call-sites).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase8_open_tasks_tracking -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import series_memory as sm  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402


# Протокол встречи M1: только что назначенные задачи (блок «Задачи»), без хвоста.
_PROTO_M1 = """#протоколвстречи 01.06.2026

**Встреча:** Координация по маркетплейсам.

**Длительность:** 20 мин

**Участники:** Илья Рыбалка, Татьяна Филиппова

**Транскрипт:** [2026-06-01.md](2026-06-01.md)

---

## 1) Бюджет

▪️ Обсудили бюджет на июнь.

## Решения / что внедряем

🔸 Запустить рекламу с 1 июня.

## Задачи

**Татьяна Филиппова**

- 🟠 Прислать медиаплан по WB — до пятницы

**Илья Рыбалка**

- Согласовать бюджет с финансами
"""

# Протокол M2: новая задача + раздел «🔻 С прошлых встреч» с одной закрытой и одной
# висящей перенесённой задачей.
_PROTO_M2 = """#протоколвстречи 08.06.2026

**Встреча:** Координация по маркетплейсам.

**Длительность:** 25 мин

**Участники:** Илья Рыбалка, Татьяна Филиппова

**Транскрипт:** [2026-06-08.md](2026-06-08.md)

---

## 1) Реклама

▪️ Реклама запущена, идёт.

## Задачи

**Илья Рыбалка**

- 🟠 Подготовить отчёт по ДРР — к среде

## 🔻 С прошлых встреч

- Татьяна Филиппова: Прислать медиаплан по WB — до пятницы — закрыта
- Илья Рыбалка: Согласовать бюджет с финансами — висит
"""

_META = {"series": "koordinaciya", "date": "2026-06-15",
         "participants": ["Илья Рыбалка"], "expectedParticipants": ["Илья Рыбалка"]}


def _capture_call():
    """side_effect для call_claude_print: пишет аргументы и отдаёт протокол."""
    cap: dict = {}

    def _fake(user_prompt, *, system, timeout, model):
        cap["user"] = user_prompt
        cap["system"] = system
        cap["model"] = model
        return _PROTO_M1  # любой валидный протокол (с шапкой)

    return _fake, cap


# ==========================================================================
# extract_open_tasks — чистая функция извлечения
# ==========================================================================
class TestExtractOpenTasks(unittest.TestCase):

    def test_fresh_tasks_from_zadachi_with_owner_prefix(self):
        tasks = sm.extract_open_tasks(_PROTO_M1)
        # обе задачи извлечены, исполнитель префиксом
        self.assertTrue(any(t.startswith("Татьяна Филиппова:") and "медиаплан" in t for t in tasks))
        self.assertTrue(any(t.startswith("Илья Рыбалка:") and "бюджет" in t.lower() for t in tasks))

    def test_markers_stripped(self):
        tasks = sm.extract_open_tasks(_PROTO_M1)
        for t in tasks:
            self.assertNotIn("🟠", t)
            self.assertFalse(t.lstrip().startswith("-"))

    def test_carryover_closed_dropped_hanging_kept(self):
        tasks = sm.extract_open_tasks(_PROTO_M2)
        joined = " | ".join(tasks)
        # закрытая перенесённая — отброшена
        self.assertNotIn("медиаплан", joined)
        # висящая перенесённая — несётся дальше, БЕЗ суффикса статуса
        self.assertTrue(any("Согласовать бюджет с финансами" in t for t in tasks))
        self.assertNotIn("висит", joined)
        self.assertNotIn("закрыта", joined)
        # новая задача этой встречи присутствует
        self.assertTrue(any("ДРР" in t for t in tasks))

    def test_carryover_status_question_kept(self):
        proto = _PROTO_M2.replace(
            "- Илья Рыбалка: Согласовать бюджет с финансами — висит",
            "- Илья Рыбалка: Согласовать бюджет с финансами — висит, статус?",
        )
        tasks = sm.extract_open_tasks(proto)
        self.assertTrue(any("Согласовать бюджет с финансами" == t.split(": ", 1)[-1] for t in tasks))
        self.assertNotIn("статус?", " | ".join(tasks))

    def test_closed_requires_dash_no_false_positive(self):
        # Висящая задача, формулировка которой ОКАНЧИВАЕТСЯ на «закрыто» без
        # дефиса-разделителя статуса → НЕ должна схлопнуться в «закрыта».
        proto = _PROTO_M2.replace(
            "- Илья Рыбалка: Согласовать бюджет с финансами — висит",
            "- Илья Рыбалка: Проверить, всё ли по складу закрыто — висит",
        )
        tasks = sm.extract_open_tasks(proto)
        self.assertTrue(any("всё ли по складу закрыто" in t for t in tasks))

    def test_status_suffix_trailing_punctuation_tolerated(self):
        # Дрейф (У1): модель дописала хвостовую пунктуацию/эмодзи к статусу.
        # Суффикс всё равно срезается (carryover не копит статусы), а закрытая
        # с «✅.» — отбрасывается. Дефис-разделитель по-прежнему обязателен.
        proto = _PROTO_M2.replace(
            "- Илья Рыбалка: Согласовать бюджет с финансами — висит",
            "- Илья Рыбалка: Согласовать бюджет с финансами — висит.",
        ).replace(
            "- Татьяна Филиппова: Прислать медиаплан по WB — до пятницы — закрыта",
            "- Татьяна Филиппова: Прислать медиаплан по WB — до пятницы — закрыта ✅.",
        )
        tasks = sm.extract_open_tasks(proto)
        joined = " | ".join(tasks)
        self.assertNotIn("медиаплан", joined)   # закрыта с «✅.» — отброшена
        self.assertNotIn("висит", joined)       # суффикс с точкой срезан
        self.assertTrue(any(t.endswith("Согласовать бюджет с финансами") for t in tasks))

    def test_dedup(self):
        proto = _PROTO_M1 + "\n## 🔻 С прошлых встреч\n\n- Илья Рыбалка: Согласовать бюджет с финансами — висит\n"
        tasks = sm.extract_open_tasks(proto)
        n = sum(1 for t in tasks if "Согласовать бюджет с финансами" in t)
        self.assertEqual(n, 1)

    def test_cap_enforced(self):
        body = "## Задачи\n\n**Команда**\n\n" + "\n".join(
            f"- Задача номер {i}" for i in range(60))
        proto = "#протоколвстречи 01.06.2026\n\n**Участники:** Илья\n\n---\n\n" + body
        tasks = sm.extract_open_tasks(proto)
        self.assertLessEqual(len(tasks), sm._MAX_OPEN_TASKS)

    def test_no_raw_replies(self):
        # В извлечённых задачах не должно быть сырых таймкодов-реплик (РИСК4).
        for t in sm.extract_open_tasks(_PROTO_M2):
            self.assertNotRegex(t, r"\*\*\[\d")
            self.assertNotIn("[00:", t)


# ==========================================================================
# Классификация carryover-секции — не тема, не key_points
# ==========================================================================
class TestCarryoverClassification(unittest.TestCase):

    def test_classify_heading_carryover(self):
        self.assertEqual(sm._classify_heading("🔻 С прошлых встреч"), "carryover")
        self.assertEqual(sm._classify_heading("С прошлых встреч"), "carryover")

    def test_carryover_not_in_themes_or_keypoints(self):
        themes, kp = sm.extract_protocol_sections(_PROTO_M2)
        for t in themes:
            self.assertNotIn("прошлых встреч", t.lower())
        joined = " | ".join(kp)
        # перенесённые задачи не должны утечь в key_points выжимки
        self.assertNotIn("медиаплан", joined)
        self.assertNotIn("Согласовать бюджет", joined)


# ==========================================================================
# build_digest — лазивое поле open_tasks
# ==========================================================================
class TestBuildDigestOpenTasks(unittest.TestCase):

    def test_open_tasks_present_when_nonempty(self):
        d = sm.build_digest(_PROTO_M1, {"series": "s", "date": "2026-06-01"})
        self.assertIn("open_tasks", d)
        self.assertTrue(d["open_tasks"])

    def test_open_tasks_lazy_absent_when_empty(self):
        text = "#протоколвстречи 01.06.2026\n\n**Участники:** Илья\n\n---\n\n## 1) Тема\n\n▪️ просто обсудили\n"
        d = sm.build_digest(text, {"series": "s", "date": "2026-06-01"})
        self.assertNotIn("open_tasks", d)

    def test_digest_no_raw_replies(self):
        d = sm.build_digest(_PROTO_M2, {"series": "s", "date": "2026-06-08"})
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotRegex(blob, r"\*\*\[\d")

    def test_m2_digest_drops_closed(self):
        d = sm.build_digest(_PROTO_M2, {"series": "s", "date": "2026-06-08"})
        blob = json.dumps(d.get("open_tasks") or [], ensure_ascii=False)
        self.assertNotIn("медиаплан", blob)        # закрытая — не в хвосте
        self.assertIn("ДРР", blob)                 # новая — в хвосте


# ==========================================================================
# resolve_open_tasks — хвост из самой свежей выжимки
# ==========================================================================
class TestResolveOpenTasks(unittest.TestCase):

    def test_latest_digest_wins(self):
        digests = [
            {"date": "2026-06-01", "open_tasks": ["старая A", "старая B"]},
            {"date": "2026-06-08", "open_tasks": ["свежая C"]},
        ]
        self.assertEqual(sm.resolve_open_tasks(digests), ["свежая C"])

    def test_empty_when_no_key(self):
        self.assertEqual(sm.resolve_open_tasks([{"date": "2026-06-01"}]), [])
        self.assertEqual(sm.resolve_open_tasks([]), [])

    def test_latest_empty_overrides_older(self):
        # Самая свежая выжимка без ключа (всё закрыли) → [] (не воскрешаем старое).
        digests = [
            {"date": "2026-06-01", "open_tasks": ["висела X"]},
            {"date": "2026-06-08"},  # ключа нет
        ]
        self.assertEqual(sm.resolve_open_tasks(digests), [])

    def test_cap_respected(self):
        many = [f"задача {i}" for i in range(40)]
        digests = [{"date": "2026-06-08", "open_tasks": many}]
        with mock.patch.dict(os.environ, {"OPEN_TASKS_MAX": "5"}):
            self.assertEqual(len(sm.resolve_open_tasks(digests)), 5)

    def test_dedup_in_resolve(self):
        digests = [{"date": "2026-06-08", "open_tasks": ["A", "a", "B"]}]
        self.assertEqual(sm.resolve_open_tasks(digests), ["A", "B"])


# ==========================================================================
# format/build блока + kill-switch + приватность лога
# ==========================================================================
class TestFormatAndBuildBlock(unittest.TestCase):

    def test_format_empty_is_blank(self):
        self.assertEqual(sm.format_open_tasks_block([]), "")

    def test_format_has_section_heading_and_tasks(self):
        block = sm.format_open_tasks_block(["Илья: сделать X", "Татьяна: прислать Y"])
        # Ф3: мягкий заголовок (вариант владельца), без 🔻.
        self.assertIn(f"## {sm.PENDING_SECTION_HEADING}", block)
        self.assertIn("сделать X", block)
        self.assertIn("прислать Y", block)
        # дисциплина: статус определять по текущему транскрипту
        self.assertIn("закрыта", block)
        self.assertIn("висит", block)

    def test_header_instructs_preserve_owner_prefix(self):
        # У2: промпт явно велит сохранять префикс исполнителя — иначе на round-trip
        # через carryover теряется «кто следующий шаг» (половина ценности G9).
        block = sm.format_open_tasks_block(["Татьяна: прислать Y"])
        self.assertIn("префикс исполнителя", block)

    def test_build_block_killswitch_off(self):
        digests = [{"date": "2026-06-08", "open_tasks": ["висит X"]}]
        with mock.patch.dict(os.environ, {"ENABLE_OPEN_TASKS_TRACKING": "0"}):
            self.assertEqual(sm.build_open_tasks_block(digests), "")

    def test_build_block_on_by_default(self):
        digests = [{"date": "2026-06-08", "open_tasks": ["Илья: сделать X"]}]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_OPEN_TASKS_TRACKING", None)
            block = sm.build_open_tasks_block(digests)
        self.assertIn("сделать X", block)

    def test_build_block_log_only_counters(self):
        secret = "СЕКРЕТНАЯ-ЗАДАЧА-Ф8"
        digests = [{"date": "2026-06-08", "open_tasks": [f"Илья: {secret}"]}]
        with self.assertLogs("meeting_notary.series_memory", level="INFO") as cm:
            sm.build_open_tasks_block(digests, meeting_sid="sid8")
        blob = "\n".join(cm.output)
        self.assertIn("carried=1", blob)       # счётчик есть
        self.assertNotIn(secret, blob)         # текста задачи нет

    def test_env_helpers(self):
        with mock.patch.dict(os.environ, {"OPEN_TASKS_MAX": "0"}):
            self.assertEqual(sm.open_tasks_max(), sm.DEFAULT_OPEN_TASKS_MAX)
        with mock.patch.dict(os.environ, {"OPEN_TASKS_MAX": "999"}):
            # потолок подачи = storage cap (просить больше, чем хранится, нельзя)
            self.assertEqual(sm.open_tasks_max(), sm._MAX_OPEN_TASKS)
        with mock.patch.dict(os.environ, {"ENABLE_OPEN_TASKS_TRACKING": "no"}):
            self.assertFalse(sm.is_open_tasks_enabled())


# ==========================================================================
# Достижимость в Вызов 1 генерации (ГРАН1/НЕС1)
# ==========================================================================
class TestOpenTasksReachesGeneration(unittest.TestCase):

    def test_block_in_user_prompt(self):
        block = sm.format_open_tasks_block(["Илья: добить отчёт"])
        prompt = lp._format_protocol_user_prompt("транскрипт", _META, open_tasks=block)
        self.assertIn(f"## {sm.PENDING_SECTION_HEADING}", prompt)
        self.assertIn("добить отчёт", prompt)
        # блок ПЕРЕД транскриптом
        self.assertLess(prompt.index("добить отчёт"), prompt.index("Транскрипт:"))

    def test_block_after_memory_block(self):
        mem = sm.format_memory_block([
            {"date": "2026-06-01", "participants": ["Илья"], "themes": ["Бюджет"], "key_points": []}
        ])
        block = sm.format_open_tasks_block(["Илья: добить отчёт"])
        prompt = lp._format_protocol_user_prompt(
            "транскрипт", _META, series_memory=mem, open_tasks=block)
        # открытые задачи идут ПОСЛЕ блока памяти серии (обратная дисциплина)
        self.assertLess(prompt.index("СПРАВКА"), prompt.index("ВОПРОСЫ С ПРОШЛЫХ ВСТРЕЧ"))

    def test_none_open_tasks_no_block(self):
        prompt = lp._format_protocol_user_prompt("транскрипт", _META, open_tasks=None)
        self.assertNotIn("🔻 С прошлых встреч", prompt)

    def test_reaches_generate_protocol_single_call(self):
        """End-to-end: блок доходит до user-промпта генерации РОВНО одним вызовом
        (ГРАН1/НЕС1 — трекинг НЕ плодит третий Opus-вызов)."""
        block = sm.format_open_tasks_block(["Илья: добить отчёт по складу"])
        calls = []

        def _fake(user_prompt, *, system, timeout, model):
            calls.append(user_prompt)
            return _PROTO_M1

        with mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            lp.generate_protocol("транскрипт", _META, method_text="М",
                                 meeting_sid="ph8", open_tasks=block)
        self.assertEqual(len(calls), 1)
        self.assertIn("добить отчёт по складу", calls[0])

    def test_generate_protocol_not_logged(self):
        """G10/опасная тройка: ни транскрипт, ни блок задач не утекают в лог."""
        secret = "СЕКРЕТ-ЗАДАЧА-LOG"
        block = sm.format_open_tasks_block([f"Илья: {secret}"])
        fake, _cap = _capture_call()
        with mock.patch.object(lp, "call_claude_print", side_effect=fake), \
                self.assertLogs(lp.logger, level="DEBUG") as logctx:
            lp.generate_protocol("транскрипт", _META, method_text="М",
                                 meeting_sid="ph8b", open_tasks=block)
        blob = "\n".join(logctx.output)
        self.assertNotIn(secret, blob)


# ==========================================================================
# Синтетика: накопили хвост → resolve → блок в промпте
# ==========================================================================
class TestEndToEndSynthetic(unittest.TestCase):

    def test_accumulate_then_block_appears(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            series_dir = root / "koordinaciya"
            series_dir.mkdir()
            # M1: сохранили выжимку с открытыми задачами (накопление хвоста).
            d1 = sm.build_digest(_PROTO_M1, {"series": "koordinaciya", "date": "2026-06-01"})
            sm.save_digest(series_dir, "2026-06-01", d1)
            self.assertIn("open_tasks", d1)
            # Следующая встреча: резолвим память серии → строим блок.
            digests = sm.resolve_memory(series_dir, root, current_date="2026-06-15")
            self.assertTrue(digests)
            block = sm.build_open_tasks_block(digests, meeting_sid="e2e")
            self.assertIn(f"## {sm.PENDING_SECTION_HEADING}", block)
            self.assertIn("медиаплан", block)
            # блок доходит до промпта генерации
            prompt = lp._format_protocol_user_prompt("транскрипт", _META, open_tasks=block)
            self.assertIn("медиаплан", prompt)

    def test_current_meeting_excluded_no_self_loop(self):
        """Регресс (У3): резолв памяти серии исключает выжимку ТЕКУЩЕЙ встречи →
        её СОБСТВЕННЫЕ открытые задачи НЕ возвращаются как «с прошлых встреч».
        Иначе clarify-реген (выжимка текущей встречи уже сохранена первичным
        finalize) зациклил бы новые задачи встречи в раздел переноса."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            series_dir = root / "koordinaciya"
            series_dir.mkdir()
            # Прошлая встреча — висящий хвост-якорь.
            prev = sm.build_digest(_PROTO_M1, {"series": "koordinaciya", "date": "2026-06-01"})
            sm.save_digest(series_dir, "2026-06-01", prev)
            # ТЕКУЩАЯ встреча уже сохранена (как после первичного finalize) со СВОЕЙ задачей.
            cur_proto = _PROTO_M1.replace(
                "Согласовать бюджет с финансами", "ЗАДАЧА-ТЕКУЩЕЙ-ВСТРЕЧИ")
            cur = sm.build_digest(cur_proto, {"series": "koordinaciya", "date": "2026-06-08"})
            sm.save_digest(series_dir, "2026-06-08", cur)
            # Резолв ДЛЯ текущей встречи (clarify-путь передаёт current_date).
            digests = sm.resolve_memory(series_dir, root, current_date="2026-06-08")
            block = sm.build_open_tasks_block(digests, meeting_sid="loop-guard")
            self.assertNotIn("ЗАДАЧА-ТЕКУЩЕЙ-ВСТРЕЧИ", block)  # свои задачи не зациклены
            self.assertIn("медиаплан", block)                  # хвост прошлой встречи на месте


# ==========================================================================
# Паритет боевых call-site (footgun review-checks-two-call-sites)
# ==========================================================================
class TestCallSiteParity(unittest.TestCase):

    def test_both_call_sites_build_and_pass_open_tasks(self):
        """finalize И clarify: оба зовут build_open_tasks_block и передают open_tasks=
        в regenerate_protocol_for_meeting — иначе один из триггеров терял бы трекинг."""
        for rel in ("finalize-meeting.py", "lib/clarify_worker.py"):
            src = (_NOTARY / rel).read_text(encoding="utf-8")
            self.assertIn("build_open_tasks_block", src, f"{rel}: блок не строится")
            i = src.find("regenerate_protocol_for_meeting(")
            self.assertNotEqual(i, -1, f"{rel}: вызов генерации не найден")
            call_chunk = src[i:i + 700]
            self.assertIn("open_tasks=", call_chunk,
                          f"{rel}: regenerate_protocol_for_meeting без open_tasks=")


if __name__ == "__main__":
    unittest.main(verbosity=2)
