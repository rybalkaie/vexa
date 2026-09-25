'use strict';
/*
 * Диктор самопроверки бота (ISS-60, фаза 2, 25.09.2026).
 *
 * ЗАЧЕМ. Самопроверка (scripts/notary/canary.py) каждое утро и после обновления
 * Телемоста заводит в запасную переговорку Т-10 бота и «Тест-диктора». Диктор
 * говорит фразу с кодовым словом, бот её записывает, самопроверка ищет слово в
 * расшифровке. Если бот не зашёл / не слышит / пишет тишину — мы узнаём об этом
 * до настоящей встречи, а не после.
 *
 * КАК. Чистый JS без сборки; запускается в контейнере ТОГО ЖЕ образа, что
 * проверяется (speaker.sh поднимает экран и PulseAudio как entrypoint.sh бота).
 * Вход — модулем входа из образа (`/app/dist/platforms/yandextelemost/join.js`),
 * тем же кодом, что у бота: если Телемост поменял вход, диктор не войдёт так же,
 * как не вошёл бы бот, и самопроверка покажет это красным с причиной.
 *
 * Параметры — env:
 *   SPEAKER_CONFIG  JSON {meetingUrl, name, botName, phraseFile, maxSeconds, plays}
 *   BOT_CONFIG      JSON {sessionUid: "<run_id>-speaker", ...} — по нему
 *                   join-status.js / crashkit.js из образа пишут статус входа и
 *                   аварийный снимок диктора в его собственные файлы.
 *
 * Протокол с canary.py — файлы в /transcripts/.canary/:
 *   speaker.json  пишет диктор (атомарно): phase ∈ joining | alone | busy |
 *                 speaking | spoke | left | failed, others, reason, updated_at…
 *   cmd.json      пишет canary.py: {"action": "speak"|"leave", "seq": N}
 *
 * 🔴 ЧУЖИЕ ЛЮДИ. Сразу после входа диктор считает участников: кто-то кроме него
 * (или посчитать не удалось) → `busy` и немедленный выход, ни слова. Пока он в
 * звонке, пересчёт каждые ~3 с: разрешены только он и бот; больше → `busy`,
 * голос глушится, выход. Незнакомое имя на плитке (не диктор и не бот) — тоже
 * `busy`, в ожидании — после двух пересчётов подряд: по одному числу человека,
 * вошедшего раньше бота, от бота не отличить (`idleVerdict`).
 *
 * 🔴 МОЛЧАНИЕ ПО УМОЛЧАНИЮ. Голос идёт через PulseAudio (tts_sink → virtual_mic),
 * оба заглушены с запуска (speaker.sh). Интерфейсный микрофон Телемоста 3.0
 * после входа включён у всех (живая разведка 25.09) — на него не полагаемся:
 * пока нет команды speak, в virtual_mic физически тишина.
 *
 * 🔴 ССЫЛКА КОМНАТЫ в лог диктора не пишется (join.js из образа пишет её в
 * свой шаг join_start — canary.py вычищает её, когда сохраняет логи).
 */

const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { createRequire } = require('module');

const APP_DIR = process.env.SPEAKER_APP_DIR || '/app';
const DIST = path.join(APP_DIR, 'dist');
const TELEMOST = path.join(DIST, 'platforms', 'yandextelemost');
// Пакеты (playwright-extra, stealth) лежат в /app/node_modules — ищем их от
// каталога образа, а не от /canary, где лежит этот файл.
const appRequire = createRequire(path.join(DIST, 'index.js'));

const TRANSCRIPT_DIR = process.env.TELEMOST_TRANSCRIPT_DIR || '/transcripts';
const CANARY_DIR = path.join(TRANSCRIPT_DIR, '.canary');
const STATE_FILE = path.join(CANARY_DIR, 'speaker.json');
const CMD_FILE = path.join(CANARY_DIR, 'cmd.json');
const KIT_DIR = process.env.SPEAKER_KIT_DIR || '/canary';

const TELEMOST_HOST_RE = /^(?:[a-z0-9-]+\.)*telemost(?:\.360)?\.yandex\.ru$/i;
const PHRASE_FILE_RE = /^[a-z0-9-]+\.wav$/;

