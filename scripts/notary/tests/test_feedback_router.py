"""Ф7 D6 — маршрутизатор фидбэка по слоям.

Критерий D6: правка роли → знание компании/приватно (оргструктура); термин серии →
карточка серии; имя/формат → конфиг/шаблон — КАЖДАЯ в своём слое.

Покрывает: классификацию типа, извлечение роли, диспетчеризацию по слоям,
формат→шаблон (F2) / имя→конфиг, D5-отброс кредов, инвариант «delivered/сырьё не
трогаем» (роутер пишет только outbox/шаблон/конфиг).

Изоляция: outbox/private/ratchet/template/config — в темп через env.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_feedback_router -v
"""
from __future__ import annotations

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

from notary.lib import feedback_router as fr  # noqa: E402
from notary.lib import knowledge_writeback as wb  # noqa: E402
from notary.lib import protocol_template as pt  # noqa: E402


class TestClassifyEdit(unittest.TestCase):
    """Чистая классификация типа правки."""

    def test_role(self):
        for txt in (
            "за поставки отвечает Мария Михина",
            "Саргин ведёт сервис",
            "коммерцию курирует Сона",
            "поставки ведёт Мария",
        ):
            with self.subTest(txt=txt):
                self.assertEqual(fr.classify_edit(txt), fr.KIND_ROLE)

    def test_series_term(self):
        self.assertEqual(fr.classify_edit("не РСЯ, а РЕЦ"), fr.KIND_SERIES_TERM)
        self.assertEqual(fr.classify_edit("Гарсия → Гарсиа"), fr.KIND_SERIES_TERM)

    def test_name_format(self):
        for txt in ("заголовок должен быть Координация", "убери раздел рисков",
                    "суммы выводи таблицей", "резюме сделай короче"):
            with self.subTest(txt=txt):
                self.assertEqual(fr.classify_edit(txt), fr.KIND_NAME_FORMAT)

    def test_content_one_off(self):
        self.assertEqual(fr.classify_edit("131 не под досмотром, а на доставке"), fr.KIND_CONTENT)
        self.assertEqual(fr.classify_edit("забыли добавить итог по складу"), fr.KIND_CONTENT)

    def test_format_vs_name_subtype(self):
        self.assertTrue(fr.is_format_directive("убери раздел рисков"))
        self.assertFalse(fr.is_format_directive("заголовок — Координация"))


class TestExtractRole(unittest.TestCase):
    def test_extract_variants(self):
        self.assertEqual(fr.extract_role("за поставки отвечает Мария Михина"),
                         {"name": "Мария Михина", "domain": "поставки"})
        self.assertEqual(fr.extract_role("Саргин ведёт сервис"),
                         {"name": "Саргин", "domain": "сервис"})
        self.assertEqual(fr.extract_role("поставки ведёт Мария"),
                         {"name": "Мария", "domain": "поставки"})

    def test_no_role(self):
        self.assertIsNone(fr.extract_role("не РСЯ, а РЕЦ"))
        self.assertIsNone(fr.extract_role("это отвечает требованиям"))

    def test_credential_role_rejected(self):
        self.assertIsNone(fr.extract_role("за прод отвечает ghp_" + "A" * 36))


class _RoutedMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.template_root = base / "tmpl"
        self._env = {
            "NOTARY_KNOWLEDGE_OUTBOX_DIR": str(base / "outbox"),
            "NOTARY_PRIVATE_KNOWLEDGE_QUEUE": str(base / "private.md"),
            "NOTARY_KNOWLEDGE_RATCHET_PATH": str(base / "ratchet.json"),
            "NOTARY_CONFIG_PROPOSALS_PATH": str(base / "config.jsonl"),
            "MEETING_NOTARY_CONTEXT_DIR": str(base / "no-context"),
        }
        self._old = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self.base = base

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class TestRouting(_RoutedMixin):
    """Каждый тип правки попадает в СВОЙ слой (D6)."""

    def test_role_routes_to_knowledge_layer(self):
        state = {"series": "s1", "date": "2026-06-10", "feedback_id": "fid1",
                 "participants": ["Илья", "Мария Михина", "Сона", "Саргин"]}
        out = fr.route_edits(state, [{"text": "за поставки отвечает Мария Михина"}])
        self.assertEqual(out["routed"][fr.KIND_ROLE], 1)
        role_detail = [d for d in out["details"] if d.get("kind") == fr.KIND_ROLE][0]
        # Без публикации/ратчета — приватно (D3 дефолт), но это ЗНАНИЕ-слой, не контент.
        self.assertIn(role_detail["layer"], ("private", "company"))
        self.assertTrue(Path(self._env["NOTARY_PRIVATE_KNOWLEDGE_QUEUE"]).is_file())

    def test_format_routes_to_template_version(self):
        self.assertEqual(pt.active_version(root=self.template_root), 1)
        state = {"series": "s1"}
        out = fr.route_edits(state, [{"text": "убери раздел рисков, суммы таблицей"}],
                             template_root=self.template_root)
        self.assertEqual(out["routed"][fr.KIND_NAME_FORMAT], 1)
        # Формат → новая версия шаблона (F2).
        self.assertEqual(pt.active_version(root=self.template_root), 2)

    def test_name_routes_to_config_queue(self):
        out = fr.route_edits({"series": "s1"}, [{"text": "заголовок встречи — Координация"}])
        detail = out["details"][0]
        self.assertEqual(detail["layer"], "config")
        self.assertTrue(Path(self._env["NOTARY_CONFIG_PROPOSALS_PATH"]).is_file())

    def test_series_term_not_duplicated(self):
        # Терм/смысл — карточка серии (feedback_learning), роутер их НЕ дублирует.
        out = fr.route_edits({"series": "s1"}, [{"text": "не РСЯ, а РЕЦ"}])
        self.assertEqual(out["routed"][fr.KIND_SERIES_TERM], 1)
        self.assertEqual(out["details"][0]["layer"], "series-card")
        # В company-outbox терм НЕ кладётся роутером (его слой — карточка серии).
        self.assertEqual(wb._read_outbox("anzhee"), [])

    def test_credential_edit_dropped(self):
        out = fr.route_edits({"series": "s1"}, [{"text": "пароль: qwerty123456 для входа"}])
        self.assertEqual(out["dropped_creds"], 1)
        self.assertEqual(sum(out["routed"].values()), 0)

    def test_mixed_batch_each_to_its_layer(self):
        state = {"series": "s1", "participants": ["Илья", "Мария", "Сона", "Саргин"]}
        edits = [
            {"text": "за поставки отвечает Мария Михина"},   # role
            {"text": "не РСЯ, а РЕЦ"},                         # series-term
            {"text": "убери раздел рисков"},                  # name-format→template
            {"text": "131 уже на доставке"},                  # content
        ]
        out = fr.route_edits(state, edits, template_root=self.template_root)
        self.assertEqual(out["routed"][fr.KIND_ROLE], 1)
        self.assertEqual(out["routed"][fr.KIND_SERIES_TERM], 1)
        self.assertEqual(out["routed"][fr.KIND_NAME_FORMAT], 1)
        self.assertEqual(out["routed"][fr.KIND_CONTENT], 1)

    def test_empty_edits_noop(self):
        out = fr.route_edits({"series": "s1"}, [])
        self.assertEqual(sum(out["routed"].values()), 0)
        self.assertEqual(out["dropped_creds"], 0)


if __name__ == "__main__":
    unittest.main()
