"""Ф5: единый загрузчик YAML-знания компании из VPS-клона `*-context`.

Реализует ИНТЕРФЕЙС, зафиксированный контрактом Ф4 (`docs/notary-context-contract.md`,
§1–§2): бот читает производное ЗНАНИЕ (глоссарий + оргструктуру) из выделенного
поддерева `knowledge/notary/` репозитория контекста нужной компании.

Топология (контракт §3.1):

    <context-root>/<repo>/knowledge/notary/glossary.yaml
    <context-root>/<repo>/knowledge/notary/org-structure.yaml

  - VPS:  context-root = /srv/meeting-notary/context (env MEETING_NOTARY_CONTEXT_DIR,
          задаётся в .env.notary); repo = anzhee-context | mpfirst-context.
  - мак:  context-root = ~/Projects (клоны `anzhee-context`/`mpfirst-context` уже там).
  Env `MEETING_NOTARY_CONTEXT_DIR` переопределяет корень (тесты указывают на фикстуру).

Дисциплина загрузчика (как `auto_vocab/sources.py`, контракт §2): нет клона / нет
файла / нет pyyaml / битый YAML → **пустой результат**, НЕ падаем, НЕ зануляем.
Поведение «как до Ф5» (fallback на встроенные источники) обеспечивают вызыватели
(`glossary.py`, `series_roster.py`) — этот модуль лишь возвращает пусто.

Lazy `import yaml` ВНУТРИ функции: системный python3 юнит-тестов pyyaml не несёт
(контракт §1.2), а прод-venv несёт. Чистые проекции (`glossary.py`) и фильтр
company-scope тестируются на вход-словаре без YAML.

Company-scope (контракт §1.5): для встречи компании X читаются записи `scope == X`
И `scope == cross`. Кросс-термин дублируется в ОБА репо — рассинхрон ловит
`lint_cross_sync()`. Карта `company → repo` живёт здесь (не в `*-context` — иначе
курица-яйцо), переопределяема env.

Опасная тройка (CLAUDE.md проекта): знание — производные термины/роли/доменная
лексика, НЕ сырьё реплик. Тексты транскриптов сюда не попадают и не логируются.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple, Optional, TypedDict

logger = logging.getLogger(__name__)

# Корень, где лежат клоны репозиториев контекста. На VPS — задаётся env из
# .env.notary; на маке дефолт — ~/Projects (там клоны уже есть). Совпадает с
# логикой `auto_vocab/sources.py::me_dir` (env-override + домашний дефолт).
DEFAULT_CONTEXT_ROOT = "~/Projects"

# Поддерево бот-знания внутри репо контекста (контракт §1.1).
KNOWLEDGE_SUBPATH = "knowledge/notary"
GLOSSARY_FILE = "glossary.yaml"
ORG_STRUCTURE_FILE = "org-structure.yaml"

# --- R6 (Ф2): БЕЛЫЙ СПИСОК knowledge/ в контекст сборки протокола --------------
#
# 🔒 ПРИВАТНОСТЬ (РИСК4/ВОПР2, подтверждено владельцем 2026-06-24). В контекст
# сборки протокола встречи уезжает во ВНЕШНИЙ LLM (Anthropic). Поэтому сюда
# подаётся ЯВНЫЙ allow-list файлов knowledge/ — НЕ deny-list, НЕ сырой дамп всего
# каталога знаний. Всё, чего НЕТ в `PROTOCOL_CONTEXT_WHITELIST`, в промпт НЕ
# попадает (fail-closed): новый файл знания по умолчанию НЕвидим боту, пока его
# СОЗНАТЕЛЬНО не впишут сюда. Сомневаешься, можно ли файл — НЕ вписывай.
#
# glossary.yaml / org-structure.yaml в этот список НЕ входят: их читают СТРУКТУРНО
# (`load_glossary` / `load_org_structure`) своими загрузчиками — они уже whitelisted
# самим фактом наличия отдельного загрузчика. Здесь — только СЫРЫЕ markdown-файлы
# знания (каталог товаров), которые подаются в промпт текстом as-is.
#
# Пути — относительно КОРНЯ репо контекста (`<root>/<repo>/...`), не `knowledge/notary`.
PROTOCOL_CONTEXT_WHITELIST: tuple[str, ...] = (
    # Каталог товаров МПервый (ENVONIX/ALTRONIX): реальные названия/артикулы, чтобы
    # бот подставлял канон, а не фонетику (ISS-21 Gap-2). Для anzhee файла нет —
    # graceful: его секция каталога просто не рендерится.
    "knowledge/product-catalog-wb.md",
)

# Денилист-БЭКСТОП (defense-in-depth, НЕ основной фильтр — фильтр это allow-list
# выше). Файлы, которые НИКОГДА не должны попасть в контекст бота: финансовые
# карты, drive-карты (внутренняя навигация), экономика/финмодель/HR-конфиденшел.
# Если кто-то по ошибке впишет такой файл в whitelist (или в манифест Ф2) —
# `load_whitelisted_context` его всё равно отбросит, а `assert_whitelist_safe()`
# (вызывается в тесте) упадёт. Сверяем по basename — денилист ловит файл, даже
# если его ПЕРЕНЕСЛИ из `restricted/` в обычную папку.
PROTOCOL_CONTEXT_DENYLIST: frozenset[str] = frozenset({
    "accounting-tables-registry.md",      # реестр таблиц учёта (финансы)
    "payment-planning-tables-map.md",     # карта платёжного планирования (финансы)
    "google-drive-map.md",                # карта Google Drive (внутренняя навигация)
    # Ф1 плана watson-company-memory: чувствительное МПервый. Сегодня эти файлы
    # лежат в `restricted/` (их ловит и запрет по пути ниже), но денилист по имени
    # переживает переезд файла в обычную папку — это и есть defense-in-depth.
    "altronix-ekonomika.md",              # экономика линейки (финансы)
    "finmodel-orgstruktura.md",           # финмодель оргструктуры (финансы + HR)
    "people-confidential.md",             # HR-конфиденшел (люди, зарплаты, оценки)
    "marketplace-affiliation-safety.md",  # аффилированность на маркетплейсах (юр. риск)
    # Ф2 (цикл из 5 ходов). Про эти три файла ПРОВЕРЕНО, что внутри чувствительное —
    # и при этом план велел вписать их в бриф (`plans/2026-07-12-…:152`). Из манифеста
    # их исключили, но в коде не закрывали: файл держался снаружи только тем, что строки
    # нет в текстовом файле чужого репо, который правит команда без ревью кода. Это
    # противоречит R9/A3 («бэкстоп живёт в КОДЕ и правкой манифеста не обходится»):
    # достаточно одной строки — и наружу уезжает то, что уезжать не должно никогда.
    "o-kompanii-mpervyi.md",  # МПервый: чистая прибыль, маржа, ROIC, налоговый план
    "vision.md",              # Anzhee: антимонопольная механика + указания по формулировкам
    "dry-rental.md",          # Anzhee: дилерские цены, маржа прокатчика, ссылка на закрытый прайс
})

# Запрет по ПУТИ: имя директории, содержимое которой не читается НИКОГДА, в любом
# месте дерева репо (`restricted/x.md`, `company/restricted/y/z.md` — всё). Это
# ПЕРВИЧНЫЙ барьер для чувствительного: во втором мозге МПервый вся экономика/
# финмодель/HR лежит именно там, и новый файл, положенный туда командой, закрыт
# автоматически — без правки этого кода.
PROTOCOL_CONTEXT_RESTRICTED_DIRS: frozenset[str] = frozenset({"restricted"})

# Чувствительные ПАТТЕРНЫ имени (fnmatch, case-insensitive; сверяются с КАЖДЫМ
# сегментом относительного пути, поэтому папка `finmodel/` закрыта так же, как
# файл `x-finmodel.md`).
#
# ⚠️ ЭТО ВТОРИЧНАЯ СТРАХОВКА, НЕ БАРЬЕР (РИСК1 плана). Реальные финфайлы МПервый в
# `company/baza-znaniy/` названы транслитом БЕЗ этих корней — `924-uchet-dds.md`,
# `zifriend-vzaimoraschety-itog.md`, `867-nalichnye-kassy-…md`,
# `925-…-sebestoimosti-postavki.md`, `kak-uchityvat-kursy-valyut-…md` — и НИ ОДИН
# паттерн их не ловит (зафиксировано тестом `test_translit_fin_names_slip_patterns`).
# Отсюда закон Ф2: `baza-znaniy/` подключается ТОЛЬКО явным short-list'ом конкретных
# файлов, НИКОГДА glob'ом папки. Паттерны ловят лишь «честно названное» чувствительное.
PROTOCOL_CONTEXT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "*ekonomik*",     # план просил `*ekonomika*`; берём корень — ловит и ekonomiki/ekonomike
    "*economic*",     # план просил `*economics*`; корень ловит economics/economical
    "*finmodel*",
    "*confidential*",
    "*konfiden*",     # транслит «конфиденциально»
)

# Защитный потолок размера ОДНОГО файла знания (рантайм-страховка от того, что файл
# когда-нибудь распухнет и раздует промпт). Текущий каталог ~17 КБ. Превышение НЕ
# молча режется — логируется WARNING (принцип «no silent caps»).
PROTOCOL_CONTEXT_FILE_MAX_BYTES = 131072  # 128 КБ

# Бюджет СУММАРНОГО размера знания, уходящего в промпт (Ф1). Пофайлового потолка
# мало: с Ф2 файлов станет много (бриф компании), и раздувание промпта — это и
# деньги (бриф уходит в ДВА Opus-вызова), и дилюция инструкций. Ориентир плана —
# 40–60 КБ; берём 50 КБ: у МПервый база уже 31.5 КБ (o-kompanii 14.5 + каталог 17),
# у Anzhee один `vision.md` = 18.5 КБ. Превышение = WARNING + честная обрезка ПО
# ГРАНИЦЕ ФАЙЛОВ (не молча, не в середине файла) — см. `apply_total_budget`.
PROTOCOL_CONTEXT_TOTAL_MAX_BYTES = 51200  # 50 КБ

# Карта компания → имя репозитория контекста (контракт §1.5). Дефолт; каждую
# запись можно переопределить env `NOTARY_CONTEXT_REPO_<COMPANY>` (uppercase).
_DEFAULT_REPO_MAP = {
    "anzhee": "anzhee-context",
    "mpfirst": "mpfirst-context",
}

# Карта компания → GitHub-владелец репо контекста (Ф8). Репо двух компаний лежат
# под РАЗНЫМИ владельцами: anzhee-context — в орге `anzhee-dev`, mpfirst-context —
# в личном аккаунте `rybalkaie`. Поэтому full_repo = `<owner>/<repo>`, а git-токен
# выдаётся per-company (fine-grained PAT не покрывает двух владельцев одним токеном).
# Значения совпадают с Makefile (ANZHEE_CONTEXT_REPO/MPFIRST_CONTEXT_REPO) — там
# источник для read-bootstrap, здесь — для write-back PR. Переопределяемо env
# `NOTARY_CONTEXT_OWNER_<COMPANY>`.
_DEFAULT_OWNER_MAP = {
    "anzhee": "anzhee-dev",
    "mpfirst": "rybalkaie",
}

# Допустимые значения поля scope в glossary.yaml.
SCOPE_CROSS = "cross"


class GlossaryEntry(TypedDict, total=False):
    """Одна запись `glossary.yaml` (контракт §1.3). total=False — note/aliases опц."""

    canonical: str
    scope: str
    note: str
    aliases: list[str]
    asr_sounds_like: bool
    protocol_regex: bool


# Справочник людей компании (R18, план 2026-06-16): статусы участия + канон имени
# + алиасы. Company-level (как rosters), читается из ТОГО ЖЕ org-structure.yaml в
# верхнеуровневой секции `people:`. Нужен, чтобы:
#   • не подставлять автором того, кто больше не ходит на встречи (status=inactive
#     → выпадает из name_pool LLM-добивки и из ожидаемых; A5/R4/R5);
#   • знать каноничное написание имени (будущий замок исправлений Ф2 — R6/R10).
PERSON_STATUS_ACTIVE = "active"
PERSON_STATUS_INACTIVE = "inactive"


class PersonEntry(TypedDict, total=False):
    """Одна запись `people:` (R18). `status`/`aliases` опциональны: запись без
    status трактуется как активный участник (бэкомпат со старыми файлами)."""

    name: str
    status: str
    aliases: list[str]


# --- Резолв путей -------------------------------------------------------------


def context_root() -> Path:
    """Корень клонов `*-context`. Env `MEETING_NOTARY_CONTEXT_DIR` → дефолт ~/Projects."""
    return Path(
        os.environ.get("MEETING_NOTARY_CONTEXT_DIR", DEFAULT_CONTEXT_ROOT)
    ).expanduser()


def known_companies() -> list[str]:
    """Список компаний, для которых известен репозиторий контекста."""
    return list(_repo_map().keys())


def _repo_map() -> dict[str, str]:
    """company → repo-name с env-переопределением (`NOTARY_CONTEXT_REPO_ANZHEE=...`)."""
    out = dict(_DEFAULT_REPO_MAP)
    for company in _DEFAULT_REPO_MAP:
        override = os.environ.get(f"NOTARY_CONTEXT_REPO_{company.upper()}")
        if override and override.strip():
            out[company] = override.strip()
    return out


def repo_for_company(company: Optional[str]) -> Optional[str]:
    """Имя репо контекста для компании. Неизвестная/пустая компания → None."""
    if not company or not str(company).strip():
        return None
    return _repo_map().get(str(company).strip().lower())


def _owner_map() -> dict[str, str]:
    """company → github-owner с env-переопределением (`NOTARY_CONTEXT_OWNER_ANZHEE=...`)."""
    out = dict(_DEFAULT_OWNER_MAP)
    for company in _DEFAULT_OWNER_MAP:
        override = os.environ.get(f"NOTARY_CONTEXT_OWNER_{company.upper()}")
        if override and override.strip():
            out[company] = override.strip()
    return out


def owner_for_company(company: Optional[str]) -> Optional[str]:
    """GitHub-владелец репо контекста (орг/аккаунт). Неизвестная компания → None."""
    if not company or not str(company).strip():
        return None
    return _owner_map().get(str(company).strip().lower())


def full_repo_for_company(company: Optional[str]) -> Optional[str]:
    """`<owner>/<repo>` для компании (для `gh pr --repo` и git-remote). None, если
    неизвестен владелец ИЛИ имя репо (write-back на неизвестную компанию невозможен)."""
    owner = owner_for_company(company)
    repo = repo_for_company(company)
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def git_token_for_company(company: Optional[str], *, write: bool = False) -> Optional[str]:
    """Git-токен per-company (Ф8). fine-grained PAT привязан к одному владельцу,
    поэтому токен ищется по компании, затем — глобальный фолбэк.

    write=True (push/PR):  `NOTARY_CONTEXT_GIT_WRITE_TOKEN_<CO>` → `..._WRITE_TOKEN`.
    write=False (clone/fetch): `NOTARY_CONTEXT_GIT_TOKEN_<CO>` → `..._GIT_TOKEN`, а
      при их отсутствии — write-токен (write implies read, отдельный read-токен
      опционален). Пусто во всех слоях → None (инертно, как до провижининга)."""
    co = (str(company).strip().lower() if company and str(company).strip() else "")
    chain: list[str] = []
    if write:
        if co:
            chain.append(f"NOTARY_CONTEXT_GIT_WRITE_TOKEN_{co.upper()}")
        chain.append("NOTARY_CONTEXT_GIT_WRITE_TOKEN")
    else:
        if co:
            chain.append(f"NOTARY_CONTEXT_GIT_TOKEN_{co.upper()}")
        chain.append("NOTARY_CONTEXT_GIT_TOKEN")
        if co:
            chain.append(f"NOTARY_CONTEXT_GIT_WRITE_TOKEN_{co.upper()}")
        chain.append("NOTARY_CONTEXT_GIT_WRITE_TOKEN")
    for var in chain:
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    return None


def knowledge_dir(company: Optional[str]) -> Optional[Path]:
    """Путь к `<root>/<repo>/knowledge/notary` для компании. None, если репо неизвестно."""
    repo = repo_for_company(company)
    if not repo:
        return None
    return context_root() / repo / KNOWLEDGE_SUBPATH


def repo_root(company: Optional[str]) -> Optional[Path]:
    """Путь к корню репо контекста `<root>/<repo>` для компании. None, если репо
    неизвестно. Нужен для whitelist-файлов, которые лежат ВЫШЕ `knowledge/notary`
    (каталог товаров — в `knowledge/`, не в `knowledge/notary`)."""
    repo = repo_for_company(company)
    if not repo:
        return None
    return context_root() / repo


# --- Ф1 плана `knowledge-content-barrier`: БАРЬЕР ПО СОДЕРЖИМОМУ ---------------
#
# ЗАЧЕМ. Барьеры выше судят файл ПО ИМЕНИ. Независимый аудит доказал, что этого мало:
# финансовые файлы `company/baza-znaniy/` названы транслитом (`867-nalichnye-kassy-…`,
# `924-uchet-dds`, `925-…sebestoimosti-postavki`, `837-planirovanie-oplat-…`) и проходят
# ВСЕ проверки имени. Манифест ведёт команда без ревью кода — одна строка, и деньги
# компании уехали бы во внешнюю модель. Здесь мы смотрим ВНУТРЬ файла.
#
# ЭМПИРИКА, на которой построены пороги (замерено на боевых `*-context`, 2026-07-13):
#   • Реальные финфайлы базы знаний ЧАСТО НЕ СОДЕРЖАТ СУММ вообще. `924-uchet-dds` —
#     это заголовок + ссылка на видео; `922-…reklamnykh-integratsiy` — заголовок +
#     ссылка на закрытую таблицу. Барьер «ищем ₽» их бы НЕ поймал. Зато их безошибочно
#     выдаёт ТЕМА документа (заголовок + имя файла) — поэтому она первый и главный слой.
#   • Боевые файлы знания (каталоги, роли, позиционирование) упоминают деньги ТОЛЬКО в
#     дисклеймерах («Что здесь НЕ лежит: цены, себестоимость, маржа») и в определениях
#     терминов. Поэтому строки-отрицания из подсчёта плотности исключаются, а одиночное
#     упоминание мягкого термина не блокирует — иначе отвалился бы каталог товаров, и
#     бот снова начал бы врать в артикулах (ISS-21, ровно та боль, ради которой всё).
#
# ГРАНИЦЫ СЛОВ ОБЯЗАТЕЛЬНЫ. Каталог Anzhee полон «стРУБцин» и «тРУБок», каталог WB —
# артикулов с цифрами; «товар пРИБЫЛ на склад» — это не «прибыль». Все маркеры ниже —
# регексы с `\b` (в Python он работает с кириллицей), НЕ поиск подстроки.
#
# Опасная тройка: наружу (лог, Telegram) уходит только ТИП маркера и счётчик. Ни текст
# файла, ни найденные суммы не логируются и не попадают в причину отказа.

_SEP = r"[    ]"  # пробел, nbsp, narrow-nbsp, thin-space (разделители тысяч)

# Денежная сумма: «1 250 000 ₽», «42 000,00 руб.», «$1,200». Разделитель тысяч — только
# пробельные из `_SEP` (НЕ `\s`: перевод строки склеил бы «пункт 5» и «руб» с разных строк).
#
# ⚠️ Квантификаторы ОГРАНИЧЕНЫ ({0,24}, {0,12}), а не `*` — это защита от квадратичного
# бэктрекинга, не косметика (цикл из 5 ходов, Н3). С `*` движок на тексте из чисел и
# пробелов БЕЗ валюты в конце перебирает все разбиения хвоста: замерено 16 КБ → 11 с,
# 40 КБ → 98 с. Пофайловый потолок — 128 КБ, то есть один файл с числовой таблицей
# (остатки, размеры, прайс-выгрузка) вешал бы сборку КАЖДОГО протокола на минуты.
#
# Границы подобраны так, чтобы НЕ ослепить барьер (тот же цикл, ход 4): 24 знака покрывают
# «1 234 567 890 123,00», а 12 разделителей — выравнивание пробелами в таблицах
# («Итого:  1 250 000    ₽»). Слишком тесная граница здесь = дыра в приватности, а не
# просто «строгий регекс»: несосчитанная сумма пропускает финансовый файл в промпт.
_FIN_MONEY_RE = re.compile(
    rf"\d(?:[\d{_SEP[1:-1]}]{{0,24}}\d)?(?:[.,]\d+)?{_SEP}{{0,12}}"
    r"(?:₽|€|\$|¥|\bруб[а-яё]*\b|\bр\.|\brub\b|\busd\b|\beur\b|\bcny\b|\bюан[а-яё]*\b)",
    re.IGNORECASE,
)
_FIN_MONEY_PREFIX_RE = re.compile(rf"(?:₽|€|\$|¥){_SEP}?\d")

# ЖЁСТКИЕ термины денежного учёта: в боевых файлах знания их НОЛЬ (проверено на обоих
# `*-context`), поэтому одного вхождения вне дисклеймера достаточно для отказа.
# `\bза[йё]м\b|займ[аыуе]\b|…` — НЕ `займ\w*`: иначе «это займёт 5 минут» = финансы.
_FIN_HARD_RE = re.compile(
    r"\bддс\b"
    r"|\bвзаиморасч[её]т\w*"
    r"|\bоклад\w*|\bзарплат\w*|\bзаработн\w+" + _SEP + r"+плат\w+|\bфонд" + _SEP + r"+оплаты" + _SEP + r"+труда\b"
    r"|\bреинвестир\w*"
    r"|\bдебиторк\w*|\bдебиторск\w*|\bкредиторк\w*|\bкредиторск\w*"
    # Падежи перечислены поимённо, а не `касс\w*`/`займ\w*`: иначе «кассета» и «это
    # займёт 5 минут» стали бы финансами. Творительный («кассой», «займом») добавлен
    # циклом из 5 ходов (Н5): класс `[аыуеой]` матчит ОДИН символ и окончание «-ой»/«-ом» не ловил.
    r"|\bкасс[аыуеой]\b|\bкассой\b|\bкассам\w*|\bкассах\b|\bкассов\w*"  # «кассета» НЕ ловится (\b)
    r"|\bза[йё]м\b|\bзайм[аыуе]\b|\bзаймом\b|\bзаймов\b|\bзаймам[и]?\b|\bзаймах\b|\bссуд[аыуе]\b"
    r"|\bp&l\b|\bпи[- ]?энд[- ]?эл\b",
    re.IGNORECASE,
)

# МЯГКИЕ термины: встречаются и в легитимных файлах (дисклеймер каталога, определение
# термина в брифе, зона ответственности в «кто за что отвечает»). Блокируют только
# ПЛОТНОСТЬЮ. `\bприбыл[ьи]\b` — не `прибыл\w*`: «товар прибыл» это глагол.
_FIN_SOFT_RE = re.compile(
    r"\bмарж[аиуе]\b|\bмаржинальн\w*"
    r"|\bсебестоимост\w*"
    r"|\bнаценк\w*"
    r"|\bприбыл[ьи]\b|\bприбыльн\w*"
    r"|\bвыручк\w*"
    r"|\bоборот[аеуы]?\b|\bоборотн\w*"
    r"|\bдилерск\w*"
    r"|\bпрайс\w*"
    r"|\bррц\b|\brrc\b"
    r"|\bзакупочн\w*"
    r"|\bрентабельн\w*"
    r"|\bюнитк\w*|\bюнит[- ]эконом\w*|\bunit[- ]эконом\w*",
    re.IGNORECASE,
)

# ТЕМА документа = заголовок H1 + имя файла. Заявление «этот документ ПРО деньги» —
# сильнее любого упоминания в теле. Транслит обязателен: имена файлов базы знаний —
# `nalichnye-kassy`, `uchet-dds`, `sebestoimost`, `vzaimoraschety`, `planirovanie-oplat`.
_FIN_TOPIC_STRONG_RE = re.compile(
    r"\bддс\b|\bdds\b"
    r"|взаиморасч|vzaimorasch"
    r"|себестоимост|sebestoimost"
    r"|\bкасс[аыои]\b|\bкассам|\bкассах|nalichnye|\bkass[aeouy]"
    r"|\bза[йё]м\b|\bзайм[аыуео]|\bzaym|ссуд[аыое]"
    r"|реинвестир|reinvest"
    r"|зарплат|zarplat|\bоклад|\boklad"
    r"|\bмарж[аиуе]\b|маржинальн|marzh"
    r"|эконом|ekonomik|economic"
    r"|финмодел|finmodel|финанс|finans"
    r"|бюджет|budget|\bсмет[аыу]\b"
    r"|прайс|\bprice\b"
    r"|курс\w*" + _SEP + r"+валют|kursy-valyut|valyut"
    r"|\bприбыл[ьи]\b|pribyl"
    r"|выручк|vyruchk"
    r"|дебитор|debitor|кредитор"
    r"|наценк|natsenk"
    r"|оплат|oplat"
    r"|сверк\w*" + _SEP + r"+остатк|sverka-ostatkov"
    r"|дивиденд|divident|инвестиц|investic",
    re.IGNORECASE,
)
# Мягкая тема: «учёт», «расход», «доход», «отчёт» сами по себе бывают и не про деньги
# («учёт рабочего времени»). Блокируют только с ПОДКРЕПЛЕНИЕМ (ссылка на таблицу учёта,
# метка финдепартамента, денежный термин или сумма в теле).
_FIN_TOPIC_SOFT_RE = re.compile(
    r"уч[её]т|учитыва|\bucheta?\b|uchityvat|\buchet"
    r"|расход|rashod|доход|dohod"
    r"|закупк|zakupk",
    re.IGNORECASE,
)

# Метка базы знаний Platrum: шапка-цитата статьи несёт «раздел «Финансовый департамент»».
# Прямое заявление источника, что документ финансовый.
_FIN_PLATRUM_RE = re.compile(
    r"раздел\s*[«\"']?\s*(?:финансов\w+|бухгалтер\w+)\s+департамент", re.IGNORECASE
)
# Файл сам объявляет себя закрытым — уважаем это, даже если денег внутри нет.
_CLOSED_MARK_RE = re.compile(
    r"конфиденциальн\w*|закрыт\w+\s+слой|\bconfidential\b|не\s+для\s+распространения", re.IGNORECASE
)
# Процент рядом с финансовым термином: «Маржинальность более 20%» — это финансовый
# критерий, хотя ни одной суммы в файле нет.
_FIN_PERCENT_RE = re.compile(rf"\d+(?:[.,]\d+)?{_SEP}*%")

# ⚠️ ТОЛЬКО URL-ссылки на Google-документы — НЕ голые ID! В шапке боевого каталога WB
# лежат три ГОЛЫХ ID таблиц (`10kEZ…`!`Артикулы`) без домена: посчитай мы их «ссылками
# на таблицы учёта» — каталог товаров отвалился бы целиком (ISS-21). Голые ID — забота
# `redact_internal_ids` (вырезаются на выходе), а не барьера.
_GSHEET_URL_RE = re.compile(
    r"docs\.google\.com/(?:spreadsheets|document|presentation)"
    r"|drive\.google\.com/(?:drive/)?(?:folders|u/\d+/folders)",
    re.IGNORECASE,
)
# Строка-дисклеймер: «Что здесь НЕ лежит: цены, себестоимость, маржа». Легитимные файлы
# знания перечисляют денежные слова именно так — в отрицании. Такие строки в подсчёт
# плотности не идут, иначе каталог товаров блокировал бы сам себя своим же дисклеймером.
_FIN_DISCLAIMER_RE = re.compile(
    r"не\s+лежит|не\s+лежат|лежать\s+не\s+должн|не\s+должн\w*\s+быть"
    r"|здесь\s+нет|тут\s+нет|чего\s+здесь\s+нет|внутри\s+нет|\bне\s+вынош"
    r"|сюда\s+не\b|не\s+вписыва|\bнельзя\b|не\s+вход\w+|не\s+содержит"
    r"|без\s+цен|отсутству\w+|не\s+указыва\w+|это\s+не\b|\bне\s+про\b",
    re.IGNORECASE,
)

# Пороги плотности мягких терминов. Замер на боевых (худший случай — `dry-rental-brief`:
# 4 вида, 6 вхождений СЫРЫХ, но почти все в дисклеймере → после фильтра 1–2) против
# закрытых (`dry-rental.md`: 12 вхождений, 4 вида; `o-kompanii-mpervyi.md`: 22/8).
FIN_SOFT_MIN_HITS = 5    # вхождений мягких терминов вне дисклеймеров
FIN_SOFT_MIN_KINDS = 4   # ИЛИ разных видов мягких терминов
FIN_GSHEET_MAP_MIN = 3   # ссылок на Google-таблицы → это карта учётных таблиц


def _doc_title(text: str) -> str:
    """Заголовок документа — первый markdown-H1. Пусто, если его нет."""
    for line in (text or "").splitlines()[:40]:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def content_block_reason(text: str, rel: object = None) -> Optional[str]:
    """Судит файл знания ПО СОДЕРЖИМОМУ: «это про деньги компании?» → причина отказа.

    Чистая (без IO) — тестируется инъекцией строки, как `parse_manifest`. Зовётся ТОЛЬКО
    из `context_path_block_reason(rel, text=…)`: единая точка правды одна, второго фильтра
    в проекте нет (конвенция плана `watson-company-memory`).

    Причина отказа — метаданные (тип маркера + счётчик), НИКОГДА не цитата из файла и не
    найденная сумма: причина уходит в лог VPS и в Telegram владельцу (Опасная тройка).

    Предел (осознанный, зафиксирован в плане): барьер ловит ДЕНЬГИ, а не «всё
    чувствительное». Файл без денежных маркеров, но чувствительный по сути (стратегия
    сделки), он не поймает — остаток закрывает уведомление о ПРИНЯТЫХ файлах.
    """
    if not text or not str(text).strip():
        return None
    body = str(text)
    topic = f"{_doc_title(body)} {Path(str(rel or '')).name}".strip()

    # (1) ТЕМА документа — главный слой: реальные финфайлы базы знаний опознаются только им
    # (сумм внутри может не быть вовсе, см. эмпирику у констант).
    m = _FIN_TOPIC_STRONG_RE.search(topic)
    if m:
        return f"тема документа финансовая (маркер «{m.group(0).lower()}» в заголовке/имени файла)"

    # (2) Метка источника: статья базы знаний из финансового департамента.
    if _FIN_PLATRUM_RE.search(body):
        return "документ финансового департамента (метка базы знаний)"

    # (3) Файл сам объявил себя закрытым — только по ЗАГОЛОВКУ: слово «конфиденциально»
    # в теле легитимного файла (напр. «условия конфиденциальны») блокировать не за что.
    m = _CLOSED_MARK_RE.search(_doc_title(body))
    if m:
        return f"файл помечен закрытым («{m.group(0).lower()}» в заголовке)"

    # (4) Денежные суммы. В боевых файлах знания их НОЛЬ — одной достаточно (fail-closed).
    money = len(_FIN_MONEY_RE.findall(body)) + len(_FIN_MONEY_PREFIX_RE.findall(body))
    if money:
        return f"денежные суммы в тексте ({money} шт.)"

    # Построчный разбор: строки-дисклеймеры («Что здесь НЕ лежит: цены, маржа») из подсчёта
    # МЯГКИХ терминов исключаем — именно так легитимные файлы упоминают деньги.
    #
    # ⚠️ Дисклеймер гасит ТОЛЬКО плотность МЯГКИХ терминов (цикл из 5 ходов, Н1).
    # Слова-триггеры дисклеймера широки («нельзя», «это не», «отсутствуют») — и они же
    # обычная лексика финансового регламента. Пока под ними стояли ВСЕ сигналы, одно слово
    # переворачивало вердикт барьера:
    #   «Нельзя закрывать взаиморасчёты без акта сверки»      → файл проходил в промпт
    #   «Нельзя брать товар с маржинальностью ниже 20%»        → файл проходил в промпт
    # Поэтому жёсткий термин и финансовый процент считаются ВСЕГДА: первый блокирует с
    # первого вхождения, второй — это финансовый КРИТЕРИЙ, а не упоминание денег в
    # отрицании. Мягкие термины остаются под фильтром — их порог держится на плотности,
    # и без фильтра каталог товаров блокировал бы сам себя своим же дисклеймером
    # («Что здесь НЕ лежит: цены, себестоимость, маржа»).
    # Проверено на боевых обеих компаний: ни одной строки, где дисклеймер соседствует с
    # жёстким термином или процентом, — регресса каталога фикс не даёт.
    hard = 0
    soft_hits = 0
    soft_kinds: set[str] = set()
    percent_with_term = False
    for line in body.splitlines():
        h = _FIN_HARD_RE.findall(line)
        s = _FIN_SOFT_RE.findall(line)
        hard += len(h)
        if (h or s) and _FIN_PERCENT_RE.search(line):
            percent_with_term = True
        if _FIN_DISCLAIMER_RE.search(line):
            continue
        soft_hits += len(s)
        soft_kinds.update(x.lower() for x in s)

    # (5) Жёсткий термин учёта: ДДС / взаиморасчёты / оклад / касса / займ. В боевых — ноль.
    if hard:
        return f"термины денежного учёта ({hard} вхожд.)"

    # (6) Финансовый показатель в процентах («маржинальность более 20%») — денег в рублях
    # может не быть, а финансовый критерий есть.
    if percent_with_term:
        return "финансовый показатель в процентах (маржинальность / рентабельность)"

    gsheets = len(_GSHEET_URL_RE.findall(body))
    # (7) Карта учётных таблиц: файл-навигатор по закрытым Google-таблицам. Ровно за это
    # `accounting-tables-registry.md` и `google-drive-map.md` держат в денилисте.
    if gsheets >= FIN_GSHEET_MAP_MIN:
        return f"навигация по закрытым Google-таблицам ({gsheets} ссылок)"

    # (8) Плотность мягких терминов. Одно упоминание — шум (дисклеймер, определение
    # термина, зона ответственности); пять — документ про деньги.
    if soft_hits >= FIN_SOFT_MIN_HITS or len(soft_kinds) >= FIN_SOFT_MIN_KINDS:
        return f"плотность финансовых терминов ({soft_hits} вхожд., {len(soft_kinds)} видов)"

    # (9) Ссылка на таблицу учёта + денежный термин: обвязка вокруг закрытой таблицы.
    if gsheets and (soft_hits or hard):
        return f"ссылка на закрытую Google-таблицу рядом с денежными терминами ({gsheets} шт.)"

    # (10) Мягкая тема («учёт», «расход») — только с подкреплением: сама по себе она
    # бывает и не про деньги («учёт рабочего времени»).
    m = _FIN_TOPIC_SOFT_RE.search(topic)
    if m and (gsheets or soft_hits or hard):
        return f"учётный документ («{m.group(0).lower()}» в заголовке + денежные признаки внутри)"
    return None


# --- R6 (Ф2): чтение белого списка knowledge/ в контекст сборки протокола ------


def context_path_block_reason(rel: object, text: Optional[str] = None) -> Optional[str]:
    """ЕДИНСТВЕННАЯ точка правды «можно ли этот путь знания отдать во внешний LLM».

    Возвращает ПРИЧИНУ блокировки (короткая строка для лога) либо `None`, если файл
    допустим. Барьеры, по порядку:
      1. структура пути — абсолютный / `~` / `..` (выход за репо);
      2. денилист по basename (финкарты, экономика, финмодель, HR-конфиденшел);
      3. запрет по пути — любой сегмент-директория из `PROTOCOL_CONTEXT_RESTRICTED_DIRS`
         (всё под `restricted/`, где бы оно ни лежало);
      4. чувствительный паттерн имени (вторичная страховка, см. РИСК1 у констант);
      5. **СОДЕРЖИМОЕ файла** (`content_block_reason`) — только если `text` передан.

    `text=None` → работают барьеры 1–4 (путь известен, файл ещё не прочитан: так зовут
    self-check allow-list и любая проверка «можно ли вписать такой путь»). `text` передан
    → добавляется барьер 5. Это НЕ второй фильтр, а тот же самый: слой 5 живёт здесь же,
    и обойти его, минуя эту функцию, нельзя.

    Зачем слой 5 (план `knowledge-content-barrier`, эмпирика аудита). Слои 1–4 судят файл
    ПО ИМЕНИ, а финансовые файлы базы знаний названы транслитом — `867-nalichnye-kassy-…`,
    `924-uchet-dds`, `925-…sebestoimosti-postavki` — и ни один паттерн их не ловит. Впиши
    такой файл в манифест (а манифест ведёт команда, без ревью кода) — и деньги компании
    уехали бы во внешнюю модель. Слой 5 смотрит внутрь и судит по содержимому.

    Бэкстоп живёт в КОДЕ: правкой манифеста в `*-context` его не обойти (A3 плана).
    """
    p = str(rel or "").strip()
    if not p:
        return "пустой путь"
    parts = Path(p).parts
    if p.startswith("/") or p.startswith("~") or ".." in parts:
        return "небезопасный путь (абсолютный / `~` / выход за репо через `..`)"
    if Path(p).name.lower() in {d.lower() for d in PROTOCOL_CONTEXT_DENYLIST}:
        return "денилист (финансы / HR-конфиденшел / внутренняя карта)"
    segments = [seg.lower() for seg in parts]
    # ВСЕ сегменты, включая последний: путь может указывать на саму директорию
    # (`company/restricted`), а фильтр манифеста Ф2 зовёт эту функцию БЕЗ проверки
    # `is_file()` — там такой путь прошёл бы. Файл, буквально названный `restricted`
    # (без расширения), не существует ни в одном `*-context` (проверено), а
    # `restricted.md` — другой сегмент и под запрет не попадает.
    for seg in segments:
        if seg in PROTOCOL_CONTEXT_RESTRICTED_DIRS:
            return f"путь внутри `{seg}/` (закрытая директория)"
    for seg in segments:
        for pattern in PROTOCOL_CONTEXT_SENSITIVE_PATTERNS:
            if fnmatch.fnmatch(seg, pattern):
                return f"чувствительный паттерн имени `{pattern}`"
    if text is not None:
        return content_block_reason(text, rel=p)
    return None


def assert_whitelist_safe() -> None:
    """Самопроверка инвариантов whitelist (вызывается из теста R6/R1): каждый путь
    allow-list обязан проходить бэкстоп `context_path_block_reason`. Падает, если
    в allow-list просочился денилист-файл / `restricted/` / чувствительное имя /
    абсолютный путь или `..`.

    Это НЕ рантайм-страховка (её несёт `load_whitelisted_context`), а контрактный
    тест: ошибка конфигурации allow-list обязана падать громко, а не утекать."""
    for rel in PROTOCOL_CONTEXT_WHITELIST:
        reason = context_path_block_reason(rel)
        if reason:
            raise AssertionError(
                f"whitelist-путь {str(rel)!r} нарушает бэкстоп приватности: {reason}"
            )


def _is_within(child: Path, parent: Path) -> bool:
    """`child` лежит внутри `parent` (после резолва симлинков/`..`). Бэкстоп от
    path-traversal: даже если whitelist кто-то испортит, читаем только из репо."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _real_path_block_reason(path: Path, root: Path) -> Optional[str]:
    """Тот же бэкстоп, но по РЕАЛЬНОМУ пути файла (после резолва симлинков).

    Закрывает обход через симлинк: `knowledge/ok.md → ../restricted/salaries.md`
    имеет чистое имя в allow-list и лежит внутри репо (`_is_within` доволен), но
    реально указывает в закрытую директорию. Сверяем ещё раз — уже по тому, что
    файл есть на самом деле."""
    try:
        real_rel = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return "реальный путь (после симлинков) вне корня репо"
    return context_path_block_reason(str(real_rel))


