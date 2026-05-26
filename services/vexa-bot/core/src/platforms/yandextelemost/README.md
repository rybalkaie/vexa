# Yandex Telemost adapter

Адаптер Vexa для Яндекс Телемост — реализует Фазу 2 проекта meeting-notary
([план](../../../../../../../me/plans/2026-05-26-bot-notarius-telemost.md)).

## Архитектура

Strategy-pattern как у остальных платформ Vexa. Поток:

1. `joinYandexTelemost` — открыть URL, пройти промежуточный экран
   «Продолжить в браузере», ввести имя, нажать «Подключиться».
2. `waitForYandexTelemostAdmission` — детект admitted / waiting / rejected;
   на waiting room ≥ 30s шлём push в Telegram, 2 мин без admit → abandon.
3. `prepareForRecording` — exposeFunction `logBot` / `getBotConfig` /
   `performLeaveAction`.
4. `startYandexTelemostRecording` — основной цикл (см. ниже).
5. `startYandexTelemostRemovalMonitor` — стерегёт removal-индикаторы.
6. `leaveYandexTelemost` — клик «Выйти», fallback на закрытие страницы.

## Захват аудио (Ф2 — без диарезации)

Browser-сторона:

- Собирает все `<audio>`/`<video>` элементы с активным `srcObject`.
- Сводит их в один `MediaStreamDestination` (combined stream).
- `ScriptProcessor` 16kHz mono режет на чанки ~3 секунды.
- Каждый чанк (Float32 → base64) шлёт в Node.js через
  `__vexaTelemostAudio(b64, rms)`.

Node.js-сторона:

- Декодирует Float32, считает RMS — тихие чанки не транскрибирует.
- Не-тихий чанк → WAV → POST в `TRANSCRIPTION_SERVICE_URL`
  (faster-whisper medium на VPS).
- Текст с таймкодом дописывает в `/transcripts/<date>-<sessionUid>.txt`
  (внутри контейнера; в compose-override это bind-mount наружу в
  `~/meeting-notary/_tmp/transcripts/` на VPS).
- Дублирует в stdout с пометкой `[telemost-transcript]`.

## Метрики конца встречи

- 60 секунд непрерывной тишины (`RMS < 0.003`) → завершение.
- URL ушёл с `telemost.yandex.ru` → завершение.
- Был хотя бы 1 participant-тайл, потом 0 в течение 30 секунд → завершение.
- Page закрылась — завершение.

## Selectors

Все DOM-селекторы вынесены в [`selectors.ts`](./selectors.ts). Lobby-селекторы
подтверждены живой разведкой Playwright 2026-05-26 и завязаны на стабильные
`data-testid` дизайн-системы Orb:

- `[data-testid="orb-textinput-input"]` — поле имени
- `[data-testid="turn-on-mic-button"]` / `turn-on-camera-button` — mic/cam
  (по дефолту ВЫКЛЮЧЕНЫ — бот их не кликает)
- `[data-testid="enter-conference-button"]` — «Подключиться»
- `[data-testid="to-home-screen-button"]` или текст «Продолжить в браузере» —
  интерстициал

In-meeting селекторы (leave / participants / waiting) — гипотезы из Orb
naming convention, помечены в коде «уточним на первом прогоне». Адаптер при
admitted делает один-раз DOM dump (`dom_dump` / `dom_batch=N` в логах),
чтобы можно было допилить селекторы постфактум.

## Telegram push

Через `telegram.ts` — прямой вызов Telegram Bot API из контейнера.
Требуются env-переменные:

- `TELEGRAM_BOT_TOKEN` — токен бота (Ф1 заявил `@Ilia_claude_1_bot`)
- `TELEGRAM_CHAT_ID` — chat_id Ильи

Если переменных нет — push молча пропускается (адаптер продолжает работу).

## Что НЕ делает на Ф2

- Не делает per-speaker diarization (это Ф3).
- Не делает маппинг имён участников (это Ф3).
- Не делает шаблон протокола `.md` (это Ф3).
- Не интегрируется с meeting-api / runtime-api dispatcher (это Ф5).
  Запускается напрямую через `BOT_CONFIG` env с `platform: "yandex_telemost"`.

## Запуск (Ф2 — ручной)

```bash
docker run --rm --platform linux/amd64 \
  --network vexa_dev_vexa_default \
  --env-file ~/meeting-notary/vexa/.env.notary \
  -e BOT_CONFIG='{
    "platform":"yandex_telemost",
    "meetingUrl":"https://telemost.yandex.ru/j/XXXXXXXX",
    "botName":"Бот — протокол встречи",
    "token":"dev",
    "connectionId":"test-1",
    "nativeMeetingId":"XXXXXXXX",
    "meeting_id":1,
    "language":"ru",
    "redisUrl":"redis://redis:6379/0",
    "automaticLeave":{
      "waitingRoomTimeout":150000,
      "noOneJoinedTimeout":300000,
      "everyoneLeftTimeout":30000
    }
  }' \
  -e TRANSCRIPTION_SERVICE_URL=http://172.17.0.1:8083/v1/audio/transcriptions \
  -v ~/meeting-notary/_tmp/transcripts:/transcripts \
  vexaai/vexa-bot:notarius-telemost
```
