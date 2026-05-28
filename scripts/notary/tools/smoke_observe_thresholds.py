#!/usr/bin/env python3
"""Smoke: observe_thresholds.py — фиксирует контракт лог-форматов.

Если завтра кто-то поменяет формат `[delivery] sent meeting=X ...` или
`Protocol written → ...` в llm_postprocess/finalize-meeting — этот smoke
упадёт, observe_thresholds не сломается тихо.

Запуск:
    cd ~/Projects/meeting-notary/vexa/scripts/notary
    python3 tools/smoke_observe_thresholds.py
"""
from __future__ import annotations

import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
sys.path.insert(0, str(NOTARY_DIR))
sys.path.insert(0, str(THIS_DIR))

# Импортируем как module (имя без дефиса в файле, OK для import).
# Регистрация в sys.modules ДО exec — Python 3.9 dataclass требует.
import importlib.util
spec = importlib.util.spec_from_file_location("observe_thresholds", THIS_DIR / "observe_thresholds.py")
observe = importlib.util.module_from_spec(spec)
sys.modules["observe_thresholds"] = observe
spec.loader.exec_module(observe)


def case(name, expected_kind, expected_fields, raw_line):
    events = observe.parse_log([raw_line])
    if not events:
        print(f"FAIL {name}: parse_log вернул 0 событий из строки\n  {raw_line!r}")
        return False
    ev = events[0]
    if ev.kind != expected_kind:
        print(f"FAIL {name}: kind={ev.kind!r}, ожидалось {expected_kind!r}")
        return False
    for k, v in expected_fields.items():
        if ev.fields.get(k) != v:
            print(f"FAIL {name}: fields[{k!r}]={ev.fields.get(k)!r}, ожидалось {v!r}")
            return False
    print(f"OK   {name}")
    return True


def main() -> int:
    passed = []
    passed.append(case(
        "delivery_sent",
        "delivery_sent",
        {"sid": "auto-tm-123", "chat_id": -1001234567890, "parts": 2},
        "2026-05-28T15:00:00Z INFO [delivery] sent meeting=auto-tm-123 chat_id=-1001234567890 parts=2 message_ids=[1,2] elapsed=0.5s at=2026-05-28T15:00:00Z",
    ))
    passed.append(case(
        "delivery_idempotent",
        "delivery_idempotent",
        {"sid": "auto-tm-456", "chat_id": -1001234567890, "parts": 1},
        "[delivery] idempotent skip meeting=auto-tm-456 chat_id=-1001234567890 parts=1",
    ))
    passed.append(case(
        "delivery_disabled",
        "delivery_disabled",
        {},
        "INFO [delivery] disabled by ENABLE_PROTOCOL_DELIVERY=0",
    ))
    passed.append(case(
        "delivery_asked",
        "delivery_asked",
        {"sid": "auto-tm-789"},
        "[delivery] asked meeting=auto-tm-789 reason=no_binding",
    ))
    passed.append(case(
        "delivery_failed",
        "delivery_failed",
        {"error": "Forbidden: bot was kicked from the supergroup"},
        "WARNING [delivery] failed (non-fatal): Forbidden: bot was kicked from the supergroup",
    ))
    passed.append(case(
        "correction_applied",
        "correction_applied",
        {"sid": "auto-tm-NN", "kind": "targeted_remove", "version": "v1"},
        "[correction] applied meeting=auto-tm-NN kind=targeted_remove version=v1",
    ))
    passed.append(case(
        "protocol_written_simple",
        "protocol_written",
        {"path": "/srv/meeting-notary/встречи/sales-quality/2026-05-28.md"},
        "Protocol written → /srv/meeting-notary/встречи/sales-quality/2026-05-28.md",
    ))
    passed.append(case(
        "protocol_written_with_suffix_strip",
        "protocol_written",
        # Хвостовой суффикс ` (took 1.2s)` отрезается фиксом Н5 хода 1.
        {"path": "/tmp/x.md"},
        "Protocol written → /tmp/x.md (took 1.2s)",
    ))
    passed.append(case(
        "route_tasks",
        "route_tasks",
        {"sid": "auto-tm-RT", "ilia": 2, "others": 1, "unknown": 0, "pending_deadline": 1, "errors": 0},
        "[route_tasks] meeting=auto-tm-RT ilia=2 others=1 unknown=0 pending_deadline=1 errors=0",
    ))

    # Misdelivery с correction между sent[i] и sent[i+1] — не помечаем.
    lines = [
        "[delivery] sent meeting=auto-X chat_id=-100 parts=1 message_ids=[1] elapsed=0.1s at=2026-05-25T10:00:00Z",
        "[correction] applied meeting=auto-X kind=targeted_remove version=v1",
        "[delivery] sent meeting=auto-X chat_id=-100 parts=1 message_ids=[2] elapsed=0.1s at=2026-05-25T10:01:00Z",
    ]
    events = observe.parse_log(lines)
    misd = observe.find_misdelivery(events)
    if misd:
        print(f"FAIL misdelivery_skips_correction: ожидался 0 cases, получено {len(misd)}: {misd}")
        passed.append(False)
    else:
        print("OK   misdelivery_skips_correction")
        passed.append(True)

    # Реальный дубль (без correction между) — помечаем.
    lines = [
        "[delivery] sent meeting=auto-Y chat_id=-100 parts=1 message_ids=[1] elapsed=0.1s at=2026-05-25T10:00:00Z",
        "[delivery] sent meeting=auto-Y chat_id=-100 parts=1 message_ids=[2] elapsed=0.1s at=2026-05-25T10:01:00Z",
    ]
    events = observe.parse_log(lines)
    misd = observe.find_misdelivery(events)
    if len(misd) != 1 or misd[0]["reason"] != "duplicate-send-same-chat":
        print(f"FAIL misdelivery_real_dup: {misd}")
        passed.append(False)
    else:
        print("OK   misdelivery_real_dup")
        passed.append(True)

    # Разные chat_id для одного meeting → multiple-chat-ids.
    lines = [
        "[delivery] sent meeting=auto-Z chat_id=-100 parts=1 message_ids=[1] elapsed=0.1s at=2026-05-25T10:00:00Z",
        "[delivery] sent meeting=auto-Z chat_id=-200 parts=1 message_ids=[2] elapsed=0.1s at=2026-05-25T10:01:00Z",
    ]
    events = observe.parse_log(lines)
    misd = observe.find_misdelivery(events)
    if len(misd) != 1 or misd[0]["reason"] != "multiple-chat-ids":
        print(f"FAIL misdelivery_multi_chat: {misd}")
        passed.append(False)
    else:
        print("OK   misdelivery_multi_chat")
        passed.append(True)

    total = len(passed)
    ok = sum(passed)
    print(f"\n{ok}/{total} PASSED")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
