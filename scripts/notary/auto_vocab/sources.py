"""Слой 1 — тихая авто-синхронизация vocab из `~/Projects/me/*.md`.

Раз в день (systemd-таймер 04:00 МСК, см. `systemd/meeting-notary-vocab-sources.*`)
читает источники владельца, извлекает доменные термины (имена, бренды, проекты)
и дописывает НОВЫЕ в `speechmatics-vocab.json`. Дедуп по `content`, ручные
добавки Ф2 и служебные ключи не трогаются (см. `vocab_io`).

Источники и где лежат:
  - `people.md`         — команда/контрагенты (имена/фамилии).
  - `companies/anzhee.md` — бренды/продукты Anzhee.
  - `projects.md`       — внутренние проекты (Dealer 360, Meeting-notary, ...).
  - `ideas.md`          — упоминания фич/систем (минимально, высокий шум).

Где источники физически:
  на маке — `~/Projects/me/`; на VPS — зеркало `~/Projects/me/` (mirror'ится с
  мака, см. `.env.notary.example` про me-mirror и methods-push). Путь
  переопределяется env `MEETING_NOTARY_ME_DIR`. Если каталога/файла нет —
  источник тихо пропускается (не падаем, не зануляем vocab).

Безопасность извлечения (осознанно консервативно — мусор в vocab вреднее, чем
пропущенный термин):
  - источники дают записи БЕЗ `sounds_like` (content-only) — они только
    смещают распознавание к нужному написанию, НЕ перемапливают чужие слова
    (риск over-correction из ноты Ф2);
  - из прозы берём только латинские бренды (ALL-CAPS / CamelCase) — в русском
    тексте латиница почти всегда термин/бренд;
  - кириллические термины берём только из **жирных** спанов (`**...**`), иначе
    начала предложений дают шквал ложных кандидатов;
  - стоп-лист отсекает секции-заголовки и dev-мусор (README, GitHub, ...).

CLI:
  python -m notary.auto_vocab.sources [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from notary.auto_vocab import state, vocab_io

logger = logging.getLogger(__name__)

DEFAULT_ME_DIR = "~/Projects/me"

# Относительные пути источников внутри ME_DIR.
_SOURCE_FILES = ("people.md", "companies/anzhee.md", "projects.md", "ideas.md")

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADER_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_QUOTED_RE = re.compile(r"[«\"]([^«»\"]{2,40})[»\"]")

# Латинские бренды из прозы: ALL-CAPS (YME, OZON) и CamelCase (ProLights).
_ALLCAPS_LAT_RE = re.compile(r"\b([A-Z]{3,})\b")
_CAMEL_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-zA-Z]+)+)\b")

# Слова, которые НЕ берём (заголовки секций, dev-инфраструктура, аббревиатуры,
# фрагменты путей/англ. прозы). Сравнение по lower().
_STOPLIST = {
    # dev / инфраструктура / общие англ. слова из прозы и путей
    "readme", "claude", "index", "github", "http", "https", "url", "json",
    "vps", "api", "oem", "odm", "seo", "mvp", "html", "utc", "msk", "ndс",
    "wechat", "launchagent", "telegram", "google", "calendar", "apache",
    "vexa", "hetzner", "docker", "md", "projects", "live", "power", "moy",
    "math", "kids", "leva", "notary", "meeting", "html", "fzco", "uae",
    "gcc", "ckp", "ooo", "too", "проект", "проекты", "live",
    # юр./общие аббревиатуры (кириллица)
    "ооо", "тоо", "цкп", "ндс", "ип", "фот", "вэд", "hr",
    # секции / служебные слова заголовков
    "anzhee", "мпервый",  # уже в vocab Ф2/Ф4 — дедуп отсечёт, но не шумим
    "команда", "бренды", "цель", "триггер", "модель", "стадия", "динамика",
    "финмодель", "зависимости", "риски", "процессы", "инциденты", "приоритеты",
    "базовое", "что", "правило", "статус", "главное", "события", "партнёры",
    "поставщики", "локализация", "идеи", "inbox", "парковка", "новые",
    "путь", "кого", "когда", "правила", "китайские", "фабрики", "внешние",
    "эксперты", "консультанты", "бизнес", "детей", "демоны", "инструменты",
}

# Месяцы — чтобы не утянуть из дат «октября», «декабря».
_MONTHS = {
    "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
    "сентябрь", "октябрь", "ноябрь", "декабрь",
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
    "сентября", "октября", "ноября", "декабря",
}

# Моноскриптовый токен имени: либо весь кириллица, либо вся латиница, с
# опциональным внутренним дефисом (Абрамова-Михина). Смешанные («OEM-фабрики») —
# не имена.
_NAME_CYR_RE = re.compile(r"^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ]?[а-яё]+)?$")
_NAME_LAT_RE = re.compile(r"^[A-Z][a-zA-Z]+(?:-[A-Za-z]+)?$")
# «МПервый» — заглавная, ВНУТРЕННЯЯ заглавная, затем строчные. «МСК»/«Китайские»
# сюда НЕ попадают (нет внутренней заглавной + хвоста строчных одновременно).
_CYR_MIXED_RE = re.compile(r"^[А-ЯЁ][а-яё]*[А-ЯЁ][а-яё]+$")
_CAP_LAT_RE = re.compile(r"^[A-Z][a-zA-Z]{2,}$")
_CAP_CYR_RE = re.compile(r"^[А-ЯЁ][а-яё]{2,}$")


def _is_cyr(tok: str) -> bool:
    return bool(re.fullmatch(r"[А-ЯЁа-яё-]+", tok))


def me_dir() -> Path:
    return Path(os.environ.get("MEETING_NOTARY_ME_DIR", DEFAULT_ME_DIR)).expanduser()


def _stopped(token: str) -> bool:
    low = token.strip().lower().strip(".,:;!?()[]")
    return low in _STOPLIST or low in _MONTHS or len(low) < 3


def _looks_like_name(span: str) -> bool:
    """Жирный спан — это ФИО? 1-3 моноскриптовых токена, без цифр/хвостов.

    Каждый токен — либо весь кириллица, либо вся латиница (смешанные
    «OEM-фабрики» отсекаются). Это режет ложные «Китайские OEM-фабрики».
    """
    span = span.strip()
    if any(ch.isdigit() for ch in span):
        return False
    tokens = span.split()
    if not (1 <= len(tokens) <= 3):
        return False
    return all(_NAME_CYR_RE.match(t) or _NAME_LAT_RE.match(t) for t in tokens)


def _term_tokens(span: str) -> list[str]:
    """Извлечь термин-токены из жирного спана (бренды/проекты, не ФИО)."""
    out: list[str] = []
    for raw in re.split(r"[\s/(),]+", span.strip()):
        tok = raw.strip().strip(".,:;!?«»\"'")
        if not tok or _stopped(tok):
            continue
        if (
            _CAP_LAT_RE.match(tok)
            or _CYR_MIXED_RE.match(tok)
            or _ALLCAPS_LAT_RE.fullmatch(tok)
            or _CAMEL_RE.fullmatch(tok)
        ):
            out.append(tok)
    return out


def _candidates_from_bold(text: str) -> list[str]:
    out: list[str] = []
    for m in _BOLD_RE.finditer(text):
        span = m.group(1).strip()
        if _looks_like_name(span):
            out.append(span)  # полное «Имя Фамилия» / название проекта
            # Сабтокены — ТОЛЬКО кириллица (имя/фамилия). Латинские названия
            # («Math-kids», «Meeting-notary») оставляем цельными, не дробим.
            if len(span.split()) >= 2:
                for tok in span.replace("-", " ").split():
                    if len(tok) >= 3 and _is_cyr(tok) and not _stopped(tok):
                        out.append(tok)
        else:
            out.extend(_term_tokens(span))
    return out


def _candidates_from_prose_latin(text: str) -> list[str]:
    out: list[str] = []
    for rx in (_ALLCAPS_LAT_RE, _CAMEL_RE):
        for m in rx.finditer(text):
            tok = m.group(1)
            if not _stopped(tok):
                out.append(tok)
    return out


def _candidates_from_headers(text: str) -> list[str]:
    """ideas.md и пр.: из заголовков только латинские бренды и короткие
    «кавычки» — НЕ целые длинные названия (они — плохой vocab)."""
    out: list[str] = []
    for m in _HEADER_RE.finditer(text):
        head = m.group(1)
        out.extend(_candidates_from_prose_latin(head))
        for q in _QUOTED_RE.finditer(head):
            phrase = q.group(1).strip()
            if 1 <= len(phrase.split()) <= 3:
                for tok in phrase.split():
                    if (_CAP_LAT_RE.match(tok) or _CAP_CYR_RE.match(tok) or _CYR_MIXED_RE.match(tok)) and not _stopped(tok):
                        out.append(tok)
    return out


def _parse_people(text: str) -> list[str]:
    # Только жирные ФИО. Латиница из прозы (Ken, OEM, WeChat) — шумная, не берём;
    # значимые латинские бренды живут в companies/anzhee.md.
    return _candidates_from_bold(text)


def _parse_company(text: str) -> list[str]:
    # Файл брендов — единственный, где проза-латиница оправдана (ALTRONIX,
    # ENVONIX, ProLights, YME).
    return _candidates_from_bold(text) + _candidates_from_prose_latin(text)


def _parse_projects(text: str) -> list[str]:
    # Только жирные названия проектов; проза проектов — пути/англ.слова (шум).
    return _candidates_from_bold(text)


def _parse_ideas(text: str) -> list[str]:
    # ideas.md шумный — только заголовки (латиница + короткие кавычки).
    return _candidates_from_headers(text)


_DISPATCH = {
    "people.md": _parse_people,
    "companies/anzhee.md": _parse_company,
    "projects.md": _parse_projects,
    "ideas.md": _parse_ideas,
}


def collect_candidates(root: Path | None = None) -> dict[str, list[str]]:
    """Прочитать все источники → {rel_path: [уникальные content-токены]}.

    Отсутствующий файл/каталог → пустой список для него (не падаем).
    """
    base = root or me_dir()
    result: dict[str, list[str]] = {}
    for rel, parser in _DISPATCH.items():
        path = base / rel
        if not path.is_file():
            logger.info("источник не найден, пропуск: %s", path)
            result[rel] = []
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("не прочитать источник %s: %s", path, e)
            result[rel] = []
            continue
        # стабильная дедупликация с сохранением порядка появления
        seen: set[str] = set()
        uniq: list[str] = []
        for c in parser(text):
            c = c.strip()
            k = c.lower()
            if c and k not in seen:
                seen.add(k)
                uniq.append(c)
        result[rel] = uniq
    return result


def _append_change_log(added: list[str], *, dry_run: bool) -> None:
    if not added:
        return
    log_path = state.state_path().parent / "auto_vocab_changes.log"
    line = (
        f"{datetime.now().isoformat(timespec='seconds')} sources "
        f"+{len(added)}: {', '.join(added)}\n"
    )
    if dry_run:
        logger.info("[dry-run] change-log не пишу; было бы: %s", line.strip())
        return
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.warning("change-log write failed (%s) — не критично", e)


def sync(*, dry_run: bool = False, root: Path | None = None) -> dict:
    """Главная функция таймера: собрать кандидатов, домержить новые, записать.

    Возвращает summary: {per_source, total_candidates, added:[...], skipped_existing}.
    Если базовый vocab не читается (битый/нет) — НИЧЕГО не пишем (защита Ф2).
    """
    per_source = collect_candidates(root)
    # уплощаем с сохранением порядка и без повторов между источниками
    seen: set[str] = set()
    flat: list[dict] = []
    for terms in per_source.values():
        for t in terms:
            k = t.lower()
            if k not in seen:
                seen.add(k)
                flat.append({"content": t})

    # Дедуп против ОБОИХ файлов (главный + авто) — чтобы не добавлять то, что уже
    # есть где-либо. Пишем ТОЛЬКО в авто-файл (вариант «б»): главный остаётся
    # под ручным контролем + `make deploy-vocab`.
    try:
        existing = vocab_io.combined_existing_keys()
    except Exception as e:  # noqa: BLE001
        logger.error("vocab не читается (%s: %s) — пополнение из источников отменено", type(e).__name__, e)
        return {"error": str(e), "per_source": {k: len(v) for k, v in per_source.items()}, "added": []}

    before = len(existing)
    new_terms = [c for c in flat if vocab_io.normalize(c["content"]) not in existing]
    added_terms = [c["content"] for c in new_terms]

    summary = {
        "per_source": {k: len(v) for k, v in per_source.items()},
        "total_candidates": len(flat),
        "added": added_terms,
        "added_count": len(added_terms),
        "vocab_before": before,
        "vocab_after": before + len(added_terms),
        "dry_run": dry_run,
    }

    if not added_terms:
        logger.info("источники: новых терминов нет (кандидатов %d, в vocab %d)", len(flat), before)
        return summary

    if dry_run:
        logger.info("[dry-run] добавил бы %d: %s", len(added_terms), ", ".join(added_terms))
        return summary

    # add_to_auto сам под flock дедупит против main+auto (на случай гонки с
    # applier'ом между нашей проверкой выше и записью) — added отражает факт.
    really_added = vocab_io.add_to_auto(new_terms)
    added_terms = [e["content"] for e in really_added]
    summary["added"] = added_terms
    summary["added_count"] = len(added_terms)
    summary["vocab_after"] = before + len(added_terms)
    if not added_terms:
        logger.info("источники: гонка — всё уже добавлено параллельно, 0 новых")
        return summary
    _append_change_log(added_terms, dry_run=False)
    try:
        state.bump_weekly("sources_added", len(added_terms))
    except Exception as e:  # noqa: BLE001
        logger.warning("weekly_stats.sources_added не обновлён (%s) — не критично", e)
    logger.info("источники: +%d терминов в vocab (%d→%d): %s",
                len(added_terms), before, summary["vocab_after"], ", ".join(added_terms))
    return summary


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [auto_vocab.sources] %(message)s",
    )
    ap = argparse.ArgumentParser(description="Пополнить Speechmatics vocab из ~/Projects/me/*")
    ap.add_argument("--dry-run", action="store_true", help="показать кандидатов, ничего не писать")
    ap.add_argument("--me-dir", default=None, help="корень источников (по умолчанию $MEETING_NOTARY_ME_DIR или ~/Projects/me)")
    args = ap.parse_args(argv)
    root = Path(args.me_dir).expanduser() if args.me_dir else None
    summary = sync(dry_run=args.dry_run, root=root)
    if summary.get("error"):
        print(f"ОШИБКА: {summary['error']}", file=sys.stderr)
        return 1
    print(
        f"источников: {summary['per_source']}; кандидатов {summary['total_candidates']}; "
        f"добавлено {summary['added_count']} (vocab {summary['vocab_before']}→{summary['vocab_after']})"
    )
    if summary["added"]:
        print("новые: " + ", ".join(summary["added"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