// Разрешено «я + бот»: посторонних — ноль.
const ALLOWED_OTHERS = 1;
// Сколько пересчётов подряд «не смог посчитать» терпим, прежде чем уйти.
const MAX_UNKNOWN_STREAK = 3;
// Незнакомое имя на плитке в ожидании — сколько пересчётов подряд, прежде чем
// уйти. Два, а не один: мигнувшая плитка входящего бота не выгоняет диктора.
const FOREIGN_NAME_STREAK = 2;

// ---------------------------------------------------------------------------
// Мелочи
// ---------------------------------------------------------------------------

function say(msg, extra) {
  const line = { ts: new Date().toISOString(), src: 'canary-speaker', msg };
  if (extra && typeof extra === 'object') Object.assign(line, extra);
  try { console.log(JSON.stringify(line)); } catch { /* */ }
}

function shortErr(e, max = 160) {
  const s = String((e && e.message) || e || '').replace(/\s+/g, ' ').trim();
  // Ссылки не пишем никуда: в тексте ошибки Playwright бывает адрес страницы.
  const clean = s.replace(/https?:\/\/\S+/g, '<url>');
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function readConfig() {
  let raw;
  try {
    raw = JSON.parse(process.env.SPEAKER_CONFIG || '{}');
  } catch {
    throw Object.assign(new Error('SPEAKER_CONFIG is not JSON'), { reason: 'bad_config' });
  }
  const bad = (what) => Object.assign(new Error(`SPEAKER_CONFIG: ${what}`), { reason: 'bad_config' });
  let url;
  try {
    url = new URL(String(raw.meetingUrl || ''));
  } catch {
    throw bad('meetingUrl');
  }
  // Браузер диктора открывает только Телемост — никаких других страниц.
  if (url.protocol !== 'https:' || !TELEMOST_HOST_RE.test(url.hostname)) throw bad('meetingUrl host');
  const phraseFile = String(raw.phraseFile || '');
  if (!PHRASE_FILE_RE.test(phraseFile)) throw bad('phraseFile');
  const clamp = (v, lo, hi, dflt) => {
    const n = Number(v);
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, Math.round(n))) : dflt;
  };
  const name = String(raw.name || 'Тест-диктор').slice(0, 40);
  return {
    meetingUrl: url.toString(),
    name,
    botName: String(raw.botName || 'Бот — протокол встречи').slice(0, 60),
    phraseFile,
    maxSeconds: clamp(raw.maxSeconds, 60, 1800, 360),
    plays: clamp(raw.plays, 1, 6, 3),
    checkEverySec: clamp(raw.checkEverySec, 2, 10, 3),
    admissionTimeoutSec: clamp(raw.admissionTimeoutSec, 30, 300, 120),
    // Перед голосом и во время него: в звонке есть имя, не похожее ни на
    // диктора, ни на бота → уходим. Выключается `strictNames: false`, если живая
    // проверка покажет, что Телемост подписывает кого-то из нас иначе.
    strictNames: raw.strictNames !== false,
  };
}

// ---------------------------------------------------------------------------
// Протокол с canary.py
// ---------------------------------------------------------------------------

const TERMINAL = new Set(['busy', 'failed', 'left']);
let state = { history: [] };

