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
Тебе дан список имён участников встречи и реплики кластеров SPEAKER_00, SPEAKER_01 и т.д.
Назначь каждому кластеру имя из списка по контексту речи.

Правила:
- Используй только имена из списка. Не придумывай новые.
- Если ты не уверен про конкретный кластер — поставь null.
- Один кластер — одно имя. Одно имя — один кластер.
- Ключи в JSON используй РОВНО как указано (SPEAKER_00 заглавными с подчёркиванием).

Ответь ТОЛЬКО валидным JSON в формате:
{"SPEAKER_00": "Имя_или_null", "SPEAKER_01": "Имя_или_null", ...}
Без markdown, без объяснений, без префиксов."""


def map_from_claude_haiku(
    turns: list[AlignedTurn],
    participants: list[str],
    already_mapped: dict[str, str],
    api_key: Optional[str] = None,  # kept for API compat; не используется
    model: str = "claude-haiku-4-5-20251001",  # kept for API compat; не используется
    max_tokens: int = 200,  # kept for API compat; не используется
) -> dict[str, str]:
    """LLM-добивка через `claude` CLI (подписка владельца, без отдельного API ключа).

    В Ф4 решено не заводить ANTHROPIC_API_KEY — `claude --print` ходит через
    ту же подписку Claude Code, что и интерактивный режим. Минус — задержка
    5-10s на запуск CLI; запускается после транскрипции, до встречи не доходит.

    Дисциплина «Опасной тройки» (см. CLAUDE.md проекта):
      - В промпте только непривязанные кластеры + их короткие реплики +
        список оставшихся имён.
      - Логируем только метаданные (число кластеров, число имён, статус).
      - Не сохраняем сырой ответ — только результат {cluster: name | None}.
    """
    import subprocess
    import shutil

    enabled = os.environ.get("ENABLE_CLAUDE_NAME_MAPPING", "0").strip().lower() in ("1", "true", "yes")
    if not enabled:
        logger.info("Source 3 (Claude CLI): disabled by ENABLE_CLAUDE_NAME_MAPPING")
        return {}

    claude_bin = shutil.which("claude")
    if not claude_bin:
        logger.warning("Source 3 (Claude CLI): `claude` not in PATH — пропускаем")
        return {}

    unresolved_clusters = sorted({t.speaker for t in turns if t.speaker and t.speaker not in already_mapped})
    available_names = [p for p in participants if p not in already_mapped.values()]
    if not unresolved_clusters or not available_names:
        logger.info("Source 3 (Claude CLI): nothing to resolve")
        return {}

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

    full_prompt = (
        CLAUDE_MAPPING_SYSTEM_PROMPT
        + "\n\n"
        + f"Список имён участников встречи: {', '.join(available_names)}\n\n"
        + f"Реплики спикеров:\n{transcript_block}"
    )

    logger.info(
        "Source 3 (Claude CLI): %d clusters × %d names, total reply lines=%d",
        len(unresolved_clusters), len(available_names),
        sum(len(v) for v in cluster_to_lines.values()),
    )

    try:
        result = subprocess.run(
            [claude_bin, "--print"],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(
                "Source 3 (Claude CLI): exit=%d, stderr=%s",
                result.returncode, result.stderr.strip()[:200],
            )
            return {}
        raw = (result.stdout or "").strip()
        if not raw:
            logger.warning("Source 3 (Claude CLI): пустой ответ")
            return {}
        # Иногда модель оборачивает в ```json … ``` или говорит лишнее.
        # Сначала вырежем markdown fence, затем найдём первый JSON-объект.
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if not m:
            logger.warning("Source 3 (Claude CLI): не нашли JSON в ответе")
            return {}
        parsed = json.loads(m.group(0))
    except subprocess.TimeoutExpired:
        logger.warning("Source 3 (Claude CLI): timeout 60s — пропуск")
        return {}
    except json.JSONDecodeError as e:
        logger.warning("Source 3 (Claude CLI): invalid JSON: %s", e)
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Source 3 (Claude CLI) failed: %s", e)
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
