"""Гейт доставки протокола в групповой чат серии (Ф3 плана eviction-recovery: R4c + R13-доставка).

ЧИСТАЯ (без IO/docker/Telegram) логика «пускать ли финальный протокол в
групповой чат серии или подавить как обрывок/провал». Вынесена из
`finalize-meeting.py`, чтобы покрыть её unittest'ом без подъёма тяжёлого
STT-пайплайна (как Ф1 вынесла R6-хелперы в collector).

Решение принимается по ДВУМ сигналам:
  1. `endReason` — метка причины конца встречи, которую ставит бот-сторона
     (`recording.ts`) в `meta`. Ф2 ввела новые причины (старого плоского
     `evicted` больше НЕТ):
       • `evicted_normal_end`  — полная запись, выброс-в-конце = норма  → ДОСТАВЛЯТЬ.
       • `evicted_failure`     — нет существенной записи / перезаход исчерпан → подавить
                                  ОБРЫВОК (R4c), НО см. п.2 (FU-4).
       • `deaf_after_start`    — бот оглох при живом `/j/`; до оглушения мог записать
                                  реальную встречу → подавить только если несущественно.
       • `evicted_recovered`   — НЕ терминальный: после возврата endReason сбрасывается
                                  в `"unknown"`; этот гейт его не встретит.
  2. СУЩЕСТВЕННОСТЬ записи (число реплик) — FU-4: короткую ПОЛНУЮ встречу (<25 мин,
     завершённую хостом через bounce-на-корень и неперезаходимую), ошибочно
     помеченную `evicted_failure` порогом A7=25 мин, НЕЛЬЗЯ терять только из-за метки.
     Поэтому подавляем по СУЩЕСТВЕННОСТИ записи, а не только по метке reason. Это снимает
     главный РИСК1 плана («лекарство бьёт по здоровым встречам»).

Опасная тройка (R11): сюда приходят только МЕТАДАННЫЕ (строка-причина, счётчики
реплик), текста реплик/имён/транскрипта НЕТ.
"""
from __future__ import annotations

from dataclasses import dataclass

# Причины старого образца: запись по своей природе пустая (никто не пришёл /
# был в комнате, но не получил звук). Бот в момент встречи их НЕ алертил →
# finalize шлёт ЛИЧНЫЙ owner-alert. Подавляются ВСЕГДА, без оглядки на объём.
HARD_FAILURE_REASONS = frozenset({"no_one_joined", "deaf_with_participants"})

# Новые причины Ф2 (детект-сторона): бот УЖЕ позвал на помощь в момент детекта
# (R4a лично + R4b в групповой чат для выброса; личный алерт для deaf). Подавляются
# ТОЛЬКО при несущественной записи (FU-4) — иначе короткая полная встреча потерялась
# бы. При подавлении finalize НЕ дублирует ни групповое, ни личное сообщение —
# бот уже отзвонился.
BOT_ALERTED_FAILURE_REASONS = frozenset({"evicted_failure", "deaf_after_start"})

# Порог «существенности» записи в числе реплик (turns протокола / utterances
# внешнего STT). Ниже порога запись считаем обрывком/мусором (ISS-28: запись
# тишины → почти-пустой протокол на пару реплик). Реальная даже короткая встреча
# даёт кратно больше. Дефолт, владелец валидирует на наблюдении 7 июля (как порог
# A7 в Ф2). Обоснование выбора числа — §3 пакета Ф3.
SUBSTANTIAL_MIN_REPLIES = 3


@dataclass(frozen=True)
class DeliveryDecision:
    """Вердикт гейта доставки. Только метаданные (R11), без текста встречи."""

    deliver: bool               # пускать ли протокол в групповой чат серии
    suppressed: bool            # подавлено как провал/обрывок (== not deliver)
    reason: str                 # нормализованный endReason (strip)
    reply_count: int            # число реплик, по которому мерили существенность
    substantial: bool           # запись существенна (>= порога реплик)
    bot_already_alerted: bool   # бот уже звал на помощь в момент детекта → finalize молчит


