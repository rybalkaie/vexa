# -*- coding: utf-8 -*-
"""Ф3 (ISS-51…55): поведение СПРАВОЧНОГО режима серии (`last-only`).

Что проверяем (пункты Фазы 3 плана `2026-07-29-open-questions-mode-per-series.md`):
  • `carry` не изменился НИ НА БАЙТ — эталон снят с кода ДО правок (git HEAD) и
    вморожен строкой; любое изменение документа серии-накопления красит тест.
    ВАЖНО: эталон сверяется на ДЕФОЛТНОМ окружении (переменные не подставляем) —
    иначе тест слеп ровно к тому классу регрессии, который ловит (см. TZ ниже);
  • сразу после переключения в `last-only` в шпаргалку попадают ТОЛЬКО задачи
    последней прошедшей встречи, а не весь накопленный ком (РИСК2). Фикстуры —
    с ПЕРЕПИСАННЫМИ формулировками хвоста: на бою модель переписывает раздел «с
    прошлых встреч» на каждой генерации, и отбор «по совпадению текста» там не
    работает вовсе (пересечение соседних боевых выжимок 0/16/1/6/0 из ~18–25);
  • мелочь «просто напоминаю» входит, группировка по ответственным сохранена;
  • статусов не просим: нет нумерации, нет инструкции «ответьте статусом», нет
    ярлыка «Статус неизвестен — уточнить», слепок нумерации НЕ пишется;
  • возврат в `carry` включает слепок обратно с ПЕРВОГО же дайджеста;
  • раздел «Вопросы с прошлых встреч…» гейтится в ОБОИХ call-site протокола
    (finalize И clarify) — [[review-checks-two-call-sites]], грабли ISS-10. Сторож
    ИСПОЛНЯЕТ настоящий гейт из исходника, а не ищет подстроку: гейт, обезвреженный
    как `carries_open_questions(...) or True`, подстрочную проверку проходил;
  • следующая выжимка хвост НЕ наследует (протокол без раздела → нечего парсить);
  • статус-машина молчит: ночной сверщик пропускает серию (но буфер `doubt` при
    этом продолжает протухать), reply со статусом получает справочную фразу и
    НИЧЕГО не пишет в стор — во ВСЕХ роутах, включая ack-роут и ИИ-разбор;
  • fail-safe: реестр не прочитался (в т.ч. `SystemExit` из `_read_yaml` на
    настоящем битом файле), серии в нём нет, значение поля не читается, другая
    запись серии просит накопление → `carry` (старое поведение).

Плюс две находки, переданные в эту фазу:
  • ISS-55/Ф4: время встречи в дайджесте — из `MEETING_NOTARY_DISPLAY_TZ`, ДЕФОЛТ
    московский (дайджест печатал МСК с 07.2026; чужой дефолт менял бы документ
    серии-НАКОПЛЕНИЯ на глазах у людей — критерий 4 плана);
  • ISS-45/Ф1: переотправка по просьбе НЕ отменяет плановый дайджест дня и не
    ломает резолв ответов на предыдущее сообщение того же дня.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary/tests
    python3 test_iss51_last_only_digest.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent
_SCRIPTS = _NOTARY.parent
for _p in (str(_SCRIPTS), str(_NOTARY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notary.cli import registry as reg  # noqa: E402
from notary.lib import morning_digest as md  # noqa: E402
from notary.lib import pending_reconciler as pr  # noqa: E402
from notary.lib import series_memory as sm  # noqa: E402
import notary.meetings_listener as ml  # noqa: E402

MSK = timezone(timedelta(hours=3))
CARRY = sm.OPEN_QUESTIONS_MODE_CARRY
LAST_ONLY = sm.OPEN_QUESTIONS_MODE_LAST_ONLY

# --------------------------------------------------------------------------
# ЭТАЛОН carry. Снят прогоном ИСХОДНОГО `lib/morning_digest.py` (git HEAD, до
# Ф3) на тех же входных данных. Сверяется на ЧИСТОМ окружении: дайджест обязан
# давать этот документ БЕЗ подставленных переменных — именно так он работает на
# любом стенде, где `MEETING_NOTARY_DISPLAY_TZ` никто не задал (а её нет ни в
# `.env.notary.example` до этой фазы, ни в юнитах). Правишь документ накопления —
# эталон краснеет: так и задумано, менять его можно только осознанно.
# --------------------------------------------------------------------------
GOLDEN_CARRY_MD = """# Открытые вопросы к встрече

**Koordinatsiya** — 2026-07-15 · встреча в 12:00 МСК

Если вопрос будете проговаривать на сегодняшней встрече — статус можно не писать: бот услышит и пометит сам. Если по задаче всё уже известно и на встрече её не будет — напишите ответом на это сообщение текущий статус (сделано/не сделано, актуально/неактуально) и короткий комментарий — бот скорректирует свой учёт.

_Список открытых вопросов серии на сегодня. Если вопрос уже неактуален — просто напишите, снимем._

## Без ответственного / неразобрано

1. разобрать почту — Статус неизвестен — уточнить

## Илья

2. от 01.07.2026 подготовить расчёт по марже — Статус неизвестен — уточнить

## Татьяна

3. от 08.07.2026 согласовать договор — Статус неизвестен — уточнить

## Просто напоминаю (статус не нужен)

_Мелочи, статус по ним писать не нужно. Если никто не поднимет их на встрече — они снимутся сами._

