#!/usr/bin/env python3
"""stt_weekly_guard.py — недельный монитор трат Speechmatics + kill-switch (CG4–CG9).

Запускается systemd-timer'ом `meeting-notary-stt-weekly-guard.timer` раз в сутки.
На каждом тике:

  1. CG4 — через Speechmatics jobs API суммирует часы аудио за скользящие 7 дней.
  2. CG5 — ≤WARN ч  → молчит;
            WARN..BLOCK → пуш с цифрой и разбивкой по дням.
  3. CG6 — ≥BLOCK ч → взводит kill-switch (флаг-файл) + пуш «расшифровка остановлена».
  4. CG9 — пока kill-switch взведён → ежедневное напоминание с числом ждущих встреч.
  5. CG8 — kill-switch снимается ТОЛЬКО вручную (`rm <flag>`), автоснятия здесь нет.

Пороги (env, дефолты из плана Ф2): STT_WEEKLY_WARN_H=10, STT_WEEKLY_BLOCK_H=15.
Флаг kill-switch: STT_KILLSWITCH_PATH (тот же env, что читает speechmatics_client —
проверка перед каждым сабмитом, CG7).

Тестируемость: вся бизнес-логика — чистые функции (compute_weekly_summary,
count_waiting_meetings, формат сообщений) + `run_guard` с инъекцией `jobs_fetcher`
и `pusher`. HTTP и tg-send в проде подставляются дефолтами; httpx импортируется
лениво внутри fetcher'а, чтобы модуль грузился и под system python3 (без httpx).

Логи: stdout/stderr → journal (через ExecStart systemd-юнита).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, NamedTuple


logger = logging.getLogger("stt-weekly-guard")


# Дефолты порогов и путей — держать в синхроне с планом Ф2 и
# speechmatics_client.DEFAULT_KILLSWITCH_PATH (общий контракт — env-переменные,
# которые в проде задаются в .env.notary; литералы лишь fallback).
DEFAULT_WARN_H = 10.0
DEFAULT_BLOCK_H = 15.0
DEFAULT_KILLSWITCH_PATH = "/srv/meeting-notary/state/stt-killswitch.flag"
DEFAULT_FAILED_DIR = "/srv/meeting-notary/_failed"

WINDOW_DAYS = 7
_WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class WeeklySummary(NamedTuple):
    total_hours: float
    by_day: dict       # {"YYYY-MM-DD": hours_float}
    job_count: int


# --------------------------------------------------------------------------- #
# Время
# --------------------------------------------------------------------------- #

def _now_utc() -> dt.datetime:
    return dt.datetime.utcnow()


def _parse_iso(s) -> dt.datetime | None:
    """Speechmatics ISO ("2026-06-05T10:00:00.000Z") → naive UTC datetime.

    Кривой/пустой/неизвестный формат → None (job просто не учитывается; это
    защита от мусора в ответе API, а не тихий пропуск своих данных)."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(t)
    except (ValueError, TypeError):
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def _env_float(name: str, default: float) -> float:
    """Положительный float из env, иначе default (терпим мусор/пусто)."""
    try:
        v = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


# --------------------------------------------------------------------------- #
# CG4 — сумма часов за 7 дней
# --------------------------------------------------------------------------- #

