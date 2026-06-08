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
  - `generate_protocol` — Claude Sonnet 4.6 рендерит структурированный
    протокол из транскрипта по методичке (Ф4 того же плана).
  - `regenerate_protocol_for_meeting` — обёртка: читает transcript с диска,
    зовёт `generate_protocol`, atomic-write `<date>-protokol.md`. Переиспользуется
    CLI `tools/regenerate-protocol.py`, Telegram-командой и hook'ом из
    clarify-worker.

Будущий состав (по фазам того же плана):
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

import fcntl
import hashlib
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
from . import glossary
from . import protocol_to_tg
from . import protocol_to_pdf
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

# Строка реплики транскрипта: `**[01:23] <label>:** текст` или
# `**[01:23:45] <label>:** текст`. Группы: (префикс с таймкодом)(label/имя)(суффикс).
# `<label>` ленивый до первого `:**` — имена/«Спикер N» двоеточий не содержат.
_TRANSCRIPT_SPEAKER_RE = re.compile(
    r"(\*\*\[\d{2}:\d{2}(?::\d{2})?\] )(.+?)(:\*\*)"
)


def remap_transcript_speakers(text: str, remap: dict[str, str]) -> str:
    """Ф4б: своп-безопасная замена меток/имён спикеров в теле транскрипта.

    `remap`: `{текущая_метка_или_имя: новое_имя}` (напр. `{"Илья": "Михаил",
    "Михаил": "Илья"}` для свопа авторства, или `{"Спикер 3": "Дарья"}`). В отличие
    от последовательных `subn` (которые на свопе схлопываются), делаем ОДИН проход:
    каждую строку `**[ts] X:**` смотрим в `remap` и заменяем X независимо — поэтому
    своп Илья↔Михаил применяется корректно. Если X нет в `remap` — строку не трогаем.
    """
    if not remap or not text:
        return text

    def _repl(m: "re.Match") -> str:
        cur = m.group(2)
        new = remap.get(cur)
        if new is None:
            return m.group(0)
        return m.group(1) + new + m.group(3)

    return _TRANSCRIPT_SPEAKER_RE.sub(_repl, text)


def apply_clarify_mapping_to_transcript(
    transcript_path: Path,
    label_to_name: dict[str, str],
) -> bool:
    """Переписывает .md файл, заменяя `**[ts] Спикер N:**` → `**[ts] <Имя>:**`.

    `label_to_name`: `{"Спикер 3": "Дарья Набережная", ...}`.

    Возвращает True если файл изменился. Атомарная запись через `tempfile + rename`.
    Не падает если файла нет — возвращает False.

    Завязка на формат `render.py`: `**[<ts>] <label>:**` с label ровно
    в виде «Спикер N». render.py:_speaker_label это гарантирует. Ф4б: замена
    своп-безопасна (один проход через `remap_transcript_speakers`) — clarify-кейс
    (метки→имена, ключи и значения не пересекаются) ведёт себя как прежде.
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

    new_text = remap_transcript_speakers(text, label_to_name)
    logger.info("[clarify-apply] remap labels=%d changed=%s",
                len(label_to_name), new_text != text)

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
    *,
    resolved_names: Optional[list[str]] = None,
) -> str:
    """Markdown-ish текст сообщения для Ильи. Telegram parse_mode НЕ используем,
    чтобы не залипнуть на эскейпинге символов в именах/репликах.

    `resolved_names` — кто уже определён в этой встрече. Добавляется первой
    строкой («уже определены: Илья, Михаил»), чтобы Илья видел контекст и
    не путался при ответе про оставшиеся cluster'ы.
    """
    series = series or "—"
    n_clusters = len(unclear_clusters)
    suffix = "" if n_clusters == 1 else ("а" if 2 <= n_clusters <= 4 else "ов")
    lines: list[str] = [
        f"{CLARIFY_MSG_PREFIX} Встреча «{series}» от {date_str}",
    ]
    if resolved_names:
        lines.append("Уже определены: " + ", ".join(resolved_names) + ".")
    lines.append(f"Нужны имена: {n_clusters} спикер{suffix}.")
    lines.append("")
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

    # Ф1-доработки (бывшая Ф7 INBOX#7): гейт clarify по «есть имя».
    # Раньше clarify шёл и на low-confidence (имя есть, но conf<threshold) —
    # это давало false-positive clarify на встречах вроде 29.05, где все
    # имена резолвлены, но LLM не дотянул до 0.7. Владелец принял: clarify
    # шлём ТОЛЬКО на полностью unresolved cluster'ы (`name is None`).
    # Low-conf клиента не дёргает; имя в `<date>.md` уже подставлено через
    # `_apply_clarify_mapping_to_transcript` (см. план 28.05 Ф3) — для
    # ситуации «решил оспорить low-conf» есть отдельный путь correction.
    unclear: dict[str, dict] = {}
    resolved_names_ordered: list[str] = []
    seen_resolved: set[str] = set()
    for cluster_key in clusters_in_md:
        name = cluster_to_name.get(cluster_key)
        if name is None:
            unclear[cluster_key] = {
                "speaker_label_in_md": cluster_to_human_label[cluster_key],
                "confidence": None,
            }
            continue
        # Имя есть — собираем в список «уже определены». low-conf явно
        # игнорируем (см. комментарий выше). Порядок — как в clusters_in_md.
        if name not in seen_resolved:
            seen_resolved.add(name)
            resolved_names_ordered.append(name)

    # Логируем для аудита: что именно сэкономили low-conf-уведомлений.
    suppressed_low_conf = 0
    if threshold > 0:
        for cluster_key, name in cluster_to_name.items():
            if cluster_key not in clusters_in_md:
                continue
            if cluster_key in unclear:
                continue
            conf = speaker_confidence.get(cluster_key)
            if conf is not None and conf < threshold:
                suppressed_low_conf += 1

    if not unclear:
        logger.info(
            "[clarify] meeting=%s nothing to clarify "
            "(all resolved; suppressed_low_conf=%d, threshold=%.2f)",
            meeting_id, suppressed_low_conf, threshold,
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

    # 5.4: сверка с people.md / expected_participants ПЕРЕД вопросом.
    # Если кластер однозначно ложится на известного участника (строгий 1:1) —
    # подставляем авто, не дёргаем Илью. 2+ совпадения = не угадываем (ask).
    try:
        people_md = protocol_to_tg._read_people_md()
        people_names = protocol_to_tg._extract_names_from_people(people_md) if people_md else []
    except Exception as e:  # noqa: BLE001
        logger.warning("[clarify] meeting=%s people.md read failed (5.4): %s", meeting_id, e)
        people_names = []
    auto_map = auto_resolve_known_speakers(
        unclear, resolved_names_ordered, name_pool,
        people_names=people_names, expected_participants=list(expected_participants),
    )
    if auto_map:
        label_to_name: dict[str, str] = {}
        for cluster_key, name in auto_map.items():
            label = unclear.get(cluster_key, {}).get("speaker_label_in_md")
            if label:
                label_to_name[label] = name
            unclear.pop(cluster_key, None)
            if name not in seen_resolved:
                seen_resolved.add(name)
                resolved_names_ordered.append(name)
        if label_to_name:
            try:
                file_updated = apply_clarify_mapping_to_transcript(transcript_path, label_to_name)
            except Exception as e:  # noqa: BLE001
                logger.warning("[clarify] meeting=%s auto-apply failed (5.4): %s", meeting_id, e)
                file_updated = False
            if file_updated:
                # Перегенерируем протокол, чтобы первичная доставка ушла уже
                # с подставленным именем (а не «Спикер N»).
                try:
                    proto_path = transcript_path.parent / f"{transcript_path.stem}-protokol.md"
                    regenerate_protocol_for_meeting(
                        transcript_path=transcript_path,
                        protocol_path=proto_path,
                        meeting_meta={
                            "series": meta.get("series") or "",
                            "date": meta.get("date") or (meta.get("startTs") or "")[:10] or "",
                            "sessionUid": meta.get("sessionUid"),
                            "expectedParticipants": list(expected_participants),
                            "participants": list(panel_participants),
                            "transcript_filename": transcript_path.name,
                        },
                        meeting_sid=meeting_id,
                    )
                except ProtocolGenerationError as e:
                    logger.warning("[clarify] meeting=%s auto-resolve regen failed (5.4): %s", meeting_id, e)
            logger.info(
                "[clarify] meeting=%s auto-resolved %d known speaker(s): %s",
                meeting_id, len(label_to_name), label_to_name,
            )
    if not unclear:
        logger.info("[clarify] meeting=%s all clusters auto-resolved (5.4) — no clarify", meeting_id)
        return None

    # Samples — реплики из turns для каждого unclear cluster'а.
    for cluster_key, data in unclear.items():
        data["samples"] = _build_samples_for_cluster(turns, cluster_key)
        data["name_options"] = list(name_pool)  # одни и те же варианты для всех

    # Сообщение + клавиатура.
    series = meta.get("series") or ""
    date_str = (meta.get("date") or (meta.get("startTs") or "")[:10] or "—")
    text = _build_clarify_message_text(
        series, date_str, unclear,
        resolved_names=resolved_names_ordered,
    )
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
        timeout_s = int(os.environ.get("CLARIFY_TIMEOUT", "86400"))
    except ValueError:
        timeout_s = 86400
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


# ---------- Ф4: генератор протокола встречи ------------------------------

# Модель для генерации протокола. Sonnet 4.6 — компромисс между качеством
# (структура, формулировки) и латентностью (на 21-минутном sales-quality
# ответ приходит за ~15-30 сек). Передаётся в `call_claude_print(model=...)`,
# который прокидывает `--model claude-sonnet-4-6` в subprocess.
PROTOCOL_GEN_MODEL = "claude-sonnet-4-6"

# Имя файла метода на диске (общий для мака и VPS). На маке живёт в
# `~/Projects/me/methods/`, на VPS — копируется через cron rsync (см.
# README раздел «Cron rsync метода»).
METHOD_FILE_NAME = "kak-delat-protokol-vstrechi.md"

# Дефолтные корни поиска метод-файла:
# - мак: `~/Projects/me/methods/` (там же, где живёт сам метод).
# - VPS: `/opt/meeting-notary/_methods/` (куда cron его кладёт).
# Можно переопределить env-переменной `MEETING_NOTARY_METHODS_DIR` (на VPS
# unit-файл задаёт её через EnvironmentFile=/srv/meeting-notary/.env.notary).
_METHODS_DIR_DEFAULTS = (
    os.path.expanduser("~/Projects/me/methods"),
    "/opt/meeting-notary/_methods",
)


GENERATE_PROTOCOL_BASE_PROMPT = """Ты редактор протокола встречи.

На вход тебе дан:
1. Стандарт оформления (ниже — секция «Метод» в формате markdown).
2. Транскрипт встречи (в пользовательском сообщении) — последовательность реплик в формате `**[ts] Имя:**` (либо `**[ts] Спикер N:**` если имя не известно).
3. Метаданные встречи (series, дата, длительность, участники) — в пользовательском сообщении.

Твоя задача: сгенерировать готовый .md-файл протокола строго по стандарту из секции «Метод».

