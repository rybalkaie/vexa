"""Тесты Ф2 плана `2026-06-16-notary-name-authority-targeted-edits` (ISS-11):
COMPANY-level замок исправлений имён/ролей — исправление, принятое раз, держится.

Покрывает REQ (дословные критерии приёмки):
  - R6  — правка → durable company-факт, реприменяется на будущей встрече ДРУГОЙ
          серии той же компании (cross-series).
  - R7  — кумулятивно: факты из разных встреч сосуществуют, не затирают друг друга.
  - R8  — расширенный захват: «перепутал A и B», «не A, а B», «B отвечает за X»,
          «A не было» (вкл. склонения) → remap/role/exclusion.
  - R9  — негативный слой «кого не было»: ТЕКУЩАЯ встреча, вперёд НЕ запоминается;
          механизм ОТДЕЛЁН от R4-сужения пула (НЕС1).
  - R10 — last-write-wins: свежая правка бьёт выученный факт и якорь.
  - R11 — развязка: локально сразу / в `*-context` через PR (не мгновенно в main).
  - R12 — правка имени держится внутри встречи между перевыпусками (полная
          регенерация не откатывает).

Дисциплина «опасной тройки»: фикстуры синтетические (доменная лексика, не ПДн);
overlay пишется в ВРЕМЕННУЮ директорию (root=), боевые `*-context` не трогаются.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_phase2_correction_lock -v
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

# name_mapping → align тянет тяжёлые пакеты, которых нет в CI/маке. Стабим (как
# делают test_phase1_name_authority / test_roster_domain_mapping).
for _mod in ("requests", "httpx", "numpy", "torch"):
    if _mod not in sys.modules:
        try:  # noqa: SIM105
            __import__(_mod)
        except ModuleNotFoundError:
            sys.modules[_mod] = types.ModuleType(_mod)

from lib.align import AlignedTurn  # noqa: E402
from lib import name_mapping as nm  # noqa: E402
from lib import correction_facts as cf  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402

MARIA = "Мария Михина"        # поставки
SARGIN = "Михаил Саргин"      # сервис (активный)
OLGA = "Ольга Новикова"       # финансы
EREMEEV = "Михаил Еремеев"    # тёзка Саргина, стухший
IVANOV = "Иван Иванов"
TATIANA = "Татьяна"
NAMES = [MARIA, SARGIN, OLGA, EREMEEV, TATIANA, IVANOV]

CO = "anzhee"

# Доменная лексика для роста-домен-матча (≥2 разных стема, как в Ф1-тестах).
SERVICE_TXT = (
    "Сервис и обслуживание: ремонт по гарантии закрыли, "
    "монтаж оборудования на объекте, поддержка клиентов."
)
SERVICE_KW = ["сервис", "обслуживан", "ремонт", "гаранти", "монтаж", "поддержк"]
FINANCE_TXT = "Финансы: план платежей, оплата юаней, кредит и ковенанта."
FINANCE_KW = ["финанс", "платёж", "оплат", "юан", "кредит", "ковенант"]


def _t(speaker, text, *, start=0.0):
    return AlignedTurn(start=start, end=start + 1.0, speaker=speaker, text=text)


class _TmpRoot(unittest.TestCase):
    """База: каждый тест получает изолированный overlay-root (temp dir)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="corr-facts-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


