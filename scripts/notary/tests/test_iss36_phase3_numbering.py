"""Тесты Ф3 (план `2026-07-16-open-questions-reply-fix.md`) — нумерация висяков
(дайджест + протокол), единый канонический порядок `ordered_open_tasks` и снапшот
нумерации ОБОИХ документов по message_id.

Покрывает REQ:
  - 5.1 — дайджест: сквозной номер «1.»…«N.» по всем секциям (безымянные → по людям),
    не рестартится в секции владельца;
  - 5.2 — протокол: раздел висяков пронумерован детерминированно (инструкция генератору);
  - 5.3 — номер СТРОГО display-only: в текст задачи / `_status_key` не входит (перенос-
    хвост не сдвигается, задача не «воскресает»); ординальная запись статуса владельца
    пишется под ПОЛНЫЙ key-текст переноса (RISK2).
  + A9/RISK3 — `ordered_open_tasks` строится ТЕМ ЖЕ пайплайном, что дайджест (merge
    sidecar-статусов), НЕ сырым `resolve_open_tasks` → sidecar-закрытые в нумерацию не
    попадают; A8 — мелочь не нумеруется; A14/Гейт 2 Q1 — снапшот обоих документов по
    message_id, полный key-текст по позициям.

RAZM2: фикстура НЕ all-open — несёт sidecar-закрытую задачу в хвосте + owned-задачу
с обрезанным именем + мелочь.

Опасная тройка: в лог снапшота — только счётчик позиций, не тексты.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss36_phase3_numbering -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
sys.path.insert(0, str(_NOTARY))

from lib import morning_digest as md  # noqa: E402
from lib import series_memory as sm  # noqa: E402

_SERIES = "koordinatsiya"
_DATE = "2026-07-15"

# RAZM2-хвост: безымянный + owned-закрытый (в хвосте) + два owned-открытых (имя
# срезается в подзаголовок) + мелочь. НЕ all-open.
_NAMELESS = "определиться со сроком запуска"
_CLOSED = "Илья: подготовить расчёт по марже"       # закроем через sidecar
_OWNED_1 = "Илья: решить по найму"                   # display «решить по найму»
_OWNED_2 = "Татьяна: согласовать договор с типографией"
_MINOR = "Илья: отправить макет в печать"

# Ожидаемый ВИСЯЩИЙ порядок (после наложения sidecar-статусов): безымянный → Илья →
# Татьяна. Закрытый и мелочь ОТСУТСТВУЮТ (RISK3/A8).
_EXPECTED_KEYTEXTS = [_NAMELESS, _OWNED_1, _OWNED_2]


def _make_series(root: Path) -> Path:
    """Каталог серии с одной свежей выжимкой RAZM2 + sidecar-закрытой задачей в хвосте."""
    sd = root / _SERIES
    sd.mkdir(parents=True, exist_ok=True)
    sm.save_digest(sd, _DATE, {
        "schema": 1, "date": _DATE, "series": _SERIES,
        "participants": ["Илья Рыбалка", "Татьяна Филиппова"],
        "themes": [], "key_points": [],
        "open_tasks": [
            _NAMELESS, _CLOSED, _OWNED_1, _OWNED_2,
            {"text": _MINOR, "minor": True},
        ],
    })
    # sidecar-закрытие: терминально закрытая задача в хвосте не должна попасть в
    # нумерацию (RISK3 — иначе номер человека ≠ order матчера).
    sm.set_task_status(sd, _CLOSED, sm.STATUS_DONE, reason="по ответу",
                       source="reply", date=_DATE)
    return sd


# ==========================================================================
# A9/RISK3/A8 — единый канонический порядок `ordered_open_tasks`
# ==========================================================================
class TestOrderedOpenTasks(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = _make_series(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_positions_structure_keytext_display_owner(self):
        # RISK2: key_text — ПОЛНЫЙ («Имя: задача»); display — с срезанным именем.
        pos = sm.ordered_open_task_positions([_NAMELESS, _OWNED_1, _OWNED_2])
        self.assertEqual([p["key_text"] for p in pos], _EXPECTED_KEYTEXTS)
        self.assertEqual(pos[0]["display_text"], _NAMELESS)      # безымянный — полный
        self.assertEqual(pos[0]["owner"], None)
        self.assertEqual(pos[1]["display_text"], "решить по найму")  # имя срезано
        self.assertEqual(pos[1]["owner"], "Илья")
        self.assertEqual(pos[2]["owner"], "Татьяна")

    def test_pipeline_excludes_sidecar_closed_and_minor(self):
        # RISK3: строим merge-пайплайном → sidecar-закрытый _CLOSED отсутствует;
        # A8: мелочь _MINOR отсутствует.
        keytexts = [p["key_text"] for p in sm.ordered_open_tasks(self.dir)]
        self.assertEqual(keytexts, _EXPECTED_KEYTEXTS)
        self.assertNotIn(_CLOSED, keytexts)
        self.assertNotIn(_MINOR, keytexts)

    def test_ordered_matches_digest_open_bucket(self):
        # `ordered_open_tasks(series_dir)` == порядок показа дайджеста (collect_items open).
        hanging = md.collect_items(self.dir)["open"]
        self.assertEqual(hanging, _EXPECTED_KEYTEXTS)
        keytexts = [p["key_text"] for p in sm.ordered_open_tasks(self.dir)]
        self.assertEqual(keytexts, hanging)

    def test_empty_series_dir_gives_empty(self):
        self.assertEqual(sm.ordered_open_tasks(self.root / "no-such"), [])
        self.assertEqual(sm.ordered_open_task_positions([]), [])


# ==========================================================================
# REQ 5.1 — нумерация дайджеста сквозная, не рестартится
# ==========================================================================
class TestDigestNumbering(unittest.TestCase):

    def test_numbering_sequential_across_sections(self):
        out = md.build_digest_markdown(_SERIES, _DATE, _EXPECTED_KEYTEXTS, [])
        # безымянный=1; Илья=2 (НЕ рестарт на 1 в секции владельца); Татьяна=3
        self.assertIn(f"1. {_NAMELESS}", out)
        self.assertIn("## Илья", out)
        self.assertIn("2. решить по найму", out)
        self.assertIn("## Татьяна", out)
        self.assertIn("3. согласовать договор с типографией", out)
        # номер показа N → key_text позиции N-1 (порядок совпал)
        pos = sm.ordered_open_task_positions(_EXPECTED_KEYTEXTS)
        self.assertEqual(pos[1]["key_text"], _OWNED_1)

    def test_number_does_not_restart_owner_only(self):
        # Только owned, два владельца — сквозной 1..3, второй владелец не с 1.
        out = md.build_digest_markdown(
            _SERIES, _DATE, ["Илья: A-задача", "Илья: B-задача", "Пётр: C-задача"], [])
        self.assertIn("1. A-задача", out)
        self.assertIn("2. B-задача", out)
        self.assertIn("3. C-задача", out)  # НЕ «1. C-задача»

    def test_minor_not_numbered(self):
        # A8: мелочь буллетом «- », без номера.
        out = md.build_digest_markdown(_SERIES, _DATE, ["Илья: расчёт"], ["Илья: мелочь"])
        self.assertIn("- Илья: мелочь", out)
        self.assertNotIn("1. Илья: мелочь", out)
        self.assertNotIn("2.", out)  # висяк один → номера мелочи «2.» нет


# ==========================================================================
# REQ 5.3 — номер display-only: `_status_key` инвариантен, задача не воскресает
# ==========================================================================
class TestStatusKeyInvariant(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = _make_series(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keytext_status_key_equals_original(self):
        # Номер в key_text не попадает: _status_key(key_text) == _status_key(исходной строки).
        pos = sm.ordered_open_task_positions([_NAMELESS, _OWNED_1, _OWNED_2])
        for p, original in zip(pos, _EXPECTED_KEYTEXTS):
            self.assertEqual(sm._status_key(p["key_text"]), sm._status_key(original))

    def test_ordinal_close_of_owned_writes_under_transfer_key(self):
        # RISK2: закрытие ординалом задачи ВЛАДЕЛЬЦА пишет статус под ключ == _status_key
        # переносимой строки «Имя: задача», НЕ под чужой/фантомный (тихая потеря).
        pos = sm.ordered_open_tasks(self.dir)
        owned = pos[1]  # Илья: решить по найму
        self.assertEqual(owned["key_text"], _OWNED_1)
        sm.set_task_status(self.dir, owned["key_text"], sm.STATUS_DONE, date=_DATE)
        store = sm.load_task_status(self.dir)
        self.assertIn(sm._status_key(_OWNED_1), store)
        self.assertEqual(store[sm._status_key(_OWNED_1)]["status"], sm.STATUS_DONE)

    def test_task_does_not_resurrect_after_ordinal_close(self):
        # Закрыли по ординалу → на следующем рендере задача не висит (не воскресает).
        sm.set_task_status(self.dir, _OWNED_1, sm.STATUS_DONE, date=_DATE)
        after = [p["key_text"] for p in sm.ordered_open_tasks(self.dir)]
        self.assertNotIn(_OWNED_1, after)
        self.assertEqual(after, [_NAMELESS, _OWNED_2])  # перенумеровался сдвигом, без «дыры»


# ==========================================================================
# REQ 5.2 — нумерация протокола детерминированно из ordered_open_task_positions
# ==========================================================================
class TestProtocolNumbering(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = _make_series(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_block_numbered_continuous_with_subheadings(self):
        block = sm.format_open_tasks_block(
            [_NAMELESS, "Илья: смета по логистике", "Илья: найм РОПа",
             "Татьяна: макет обложки"])
        self.assertIn(f"1. {_NAMELESS}", block)   # безымянный первым
        self.assertIn("2. смета по логистике", block)
        self.assertIn("3. найм РОПа", block)
        self.assertIn("4. макет обложки", block)  # сквозной, не рестарт у Татьяны
        self.assertIn("**Илья**", block)
        self.assertIn("**Татьяна**", block)
        # инструкция велит LLM сохранить номера дословно (показ не расходится со слепком)
        self.assertIn("СОХРАНИ номер", block)

    def test_open_sink_captures_ordered_keytexts_matching_show(self):
        # A10: слепок (open_sink) = те же key-тексты и порядок, что показ протокола →
        # номер показа == позиция слепка.
        digs = sm.list_series_digests(self.dir)
        sink: list = []
        block = sm.build_open_tasks_block(
            digs, series_dir=self.dir, date=_DATE, open_sink=sink)
        self.assertTrue(block)  # раздел висяков собран
        self.assertEqual(sink, _EXPECTED_KEYTEXTS)
        # показ несёт те же номера, что позиции слепка
        self.assertIn("2. решить по найму", block)   # sink[1] == _OWNED_1
        self.assertEqual(sink[1], _OWNED_1)

    def test_open_sink_stays_empty_when_no_open_section(self):
        # Нет висяков (пустой хвост) → раздел не строится, слепок пуст (снапшот не пишем).
        empty_dir = self.root / "empty-series"
        empty_dir.mkdir()
        sm.save_digest(empty_dir, _DATE, {
            "schema": 1, "date": _DATE, "series": "empty-series", "open_tasks": []})
        sink: list = []
        block = sm.build_open_tasks_block(
            sm.list_series_digests(empty_dir), series_dir=empty_dir, date=_DATE, open_sink=sink)
        self.assertEqual(block, "")
        self.assertEqual(sink, [])


# ==========================================================================
# REQ 5.1 (рендер) — сквозная нумерация ПЕРЕЖИВАЕТ bleach (атрибут `ol[start]`)
# ==========================================================================
class TestRenderNumberingSurvivesBleach(unittest.TestCase):
    """Каждая секция — отдельный `<ol>`; markdown ставит `start="N"` у 2-й и далее.
    Если bleach срежет `start`, браузер отрисует каждую секцию с «1.» → показ
    разойдётся со слепком. Страж на allowlist `ol[start]` в protocol_to_pdf."""

    def test_ol_start_survives_bleach_numbers_continuous(self):
        from lib import protocol_to_pdf as pp
        out = md.build_digest_markdown(
            _SERIES, _DATE,
            [_NAMELESS, "Илья: подготовить расчёт", _OWNED_1, _OWNED_2], [])
        try:
            html = pp.markdown_to_html(out, title="T", subtitle="S")
        except pp.PdfRenderError as e:
            self.skipTest(f"markdown/bleach недоступны в этом venv: {e}")
        body = html.split("</style>")[-1]  # без CSS-шума из <head>
        # безымянный = <ol> (start=1 неявно), Илья = start=2, Татьяна = start=4
        self.assertIn('<ol start="2">', body)
        self.assertIn('<ol start="4">', body)


# ==========================================================================
# A14/Гейт 2 Q1 — снапшот нумерации ОБОИХ документов по message_id
# ==========================================================================
class TestSnapshot(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = _make_series(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_digest_snapshot_recorded_by_message_id(self):
        # Полный путь дайджеста: send_series_digest → снапшот по message_id.
        captured: dict = {}

        def _sender(chat_id, md_text, caption, date):
            captured["md"] = md_text
            return 5555

        res = md.send_series_digest(
            _SERIES, date=_DATE, root=self.root,
            chat_resolver=lambda _s: -100500, sender=_sender)
        self.assertEqual(res["status"], "sent", res)
        snap = sm.open_tasks_snapshot_for_message(self.dir, 5555)
        self.assertEqual(snap, _EXPECTED_KEYTEXTS)
        # позиция N (номер показа) → snap[N-1] ПОЛНЫЙ key-текст (RISK2)
        self.assertIn("2. решить по найму", captured["md"])
        self.assertEqual(snap[1], _OWNED_1)

    def test_protocol_snapshot_roundtrip(self):
        # Точка отправки протокола (finalize) пишет тот же слепок по message_id протокола.
        sink = [_NAMELESS, _OWNED_1, _OWNED_2]
        sm.record_open_tasks_snapshot(
            self.dir, 9001, sink, surface="protocol", date=_DATE, chat_id=-100500)
        self.assertEqual(sm.open_tasks_snapshot_for_message(self.dir, 9001), sink)

    def test_snapshot_backcompat_missing_returns_none(self):
        # A14: нет записи на этот message_id / нет файла вовсе → None (ординал недоступен,
        # миграции нет).
        sm.record_open_tasks_snapshot(self.dir, 9001, [_OWNED_1], surface="protocol", date=_DATE)
        self.assertIsNone(sm.open_tasks_snapshot_for_message(self.dir, 8888))
        self.assertIsNone(sm.open_tasks_snapshot_for_message(self.root / "no-such", 9001))

    def test_snapshot_string_and_int_message_id_interop(self):
        # message_id пишем int, читаем и по int — ключуем через str(int(...)).
        sm.record_open_tasks_snapshot(self.dir, 4242, [_OWNED_2], surface="digest", date=_DATE)
        self.assertEqual(sm.open_tasks_snapshot_for_message(self.dir, 4242), [_OWNED_2])

    def test_snapshot_log_carries_no_task_text(self):
        # Опасная тройка: в лог — только счётчик позиций, не текст задачи.
        with self.assertLogs("meeting_notary.series_memory", level="INFO") as cm:
            sm.record_open_tasks_snapshot(
                self.dir, 7001, ["Илья: секретная маржа пять миллионов"],
                surface="protocol", date=_DATE)
        joined = " ".join(cm.output)
        self.assertNotIn("маржа", joined)
        self.assertNotIn("секретн", joined)
        self.assertIn("positions=1", joined)


# ==========================================================================
# Интеграция: finalize реально проводит слепок протокола (source-scan, как
# test_both_call_sites_pass_date — без запуска тяжёлого finalize)
# ==========================================================================
class TestFinalizeWiring(unittest.TestCase):

    def test_finalize_passes_open_sink_and_records_protocol_snapshot(self):
        src = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        self.assertIn("open_sink=pending_open_positions", src)
        self.assertIn("record_open_tasks_snapshot", src)
        self.assertIn('surface="protocol"', src)

    def test_digest_send_records_snapshot(self):
        src = (_NOTARY / "lib" / "morning_digest.py").read_text(encoding="utf-8")
        self.assertIn("record_open_tasks_snapshot", src)
        self.assertIn('surface="digest"', src)


if __name__ == "__main__":
    unittest.main()
