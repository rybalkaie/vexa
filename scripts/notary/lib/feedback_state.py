"""Персистенция состояния сбора правок в чате (фича «правки реплаем», Ф3).

Один файл = одна встреча-в-чате: `<root>/<feedback_id>-feedback.json`, где
`feedback_id = fb-<series>-<date>-<chat_id>`. Атомарная запись через
`tempfile.mkstemp + os.rename` (как `clarify_state`), чтобы listener не прочитал
полузаписанный JSON.

🔴 ПИСАТЕЛЕЙ ДВА ПРОЦЕССА (с Ф10d): listener (правки реплаем, sweep, claim) и
шлюз (правки с портала). Атомарности `rename` для этого мало — она защищает от
полузаписи, но не от «прочитали оба, записали по очереди, первый затёрт».
Поэтому read→изменить→write идёт ТОЛЬКО через `mutate()`/`locked()` (flock на
`.<fid>.lock`), как в реестре нарядов `orders._mutate`.

Каталог СВОЙ (не `_pending_clarification/`) — env `MEETING_NOTARY_FEEDBACK_DIR`,
дефолт `/opt/meeting-notary/_feedback_edits` (VPS) или мак-путь. Изоляция
намеренная: clarify/delivery sweep'ы глобают `_pending_clarification/` и не должны
видеть наши state'ы (и наоборот).

Состояние:
    {
      "feedback_id":   "fb-coord-2026-06-02--1001234",
      "series":        "coord",
      "date":          "2026-06-02",
      "chat_id":       -1001234,
      "meta_path":     "/abs/path/<series>/meta.json",   # где лежит meta.delivered (для Ф4)
      "protocol_message_ids": [101, 102],   # message_id доставленного протокола, на который ответили
      "round":         1,                   # номер раунда (растёт при перевыпуске → новый сбор, FB12)
      "window_min":    20,                  # FEEDBACK_WINDOW_MIN на момент старта окна
      "max_window_min": 120,                # FEEDBACK_MAX_WINDOW_MIN (потолок)
      "window_started_at": "2026-06-02T12:00:00Z",   # = время ПЕРВОЙ правки раунда
      "last_edit_at":      "2026-06-02T12:05:00Z",
      "deadline_at":       "2026-06-02T12:25:00Z",   # min(last_edit+window, window_started+max)
      "hard_deadline_at":  "2026-06-02T14:00:00Z",   # window_started + max (потолок)
      "edits": [
        {
          "edit_id":            "e-555",
          "tg_message_id":      555,           # message_id самой правки (дедуп повторной доставки)
          "reply_to_message_id": 101,          # на какое сообщение протокола ответили
          "from_user_id":       777,
          "author":             "Михаил Саргин",
          "text":               "131 не под досмотром, а на доставке",
          "at":                 "2026-06-02T12:00:00Z"
        }, ...
      ],
      "status": "collecting" | "ready_for_reissue" | "dormant",
      "created_at": "...",
      "updated_at": "..."
    }

Статусы (контракт Ф3→Ф4):
  collecting        — окно открыто, копим правки. Когда `now >= deadline_at`
                      (естественный дебаунс или потолок) — sweep переводит в
                      `ready_for_reissue`.
  ready_for_reissue — окно закрылось; Ф4 должна забрать `edits`, пересобрать
                      протокол, дописать новый message_id в meta.delivered и
                      перевести state в `dormant`. В Ф3 (без Ф4) state остаётся
                      `ready_for_reissue` до следующего reply (тогда новый раунд).
  reissuing         — Ф4 «забрала» state на перевыпуск (claim, см.
                      `claim_for_reissue`). Промежуточный статус: пока он стоит,
                      конкурентный reply НЕ дозаписывается в съедаемые `edits`, а
                      открывает СЛЕДУЮЩИЙ раунд (Н1, FM-10 — защита от потери
                      правок в окне между закрытием окна и перевыпуском).
  dormant           — спит после перевыпуска; новый reply на любую версию серии
                      открывает новый раунд (FB12, многораундовость).
  failed            — терминальный провал: перевыпуск исчерпал MAX_REISSUE_ATTEMPTS
                      (claude/telegram стабильно падали). Item ВЫВЕДЕН из очереди
                      (sweep его больше не клеймит — иначе вечный silent-skip,
                      coordination-баг №3) И владелец получил one-shot уведомление
                      «не смог применить правки, вот они». Новый reply открывает
                      свежий раунд (apply_edit: failed → round+1), как из dormant.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional


logger = logging.getLogger(__name__)

DEFAULT_LOCAL_ROOT = os.path.expanduser("~/Projects/meeting-notary/_feedback_edits")
DEFAULT_VPS_ROOT = "/opt/meeting-notary/_feedback_edits"

STATE_SUFFIX = "-feedback.json"

VALID_STATUSES = ("collecting", "ready_for_reissue", "reissuing", "dormant", "failed")

# Н1 (FM-10): потолок попыток перевыпуска одного раунда. После него
# `process_ready_reissues` перестаёт клеймить state (claude/telegram стабильно
# падают) — оставляем `ready_for_reissue` владельцу/Ф9, не крутим claude в холостую.
# Новый reply всё равно откроет свежий раунд (apply_edit: ready_for_reissue → round+1).
MAX_REISSUE_ATTEMPTS = 3

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
_FEEDBACK_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def resolve_feedback_dir(*, override: Optional[str] = None) -> Path:
    """Корень `_feedback_edits/`.

    Приоритет: явный аргумент > env `MEETING_NOTARY_FEEDBACK_DIR` >
    дефолт (VPS если `/opt/meeting-notary` существует, иначе мак-путь).
    """
    if override:
        return Path(os.path.expanduser(override))
    env = os.environ.get("MEETING_NOTARY_FEEDBACK_DIR")
    if env:
        return Path(os.path.expanduser(env))
    if Path("/opt/meeting-notary").is_dir():
        return Path(DEFAULT_VPS_ROOT)
    return Path(DEFAULT_LOCAL_ROOT)


def _sanitize(part: str) -> str:
    """Любой символ вне `[A-Za-z0-9._-]` → `_`. Серия может быть кириллицей/с пробелами."""
    return _SAFE_ID_RE.sub("_", str(part or "").strip()) or "x"


def build_feedback_id(series: Optional[str], date: Optional[str], chat_id: Any) -> str:
    """Детерминированный id встречи-в-чате. Один и тот же для всех раундов (FB12)."""
    return f"fb-{_sanitize(series)}-{_sanitize(date)}-{_sanitize(str(chat_id))}"


def _validate_feedback_id(feedback_id: str) -> str:
    """feedback_id попадает в имя файла. Защита от `/`, `..`, NUL."""
    if not isinstance(feedback_id, str) or not feedback_id:
        raise ValueError("feedback_id must be a non-empty string")
    if not _FEEDBACK_ID_RE.match(feedback_id):
        raise ValueError(
            "feedback_id contains invalid characters (allowed: A-Z a-z 0-9 . _ -): %r" % feedback_id
        )
    if feedback_id in (".", "..") or "/" in feedback_id or "\\" in feedback_id:
        raise ValueError("feedback_id path traversal blocked: %r" % feedback_id)
    return feedback_id


def path_for(feedback_id: str, *, root: Optional[Path] = None) -> Path:
    """Полный путь к файлу состояния (без создания директории)."""
    root = root or resolve_feedback_dir()
    return root / f"{_validate_feedback_id(feedback_id)}{STATE_SUFFIX}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def locked(feedback_id: str, *, root: Optional[Path] = None) -> Iterator[None]:
    """Межпроцессный замок на ОДНО окно правок (`flock` на `.<fid>.lock`).

    🔴 Писателей больше не один. Раньше здесь стояло «единственный писатель —
    listener (один процесс)», и атомарность `rename` этого хватало. Ф10d завела
    ВТОРОГО: ручка правок с портала живёт в процессе ШЛЮЗА и делает тот же
    read→изменить→write. Без замка правка реплаем и правка с экрана в одну минуту
    затирают друг друга целиком (`write_state` пишет объект, а не поле), а
    конкурентная правка возвращает уже заклеймленный `reissuing` обратно в
    `collecting` — и раунд перевыпускается дважды.

    Замок — отдельный файл рядом (не сам state): state пересоздаётся `rename`'ом,
    и flock на нём держался бы за уже удалённый inode. Тот же приём, что у
    `orders._mutate` и у листа ожидания.
    """
    root = root or resolve_feedback_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / (".%s.lock" % _validate_feedback_id(feedback_id))
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def mutate(
    feedback_id: str,
    fn: Callable[[Optional[dict]], Optional[dict]],
    *,
    root: Optional[Path] = None,
) -> Optional[dict]:
    """Единственная дверь «прочитать → изменить → записать» окна правок.

    Под замком читает текущее состояние, отдаёт его `fn` и записывает то, что
    `fn` вернула. `fn` вернула None → не пишем ничего (правку не приняли).
    Возвращает записанное состояние (или None).

    Кто меняет окно правок — обязан идти сюда, а не звать `read_state` +
    `write_state` подряд: между ними влезает второй процесс.
    """
    with locked(feedback_id, root=root):
        current = read_state(feedback_id, root=root)
        new_state = fn(current)
        if new_state is None:
            return None
        write_state(new_state, root=root, under_lock=True)
        return new_state


class ConcurrentWrite(RuntimeError):
    """Состояние под руками изменил кто-то ещё — запись затёрла бы его правку."""


def _on_disk_rev(target: Path) -> Optional[int]:
    """Номер ревизии того, что сейчас лежит на диске (None — файла нет/битый).

    Считаем ревизии, а не метки времени: `updated_at` идёт с точностью до
    СЕКУНДЫ, и две записи в одну секунду выглядели бы одинаковыми — то есть
    защита не сработала бы ровно в том случае, ради которого сделана (две
    правки в одну минуту). Поле ленивое: у состояний до этой правки его нет,
    первая же запись его заводит.
    """
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return int(data.get("rev")) if data.get("rev") is not None else None
    except (TypeError, ValueError):
        return None


def write_state(state: dict, *, root: Optional[Path] = None, under_lock: bool = False) -> Path:
    """Атомарно записывает state-файл (`tempfile + os.rename`, fsync). Возвращает Path.

    🔴 ПРОВЕРКА НА ЗАТИРАНИЕ (оптимистичная блокировка). Пишем объект ЦЕЛИКОМ,
    поэтому запись состояния, прочитанного ДО чужой правки, стирает её без следа:
    человек получил «✅ принял», а правки в новой версии нет. Замок (`mutate`)
    закрывает это для нашего кода, но низкоуровневый `read_state`+`write_state`
    остаётся доступен — и однажды кто-нибудь его так и позовёт. Поэтому сверяем
    `updated_at`: у прочитанного «мимо» замка он разойдётся с диском, и мы
    поднимаем `ConcurrentWrite` вместо тихой потери. Лечится переводом вызова на
    `mutate()`.

    `under_lock=True` ставит ТОЛЬКО `mutate` — там конкуренцию держит flock, а
    состояние внутри транзакции может быть собрано с нуля (новый раунд поверх
    `reissuing`), то есть законно без метки.
    """
    feedback_id = _validate_feedback_id(state.get("feedback_id", ""))
    root = root or resolve_feedback_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{feedback_id}{STATE_SUFFIX}"
    disk_rev = _on_disk_rev(target) if target.exists() else None
    if not under_lock and target.exists():
        seen_rev = state.get("rev")
        # `seen_rev is None` при ревизованном файле — это «состояние собрали с
        # нуля, пока файл уже был»: ровно первый ход гонки (читатель пришёл,
        # когда файла ещё не было, а записать успел позже). Битый JSON на диске
        # (`disk_rev is None`) перезаписывать МОЖНО — это починка, а не затирание.
        if disk_rev is not None and disk_rev != seen_rev:
            raise ConcurrentWrite(
                "состояние %s изменилось под руками (ревизия на диске %s, у нас "
                "%s) — запись затёрла бы чужую правку; читай и меняй через "
                "feedback_state.mutate()" % (feedback_id, disk_rev, seen_rev)
            )
    state["rev"] = (disk_rev or 0) + 1
    state["updated_at"] = now_iso()

    fd, tmp = tempfile.mkstemp(prefix=f".{feedback_id}.", suffix=".json.tmp", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, target)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return target


def read_state(feedback_id: str, *, root: Optional[Path] = None) -> Optional[dict]:
    """Читает state или None если файла нет / битый JSON."""
    p = path_for(feedback_id, root=root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[feedback-state] read %s failed: %s", p, e)
        return None


def list_states(
    *, root: Optional[Path] = None, status_filter: Optional[Iterable[str]] = None
) -> list[dict]:
    """Сканирует папку, возвращает state'ы со статусом из `status_filter` (None → все)."""
    root = root or resolve_feedback_dir()
    if not root.exists():
        return []
    out: list[dict] = []
    allowed = set(status_filter) if status_filter else None
    for f in root.glob(f"*{STATE_SUFFIX}"):
        if f.name.startswith("."):  # скрытые tempfile'ы
            continue
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[feedback-state] skip malformed %s: %s", f.name, e)
            continue
        if not isinstance(state, dict):
            continue
        if allowed is not None and state.get("status") not in allowed:
            continue
        out.append(state)
    return out