def _hardlink_block_reason(path: Path) -> Optional[str]:
    """Второй обход, симметричный симлинку: ЖЁСТКАЯ ссылка (hardlink).

    У симлинка есть «цель», и `resolve()` её раскрывает — поэтому `knowledge/ok.md →
    restricted/salaries.md` мы ловим. У hardlink'а цели НЕТ: это второе ИМЯ того же
    инода, и `resolve()` возвращает сам путь, не выдавая содержимое. То есть
    `ln restricted/people-confidential.md knowledge/ok.md` утащил бы HR-файл в промпт
    мимо ВСЕХ проверок по пути — имя-то чистое.

    Признак: `st_nlink > 1` (на файл ссылаются ≥2 имени). Git иноды не переносит —
    после `clone`/`pull` (и после `context_pull.sh` на VPS) у каждого файла nlink=1,
    поэтому >1 означает, что ссылку сделали руками локально. Fail-closed: не читаем.
    Цена ложного срабатывания — файл знания не попал в промпт + WARNING; цена
    пропуска — зарплаты во внешнем LLM. Асимметрия однозначная.
    """
    try:
        if path.stat().st_nlink > 1:
            return "жёсткая ссылка (hardlink) — имя файла не доказывает его содержимое"
    except OSError as e:
        return f"не прочитать метаданные файла ({type(e).__name__})"
    return None


