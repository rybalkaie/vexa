/**
 * Копии экранов Телемоста в настоящем браузере (ISS-60, фаза 3а плана
 * telemost-update-resilience, 25.09.2026).
 *
 * Главная `https://telemost.yandex.ru/j/…` и вложенное окно звонка
 * `https://telemost.yandex.ru/private-join/…` подаются перехватом запросов
 * (context.route) — синтетический HTML по фактам живой разведки 25.09
 * (testid, классы плиток, мини-виджет «Открыть экран звонка»). В сеть не уходит
 * ничего: всё, что не Телемост, обрывается. Имена и номер комнаты выдуманные.
 *
 * Проверяются боевые функции бота целиком, вместе со сбором признаков внутри
 * страницы: экран (screen.ts), участники (participants.ts), конец встречи
 * (removal.ts), микрофон и камера (devices.ts), выход (leave.ts), впуск
 * (admission.ts), вход через лобби (join.ts) и снимок по сигналу остановки
 * (stop-signal.ts → leave.ts → crashkit.ts, в отдельном процессе).
 *
 * Не доказывает вход в живой Телемост — только то, что код верно читает экраны,
 * сложенные по разведке. Живую проверку делает самопроверка в Т-10.
 *
 * Run (в образе): node /app/dist/platforms/yandextelemost/screen-copies.test.js
 * На маке (браузер Playwright другой сборки):
 *   CHROMIUM_PATH=<путь к chrome-headless-shell> node <сборка>/screen-copies.test.js
 * VERBOSE=1 — печатать журнал бота; ONLY=screens,devices,admission,join,stop —
 * только эти части; NO_SIGNAL_OPTIONS=1 — браузер «как раньше» (ломание для
 * проверки: части stop должны покраснеть).
 */

import { spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { chromium, Browser, BrowserContext, Frame, LaunchOptions, Page } from "playwright";
import { readTelemostScreen, collectFrameSignals, removalEvidence, frameKind } from "./screen";
import { collectTileParticipants, cleanParticipantNames, presenceFromTiles } from "./participants";
import { ensureDeviceOff, ensureTelemostDevicesOff } from "./devices";
import { clickTelemostLeave, leaveYandexTelemost } from "./leave";
import { checkForTelemostRemoval } from "./removal";
import { waitForYandexTelemostAdmission } from "./admission";
import { joinYandexTelemost } from "./join";
import { classifyJoinError, writeJoinStatus } from "./join-status";
import { armStopSignalCapture, BROWSER_SIGNAL_OPTIONS } from "./stop-signal";

const ROOM = "00000000000000";
const ORIGIN = "https://telemost.yandex.ru";
const MAIN_URL = `${ORIGIN}/j/${ROOM}`;
const CALL_PATH = `/private-join/${ROOM}`;
const BOT = "Бот — протокол встречи";
const SPEAKER = "Тест-диктор";

// ---------------------------------------------------------------------------
// Копии экранов
// ---------------------------------------------------------------------------

const CSS = `<style>
body{font:14px/1.3 sans-serif;margin:8px}
iframe{width:1100px;height:600px;border:0;display:block}
button,[role=button]{display:inline-block;min-width:28px;min-height:24px;margin:2px}
.grid{display:flex;gap:8px;margin-top:8px}
.tile{width:180px;height:110px;background:#223;color:#fff}
.ph{width:60px;height:40px;background:#445}
.overlay{position:fixed;left:0;top:0;right:0;bottom:0;z-index:50;background:rgba(0,0,0,0.01)}
.hidden{display:none}
</style>`;

// Поведение кнопок окна звонка: микрофон (подпись панели и testid лобби меняются
// вместе), камера, выход, «Подключиться» → следующий экран.
const CALL_JS = `
window.__micClicks = 0;
function micOn() {
  var m = document.querySelector('[data-testid="mute-audio"]');
  if (m) return m.getAttribute('aria-label') === 'Выключить микрофон';
  return !!document.querySelector('[data-testid="turn-off-mic-button"]');
}
function setMic(on) {
  var m = document.querySelector('[data-testid="mute-audio"]');
  if (m) m.setAttribute('aria-label', on ? 'Выключить микрофон' : 'Включить микрофон');
  var t = document.querySelector('[data-testid="turn-off-mic-button"],[data-testid="turn-on-mic-button"]');
  if (t) t.setAttribute('data-testid', on ? 'turn-off-mic-button' : 'turn-on-mic-button');
}
function toggleMic() { window.__micClicks++; if (window.__micDead) return; setMic(!micOn()); }
function toggleCam(b) {
  b.setAttribute('data-testid', b.getAttribute('data-testid') === 'turn-off-camera-button' ? 'turn-on-camera-button' : 'turn-off-camera-button');
}
function leaveCall() {
  window.__left = (window.__left || 0) + 1;
  document.body.innerHTML = '<div>Вы вышли из звонка</div><button>Вернуться в звонок</button>';
  try { var w = parent.document.getElementById('widget'); if (w) w.innerHTML = ''; } catch (e) {}
}
function joinClicked() {
  var inp = document.querySelector('input');
  window.__name = inp ? inp.value : null;
  window.__joinClicks = (window.__joinClicks || 0) + 1;
  setTimeout(function () {
    document.body.innerHTML = window.__nextScreen || '';
    try { var w = parent.document.getElementById('widget'); if (w && window.__nextWidget) w.innerHTML = window.__nextWidget; } catch (e) {}
  }, 400);
}
`;

function callDoc(body: string, extraJs = ""): string {
  return `<!doctype html><html><head><meta charset="utf-8">${CSS}</head><body>${body}<script>${CALL_JS}${extraJs}</script></body></html>`;
}

function mainDoc(body: string, extraJs = ""): string {
  return `<!doctype html><html><head><meta charset="utf-8"><title>Телемост</title>${CSS}</head><body>${body}<script>${extraJs}</script></body></html>`;
}

const IFRAME = `<iframe id="call" src="${CALL_PATH}?embed=1"></iframe>`;
// Поле поиска чатов на главной 3.0 носит тот же testid, что поле имени лобби.
const SEARCH = `<input data-testid="orb-textinput-input" placeholder="Поиск">`;
// Главная 3.0 в лобби: виджет «Групповой звонок / Подключение», где «Завершить
// звонок» и «Выключить микрофон» видны уже до входа.
const WIDGET_LOBBY =
  `<div>Групповой звонок</div><div>Подключение</div>` +
  `<button aria-label="Завершить звонок" onclick="window.__leftMain=(window.__leftMain||0)+1">×</button>` +
  `<button aria-label="Выключить микрофон">м</button>`;
// Главная 3.0 в звонке: мини-виджет со счётчиком.
const widgetCall = (n: number) =>
  `<div role="button" aria-label="Открыть экран звонка"><div>10</div><div>${n} ${n === 1 ? "участник" : "участника"}</div><div>•</div><div>00:36</div></div>`;

const main30 = (widget: string) => mainDoc(`<div>Телемост</div>${SEARCH}<div id="widget">${widget}</div>${IFRAME}`);

const TOOLBAR =
  `<div id="toolbar">` +
  `<button data-testid="end-call-alt-button" onclick="leaveCall()">⏹</button>` +
  `<button data-testid="more-popup-alt-button">…</button>` +
  `<button data-testid="mute-audio" aria-label="Выключить микрофон" onclick="toggleMic()">🎤</button>` +
  `<button data-testid="turn-off-mic-button" onclick="toggleMic()">м</button>` +
  `<button data-testid="turn-on-camera-button" onclick="toggleCam(this)">к</button>` +
  `<button data-testid="settings-button">⚙</button>` +
  `<button data-testid="share-button">⇪</button>` +
  `</div>`;

const tiles = (names: string[]) =>
  `<div class="grid">` +
  names
    .map(
      (n) =>
        `<div class="newGridItem_k7f2 tile"><div class="ph" data-testid="participant-video-placeholder"></div>` +
        `<div class="avatarWithNameBlock_p1x9"><span class="participantName_z3q8">${n}</span></div></div>`,
    )
    .join("") +
  `</div>`;

const LOBBY_BODY =
  `<div>Подключение к звонку</div>` +
  `<input data-testid="orb-textinput-input" placeholder="Ваше имя">` +
  `<button data-testid="turn-off-mic-button" onclick="toggleMic()">м</button>` +
  `<button data-testid="turn-off-camera-button" onclick="toggleCam(this)">к</button>` +
  `<button data-testid="enter-conference-button" onclick="joinClicked()">Подключиться</button>`;

const IN_CALL_BODY = (names: string[]) => TOOLBAR + tiles(names);
// Зал ожидания 3.0 вживую не видели: худший случай — панель звонка уже на экране.
const WAITING_BODY = `<div>Ожидание организатора</div><div>Организатор скоро впустит вас в звонок</div>${TOOLBAR}`;
const REJECTED_BODY = `<div>Организатор отклонил ваш запрос</div><button>На главную</button>`;
const ENDED_BODY = `<div>Встреча завершена</div><button>На главную</button>`;
const UNCLEAR_BODY = `<div>Загрузка</div>`;
// Чат комнаты: в 3.0 звонок живёт в чате, там бывает «Звонок завершён» от прошлой встречи.
const CHAT = `<div class="chat"><div>${SPEAKER}</div><div>Звонок завершён</div><div>Встреча завершена</div></div>`;

// Старый интерфейс: всё на главной, вложенного окна нет.
const OLD_LOBBY = mainDoc(
  `<div>Подключение к встрече</div><input data-testid="orb-textinput-input" placeholder="Введите имя">` +
    `<button data-testid="turn-off-mic-button">м</button><button data-testid="enter-conference-button">Подключиться</button>`,
);
const OLD_CALL = mainDoc(
  `<div>Встреча</div><button aria-label="Завершить звонок" onclick="window.__leftMain=(window.__leftMain||0)+1">×</button>` +
    `<button data-testid="mute-audio" aria-label="Выключить микрофон">м</button>` +
    `<button data-testid="participants-button" aria-label="Участники">у</button>`,
);
const GONE_30 = mainDoc(`<div>Телемост</div><div>Войдите в аккаунт</div><h1>Такого звонка нет</h1><div>Возможно, в ссылке опечатка</div><button>На главную</button>`);

// Главная для полного входа: окно онбординга 3.0 и «Продолжить в браузере»,
// после которого появляется вложенное окно лобби.
const JOIN_MAIN = mainDoc(
  `<div>Телемост</div>${SEARCH}<div id="widget">${WIDGET_LOBBY}</div>` +
    `<div role="dialog" id="onb">Большое обновление в Телемосте ` +
    `<button data-testid="telemost-3-onboarding-close" onclick="document.getElementById('onb').remove()">×</button></div>` +
    `<div id="inter">Вы подключаетесь к видеовстрече ` +
    `<button data-testid="meeting-continue-in-browser-continue" onclick="cont()">Продолжить в браузере</button></div>`,
  `function cont(){var i=document.getElementById('inter'); if(i) i.remove(); if(document.getElementById('call')) return;` +
    `var f=document.createElement('iframe'); f.id='call'; f.src='${CALL_PATH}?embed=1'; document.body.appendChild(f);}`,
);
const lobbyThen = (next: string, nextWidget: string) =>
  callDoc(LOBBY_BODY, `window.__nextScreen = ${JSON.stringify(next)}; window.__nextWidget = ${JSON.stringify(nextWidget)};`);

type Screens = { main: string; call?: string };

// ---------------------------------------------------------------------------
// Обвязка
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;
let captured: string[] = [];

function expect(name: string, actual: any, expected: any) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    process.stdout.write(`  PASS  ${name}\n`);
    passed++;
  } else {
    process.stdout.write(`  FAIL  ${name}\n        expected: ${e}\n        actual:   ${a}\n`);
    failed++;
  }
}

