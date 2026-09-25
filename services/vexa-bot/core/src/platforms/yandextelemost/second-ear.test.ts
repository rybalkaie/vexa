/**
 * Второе ухо — решения судьи (ISS-60, фаза 4 плана telemost-update-resilience).
 * Время подаётся руками: куски по 3 с, как у основной записи и второго уха.
 *
 * Run (в образе): node /app/dist/platforms/yandextelemost/second-ear.test.js
 */

import {
  SecondEarJudge,
  pcm16Rms,
  forceDeafFromRawConfig,
  earShouldWrite,
  audioSourceView,
  ChunkMeta,
  EAR_BLOCK_MS,
  EAR_SPEECH_MIN_BLOCKS,
  EAR_PAUSE_AFTER_SILENCE_MS,
  PRIMARY_DEAF_AFTER_MS,
  EAR_MIN_LOUD_MS_TO_USE,
  SECOND_EAR_CHUNK_IDX,
  SECOND_EAR_DEVICE,
} from "./second-ear-judge";
import { EarWavWriter, WAV_HEADER_LEN } from "./second-ear-wav";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

let passed = 0;
let failed = 0;

function expect(name: string, actual: any, expected: any) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    console.log(`  PASS  ${name}`);
    passed++;
  } else {
    console.log(`  FAIL  ${name}\n        expected: ${e}\n        actual:   ${a}`);
    failed++;
  }
}

const T0 = 1_760_000_000_000;
const LOUD = 0.05; // ≈ −26 дБ — голос
const QUIET = 0.0001; // ≈ −80 дБ — тишина/шум

/** Подать куски с fromSec до toSec (не включая) — оба уха, по 3 с. */
function feed(
  j: SecondEarJudge,
  fromSec: number,
  toSec: number,
  primary: (s: number) => number | null,
  ear: (s: number) => number | null,
): void {
  for (let s = fromSec; s < toSec; s += EAR_BLOCK_MS / 1000) {
    const ts = T0 + s * 1000;
    const p = primary(s);
    const e = ear(s);
    if (p !== null) j.onPrimaryBlock(ts, p);
    if (e !== null) j.onEarBlock(ts, e);
  }
}

/** Первый момент (с шагом 3 с), когда правило «минута подряд» объявило глухоту. */
function detectAt(j: SecondEarJudge, fromSec: number, toSec: number,
  primary: (s: number) => number | null, ear: (s: number) => number | null): number | null {
  for (let s = fromSec; s < toSec; s += 3) {
    feed(j, s, s + 3, primary, ear);
    if (j.checkPrimaryDeaf(T0 + s * 1000)) return s;
  }
  return null;
}

console.log("Константы (N и пороги из журнала решений)");
expect("N = 1 минута", PRIMARY_DEAF_AFTER_MS, 60_000);
expect("речь = 3 куска по 3 с", EAR_SPEECH_MIN_BLOCKS * EAR_BLOCK_MS, 9_000);
expect("минимум звука второго уха — 15 с", EAR_MIN_LOUD_MS_TO_USE, 15_000);
expect("кусок второго уха — chunk0 (основная нумерует с 1)", SECOND_EAR_CHUNK_IDX, 0);
expect("слушаем вывод браузера zoom_sink, не микрофон бота", SECOND_EAR_DEVICE, "zoom_sink");

console.log("pcm16Rms — громкость 16-битного звука");
expect("пусто", pcm16Rms(new Uint8Array(0)), 0);
expect("тишина", pcm16Rms(new Uint8Array(4800)), 0);
{
  const half = new Uint8Array(8); // 4 сэмпла ±16384 (0,5 полной шкалы)
  const v = [16384, -16384, 16384, -16384];
  v.forEach((x, i) => { const u = x & 0xffff; half[2 * i] = u & 0xff; half[2 * i + 1] = u >> 8; });
  expect("±0,5 полной шкалы → 0,5", Math.round(pcm16Rms(half) * 1000) / 1000, 0.5);
  expect("нечётный байт в конце не мешает", Math.round(pcm16Rms(new Uint8Array([...half, 7])) * 1000) / 1000, 0.5);
}