def compute_weekly_summary(
    jobs: list[dict], now: dt.datetime, *, window_days: int = WINDOW_DAYS
) -> WeeklySummary:
    """Сумма часов аудио и разбивка по дням за скользящее окно `window_days`.

    `jobs` — список job'ов Speechmatics ({created_at, duration, ...}). Учитываем
    только созданные в окне [now-window; now]. `duration` — секунды аудио; None /
    мусор → 0. Группировка по дате создания (UTC)."""
    window_start = now - dt.timedelta(days=window_days)
    by_day: dict[str, float] = {}
    total_s = 0.0
    n = 0
    for j in jobs:
        ca = _parse_iso(j.get("created_at"))
        if ca is None or ca < window_start:
            continue
        try:
            dur = float(j.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if dur < 0:
            dur = 0.0
        total_s += dur
        n += 1
        key = ca.date().isoformat()
        by_day[key] = by_day.get(key, 0.0) + dur
    return WeeklySummary(
        total_hours=total_s / 3600.0,
        by_day={k: v / 3600.0 for k, v in by_day.items()},
        job_count=n,
    )


def fetch_jobs_speechmatics(
    now: dt.datetime,
    *,
    window_days: int = WINDOW_DAYS,
    page_limit: int = 100,
    max_pages: int = 20,
) -> list[dict]:
    """CG4: вытянуть job'ы Speechmatics за последние `window_days` через jobs API.

    Пагинация `created_before` (API отдаёт от новых к старым). Останавливаемся,
    как только встретили job старше окна или страница неполная. httpx и ключ
    импортируются ЛЕНИВО — чтобы модуль грузился под system python3 без httpx."""
    import httpx  # noqa: PLC0415  (ленивый импорт — см. docstring модуля)
    from lib.speechmatics_client import (  # noqa: PLC0415
        _get_api_key,
        SPEECHMATICS_BASE_URL,
        HTTP_TIMEOUT_S,
    )

    headers = {"Authorization": f"Bearer {_get_api_key()}"}
    window_start = now - dt.timedelta(days=window_days)
    out: list[dict] = []
    seen_ids: set[str] = set()  # дедуп: если API проигнорит created_before, страницы
    created_before: str | None = None  # повторятся — id-дедуп спасает от двойного счёта
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        for page in range(max_pages):
            params: dict = {"limit": page_limit}
            if created_before:
                params["created_before"] = created_before
            r = client.get(f"{SPEECHMATICS_BASE_URL}/jobs", headers=headers, params=params)
            r.raise_for_status()
            jobs = r.json().get("jobs") or []
            if not jobs:
                break
            reached_old = False
            new_on_page = 0
            for j in jobs:
                jid = j.get("id")
                if jid and jid in seen_ids:
                    continue
                if jid:
                    seen_ids.add(jid)
                new_on_page += 1
                ca = _parse_iso(j.get("created_at"))
                if ca is None:
                    continue
                if ca < window_start:
                    reached_old = True
                    continue
                out.append(j)
            # reached_old — на странице есть job старше окна (всё нужное собрано);
            # неполная страница — последняя; new_on_page==0 — API повторяет страницы
            # (created_before не поддержан) → выходим, чтобы не крутиться вхолостую.
            if reached_old or len(jobs) < page_limit or new_on_page == 0:
                break
            created_before = jobs[-1].get("created_at")
            if page == max_pages - 1:
                # Не тихий потолок: предупреждаем, что окно могло недосчитаться.
                logger.warning(
                    "Speechmatics jobs: достигнут потолок %d страниц — сумма за "
                    "%d дней может быть НЕПОЛНОЙ (недооценка трат)", max_pages, window_days,
                )
    return out


# --------------------------------------------------------------------------- #
# CG6/CG7/CG8 — kill-switch (флаг-файл)
# --------------------------------------------------------------------------- #

def killswitch_armed(killswitch_path: Path) -> bool:
    try:
        return Path(killswitch_path).exists()
    except OSError:
        return False


def arm_killswitch(
    killswitch_path: Path, *, hours: float, now: dt.datetime, reason: str = ""
) -> None:
    """CG6: взвести kill-switch. Пишем мета-JSON (для аудита), но арм определяется
    САМИМ ФАКТОМ существования файла. Идемпотентно: повторный вызов не страшен."""
    p = Path(killswitch_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "armed_at": now.isoformat() + "Z",
        "weekly_hours": round(hours, 2),
        "reason": reason or "weekly hours >= block threshold",
        "note": "Снимается ТОЛЬКО вручную: rm этот файл (CG8). Автоснятия нет.",
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# CG9 — счётчик ждущих встреч
# --------------------------------------------------------------------------- #

def count_waiting_meetings(failed_dir: Path) -> int:
    """Сколько встреч ждут расшифровки из-за kill-switch (CG9).

    Считаем `_failed/*.retry-state.json` с флагом `blocked_by_killswitch`. Именно
    эти отложены kill-switch'ем (а не упали по другой причине)."""
    d = Path(failed_dir)
    if not d.exists():
        return 0
    n = 0
    for sp in d.glob("*.retry-state.json"):
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(st, dict) and st.get("blocked_by_killswitch"):
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Форматирование сообщений
# --------------------------------------------------------------------------- #

def _fmt_by_day(by_day: dict) -> str:
    if not by_day:
        return "  (нет job'ов за окно)"
    lines = []
    for day in sorted(by_day):
        try:
            d = dt.date.fromisoformat(day)
            label = f"{_WEEKDAYS_RU[d.weekday()]} {d.strftime('%d.%m')}"
        except ValueError:
            label = day
        lines.append(f"  {label}: {by_day[day]:.1f} ч")
    return "\n".join(lines)


def _fmt_warn(summary: WeeklySummary, warn_h: float, block_h: float) -> str:
    return (
        f"📊 Speechmatics: за 7 дней {summary.total_hours:.1f} ч "
        f"(порог предупреждения {warn_h:g} ч, стоп на {block_h:g} ч).\n"
        f"По дням:\n{_fmt_by_day(summary.by_day)}\n"
        f"Пока только слежу — расшифровку не останавливаю."
    )


def _fmt_block(summary: WeeklySummary, block_h: float, killswitch_path: Path) -> str:
    return (
        f"🛑 Speechmatics: за 7 дней {summary.total_hours:.1f} ч — достигнут стоп-порог "
        f"{block_h:g} ч. Расшифровка ОСТАНОВЛЕНА (kill-switch взведён). Новые встречи "
        f"копятся в очереди и НЕ теряются.\n"
        f"По дням:\n{_fmt_by_day(summary.by_day)}\n"
        f"Снять стоп вручную: rm {killswitch_path}"
    )


def _fmt_reminder(summary: WeeklySummary, n_waiting: int, killswitch_path: Path) -> str:
    return (
        f"⏸ Расшифровка всё ещё на стопе (kill-switch недельного лимита). "
        f"За 7 дней {summary.total_hours:.1f} ч. Ждут обработки: {n_waiting} встреч(и). "
        f"Когда будешь готов снять стоп: rm {killswitch_path}"
    )


# --------------------------------------------------------------------------- #
# Оркестрация одного тика
# --------------------------------------------------------------------------- #

def run_guard(
    *,
    now: dt.datetime,
    failed_dir: Path,
    warn_h: float,
    block_h: float,
    killswitch_path: Path,
    jobs_fetcher: Callable[[dt.datetime], list[dict]],
    pusher: Callable[..., object],
    dry_run: bool = False,
    log: logging.Logger | None = None,
) -> dict:
    """Один проход монитора. Возвращает {summary, actions, armed, waiting}.

    `pusher(message, *, dedupe_key=None)` — отправитель (notify.push в проде,
    рекордер в тестах). `jobs_fetcher(now) -> list[dict]` — источник job'ов."""
    log = log or logger
    jobs = jobs_fetcher(now)
    summary = compute_weekly_summary(jobs, now)
    day = now.date().isoformat()
    armed_before = killswitch_armed(killswitch_path)
    actions: list[str] = []
    log.info(
        "STT weekly: %.2f ч за 7 дней (%d job'ов), kill-switch=%s, пороги warn=%s block=%s%s",
        summary.total_hours, summary.job_count, "ON" if armed_before else "off",
        warn_h, block_h, " [dry-run]" if dry_run else "",
    )

    if armed_before:
        # CG8/CG9: стоп снимается только вручную → пока флаг стоит, ежедневно
        # напоминаем с числом ждущих встреч. Текущие часы не важны — расшифровки
        # нет, об этом и надо напоминать.
        n_waiting = count_waiting_meetings(failed_dir)
        pusher(_fmt_reminder(summary, n_waiting, killswitch_path),
               dedupe_key=f"stt-killswitch-reminder:{day}")
        actions.append("reminder")
        return {"summary": summary, "actions": actions, "armed": True, "waiting": n_waiting}

    if summary.total_hours >= block_h:
        # CG6: ≥block → взвести + пуш «остановлена».
        if not dry_run:
            arm_killswitch(killswitch_path, hours=summary.total_hours, now=now,
                           reason=f"weekly {summary.total_hours:.1f}h >= {block_h}h")
        pusher(_fmt_block(summary, block_h, killswitch_path),
               dedupe_key=f"stt-killswitch-armed:{day}")
        actions.append("armed")
    elif summary.total_hours >= warn_h:
        # CG5: warn..block → пуш с разбивкой по дням.
        pusher(_fmt_warn(summary, warn_h, block_h),
               dedupe_key=f"stt-weekly-warn:{day}")
        actions.append("warn")
    else:
        # CG5: ≤warn → молча.
        actions.append("quiet")

    return {
        "summary": summary,
        "actions": actions,
        "armed": killswitch_armed(killswitch_path),
        "waiting": 0,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _resolve_killswitch_path() -> str:
    return os.environ.get("STT_KILLSWITCH_PATH") or DEFAULT_KILLSWITCH_PATH


def _default_pusher(message: str, *, dedupe_key: str | None = None):
    from lib.notify import push  # noqa: PLC0415  (stdlib-only, но держим ленивым)
    return push(message, dedupe_key=dedupe_key)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Недельный монитор трат Speechmatics + kill-switch (CG4–CG9)"
    )
    parser.add_argument(
        "--failed-dir",
        default=os.environ.get("MEETING_NOTARY_FAILED_DIR") or DEFAULT_FAILED_DIR,
    )
    parser.add_argument("--killswitch-path", default=_resolve_killswitch_path())
    parser.add_argument("--warn-h", type=float, default=_env_float("STT_WEEKLY_WARN_H", DEFAULT_WARN_H))
    parser.add_argument("--block-h", type=float, default=_env_float("STT_WEEKLY_BLOCK_H", DEFAULT_BLOCK_H))
    parser.add_argument("--dry-run", action="store_true",
                        help="Не взводить флаг и не слать push (только лог)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.dry_run:
        def pusher(message, *, dedupe_key=None):
            logger.info("[dry-run] PUSH (key=%s): %s", dedupe_key, message.replace("\n", " | "))
    else:
        pusher = _default_pusher

    now = _now_utc()
    try:
        result = run_guard(
            now=now,
            failed_dir=Path(args.failed_dir),
            warn_h=args.warn_h,
            block_h=args.block_h,
            killswitch_path=Path(args.killswitch_path),
            jobs_fetcher=fetch_jobs_speechmatics,
            pusher=pusher,
            dry_run=args.dry_run,
        )
    except Exception as e:  # noqa: BLE001
        # Монитор не должен валиться молча — это «сторож денег». Логируем в journal
        # и один раз пушим (дедуп по дню), но не роняем процесс с ненулевым кодом
        # в бесконечном таймере. Не цитируем тело ошибки целиком (может быть HTTP-
        # ответ Speechmatics с токеном в заголовках) — только тип + короткий хвост.
        logger.exception("stt_weekly_guard упал: %s", e)
        if not args.dry_run:
            try:
                _default_pusher(
                    f"⚠️ Недельный монитор трат Speechmatics не отработал: "
                    f"{type(e).__name__}: {str(e)[:120]}. Слежение за лимитом сейчас не идёт.",
                    dedupe_key=f"stt-weekly-guard-error:{now.date().isoformat()}",
                )
            except Exception:  # noqa: BLE001
                pass
        return 1

    logger.info("stt_weekly_guard tick done: actions=%s", result.get("actions"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