/** Журнал бота (console.log → JSON-строки) — в память; VERBOSE=1 — ещё и на экран. */
function captureBotLog(): void {
  const orig = console.log.bind(console);
  console.log = (...args: any[]) => {
    const line = args.map((x) => String(x)).join(" ");
    captured.push(line);
    if (process.env.VERBOSE === "1") orig(line);
  };
}
/** Сообщения журнала бота (поле msg JSON-строки, без префикса подсистемы). */
function botMsgs(): string[] {
  return captured.map((l) => {
    try {
      const o = JSON.parse(l);
      return typeof o.msg === "string" ? o.msg : l;
    } catch {
      return l;
    }
  });
}
const logHas = (re: RegExp) => botMsgs().some((m) => re.test(m));

function launchOptions(): LaunchOptions {
  const exe = process.env.CHROMIUM_PATH;
  return { headless: true, ...(exe ? { executablePath: exe } : {}) };
}

async function routeScreens(ctx: BrowserContext, screens: Screens): Promise<void> {
  await ctx.route("**/*", async (route) => {
    let u: URL;
    try {
      u = new URL(route.request().url());
    } catch {
      return route.abort();
    }
    const html = (body: string) => route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body });
    if (u.origin === ORIGIN && u.pathname === `/j/${ROOM}`) return html(screens.main);
    if (u.origin === ORIGIN && u.pathname === CALL_PATH && screens.call !== undefined) return html(screens.call);
    if (u.origin === ORIGIN) return route.fulfill({ status: 404, body: "" });
    return route.abort();
  });
}

