"""Ф5 (R17/R18/R19): детерминированная кросс-проверка вокативов в разметке спикеров.

Страховка ПОВЕРХ LLM-маппера имён (`llm_postprocess.map_speaker_names`). Маппер иногда
принимает ОБРАЩЕНИЕ по имени за самопредставление говорящего: реплику «Привет, Тань»
произносит НЕ Татьяна (она зовёт Таню), а кто-то другой. Если после разметки спикер,
подписанный именем N, в СВОИХ репликах обращается по имени к N — это почти наверняка
путаница людей. Такую разметку помечаем НЕуверенной, называя КОНКРЕТНЫЕ спорные имена
(решение владельца УТР1: помечать, НЕ переспрашивать).

Инвариант R18: «A зовёт B по имени ⇒ A ≠ B». Нарушение (спикер зовёт сам себя) →
разметка спорная.

Работает БЕЗ pymorphy/сети: словарь уменьшительных форм + извлечение обращений
регэкспами. Причина — тесты офлайн, а `.venv-cli` на маке pymorphy3 не имеет (на VPS
есть, но опираться на него в детерминированной страховке нельзя — деградировала бы молча).

🔒 Опасная тройка (ПДн): функции получают тексты реплик как ВХОД, но САМИ ничего не
логируют и наружу отдают только имена/пары и счётчики. Вызывающий (`llm_postprocess`)
пишет в лог лишь ЧИСЛО спорных пар, без текста реплик и без списка имён.
"""

from __future__ import annotations

import re
from typing import Optional

# ── Словарь уменьшительных (диминутивов/гипокористик) ────────────────────────
# Канон полного имени → множество форм-обращений (lowercase, ё→е), которыми к
# этому человеку обращаются. Включая усечённо-звательные («Тань», «Машунь»), к
# которым pymorphy бесполезен. НЕ исчерпывающий справочник русских имён — покрывает
# частые имена и, главное, участников серий Ильи (Ольга, Мария, Михаил, Татьяна…).
# РИСК5: неоднозначные формы («саш» → и Александр, и Александра) намеренно попадают
# в ДВЕ группы — так `resolve_address` вернёт множество >1 и решение уйдёт в
# «неуверенно», а не в угадывание.
_CANON_TO_FORMS: dict[str, set[str]] = {
    "Александр": {"саша", "саш", "сань", "саня", "шура", "шурик", "сашка", "сашенька", "алекс"},
    "Александра": {"саша", "саш", "сань", "саня", "шура", "сашка", "сашенька", "аля", "алекс"},
    "Алексей": {"леша", "леш", "леха", "лешенька", "алекс", "алеша", "алеш"},
    "Анастасия": {"настя", "насть", "настенька", "настюша", "ася"},
    "Анна": {"аня", "ань", "анюта", "анечка", "нюра", "нюша", "анюша"},
    "Артем": {"тема", "тем", "темка", "артемка", "темыч"},
    "Борис": {"боря", "борь", "боренька", "борян"},
    "Валентина": {"валя", "валь", "валечка", "валюша"},
    "Валерий": {"валера", "валер", "валерка", "лера"},
    "Виктор": {"витя", "вить", "витенька", "витек", "витька"},
    "Виктория": {"вика", "вик", "викуся", "викуша", "вита"},
    "Владимир": {"вова", "вов", "володя", "володь", "вовка", "вовчик", "влад"},
    "Дарья": {"даша", "даш", "дашенька", "дашуня", "дарьюшка"},
    "Дмитрий": {"дима", "дим", "димон", "димка", "митя", "мить", "димочка"},
    "Евгений": {"женя", "жень", "женечка", "жека", "евгеша"},
    "Евгения": {"женя", "жень", "женечка", "женя"},
    "Екатерина": {"катя", "кать", "катюша", "катенька", "катька"},
    "Елена": {"лена", "лен", "леночка", "аленка", "аленушка", "ленка"},
    "Иван": {"ваня", "вань", "ванюша", "ванька", "иванушка"},
    "Игорь": {"игореша", "игорек", "гарик", "игорьок"},
    "Илья": {"илюша", "илюш", "ильюша", "илюшка", "ильюшенька"},
    "Ирина": {"ира", "ир", "иринка", "ируся", "иришка", "ириша"},
    "Кирилл": {"кира", "кирюша", "кирюх", "кирюха", "кирилка"},
    "Константин": {"костя", "кость", "костенька", "костян", "котик"},
    "Ксения": {"ксюша", "ксюш", "ксю", "ксенька", "ксюха"},
    "Лариса": {"лара", "лар", "ларочка", "лариска"},
    "Любовь": {"люба", "люб", "любаша", "любочка", "любка"},
    "Людмила": {"люда", "люд", "людочка", "мила", "люся", "людмилка"},
    "Марина": {"марина", "марин", "мариша", "маринка", "мариночка"},
    "Мария": {"маша", "маш", "машунь", "машуня", "маня", "мань", "манечка",
              "машенька", "маруся", "мари", "муся", "машка"},
    "Михаил": {"миша", "миш", "мишенька", "мишаня", "мишка", "мишутка", "миха"},
    "Надежда": {"надя", "надь", "наденька", "надюша", "надюшка"},
    "Наталья": {"наташа", "наташ", "натали", "ната", "наташенька", "наталка"},
    "Николай": {"коля", "коль", "николаша", "колян", "коленька", "колька"},
    "Олег": {"олежка", "олежек", "олеж", "олегушка"},
    "Ольга": {"оля", "оль", "олечка", "оленька", "олюшка", "олька"},
    "Павел": {"паша", "паш", "пашенька", "павлик", "пашка"},
    "Петр": {"петя", "петь", "петенька", "петруша", "петька"},
    "Роман": {"рома", "ром", "ромка", "ромчик", "романыч"},
    "Светлана": {"света", "свет", "светочка", "светик", "светка"},
    "Сергей": {"сережа", "сереж", "сереженька", "серега", "серый", "серж", "серж"},
    "Станислав": {"стас", "стасик", "стася", "станиславчик"},
    "Степан": {"степа", "степ", "степашка", "стёпка"},
    "Татьяна": {"таня", "тань", "танюша", "танюш", "танечка", "тата", "танюшка", "танька"},
    "Юлия": {"юля", "юль", "юленька", "юлечка", "юлька"},
    "Юрий": {"юра", "юр", "юрочка", "юрик", "юрка"},
    "Яна": {"яночка", "янка", "янчик"},
}


