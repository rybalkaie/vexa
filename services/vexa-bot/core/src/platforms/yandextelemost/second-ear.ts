// Второе ухо бота — запись всего, что играет браузер (ISS-60, фаза 4 плана
// telemost-update-resilience, 25.09.2026). Решения — в second-ear-judge.ts.
//
// КАК. Браузер бота играет звук звонка в звуковую систему контейнера: вывод по
// умолчанию — `zoom_sink` (entrypoint.sh задаёт его явно). Готовый класс Vexa
// `PulseAudioCapture` (services/audio-pipeline.ts, у Zoom тот же приём) снимает
// монитор этого вывода через `parecord`: 16 кГц, моно, 16 бит, куски по 3 с.
// Каждый кусок: RMS → судье (держать ли бота на встрече, не оглохла ли основная
// запись) → в файл `<дата>-<сессия>.chunk0.wav` рядом с основной записью.
//
// 🔴 НИКОГДА НЕ tts_sink. Из монитора tts_sink сделан микрофон бота (virtual_mic):
// звук встречи, выведенный туда или снятый оттуда с заглушки, вернулся бы людям
// эхом через микрофон бота. Второе ухо только СЛУШАЕТ монитор zoom_sink.
//
// Файл пишется как основная запись: заголовок WAV обновляется после каждого куска
// (убитый процесс оставляет валидный файл), права 0666 (хост убирает от dev).
// После 5 мин тишины файл не растёт (earShouldWrite) — снова пишет с первого звука.
//
// Опасная тройка: второе ухо не расшифровывает и в журнал пишет только числа
// (длительность, громкость, число перезапусков) — ни звука, ни текста.
//
// Сбой второго уха (нет parecord, звуковая система не отвечает) основную запись не
// трогает: start()/stop() не бросают, статус «failed» уходит в meta.

import { PulseAudioCapture, AudioChunk } from "../../services/audio-pipeline";
import {
  EAR_BLOCK_MS,
  EAR_SILENCE_RMS,
  SECOND_EAR_DEVICE,
  earShouldWrite,
  pcm16Rms,
} from "./second-ear-judge";
import { EarWavWriter, SECOND_EAR_SAMPLE_RATE as SAMPLE_RATE, WAV_HEADER_LEN } from "./second-ear-wav";

/** Нет данных от parecord дольше — перезапуск (процесс мог умереть). */
const STALL_RESTART_MS = 12_000;
const WATCH_EVERY_MS = 4_000;
const MAX_RESTARTS = 5;

export type SecondEarBlock = { ts: number; rms: number; loud: boolean };
export type SecondEarStatus = "idle" | "starting" | "recording" | "failed" | "stopped";

export type SecondEarStats = {
  wav: string;
  device: string;
  status: SecondEarStatus;
  error: string | null;
  restarts: number;
  /** Сэмплов, записанных в файл (без пауз тишины). */
  samples: number;
  durationS: number;
  startTs: string | null;
  endTs: string | null;
  /** Сколько раз файл вставал на паузу тишины. */
  pauses: number;
};

type LogFn = (step: string, ctx?: Record<string, unknown>) => void;

export class SecondEarRecorder {
  private capture: PulseAudioCapture | null = null;
  private writer: EarWavWriter;
  private status: SecondEarStatus = "idle";
  private error: string | null = null;
  private restarts = 0;
  private pauses = 0;
  private startedAt: number | null = null;
  private endedAt: number | null = null;
  private lastDataAt: number | null = null;
  private lastRestartAt: number | null = null;
  private lastLoudAt: number | null = null;
  private writing = true;
  private watch: ReturnType<typeof setInterval> | null = null;
  private writeFailed = false;
  private readonly device: string;

  constructor(
    private readonly wavPath: string,
    private readonly onBlock: (b: SecondEarBlock) => void,
    private readonly log: LogFn,
    device: string = SECOND_EAR_DEVICE,
  ) {
    // Страховка от опечатки в настройках: микрофон бота второе ухо не слушает никогда.
    this.device = /tts/i.test(device) ? SECOND_EAR_DEVICE : device;
    this.writer = new EarWavWriter(wavPath);
  }

