#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ф6 (план `2026-07-16-open-questions-reply-fix.md`, REQ 4.1/4.2): РАЗОВОЕ
переприменение УЖЕ НАПИСАННОГО ответа участника на утренний дайджест «Открытые
вопросы к встрече».

Зачем: сотрудник (реальный кейс — Михаил Еремеев, серия `anzhee-direktorat`,
2026-07-15) ответил ОДНИМ сообщением статусами по ~25 задачам. До фикса Ф1–Ф4 бот
это не разбирал. Инструмент прогоняет ЕГО ИСХОДНЫЙ текст через ТОТ ЖЕ путь, что и
живой digest-роут — участник ничего не переписывает.

НЕ СВОЙ ПАРСЕР. Инструмент зовёт ровно те же либ-примитивы, что
`meetings_listener._apply_digest_status_batch` (батч-путь Ф2):
  • `pending_status_reply.detect_status_intent`  — есть ли вообще статус-маркер (Ф1);
  • `mixed_reply_split.split_reply`              — LLM-сплит пачки на сегменты (Ф2);
  • `mixed_reply_split.run_status_loop`          — общий status-loop (Ф2, KODE1);
  • `pending_status_reply.apply_status_reply`    — финальный статус (Ф1) + ординал (Ф4);
  • `mixed_reply_split.build_mixed_ack`          — тот же структурный ack (Ф2, для превью).
Логика оркестрации — зеркало `_apply_digest_status_batch` (см. там же), чтобы поведение
инструмента совпадало с продом.

OWNER-IN-LOOP (REQ 4.2). По умолчанию — DRY-RUN: печатает «задача → статус» и НИЧЕГО
не пишет. Реальная запись в sidecar (Ф2 `series_memory.set_task_status`) — только по
явному `--apply`. В ГРУППОВОЙ ЧАТ не постит НИКОГДА (решение Q3): единственный писатель
статуса — sidecar; ack показывается владельцу для сверки, но в Telegram-группу не уходит.

┌─ OWNER-ЧЕК-ЛИСТ ЗАПУСКА (руками, ПОСЛЕ деплоя Ф1–Ф4 на прод) ────────────────────┐
│ 1. Дать текст сообщения участника — файлом:                                       │
│      reapply-digest-reply.py <серия> <дата> --text-file /путь/к/ответу.txt        │
│    (или `--text "..."`, или подать на stdin).                                     │
│ 2. Прогнать DRY-RUN (это дефолт — без --apply запись НЕ идёт), сверить список      │
│    «задача → статус»: те ли задачи, те ли статусы.                                 │
│ 3. Если всё верно — повторить с `--apply` (тогда статусы записываются в sidecar).  │
│ 4. Следующий протокол/дайджест серии учтёт проставленные статусы автоматически.    │
│                                                                                    │
│ ВАЖНО: запись имеет смысл ТОЛЬКО против ПРОД-sidecar и ТОЛЬКО после того, как фикс  │
│ Ф1–Ф4 задеплоен (иначе живой бот всё равно разберёт пачку по-старому). Деплой      │
│ meeting-notary — ручной шаг владельца, не этого инструмента.                       │
│                                                                                    │
│ Ординал («первая — сделана»): резолвится против СЛЕПКА нумерации отвечаемого        │
│ документа. Слепок появился только с Ф3, поэтому для СТАРЫХ сообщений (до деплоя)   │
│ слепка нет → ординал недоступен, матчинг идёт по НАЗВАНИЮ задачи (overlap) — это    │
│ норма и именно так же ведёт себя живой бот. Если знаете message_id отвечаемого     │
│ документа и слепок по нему есть — передайте `--reply-to-message-id N`.             │
└────────────────────────────────────────────────────────────────────────────────┘

