"""Ф1 плана `knowledge-content-barrier`: барьер знания ПО СОДЕРЖИМОМУ + уведомление
о принятых файлах + срезание ID Google-таблиц.

Покрывает критерии приёмки дословно:
  B1 — синтетика с ТРАНСЛИТНЫМИ именами (`nalichnye-kassy`, `uchet-dds`, `sebestoimost-postavki`,
       `vzaimoraschety`, `planirovanie-oplat`) и финансовым содержимым, вписанная в манифест,
       в промпт НЕ попадает; причина отказа — в логе, БЕЗ текста файла.
  B2 — ложных срабатываний нет: боевые манифесты ОБЕИХ компаний собираются как раньше
       (каталоги, team/about/values, позиционирование, роли). Регресс-тест на реальных
       `*-context` — он и есть главный предохранитель ISS-21 (отвалится каталог → бот снова
       начнёт врать в названиях моделей).
  B3 — новый файл в манифесте → владельцу «в бриф добавлен: X (N КБ)». Состав не менялся
       → молчим. Первая сборка (снимка нет) → тоже молчим (не спамим «добавлено всё»).
  B4 — ID закрытых Google-таблиц из шапки каталога в собранный промпт НЕ попадают, сами
       артикулы — на месте.
  B5 — Опасная тройка: в лог уходят только метаданные (имя, размер, причина). Ни текста
       файла, ни найденных сумм.

🔒 Главный инвариант фазы: барьер судит СОДЕРЖИМОЕ, а не имя. Аудит доказал, что имени мало —
финансовые файлы базы знаний названы транслитом и проходят все проверки имени насквозь.

Главная ловушка (дайджест плана): порог обязан ловить ДЕНЬГИ, а не «любые числа/слова».
Каталог Anzhee полон «стРУБцин» и «тРУБок» (содержат подстроку «руб»), каталог WB — артикулов
с цифрами, а «товар пРИБЫЛ на склад» — не «прибыль». Границы слов проверяются отдельным тестом.

Тестам не нужен pyyaml (знание — markdown) и не нужен `claude` CLI.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_knowledge_content_barrier -v
"""
from __future__ import annotations

import logging
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

from notary.lib import context_knowledge as ck  # noqa: E402

# Боевые репозитории контекста. Регресс-тест B2 обязан идти по НИМ: синтетическая фикстура
# не доказывает, что реальный каталог товаров продолжает проходить барьер.
_LIVE_ROOT = Path("~/Projects").expanduser()
_LIVE_AVAILABLE = (_LIVE_ROOT / "mpfirst-context").is_dir() and (_LIVE_ROOT / "anzhee-context").is_dir()

# Синтетика B1: реальные ИМЕНА файлов из аудита + правдоподобное финансовое содержимое.
# Сентинель в каждом — если он всплывёт в собранном промпте, значит файл просочился.
_FIN_SENTINEL = "FIN_LEAK_SENTINEL"
_FIN_FILES: dict[str, str] = {
    "company/baza-znaniy/867-nalichnye-kassy-sverka-ostatkov-po-kassam.md": (
        "# Наличные кассы. Сверка остатков по кассам\n\n"
        f"{_FIN_SENTINEL}\n\n"
        "Раз в неделю сверяем остаток по кассе с ДДС. Расхождение фиксируем в таблице.\n"
    ),
    "company/baza-znaniy/924-uchet-dds.md": (
        "# Учет ДДС\n\n"
        f"{_FIN_SENTINEL}\n\n"
        "> Источник: база знаний, раздел «Финансовый департамент».\n\n"
        "Обзор системы учета движения денежных средств представлен в видео.\n"
    ),
    "company/baza-znaniy/925-otchet-po-sebestoimosti-postavki.md": (
        "# Предоставление отчета по себестоимости поставки\n\n"
        f"{_FIN_SENTINEL}\n\n"
        "Себестоимость поставки считается с учётом логистики и таможни.\n"
    ),
    "company/baza-znaniy/zifriend-vzaimoraschety-itog.md": (
        "# Zifriend — итог взаиморасчётов\n\n"
        f"{_FIN_SENTINEL}\n\n"
        "Договорённость финальная: никто никому ничего не должен.\n"
    ),
    "company/baza-znaniy/837-planirovanie-oplat-zakazov-tovara.md": (
        "# Планирование оплат заказов товара\n\n"
        f"{_FIN_SENTINEL}\n\n"
        "Как устроен учет планируемых оплат по заказам рассказано в видео.\n"
    ),
    # Нейтральное имя И нейтральный заголовок — ловится ТОЛЬКО суммами внутри. Это
    # проверка того, что барьер не выродился в «денилист по заголовку».
    "company/baza-znaniy/zametki-po-proektu.md": (
        "# Заметки по проекту\n\n"
        f"{_FIN_SENTINEL}\n\n"
        "Закупили партию на 1 250 000 ₽, продали за 2 100 000 руб.\n"
    ),
}

# Безопасная синтетика B2: то, что обязано проходить. Слова про деньги здесь есть — но так,
# как они встречаются в реальных боевых файлах: в дисклеймере и в определении термина.
_SAFE_SENTINEL = "SAFE_CATALOG_SENTINEL"
_SAFE_FILES: dict[str, str] = {
    "knowledge/product-catalog-anzhee.md": (
        "# Каталог оборудования Anzhee — канон названий моделей\n\n"
        f"{_SAFE_SENTINEL}\n\n"
        "> **Что здесь НЕ лежит и лежать не должно:** цены, дилерские условия, себестоимость,\n"
        "> маржа, остатки. Только идентичность товара.\n\n"
        "- Anzhee H19x40Z-WASH — вращающаяся голова\n"
        "- Anzhee CL30-52-SQ — струбцина (6 струбцин в комплекте)\n"
        "- Пиксельные трубки / Трубки типа AA — 40 трубок\n"
        "- PAR LED 18-10, BAR42x3-UV\n"
    ),
    "company/team.md": (
        "# Команда\n\n"
        f"{_SAFE_SENTINEL}\n\n"
        "- Михаил — закупки: планируемые приходы, просчёт себестоимости поставки.\n"
        "- Татьяна — ценообразование и подрядчики.\n"
    ),
}


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


