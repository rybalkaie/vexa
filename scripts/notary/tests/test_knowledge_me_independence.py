"""Ф6 (C4 / РИСК3): «бот умён без папки владельца me/».

План `2026-06-09-notary-memory-knowledge-rework`, Фаза 6, REQ C4: «должен работать
без наличия моей папки me». Знание (глоссарий + оргструктура/ростер) читается из
`*-context`, а не из `me/`. Этот тест ставит `MEETING_NOTARY_ME_DIR` на
НЕсуществующий путь (me/ как будто нет) и `MEETING_NOTARY_CONTEXT_DIR` на фикстуры
`*-context`, затем проверяет, что:
  - термины глоссария резолвятся (`load_glossary`) — бот распознаёт термины;
  - ростер серии резолвится (`series_roster.get_roster`) — бот знает роли;
  - компания серии резолвится (`company_for_series`) по оргструктуре;
  - ASR-проекция знания (`sources.glossary_vocab_entries`) собирается;
  - чтение источников me/ при отсутствии каталога — graceful (пустo, НЕ падаем).

Сырьё (транскрипты/протоколы) остаётся в `me/встречи` — это норма (РИСК1 снят),
а не зависимость функции знания: тест НЕ трогает путь сырья.

Чтение реального YAML-фикстура требует pyyaml → `skipTest`, если недоступен
(в прод-venv тест исполняется полностью; данные синтетические — опасная тройка цела).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_knowledge_me_independence -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.lib import context_knowledge as ck  # noqa: E402
from notary.lib import series_roster  # noqa: E402
from notary.auto_vocab import sources  # noqa: E402

_FIXTURE_CONTEXT = _HERE / "fixtures" / "context"
ANZHEE_SLUG = series_roster.ANZHEE_COORDINATION_SLUG
# Гарантированно несуществующий путь — «папки me/ нет».
_NO_ME = "/nonexistent-me-dir-for-c4-test-zzz"


def _real_yaml():
    """Реальный pyyaml (не заглушка) или None — проверка в рантайме (другой тест
    мог подменить sys.modules['yaml'])."""
    try:
        import yaml
    except ImportError:
        return None
    return yaml if callable(getattr(yaml, "safe_load", None)) else None


class TestKnowledgeWithoutMe(unittest.TestCase):
    """C4: знание читается из `*-context` при отсутствующей me/."""

    def setUp(self):
        if _real_yaml() is None:
            self.skipTest("pyyaml недоступен/заглушён — фикстурный C4-тест пропущен")
        self._saved_ctx = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        self._saved_me = os.environ.get("MEETING_NOTARY_ME_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_FIXTURE_CONTEXT)
        os.environ["MEETING_NOTARY_ME_DIR"] = _NO_ME  # me/ как будто нет

    def tearDown(self):
        for key, saved in (("MEETING_NOTARY_CONTEXT_DIR", self._saved_ctx),
                           ("MEETING_NOTARY_ME_DIR", self._saved_me)):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved

    def test_me_dir_really_absent(self):
        self.assertFalse(Path(_NO_ME).exists(), "санити: путь me/ не должен существовать")

    def test_glossary_terms_resolve_from_context(self):
        terms = ck.load_glossary("anzhee")
        self.assertTrue(terms, "глоссарий Anzhee должен читаться из *-context без me/")
        canon = {str(t.get("canonical")) for t in terms}
        self.assertIn("Anzhee", canon)

    def test_roster_resolves_from_context(self):
        roster = series_roster.get_roster(ANZHEE_SLUG)
        self.assertTrue(roster, "ростер серии должен резолвиться из *-context без me/")
        names = {e.get("name") for e in roster}
        self.assertIn("Мария Михина", names)

    def test_company_resolves_from_org_structure(self):
        self.assertEqual(ck.company_for_series(ANZHEE_SLUG), "anzhee")

    def test_asr_projection_builds_from_context(self):
        entries = sources.glossary_vocab_entries("anzhee")
        # Хотя бы один термин с sounds_like (проекция A) собирается из знания.
        self.assertTrue(entries, "ASR-проекция знания должна собираться из *-context без me/")

    def test_mpfirst_company_scoped(self):
        # C2-инвариант держится и без me/: Bolong (mpfirst) не виден в anzhee.
        anzhee_canon = {str(t.get("canonical")) for t in ck.load_glossary("anzhee")}
        self.assertNotIn("Bolong", anzhee_canon)


class TestMeSourcesGracefulWhenAbsent(unittest.TestCase):
    """РИСК3: чтение источников me/ при отсутствии каталога — graceful, не падаем."""

    def test_collect_candidates_empty_on_missing_me(self):
        out = sources.collect_candidates(root=Path(_NO_ME))
        # Все источники → пустые списки (файлов нет), без исключения.
        self.assertTrue(all(v == [] for v in out.values()))

    def test_sync_no_crash_on_missing_me(self):
        # sync читает vocab (не me/) для дедупа; источники me/ пусты → 0 добавлений,
        # без падения. dry_run, чтобы ничего не писать.
        summary = sources.sync(dry_run=True, root=Path(_NO_ME))
        self.assertIn("added", summary)
        self.assertEqual(summary.get("added"), [])


if __name__ == "__main__":
    unittest.main()
