// Микрофон и камера бота: надёжно выключены в лобби и в звонке.
// (ISS-60, фаза 3а плана plans/2026-09-23-telemost-update-resilience.md, 25.09.2026)
//
// ЗАЧЕМ. Живая разведка 25.09 (Т-10, Телемост 3.0): бот входит, а кнопка
// `mute-audio` во вложенном окне звонка остаётся «Выключить микрофон» — у людей
// бот «с включённым микрофоном». Звук при этом — тишина (виртуальный микрофон
// заглушён в PulseAudio, entrypoint.sh), но выглядит это плохо и путает людей.
// Прежний код гасил микрофон одним кликом по виджету на главной без проверки.
//
// КАК. Состояние читается по подписи кнопки (подпись «Выключить …» / testid
// turn-off-* видна, когда устройство ВКЛЮЧЕНО). Включено → Playwright-клик →
// ждём смены подписи → не сменилась → РОВНО ОДИН JS-клик по той же кнопке В ТОМ
// ЖЕ состоянии → снова проверка. Повторный клик по уже выключенному включил бы
// его обратно: селектор цели требует подписи «включено», поэтому по кнопке,
// которая уже переключилась, JS-клик не попадёт. Выключенное не трогаем никогда.
//
// Окна: сначала окно звонка 3.0 (`/private-join/`), потом главное, потом прочие.

import { Frame, Page } from "playwright";
import { log } from "../../utils";
import { telemostDeviceLabels } from "./selectors";
import { ScreenButton, frameKind, orderedFrames } from "./screen";

export type Device = "mic" | "camera";
export type DeviceState = "on" | "off" | null;
export type DeviceTarget = { tid: string | null; aria: string | null };
export type DeviceReading = { state: DeviceState; target: DeviceTarget | null; evidence: string };

const LOG_PREFIX = "[adapter-telemost]";
/** Сколько ждём смены подписи кнопки после нажатия. */
const LABEL_SETTLE_MS = 2500;

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

/**
 * Состояние устройства по видимым кнопкам одного окна — ЧИСТАЯ функция
 * (тест: call-ui.test.ts). Порядок доверия:
 *   1) кнопка панели звонка 3.0 (`mute-audio`) — по её подписи;
 *   2) кнопки лобби/звонка по testid (turn-off-* = включено, turn-on-* = выключено);
 *   3) любая кнопка с точной подписью «Выключить …» / «Включить …».
 */
export function readDeviceState(buttons: ScreenButton[], device: Device): DeviceReading {
  const L = telemostDeviceLabels[device];
  if (L.toolbarTid) {
    for (const b of buttons) {
      if (b.tid !== L.toolbarTid) continue;
      if (b.aria === L.onAria) return { state: "on", target: { tid: L.toolbarTid, aria: L.onAria }, evidence: `${L.toolbarTid}:on` };
      if (b.aria === L.offAria) return { state: "off", target: null, evidence: `${L.toolbarTid}:off` };
    }
  }
  if (buttons.some((b) => b.tid === L.onTid)) return { state: "on", target: { tid: L.onTid, aria: null }, evidence: L.onTid };
  if (buttons.some((b) => b.tid === L.offTid)) return { state: "off", target: null, evidence: L.offTid };
  if (buttons.some((b) => b.aria === L.onAria)) return { state: "on", target: { tid: null, aria: L.onAria }, evidence: "aria_on" };
  if (buttons.some((b) => b.aria === L.offAria)) return { state: "off", target: null, evidence: "aria_off" };
  return { state: null, target: null, evidence: "none" };
}

/** CSS-селектор цели: кнопка ИМЕННО в состоянии «включено». */
export function deviceTargetSelector(t: DeviceTarget): string {
  const q = (s: string) => s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  if (t.tid && t.aria) return `[data-testid="${q(t.tid)}"][aria-label="${q(t.aria)}"]`;
  if (t.tid) return `[data-testid="${q(t.tid)}"]`;
  const a = q(t.aria || "");
  return `button[aria-label="${a}"], [role="button"][aria-label="${a}"]`;
}

/** Метки и подписи кнопок микрофона и камеры (обоих интерфейсов). */
function deviceMarks(): { tids: string[]; arias: string[] } {
  const tids: string[] = [];
  const arias: string[] = [];
  for (const L of [telemostDeviceLabels.mic, telemostDeviceLabels.camera]) {
    tids.push(L.onTid, L.offTid);
    if (L.toolbarTid) tids.push(L.toolbarTid);
    arias.push(L.onAria, L.offAria);
  }
  return { tids, arias };
}

/**
 * Видимые кнопки микрофона и камеры в окне (testid, подпись). Берутся ТОЛЬКО
 * элементы с нашими метками или подписями — и неважно, `button` это или `div`:
 * на большой встрече кнопки плиток не вытеснят панель, а смена тега у Телемоста
 * не спрячет кнопку. Окно ушло → null.
 */
