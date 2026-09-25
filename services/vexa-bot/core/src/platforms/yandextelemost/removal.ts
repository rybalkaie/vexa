// Yandex Telemost: периодическая проверка «нас выкинули из встречи».
//
// На Ф2 это лёгкая страховка: если на странице появился один из removal-
// индикаторов («Встреча завершена», «Вы отключены» и т.п.) — сигналим
// runMeetingFlow, который прервёт запись и сделает graceful leave.
//
// 25.09.2026 (фаза 3а, ISS-60): в Телемосте 3.0 звонок живёт во вложенном окне
// `/private-join/`, а признаки искались только на главной — конец встречи бот
// не видел. Теперь, кроме прежних признаков на главной (старый интерфейс, как
// было), во ВСЕХ окнах ищутся строгие признаки (screen.ts, removalEvidence):
// короткие отдельные строки «встреча завершена / вы вышли / такого звонка нет»
// и метки `*meeting-ended*`. Реплика чата в длинной строке сюда не попадёт.

import { Page } from "playwright";
import { log } from "../../utils";
import { telemostRemovalIndicators } from "./selectors";
import { collectFrameSignals, removalEvidence } from "./screen";

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
  try {
    const hit = removalEvidence(await collectFrameSignals(page));
    if (hit) {
      log(`${LOG_PREFIX} removal_indicator_visible evidence=${hit}`);
      return true;
    }
  } catch {
    // страница закрывается — не наше дело
  }
  return false;
}

export function startYandexTelemostRemovalMonitor(
  page: Page,
  onRemoval?: () => void | Promise<void>
): () => void {
  log(`${LOG_PREFIX} removal_monitor_start`);
  let fired = false;
  let busy = false;

  const interval = setInterval(async () => {
    // Проверка всех окон может занять дольше интервала — не наслаиваем.
    if (busy || fired) return;
    busy = true;
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
    } finally {
      busy = false;
    }
  }, 2000);

  return () => clearInterval(interval);
}
