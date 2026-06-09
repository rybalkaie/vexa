"""Ф7 D5: жёсткий фильтр кредов — НИКОГДА не сохранять ни в один слой.

🔴 Назначение (REQ D5, «опасная тройка»). Пароли, API-ключи, токены доступа,
секреты OAuth, JWT, ключи SSH, seed-фразы/приватные ключи кошельков, номера
банковских карт, CVV/CVC, PIN-коды, коды 2FA — НЕ должны попасть НИ в один слой
персистентности нотариуса: ни в `me/`, ни в `*-context`, ни в auto-vocab, ни в
learning-логи, ни в auto-memory, ни в обычные логи. Этот модуль — единственный
детектор, который зовут ВСЕ write-пути знания (auto_vocab → `vocab_io.merge_new`,
knowledge writeback, feedback-router, самообучение) перед записью кандидата.

Дисциплина «лучше пере-отбросить, чем сохранить секрет»: на развилке «может быть
секрет / может быть легит-термин» выбираем ОТБРОСИТЬ (fail-closed). Кандидаты
знания короткие и структурные (термин, имя-роль, пара написания) — ложно-срабат
на нормальном бизнес-термине почти исключён (пороги длины подобраны так, что
«ALTRONIX», «Dealer 360», «Zifriend», «059ктк», «6 000 440» не флагаются), а цена
ошибки в другую сторону — необратимая утечка секрета в git-историю команды.

Чистые функции, только stdlib `re` — НЕТ IO, НЕТ тяжёлых импортов, не падает.
Логировать можно ТОЛЬКО `secret_kind` (вид «jwt»/«aws-key»), НИКОГДА не значение.
"""

from __future__ import annotations

import re
from typing import Optional

# ── Каталог детекторов (вид → компилированный паттерн) ────────────────────────
# Порядок важен только для `secret_kind` (возвращает ПЕРВЫЙ совпавший вид);
# `looks_like_secret` — это просто «совпал хоть один».

# Блок приватного ключа (RSA/EC/OPENSSH/PGP …).
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----", re.IGNORECASE
)
# Публичный SSH-ключ (тоже не храним — это материал доступа).
_SSH_KEY_RE = re.compile(r"\bssh-(?:rsa|ed25519|dss|ecdsa)\s+AAAA[0-9A-Za-z+/]{20,}")
# JWT: three base64url-сегмента, начинается с `eyJ` (base64 `{"`).
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")
# AWS access key id.
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b")
# GitHub PAT (classic ghp_/gho_/ghu_/ghs_/ghr_ и fine-grained github_pat_).
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")
# Slack-токены.
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
# OpenAI/Anthropic и подобные `sk-…` / `sk-ant-…` / `sk-proj-…`.
_SK_KEY_RE = re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}\b")
# Google API key.
_GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")
# Stripe и подобные `(sk|pk|rk)_(live|test)_…`.
_STRIPE_KEY_RE = re.compile(r"\b[sprk]k_(?:live|test)_[A-Za-z0-9]{16,}\b")
# Bearer-заголовок с непустым токеном.
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b")
# Явное присваивание секрета: ключевое слово + `:`/`=` + непробельное значение ≥6.
# Требуем явный разделитель — «пароль администратора» (без `:`/`=`) НЕ флагается,
# «пароль: qwerty123» / «api_key=…» — флагается.
_ASSIGNMENT_RE = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"client[_-]?secret|private[_-]?key|auth[_-]?token|refresh[_-]?token|"
    r"пароль|секрет|токен|ключ\s*доступа|api[_-]?ключ)"
    r"\s*[:=]\s*['\"]?[^\s'\"]{6,}",
    re.IGNORECASE,
)
# Seed/mnemonic-фраза: ≥12 подряд строчных слов 3–8 латинских букв (BIP39-форма).
_SEED_PHRASE_RE = re.compile(r"\b(?:[a-z]{3,8}\s+){11,}[a-z]{3,8}\b")
# Явный маркер seed/мнемоники рядом со словами.
_SEED_MARKER_RE = re.compile(
    r"\b(?:seed[\s-]?(?:phrase|фраз\w*)|mnemonic|мнемоник\w*|recovery\s+phrase|"
    r"seed[\s-]?слов\w*)\b",
    re.IGNORECASE,
)
# CVV/CVC.
_CVV_RE = re.compile(r"\b(?:cvv2?|cvc2?|квв|цвц)\b\s*[:=]?\s*\d{3,4}\b", re.IGNORECASE)
# PIN-код.
_PIN_RE = re.compile(r"\b(?:pin|пин)(?:[-\s]?код[а-я]*)?\b\s*[:=]?\s*\d{4,6}\b", re.IGNORECASE)
# Код 2FA/OTP/подтверждения. «код» сам по себе слишком част — требуем явный якорь.
_OTP_RE = re.compile(
    r"\b(?:2fa|otp|одноразов\w*\s+код\w*|код\w*\s+(?:из\s+смс|подтвержд\w+)|"
    r"verification\s+code)\b\s*[:=]?\s*\d{4,8}\b",
    re.IGNORECASE,
)
# Длинная hex-строка (MD5/SHA/hex-ключ) ≥32.
_HEX_SECRET_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
# Длинная base64-строка ≥40 (ключи/секреты в base64).
_BASE64_SECRET_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")

