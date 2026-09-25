"""Самопроверка бота в Т-10 и «нюх» версии Телемоста (ISS-60, фаза 2, 25.09.2026).

ЗАЧЕМ. Телемост меняет вход без предупреждения примерно раз в 1–2 месяца; 21–23.09
бот три дня не заходил ни на одну встречу, и тревоги не было. Сторож входа
(фаза 1, `join_watch`) ловит срыв на НАСТОЯЩЕЙ встрече — когда она уже теряется.
Самопроверка ловит поломку заранее: каждое утро (04:40 МСК) и сразу после
обновления Телемоста в запасную переговорку Т-10 (решение владельца 23.09: ни одна
серия там не сидит) заходят «Тест-диктор» и бот, диктор говорит фразу с кодовым
словом, бот её записывает, мы ищем слово в расшифровке.

КАК (один прогон, `run_once`).
  1. Защиты (`guard_reason`): идёт настоящий бот → не мешаем; встреча в очереди
     ближе 40 минут → не мешаем (`--force` снимает только эту проверку); уже идёт
     другая самопроверка (замок) → выходим.
  2. Диктор (`canary-kit/speaker.js`) входит ТЕМ ЖЕ кодом входа из ТОГО ЖЕ образа,
     что бот. Не вошёл → красный с причиной: так же не вошёл бы бот.
  3. Диктор проверяет, что в комнате он один; кто-то есть → `room_busy`, уходим.
  4. Бот — тот же образ и те же настройки, что у запускателя, но своя папка
     записей (иначе сборщик прислал бы владельцу протокол тестовой встречи), своя
     метка `meeting-notary.role=canary` (запускатель, сборщик и сторож его не видят)
     и свой диапазон номеров канала команд.
  5. Диктор говорит → бот уходит по команде `leave` → оценка (`evaluate`):
     зашёл / есть запись ≥ 10 с / не тишина / кодовое слово в расшифровке —
     критично (провал = 🔴); «Тест-диктор» в участниках — не критично (🟡: в 3.0
     список участников пока не собирается, фаза 3).
  6. 🔴 перепроверяется вторым полным прогоном через 3 минуты (`run_cycle`);
     сообщение владельцу — только если красный оба раза.

ЧТО ЕЩЁ. `sniff` раз в час смотрит версию Телемоста на главной странице;
сменилась → внеплановая самопроверка. `warn_tick` (из тика запускателя): пока
самопроверка красная — за ~10 минут до каждой встречи предупредить владельца,
чтобы он включил запись в Телемосте сам.

🔴 ЗАПИСИ ПРОГОНОВ НЕ УДАЛЯЮТСЯ — это синтетический голос, мало места, и они
нужны для разбора. ЕДИНСТВЕННОЕ ИСКЛЮЧЕНИЕ — `room_busy`: в комнате оказался
кто-то посторонний, в записи могут быть чужие голоса и имена. Тогда папка прогона
(записи, логи, снимки) удаляется целиком сразу, остаётся только файл результата
без аудио. Это осознанное исключение из правила «записи встреч не удалять
автоматически» (память `transcripts-never-auto-delete`): то правило защищает
встречи людей, а здесь удаление защищает посторонних от записи без их ведома.

🔴 ОПАСНАЯ ТРОЙКА. Текст расшифровки не пишется ни в логи, ни в файлы состояния:
только «кодовое слово найдено да/нет» и длина текста.

🔴 ССЫЛКА КОМНАТЫ берётся из `rooms.yaml` на сервере по имени комнаты; в логи,
сообщения и файлы состояния попадает только имя (`t10`). Логи контейнеров,
сохраняемые в папку прогона, проходят вычистку ссылок (`redact`).

🔴 СООБЩЕНИЯ — только владельцу (`notify.push` → tg-send). Группы и чаты серий
самопроверка не трогает.

Чистые функции (`evaluate`, `guard_reason`, `apply_final`, `sniff_update`,
`due_warnings`, сборка команд docker) — без docker и сети: их и тестируем.
Внешний мир приходит через `Deps` (настоящий — из `notary/canary.py`).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("notary.canary")

try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    _MSK = ZoneInfo("Europe/Moscow")
except Exception:  # noqa: BLE001 — нет базы поясов → фиксированный +3
    _MSK = timezone(timedelta(hours=3))

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

DEFAULT_IMAGE = "vexa-bot:notarius-telemost"
DEFAULT_ROOM = "t10"
SPEAKER_NAME = "Тест-диктор"
#: Боевое имя бота — самопроверка проверяет ровно то, что видят люди.
BOT_NAME = "Бот — протокол встречи"
SERIES = "canary"

#: Метки контейнеров. `role=canary`, а НЕ `notarius`: запускатель считает живых
#: ботов по имени `vexa-notarius-*`, сборщик ищет «бот ещё в звонке» по метке
#: серии, сторож — по своему журналу. Контейнер самопроверки не должен занимать
#: слот, гейтить сборку или попадать под сторож.
ROLE_LABEL = "meeting-notary.role"
ROLE_CANARY = "canary"
ROLE_NOTARIUS = "notarius"
RUN_LABEL = "meeting-notary.canary-run"
CONTAINER_PREFIX = "vexa-canary-"
NOTARIUS_NAME_PREFIX = "vexa-notarius-"

# Держать в синхроне с runner.py (тест `test_constants_match_runner` сверяет).
DOCKER_NETWORK = "vexa_vexa"
TRANSCRIPTION_URL = "http://172.17.0.1:8083/v1/audio/transcriptions"
DEFAULT_REDIS_URL = "redis://vexa-redis-1:6379"

#: Номера канала команд (`bot_commands:meeting:<id>`). Запускатель даёт
#: `int(unix_time) + seq` (seq < ~100 + счётчик повторов) — это ~1,7–4,3 млрд до
#: 2100 года. Самопроверка берёт 9_000_000_000 + (unix_time mod 10^9): диапазоны
#: не пересекаются, пока unix_time < 9·10^9 (год 2255), а два прогона в разные
#: секунды получают разные номера. JS хранит такие числа точно (до 2^53).
CANARY_MEETING_ID_BASE = 9_000_000_000
CANARY_MEETING_ID_SPAN = 1_000_000_000

# Сроки одного прогона.
POLL_SEC = 3
SPEAKER_ALONE_TIMEOUT_SEC = 240       # диктор: вход в звонок (бот входит за 30–135 с)
BOT_IN_CALL_TIMEOUT_SEC = 300         # бот: вход в звонок
SPOKE_TIMEOUT_SEC = 90                # диктор: 3 повтора фразы по ~6,5 с + запас
AFTER_SPEAK_PAUSE_SEC = 10            # хвост записи после фразы
BOT_EXIT_TIMEOUT_SEC = 180            # бот: выход по `leave` и дописывание записи
SPEAKER_EXIT_TIMEOUT_SEC = 30
SPEAK_PLAYS = 3
#: Срок жизни диктора: вход + ожидание бота + голос + пауза + запас. Больше
#: дефолта speaker.js (360 с), иначе при медленном входе бота диктор ушёл бы сам.
SPEAKER_MAX_SECONDS = (
    SPEAKER_ALONE_TIMEOUT_SEC + BOT_IN_CALL_TIMEOUT_SEC + SPOKE_TIMEOUT_SEC
    + AFTER_SPEAK_PAUSE_SEC + 80
)
TRANSCRIBE_TIMEOUT_SEC = 240
TRANSCRIBE_MAX_FILES = 2

# Повтор перед тревогой.
RETRY_PAUSE_SEC = 180
#: Юнит самопроверки живёт ≤ 40 мин (TimeoutStartSec); повтор не начинаем, если
#: худший второй прогон не успеет — красный тогда перепроверит часовой «нюх».
CYCLE_BUDGET_SEC = 38 * 60
MAX_RUN_SEC = (
    SPEAKER_ALONE_TIMEOUT_SEC + BOT_IN_CALL_TIMEOUT_SEC + SPOKE_TIMEOUT_SEC
    + AFTER_SPEAK_PAUSE_SEC + BOT_EXIT_TIMEOUT_SEC + TRANSCRIBE_MAX_FILES * TRANSCRIBE_TIMEOUT_SEC
)

# Защиты.
MEETING_GUARD_AHEAD_MIN = 40
#: Встреча, начавшаяся недавно, могла ещё не получить бота (ожидание слота,
#: «зайти сейчас») — её тоже не толкаем.
MEETING_GUARD_PAST_MIN = 10

# Оценка записи.
MIN_RECORDING_SEC = 10.0
#: «Не тишина» (ffmpeg volumedetect, dBFS). Цифровая тишина бота = −91 дБ.
#: Голос диктора после Opus и записи бота даёт пики около −20…−6 дБ; −35 дБ
#: оставляет двойной запас на тихий тракт, но отсекает щелчки и шум.
#: Средний уровень по всему файлу разбавлен паузами (фраза ~20 с из ~60–120 с
#: записи → ≈ −30…−40 дБ); −65 дБ — только «совсем пусто» с одиночным щелчком.
SOUND_MAX_DB = -35.0
SOUND_MEAN_DB = -65.0

# Сообщения.
ALERT_DEDUPE_HOURS = 24
SKIP_ALERT_AFTER = 3
NIGHT_FROM_H, NIGHT_TO_H = 22, 8   # МСК: ночью — беззвучно

# «Нюх».
TELEMOST_HOME = "https://telemost.yandex.ru/"
VERSION_HISTORY = 20
SNIFF_ERROR_ALERT_AFTER = 6
#: Комната была занята при внеплановой проверке → следующая попытка не раньше,
#: чем через 2 часа: днём Т-10 бывает занята, а заходить к людям каждый час — нет.
BUSY_RETRY_BACKOFF_MIN = 120

# Предупреждение перед встречами.
WARN_FROM_MIN, WARN_TO_MIN = 8, 12
WARN_TTL_HOURS = 24

# Статусы.
GREEN, YELLOW, RED, SKIPPED, ERROR = "green", "yellow", "red", "skipped", "error"
RED_UNCONFIRMED = "red_unconfirmed"

#: Причины диктора, при которых он даже не пытался войти — сбой самой проверки.
SPEAKER_SETUP_REASONS = frozenset({"bad_config", "phrase_missing", "pulse_unavailable", "image_modules_missing"})
#: Причины «ошибка самой проверки» (не бот): не краснеем, тихое сообщение.
ERROR_REASONS = frozenset({
    "speaker_setup_failed", "room_not_found", "room_url_invalid", "kit_missing", "internal_error",
})
#: Некритичные причины → 🟡.
YELLOW_REASONS = frozenset({"transcribe_failed", "names_missing"})

REASON_TEXT: Dict[str, str] = {
    "speaker_start_failed": "не запустился контейнер диктора из образа бота",
    "speaker_join_failed": "диктор не вошёл кодом бота",
    "speaker_timeout": "диктор не дошёл до звонка за 4 минуты",
    "speaker_crashed": "контейнер диктора упал до входа в звонок",
    "speaker_lost": "диктор выпал из звонка до проверки звука",
    "speaker_mic_failed": "диктор не смог включить микрофон — звук не проверен (возможно, Телемост поменял кнопки)",
    "speak_timeout": "диктор не отчитался, что сказал фразу",
    "bot_start_failed": "не запустился контейнер бота",
    "bot_join_failed": "бот не вошёл в звонок",
    "bot_join_timeout": "бот не вошёл в звонок за 5 минут",
    "bot_crashed": "контейнер бота упал до входа в звонок",
    "bot_not_joined": "бот не удержался в звонке",
    "no_recording": "бот не оставил записи",
    "recording_too_short": "запись слишком короткая",
    "silence": "в записи тишина — бот не слышит звонок",
    "code_word_missing": "в расшифровке нет кодового слова — бот пишет не тот звук",
    "transcribe_failed": "расшифровка не удалась — кодовое слово не проверено",
    "names_missing": "бот не увидел «Тест-диктора» в списке участников (имена в протоколах могут пропасть)",
    "room_busy": "диктор ушёл, не проверив бота",
    "bot_running": "идёт настоящая встреча — проверка отложена",
    "meeting_soon": "скоро встреча — проверка отложена",
    "docker_unavailable": "docker не ответил",
    "lock_busy": "уже идёт другая самопроверка",
    "speaker_setup_failed": "диктор не смог подготовиться (звук/настройки контейнера)",
    "room_not_found": "комната самопроверки не найдена в rooms.yaml",
    "room_url_invalid": "у комнаты самопроверки не ссылка Телемоста",
    "kit_missing": "нет фраз диктора (canary-kit)",
    "internal_error": "самопроверка упала сама",
    "retry_deferred": "перепроверка отложена",
}
#: Подробности, которые понятнее словами: состояние комнаты.
ROOM_BUSY_DETAIL = {
    "strangers": "в комнате кто-то есть",
    "foreign_name": "в комнате незнакомое имя",
    "count_unknown": "диктор не смог посчитать участников — нужна поправка счётчика в speaker.js",
}


# ---------------------------------------------------------------------------
# Пути и файлы
# ---------------------------------------------------------------------------


def is_local_docker() -> bool:
    """Самопроверка живёт там же, где боты (сервер, docker локально)."""
    return os.environ.get("MEETING_NOTARY_LOCAL_DOCKER") == "1"


def state_dir() -> Path:
    """`state/canary/`: env → `/srv/meeting-notary/state/canary` (сервер) → домашний дефолт."""
    env = (os.environ.get("MEETING_NOTARY_CANARY_STATE_DIR") or "").strip()
    if env:
        return Path(os.path.expanduser(env))
    vps = Path("/srv/meeting-notary/state")
    if vps.is_dir():
        return vps / "canary"
    return Path(os.path.expanduser("~/.local/state/meeting-notary/canary"))


def runs_dir() -> Path:
    return state_dir() / "runs"


def health_path() -> Path:
    return state_dir() / "health.json"


def version_path() -> Path:
    return state_dir() / "telemost-version.json"


def warned_path() -> Path:
    return state_dir() / "warned.json"


def journal_path() -> Path:
    return state_dir() / "journal.jsonl"


def lock_path() -> Path:
    return state_dir() / ".run.lock"


def canary_root() -> Path:
    """Папка записей самопроверки — ВНЕ папки записей бота (сборщик её не видит)."""
    env = (os.environ.get("MEETING_NOTARY_CANARY_DIR") or "").strip()
    return Path(os.path.expanduser(env)) if env else Path("/home/dev/meeting-notary/_tmp/canary")


def kit_dir() -> Path:
    env = (os.environ.get("MEETING_NOTARY_CANARY_KIT_DIR") or "").strip()
    return Path(os.path.expanduser(env)) if env else Path(__file__).resolve().parent.parent / "canary-kit"


def redis_url() -> str:
    return os.environ.get("MEETING_NOTARY_REDIS_URL") or DEFAULT_REDIS_URL


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("[canary] %s не прочитан (%s)", path.name, type(e).__name__)
        return None


def write_json(path: Path, data: Any) -> None:
    """Атомарно (tmp + rename в той же папке)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def load_health() -> Dict[str, Any]:
    return _dict(read_json(health_path()))


