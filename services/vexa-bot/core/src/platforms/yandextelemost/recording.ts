// Yandex Telemost: запись и транскрипция.
//
// Архитектура Ф2 (stream-режим, draft-транскрипт):
//   1) Browser-сторона:
//        - Найти все <audio>/<video> элементы на странице.
//        - Свести их MediaStream в один combinedStream (без per-speaker).
//        - ScriptProcessor 16kHz mono → Float32 chunks ~3s длиной.
//        - Передавать чанки в Node.js через exposed function __vexaTelemostAudio.
//   2) Node.js-сторона:
//        - Каждый чанк → WAV → POST в TRANSCRIPTION_SERVICE_URL → текст
//          (draft, для real-time мониторинга).
//        - Текст с таймкодом писать в /transcripts/<sessionUid>.txt.
//
// Архитектура Ф3 (добавлено — для финального протокола):
//   3) Параллельно стриму копим ВСЕ Float32-сэмплы в один большой буфер
//      и в конце встречи пишем непрерывный WAV /transcripts/<sessionUid>.wav.
//      Этот WAV идёт в post-processing (whisper полный + pyannote диарезация
//      + name mapping + markdown protocol) — см. scripts/finalize-meeting.py.
//   4) Параллельно polling списка участников Telemost (participants.ts).
//   5) В конце встречи пишем meta.json с (sessionUid, startTs, endTs,
//      participants[], meetingUrl, botName, duration_s, wav_path, txt_path).

import { Page, ConsoleMessage, Frame, WebSocket as PWWebSocket } from "playwright";
import { BotConfig } from "../../types";
import { log } from "../../utils";
import { isHallucination } from "../../services/hallucination-filter";
import { startParticipantsPolling } from "./participants";
// Ф2 (ISS-28): перезаход после выброса переиспользует штатный join-флоу
// (joinYandexTelemost + waitForYandexTelemostAdmission) и канал уведомлений
// (sendTelegramMessage). recording.ts раньше их не импортировал — join
// оркестрировался выше по стеку (index.ts → meetingFlow). Для авто-перезахода
// «изнутри» ветки evicted зовём их здесь напрямую.
import { joinYandexTelemost } from "./join";
import { waitForYandexTelemostAdmission } from "./admission";
import { addJoinEvent } from "./join-status";
import { installFrameWalker } from "./rtc-hook";
import { ensureTelemostDevicesOff } from "./devices";
import * as fs from "fs";
import * as path from "path";

const LOG_PREFIX = "[adapter-telemost]";
const TRANSCRIPT_DIR = process.env.TELEMOST_TRANSCRIPT_DIR || "/transcripts";
// Ф5 (2026-05-29): chunk-декаплинг записи и присутствия в звонке (Вариант Б
// владельца). Вместо единого silence-таймаута выхода — две границы тишины:
//   • CHUNK_SILENCE_PAUSE_MS — тишина дольше этого ПРИОСТАНАВЛИВАЕТ запись:
//     закрываем текущий chunk_N.wav, но бот ОСТАЁТСЯ в звонке. Новая речь
//     открывает chunk_(N+1).wav. Режет хвосты тишины в WAV и (с Ф5.3 multi-chunk
//     finalize) позволяет финализировать части по ходу встречи.
//   • OUT_OF_CALL_SILENCE_MS — суммарная тишина дольше этого = бот выходит из
//     звонка целиком (как раньше делал единый silence_20min).
// Семантика выхода из паузы: пауза наступает на CHUNK_SILENCE_PAUSE_MS (5 мин)
// тишины; из паузы бот выходит, когда суммарная тишина достигает
// OUT_OF_CALL_SILENCE_MS (20 мин) — т.е. ещё 15 мин после закрытия chunk'а.
// Раньше (до Ф5) был единый SILENCE_END_AFTER_MS = 20 мин (а ещё раньше 60s —
// убивало бота на тихих минутах, см. sales-quality 2026-05-27). Пороги
// зафиксированы владельцем 2026-05-29.
const CHUNK_SILENCE_PAUSE_MS = 5 * 60_000; // 5 мин — приостановка записи (chunk close)
const OUT_OF_CALL_SILENCE_MS = 20 * 60_000; // 20 мин — выход из звонка
const URL_CHECK_INTERVAL_MS = 3_000;
const SAMPLE_RATE = 16000;

// ── ISS-28 / Ф1: детект выброса из комнаты + инструментация причины ──
// Корень ISS-28: Телемост уводит страницу гостя с `/j/<meetingId>` на голый
// `telemost.yandex.ru/` (выброс в лобби), аудио-дорожки умирают вместе со
// страницей, а старый детект выхода смотрел на ДОМЕН (корень его всё ещё
// содержит) → бот не замечал выброс, 20 мин слушал тишину. Точный сигнал =
// «адрес потерял `/j/<meetingId>`». См. plans/notary-issues-log.md ISS-28.
//
// R3-инструментация (БЕЛЫЙ СПИСОК — RISK5/«опасная тройка»): в момент выброса
// фиксируем причину со стороны Яндекса — ТОЛЬКО технический сигналинг, НЕ медиа
// и НЕ текст реплик/имён. Захватываем три источника:
//   • навигации главного фрейма (куда ушла страница) — причина навигации;
//   • console-сообщения ТОЛЬКО типов error/warning (НЕ console.log/info/debug —
//     там приложение может дампить состояние с ПДн);
//   • WebSocket close-код (Playwright его не отдаёт → лёгкая обёртка
//     window.WebSocket на browser-стороне проталкивает {code,reason} в Node).
// Кольцевые буферы ограничены — память не растёт; на выбросе снимок едет в
// docker-лог (его персистит R6) + meta.recording.eviction.
const EVICTION_NAV_MAX = 20;
const EVICTION_CONSOLE_MAX = 40;
const EVICTION_WS_MAX = 40;
const EVICTION_TEXT_TRUNC = 300;

// `/j/<meetingId>` — сегмент «в комнате». Любой адрес без него = вне комнаты.
const TELEMOST_ROOM_RE = /\/j\/([A-Za-z0-9_-]+)/;

// ── ISS-28 / Ф2: подтверждение выброса, перезаход, guard «штатный конец» ──
// R2b — подтверждение: реагируем только после 2 чтений подряд без `/j/` (1 цикл
// URL_CHECK_INTERVAL_MS ≈ 3с), чтобы под-секундный флик SPA-роутера не считался
// выбросом. Эскалация к алертам — исчерпав 5 попыток за ~2 мин с backoff'ом.
const EVICTION_CONFIRM_READS = 2;
const EVICTION_MAX_ATTEMPTS = 5;
// Защита от цикла «вернулись → Яндекс снова выбросил → вернулись …» (риск плана).
// Per-event лимит в 5 попыток сам по себе его НЕ ловит (каждый новый выброс даёт
// свежий бюджет). Потому ограничиваем и число УСПЕШНЫХ восстановлений за сессию:
// после стольких возвратов очередной выброс сразу эскалируем (алерт), не цикля.
const EVICTION_MAX_RECOVERY_CYCLES = 3;
// Пороги backoff заданы владельцем (Q3, /план-фичи-2): 0/10/20/40/80с (=150с
// «голого» ожидания). Реальный wall-clock больше на время самих join-попыток.
const EVICTION_BACKOFF_MS = [0, 10_000, 20_000, 40_000, 80_000];
// Таймаут admission на КАЖДУЮ попытку перезахода. Живая комната впускает быстро;
// если комната мертва (встреча кончилась) — join бросит раньше (нет lobby).
const REJOIN_ADMISSION_TIMEOUT_MS = 60_000;

// R13 (guard «штатный конец ≠ сбой»), порог A7 — выбран исполнителем (план явно
// делегирует, A7 в реестре допущений). «Существенный объём речи» = выброс при
// уже записанном большом разговоре → встреча фактически прошла (норма), НЕ сбой.
// Якоря плана: ранний кейс ISS-28 (≈1 мин речи) = однозначно сбой; конец
// директората (≈52 мин) = однозначно норма. Берём середину: ≥25 мин РЕЧИ
// (сумма speech-span по chunks, без межчанковых пауз). Ниже → кандидат в сбой
// (перезаход; если не вернулись — алерт). Ожидаемой длительности встречи у бота
// НЕТ (runner её не прокидывает в BOT_CONFIG) — потому порог по объёму речи, а
// не по % длительности. Если порог не тот — поправим на наблюдении 7 июля.
const EVICTION_NORMAL_END_MIN_SPEECH_MS = 25 * 60_000;

// R5 (deaf-after-start): «оглох ПОСЛЕ начала» при УДЕРЖАНИИ `/j/`. Сигнал =
// browser-флаг __vexa_telemost_degraded (0 живых аудио-дорожек после rescan).
// Срабатывает только если деградация удержана этот срок ПРИ видимых реальных
// участниках — отсекает кратковременные renegotiation-окна Телемоста (он рвёт и
// заново поднимает дорожки) и конец встречи (люди ушли → панель пуста). 5 мин <
// OUT_OF_CALL_SILENCE_MS (20 мин) → классифицируем deaf_after_start РАНЬШЕ, чем
// «молчаливый» silence_out_of_call, как требует критерий R5.
const DEAF_AFTER_START_TIMEOUT_MS = 5 * 60_000;