def apply_total_budget(
    files: list[tuple[str, str]], budget: Optional[int] = None
) -> list[tuple[str, str]]:
    """Обрезать набор знания под СУММАРНЫЙ бюджет `PROTOCOL_CONTEXT_TOTAL_MAX_BYTES`.

    Чистая (без IO) — тестируется инъекцией списка, как `filter_glossary_by_company`.
    Порядок входа = ПРИОРИТЕТ (порядок allow-list/манифеста): файлы набираются
    подряд, пока влезают; первый непоместившийся и весь хвост за ним отбрасываются
    ЦЕЛИКОМ — «обрезка по границе файлов», без дырок в середине и без обрезанных
    посреди фразы файлов.

    Единственное исключение — когда ПЕРВЫЙ же файл больше всего бюджета: выбросить
    его целиком нельзя (бриф стал бы пуст и из промпта пропал бы канон артикулов —
    регресс ISS-21), поэтому он честно режется по бюджету, тоже с WARNING.

    Превышение НИКОГДА не проходит молча: в лог уходят счётчики и ИМЕНА файлов
    (метаданные), но НЕ их текст — Опасная тройка (R8).
    """
    limit = PROTOCOL_CONTEXT_TOTAL_MAX_BYTES if budget is None else int(budget)
    out: list[tuple[str, str]] = []
    dropped: list[tuple[str, int]] = []
    used = 0
    for i, (rel, text) in enumerate(files):
        size = len(text.encode("utf-8"))
        if used + size <= limit:
            out.append((rel, text))
            used += size
            continue
        if not out and used == 0:
            # Первый файл сам по себе не влезает — режем по бюджету, не выбрасываем.
            head = text.encode("utf-8")[: max(0, limit)].decode("utf-8", errors="ignore").strip()
            logger.warning(
                "бюджет знания: файл %s = %d Б один превышает бюджет %d Б — урезан по бюджету "
                "(часть файла в промпт не попадёт)",
                rel, size, limit,
            )
            if head:
                out.append((rel, head))
                used = len(head.encode("utf-8"))
        else:
            dropped.append((rel, size))
        dropped.extend((r, len(t.encode("utf-8"))) for r, t in files[i + 1:])
        break
    if dropped:
        logger.warning(
            "бюджет знания исчерпан (%d Б из %d Б): %d файл(ов) НЕ вошли в бриф — %s. "
            "Обрезка по границе файлов; порядок allow-list = приоритет (правь состав манифеста)",
            used, limit, len(dropped), ", ".join(r for r, _ in dropped),
        )
    return out


