#!/usr/bin/env python3
"""Unit-smoke `deliver_protocol` + correction-функций — без сети.

Monkey-patch'и `telegram_api.send_message`/`delete_message` чтобы:
  - не дёргать настоящий Telegram (smoke бежит в любом окне без VPN/токенов);
  - проверить идемпотентность доставки (повторный finalize не отправляет дубль);
  - проверить targeted_remove (удаление задачи N из секции «Задачи»);
  - проверить _save_protocol_version (запись `_versions/<date>-protokol-vN.md`);
  - проверить _classify_instruction (3 формы коррекции).

Реальный TG-smoke в группе живёт в `smoke_boevoy_delivery.py` (Ф6 шаг 10).
"""
from __future__ import annotations

import os
import sys
import tempfile
import json
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent.parent))

from notary.lib import llm_postprocess as L  # noqa: E402
from notary.lib import telegram_api  # noqa: E402


# ----- mock send/delete/document/render -----

_sent: list[tuple[int, str]] = []
_deleted: list[tuple[int, int]] = []
_sent_docs: list[tuple] = []          # (chat_id, file_path, caption)
_render_calls: list[tuple] = []       # (title, subtitle)


def _fake_send_message(token, chat_id, text, **kwargs):
    _sent.append((chat_id, text))
    return {"message_id": 1000 + len(_sent), "chat": {"id": chat_id}, "text": text[:50]}


def _fake_delete_message(token, chat_id, message_id):
    _deleted.append((chat_id, message_id))
    return True


def _fake_send_document(token, chat_id, file_path, *, caption=None, filename=None, **kwargs):
    _sent_docs.append((chat_id, file_path, caption))
    return {"message_id": 2000 + len(_sent_docs), "chat": {"id": chat_id}}


def _fake_render(md_text, out_pdf, *, title, subtitle, **kwargs):
    """Mock PDF-рендера: валидная заглушка `%PDF`, без chrome/markdown."""
    p = Path(out_pdf)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n" + b"x" * 64)
    _render_calls.append((title, subtitle))
    return p


def case_idempotent_skip(tmpdir: Path) -> bool:
    """RISK3: legacy-текстовая запись (N message_ids) → skip, PDF-дубль НЕ шлём."""
    _sent_docs.clear()
    _render_calls.clear()
    meta_json = tmpdir / "meta.json"
    meta_json.write_text(json.dumps({
        "sessionUid": "smoke-1",
        "delivered": {  # legacy object-формат, 2 текстовых чанка до деплоя
            "chat_id": -1001234567890,
            "message_ids": [100, 101],
            "at": "2026-05-28T20:00:00Z",
        },
    }), encoding="utf-8")

    result = L.deliver_protocol(
        meeting_meta={"series": "test-smoke", "date": "2026-05-28"},
        protocol_text="## Протокол\n\nтекст\n",
        meta_json_path=meta_json,
        target_chat_id=-1001234567890,
        meeting_sid="smoke-1",
    )
    if result.get("status") != "skipped":
        print(f"  [case idempotent] got status={result.get('status')}, expected skipped")
        return False
    if _sent_docs:
        print("  [case idempotent] был send_document — RISK3 нарушен (PDF-дубль)")
        return False
    if _render_calls:
        print("  [case idempotent] PDF собирался зря при skip")
        return False
    return True