- Илья: скинуть ссылку
"""

GOLDEN_CARRY_CAPTION = (
    "📋 Открытые вопросы к встрече — Koordinatsiya, 2026-07-15\n"
    "Открытых вопросов: 3 · мелочей «просто напоминаю»: 1\n\n"
    "Если вопрос будете проговаривать на сегодняшней встрече — статус можно не писать: "
    "бот услышит и пометит сам. Если по задаче всё уже известно и на встрече её не будет "
    "— напишите ответом на это сообщение текущий статус (сделано/не сделано, "
    "актуально/неактуально) и короткий комментарий — бот скорректирует свой учёт."
)

_GOLDEN_HANGING = ["Илья: подготовить расчёт по марже", "Татьяна: согласовать договор",
                   "разобрать почту"]
_GOLDEN_MINOR = ["Илья: скинуть ссылку"]


def _golden_origins():
    return {
        sm._status_key("Илья: подготовить расчёт по марже"): "2026-07-01",
        sm._status_key("Татьяна: согласовать договор"): "2026-07-08",
        sm._status_key("разобрать почту"): "2026-07-15",
    }


def _rec(series, mode=None, **fields):
    rec = {"id": f"{series}-evt", "series": series, "type": "manual",
           "cron": "0 11 * * 2", "tz": "Europe/Moscow"}
    if mode is not None:
        rec["open_questions_mode"] = mode
    rec.update(fields)
    return rec


def _watched(*recs):
    return {"watched": list(recs)}


def _registry(*recs):
    """Патч реестра режимов: боевой путь читает его через `load_watched_for_modes`."""
    return mock.patch.object(sm, "load_watched_for_modes", return_value=_watched(*recs))


def _clean_env():
    """Окружение БЕЗ подставленных переменных показа — как на чистом стенде."""
    env = {k: v for k, v in os.environ.items() if k != md.DISPLAY_TZ_ENV}
    return mock.patch.dict("os.environ", env, clear=True)


def _protocol_md(fresh, carried=(), *, title="Протокол") -> str:
    """Протокол в КАНОНИЧЕСКОЙ структуре: раздел «Задачи» = эта встреча, раздел
    «Вопросы с прошлых встреч…» = унаследованный хвост со статусом."""
    lines = [f"# {title}", "", "## Задачи", ""]
    lines += [f"- {t}" for t in fresh]
    if carried:
        lines += ["", f"## {sm.PENDING_SECTION_HEADING}", ""]
        lines += [f"- {t} — {sm.STATUS_OPEN_LABEL}" for t in carried]
    return "\n".join(lines) + "\n"


class _Series:
    """Каталог серии. `meeting()` кладёт встречу БОЕВЫМ путём: настоящий протокол
    на диск + выжимка настоящим `build_digest` — то есть ровно то, что читает
    утренний дайджест на проде. `write()` — короткий путь для случаев, где
    происхождение не важно (накопление)."""

    def __init__(self, tmp: Path, series: str = "koordinatsiya"):
        self.series = series
        self.root = tmp
        self.dir = tmp / series
        self.dir.mkdir(parents=True, exist_ok=True)

    def meeting(self, date: str, *, fresh=(), carried=(), minor=(), save_protocol=True):
        proto = _protocol_md(list(fresh) + list(minor), carried)
        minor_set = set(minor)

        def _significance(items):
            return [{"text": t, "minor": True} if t in minor_set else t for t in items]

        digest = sm.build_digest(
            proto, {"series": self.series, "date": date}, date=date,
            significance_filter=_significance if minor_set else None)
        sm.save_digest(self.dir, date, digest)
        if save_protocol:
            (self.dir / f"{date}-protokol.md").write_text(proto, encoding="utf-8")
        return digest

    def write(self, date: str, open_tasks: list, *, key_points=None, themes=None,
              born_here=None):
        payload = {
            "schema": 1, "date": date, "series": self.series,
            "participants": ["Илья Рыбалка", "Татьяна Филиппова"],
            "themes": list(themes or []), "key_points": list(key_points or []),
            "open_tasks": open_tasks,
        }
        if born_here is not None:
            payload[sm.OPEN_TASKS_BORN_HERE_KEY] = list(born_here)
        sm.save_digest(self.dir, date, payload)


# ==========================================================================
# 1. carry не изменился ни на байт
# ==========================================================================
class TestCarryUnchangedByteForByte(unittest.TestCase):

    def test_markdown_matches_pre_change_golden_on_clean_env(self):
        # Ни одной переменной не подставляем: документ накопления обязан быть тем
        # же самым на голом стенде. Раньше тест сам выставлял МСК в setUp и потому
        # не увидел, как дайджест уехал на час и потерял ярлык зоны.
        with _clean_env():
            out = md.build_digest_markdown(
                "koordinatsiya", "2026-07-15", _GOLDEN_HANGING, _GOLDEN_MINOR,
                start_at=datetime(2026, 7, 15, 12, 0, tzinfo=MSK),
                origins=_golden_origins(), mode=CARRY)
        self.assertEqual(out, GOLDEN_CARRY_MD)

    def test_prod_tz_value_gives_the_same_document(self):
        # На бою переменная выставлена в Europe/Moscow. Заданная и незаданная
        # обязаны давать ОДИН документ — иначе поведение зависит от того, дошли ли
        # руки прописать переменную на стенде.
        with mock.patch.dict("os.environ", {md.DISPLAY_TZ_ENV: "Europe/Moscow"}):
            out = md.build_digest_markdown(
                "koordinatsiya", "2026-07-15", _GOLDEN_HANGING, _GOLDEN_MINOR,
                start_at=datetime(2026, 7, 15, 12, 0, tzinfo=MSK),
                origins=_golden_origins(), mode=CARRY)
        self.assertEqual(out, GOLDEN_CARRY_MD)

    def test_default_mode_is_carry_for_direct_calls(self):
        # Прямой вызов без `mode` (тесты Ф4, ручная отладка) обязан давать ТОТ ЖЕ
        # документ, что до Ф3 — иначе «ни на байт» держалось бы только на caller'е.
        with _clean_env():
            out = md.build_digest_markdown(
                "koordinatsiya", "2026-07-15", _GOLDEN_HANGING, _GOLDEN_MINOR,
                start_at=datetime(2026, 7, 15, 12, 0, tzinfo=MSK),
                origins=_golden_origins())
        self.assertEqual(out, GOLDEN_CARRY_MD)

    def test_caption_matches_pre_change_golden(self):
        self.assertEqual(
            md.build_caption("koordinatsiya", "2026-07-15", _GOLDEN_HANGING, _GOLDEN_MINOR,
                             mode=CARRY),
            GOLDEN_CARRY_CAPTION)
        self.assertEqual(
            md.build_caption("koordinatsiya", "2026-07-15", _GOLDEN_HANGING, _GOLDEN_MINOR),
            GOLDEN_CARRY_CAPTION)

    def test_pdf_subtitle_unchanged_in_carry(self):
        self.assertEqual(md.digest_subtitle("2026-07-15", CARRY),
                         "Сверка перед встречей · 2026-07-15")
        self.assertEqual(md.digest_subtitle("2026-07-15"),
                         "Сверка перед встречей · 2026-07-15")

    def test_carry_collect_items_keeps_whole_tail(self):
        # Серия-накопление продолжает показывать ВЕСЬ хвост последней выжимки.
        with tempfile.TemporaryDirectory() as td:
            fx = _Series(Path(td))
            fx.meeting("2026-07-01", fresh=["Илья: задача от первой встречи"])
            fx.meeting("2026-07-08", carried=["Илья: задача от первой встречи"],
                       fresh=["Илья: задача второй"])
            items = md.collect_items(fx.dir, mode=CARRY)
        self.assertEqual(len(items["open"]), 2)


# ==========================================================================
# 2. Шпаргалка: только последняя встреча, без статусов
# ==========================================================================
class TestLastOnlyContent(unittest.TestCase):

    def _fixture(self, td):
        """Серия с накопленным комом. Хвост в каждом следующем протоколе
        ПЕРЕПИСАН другими словами — как это делает модель на бою."""
        fx = _Series(Path(td))
        fx.meeting("2026-07-01", fresh=["Илья: подготовить расчёт по марже за июнь"])
        fx.meeting(
            "2026-07-08",
            carried=["Илья: посчитать маржу (июнь), пока не готово"],
            fresh=["Татьяна: согласовать договор с сетью"])
        fx.meeting(
            "2026-07-15",
            carried=["Илья: маржа за июнь — всё ещё считают",
                     "Татьяна: договор с сетью — на подписи"],
            fresh=["Илья: свежий вопрос последней встречи"])
        return fx

    def test_only_tasks_born_on_last_meeting(self):
        # РИСК2 плана: первый же дайджест после переключения не должен вывалить ком.
        with tempfile.TemporaryDirectory() as td:
            fx = self._fixture(td)
            items = md.collect_items(fx.dir, mode=LAST_ONLY)
        self.assertEqual(items["open"], ["Илья: свежий вопрос последней встречи"])

    def test_rewritten_tail_would_fool_text_matching(self):
        """СТОРОЖ САМОГО СТОРОЖА. Проверяет, что фикстура действительно
        воспроизводит боевой случай: по СОВПАДЕНИЮ ТЕКСТА весь ком выглядит
        «рождённым на последней встрече». Если этот тест позеленеет наоборот
        (пересечение появится), значит фикстуру упростили до дословных повторов —
        и главный тест выше снова станет слепым, как был."""
        with tempfile.TemporaryDirectory() as td:
            fx = self._fixture(td)
            digests = sm.list_series_digests(fx.dir)
            origins = sm.origin_dates_for_tasks(digests)
            tail = sm._resolve_open_tasks_raw(digests)
        self.assertEqual(len(tail), 3)
        # все три «родились» 15-го по текстовому ключу — то есть отбор по origin
        # не отсёк бы НИЧЕГО (ровно то, что померили на боевых сериях)
        self.assertEqual({origins.get(sm._status_key(t)) for t in tail}, {"2026-07-15"})

    def test_same_fixture_in_carry_shows_the_pile(self):
        # Контроль: та же серия в накоплении показывает все три — значит отличие
        # даёт именно режим, а не фикстура.
        with tempfile.TemporaryDirectory() as td:
            fx = self._fixture(td)
            items = md.collect_items(fx.dir, mode=CARRY)
        self.assertEqual(len(items["open"]), 3)

    def test_legacy_digest_without_marker_falls_back_to_protocol(self):
        # Выжимки, лежащие на диске СЕЙЧАС, пометки происхождения не несут. Пока
        # серия не пройдёт одну встречу, источник — протокол рядом с выжимкой.
        with tempfile.TemporaryDirectory() as td:
            fx = self._fixture(td)
            for p in fx.dir.glob("*-memory.json"):
                data = json.loads(p.read_text(encoding="utf-8"))
                data.pop(sm.OPEN_TASKS_BORN_HERE_KEY, None)
                p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            items = md.collect_items(fx.dir, mode=LAST_ONLY)
        self.assertEqual(items["open"], ["Илья: свежий вопрос последней встречи"])

    def test_no_provenance_at_all_shows_nothing_rather_than_the_pile(self):
        # Ни пометки, ни протокола: происхождение неизвестно. Показать ком нельзя
        # ни при каких обстоятельствах — это ровно тот исход, от которого уходим.
        with tempfile.TemporaryDirectory() as td:
            fx = _Series(Path(td))
            fx.write("2026-07-01", ["Илья: старый вопрос"])
            fx.write("2026-07-15", ["Илья: старый вопрос", "Илья: свежий вопрос"])
            items = md.collect_items(fx.dir, mode=LAST_ONLY)
        self.assertEqual(items["open"], [])

    def test_reissued_protocol_wins_over_stale_digest(self):
        # Боевой случай (04.08.2026): протокол перевыпущен ПОСЛЕ записи выжимки,
        # формулировки разошлись полностью. Людям ушёл протокол — его и показываем,
        # а не 21 пункт накопленного кома из устаревшей выжимки.
        with tempfile.TemporaryDirectory() as td:
            fx = _Series(Path(td))
            fx.write("2026-07-15", [
                "Илья: старая формулировка кома 1", "Илья: старая формулировка кома 2",
                "Илья: старая формулировка кома 3",
            ])
            (fx.dir / "2026-07-15-protokol.md").write_text(
                _protocol_md(["Илья: задача этой встречи, новыми словами"],
                             ["Илья: хвост, тоже новыми словами"]),
                encoding="utf-8")
            items = md.collect_items(fx.dir, mode=LAST_ONLY)
        self.assertEqual(items["open"], ["Илья: задача этой встречи, новыми словами"])

    def test_cap_does_not_eat_last_meeting_tasks(self):
        # Отбор происхождения обязан идти ДО капа: иначе OPEN_TASKS_MAX съели бы
        # слоты старыми пунктами и от прошлой встречи не осталось бы ничего.
        with tempfile.TemporaryDirectory() as td:
            fx = _Series(Path(td))
            old = [f"Илья: старая задача {i}" for i in range(5)]
            fx.meeting("2026-07-01", fresh=old)
            fx.meeting("2026-07-08", carried=[f"{t} (переформулировано)" for t in old],
                       fresh=["Илья: единственная свежая"])
            with mock.patch.object(sm, "open_tasks_max", return_value=3):
                items = md.collect_items(fx.dir, mode=LAST_ONLY)
        self.assertEqual(items["open"], ["Илья: единственная свежая"])

    def test_minor_stays_in_cheatsheet(self):
        # Решение владельца 29.07: мелочь в шпаргалку ВХОДИТ.
        with tempfile.TemporaryDirectory() as td:
            fx = _Series(Path(td))
            fx.meeting("2026-07-08", fresh=["Илья: свежая значимая"],
                       minor=["Илья: скинуть ссылку"])
            items = md.collect_items(fx.dir, mode=LAST_ONLY)
        self.assertEqual(items["minor"], ["Илья: скинуть ссылку"])
        self.assertEqual(items["open"], ["Илья: свежая значимая"])

    def test_minor_survives_a_series_with_history(self):
        # Мелочь не отбирается по происхождению ВООБЩЕ (она живёт одну встречу и в
        # протокол не переезжает). Фикстура — с накопленным хвостом, иначе тест
        # зелёный на одной встрече и слеп к «мелочь отфильтровали как старьё».
        with tempfile.TemporaryDirectory() as td:
            fx = _Series(Path(td))
            fx.meeting("2026-07-01", fresh=["Илья: давняя задача"],
                       minor=["Илья: скинуть ссылку"])
            fx.meeting("2026-07-08", carried=["Илья: давняя задача, другими словами"],
                       fresh=["Илья: свежая значимая"],
                       minor=["Илья: скинуть ссылку"])
            items = md.collect_items(fx.dir, mode=LAST_ONLY)
        self.assertEqual(items["minor"], ["Илья: скинуть ссылку"])
        self.assertEqual(items["open"], ["Илья: свежая значимая"])

    def test_header_is_owner_draft_verbatim(self):
        out = md.build_digest_markdown("koordinatsiya", "2026-07-15",
                                       ["Илья: расчёт"], [], mode=LAST_ONLY)
        self.assertIn(
            "Для удобства — задачи и вопросы, которые зафиксировали на прошлой встрече. "
            "Отвечать не нужно, это шпаргалка к сегодняшней встрече.", out)

    def test_pdf_cover_is_soft_too(self):
        # Обложка PDF — первое, что человек видит, открыв документ. «Сверка перед
        # встречей» над мягкой шапкой = рамка отчётности поверх справки.
        self.assertEqual(md.digest_subtitle("2026-07-15", LAST_ONLY),
                         "Справка к встрече · 2026-07-15")
        self.assertNotIn("Сверка", md.digest_subtitle("2026-07-15", LAST_ONLY))

    def test_no_status_request_anywhere(self):
        # В данных ОБЯЗАН быть и безымянный пункт, и пункт с ответственным: секции
        # рендерятся разными ветками, и нумерация могла бы вернуться в любую из них.
        out = md.build_digest_markdown(
            "koordinatsiya", "2026-07-15",
            ["разобрать почту", "Илья: расчёт", "Татьяна: договор"],
            ["Илья: ссылка"], origins=_golden_origins(), mode=LAST_ONLY)
        self.assertNotIn(md.HEADER_INSTRUCTION, out)          # «напишите ответом… статус»
        self.assertNotIn(sm.STATUS_OPEN_LABEL, out)           # «Статус неизвестен — уточнить»
        self.assertNotIn("1. ", out)                          # нумерации под ответ нет
        self.assertNotIn("2. ", out)
        self.assertNotIn("3. ", out)
        self.assertNotIn("статус не нужен", out)              # и намёка на статусы у мелочи
        self.assertIn("- разобрать почту", out)               # безымянный — обычным буллетом
        self.assertIn("- расчёт", out)                        # и с ответственным тоже
        self.assertIn("- Илья: ссылка", out)

    def test_owner_grouping_survives(self):
        out = md.build_digest_markdown(
            "koordinatsiya", "2026-07-15",
            ["Илья: расчёт", "Татьяна: договор", "разобрать почту"], [], mode=LAST_ONLY)
        self.assertIn("## Илья", out)
        self.assertIn("## Татьяна", out)
        self.assertIn("## Без ответственного / неразобрано", out)

    def test_no_origin_prefix_in_cheatsheet(self):
        # Все пункты с ОДНОЙ прошлой встречи — «от 01.07.2026» читалось бы как «висит с…».
        origins = {sm._status_key("Илья: расчёт"): "2026-07-01"}
        out = md.build_digest_markdown("koordinatsiya", "2026-07-15", ["Илья: расчёт"], [],
                                       origins=origins, mode=LAST_ONLY)
        self.assertNotIn("от 01.07.2026", out)

    def test_caption_is_soft(self):
        cap = md.build_caption("koordinatsiya", "2026-07-15", ["Илья: расчёт"], [],
                               mode=LAST_ONLY)
        self.assertIn(md.LAST_ONLY_HEADER, cap)
        self.assertNotIn(md.HEADER_INSTRUCTION, cap)

    def test_broken_date_on_freshest_digest_does_not_slide_back(self):
        # Битая дата у САМОЙ СВЕЖЕЙ выжимки не должна молча превращать справку в
        # справку по позапрошлой встрече — человек читает список, которого вчера
        # не было, и не видит того, что было.
        self.assertIsNone(sm.latest_digest_date(
            [{"date": "2026-07-08"}, {"date": 20260715}]))
        self.assertEqual(sm.latest_digest_date(
            [{"date": "2026-07-08"}, {"date": "2026-07-15"}]), "2026-07-15")


# ==========================================================================
# 3. Слепок нумерации: нет в шпаргалке, возвращается при возврате в carry
# ==========================================================================
class TestSnapshotGate(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.addCleanup(self.tmpdir.cleanup)
        self.fx = _Series(self.tmp)
        self.fx.meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
        self.sent = []

    def _sender(self, chat_id, md_text, caption, date):
        self.sent.append({"md": md_text, "caption": caption})
        return 4242

    def _send(self, mode, date="2026-07-15"):
        return md.send_series_digest(
            self.fx.series, date=date, root=self.tmp, mode=mode,
            chat_resolver=lambda _s: -100500, sender=self._sender)

    def test_last_only_records_no_snapshot(self):
        res = self._send(LAST_ONLY)
        self.assertEqual(res["status"], "sent", res)
        self.assertIsNone(sm.open_tasks_snapshot_for_message(self.fx.dir, 4242))

    def test_carry_records_snapshot(self):
        res = self._send(CARRY)
        self.assertEqual(res["status"], "sent", res)
        self.assertEqual(sm.open_tasks_snapshot_for_message(self.fx.dir, 4242),
                         ["Илья: подготовить расчёт"])

    def test_return_to_carry_registers_snapshot_on_first_digest(self):
        # Память [[snapshot-keyed-by-first-send-copies-lack-it]]: без слепка ответы
        # по номерам не резолвятся, значит ПЕРВЫЙ же carry-дайджест обязан его писать.
        self._send(LAST_ONLY, date="2026-07-15")
        self.assertIsNone(sm.open_tasks_snapshot_for_message(self.fx.dir, 4242))
        self._send(CARRY, date="2026-07-22")
        self.assertEqual(sm.open_tasks_snapshot_for_message(self.fx.dir, 4242),
                         ["Илья: подготовить расчёт"])

    def test_sent_document_remembers_its_mode(self):
        # Нужно, чтобы ответ на документ, который САМ просил статус, получил
        # человеческое объяснение, а не «я тут вопросы не веду».
        self._send(CARRY)
        self.assertEqual(md.digest_mode_for_message(self.fx.dir, -100500, 4242), CARRY)

    def test_mode_read_from_registry_when_not_injected(self):
        # Боевой путь: режим берётся из реестра, а не из параметра.
        with _registry(_rec("koordinatsiya", mode="last-only")):
            res = md.send_series_digest(
                self.fx.series, date="2026-07-15", root=self.tmp,
                chat_resolver=lambda _s: -100500, sender=self._sender)
        self.assertEqual(res["mode"], LAST_ONLY)
        self.assertNotIn(md.HEADER_INSTRUCTION, self.sent[0]["md"])


# ==========================================================================
# 4. Гейт хвоста в ОБОИХ call-site протокола + невозврат хвоста в выжимку
# ==========================================================================
class _Loose:
    """Заглушка «любой объект»: гейт call-site трогает соседние переменные
    (логгер, meta, пути), а нас интересует ровно одна ветка."""

    def __getattr__(self, name):
        return _Loose()

    def __call__(self, *a, **k):
        return _Loose()

    def __truediv__(self, other):
        return _Loose()

    def __getitem__(self, item):
        return _Loose()

    def __iter__(self):
        return iter(())

    def get(self, *a, **k):
        return "koordinatsiya"


class _LooseNS(dict):
    def __missing__(self, key):
        return _Loose()


def _contains_call(node, func_name: str) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == func_name:
                return True
    return False


def _tail_gate_statements(src: str):
    """Найти в исходнике блок «резолв режима + `if` вокруг построения блока хвоста».

    Возвращает список statement-узлов, который можно ИСПОЛНИТЬ. Если вызов
    `build_open_tasks_block` не стоит внутри `if` — вернём None: это и есть
    «гейта нет», сторож обязан покраснеть."""
    tree = ast.parse(src)
    candidates = []  # (размер поддерева, body, индекс, узел)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for j, stmt in enumerate(body):
            if isinstance(stmt, ast.If) and _contains_call(stmt, "build_open_tasks_block"):
                candidates.append((len(list(ast.walk(stmt))), id(stmt), body, j, stmt))
    # БЛИЖАЙШИЙ `if` вокруг вызова = самое маленькое поддерево. Внешние условия
    # («серия резолвится», «трекинг включён») к режиму отношения не имеют.
    if not candidates:
        return None
    _, _, body, j, stmt = sorted(candidates)[0]
    if not isinstance(stmt.test, ast.Name):
        return [stmt]  # условие прямо в `if` (в т.ч. мутант `if True:`)
    # `if open_questions_carry:` — тянем присвоение этой переменной, иначе
    # исполнять нечего (и мутация в самом присвоении осталась бы невидимой).
    for k in range(j - 1, -1, -1):
        prev = body[k]
        if isinstance(prev, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == stmt.test.id
                for t in prev.targets):
            return [prev, stmt]
    raise AssertionError(
        f"условие `{stmt.test.id}` присваивается не рядом с гейтом — "
        f"сторож не может его исполнить, проверьте call-site вручную")


def _run_tail_gate(rel: str, *, carry: bool) -> str:
    """ИСПОЛНИТЬ настоящий гейт из исходника call-site и вернуть то, что он
    положил в `open_tasks_block`."""
    stmts = _tail_gate_statements((_NOTARY / rel).read_text(encoding="utf-8"))
    if stmts is None:
        raise AssertionError(f"{rel}: построение блока хвоста не под `if` — гейта нет")
    mod = ast.Module(body=stmts, type_ignores=[])
    ast.fix_missing_locations(mod)

    class _SM(_Loose):
        def carries_open_questions(self, *a, **k):
            return carry

        def build_open_tasks_block(self, *a, **k):
            return "<ХВОСТ>"

    ns = _LooseNS(series_memory=_SM(), open_tasks_block="")
    exec(compile(mod, f"<gate:{rel}>", "exec"), ns)  # noqa: S102 — исполняем свой же код
    return ns.get("open_tasks_block")


class TestProtocolTailGate(unittest.TestCase):

    CALL_SITES = ("finalize-meeting.py", "lib/clarify_worker.py")

    def test_both_call_sites_build_the_tail_in_carry(self):
        for rel in self.CALL_SITES:
            with self.subTest(rel=rel):
                self.assertEqual(_run_tail_gate(rel, carry=True), "<ХВОСТ>",
                                 f"{rel}: в накоплении раздел обязан строиться")

    def test_both_call_sites_skip_the_tail_in_last_only(self):
        """Память [[review-checks-two-call-sites]] (ISS-10): фикс в одном call-site
        означает, что поздняя пересборка протокола вернёт хвост в шпаргалку.

        Сторож ИСПОЛНЯЕТ гейт, а не ищет подстроку: `carries_open_questions(...) or
        True` и `if True:` подстрочную проверку проходили насквозь."""
        for rel in self.CALL_SITES:
            with self.subTest(rel=rel):
                self.assertEqual(_run_tail_gate(rel, carry=False), "",
                                 f"{rel}: в шпаргалке раздел «с прошлых встреч» "
                                 f"строиться не должен")

    def test_next_digest_does_not_inherit_tail(self):
        """Протокол БЕЗ раздела «с прошлых встреч» → следующая выжимка хвост не тянет.

        Это и есть механизм накопления: раздел парсится обратно `extract_open_tasks`.
        Нет раздела — нечего наследовать, ком не растёт (и обнуляется сам собой).
        """
        proto_with_tail = _protocol_md(["Илья: новая задача этой встречи"],
                                       ["Илья: старый вопрос про маржу"])
        proto_without_tail = _protocol_md(["Илья: новая задача этой встречи"])
        meta = {"series": "koordinatsiya", "date": "2026-07-22"}
        carried = sm.build_digest(proto_with_tail, meta)["open_tasks"]
        clean = sm.build_digest(proto_without_tail, meta)["open_tasks"]
        joined_carried = " | ".join(sm._open_task_text(t) for t in carried)
        joined_clean = " | ".join(sm._open_task_text(t) for t in clean)
        self.assertIn("маржу", joined_carried)      # накопление работает как раньше
        self.assertNotIn("маржу", joined_clean)     # шпаргалка хвост не наследует
        self.assertIn("новая задача", joined_clean)  # свои задачи встречи на месте

    def test_digest_marks_what_was_born_here(self):
        # Пометка происхождения пишется рядом с выжимкой — единственный момент,
        # когда границу двух разделов ещё видно.
        proto = _protocol_md(["Илья: новая задача этой встречи"],
                             ["Илья: старый вопрос про маржу"])
        d = sm.build_digest(proto, {"series": "koordinatsiya", "date": "2026-07-22"})
        self.assertEqual(d[sm.OPEN_TASKS_BORN_HERE_KEY],
                         ["Илья: новая задача этой встречи"])
        self.assertEqual(len(d["open_tasks"]), 2)

    def test_born_here_survives_the_storage_cap(self):
        # Боевой Директорат: 21 хвостовой пункт + 13 своих. Хранилище режет список на
        # 25 и режет С КОНЦА — то есть первыми выпадают как раз задачи ЭТОЙ встречи.
        # Пометка происхождения обязана их сохранить, иначе справка про прошлую
        # встречу теряет девять её же пунктов.
        proto = _protocol_md([f"Илья: задача этой встречи {i}" for i in range(13)],
                             [f"Илья: хвостовой вопрос {i}" for i in range(21)])
        d = sm.build_digest(proto, {"series": "koordinatsiya", "date": "2026-07-22"})
        self.assertEqual(len(d["open_tasks"]), 25)          # хранилище подрезало
        self.assertEqual(len(d[sm.OPEN_TASKS_BORN_HERE_KEY]), 13)  # а происхождение — нет

    def test_born_here_written_even_when_empty(self):
        # Пустой список ≠ отсутствие ключа: по отсутствию читатель узнаёт СТАРЫЙ
        # формат выжимки и включает фолбэк по протоколу.
        proto = _protocol_md([], ["Илья: только хвост"])
        d = sm.build_digest(proto, {"series": "koordinatsiya", "date": "2026-07-22"})
        self.assertIn(sm.OPEN_TASKS_BORN_HERE_KEY, d)
        self.assertEqual(d[sm.OPEN_TASKS_BORN_HERE_KEY], [])

    def test_finalize_skips_onboarding_commit_in_last_only(self):
        """РИСК3: `commit_pending_shown` при пустом sink всё равно ставит маркер
        онбординга. В шпаргалке хвост не показывали — маркера быть не должно,
        иначе при возврате в carry холодный старт R4 бесшумно пропадёт."""
        src = (_NOTARY / "finalize-meeting.py").read_text(encoding="utf-8")
        i = src.find("series_memory.commit_pending_shown(")
        self.assertNotEqual(i, -1)
        head = src[max(0, i - 800):i]
        self.assertIn("open_questions_carry", head,
                      "commit_pending_shown зовётся без гейта режима")

    def test_commit_pending_shown_with_empty_sink_is_safe(self):
        # Прямая проверка поведения, на которое ссылается гейт: пустой sink +
        # carry-серия → онбординг ставится (прежнее поведение сохранено).
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koordinatsiya"
            sd.mkdir()
            self.assertFalse(sm.is_open_tasks_onboarded(sd))
            sm.commit_pending_shown(sd, [], date="2026-07-15")
            self.assertTrue(sm.is_open_tasks_onboarded(sd))


# ==========================================================================
# 5. Тишина статус-машины: сверщик, стор, reply-роуты
# ==========================================================================
class TestStatusMachineSilence(unittest.TestCase):

    def test_status_store_write_blocked_in_last_only(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koordinatsiya"
            sd.mkdir()
            with _registry(_rec("koordinatsiya", mode="last-only")):
                rec = sm.set_task_status(sd, "Илья: расчёт", sm.STATUS_DONE,
                                         reason="по ответу", source="reply",
                                         date="2026-07-15")
            self.assertIsNone(rec)
            self.assertFalse(sm.task_status_path(sd).exists(), "стор статусов создан")

    def test_status_store_write_allowed_in_carry(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koordinatsiya"
            sd.mkdir()
            with _registry(_rec("koordinatsiya", mode="carry")):
                rec = sm.set_task_status(sd, "Илья: расчёт", sm.STATUS_DONE,
                                         reason="по ответу", source="reply",
                                         date="2026-07-15")
            self.assertIsNotNone(rec)
            self.assertTrue(sm.task_status_path(sd).exists())

    def test_batch_of_statuses_does_not_reread_registry_each_time(self):
        # Пачка статусов одним ответом (8–20 задач) не должна разбирать YAML на
        # каждую запись — это же обнуляло префетч ночного сверщика.
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koordinatsiya"
            sd.mkdir()
            watched = _watched(_rec("koordinatsiya", mode="carry"))
            with mock.patch.object(sm, "load_watched_for_modes",
                                   return_value=watched) as lw:
                for i in range(3):
                    sm.set_task_status(sd, f"Илья: задача {i}", sm.STATUS_DONE,
                                       reason="по ответу", source="reply",
                                       date="2026-07-15", watched=watched)
            lw.assert_not_called()

    def test_reconciler_skips_last_only_series(self):
        matched_items = []

        def _matcher(items, evidence):
            matched_items.extend(items)
            return [pr.VERDICT_CLOSE] * len(items)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("koordinatsiya", "anzhee-direktorat"):
                _Series(root, name).meeting("2026-07-14",
                                            fresh=[f"Илья: висяк серии {name}"])
            # Источник свидетельств для кросс-серийной сверки (иначе сводить нечем).
            _Series(root, "series-evidence").write(
                "2026-07-14", [],
                key_points=["висяк серии anzhee-direktorat закрыт и принят",
                            "висяк серии koordinatsiya закрыт и принят"],
                themes=["Висяки"])
            with _registry(_rec("koordinatsiya", mode="last-only"),
                           _rec("anzhee-direktorat", mode="carry"),
                           _rec("series-evidence", mode="carry")):
                results = pr.reconcile_all(root, matcher=_matcher, date="2026-07-15")
            by_series = {r.series: r for r in results}
            # Висяк шпаргалочной серии до матчера даже не доехал.
            self.assertNotIn("Илья: висяк серии koordinatsiya", matched_items)
            self.assertTrue(by_series["koordinatsiya"].skipped)
            self.assertEqual(by_series["koordinatsiya"].mode, LAST_ONLY)
            self.assertEqual(by_series["koordinatsiya"].closed, 0)
            # sidecar шпаргалочной серии не создан вовсе
            self.assertFalse((root / "koordinatsiya" / "task-status.json").exists())
            # а серию-накопление сверщик отработал как раньше
            self.assertEqual(by_series["anzhee-direktorat"].closed, 1)

    def test_doubt_buffer_keeps_ticking_in_last_only(self):
        # Буфер «под сомнением» — с ЧАСАМИ (TTL). Отсечь серию до пруна значит
        # остановить их: при возврате в накопление всё протухшее оживёт разом.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fx = _Series(root)
            fx.meeting("2026-07-01", fresh=["Илья: спорная задача"])
            with _registry(_rec("koordinatsiya", mode="carry")):
                sm.set_task_status(fx.dir, "Илья: спорная задача", sm.STATUS_DOUBT,
                                   reason="по встрече", source="reconciler",
                                   date="2026-06-01")
            with _registry(_rec("koordinatsiya", mode="last-only")):
                results = pr.reconcile_all(root, matcher=lambda i, e: None,
                                           date="2026-07-15", doubt_ttl=3)
            res = {r.series: r for r in results}["koordinatsiya"]
            self.assertTrue(res.skipped)
            self.assertEqual(res.doubt_pruned, 1, "буфер doubt заморожен режимом")

    def test_digest_reply_with_status_answers_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fx = _Series(root)
            fx.meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
            msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 10,
                   "date": 1718000000, "text": "расчёт — сделано",
                   "reply_to_message": {"message_id": 900}}
            with mock.patch.object(ml, "_protokol_root", return_value=root), \
                 mock.patch.object(ml, "_reply_context_series_digest",
                                   return_value="koordinatsiya"), \
                 mock.patch.object(ml, "send_message", return_value=1) as send, \
                 _registry(_rec("koordinatsiya", mode="last-only")):
                handled = ml.maybe_route_to_digest_reply("tok", -100500, msg)
            self.assertTrue(handled)
            self.assertIn("вопросы не веду", send.call_args[0][2])
            self.assertFalse((root / "koordinatsiya" / "task-status.json").exists())

    def test_digest_reply_after_mode_switch_explains_itself(self):
        # Документ ушёл со статусами, серию переключили после. Человек выполнил
        # ровно то, о чём его попросили, — отвечать «я тут вопросы не веду» нельзя.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fx = _Series(root)
            fx.meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
            md.record_sent(fx.dir, "2026-07-15", chat_id=-100500, message_id=900,
                           mode=CARRY)
            msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 10,
                   "date": 1718000000, "text": "расчёт — сделано",
                   "reply_to_message": {"message_id": 900}}
            with mock.patch.object(ml, "_protokol_root", return_value=root), \
                 mock.patch.object(ml, "_reply_context_series_digest",
                                   return_value="koordinatsiya"), \
                 mock.patch.object(ml, "send_message", return_value=1) as send, \
                 _registry(_rec("koordinatsiya", mode="last-only")):
                ml.maybe_route_to_digest_reply("tok", -100500, msg)
            self.assertIn("перевели в справочный режим", send.call_args[0][2])
            self.assertFalse((root / "koordinatsiya" / "task-status.json").exists())

    def test_digest_reply_without_status_marker_stays_silent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _Series(root).meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
            msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 10,
                   "date": 1718000000, "text": "спасибо, посмотрю",
                   "reply_to_message": {"message_id": 900}}
            with mock.patch.object(ml, "_protokol_root", return_value=root), \
                 mock.patch.object(ml, "_reply_context_series_digest",
                                   return_value="koordinatsiya"), \
                 mock.patch.object(ml, "send_message") as send, \
                 _registry(_rec("koordinatsiya", mode="last-only")):
                handled = ml.maybe_route_to_digest_reply("tok", -100500, msg)
            self.assertTrue(handled)
            send.assert_not_called()

    def test_protocol_status_reply_answers_in_last_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _Series(root).meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
            msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 11,
                   "date": 1718000000, "text": "расчёт — сделано",
                   "reply_to_message": {"message_id": 800}}
            with mock.patch.object(ml, "_protokol_root", return_value=root), \
                 mock.patch.object(ml, "_reply_context_series",
                                   return_value="koordinatsiya"), \
                 mock.patch.object(ml, "send_message", return_value=1) as send, \
                 _registry(_rec("koordinatsiya", mode="last-only")):
                handled = ml.maybe_route_to_pending_status_reply("tok", -100500, 1, msg)
            self.assertTrue(handled)
            self.assertIn("вопросы не веду", send.call_args[0][2])
            self.assertFalse((root / "koordinatsiya" / "task-status.json").exists())

    def test_status_plus_edit_in_one_message_keeps_the_edit(self):
        # Класс ISS-33: «расчёт сделан, и в протоколе поправь Иванова на Петрова».
        # Статус в шпаргалке не нужен, а правка нужна — проглотить reply целиком
        # значит потерять её молча (худший режим).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _Series(root).meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
            msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 11,
                   "date": 1718000000,
                   "text": "расчёт сделан, и в протоколе поправь Иванова на Петрова",
                   "reply_to_message": {"message_id": 800}}
            with mock.patch.object(ml, "_protokol_root", return_value=root), \
                 mock.patch.object(ml, "_reply_context_series",
                                   return_value="koordinatsiya"), \
                 mock.patch.object(ml, "send_message", return_value=1) as send, \
                 _registry(_rec("koordinatsiya", mode="last-only")):
                handled = ml.maybe_route_to_pending_status_reply("tok", -100500, 1, msg)
            self.assertFalse(handled, "reply съеден целиком — правка потеряна")
            self.assertIn("вопросы не веду", send.call_args[0][2])

    def test_protocol_status_reply_still_works_in_carry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _Series(root).meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
            msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 11,
                   "date": 1718000000, "text": "расчёт — сделано",
                   "reply_to_message": {"message_id": 800}}
            with mock.patch.object(ml, "_protokol_root", return_value=root), \
                 mock.patch.object(ml, "_reply_context_series",
                                   return_value="koordinatsiya"), \
                 mock.patch.object(ml, "send_message", return_value=1) as send, \
                 _registry(_rec("koordinatsiya", mode="carry")):
                handled = ml.maybe_route_to_pending_status_reply("tok", -100500, 1, msg)
            self.assertTrue(handled)
            self.assertNotIn("вопросы не веду", send.call_args[0][2])
            self.assertTrue((root / "koordinatsiya" / "task-status.json").exists())

    def test_mixed_route_does_not_call_splitter_in_last_only(self):
        # Платный LLM-сплит ради статусов, которые всё равно не применим, не зовём.
        msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 12,
               "date": 1718000000, "text": "расчёт сделан, договор жду",
               "reply_to_message": {"message_id": 800}}
        from notary.lib import mixed_reply_split
        with mock.patch.object(ml, "_reply_context_series", return_value="koordinatsiya"), \
             mock.patch.object(mixed_reply_split, "is_enabled", return_value=True), \
             mock.patch.object(mixed_reply_split, "split_reply") as split, \
             _registry(_rec("koordinatsiya", mode="last-only")):
            handled = ml.maybe_route_to_mixed_reply("tok", -100500, 1, msg)
        self.assertFalse(handled)
        split.assert_not_called()


# ==========================================================================
# 5б. Роуты, у которых гейта режима не было вовсе (ack-роут и ИИ-разбор)
# ==========================================================================
class TestRoutesWithoutGate(unittest.TestCase):
    """Оба роута стоят ВЫШЕ детерминированного статус-роута и до этого фикса
    отвечали в шпаргалочной серии своим языком: «Не нашёл, что откатить —
    уточните» и «Не разобрал, про какую задачу речь»."""

    def _ack_msg(self, text):
        return {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 21,
                "date": 1718000000, "text": text,
                "reply_to_message": {"message_id": 700}}

    def _run_ack(self, text, mode, root):
        from notary.lib import reply_ack_state, reply_understand
        state = {"chat_id": -100500, "series_dir": str(root / "koordinatsiya"),
                 "question": None, "replier": "Илья"}
        with mock.patch.object(ml, "_protokol_root", return_value=root), \
             mock.patch.object(reply_understand, "is_enabled", return_value=True), \
             mock.patch.object(reply_ack_state, "get", return_value=state), \
             mock.patch.object(ml, "send_message", return_value=1) as send, \
             mock.patch.object(ml, "_understand_and_ack") as understand, \
             _registry(_rec("koordinatsiya", mode=mode)):
            handled = ml.maybe_route_to_ack_reply("tok", -100500, self._ack_msg(text), 1)
        return handled, send, understand

    def test_ack_route_answers_reference_phrase_in_last_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _Series(root).meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
            handled, send, understand = self._run_ack("верни расчёт", LAST_ONLY, root)
        self.assertTrue(handled)
        self.assertIn("вопросы не веду", send.call_args[0][2])
        understand.assert_not_called()

    def test_ack_route_untouched_in_carry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _Series(root).meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
            handled, send, _ = self._run_ack("верни расчёт", CARRY, root)
        if send.call_args is not None:
            self.assertNotIn("вопросы не веду", send.call_args[0][2])

    def test_understand_route_gates_imperative_status_talk(self):
        # «закрой первую» статус-маркера не несёт: узкий гейт пропускал такую фразу
        # в ИИ-разбор, и человек получал «Не разобрал, про какую задачу речь».
        from notary.lib import reply_understand
        msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 22,
               "date": 1718000000, "text": "закрой первую",
               "reply_to_message": {"message_id": 800}}
        with mock.patch.object(reply_understand, "is_enabled", return_value=True), \
             mock.patch.object(ml, "_reply_context_series", return_value="koordinatsiya"), \
             mock.patch.object(ml, "_understand_and_ack") as understand, \
             _registry(_rec("koordinatsiya", mode="last-only")):
            handled = ml.maybe_route_to_reply_understand("tok", -100500, 1, msg)
        self.assertFalse(handled, "статус-разговор ушёл в платный ИИ-разбор")
        understand.assert_not_called()

    def test_understand_route_still_takes_edits_in_last_only(self):
        # Правки текста от режима НЕ зависят: они обязаны идти через understand,
        # иначе «поправьте фамилию» получит «я тут вопросы не веду».
        from notary.lib import reply_understand
        msg = {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 23,
               "date": 1718000000,
               "text": "в протоколе вместо Иванова правильно Петров, поправьте",
               "reply_to_message": {"message_id": 800}}
        with mock.patch.object(reply_understand, "is_enabled", return_value=True), \
             mock.patch.object(ml, "_reply_context_series", return_value="koordinatsiya"), \
             mock.patch.object(ml, "_understand_and_ack", return_value=True) as understand, \
             mock.patch.object(ml, "_reply_open_tasks_snapshot", return_value=None), \
             mock.patch.object(ml, "_protokol_root", return_value=Path("/tmp")), \
             _registry(_rec("koordinatsiya", mode="last-only")):
            handled = ml.maybe_route_to_reply_understand("tok", -100500, 1, msg)
        self.assertTrue(handled)
        understand.assert_called_once()


# ==========================================================================
# 6. Fail-safe: не знаю режим → веду себя как вчера (carry)
# ==========================================================================
class TestModeFailSafe(unittest.TestCase):

    def _broken_registry(self, text: str):
        """Настоящий битый файл реестра под настоящим `load_watched`."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "watched.yaml"
        p.write_text(text, encoding="utf-8")
        return mock.patch.object(reg, "WATCHED_FILE", p)

    def test_yaml_top_level_not_a_dict_falls_back_to_carry(self):
        # `cli/registry._read_yaml` кидает `SystemExit` (BaseException!) — обычный
        # исход ручной правки, к которой план прямо приглашает. Раньше это роняло
        # дайджест, сверщик, финализатор и слушателя (последнего — в цикл падений).
        with self._broken_registry("- просто список\n- а не словарь\n"):
            self.assertEqual(sm.open_questions_mode("koordinatsiya"), CARRY)
            self.assertTrue(sm.carries_open_questions("koordinatsiya"))
            self.assertIsNone(sm.load_watched_for_modes())

    def test_unparsable_yaml_falls_back_to_carry(self):
        with self._broken_registry("watched: [ {oops\n"):
            self.assertEqual(sm.open_questions_mode("koordinatsiya"), CARRY)

    def test_status_writer_survives_broken_registry(self):
        # Писатель статусов резолвит режим на КАЖДУЮ запись — он падал тем же
        # SystemExit'ом и уносил с собой обработку ответа.
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "koordinatsiya"
            sd.mkdir()
            with self._broken_registry("[]\n"):
                rec = sm.set_task_status(sd, "Илья: расчёт", sm.STATUS_DONE,
                                         reason="по ответу", source="reply",
                                         date="2026-07-15")
            self.assertIsNotNone(rec, "запись статуса не пережила битый реестр")

    def test_reconciler_survives_broken_registry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _Series(root).meeting("2026-07-14", fresh=["Илья: висяк"])
            with self._broken_registry("строка вместо словаря\n"):
                results = pr.reconcile_all(root, matcher=lambda i, e: None,
                                           date="2026-07-15")
            self.assertEqual([r.series for r in results], ["koordinatsiya"])

    def test_unreadable_registry_falls_back_to_carry(self):
        with mock.patch.object(sm, "load_watched_for_modes", side_effect=OSError("нет файла")):
            self.assertEqual(sm.open_questions_mode("koordinatsiya"), CARRY)

    def test_systemexit_from_loader_falls_back_to_carry(self):
        with mock.patch.object(sm, "load_watched_for_modes", side_effect=SystemExit("битый yaml")):
            self.assertEqual(sm.open_questions_mode("koordinatsiya"), CARRY)

    def test_series_absent_from_registry_falls_back_to_carry(self):
        with _registry(_rec("anzhee-direktorat", mode="carry")):
            self.assertEqual(sm.open_questions_mode("koordinatsiya"), CARRY)

    def test_undefined_mode_is_distinguishable_in_the_journal(self):
        # «Не знаю режим — веду себя как вчера» и «владелец выбрал накопление» дают
        # ОДИН И ТОТ ЖЕ `mode=carry` в итоговой строке. Отличить их можно только по
        # журналу, иначе серия годами едет на фолбэке и это выглядит как норма.
        with _registry(_rec("anzhee-direktorat", mode="carry")):
            with self.assertLogs("meeting_notary.series_memory", level="WARNING") as logs:
                sm.open_questions_mode("koordinatsiya")
        self.assertTrue(any("РЕЖИМ НЕ ОПРЕДЕЛЁН" in m for m in logs.output), logs.output)
        # А честное накопление в журнал ничего тревожного не пишет.
        with _registry(_rec("koordinatsiya", mode="carry")):
            with mock.patch.object(sm.logger, "warning") as warn:
                sm.open_questions_mode("koordinatsiya")
            warn.assert_not_called()

    def test_registry_field_absent_gives_last_only(self):
        # Решение владельца 29.07: у ЗАПИСИ реестра без поля дефолт — справка.
        with _registry(_rec("koordinatsiya")):
            self.assertEqual(sm.open_questions_mode("koordinatsiya"), LAST_ONLY)

    def test_typo_in_mode_value_keeps_carry(self):
        # Поле ЗАДАНО, но не читается. Потеря хвоста НЕОБРАТИМА (он живёт только в
        # разделе протокола), лишний хвост — нет. Ошибаемся в сторону накопления.
        for junk in ("карри", "last only", 123, True, ["carry"], {"m": "carry"}, ""):
            with self.subTest(junk=junk):
                with _registry(_rec("koordinatsiya", mode=junk)):
                    self.assertEqual(sm.open_questions_mode("koordinatsiya"), CARRY)

    def test_dead_record_asking_carry_wins_over_live_record_without_field(self):
        # Запись серии явно просит накопление — молча уводить серию в справку нельзя.
        with _registry(_rec("koordinatsiya", mode="carry", enabled=False),
                       _rec("koordinatsiya")):
            self.assertEqual(sm.open_questions_mode("koordinatsiya"), CARRY)

    def test_bot_answers_the_mode_it_actually_behaves_in(self):
        # «В каком режиме эта серия?» при нечитаемом значении обязано отвечать
        # «накопление» — именно так бот себя и ведёт. Ответ «шпаргалка» был бы
        # враньём про собственное поведение.
        watched = _watched(_rec("koord", telegram_chat_id=-100500,
                                open_questions_mode="карри"))
        with mock.patch.object(ml, "_protokol_root", return_value=Path("/tmp")), \
             mock.patch.object(ml, "send_message", return_value=1) as send, \
             mock.patch.object(reg, "load_watched", return_value=watched):
            ml.maybe_route_to_mode_command(
                "tok", -100500, 1,
                {"chat": {"id": -100500}, "from": {"id": 1}, "message_id": 5,
                 "date": 1718000000, "text": "в каком режиме эта серия?"})
        said = " ".join(str(c[0][2]) for c in send.call_args_list)
        self.assertIn("накопления", said, said)

    def test_all_records_last_only_stays_last_only(self):
        # Контроль: честная шпаргалка не должна «чиниться» обратно в накопление.
        with _registry(_rec("koordinatsiya", mode="last-only", enabled=False),
                       _rec("koordinatsiya", mode="last-only")):
            self.assertEqual(sm.open_questions_mode("koordinatsiya"), LAST_ONLY)

    def test_carry_series_reads_as_carry(self):
        with _registry(_rec("anzhee-direktorat", mode="carry")):
            self.assertTrue(sm.carries_open_questions("anzhee-direktorat"))

    def test_injected_watched_is_not_reread(self):
        with mock.patch.object(sm, "load_watched_for_modes") as lw:
            mode = sm.open_questions_mode("koordinatsiya",
                                          watched=_watched(_rec("koordinatsiya", mode="carry")))
        self.assertEqual(mode, CARRY)
        lw.assert_not_called()


