"""Тесты Ф3 плана `watson-company-memory`: самокоррекция Watson против знания компании.

Покрывает критерии приёмки дословно:
  R6 — черновик с неверным названием товара (есть в каталоге) / неверной ролью (есть в
       оргструктуре) → после прохода в финале канон подставлен верно.
  R7 — спорный факт, которого в знании нет однозначно → Watson НЕ переписывает молча, а
       ставит «⚠️ сверьте: …»; задачи/решения черновика присутствуют в финале (инвариант
       «финал ⊇ прошлой версии» из Ф7 не нарушен).
  R8 (часть) — знание в проходе — ДАННЫЕ (анти-инъекционная рамка); в лог только
       счётчики (сколько правок, какого типа), НЕ текст правок/реплик/знания.

🔴 УПУ1 — корень фазы: до Ф3 `review_and_rewrite_protocol` НЕ получал знание вовсе, и
строка «сверь со знанием компании» в чек-листе критика сверять было не с чем. Поэтому
главный тест здесь — `TestKnowledgeReachesCritic`: справка РЕАЛЬНО доезжает в промпт
критик-вызова. Без него все остальные проверки — театр.

Граница честности: реальную правку делает LLM, в юнит-тесте она замокана. Поэтому
тесты доказывают ровно то, что можно доказать без живого Opus:
  • критику ПОДАНО знание (каталог + бриф) и ПРАВИЛО его применения (пункт 6 + A4);
  • ответ критика с каноном/хеджем доезжает до файла на диске боевым путём;
  • инвариант «не теряем» пережил knowledge-правку;
  • ни знание, ни текст протокола не утекают в лог.
Что критик РЕАЛЬНО поправит на живой модели — вопрос приёмки на боевой встрече, не юнита.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_watson_selfcorrection -v
"""
from __future__ import annotations

import ast
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

for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib import context_knowledge as ck  # noqa: E402
from lib import glossary  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402

_FIXTURE_MANIFEST = _HERE / "fixtures" / "context-manifest"

# Сентинели фикстуры (те же, что в тестах Ф2 — новых фикстур не плодим).
_CATALOG_CANON = "Pulsar 63ML"                  # канон каталога МПервый
_ROLES_SENTINEL = "ROLES_SENTINEL_KOMANDA"      # бриф: кто за что отвечает
_SENSITIVE = "SENSITIVE_SENTINEL_SALARIES_MUST_NOT_LEAK"  # в манифесте есть, в промпт НЕ идёт

_CHECKS = ("values", "roles", "memory", "diarization", "knowledge")
_DISCLAIMER = "> _Авторство реплик распознано автоматически, возможны неточности._"

# Черновик: ASR переврал название товара («Пульсар шестьдесят три») + есть живая задача.
DRAFT = (
    "#протоколвстречи 13.07.2026\n\n"
    + _DISCLAIMER + "\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "▪️ Заказать партию Пульсар шестьдесят три — Михаил, до пятницы.\n"
    "▪️ Свести остатки по складу — Илья.\n"
)

TRANSCRIPT = (
    "#транскрипт 2026-07-13\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "**[00:00] Илья:** Михаил, закажи партию Пульсар шестьдесят три к пятнице.\n\n"
    "**[00:10] Михаил:** Понял, закажу.\n\n"
    "**[00:20] Илья:** Я сведу остатки по складу.\n"
)

# Что вернул бы критик: КАНОН подставлен молча (A4), задачи обе на месте.
IMPROVED_CANON = (
    "#протоколвстречи 13.07.2026\n\n"
    + _DISCLAIMER + "\n\n"
    "**Участники:** Илья, Михаил\n\n"
    "---\n\n"
    "## 1) Задачи\n\n"
    "▪️ Заказать партию Pulsar 63ML — Михаил, до пятницы.\n"
    "▪️ Свести остатки по складу — Илья.\n"
)