def case_sent_pdf(tmpdir: Path) -> bool:
    """REQ 1.1/3.1/3.2: один PDF + caption; meta.delivered помечен document:true."""
    _sent_docs.clear()
    _render_calls.clear()
    meta_json = tmpdir / "meta_pdf.json"
    meta_json.write_text(json.dumps({}), encoding="utf-8")
    proto = (
        "#протоколвстречи 03.06.2026\n\n"
        "**Встреча:** Маркетплейсы — статус.\n\n"
        "---\n\n"
        "## 1) Раздел\n\n"
        "▪️ Буллет один.\n\n▪️ Буллет два.\n"
    )
    result = L.deliver_protocol(
        meeting_meta={
            "series": "marketplaces-tatiana", "date": "2026-06-03",
            "expectedParticipants": ["Илья Рыбалка", "Татьяна"], "participants": [],
            "recording": {"firstSpeechMs": 0, "lastSpeechMs": 97 * 60 * 1000},
        },
        protocol_text=proto,
        meta_json_path=meta_json,
        target_chat_id=-1001234567890,
        meeting_sid="smoke-pdf",
    )
    if result.get("status") != "sent":
        print(f"  [case sent-pdf] status={result.get('status')}")
        return False
    if result.get("parts_count") != 1 or not result.get("document"):
        print(f"  [case sent-pdf] ожидали parts_count=1 + document=true, got {result}")
        return False
    if len(_sent_docs) != 1:
        print(f"  [case sent-pdf] документов отправлено: {len(_sent_docs)} (ожидали 1)")
        return False
    if len(_sent) != 0:
        print("  [case sent-pdf] был send_message — тело текстом дублируется (REQ 3.2)")
        return False
    # Caption: 4 строки, слитный хэштег, человекочитаемая серия (REQ 3.1).
    _, _, caption = _sent_docs[0]
    if caption is None or caption.splitlines()[0] != "📋 #протоколвстречи":
        print(f"  [case sent-pdf] caption 1-я строка неверна: {caption!r}")
        return False
    if "Маркетплейсы (Татьяна) — 03.06.2026" not in caption:
        print(f"  [case sent-pdf] нет человекочитаемой серии в caption: {caption!r}")
        return False
    # meta.delivered: array + document:true (RISK1).
    data = json.loads(meta_json.read_text(encoding="utf-8"))
    records = data.get("delivered")
    records = records if isinstance(records, list) else [records]
    matched = next((r for r in reversed(records)
                    if isinstance(r, dict) and r.get("chat_id") == -1001234567890), None)
    if matched is None or matched.get("message_ids") != [2001] or not matched.get("document"):
        print(f"  [case sent-pdf] meta.delivered неверен: {records}")
        return False
    return True


def case_pdf_failure_alert(tmpdir: Path) -> bool:
    """REQ 1.4: сбой сборки PDF → status error + алерт Илье; текстом НЕ слать."""
    _sent_docs.clear()
    _sent.clear()
    alerts: list = []
    meta_json = tmpdir / "meta_fail.json"
    meta_json.write_text(json.dumps({}), encoding="utf-8")

    orig_render = L.protocol_to_pdf.render_pdf_from_markdown
    orig_alert = L._alert_owner_pdf_failure

    def _boom(*a, **k):
        raise L.protocol_to_pdf.PdfRenderError("smoke: chromium boom")

    def _capture_alert(meta, chat_id, sid, err):
        alerts.append((chat_id, type(err).__name__))

    L.protocol_to_pdf.render_pdf_from_markdown = _boom
    L._alert_owner_pdf_failure = _capture_alert
    try:
        result = L.deliver_protocol(
            meeting_meta={"series": "test-smoke", "date": "2026-06-03"},
            protocol_text="#протоколвстречи 03.06.2026\n\n## 1) Раздел\n\n▪️ x\n",
            meta_json_path=meta_json,
            target_chat_id=-1001234567890,
            meeting_sid="smoke-fail",
        )
    finally:
        L.protocol_to_pdf.render_pdf_from_markdown = orig_render
        L._alert_owner_pdf_failure = orig_alert

    if result.get("status") != "error":
        print(f"  [case fail-alert] status={result.get('status')}, expected error")
        return False
    if len(alerts) != 1:
        print(f"  [case fail-alert] алерт Илье не отправлен: {alerts}")
        return False
    if _sent_docs or _sent:
        print("  [case fail-alert] что-то отправлено при сбое — текстом слать НЕЛЬЗЯ")
        return False
    data = json.loads(meta_json.read_text(encoding="utf-8"))
    if data.get("delivered"):
        print("  [case fail-alert] meta.delivered записан при сбое — блокирует ретрай")
        return False
    return True


def case_disabled_gate(tmpdir: Path) -> bool:
    _sent.clear()
    os.environ["ENABLE_PROTOCOL_DELIVERY"] = "0"
    try:
        result = L.deliver_protocol(
            meeting_meta={"series": "x", "date": "2026-05-28"},
            protocol_text="что-то",
            meta_json_path=None,
            target_chat_id=42,
            meeting_sid="smoke-disabled",
        )
    finally:
        del os.environ["ENABLE_PROTOCOL_DELIVERY"]
    if result.get("status") != "disabled":
        print(f"  [case disabled] status={result.get('status')}")
        return False
    if _sent:
        print("  [case disabled] был вызов send_message при OFF")
        return False
    return True