class _FixtureRepo:
    """Временный `*-context` с манифестом: пишем файлы, вписываем их в манифест, читаем."""

    def __init__(self, company: str = "mpfirst"):
        self.company = company
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / f"{company}-context"
        (self.repo / ck.KNOWLEDGE_SUBPATH).mkdir(parents=True, exist_ok=True)
        self._env = {}

    def __enter__(self):
        self._env = {
            "MEETING_NOTARY_CONTEXT_DIR": os.environ.get("MEETING_NOTARY_CONTEXT_DIR"),
            "NOTARY_KNOWLEDGE_COMPOSITION_PATH": os.environ.get("NOTARY_KNOWLEDGE_COMPOSITION_PATH"),
        }
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(self.root)
        os.environ["NOTARY_KNOWLEDGE_COMPOSITION_PATH"] = str(self.root / "composition.json")
        return self

    def __exit__(self, *exc):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()
        return False

    def add(self, files: dict[str, str]) -> None:
        _write_tree(self.repo, files)

    def manifest(self, lines: list[str]) -> None:
        (self.repo / ck.KNOWLEDGE_SUBPATH / ck.PROTOCOL_KNOWLEDGE_MANIFEST_FILE).write_text(
            "# тестовый манифест\n" + "\n".join(lines) + "\n", encoding="utf-8"
        )

    def load(self):
        return ck.load_protocol_knowledge(self.company)

    def blob(self) -> str:
        b = self.load()
        return "\n".join(t for _, t in list(b.catalog) + list(b.brief))


# --- B1: транслитные финфайлы в манифесте НЕ попадают в промпт -----------------


class TestB1TranslitFinanceBlocked(unittest.TestCase):
    """B1 дословно: впиши в манифест синтетику с транслитными именами и финансовым
    содержимым → ни один файл не попадает в промпт; причина отказа — в логе."""

    def test_translit_finance_files_never_reach_prompt(self):
        with _FixtureRepo() as fx:
            fx.add(_FIN_FILES)
            fx.add(_SAFE_FILES)
            fx.manifest(
                [f"catalog: {list(_SAFE_FILES)[0]}"]
                + [list(_SAFE_FILES)[1]]
                + list(_FIN_FILES)  # человек вписал финансы — код обязан их не пустить
            )
            bundle = fx.load()
            blob = "\n".join(t for _, t in list(bundle.catalog) + list(bundle.brief))
            self.assertNotIn(_FIN_SENTINEL, blob, "финансовый файл просочился в промпт")
            included = [r for r, _ in list(bundle.catalog) + list(bundle.brief)]
            for rel in _FIN_FILES:
                self.assertNotIn(rel, included, f"{rel} попал в промпт")
            # …и при этом безопасные файлы прошли (барьер не «отверг всё подряд»).
            self.assertIn(_SAFE_SENTINEL, blob)

    def test_each_audit_file_blocked_with_reason(self):
        """Каждый файл из аудита отвергнут, и у отказа есть человекочитаемая причина."""
        with _FixtureRepo() as fx:
            fx.add(_FIN_FILES)
            fx.manifest(list(_FIN_FILES))
            rejected = dict(fx.load().rejected)
            for rel in _FIN_FILES:
                self.assertIn(rel, rejected, f"{rel} не отвергнут")
                self.assertTrue(rejected[rel].strip(), f"у отказа {rel} пустая причина")

    def test_name_alone_is_not_enough(self):
        """Файл с БЕЗОБИДНЫМ именем и заголовком, но с суммами внутри — тоже блокируется.
        Иначе барьер остался бы «денилистом по заголовку», а дыра — открытой."""
        r = ck.content_block_reason(
            "# Заметки со встречи\n\nДоговорились: закупка на 1 250 000 ₽, аванс 40%.\n",
            rel="notes.md",
        )
        self.assertIsNotNone(r)

    def test_manifest_cannot_widen_rights(self):
        """A3-инвариант: барьер живёт в КОДЕ. Правка манифеста (чужой репо, правит команда
        без ревью кода) не может открыть финансы."""
        with _FixtureRepo() as fx:
            fx.add({"company/baza-znaniy/924-uchet-dds.md": _FIN_FILES[
                "company/baza-znaniy/924-uchet-dds.md"]})
            fx.manifest(["company/baza-znaniy/924-uchet-dds.md"])
            self.assertEqual(fx.load().brief, [], "манифест расширил права — барьер обойдён")


# --- B2: регресс на БОЕВЫХ манифестах обеих компаний ---------------------------