type EvictionSnapshot = {
  navigations: { url: string; ts: string }[];
  consoleErrors: { type: string; text: string; ts: string }[];
  websocketEvents: { url: string; code?: number; reason?: string; closed?: boolean; ts: string }[];
};

type EvictionCapture = {
  getSnapshot: () => EvictionSnapshot;
  detach: () => Promise<void>;
};

// Усечь до одной строки и max символов — защита от раздувания лога/мета и от
// многострочных дампов в новом лог-канале.
function truncOneLine(s: string, max: number): string {
  return String(s || "").replace(/\s+/g, " ").trim().slice(0, max);
}

// Экранирование под parse_mode:"HTML" (telegram.ts). Динамические значения
// (имя серии, URL комнаты) попадают в HTML-сообщения алертов; неэкранированный
// `&`/`<`/`>` ломает парсинг → Telegram 400 «can't parse entities» и алерт о
// СБОЕ записи не доставляется. Bot API требует экранировать ровно эти три.
function escapeHtml(s: string): string {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/**
 * Browser-обёртка window.WebSocket: единственный способ достать close-КОД
 * (Playwright его не экспонирует). Идемпотентна по `__vexa_ws_wrapped` — второй
 * вызов на ТОМ ЖЕ документе ничего не делает. Вынесена из setupEvictionCapture
 * отдельно, потому что Ф2-перезаход (page.goto) уничтожает window вместе с
 * обёрткой → её надо переустановить на НОВОМ документе. Node-канал
 * `__vexaTelemostSignal` (exposeFunction) переживает навигацию — его НЕ трогаем.
 */
async function installEvictionWsWrapper(page: Page): Promise<void> {
  try {
    await page.evaluate(() => {
      const w = window as any;
      if (w.__vexa_ws_wrapped) return;
      const Orig = w.WebSocket;
      if (typeof Orig !== "function") return;
      const Wrapped: any = function (this: any, ...args: any[]) {
        const ws = new Orig(...args);
        try {
          ws.addEventListener("close", (e: any) => {
            try {
              w.__vexaTelemostSignal?.({
                kind: "ws_close",
                url: String(args[0] || ""),
                code: e?.code,
                reason: String(e?.reason || ""),
              });
            } catch { /* контекст мог умереть на выбросе */ }
          });
        } catch { /* */ }
        return ws;
      };
      try {
        Wrapped.prototype = Orig.prototype;
        Wrapped.CONNECTING = Orig.CONNECTING;
        Wrapped.OPEN = Orig.OPEN;
        Wrapped.CLOSING = Orig.CLOSING;
        Wrapped.CLOSED = Orig.CLOSED;
      } catch { /* */ }
      w.WebSocket = Wrapped;
      w.__vexa_ws_wrapped = true;
    });
  } catch (err: any) {
    log(`${LOG_PREFIX} eviction WS wrap failed (non-fatal): ${err?.message}`);
  }
}

/**
 * R3 (ISS-28): инструментация причины выброса. Вешает Node-слушатели на
 * console/navigation/websocket страницы + browser-обёртку window.WebSocket
 * (close-код Playwright не отдаёт). Все буферы — кольцевые и по БЕЛОМУ СПИСКУ
 * типов; текст реплик/имён сюда НЕ попадает (RISK5). Возвращает снимок-геттер и
 * detach. Идемпотентна к контексту: на выбросе НЕ зовём page.evaluate (контекст
 * уже мог умереть от навигации) — данные уже в Node-буферах.
 */
async function setupEvictionCapture(page: Page): Promise<EvictionCapture> {
  const navigations: EvictionSnapshot["navigations"] = [];
  const consoleErrors: EvictionSnapshot["consoleErrors"] = [];
  const websocketEvents: EvictionSnapshot["websocketEvents"] = [];
  const pushCapped = <T>(arr: T[], item: T, cap: number): void => {
    arr.push(item);
    if (arr.length > cap) arr.shift();
  };

  const onConsole = (msg: ConsoleMessage): void => {
    // БЕЛЫЙ СПИСОК: только error/warning. console.log/info/debug НЕ берём.
    const type = msg.type();
    if (type !== "error" && type !== "warning") return;
    pushCapped(
      consoleErrors,
      { type, text: truncOneLine(msg.text(), EVICTION_TEXT_TRUNC), ts: new Date().toISOString() },
      EVICTION_CONSOLE_MAX,
    );
  };
  const onFrameNav = (frame: Frame): void => {
    if (frame !== page.mainFrame()) return; // только главный фрейм = реальный уход страницы
    pushCapped(navigations, { url: frame.url(), ts: new Date().toISOString() }, EVICTION_NAV_MAX);
  };
  const onWebSocket = (ws: PWWebSocket): void => {
    // Playwright-уровень: close-код недоступен, фиксируем факт+время+url.
    // Близкий код (где доступен) приходит отдельно из browser-обёртки ниже.
    let url = "(unknown)";
    try { url = ws.url(); } catch { /* */ }
    const u = truncOneLine(url, EVICTION_TEXT_TRUNC);
    ws.on("close", () =>
      pushCapped(websocketEvents, { url: u, closed: true, ts: new Date().toISOString() }, EVICTION_WS_MAX));
    ws.on("socketerror", () =>
      pushCapped(websocketEvents, { url: u, closed: false, ts: new Date().toISOString() }, EVICTION_WS_MAX));
  };

  // Browser→Node канал для WS close-кода. exposeFunction — ОДИН раз на сессию
  // (Ф2-нота: при перезаходе НЕ переэкспонировать — второй expose БРОСАЕТ, как и
  // __vexaTelemostAudio; при возврате только re-run browser-обёртки).
  await page.exposeFunction("__vexaTelemostSignal", (payload: any): void => {
    try {
      if (!payload || payload.kind !== "ws_close") return;
      pushCapped(
        websocketEvents,
        {
          url: truncOneLine(String(payload.url || ""), EVICTION_TEXT_TRUNC),
          code: typeof payload.code === "number" ? payload.code : undefined,
          reason: truncOneLine(String(payload.reason || ""), 120),
          closed: true,
          ts: new Date().toISOString(),
        },
        EVICTION_WS_MAX,
      );
    } catch { /* */ }
  });

  page.on("console", onConsole);
  page.on("framenavigated", onFrameNav);
  page.on("websocket", onWebSocket);

  // Browser-обёртка window.WebSocket (close-КОД). Вынесена в отдельную функцию,
  // т.к. Ф2-перезаход переустанавливает её на новом документе (Node-listeners
  // выше и exposeFunction переживают навигацию, а browser-обёртка — нет).
  // Ловит сокеты, открытые ПОСЛЕ старта записи — их и переустанавливает Телемост
  // при renegotiation (ведущая гипотеза ISS-28). Нестандартное — см. пакет §7.
  await installEvictionWsWrapper(page);

  return {
    getSnapshot: () => ({
      navigations: [...navigations],
      consoleErrors: [...consoleErrors],
      websocketEvents: [...websocketEvents],
    }),
    detach: async () => {
      try { page.off("console", onConsole); } catch { /* */ }
      try { page.off("framenavigated", onFrameNav); } catch { /* */ }
      try { page.off("websocket", onWebSocket); } catch { /* */ }
    },
  };
}

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

function ensureTranscriptDir(): void {
  try {
    if (!fs.existsSync(TRANSCRIPT_DIR)) {
      fs.mkdirSync(TRANSCRIPT_DIR, { recursive: true });
    }
  } catch (err: any) {
    log(`${LOG_PREFIX} transcript dir ensure failed: ${err.message}`);
  }
}

function dateStamp(): string {
  return new Date().toISOString().split("T")[0];
}

function transcriptTxtPath(sessionUid: string): string {
  return path.join(TRANSCRIPT_DIR, `${dateStamp()}-${sessionUid}.txt`);
}

// Ф5: путь к WAV отдельного chunk'а. `stamp` фиксируется один раз на встречу
// (иначе chunk'и встречи через полночь получат разные date-префиксы и
// финализатор не сгруппирует их). idx — 1-based.
function chunkWavPath(stamp: string, sessionUid: string, idx: number): string {
  return path.join(TRANSCRIPT_DIR, `${stamp}-${sessionUid}.chunk${idx}.wav`);
}

function metaJsonPath(sessionUid: string): string {
  return path.join(TRANSCRIPT_DIR, `${dateStamp()}-${sessionUid}.meta.json`);
}

function appendTranscript(sessionUid: string, line: string): void {
  try {
    const p = transcriptTxtPath(sessionUid);
    const existed = fs.existsSync(p);
    fs.appendFileSync(p, line + "\n", "utf8");
    // ВАЖНО: bot бежит как root в Docker, host post-processing — как dev.
    // Чтобы dev мог удалять/перезаписывать draft.txt — даём 0666 при создании.
    if (!existed) {
      try { fs.chmodSync(p, 0o666); } catch {}
    }
  } catch (err: any) {
    log(`${LOG_PREFIX} transcript append failed: ${err.message}`);
  }
}

// WAV encode для Float32 buffer (16kHz, mono).
function float32ToWavBuffer(samples: Float32Array, sampleRate = 16000): Buffer {
  const numChannels = 1;
  const bytesPerSample = 2; // 16-bit
  const dataLength = samples.length * bytesPerSample;
  const buf = Buffer.alloc(44 + dataLength);
  // RIFF chunk
  buf.write("RIFF", 0);
  buf.writeUInt32LE(36 + dataLength, 4);
  buf.write("WAVE", 8);
  // fmt subchunk
  buf.write("fmt ", 12);
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20); // PCM
  buf.writeUInt16LE(numChannels, 22);
  buf.writeUInt32LE(sampleRate, 24);
  buf.writeUInt32LE(sampleRate * numChannels * bytesPerSample, 28);
  buf.writeUInt16LE(numChannels * bytesPerSample, 32);
  buf.writeUInt16LE(8 * bytesPerSample, 34);
  // data subchunk
  buf.write("data", 36);
  buf.writeUInt32LE(dataLength, 40);
  let off = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    buf.writeInt16LE(s < 0 ? s * 0x8000 : s * 0x7fff, off);
    off += 2;
  }
  return buf;
}