async function withScreens(
  browser: Browser,
  screens: Screens,
  fn: (page: Page) => Promise<void>,
  opts: { goto?: boolean } = {},
): Promise<void> {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await routeScreens(ctx, screens);
  const page = await ctx.newPage();
  try {
    if (opts.goto !== false) await page.goto(MAIN_URL, { waitUntil: "load" });
    await fn(page);
  } catch (e: any) {
    expect(`сценарий не упал (${e?.message || e})`, false, true);
  } finally {
    await ctx.close().catch(() => {});
  }
}

function callFrame(page: Page): Frame | null {
  return page.frames().find((f) => frameKind(page, f) === "call") || null;
}

async function inCall<T>(page: Page, fn: () => T): Promise<T | null> {
  const fr = callFrame(page);
  return fr ? ((await fr.evaluate(fn).catch(() => null)) as T | null) : null;
}

async function mustThrow(p: Promise<unknown>): Promise<string> {
  try {
    await p;
    return "(не бросило)";
  } catch (e: any) {
    return String(e?.message || e);
  }
}

// ---------------------------------------------------------------------------
// Сценарии
// ---------------------------------------------------------------------------

async function screensAndParticipants(browser: Browser): Promise<void> {
  console.info("Телемост 3.0 — экран, участники, конец встречи");

  await withScreens(browser, { main: main30(widgetCall(1)), call: callDoc(IN_CALL_BODY([BOT])) }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("одна плитка (бот один): в звонке, 3.0", [v.state, v.ui], ["in_call", "3.0"]);
    const snap = await collectTileParticipants(page);
    const names = cleanParticipantNames(snap.rawNames, BOT);
    expect("одна плитка: имён людей нет, счётчик 1, плитка 1", [names, snap.count, snap.tilesSeen], [[], 1, 1]);
    expect("одна плитка: людей кроме бота — 0", presenceFromTiles(names, snap.count, snap.sourceFound, snap.tilesSeen), 0);
    expect("одна плитка: конца встречи нет", await checkForTelemostRemoval(page), false);
  });

  await withScreens(browser, { main: main30(widgetCall(2)), call: callDoc(IN_CALL_BODY([BOT, SPEAKER]) + CHAT) }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("две плитки + чат с «Звонок завершён»: в звонке", v.state, "in_call");
    const snap = await collectTileParticipants(page);
    const names = cleanParticipantNames(snap.rawNames, BOT);
    expect("две плитки: имя диктора с плитки, счётчик 2", [names, snap.count], [[SPEAKER], 2]);
    expect("две плитки: людей кроме бота — 1", presenceFromTiles(names, snap.count, snap.sourceFound, snap.tilesSeen), 1);
    expect("реплика чата «Встреча завершена» не выгоняет бота", removalEvidence(await collectFrameSignals(page)), null);
    expect("сторож выброса: встреча идёт", await checkForTelemostRemoval(page), false);
  });

  await withScreens(browser, { main: main30(WIDGET_LOBBY), call: callDoc(WAITING_BODY) }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("зал ожидания во вложенном окне (панель звонка уже видна) — зал ожидания", [v.state, v.evidence], ["waiting_room", "call:text_waiting"]);
  });

  await withScreens(browser, { main: main30(WIDGET_LOBBY), call: callDoc(REJECTED_BODY) }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("отказ во вложенном окне", [v.state, v.evidence], ["rejected", "call:text_rejected"]);
  });

  await withScreens(browser, { main: main30(""), call: callDoc(ENDED_BODY) }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("«Встреча завершена» во вложенном окне — встречи нет", v.state, "gone");
    expect("сторож выброса видит конец встречи во вложенном окне", await checkForTelemostRemoval(page), true);
  });

  await withScreens(browser, { main: main30(WIDGET_LOBBY), call: callDoc(UNCLEAR_BODY) }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("неясный экран («Завершить звонок» только на главной) — НЕ в звонке", v.state, "unknown");
  });

  await withScreens(browser, { main: main30(WIDGET_LOBBY), call: callDoc(LOBBY_BODY) }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("лобби 3.0 (поле поиска на главной не путается с полем имени)", [v.state, v.evidence], ["lobby", "call:join_button"]);
  });

  await withScreens(browser, { main: GONE_30 }, async (page) => {
    expect("«Такого звонка нет» (живой экран 24.09) — встречи нет", (await readTelemostScreen(page)).state, "gone");
  });

  console.info("Старый интерфейс (без вложенного окна)");
  await withScreens(browser, { main: OLD_LOBBY }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("старое лобби", [v.state, v.ui], ["lobby", "old"]);
  });
  await withScreens(browser, { main: OLD_CALL }, async (page) => {
    const v = await readTelemostScreen(page);
    expect("старый звонок: «Завершить звонок» на главной", [v.state, v.ui, v.evidence], ["in_call", "old", "main:aria_end_call"]);
    const r = await clickTelemostLeave(page);
    expect("старый звонок: выход нажат на главной", [r.clicked, r.frame], [true, "main"]);
    expect("старый звонок: нажат ровно один раз", await page.evaluate(() => (window as any).__leftMain), 1);
  });
}

