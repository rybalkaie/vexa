# Notary post-processing pipeline

Скрипты Фазы 3 проекта **meeting-notary** ([план](../../../../me/plans/2026-05-26-bot-notarius-telemost.md)).

## Что делает

Берёт записанную ботом встречу (WAV + meta.json) и собирает финальный
Markdown-протокол: транскрипт по спикерам, имена там где удалось определить,
таймкоды.

```text
<sessionUid>.meta.json + <sessionUid>.wav
        │
        ▼
finalize-meeting.py
        │
        ├─► Whisper (полный WAV → segments)         lib/transcribe.py
        ├─► pyannote (полный WAV → speakers)        lib/diarize.py
        ├─► alignment + merge                        lib/align.py
        ├─► name mapping (3 источника)               lib/name_mapping.py
        └─► render markdown                          lib/render.py
        │
        ▼
$TELEMOST_PROTOCOL_DIR/<date>-<sessionUid>.md
```

## Зачем post-processing, а не стрим

Стримовая транскрипция Ф2 (3-сек чанки) — это **черновик для real-time мониторинга**.
Финальный протокол требует:
- Контекста ≥30 сек для качественной диарезации (pyannote);
- `condition_on_previous_text` в Whisper — работает только в рамках одного запроса.

## Зависимости

```bash
python3 -m venv ~/meeting-notary/venv
source ~/meeting-notary/venv/bin/activate
pip install -r requirements.txt
```

Требуется:
- **HF_TOKEN** — Hugging Face read-токен, принятые условия моделей
  `pyannote/speaker-diarization-3.1` и `pyannote/segmentation-3.0`.
  Получить: [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens),
  принять условия на странице модели.
- **TRANSCRIPTION_SERVICE_URL** — URL Vexa transcription-service
  (default: `http://127.0.0.1:8083/v1/audio/transcriptions`).
- **TRANSCRIPTION_SERVICE_TOKEN** — если у сервиса включён API_TOKEN.
- **`claude` CLI** (подписка Claude Code) — для LLM-маппинга имён
  `lib/llm_postprocess.map_speaker_names`. Включён по умолчанию; гейт —
  `ENABLE_LLM_NAME_MAPPING` (set to `0`/`false`/`no` чтобы выключить).
  Anthropic API ключ НЕ требуется (вызов идёт через `claude --print`).

## Использование

```bash
# Базовый вариант — meta.json указывает на WAV.
python3 finalize-meeting.py /opt/meeting-notary/_tmp/transcripts/2026-05-26-<uid>.meta.json

# С явными параметрами.
ENABLE_LLM_NAME_MAPPING=1 \
HF_TOKEN=hf_... \
python3 finalize-meeting.py \
    /opt/meeting-notary/_tmp/transcripts/2026-05-26-<uid>.meta.json \
    --output-dir ~/meeting-notary/_tmp/protocols \
    --num-speakers 2 \
    --keep-audio \
    -v
```

## Источники маппинга имён

Идут по убывающей надёжности:

1. **`telemost_list`** — список участников Telemost (бот polling'ит панель
   participants). Если ровно 1 кластер диарезации = 1 имя → прямое назначение.
2. **`regex_pymorphy3`** — vocative-обращения «Михаил, …» в репликах. Все
   падежные формы через pymorphy3. Бесплатно, локально, ~80% покрытия.
3. **`llm-postprocess`** — LLM-добивка для непривязанных «Спикер N» через
   `lib/llm_postprocess.map_speaker_names` (Claude Haiku 4.5 через
   `claude --print`). Возвращает `{cluster: (name, confidence)}` — confidence
   используется в Ф3 для clarify-flow. Гейт: `ENABLE_LLM_NAME_MAPPING`
   (дефолт ON; `0`/`false`/`no` чтобы выключить).

   **При правке `MAP_SPEAKER_NAMES_SYSTEM_PROMPT`** (промт в
   `lib/llm_postprocess.py`) — обязательно прогоняй smoke до коммита:
   ```bash
   # Из корня репо ~/Projects/meeting-notary/:
   .venv-cli/bin/python vexa/scripts/notary/tools/smoke_map_speaker_names.py
   ```
   Целевой результат: `2/2 правильных, оба confidence ≥ 0.85`. Без этого
   тюнинг тихо ломает прод (LLM начнёт занижать confidence → Ф3 clarify
   запускается на каждой встрече).

## Speechmatics client (`lib/speechmatics_client.py`)