# --- Ф2 (R3/R9): per-company МАНИФЕСТ знания для протокола ---------------------
#
# Состав знания, который видит бот, ведёт КОМАНДА — текстовым файлом в своём репо
# контекста, без деплоя кода (A3/R9 плана `watson-company-memory`):
#
#     <repo>/knowledge/notary/protocol-knowledge-allowlist.txt
#
# Формат (нарочно примитивный — его правит не программист):
#     # строка-комментарий
#     catalog: knowledge/product-catalog-wb.md      ← каталог товаров (канон артикулов)
#     company/o-kompanii-mpervyi.md                 ← файл брифа компании
#
# Путь — относительно КОРНЯ репо контекста. Префикс `catalog:` помечает каталог
# товаров: он рендерится отдельной секцией со своим правилом подстановки товаров
# (R7) и ставится ПЕРВЫМ в бюджете. Без префикса — файл «брифа компании» (R5).
#
# 🔒 ГРАНИЦА (A3, инвариант Ф1). Манифест — это ДАННЫЕ в чужом репо, их правит
# команда, НЕ прогоняя наши тесты. Поэтому манифест НЕ может расширить права: любой
# его путь проходит тот же `context_path_block_reason`, что и хардкод-whitelist.
# Вписать в манифест финансы/HR/`restricted` можно — прочитать их нельзя.
PROTOCOL_KNOWLEDGE_MANIFEST_FILE = "protocol-knowledge-allowlist.txt"
MANIFEST_CATALOG_PREFIX = "catalog:"

# Сколько проблемных файлов перечислять в Telegram-алерте (у сообщения есть потолок
# длины; массовая поломка манифеста не должна утопить сам сигнал о поломке).
_ALERT_MAX_ITEMS = 10

# Порог раннего предупреждения о бюджете. Сигнал «файл выпал» приходит ПОСТФАКТУМ — когда
# один протокол уже собрался обеднённым. У МПервый занято 75% (38.5 из 50 КБ), запас — 2–3
# средних файла: команда добавит второй бренд, и хвост вытеснится. Предупреждаем ЗАРАНЕЕ.
_BUDGET_WARN_RATIO = 0.85

# `\s+#` — комментарий в конце строки (не режем `#` внутри имени файла, хотя таких
# у нас нет: путь `a#b.md` остался бы целым, а `a.md  # коммент` — обрезался).
_MANIFEST_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


class KnowledgeBundle(NamedTuple):
    """Знание компании, готовое к вставке в промпт (результат `load_protocol_knowledge`).

    catalog  — файлы каталога товаров (канон названий/артикулов), секция R6/R7.
    brief    — файлы «брифа компании» (позиционирование/линейки/кто есть кто), секция R5.
    rejected — [(путь, причина)] того, что в промпт НЕ попало: бэкстоп, нет файла,
               не влез в бюджет. Основа ВИДИМОГО СИГНАЛА (follow-up 1 из Ф1): без него
               человек правит манифест и не знает, что бот его файл не взял.
    source   — откуда взят СОСТАВ: `manifest` (файл компании) | `code-fallback`
               (константа в коде = поведение как до Ф2) | `none` (репо неизвестно).
    """

    catalog: list[tuple[str, str]]
    brief: list[tuple[str, str]]
    rejected: list[tuple[str, str]]
    source: str


def manifest_path(company: Optional[str]) -> Optional[Path]:
    """Путь к манифесту знания компании. None, если репо компании неизвестно."""
    kdir = knowledge_dir(company)
    if not kdir:
        return None
    return kdir / PROTOCOL_KNOWLEDGE_MANIFEST_FILE


def parse_manifest(text: str) -> tuple[list[str], list[str]]:
    """Текст манифеста → `(пути каталога, пути брифа)`. Чистая, без IO (тестируется
    инъекцией строки, как `filter_glossary_by_company`).

    Пустые строки и `#`-комментарии игнорируются; порядок сохраняется (= приоритет
    при бюджете); дубли схлопываются (первое вхождение побеждает). Разбор НЕ судит
    о безопасности пути — это работа `context_path_block_reason` на чтении.
    """
    catalog: list[str] = []
    brief: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _MANIFEST_INLINE_COMMENT_RE.sub("", line).strip()
        if not line:
            continue
        is_catalog = line.lower().startswith(MANIFEST_CATALOG_PREFIX)
        if is_catalog:
            line = line[len(MANIFEST_CATALOG_PREFIX):].strip()
        # Нормализуем бытовые формы записи пути, чтобы дубль `./a.md` и `a.md` не
        # проехал дважды. `/`-абсолют и `..` НЕ чиним — их обязан отбить бэкстоп.
        while line.startswith("./"):
            line = line[2:]
        line = line.strip().strip('"').strip("'").strip()
        if not line or line in seen:
            continue
        seen.add(line)
        (catalog if is_catalog else brief).append(line)
    return catalog, brief