@unittest.skipUnless(_LIVE_AVAILABLE, "нет клонов *-context (боевой регресс пропущен)")
class TestB2NoFalsePositivesOnLiveRepos(unittest.TestCase):
    """B2 дословно: боевые манифесты ОБЕИХ компаний собираются как раньше. Это главный
    предохранитель: отвалится каталог — бот снова начнёт врать в названиях (ISS-21)."""

    def setUp(self):
        self._prev = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_LIVE_ROOT)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
        else:
            os.environ["MEETING_NOTARY_CONTEXT_DIR"] = self._prev

    def test_mpfirst_live_manifest_fully_assembles(self):
        b = ck.load_protocol_knowledge("mpfirst")
        self.assertTrue(b.catalog, "каталог товаров МПервый выпал из промпта — регресс ISS-21")
        self.assertTrue(b.brief, "бриф МПервый пуст")
        blocked = [(r, why) for r, why in b.rejected if "бюджет" not in why]
        self.assertEqual(blocked, [], f"боевые файлы МПервый отвергнуты барьером: {blocked}")

    def test_anzhee_live_manifest_fully_assembles(self):
        b = ck.load_protocol_knowledge("anzhee")
        self.assertTrue(b.catalog, "каталог оборудования Anzhee выпал из промпта — регресс ISS-21")
        self.assertTrue(b.brief, "бриф Anzhee пуст")
        blocked = [(r, why) for r, why in b.rejected if "бюджет" not in why]
        self.assertEqual(blocked, [], f"боевые файлы Anzhee отвергнуты барьером: {blocked}")

    def test_live_catalogs_pass_content_barrier(self):
        """Каталоги обеих компаний проходят контент-барьер напрямую — включая дисклеймер
        «здесь НЕ лежат цены, себестоимость, маржа» и «струбцины/трубки» внутри."""
        for company, rel in (
            ("mpfirst", "mpfirst-context/knowledge/product-catalog-wb.md"),
            ("anzhee", "anzhee-context/knowledge/notary/product-catalog-anzhee.md"),
        ):
            p = _LIVE_ROOT / rel
            with self.subTest(company=company):
                reason = ck.content_block_reason(p.read_text(encoding="utf-8"), rel=p.name)
                self.assertIsNone(reason, f"каталог {company} отвергнут барьером: {reason}")

    def test_live_finance_files_are_blocked(self):
        """Обратная сторона B2: реальные финфайлы `baza-znaniy` барьер ловит по содержимому
        (сегодня их в манифесте нет — впишет человек завтра)."""
        base = _LIVE_ROOT / "mpfirst-context/company/baza-znaniy"
        for name in (
            "867-nalichnye-kassy-sverka-ostatkov-po-kassam.md",
            "924-uchet-dds.md",
            "793-kak-uchityvat-reinvestirovanie-protsentov-po-zaymam.md",
            "837-planirovanie-oplat-zakazov-tovara.md",
            "925-predostavlenie-otcheta-po-sebestoimosti-postavki.md",
        ):
            p = base / name
            if not p.is_file():
                continue
            with self.subTest(file=name):
                self.assertIsNotNone(
                    ck.content_block_reason(p.read_text(encoding="utf-8"), rel=name),
                    f"{name} прошёл барьер — деньги уехали бы во внешнюю модель",
                )


# --- Ловушки границ слов (дайджест: «стРУБцина» / «тРУБа») ---------------------


class TestWordBoundaries(unittest.TestCase):
    """Порог ловит ДЕНЬГИ, а не «любые числа и похожие буквы». На кириллице `\\b` в Python
    работает, но проверяем именно те слова, на которых защита уже спотыкалась."""

    def test_catalog_words_are_not_money(self):
        for text in (
            "струбцина CL30-52-SQ, 6 струбцин в комплекте",
            "Пиксельные трубки / Трубки типа AA, 40 трубок",
            "Товар прибыл на склад, прибыли все 3 короба",
            "Это займёт 5 минут — займись задачей",
            "Кассета для микрофона, 12 кассет",
            "Модели H19x40Z, BAR42x3-UV, PAR LED 18-10",
        ):
            with self.subTest(text=text[:32]):
                self.assertIsNone(ck.content_block_reason(text, rel="catalog.md"), text)

    def test_real_money_is_caught(self):
        for text in (
            "Закупка на 1 250 000 ₽",
            "Аванс 42 000,00 руб.",
            "Остаток по кассе сверен с ДДС",
            "Маржинальность более 20%",
            "Оклад менеджера пересмотрен",
        ):
            with self.subTest(text=text[:32]):
                self.assertIsNotNone(ck.content_block_reason(text, rel="doc.md"), text)


# --- B3: уведомление о ПРИНЯТЫХ файлах (дифф состава) --------------------------


class TestB3AddedFilesNotified(unittest.TestCase):
    """B3 дословно: новый файл в манифесте → «в бриф добавлен: X (N КБ)». Состав не менялся
    → молчим. Первая сборка (снимка нет) → молчим (иначе спам «добавлено всё»)."""

    def test_first_run_is_silent_but_records_baseline(self):
        with _FixtureRepo() as fx:
            fx.add(_SAFE_FILES)
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}", list(_SAFE_FILES)[1]])
            sent: list[str] = []
            self.assertFalse(
                ck.alert_new_knowledge_files(
                    "mpfirst", fx.load(), pusher=lambda m, **kw: sent.append(m) or True
                ),
                "на первой сборке пришёл спам «добавлено всё»",
            )
            self.assertEqual(sent, [])
            self.assertTrue(ck.load_composition_state(), "baseline-снимок не сохранён")

    def test_unchanged_composition_is_silent(self):
        with _FixtureRepo() as fx:
            fx.add(_SAFE_FILES)
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}", list(_SAFE_FILES)[1]])
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)
            sent: list[str] = []
            for _ in range(3):  # три встречи подряд — состав тот же
                ck.alert_new_knowledge_files(
                    "mpfirst", fx.load(), pusher=lambda m, **kw: sent.append(m) or True
                )
            self.assertEqual(sent, [], "спам на каждой сборке при неизменном составе")

    def test_new_file_triggers_alert_with_name_and_size(self):
        with _FixtureRepo() as fx:
            fx.add(_SAFE_FILES)
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}"])
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)  # baseline
            fx.add({"company/about.md": "# О компании\n\nПортал для дилеров и интеграторов.\n"})
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}", "company/about.md"])
            sent: list[str] = []
            self.assertTrue(
                ck.alert_new_knowledge_files(
                    "mpfirst", fx.load(), pusher=lambda m, **kw: sent.append(m) or True
                )
            )
            self.assertEqual(len(sent), 1)
            self.assertIn("company/about.md", sent[0])
            self.assertIn("добавлен", sent[0])
            self.assertIn("КБ", sent[0], "в уведомлении нет размера файла")

    def test_alert_carries_no_file_content(self):
        """B5 на канале уведомления: имя и размер — да, текст файла — никогда."""
        with _FixtureRepo() as fx:
            fx.add(_SAFE_FILES)
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}"])
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)
            fx.add({"company/about.md": f"# О компании\n\n{_SAFE_SENTINEL}\n"})
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}", "company/about.md"])
            sent: list[str] = []
            ck.alert_new_knowledge_files(
                "mpfirst", fx.load(), pusher=lambda m, **kw: sent.append(m) or True
            )
            self.assertNotIn(_SAFE_SENTINEL, "\n".join(sent))

    def test_empty_bundle_does_not_wipe_snapshot(self):
        """Репо контекста не доехал на VPS → состав пуст. Снимок обязан выжить: иначе после
        восстановления репо все файлы выглядели бы новыми и владельцу пришёл бы ложный алерт."""
        with _FixtureRepo() as fx:
            fx.add(_SAFE_FILES)
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}", list(_SAFE_FILES)[1]])
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)
            before = ck.load_composition_state()
            empty = ck.KnowledgeBundle(catalog=[], brief=[], rejected=[], source="none")
            added, changed, grown, _first = ck.diff_knowledge_composition("mpfirst", empty)
            self.assertEqual((added, changed, grown), ([], [], []))
            self.assertEqual(ck.load_composition_state(), before, "пустой состав затёр снимок")

    def test_broken_snapshot_does_not_crash(self):
        with _FixtureRepo() as fx:
            Path(os.environ["NOTARY_KNOWLEDGE_COMPOSITION_PATH"]).write_text("{битый", encoding="utf-8")
            fx.add(_SAFE_FILES)
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}"])
            self.assertFalse(ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True))


