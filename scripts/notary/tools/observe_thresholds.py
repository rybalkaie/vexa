#!/usr/bin/env python3
"""CLI: observe_thresholds.py — собирает метрики 7-дневного наблюдения Ф8.

Источник: лог `meeting-notary-listener.service` (journalctl на VPS) или
произвольный файл `--log-file <path>` (для smoke и backfill).

Три порога (РАЗМ3 плана):
  - ≤1 misdelivery   (две `[delivery] sent` для одного meeting_id с разными
                      chat_id — дубль или доставка не в ту группу)
  - ≤2 ложных задач  (строки в `tasks.md` с пометкой `протокол <series> <date>`,
                      попавшие в секции «❌ Отменено» / «Удалено» / зачёркнутые
                      `~~text~~`. ~/Projects/me/ не git → эвристика по секциям)
  - 0 потерь файлов  (для каждого `Protocol written → <path>` проверяем что
                      файл существует. Нет → потеря.)

Использование:

  # VPS, последние 7 дней:
  python3 tools/observe_thresholds.py --journal-host meeting-notary --days 7

  # Локальный файл с логом (для smoke):
  python3 tools/observe_thresholds.py --log-file ./test-log.txt --days 7

  # JSON в stdout, markdown в файл:
  python3 tools/observe_thresholds.py --journal-host meeting-notary \\
      --format both --out ./report.md

  # Дайджест Илье в личку через @ilya_protocol_meeting_bot:
  python3 tools/observe_thresholds.py --journal-host meeting-notary \\
      --push-telegram

Exit code:
  0  все пороги выдержаны
  1  хотя бы один порог превышен
  2  ошибка чтения лога / отсутствует источник
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

THIS_DIR = Path(__file__).resolve().parent
NOTARY_DIR = THIS_DIR.parent

DEFAULT_MEETINGS_ROOT = Path(os.path.expanduser("~/Projects/me/встречи"))
DEFAULT_TASKS_MD = Path(os.path.expanduser("~/Projects/me/tasks.md"))

DELIVERY_SENT_RE = re.compile(
    r"\[delivery\] sent meeting=(?P<sid>\S+) chat_id=(?P<chat_id>-?\d+) parts=(?P<parts>\d+)"
)
DELIVERY_IDEMPOTENT_RE = re.compile(
    r"\[delivery\] idempotent skip meeting=(?P<sid>\S+) chat_id=(?P<chat_id>-?\d+) parts=(?P<parts>\d+)"
)
DELIVERY_DISABLED_RE = re.compile(r"\[delivery\] disabled by ENABLE_PROTOCOL_DELIVERY=0")
DELIVERY_ASKED_RE = re.compile(r"\[delivery\] asked meeting=(?P<sid>\S+) reason=")
CORRECTION_APPLIED_RE = re.compile(
    r"\[correction\] applied meeting=(?P<sid>\S+) kind=(?P<kind>\S+) version=(?P<ver>\S+)"
)
# `[delivery] failed (non-fatal): <error>` пишется в finalize-meeting.py на любой
# exception от deliver_protocol (bot не в группе, токен невалиден, сеть). Сама
# финализация не валится, но это серьёзный сигнал — слежу отдельной категорией.
DELIVERY_FAILED_RE = re.compile(r"\[delivery\] failed[^:]*: (?P<error>.+)$")
# Путь без хвостовых суффиксов типа ` (took 2.3s)`. Жадно до конца строки,
# но всё что после ` (` отрезаем — это будущий лог-формат.
PROTOCOL_WRITTEN_RE = re.compile(r"Protocol written → (?P<path>[^()]+?)(?: \(.+)?$")
ROUTE_TASKS_RE = re.compile(
    r"\[route_tasks\] meeting=(?P<sid>\S+) ilia=(?P<ilia>\d+) others=(?P<others>\d+)"
    r" unknown=(?P<unknown>\d+) pending_deadline=(?P<pd>\d+) errors=(?P<err>\d+)"
)


@dataclass
class LogEvent:
    """Один распознанный event из лога."""
    kind: str           # delivery_sent | delivery_idempotent | delivery_disabled | delivery_asked | correction_applied | protocol_written | route_tasks
    line_num: int       # 1-based номер строки в исходном логе
    raw: str            # сырая строка
    fields: dict        # распарсенные поля


@dataclass
class Metrics:
    period_start: datetime
    period_end: datetime
    days: int
    log_source: str
    log_lines_count: int = 0
    events: list[LogEvent] = field(default_factory=list)

    # Counts
    meetings_finalized: int = 0
    deliveries_sent: int = 0
    deliveries_skipped_idempotent: int = 0
    deliveries_disabled: int = 0
    deliveries_asked: int = 0
    corrections_applied: int = 0
    tasks_extracted_total: int = 0

    # Threshold cases
    misdelivery_cases: list[dict] = field(default_factory=list)
    false_task_cases: list[dict] = field(default_factory=list)
    file_loss_cases: list[dict] = field(default_factory=list)
    delivery_failed_cases: list[dict] = field(default_factory=list)
    tasks_md_missing: bool = False


# ----- Загрузка логов -----


def fetch_log_lines(args) -> tuple[list[str], str]:
    """Возвращает (lines, source_description)."""
    if args.log_file:
        path = Path(args.log_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"--log-file: файл не найден: {path}")
        return path.read_text(encoding="utf-8", errors="replace").splitlines(), f"file:{path}"

    if args.journal_host:
        # journalctl принимает YYYY-MM-DD (полный день) — без двоеточий,
        # которые ssh теряет при разборе аргументов.
        since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        since = since_dt.strftime("%Y-%m-%d")
        cmd = ["ssh", args.journal_host, "journalctl", "-u", args.service,
               "-S", since, "--no-pager", "-o", "cat"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        except FileNotFoundError as e:
            raise SystemExit(f"ssh недоступен: {e}")
        if out.returncode != 0:
            raise SystemExit(f"journalctl на {args.journal_host} вернул код {out.returncode}: {out.stderr[:200]}")
        return out.stdout.splitlines(), f"journalctl@{args.journal_host}:{args.service}"

    raise SystemExit("Нужен один из источников: --journal-host <host> или --log-file <path>")


# ----- Парсинг -----


def parse_log(lines: Iterable[str]) -> list[LogEvent]:
    """Достаёт релевантные events из лога."""
    events: list[LogEvent] = []
    for i, line in enumerate(lines, start=1):
        m = DELIVERY_SENT_RE.search(line)
        if m:
            events.append(LogEvent("delivery_sent", i, line,
                                    {"sid": m["sid"], "chat_id": int(m["chat_id"]), "parts": int(m["parts"])}))
            continue
        m = DELIVERY_IDEMPOTENT_RE.search(line)
        if m:
            events.append(LogEvent("delivery_idempotent", i, line,
                                    {"sid": m["sid"], "chat_id": int(m["chat_id"]), "parts": int(m["parts"])}))
            continue
        m = DELIVERY_DISABLED_RE.search(line)
        if m:
            events.append(LogEvent("delivery_disabled", i, line, {}))
            continue
        m = DELIVERY_ASKED_RE.search(line)
        if m:
            events.append(LogEvent("delivery_asked", i, line, {"sid": m["sid"]}))
            continue
        m = CORRECTION_APPLIED_RE.search(line)
        if m:
            events.append(LogEvent("correction_applied", i, line,
                                    {"sid": m["sid"], "kind": m["kind"], "version": m["ver"]}))
            continue
        m = DELIVERY_FAILED_RE.search(line)
        if m:
            events.append(LogEvent("delivery_failed", i, line, {"error": m["error"].strip()[:200]}))
            continue
        m = PROTOCOL_WRITTEN_RE.search(line)
        if m:
            events.append(LogEvent("protocol_written", i, line, {"path": m["path"].rstrip()}))
            continue
        m = ROUTE_TASKS_RE.search(line)
        if m:
            events.append(LogEvent("route_tasks", i, line,
                                    {"sid": m["sid"], "ilia": int(m["ilia"]), "others": int(m["others"]),
                                     "unknown": int(m["unknown"]), "pending_deadline": int(m["pd"]),
                                     "errors": int(m["err"])}))
    return events


# ----- Метрики порогов -----


def find_misdelivery(events: list[LogEvent]) -> list[dict]:
    """Misdelivery = две `[delivery] sent` для одного meeting_id с разными chat_id.

    Не считается misdelivery: повторная отправка после `[correction] applied`
    (correction re-trigger `deliver_protocol` для отправки новой версии в группу
    — это штатный поток Ф6, message_ids обновляются на новые, но `delivery_sent`
    логируется ещё раз с тем же chat_id).
    """
    by_sid: dict[str, list[LogEvent]] = defaultdict(list)
    corrections_by_sid: dict[str, list[int]] = defaultdict(list)
    for ev in events:
        if ev.kind == "delivery_sent":
            by_sid[ev.fields["sid"]].append(ev)
        elif ev.kind == "correction_applied":
            corrections_by_sid[ev.fields["sid"]].append(ev.line_num)
    cases: list[dict] = []
    for sid, evs in by_sid.items():
        chats = {ev.fields["chat_id"] for ev in evs}
        if len(chats) > 1:
            cases.append({
                "meeting_id": sid,
                "chat_ids": sorted(chats),
                "log_lines": [ev.line_num for ev in evs],
                "reason": "multiple-chat-ids",
            })
        elif len(evs) > 1:
            # Если между sent[i] и sent[i+1] есть correction_applied — штатный re-send.
            corr_lines = corrections_by_sid.get(sid, [])
            sent_lines = sorted(ev.line_num for ev in evs)
            all_explained_by_correction = True
            for i in range(len(sent_lines) - 1):
                lo, hi = sent_lines[i], sent_lines[i + 1]
                if not any(lo < cl < hi for cl in corr_lines):
                    all_explained_by_correction = False
                    break
            if all_explained_by_correction:
                continue
            cases.append({
                "meeting_id": sid,
                "chat_ids": sorted(chats),
                "log_lines": sent_lines,
                "reason": "duplicate-send-same-chat",
            })
    return cases


def find_file_losses(events: list[LogEvent], meetings_root: Path) -> list[dict]:
    """Потеря = `Protocol written → <path>` есть, но файла нет на диске."""
    cases: list[dict] = []
    for ev in events:
        if ev.kind != "protocol_written":
            continue
        p = ev.fields["path"]
        path = Path(p)
        if not path.is_absolute():
            path = meetings_root / p
        if not path.is_file():
            cases.append({
                "expected_path": str(path),
                "log_line": ev.line_num,
                "raw": ev.raw[:200],
            })
    return cases


# Секции tasks.md, в которых задача считается отменённой / удалённой.
_CANCELED_SECTION_RE = re.compile(r"^##\s*[❌🚫]?\s*(Отменен|Удалён|Удалено|Снят|Cancel)", re.IGNORECASE)
_DONE_SECTION_RE = re.compile(r"^##\s*[✅]?\s*(Готово|Сделано|Закрыт|Done|Closed)", re.IGNORECASE)
_STRIKETHROUGH_RE = re.compile(r"~~[^~]+~~")
# Маркер связи строки с финализированным протоколом.
_PROTOCOL_REF_RE = re.compile(r"протокол\s+([a-z0-9][a-z0-9_-]*)\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def count_false_tasks(tasks_md: Path, period_start: datetime, period_end: datetime) -> list[dict]:
    """Эвристика: строки в `tasks.md`, у которых:
      - есть «протокол <series> <date>» с <date> внутри окна наблюдения,
      - и сама строка находится в секции «Отменено/Удалено/…» либо зачёркнута ~~..~~.

    Без git history (`~/Projects/me/` — не git) другого надёжного способа нет.
    Закрытые задачи (раздел «Готово/Сделано») НЕ считаются ложными — это успех.
    """
    if not tasks_md.is_file():
        return []
    cases: list[dict] = []
    current_section = ""
    section_is_canceled = False
    section_is_done = False
    text = tasks_md.read_text(encoding="utf-8", errors="replace")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("## "):
            current_section = stripped
            section_is_canceled = bool(_CANCELED_SECTION_RE.match(stripped))
            section_is_done = bool(_DONE_SECTION_RE.match(stripped))
            continue
        ref = _PROTOCOL_REF_RE.search(raw)
        if not ref:
            continue
        series = ref.group(1)
        try:
            date = datetime.strptime(ref.group(2), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if not (period_start <= date <= period_end + timedelta(days=1)):
            continue
        is_strikethrough = bool(_STRIKETHROUGH_RE.search(raw))
        if not (section_is_canceled or is_strikethrough):
            continue
        if section_is_done:  # done не считаем ложным даже если зачёркнут
            continue
        cases.append({
            "tasks_md_line": lineno,
            "series": series,
            "date": ref.group(2),
            "section": current_section.rstrip(),
            "reason": "canceled-section" if section_is_canceled else "strikethrough",
            "preview": raw.strip()[:140],
        })
    return cases


# ----- Сборка метрик -----


def compute_metrics(args, lines: list[str], source_desc: str) -> Metrics:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    m = Metrics(period_start=start, period_end=end, days=args.days,
                log_source=source_desc, log_lines_count=len(lines))
    m.events = parse_log(lines)

    for ev in m.events:
        if ev.kind == "protocol_written":
            m.meetings_finalized += 1
        elif ev.kind == "delivery_sent":
            m.deliveries_sent += 1
        elif ev.kind == "delivery_idempotent":
            m.deliveries_skipped_idempotent += 1
        elif ev.kind == "delivery_disabled":
            m.deliveries_disabled += 1
        elif ev.kind == "delivery_asked":
            m.deliveries_asked += 1
        elif ev.kind == "correction_applied":
            m.corrections_applied += 1
        elif ev.kind == "delivery_failed":
            m.delivery_failed_cases.append({"log_line": ev.line_num, "error": ev.fields["error"]})
        elif ev.kind == "route_tasks":
            m.tasks_extracted_total += ev.fields["ilia"] + ev.fields["others"] + ev.fields["unknown"]

    m.misdelivery_cases = find_misdelivery(m.events)
    m.file_loss_cases = find_file_losses(m.events, Path(args.meetings_root).expanduser())
    tasks_md_path = Path(args.tasks_md).expanduser()
    if not tasks_md_path.is_file():
        # Не молчим: иначе на VPS-запуске тут всегда 0 → ложно-зелёный
        # сигнал. Лучше явно сказать «не проверено».
        m.tasks_md_missing = True
        sys.stderr.write(
            f"[observe] WARNING: --tasks-md не найден: {tasks_md_path}. "
            f"Ложные задачи НЕ проверены (порог считается выдержанным по умолчанию). "
            f"Запускай скрипт на машине, где лежит tasks.md (мак Ильи).\n"
        )
    else:
        m.tasks_md_missing = False
    m.false_task_cases = count_false_tasks(tasks_md_path, start, end)
    return m


def thresholds_summary(m: Metrics) -> dict:
    misdelivery_n = len(m.misdelivery_cases)
    false_tasks_n = len(m.false_task_cases)
    losses_n = len(m.file_loss_cases)
    return {
        "misdelivery": {"count": misdelivery_n, "limit": 1, "ok": misdelivery_n <= 1},
        "false_tasks": {"count": false_tasks_n, "limit": 2, "ok": false_tasks_n <= 2},
        "file_losses": {"count": losses_n, "limit": 0, "ok": losses_n == 0},
    }


# ----- Рендеринг -----


def render_json(m: Metrics) -> str:
    thr = thresholds_summary(m)
    payload = {
        "period": {
            "start": m.period_start.isoformat(),
            "end": m.period_end.isoformat(),
            "days": m.days,
        },
        "log_source": m.log_source,
        "log_lines_count": m.log_lines_count,
        "totals": {
            "meetings_finalized": m.meetings_finalized,
            "deliveries_sent": m.deliveries_sent,
            "deliveries_skipped_idempotent": m.deliveries_skipped_idempotent,
            "deliveries_disabled": m.deliveries_disabled,
            "deliveries_asked": m.deliveries_asked,
            "corrections_applied": m.corrections_applied,
            "delivery_failed_count": len(m.delivery_failed_cases),
            "delivery_failed_cases": m.delivery_failed_cases,
            "tasks_extracted_total": m.tasks_extracted_total,
            "tasks_md_missing": m.tasks_md_missing,
        },
        "thresholds": {
            "misdelivery": {**thr["misdelivery"], "cases": m.misdelivery_cases},
            "false_tasks": {**thr["false_tasks"], "cases": m.false_task_cases},
            "file_losses": {**thr["file_losses"], "cases": m.file_loss_cases},
        },
        "all_thresholds_passed": all(v["ok"] for v in thr.values()),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_markdown(m: Metrics) -> str:
    thr = thresholds_summary(m)
    all_ok = all(v["ok"] for v in thr.values())
    status = "✅ Все 3 порога выдержаны" if all_ok else "🔴 Хотя бы один порог превышен"

    out = [
        f"# meeting-notary LLM — наблюдение Ф8",
        "",
        f"_Сгенерирован: {datetime.now(timezone.utc).isoformat()}_",
        f"_Период: {m.period_start.date()} → {m.period_end.date()} ({m.days} дн.)_",
        f"_Источник: `{m.log_source}` ({m.log_lines_count} строк)_",
        "",
        f"## {status}",
        "",
        "| Порог | Значение | Лимит | Статус |",
        "|---|---:|---:|:---:|",
        f"| misdelivery | {thr['misdelivery']['count']} | ≤{thr['misdelivery']['limit']} | "
        f"{'✅' if thr['misdelivery']['ok'] else '🔴'} |",
        f"| ложных задач | {thr['false_tasks']['count']} | ≤{thr['false_tasks']['limit']} | "
        f"{'✅' if thr['false_tasks']['ok'] else '🔴'} |",
        f"| потерь файлов | {thr['file_losses']['count']} | ={thr['file_losses']['limit']} | "
        f"{'✅' if thr['file_losses']['ok'] else '🔴'} |",
        "",
        "## Сводка по логу",
        "",
        f"- Финализаций (`Protocol written →`): {m.meetings_finalized}",
        f"- Доставок в группу (`[delivery] sent`): {m.deliveries_sent}",
        f"- Идемпотентных пропусков: {m.deliveries_skipped_idempotent}",
        f"- Disabled (гейт OFF): {m.deliveries_disabled}",
        f"- Asked (без привязки): {m.deliveries_asked}",
        f"- Коррекций: {m.corrections_applied}",
        f"- Задач извлечено всего: {m.tasks_extracted_total}",
        f"- 🟡 Ошибок доставки `[delivery] failed`: {len(m.delivery_failed_cases)}",
        "",
    ]
    if m.tasks_md_missing:
        out.append("> ⚠️ `tasks.md` не найден на этой машине — ложные задачи НЕ проверены."
                   " Запускай скрипт на маке Ильи.")
        out.append("")
    if m.delivery_failed_cases:
        out.append("## 🟡 Ошибки доставки (non-fatal)")
        out.append("")
        for c in m.delivery_failed_cases:
            out.append(f"- log_line={c['log_line']}: {c['error']}")
        out.append("")

    if m.misdelivery_cases:
        out.append("## 🔴 Misdelivery")
        out.append("")
        for c in m.misdelivery_cases:
            out.append(f"- meeting={c['meeting_id']} chat_ids={c['chat_ids']} "
                       f"reason={c['reason']} log_lines={c['log_lines']}")
        out.append("")

    if m.false_task_cases:
        out.append("## 🟡 Ложные задачи (эвристика по секциям tasks.md)")
        out.append("")
        for c in m.false_task_cases:
            out.append(f"- tasks.md:{c['tasks_md_line']} протокол `{c['series']}` {c['date']} "
                       f"({c['reason']}, секция: {c['section']!r})")
            out.append(f"    > {c['preview']}")
        out.append("")

    if m.file_loss_cases:
        out.append("## 🔴 Потери файлов")
        out.append("")
        for c in m.file_loss_cases:
            out.append(f"- log_line={c['log_line']}: expected `{c['expected_path']}`")
            out.append(f"    > {c['raw']}")
        out.append("")

    if all_ok:
        out.append("## Итог")
        out.append("")
        out.append("Все 3 порога выдержаны. План `2026-05-28-meeting-notary-llm-protokol-i-dostavka.md` "
                   "можно закрывать — заполнить «## Итог», статус `[x] Фаза 8`.")
        out.append("")
    else:
        out.append("## Итог")
        out.append("")
        out.append("Хотя бы один порог превышен → `/план-доработок-1`.")
        out.append("")

    return "\n".join(out)


# ----- Push Telegram (опц.) -----


def _shorten_for_telegram(md: str, max_len: int = 3500) -> str:
    if len(md) <= max_len:
        return md
    return md[:max_len - 50].rstrip() + "\n\n_… (обрезано — см. полный отчёт)_"


def push_telegram(text: str) -> tuple[bool, str]:
    """Через `~/.local/bin/tg-send` (если есть) — простой текст в личку Илье."""
    tg_send = Path(os.path.expanduser("~/.local/bin/tg-send"))
    if not tg_send.is_file():
        return False, f"tg-send не найден: {tg_send}"
    text = _shorten_for_telegram(text)
    try:
        out = subprocess.run([str(tg_send), text], capture_output=True, text=True, timeout=30, check=False)
        if out.returncode != 0:
            return False, f"tg-send код {out.returncode}: {out.stderr[:200]}"
        return True, "sent"
    except FileNotFoundError as e:
        return False, f"tg-send недоступен: {e}"
    except subprocess.TimeoutExpired:
        return False, "tg-send timeout 30s"


# ----- CLI -----


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="окно в днях (default 7)")
    ap.add_argument("--journal-host", default=None,
                    help="ssh-хост для journalctl (например 'meeting-notary')")
    ap.add_argument("--service", default="meeting-notary-listener",
                    help="systemd-юнит для journalctl (default meeting-notary-listener)")
    ap.add_argument("--log-file", default=None,
                    help="прочитать лог из файла вместо journalctl (для smoke)")
    ap.add_argument("--meetings-root", default=str(DEFAULT_MEETINGS_ROOT),
                    help=f"корень {DEFAULT_MEETINGS_ROOT}")
    ap.add_argument("--tasks-md", default=str(DEFAULT_TASKS_MD),
                    help=f"путь к tasks.md (default {DEFAULT_TASKS_MD})")
    ap.add_argument("--format", choices=("markdown", "json", "both"), default="markdown")
    ap.add_argument("--out", default=None,
                    help="записать markdown в файл (json всё равно идёт в stdout если --format=both)")
    ap.add_argument("--push-telegram", action="store_true",
                    help="отправить краткое резюме Илье через ~/.local/bin/tg-send")
    args = ap.parse_args()

    lines, source = fetch_log_lines(args)
    metrics = compute_metrics(args, lines, source)
    md = render_markdown(metrics)
    js = render_json(metrics)

    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        sys.stderr.write(f"[observe] markdown → {args.out}\n")

    if args.format in ("markdown", "both"):
        if not args.out:
            print(md)
    if args.format in ("json", "both"):
        print(js)

    if args.push_telegram:
        # Короткое резюме: только статус + 3 порога + ключевые числа.
        thr = thresholds_summary(metrics)
        all_ok = all(v["ok"] for v in thr.values())
        head = "✅ meeting-notary Ф8" if all_ok else "🔴 meeting-notary Ф8"
        failed_n = len(metrics.delivery_failed_cases)
        failed_line = f"\n🟡 Ошибок доставки (non-fatal): {failed_n}" if failed_n else ""
        tasks_warn = "\n⚠️ tasks.md не найден — ложные задачи не проверены" if metrics.tasks_md_missing else ""
        digest = (
            f"{head} (период: {metrics.period_start.date()} → {metrics.period_end.date()})\n\n"
            f"Финализаций: {metrics.meetings_finalized}\n"
            f"Доставок: {metrics.deliveries_sent}\n"
            f"Задач: {metrics.tasks_extracted_total}"
            f"{failed_line}{tasks_warn}\n\n"
            f"Пороги:\n"
            f"  misdelivery {thr['misdelivery']['count']}/{thr['misdelivery']['limit']} "
            f"{'✅' if thr['misdelivery']['ok'] else '🔴'}\n"
            f"  ложных задач {thr['false_tasks']['count']}/{thr['false_tasks']['limit']} "
            f"{'✅' if thr['false_tasks']['ok'] else '🔴'}\n"
            f"  потерь файлов {thr['file_losses']['count']}/{thr['file_losses']['limit']} "
            f"{'✅' if thr['file_losses']['ok'] else '🔴'}\n\n"
            f"План: {'закрывается' if all_ok else 'нужны доработки (/план-доработок-1)'}."
        )
        ok, msg = push_telegram(digest)
        sys.stderr.write(f"[observe] telegram push: {'OK' if ok else 'FAIL'} ({msg})\n")

    all_ok = all(v["ok"] for v in thresholds_summary(metrics).values())
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
