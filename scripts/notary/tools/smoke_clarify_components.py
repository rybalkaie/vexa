#!/usr/bin/env python3
"""Smoke-проверка компонентов clarify-flow (Ф3) БЕЗ реального Telegram.

Что проверяем:
  1. parse_clarify_callback_data — все формы (с meeting_id, чужой meeting_id,
     __other__, bad data).
  2. parse_clarify_text_answer (regex-ветка) — 4 заявленных формата.
  3. clarify_state — atomic write + read + mark_status + is_past_deadline.
  4. apply_clarify_mapping_to_transcript — на синтетическом .md.

Что НЕ проверяем (нужен реальный бот):
  - telegram_api.send_message / get_updates — это бы стучало в Bot API.
  - Worker long-poll loop — бесконечный, и опять же требует бота.

Запуск:
    cd ~/Projects/meeting-notary
    .venv-cli/bin/python vexa/scripts/notary/tools/smoke_clarify_components.py

Выход:  `SMOKE OK` либо `SMOKE FAIL: <reason>` с exit-code 1.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

THIS_FILE = Path(__file__).resolve()
NOTARY_DIR = THIS_FILE.parent.parent
sys.path.insert(0, str(NOTARY_DIR))

from lib import clarify_state           # noqa: E402
from lib import llm_postprocess         # noqa: E402


def _fail(msg: str) -> None:
    print(f"SMOKE FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        _fail(msg)


# ----- 1. callback_data parser -----

def test_callback_parser() -> None:
    meeting_id = "auto-tm-1779869180376-20260527T154900Z"
    short = llm_postprocess._short_id(meeting_id)
    cluster_keys = ["SPEAKER_00", "SPEAKER_02"]
    name_options = {
        "SPEAKER_00": ["Илья Рыбалка", "Михаил Саргин"],
        "SPEAKER_02": ["Илья Рыбалка", "Михаил Саргин", "Дарья Набережная"],
    }

    # OK: первый cluster, второе имя
    res = llm_postprocess.parse_clarify_callback_data(
        f"cl:{short}:0:1",
        meeting_id=meeting_id,
        cluster_keys=cluster_keys,
        name_options_per_cluster=name_options,
    )
    _assert(res == ("SPEAKER_00", "Михаил Саргин"), f"basic ok: {res}")

    # __other__
    res = llm_postprocess.parse_clarify_callback_data(
        f"cl:{short}:1:o",
        meeting_id=meeting_id,
        cluster_keys=cluster_keys,
        name_options_per_cluster=name_options,
    )
    _assert(res == ("SPEAKER_02", None), f"__other__: {res}")

    # Чужой meeting_id
    res = llm_postprocess.parse_clarify_callback_data(
        "cl:deadbeef:0:1",
        meeting_id=meeting_id,
        cluster_keys=cluster_keys,
        name_options_per_cluster=name_options,
    )
    _assert(res is None, f"foreign meeting_id should None: {res}")

    # bad format
    for bad in ("perm:allow:abcde", "cl:bad", "cl:" + short + ":x:1", "cl:" + short + ":0:99"):
        res = llm_postprocess.parse_clarify_callback_data(
            bad, meeting_id=meeting_id, cluster_keys=cluster_keys,
            name_options_per_cluster=name_options,
        )
        _assert(res is None, f"bad data {bad!r} → expected None, got {res}")

    print("[ok] callback parser: 5 cases passed")


# ----- 2. text response regex parser -----

def test_text_regex_parser() -> None:
    name_pool = ["Илья Рыбалка", "Михаил Саргин", "Дарья Набережная"]
    cluster_label_to_key = {
        "Спикер 1": "SPEAKER_00",
        "Спикер 2": "SPEAKER_01",
        "Спикер 3": "SPEAKER_02",
    }
    cluster_keys_ordered = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]

    cases = [
        # (text, expected dict subset)
        ("Спикер 3 = Дарья Набережная", {"SPEAKER_02": "Дарья Набережная"}),
        ("спикер 3 - Дарья", {"SPEAKER_02": "Дарья Набережная"}),
        ("3 — Дарья", {"SPEAKER_02": "Дарья Набережная"}),
        ("Дарья — это 3", {"SPEAKER_02": "Дарья Набережная"}),
        ("Дарья = спикер 3", {"SPEAKER_02": "Дарья Набережная"}),
        ("Спикер 1 = Илья, Спикер 3 = Дарья", {"SPEAKER_00": "Илья Рыбалка", "SPEAKER_02": "Дарья Набережная"}),
        ("не знаю", {}),
        ("пропусти, без понятия", {}),
    ]
    for text, expected in cases:
        # Используем регекс-ветку напрямую (без LLM-fallback) — он внутри
        # parse_clarify_text_answer срабатывает первым.
        got = llm_postprocess._regex_parse_text_answer(text, cluster_label_to_key, name_pool)
        # Проверяем что expected ⊆ got (LLM-парсер может добавить ещё, но это в LLM-вeтке)
        for k, v in expected.items():
            _assert(got.get(k) == v, f"regex case {text!r}: want {expected}, got {got}")
        if not expected:
            _assert(got == {}, f"regex case {text!r}: expected empty, got {got}")
    print(f"[ok] text regex parser: {len(cases)} cases passed")


# ----- 3. clarify_state -----

def test_clarify_state() -> None:
    with tempfile.TemporaryDirectory(prefix="clarify-state-smoke-") as tmp_root_str:
        tmp_root = Path(tmp_root_str)
        meeting_id = "test-meeting-001"
        state = {
            "meeting_id": meeting_id,
            "transcript_path": str(tmp_root / "fake.md"),
            "meta": {"series": "test", "date": "2026-05-28", "sessionUid": "x"},
            "unclear_clusters": {
                "SPEAKER_00": {
                    "name_options": ["Илья", "Михаил"],
                    "samples": ["[00:01] sample"],
                    "speaker_label_in_md": "Спикер 1",
                    "confidence": 0.65,
                    "current_guess": "Илья",
                }
            },
            "cluster_keys_ordered": ["SPEAKER_00"],
            "name_pool": ["Илья", "Михаил"],
            "chat_id": 12345,
            "message_id": 67890,
            "sent_at": clarify_state.now_iso(),
            "timeout_s": 420,
            "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=420)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "pending",
            "resolved_via": None,
            "resolved_at": None,
            "applied_mapping": None,
        }
        path = clarify_state.write_state(state, root=tmp_root)
        _assert(path.exists(), f"state file should exist: {path}")

        # Прочитать обратно
        loaded = clarify_state.read_state(meeting_id, root=tmp_root)
        _assert(loaded is not None and loaded["status"] == "pending", "read_state pending")

        # list_pending по статусу
        pendings = clarify_state.list_pending(root=tmp_root, status_filter=["pending"])
        _assert(len(pendings) == 1, f"list_pending: want 1, got {len(pendings)}")

        # mark_status
        new = clarify_state.mark_status(meeting_id, "resolved", root=tmp_root,
                                        extra={"resolved_via": "callback"})
        _assert(new is not None and new["status"] == "resolved", "mark_status resolved")
        _assert(new["resolved_via"] == "callback", "extra applied")

        # is_past_deadline: фейковый state с истёкшим deadline
        expired_state = dict(state)
        expired_state["meeting_id"] = "expired-001"
        expired_state["deadline_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        clarify_state.write_state(expired_state, root=tmp_root)
        _assert(clarify_state.is_past_deadline(expired_state), "expired should be past deadline")
        _assert(not clarify_state.is_past_deadline(state), "fresh should not be past")

        # path traversal
        try:
            clarify_state.path_for("../etc/passwd", root=tmp_root)
            _fail("expected ValueError on path traversal in meeting_id")
        except ValueError:
            pass
    print("[ok] clarify_state: write/read/list/mark/deadline/traversal")


# ----- 4. apply transcript mapping -----

def test_apply_transcript() -> None:
    with tempfile.TemporaryDirectory(prefix="apply-smoke-") as tmp_root:
        md = Path(tmp_root) / "transcript.md"
        md.write_text(
            "# Test\n\n"
            "**[00:05] Спикер 1:** Привет, начинаем.\n\n"
            "**[00:12] Спикер 2:** Да-да.\n\n"
            "**[01:23] Спикер 3:** Я подключилась.\n\n"
            "**[02:30] Спикер 1:** Продолжаем.\n",
            encoding="utf-8",
        )
        changed = llm_postprocess.apply_clarify_mapping_to_transcript(
            md, {"Спикер 3": "Дарья Набережная", "Спикер 1": "Илья Рыбалка"},
        )
        _assert(changed, "apply_clarify_mapping must change file")
        text = md.read_text(encoding="utf-8")
        _assert("**[00:05] Илья Рыбалка:**" in text, f"label 1 not replaced: {text!r}")
        _assert("**[02:30] Илья Рыбалка:**" in text, "second occurrence of Спикер 1 must be replaced")
        _assert("**[01:23] Дарья Набережная:**" in text, "Спикер 3 not replaced")
        _assert("Спикер 2" in text, "Спикер 2 must remain untouched")

        # Идемпотентность: повторный вызов не должен ничего ломать.
        changed2 = llm_postprocess.apply_clarify_mapping_to_transcript(
            md, {"Спикер 3": "Дарья Набережная"},
        )
        _assert(not changed2, "second apply must report no change")
    print("[ok] apply_transcript: replacement + idempotency")


# ----- 5. message + keyboard builders -----

def test_builders() -> None:
    meeting_id = "auto-tm-xxx-20260527T154900Z"
    short = llm_postprocess._short_id(meeting_id)
    unclear = {
        "SPEAKER_02": {
            "speaker_label_in_md": "Спикер 3",
            "confidence": 0.62,
            "name_options": ["Илья Рыбалка", "Михаил Саргин"],
            "samples": ["[00:01] «привет»", "[00:05] «как дела»"],
        }
    }
    text = llm_postprocess._build_clarify_message_text("test-series", "2026-05-28", unclear)
    _assert("Спикер 3" in text and "62%" in text, f"message text bad: {text}")

    kb = llm_postprocess._build_clarify_inline_keyboard(meeting_id, ["SPEAKER_02"], unclear)
    rows = kb.get("inline_keyboard") or []
    # Должны быть: header + 2 имени + «Другое»
    flat_callback = [
        btn.get("callback_data", "")
        for row in rows for btn in row
    ]
    _assert(any(cb == f"cl:{short}:0:0" for cb in flat_callback), "name button 0 missing")
    _assert(any(cb == f"cl:{short}:0:1" for cb in flat_callback), "name button 1 missing")
    _assert(any(cb == f"cl:{short}:0:o" for cb in flat_callback), "other button missing")
    # Все cb влезают в 64 байта
    for cb in flat_callback:
        _assert(len(cb.encode("utf-8")) <= 64, f"callback >64 bytes: {cb!r}")
    print("[ok] message text + inline keyboard builders")


# ----- main -----

def main() -> int:
    test_callback_parser()
    test_text_regex_parser()
    test_clarify_state()
    test_apply_transcript()
    test_builders()
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