# --- B4: ID закрытых Google-таблиц наружу не уходят ----------------------------


class TestB4GoogleSheetIdsRedacted(unittest.TestCase):
    """B4 дословно: в собранном промпте МПервый нет ID Google-таблиц из шапки каталога;
    сами артикулы — на месте."""

    def test_bare_ids_from_catalog_header_are_hidden(self):
        text = (
            "# Каталог товаров\n"
            "> Где брать живьём: ALTRONIX `10kEZCmDItFu5yArvxFfRiu9V3lZcCnP4dNCQSwhRup0`!`Артикулы`\n"
            "- Pulsar 63ML — артикул ALT-63ML\n"
        )
        out, n = ck.redact_internal_ids(text)
        self.assertEqual(n, 1)
        self.assertNotIn("10kEZCmDItFu5yArvxFfRiu9V3lZcCnP4dNCQSwhRup0", out)
        self.assertIn("Pulsar 63ML", out, "артикул пострадал при вырезании ID")
        self.assertIn("ALT-63ML", out)

    def test_id_inside_url_is_hidden_even_if_lowercase(self):
        """ID в ссылке вырезается по КОНТЕКСТУ, даже если он не смешанного регистра (тогда
        проверка «по форме» его бы пропустила)."""
        out, n = ck.redact_internal_ids(
            "Таблица: https://docs.google.com/spreadsheets/d/abcdefghijklmnopqrstuvwxyz012345/edit"
        )
        self.assertGreaterEqual(n, 1)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", out)

    @unittest.skipUnless(_LIVE_AVAILABLE, "нет клонов *-context")
    def test_live_mpfirst_prompt_has_no_table_ids(self):
        prev = os.environ.get("MEETING_NOTARY_CONTEXT_DIR")
        os.environ["MEETING_NOTARY_CONTEXT_DIR"] = str(_LIVE_ROOT)
        try:
            blob = "\n".join(t for _, t in ck.load_whitelisted_context("mpfirst"))
            raw = (_LIVE_ROOT / "mpfirst-context/knowledge/product-catalog-wb.md").read_text(
                encoding="utf-8"
            )
            ids = ck._INTERNAL_ID_RE.findall(raw)
            self.assertTrue(ids, "в боевом каталоге не нашлось ID — тест потерял смысл")
            for gid in ids:
                self.assertNotIn(gid, blob, f"ID закрытой таблицы {gid[:8]}… уехал бы в промпт")
            self.assertIn("Pulsar", blob, "каталог потерял артикулы")
        finally:
            if prev is None:
                os.environ.pop("MEETING_NOTARY_CONTEXT_DIR", None)
            else:
                os.environ["MEETING_NOTARY_CONTEXT_DIR"] = prev


# --- B5: Опасная тройка — в лог только метаданные ------------------------------


class TestB5NoContentInLogs(unittest.TestCase):
    """B5 дословно: в лог уходят только метаданные (имя, размер, причина). НЕ текст файла и
    не найденные суммы."""

    def test_logs_carry_no_file_text_and_no_amounts(self):
        with _FixtureRepo() as fx:
            fx.add(_FIN_FILES)
            fx.add(_SAFE_FILES)
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}"] + list(_FIN_FILES))
            with self.assertLogs("notary.lib.context_knowledge", level="INFO") as cm:
                fx.load()
            logs = "\n".join(cm.output)
            self.assertIn("не подключено", logs, "отказ вообще не залогирован")
            self.assertNotIn(_FIN_SENTINEL, logs, "текст файла знания утёк в лог")
            for amount in ("1 250 000", "2 100 000"):
                self.assertNotIn(amount, logs, f"найденная сумма {amount} утекла в лог")

    def test_reject_reason_has_no_quote_from_file(self):
        """Причина отказа уходит владельцу в Telegram — в ней не должно быть содержимого."""
        reason = ck.content_block_reason(
            f"# Заметки\n\n{_FIN_SENTINEL}\n\nЗакупка на 1 250 000 ₽\n", rel="notes.md"
        )
        self.assertIsNotNone(reason)
        self.assertNotIn(_FIN_SENTINEL, reason)
        self.assertNotIn("1 250 000", reason)

    def test_no_logger_call_passes_file_content(self):
        """Статический контроль (grep-тест из критерия B5): ни один `logger.*` в модуле не
        получает содержимое файла знания. Разбираем AST, а не текст: `type(data).__name__`
        и `len(text)` — это метаданные и они законны, а вот голый `text` аргументом — нет.

        Тест сторожит БУДУЩИЕ правки: добавит кто-нибудь `logger.debug("файл: %s", text)`
        для отладки — и при утечке логов наружу уедут финансы компании."""
        import ast

        forbidden = {"text", "data", "body", "raw", "content"}
        tree = ast.parse(Path(ck.__file__).read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    and fn.value.id == "logger"):
                continue
            for arg in node.args + [kw.value for kw in node.keywords]:
                # голая переменная с содержимым файла
                if isinstance(arg, ast.Name) and arg.id in forbidden:
                    offenders.append(f"строка {node.lineno}: logger.{fn.attr}(…, {arg.id})")
                # она же, зашитая в f-строку
                if isinstance(arg, ast.JoinedStr):
                    for v in ast.walk(arg):
                        if isinstance(v, ast.Name) and v.id in forbidden:
                            offenders.append(f"строка {node.lineno}: logger.{fn.attr}(f'…{{{v.id}}}')")
        self.assertEqual(offenders, [], f"logger получает содержимое файла знания: {offenders}")


