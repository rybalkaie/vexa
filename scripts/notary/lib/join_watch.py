"""Сторож входа бота во встречу (ISS-60, 23.09.2026).

ЧТО БЫЛО. Запускатель смотрел на контейнер бота один раз — через 3 секунды после
старта (`runner._verify_container_alive`). С 21 по 23.09.2026 бот падал на 40-й
секунде (Телемост 3.0 спрятал лобби во вложенное окно), и три дня ни одна встреча
не записалась без единой тревоги: тревога «пустая запись» живёт от `meta.json`,
а его бот пишет только после входа.

ЧТО СТАЛО. Запускатель заносит каждый старт в журнал сторожа, а на каждом своём
тике (раз в минуту) сторож смотрит на статус входа, который пишет сам бот
(`<transcripts>/.join-status/<session_uid>.json`: joining → waiting_room →
in_call / failed + причина), и на состояние контейнера:

* в звонке → всё хорошо, дальше встречу стережёт сам бот (выброс, глухота);
* сорвался вход → ОДНА повторная попытка новым контейнером; снова сорвался →
  сообщение туда, куда уходит протокол (`join_alert`), и технический разбор
  владельцу;
* завис на входе дольше 4 минут → остановить и повторить / сообщить.

🔴 СТАРЫЙ ОБРАЗ БЕЗ СТАТУСОВ. Скрипты и образ бота выкатываются раздельно. Пока
образ не пишет статус, сторож не знает, где бот, и потому НИКОГО не
останавливает: реагирует только на выход контейнера с кодом 1 (так upstream
выходит при сорванном входе) и забывает про встречу через 20 минут.

🔴 СТОРОЖ НЕ МЕШАЕТ ЗАПУСКУ. Всё, что здесь происходит, вызывается запускателем
под `try`: упади сторож — боты запускаются как раньше.

Решение (`decide`) — чистая функция без docker и файлов: её и тестируем.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("notary.join_watch")


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def is_enabled() -> bool:
    """Выключатель: `MEETING_NOTARY_JOIN_WATCH=0`. По умолчанию сторож включён —
    это прямая просьба владельца 23.09.2026 («не молчать»)."""
    return (os.environ.get("MEETING_NOTARY_JOIN_WATCH") or "1").strip() not in {"0", "false", "no", "off"}


def max_attempts() -> int:
    return _env_int("MEETING_NOTARY_JOIN_MAX_ATTEMPTS", 2)


#: Сколько бот может висеть в фазе «вход» (не в зале ожидания), прежде чем
#: считаться зависшим. Нормальный вход — 30–60 с.
STUCK_JOINING_SEC = _env_int("MEETING_NOTARY_JOIN_STUCK_SEC", 240)
#: Зал ожидания бот бросает сам через 2,5 мин; это страховка от зависания.
STUCK_WAITING_SEC = _env_int("MEETING_NOTARY_JOIN_STUCK_WAITING_SEC", 600)
#: Сорвавшийся бот выходит за секунды; если контейнер ещё жив через столько —
#: останавливаем его, чтобы не держать повтор.
FAILED_EXIT_GRACE_SEC = 90
#: «В звонке» засчитывается, только если продержалось столько: сразу после
#: in_call бот ещё раз проверяет, что он в звонке, и при промахе пишет failed
#: (admission_false_positive). Сторож не должен успеть закрыть вход «успехом».
IN_CALL_STABLE_SEC = 20
#: Повторять вход имеет смысл до этого момента после старта встречи.
RETRY_DEADLINE_AFTER_START_MIN = 30
#: Старый образ без статусов — сколько следим за контейнером.
LEGACY_WATCH_SEC = 20 * 60
#: Ошибка docker — сколько ждём, прежде чем забыть.
UNKNOWN_FORGET_SEC = 30 * 60
#: Сколько живут записи журнала.
LEDGER_TTL_HOURS = 12
#: Аварийные снимки и статусы старше — убираются (это диагностика, не записи встреч).
CRASH_KIT_TTL_DAYS = 30
STATUS_TTL_DAYS = 7

#: Причины, где повтор бессмысленен: встречи нет, людей отклонили бота.
NO_RETRY_REASONS = frozenset({"meeting_not_found", "admission_rejected"})

# Действия решения.
OK = "ok"
WAIT = "wait"
RETRY = "retry"
ALERT = "alert"
STOP_RETRY = "stop_retry"
STOP_ALERT = "stop_alert"
FORGET = "forget"


@dataclass(frozen=True)
class ContainerState:
    #: running / exited / created / restarting / missing / unknown
    status: str
    exit_code: Optional[int] = None


@dataclass(frozen=True)
class Observation:
    phase: Optional[str]
    reason: Optional[str]
    status_seen: bool
    container: ContainerState
    fallback_used: Optional[Dict[str, Any]] = None
    crash_kit: Optional[str] = None
    #: Сколько секунд назад бот записал текущую фазу.
    phase_age_sec: Optional[float] = None


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ""


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def decide(
    obs: Observation,
    *,
    attempt: int,
    max_attempts_: int,
    launched_at: datetime,
    start_at: Optional[datetime],
    now: datetime,
) -> Decision:
    """Что делать с одной попыткой входа. Чистая функция."""
    age = (now - launched_at).total_seconds()
    retry_allowed = attempt < max_attempts_ and (
        start_at is None or now <= start_at + timedelta(minutes=RETRY_DEADLINE_AFTER_START_MIN)
    )

    def fail(reason: str, *, stop: bool = False) -> Decision:
        if reason in NO_RETRY_REASONS or not retry_allowed:
            return Decision(STOP_ALERT if stop else ALERT, reason)
        return Decision(STOP_RETRY if stop else RETRY, reason)

    running = obs.container.status in {"running", "restarting", "created"}

    # 1. Бот сам сказал «сорвалось» — даже если перед этим мелькнуло «в звонке»
    #    (admission_false_positive пишется сразу после in_call).
    if obs.phase == "failed":
        reason = obs.reason or "join_error"
        if running:
            # Сорвавшийся бот выходит за секунды. Ждём, чтобы не было двух ботов разом;
            # завис на выходе — останавливаем.
            if (obs.phase_age_sec or 0) >= FAILED_EXIT_GRACE_SEC:
                return fail(reason, stop=True)
            return Decision(WAIT, reason)
        return fail(reason)

    # 2. В звонке и держится — вход удался. Дальше встречу стережёт сам бот
    #    (выброс, глухота). Вышел после входа — это уже не дело сторожа входа.
    if obs.phase == "in_call":
        if not running or (obs.phase_age_sec or 0) >= IN_CALL_STABLE_SEC:
            return Decision(OK)
        return Decision(WAIT)

    # 3. Контейнера больше нет или он вышел, а в звонок не попал.
    if obs.container.status in {"exited", "missing", "dead"}:
        if obs.status_seen:
            return fail("crashed")
        # Старый образ без статусов: судим только по коду выхода.
        if obs.container.exit_code == 1:
            return fail("join_error")
        return Decision(FORGET, "legacy_exit")

    # 4. Контейнер жив.
    if running:
        if obs.status_seen:
            if obs.phase == "waiting_room":
                if age >= STUCK_WAITING_SEC:
                    return fail("waiting_room_not_admitted", stop=True)
                return Decision(WAIT)
            if age >= STUCK_JOINING_SEC:
                return fail("stuck_joining", stop=True)
            return Decision(WAIT)
        # Старый образ: никого не останавливаем.
        if age >= LEGACY_WATCH_SEC:
            return Decision(FORGET, "legacy_running")
        return Decision(WAIT)

    # 5. docker не ответил — ждём, но не вечно.
    if age >= UNKNOWN_FORGET_SEC:
        return Decision(FORGET, "docker_unknown")
    return Decision(WAIT)


# ---------------------------------------------------------------------------
# Журнал стартов
# ---------------------------------------------------------------------------

#: Поля элемента очереди, нужные для повтора и сообщения. Ссылка на комнату
#: нужна для повтора; журнал живёт рядом с реестром на сервере (там же ссылки).
_ITEM_FIELDS = (
    "meeting_id", "event_id", "series", "start_at", "duration_minutes", "url",
    "expected_participants", "keep_audio", "type", "calendar_id", "ical_uid",
    "order_id", "immediate", "series_title",
)


def ledger_path() -> Path:
    from notary.cli.registry import _resolve_registry_dir  # noqa: PLC0415

    return _resolve_registry_dir() / ".join-watch.json"


def load_ledger() -> Dict[str, Any]:
    p = ledger_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("[join-watch] журнал не прочитан (%s) — начинаю с пустого", type(e).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def save_ledger(data: Dict[str, Any]) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def register_launch(
    ledger: Dict[str, Any],
    key: str,
    item: Dict[str, Any],
    *,
    session_uid: str,
    container: str,
    attempt: int,
    now: datetime,
) -> None:
    entry = ledger.get(key)
    if not isinstance(entry, dict) or entry.get("state") not in (None, "watching"):
        entry = {
            "item": {k: item.get(k) for k in _ITEM_FIELDS if item.get(k) is not None},
            "series": str(item.get("series") or item.get("meeting_id") or ""),
            "start_at": item.get("start_at"),
            "order_id": item.get("order_id"),
            "series_title": item.get("series_title"),
            "attempts": [],
            "state": "watching",
            "created_at": now.isoformat(),
        }
        ledger[key] = entry
    entry["attempts"].append({
        "attempt": attempt,
        "session_uid": session_uid,
        "container": container,
        "launched_at": now.isoformat(),
    })
    entry["container"] = container
    entry["state"] = "watching"


# ---------------------------------------------------------------------------
# Наблюдение
# ---------------------------------------------------------------------------


def read_status(status_dir: Path, session_uid: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((status_dir / f"{session_uid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def observe(
    status: Optional[Dict[str, Any]], container: ContainerState, now: datetime
) -> Observation:
    st = status or {}
    phase = st.get("phase")
    ts = _parse_ts(st.get("updated_at"))
    return Observation(
        phase=phase if isinstance(phase, str) else None,
        reason=st.get("reason") if isinstance(st.get("reason"), str) else None,
        status_seen=status is not None,
        container=container,
        fallback_used=st.get("fallback_used") if isinstance(st.get("fallback_used"), dict) else None,
        crash_kit=st.get("crash_kit") if isinstance(st.get("crash_kit"), str) else None,
        phase_age_sec=(now - ts).total_seconds() if ts else None,
    )


# ---------------------------------------------------------------------------
# Тик
# ---------------------------------------------------------------------------


@dataclass
class Hooks:
    """Всё, что трогает внешний мир, — снаружи (запускатель даёт настоящее, тесты — поддельное)."""

    inspect: Callable[[str], ContainerState]
    read_status: Callable[[str], Optional[Dict[str, Any]]]
    stop: Callable[[str], None]
    relaunch: Callable[[Dict[str, Any], int], Optional[Dict[str, str]]]
    alert: Callable[[Dict[str, Any], str, int, Optional[str]], None]
    note_owner: Callable[[str], None]
    save_logs: Callable[[str], None] = lambda name: None


def _quiet_note_text(entry: Dict[str, Any], kind: str, detail: str) -> str:
    try:
        from notary.lib.join_alert import meeting_name, when_text, reason_human  # noqa: PLC0415

        name, when = meeting_name(entry), when_text(entry.get("start_at"))
    except Exception:  # noqa: BLE001
        name, when = str(entry.get("series") or "встреча"), ""
        reason_human = lambda r: r  # noqa: E731
    if kind == "second_try":
        return (
            f"ℹ️ Ватсон зашёл на «{name}» ({when}) со второй попытки; первая сорвалась: "
            f"{reason_human(detail)}. Если повторится — Телемост что-то меняет."
        )
    return (
        f"ℹ️ Ватсон зашёл на «{name}» ({when}) запасным путём: {detail}. "
        f"Телемост поменял вход — основной путь стоит поправить, бот пока справляется сам."
    )


def tick(ledger: Dict[str, Any], *, now: datetime, hooks: Hooks, allow_retry: bool = True) -> bool:
    """Один проход сторожа по журналу. Возвращает True, если журнал изменился."""
    changed = False
    limit = max_attempts()
    for key in list(ledger.keys()):
        entry = ledger.get(key)
        if not isinstance(entry, dict):
            ledger.pop(key, None)
            changed = True
            continue
        created = _parse_ts(entry.get("created_at")) or now
        if now - created > timedelta(hours=LEDGER_TTL_HOURS):
            ledger.pop(key, None)
            changed = True
            continue
        if entry.get("state") != "watching":
            continue
        attempts = entry.get("attempts") or []
        if not attempts:
            entry["state"] = "forgotten"
            changed = True
            continue
        cur = attempts[-1]
        attempt_no = int(cur.get("attempt") or len(attempts))
        name = str(cur.get("container") or "")
        uid = str(cur.get("session_uid") or "")
        launched_at = _parse_ts(cur.get("launched_at")) or now
        try:
            container = hooks.inspect(name)
        except Exception:  # noqa: BLE001
            container = ContainerState("unknown")
        status = hooks.read_status(uid) if uid else None
        obs = observe(status, container, now)
        d = decide(
            obs,
            attempt=attempt_no,
            max_attempts_=limit if allow_retry else attempt_no,
            launched_at=launched_at,
            start_at=_parse_ts(entry.get("start_at")),
            now=now,
        )
        if d.action == WAIT:
            continue
        changed = True
        if d.action == OK:
            entry["state"] = "ok"
            entry["closed_at"] = now.isoformat()
            if attempt_no > 1:
                prev = attempts[-2] if len(attempts) > 1 else {}
                hooks.note_owner(_quiet_note_text(entry, "second_try", str(prev.get("reason") or "join_error")))
            if obs.fallback_used:
                detail = ", ".join(f"{k}: {v}" for k, v in obs.fallback_used.items())[:300]
                hooks.note_owner(_quiet_note_text(entry, "fallback", detail))
            logger.info("[join-watch] %s: вход подтверждён (попытка %d)", key, attempt_no)
            continue
        if d.action == FORGET:
            entry["state"] = "forgotten"
            entry["closed_at"] = now.isoformat()
            logger.info("[join-watch] %s: слежу дальше не буду (%s)", key, d.reason)
            continue

        # Сорвалось.
        cur["reason"] = d.reason
        cur["crash_kit"] = obs.crash_kit
        if d.action in (STOP_RETRY, STOP_ALERT) and obs.status_seen:
            try:
                hooks.stop(name)
            except Exception as e:  # noqa: BLE001
                logger.warning("[join-watch] не смог остановить %s (%s)", name, type(e).__name__)
        try:
            hooks.save_logs(name)
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[join-watch] %s: вход сорвался (попытка %d, %s)", key, attempt_no, d.reason)

        if d.action in (RETRY, STOP_RETRY):
            launched = None
            try:
                launched = hooks.relaunch(entry, attempt_no + 1)
            except Exception as e:  # noqa: BLE001
                logger.warning("[join-watch] повтор %s упал (%s)", key, type(e).__name__)
            if launched:
                attempts.append({
                    "attempt": attempt_no + 1,
                    "session_uid": launched.get("session_uid"),
                    "container": launched.get("container"),
                    "launched_at": now.isoformat(),
                })
                entry["container"] = launched.get("container")
                logger.info("[join-watch] %s: повторная попытка %d запущена", key, attempt_no + 1)
                continue
            d = Decision(ALERT, "no_slot_for_retry" if d.reason not in NO_RETRY_REASONS else d.reason)
            cur["reason"] = cur.get("reason") or d.reason

        entry["state"] = "alerted"
        entry["closed_at"] = now.isoformat()
        entry["alert_reason"] = cur.get("reason") or d.reason
        try:
            hooks.alert(entry, str(cur.get("reason") or d.reason), len(attempts), obs.crash_kit)
        except Exception as e:  # noqa: BLE001
            logger.warning("[join-watch] сообщение о %s не ушло (%s)", key, type(e).__name__)
    return changed


# ---------------------------------------------------------------------------
# Уборка диагностики
# ---------------------------------------------------------------------------


def prune_diagnostics(transcripts_dir: Path, *, now_ts: Optional[float] = None) -> int:
    """Убрать старые статусы и аварийные снимки. Записи встреч не трогает никогда."""
    now_ts = now_ts or time.time()
    removed = 0
    status_dir = transcripts_dir / ".join-status"
    kits_dir = transcripts_dir / ".crash-kits"
    try:
        for p in status_dir.glob("*.json"):
            if now_ts - p.stat().st_mtime > STATUS_TTL_DAYS * 86400:
                p.unlink(missing_ok=True)
                removed += 1
    except OSError:
        pass
    try:
        for d in kits_dir.iterdir():
            if not d.is_dir() or now_ts - d.stat().st_mtime <= CRASH_KIT_TTL_DAYS * 86400:
                continue
            for f in d.iterdir():
                if f.is_file():
                    f.unlink(missing_ok=True)
            d.rmdir()
            removed += 1
    except OSError:
        pass
    return removed