def read_manifest(company: Optional[str]) -> Optional[tuple[list[str], list[str]]]:
    """Манифест компании с диска. `None` — файла нет / не читается / он пуст по сути
    (тогда вызыватель уходит в код-фолбэк). Graceful, как весь загрузчик."""
    mpath = manifest_path(company)
    if not mpath or not mpath.is_file():
        return None
    try:
        # `utf-8-sig`, а не `utf-8`: манифест по замыслу правит НЕ программист (в этом вся
        # суть R3/A3), а Блокнот / экспорт из Excel / копипаст из Word сохраняют файл с BOM.
        # Глазами файл выглядит нормально, но BOM прилипает к первой строке: шапка-комментарий
        # перестаёт быть комментарием и становится «путём», а если BOM сядет на строку
        # `catalog:` — префикс не распознается и каталог тихо деградирует в справку.
        text = mpath.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        logger.warning("не прочитать манифест знания %s: %s — код-фолбэк", mpath, e)
        return None
    catalog, brief = parse_manifest(text)
    if not catalog and not brief:
        logger.warning(
            "манифест знания %s пуст (ни одного пути) — код-фолбэк (поведение как до Ф2)", mpath
        )
        return None
    return catalog, brief


def resolve_knowledge_manifest(
    company: Optional[str],
) -> tuple[list[str], list[str], str, list[tuple[str, str]]]:
    """Эффективный СОСТАВ знания компании: `(каталог, бриф, источник, потери)`.

    Манифест есть → он источник истины («бот читает ИМЕННО их», критерий R3).
    Манифеста нет / он пуст → код-фолбэк `PROTOCOL_CONTEXT_WHITELIST` (РИСК2: канон
    артикулов ISS-21 не должен пропасть только оттого, что манифест не доехал).

    `потери` — [(путь, причина)] того, что команда НЕ вписала, хотя файл лежит в репо:
    известный каталог товаров пропал из манифеста. Это не «файл отклонён» (его никто
    не просил) — это ТИХАЯ ПОЛОМКА: канон артикулов молча исчезает из промпта, и бот
    снова начинает фонетить названия товаров (регресс ISS-21). Возвращаем наверх,
    чтобы сигнал дошёл до ЧЕЛОВЕКА (Telegram-алерт + CLI), а не только до лога VPS,
    который никто не читает — иначе «видимый сигнал» слеп ровно в самом дорогом случае.

    Здесь НЕТ фильтрации по безопасности — только состав. Фильтр один, и он на
    чтении (`_read_knowledge_file` → `context_path_block_reason`).
    """
    manifest = read_manifest(company)
    if manifest is None:
        return list(PROTOCOL_CONTEXT_WHITELIST), [], "code-fallback", []
    # Копии: ниже мы переставляем файл из брифа в каталог, а вход мутировать нельзя —
    # сегодня `parse_manifest` отдаёт свежие списки, но стоит кому-то закешировать разбор
    # манифеста (соблазн есть: диск читается на каждой генерации), и мутация испортит кеш.
    catalog, brief = list(manifest[0]), list(manifest[1])
    missing: list[tuple[str, str]] = []
    root = repo_root(company)
    if root:
        for rel in PROTOCOL_CONTEXT_WHITELIST:
            if rel in catalog:
                continue
            if rel in brief:
                # Каталог вписан, но БЕЗ префикса `catalog:`. Воля команды есть —
                # ошиблись только в форме записи. Как файл брифа он потерял бы правило
                # R7 («незнакомый товаро-подобный токен пиши как услышано») и приоритет
                # в бюджете. Классифицируем правильно и говорим об этом громко: это НЕ
                # молчаливая дописка (файл в манифесте), а починка формы.
                brief.remove(rel)
                # В КОНЕЦ каталогов, не в начало: то, что команда пометила `catalog:`
                # явно, приоритетнее того, что мы опознали за неё. Главное уже достигнуто —
                # файл попал в каталог, а значит идёт впереди всего брифа в бюджете и
                # получает правило подстановки артикулов.
                catalog.append(rel)
                logger.warning(
                    "каталог товаров %s вписан в манифест БЕЗ префикса `catalog:` — "
                    "читаю его как каталог (иначе пропали бы правило подстановки артикулов "
                    "и приоритет в бюджете). Поправь строку в манифесте: `catalog: %s`",
                    rel, rel,
                )
                continue
            if (root / rel).is_file():
                missing.append((
                    str(rel),
                    "каталог товаров ЕСТЬ в репозитории, но НЕ вписан в манифест — "
                    f"канон названий и артикулов потерян (добавь строку `catalog: {rel}`)",
                ))
                logger.warning(
                    "каталог товаров %s ЕСТЬ в репозитории, но НЕ вписан в манифест — "
                    "он НЕ попадёт в промпт (канон артикулов будет потерян, ISS-21)", rel,
                )
    return catalog, brief, "manifest", missing


# --- Видимый сигнал: «твой файл не подключён, причина Y» (follow-up 1 из Ф1) ----


def manifest_report(company: Optional[str], bundle: Optional[KnowledgeBundle] = None) -> str:
    """Человекочитаемый отчёт «что бот РЕАЛЬНО видит у компании» — для CLI-самопроверки
    (`tools/check-knowledge-manifest.py`) и как тело Telegram-алерта.

    Зачем: манифест правит команда, а бэкстоп живёт в коде. Без этого отчёта отказ
    виден только в логе VPS, которого никто не читает, — и человек уверен, что бот
    знает про его файл, хотя бот не знает (тихая поломка, РИСК из Ф1).

    Опасная тройка: в отчёте ТОЛЬКО пути, размеры и причины — ни байта содержимого.
    """
    b = bundle if bundle is not None else load_protocol_knowledge(company)
    lines: list[str] = []
    total = sum(len(t.encode("utf-8")) for _, t in list(b.catalog) + list(b.brief))
    src = {
        "manifest": f"манифест knowledge/notary/{PROTOCOL_KNOWLEDGE_MANIFEST_FILE}",
        "code-fallback": "код-фолбэк (манифеста нет — поведение как до Ф2)",
        "none": "репозиторий контекста компании неизвестен",
    }.get(b.source, b.source)
    pct = int(100 * total / max(1, PROTOCOL_CONTEXT_TOTAL_MAX_BYTES))
    lines.append(f"Компания: {company or '—'} · источник состава: {src}")
    lines.append(
        f"В промпт войдёт: каталог={len(b.catalog)}, бриф={len(b.brief)}, "
        f"итого {total} Б из бюджета {PROTOCOL_CONTEXT_TOTAL_MAX_BYTES} Б ({pct}%)"
    )
    # Предупреждаем ДО того, как файл выпал: иначе человек узнаёт о потолке постфактум.
    if total > PROTOCOL_CONTEXT_TOTAL_MAX_BYTES * _BUDGET_WARN_RATIO:
        lines.append(
            f"  ⚠️  бюджет заполнен на {pct}% — следующий крупный файл вытеснит из промпта "
            "ПОСЛЕДНИЕ строки манифеста. Прежде чем добавлять, реши, что важнее"
        )
    for rel, text in b.catalog:
        lines.append(f"  ✅ каталог  {rel} ({len(text.encode('utf-8'))} Б)")
    for rel, text in b.brief:
        lines.append(f"  ✅ бриф     {rel} ({len(text.encode('utf-8'))} Б)")
    for rel, reason in b.rejected:
        lines.append(f"  ❌ НЕ вошёл {rel} — {reason}")
    return "\n".join(lines)


def alert_manifest_problems(
    company: Optional[str],
    bundle: KnowledgeBundle,
    *,
    pusher: object = None,
) -> bool:
    """Видимый сигнал владельцу в Telegram: файлы из манифеста НЕ подключены.

    Шлём ТОЛЬКО когда есть что сказать (`rejected` непуст) — молчание при здоровом
    манифесте, никакого спама на каждой встрече. Дедуп — по НАБОРУ проблем: та же
    поломка не повторяется 6 ч (окно `notify`), но НОВАЯ (другой файл/причина) даёт
    новый ключ и доходит сразу.

    `pusher` инъектируется (как в `stt_weekly_guard`): в проде — `notify.push`, в
    тестах — заглушка. Никогда не бросает: сломанный алерт не должен ронять протокол.
    """
    if not bundle.rejected:
        return False
    head = f"⚠️ Знание компании «{company}»: {len(bundle.rejected)} файл(ов) НЕ подключены к боту"
    # Лимит строк: у Telegram потолок сообщения (~4096 знаков), и массовая поломка
    # манифеста (десятки кривых строк) утопила бы САМ алерт о поломке. Показываем
    # первые, остальные — счётчиком; полный разбор даёт CLI `check-knowledge-manifest.py`.
    shown = bundle.rejected[:_ALERT_MAX_ITEMS]
    body = "\n".join(f"• {rel} — {reason}" for rel, reason in shown)
    if len(bundle.rejected) > _ALERT_MAX_ITEMS:
        body += (
            f"\n• …и ещё {len(bundle.rejected) - _ALERT_MAX_ITEMS}. "
            "Полный список: `python3 tools/check-knowledge-manifest.py`"
        )
    tail = (
        f"Протокол соберётся БЕЗ них. Состав правится в "
        f"{KNOWLEDGE_SUBPATH}/{PROTOCOL_KNOWLEDGE_MANIFEST_FILE} репозитория контекста."
    )
    msg = f"{head}\n{body}\n{tail}"
    key = "knowledge-manifest:%s:%s" % (
        company,
        hashlib.sha256("|".join(f"{r}:{c}" for r, c in bundle.rejected).encode("utf-8")).hexdigest()[:12],
    )
    if pusher is None:
        try:
            from . import notify  # noqa: PLC0415 — lazy: тестам не нужен tg-send
            pusher = notify.push
        except Exception:  # noqa: BLE001
            return False
    try:
        return bool(pusher(msg, dedupe_key=key))  # type: ignore[operator]
    except Exception as e:  # noqa: BLE001 — алерт не имеет права уронить сборку
        logger.warning("не отправить алерт о манифесте знания: %s", type(e).__name__)
        return False


# --- Ф1: уведомление о ПРИНЯТЫХ файлах (дифф состава брифа) --------------------
#
# ЗАЧЕМ. Сигнал `alert_manifest_problems` выше шлёт владельцу только ОТКЛОНЁННЫЕ файлы.
# Принятые проходят молча — а значит, расширение состава знания (то самое, из-за которого
# что-то может уехать во внешнюю модель) невидимо. Барьер по содержимому ловит ДЕНЬГИ, но
# не «всё чувствительное»: файл со стратегией сделки он пропустит. Уведомление о принятых
# закрывает остаток — владелец видит КАЖДЫЙ новый файл, попавший в промпт.
#
# Молчим, когда состав не менялся: иначе на каждой встрече приходил бы один и тот же
# список — и его перестали бы читать (а с ним и настоящие сигналы).

_COMPOSITION_FILE = "knowledge_composition.json"
# Файл вырос существенно — это тоже расширение состава (кто-то влил в него много нового
# текста), но пороги высокие: квартальное обновление каталога (+5%) молчит, а удвоение —
# нет. Без этого «состав тот же» скрывал бы подмену содержимого файла.
_COMPOSITION_GROWTH_RATIO = 1.5
_COMPOSITION_GROWTH_MIN_BYTES = 2048


def _notary_state_dir() -> Path:
    """Каталог состояния нотариуса (`config/` на VPS) — тот же, что у `knowledge_ratchet`
    и `auto_vocab/state`: берётся из `SPEECHMATICS_VOCAB_PATH`."""
    vocab = os.environ.get(
        "SPEECHMATICS_VOCAB_PATH", "/srv/meeting-notary/config/speechmatics-vocab.json"
    )
    return Path(vocab).expanduser().parent


def composition_state_path() -> Path:
    """Путь к снимку состава знания. Env `NOTARY_KNOWLEDGE_COMPOSITION_PATH` → override."""
    env = os.environ.get("NOTARY_KNOWLEDGE_COMPOSITION_PATH")
    if env:
        return Path(env).expanduser()
    return _notary_state_dir() / _COMPOSITION_FILE


