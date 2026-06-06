"""Шлюз правок в чате (фича «правки реплаем», Ф3): FB1–FB4 + задел FB12.

Поток:
  reply в чате серии → шлюз `route_feedback_reply`:
    1. FB1 — реагируем ТОЛЬКО если `reply_to_message.message_id ∈ meta.delivered`
       этого чата (см. `find_delivered_protocol`). Прочее (не-reply, @-упоминание,
       reply на чужое) — drop с логом «не reply на протокол — пропуск».
    2. FB2 — опознаём автора (`message.from` → имя через expectedParticipants /
       people.md, иначе профиль) и шлём ack.
    3. FB3 — персистентное окно-дебаунс: старт от первой правки, новая правка
       сбрасывает таймер на `FEEDBACK_WINDOW_MIN`, но не дольше потолка
       `FEEDBACK_MAX_WINDOW_MIN` от первой правки → сигнал «перевыпускать».
    4. FB4 — состояние на диске (`feedback_state`), переживает рестарт listener.

Перевыпуск протокола и применение правок к содержанию — НЕ здесь (это Ф4).
Ф3 только собирает правки и переводит state в `ready_for_reissue` по истечении
окна (sweep). Голос (FB9) — Ф5: правка без текста дропается с логом.

Модуль намеренно лёгкий (только stdlib + `feedback_state` + `telegram_api`),
без импорта `llm_postprocess` — грузится под системным python3 без заглушек.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import feedback_state
from . import telegram_api


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# env
# --------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def window_min() -> int:
    """FEEDBACK_WINDOW_MIN — длина окна-дебаунса от последней правки (дефолт 20)."""
    return _env_int("FEEDBACK_WINDOW_MIN", 20)


def max_window_min() -> int:
    """FEEDBACK_MAX_WINDOW_MIN — потолок окна от ПЕРВОЙ правки (дефолт 120).

    Инвариант: не меньше `window_min()` (иначе окно закрывалось бы раньше старта
    дебаунса). Если env задан меньше — поднимаем до window_min.
    """
    return max(_env_int("FEEDBACK_MAX_WINDOW_MIN", 120), window_min())


def is_enabled() -> bool:
    """Гейт ENABLE_FEEDBACK_EDITS (дефолт ON; `0/false/no` → OFF)."""
    raw = (os.environ.get("ENABLE_FEEDBACK_EDITS") or "").strip().lower()
    return raw not in ("0", "false", "no")


# --------------------------------------------------------------------------
# Поиск доставленного протокола по (chat_id, message_id) — FB1
# --------------------------------------------------------------------------

def _normalize_delivered(raw) -> list[dict]:
    """Копия `llm_postprocess._normalize_delivered` (без heavy-импорта).

    Старый формат — один объект `{chat_id, message_ids, at, ...}`; новый — массив
    таких объектов. Не-dict/не-list → [].
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _delivered_roots() -> list[Path]:
    """Каталоги, где finalize/delivery пишут meta.json с полем `delivered`.

    env `MEETING_NOTARY_DELIVERED_ROOTS` (os.pathsep-разделённый) переопределяет.
    Дефолт: `$TELEMOST_PROTOCOL_DIR` (куда finalize кладёт `<series>/meta.json`)
    + каталог архива встреч. Несуществующие пропускаются при скане.
    """
    env = os.environ.get("MEETING_NOTARY_DELIVERED_ROOTS")
    if env:
        parts = [p for p in env.split(os.pathsep) if p.strip()]
    else:
        parts = [
            os.environ.get("TELEMOST_PROTOCOL_DIR") or "/opt/meeting-notary/_tmp/protocols",
            os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR") or "~/Projects/me/встречи",
        ]
    seen: dict[str, None] = {}
    out: list[Path] = []
    for p in parts:
        ap = os.path.abspath(os.path.expanduser(p))
        if ap not in seen:
            seen[ap] = None
            out.append(Path(ap))
    return out


_META_FILE_RE = re.compile(r"(^meta\.json$)|(\.meta\.json$)")
_SCAN_FILE_LIMIT = 5000


