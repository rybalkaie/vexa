# -*- coding: utf-8 -*-
"""Ф4 (R15/R16): точечная пересборка протокола по правке авторства/имени.

Корень проблемы. Перевыпуск по правке регенерил протокол из транскрипта ЦЕЛИКОМ
(`feedback_reissue.reissue_one` → `generate_protocol`) → плыл весь текст (жалоба
владельца «переписывает всё, не только правку»). Ф3 застабилизировала
механически-нормализуемый дрейф (порядок участников/секций — `stabilize_protocol_text`),
но прозу LLM всё равно переписывала заново. Ф4 добивает остаток до по-настоящему
ТОЧЕЧНОЙ правки.

Механизм. Для ПРАВОК АВТОРСТВА/ИМЕНИ (есть детерминированный name-remap из
`parse_authorship_remap`, нет контентных правок) НЕ регенерируем — берём СТАРЫЙ
протокол и подменяем имя точечно. Масштаб подмены — по тексту обратной связи (R15):
  • broad  («вообще перепутал человека»; дефолт name-фикса) → ВСЕ вхождения имени;
  • narrow («эту реплику не тому приписал»)                 → ОДНО вхождение в теле.
Весь остальной текст — побайтно как был.

Инварианты (R16). После подмены проверяем, что целы: якорь `#протоколвстречи`,
⚠️-пометки (вкл. дисклеймер авторства R3 — он тоже ⚠️/ℹ️-врезка), markdown-структура
(число `## `-секций). Не прошло — механизм ОТКАЗЫВАЕТСЯ (возвращает None) и caller
честно падает на обычную регенерацию (без регресса поведения).

Опасная тройка (CLAUDE.md проекта). Модуль ЧИСТО ТЕКСТОВЫЙ: без LLM, без сети, без
логирования текста реплик. Правки приходят сюда НЕ как промпт, а как уже-распознанный
детерминированный remap имён → anti-injection-поверхность нулевая (сильнее, чем
anti-injection-рамка LLM-пути: тут просто нет модели, которую можно отравить).

stdlib-only — модуль исполняется в reissue/listener-контексте (системный python3.9
без venv). Константы-инварианты задаём локально и СВЕРЯЕМ с источником тестом
(`test_constants_match_source`), чтобы не тянуть тяжёлый `llm_postprocess`/`protocol_to_tg`
ради трёх строк и при этом ловить дрейф значений.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("notary.targeted_edit")

# --- Инварианты (R16). Локальные зеркала; сверяются с источником тестом. -------
# Источники: llm_postprocess.REVIEW_FLAG_MARKER ("⚠️"),
#            llm_postprocess._HEADER_ANCHOR / generate_protocol ("#протоколвстречи"),
#            protocol_to_tg.PROTOCOL_DISCLAIMER_SENTINEL ("Авторство реплик").
PROTOCOL_ANCHOR = "#протоколвстречи"
REVIEW_FLAG_MARKER = "⚠️"
DISCLAIMER_SENTINEL = "Авторство реплик"

# Строка участников протокола. Толерантна к обрамлению (`**Участники:**`,
# `*Участники:*`, `Участники:`) — как распознаёт источник `_PARTICIPANTS_LINE_RE`
# в llm_postprocess (`^(\s*\*{0,2}\s*Участники:\*{0,2}\s*)(.*)$`): narrow НЕ должен
# под-распознать её и сменить имя УЧАСТНИКА вместо реплики (раньше зеркало было
# строже источника). Сверяется с источником `ParticipantsLineDriftTest` (защита от
# дрейфа формата при merge upstream — раздел README про обязательную проверку).
_PARTICIPANTS_LINE_RE = re.compile(r"^\s*\*{0,2}\s*Участники:", re.UNICODE)

# --- Сигналы масштаба правки (R15). По тексту обратной связи. ------------------
# narrow — правка точечная, про одну реплику/место; broad — про человека целиком.
# Дефолт при name-remap — broad: типичный фикс имени («не A, а B») означает, что
# человек неверен везде (боевой инцидент 16.06 Еремеев→Саргин — ровно broad).
_NARROW_SIGNALS = (
    "эту реплику", "эту фразу", "это сообщение", "эту строку", "этот пункт",
    "в этом пункте", "одну реплику", "одну фразу", "именно эту", "именно здесь",
    "вот эту", "вот тут", "вот здесь", "только здесь", "только эту", "в этом месте",
    "в этой реплике", "конкретно эту", "конкретно здесь",
)
_BROAD_SIGNALS = (
    "вообще", "везде", "всюду", "повсюду", "целиком", "полностью", "во всех",
    "каждый раз", "постоянно", "перепутал человека", "перепутали человека",
    "не тот человек", "вообще не тот", "по всему", "по всей встрече", "всю встречу",
    "это другой человек", "совсем другой",
)


def classify_edit_scope(edit_texts: list[str]) -> str:
    """Масштаб правки по тексту обратной связи: "narrow" | "broad".

    narrow только при явном узком сигнале («эту реплику», «в этом пункте»…).
    Иначе — broad (дефолт name-фикса: человек неверен везде). Сигналы ищем по
    схлопнутому пробелами casefold-тексту, чтобы «эту   Реплику» тоже ловилось.
    """
    blob = " ".join(t for t in (edit_texts or []) if t)
    blob = re.sub(r"\s+", " ", blob).strip().casefold()
    if not blob:
        return "broad"
    has_narrow = any(sig in blob for sig in _NARROW_SIGNALS)
    has_broad = any(sig in blob for sig in _BROAD_SIGNALS)
    # Явный broad-сигнал перебивает narrow («вообще перепутал, вот эту тоже» = broad).
    if has_broad:
        return "broad"
    if has_narrow:
        return "narrow"
    return "broad"


def _compile_name_alt(keys) -> Optional["re.Pattern"]:
    """Альтернация имён с границами слова (своп-безопасная замена за один проход).

    Длинные ключи раньше коротких («Михаил Еремеев» матчится раньше «Михаил»),
    чтобы полное имя не разорвалось на токены. Границы `(?<!\\w)`/`(?!\\w)` —
    Unicode-aware для str в Python re (кириллица = `\\w`), поэтому «Илья» не
    матчит «Ильяс», а «**Илья Рыбалка**» (обрамление `*`, не-`\\w`) — матчит.
    """
    ordered = sorted({k for k in keys if k}, key=len, reverse=True)
    if not ordered:
        return None
    alt = "|".join(re.escape(k) for k in ordered)
    return re.compile(r"(?<!\w)(" + alt + r")(?!\w)")


def _subst_all(text: str, remap: dict, pattern: "re.Pattern") -> tuple[str, int]:
    """Замена ВСЕХ вхождений имён за ОДИН проход (своп-безопасно).

    Один `sub` с функцией-резолвером: каждое вхождение независимо берётся из
    remap → своп A↔B не схлопывается (в отличие от последовательных replace).
    Возвращает (новый_текст, число_замен).
    """
    n = 0

    def _repl(m: "re.Match") -> str:
        nonlocal n
        new = remap.get(m.group(1))
        if new is None:
            return m.group(0)
        n += 1
        return new

    return pattern.sub(_repl, text), n


def _is_skippable_for_narrow(line: str) -> bool:
    """Строки, которые narrow НЕ трогает: якорь, участники, дисклеймер.

    narrow меняет одну КОНТЕНТНУЮ реплику/место; шапочные/служебные/структурные
    строки (где имя — это роль участника или часть заголовка секции, а не
    приписанная реплика) не его цель. Жирный заголовок-владелец задач (`**Имя**`)
    НЕ пропускаем — это адресная атрибуция, валидная цель «не тому приписал».
    """
    s = line.strip()
    if s.startswith(PROTOCOL_ANCHOR):
        return True
    if s.startswith("## "):  # заголовок секции — структура, не реплика
        return True
    if _PARTICIPANTS_LINE_RE.match(line):
        return True
    if DISCLAIMER_SENTINEL in line:
        return True
    return False


def apply_targeted_name_edit(
    old_protocol: str, remap: dict, scope: str,
) -> Optional[str]:
    """Точечная подмена имени в СТАРОМ протоколе. None → caller падает на регенерацию.

    `remap` — `{старое_имя: новое_имя}` ПОЛНЫМИ отображаемыми именами (как их
    отдаёт `parse_authorship_remap`: ключи/значения — полные имена спикеров или
    «Спикер N»). Подменяем только те ключи, что РЕАЛЬНО встречаются в протоколе
    (полным токеном) — «Спикер N»-ключи, которых в синтезированном протоколе нет,
    тихо отсеиваются. Не осталось применимых ключей / результат == исходник →
    None (нечего точечно править, пусть решает регенерация).

    scope (R15):
      • "broad"  → все вхождения каждого применимого имени;
      • "narrow" → ОДНО вхождение, в первой подходящей КОНТЕНТНОЙ строке тела
        (шапка/участники/дисклеймер пропускаются — см. `_is_skippable_for_narrow`).
    """
    if not old_protocol or not remap:
        return None
    # Оставляем только ключи, реально присутствующие в протоколе полным токеном.
    present = {}
    for k, v in remap.items():
        if not k or not v or k == v:
            continue
        pat = _compile_name_alt([k])
        if pat is not None and pat.search(old_protocol):
            present[k] = v
    if not present:
        return None
    pattern = _compile_name_alt(present.keys())
    if pattern is None:
        return None

    if scope == "narrow":
        lines = old_protocol.split("\n")
        changed = False
        for i, line in enumerate(lines):
            if _is_skippable_for_narrow(line):
                continue
            if not pattern.search(line):
                continue
            # Ровно ОДНО вхождение в этой строке (count=1) → «одна реплика».
            new_line, n = _subst_all_count1(line, present, pattern)
            if n:
                lines[i] = new_line
                changed = True
                break
        if not changed:
            return None
        new_text = "\n".join(lines)
    else:  # broad
        new_text, n = _subst_all(old_protocol, present, pattern)
        if not n:
            return None

    if new_text == old_protocol:
        return None
    return new_text


def _subst_all_count1(text: str, remap: dict, pattern: "re.Pattern") -> tuple[str, int]:
    """Как `_subst_all`, но не более ОДНОЙ замены (для narrow)."""
    n = 0

    def _repl(m: "re.Match") -> str:
        nonlocal n
        if n >= 1:
            return m.group(0)
        new = remap.get(m.group(1))
        if new is None:
            return m.group(0)
        n += 1
        return new

    return pattern.sub(_repl, text), n


# --- Инварианты (R16) ---------------------------------------------------------

def protocol_invariants(text: str) -> dict:
    """Снимок структурных инвариантов протокола для сравнения до/после правки."""
    text = text or ""
    return {
        "anchor": bool(re.search(r"^#протоколвстречи\b", text, flags=re.MULTILINE)),
        "warn_count": text.count(REVIEW_FLAG_MARKER),
        "disclaimer": DISCLAIMER_SENTINEL in text,
        "heading_count": sum(1 for ln in text.split("\n") if ln.startswith("## ")),
        "line_count": text.count("\n"),
    }


def invariants_preserved(old_inv: dict, new_inv: dict) -> tuple[bool, str]:
    """Проверка R16: точечная правка не должна терять инварианты.

    Якорь не пропал, ни одной ⚠️ не убыло (дисклеймер авторства R3 — тоже
    врезка, входит в общий счёт/проверку sentinel), дисклеймер на месте, число
    `## `-секций и строк не изменилось (подмена имени не добавляет/не удаляет
    строк и секций). Возвращает (ok, причина-если-нет).
    """
    if old_inv.get("anchor") and not new_inv.get("anchor"):
        return False, "потерян якорь #протоколвстречи"
    if new_inv.get("warn_count", 0) < old_inv.get("warn_count", 0):
        return False, (
            f"убыло ⚠️-пометок: было {old_inv.get('warn_count')} "
            f"стало {new_inv.get('warn_count')}"
        )
    if old_inv.get("disclaimer") and not new_inv.get("disclaimer"):
        return False, "потерян дисклеймер авторства (R3)"
    if new_inv.get("heading_count") != old_inv.get("heading_count"):
        return False, (
            f"изменилось число секций: было {old_inv.get('heading_count')} "
            f"стало {new_inv.get('heading_count')}"
        )
    if new_inv.get("line_count") != old_inv.get("line_count"):
        return False, (
            f"изменилось число строк: было {old_inv.get('line_count')} "
            f"стало {new_inv.get('line_count')}"
        )
    return True, ""


def diff_is_bounded(
    old_text: str, new_text: str, remap: dict, scope: str,
) -> tuple[bool, str]:
    """РИСК5: контроль объёма изменений диффом — изменились ТОЛЬКО имя-строки.

    Построчно: число строк совпадает (подмена имени строк не добавляет); каждая
    изменившаяся строка обязана отличаться РОВНО подменой имени (применяем тот же
    remap к старой строке → должно дать новую). Любая иная изменившаяся строка =
    выход за рамки точечной правки → отказ. Для narrow дополнительно: не более
    одной изменившейся строки.
    """
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    if len(old_lines) != len(new_lines):
        return False, "изменилось число строк (не точечная правка)"
    pattern = _compile_name_alt(remap.keys())
    if pattern is None:
        return False, "пустой remap"
    changed = 0
    for o, n in zip(old_lines, new_lines):
        if o == n:
            continue
        changed += 1
        # Строка изменилась ИМЕННО подменой имени? (broad — все вхождения, narrow —
        # одно; проверяем, что хотя бы одно из преобразований воспроизводит new).
        cand_all, _ = _subst_all(o, remap, pattern)
        cand_one, _ = _subst_all_count1(o, remap, pattern)
        if n not in (cand_all, cand_one):
            # Опасная тройка: НЕ кладём текст строки в reason (он логируется
            # caller'ом) — только факт выхода за рамки, без содержимого протокола.
            return False, "строка изменилась не только подменой имени"
    if changed == 0:
        return False, "ничего не изменилось"
    if scope == "narrow" and changed > 1:
        return False, f"narrow затронул {changed} строк (ожидалась одна)"
    return True, ""


# Лейбл-слова (не distinctive личное имя) — их остаток НЕ считаем «неполной заменой».
_NON_DISTINCTIVE_TOKENS = frozenset({"спикер", "speaker", "участник", "участники"})


def _broad_replacement_incomplete(new_text: str, oneway: dict) -> bool:
    """Эвристика: broad оставил склонённую форму старого имени → замена НЕПОЛНА.

    broad подменяет ПОЛНОЕ имя в номинативе, но склонённые формы в прозе («поручили
    Михаилу Еремееву», «с Еремеевым») не матчатся word-boundary'ом полного имени —
    иначе доставился бы протокол со СМЕСЬЮ старого и нового имени (ровно жалоба
    «не до конца исправил»). Морфология — вне scope Ф4 (§5 плана): здесь НЕ склоняем,
    а ДЕТЕКТИМ неполноту. Если distinctive-токен СТАРОГО имени (≥4 букв, алфавитный,
    не лейбл-слово, не общий с новым именем — напр. фамилия «Еремеев») уцелел
    подстрокой (ловит «Еремееву»/«Еремееве») → True → caller честно регенерирует
    (LLM применит правку со склонениями).

    Направление безопасно: ложное срабатывание (тёзка-фамилия у другого человека)
    даёт лишь ЛИШНЮЮ регенерацию — корректный (хоть и «плывущий») протокол, до-Ф4
    поведение — но НИКОГДА неверный текст. Не полная морфология (first-name-склонение
    при общей фамилии не ловит) — узкий безопасный сетап под главный инцидент-класс
    (смена фамилии). Полное решение — деклинатор, §5 бэклога.
    """
    low = (new_text or "").casefold()
    for old_name, new_name in (oneway or {}).items():
        new_toks = {t.casefold() for t in (new_name or "").split()}
        for tok in (old_name or "").split():
            tl = tok.casefold()
            if (
                len(tl) >= 4
                and tok[0].isalpha()
                and tl not in new_toks
                and tl not in _NON_DISTINCTIVE_TOKENS
                and tl in low
            ):
                return True
    return False


def targeted_name_reissue(
    old_protocol: str, remap: dict, edit_texts: list[str],
) -> Optional[tuple[str, dict]]:
    """Высокоуровневый вход для `reissue_one`: точечный перевыпуск или None.

    Алгоритм: масштаб по тексту правки → подмена имени в старом протоколе →
    проверка инвариантов (R16) и объёма диффа (РИСК5). Любой провал → None
    (caller честно падает на обычную регенерацию, без регресса).

    Возвращает (новый_протокол, meta) либо None. meta: {scope, n_changes}.
    Текст реплик/правок НЕ логируем (опасная тройка) — только масштаб/счётчики.

    ГРАНИЦА (важно): точечный путь берёт ТОЛЬКО односторонние переименования
    (A→B, где B сам не является ключом remap — т.е. известная корректная личность
    замещает неверную, как боевой инцидент 16.06 Еремеев→Саргин и R15 «перепутал
    человека»). СВОП (A↔B, взаимная путаница спикеров) оставляем проверенному пути
    Ф4б (remap транскрипта + регенерация): своп в синтезированном протоколе тоньше
    прямой подмены имени, а его ретрай-безопасность/без-tmp-течь там уже покрыты.
    ЛЮБОЙ своп в наборе (в т.ч. в СМЕСИ с односторонними) → None: иначе частичное
    применение «только односторонней части» молча потеряло бы своп из ДОСТАВЛЯЕМОГО
    протокола (он ушёл бы лишь в транскрипт), хотя владелец просил и его. caller
    честно регенерирует — там своп И переименование применяются вместе (без регресса Ф4б).
    """
    if not old_protocol or not remap:
        return None
    # Своп где-либо в наборе → точечный путь НЕ применим целиком. Частичное
    # применение только односторонней части пропустило бы регенерацию (см. caller),
    # и запрошенный своп не попал бы в доставленный протокол. Любой своп → None.
    has_swap = any(
        k and v and k != v and remap.get(v) == k for k, v in remap.items()
    )
    if has_swap:
        return None
    oneway = {k: v for k, v in remap.items() if k and v and k != v}
    if not oneway:
        return None
    scope = classify_edit_scope(edit_texts)
    new_text = apply_targeted_name_edit(old_protocol, oneway, scope)
    if new_text is None:
        return None

    # Ф4 (ход3): broad обязан убрать СТАРОЕ имя целиком. Уцелевшая склонённая форма
    # (морфология — вне scope, §5) → доставили бы СМЕСЬ имён → честный фолбэк на
    # регенерацию (без неверного текста; в худшем — лишняя регенерация).
    if scope == "broad" and _broad_replacement_incomplete(new_text, oneway):
        logger.info(
            "[targeted-edit] broad-замена неполна (склонённая форма?) → фолбэк регенерация"
        )
        return None

    inv_ok, reason = invariants_preserved(
        protocol_invariants(old_protocol), protocol_invariants(new_text)
    )
    if not inv_ok:
        logger.info("[targeted-edit] инвариант нарушен (%s) → фолбэк регенерация", reason)
        return None

    bound_ok, breason = diff_is_bounded(old_protocol, new_text, oneway, scope)
    if not bound_ok:
        logger.info("[targeted-edit] дифф вне рамок (%s) → фолбэк регенерация", breason)
        return None

    # Число изменившихся строк — для лога/телеметрии (без текста).
    n_changes = sum(
        1 for o, n in zip(old_protocol.split("\n"), new_text.split("\n")) if o != n
    )
    return new_text, {"scope": scope, "n_changes": n_changes}
