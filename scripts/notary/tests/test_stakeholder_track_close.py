"""Тесты Ф3 — автосвязка протокол → трек стейкхолдера (закрытие + добавление).

Покрывает:
  - `lib.stakeholder_track.close_open_item` (REQ 4.1/4.3/4.5):
      перенос «🟢 Открыто» → «✅ Закрытые» с датой и пометкой; обратимость
      (перенос, НЕ удаление); markdown валиден; lock/atomic соблюдены;
      exact-match guard (РАЗМ3); идемпотентность; создание секции «Закрытые».
  - `lib.llm_postprocess._parse_track_sync_response` (REQ 4.4):
      деление closed/new; отбрасывание не-дословных closed; дедуп new.
  - `lib.llm_postprocess.sync_stakeholder_track` (REQ 4.1/4.2/4.4/4.5):
      end-to-end с mock-LLM (closed уезжает в Закрытые, new появляется в
      Открыто); при выключенном флаге — no-op (LLM не зовётся).

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_stakeholder_track_close -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import stakeholder_track as st  # noqa: E402
from lib import llm_postprocess as lp  # noqa: E402

_FIXTURE = _HERE / "fixtures" / "stakeholder_track_sample.md"

# Дословный текст одного из открытых пунктов фикстуры (тело без `- [ ]`).
_OPEN_ITEM = (
    "**Профиль роли продуктолога** — подготовить, чтобы был под рукой. "
    "Спросить статус."
)


def _open_section(text: str) -> str:
    """Возвращает блок «## 🟢 Открыто …» (до следующего `## `)."""
    lines = text.split("\n")
    out: list[str] = []
    inside = False
    for ln in lines:
        if ln.startswith("## 🟢 Открыто"):
            inside = True
            out.append(ln)
            continue
        if inside and ln.startswith("## "):
            break
        if inside:
            out.append(ln)
    return "\n".join(out)