async function frameButtons(fr: Frame): Promise<ScreenButton[] | null> {
  try {
    return await Promise.race([
      fr.evaluate((m: { tids: string[]; arias: string[] }) => {
        const vis = (el: Element): boolean => {
          const h = el as HTMLElement;
          if (!(h.offsetWidth || h.offsetHeight || h.getClientRects().length)) return false;
          const cs = getComputedStyle(h);
          return cs.visibility !== "hidden" && cs.display !== "none";
        };
        // По метке — любой элемент; по одной подписи — только кнопка (подпись
        // «Выключить микрофон» может стоять и на значке состояния в плитке).
        const isButton = (el: Element) => el.tagName === "BUTTON" || el.getAttribute("role") === "button";
        return Array.from(document.querySelectorAll("[data-testid], [aria-label]"))
          .filter(
            (el) =>
              m.tids.includes(el.getAttribute("data-testid") || "") ||
              (isButton(el) && m.arias.includes(el.getAttribute("aria-label") || "")),
          )
          .filter(vis)
          .slice(0, 50)
          .map((b) => ({ tid: b.getAttribute("data-testid"), aria: b.getAttribute("aria-label"), text: "" }));
      }, deviceMarks()),
      new Promise<null>((r) => setTimeout(() => r(null), 2500)),
    ]);
  } catch {
    return null;
  }
}

async function readFrameDevice(fr: Frame, device: Device): Promise<DeviceReading> {
  const buttons = await frameButtons(fr);
  if (!buttons) return { state: null, target: null, evidence: "frame_unavailable" };
  return readDeviceState(buttons, device);
}

async function waitDeviceState(fr: Frame, device: Device, want: DeviceState, timeoutMs: number): Promise<DeviceState> {
  const start = Date.now();
  let last: DeviceState = null;
  while (Date.now() - start < timeoutMs) {
    last = (await readFrameDevice(fr, device)).state;
    if (last === want) return last;
    await new Promise((r) => setTimeout(r, 250));
  }
  return last;
}

export type EnsureOffOptions = {
  /** Только это окно (лобби 3.0) — иначе все окна, окно звонка первым. */
  onlyFrame?: Frame;
  /** Для логов: lobby / call / guard. */
  scope: string;
  /** Тихий режим (периодическая страховка): пишем в лог только нажатия. */
  quiet?: boolean;
};

/** Выключить устройство, если оно включено. Возвращает итоговое состояние. Не бросает. */
export async function ensureDeviceOff(page: Page, device: Device, opts: EnsureOffOptions): Promise<DeviceState> {
  if (!page || page.isClosed()) return null;
  const frames = opts.onlyFrame ? [opts.onlyFrame] : orderedFrames(page);
  for (const fr of frames) {
    if (fr.isDetached()) continue;
    const r = await readFrameDevice(fr, device);
    if (r.state === null) continue;
    const kind = frameKind(page, fr);
    if (r.state === "off" || !r.target) {
      if (!opts.quiet) logStep("device_off_confirmed", { device, scope: opts.scope, frame: kind, evidence: r.evidence });
      return r.state;
    }
    const sel = deviceTargetSelector(r.target);
    let method = "click";
    const clicked = await fr.locator(sel).first().click({ timeout: 2000 }).then(() => true).catch(() => false);
    // Подпись меняется не мгновенно: ждём её с запасом, иначе JS-клик пришёлся бы
    // на уже нажатую кнопку, чья подпись ещё не успела смениться, и включил бы
    // устройство обратно.
    let after = await waitDeviceState(fr, device, "off", LABEL_SETTLE_MS);
    if (after !== "off") {
      // Ровно один JS-клик — по кнопке, всё ещё в состоянии «включено».
      method = "js_click";
      await fr
        .evaluate((s: string) => {
          const b = document.querySelector(s) as HTMLElement | null;
          if (b) b.click();
        }, sel)
        .catch(() => {});
      after = await waitDeviceState(fr, device, "off", LABEL_SETTLE_MS);
    }
    logStep("device_off", { device, scope: opts.scope, frame: kind, method, clicked, state: after, evidence: r.evidence });
    return after;
  }
  if (!opts.quiet) logStep("device_button_not_found", { device, scope: opts.scope });
  return null;
}

/** Микрофон и камера — выключить (звонок или страховка по ходу встречи). Не бросает. */
export async function ensureTelemostDevicesOff(
  page: Page,
  scope: string,
  quiet = false,
): Promise<{ mic: DeviceState; camera: DeviceState }> {
  const mic = await ensureDeviceOff(page, "mic", { scope, quiet }).catch(() => null);
  const camera = await ensureDeviceOff(page, "camera", { scope, quiet }).catch(() => null);
  return { mic, camera };
}