Замена локальной связки `transcribe.py` (Whisper) + `diarize.py` (pyannote)
одним облачным вызовом. Закрывает узкое место CPU CCX13 (60–90 мин обработки
на час встречи → 15–30 сек). Интеграция в `finalize-meeting.py` — Ф2;
полный план: [`~/Projects/me/plans/2026-05-27-meeting-notary-speechmatics.md`](../../../../me/plans/2026-05-27-meeting-notary-speechmatics.md).

**Endpoint:** `POST https://asr.api.speechmatics.com/v2/jobs` (Batch API,
регион EU1). Полный цикл: submit → poll `/v2/jobs/<id>` каждые 5 сек до
`status=done` → fetch `/v2/jobs/<id>/transcript?format=json-v2`.

**Конфиг job'а:** `{language: "ru", diarization: "speaker", operating_point: "enhanced"}`.

**Формат ответа (json-v2):** массив `results[]` с двумя типами элементов:
- `type=word` — обычное слово, `alternatives[0].speaker` ("S1", "S2", …),
  `start_time`/`end_time` в секундах float;
- `type=punctuation` — знак препинания, **клеится к предыдущему слову без
  пробела** (естественная русская типографика). Парсер открывает новую
  реплику только при смене speaker'а на `type=word`; punctuation не триггерит
  смену.

**Контракт модуля** (фиксирован Ф1, см. `Utterance` в файле):
```python
Utterance = NamedTuple(speaker: str, start: float, end: float, text: str)
def transcribe_diarize_wav(wav_path: str | Path) -> list[Utterance]
def to_aligned_turns(utterances) -> list[AlignedTurn]   # adapter для Ф2
```

**Retry policy (Ф1, для smoke):** на 5xx/timeout — 1 быстрый retry через
30 сек, иначе raise. На `rejected` job → сразу raise (конфиг-ошибка). Полное
24-часовое retry-расписание — Ф2, на уровне `finalize-meeting.py` +
systemd-timer `meeting-notary-retry-failed.timer`.

**Smoke-команда (на VPS):**
```bash
sudo -u dev bash -c '
  cd /srv/meeting-notary/vexa/scripts
  set -a; source /srv/meeting-notary/.env.notary; set +a
  /home/dev/meeting-notary/venv/bin/python -m notary.lib.speechmatics_client <wav>
'
```
Выведет первые 10 реплик в формате `[start–end] Speaker S1: текст`.

**ADR (почему именно Speechmatics):**
[`~/Projects/anzhee-dealer-360/decisions/2026-05-27-stt-speechmatics.md`](../../../../anzhee-dealer-360/decisions/2026-05-27-stt-speechmatics.md).

## Ф3: Interactive clarification через Telegram

Если LLM-маппинг (`map_speaker_names`) дал низкий confidence хотя бы по
одному кластеру (порог — `CLARIFY_THRESHOLD`, дефолт `0.7`), или какой-то
кластер вообще не разрешён — `finalize-meeting.py` дополнительно:
1. Отправляет Илье в личку сообщение с цитатами реплик + inline keyboard
   (кнопки с вариантами имён + «Другое (текстом)»).
2. Атомарно пишет состояние в `_pending_clarification/<meeting_id>.json`.
3. **НЕ блокирует** финализацию: транскрипт уже на диске с «Спикер N» по
   unresolved cluster'ам.

Clarify-хендлеры (callback / текст / sweep таймаутов) встроены **в уже
работающий** `meetings_listener.py` daemon, который 24/7 long-poll'ит
`TELEGRAM_NOTARIUS_BOT_TOKEN` (`@ilya_protocol_meeting_bot`).

### Архитектурное замечание про Telegram-бот

Clarify + delivery (Ф6) + старый apply-reply на блок 📅 идут через **один**
бот `@ilya_protocol_meeting_bot` (env `TELEGRAM_NOTARIUS_BOT_TOKEN`).
Решение Ильи 2026-05-28: третий бот не плодим, переиспользуем существующий.

Технически: Telegram отдаёт `getUpdates` только одному потребителю на токен.
На VPS этого потребителя владеет уже работающий `meeting-notary-listener.service`
(systemd, аптайм с 2026-05-27, обрабатывает Reply Ильи на вечерний блок 📅).
Поэтому Ф3 не поднимает отдельный процесс — `lib/clarify_worker.py` стал
библиотекой хендлеров (`process_callback`, `process_text_message`,
`sweep_timeouts`), которые listener импортирует и вызывает в своём цикле.