Правила:
- НЕ выдумывай факты. Если в транскрипте чего-то нет — не пиши этого в протоколе.
- Если в транскрипте нет принятых решений по теме — НЕ пиши блок «Решения», пропусти его.
- Задачи (блок «Задачи») извлекай ТОЛЬКО те, что явно прозвучали как договорённости («сделаю X», «пришлю Y», «договорились что Z к пятнице»). Не додумывай задачи из общего смысла.
- Имена в задачах и решениях бери ровно как они в транскрипте. Если в транскрипте «Спикер 3» — оставь «Спикер 3» (не выдумывай имя). ИСКЛЮЧЕНИЕ: если ниже есть блок правок участников и правка переназначает автора реплики/задачи — следуй правке, а не транскрипту (в транскрипте бывают ошибки определения говорящего).
- Темы (## 1) ... ## 2) ...) группируй по СМЫСЛУ, а не по хронологии транскрипта.
- Сохраняй СУБСТАНТИВНЫЙ контекст пункта — то конкретное, что человек явно дал: основание/причину («потому что …»), канал/источник («лиды из РСЯ», «через кабинет YME»), состояние/способ («распределяем вручную», «склад занят»), объект («палетное хранение»). Не сворачивай это до пустого «обсудили вопрос лидов» — теряется суть, ради которой пункт и попал в протокол.
- Но это НЕ отмена сжатия: режь воду, повторы, болтовню, формулируй ёмко. Цель — «коротко, но с сохранением смысла», а не «длинно». Объём протокола должен оставаться компактным (см. длину ниже); добавляется не объём, а точность пункта.
- Длина: компактнее транскрипта в 5–10 раз.
- Каждый буллет тематического блока — на отдельной строке с ПУСТОЙ строкой между буллетами (иначе они склеятся в один параграф).
- Эмодзи-маркеры — только функциональные из стандарта (▪️ ▫️ 🔸 🟠). Никаких декоративных.
- Доменные термины пиши ТОЧНО по глоссарию проекта (ниже, после методички): не заменяй их на похожие по звучанию обычные слова.

Шапка протокола:
- Первая строка: `#протоколвстречи DD.MM.YYYY` (дата из метаданных, формат DD.MM.YYYY).
- Поля `**Встреча:**`, `**Длительность:**`, `**Участники:**`, `**Транскрипт:**`.
- Поле `**Транскрипт:**` — относительная markdown-ссылка `[<date>.md](<date>.md)` (имя файла транскрипта из метаданных).
- После шапки — горизонтальный разделитель `---`.

Ответь СТРОГО готовым markdown-файлом протокола — без markdown-обёртки ```markdown ... ```, без префиксов «вот протокол:», без объяснений. Только сам файл от первой строки `#протоколвстречи` до последней строки.

Метод (стандарт оформления):

"""


class ProtocolGenerationError(RuntimeError):
    """Сбой генерации протокола (CLI/Claude/IO)."""


def _load_method_text(*, override_dir: Optional[str] = None) -> str:
    """Читает метод-файл `kak-delat-protokol-vstrechi.md`.

    Порядок поиска:
      1. `override_dir` (если задан) — для тестов.
      2. env `MEETING_NOTARY_METHODS_DIR` — для VPS / кастомных сетапов.
      3. `_METHODS_DIR_DEFAULTS` (мак → VPS-каталог) — первый существующий.

    `ProtocolGenerationError` если файл не найден ни в одном кандидате.
    """
    candidates: list[str] = []
    if override_dir:
        candidates.append(override_dir)
    env_dir = (os.environ.get("MEETING_NOTARY_METHODS_DIR") or "").strip()
    if env_dir:
        candidates.append(os.path.expanduser(env_dir))
    candidates.extend(_METHODS_DIR_DEFAULTS)

    tried: list[str] = []
    for d in candidates:
        p = Path(d) / METHOD_FILE_NAME
        tried.append(str(p))
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except OSError as e:
                raise ProtocolGenerationError(
                    f"method-файл найден ({p}) но не читается: {e}"
                ) from e
    raise ProtocolGenerationError(
        "method-файл `%s` не найден. Проверял: %s. "
        "На VPS нужен cron rsync `~/Projects/me/methods/` → "
        "`/opt/meeting-notary/_methods/` (см. README раздел «Cron rsync метода»)."
        % (METHOD_FILE_NAME, ", ".join(tried))
    )


def _format_protocol_user_prompt(
    transcript_md: str,
    meeting_meta: dict,
    *,
    series_memory: Optional[str] = None,
) -> str:
    """Собирает user-prompt: метаданные + (Ф7) справка памяти серии + транскрипт.

    Метаданные специально дублируют шапку транскрипта (Sonnet не должен полагаться
    на её парсинг — там может не быть `Длительность`, если STT-pipeline её не положил).

    `series_memory` (Ф7 7.3/7.4) — готовый справочный блок выжимок прошлых встреч
    серии (из `series_memory.format_memory_block`). Идёт ПЕРЕД транскриптом с явной
    дисциплиной «справка, не факт». None/"" → блок не добавляется.
    """
    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"
    # Парсим в DD.MM.YYYY для подсказки модели (она всё равно сама форматирует
    # шапку, но дадим готовый формат, чтобы не было «27.5.2026»).
    date_dmy = date
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        y, m, d = date.split("-")
        date_dmy = f"{d}.{m}.{y}"

    duration = meeting_meta.get("duration") or meeting_meta.get("durationMin")
    duration_str = ""
    if duration is not None:
        duration_str = f"{duration} мин" if not str(duration).endswith("мин") else str(duration)

    expected = meeting_meta.get("expectedParticipants") or []
    participants = meeting_meta.get("participants") or []
    # Слияние без дублей с сохранением порядка (expected first).
    seen: set[str] = set()
    merged: list[str] = []
    for n in list(expected) + list(participants):
        if isinstance(n, str) and n and n not in seen:
            seen.add(n)
            merged.append(n)
    participants_str = ", ".join(merged) if merged else "—"

    transcript_filename = meeting_meta.get("transcript_filename") or f"{date}.md"

    meta_block = [
        "Метаданные встречи:",
        f"- series: {series}",
        f"- date: {date} (для шапки используй формат DD.MM.YYYY → {date_dmy})",
    ]
    if duration_str:
        meta_block.append(f"- duration: {duration_str}")
    meta_block.append(f"- participants: {participants_str}")
    meta_block.append(f"- transcript_filename: {transcript_filename}")

    # У4 (цикл5/ход3): correction_instruction (если есть) идёт ПЕРЕД
    # транскриптом с явным префиксом приоритета — иначе Sonnet может
    # проигнорировать правку, опираясь только на общие правила из system_prompt.
    correction = meeting_meta.get("correction_instruction")
    correction_block = ""
    if isinstance(correction, str) and correction.strip():
        correction_block = (
            "\n\nКРИТИЧЕСКАЯ ИНСТРУКЦИЯ ОТ ПОЛЬЗОВАТЕЛЯ ДЛЯ ЭТОЙ КОРРЕКЦИИ "
            "(приоритет выше общих правил методички):\n"
            + correction.strip()
            + "\nУчти её при формировании итогового протокола."
        )

    # Ф4 (FB5/FB6/FB7): правки участников из чата. В ОТЛИЧИЕ от
    # `correction_instruction` (доверенная команда владельца, приоритет над
    # методичкой) — это НЕДОВЕРЕННЫЕ ДАННЫЕ от участников встречи. Блок уже
    # собран и обрамлён anti-injection-рамкой в `feedback_reissue` (правки =
    # данные, инструкции внутри текста игнорировать), текст каждой правки
    # санитизирован. Сюда приходит готовая строка — вставляем как есть.
    feedback_block_raw = meeting_meta.get("feedback_edits_block")
    feedback_block = ""
    if isinstance(feedback_block_raw, str) and feedback_block_raw.strip():
        feedback_block = "\n\n" + feedback_block_raw.strip()

    memory_block = ""
    if isinstance(series_memory, str) and series_memory.strip():
        memory_block = "\n\n" + series_memory.strip()

    # Ф6 (FB10): выученные из правок участников терм-замены ЭТОЙ серии. Тот же
    # канал, что `series_memory` — справочный блок-ДАННЫЕ ПЕРЕД транскриптом (не
    # команда модели, не факт). Источник — append-only лог на серию; активные
    # правила применяются, откаченные — нет. Single chokepoint: покрывает finalize/
    # clarify/regenerate/reissue. Best-effort и под собственным гейтом — при
    # выключенном `ENABLE_FEEDBACK_LEARNING` / отсутствии правил блок пуст (поведение
    # генерации не меняется, как и до Ф6).
    learned_block = ""
    try:
        from . import feedback_learning  # noqa: PLC0415  (lazy: избегаем цикла импорта)

        lb = feedback_learning.format_learned_terms_block(meeting_meta.get("series"))
        if lb.strip():
            learned_block = "\n\n" + lb.strip()
    except Exception:  # noqa: BLE001  (самообучение не должно ронять генерацию)
        learned_block = ""

    return (
        "\n".join(meta_block)
        + memory_block
        + learned_block
        + correction_block
        + feedback_block
        + "\n\nТранскрипт:\n\n"
        + transcript_md
    )


def _is_protocol_generation_enabled() -> bool:
    """Гейт `ENABLE_PROTOCOL_GENERATION` (дефолт ON; `0/false/no` → OFF)."""
    raw = (os.environ.get("ENABLE_PROTOCOL_GENERATION") or "").strip().lower()
    return raw not in ("0", "false", "no")


# Поле `**Длительность:**` в шапке протокола (метод `kak-delat-protokol-vstrechi`).
# Захватываем префикс «**Длительность:** » и заменяем значение целиком.
_DURATION_HEADER_RE = re.compile(
    r"^(\s*\*\*Длительность:\*\*[ \t]*).*$", re.MULTILINE
)


def _normalize_protocol_duration(
    protocol_text: str,
    meeting_meta: dict,
    *,
    transcript_json_path=None,
) -> str:
    """FU-12: тело протокола показывает то же «чистое время», что шапка/подпись.

    Контекст бага (02.06): Sonnet кладёт в `**Длительность:**` тела wall-time из
    календаря (`meta.duration`, присутствие бота), а подпись PDF / шапка TG-текста
    показывают `compute_duration_label` (чистое речевое время). Два разных числа в
    одном документе читаются как ошибка. Здесь приводим тело к ЕДИНОМУ источнику —
    `protocol_to_tg.compute_duration_label` (тот же, что подпись и `_format_header`).

    Перезаписываем тело ТОЛЬКО когда доступно именно чистое время (источники 1–3
    в `compute_duration_label`; `presence_fallback=False` глушит присутствие
    endTs−startTs и durationLabel). Иначе (старая встреча без `recording.*`) тело
    НЕ трогаем: вписать «грязный» wall-time нельзя — подпись для таких встреч берёт
    чистое время из transcript-json, и тело снова разошлось бы с ней (баг, найденный
    циклом5). Для свежих встреч `meta.recording.first/lastSpeechMs` проставлен в
    finalize ДО генерации → тело совпадает с подписью точь-в-точь.
    """
    if not protocol_text:
        return protocol_text
    label = protocol_to_tg.compute_duration_label(
        meeting_meta or {}, transcript_json_path=transcript_json_path,
        presence_fallback=False,
    )
    if not label or label == "—":
        return protocol_text
    new_text, n = _DURATION_HEADER_RE.subn(
        lambda m: f"{m.group(1)}{label}", protocol_text
    )
    if n == 0:
        logger.info(
            "[protocol] FU-12: поле `**Длительность:**` не найдено — нормализация пропущена"
        )
    return new_text


def generate_protocol(
    transcript_md: str,
    meeting_meta: dict,
    *,
    method_text: Optional[str] = None,
    timeout: int = 180,
    meeting_sid: Optional[str] = None,
    series_memory: Optional[str] = None,
) -> str:
    """Генерирует .md-файл протокола встречи из транскрипта через Claude Sonnet 4.6.

    Параметры:
      transcript_md: содержимое финального транскрипта (с применёнными именами).
      meeting_meta: dict с полями `series` (str|None), `date` (YYYY-MM-DD),
        `duration` или `durationMin`, `expectedParticipants` (list[str]),
        `participants` (list[str]), `transcript_filename` (опц., имя файла .md
        транскрипта; дефолт `<date>.md`).
      method_text: содержимое метод-файла. None → читаем с диска через
        `_load_method_text()` (это default-путь; явный текст нужен только тестам).
      timeout: timeout subprocess `claude --print` (default 180s — генерация
        протокола занимает 15–60s, запас на медленные ответы).
      meeting_sid: для structured-лога.

    Возвращает: готовый markdown-текст протокола (от строки `#протоколвстречи`).

    Бросает `ProtocolGenerationError` если: CLI недоступен, claude вернул
    пустой/невалидный ответ, метод-файл не найден.

    Не делает atomic write — caller (finalize-meeting.py, regenerate-CLI,
    clarify hook) решает куда писать и через какой механизм.
    """
    if method_text is None:
        method_text = _load_method_text()

    # FU-11: глоссарий проекта в КОНЕЦ system-prompt (после методички) —
    # отдельной секцией, чтобы Sonnet писал доменные термины точно.
    system_prompt = (
        GENERATE_PROTOCOL_BASE_PROMPT
        + method_text
        + "\n\n---\n\n"
        + glossary.PROJECT_GLOSSARY_PROMPT_BLOCK
    )
    user_prompt = _format_protocol_user_prompt(
        transcript_md, meeting_meta, series_memory=series_memory,
    )

    started = time.monotonic()
    try:
        raw = call_claude_print(
            user_prompt,
            system=system_prompt,
            timeout=timeout,
            model=PROTOCOL_GEN_MODEL,
        )
    except ClaudeCliNotInstalled as e:
        raise ProtocolGenerationError(
            "`claude` CLI не найден в PATH — генерация протокола невозможна"
        ) from e
    except ClaudeCliError as e:
        logger.warning(
            "[protocol] failed meeting=%s error=%s retry=0",
            meeting_sid or "?", str(e)[:200],
        )
        raise ProtocolGenerationError(f"claude --print: {e}") from e
    elapsed = time.monotonic() - started

    text = raw.strip()
    # Срезаем markdown-fence на случай если Sonnet всё-таки обернул ответ.
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    if not text.startswith("#протоколвстречи"):
        # Терпимо: модель могла начать с «Вот протокол:» — обрезаем всё до
        # шапки. Якорь — `#протоколвстречи` (точное соответствие методичке),
        # НЕ просто `^#` (иначе «# Здравствуйте! ...» в начале попадёт в файл).
        m = re.search(r"^#протоколвстречи\b", text, flags=re.MULTILINE)
        if m:
            text = text[m.start():]
        else:
            logger.warning(
                "[protocol] failed meeting=%s error=output-without-header retry=0",
                meeting_sid or "?",
            )
            raise ProtocolGenerationError(
                "Sonnet вернул ответ без шапки протокола (`#протоколвстречи`)"
            )

    # FU-11: детерминированный пост-проход доменных терминов (остаточные
    # перевирания мимо STT-словаря и подсказки).
    text = glossary.apply_glossary_corrections(text)
    # FU-12: тело показывает то же чистое время, что подпись/шапка.
    text = _normalize_protocol_duration(text, meeting_meta)
    # Ф4а: постоянный дисклеймер авторства в начало тела (.md + PDF читают тело;
    # TG-текст инжектит свой вариант). Идемпотентно — повторная генерация не
    # плодит дубль. Ставится ДО первой секции → не уходит в series_memory-дайджест.
    text = protocol_to_tg.insert_protocol_disclaimer(text)

    logger.info(
        "[protocol] generated meeting=%s elapsed=%.1fs prompt_len=%d output_len=%d model=%s",
        meeting_sid or "?", elapsed, len(system_prompt) + len(user_prompt),
        len(text), PROTOCOL_GEN_MODEL,
    )
    return text


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic write через `tempfile + os.rename` в той же директории.

    Тот же паттерн, что `apply_clarify_mapping_to_transcript` — гарантирует,
    что параллельная финализация другой встречи не увидит полу-файла.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def regenerate_protocol_for_meeting(
    transcript_path: Path,
    protocol_path: Path,
    meeting_meta: dict,
    *,
    method_text: Optional[str] = None,
    meeting_sid: Optional[str] = None,
    series_memory: Optional[str] = None,
) -> Path:
    """Высокоуровневая обёртка: читает transcript → генерирует → atomic write.

    Переиспользуется:
      - CLI `tools/regenerate-protocol.py <series> <date>` (backfill / отладка).
      - Telegram-команда «протокол <series> <date>» в meetings_listener'е.
      - Hook из `clarify_worker._apply_resolution` после resolved-mapping'а.

    `meeting_meta` должен содержать минимум `date` (или `startTs`). Имя файла
    транскрипта подставляется автоматически — `transcript_path.name`.

    Возвращает `protocol_path` (на успех). Бросает `ProtocolGenerationError`
    при сбое чтения transcript'а или генерации.
    """
    if not transcript_path.is_file():
        raise ProtocolGenerationError(
            f"transcript-файл не найден: {transcript_path}"
        )
    try:
        transcript_md = transcript_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ProtocolGenerationError(
            f"transcript-файл не читается ({transcript_path}): {e}"
        ) from e
    if not transcript_md.strip():
        raise ProtocolGenerationError(
            f"transcript-файл пустой: {transcript_path}"
        )

    enriched_meta = dict(meeting_meta)
    enriched_meta.setdefault("transcript_filename", transcript_path.name)

    protocol_text = generate_protocol(
        transcript_md,
        enriched_meta,
        method_text=method_text,
        meeting_sid=meeting_sid,
        series_memory=series_memory,
    )
    _atomic_write_text(protocol_path, protocol_text)
    return protocol_path


# ---------- Ф5: извлечение задач + маршрутизация ------------------------

# Закрытый список сфер из методички tasks.md (раздел «Правила работы» →
# «Сферы (тэги) — закрытый список»). Дублируется здесь, потому что промт
# должен явно перечислять допустимые значения — иначе Sonnet «галлюцинирует»
# свои. Источник правды — `~/Projects/me/tasks.md`. При расширении списка —
# обновить ОБЕ копии (тут и в tasks.md).
TASK_SPHERES_CLOSED_LIST = (
    "anzhee",
    "мпервый",
    "envyton",
    "сценалогия",
    "личное",
    "здоровье",
    "семья",
    "дубай",
    "дом",
    "me-clone",
    "процессы-ai",
    "обучение",
    "новый-доход",
)

TASK_EXTRACTION_MODEL = "claude-sonnet-4-6"
TASK_PARSE_FALLBACK_MODEL = "claude-haiku-4-5-20251001"

# Порог анти-галлюцинации задач по длительности встречи (Идея2 + РИСК3
# в плане). Длительность в минутах из meta.audioDurationS / meta.durationS.
_TASK_THRESHOLD_BY_DURATION = (
    (60, 7),    # < 60 мин → > 7 задач = подозрительно
    (120, 12),  # 60–120 мин → > 12
    (10**9, 20),  # > 120 мин → > 20
)


class TaskExtractionError(RuntimeError):
    """Сбой `extract_tasks` (LLM/parse/IO)."""


EXTRACT_TASKS_SYSTEM_PROMPT = """Ты помогаешь извлечь задачи из протокола встречи.

На вход:
1. Финальный markdown-протокол встречи (имена спикеров уже подставлены, может встретиться «Спикер N» если имя не было известно).
2. Метаданные встречи (series, дата, длительность, участники).
3. Закрытый список сфер задач — выбирать ровно из него.

Твоя задача: найти все конкретные ДОГОВОРЁННОСТИ — где один человек обязался что-то сделать («сделаю X», «пришлю Y», «договорились что Z к пятнице», «возьмёшь на себя», «отправлю до завтра»).

Правила:
- НЕ выдумывай задачи. Если в протоколе нет договорённости — не возвращай её.
- Цитата `source_quote` ОБЯЗАТЕЛЬНА — 1–2 предложения из протокола, где задача прозвучала. Точная цитата (можно сократить «...» в середине), не пересказ.
- `owner` — ровно как в протоколе. Если протокол говорит «Илья», верни «Илья». Если «Михаил Саргин» — верни «Михаил Саргин». Если «Спикер 3» — верни «Спикер 3». НЕ выдумывай имя для «Спикера N».
- `text` — формулировка задачи одним предложением в форме повелительного наклонения или «<глагол>+что» («прислать звонки», «оформить документ»). Без водных слов вроде «нужно бы», «не забыть».
- `deadline`: только если срок ЯВНО прозвучал. Если «к пятнице» — посчитай относительно даты встречи (день недели) и верни ISO YYYY-MM-DD. Если «к концу недели» — пятница недели встречи. Если «к понедельнику» — ближайший понедельник. Если «после X» / «когда будет время» / «потом» / срок не упоминался — верни null. Лучше пропустить срок чем выдумать.
- `sphere`: выбери одну сферу из переданного `spheres` списка по СМЫСЛУ задачи. Если задача про продажи на маркетплейсах / категорию товаров (проекторы / караоке / т.п. для бренда МПервый) → `мпервый`. Если про дилеров Anzhee, аудио-оборудование, B2B-портал, дилер-360, локализацию YME, неликвид, продукт-и-цены Anzhee → `anzhee`. Если про ENVYTON (отдельный B2C-бренд проекторов) → `envyton`. Если про Сценалогию (хищение 6 млн) → `сценалогия`. Если про здоровье/врачей/операцию ахилла → `здоровье`. Если про переезд в Дубай / квартиры / арендодателей → `дубай`. Если про детей / маму / семью → `семья`. Если про инструменты Ильи (бот-нотариус, бот-почта, дашборд) → `me-clone`. Если про AI-автоматизацию бизнес-процессов компаний (Anzhee/Мпервый ботами/CRM/AI) → `процессы-ai`. Если про обучение Миллера / курсы → `обучение`. Если про новые источники дохода → `новый-доход`. Иначе если про дом/быт → `дом`. Иначе → `личное`. ИЗ ПЕРЕДАННОГО СПИСКА; НЕ ПРИДУМЫВАЙ НОВЫХ.
- Если сильно не уверен в сфере — поставь `sphere: null` и `confidence_sphere: 0.0..0.5` (мы переспросим Илью).
- `confidence_sphere`: 0.0..1.0 — насколько уверен в выбранной сфере. 0.85+ — несколько сходящихся сигналов; 0.5..0.85 — один умеренный; ниже — лучше null.

Формат ответа — СТРОГО валидный JSON-массив (без markdown-обёртки, без объяснений, без префиксов):
[
  {
    "owner": "Илья",
    "text": "прислать тестовые звонки",
    "deadline": "2026-06-05",
    "sphere": "anzhee",
    "source_quote": "Илья: пришлю тебе тестовые звонки до пятницы.",
    "confidence_sphere": 0.85
  },
  ...
]
Если задач нет — верни `[]`.
"""


def _meeting_duration_minutes(meta: dict) -> Optional[int]:
    """Минуты встречи из meta. Возвращает None если поля нет/невалидно."""
    raw = meta.get("durationMin") or meta.get("duration")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    audio_s = meta.get("audioDurationS") or meta.get("durationS")
    if isinstance(audio_s, (int, float)) and audio_s > 0:
        return max(1, int(round(audio_s / 60)))
    return None


def _task_threshold_for_duration(minutes: Optional[int]) -> int:
    """Анти-галлюцинация: при > порога задач отправляем clarify Илье."""
    if minutes is None or minutes <= 0:
        # Без длительности применяем средний порог (60–120 мин).
        return 12
    for upper, threshold in _TASK_THRESHOLD_BY_DURATION:
        if minutes <= upper:
            return threshold
    return 20


def _parse_iso_date(s: Optional[str]) -> Optional[str]:
    """Валидация ISO-даты `YYYY-MM-DD`. None для невалидной/пустой."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def _parse_extract_tasks_response(
    raw: str,
    *,
    spheres: tuple[str, ...],
) -> list[dict]:
    """Парсит JSON-массив задач из ответа Sonnet.

    Валидации:
      - JSON-массив (markdown-fence срезаем).
      - Каждый элемент — dict с owner/text/source_quote (обязательно).
      - sphere ∈ spheres или null.
      - deadline — валидная ISO-дата или null.
      - confidence_sphere — float 0..1 или null.
    """
    raw = _strip_markdown_fence(raw)
    start = raw.find("[")
    if start < 0:
        raise TaskExtractionError("ответ Sonnet не содержит JSON-массив")
    try:
        parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as e:
        raise TaskExtractionError(f"невалидный JSON: {e}") from e
    if not isinstance(parsed, list):
        raise TaskExtractionError(
            f"ожидался массив, получили {type(parsed).__name__}"
        )

    out: list[dict] = []
    allowed_spheres = set(spheres)
    for item in parsed:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner")
        text = item.get("text")
        source_quote = item.get("source_quote") or item.get("quote")
        if not isinstance(owner, str) or not owner.strip():
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        # source_quote — обязательное поле (защита от галлюцинации).
        if not isinstance(source_quote, str) or not source_quote.strip():
            continue
        deadline = _parse_iso_date(item.get("deadline"))
        sphere = item.get("sphere")
        if isinstance(sphere, str):
            sphere = sphere.strip().lower().lstrip("[").rstrip("]")
            if sphere not in allowed_spheres:
                sphere = None
        else:
            sphere = None
        conf_raw = item.get("confidence_sphere")
        try:
            conf = float(conf_raw) if conf_raw is not None else None
        except (TypeError, ValueError):
            conf = None
        if conf is not None:
            conf = max(0.0, min(1.0, conf))
        out.append({
            "owner": owner.strip(),
            "text": text.strip(),
            "deadline": deadline,
            "sphere": sphere,
            "source_quote": source_quote.strip(),
            "confidence_sphere": conf,
        })
    return out


def extract_tasks(
    protocol_md: str,
    meeting_meta: dict,
    *,
    method_text: Optional[str] = None,  # сохраняется для совместимости с сигнатурой плана
    model: str = TASK_EXTRACTION_MODEL,
    timeout: int = 180,
    meeting_sid: Optional[str] = None,
) -> list[dict]:
    """Извлекает задачи из протокола через Claude Sonnet 4.6.

    Возвращает: `list[dict]` со схемой `{owner, text, deadline, sphere,
    source_quote, confidence_sphere}` — см. `_parse_extract_tasks_response`.

    Гейт: env `ENABLE_TASK_EXTRACTION` (дефолт ON). Если OFF — `[]`.

    На сбой LLM/parse возвращает `[]` + warning (finalize не валится).

    `method_text` зарезервирован под склеивание методички tasks.md в промт,
    если в будущем понадобится — сейчас Sonnet получает только закрытый
    список сфер.
    """
    if not _is_task_extraction_enabled():
        logger.info("[extract_tasks] disabled by ENABLE_TASK_EXTRACTION=0")
        return []
    if not protocol_md or not protocol_md.strip():
        logger.info("[extract_tasks] meeting=%s протокол пустой — пропуск", meeting_sid or "?")
        return []

    spheres_str = ", ".join(TASK_SPHERES_CLOSED_LIST)
    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"
    duration_min = _meeting_duration_minutes(meeting_meta)
    duration_str = f"{duration_min} мин" if duration_min else "—"
    expected = meeting_meta.get("expectedParticipants") or []
    participants = meeting_meta.get("participants") or []
    merged: list[str] = []
    seen: set[str] = set()
    for n in list(expected) + list(participants):
        if isinstance(n, str) and n and n not in seen:
            seen.add(n)
            merged.append(n)

    # У6 (ход 3): даём Sonnet явный день недели + ISO дату следующей пятницы /
    # понедельника. Sonnet 4.6 cutoff январь 2026 — он не должен «угадывать»
    # день недели в 2026+ году. Без этой подсказки промт мог посчитать «к
    # пятнице» относительно своей internal даты.
    weekday_hint = ""
    next_friday_iso = ""
    next_monday_iso = ""
    end_of_week_iso = ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        try:
            base_dt = datetime.strptime(date, "%Y-%m-%d")
            ru_weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
            wd = base_dt.weekday()
            weekday_hint = f"\n- weekday: {ru_weekdays[wd]} (если в протоколе «к пятнице» / «к понедельнику» — считай от этого дня)"
            # Пятница недели встречи (если суббота/воскресенье — пятница СЛЕДУЮЩЕЙ недели).
            delta_friday = 4 - wd if wd <= 4 else (4 - wd) + 7
            end_of_week_iso = (base_dt + timedelta(days=delta_friday)).strftime("%Y-%m-%d")
            # Ближайший понедельник после встречи.
            delta_monday = (0 - wd) % 7
            if delta_monday == 0:
                delta_monday = 7
            next_monday_iso = (base_dt + timedelta(days=delta_monday)).strftime("%Y-%m-%d")
            # Следующая пятница (если на встрече сказали «к пятнице» — обычно эта пятница).
            next_friday_iso = end_of_week_iso
        except ValueError:
            pass
    deadlines_hint = ""
    if next_friday_iso:
        deadlines_hint = (
            f"\n- end_of_meeting_week: {end_of_week_iso} (пятница недели встречи)"
            f"\n- next_monday: {next_monday_iso} (ближайший понедельник после встречи)"
            f"\n- next_friday: {next_friday_iso} (пятница после встречи, если «к пятнице»)"
        )

    user_prompt = (
        f"spheres: {spheres_str}\n\n"
        f"Метаданные встречи:\n"
        f"- series: {series}\n"
        f"- date: {date}{weekday_hint}{deadlines_hint}\n"
        f"- duration: {duration_str}\n"
        f"- participants: {', '.join(merged) if merged else '—'}\n\n"
        f"Протокол:\n\n{protocol_md.strip()}"
    )

    started = time.monotonic()
    try:
        raw = call_claude_print(
            user_prompt,
            system=EXTRACT_TASKS_SYSTEM_PROMPT,
            timeout=timeout,
            model=model,
        )
    except ClaudeCliNotInstalled:
        logger.warning("[extract_tasks] `claude` не в PATH — извлечение пропущено")
        return []
    except ClaudeCliError as e:
        logger.warning("[extract_tasks] meeting=%s CLI error: %s", meeting_sid or "?", e)
        return []
    elapsed = time.monotonic() - started

    try:
        tasks = _parse_extract_tasks_response(raw, spheres=TASK_SPHERES_CLOSED_LIST)
    except TaskExtractionError as e:
        logger.warning("[extract_tasks] meeting=%s parse error: %s", meeting_sid or "?", e)
        return []

    threshold = _task_threshold_for_duration(duration_min)
    logger.info(
        "[extract_tasks] meeting=%s count=%d elapsed=%.1fs threshold=%d filtered=%d model=%s",
        meeting_sid or "?", len(tasks), elapsed, threshold, 0, model,
    )
    return tasks


