"""Тесты Ф3 delivery-fixes — ISS-7 (маршрутизация личка→группа + команда смены чата).

Покрывает REQ 7.1–7.4 + R-REPLY:
  - 7.1: серия с group-привязкой → `deliver_protocol` адресует ЭТОТ chat_id, не DM.
  - 7.2: нет привязки → авто-в-личку (DM), БЕЗ интерактивного вопроса/зависания.
  - 7.3: корень РИСК4 — точное `==` без нормализации роняло привязку молча;
         теперь резолв нормализуется (регистр/пробелы), а silent-fallthrough
         (привязка есть под другим ключом) ЛОГИРУЕТСЯ. + резолв display→slug.
  - 7.4: команда «серию X шли сюда/в личку/в чат N» → привязка записана,
         применяется к будущим протоколам, повторная команда перезаписывает.
  - R-REPLY: обработчик команды даёт видимое подтверждение («✅ принял»);
             owner-гейт; путь общий для лички и группы.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_delivery_fixes_iss7 -v
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_NOTARY = _HERE.parent           # .../scripts/notary
_SCRIPTS = _NOTARY.parent        # .../scripts (для `from notary.cli.registry import …`)
sys.path.insert(0, str(_NOTARY))
sys.path.insert(0, str(_SCRIPTS))

from lib import llm_postprocess as lp  # noqa: E402
from notary.cli import registry as reg  # noqa: E402
from lib.correction_command import parse_correction_command  # noqa: E402


SAMPLE_PROTOCOL = """#протоколвстречи 11.06.2026

**Встреча:** Тест маршрутизации.

**Длительность:** 30 мин

**Участники:** Илья Рыбалка

**Транскрипт:** [2026-06-11.md](2026-06-11.md)

---

## 1) Раздел

▪️ Один буллет.

---

## Решения

🔸 Решение.

---

## Задачи

**Илья Рыбалка**