# ==========================================================================
# R8 — расширенный захват правок (чистый парсер, без IO — всегда зелёный)
# ==========================================================================
class TestCaptureR8(unittest.TestCase):
    def test_role_direct(self):
        out = cf.parse_correction_facts("Саргин отвечает за сервис", known_names=NAMES)
        self.assertIn({"kind": "role", "name": SARGIN, "domain": "сервис"}, out)

    def test_role_inverse(self):
        out = cf.parse_correction_facts("за сервис отвечает Саргин", known_names=NAMES)
        self.assertIn({"kind": "role", "name": SARGIN, "domain": "сервис"}, out)

    def test_role_vedet(self):
        out = cf.parse_correction_facts("Саргин ведёт сервис", known_names=NAMES)
        self.assertIn({"kind": "role", "name": SARGIN, "domain": "сервис"}, out)

    def test_name_ne_a(self):
        out = cf.parse_correction_facts("это не Еремеев, а Саргин", known_names=NAMES)
        self.assertIn({"kind": "name", "wrong": EREMEEV, "right": SARGIN}, out)

    def test_swap_pereputal(self):
        out = cf.parse_correction_facts("ты перепутал Марию и Татьяну", known_names=NAMES)
        swaps = [f for f in out if f.get("kind") == "swap"]
        self.assertEqual(len(swaps), 1)
        self.assertEqual({swaps[0]["a"], swaps[0]["b"]}, {MARIA, TATIANA})

    def test_declension_resolve(self):
        # Склонения резолвятся к канону без pymorphy3 (суффикс-стеммер).
        self.assertEqual(cf.resolve_known_name("Еремеева", NAMES), EREMEEV)
        self.assertEqual(cf.resolve_known_name("Марию", NAMES), MARIA)
        self.assertEqual(cf.resolve_known_name("Татьяну", NAMES), TATIANA)
        self.assertEqual(cf.resolve_known_name("Ольгой", NAMES), OLGA)

    def test_absent_capture_with_declension(self):
        self.assertEqual(cf.parse_absent_names("Еремеева не было на встрече", known_names=NAMES), [EREMEEV])
        self.assertEqual(cf.parse_absent_names("Ольги не было", known_names=NAMES), [OLGA])
        self.assertEqual(cf.parse_absent_names("совещание прошло без Марии", known_names=NAMES), [MARIA])

    def test_namesake_ambiguous_not_resolved(self):
        # Тёзка-безопасность: «Михаил» неоднозначен (Саргин/Еремеев) → не резолвим.
        self.assertIsNone(cf.resolve_known_name("Михаил", NAMES))

    def test_unknown_formulation_silent(self):
        self.assertEqual(cf.parse_correction_facts("спасибо, отлично сделано", known_names=NAMES), [])


# ==========================================================================
# R7 — кумулятивность + R10 — last-write-wins
# ==========================================================================
class TestCumulativeAndLastWriteR7R10(_TmpRoot):
    def test_r7_facts_from_different_meetings_coexist(self):
        cf.record_role_fact(CO, SARGIN, "сервис", keywords=SERVICE_KW, ts=100.0, root=self.root, share=False)
        cf.record_role_fact(CO, MARIA, "поставки", keywords=["поставк"], ts=200.0, root=self.root, share=False)
        domains = {f["domain"]: f["name"] for f in cf.role_facts(CO, root=self.root)}
        self.assertEqual(domains.get("сервис"), SARGIN)
        self.assertEqual(domains.get("поставки"), MARIA)

    def test_r10_last_write_wins_role(self):
        cf.record_role_fact(CO, SARGIN, "сервис", keywords=SERVICE_KW, ts=100.0, root=self.root, share=False)
        cf.record_role_fact(CO, IVANOV, "сервис", keywords=SERVICE_KW, ts=300.0, root=self.root, share=False)
        domains = {f["domain"]: f["name"] for f in cf.role_facts(CO, root=self.root)}
        self.assertEqual(domains.get("сервис"), IVANOV)  # свежий бьёт старый
        # Кумулятивный лог не теряет историю (append-only), но резолв — один.
        self.assertEqual(len(cf.load_correction_facts(CO, root=self.root)), 2)

    def test_r10_last_write_wins_name_canon(self):
        cf.record_name_fact(CO, EREMEEV, SARGIN, ts=100.0, root=self.root)
        cf.record_name_fact(CO, EREMEEV, IVANOV, ts=300.0, root=self.root)
        self.assertEqual(cf.name_canon_map(CO, root=self.root), {EREMEEV.lower(): IVANOV})


# ==========================================================================
# R6 — durable company-факт реприменяется на ДРУГОЙ серии той же компании
# ==========================================================================
class TestDurableCrossSeriesR6(_TmpRoot):
    def test_role_fact_applies_on_other_series_meeting(self):
        # Правка «сервис→Саргин» принята на серии A. На серии B (другой slug, та же
        # компания) ростер из YAML ещё несёт СТАРОЕ имя на домене сервис.
        cf.record_role_fact(CO, SARGIN, "сервис", keywords=SERVICE_KW, ts=100.0, root=self.root, share=False)
        series_b_roster = [
            {"name": EREMEEV, "domain": "сервис", "keywords": SERVICE_KW},  # устаревший YAML
            {"name": OLGA, "domain": "финансы", "keywords": FINANCE_KW},
        ]
        merged = cf.merge_roster(series_b_roster, CO, root=self.root)
        by_domain = {e["domain"]: e["name"] for e in merged}
        self.assertEqual(by_domain["сервис"], SARGIN)   # company-факт перекрыл
        self.assertEqual(by_domain["финансы"], OLGA)    # прочее не тронуто
        # Лексика домена сохранена (богатая из YAML) → доменный матч работает.
        kws = next(e["keywords"] for e in merged if e["domain"] == "сервис")
        self.assertGreaterEqual(len(kws), 2)

        # И сквозной прогон map_all на сервис-кластере серии B → Саргин.
        turns = [_t("SPEAKER_02", SERVICE_TXT), _t("SPEAKER_04", FINANCE_TXT)]
        pool = [SARGIN, OLGA, MARIA]  # Саргин присутствует (A5)
        res = nm.map_all(turns, pool, roster=merged, present=pool)
        self.assertEqual(res.cluster_to_name["SPEAKER_02"], SARGIN)
        self.assertNotIn(EREMEEV, res.cluster_to_name.values())

    def test_no_facts_returns_base_roster_unchanged(self):
        base = [{"name": SARGIN, "domain": "сервис", "keywords": SERVICE_KW}]
        self.assertEqual(cf.merge_roster(base, CO, root=self.root), base)