`meetings_listener.py` теперь принимает оба типа апдейтов:
- `callback_query` — всегда clarify (inline-кнопки от Ф3).
- `message` с Reply на 📅 — старый apply-reply flow.
- `message` без Reply — если есть pending clarify state, передаётся в clarify;
  иначе — apply-reply (как было).

Periodic sweep таймаутов — каждые 30 секунд в основном цикле listener'а.

### Запуск (на VPS — уже запущен!)

Listener:
```bash
sudo systemctl status meeting-notary-listener.service
# active (running) since 2026-05-27, ловит и apply_reply, и clarify.

# После обновления кода — рестарт обязателен (Type=simple, новый код в памяти не подхватится):
sudo systemctl restart meeting-notary-listener.service
sudo journalctl -u meeting-notary-listener -f
```

Standalone `tools/run_clarify_worker.py` остался **только для локального
smoke на маке** (env `TELEGRAM_NOTARIUS_BOT_TOKEN`). В проде на VPS его
запускать НЕ нужно — listener делает то же самое.

### Env-флаги Ф3

| Env | Дефолт | Назначение |
|-----|--------|------------|
| `TELEGRAM_NOTARIUS_BOT_TOKEN` | _из .env.notary_ | Токен `@ilya_protocol_meeting_bot`, тот же что у listener'а. Без него clarify не шлётся. |
| `ENABLE_LLM_CLARIFY` | `1` | `0`/`false`/`no` отключает clarify целиком. |
| `CLARIFY_THRESHOLD` | `0.7` | Порог confidence — ниже → cluster идёт на уточнение. |
| `CLARIFY_TIMEOUT` | `420` | Секунд до `timed_out`. Поздний ответ только обновляет файл. |
| `CLARIFY_LONG_POLL_TIMEOUT` | `25` | long-poll окно (используется только в standalone-mode). |
| `TELEGRAM_NOTARIUS_CHAT_ID` / `TELEGRAM_CHAT_ID` | _из .env.notary_ | chat_id Ильи (число). Один из двух обязателен. |
| `MEETING_NOTARY_PENDING_DIR` | авто | Где хранить state-файлы. |

## Ф4: LLM-генератор протокола встречи (`generate_protocol`)

После того как транскрипт записан и (опц.) ушёл clarify, finalize-meeting.py
автоматически генерирует `<series>/<date>-protokol.md` рядом с транскриптом
через Claude Sonnet 4.6. Промт собирается из:
- системной части (`GENERATE_PROTOCOL_BASE_PROMPT`);
- содержимого метод-файла `kak-delat-protokol-vstrechi.md` — читается с диска
  в момент генерации (правки метода применяются сразу).

### Где живёт метод-файл

- **На маке (источник истины):** `~/Projects/me/methods/kak-delat-protokol-vstrechi.md`.
- **На VPS (рабочая копия):** `/opt/meeting-notary/_methods/kak-delat-protokol-vstrechi.md`
  — копируется launchd-агентом `com.ilarybalka.meeting-notary.methods-push`
  раз в час (см. ниже).
- Каталог задаётся env'ом `MEETING_NOTARY_METHODS_DIR` в `.env.notary`
  (на VPS) или дефолтится в путь мака.

### Cron rsync метода (push с мака на VPS)

Чтобы правки метода доезжали до VPS в течение часа:

1. **Скрипт push:** `~/.local/bin/meeting-notary-methods-push.sh`
   (rsync `~/Projects/me/methods/` → `meeting-notary:/opt/meeting-notary/_methods/`,
    SSH alias `meeting-notary`, `--include='*.md' --exclude='*' --delete`).
2. **launchd-агент:** `~/Library/LaunchAgents/com.ilarybalka.meeting-notary.methods-push.plist`
   (`StartInterval=3600`, `RunAtLoad=true`).
3. **Лог:** `~/Library/Logs/meeting-notary/methods-push.log`.

Установка / переустановка:

```bash
launchctl unload ~/Library/LaunchAgents/com.ilarybalka.meeting-notary.methods-push.plist 2>/dev/null
launchctl load -w ~/Library/LaunchAgents/com.ilarybalka.meeting-notary.methods-push.plist
launchctl list | grep meeting-notary.methods-push   # должно появиться
~/.local/bin/meeting-notary-methods-push.sh         # ручной прогон для проверки
ssh meeting-notary 'ls -la /opt/meeting-notary/_methods/'  # верификация на VPS
```

