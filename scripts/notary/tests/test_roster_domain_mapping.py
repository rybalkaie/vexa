"""Тесты Ф3 (план `2026-06-09-notary-memory-knowledge-rework`): ростер ролей,
доменный маппинг и правило «нет голоса — нет имени».

Покрывает REQ:
  - A4  — спикеры маппятся ПО СМЫСЛУ (домен реплики ↔ зона ответственности роли).
          Регресс на ДВУХ составах серии (09.06 + 02.06; РИСК4 — одна фикстура
          генерализацию не доказывает) даёт раскладку:
          поставки→Мария Михина, коммерция→Сона Енгибарян, сервис→Михаил Саргин,
          резервы→Дарья Набережная, финансы→Ольга Новикова.
  - A5  — приглашённый без кластера голоса (отпускник «Еремеев») НЕ попадает ни в
          авторы (доменный маппинг не подставляет отсутствующего в составе), ни в
          участники (`resolve_present_participants`: «нет голоса — нет имени»).
  - B3  — ростер подаётся в маппинг доменной проверкой: реплика по домену X →
          человек с зоной X (детерминированный `map_from_roster_domain` + проводка
          через `map_all`, реально зовущийся из finalize).

Регресс Ф2 (не сломать): якорь серии в `map_all` применяется и при наличии
ростера; `roster=None` → поведение до Ф3 без изменений.

Дисциплина «опасной тройки»: фикстуры синтетические (доменная лексика, не реальные
ПДн-транскрипты прошлых встреч) — это и приватно, и детерминированно.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_roster_domain_mapping -v
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

# name_mapping → align тянет тяжёлые пакеты, которых нет в CI/маке. Тестируемая
# логика их не использует — подкладываем минимальные стабы (как в phase4b).
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib.align import AlignedTurn  # noqa: E402
from lib import name_mapping as nm  # noqa: E402
from lib import series_roster as sr  # noqa: E402
from lib import protocol_to_tg as ptg  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402

SLUG = sr.ANZHEE_COORDINATION_SLUG

# Каноничные имена ответственных (критерий A4).
MARIA = "Мария Михина"        # поставки
SONA = "Сона Енгибарян"       # коммерция
SARGIN = "Михаил Саргин"      # сервис
DARIA = "Дарья Набережная"    # резервы
OLGA = "Ольга Новикова"       # финансы
ILYA = "Илья Рыбалка"         # владелец — НЕ в ростере


def _turn(speaker, text, *, start=0.0):
    return AlignedTurn(start=start, end=start + 1.0, speaker=speaker, text=text)


# Доменно-насыщенные синтетические реплики (≥2 РАЗНЫХ ключевых слова домена,
# без чужой доменной лексики и без vocative-обращений).
SUPPLY_TXT = (
    "Поставка 131 на досмотре, готовим таможенную декларацию. "
    "Контейнер почти весь на складе агента, скоро отгрузка."
)
COMMERCE_TXT = (
    "Продажи за неделю, выручка идёт по плану. Подготовили оффер, "
    "ведём тендеры и работаем по сделкам."
)
SERVICE_TXT = (
    "Сервис и обслуживание: ремонт по гарантии закрыли, "
    "провёл обучение ателье, монтаж оборудования на объекте."
)
RESERVE_TXT = (
    "Контрольный срез по резервам: остатки и оборачиваемость, "
    "по неликвиду дам комментарий отдельно."
)
FINANCE_TXT = (
    "Финансы: план платежей на неделю, оплата юаней, кредит и ковенанта, "
    "бюджет на ЗП собираем."
)
GENERIC_TXT = "Коллеги, начинаем. Послушаем всех по очереди и подведём итоги."


# ==========================================================================
# series_roster — структура ростера и точка расширения
# ==========================================================================
class TestRosterStructure(unittest.TestCase):
    def test_anzhee_roster_layout(self):
        roster = sr.get_roster(SLUG)
        by_domain = {e["domain"]: e["name"] for e in roster}
        self.assertEqual(by_domain["поставки"], MARIA)
        self.assertEqual(by_domain["коммерция"], SONA)
        self.assertEqual(by_domain["сервис"], SARGIN)
        self.assertEqual(by_domain["резервы"], DARIA)
        self.assertEqual(by_domain["финансы"], OLGA)

    def test_unknown_series_returns_empty(self):
        # ЗАВ1: незнакомая серия (напр. МПервый до Ф5) → [] (доменного маппинга нет).
        self.assertEqual(sr.get_roster("mpervyi-pn-koord-finplan"), [])
        self.assertEqual(sr.get_roster(None), [])
        self.assertEqual(sr.get_roster(""), [])

    def test_domain_for_name_by_full_and_first(self):
        roster = sr.get_roster(SLUG)
        self.assertEqual(sr.domain_for_name(roster, MARIA), "поставки")
        self.assertEqual(sr.domain_for_name(roster, "Мария"), "поставки")  # first-word
        self.assertIsNone(sr.domain_for_name(roster, "Кто-то Чужой"))

    def test_format_roster_hint_lists_zones(self):
        hint = sr.format_roster_hint(sr.get_roster(SLUG))
        for name in (MARIA, SONA, SARGIN, DARIA, OLGA):
            self.assertIn(name, hint)
        self.assertEqual(sr.format_roster_hint([]), "")


# ==========================================================================
# B3 — доменная проверка: реплика по домену X → человек с зоной X
# ==========================================================================
class TestDomainMapping(unittest.TestCase):
    ROSTER = sr.get_roster(SLUG)
    POOL = [MARIA, SONA, SARGIN, DARIA, OLGA, ILYA]

    def test_each_domain_maps_to_its_owner(self):
        turns = [
            _turn("SPEAKER_00", SUPPLY_TXT),
            _turn("SPEAKER_01", COMMERCE_TXT),
            _turn("SPEAKER_02", SERVICE_TXT),
            _turn("SPEAKER_03", RESERVE_TXT),
            _turn("SPEAKER_04", FINANCE_TXT),
        ]
        m = nm.map_from_roster_domain(turns, self.ROSTER, {}, participants=self.POOL)
        self.assertEqual(m["SPEAKER_00"], MARIA)
        self.assertEqual(m["SPEAKER_01"], SONA)
        self.assertEqual(m["SPEAKER_02"], SARGIN)
        self.assertEqual(m["SPEAKER_03"], DARIA)
        self.assertEqual(m["SPEAKER_04"], OLGA)

    def test_single_domain_reply_maps_one(self):
        # B3 в минимальной форме: одна реплика по домену «финансы» → Ольга.
        turns = [_turn("SPEAKER_09", FINANCE_TXT)]
        m = nm.map_from_roster_domain(turns, self.ROSTER, {}, participants=self.POOL)
        self.assertEqual(m, {"SPEAKER_09": OLGA})

    def test_ambiguous_domain_abstains(self):
        # 2 поставки + 2 коммерции ключевых слова → ничья → не маппим (консервативно).
        mixed = "Поставка и контейнер. Продажи и выручка."
        turns = [_turn("SPEAKER_00", mixed)]
        m = nm.map_from_roster_domain(turns, self.ROSTER, {}, participants=self.POOL)
        self.assertEqual(m, {})

    def test_weak_signal_abstains(self):
        # Один ключ слова домена (< порога 2 различных) → не маппим.
        turns = [_turn("SPEAKER_00", "Поставка пришла.")]
        m = nm.map_from_roster_domain(turns, self.ROSTER, {}, participants=self.POOL)
        self.assertEqual(m, {})

    def test_generic_speaker_not_assigned(self):
        # Владелец говорит общими словами (нет домена) → роль не подставляется.
        turns = [_turn("SPEAKER_05", GENERIC_TXT)]
        m = nm.map_from_roster_domain(turns, self.ROSTER, {}, participants=self.POOL)
        self.assertEqual(m, {})

    def test_absent_owner_not_substituted(self):
        # A5 на уровне домена: Дарьи нет в составе → реплики по резервам её НЕ
        # подставляют (нет в participants → не кандидат).
        pool_no_daria = [MARIA, SONA, SARGIN, OLGA, ILYA]
        turns = [_turn("SPEAKER_03", RESERVE_TXT)]
        m = nm.map_from_roster_domain(turns, self.ROSTER, {}, participants=pool_no_daria)
        self.assertEqual(m, {})

    def test_vocative_contradiction_blocks_assignment(self):
        # Кластер строго окликнул «Мария, …» → он НЕ Мария, даже если домен
        # поставок (защита от инверсии тем же strict-vocative, что у якоря Ф4б).
        turns = [_turn("SPEAKER_00", "Мария, " + SUPPLY_TXT)]
        m = nm.map_from_roster_domain(turns, self.ROSTER, {}, participants=self.POOL)
        self.assertNotIn("SPEAKER_00", m)

    def test_already_mapped_excluded(self):
        # Имя, занятое якорем/S1, доменный маппинг повторно не назначает.
        turns = [_turn("SPEAKER_00", FINANCE_TXT)]
        m = nm.map_from_roster_domain(
            turns, self.ROSTER, {"SPEAKER_07": OLGA}, participants=self.POOL
        )
        self.assertEqual(m, {})  # Ольга занята → SPEAKER_00 не маппится

    def test_empty_roster_noop(self):
        turns = [_turn("SPEAKER_00", FINANCE_TXT)]
        self.assertEqual(nm.map_from_roster_domain(turns, [], {}), {})


# ==========================================================================
# A4 — регресс раскладки на ДВУХ составах серии (РИСК4)
# ==========================================================================
class TestRegressLayout(unittest.TestCase):
    ROSTER = sr.get_roster(SLUG)

    def _full_cast_turns(self):
        return [
            _turn("SPEAKER_00", SUPPLY_TXT),
            _turn("SPEAKER_01", COMMERCE_TXT),
            _turn("SPEAKER_02", SERVICE_TXT),
            _turn("SPEAKER_03", RESERVE_TXT),
            _turn("SPEAKER_04", FINANCE_TXT),
        ]

    def test_regress_0609_via_map_all(self):
        # Состав 09.06: 5 зон + владелец (общая реплика). Проводка через map_all
        # (реальный путь финализации).
        turns = self._full_cast_turns() + [_turn("SPEAKER_05", GENERIC_TXT)]
        pool = [MARIA, SONA, SARGIN, DARIA, OLGA, ILYA]
        res = nm.map_all(turns, pool, roster=self.ROSTER)
        c2n = res.cluster_to_name
        self.assertEqual(c2n["SPEAKER_00"], MARIA)
        self.assertEqual(c2n["SPEAKER_01"], SONA)
        self.assertEqual(c2n["SPEAKER_02"], SARGIN)
        self.assertEqual(c2n["SPEAKER_03"], DARIA)
        self.assertEqual(c2n["SPEAKER_04"], OLGA)
        self.assertIn("roster_domain", res.sources_used)
        # Владелец общими словами — роль зоны ему не подставлена.
        self.assertNotIn(c2n.get("SPEAKER_05"), {MARIA, SONA, SARGIN, DARIA, OLGA})

    def test_regress_0602_layout_and_no_phantom(self):
        # Состав 02.06: 5 зон присутствуют; «Михаил Еремеев» — в ОЖИДАЕМЫХ
        # (watched.yaml), но в отпуске → кластера голоса нет. Раскладка верна И
        # фантом не становится автором (A4 + A5).
        EREMEEV = "Михаил Еремеев"
        turns = self._full_cast_turns()  # 5 кластеров, Еремеева среди них НЕТ
        pool = [MARIA, SONA, SARGIN, DARIA, OLGA, EREMEEV]  # Еремеев в составе-пуле
        res = nm.map_all(turns, pool, roster=self.ROSTER)
        c2n = res.cluster_to_name
        self.assertEqual(c2n["SPEAKER_00"], MARIA)
        self.assertEqual(c2n["SPEAKER_01"], SONA)
        self.assertEqual(c2n["SPEAKER_02"], SARGIN)
        self.assertEqual(c2n["SPEAKER_03"], DARIA)
        self.assertEqual(c2n["SPEAKER_04"], OLGA)
        # Еремеев нигде не автор (нет кластера → не подставлен).
        self.assertNotIn(EREMEEV, c2n.values())


# ==========================================================================
# Регресс Ф2 — якорь серии не сломан ростером; roster=None ≡ поведение до Ф3
# ==========================================================================
class TestAnchorStillWorks(unittest.TestCase):
    ROSTER = sr.get_roster(SLUG)

    def test_anchor_applied_alongside_roster(self):
        # Якорь закрепляет SPEAKER_04→Ольга (без vocative-противоречия), домен
        # докидывает остальных. Якорь не теряется при наличии ростера.
        turns = [
            _turn("SPEAKER_00", SUPPLY_TXT),
            _turn("SPEAKER_04", FINANCE_TXT),
        ]
        pool = [MARIA, SONA, SARGIN, DARIA, OLGA]
        res = nm.map_all(
            turns, pool, anchor={"SPEAKER_04": OLGA}, roster=self.ROSTER
        )
        self.assertEqual(res.cluster_to_name["SPEAKER_04"], OLGA)
        self.assertEqual(res.cluster_to_name["SPEAKER_00"], MARIA)
        self.assertIn("series_anchor", res.sources_used)

    def test_roster_none_unchanged(self):
        # roster=None → доменного источника нет (как до Ф3).
        turns = [_turn("SPEAKER_00", SUPPLY_TXT)]
        res = nm.map_all(turns, [MARIA, SONA], roster=None)
        self.assertNotIn("roster_domain", res.sources_used)
        self.assertNotIn("SPEAKER_00", res.cluster_to_name)


# ==========================================================================
# A5 — «нет голоса — нет имени» (состав протокола)
# ==========================================================================
class TestPresentParticipants(unittest.TestCase):
    def test_expected_only_dropped(self):
        # Еремеев только в expected (отпускник), в панели/голосах его нет → выпал.
        present = ptg.resolve_present_participants(
            expected=[MARIA, "Михаил Еремеев", OLGA],
            panel=[MARIA, OLGA],
            voiced=[MARIA, OLGA],
        )
        self.assertNotIn("Михаил Еремеев", present)
        self.assertIn(MARIA, present)
        self.assertIn(OLGA, present)

    def test_silent_panel_attendee_kept(self):
        # Присутствовал (панель), но не говорил → остаётся участником.
        present = ptg.resolve_present_participants(
            expected=[],
            panel=[MARIA, "Инна Белобородова"],
            voiced=[MARIA],
        )
        self.assertIn("Инна Белобородова", present)

    def test_voiced_only_kept(self):
        # Говорил (есть кластер голоса), хоть и не в панели → участник.
        present = ptg.resolve_present_participants(
            expected=[],
            panel=[],
            voiced=[SARGIN],
        )
        self.assertEqual(present, [SARGIN])

    def test_fuller_spelling_preferred(self):
        # Панель «Мария», голос «Мария Михина» → один человек, полное написание.
        present = ptg.resolve_present_participants(
            expected=[],
            panel=["Мария"],
            voiced=[MARIA],
        )
        self.assertEqual(present, [MARIA])

    def test_degraded_fallback_to_expected(self):
        # Нет ни панели, ни голосов (нет сигнала присутствия) → прежнее поведение.
        present = ptg.resolve_present_participants(
            expected=[MARIA, OLGA], panel=[], voiced=[],
        )
        self.assertIn(MARIA, present)
        self.assertIn(OLGA, present)

    def test_ui_noise_filtered(self):
        # Ф1-фильтр UI-мусора панели работает и здесь (единый chokepoint).
        present = ptg.resolve_present_participants(
            expected=[], panel=[MARIA, "Скопировать ссылку"], voiced=[MARIA],
        )
        self.assertNotIn("Скопировать ссылку", present)


# ==========================================================================
# A5 — парс голосов из транскрипта
# ==========================================================================
class TestVoicedFromTranscript(unittest.TestCase):
    TRANSCRIPT = (
        "#транскрипт 2026-06-09\n\n"
        "**Участники:** Мария Михина, Ольга Новикова\n\n"
        "---\n\n"
        "**[00:00] Мария Михина:** Поставка 131 на досмотре.\n\n"
        "**[00:12] Спикер 3:** Тут без имени.\n\n"
        "**[01:05:30] Ольга Новикова:** План платежей на неделю.\n\n"
        "**[02:00] Спикер ?:** Артефакт без кластера.\n"
    )

    def test_extracts_named_speakers_only(self):
        voiced = ptg.voiced_speaker_names_from_transcript(self.TRANSCRIPT)
        self.assertIn(MARIA, voiced)
        self.assertIn(OLGA, voiced)
        # «Спикер N»/«Спикер ?» — не имена (нераспознанный кластер).
        self.assertNotIn("Спикер 3", voiced)
        self.assertFalse(any(v.startswith("Спикер") for v in voiced))

    def test_empty_transcript(self):
        self.assertEqual(ptg.voiced_speaker_names_from_transcript(""), [])


# ==========================================================================
# A5 — интеграция: шапка протокола не содержит отпускника
# ==========================================================================
class TestProtocolPromptParticipants(unittest.TestCase):
    def test_absent_invitee_not_in_header_participants(self):
        # Транскрипт: говорили Мария и Ольга. Еремеев в expected (watched.yaml),
        # но без голоса/панели → в строке participants промпта его нет.
        transcript_md = (
            "#транскрипт 2026-06-09\n\n"
            "---\n\n"
            "**[00:00] Мария Михина:** Поставка на досмотре, контейнер на складе агента.\n\n"
            "**[00:30] Ольга Новикова:** План платежей и оплата юаней.\n"
        )
        meta = {
            "series": SLUG,
            "date": "2026-06-09",
            "expectedParticipants": [MARIA, OLGA, "Михаил Еремеев"],
            "participants": [MARIA, OLGA],  # панель Телемоста
        }
        prompt = lp._format_protocol_user_prompt(transcript_md, meta)
        # Находим строку «- participants: …» из meta-блока.
        line = next(l for l in prompt.splitlines() if l.startswith("- participants:"))
        self.assertIn(MARIA, line)
        self.assertIn(OLGA, line)
        self.assertNotIn("Еремеев", line)


if __name__ == "__main__":
    unittest.main()
