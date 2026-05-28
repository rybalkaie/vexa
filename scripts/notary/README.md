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

## Ф5 — Извлечение задач из протокола + маршрутизация

После генерации протокола (Ф4) и до доставки в группу (Ф6) finalize-meeting
извлекает задачи через Claude Sonnet 4.6 и распределяет их:

- **owner=Илья** → `~/Projects/me/tasks.md`, раздел `## 📥 Актуальные`,
  atomic append через tempfile+rename. Формат строки — стандарт из
  [`~/Projects/me/methods/как-работать-с-системой-задач.md`](../../../../me/methods/как-работать-с-системой-задач.md).
- **owner=<имя_стейкхолдера>** И встреча 1:1 (`expectedParticipants` ровно 2:
  Илья + один из реестра) → его трек `companies/<co>/совещания/<slug>-otkrytye-voprosy.md`
  через `~/.local/bin/stakeholder-track.sh append <file> <section> <stdin>`.
  Подсекция `### 📋 Из встречи YYYY-MM-DD` — одна на встречу; первой строкой —
  ссылка на протокол.
- **owner=Спикер N** (имя не было смаплено) → `tasks.md` с маркером `[?]` +
  отдельное сводное уведомление Илье «N задач с нераспознанным владельцем».
- **owner=<имя> НЕ из реестра** или **встреча НЕ 1:1** → лог, skip (вне скоупа).

### Анти-галлюцинация задач

Порог по длительности встречи:

| Длительность | Порог задач |
|---|---|
| < 60 мин | > 7 |
| 60–120 мин | > 12 |
| > 120 мин | > 20 |

При превышении бот через `@ilya_protocol_meeting_bot` шлёт Илье список и
ждёт `CLARIFY_TIMEOUT=420 сек` (7 минут). Ответы: «оставить все» / «убрать 3,5,7».
Без ответа — оставляем все (поведение «лучше шум, чем потеря»).

### Clarification дедлайнов

Задачи Ильи без срока → отдельное сообщение «1=2026-06-05, 2=на этой неделе,
3=без срока». Парсер понимает: ISO даты, «сегодня/завтра», «на этой/следующей
неделе», «к понедельнику/.../воскресенью», «без срока».

Поздний ответ (после таймаута) обновляет строки в tasks.md (atomic write);
в группу повторно ничего не уходит (тот же контракт, что у Ф3).

### Реестр стейкхолдеров

