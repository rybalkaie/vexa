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

Отдельный worker (`tools/run_clarify_worker.py`) long-poll'ит ответы Ильи,
парсит callback / текст, переразмечает транскрипт на диске **атомарно**.

### Архитектурное замечание про Telegram-бот

Worker использует **отдельный** Telegram-бот через `CLARIFY_BOT_TOKEN`,
**НЕ** существующий `@Ilia_claude_1_bot`. Причина: claude-telegram daemon
(`~/.claude/channels/telegram/` → плагин `claude-plugins-official/telegram`)
монопольно владеет `getUpdates` для своего токена, а Telegram отдаёт
updates только одному getUpdates-консюмеру одновременно. Плагин daemon'а
молча проглатывает callback_query чужих паттернов (server.ts:731-737 —
матчит только `^perm:`), поэтому переиспользовать его нельзя без правки
upstream-кода плагина.

Шаг владельца: создать **второго** бота через @BotFather, токен положить
в `.env.notary` как `CLARIFY_BOT_TOKEN`, добавить бота в личку Ильи
(или `/start` от Ильи). `TELEGRAM_CHAT_ID` остаётся прежним — это chat_id
самого Ильи (число), куда шлются уточнения.

### Запуск worker'а

```bash
# Ручной запуск (для теста):
cd /srv/meeting-notary  # на VPS, или ~/Projects/meeting-notary на маке
set -a; source .env.notary; set +a
python3 vexa/scripts/notary/tools/run_clarify_worker.py
```

systemd unit (на VPS, путь `/etc/systemd/system/meeting-notary-clarify.service`):

```ini
[Unit]
Description=meeting-notary clarify worker (long-poll Telegram callbacks)
After=network-online.target

[Service]
Type=simple
User=dev
WorkingDirectory=/srv/meeting-notary
EnvironmentFile=/srv/meeting-notary/.env.notary
# Создаём pending-dir под dev'ом ДО запуска worker'а — иначе clarify падает
# на silent OSError при первой записи state-файла (Ход 3 У4/У5/У8).
ExecStartPre=/bin/mkdir -p /opt/meeting-notary/_pending_clarification
ExecStart=/home/dev/meeting-notary/venv/bin/python /srv/meeting-notary/vexa/scripts/notary/tools/run_clarify_worker.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Установка:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now meeting-notary-clarify.service
journalctl --user-unit meeting-notary-clarify -f   # либо без --user, см. куда положил unit
```

### Env-флаги Ф3

| Env | Дефолт | Назначение |
|-----|--------|------------|
| `CLARIFY_BOT_TOKEN` | _нет_ | Токен ОТДЕЛЬНОГО бота (см. выше). Пусто → clarify не шлётся. |
| `ENABLE_LLM_CLARIFY` | `1` | `0`/`false`/`no` отключает clarify целиком. |
| `CLARIFY_THRESHOLD` | `0.7` | Порог confidence — ниже → cluster идёт на уточнение. |
| `CLARIFY_TIMEOUT` | `420` | Секунд до `timed_out`. Поздний ответ только обновляет файл. |
| `CLARIFY_LONG_POLL_TIMEOUT` | `25` | long-poll окно одного `getUpdates`. |
| `TELEGRAM_CHAT_ID` | _нет_ | chat_id Ильи (число). Шлётся туда. |
| `MEETING_NOTARY_PENDING_DIR` | авто | Где хранить state-файлы. |

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
