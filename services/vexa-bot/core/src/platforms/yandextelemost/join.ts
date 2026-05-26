// Yandex Telemost: заход бота в комнату.
//
// Поток:
//   1) Открываем https://telemost.yandex.ru/j/<id>
//   2) Промежуточный экран «Вы подключаетесь к видеовстрече» → клик «Продолжить в браузере»
//   3) Lobby:
//        a) Вводим имя бота
//        b) НЕ кликаем по mic/cam (по дефолту выключены, Orb-кнопки «Включить...»)
//        c) Клик «Подключиться»
//   4) Выход в meetingFlow.waitForAdmission

import { Page } from "playwright";
import { log, callJoiningCallback } from "../../utils";
import { BotConfig } from "../../types";
import {
  telemostInterstitialContinueSelectors,
  telemostNameInputSelectors,
  telemostJoinButtonSelectors,
  telemostMicButtonSelectors,
  telemostCameraButtonSelectors,
} from "./selectors";

const LOG_PREFIX = "[adapter-telemost]";

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

async function tryClickFirstVisible(page: Page, selectors: string[], stepName: string, timeoutMs = 30000): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    for (const sel of selectors) {
      try {
        const loc = page.locator(sel).first();
        if (await loc.isVisible({ timeout: 500 })) {
          await loc.click({ timeout: 2000 });
          logStep(`${stepName}_clicked`, { selector: sel });
          return true;
        }
      } catch {
        // try next selector
      }
    }
    await page.waitForTimeout(500);
  }
  logStep(`${stepName}_timeout`, { tried_selectors: selectors });
  return false;
}

async function fillNameInput(page: Page, name: string): Promise<boolean> {
  for (const sel of telemostNameInputSelectors) {
    try {
      const loc = page.locator(sel).first();
      if (await loc.isVisible({ timeout: 1000 })) {
        await loc.click({ timeout: 2000 });
        // Reliable clear-and-fill: select all, type new value
        await page.keyboard.press("Control+A").catch(() => {});
        await page.keyboard.press("Meta+A").catch(() => {});
        await page.keyboard.press("Delete").catch(() => {});
        await loc.fill(name, { timeout: 5000 });
        logStep("name_filled", { selector: sel, name });
        return true;
      }
    } catch {
      continue;
    }
  }
  logStep("name_input_not_found", { tried_selectors: telemostNameInputSelectors });
  return false;
}

export async function joinYandexTelemost(
  page: Page,
  meetingUrl: string,
  botName: string,
  botConfig: BotConfig
): Promise<void> {
  logStep("join_start", { url: meetingUrl, name: botName });

  await page.goto(meetingUrl, { waitUntil: "domcontentloaded" });
  await page.bringToFront();
  logStep("page_loaded", { url: page.url() });

  try {
    await page.screenshot({ path: "/app/storage/screenshots/telemost-00-after-navigation.png", fullPage: true });
  } catch {}

  await callJoiningCallback(botConfig).catch((e: any) => log(`[telemost] joining-callback failed (non-fatal): ${e.message}`));

  // 1) Промежуточный экран «Продолжить в браузере». Не всегда появляется,
  // зависит от user-agent и наличия установленного desktop-приложения.
  await page.waitForTimeout(1500);
  const interstitialClicked = await tryClickFirstVisible(
    page,
    telemostInterstitialContinueSelectors,
    "interstitial_continue",
    8000
  );
  if (!interstitialClicked) {
    logStep("interstitial_skipped", { reason: "not_visible_or_already_past" });
  }

  // 2) Lobby — ждём появления поля имени.
  await page.waitForTimeout(2000);
  try {
    await page.screenshot({ path: "/app/storage/screenshots/telemost-01-lobby.png", fullPage: true });
  } catch {}

  const nameOk = await fillNameInput(page, botName);
  if (!nameOk) {
    // Это критично — без имени бот не сможет идентифицировать себя в комнате.
    throw new Error("Lobby: поле ввода имени не найдено в Telemost");
  }

  // 3) Mic / camera — НЕ кликаем. По дефолту они «выключены» (кнопки «Включить...»).
  // Проверка наличия — для лога / самопроверки.
  for (const sel of telemostMicButtonSelectors) {
    try {
      const loc = page.locator(sel).first();
      if (await loc.isVisible({ timeout: 300 })) {
        const label = await loc.getAttribute("data-testid").catch(() => null);
        logStep("mic_button_state", { testid: label });
        break;
      }
    } catch {}
  }
  for (const sel of telemostCameraButtonSelectors) {
    try {
      const loc = page.locator(sel).first();
      if (await loc.isVisible({ timeout: 300 })) {
        const label = await loc.getAttribute("data-testid").catch(() => null);
        logStep("camera_button_state", { testid: label });
        break;
      }
    } catch {}
  }

  // 4) Клик «Подключиться»
  const joined = await tryClickFirstVisible(page, telemostJoinButtonSelectors, "join_clicked", 15000);
  if (!joined) {
    throw new Error("Lobby: кнопка «Подключиться» не найдена в Telemost");
  }

  try {
    await page.screenshot({ path: "/app/storage/screenshots/telemost-02-after-join-click.png", fullPage: true });
  } catch {}

  logStep("join_done");
}
