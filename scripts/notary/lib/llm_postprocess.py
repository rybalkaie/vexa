"""Единый постпроцессинг-модуль на базе Claude (LLM-добивка после STT).

Содержит ТОЛЬКО функции, использующие LLM (Claude Haiku 4.5 / Sonnet 4.6),
дополняющие детерминированные шаги (`name_mapping` Source 1+2,
`render`-шаблон транскрипта).

Текущий состав модуля:
  - `map_speaker_names` — LLM-маппинг unresolved cluster'ов на имена
    участников (Ф2 плана `2026-05-28-meeting-notary-llm-...`).

Будущий состав (по фазам того же плана):
  - Ф3: `clarify_speakers_via_telegram` — clarify-flow при низком confidence.
  - Ф4: `generate_protocol` — структурированный протокол по методичке.
  - Ф5: `extract_tasks` / `route_tasks` — извлечение задач + маршрутизация.
  - Ф6: `deliver_protocol` — доставка в Telegram + идемпотентность.

Дисциплина «Опасной тройки» (CLAUDE.md проекта):
  - НЕ логируем содержимое реплик.
  - В лог идут только число cluster'ов, число имён, итог маппинга, elapsed.
  - НЕ сохраняем сырой ответ Claude в долгоживущие файлы.

Транспорт LLM: `claude --print` CLI через `lib/claude_cli.py` (подписка
владельца, без ANTHROPIC_API_KEY). См. `claude_cli.py` для деталей.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from .align import AlignedTurn
from .claude_cli import (
    ClaudeCliError,
    ClaudeCliNotInstalled,
    call_claude_print,
)


logger = logging.getLogger(__name__)


# ---------- Source 3 (LLM): маппинг имён ----------

MAP_SPEAKER_NAMES_SYSTEM_PROMPT = """Ты помогаешь определить, кто из участников встречи какой реплики говорил.
Тебе дан список имён участников и блоки реплик кластеров SPEAKER_00, SPEAKER_01 и т.д.
По смыслу речи (роль, манера, упоминания «я», обращения к другим по имени и т.п.) сопоставь каждому кластеру имя из списка.

Правила:
- Используй только имена из переданного списка. Не придумывай новые имена и не сокращай переданные.
- Один кластер — одно имя. Одно имя — один кластер.
- Если по конкретному кластеру ты не уверен — поставь "name": null. Лучше пропустить, чем угадать неверно.

Калибровка confidence (важно):
- 0.95–1.00 — есть прямое самопредставление («я — Илья», «меня зовут …») или vocative-обращение к этому спикеру от другого («Илья, расскажи…»).
- 0.85–0.95 — есть несколько сильных сходящихся сигналов: явная роль (владелец / менеджер / технический эксперт), упоминания «я», «мы» с привязкой к контексту имени, манера, предмет речи. Это уровень «уверен почти точно».
- 0.70–0.85 — один сильный сигнал ИЛИ несколько слабых, но они единогласны.
- 0.50–0.70 — склоняюсь, но не уверен; контекст допускает обе версии.
- < 0.50 — лучше отдать null.

Не занижай confidence от страха ошибиться: если на репликах кластера есть 2+ согласованных сигнала про конкретного человека из списка — это уже 0.85+. 0.80 — это сигнал «я не уверен», который запустит ручной clarify; используй его только когда правда не уверен.

Vocative-обращения («Михаил, посмотри…»), упоминания «я …» с проверяемой ролью и предметная специфика (кто про что говорит как профессионал) — самые надёжные сигналы.

Ответь СТРОГО валидным JSON-массивом в формате:
[
  {"cluster": "SPEAKER_00", "name": "Имя из списка или null", "confidence": 0.0_до_1.0},
  {"cluster": "SPEAKER_01", "name": "Имя из списка или null", "confidence": 0.0_до_1.0}
]
Без markdown-обёртки, без объяснений, без префиксов/постфиксов — только JSON-массив."""


def _build_user_prompt(
    cluster_to_lines: dict[str, list[str]],
    available_names: list[str],
) -> str:
    """Собирает payload для Claude: список имён + блоки реплик по cluster'ам."""
    parts = []
    for c, lines in cluster_to_lines.items():
        if not lines:
            continue
        body = " | ".join(lines)
        parts.append(f"{c}: {body}")
    transcript_block = "\n".join(parts)
    return (
        f"Список имён участников встречи: {', '.join(available_names)}\n\n"
        f"Реплики кластеров:\n{transcript_block}"
    )


def _select_longest_lines(
    turns: list[AlignedTurn],
    clusters: list[str],
    max_lines_per_cluster: int = 8,
) -> dict[str, list[str]]:
    """Берёт до N самых длинных реплик каждого cluster'а (по числу слов).

    Длинные реплики информативнее коротких «угу/да/понял» для определения
    говорящего по смыслу. Ограничение — токенный бюджет промта.
    """
    bucket: dict[str, list[str]] = {c: [] for c in clusters}
    for t in turns:
        if t.speaker in bucket and t.text and t.text.strip():
            bucket[t.speaker].append(t.text.strip())
    for c in clusters:
        bucket[c].sort(key=lambda line: len(line.split()), reverse=True)
        bucket[c] = bucket[c][:max_lines_per_cluster]
    return bucket