def _norm(s: str) -> str:
    """Нормализует имя/токен для сравнения: lower, ё→е, обрезка пробелов/пунктуации."""
    s = (s or "").strip().lower().replace("ё", "е")
    return s.strip(" \t\r\n.,!?;:«»\"'()-–—")


# form(normalized) → set(канон). Строится один раз; включает и сам канон
# (номинатив-обращение «Михаил, посмотри» → Михаил).
def _build_form_index() -> dict[str, set[str]]:
    idx: dict[str, set[str]] = {}
    for canon, forms in _CANON_TO_FORMS.items():
        allforms = set(forms) | {_norm(canon)}
        for f in allforms:
            fn = _norm(f)
            if not fn:
                continue
            idx.setdefault(fn, set()).add(canon)
    return idx


_FORM_INDEX = _build_form_index()

# Все известные формы + каноны (для быстрого «это вообще может быть именем?»).
_ALL_FORMS: frozenset[str] = frozenset(_FORM_INDEX.keys())

# Номинативы (полные каноны в нижнем регистре) — чтобы отличать усечённо-звательные
# формы («тань») от полного имени в номинативе («татьяна»). Усечённые почти никогда
# не бывают подлежащим — по ним детект точнее.
_CANON_LOWER: frozenset[str] = frozenset(_norm(c) for c in _CANON_TO_FORMS)

# Усечённые формы, СОВПАДАЮЩИЕ (после ё→е) с частыми обычными словами: «Тёма/Тём»→
# «тема»/«тем» (тема разговора / «тем более»), «Серый»→серый цвет, «Петь»→петь,
# «Мила»→мила, «Вить»→вить, «Мишка»→мишка (игрушка). На нарративе они дали бы ложное
# само-обращение (спикер «Артём», сказавший «тема закрыта», выглядел бы как «зовёт
# сам себя»). Поэтому их берём в обращения ТОЛЬКО в звательной позиции (приветствие/
# имя+запятая), как полные номинативы, а не «везде» (ход1 цикла5).
_COMMON_WORD_FORMS: frozenset[str] = frozenset(
    {"тема", "тем", "серый", "петь", "мила", "вить", "мишка"}
)

