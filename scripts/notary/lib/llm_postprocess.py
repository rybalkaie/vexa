"""Единый постпроцессинг-модуль на базе Claude (LLM-добивка после STT).

Содержит ТОЛЬКО функции, использующие LLM (Claude Haiku 4.5 / Sonnet 4.6),
дополняющие детерминированные шаги (`name_mapping` Source 1+2,
`render`-шаблон транскрипта).

Текущий состав модуля:
  - `map_speaker_names` — LLM-маппинг unresolved cluster'ов на имена
    участников (Ф2 плана `2026-05-28-meeting-notary-llm-...`).
  - `clarify_speakers_via_telegram` — отправка inline-keyboard уведомления
    Илье при низком confidence / нерешённых cluster'ах (Ф3 того же плана).
  - `parse_clarify_callback_data`, `parse_clarify_text_answer`,
    `apply_clarify_mapping` — парсеры + applier ответа Ильи (Ф3).

Будущий состав (по фазам того же плана):
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
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

# AlignedTurn используется только в сигнатурах типов finalize-path функций
# (`map_speaker_names`, `clarify_speakers_via_telegram`, `_build_samples_for_cluster`).
# Listener / clarify-worker зовут только parse_* / apply_* — поэтому `.align`
# (тянет pyannote через `.diarize`) импортируем лениво, чтобы `venv-cli` без
# pyannote/torch мог импортить этот модуль для парсинга callback_data.
if TYPE_CHECKING:
    from .align import AlignedTurn

from .claude_cli import (
    ClaudeCliError,
    ClaudeCliNotInstalled,
    call_claude_print,
)
from . import clarify_state
from . import telegram_api


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


# ---------- Ф3: Interactive clarification через Telegram ----------

CLARIFY_OTHER_TOKEN = "__other__"
# `callback_data` ограничен 64 байтами (Telegram). Используем короткий префикс
# `cl:` и индекс кнопки вместо имени — так умещается длинный sessionUid.
# Формат: `cl:<meeting_id>:<cluster_idx>:<name_idx>`, где `<name_idx>` ∈
# `0..len(name_options)-1` или строка `o` (= __other__).
_CALLBACK_PREFIX = "cl:"

# Ограничение длины sample-реплики, попадающей в Telegram. Защита от длинного
# сообщения, плюс не сливаем весь транскрипт в чат.
_SAMPLE_MAX_CHARS = 220
_SAMPLES_PER_CLUSTER = 3

# 4096 — лимит Telegram. С запасом на форматирование/HTML — режем ~3500.
_TELEGRAM_MSG_MAX = 3500

# Префикс clarify-сообщения — публичная константа.
# meetings_listener.py использует её для роутинга Reply на clarify-сообщения
# (отличая их от Reply на блок 📅, который идёт в apply_reply flow).
CLARIFY_MSG_PREFIX = "\U0001F399"  # 🎙 STUDIO MICROPHONE


# --- Парсеры ответа (callback + текст) ----------------------------------

PARSE_TEXT_FALLBACK_SYSTEM_PROMPT = """Ты помогаешь распарсить ответ Ильи на уточнение имён спикеров.

Контекст: бот спросил «Спикер N = кто из <список имён>?». Илья ответил свободным текстом.
Твоя задача: вытащить пары `cluster_index → name` ИЛИ маркер «не знаю» для каждого кластера.

Правила:
- Имена бери ТОЛЬКО из переданного списка `name_pool` (никаких новых).
- cluster_index — это число от 1 до N (как в подсказке «Спикер 1», «Спикер 2», …).
- Если в ответе нет уверенного маппинга — оставь cluster без имени (просто не включай в массив).
- «не знаю» / «пропусти» / «не помню» / «без понятия» → не возвращай этот cluster.