async function devicesAndLeave(browser: Browser): Promise<void> {
  console.info("Микрофон, камера, выход — Телемост 3.0");

  await withScreens(browser, { main: main30(widgetCall(2)), call: callDoc(IN_CALL_BODY([BOT, SPEAKER])) }, async (page) => {
    const res = await ensureTelemostDevicesOff(page, "call");
    expect("в звонке: микрофон выключен, камера и так выключена", res, { mic: "off", camera: "off" });
    const aria = await inCall(page, () => document.querySelector('[data-testid="mute-audio"]')?.getAttribute("aria-label"));
    expect("подпись кнопки стала «Включить микрофон»", aria, "Включить микрофон");
    expect("нажатие одно", await inCall(page, () => (window as any).__micClicks), 1);
    const again = await ensureTelemostDevicesOff(page, "guard", true);
    expect("страховка по ходу встречи: выключенное не трогает", [again.mic, await inCall(page, () => (window as any).__micClicks)], ["off", 1]);
    const r = await clickTelemostLeave(page);
    expect("выход: кнопка end-call-alt-button во вложенном окне", [r.clicked, r.left, r.frame, r.selector], [true, true, "call", '[data-testid="end-call-alt-button"]']);
    expect("выход нажат ровно один раз", await inCall(page, () => (window as any).__left), 1);
  });

  // Обычный клик не проходит (кнопку накрыл прозрачный слой) → один JS-клик.
  await withScreens(
    browser,
    { main: main30(widgetCall(1)), call: callDoc(IN_CALL_BODY([BOT]) + `<div class="overlay"></div>`) },
    async (page) => {
      const st = await ensureDeviceOff(page, "mic", { scope: "call" });
      expect("накрытая кнопка: после JS-клика микрофон выключен", st, "off");
      expect("накрытая кнопка: нажатий ровно одно (JS)", await inCall(page, () => (window as any).__micClicks), 1);
    },
  );

  // Кнопка не срабатывает вовсе → ровно два нажатия (обычное + один JS), не больше.
  await withScreens(
    browser,
    { main: main30(widgetCall(1)), call: callDoc(IN_CALL_BODY([BOT]), "window.__micDead = true;") },
    async (page) => {
      const st = await ensureDeviceOff(page, "mic", { scope: "call" });
      expect("мёртвая кнопка: честно «включён»", st, "on");
      expect("мёртвая кнопка: нажатий два (обычное + один JS)", await inCall(page, () => (window as any).__micClicks), 2);
    },
  );

  // Панель звонка спрятана (display:none) → выход JS-кликом по кнопке 3.0.
  await withScreens(
    browser,
    { main: main30(widgetCall(1)), call: callDoc(IN_CALL_BODY([BOT]), "document.getElementById('toolbar').className='hidden';") },
    async (page) => {
      const r = await clickTelemostLeave(page);
      expect("спрятанная панель: выход всё равно нажат во вложенном окне", [r.clicked, r.frame], [true, "call"]);
      expect("спрятанная панель: нажат один раз", await inCall(page, () => (window as any).__left), 1);
    },
  );

  // Лобби: микрофон и камера выключаются в окне лобби.
  await withScreens(browser, { main: main30(WIDGET_LOBBY), call: callDoc(LOBBY_BODY) }, async (page) => {
    const lobby = callFrame(page)!;
    const mic = await ensureDeviceOff(page, "mic", { onlyFrame: lobby, scope: "lobby" });
    const cam = await ensureDeviceOff(page, "camera", { onlyFrame: lobby, scope: "lobby" });
    expect("лобби: микрофон и камера выключены", [mic, cam], ["off", "off"]);
    const tids = await inCall(page, () => Array.from(document.querySelectorAll("[data-testid]")).map((e) => e.getAttribute("data-testid")));
    expect("лобби: видны «включить микрофон/камеру»", [tids?.includes("turn-on-mic-button"), tids?.includes("turn-on-camera-button")], [true, true]);
  });
}