ГДЕ ЗАПУСКАТЬ (важно, иначе запись «тихо не сработает»). Запускать на ТОМ хосте, где
живёт живой бот (`listener`) и его sidecar — проект VPS-only с 27.05.2026, значит на VPS
(`ssh meeting-notary`, деплой-копия инструмента `/srv/meeting-notary/vexa/scripts/notary`),
причём с ТЕМ ЖЕ корнем папки встреч, что читает listener — переменная
`MEETING_NOTARY_PROTOCOLS_DIR` (или флаг `--root <та же папка>`). На маке `--apply` НЕ
запускать: мак держит лишь read-only ЗЕРКАЛО папки встреч (агент `mirror` копирует
VPS→мак, односторонне) — запись на маке уйдёт в зеркало, которого listener не читает, и
статусы до бота НЕ доедут. Dry-run (превью) можно где угодно; запись — только там, где
sidecar читает бот. Точные пути/корни — `docs/how-it-works.md`, раздел «Где всё крутится».

ОПАСНАЯ ТРОЙКА (CLAUDE.md проекта — PII встреч, план п.9). Сырой текст ответа и любые
финансовые цифры из него: НЕ логируем, НЕ пишем в файлы, НЕ возвращаем наружу. В stdout
идёт результат МАППИНГА — РАСПОЗНАННЫЕ ботом названия задач (из канонического списка
висяков серии, не эхо участника) и их статусы. ЕДИНСТВЕННОЕ исключение — ack-превью «для
сверки» может эхом показать ОДИН короткий необработанный фрагмент ответа (`unclear`,
обрезан до `_MAX_UNCLEAR_LEN`) — ровно как живой бот эхит его в групповой чат; это
owner-facing превью, оно НЕ логируется и НЕ пишется в файлы/память. Поэтому НЕ
перенаправляй превью в общий лог/файл (`> out.txt`). Сам сплит-запрос уходит в Claude — это принятая владельцем опасная тройка
(маппинг имён/сегментов), тот же канал, что у живого бота.

stdlib-only, системный/venv python3.9. Импорт НЕ включает сплиттер (флаг
`ENABLE_MIXED_REPLY_SPLIT` — только в `main()`) и НЕ ходит в сеть → тесты грузят модуль
офлайн. Таймаут сплиттера поднят на импорте (benign env-дефолт для больших пачек — реальный
split ~25 задач идёт ~минуту, дефолт mixed_reply_split 30с их не успевает; влияет лишь на
длительность реального claude-вызова, не на демон/тесты).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent
SCRIPTS_DIR = NOTARY_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

# Таймаут LLM-сплиттера читается ДИНАМИЧЕСКИ (`mixed_reply_split._split_timeout()`, дефолт
# 120с, R3/FU-8). Реальная пачка ~25 задач расщепляется дольше (замер — ~57с) и инструмент
# гоняют вручную на самых больших пачках, поэтому для процесса ИНСТРУМЕНТА поднимаем ещё
# выше — 240с (setdefault — владелец может переопределить своим env). Динамическое чтение
# делает `setdefault` независимым от порядка импорта.
os.environ.setdefault("MIXED_REPLY_SPLIT_TIMEOUT", "240")

from notary.lib import mixed_reply_split as _mrs  # noqa: E402
from notary.lib import pending_status_reply as _psr  # noqa: E402
from notary.lib import series_memory as _sm  # noqa: E402
import notary.meetings_listener as _ml  # noqa: E402

logger = logging.getLogger("notary.reapply_digest_reply")

# Заглушки: инструмент НИКОГДА не ходит в сеть Telegram — send_message перехвачен,
# а сюда передаём заведомо нерабочий токен/чат как дополнительную страховку.
_DUMMY_TOKEN = "REAPPLY-DRY-RUN-NO-NETWORK"
_DUMMY_CHAT = 0

# Код статуса sidecar → человекочитаемый ярлык превью (owner-facing).
_STATUS_PREVIEW_LABEL = {
    _sm.STATUS_OPEN: "висит (открыт / жду)",
    _sm.STATUS_DONE: "закрыто",
    _sm.STATUS_CANCELLED: "снято (неактуально)",
    _sm.STATUS_DOUBT: "под вопросом",
    _sm.STATUS_AUTO_CLOSED: "закрыто (авто)",
}


