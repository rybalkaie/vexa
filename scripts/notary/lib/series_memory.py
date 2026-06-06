"""Ф7: память серии встреч — компактные выжимки прошлых встреч серии.

На регулярных и повторных встречах бот опирается на выжимки прошлых встреч
той же серии: узнаёт постоянных участников, держит сквозные темы/термины,
видит динамику чисел. Протокол при этом строится в продолжающемся контексте,
НО факты по-прежнему берутся строго из текущей записи (см. `format_memory_block`
и ревью-секцию "memory" в `llm_postprocess`).

Состав модуля:
  - 7.1  `build_digest`        — детерминированная выжимка из готового протокола
                                 (участники, темы, ключевые пункты/числа/решения);
         `save_digest`/`load_digest`/`list_series_digests`/`prune_old_digests`
                                 — хранилище рядом с протоколом (`<date>-memory.json`).
  - 7.2  `resolve_memory`      — резолвер серии: (а) прошлые выжимки той же серии
                                 (стабильный slug из календаря/watched.yaml), (б)
                                 fallback по совпадению состава участников
                                 (нерегулярные 1-на-1).
  - 7-связка `permanent_participants` — постоянный состав из памяти серии для
                                 обогащения `expected_participants` (усиливает 5.4/6.1).
  - 7.3/7.4 `format_memory_block` — справочный блок для промта генерации с явной
                                 дисциплиной «прошлое = справка, не факт».
  - 7.5  `backfill_series`/`backfill_root` — разовый сбор выжимок из уже лежащих
                                 протоколов (детерминированно, без claude).

РИСК4 / «опасная тройка» (решение владельца 2026-05-26). Память серии — новое
хранилище производных от ПДн данных. Дисциплина зашита в код:
  • выжимка = ТОЛЬКО производные поля (участники, темы, ключевые пункты), которые
    уже прошли сжатие в протоколе. НИКОГДА не сырой транскрипт и не сырые реплики;
  • текст выжимки НЕ логируется (логируем только счётчики/серию/дату);
  • срок хранения ограничен (`SERIES_MEMORY_RETENTION_DAYS`, прунинг старых файлов);
  • генерация выжимки детерминированна (без claude) — в Claude уходит только уже
    собранный справочный блок на генерации протокола (тот же путь, что транскрипт).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("meeting_notary.series_memory")

# Версия схемы выжимки — на случай миграции формата.
SCHEMA_VERSION = 1

# Имя файла выжимки рядом с протоколом `<date>-protokol.md`.
MEMORY_FILE_SUFFIX = "-memory.json"
PROTOCOL_FILE_SUFFIX = "-protokol.md"

# Дефолты (переопределяются env, см. ниже).
DEFAULT_DEPTH = 3
DEFAULT_RETENTION_DAYS = 180

# Лимиты состава выжимки (РИСК4: компактность + предсказуемый размер хранилища).
_MAX_PARTICIPANTS = 12
_MAX_THEMES = 14
_MAX_KEY_POINTS = 12
_MAX_POINT_LEN = 200

# Маркеры протокола (см. методичку kak-delat-protokol-vstrechi.md и реальный
# пример `встречи/.../<date>-protokol.md`): темы `## N) Тема`, буллеты
# ▪️/▫️ (тематические), 🔸 (решения), 🟠 (срочные задачи).
_HEADING_RE = re.compile(r"^\s{0,3}#{2,4}\s+(.*\S)\s*$")
# Срезаем нумерацию «1) », «2. » в начале заголовка темы.
_THEME_NUM_PREFIX_RE = re.compile(r"^\d+[.)]\s*")
# Любой буллет-маркер в начале строки (функциональные эмодзи + markdown-дефис).
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|▪️|▫️|🔸|🟠|🔹|◾|◽)\s*")
# ⚠️-пометка ревью (5.2/6.2) — срезаем из выжимки, это не контент встречи.
_REVIEW_FLAG_RE = re.compile(r"\s*⚠️.*$")
_HAS_DIGIT_RE = re.compile(r"\d")

# Служебные заголовки протокола — НЕ темы встречи.
_DECISION_HEADING_KEYS = ("решен", "что внедряем", "договорил")
_TASK_HEADING_KEYS = ("задач",)
_SERVICE_HEADING_KEYS = ("провер", "что изменилось", "🔁", "приложен")

# Владелец присутствует на каждой встрече — при матчинге серии по составу
# участников его исключаем (иначе любая встреча «похожа» на любую). Можно
# переопределить через env `SERIES_MEMORY_OWNER_NAMES` (через запятую).
_DEFAULT_OWNER_TOKENS = ("илья", "рыбалка", "ilya", "rybalka")

# Порог совпадения состава для fallback-матчинга (Jaccard по не-владельцам).
_PARTICIPANT_MATCH_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# env-конфиг
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    """Kill-switch `ENABLE_SERIES_MEMORY` (дефолт ON; `0/false/no` → OFF).

    Нужен, чтобы при сбое в проде отключить чтение/запись памяти без передеплоя.
    """
    raw = (os.environ.get("ENABLE_SERIES_MEMORY") or "").strip().lower()
    return raw not in ("0", "false", "no")


def has_series_slug(series: Optional[str]) -> bool:
    """True, если у встречи есть осмысленный slug серии (не пусто/пробелы).

    Память серии работает ТОЛЬКО при наличии slug. Встреча без серии (legacy-путь
    `_output_dir_for_meta` → `<date>-<sid>.md` в КОРНЕ протоколов) папки-серии не
    имеет: её `series_dir` схлопывается в общий корень. Без этого гейта несвязанные
    series-less встречи делили бы один пул памяти (ложная «та же серия») — поэтому
    резолв/сохранение памяти на стороне finalize/clarify гейтятся этим предикатом.
    """
    return bool((series or "").strip())


def series_memory_depth() -> int:
    """Глубина контекста `SERIES_MEMORY_DEPTH` (число прошлых выжимок, дефолт 3).

    Невалидное/<=0 → дефолт. Жёсткий потолок 20 — защита от случайного раздутия
    промта генерации.
    """
    raw = (os.environ.get("SERIES_MEMORY_DEPTH") or "").strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DEPTH
    if val <= 0:
        return DEFAULT_DEPTH
    return min(val, 20)


def retention_days() -> int:
    """Срок хранения выжимок `SERIES_MEMORY_RETENTION_DAYS` (дни, дефолт 180).

    `0` / отрицательное → 0 = «не прунить» (хранение без ограничения по времени).
    """
    raw = (os.environ.get("SERIES_MEMORY_RETENTION_DAYS") or "").strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return max(val, 0)


def _owner_tokens() -> tuple[str, ...]:
    raw = (os.environ.get("SERIES_MEMORY_OWNER_NAMES") or "").strip()
    if not raw:
        return _DEFAULT_OWNER_TOKENS
    toks = tuple(t.strip().lower() for t in raw.split(",") if t.strip())
    return toks or _DEFAULT_OWNER_TOKENS


# ---------------------------------------------------------------------------
# 7.1 — построение выжимки (детерминированно, без claude)
# ---------------------------------------------------------------------------
def _clean_bullet_text(line: str) -> str:
    """Текст буллета без маркера, ⚠️-пометки ревью и лишних пробелов, с обрезкой."""
    text = _BULLET_PREFIX_RE.sub("", line)
    text = _REVIEW_FLAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Срезаем markdown-эмфазу — она для отображения, в выжимке шумит при матчинге.
    text = re.sub(r"[*_`]{1,2}", "", text)
    if len(text) > _MAX_POINT_LEN:
        text = text[: _MAX_POINT_LEN - 1].rstrip() + "…"
    return text


def _is_bullet(line: str) -> bool:
    return bool(_BULLET_PREFIX_RE.match(line))


def _classify_heading(title: str) -> str:
    """Классифицирует заголовок: theme | decisions | tasks | service."""
    low = title.lower()
    if any(k in low for k in _SERVICE_HEADING_KEYS):
        return "service"
    if any(k in low for k in _DECISION_HEADING_KEYS):
        return "decisions"
    if any(k in low for k in _TASK_HEADING_KEYS):
        return "tasks"
    return "theme"


def _extract_participants_from_header(protocol_text: str) -> list[str]:
    """Парсит строку `**Участники:** Имя, Имя2` из шапки протокола."""
    m = re.search(r"^\s*\*\*Участники:\*\*\s*(.+)$", protocol_text, flags=re.MULTILINE)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw or raw == "—":
        return []
    out: list[str] = []
    for part in raw.split(","):
        nm = re.sub(r"[*_`]{1,2}", "", part).strip()
        if nm and nm not in out:
            out.append(nm)
    return out


def _norm_participants(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names or []:
        if not isinstance(n, str):
            continue
        nm = n.strip()
        if nm and nm not in out:
            out.append(nm)
    return out[:_MAX_PARTICIPANTS]


def extract_protocol_sections(protocol_text: str) -> tuple[list[str], list[str]]:
    """Возвращает (themes, key_points) из markdown-протокола.

    themes      — заголовки тематических блоков `## N) Тема` (без служебных).
    key_points  — решения (🔸 под «## Решения…») + тематические буллеты с числами
                  (для динамики чисел). Каждый — очищен от маркера/⚠️/эмфазы.

    Чистая функция (без IO) — основной объект unit-тестов 7.1.
    """
    themes: list[str] = []
    decisions: list[str] = []
    number_points: list[str] = []
    section = None  # theme|decisions|tasks|service|None (до первого заголовка)
    for raw_line in protocol_text.splitlines():
        hm = _HEADING_RE.match(raw_line)
        if hm:
            title = _THEME_NUM_PREFIX_RE.sub("", hm.group(1).strip()).strip()
            title = re.sub(r"[*_`]{1,2}", "", title).strip()
            section = _classify_heading(title)
            if section == "theme" and title and title not in themes:
                themes.append(title)
            continue
        if not _is_bullet(raw_line):
            continue
        if section == "decisions":
            txt = _clean_bullet_text(raw_line)
            if txt and txt not in decisions:
                decisions.append(txt)
        elif section == "theme":
            txt = _clean_bullet_text(raw_line)
            if txt and _HAS_DIGIT_RE.search(txt) and txt not in number_points:
                number_points.append(txt)
    # Решения приоритетнее — заполняем сначала ими, остаток добиваем числами.
    key_points: list[str] = []
    for p in decisions:
        if len(key_points) >= _MAX_KEY_POINTS:
            break
        key_points.append(p)
    for p in number_points:
        if len(key_points) >= _MAX_KEY_POINTS:
            break
        if p not in key_points:
            key_points.append(p)
    return themes[:_MAX_THEMES], key_points


def _norm_speaker_mapping(speaker_mapping: Optional[dict]) -> dict[str, str]:
    """Ф4б: нормализует cluster→name маппинг для хранения в выжимке.

    Пустые ключи/значения отбрасываем; всё к строкам. Возвращает {} если нечего
    хранить (тогда build_digest не кладёт ключ — поле остаётся ленивым, УПУ3).
    """
    out: dict[str, str] = {}
    if not isinstance(speaker_mapping, dict):
        return out
    for cluster, name in speaker_mapping.items():
        if cluster is None or name is None:
            continue
        c = str(cluster).strip()
        n = str(name).strip()
        if c and n:
            out[c] = n
    return out


def build_digest(
    protocol_text: str,
    meeting_meta: dict,
    *,
    date: Optional[str] = None,
    speaker_mapping: Optional[dict] = None,
) -> dict:
    """7.1: компактная выжимка-память из готового протокола.

    participants: ПРИОРИТЕТ — шапка протокола «**Участники:**» (её модель пишет по
    факту транскрипта = реально присутствовавшие); fallback на meta
    (participants ∪ expectedParticipants). Так в памяти оседают фактические
    участники, а не приглашённые-но-отсутствовавшие — важно для матчинга серии
    по составу (7.2) и постоянного состава (7-связка). Работает и для бэкфилла,
    где meta нет. themes/key_points — из протокола. НЕ кладём сырые реплики (РИСК4).

    Ф4б (REQ 1.2): `speaker_mapping` — резолвленное на этой встрече сопоставление
    cluster→имя (после правок авторства). Кладём ленивым ключом ТОЛЬКО если непусто
    (нет ключа в старых файлах → resolve_speaker_anchor вернёт {}, миграции/бэкфилл
    не нужны, УПУ3). НЕ ПДн сверх уже хранимого: имена и так есть в `participants`.

    Возвращает dict со схемой v1. `date` — YYYY-MM-DD (из аргумента или meta).
    """
    meta = meeting_meta or {}
    dt = (date or meta.get("date") or (meta.get("startTs") or "")[:10] or "").strip()
    series = (meta.get("series") or "").strip()

    participants = _norm_participants(_extract_participants_from_header(protocol_text))
    if not participants:
        expected = meta.get("expectedParticipants") or meta.get("expected_participants") or []
        panel = meta.get("participants") or []
        participants = _norm_participants(list(panel) + list(expected))

    themes, key_points = extract_protocol_sections(protocol_text or "")
    digest: dict = {
        "schema": SCHEMA_VERSION,
        "date": dt,
        "series": series,
        "participants": participants,
        "themes": themes,
        "key_points": key_points,
    }
    sm = _norm_speaker_mapping(speaker_mapping)
    if sm:
        digest["speaker_mapping"] = sm
    return digest


# ---------------------------------------------------------------------------
# хранилище
# ---------------------------------------------------------------------------
def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic write через tempfile + fsync + os.replace (как vstrechi_index)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def digest_path(series_dir: Path, date: str) -> Path:
    """Путь файла выжимки рядом с протоколом: `<series_dir>/<date>-memory.json`."""
    return Path(series_dir) / f"{date}{MEMORY_FILE_SUFFIX}"


def save_digest(series_dir: Path, date: str, digest: dict) -> Optional[Path]:
    """Сохраняет выжимку рядом с протоколом (atomic). Возвращает путь или None.

    НЕ логирует содержимое (РИСК4) — только серию/дату/счётчики. Best-effort: на
    сбой IO → None. Прунинг по сроку хранения — ОТДЕЛЬНОЙ операцией
    (`prune_old_digests`), её зовёт finalize по текущей серии; так бэкфилл старых
    протоколов не самоудаляет результат на лету.
    """
    if not date:
        return None
    path = digest_path(series_dir, date)
    try:
        text = json.dumps(digest, ensure_ascii=False, indent=2)
        _atomic_write_text(path, text)
    except (OSError, TypeError, ValueError) as e:
        logger.warning("[series-memory] save failed series=%s date=%s: %s",
                       digest.get("series") if isinstance(digest, dict) else "?", date, e)
        return None
    logger.info("[series-memory] saved digest series=%s date=%s participants=%d themes=%d points=%d",
                digest.get("series") or "?", date,
                len(digest.get("participants") or []),
                len(digest.get("themes") or []),
                len(digest.get("key_points") or []))
    return path


def load_digest(path: Path) -> Optional[dict]:
    """Читает один файл выжимки. На сбой/мусор → None."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _date_from_memory_filename(name: str) -> Optional[str]:
    """`2026-05-27-memory.json` → `2026-05-27`. Иначе None."""
    if not name.endswith(MEMORY_FILE_SUFFIX):
        return None
    stem = name[: -len(MEMORY_FILE_SUFFIX)]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", stem):
        return stem
    return None


def list_series_digests(
    series_dir: Path,
    *,
    exclude_date: Optional[str] = None,
) -> list[dict]:
    """Все выжимки серии из `series_dir`, отсортированы по дате ВОЗРАСТАНИЮ.

    `exclude_date` — пропустить выжимку текущей встречи (чтобы не подмешивать
    её саму в собственный контекст). Битые/нечитаемые файлы пропускаем молча.
    """
    d = Path(series_dir)
    if not d.is_dir():
        return []
    items: list[tuple[str, dict]] = []
    for entry in d.iterdir():
        if not entry.is_file():
            continue
        date = _date_from_memory_filename(entry.name)
        if not date:
            continue
        if exclude_date and date == exclude_date:
            continue
        dig = load_digest(entry)
        if dig is None:
            continue
        dig.setdefault("date", date)
        items.append((date, dig))
    items.sort(key=lambda x: x[0])
    return [dig for _, dig in items]


def prune_old_digests(series_dir: Path, days: int, *, today: Optional[str] = None) -> int:
    """Удаляет выжимки старше `days` дней. `days<=0` → ничего не делаем.

    `today` (YYYY-MM-DD) — для детерминизма в тестах; в проде вычисляется сам.
    Возвращает число удалённых файлов. Дату сравниваем строкой (ISO сортируем
    лексикографически) — порог = today − days.
    """
    if days <= 0:
        return 0
    d = Path(series_dir)
    if not d.is_dir():
        return 0
    if today is None:
        from datetime import date as _date  # локальный импорт: модуль stdlib-only
        today = _date.today().isoformat()
    try:
        from datetime import date as _date, timedelta as _td
        y, m, dd = (int(x) for x in today.split("-"))
        cutoff = (_date(y, m, dd) - _td(days=days)).isoformat()
    except (ValueError, TypeError):
        return 0
    removed = 0
    for entry in d.iterdir():
        if not entry.is_file():
            continue
        date = _date_from_memory_filename(entry.name)
        if not date:
            continue
        if date < cutoff:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("[series-memory] pruned %d digest(s) older than %s in %s",
                    removed, cutoff, d.name)
    return removed


def prune_root(root: Path, days: int, *, today: Optional[str] = None) -> dict:
    """РИСК4: прунит выжимки старше `days` по ВСЕМ сериям под `root`.

    `prune_old_digests` на финализации чистит только АКТИВНУЮ серию — dormant-
    серия (встреч больше нет) свои старые выжимки иначе не теряет НИКОГДА, и срок
    хранения ПДн-производных по такой серии нарушается. Этот проход — для разовой
    / периодической операции обслуживания (утренний батч владельца): соблюсти
    retention по всему архиву. Служебные `_*`/`.`-папки пропускаем. `days<=0` →
    no-op (хранение без ограничения по времени). `today` (YYYY-MM-DD) — для тестов.

    Возвращает {"series": тронуто_серий, "digests": удалено_выжимок}.
    """
    r = Path(root)
    if days <= 0 or not r.is_dir():
        return {"series": 0, "digests": 0}
    series_touched = 0
    digests_removed = 0
    for entry in sorted(r.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        n = prune_old_digests(entry, days, today=today)
        if n:
            series_touched += 1
            digests_removed += n
    logger.info("[series-memory] prune_root: series=%d digests=%d (>%dd)",
                series_touched, digests_removed, days)
    return {"series": series_touched, "digests": digests_removed}


# ---------------------------------------------------------------------------
# 7.2 — резолвер серии (память той же серии + fallback по составу участников)
# ---------------------------------------------------------------------------
def _participant_norm(name: str) -> str:
    """Нормализует имя для матчинга: lower, схлопнутые пробелы, без эмфазы."""
    s = re.sub(r"[*_`]{1,2}", "", name or "")
    return re.sub(r"\s+", " ", s).strip().lower()


def _non_owner_keys(names: list[str]) -> set[str]:
    """Множество не-владельцев (по first-word нормализованного имени).

    Матчим по первому слову («Саргин Иванов» ↔ «Саргин») — состав на разных
    встречах может записываться полным/коротким именем.
    """
    owners = _owner_tokens()
    keys: set[str] = set()
    for n in names or []:
        norm = _participant_norm(n)
        if not norm:
            continue
        first = norm.split()[0] if norm.split() else norm
        if any(tok in norm for tok in owners):
            continue
        keys.add(first)
    return keys


def _participant_overlap(a: list[str], b: list[str]) -> float:
    """Jaccard по не-владельцам. 0.0 если у текущей встречи нет не-владельцев."""
    ka = _non_owner_keys(a)
    kb = _non_owner_keys(b)
    if not ka or not kb:
        return 0.0
    inter = ka & kb
    union = ka | kb
    return len(inter) / len(union) if union else 0.0


def _match_by_participants(
    root: Path,
    *,
    current_series_dir: Path,
    current_participants: list[str],
    current_date: Optional[str],
    depth: int,
) -> list[dict]:
    """Fallback 7.2(б): ищет выжимки встреч с похожим составом по всем сериям.

    Сканирует папки-серии под `root`, берёт их выжимки, оставляет те, чей состав
    (по не-владельцам) совпадает с текущим выше порога. Собирает все совпавшие,
    сортирует по дате, отдаёт последние `depth`. Текущую папку серии и `_*`
    служебные папки пропускаем.
    """
    r = Path(root)
    if not r.is_dir():
        return []
    cur_keys = _non_owner_keys(current_participants)
    if not cur_keys:
        return []
    try:
        cur_resolved = current_series_dir.resolve()
    except OSError:
        cur_resolved = current_series_dir
    matched: list[tuple[str, dict]] = []
    for entry in r.iterdir():
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        try:
            if entry.resolve() == cur_resolved:
                continue
        except OSError:
            pass
        for dig in list_series_digests(entry, exclude_date=current_date):
            score = _participant_overlap(current_participants, dig.get("participants") or [])
            if score >= _PARTICIPANT_MATCH_THRESHOLD:
                matched.append((dig.get("date") or "", dig))
    matched.sort(key=lambda x: x[0])
    if depth > 0:
        matched = matched[-depth:]
    return [dig for _, dig in matched]


def resolve_memory(
    series_dir: Path,
    root: Path,
    *,
    current_participants: Optional[list[str]] = None,
    current_date: Optional[str] = None,
    depth: Optional[int] = None,
) -> list[dict]:
    """7.2: выжимки последних встреч серии для контекста генерации.

    Стратегия:
      (а) ПРЯМАЯ — прошлые выжимки той же серии (та же папка). Серия стабильна
          по slug из календаря/watched.yaml → регулярная встреча (Татьяна Ср 12:00)
          матчится сразу.
      (б) FALLBACK — если прямых нет (новая/нерегулярная серия, напр. 1-на-1 с
          Саргиным в отдельной папке-одиночке) → ищем по совпадению состава
          участников среди других серий.

    Возвращает список выжимок по дате ВОЗРАСТАНИЮ (старые → новые), не больше
    `depth`. Текущую дату исключаем. Best-effort: на любой сбой → [].
    """
    if depth is None:
        depth = series_memory_depth()
    try:
        same = list_series_digests(series_dir, exclude_date=current_date)
    except Exception as e:  # noqa: BLE001
        logger.warning("[series-memory] list same-series failed (non-fatal): %s", e)
        same = []
    if same:
        return same[-depth:] if depth > 0 else same
    # Прямой памяти нет — пробуем по составу участников.
    try:
        return _match_by_participants(
            root,
            current_series_dir=Path(series_dir),
            current_participants=current_participants or [],
            current_date=current_date,
            depth=depth,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[series-memory] participant-fallback failed (non-fatal): %s", e)
        return []


# ---------------------------------------------------------------------------
# Ф4б — якорь авторства серии (cluster→имя) для диаризации (REQ 1.2)
# ---------------------------------------------------------------------------
def resolve_speaker_anchor(digests: list[dict]) -> dict[str, str]:
    """Ф4б: последнее непустое `speaker_mapping` среди выжимок серии.

    `digests` — как из `resolve_memory` (по дате ВОЗРАСТАНИЮ). Берём самое свежее
    непустое сопоставление cluster→имя (последняя правка авторства бьёт ранние).
    Ленивое поле: нет ключа в старых файлах → {} (бэкфилл/миграция не нужны, УПУ3).

    NB (РИСК-диаризация): метки S1/S2 нестабильны между джобами, поэтому якорь —
    паллиатив; в `map_all` он применяется с валидацией строгим vocative текущей
    встречи (см. `name_mapping._apply_series_anchor`), чтобы не вернуть инверсию.
    """
    anchor: dict[str, str] = {}
    for dig in digests or []:
        sm = dig.get("speaker_mapping") if isinstance(dig, dict) else None
        norm = _norm_speaker_mapping(sm)
        if norm:
            anchor = norm  # ascending → последняя непустая выжимка побеждает
    return anchor


def update_digest_speaker_mapping(
    series_dir: Path, date: str, remap: dict[str, str]
) -> bool:
    """Ф4б (REQ 1.2): применяет name-remap к `speaker_mapping` выжимки встречи.

    Зовётся из перевыпуска (`feedback_reissue`) при правке авторства реплаем —
    чтобы память серии понесла ИСПРАВЛЕННОЕ авторство вперёд. `remap` —
    `{старое_имя_или_метка: новое_имя}` (тот же, что применён к транскрипту);
    применяем к ЗНАЧЕНИЯМ (именам) хранимого cluster→имя. Своп-безопасно:
    каждое значение мапится независимо через `remap.get(v, v)`.

    Ленивое/идемпотентно: нет файла выжимки / нет ключа `speaker_mapping` /
    remap ничего не меняет → no-op, возвращает False. Бэкфилл не нужен.
    """
    if not remap or not date:
        return False
    path = digest_path(series_dir, date)
    dig = load_digest(path)
    if not dig:
        return False
    sm = _norm_speaker_mapping(dig.get("speaker_mapping"))
    if not sm:
        return False
    new_sm = {c: remap.get(n, n) for c, n in sm.items()}
    if new_sm == sm:
        return False
    dig["speaker_mapping"] = new_sm
    saved = save_digest(series_dir, date, dig)
    if saved:
        logger.info("[series-memory] speaker_mapping remapped series=%s date=%s", dig.get("series") or "?", date)
    return bool(saved)


# ---------------------------------------------------------------------------
# 7-связка — постоянный состав из памяти (обогащение expected_participants)
# ---------------------------------------------------------------------------
def permanent_participants(
    digests: list[dict],
    *,
    min_occurrences: int = 2,
) -> list[str]:
    """Имена, встречающиеся в ≥`min_occurrences` выжимках — постоянный состав.

    Усиливает 5.4 (авто-подстановка известного) и 6.1 (подсказка числа спикеров):
    «известный участник» = people.md ∪ постоянный состав серии. Владельца НЕ
    включаем (он и так в expected). Сохраняем наиболее частое написание имени.
    """
    if not digests:
        return []
    owners = _owner_tokens()
    # key (first-word) → {full_name: count}, и общий счётчик по key.
    variants: dict[str, dict[str, int]] = {}
    key_count: dict[str, int] = {}
    for dig in digests:
        seen_keys: set[str] = set()
        for n in dig.get("participants") or []:
            norm = _participant_norm(n)
            if not norm or any(tok in norm for tok in owners):
                continue
            key = norm.split()[0] if norm.split() else norm
            variants.setdefault(key, {})
            variants[key][n.strip()] = variants[key].get(n.strip(), 0) + 1
            if key not in seen_keys:
                key_count[key] = key_count.get(key, 0) + 1
                seen_keys.add(key)
    out: list[str] = []
    for key, cnt in key_count.items():
        if cnt < min_occurrences:
            continue
        # самое частое написание (при равенстве — самое длинное = полнее).
        best = max(variants[key].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        out.append(best)
    return out


# ---------------------------------------------------------------------------
# 7.3/7.4 — справочный блок для промта генерации
# ---------------------------------------------------------------------------
_MEMORY_BLOCK_HEADER = (
    "СПРАВКА — прошлые встречи этой серии (это НЕ источник фактов для протокола!).\n"
    "Ниже краткие выжимки последних встреч серии. Используй их СТРОГО для:\n"
    "- правильного распознавания ИМЁН постоянных участников и устойчивых "
    "ТЕРМИНОВ/проектов;\n"
    "- понимания ДИНАМИКИ чисел (если в текущей записи число другое — это норма, "
    "бери ТЕКУЩЕЕ).\n"
    "ЗАПРЕЩЕНО переносить в протокол темы, решения, задачи или числа, которых НЕТ "
    "в текущем транскрипте. Все факты протокола — только из текущей записи. Если "
    "темы из прошлой встречи в текущей не было — её в протоколе быть НЕ должно."
)


def format_memory_block(digests: list[dict]) -> str:
    """7.3/7.4: справочный блок «прошлые встречи серии» с дисциплиной «справка,
    не факт». Пустой список → "" (блок не добавляется).
    """
    if not digests:
        return ""
    lines = [_MEMORY_BLOCK_HEADER, ""]
    for dig in digests:
        date = dig.get("date") or "—"
        lines.append(f"— Встреча {date}:")
        participants = dig.get("participants") or []
        if participants:
            lines.append(f"  Участники: {', '.join(participants)}")
        themes = dig.get("themes") or []
        if themes:
            lines.append(f"  Темы: {'; '.join(themes)}")
        key_points = dig.get("key_points") or []
        for kp in key_points:
            lines.append(f"  • {kp}")
        lines.append("")
    lines.append("(конец справки — ниже текущая встреча, факты только из неё)")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 7.5 — разовый бэкфилл выжимок из уже лежащих протоколов
# ---------------------------------------------------------------------------
def _date_from_protocol_filename(name: str) -> Optional[str]:
    """`2026-05-27-protokol.md` → `2026-05-27`. Иначе None."""
    if not name.endswith(PROTOCOL_FILE_SUFFIX):
        return None
    stem = name[: -len(PROTOCOL_FILE_SUFFIX)]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", stem):
        return stem
    return None


def backfill_series(
    series_dir: Path,
    *,
    overwrite: bool = False,
) -> int:
    """7.5: собирает выжимки из готовых протоколов `<date>-protokol.md` серии.

    Для каждого протокола без `<date>-memory.json` (или с `overwrite=True`)
    строит выжимку и сохраняет рядом. Детерминированно, без claude — безопасно
    гонять разово. `series` и `participants` берём из шапки протокола (meta.json
    рядом не парсим — шапка протокола уже несёт состав).

    Возвращает число записанных выжимок.
    """
    d = Path(series_dir)
    if not d.is_dir():
        return 0
    series_slug = d.name
    written = 0
    for entry in sorted(d.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        date = _date_from_protocol_filename(entry.name)
        if not date:
            continue
        mem_path = digest_path(d, date)
        if mem_path.exists() and not overwrite:
            continue
        try:
            protocol_text = entry.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("[series-memory] backfill read failed %s: %s", entry, e)
            continue
        if not protocol_text.strip():
            continue
        meta = {
            "series": series_slug,
            "date": date,
            # participants возьмёт из шапки протокола (meta пуст по составу).
        }
        digest = build_digest(protocol_text, meta, date=date)
        if save_digest(d, date, digest):
            written += 1
    if written:
        logger.info("[series-memory] backfill series=%s wrote=%d", series_slug, written)
    return written


def backfill_root(root: Path, *, overwrite: bool = False) -> dict:
    """7.5: бэкфилл по всем сериям под `root`. Служебные `_*`/`.`-папки пропускаем.

    Возвращает {"series": N, "digests": M} — сколько серий тронуто и выжимок
    записано. РЕАЛЬНЫЙ прогон по живым протоколам — операция утреннего деплоя
    (ПДн): ночью только код+unit на синтетике.
    """
    r = Path(root)
    if not r.is_dir():
        return {"series": 0, "digests": 0}
    series_touched = 0
    digests_written = 0
    for entry in sorted(r.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        n = backfill_series(entry, overwrite=overwrite)
        if n:
            series_touched += 1
            digests_written += n
    logger.info("[series-memory] backfill_root: series=%d digests=%d", series_touched, digests_written)
    return {"series": series_touched, "digests": digests_written}