def _envelope(protocol: str, *, findings=None, edits=None) -> str:
    """Envelope-ответ второго прохода, как его вернул бы claude."""
    diag = {"findings": findings or [], "edits": edits or {}}
    return (
        lp._REWRITE_DIAG_MARKER + "\n"
        + json.dumps(diag, ensure_ascii=False) + "\n"
        + lp._REWRITE_PROTOCOL_MARKER + "\n"
        + protocol
    )


class _KnowledgeEnv:
    """Боевое окружение Ф3: знание компании из фикстуры + включённый self-review.

    `series_markup.company_for_series` подменён на «серия → mpfirst»: в юните нет
    watched.yaml, а компанию критик выводит именно этой функцией (тем же путём, что
    генерация). Патчим её — а не `context_knowledge.company_for_series` — чтобы тест
    ломался, если код когда-нибудь переедет на оргструктурный резолв и разойдётся с
    генерацией.
    """

    def setUp(self):
        self._saved_ctx = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_FIXTURE_MANIFEST)
        self._env = mock.patch.dict(
            os.environ,
            {"ENABLE_PROTOCOL_REVIEW": "1", "ENABLE_PROTOCOL_SELFREVIEW": "1"},
        )
        self._env.start()
        self._company = mock.patch.object(
            lp.series_markup, "company_for_series", return_value="mpfirst",
        )
        self._company.start()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.transcript = d / "2026-07-13.md"
        self.protocol = d / "2026-07-13-protokol.md"
        self.transcript.write_text(TRANSCRIPT, encoding="utf-8")
        self.protocol.write_text(DRAFT, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()
        self._company.stop()
        self._env.stop()
        if self._saved_ctx is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved_ctx

    def _run(self, response: str, *, checks=_CHECKS, series="mpfirst-weekly"):
        """Боевой путь: та же обёртка, что зовут finalize/clarify/команда/CLI."""
        with mock.patch.object(lp, "call_claude_print", return_value=response) as m:
            n = lp.review_and_flag_protocol_file(
                self.protocol, self.transcript, checks=checks,
                meeting_sid="sid", rewrite=True, series=series,
            )
        self.assertEqual(m.call_count, 1, "критик — РОВНО один claude-вызов")
        self.calls = m.call_args
        return n

    @property
    def user_prompt(self) -> str:
        return self.calls.args[0]

    @property
    def system_prompt(self) -> str:
        return self.calls.kwargs["system"]


# ==========================================================================
# 🔴 УПУ1 — знание РЕАЛЬНО доезжает до критика (корень фазы)
# ==========================================================================
class TestKnowledgeReachesCritic(_KnowledgeEnv, unittest.TestCase):

    def test_catalog_and_brief_are_in_critic_prompt(self):
        """🔴 УПУ1: критик ВИДИТ справку компании — иначе «сверь со знанием» нечем."""
        self._run(_envelope(IMPROVED_CANON))
        self.assertIn(_CATALOG_CANON, self.user_prompt, "каталог не доехал до критика")
        self.assertIn(_ROLES_SENTINEL, self.user_prompt, "бриф (роли) не доехал до критика")
        self.assertIn("СПРАВКА КОМПАНИИ", self.user_prompt)

    def test_checklist_item_present_when_knowledge_given(self):
        """Пункт 6 чек-листа + правило A4 (канон молча / спорное — ⚠️ сверьте)."""
        self._run(_envelope(IMPROVED_CANON))
        sp = self.system_prompt
        self.assertIn("ЗНАНИЕ КОМПАНИИ", sp)
        self.assertIn("ИСПРАВЬ МОЛЧА", sp)     # канон (A4)
        self.assertIn("сверьте", sp)           # спорное (A4)
        self.assertIn("knowledge_fixed", sp)   # счётчик правок в контракте

    def test_no_knowledge_check_no_knowledge_in_prompt(self):
        """Без "knowledge" в checks — поведение ровно как до Ф3 (регресс)."""
        self._run(_envelope(IMPROVED_CANON), checks=("values", "roles", "memory", "diarization"))
        self.assertNotIn(_CATALOG_CANON, self.user_prompt)
        self.assertNotIn("ЗНАНИЕ КОМПАНИИ", self.system_prompt)

    def test_empty_knowledge_means_no_checklist_item(self):
        """Компания не определилась / знания нет → пункта 6 НЕТ.

        Правило «сверь со справкой» БЕЗ справки — это приглашение фантазировать
        (ровно та дыра УПУ1, ради которой фаза и делалась). Молчим, а не просим
        сверяться с пустотой."""
        with mock.patch.object(lp.series_markup, "company_for_series", return_value=None):
            self._run(_envelope(IMPROVED_CANON))
        self.assertNotIn("ЗНАНИЕ КОМПАНИИ", self.system_prompt)
        self.assertNotIn("СПРАВКА КОМПАНИИ", self.user_prompt)

    def test_knowledge_loaded_once_per_pass(self):
        """Инвариант Ф2: знание грузится ОДИН раз за проход (bundle прокидывается,
        не собирается заново на каждый рендер) — иначе диск читается дважды и бюджет
        применяется дважды."""
        real = ck.load_protocol_knowledge
        with mock.patch.object(ck, "load_protocol_knowledge", side_effect=real) as loader:
            self._run(_envelope(IMPROVED_CANON))
        self.assertEqual(loader.call_count, 1, "знание прочитано с диска больше одного раза")


# ==========================================================================
# R6 — канон из знания подставлен в финал (товар + роль)
# ==========================================================================
class TestR6CanonApplied(_KnowledgeEnv, unittest.TestCase):

    def test_wrong_product_name_replaced_by_catalog_canon(self):
        """🔴 R6: неверное название товара (канон есть в каталоге) → в финале канон."""
        n = self._run(_envelope(IMPROVED_CANON, edits={"knowledge_fixed": 1}))
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertIn(_CATALOG_CANON, on_disk)                     # канон подставлен
        self.assertNotIn("Пульсар шестьдесят три", on_disk)        # фонетика ушла
        self.assertGreater(n, 0)

    def test_wrong_role_replaced_by_canon(self):
        """🔴 R6: неверная роль/ответственный (есть в знании) → в финале канон.

        Критик переотносит задачу к верному человеку; проверяем, что правка доезжает
        до диска боевым путём (сам выбор «кто верный» — работа LLM со справкой, она
        подана: см. TestKnowledgeReachesCritic)."""
        fixed_roles = IMPROVED_CANON.replace(
            "▪️ Свести остатки по складу — Илья.",
            "▪️ Свести остатки по складу — Михаил.",
        )
        self._run(_envelope(fixed_roles, edits={"knowledge_fixed": 1}))
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertIn("Свести остатки по складу — Михаил.", on_disk)

    def test_canon_fix_is_silent_no_hedge_no_edit_log_block(self):
        """A4: канон правится МОЛЧА — ни ⚠️-пометки, ни блока «что я поправил»."""
        self._run(_envelope(IMPROVED_CANON, edits={"knowledge_fixed": 1}))
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertNotIn("⚠️", on_disk)
        self.assertNotIn("поправил", on_disk.lower())


# ==========================================================================
# R7 — спорное: ⚠️ сверьте, а НЕ молчаливая переписка; задачи не теряются
# ==========================================================================
class TestR7HedgeNotSilentRewrite(_KnowledgeEnv, unittest.TestCase):

    def test_disputed_fact_gets_visible_hedge(self):
        """🔴 R7: спорного нет в знании однозначно → «⚠️ сверьте: …», текст не тронут."""
        finding = {
            "section": "knowledge",
            "quote": "Свести остатки по складу",
            "note": "склад за Ильёй или за Михаилом",
        }
        self._run(_envelope(DRAFT, findings=[finding]))
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertIn("⚠️ сверьте: склад за Ильёй или за Михаилом", on_disk)
        # Текст пункта НЕ переписан — только помечен.
        self.assertIn("Свести остатки по складу", on_disk)

    def test_hedge_lands_in_tail_block_when_line_not_found(self):
        """Строка под quote не нашлась → пометка не теряется, уходит в хвостовой блок."""
        finding = {
            "section": "knowledge",
            "quote": "СТРОКИ С ТАКИМ ТЕКСТОМ В ПРОТОКОЛЕ НЕТ",
            "note": "модель не сходится со справкой",
        }
        self._run(_envelope(DRAFT, findings=[finding]))
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertIn("## ⚠️ Проверить", on_disk)
        self.assertIn("⚠️ сверьте: модель не сходится со справкой", on_disk)

    def test_action_items_survive_knowledge_correction(self):
        """🔴 R7/ISS-22: самокоррекция НЕ роняет задачи. Черновик: action item +
        неверный товар → финал: товар исправлен, ЗАДАЧА НА МЕСТЕ (инвариант ⊇)."""
        n = self._run(_envelope(IMPROVED_CANON, edits={"knowledge_fixed": 1}))
        on_disk = self.protocol.read_text(encoding="utf-8")
        self.assertIn(_CATALOG_CANON, on_disk)                    # товар исправлен
        self.assertIn("Заказать партию", on_disk)                 # задача 1 на месте
        self.assertIn("— Михаил, до пятницы", on_disk)            # с владельцем и сроком
        self.assertIn("Свести остатки по складу", on_disk)        # задача 2 на месте
        self.assertGreater(n, 0)

    def test_prior_items_invariant_still_holds_with_knowledge(self):
        """Инвариант Ф7 «финал ⊇ прошлой версии» жив вместе с knowledge-проходом:
        обе секции (инвариант + знание) в ОДНОМ system-prompt, не вытесняют друг друга."""
        prior = (
            "#протоколвстречи 06.07.2026\n\n## 1) Задачи\n\n"
            "▪️ Продлить договор с поставщиком — Илья.\n"
        )
        with mock.patch.object(lp, "call_claude_print",
                               return_value=_envelope(IMPROVED_CANON)) as m:
            lp.review_and_flag_protocol_file(
                self.protocol, self.transcript, checks=_CHECKS, meeting_sid="sid",
                rewrite=True, series="mpfirst-weekly", prior_sources=[prior],
            )
        sp = m.call_args.kwargs["system"]
        self.assertIn("НИЧЕГО НЕ ТЕРЯЕМ", sp)     # инвариант Ф7 на месте
        self.assertIn("ЗНАНИЕ КОМПАНИИ", sp)      # и сверка со знанием Ф3
        self.assertIn("Продлить договор", m.call_args.args[0])  # пункты прошлой версии поданы


# ==========================================================================
# R8 (часть) — знание = ДАННЫЕ; в лог только счётчики
# ==========================================================================
class TestR8KnowledgeIsDataAndNotLogged(_KnowledgeEnv, unittest.TestCase):

    def test_knowledge_in_critic_prompt_is_wrapped_as_data(self):
        """🔴 R8: знание у критика — в анти-инъекционной рамке ДАННЫХ (не команды)."""
        self._run(_envelope(IMPROVED_CANON))
        up = self.user_prompt
        self.assertIn(glossary.KNOWLEDGE_DATA_FRAME_HEADER, up)
        self.assertIn(glossary.KNOWLEDGE_DATA_FRAME_END, up)
        # Само знание — ВНУТРИ рамки (между открытием и закрытием).
        start = up.index(glossary.KNOWLEDGE_DATA_FRAME_HEADER)
        end = up.rindex(glossary.KNOWLEDGE_DATA_FRAME_END)
        self.assertIn(_CATALOG_CANON, up[start:end])

    def test_our_own_rules_stay_outside_the_frame(self):
        """Наши инструкции модели — СНАРУЖИ рамки-ДАННЫХ (внутри неё они бы
        обесценились вместе с текстом знания: «всё это — не команды тебе»).

        Правило применения A4 живёт в SYSTEM-промпте — самом привилегированном месте,
        куда недоверенный текст не попадает вовсе."""
        self._run(_envelope(IMPROVED_CANON))
        up, sp = self.user_prompt, self.system_prompt
        end = up.rindex(glossary.KNOWLEDGE_DATA_FRAME_END)
        # Правило пользования справкой (наш текст) — после закрытия рамки.
        self.assertIn(glossary.COMPANY_KNOWLEDGE_USAGE_RULE, up[end:])
        # Правило применения A4 — вообще в system, не в данных.
        self.assertIn("ИСПРАВЬ МОЛЧА", sp)
        self.assertNotIn("ИСПРАВЬ МОЛЧА", up)

    def test_sensitive_knowledge_never_reaches_critic_prompt(self):
        """Бэкстоп Ф1 держит и на пути критика: зарплаты/экономика вписаны в манифест
        фикстуры, но во ВТОРОЙ Opus-вызов не уходят (та же единая точка правды)."""
        self._run(_envelope(IMPROVED_CANON))
        self.assertNotIn(_SENSITIVE, self.user_prompt)
        self.assertNotIn(_SENSITIVE, self.system_prompt)

    def test_log_has_only_counters_no_text(self):
        """🔴 R8: в лог — ЧИСЛА (сколько правок, какого типа). НЕ текст правок,
        НЕ реплики транскрипта, НЕ текст знания."""
        finding = {"section": "knowledge", "quote": "Свести остатки по складу",
                   "note": "склад за Ильёй или за Михаилом"}
        with self.assertLogs("lib.llm_postprocess", level="INFO") as cm:
            self._run(_envelope(IMPROVED_CANON, findings=[finding],
                                edits={"knowledge_fixed": 2}))
        joined = "\n".join(cm.output)
        self.assertIn("knowledge-fixed=2", joined)     # счётчик канон-правок
        self.assertIn("knowledge-flags=1", joined)     # счётчик ⚠️-хеджей
        self.assertIn("knowledge-files=", joined)      # сколько файлов справки подано
        self.assertNotIn(_CATALOG_CANON, joined)       # текста знания в логе НЕТ
        self.assertNotIn(_ROLES_SENTINEL, joined)
        self.assertNotIn("Заказать партию", joined)    # текста протокола НЕТ
        self.assertNotIn("закажи партию", joined)      # текста транскрипта НЕТ
        self.assertNotIn("склад за Ильёй", joined)     # текста правки/хеджа НЕТ


# ==========================================================================
# R8 — статический сторож: текст знания/протокола не уходит в logger
# ==========================================================================
class TestNoTextInLoggerAST(unittest.TestCase):
    """AST-проверка (приём из тестов Ф1): аргументы logger.* в коде самокоррекции —
    только счётчики/метаданные. Тест ловит утечку, которую рантайм-тест выше пропустил
    бы на не пройденной ветке (например в редком except)."""

    _MODULE = _NOTARY / "lib" / "llm_postprocess.py"
    # Имена, чьё ЗНАЧЕНИЕ — недоверенный/чувствительный текст.
    _FORBIDDEN = {
        "protocol_text", "transcript_md", "knowledge_block", "user_prompt",
        "system_prompt", "base_protocol", "flagged", "prior_block", "raw",
    }
    _COUNTER_WRAPPERS = {"len", "sum", "bool", "type", "id", "int"}

    def _leaking_names(self, node: ast.AST) -> list[str]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in self._COUNTER_WRAPPERS:
            return []
        found: list[str] = []
        if isinstance(node, ast.Name) and node.id in self._FORBIDDEN:
            found.append(node.id)
        for child in ast.iter_child_nodes(node):
            found.extend(self._leaking_names(child))
        return found

    def test_logger_calls_in_selfreview_never_take_text(self):
        tree = ast.parse(self._MODULE.read_text(encoding="utf-8"), filename=str(self._MODULE))
        offenders: list[str] = []
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
                    offenders.append(f"llm_postprocess.py:{node.lineno} logger.{func.attr}(… {name} …)")
        self.assertEqual(
            offenders, [],
            "текст протокола/транскрипта/знания уходит в logger — опасная тройка:\n"
            + "\n".join(offenders),
        )

    def test_ast_check_actually_catches_a_leak(self):
        """Мета-тест: проверка выше не бутафория."""
        leak = ast.parse('logger.info("%s", knowledge_block)').body[0].value
        self.assertEqual(self._leaking_names(leak.args[1]), ["knowledge_block"])
        safe = ast.parse('logger.info("%d", len(knowledge_block))').body[0].value
        self.assertEqual(self._leaking_names(safe.args[1]), [])


# ==========================================================================
# НЕС3 — паритет ВСЕХ боевых call-site (память review-checks-two-call-sites)
# ==========================================================================
class TestCallSiteKnowledgeParity(unittest.TestCase):
    """Пропущенный call-site = молчаливая потеря проверки на этом пути. Покрытие
    rewrite=True уже сторожит Ф7 (`TestCallSiteParity`); здесь — что на КАЖДОМ из них
    в checks есть "knowledge" И передан `series` (без series справки не будет — критик
    молча деградирует в поведение до Ф3)."""

    _CALL_SITES = (
        "finalize-meeting.py",
        "lib/clarify_worker.py",
        "meetings_listener.py",
        "tools/regenerate-protocol.py",
    )

    def test_all_call_sites_pass_knowledge_check_and_series(self):
        for rel in self._CALL_SITES:
            src = (_NOTARY / rel).read_text(encoding="utf-8")
            i = src.find("review_and_flag_protocol_file(")
            self.assertNotEqual(i, -1, f"{rel}: вызов не найден")
            chunk = src[i:i + 700]
            self.assertIn('"knowledge"', chunk, f'{rel}: в checks нет "knowledge" (Ф3)')
            self.assertIn("series=", chunk, f"{rel}: не передан series → справки не будет")

    def test_no_fifth_call_site_left_behind(self):
        """4 боевых call-site — исчерпывающий список. Появился 5-й → тест падает, и
        его владелец обязан решить, нужна ли там сверка со знанием (а не узнать об
        этом по кривому протоколу через месяц)."""
        found: list[str] = []
        for path in _NOTARY.rglob("*.py"):
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            src = path.read_text(encoding="utf-8", errors="ignore")
            if "review_and_flag_protocol_file(" in src and "def review_and_flag_protocol_file" not in src:
                found.append(str(path.relative_to(_NOTARY)))
        self.assertEqual(
            sorted(found), sorted(self._CALL_SITES),
            "список боевых call-site изменился — проверь, что новый путь тоже "
            "гонит checks с \"knowledge\" и series (иначе тихая потеря проверки)",
        )


# ==========================================================================
# Деградация: сбой знания не роняет встречу
# ==========================================================================
class TestKnowledgeFailureIsNonFatal(_KnowledgeEnv, unittest.TestCase):

    def test_broken_knowledge_load_does_not_kill_the_pass(self):
        """Знание опционально: развалилось чтение → критик отрабатывает как до Ф3,
        протокол не теряется (best-effort, как весь self-review)."""
        with mock.patch.object(ck, "load_protocol_knowledge",
                               side_effect=OSError("диск отвалился")):
            n = self._run(_envelope(IMPROVED_CANON))
        self.assertIn("Pulsar 63ML", self.protocol.read_text(encoding="utf-8"))
        self.assertGreater(n, 0)
        self.assertNotIn("ЗНАНИЕ КОМПАНИИ", self.system_prompt)  # пункта 6 нет — сверять нечем

    def test_no_series_degrades_silently_to_pre_phase3(self):
        """Серии нет (легаси-встреча без привязки) → проход идёт без справки."""
        with mock.patch.object(lp.series_markup, "company_for_series", return_value=None):
            n = self._run(_envelope(IMPROVED_CANON), series=None)
        self.assertGreater(n, 0)
        self.assertNotIn("СПРАВКА КОМПАНИИ", self.user_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
