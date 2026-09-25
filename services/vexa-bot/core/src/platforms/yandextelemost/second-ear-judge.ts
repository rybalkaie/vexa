// Второе ухо бота — чистая логика решений (ISS-60, фаза 4 плана
// telemost-update-resilience, 25.09.2026). Без браузера, звука и файлов: её
// проверяет second-ear.test.ts, а запись с монитора звуковой системы делает
// second-ear.ts.
//
// ЗАЧЕМ. Основная запись берёт звук из дорожек звонка внутри страницы
// (recording.ts). Если Телемост поменяет, как отдаёт звук (так уже было: ISS-20 —
// дорожки умирали и подменялись), основная запись «оглохнет»: люди говорят, а в
// файле тишина. Второе ухо пишет то, что браузер реально ИГРАЕТ в звуковую
// систему контейнера (вывод zoom_sink, запись с его монитора), — этот путь от
// устройства страницы не зависит.
//
// ЧТО РЕШАЕТ ЭТОТ МОДУЛЬ.
//  1. «Второе ухо слышит речь» — не одиночный щелчок или звонок входа, а
//     EAR_SPEECH_MIN_BLOCKS громких кусков по 3 с за EAR_SPEECH_WINDOW_MS. Тогда
//     встреча считается идущей: бот не уходит по «никто не пришёл» и по тишине.
//  2. «Основная запись оглохла» (правило sustained): основная не услышала ничего
//     PRIMARY_DEAF_AFTER_MS подряд, а второе ухо за это же время слышало звук хотя
//     бы PRIMARY_DEAF_MIN_EAR_SHARE времени. Решение липкое — дальше протокол
//     собирается из второго уха (в нём вся встреча с начала записи).
//  3. Итог в конце встречи (finalDecision): короткую встречу правило «минута
//     подряд» может не успеть поймать — тогда второе ухо берётся, если основная
//     услышала меньше половины того, что услышало второе ухо (ear_heard_more).
//     Объявленная глухота отменяется, если второе ухо в итоге услышало МЕНЬШЕ
//     основной (например, само умерло посреди встречи, а основная ожила).
//
// Громкость — RMS 3-секундного куска в долях полной шкалы, тот же порог, что у
// основной записи (RMS_SILENCE_THRESHOLD = 0.003 ≈ −50 дБ; цифровая тишина −91 дБ).

/** Длительность одного куска второго уха и основной записи (оба — 3 с). */
export const EAR_BLOCK_MS = 3_000;
/** Порог «не тишина» — как у основной записи (recording.ts, RMS_SILENCE_THRESHOLD). */
export const EAR_SILENCE_RMS = 0.003;
/** «Второе ухо слышит речь»: столько громких кусков… */
export const EAR_SPEECH_MIN_BLOCKS = 3;
/** …за это окно (≥ 9 с звука за 30 с — не щелчок и не звонок входа). */
export const EAR_SPEECH_WINDOW_MS = 30_000;
/**
 * N из плана: основная молчит столько подряд, пока второе ухо слышит речь, —
 * основная оглохла. 1 минута: вдвое дольше самой длинной законной тишины основной
 * (стартовое ожидание первой дорожки — до 30 с), переподключение дорожек Телемоста
 * — секунды (пересканирование раз в 2 с). Ложное срабатывание почти бесплатно:
 * второе ухо слышит ту же встречу, протокол не хуже. Пропуск дороже: потерянные
 * минуты разговора.
 */
export const PRIMARY_DEAF_AFTER_MS = 60_000;
/** Второе ухо за эту минуту слышало звук хотя бы четверть времени (≥ 15 с). */
export const PRIMARY_DEAF_MIN_EAR_SHARE = 0.25;
/**
 * Меньше 15 с звука за всю встречу — второе ухо ничего не решает (реплика «алло»,
 * звонки входа участников). Столько же требует правило «минута подряд».
 */
