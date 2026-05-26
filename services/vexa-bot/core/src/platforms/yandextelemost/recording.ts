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
const SILENCE_END_AFTER_MS = 60_000;
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

function transcriptWavPath(sessionUid: string): string {
  return path.join(TRANSCRIPT_DIR, `${dateStamp()}-${sessionUid}.wav`);
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

export async function startYandexTelemostRecording(page: Page, botConfig: BotConfig): Promise<void> {
  const sessionUid = botConfig.connectionId || `tm-${Date.now()}`;
  ensureTranscriptDir();

  const wavPath = transcriptWavPath(sessionUid);
  const txtPath = transcriptTxtPath(sessionUid);
  const metaPath = metaJsonPath(sessionUid);

  logStep("recording_start", { session: sessionUid, txt: txtPath, wav: wavPath, meta: metaPath });

  const explicitUrl = botConfig.transcriptionServiceUrl || process.env.TRANSCRIPTION_SERVICE_URL;
  const transcriptionUrl = explicitUrl || "http://172.17.0.1:8083/v1/audio/transcriptions";
  if (!explicitUrl) {
    log(`${LOG_PREFIX} WARNING: TRANSCRIPTION_SERVICE_URL не задан в env, используем default ${transcriptionUrl}. На другой VPS/маке gateway IP может отличаться — задай явно в .env.notary.`);
  }
  const transcriptionToken = botConfig.transcriptionServiceToken || process.env.TRANSCRIPTION_SERVICE_TOKEN;
  const language = botConfig.language || "ru";
  const botName = botConfig.botName || "Бот";

  // Full WAV writer — пишем все сэмплы непрерывным потоком.
  const wavWriter = new WavStreamWriter(wavPath, SAMPLE_RATE);
  wavWriter.open();
  logStep("wav_writer_opened", { path: wavPath });

  // Participants polling — параллельно встрече.
  const participantsPoll = startParticipantsPolling(page, botName);

  // Метрики конца встречи.
  // ВАЖНО: silence-таймер стартует только ПОСЛЕ того как мы услышали первый
  // не-тихий чанк. До этого считается «стартовое ожидание» с лимитом
  // noOneJoinedTimeout — это не «встреча идёт в тишине», а «встреча ещё не
  // началась» (план Ф2: «Встреча идёт» = был хотя бы 1 не-тихий аудио-кадр).
  let meetingStarted = false;
  let lastNonSilenceTs = Date.now();
  const startTs = Date.now();
  const noOneJoinedTimeoutMs = botConfig.automaticLeave?.noOneJoinedTimeout ?? 300_000;

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

      // 2) Пишем в полный WAV ВСЕ сэмплы — и тихие, и шумные. Без этого
      //    pyannote-диарезация смотрит обрезанное аудио и таймкоды поедут.
      try {
        wavWriter.writeSamples(samples);
      } catch (err: any) {
        log(`${LOG_PREFIX} wav write failed: ${err.message}`);
      }

      // 3) Метрики «встреча идёт».
      if (!isSilent) {
        lastNonSilenceTs = nowMs;
        if (!meetingStarted) {
          meetingStarted = true;
          logStep("meeting_started_first_audio");
        }
      }

      // 4) Стримовая транскрипция (draft) — только не-тихие чанки.
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

  let endReason = "unknown";

  // Все cleanup'ы в finally — иначе при исключении в main Promise
  // (например, в setInterval/setupBrowserCapture) WAV-fd останется
  // открытым в долгоживущем runner-процессе Ф5+ (в Ф3 docker run --rm
  // умирает и kernel закрывает fd — не утечка, но в Ф5 будет).
  try {
  // Метрики и завершение работы — Promise, который resolve'ится при окончании встречи.
  await new Promise<void>(async (resolve, reject) => {
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

        // Silence check (только после meetingStarted)
        if (now - lastNonSilenceTs >= SILENCE_END_AFTER_MS) {
          endReason = "silence_60s";
          logStep("end_silence_60s", { silent_for_ms: now - lastNonSilenceTs });
          clearInterval(timer);
          return resolve();
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
    // потом stopCapture, потом close WAV.
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

    // Закрываем WAV — обновляются длины в заголовке.
    let wavStats = { samples: 0, durationS: 0 };
    try {
      wavStats = wavWriter.close();
      logStep("wav_writer_closed", { samples: wavStats.samples, duration_s: wavStats.durationS });
    } catch (err: any) {
      log(`${LOG_PREFIX} wav close failed: ${err.message}`);
    }

    // Пишем meta.json. ВАЖНО: НЕ кладём в meta содержимое транскрипта — только пути.
    // Это правило конфиденциальности Ф3 «опасной тройки»: транскрипт = личные данные,
    // metadata = структура. Имена участников — да, они оправдают существование маппинга.
    const endTs = Date.now();
    const meta = {
      sessionUid,
      botName,
      meetingUrl: botConfig.meetingUrl || null,
      nativeMeetingId: (botConfig as any).nativeMeetingId || null,
      language,
      startTs: new Date(startTs).toISOString(),
      endTs: new Date(endTs).toISOString(),
      durationS: Math.round((endTs - startTs) / 1000),
      audioDurationS: Math.round(wavStats.durationS),
      audioSamples: wavStats.samples,
      sampleRate: SAMPLE_RATE,
      endReason,
      participants: participantsPoll.getNames(),
      files: {
        wav: wavPath,
        draftTxt: txtPath,
        meta: metaPath,
      },
    };
    try {
      fs.writeFileSync(metaPath, JSON.stringify(meta, null, 2), "utf8");
      // 0666 чтобы dev мог удалить/перезаписать с хоста (см. WavStreamWriter.open).
      try { fs.chmodSync(metaPath, 0o666); } catch {}
      logStep("meta_written", { path: metaPath, participants_count: meta.participants.length });
    } catch (err: any) {
      log(`${LOG_PREFIX} meta write failed: ${err.message}`);
    }

    logStep("recording_done", {
      wav: wavPath,
      duration_s: wavStats.durationS,
      end_reason: endReason,
      participants_count: meta.participants.length,
    });
  }
}