# ==========================================================================
# 7. ISS-55 (находка Ф4): время встречи — из MEETING_NOTARY_DISPLAY_TZ
# ==========================================================================
class TestDisplayTimezone(unittest.TestCase):

    def _md(self, tz_value):
        ctx = _clean_env() if tz_value is None else mock.patch.dict(
            "os.environ", {md.DISPLAY_TZ_ENV: tz_value})
        with ctx:
            return md.build_digest_markdown(
                "koordinatsiya", "2026-07-15", ["Илья: расчёт"], [],
                start_at=datetime(2026, 7, 15, 12, 0, tzinfo=MSK))

    def test_unset_variable_keeps_the_historical_msk_line(self):
        # Дайджест печатал «12:00 МСК» с 07.2026. Переменной нет ни в примере
        # конфига, ни в юнитах — значит незаданная обязана давать ровно это.
        self.assertIn("встреча в 12:00 МСК", self._md(None))

    def test_moscow_keeps_msk_label(self):
        self.assertIn("встреча в 12:00 МСК", self._md("Europe/Moscow"))

    def test_dubai_shows_local_time_without_false_msk(self):
        # ISS-55: вечерний блок про эту же встречу говорит «13:00» — утро обязано
        # совпадать. И не называть дубайское время московским.
        out = self._md("Asia/Dubai")
        self.assertIn("встреча в 13:00", out)
        self.assertNotIn("МСК", out)

    def test_garbage_tz_falls_back_to_msk_not_to_someone_elses_zone(self):
        out = self._md("Не/Зона")
        self.assertIn("встреча в 12:00 МСК", out)

    def test_schedule_stays_in_msk(self):
        # Переменная влияет ТОЛЬКО на показ: правило «09:00 МСК, но не позже чем за
        # час до начала» от локали читателя не зависит.
        with mock.patch.dict("os.environ", {md.DISPLAY_TZ_ENV: "Asia/Dubai"}):
            self.assertEqual(md.digest_send_at(datetime(2026, 7, 15, 12, 0, tzinfo=MSK)),
                             datetime(2026, 7, 15, 9, 0, tzinfo=MSK))

    def test_example_config_documents_the_variable(self):
        # Переменной не было ни в примере конфига, ни в юнитах — из-за этого
        # поверхности расходились на час, и никто об этом не знал.
        example = _SCRIPTS.parent / ".env.notary.example"
        self.assertIn(md.DISPLAY_TZ_ENV, example.read_text(encoding="utf-8"))