def _is_task_extraction_enabled() -> bool:
    raw = (os.environ.get("ENABLE_TASK_EXTRACTION") or "").strip().lower()
    return raw not in ("0", "false", "no")


def _is_task_routing_enabled() -> bool:
    raw = (os.environ.get("ENABLE_TASK_ROUTING") or "").strip().lower()
    return raw not in ("0", "false", "no")


# --- route_tasks: маршрутизация в tasks.md / трек стейкхолдера ---------

# Имя «Илья» в разных формах. LLM может вернуть «Илья», «Илья Рыбалка»,
# «И. Рыбалка». Нормализуем до first-word для матча.
ILYA_NAMES = ("Илья", "Илья Рыбалка", "Рыбалка")

# Спикеры без имени — формат render.py: «Спикер N» (1-based).
_SPEAKER_LABEL_RE = re.compile(r"^Спикер\s*\d+$", re.IGNORECASE)


def _owner_kind(owner: str, stakeholders: list[dict]) -> tuple[str, Optional[dict]]:
    """Классифицирует owner:
      - ("ilia", None)            — Илья (в любой форме).
      - ("stakeholder", <stk>)    — найден в реестре.
      - ("unknown_owner", None)   — «Спикер N».
      - ("other", None)           — конкретный человек, но не Илья и не в реестре.
    """
    if not owner:
        return ("other", None)
    raw = owner.strip()
    raw_low = raw.lower()
    # Илья — точное / first-word совпадение. Регистронезависимо для обеих
    # сторон (Н8 ход 1: «РЫБАЛКА» в верхнем регистре не должен попадать в
    # `other` и терять задачу Ильи).
    raw_first_low = raw_low.split()[:1]
    for nm in ILYA_NAMES:
        nm_low = nm.lower()
        nm_first_low = nm_low.split()[:1]
        if raw_low == nm_low or raw_first_low == nm_first_low:
            return ("ilia", None)
    if _SPEAKER_LABEL_RE.match(raw):
        return ("unknown_owner", None)
    from . import stakeholders as stk_lib  # lazy: тесты могут не иметь me-dashboard
    found = stk_lib.find_stakeholder_by_name(raw, stakeholders)
    if found:
        return ("stakeholder", found)
    return ("other", None)


def _is_one_on_one_meeting(meeting_meta: dict, stakeholder: dict) -> bool:
    """1:1 встреча со стейкхолдером.

    Критерий: `expectedParticipants` содержит ровно 2 имени, одно из которых —
    Илья (по first-word), второе — этот стейкхолдер (по имени или first-word).
    """
    expected = meeting_meta.get("expectedParticipants") or []
    if not isinstance(expected, list) or len(expected) != 2:
        return False
    expected_low = [str(p).strip().lower() for p in expected if isinstance(p, str)]
    if len(expected_low) != 2:
        return False
    has_ilia = any(
        p.startswith("илья") or p == "рыбалка" or "рыбалка" in p.split()
        for p in expected_low
    )
    stk_name = (stakeholder.get("name") or "").strip().lower()
    stk_first = stk_name.split()[0] if stk_name.split() else ""
    has_stk = any(
        p == stk_name or (stk_first and stk_first in p.split())
        for p in expected_low
    )
    return has_ilia and has_stk


def _default_start_for_deadline(deadline: Optional[str], *, is_large: bool = True) -> Optional[str]:
    """Дефолт `с <старт>` по правилам tasks.md: крупная — `до − 7`, мелкая — `до − 3`."""
    if not deadline:
        return None
    try:
        dt = datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        return None
    delta = 7 if is_large else 3
    return (dt - timedelta(days=delta)).strftime("%Y-%m-%d")


def _format_sphere_tag(sphere: Optional[str]) -> str:
    """`anzhee` → `[anzhee]`; None / пусто → `[личное]` дефолтная сфера."""
    if not sphere:
        return "[личное]"
    return f"[{sphere}]"


def _format_task_line(task: dict, *, created: str, series: str, date: str, marker: Optional[str] = None) -> str:
    """Форматирует строку задачи под `tasks.md`.

    `marker`: опциональный префикс к owner (например `[?]` для unknown_owner).
    """
    deadline = task.get("deadline") or "—"
    start = task.get("start") or _default_start_for_deadline(task.get("deadline")) or "—"
    sphere = _format_sphere_tag(task.get("sphere"))
    text = (task.get("text") or "").strip()
    quote = (task.get("source_quote") or "").strip().replace("\n", " ")
    if marker:
        text = f"{marker} {text}"
    context = f"контекст: протокол {series} {date}"
    if quote:
        # Сокращаем цитату до 160 символов, чтобы строка не разрослась.
        if len(quote) > 160:
            quote = quote[:159].rstrip() + "…"
        context += f", цитата: «{quote}»"
    return (
        f"- {created} | до {deadline} | с {start} | {sphere} | {text} | {context}"
    )


def _task_already_exists(tasks_md_path: Path, owner_marker: str, text: str, series: str, date: str) -> bool:
    """Дубль-чек: задача с тем же `text` + контекстом `протокол <series> <date>`
    уже существует в tasks.md.

    Сравнение — sha1[:12] от `(text.strip().lower() + "|" + ctx.lower())`. Менее
    подвержено ложным совпадениям, чем сравнение по первым 30 символам (две
    задачи могут иметь одинаковое начало, но разные хвосты — см. ход 1 Н3).
    """
    if not tasks_md_path.is_file():
        return False
    try:
        raw = tasks_md_path.read_text(encoding="utf-8")
    except OSError:
        return False
    ctx = f"протокол {series} {date}".lower()

    def _normalize(s: str) -> str:
        """Свёрнутая нормализация для хэш-сравнения дублей (ход 4 НОВ2):
        нижний регистр + сжатые подряд пробелы + trim. Защита от
        двойных пробелов в формулировках LLM при двух прогонах."""
        return re.sub(r"\s+", " ", s.strip().lower())

    needle_hash = hashlib.sha1(
        (_normalize(text) + "|" + ctx).encode("utf-8")
    ).hexdigest()[:12]
    # Идём по строкам, для каждой с маркером ctx — извлекаем text (между предпоследним
    # `|` и последним), считаем хэш, сравниваем.
    for line in raw.splitlines():
        ln_low = line.lower()
        if ctx not in ln_low:
            continue
        # Формат строки: `- <created> | до <due> | с <start> | [сфера] | <text> | контекст: ...`
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6:
            continue
        # `text` обычно в parts[-2] (последний — «контекст: …»). Подстраховка
        # для случая, когда text сам содержит ` | `: берём предпоследний.
        line_text = parts[-2]
        line_text_clean = re.sub(r"^\[\?\]\s+\S+:\s+", "", line_text)  # снимаем «[?] Спикер N:» если есть
        line_hash = hashlib.sha1(
            (_normalize(line_text_clean) + "|" + ctx).encode("utf-8")
        ).hexdigest()[:12]
        if line_hash == needle_hash:
            return True
    return False


def _append_to_tasks_md(tasks_md_path: Path, new_lines: list[str]) -> int:
    """Дописывает строки в раздел `## 📥 Актуальные (живые задачи)` через atomic write
    под `fcntl.flock` (РИСК5 в Тех-решениях плана: lock-конкуренция при параллельной
    финализации двух встреч).

    Возвращает число записанных строк.
    Если файл не найден — возвращает 0 + warning.
    """
    if not tasks_md_path.is_file():
        logger.warning("[route_tasks] tasks.md не найден: %s", tasks_md_path)
        return 0
    if not new_lines:
        return 0

    # Lock-файл рядом с tasks.md. flock(LOCK_EX) сериализует read-modify-write
    # двух параллельных финализаций. Без него — lost-update (вторая запись
    # затирает первую). Lock-файл может пережить процесс — это ок, fcntl
    # снимает блокировку при close(), сам файл остаётся.
    lock_path = tasks_md_path.with_suffix(tasks_md_path.suffix + ".lock")
    try:
        lock_fh = open(lock_path, "a")
    except OSError as e:
        logger.warning("[route_tasks] lock-файл недоступен (%s): продолжаю без блокировки — РИСК lost-update", e)
        lock_fh = None
    locked = False
    if lock_fh is not None:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError as e:
            logger.warning("[route_tasks] flock failed: %s — продолжаю без блокировки", e)
    try:
        try:
            raw = tasks_md_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("[route_tasks] tasks.md не читается: %s", e)
            return 0

        lines = raw.split("\n")
        # Ищем заголовок «## 📥 Актуальные (живые задачи)».
        actual_idx = None
        next_h2_idx = None
        for i, line in enumerate(lines):
            if line.startswith("## 📥 Актуальные"):
                actual_idx = i
                break
        if actual_idx is None:
            logger.warning("[route_tasks] раздел '## 📥 Актуальные' не найден в tasks.md")
            return 0
        for i in range(actual_idx + 1, len(lines)):
            if lines[i].startswith("## "):
                next_h2_idx = i
                break
        if next_h2_idx is None:
            next_h2_idx = len(lines)

        # Точка вставки — в конец блока «📥 Актуальные», перед next_h2_idx,
        # пропустив висячие пустые строки.
        insert_at = next_h2_idx
        while insert_at > actual_idx + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1

        inject: list[str] = []
        # Гарантируем пустую строку отделения от предыдущего контента.
        if insert_at > 0 and lines[insert_at - 1].strip() != "":
            inject.append("")
        inject.extend(new_lines)

        new_lines_total = list(lines)
        new_lines_total[insert_at:insert_at] = inject
        new_text = "\n".join(new_lines_total)

        _atomic_write_text(tasks_md_path, new_text)
        return len(new_lines)
    finally:
        if lock_fh is not None:
            try:
                if locked:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                try:
                    lock_fh.close()
                except OSError:
                    pass


def _append_to_stakeholder_track(
    file_path: Path,
    section_title: str,
    bullet_block: str,
) -> bool:
    """Атомарная дописка в накопитель стейкхолдера (pure-Python).

    Ф6 закрыл архитектурный долг Ф5: вместо shell-обёртки
    `~/.local/bin/stakeholder-track.sh` (живёт только на маке) используем
    `lib.stakeholder_track.append_to_open_subsection` (`fcntl.flock` +
    `tempfile + os.rename` + whitelist из реестра). Работает одинаково
    на маке и на VPS.

    Возвращает True на успех. False на сбой (whitelist / lock / write).
    """
    from . import stakeholder_track
    return stakeholder_track.append_to_open_subsection(
        file_path, section_title, bullet_block,
    )


def route_tasks(
    tasks: list[dict],
    meeting_meta: dict,
    *,
    tasks_md_path: Optional[Path] = None,
    stakeholders_override: Optional[list[dict]] = None,
    meeting_sid: Optional[str] = None,
) -> dict:
    """Маршрутизирует задачи:
      - owner=Илья → tasks.md (atomic).
      - owner=<stakeholder> И 1:1 встреча → его трек через stakeholder-track.sh.
      - owner=Спикер N → tasks.md с маркером `[?]`.
      - owner=other → лог + skip.

    Возвращает dict с метриками:
      {"ilia": N, "others": M, "pending_deadline": K,
       "unknown_owner": U, "errors": [...]}.

    Не падает при отсутствии tasks.md / реестра — просто записывает в errors.
    """
    result = {
        "ilia": 0,
        "others": 0,
        "pending_deadline": 0,
        "unknown_owner": 0,
        "errors": [],
    }

    if not _is_task_routing_enabled():
        logger.info("[route_tasks] disabled by ENABLE_TASK_ROUTING=0")
        return result
    if not tasks:
        return result

    if tasks_md_path is None:
        env_tasks = os.environ.get("MEETING_NOTARY_TASKS_MD")
        if env_tasks:
            tasks_md_path = Path(os.path.expanduser(env_tasks))
        else:
            me_dir = os.environ.get("ME_DIR") or os.path.expanduser("~/Projects/me")
            tasks_md_path = Path(me_dir) / "tasks.md"

    from . import stakeholders as stk_lib  # lazy
    stakeholders = (
        stakeholders_override
        if stakeholders_override is not None
        else stk_lib.load_stakeholders()
    )

    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Делим задачи на корзины.
    ilia_lines: list[str] = []
    unknown_lines: list[str] = []
    stakeholder_groups: dict[str, list[dict]] = {}  # slug → [tasks]
    stakeholder_map: dict[str, dict] = {}  # slug → stakeholder

    for task in tasks:
        owner = (task.get("owner") or "").strip()
        kind, stk = _owner_kind(owner, stakeholders)

        if kind == "ilia":
            line = _format_task_line(task, created=created, series=series, date=date)
            if _task_already_exists(tasks_md_path, "", task.get("text", ""), series, date):
                logger.info("[route_tasks] skip-dup ilia text=%r", (task.get("text") or "")[:60])
                continue
            ilia_lines.append(line)
            result["ilia"] += 1
            if not task.get("deadline"):
                result["pending_deadline"] += 1
            continue

        if kind == "unknown_owner":
            marker = f"[?] {owner}:"
            line = _format_task_line(task, created=created, series=series, date=date, marker=marker)
            if _task_already_exists(tasks_md_path, "[?]", task.get("text", ""), series, date):
                logger.info("[route_tasks] skip-dup unknown text=%r", (task.get("text") or "")[:60])
                continue
            unknown_lines.append(line)
            result["unknown_owner"] += 1
            continue

        if kind == "stakeholder":
            assert stk is not None
            if not _is_one_on_one_meeting(meeting_meta, stk):
                logger.info(
                    "[route_tasks] meeting=%s owner=%r stakeholder %s но встреча не 1:1 — skip (вне скоупа)",
                    meeting_sid or "?", owner, stk.get("slug"),
                )
                continue
            stakeholder_groups.setdefault(stk["slug"], []).append(task)
            stakeholder_map[stk["slug"]] = stk
            result["others"] += 1
            continue

        # other: имя есть, не Илья, не в реестре, не «Спикер N».
        logger.info(
            "[route_tasks] meeting=%s owner=%r неизвестный участник — skip (вне скоупа)",
            meeting_sid or "?", owner,
        )

    # Запись в tasks.md (Илья + неопределённые).
    all_md_lines = list(ilia_lines) + list(unknown_lines)
    if all_md_lines:
        written = _append_to_tasks_md(tasks_md_path, all_md_lines)
        if written == 0 and all_md_lines:
            result["errors"].append(f"tasks.md write failed ({tasks_md_path})")

    # Запись в треки стейкхолдеров (только для 1:1).
    section_title = f"📋 Из встречи {date}"
    for slug, group in stakeholder_groups.items():
        stk = stakeholder_map[slug]
        track_path = stk_lib.stakeholder_abs_track_path(stk)
        if track_path is None or not track_path.is_file():
            result["errors"].append(f"track-missing:{slug}")
            logger.warning(
                "[route_tasks] трек стейкхолдера %s не найден: %s",
                slug, track_path,
            )
            continue
        bullet_lines = _build_stakeholder_bullet_block(group, meeting_meta)
        ok = _append_to_stakeholder_track(track_path, section_title, "\n".join(bullet_lines))
        if not ok:
            result["errors"].append(f"track-append-failed:{slug}")

    logger.info(
        "[route_tasks] meeting=%s ilia=%d others=%d unknown=%d pending_deadline=%d errors=%d",
        meeting_sid or "?", result["ilia"], result["others"],
        result["unknown_owner"], result["pending_deadline"], len(result["errors"]),
    )
    return result


def _build_stakeholder_bullet_block(tasks: list[dict], meeting_meta: dict) -> list[str]:
    """Собирает блок для добавления в трек стейкхолдера.

    Первой строкой — ссылка на протокол. Далее — задачи как `- [ ]` пункты.
    """
    series = meeting_meta.get("series") or ""
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or ""
    lines: list[str] = []
    # Ссылка на протокол: трек живёт в `companies/<co>/совещания/`, протокол —
    # в `встречи/<series>/<date>-protokol.md`. Относительный путь = `../../../встречи/<series>/<date>-protokol.md`.
    if series and date:
        lines.append(
            f"- [протокол встречи](../../../встречи/{series}/{date}-protokol.md)"
        )
    for task in tasks:
        text = (task.get("text") or "").strip()
        quote = (task.get("source_quote") or "").strip().replace("\n", " ")
        if len(quote) > 200:
            quote = quote[:199].rstrip() + "…"
        deadline = task.get("deadline")
        suffix = ""
        if deadline:
            suffix = f" (до {deadline})"
        line = f"- [ ] **{text}**{suffix}"
        if quote:
            line += f" контекст: «{quote}»"
        lines.append(line)
    return lines


# --- Ф3: автосвязка протокол → трек стейкхолдера (закрытие + добавление) ---

STAKEHOLDER_TRACK_SYNC_MODEL = "claude-sonnet-4-6"

_STK_SYNC_OPEN_H2_RE = re.compile(r"^##\s+🟢\s+Открыто\b")
# Подсекция «🟢 Открыто», помеченная «… не закрывать …» (боевой паттерн:
# «### Хвосты — не закрывать молча»). Её пункты НЕ предлагаем LLM на
# авто-закрытие (Ф3, цикл5/У1): это явная пометка владельца «нужно ручное
# внимание». Сужает только closed-кандидатов — безопасная сторона.
_STK_SYNC_NO_AUTOCLOSE_RE = re.compile(r"^###\s+.*не\s+закрыва", re.IGNORECASE)


class StakeholderTrackSyncError(RuntimeError):
    """Сбой парсинга ответа LLM на шаге «что закрыть / что добавить»."""


STAKEHOLDER_TRACK_SYNC_SYSTEM_PROMPT = """Ты ведёшь накопитель открытых вопросов/долгов по стейкхолдеру (1:1 встречи с Ильёй).

На вход:
1. Текущий список ОТКРЫТЫХ вопросов/долгов стейкхолдера (дословные формулировки, нумерованы).
2. Протокол прошедшей 1:1 встречи.

Реши две вещи:
- closed: какие из текущих ОТКРЫТЫХ пунктов были РЕАЛЬНО обсуждены и закрыты/решены на этой встрече. Каждый элемент closed — ДОСЛОВНЫЙ текст пункта из списка открытых (копируй точно, без номера, без правок). НЕ закрывай пункт, если он лишь вскользь упомянут или не решён по сути. Сомневаешься — НЕ закрывай (лучше оставить открытым: закрытие потом стоит дороже, чем лишний открытый пункт).
- new: какие НОВЫЕ вопросы / долги / договорённости на контроль возникли на встрече и которых ещё НЕТ в списке открытых. Короткая формулировка одной строкой (без markdown, без «- [ ]», без номера). Не дублируй то, что уже открыто.

Правила:
- НЕ выдумывай. closed — только дословно из переданного списка открытых; new — только то, что реально прозвучало в протоколе.
- Если закрывать нечего — closed: []. Если новых нет — new: [].

Формат ответа — СТРОГО валидный JSON-объект (без markdown-обёртки, без пояснений, без префиксов):
{"closed": ["<дословный открытый пункт>", ...], "new": ["<новый вопрос одной строкой>", ...]}
"""


def _is_stakeholder_track_close_enabled() -> bool:
    """Гейт Ф3 `ENABLE_STAKEHOLDER_TRACK_CLOSE`.

    Дефолт ON (решение владельца 04.06: авто-закрытие сразу в проде, обратимо —
    перенос с пометкой, не удаление). Выключить = `0`/`false`/`no` (тот же
    контракт, что у `ENABLE_TASK_ROUTING`/`ENABLE_TASK_EXTRACTION`). Флаг
    оставлен, чтобы можно было быстро выключить новый путь без отката кода."""
    raw = (os.environ.get("ENABLE_STAKEHOLDER_TRACK_CLOSE") or "").strip().lower()
    return raw not in ("0", "false", "no")


