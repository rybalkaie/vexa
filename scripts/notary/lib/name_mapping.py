"""Маппинг имён спикеров: детерминированные источники S1+S2.

Источник 1 — Telemost participants list (точная информация: кто был в комнате).
Источник 2 — regex + pymorphy3 по транскрипту (vocative: «Михаил, посмотри»).

LLM-добивка (бывший Source 3 / Claude Haiku) живёт отдельно в
`lib/llm_postprocess.py::map_speaker_names`. Из `map_all` она НЕ
вызывается — это делает `finalize-meeting.py` после `map_all`, чтобы
получить confidence-per-cluster для Ф3 clarify-flow.

Дисциплина «Опасной тройки» (CLAUDE.md проекта meeting-notary):
  - НЕ логируем текст реплик (только число реплик, число имён).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from .align import AlignedTurn


logger = logging.getLogger(__name__)


@dataclass
class MappingResult:
    cluster_to_name: dict[str, str]  # SPEAKER_00 → «Илья»; пустой если ничего не уверены
    sources_used: list[str]          # подмножество из ["telemost_list", "regex_pymorphy3"]
    unresolved_clusters: list[str]   # кластеры, которым не нашли имя


# ---------- Источник 1: Telemost participants ----------

def map_from_telemost_list(
    clusters: list[str],
    participants: list[str],
) -> dict[str, str]:
    """Если кластер ровно один и имя ровно одно — назначаем напрямую.

    Случаи где >1 имени и >1 кластера решаются источниками 2/3 (мы не знаем,
    какой голос принадлежит кому без анализа речи).
    """
    if not clusters or not participants:
        return {}
    if len(clusters) == 1 and len(participants) == 1:
        logger.info("Source 1 (Telemost list): 1 cluster ↔ 1 name → direct match")
        return {clusters[0]: participants[0]}
    logger.info(
        "Source 1 (Telemost list): %d clusters, %d participants — passing to sources 2/3",
        len(clusters), len(participants),
    )
    return {}


# ---------- Источник 2: regex + pymorphy3 ----------

def _expand_name_forms(name: str) -> set[str]:
    """Разворачивает имя во все падежные/числовые формы через pymorphy3.

    Возвращает set строк в нижнем регистре. Если pymorphy3 недоступен —
    возвращает только исходное имя.
    """
    forms: set[str] = {name.lower()}
    try:
        import pymorphy3  # type: ignore

        morph = pymorphy3.MorphAnalyzer()
        parses = morph.parse(name)
        if parses:
            best = parses[0]
            for lex in best.lexeme:
                forms.add(lex.word.lower())
    except ImportError:
        logger.warning("pymorphy3 not installed — using surface form only for %r", name)
    except Exception as e:
        logger.warning("pymorphy3 failed for %r: %s", name, e)
    return forms


def _scan_speech_votes(
    turns: list[AlignedTurn],
    name_forms: dict[str, set[str]],
) -> tuple[
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
]:
    """Сканирует реплики, считает голоса по vocative-обращениям.

    Возвращает (votes, anti_votes, strict_anti_votes), где ключ — (cluster, name):
      - votes — «следующий говорящий = это имя» (vocative-сигнал weight >= 1).
      - anti_votes — «этот cluster упомянул это имя → он НЕ это имя» (любой вес).
      - strict_anti_votes — то же, но ТОЛЬКО строгий vocative (запятая/знак после
        имени в начале реплики, weight 2.0). Самый надёжный негативный сигнал:
        кто окликнул именем N («Михаил, …»), тот точно НЕ N. На нём решается кейс
        ровно-2-спикера (Ф4а) и валидируется якорь серии (Ф4б).

    Чистая функция (без IO) — переиспользуется и Source 2, и проверкой якоря.
    """
    votes: dict[tuple[str, str], float] = {}
    anti_votes: dict[tuple[str, str], float] = {}
    strict_anti_votes: dict[tuple[str, str], float] = {}

    for i, turn in enumerate(turns):
        if not turn.text or not turn.speaker:
            continue
        text_lower = turn.text.lower()

        # Найдём имена (любая форма), упомянутые в этой реплике.
        # Уровни уверенности:
        #   2.0 — vocative-strict: «Михаил, ...» / «Михаил! ...» (знак после имени).
        #   1.0 — name-at-beginning: «Михаил рад тебя видеть.» — Whisper-medium
        #         иногда теряет запятую после vocative, но имя в начале реплики
        #         почти всегда означает обращение.
        #   0.5 — name-anywhere: упоминание имени в середине/конце.
        mentioned: list[tuple[str, float]] = []  # (name, vote_weight)
        for name, forms in name_forms.items():
            best_weight = 0.0
            for form in forms:
                if re.match(rf"^\s*{re.escape(form)}\s*[,!\?\-—:]", text_lower):
                    best_weight = max(best_weight, 2.0)
                    break
                if re.match(rf"^\s*{re.escape(form)}\b", text_lower):
                    best_weight = max(best_weight, 1.0)
                    # не break — может оказаться и vocative-strict для другой формы
                if re.search(rf"\b{re.escape(form)}\b", text_lower):
                    best_weight = max(best_weight, 0.5)
            if best_weight > 0:
                mentioned.append((name, best_weight))

        if not mentioned:
            continue

        # Cluster, который произнёс эту реплику.
        speaker_cluster = turn.speaker

        # Следующий cluster в timeline (не равный speaker_cluster).
        next_cluster: Optional[str] = None
        for j in range(i + 1, len(turns)):
            if turns[j].speaker and turns[j].speaker != speaker_cluster:
                next_cluster = turns[j].speaker
                break

        for name, weight in mentioned:
            # speaker_cluster — НЕ это имя (с весом всегда).
            anti_votes[(speaker_cluster, name)] = anti_votes.get((speaker_cluster, name), 0.0) + weight
            # Строгий vocative (weight >= 2.0 — запятая/знак после имени) — самый
            # надёжный anti-сигнал, отдельным счётчиком для 2-спикерного решения.
            if weight >= 2.0:
                strict_anti_votes[(speaker_cluster, name)] = (
                    strict_anti_votes.get((speaker_cluster, name), 0.0) + weight
                )
            # Vocative-сигнал (weight >= 1) — следующий cluster = это имя.
            # Mention-only (weight < 1) — не назначаем имя, только anti-vote.
            if weight >= 1.0 and next_cluster:
                votes[(next_cluster, name)] = votes.get((next_cluster, name), 0.0) + weight

    return votes, anti_votes, strict_anti_votes


def map_from_speech_regex(
    turns: list[AlignedTurn],
    participants: list[str],
    already_mapped: dict[str, str],
) -> dict[str, str]:
    """Ищет vocative обращения «Имя, ...» в начале реплик.

    Логика голосования:
      - Если в реплике cluster A в начале «Имя_X, ...» — A НЕ Имя_X,
        и следующий speaking cluster B — Имя_X (с весом 1).
      - Если в реплике cluster A в середине «..., Имя_X, ...» — слабее,
        тоже даём anti-vote для A (вес 0.5).

    В конце greedy: cluster с max(votes_for_name - anti_votes_for_name) > 0
    получает это имя. Один cluster — одно имя, один name — один cluster.
    """
    if not participants or len(turns) < 2:
        return {}

    # Кластеры, для которых уже есть имя — не трогаем.
    available_clusters = sorted({t.speaker for t in turns if t.speaker and t.speaker not in already_mapped})
    available_names = [p for p in participants if p not in already_mapped.values()]
    if not available_clusters or not available_names:
        return {}

    # Разворачиваем формы имён и считаем голоса (вынесено в _scan_speech_votes).
    name_forms: dict[str, set[str]] = {n: _expand_name_forms(n) for n in available_names}
    votes, anti_votes, strict_anti_votes = _scan_speech_votes(turns, name_forms)

    # Ф4а: спец-случай ровно 2 спикера и 2 имени. Forward-голос («следующий
    # говорящий = названное имя») на двух спикерах ненадёжен и переворачивает
    # авторство (директорат 03.06: Илья↔Михаил). Решаем по СТРОГИМ vocative-
    # обращениям (надёжный негативный сигнал), forward-голоса игнорируем. Нет
    # различающего строгого сигнала → отдаём остаток LLM-добивке (не угадываем
    # вслепую — лучше «Спикер N» + дисклеймер, чем уверенно неверный автор).
    if len(available_clusters) == 2 and len(available_names) == 2:
        two = _resolve_two_speakers(
            strict_anti_votes, available_clusters, available_names
        )
        if two:
            logger.info("Source 2 (2-speaker strict-vocative): mapped 2 clusters")
        else:
            logger.info(
                "Source 2 (2-speaker): no discriminating strict vocative — defer to LLM"
            )
        return two

    # Greedy назначение (3+ спикеров / неполный состав).
    result: dict[str, str] = {}
    remaining_clusters = set(available_clusters)
    remaining_names = set(available_names)

    while remaining_clusters and remaining_names:
        best_score = -1.0
        best_pair: Optional[tuple[str, str]] = None
        for c in remaining_clusters:
            for n in remaining_names:
                score = votes.get((c, n), 0.0) - anti_votes.get((c, n), 0.0)
                if score > best_score:
                    best_score = score
                    best_pair = (c, n)
        if best_pair is None or best_score <= 0:
            break  # ниже порога — оставшиеся не приписываем
        c, n = best_pair
        result[c] = n
        remaining_clusters.discard(c)
        remaining_names.discard(n)

    if result:
        logger.info("Source 2 (regex+pymorphy3): mapped %d clusters", len(result))
    else:
        logger.info("Source 2 (regex+pymorphy3): no confident matches")
    return result


def _resolve_two_speakers(
    strict_anti_votes: dict[tuple[str, str], float],
    clusters: list[str],
    names: list[str],
) -> dict[str, str]:
    """Ф4а: решает ровно 2 cluster ↔ 2 name по строгим vocative-обращениям.

    Идея: «X окликнул именем N запятой → X точно НЕ N». На двух спикерах этого
    достаточно: тот, у кого строгий anti против N сильнее, — НЕ N, значит он —
    другое имя. Forward-голос («следующий говорит = N») сюда НЕ заходит: именно
    он инвертирует авторство (директорат 03.06).

    Сравниваем две раскладки по сумме строгого anti ПРОТИВ выбранных пар —
    выигрывает меньшая (меньше противоречит «X окликнул N»). Равенство (в т.ч.
    оба нуля = нет строгого сигнала) → `{}` (пусто): отдаём LLM-добивке, не
    угадываем неверного автора.
    """
    if len(clusters) != 2 or len(names) != 2:
        return {}
    c0, c1 = clusters
    n0, n1 = names

    def _sa(c: str, n: str) -> float:
        return strict_anti_votes.get((c, n), 0.0)

    # Раскладка A: c0→n0, c1→n1; B: c0→n1, c1→n0. «Противоречие» раскладки —
    # строгий anti против именно её пар (X назначили имя, которое X окликал).
    contra_a = _sa(c0, n0) + _sa(c1, n1)
    contra_b = _sa(c0, n1) + _sa(c1, n0)
    if contra_a == contra_b:
        return {}  # нет различающего строгого сигнала
    if contra_a < contra_b:
        return {c0: n0, c1: n1}
    return {c0: n1, c1: n0}


# ---------- Ф4б: якорь авторства из памяти серии ----------

def _apply_series_anchor(
    turns: list[AlignedTurn],
    clusters: list[str],
    participants: list[str],
    anchor: dict[str, str],
) -> dict[str, str]:
    """Ф4б: применяет закреплённое человеком сопоставление спикер→имя из памяти серии.

    `anchor` = `{cluster: name}` из выжимки прошлой встречи серии (REQ 1.2). Человек
    поправил авторство реплаем → это закрепление БЬЁТ начальную догадку Ф4а. Но
    применяем ТОЛЬКО валидные и НЕпротиворечивые записи (защита от инверсии,
    которую Ф4а закрыла; метки S1/S2 нестабильны между джобами — см. РИСК-диаризация):
      • cluster существует в текущей встрече И имя в составе участников;
      • НИ ОДНА запись не противоречит строгому vocative ТЕКУЩЕЙ записи. Если в
        текущей встрече cluster C сам окликнул имя N («N, …») — он точно НЕ N; значит
        якорь C→N неверен. А так как метки S1/S2 между джобами нестабильны, ЛЮБОЕ
        такое противоречие означает, что метки этого якоря НЕ соответствуют текущей
        джобе → ВЕСЬ якорь недостоверен и отбрасывается целиком (иначе уцелевшая
        половина инвертированного якоря дотянула бы инверсию через Source 1 по
        исключению). При расхождении current-evidence всегда побеждает — Ф4а-фолбэк
        чинит авторство сам.

    Возвращает применимые записи `{cluster: name}` (один cluster — одно имя), либо
    `{}` если якорь невалиден/противоречив.
    """
    if not anchor:
        return {}
    cluster_set = set(clusters)
    name_set = set(participants)
    cands = {
        str(c): str(n)
        for c, n in anchor.items()
        if str(c) in cluster_set and str(n) in name_set
    }
    if not cands:
        return {}

    # Строгий vocative текущей встречи — для проверки на противоречие.
    name_forms = {n: _expand_name_forms(n) for n in set(cands.values())}
    _, _, strict_anti_votes = _scan_speech_votes(turns, name_forms)
    contradicted = any(strict_anti_votes.get((c, n), 0.0) > 0 for c, n in cands.items())
    if contradicted:
        logger.info(
            "Ф4б series anchor: противоречие строгому vocative → отбрасываем весь якорь "
            "(метки не соответствуют текущей джобе)"
        )
        return {}

    out: dict[str, str] = {}
    used_names: set[str] = set()
    for cluster, name in cands.items():
        if name in used_names:
            continue  # дедуп: одно имя на один cluster
        out[cluster] = name
        used_names.add(name)
    if out:
        logger.info("Ф4б series anchor: applied=%d cluster(s)", len(out))
    return out


# ---------- Оркестрация ----------

def map_all(
    turns: list[AlignedTurn],
    participants: list[str],
    *,
    anchor: Optional[dict[str, str]] = None,
) -> MappingResult:
    """Прогоняет детерминированные источники по очереди: якорь серии → S1 → S2.

    `anchor` (Ф4б, REQ 1.2) — закреплённое человеком сопоставление спикер→имя из
    памяти серии. Применяется ПЕРВЫМ (бьёт догадку Ф4а), но лишь валидные и
    непротиворечивые записи (см. `_apply_series_anchor`). Догадка Ф4а
    (`map_from_speech_regex`/`_resolve_two_speakers`) остаётся фолбэком для
    незакреплённых кластеров. `anchor=None` → поведение Ф4а без изменений.

    LLM-добивка (бывший Source 3 / Claude Haiku) вынесена в
    `lib/llm_postprocess.py::map_speaker_names` и вызывается отдельно из
    `finalize-meeting.py` для unresolved'ов после якоря+S1+S2.
    """
    clusters = sorted({t.speaker for t in turns if t.speaker})
    cluster_to_name: dict[str, str] = {}
    sources_used: list[str] = []

    if not clusters:
        return MappingResult(cluster_to_name={}, sources_used=[], unresolved_clusters=[])

    # 0) Ф4б: якорь авторства из памяти серии (бьёт догадку, но валидируется).
    if anchor:
        delta = _apply_series_anchor(turns, clusters, participants, anchor)
        if delta:
            cluster_to_name.update(delta)
            sources_used.append("series_anchor")

    # 1) Telemost list (по свободным от якоря кластерам/именам).
    free_clusters = [c for c in clusters if c not in cluster_to_name]
    free_names = [p for p in participants if p not in cluster_to_name.values()]
    delta = map_from_telemost_list(free_clusters, free_names)
    if delta:
        cluster_to_name.update(delta)
        sources_used.append("telemost_list")

    # 2) Regex + pymorphy3 (already_mapped исключает закреплённое якорем/S1).
    delta = map_from_speech_regex(turns, participants, cluster_to_name)
    if delta:
        cluster_to_name.update(delta)
        sources_used.append("regex_pymorphy3")

    unresolved = [c for c in clusters if c not in cluster_to_name]
    return MappingResult(
        cluster_to_name=cluster_to_name,
        sources_used=sources_used,
        unresolved_clusters=unresolved,
    )


def apply_mapping(turns: list[AlignedTurn], mapping: dict[str, str]) -> list[AlignedTurn]:
    """Заполняет display_name в Turns по полученному cluster→name маппингу.

    Для непривязанных кластеров display_name остаётся None — рендерер
    выведет «Спикер N» по индексу.
    """
    for t in turns:
        if t.speaker and t.speaker in mapping:
            t.display_name = mapping[t.speaker]
    return turns
