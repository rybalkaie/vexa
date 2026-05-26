// Yandex Telemost: запись и транскрипция.
//
// Архитектура Ф2 (диарезация — Ф3, не делаем):
//   1) Browser-сторона:
//        - Найти все <audio>/<video> элементы на странице.
//        - Свести их MediaStream в один combinedStream (без per-speaker).
//        - ScriptProcessor 16kHz mono → Float32 chunks ~3s длиной.
//        - Передавать чанки в Node.js через exposed function __vexaTelemostAudio.
//   2) Node.js-сторона:
//        - Каждый чанк → WAV → POST в TRANSCRIPTION_SERVICE_URL → текст.
//        - Текст с таймкодом писать в /transcripts/<sessionUid>.txt (bind-mount).
//        - Дублировать в stdout (с пометкой «[telemost-transcript]» — для VPS-логов).
//   3) Метрики конца встречи:
//        - 60s полной тишины (RMS < threshold по всем чанкам) → завершение.
//        - URL ушёл с telemost.yandex.ru/j/... → завершение.
//        - 0 видимых participant-тайлов в течение 30s → завершение (стартовый
//          alone-timeout — берём из botConfig.automaticLeave).

import { Page } from "playwright";
import { BotConfig } from "../../types";
import { log } from "../../utils";
import { telemostParticipantSelectors } from "./selectors";
import * as fs from "fs";
import * as path from "path";

const LOG_PREFIX = "[adapter-telemost]";
const TRANSCRIPT_DIR = process.env.TELEMOST_TRANSCRIPT_DIR || "/transcripts";
const SILENCE_END_AFTER_MS = 60_000;
const NO_PARTICIPANTS_END_AFTER_MS = 30_000;
const URL_CHECK_INTERVAL_MS = 5_000;
const PARTICIPANT_CHECK_INTERVAL_MS = 5_000;

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

function transcriptPath(sessionUid: string): string {
  const date = new Date().toISOString().split("T")[0];
  return path.join(TRANSCRIPT_DIR, `${date}-${sessionUid}.txt`);
}

function appendTranscript(sessionUid: string, line: string): void {
  try {
    const p = transcriptPath(sessionUid);
    fs.appendFileSync(p, line + "\n", "utf8");
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

async function transcribeChunk(
  wav: Buffer,
  language: string,
  serviceUrl: string,
  serviceToken?: string
): Promise<string | null> {
  try {
    const form = new FormData();
    // Cast to Uint8Array — Node Buffer<ArrayBufferLike> isn't directly BlobPart-compatible in TS DOM types.
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
  logStep("recording_start", { session: sessionUid, transcript_path: transcriptPath(sessionUid) });

  const transcriptionUrl = botConfig.transcriptionServiceUrl
    || process.env.TRANSCRIPTION_SERVICE_URL
    || "http://172.17.0.1:8083/v1/audio/transcriptions";
  const transcriptionToken = botConfig.transcriptionServiceToken || process.env.TRANSCRIPTION_SERVICE_TOKEN;
  const language = botConfig.language || "ru";

  // Метрики конца встречи
  let lastNonSilenceTs = Date.now();
  let lastSeenParticipantsTs = Date.now();
  let knownParticipantCount = 0;
  const startTs = Date.now();

  // Регистрируем exposed function ДО запуска browser-капчи.
  await page.exposeFunction(
    "__vexaTelemostAudio",
    async (b64: string, rms: number) => {
      const RMS_SILENCE_THRESHOLD = 0.003;
      const nowMs = Date.now();
      const isSilent = rms < RMS_SILENCE_THRESHOLD;
      if (!isSilent) {
        lastNonSilenceTs = nowMs;
      }
      // Транскрибируем только не-тихие чанки — экономия CPU.
      if (isSilent) return;

      try {
        const bin = Buffer.from(b64, "base64");
        const samples = new Float32Array(bin.buffer, bin.byteOffset, bin.byteLength / 4);
        const wav = float32ToWavBuffer(samples, 16000);
        const text = await transcribeChunk(wav, language, transcriptionUrl, transcriptionToken);
        if (text) {
          const elapsedS = Math.round((nowMs - startTs) / 1000);
          const line = `[${new Date(nowMs).toISOString()}] (+${elapsedS}s) ${text}`;
          appendTranscript(sessionUid, line);
          log(`${LOG_PREFIX} [telemost-transcript] ${line}`);
        }
      } catch (err: any) {
        log(`${LOG_PREFIX} chunk handle failed: ${err.message}`);
      }
    }
  );

  const stopCapture = await setupBrowserCapture(page);
  logStep("browser_capture_initialized");

  // Метрики и завершение работы — Promise, который resolve'ится при окончании встречи.
  await new Promise<void>(async (resolve, reject) => {
    const checkLoop = async () => {
      try {
        const now = Date.now();
        const url = page.url();

        // URL change check
        if (!url.includes("telemost.yandex.ru")) {
          logStep("end_url_changed", { url });
          clearInterval(timer);
          return resolve();
        }

        // Silence check
        if (now - lastNonSilenceTs >= SILENCE_END_AFTER_MS) {
          logStep("end_silence_60s", { silent_for_ms: now - lastNonSilenceTs });
          clearInterval(timer);
          return resolve();
        }

        // Participant count — каждые N секунд
        if (now - lastSeenParticipantsTs > PARTICIPANT_CHECK_INTERVAL_MS) {
          let cnt = 0;
          try {
            cnt = await page.evaluate((selectors: string[]) => {
              const seen = new Set<string>();
              for (const sel of selectors) {
                document.querySelectorAll(sel).forEach((el, idx) => {
                  const key = el.getAttribute("data-testid") || `${sel}-${idx}`;
                  seen.add(key);
                });
              }
              return seen.size;
            }, telemostParticipantSelectors);
          } catch (e: any) {
            // page может закрыться при leave — игнорируем
          }
          if (cnt > 0) {
            knownParticipantCount = cnt;
            lastSeenParticipantsTs = now;
          } else if (knownParticipantCount > 0 && now - lastSeenParticipantsTs >= NO_PARTICIPANTS_END_AFTER_MS) {
            logStep("end_no_participants_30s", { last_known: knownParticipantCount });
            clearInterval(timer);
            return resolve();
          }
        }
      } catch (err: any) {
        // page may be closed during leave; not fatal
      }
    };

    const timer = setInterval(checkLoop, URL_CHECK_INTERVAL_MS);

    // Если page закрылся — выходим.
    page.on("close", () => {
      logStep("end_page_closed");
      clearInterval(timer);
      resolve();
    });
  });

  await stopCapture();
  logStep("recording_done", { transcript: transcriptPath(sessionUid) });
}