def _norm_track_item(s: str) -> str:
    """Нормализация для сравнения пунктов: lower + сжатые пробелы + trim."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _extract_open_track_items(track_text: str) -> list[str]:
    """Дословные тела открытых пунктов (без `- [ ]` префикса) из блока
    «## 🟢 Открыто». Заголовки подсекций (`### ...`) и пустые строки — мимо.

    Пункты подсекций «… не закрывать …» (напр. «### Хвосты — не закрывать
    молча») ИСКЛЮЧАЮТСЯ из кандидатов: это явная пометка владельца «ручное
    внимание», бот их не авто-закрывает (цикл5/У1).

    Логика снятия префикса — общая со `stakeholder_track._strip_bullet_prefix`,
    чтобы то, что мы кладём LLM на вход, точно совпало с тем, что `close_open_item`
    потом матчит exact-match'ем."""
    from . import stakeholder_track  # lazy: одна копия логики снятия префикса
    lines = track_text.split("\n")
    open_idx: Optional[int] = None
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and _STK_SYNC_OPEN_H2_RE.match(ln):
            open_idx = i
            break
    if open_idx is None:
        return []
    next_idx = len(lines)
    for j in range(open_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            next_idx = j
            break
    items: list[str] = []
    protected = False  # внутри подсекции «… не закрывать …» — пункты пропускаем
    for k in range(open_idx + 1, next_idx):
        ln = lines[k]
        if ln.startswith("### "):
            protected = bool(_STK_SYNC_NO_AUTOCLOSE_RE.match(ln))
            continue
        if protected:
            continue
        if not ln.lstrip().startswith("- "):
            continue
        content = stakeholder_track._strip_bullet_prefix(ln)
        if content:
            items.append(content)
    return items


def _build_track_sync_prompt(open_items: list[str], protocol_md: str, meeting_meta: dict) -> str:
    """User-промт: открытые пункты + протокол. Дисциплина «Опасной тройки» —
    кладём только открытые пункты и протокол, ничего лишнего."""
    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"
    if open_items:
        open_block = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(open_items))
    else:
        open_block = "(открытых пунктов нет)"
    return (
        f"Встреча: {series} — {date}\n\n"
        f"Текущие ОТКРЫТЫЕ вопросы/долги (для closed возвращай ДОСЛОВНО текст без номера):\n"
        f"{open_block}\n\n"
        f"Протокол встречи:\n\n{protocol_md.strip()}"
    )


def _parse_track_sync_response(raw: str, open_items: list[str]) -> dict:
    """Парсит `{closed:[...], new:[...]}` из ответа LLM.

    - closed: оставляем ТОЛЬКО дословные совпадения с `open_items` (РАЗМ3 —
      exact-match защита от ложного закрытия). Возвращаем канонический текст
      пункта из файла (чтобы `close_open_item` гарантированно нашёл его).
      Не совпавшие — отбрасываем + лог.
    - new: непустые строки, тримминг, дедуп против `open_items` (не добавляем
      то, что уже открыто) и между собой.
    """
    raw = _strip_markdown_fence(raw)
    start = raw.find("{")
    if start < 0:
        raise StakeholderTrackSyncError("ответ LLM не содержит JSON-объект")
    try:
        parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as e:
        raise StakeholderTrackSyncError(f"невалидный JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise StakeholderTrackSyncError(
            f"ожидался объект, получили {type(parsed).__name__}"
        )

    open_norm = {_norm_track_item(s): s for s in open_items}

    closed: list[str] = []
    seen_c: set[str] = set()
    closed_raw = parsed.get("closed")
    if isinstance(closed_raw, list):
        for it in closed_raw:
            if not isinstance(it, str) or not it.strip():
                continue
            key = _norm_track_item(it)
            if key in open_norm:
                if key not in seen_c:
                    closed.append(open_norm[key])  # канонический текст из файла
                    seen_c.add(key)
            else:
                logger.warning(
                    "[track-sync] LLM вернул closed-пункт не из списка открытых — отброшен (РАЗМ3): %r",
                    it[:80],
                )

    new: list[str] = []
    seen_n: set[str] = set()
    new_raw = parsed.get("new")
    if isinstance(new_raw, list):
        for it in new_raw:
            if not isinstance(it, str) or not it.strip():
                continue
            # Санитизация входа (ход5): схлопываем переводы строк и повторные
            # пробелы в одну строку. `new` пишется буллетом `- [ ] {txt}` в трек;
            # многострочный текст инжектил бы лишние строки/фейковый заголовок
            # (## ✅ Закрытые) и сломал бы разбор секций при следующем sync.
            txt = re.sub(r"\s+", " ", it).strip()
            key = _norm_track_item(txt)
            if key in open_norm or key in seen_n:
                continue  # уже открыт / дубль внутри ответа
            seen_n.add(key)
            new.append(txt)

    return {"closed": closed, "new": new}


def _resolve_one_on_one_stakeholder(
    meeting_meta: dict, stakeholders: list[dict]
) -> Optional[dict]:
    """Стейкхолдер 1:1 встречи (тот же гейт, что `route_tasks`: `expectedParticipants`
    = Илья + один из реестра). None — если встреча не 1:1 со стейкхолдером."""
    for stk in stakeholders:
        if _is_one_on_one_meeting(meeting_meta, stk):
            return stk
    return None


def sync_stakeholder_track(
    protocol_md: str,
    meeting_meta: dict,
    *,
    stakeholders_override: Optional[list[dict]] = None,
    meeting_sid: Optional[str] = None,
    model: str = STAKEHOLDER_TRACK_SYNC_MODEL,
    timeout: int = 180,
) -> dict:
    """Ф3: по итогам 1:1 встречи закрывает обсуждённые открытые вопросы трека
    стейкхолдера (перенос «🟢 Открыто» → «✅ Закрытые», обратимо) и добавляет
    новые на контроль в «🟢 Открыто».

    Гейты:
      - env `ENABLE_STAKEHOLDER_TRACK_CLOSE` (дефолт ON; OFF = no-op);
      - скоуп тот же, что у `route_tasks`: встреча 1:1 со стейкхолдером из реестра.

    Решение «что закрыть / что добавить» принимает LLM по протоколу + текущему
    списку открытых (REQ 4.4). Закрытие — exact-match по дословному тексту
    открытого пункта (РАЗМ3); закрытие обратимо (REQ 4.5).

    Возвращает: {"enabled": bool, "stakeholder": slug|None, "closed": N,
    "new": M, "errors": [...]}. Best-effort — не бросает (finalize не валим).
    """
    result = {"enabled": True, "stakeholder": None, "closed": 0, "new": 0, "errors": []}

    if not _is_stakeholder_track_close_enabled():
        result["enabled"] = False
        logger.info("[track-sync] disabled by ENABLE_STAKEHOLDER_TRACK_CLOSE=0")
        return result
    if not protocol_md or not protocol_md.strip():
        logger.info("[track-sync] meeting=%s протокол пустой — пропуск", meeting_sid or "?")
        return result

    from . import stakeholders as stk_lib  # lazy
    from . import stakeholder_track
    stakeholders = (
        stakeholders_override
        if stakeholders_override is not None
        else stk_lib.load_stakeholders()
    )
    if not stakeholders:
        logger.info("[track-sync] meeting=%s реестр стейкхолдеров пуст — пропуск", meeting_sid or "?")
        return result

    stk = _resolve_one_on_one_stakeholder(meeting_meta, stakeholders)
    if stk is None:
        logger.info(
            "[track-sync] meeting=%s не 1:1 со стейкхолдером из реестра — skip (вне скоупа)",
            meeting_sid or "?",
        )
        return result
    result["stakeholder"] = stk.get("slug")

    track_path = stk_lib.stakeholder_abs_track_path(stk)
    if track_path is None or not track_path.is_file():
        result["errors"].append(f"track-missing:{stk.get('slug')}")
        logger.warning("[track-sync] трек стейкхолдера %s не найден: %s", stk.get("slug"), track_path)
        return result

    try:
        track_text = track_path.read_text(encoding="utf-8")
    except OSError as e:
        result["errors"].append(f"track-read-failed:{stk.get('slug')}")
        logger.warning("[track-sync] трек %s не читается: %s", track_path, e)
        return result

    open_items = _extract_open_track_items(track_text)
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"

    user_prompt = _build_track_sync_prompt(open_items, protocol_md, meeting_meta)
    started = time.monotonic()
    try:
        raw = call_claude_print(
            user_prompt,
            system=STAKEHOLDER_TRACK_SYNC_SYSTEM_PROMPT,
            timeout=timeout,
            model=model,
        )
    except ClaudeCliNotInstalled:
        logger.warning("[track-sync] `claude` не в PATH — пропуск")
        result["errors"].append("claude-cli-missing")
        return result
    except ClaudeCliError as e:
        logger.warning("[track-sync] meeting=%s CLI error: %s", meeting_sid or "?", e)
        result["errors"].append("claude-cli-error")
        return result
    elapsed = time.monotonic() - started

    try:
        decision = _parse_track_sync_response(raw, open_items)
    except StakeholderTrackSyncError as e:
        logger.warning("[track-sync] meeting=%s parse error: %s", meeting_sid or "?", e)
        result["errors"].append("parse-error")
        return result

    # Закрываем обсуждённые (перенос Открыто→Закрытые, обратимо).
    for item in decision["closed"]:
        ok = stakeholder_track.close_open_item(
            track_path, item, closed_date=date,
            note=f"закрыто ботом по встрече {date}",
        )
        if ok:
            result["closed"] += 1
        # ok=False = no-op (exact-match не прошёл / уже закрыт) — это не ошибка.

    # Добавляем новые на контроль (переиспользуем существующую append-ветку).
    if decision["new"]:
        section_title = f"📋 Из встречи {date}"
        bullet_block = "\n".join(f"- [ ] {t}" for t in decision["new"])
        ok = stakeholder_track.append_to_open_subsection(
            track_path, section_title, bullet_block,
        )
        if ok:
            result["new"] = len(decision["new"])
        else:
            result["errors"].append(f"track-append-failed:{stk.get('slug')}")

    logger.info(
        "[track-sync] meeting=%s stakeholder=%s closed=%d new=%d errors=%d elapsed=%.1fs",
        meeting_sid or "?", result["stakeholder"], result["closed"], result["new"],
        len(result["errors"]), elapsed,
    )
    return result


# --- Анти-галлюцинация: clarification к Илье ----------------------------

CLARIFY_TASK_FILTER_CALLBACK_PREFIX = "tf:"
CLARIFY_TASK_DEADLINES_CALLBACK_PREFIX = "td:"


def maybe_clarify_task_count(
    meeting_id: str,
    tasks: list[dict],
    meeting_meta: dict,
) -> Optional[Path]:
    """Если задач больше порога — шлём Илье сообщение «подтверди или вычеркни».

    Возвращает Path сохранённого state-файла (или None если порог не превышен /
    bot/chat не сконфигурированы / гейт OFF).

    State хранится отдельно от clarify спикеров: `_pending_clarification/
    <meeting_id>-tasks.json`. Listener распознаёт его по callback-префиксу `tf:`.
    """
    if not tasks:
        return None
    duration_min = _meeting_duration_minutes(meeting_meta)
    threshold = _task_threshold_for_duration(duration_min)
    if len(tasks) <= threshold:
        return None

    bot_token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    chat_id_raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    if not bot_token or not chat_id_raw:
        logger.warning(
            "[task-clarify] meeting=%s tasks=%d > threshold=%d, "
            "но TELEGRAM_NOTARIUS_BOT_TOKEN/CHAT_ID не заданы — пропуск",
            meeting_id, len(tasks), threshold,
        )
        return None
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        logger.warning("[task-clarify] meeting=%s TELEGRAM_CHAT_ID не число", meeting_id)
        return None

    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"

    lines: list[str] = [
        f"⚠️ Протокол «{series}» {date}: вижу {len(tasks)} задач "
        f"(порог для встречи {duration_min or '?'} мин = {threshold}). "
        f"Это много — подтверди или вычеркни.",
        "",
    ]
    for idx, t in enumerate(tasks, start=1):
        owner = (t.get("owner") or "—")
        text = (t.get("text") or "").strip()
        lines.append(f"{idx}) {owner}: {text}")
    lines.append("")
    lines.append("Ответом: «оставить все» / «убрать 3,5,7».")
    text = "\n".join(lines)

    rows = [
        [{"text": "✅ Оставить все", "callback_data": f"{CLARIFY_TASK_FILTER_CALLBACK_PREFIX}{_short_id(meeting_id)}:keep"}],
    ]
    reply_markup = telegram_api.build_inline_keyboard(rows)

    try:
        result = telegram_api.send_message(bot_token, chat_id, text, reply_markup=reply_markup)
    except telegram_api.TelegramApiError as e:
        logger.warning("[task-clarify] meeting=%s send failed: %s", meeting_id, e)
        return None

    try:
        timeout_s = int(os.environ.get("CLARIFY_TIMEOUT", "86400"))
    except ValueError:
        timeout_s = 86400
    sent_at = datetime.now(timezone.utc)
    deadline = sent_at + timedelta(seconds=timeout_s)
    state = {
        "meeting_id": meeting_id,
        "kind": "task_filter",
        "tasks": tasks,
        "meta": {"series": series, "date": date},
        "chat_id": chat_id,
        "message_id": int(result.get("message_id") or 0),
        "sent_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deadline_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeout_s": timeout_s,
        "status": "pending",
    }
    pending_root = clarify_state.resolve_pending_dir()
    pending_root.mkdir(parents=True, exist_ok=True)
    target = pending_root / f"{_validate_meeting_id_for_task(meeting_id)}-tasks.json"
    _atomic_write_text(target, json.dumps(state, ensure_ascii=False, indent=2))
    logger.info(
        "[task-clarify] sent meeting=%s type=task_filter tasks=%d threshold=%d",
        meeting_id, len(tasks), threshold,
    )
    return target


def _validate_meeting_id_for_task(meeting_id: str) -> str:
    """Тот же inline-pattern, что у clarify_state, но без import цикла.

    `meeting_id` — попадает в имя файла, защита от path traversal.
    """
    if not isinstance(meeting_id, str) or not meeting_id:
        raise ValueError("meeting_id must be a non-empty string")
    if not re.match(r"^[A-Za-z0-9._\-]+$", meeting_id):
        raise ValueError(f"meeting_id invalid chars: {meeting_id!r}")
    if meeting_id in (".", "..") or "/" in meeting_id or "\\" in meeting_id:
        raise ValueError(f"meeting_id path traversal: {meeting_id!r}")
    return meeting_id


def maybe_clarify_pending_deadlines(
    meeting_id: str,
    ilia_tasks_no_deadline: list[dict],
    meeting_meta: dict,
    *,
    unknown_owner_count: int = 0,
) -> Optional[Path]:
    """Если есть задачи Ильи без срока — шлём Илье «какие даты ставим?».

    `unknown_owner_count` (ход 3 У8): если >0 — в это же сообщение добавляем
    блок «N задач с нераспознанным владельцем (`[?]` в tasks.md)». Так Илья
    получает ОДНО сообщение вместо двух подряд (deadlines + unknown).

    State: `_pending_clarification/<meeting_id>-deadlines.json`. Listener
    обрабатывает callback с префиксом `td:` либо текстовый ответ.

    Возвращает Path state'а или None.
    """
    if not ilia_tasks_no_deadline:
        return None
    bot_token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    chat_id_raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    if not bot_token or not chat_id_raw:
        logger.warning(
            "[task-clarify] meeting=%s deadlines=%d, но bot/chat не заданы — пропуск",
            meeting_id, len(ilia_tasks_no_deadline),
        )
        return None
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        return None

    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"
    lines: list[str] = [
        f"📅 Протокол «{series}» {date}: "
        f"{len(ilia_tasks_no_deadline)} задач Ильи без срока.",
        "",
    ]
    for idx, t in enumerate(ilia_tasks_no_deadline, start=1):
        lines.append(f"{idx}) {(t.get('text') or '').strip()}")
    lines.append("")
    lines.append(
        "Какие даты ставим? Ответ форматом «1=2026-06-05, 2=на этой неделе, 3=без срока». "
        "Поддерживаю: ISO-даты, «сегодня/завтра», «на этой/следующей неделе», "
        "«к понедельнику/вторнику/...», «без срока»."
    )
    # У8 (ход 3): прицепляем блок про [?]-задачи, чтобы не отправлять отдельное
    # сообщение Илье — он и так на этом же UI отвечает по дедлайнам.
    # НОВ4 (ход 4): clamp на отрицательные значения от потенциально кривого caller'а.
    if isinstance(unknown_owner_count, int) and unknown_owner_count > 0:
        lines.append("")
        lines.append(
            f"❓ Ещё {unknown_owner_count} задач(и) с нераспознанным владельцем "
            f"(в tasks.md помечены `[?]`). Поправь руками когда увидишь."
        )
    text = "\n".join(lines)

    try:
        result = telegram_api.send_message(bot_token, chat_id, text)
    except telegram_api.TelegramApiError as e:
        logger.warning("[task-clarify] meeting=%s deadlines send failed: %s", meeting_id, e)
        return None

    try:
        timeout_s = int(os.environ.get("CLARIFY_TIMEOUT", "86400"))
    except ValueError:
        timeout_s = 86400
    sent_at = datetime.now(timezone.utc)
    deadline = sent_at + timedelta(seconds=timeout_s)
    state = {
        "meeting_id": meeting_id,
        "kind": "task_deadlines",
        "tasks": ilia_tasks_no_deadline,
        "meta": {"series": series, "date": date},
        "chat_id": chat_id,
        "message_id": int(result.get("message_id") or 0),
        "sent_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deadline_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeout_s": timeout_s,
        "status": "pending",
    }
    pending_root = clarify_state.resolve_pending_dir()
    pending_root.mkdir(parents=True, exist_ok=True)
    target = pending_root / f"{_validate_meeting_id_for_task(meeting_id)}-deadlines.json"
    _atomic_write_text(target, json.dumps(state, ensure_ascii=False, indent=2))
    logger.info(
        "[task-clarify] sent meeting=%s type=task_deadlines tasks=%d",
        meeting_id, len(ilia_tasks_no_deadline),
    )
    return target


def notify_unknown_owners(
    meeting_id: str,
    count: int,
    meeting_meta: dict,
) -> bool:
    """Сводное уведомление Илье о задачах с `[?]` owner. Без ответа — Илья сам поправит.

    Возвращает True если отправили; False на сбой/конфиг.
    """
    if count <= 0:
        return False
    bot_token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    chat_id_raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    if not bot_token or not chat_id_raw:
        return False
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        return False
    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"
    text = (
        f"❓ Протокол «{series}» {date}: {count} задач с нераспознанным владельцем "
        f"(в tasks.md помечены `[?]`). Поправь руками когда увидишь."
    )
    try:
        telegram_api.send_message(bot_token, chat_id, text)
    except telegram_api.TelegramApiError as e:
        logger.warning("[task-clarify] meeting=%s unknown-notify failed: %s", meeting_id, e)
        return False
    return True


# --- Парсер ответа на task_deadlines clarification ---------------------