_SPEAKER_PLACEHOLDER_RE = re.compile(r"^спикер\s*\d+$", re.IGNORECASE)

# Строка транскрипта: `**[HH:MM] Имя:**  текст` (совместимо с
# llm_postprocess._TRANSCRIPT_SPEAKER_RE).
_SPEAKER_LINE_RE = re.compile(r"^\s*\*\*\[\d{2}:\d{2}(?::\d{2})?\]\s+(.+?):\*\*\s*(.*)$")

# Слова-приветствия/зачины, после которых почти наверняка идёт ОБРАЩЕНИЕ по имени.
_GREETING_RE = (
    r"привет|здравствуй(?:те)?|здравствуй|здорово|салют|доброе\s+утро|добрый\s+день|"
    r"добрый\s+вечер|слушай(?:те)?|слуш|скажи(?:те)?|смотри(?:те)?|глянь(?:те)?|"
    r"спасибо|благодарю|извини(?:те)?|прости(?:те)?|эй|ало|алло|коллеги"
)

# Кандидат-токен имени: слово из кириллицы (2+ буквы). Границы — не-буквенные.
_WORD_RE = re.compile(r"[а-яёА-ЯЁ][а-яёА-ЯЁ-]+")


def is_placeholder_label(label: str) -> bool:
    """`Спикер N` (не разрешённое имя) — по такому конфликта авторства нет."""
    return bool(_SPEAKER_PLACEHOLDER_RE.match((label or "").strip()))


def _first_name_token(name: str) -> str:
    """Первый токен отображаемого имени: «Илья Рыбалка» → «Илья»."""
    n = (name or "").strip()
    if not n:
        return ""
    return n.split()[0]


def _same_person_form(token: str, canon_first: str) -> str:
    """Может ли форма-обращение `token` адресоваться человеку с каноничным первым
    именем `canon_first`?  Возвращает: "yes" | "no" | "ambiguous".

    "ambiguous" — форма попадает в 2+ группы канонов (напр. «саш» → Александр И
    Александра): совпадает с нашим каноном, но однозначности нет (РИСК5).
    """
    t = _norm(token)
    c = _norm(canon_first)
    if not t or not c:
        return "no"
    if t == c:
        return "yes"
    canon_set = _FORM_INDEX.get(t)
    if not canon_set:
        return "no"
    # Каноны в индексе — в TitleCase; сравниваем по нормали.
    matched = {cc for cc in canon_set if _norm(cc) == c}
    if not matched:
        return "no"
    return "ambiguous" if len(canon_set) > 1 else "yes"


def resolve_address(token: str, candidate_names: list[str]) -> set[str]:
    """К каким из `candidate_names` может обращаться форма `token` ОДНОЗНАЧНО.

    Возвращает подмножество `candidate_names`. Если форма адресна к нескольким
    присутствующим ИЛИ сама по себе неоднозначна (в 2+ группах канонов) — считаем
    неоднозначной и НЕ используем для жёсткого конфликта: возвращаем set >1 или
    помечаем через `resolve_address_ambiguous`. Здесь — только однозначные.
    """
    hits: set[str] = set()
    for name in candidate_names:
        verdict = _same_person_form(token, _first_name_token(name))
        if verdict == "yes":
            hits.add(name)
    return hits


def resolve_address_ambiguous(token: str, candidate_names: list[str]) -> bool:
    """True, если форма `token` МОГЛА бы адресоваться кому-то из `candidate_names`,
    но неоднозначно (форма в 2+ группах канонов, напр. «саш»)."""
    for name in candidate_names:
        if _same_person_form(token, _first_name_token(name)) == "ambiguous":
            return True
    return False