End-to-end smoke (правка доедет за секунды при ручном прогоне):

```bash
echo "TEST $(date +%s)" >> ~/Projects/me/methods/kak-delat-protokol-vstrechi.md
~/.local/bin/meeting-notary-methods-push.sh
ssh meeting-notary 'tail -1 /opt/meeting-notary/_methods/kak-delat-protokol-vstrechi.md'
# удалить тестовую строку и повторить push
```

Direction обоснован: push с мака не требует SSH-ключа от VPS на мак
(не расширяет attack surface). Минус — если мак выключен > 1 часа,
правки запаздывают; для одного редактора (Илья) это приемлемо.

### Ручная регенерация

CLI `tools/regenerate-protocol.py <series> <date>` — оборачивает
`regenerate_protocol_for_meeting`. Используется для backfill архивных
транскриптов и для отладки промта.

```bash
# На VPS (под production-venv):
cd /home/dev/meeting-notary && venv/bin/python vexa/scripts/notary/tools/regenerate-protocol.py sales-quality 2026-05-27

# На маке (под venv-cli — без pyannote/torch, того что нужно для генерации):
cd ~/Projects/meeting-notary && .venv-cli/bin/python vexa/scripts/notary/tools/regenerate-protocol.py sales-quality 2026-05-27 --duration 21 --participants "Илья Рыбалка,Михаил Саргин,Дарья Набережная,Михаил Еремеев"

# Backfill sales-quality рядом с эталоном:
... regenerate-protocol.py sales-quality-2026-05-27 2026-05-27 --out 2026-05-27-protokol-auto.md ...
```

### Telegram-команда «протокол <series> <date>»

В `@ilya_protocol_meeting_bot` Илья пишет в личку команду, бот находит
транскрипт, прогоняет генерацию, отправляет результат текстом + перезаписывает
файл на диске. Распознаются формы (регистронезависимо):

- «протокол sales-quality 2026-05-27»
- «сгенерируй протокол sales-quality 2026-05-27»
- «перегенерируй sales-quality 2026-05-27»
- «обнови протокол anzhee-direktorat 2026-06-01»

Парсер: `lib/protocol_command.py::parse_protocol_command`. Реализация
команды — `meetings_listener.maybe_route_to_protocol_command` (роутится
ДО clarify, имеет высший приоритет).

Если файл > 3500 символов — `lib/telegram_api.split_long_message` разрезает
по `---` (тематическим границам протокола) с маркером `(N/M)`.

### Env-флаги Ф4

| Env | Дефолт | Назначение |
|-----|--------|------------|
| `ENABLE_PROTOCOL_GENERATION` | `1` | `0`/`false`/`no` отключает автогенерацию в finalize. CLI и Telegram-команда работают всегда. |
| `MEETING_NOTARY_METHODS_DIR` | `/opt/meeting-notary/_methods` (VPS) / `~/Projects/me/methods` (мак) | Где искать `kak-delat-protokol-vstrechi.md`. |
| `MEETING_NOTARY_PROTOCOLS_DIR` | `~/Projects/me/встречи` | Корень для Telegram-команды (поиск transcript-файла). |

### Hook от clarify в protocol

После `clarify_worker._apply_resolution` (резолв или late-answer) hook
автоматически перегенерирует `<date>-protokol.md` рядом с обновлённым
транскриптом. Лог: `[protocol] regenerated meeting=<sid> via=clarify_resolved|clarify_late`.
Группу не уведомляем — Ф6 (доставка) сам решит что отправить с учётом
поля `delivered` в `meta.json`.

## Дисциплина «Опасной тройки» (Ф3)

См. [`~/Projects/meeting-notary/CLAUDE.md`](../../../CLAUDE.md), секция
«Опасная тройка приходит в Ф3»:
- НЕ логируем сам текст транскрипта — только metadata (длина, число спикеров, время).
- В промпт Claude кладём ТОЛЬКО непривязанные кластеры + их короткие реплики
  + список оставшихся имён. Не весь транскрипт.
- НЕ сохраняем сырой response Claude в файлы — только `{cluster: name | null}`
  результат маппинга в финальный meta.

## Удаление аудио

По дефолту WAV удаляется после успешного рендера протокола (экономия диска).
Чтобы сохранить — флаг `--keep-audio` или `keepAudio: true` в meta.json.
