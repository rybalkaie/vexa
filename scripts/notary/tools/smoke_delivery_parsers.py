#!/usr/bin/env python3
"""Unit-кейсы для парсеров Ф6: chat-destination и correction-command.

НЕ ходит в сеть, не дёргает LLM — только regex/логика.
"""
from __future__ import annotations

import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent.parent))


def case(name: str, expected, actual) -> bool:
    if expected == actual:
        print(f"  ✅ {name}")
        return True
    print(f"  ❌ {name}: expected={expected!r}, got={actual!r}")
    return False


def main() -> int:
    from notary.lib.llm_postprocess import parse_chat_destination_answer
    from notary.lib.correction_command import parse_correction_command, CorrectionCommand

    print("--- parse_chat_destination_answer ---")
    ok = True
    ok &= case("число chat_id", ("chat", -1001234567890), parse_chat_destination_answer("-1001234567890"))
    ok &= case("число positive", ("chat", 12345), parse_chat_destination_answer("12345"))
    ok &= case("ссылка t.me/c", ("chat", -1001234567890), parse_chat_destination_answer("https://t.me/c/1234567890/42"))
    ok &= case("ссылка t.me/c без msg", ("chat", -1001234567890), parse_chat_destination_answer("https://t.me/c/1234567890"))
    ok &= case("никуда", ("skip", None), parse_chat_destination_answer("никуда"))
    ok &= case("не отправляй", ("skip", None), parse_chat_destination_answer("не отправляй"))
    ok &= case("пропусти", ("skip", None), parse_chat_destination_answer("пропусти"))
    ok &= case("в личку", ("dm", None), parse_chat_destination_answer("в личку"))
    ok &= case("мне в личку", ("dm", None), parse_chat_destination_answer("мне в личку"))
    ok &= case("пустота", ("invalid", None), parse_chat_destination_answer(""))
    ok &= case("мусор", ("invalid", None), parse_chat_destination_answer("чтобы что"))

    print("\n--- parse_correction_command ---")
    ok &= case(
        "поправь протокол с двоеточием",
        CorrectionCommand("sales-quality", "2026-05-27", "переделай блок задач", "fix_protocol"),
        parse_correction_command("поправь протокол sales-quality 2026-05-27: переделай блок задач"),
    )
    ok &= case(
        "поправь протокол без двоеточия",
        CorrectionCommand("sales-quality", "2026-05-27", "переделай блок задач", "fix_protocol"),
        parse_correction_command("поправь протокол sales-quality 2026-05-27 переделай блок задач"),
    )
    ok &= case(
        "удали задачу N из...",
        CorrectionCommand("sales-quality", "2026-05-27", "удали задачу 3 из sales-quality 2026-05-27", "remove_task"),
        parse_correction_command("удали задачу 3 из sales-quality 2026-05-27"),
    )
    ok &= case(
        "удалить задачу 1 из ...",
        CorrectionCommand("anzhee-direktorat", "2026-06-01", "удалить задачу 1 из anzhee-direktorat 2026-06-01", "remove_task"),
        parse_correction_command("удалить задачу 1 из anzhee-direktorat 2026-06-01"),
    )
    ok &= case(
        "tail_negate",
        CorrectionCommand("sales-quality", "2026-05-27", "задачу 3 не было", "tail_negate"),
        parse_correction_command("sales-quality 2026-05-27: задачу 3 не было"),
    )
    ok &= case(
        "обычная команда Ф4 (не должна сработать)",
        None,
        parse_correction_command("протокол sales-quality 2026-05-27"),
    )
    ok &= case(
        "без даты — None",
        None,
        parse_correction_command("поправь протокол sales-quality"),
    )
    ok &= case(
        "разговор о протоколе — None",
        None,
        parse_correction_command("по протоколу sales-quality 2026-05-27 договорились"),
    )

    if ok:
        print("\nРЕЗУЛЬТАТ: PASS")
        return 0
    print("\nРЕЗУЛЬТАТ: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