def evaluate_delivery(
    *,
    end_reason: str | None,
    turn_count: int,
    utter_count: int | None,
    min_replies: int = SUBSTANTIAL_MIN_REPLIES,
) -> DeliveryDecision:
    """Решает, доставлять ли финальный протокол в групповой чат серии.

    Параметры (всё — метаданные, без текста реплик):
      end_reason  — строка причины конца из `meta` (нормализуется strip'ом).
      turn_count  — число реплик-ходов итогового протокола (`len(turns)`),
                    backend-agnostic (есть и у внутреннего whisper_pyannote, и у
                    внешнего STT).
      utter_count — число распознанных реплик внешнего STT (None для внутреннего
                    whisper_pyannote — пустоту по нему детектим только у внешнего STT,
                    иначе каждая внутренняя встреча ложно помечалась бы провалом).
      min_replies — порог существенности (по умолчанию SUBSTANTIAL_MIN_REPLIES).

    Возвращает DeliveryDecision. Логика:
      • `no_one_joined` / `deaf_with_participants`     → подавить ВСЕГДА.
      • внешний STT нашёл 0 реплик (utter_count == 0)  → подавить ВСЕГДА (пустой
        протокол не шлём даже при evicted_normal_end).
      • `evicted_failure` / `deaf_after_start`          → подавить ТОЛЬКО если запись
        несущественна (FU-4: иначе короткая полная встреча доставляется).
      • `evicted_normal_end` / прочее с записью          → ДОСТАВИТЬ.
    """
    reason = (end_reason or "").strip()

    # Существенность — по числу реплик. Приоритет у распознанных реплик внешнего
    # STT (прод AssemblyAI), иначе число реплик-ходов протокола (внутренний бэкенд,
    # где utter_count=None). Оба — «сколько реального разговора записали».
    reply_count = utter_count if utter_count is not None else turn_count
    substantial = reply_count >= min_replies

    # Универсальный жёсткий провал внешнего STT: 0 распознанных реплик → доставлять
    # нечего при ЛЮБОЙ причине (пустой протокол в группу не шлём).
    empty_external = utter_count == 0

    bot_alerted = reason in BOT_ALERTED_FAILURE_REASONS

    failed = (
        reason in HARD_FAILURE_REASONS
        or empty_external
        or (bot_alerted and not substantial)
    )
    return DeliveryDecision(
        deliver=not failed,
        suppressed=failed,
        reason=reason,
        reply_count=reply_count,
        substantial=substantial,
        bot_already_alerted=bot_alerted,
    )


# ---------------------------------------------------------------------------
# Пометка «собран из присланной записи» (ISS-60, фаза 6, копилка владельца п.5)
# ---------------------------------------------------------------------------
#: Протокол, собранный из записи, которую прислали люди (бот встречу не записал), и
#: отправленный ботом ШТАТНО — путь Б обёртки восстановления (`tools/ingest-recording.py
#: --mode standard|auto`) или очередь повторов после сбоя расшифровки. 23.09.2026 такой
#: протокол ушёл в группу без пометки, и перепутанные имена приняли за выверенные.
#: Путь А обёртка доставляет сама со своей пометкой («диаризация не выверена»), поэтому
#: при выключенной доставке finalize пометку не ставит — иначе их было бы две.
RECOVERED_FROM_RECORDING_NOTE = (
    "> ♻️ **Протокол собран из присланной записи** — бот встречу не записал. Кто что "
    "сказал, определено автоматически и может быть перепутано: заметили ошибку — "
    "ответьте на этот протокол правкой."
)
#: Причина конца, которую ставит обёртка восстановления в синтетической meta.
RECOVERY_END_REASON = "manual_recovery"


def is_recovered_meeting(meta: dict | None) -> bool:
    """meta встречи, собранной из присланной записи (синтетика обёртки восстановления)."""
    m = meta if isinstance(meta, dict) else {}
    reason = str(m.get("endReason") or m.get("end_reason") or "").strip()
    return m.get("recovery") is True and reason == RECOVERY_END_REASON


def with_recovery_note(protocol_text: str, meta: dict | None, *, delivery_enabled: bool) -> str:
    """Текст протокола с пометкой «собран из присланной записи» — если это такая встреча
    и протокол уходит из finalize (доставка включена). Иначе — текст без изменений.

    Пометка идёт после первой строки-заголовка (шапка остаётся первой), иначе — в
    начало. Уже есть — не дублируется (повторная финализация той же встречи)."""
    text = protocol_text or ""
    if not delivery_enabled or not is_recovered_meeting(meta) or not text.strip():
        return text
    if RECOVERED_FROM_RECORDING_NOTE in text:
        return text
    stripped = text.lstrip("\n")
    first, _, rest = stripped.partition("\n")
    if first.startswith("#"):
        body = rest.lstrip("\n")
        return f"{first}\n\n{RECOVERED_FROM_RECORDING_NOTE}\n\n{body}" if body else (
            f"{first}\n\n{RECOVERED_FROM_RECORDING_NOTE}\n")
    return f"{RECOVERED_FROM_RECORDING_NOTE}\n\n{stripped}"