def _iter_meta_files(root: Path, max_age_days: Optional[int]):
    """meta.json / *.meta.json под `root`, моложе `max_age_days` (None → без фильтра).

    Пропускаем скрытые и служебные подкаталоги (`.`-/`_`-префикс: `_versions`,
    `_transcripts`, `_pending_*` и т.п.) — там нет `delivered`.
    """
    cutoff = (time.time() - max_age_days * 86400) if max_age_days else None
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "_"))]
        for fn in filenames:
            if not _META_FILE_RE.search(fn):
                continue
            p = Path(dirpath) / fn
            try:
                if cutoff is not None and p.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            yield p
            count += 1
            if count >= _SCAN_FILE_LIMIT:
                logger.warning("[feedback] scan limit %d hit under %s — обрываю", _SCAN_FILE_LIMIT, root)
                return


def _read_json(path: Path) -> Optional[dict]:
    try:
        import json  # локально — модуль лёгкий, но импорт держим рядом с использованием
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def _build_delivered_index(roots: list[Path], max_age_days: Optional[int]) -> dict:
    """{(chat_id, message_id): meeting-dict} по всем meta.json в `roots`."""
    index: dict[tuple, dict] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for mp in _iter_meta_files(root, max_age_days):
            meta = _read_json(mp)
            if meta is None:
                continue
            date = meta.get("date") or (meta.get("startTs") or "")[:10]
            for rec in _normalize_delivered(meta.get("delivered")):
                cid = rec.get("chat_id")
                if cid is None:
                    continue
                try:
                    cid_i = int(cid)
                except (TypeError, ValueError):
                    continue
                for mid in (rec.get("message_ids") or []):
                    try:
                        mid_i = int(mid)
                    except (TypeError, ValueError):
                        continue
                    index[(cid_i, mid_i)] = {
                        "series": meta.get("series"),
                        "date": date,
                        "chat_id": cid_i,
                        "meta_path": str(mp),
                        "message_ids": list(rec.get("message_ids") or []),
                        "meta": meta,
                    }
    return index


# TTL-кэш индекса: всплеск reply'ев не пере-сканирует диск каждый раз.
_INDEX_CACHE: dict[str, Any] = {"built_at": 0.0, "roots": None, "index": {}}
_INDEX_TTL_S = 15.0
_DEFAULT_MAX_AGE_DAYS = 45


def find_delivered_protocol(
    chat_id: Any,
    message_id: Any,
    *,
    roots: Optional[list[Path]] = None,
    max_age_days: Optional[int] = _DEFAULT_MAX_AGE_DAYS,
    use_cache: bool = True,
    now_ts: Optional[float] = None,
) -> Optional[dict]:
    """FB1: встреча, чей `meta.delivered` содержит (chat_id, message_id), или None.

    Возвращает dict {series, date, chat_id, meta_path, message_ids, meta}.
    В тестах: `roots=[tmp]`, `use_cache=False`.
    """
    if chat_id is None or message_id is None:
        return None
    try:
        key = (int(chat_id), int(message_id))
    except (TypeError, ValueError):
        return None
    roots = roots if roots is not None else _delivered_roots()
    roots_key = tuple(str(r) for r in roots)
    now_ts = now_ts if now_ts is not None else time.time()
    if (
        use_cache
        and _INDEX_CACHE["roots"] == roots_key
        and (now_ts - _INDEX_CACHE["built_at"]) < _INDEX_TTL_S
    ):
        index = _INDEX_CACHE["index"]
    else:
        index = _build_delivered_index(roots, max_age_days)
        # Диагностика Ф9 (У2): тихий режим отказа фичи — listener сканирует не тот
        # каталог (env roots мимо реального meta.delivered) → индекс пуст, ни один
        # reply не матчится. Лог на DEBUG показывает «сканирую X, нашёл N».
        logger.debug(
            "[feedback] индекс delivered перестроен: roots=%s записей=%d",
            [str(r) for r in roots], len(index),
        )
        if use_cache:
            _INDEX_CACHE.update(built_at=now_ts, roots=roots_key, index=index)
    return index.get(key)


# --------------------------------------------------------------------------
# Опознание автора — FB2
# --------------------------------------------------------------------------

def _profile_name(from_user: dict) -> str:
    """Имя из Telegram-профиля: «Имя Фамилия» / «@username» / «участник <id>»."""
    if not isinstance(from_user, dict):
        return "участник"
    first = (from_user.get("first_name") or "").strip()
    last = (from_user.get("last_name") or "").strip()
    full = (first + " " + last).strip()
    if full:
        return full
    uname = (from_user.get("username") or "").strip()
    if uname:
        return "@" + uname.lstrip("@")
    uid = from_user.get("id")
    return f"участник {uid}" if uid is not None else "участник"


