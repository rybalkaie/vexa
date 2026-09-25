// Файл второго уха (ISS-60, фаза 4): WAV 16 кГц моно 16 бит, который валиден на
// диске в любой момент — как у основной записи (recording.ts, WavStreamWriter):
// заголовок обновляется после каждого куска, поэтому убитый процесс (OOM, kill -9)
// оставляет файл, который финализация прочитает целиком. Отдельный модуль — чтобы
// его можно было проверить тестом без звуковой системы (second-ear.test.ts).

import * as fs from "fs";

export const SECOND_EAR_SAMPLE_RATE = 16_000;
export const WAV_HEADER_LEN = 44;

/** Заголовок WAV (PCM, моно, 16 бит) под `dataLength` байт данных. */
export function wavHeader(dataLength: number, sampleRate: number = SECOND_EAR_SAMPLE_RATE): Buffer {
  const h = Buffer.alloc(WAV_HEADER_LEN);
  h.write("RIFF", 0);
  h.writeUInt32LE(36 + dataLength, 4);
  h.write("WAVE", 8);
  h.write("fmt ", 12);
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20);
  h.writeUInt16LE(1, 22);
  h.writeUInt32LE(sampleRate, 24);
  h.writeUInt32LE(sampleRate * 2, 28);
  h.writeUInt16LE(2, 32);
  h.writeUInt16LE(16, 34);
  h.write("data", 36);
  h.writeUInt32LE(dataLength, 40);
  return h;
}

/**
 * Файл открывается по первому куску (нет звука — нет и пустого файла), после
 * каждого куска в шапке актуальные длины. После close() запись больше не идёт:
 * повторное открытие с "w" стёрло бы уже записанное.
 */
export class EarWavWriter {
  private fd: number | null = null;
  private bytes = 0;
  private closed = false;

  constructor(private readonly filePath: string) {}

  append(pcm: Buffer): void {
    if (this.closed) return;
    const len = pcm.length - (pcm.length % 2);
    if (len <= 0) return;
    if (this.fd === null) {
      this.fd = fs.openSync(this.filePath, "w");
      // Бот пишет от root, хост убирает от dev — как основная запись.
      try { fs.chmodSync(this.filePath, 0o666); } catch { /* */ }
      fs.writeSync(this.fd, wavHeader(0), 0, WAV_HEADER_LEN);
    }
    fs.writeSync(this.fd, pcm, 0, len);
    this.bytes += len;
    this.syncHeader();
  }

  private syncHeader(): void {
    if (this.fd === null) return;
    const h = wavHeader(this.bytes);
    // С позицией → pwrite: курсор дописывания не сдвигается (как у основной записи).
    fs.writeSync(this.fd, h, 4, 4, 4);
    fs.writeSync(this.fd, h, 40, 4, 40);
  }

  close(): void {
    this.closed = true;
    if (this.fd === null) return;
    try { this.syncHeader(); } catch { /* */ }
    try { fs.fsyncSync(this.fd); } catch { /* */ }
    try { fs.closeSync(this.fd); } catch { /* */ }
    this.fd = null;
  }

  get samples(): number {
    return this.bytes / 2;
  }
}