export const EAR_MIN_LOUD_MS_TO_USE = 15_000;
/**
 * Второе ухо перестаёт писать файл после стольких минут тишины (как пауза основной
 * записи, CHUNK_SILENCE_PAUSE_MS) и снова пишет с первого громкого куска: иначе файл
 * рос бы всегда, и сторож зависших ботов в сборщике (запись не растёт 30 мин) не
 * увидел бы зависания никогда.
 */
export const EAR_PAUSE_AFTER_SILENCE_MS = 5 * 60_000;
/**
 * Номер куска второго уха в имени файла: `<дата>-<сессия>.chunk0.wav`. Основная
 * запись нумерует куски с 1, поэтому 0 не пересекается ни с одним её куском, а имя
 * `*.chunk<цифры>.wav` уже знают удержание для портала (portal_delivery.
 * session_wav_files) и сторож зависаний (collector). Обычная финализация берёт
 * только куски из meta.recording.chunks[] — второе ухо туда попадает, лишь когда
 * им заменили оглохшую основную запись (без двойного звука).
 */
export const SECOND_EAR_CHUNK_IDX = 0;
/** Вывод звука браузера в контейнере бота (entrypoint.sh). НЕ tts_sink — это микрофон бота. */
export const SECOND_EAR_DEVICE = "zoom_sink";

/** RMS 16-битного PCM (s16le) в долях полной шкалы, 0…1. Пусто → 0. */
export function pcm16Rms(pcm: Uint8Array): number {
  const n = Math.floor(pcm.length / 2);
  if (n <= 0) return 0;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    let v = pcm[2 * i] | (pcm[2 * i + 1] << 8);
    if (v & 0x8000) v -= 0x10000;
    const f = v / 32768;
    sum += f * f;
  }
  return Math.sqrt(sum / n);
}

/**
 * Живая проверка (только тестовый образ, самопроверка в Т-10): основная запись
 * принудительно «оглохла» — её звук обнуляется, второе ухо работает как обычно.
 * Флаг — сырой BOT_CONFIG.telemostForceDeaf (Zod апстрима вырезает незнакомые ключи).
 * По умолчанию выключен; запускатель его никогда не ставит.
 */
export function forceDeafFromRawConfig(raw: any): boolean {
  const v = raw && typeof raw === "object" ? raw.telemostForceDeaf : undefined;
  return v === true || v === 1 || v === "1" || v === "true";
}

/** Писать ли кусок второго уха в файл: пауза после долгой тишины, запись — с первого громкого. */
export function earShouldWrite(lastLoudTs: number | null, startTs: number, now: number, loud: boolean): boolean {
  if (loud) return true;
  return now - (lastLoudTs ?? startTs) < EAR_PAUSE_AFTER_SILENCE_MS;
}

/** Кусок записи в meta.recording.chunks[] (как пишет recording.ts). */
export type ChunkMeta = {
  idx: number;
  wav: string;
  startTs: string | null;
  firstSpeechMs: number | null;
  lastSpeechMs: number | null;
  endTs: string | null;
  durationS: number;
};

/** Что meta говорит финализации о звуке встречи. */
export type AudioSourceView = {
  audioSource: "primary" | "second_ear";
  chunks: ChunkMeta[];
  /** Куски основной записи — только когда звук берётся из второго уха (для разбора). */
  primaryChunks: ChunkMeta[] | null;
  filesWav: string | null;
  firstSpeechMs: number | null;
  lastSpeechMs: number | null;
  /** null — оставить итоги основной записи. */
  audioSamples: number | null;
  audioDurationS: number | null;
};

/**
 * Поля meta о звуке. Пока основная слышит — chunks[] только основной записи: файл
 * второго уха туда НЕ попадает, иначе финализация склеила бы два звука одной
 * встречи. Основная оглохла → chunks[] и files.wav — второе ухо (в нём вся встреча
 * с начала записи), куски основной — в primaryChunks. На пустое второе ухо (ни
 * одного сэмпла) не переключаемся никогда.
 */
