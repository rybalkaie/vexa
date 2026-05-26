"""Маппинг имён спикеров: три источника по убывающей надёжности.

Источник 1 — Telemost participants list (точная информация: кто был в комнате).
Источник 2 — regex + pymorphy3 по транскрипту (vocative: «Михаил, посмотри»).
Источник 3 — Claude Haiku (LLM-добивка для непривязанных «Спикер N»).

Дисциплина «Опасной тройки» (CLAUDE.md проекта meeting-notary):
  - НЕ логируем текст реплик (только число реплик, число имён).
  - В промпт Claude — ТОЛЬКО непривязанные кластеры + список имён, без
    лишнего контекста.
  - НЕ сохраняем сырой ответ Claude — только результат {cluster: name | None}.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from .align import AlignedTurn


logger = logging.getLogger(__name__)


@dataclass
class MappingResult:
    cluster_to_name: dict[str, str]  # SPEAKER_00 → «Илья»; пустой если ничего не уверены
    sources_used: list[str]          # подмножество из ["telemost_list", "regex_pymorphy3", "claude_haiku"]
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

    # Разворачиваем формы имён.
    name_forms: dict[str, set[str]] = {n: _expand_name_forms(n) for n in available_names}

    # Считаем голоса.
    votes: dict[tuple[str, str], float] = {}    # (cluster, name) → score
    anti_votes: dict[tuple[str, str], float] = {}

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
            # Vocative-сигнал (weight >= 1) — следующий cluster = это имя.
            # Mention-only (weight < 1) — не назначаем имя, только anti-vote.
            if weight >= 1.0 and next_cluster:
                votes[(next_cluster, name)] = votes.get((next_cluster, name), 0.0) + weight

    # Greedy назначение.
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


# ---------- Источник 3: Claude Haiku ----------

CLAUDE_MAPPING_SYSTEM_PROMPT = """Ты помогаешь определить, кто из участников встречи какой реплики говорил.
Тебе дан список имён участников встречи и реплики «Спикер 0», «Спикер 1» и т.д.
Назначь каждому спикеру имя из списка по контексту речи.

Правила:
- Используй только имена из списка. Не придумывай новые.
- Если ты не уверен про конкретного спикера — поставь null.
- Один спикер — одно имя. Одно имя — один спикер.

Ответь ТОЛЬКО валидным JSON в формате:
{"СПИКЕР_0": "Имя_или_null", "СПИКЕР_1": "Имя_или_null", ...}
Без markdown, без объяснений, без префиксов."""


def map_from_claude_haiku(
    turns: list[AlignedTurn],
    participants: list[str],
    already_mapped: dict[str, str],
    api_key: Optional[str] = None,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 200,
) -> dict[str, str]:
    """LLM-добивка через Claude Haiku для непривязанных кластеров.

    Дисциплина:
      - В промпте только непривязанные кластеры + их короткие реплики +
        список оставшихся имён.
      - Логируем только metadata (число кластеров, число имён, статус).
      - Не сохраняем сырой ответ Claude — только результат map.
    """
    enabled = os.environ.get("ENABLE_CLAUDE_NAME_MAPPING", "0").strip().lower() in ("1", "true", "yes")
    if not enabled:
        logger.info("Source 3 (Claude Haiku): disabled by ENABLE_CLAUDE_NAME_MAPPING")
        return {}

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.info("Source 3 (Claude Haiku): ANTHROPIC_API_KEY not set, skipping")
        return {}

    unresolved_clusters = sorted({t.speaker for t in turns if t.speaker and t.speaker not in already_mapped})
    available_names = [p for p in participants if p not in already_mapped.values()]
    if not unresolved_clusters or not available_names:
        logger.info("Source 3 (Claude Haiku): nothing to resolve")
        return {}

    try:
        import anthropic  # type: ignore
    except ImportError:
        logger.warning("anthropic SDK not installed — Source 3 skipped")
        return {}

    # Готовим компактный transcript-фрагмент: для каждого непривязанного кластера
    # берём до 5 его реплик. Этого достаточно для контекста и не сливает весь
    # текст наружу.
    cluster_to_lines: dict[str, list[str]] = {c: [] for c in unresolved_clusters}
    for turn in turns:
        if turn.speaker in cluster_to_lines and turn.text.strip():
            if len(cluster_to_lines[turn.speaker]) < 5:
                cluster_to_lines[turn.speaker].append(turn.text.strip())

    parts = []
    for c, lines in cluster_to_lines.items():
        if not lines:
            continue
        body = " | ".join(lines)
        parts.append(f"{c}: {body}")
    transcript_block = "\n".join(parts)

    user_prompt = (
        f"Список имён участников встречи: {', '.join(available_names)}\n\n"
        f"Реплики спикеров:\n{transcript_block}"
    )

    logger.info(
        "Source 3 (Claude Haiku): %d clusters × %d names, total reply lines=%d",
        len(unresolved_clusters), len(available_names),
        sum(len(v) for v in cluster_to_lines.values()),
    )

    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=CLAUDE_MAPPING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        # Конфиденциальность: не логируем resp целиком, только метаданные.
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", "?") if usage else "?"
        out_tok = getattr(usage, "output_tokens", "?") if usage else "?"
        logger.info("Claude response received (in=%s, out=%s tokens)", in_tok, out_tok)

        # Берём текст из первого text block'а.
        text_blocks = [b for b in resp.content if getattr(b, "type", None) == "text"]
        if not text_blocks:
            logger.warning("Claude returned no text blocks")
            return {}
        raw = text_blocks[0].text.strip()
        # Иногда модель оборачивает в ```json ... ``` — отрежем.
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Claude returned invalid JSON: %s", e)
        return {}
    except Exception as e:
        logger.warning("Claude call failed: %s", e)
        return {}

    # Валидируем: ключ должен быть в unresolved_clusters, значение — из available_names.
    result: dict[str, str] = {}
    used_names: set[str] = set()
    for cluster, name in parsed.items():
        if not isinstance(cluster, str) or cluster not in unresolved_clusters:
            continue
        if name is None or not isinstance(name, str):
            continue
        if name not in available_names:
            continue
        if name in used_names:
            continue
        result[cluster] = name
        used_names.add(name)

    if result:
        logger.info("Source 3 (Claude Haiku): mapped %d clusters", len(result))
    else:
        logger.info("Source 3 (Claude Haiku): no confident matches")
    return result


# ---------- Оркестрация ----------

def map_all(
    turns: list[AlignedTurn],
    participants: list[str],
) -> MappingResult:
    """Прогоняет все три источника по очереди. Накапливает результат."""
    clusters = sorted({t.speaker for t in turns if t.speaker})
    cluster_to_name: dict[str, str] = {}
    sources_used: list[str] = []

    if not clusters:
        return MappingResult(cluster_to_name={}, sources_used=[], unresolved_clusters=[])

    # 1) Telemost list
    delta = map_from_telemost_list(clusters, participants)
    if delta:
        cluster_to_name.update(delta)
        sources_used.append("telemost_list")

    # 2) Regex + pymorphy3
    delta = map_from_speech_regex(turns, participants, cluster_to_name)
    if delta:
        cluster_to_name.update(delta)
        sources_used.append("regex_pymorphy3")

    # 3) Claude Haiku
    delta = map_from_claude_haiku(turns, participants, cluster_to_name)
    if delta:
        cluster_to_name.update(delta)
        sources_used.append("claude_haiku")

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