def _date_to_msg_ts(date: str) -> Optional[int]:
    """`YYYY-MM-DD` → unix-ts локальной полуночи. Кладём в `msg["date"]`, чтобы
    `_reply_date_str(msg)` вернул РОВНО эту дату в поле `updated` sidecar — дату
    ОТВЕТА участника (напр. 2026-07-15), а не «сегодня». Мусор → None (дата
    метаданная, статус проставится и без неё)."""
    try:
        return int(time.mktime(time.strptime(date, "%Y-%m-%d")))
    except (ValueError, OverflowError):
        return None


def _run_digest_path(
    series_dir: Path, date: Optional[str], text: str, snapshot: Optional[list],
) -> dict:
    """Зеркало ТЕКСТ-пути `maybe_route_to_digest_reply` + `_apply_digest_status_batch`
    (meetings_listener.py) на либ-примитивах. Возвращает структуру превью. НЕ пишет и
    НЕ постит сам — запись перехватывается снаружи (`set_task_status`), пост
    (`send_message`) тоже. Опасная тройка: тексты не логируем — только счётчики.

    Ключи результата:
      no_markers    — в тексте нет статус-маркера (Ф1) → бот промолчал бы, применять нечего;
      unparsed_batch— похоже на пачку, но сплит не удался (UPU1) → честная подсказка;
      engaged_batch — сработал батч-путь Ф2 (≥2 статуса / статус+правки/непонятое);
      single        — пошёл одиночный fast-path (REQ 2.3);
      single_res    — результат одиночного apply (dict|None);
      n_ok/n_unresolved/groups/n_raw/overflow/edits/unclear — данные батча для ack/сводки.
    """
    out: dict[str, Any] = {
        "no_markers": False, "unparsed_batch": False, "engaged_batch": False,
        "single": False, "single_res": None,
        "n_ok": 0, "n_unresolved": 0, "groups": [], "n_raw": 0,
        "overflow": None, "edits": 0, "unclear": "",
    }
    # Ф1: нет статус-маркера → это не попытка учёта (реакция «ок»/«спасибо»/вопрос).
    if _psr.detect_status_intent(text) is None:
        out["no_markers"] = True
        return out
    # Ф2 (батч, _apply_digest_status_batch): пачка статусов под флагом
    # ENABLE_MIXED_REPLY_SPLIT. Одиночный статус сюда не заходит (fast-path).
    if _mrs.is_enabled() and _mrs.looks_like_multi_status(text):
        items = _psr.load_series_pending(series_dir)
        try:
            split = _mrs.split_reply(text, items)
        except Exception as e:  # noqa: BLE001 — сбой границы LLM не роняет прогон
            logger.warning("[reapply] сплит пачки упал (non-fatal): %s", type(e).__name__)
            split = None
        if split is None:
            out["unparsed_batch"] = True  # UPU1: похоже на пачку, но не разобрали
            return out
        if _mrs.should_engage(split):
            out["engaged_batch"] = True
            status_segments = split["status_segments"]
            n_raw = split.get("n_status_raw") or len(status_segments)

            def _apply_one(seg):
                return _psr.apply_status_reply(
                    seg, series_dir, date=date, source="digest-reply", snapshot=snapshot,
                )

            loop = _mrs.run_status_loop(status_segments, _apply_one)
            out["n_ok"] = loop["n_ok"]
            out["n_unresolved"] = loop["n_unresolved"]
            out["groups"] = loop["groups"]
            out["n_raw"] = n_raw
            out["edits"] = len(split.get("edits") or [])
            out["unclear"] = split.get("unclear") or ""
            # REQ 3.2: объём превышен (сплиттер вернул больше, чем cap) → «N из M».
            if n_raw > len(status_segments):
                out["overflow"] = (len(status_segments), n_raw)
            return out
    # Одиночный fast-path (REQ 2.3): флаг OFF / не похоже на пачку / сплит не смешанный.
    out["single"] = True
    out["single_res"] = _psr.apply_status_reply(
        text, series_dir, date=date, source="digest-reply", snapshot=snapshot,
    )
    return out


