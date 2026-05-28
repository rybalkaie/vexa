#!/usr/bin/env python3
"""Реальный smoke 1 для Ф3 clarify (требует Telegram + Илью у телефона).

Создаёт синтетический `_pending_clarification/<sid>.json`, шлёт сообщение
в `@ilya_protocol_meeting_bot`, ждёт пока Илья нажмёт inline-кнопку.
Listener (PID 1811997+) подхватит callback, применит mapping, перезапишет
тестовый транскрипт, обновит state.

Использование (на VPS):
  set -a; source /srv/meeting-notary/.env.notary; set +a
  /srv/meeting-notary/venv-cli/bin/python /srv/meeting-notary/vexa/scripts/notary/tools/smoke_clarify_real_callback.py

  С таймаутом 60s для smoke 2:
  CLARIFY_TIMEOUT=60 ... smoke_clarify_real_callback.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

THIS = Path(__file__).resolve()
sys.path.insert(0, str(THIS.parent.parent.parent))  # vexa/scripts/

from notary.lib import clarify_state, telegram_api, llm_postprocess  # noqa: E402


def main() -> int:
    token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    chat_raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID") or ""
    ).strip()
    if not token or not chat_raw:
        print("ERROR: TELEGRAM_NOTARIUS_BOT_TOKEN / TELEGRAM_NOTARIUS_CHAT_ID не заданы")
        return 2
    chat_id = int(chat_raw)
    timeout_s = int(os.environ.get("CLARIFY_TIMEOUT", "420"))

    # Создаём тестовый транскрипт во временном месте, чтобы воркер мог его обновить.
    tmpdir = Path(tempfile.mkdtemp(prefix="clarify-smoke-"))
    transcript_path = tmpdir / "2026-05-28.md"
    transcript_path.write_text(
        "# Test transcript for clarify smoke\n\n"
        "**[00:05] Спикер 1:** короткая реплика тестовая первая\n\n"
        "**[00:12] Спикер 2:** это вторая реплика, чуть длиннее чем первая\n\n"
        "**[00:18] Спикер 1:** ещё раз спикер один\n",
        encoding="utf-8",
    )
    print(f"[smoke] transcript: {transcript_path}")

    sid = f"smoke-{int(time.time())}"
    name_pool = ["Илья Рыбалка", "Михаил Саргин", "Дарья Набережная"]
    cluster_key = "SPEAKER_01"
    unclear = {
        cluster_key: {
            "name_options": list(name_pool),
            "samples": [
                "[00:12] «это вторая реплика, чуть длиннее чем первая»",
            ],
            "speaker_label_in_md": "Спикер 2",
            "confidence": 0.62,
            "current_guess": "Михаил Саргин",
        }
    }

    text = llm_postprocess._build_clarify_message_text(
        "test-clarify-smoke", "2026-05-28", unclear,
    )
    keyboard = llm_postprocess._build_clarify_inline_keyboard(
        sid, [cluster_key], unclear,
    )
    print(f"[smoke] sending to chat={chat_id} len(text)={len(text)}")
    try:
        result = telegram_api.send_message(token, chat_id, text, reply_markup=keyboard)
    except telegram_api.TelegramApiError as e:
        print(f"ERROR: sendMessage failed: {e}")
        return 1
    message_id = int(result.get("message_id") or 0)
    sent_at = datetime.now(timezone.utc)
    deadline = sent_at + timedelta(seconds=timeout_s)

    state = {
        "meeting_id": sid,
        "transcript_path": str(transcript_path),
        "meta": {
            "series": "test-clarify-smoke",
            "date": "2026-05-28",
            "sessionUid": sid,
        },
        "unclear_clusters": unclear,
        "cluster_keys_ordered": [cluster_key],
        "name_pool": name_pool,
        "chat_id": chat_id,
        "message_id": message_id,
        "sent_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeout_s": timeout_s,
        "deadline_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "pending",
        "resolved_via": None,
        "resolved_at": None,
        "applied_mapping": None,
    }
    state_path = clarify_state.write_state(state)
    print(f"[smoke] state written: {state_path}")
    print(f"[smoke] meeting_id: {sid}")
    print(f"[smoke] deadline: {deadline.isoformat()} (timeout={timeout_s}s)")
    print(f"[smoke] waiting for Илья to press button... (ctrl-c to stop watching)")
    print(f"[smoke] watch with: ssh meeting-notary 'sudo journalctl -u meeting-notary-listener -f'")
    print(f"[smoke] state path: {state_path}")
    print(f"[smoke] transcript path: {transcript_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
