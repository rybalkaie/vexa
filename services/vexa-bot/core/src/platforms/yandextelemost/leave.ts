// Yandex Telemost: graceful leave.
//
// 1) prepareForRecording — expose logBot и getBotConfig в browser context
//    (одинаковая практика во всех адаптерах Vexa).
// 2) leaveYandexTelemost — клик кнопки выхода.
//    Если ни один не сработал — page.close() через основной flow.
//
// 25.09.2026 (фаза 3а, ISS-60): в Телемосте 3.0 кнопка выхода —
// `end-call-alt-button` во вложенном окне `/private-join/`, а прежний поиск шёл
// только по главной странице → `leave_result clicked=false`, бот уходил лишь
// закрытием браузера. Теперь выход ищется во всех окнах (окно звонка первым),
// нажатие проверяется. performLeaveAction (его зовёт общий meetingFlow при
// таймауте впуска) тоже смотрит вложенные окна того же происхождения.
// По сигналу остановки, пока бот ещё не в звонке, сначала снимается аварийный
// снимок (stop-signal.ts, хвост У8).
//
// 25.09.2026 (фаза 3б): живая самопроверка 3а показала `leave_result clicked=true
// left=false`. Выход проверялся одним признаком — пропала ли кнопка в ТОМ ЖЕ окне
// за 3 с. В 3.0 окно звонка после выхода не удаляется (его прячет виджет главной
// или поверх ложится экран «звонок завершён»), а видимость Playwright считает
// внутри окна, не глядя, спрятано ли само окно, — кнопка «видна», выход «не
// подтверждён». И второй попытки не было: если клик не дошёл до обработчика
// Телемоста, бот уходил только закрытием браузера. Теперь выход подтверждается
// любым из признаков — окно ушло, кнопка пропала, соединения звонка закрылись
// (rtc-hook.ts); не подтвердился — окно «Выйти из звонка?» (только «Выйти», никогда
// «для всех») и ровно один JS-клик по той же кнопке. В журнале — чем подтвердилось.

import { Frame, Page } from "playwright";
import { log, callLeaveCallback } from "../../utils";
import { BotConfig } from "../../types";
import { telemostLeaveButtonSelectors3, telemostLeaveConfirmLabels, telemostLeaveNeverPattern } from "./selectors";
import { frameKind, orderedFrames } from "./screen";
import { captureStopSnapshot, stopSignalReceived } from "./stop-signal";
import { normLabel, scanControls } from "./fallback";

const LOG_PREFIX = "[adapter-telemost]";

export async function prepareForRecording(page: Page, botConfig: BotConfig): Promise<void> {
  await page.exposeFunction("logBot", (msg: string) => {
    log(msg);
  });
  await page.exposeFunction("getBotConfig", (): BotConfig => botConfig);

  // Зеркало performLeaveAction из googlemeet — его зовёт shared meetingFlow при
  // admission timeout (window.performLeaveAction). Ищет кнопку в главном окне и
  // во вложенных окнах того же происхождения (Телемост 3.0), окна звонка первыми.
  await page.evaluate((selectorsData) => {
    if (typeof (window as any).performLeaveAction !== "function") {
      (window as any).performLeaveAction = async () => {
        const leaveSelectors: string[] = selectorsData.leaveSelectors || [];
        const marker: string = selectorsData.callFrameMarker || "";
        const docs: Document[] = [];
        const walk = (d: Document, depth: number) => {
          if (docs.indexOf(d) !== -1) return;
          docs.push(d);
          if (depth >= 6) return;
          for (const fr of Array.from(d.querySelectorAll("iframe"))) {
            try {
              const cd = (fr as HTMLIFrameElement).contentDocument;
              if (cd) walk(cd, depth + 1);
            } catch {
              /* чужой origin */
            }
          }
        };
        walk(document, 0);
        const isCall = (d: Document) => {
          try {
            return !!marker && String(d.location && d.location.href).includes(marker);
          } catch {
            return false;
          }
        };
        const ordered = [...docs.filter(isCall), ...docs.filter((d) => !isCall(d))];
        for (const d of ordered) {
          for (const sel of leaveSelectors) {
            try {
              const btn = d.querySelector(sel) as HTMLElement | null;
              if (!btn) continue;
              const rect = btn.getBoundingClientRect();
              const view = d.defaultView || window;
              const cs = view.getComputedStyle(btn);
              const visible =
                rect.width > 0 &&
                rect.height > 0 &&
                cs.display !== "none" &&
                cs.visibility !== "hidden" &&
                cs.opacity !== "0";
              if (!visible) continue;
              btn.scrollIntoView({ behavior: "smooth", block: "center" });
              await new Promise((r) => setTimeout(r, 300));
              btn.click();
              (window as any).logBot?.(`[adapter-telemost] leave_button_clicked selector=${sel} call_frame=${isCall(d)}`);
              return true;
            } catch {
              continue;
            }
          }
        }
        (window as any).logBot?.("[adapter-telemost] leave_button_none_found");
        return false;
      };
    }
  }, { leaveSelectors: telemostLeaveButtonSelectors3, callFrameMarker: "/private-join/" });
}