# ==========================================================================
# R9 — негативный слой «кого не было» (текущая встреча, без запоминания вперёд)
# ==========================================================================
class TestNegativeRosterR9(_TmpRoot):
    def test_absent_excluded_from_llm_pool(self):
        # НЕС1: absent — отдельный негативный слой, не сужение пула людей компании.
        pool, dropped = lp._build_llm_name_pool(
            [SARGIN, EREMEEV], [OLGA], {},
            absent_names={EREMEEV},
        )
        self.assertNotIn(EREMEEV, pool)
        self.assertIn(SARGIN, pool)
        self.assertEqual(dropped, 1)

    def test_absent_not_substituted_in_map_all(self):
        # «Еремеева не было» → его убирают из present → доменный/тёзка-маппинг не
        # подставит (A5-гейт присутствия). Здесь present без Еремеева.
        turns = [_t("SPEAKER_02", SERVICE_TXT)]
        roster = [{"name": SARGIN, "domain": "сервис", "keywords": SERVICE_KW}]
        present = [SARGIN]  # Еремеева тут нет
        res = nm.map_all(turns, [SARGIN], roster=roster, present=present)
        self.assertNotIn(EREMEEV, res.cluster_to_name.values())

    def test_absent_not_persisted_forward(self):
        # R9/A3: «не было» НЕ пишется в company-overlay (вперёд не запоминаем).
        absent = cf.parse_absent_names("Еремеева не было", known_names=NAMES)
        self.assertEqual(absent, [EREMEEV])
        # record_facts_from_text не трогает absent — overlay остаётся пустым.
        cf.record_facts_from_text(CO, "Еремеева не было", known_names=NAMES, root=self.root, share=False)
        self.assertEqual(cf.load_correction_facts(CO, root=self.root), [])


# ==========================================================================
# R11 — развязка: локально сразу / в *-context через PR (не мгновенно в main)
# ==========================================================================
class TestDecouplingR11(_TmpRoot):
    def test_local_immediate_no_context_clone_touch(self):
        # Локальная запись идёт в overlay-root (НЕ в боевой клон *-context).
        cf.record_role_fact(CO, SARGIN, "сервис", keywords=SERVICE_KW, root=self.root, share=False)
        store = cf.path_for(CO, root=self.root)
        self.assertTrue(store.is_file())
        # В overlay-root нет ничего «*-context» (мы не пишем в синкаемый клон).
        names = [p.name for p in self.root.rglob("*")]
        self.assertFalse(any("context" in n for n in names))
        # И факт сразу виден перевыпуску (re-read overlay).
        self.assertEqual(
            {f["domain"]: f["name"] for f in cf.role_facts(CO, root=self.root)}.get("сервис"),
            SARGIN,
        )

    def test_team_share_goes_through_writeback_pr(self):
        # share=True ставит факт в очередь team-share через knowledge_writeback
        # (company-outbox → ветка + PR). Перехватываем propose_role.
        from lib import knowledge_writeback as kw
        calls = []
        orig = kw.propose_role
        kw.propose_role = lambda *a, **k: calls.append((a, k)) or types.SimpleNamespace(action="enqueue")
        try:
            cf.record_role_fact(CO, SARGIN, "сервис", keywords=SERVICE_KW, root=self.root, share=True)
        finally:
            kw.propose_role = orig
        self.assertEqual(len(calls), 1)
        # Имя+домен ушли в PR-очередь, не мгновенно в main.
        self.assertEqual(calls[0][0][:2], (SARGIN, "сервис"))