def load_composition_state() -> dict:
    """Снимок состава `{company: {rel: размер}}`. Нет файла / битый → пустой (graceful)."""
    path = composition_state_path()
    if not path.is_file():
        return {}
    try:
        import json  # noqa: PLC0415 — lazy, как yaml: модуль нужен только этой ветке
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("снимок состава не объект")
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:  # noqa: BLE001 — битый снимок не имеет права ронять протокол
        logger.warning(
            "снимок состава знания битый (%s) — считаем, что снимка нет", type(e).__name__
        )
        return {}


def _save_composition_state(state: dict) -> bool:
    """Атомарная запись снимка (tmp + replace, как `knowledge_ratchet._write_atomic`)."""
    path = composition_state_path()
    try:
        import json  # noqa: PLC0415
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as e:
        logger.warning("не сохранить снимок состава знания: %s", type(e).__name__)
        return False


# Сколько ждать лок снимка, прежде чем писать без него. Критическая секция — чтение и
# запись пары килобайт JSON, то есть миллисекунды; 2 с — это запас на порядки.
_COMPOSITION_LOCK_WAIT_SEC = 2.0
_COMPOSITION_LOCK_POLL_SEC = 0.05


@contextmanager
def _composition_lock() -> "Iterator[None]":
    """Эксклюзивный лок на снимок состава — как `knowledge_ratchet._lock()`.

    Снимок общий для ВСЕХ компаний (`{company: {...}}`), а обновляется он циклом
    read-modify-write. Сборки протокола идут из разных процессов (`finalize-meeting`,
    `meetings_listener`, `clarify_worker`, ручной `regenerate-protocol`) и пересекаются
    во времени: без лока второй пишущий затирает ключ первого целиком — и добавленный
    файл другой компании выпадает из снимка. Следующая сборка увидит `first_run` и
    ПРОМОЛЧИТ — то есть новый файл уедет во внешнюю модель без уведомления (цикл из 5
    ходов, Н4).

    ⚠️ Захват НЕ блокирующий (`LOCK_NB` + опрос), и это принципиально. `flock` не
    реентрантен: два `LOCK_EX` на один файл ИЗ ОДНОГО процесса встают намертво друг
    против друга. Блокирующий вариант превратил бы любой будущий вложенный вызов
    («давайте зафиксируем снимок прямо внутри диффа») в вечный вис сборки протокола на
    VPS — бот замолчал бы без единой ошибки в логе. Не дождались за `_LOCK_WAIT` →
    пишем без лока и говорим об этом в лог: редкая гонка переживаема, вечный вис — нет.
    Та же дисциплина, что во всём модуле: снимок важен, но ронять протокол он не вправе.
    """
    path = composition_state_path()
    lf = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lf = open(path.with_suffix(path.suffix + ".lock"), "w")
    except OSError as e:
        logger.warning("снимок состава: лок недоступен (%s) — пишу без него", type(e).__name__)
        lf = None
    locked = False
    if lf is not None:
        deadline = time.monotonic() + _COMPOSITION_LOCK_WAIT_SEC
        while True:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:  # BlockingIOError — лок держит кто-то другой
                if time.monotonic() >= deadline:
                    logger.warning(
                        "снимок состава: лок занят дольше %.0f с — пишу без него",
                        _COMPOSITION_LOCK_WAIT_SEC,
                    )
                    break
                time.sleep(_COMPOSITION_LOCK_POLL_SEC)
    try:
        yield
    finally:
        if lf is not None:
            try:
                if locked:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            finally:
                lf.close()


def persist_knowledge_composition(company: Optional[str], bundle: KnowledgeBundle) -> bool:
    """Зафиксировать текущий состав как эталон для следующего диффа.

    Отделена от `diff_knowledge_composition` НАМЕРЕННО (цикл из 5 ходов, Н2): снимок
    обязан обновляться ТОЛЬКО после того, как владелец реально узнал о новых файлах.
    Записать его раньше отправки — значит потерять алерт навсегда при первом же сбое
    Telegram: следующая сборка увидит «состав тот же» и промолчит, а файл уже в промпте.
    Ровно та тихая утечка, которую B3 объявляет невозможной.

    Пустой состав снимок НЕ затирает: пустой bundle — это деградация (репо контекста не
    доехал на VPS), а не «состав обнулился».
    """
    current = bundle_composition(bundle)
    if not current:
        return False
    key = _composition_key(company)
    with _composition_lock():
        state = load_composition_state()
        if state.get(key) == current:
            return True  # уже актуален — диск не трогаем
        state[key] = current
        return _save_composition_state(state)


def _composition_key(company: Optional[str]) -> str:
    return str(company or "").strip().lower() or "—"


def bundle_composition(bundle: KnowledgeBundle) -> dict[str, int]:
    """Состав брифа как `{путь: размер в байтах}` — то, что реально ушло бы в промпт."""
    return {
        str(rel): len(text.encode("utf-8"))
        for rel, text in list(bundle.catalog) + list(bundle.brief)
    }


def diff_knowledge_composition(
    company: Optional[str], bundle: KnowledgeBundle, *, persist: bool = True
) -> tuple[list[tuple[str, int]], list[tuple[str, int, int]], bool]:
    """Сравнить состав брифа с прошлой сборкой. → `(добавленные, выросшие, первый_запуск)`.

    `добавленные` — [(путь, размер)] файлов, которых в прошлом снимке не было.
    `выросшие`    — [(путь, было, стало)] файлов, распухших выше порога.
    `первый_запуск` — снимка для этой компании ещё нет: значит СРАВНИВАТЬ НЕ С ЧЕМ.
      Тогда `добавленные` пуст НАМЕРЕННО: иначе на первой же встрече после деплоя владелец
      получил бы «в бриф добавлено 5 файлов» — весь текущий состав, который он и так знает.
      Снимок при этом сохраняется (baseline), и со следующей сборки дифф работает честно.

    Пустой состав снимок НЕ перезаписывает: пустой bundle — это деградация (репо контекста
    не доехал на VPS), а не «состав обнулился». Иначе после восстановления репо все файлы
    выглядели бы новыми — и уведомление пришло бы на ровном месте.
    """
    current = bundle_composition(bundle)
    key = _composition_key(company)
    state = load_composition_state()
    previous = state.get(key)
    if not current:
        return [], [], previous is None
    first_run = not isinstance(previous, dict) or not previous
    added: list[tuple[str, int]] = []
    grown: list[tuple[str, int, int]] = []
    if not first_run:
        for rel, size in current.items():
            was = previous.get(rel)
            if not isinstance(was, int):
                added.append((rel, size))
            elif size >= was * _COMPOSITION_GROWTH_RATIO and size - was >= _COMPOSITION_GROWTH_MIN_BYTES:
                grown.append((rel, was, size))
    # `persist=True` — только для вызывателей, которым НЕЧЕГО сообщать владельцу (baseline,
    # обновление размеров). Когда есть что сообщать, снимок фиксирует `alert_new_knowledge_files`
    # ПОСЛЕ успешной отправки: иначе сбой Telegram навсегда съедает алерт (Н2).
    if persist and (first_run or added or grown or current != previous):
        with _composition_lock():
            fresh = load_composition_state()
            fresh[key] = current
            _save_composition_state(fresh)
    return added, grown, first_run


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} КБ".replace(".0 КБ", " КБ")


def alert_new_knowledge_files(
    company: Optional[str], bundle: KnowledgeBundle, *, pusher: object = None
) -> bool:
    """Видимый сигнал владельцу: в бриф компании ДОБАВИЛСЯ файл (B3).

    Шлём только на ИЗМЕНЕНИЕ состава — молчим, когда бриф тот же (никакого спама на каждой
    встрече). Канал и дисциплина — те же, что у `alert_manifest_problems` (Telegram, дедуп,
    инъекция `pusher`, никогда не бросает).

    СНИМОК ФИКСИРУЕТСЯ ТОЛЬКО ПОСЛЕ УСПЕШНОЙ ОТПРАВКИ (цикл из 5 ходов, Н2). Сбой канала
    (Telegram лёг, tg-send не найден) → снимок остаётся прежним, и на следующей сборке мы
    попробуем снова. Записать снимок раньше — значит навсегда потерять алерт о файле,
    который УЖЕ уехал во внешнюю модель: «состав тот же» → молчим. Повторного спама это не
    даёт — дедуп `notify.push` по ключу набора глушит повтор на 6 ч; ровно так же (без
    состояния, с опорой на дедуп) живёт соседний `alert_manifest_problems`.

    Опасная тройка: в сообщении только имя файла и размер — ни байта содержимого.
    """
    try:
        added, grown, first_run = diff_knowledge_composition(company, bundle, persist=False)
    except Exception as e:  # noqa: BLE001 — дифф не имеет права уронить сборку протокола
        logger.warning("не сравнить состав знания: %s", type(e).__name__)
        return False
    if first_run or (not added and not grown):
        # Сообщать нечего (baseline / состав не менялся / файл убрали) — снимок можно
        # фиксировать сразу: терять нечего.
        try:
            persist_knowledge_composition(company, bundle)
        except Exception as e:  # noqa: BLE001
            logger.warning("не сохранить снимок состава знания: %s", type(e).__name__)
        return False
    lines: list[str] = []
    for rel, size in added[:_ALERT_MAX_ITEMS]:
        lines.append(f"• в бриф добавлен: {rel} ({_kb(size)})")
    for rel, was, now in grown[:_ALERT_MAX_ITEMS]:
        lines.append(f"• вырос файл: {rel} ({_kb(was)} → {_kb(now)})")
    extra = max(0, len(added) + len(grown) - len(lines))
    if extra:
        lines.append(f"• …и ещё {extra}")
    head = f"📎 Знание компании «{company}»: состав брифа расширен"
    tail = (
        "Эти файлы теперь уходят во внешнюю модель при сборке КАЖДОГО протокола. "
        f"Состав правится в {KNOWLEDGE_SUBPATH}/{PROTOCOL_KNOWLEDGE_MANIFEST_FILE} "
        "репозитория контекста. Если файл не должен там быть — убери строку."
    )
    msg = f"{head}\n" + "\n".join(lines) + f"\n{tail}"
    key = "knowledge-added:%s:%s" % (
        company,
        hashlib.sha256(
            "|".join([f"+{r}:{s}" for r, s in added] + [f">{r}:{n}" for r, _w, n in grown]).encode("utf-8")
        ).hexdigest()[:12],
    )
    if pusher is None:
        try:
            from . import notify  # noqa: PLC0415 — lazy: тестам не нужен tg-send
            pusher = notify.push
        except Exception:  # noqa: BLE001
            return False
    try:
        sent = bool(pusher(msg, dedupe_key=key))  # type: ignore[operator]
    except Exception as e:  # noqa: BLE001
        logger.warning("не отправить алерт о новых файлах знания: %s", type(e).__name__)
        return False
    if sent:
        # Владелец узнал → фиксируем состав как эталон. Не отправилось → снимок НЕ трогаем:
        # повторим на следующей сборке (Н2).
        try:
            persist_knowledge_composition(company, bundle)
        except Exception as e:  # noqa: BLE001
            logger.warning("не сохранить снимок состава знания: %s", type(e).__name__)
    return sent


# Внутренняя навигация наружу не уходит (цикл из 5 ходов, Н4). В шапке боевого каталога
# товаров («где брать живьём») лежат ID трёх ЗАКРЫТЫХ Google-таблиц — людям они там нужны,
# файл это источник истины команды. Но файл целиком уходит во ВНЕШНИЙ LLM, а политика
# проекта держит drive-карты в денилисте ровно потому, что внутреннюю навигацию наружу не
# отдают. Правим не чужие данные, а СВОЁ чтение: идентификатор редактируется на выходе,
# текст и смысл файла остаются. Барьер общий — он накрывает и каталог, и бриф, и любой
# файл знания, который команда добавит завтра.
#
# Регекс намеренно узкий: Google-ID — длинная строка СМЕШАННОГО регистра с цифрами
# (`10kEZCmDItFu5yArvxFfRiu9V3lZcCnP4dNCQSwhRup0`). Требование «есть строчная И заглавная
# И цифра» отсекает ложные срабатывания на ALL_CAPS-константах и артикулах товаров.
_INTERNAL_ID_RE = re.compile(
    r"\b(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{25,}\b"
)
# Второй заход — ID В КОНТЕКСТЕ ссылки (`docs.google.com/spreadsheets/d/<id>`). Регекс выше
# требует смешанного регистра И цифры, чтобы не трогать артикулы товаров; ID, состоящий из
# одних строчных букв, он бы пропустил. Здесь регистр не важен: то, что стоит после
# `/spreadsheets/d/`, — идентификатор по построению, чем бы оно ни выглядело (Ф1 B4).
_INTERNAL_ID_IN_URL_RE = re.compile(
    r"(docs\.google\.com/(?:spreadsheets|document|presentation)/d/"
    r"|drive\.google\.com/(?:drive/)?(?:u/\d+/)?folders/)([A-Za-z0-9_-]{20,})",
    re.IGNORECASE,
)
_INTERNAL_ID_PLACEHOLDER = "‹идентификатор скрыт›"


