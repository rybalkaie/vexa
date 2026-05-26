# CLI бота-нотариуса

Два инструмента: `meeting-rooms` (справочник переговорок) и `meeting-watch` (реестр отслеживаемых встреч).

## Установка (мак)

Зависимости в отдельном venv проекта:

```bash
python3 -m venv ~/Projects/meeting-notary/.venv-cli
~/Projects/meeting-notary/.venv-cli/bin/pip install -r ~/Projects/meeting-notary/vexa/scripts/notary/cli/requirements.txt
```

Симлинки в `~/.local/bin/` (там же лежат остальные локальные обёртки):

```bash
ln -sf ~/Projects/meeting-notary/vexa/scripts/notary/cli/meeting_rooms.py ~/.local/bin/meeting-rooms
ln -sf ~/Projects/meeting-notary/vexa/scripts/notary/cli/meeting_watch.py ~/.local/bin/meeting-watch
chmod +x ~/Projects/meeting-notary/vexa/scripts/notary/cli/meeting_rooms.py
chmod +x ~/Projects/meeting-notary/vexa/scripts/notary/cli/meeting_watch.py
```

Shebang в скриптах — `/usr/bin/env python3`. Сам venv подключается через PYTHONPATH автоматически через `sys.path.insert` в начале скрипта. Реальная команда запуска:

```bash
~/Projects/meeting-notary/.venv-cli/bin/python ~/Projects/meeting-notary/vexa/scripts/notary/cli/meeting_rooms.py list
```

Симлинк `~/.local/bin/meeting-rooms` указывает прямо на `.py` — чтобы он работал через venv-python, замени shebang на абсолютный путь к venv (см. ниже install-скрипт).

## Реестры

- `~/Projects/me/встречи/rooms.yaml` — справочник постоянных Telemost-комнат.
- `~/Projects/me/встречи/watched.yaml` — реестр отслеживаемых встреч.
- `~/Projects/me/встречи/.pause-until` — флаг паузы (создаётся `meeting-watch pause`, удаляется `resume`).

## Команды

### meeting-rooms

```bash
meeting-rooms list
meeting-rooms add --name t99 --url https://telemost.yandex.ru/j/... --description "..."
meeting-rooms add                     # интерактивный диалог
meeting-rooms remove t99 --yes
```

### meeting-watch

```bash
meeting-watch list                    # активные (enabled, не ignored)
meeting-watch list --all              # включая disabled/ignored

meeting-watch add --type google-calendar \
    --id my-series --series my-series \
    --calendar-id ilya.rybalka@anzhee.ru \
    --recurring-event-id <id> \
    --room @t11 \
    --participants "Илья Рыбалка" --participants "Татьяна"

meeting-watch add                     # интерактивный диалог
meeting-watch remove <id> --yes
meeting-watch disable <id>
meeting-watch enable <id>

meeting-watch pause 7d                # 7d / 4h / 90m
meeting-watch resume

meeting-watch run <id>                # печатает команду ручного запуска бота (Ф5 ещё не написан)
meeting-watch run <id> --execute      # сразу выполнить через ssh meeting-notary
```

## Где искать recurring_event_id

Через Google Calendar MCP в Claude Code:

```
mcp__google-calendar__search-events {
  "calendarId": ["ilya.rybalka@anzhee.ru", "c_30pdgkfh5stb08s4pkkvqmpe3o@group.calendar.google.com"],
  "query": "<поисковая строка из названия серии>",
  "timeMin": "<ISO>",
  "timeMax": "<ISO>",
  "fields": ["recurringEventId"]
}
```

В ответе у нужного instance `recurringEventId` — это id мастер-события серии.