// Стримовая запись WAV: открываем файл, пишем заголовок placeholder,
// потом по мере прихода чанков дописываем 16-bit PCM сэмплы, в финале
// возвращаемся в заголовок и обновляем длины.
class WavStreamWriter {
  private fd: number | null = null;
  private samplesWritten = 0;

  constructor(private readonly filePath: string, private readonly sampleRate = SAMPLE_RATE) {}

  open(): void {
    this.fd = fs.openSync(this.filePath, "w");
    // ВАЖНО: bot бежит как root в Docker, host post-processing — как dev.
    // Чтобы dev мог удалять/перезаписывать WAV — даём 0666.
    try { fs.chmodSync(this.filePath, 0o666); } catch {}
    // Записываем заголовок с placeholder-длинами (заполним при close).
    // ВАЖНО: fs.writeSync БЕЗ position — иначе file cursor остаётся 0 и
    // следующий write samples перезаписывает header (Node docs:
    // "If position is an integer, the file position will remain unchanged").
    const header = float32ToWavBuffer(new Float32Array(0), this.sampleRate).subarray(0, 44);
    fs.writeSync(this.fd, header, 0, 44);
  }

  // Обновить RIFF size (offset 4) и data size (offset 40) по текущему
  // samplesWritten. position-аргумент у writeSync → pwrite(2): append-курсор
  // НЕ сдвигается (Node/Linux), поэтому безопасно звать и по ходу записи, и при
  // close(). На этом и держался корректный заголовок при штатном close().
  private writeHeaderSizes(): void {
    if (this.fd === null) return;
    const dataLength = this.samplesWritten * 2;
    const riffSizeBuf = Buffer.alloc(4);
    riffSizeBuf.writeUInt32LE(36 + dataLength, 0);
    fs.writeSync(this.fd, riffSizeBuf, 0, 4, 4);
    const dataSizeBuf = Buffer.alloc(4);
    dataSizeBuf.writeUInt32LE(dataLength, 0);
    fs.writeSync(this.fd, dataSizeBuf, 0, 4, 40);
  }

  writeSamples(samples: Float32Array): void {
    if (this.fd === null) return;
    const bytesPerSample = 2;
    const buf = Buffer.alloc(samples.length * bytesPerSample);
    let off = 0;
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      buf.writeInt16LE(s < 0 ? s * 0x8000 : s * 0x7fff, off);
      off += 2;
    }
    fs.writeSync(this.fd, buf, 0, buf.length);
    this.samplesWritten += samples.length;
    // P0-фикс (Ф2, 2026-06-03): держим WAV-заголовок актуальным на диске ПОСЛЕ
    // каждого ~3с-чанка. Корень потери WAV 29.05–03.06: PCM стримился на диск
    // (bind-mount), но data_size в шапке финализировался ТОЛЬКО в close() из
    // finally-teardown'а. node бежал ребёнком bash (PID 1, см. entrypoint.sh),
    // поэтому docker stop / рестарт / OOM убивали его SIGKILL'ом → finally не
    // отрабатывал (с 25.09.2026 entrypoint.sh запускает node через exec и
    // docker stop доходит до node; OOM и kill -9 по-прежнему мгновенные)
    // → шапка оставалась placeholder'ом data_size=0, и ридер видит
    // «0 байт аудио» при десятках МБ реального PCM (06-01 .recover-bak: 86 МБ,
    // data_size=0). Теперь на диске ВСЕГДА валидный WAV со всей речью минус
    // последний <3с-кадр. Цена — 2 pwrite по 4 байта на чанк (мизер).
    this.writeHeaderSizes();
  }

  close(): { samples: number; durationS: number } {
    if (this.fd === null) return { samples: 0, durationS: 0 };
    this.writeHeaderSizes();
    // fsync перед close: вытолкнуть данные+заголовок из page cache на диск
    // (durability на случай reboot/краша/OOM хоста). Для видимости host'у через
    // bind-mount fsync не нужен — но защищает от потери при жёстком сбое.
    try { fs.fsyncSync(this.fd); } catch (err: any) {
      log(`${LOG_PREFIX} wav fsync failed (non-fatal): ${err?.message}`);
    }
    fs.closeSync(this.fd);
    this.fd = null;
    return { samples: this.samplesWritten, durationS: this.samplesWritten / this.sampleRate };
  }

  isOpen(): boolean {
    return this.fd !== null;
  }

  get path(): string {
    return this.filePath;
  }
}

async function transcribeChunk(
  wav: Buffer,
  language: string,
  serviceUrl: string,
  serviceToken?: string
): Promise<string | null> {
  try {
    const form = new FormData();
    const wavBlob = new Blob([new Uint8Array(wav)], { type: "audio/wav" });
    form.append("file", wavBlob, "chunk.wav");
    form.append("model", "Systran/faster-whisper-medium");
    if (language) form.append("language", language);
    form.append("response_format", "json");

    const headers: Record<string, string> = {};
    if (serviceToken) headers["Authorization"] = `Bearer ${serviceToken}`;

    const res = await fetch(serviceUrl, { method: "POST", body: form as any, headers });
    if (!res.ok) {
      log(`${LOG_PREFIX} transcription HTTP ${res.status}`);
      return null;
    }
    const json: any = await res.json();
    const text = (json && (json.text || json.transcription)) || "";
    return text.trim() || null;
  } catch (err: any) {
    log(`${LOG_PREFIX} transcribe error: ${err.message}`);
    return null;
  }
}

/**
 * Установить browser-side capture: combined media stream → exposed function calls.
 * Возвращает stopper.
 */
