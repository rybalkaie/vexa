#!/usr/bin/env python3
"""Guard-скрипт: проверяет, что Speechmatics НЕ искажает доменные термины.

Прогоняет переданный WAV/аудио через текущую конфигурацию Speechmatics
(тот же клиент `lib/speechmatics_client.py`, тот же `additional_vocab` из
`/srv/meeting-notary/config/speechmatics-vocab.json`) и грепает результат на
список известных искажений («known-bad-words»), которые vocab обязан был
вылечить. Если искажение всплыло — это регрессия словаря.

Зависит ТОЛЬКО от Speechmatics (API-ключ через env `SPEECHMATICS_API_KEY`).
НЕ трогает Anthropic / Claude / Telegram — чистая проверка STT-слоя.

Коды возврата:
    0 — искажений из HARD-списка не найдено (vocab работает).
    1 — найдено хотя бы одно HARD-искажение (с указанием каких именно).
    2 — ошибка использования / Speechmatics недоступен / файл не найден.

Использование:
    SPEECHMATICS_API_KEY=... python -m notary.check_vocab_applied <audio.wav>
    # строго: SOFT-предупреждения (СТ-1, кризис) тоже валят rc=1
    ... python -m notary.check_vocab_applied <audio.wav> --strict

Применение (Ф2 доработок-бот-нотариус, 2026-05-29):
    - после правки vocab — одной командой убедиться, что термины распознаются;
    - как регрессионный guard в cron/CI на боевых тестовых аудио.

Тиры:
    HARD  — однозначные искажения (несуществующие слова или термины, которые
            в контексте встреч Ильи всегда = искажение). Валят rc=1.
    SOFT  — слова, которые БЫВАЮТ настоящими (СТ-1 — реальный сертификат YME для
            Минпромторга; «кризис» — обычное слово), но также являются частыми
            искажениями (1С→СТ-1, Битрикс→кризис). По умолчанию — только варнинг,
            rc не меняют. С флагом --strict — тоже валят rc=1.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

# Подключаем lib (когда запускается из родительской директории) — тот же приём,
# что в finalize-meeting.py.
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

try:
    from lib.speechmatics_client import (  # noqa: E402
        VOCAB_CONFIG_PATH,
        SpeechmaticsError,
        _load_additional_vocab,
        transcribe_diarize_wav,
    )
except ImportError as _e:  # noqa: E402 — понятная диагностика вместо немой трассы
    print(
        f"ОШИБКА импорта lib.speechmatics_client: {_e}\n"
        "Запускай из каталога notary/ (или `python -m notary.check_vocab_applied`); "
        "если клиент рефакторили — сверь имена VOCAB_CONFIG_PATH / _load_additional_vocab.",
        file=sys.stderr,
    )
    sys.exit(2)

logger = logging.getLogger("check_vocab_applied")

# Однозначные искажения. Если всплыли в выводе Speechmatics — vocab не сработал.
# Каждый known-bad помечен «<что это на самом деле>» для понятного отчёта.
HARD_BAD_WORDS: dict[str, str] = {
    "беатрикс": "Битрикс",
    "битрэкс": "Битрикс",
    "евросеть": "нейросеть",
    "евросети": "нейросеть",
    "регистратор": "оркестратор",
    "арестратор": "оркестратор",
    "диарезация": "диаризация",
    "диарезацио": "диаризация",
    "спичметикс": "Speechmatics",
    "спич матикс": "Speechmatics",
    "энвитон": "ENVYTON",
    "транскрибация": "транскрипция",
}

# Двусмысленные: бывают и настоящими словами, и искажениями. По умолчанию warn-only.
SOFT_BAD_WORDS: dict[str, str] = {
    "СТ-1": "возможно искажённое «1С» (но СТ-1 — реальный сертификат YME, проверь вручную)",
    "кризис": "возможно искажённое «Битрикс» (но «кризис» — обычное слово)",
}


def _find_hits(text: str, vocabulary: dict[str, str]) -> list[tuple[str, str, int]]:
    """Найти вхождения известных искажений. Возврат: [(bad, что_должно_быть, count)]."""
    low = text.lower()
    hits: list[tuple[str, str, int]] = []
    for bad, should_be in vocabulary.items():
        bad_low = bad.lower()
        # Граница: искомое не должно быть частью более длинного слова.
        # Для многословных («спич матикс») и с дефисом («ст-1») \b ведёт себя
        # корректно на границах с буквами/цифрами; экранируем спецсимволы.
        pattern = r"(?<![\w-])" + re.escape(bad_low) + r"(?![\w-])"
        n = len(re.findall(pattern, low))
        if n:
            hits.append((bad, should_be, n))
    return hits


def _transcript_text(wav_path: str) -> str:
    """Прогнать аудио через Speechmatics и склеить весь распознанный текст."""
    result = transcribe_diarize_wav(wav_path)
    return "\n".join(u.text for u in result.utterances)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проверка, что Speechmatics не искажает доменные термины (vocab-guard).",
    )
    parser.add_argument("audio", help="путь к WAV/аудио для прогона через Speechmatics")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="SOFT-предупреждения (СТ-1, кризис) тоже считать провалом (rc=1)",
    )
    parser.add_argument(
        "--print-transcript",
        action="store_true",
        help="напечатать полный распознанный текст (отладка; ВНИМАНИЕ: дампит "
        "персональные данные встречи в stdout/логи — не включай на боевом аудио в cron/CI)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    audio = Path(args.audio)
    if not audio.exists():
        print(f"ОШИБКА: файл не найден: {audio}", file=sys.stderr)
        return 2

    # Н2 (цикл): явно показываем, КАКОЙ vocab реально тестируется. Запуск не на VPS
    # без SPEECHMATICS_VOCAB_PATH → файла по дефолт-пути нет → клиент грузит 0 терминов
    # → job идёт без additional_vocab → результат guard'а не репрезентативен.
    vocab = _load_additional_vocab()
    print(f"ℹ️  vocab: {len(vocab)} терминов из {VOCAB_CONFIG_PATH}")
    if not vocab:
        print(
            "⚠️  vocab ПУСТ — Speechmatics пойдёт без additional_vocab, проверка "
            "не репрезентативна. Задай SPEECHMATICS_VOCAB_PATH или запусти на VPS.",
            file=sys.stderr,
        )
        return 2

    try:
        text = _transcript_text(str(audio))
    except SpeechmaticsError as e:
        print(f"ОШИБКА Speechmatics: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — guard не должен молча падать
        print(f"ОШИБКА: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    # Н1 (цикл): пустой транскрипт (тишина / битый WAV / не та дорожка) → искажений
    # «не найдено» формально, но проверять было нечего. Зелёный rc=0 тут вводит в
    # заблуждение — возвращаем rc=2 «проверка невозможна».
    if not text.strip():
        print(
            "⚠️  Speechmatics вернул пустой транскрипт (тишина / битый файл / не та "
            "аудиодорожка?) — проверять нечего, vocab НЕ протестирован.",
            file=sys.stderr,
        )
        return 2

    if args.print_transcript:
        print("=== РАСПОЗНАННЫЙ ТЕКСТ ===")
        print(text)
        print("=== /ТЕКСТ ===\n")

    hard_hits = _find_hits(text, HARD_BAD_WORDS)
    soft_hits = _find_hits(text, SOFT_BAD_WORDS)

    if soft_hits:
        print("⚠️  Двусмысленные совпадения (проверь вручную):")
        for bad, note, n in soft_hits:
            print(f"   • «{bad}» ×{n} — {note}")

    if hard_hits:
        print("❌ НАЙДЕНЫ ИСКАЖЕНИЯ (vocab не сработал):")
        for bad, should_be, n in hard_hits:
            print(f"   • «{bad}» ×{n} → должно быть «{should_be}»")
        return 1

    if args.strict and soft_hits:
        print("❌ --strict: двусмысленные совпадения считаются провалом.")
        return 1

    print("✅ Искажений из known-bad-words не найдено — vocab работает.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
