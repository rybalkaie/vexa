"""Ф6 (E1–E5): гейтинг публикации знания — тесты `lib/publication_gate`.

План `2026-06-09-notary-memory-knowledge-rework`, Фаза 6. Покрывает критерии:
  - E1: групповая координация размеченной компании → знание ПУБЛИКУЕМО;
  - E2/E3: 1-на-1 / private / НЕразмечено → знание private (fail-closed);
  - E4: владелец + РОВНО один → private независимо от разметки;
  - E5: предохранитель круга («осторожно добавлять») — внешний участник → private;
  - проводка вердикта в память серии (PII-free, достижимо из финализации).

Чистая логика гейта (`decide_publication`) тестируется БЕЗ YAML/IO — набором
серий. Интеграция (`decide_for_meeting`) — на статическом ростере Anzhee
(fallback `series_roster`, без pyyaml). Опасная тройка: имена — синтетические
метаданные состава, сырья реплик нет.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_publication_gate -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.lib import publication_gate as pg  # noqa: E402
from notary.lib import series_memory  # noqa: E402
from notary.lib import series_roster  # noqa: E402

ANZHEE_SLUG = series_roster.ANZHEE_COORDINATION_SLUG

OWNER = "Илья Рыбалка"
# Реальные имена Anzhee-ростера (для круга E5) — есть в _STATIC_ROSTERS fallback.
MARIA = "Мария Михина"
SONA = "Сона Енгибарян"
SARGIN = "Михаил Саргин"
DARYA = "Дарья Набережная"
OLGA = "Ольга Новикова"
ANZHEE_ROSTER = [MARIA, SONA, SARGIN, DARYA, OLGA]


class TestDecidePublicationGroup(unittest.TestCase):
    """E1 — групповая координация компании со знакомым составом → ПУБЛИКУЕМО."""

    def test_group_company_in_roster_allowed(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [OWNER, MARIA, SONA, SARGIN],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.visibility, "company")
        self.assertEqual(d.company, "anzhee")
        self.assertEqual(d.reason, "ok-group")

    def test_group_without_owner_routes_by_markup(self):
        # Edge плана: групповая БЕЗ владельца → роутинг по company-разметке, не 1-на-1.
        d = pg.decide_publication(
            ANZHEE_SLUG, [MARIA, SONA],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.reason, "ok-group")

    def test_readers_count_as_circle(self):
        # Михаил/Татьяна (читатели) + ростер → все в круге.
        d = pg.decide_publication(
            ANZHEE_SLUG, [OWNER, "Михаил", "Татьяна", MARIA],
            visibility="company", company="anzhee", roster_names=[MARIA],
        )
        self.assertTrue(d.allowed)

    def test_case_insensitive_markup(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [OWNER, MARIA, SONA],
            visibility="Company", company="ANZHEE", roster_names=ANZHEE_ROSTER,
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.company, "anzhee")

    def test_owner_detected_by_token(self):
        # «Рыбалка И.Е.» — владелец по подстроке → группа из 2 не-владельцев.
        d = pg.decide_publication(
            ANZHEE_SLUG, ["Рыбалка И.Е.", MARIA, SONA],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertTrue(d.allowed)


class TestDecidePublicationPrivate(unittest.TestCase):
    """E2/E3/E4 — всё, кроме явной групповой company, → private (fail-closed)."""

    def test_one_on_one_owner_plus_one_forced_private(self):
        # E4: даже при visibility=company (ошибочной разметке) → private.
        d = pg.decide_publication(
            "marketplaces-tatiana", [OWNER, "Татьяна Филиппова"],
            visibility="company", company="mpfirst", roster_names=[],
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.visibility, "private")
        self.assertEqual(d.reason, "one-on-one")

    def test_private_markup(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [OWNER, MARIA, SONA],
            visibility="private", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "private-markup")

    def test_unmarked_defaults_private(self):
        # E3: visibility=None (неразмечено) → private.
        d = pg.decide_publication(
            "new-unmarked-series", [OWNER, MARIA, SONA],
            visibility=None, company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "unmarked")

    def test_owner_only_no_group(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [OWNER],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "no-group")

    def test_empty_present_no_group(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "no-group")

    def test_single_non_owner_no_group(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [MARIA],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "no-group")

    def test_unknown_company_private(self):
        d = pg.decide_publication(
            "x", [OWNER, MARIA, SONA],
            visibility="company", company="acme", roster_names=ANZHEE_ROSTER,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "no-company")


class TestDecidePublicationE5Circle(unittest.TestCase):
    """E5 — предохранитель «круг читателей ≥ участники» (осторожно добавлять)."""

    def test_outsider_blocks_publication(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [OWNER, MARIA, SONA, "Иван Контрагент"],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "outside-circle")

    def test_empty_roster_blocks_group(self):
        # МПервый-стаб: ростер пуст → круг = {владелец, читатели}; реальные
        # не-владельцы вне круга → private (fail-closed, пока нет данных ростера).
        d = pg.decide_publication(
            "mpervyi-pn-koord-finplan", [OWNER, "Аноним Один", "Аноним Два"],
            visibility="company", company="mpfirst", roster_names=[],
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "outside-circle")

    def test_all_in_roster_passes_circle(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [OWNER, MARIA, SONA, SARGIN, DARYA, OLGA],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        self.assertTrue(d.allowed)

    def test_custom_readers_env_override(self):
        # Переопределение круга читателей через env — «гость» становится читателем.
        saved = os.environ.get("NOTARY_CONTEXT_READERS")
        os.environ["NOTARY_CONTEXT_READERS"] = "Гость Один"
        try:
            d = pg.decide_publication(
                ANZHEE_SLUG, [OWNER, MARIA, "Гость Один"],
                visibility="company", company="anzhee", roster_names=[MARIA],
            )
            self.assertTrue(d.allowed)
        finally:
            if saved is None:
                os.environ.pop("NOTARY_CONTEXT_READERS", None)
            else:
                os.environ["NOTARY_CONTEXT_READERS"] = saved


class TestDecideForMeetingIntegration(unittest.TestCase):
    """Интеграция `decide_for_meeting`: разметка (инъекция watched) + статический
    ростер Anzhee (fallback series_roster, без pyyaml)."""

    def _watched(self, series, **fields):
        rec = {"id": f"{series}-evt", "series": series, "type": "manual"}
        rec.update(fields)
        return {"watched": [rec]}

    def test_anzhee_group_allowed_via_markup(self):
        w = self._watched(ANZHEE_SLUG, company="anzhee", visibility="company")
        d = pg.decide_for_meeting(ANZHEE_SLUG, [OWNER, MARIA, SONA], watched=w)
        self.assertTrue(d.allowed)
        self.assertEqual(d.company, "anzhee")
        self.assertEqual(d.reason, "ok-group")

    def test_anzhee_unmarked_private_via_markup(self):
        w = self._watched(ANZHEE_SLUG)  # нет company/visibility
        d = pg.decide_for_meeting(ANZHEE_SLUG, [OWNER, MARIA, SONA], watched=w)
        self.assertFalse(d.allowed)
        # company может резолвнуться оргструктурным fallback'ом, но visibility=None
        # → unmarked → private.
        self.assertEqual(d.reason, "unmarked")

    def test_one_on_one_private_via_markup(self):
        w = self._watched("marketplaces-tatiana", company="mpfirst", visibility="company")
        d = pg.decide_for_meeting("marketplaces-tatiana", [OWNER, "Татьяна Филиппова"], watched=w)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "one-on-one")

    def test_anzhee_outsider_private_via_markup(self):
        w = self._watched(ANZHEE_SLUG, company="anzhee", visibility="company")
        d = pg.decide_for_meeting(ANZHEE_SLUG, [OWNER, MARIA, "Чужой Гость"], watched=w)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "outside-circle")


class TestVerdictPersistedInDigest(unittest.TestCase):
    """Проводка: вердикт PII-free оседает в памяти серии (`save_meeting_digest`)."""

    _PROTO = (
        "#протоколвстречи\n\n"
        "**Участники:** Илья Рыбалка, Мария Михина, Сона Енгибарян\n\n"
        "## 1) Поставки\n▪️ Контейнер пришёл, 3 штуки.\n"
    )

    def test_build_digest_includes_publication_when_passed(self):
        verdict = {"allowed": True, "visibility": "company", "company": "anzhee", "reason": "ok-group"}
        dig = series_memory.build_digest(
            self._PROTO, {"series": ANZHEE_SLUG, "date": "2026-06-09"},
            date="2026-06-09", publication=verdict,
        )
        self.assertEqual(dig.get("publication"), verdict)

    def test_build_digest_omits_publication_when_none(self):
        dig = series_memory.build_digest(
            self._PROTO, {"series": ANZHEE_SLUG, "date": "2026-06-09"}, date="2026-06-09",
        )
        self.assertNotIn("publication", dig)  # ленивый ключ (бэкфилл его не несёт)

    def test_save_meeting_digest_roundtrip_persists_verdict(self):
        verdict = {"allowed": False, "visibility": "private", "company": None, "reason": "one-on-one"}
        with tempfile.TemporaryDirectory() as tmp:
            series_dir = Path(tmp) / ANZHEE_SLUG
            series_dir.mkdir(parents=True)
            saved = series_memory.save_meeting_digest(
                series_dir, "2026-06-09", self._PROTO,
                {"series": ANZHEE_SLUG, "date": "2026-06-09"},
                publication=verdict,
            )
            self.assertIsNotNone(saved)
            data = json.loads(Path(saved).read_text(encoding="utf-8"))
            self.assertEqual(data.get("publication"), verdict)
            # Опасная тройка: в памяти нет сырых реплик, только производное.
            self.assertNotIn("transcript", data)

    def test_as_metadata_is_pii_free_shape(self):
        d = pg.decide_publication(
            ANZHEE_SLUG, [OWNER, MARIA, SONA],
            visibility="company", company="anzhee", roster_names=ANZHEE_ROSTER,
        )
        meta = d.as_metadata()
        self.assertEqual(set(meta.keys()), {"allowed", "visibility", "company", "reason"})
        # Никаких имён участников в вердикте.
        self.assertNotIn(MARIA, json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