def reapply(
    series: str,
    date: str,
    text: str,
    *,
    root: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    apply: bool = False,
) -> dict:
    """Прогнать `text` через живой digest-путь и собрать «задача → статус».

    apply=False (ДЕФОЛТ) — dry-run: `series_memory.set_task_status` перехвачен, в
    sidecar НИЧЕГО не пишется. apply=True — перехватчик делегирует РЕАЛЬНОМУ писателю
    (идемпотентно по ключу). В обоих режимах `send_message` перехвачен → в группу не
    уходит ничего (Q3). Возвращает превью-структуру (без сырого текста — опасная тройка).

    `root` — корень папки встреч (override `MEETING_NOTARY_PROTOCOLS_DIR`); None → дефолт
    (`~/Projects/me/встречи`). `reply_to_message_id` — message_id отвечаемого документа
    для резолва ординала против его слепка (Ф3/Ф4); None → слепка нет → ординал
    недоступен, матчинг по названию (overlap), как у живого бота на старых сообщениях.

    Возврат (только результат МАППИНГА, НЕ сырой текст):
      applied      — list[(recognized_task_key, status_code, reason)] дедуп по ключу;
      ack          — структурный ответ бота (в группу НЕ уходит; для сверки владельцем);
      batch        — сработал ли батч-путь Ф2;
      no_status_markers — в тексте не было статус-маркера;
      unparsed_batch    — похоже на пачку, но не разобрали (UPU1);
      unresolved   — сколько сегментов не привязались к висяку (честный остаток);
      overflow     — (n_done, n_total) при превышении объёма или None;
      snapshot_used— резолвился ли ординал против слепка;
      wrote        — реально ли писали (apply=True).
    """
    prev_root_env = os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR")
    if root:
        os.environ["MEETING_NOTARY_PROTOCOLS_DIR"] = str(Path(root).expanduser())

    msg: dict[str, Any] = {"text": text, "message_id": 1}
    ts = _date_to_msg_ts(date)
    if ts is not None:
        msg["date"] = ts
    if reply_to_message_id is not None:
        msg["reply_to_message"] = {"message_id": reply_to_message_id}

    captured: list[tuple[str, str, Any]] = []  # (task_text, status, reason)
    acks: list[str] = []
    orig_set = _sm.set_task_status
    orig_send = _ml.send_message

    def _cap_set(series_dir_arg, task_text, status, *, reason=None, source=None, date=None):
        # Единственная граница записи sidecar. dry-run — не пишем, возвращаем
        # синтетический truthy-rec той же формы (apply_status_reply проверяет лишь
        # `rec is None`). apply — делегируем настоящему писателю (идемпотентно).
        captured.append((task_text, status, reason))
        if apply:
            return orig_set(
                series_dir_arg, task_text, status,
                reason=reason, source=source, date=date,
            )
        return {"status": status, "reason": reason, "source": source,
                "shown": False, "text": (task_text or "").strip(), "updated": date}

    def _cap_send(token, chat_id, text_, *, reply_to=None, html=False):
        # Q3: в групповой чат ничего не постим. Ack собираем как превью владельцу.
        acks.append(text_)

    try:
        series_dir = _ml._protokol_root() / series
        snapshot = _ml._reply_open_tasks_snapshot(series_dir, msg)
        _sm.set_task_status = _cap_set
        _ml.send_message = _cap_send
        try:
            date_field = _ml._reply_date_str(msg)
            res = _run_digest_path(series_dir, date_field, text, snapshot)
            # Ack для сверки (тот же вызов, что делает живой роут) — в группу НЕ уходит.
            if res["engaged_batch"]:
                acks.append(_mrs.build_mixed_ack(
                    status_groups=res["groups"], n_status_unresolved=res["n_unresolved"],
                    n_edits=res["edits"], edit_queued=False, edit_as_hint=True,
                    unclear=res["unclear"], overflow=res["overflow"],
                ))
            elif res["unparsed_batch"]:
                acks.append(_ml.DIGEST_BATCH_UNPARSED_HINT)
            elif res["single"] and res["single_res"]:
                acks.append(res["single_res"].get("ack") or "")
            elif res["single"]:
                acks.append(_ml.DIGEST_UNPARSED_HINT)
        finally:
            _sm.set_task_status = orig_set
            _ml.send_message = orig_send
    finally:
        if root:
            if prev_root_env is None:
                os.environ.pop("MEETING_NOTARY_PROTOCOLS_DIR", None)
            else:
                os.environ["MEETING_NOTARY_PROTOCOLS_DIR"] = prev_root_env

    # Дедуп по ключу sidecar (идемпотентность: последний статус на ключ побеждает —
    # ровно как в store). Порядок первого появления сохраняем.
    applied: list[tuple[str, str, Any]] = []
    idx: dict[str, int] = {}
    for task_text, status, reason in captured:
        key = _sm._status_key(task_text)
        if not key:
            continue
        if key in idx:
            applied[idx[key]] = (task_text, status, reason)
        else:
            idx[key] = len(applied)
            applied.append((task_text, status, reason))

    return {
        "applied": applied,
        "ack": "\n".join(a for a in acks if a),
        "batch": res["engaged_batch"],
        "no_status_markers": res["no_markers"],
        "unparsed_batch": res["unparsed_batch"],
        "unresolved": res["n_unresolved"],
        "overflow": res["overflow"],
        "snapshot_used": snapshot is not None,
        "wrote": bool(apply),
    }