def _iter_vocative_tokens(text: str):
    """Извлекает токены-КАНДИДАТЫ в обращения из текста реплики.

    Высокая точность (не хотим ложно кошмарить корректную разметку — как и прочие
    ревью-секции, порог высокий):
      1) усечённо-звательные формы («Тань», «Машунь», «Оль») — по природе обращения,
         матчатся ВЕЗДЕ как слово;
      2) полное имя в номинативе («Михаил») — только рядом с приветствием/зачином
         («Привет, Михаил», «Слушай, Михаил») ИЛИ имя+запятая в НАЧАЛЕ реплики
         («Михаил, глянь…»), чтобы не ловить перечисления/нарратив в середине.
    Отдаёт последовательность (token) — резолвит их вызывающий против участников.
    """
    if not text:
        return
    # (1) любые слова, чья нормаль — известная УСЕЧЁННАЯ форма (не полный канон).
    #     Формы-гомографы частых слов (`_COMMON_WORD_FORMS`) тут НЕ берём — только
    #     в звательной позиции ниже (иначе «тема закрыта» = ложное само-обращение).
    for m in _WORD_RE.finditer(text):
        w = _norm(m.group(0))
        if (w and w in _ALL_FORMS and w not in _CANON_LOWER
                and w not in _COMMON_WORD_FORMS):
            yield m.group(0)

    # (2) полное имя (номинатив) в звательной позиции.
    # 2a. приветствие + имя, ОБОСОБЛЕННОЕ пунктуацией/концом: «привет, Михаил!» /
    #     «слушай, Иван,». Трейлинг-пунктуация обязательна — иначе «Смотри, Иван уже
    #     сделал» (Иван — ПОДЛЕЖАЩЕЕ, не адресат) дало бы ложное обращение.
    greet = re.compile(
        rf"(?:{_GREETING_RE})[\s,]+({_WORD_RE.pattern})\s*(?=[,.!?…]|$)", re.IGNORECASE)
    for m in greet.finditer(text):
        w = _norm(m.group(1))
        if w in _CANON_LOWER or w in _COMMON_WORD_FORMS:
            yield m.group(1)
    # 2b. имя + запятая в НАЧАЛЕ реплики/предложения: «Михаил, посмотри…».
    head = re.compile(rf"(?:^|[.!?…]\s+)({_WORD_RE.pattern})\s*,", re.IGNORECASE)
    for m in head.finditer(text):
        w = _norm(m.group(1))
        if w in _CANON_LOWER or w in _COMMON_WORD_FORMS:
            yield m.group(1)


def addressed_names(text: str, candidate_names: list[str]) -> tuple[set[str], bool]:
    """Кого из участников адресует эта реплика (однозначно) + флаг «была неоднозначная
    форма». Возвращает `(set однозначно-адресованных имён, seen_ambiguous)`."""
    out: set[str] = set()
    seen_ambiguous = False
    for token in _iter_vocative_tokens(text):
        resolved = resolve_address(token, candidate_names)
        if len(resolved) == 1:
            out |= resolved
        elif len(resolved) >= 2:
            # форма адресна к нескольким присутствующим (два тёзки) — неоднозначно.
            seen_ambiguous = True
        elif resolve_address_ambiguous(token, candidate_names):
            seen_ambiguous = True
    return out, seen_ambiguous


def parse_speaker_lines(transcript_md: str) -> dict[str, list[str]]:
    """Транскрипт → {метка_спикера: [тексты реплик]}. Пустые тексты отброшены."""
    out: dict[str, list[str]] = {}
    if not transcript_md:
        return out
    for ln in transcript_md.split("\n"):
        m = _SPEAKER_LINE_RE.match(ln)
        if not m:
            continue
        label = (m.group(1) or "").strip()
        text = (m.group(2) or "").strip()
        if not label or not text:
            continue
        out.setdefault(label, []).append(text)
    return out


class VocativeConflict:
    """Спорная пара имён: разметка вероятно перепутала `name_a` и `name_b`.

    `kind`: "swap" (оба зовут сами себя — классический своп), "pair" (один зовёт
    сам себя, второй — единственный другой участник)."""

    __slots__ = ("name_a", "name_b", "kind")

    def __init__(self, name_a: str, name_b: str, kind: str = "swap"):
        # Стабилизируем порядок для идемпотентности/дедупа.
        a, b = sorted((name_a, name_b))
        self.name_a = a
        self.name_b = b
        self.kind = kind

    def key(self) -> tuple[str, str]:
        return (self.name_a, self.name_b)

    def __eq__(self, other) -> bool:
        return isinstance(other, VocativeConflict) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"VocativeConflict({self.name_a!r}, {self.name_b!r}, {self.kind!r})"


