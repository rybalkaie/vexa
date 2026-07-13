# -*- coding: utf-8 -*-
"""Ф4 (план `2026-07-12-watson-company-memory.md`, REQ R10–R13): утренний дайджест
открытых вопросов в день встречи серии.

Зачем. Сегодня участник видит свои висяки только В ПРОТОКОЛЕ — то есть ПОСЛЕ встречи.
До встречи он к ним не готовится, и на встрече время уходит на «а что там было?».
Дайджест переворачивает порядок: утром в день встречи в чат серии приходит PDF
«Открытые вопросы к встрече», сгруппированный по ответственным. Что не требует
обсуждения — человек закрывает письменно, ответом на это же сообщение; остальное
проговаривается на встрече и закрывается обычным механизмом.

Что это НЕ делает (границы, решение плана):
  - НЕ заводит новый трекинг висяков. Источник — существующая машинерия «жизненного
    цикла висяка»: выжимки серии (`series_memory`), sidecar статусов Ф2
    (`task-status.json`), reply-канал Ф5 (`pending_status_reply`), ночной сверщик Ф6.
    Здесь только ЧТЕНИЕ этого состояния + отправка + регистрация id сообщения.
  - НЕ шлёт личные DM каждому участнику — ОДИН общий PDF в чат серии (решение
    владельца, ВОПР1 2026-07-13); каждый находит в нём свою секцию.
  - НЕ трогает знание компании (Ф1–Ф3 плана) — дайджест про висяки.

Три корзины дайджеста:
  1. значимые висяки по ответственным (`series_memory.group_open_tasks_by_owner`);
  2. «без ответственного / неразобрано» — отдельный бакет, чтобы группировка по людям
     не проглотила безымянные (УПУ2);
  3. «просто напоминаю» — мелочь (`minor`, R12): показываем БЕЗ запроса статуса, после
     встречи она снимается сама (в протокол не едет → в следующую выжимку не переносится).

Опасная тройка (правило проекта `CLAUDE.md`): тексты задач НЕ логируем — только
счётчики (число висяков/мелочи/получателей) и метаданные (серия, дата, chat_id).
LLM здесь не участвует вовсе: дайджест собирается детерминированно из уже лежащих
на диске выжимок, наружу уходит только Telegram-сообщение в тот же чат, где эти же
задачи уже опубликованы протоколом.

stdlib-only (кроме ленивых импортов рендера PDF / Telegram / реестра на боевом пути).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import series_memory

logger = logging.getLogger("notary.morning_digest")

# Москва — фиксированный UTC+3 круглый год (перевода часов нет с 2014). Берём
# фиксированный офсет, а не `zoneinfo`: не зависим от наличия tzdata в контейнере
# и тесты детерминированны. РИСК6 плана (TZ) закрыт этим + суффиксом таймзоны в
# systemd-таймере (VPS живёт в UTC, systemd сам конвертирует; мак-launchd не при
# делах — проект VPS-only с 27.05.2026).
MSK = timezone(timedelta(hours=3))

# R10: целевое время отправки — 09:00 МСК, но НЕ позже чем за час до начала
# (встреча в 09:30 → дайджест в 08:30). Это же даёт «встреча раньше 10:00 → сдвиг».
SEND_HOUR_MSK = 9
LEAD_MINUTES = 60

# Sidecar отправок рядом с памятью серии. Держит ДВЕ вещи: (а) идемпотентность
# «один дайджест на серию в день» (R13 — таймер тикает часто, дублей быть не должно);
# (б) карту `message_id → серия`, по которой listener роутит ОТВЕТ на дайджест в тот же
# `pending_status_reply` (R11). Отдельный файл: sidecar статусов Ф2 не трогаем.
DIGEST_FILE = "morning-digest.json"
DIGEST_SCHEMA = 1
# Сколько дней отправок держим в sidecar (reply приходит по горячим следам; старые
# записи — мусор). Ограничивает и рост файла.
_KEEP_SENT_DAYS = 45

# Шапка-инструкция — ДОСЛОВНО слова владельца (2026-07-12). Не переписывать:
# это его формулировка тона «союзник, не контролёр».
HEADER_INSTRUCTION = (
    "Если вопрос будете проговаривать на сегодняшней встрече — статус можно не писать: "
    "бот услышит и пометит сам. Если по задаче всё уже известно и на встрече её не будет "
    "— напишите ответом на это сообщение текущий статус (сделано/не сделано, "
    "актуально/неактуально) и короткий комментарий — бот скорректирует свой учёт."
)

TITLE = "Открытые вопросы к встрече"
# Тон союзника (канон мягких формулировок lifecycle Ф3): снять вопрос, не делая, —
# нормальный исход, а не провал.
_TONE_LINE = (
    "_Это не аудит и не спрос — просто сверка перед встречей. Снять вопрос, не выполняя "
    "его (передумали, стало неактуально), — такой же нормальный исход, как и сделать._"
)
_NO_OWNER_HEADING = "Без ответственного / неразобрано"
_MINOR_HEADING = "Просто напоминаю (статус не нужен)"


def is_enabled() -> bool:
    """Kill-switch `ENABLE_MORNING_DIGEST` (дефолт ON; `0/false/no` → OFF).

    Полярность ON — сознательно (в отличие от LLM-ярусов, у которых дефолт-OFF по
    [[reissue-llm-tier-gate-default-off]]): здесь НЕТ обращения к LLM и нет нового
    egress ПДн — дайджест шлёт в тот самый чат, где эти же задачи уже опубликованы
    протоколом. Фактический предохранитель — сам systemd-таймер: пока владелец не
    поставил юнит на VPS, отправлять некому. Env-выключатель нужен, чтобы погасить
    рассылку в проде без передеплоя.
    """
    raw = (os.environ.get("ENABLE_MORNING_DIGEST") or "").strip().lower()
    return raw not in ("0", "false", "no")


def protocols_root() -> Path:
    """Корень протоколов (тот же, что у listener/сверщика): `MEETING_NOTARY_PROTOCOLS_DIR`."""
    raw = os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR") or "~/Projects/me/встречи"
    return Path(os.path.expanduser(raw))


# ---------------------------------------------------------------------------
# Когда слать: расписание серии (очередь планировщика) + правило R10
# ---------------------------------------------------------------------------
def _parse_start(raw: Any) -> Optional[datetime]:
    """`start_at` элемента очереди → aware datetime. Наивное трактуем как UTC (так же,
    как `scheduler._expand_one_off`). Мусор → None (элемент пропускаем)."""
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def digest_send_at(start_at: datetime) -> datetime:
    """R10: момент отправки = 09:00 МСК дня встречи, но НЕ позже `start - 1 час`.

    Встреча в 12:00 МСК → 09:00 МСК. Встреча в 09:30 МСК → 08:30 МСК (сдвиг раньше,
    «не позже чем за час до начала»). Встреча в 07:00 МСК → 06:00 МСК. Возвращает
    aware-datetime в МСК.
    """
    start_msk = start_at.astimezone(MSK)
    nine = start_msk.replace(hour=SEND_HOUR_MSK, minute=0, second=0, microsecond=0)
    lead = start_msk - timedelta(minutes=LEAD_MINUTES)
    return min(nine, lead)


def due_meetings_today(
    queue: list, now: datetime, *, only_series: Optional[str] = None
) -> list[dict]:
    """Серии, которым СЕЙЧАС пора слать дайджест: встреча сегодня (по МСК), момент
    отправки уже наступил, встреча ещё не началась.

    `queue` — элементы `.queue.json` планировщика (он же источник расписания серии:
    google-calendar / cron-правило / one-off, горизонт 48ч). Несколько встреч одной
    серии за день → берём САМУЮ РАННЮЮ (дайджест один на серию в день). Встреча уже
    идёт/прошла (now >= start) → не шлём: дайджест «к встрече» задним числом бессмысленен.
    Возвращает `[{series, start_at, send_at, date}]`, отсортированные по началу.
    """
    now_msk = now.astimezone(MSK)
    today = now_msk.date()
    best: dict[str, dict] = {}
    for item in queue or []:
        if not isinstance(item, dict):
            continue
        series = (item.get("series") or "").strip()
        if not series or (only_series and series != only_series):
            continue
        start = _parse_start(item.get("start_at"))
        if start is None:
            continue
        start_msk = start.astimezone(MSK)
        if start_msk.date() != today:
            continue
        prev = best.get(series)
        if prev is None or start_msk < prev["start_at"]:
            best[series] = {
                "series": series,
                "start_at": start_msk,
                "send_at": digest_send_at(start_msk),
                "date": start_msk.date().isoformat(),
            }
    due = [
        rec for rec in best.values()
        if rec["send_at"] <= now_msk < rec["start_at"]
    ]
    due.sort(key=lambda r: r["start_at"])
    return due


# ---------------------------------------------------------------------------
# Что слать: три корзины из существующего учёта висяков
# ---------------------------------------------------------------------------
def collect_items(series_dir: Path) -> dict:
    """Открытые вопросы серии для дайджеста — ЧИТАЕМ существующий учёт, не заводим свой.

    `open`  — значимые ВИСЯЩИЕ: свежий хвост выжимок под наложением sidecar-статусов Ф2
              (`merge_open_tasks_with_status` → корзина `open`). Терминально закрытые
              (сделано / снято / закрыто-авто) и «под сомнением» сюда НЕ попадают:
              закрытое не воскрешаем (ядро Ф2), а doubt — буфер протокола, не повестка.
    `minor` — мелочь (`minor: true`, R12) под теми же статусами: закрытую руками мелочь
              не напоминаем.
    Сбой/нет каталога → пустые корзины (дайджест просто не уйдёт). НЕ логирует тексты.
    """
    empty = {"open": [], "minor": []}
    try:
        digests = series_memory.list_series_digests(Path(series_dir))
        if not digests:
            return empty
        store = series_memory.load_task_status(Path(series_dir))
        fresh = series_memory.resolve_open_tasks(digests)
        hanging = series_memory.merge_open_tasks_with_status(fresh, store).get("open") or []
        minor_all = series_memory.resolve_minor_open_tasks(digests)
        minor = series_memory.merge_open_tasks_with_status(minor_all, store).get("open") or []
    except Exception as e:  # noqa: BLE001 — дайджест best-effort, ничего не валим
        logger.warning("[morning-digest] сбор висяков не удался (non-fatal): %s", type(e).__name__)
        return empty
    return {"open": list(hanging), "minor": list(minor)}


def series_display_name(series: str) -> str:
    """Человеческое имя серии для шапки: slug → «Слово Слово». Реестр не дёргаем
    (он на боевом пути и так читается ради chat_id; лишняя зависимость рендеру не нужна)."""
    s = re.sub(r"[-_]+", " ", (series or "").strip()).strip()
    return s[:1].upper() + s[1:] if s else "Встреча"


def build_digest_markdown(
    series: str,
    date: str,
    hanging: list,
    minor: list,
    *,
    start_at: Optional[datetime] = None,
) -> str:
    """Markdown дайджеста (его же рендерим в PDF существующим генератором).

    Структура: шапка-инструкция владельца (дословно) → тон союзника → безымянные
    (УПУ2, ПЕРВЫМИ — их надо разобрать) → секции по ответственным → «просто напоминаю».
    Пусто в обеих корзинах → "" (caller не шлёт, R13).
    """
    hanging = [t for t in (hanging or []) if isinstance(t, str) and t.strip()]
    minor = [t for t in (minor or []) if isinstance(t, str) and t.strip()]
    if not hanging and not minor:
        return ""
    when = ""
    if start_at is not None:
        when = f" · встреча в {start_at.astimezone(MSK).strftime('%H:%M')} МСК"
    lines = [
        f"# {TITLE}",
        "",
        f"**{series_display_name(series)}** — {date}{when}",
        "",
        HEADER_INSTRUCTION,
        "",
        _TONE_LINE,
        "",
    ]
    nameless, owned = series_memory.group_open_tasks_by_owner(hanging)
    if nameless:
        lines.append(f"## {_NO_OWNER_HEADING}")
        lines.append("")
        for t in nameless:
            lines.append(f"- {t}")
        lines.append("")
    for owner, items in owned:
        lines.append(f"## {owner}")
        lines.append("")
        for t in items:
            lines.append(f"- {t}")
        lines.append("")
    if minor:
        lines.append(f"## {_MINOR_HEADING}")
        lines.append("")
        lines.append(
            "_Мелочи, статус по ним писать не нужно. Если никто не поднимет их на "
            "встрече — они снимутся сами._"
        )
        lines.append("")
        for t in minor:
            lines.append(f"- {t}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_caption(series: str, date: str, hanging: list, minor: list) -> str:
    """Подпись к PDF в Telegram. Инструкция владельца ОБЯЗАНА быть здесь, а не только
    внутри PDF: отвечают-то реплаем на ЭТО сообщение, а не на документ (R11). ≤1024
    символов — лимит caption у Telegram."""
    head = f"📋 {TITLE} — {series_display_name(series)}, {date}"
    counts = f"Открытых вопросов: {len(hanging or [])}"
    if minor:
        counts += f" · мелочей «просто напоминаю»: {len(minor)}"
    text = f"{head}\n{counts}\n\n{HEADER_INSTRUCTION}"
    return text[:1024]


# ---------------------------------------------------------------------------
# Sidecar отправок: идемпотентность (R13) + карта message_id → серия (R11)
# ---------------------------------------------------------------------------
def digest_state_path(series_dir: Path) -> Path:
    return Path(series_dir) / DIGEST_FILE


def load_digest_state(series_dir: Optional[Path]) -> dict:
    """`{date: {chat_id, message_id, at}}`. Нет файла/мусор → {} (миграции не нужны)."""
    if series_dir is None:
        return {}
    p = digest_state_path(series_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sent = data.get("sent") if isinstance(data, dict) else None
    return sent if isinstance(sent, dict) else {}


def already_sent(series_dir: Optional[Path], date: str) -> bool:
    """R13: дайджест этой серии за эту дату уже ушёл? (таймер тикает часто — защита от дубля)."""
    rec = load_digest_state(series_dir).get(date)
    return isinstance(rec, dict) and bool(rec.get("message_id"))


def record_sent(
    series_dir: Path, date: str, *, chat_id: int, message_id: int, at: Optional[str] = None
) -> Optional[Path]:
    """Зафиксировать отправку: идемпотентность + якорь для reply-канала. Прунит старьё.

    Пишем ПОСЛЕ подтверждённой отправки (message_id пришёл от Telegram) — иначе при
    сбое отправки день пометился бы «уже слали» и дайджест пропал бы совсем.
    """
    sent = dict(load_digest_state(series_dir))
    sent[date] = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "at": at or datetime.now(timezone.utc).isoformat(),
    }
    cutoff = (datetime.now(MSK).date() - timedelta(days=_KEEP_SENT_DAYS)).isoformat()
    sent = {d: r for d, r in sent.items()
            if isinstance(d, str) and (len(d) != 10 or d >= cutoff)}
    payload = {"schema": DIGEST_SCHEMA, "sent": sent}
    try:
        series_memory._atomic_write_text(
            digest_state_path(series_dir), json.dumps(payload, ensure_ascii=False, indent=2)
        )
    except (OSError, TypeError, ValueError) as e:
        logger.warning("[morning-digest] запись состояния не удалась (non-fatal): %s",
                       type(e).__name__)
        return None
    return digest_state_path(series_dir)


def digest_series_for_message(
    chat_id: Any, message_id: Any, *, root: Optional[Path] = None
) -> Optional[str]:
    """R11 (РИСК5): серия, чей ДАЙДЖЕСТ был опубликован сообщением (chat_id, message_id).

    Нужна, потому что штатный роутер reply'ев (`_reply_context_series` → индекс
    `meta.delivered`) знает только сообщения ПРОТОКОЛОВ: дайджест — не протокол, его
    message_id в том индексе нет, и без этой карты ответ «сделано» на дайджест уехал бы
    в шлюз правок вместо учёта висяков. Сознательно НЕ дописываем дайджест в
    `meta.delivered`: там он выглядел бы как протокол, и НЕ-статусный reply на него
    ушёл бы править текст протокола прошлой встречи.

    Нет совпадения / сбой → None (прежнее поведение: reply идёт обычным маршрутом).
    """
    if chat_id is None or message_id is None:
        return None
    try:
        cid, mid = int(chat_id), int(message_id)
    except (TypeError, ValueError):
        return None
    base = Path(root) if root is not None else protocols_root()
    if not base.is_dir():
        return None
    try:
        for entry in base.iterdir():
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            for rec in load_digest_state(entry).values():
                if not isinstance(rec, dict):
                    continue
                if rec.get("chat_id") == cid and rec.get("message_id") == mid:
                    return entry.name
    except OSError as e:
        logger.debug("[morning-digest] скан состояний не удался: %s", type(e).__name__)
    return None


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------
def resolve_chat_id(series: str) -> Optional[int]:
    """Чат серии — ТЕМ ЖЕ путём, что доставка протокола: привязка в `watched.yaml`,
    иначе личка владельца (`TELEGRAM_NOTARIUS_CHAT_ID`). Дайджест обязан приходить туда
    же, где лежит протокол с этими же задачами, — иначе reply не на что вешать."""
    cid: Optional[int] = None
    try:
        from .llm_postprocess import _load_watched_for_series  # noqa: PLC0415
        cid = _load_watched_for_series(series)
    except Exception as e:  # noqa: BLE001 — реестр недоступен → пробуем личку
        logger.warning("[morning-digest] реестр серий недоступен (non-fatal): %s", type(e).__name__)
    if cid is not None:
        return cid
    raw = (os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID") or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _default_sender(chat_id: int, md_text: str, caption: str, date: str) -> Optional[int]:
    """Боевая отправка: markdown → PDF (тот же генератор, что у протокола) → sendDocument.
    Возвращает message_id или None. Тексты задач не логируем — только размеры/коды."""
    from . import protocol_to_pdf, telegram_api  # noqa: PLC0415

    token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    if not token:
        logger.warning("[morning-digest] TELEGRAM_NOTARIUS_BOT_TOKEN не задан — не шлю")
        return None
    safe_date = re.sub(r"[^0-9A-Za-z._-]", "-", str(date)) or "digest"
    filename = f"otkrytye-voprosy-{safe_date}.pdf"
    try:
        with tempfile.TemporaryDirectory(prefix="morning-digest-") as td:
            pdf_path = Path(td) / filename
            protocol_to_pdf.render_pdf_from_markdown(
                md_text, str(pdf_path), title=TITLE,
                subtitle=f"Сверка перед встречей · {date}",
            )
            res = telegram_api.send_document(
                token, chat_id, str(pdf_path), caption=caption, filename=filename,
            )
    except Exception as e:  # noqa: BLE001 — сбой рендера/отправки не валит прогон серий
        logger.error("[morning-digest] отправка не удалась chat=%s: %s: %s",
                     chat_id, type(e).__name__, e)
        return None
    mid = res.get("message_id") if isinstance(res, dict) else None
    try:
        return int(mid) if mid else None
    except (TypeError, ValueError):
        return None


def send_series_digest(
    series: str,
    *,
    date: str,
    start_at: Optional[datetime] = None,
    root: Optional[Path] = None,
    dry_run: bool = False,
    chat_resolver: Optional[Callable[[str], Optional[int]]] = None,
    sender: Optional[Callable[..., Optional[int]]] = None,
) -> dict:
    """Собрать и отправить дайджест ОДНОЙ серии. Возвращает счётчики (без текстов).

    Исходы: `sent` (ушёл), `skipped` — `empty` (нет висяков → не шлём, R13),
    `already` (сегодня уже слали), `no-chat` (серия ни к чему не привязана и нет лички),
    `send-failed`. `sender`/`chat_resolver` инъектируются тестами.
    """
    series_dir = (Path(root) if root is not None else protocols_root()) / series
    res: dict = {"series": series, "date": date, "status": "skipped",
                 "open": 0, "minor": 0, "reason": None, "message_id": None}
    if already_sent(series_dir, date):
        res["reason"] = "already"
        return res
    items = collect_items(series_dir)
    hanging, minor = items["open"], items["minor"]
    res["open"], res["minor"] = len(hanging), len(minor)
    if not hanging and not minor:
        res["reason"] = "empty"  # R13: пустой список → дайджест не шлём
        return res
    md = build_digest_markdown(series, date, hanging, minor, start_at=start_at)
    caption = build_caption(series, date, hanging, minor)
    if dry_run:
        res["status"] = "dry-run"
        return res
    chat_id = (chat_resolver or resolve_chat_id)(series)
    if chat_id is None:
        logger.warning("[morning-digest] серия %s: чат не определён — дайджест не ушёл", series)
        res["reason"] = "no-chat"
        return res
    res["chat_id"] = chat_id
    mid = (sender or _default_sender)(chat_id, md, caption, date)
    if not mid:
        res["reason"] = "send-failed"
        return res
    record_sent(series_dir, date, chat_id=chat_id, message_id=mid)
    res["status"] = "sent"
    res["message_id"] = mid
    return res


def run(
    *,
    now: Optional[datetime] = None,
    queue: Optional[list] = None,
    root: Optional[Path] = None,
    dry_run: bool = False,
    only_series: Optional[str] = None,
    chat_resolver: Optional[Callable[[str], Optional[int]]] = None,
    sender: Optional[Callable[..., Optional[int]]] = None,
) -> list[dict]:
    """Один тик планировщика: кому сегодня пора — тому и шлём. Ничего не должен —
    молчим (R13: нет встречи сегодня / нет висяков → сообщения нет).

    Таймер тикает часто (каждые 15 мин), поэтому тик обязан быть дешёвым и
    идемпотентным: чтение очереди + выжимок с диска, никаких LLM. Логи — счётчики.
    """
    if not is_enabled():
        logger.info("[morning-digest] выключен (ENABLE_MORNING_DIGEST=0)")
        return []
    now = now or datetime.now(timezone.utc)
    if queue is None:
        try:
            from . import state  # noqa: PLC0415 — ленивый: тянет cli.registry
            queue = state.read_queue()
        except Exception as e:  # noqa: BLE001
            logger.warning("[morning-digest] очередь встреч недоступна: %s", type(e).__name__)
            return []
    due = due_meetings_today(queue, now, only_series=only_series)
    if not due:
        logger.info("[morning-digest] сегодня отправлять нечего (встреч к отправке: 0)")
        return []
    out: list[dict] = []
    for rec in due:
        try:
            res = send_series_digest(
                rec["series"], date=rec["date"], start_at=rec["start_at"], root=root,
                dry_run=dry_run, chat_resolver=chat_resolver, sender=sender,
            )
        except Exception as e:  # noqa: BLE001 — одна серия не валит остальные
            logger.exception("[morning-digest] серия %s упала: %s", rec["series"], e)
            continue
        out.append(res)
        logger.info(
            "[morning-digest] series=%s date=%s status=%s reason=%s open=%d minor=%d dry_run=%s",
            res["series"], res["date"], res["status"], res.get("reason"),
            res["open"], res["minor"], dry_run,
        )
    return out


def main(argv: Optional[list] = None) -> int:
    """CLI (systemd-таймер зовёт без аргументов)."""
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Утренний дайджест открытых вопросов")
    parser.add_argument("--dry-run", action="store_true",
                        help="посчитать, кому бы ушло, ничего не отправляя")
    parser.add_argument("--series", help="ограничить одной серией (отладка)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    results = run(dry_run=args.dry_run, only_series=args.series)
    sent = sum(1 for r in results if r.get("status") == "sent")
    logger.info("[morning-digest] тик завершён: серий=%d отправлено=%d", len(results), sent)
    return 0