def save_health(h: Dict[str, Any]) -> None:
    h["updated_at"] = iso(datetime.now(timezone.utc))
    write_json(health_path(), h)


def load_version_state() -> Dict[str, Any]:
    return _dict(read_json(version_path()))


def save_run(result: Dict[str, Any]) -> None:
    write_json(runs_dir() / f"{result['run_id']}.json", result)


def append_journal(entry: Dict[str, Any]) -> None:
    try:
        p = journal_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("[canary] журнал не дописан (%s)", type(e).__name__)


def try_lock() -> Optional[int]:
    """Замок самопроверки (fcntl, без ожидания). None — уже занят."""
    p = lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def unlock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Время, номера, тексты
# ---------------------------------------------------------------------------


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_night(now: datetime) -> bool:
    h = now.astimezone(_MSK).hour
    return h >= NIGHT_FROM_H or h < NIGHT_TO_H


def msk_hm(dt: datetime) -> str:
    return dt.astimezone(_MSK).strftime("%H:%M") + " МСК"


def msk_day_hm(dt: datetime) -> str:
    return dt.astimezone(_MSK).strftime("%d.%m %H:%M") + " МСК"


def make_run_id(now: datetime) -> str:
    return "canary-" + now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def canary_meeting_id(now: datetime) -> int:
    return CANARY_MEETING_ID_BASE + int(now.timestamp()) % CANARY_MEETING_ID_SPAN


def room_label(room: str) -> str:
    """`t10` → «Т-10» (как переговорку называют люди)."""
    m = re.fullmatch(r"[tт]-?(\d+)", (room or "").strip().lower())
    return f"Т-{m.group(1)}" if m else (room or "?")


def speaker_container(run_id: str) -> str:
    return f"{CONTAINER_PREFIX}speaker-{run_id}"