export function audioSourceView(a: {
  useEar: boolean;
  mainChunks: ChunkMeta[];
  primaryWav: string | null;
  primaryFirstMs: number | null;
  primaryLastMs: number | null;
  earPath: string;
  ear: { startTs: string | null; endTs: string | null; samples: number; durationS: number } | null;
  earFirstMs: number | null;
  earLastMs: number | null;
}): AudioSourceView {
  if (!a.useEar || !a.ear || !(a.ear.samples > 0)) {
    return {
      audioSource: "primary",
      chunks: a.mainChunks,
      primaryChunks: null,
      filesWav: a.primaryWav,
      firstSpeechMs: a.primaryFirstMs,
      lastSpeechMs: a.primaryLastMs,
      audioSamples: null,
      audioDurationS: null,
    };
  }
  const earChunk: ChunkMeta = {
    idx: SECOND_EAR_CHUNK_IDX,
    wav: a.earPath,
    startTs: a.ear.startTs,
    firstSpeechMs: a.earFirstMs,
    lastSpeechMs: a.earLastMs,
    endTs: a.ear.endTs,
    durationS: Math.round(a.ear.durationS),
  };
  return {
    audioSource: "second_ear",
    chunks: [earChunk],
    primaryChunks: a.mainChunks,
    filesWav: a.earPath,
    firstSpeechMs: a.earFirstMs,
    lastSpeechMs: a.earLastMs,
    audioSamples: a.ear.samples,
    audioDurationS: Math.round(a.ear.durationS),
  };
}

export type PrimaryDeafRule = "sustained" | "ear_heard_more";

export type PrimaryDeafVerdict = {
  rule: PrimaryDeafRule;
  /** С какого момента основная не слышит (epoch ms). */
  since: number;
  detectedAt: number;
  primarySilentMs: number;
  /** Громкое время второго уха: за окно правила (sustained) или за всю встречу. */
  earLoudMs: number;
  primaryLoudMs: number;
  /** Живая проверка: основная заглушена флагом (не поломка). */
  forced: boolean;
};

export type FinalDecision = {
  useEar: boolean;
  verdict: PrimaryDeafVerdict | null;
  /** Глухоту объявляли, но второе ухо в итоге услышало меньше основной. */
  reverted: boolean;
  earLoudMs: number;
  primaryLoudMs: number;
};

export class SecondEarJudge {
  private primaryLoudTotal = 0;
  private earLoudTotal = 0;
  private lastPrimaryLoud: number | null = null;
  /** Метки времени громких кусков второго уха за последнее окно правил. */
  private recentEarLoud: number[] = [];
  private declared: PrimaryDeafVerdict | null = null;
  private firstEarLoud: number | null = null;
  private lastEarLoud: number | null = null;
  private lastEarSpeech: number | null = null;
  private earBlockCount = 0;

  constructor(private readonly startMs: number, private readonly forced = false) {}

  /** Кусок основной записи (3 с) с его RMS. */
  onPrimaryBlock(ts: number, rms: number): void {
    if (rms >= EAR_SILENCE_RMS) {
      this.lastPrimaryLoud = ts;
      this.primaryLoudTotal += EAR_BLOCK_MS;
    }
  }

  /** Кусок второго уха (3 с). Возвращает «громкий ли». */
  onEarBlock(ts: number, rms: number): boolean {
    this.earBlockCount += 1;
    const loud = rms >= EAR_SILENCE_RMS;
    if (loud) {
      this.recentEarLoud.push(ts);
      this.earLoudTotal += EAR_BLOCK_MS;
      if (this.firstEarLoud === null) this.firstEarLoud = ts;
      this.lastEarLoud = ts;
    }
    const keep = Math.max(PRIMARY_DEAF_AFTER_MS, EAR_SPEECH_WINDOW_MS);
    while (this.recentEarLoud.length && this.recentEarLoud[0] <= ts - keep) this.recentEarLoud.shift();
    if (loud && this.earHearsSpeech(ts)) this.lastEarSpeech = ts;
    return loud;
  }