# ==========================================================================
# 8. ISS-45 (находка Ф1): переотправка не съедает плановый дайджест
# ==========================================================================
class TestForcedResend(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.addCleanup(self.tmpdir.cleanup)
        self.fx = _Series(self.tmp)
        self.fx.meeting("2026-07-14", fresh=["Илья: подготовить расчёт"])
        self.mids = iter([111, 222, 333])

    def _sender(self, chat_id, md_text, caption, date):
        return next(self.mids)

    def _send(self, **kw):
        return md.send_series_digest(
            self.fx.series, date="2026-07-15", root=self.tmp, mode=CARRY,
            chat_resolver=lambda _s: -100500, sender=self._sender, **kw)

    def test_forced_resend_does_not_cancel_planned_digest(self):
        # Участник попросил файл в 08:20 → плановая рассылка в 09:00 обязана уйти.
        self.assertEqual(self._send(force=True)["status"], "sent")
        self.assertFalse(md.already_sent(self.fx.dir, "2026-07-15"))
        self.assertEqual(self._send()["status"], "sent")
        self.assertTrue(md.already_sent(self.fx.dir, "2026-07-15"))

    def test_planned_digest_is_still_idempotent(self):
        # R13 не ослаблен: плановый дайджест за день по-прежнему один.
        self.assertEqual(self._send()["status"], "sent")
        self.assertEqual(self._send()["reason"], "already")

    def test_earlier_message_ids_still_resolve_to_series(self):
        # Ответы-статусы на ПЕРВОЕ сообщение дня должны резолвиться и после переотправки.
        self._send()
        self._send(force=True)
        self.assertEqual(md.digest_series_for_message(-100500, 111, root=self.tmp),
                         "koordinatsiya")
        self.assertEqual(md.digest_series_for_message(-100500, 222, root=self.tmp),
                         "koordinatsiya")

    def test_old_state_format_still_readable(self):
        # На диске лежит запись СТАРОГО формата (без `also`) — читаем как раньше.
        path = md.digest_state_path(self.fx.dir)
        path.write_text(json.dumps({"schema": 1, "sent": {"2026-07-15": {
            "chat_id": -100500, "message_id": 999, "at": "2026-07-15T06:00:00+00:00"}}},
            ensure_ascii=False), encoding="utf-8")
        self.assertTrue(md.already_sent(self.fx.dir, "2026-07-15"))
        self.assertEqual(md.digest_series_for_message(-100500, 999, root=self.tmp),
                         "koordinatsiya")
        self.assertIsNone(md.digest_mode_for_message(self.fx.dir, -100500, 999))


# ==========================================================================
# 9. Т6 (решение владельца 7): возврат в накопление — счёт с ЧИСТОГО ЛИСТА
# ==========================================================================
# 🔴 ШОВ, найденный сквозной проверкой (S1). «Чистый лист» был обещан двумя
# сообщениями бота — подтверждением владельцу («то, что копилось до
# переключения, я не поднимаю») и строкой всему чату серии, — но кодом не
# реализован НИГДЕ: он получался ПОБОЧНО из того, что протокол в шпаргалке
# печатается без раздела хвоста. Стоило переключить в справку и передумать в тот
# же день (встречи между переключениями не было — хвост не перегенерировался), и
# весь до-переключательный ком возвращался целиком: в дайджест, в раздел
# протокола и дальше копиться. Единственный тест про «чистый лист» сверял ТЕКСТ
# подтверждения, поэтому поведение никто не держал.
#
# Тесты ниже держат ПОВЕДЕНИЕ. Даты фикстур считаются от «сегодня» — отсечку
# ставит команда бота по часам, и вмороженные даты сделали бы тест зависимым от
# того, какое сегодня число на машине.
def _days_ago(n: int) -> str:
    return (datetime.now(MSK).date() - timedelta(days=n)).isoformat()


def _today_msk() -> str:
    return datetime.now(MSK).date().isoformat()


class TestCleanSlateOnReturnToCarry(unittest.TestCase):

    OLD_A = "Пётр: согласовать бюджет"
    OLD_B = "Анна: прислать смету"
    FRESH = "Иван: закрыть договор"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.addCleanup(self.tmpdir.cleanup)
        self.fx = _Series(self.tmp)
        # Две встречи ДО переключения: ком копился как обычно.
        self.fx.meeting(_days_ago(14), fresh=[self.OLD_A])
        self.fx.meeting(_days_ago(7), carried=[self.OLD_A], fresh=[self.OLD_B])

    def _switch(self, watched, text, chat=-100500, owner=12345):
        """Переключить режим НАСТОЯЩИМ роутом листенера (он и ставит отсечку)."""
        msg = {"chat": {"id": chat}, "from": {"id": owner}, "message_id": 1,
               "date": 1718000000, "text": text}
        with mock.patch("notary.cli.registry.load_watched", return_value=watched), \
             mock.patch("notary.cli.registry.save_watched"), \
             mock.patch("notary.cli.registry.release_watched_lock"), \
             mock.patch.object(ml, "_series_display_map", return_value={}), \
             mock.patch.object(ml, "send_message", return_value=1):
            claimed = ml.maybe_route_to_mode_command("tok", chat, owner, msg)
        self.assertTrue(claimed, text)
        return watched

    def _open(self, watched):
        with _registry(*watched["watched"]):
            return md.collect_items(self.fx.dir, mode=CARRY)["open"]

    def test_there_and_back_without_a_meeting_starts_from_zero(self):
        # 🔴 ЯДРО Т6. Владелец попробовал шпаргалку и в тот же день передумал.
        # Встречи между переключениями не было — и старый ком обязан НЕ вернуться.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self.assertEqual(sorted(self._open(w)), sorted([self.OLD_A, self.OLD_B]))
        self._switch(w, "режим шпаргалка")
        self._switch(w, "режим накопление")
        self.assertEqual(self._open(w), [],
                         "старые вопросы поднялись после возврата в накопление")

    def test_the_switch_writes_the_cutoff_to_every_record(self):
        # Отсечка — не память процесса, а факт в реестре: переживает рестарт бота
        # и одинаково видна дайджесту, протоколу, сверщику и ответам.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500),
                     _rec(self.fx.series, CARRY, enabled=False))
        self._switch(w, "режим шпаргалка")
        self._switch(w, "режим накопление")
        self.assertEqual(
            [r.get(reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD) for r in w["watched"]],
            [_today_msk(), _today_msk()])

    def test_meeting_after_the_return_accumulates_again(self):
        # «С ближайшей встречи вопросы снова копятся» — отсечка не выключает
        # накопление навсегда, она только не поднимает старое.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self._switch(w, "режим шпаргалка")
        self._switch(w, "режим накопление")
        self.fx.meeting(_today_msk(), fresh=[self.FRESH])
        self.assertEqual(self._open(w), [self.FRESH])

    def test_protocol_tail_is_clean_too(self):
        # Иначе «чистый лист» продержался бы ровно до следующего протокола:
        # раздел хвоста парсится обратно в выжимку (`extract_open_tasks`), и ком
        # въехал бы в накопление снова — уже официально.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self._switch(w, "режим шпаргалка")
        self._switch(w, "режим накопление")
        with _registry(*w["watched"]):
            block = sm.build_open_tasks_block(
                sm.list_series_digests(self.fx.dir), series_dir=self.fx.dir,
                date=_today_msk())
        self.assertNotIn("бюджет", block)
        self.assertNotIn("смету", block)

    def test_night_reconciler_does_not_raise_them_either(self):
        # Сверщик спрашивает по своему универсуму: если бы отсечка стояла только
        # в дайджесте, ночью бот всё равно вернулся бы к тем же вопросам.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self._switch(w, "режим шпаргалка")
        self._switch(w, "режим накопление")
        with _registry(*w["watched"]):
            self.assertEqual(pr._hanging_items(self.fx.dir), [])

    def test_reply_universe_matches_what_is_shown(self):
        # Универсум ответов обязан совпадать с показом: иначе ординал «закрой
        # первую» уехал бы на пункт, которого человек не видел.
        from notary.lib import pending_status_reply as psr  # noqa: PLC0415
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self._switch(w, "режим шпаргалка")
        self._switch(w, "режим накопление")
        with _registry(*w["watched"]):
            universe = psr.load_series_pending(self.fx.dir)
        self.assertNotIn(self.OLD_A, universe)
        self.assertNotIn(self.OLD_B, universe)

    def test_same_question_raised_again_after_the_cutoff_is_shown(self):
        # 🔴 Тонкое место отсечки: пункт, снятый чистым листом, могут поставить
        # заново теми же словами. Считай мы происхождение «первым появлением за
        # всю историю», он не показался бы НИКОГДА — тихая потеря живого вопроса.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self._switch(w, "режим шпаргалка")
        self._switch(w, "режим накопление")
        self.fx.meeting(_today_msk(), fresh=[self.OLD_A])
        self.assertEqual(self._open(w), [self.OLD_A])

    def test_repeat_of_the_same_command_does_not_wipe_the_tail(self):
        # Серия и так в накоплении, владелец повторил команду (или разложил
        # режимы руками при выкатке) — хвост трогать НЕЛЬЗЯ: он живёт только в
        # разделе протокола, и стереть его нечем будет восстановить.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self._switch(w, "режим накопление")
        self.assertIsNone(w["watched"][0].get(reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD))
        self.assertEqual(sorted(self._open(w)), sorted([self.OLD_A, self.OLD_B]))

    def test_healing_a_diverged_registry_does_not_wipe_the_tail(self):
        # 🔴 Опаснее повтора: записи серии разошлись (у второй поля нет), и
        # команда «режим накопление» реестр ЛЕЧИТ — то есть до записи доходит.
        # Формат встреч при этом не меняется, значит отсечку ставить нельзя:
        # иначе ремонтная команда (и ручная раскладка режимов при выкатке)
        # стирала бы накопленный хвост, а восстановить его нечем.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500),
                     _rec(self.fx.series))
        self._switch(w, "режим накопление")
        self.assertEqual([r["open_questions_mode"] for r in w["watched"]],
                         [CARRY, CARRY])   # реестр вылечен
        self.assertEqual([r.get(reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD)
                          for r in w["watched"]], [None, None])
        self.assertEqual(sorted(self._open(w)), sorted([self.OLD_A, self.OLD_B]))

    def test_switch_to_last_only_alone_sets_no_cutoff(self):
        # Отсечку ставит ВОЗВРАТ в накопление, а не уход в справку: иначе серия,
        # которую перевели в шпаргалку и оставили там, потеряла бы хвост молча.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self._switch(w, "режим шпаргалка")
        self.assertIsNone(w["watched"][0].get(reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD))

    def test_no_cutoff_in_registry_keeps_the_whole_tail(self):
        # Фолбэк для боевого реестра, разложенного руками (Ф7): поля нет —
        # поведение ровно прежнее.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500))
        self.assertEqual(sorted(self._open(w)), sorted([self.OLD_A, self.OLD_B]))

    def test_garbage_cutoff_is_ignored(self):
        # Опечатка в дате — не повод молча стереть хвост.
        w = _watched(_rec(self.fx.series, CARRY, telegram_chat_id=-100500,
                          open_questions_carry_since="вчера"))
        self.assertEqual(sorted(self._open(w)), sorted([self.OLD_A, self.OLD_B]))
        self.assertIsNone(reg.series_open_questions_carry_since(self.fx.series, w))

    def test_unreadable_registry_keeps_the_whole_tail(self):
        # Реестр не читается (нет PyYAML, битый YAML) → отсечки не знаем →
        # ведём себя как вчера. Ошибка в сторону лишнего хвоста обратима.
        with mock.patch.object(sm, "load_watched_for_modes", side_effect=SystemExit(1)):
            items = md.collect_items(self.fx.dir, mode=CARRY)
        self.assertEqual(len(items["open"]), 2)

    def test_last_only_series_is_untouched_by_the_cutoff(self):
        # Шпаргалка собирается из раздела «Задачи» протокола и отсечки не знает —
        # проверяем, что мы не сломали её заодно.
        w = _watched(_rec(self.fx.series, LAST_ONLY, telegram_chat_id=-100500,
                          open_questions_carry_since=_today_msk()))
        with _registry(*w["watched"]):
            items = md.collect_items(self.fx.dir, mode=LAST_ONLY)
        self.assertEqual(items["open"], [self.OLD_B])


