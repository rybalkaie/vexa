// Yandex Telemost: периодическая проверка «нас выкинули из встречи».
//
// На Ф2 это лёгкая страховка: если на странице появился один из removal-
// индикаторов («Встреча завершена», «Вы отключены» и т.п.) — сигналим
// runMeetingFlow, который прервёт запись и сделает graceful leave.

import { Page } from "playwright";
import { log } from "../../utils";
import { telemostRemovalIndicators } from "./selectors";

const LOG_PREFIX = "[adapter-telemost]";

export async function checkForTelemostRemoval(page: Page): Promise<boolean> {
  for (const sel of telemostRemovalIndicators) {
    try {
      const loc = page.locator(sel).first();
      if (await loc.isVisible({ timeout: 200 })) {
        log(`${LOG_PREFIX} removal_indicator_visible selector=${sel}`);
        return true;
      }
    } catch {
      continue;
    }
  }
  return false;
}

export function startYandexTelemostRemovalMonitor(
  page: Page,
  onRemoval?: () => void | Promise<void>
): () => void {
  log(`${LOG_PREFIX} removal_monitor_start`);
  let fired = false;

  const interval = setInterval(async () => {
    try {
      if (await checkForTelemostRemoval(page)) {
        if (!fired) {
          fired = true;
          log(`${LOG_PREFIX} removal_detected_from_node`);
          clearInterval(interval);
          try {
            await onRemoval?.();
          } catch {}
        }
      }
    } catch {
      // page closed mid-check is fine
    }
  }, 2000);

  return () => clearInterval(interval);
}