def bot_container(run_id: str) -> str:
    return f"{CONTAINER_PREFIX}bot-{run_id}"


def speaker_session_uid(run_id: str) -> str:
    return f"{run_id}-speaker"


def bot_session_uid(run_id: str) -> str:
    return f"{run_id}-bot"


_URL_RE = re.compile(r"https?://[^\s\"'<>]*telemost[^\s\"'<>]*", re.I)


def redact(text: str, url: Optional[str] = None) -> str:
    """Вычистить ссылки на комнаты Телемоста (логи контейнеров, тексты ошибок)."""
    out = text or ""
    if url:
        out = out.replace(url, "<комната>")
        bare = url.split("?", 1)[0]
        if bare:
            out = out.replace(bare, "<комната>")
    return _URL_RE.sub("<комната>", out)


def _human_detail(code: str, detail: Any) -> str:
    if code == "room_busy":
        return ROOM_BUSY_DETAIL.get(str(detail), str(detail))
    if code in ("speaker_join_failed", "bot_join_failed", "bot_not_joined") and detail:
        try:
            from notary.lib.join_alert import reason_human  # noqa: PLC0415

            return reason_human(str(detail))
        except Exception:  # noqa: BLE001
            return str(detail)
    if code == "recording_too_short" and detail is not None:
        return f"{detail} с"
    return str(detail) if detail not in (None, "") else ""


_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")


def human_reason(code: str, detail: Any = None) -> str:
    """«<словами>: <подробность> (код[: код подробности])»."""
    text = REASON_TEXT.get(code) or code
    extra = _human_detail(code, detail)
    tail = f"{code}: {detail}" if isinstance(detail, str) and _CODE_RE.match(detail) else code
    return f"{text}: {extra} ({tail})" if extra else f"{text} ({tail})"


def human_reasons(result: Dict[str, Any], limit: int = 3) -> str:
    details = _dict(result.get("details"))
    codes = list(result.get("reasons") or [])
    if not codes and result.get("skip_reason"):
        codes = [result["skip_reason"]]
    parts = [human_reason(c, details.get(c)) for c in codes[:limit]]
    return "; ".join(parts) or "причина не записана"


# ---------------------------------------------------------------------------
# Фразы и кодовое слово
# ---------------------------------------------------------------------------

_PHRASE_FILE_RE = re.compile(r"^[a-z0-9-]+\.wav$")


def load_phrases(kit: Path) -> List[Dict[str, str]]:
    data = _dict(read_json(kit / "phrases" / "phrases.json"))
    out: List[Dict[str, str]] = []
    for p in data.get("phrases") or []:
        if not isinstance(p, dict):
            continue
        f, w = str(p.get("file") or ""), str(p.get("word") or "").strip()
        if _PHRASE_FILE_RE.match(f) and w and (kit / "phrases" / f).is_file():
            out.append({"file": f, "word": w})
    return out