# --- Сторожа дыр, найденных циклом из 5 ходов ---------------------------------
#
# Все шесть дыр ниже были живыми при 20 зелёных тестах фазы. Барьер приватности будут
# править (план прямо разрешает «ослабить порог», если легитимный файл отвергнут) —
# без этих тестов правка вернёт дыру молча, и узнают о ней по факту утечки финансов.


class TestCycle5Regressions(unittest.TestCase):
    """Н1–Н5 цикла из 5 ходов: каждая дыра пропускала деньги компании во внешнюю модель
    или глушила единственный сигнал о том, что состав знания расширился."""

    def test_hard_term_in_disclaimer_line_still_blocks(self):
        """Н1. Слова-триггеры дисклеймера («нельзя», «это не», «отсутствуют») — рядовая
        лексика финансового регламента. Пока они гасили строку целиком, ОДНО такое слово
        рядом с жёстким термином пропускало финфайл в промпт."""
        doc = (
            "# Регламент работы с партнёрами\n\n"
            "Нельзя закрывать взаиморасчёты с партнёром без подписанного акта сверки.\n"
        )
        self.assertIsNotNone(
            ck.content_block_reason(doc, rel="reglament-partnery.md"),
            "жёсткий термин в строке со словом «нельзя» пропустил финфайл в промпт",
        )

    def test_percent_criterion_in_disclaimer_line_still_blocks(self):
        """Н1 (тот же приём через процент). Боевой `912-chek-list-vybora-tovarov-mp.md`
        ловится ТОЛЬКО финансовым процентом — сумм в рублях в нём нет вовсе."""
        doc = "# Чек-лист выбора товара\n\nНельзя брать товар с маржинальностью ниже 20%.\n"
        self.assertIsNotNone(
            ck.content_block_reason(doc, rel="chek-list-vybora.md"),
            "финансовый критерий в процентах со словом «нельзя» пропустил файл в промпт",
        )

    def test_disclaimer_still_protects_the_catalog(self):
        """Обратная сторона Н1: фильтр дисклеймеров обязан остаться для МЯГКИХ терминов —
        иначе каталог товаров блокирует сам себя своим же дисклеймером («Что здесь НЕ
        лежит: цены, себестоимость, маржа») и бот снова врёт в артикулах (ISS-21)."""
        self.assertIsNone(
            ck.content_block_reason(
                _SAFE_FILES["knowledge/product-catalog-anzhee.md"],
                rel="knowledge/product-catalog-anzhee.md",
            ),
            "каталог товаров отвергнут собственным дисклеймером — это регресс ISS-21",
        )

    def test_alert_survives_dead_telegram(self):
        """Н2. Снимок состава фиксировался ДО отправки: один сбой Telegram — и файл,
        уехавший во внешнюю модель, больше никогда бы не всплыл («состав тот же» → молчим).
        Ровно та тихая утечка, которую B3 объявляет невозможной."""
        with _FixtureRepo() as fx:
            fx.add(_SAFE_FILES)
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}"])
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)

            fx.add({"company/strategiya-sdelki.md": "# Стратегия сделки\n\nПереговорная позиция.\n"})
            fx.manifest([f"catalog: {list(_SAFE_FILES)[0]}", "company/strategiya-sdelki.md"])

            def dead(msg, **kw):
                raise RuntimeError("Telegram 502")

            self.assertFalse(ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=dead))
            self.assertNotIn(
                "company/strategiya-sdelki.md",
                ck.load_composition_state().get("mpfirst", {}),
                "снимок зафиксирован при мёртвом канале — алерт потерян навсегда",
            )

            sent: list[str] = []
            self.assertTrue(
                ck.alert_new_knowledge_files(
                    "mpfirst", fx.load(), pusher=lambda m, **kw: sent.append(m) or True
                ),
                "канал ожил, а алерт о новом файле так и не пришёл",
            )
            self.assertIn("company/strategiya-sdelki.md", sent[0])
            self.assertIn(
                "company/strategiya-sdelki.md",
                ck.load_composition_state().get("mpfirst", {}),
                "после успешной отправки снимок обязан зафиксироваться (иначе спам)",
            )

    def test_snapshot_of_other_company_survives_concurrent_write(self):
        """Н4. Снимок общий для всех компаний, а сборки идут из четырёх процессов
        (`finalize-meeting`, `meetings_listener`, `clarify_worker`, ручной regenerate).
        Read-modify-write без лока затирал чужой ключ → следующая сборка видела `first_run`
        и МОЛЧАЛА о новом файле."""
        with _FixtureRepo() as fx:
            B = ck.KnowledgeBundle
            ck.persist_knowledge_composition("mpfirst", B([("a.md", "x" * 10)], [], [], "m"))
            ck.persist_knowledge_composition("anzhee", B([("z.md", "y" * 5)], [], [], "m"))
            ck.persist_knowledge_composition(
                "mpfirst", B([("a.md", "x" * 10)], [("new.md", "n" * 99)], [], "m")
            )
            state = ck.load_composition_state()
            self.assertIn("anzhee", state, "ключ соседней компании затёрт конкурентной записью")
            self.assertIn("new.md", state.get("mpfirst", {}), "новый файл не попал в снимок")

    def test_money_regex_stays_linear_on_numeric_table(self):
        """Н3. `_FIN_MONEY_RE` с открытыми квантификаторами уходил в квадратичный
        бэктрекинг: 16 КБ чисел → 11 с, 40 КБ → 98 с. Пофайловый потолок знания — 128 КБ,
        то есть одна числовая таблица в манифесте вешала сборку КАЖДОГО протокола."""
        import time

        payload = "1 " * 20000  # 40 КБ цифр и пробелов, валюты нет — худший случай
        started = time.perf_counter()
        ck.content_block_reason(payload, rel="tablitsa-ostatkov.md")
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0, f"барьер думал {elapsed:.1f} с — вернулся ReDoS")

    def test_money_is_still_caught_after_redos_fix(self):
        """Обратная сторона Н3: ограничение квантификаторов не должно ослепить барьер."""
        for amount in ("итого 1 250 000 ₽", "42 000,00 руб.", "$1,200", "1 234 567 890 123,00 ₽"):
            self.assertIsNotNone(
                ck.content_block_reason(f"# Заметки\n\nЗакупка: {amount}\n", rel="zametki.md"),
                f"денежная сумма «{amount}» перестала ловиться",
            )

    def test_hard_terms_cover_instrumental_case(self):
        """Н5. Класс `[аыуеой]` матчит ОДИН символ, поэтому окончания «-ой»/«-ом» мимо:
        «расчёт кассой», «под займом» проходили насквозь."""
        for word in ("кассой", "займом"):
            self.assertTrue(
                ck._FIN_HARD_RE.search(f"оплата {word} по договору"),
                f"жёсткий термин в творительном падеже («{word}») не ловится",
            )
        for safe in ("кассета", "это займёт 5 минут", "струбцина", "трубка"):
            self.assertFalse(
                ck._FIN_HARD_RE.search(f"в комплекте {safe}"),
                f"«{safe}» ошибочно принято за финансовый термин — отвалится каталог (ISS-21)",
            )

    def test_aligned_amounts_are_still_caught(self):
        """Ход 4, регрессия ФИКСА Н3. Ограничивая квантификаторы ради ReDoS, легко зажать
        их слишком туго — и барьер ослепнет на суммах, выровненных пробелами в таблице.
        Несосчитанная сумма = финансовый файл в промпте, то есть дыра в приватности."""
        for line in (
            "Итого: 1 250 000 ₽",
            "Итого:  1 250 000    ₽",
            "| Выручка | 1 250 000     ₽ |",
        ):
            self.assertTrue(
                ck._FIN_MONEY_RE.search(line), f"денежная сумма не поймана: {line!r}"
            )

    def test_composition_lock_never_deadlocks_on_reentry(self):
        """Ход 4, риск ФИКСА Н4. `flock` НЕ реентрантен: два `LOCK_EX` на один файл из
        одного процесса встают намертво. Блокирующий захват превратил бы любой будущий
        вложенный вызов в вечный вис сборки протокола на VPS — без единой ошибки в логе.
        Захват обязан быть с таймаутом: не дождались → пишем без лока, но НЕ висим."""
        import threading

        with _FixtureRepo():
            done = threading.Event()

            def nested():
                with ck._composition_lock():          # внешний
                    with ck._composition_lock():      # вложенный — не имеет права повиснуть
                        pass
                done.set()

            t = threading.Thread(target=nested, daemon=True)
            t.start()
            self.assertTrue(
                done.wait(timeout=ck._COMPOSITION_LOCK_WAIT_SEC + 5),
                "вложенный захват лока повесил процесс — бот замолчал бы навсегда",
            )


