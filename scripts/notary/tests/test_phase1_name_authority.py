"""Тесты Ф1 плана `2026-06-16-notary-name-authority-targeted-edits` (ISS-11):
авторитетное определение имени — приоритет источников, лечение ядовитого якоря,
дизамбигуация тёзок, сужение name_pool, справочник людей.

Покрывает REQ:
  - R18 — справочник людей `people:` в org-structure.yaml (статус/канон/алиасы) +
          парсер + бэкомпат (запись без status = активный) + фикстуры.
  - R19 — тотальный порядок источников имени: правка-факт > ростер-домен > якорь >
          S1 > S2 > LLM (детерминированный конфликт).
  - R1  — достоверный company-факт (роль/ростер) перебивает устаревший якорь.
  - R2  — «ядовитый» якорь серии, противоречащий ростеру, не проносится.
  - R3  — дизамбигуация тёзок: вероятный по роли подставлен + ⚠️ «авторство под
          вопросом, поправьте» (system-applied, content-hash, оба call-site).
  - R4  — name_pool LLM-добивки сужен (минус неактивные/негативный ростер), но по
          ГОЛОСУ: панель не жёсткий фильтр (телефонный участник сохраняется).
  - R5  — капабилити inactive: реально УШЕДШИЙ из компании человек (синтетический
          «Пётр Уволенный») помечен inactive в справочнике → выпадает из пула/не
          подставляется. NB: «Михаил Еремеев» НЕ inactive — это искажённая фамилия
          присутствующего Михаила Саргина (см. R2-якорь); разовое отсутствие → R9.

Дисциплина «опасной тройки»: фикстуры синтетические (доменная лексика, не ПДн).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase1_name_authority -v
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

# name_mapping → align тянет тяжёлые пакеты, которых нет в CI/маке. Стабим (как
# делает test_roster_domain_mapping).
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib.align import AlignedTurn  # noqa: E402
from lib import name_mapping as nm  # noqa: E402
from lib import series_roster as sr  # noqa: E402
from lib import context_knowledge as ck  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402

_FIXTURE_CONTEXT = _HERE / "fixtures" / "context"
SLUG = sr.ANZHEE_COORDINATION_SLUG
ROSTER = sr.get_roster(SLUG)

MARIA = "Мария Михина"        # поставки
SONA = "Сона Енгибарян"       # коммерция
SARGIN = "Михаил Саргин"      # сервис (активный)
DARIA = "Дарья Набережная"    # резервы
OLGA = "Ольга Новикова"       # финансы
EREMEEV = "Михаил Еремеев"    # НЕ отдельный человек: искажённая фамилия present-Михаила
                              # (Саргина). Используется только в R2 как «ядовитый якорь».
DEPARTED = "Пётр Уволенный"   # синтетический УШЕДШИЙ из компании — носитель капабилити inactive
# Синтетический тёзка Саргина по первому слову («Михаил»). Используется в R3 как
# второй Михаил в пуле — без утверждения, что это реальный человек Anzhee.
MIKHAIL_NAMESAKE = "Михаил Тёзкин"
# Синтетический УШЕДШИЙ Михаил-тёзка — носитель капабилити «inactive выпадает из
# группы тёзок, активный подставляется». Не Еремеев (тот не отдельный человек).
MIKHAIL_DEPARTED = "Михаил Уволенный"
ILYA = "Илья Рыбалка"         # владелец — не в ростере


def _t(speaker, text, *, start=0.0):
    return AlignedTurn(start=start, end=start + 1.0, speaker=speaker, text=text)


SERVICE_TXT = (
    "Сервис и обслуживание: ремонт по гарантии закрыли, "
    "провёл обучение ателье, монтаж оборудования на объекте."
)
# Слабый сервис-сигнал: ровно ОДНО ключевое слово домена («сервис») — НИЖЕ строгого
# порога map_from_roster_domain (2 разных). Доменный маппинг такой кластер пропускает,
# его подхватывает дизамбигуация тёзок (R3) с пометкой неуверенности.
WEAK_SERVICE_TXT = "По сервису пара слов, дальше общие организационные вопросы."
FINANCE_TXT = "Финансы: план платежей на неделю, оплата юаней, кредит и ковенанта."
GENERIC_TXT = "Коллеги, начинаем. Послушаем всех по очереди и подведём итоги."


def _real_yaml():
    try:
        import yaml
    except ImportError:
        return None
    return yaml if callable(getattr(yaml, "safe_load", None)) else None


# ==========================================================================
# R18 — справочник людей: парсер (инъекция dict, без YAML — всегда зелёный)
# ==========================================================================
class TestPeopleDirectoryParser(unittest.TestCase):
    DATA = {
        "people": [
            {"name": "Михаил Саргин", "status": "active", "aliases": ["Михаил С."]},
            # Капабилити inactive на синтетическом УШЕДШЕМ из компании человеке
            # (не на Еремееве — тот не отдельный человек, а искажённая фамилия Саргина).
            {"name": "Пётр Уволенный", "status": "inactive", "aliases": ["Уволенный"]},
            {"name": "Илья Рыбалка", "aliases": ["Илья"]},   # без status → active
            {"name": "  ", "status": "active"},               # пустое имя → отброшено
            "не-словарь",                                      # мусор → отброшен
        ]
    }

    def test_parses_status_and_aliases(self):
        people = ck.parse_people(self.DATA)
        by_name = {p["name"]: p for p in people}
        self.assertEqual(len(people), 3)
        self.assertEqual(by_name["Михаил Саргин"]["status"], ck.PERSON_STATUS_ACTIVE)
        self.assertEqual(by_name["Пётр Уволенный"]["status"], ck.PERSON_STATUS_INACTIVE)
        self.assertEqual(by_name["Михаил Саргин"]["aliases"], ["Михаил С."])

    def test_backcompat_no_status_is_active(self):
        # Старая запись без поля status трактуется как активный участник (R18).
        people = ck.parse_people(self.DATA)
        ilya = next(p for p in people if p["name"] == ILYA)
        self.assertEqual(ilya["status"], ck.PERSON_STATUS_ACTIVE)

    def test_accepts_raw_list_too(self):
        # Принимает и весь dict, и уже извлечённый список (удобно тестам).
        people = ck.parse_people(self.DATA["people"])
        self.assertEqual(len(people), 3)

    def test_unknown_status_defaults_active(self):
        # Неизвестное значение НЕ выключает человека по ошибке (только inactive выключает).
        people = ck.parse_people({"people": [{"name": "X", "status": "frozen"}]})
        self.assertEqual(people[0]["status"], ck.PERSON_STATUS_ACTIVE)

    def test_no_people_section_empty(self):
        self.assertEqual(ck.parse_people({"rosters": {}}), [])
        self.assertEqual(ck.parse_people({"people": "не-список"}), [])
        self.assertEqual(ck.parse_people(None), [])

    def test_inactive_names_set_via_monkeypatch(self):
        # inactive_person_names собирает канон + алиасы неактивных (lowercase).
        # Капабилити проверяется на синтетическом УШЕДШЕМ человеке (Пётр Уволенный).
        orig = ck.load_people
        ck.load_people = lambda company: ck.parse_people(self.DATA)
        try:
            inact = ck.inactive_person_names("anzhee")
            self.assertIn("пётр уволенный", inact)
            self.assertIn("уволенный", inact)            # алиас тоже
            self.assertNotIn("михаил саргин", inact)     # активный — не в наборе
            self.assertTrue(ck.is_name_inactive("Пётр Уволенный", "anzhee"))
            self.assertFalse(ck.is_name_inactive("Михаил Саргин", "anzhee"))
            # bare «Пётр» НЕ inactive (строгий матч полного имени, не первословный).
            self.assertFalse(ck.is_name_inactive("Пётр", "anzhee"))
        finally:
            ck.load_people = orig


# ==========================================================================
# R18/R5 — справочник людей из YAML-фикстуры (skip без pyyaml)
# ==========================================================================
class TestPeopleDirectoryYaml(unittest.TestCase):
    def setUp(self):
        if not _real_yaml():
            self.skipTest("pyyaml недоступен/заглушён — фикстурный тест пропущен")
        self._saved = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_FIXTURE_CONTEXT)

    def tearDown(self):
        if getattr(self, "_saved", "__unset__") == "__unset__":
            return
        if self._saved is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._saved

    def test_anzhee_people_loaded(self):
        people = ck.load_people("anzhee")
        by_name = {p["name"]: p for p in people}
        self.assertIn(SARGIN, by_name)
        self.assertEqual(by_name[SARGIN]["status"], ck.PERSON_STATUS_ACTIVE)
        # Бэкомпат: запись без status (Илья) = активный.
        self.assertEqual(by_name[ILYA]["status"], ck.PERSON_STATUS_ACTIVE)
        # «Михаил Еремеев» в справочнике НЕТ: это искажённая фамилия present-Михаила
        # (Саргина), а не отдельный ушедший человек. Никто не помечен inactive по ошибке.
        self.assertNotIn(EREMEEV, by_name)

    def test_no_one_falsely_inactive(self):
        # В боевой фикстуре никто не помечен inactive (нет реально ушедших). В частности
        # Еремеев не утверждается как неактивный человек.
        inact = ck.inactive_person_names("anzhee")
        self.assertNotIn(EREMEEV.lower(), inact)
        self.assertNotIn(SARGIN.lower(), inact)

    def test_mpfirst_people_stub_empty(self):
        # Стаб МПервый: people: [] → парсер отдаёт [] (graceful, не падает).
        self.assertEqual(ck.load_people("mpfirst"), [])


# ==========================================================================
# R19 — тотальный порядок источников + R1/R2 (ядовитый якорь)
# ==========================================================================
class TestPriorityOrderR19(unittest.TestCase):
    def test_edit_fact_is_highest(self):
        # R19: правка-факт бьёт всё — даже доменный ростер.
        turns = [_t("SPEAKER_02", SERVICE_TXT)]
        pool = [SARGIN, OLGA]
        res = nm.map_all(
            turns, pool, roster=ROSTER, present=pool,
            edit_facts={"SPEAKER_02": OLGA},  # человек сказал «это Ольга»
        )
        self.assertEqual(res.cluster_to_name["SPEAKER_02"], OLGA)
        self.assertIn("edit_facts", res.sources_used)

    def test_roster_above_anchor_neutralizes_poison(self):
        # R2: ядовитый якорь {service-cluster → Еремеев} при ростере сервис→Саргин
        # и доменной речи про сервис → итог Саргин (якорь НЕ перебивает ростер).
        turns = [_t("SPEAKER_02", SERVICE_TXT), _t("SPEAKER_04", FINANCE_TXT)]
        pool = [SARGIN, OLGA, MARIA]   # Еремеева на встрече нет
        res = nm.map_all(
            turns, pool, anchor={"SPEAKER_02": EREMEEV}, roster=ROSTER, present=pool,
        )
        self.assertEqual(res.cluster_to_name["SPEAKER_02"], SARGIN)
        self.assertNotIn(EREMEEV, res.cluster_to_name.values())
        self.assertIn("roster_domain", res.sources_used)

    def test_r1_company_fact_beats_stale_anchor(self):
        # R1: достоверный company-факт о роли (сервис→Саргин) перебивает якорь
        # прошлой встречи, нёсший другое имя на тот же кластер.
        turns = [_t("SPEAKER_02", SERVICE_TXT)]
        pool = [SARGIN, MARIA, OLGA]
        res = nm.map_all(
            turns, pool, anchor={"SPEAKER_02": MARIA}, roster=ROSTER, present=pool,
        )
        self.assertEqual(res.cluster_to_name["SPEAKER_02"], SARGIN)

    def test_anchor_still_applies_where_roster_silent(self):
        # Якорь не выключен: добивает кластер, который ростер не достаёт (генерик).
        turns = [_t("SPEAKER_02", SERVICE_TXT), _t("SPEAKER_05", GENERIC_TXT)]
        pool = [SARGIN, ILYA, OLGA]
        res = nm.map_all(
            turns, pool, anchor={"SPEAKER_05": ILYA}, roster=ROSTER, present=pool,
        )
        self.assertEqual(res.cluster_to_name["SPEAKER_02"], SARGIN)   # ростер
        self.assertEqual(res.cluster_to_name["SPEAKER_05"], ILYA)     # якорь
        self.assertIn("series_anchor", res.sources_used)

    def test_anchor_skips_name_taken_by_roster(self):
        # Якорь не может назначить имя, уже занятое ростером (one-to-one, R19).
        turns = [_t("SPEAKER_02", SERVICE_TXT), _t("SPEAKER_03", GENERIC_TXT)]
        pool = [SARGIN, OLGA]
        # Якорь хочет повесить Саргина на ДРУГОЙ кластер — но Саргин уже у ростера.
        res = nm.map_all(
            turns, pool, anchor={"SPEAKER_03": SARGIN}, roster=ROSTER, present=pool,
        )
        self.assertEqual(res.cluster_to_name["SPEAKER_02"], SARGIN)
        self.assertNotEqual(res.cluster_to_name.get("SPEAKER_03"), SARGIN)


# ==========================================================================
# R3 — дизамбигуация тёзок (вероятный по роли + неуверенность)
# ==========================================================================
class TestNamesakeDisambiguationR3(unittest.TestCase):
    def test_inactive_namesake_excluded_active_substituted(self):
        # Капабилити: два «Михаил» в пуле, кластер со слабым сервис-сигналом (без строго
        # разделяющего домена), синтетический УШЕДШИЙ Михаил inactive → подставлен
        # активный Саргин. (Демонстрирует механизм inactive на реально-ушедшем тёзке.)
        turns = [_t("SPEAKER_07", WEAK_SERVICE_TXT)]
        pool = [SARGIN, MIKHAIL_DEPARTED, OLGA]
        d = nm.disambiguate_namesakes(
            turns, pool, {}, roster=ROSTER, present=pool,
            inactive={MIKHAIL_DEPARTED.lower()},
        )
        self.assertEqual(d, {"SPEAKER_07": SARGIN})

    def test_role_discriminates_between_active_namesakes(self):
        # Оба Михаила активны, но роль сервиса есть только у Саргина (второго тёзки в
        # ростере нет) → «вероятный по роли» = Саргин.
        turns = [_t("SPEAKER_07", WEAK_SERVICE_TXT)]
        pool = [SARGIN, MIKHAIL_NAMESAKE, OLGA]
        d = nm.disambiguate_namesakes(
            turns, pool, {}, roster=ROSTER, present=pool, inactive=set(),
        )
        self.assertEqual(d.get("SPEAKER_07"), SARGIN)

    def test_no_domain_signal_abstains(self):
        # Нет доменного сигнала к группе тёзок → не угадываем (отдаём clarify/LLM).
        turns = [_t("SPEAKER_09", GENERIC_TXT)]
        d = nm.disambiguate_namesakes(
            turns, [SARGIN, MIKHAIL_NAMESAKE], {}, roster=ROSTER,
            present=[SARGIN, MIKHAIL_NAMESAKE], inactive=set(),
        )
        self.assertEqual(d, {})

    def test_no_namesake_group_noop(self):
        # Нет тёзок (одно «Михаил») → дизамбигуация не вмешивается.
        turns = [_t("SPEAKER_07", WEAK_SERVICE_TXT)]
        d = nm.disambiguate_namesakes(
            turns, [SARGIN, OLGA], {}, roster=ROSTER, present=[SARGIN, OLGA], inactive=set(),
        )
        self.assertEqual(d, {})

    def test_map_all_marks_uncertain(self):
        # Через map_all: тёзка-подстановка попадает в uncertain_clusters (для ⚠️).
        turns = [_t("SPEAKER_07", WEAK_SERVICE_TXT)]
        pool = [SARGIN, MIKHAIL_DEPARTED, OLGA]
        res = nm.map_all(turns, pool, roster=ROSTER, present=pool,
                         inactive={MIKHAIL_DEPARTED.lower()})
        self.assertEqual(res.cluster_to_name["SPEAKER_07"], SARGIN)
        self.assertIn("SPEAKER_07", res.uncertain_clusters)
        self.assertIn("namesake_disambig", res.sources_used)

    def test_strict_vocative_blocks_namesake(self):
        # Кластер сам строго окликнул «Михаил, …» → он НЕ Михаил, не подставляем.
        turns = [_t("SPEAKER_07", "Михаил, " + WEAK_SERVICE_TXT)]
        pool = [SARGIN, MIKHAIL_DEPARTED, OLGA]
        d = nm.disambiguate_namesakes(
            turns, pool, {}, roster=ROSTER, present=pool,
            inactive={MIKHAIL_DEPARTED.lower()},
        )
        self.assertNotIn("SPEAKER_07", d)


# ==========================================================================
# R3 — system-applied ⚠️ «авторство под вопросом, поправьте»
# ==========================================================================
class TestAuthorshipFlagR3(unittest.TestCase):
    PROTOCOL = (
        "# Протокол координации\n\n"
        "**Участники:** Михаил Саргин, Ольга Новикова\n\n"
        "## Сервис\n\n- Михаил Саргин закрыл рекламации.\n"
    )

    def test_findings_built(self):
        f = lp.build_authorship_uncertainty_findings([SARGIN, SARGIN, ""])
        self.assertEqual(len(f), 1)  # дедуп + пустые отброшены
        self.assertEqual(f[0]["section"], "authorship")
        self.assertEqual(f[0]["quote"], SARGIN)
        self.assertEqual(f[0]["note"], lp.AUTHORSHIP_FLAG_NOTE)

    def test_flag_applied_to_protocol(self):
        findings = lp.build_authorship_uncertainty_findings([SARGIN])
        out = lp.apply_review_flags(self.PROTOCOL, findings)
        self.assertIn("авторство под вопросом, поправьте", out)
        self.assertIn(SARGIN, out)
        self.assertIn(lp.REVIEW_FLAG_MARKER, out)
        # В хвостовом блоке «## ⚠️ Проверить».
        self.assertIn(f"## {lp.REVIEW_FLAG_MARKER} Проверить", out)

    def test_flag_enters_content_hash(self):
        # ⚠️-пометка — смысловое изменение → меняет content-hash (revision-идемпотентность).
        findings = lp.build_authorship_uncertainty_findings([SARGIN])
        out = lp.apply_review_flags(self.PROTOCOL, findings)
        self.assertNotEqual(
            lp._protocol_content_hash(self.PROTOCOL),
            lp._protocol_content_hash(out),
        )

    def test_flag_idempotent(self):
        findings = lp.build_authorship_uncertainty_findings([SARGIN])
        once = lp.apply_review_flags(self.PROTOCOL, findings)
        twice = lp.apply_review_flags(once, findings)
        self.assertEqual(once.count("авторство под вопросом, поправьте"),
                         twice.count("авторство под вопросом, поправьте"))


# ==========================================================================
# R4 — name_pool LLM-добивки: union по голосу, минус неактивные/негативные
# ==========================================================================
class TestNamePoolNarrowingR4(unittest.TestCase):
    def test_inactive_dropped_active_namesake_kept(self):
        # Капабилити на синтетическом УШЕДШЕМ Михаиле-тёзке: неактивный выпадает,
        # активный тёзка (Саргин) сохраняется.
        pool, dropped = lp._build_llm_name_pool(
            [SARGIN, MIKHAIL_DEPARTED, OLGA], [], {},
            inactive_names={MIKHAIL_DEPARTED.lower()},
        )
        self.assertIn(SARGIN, pool)            # активный тёзка сохранён
        self.assertNotIn(MIKHAIL_DEPARTED, pool)  # неактивный выпал (R4/R5)
        self.assertEqual(dropped, 1)

    def test_panel_only_voiced_attendee_kept(self):
        # Решение владельца A6: участник со своим голосом, но без тайла в панели
        # (телефонный / один экран на двоих) — сохраняется. Пул = expected ∪ panel.
        pool, _ = lp._build_llm_name_pool(
            [MARIA], ["Инна Белобородова"], {},
        )
        self.assertIn(MARIA, pool)
        self.assertIn("Инна Белобородова", pool)

    def test_bare_namesake_not_dropped_as_inactive(self):
        # Строгий матч: bare «Михаил» НЕ отсекается как неактивный «Михаил Уволенный»
        # (иначе затёрли бы присутствующего Саргина, записанного коротко).
        pool, _ = lp._build_llm_name_pool(
            ["Михаил"], [], {}, inactive_names={MIKHAIL_DEPARTED.lower()},
        )
        self.assertIn("Михаил", pool)

    def test_already_mapped_excluded(self):
        pool, _ = lp._build_llm_name_pool(
            [SARGIN, OLGA], [], {"SPEAKER_00": OLGA},
        )
        self.assertNotIn(OLGA, pool)
        self.assertIn(SARGIN, pool)

    def test_absent_names_placeholder_drops(self):
        # Ф2-заготовка негативного ростера текущей встречи: absent_names исключает.
        pool, dropped = lp._build_llm_name_pool(
            [SARGIN, OLGA], [], {}, absent_names={OLGA.lower()},
        )
        self.assertNotIn(OLGA, pool)
        self.assertEqual(dropped, 1)


if __name__ == "__main__":
    unittest.main()