console.log("forceDeafFromRawConfig — флаг живой проверки (по умолчанию выключен)");
for (const v of [true, 1, "1", "true"]) expect(`telemostForceDeaf=${JSON.stringify(v)} → вкл`, forceDeafFromRawConfig({ telemostForceDeaf: v }), true);
for (const v of [false, 0, "0", "false", "yes", null, undefined]) {
  expect(`telemostForceDeaf=${JSON.stringify(v)} → выкл`, forceDeafFromRawConfig({ telemostForceDeaf: v }), false);
}
expect("без поля → выкл", forceDeafFromRawConfig({ platform: "yandex_telemost" }), false);
expect("не объект → выкл", forceDeafFromRawConfig(null), false);

console.log("earShouldWrite — файл второго уха встаёт на паузу после 5 мин тишины");
expect("громкий кусок — пишем", earShouldWrite(null, T0, T0 + 3_600_000, true), true);
expect("тишина с начала, 10 с — пишем", earShouldWrite(null, T0, T0 + 10_000, false), true);
expect("тишина 4:59 после звука — пишем", earShouldWrite(T0, T0, T0 + EAR_PAUSE_AFTER_SILENCE_MS - 1000, false), true);
expect("тишина 5:00 после звука — пауза", earShouldWrite(T0, T0, T0 + EAR_PAUSE_AFTER_SILENCE_MS, false), false);

console.log("earHearsSpeech — речь, а не щелчок или звонок входа");
{
  const j = new SecondEarJudge(T0);
  j.onEarBlock(T0 + 3000, LOUD);
  j.onEarBlock(T0 + 6000, LOUD);
  expect("2 громких куска — ещё не речь", j.earHearsSpeech(T0 + 6000), false);
  j.onEarBlock(T0 + 9000, LOUD);
  expect("3 громких куска подряд — речь", j.earHearsSpeech(T0 + 9000), true);
  expect("момент речи запомнен", j.lastEarSpeechTs, T0 + 9000);
  expect("через 40 с тишины — уже не речь", j.earHearsSpeech(T0 + 49_000), false);
}
{
  const j = new SecondEarJudge(T0);
  for (const s of [3, 18, 33]) j.onEarBlock(T0 + s * 1000, LOUD);
  expect("3 звонка входа за 30+ с (редко) — не речь", j.earHearsSpeech(T0 + 33_000), false);
  expect("и момента речи нет", j.lastEarSpeechTs, null);
}

console.log("Правило «минута подряд» (sustained)");
{
  // Основная молчит с начала; второе ухо слышит фразу диктора 6…27 с.
  const j = new SecondEarJudge(T0);
  const at = detectAt(j, 0, 120, () => QUIET, (s) => (s >= 6 && s < 27 ? LOUD : QUIET));
  expect("объявлено ровно на 60-й секунде, не раньше", at, 60);
  const v = j.deaf;
  expect("правило sustained", v && v.rule, "sustained");
  expect("«оглохла с» — с первого звука второго уха, а не с начала записи", v && (v.since - T0) / 1000, 3);
  expect("не принудительно", v && v.forced, false);
  expect("вердикт выдаётся один раз", j.checkPrimaryDeaf(T0 + 63_000), null);
  const f = j.finalDecision(T0 + 90_000);
  expect("итог: берём второе ухо", [f.useEar, f.reverted], [true, false]);
}
{
  const j = new SecondEarJudge(T0, true);
  detectAt(j, 0, 90, () => 0, (s) => (s >= 3 && s < 33 ? LOUD : QUIET));
  expect("флаг живой проверки попадает в вердикт", j.deaf && j.deaf.forced, true);
}
{
  // Основная слышит встречу — второе ухо ничего не меняет.
  const j = new SecondEarJudge(T0);
  const at = detectAt(j, 0, 600, () => LOUD, () => LOUD);
  expect("основная слышит — глухоты нет", at, null);
  const f = j.finalDecision(T0 + 600_000);
  expect("итог: основная", f.useEar, false);
}
{
  // Основная молчит, второе ухо слышит только звонки входа (2 куска в минуту).
  const j = new SecondEarJudge(T0);
  const at = detectAt(j, 0, 240, () => QUIET, (s) => (s % 60 === 0 || s % 60 === 3 ? LOUD : QUIET));
  expect("звонки входа — не глухота основной", at, null);
  const f = j.finalDecision(T0 + 240_000);
  expect("итог: основная (разрозненные звуки речью не считаются)", f.useEar, false);
}
{
  // Сбой основной на 45 с при речи — меньше минуты, основная потом слышит.
  const j = new SecondEarJudge(T0);
  const at = detectAt(j, 0, 300, (s) => (s >= 60 && s < 105 ? QUIET : LOUD), () => LOUD);
  expect("провал основной 45 с — не глухота (N = 1 мин)", at, null);
  expect("итог: основная", j.finalDecision(T0 + 300_000).useEar, false);
}
{
  // Основная оглохла посреди встречи на 3 минуты.
  const j = new SecondEarJudge(T0);
  const at = detectAt(j, 0, 600, (s) => (s >= 120 && s < 300 ? QUIET : LOUD), () => LOUD);
  expect("последний звук основной на 117-й с → объявлено ровно через минуту, на 177-й", at, 177);
  expect("«оглохла с» ≈ 117-я с", j.deaf && (j.deaf.since - T0) / 1000, 117);
  expect("итог: второе ухо (в нём и провал, и остальное)", j.finalDecision(T0 + 600_000).useEar, true);
}
{
  // Основная молчит минуту, но и в комнате тишина — ухо слышит только 12 с.
  const j = new SecondEarJudge(T0);
  const at = detectAt(j, 0, 180, () => QUIET, (s) => (s >= 30 && s < 42 ? LOUD : QUIET));
  expect("12 с звука за минуту (< четверти) — не глухота", at, null);
}

