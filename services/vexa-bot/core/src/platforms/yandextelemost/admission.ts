// Yandex Telemost: ожидание admission в комнату.
//
// Логика (под план Ф2):
//  - Если страница уже в комнате (виден тулбар встречи) — admitted.
//  - Если виден индикатор waiting room ≥ 30s — push в Telegram (один раз).
//  - Если за 2 минуты в waiting room не пустили — выход + push «не записал».
//  - Если виден индикатор «встречи не существует» → rejected.
//
// In-meeting индикаторы — гипотеза, проверяется логированием на первом
// прогоне. Если ни один не сработал, но прошло > 10s и нет lobby/waiting/error —
// считаем admitted по принципу «нигде не видим точно отрицания».

import { Page } from "playwright";
import { log, callAwaitingAdmissionCallback } from "../../utils";
import { BotConfig } from "../../types";
import {
  telemostWaitingRoomIndicators,
  telemostRejectionIndicators,
  telemostInMeetingIndicators,
  telemostNameInputSelectors,
} from "./selectors";
import { sendTelegramMessage } from "./telegram";

const LOG_PREFIX = "[adapter-telemost]";
const WAITING_ROOM_PUSH_AFTER_MS = 30_000;
const WAITING_ROOM_ABANDON_AFTER_MS = 30_000 + 2 * 60_000; // 30s + 2min

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

async function anyVisible(page: Page, selectors: string[]): Promise<{ matched: boolean; selector?: string }> {
  for (const sel of selectors) {
    try {
      const loc = page.locator(sel).first();
      if (await loc.isVisible({ timeout: 300 })) {
        return { matched: true, selector: sel };
      }
    } catch {
      continue;
    }
  }
  return { matched: false };
}

async function isLobbyStillVisible(page: Page): Promise<boolean> {
  // Если поле ввода имени до сих пор на экране — мы ещё в lobby, не в комнате.
  const r = await anyVisible(page, telemostNameInputSelectors);
  return r.matched;
}

export async function checkForTelemostAdmissionIndicators(page: Page): Promise<boolean> {
  // Сначала исключаем lobby: если поле имени всё ещё на месте — мы НЕ в комнате.
  if (await isLobbyStillVisible(page)) {
    return false;
  }
  const r = await anyVisible(page, telemostInMeetingIndicators);
  if (r.matched) {
    logStep("admission_indicator_found", { selector: r.selector });
    return true;
  }
  return false;
}

export async function checkForTelemostAdmissionSilent(page: Page): Promise<boolean> {
  return checkForTelemostAdmissionIndicators(page);
}

export async function checkForTelemostWaitingRoom(page: Page): Promise<boolean> {
  const r = await anyVisible(page, telemostWaitingRoomIndicators);
  return r.matched;
}

export async function checkForTelemostRejection(page: Page): Promise<boolean> {
  const r = await anyVisible(page, telemostRejectionIndicators);
  if (r.matched) {
    logStep("rejection_indicator_found", { selector: r.selector });
    return true;
  }
  return false;
}

/**
 * Дамп DOM для разведки in-meeting селекторов на первом прогоне.
 * Не логирует контент транскрипта — только структурные testid/aria-label.
 */
async function dumpInMeetingDom(page: Page): Promise<void> {
  try {
    const dump = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll("[data-testid]"));
      const items = els.slice(0, 80).map((el) => ({
        testid: el.getAttribute("data-testid"),
        tag: el.tagName,
        aria: el.getAttribute("aria-label"),
        visible: (() => {
          const r = (el as HTMLElement).getBoundingClientRect();
          const cs = getComputedStyle(el as HTMLElement);
          return r.width > 0 && r.height > 0 && cs.display !== "none" && cs.visibility !== "hidden";
        })(),
      }));
      return { url: location.href, count: els.length, items };
    });
    logStep("dom_dump", { url: dump.url, testid_count: dump.count });
    // Печатаем порциями, чтобы не упереться в length лимит одной строки лога.
    const batches: typeof dump.items[] = [];
    for (let i = 0; i < dump.items.length; i += 10) batches.push(dump.items.slice(i, i + 10));
    batches.forEach((batch, idx) => {
      log(`${LOG_PREFIX} dom_batch=${idx} items=${JSON.stringify(batch)}`);
    });
  } catch (err: any) {
    logStep("dom_dump_failed", { error: err.message });
  }
}