def _parse_llm_response(
    raw: str,
    unresolved_clusters: list[str],
    available_names: list[str],
) -> dict[str, tuple[str, float]]:
    """Парсит ответ Claude → dict[cluster, (name, confidence)].

    Валидации:
      - Это JSON-массив объектов.
      - cluster ∈ unresolved_clusters.
      - name ∈ available_names (не выдумано) или null (тогда пропускаем).
      - confidence в [0.0, 1.0]; иначе clamp.
      - Уникальность name по cluster'ам (greedy по убыванию confidence).
    """
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    # Берём ПЕРВЫЙ полный JSON-массив (иначе если LLM выдаст текст вида
    # "вот варианты: [...], итог: [...]" или скобка попадёт внутрь строки —
    # склеим/обрежем не там). Используем стандартный raw_decode, он корректно
    # парсит JSON-токены, не путая скобки в строках.
    start = raw.find("[")
    if start < 0:
        raise ValueError("LLM response: JSON-массив не найден")
    try:
        parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM response: невалидный JSON-массив: {e}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"LLM response: ожидался массив, получили {type(parsed).__name__}")

    candidates: list[tuple[str, str, float]] = []  # (cluster, name, conf)
    seen_clusters: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        cluster = item.get("cluster")
        name = item.get("name")
        conf = item.get("confidence")
        if not isinstance(cluster, str) or cluster not in unresolved_clusters:
            continue
        if cluster in seen_clusters:
            continue
        seen_clusters.add(cluster)
        if name is None:
            continue
        if not isinstance(name, str) or name not in available_names:
            continue
        try:
            conf_f = float(conf)
        except (TypeError, ValueError):
            conf_f = 0.0
        conf_f = max(0.0, min(1.0, conf_f))
        candidates.append((cluster, name, conf_f))

    candidates.sort(key=lambda x: x[2], reverse=True)
    result: dict[str, tuple[str, float]] = {}
    used_names: set[str] = set()
    for cluster, name, conf in candidates:
        if name in used_names:
            continue
        result[cluster] = (name, conf)
        used_names.add(name)
    return result


def map_speaker_names(
    turns: list[AlignedTurn],
    expected_participants: list[str],
    panel_participants: list[str],
    *,
    already_mapped: Optional[dict[str, str]] = None,
    meeting_sid: Optional[str] = None,
) -> dict[str, tuple[str, float]]:
    """Маппит cluster label (SPEAKER_NN) на имя из участников через Claude.

    Принимает:
      turns — все AlignedTurn'ы встречи (нужен контекст соседних реплик).
      expected_participants — `meta.expectedParticipants` (из watched.yaml).
      panel_participants — `meta.participants` (то, что показал Telemost UI).
      already_mapped — уже разрешённые Source 1+2 (передаются для исключения
                      из unresolved и из доступных имён).
      meeting_sid — для structured-лога (не передаётся в промт).

    Возвращает dict[cluster, (name, confidence)] ТОЛЬКО для cluster'ов,
    по которым LLM дал ответ с непустым name. Прочие cluster'ы остаются
    unresolved — рендерер выведет «Спикер N».

    Не падает на ошибках LLM/JSON/CLI — возвращает {} с warning'ом в лог.
    Source 1+2 уже сделали свою работу, частичный маппинг лучше падения.

    Гейт включения: env `ENABLE_LLM_NAME_MAPPING`. Дефолт — ВКЛЮЧЕНО
    (`"" / "1" / "true" / "yes"` → on; `"0" / "false" / "no"` → off).
    """
    already_mapped = already_mapped or {}

    raw_flag = (os.environ.get("ENABLE_LLM_NAME_MAPPING") or "").strip().lower()
    if raw_flag in ("0", "false", "no"):
        logger.info("[llm-map] disabled by ENABLE_LLM_NAME_MAPPING=%s", raw_flag)
        return {}

    unresolved_clusters = sorted(
        {t.speaker for t in turns if t.speaker and t.speaker not in already_mapped}
    )
    name_pool: list[str] = []
    seen: set[str] = set()
    for name in list(expected_participants) + list(panel_participants):
        if not name or not isinstance(name, str):
            continue
        if name in already_mapped.values():
            continue
        if name in seen:
            continue
        seen.add(name)
        name_pool.append(name)

    if not unresolved_clusters or not name_pool:
        logger.info(
            "[llm-map] meeting=%s nothing to resolve (unresolved=%d, names=%d)",
            meeting_sid or "?", len(unresolved_clusters), len(name_pool),
        )
        return {}

    cluster_to_lines = _select_longest_lines(turns, unresolved_clusters)
    non_empty = {c: lines for c, lines in cluster_to_lines.items() if lines}
    if not non_empty:
        logger.info("[llm-map] meeting=%s no text for unresolved clusters", meeting_sid or "?")
        return {}

    user_prompt = _build_user_prompt(non_empty, name_pool)

    started = time.monotonic()
    try:
        raw = call_claude_print(
            user_prompt,
            system=MAP_SPEAKER_NAMES_SYSTEM_PROMPT,
            timeout=90,
        )
    except ClaudeCliNotInstalled:
        logger.warning("[llm-map] `claude` not in PATH — пропуск")
        return {}
    except ClaudeCliError as e:
        logger.warning("[llm-map] CLI error: %s", e)
        return {}
    elapsed = time.monotonic() - started

    try:
        decided = _parse_llm_response(raw, unresolved_clusters, name_pool)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("[llm-map] parse error: %s", e)
        return {}

    skipped = len(unresolved_clusters) - len(decided)
    logger.info(
        "[llm-map] meeting=%s unresolved=%d decided=%d skipped=%d elapsed=%.1fs",
        meeting_sid or "?", len(unresolved_clusters), len(decided), skipped, elapsed,
    )
    return decided