_PEOPLE_BOLD_RE = re.compile(r"\*\*([^*\n]{2,60}?)\*\*")


def parse_people_md(path: Optional[str]) -> list[str]:
    """Имена из people.md: жирные токены `**Имя Фамилия**`. Best-effort, [] при сбое.

    Формат файла — прозаический список людей (см. `~/Projects/me/people.md`):
    каждый человек начинается с `**Имя**`. Парсер толерантный: берёт жирные
    токены, в которых есть буква. Отсутствие/непарсабельность файла → [].
    """
    if not path:
        return []
    try:
        text = Path(os.path.expanduser(path)).read_text(encoding="utf-8")
    except OSError:
        return []
    names: list[str] = []
    for m in _PEOPLE_BOLD_RE.finditer(text):
        cand = m.group(1).strip()
        if cand and re.search(r"[A-Za-zА-Яа-яЁё]", cand):
            names.append(cand)
    return list(dict.fromkeys(names))


def _people_md_path() -> Optional[str]:
    """env `MEETING_NOTARY_PEOPLE_MD` > meeting-notary/people.md > ~/Projects/me/people.md."""
    env = os.environ.get("MEETING_NOTARY_PEOPLE_MD")
    if env:
        return env
    for cand in ("~/Projects/meeting-notary/people.md", "~/Projects/me/people.md"):
        if Path(os.path.expanduser(cand)).is_file():
            return cand
    return None


def resolve_author(
    from_user: dict,
    *,
    expected_participants: Optional[list] = None,
    people_md_names: Optional[list] = None,
) -> str:
    """FB2: каноничное имя автора правки.

    Приоритет: точное совпадение полного имени → совпадение по first-name в
    курируемом списке (expectedParticipants, затем people.md) → профиль. Тёзки
    (2+ кандидата по first-name) — НЕ угадываем (строгий 1:1, как в репо),
    падаем в профиль.
    """
    profile = _profile_name(from_user)
    first = (from_user.get("first_name") or "").strip() if isinstance(from_user, dict) else ""
    full_l = profile.lower()
    is_profile_handle = profile.startswith("@") or profile.startswith("участник")

    pools = []
    if expected_participants:
        pools.append([str(n).strip() for n in expected_participants if n])
    if people_md_names:
        pools.append([str(n).strip() for n in people_md_names if n])

    for pool in pools:
        if not is_profile_handle:
            for name in pool:
                if name.lower() == full_l:
                    return name
        if first:
            fl = first.lower()
            matches = [n for n in pool if n.split() and n.split()[0].lower() == fl]
            if len(matches) == 1:
                return matches[0]
            # 2+ тёзки → неоднозначно, не угадываем; следующий pool / профиль.
    return profile


# --------------------------------------------------------------------------
# ack-тексты
# --------------------------------------------------------------------------

def ack_first(author: str, win_min: int) -> str:
    """FB2: ack на ПЕРВУЮ правку раунда."""
    return (
        f"✅ Замечание принял, {author}. Жду {win_min} минут — если будут ещё правки, "
        f"ответьте на протокол; потом пришлю обновлённую версию."
    )


def ack_more(author: str) -> str:
    """FB3: ack на каждую последующую правку в окне."""
    return f"✅ Принял, {author}."


# --------------------------------------------------------------------------
# Окно / дебаунс — FB3 (чистая логика, без IO)
# --------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_deadlines(
    window_started: datetime, last_edit: datetime, win_min: int, max_min: int
) -> tuple[datetime, datetime]:
    """(deadline_at, hard_deadline_at).

    soft = last_edit + win_min; hard = window_started + max_min;
    deadline = min(soft, hard) — потолок от первой правки не даёт болтливому треду
    откладывать перевыпуск бесконечно (РИСК4).
    """
    soft = last_edit + timedelta(minutes=win_min)
    hard = window_started + timedelta(minutes=max_min)
    return min(soft, hard), hard


def _new_round_state(meeting: dict, edit: dict, win_min: int, max_min: int,
                     now: datetime, rnd: int, prev: Optional[dict]) -> dict:
    deadline, hard = _compute_deadlines(now, now, win_min, max_min)
    fid = feedback_state.build_feedback_id(meeting.get("series"), meeting.get("date"), meeting.get("chat_id"))
    return {
        "feedback_id": fid,
        "series": meeting.get("series"),
        "date": meeting.get("date"),
        "chat_id": meeting.get("chat_id"),
        "meta_path": meeting.get("meta_path"),
        "protocol_message_ids": list(meeting.get("message_ids") or []),
        "round": rnd,
        "window_min": win_min,
        "max_window_min": max_min,
        "window_started_at": _iso(now),
        "last_edit_at": _iso(now),
        "deadline_at": _iso(deadline),
        "hard_deadline_at": _iso(hard),
        "edits": [edit],
        "status": "collecting",
        "created_at": (prev.get("created_at") if prev else _iso(now)),
    }