def parse_task_deadlines_answer(
    text: str,
    *,
    meeting_date: str,
    n_tasks: int,
) -> dict[int, Optional[str]]:
    """Парсит ответ «1=2026-06-05, 2=на этой неделе, 3=без срока».

    Возвращает `{task_idx_1based: ISO-дата или None для "без срока"}`. Если
    идекса нет в ответе — он отсутствует в результате (caller интерпретирует
    как «не трогать»).

    Поддерживает:
      - ISO `YYYY-MM-DD`
      - «сегодня», «завтра», «послезавтра»
      - «на этой неделе» → пятница недели встречи
      - «на следующей неделе» → пятница следующей недели
      - «к понедельнику/вторнику/...» → ближайший день недели после встречи
      - «без срока» → None (явный сигнал)
    """
    out: dict[int, Optional[str]] = {}
    if not text or not text.strip():
        return out
    try:
        base = datetime.strptime(meeting_date, "%Y-%m-%d")
    except ValueError:
        base = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Регулярка: число = что-то (значение до запятой / конца строки / следующего «N=»).
    pattern = re.compile(r"(\d+)\s*[=:]\s*([^,;\n]+?)(?=\s*\d+\s*[=:]|[,;\n]|$)", re.IGNORECASE)
    for m in pattern.finditer(text):
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        if idx < 1 or idx > n_tasks:
            continue
        value = m.group(2).strip().lower()
        if not value:
            continue
        # «без срока»
        if re.match(r"^(без\s+срока|пропусти|не\s+знаю)$", value):
            out[idx] = None
            continue
        # ISO
        iso = _parse_iso_date(value)
        if iso:
            out[idx] = iso
            continue
        # «сегодня / завтра / послезавтра»
        if value == "сегодня":
            out[idx] = base.strftime("%Y-%m-%d")
            continue
        if value == "завтра":
            out[idx] = (base + timedelta(days=1)).strftime("%Y-%m-%d")
            continue
        if value == "послезавтра":
            out[idx] = (base + timedelta(days=2)).strftime("%Y-%m-%d")
            continue
        # «на этой неделе» / «к концу недели» → пятница недели встречи
        if re.search(r"(на\s+этой\s+неделе|к\s+концу\s+недели|до\s+конца\s+недели)", value):
            weekday = base.weekday()  # понедельник = 0, пятница = 4
            delta = 4 - weekday if weekday <= 4 else 4 + 7 - weekday
            out[idx] = (base + timedelta(days=delta)).strftime("%Y-%m-%d")
            continue
        # «на следующей неделе» → пятница следующей недели
        if re.search(r"(на\s+следующей\s+неделе|следующая\s+неделя)", value):
            weekday = base.weekday()
            delta = (4 - weekday) + 7
            out[idx] = (base + timedelta(days=delta)).strftime("%Y-%m-%d")
            continue
        # «к понедельнику / вторнику / ...»
        day_match = re.search(
            r"к\s+(понедельник|вторник|сред|четверг|пятниц|суббот|воскресен)",
            value,
        )
        if day_match:
            target_map = {
                "понедельник": 0,
                "вторник": 1,
                "сред": 2,
                "четверг": 3,
                "пятниц": 4,
                "суббот": 5,
                "воскресен": 6,
            }
            tgt = target_map.get(day_match.group(1))
            if tgt is not None:
                weekday = base.weekday()
                delta = (tgt - weekday) % 7
                if delta == 0:
                    delta = 7
                out[idx] = (base + timedelta(days=delta)).strftime("%Y-%m-%d")
                continue
        # Не распознали — пропуск (caller увидит, что idx нет в out).
    return out


def apply_deadlines_to_tasks_md(
    tasks_md_path: Path,
    meeting_meta: dict,
    task_texts: list[str],
    deadlines: dict[int, Optional[str]],
) -> int:
    """Обновляет дедлайны строк в tasks.md по результату clarification.

    `task_texts` — упорядоченный список текстов задач (как в clarify-сообщении,
    1-based индексирование). `deadlines` — `{idx: ISO|None}` от парсера.

    Логика: для каждой пары находим строку с этим текстом + контекстом
    `протокол <series> <date>`, заменяем `до —` на `до <date>` и `с —` на
    `с <date − 7>`. Если задача уже имеет дедлайн — не трогаем.

    Возвращает число применённых правок.
    """
    if not deadlines or not tasks_md_path.is_file():
        return 0
    try:
        raw = tasks_md_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[task-deadlines] read failed: %s", e)
        return 0

    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or "—"
    ctx_needle = f"протокол {series} {date}".lower()

    lines = raw.split("\n")
    applied = 0
    for idx_1, new_deadline in deadlines.items():
        if new_deadline is None:
            continue
        if idx_1 < 1 or idx_1 > len(task_texts):
            continue
        text = task_texts[idx_1 - 1].strip()
        text_low = text[:30].lower()
        new_start = _default_start_for_deadline(new_deadline) or new_deadline
        for i, line in enumerate(lines):
            ln_low = line.lower()
            if (
                text_low in ln_low
                and ctx_needle in ln_low
                and " | до — " in line
                and " | с — " in line
            ):
                new_line = line.replace(" | до — ", f" | до {new_deadline} ", 1)
                new_line = new_line.replace(" | с — ", f" | с {new_start} ", 1)
                lines[i] = new_line
                applied += 1
                break

    if applied:
        _atomic_write_text(tasks_md_path, "\n".join(lines))
    return applied


def parse_task_filter_answer(text: str, n_tasks: int) -> Optional[list[int]]:
    """Парсит ответ «оставить все» / «убрать 3,5,7» / «3,5,7».

    Возвращает:
      - `None` если «оставить все» / пусто / нераспознано (фоллбэк: оставляем все).
      - `list[int]` 1-based индексов к УДАЛЕНИЮ (например `[3, 5, 7]`).
    """
    if not text or not text.strip():
        return None
    low = text.strip().lower()
    if re.search(r"оставить\s+(все|всё)", low):
        return []
    # «убрать N,M,...» или просто «N,M,...»
    m = re.search(r"(?:убрать|удалить|выкинь|вычеркни)?\s*([\d\s,;\-]+)", low)
    if not m:
        return None
    body = m.group(1)
    indices: list[int] = []
    for tok in re.split(r"[,;\s]+", body):
        if not tok:
            continue
        if tok.isdigit():
            v = int(tok)
            if 1 <= v <= n_tasks and v not in indices:
                indices.append(v)
    if not indices:
        return None
    return indices


# ===========================================================================
# Ф6: доставка протокола в Telegram-группу + correction flow
# ===========================================================================
#
# Поток финализации (после Ф5):
#   1. `deliver_protocol(meta, text, *, meta_json_path)` — идемпотентная
#      отправка: split по 4000 символов, send_message в группу, append
#      `message_id` в `meta.delivered.message_ids` atomic.
#   2. Если `telegram_chat_id` для series нет — `ask_delivery_destination`
#      шлёт Илье вопрос «куда отправить?» через того же бота, state
#      `<meeting_id>-delivery.json` в `_pending_clarification/`.
#   3. После ответа Ильи (callback «Не отправлять» / «В личку» / chat_id /
#      ссылка) — `delivery_worker.process_*` применяет: либо send в группу +
#      сохранить привязку в watched.yaml, либо записать decision=skip/dm.
#   4. Correction flow: команды в личке боту «удали задачу N из <series>
#      <date>» / «поправь протокол <series> <date>: ...» парсит
#      `correction_command.parse_correction_command`, исполнение — в
#      `apply_correction` (этом модуле, ниже).
#
# Идемпотентность (УПУ1): `meta.delivered = {chat_id, message_ids[], at,
# decision?, history?}`. Перед каждой отправкой — проверка совпадения; если
# уже доставлено в нужный chat и количество частей совпадает — пропуск.
# Любое изменение `meta.json` — atomic через `_atomic_write_text(json.dumps)`.

# Модель для summary «было/стало» при коррекции — Haiku 4.5 (быстро, дёшево,
# хватит на 5-10 строк сравнения).
CORRECTION_SUMMARY_MODEL = "claude-haiku-4-5-20251001"

# Telegram delete_message: 48ч от момента доставки. После — старое сообщение
# не удалить, новая версия прилетит как продолжение + предупреждение.
DELETE_MESSAGE_WINDOW_SEC = 48 * 3600

# Префикс callback_data для «куда отправить» (chat-destination clarify).
DELIVERY_CALLBACK_PREFIX = "cd:"

# Лимит одной TG-части после split (с запасом до 4096 на маркер «(N/M) »).
DELIVERY_MAX_LEN = 3500


def _is_protocol_delivery_enabled() -> bool:
    """Гейт `ENABLE_PROTOCOL_DELIVERY` (дефолт ON; `0/false/no` → OFF)."""
    raw = (os.environ.get("ENABLE_PROTOCOL_DELIVERY") or "").strip().lower()
    return raw not in ("0", "false", "no")