Источник правды — [`~/Projects/me-dashboard/stakeholders.py`](../../../../me-dashboard/stakeholders.py).
На VPS дублируется как `stakeholders.json` в `_methods/`. Push через тот же
launchd-агент, что и методички (`meeting-notary-methods-push.sh` теперь
делает два rsync'а — методички и `stakeholders.json`).

`lib/stakeholders.py::load_stakeholders()` ищет JSON в env-пути → дефолтных
кандидатах → fallback на прямой `.py` import.

### Env-флаги Ф5

| Env | Дефолт | Назначение |
|-----|--------|------------|
| `ENABLE_TASK_EXTRACTION` | `1` | Выключить = `extract_tasks` всегда `[]`. |
| `ENABLE_TASK_ROUTING` | `1` | Выключить = задачи извлекаются, но не пишутся (дебаг промта). |
| `MEETING_NOTARY_TASKS_MD` | `~/Projects/me/tasks.md` | Целевой файл записи задач Ильи. |
| `MEETING_NOTARY_STAKEHOLDERS_JSON` | — | Явный путь к JSON-реестру. |
| `CLARIFY_TIMEOUT` | `420` (общий с Ф3) | Таймаут ответа на clarification по задачам. |

### Structured-лог Ф5

```
[extract_tasks] meeting=<sid> count=N elapsed=Xs threshold=K filtered=M model=claude-sonnet-4-6
[route_tasks] meeting=<sid> ilia=N others=M unknown=K pending_deadline=Q errors=E
[task-clarify] sent meeting=<sid> type=task_filter|task_deadlines tasks=N
[task-clarify] resolved meeting=<sid> type=... applied=Y
```

В финальный stdout finalize-meeting добавляется поле
`tasks_extracted: {ilia, others, pending_deadline, unknown_owner}`.

## Ф6 — Доставка протокола в Telegram-группу + correction flow

После генерации протокола (Ф4) и извлечения задач (Ф5) `finalize-meeting.py`
вызывает `deliver_protocol(...)` из `lib/llm_postprocess.py` — идемпотентная
отправка `.md` в Telegram-группу через `@ilya_protocol_meeting_bot`.

**Поток доставки:**

1. **chat_id** берётся из (а) явного `target_chat_id` параметра, (б)
   `meta.telegram_chat_id` если есть, (в) `watched.yaml` по
   `meeting_meta.series` через `cli.registry.get_telegram_chat_id_for_series`.
2. **Идемпотентность (УПУ1):** до отправки читает `meta.delivered` из
   `meta.json` финализированной встречи. Если `delivered.chat_id == target` и
   `len(delivered.message_ids) == expected_parts_count` → пропуск
   (`status="skipped"`), лог `[delivery] idempotent skip`.
3. **Split:** через `telegram_api.split_long_message` (Ф4), max 3500 символов.
   Каждая часть префиксится `(N/M)`.
4. **Отправка по частям:** после каждого успешного `send_message` →
   `meta.delivered.message_ids` обновляется atomic (`tempfile + os.rename`).
   Если упало на середине — частичный `delivered.decision="partial-failure"`,
   на следующей финализации idempotent skip пропустит уже отправленные.

**Если `telegram_chat_id` неизвестен:** `ask_delivery_destination(...)` шлёт
Илье в личку вопрос с inline keyboard («🚫 Не отправлять», «💬 В личку»);
state `_pending_clarification/<meeting_id>-delivery.json`. Listener
(`meetings_listener.maybe_route_to_delivery_text`) подбирает текстовый ответ:

- **chat_id (число)** → отправка + `_persist_telegram_chat_id(series, cid)`
  пишет привязку в `watched.yaml` через atomic upsert.
- **t.me/c/-ссылка** → парсер вычисляет `chat_id = -1000000000000 - <internal>`.
- **«в личку»** → отправка Илье в DM, привязка НЕ сохраняется.
- **«никуда»** → `delivered.decision="skip"`, ничего не шлём.
- **Таймаут** (`CLARIFY_TIMEOUT=420s`, общий с Ф3/Ф5) → `decision="skip-timeout"`.

**Correction flow** (команды в личке боту):

- `«удали задачу N из <series> <date>»` — точечный edit, удаляет N-ю
  bullet-задачу из секции `## Задачи` в `.md` (без LLM).
- `«поправь протокол <series> <date>: <инструкция>»` — структурная правка
  через `generate_protocol` с prompt-инъекцией инструкции.
- `«<series> <date>: задачу X не было»` — структурная правка.

Поток коррекции:

1. **Версия:** копия текущего `.md` → `_versions/<date>-protokol-vN.md`
   (atomic, N инкрементальный).
2. **Apply:** либо точечный, либо `generate_protocol`. Atomic write нового
   `.md`.
3. **In-group отправка:** если `(now − delivered.at) < 48ч` →
   `bot.delete_message` для каждого старого `message_id` → send новой
   версии (split). Если ≥48ч → отправляем новую с предупреждением «старая
   версия выше осталась — Telegram запрещает удалять старше 48ч».
4. **Summary «было/стало»:** Claude Haiku 4.5 (`call_claude_print(model=
   "claude-haiku-4-5-20251001")`) по `difflib.unified_diff` (5–10 строк
   русского текста, не дублирует протокол).
5. **History:** новая запись в `meta.delivered.history: [{at, chat_id,
   message_ids, reason: "correction"}]`. `meta.delivered.message_ids`
   замещаются новыми.

**Env-гейты:** `ENABLE_PROTOCOL_DELIVERY=1` (дефолт ON). Когда OFF —
`deliver_protocol` логирует `disabled` и возвращает `{status: "disabled"}`,
finalize не падает.

**Schema `meta.delivered` в meta.json:**

```json
{
  "chat_id": -1001234567890,
  "message_ids": [42, 43],
  "at": "2026-05-28T19:55:12Z",
  "decision": "skip" | "skip-timeout" | "partial-failure" | null,
  "history": [
    {"at": "2026-05-28T20:30:00Z", "chat_id": -1001234567890,
     "message_ids": [42, 43], "reason": "correction"}
  ]
}
```

**Structured лог:**

```
[delivery] sent meeting=<sid> chat_id=<id> parts=N message_ids=[...] elapsed=Xs
[delivery] idempotent skip meeting=<sid> chat_id=<id> parts=N
[delivery] disabled by ENABLE_PROTOCOL_DELIVERY=0
[delivery] asked meeting=<sid> reason=no_binding
[delivery] skipped meeting=<sid> reason=user-skip|user-skip-text|skip-timeout
[correction] applied meeting=<sid> kind=targeted_remove|structural version=N
[correction] in-group meeting=<sid> deleted_old=K sent_new=M summary_sent=true
```

В финальный stdout `finalize-meeting.py` добавлено поле `delivery:
{status, chat_id, message_ids, parts_count, at, elapsed_s}`.

**Разблокировка после `partial-failure`:** если сетка упала посреди многочастной
доставки и `meta.delivered.decision="partial-failure"` — повторная финализация
`idempotent skip` пропускает (чтобы не отправить дубль уже доставленных частей).
Чтобы переотправить с нуля:

```bash
# 1) Очистить meta.delivered (вручную, после удаления частично-доставленных
#    сообщений из группы — если они там нежелательны):
python3 -c '
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
m = json.loads(p.read_text(encoding="utf-8"))
m.pop("delivered", None)
p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
' /path/to/meta.json

# 2) Повторить finalize-meeting (или triggered correction command, или
#    Telegram-команда «протокол <series> <date>» в Ф4 — она перегенерирует
#    .md и потом следующий finalize пушнёт).
```

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