def mark_status(
    feedback_id: str,
    new_status: str,
    *,
    root: Optional[Path] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Optional[dict]:
    """Обновляет статус (атомарно). Возвращает новый state или None если файла нет."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {new_status!r}")

    def _apply(state: Optional[dict]) -> Optional[dict]:
        if state is None:
            return None
        state["status"] = new_status
        if extra:
            state.update(extra)
        return state

    return mutate(feedback_id, _apply, root=root)


def claim_for_reissue(feedback_id: str, *, root: Optional[Path] = None) -> Optional[dict]:
    """Н1 (FM-10): атомарно «забирает» state на перевыпуск ДО чтения `edits`.

    `ready_for_reissue` → `reissuing` одной атомарной записью (mkstemp+rename).
    Возвращает claimed-state (со статусом `reissuing`) если claim удался, иначе
    None (статус уже не `ready_for_reissue` — кто-то перевёл его раньше, либо
    конкурентный reply открыл новый раунд).

    Зачем claim ПЕРЕД чтением edits: пока стоит `reissuing`, `apply_edit`
    трактует входящий reply как НОВЫЙ раунд (а не дозапись в съедаемые edits) —
    правки текущего раунда уходят в перевыпуск, правки конкурентного reply'я — в
    следующий раунд. Без claim reply в окне между `ready_for_reissue` и
    перевыпуском затирал бы несъеденные edits (`_new_round_state`).

    Модель конкуренции: писателей ДВА процесса — listener (реплаи, sweep) и шлюз
    (правки с портала). Проверка статуса и запись claim'а идут под межпроцессным
    замком (`locked`), иначе правка с портала, приехавшая между чтением и
    записью, вернула бы статус в `collecting` со старым списком — и раунд ушёл бы
    в перевыпуск дважды.
    """

    def _claim(state: Optional[dict]) -> Optional[dict]:
        if state is None or state.get("status") != "ready_for_reissue":
            return None
        state["status"] = "reissuing"
        state["reissue_claimed_at"] = now_iso()
        return state

    return mutate(feedback_id, _claim, root=root)


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """ISO-метка (в т.ч. '...Z') → tz-aware datetime (UTC); None если пусто/битое."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cleanup_dormant_states(
    *,
    root: Optional[Path] = None,
    max_age_days: int = 30,
    now: Optional[datetime] = None,
) -> int:
    """R10 (Ф8) + I9 (И1): удаляет служебные state-файлы `*-feedback.json` старше
    `max_age_days` (по `updated_at`). Возвращает число удалённых. Удаляемые статусы:

      • `dormant` — терминальный успех «давно отдыхает» (R10);
      • `ready_for_reissue` с `reissue_attempts >= MAX_REISSUE_ATTEMPTS` (I9) —
        перманентно-провальный перевыпуск (claude падал MAX раз подряд, попытки
        исчерпаны, владельцу/Ф9 уже отдано). `ready_for_reissue` с attempts<MAX —
        ещё ретраится, НЕ трогаем (даже старый — он живой кандидат на следующий
        sweep). Новый reply откроет свежий раунд независимо от удаления.

    Скоуп жёстко ограничен: путь — ТОЛЬКО `resolve_feedback_dir()` (или переданный
    `root`), удаляем ТОЛЬКО файлы с суффиксом `STATE_SUFFIX`. Транскрипты и протоколы
    в `~/Projects/me/встречи/` по построению недостижимы. Прочие статусы и свежие
    (моложе порога, либо без/битым `updated_at`) — не трогаем. Скрытые tempfile'ы
    (имя на `.`) пропускаем.

    R9: логируем только число удалённых — без имён участников/текста.
    """
    root = root or resolve_feedback_dir()
    if not root.exists():
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff_sec = max(0, int(max_age_days)) * 86400
    removed = 0
    for f in root.glob(f"*{STATE_SUFFIX}"):
        if f.name.startswith("."):  # скрытые tempfile'ы — не трогаем
            continue
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[feedback-state] cleanup skip malformed %s: %s", f.name, e)
            continue
        if not isinstance(state, dict):
            continue
        status = state.get("status")
        # Решаем, кандидат ли файл на удаление по статусу/attempts (свежесть — ниже,
        # единым cutoff для всех кандидатов).
        if status in ("dormant", "failed"):
            pass  # терминальные: dormant (R10, успех) / failed (исчерпан reissue) —
                  # удаляем по возрасту. failed уже отдан владельцу one-shot'ом.
        elif status == "ready_for_reissue":
            # I9: только исчерпавшие лимит попыток (attempts<MAX ещё ретраится — живой).
            # NB (Ф1 delivery-fixes): исчерпавшие теперь терминализуются в `failed`
            # на claim-этапе, так что эта ветка ловит лишь legacy-состояния до фикса.
            attempts = int(state.get("reissue_attempts") or 0)
            if attempts < MAX_REISSUE_ATTEMPTS:
                continue
        else:
            continue  # collecting / reissuing — не наша забота
        updated = _parse_iso(state.get("updated_at"))
        if updated is None:  # без валидной метки — консервативно не удаляем
            continue
        if (now - updated).total_seconds() < cutoff_sec:
            continue  # свежий — оставляем
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            logger.warning("[feedback-state] cleanup unlink failed %s: %s", f.name, e)
    if removed:
        logger.info("[feedback-state] cleanup_dormant_states: удалено %d (dormant + "
                    "стухший ready_for_reissue, старше %dд)", removed, max_age_days)
    return removed


def is_window_expired(state: dict, *, now: Optional[datetime] = None) -> bool:
    """True если окно сбора закрылось (`now >= deadline_at`). `now` инъектируется в тестах."""
    deadline = _parse_iso(state.get("deadline_at"))
    if deadline is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= deadline
