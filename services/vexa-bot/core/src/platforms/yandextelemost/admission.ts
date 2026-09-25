// Yandex Telemost: ожидание admission в комнату.
//
// Логика (под план Ф2):
//  - Если страница уже в комнате (виден тулбар встречи) — admitted.
//  - Если виден индикатор waiting room ≥ 30s — push в Telegram (один раз).
//  - Если за 2 минуты в waiting room не пустили — выход + push «не записал».
//  - Если виден индикатор «встречи не существует» → rejected.
//
// 25.09.2026 (фаза 3а, ISS-60, цикл5 фазы 1 Н6). Экран читается во ВСЕХ окнах
// страницы и классифицируется одной чистой функцией (screen.ts) в строгом
// порядке: встречи нет / отказали → зал ожидания → лобби → в звонке → неясно.
// Раньше признаки искались только на главной, «в звонке» проверялось раньше
// зала ожидания (а «Завершить звонок» в 3.0 виден уже в лобби) → бот в зале
// ожидания считал себя в звонке. И при неясном экране бот в конце объявлял себя
// в звонке «методом исключения» — больше нет: неясный экран = сорванный вход
// (`admission_unclear`), сторож повторит вход и сообщит.
// После впуска бот сразу выключает у себя микрофон и камеру с проверкой подписи
// кнопки (devices.ts): в 3.0 гость входит с включённым микрофоном.

import { Page } from "playwright";
import { log, callAwaitingAdmissionCallback } from "../../utils";
import { BotConfig } from "../../types";
import { writeJoinStatus, MEETING_GONE_ERROR, ADMISSION_UNCLEAR_ERROR } from "./join-status";
import { readTelemostScreen, ScreenVerdict } from "./screen";
import { ensureTelemostDevicesOff } from "./devices";

const LOG_PREFIX = "[adapter-telemost]";
const WAITING_ROOM_PUSH_AFTER_MS = 30_000;
const WAITING_ROOM_ABANDON_AFTER_MS = 30_000 + 2 * 60_000; // 30s + 2min

// Тексты исключений — их понимают index.ts / meetingFlow (rejected by meeting
// admin) и classifyJoinError (join-status.ts). Не переформулировать.
const REJECTED_ERROR = "Bot admission was rejected by meeting admin (telemost rejection indicator)";
const WAITING_ROOM_ERROR = "Bot was not admitted from Telemost waiting room within 2 minutes";
const STILL_IN_LOBBY_ERROR = "Lobby: после «Подключиться» бот остался в лобби Telemost";

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

/** В звонке ли бот — по всем окнам страницы (строгая проверка, «неясно» ≠ «в звонке»). */
export async function checkForTelemostAdmissionIndicators(page: Page): Promise<boolean> {
  const v = await readTelemostScreen(page);
  if (v.state === "in_call") {
    logStep("admission_indicator_found", { evidence: v.evidence, ui: v.ui });
    return true;
  }
  return false;
}

/**
 * Проверка «всё ещё в звонке» сразу после впуска (index.ts → checkAdmissionSilent).
 * Несколько чтений: мгновенная перерисовка экрана не должна выгнать бота с живой
 * встречи как «ложный вход». «Неясно» во всех чтениях — это «нет».
 */
export async function checkForTelemostAdmissionSilent(page: Page): Promise<boolean> {
  for (let i = 0; i < 3; i++) {
    if (await checkForTelemostAdmissionIndicators(page)) return true;
    if (i < 2) await page.waitForTimeout(1500).catch(() => {});
  }
  return false;
}

export async function checkForTelemostWaitingRoom(page: Page): Promise<boolean> {
  return (await readTelemostScreen(page)).state === "waiting_room";
}