def _read_text(args) -> str:
    """Текст ответа участника: `--text-file` → `--text` → stdin. Опасная тройка:
    ничего из прочитанного не логируем."""
    if args.text_file:
        return Path(args.text_file).expanduser().read_text(encoding="utf-8")
    if args.text is not None:
        return args.text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit(
        "Не задан текст ответа. Укажите --text-file <путь>, --text \"...\" "
        "или подайте на stdin."
    )


def _print_preview(series: str, date: str, text_len: int, res: dict, *, apply: bool) -> None:
    """Человекочитаемое превью владельцу. Опасная тройка: печатаем ТОЛЬКО результат
    маппинга (распознанные названия задач + статусы) и счётчики — НЕ сырой текст ответа.
    Также машинная строка `SUMMARY:` (чистые счётчики, без текста) для проверки прогона.
    """
    mode = "ЗАПИСЬ В SIDECAR" if apply else "DRY-RUN (запись НЕ выполняется)"
    print("=== Переприменение ответа на дайджест «Открытые вопросы» ===")
    print(f"Серия: {series}   Дата ответа: {date}   Длина сообщения: {text_len} симв.")
    print(f"Режим: {mode}")
    print(f"Флаг ENABLE_MIXED_REPLY_SPLIT: {'ON' if _mrs.is_enabled() else 'OFF'}")
    print(
        "Слепок нумерации отвечаемого документа: "
        + ("используется (ординал активен)" if res["snapshot_used"]
           else "недоступен → матчинг по названию (overlap)")
    )
    print()

    applied = res["applied"]
    if res["no_status_markers"]:
        print("В сообщении не найдено статус-маркеров — применять нечего "
              "(бот в этом случае молчит).")
    elif res["unparsed_batch"]:
        print("⚠️ Похоже на пачку статусов, но надёжно разобрать не удалось (UPU1). "
              "Ничего не применено. Живой бот в этом случае просит прислать по одной "
              "задаче на строку.")
    elif not applied:
        print("Ни один статус не привязался к висяку серии — применять нечего "
              "(честный остаток; см. ответ бота ниже).")
    else:
        verb = "Записал" if apply else "Применю"
        print(f"{verb} (распознанная задача → статус):")
        for i, (task_text, status, reason) in enumerate(applied, 1):
            name = _mrs._short_task_name(task_text)
            label = _STATUS_PREVIEW_LABEL.get(status, status)
            suffix = f" — {reason}" if reason and status != _sm.STATUS_OPEN else ""
            print(f"  {i:>2}. {name} → {label}{suffix}")

    counts = Counter(status for _t, status, _r in applied)
    n_recognized = len(applied)
    if res["overflow"]:
        n_done, n_total = res["overflow"]
        print(f"\n📦 Объём превышен: обработано {n_done} из {n_total} — "
              "остальное пришлите вторым сообщением.")
    if res["unresolved"]:
        print(f"⚠️ Не привязано к висяку: {res['unresolved']} (честный остаток — "
              "уточнить, по какому вопросу).")

    if res["ack"]:
        print("\n--- Ответ бота (в ГРУППУ НЕ отправляется, показан для сверки) ---")
        print(res["ack"])

    # Машинная сводка без какого-либо текста задач — безопасна для лога/сверки прогона.
    print(
        "\nSUMMARY: "
        f"series={series} date={date} mode={'apply' if apply else 'dry-run'} "
        f"batch={int(res['batch'])} recognized={n_recognized} "
        f"done={counts.get(_sm.STATUS_DONE, 0)} "
        f"cancelled={counts.get(_sm.STATUS_CANCELLED, 0)} "
        f"open={counts.get(_sm.STATUS_OPEN, 0)} "
        f"doubt={counts.get(_sm.STATUS_DOUBT, 0)} "
        f"unresolved={res['unresolved']} "
        f"snapshot={int(res['snapshot_used'])} "
        f"flag_split={'on' if _mrs.is_enabled() else 'off'}"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ф6: разовое переприменение написанного ответа на утренний дайджест "
                    "«Открытые вопросы к встрече» через живой путь Ф1–Ф4. Dry-run по "
                    "умолчанию; запись — только по --apply. В группу не постит.",
    )
    p.add_argument("series", help="имя ПАПКИ серии в ~/Projects/me/встречи/ "
                                  "(напр. anzhee-direktorat)")
    p.add_argument("date", help="дата ОТВЕТА участника, YYYY-MM-DD (напр. 2026-07-15)")
    p.add_argument("--text-file", help="файл с текстом ответа участника (utf-8)")
    p.add_argument("--text", help="текст ответа участника строкой")
    p.add_argument("--reply-to-message-id", type=int, default=None,
                   help="message_id отвечаемого документа — для резолва ординала против "
                        "его слепка (Ф3/Ф4). Нет слепка → ординал недоступен (overlap).")
    p.add_argument("--root", default=None,
                   help="корень папки встреч (override MEETING_NOTARY_PROTOCOLS_DIR)")
    p.add_argument("--apply", action="store_true",
                   help="РЕАЛЬНО записать статусы в sidecar. Без флага — dry-run (превью).")
    p.add_argument("--dry-run", action="store_true",
                   help="явно указать превью-режим (дефолт, взаимоисключающ с --apply)")
    return p


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = build_arg_parser().parse_args(argv)
    if args.apply and args.dry_run:
        raise SystemExit("--apply и --dry-run взаимоисключающи: без флага — dry-run.")
    apply = bool(args.apply)

    # Инструмент — БАТЧ-путь дайджеста: сплиттер нужен, чтобы разобрать пачку из ~25
    # задач. Включаем флаг для СВОЕГО процесса, если владелец не задал его сам. Не
    # трогает прод-демон (это отдельный процесс). is_enabled() читает env на вызове.
    os.environ.setdefault("ENABLE_MIXED_REPLY_SPLIT", "1")

    text = _read_text(args)
    if not text or not text.strip():
        raise SystemExit("Пустой текст ответа — применять нечего.")

    series_dir = _ml._protokol_root() / args.series if not args.root \
        else Path(args.root).expanduser() / args.series
    if not series_dir.exists():
        root = series_dir.parent
        avail = ""
        if root.is_dir():
            names = sorted(p.name for p in root.iterdir() if p.is_dir())
            avail = "\nДоступные серии:\n  " + "\n  ".join(names) if names else ""
        raise SystemExit(f"Папка серии не найдена: {series_dir}{avail}")

    if apply:
        # Owner-in-loop страховка: реальная запись — осознанный шаг. Печатаем баннер.
        print("⚠️  РЕЖИМ ЗАПИСИ (--apply): статусы будут записаны в sidecar серии.\n")

    res = reapply(
        args.series, args.date, text,
        root=args.root, reply_to_message_id=args.reply_to_message_id, apply=apply,
    )
    _print_preview(args.series, args.date, len(text), res, apply=apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