async function setupBrowserCapture(page: Page): Promise<() => Promise<void>> {
  // Обходчик вложенных окон (rtc-hook.ts) — в текущий документ; при перезаходе
  // (page.goto) документ новый, поэтому ставится при каждом старте захвата.
  await page.evaluate(installFrameWalker).catch((e: any) => log(`${LOG_PREFIX} frame walker install failed (non-fatal): ${e?.message}`));
  await page.evaluate(() => {
    const win = window as any;

    async function start() {
      // Ф2 (RISK2/ISS-28): идемпотентность на ОДНОМ документе. При перезаходе
      // через page.goto документ новый — `win` свежий, флаг сброшен → поднимаем
      // граф заново. Но при «само-возврате» Яндекса (SPA-роутинг pushState, тот
      // же документ) исходный граф захвата ещё ЖИВ (его 2-сек rescan сам
      // подхватит вернувшиеся дорожки) — второй setupBrowserCapture поднял бы
      // ВТОРОЙ AudioContext/ScriptProcessor → каждый кадр пришёл бы дважды
      // (двойной writeSamples → битый WAV, поехавшая диаризация) и перетёр бы
      // ссылку на старый rescan-таймер (утечка). Потому здесь — ранний выход.
      if (win.__vexa_telemost_capture_running) {
        win.logBot?.("[telemost-audio] capture уже идёт на этом документе — re-init пропущен");
        return;
      }
      win.__vexa_telemost_capture_running = true;
      const TARGET_RATE = 16000;
      const CHUNK_DURATION_MS = 3000;

      win.logBot?.("[telemost-audio] discovering media elements…");

      const AudioCtxCls = win.AudioContext || win.webkitAudioContext;
      const audioCtx = new AudioCtxCls({ sampleRate: TARGET_RATE });
      const dest = audioCtx.createMediaStreamDestination();

      // ISS-20 (инцидент 2026-06-23): захват — НЕ разовый снимок при входе, а
      // ЖИВОЕ слежение. Телемост периодически обрывает аудио-дорожки
      // (Track ENDED) и поднимает новые при переустановке соединения; разовый
      // снимок остаётся припаянным к мёртвым дорожкам → вечная тишина, даже
      // когда люди говорят. Поэтому пере-сканируем DOM всю встречу и подмешиваем
      // КАЖДУЮ новую аудио-дорожку в общий sink (dest). wired по track.id —
      // защита от двойного подключения одной дорожки.
      const wired = new Set<string>();
      const rescan = (): number => {
        let added = 0;
        // Телемост 3.0 (23.09.2026): звонок живёт во вложенном iframe того же
        // origin (/private-join/) — смотрим и его документ. Плюс входящие дорожки,
        // пойманные хуком RTCPeerConnection из join.ts (если звук играет не через
        // <audio>). Дубли отсекает wired по track.id.
        // 25.09.2026 (фаза 3а): окна обходятся ВГЛУБЬ (раньше — один уровень),
        // а завершённые дорожки из списков хука выкидываются перед подмешиванием.
        const docs: Document[] = [];
        const wins: any[] = [];
        const walkFrames = win.__vexaSameOriginFrameWindows;
        if (typeof walkFrames === "function") {
          for (const w of walkFrames(win, 6)) {
            try {
              if (w && w.document) { docs.push(w.document); wins.push(w); }
            } catch { /* чужой origin — не наш звонок */ }
          }
        }
        if (docs.length === 0) {
          docs.push(document); wins.push(win);
          for (const fr of Array.from(document.querySelectorAll("iframe"))) {
            try {
              const d = (fr as HTMLIFrameElement).contentDocument;
              if (d) { docs.push(d); wins.push((fr as HTMLIFrameElement).contentWindow); }
            } catch { /* чужой origin — не наш звонок */ }
          }
        }
        for (const w of wins) {
          try { if (w && typeof w.__vexaPruneRemoteAudioTracks === "function") w.__vexaPruneRemoteAudioTracks(); } catch { /* */ }
        }
        const candidates: MediaStreamTrack[] = [];
        for (const d of docs) {
          for (const el of Array.from(d.querySelectorAll("audio, video")) as HTMLMediaElement[]) {
            let ms: MediaStream | null = null;
            try { ms = (el as any).srcObject as MediaStream | null; } catch { ms = null; }
            if (!ms) continue;
            try { candidates.push(...ms.getAudioTracks()); } catch { /* */ }
          }
        }
        for (const w of wins) {
          try { candidates.push(...((w && w.__vexaRemoteAudioTracks) || [])); } catch { /* */ }
        }
        {
          const tracks = candidates;
          for (const track of tracks) {
            if (!track || wired.has(track.id)) continue;
            if (track.readyState === "ended") continue;
            try {
              const src = audioCtx.createMediaStreamSource(new MediaStream([track]));
              src.connect(dest);
              wired.add(track.id);
              added++;
              const shortId = String(track.id).slice(0, 11);
              track.addEventListener("ended", () => {
                wired.delete(track.id);
                win.logBot?.(`[telemost-audio] track ended id=${shortId} — rescan подхватит замену`);
              });
            } catch (e) {
              win.logBot?.(`[telemost-audio] wire failed: ${(e as Error).message}`);
            }
          }
        }
        if (added > 0) {
          win.__vexa_telemost_degraded = false;
          win.logBot?.(`[telemost-audio] wired +${added} audio track(s), total=${wired.size}`);
        } else if (wired.size === 0) {
          win.__vexa_telemost_degraded = true;
        }
        return added;
      };

      // Стартовое ожидание первой дорожки (как раньше, ≤30 c) — но через тот же
      // rescan, чтобы стартовый и live-путь были одной логикой.
      let attempts = 0;
      while (attempts++ < 30) {
        if (rescan() > 0) break;
        await new Promise((r) => setTimeout(r, 1000));
      }
      win.logBot?.(`[telemost-audio] initial wired=${wired.size} after ${attempts}s`);
      if (wired.size === 0) {
        win.logBot?.("[telemost-audio] пока нет аудио-источников — rescan остаётся включённым, подхвачу как появятся");
      }

      // Граф захвата строим ВСЕГДА (даже если сейчас 0 дорожек): новые дорожки
      // подмешиваются в dest по мере появления, ScriptProcessor читает микс.
      const proc = audioCtx.createScriptProcessor(4096, 1, 1);

      const bufferSize = Math.round(TARGET_RATE * (CHUNK_DURATION_MS / 1000));
      let acc: number[] = [];

      proc.onaudioprocess = (ev: AudioProcessingEvent) => {
        const ch = ev.inputBuffer.getChannelData(0);
        for (let i = 0; i < ch.length; i++) acc.push(ch[i]);
        while (acc.length >= bufferSize) {
          const chunk = acc.slice(0, bufferSize);
          acc = acc.slice(bufferSize);
          // RMS
          let sum = 0;
          for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
          const rms = Math.sqrt(sum / chunk.length);
          // Send Float32Array via base64
          const f32 = new Float32Array(chunk);
          const u8 = new Uint8Array(f32.buffer);
          let bin = "";
          const CHUNK = 0x8000;
          for (let i = 0; i < u8.length; i += CHUNK) {
            bin += String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK) as any);
          }
          const b64 = btoa(bin);
          try {
            win.__vexaTelemostAudio?.(b64, rms);
          } catch (e) {
            win.logBot?.(`[telemost-audio] exposed call failed: ${(e as Error).message}`);
          }
        }
      };

      const sink = audioCtx.createMediaStreamSource(dest.stream);
      sink.connect(proc);
      proc.connect(audioCtx.destination);

      // ЖИВОЕ слежение: пере-скан DOM каждые 2 c всю встречу — подхватывает
      // новые/заменённые аудио-дорожки (см. ISS-20). Таймер гасится в stop().
      const rescanTimer = setInterval(rescan, 2000);
      win.__vexa_telemost_rescan_timer = rescanTimer;

      // __vexa_telemost_capture_running уже выставлен в начале start() (гард
      // идемпотентности) — повторно здесь не ставим.
      win.logBot?.("[telemost-audio] capture started (16kHz mono, ~3s chunks, live re-hook on)");
    }

    win.__vexa_telemost_start = start;
    win.__vexa_telemost_stop = () => {
      win.__vexa_telemost_capture_running = false;
      try {
        if (win.__vexa_telemost_rescan_timer) {
          clearInterval(win.__vexa_telemost_rescan_timer);
          win.__vexa_telemost_rescan_timer = null;
        }
      } catch {}
    };
    start().catch((e: any) => win.logBot?.(`[telemost-audio] start failed: ${e?.message}`));
  });

  return async () => {
    try {
      await page.evaluate(() => (window as any).__vexa_telemost_stop?.());
    } catch {}
  };
}

// Ф5: запись об одном chunk'е (фрагменте записи между паузами тишины).
// Все *Ms — UNIX epoch ms (Date.now()), та же конвенция, что задал Ф3.
// startTs/endTs — ISO-строки (согласованы с meta.startTs/endTs). Длительность
// речи chunk'а = lastSpeechMs - firstSpeechMs; compute_duration_label (Ф1)
// суммирует это по всем chunks[].
type ChunkRecord = {
  idx: number;
  wav: string;
  startTs: string;
  firstSpeechMs: number | null;
  lastSpeechMs: number | null;
  endTs: string | null;
  samples: number;
  durationS: number;
};