- Сделать что-то.
"""

META = {"series": "anzhee-direktorat", "date": "2026-06-11", "sessionUid": "tm-iss7"}


def _fake_render(md_text, out_pdf, *, title, subtitle, **kwargs):
    """Mock PDF-рендера: валидная заглушка `%PDF`, без chromium."""
    p = Path(out_pdf)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n" + b"x" * 64)
    return p


# ---------------------------------------------------------------------------
# REQ 7.3 (корень РИСК4) + 7.1 резолв: чистые функции registry
# ---------------------------------------------------------------------------
class TestSeriesKeyResolution(unittest.TestCase):
    def setUp(self):
        self.watched = {"watched": [
            {"id": "d1", "series": "anzhee-direktorat", "telegram_chat_id": -100111},
            {"id": "t1", "series": "marketplaces-tatiana"},  # без привязки
        ]}

    def test_exact_match(self):
        self.assertEqual(reg.get_telegram_chat_id_for_series("anzhee-direktorat", self.watched), -100111)

    def test_case_drift_resolves(self):
        """РИСК4: рассинхрон регистра больше НЕ роняет привязку в личку."""
        self.assertEqual(reg.get_telegram_chat_id_for_series("Anzhee-Direktorat", self.watched), -100111)

    def test_whitespace_and_quotes_drift_resolves(self):
        self.assertEqual(reg.get_telegram_chat_id_for_series("  anzhee-direktorat  ", self.watched), -100111)
        self.assertEqual(reg.get_telegram_chat_id_for_series("«anzhee-direktorat»", self.watched), -100111)

    def test_no_binding_returns_none(self):
        self.assertIsNone(reg.get_telegram_chat_id_for_series("marketplaces-tatiana", self.watched))

    def test_unknown_series_returns_none(self):
        self.assertIsNone(reg.get_telegram_chat_id_for_series("nope-series", self.watched))

    def test_exact_wins_over_normalized(self):
        """Если есть точная запись — берётся она, не нормализованная другая."""
        w = {"watched": [
            {"series": "Foo", "telegram_chat_id": 11},   # точное "Foo"
            {"series": "foo", "telegram_chat_id": 22},   # нормализованное совпадение
        ]}
        self.assertEqual(reg.get_telegram_chat_id_for_series("Foo", w), 11)

    def test_find_series_bindings(self):
        self.assertEqual(reg.find_series_bindings(self.watched), [("anzhee-direktorat", -100111)])

    def test_resolve_ref_by_slug(self):
        self.assertEqual(reg.resolve_series_ref("anzhee-direktorat", self.watched), "anzhee-direktorat")

    def test_resolve_ref_by_display_name(self):
        """REQ 7.4: владелец называет серию человеческим именем → резолв в slug."""
        dm = {"anzhee-direktorat": "Директорат"}
        self.assertEqual(reg.resolve_series_ref("Директорат", self.watched, display_map=dm), "anzhee-direktorat")
        self.assertEqual(reg.resolve_series_ref("директорат", self.watched, display_map=dm), "anzhee-direktorat")

    def test_resolve_ref_unknown_returns_none(self):
        self.assertIsNone(reg.resolve_series_ref("несуществующая", self.watched))

    def test_resolve_ref_display_for_series_absent_in_watched(self):
        """Имя резолвится только если slug реально есть в watched (привязка к записи)."""
        dm = {"some-other-slug": "Директорат"}
        self.assertIsNone(reg.resolve_series_ref("Директорат", self.watched, display_map=dm))

    def test_normalize_series_key(self):
        self.assertEqual(reg.normalize_series_key("  «Директорат» "), "директорат")
        self.assertEqual(reg.normalize_series_key(None), "")


# ---------------------------------------------------------------------------
# REQ 7.1 / 7.2: маршрутизация в deliver_protocol
# ---------------------------------------------------------------------------
class TestDeliverProtocolRouting(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test-iss7-deliver-")
        self.meta_path = Path(self.tmpdir) / "test.meta.json"
        self.meta_path.write_text(json.dumps({}), encoding="utf-8")
        self._env = {k: os.environ.get(k) for k in
                     ("TELEGRAM_NOTARIUS_BOT_TOKEN", "ENABLE_PROTOCOL_DELIVERY", "TELEGRAM_NOTARIUS_CHAT_ID")}
        os.environ["TELEGRAM_NOTARIUS_BOT_TOKEN"] = "fake-token"
        os.environ.pop("ENABLE_PROTOCOL_DELIVERY", None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @mock.patch.object(lp, "_load_watched_for_series", return_value=-100222)
    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    @mock.patch.object(lp.telegram_api, "send_document")
    def test_group_binding_routed_to_group(self, mock_send_doc, mock_render, mock_watched):
        """REQ 7.1: задан групповой чат → протокол идёт В ГРУППУ (не DM), без target_chat_id."""
        os.environ["TELEGRAM_NOTARIUS_CHAT_ID"] = "777"  # DM-дефолт, НЕ должен сработать
        mock_send_doc.return_value = {"message_id": 7}
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path, meeting_sid="sid-71",
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["chat_id"], -100222)           # групповой, не 777
        # send_document адресован групповому chat_id (2-й позиционный аргумент).
        args, _ = mock_send_doc.call_args
        self.assertEqual(args[1], -100222)
        mock_watched.assert_called_once()

    @mock.patch.object(lp, "_load_watched_for_series", return_value=None)
    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    @mock.patch.object(lp.telegram_api, "send_document")
    def test_no_binding_auto_dm_no_question(self, mock_send_doc, mock_render, mock_watched):
        """REQ 7.2 (ВОПР1=A): нет привязки → авто-в-личку, БЕЗ вопроса/зависания."""
        os.environ["TELEGRAM_NOTARIUS_CHAT_ID"] = "777"  # личка владельца
        mock_send_doc.return_value = {"message_id": 8}
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path, meeting_sid="sid-72",
        )
        self.assertEqual(result["status"], "sent")          # не "asked" — вопрос НЕ задаётся
        self.assertNotEqual(result["status"], "asked")
        self.assertEqual(result["chat_id"], 777)            # ушло в личку
        args, _ = mock_send_doc.call_args
        self.assertEqual(args[1], 777)

    @mock.patch.object(lp, "_load_watched_for_series", return_value=None)
    @mock.patch.object(lp.protocol_to_pdf, "render_pdf_from_markdown", side_effect=_fake_render)
    @mock.patch.object(lp.telegram_api, "send_document")
    def test_explicit_target_overrides_binding_lookup(self, mock_send_doc, mock_render, mock_watched):
        """target_chat_id (перевыпуск/смена) перебивает резолв привязки."""
        mock_send_doc.return_value = {"message_id": 9}
        result = lp.deliver_protocol(
            META, SAMPLE_PROTOCOL, meta_json_path=self.meta_path,
            target_chat_id=-100333, meeting_sid="sid-73",
        )
        self.assertEqual(result["chat_id"], -100333)
        mock_watched.assert_not_called()  # при явном target резолв привязки не нужен


# ---------------------------------------------------------------------------
# REQ 7.3: диагностика silent-fallthrough в _load_watched_for_series
# ---------------------------------------------------------------------------
class TestSilentFallthroughDiagnostic(unittest.TestCase):
    def test_near_miss_key_logs_warning(self):
        """REQ 7.3: близкий ключ-промах (дефис vs подчёркивание) → WARNING (видимый корень).

        Резолвер (консервативный) не матчит `anzhee_direktorat` с `anzhee-direktorat`;
        диагностика ловит близость (схлопнутые разделители) и логирует — корень виден."""
        watched = {"watched": [{"series": "anzhee-direktorat", "telegram_chat_id": -100444}]}
        with self.assertLogs("lib.llm_postprocess", level="WARNING") as cm:
            cid = lp._load_watched_for_series("anzhee_direktorat", _watched=watched)
        self.assertIsNone(cid)
        joined = "\n".join(cm.output)
        self.assertIn("silent-fallthrough", joined)
        self.assertIn("anzhee-direktorat", joined)   # лог несёт ключ привязки (метаданные)

    def test_unrelated_binding_no_warning(self):
        """Чужая серия с привязкой НЕ триггерит WARNING (иначе шум на каждой DM-серии)."""
        watched = {"watched": [{"series": "anzhee-direktorat", "telegram_chat_id": -100444}]}
        logger = logging.getLogger("lib.llm_postprocess")
        with mock.patch.object(logger, "warning") as warn:
            cid = lp._load_watched_for_series("marketplaces-tatiana", _watched=watched)
        self.assertIsNone(cid)
        warn.assert_not_called()

    def test_no_bindings_no_warning(self):
        """Нет привязок вовсе → None без ложного WARNING (отличаем от рассинхрона)."""
        watched = {"watched": [{"series": "anzhee-direktorat"}]}  # без telegram_chat_id
        logger = logging.getLogger("lib.llm_postprocess")
        with mock.patch.object(logger, "warning") as warn:
            cid = lp._load_watched_for_series("anzhee-direktorat", _watched=watched)
        self.assertIsNone(cid)
        warn.assert_not_called()

    def test_matching_binding_returns_cid_no_warning(self):
        watched = {"watched": [{"series": "anzhee-direktorat", "telegram_chat_id": -100555}]}
        logger = logging.getLogger("lib.llm_postprocess")
        with mock.patch.object(logger, "warning") as warn:
            cid = lp._load_watched_for_series("anzhee-direktorat", _watched=watched)
        self.assertEqual(cid, -100555)
        warn.assert_not_called()

    def test_case_drift_resolves_via_load_watched(self):
        """Сквозь _load_watched_for_series: дрейф регистра резолвится (не fallthrough)."""
        watched = {"watched": [{"series": "anzhee-direktorat", "telegram_chat_id": -100666}]}
        cid = lp._load_watched_for_series("Anzhee-Direktorat", _watched=watched)
        self.assertEqual(cid, -100666)


# ---------------------------------------------------------------------------
# REQ 7.4: парсинг команды смены чата
# ---------------------------------------------------------------------------
class TestSetChatCommandParsing(unittest.TestCase):
    def _parse(self, t):
        return parse_correction_command(t)

    def test_series_here(self):
        c = self._parse("серию anzhee-direktorat шли сюда")
        self.assertEqual((c.kind, c.series, c.target), ("set_chat", "anzhee-direktorat", "here"))

    def test_verb_first(self):
        c = self._parse("шли серию anzhee-direktorat в этот чат")
        self.assertEqual((c.kind, c.series, c.target), ("set_chat", "anzhee-direktorat", "here"))

    def test_series_dm(self):
        c = self._parse("серию Директорат слать в личку")
        self.assertEqual((c.kind, c.series, c.target), ("set_chat", "Директорат", "dm"))

    def test_series_explicit_chat_id(self):
        c = self._parse("серию marketplaces-tatiana шли в чат -1001234567890")
        self.assertEqual((c.kind, c.series, c.target), ("set_chat", "marketplaces-tatiana", "-1001234567890"))

    def test_reply_context_empty_series(self):
        c = self._parse("эту серию шли сюда")
        self.assertEqual((c.kind, c.series, c.target), ("set_chat", "", "here"))

    def test_multiword_display_name(self):
        c = self._parse("серию 1-на-1 с Татьяной Филипповой шли сюда")
        self.assertEqual(c.kind, "set_chat")
        self.assertEqual(c.series, "1-на-1 с Татьяной Филипповой")
        self.assertEqual(c.target, "here")

    def test_correction_not_swallowed(self):
        """Гард приоритета: команды коррекции НЕ перехватываются set_chat."""
        c = self._parse("поправь протокол sales-quality 2026-05-27: переделай блок задач")
        self.assertEqual(c.kind, "fix_protocol")
        c2 = self._parse("удали задачу 3 из sales-quality 2026-05-27")
        self.assertEqual(c2.kind, "remove_task")

    def test_correction_with_series_words_not_swallowed(self):
        """Даже если в инструкции коррекции есть «серию … отправь … сюда» — приоритет коррекции."""
        c = self._parse("поправь протокол anzhee-direktorat 2026-05-27: серию задач отправь сюда")
        self.assertEqual(c.kind, "fix_protocol")

    def test_plain_chatter_not_matched(self):
        self.assertIsNone(self._parse("привет, как дела"))
        self.assertIsNone(self._parse("шли мне фоточки"))  # нет слова «серию»


# ---------------------------------------------------------------------------
# REQ 7.4: запись/чтение привязки (round-trip + перезапись) на уровне registry
# ---------------------------------------------------------------------------
class TestSetChatBindingPersist(unittest.TestCase):
    def setUp(self):
        self.watched = {"watched": [
            {"id": "d1", "series": "anzhee-direktorat", "type": "manual"},
            {"id": "d2", "series": "anzhee-direktorat", "type": "manual"},  # 2 записи серии
        ]}

    def test_resolve_set_get_roundtrip(self):
        """REQ 7.4: команда → привязка записана → СЛЕДУЮЩИЙ протокол серии резолвит её."""
        slug = reg.resolve_series_ref("Директорат", self.watched,
                                      display_map={"anzhee-direktorat": "Директорат"})
        self.assertEqual(slug, "anzhee-direktorat")
        n = reg.set_telegram_chat_id_for_series(slug, -100999, self.watched)
        self.assertEqual(n, 2)  # обе записи серии получили привязку
        self.assertEqual(reg.get_telegram_chat_id_for_series("anzhee-direktorat", self.watched), -100999)

    def test_overwrite_changes_binding(self):
        """REQ 7.4: повторная команда «в Z» перезаписывает привязку."""
        reg.set_telegram_chat_id_for_series("anzhee-direktorat", -100999, self.watched)
        reg.set_telegram_chat_id_for_series("anzhee-direktorat", -100777, self.watched)
        self.assertEqual(reg.get_telegram_chat_id_for_series("anzhee-direktorat", self.watched), -100777)

    def test_set_normalized_key_still_writes(self):
        """Дрейф ключа на записи (CLI/ручной путь) тоже находит запись."""
        n = reg.set_telegram_chat_id_for_series("Anzhee-Direktorat", -100888, self.watched)
        self.assertEqual(n, 2)
        self.assertEqual(reg.get_telegram_chat_id_for_series("anzhee-direktorat", self.watched), -100888)


# ---------------------------------------------------------------------------
# REQ 7.4 + R-REPLY: обработчик листенера (owner-гейт, видимое подтверждение)
# ---------------------------------------------------------------------------
class TestSetChatHandler(unittest.TestCase):
    OWNER = 12345          # user_id владельца == его DM chat_id == allowed_chat
    GROUP = -100424242     # групповой чат

    def setUp(self):
        import meetings_listener as ml
        self.ml = ml
        self.watched = {"watched": [{"id": "d1", "series": "anzhee-direktorat", "type": "manual"}]}
        self.saved = None

        def fake_load(lock=False):
            return copy.deepcopy(self.watched)

        def fake_save(data):
            self.saved = data
            self.watched = data

        self._patches = [
            mock.patch.object(reg, "load_watched", side_effect=fake_load),
            mock.patch.object(reg, "save_watched", side_effect=fake_save),
            mock.patch.object(reg, "release_watched_lock", side_effect=lambda: None),
            mock.patch.object(ml, "_series_display_map",
                              return_value={"anzhee-direktorat": "Директорат"}),
            mock.patch.object(ml, "send_message"),
        ]
        self.mocks = [p.start() for p in self._patches]
        self.send = self.mocks[-1]

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _msg(self, text, *, chat_id, from_id):
        return {
            "text": text,
            "chat": {"id": chat_id},
            "from": {"id": from_id},
            "message_id": 555,
        }

    def _last_sent_text(self):
        self.assertTrue(self.send.called, "send_message не вызван — нет видимой реакции (R-REPLY)")
        args, kwargs = self.send.call_args
        # send_message(token, chat_id, text, *, reply_to=...)
        return args[1], args[2]

    def test_owner_dm_named_series_binds_and_confirms(self):
        """REQ 7.4 + R-REPLY: владелец в личке «серию X шли в чат N» → привязка + ✅."""
        msg = self._msg("серию anzhee-direktorat шли в чат -100424242",
                        chat_id=self.OWNER, from_id=self.OWNER)
        handled = self.ml.maybe_route_to_set_chat_command("tok", self.OWNER, self.OWNER, msg)
        self.assertTrue(handled)
        self.assertIsNotNone(self.saved)
        self.assertEqual(reg.get_telegram_chat_id_for_series("anzhee-direktorat", self.saved), -100424242)
        chat_id, text = self._last_sent_text()
        self.assertIn("✅", text)
        self.assertEqual(chat_id, self.OWNER)

    def test_owner_in_group_here_binds_to_group(self):
        """REQ 7.4: владелец прямо в группе «серию X шли сюда» → привязка к этой группе."""
        msg = self._msg("серию anzhee-direktorat шли сюда",
                        chat_id=self.GROUP, from_id=self.OWNER)
        handled = self.ml.maybe_route_to_set_chat_command("tok", self.GROUP, self.OWNER, msg)
        self.assertTrue(handled)
        self.assertEqual(reg.get_telegram_chat_id_for_series("anzhee-direktorat", self.saved), self.GROUP)
        chat_id, text = self._last_sent_text()
        self.assertIn("✅", text)
        self.assertEqual(chat_id, self.GROUP)  # подтверждение в том же чате

    def test_non_owner_in_group_ignored(self):
        """Owner-гейт: НЕ владелец в группе → команда не применяется, не перехватывается."""
        msg = self._msg("серию anzhee-direktorat шли сюда",
                        chat_id=self.GROUP, from_id=99999)  # чужой
        handled = self.ml.maybe_route_to_set_chat_command("tok", self.GROUP, self.OWNER, msg)
        self.assertFalse(handled)
        self.assertIsNone(self.saved)        # ничего не записано
        self.send.assert_not_called()        # и ничего не отправлено

    def test_unknown_series_visible_response_no_write(self):
        """Серия не в реестре → видимое «не нашёл», без записи (НЕ молчаливый провал)."""
        msg = self._msg("серию nesuschestvuyushchaya шли сюда",
                        chat_id=self.OWNER, from_id=self.OWNER)
        handled = self.ml.maybe_route_to_set_chat_command("tok", self.OWNER, self.OWNER, msg)
        self.assertTrue(handled)            # прожевали (дали реакцию)
        self.assertIsNone(self.saved)       # но не записали
        _, text = self._last_sent_text()
        self.assertIn("Не нашёл", text)

    def test_overwrite_via_second_command(self):
        """REQ 7.4: вторая команда «в Z» перезаписывает привязку."""
        m1 = self._msg("серию anzhee-direktorat шли в чат -100111",
                       chat_id=self.OWNER, from_id=self.OWNER)
        self.ml.maybe_route_to_set_chat_command("tok", self.OWNER, self.OWNER, m1)
        self.assertEqual(reg.get_telegram_chat_id_for_series("anzhee-direktorat", self.saved), -100111)
        m2 = self._msg("серию anzhee-direktorat шли в чат -100222",
                       chat_id=self.OWNER, from_id=self.OWNER)
        self.ml.maybe_route_to_set_chat_command("tok", self.OWNER, self.OWNER, m2)
        self.assertEqual(reg.get_telegram_chat_id_for_series("anzhee-direktorat", self.saved), -100222)

    def test_non_command_passes_through(self):
        """Обычное сообщение владельца → False (не наша команда, обычный dispatch)."""
        msg = self._msg("когда там встреча по директорату?", chat_id=self.OWNER, from_id=self.OWNER)
        handled = self.ml.maybe_route_to_set_chat_command("tok", self.OWNER, self.OWNER, msg)
        self.assertFalse(handled)
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