  /** Запустить запись. Не бросает: сбой → статус failed, основная запись идёт как шла. */
  async start(): Promise<void> {
    if (this.status !== "idle") return;
    this.status = "starting";
    this.startedAt = Date.now();
    try {
      const cap = new PulseAudioCapture({
        device: this.device,
        sampleRate: SAMPLE_RATE,
        channels: 1,
        chunkDurationSec: EAR_BLOCK_MS / 1000,
      });
      // Без слушателя 'error' EventEmitter бросил бы исключение на ошибке parecord.
      cap.on("error", (err: Error) => {
        this.error = String(err?.message || err).slice(0, 200);
        this.log("second_ear_error", { error: this.error, status: this.status });
        if (this.status === "starting") this.status = "failed";
      });
      cap.on("chunk", (c: AudioChunk) => this.onChunk(c));
      this.capture = cap;
      await cap.start();
      if (this.status === "starting") this.status = "recording";
      this.watch = setInterval(() => this.checkAlive(), WATCH_EVERY_MS);
      this.log("second_ear_started", { device: `${this.device}.monitor`, wav: this.wavPath });
    } catch (err: any) {
      this.status = "failed";
      this.error = String(err?.message || err).slice(0, 200);
      this.log("second_ear_failed", { error: this.error });
    }
  }

  private checkAlive(): void {
    if (this.status !== "recording" || !this.capture) return;
    const now = Date.now();
    // Последний признак жизни: звук, прошлый перезапуск или старт. lastDataAt — только
    // настоящий звук (по нему stop() решает, работало ли второе ухо вообще).
    const lastSign = Math.max(this.lastDataAt ?? 0, this.lastRestartAt ?? 0, this.startedAt ?? 0);
    if (now - lastSign < STALL_RESTART_MS) return;
    if (this.restarts >= MAX_RESTARTS) return;
    this.restarts += 1;
    this.lastRestartAt = now;
    this.log("second_ear_restart", { attempt: this.restarts });
    // parecord умер → process=null → start() поднимет новый; жив, но молчит → start() ничего не делает.
    this.capture.start().catch((e: any) => {
      this.error = String(e?.message || e).slice(0, 200);
      this.log("second_ear_restart_failed", { attempt: this.restarts, error: this.error });
    });
  }

  private onChunk(c: AudioChunk): void {
    const pcm = c.data.length > WAV_HEADER_LEN ? c.data.subarray(WAV_HEADER_LEN) : Buffer.alloc(0);
    if (pcm.length === 0) return;
    const now = Date.now();
    if (this.lastDataAt === null) this.log("second_ear_first_audio", {});
    this.lastDataAt = now;
    if (this.status === "starting") this.status = "recording";
    const rms = pcm16Rms(pcm);
    const loud = rms >= EAR_SILENCE_RMS;
    const write = earShouldWrite(this.lastLoudAt, this.startedAt ?? now, now, loud);
    if (loud) this.lastLoudAt = now;
    if (write !== this.writing) {
      this.writing = write;
      if (!write) this.pauses += 1;
      this.log(write ? "second_ear_resumed" : "second_ear_paused", {});
    }
    if (write && !this.writeFailed) {
      try {
        this.writer.append(pcm);
      } catch (e: any) {
        this.writeFailed = true;
        this.log("second_ear_write_failed", { error: String(e?.message || e).slice(0, 200) });
      }
    }
    if (!c.isFinal) {
      try {
        this.onBlock({ ts: now, rms, loud });
      } catch (e: any) {
        this.log("second_ear_block_handler_failed", { error: String(e?.message || e).slice(0, 200) });
      }
    }
  }

  /** Остановить и закрыть файл. Не бросает. */
  async stop(): Promise<void> {
    if (this.watch) {
      clearInterval(this.watch);
      this.watch = null;
    }
    if (this.status === "stopped") return;
    try {
      if (this.capture) await this.capture.stop(); // хвост приходит последним куском (isFinal)
    } catch (e: any) {
      this.log("second_ear_stop_failed", { error: String(e?.message || e).slice(0, 200) });
    }
    this.writer.close();
    this.endedAt = Date.now();
    // Звук хоть раз пришёл — второе ухо работало (ошибка, если была, остаётся в error).
    // Не пришёл ни разу (нет parecord, звуковая система молчит) — не работало.
    this.status = this.lastDataAt === null ? "failed" : "stopped";
    if (this.lastDataAt === null && !this.error) this.error = "no_audio_from_monitor";
    const st = this.stats();
    this.log("second_ear_stopped", {
      status: st.status, duration_s: st.durationS, restarts: st.restarts, pauses: st.pauses,
    });
  }

  stats(): SecondEarStats {
    const samples = this.writer.samples;
    return {
      wav: this.wavPath,
      device: this.device,
      status: this.status,
      error: this.error,
      restarts: this.restarts,
      samples,
      durationS: Math.round((samples / SAMPLE_RATE) * 10) / 10,
      startTs: this.startedAt !== null ? new Date(this.startedAt).toISOString() : null,
      endTs: this.endedAt !== null ? new Date(this.endedAt).toISOString() : null,
      pauses: this.pauses,
    };
  }
}
