"""Ф7 D5 — тесты жёсткого фильтра кредов (`lib/cred_filter`).

🔴 Защита от утечки: эти тесты должны быть ЖЕЛЕЗНЫМИ. Две оси:
  1. ВСЁ креденшел-подобное (пароль/ключ/токен/OAuth/JWT/SSH/seed/карта/CVV/PIN/2FA)
     детектируется → `is_safe_to_store` == False (никуда не сохраняем);
  2. нормальные бизнес-термины/имена/числа протокола НЕ ложно-флагаются
     (иначе фильтр зарежет легитимное знание).

Чистые функции, без IO/pyyaml — зелёные на системном python3.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 -m unittest tests.test_cred_filter -v
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

from notary.lib import cred_filter as cf  # noqa: E402


# Синтетические секреты (НЕ настоящие — сгенерированы для теста, формат-валидные).
SECRETS = {
    "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQdummysig",
    "aws-key": "AKIAIOSFODNN7EXAMPLE",
    "github-token": "ghp_" + "A" * 36,
    "github-pat": "github_pat_" + "B" * 40,
    "slack-token": "xoxb-" + "123456789012-abcdefABCDEF1234",
    "sk-key": "sk-ant-api03-" + "x" * 40,
    "openai-key": "sk-" + "z" * 40,
    "google-key": "AIza" + "a" * 35,
    "stripe-key": "sk_live_" + "9" * 24,
    "bearer": "Authorization: Bearer abcDEF1234567890ghIJ",
    "ssh-key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAID" + "q" * 25 + " user@host",
    "private-key": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r...\n-----END OPENSSH PRIVATE KEY-----",
    "password-assign": "пароль: qwerty123456",
    "password-eq": "password=Sup3rS3cret!",
    "apikey-assign": "api_key = abcd1234efgh5678",
    "token-ru": "токен: ghs_abcDEF1234567890",
    "hex-secret": "deadbeef" * 5,  # 40 hex chars
    "base64-secret": "QWxhZGRpbjpvcGVuc2VzYW1lQWxhZGRpbjpvcGVuc2VzYW1l12",  # 50 base64
    "seed-phrase": "legal winner thank year wave sausage worth useful legal winner thank yellow",
    "seed-marker": "вот моя seed-фраза для кошелька",
    "cvv": "CVV: 123",
    "pin": "pin-код 4821",
    "2fa": "2fa 483920",
    "otp": "код из смс 558213",
}

# Карта Visa тестовая (проходит Луна) — отдельно, проверяем Luhn-ветку.
TEST_CARD = "4111 1111 1111 1111"

# Легит-кейсы: НЕ должны флагаться (термины/имена/числа из боевых протоколов).
SAFE = [
    "Bolong", "РСЯ", "ЭДО", "АКБ", "оффер", "Dealer 360", "Space Projector 18",
    "ALTRONIX", "ENVONIX", "Zifriend", "Мария Михина", "Сона Енгибарян",
    "059ктк", "6 000 440", "поставки", "коммерция", "Anzhee", "МПервый",
    "Еженедельная координация", "1С", "Битрикс24", "Dream Story",
    "за поставки отвечает Мария", "не РСЯ, а РЕЦ",
    "пароль администратора знает только Михаил",  # «пароль» без значения — НЕ секрет
    "обсудили ключ к успеху продаж",              # «ключ» без `:` значения — НЕ секрет
    "заказ номер 12345 на доставку",
]


class TestSecretsDetected(unittest.TestCase):
    """Каждый секрет распознан и помечен небезопасным к хранению (D5)."""

    def test_every_secret_flagged(self):
        for label, payload in SECRETS.items():
            with self.subTest(secret=label):
                self.assertTrue(cf.looks_like_secret(payload),
                                f"{label} НЕ задетектирован — утечка!")
                self.assertFalse(cf.is_safe_to_store(payload),
                                 f"{label} помечен безопасным — утечка!")
                self.assertIsNotNone(cf.secret_kind(payload))

    def test_card_luhn_detected(self):
        self.assertTrue(cf.looks_like_secret(TEST_CARD))
        self.assertEqual(cf.secret_kind(TEST_CARD), "card")

    def test_card_non_luhn_not_flagged_as_card(self):
        # Случайные 16 цифр, НЕ проходящие Луна, картой не считаем.
        self.assertNotEqual(cf.secret_kind("1234 5678 9012 3456"), "card")

    def test_secret_inside_sentence(self):
        # Секрет посреди обычного текста правки — всё равно ловим (D5: «строка с токеном»).
        txt = "коллеги, вот ключ для интеграции AKIAIOSFODNN7EXAMPLE, сохраните"
        self.assertTrue(cf.looks_like_secret(txt))


class TestSafeNotFlagged(unittest.TestCase):
    """Легит-термины/имена/числа НЕ ложно-флагаются (фильтр не режет знание)."""

    def test_business_terms_safe(self):
        for value in SAFE:
            with self.subTest(value=value):
                self.assertTrue(cf.is_safe_to_store(value),
                                f"легит «{value}» ложно помечен секретом")
                self.assertIsNone(cf.secret_kind(value))


class TestKindIsValueFree(unittest.TestCase):
    """secret_kind не возвращает само значение (безопасно логировать)."""

    def test_kind_label_only(self):
        kind = cf.secret_kind(SECRETS["jwt"])
        self.assertEqual(kind, "jwt")
        self.assertNotIn("eyJ", kind)


class TestScrub(unittest.TestCase):
    """scrub вычищает секрет, сохраняя окружение."""

    def test_scrub_removes_secret_keeps_text(self):
        txt = "ключ AKIAIOSFODNN7EXAMPLE для прода"
        out = cf.scrub(txt)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
        self.assertIn("для прода", out)

    def test_scrub_card(self):
        out = cf.scrub(f"карта {TEST_CARD} оплата")
        self.assertNotIn("4111", out)
        self.assertIn("оплата", out)

    def test_scrub_non_string(self):
        self.assertEqual(cf.scrub(None), "")
        self.assertEqual(cf.scrub(123), "")


class TestFilterSafe(unittest.TestCase):
    """filter_safe оставляет только безопасные строки."""

    def test_drops_secrets_keeps_terms(self):
        mixed = ["Bolong", SECRETS["jwt"], "РСЯ", SECRETS["aws-key"], "оффер"]
        out = cf.filter_safe(mixed)
        self.assertEqual(out, ["Bolong", "РСЯ", "оффер"])

    def test_empty_and_none(self):
        self.assertEqual(cf.filter_safe(None), [])
        self.assertEqual(cf.filter_safe([]), [])


class TestRobustness(unittest.TestCase):
    """Не падает на не-строках / пустом (write-пути зовут на любом входе)."""

    def test_non_string_inputs(self):
        for bad in (None, 123, [], {}, 4.5):
            self.assertFalse(cf.looks_like_secret(bad))
            self.assertIsNone(cf.secret_kind(bad))
            self.assertTrue(cf.is_safe_to_store(bad))


class TestNoLayerAcceptsCredential(unittest.TestCase):
    """🔴 D5 «ни в один слой»: токен не попадает НИ в ASR-словарь, НИ в company-
    outbox/приватную очередь, НИ в карточку серии (learning), НИ через роутер.

    Это сквозная проверка критерия «строка с токеном не попадает никуда».
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._env = {
            "NOTARY_KNOWLEDGE_OUTBOX_DIR": str(base / "outbox"),
            "NOTARY_PRIVATE_KNOWLEDGE_QUEUE": str(base / "private.md"),
            "NOTARY_KNOWLEDGE_RATCHET_PATH": str(base / "ratchet.json"),
            "NOTARY_CONFIG_PROPOSALS_PATH": str(base / "config.jsonl"),
            "MEETING_NOTARY_CONTEXT_DIR": str(base / "no-context"),
            "MEETING_NOTARY_FEEDBACK_DIR": str(base / "_feedback_edits"),
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

    def test_vocab_layer_rejects(self):
        from notary.auto_vocab import vocab_io
        _doc, added = vocab_io.merge_new({"additional_vocab": []},
                                         [{"content": SECRETS["aws-key"]}, {"content": "Bolong"}])
        names = [e["content"] for e in added]
        self.assertIn("Bolong", names)
        self.assertNotIn(SECRETS["aws-key"], names)

    def test_writeback_layer_rejects(self):
        from notary.lib import knowledge_writeback as wb
        res = wb.propose_term(SECRETS["sk-key"], series="s1", company="anzhee",
                              publication_allowed=True)
        self.assertEqual(res.layer, "drop")
        self.assertEqual(wb._read_outbox("anzhee"), [])
        self.assertFalse(Path(self._env["NOTARY_PRIVATE_KNOWLEDGE_QUEUE"]).is_file())

    def test_learning_series_card_rejects(self):
        from notary.lib import feedback_learning as fl
        # Терм-пара с токеном не учится (карточка серии).
        self.assertIsNone(fl._pair_from("РСЯ", SECRETS["github-token"]))
        # Смысл-правило с токеном не сохраняется.
        self.assertIsNone(fl.record_meaning_rule("s1", "ключ", SECRETS["jwt"], root=self.base))

    def test_router_layer_drops(self):
        from notary.lib import feedback_router as fr
        out = fr.route_edits({"series": "s1"},
                             [{"text": f"вот токен {SECRETS['github-token']} сохрани"}])
        self.assertEqual(out["dropped_creds"], 1)
        self.assertEqual(sum(out["routed"].values()), 0)


if __name__ == "__main__":
    unittest.main()