# --- Приёмка Ф1: барьер обязан ВИДЕТЬ ДЕНЬГИ, а не гадать по имени ------------
#
# Приёмка (независимый аудитор + перепроверка оркестратора на боевых данных) сломала первую
# версию барьера: она держалась на теме документа (H1 + имя файла). Убери финансовую подсказку
# из имени — и 8 форматов денег из 8 уезжали во внешнюю модель, при 20 зелёных тестах фазы.
#
# Все файлы ниже — с НЕВИННЫМ именем и НЕВИННЫМ заголовком. Пройти barrier они могут только
# если он реально увидел деньги в тексте.

_INNOCENT_NAMES = ("itogi-kvartala.md", "svodka.md", "zakupki.md")

# (метка, строка) — то, что человек пишет от руки в выжимке.
_MONEY_FORMATS: list[tuple[str, str]] = [
    ("разделитель точкой",        "Закупка партии — 1.250.000"),
    ("множители без валюты",      "Оборот 1,25 млн, закупка 800 тыс."),
    ("сумма в «к»",               "Сделка на 250к"),
    ("валюта ПЕРЕД суммой",       "Контейнер: USD 12 000"),
    ("доллары словом",            "Поставщик выставил 12 000 долларов"),
    ("евро словом",               "Логистика 3 000 евро"),
    ("условные единицы",          "Расчёт по курсу: 5 000 у.е."),
    ("валюта только в шапке",     "| Статья | Сумма, ₽ |\n|---|---|\n| Аренда | 120 000 |\n| Логистика | 45 000 |"),
    ("ФОТ и оклады людей",        "ФОТ отдела 450 000. Менеджер — 90 000"),
    ("EBITDA",                    "EBITDA за квартал выросла"),
    ("cash flow",                 "Считаем cash flow помесячно"),
    ("НДС",                       "НДС к возмещению оформляем в срок"),
]

# 🛑 Ловушки-легитимы. Каждая — из БОЕВОГО файла или его ближайшей окрестности. Отвергни
# барьер хоть одну — отвалится каталог товаров, и бот снова начнёт врать в названиях моделей
# (ISS-21, ровно та боль, ради которой знание вообще заводили).
_LEGIT_LINES: list[tuple[str, str]] = [
    ("боевой: счастливые пользователи",
     "1. **Более 3 000 счастливых пользователей** караоке-систем Altronix — сотни отзывов"),
    ("боевой: артикул с ёмкостью",
     "| Li-Ion 18650 1200mAh | Li-Ion Аккумулятор 18650 1200mAh | 209705542 |"),
    ("боевой: дата в тексте",           "Состав на 24.06.2026."),
    ("боевой: 60% клавиатура",
     "| Blazar GBR[BS] | Клавиатура-конструктор механическая 60% серая | 247140799 |"),
    ("дисклеймер каталога + счёт позиций",
     "Что здесь НЕ лежит: цены, себестоимость. Здесь только 1 200 позиций."),
    ("струбцина и трубки",              "Струбцина для пиксельных трубок, товар прибыл на склад"),
    ("артикулы",                        "Модели H19x40Z и BAR42x3-UV — крепёж"),
    ("размеры товара",                  "Габариты 600 x 400 мм, вес 12 кг"),
    ("«займёт 5 минут»",                "Настройка — это займёт 5 минут"),
    ("более 500 моделей",               "Более 500 моделей в линейке"),
    ("4K-разрешение",                   "Экран 4к, поддержка 60 Гц"),
    ("телефон",                         "Телефон: +7 495 123 45 67"),
]