def redact_internal_ids(text: str) -> tuple[str, int]:
    """Скрыть из текста знания идентификаторы закрытых Google-таблиц/папок. → `(текст, N)`.

    Чистая (без IO) — тестируется инъекцией строки. Возвращает счётчик, а не сам найденный
    идентификатор: в лог уходит «скрыто 3 шт.», а не то, что именно скрыли (Опасная тройка).

    Два прохода: по форме ID (длинная смешанно-регистровая строка — так ID лежат в шапке
    боевого каталога, голыми, без домена) и по КОНТЕКСТУ ссылки (`/spreadsheets/d/<id>` —
    там ID может быть любым). Домен и текст ссылки остаются: скрывается только ключ доступа.
    """
    if not text:
        return "", 0
    out, n = _INTERNAL_ID_RE.subn(_INTERNAL_ID_PLACEHOLDER, text)
    out, n2 = _INTERNAL_ID_IN_URL_RE.subn(rf"\g<1>{_INTERNAL_ID_PLACEHOLDER}", out)
    return out, n + n2


def _read_knowledge_file(root: Path, rel: str) -> tuple[Optional[str], Optional[str]]:
    """Прочитать ОДИН файл знания через все бэкстопы. → `(text, reason)`.

    Ровно один из двух непуст: либо текст (файл допущен), либо причина отказа
    (человекочитаемая — она уходит в WARNING и в видимый сигнал `manifest_report`).
    Тело общее для обоих источников состава — манифеста компании и код-фолбэка,
    чтобы барьеры физически не могли разойтись между путями.

    Слои (порядок важен — от дешёвых к дорогим):
      1. `context_path_block_reason` — ЕДИНАЯ точка правды: денилист / `restricted/` /
         чувствительный паттерн / абсолютный путь / `..`;
      2. резолв ВНУТРИ корня репо (path-traversal);
      3. тот же бэкстоп по РЕАЛЬНОМУ пути после симлинков;
      4. жёсткая ссылка (у неё нет цели — слой 3 бессилен);
      5. пофайловый потолок размера (урезаем, не молча).
    Суммарный бюджет — слой 6, он общий для набора (`apply_total_budget`).
    """
    reason = context_path_block_reason(rel)
    if reason:
        return None, reason
    path = root / rel
    if not _is_within(path, root):
        return None, "путь вне корня репозитория контекста"
    real_reason = _real_path_block_reason(path, root)
    if real_reason:
        return None, f"реальный путь (после симлинков) заблокирован: {real_reason}"
    if not path.is_file():
        return None, "файла нет в репозитории контекста"
    hard_reason = _hardlink_block_reason(path)
    if hard_reason:
        return None, hard_reason
    try:
        data = path.read_bytes()
    except OSError as e:
        return None, f"не прочитать файл ({type(e).__name__})"
    if len(data) > PROTOCOL_CONTEXT_FILE_MAX_BYTES:
        logger.warning(
            "файл знания %s = %d Б > пофайлового потолка %d Б — урезаю (часть не попадёт)",
            rel, len(data), PROTOCOL_CONTEXT_FILE_MAX_BYTES,
        )
        data = data[:PROTOCOL_CONTEXT_FILE_MAX_BYTES]
    try:
        text = data.decode("utf-8", errors="replace").strip()
    except Exception as e:  # noqa: BLE001 — декод не должен ронять сборку
        return None, f"не декодировать файл ({type(e).__name__})"
    if not text:
        return None, "файл пуст"
    # (5.5) БАРЬЕР ПО СОДЕРЖИМОМУ (Ф1 плана `knowledge-content-barrier`). Та же единая
    # точка правды, что и слой 1, — просто теперь у неё есть текст файла.
    #
    # Порядок принципиален: судим ИСХОДНЫЙ текст, ДО `redact_internal_ids`. Редактирование
    # вырезает ID таблиц, а они — один из признаков финансового документа («обвязка вокруг
    # закрытой таблицы учёта»): сначала отредактируй, потом суди — и сам признак исчезнет.
    content_reason = context_path_block_reason(rel, text=text)
    if content_reason:
        return None, content_reason
    # (5.6) Внутренняя навигация: ID закрытых Google-таблиц из шапок файлов знания наружу
    # не отдаём. В лог — только СЧЁТЧИК, не то, что скрыли (Опасная тройка).
    text, hidden = redact_internal_ids(text)
    if hidden:
        logger.info(
            "знание %s: скрыто %d внутренних идентификатор(ов) — во внешний LLM не уйдут", rel, hidden
        )
    return text, None


def load_protocol_knowledge(company: Optional[str]) -> KnowledgeBundle:
    """ГЛАВНАЯ точка входа Ф2: собрать знание компании для сборки протокола.

    Состав берётся из ДВУХ возможных источников (см. `resolve_knowledge_manifest`):
      • манифест компании `knowledge/notary/protocol-knowledge-allowlist.txt` — его
        ведёт КОМАНДА без деплоя кода (R3/R9);
      • нет манифеста → код-фолбэк `PROTOCOL_CONTEXT_WHITELIST` = поведение как до
        Ф2 (каталог WB у МПервый, пусто у Anzhee). РИСК2 плана: манифест мог не
        доехать на VPS (таймер `context_pull` ещё не активирован, ЗАВ1) — тогда
        канон артикулов ISS-21 обязан остаться в промпте, и он остаётся.

    Возвращает `KnowledgeBundle`: каталог и бриф РАЗДЕЛЕНЫ (R5 — «Знание о компании»
    отдельной секцией, чтобы не разбавлять soft-ASR-bias каталога), плюс `rejected`
    — то, что НЕ доехало до промпта, с причиной (видимый сигнал, follow-up 1 из Ф1).

    Бюджет (50 КБ) применяется к каталогу и брифу СУММАРНО и режет с хвоста, поэтому
    каталог всегда идёт первым — иначе при переполнении он выпал бы целиком (ISS-21).

    Опасная тройка (R8): в лог уходят ПУТИ, ПРИЧИНЫ и СЧЁТЧИКИ — текст знания не
    логируется никогда.
    """
    root = repo_root(company)
    if not root:
        return KnowledgeBundle(catalog=[], brief=[], rejected=[], source="none")

    # `missing` — то, что команда забыла вписать (каталог товаров). Кладём его в
    # `rejected` СРАЗУ: именно `rejected` — единственный канал, по которому проблема
    # доходит до человека (Telegram-алерт + `manifest_report` + ненулевой код CLI).
    # Без этого «забытый каталог» виден только в логе VPS, а все три видимые поверхности
    # рапортуют «всё хорошо» — при том что канон артикулов из промпта пропал.
    catalog_rel, brief_rel, source, missing = resolve_knowledge_manifest(company)
    rejected: list[tuple[str, str]] = list(missing)

    # Каталог ВСЕГДА впереди брифа: `apply_total_budget` режет хвост, и порядок
    # здесь — единственное, что защищает канон артикулов от переполнения бюджета.
    read: list[tuple[str, str, str]] = []  # (kind, rel, text)
    for kind, rels in (("catalog", catalog_rel), ("brief", brief_rel)):
        for rel in rels:
            text, reason = _read_knowledge_file(root, str(rel))
            if reason:
                logger.warning("знание НЕ подключено (%s): %s", reason, rel)
                rejected.append((str(rel), reason))
                continue
            read.append((kind, str(rel), text or ""))

    # (6) Суммарный бюджет. `apply_total_budget` сам логирует WARNING; здесь мы
    # ДОБИРАЕМ выпавшее в `rejected`, чтобы человек увидел это не только в логе VPS.
    kept = apply_total_budget([(rel, text) for _kind, rel, text in read])
    kept_map = {rel: text for rel, text in kept}
    for _kind, rel, text in read:
        if rel not in kept_map:
            rejected.append((rel, "не влез в бюджет знания (порядок манифеста = приоритет)"))
        elif len(kept_map[rel]) < len(text):
            rejected.append((rel, "урезан по бюджету знания (в промпт вошла только часть)"))

    catalog = [(rel, kept_map[rel]) for kind, rel, _t in read if kind == "catalog" and rel in kept_map]
    brief = [(rel, kept_map[rel]) for kind, rel, _t in read if kind == "brief" and rel in kept_map]

    total = sum(len(t.encode("utf-8")) for _, t in catalog + brief)
    if catalog or brief:
        logger.info(
            "знание компании %s (источник: %s): каталог=%d, бриф=%d, байт=%d, не подключено=%d "
            "(текст знания не логируется — Опасная тройка)",
            company, source, len(catalog), len(brief), total, len(rejected),
        )
    # Ранний сигнал о бюджете: без него человек узнаёт о потолке только когда файл УЖЕ
    # выпал — то есть один протокол уже собрался обеднённым. Молчим, если выпадение уже
    # случилось: там свой, более громкий WARNING (не дублируем шум).
    if not rejected and total > PROTOCOL_CONTEXT_TOTAL_MAX_BYTES * _BUDGET_WARN_RATIO:
        logger.warning(
            "бюджет знания %s заполнен на %d%% (%d Б из %d Б) — следующий крупный файл "
            "вытеснит хвост манифеста из промпта. Проверь состав заранее",
            company, int(100 * total / max(1, PROTOCOL_CONTEXT_TOTAL_MAX_BYTES)),
            total, PROTOCOL_CONTEXT_TOTAL_MAX_BYTES,
        )
    return KnowledgeBundle(catalog=catalog, brief=brief, rejected=rejected, source=source)


def load_whitelisted_context(company: Optional[str]) -> list[tuple[str, str]]:
    """Совместимость: ВЕСЬ допущенный набор знания одним списком (каталог + бриф).

    Была главной точкой входа до Ф2 (тогда набор = только `PROTOCOL_CONTEXT_WHITELIST`).
    Сейчас — тонкая обёртка над `load_protocol_knowledge`, чтобы существующие
    вызыватели и тесты R6/ISS-21 продолжали работать без правок. Новый код, которому
    важно РАЗДЕЛЕНИЕ каталога и брифа (рендер секций) или список отклонённых файлов,
    должен звать `load_protocol_knowledge` напрямую.
    """
    bundle = load_protocol_knowledge(company)
    return list(bundle.catalog) + list(bundle.brief)


# --- Чтение YAML (lazy import, graceful) --------------------------------------


def _read_yaml(path: Path) -> Optional[dict]:
    """Прочитать YAML-файл в dict. Нет pyyaml / нет файла / битый YAML → None.

    pyyaml импортируется ЛЕНИВО: системный python3 тестов его не несёт (контракт
    §1.2), и отсутствие модуля — это та же graceful degradation, что отсутствие
    файла (бот падать не должен).
    """
    try:
        import yaml  # lazy — см. docstring
    except ImportError:
        logger.info("pyyaml недоступен — знание из %s не читается (degradation)", path)
        return None
    # Защита от заглушки yaml (часть юнит-тестов делает sys.modules["yaml"]=stub):
    # без реального safe_load трактуем как «pyyaml недоступен» (degradation), а не
    # падаем на AttributeError.
    safe_load = getattr(yaml, "safe_load", None)
    if not callable(safe_load):
        logger.info("yaml без safe_load (заглушка?) — знание из %s не читается", path)
        return None
    if not path.is_file():
        logger.info("файл знания не найден, пропуск: %s", path)
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("не прочитать файл знания %s: %s", path, e)
        return None
    try:
        data = safe_load(text)
    except Exception as e:  # noqa: BLE001 — yaml.YAMLError и любой сбой парсера/заглушки
        logger.warning("битый YAML %s: %s — пропуск (degradation)", path, e)
        return None
    if not isinstance(data, dict):
        logger.warning("корень YAML %s не объект (%s) — пропуск", path, type(data).__name__)
        return None
    return data