Формат ответа — СТРОГО JSON-массив (без markdown, без комментариев):
[
  {"cluster_index": 3, "name": "Дарья Набережная"},
  {"cluster_index": 1, "name": "Илья Рыбалка"}
]
Без кавычек вокруг массива, без префиксов/постфиксов."""


def parse_clarify_callback_data(
    data: str,
    *,
    meeting_id: str,
    cluster_keys: list[str],
    name_options_per_cluster: dict[str, list[str]],
) -> Optional[tuple[str, Optional[str]]]:
    """Парсит `callback_data` от inline-кнопки.

    Возвращает:
      `(cluster_key, name)` если кнопка с именем,
      `(cluster_key, None)` если кнопка «Другое» (__other__),
      `None` если data чужая / не парсится / meeting_id не совпал.

    Контракт data: `cl:<meeting_id>:<cluster_idx>:<name_idx_or_'o'>`.
    `meeting_id` хешируется в 8 hex-символов через `_short_id` (см. ниже),
    чтобы влезть в 64 байта Telegram. На приёме сверяем с собственным хешем.
    """
    if not isinstance(data, str) or not data.startswith(_CALLBACK_PREFIX):
        return None
    body = data[len(_CALLBACK_PREFIX):]
    parts = body.split(":")
    if len(parts) != 3:
        return None
    mid_short, cluster_idx_s, name_idx_s = parts
    if mid_short != _short_id(meeting_id):
        return None
    try:
        cluster_idx = int(cluster_idx_s)
    except ValueError:
        return None
    if cluster_idx < 0 or cluster_idx >= len(cluster_keys):
        return None
    cluster_key = cluster_keys[cluster_idx]
    options = name_options_per_cluster.get(cluster_key) or []
    if name_idx_s == "o":
        return (cluster_key, None)  # __other__
    try:
        name_idx = int(name_idx_s)
    except ValueError:
        return None
    if name_idx < 0 or name_idx >= len(options):
        return None
    return (cluster_key, options[name_idx])


def _strip_markdown_fence(raw: str) -> str:
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw


def _regex_parse_text_answer(
    text: str,
    cluster_label_to_key: dict[str, str],
    name_pool: list[str],
) -> dict[str, str]:
    """Простой regex-парсер 4 форматов ответа Ильи.

    Понимает (case-insensitive по «Спикер»):
      - «Спикер 3 = Дарья», «Спикер 3 - Дарья», «Спикер 3 — Дарья», «спикер 3: Дарья»
      - «3 — Дарья», «3 = Дарья», «3: Дарья»
      - «Дарья — это 3», «Дарья = 3», «Дарья это спикер 3»
      - «не знаю» / «пропусти» / «не помню» — возвращает пустой dict

    `cluster_label_to_key` маппит «Спикер 3» (1-based по индексу в .md) → SPEAKER_NN.
    """
    if not text or not text.strip():
        return {}
    text_norm = text.strip()
    # Маркеры «не знаю» — короткое замыкание.
    if re.search(r"(?i)\b(не\s+знаю|пропусти|не\s+помню|без\s+понятия|пас)\b", text_norm):
        return {}

    name_by_lower = {n.lower(): n for n in name_pool}
    # Первое слово имени тоже учитываем (Илья → «Илья Рыбалка»).
    first_word_to_full: dict[str, str] = {}
    for n in name_pool:
        fw = n.split()[0] if n.split() else ""
        if fw and fw.lower() not in name_by_lower:
            first_word_to_full[fw.lower()] = n

    def resolve_name(s: str) -> Optional[str]:
        s_low = s.strip().strip(",.;:").lower()
        if not s_low:
            return None
        if s_low in name_by_lower:
            return name_by_lower[s_low]
        return first_word_to_full.get(s_low)

    out: dict[str, str] = {}

    # Универсальная регулярка под все 3 группы форматов. cluster_idx = группа N,
    # name = группа M (либо обратный порядок).
    # Допускаем '=', '-', '—', ':' как разделитель + любое количество пробелов.
    sep = r"\s*(?:=|-|—|:|это|—\s*это)\s*"

    # Формат A: «Спикер N <sep> <name>» / «спикер N <sep> <имя>»
    for m in re.finditer(r"(?i)спикер\s*(\d+)" + sep + r"([А-Яа-яЁёA-Za-z][\w\s\-]*)", text_norm):
        idx_s, raw_name = m.group(1), m.group(2)
        label = f"Спикер {idx_s}"
        if label in cluster_label_to_key:
            # Берём только первое слово (или два) после разделителя — без хвоста.
            # «Дарья Набережная,» → имя = «Дарья Набережная».
            name_token = re.split(r"[.,;]|\s—|\sили", raw_name, maxsplit=1)[0].strip()
            name = resolve_name(name_token) or resolve_name(name_token.split()[0])
            if name:
                out[cluster_label_to_key[label]] = name

    # Формат B: «N <sep> <name>» (без «Спикер»), но только в начале строки/после переноса.
    for m in re.finditer(r"(?:^|\n)\s*(\d+)" + sep + r"([А-Яа-яЁёA-Za-z][\w\s\-]*)", text_norm):
        idx_s, raw_name = m.group(1), m.group(2)
        label = f"Спикер {idx_s}"
        if label in cluster_label_to_key and cluster_label_to_key[label] not in out:
            name_token = re.split(r"[.,;]|\s—|\sили", raw_name, maxsplit=1)[0].strip()
            name = resolve_name(name_token) or resolve_name(name_token.split()[0])
            if name:
                out[cluster_label_to_key[label]] = name

    # Формат C: «<name> <sep> N» или «<name> это спикер N» / «<name> — это 3».
    # Разделитель составной: `[—\-=:]` + опциональное «это» + опц. вторая часть
    # разделителя + опц. «спикер». Покрывает все варианты в smoke-кейсе.
    for m in re.finditer(
        r"(?i)([А-Яа-яЁёA-Za-z][\w\-]*(?:\s[А-Яа-яЁёA-Za-z][\w\-]*)?)"
        r"\s*[—\-=:]?\s*(?:это\s*)?(?:[—\-=:]\s*)?(?:спикер\s*)?(\d+)",
        text_norm,
    ):
        raw_name, idx_s = m.group(1), m.group(2)
        label = f"Спикер {idx_s}"
        if label in cluster_label_to_key and cluster_label_to_key[label] not in out:
            name = resolve_name(raw_name) or resolve_name(raw_name.split()[0])
            if name:
                out[cluster_label_to_key[label]] = name

    return out


def _llm_parse_text_answer(
    text: str,
    cluster_keys_ordered: list[str],
    cluster_label_to_key: dict[str, str],
    name_pool: list[str],
    *,
    meeting_sid: Optional[str] = None,
) -> dict[str, str]:
    """LLM-fallback на случай нестандартной формулировки.

    Возвращает `{cluster_key: name}` после валидации против `name_pool`.
    Тихий fail на ошибке CLI / парса — `{}`.
    """
    if not text.strip():
        return {}
    label_lines = "\n".join(
        f"  - Спикер {i + 1} (cluster_index={i + 1})" for i in range(len(cluster_keys_ordered))
    )
    user_prompt = (
        f"Список имён участников: {', '.join(name_pool)}\n\n"
        f"Кластеры на встрече:\n{label_lines}\n\n"
        f"Ответ Ильи: «{text.strip()}»"
    )
    try:
        raw = call_claude_print(
            user_prompt,
            system=PARSE_TEXT_FALLBACK_SYSTEM_PROMPT,
            timeout=45,
        )
    except ClaudeCliNotInstalled:
        logger.warning("[clarify-parse-llm] `claude` not in PATH — fallback skipped")
        return {}
    except ClaudeCliError as e:
        logger.warning("[clarify-parse-llm] meeting=%s CLI error: %s", meeting_sid or "?", e)
        return {}

    raw = _strip_markdown_fence(raw)
    start = raw.find("[")
    if start < 0:
        logger.warning("[clarify-parse-llm] meeting=%s no JSON array", meeting_sid or "?")
        return {}
    try:
        parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as e:
        logger.warning("[clarify-parse-llm] meeting=%s parse error: %s", meeting_sid or "?", e)
        return {}
    if not isinstance(parsed, list):
        return {}

    name_set = set(name_pool)
    out: dict[str, str] = {}
    used_names: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("cluster_index"))
        except (TypeError, ValueError):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name not in name_set:
            continue
        if name in used_names:
            continue
        label = f"Спикер {idx}"
        cluster_key = cluster_label_to_key.get(label)
        if not cluster_key or cluster_key in out:
            continue
        out[cluster_key] = name
        used_names.add(name)
    return out


def parse_clarify_text_answer(
    text: str,
    *,
    cluster_keys_ordered: list[str],
    cluster_label_to_key: dict[str, str],
    name_pool: list[str],
    meeting_sid: Optional[str] = None,
) -> dict[str, str]:
    """Парсер текстового ответа: regex → LLM-fallback. Возвращает `{cluster_key: name}`.

    `cluster_keys_ordered` — порядок SPEAKER_NN ровно как в .md (Спикер 1,2,3...).
    `cluster_label_to_key` — `{"Спикер 3": "SPEAKER_02"}` (1-based human индекс).
    """
    regex_hits = _regex_parse_text_answer(text, cluster_label_to_key, name_pool)
    if regex_hits:
        logger.info("[clarify-parse] meeting=%s regex hits=%d", meeting_sid or "?", len(regex_hits))
        return regex_hits
    # regex не сработал — LLM-fallback.
    llm_hits = _llm_parse_text_answer(
        text, cluster_keys_ordered, cluster_label_to_key, name_pool, meeting_sid=meeting_sid,
    )
    logger.info("[clarify-parse] meeting=%s llm-fallback hits=%d", meeting_sid or "?", len(llm_hits))
    return llm_hits


# --- Atomic apply mapping в transcript-файл ----------------------------

def apply_clarify_mapping_to_transcript(
    transcript_path: Path,
    label_to_name: dict[str, str],
) -> bool:
    """Переписывает .md файл, заменяя `**[ts] Спикер N:**` → `**[ts] <Имя>:**`.

    `label_to_name`: `{"Спикер 3": "Дарья Набережная", ...}`.

    Возвращает True если файл изменился. Атомарная запись через `tempfile + rename`.
    Не падает если файла нет — возвращает False.

    Завязка на формат `render.py`: `**[<ts>] <label>:**` с label ровно
    в виде «Спикер N». render.py:_speaker_label это гарантирует.
    """
    if not transcript_path.exists():
        logger.warning("[clarify-apply] transcript missing: %s", transcript_path)
        return False
    if not label_to_name:
        return False
    try:
        text = transcript_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[clarify-apply] read failed: %s", e)
        return False

    new_text = text
    for label, name in label_to_name.items():
        # `**[01:23] Спикер 3:**`  → `**[01:23] Дарья Набережная:**`
        pat = re.compile(
            r"(\*\*\[\d{2}:\d{2}(?::\d{2})?\] )"
            + re.escape(label)
            + r"(:\*\*)"
        )
        new_text, n = pat.subn(r"\g<1>" + name + r"\g<2>", new_text)
        logger.info("[clarify-apply] %s → %s replaced=%d", label, name, n)

    if new_text == text:
        return False

    # Атомарная запись в той же папке, чтобы os.rename был атомарным (один FS).
    target_dir = transcript_path.parent
    fd, tmp = tempfile.mkstemp(
        prefix=f".{transcript_path.name}.", suffix=".tmp", dir=str(target_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, transcript_path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return True


# --- Хелперы построения сообщения ---------------------------------------

def _short_id(meeting_id: str) -> str:
    """8-символьный детерминированный хеш `meeting_id` для callback_data.

    Telegram режет callback_data на 64 байтах. sessionUid типа
    `auto-tm-1779869180376-20260527T154900Z` (44 байта) + префикс + индексы =
    переполнение. Хеш 8 hex-символов → 8 байт. Сверка на приёме —
    `parse_clarify_callback_data`.
    """
    import hashlib
    return hashlib.sha1(meeting_id.encode("utf-8")).hexdigest()[:8]


def _truncate_sample(s: str, max_chars: int = _SAMPLE_MAX_CHARS) -> str:
    s = s.strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _format_timecode(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _build_samples_for_cluster(
    turns: list[AlignedTurn],
    cluster_key: str,
    *,
    max_samples: int = _SAMPLES_PER_CLUSTER,
) -> list[str]:
    """Берёт до N самых длинных реплик cluster'а, форматирует как `[ts] «текст»`."""
    bucket: list[AlignedTurn] = [
        t for t in turns if t.speaker == cluster_key and t.text and t.text.strip()
    ]
    bucket.sort(key=lambda t: len(t.text.split()), reverse=True)
    out: list[str] = []
    for t in bucket[:max_samples]:
        ts = _format_timecode(t.start)
        out.append(f"[{ts}] «{_truncate_sample(t.text)}»")
    return out


