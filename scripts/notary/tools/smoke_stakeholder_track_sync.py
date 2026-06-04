#!/usr/bin/env python3
"""Smoke Ф3 — `lib.llm_postprocess.sync_stakeholder_track` (закрытие + добавление).

Прогоняет автосвязку протокол → трек стейкхолдера на /tmp-копии фикстуры трека
с МОК-ответом LLM (без реального `claude --print`). Кейсы:

  1. Флаг ON: LLM возвращает {closed:[<открытый пункт>], new:[<новый>]} →
     закрытый пункт уезжает в «✅ Закрытые» с пометкой; убран из «🟢 Открыто»;
     новый появляется в «🟢 Открыто».
  2. Флаг OFF (ENABLE_STAKEHOLDER_TRACK_CLOSE=0): no-op, LLM не зовётся,
     трек не изменён.
  3. РАЗМ3: LLM вернул closed-пункт НЕ из списка открытых → отброшен,
     ложного закрытия нет.

Запуск:
  cd ~/Projects/meeting-notary/vexa/scripts/notary && \\
    python3 tools/smoke_stakeholder_track_sync.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
sys.path.insert(0, str(NOTARY_DIR))

from lib import llm_postprocess as lp  # noqa: E402

_FIXTURE = NOTARY_DIR / "tests" / "fixtures" / "stakeholder_track_sample.md"
_OPEN_ITEM = (
    "**Профиль роли продуктолога** — подготовить, чтобы был под рукой. "
    "Спросить статус."
)
_PROTOCOL = (
    "#протоколвстречи 04.06.2026\n\n"
    "Обсудили профиль роли продуктолога — Илья подготовил, вопрос закрыт.\n"
    "Договорились: Тестовый пришлёт новый прайс по караоке до пятницы.\n"
)


def _setup(tmp: Path):
    track = tmp / "companies" / "test" / "совещания" / "test-otkrytye-voprosy.md"
    track.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURE, track)
    reg = tmp / "stakeholders.json"
    reg.write_text(json.dumps({"stakeholders": [{
        "slug": "test", "name": "Тестовый",
        "file_path": "companies/test/совещания/test-otkrytye-voprosy.md",
        "company_tag": "test",
    }]}, ensure_ascii=False), encoding="utf-8")
    meta = {"series": "test", "date": "2026-06-04",
            "expectedParticipants": ["Илья Рыбалка", "Тестовый"]}
    env = {"MEETING_NOTARY_STAKEHOLDERS_JSON": str(reg), "ME_DIR": str(tmp)}
    return track, meta, env


def _open_part(text: str) -> str:
    return text.split("## ✅ Закрыт")[0]


def case_on(tmp: Path) -> bool:
    track, meta, env = _setup(tmp / "on")
    llm_json = json.dumps({
        "closed": [_OPEN_ITEM],
        "new": ["Прислать новый прайс по караоке до пятницы"],
    }, ensure_ascii=False)
    env = dict(env)
    env["ENABLE_STAKEHOLDER_TRACK_CLOSE"] = "1"
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(lp, "call_claude_print", return_value=llm_json):
        res = lp.sync_stakeholder_track(_PROTOCOL, meta, meeting_sid="smoke-on")
    if not (res["enabled"] and res["closed"] == 1 and res["new"] == 1 and not res["errors"]):
        print(f"  [on] неожиданный результат: {res}")
        return False
    text = track.read_text(encoding="utf-8")
    op = _open_part(text)
    closed = text.split("## ✅ Закрыт", 1)[1] if "## ✅ Закрыт" in text else ""
    checks = [
        (_OPEN_ITEM not in op, "закрытый пункт всё ещё в «Открыто»"),
        (_OPEN_ITEM in closed, "закрытого пункта нет в «Закрытые»"),
        ("закрыто ботом по встрече 2026-06-04" in closed, "нет пометки/даты в «Закрытые»"),
        ("Прислать новый прайс по караоке" in op, "новый пункт не добавлен в «Открыто»"),
    ]
    for ok, msg in checks:
        if not ok:
            print(f"  [on] {msg}")
            return False
    return True


def case_off(tmp: Path) -> bool:
    track, meta, env = _setup(tmp / "off")
    before = track.read_text(encoding="utf-8")
    env = dict(env)
    env["ENABLE_STAKEHOLDER_TRACK_CLOSE"] = "0"

    def _boom(*a, **k):
        raise AssertionError("LLM зван при выключенном флаге")

    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(lp, "call_claude_print", side_effect=_boom):
        res = lp.sync_stakeholder_track(_PROTOCOL, meta, meeting_sid="smoke-off")
    if res["enabled"]:
        print(f"  [off] enabled=True при OFF: {res}")
        return False
    if track.read_text(encoding="utf-8") != before:
        print("  [off] трек изменён при выключенном флаге")
        return False
    return True


def case_razm3(tmp: Path) -> bool:
    track, meta, env = _setup(tmp / "razm3")
    before = track.read_text(encoding="utf-8")
    llm_json = json.dumps({"closed": ["Выдуманный пункт, которого нет"], "new": []},
                          ensure_ascii=False)
    env = dict(env)
    env["ENABLE_STAKEHOLDER_TRACK_CLOSE"] = "1"
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(lp, "call_claude_print", return_value=llm_json):
        res = lp.sync_stakeholder_track(_PROTOCOL, meta, meeting_sid="smoke-razm3")
    if res["closed"] != 0:
        print(f"  [razm3] закрыл несуществующий пункт: {res}")
        return False
    if track.read_text(encoding="utf-8") != before:
        print("  [razm3] трек изменён при ложном closed")
        return False
    return True


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory(prefix="smoke-track-sync-") as td_str:
        td = Path(td_str)
        cases = [
            ("case ON (закрытие + добавление)", case_on),
            ("case OFF (no-op)", case_off),
            ("case РАЗМ3 (ложный closed отброшен)", case_razm3),
        ]
        for name, fn in cases:
            try:
                ok = fn(td)
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ {name}: исключение {type(e).__name__}: {e}")
                failed += 1
                continue
            print(f"  {'✅' if ok else '❌'} {name}")
            if not ok:
                failed += 1
    if failed:
        print(f"\nРЕЗУЛЬТАТ: FAIL ({failed} fail)")
        return 1
    print("\nРЕЗУЛЬТАТ: PASS (3/3)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