async function admissionFlow(browser: Browser): Promise<void> {
  console.info("Впуск (admission.ts) на копиях экранов");
  const cfg: any = { platform: "yandex_telemost", meetingUrl: MAIN_URL, botName: BOT };

  await withScreens(browser, { main: main30(widgetCall(2)), call: callDoc(IN_CALL_BODY([BOT, SPEAKER])) }, async (page) => {
    expect("звонок: впустили", await waitForYandexTelemostAdmission(page, 15_000, cfg), true);
    const aria = await inCall(page, () => document.querySelector('[data-testid="mute-audio"]')?.getAttribute("aria-label"));
    expect("звонок: сразу после впуска микрофон выключен", aria, "Включить микрофон");
  });

  await withScreens(browser, { main: main30(WIDGET_LOBBY), call: callDoc(WAITING_BODY) }, async (page) => {
    const msg = await mustThrow(waitForYandexTelemostAdmission(page, 6_000, cfg));
    expect("зал ожидания: бот НЕ считает себя в звонке", classifyJoinError(msg), "waiting_room_not_admitted");
  });

  await withScreens(browser, { main: main30(WIDGET_LOBBY), call: callDoc(REJECTED_BODY) }, async (page) => {
    const msg = await mustThrow(waitForYandexTelemostAdmission(page, 15_000, cfg));
    expect("отказ: причина «отклонили»", classifyJoinError(msg), "admission_rejected");
  });

  await withScreens(browser, { main: main30(""), call: callDoc(ENDED_BODY) }, async (page) => {
    const msg = await mustThrow(waitForYandexTelemostAdmission(page, 15_000, cfg));
    expect("встреча завершена: причина «встречи нет»", classifyJoinError(msg), "meeting_not_found");
  });

  await withScreens(browser, { main: main30(WIDGET_LOBBY), call: callDoc(UNCLEAR_BODY) }, async (page) => {
    const msg = await mustThrow(waitForYandexTelemostAdmission(page, 5_000, cfg));
    expect("неясный экран: сорванный вход, а не «в звонке»", classifyJoinError(msg), "admission_unclear");
  });
}

