/**
 * Звонок Телемоста: микрофон и камера, участники, дорожки звука, вложенные окна,
 * снимок по сигналу остановки (ISS-60, фаза 3а плана telemost-update-resilience).
 *
 * Браузерный код (перехват дорожек, обход окон) проверяется на поддельном окне:
 * обе функции самодостаточны и в странице работают тем же текстом.
 * Имена — выдуманные.
 *
 * Run (в образе): node /app/dist/platforms/yandextelemost/call-ui.test.js
 */

import { readDeviceState, deviceTargetSelector } from "./devices";
import { cleanParticipantNames, presenceFromTiles, panelDue } from "./participants";
import { installRtcAudioHook, installFrameWalker } from "./rtc-hook";
import { shouldCaptureOnStop } from "./stop-signal";

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

const btn = (tid: string | null, aria: string | null = null) => ({ tid, aria, text: "" });

console.log("readDeviceState — микрофон");
expect("панель звонка 3.0: «Выключить микрофон» = включён (разведка 25.09)",
  readDeviceState([btn("mute-audio", "Выключить микрофон"), btn("end-call-alt-button")], "mic"),
  { state: "on", target: { tid: "mute-audio", aria: "Выключить микрофон" }, evidence: "mute-audio:on" });
expect("после нажатия: «Включить микрофон» = выключен",
  readDeviceState([btn("mute-audio", "Включить микрофон"), btn("turn-on-mic-button")], "mic").state, "off");
expect("подпись панели важнее соседней кнопки лобби",
  readDeviceState([btn("mute-audio", "Включить микрофон"), btn("turn-off-mic-button")], "mic").state, "off");
expect("лобби 3.0: turn-off-mic-button = включён",
  readDeviceState([btn("turn-off-mic-button"), btn("enter-conference-button")], "mic"),
  { state: "on", target: { tid: "turn-off-mic-button", aria: null }, evidence: "turn-off-mic-button" });
expect("лобби: turn-on-mic-button = выключен", readDeviceState([btn("turn-on-mic-button")], "mic").state, "off");
expect("старый интерфейс: только подпись", readDeviceState([btn(null, "Выключить микрофон")], "mic").target,
  { tid: null, aria: "Выключить микрофон" });
expect("кнопки нет", readDeviceState([btn("end-call-alt-button")], "mic").state, null);

console.log("readDeviceState — камера");
expect("лобби: turn-off-camera-button = включена", readDeviceState([btn("turn-off-camera-button")], "camera").state, "on");
expect("звонок 3.0: turn-on-camera-button = выключена", readDeviceState([btn("turn-on-camera-button")], "camera").state, "off");
expect("подпись «Выключить камеру»", readDeviceState([btn(null, "Выключить камеру")], "camera").state, "on");
expect("камера не путается с микрофоном", readDeviceState([btn("mute-audio", "Выключить микрофон")], "camera").state, null);

console.log("deviceTargetSelector — цель только в состоянии «включено»");
expect("testid + подпись", deviceTargetSelector({ tid: "mute-audio", aria: "Выключить микрофон" }),
  '[data-testid="mute-audio"][aria-label="Выключить микрофон"]');
expect("только testid", deviceTargetSelector({ tid: "turn-off-mic-button", aria: null }), '[data-testid="turn-off-mic-button"]');
expect("только подпись — только кнопка", deviceTargetSelector({ tid: null, aria: "Выключить микрофон" }),
  'button[aria-label="Выключить микрофон"], [role="button"][aria-label="Выключить микрофон"]');

console.log("cleanParticipantNames — имена с плиток");
const BOT = "Бот — протокол встречи";
expect("без бота, повторов и мусора",
  cleanParticipantNames([BOT, "Тест-диктор", "Тест-диктор", "Вы", "  Мария  Тестова ", "<b>x</b>", "", "12345", "Участники"], BOT),
  ["Тест-диктор", "Мария Тестова"]);
expect("бот с пометкой «(вы)» — тоже бот", cleanParticipantNames([`${BOT} (вы)`], BOT), []);

console.log("presenceFromTiles — сколько людей кроме бота сейчас");
expect("имя + счётчик 2", presenceFromTiles(["Тест-диктор"], 2, true), 1);
expect("имён не прочли, счётчик 2", presenceFromTiles([], 2, true), 1);
expect("бот один (своя плитка)", presenceFromTiles([], null, true), 0);
expect("имён больше, чем в счётчике", presenceFromTiles(["А", "Б", "В"], 2, true), 3);
expect("источника нет — не знаю, а не ноль", presenceFromTiles([], null, false), null);
expect("имена не прочлись, счётчика нет, плиток 3 (с ботом)", presenceFromTiles([], null, true, 3), 2);
expect("только своя плитка", presenceFromTiles([], null, true, 1), 0);

