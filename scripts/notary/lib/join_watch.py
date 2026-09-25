"""Сторож входа и хода встречи (ISS-60, 23–24.09.2026).

ЧТО БЫЛО. Запускатель смотрел на контейнер бота один раз — через 3 секунды после
старта (`runner._verify_container_alive`). С 21 по 23.09.2026 бот падал на 40-й
секунде (Телемост 3.0 спрятал лобби во вложенное окно), и три дня ни одна встреча
не записалась без единой тревоги: тревога «пустая запись» живёт от `meta.json`,
а его бот пишет только после входа. Свои собственные тревоги (зал ожидания,
выброс, «оглох») бот слал сам — но токена Телеграма в контейнере не было, и они
никогда никуда не доходили.

ЧТО СТАЛО. Запускатель заносит каждый старт в журнал сторожа, а на каждом своём
тике (раз в минуту) сторож смотрит на статус, который пишет сам бот
(`<transcripts>/.join-status/<session_uid>.json`: joining → admission →
waiting_room → in_call / failed + причина, плюс события по ходу встречи), и на
состояние контейнера:

* вход удался → молча; дальше сторож доставляет события встречи (выкинуло и не
  вернулся, оглох) до выхода контейнера;
* сорвался вход → ОДНА повторная попытка новым контейнером; снова сорвался →
  сообщение туда, куда уходит протокол (`join_alert`), и разбор владельцу;
* завис на входе / в ожидании впуска дольше срока → остановить и повторить;
* ждёт в зале ожидания дольше 30 с → попросить людей впустить бота.

Сообщения шлёт ТОЛЬКО сторож: токена в контейнере с браузером нет намеренно
(браузер открывает чужие страницы), а адресат людям один на всё — маршрут
протокола.

🔴 СНАЧАЛА ЗАПИСЬ, ПОТОМ ДЕЙСТВИЕ. Перед каждым повтором и сообщением сторож
сохраняет журнал с отметкой «сделано». Не сохранился (диск, права) → действие не
выполняется, владельцу одно сообщение. Иначе при сломанной записи журнала группа
получала бы одно и то же сообщение каждую минуту до 12 часов.

🔴 СТАРЫЙ ОБРАЗ БЕЗ СТАТУСОВ. Пока образ не пишет статус, сторож никого не
останавливает: реагирует только на выход контейнера с кодом 1.

🔴 СТОРОЖ НЕ МЕШАЕТ ЗАПУСКУ. Всё вызывается запускателем под `try`.

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
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("notary.join_watch")


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def is_enabled() -> bool:
    """Выключатель: `MEETING_NOTARY_JOIN_WATCH=0`. По умолчанию включён —
    прямая просьба владельца 23.09.2026 («не молчать»)."""
    return (os.environ.get("MEETING_NOTARY_JOIN_WATCH") or "1").strip() not in {"0", "false", "no", "off"}


def max_attempts() -> int:
    return _env_int("MEETING_NOTARY_JOIN_MAX_ATTEMPTS", 2)


#: Фаза «вход» (лобби): нормальный вход — 30–135 с от запуска контейнера.
STUCK_JOINING_SEC = _env_int("MEETING_NOTARY_JOIN_STUCK_SEC", 300)
#: Фаза «жду впуска» (после «Подключиться»): бот сам ждёт до waitingRoomTimeout
#: (по умолчанию 300 с) и в конце может войти «методом исключения» — даём запас.
STUCK_ADMISSION_SEC = _env_int("MEETING_NOTARY_ADMISSION_STUCK_SEC", 420)
#: Зал ожидания бот бросает сам через 2,5 мин; это страховка от зависания.
STUCK_WAITING_SEC = _env_int("MEETING_NOTARY_JOIN_STUCK_WAITING_SEC", 600)
#: Через сколько в зале ожидания просить людей впустить бота.
WAITING_NUDGE_SEC = 30
#: Сорвавшийся бот выходит за секунды; если контейнер ещё жив через столько —
#: останавливаем его, чтобы не держать повтор.
FAILED_EXIT_GRACE_SEC = 90
#: «В звонке» засчитывается, только если продержалось столько: сразу после
#: in_call бот ещё раз проверяет вход и при промахе пишет failed.
IN_CALL_STABLE_SEC = 20
#: Упал с кодом 1 вскоре после входа — это сорванная встреча, а не её конец.
IN_CALL_CRASH_WINDOW_SEC = 300
#: Повторять вход имеет смысл до этого момента после старта встречи.
RETRY_DEADLINE_AFTER_START_MIN = 30
#: Старый образ без статусов — сколько следим за контейнером.
LEGACY_WATCH_SEC = 20 * 60
#: Ошибка docker — сколько ждём, прежде чем забыть.
UNKNOWN_FORGET_SEC = 30 * 60
#: Сколько живут записи журнала (длинная встреча укладывается).
LEDGER_TTL_HOURS = 12
#: Аварийные снимки и статусы старше — убираются (диагностика, не записи встреч).
CRASH_KIT_TTL_DAYS = 30
STATUS_TTL_DAYS = 7

#: Причины, где повтор бессмысленен: встречи нет, бота отклонили, ссылка не на
#: Телемост (фаза 3а, хвост С10 — бот её не открывает, повтор дал бы то же).
NO_RETRY_REASONS = frozenset({"meeting_not_found", "admission_rejected", "meeting_url_rejected"})
#: События по ходу встречи, о которых сторож сообщает.
MEETING_EVENTS = frozenset({"evicted_failure", "deaf_after_start"})

# Действия решения.
OK = "ok"
WAIT = "wait"
RETRY = "retry"
ALERT = "alert"
STOP_RETRY = "stop_retry"
STOP_ALERT = "stop_alert"
FORGET = "forget"

_GONE = {"exited", "missing", "dead"}
_RUNNING = {"running", "restarting", "created"}


@dataclass(frozen=True)
class ContainerState:
    #: running / exited / created / restarting / missing / paused / unknown
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
    events: tuple = ()


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
    """Что делать с одной попыткой входа (до подтверждённого входа). Чистая функция."""
    age = (now - launched_at).total_seconds()
    phase_age = obs.phase_age_sec if obs.phase_age_sec is not None else age
    retry_allowed = attempt < max_attempts_ and (
        start_at is None or now <= start_at + timedelta(minutes=RETRY_DEADLINE_AFTER_START_MIN)
    )

    def fail(reason: str, *, stop: bool = False) -> Decision:
        if reason in NO_RETRY_REASONS or not retry_allowed:
            return Decision(STOP_ALERT if stop else ALERT, reason)
        return Decision(STOP_RETRY if stop else RETRY, reason)

    status = obs.container.status
    running = status in _RUNNING
    gone = status in _GONE

    # 1. Бот сам сказал «сорвалось» — даже если перед этим мелькнуло «в звонке»
    #    (admission_false_positive пишется сразу после in_call).
    if obs.phase == "failed":
        reason = obs.reason or "join_error"
        if running:
            if phase_age >= FAILED_EXIT_GRACE_SEC:
                return fail(reason, stop=True)
            return Decision(WAIT, reason)
        return fail(reason)

    # 2. В звонке.
    if obs.phase == "in_call":
        if gone:
            # Упал с ошибкой почти сразу после входа — встреча не пишется.
            if obs.container.exit_code == 1 and phase_age < IN_CALL_CRASH_WINDOW_SEC:
                return fail("crashed_after_join")
            return Decision(OK)
        if running and phase_age >= IN_CALL_STABLE_SEC:
            return Decision(OK)
        return Decision(WAIT)  # свежий вход или docker не ответил

    # 3. Контейнера больше нет или он вышел, а в звонок не попал.
    if gone:
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
            if obs.phase == "admission":
                if phase_age >= STUCK_ADMISSION_SEC:
                    return fail("stuck_admission", stop=True)
                return Decision(WAIT)
            if age >= STUCK_JOINING_SEC:
                return fail("stuck_joining", stop=True)
            return Decision(WAIT)
        # Старый образ: никого не останавливаем.
        if age >= LEGACY_WATCH_SEC:
            return Decision(FORGET, "legacy_running")
        return Decision(WAIT)

    # 5. docker не ответил (или контейнер на паузе) — ждём, но не вечно.
    if age >= UNKNOWN_FORGET_SEC:
        return Decision(FORGET, "docker_unknown")
    return Decision(WAIT)


# ---------------------------------------------------------------------------
# Журнал стартов
# ---------------------------------------------------------------------------

#: Поля элемента очереди, нужные для повтора и сообщения. Ссылка на комнату
#: нужна для повтора; журнал живёт рядом с реестром на сервере (там же ссылки),
#: права 0600.
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
    """Атомарно; временный файл сразу с правами 0600 (в журнале ссылки на комнаты)."""
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
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
    events = st.get("events") if isinstance(st.get("events"), list) else []
    return Observation(
        phase=phase if isinstance(phase, str) else None,
        reason=st.get("reason") if isinstance(st.get("reason"), str) else None,
        status_seen=status is not None,
        container=container,
        fallback_used=st.get("fallback_used") if isinstance(st.get("fallback_used"), dict) else None,
        crash_kit=st.get("crash_kit") if isinstance(st.get("crash_kit"), str) else None,
        phase_age_sec=(now - ts).total_seconds() if ts else None,
        events=tuple(e for e in events if isinstance(e, dict)),
    )


# ---------------------------------------------------------------------------
# Тик
# ---------------------------------------------------------------------------


def _noop(*_a: Any, **_k: Any) -> None:
    return None


@dataclass
class Hooks:
    """Всё, что трогает внешний мир, — снаружи (запускатель даёт настоящее, тесты — поддельное)."""

    inspect: Callable[[str], ContainerState]
    read_status: Callable[[str], Optional[Dict[str, Any]]]
    stop: Callable[[str], None]
    #: Имя сессии и контейнера для попытки N — без побочных эффектов.
    plan_retry: Callable[[Dict[str, Any], int], Dict[str, str]]
    #: Запустить попытку N. True — контейнер поднят.
    relaunch: Callable[[Dict[str, Any], int], bool]
    #: Сорванный вход: (запись, причина, попыток, снимок, пометка для владельца).
    alert: Callable[[Dict[str, Any], str, int, Optional[str], Optional[str]], None]
    #: Событие встречи: (запись, тип, событие).
    event_alert: Callable[[Dict[str, Any], str, Dict[str, Any]], None]
    note_owner: Callable[[str], None]
    #: Сохранить журнал СЕЙЧАС (бросает при сбое) — до любого действия.
    persist: Callable[[Dict[str, Any]], None]
    #: Журнал не сохранился — одно сообщение владельцу (с глушилкой повторов).
    persist_failed: Callable[[str], None] = _noop
    save_logs: Callable[[str], None] = _noop


def _commit(ledger: Dict[str, Any], hooks: Hooks) -> bool:
    try:
        hooks.persist(ledger)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[join-watch] журнал не сохранён (%s) — действие отложено", type(e).__name__)
        try:
            hooks.persist_failed(
                "⚠️ Сторож Ватсона не может сохранить свой журнал на сервере — повторы входа "
                "и сообщения людям о сбоях сейчас не отправляются (чтобы не слать одно и то же "
                "каждую минуту). Проверь место на диске сервера."
            )
        except Exception:  # noqa: BLE001
            pass
        return False


def _safe(fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 — внешний мир не должен ронять тик
        logger.warning("[join-watch] внешний вызов упал (%s)", type(e).__name__)


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


def _event_key(ev: Dict[str, Any]) -> str:
    return f"{ev.get('type')}@{ev.get('at')}"


def _deliver_events(ledger: Dict[str, Any], entry: Dict[str, Any], obs: Observation, hooks: Hooks) -> bool:
    """Доставить новые события встречи. Возвращает True, если журнал менялся."""
    changed = False
    sent = entry.setdefault("events_sent", [])
    for ev in obs.events:
        et = ev.get("type")
        if et not in MEETING_EVENTS:
            continue
        key = _event_key(ev)
        if key in sent:
            continue
        sent.append(key)
        changed = True
        if not _commit(ledger, hooks):
            sent.remove(key)
            return changed
        _safe(hooks.event_alert, entry, et, ev)
    return changed


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
        state = entry.get("state")
        if state not in ("watching", "in_call"):
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
        try:
            status = hooks.read_status(uid) if uid else None
        except Exception:  # noqa: BLE001
            status = None
        obs = observe(status, container, now)

        # --- Вход уже подтверждён: доставляем события встречи до выхода бота.
        if state == "in_call":
            changed = _deliver_events(ledger, entry, obs, hooks) or changed
            if container.status in _GONE:
                entry["state"] = "done"
                entry["closed_at"] = now.isoformat()
                changed = True
            continue

        # --- Бот в зале ожидания: попросить людей впустить (один раз).
        if (
            obs.phase == "waiting_room"
            and (obs.phase_age_sec or 0) >= WAITING_NUDGE_SEC
            and not entry.get("waiting_nudged")
        ):
            entry["waiting_nudged"] = True
            changed = True
            if _commit(ledger, hooks):
                _safe(hooks.event_alert, entry, "waiting_room", {"type": "waiting_room"})
            else:
                entry["waiting_nudged"] = False

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
            entry["state"] = "in_call"
            entry["joined_at"] = now.isoformat()
            notes = []
            if attempt_no > 1:
                prev = attempts[-2] if len(attempts) > 1 else {}
                notes.append(_quiet_note_text(entry, "second_try", str(prev.get("reason") or "join_error")))
            if obs.fallback_used:
                detail = ", ".join(f"{k}: {v}" for k, v in obs.fallback_used.items())[:300]
                notes.append(_quiet_note_text(entry, "fallback", detail))
            if not _commit(ledger, hooks):
                entry["state"] = "watching"
                continue
            for text in notes:
                _safe(hooks.note_owner, text)
            logger.info("[join-watch] %s: вход подтверждён (попытка %d)", key, attempt_no)
            changed = _deliver_events(ledger, entry, obs, hooks) or changed
            if container.status in _GONE:
                entry["state"] = "done"
                entry["closed_at"] = now.isoformat()
            continue
        if d.action == FORGET:
            entry["state"] = "forgotten"
            entry["closed_at"] = now.isoformat()
            logger.info("[join-watch] %s: слежу дальше не буду (%s)", key, d.reason)
            continue

        # --- Сорвалось.
        cur["reason"] = d.reason
        cur["crash_kit"] = obs.crash_kit
        if d.action in (STOP_RETRY, STOP_ALERT) and obs.status_seen:
            _safe(hooks.stop, name)
            # Хвост У8 (фаза 3а): по сигналу остановки бот, ещё не бывший в
            # звонке, снимает экран и дописывает путь к снимку в статус —
            # берём его для разбора владельцу (`docker stop` ждёт выхода бота).
            if not cur.get("crash_kit") and uid:
                try:
                    after = hooks.read_status(uid)
                except Exception:  # noqa: BLE001
                    after = None
                kit = after.get("crash_kit") if isinstance(after, dict) else None
                if isinstance(kit, str) and kit:
                    cur["crash_kit"] = kit
        _safe(hooks.save_logs, name)
        logger.warning("[join-watch] %s: вход сорвался (попытка %d, %s)", key, attempt_no, d.reason)

        owner_note: Optional[str] = None
        if d.action in (RETRY, STOP_RETRY):
            nxt = attempt_no + 1
            planned = None
            try:
                planned = hooks.plan_retry(entry, nxt)
            except Exception as e:  # noqa: BLE001
                logger.warning("[join-watch] повтор %s не спланирован (%s)", key, type(e).__name__)
            if planned:
                attempts.append({
                    "attempt": nxt,
                    "session_uid": planned.get("session_uid"),
                    "container": planned.get("container"),
                    "launched_at": now.isoformat(),
                })
                entry["container"] = planned.get("container")
                if not _commit(ledger, hooks):
                    attempts.pop()
                    entry["container"] = name
                    continue
                launched = False
                try:
                    launched = bool(hooks.relaunch(entry, nxt))
                except Exception as e:  # noqa: BLE001
                    logger.warning("[join-watch] повтор %s упал (%s)", key, type(e).__name__)
                if launched:
                    logger.info("[join-watch] %s: повторная попытка %d запущена", key, nxt)
                    continue
                attempts.pop()
                entry["container"] = name
            owner_note = "повтор не запущен: не нашлось свободного места или запуск не удался"

        entry["state"] = "alerted"
        entry["closed_at"] = now.isoformat()
        entry["alert_reason"] = cur.get("reason") or d.reason
        if not _commit(ledger, hooks):
            entry["state"] = "watching"
            entry.pop("closed_at", None)
            continue
        _safe(hooks.alert, entry, str(cur.get("reason") or d.reason), len(attempts), cur.get("crash_kit"), owner_note)
    return changed


# ---------------------------------------------------------------------------
# Уборка диагностики
# ---------------------------------------------------------------------------


def prune_diagnostics(transcripts_dir: Path, *, now_ts: Optional[float] = None) -> int:
    """Убрать старые статусы и аварийные снимки. Записи встреч не трогает никогда.

    🔴 Папки наполняет контейнер (root, браузер на чужих страницах), а убирает
    пользователь сервера. Поэтому НИ ОДНОЙ символической ссылки не проходим и не
    удаляем по ней: иначе ссылка «.crash-kits/x → папка протоколов» стёрла бы
    протоколы (цикл5 С2). Сами папки тоже не должны быть ссылками.
    """
    now_ts = now_ts or time.time()
    removed = 0

    def _real_dir(p: Path) -> bool:
        try:
            return p.is_dir() and not p.is_symlink()
        except OSError:
            return False

    status_dir = transcripts_dir / ".join-status"
    if _real_dir(status_dir):
        try:
            with os.scandir(status_dir) as it:
                for de in it:
                    if not de.name.endswith(".json") or not de.is_file(follow_symlinks=False):
                        continue
                    if now_ts - de.stat(follow_symlinks=False).st_mtime > STATUS_TTL_DAYS * 86400:
                        os.unlink(de.path)
                        removed += 1
        except OSError:
            pass
    kits_dir = transcripts_dir / ".crash-kits"
    if _real_dir(kits_dir):
        try:
            with os.scandir(kits_dir) as it:
                for de in it:
                    if not de.is_dir(follow_symlinks=False):
                        continue
                    if now_ts - de.stat(follow_symlinks=False).st_mtime <= CRASH_KIT_TTL_DAYS * 86400:
                        continue
                    with os.scandir(de.path) as inner:
                        for f in inner:
                            if f.is_file(follow_symlinks=False) or f.is_symlink():
                                os.unlink(f.path)  # ссылку удаляем как ссылку, не цель
                    os.rmdir(de.path)
                    removed += 1
        except OSError:
            pass
    return removed