# --- Глоссарий ----------------------------------------------------------------


def _scope_of(entry: dict) -> str:
    return str(entry.get("scope") or "").strip().lower()


def filter_glossary_by_company(entries: list[dict], company: Optional[str]) -> list[GlossaryEntry]:
    """Чистый company-scope фильтр (контракт §1.5): оставить `scope == company`
    И `scope == cross`. Записи без canonical отбрасываются.

    Пуст без YAML — тестируется инъекцией списка (Bolong[mpfirst] виден в mpfirst,
    не виден в anzhee). company=None → только cross (нет привязки к компании).
    """
    comp = (str(company).strip().lower() if company else "")
    out: list[GlossaryEntry] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        canonical = str(e.get("canonical") or "").strip()
        if not canonical:
            continue
        scope = _scope_of(e)
        if scope == SCOPE_CROSS or (comp and scope == comp):
            out.append(e)  # type: ignore[arg-type]
    return out


def _read_glossary_terms(company: Optional[str]) -> list[dict]:
    """Сырой список `terms` из glossary.yaml репо компании (без company-фильтра).

    Нужен и для company-scope загрузки, и для lint кросс-дублей (видит все scope).
    """
    kdir = knowledge_dir(company)
    if not kdir:
        return []
    data = _read_yaml(kdir / GLOSSARY_FILE)
    if not data:
        return []
    terms = data.get("terms")
    if not isinstance(terms, list):
        return []
    return [t for t in terms if isinstance(t, dict)]


def load_glossary(company: Optional[str]) -> list[GlossaryEntry]:
    """Записи glossary.yaml компании, отфильтрованные по company-scope (§1.5).

    Читает репо ИМЕННО этой компании (anzhee-context для anzhee) — он содержит
    только `scope: anzhee` + `scope: cross`, поэтому `scope: mpfirst` (напр.
    Bolong) сюда не попадает по построению; фильтр — вторая страховка.
    Graceful: нет клона/файла/yaml/битый → `[]`.
    """
    return filter_glossary_by_company(_read_glossary_terms(company), company)


def load_guidance(company: Optional[str]) -> list[str]:
    """Опциональная doc-level секция `guidance` из glossary.yaml компании (Ф8, У1/У2).

    Свободные cross-cutting правила, которые НЕ выражаются записью term→canonical
    (контекстная дизамбигуация, анти-галлюцинация чисел и т.п.) — команда может
    их вести в YAML рядом с терминами. Схема: `guidance:` — список строк. Любой не-
    список / отсутствие / нестроковые элементы → `[]` (graceful, как load_glossary).

    Это НАДСТРОЙКА над стабильным встроенным базисом `glossary.CROSS_CUTTING_GUIDANCE_BLOCK`
    (который рендерится всегда при активном YAML) — потеря базиса при активации YAML
    исключена даже если секция пустая."""
    kdir = knowledge_dir(company)
    if not kdir:
        return []
    data = _read_yaml(kdir / GLOSSARY_FILE)
    if not data:
        return []
    raw = data.get("guidance")
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


# --- Оргструктура (ростеры серий) ---------------------------------------------


def _roles_to_entries(roles: object) -> list[dict]:
    """`roles[*]` YAML → список dict (1-в-1 с `series_roster.RosterEntry`:
    name/domain/keywords). Невалидные/без имени роли отбрасываются."""
    out: list[dict] = []
    if not isinstance(roles, list):
        return out
    for r in roles:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        kws = r.get("keywords")
        keywords = [str(k).strip() for k in kws if str(k).strip()] if isinstance(kws, list) else []
        out.append({
            "name": name,
            "domain": str(r.get("domain") or "").strip(),
            "keywords": keywords,
        })
    return out


def load_org_structure(company: Optional[str]) -> dict[str, list[dict]]:
    """`{slug: [RosterEntry...]}` из org-structure.yaml репо компании.

    Контракт §1.4: `rosters[slug].roles[*]`. Graceful: нет клона/файла/yaml →
    `{}`. Пустой ростер серии (стаб МПервый) → `{slug: []}` (slug известен, ролей
    нет → у потребителя fallback S1+S2+LLM).
    """
    kdir = knowledge_dir(company)
    if not kdir:
        return {}
    data = _read_yaml(kdir / ORG_STRUCTURE_FILE)
    if not data:
        return {}
    rosters = data.get("rosters")
    if not isinstance(rosters, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for slug, spec in rosters.items():
        if not isinstance(slug, str) or not slug.strip():
            continue
        roles = spec.get("roles") if isinstance(spec, dict) else None
        out[slug.strip()] = _roles_to_entries(roles)
    return out


# --- Справочник людей компании (R18, план 2026-06-16) -------------------------


def parse_people(data: object) -> list[PersonEntry]:
    """`people:` YAML-объект → список `PersonEntry` (чистая, без IO).

    Схема (R18): `people:` — список записей `{name, status?, aliases?}`.
      • `name` обязателен и непуст (иначе запись отбрасывается);
      • `status` нормализуется в lowercase; всё, что не `inactive`, считается
        `active` — бэкомпат: СТАРАЯ запись без `status` = активный участник, и
        неизвестное значение не «выключает» человека по ошибке;
      • `aliases` — список непустых строк (нет/не список → `[]`).

    Принимает как весь dict org-structure.yaml (берёт ключ `people`), так и уже
    извлечённый список — удобно тестам инъекцией без YAML (как
    `filter_glossary_by_company`).
    """
    if isinstance(data, dict):
        raw = data.get("people")
    else:
        raw = data
    if not isinstance(raw, list):
        return []
    out: list[PersonEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        status_raw = str(item.get("status") or "").strip().lower()
        status = PERSON_STATUS_INACTIVE if status_raw == PERSON_STATUS_INACTIVE else PERSON_STATUS_ACTIVE
        aliases_raw = item.get("aliases")
        aliases = (
            [str(a).strip() for a in aliases_raw if str(a).strip()]
            if isinstance(aliases_raw, list)
            else []
        )
        out.append({"name": name, "status": status, "aliases": aliases})
    return out


def load_people(company: Optional[str]) -> list[PersonEntry]:
    """Справочник людей компании из `people:` org-structure.yaml. Graceful: нет
    клона/файла/yaml/секции → `[]` (как `load_org_structure`)."""
    kdir = knowledge_dir(company)
    if not kdir:
        return []
    data = _read_yaml(kdir / ORG_STRUCTURE_FILE)
    if not data:
        return []
    return parse_people(data)


def inactive_person_names(company: Optional[str]) -> set[str]:
    """Множество ИМЁН (канон + алиасы) людей со статусом `inactive` у компании.

    Семантика `inactive`: человек УШЁЛ из компании / вообще не бывает на встречах
    серии (постоянное отсутствие). РАЗОВОЕ отсутствие активного человека на КОНКРЕТНОЙ
    встрече — это НЕ inactive, а слой R9 (negative-roster текущей встречи).

    Для сужения `name_pool` LLM-добивки и чистки ожидаемых (R4/R5): такого ушедшего
    человека автором не подставляем. Алиасы включены, чтобы короткое/искажённое
    написание неактивного тоже отсеялось. Сравнение СТРОГОЕ (точное, case-insensitive)
    — НЕ тёзко-первословное: иначе bare «Михаил» отсёкся бы как однофамильный
    неактивный тёзка и затёр присутствующего активного Саргина."""
    out: set[str] = set()
    for p in load_people(company):
        if p.get("status") != PERSON_STATUS_INACTIVE:
            continue
        name = str(p.get("name") or "").strip()
        if name:
            out.add(name.lower())
        for a in p.get("aliases") or []:
            al = str(a).strip()
            if al:
                out.add(al.lower())
    return out


def is_name_inactive(name: Optional[str], company: Optional[str]) -> bool:
    """Имя принадлежит неактивному человеку компании (строгое точное совпадение по
    канону/алиасу). Пустое имя / неизвестная компания → False (инертно)."""
    n = (str(name).strip().lower() if name else "")
    if not n:
        return False
    return n in inactive_person_names(company)


def company_for_series(series_slug: Optional[str]) -> Optional[str]:
    """Компания серии по её slug — поиск slug по оргструктурам всех компаний.

    До Ф6 (поле `company` в разметке серии) это единственный источник привязки
    серии к компании. Slug уникален (папка серии) → первое совпадение корректно.
    Нет совпадения / нет YAML → None (вызыватель уходит в fallback на встроенное).
    """
    if not series_slug or not str(series_slug).strip():
        return None
    slug = str(series_slug).strip()
    for company in known_companies():
        if slug in load_org_structure(company):
            return company
    return None


def roster_for_series(series_slug: Optional[str]) -> list[dict]:
    """Ростер серии из оргструктуры (поиск по всем компаниям). Нет → `[]`.

    Тело `series_roster.get_roster` (ЗАВ1): источник `_STATIC_ROSTERS` → этот
    YAML-поиск; при пустом результате get_roster падает на встроенный хардкод
    (graceful, поведение как до Ф5).
    """
    if not series_slug or not str(series_slug).strip():
        return []
    slug = str(series_slug).strip()
    for company in known_companies():
        os_map = load_org_structure(company)
        if slug in os_map and os_map[slug]:
            return os_map[slug]
    return []


# --- Lint синхронности кросс-дублей (контракт §1.5) ---------------------------


def _cross_signature(entries: list[dict]) -> dict[str, list[str]]:
    """{canonical: sorted(aliases)} для записей scope==cross. Ключ дедупа — для
    сверки, что набор кросс-терминов идентичен в обоих репо."""
    sig: dict[str, list[str]] = {}
    for e in entries:
        if _scope_of(e) != SCOPE_CROSS:
            continue
        canonical = str(e.get("canonical") or "").strip()
        if not canonical:
            continue
        aliases = e.get("aliases")
        al = sorted(str(a).strip() for a in aliases if str(a).strip()) if isinstance(aliases, list) else []
        sig[canonical] = al
    return sig


def lint_cross_sync_signatures(by_company: dict[str, list[dict]]) -> list[str]:
    """Чистый lint (без YAML): набор `cross`-записей во всех компаниях обязан
    совпадать по `canonical`+`aliases`. Возвращает список расхождений (пусто = ОК).

    Принимает {company: [raw terms]} — тестируется инъекцией. Боевую обёртку даёт
    `lint_cross_sync()` (читает YAML каждого репо).
    """
    companies = list(by_company.keys())
    if len(companies) < 2:
        return []
    sigs = {c: _cross_signature(by_company[c]) for c in companies}
    base_company = companies[0]
    base = sigs[base_company]
    problems: list[str] = []
    for other in companies[1:]:
        other_sig = sigs[other]
        for canonical in sorted(set(base) | set(other_sig)):
            in_base = canonical in base
            in_other = canonical in other_sig
            if in_base and not in_other:
                problems.append(f"cross-термин «{canonical}» есть в {base_company}, нет в {other}")
            elif in_other and not in_base:
                problems.append(f"cross-термин «{canonical}» есть в {other}, нет в {base_company}")
            elif base.get(canonical) != other_sig.get(canonical):
                problems.append(
                    f"cross-термин «{canonical}»: aliases расходятся "
                    f"({base_company}={base.get(canonical)} vs {other}={other_sig.get(canonical)})"
                )
    return problems


def lint_cross_sync() -> list[str]:
    """Боевой lint: читает glossary.yaml каждого известного репо и сверяет
    кросс-дубли. Нет YAML у репо → его кросс-набор пуст (рассинхрон всплывёт)."""
    by_company = {c: _read_glossary_terms(c) for c in known_companies()}
    # Репо, у которых файл вовсе не читается (нет клона/pyyaml), из сверки
    # исключаем — иначе degradation выглядит как рассинхрон. Сверяем только те,
    # где термины реально прочитаны.
    present = {c: terms for c, terms in by_company.items() if terms}
    if len(present) < 2:
        return []
    return lint_cross_sync_signatures(present)
