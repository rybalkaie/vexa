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

import { Frame, Page } from "playwright";
import { log, callLeaveCallback } from "../../utils";
import { BotConfig } from "../../types";
import { telemostLeaveButtonSelectors3 } from "./selectors";
import { frameKind, orderedFrames } from "./screen";
import { captureStopSnapshot } from "./stop-signal";

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

/**
 * Нажать выход в любом окне страницы (окно звонка 3.0 первым) и проверить, что
 * кнопка ушла. Возвращает {clicked, left}. Не бросает.
 */
export async function clickTelemostLeave(page: Page): Promise<{ clicked: boolean; left: boolean; selector?: string; frame?: string }> {
  for (const fr of orderedFrames(page)) {
    if (fr.isDetached()) continue;
    for (const sel of telemostLeaveButtonSelectors3) {
      try {
        const loc = fr.locator(sel).first();
        if (!(await loc.isVisible({ timeout: 300 }))) continue;
        const kind = frameKind(page, fr);
        const ok = await loc.click({ timeout: 2000 }).then(() => true).catch(() => false);
        if (!ok) {
          // Не нажалось обычным кликом (перекрыто) — один JS-клик по той же кнопке.
          await fr.evaluate((s: string) => {
            (document.querySelector(s) as HTMLElement | null)?.click();
          }, sel).catch(() => {});
        }
        log(`${LOG_PREFIX} leave_button_clicked selector=${sel} frame=${kind} method=${ok ? "click" : "js_click"}`);
        const left = await waitLeaveButtonGone(fr, sel);
        return { clicked: true, left, selector: sel, frame: kind };
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
      const left = await waitLeaveButtonGone(fr, sel3, true);
      return { clicked: true, left, selector: sel3, frame: "call" };
    }
  }
  return { clicked: false, left: false };
}

/**
 * Вышли, если окно звонка ушло или кнопка выхода пропала (для спрятанной кнопки —
 * пропала из окна совсем). Ждём до 3 с.
 */
async function waitLeaveButtonGone(fr: Frame, sel: string, hidden = false): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < 3000) {
    if (fr.isDetached()) return true;
    const still = hidden
      ? await fr.locator(sel).count().then((n) => n > 0).catch(() => false)
      : await fr.locator(sel).first().isVisible({ timeout: 300 }).catch(() => false);
    if (!still) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
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
      log(`${LOG_PREFIX} leave_result clicked=true left=${r.left} frame=${r.frame}`);
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