def _build_clarify_message_text(
    series: str,
    date_str: str,
    unclear_clusters: dict[str, dict],
) -> str:
    """Markdown-ish текст сообщения для Ильи. Telegram parse_mode НЕ используем,
    чтобы не залипнуть на эскейпинге символов в именах/репликах.
    """
    series = series or "—"
    n_clusters = len(unclear_clusters)
    suffix = "" if n_clusters == 1 else ("а" if 2 <= n_clusters <= 4 else "ов")
    lines: list[str] = [
        f"{CLARIFY_MSG_PREFIX} Встреча «{series}» от {date_str}",
        f"Нужны имена: {n_clusters} спикер{suffix}.",
        "",
    ]
    for cluster_key, data in unclear_clusters.items():
        speaker_label = data.get("speaker_label_in_md", cluster_key)
        confidence = data.get("confidence")
        if confidence is not None:
            try:
                conf_pct = int(round(float(confidence) * 100))
                lines.append(f"▸ {speaker_label} (уверенность {conf_pct}%):")
            except (TypeError, ValueError):
                lines.append(f"▸ {speaker_label}:")
        else:
            lines.append(f"▸ {speaker_label}:")
        for sample in data.get("samples", []):
            lines.append(f"  • {sample}")
        lines.append("")
    lines.append("Нажми кнопку или ответь текстом: «Спикер 3 = Дарья».")
    text = "\n".join(lines).rstrip()
    # Аккуратный clamp на лимит сообщения (защита от очень длинных samples).
    if len(text) > _TELEGRAM_MSG_MAX:
        text = text[: _TELEGRAM_MSG_MAX - 1] + "…"
    return text