# Кандидаты на номер карты: 13–19 цифр, опц. разбитые пробел/дефис группами.
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# (вид, паттерн) в порядке проверки. Карта проверяется отдельно (нужен Luhn).
_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("private-key", _PRIVATE_KEY_RE),
    ("ssh-key", _SSH_KEY_RE),
    ("jwt", _JWT_RE),
    ("aws-key", _AWS_KEY_RE),
    ("github-token", _GITHUB_TOKEN_RE),
    ("slack-token", _SLACK_TOKEN_RE),
    ("api-key", _SK_KEY_RE),
    ("google-key", _GOOGLE_KEY_RE),
    ("stripe-key", _STRIPE_KEY_RE),
    ("bearer", _BEARER_RE),
    ("secret-assignment", _ASSIGNMENT_RE),
    ("seed-marker", _SEED_MARKER_RE),
    ("seed-phrase", _SEED_PHRASE_RE),
    ("cvv", _CVV_RE),
    ("pin", _PIN_RE),
    ("otp-2fa", _OTP_RE),
    ("hex-secret", _HEX_SECRET_RE),
    ("base64-secret", _BASE64_SECRET_RE),
)


def _luhn_ok(digits: str) -> bool:
    """Проверка Луна — отсекает случайные длинные числа (заказы/ID) от номеров карт."""
    if not (13 <= len(digits) <= 19):
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if d < 0 or d > 9:
            return False
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _looks_like_card(text: str) -> bool:
    for m in _CARD_CANDIDATE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group(0))
        if _luhn_ok(digits):
            return True
    return False


def secret_kind(text: object) -> Optional[str]:
    """Вид обнаруженного секрета (для лога: «jwt», «aws-key», …) или None.

    🔴 Возвращает ТОЛЬКО вид, НИКОГДА не значение — безопасно логировать.
    Принимает любой тип (не-строка → None): защищает write-пути от падения.
    """
    if not isinstance(text, str) or not text:
        return None
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            return kind
    if _looks_like_card(text):
        return "card"
    return None


def looks_like_secret(text: object) -> bool:
    """True, если в тексте есть что-либо креденшел-подобное (D5). Fail-closed-якорь."""
    return secret_kind(text) is not None


def is_safe_to_store(value: object) -> bool:
    """Гейт для write-путей: можно ли сохранять `value` в любой слой. = НЕ секрет."""
    return not looks_like_secret(value)


_REDACTION = "[секрет удалён]"


def scrub(text: object) -> str:
    """Заменить все обнаруженные секреты на «[секрет удалён]» (для лог-безопасности).

    Используется там, где надо сохранить ОКРУЖАЮЩИЙ текст, но убрать секрет (напр.
    причина отката в логе). Не-строка → "". Карты вычищаем последними, цифры → маска.
    """
    if not isinstance(text, str) or not text:
        return ""
    out = text
    for _kind, pattern in _PATTERNS:
        out = pattern.sub(_REDACTION, out)
    out = _CARD_CANDIDATE_RE.sub(
        lambda m: _REDACTION if _luhn_ok(re.sub(r"\D", "", m.group(0))) else m.group(0),
        out,
    )
    return out


def filter_safe(values: object) -> list:
    """Оставить из итерируемого только безопасные к хранению строки (отсеять креды)."""
    out: list = []
    for v in values or []:
        if is_safe_to_store(v if isinstance(v, str) else str(v)):
            out.append(v)
    return out