def apply_edit(
    state: Optional[dict],
    *,
    edit: dict,
    meeting: dict,
    win_min: int,
    max_min: int,
    now: datetime,
) -> tuple[dict, str]:
    """Чистый переход состояния. Возвращает (new_state, kind).

    kind: "first" (старт раунда — первая правка), "more" (правка в окне),
    "dup" (повторная доставка той же правки — без изменений).
    """
    # Новый раунд: state нет, либо предыдущий уже закрыт (FB12 — сон/пробуждение).
    if state is None or state.get("status") in ("ready_for_reissue", "dormant"):
        rnd = (int(state.get("round", 1)) + 1) if state else 1
        return _new_round_state(meeting, edit, win_min, max_min, now, rnd, state), "first"

    # status == collecting → дозапись в открытое окно.
    edits = state.setdefault("edits", [])
    tmid = edit.get("tg_message_id")
    if tmid is not None and any(e.get("tg_message_id") == tmid for e in edits):
        return state, "dup"
    edits.append(edit)
    state["last_edit_at"] = _iso(now)
    started = feedback_state._parse_iso(state.get("window_started_at")) or now
    cur_win = int(state.get("window_min", win_min))
    cur_max = int(state.get("max_window_min", max_min))
    deadline, hard = _compute_deadlines(started, now, cur_win, cur_max)
    state["deadline_at"] = _iso(deadline)
    state["hard_deadline_at"] = _iso(hard)
    return state, "more"


# --------------------------------------------------------------------------
# Точка входа шлюза (IO) — вызывается из meetings_listener
# --------------------------------------------------------------------------

def _build_edit(msg: dict, author: str, now: datetime) -> dict:
    from_user = msg.get("from") or {}
    reply_to = msg.get("reply_to_message") or {}
    return {
        "edit_id": f"e-{msg.get('message_id')}",
        "tg_message_id": msg.get("message_id"),
        "reply_to_message_id": reply_to.get("message_id"),
        "from_user_id": from_user.get("id"),
        "author": author,
        "text": (msg.get("text") or "").strip(),
        "at": _iso(now),
    }