# ==========================================================================
# R12 — правка имени держится внутри встречи между перевыпусками
# ==========================================================================
class TestWithinMeetingR12(_TmpRoot):
    def test_name_canon_survives_full_regen_with_unrelated_edit(self):
        # Раунд 1: правка имени «Еремеев→Саргин» (durable name-канон).
        cf.record_name_fact(CO, EREMEEV, SARGIN, ts=100.0, root=self.root)
        # Раунд 2: несвязанная правка (роль по поставкам) — не затирает имя.
        cf.record_role_fact(CO, MARIA, "поставки", keywords=["поставк"], ts=200.0, root=self.root, share=False)
        # Раунд 3: ПОЛНАЯ регенерация → свежий map_all снова дал бы Еремеева
        # (стухший источник). company name-канон переименовывает поверх.
        fresh_mapping = {"SPEAKER_02": EREMEEV, "SPEAKER_03": MARIA}
        locked = cf.apply_name_canon(fresh_mapping, CO, root=self.root)
        self.assertEqual(locked["SPEAKER_02"], SARGIN)   # имя из раунда 1 сохранено
        self.assertEqual(locked["SPEAKER_03"], MARIA)
        # И несвязанный факт раунда 2 по-прежнему активен (кумулятивно, R7).
        self.assertEqual(
            {f["domain"]: f["name"] for f in cf.role_facts(CO, root=self.root)}.get("поставки"),
            MARIA,
        )

    def test_excluded_names_targets_namesakes_only(self):
        # ISS-11 корень: Еремеев↔Саргин — тёзки (оба «Михаил») → глушим неверного.
        cf.record_name_fact(CO, EREMEEV, SARGIN, ts=100.0, root=self.root)
        self.assertIn(EREMEEV.lower(), cf.excluded_names(CO, root=self.root))
        # Не-тёзка правка («Илья Рыбалка»→«Михаил Саргин») НЕ глушит Илью на будущее
        # (его подстрахует rename apply_name_canon, но в пул он не запрещён).
        cf.record_name_fact(CO, "Илья Рыбалка", SARGIN, ts=200.0, root=self.root)
        self.assertNotIn("илья рыбалка", cf.excluded_names(CO, root=self.root))

    def test_name_canon_strict_no_namesake_damage(self):
        # Строгий матч: «Еремеев→Саргин» НЕ переименует bare «Михаил» (тёзка-защита).
        cf.record_name_fact(CO, EREMEEV, SARGIN, root=self.root)
        out = cf.apply_name_canon({"SPEAKER_00": "Михаил", "SPEAKER_01": EREMEEV}, CO, root=self.root)
        self.assertEqual(out["SPEAKER_00"], "Михаил")    # тёзка не тронут
        self.assertEqual(out["SPEAKER_01"], SARGIN)      # точный матч переименован


# ==========================================================================
# Захват → запись (роутинг durable vs current-meeting) — интеграция
# ==========================================================================
class TestRecordFromText(_TmpRoot):
    def test_role_and_name_recorded_swap_and_absent_not(self):
        roster = [{"name": EREMEEV, "domain": "сервис", "keywords": SERVICE_KW}]
        # role + name → durable
        c1 = cf.record_facts_from_text(
            CO, "Саргин отвечает за сервис", known_names=NAMES,
            roster=roster, root=self.root, share=False,
        )
        self.assertEqual(c1["role"], 1)
        c2 = cf.record_facts_from_text(
            CO, "это не Еремеев, а Саргин", known_names=NAMES, root=self.root, share=False,
        )
        self.assertEqual(c2["name"], 1)
        # swap не персистится как durable
        before = len(cf.load_correction_facts(CO, root=self.root))
        cf.record_facts_from_text(CO, "перепутал Марию и Татьяну", known_names=NAMES, root=self.root, share=False)
        self.assertEqual(len(cf.load_correction_facts(CO, root=self.root)), before)
        # role-факт обогатил лексику из ростера (≥2 ключевых слова прошли порог).
        role = {f["domain"]: f for f in cf.role_facts(CO, root=self.root)}["сервис"]
        self.assertGreaterEqual(len(role["keywords"]), 2)

    def test_unknown_company_is_noop(self):
        self.assertEqual(
            cf.record_facts_from_text(None, "Саргин отвечает за сервис", root=self.root),
            {"role": 0, "name": 0},
        )


if __name__ == "__main__":
    unittest.main()
