#!/usr/bin/env python3
"""Smoke pure-Python `lib.stakeholder_track.append_to_open_subsection`.

Закрывает архитектурный долг Ф5: запись в трек теперь без shell-обёртки,
работает одинаково на маке и на VPS. Smoke прогоняет 4 кейса:

  1. Подсекции нет → создаётся новая в конце блока «Открыто».
  2. Подсекция есть → блок добавляется в её конец (не плодит дубль `### `).
  3. Файла «Открыто» нет (странный кейс) → создаётся вместе с подсекцией.
  4. Whitelist enforcement (с реестром) — отказ при path вне списка.

Запуск:
  cd ~/Projects/meeting-notary/vexa/scripts/notary && \\
    python3 tools/smoke_stakeholder_track.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
sys.path.insert(0, str(NOTARY_DIR.parent))

from notary.lib import stakeholder_track  # noqa: E402


def case_1_no_subsection(tmpdir: Path) -> bool:
    f = tmpdir / "case1.md"
    f.write_text(
        "# Татьяна — открытые вопросы\n\n"
        "## 🟢 Открыто\n"
        "\n"
        "### Маркетплейсы\n"
        "\n"
        "- [ ] Старая задача\n"
        "\n"
        "## ✅ Закрытые\n"
        "\n"
        "- ✅ что-то закрытое\n",
        encoding="utf-8",
    )
    block = (
        "- [протокол встречи](../../../встречи/marketplaces-tatiana/2026-05-28-protokol.md)\n"
        "- [ ] **Прислать звонки** контекст: «...»"
    )
    ok = stakeholder_track.append_to_open_subsection(
        f, "📋 Из встречи 2026-05-28", block, skip_whitelist=True
    )
    if not ok:
        print("  [case1] вернула False")
        return False
    txt = f.read_text(encoding="utf-8")
    needed = [
        "### 📋 Из встречи 2026-05-28",
        "Прислать звонки",
        "## ✅ Закрытые",
        "### Маркетплейсы",
    ]
    for n in needed:
        if n not in txt:
            print(f"  [case1] нет ожидаемого: {n!r}")
            return False
    # Подсекция «📋 Из встречи» должна быть ВНУТРИ Открыто, не после Закрытых.
    idx_subs = txt.index("### 📋 Из встречи 2026-05-28")
    idx_closed = txt.index("## ✅ Закрытые")
    if idx_subs > idx_closed:
        print("  [case1] подсекция после Закрытые — ошибка позиции")
        return False
    return True


def case_2_existing_subsection(tmpdir: Path) -> bool:
    f = tmpdir / "case2.md"
    f.write_text(
        "# Михаил\n\n"
        "## 🟢 Открыто\n"
        "\n"
        "### 📋 Из встречи 2026-05-28\n"
        "\n"
        "- [протокол встречи](../../../встречи/anzhee-eremeev/2026-05-28-protokol.md)\n"
        "- [ ] **Первая задача**\n"
        "\n"
        "## ✅ Закрытые\n",
        encoding="utf-8",
    )
    block = "- [ ] **Вторая задача** контекст: новый блок добавлен"
    ok = stakeholder_track.append_to_open_subsection(
        f, "📋 Из встречи 2026-05-28", block, skip_whitelist=True
    )
    if not ok:
        print("  [case2] вернула False")
        return False
    txt = f.read_text(encoding="utf-8")
    if txt.count("### 📋 Из встречи 2026-05-28") != 1:
        print(f"  [case2] заголовок дублирован: {txt.count('### 📋 Из встречи 2026-05-28')}")
        return False
    if "Первая задача" not in txt or "Вторая задача" not in txt:
        print("  [case2] потерял задачу")
        return False
    # Порядок задач: Первая до Второй.
    if txt.index("Первая задача") >= txt.index("Вторая задача"):
        print("  [case2] неправильный порядок задач")
        return False
    return True


def case_3_no_open_block(tmpdir: Path) -> bool:
    f = tmpdir / "case3.md"
    f.write_text(
        "# Дарья — открытые вопросы\n\nИстория пустая.\n",
        encoding="utf-8",
    )
    block = "- [ ] **Совершенно новая**"
    ok = stakeholder_track.append_to_open_subsection(
        f, "📋 Из встречи 2026-05-28", block, skip_whitelist=True
    )
    if not ok:
        print("  [case3] вернула False")
        return False
    txt = f.read_text(encoding="utf-8")
    for n in ["## 🟢 Открыто", "### 📋 Из встречи 2026-05-28", "Совершенно новая"]:
        if n not in txt:
            print(f"  [case3] нет {n!r}")
            return False
    return True


def case_4_whitelist_block(tmpdir: Path) -> bool:
    f = tmpdir / "case4-rogue.md"
    f.write_text(
        "## 🟢 Открыто\n\n",
        encoding="utf-8",
    )
    # Подсунем пустой реестр (env вне whitelist), expected — False.
    os.environ["MEETING_NOTARY_STAKEHOLDERS_JSON"] = str(tmpdir / "no-such.json")
    block = "- [ ] **Не должен дойти**"
    ok = stakeholder_track.append_to_open_subsection(
        f, "📋 Из встречи 2026-05-28", block
        # skip_whitelist=False → проверка whitelist
    )
    del os.environ["MEETING_NOTARY_STAKEHOLDERS_JSON"]
    if ok:
        print("  [case4] записал в файл вне whitelist — это бага")
        return False
    txt = f.read_text(encoding="utf-8")
    if "Не должен дойти" in txt:
        print("  [case4] контент дописался несмотря на отказ")
        return False
    return True


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory(prefix="smoke-track-") as td_str:
        td = Path(td_str)
        cases = [
            ("case1 (нет подсекции)", case_1_no_subsection),
            ("case2 (подсекция есть)", case_2_existing_subsection),
            ("case3 (нет «Открыто»)", case_3_no_open_block),
            ("case4 (whitelist отказ)", case_4_whitelist_block),
        ]
        for name, fn in cases:
            try:
                ok = fn(td)
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ {name}: исключение {type(e).__name__}: {e}")
                failed += 1
                continue
            if ok:
                print(f"  ✅ {name}")
            else:
                print(f"  ❌ {name}")
                failed += 1
    if failed:
        print(f"\nРЕЗУЛЬТАТ: FAIL ({failed} fail)")
        return 1
    print("\nРЕЗУЛЬТАТ: PASS (4/4)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