def handle_feedback_reply(
    token: str,
    chat_id: int,
    msg: dict,
    *,
    meeting: dict,
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
    send=None,
) -> dict:
    """Записывает правку в state + шлёт ack. Возвращает итоговый state.

    `send` (по умолчанию `telegram_api.send_message`) инъектируется в тестах.
    Текст правки НЕ логируем (только author/kind/round) — гигиена приватности.
    """
    root = root or feedback_state.resolve_feedback_dir()
    now = now or datetime.now(timezone.utc)
    send = send or telegram_api.send_message
    win = window_min()
    mx = max_window_min()

    from_user = msg.get("from") or {}
    meta = meeting.get("meta") or {}
    expected = meta.get("expectedParticipants") or meta.get("participants") or []
    author = resolve_author(
        from_user,
        expected_participants=expected,
        people_md_names=parse_people_md(_people_md_path()),
    )
    edit = _build_edit(msg, author, now)

    fid = feedback_state.build_feedback_id(meeting.get("series"), meeting.get("date"), meeting.get("chat_id"))
    state = feedback_state.read_state(fid, root=root)
    prior_status = state.get("status") if isinstance(state, dict) else None
    prior_edits = len(state.get("edits") or []) if isinstance(state, dict) else 0
    new_state, kind = apply_edit(state, edit=edit, meeting=meeting, win_min=win, max_min=mx, now=now)

    # Н1 (race Ф3/Ф4): новый раунд поверх ЕЩЁ НЕ перевыпущенного ready_for_reissue
    # затирает несъеденные правки прошлого раунда. В Ф3 (без Ф4) не наблюдаемо, но
    # как только появится Ф4 — это потеря данных в окне между закрытием окна и
    # reissue. Не делаем тихо: сигналим, чтобы Ф4 реализовала атомарный «claim».
    if kind == "first" and prior_status == "ready_for_reissue" and prior_edits:
        logger.warning(
            "[feedback] новый раунд поверх неперевыпущенного ready_for_reissue "
            "(fid=%s было_правок=%d) — правки прошлого раунда не применены (нет Ф4 "
            "или race перед reissue); Ф4 обязана атомарно забирать state до перевыпуска",
            fid, prior_edits,
        )

    if kind == "dup":
        logger.info("[feedback] дубль правки tg_message_id=%s fid=%s — ack не шлём", edit.get("tg_message_id"), fid)
        return new_state

    feedback_state.write_state(new_state, root=root)
    text = ack_first(author, int(new_state.get("window_min", win))) if kind == "first" else ack_more(author)
    try:
        send(token, chat_id, text, reply_to_message_id=msg.get("message_id"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[feedback] ack send failed (non-fatal): %s", e)
    logger.info(
        "[feedback] правка записана fid=%s round=%s kind=%s author=%s edits=%d deadline=%s",
        fid, new_state.get("round"), kind, author, len(new_state.get("edits", [])), new_state.get("deadline_at"),
    )
    return new_state


def route_feedback_reply(
    token: str,
    chat_id: int,
    msg: dict,
    *,
    allowed_chat: Optional[int],
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> bool:
    """FB1-шлюз. Возвращает True если сообщение «прожёвано» фичей правок
    (приложили правку ИЛИ осознанно дропнули в групповом чате), иначе False —
    caller продолжает свой dispatch (DM-flow).

    Решение:
      • reply на доставленный протокол + есть текст → правка (ack + state) → True.
      • reply на протокол, но голос/без текста → Ф5 (голос): в группе drop+True,
        в DM → False (отдаём старому voice/clarify-flow).
      • не reply на протокол: в DM → False; в группе → drop с логом FB1 + True.
    """
    if not is_enabled():
        return False
    reply_to = msg.get("reply_to_message")
    meeting = None
    if isinstance(reply_to, dict):
        meeting = find_delivered_protocol(chat_id, reply_to.get("message_id"))

    if meeting:
        text = (msg.get("text") or "").strip()
        if msg.get("voice") or msg.get("audio") or not text:
            # FB9 (голос) — Ф5. Ф3 принимает только текст.
            if chat_id != allowed_chat:
                logger.info("[feedback] правка без текста (голос/вложение) — Ф5, пропуск chat=%s", chat_id)
                return True
            return False  # DM: пусть отработает существующий voice/clarify-flow
        try:
            handle_feedback_reply(token, chat_id, msg, meeting=meeting, root=root, now=now)
        except Exception as e:  # noqa: BLE001
            logger.exception("[feedback] handle_feedback_reply упал: %s", e)
        return True

    # Не reply на наш протокол.
    if chat_id == allowed_chat:
        return False  # DM — отдаём существующим flow (📅 / 🎙 / команды)
    logger.info(
        "[feedback] не reply на протокол — пропуск (chat=%s is_reply=%s)",
        chat_id, bool(reply_to),
    )
    return True


# --------------------------------------------------------------------------
# Sweep окна → сигнал перевыпуска (FB3 потолок/дебаунс) — вызывается listener'ом
# --------------------------------------------------------------------------

def sweep_timeouts(root: Optional[Path] = None, *, now: Optional[datetime] = None) -> int:
    """collecting, у кого окно закрылось (`now >= deadline_at`) → ready_for_reissue.

    `ready_for_reissue` — сигнал Ф4 «забери edits и перевыпусти». Возвращает
    число переведённых. Сам перевыпуск (Ф4) тут НЕ делается.
    """
    root = root or feedback_state.resolve_feedback_dir()
    n = 0
    for state in feedback_state.list_states(root=root, status_filter=["collecting"]):
        if feedback_state.is_window_expired(state, now=now):
            feedback_state.mark_status(
                state["feedback_id"], "ready_for_reissue", root=root,
                extra={"window_closed_at": feedback_state.now_iso()},
            )
            logger.info(
                "[feedback] окно закрылось → ready_for_reissue fid=%s round=%s edits=%d",
                state.get("feedback_id"), state.get("round"), len(state.get("edits", [])),
            )
            n += 1
    return n


def has_any_active(root: Optional[Path] = None) -> bool:
    """True если есть collecting / ready_for_reissue (для диагностики/роутинга)."""
    root = root or feedback_state.resolve_feedback_dir()
    return bool(feedback_state.list_states(root=root, status_filter=["collecting", "ready_for_reissue"]))