def normalize_text(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


_TOKEN_RE = re.compile(r"[a-zа-я0-9]+")


def word_stem(word: str) -> str:
    """Основа слова: без двух последних букв (падежи), не короче 4."""
    w = normalize_text(word).strip()
    return w[: max(4, len(w) - 2)] if len(w) > 4 else w


def code_word_found(text: str, word: str) -> bool:
    stem = word_stem(word)
    if not stem:
        return False
    return any(t.startswith(stem) for t in _TOKEN_RE.findall(normalize_text(text)))


def _name_key(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", normalize_text(s))


def names_contain(participants: Sequence[str], name: str = SPEAKER_NAME) -> bool:
    key = _name_key(name)
    return any(key and key in _name_key(p) for p in participants if isinstance(p, str))


# ---------------------------------------------------------------------------
# Команды docker (чистые — их проверяют тесты)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    run_dir: Path
    image: str
    room: str
    meeting_url: str
    phrase_file: str
    code_word: str
    meeting_id: int
    kit: Path


def _labels(run_id: str) -> List[str]:
    return ["--label", f"{ROLE_LABEL}={ROLE_CANARY}", "--label", f"{RUN_LABEL}={run_id}"]


def speaker_docker_cmd(plan: RunPlan) -> List[str]:
    """Диктор: тот же образ, своя точка входа, папка прогона как /transcripts.

    Токена Телеграма в контейнере нет (как и у бота: браузер на чужих страницах).
    """
    speaker_cfg = {
        "meetingUrl": plan.meeting_url,
        "name": SPEAKER_NAME,
        "botName": BOT_NAME,
        "phraseFile": plan.phrase_file,
        "maxSeconds": SPEAKER_MAX_SECONDS,
        "plays": SPEAK_PLAYS,
    }
    # Сырой BOT_CONFIG — только ради sessionUid: join-status.js и crashkit.js из
    # образа пишут статус и снимок диктора в его собственные файлы.
    status_cfg = {"platform": "yandex_telemost", "botName": SPEAKER_NAME,
                  "sessionUid": speaker_session_uid(plan.run_id)}
    return [
        "docker", "run", "-d",
        "--name", speaker_container(plan.run_id),
        "--network", DOCKER_NETWORK,
        "-v", f"{plan.run_dir}:/transcripts",
        "-v", f"{plan.kit}:/canary:ro",
        *_labels(plan.run_id),
        "-e", "BOT_CONFIG=" + json.dumps(status_cfg, ensure_ascii=False),
        "-e", "SPEAKER_CONFIG=" + json.dumps(speaker_cfg, ensure_ascii=False),
        "--entrypoint", "sh",
        plan.image, "/canary/speaker.sh",
    ]


def bot_config(plan: RunPlan) -> Dict[str, Any]:
    """BOT_CONFIG как у запускателя (`runner._launch_bot`), кроме серии и номера канала."""
    return {
        "platform": "yandex_telemost",
        "meetingUrl": plan.meeting_url,
        "botName": BOT_NAME,
        "sessionUid": bot_session_uid(plan.run_id),
        "language": "ru",
        "task": "transcribe",
        "redisUrl": redis_url(),
        "meeting_id": plan.meeting_id,
        "series": SERIES,
        "expectedParticipants": [],
    }


def bot_docker_cmd(plan: RunPlan) -> List[str]:
    return [
        "docker", "run", "-d",
        "--name", bot_container(plan.run_id),
        "--network", DOCKER_NETWORK,
        "-v", f"{plan.run_dir}:/transcripts",
        *_labels(plan.run_id),
        "-e", "BOT_CONFIG=" + json.dumps(bot_config(plan), ensure_ascii=False),
        "-e", f"TRANSCRIPTION_SERVICE_URL={TRANSCRIPTION_URL}",
        "-e", "ENABLE_LIVE_DRAFT=0",
        plan.image,
    ]


def leave_channel(meeting_id: int) -> str:
    return f"bot_commands:meeting:{meeting_id}"


LEAVE_MESSAGE = json.dumps({"action": "leave"})


# ---------------------------------------------------------------------------
# Оценка (чистая)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    #: Последняя фаза статуса входа бота и все фазы, что он проходил.
    bot_phase: Optional[str]
    bot_phases_seen: Tuple[str, ...]
    #: Длительность каждого wav (секунды; None — не удалось измерить).
    wav_durations: Tuple[Optional[float], ...]
    #: Лучшая громкость по файлам: (mean_db, max_db) или None.
    volume: Optional[Tuple[float, float]]
    #: Кодовое слово нашлось; None — расшифровка не удалась.
    code_word_found: Optional[bool]
    #: Участники из meta.json бота; None — meta.json нет.
    participants: Optional[Tuple[str, ...]]


def evaluate(ev: Evidence, *, speaker_name: str = SPEAKER_NAME) -> Dict[str, Any]:
    """Итог записи: {status, reasons, details, checks, metrics}. Чистая функция."""
    reasons: List[str] = []
    details: Dict[str, Any] = {}

    joined = ev.bot_phase != "failed" and (ev.bot_phase == "in_call" or "in_call" in ev.bot_phases_seen)
    durations = [d for d in ev.wav_durations if isinstance(d, (int, float))]
    total = round(sum(durations), 1)
    has_wav = bool(ev.wav_durations)
    recorded = total >= MIN_RECORDING_SEC
    sound = (
        ev.volume is not None
        and ev.volume[1] >= SOUND_MAX_DB
        and ev.volume[0] >= SOUND_MEAN_DB
    )
    names: Optional[bool] = None if ev.participants is None else names_contain(ev.participants, speaker_name)

    if not joined:
        reasons.append("bot_not_joined")
        details["bot_not_joined"] = ev.bot_phase or "нет статуса"
    if not has_wav:
        reasons.append("no_recording")
    elif not recorded:
        reasons.append("recording_too_short")
        details["recording_too_short"] = total
    if has_wav and not sound:
        reasons.append("silence")
    if ev.code_word_found is False and (not has_wav or sound):
        # При тишине корень — тишина; слово искать бессмысленно.
        reasons.append("code_word_missing")
    critical = bool(reasons)
    if ev.code_word_found is None and has_wav and sound:
        reasons.append("transcribe_failed")
    if not names:
        reasons.append("names_missing")

    status = RED if critical else (YELLOW if reasons else GREEN)
    return {
        "status": status,
        "reasons": reasons,
        "details": details,
        "checks": {
            "joined": joined,
            "recorded": recorded,
            "sound": sound if has_wav else False,
            "code_word": ev.code_word_found,
            "names": names,
        },
        "metrics": {
            "recording_sec": total,
            "wav_count": len(ev.wav_durations),
            "mean_db": ev.volume[0] if ev.volume else None,
            "max_db": ev.volume[1] if ev.volume else None,
            "participants_count": None if ev.participants is None else len(ev.participants),
        },
    }


_VOL_RE = {
    "mean": re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB"),
    "max": re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB"),
}


def parse_volumedetect(stderr: str) -> Optional[Tuple[float, float]]:
    """(mean_db, max_db) из вывода `ffmpeg -af volumedetect`."""
    m1, m2 = _VOL_RE["mean"].search(stderr or ""), _VOL_RE["max"].search(stderr or "")
    if not (m1 and m2):
        return None
    return float(m1.group(1)), float(m2.group(1))


# ---------------------------------------------------------------------------
# Защиты (чистые)
# ---------------------------------------------------------------------------


def meeting_soon(queue: Sequence[Any], now: datetime) -> Optional[Dict[str, Any]]:
    lo = now - timedelta(minutes=MEETING_GUARD_PAST_MIN)
    hi = now + timedelta(minutes=MEETING_GUARD_AHEAD_MIN)
    for item in queue or []:
        if not isinstance(item, dict):
            continue
        start = parse_ts(item.get("start_at"))
        if start and lo <= start <= hi:
            return item
    return None


def guard_reason(
    containers: Optional[Sequence[Tuple[str, str]]],
    queue: Sequence[Any],
    now: datetime,
    *,
    force: bool,
) -> Optional[str]:
    """Причина не начинать прогон или None. `force` снимает только проверку очереди."""
    if containers is None:
        return "docker_unavailable"
    for name, role in containers:
        if (name or "").startswith(NOTARIUS_NAME_PREFIX) or role == ROLE_NOTARIUS:
            return "bot_running"
    if not force and meeting_soon(queue, now) is not None:
        return "meeting_soon"
    return None


# ---------------------------------------------------------------------------
# Внешний мир
# ---------------------------------------------------------------------------


def _noop_notify(_text: str, _silent: bool) -> bool:
    return False


@dataclass
class Deps:
    """Всё, что трогает docker, сеть и Телеграм. Настоящее — в `notary/canary.py`."""

    docker_run: Callable[[List[str]], Tuple[bool, str]]
    #: (status, exit_code): running / exited / created / missing / unknown …
    container_state: Callable[[str], Tuple[str, Optional[int]]]
    #: Живые контейнеры (имя, роль-метка); None — docker не ответил.
    containers: Callable[[], Optional[List[Tuple[str, str]]]]
    #: Все (в т.ч. остановленные) контейнеры с меткой role=canary — имена.
    canary_leftovers: Callable[[], List[str]]
    stop: Callable[[str], None]
    remove: Callable[[str], None]
    logs: Callable[[str], str]
    #: redis PUBLISH → получил ли кто-то (None — не знаем).
    publish: Callable[[str, str], Optional[bool]]
    probe_duration: Callable[[Path], Optional[float]]
    volume: Callable[[Path], Optional[Tuple[float, float]]]
    #: Текст расшифровки (бросает при сбое). Текст дальше функции не уходит.
    transcribe: Callable[[Path], str]
    read_queue: Callable[[], List[Dict[str, Any]]]
    room_url: Callable[[str], Optional[str]]
    notify: Callable[[str, bool], bool] = _noop_notify
    http_get: Optional[Callable[[str], Tuple[int, str]]] = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)


_GONE = {"exited", "missing", "dead"}


def _poll(deps: Deps, timeout_sec: float, fn: Callable[[], Any]) -> Any:
    """Звать `fn` раз в POLL_SEC, пока не вернёт не-None или не выйдет срок."""
    start = deps.now()
    while True:
        v = fn()
        if v is not None:
            return v
        if (deps.now() - start).total_seconds() >= timeout_sec:
            return None
        deps.sleep(POLL_SEC)


def _safe(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — уборка не должна ронять прогон
        logger.warning("[canary] внешний вызов упал (%s)", type(e).__name__)
        return None


def _host_path(run_dir: Path, container_path: Any) -> Optional[str]:
    """`/transcripts/.crash-kits/x` (как видит контейнер) → путь на сервере."""
    if not isinstance(container_path, str) or not container_path.startswith("/transcripts/"):
        return None
    rel = container_path[len("/transcripts/"):]
    if ".." in Path(rel).parts:
        return None
    return str(run_dir / rel)


def resolve_room_url(room: str, loader: Optional[Callable[[], Dict[str, Any]]] = None) -> Optional[str]:
    """Ссылка комнаты из rooms.yaml (`rooms: [{name, url, description}]`) по имени."""
    from notary.cli import registry  # noqa: PLC0415

    data = (loader or registry.load_rooms)()
    rec = registry.find_room(room, data if isinstance(data, dict) else {})
    url = (rec or {}).get("url")
    return str(url) if url else None


def valid_room_url(url: str) -> bool:
    m = re.match(r"^https://([^/?#]+)/", url or "")
    host = (m.group(1).lower() if m else "")
    return host in ("telemost.yandex.ru", "telemost.360.yandex.ru") or host.endswith(".telemost.yandex.ru")


# ---------------------------------------------------------------------------
# Один прогон
# ---------------------------------------------------------------------------


def _read_speaker(run_dir: Path) -> Dict[str, Any]:
    return _dict(read_json(run_dir / ".canary" / "speaker.json"))


def _read_bot_status(run_dir: Path, run_id: str) -> Dict[str, Any]:
    return _dict(read_json(run_dir / ".join-status" / f"{bot_session_uid(run_id)}.json"))


def _write_cmd(run_dir: Path, action: str, seq: int) -> None:
    p = run_dir / ".canary" / "cmd.json"
    write_json(p, {"action": action, "seq": seq})
    try:
        os.chmod(p, 0o644)
    except OSError:
        pass


def _make_run_dir(run_dir: Path) -> None:
    """Папка прогона: контейнер пишет от root — права 0777, как у бота."""
    run_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(run_dir, 0o777)
    for sub in (".canary", ".join-status", ".crash-kits"):
        (run_dir / sub).mkdir()
        os.chmod(run_dir / sub, 0o777)


def _remove_run_dir(run_dir: Path) -> bool:
    """Удалить папку прогона целиком (room_busy). Ссылкам не следуем."""
    try:
        if run_dir.is_symlink() or not run_dir.is_dir():
            return False
        shutil.rmtree(run_dir)
        return True
    except OSError as e:
        logger.warning("[canary] папка прогона не удалена (%s)", type(e).__name__)
        return False


def _save_logs(deps: Deps, run_dir: Path, name: str, role: str, url: str) -> None:
    text = _safe(deps.logs, name)
    if not isinstance(text, str):
        return
    cap = 5 * 1024 * 1024
    if len(text) > cap:
        text = text[-cap:]
    try:
        d = run_dir / "logs"
        d.mkdir(exist_ok=True)
        (d / f"{role}.log").write_text(redact(text, url), encoding="utf-8")
    except OSError as e:
        logger.warning("[canary] лог %s не сохранён (%s)", role, type(e).__name__)


def _new_result(plan: RunPlan, reason: str, now: datetime, telemost_version: Optional[str]) -> Dict[str, Any]:
    return {
        "run_id": plan.run_id,
        "reason": reason,
        "image": plan.image,
        "room": plan.room,
        "meeting_id": plan.meeting_id,
        "code_word": plan.code_word,
        "telemost_version": telemost_version,
        "started_at": iso(now),
        "finished_at": None,
        "status": None,
        "skip_reason": None,
        "reasons": [],
        "details": {},
        "notes": [],
        "checks": {},
        "metrics": {},
        "speaker": {},
        "bot": {},
        "snapshot": None,
        "run_dir": str(plan.run_dir),
    }


def run_once(
    *,
    deps: Deps,
    image: str,
    room: str,
    meeting_url: str,
    reason: str,
    kit: Optional[Path] = None,
    telemost_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Один полный прогон. Защиты и замок — снаружи (`run_cycle`)."""
    kit = kit or kit_dir()
    now = deps.now()
    run_id = make_run_id(now)
    run_dir = canary_root() / run_id
    phrases = load_phrases(kit)
    if not phrases:
        return {"run_id": run_id, "reason": reason, "image": image, "room": room, "status": ERROR,
                "reasons": ["kit_missing"], "details": {}, "started_at": iso(now), "finished_at": iso(now),
                "run_dir": None}
    phrase = deps.rng.choice(phrases)
    plan = RunPlan(
        run_id=run_id, run_dir=run_dir, image=image, room=room, meeting_url=meeting_url,
        phrase_file=phrase["file"], code_word=phrase["word"], meeting_id=canary_meeting_id(now), kit=kit,
    )
    res = _new_result(plan, reason, now, telemost_version)
    _make_run_dir(run_dir)
    speaker, bot = speaker_container(run_id), bot_container(run_id)
    started: List[str] = []
    flow: Dict[str, Any] = {"busy": None, "red": None, "notes": []}
    logger.info("[canary] прогон %s: комната %s, образ %s, причина %s", run_id, room, image, reason)

    def red(code: str, detail: Any = None, snapshot: Optional[str] = None) -> None:
        flow["red"] = (code, detail, snapshot)

    def speaker_view() -> Dict[str, Any]:
        st = _read_speaker(run_dir)
        res["speaker"] = {k: st.get(k) for k in ("phase", "reason", "others", "count_method", "mic_ui", "plays_done")
                          if st.get(k) is not None}
        return st

    try:
        ok, err = deps.docker_run(speaker_docker_cmd(plan))
        if not ok:
            red("speaker_start_failed", redact(err, meeting_url)[:200] or None)
        else:
            started.append(speaker)
            _speaker_to_bot(deps, plan, res, flow, speaker_view, red, started)
    finally:
        busy = flow["busy"]
        if busy is not None:
            # Посторонние: бот уходит сразу, контейнеры — стоп, записи — удалить.
            if bot in started:
                _safe(deps.publish, leave_channel(plan.meeting_id), LEAVE_MESSAGE)
            _safe(_write_cmd, run_dir, "leave", 9)
            for name in started:
                _safe(deps.stop, name)
        else:
            # Кто ещё в звонке (красный на середине) — сперва просим уйти штатно.
            if bot in started and (_safe(deps.container_state, bot) or ("unknown",))[0] not in _GONE:
                _safe(deps.publish, leave_channel(plan.meeting_id), LEAVE_MESSAGE)
            if speaker in started:
                _safe(_write_cmd, run_dir, "leave", 9)
            for name, role in ((speaker, "speaker"), (bot, "bot")):
                if name in started:
                    cs = _safe(deps.container_state, name) or ("unknown", None)
                    if cs[0] not in _GONE:
                        _safe(deps.stop, name)
                    _save_logs(deps, run_dir, name, role, meeting_url)
        for name in started:
            _safe(deps.remove, name)

    res["finished_at"] = iso(deps.now())
    res["notes"] = flow["notes"]
    if flow["busy"] is not None:
        res["status"] = SKIPPED
        res["skip_reason"] = "room_busy"
        res["details"] = {"room_busy": flow["busy"]}
        res["run_dir"] = None if _remove_run_dir(run_dir) else str(run_dir)
        res["recordings_deleted"] = res["run_dir"] is None
        logger.warning("[canary] %s: комната занята (%s) — записи прогона удалены", run_id, flow["busy"])
        return res
    if flow["red"] is not None and flow["red"][0] not in ("speaker_mic_failed", "speak_timeout", "speaker_lost_speaking"):
        code, detail, snap = flow["red"]
        if code == "speaker_setup_failed":
            res["status"] = ERROR
        else:
            res["status"] = RED
        res["reasons"] = [code]
        res["details"] = {code: detail} if detail not in (None, "") else {}
        res["snapshot"] = snap or str(run_dir)
        logger.warning("[canary] %s: %s (%s)", run_id, res["status"], code)
        return res

    _evaluate_run(deps, plan, res, flow)
    logger.info("[canary] %s: итог %s %s", run_id, res["status"], ",".join(res["reasons"]))
    return res


def _speaker_to_bot(deps, plan, res, flow, speaker_view, red, started) -> None:  # noqa: ANN001
    run_dir, run_id = plan.run_dir, plan.run_id
    speaker, bot = speaker_container(run_id), bot_container(run_id)

    # 1. Диктор входит и проверяет, что он один.
    def s1() -> Optional[Dict[str, Any]]:
        st = speaker_view()
        if st.get("phase") in ("alone", "busy", "failed", "left"):
            return st
        cs, code = deps.container_state(speaker)
        if cs in _GONE:
            return {"phase": "crashed", "exit_code": code}
        return None

    st = _poll(deps, SPEAKER_ALONE_TIMEOUT_SEC, s1)
    if st is None:
        return red("speaker_timeout")
    ph = st.get("phase")
    if ph == "busy":
        flow["busy"] = st.get("reason") or "strangers"
        return None
    if ph == "failed":
        reason = str(st.get("reason") or "join_error")
        if reason in SPEAKER_SETUP_REASONS:
            return red("speaker_setup_failed", reason)
        return red("speaker_join_failed", reason, _host_path(run_dir, st.get("crash_kit")))
    if ph != "alone":
        return red("speaker_crashed", st.get("exit_code"))

    # 2. Бот.
    ok, err = deps.docker_run(bot_docker_cmd(plan))
    if not ok:
        return red("bot_start_failed", redact(err, plan.meeting_url)[:200] or None)
    started.append(bot)

    def s2() -> Optional[Tuple[str, Dict[str, Any]]]:
        sp = speaker_view()
        if sp.get("phase") == "busy":
            return ("busy", sp)
        if sp.get("phase") in ("failed", "left"):
            return ("speaker_lost", sp)
        bs = _read_bot_status(run_dir, run_id)
        res["bot"] = {k: bs.get(k) for k in ("phase", "reason") if bs.get(k)}
        if bs.get("phase") == "in_call":
            return ("in_call", bs)
        if bs.get("phase") == "failed":
            return ("failed", bs)
        cs, _code = deps.container_state(bot)
        if cs in _GONE:
            return ("bot_gone", bs)
        cs2, _ = deps.container_state(speaker)
        if cs2 in _GONE:
            return ("speaker_lost", sp)
        return None

    r = _poll(deps, BOT_IN_CALL_TIMEOUT_SEC, s2)
    if r is None:
        last = _read_bot_status(run_dir, run_id).get("phase")
        return red("bot_join_timeout", last or "нет статуса")
    kind, data = r
    if kind == "busy":
        flow["busy"] = data.get("reason") or "strangers"
        return None
    if kind == "speaker_lost":
        return red("speaker_lost", data.get("reason"))
    if kind == "failed":
        return red("bot_join_failed", data.get("reason") or "join_error", _host_path(run_dir, data.get("crash_kit")))
    if kind == "bot_gone":
        return red("bot_crashed")

    # 3. Голос.
    _write_cmd(run_dir, "speak", 1)

    def s3() -> Optional[Tuple[str, Dict[str, Any]]]:
        sp = speaker_view()
        if sp.get("phase") in ("spoke", "busy", "failed", "left"):
            return (sp["phase"], sp)
        cs, _ = deps.container_state(speaker)
        if cs in _GONE:
            return ("left", sp)
        return None

    r = _poll(deps, SPOKE_TIMEOUT_SEC, s3)
    if r is None:
        flow["notes"].append("speak_timeout")
        red("speak_timeout")
    else:
        kind, sp = r
        if kind == "busy":
            flow["busy"] = sp.get("reason") or "strangers"
            return None
        if kind == "failed" and sp.get("reason") in SPEAKER_SETUP_REASONS:
            red("speaker_setup_failed", sp.get("reason"))  # звук диктора отказал — сбой проверки
        elif kind in ("failed", "left"):
            flow["notes"].append("speaker_lost_speaking")
            red("speaker_lost_speaking", sp.get("reason"))
        elif sp.get("mic_ui") == "off":
            flow["notes"].append("speaker_mic_failed")
            red("speaker_mic_failed")

    # 4. Хвост записи, не упуская посторонних.
    def s4() -> Optional[str]:
        sp = speaker_view()
        return sp.get("reason") or "strangers" if sp.get("phase") == "busy" else None

    busy = _poll(deps, AFTER_SPEAK_PAUSE_SEC, s4)
    if busy is not None:
        flow["busy"] = busy
        return None

    # 5. Штатный выход: бот — по команде leave (запись дописывается), диктор — по cmd.
    got = _safe(deps.publish, leave_channel(plan.meeting_id), LEAVE_MESSAGE)
    _write_cmd(run_dir, "leave", 2)
    if got is False:
        flow["notes"].append("leave_not_received")
        _safe(deps.stop, bot)  # SIGTERM — бот уходит штатно и дописывает запись

    def gone(name: str) -> Callable[[], Optional[bool]]:
        return lambda: True if deps.container_state(name)[0] in _GONE else None

    if _poll(deps, BOT_EXIT_TIMEOUT_SEC, gone(bot)) is None:
        flow["notes"].append("bot_exit_timeout")
    if _poll(deps, SPEAKER_EXIT_TIMEOUT_SEC, gone(speaker)) is None:
        flow["notes"].append("speaker_exit_timeout")
    res["bot"]["exit_code"] = deps.container_state(bot)[1]
    return None


def _top_files(run_dir: Path, pattern: str) -> List[Path]:
    out = []
    for p in sorted(run_dir.glob(pattern)):
        try:
            if p.is_file() and not p.is_symlink():
                out.append(p)
        except OSError:
            continue
    return out


def _evaluate_run(deps: Deps, plan: RunPlan, res: Dict[str, Any], flow: Dict[str, Any]) -> None:
    run_dir = plan.run_dir
    bs = _read_bot_status(run_dir, plan.run_id)
    phases = tuple(h.get("phase") for h in (bs.get("history") or []) if isinstance(h, dict) and h.get("phase"))
    wavs = _top_files(run_dir, "*.wav")
    durations = tuple(_safe(deps.probe_duration, w) for w in wavs)
    volumes = [(w, _safe(deps.volume, w)) for w in wavs]
    measured = [(w, v) for w, v in volumes if isinstance(v, tuple) and len(v) == 2]
    best = max((v for _w, v in measured), key=lambda v: v[1], default=None)

    found: Optional[bool] = None
    chars = 0
    for w, _v in sorted(measured, key=lambda wv: wv[1][1], reverse=True)[:TRANSCRIBE_MAX_FILES]:
        try:
            text = deps.transcribe(w)
        except Exception as e:  # noqa: BLE001 — whisper лёг: слово не проверено, не «нет слова»
            logger.warning("[canary] расшифровка %s не удалась (%s)", w.name, type(e).__name__)
            continue
        chars += len(text or "")
        hit = code_word_found(text or "", plan.code_word)
        found = bool(found) or hit
        text = ""  # текст дальше не живёт — опасная тройка
        if hit:
            break
    logger.info("[canary] расшифровка: кодовое слово найдено=%s, длина текста=%d", found, chars)

    participants: Optional[Tuple[str, ...]] = None
    for mp in _top_files(run_dir, "*.meta.json"):
        meta = _dict(read_json(mp))
        names = tuple(n for n in (meta.get("participants") or []) if isinstance(n, str))
        participants = (participants or ()) + names

    ev = Evidence(
        bot_phase=bs.get("phase") if isinstance(bs.get("phase"), str) else None,
        bot_phases_seen=phases,
        wav_durations=durations,
        volume=best,
        code_word_found=found,
        participants=participants,
    )
    out = evaluate(ev)
    reasons, details = list(out["reasons"]), dict(out["details"])
    if "bot_not_joined" in details:
        details["bot_not_joined"] = bs.get("reason") or bs.get("phase") or "нет статуса"
    if out["status"] == RED and flow.get("red") is not None:
        # Корень — у диктора (не сказал / выпал / микрофон): ставим первым.
        code, detail, _snap = flow["red"]
        if code == "speaker_lost_speaking":
            code = "speaker_lost"
        reasons = [code] + [r for r in reasons if r != code]
        if detail not in (None, ""):
            details[code] = detail
    res.update({
        "status": out["status"],
        "reasons": reasons,
        "details": details,
        "checks": out["checks"],
        "metrics": {**out["metrics"], "transcript_chars": chars},
    })
    if out["status"] == RED:
        crash = _host_path(run_dir, bs.get("crash_kit"))
        res["snapshot"] = crash or str(run_dir)


# ---------------------------------------------------------------------------
# Цикл: защиты → прогон → повтор → состояние → сообщения
# ---------------------------------------------------------------------------


def _mini(result: Dict[str, Any]) -> Dict[str, Any]:
    return {k: result.get(k) for k in ("run_id", "status", "skip_reason", "reasons") if result.get(k) is not None}


def apply_final(
    health: Dict[str, Any],
    final: Dict[str, Any],
    now: datetime,
    *,
    first: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[Tuple[str, bool]]]:
    """Новое состояние «бот здоров/сломан» + сообщения владельцу [(текст, беззвучно)]."""
    h = dict(health or {})
    msgs: List[Tuple[str, bool]] = []
    status = final.get("status")
    prev = h.get("status")
    night = is_night(now)
    room = room_label(str(final.get("room") or DEFAULT_ROOM))
    h["last_run_at"] = iso(now)
    h["run_id"] = final.get("run_id")
    if final.get("image"):
        h["image"] = final["image"]
    if final.get("telemost_version"):
        h["telemost_version"] = final["telemost_version"]

    if status in (SKIPPED, ERROR, RED_UNCONFIRMED):
        h["last_skip"] = {"at": iso(now), "status": status, "reason": final.get("skip_reason")
                          or ",".join(final.get("reasons") or [])}
        if status == RED_UNCONFIRMED:
            h["pending_recheck"] = True
            h["last_unconfirmed_red"] = {"at": iso(now), **_mini(first or final)}
        h["consecutive_skips"] = int(h.get("consecutive_skips") or 0) + 1
        if h["consecutive_skips"] == SKIP_ALERT_AFTER:
            msgs.append((
                f"ℹ️ Самопроверка бота в {room} не доходит до конца уже {SKIP_ALERT_AFTER} раза подряд: "
                f"{human_reasons(final)}. Бот при этом не проверен — если так пойдёт и дальше, "
                f"поломку Телемоста узнаем только на встрече.",
                True,
            ))
        if status == ERROR:
            key = "error:" + ((final.get("reasons") or ["internal_error"])[0])
            if _alert_due(h, key, now):
                msgs.append((
                    f"⚙️ Самопроверка бота не выполнилась: {human_reasons(final)}. "
                    f"Это сбой самой проверки, не бота.",
                    True,
                ))
        return h, msgs

    h["consecutive_skips"] = 0
    h["pending_recheck"] = False
    h["reasons"] = list(final.get("reasons") or [])
    h["reasons_human"] = human_reasons(final) if final.get("reasons") else ""
    if status != prev or not h.get("since"):
        h["since"] = iso(now)
    h["status"] = status
    if status == GREEN:
        h["last_green_at"] = iso(now)
    if status in (GREEN, YELLOW):
        h["last_pass_at"] = iso(now)
        if first and first.get("status") == RED:
            h["last_flake"] = {"at": iso(now), **_mini(first)}

    if status == RED:
        key = (final.get("reasons") or ["unknown"])[0]
        if _alert_due(h, key, now):
            twice = " (дважды)" if first and first.get("status") == RED else ""
            snap = final.get("snapshot")
            msgs.append((
                f"🔴 Самопроверка бота в {room} не прошла{twice}: {human_reasons(final)}. "
                f"Бот может не зайти на встречи." + (f" Снимок: {snap}" if snap else ""),
                night,
            ))
    elif prev == RED:
        extra = f" (с замечанием: {human_reasons(final)})" if status == YELLOW else ""
        msgs.append((f"🟢 Самопроверка бота в {room} снова проходит{extra}.", night))
    elif status == YELLOW and prev != YELLOW:
        msgs.append((
            f"🟡 Самопроверка бота в {room} прошла с замечанием: {human_reasons(final)}. "
            f"Не срочно: бот заходит и пишет.",
            True,
        ))
    return h, msgs


def _alert_due(h: Dict[str, Any], key: str, now: datetime) -> bool:
    """Дедуп по ключу причины на сутки. Отмечает отправку в состоянии."""
    alerts = {k: v for k, v in _dict(h.get("alerts")).items()
              if (parse_ts(v) or now) > now - timedelta(days=7)}
    last = parse_ts(alerts.get(key))
    if last is not None and now - last < timedelta(hours=ALERT_DEDUPE_HOURS):
        h["alerts"] = alerts
        return False
    alerts[key] = iso(now)
    h["alerts"] = alerts
    return True


def _skipped(reason: str, now: datetime, *, image: str, room: str, why: str, detail: Any = None) -> Dict[str, Any]:
    r = {"run_id": make_run_id(now), "reason": why, "image": image, "room": room, "status": SKIPPED,
         "skip_reason": reason, "reasons": [], "details": {}, "started_at": iso(now), "finished_at": iso(now),
         "run_dir": None}
    if detail is not None:
        r["details"] = {reason: detail}
    return r


def _error(code: str, now: datetime, *, image: str, room: str, why: str) -> Dict[str, Any]:
    return {"run_id": make_run_id(now), "reason": why, "image": image, "room": room, "status": ERROR,
            "reasons": [code], "details": {}, "started_at": iso(now), "finished_at": iso(now), "run_dir": None}


def _current_version() -> Optional[str]:
    v = load_version_state().get("version")
    return v if isinstance(v, str) else None


def _cleanup_leftovers(deps: Deps) -> None:
    """Контейнеры самопроверки от прерванного прогона (systemd убил по сроку) — наши, убрать."""
    for name in _safe(deps.canary_leftovers) or []:
        if isinstance(name, str) and name.startswith(CONTAINER_PREFIX):
            logger.warning("[canary] убираю оставшийся контейнер прошлого прогона %s", name)
            _safe(deps.stop, name)
            _safe(deps.remove, name)


def _notify_all(deps: Deps, msgs: List[Tuple[str, bool]]) -> None:
    for text, silent in msgs:
        try:
            deps.notify(text, silent)
        except Exception as e:  # noqa: BLE001
            logger.warning("[canary] сообщение владельцу не ушло (%s)", type(e).__name__)


def run_cycle(
    *,
    deps: Deps,
    image: str = DEFAULT_IMAGE,
    room: str = DEFAULT_ROOM,
    reason: str = "manual",
    force: bool = False,
    kit: Optional[Path] = None,
) -> Dict[str, Any]:
    """Защиты → прогон → (красный) пауза и второй прогон → состояние → сообщения."""
    fd = try_lock()
    if fd is None:
        logger.info("[canary] уже идёт другая самопроверка — выхожу")
        return _skipped("lock_busy", deps.now(), image=image, room=room, why=reason)
    try:
        return _run_cycle_locked(deps=deps, image=image, room=room, reason=reason, force=force, kit=kit)
    finally:
        unlock(fd)


def _preflight(deps: Deps, room: str) -> Tuple[Optional[str], Optional[str]]:
    """(ссылка, None) или (None, код ошибки). Ссылку не логируем."""
    try:
        url = deps.room_url(room)
    except Exception as e:  # noqa: BLE001
        logger.warning("[canary] rooms.yaml не прочитан (%s)", type(e).__name__)
        return None, "room_not_found"
    if not url:
        return None, "room_not_found"
    if not valid_room_url(url):
        return None, "room_url_invalid"
    return url, None


def _run_cycle_locked(*, deps, image, room, reason, force, kit) -> Dict[str, Any]:  # noqa: ANN001
    start = deps.now()
    health = load_health()

    def finish(final: Dict[str, Any], first: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        new_h, msgs = apply_final(health, final, deps.now(), first=first)
        try:
            save_health(new_h)
        except OSError as e:
            logger.warning("[canary] health.json не сохранён (%s)", type(e).__name__)
        _notify_all(deps, msgs)
        append_journal({
            "at": iso(deps.now()), "reason": reason, "final": _mini(final),
            "first": _mini(first) if first and first is not final else None,
            "note": "разовый сбой" if first and first.get("status") == RED and final.get("status") in (GREEN, YELLOW)
            else None,
        })
        return final

    url, err = _preflight(deps, room)
    if err:
        return finish(_error(err, start, image=image, room=room, why=reason))
    kit = kit or kit_dir()
    if not load_phrases(kit):
        return finish(_error("kit_missing", start, image=image, room=room, why=reason))

    def guards() -> Optional[str]:
        return guard_reason(_safe(deps.containers), _safe(deps.read_queue) or [], deps.now(), force=force)

    g = guards()
    if g:
        logger.info("[canary] прогон не начат: %s", g)
        return finish(_skipped(g, start, image=image, room=room, why=reason))
    _cleanup_leftovers(deps)

    version = _current_version()
    try:
        first = run_once(deps=deps, image=image, room=room, meeting_url=url, reason=reason, kit=kit,
                         telemost_version=version)
    except Exception:  # noqa: BLE001
        logger.exception("[canary] прогон упал")
        return finish(_error("internal_error", start, image=image, room=room, why=reason))
    _safe(save_run, first)
    if first.get("status") != RED:
        return finish(first)

    # Красный — перепроверяем, прежде чем тревожить владельца.
    elapsed = (deps.now() - start).total_seconds()
    if elapsed + RETRY_PAUSE_SEC + MAX_RUN_SEC > CYCLE_BUDGET_SEC:
        logger.warning("[canary] перепроверку не успеть в срок юнита — отложена до «нюха»")
        return finish({**first, "status": RED_UNCONFIRMED, "skip_reason": "retry_deferred"}, first)
    deps.sleep(RETRY_PAUSE_SEC)
    g = guards()
    if g:
        logger.warning("[canary] перепроверка не начата (%s) — отложена до «нюха»", g)
        return finish({**first, "status": RED_UNCONFIRMED, "skip_reason": g}, first)
    try:
        second = run_once(deps=deps, image=image, room=room, meeting_url=url, reason=reason + "-recheck",
                          kit=kit, telemost_version=version)
    except Exception:  # noqa: BLE001
        logger.exception("[canary] перепроверка упала")
        return finish({**first, "status": RED_UNCONFIRMED, "skip_reason": "internal_error"}, first)
    _safe(save_run, second)
    if second.get("status") in (SKIPPED, ERROR):
        return finish({**first, "status": RED_UNCONFIRMED,
                       "skip_reason": second.get("skip_reason") or (second.get("reasons") or ["?"])[0]}, first)
    if second.get("status") in (GREEN, YELLOW):
        logger.warning("[canary] первый прогон красный, второй %s — разовый сбой", second.get("status"))
    return finish(second, first)


# ---------------------------------------------------------------------------
# «Нюх» версии Телемоста
# ---------------------------------------------------------------------------

_VERSION = r"(\d+(?:\.\d+){1,3})"
_VERSION_PATTERNS = (
    re.compile(r"telemessenger/_/" + _VERSION + r"/web/app\.js"),
    re.compile(r"telemessenger[^\"'\s<>]{0,120}?/_/" + _VERSION + r"/"),
    re.compile(r"/_/" + _VERSION + r"/[^\"'\s<>]{0,120}?telemessenger"),
)


def parse_version(html: str) -> Optional[str]:
    """Версия фронта Телемоста по пути скрипта `telemessenger/_/<версия>/web/app.js`."""
    for pat in _VERSION_PATTERNS:
        m = pat.search(html or "")
        if m:
            return m.group(1)
    return None


def sniff_update(
    state: Dict[str, Any], version: Optional[str], error: Optional[str], now: datetime
) -> Tuple[Dict[str, Any], List[Tuple[str, bool]], bool]:
    """(новое состояние, сообщения, версия сменилась). Чистая функция."""
    st = dict(state or {})
    msgs: List[Tuple[str, bool]] = []
    if error or not version:
        st["consecutive_errors"] = int(st.get("consecutive_errors") or 0) + 1
        st["last_error"] = {"at": iso(now), "error": error or "version_not_found"}
        if st["consecutive_errors"] >= SNIFF_ERROR_ALERT_AFTER and not st.get("error_alerted"):
            st["error_alerted"] = True
            msgs.append((
                f"ℹ️ Не могу узнать версию Телемоста уже {st['consecutive_errors']} раз подряд "
                f"({error or 'version_not_found'}). Если Телемост обновится, внеплановой самопроверки не будет — "
                f"останется ежедневная в 04:40 МСК.",
                True,
            ))
        return st, msgs, False
    st["consecutive_errors"] = 0
    st["error_alerted"] = False
    st["checked_at"] = iso(now)
    prev = st.get("version")
    if prev == version:
        return st, msgs, False
    history = [h for h in (st.get("history") or []) if isinstance(h, dict)]
    history.append({"version": version, "seen_at": iso(now)})
    st["history"] = history[-VERSION_HISTORY:]
    st["version"] = version
    st["seen_at"] = iso(now)
    if not prev:
        return st, msgs, False  # первое наблюдение — только записать
    st["pending_check"] = True
    st.pop("retry_after", None)
    msgs.append((f"Телемост обновился: {prev} → {version}. Запускаю внеплановую самопроверку.", True))
    return st, msgs, True


def sniff(
    *,
    deps: Deps,
    image: str = DEFAULT_IMAGE,
    room: str = DEFAULT_ROOM,
    run: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Посмотреть версию; сменилась или висит отложенная проверка → прогон."""
    now = deps.now()
    version: Optional[str] = None
    error: Optional[str] = None
    try:
        if deps.http_get is None:
            raise RuntimeError("no_http")
        code, html = deps.http_get(TELEMOST_HOME)
        if code != 200:
            error = f"http_{code}"
        else:
            version = parse_version(html)
            if not version:
                error = "version_not_found"
    except Exception as e:  # noqa: BLE001
        error = type(e).__name__
    st, msgs, changed = sniff_update(load_version_state(), version, error, now)
    write_json(version_path(), st)
    _notify_all(deps, msgs)
    logger.info("[canary] нюх: версия=%s ошибка=%s смена=%s", version, error, changed)

    out: Dict[str, Any] = {"version": version, "error": error, "changed": changed, "run": None}
    health = load_health()
    pending = bool(st.get("pending_check")) or bool(health.get("pending_recheck"))
    if not pending:
        return out
    retry_after = parse_ts(st.get("retry_after"))
    if retry_after and now < retry_after:
        logger.info("[canary] внеплановая проверка отложена до %s (комната была занята)", st["retry_after"])
        return out
    why = "version-change" if st.get("pending_check") else "recheck"
    result = (run or run_cycle)(deps=deps, image=image, room=room, reason=why, force=False)
    out["run"] = _mini(result)
    st = load_version_state()
    status = result.get("status")
    if status == SKIPPED:
        if result.get("skip_reason") == "room_busy":
            st["retry_after"] = iso(now + timedelta(minutes=BUSY_RETRY_BACKOFF_MIN))
        # остальные защиты (идёт бот, скоро встреча, замок) — просто попробуем через час
    else:
        st["pending_check"] = False
        st.pop("retry_after", None)
    write_json(version_path(), st)
    return out


# ---------------------------------------------------------------------------
# Предупреждение перед встречами, пока самопроверка красная
# ---------------------------------------------------------------------------


def is_red(health: Optional[Dict[str, Any]]) -> bool:
    """Самопроверка сейчас красная (подтверждённо, после повтора)."""
    return (health or {}).get("status") == RED


def warn_enabled() -> bool:
    return (os.environ.get("MEETING_NOTARY_CANARY_WARN") or "1").strip().lower() not in {"0", "false", "no", "off"}


def _warn_key(item: Dict[str, Any], start: datetime) -> str:
    ident = item.get("event_id") or item.get("order_id") or item.get("meeting_id") or item.get("series") or "?"
    return f"{ident}@{start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M')}"


def due_warnings(
    queue: Sequence[Any], health: Dict[str, Any], warned: Dict[str, Any], now: datetime
) -> List[Tuple[str, Dict[str, Any], datetime]]:
    """Встречи через 8–12 минут, ещё не предупреждённые, — если самопроверка красная."""
    if not is_red(health):
        return []
    out = []
    for item in queue or []:
        if not isinstance(item, dict):
            continue
        start = parse_ts(item.get("start_at"))
        if not start:
            continue
        mins = (start - now).total_seconds() / 60.0
        if not (WARN_FROM_MIN <= mins <= WARN_TO_MIN):
            continue
        key = _warn_key(item, start)
        if key in (warned or {}):
            continue
        out.append((key, item, start))
    return out


def warning_text(health: Dict[str, Any], item: Dict[str, Any], start: datetime) -> str:
    try:
        from notary.lib.join_alert import meeting_name  # noqa: PLC0415

        name = meeting_name(item)
    except Exception:  # noqa: BLE001
        name = str(item.get("series") or "встреча")
    since = parse_ts(health.get("since"))
    since_txt = msk_day_hm(since) if since else "недавно"
    codes = health.get("reasons") or []
    why = health.get("reasons_human") or (human_reason(codes[0]) if codes else "причина не записана")
    text = (
        f"⚠️ Самопроверка бота красная с {since_txt}: {why}. Встреча «{name}» в {msk_hm(start)} — "
        f"бот может не зайти. Подстрахуйся: включи запись в Телемосте."
    )
    if item.get("order_id"):
        text += " Это встреча сотрудников с портала — предупреди организатора."
    return text


def warn_tick(
    queue: Sequence[Any],
    now: datetime,
    *,
    notify: Optional[Callable[[str], Any]] = None,
) -> int:
    """Из тика запускателя. Возвращает число отправленных предупреждений.

    Сначала запись «предупреждён», потом отправка: не сохранилось — не шлём
    (иначе при сломанной записи владелец получал бы одно и то же каждую минуту).
    """
    health = load_health()
    if not is_red(health):
        return 0  # дёшево: без красного даже warned.json не читаем
    warned_raw = _dict(read_json(warned_path()))
    warned = {k: v for k, v in warned_raw.items()
              if (parse_ts(v) or now) > now - timedelta(hours=WARN_TTL_HOURS)}
    due = due_warnings(queue, health, warned, now)
    if not due and len(warned) == len(warned_raw):
        return 0
    for key, _item, _start in due:
        warned[key] = iso(now)
    try:
        write_json(warned_path(), warned)
    except OSError as e:
        logger.warning("[canary] warned.json не сохранён (%s) — предупреждения не отправлены", type(e).__name__)
        return 0
    if notify is None:
        from notary.lib.notify import push  # noqa: PLC0415

        notify = lambda text: push(text, dedupe=False)  # noqa: E731
    sent = 0
    for _key, item, start in due:
        try:
            notify(warning_text(health, item, start))
            sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[canary] предупреждение не ушло (%s)", type(e).__name__)
    if sent:
        logger.info("[canary] предупредил владельца о %d встреч(е/ах): самопроверка красная", sent)
    return sent


# ---------------------------------------------------------------------------
# Сводка для `status`
# ---------------------------------------------------------------------------

_STATUS_WORD = {GREEN: "🟢 проходит", YELLOW: "🟡 проходит с замечанием", RED: "🔴 не проходит"}


def status_text() -> str:
    h = load_health()
    v = load_version_state()
    lines = []
    st = h.get("status")
    lines.append(f"Самопроверка: {_STATUS_WORD.get(st, 'ещё не было прогонов')}"
                 + (f" с {msk_day_hm(parse_ts(h['since']))}" if parse_ts(h.get("since")) else ""))
    if h.get("reasons_human"):
        lines.append(f"Причины: {h['reasons_human']}")
    for label, key in (("Последний прогон", "last_run_at"), ("Последний зелёный", "last_green_at")):
        ts = parse_ts(h.get(key))
        if ts:
            lines.append(f"{label}: {msk_day_hm(ts)}")
    if h.get("last_skip"):
        s = h["last_skip"]
        lines.append(f"Последний пропуск: {s.get('status')} {s.get('reason')} ({s.get('at')})")
    if h.get("pending_recheck"):
        lines.append("Ждёт перепроверки красного (сделает часовой «нюх»).")
    if h.get("image"):
        lines.append(f"Образ: {h['image']}")
    lines.append(f"Телемост: версия {v.get('version') or '?'}"
                 + (" — ждёт внеплановой проверки" if v.get("pending_check") else "")
                 + (f", ошибок загрузки подряд: {v['consecutive_errors']}" if v.get("consecutive_errors") else ""))
    return "\n".join(lines)


def summary_line(result: Dict[str, Any]) -> str:
    st = result.get("status")
    if st in (GREEN,):
        return f"{result.get('run_id')}: 🟢 зелёный"
    if st == SKIPPED:
        return f"{result.get('run_id')}: пропуск — {human_reasons(result)}"
    return f"{result.get('run_id')}: {st} — {human_reasons(result)}"