export async function startYandexTelemostRecording(page: Page, botConfig: BotConfig): Promise<void> {
  const sessionUid = botConfig.connectionId || `tm-${Date.now()}`;
  ensureTranscriptDir();

  // Ф5: date-префикс фиксируется ОДИН раз на встречу (см. chunkWavPath) —
  // иначе chunk'и встречи через полночь разъедутся по префиксам.
  const stamp = dateStamp();
  const txtPath = transcriptTxtPath(sessionUid);
  const metaPath = metaJsonPath(sessionUid);

  logStep("recording_start", { session: sessionUid, txt: txtPath, meta: metaPath, stamp });

  const explicitUrl = botConfig.transcriptionServiceUrl || process.env.TRANSCRIPTION_SERVICE_URL;
  const transcriptionUrl = explicitUrl || "http://172.17.0.1:8083/v1/audio/transcriptions";
  if (!explicitUrl) {
    log(`${LOG_PREFIX} WARNING: TRANSCRIPTION_SERVICE_URL не задан в env, используем default ${transcriptionUrl}. На другой VPS/маке gateway IP может отличаться — задай явно в .env.notary.`);
  }
  const transcriptionToken = botConfig.transcriptionServiceToken || process.env.TRANSCRIPTION_SERVICE_TOKEN;
  const language = botConfig.language || "ru";
  const botName = botConfig.botName || "Бот";

  // Ф3 (2026-05-29): live-whisper draft по умолчанию ОТКЛЮЧЁН.
  // На CPU-VPS он тонет (transcribeChain копит сотни 3s-чанков, каждый — POST в
  // faster-whisper-medium на CPU), заваливает логи строками "whisper failed" и
  // грузит transcription-service, при этом для финального протокола бесполезен —
  // протокол собирается из полного WAV через Speechmatics (см. finalize-meeting.py).
  // Код live-draft НЕ удалён — оставлен на случай GPU-сценария в будущем.
  // ENABLE_LIVE_DRAFT=1 — включить (только если есть GPU); дефолт "0" — выключить.
  const liveDraftEnabled = (process.env.ENABLE_LIVE_DRAFT || "0") === "1";
  logStep("live_draft_flag", { enabled: liveDraftEnabled });

  // Ф5 (2026-05-29): SHA коммита vexa/, из которого СОБРАН образ бота.
  // Прокидывается build-time через --build-arg GIT_SHA (см. Dockerfile +
  // Makefile build-bot). Пишется в meta.recording.startedFromCommit — будущий
  // рассинхрон git↔образ виден сразу из meta (инцидент 29.05: образ silence_20min
  // vs HEAD silence_60s — был незаметен, ловился только сравнением вручную).
  // Если build-arg не передан / "unknown" — поле null, не падаем.
  const rawCommit = process.env.NOTARY_BOT_GIT_SHA;
  const startedFromCommit = rawCommit && rawCommit !== "unknown" ? rawCommit : null;
  logStep("bot_image_commit", { startedFromCommit });

  // Ф3-фикс (2026-06-08): series / expectedParticipants приходят в BOT_CONFIG от
  // runner'а, но НЕ объявлены в апстримовой BotConfigSchema (docker.ts) → Zod
  // .parse() по умолчанию ВЫРЕЗАЕТ незнакомые ключи из botConfig. Из-за этого
  // meta.series=null → finalize орфанит протокол (не цепляет серию → уходит в личку
  // вместо группы), а пустой expectedParticipants глушит маппинг имён. Чиним в
  // НАШЕМ адаптере, не трогая апстрим-схему (иначе merge-конфликт при обновлении
  // Vexa): читаем оба поля из СЫРОГО env BOT_CONFIG (его Zod не касался), с
  // фолбэком на botConfig для случая, когда апстрим однажды добавит их в схему.
  const rawBotConfig: any = (() => {
    try { return JSON.parse(process.env.BOT_CONFIG || "{}"); } catch { return {}; }
  })();
  const series: string | null = (botConfig as any).series ?? rawBotConfig.series ?? null;
  const expectedParticipants: string[] =
    (botConfig as any).expectedParticipants ?? rawBotConfig.expectedParticipants ?? [];
  logStep("series_resolved", {
    series,
    expected_count: Array.isArray(expectedParticipants) ? expectedParticipants.length : 0,
  });

  // Ф1 (ISS-28): chat_id группового чата серии. Прокидывается runner'ом в
  // BOT_CONFIG (runner._launch_bot → get_telegram_chat_id_for_series). Читаем из
  // СЫРОГО env BOT_CONFIG (как series — Zod апстрима вырезает незнакомые ключи),
  // с фолбэком на botConfig. В Ф1 НИЧЕГО им не шлём — это задел под Ф2
  // (уведомление в групповой чат «в момент детекта», R4b). Отрицательный int =
  // группа. Кладём в снимок выброса, чтобы Ф2 строила уведомление на готовом.
  const seriesChatId: number | null = (() => {
    const v = (botConfig as any).seriesChatId ?? rawBotConfig.seriesChatId;
    return typeof v === "number" && Number.isFinite(v) ? v : null;
  })();
  logStep("series_chat_resolved", { has_chat: seriesChatId != null });

  // Ф1 (ISS-28) R1: id комнаты из `/j/<meetingId>`. Детект выброса = «адрес
  // потерял этот сегмент» (не «ушёл с домена telemost» — корень его содержит).
  const meetingUrl = botConfig.meetingUrl || (botConfig as any).meetingUrl || "";
  const meetingIdMatch = String(meetingUrl).match(TELEMOST_ROOM_RE);
  const meetingId: string | null =
    (meetingIdMatch && meetingIdMatch[1]) ||
    (botConfig as any).nativeMeetingId ||
    rawBotConfig.nativeMeetingId ||
    null;
  // «В комнате»: адрес держит `/j/<meetingId>`. Если id не распарсился — мягкий
  // фолбэк на наличие любого `/j/<...>` сегмента (всё равно строже старого
  // доменного чека: корень `/` его не имеет → выброс детектится).
  const isInRoom = (u: string): boolean => {
    if (meetingId) return u.includes(`/j/${meetingId}`);
    return TELEMOST_ROOM_RE.test(u);
  };
  logStep("room_detect_armed", { meeting_id: meetingId });

  // Participants polling — параллельно встрече.
  const participantsPoll = startParticipantsPolling(page, botName);

  // Метрики конца встречи.
  // ВАЖНО: silence/pause-таймеры стартуют только ПОСЛЕ первого не-тихого чанка.
  // До этого работает «стартовое ожидание» с лимитом noOneJoinedTimeout — это
  // не «встреча идёт в тишине», а «встреча ещё не началась». Стартовая «фора до
  // первой речи» (meetingStarted) Ф5 НЕ затронута.
  let meetingStarted = false;
  let lastNonSilenceTs = Date.now();
  const startTs = Date.now();
  const noOneJoinedTimeoutMs = botConfig.automaticLeave?.noOneJoinedTimeout ?? 300_000;
  let endReason = "unknown";
  // Ф1 (ISS-28) R3: снимок причины выброса (заполняется в момент детекта evicted).
  let lastEvictionSnapshot: EvictionSnapshot | null = null;

  // Ф2 (ISS-28) состояние детекта/перезахода:
  //   consecutiveOutOfRoom — счётчик подряд идущих чтений URL без `/j/` (R2b:
  //     реагируем на 2-м, флик сбрасывает в 0).
  //   evictionHandling — re-entrancy guard: пока идёт async-перезаход (минуты с
  //     backoff'ом), setInterval продолжает тикать → последующие тики no-op'ят.
  //   deafAfterStartSince — момент начала непрерывной деградации захвата при
  //     удержании `/j/` (R5); null = не деградируем.
  let consecutiveOutOfRoom = 0;
  let evictionHandling = false;
  let evictionRecoveryCount = 0; // успешных возвратов за сессию (антизацикл)
  let deafAfterStartSince: number | null = null;

  // ISS-20 #2: «никто не пришёл» нельзя объявлять, если в панели ВИДНЫ люди —
  // это «оглох» (вероятный сбой захвата), а не пустая комната. При живых людях
  // ждём дольше (вдруг live-перехват #1 подцепит звук), и если к жёсткому
  // потолку звука всё нет — выходим с ОТДЕЛЬНЫМ endReason для точечного алерта.
  const DEAF_WITH_PEOPLE_TIMEOUT_MS = 1_800_000; // 30 мин потолок «оглох при людях»
  let deafFlagged = false;
  const PARTICIPANT_NOISE = ["скопировать ссылк", "копировать ссылк"]; // UI-шум панели, не имя
  // 25.09.2026 (фаза 3а): «есть люди» — по тем, кого видно СЕЙЧАС (плитки и
  // счётчик «N участников», participants.ts), а не по всем, кого бот видел за
  // встречу: иначе после ухода людей «оглох» срабатывал бы ложно. Свежего опроса
  // нет (страница подвисла) — по-старому, по накопленному списку.
  const PRESENCE_FRESH_MS = 3 * 60_000;
  const hasRealParticipants = (): boolean => {
    const p = participantsPoll.getPresence();
    if (p.others !== null && p.at !== null && Date.now() - p.at <= PRESENCE_FRESH_MS) return p.others > 0;
    return participantsPoll.getNames().some((n) => {
      const s = (n || "").trim().toLowerCase();
      return s.length > 0 && !PARTICIPANT_NOISE.some((t) => s.includes(t));
    });
  };
  const presentNow = (): number | null => participantsPoll.getPresence().others;

  // 25.09.2026 (фаза 3а): страховка микрофона и камеры по ходу встречи — если
  // Телемост включил их обратно (или после впуска кнопка ещё не отрисовалась),
  // бот выключает. Нажимает, только если видит «включено»; в лог — только нажатия.
  const DEVICES_GUARD_MS = 60_000;
  let devicesGuardBusy = false;
  const devicesGuard = setInterval(() => {
    if (devicesGuardBusy || page.isClosed()) return;
    devicesGuardBusy = true;
    ensureTelemostDevicesOff(page, "guard", true)
      .catch(() => null)
      .finally(() => { devicesGuardBusy = false; });
  }, DEVICES_GUARD_MS);

  // Ф5: состояния chunk-декаплинга.
  //   recording_active        — пишем сэмплы в текущий chunk_N.wav.
  //   recording_paused_in_call — тишина > CHUNK_SILENCE_PAUSE_MS, writer закрыт,
  //                              бот ОСТАЁТСЯ в звонке, ждём новой речи (resume)
  //                              или суммарной тишины > OUT_OF_CALL_SILENCE_MS (выход).
  // Конвенция *Ms — UNIX epoch ms (Date.now()), та же, что в Ф3 (см. ChunkRecord).
  type RecState = "recording_active" | "recording_paused_in_call";
  let recState: RecState = "recording_active";
  const chunks: ChunkRecord[] = [];
  let currentChunk: ChunkRecord | null = null;
  let currentWriter: WavStreamWriter | null = null;

  // Собрать meta-объект из текущего состояния chunks[].
  const buildMeta = () => {
    const endTs = Date.now();
    // Топ-уровневые агрегаты для Ф1 fallback (НЕС1): первый/последний chunk С РЕЧЬЮ
    // (а не буквально chunks[0]/chunks[-1] — chunk без речи дал бы null и сломал
    // single-chunk fallback). В нормальном прогоне это и есть first/last chunk.
    const withSpeech = chunks.filter((c) => c.firstSpeechMs != null && c.lastSpeechMs != null);
    const topFirst = withSpeech.length ? withSpeech[0].firstSpeechMs : null;
    const topLast = withSpeech.length ? withSpeech[withSpeech.length - 1].lastSpeechMs : null;
    // Страховка Ф2: files.wav → первый chunk С РЕЧЬЮ (withSpeech[0]), не
    // буквально chunks[0]. Кейс «открыли chunk_1 на форе до первой речи, тишина,
    // речь пошла только в chunk_2»: chunks[0] был бы пустым → single-chunk
    // финализатор (legacy) взял бы тишину → «WAV not found»/пустой протокол.
    // (Multichunk-финализатор Ф2 и так читает ВСЕ chunks[], но files.wav —
    // backward-compat fallback, и его надо целить в кусок с реальной речью.)
    const primaryWav = withSpeech.length
      ? withSpeech[0].wav
      : (chunks.length ? chunks[0].wav : null);
    const totalSamples = chunks.reduce((a, c) => a + c.samples, 0);
    const totalDurationS = chunks.reduce((a, c) => a + c.durationS, 0);
    return {
      sessionUid,
      botName,
      meetingUrl: botConfig.meetingUrl || null,
      nativeMeetingId: (botConfig as any).nativeMeetingId || null,
      series: series || null,
      expectedParticipants: expectedParticipants || [],
      language,
      startTs: new Date(startTs).toISOString(),
      endTs: new Date(endTs).toISOString(),
      durationS: Math.round((endTs - startTs) / 1000),
      audioDurationS: Math.round(totalDurationS),
      audioSamples: totalSamples,
      sampleRate: SAMPLE_RATE,
      endReason,
      participants: participantsPoll.getNames(),
      // ВАЖНО (конфиденциальность Ф3 «опасной тройки»): НЕ кладём содержимое
      // транскрипта — только структуру/таймстемпы. Имена участников — да.
      recording: {
        // Ф5: SHA сборки образа — детект рассинхрона git↔образ из meta.
        startedFromCommit,
        // Ф3-конвенция (UNIX epoch ms). Топ-уровень = агрегат по chunks для Ф1
        // single-chunk fallback; основной источник длительности — chunks[].
        firstSpeechMs: topFirst,
        lastSpeechMs: topLast,
        chunks: chunks.map((c) => ({
          idx: c.idx,
          wav: c.wav,
          startTs: c.startTs,
          firstSpeechMs: c.firstSpeechMs,
          lastSpeechMs: c.lastSpeechMs,
          endTs: c.endTs,
          durationS: Math.round(c.durationS),
        })),
        // Ф1 (ISS-28) R3: причина выброса (заполняется при подтверждённом выбросе:
        // evicted_normal_end/_failure/_recovered). По БЕЛОМУ СПИСКУ типов —
        // навигации/console-error-warning/WS-close. Текста реплик/имён здесь нет
        // (RISK5). null для встреч без выброса.
        eviction: lastEvictionSnapshot,
      },
      files: {
        // Backward-compat: до Ф5.3 (multi-chunk finalize) финализатор читает
        // files.wav — указываем на первый chunk. Для классического прогона без
        // пауз chunk_1 = весь WAV встречи. Multi-chunk → meta.recording.chunks[].
        wav: primaryWav,
        draftTxt: txtPath,
        meta: metaPath,
      },
    };
  };

  const writeMeta = (reason: string) => {
    try {
      const meta = buildMeta();
      fs.writeFileSync(metaPath, JSON.stringify(meta, null, 2), "utf8");
      // 0666 чтобы dev мог удалить/перезаписать с хоста (см. WavStreamWriter.open).
      try { fs.chmodSync(metaPath, 0o666); } catch {}
      logStep("meta_written", { path: metaPath, reason, chunks: chunks.length, participants_count: meta.participants.length });
    } catch (err: any) {
      log(`${LOG_PREFIX} meta write failed: ${err.message}`);
    }
  };

  // Открыть новый chunk_(N+1).wav и сделать его текущим (recording_active).
  // Возвращает созданную запись (TS не отслеживает мутацию currentChunk через
  // замыкание — вызывающий использует возврат, а не суженный до null currentChunk).
  const openChunk = (nowMs: number): ChunkRecord => {
    const idx = chunks.length + 1;
    const wav = chunkWavPath(stamp, sessionUid, idx);
    const writer = new WavStreamWriter(wav, SAMPLE_RATE);
    writer.open();
    const rec: ChunkRecord = {
      idx,
      wav,
      startTs: new Date(nowMs).toISOString(),
      firstSpeechMs: null,
      lastSpeechMs: null,
      endTs: null,
      samples: 0,
      durationS: 0,
    };
    chunks.push(rec);
    currentChunk = rec;
    currentWriter = writer;
    recState = "recording_active";
    logStep("chunk_opened", { idx, wav });
    return rec;
  };

  // Закрыть текущий chunk: дописать длины WAV, зафиксировать endTs/samples,
  // эмитить «событие» chunk_closed (инкрементальная meta — внешний poll/Ф5.3
  // увидит закрытый chunk при живом боте; collector гейтит finalize по
  // live-контейнеру (Ф4), так что преждевременной финализации не будет).
  const closeChunk = (nowMs: number, reason: string) => {
    if (currentWriter && currentWriter.isOpen()) {
      let stats = { samples: 0, durationS: 0 };
      try {
        stats = currentWriter.close();
      } catch (err: any) {
        log(`${LOG_PREFIX} chunk close failed: ${err.message}`);
      }
      if (currentChunk) {
        currentChunk.endTs = new Date(nowMs).toISOString();
        currentChunk.samples = stats.samples;
        currentChunk.durationS = stats.durationS;
      }
      logStep("chunk_closed", {
        idx: currentChunk?.idx,
        reason,
        samples: stats.samples,
        duration_s: stats.durationS,
        firstSpeechMs: currentChunk?.firstSpeechMs,
        lastSpeechMs: currentChunk?.lastSpeechMs,
      });
    }
    currentWriter = null;
    writeMeta(`chunk_closed:${reason}`);
  };

  // Открываем chunk_1 сразу — ловим «фору до первой речи» (silence/pause-таймеры
  // стартуют только после meetingStarted, см. checkLoop).
  const firstChunk = openChunk(startTs);
  logStep("wav_writer_opened", { path: firstChunk.wav });

  // FIFO promise-chain для draft-транскрипции (порядок строк в .txt).
  let transcribeChain: Promise<void> = Promise.resolve();

  // Регистрируем exposed function ДО запуска browser-капчи.
  await page.exposeFunction(
    "__vexaTelemostAudio",
    async (b64: string, rms: number) => {
      const RMS_SILENCE_THRESHOLD = 0.003;
      const nowMs = Date.now();
      const isSilent = rms < RMS_SILENCE_THRESHOLD;

      // 1) Декодируем сэмплы СРАЗУ — нужны и для WAV-стрима, и для транскрипции.
      const bin = Buffer.from(b64, "base64");
      const samples = new Float32Array(bin.buffer, bin.byteOffset, bin.byteLength / 4);

      // 2) Речь + границы + resume из паузы. ВАЖНО: resume-транзишн ДО writeSamples,
      //    чтобы первый не-тихий кадр после паузы попал уже в новый chunk_(N+1).
      if (!isSilent) {
        lastNonSilenceTs = nowMs;
        if (recState === "recording_paused_in_call") {
          // Возобновление записи новым chunk'ом.
          openChunk(nowMs);
          logStep("recording_resumed", { idx: currentChunk?.idx });
        }
        if (currentChunk) {
          if (currentChunk.firstSpeechMs == null) currentChunk.firstSpeechMs = nowMs;
          currentChunk.lastSpeechMs = nowMs;
        }
        if (!meetingStarted) {
          meetingStarted = true;
          logStep("meeting_started_first_audio");
        }
      }

      // 3) Пишем сэмплы только когда активны (в паузе writer закрыт). Пишем И
      //    тихие, и шумные кадры — без этого pyannote-диарезация смотрит
      //    обрезанное аудио и таймкоды поедут.
      if (recState === "recording_active" && currentWriter && currentWriter.isOpen()) {
        try {
          currentWriter.writeSamples(samples);
        } catch (err: any) {
          log(`${LOG_PREFIX} wav write failed: ${err.message}`);
        }
      }

      // 4) Стримовая транскрипция (draft) — только не-тихие чанки.
      // Ф3: при ENABLE_LIVE_DRAFT=0 (дефолт) live-whisper не зовётся вообще —
      // transcribeChunk не вызывается, сетевые запросы к TRANSCRIPTION_SERVICE_URL
      // не идут, transcribeChain не растёт. WAV (шаг 3) и метрики (шаг 2) при
      // этом пишутся как обычно — полный WAV для Speechmatics не страдает.
      if (!liveDraftEnabled) return;
      if (isSilent) return;

      transcribeChain = transcribeChain.then(async () => {
        try {
          const wav = float32ToWavBuffer(samples, SAMPLE_RATE);
          const text = await transcribeChunk(wav, language, transcriptionUrl, transcriptionToken);
          if (text) {
            if (isHallucination(text)) {
              log(`${LOG_PREFIX} [telemost-transcript] hallucination filtered: ${text.substring(0, 60)}`);
              return;
            }
            const elapsedS = Math.round((nowMs - startTs) / 1000);
            const line = `[${new Date(nowMs).toISOString()}] (+${elapsedS}s) ${text}`;
            appendTranscript(sessionUid, line);
            log(`${LOG_PREFIX} [telemost-transcript] ${line}`);
          }
        } catch (err: any) {
          log(`${LOG_PREFIX} chunk handle failed: ${err.message}`);
        }
      });
    }
  );

  // Ф2: stopCapture переопределяется при перезаходе (новый browser-capture на
  // новом документе после page.goto) — потому let, не const. finally зовёт
  // актуальное значение.
  let stopCapture = await setupBrowserCapture(page);
  logStep("browser_capture_initialized");

  // Ф1 (ISS-28) R3: вооружаем захват причины выброса (console/nav/WS close-код).
  // Не фатально, если упало — детект выброса (R1) и так работает по URL.
  let evictionCapture: EvictionCapture | null = null;
  try {
    evictionCapture = await setupEvictionCapture(page);
    logStep("eviction_capture_armed");
  } catch (err: any) {
    log(`${LOG_PREFIX} eviction capture setup failed (non-fatal): ${err?.message}`);
  }

  // ── Ф2 (ISS-28) хелперы перезахода/уведомлений ──
  const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

  // R13: сумма РЕЧЕВОГО времени по chunks (speech-span каждого chunk'а, без
  // межчанковых пауз). Прокси «сколько реального разговора записали» — основа
  // guard'а «штатный конец vs ранний сбой». firstSpeechMs/lastSpeechMs ставятся
  // в обработчике аудио независимо от close, так что значение корректно и до, и
  // после closeChunk.
  const recordedSpeechMs = (): number =>
    chunks.reduce(
      (acc, c) =>
        acc + (c.firstSpeechMs != null && c.lastSpeechMs != null ? c.lastSpeechMs - c.firstSpeechMs : 0),
      0,
    );

  // Техпричина выброса для алерта — ТОЛЬКО метаданные (опасная тройка/R11): коды
  // закрытия WS (числа) либо общая фраза. Тексты console-error/реплик сюда НЕ
  // попадают.
  const describeEvictionReason = (snap: EvictionSnapshot | null): string => {
    const wsCodes = snap ? snap.websocketEvents.map((e) => e.code).filter((c) => c != null) : [];
    return wsCodes.length
      ? `соединение закрыто (код ${wsCodes.join(",")})`
      : "соединение с Телемостом разорвано";
  };

  // R2a: одна попытка перезахода. Ре-навигация + штатный join + admission +
  // re-init browser-capture. RISK2: exposeFunction (__vexaTelemostAudio /
  // __vexaTelemostSignal) переживает page.goto — НЕ переэкспонируем (второй
  // expose БРОСАЕТ); пере-запускаем ТОЛЬКО browser-часть (WS-обёртка +
  // setupBrowserCapture, идемпотентны на свежем документе). Возвращает true,
  // если после захода снова в комнате.
  const rejoinTelemost = async (): Promise<boolean> => {
    logStep("rejoin_start", { url: meetingUrl });
    await joinYandexTelemost(page, meetingUrl, botName, botConfig);
    const admitted = await waitForYandexTelemostAdmission(page, REJOIN_ADMISSION_TIMEOUT_MS, botConfig);
    if (!admitted) {
      logStep("rejoin_not_admitted");
      return false;
    }
    // Re-init capture (RISK2): только browser-сторона.
    await installEvictionWsWrapper(page);
    stopCapture = await setupBrowserCapture(page);
    const back = isInRoom(page.url());
    logStep("rejoin_result", { in_room: back });
    return back;
  };

  // R4a + R4b: уведомления при НЕустранимом сбое (перезаход исчерпан). С 24.09.2026
  // (цикл5 фазы 1 плана telemost-update-resilience, С1/Н4) бот сам в Телеграм НЕ
  // пишет: записывает событие в статус входа, а сообщение отправляет сторож на
  // хосте (join_watch → join_alert) — людям туда же, куда уходит протокол, и
  // технический разбор владельцу. Токена в контейнере с браузером нет намеренно.
  // До этого оба сообщения молча пропускались: запускатель не передавал боту токен.
  const sendEvictionAlerts = async (snap: EvictionSnapshot | null): Promise<void> => {
    const reason = describeEvictionReason(snap); // только числовые WS-коды/фикс-фраза
    addJoinEvent("evicted_failure", { detail: reason, attempts: EVICTION_MAX_ATTEMPTS });
    logStep("eviction_alert_event_written", { has_series_chat: seriesChatId != null });
  };

  // Все cleanup'ы в finally — иначе при исключении в main Promise
  // (например, в setInterval/setupBrowserCapture) WAV-fd останется
  // открытым в долгоживущем runner-процессе Ф5+ (в Ф3 docker run --rm
  // умирает и kernel закрывает fd — не утечка, но в Ф5 будет).
  try {
  // Метрики и завершение работы — Promise, который resolve'ится при окончании встречи.
  await new Promise<void>(async (resolve) => {
    const checkLoop = async () => {
      try {
        // Ф2: пока идёт async-перезаход (минуты с backoff'ом) — тики no-op,
        // чтобы не запускать второй перезаход и не дёргать URL-логику.
        if (evictionHandling) return;

        const now = Date.now();
        const url = page.url();

        // Ф1/Ф2 (ISS-28) R1+R2b+R13+R2a: ВЫБРОС из комнаты = адрес потерял
        // `/j/<meetingId>` (раньше был доменный чек, не ловил уход на голый
        // корень — корень `/j/`-сегмента не имеет). R2b: реагируем только после
        // EVICTION_CONFIRM_READS чтений подряд без `/j/` (под-секундный флик
        // SPA-роутера сбрасывает счётчик). На подтверждённом выбросе — guard R13
        // (штатный конец vs сбой), при сбое — авто-перезаход (R2a), при
        // исчерпании попыток — уведомления «в момент детекта» (R4a/R4b).
        if (!isInRoom(url)) {
          consecutiveOutOfRoom++;
          if (consecutiveOutOfRoom < EVICTION_CONFIRM_READS) {
            logStep("eviction_suspected", { url, reads: consecutiveOutOfRoom });
            return; // ждём подтверждения следующим циклом (~3с) — R2b
          }

          // ── Выброс ПОДТВЕРЖДЁН (2 чтения подряд без /j/) ──
          evictionHandling = true; // синхронно ДО первого await — тики off
          // R3: снимок причины (бел. список). page.evaluate НЕ зовём — контекст
          // мог умереть от навигации; данные уже в Node-буферах капчи.
          const snap = evictionCapture ? evictionCapture.getSnapshot() : null;
          lastEvictionSnapshot = snap;
          const lastNav = snap && snap.navigations.length
            ? snap.navigations[snap.navigations.length - 1].url : null;
          const wsCloseCodes = snap
            ? snap.websocketEvents.map((e) => e.code).filter((c) => c != null) : [];

          // Закрываем текущий chunk (речь ДО выброса финализируется как chunk_N);
          // recState=paused → при успешном возврате обработчик аудио откроет
          // chunk_(N+1) на первом не-тихом кадре — нумерация/WAV целы (RISK2).
          closeChunk(now, "evicted");
          recState = "recording_paused_in_call";

          const speechMs = recordedSpeechMs();
          // В docker-лог (его персистит R6) — только МЕТАДАННЫЕ + навигации/WS-коды
          // (тексты console-error НЕ кладём — могли бы нести ПДн; едут лишь в meta).
          logStep("evicted_confirmed", {
            url, meeting_id: meetingId, series_chat_id: seriesChatId,
            meeting_started: meetingStarted, speech_ms: speechMs, last_nav: lastNav,
            ws_events: snap ? snap.websocketEvents.length : 0, ws_close_codes: wsCloseCodes,
            console_errors: snap ? snap.consoleErrors.length : 0,
          });

          // ── R13 guard: штатный конец vs сбой ──
          // Встреча фактически прошла, если началась И записан существенный объём
          // речи (порог A7). Тогда выброс = `evicted_normal_end`: НЕ перезаходить,
          // НЕ алертить, протокол отдать как обычно (Ф3 гейтит доставку по этому
          // reason — для нас это «доставлять»).
          if (meetingStarted && speechMs >= EVICTION_NORMAL_END_MIN_SPEECH_MS) {
            endReason = "evicted_normal_end";
            logStep("evicted_normal_end", { speech_ms: speechMs, threshold_ms: EVICTION_NORMAL_END_MIN_SPEECH_MS });
            writeMeta("evicted_normal_end");
            clearInterval(timer);
            return resolve();
          }

          // Антизацикл: слишком много возвратов за сессию → не перезаходим
          // снова, сразу эскалируем (Яндекс рвёт нас по кругу — руками надёжнее).
          if (evictionRecoveryCount >= EVICTION_MAX_RECOVERY_CYCLES) {
            endReason = "evicted_failure";
            logStep("evicted_failure_recovery_capped", { recovery_cycles: evictionRecoveryCount });
            try {
              await sendEvictionAlerts(snap);
            } catch (e: any) {
              log(`${LOG_PREFIX} eviction alerts failed (non-fatal): ${e?.message}`);
            }
            writeMeta("evicted_failure");
            clearInterval(timer);
            return resolve();
          }

          // ── R2a: авто-перезаход (5 попыток, backoff 0/10/20/40/80с) ──
          let recovered = false;
          for (let attempt = 1; attempt <= EVICTION_MAX_ATTEMPTS; attempt++) {
            const backoff = EVICTION_BACKOFF_MS[attempt - 1] ?? 80_000;
            if (backoff > 0) await sleep(backoff);
            // Между backoff'ами Яндекс мог сам вернуть нас в комнату.
            if (isInRoom(page.url())) {
              await installEvictionWsWrapper(page);
              stopCapture = await setupBrowserCapture(page);
              recovered = true;
              logStep("rejoin_self_recovered", { attempt });
              break;
            }
            try {
              if (await rejoinTelemost()) {
                recovered = true;
                logStep("evicted_recovered", { attempt });
                break;
              }
            } catch (e: any) {
              logStep("rejoin_attempt_failed", { attempt, error: truncOneLine(e?.message || "", 200) });
            }
          }

          if (recovered) {
            // Снова в комнате — продолжаем запись. Сбрасываем guard'ы; recState
            // остаётся paused → первый не-тихий кадр откроет следующий chunk.
            evictionRecoveryCount++;
            endReason = "unknown";
            consecutiveOutOfRoom = 0;
            lastNonSilenceTs = Date.now(); // backoff-окно не считать «тишиной выхода»
            deafAfterStartSince = null;    // deaf-таймер из ДО-выбросного окна стух — не тащим его через возврат
            evictionHandling = false;
            logStep("eviction_recovery_count", { cycles: evictionRecoveryCount });
            writeMeta("evicted_recovered");
            return;
          }

          // ── Перезаход исчерпан → НЕустранимый сбой (R4a/R4b) ──
          endReason = "evicted_failure";
          logStep("evicted_failure", { attempts: EVICTION_MAX_ATTEMPTS, speech_ms: speechMs });
          try {
            await sendEvictionAlerts(snap);
          } catch (e: any) {
            log(`${LOG_PREFIX} eviction alerts failed (non-fatal): ${e?.message}`);
          }
          writeMeta("evicted_failure");
          clearInterval(timer);
          return resolve();
        }

        // В комнате — сбросить счётчик флика, если копился (R2b).
        if (consecutiveOutOfRoom > 0) {
          logStep("eviction_flicker_recovered", { reads: consecutiveOutOfRoom });
          consecutiveOutOfRoom = 0;
        }

        // До «начала встречи» работает только noOneJoinedTimeout.
        if (!meetingStarted) {
          const elapsed = now - startTs;
          const sawPeople = hasRealParticipants();
          // ISS-20 #2: люди в панели → не «пустая комната». Ждём дольше (до
          // DEAF_WITH_PEOPLE_TIMEOUT_MS), вдруг #1-перехват подцепит звук.
          const effectiveTimeoutMs = sawPeople ? DEAF_WITH_PEOPLE_TIMEOUT_MS : noOneJoinedTimeoutMs;
          // Один раз отметим «вижу людей, но глухо» по достижении обычного
          // таймаута — сигнал для алерта владельцу (#5), даже если ещё ждём.
          if (sawPeople && !deafFlagged && elapsed >= noOneJoinedTimeoutMs) {
            deafFlagged = true;
            logStep("deaf_with_participants_warning", {
              elapsed_ms: elapsed, participants: participantsPoll.getNames().length, present: presentNow(),
            });
          }
          if (elapsed >= effectiveTimeoutMs) {
            endReason = sawPeople ? "deaf_with_participants" : "no_one_joined";
            logStep(sawPeople ? "end_deaf_with_participants" : "end_no_one_joined", {
              elapsed_ms: elapsed, timeout_ms: effectiveTimeoutMs,
              participants: participantsPoll.getNames().length, present: presentNow(),
            });
            clearInterval(timer);
            return resolve();
          }
          return;
        }

        // R5 (ISS-28): «оглох ПОСЛЕ начала» при УДЕРЖАНИИ `/j/`. Сюда попадаем
        // только если адрес ДЕРЖИТ `/j/` (выброс отработал бы выше и вышел/
        // перезашёл) — значит это чистый ISS-20 (аудио-дорожки умерли, а
        // подключение живо), а НЕ R1. Сигнал = browser-флаг __vexa_telemost_degraded
        // (0 живых дорожек после rescan) + видимые реальные участники, удержанные
        // DEAF_AFTER_START_TIMEOUT_MS. Классифицируем как ОТДЕЛЬНУЮ причину
        // `deaf_after_start` (не «молчаливый» silence_out_of_call) + личный алерт
        // в момент детекта. Дорожки вернулись / люди ушли → сбрасываем таймер.
        let degraded = false;
        try {
          degraded = await page.evaluate(() => (window as any).__vexa_telemost_degraded === true);
        } catch {
          degraded = false; // контекст недоступен — не триггерим (безопасно)
        }
        if (degraded && hasRealParticipants()) {
          if (deafAfterStartSince == null) {
            deafAfterStartSince = now;
            logStep("deaf_after_start_suspected", { participants: participantsPoll.getNames().length, present: presentNow() });
          } else if (now - deafAfterStartSince >= DEAF_AFTER_START_TIMEOUT_MS) {
            evictionHandling = true; // подавить тики, пока шлём алерт и выходим
            endReason = "deaf_after_start";
            logStep("end_deaf_after_start", {
              degraded_for_ms: now - deafAfterStartSince,
              participants: participantsPoll.getNames().length,
            });
            try {
              // Сообщение владельцу шлёт сторож на хосте по этому событию (см. выше, R4a).
              addJoinEvent("deaf_after_start", { degraded_for_s: Math.round((now - deafAfterStartSince) / 1000) });
            } catch (e: any) {
              log(`${LOG_PREFIX} deaf alert failed (non-fatal): ${e?.message}`);
            }
            writeMeta("deaf_after_start");
            clearInterval(timer);
            return resolve();
          }
        } else if (deafAfterStartSince != null) {
          logStep("deaf_after_start_recovered", { degraded, has_people: hasRealParticipants() });
          deafAfterStartSince = null;
        }

        // Ф5 chunk-декаплинг: две границы тишины.
        const silentForMs = now - lastNonSilenceTs;
        if (recState === "recording_active") {
          // Тишина > порога приостановки → закрыть chunk, остаться в звонке.
          if (silentForMs >= CHUNK_SILENCE_PAUSE_MS) {
            closeChunk(now, "silence_pause");
            recState = "recording_paused_in_call";
            logStep("recording_paused", { silent_for_ms: silentForMs, chunks: chunks.length });
          }
        } else {
          // recording_paused_in_call: суммарная тишина > выходного порога → выход.
          if (silentForMs >= OUT_OF_CALL_SILENCE_MS) {
            endReason = "silence_out_of_call";
            logStep("end_silence_out_of_call", { silent_for_ms: silentForMs });
            clearInterval(timer);
            return resolve();
          }
        }
      } catch {
        // page может закрыться — не fatal
      }
    };

    const timer = setInterval(checkLoop, URL_CHECK_INTERVAL_MS);

    // Если page закрылся — выходим.
    page.on("close", () => {
      endReason = "page_closed";
      logStep("end_page_closed");
      clearInterval(timer);
      resolve();
    });
  });
  } finally {
    // ВАЖНО: cleanup в finally — даже если main Promise бросил.
    // Порядок: сначала остановить polling (он мог быть в середине открытия панели),
    // потом stopCapture, потом close текущего chunk'а + final_exit meta.
    try {
      participantsPoll.stop();
    } catch (err: any) {
      log(`${LOG_PREFIX} participants stop failed: ${err.message}`);
    }
    clearInterval(devicesGuard);
    try {
      await stopCapture();
    } catch (err: any) {
      log(`${LOG_PREFIX} stop capture failed: ${err.message}`);
    }
    try {
      if (evictionCapture) await evictionCapture.detach();
    } catch (err: any) {
      log(`${LOG_PREFIX} eviction capture detach failed: ${err.message}`);
    }

    // final_exit «событие». closeChunk само-защищён (закрывает writer только
    // если он открыт) и ВСЕГДА пишет финальную meta с полным chunks[]. Если вышли
    // из паузы (writer уже закрыт) — chunk не трогается, пишется только meta.
    const closeTs = Date.now();
    closeChunk(closeTs, "final_exit");

    logStep("recording_done", {
      chunks: chunks.length,
      end_reason: endReason,
      participants_count: participantsPoll.getNames().length,
      startedFromCommit,
    });
  }
}
