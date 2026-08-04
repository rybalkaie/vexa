# Карта зоны (этап 0.5, 7 разведчиков)

Полные отчёты разведчиков — в контексте прогона; здесь выжимка того, что меняет план работ.

## Что можно менять / что только читать / что не трогать

| Класс | Пути |
|---|---|
| **Меняю** | `scripts/notary/cli/registry.py`, `scripts/notary/lib/{morning_digest,series_memory,clarify_worker,send_artifact,gcal}.py`, `scripts/notary/{finalize-meeting.py,meetings_listener.py,meetings_apply_reply.py,meetings_evening_block.py,scheduler.py}`, `scripts/notary/tests/**` |
| **Читаю, не меняю** | `lib/llm_postprocess.py`, `lib/pending_reconciler.py`, `lib/pending_status_reply.py`, `lib/reply_ack.py`, `lib/ack_understand.py`, `runner.py`, `collector.py` |
| **Не трогаю** | всё вне `scripts/notary/`; боевой `watched.yaml`; `deploy/`, `systemd/`, прод |

## 🔴 Находки, меняющие исполнение плана

**Н1. `iCalUID` в коде нет вообще.** План Ф5 велит «сверять `iCalUID` события». Grep по `scripts/notary`: ноль попаданий. `lib/gcal.py:252-290` читает из события только `id/status/start/end/summary/conferenceData/hangoutLink/location/description/recurringEventId`; сырое событие ниже gcal-слоя не пробрасывается (`scheduler.py:309-345` выбрасывает всё лишнее). **Но** запросы к Google не сужают `fields` (`gcal.py:150-155`, `:185-192`, `:215-217`), то есть API отдаёт `iCalUID` в ресурсе. Вывод: требование выполнимо, но это не «сверить существующее поле», а «протянуть новое поле от gcal-слоя до записи реестра». Объём больше, чем читается из плана.

**Н2. У Ф4 и Ф5 нет ни одного сторожа.** Тестов на `meetings_evening_block.py` и `meetings_apply_reply.py` в проекте **нет ни одного** (проверено grep по всему дереву). Тестов, импортирующих `scheduler.py`, — тоже нет. Это ровно случай §4 ворот («нельзя сломать сторожа, которого нет»): мутации в этих фазах бессмысленны, пока сторожа не написаны. Значит в Ф4/Ф5 приёмочный тест пишется ПЕРВЫМ, иначе фаза не проверяема.

**Н3. Дайджест `commit_pending_shown` не зовёт вовсе** и `shown_sink`/`open_sink` не использует (`morning_digest.py` — прямые вызовы приватных `series_memory._resolve_open_tasks_raw` / `merge_open_tasks_with_status`). Риск 3 плана касается только протокольного пути. В `finalize-meeting.py:1773` `record_open_tasks_snapshot` уже под гейтом `if pending_open_positions:`, а `commit_pending_shown` (`finalize-meeting.py:1766`) вызывается при доставке всегда и при пустом списке всё равно ставит `mark_open_tasks_onboarded` (`series_memory.py:1966-1969`).

**Н4. Снапшот дайджеста пишется даже пустым.** `record_open_tasks_snapshot` (`morning_digest.py:630`) гейта на непустой список НЕ имеет; `positions: []` — валидная запись, и читатель различает `None` (живой хвост) и `[]` (корзина пуста) — `pending_status_reply.py:761-768`. Для `last-only` важно: если просто не звать `record`, снапшота не будет → reply-путь пойдёт по живому хвосту. Значит тишина статус-машины в `last-only` требует ЯВНОГО гейта в reply-пути, а не «само не запишется».

**Н5. Асимметрия резолва серии.** `get_telegram_chat_id_for_series` (`registry.py:363`) нормализует ключ (`normalize_series_key:346`, грабли ISS-7), а `get_company_for_series:447` / `get_visibility_for_series:466` / `get_genre_for_series:484` матчат точным `==`. Новый хелпер режима обязан идти по нормализованному варианту — план это и требует.

**Н6. Обратного резолва «чат → серия» нет.** Есть только `find_series_bindings` (`registry.py:389`, все пары `(series, chat_id)`), используется в одной диагностике. Инвариант «один чат → одна серия» не гарантирован ничем: `set_telegram_chat_id_for_series` спокойно привяжет один чат к разным сериям, и одна серия обычно = несколько записей с разными `id`. Значит фолбэк ISS-52 обязан схлопывать по УНИКАЛЬНЫМ сериям, иначе «несколько записей одной серии» ложно прочитается как «несколько серий».

**Н7. Гейт `detect_status_intent` в артефакт-роуте не покрыт ни одним тестом.** `meetings_listener.py:1026-1031` — единственная защита от кражи статус-ответов, и теста с текстом «глагол отправки + объект + статус-маркер» в базе нет. Существующий `test_status_reply_not_stolen` (`test_send_artifact.py:134`) проверяет другой путь — текст без глагола отправки, он отсекается раньше. Для Ф6 (переход на ИИ) это дыра ровно в том месте, где ИИ шире по определению.