async function fullJoin(browser: Browser): Promise<void> {
  console.info("Полный вход через лобби (join.ts) на копиях экранов 3.0");
  const cfg: any = { platform: "yandex_telemost", meetingUrl: MAIN_URL, botName: BOT };

  captured = [];
  await withScreens(
    browser,
    { main: JOIN_MAIN, call: lobbyThen(IN_CALL_BODY([BOT, SPEAKER]), widgetCall(2)) },
    async (page) => {
      await joinYandexTelemost(page, MAIN_URL, BOT, cfg);
      expect("вход: имя вписано в поле лобби", await inCall(page, () => (window as any).__name), BOT);
      expect("вход: «Подключиться» нажата", Number(await inCall(page, () => (window as any).__joinClicks)) >= 1, true);
      expect("вход: в журнале «лобби пройдено» (за лобби звонок)", logHas(/^step=lobby_left ts=/), true);
      expect("вход: микрофон в лобби выключен с проверкой подписи",
        logHas(/^step=device_off .*device="mic" scope="lobby" .*state="off"/), true);
      expect("вход → впуск", await waitForYandexTelemostAdmission(page, 15_000, cfg), true);
    },
    { goto: false },
  );

  captured = [];
  await withScreens(
    browser,
    { main: JOIN_MAIN, call: lobbyThen(WAITING_BODY, "") },
    async (page) => {
      await joinYandexTelemost(page, MAIN_URL, BOT, cfg);
      expect("вход в зал ожидания: «лобби пройдено» НЕ пишется", logHas(/^step=lobby_left ts=/), false);
      expect("вход в зал ожидания: пишется «ушёл в зал ожидания»", logHas(/^step=lobby_left_to_waiting_room /), true);
      expect("вход в зал ожидания: экран — зал ожидания", (await readTelemostScreen(page)).state, "waiting_room");
    },
    { goto: false },
  );

  captured = [];
  await withScreens(browser, { main: GONE_30 }, async (page) => {
    const msg = await mustThrow(joinYandexTelemost(page, "https://evil.example/telemost.yandex.ru/j/1", BOT, cfg));
    expect("чужая ссылка (С10): бот её не открывает", classifyJoinError(msg), "meeting_url_rejected");
    expect("чужая ссылка: страница не открывалась", page.url(), "about:blank");
  }, { goto: false });
}

// ---------------------------------------------------------------------------
// Сигнал остановки: отдельный процесс, как бот под `docker stop`.
// ---------------------------------------------------------------------------

async function stopChild(phase: string): Promise<void> {
  // Как у бота (index.ts): сигналы остановки ловит бот, а не Playwright.
  // NO_SIGNAL_OPTIONS=1 — запуск как было раньше (для проверки ломанием).
  const signalOptions = process.env.NO_SIGNAL_OPTIONS === "1" ? {} : BROWSER_SIGNAL_OPTIONS;
  const browser = await chromium.launch({ ...launchOptions(), ...signalOptions });
  const ctx = await browser.newContext();
  const inCallNow = phase === "in_call";
  await routeScreens(ctx, inCallNow
    ? { main: main30(widgetCall(2)), call: callDoc(IN_CALL_BODY([BOT, SPEAKER])) }
    : { main: main30(WIDGET_LOBBY), call: callDoc(LOBBY_BODY) });
  const page = await ctx.newPage();
  await page.goto(MAIN_URL, { waitUntil: "load" });
  writeJoinStatus("joining");
  if (inCallNow) writeJoinStatus("in_call");
  armStopSignalCapture();
  process.on("SIGTERM", async () => {
    const left = await leaveYandexTelemost(page, undefined, "signal_test");
    const leftMain = await page.evaluate(() => (window as any).__leftMain || 0).catch(() => -1);
    const leftCall = await (callFrame(page)?.evaluate(() => (window as any).__left || 0).catch(() => -1) ?? Promise.resolve(-1));
    process.stdout.write(`STOP_CHILD_RESULT ${JSON.stringify({ left, leftMain, leftCall })}\n`);
    await browser.close().catch(() => {});
    process.exit(0);
  });
  process.stdout.write("STOP_CHILD_READY\n");
  await new Promise(() => {}); // «завис» — ждёт сигнала
}