export async function waitForYandexTelemostAdmission(
  page: Page,
  timeoutMs: number,
  botConfig: BotConfig
): Promise<boolean> {
  logStep("admission_wait_start", { timeout_ms: timeoutMs });
  const start = Date.now();

  // Маленькая пауза чтобы клиент-сайд успел подгрузить состояние после клика «Подключиться».
  await page.waitForTimeout(3000);

  let waitingRoomFirstSeenAt: number | null = null;
  let waitingRoomPushSent = false;
  let domDumped = false;

  const POLL_INTERVAL_MS = 1500;

  while (Date.now() - start < timeoutMs) {
    // Rejection — финальная история.
    if (await checkForTelemostRejection(page)) {
      throw new Error("Bot admission was rejected by meeting admin (telemost rejection indicator)");
    }

    // Admitted — успех.
    const admitted = await checkForTelemostAdmissionIndicators(page);
    if (admitted) {
      logStep("admitted", { elapsed_s: Math.round((Date.now() - start) / 1000) });
      if (!domDumped) {
        await dumpInMeetingDom(page);
        domDumped = true;
      }
      return true;
    }

    // Waiting room.
    const inWaiting = await checkForTelemostWaitingRoom(page);
    if (inWaiting) {
      if (waitingRoomFirstSeenAt === null) {
        waitingRoomFirstSeenAt = Date.now();
        logStep("waiting_room_detected");
        try {
          await callAwaitingAdmissionCallback(botConfig);
        } catch {
          // non-fatal
        }
      }
      const inWaitingFor = Date.now() - waitingRoomFirstSeenAt;
      if (!waitingRoomPushSent && inWaitingFor >= WAITING_ROOM_PUSH_AFTER_MS) {
        waitingRoomPushSent = true;
        await sendTelegramMessage(
          `🟡 <b>Telemost</b>: бот в waiting room.\nКомната: <code>${botConfig.meetingUrl ?? "?"}</code>\nВпусти бота, если ты на встрече.`,
          { dedupeKey: `wait-${botConfig.connectionId}`, dedupeMs: 5 * 60_000 }
        );
      }
      if (inWaitingFor >= WAITING_ROOM_ABANDON_AFTER_MS) {
        await sendTelegramMessage(
          `🔴 <b>Telemost</b>: бот ушёл из waiting room — admit не получен за 2 мин. Запись не сделана.\nКомната: <code>${botConfig.meetingUrl ?? "?"}</code>`,
          { dedupeKey: `abandon-${botConfig.connectionId}`, dedupeMs: 10 * 60_000 }
        );
        throw new Error("Bot was not admitted from Telemost waiting room within 2 minutes");
      }
    }

    // Раз в N секунд — диагностический срез того что видно на странице.
    const elapsedS = Math.round((Date.now() - start) / 1000);
    if (elapsedS % 10 === 0) {
      logStep("admission_polling", { elapsed_s: elapsedS, waiting_room: inWaiting });
    }
    // Каждые 30s — periodic DOM dump (для диагностики, когда нашими селекторами
    // не виден ни lobby, ни waiting, ни in-meeting indicator).
    if (elapsedS > 0 && elapsedS % 30 === 0 && !domDumped) {
      await dumpInMeetingDom(page);
      domDumped = true; // one-shot до admission_timeout_dump_starting
    }

    await page.waitForTimeout(POLL_INTERVAL_MS);
  }

  // Финал — обязательно делаем dom dump, что бы ни случилось.
  // Без этого Ф2 заведомо непродиагностируем на следующем прогоне.
  logStep("admission_timeout_dump_starting");
  await dumpInMeetingDom(page);

  if (await checkForTelemostAdmissionIndicators(page)) {
    return true;
  }

  // Если ни lobby, ни waiting — считаем admitted и продолжаем
  // (возможно в комнате, но in-meeting indicators не сработали).
  const lobbyStill = await isLobbyStillVisible(page);
  const waitingStill = await checkForTelemostWaitingRoom(page);
  logStep("admission_final_state", { lobby_still: lobbyStill, waiting_still: waitingStill });
  if (!lobbyStill && !waitingStill) {
    logStep("admitted_by_elimination", { reason: "no lobby, no waiting, no rejection — assume in meeting" });
    return true;
  }

  throw new Error("Telemost admission timeout");
}