def _build_clarify_inline_keyboard(
    meeting_id: str,
    cluster_keys_ordered: list[str],
    unclear_clusters: dict[str, dict],
) -> dict:
    """Inline keyboard: на каждый cluster — ряд кнопок-имён + кнопка «Другое».

    Кнопки длинных имён режутся по 30 символов, чтобы влезли на телефоне.
    """
    mid_short = _short_id(meeting_id)
    rows: list[list[dict]] = []
    for cluster_idx, cluster_key in enumerate(cluster_keys_ordered):
        data = unclear_clusters.get(cluster_key) or {}
        speaker_label = data.get("speaker_label_in_md", cluster_key)
        rows.append([{"text": f"— {speaker_label} —", "callback_data": _CALLBACK_PREFIX + "h"}])
        # Заголовочная кнопка-разделитель. callback_data `cl:h` отфильтруется
        # парсером (не матчит формат cluster_idx:name_idx).
        options: list[str] = data.get("name_options") or []
        # Раскладываем по 1 кнопке в ряд (имена длинные на русском).
        for name_idx, name in enumerate(options):
            short = name if len(name) <= 30 else name[:29] + "…"
            cb = f"{_CALLBACK_PREFIX}{mid_short}:{cluster_idx}:{name_idx}"
            rows.append([{"text": short, "callback_data": cb}])
        rows.append([
            {
                "text": "Другое (ответь текстом)",
                "callback_data": f"{_CALLBACK_PREFIX}{mid_short}:{cluster_idx}:o",
            }
        ])
    return telegram_api.build_inline_keyboard(rows)