/** Записать фазу диктора атомарно (tmp + rename). Никогда не бросает. */
function writeState(phase, extra = {}) {
  const now = new Date().toISOString();
  state = {
    ...state,
    ...extra,
    phase,
    updated_at: now,
    history: [...(state.history || []), { phase, at: now }].slice(-20),
  };
  try {
    fs.mkdirSync(CANARY_DIR, { recursive: true });
    const tmp = `${STATE_FILE}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(state, null, 2), 'utf8');
    // 0666 — пользователь сервера читает и убирает (контейнер пишет от root).
    try { fs.chmodSync(tmp, 0o666); } catch { /* */ }
    fs.renameSync(tmp, STATE_FILE);
  } catch (e) {
    say('state_write_failed', { error: shortErr(e) });
  }
  const loggable = {};
  for (const k of ['reason', 'others', 'count_method', 'step', 'mic_ui', 'plays_done']) {
    if (extra[k] !== undefined) loggable[k] = extra[k];
  }
  say('phase', { phase, ...loggable });
}

function isTerminal() {
  return TERMINAL.has(state.phase);
}

/** Команда от canary.py: {action, seq} или null. */
function readCmd() {
  try {
    const c = JSON.parse(fs.readFileSync(CMD_FILE, 'utf8'));
    if (c && typeof c.action === 'string' && Number.isInteger(c.seq)) return c;
  } catch { /* файла нет или пишется */ }
  return null;
}

// ---------------------------------------------------------------------------
// Звук (PulseAudio, как голосовой агент Vexa)
// ---------------------------------------------------------------------------

function run(cmd, args, timeoutMs = 20000) {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout: timeoutMs }, (err, _stdout, stderr) => {
      resolve({ ok: !err, err: err ? shortErr(stderr || err) : '' });
    });
  });
}

/** Заглушить (true) или открыть (false) виртуальный микрофон и его «колонку». */
async function pulseMute(muted) {
  const v = muted ? '1' : '0';
  if (muted) {
    const a = await run('pactl', ['set-sink-mute', 'tts_sink', v]);
    const b = await run('pactl', ['set-source-mute', 'virtual_mic', v]);
    return a.ok && b.ok;
  }
  const b = await run('pactl', ['set-source-mute', 'virtual_mic', v]);
  const a = await run('pactl', ['set-sink-mute', 'tts_sink', v]);
  return a.ok && b.ok;
}

// ---------------------------------------------------------------------------
// ======== Счётчик участников — ПРАВИТЬ ЗДЕСЬ после живой разведки ========
// Разведка оркестратора 25.09.2026 (Телемост 3.0, Т-10, два тестовых участника):
//  а) главное окно: элемент с aria-label «Открыть экран звонка», его текст вида
//     «10\n2 участника\n•\n00:36» → число перед «участник»;
//  б) вложенное окно звонка (адрес содержит /private-join/): число элементов
//     [class*="participantName_"] (запасной — плитки по data-testid, дали ровно
//     1 при одном и 2 при двух участниках).
//  Источники расходятся → берём БОЛЬШЕЕ (безопасная сторона). Ни одного → null.
//  window.__vexaRemoteAudioTracks в главном окне = 0 даже при двоих — не годится.
// ---------------------------------------------------------------------------

const COUNT = {
  callButtonAria: 'Открыть экран звонка',
  countTextRe: '(\\d{1,3})\\s+участник',
  callFrameMarker: '/private-join/',
  nameSelector: '[class*="participantName_"]',
  tileFallbackSelector: '[data-testid*="participant"]:not([data-testid="participants-button"])',
};

function normName(s) {
  return String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/[^a-zа-я0-9]+/g, '');
}

/**
 * Сколько людей в звонке (включая самого диктора) → {count, method, diag}.
 * count = null — посчитать не удалось. Имён наружу не отдаёт: в diag только
 * число «незнакомых» имён (не диктор и не бот) — для разведки.
 */
async function countParticipants(page, ourNames) {
  let fromButton = null;
  let fromTiles = null;
  let foreign = null;
  for (const fr of page.frames()) {
    let r = null;
    try {
      r = await fr.evaluate((c) => {
        const out = { button: null, tiles: null, names: null };
        const re = new RegExp(c.countTextRe, 'i');
        const labelled = Array.from(document.querySelectorAll('[aria-label]'))
          .filter((el) => el.getAttribute('aria-label') === c.callButtonAria);
        for (const el of labelled) {
          const m = ((el.innerText || el.textContent || '') + '').match(re);
          if (m) out.button = Math.max(out.button || 0, Number(m[1]));
        }
        if (location.href.includes(c.callFrameMarker)) {
          const named = Array.from(document.querySelectorAll(c.nameSelector));
          if (named.length) {
            out.tiles = named.length;
            out.names = named.map((e) => ((e.innerText || e.textContent || '') + '').trim()).slice(0, 30);
          } else {
            const tiles = document.querySelectorAll(c.tileFallbackSelector).length;
            if (tiles) out.tiles = tiles;
          }
        }
        return out;
      }, COUNT);
    } catch {
      continue; // окно ушло или чужой origin
    }
    if (!r) continue;
    if (Number.isInteger(r.button)) fromButton = Math.max(fromButton || 0, r.button);
    if (Number.isInteger(r.tiles)) fromTiles = Math.max(fromTiles || 0, r.tiles);
    if (Array.isArray(r.names)) {
      const ours = ourNames.map(normName).filter(Boolean).map((n) => n.slice(0, 6));
      foreign = r.names.filter((n) => {
        const x = normName(n);
        return x && !ours.some((o) => x.startsWith(o));
      }).length;
    }
  }
  const counts = [fromButton, fromTiles].filter((n) => Number.isInteger(n) && n >= 1 && n <= 500);
  const count = counts.length ? Math.max(...counts) : null;
  let method = 'none';
  if (count !== null) {
    method = fromButton === count && fromTiles === count ? 'button+tiles'
      : fromButton === count ? 'button' : 'tiles';
  }
  return { count, method, diag: { button: fromButton, tiles: fromTiles, foreign_names: foreign } };
}

/** Несколько замеров подряд → наибольшее число (UI звонка устаканивается не сразу). */
async function sampleCount(page, ourNames, samples = 3, gapMs = 1500) {
  let best = { count: null, method: 'none', diag: {} };
  for (let i = 0; i < samples; i++) {
    const r = await countParticipants(page, ourNames);
    if (r.count !== null && (best.count === null || r.count > best.count)) best = r;
    else if (best.count === null) best = r;
    if (i < samples - 1) await sleep(gapMs);
  }
  return best;
}

// ---------------------------------------------------------------------------
// Интерфейс звонка: микрофон и выход (разведка 25.09, Телемост 3.0)
// ---------------------------------------------------------------------------

const UI = {
  micButton: '[data-testid="mute-audio"]', // вложенное окно звонка
  micOnAria: 'Выключить микрофон',        // кнопка предлагает выключить → микрофон включён
  micOffAria: 'Включить микрофон',
  leaveSelectors: [
    '[data-testid="end-call-alt-button"]', // 3.0: во вложенном окне /private-join/
    'button[aria-label="Завершить звонок"]',
    'button[aria-label*="Выйти из встречи"]',
    'button[aria-label*="Выйти"]',
  ],
};

function callFramesFirst(page) {
  const frames = page.frames();
  const call = frames.filter((f) => f.url().includes(COUNT.callFrameMarker));
  return [...call, ...frames.filter((f) => !call.includes(f))];
}

/** Состояние интерфейсного микрофона: 'on' | 'off' | null (кнопку не нашли). */
async function micUiState(page) {
  for (const fr of callFramesFirst(page)) {
    try {
      const st = await fr.evaluate((u) => {
        const vis = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const byTid = Array.from(document.querySelectorAll(u.micButton)).filter(vis);
        const byAria = Array.from(document.querySelectorAll('button[aria-label]')).filter(vis)
          .filter((b) => [u.micOnAria, u.micOffAria].includes(b.getAttribute('aria-label')));
        for (const b of [...byTid, ...byAria]) {
          const a = b.getAttribute('aria-label') || '';
          if (a === u.micOnAria) return 'on';
          if (a === u.micOffAria) return 'off';
        }
        return null;
      }, UI);
      if (st) return { state: st, frame: fr };
    } catch { /* следующее окно */ }
  }
  return { state: null, frame: null };
}

/**
 * Убедиться, что интерфейсный микрофон включён. Звук всё равно решает PulseAudio:
 * это лишь «не мешать» — если Телемост держит микрофон выключенным, фраза не уйдёт.
 * Один Playwright-клик; не сработал и кнопка всё ещё «Включить» — ровно один
 * JS-клик (повторный Playwright-клик мог бы переключить обратно).
 */
async function ensureMicOn(page) {
  const first = await micUiState(page);
  if (first.state !== 'off') return first.state; // 'on' или null (не видим кнопку)
  const clickIn = async (fr, js) => {
    const sel = `${UI.micButton}[aria-label="${UI.micOffAria}"], button[aria-label="${UI.micOffAria}"]`;
    if (js) {
      await fr.evaluate((s) => { const b = document.querySelector(s); if (b) b.click(); }, sel).catch(() => {});
    } else {
      await fr.locator(sel).first().click({ timeout: 2000 }).catch(() => {});
    }
  };
  await clickIn(first.frame, false);
  await sleep(800);
  let now = await micUiState(page);
  if (now.state === 'off' && now.frame) {
    await clickIn(now.frame, true);
    await sleep(800);
    now = await micUiState(page);
  }
  return now.state;
}

/** Нажать первую видимую кнопку выхода в любом окне. */
async function clickLeave(page) {
  for (const fr of callFramesFirst(page)) {
    for (const sel of UI.leaveSelectors) {
      try {
        const loc = fr.locator(sel).first();
        if (await loc.isVisible({ timeout: 300 })) {
          await loc.click({ timeout: 2000 });
          say('leave_clicked', { selector: sel });
          return true;
        }
      } catch { /* дальше */ }
    }
  }
  say('leave_button_not_found');
  return false;
}

// ---------------------------------------------------------------------------
// Главный ход
// ---------------------------------------------------------------------------

let browser = null;
let page = null;
let playing = null; // процесс paplay, если идёт фраза

async function shutdown(code) {
  try { if (playing) playing.kill('SIGKILL'); } catch { /* */ }
  await pulseMute(true).catch(() => {});
  if (page && !page.isClosed()) {
    await Promise.race([clickLeave(page).catch(() => false), sleep(5000)]);
    await sleep(1500);
  }
  if (browser) await Promise.race([browser.close().catch(() => {}), sleep(10000)]);
  process.exit(code);
}

/** Посторонние в звонке → `busy` (фаза пишется ДО выхода: самопроверка реагирует сразу). */
async function leaveBusy(reason, counted) {
  try { if (playing) playing.kill('SIGKILL'); } catch { /* */ }
  await pulseMute(true).catch(() => {});
  writeState('busy', {
    reason,
    others: counted && counted.count !== null ? counted.count - 1 : null,
    count_method: counted ? counted.method : 'none',
    count_diag: counted ? counted.diag : null,
  });
  await shutdown(0);
}

function othersOf(counted) {
  return counted.count === null ? null : counted.count - 1;
}

/** Проиграть фразу через tts_sink, пересчитывая участников каждые ~3 с. */
function playOnce(file) {
  return new Promise((resolve) => {
    playing = execFile('paplay', ['--device=tts_sink', file], { timeout: 30000 }, (err) => {
      playing = null;
      resolve(!err);
    });
  });
}

/** Во время голоса: посторонний по числу или (строго) по имени. */
function strangerWhileSpeaking(cfg, c) {
  if (c.count !== null && othersOf(c) > ALLOWED_OTHERS) return 'strangers';
  if (cfg.strictNames && c.diag && Number(c.diag.foreign_names) > 0) return 'foreign_name';
  return null;
}

/**
 * Пересчёт в ожидании команды (до голоса и после): → {busy, unknownStreak, foreignStreak}.
 * busy — причина уйти или null. Чистая функция: её проверяет tests/test_canary_iss60.py.
 *
 * По одному числу постороннего, вошедшего раньше бота, не отличить от бота
 * (ALLOWED_OTHERS = 1) — самопроверка запустила бы бота в комнату с человеком.
 * По имени на плитке — можно: «ни диктор, ни бот» два пересчёта подряд → уходим
 * (цикл5 фазы 2, Н5). Выключается тем же `strictNames: false`.
 */
function idleVerdict(cfg, c, streaks) {
  let unknownStreak = (streaks && streaks.unknownStreak) || 0;
  let foreignStreak = (streaks && streaks.foreignStreak) || 0;
  if (c.count === null) {
    unknownStreak += 1;
    if (unknownStreak >= MAX_UNKNOWN_STREAK) return { busy: 'count_unknown', unknownStreak, foreignStreak };
  } else {
    unknownStreak = 0;
    if (othersOf(c) > ALLOWED_OTHERS) return { busy: 'strangers', unknownStreak, foreignStreak };
  }
  if (cfg.strictNames && c.diag && Number(c.diag.foreign_names) > 0) {
    foreignStreak += 1;
    if (foreignStreak >= FOREIGN_NAME_STREAK) return { busy: 'foreign_name', unknownStreak, foreignStreak };
  } else {
    foreignStreak = 0;
  }
  return { busy: null, unknownStreak, foreignStreak };
}

async function speak(cfg, phrasePath, ourNames) {
  // Перед голосом — ещё раз: в комнате только мы с ботом.
  const before = await countParticipants(page, ourNames);
  if (before.count === null) return leaveBusy('count_unknown', before);
  const why = strangerWhileSpeaking(cfg, before);
  if (why) return leaveBusy(why, before);

  const micUi = await ensureMicOn(page);
  writeState('speaking', { others: othersOf(before), count_method: before.method, mic_ui: micUi });
  if (!(await pulseMute(false))) {
    await pulseMute(true).catch(() => {});
    writeState('failed', { reason: 'pulse_unavailable', step: 'speak' });
    return shutdown(1);
  }
  let played = 0;
  for (let i = 0; i < cfg.plays; i++) {
    const done = playOnce(phrasePath);
    let finished = false;
    done.then(() => { finished = true; });
    while (!finished) {
      await Promise.race([done, sleep(cfg.checkEverySec * 1000)]);
      if (finished) break;
      const c = await countParticipants(page, ourNames);
      const w = strangerWhileSpeaking(cfg, c);
      if (w) return leaveBusy(w, c);
    }
    if (await done) played += 1;
  }
  await pulseMute(true);
  writeState('spoke', { plays_done: played, mic_ui: micUi });
  return null;
}

async function main() {
  let cfg;
  try {
    cfg = readConfig();
  } catch (e) {
    writeState('failed', { reason: e.reason || 'bad_config' });
    process.exit(1);
  }
  const startedAt = Date.now();
  const deadline = startedAt + cfg.maxSeconds * 1000;
  // Жёсткий общий срок: что бы ни зависло — через него процесс выходит сам.
  setTimeout(() => {
    if (!isTerminal()) writeState('failed', { reason: 'hard_timeout' });
    pulseMute(true).finally(() => process.exit(1));
    setTimeout(() => process.exit(1), 5000);
  }, (cfg.maxSeconds + 90) * 1000);

  process.on('SIGTERM', () => {
    if (!isTerminal()) writeState('left', { reason: 'sigterm' });
    shutdown(0);
  });

  writeState('joining', { others: null });

  if (!(await pulseMute(true))) {
    writeState('failed', { reason: 'pulse_unavailable', step: 'setup' });
    process.exit(1);
  }
  const phrasePath = path.join(KIT_DIR, 'phrases', cfg.phraseFile);
  if (!fs.existsSync(phrasePath)) {
    writeState('failed', { reason: 'phrase_missing', step: 'setup' });
    process.exit(1);
  }

  let joinMod; let admission; let joinStatus; let crashkit; let constans; let chromium; let StealthPlugin;
  try {
    joinMod = require(path.join(TELEMOST, 'join.js'));
    admission = require(path.join(TELEMOST, 'admission.js'));
    joinStatus = require(path.join(TELEMOST, 'join-status.js'));
    crashkit = require(path.join(TELEMOST, 'crashkit.js'));
    constans = require(path.join(DIST, 'constans.js'));
    ({ chromium } = appRequire('playwright-extra'));
    StealthPlugin = appRequire('puppeteer-extra-plugin-stealth');
  } catch (e) {
    say('modules_failed', { error: shortErr(e) });
    writeState('failed', { reason: 'image_modules_missing', step: 'setup' });
    process.exit(1);
  }

  // Браузер — как у бота (index.ts): stealth, headless:false, те же аргументы.
  const stealth = StealthPlugin();
  stealth.enabledEvasions.delete('iframe.contentWindow');
  stealth.enabledEvasions.delete('media.codecs');
  chromium.use(stealth);
  browser = await chromium.launch({ headless: false, args: constans.getBrowserArgs(false) });
  const context = await browser.newContext({
    permissions: ['camera', 'microphone'],
    userAgent: constans.userAgent,
    viewport: null,
  });
  page = await context.newPage();

  // Настройки для кода входа из образа: без адреса колбэка — колбэки молчат.
  const botCfg = { platform: 'yandex_telemost', meetingUrl: cfg.meetingUrl, botName: cfg.name };
  const ourNames = [cfg.name, cfg.botName];

  joinStatus.writeJoinStatus('joining');
  try {
    await joinMod.joinYandexTelemost(page, cfg.meetingUrl, cfg.name, botCfg);
    joinStatus.writeJoinStatus('admission', { admission_timeout_ms: cfg.admissionTimeoutSec * 1000 });
    const admitted = await admission.waitForYandexTelemostAdmission(page, cfg.admissionTimeoutSec * 1000, botCfg);
    if (!admitted) throw new Error('Telemost admission timeout');
    joinStatus.writeJoinStatus('in_call');
  } catch (error) {
    // Как markJoinFailed бота: причина → снимок → статус failed.
    const message = (error && error.message) || String(error);
    let reason = joinStatus.classifyJoinError(message);
    if (reason !== 'admission_rejected' && (await joinStatus.detectMeetingGone(page).catch(() => false))) {
      reason = 'meeting_not_found';
    }
    const kit = await crashkit.captureCrashKit(page, reason).catch(() => null);
    joinStatus.writeJoinStatus('failed', { reason, step: 'join', message: joinStatus.shortMessage(message), crash_kit: kit });
    writeState('failed', { reason, step: 'join', crash_kit: kit });
    return shutdown(1);
  }

  // Сразу после входа — «в комнате только я».
  await sleep(3000);
  const first = await sampleCount(page, ourNames);
  if (first.count === null) return leaveBusy('count_unknown', first);
  if (othersOf(first) > 0) return leaveBusy('strangers', first);
  writeState('alone', { others: 0, count_method: first.method, count_diag: first.diag });

  let lastSeq = 0;
  let streaks = { unknownStreak: 0, foreignStreak: 0 };
  let nextCheck = Date.now() + cfg.checkEverySec * 1000;
  for (;;) {
    if (page.isClosed() || !browser.isConnected()) {
      writeState('failed', { reason: 'browser_gone' });
      return shutdown(1);
    }
    if (Date.now() >= deadline) {
      writeState('left', { reason: 'max_seconds' });
      return shutdown(0);
    }
    const cmd = readCmd();
    if (cmd && cmd.seq > lastSeq) {
      lastSeq = cmd.seq;
      say('command', { action: cmd.action, seq: cmd.seq });
      if (cmd.action === 'leave') {
        await pulseMute(true).catch(() => {});
        writeState('left', { reason: 'command' });
        return shutdown(0);
      }
      if (cmd.action === 'speak' && state.phase === 'alone') {
        await speak(cfg, phrasePath, ourNames);
        if (isTerminal()) return null;
      }
    }
    if (Date.now() >= nextCheck) {
      nextCheck = Date.now() + cfg.checkEverySec * 1000;
      const c = await countParticipants(page, ourNames);
      streaks = idleVerdict(cfg, c, streaks);
      if (streaks.busy) return leaveBusy(streaks.busy, c);
    }
    await sleep(1000);
  }
}

if (require.main === module) {
  main().catch(async (e) => {
    say('unexpected_error', { error: shortErr(e) });
    if (!isTerminal()) {
      let kit = null;
      try {
        const crashkit = require(path.join(TELEMOST, 'crashkit.js'));
        kit = await crashkit.captureCrashKit(page, 'speaker_error');
      } catch { /* */ }
      writeState('failed', { reason: 'speaker_error', crash_kit: kit });
    }
    await shutdown(1);
  });
} else {
  // Подключён как модуль (тест) — только чистые решения, без браузера и звука.
  module.exports = { idleVerdict, strangerWhileSpeaking, ALLOWED_OTHERS, MAX_UNKNOWN_STREAK, FOREIGN_NAME_STREAK };
}