class TestAcceptanceB1MoneyWithoutNameHint(unittest.TestCase):
    """B1 (пересдача): деньги ловятся БЕЗ подсказки в имени файла и заголовке."""

    def test_all_money_formats_are_blocked(self):
        for label, line in _MONEY_FORMATS:
            for name in _INNOCENT_NAMES:
                with self.subTest(формат=label, имя=name):
                    reason = ck.content_block_reason(f"# Итоги квартала\n\n{line}\n", rel=name)
                    self.assertIsNotNone(
                        reason,
                        f"деньги «{label}» уехали во внешнюю модель: имя и заголовок невинны, "
                        "барьер обязан увидеть их в ТЕКСТЕ",
                    )

    def test_reason_carries_no_amount(self):
        """Опасная тройка: причина отказа уходит в лог VPS и в Telegram. В ней — тип маркера
        и счётчик, НИКОГДА не сама сумма."""
        for label, line in _MONEY_FORMATS:
            reason = ck.content_block_reason(f"# Итоги квартала\n\n{line}\n", rel="svodka.md") or ""
            for leak in ("1.250.000", "250к", "12 000", "450 000", "120 000", "90 000", "800 тыс"):
                with self.subTest(формат=label, утечка=leak):
                    self.assertNotIn(leak, reason, f"причина отказа цитирует сумму: {reason!r}")


class TestAcceptanceB1NoFalsePositives(unittest.TestCase):
    """B2 под давлением B1. Расширяя денежные маркеры, ломают именно это — поэтому
    каждая ловушка проверяется в САМОМ уязвимом окружении: файл каталога, где рядом лежат
    и денежные слова (в дисклеймере), и числа."""

    def test_legit_lines_still_pass(self):
        for label, line in _LEGIT_LINES:
            with self.subTest(строка=label):
                reason = ck.content_block_reason(
                    "# Каталог товаров — канон названий моделей\n\n"
                    "> **Что здесь НЕ лежит:** цены, дилерские условия, себестоимость, маржа.\n\n"
                    f"{line}\n",
                    rel="product-catalog-wb.md",
                )
                self.assertIsNone(
                    reason,
                    f"каталог отвергнут из-за «{label}» → бот снова врёт в названиях моделей "
                    f"(ISS-21). Причина: {reason}",
                )

    def test_grouped_number_alone_is_not_money(self):
        """Ядро защиты B2. «3 000» само по себе деньгами НЕ считается — смысл ему придаёт
        соседнее слово. Правило «группированное число = деньги» отвергло бы боевой файл
        позиционирования МПервый."""
        self.assertIsNone(
            ck.content_block_reason("# Позиционирование\n\nБолее 3 000 счастливых пользователей\n",
                                    rel="pozitsionirovanie.md")
        )
        self.assertIsNotNone(
            ck.content_block_reason("# Позиционирование\n\nЗакупка партии — 3 000 000\n",
                                    rel="pozitsionirovanie.md"),
            "то же число в денежном контексте обязано блокировать",
        )

    def test_money_context_word_alone_is_not_money(self):
        """Обратная сторона: денежное слово БЕЗ числа тоже ничего не блокирует — иначе
        отвалились бы боевые `komanda-roli.md` («согласование оплат подрядчиков») и
        `values.md` («чистая прибыль приоритетнее оборота»)."""
        for line in (
            "Аналитика и просчёт для закупки товаров · согласование оплат подрядчиков",
            "Чистая прибыль приоритетнее оборота.",
            "Финансовые отчёты · контроль финансов организации · оплаты закупок товаров",
        ):
            with self.subTest(строка=line[:40]):
                self.assertIsNone(
                    ck.content_block_reason(f"# Кто за что отвечает\n\n{line}\n", rel="komanda-roli.md")
                )

    def test_new_regexes_stay_linear(self):
        """Анти-DoS для НОВЫХ ярусов (группировка, множители, таблица с валютой в шапке).
        Худший случай: большая числовая таблица, в каждой строке — денежное слово, то есть
        ярус 3 включён на каждой строке. 40 КБ обязаны разбираться мгновенно: пофайловый
        потолок знания — 128 КБ, а барьер зовётся на сборке КАЖДОГО протокола."""
        import time

        row = "| Закупка | 1 250 000 | 18650 1200mAh | 600 x 400 мм |\n"
        payload = "# Остатки\n\n" + row * 800  # ≈ 40 КБ
        started = time.perf_counter()
        ck.content_block_reason(payload, rel="ostatki.md")
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0, f"барьер думал {elapsed:.1f} с — ReDoS в новых ярусах")