def _read_meta_json(meta_json_path: Path) -> Optional[dict]:
    """Читает meta.json. None если файла нет / битый JSON."""
    if not meta_json_path or not meta_json_path.is_file():
        return None
    try:
        return json.loads(meta_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[delivery] meta read failed %s: %s", meta_json_path, e)
        return None


def _normalize_delivered(raw) -> list[dict]:
    """Принимает значение `delivered` из meta.json в любом формате,
    возвращает массив записей (новый формат Ф1).

    Миграция (доработки 2026-05-29): старый формат — `{chat_id, message_ids, at, decision?}`
    (object), новый — массив таких объектов. При чтении старого формата
    оборачиваем в одноэлементный массив. Не-dict/не-list игнорируем (None).
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        # Старый формат — один объект.
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _find_delivery_for_chat(records: list[dict], chat_id: int) -> Optional[dict]:
    """Ищет последнюю запись доставки для конкретного chat_id."""
    for rec in reversed(records):
        if rec.get("chat_id") == chat_id:
            return rec
    return None


def _update_meta_delivered(
    meta_json_path: Path,
    new_record: dict,
    *,
    replace_for_chat_id: bool = True,
) -> bool:
    """Atomic update поля `delivered` в meta.json (read-merge-write).

    Новый формат (Ф1, 2026-05-29): `delivered` — массив записей вида
    `{chat_id, message_ids, at[, decision]}`. `new_record` добавляется в
    конец массива; при `replace_for_chat_id=True` (дефолт) предыдущая
    запись с тем же chat_id заменяется в-place (используется для частичного
    delivered, когда чанки отправляются по одному и каждый раз обновляется
    «прогресс» одной записи).

    Caller'ы, которые хотят сохранить ВСЕ исторические записи (включая старые
    в тот же chat_id), могут передать `replace_for_chat_id=False`.

    Миграция (УПУ2 доработок 29.05): при чтении старого формата
    `{chat_id, ...}` оборачиваем в одноэлементный массив через
    `_normalize_delivered`.

    Н10 (цикл5/ход1): exclusive flock на `.{name}.lock`-файле в той же
    директории — защита от race между delivery (finalize) и correction worker'ом
    при перекрытии окон. Lock держится только на время read-merge-write.
    """
    if not meta_json_path:
        logger.warning("[delivery] meta_json_path не задан — пропуск")
        return False
    meta_json_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = meta_json_path.parent / f".{meta_json_path.name}.lock"
    fd: Optional[int] = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            logger.warning("[delivery] flock failed %s: %s", lock_path, e)
            return False
        meta = _read_meta_json(meta_json_path)
        if meta is None:
            meta = {}
        existing = _normalize_delivered(meta.get("delivered"))
        chat_id = new_record.get("chat_id")
        if replace_for_chat_id and chat_id is not None:
            existing = [r for r in existing if r.get("chat_id") != chat_id]
        existing.append(new_record)
        meta["delivered"] = existing
        try:
            _atomic_write_text(meta_json_path, json.dumps(meta, ensure_ascii=False, indent=2))
        except OSError as e:
            logger.warning("[delivery] meta write failed %s: %s", meta_json_path, e)
            return False
        return True
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _load_watched_for_series(series: str, watched_path: Optional[Path] = None) -> Optional[int]:
    """Возвращает `telegram_chat_id` для series из watched.yaml или None.

    Импорт `cli.registry` ленивый (`venv-cli` имеет PyYAML, на VPS — тоже).
    На сбое — None (caller спросит Илью).
    """
    try:
        from notary.cli.registry import load_watched, get_telegram_chat_id_for_series
    except Exception as e:  # noqa: BLE001
        logger.warning("[delivery] cli.registry import failed: %s", e)
        return None
    try:
        watched = load_watched()
    except Exception as e:  # noqa: BLE001
        logger.warning("[delivery] load_watched failed: %s", e)
        return None
    cid = get_telegram_chat_id_for_series(series, watched)
    if cid is None:
        return None
    return cid


def _persist_telegram_chat_id(series: str, chat_id: int) -> bool:
    """Atomic upsert `telegram_chat_id` для series в watched.yaml.

    Возвращает True если хоть одна запись обновлена. False — иначе или на ошибку.

    Н3 (цикл5/ход1): release_watched_lock гарантированно вызывается через
    finally — раньше при ValueError из `set_telegram_chat_id_for_series`
    lock висел на watched.yaml до stale-cleanup fs.
    """
    try:
        from notary.cli.registry import (  # noqa: PLC0415
            load_watched, save_watched, set_telegram_chat_id_for_series,
            release_watched_lock,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[delivery] cli.registry import failed (persist): %s", e)
        return False
    try:
        watched = load_watched(lock=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[delivery] load_watched lock failed: %s", e)
        return False
    saved = False
    try:
        try:
            n = set_telegram_chat_id_for_series(series, chat_id, watched)
        except Exception as e:  # noqa: BLE001
            logger.warning("[delivery] set_telegram_chat_id_for_series failed: %s", e)
            return False
        if n == 0:
            logger.warning("[delivery] series=%s нет в watched.yaml — привязку не сохранил", series)
            return False
        try:
            save_watched(watched)
            saved = True
        except Exception as e:  # noqa: BLE001
            logger.warning("[delivery] save_watched failed: %s", e)
            return False
    finally:
        # save_watched сам делает release_lock. Если до save_watched не дошли —
        # выпускаем явно, иначе flock висит на process'е до его смерти.
        if not saved:
            try:
                release_watched_lock()
            except Exception:  # noqa: BLE001
                pass
    return True


class DeliveryError(RuntimeError):
    """Сбой при попытке доставки протокола в Telegram."""


def deliver_protocol(
    meeting_meta: dict,
    protocol_text: str,
    *,
    meta_json_path: Optional[Path] = None,
    target_chat_id: Optional[int] = None,
    meeting_sid: Optional[str] = None,
) -> dict:
    """Идемпотентно доставляет протокол в Telegram-группу.

    Параметры:
      meeting_meta: dict с `series`, `date`, `sessionUid` (для лога).
      protocol_text: содержимое `<date>-protokol.md`.
      meta_json_path: путь к meta.json встречи — для записи `delivered`.
        Если None — идемпотентность через диск не работает (рискованно,
        caller должен сам гарантировать одинокий вызов).
      target_chat_id: явно заданный chat_id (если None — берём из
        watched.yaml по `meeting_meta.series`).
      meeting_sid: для structured-лога.

    Возвращает dict:
      {status: "sent"|"skipped"|"asked"|"disabled"|"error"|"partial-skipped",
       chat_id: int|None,
       message_ids: list[int],
       parts_count: int,
       document?: bool,
       error?: str}

    Семантика статусов (RISK2: hot-path-потребители завязаны на `{sent,skipped}`):
      - "sent"      — PDF отправлен (`message_ids=[<id документа>]`,
                      `delivered` обновлён, флаг `document:true`).
      - "skipped"   — идемпотентный пропуск (в этот chat уже доставлено —
                      PDF ИЛИ legacy-текст; RISK3 — не сверяем число частей).
      - "asked"     — `target_chat_id` неизвестен → отправили вопрос Илье,
                      state в `_pending_clarification/<sid>-delivery.json`.
      - "disabled"  — `ENABLE_PROTOCOL_DELIVERY=0`.
      - "error"     — сбой сборки/отправки PDF → алерт Илье (REQ 1.4), текстом
                      протокол НЕ шлём; либо нет токена.
      - "partial-skipped" — legacy partial-failure: ручное восстановление.

    Доставка — PDF-вложением с 4-строчной подписью (Ф2: `protocol_to_pdf` +
    `telegram_api.send_document`). Тело протокола текстом НЕ дублируется (REQ
    3.2). Старый текстовый путь (`format_protocol_as_tg_text`+чанки) убран из
    боевой доставки — при сбое PDF только алерт (REQ 1.4), без fallback-текста.
    """
    if not _is_protocol_delivery_enabled():
        logger.info("[delivery] disabled by ENABLE_PROTOCOL_DELIVERY=0")
        return {"status": "disabled", "chat_id": None, "message_ids": [], "parts_count": 0}

    if not protocol_text or not protocol_text.strip():
        return {
            "status": "error",
            "chat_id": None,
            "message_ids": [],
            "parts_count": 0,
            "error": "empty protocol text",
        }

    series = meeting_meta.get("series") or ""
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"

    # Шаг 1: chat_id из meta / watched.yaml.
    chat_id = target_chat_id
    if chat_id is None:
        chat_id_from_meta = meeting_meta.get("telegram_chat_id")
        if isinstance(chat_id_from_meta, int) and not isinstance(chat_id_from_meta, bool):
            chat_id = chat_id_from_meta
    if chat_id is None and series:
        chat_id = _load_watched_for_series(series)

    bot_token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    if not bot_token:
        logger.warning("[delivery] meeting=%s TELEGRAM_NOTARIUS_BOT_TOKEN не задан", meeting_sid or "?")
        return {
            "status": "error",
            "chat_id": chat_id,
            "message_ids": [],
            "parts_count": 0,
            "error": "no bot token",
        }

    # Шаг 2: chat_id неизвестен → правило владельца: нет привязки → доставляем
    # в личку (TELEGRAM_NOTARIUS_CHAT_ID) АВТОМАТИЧЕСКИ, без вопроса. Привязка серии
    # (Шаг 1) перебивает это. Старый интерактивный ask_delivery_destination убран:
    # inline-кнопки (callback_query) листенером не обрабатываются — нажатие «в личку»
    # не срабатывало, протокол зависал. Правило «нет привязки → личка» однозначно.
    if chat_id is None:
        owner_chat_raw = (os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID") or "").strip()
        try:
            chat_id = int(owner_chat_raw) if owner_chat_raw else None
        except ValueError:
            chat_id = None
        if chat_id is None:
            logger.warning(
                "[delivery] meeting=%s нет привязки И TELEGRAM_NOTARIUS_CHAT_ID не задан",
                meeting_sid or "?",
            )
            return {
                "status": "error",
                "chat_id": None,
                "message_ids": [],
                "parts_count": 0,
                "error": "no chat_id and no owner fallback",
            }
        logger.info(
            "[delivery] meeting=%s нет привязки → дефолт в личку chat=%s",
            meeting_sid or "?", chat_id,
        )

    # Шаг 3: идемпотентность ПЕРЕД дорогой сборкой PDF (RISK3).
    # Доставка теперь — ОДИН PDF-документ, не N текстовых чанков. «Уже
    # доставлено» определяем по наличию НЕПУСТОЙ записи для chat_id, а НЕ по
    # совпадению числа частей: legacy-текст имел N message_ids, PDF — один;
    # сверка `len==expected` сломала бы миграцию и слала бы PDF-дубль поверх
    # уже доставленного текста. Доставки в другие chat_id не блокируют (РИСК5 —
    # смена telegram_chat_id в watched.yaml без дубля в старый). partial-failure
    # из старого текстового пути по-прежнему НЕ авто-ретраим.
    meta = _read_meta_json(meta_json_path) if meta_json_path else None
    if meta:
        records = _normalize_delivered(meta.get("delivered"))
        rec = _find_delivery_for_chat(records, chat_id)
        if rec is not None:
            d_msgs = rec.get("message_ids") or []
            d_decision = rec.get("decision")
            if (d_decision == "partial-failure"
                    and isinstance(d_msgs, list) and len(d_msgs) > 0):
                logger.warning(
                    "[delivery] legacy partial-failure meeting=%s sent=%d — "
                    "skip auto-retry (manual recovery: clear meta.delivered).",
                    meeting_sid or "?", len(d_msgs),
                )
                return {
                    "status": "partial-skipped",
                    "chat_id": chat_id,
                    "message_ids": list(d_msgs),
                    "parts_count": len(d_msgs),
                    "error": "partial-failure-skip",
                }
            if isinstance(d_msgs, list) and len(d_msgs) > 0:
                logger.info(
                    "[delivery] idempotent skip meeting=%s chat_id=%s "
                    "(already delivered, %d msg id(s), document=%s)",
                    meeting_sid or "?", chat_id, len(d_msgs), rec.get("document"),
                )
                return {
                    "status": "skipped",
                    "chat_id": chat_id,
                    "message_ids": list(d_msgs),
                    "parts_count": 1,
                    "document": bool(rec.get("document")),
                }

    # Шаг 4: подпись (4 строки, REQ 3.1) + шапка PDF. Чистое время (Ф1) для
    # СТАРЫХ встреч без recording.* берём из архива транскрипта — путь
    # `series_dir/_transcripts/<date>.json`, но ТОЛЬКО если он реально есть
    # (иначе compute_duration_label зашумит legacy warning'ом). series_dir =
    # директория meta.json (collector/finalize кладут их рядом). FU-2 дайджеста.
    transcript_json_path = None
    if meta_json_path is not None:
        candidate = meta_json_path.parent / "_transcripts" / f"{date}.json"
        if candidate.is_file():
            transcript_json_path = candidate
    caption = protocol_to_tg.build_pdf_caption(
        protocol_text, meeting_meta, transcript_json_path=transcript_json_path,
    )
    pdf_title, pdf_subtitle = protocol_to_tg.build_pdf_title_subtitle(
        protocol_text, meeting_meta, transcript_json_path=transcript_json_path,
    )

    # Шаг 5: собрать PDF во временный файл и отправить документом. Любой сбой
    # (сборка/отправка) → алерт Илье (REQ 1.4), текстом протокол НЕ шлём,
    # статус "error" (старый текстовый fallback убран — ответ владельца 04.06).
    started = time.monotonic()
    safe_date = re.sub(r"[^0-9A-Za-z._-]", "-", str(date)) or "protokol"
    pdf_filename = f"protokol-{safe_date}.pdf"
    try:
        with tempfile.TemporaryDirectory(prefix="deliver-pdf-") as td:
            pdf_path = Path(td) / pdf_filename
            protocol_to_pdf.render_pdf_from_markdown(
                protocol_text, str(pdf_path),
                title=pdf_title, subtitle=pdf_subtitle,
            )
            send_result = telegram_api.send_document(
                bot_token, chat_id, str(pdf_path),
                caption=caption, filename=pdf_filename,
            )
    except (protocol_to_pdf.PdfRenderError, telegram_api.TelegramApiError, OSError) as e:
        _alert_owner_pdf_failure(meeting_meta, chat_id, meeting_sid, e)
        logger.error(
            "[delivery] PDF доставка упала meeting=%s chat_id=%s: %s — алерт Илье, "
            "текстом НЕ шлём",
            meeting_sid or "?", chat_id, e,
        )
        return {
            "status": "error",
            "chat_id": chat_id,
            "message_ids": [],
            "parts_count": 0,
            "error": str(e),
        }

    msg_id = int(send_result.get("message_id") or 0)
    elapsed = time.monotonic() - started
    at = _now_iso()

    # Шаг 6: запись `meta.delivered` (RISK1 — формат НЕ ломаем, читают 3
    # потребителя: идемпотентность, _is_success_record/rc=10, cleanup WAV).
    # PDF метим `document:true` + `decision:"pdf"`. `decision != "partial-failure"`
    # → _is_success_record видит успех; downstream без правок.
    if meta_json_path:
        persisted = _update_meta_delivered(meta_json_path, {
            "chat_id": chat_id,
            "message_ids": [msg_id],
            "at": at,
            "decision": "pdf",
            "document": True,
        })
        if not persisted:
            # PDF уже ушёл, но запись `delivered` НЕ легла (flock/запись упали).
            # Повторный finalize пройдёт идемпотентность мимо (Шаг 3 не найдёт
            # записи) и пришлёт ДУБЛЬ PDF в чат. Раньше это был только warning
            # внутри _update_meta_delivered, а наружу уходил status="sent" —
            # риск дубля молчал. Поднимаем до error: оператор чинит meta.json
            # ДО следующего finalize. (Цикл5/ход1, Н1.)
            logger.error(
                "[delivery] PDF ОТПРАВЛЕН (meeting=%s chat_id=%s msg_id=%s), но "
                "meta.delivered НЕ записан — повторный finalize пришлёт ДУБЛЬ; "
                "почини meta.json вручную",
                meeting_sid or "?", chat_id, msg_id,
            )
    logger.info(
        "[delivery] sent PDF meeting=%s chat_id=%s msg_id=%s elapsed=%.1fs at=%s",
        meeting_sid or "?", chat_id, msg_id, elapsed, at,
    )
    return {
        "status": "sent",
        "chat_id": chat_id,
        "message_ids": [msg_id],
        "parts_count": 1,
        "document": True,
        "at": at,
        "elapsed_s": round(elapsed, 1),
    }


def _alert_owner_pdf_failure(
    meeting_meta: dict,
    chat_id: Optional[int],
    meeting_sid: Optional[str],
    error: Exception,
) -> None:
    """Короткий алерт Илье в личку при сбое PDF-доставки (REQ 1.4, УПУ1).

    Источник owner-chat — `lib.notify.push` (обёртка над `~/.local/bin/tg-send`,
    тот же путь, что шлёт P0-алерты scheduler/collector — «known-owner chat»).
    Дедуп по встрече (`pdf-fail:<sid>`): повтор по той же встрече глушится на 6ч,
    сбой по ДРУГОЙ встрече всегда доходит. Если tg-send недоступен — push вернёт
    False; мы логируем `error` (НЕ немой отказ, УПУ1). Текстом протокол НЕ шлём.
    """
    series = (meeting_meta or {}).get("series") or "?"
    date = (meeting_meta or {}).get("date") or "?"
    sid = meeting_sid or (meeting_meta or {}).get("sessionUid") or "?"
    msg = (
        f"⚠️ Протокол по встрече «{series}» {date} не ушёл PDF-вложением "
        f"(chat {chat_id}). Текстом не слал. Посмотри логи: {type(error).__name__}."
    )
    try:
        from .notify import push  # noqa: PLC0415
        ok = push(msg, dedupe_key=f"pdf-fail:{sid}")
    except Exception as e:  # noqa: BLE001
        logger.error("[delivery] алерт Илье о сбое PDF не отправлен (push упал): %s", e)
        return
    if not ok:
        logger.error(
            "[delivery] алерт Илье о сбое PDF НЕ доставлен (tg-send недоступен?) "
            "meeting=%s — сбой не немой (УПУ1), см. error выше", sid,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- Ф6: clarification «куда отправить» --------------------------------


def ask_delivery_destination(
    meeting_id: str,
    meeting_meta: dict,
    *,
    bot_token: Optional[str] = None,
) -> Optional[Path]:
    """Шлёт Илье в личку вопрос «куда отправить протокол?» с inline keyboard.

    State пишет в `_pending_clarification/<meeting_id>-delivery.json` —
    listener позже подбирает callback (`cd:<short>:<action>`) или текст.

    Возвращает Path сохранённого state-файла или None при ошибке.

    Поддерживаемые ответы Ильи:
      - callback «🚫 Никуда» → `decision=skip`.
      - callback «💬 В личку» → отправить ему в DM, привязку НЕ сохранять.
      - текст: число (chat_id) / `https://t.me/c/<id>/...` ссылка.
    """
    bot_token = (bot_token or os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    chat_id_raw = (
        os.environ.get("TELEGRAM_NOTARIUS_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    if not bot_token or not chat_id_raw:
        logger.warning(
            "[delivery] meeting=%s ask_destination skipped: no bot/chat env",
            meeting_id,
        )
        return None
    try:
        dm_chat_id = int(chat_id_raw)
    except ValueError:
        logger.warning("[delivery] meeting=%s TELEGRAM_CHAT_ID не число", meeting_id)
        return None

    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or (meeting_meta.get("startTs") or "")[:10] or "—"

    text = (
        f"📬 Куда отправить протокол «{series}» {date}?\n\n"
        f"Ответь: chat_id (число), ссылкой на группу (https://t.me/c/.../) — "
        f"привязка запомнится для будущих встреч этой series.\n\n"
        f"Кнопки ниже — для one-off без привязки."
    )
    rows = [
        [{"text": "💬 Отправить в личку", "callback_data": f"{DELIVERY_CALLBACK_PREFIX}{_short_id(meeting_id)}:dm"}],
        [{"text": "🚫 Не отправлять", "callback_data": f"{DELIVERY_CALLBACK_PREFIX}{_short_id(meeting_id)}:skip"}],
    ]
    reply_markup = telegram_api.build_inline_keyboard(rows)

    try:
        result = telegram_api.send_message(bot_token, dm_chat_id, text, reply_markup=reply_markup)
    except telegram_api.TelegramApiError as e:
        logger.warning("[delivery] meeting=%s ask send failed: %s", meeting_id, e)
        return None

    try:
        timeout_s = int(os.environ.get("CLARIFY_TIMEOUT", "86400"))
    except ValueError:
        timeout_s = 86400
    sent_at = datetime.now(timezone.utc)
    deadline = sent_at + timedelta(seconds=timeout_s)

    state = {
        "meeting_id": meeting_id,
        "kind": "delivery",
        "meta": {
            "series": series,
            "date": date,
            "sessionUid": meeting_meta.get("sessionUid"),
        },
        "chat_id": dm_chat_id,
        "message_id": int(result.get("message_id") or 0),
        "sent_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deadline_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeout_s": timeout_s,
        "status": "pending",
    }
    pending_root = clarify_state.resolve_pending_dir()
    pending_root.mkdir(parents=True, exist_ok=True)
    target = pending_root / f"{_validate_meeting_id_for_task(meeting_id)}-delivery.json"
    _atomic_write_text(target, json.dumps(state, ensure_ascii=False, indent=2))
    logger.info("[delivery] asked meeting=%s state=%s", meeting_id, target.name)
    return target


# Регулярка для t.me ссылок на группу/канал. Распознаёт:
#   https://t.me/c/<internal>/<msg>     — internal id (без -100 префикса)
#   https://t.me/<username>/<msg>       — public username (chat_id неизвестен)
#   tg://...                            — игнорируем (не поддерживаем)
_TG_GROUP_LINK_RE = re.compile(
    r"https?://t\.me/c/(\d+)(?:/\d+)?",
    re.IGNORECASE,
)


def parse_chat_destination_answer(text: str) -> tuple[str, Optional[int]]:
    """Парсер ответа Ильи на вопрос «куда отправить».

    Возвращает `(kind, chat_id|None)`:
      - ("chat", <int>)   — извлекли chat_id из числа или ссылки.
      - ("skip", None)    — «никуда» / «не нужно» / «пропусти».
      - ("dm", None)      — «личка» / «мне в личку».
      - ("invalid", None) — не распознали (caller отвечает «не понял»).
    """
    if not text or not text.strip():
        return ("invalid", None)
    raw = text.strip()
    low = raw.lower()

    # «никуда» / «не отправлять».
    if re.search(r"\b(никуда|не\s+отправляй|не\s+нужно|skip|пропусти)\b", low):
        return ("skip", None)
    # «в личку» / «мне» / «лично».
    if re.search(r"\b(в\s+личку|лично|мне\s+в\s+личку|dm)\b", low):
        return ("dm", None)

    # Ссылка t.me/c/<id>/...: chat_id = -100 * <id> (см. документацию TG).
    link_m = _TG_GROUP_LINK_RE.search(raw)
    if link_m:
        try:
            internal = int(link_m.group(1))
            return ("chat", -1000000000000 - internal)
        except ValueError:
            pass

    # Просто число — chat_id (с минусом для группы).
    num_m = re.search(r"-?\d+", raw)
    if num_m:
        try:
            cid = int(num_m.group(0))
            return ("chat", cid)
        except ValueError:
            pass
    return ("invalid", None)


def parse_delivery_callback_data(data: str, *, meeting_id: str) -> Optional[str]:
    """Парсер callback_data вида `cd:<short_id>:<action>`.

    Возвращает action ("dm" / "skip") если data — наш callback и meeting_id
    совпал. None иначе.
    """
    if not isinstance(data, str) or not data.startswith(DELIVERY_CALLBACK_PREFIX):
        return None
    body = data[len(DELIVERY_CALLBACK_PREFIX):]
    parts = body.split(":")
    if len(parts) != 2:
        return None
    mid_short, action = parts
    if mid_short != _short_id(meeting_id):
        return None
    if action not in ("dm", "skip"):
        return None
    return action


# ===========================================================================
# Ф6: correction flow — версия + diff-summary
# ===========================================================================


class CorrectionError(RuntimeError):
    """Сбой при коррекции протокола (правка / запись версии / send)."""


def _next_version_path(protocol_path: Path) -> Path:
    """Вычисляет путь `_versions/<date>-protokol-vN.md` для следующей версии.

    Папка `_versions/` создаётся в той же директории, что и протокол.
    Имя протокола — `<date>-protokol.md` → версия `<date>-protokol-vN.md`.
    N инкрементальный: max(существующих vN) + 1.
    """
    versions_dir = protocol_path.parent / "_versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    stem = protocol_path.stem  # "<date>-protokol"
    pattern = re.compile(rf"^{re.escape(stem)}-v(\d+)\.md$")
    max_n = 0
    for f in versions_dir.glob(f"{stem}-v*.md"):
        m = pattern.match(f.name)
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except ValueError:
                continue
    return versions_dir / f"{stem}-v{max_n + 1}.md"


def _save_protocol_version(protocol_path: Path) -> Optional[Path]:
    """Копирует текущий `<date>-protokol.md` в `_versions/<date>-protokol-vN.md`.

    Возвращает путь сохранённой версии или None если файла-протокола нет.
    """
    if not protocol_path.is_file():
        return None
    try:
        content = protocol_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[correction] read protocol failed %s: %s", protocol_path, e)
        return None
    version_path = _next_version_path(protocol_path)
    try:
        _atomic_write_text(version_path, content)
    except OSError as e:
        logger.warning("[correction] write version failed %s: %s", version_path, e)
        return None
    return version_path


_TARGETED_REMOVE_RE = re.compile(
    r"^удали(?:ть)?\s+задачу\s+(\d+)\s+из\b",
    re.IGNORECASE,
)
_TARGETED_NEGATE_RE = re.compile(
    r"задачу\s+(\d+)\s+не\s+было",
    re.IGNORECASE,
)


def _classify_instruction(instruction: str) -> tuple[str, Optional[int]]:
    """Классифицирует инструкцию: ("targeted_remove", N) / ("structural", None).

    `targeted_remove` — точечный edit (удалить задачу N) без LLM.
    `structural` — структурная правка → regenerate_protocol с prompt-инъекцией.
    """
    if not instruction:
        return ("structural", None)
    s = instruction.strip()
    m = _TARGETED_REMOVE_RE.search(s)
    if m:
        try:
            return ("targeted_remove", int(m.group(1)))
        except ValueError:
            pass
    m = _TARGETED_NEGATE_RE.search(s)
    if m:
        try:
            return ("targeted_remove", int(m.group(1)))
        except ValueError:
            pass
    return ("structural", None)


def _apply_targeted_remove(protocol_text: str, task_number: int) -> Optional[str]:
    """Точечный edit: удалить N-ю задачу из секции `## Задачи` / `## 🟠 Задачи`.

    Подсчёт задач — по строкам, начинающимся с `- ` после заголовка задач.
    Возвращает новый текст или None если не нашли заголовок задач / N вне диапазона.
    """
    if task_number < 1:
        return None
    lines = protocol_text.split("\n")
    # Ищем секцию задач. ОТДЕЛЬНОЕ слово «Задачи» в заголовке (опц. с эмодзи
    # или другими non-letter символами вначале), НЕ часть композита «Решения
    # и задачи». Допустимо: «## Задачи», «## 🟠 Задачи», «## 🟡 Задачи Ильи».
    # У8 (цикл5/ход3): универсальный поиск через regex — срезаем `^##\s+` +
    # любую последовательность non-letter (эмодзи, *, и т.п.) + опц. пробелы,
    # затем сверяемся с «задачи»/«задача» как первым словом. Не зависит от
    # конкретного списка эмодзи в методичке.
    # НОВ2 (цикл5/ход4): дополнительно срезаем markdown bold-обёртку `**` —
    # `## **Задачи**` или `## **🟠 Задачи**` поддерживаются.
    _NON_LETTER_PREFIX = re.compile(r"^##\s+(?:[^\w\s]+\s*)*", re.UNICODE)
    tasks_h_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        body = _NON_LETTER_PREFIX.sub("", line).rstrip("*").lstrip("*")
        # Срезаем замыкающий `**` если он попал в конец первого слова.
        first_word = body.strip().split()[:1]
        if not first_word:
            continue
        word = first_word[0].lower().rstrip(":.*").lstrip("*")
        if word in ("задачи", "задача"):
            tasks_h_idx = i
            break
    if tasks_h_idx is None:
        return None
    # Дальше до следующей H2 — список задач (буллеты `- `).
    next_h_idx = len(lines)
    for j in range(tasks_h_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            next_h_idx = j
            break

    # Соберём индексы строк-буллетов в этом окне.
    bullet_indices: list[int] = []
    for k in range(tasks_h_idx + 1, next_h_idx):
        if re.match(r"^-\s+", lines[k]):
            bullet_indices.append(k)
    if task_number > len(bullet_indices):
        return None
    start = bullet_indices[task_number - 1]
    # Конец задачи: до следующего буллета / heading / EOF.
    end = next_h_idx
    if task_number < len(bullet_indices):
        end = bullet_indices[task_number]

    # Срезаем хвостовые пустые строки (косметика).
    rm_end = end
    while rm_end > start + 1 and rm_end - 1 < len(lines) and lines[rm_end - 1].strip() == "":
        rm_end -= 1
    # И ведущую пустую строку перед start (если есть и предыдущая не пустая).
    rm_start = start
    if rm_start > 0 and lines[rm_start - 1].strip() == "":
        # Не трогаем — обычно одна пустая между заголовком и буллетом.
        pass

    new_lines = lines[:rm_start] + lines[rm_end:]
    return "\n".join(new_lines)


CORRECTION_SUMMARY_PROMPT = """Ты помогаешь сформулировать короткое сравнение «было/стало» для пользователя после правки протокола встречи.

На вход тебе дан unified-diff между старой и новой версией протокола. Сформулируй 5–10 строк русского текста в формате:

🔄 Обновил протокол «<series>» <date>.

**Было:** <одна-две фразы что было>
**Стало:** <одна-две фразы что стало>

Дополнительно (опционально, если уместно):
- если удалили задачу — укажи кратко (одной строкой) какую;
- если изменили формулировку — укажи в чём суть изменения;
- если изменили решение — то же.

Не дублируй протокол целиком. Не выдумывай ничего, чего нет в diff.