export async function checkForTelemostRejection(page: Page): Promise<boolean> {
  const v = await readTelemostScreen(page);
  if (v.state === "rejected" || v.state === "gone") {
    logStep("rejection_indicator_found", { state: v.state, evidence: v.evidence });
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

/** Впустили: микрофон и камеру — выключить с проверкой (не бросает). */
async function onAdmitted(page: Page, v: ScreenVerdict, start: number): Promise<void> {
  logStep("admitted", { elapsed_s: Math.round((Date.now() - start) / 1000), evidence: v.evidence, ui: v.ui });
  const dev = await ensureTelemostDevicesOff(page, "call").catch(() => ({ mic: null, camera: null }));
  logStep("devices_after_admission", { mic: dev.mic, camera: dev.camera });
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
  let lastState: string | null = null;

  const POLL_INTERVAL_MS = 1500;

  while (Date.now() - start < timeoutMs) {
    const v = await readTelemostScreen(page);
    if (v.state !== lastState) {
      logStep("screen_state", { state: v.state, ui: v.ui, evidence: v.evidence });
      lastState = v.state;
    }

    // Отказ и «встречи нет» — финальная история (проверяются первыми).
    if (v.state === "rejected") {
      throw new Error(REJECTED_ERROR);
    }
    if (v.state === "gone") {
      throw new Error(MEETING_GONE_ERROR);
    }

    // Зал ожидания — раньше «в звонке» (классификатор уже так упорядочен).
    const inWaiting = v.state === "waiting_room";
    if (inWaiting) {
      if (waitingRoomFirstSeenAt === null) {
        waitingRoomFirstSeenAt = Date.now();
        logStep("waiting_room_detected", { evidence: v.evidence });
        // ISS-60: сторож на хосте видит «ждёт в зале ожидания» и не считает это зависанием.
        writeJoinStatus("waiting_room");
        try {
          await callAwaitingAdmissionCallback(botConfig);
        } catch {
          // non-fatal
        }
      }
      const inWaitingFor = Date.now() - waitingRoomFirstSeenAt;
      // 24.09.2026 (цикл5 фазы 1, С1/С4/У1): «впустите бота» и «не впустили» бот
      // сам больше не шлёт — токена в контейнере с браузером нет. По фазе
      // waiting_room сторож на хосте через ~30 с просит впустить бота тех, кому
      // уходит протокол (группа / организатор), а после срыва — обычным сообщением.
      if (!waitingRoomPushSent && inWaitingFor >= WAITING_ROOM_PUSH_AFTER_MS) {
        waitingRoomPushSent = true;
        logStep("waiting_room_long", { waited_ms: inWaitingFor });
      }
      if (inWaitingFor >= WAITING_ROOM_ABANDON_AFTER_MS) {
        throw new Error(WAITING_ROOM_ERROR);
      }
    }

    // Admitted — успех.
    if (v.state === "in_call") {
      if (!domDumped) {
        await dumpInMeetingDom(page);
        domDumped = true;
      }
      await onAdmitted(page, v, start);
      return true;
    }

    // Раз в N секунд — диагностический срез того что видно на странице.
    const elapsedS = Math.round((Date.now() - start) / 1000);
    if (elapsedS % 10 === 0) {
      logStep("admission_polling", { elapsed_s: elapsedS, state: v.state });
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

  const last = await readTelemostScreen(page);
  logStep("admission_final_state", { state: last.state, ui: last.ui, evidence: last.evidence });
  if (last.state === "in_call") {
    await onAdmitted(page, last, start);
    return true;
  }
  if (last.state === "rejected") throw new Error(REJECTED_ERROR);
  if (last.state === "gone") throw new Error(MEETING_GONE_ERROR);
  if (last.state === "waiting_room") throw new Error(WAITING_ROOM_ERROR);
  if (last.state === "lobby") throw new Error(STILL_IN_LOBBY_ERROR);
  // Неясный экран — НЕ «в звонке» (раньше здесь был admitted_by_elimination).
  throw new Error(ADMISSION_UNCLEAR_ERROR);
}
