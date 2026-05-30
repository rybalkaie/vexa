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

import { Page } from "playwright";
import { BotConfig } from "../../types";
import { log } from "../../utils";
import { isHallucination } from "../../services/hallucination-filter";
import { startParticipantsPolling } from "./participants";
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
  }

  close(): { samples: number; durationS: number } {
    if (this.fd === null) return { samples: 0, durationS: 0 };
    const dataLength = this.samplesWritten * 2;
    const riffSize = 36 + dataLength;
    // Обновляем RIFF size (offset 4) и data size (offset 40).
    const riffSizeBuf = Buffer.alloc(4);
    riffSizeBuf.writeUInt32LE(riffSize, 0);
    fs.writeSync(this.fd, riffSizeBuf, 0, 4, 4);
    const dataSizeBuf = Buffer.alloc(4);
    dataSizeBuf.writeUInt32LE(dataLength, 0);
    fs.writeSync(this.fd, dataSizeBuf, 0, 4, 40);
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
  await page.evaluate(() => {
    const win = window as any;

    async function start() {
      const TARGET_RATE = 16000;
      const CHUNK_DURATION_MS = 3000;

      win.logBot?.("[telemost-audio] discovering media elements…");

      // Wait until at least one <audio>/<video> with audio is present
      let attempts = 0;
      let mediaElements: HTMLMediaElement[] = [];
      while (attempts++ < 30) {
        const all = Array.from(document.querySelectorAll("audio, video")) as HTMLMediaElement[];
        mediaElements = all.filter((el) => {
          try {
            const ms = (el as any).srcObject as MediaStream | null;
            return ms && ms.getAudioTracks().length > 0;
          } catch {
            return false;
          }
        });
        if (mediaElements.length > 0) break;
        await new Promise((r) => setTimeout(r, 1000));
      }

      win.logBot?.(`[telemost-audio] found ${mediaElements.length} media elements with audio after ${attempts}s`);
      if (mediaElements.length === 0) {
        win.logBot?.("[telemost-audio] no audio sources — entering degraded mode (silent transcripts)");
        win.__vexa_telemost_degraded = true;
        return;
      }

      const AudioCtxCls = win.AudioContext || win.webkitAudioContext;
      const audioCtx = new AudioCtxCls({ sampleRate: TARGET_RATE });
      const dest = audioCtx.createMediaStreamDestination();
      for (const el of mediaElements) {
        try {
          const ms = (el as any).srcObject as MediaStream;
          if (!ms) continue;
          const sourceTracks = ms.getAudioTracks();
          if (sourceTracks.length === 0) continue;
          const src = audioCtx.createMediaStreamSource(new MediaStream([sourceTracks[0]]));
          src.connect(dest);
        } catch (e) {
          win.logBot?.(`[telemost-audio] failed to wire element: ${(e as Error).message}`);
        }
      }
      const combined = dest.stream;
      const source = audioCtx.createMediaStreamSource(combined);
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

      source.connect(proc);
      proc.connect(audioCtx.destination);
      win.__vexa_telemost_capture_running = true;
      win.logBot?.("[telemost-audio] capture started (16kHz mono, ~3s chunks)");
    }

    win.__vexa_telemost_start = start;
    win.__vexa_telemost_stop = () => {
      win.__vexa_telemost_capture_running = false;
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
    const primaryWav = chunks.length ? chunks[0].wav : null;
    const totalSamples = chunks.reduce((a, c) => a + c.samples, 0);
    const totalDurationS = chunks.reduce((a, c) => a + c.durationS, 0);
    return {
      sessionUid,
      botName,
      meetingUrl: botConfig.meetingUrl || null,
      nativeMeetingId: (botConfig as any).nativeMeetingId || null,
      series: (botConfig as any).series || null,
      expectedParticipants: (botConfig as any).expectedParticipants || [],
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

  const stopCapture = await setupBrowserCapture(page);
  logStep("browser_capture_initialized");

  // Все cleanup'ы в finally — иначе при исключении в main Promise
  // (например, в setInterval/setupBrowserCapture) WAV-fd останется
  // открытым в долгоживущем runner-процессе Ф5+ (в Ф3 docker run --rm
  // умирает и kernel закрывает fd — не утечка, но в Ф5 будет).
  try {
  // Метрики и завершение работы — Promise, который resolve'ится при окончании встречи.
  await new Promise<void>(async (resolve) => {
    const checkLoop = async () => {
      try {
        const now = Date.now();
        const url = page.url();

        // URL change.
        if (!url.includes("telemost.yandex.ru")) {
          endReason = "url_changed";
          logStep("end_url_changed", { url });
          clearInterval(timer);
          return resolve();
        }

        // До «начала встречи» работает только noOneJoinedTimeout.
        if (!meetingStarted) {
          if (now - startTs >= noOneJoinedTimeoutMs) {
            endReason = "no_one_joined";
            logStep("end_no_one_joined", { elapsed_ms: now - startTs, timeout_ms: noOneJoinedTimeoutMs });
            clearInterval(timer);
            return resolve();
          }
          return;
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
    try {
      await stopCapture();
    } catch (err: any) {
      log(`${LOG_PREFIX} stop capture failed: ${err.message}`);
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