console.log("Итог в конце встречи");
{
  // Короткая встреча 45 с: основная ничего не услышала, второе ухо — 21 с речи.
  const j = new SecondEarJudge(T0);
  const at = detectAt(j, 0, 45, () => QUIET, (s) => (s >= 6 && s < 27 ? LOUD : QUIET));
  expect("«минута подряд» не успела", at, null);
  const f = j.finalDecision(T0 + 45_000);
  expect("итог: второе ухо по правилу ear_heard_more", [f.useEar, f.verdict && f.verdict.rule], [true, "ear_heard_more"]);
}
{
  // Один щелчок в основной не спасает: основная 3 с против 21 с второго уха.
  const j = new SecondEarJudge(T0);
  detectAt(j, 0, 45, (s) => (s === 3 ? LOUD : QUIET), (s) => (s >= 6 && s < 27 ? LOUD : QUIET));
  expect("щелчок в основной — итог всё равно второе ухо", j.finalDecision(T0 + 45_000).useEar, true);
}
{
  // Второе ухо слышало только 12 с — мало, чтобы решать.
  const j = new SecondEarJudge(T0);
  detectAt(j, 0, 45, () => QUIET, (s) => (s >= 6 && s < 18 ? LOUD : QUIET));
  expect("12 с второго уха — итог основная", j.finalDecision(T0 + 45_000).useEar, false);
}
{
  // Глухоту объявили, а потом второе ухо умерло (куски не приходят), основная ожила.
  const j = new SecondEarJudge(T0);
  detectAt(j, 0, 90, () => QUIET, () => LOUD);
  expect("глухота объявлена", j.deaf !== null, true);
  detectAt(j, 90, 1200, () => LOUD, () => null);
  const f = j.finalDecision(T0 + 1_200_000);
  expect("второе ухо в итоге услышало меньше основной → откат на основную", [f.useEar, f.reverted], [false, true]);
}
{
  // Основная пропустила больше половины (не подряд минуту, а урывками).
  const j = new SecondEarJudge(T0);
  const at = detectAt(j, 0, 600, (s) => (Math.floor(s / 30) % 3 === 0 ? LOUD : QUIET), () => LOUD);
  expect("урывки по 60 с тишины со звуком в ухе ловит «минута подряд»", at !== null, true);
}
{
  const j = new SecondEarJudge(T0);
  feed(j, 0, 600, (s) => (s % 9 === 0 ? LOUD : QUIET), () => LOUD);
  const f = j.finalDecision(T0 + 600_000);
  expect("основная слышит треть — итог второе ухо (ear_heard_more)", f.useEar, true);
}

