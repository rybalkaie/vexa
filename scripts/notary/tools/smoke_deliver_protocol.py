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


# ----- mock send/delete -----

_sent: list[tuple[int, str]] = []
_deleted: list[tuple[int, int]] = []


def _fake_send_message(token, chat_id, text, **kwargs):
    _sent.append((chat_id, text))
    return {"message_id": 1000 + len(_sent), "chat": {"id": chat_id}, "text": text[:50]}


def _fake_delete_message(token, chat_id, message_id):
    _deleted.append((chat_id, message_id))
    return True


def case_idempotent_skip(tmpdir: Path) -> bool:
    _sent.clear()
    meta_json = tmpdir / "meta.json"
    meta_json.write_text(json.dumps({
        "sessionUid": "smoke-1",
        "delivered": {
            "chat_id": -1001234567890,
            "message_ids": [100, 101],
            "at": "2026-05-28T20:00:00Z",
        },
    }), encoding="utf-8")

    proto = "## Протокол\n\nдлинный текст " * 10  # < лимита → одна часть
    # Заметим: ожидаем 1 часть → меняем delivered.message_ids на [100] до вызова.
    data = json.loads(meta_json.read_text(encoding="utf-8"))
    data["delivered"]["message_ids"] = [100]
    meta_json.write_text(json.dumps(data), encoding="utf-8")

    result = L.deliver_protocol(
        meeting_meta={"series": "test-smoke", "date": "2026-05-28"},
        protocol_text=proto,
        meta_json_path=meta_json,
        target_chat_id=-1001234567890,
        meeting_sid="smoke-1",
    )
    if result.get("status") != "skipped":
        print(f"  [case idempotent] got status={result.get('status')}, expected skipped")
        return False
    if _sent:
        print("  [case idempotent] был вызов send_message — баг идемпотентности")
        return False
    return True


def case_sent_with_split(tmpdir: Path) -> bool:
    _sent.clear()
    meta_json = tmpdir / "meta_split.json"
    meta_json.write_text(json.dumps({}), encoding="utf-8")
    # Ф1-доработки 29.05: split идёт `split_protocol_smart` на сформированном
    # TG-тексте (max_len=4096). Сырой протокол со множеством секций — чтобы
    # текст после `format_protocol_as_tg_text` гарантированно превысил лимит.
    # Каждая секция = шапка + 30 длинных буллетов; 4 секции = ~6000+ символов.
    sections = []
    for i in range(1, 5):
        bullets = "\n\n".join(
            f"▪️ Длинный буллет №{j} в секции {i} с подробностями про разные аспекты "
            f"и контекст обсуждения участников встречи."
            for j in range(1, 31)
        )
        sections.append(f"## {i}) Тема номер {i}\n\n{bullets}\n")
    proto = (
        "#протоколвстречи 29.05.2026\n\n"
        "**Встреча:** Тест разбиения на части.\n\n"
        "**Длительность:** 30 мин\n\n"
        "**Участники:** Илья Рыбалка\n\n"
        "---\n\n" + "\n---\n\n".join(sections)
    )
    result = L.deliver_protocol(
        meeting_meta={"series": "test-smoke", "date": "2026-05-28"},
        protocol_text=proto,
        meta_json_path=meta_json,
        target_chat_id=-1001234567890,
        meeting_sid="smoke-split",
    )
    if result.get("status") != "sent":
        print(f"  [case sent] status={result.get('status')}")
        return False
    if result.get("parts_count") < 2:
        print(f"  [case sent] split не сработал, parts={result.get('parts_count')}")
        return False
    if len(_sent) != result.get("parts_count"):
        print(f"  [case sent] sent={len(_sent)} != parts={result.get('parts_count')}")
        return False
    # Проверка: meta.delivered обновлён (Ф1-доработки 29.05 — теперь array,
    # не object; ищем запись для нужного chat_id).
    data = json.loads(meta_json.read_text(encoding="utf-8"))
    raw_delivered = data.get("delivered")
    if isinstance(raw_delivered, dict):
        records = [raw_delivered]
    elif isinstance(raw_delivered, list):
        records = [r for r in raw_delivered if isinstance(r, dict)]
    else:
        records = []
    matched = None
    for rec in reversed(records):
        if rec.get("chat_id") == -1001234567890:
            matched = rec
            break
    if matched is None:
        print(f"  [case sent] meta.delivered не содержит chat_id=-1001234567890: {records}")
        return False
    if len(matched.get("message_ids") or []) != result.get("parts_count"):
        print(f"  [case sent] message_ids count != parts")
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
    # Monkey-patch.
    telegram_api.send_message = _fake_send_message
    telegram_api.delete_message = _fake_delete_message
    # Bot-token: пустой в smoke-окружении, делаем фикстуру чтобы deliver_protocol
    # не early-return'нул на check'е токена. Реальный bot API мокаем выше.
    os.environ.setdefault("TELEGRAM_NOTARIUS_BOT_TOKEN", "FAKE-SMOKE-TOKEN")
    os.environ.setdefault("TELEGRAM_NOTARIUS_CHAT_ID", "359008340")

    failed = 0
    with tempfile.TemporaryDirectory(prefix="smoke-deliver-") as td_str:
        td = Path(td_str)
        cases = [
            ("idempotent skip", lambda: case_idempotent_skip(td)),
            ("sent with split", lambda: case_sent_with_split(td)),
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
    if failed:
        print(f"\nРЕЗУЛЬТАТ: FAIL ({failed} fail)")
        return 1
    print("\nРЕЗУЛЬТАТ: PASS (6/6)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
