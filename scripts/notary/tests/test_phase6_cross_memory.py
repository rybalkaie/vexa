"""Ф6 (umnyi-protokol-assemblyai) — кросс-встречная память компании + фильтр G11.

Покрывает REQ плана `plans/2026-06-13-umnyi-protokol-assemblyai.md`, Фаза 6:
  - G6 — ШИРОКИЙ кросс-встречный фон: пул = другие серии (приоритет своей компании,
         кросс-компания при релевантности — A7, не стена); relevance-ранжирование.
  - G7 — контроль объёма: ранкер участники+тема+свежесть; числовой лимит N выжимок /
         M символов (РАЗМ1).
  - G11 — фильтр чувствительного как ГЛАВНЫЙ guard: LLM-классификатор помечает
         чувствительное → в фон не идёт; ручной маркер серии `visibility=private` →
         серия в пул не берётся; при сомнении/сбое — исключить (консервативно).
  - G10 (scope) — приватность: текст кандидатов/фона НЕ логируется (только счётчики).

Deploy-гейт фазы: подготовленный чувствительный фрагмент (увольнение / приватная
серия) НЕ появляется в фоне другой встречи — см. TestSensitiveDoesNotLeak.

Третий-party (pyyaml/claude) не нужен: реестр инъектируется моками, claude мокается.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase6_cross_memory -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import series_memory as sm  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402
from lib import claude_cli  # noqa: E402

_TODAY = "2026-06-13"


def _write_digest(series_dir: Path, date: str, *, series: str,
                  participants=None, themes=None, key_points=None) -> None:
    series_dir.mkdir(parents=True, exist_ok=True)
    dig = {
        "schema": 1, "date": date, "series": series,
        "participants": participants or [],
        "themes": themes or [],
        "key_points": key_points or [],
    }
    (series_dir / f"{date}{sm.MEMORY_FILE_SUFFIX}").write_text(
        json.dumps(dig, ensure_ascii=False), encoding="utf-8")


def _all_safe(cands):
    """Стаб-классификатор G11: ничего не чувствительно (все безопасны)."""
    return [False] * len(cands)


def _flag_if(substring):
    """Стаб-классификатор: помечает кандидата чувствительным, если в его темах/
    пунктах встречается `substring` (имитирует решение LLM без вызова claude)."""
    def _clf(cands):
        out = []
        for dig in cands:
            blob = " ".join((dig.get("themes") or []) + (dig.get("key_points") or []))
            out.append(substring in blob)
        return out
    return _clf


# ===========================================================================
# G6/G7 — широкий пул, ранжирование, мягкий приоритет компании (A7)
# ===========================================================================
class TestCrossMemoryRanker(unittest.TestCase):

    def _markup(self, mapping):
        """mapping: slug -> (company, visibility). Возвращает callable-резолвер."""
        return lambda slug: mapping.get(slug, (None, None))

    def test_broad_pool_connects_topic_from_other_series(self):
        """G6: тема из ДРУГОЙ серии той же компании подхватывается в фон."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "anzhee-coord"
            cur.mkdir()
            _write_digest(root / "anzhee-logistics", "2026-06-10",
                          series="anzhee-logistics",
                          themes=["Поставки контейнеров", "Маркетплейс WB"],
                          key_points=["Контейнер на таможне до 15 июня"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company="anzhee",
                current_participants=["Илья Рыбалка"],
                current_topic_tokens=sm._topic_tokens(["Поставки маркетплейс"]),
                current_date="2026-06-13",
                markup_resolver=self._markup({"anzhee-logistics": ("anzhee", None)}),
                sensitive_classifier=_all_safe, today=_TODAY)
            self.assertEqual([d.get("series") for d in sel], ["anzhee-logistics"])

    def test_participant_overlap_pulls_related_series(self):
        """G7: пересечение состава (не-владельцы) даёт релевантность даже без темы."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "cur"
            cur.mkdir()
            _write_digest(root / "sargin-1on1", "2026-06-11", series="sargin-1on1",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Разное"], key_points=["Договорились по плану"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company=None,
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=set(),  # темы нет — тянем по людям
                current_date="2026-06-13",
                markup_resolver=self._markup({}),
                sensitive_classifier=_all_safe, today=_TODAY)
            self.assertEqual([d.get("series") for d in sel], ["sargin-1on1"])

    def test_company_soft_priority_not_a_wall(self):
        """A7: своя компания ранжируется ВЫШЕ, но кросс-компания при сильной
        релевантности всё равно в фоне (стены между компаниями нет)."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "anzhee-coord"
            cur.mkdir()
            # Обе серии: общий не-владелец (Саргин) + общая тема (поставки).
            _write_digest(root / "anzhee-b", "2026-06-10", series="anzhee-b",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки маркетплейс"],
                          key_points=["Поставки в срок"])
            _write_digest(root / "mpfirst-c", "2026-06-10", series="mpfirst-c",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки маркетплейс"],
                          key_points=["Поставки в срок"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company="anzhee",
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки маркетплейс"]),
                current_date="2026-06-13",
                markup_resolver=self._markup({
                    "anzhee-b": ("anzhee", None), "mpfirst-c": ("mpfirst", None)}),
                sensitive_classifier=_all_safe, max_digests=5, today=_TODAY)
            slugs = [d.get("series") for d in sel]
            self.assertIn("mpfirst-c", slugs, "кросс-компания не должна быть стеной (A7)")
            self.assertIn("anzhee-b", slugs)
            self.assertLess(slugs.index("anzhee-b"), slugs.index("mpfirst-c"),
                            "своя компания должна ранжироваться выше кросс-компании")

    def test_cross_company_weak_relevance_excluded(self):
        """A7/«только релевантное»: кросс-компания со СЛАБОЙ релевантностью
        отсекается штрафом ниже порога (а своя — проходит)."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "anzhee-coord"
            cur.mkdir()
            # Слабое пересечение: один общий тематический токен, без общих людей.
            _write_digest(root / "mpfirst-weak", "2026-02-01", series="mpfirst-weak",
                          participants=["Илья Рыбалка", "Болонг"],
                          themes=["Поставки разное прочее"],
                          key_points=["Много несвязанного текста про другое"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company="anzhee",
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки"]),
                current_date="2026-06-13",
                markup_resolver=self._markup({"mpfirst-weak": ("mpfirst", None)}),
                sensitive_classifier=_all_safe, today=_TODAY)
            self.assertEqual(sel, [], "слабая кросс-компания не должна попадать в фон")

    def test_irrelevant_series_not_pulled_by_freshness_alone(self):
        """Свежая, но без пересечения по людям И теме → НЕ фон."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "cur"
            cur.mkdir()
            _write_digest(root / "unrelated", _TODAY, series="unrelated",
                          participants=["Илья Рыбалка", "Незнакомец"],
                          themes=["Совершенно другая тема"],
                          key_points=["Ничего общего"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company=None,
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки маркетплейс"]),
                current_date="2026-06-13",
                markup_resolver=self._markup({}),
                sensitive_classifier=_all_safe, today=_TODAY)
            self.assertEqual(sel, [])

    def test_current_series_skipped(self):
        """Своя серия в кросс-пул не попадает (её несёт resolve_memory)."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "cur"
            _write_digest(cur, "2026-06-01", series="cur",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки"], key_points=["Пункт"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company=None,
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки"]),
                current_date="2026-06-13",
                markup_resolver=self._markup({}),
                sensitive_classifier=_all_safe, today=_TODAY)
            self.assertEqual(sel, [])

    def test_exclude_keys_dedup(self):
        """Выжимка, уже показанная в памяти ТОЙ ЖЕ серии (fallback по составу),
        не дублируется в кросс-фоне."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "cur"
            cur.mkdir()
            _write_digest(root / "sargin-1on1", "2026-06-11", series="sargin-1on1",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки"], key_points=["Пункт"])
            excl = {("sargin-1on1", "2026-06-11")}
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company=None,
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки"]),
                current_date="2026-06-13", exclude_keys=excl,
                markup_resolver=self._markup({}),
                sensitive_classifier=_all_safe, today=_TODAY)
            self.assertEqual(sel, [])


# ===========================================================================
# G7/РАЗМ1 — числовой лимит N выжимок / M символов
# ===========================================================================
class TestCrossMemoryLimit(unittest.TestCase):

    def _populate(self, root, n):
        cur = root / "cur"
        cur.mkdir()
        for i in range(n):
            _write_digest(root / f"anzhee-s{i}", f"2026-06-{10 + i:02d}",
                          series=f"anzhee-s{i}",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки маркетплейс"],
                          key_points=[f"Пункт серии {i}"])
        return cur

    def test_pool_wider_than_n_limited(self):
        """Широкий пул (6 серий) → в выдачу идёт ≤N выжимок (РАЗМ1)."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = self._populate(root, 6)
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company="anzhee",
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки маркетплейс"]),
                current_date="2026-06-13", max_digests=2,
                markup_resolver=lambda s: ("anzhee", None),
                sensitive_classifier=_all_safe, today=_TODAY)
            self.assertEqual(len(sel), 2)

    def test_pool_cap_limits_classifier_input(self):
        """Cost-guard: в классификатор уходит ≤ _CROSS_POOL_CAP кандидатов даже
        при огромном пуле (релевантных серий 12, cap=8)."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = self._populate(root, 12)
            seen = {}

            def _counting_clf(cands):
                seen["n"] = len(cands)
                return [False] * len(cands)

            sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company="anzhee",
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки маркетплейс"]),
                current_date="2026-06-13", max_digests=3,
                markup_resolver=lambda s: ("anzhee", None),
                sensitive_classifier=_counting_clf, today=_TODAY)
            self.assertLessEqual(seen["n"], sm._CROSS_POOL_CAP)

    def test_format_respects_max_chars(self):
        """M-лимит: при тесном бюджете блок ≤ max_chars и ХВОСТ (наименее
        релевантная выжимка) выкинут целиком."""
        digests = [
            {"series": "s1", "date": "2026-06-10", "themes": ["Поставки"],
             "key_points": ["Пункт про контейнеры на таможне"]},
            {"series": "s2", "date": "2026-06-11", "themes": ["Маркетплейс"],
             "key_points": ["Пункт про конверсию недели"]},
            {"series": "s3", "date": "2026-06-12", "themes": ["Бюджет"],
             "key_points": ["Пункт про распределение средств"]},
        ]
        full = sm.format_cross_memory_block(digests, max_chars=100000)
        self.assertIn("s3", full)  # без лимита влезают все три
        tight = sm.format_cross_memory_block(digests, max_chars=len(full) - 40)
        self.assertNotEqual(tight, "")
        self.assertIn("ФОН", tight)
        self.assertLessEqual(len(tight), len(full) - 40)
        self.assertIn("s1", tight)       # самая релевантная (голова) осталась
        self.assertNotIn("s3", tight)    # хвост выкинут целиком

    def test_format_empty_is_blank(self):
        self.assertEqual(sm.format_cross_memory_block([]), "")

    def test_format_omits_other_meeting_participants(self):
        """Приватность/egress: имена участников ДРУГИХ встреч в блок не выводятся."""
        digests = [{"series": "s1", "date": "2026-06-10",
                    "participants": ["Конфиденциальный Гость"],
                    "themes": ["Поставки"], "key_points": ["Пункт"]}]
        block = sm.format_cross_memory_block(digests)
        self.assertNotIn("Конфиденциальный Гость", block)


# ===========================================================================
# G11 — фильтр чувствительного: ЧУВСТВИТЕЛЬНОЕ НЕ ПРОТЕКАЕТ (deploy-гейт)
# ===========================================================================
class TestSensitiveDoesNotLeak(unittest.TestCase):
    """Deploy-гейт плана: подготовленный чувствительный фрагмент НЕ появляется в
    фоне другой встречи — двумя независимыми guard'ами (ручной маркер + LLM G11)."""

    def test_manual_private_marker_excludes_whole_series(self):
        """Серия с `visibility=private` («приватное — не использовать как фон»)
        в пул не берётся — даже если она максимально релевантна."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "anzhee-coord"
            cur.mkdir()
            _write_digest(root / "hr-private", "2026-06-10", series="hr-private",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки кадры"],
                          key_points=["СЕКРЕТ-УВОЛЬНЕНИЕ Петрова согласовали"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company="anzhee",
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки"]),
                current_date="2026-06-13",
                markup_resolver=lambda s: ("anzhee", "private") if s == "hr-private" else (None, None),
                sensitive_classifier=_all_safe, today=_TODAY)
            self.assertEqual(sel, [])
            block = sm.format_cross_memory_block(sel)
            self.assertNotIn("СЕКРЕТ-УВОЛЬНЕНИЕ", block)

    def test_classifier_flag_excludes_candidate(self):
        """G11 LLM: помеченная чувствительной выжимка не идёт в фон и в блок."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "anzhee-coord"
            cur.mkdir()
            _write_digest(root / "anzhee-ok", "2026-06-10", series="anzhee-ok",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки маркетплейс"],
                          key_points=["Контейнер на таможне"])
            _write_digest(root / "anzhee-hr", "2026-06-11", series="anzhee-hr",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки команда"],
                          key_points=["СЕКРЕТ-УВОЛЬНЕНИЕ обсудили"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company="anzhee",
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки маркетплейс"]),
                current_date="2026-06-13", max_digests=5,
                markup_resolver=lambda s: ("anzhee", None),
                sensitive_classifier=_flag_if("СЕКРЕТ-УВОЛЬНЕНИЕ"), today=_TODAY)
            slugs = [d.get("series") for d in sel]
            self.assertIn("anzhee-ok", slugs)
            self.assertNotIn("anzhee-hr", slugs)
            self.assertNotIn("СЕКРЕТ-УВОЛЬНЕНИЕ", sm.format_cross_memory_block(sel))

    def test_no_classifier_returns_empty(self):
        """G11 — главный guard: без классификатора фон НЕ отдаётся (консервативно)."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "cur"
            cur.mkdir()
            _write_digest(root / "anzhee-b", "2026-06-10", series="anzhee-b",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки"], key_points=["Пункт"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company=None,
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки"]),
                current_date="2026-06-13",
                markup_resolver=lambda s: (None, None),
                sensitive_classifier=None, today=_TODAY)
            self.assertEqual(sel, [])

    def test_classifier_length_mismatch_excludes_all(self):
        """Несоответствие длины вердикта = доверять нельзя → исключить всё."""
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "cur"
            cur.mkdir()
            _write_digest(root / "anzhee-b", "2026-06-10", series="anzhee-b",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки"], key_points=["Пункт"])
            sel = sm.resolve_cross_memory(
                root, current_series_dir=cur, current_company=None,
                current_participants=["Илья Рыбалка", "Михаил Саргин"],
                current_topic_tokens=sm._topic_tokens(["Поставки"]),
                current_date="2026-06-13",
                markup_resolver=lambda s: (None, None),
                sensitive_classifier=lambda c: [False, False, False],  # длиннее пула
                today=_TODAY)
            self.assertEqual(sel, [])


# ===========================================================================
# G11 — LLM-классификатор (llm_postprocess.classify_sensitive_memory)
# ===========================================================================
class TestSensitiveClassifier(unittest.TestCase):

    def test_flags_from_json_object(self):
        cands = [{"themes": ["Поставки"], "key_points": ["Контейнер"]},
                 {"themes": ["Кадры"], "key_points": ["Увольнение"]}]
        with mock.patch.object(lp, "call_claude_print",
                               return_value='{"sensitive":[2]}'):
            self.assertEqual(lp.classify_sensitive_memory(cands), [False, True])

    def test_timeout_is_conservative_exclude_all(self):
        """Своя защита от таймаута на call-site → все True (исключить всё)."""
        cands = [{"themes": ["a"]}, {"themes": ["b"]}]
        with mock.patch.object(lp, "call_claude_print",
                               side_effect=claude_cli.ClaudeCliTimeout("boom")):
            self.assertEqual(lp.classify_sensitive_memory(cands), [True, True])

    def test_cli_error_is_conservative(self):
        cands = [{"themes": ["a"]}]
        with mock.patch.object(lp, "call_claude_print",
                               side_effect=claude_cli.ClaudeCliFailed("exit 1")):
            self.assertEqual(lp.classify_sensitive_memory(cands), [True])

    def test_parse_failure_is_conservative(self):
        cands = [{"themes": ["a"]}, {"themes": ["b"]}]
        with mock.patch.object(lp, "call_claude_print",
                               return_value="бла-бла без json"):
            self.assertEqual(lp.classify_sensitive_memory(cands), [True, True])

    def test_empty_candidates_no_claude_call(self):
        """Пустой список → [] и claude НЕ зовётся (cost)."""
        with mock.patch.object(lp, "call_claude_print") as m:
            self.assertEqual(lp.classify_sensitive_memory([]), [])
            m.assert_not_called()

    def test_prompt_carries_only_digest_text_not_raw_transcript(self):
        """Опасная тройка: в промпт идут ТОЛЬКО выжимки (темы/пункты)."""
        captured = {}

        def _fake(prompt, *, system, timeout, model):
            captured["prompt"] = prompt
            captured["system"] = system
            return '{"sensitive":[]}'

        cands = [{"themes": ["Поставки"], "key_points": ["Контейнер на таможне"]}]
        with mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            lp.classify_sensitive_memory(cands)
        self.assertIn("Контейнер на таможне", captured["prompt"])
        self.assertIn("приватности", captured["system"])


# ===========================================================================
# G10 (scope) — приватность: текст НЕ логируется (только счётчики)
# ===========================================================================
class TestCrossMemoryPrivacyLogging(unittest.TestCase):

    def test_resolver_does_not_log_candidate_text(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "cur"
            cur.mkdir()
            secret = "СЕКРЕТ-РЕПЛИКА-кросс-фон-не-логируй"
            _write_digest(root / "anzhee-b", "2026-06-10", series="anzhee-b",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки"], key_points=[secret])
            with self.assertLogs(sm.logger, level="DEBUG") as ctx:
                sm.resolve_cross_memory(
                    root, current_series_dir=cur, current_company=None,
                    current_participants=["Илья Рыбалка", "Михаил Саргин"],
                    current_topic_tokens=sm._topic_tokens(["Поставки"]),
                    current_date="2026-06-13",
                    markup_resolver=lambda s: (None, None),
                    sensitive_classifier=_all_safe, today=_TODAY)
                sm.logger.debug("anchor")  # гарантируем непустой лог-контекст
            self.assertNotIn(secret, "\n".join(ctx.output))

    def test_classifier_does_not_log_candidate_or_response_text(self):
        secret = "СЕКРЕТ-выжимка-классификатор"
        cands = [{"themes": [secret], "key_points": ["ответ тоже секрет"]}]
        with self.assertLogs(lp.logger, level="DEBUG") as ctx:
            with mock.patch.object(lp, "call_claude_print",
                                   return_value='{"sensitive":[1]}'):
                lp.classify_sensitive_memory(cands, meeting_sid="m1")
        blob = "\n".join(ctx.output)
        self.assertNotIn(secret, blob)
        self.assertNotIn("ответ тоже секрет", blob)


# ===========================================================================
# Оркестратор build_cross_memory_block — реальный путь finalize/clarify
# ===========================================================================
class TestBuildCrossMemoryBlock(unittest.TestCase):

    def _patched_markup(self, company_map, vis_map):
        """Мокаем реестр: company_for_series/markup_for_series/_load_watched_safe."""
        def _company(series, *, watched=None):
            return company_map.get(series)

        def _markup(series, *, watched=None):
            return company_map.get(series), vis_map.get(series)

        return [
            mock.patch.object(lp.series_markup, "company_for_series", side_effect=_company),
            mock.patch.object(lp.series_markup, "markup_for_series", side_effect=_markup),
            mock.patch.object(lp.series_markup, "_load_watched_safe", return_value={"watched": []}),
        ]

    def test_end_to_end_excludes_private_includes_relevant(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            cur = root / "anzhee-coord"
            cur.mkdir()
            _write_digest(root / "anzhee-logi", "2026-06-10", series="anzhee-logi",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки маркетплейс"],
                          key_points=["Контейнер на таможне"])
            _write_digest(root / "hr-priv", "2026-06-09", series="hr-priv",
                          participants=["Илья Рыбалка", "Михаил Саргин"],
                          themes=["Поставки кадры"],
                          key_points=["СЕКРЕТ-УВОЛЬНЕНИЕ Петрова"])
            patches = self._patched_markup(
                {"anzhee-coord": "anzhee", "anzhee-logi": "anzhee", "hr-priv": "anzhee"},
                {"hr-priv": "private"})
            same_series = [{"series": "anzhee-coord", "date": "2026-06-12",
                            "themes": ["Поставки"], "key_points": []}]
            with patches[0], patches[1], patches[2], \
                    mock.patch.object(lp, "call_claude_print", return_value='{"sensitive":[]}'):
                block = lp.build_cross_memory_block(
                    root, cur, {"series": "anzhee-coord", "date": "2026-06-13"},
                    current_participants=["Илья Рыбалка", "Михаил Саргин"],
                    same_series_digests=same_series, meeting_sid="m1")
            self.assertIn("anzhee-logi", block)
            self.assertNotIn("СЕКРЕТ-УВОЛЬНЕНИЕ", block)
            self.assertNotIn("hr-priv", block)

    def test_killswitch_disabled_returns_blank(self):
        with mock.patch.dict("os.environ", {"ENABLE_CROSS_MEMORY": "0"}):
            self.assertEqual(
                lp.build_cross_memory_block(Path("/tmp"), Path("/tmp/x"),
                                            {"series": "s"}), "")

    def test_no_series_slug_returns_blank(self):
        self.assertEqual(
            lp.build_cross_memory_block(Path("/tmp"), Path("/tmp/x"), {"series": ""}), "")

    def test_best_effort_on_internal_error(self):
        """Любой сбой внутри → "" (генерацию не роняем)."""
        with mock.patch.object(lp.series_memory, "resolve_cross_memory",
                               side_effect=RuntimeError("boom")), \
                mock.patch.object(lp.series_markup, "_load_watched_safe", return_value=None), \
                mock.patch.object(lp.series_markup, "company_for_series", return_value=None):
            self.assertEqual(
                lp.build_cross_memory_block(Path("/tmp"), Path("/tmp/x"),
                                            {"series": "s", "date": "2026-06-13"}), "")


# ===========================================================================
# Реальный путь до промпта: блок ФОН доходит до user-промпта генерации
# ===========================================================================
class TestCrossBlockReachesPrompt(unittest.TestCase):

    def test_folded_cross_block_in_generation_prompt(self):
        """Кросс-фон складывается в `series_memory` → доходит до промпта генерации
        (тот же канал, что блок памяти серии — уже доказан в Ф7-сьюте)."""
        captured = {}

        def _fake(user_prompt, *, system, timeout, model):
            captured["user"] = user_prompt
            return ("#протоколвстречи 13.06.2026\n\n**Встреча:** Тест.\n\n"
                    "**Длительность:** 10 мин\n\n**Участники:** Михаил Саргин\n\n"
                    "**Транскрипт:** [2026-06-13.md](2026-06-13.md)\n\n---\n\n"
                    "## 1) Тема\n\n▪️ Пункт.\n")

        cross = sm.format_cross_memory_block([{
            "series": "anzhee-logi", "date": "2026-06-10",
            "themes": ["Поставки маркетплейс"], "key_points": ["Контейнер"]}])
        combined = "\n\n".join(p for p in ("", cross.strip()) if p)
        meta = {"series": "anzhee-coord", "date": "2026-06-13"}
        with mock.patch.object(lp, "call_claude_print", side_effect=_fake):
            lp.generate_protocol("транскрипт", meta, method_text="М",
                                 series_memory=combined, meeting_sid="cross1")
        self.assertIn("ФОН — связанные встречи", captured["user"])
        self.assertIn("anzhee-logi", captured["user"])

    def test_wiring_present_in_finalize_and_clarify(self):
        """Гард wiring: оба call-site реально зовут build_cross_memory_block и
        складывают результат в series_memory_block (как блок памяти серии)."""
        fin = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        clr = (_NOTARY / "lib" / "clarify_worker.py").read_text(encoding="utf-8")
        for src, name in ((fin, "finalize"), (clr, "clarify_worker")):
            self.assertIn("build_cross_memory_block(", src,
                          f"{name} должен звать build_cross_memory_block")
            self.assertIn("series_memory_block", src)


if __name__ == "__main__":
    unittest.main()