## Опорные адреса

**Реестр:** `load_watched` `registry.py:207` · `save_watched` `:213` · `release_watched_lock` `:220` · `validate_watched_record` `:291` · `normalize_series_key` `:346` · `get_telegram_chat_id_for_series` `:363` · `find_series_bindings` `:389` · `resolve_series_ref` `:406`. Логгера в модуле нет вовсе. Образец «неизвестное значение → пропуск + warning» — `scheduler.py:129-130`.

**Роутинг:** диспетчер `process_message` `meetings_listener.py:2808`; порядок: `set_chat` `:2817` → `ack_reply` `:2824` → `send_artifact` `:2830` → `digest_reply` `:2838` → voice `:2845` → `reply_understand` `:2851` → `mixed_reply` `:2860` → `pending_status_reply` `:2868` → DM-гейт `:2877`. Образец команды — `maybe_route_to_set_chat_command` `:1043-1217`, owner-гейт `:1075-1083`, парсер `lib/correction_command.py:86-140`.

**Дайджест:** `run` `morning_digest.py:652` · `send_series_digest` `:556` · `collect_items` `:218` · `build_digest_markdown` `:281` · `build_caption` `:353` · `HEADER_INSTRUCTION` `:82` (дословные слова владельца, не переписывать) · `_TONE_LINE` `:93` · `record_sent` `:393` · снапшот `:630`. Доставка — PDF-документ с caption.

**Протокол:** `build_open_tasks_block` `series_memory.py:1813`, ровно два call-site: `finalize-meeting.py:852` и `lib/clarify_worker.py:238`. Заголовок раздела — `PENDING_SECTION_HEADING` `series_memory.py:1471`. Раздел печатает МОДЕЛЬ по инструкции `_OPEN_TASKS_BLOCK_HEADER` `:1714`. Обратный парсинг в выжимку — `extract_open_tasks` `:585`, зовётся из `build_digest` `:866`. Сторожа паритета call-site: `test_phase8_open_tasks_tracking.py:451` и `test_iss34_phase4_display.py:421`.

**Происхождение задач:** `origin_dates_for_tasks` `series_memory.py:1671` → `{_status_key: "YYYY-MM-DD"}`, самая ранняя выжимка, где ключ встретился. `origin_prefix_for` `:1696`.

**Вечерний блок:** `index_watched` `meetings_evening_block.py:119` (ключ `(calendar_id, recurring|event_id)`, `enabled`/`ignored` НЕ смотрит) · отбор `:311-339` · `fmt_when` `:175` (читает `DISPLAY_TZ` `:82`). `apply_decisions` `meetings_apply_reply.py:218`, reject `:248-251` (следа на диске не оставляет), условие сохранения `:320` — дословно `if applied > 0 or ignored > 0:`. `fmt_when_short` `:202-215` — жёстко `ZoneInfo("Asia/Dubai")` `:212`. `MEETING_NOTARY_DISPLAY_TZ` читается ровно в одном месте — `meetings_evening_block.py:82`.

**Планировщик:** очередь `.queue.json`, элемент — `_queue_item` `scheduler.py:309-345` (`calendar_id` в элемент не кладётся). Дедупа нет вообще; единственное место, где виден весь список, — `scheduler.py:140-142`. Барьеры ниже по течению не спасают: `runner._candidates` `:220` ключует `event_id`, `_concurrency_decision` `:302-306` — `series`, `state_acquire` — `event_id`; у двух записей всё это различается. `make_record_id` `meetings_apply_reply.py:90` — `series-<sha1(recurring_event_id)[:8]>` / `oneoff-<sha1(event_id)[:8]>`, `calendar_id` в ключ не входит.

**Детект артефакта:** `send_artifact.py` — чистый детектор, 73 строки, `detect_artifact_request` `:49-72`, два класса `protocol`/`digest`. Исполнение — `_resend_protocol` `meetings_listener.py:914` / `_resend_digest` `:958`. Образец ИИ-первого разбора — `ack_understand.py`, гейт `ENABLE_ACK_ANSWER_AI` дефолт-OFF `:42-49`, форма чтения env: `raw = (os.environ.get("<FLAG>") or "").strip().lower(); return raw in ("1","true","yes","on")`. Вызов модели — `claude_cli.call_claude_print` `claude_cli.py:41`, модель `mixed_reply_split._SPLIT_MODEL` (`claude-haiku-4-5-20251001`), таймаут `_split_timeout()` (120 с). Парсер `parse_answer_json` `ack_understand.py:186`, верификация диапазона — `reply_ack.py:552-555`. Двойной гейт `reply_ack.py:529-530`.
