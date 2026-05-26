// Yandex Telemost: graceful leave.
//
// 1) prepareForRecording — expose logBot и getBotConfig в browser context
//    (одинаковая практика во всех адаптерах Vexa).
// 2) leaveYandexTelemost — клик кнопки «Выйти» по списку селекторов.
//    Если ни один не сработал — page.close() через основной flow.

import { Page } from "playwright";
import { log, callLeaveCallback } from "../../utils";
import { BotConfig } from "../../types";
import { telemostLeaveButtonSelectors } from "./selectors";

const LOG_PREFIX = "[adapter-telemost]";

export async function prepareForRecording(page: Page, botConfig: BotConfig): Promise<void> {
  await page.exposeFunction("logBot", (msg: string) => {
    log(msg);
  });
  await page.exposeFunction("getBotConfig", (): BotConfig => botConfig);

  // Зеркало performLeaveAction из googlemeet — потенциально пригодится в
  // shared meetingFlow при admission timeout (он вызывает window.performLeaveAction).
  await page.evaluate((selectorsData) => {
    if (typeof (window as any).performLeaveAction !== "function") {
      (window as any).performLeaveAction = async () => {
        const leaveSelectors = selectorsData.leaveSelectors || [];
        for (const sel of leaveSelectors) {
          try {
            const btn = document.querySelector(sel) as HTMLElement | null;
            if (!btn) continue;
            const rect = btn.getBoundingClientRect();
            const cs = getComputedStyle(btn);
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
            (window as any).logBot?.(`[adapter-telemost] leave_button_clicked selector=${sel}`);
            return true;
          } catch {
            continue;
          }
        }
        (window as any).logBot?.("[adapter-telemost] leave_button_none_found");
        return false;
      };
    }
  }, { leaveSelectors: telemostLeaveButtonSelectors });
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

  if (botConfig) {
    try {
      await callLeaveCallback(botConfig, reason);
    } catch (e: any) {
      log(`${LOG_PREFIX} leave_callback_failed: ${e.message}`);
    }
  }

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
