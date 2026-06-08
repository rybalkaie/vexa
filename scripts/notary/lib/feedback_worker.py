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
окна (sweep). Голос/аудио правки (FB9, Ф5) — транскрибируются reuse'ом
задеплоенного Groq-стека (`_transcribe_feedback_voice` → `voice_input.voice_to_text`)
и идут ТЕМ ЖЕ путём, что текст (anti-injection/sanitize наследуются от Ф4);
не распозналось/пусто → ack «пришли текстом», без молчаливого дропа.

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


def series_allowlist_enabled() -> bool:
    """Гейт FB8 `ENABLE_FEEDBACK_ALLOWLIST` (дефолт OFF; `1/true/yes/on` → ON).

    Дефолт OFF намеренно: владелец решил 05.06 «правят все» — текущее прод-поведение
    НЕ меняем без явного включения. Ф9 включит одним env, когда нужно ограничить
    правки кругом участников серии. Это критично именно с Ф6: rogue-правка персистит
    как выученный терм через будущие протоколы серии, а не правит один протокол.
    Семантика «включатель», а НЕ kill-switch — поэтому дефолт OFF, в отличие от
    `ENABLE_FEEDBACK_EDITS`/`ENABLE_FEEDBACK_LEARNING` (там флаг — аварийный тумблер).
    """
    raw = (os.environ.get("ENABLE_FEEDBACK_ALLOWLIST") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def max_edits_per_window() -> int:
    """FM-13: кап числа правок в одном окне сбора (env `FEEDBACK_MAX_EDITS_PER_WINDOW`).

    Дефолт 30 — щедро для реальной встречи (правок редко >10), но режет флуд/спам.
    Кап ОБЩИЙ на окно: покрывает и текст, и голос. Каждая голос-правка = транскрипция
    Groq (деньги), поэтому ограничение частоты реплаев ограничивает и стоимость STT,
    а не только размер списка `edits`.
    """
    return _env_int("FEEDBACK_MAX_EDITS_PER_WINDOW", 30)


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


def _author_pools(
    expected_participants: Optional[list], people_md_names: Optional[list]
) -> list[list[str]]:
    """Курируемые пулы имён для матчинга автора: expectedParticipants, затем people.md."""
    pools: list[list[str]] = []
    if expected_participants:
        pools.append([str(n).strip() for n in expected_participants if n])
    if people_md_names:
        pools.append([str(n).strip() for n in people_md_names if n])
    return pools


def _find_registry_match(from_user: dict, pools: list[list[str]]) -> Optional[str]:
    """Каноничное имя автора из курируемых пулов, либо None если не сматчился.

    Приоритет: точное совпадение полного имени → уникальное совпадение по first-name.
    Тёзки (2+ кандидата по first-name) — НЕ угадываем (строгий 1:1), пробуем
    следующий пул. None = автора в реестре нет (для allowlist это «не вправе слать
    правки»).

    ЕДИНЫЙ matcher для `resolve_author` (имя для ack/лога) и `author_allowed`
    (bool-гейт FB8) — нарочно один источник правды: иначе опознание автора и
    allowlist-гейт могли бы разъехаться (дыра: гейт пускает не того, кого опознали,
    или режет того, кого пускает атрибуция).
    """
    if not isinstance(from_user, dict):
        return None
    profile = _profile_name(from_user)
    full_l = profile.lower()
    is_profile_handle = profile.startswith("@") or profile.startswith("участник")
    first = (from_user.get("first_name") or "").strip()
    fl = first.lower()
    for pool in pools:
        if not is_profile_handle:
            for name in pool:
                if name.lower() == full_l:
                    return name
        if first:
            matches = [n for n in pool if n.split() and n.split()[0].lower() == fl]
            if len(matches) == 1:
                return matches[0]
            # 2+ тёзки → неоднозначно, не угадываем; следующий pool.
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
    pools = _author_pools(expected_participants, people_md_names)
    return _find_registry_match(from_user, pools) or _profile_name(from_user)


# --------------------------------------------------------------------------
# Allowlist авторов — FB8 (Ф7): кто вправе слать правки в серии
# --------------------------------------------------------------------------

def _explicit_user_id_whitelist(meeting: dict) -> set:
    """Опц. явный per-series whitelist user_id (задел FB8) — ПОВЕРХ авто-реестра.

    Источники объединяются: `meta.feedbackAllowlistUserIds` конкретной встречи +
    глобальный env `FEEDBACK_ALLOWLIST_USER_IDS` (comma/space-разделённый). Не-int
    элементы тихо игнорируются. Пусто → set() (тогда работает только авто-реестр).
    Это конфиг-канал (meta/env), НЕ хардкод — как требует задел FB8.
    """
    out: set = set()

    def _ingest(raw):
        if raw is None:
            return
        items = raw if isinstance(raw, (list, tuple, set)) else re.split(r"[,\s]+", str(raw))
        for it in items:
            s = str(it).strip()
            if not s:
                continue
            try:
                out.add(int(s))
            except (TypeError, ValueError):
                continue

    meta = (meeting or {}).get("meta") or {}
    _ingest(meta.get("feedbackAllowlistUserIds"))
    _ingest(os.environ.get("FEEDBACK_ALLOWLIST_USER_IDS"))
    return out


def author_allowed(from_user: dict, meeting: dict) -> bool:
    """FB8: вправе ли автор слать правки в этой серии.

    Гейт `ENABLE_FEEDBACK_ALLOWLIST` ВЫКЛ (дефолт) → всегда True (прежнее поведение
    «правят все», решение владельца 05.06). ВКЛ → вправе только:
      1. user_id из явного per-series whitelist (`_explicit_user_id_whitelist`), ЛИБО
      2. автор, однозначно сматченный с реестром серии (expectedParticipants ∪
         people.md) ТЕМ ЖЕ `_find_registry_match`, что опознаёт автора для ack.
    Иначе False (не участник / не опознан → не вправе).

    ОГРАНИЧЕНИЕ (документировано, см. пакет сдачи): матчинг по first-name наследуется
    от resolve_author, поэтому тёзка участника с тем же first-name может пройти
    авто-реестр. Жёсткая защита от подмены — явный user_id whitelist (п.1). Allowlist —
    это ДОПОЛНИТЕЛЬНЫЙ слой ПЕРЕД anti-injection-рамкой Ф4 (правки = ДАННЫЕ), не замена.
    """
    if not series_allowlist_enabled():
        return True
    if not isinstance(from_user, dict):
        return False  # allowlist ON, автор не идентифицируется → не вправе
    uid = from_user.get("id")
    if uid is not None:
        explicit = _explicit_user_id_whitelist(meeting)
        if explicit:
            try:
                if int(uid) in explicit:
                    return True
            except (TypeError, ValueError):
                pass
    meta = (meeting or {}).get("meta") or {}
    expected = meta.get("expectedParticipants") or meta.get("participants") or []
    pools = _author_pools(expected, parse_people_md(_people_md_path()))
    return _find_registry_match(from_user, pools) is not None


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
# FB-now: команда «делай сразу» — перевыпуск без ожидания окна дебаунса
# --------------------------------------------------------------------------

# Явная императивная команда боту «не жди окно, перевыпусти сейчас». Нарочно
# ТРЕБУЕМ глагол-императив рядом со «сразу/сейчас» (или «не жди…») — голое «сразу»
# часто встречается в самой правке как ДАННЫЕ («Ольга внесла сразу на встрече»),
# и матчить его = ложно флэшить. Кейс владельца: диктует правки, в конце —
# «делай сразу» (отдельным реплаем или в хвосте правки). Работает и текстом, и
# голосом (голос → транскрипт → тот же handle_feedback_reply).
_FLUSH_RE = re.compile(
    r"\b(?:делай|сделай|применяй|примени|применить|перевыпусти|перевыпускай|"
    r"публикуй|опубликуй|отправляй|отправь|пришли|шли|выпускай|выпусти)\s+"
    r"(?:сразу|сейчас|уже|немедленно)\b"
    r"|\bсразу\s+(?:делай|перевыпуск\w*|применяй|публикуй|отправляй)\b"
    r"|\bне\s+жди(?:те)?(?:\s+\d+\s*мин\w*)?\b"
    r"|\bне\s+(?:надо|нужно)\s+ждать\b"
    r"|\bбез\s+ожидани\w*\b",
    re.IGNORECASE,
)


def wants_immediate_reissue(text: Optional[str]) -> bool:
    """True, если в тексте правки есть команда «перевыпусти сейчас, не жди окно»."""
    return bool(text) and bool(_FLUSH_RE.search(text))


def strip_flush_command(text: Optional[str]) -> str:
    """Убирает флэш-команду из текста, оставляя содержательную часть правки.

    «перепутал Марию и Татьяну, делай сразу» → «перепутал Марию и Татьяну».
    «делай сразу» → «» (чистая команда, без правки).
    """
    if not text:
        return ""
    cleaned = _FLUSH_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip(" \t\n.,;:—-")


def ack_immediate(author: str) -> str:
    """ack на правку с командой «делай сразу»: правку принял + перевыпускаю немедленно."""
    return f"✅ Принял, {author}. Перевыпускаю сейчас — обновлённую версию пришлю в ближайшую минуту."


def ack_flush(author: str) -> str:
    """ack на чистую команду «делай сразу» (без новой правки): перевыпускаю сейчас."""
    return f"🚀 Перевыпускаю сейчас, {author} — обновлённую версию пришлю в ближайшую минуту."


def ack_flush_noop(author: str) -> str:
    """ack на «делай сразу», когда нечего перевыпускать (нет накопленных правок)."""
    return (
        f"Пока нет накопленных правок, {author}. Ответьте на протокол с правкой — "
        f"и добавьте «делай сразу», если не нужно ждать окно."
    )


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
    # Новый раунд: state нет, либо предыдущий уже закрыт/перевыпускается.
    #   - ready_for_reissue / dormant — FB12 (сон → пробуждение новым reply);
    #   - reissuing — Ф4 уже «забрала» edits на перевыпуск (claim); reply
    #     обязан уйти в СЛЕДУЮЩИЙ раунд, а не дозаписаться в съедаемый список
    #     (Н1/FM-10). conditional-dormant в reissue-воркере не затрёт этот раунд.
    if state is None or state.get("status") in ("ready_for_reissue", "reissuing", "dormant"):
        rnd = (int(state.get("round", 1)) + 1) if state else 1
        return _new_round_state(meeting, edit, win_min, max_min, now, rnd, state), "first"

    # status == collecting → дозапись в открытое окно.
    edits = state.setdefault("edits", [])
    tmid = edit.get("tg_message_id")
    if tmid is not None and any(e.get("tg_message_id") == tmid for e in edits):
        return state, "dup"
    # FM-13: кап числа правок в окне. Сверх капа — дроп (НЕ аппендим; state без
    # изменений), kind="capped". Дедуп проверяем ДО капа: повторная доставка той же
    # правки не должна ни считаться в кап, ни вызывать «capped».
    if len(edits) >= max_edits_per_window():
        return state, "capped"
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

    fid = feedback_state.build_feedback_id(meeting.get("series"), meeting.get("date"), meeting.get("chat_id"))

    # FB-now: команда «делай сразу». Содержательную часть (если есть) оставляем как
    # правку, флэш-команду вырезаем, чтобы она не попала в инструкцию перевыпуска.
    raw_text = (msg.get("text") or "").strip()
    immediate = wants_immediate_reissue(raw_text)
    residual = strip_flush_command(raw_text) if immediate else raw_text
    if immediate and not residual:
        # Чистая команда без новой правки — не пишем edit, просто закрываем окно
        # текущего раунда «сейчас» (ближайший sweep → ready_for_reissue → перевыпуск).
        return _flush_collecting_round(
            token, chat_id, msg, fid=fid, author=author, root=root, now=now, send=send,
        )

    edit_msg = msg if residual == raw_text else {**msg, "text": residual}
    edit = _build_edit(edit_msg, author, now)

    state = feedback_state.read_state(fid, root=root)
    prior_status = state.get("status") if isinstance(state, dict) else None
    prior_edits = len(state.get("edits") or []) if isinstance(state, dict) else 0
    new_state, kind = apply_edit(state, edit=edit, meeting=meeting, win_min=win, max_min=mx, now=now)

    # Н1 (race Ф3/Ф4): новый раунд поверх ЕЩЁ НЕ заклеймленного ready_for_reissue
    # затёр бы несъеденные правки прошлого раунда. Ф4 закрывает это claim'ом
    # (`ready_for_reissue` → `reissuing` ДО чтения edits, см.
    # `feedback_reissue.process_ready_reissues`): после claim reply попадает в ветку
    # `reissuing` выше (новый раунд, без затирания). Остаточное окно — между
    # `sweep_timeouts` и claim в ОДНОМ проходе sweep (без обработки сообщений между
    # ними), т.е. на практике пустое. Если reissue-воркер отключён/упал и state
    # завис в `ready_for_reissue` — этот reply открывает новый раунд, и warning
    # фиксирует, что правки прошлого раунда так и не перевыпустились.
    if kind == "first" and prior_status == "ready_for_reissue" and prior_edits:
        logger.warning(
            "[feedback] новый раунд поверх неперевыпущенного ready_for_reissue "
            "(fid=%s было_правок=%d) — reissue-воркер не успел заклеймить (отключён/упал?); "
            "правки прошлого раунда не перевыпущены",
            fid, prior_edits,
        )

    if kind == "dup":
        logger.info("[feedback] дубль правки tg_message_id=%s fid=%s — ack не шлём", edit.get("tg_message_id"), fid)
        return new_state

    if kind == "capped":
        # FM-13: окно достигло капа — правку дропаем МОЛЧА (без ack). ack на каждую
        # сверх-капа правку сам стал бы вектором усиления спама: флудер шлёт N+1 →
        # бот отвечает N+1 раз, превращаясь в спамера и сжигая send-квоту. Собранные
        # правки (1..cap) перевыпустятся как есть — теряем только флуд сверх капа.
        # state не меняли → не пишем.
        logger.warning(
            "[feedback] кап правок в окне (%d) достигнут — правка дропнута fid=%s author=%s",
            max_edits_per_window(), fid, author,
        )
        return new_state

    if immediate:
        # FB-now: правка + «делай сразу» — закрываем окно немедленно (deadline=now),
        # ближайший sweep (≤30с) переведёт в ready_for_reissue → перевыпуск.
        new_state["deadline_at"] = _iso(now)
        new_state["hard_deadline_at"] = _iso(now)

    feedback_state.write_state(new_state, root=root)
    if immediate:
        text = ack_immediate(author)
    else:
        text = ack_first(author, int(new_state.get("window_min", win))) if kind == "first" else ack_more(author)
    try:
        send(token, chat_id, text, reply_to_message_id=msg.get("message_id"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[feedback] ack send failed (non-fatal): %s", e)
    logger.info(
        "[feedback] правка записана fid=%s round=%s kind=%s author=%s edits=%d deadline=%s immediate=%s",
        fid, new_state.get("round"), kind, author, len(new_state.get("edits", [])), new_state.get("deadline_at"), immediate,
    )
    return new_state


def _flush_collecting_round(
    token: str,
    chat_id: int,
    msg: dict,
    *,
    fid: str,
    author: str,
    root: Path,
    now: datetime,
    send,
) -> dict:
    """FB-now: чистая команда «делай сразу» (без новой правки) — закрыть окно сейчас.

    Если есть открытый раунд с правками — выставляем deadline=now (ближайший sweep
    перевыпустит). Если правок нет/раунд не collecting — ack «нечего перевыпускать».
    """
    state = feedback_state.read_state(fid, root=root)
    if isinstance(state, dict) and state.get("status") == "collecting" and (state.get("edits") or []):
        state["deadline_at"] = _iso(now)
        state["hard_deadline_at"] = _iso(now)
        feedback_state.write_state(state, root=root)
        text = ack_flush(author)
        logger.info(
            "[feedback] флэш-команда «делай сразу» — окно закрыто немедленно fid=%s edits=%d",
            fid, len(state.get("edits") or []),
        )
    else:
        text = ack_flush_noop(author)
        logger.info(
            "[feedback] флэш-команда без накопленных правок fid=%s status=%s",
            fid, state.get("status") if isinstance(state, dict) else None,
        )
    try:
        send(token, chat_id, text, reply_to_message_id=msg.get("message_id"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[feedback] флэш-ack send failed (non-fatal): %s", e)
    return state if isinstance(state, dict) else {}


def _transcribe_feedback_voice(token: str, msg: dict) -> Optional[str]:
    """FB9 (Ф5): голос/аудио правки → текст через УЖЕ задеплоенный Groq-стек.

    Reuse, НЕ новый STT: делегируем listener-обёртке `transcribe_voice_or_none`,
    которая зовёт `voice_input.voice_to_text` (transcribe-smart → прямой Groq).
    Импорт ленивый — модуль намеренно лёгкий (см. docstring), `voice_input`/
    listener тянут больше. None при любой неудаче (нет модуля / не скачалось /
    не расшифровалось / это `audio` — reuse-стек берёт только `voice`) → caller
    ответит фолбэком. Текст транскрипта НЕ логируем (reuse уже это соблюдает).
    """
    try:
        from notary.meetings_listener import transcribe_voice_or_none  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        logger.warning("[feedback] транскрибация недоступна (import): %s", type(e).__name__)
        return None
    try:
        return transcribe_voice_or_none(token, msg)
    except Exception as e:  # noqa: BLE001
        logger.exception("[feedback] транскрибация упала: %s", e)
        return None


def _send_voice_fallback(token: str, chat_id: int, msg: dict) -> None:
    """Ack «пришли текстом» реплаем на правку (reuse `VOICE_FALLBACK_MSG` listener'а)."""
    try:
        from notary.meetings_listener import VOICE_FALLBACK_MSG  # noqa: PLC0415
        text = VOICE_FALLBACK_MSG
    except Exception:  # noqa: BLE001
        text = "🎙 Голос пока не расшифровал. Ответь, пожалуйста, текстом."
    try:
        telegram_api.send_message(token, chat_id, text, reply_to_message_id=msg.get("message_id"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[feedback] фолбэк-ack send failed (non-fatal): %s", e)


def _window_at_cap(meeting: dict, root: Optional[Path]) -> bool:
    """FM-13: достигнут ли кап правок в АКТИВНОМ окне этой серии.

    Читает текущий state серии. True только если окно открыто (status=collecting) и
    число собранных правок ≥ капа. Закрытое/отсутствующее окно → False (входящая
    правка откроет новый раунд — кап там считается заново). Зовётся в
    `route_feedback_reply` ДО транскрипции голоса, чтобы голос-спам сверх капа не
    оплачивался транскрипцией Groq (текстовый сверх-кап ловит сам `apply_edit`).
    """
    root = root or feedback_state.resolve_feedback_dir()
    fid = feedback_state.build_feedback_id(
        meeting.get("series"), meeting.get("date"), meeting.get("chat_id")
    )
    state = feedback_state.read_state(fid, root=root)
    if not isinstance(state, dict) or state.get("status") != "collecting":
        return False
    return len(state.get("edits") or []) >= max_edits_per_window()


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
      • reply на протокол голосом/аудио (FB9, Ф5): и в DM, и в группе →
        транскрибируем reuse'ом Groq → дальше ТЕМ ЖЕ путём, что текст; не
        распозналось → ack «пришли текстом» + True. (Голос в личке включён
        2026-06-08 — владелец диктует правки голосом.)
      • reply на протокол без текста и без голоса (стикер/фото): в DM → False;
        в группе → молчаливый drop + True.
      • не reply на протокол: в DM → False; в группе → drop с логом FB1 + True.
    """
    if not is_enabled():
        return False
    reply_to = msg.get("reply_to_message")
    meeting = None
    if isinstance(reply_to, dict):
        meeting = find_delivered_protocol(chat_id, reply_to.get("message_id"))

    if meeting:
        from_user = msg.get("from") or {}
        # FB8 (Ф7): allowlist авторов — ЕДИНЫЙ chokepoint ДО ack/сбора/транскрипции/
        # самообучения. В группе автор вне реестра серии → молчаливый игнор (как
        # не-reply, FB1: ни ack, ни сбора). В DM (allowed_chat) автор = владелец,
        # доверенный канал — не гейтим. Стоит ДО _transcribe_feedback_voice, чтобы
        # голос-спам не-allowlist автора не оплачивался Groq (FM-13). Отсечение здесь
        # же закрывает вектор Ф6: дропнутая правка не дойдёт до handle_feedback_reply →
        # (sweep → reissue_one) → record_learning_from_edits, т.е. rogue НЕ станет
        # персистентным выученным термом (FB7×Ф6).
        if chat_id != allowed_chat and not author_allowed(from_user, meeting):
            logger.info(
                "[feedback] автор вне allowlist — пропуск (chat=%s series=%s uid=%s)",
                chat_id, meeting.get("series"), from_user.get("id"),
            )
            return True

        text = (msg.get("text") or "").strip()
        has_voice = bool(msg.get("voice") or msg.get("audio"))
        if has_voice or not text:
            # DM: НЕ-голос (стикер/фото/пустой текст) уходит существующему
            # voice/clarify-flow (Ф4) — не наш путь. Голос-правку (FB9) в личке
            # ОБРАБАТЫВАЕМ (решение владельца 2026-06-08: Илья диктует голосом,
            # текстом писать не хочет) — падает в блок `if has_voice:` ниже, как
            # и в группе. Автор в DM = владелец, allowlist-гейт уже пропустил по
            # chat_id == allowed_chat. Голос-реплай на clarify-сообщение 🎙 (НЕ
            # доставленный протокол) сюда не доходит: meeting=None → старый flow.
            if chat_id == allowed_chat and not has_voice:
                return False
            if has_voice:
                # FM-13: кап правок в окне — ПЕРЕД транскрипцией (Groq = деньги).
                # Голос-правка сверх капа дропается ДО оплаты STT (текстовая сверх-кап
                # дропается в apply_edit; голос ловим здесь, иначе платим за транскрипт
                # правки, которую всё равно отбросим).
                if _window_at_cap(meeting, root):
                    logger.warning(
                        "[feedback] кап правок в окне (%d) достигнут — голос-правка "
                        "дропнута ДО транскрипции (chat=%s)", max_edits_per_window(), chat_id,
                    )
                    return True
                # FB9 (Ф5): голосовая/аудио правка реплаем в чате серии (с 2026-06-08 —
                # и в личке владельца, и в группе; раньше только группа).
                # Транскрипт — те же недоверенные ДАННЫЕ, что текст правки: после
                # подстановки в msg["text"] он идёт через handle_feedback_reply →
                # apply_edit → build_edit_instruction (anti-injection + sanitize Ф4).
                transcript = _transcribe_feedback_voice(token, msg)
                if transcript and transcript.strip():
                    voice_msg = dict(msg)
                    voice_msg["text"] = transcript.strip()
                    voice_msg.pop("voice", None)
                    voice_msg.pop("audio", None)
                    try:
                        handle_feedback_reply(token, chat_id, voice_msg, meeting=meeting, root=root, now=now)
                    except Exception as e:  # noqa: BLE001
                        logger.exception("[feedback] handle_feedback_reply (голос) упал: %s", e)
                    return True
                # Не распозналось/пусто → ack «пришли текстом», без молчаливого дропа.
                _send_voice_fallback(token, chat_id, msg)
                logger.info("[feedback] голос/аудио не расшифрован → фолбэк «пришли текстом» chat=%s", chat_id)
                return True
            # Пусто и не голос (стикер/фото/документ) — прежний молчаливый drop.
            logger.info("[feedback] правка без текста (не голос) — пропуск chat=%s", chat_id)
            return True
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