  /** Громкое время второго уха за окно (now − windowMs, now]. */
  earLoudMsWithin(now: number, windowMs: number): number {
    let n = 0;
    for (const t of this.recentEarLoud) if (t > now - windowMs && t <= now) n += 1;
    return n * EAR_BLOCK_MS;
  }

  /** Второе ухо сейчас слышит речь (а не щелчок): ≥ 3 громких куска за 30 с. */
  earHearsSpeech(now: number): boolean {
    return this.earLoudMsWithin(now, EAR_SPEECH_WINDOW_MS) >= EAR_SPEECH_MIN_BLOCKS * EAR_BLOCK_MS;
  }

  /**
   * Правило «минута подряд». Возвращает вердикт ОДИН раз — в момент объявления
   * (дальше он липкий и доступен через `deaf`), иначе null.
   */
  checkPrimaryDeaf(now: number): PrimaryDeafVerdict | null {
    if (this.declared) return null;
    const silentSince = this.lastPrimaryLoud ?? this.startMs;
    const silentMs = now - silentSince;
    if (silentMs < PRIMARY_DEAF_AFTER_MS) return null;
    const earMs = this.earLoudMsWithin(now, PRIMARY_DEAF_AFTER_MS);
    if (earMs < PRIMARY_DEAF_MIN_EAR_SHARE * PRIMARY_DEAF_AFTER_MS) return null;
    // «Оглохла с» — не с последнего звука основной (комната могла молчать), а с
    // первого звука второго уха, которого основная уже не услышала.
    const firstHeard = this.recentEarLoud.find((t) => t > silentSince && t > now - PRIMARY_DEAF_AFTER_MS);
    const since = Math.max(silentSince, (firstHeard ?? now) - EAR_BLOCK_MS);
    this.declared = {
      rule: "sustained",
      since,
      detectedAt: now,
      primarySilentMs: silentMs,
      earLoudMs: earMs,
      primaryLoudMs: this.primaryLoudTotal,
      forced: this.forced,
    };
    return this.declared;
  }

  /** Итог в конце встречи: брать ли второе ухо вместо основной записи. */
  finalDecision(now: number): FinalDecision {
    const ear = this.earLoudTotal;
    const prim = this.primaryLoudTotal;
    const out = (useEar: boolean, reverted = false): FinalDecision => ({
      useEar, verdict: this.declared, reverted, earLoudMs: ear, primaryLoudMs: prim,
    });
    if (ear < EAR_MIN_LOUD_MS_TO_USE) return out(false, this.declared !== null);
    if (this.declared) return ear >= prim ? out(true) : out(false, true);
    // Разрозненные звуки (звонки входа, щелчки) речью не считаются: второе ухо хоть
    // раз должно было «слышать речь» (≥ 3 громких куска за 30 с).
    if (this.lastEarSpeech !== null && prim * 2 < ear) {
      this.declared = {
        rule: "ear_heard_more",
        since: Math.max(this.startMs, (this.firstEarLoud ?? now) - EAR_BLOCK_MS),
        detectedAt: now,
        primarySilentMs: now - (this.lastPrimaryLoud ?? this.startMs),
        earLoudMs: ear,
        primaryLoudMs: prim,
        forced: this.forced,
      };
      return out(true);
    }
    return out(false);
  }

  /** Объявленная глухота основной (липкая) или null. */
  get deaf(): PrimaryDeafVerdict | null {
    return this.declared;
  }

  /** Последний громкий кусок второго уха, когда оно «слышало речь» (для «оглох после начала»). */
  get lastEarSpeechTs(): number | null {
    return this.lastEarSpeech;
  }

  get lastEarLoudTs(): number | null {
    return this.lastEarLoud;
  }

  get firstEarLoudTs(): number | null {
    return this.firstEarLoud;
  }

  get earLoudTotalMs(): number {
    return this.earLoudTotal;
  }

  get primaryLoudTotalMs(): number {
    return this.primaryLoudTotal;
  }

  get earBlocks(): number {
    return this.earBlockCount;
  }
}