# --- Главная функция ----------------------------------------------------

def _is_clarify_enabled() -> bool:
    raw = (os.environ.get("ENABLE_LLM_CLARIFY") or "").strip().lower()
    return raw not in ("0", "false", "no")


def clarify_speakers_via_telegram(
    meeting_id: str,
    turns: list[AlignedTurn],
    speaker_confidence: dict[str, float],
    cluster_to_name: dict[str, str],
    expected_participants: list[str],
    panel_participants: list[str],
    meta: dict,
    transcript_path: Path,
) -> Optional[Path]:
    """Если есть unresolved или low-confidence cluster'ы — шлёт Илье уведомление с inline keyboard.

    Поток:
      1. Считает unresolved = (cluster ∉ cluster_to_name) и low-conf
         (cluster ∈ cluster_to_name, но confidence < CLARIFY_THRESHOLD).
      2. Если таких нет — return None, ничего не делает.
      3. Если ENABLE_LLM_CLARIFY=0 — return None (финализация идёт «как есть»).
      4. Если нет TELEGRAM_NOTARIUS_BOT_TOKEN / TELEGRAM_CHAT_ID — лог + None (не блокер).
      5. Иначе: формирует сообщение, шлёт через Bot API, записывает
         `_pending_clarification/<meeting_id>.json` атомарно.
      6. НЕ блокирует поток — finalize-meeting.py продолжает с текущими именами
         (unresolved отрендерятся как «Спикер N»). Worker подберёт ответ позже.

    Возвращает `Path` сохранённого state-файла (или None если ничего не делали).

    Реализация Ф3 плана `2026-05-28-meeting-notary-llm-...`.
    """
    if not _is_clarify_enabled():
        logger.info("[clarify] disabled by ENABLE_LLM_CLARIFY=0")
        return None

    # Порог из env.
    try:
        threshold = float(os.environ.get("CLARIFY_THRESHOLD", "0.7"))
    except ValueError:
        threshold = 0.7

    # Какие cluster'ы реально есть в этом транскрипте.
    clusters_in_md: list[str] = []
    for t in turns:
        if t.speaker and t.speaker not in clusters_in_md:
            clusters_in_md.append(t.speaker)

    # Маппинг cluster → «Спикер N» (1-based индекс в .md), как делает render.py.
    cluster_to_human_label: dict[str, str] = {
        cluster_key: f"Спикер {idx + 1}"
        for idx, cluster_key in enumerate(clusters_in_md)
    }

    unclear: dict[str, dict] = {}
    for cluster_key in clusters_in_md:
        name = cluster_to_name.get(cluster_key)
        if name is None:
            # Полностью неразрешённый — точно нужен clarify.
            unclear[cluster_key] = {
                "speaker_label_in_md": cluster_to_human_label[cluster_key],
                "confidence": None,
            }
            continue
        conf = speaker_confidence.get(cluster_key)
        if conf is not None and conf < threshold:
            # Имя есть, но LLM сам помечен «не уверен».
            unclear[cluster_key] = {
                "speaker_label_in_md": cluster_to_human_label[cluster_key],
                "confidence": conf,
                "current_guess": name,
            }

    if not unclear:
        logger.info(
            "[clarify] meeting=%s nothing to clarify (threshold=%.2f)",
            meeting_id, threshold,
        )
        return None

    # Бот + chat. Переиспользуем `@ilya_protocol_meeting_bot` —
    # тот же токен, что у meetings_listener'а (он же шлёт вечерний блок 📅
    # и принимает Reply на него; clarify-callback'и идут к нему же).
    bot_token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    chat_id_raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    if not bot_token or not chat_id_raw:
        logger.warning(
            "[clarify] meeting=%s low-conf clusters=%d, "
            "но TELEGRAM_NOTARIUS_BOT_TOKEN/TELEGRAM_(NOTARIUS_)CHAT_ID не заданы — пропуск",
            meeting_id, len(unclear),
        )
        return None
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        logger.warning("[clarify] meeting=%s TELEGRAM_CHAT_ID не число: %r", meeting_id, chat_id_raw)
        return None

    # name_pool: те же имена, что для map_speaker_names, плюс уже разрешённые
    # (иначе Илья не сможет назвать кого-то, кто уже привязан к другому cluster'у —
    # бот должен это разрешать, переразметка делается в apply_clarify_mapping).
    name_pool: list[str] = []
    seen: set[str] = set()
    for n in list(expected_participants) + list(panel_participants):
        if n and isinstance(n, str) and n not in seen:
            seen.add(n)
            name_pool.append(n)
    if not name_pool:
        logger.warning("[clarify] meeting=%s name_pool пустой — Илье не из чего выбирать", meeting_id)
        return None

    # Samples — реплики из turns для каждого unclear cluster'а.
    for cluster_key, data in unclear.items():
        data["samples"] = _build_samples_for_cluster(turns, cluster_key)
        data["name_options"] = list(name_pool)  # одни и те же варианты для всех

    # Сообщение + клавиатура.
    series = meta.get("series") or ""
    date_str = (meta.get("date") or (meta.get("startTs") or "")[:10] or "—")
    text = _build_clarify_message_text(series, date_str, unclear)
    cluster_keys_ordered = list(unclear.keys())
    reply_markup = _build_clarify_inline_keyboard(meeting_id, cluster_keys_ordered, unclear)

    try:
        result = telegram_api.send_message(
            bot_token, chat_id, text,
            reply_markup=reply_markup,
        )
    except telegram_api.TelegramApiError as e:
        logger.warning("[clarify] meeting=%s send failed: %s", meeting_id, e)
        return None
    message_id = int(result.get("message_id") or 0)

    # Сохранение state.
    try:
        timeout_s = int(os.environ.get("CLARIFY_TIMEOUT", "420"))
    except ValueError:
        timeout_s = 420
    sent_at = datetime.now(timezone.utc)
    deadline = sent_at + timedelta(seconds=timeout_s)

    # Ход 3 У4/У5: явная проверка прав на pending_dir ДО send'а сообщения.
    # Иначе сообщение Илье уйдёт, а state записать не сможем — Илья нажмёт,
    # worker найдёт пустой list_pending → silent loss. Лучше упасть здесь
    # с понятной диагностикой, чем оставить Илью без обратной связи.
    pending_root = clarify_state.resolve_pending_dir()
    try:
        pending_root.mkdir(parents=True, exist_ok=True)
        # Проверяем что реально можем писать — через tempfile, не race-условный
        # `.write-probe` (две параллельные финализации могли бы дёргать
        # unlink на одном файле — НОВ1 хода 4).
        with tempfile.NamedTemporaryFile(
            prefix=".write-probe.", dir=str(pending_root), delete=True,
        ) as _probe:
            _probe.write(b"ok")
    except OSError as e:
        logger.warning(
            "[clarify] meeting=%s pending_dir недоступен (%s): %s — clarify пропущен. "
            "Проверь права: chown dev:dev %s && chmod 755 %s",
            meeting_id, pending_root, e, pending_root, pending_root,
        )
        return None

    state = {
        "meeting_id": meeting_id,
        "transcript_path": str(transcript_path),
        "meta": {
            "series": series,
            "date": date_str,
            "sessionUid": meta.get("sessionUid"),
        },
        "unclear_clusters": {
            k: {
                "name_options": v.get("name_options", []),
                "samples": v.get("samples", []),
                "speaker_label_in_md": v.get("speaker_label_in_md"),
                "confidence": v.get("confidence"),
                "current_guess": v.get("current_guess"),
            }
            for k, v in unclear.items()
        },
        "cluster_keys_ordered": cluster_keys_ordered,
        "name_pool": name_pool,
        "chat_id": chat_id,
        "message_id": message_id,
        "sent_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeout_s": timeout_s,
        "deadline_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "pending",
        "resolved_via": None,
        "resolved_at": None,
        "applied_mapping": None,
    }
    saved_path = clarify_state.write_state(state)
    logger.info(
        "[clarify] sent meeting=%s clusters=%d message_id=%s state=%s",
        meeting_id, len(unclear), message_id, saved_path.name,
    )
    return saved_path