console.log("panelDue — панель «Участники» только запасным путём");
const T0 = 1_000_000;
expect("сразу после входа — рано", panelDue(T0 + 10_000, T0, 0, 0), false);
expect("30 с без плиток — пора", panelDue(T0 + 31_000, T0, 0, 0), true);
expect("плитки видели минуту назад — не надо", panelDue(T0 + 200_000, T0, 0, T0 + 140_000), false);
expect("плиток нет 2+ мин — пора", panelDue(T0 + 300_000, T0, 0, T0 + 170_000), true);
expect("панель смотрели 30 с назад — рано", panelDue(T0 + 300_000, T0, T0 + 270_000, 0), false);

console.log("installRtcAudioHook — список дорожек только живые (после переподключения)");
class FakeTrack extends EventTarget {
  readyState = "live";
  constructor(public kind: string, public id: string) { super(); }
  end() { this.readyState = "ended"; this.dispatchEvent(new Event("ended")); }
}
class FakePC extends EventTarget {}
const fakeWin: any = { RTCPeerConnection: FakePC };
installRtcAudioHook(fakeWin);
const pc = new fakeWin.RTCPeerConnection();
const emit = (t: FakeTrack) => pc.dispatchEvent(Object.assign(new Event("track"), { track: t }));
const ids = () => fakeWin.__vexaRemoteAudioTracks.map((t: FakeTrack) => t.id);
const a1 = new FakeTrack("audio", "a1");
const v1 = new FakeTrack("video", "v1");
const a2 = new FakeTrack("audio", "a2");
emit(a1); emit(v1); emit(a2); emit(a1);
expect("только аудио, без повторов", ids(), ["a1", "a2"]);
expect("объект соединения — настоящий класс", pc instanceof FakePC, true);
a1.end();
expect("завершённая выкинута по событию ended", ids(), ["a2"]);
a2.end();
const a3 = new FakeTrack("audio", "a3");
emit(a3);
expect("участник переподключился — в списке только новая живая", ids(), ["a3"]);
const before = fakeWin.RTCPeerConnection;
installRtcAudioHook(fakeWin);
expect("повторная установка не оборачивает второй раз", fakeWin.RTCPeerConnection === before, true);
const stale = new FakeTrack("audio", "stale");
fakeWin.__vexaRemoteAudioTracks.push(stale);
stale.readyState = "ended"; // завершилась без события
fakeWin.__vexaPruneRemoteAudioTracks();
expect("ручная чистка (её зовёт запись перед подмешиванием)", ids(), ["a3"]);
expect("окно без RTCPeerConnection — не падаем", (() => { const w: any = {}; installRtcAudioHook(w); return !!w.__vexaRtcHooked; })(), false);

console.log("installFrameWalker — вложенные окна вглубь");
function fakeWindow(name: string, children: any[] = []): any {
  const w: any = { name };
  w.document = { querySelectorAll: (sel: string) => (sel === "iframe" ? children.map((c) => ({ contentWindow: c })) : []) };
  return w;
}
const foreign: any = { name: "foreign" };
Object.defineProperty(foreign, "document", { get() { throw new Error("SecurityError: cross-origin"); } });
const w3 = fakeWindow("w3");
const w2 = fakeWindow("w2", [w3]);
const w1 = fakeWindow("w1", [w2]);
const root = fakeWindow("root", [w1, foreign]);
w3.document = { querySelectorAll: () => [{ contentWindow: root }] }; // петля — не зациклиться
const walkerHost: any = {};
installFrameWalker(walkerHost);
const walk = walkerHost.__vexaSameOriginFrameWindows;
expect("три уровня вложенности, чужое окно пропущено, без петли",
  walk(root, 6).map((w: any) => w.name), ["root", "w1", "w2", "w3"]);
expect("глубина ограничена", walk(root, 1).map((w: any) => w.name), ["root", "w1"]);

console.log("shouldCaptureOnStop — снимок по сигналу остановки только до звонка");
for (const ph of ["joining", "admission", "waiting_room"]) expect(`фаза ${ph} — снимок`, shouldCaptureOnStop(ph), true);
for (const ph of ["in_call", "failed", null]) expect(`фаза ${ph} — без снимка`, shouldCaptureOnStop(ph as any), false);
expect("зал ожидания при перезаходе после выброса (уже был в звонке) — без снимка", shouldCaptureOnStop("waiting_room", true), false);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