def _closed_section(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    inside = False
    for ln in lines:
        if ln.startswith("## ✅ Закрыт"):
            inside = True
            out.append(ln)
            continue
        if inside and ln.startswith("## "):
            break
        if inside:
            out.append(ln)
    return "\n".join(out)


class CloseOpenItemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="track-close-")
        self.track = Path(self.tmp) / "track.md"
        shutil.copy(_FIXTURE, self.track)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_move_open_to_closed_with_date_and_note(self) -> None:
        """REQ 4.1/4.5: пункт уезжает в «✅ Закрытые» с датой + пометкой,
        из «🟢 Открыто» убран, но НЕ удалён (виден в Закрытые)."""
        ok = st.close_open_item(
            self.track, _OPEN_ITEM, closed_date="2026-06-04", skip_whitelist=True
        )
        self.assertTrue(ok)
        text = self.track.read_text(encoding="utf-8")

        # Убран из «Открыто».
        self.assertNotIn(_OPEN_ITEM, _open_section(text))
        # Появился в «Закрытые» с датой и пометкой.
        closed = _closed_section(text)
        self.assertIn(_OPEN_ITEM, closed)
        self.assertIn("Закрыто 2026-06-04", closed)
        self.assertIn("закрыто ботом по встрече 2026-06-04", closed)
        # Обратимость: тело пункта по-прежнему присутствует в файле (не удалено).
        self.assertIn("Профиль роли продуктолога", text)

    def test_markdown_structure_preserved(self) -> None:
        """Обе секции на месте, прочие открытые пункты не тронуты."""
        st.close_open_item(
            self.track, _OPEN_ITEM, closed_date="2026-06-04", skip_whitelist=True
        )
        text = self.track.read_text(encoding="utf-8")
        self.assertEqual(text.count("## 🟢 Открыто"), 1)
        self.assertEqual(text.count("## ✅ Закрыт"), 1)
        # Соседние открытые пункты остались открытыми.
        self.assertIn("RNP — аномалии по Gold", _open_section(text))
        self.assertIn("Singer возвратные на складе", _open_section(text))
        # Файл заканчивается переводом строки (как исходный).
        self.assertTrue(text.endswith("\n"))

    def test_idempotent_second_close_is_noop(self) -> None:
        """Повторное закрытие уже закрытого пункта — no-op, без дубля."""
        ok1 = st.close_open_item(
            self.track, _OPEN_ITEM, closed_date="2026-06-04", skip_whitelist=True
        )
        self.assertTrue(ok1)
        ok2 = st.close_open_item(
            self.track, _OPEN_ITEM, closed_date="2026-06-05", skip_whitelist=True
        )
        self.assertFalse(ok2)
        text = self.track.read_text(encoding="utf-8")
        # Тело встречается в Закрытые ровно один раз.
        self.assertEqual(_closed_section(text).count("Профиль роли продуктолога"), 1)

    def test_exact_match_guard_no_close_of_similar(self) -> None:
        """РАЗМ3: непопадание (нет точного совпадения) → no-op + ничего не тронуто."""
        before = self.track.read_text(encoding="utf-8")
        ok = st.close_open_item(
            self.track,
            "Профиль роли продуктолога",  # похоже, но НЕ дословно
            closed_date="2026-06-04",
            skip_whitelist=True,
        )
        self.assertFalse(ok)
        self.assertEqual(self.track.read_text(encoding="utf-8"), before)

    def test_matches_full_line_with_checkbox(self) -> None:
        """item_match с префиксом `- [ ] ` тоже матчится (полная строка)."""
        ok = st.close_open_item(
            self.track,
            f"- [ ] {_OPEN_ITEM}",
            closed_date="2026-06-04",
            skip_whitelist=True,
        )
        self.assertTrue(ok)
        self.assertIn(_OPEN_ITEM, _closed_section(self.track.read_text(encoding="utf-8")))

    def test_creates_closed_section_if_missing(self) -> None:
        """Если секции «✅ Закрытые» нет — создаётся в конце файла."""
        no_closed = Path(self.tmp) / "no-closed.md"
        no_closed.write_text(
            "# Стейк\n\n## 🟢 Открыто\n\n- [ ] **Вопрос A** — детали.\n",
            encoding="utf-8",
        )
        ok = st.close_open_item(
            no_closed, "**Вопрос A** — детали.", closed_date="2026-06-04",
            skip_whitelist=True,
        )
        self.assertTrue(ok)
        text = no_closed.read_text(encoding="utf-8")
        self.assertIn("## ✅ Закрытые", text)
        self.assertIn("Вопрос A", _closed_section(text))
        self.assertNotIn("Вопрос A", _open_section(text))

    def test_lock_and_atomic_used(self) -> None:
        """REQ 4.3: lock/atomic инфра модуля переиспользована (не сырая запись)."""
        with mock.patch.object(st, "_acquire_lock", wraps=st._acquire_lock) as acq, \
             mock.patch.object(st, "_release_lock", wraps=st._release_lock) as rel, \
             mock.patch.object(st, "_atomic_write", wraps=st._atomic_write) as aw:
            ok = st.close_open_item(
                self.track, _OPEN_ITEM, closed_date="2026-06-04", skip_whitelist=True
            )
        self.assertTrue(ok)
        acq.assert_called_once()
        rel.assert_called_once()
        aw.assert_called_once()

    def test_custom_note(self) -> None:
        ok = st.close_open_item(
            self.track, _OPEN_ITEM, closed_date="2026-06-04",
            note="закрыто вручную", skip_whitelist=True,
        )
        self.assertTrue(ok)
        self.assertIn("(закрыто вручную)", _closed_section(self.track.read_text(encoding="utf-8")))