/** Сколько ждать подтверждения выхода после нажатия (и после повторной попытки). */
const LEAVE_CONFIRM_MS = 6000;
const LEAVE_RETRY_CONFIRM_MS = 4000;
/** По сигналу остановки у docker на всё 20 с (снимок + выход) — ждём меньше. */
const LEAVE_CONFIRM_ON_STOP_MS = 2500;
const LEAVE_RETRY_ON_STOP_MS = 1500;

export type LeaveClickResult = {
  clicked: boolean;
  left: boolean;
  selector?: string;
  frame?: string;
  /** Чем подтвердился выход: frame_detached / button_gone / rtc_closed / page_closed. */
  via?: string | null;
  /** click, js_click, js_click_hidden, +confirm, +js_click (повтор). */
  method?: string;
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Сколько соединений звонка (RTCPeerConnection) ещё не закрыто — во всех окнах.
 * null — перехвата нет (старый образ, копия экрана без звонка): этот признак молчит.
 */
export async function openCallConnections(page: Page): Promise<number | null> {
  let total: number | null = null;
  if (!page || page.isClosed()) return null;
  for (const fr of page.frames()) {
    if (fr.isDetached()) continue;
    const n = await Promise.race([
      fr
        .evaluate(() => {
          const f = (window as any).__vexaOpenPeerConnections;
          return typeof f === "function" ? Number(f()) : null;
        })
        .catch(() => null),
      new Promise<null>((r) => setTimeout(() => r(null), 1500)),
    ]);
    if (typeof n === "number" && Number.isFinite(n)) total = (total ?? 0) + n;
  }
  return total;
}

/** Признак выхода или null. `hidden` — кнопка была спрятана: «пропала» = ушла из окна совсем. */
async function leftSignal(page: Page, fr: Frame, sel: string, hidden: boolean, rtcBefore: number | null): Promise<string | null> {
  if (page.isClosed()) return "page_closed";
  if (fr.isDetached()) return "frame_detached";
  const still = hidden
    ? await fr.locator(sel).count().then((n) => n > 0).catch(() => false)
    : await fr.locator(sel).first().isVisible().catch(() => false);
  if (!still) return "button_gone";
  if (rtcBefore !== null && rtcBefore > 0) {
    const open = await openCallConnections(page);
    if (open === 0) return "rtc_closed";
  }
  return null;
}

async function waitLeft(page: Page, fr: Frame, sel: string, hidden: boolean, rtcBefore: number | null, ms: number): Promise<string | null> {
  const start = Date.now();
  for (;;) {
    const s = await leftSignal(page, fr, sel, hidden, rtcBefore);
    if (s) return s;
    if (Date.now() - start >= ms) return null;
    await sleep(300);
  }
}

/** JS-нажатие с полной последовательностью событий (React-обработчики Телемоста). */
async function jsClickFull(fr: Frame, sel: string): Promise<boolean> {
  return fr
    .evaluate((s: string) => {
      const el = document.querySelector(s) as HTMLElement | null;
      if (!el) return false;
      const o = { bubbles: true, cancelable: true, composed: true, view: window };
      try {
        el.dispatchEvent(new PointerEvent("pointerdown", o));
      } catch {
        /* */
      }
      el.dispatchEvent(new MouseEvent("mousedown", o));
      try {
        el.dispatchEvent(new PointerEvent("pointerup", o));
      } catch {
        /* */
      }
      el.dispatchEvent(new MouseEvent("mouseup", o));
      el.click();
      return true;
    }, sel)
    .catch(() => false);
}

/**
 * Окно «Выйти из звонка?» после нажатия выхода: нажать в нём «Выйти» (точное
 * совпадение со списком selectors.ts) и НИКОГДА — «для всех». Только в окне
 * поверх экрана. true — нажали.
 */
export async function clickLeaveConfirm(page: Page): Promise<boolean> {
  const labels = new Set(telemostLeaveConfirmLabels.map(normLabel));
  const scan = await scanControls(page, "leave", "");
  for (const c of scan.controls) {
    if (c.kind !== "button" || !c.dialog || c.disabled) continue;
    const words = [c.text, c.aria, c.title].filter(Boolean) as string[];
    if (words.some((w) => telemostLeaveNeverPattern.test(w))) continue;
    if (!words.some((w) => labels.has(normLabel(w)))) continue;
    const fr = scan.frames[c.frameIdx];
    const sel = `[data-notary-fb="${c.id}"]`;
    const ok = await fr.locator(sel).first().click({ timeout: 2000 }).then(() => true).catch(() => false);
    if (!ok) await jsClickFull(fr, sel);
    log(`${LOG_PREFIX} leave_confirm_clicked frame=${c.frame} method=${ok ? "click" : "js_click"}`);
    return true;
  }
  return false;
}

/**
 * Нажать выход в любом окне страницы (окно звонка 3.0 первым) и подтвердить выход:
 * окно ушло / кнопка пропала / соединения звонка закрылись. Не подтвердилось —
 * окно подтверждения «Выйти», потом ровно один JS-клик по той же кнопке. Не бросает.
 */
export async function clickTelemostLeave(page: Page): Promise<LeaveClickResult> {
  const rtcBefore = await openCallConnections(page).catch(() => null);
  const onStop = stopSignalReceived();
  const firstWait = onStop ? LEAVE_CONFIRM_ON_STOP_MS : LEAVE_CONFIRM_MS;
  const retryWait = onStop ? LEAVE_RETRY_ON_STOP_MS : LEAVE_RETRY_CONFIRM_MS;

  const confirmAndRetry = async (fr: Frame, sel: string, hidden: boolean, method: string, canJsRetry: boolean) => {
    let via = await waitLeft(page, fr, sel, hidden, rtcBefore, firstWait);
    if (!via && (await clickLeaveConfirm(page).catch(() => false))) {
      method += "+confirm";
      via = await waitLeft(page, fr, sel, hidden, rtcBefore, retryWait);
    }
    if (!via && canJsRetry && !fr.isDetached()) {
      // Клик «прошёл», а выхода нет (обработчик Телемоста его не поймал) — ровно
      // один JS-клик по той же кнопке, если она всё ещё на месте.
      const still = await fr.locator(sel).first().isVisible().catch(() => false);
      if (still && (await jsClickFull(fr, sel))) {
        method += "+js_click";
        via = await waitLeft(page, fr, sel, hidden, rtcBefore, retryWait);
      }
    }
    return { via, method };
  };

  for (const fr of orderedFrames(page)) {
    if (fr.isDetached()) continue;
    for (const sel of telemostLeaveButtonSelectors3) {
      try {
        const loc = fr.locator(sel).first();
        if (!(await loc.isVisible())) continue;
        const kind = frameKind(page, fr);
        const ok = await loc.click({ timeout: 2000 }).then(() => true).catch(() => false);
        if (!ok) {
          // Не нажалось обычным кликом (перекрыто) — один JS-клик по той же кнопке.
          await jsClickFull(fr, sel);
        }
        log(`${LOG_PREFIX} leave_button_clicked selector=${sel} frame=${kind} method=${ok ? "click" : "js_click"}`);
        const r = await confirmAndRetry(fr, sel, false, ok ? "click" : "js_click", ok);
        return { clicked: true, left: !!r.via, selector: sel, frame: kind, via: r.via, method: r.method };
      } catch {
        continue;
      }
    }
  }
  // Видимой кнопки нет (панель звонка 3.0 бывает спрятана, пока мышь не
  // двигается): кнопка выхода звонка 3.0 есть в окне, но не видна — нажимаем её
  // JS-кликом. Это выход, нажать его «лишний раз» не страшно.
  const sel3 = telemostLeaveButtonSelectors3[0];
  for (const fr of orderedFrames(page)) {
    if (fr.isDetached() || frameKind(page, fr) !== "call") continue;
    const hit = await fr
      .evaluate((s: string) => {
        const b = document.querySelector(s) as HTMLElement | null;
        if (!b) return false;
        b.click();
        return true;
      }, sel3)
      .catch(() => false);
    if (hit) {
      log(`${LOG_PREFIX} leave_button_clicked selector=${sel3} frame=call method=js_click_hidden`);
      const r = await confirmAndRetry(fr, sel3, true, "js_click_hidden", false);
      return { clicked: true, left: !!r.via, selector: sel3, frame: "call", via: r.via, method: r.method };
    }
  }
  return { clicked: false, left: false };
}

export async function leaveYandexTelemost(
  page: Page | null,
  botConfig?: BotConfig,
  reason: string = "manual_leave"
): Promise<boolean> {
  log(`${LOG_PREFIX} leave_invoked reason=${reason}`);
  if (!page || page.isClosed()) {
    log(`${LOG_PREFIX} leave_page_unavailable`);
    return false;
  }

  // Хвост У8: остановили, пока бот ещё не в звонке → сначала снимок экрана.
  await captureStopSnapshot(page).catch(() => null);

  if (botConfig) {
    try {
      await callLeaveCallback(botConfig, reason);
    } catch (e: any) {
      log(`${LOG_PREFIX} leave_callback_failed: ${e.message}`);
    }
  }

  try {
    const r = await clickTelemostLeave(page);
    if (r.clicked) {
      log(`${LOG_PREFIX} leave_result clicked=true left=${r.left} frame=${r.frame} via=${r.via || "none"} method=${r.method}`);
      return true;
    }
  } catch (e: any) {
    log(`${LOG_PREFIX} leave_click_failed: ${e?.message}`);
  }

  // Запасной путь — прежний: кнопка через performLeaveAction в странице.
  try {
    const ok = await page.evaluate(async () => {
      if (typeof (window as any).performLeaveAction === "function") {
        return await (window as any).performLeaveAction();
      }
      return false;
    });
    log(`${LOG_PREFIX} leave_result clicked=${ok}`);
    return ok;
  } catch (e: any) {
    log(`${LOG_PREFIX} leave_eval_failed: ${e.message}`);
    return false;
  }
}