class TestAcceptanceB3ContentHash(unittest.TestCase):
    """B3 (пересдача): «тихая утечка становится невозможной».

    Снимок «имя+размер» ловил только распухание файла. Приёмка воспроизвела обход на боевом
    `komanda-roli.md`: дописали 164 Б с окладами → рост меньше порога (2 КБ) → алерт молчит,
    а файл уже уехал в Claude. Теперь состав сверяется по ХЕШУ содержимого."""

    _ACCEPTED = {
        "knowledge/product-catalog-wb.md": "# Каталог\n\nPulsar 63ML-BG — клавиатура.\n",
        "knowledge/notary/komanda-roli.md": "# Кто за что отвечает\n\nМихаил — закупки.\n",
    }

    def _fixture(self, fx):
        fx.add(dict(self._ACCEPTED))
        fx.manifest([f"catalog: {list(self._ACCEPTED)[0]}", list(self._ACCEPTED)[1]])

    def test_small_edit_of_accepted_file_is_reported(self):
        """Правка ПРИНЯТОГО файла меньше порога роста. Барьер по деньгам её не видит и не
        должен (это не деньги) — ровно для такого остатка B3 и существует: страховка на
        случай, когда контент-барьер промахнулся."""
        with _FixtureRepo() as fx:
            self._fixture(fx)
            sent: list[str] = []
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)  # baseline

            leak = "\nСтратегия по сделке с сетью: соглашаемся не раньше третьего раунда.\n"
            self.assertLess(len(leak.encode()), ck._COMPOSITION_GROWTH_MIN_BYTES,
                            "тест обязан бить именно ПОД порогом роста")
            p = fx.repo / "knowledge/notary/komanda-roli.md"
            p.write_text(p.read_text(encoding="utf-8") + leak, encoding="utf-8")

            def pusher(msg, **kw):
                sent.append(msg)
                return True

            self.assertTrue(
                ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=pusher),
                "правка принятого файла прошла МОЛЧА — владелец не узнает никогда",
            )
            self.assertIn("komanda-roli.md", sent[0])
            self.assertIn("изменён", sent[0])
            self.assertNotIn("Стратегия", sent[0], "в алерт утекло содержимое файла")

    def test_oklady_appended_to_accepted_file_are_stopped_by_barrier(self):
        """Тот же обход, но именно ДЕНЬГАМИ (эксплойт приёмки дословно). Здесь срабатывает
        уже B1: файл не «изменён», а ОТВЕРГНУТ — в промпт он не попадает вовсе, и владелец
        узнаёт об этом соседним каналом (`alert_manifest_problems`)."""
        with _FixtureRepo() as fx:
            self._fixture(fx)
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)

            p = fx.repo / "knowledge/notary/komanda-roli.md"
            p.write_text(
                p.read_text(encoding="utf-8")
                + "\nФОТ отдела — 450 000. Михаил — 120 000 фикс. Татьяна — 90 000 фикс.\n",
                encoding="utf-8",
            )
            bundle = fx.load()
            self.assertNotIn(
                "komanda-roli.md", "\n".join(r for r, _ in list(bundle.catalog) + list(bundle.brief)),
                "оклады уехали во внешнюю модель",
            )
            self.assertTrue(bundle.rejected, "файл выпал молча — владельцу ничего не скажут")
            sent: list[str] = []
            self.assertTrue(
                ck.alert_manifest_problems("mpfirst", bundle, pusher=lambda m, **kw: sent.append(m) or True)
            )
            self.assertNotIn("450 000", sent[0], "в алерт утекла сумма из файла")

    def test_old_snapshot_without_hashes_migrates_silently(self):
        """МИГРАЦИЯ. На VPS уже лежит снимок СТАРОГО формата (имя+размер, без хеша). Первая
        сборка с новым кодом НЕ имеет права прислать «изменено всё» — это спам ровно в тот
        момент, когда доверие к каналу важнее всего."""
        with _FixtureRepo() as fx:
            self._fixture(fx)
            bundle = fx.load()
            import json

            legacy = {  # ← формат v1: голые размеры, хешей нет
                "mpfirst": {rel: len(text.encode("utf-8")) for rel, text in
                            list(bundle.catalog) + list(bundle.brief)}
            }
            snap = Path(os.environ["NOTARY_KNOWLEDGE_COMPOSITION_PATH"])
            snap.write_text(json.dumps(legacy), encoding="utf-8")

            sent: list[str] = []
            self.assertFalse(
                ck.alert_new_knowledge_files("mpfirst", bundle,
                                             pusher=lambda m, **kw: sent.append(m) or True),
                f"миграция снимка прислала владельцу «изменено всё»: {sent}",
            )
            state = ck.load_composition_state()["mpfirst"]
            self.assertTrue(
                all(isinstance(v, dict) and v.get("sha") for v in state.values()),
                "хеши не дописаны — следующая правка снова пройдёт молча",
            )

    def test_after_migration_edit_is_caught(self):
        """Вторая половина миграции: как только хеш известен, правка любого размера видна."""
        with _FixtureRepo() as fx:
            self._fixture(fx)
            import json

            bundle = fx.load()
            snap = Path(os.environ["NOTARY_KNOWLEDGE_COMPOSITION_PATH"])
            snap.write_text(json.dumps({"mpfirst": {rel: len(t.encode("utf-8")) for rel, t in
                                                    list(bundle.catalog) + list(bundle.brief)}}),
                            encoding="utf-8")
            ck.alert_new_knowledge_files("mpfirst", bundle, pusher=lambda m, **kw: True)  # молчаливая миграция

            p = fx.repo / "knowledge/notary/komanda-roli.md"
            p.write_text(p.read_text(encoding="utf-8") + "\nПлан на осень: переговоры с сетью.\n",
                         encoding="utf-8")
            sent: list[str] = []
            self.assertTrue(
                ck.alert_new_knowledge_files("mpfirst", fx.load(),
                                             pusher=lambda m, **kw: sent.append(m) or True),
                "после миграции правка снова прошла молча",
            )
            self.assertIn("изменён", sent[0])

    def test_unchanged_composition_stays_silent(self):
        """Молчание, когда менять нечего, — обязательное условие: иначе алерт превратится в
        шум и его перестанут читать вместе с настоящими сигналами."""
        with _FixtureRepo() as fx:
            self._fixture(fx)
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)
            for _ in range(3):
                self.assertFalse(
                    ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True),
                    "состав не менялся, а алерт ушёл — это спам на каждой встрече",
                )

    def test_same_size_different_content_is_a_new_alert(self):
        """Ключ дедупа — по хешу, а не по размеру. Правка «одну строку убрали, другую
        дописали» размер не меняет: по размеру дедуп `notify.push` погасил бы второй алерт
        как повтор первого, и правка осталась бы невидимой."""
        with _FixtureRepo() as fx:
            self._fixture(fx)
            ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=lambda m, **kw: True)
            keys: list[str] = []

            def pusher(msg, **kw):
                keys.append(kw.get("dedupe_key", ""))
                return True

            p = fx.repo / "knowledge/notary/komanda-roli.md"
            base = p.read_text(encoding="utf-8")
            for tail in ("\nААААА\n", "\nБББББ\n"):  # одинаковая длина, разное содержимое
                p.write_text(base + tail, encoding="utf-8")
                ck.alert_new_knowledge_files("mpfirst", fx.load(), pusher=pusher)
            self.assertEqual(len(keys), 2, "второй алерт не ушёл вовсе")
            self.assertNotEqual(keys[0], keys[1],
                                "разное содержимое дало тот же ключ дедупа — правка невидима")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