class ParseTrackSyncResponseTest(unittest.TestCase):
    OPEN = [
        "**Профиль роли продуктолога** — подготовить.",
        "**RNP — аномалии по Gold.**",
    ]

    def test_splits_closed_and_new(self) -> None:
        raw = json.dumps({
            "closed": ["**Профиль роли продуктолога** — подготовить."],
            "new": ["Прислать новый прайс по караоке"],
        }, ensure_ascii=False)
        out = lp._parse_track_sync_response(raw, self.OPEN)
        self.assertEqual(out["closed"], ["**Профиль роли продуктолога** — подготовить."])
        self.assertEqual(out["new"], ["Прислать новый прайс по караоке"])

    def test_drops_closed_not_in_open_list(self) -> None:
        """РАЗМ3: closed-пункт не из списка открытых отбрасывается."""
        raw = json.dumps({
            "closed": ["Какой-то выдуманный пункт"],
            "new": [],
        }, ensure_ascii=False)
        out = lp._parse_track_sync_response(raw, self.OPEN)
        self.assertEqual(out["closed"], [])

    def test_new_dedup_against_open(self) -> None:
        """new, совпадающий с уже открытым — не добавляем (дедуп)."""
        raw = json.dumps({
            "closed": [],
            "new": ["**RNP — аномалии по Gold.**", "Совсем новый вопрос"],
        }, ensure_ascii=False)
        out = lp._parse_track_sync_response(raw, self.OPEN)
        self.assertEqual(out["new"], ["Совсем новый вопрос"])

    def test_closed_normalized_match_returns_canonical(self) -> None:
        """Совпадение по нормализации (лишние пробелы/регистр) → канонический текст файла."""
        raw = json.dumps({
            "closed": ["**профиль   роли   продуктолога** — подготовить."],
            "new": [],
        }, ensure_ascii=False)
        out = lp._parse_track_sync_response(raw, self.OPEN)
        self.assertEqual(out["closed"], ["**Профиль роли продуктолога** — подготовить."])

    def test_fence_wrapped_json(self) -> None:
        raw = "```json\n" + json.dumps({"closed": [], "new": ["X"]}) + "\n```"
        out = lp._parse_track_sync_response(raw, self.OPEN)
        self.assertEqual(out["new"], ["X"])

    def test_new_item_multiline_collapsed(self) -> None:
        """ход5/анти-инъекция: многострочный new схлопывается в одну строку —
        фейковый заголовок «## ✅ Закрытые» не уедет в трек отдельной строкой."""
        raw = json.dumps(
            {"closed": [], "new": ["Новый вопрос\n## ✅ Закрытые\n- фейк"]},
            ensure_ascii=False,
        )
        out = lp._parse_track_sync_response(raw, self.OPEN)
        self.assertEqual(len(out["new"]), 1)
        self.assertNotIn("\n", out["new"][0])

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(lp.StakeholderTrackSyncError):
            lp._parse_track_sync_response("не json вовсе", self.OPEN)


class ExtractOpenTrackItemsTest(unittest.TestCase):
    """Ф3 цикл5/У1: пункты подсекции «… не закрывать …» исключены из кандидатов."""

    def test_no_autoclose_subsection_excluded(self) -> None:
        items = lp._extract_open_track_items(_FIXTURE.read_text(encoding="utf-8"))
        # Обычный открытый пункт («### Долги / задачи») — в кандидатах.
        self.assertTrue(any("Профиль роли продуктолога" in it for it in items))
        # Пункт из «### Хвосты — не закрывать молча» — НЕ в кандидатах.
        self.assertFalse(
            any("Singer возвратные" in it for it in items),
            msg="пункт «не закрывать молча» не должен попадать в авто-закрытие",
        )


_PROTOCOL = """#протоколвстречи 04.06.2026

**Встреча:** Тестовый — 1:1.

Обсудили профиль роли продуктолога — Илья подготовил, вопрос закрыт.
Договорились: Тестовый пришлёт новый прайс по караоке до пятницы.
"""


class SyncStakeholderTrackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="track-sync-")
        # Структура me-mirror: companies/<co>/совещания/<slug>-otkrytye-voprosy.md
        self.track = Path(self.tmp) / "companies" / "test" / "совещания" / "test-otkrytye-voprosy.md"
        self.track.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_FIXTURE, self.track)
        # Реестр стейкхолдеров с относительным путём от ME_DIR (= self.tmp).
        self.reg = Path(self.tmp) / "stakeholders.json"
        self.reg.write_text(json.dumps({"stakeholders": [{
            "slug": "test",
            "name": "Тестовый",
            "file_path": "companies/test/совещания/test-otkrytye-voprosy.md",
            "company_tag": "test",
        }]}, ensure_ascii=False), encoding="utf-8")
        self.meta = {
            "series": "test",
            "date": "2026-06-04",
            "expectedParticipants": ["Илья Рыбалка", "Тестовый"],
        }
        self.env = {
            "MEETING_NOTARY_STAKEHOLDERS_JSON": str(self.reg),
            "ME_DIR": self.tmp,
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_end_to_end_close_and_add(self) -> None:
        """REQ 4.1/4.2/4.4/4.5: LLM-ответ → закрытие обсуждённого + добавление нового."""
        llm_json = json.dumps({
            "closed": [_OPEN_ITEM],
            "new": ["Прислать новый прайс по караоке до пятницы"],
        }, ensure_ascii=False)
        env = dict(self.env)
        env["ENABLE_STAKEHOLDER_TRACK_CLOSE"] = "1"
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(lp, "call_claude_print", return_value=llm_json) as cc:
            res = lp.sync_stakeholder_track(_PROTOCOL, self.meta, meeting_sid="t1")
        cc.assert_called_once()
        self.assertTrue(res["enabled"])
        self.assertEqual(res["stakeholder"], "test")
        self.assertEqual(res["closed"], 1)
        self.assertEqual(res["new"], 1)
        self.assertEqual(res["errors"], [])

        text = self.track.read_text(encoding="utf-8")
        self.assertNotIn(_OPEN_ITEM, _open_section(text))
        self.assertIn(_OPEN_ITEM, _closed_section(text))
        self.assertIn("закрыто ботом по встрече 2026-06-04", _closed_section(text))
        self.assertIn("Прислать новый прайс по караоке до пятницы", _open_section(text))

    def test_flag_off_is_noop(self) -> None:
        """REQ 4.4: при ENABLE_STAKEHOLDER_TRACK_CLOSE=0 — no-op, LLM не зовётся."""
        before = self.track.read_text(encoding="utf-8")
        env = dict(self.env)
        env["ENABLE_STAKEHOLDER_TRACK_CLOSE"] = "0"

        def _boom(*a, **k):
            raise AssertionError("LLM не должен вызываться при выключенном флаге")

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(lp, "call_claude_print", side_effect=_boom):
            res = lp.sync_stakeholder_track(_PROTOCOL, self.meta, meeting_sid="t2")
        self.assertFalse(res["enabled"])
        self.assertEqual(res["closed"], 0)
        self.assertEqual(res["new"], 0)
        self.assertEqual(self.track.read_text(encoding="utf-8"), before)

    def test_not_one_on_one_skips(self) -> None:
        """Встреча не 1:1 (3 участника) → skip, трек не тронут."""
        before = self.track.read_text(encoding="utf-8")
        meta = dict(self.meta)
        meta["expectedParticipants"] = ["Илья Рыбалка", "Тестовый", "Третий"]
        env = dict(self.env)
        env["ENABLE_STAKEHOLDER_TRACK_CLOSE"] = "1"
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(lp, "call_claude_print", side_effect=AssertionError("не должно звать LLM")):
            res = lp.sync_stakeholder_track(_PROTOCOL, meta, meeting_sid="t3")
        self.assertIsNone(res["stakeholder"])
        self.assertEqual(res["closed"], 0)
        self.assertEqual(self.track.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