Ответь только готовым русским текстом сообщения (без markdown-обёртки, без префиксов).
"""


def _compose_correction_summary(
    old_text: str,
    new_text: str,
    meeting_meta: dict,
    *,
    meeting_sid: Optional[str] = None,
    timeout: int = 60,
) -> str:
    """Сводное сообщение «было/стало» через Claude Haiku по diff'у.

    На ошибке Haiku — fallback на короткое заводское сообщение «Обновил
    протокол… см. выше». Без всплытия исключения.
    """
    import difflib
    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or "—"
    fallback = (
        f"🔄 Обновил протокол «{series}» {date}. "
        f"Старая версия выше — пользуйся новой."
    )

    if not old_text or not new_text:
        return fallback

    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile="было",
        tofile="стало",
        lineterm="",
        n=2,
    ))
    if not diff_lines:
        return f"🔄 Обновил протокол «{series}» {date} (изменений по содержанию нет)."

    # Урезаем diff если он гигантский (>200 строк).
    if len(diff_lines) > 200:
        diff_lines = diff_lines[:200] + ["... (diff обрезан)"]

    user_prompt = (
        f"Series: {series}\nDate: {date}\n\nDiff (unified):\n"
        + "\n".join(diff_lines)
    )
    try:
        raw = call_claude_print(
            user_prompt,
            system=CORRECTION_SUMMARY_PROMPT,
            timeout=timeout,
            model=CORRECTION_SUMMARY_MODEL,
        )
    except ClaudeCliNotInstalled:
        logger.warning("[correction] `claude` not in PATH — fallback summary")
        return fallback
    except ClaudeCliError as e:
        logger.warning("[correction] summary CLI error meeting=%s: %s", meeting_sid or "?", e)
        return fallback
    text = (raw or "").strip()
    if not text:
        return fallback
    return text


def _resolve_protocol_paths(
    series: str,
    date: str,
    *,
    root: Optional[Path] = None,
) -> tuple[Optional[Path], Optional[Path], Optional[Path]]:
    """Резолвит (transcript_path, protocol_path, meta_json_path) для (series, date).

    Поддерживает legacy `<root>/<series>-<date>/` и новую `<root>/<series>/`
    структуры (как `meetings_listener.maybe_route_to_protocol_command`).

    meta_json_path — `<dir>/meta.json` (если есть).
    Возвращает (None, None, None) если папка не найдена.
    """
    root = root or Path(
        os.path.expanduser(os.environ.get("MEETING_NOTARY_PROTOCOLS_DIR") or "~/Projects/me/встречи")
    )
    if not root.is_dir():
        return (None, None, None)
    new_dir = root / series
    legacy_dir = root / f"{series}-{date}"
    target_dir: Optional[Path] = None
    if (new_dir / f"{date}.md").is_file():
        target_dir = new_dir
    elif (legacy_dir / f"{date}.md").is_file():
        target_dir = legacy_dir
    else:
        return (None, None, None)
    transcript_path = target_dir / f"{date}.md"
    protocol_path = target_dir / f"{date}-protokol.md"
    meta_json_path = target_dir / "meta.json"
    if not meta_json_path.is_file():
        meta_json_path = None  # type: ignore[assignment]
    return (transcript_path, protocol_path, meta_json_path)


def apply_correction(
    series: str,
    date: str,
    instruction: str,
    *,
    in_group: bool = True,
    root: Optional[Path] = None,
    meeting_sid: Optional[str] = None,
) -> dict:
    """Применяет коррекцию протокола: сохраняет версию + правит .md + (опц.)
    отправляет в группу с summary «было/стало».

    Параметры:
      series, date: ключ к встрече.
      instruction: команда Ильи целиком (например «удали задачу 1 из …»).
      in_group: True — пересылка в группу (delete старого msg в окне 48ч);
                False — file-only коррекция.
      root: override `~/Projects/me/встречи/`.
      meeting_sid: для лога.

    Возвращает dict:
      {status: "applied"|"file-only"|"error",
       kind: "targeted_remove"|"structural"|"none",
       version_path: str|None,
       in_group_action: "deleted-old+sent-new"|"sent-new-with-warning"|"file-only"|"none"|"none-no-binding"|None,
       summary_sent: bool,
       error?: str}
    """
    transcript_path, protocol_path, meta_json_path = _resolve_protocol_paths(series, date, root=root)
    if not protocol_path or not protocol_path.is_file():
        return {
            "status": "error",
            "kind": "none",
            "version_path": None,
            "in_group_action": None,
            "summary_sent": False,
            "error": f"protocol not found for series={series} date={date}",
        }

    try:
        old_text = protocol_path.read_text(encoding="utf-8")
    except OSError as e:
        return {
            "status": "error",
            "kind": "none",
            "version_path": None,
            "in_group_action": None,
            "summary_sent": False,
            "error": f"read failed: {e}",
        }

    # 1. Сохраняем версию vN.
    version_path = _save_protocol_version(protocol_path)

    # 2. Классифицируем и применяем правку.
    kind, target_n = _classify_instruction(instruction)
    new_text: Optional[str] = None
    if kind == "targeted_remove" and target_n:
        new_text = _apply_targeted_remove(old_text, target_n)
        if new_text is None:
            logger.warning(
                "[correction] meeting=%s targeted_remove(%d) не сработал — fallback structural",
                meeting_sid or "?", target_n,
            )
            kind = "structural"

    if kind == "structural":
        # Структурная правка через regenerate_protocol с prompt-инъекцией.
        if transcript_path and transcript_path.is_file():
            try:
                method_text = _load_method_text()
            except RuntimeError as e:
                return {
                    "status": "error",
                    "kind": "structural",
                    "version_path": str(version_path) if version_path else None,
                    "in_group_action": None,
                    "summary_sent": False,
                    "error": f"method load failed: {e}",
                }
            try:
                transcript_md = transcript_path.read_text(encoding="utf-8")
            except OSError as e:
                return {
                    "status": "error",
                    "kind": "structural",
                    "version_path": str(version_path) if version_path else None,
                    "in_group_action": None,
                    "summary_sent": False,
                    "error": f"transcript read failed: {e}",
                }
            # У4 (цикл5/ход3): инструкция Ильи идёт в `meeting_meta` →
            # user_prompt (через _format_protocol_user_prompt). Это даёт ей
            # приоритет над общими правилами методички (которые Sonnet
            # читает в system_prompt). Так Sonnet увидит «коррекция от
            # пользователя» ПЕРЕД анализом transcript'а.
            try:
                meeting_meta_for_regen = {
                    "series": series,
                    "date": date,
                    "transcript_filename": transcript_path.name,
                    "correction_instruction": instruction.strip(),
                }
                new_text = generate_protocol(
                    transcript_md,
                    meeting_meta_for_regen,
                    method_text=method_text,
                    meeting_sid=meeting_sid,
                )
            except ProtocolGenerationError as e:
                return {
                    "status": "error",
                    "kind": "structural",
                    "version_path": str(version_path) if version_path else None,
                    "in_group_action": None,
                    "summary_sent": False,
                    "error": f"regen failed: {e}",
                }
        else:
            return {
                "status": "error",
                "kind": "structural",
                "version_path": str(version_path) if version_path else None,
                "in_group_action": None,
                "summary_sent": False,
                "error": "transcript missing — structural correction requires transcript",
            }

    if new_text is None or new_text.strip() == old_text.strip():
        return {
            "status": "error",
            "kind": kind,
            "version_path": str(version_path) if version_path else None,
            "in_group_action": None,
            "summary_sent": False,
            "error": "no effective change",
        }

    # 3. Atomic write нового .md.
    try:
        _atomic_write_text(protocol_path, new_text)
    except OSError as e:
        return {
            "status": "error",
            "kind": kind,
            "version_path": str(version_path) if version_path else None,
            "in_group_action": None,
            "summary_sent": False,
            "error": f"write failed: {e}",
        }
    logger.info(
        "[correction] applied meeting=%s kind=%s version=%s",
        meeting_sid or "?", kind,
        version_path.name if version_path else "?",
    )

    if not in_group:
        return {
            "status": "file-only",
            "kind": kind,
            "version_path": str(version_path) if version_path else None,
            "in_group_action": "file-only",
            "summary_sent": False,
        }

    # 4. In-group отправка. Берём delivered из meta.json.
    delivered = None
    if meta_json_path:
        meta = _read_meta_json(meta_json_path)
        if meta:
            delivered = meta.get("delivered")
    chat_id: Optional[int] = None
    if isinstance(delivered, dict):
        d_chat = delivered.get("chat_id")
        if isinstance(d_chat, int):
            chat_id = d_chat
    if chat_id is None:
        # Нет привязки → пытаемся взять из watched.yaml.
        cid = _load_watched_for_series(series) if series else None
        if cid:
            chat_id = cid
    if chat_id is None:
        return {
            "status": "applied",
            "kind": kind,
            "version_path": str(version_path) if version_path else None,
            "in_group_action": "none-no-binding",
            "summary_sent": False,
        }

    bot_token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    if not bot_token:
        return {
            "status": "applied",
            "kind": kind,
            "version_path": str(version_path) if version_path else None,
            "in_group_action": "none",
            "summary_sent": False,
            "error": "no bot token",
        }

    # 4a. Окно 48ч? Если delivered.at + 48ч > now → можно удалить старое.
    can_delete = False
    if isinstance(delivered, dict):
        at_iso = delivered.get("at")
        if isinstance(at_iso, str):
            try:
                at_dt = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
                if at_dt.tzinfo is None:
                    at_dt = at_dt.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - at_dt).total_seconds()
                can_delete = age < DELETE_MESSAGE_WINDOW_SEC
            except ValueError:
                pass

    deleted_n = 0
    if can_delete and isinstance(delivered, dict):
        msgs = delivered.get("message_ids") or []
        for msg_id in msgs:
            try:
                ok = telegram_api.delete_message(bot_token, chat_id, int(msg_id))
                if ok:
                    deleted_n += 1
            except (TypeError, ValueError):
                continue

    # 4b. Отправка новой версии.
    chunks = telegram_api.split_long_message(new_text, max_len=DELIVERY_MAX_LEN)
    new_msg_ids: list[int] = []
    for chunk in chunks:
        try:
            result = telegram_api.send_message(bot_token, chat_id, chunk)
        except telegram_api.TelegramApiError as e:
            return {
                "status": "applied",
                "kind": kind,
                "version_path": str(version_path) if version_path else None,
                "in_group_action": "sent-new-with-warning",
                "summary_sent": False,
                "error": f"send failed: {e}",
            }
        new_msg_ids.append(int(result.get("message_id") or 0))

    # 4c. Сводка «было/стало» через Haiku.
    summary_text = _compose_correction_summary(
        old_text, new_text, {"series": series, "date": date}, meeting_sid=meeting_sid,
    )
    if not can_delete:
        summary_text = (
            "⚠️ Старая версия протокола выше осталась — Telegram запрещает "
            "удалять сообщения старше 48 часов. Используйте обновлённый "
            "протокол ниже.\n\n"
        ) + summary_text
    summary_sent = False
    try:
        telegram_api.send_message(bot_token, chat_id, summary_text)
        summary_sent = True
    except telegram_api.TelegramApiError as e:
        logger.warning("[correction] summary send failed: %s", e)

    # 4d. Обновляем meta.delivered → новые message_ids + history.
    if meta_json_path:
        history_entry = {
            "at": _now_iso(),
            "chat_id": chat_id,
            "message_ids": list(delivered.get("message_ids") or []) if isinstance(delivered, dict) else [],
            "reason": "correction",
        }
        prev_history = []
        if isinstance(delivered, dict):
            prev_history = list(delivered.get("history") or [])
        new_delivered = {
            "chat_id": chat_id,
            "message_ids": new_msg_ids,
            "at": _now_iso(),
            "history": prev_history + [history_entry],
        }
        _update_meta_delivered(meta_json_path, new_delivered)

    action = "deleted-old+sent-new" if can_delete and deleted_n else "sent-new-with-warning"
    logger.info(
        "[correction] in-group meeting=%s deleted_old=%d sent_new=%d summary_sent=%s",
        meeting_sid or "?", deleted_n, len(new_msg_ids), summary_sent,
    )
    return {
        "status": "applied",
        "kind": kind,
        "version_path": str(version_path) if version_path else None,
        "in_group_action": action,
        "summary_sent": summary_sent,
    }


# ===========================================================================
# Ф5 (5.2): LLM-постпроход «подозрительные числа / инверсии» → ⚠️ «проверь»
# ===========================================================================
#
# ВОПР1 → вариант А: НЕ авто-правим (финансы — цена ошибки авто-правки выше,
# чем пропущенная пометка). Только помечаем ⚠️ в протоколе и оставляем
# решение Илье.
#
# === ПРОЕКТНАЯ ЗАМЕТКА ДЛЯ Ф6/Ф7 (объединение в ОДИН claude-вызов) =========
# `review_protocol()` спроектирован как ЕДИНЫЙ ревью-проход, расширяемый по
# секциям (`checks`):
#   - "values"  (Ф5, ЗДЕСЬ)  — подозрительные числа / отрицания / антонимы.
#   - "roles"   (Ф6 6.2)     — у одного спикера смешаны разные роли/темы.
#   - "memory"  (Ф7 7.3)     — сверка с выжимками прошлых встреч серии.
# Чтобы НЕ плодить три отдельных claude-вызова на одну встречу, Ф6 и Ф7
# добавляют свою секцию в `_build_review_system_prompt(checks)` и свой
# разбор в `_parse_review_response()` + обработчик в `apply_review_flags()`.
# Тогда finalize зовёт review_protocol(checks=("values","roles","memory"))
# ОДИН раз, а не три. Контракт ответа — JSON с ключами по именам секций.
# ===========================================================================

# Модель ревью-прохода. Sonnet 4.6 — нужна аккуратность в сверке чисел и
# смысла «свободно↔занято»; Haiku на это слишком слаб (даёт false-positive).
PROTOCOL_REVIEW_MODEL = "claude-sonnet-4-6"

# Маркер, который вставляется в строку протокола. Якорим по нему идемпотентность
# (повторный проход не плодит дубли ⚠️).
REVIEW_FLAG_MARKER = "⚠️"


class ProtocolReviewError(RuntimeError):
    """Сбой ревью-прохода (CLI/claude/parse)."""


def _is_protocol_review_enabled() -> bool:
    """Kill-switch ревью-прохода 5.2: `ENABLE_PROTOCOL_REVIEW` (дефолт ON;
    `0/false/no` → OFF). Нужен, чтобы при сбое в проде отключить лишний
    claude-вызов в hot-path без передеплоя кода."""
    raw = (os.environ.get("ENABLE_PROTOCOL_REVIEW") or "").strip().lower()
    return raw not in ("0", "false", "no")


_REVIEW_VALUES_SECTION = """### Секция "values" — подозрительные числа и инверсии смысла

Сверь КАЖДОЕ число и КАЖДОЕ утверждение в протоколе с транскриптом. Помечай ТОЛЬКО реально подозрительное:
- число в протоколе не сходится с транскриптом (например «28 млн» в протоколе, а в речи «2,8 миллиона» — порядок/запятая) или внутренне противоречиво;
- отрицание/утверждение перевёрнуто («хватает» ↔ «не хватает», «успеваем» ↔ «не успеваем»);
- антонимная пара перепутана («свободно» ↔ «занято», «вырос» ↔ «упал», «дороже» ↔ «дешевле»).

НЕ помечай: стилистику, формулировки, орфографию, округления, которые явно следуют из речи. Лучше пропустить сомнительное, чем зашуметь — порог высокий. Если не уверен, что это ошибка, — НЕ помечай."""


_REVIEW_ROLES_SECTION = """### Секция "roles" — у одного спикера смешаны разные роли/темы

Признак того, что диаризация склеила ДВУХ РАЗНЫХ людей в одного спикера: под одним именем (или одним «Спикер N») в протоколе идут реплики/задачи/решения из явно РАЗНЫХ функциональных зон, которые обычно ведут разные люди — например финансы И кадры, продажи И склад/логистика, разработка И бухгалтерия.

Что делать:
- Для КАЖДОГО спикера мысленно собери, в каких темах он фигурирует в протоколе (по задачам, решениям, репликам).
- Если у одного спикера сходятся 2+ заметно разные функциональные зоны, которые в норме закреплены за разными ролями → пометь этого спикера на ручную сверку: «не два ли это человека под одним кластером».
- `quote` = имя спикера ровно как в протоколе (или «Спикер N», если имя не подставлено). `note` = какие именно зоны смешаны (3–7 слов, напр. «смешаны финансы и кадры»). `section` = "roles".

НЕ помечай: руководителя/владельца, который ПО РОЛИ ведёт много тем сразу — это норма, а не склейка. Один спикер с одной зоной + парой смежных вопросов — норма. Порог высокий: это флаг на РУЧНУЮ сверку, а не утверждение об ошибке. Если зоны смежные или это явно один человек широкого профиля — НЕ помечай."""


_REVIEW_MEMORY_SECTION = """### Секция "memory" — протокол не должен втягивать прошлое как факт

Этот протокол мог генерироваться со СПРАВКОЙ о прошлых встречах серии (имена, термины, прошлые числа — как контекст). Дисциплина (Ф7 7.4): факты протокола — решения, задачи, числа, договорённости — должны опираться ТОЛЬКО на текущий транскрипт. Прошлое — лишь для распознавания имён/терминов и понимания динамики чисел.

Помечай, если в протоколе есть СОДЕРЖАТЕЛЬНЫЙ факт (тема, решение, задача, число, договорённость), которого НЕТ в текущем транскрипте — выглядит перенесённым из прошлого контекста, а не сказанным на этой встрече.

`quote` = точная подстрока протокола с неподтверждённым фактом. `note` = коротко (3-7 слов), напр. «нет в записи — из прошлого?». `section` = "memory".