def case_targeted_remove() -> bool:
    proto = (
        "#протоколвстречи 28.05.2026\n\n**Встреча:** test\n\n"
        "---\n\n"
        "## 🟠 Задачи\n\n"
        "- **Задача один** контекст: что-то\n\n"
        "- **Задача два** контекст: ещё\n\n"
        "- **Задача три** последняя\n\n"
        "## 🟢 Решения\n\n"
        "решение\n"
    )
    new = L._apply_targeted_remove(proto, 2)
    if new is None:
        print("  [targeted] вернул None")
        return False
    if "Задача два" in new:
        print("  [targeted] задача 2 не удалена")
        return False
    if "Задача один" not in new or "Задача три" not in new:
        print("  [targeted] потеряли соседнюю задачу")
        return False
    if "## 🟠 Задачи" not in new or "## 🟢 Решения" not in new:
        print("  [targeted] секции потерялись")
        return False
    # Удаление N-вне-диапазона → None.
    if L._apply_targeted_remove(proto, 99) is not None:
        print("  [targeted] N=99 должен дать None")
        return False
    return True


def case_classify_instruction() -> bool:
    kind, n = L._classify_instruction("удали задачу 3 из sales-quality 2026-05-27")
    if kind != "targeted_remove" or n != 3:
        print(f"  [classify] ожидали targeted_remove/3, got {kind}/{n}")
        return False
    kind, n = L._classify_instruction("задачу 5 не было — переделай блок задач")
    if kind != "targeted_remove" or n != 5:
        print(f"  [classify] tail-negate должно дать targeted_remove/5, got {kind}/{n}")
        return False
    kind, n = L._classify_instruction("переделай блок задач полностью")
    if kind != "structural":
        print(f"  [classify] structural ожидался, got {kind}")
        return False
    return True


def case_save_version(tmpdir: Path) -> bool:
    proto_path = tmpdir / "2026-05-28-protokol.md"
    proto_path.write_text("первая версия\n", encoding="utf-8")
    v1 = L._save_protocol_version(proto_path)
    if v1 is None or not v1.is_file():
        print(f"  [version] v1 не создан: {v1}")
        return False
    if v1.name != "2026-05-28-protokol-v1.md":
        print(f"  [version] неверное имя: {v1.name}")
        return False
    # вторая версия
    proto_path.write_text("вторая версия\n", encoding="utf-8")
    v2 = L._save_protocol_version(proto_path)
    if v2 is None or v2.name != "2026-05-28-protokol-v2.md":
        print(f"  [version] v2: {v2}")
        return False
    return True


def main() -> int:
    # Monkey-patch: сеть и chrome не дёргаем (PDF-путь Ф2).
    telegram_api.send_message = _fake_send_message
    telegram_api.delete_message = _fake_delete_message
    telegram_api.send_document = _fake_send_document
    L.protocol_to_pdf.render_pdf_from_markdown = _fake_render
    # Bot-token: пустой в smoke-окружении, делаем фикстуру чтобы deliver_protocol
    # не early-return'нул на check'е токена. Реальный bot API мокаем выше.
    os.environ.setdefault("TELEGRAM_NOTARIUS_BOT_TOKEN", "FAKE-SMOKE-TOKEN")
    os.environ.setdefault("TELEGRAM_NOTARIUS_CHAT_ID", "359008340")

    failed = 0
    with tempfile.TemporaryDirectory(prefix="smoke-deliver-") as td_str:
        td = Path(td_str)
        cases = [
            ("idempotent skip (RISK3 legacy→skip)", lambda: case_idempotent_skip(td)),
            ("sent pdf + caption", lambda: case_sent_pdf(td)),
            ("pdf failure → alert, no text", lambda: case_pdf_failure_alert(td)),
            ("disabled gate", lambda: case_disabled_gate(td)),
            ("targeted_remove", case_targeted_remove),
            ("classify_instruction", case_classify_instruction),
            ("save_version", lambda: case_save_version(td)),
        ]
        for name, fn in cases:
            try:
                ok = fn()
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ {name}: исключение {type(e).__name__}: {e}")
                import traceback; traceback.print_exc()
                failed += 1
                continue
            if ok:
                print(f"  ✅ {name}")
            else:
                print(f"  ❌ {name}")
                failed += 1
    total = 7
    if failed:
        print(f"\nРЕЗУЛЬТАТ: FAIL ({failed}/{total} fail)")
        return 1
    print(f"\nРЕЗУЛЬТАТ: PASS ({total}/{total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