console.log("audioSourceView — что meta говорит финализации о звуке");
{
  const main: ChunkMeta[] = [
    { idx: 1, wav: "/transcripts/2026-09-25-tm-1.chunk1.wav", startTs: "a", firstSpeechMs: 10, lastSpeechMs: 20, endTs: "b", durationS: 30 },
    { idx: 2, wav: "/transcripts/2026-09-25-tm-1.chunk2.wav", startTs: "c", firstSpeechMs: null, lastSpeechMs: null, endTs: "d", durationS: 5 },
  ];
  const earPath = "/transcripts/2026-09-25-tm-1.chunk0.wav";
  const ear = { startTs: "s", endTs: "e", samples: 16000 * 90, durationS: 90.04 };
  const base = {
    mainChunks: main, primaryWav: main[0].wav, primaryFirstMs: 10, primaryLastMs: 20,
    earPath, ear, earFirstMs: 1000, earLastMs: 61000,
  };
  const p = audioSourceView({ ...base, useEar: false });
  expect("основная слышит → primary", p.audioSource, "primary");
  expect("основная слышит → в chunks только основная (без двойного звука)", p.chunks.map((c) => c.idx), [1, 2]);
  expect("основная слышит → files.wav основной", p.filesWav, main[0].wav);
  expect("основная слышит → primaryChunks нет", p.primaryChunks, null);
  expect("основная слышит → итоги аудио основной", [p.audioSamples, p.audioDurationS], [null, null]);
  const e = audioSourceView({ ...base, useEar: true });
  expect("оглохла → second_ear", e.audioSource, "second_ear");
  expect("оглохла → chunks — только второе ухо (idx 0)", e.chunks.map((c) => [c.idx, c.wav]), [[0, earPath]]);
  expect("оглохла → files.wav второго уха", e.filesWav, earPath);
  expect("оглохла → куски основной сохранены для разбора", e.primaryChunks && e.primaryChunks.length, 2);
  expect("оглохла → границы речи второго уха", [e.firstSpeechMs, e.lastSpeechMs, e.chunks[0].durationS], [1000, 61000, 90]);
  expect("оглохла → итоги аудио второго уха", [e.audioSamples, e.audioDurationS], [16000 * 90, 90]);
  expect("пустое второе ухо — не переключаемся", audioSourceView({ ...base, useEar: true, ear: { ...ear, samples: 0 } }).audioSource, "primary");
  expect("второго уха нет — не переключаемся", audioSourceView({ ...base, useEar: true, ear: null }).audioSource, "primary");
}

console.log("EarWavWriter — файл второго уха валиден на диске в любой момент");
{
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "second-ear-"));
  const file = path.join(dir, "2026-09-25-tm-1.chunk0.wav");
  const w = new EarWavWriter(file);
  w.append(Buffer.alloc(0));
  expect("пустой кусок — файла нет", fs.existsSync(file), false);
  w.append(Buffer.alloc(96_000, 1));
  w.append(Buffer.alloc(96_001, 1)); // нечётный байт отбрасывается
  const mid = fs.readFileSync(file);
  expect("до закрытия: размер = шапка + 2 куска", mid.length, WAV_HEADER_LEN + 192_000);
  expect("до закрытия: RIFF-длина в шапке актуальна", mid.readUInt32LE(4), 36 + 192_000);
  expect("до закрытия: длина данных в шапке актуальна", mid.readUInt32LE(40), 192_000);
  expect("сэмплов", w.samples, 96_000);
  w.close();
  w.append(Buffer.alloc(96_000, 1));
  const fin = fs.readFileSync(file);
  expect("после закрытия запись не идёт (и не стирает файл)", fin.length, WAV_HEADER_LEN + 192_000);
  expect("шапка: RIFF/WAVE/data", [fin.toString("ascii", 0, 4), fin.toString("ascii", 8, 12), fin.toString("ascii", 36, 40)],
    ["RIFF", "WAVE", "data"]);
  expect("16 кГц моно 16 бит", [fin.readUInt32LE(24), fin.readUInt16LE(22), fin.readUInt16LE(34)], [16000, 1, 16]);
  fs.rmSync(dir, { recursive: true, force: true });
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