НЕ помечай: имена участников и устоявшиеся термины/названия проектов — их подстановка из памяти серии это НОРМА, а не ошибка. Помечай только факты/числа/решения без опоры на текущую запись. Порог высокий: лучше пропустить сомнительное, чем зашуметь."""


def _build_review_system_prompt(checks: tuple[str, ...]) -> str:
    """Собирает system-prompt ревью-прохода из включённых секций.

    Ф7 добавит сюда свою секцию "memory" (см. проектную заметку выше).
    """
    sections: list[str] = []
    if "values" in checks:
        sections.append(_REVIEW_VALUES_SECTION)
    if "roles" in checks:
        sections.append(_REVIEW_ROLES_SECTION)
    if "memory" in checks:  # Ф7 (7.4): дисциплина «прошлое = справка, не факт»
        sections.append(_REVIEW_MEMORY_SECTION)
    sections_text = "\n\n".join(sections)
    allowed_sections = ", ".join(f'"{c}"' for c in checks) or '"values"'
    section_enum = "|".join(checks) or "values"
    return (
        "Ты — придирчивый проверяющий протокола встречи. Тебе дан готовый "
        "протокол и исходный транскрипт. Твоя задача — НАЙТИ подозрительные "
        "места и вернуть их списком. Ты НИЧЕГО не правишь сам.\n\n"
        + sections_text
        + "\n\nОтвет — СТРОГО JSON-объект без markdown-обёртки:\n"
        '{"findings": [{"section": "' + section_enum + '", "quote": "<для values/'
        'memory — точная подстрока из протокола (буллет/фраза, где проблема); для '
        'roles — имя спикера ровно как в протоколе>", "note": "<коротко (3-7 слов) что '
        'проверить>"}]}\n'
        f"Поле `section` — одно из: {allowed_sections} (по тому, какая секция "
        "выше дала находку).\n"
        "Если подозрительного нет — верни {\"findings\": []}. "
        "Для секции values `quote` должен быть ДОСЛОВНОЙ подстрокой протокола "
        "(можно неполная строка, но без перефраза) — по ней пометка встанет "
        "на нужное место."
    )


def _parse_review_response(raw: str) -> list[dict]:
    """Парсит JSON-ответ ревью-прохода в список findings. Терпим к обёртке.

    Возвращает список dict'ов `{section, quote, note}`. На мусор — [].
    """
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    # Вырезаем первый JSON-объект, если модель добавила прозу вокруг.
    if not text.lstrip().startswith("{"):
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            text = m.group(0)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[review] не распарсил JSON ответа (len=%d)", len(raw))
        return []
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        return []
    out: list[dict] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        quote = (f.get("quote") or "").strip()
        note = (f.get("note") or "").strip()
        if not quote or not note:
            continue
        out.append({
            "section": (f.get("section") or "values").strip() or "values",
            "quote": quote,
            "note": note,
        })
    return out


def _normalize_for_match(s: str) -> str:
    """Нормализует строку для нечёткого поиска quote в протоколе.

    Схлопывает пробелы, нижний регистр И срезает markdown-эмфазу (`*`, `_`,
    backtick) — claude нередко цитирует текст без bold-обёртки, а в протоколе
    он жирный (`**свободен**`). Только для матча; в вывод пишем исходную строку.
    """
    out = re.sub(r"[*_`]+", "", (s or ""))
    return re.sub(r"\s+", " ", out).strip().lower()


def _format_tail_finding(f: dict) -> str:
    """Строка хвостового блока «## ⚠️ Проверить» для одного finding.

    roles (6.2): спикер-уровневый флаг — `⚠️ <спикер>: <что смешано>`.
    values (5.2): не нашли строку для inline — `⚠️ <note> — «<quote>»`.
    """
    if f.get("section") == "roles":
        return f"{REVIEW_FLAG_MARKER} {f['quote']}: {f['note']}"
    return f"{REVIEW_FLAG_MARKER} {f['note']} — «{f['quote']}»"


def apply_review_flags(protocol_text: str, findings: list[dict]) -> str:
    """Вставляет ⚠️-пометки в протокол по findings (ВОПР1 → НЕ авто-правит).

    values (5.2) — построчно: ищет строку протокола, содержащую `quote`
    (нечётко, по схлопнутым пробелам/регистру). Нашёл → дописывает в конец
    строки ` ⚠️ проверь: <note>`. Не нашёл — складывает в хвостовой блок
    «## ⚠️ Проверить».

    roles (6.2) — спикер-уровневые: один спикер фигурирует во многих строках
    протокола (задачи/решения по разным темам), поэтому inline-привязка к
    одной строке бессмысленна. Такие findings всегда идут в хвостовой блок
    как флаг «<спикер>: смешаны зоны — проверь».

    Идемпотентно: если в строке уже есть ⚠️ с этим note — не дублирует;
    хвостовой блок не плодится при повторном проходе. Чистая функция
    (без IO/claude) — основной объект unit-тестов 5.2/6.2.
    """
    if not findings:
        return protocol_text
    lines = protocol_text.split("\n")
    norm_lines = [_normalize_for_match(ln) for ln in lines]
    unmatched: list[dict] = []
    for f in findings:
        # roles — всегда спикер-уровневый флаг в хвостовой блок (см. docstring).
        if f.get("section") == "roles":
            if f.get("quote") and f.get("note"):
                unmatched.append(f)
            continue
        quote_norm = _normalize_for_match(f["quote"])
        note = f["note"]
        if not quote_norm:
            continue
        # Ищем самую короткую подходящую строку (точнее попадание).
        best_idx = -1
        for i, nl in enumerate(norm_lines):
            if not nl or not lines[i].strip():
                continue
            if quote_norm in nl:
                if best_idx == -1 or len(nl) < len(norm_lines[best_idx]):
                    best_idx = i
        if best_idx == -1:
            unmatched.append(f)
            continue
        flag = f"{REVIEW_FLAG_MARKER} проверь: {note}"
        # Идемпотентность: этот note уже стоит на строке?
        if flag in lines[best_idx]:
            continue
        lines[best_idx] = lines[best_idx].rstrip() + f"  {flag}"
        norm_lines[best_idx] = _normalize_for_match(lines[best_idx])
    out = "\n".join(lines)
    if unmatched:
        block = [f"\n## {REVIEW_FLAG_MARKER} Проверить", ""]
        for f in unmatched:
            block.append(_format_tail_finding(f))
            block.append("")
        # Не дублируем блок при повторном проходе.
        if f"## {REVIEW_FLAG_MARKER} Проверить" not in out:
            out = out.rstrip() + "\n" + "\n".join(block).rstrip() + "\n"
    return out


def review_protocol(
    protocol_text: str,
    transcript_md: str,
    *,
    checks: tuple[str, ...] = ("values",),
    meeting_sid: Optional[str] = None,
    timeout: int = 120,
) -> list[dict]:
    """Ревью-проход (5.2): возвращает список findings (НЕ правит протокол).

    Единый расширяемый проход — см. ПРОЕКТНУЮ ЗАМЕТКУ выше (Ф6/Ф7 добавляют
    секции в `checks`, чтобы переиспользовать ЭТОТ claude-вызов). Best-effort:
    нет claude в PATH / сбой / пустой ответ → [] (finalize не валится).
    """
    if not _is_protocol_review_enabled():
        logger.info("[review] disabled by ENABLE_PROTOCOL_REVIEW=0")
        return []
    if not protocol_text or not protocol_text.strip():
        return []
    if not transcript_md or not transcript_md.strip():
        return []
    system_prompt = _build_review_system_prompt(checks)
    user_prompt = (
        "Протокол (проверяемый):\n\n" + protocol_text
        + "\n\n---\n\nТранскрипт (источник истины):\n\n" + transcript_md
    )
    try:
        raw = call_claude_print(
            user_prompt,
            system=system_prompt,
            timeout=timeout,
            model=PROTOCOL_REVIEW_MODEL,
        )
    except ClaudeCliNotInstalled:
        logger.warning("[review] `claude` не в PATH — пропуск ревью-прохода")
        return []
    except ClaudeCliError as e:
        logger.warning("[review] meeting=%s claude error: %s", meeting_sid or "?", str(e)[:200])
        return []
    findings = _parse_review_response(raw)
    logger.info(
        "[review] meeting=%s checks=%s findings=%d",
        meeting_sid or "?", ",".join(checks), len(findings),
    )
    return findings


def review_and_flag_protocol_file(
    protocol_path: Path,
    transcript_path: Path,
    *,
    checks: tuple[str, ...] = ("values",),
    meeting_sid: Optional[str] = None,
) -> int:
    """Высокоуровневая обёртка 5.2: читает протокол+транскрипт, прогоняет
    `review_protocol`, вписывает ⚠️ через `apply_review_flags`, atomic-write.

    Возвращает число вставленных пометок (0 — нечего/сбой). Best-effort:
    любой сбой → 0, файл не трогаем.
    """
    if not protocol_path.is_file() or not transcript_path.is_file():
        return 0
    try:
        protocol_text = protocol_path.read_text(encoding="utf-8")
        transcript_md = transcript_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[review] read failed: %s", e)
        return 0
    findings = review_protocol(
        protocol_text, transcript_md, checks=checks, meeting_sid=meeting_sid,
    )
    if not findings:
        return 0
    new_text = apply_review_flags(protocol_text, findings)
    if new_text == protocol_text:
        return 0
    try:
        _atomic_write_text(protocol_path, new_text)
    except OSError as e:
        logger.warning("[review] write failed %s: %s", protocol_path, e)
        return 0
    return len(findings)


# ===========================================================================
# Ф5 (5.4): авто-подстановка известных спикеров перед clarify
# ===========================================================================


def _is_known_person(
    name: str,
    people_names: list[str],
    expected_set: set[str],
) -> bool:
    """«Известный» = есть в expected_participants серии ИЛИ в people.md.

    people.md-членство: точное полное имя ЛИБО однозначный резолв
    короткого имени (через protocol_to_tg._resolve_full_name — там же
    защита от коллизий «2 Михаила» → не резолвится).
    """
    n = (name or "").strip()
    if not n:
        return False
    if n in expected_set:
        return True
    if people_names:
        if n in people_names:
            return True
        resolved = protocol_to_tg._resolve_full_name(n, people_names)
        if resolved != n and " " in resolved:
            return True
    return False


def auto_resolve_known_speakers(
    unclear_clusters: dict[str, dict],
    resolved_names: list[str],
    name_pool: list[str],
    *,
    people_names: Optional[list[str]] = None,
    expected_participants: Optional[list[str]] = None,
) -> dict[str, str]:
    """5.4: до clarify пытается ОДНОЗНАЧНО подставить известного участника.

    Подставляем ТОЛЬКО когда остаётся РОВНО один неразмеченный кластер И
    ровно один не-занятый известный кандидат (строгий 1:1). Любая
    неоднозначность (2+ кластера ИЛИ 2+ кандидата, в т.ч. «2 Михаила») →
    {} → отдаём Илье на clarify (защита от коллизий, ВОПР по 5.4).

    «Известный» = `expected_participants` серии ∪ people.md (см.
    `_is_known_person`). Память серии (Ф7) добавит свои имена в `name_pool` /
    `expected_participants` — хук без изменения этой функции.

    Args:
      unclear_clusters: {cluster_key: {...}} — неразмеченные кластеры.
      resolved_names: имена, уже привязанные к другим кластерам (заняты).
      name_pool: кандидаты (expected + участники панели).
      people_names: имена из people.md (см. protocol_to_tg._extract_names_from_people).
      expected_participants: ожидаемый состав серии.

    Returns:
      {cluster_key: name} для авто-подстановки (пусто, если неоднозначно).
    """
    if len(unclear_clusters) != 1:
        return {}
    expected_set = {
        (e or "").strip() for e in (expected_participants or []) if (e or "").strip()
    }
    occupied = {(r or "").strip() for r in (resolved_names or []) if (r or "").strip()}
    people_names = people_names or []
    candidates: list[str] = []
    seen: set[str] = set()
    for n in name_pool or []:
        nn = (n or "").strip()
        if not nn or nn in seen or nn in occupied:
            continue
        seen.add(nn)
        if _is_known_person(nn, people_names, expected_set):
            candidates.append(nn)
    if len(candidates) != 1:
        return {}
    (cluster_key,) = tuple(unclear_clusters.keys())
    return {cluster_key: candidates[0]}


# ===========================================================================
# Ф5 (5.5 + 5.6): до-сыл ИСПРАВЛЕННОЙ версии протокола (revision-маркер)
# ===========================================================================
#
# Контекст (digest Ф4→Ф5): поздний clarify-ответ уже перегенерирует
# `<date>-protokol.md` на диске (clarify_worker._apply_resolution), но в группу
# повторно НЕ дослыается (дизайн Ф3). 5.6 связывает «поздний clarify изменил
# протокол» → «дослать обновлённую версию в чат», ОБХОДЯ идемпотентность
# meta.delivered. 6ч-дедуп (notify.py) здесь не при чём — до-сыл идёт прямым
# telegram_api.send_message, а не через notify(). 5.5 — блок «🔁 Что
# изменилось» в досланной версии.
#
# РИСК (из промта): не сломать идемпотентность Ф1. Обычный повтор finalize
# по-прежнему даёт skip/rc=10 (deliver_protocol не трогаем). Обходит дедуп
# ТОЛЬКО этот путь — и ТОЛЬКО когда контент протокола реально изменился
# (content-hash), т.е. legitimate до-сыл, а не любой повторный вызов.

REVISION_SUMMARY_PROMPT = """Ты помогаешь сформулировать короткий блок «что изменилось» для пользователя после доразметки протокола встречи (поздно уточнили, кто говорил, и т.п.).

На вход — unified-diff между прошлой и новой версией протокола. Сформулируй 3–8 строк русского текста в формате:

🔁 Что изменилось в протоколе «<series>» <date>:

- <одно изменение одной строкой>
- <ещё одно, если есть>

Чаще всего меняется имя спикера (был «Спикер N» → стало имя) — так и пиши. Не дублируй протокол целиком, не выдумывай ничего сверх diff. Только готовый русский текст без markdown-обёртки и без префиксов."""


def _protocol_content_hash(text: str) -> str:
    """Стабильный хеш содержимого протокола (для revision-идемпотентности).

    Нормализуем хвостовые пробелы строк, чтобы косметика не считалась
    изменением. ⚠️-пометки 5.2 в хеш ВХОДЯТ (это смысловое изменение).
    """
    norm = "\n".join(ln.rstrip() for ln in (text or "").split("\n")).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _compose_revision_summary(
    old_text: str,
    new_text: str,
    meeting_meta: dict,
    *,
    meeting_sid: Optional[str] = None,
    timeout: int = 60,
) -> str:
    """Блок «🔁 Что изменилось» через Haiku по diff'у. Fallback — заводская строка."""
    import difflib
    series = meeting_meta.get("series") or "—"
    date = meeting_meta.get("date") or "—"
    fallback = f"🔁 Обновил протокол «{series}» {date} — уточнил детали, новая версия ниже."
    if not old_text or not new_text:
        return fallback
    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile="было", tofile="стало", lineterm="", n=2,
    ))
    if not diff_lines:
        return fallback
    if len(diff_lines) > 200:
        diff_lines = diff_lines[:200] + ["... (diff обрезан)"]
    user_prompt = f"Series: {series}\nDate: {date}\n\nDiff (unified):\n" + "\n".join(diff_lines)
    try:
        raw = call_claude_print(
            user_prompt, system=REVISION_SUMMARY_PROMPT,
            timeout=timeout, model=CORRECTION_SUMMARY_MODEL,
        )
    except ClaudeCliNotInstalled:
        logger.warning("[revision] `claude` не в PATH — fallback summary")
        return fallback
    except ClaudeCliError as e:
        logger.warning("[revision] summary CLI error meeting=%s: %s", meeting_sid or "?", e)
        return fallback
    text = (raw or "").strip()
    return text or fallback


def redeliver_revised_protocol(
    meeting_meta: dict,
    old_protocol_text: str,
    new_protocol_text: str,
    *,
    meta_json_path: Optional[Path],
    meeting_sid: Optional[str] = None,
    delete_previous: bool = False,
) -> dict:
    """До-сыл ИСПРАВЛЕННОЙ версии в ту же группу (5.5 + 5.6; Ф4 reissue правок).

    Вызывается из:
      - clarify_worker._apply_resolution (поздний clarify) — `delete_previous=False`
        (поведение Ф5 не меняется: старое сообщение остаётся, ревизия дослыается);
      - feedback_reissue (Ф4, правки реплаем) — `delete_previous=True` (FB5: старое
        доставленное сообщение+файл удаляются, постится новая версия).

    ОБХОДИТ идемпотентность meta.delivered (это легитимный до-сыл), шлёт блок
    «🔁 Что изменилось» + новую версию, помечает meta.delivered ревизией
    (revision++, content_hash) — чтобы повтор не задвоил.

    `delete_previous=True` (FB5): перед постингом удаляет прежние message_ids
    последней записи delivered (в 48-часовом окне Telegram). Старше 48ч / часть не
    удалилась → не падаем, дописываем в блок «что изменилось» предупреждение, что
    старая версия осталась выше (как `apply_correction`). Архив `_versions/` на
    диске пишет caller (Ф4) — здесь не трогаем.

    РИСК2 (перенос #3 bot-notarius-full): шапка ревизии бралась из `meeting_meta`,
    куда caller (clarify) клал `participants=[]` → пустой список участников в шапке.
    Чиним на уровне механизма: если в `meeting_meta` нет участников, обогащаем из
    полного meta.json (тот же источник, что обычная генерация) — фикс для ВСЕХ
    вызывающих.

    Гейты безопасности (РИСК 5.6 «не сломать Ф1»):
      - только если протокол УЖЕ был доставлен (delivered с message_ids) —
        иначе первичная доставка ещё впереди (grace окно) → not-delivered-yet;
      - только если контент реально изменился (content_hash) — иначе no-change;
      - то же содержимое, что уже помечено как revision-доставленное → skip.

    Возвращает {status: sent|not-delivered-yet|no-change|skipped|disabled|error, ...}.
    """
    if not _is_protocol_delivery_enabled():
        return {"status": "disabled"}
    if not new_protocol_text or not new_protocol_text.strip():
        return {"status": "error", "error": "empty new protocol"}

    new_hash = _protocol_content_hash(new_protocol_text)
    if old_protocol_text and _protocol_content_hash(old_protocol_text) == new_hash:
        return {"status": "no-change"}

    meta = _read_meta_json(meta_json_path) if meta_json_path else None
    records = _normalize_delivered(meta.get("delivered")) if meta else []

    # РИСК2: обогащаем участников из полного meta.json, если caller их не дал
    # (clarify клал participants=[]) — иначе шапка ревизии приходит пустой.
    meeting_meta = dict(meeting_meta or {})
    if meta:
        if not meeting_meta.get("expectedParticipants") and meta.get("expectedParticipants"):
            meeting_meta["expectedParticipants"] = meta.get("expectedParticipants")
        if not meeting_meta.get("participants") and meta.get("participants"):
            meeting_meta["participants"] = meta.get("participants")
    # Берём последнюю запись с реально отправленными message_ids.
    last = None
    for rec in reversed(records):
        if rec.get("message_ids"):
            last = rec
            break
    if last is None:
        # Ещё не доставляли (или delivered=skip/backfill) — первичная доставка
        # сама подхватит новую версию. До-сыл не нужен.
        return {"status": "not-delivered-yet"}
    if last.get("content_hash") == new_hash:
        # Эту версию уже дослали ревизией — идемпотентно молчим.
        return {"status": "skipped", "reason": "already-revised"}

    chat_id = last.get("chat_id")
    if not isinstance(chat_id, int):
        return {"status": "error", "error": "no chat_id in delivered record"}

    bot_token = (os.environ.get("TELEGRAM_NOTARIUS_BOT_TOKEN") or "").strip()
    if not bot_token:
        return {"status": "error", "error": "no bot token"}

    # Текущий номер ревизии.
    prev_rev = 0
    for rec in records:
        try:
            prev_rev = max(prev_rev, int(rec.get("revision") or 0))
        except (TypeError, ValueError):
            continue
    revision = prev_rev + 1

    # Шапка/подпись + рендер PDF — тем же `protocol_to_tg` + `protocol_to_pdf`, что
    # первичная доставка (`deliver_protocol`). Решение владельца 2026-06-08: ревизии
    # приходят в том же виде, что оригинал (PDF), а не текстом.
    date_for_pdf = meeting_meta.get("date")
    transcript_json_path = None
    if meta_json_path is not None:
        candidate = Path(meta_json_path).parent / "_transcripts" / f"{date_for_pdf}.json"
        if candidate.is_file():
            transcript_json_path = candidate
    caption = protocol_to_tg.build_pdf_caption(
        new_protocol_text, meeting_meta, transcript_json_path=transcript_json_path,
    )
    pdf_title, pdf_subtitle = protocol_to_tg.build_pdf_title_subtitle(
        new_protocol_text, meeting_meta, transcript_json_path=transcript_json_path,
    )
    safe_date = re.sub(r"[^0-9A-Za-z._-]", "-", str(date_for_pdf)) or "protokol"
    pdf_filename = f"protokol-{safe_date}.pdf"

    with tempfile.TemporaryDirectory(prefix="revision-pdf-") as td:
        pdf_path = Path(td) / pdf_filename
        # ход1/Н1: РЕНДЕРИМ PDF ДО удаления старого. Рендер — самый хрупкий шаг
        # (chromium/шрифты, README предупреждает про деградацию после apt upgrade).
        # Если он упадёт ПОСЛЕ delete — старый протокол уже удалён, новый не
        # отрендерен → в чате пусто. Поэтому рендерим первым: сбой здесь → return
        # без удаления, прежняя версия в чате цела.
        try:
            protocol_to_pdf.render_pdf_from_markdown(
                new_protocol_text, str(pdf_path),
                title=pdf_title, subtitle=pdf_subtitle,
            )
        except (protocol_to_pdf.PdfRenderError, OSError) as e:
            logger.error(
                "[revision] PDF render упал — старое НЕ трогаем meeting=%s chat_id=%s: %s",
                meeting_sid or "?", chat_id, e,
            )
            return {"status": "error", "chat_id": chat_id, "message_ids": [], "error": str(e)}

        # 0) FB5: удаляем прежнее доставленное сообщение(+файл) — только теперь, когда
        #    PDF на руках. Только delete_previous=True (Ф4 правок); clarify (Ф5) не
        #    удаляет. Telegram разрешает delete в окне 48ч — старше/не удалилось → не
        #    падаем, предупреждение в шапке «что изменилось».
        delete_warning = ""
        if delete_previous:
            can_delete = False
            at_iso = last.get("at")
            if isinstance(at_iso, str):
                try:
                    at_dt = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
                    if at_dt.tzinfo is None:
                        at_dt = at_dt.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - at_dt).total_seconds()
                    can_delete = age < DELETE_MESSAGE_WINDOW_SEC
                except ValueError:
                    pass
            old_mids = [m for m in (last.get("message_ids") or [])]
            deleted_n = 0
            if can_delete:
                for msg_id in old_mids:
                    try:
                        if telegram_api.delete_message(bot_token, chat_id, int(msg_id)):
                            deleted_n += 1
                    except (TypeError, ValueError):
                        continue
            if old_mids and (not can_delete or deleted_n < len(old_mids)):
                # Не смогли убрать всё старое → честно предупреждаем (FB5 фолбэк
                # «нельзя удалить — постит рядом»). Архив на диске не зависит от этого.
                delete_warning = (
                    "⚠️ Прежнюю версию протокола выше убрать не удалось "
                    "(Telegram не даёт удалять сообщения старше 48 часов). "
                    "Ниже — актуальная версия.\n\n"
                )
            logger.info(
                "[revision] FB5 delete_previous meeting=%s can_delete=%s deleted=%d/%d",
                meeting_sid or "?", can_delete, deleted_n, len(old_mids),
            )

        # 1) Блок «🔁 Что изменилось» — отдельным сообщением ПЕРВЫМ.
        summary = _compose_revision_summary(
            old_protocol_text, new_protocol_text,
            {"series": meeting_meta.get("series"), "date": meeting_meta.get("date")},
            meeting_sid=meeting_sid,
        )
        try:
            telegram_api.send_message(bot_token, chat_id, delete_warning + summary)
        except telegram_api.TelegramApiError as e:
            logger.warning("[revision] summary send failed meeting=%s: %s", meeting_sid or "?", e)

        # 2) Новая версия протокола — PDF-документом (отрендерен выше).
        try:
            result = telegram_api.send_document(
                bot_token, chat_id, str(pdf_path),
                caption=caption, filename=pdf_filename,
            )
        except telegram_api.TelegramApiError as e:
            logger.error(
                "[revision] PDF send упал meeting=%s chat_id=%s: %s",
                meeting_sid or "?", chat_id, e,
            )
            return {"status": "error", "chat_id": chat_id, "message_ids": [], "error": str(e)}
        sent_ids: list[int] = [int(result.get("message_id") or 0)]

    # 3) meta.delivered ← новая запись с revision-маркером и content_hash.
    #    replace_for_chat_id=True: следующий тик collector'а увидит свежие
    #    message_ids → deliver_protocol даст idempotent skip (Ф1 цела).
    at = _now_iso()
    if meta_json_path:
        prev_history = list(last.get("history") or [])
        prev_history.append({
            "at": at, "chat_id": chat_id,
            "message_ids": list(last.get("message_ids") or []),
            "reason": "revision", "revision": revision,
        })
        _update_meta_delivered(meta_json_path, {
            "chat_id": chat_id,
            "message_ids": sent_ids,
            "at": at,
            "decision": "revision",
            "revision": revision,
            "content_hash": new_hash,
            "document": True,
            "history": prev_history,
        })
    logger.info(
        "[revision] sent meeting=%s chat_id=%s rev=%d parts=%d",
        meeting_sid or "?", chat_id, revision, len(sent_ids),
    )
    return {
        "status": "sent",
        "chat_id": chat_id,
        "message_ids": sent_ids,
        "revision": revision,
        "content_hash": new_hash,
    }