class TestCarrySinceRegistry(unittest.TestCase):
    """Реестр как носитель отсечки: запись, чтение, валидация."""

    def test_writer_stamps_every_record_of_the_series(self):
        w = _watched(_rec("koord"), _rec("koord", enabled=False), _rec("other"))
        n = reg.set_open_questions_mode_for_series("koord", CARRY, w,
                                                   carry_since="2026-08-05")
        self.assertEqual(n, 2)
        self.assertEqual([r.get(reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD)
                          for r in w["watched"]], ["2026-08-05", "2026-08-05", None])

    def test_latest_cutoff_wins(self):
        # Записи серии могли разойтись (одну добавили позже, реестр правили
        # руками) — последнее решение владельца не имеет права ослабнуть.
        w = _watched(_rec("koord", open_questions_carry_since="2026-07-01"),
                     _rec("koord", open_questions_carry_since="2026-08-05"))
        self.assertEqual(reg.series_open_questions_carry_since("koord", w), "2026-08-05")

    def test_cutoff_without_carry_is_refused(self):
        w = _watched(_rec("koord"))
        with self.assertRaises(ValueError):
            reg.set_open_questions_mode_for_series("koord", LAST_ONLY, w,
                                                   carry_since="2026-08-05")
        with self.assertRaises(ValueError):
            reg.set_open_questions_mode_for_series("koord", CARRY, w, carry_since="05.08")

    def test_validator_catches_a_typo_in_the_field(self):
        rec = {"id": "koord-evt", "series": "koord", "type": "manual",
               "cron": "0 11 * * 2", "tz": "Europe/Moscow",
               reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD: "2026-8-5"}
        errors = reg.validate_watched_record(rec, {"rooms": []})
        self.assertTrue(any(reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD in e for e in errors),
                        errors)
        rec[reg.OPEN_QUESTIONS_CARRY_SINCE_FIELD] = "2026-08-05"
        self.assertEqual(reg.validate_watched_record(rec, {"rooms": []}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