function runStopChild(phase: string, dir: string, uid: string): Promise<{ code: number | null; out: string }> {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [__filename, "--stop-child", phase], {
      env: { ...process.env, BOT_CONFIG: JSON.stringify({ sessionUid: uid }), TELEMOST_TRANSCRIPT_DIR: dir },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    let signalled = false;
    const killer = setTimeout(() => child.kill("SIGKILL"), 90_000);
    child.stdout.on("data", (d) => {
      out += String(d);
      if (!signalled && out.includes("STOP_CHILD_READY")) {
        signalled = true;
        child.kill("SIGTERM");
      }
    });
    child.stderr.on("data", (d) => {
      out += String(d);
    });
    child.on("exit", (code) => {
      clearTimeout(killer);
      resolve({ code, out });
    });
  });
}

async function stopSignal(): Promise<void> {
  console.info("Сигнал остановки: снимок экрана, пока бот не в звонке, затем штатный выход");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "telemost-stop-"));
  try {
    const uid = `stoptest-${process.pid}`;
    const r = await runStopChild("joining", dir, uid);
    const status = JSON.parse(fs.readFileSync(path.join(dir, ".join-status", `${uid}.json`), "utf8"));
    const kitDir = path.join(dir, ".crash-kits", uid);
    expect("завис на входе → по SIGTERM процесс вышел сам", r.code, 0);
    expect("статус: фаза та же, путь к снимку и пометка сигнала", [status.phase, status.crash_kit, status.stop_signal], ["joining", kitDir, true]);
    const kit = JSON.parse(fs.readFileSync(path.join(kitDir, "kit.json"), "utf8"));
    expect("снимок: причина stop_signal_joining, сняты оба окна", [kit.reason, kit.frames.length >= 2], ["stop_signal_joining", true]);
    expect("снимок: картинка экрана есть", fs.existsSync(path.join(kitDir, "screen.png")), true);
    const res = JSON.parse((r.out.match(/STOP_CHILD_RESULT (.*)/) || [])[1] || "{}");
    expect("после снимка — штатный выход (нажата кнопка выхода)", [res.left, res.leftMain + res.leftCall >= 1], [true, true]);

    const uid2 = `stoptest-call-${process.pid}`;
    const r2 = await runStopChild("in_call", dir, uid2);
    const res2 = JSON.parse((r2.out.match(/STOP_CHILD_RESULT (.*)/) || [])[1] || "{}");
    expect("в звонке: снимка живой встречи нет", fs.existsSync(path.join(dir, ".crash-kits", uid2)), false);
    expect("в звонке: вышел кнопкой end-call-alt-button во вложенном окне", [r2.code, res2.left, res2.leftCall], [0, true, 1]);
  } catch (e: any) {
    expect(`сигнал остановки: проверка не упала (${e?.message || e})`, false, true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const i = process.argv.indexOf("--stop-child");
  if (i !== -1) {
    await stopChild(process.argv[i + 1] || "joining");
    return;
  }
  captureBotLog();
  // ONLY=screens,devices,admission,join,stop — прогнать только эти части.
  const only = (process.env.ONLY || "").split(",").map((x) => x.trim()).filter(Boolean);
  const on = (part: string) => only.length === 0 || only.includes(part);
  const browser = await chromium.launch(launchOptions());
  try {
    if (on("screens")) await screensAndParticipants(browser);
    if (on("devices")) await devicesAndLeave(browser);
    if (on("admission")) await admissionFlow(browser);
    if (on("join")) await fullJoin(browser);
  } finally {
    await browser.close().catch(() => {});
  }
  if (on("stop")) await stopSignal();
  process.stdout.write(`\n${passed} passed, ${failed} failed\n`);
  process.exit(failed > 0 ? 1 : 0);
}

main().catch((e) => {
  process.stdout.write(`FATAL ${e?.stack || e}\n`);
  process.exit(1);
});
