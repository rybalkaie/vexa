"""Тесты Ф4 (план `2026-07-14-reliability-and-open-tasks-ux.md`) — ISS-34: отображение
висяков в протоколе и утреннем дайджесте.

Покрывает REQ 34.1 / 34.2 / 34.3:
  - 34.1: каждый ПЕРЕНОСИМЫЙ висяк несёт origin-дату «от DD.MM.YYYY» (A4, без слова
    «висит»); свежий пункт текущей встречи (origin == дата протокола) — БЕЗ префикса
    (ОЖИД1). Origin-дата вычисляется сканом выжимок серии (A3), без миграции схемы.
  - 34.2: статус висяка словом+цветом в обоих PDF — «Статус неизвестен — уточнить»
    оранжевым (`status-open`), «Закрыто» зелёным жирным (`status-done`); статусы вне
    пары DONE/«висит» (WAIT/CANCELLED/REOPEN) НЕ перекрашиваются и НЕ переименовываются
    в «неизвестен» (НЕС2).
  - 34.3: из шапки дайджеста убрана оправдательная строка «Это не аудит и не спрос…»,
    из промпта раздела висяков протокола — «дружеская/дружелюбная сверка», «НЕ контроль
    и НЕ аудит».

Плюс регрессы:
  - перенос висяка (`extract_open_tasks`) понимает НОВЫЕ канонические ярлыки — иначе
    статус осел бы в текст задачи и порвал бы carryover/дедуп (память
    review-checks-two-call-sites);
  - консистентность канонических строк между `series_memory` (пишет) и
    `protocol_to_pdf` (красит) — единый источник не разъехался.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_iss34_phase4_display -v
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
from lib import protocol_to_pdf as pdf  # noqa: E402
from lib import series_memory as sm  # noqa: E402


def _dig(date, open_tasks):
    return {"series": "koordinatsiya", "date": date, "open_tasks": list(open_tasks)}


# ==========================================================================
# 34.1 — origin-даты (helper + prefix + ОЖИД1)
# ==========================================================================
class TestOriginDates(unittest.TestCase):

    def test_origin_dates_earliest_scan(self):
        # A3: origin = самая ранняя выжимка с этим _status_key; скан по возрастанию.
        digests = [
            _dig("2026-07-01", ["Илья: подготовить расчёт (срок: пт)"]),
            _dig("2026-07-08", ["Илья: подготовить расчёт (срок: 15.07)", "Татьяна: макет"]),
            _dig("2026-07-15", ["Илья: подготовить расчёт", "Татьяна: макет", "новая на 15-е"]),
        ]
        origins = sm.origin_dates_for_tasks(digests)
        # срок переформулирован между встречами — ключ (_status_key) не сменился → 07-01
        self.assertEqual(origins[sm._status_key("Илья: подготовить расчёт")], "2026-07-01")
        self.assertEqual(origins[sm._status_key("Татьяна: макет")], "2026-07-08")
        self.assertEqual(origins[sm._status_key("новая на 15-е")], "2026-07-15")

    def test_origin_dates_ignores_broken(self):
        origins = sm.origin_dates_for_tasks(
            [None, {"open_tasks": ["без даты"]}, _dig("2026-07-02", ["ок"])]
        )
        self.assertEqual(origins, {sm._status_key("ок"): "2026-07-02"})

    def test_prefix_format_ddmmyyyy_no_hangs_word(self):
        # A4: «от 07.07.2026» — год 4-значный, БЕЗ слова «висит».
        origins = {sm._status_key("A"): "2026-07-07"}
        pref = sm.origin_prefix_for("A", origins, "2026-07-15")
        self.assertEqual(pref, "от 07.07.2026 ")
        self.assertNotIn("висит", pref)

    def test_fresh_item_no_prefix_ozhid1(self):
        # ОЖИД1: пункт, впервые появившийся на текущей встрече, префикса НЕ несёт.
        origins = {sm._status_key("свежая"): "2026-07-15"}
        self.assertEqual(sm.origin_prefix_for("свежая", origins, "2026-07-15"), "")

    def test_unknown_or_no_date_no_prefix(self):
        self.assertEqual(sm.origin_prefix_for("x", {}, "2026-07-15"), "")
        self.assertEqual(sm.origin_prefix_for("x", {sm._status_key("x"): "2026-07-01"}, None), "")
        # не-ISO дата в выжимке → префикс не появляется (мягкая деградация)
        self.assertEqual(
            sm.origin_prefix_for("x", {sm._status_key("x"): "07/2026"}, "2026-07-15"), ""
        )


# ==========================================================================
# 34.1 — origin-даты в блоке протокола и в дайджесте (переносимый / свежий)
# ==========================================================================
class TestOriginInProtocolBlock(unittest.TestCase):

    def test_carried_gets_prefix_fresh_does_not(self):
        origins = {
            sm._status_key("Илья: подготовить расчёт"): "2026-07-01",
            sm._status_key("новая на 15-е"): "2026-07-15",
        }
        block = sm.format_open_tasks_block(
            ["Илья: подготовить расчёт", "новая на 15-е"],
            origins=origins, current_date="2026-07-15",
        )
        # Ф3: безымянная «новая на 15-е» = позиция 1, «Илья: подготовить расчёт» = 2
        self.assertIn("2. от 01.07.2026 подготовить расчёт", block)  # переносимый под owner
        self.assertIn("1. новая на 15-е", block)                    # свежий безымянный
        self.assertNotIn("от 15.07.2026", block)                    # ОЖИД1 — без префикса

    def test_no_origins_no_prefix_backcompat(self):
        # Прямой вызов без origins (обр. совместимость) — префиксов нет, блок валиден.
        block = sm.format_open_tasks_block(["Илья: добить отчёт"])
        # Ф3: единственный висяк = позиция 1; отсутствие «от …» между номером и текстом
        # подтверждает, что origin-префикса нет (иначе было бы «1. от … добить отчёт»).
        self.assertIn("1. добить отчёт", block)

    def test_build_open_tasks_block_wires_origins_end_to_end(self):
        # Полный путь: 2 выжимки на диске → блок несёт origin-дату переносимого пункта.
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koordinatsiya"
            sd.mkdir()
            sm.save_digest(sd, "2026-07-01", _dig("2026-07-01", ["Илья: подготовить расчёт"]))
            sm.save_digest(sd, "2026-07-08", _dig("2026-07-08", ["Илья: подготовить расчёт"]))
            digs = sm.list_series_digests(sd)
            block = sm.build_open_tasks_block(
                digs, series_dir=sd, meeting_sid="m", date="2026-07-15",
            )
            self.assertIn("от 01.07.2026 подготовить расчёт", block)


class TestOriginInDigest(unittest.TestCase):

    def test_digest_carried_prefix_and_status_label(self):
        origins = {sm._status_key("Илья: подготовить расчёт"): "2026-07-01"}
        out = md.build_digest_markdown(
            "koordinatsiya", "2026-07-15",
            ["Илья: подготовить расчёт"], [], origins=origins,
        )
        # переносимый висяк: номер (Ф3) + origin-префикс + канонический ярлык
        self.assertIn(
            "1. от 01.07.2026 подготовить расчёт — Статус неизвестен — уточнить", out
        )

    def test_digest_fresh_no_prefix_ozhid1(self):
        origins = {sm._status_key("свежая на сегодня"): "2026-07-15"}
        out = md.build_digest_markdown(
            "koordinatsiya", "2026-07-15", ["свежая на сегодня"], [], origins=origins,
        )
        self.assertIn("1. свежая на сегодня — Статус неизвестен — уточнить", out)
        self.assertNotIn("от 15.07.2026", out)

    def test_digest_minor_no_status_label(self):
        # Мелочь статус-ярлыка НЕ несёт (по ней статус не нужен, R12) и не красится.
        out = md.build_digest_markdown(
            "koordinatsiya", "2026-07-15", ["Илья: расчёт"], ["Илья: мелочь"],
        )
        self.assertIn("- Илья: мелочь", out)
        self.assertNotIn("мелочь — Статус неизвестен", out)

    def test_send_digest_computes_origins_from_history(self):
        # Полный путь дайджеста: send_series_digest → collect_items → origins →
        # build_digest_markdown → sender получает md с origin-датой переносимого пункта.
        captured = {}

        def _sender(chat_id, md_text, caption, date):
            captured["md"] = md_text
            return 42

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sd = root / "koordinatsiya"
            sd.mkdir()
            sm.save_digest(sd, "2026-07-01", _dig("2026-07-01", ["Илья: подготовить расчёт"]))
            sm.save_digest(sd, "2026-07-08", _dig("2026-07-08", ["Илья: подготовить расчёт"]))
            res = md.send_series_digest(
                "koordinatsiya", date="2026-07-15", root=root,
                chat_resolver=lambda s: 777, sender=_sender,
            )
        self.assertEqual(res["status"], "sent", res)
        self.assertIn("от 01.07.2026 подготовить расчёт", captured["md"])


# ==========================================================================
# 34.3 — нейтральный тон (нет оправданий)
# ==========================================================================
class TestNeutralTone(unittest.TestCase):

    def test_protocol_block_has_no_defensive_tone(self):
        block = sm.format_open_tasks_block(["Илья: добить отчёт"])
        low = block.lower()
        self.assertNotIn("не контроль", low)
        self.assertNotIn("не аудит", low)
        self.assertNotIn("дружелюб", low)
        self.assertNotIn("дружеск", low)
        # запрет давящих формулировок при этом остаётся
        self.assertIn("просрочк", low)

    def test_digest_tone_line_neutral_a7(self):
        out = md.build_digest_markdown("s", "2026-07-15", ["Илья: расчёт"], [])
        self.assertNotIn("Это не аудит и не спрос", out)
        # A7 — утверждённый владельцем текст
        self.assertIn("Список открытых вопросов серии на сегодня", out)
        self.assertIn("Если вопрос уже неактуален", out)

    def test_digest_keeps_owner_instruction_verbatim(self):
        # A7: HEADER_INSTRUCTION (слова владельца) НЕ трогаем.
        out = md.build_digest_markdown("s", "2026-07-15", ["Илья: расчёт"], [])
        self.assertIn(md.HEADER_INSTRUCTION, out)


# ==========================================================================
# 34.2 — статус словом/цветом (раскраска PDF + НЕС2)
# ==========================================================================
class TestStatusColoring(unittest.TestCase):

    def test_colorize_both_labels(self):
        html = "<li>задача A — Статус неизвестен — уточнить</li><li>задача B — Закрыто</li>"
        out = pdf._colorize_status_labels(html)
        self.assertIn('<span class="status-open">Статус неизвестен — уточнить</span>', out)
        self.assertIn('<span class="status-done">Закрыто</span>', out)

    def test_colorize_matches_canonical_labels_no_drift(self):
        # Единый источник строк: series_memory пишет ровно то, что protocol_to_pdf красит.
        open_html = pdf._colorize_status_labels(f"<li>x — {sm.STATUS_OPEN_LABEL}</li>")
        done_html = pdf._colorize_status_labels(f"<li>x — {sm.STATUS_DONE_LABEL}</li>")
        self.assertIn('class="status-open"', open_html)
        self.assertIn('class="status-done"', done_html)

    def test_nes2_wait_not_recolored_not_renamed(self):
        # НЕС2: статус вне пары DONE/«висит» (WAIT «жду расчёт») не красится и не
        # переименовывается в «неизвестен».
        html = "<li>задача C — жду расчёт</li>"
        out = pdf._colorize_status_labels(html)
        self.assertEqual(out, html)  # не изменён
        self.assertNotIn("status-open", out)
        self.assertNotIn("неизвест", out)

    def test_closed_subsection_heading_not_colored(self):
        # Заголовок Части 2 «✅ Закрыто с прошлых встреч»: «Закрыто» после «✅ », не «— »
        # → зелёным НЕ красим (иначе покрасили бы половину заголовка).
        html = "<p>✅ Закрыто с прошлых встреч</p>"
        out = pdf._colorize_status_labels(html)
        self.assertEqual(out, html)
        self.assertNotIn("status-done", out)

    def test_lowercase_zakryto_in_task_text_not_colored(self):
        # «проверить, всё ли закрыто» — строчное «закрыто» без «— » разделителя не красим.
        html = "<li>проверить, всё ли по складу закрыто — Статус неизвестен — уточнить</li>"
        out = pdf._colorize_status_labels(html)
        self.assertNotIn("status-done", out)
        self.assertIn("status-open", out)  # висящий статус при этом покрашен

    def test_css_classes_defined_with_owner_colors(self):
        self.assertIn(".status-open", pdf.CSS)
        self.assertIn("#c05621", pdf.CSS)   # A5: оранжевый «уточнить»
        self.assertIn(".status-done", pdf.CSS)
        self.assertIn("#2f855a", pdf.CSS)   # A5: зелёный «Закрыто»

    def test_pdf_allowlist_permits_span_class_only(self):
        self.assertEqual(pdf._PDF_ALLOWED_ATTRS.get("span"), ["class"])
        # style НЕ разрешён (безопасность allowlist)
        self.assertNotIn("style", pdf._PDF_ALLOWED_ATTRS.get("span", []))

    def test_markdown_render_colors_survive(self):
        # Ярлык переживает markdown-рендер и красится (markdown есть в окружении).
        try:
            import markdown  # noqa: F401,PLC0415
        except ModuleNotFoundError:
            self.skipTest("markdown не установлен")
        body = "## Вопросы\n- задача A — Статус неизвестен — уточнить\n- задача B — Закрыто\n"
        html = pdf._colorize_status_labels(
            markdown.markdown(body, extensions=["sane_lists"])
        )
        self.assertIn('class="status-open"', html)
        self.assertIn('class="status-done"', html)

    def test_both_pdfs_colored_end_to_end(self):
        # REQ 34.2 «оба PDF через один генератор»: protocol И digest markdown → PDF-HTML
        # → канонические статус-span'ы. Требует markdown+bleach (как на VPS).
        try:
            import markdown  # noqa: F401,PLC0415
            import bleach  # noqa: F401,PLC0415
        except ModuleNotFoundError:
            self.skipTest("markdown/bleach не установлены в этом окружении")
        origins = {sm._status_key("Илья: расчёт"): "2026-07-01"}
        proto_block = sm.format_open_tasks_block(
            ["Илья: расчёт"], closed=[{"text": "смета", "status": sm.STATUS_DONE}],
            origins=origins, current_date="2026-07-15",
        )
        # эмулируем то, что модель проставит статус висящему по инструкции
        proto_md = "## Итог\n- расчёт — Закрыто\n- макет — Статус неизвестен — уточнить\n"
        proto_html = pdf.markdown_to_html(proto_md, "Протокол", "серия")
        self.assertIn('class="status-done"', proto_html)
        self.assertIn('class="status-open"', proto_html)
        self.assertIn("Илья", proto_block)  # блок собран

        digest_md = md.build_digest_markdown(
            "koordinatsiya", "2026-07-15", ["Илья: расчёт"], [], origins=origins,
        )
        digest_html = pdf.markdown_to_html(digest_md, "Дайджест", "Сверка перед встречей")
        self.assertIn('class="status-open"', digest_html)


# ==========================================================================
# Регресс — перенос висяка понимает новые ярлыки (carryover не рвётся)
# ==========================================================================
class TestCarryoverRoundTrip(unittest.TestCase):

    def test_new_hanging_label_stripped_on_carry(self):
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "- добить отчёт — Статус неизвестен — уточнить\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertIn("добить отчёт", carried)
        self.assertTrue(all("Статус неизвестен" not in c for c in carried))

    def test_new_closed_label_dropped_on_carry(self):
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "- согласовать смету — Закрыто\n"
            "- добить отчёт — Статус неизвестен — уточнить\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertNotIn("согласовать смету", carried)  # «Закрыто» → не несём дальше
        self.assertIn("добить отчёт", carried)

    def test_lowercase_zakryto_in_text_still_carried(self):
        # Консерватизм: «…всё ли закрыто» без «— »-разделителя статуса — не ложно-закрыто.
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "- проверить, всё ли по складу закрыто — Статус неизвестен — уточнить\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertIn("проверить, всё ли по складу закрыто", carried)

    def test_old_hangs_label_still_stripped_backcompat(self):
        # Выжимки в проде до Ф4 несут «— висит» — перенос обязан её тоже понимать.
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "- старая задача — висит\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertIn("старая задача", carried)
        self.assertTrue(all("висит" not in c for c in carried))

    def test_origin_prefix_stripped_on_carry_key_stable(self):
        # Цикл5 Н1: origin-префикс «от DD.MM.YYYY» — display-украшение; при переносе
        # ОБЯЗАН срезаться, иначе запекается в текст, меняет `_status_key` (теряется
        # sidecar-статус) и на след. встрече навешивается ВТОРОЙ префикс.
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "Незакрытые задачи по людям:\n"
            f"- от 01.07.2026 подготовить расчёт — {sm.STATUS_OPEN_LABEL}\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertEqual(carried, ["подготовить расчёт"])  # префикс срезан, ключ чист
        self.assertEqual(
            sm._status_key(carried[0]), sm._status_key("подготовить расчёт")
        )

    def test_origin_prefix_stripped_owned_before_name(self):
        # У owned-пункта префикс идёт ПЕРЕД именем: «от 01.07 задача» под «**Имя**» →
        # срезать надо до восстановления «Имя: задача».
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "Незакрытые задачи по людям:\n"
            "**Иван Петров**\n"
            f"- от 01.07.2026 сдать отчёт — {sm.STATUS_OPEN_LABEL}\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertEqual(carried, ["Иван Петров: сдать отчёт"])

    def test_origin_prefix_stripped_despite_date_format_drift(self):
        # Цикл5 У2: модель могла отронить ведущие нули («от 1.7.2026»); срез обязан
        # терпеть дрейф, иначе префикс запечётся так же, как без среза вовсе.
        for datestr in ("01.07.2026", "1.7.2026", "1.07.2026"):
            proto = (
                f"## {sm.PENDING_SECTION_HEADING}\n"
                "Незакрытые задачи по людям:\n"
                f"- от {datestr} подготовить расчёт — {sm.STATUS_OPEN_LABEL}\n"
            )
            carried = sm.extract_open_tasks(proto)
            self.assertEqual(
                carried, ["подготовить расчёт"], f"дрейф '{datestr}' не срезан"
            )

    def test_prepositional_ot_not_stripped_as_origin(self):
        # Предлог «от Ивана …» — НЕ дата, префикс-регэксп его не трогает (за «от» строго
        # требуется DD.MM.YYYY).
        proto = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "Незакрытые задачи по людям:\n"
            f"- от Ивана получить документы — {sm.STATUS_OPEN_LABEL}\n"
        )
        carried = sm.extract_open_tasks(proto)
        self.assertEqual(carried, ["от Ивана получить документы"])

    def test_origin_date_no_drift_across_three_meetings(self):
        # Сквозной инвариант REQ 34.1: после переноса через встречу 2 origin-дата пункта
        # на встрече 3 остаётся ПЕРВОЙ встречей (01.07), а не второй, и префикс один.
        proto_m2 = (
            f"## {sm.PENDING_SECTION_HEADING}\n"
            "Незакрытые задачи по людям:\n"
            f"- от 01.07.2026 подготовить расчёт — {sm.STATUS_OPEN_LABEL}\n"
        )
        stored_m2 = sm.extract_open_tasks(proto_m2)
        digests = [
            {"date": "2026-07-01", "open_tasks": ["подготовить расчёт"]},
            {"date": "2026-07-08", "open_tasks": stored_m2},
        ]
        origins = sm.origin_dates_for_tasks(digests)
        hanging = sm._resolve_open_tasks_raw(digests)
        prefix = sm.origin_prefix_for(hanging[0], origins, "2026-07-15")
        self.assertEqual(prefix, "от 01.07.2026 ")  # первая встреча, один префикс
        self.assertNotIn("от 08.07.2026", f"{prefix}{hanging[0]}")  # нет дрейфа/дубля


# ==========================================================================
# Регресс — паритет call-site: clarify тоже передаёт date (origin-даты)
# ==========================================================================
class TestCallSiteDateParity(unittest.TestCase):

    def test_both_call_sites_pass_date_to_build_open_tasks_block(self):
        # Память review-checks-two-call-sites: без date=... clarify-перевыпуск потерял бы
        # origin-даты висяков (origin_prefix_for вернул бы "" при current_date=None).
        for rel in ("finalize-meeting.py", "lib/clarify_worker.py"):
            src = (_NOTARY / rel).read_text(encoding="utf-8")
            i = src.find("build_open_tasks_block(")
            self.assertNotEqual(i, -1, f"{rel}: вызов build_open_tasks_block не найден")
            chunk = src[i:i + 400]
            self.assertIn("date=", chunk, f"{rel}: build_open_tasks_block без date=")


if __name__ == "__main__":
    unittest.main(verbosity=2)