def find_conflicts(
    transcript_md: str,
    *,
    speaker_lines: Optional[dict[str, list[str]]] = None,
) -> list[VocativeConflict]:
    """R18: находит спорные пары имён по инварианту «A зовёт B ⇒ A≠B».

    Сигнал — САМО-обращение: спикер, подписанный именем N, в своих репликах зовёт
    по имени N (значит он, скорее всего, НЕ N). Пары строим так:
      • оба самозванца в паре (Татьяна зовёт Таню, Мария зовёт Машу) → «swap»;
      • один самозванец при ровно двух названных спикерах → пара с другим (он и
        есть перепутанный) → «pair».
    Неоднозначные диминутивы (РИСК5) в само-обращение НЕ засчитываются (не угадываем).

    Детерминированно, без сети. Порог высокий: на нормальной разметке — пусто.
    """
    lines = speaker_lines if speaker_lines is not None else parse_speaker_lines(transcript_md)
    # Только НАЗВАННЫЕ спикеры (не «Спикер N») участвуют в проверке авторства.
    named = {lbl: txts for lbl, txts in lines.items() if not is_placeholder_label(lbl)}
    candidate_names = list(named.keys())
    if len(candidate_names) < 2:
        return []

    # label → множество однозначно-адресованных имён (среди участников).
    addresses: dict[str, set[str]] = {}
    for label, texts in named.items():
        acc: set[str] = set()
        for t in texts:
            names, _amb = addressed_names(t, candidate_names)
            acc |= names
        addresses[label] = acc

    # Само-обращение: label зовёт сам себя.
    self_addr = [lbl for lbl in named if lbl in addresses.get(lbl, set())]
    if not self_addr:
        return []

    conflicts: set[VocativeConflict] = set()
    if len(self_addr) >= 2:
        # Классический своп: спарим самозванцев между собой (по стабильному порядку).
        ordered = sorted(self_addr)
        for i in range(0, len(ordered) - 1, 2):
            conflicts.add(VocativeConflict(ordered[i], ordered[i + 1], kind="swap"))
        # Нечётный хвост — спарим с ближайшим (пусть тоже подсветится).
        if len(ordered) % 2 == 1:
            conflicts.add(VocativeConflict(ordered[-1], ordered[-2], kind="swap"))
    else:
        # Ровно один самозванец. Однозначная пара — только при ровно двух названных.
        lone = self_addr[0]
        others = [n for n in candidate_names if n != lone]
        if len(others) == 1:
            conflicts.add(VocativeConflict(lone, others[0], kind="pair"))
        # >2 спикеров и один самозванец: партнёра однозначно не назвать → не пара.
        # Но авторство самозванца всё равно спорно — вернём «пару» имени с самим
        # собой нельзя; берём того, кого он адресует (это и есть спутанный «двойник»).
        else:
            addressed = addresses.get(lone, set())
            twin = next((n for n in sorted(addressed) if n != lone), None)
            if twin:
                conflicts.add(VocativeConflict(lone, twin, kind="pair"))
    return sorted(conflicts, key=lambda c: c.key())


# Секция findings для ревью-контура (см. llm_postprocess.apply_review_flags).
VOCATIVE_SECTION = "vocative"


def build_vocative_findings(conflicts: list[VocativeConflict]) -> list[dict]:
    """Спорные пары → findings `section="vocative"` для `apply_review_flags`.

    По одному finding на КАЖДОЕ имя пары (для inline-врезки у пунктов этого имени);
    `pair` несёт обе стороны (для плашки в шапке). `quote`/`note` заполнены, чтобы
    пройти фильтры `extra_findings` в review-контуре.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in conflicts or []:
        if c.key() in seen:
            continue
        seen.add(c.key())
        x, y = c.name_a, c.name_b
        # Врезка у имени X: «тут мог быть Y, а не X» (RAZM2) — и симметрично для Y.
        out.append({
            "section": VOCATIVE_SECTION,
            "quote": x,
            "note": f"тут мог быть {y}, а не {x}",
            "pair": [x, y],
        })
        out.append({
            "section": VOCATIVE_SECTION,
            "quote": y,
            "note": f"тут мог быть {x}, а не {y}",
            "pair": [x, y],
        })
    return out


def find_vocative_findings(transcript_md: str) -> list[dict]:
    """Удобная обёртка: транскрипт → findings `section="vocative"` (или [])."""
    return build_vocative_findings(find_conflicts(transcript_md))
