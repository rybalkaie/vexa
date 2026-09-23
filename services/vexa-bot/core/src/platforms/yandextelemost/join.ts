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

import { Page, Frame } from "playwright";
import { log, callJoiningCallback } from "../../utils";
import { BotConfig } from "../../types";
import {
  telemostInterstitialContinueSelectors,
  telemostNameInputSelectors,
  telemostJoinButtonSelectors,
  telemostMicButtonSelectors,
  telemostCameraButtonSelectors,
  telemostModalDismissSelectors,
  telemostOnboardingDismissSelectors,
  telemostLobbyFrameUrlMarker,
  telemostCallMicOnSelectors,
} from "./selectors";

const LOG_PREFIX = "[adapter-telemost]";

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

async function tryClickFirstVisible(page: Page | Frame, selectors: string[], stepName: string, timeoutMs = 30000): Promise<boolean> {
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

/**
 * JS-level click через page.evaluate — обходит overlay-protection и
 * срабатывает в случаях, когда обычный Playwright click отрабатывает
 * визуально, но не триггерит реактивный handler (мы видели такое
 * на интерстициале «Продолжить в браузере» в Telemost).
 */
async function jsClickByText(page: Page | Frame, text: string, stepName: string): Promise<boolean> {
  try {
    const ok = await page.evaluate((needle: string) => {
      const buttons = Array.from(document.querySelectorAll("button"));
      const target = buttons.find(
        (b) => ((b as HTMLElement).innerText || "").trim() === needle ||
               ((b as HTMLElement).innerText || "").trim().includes(needle)
      ) as HTMLElement | undefined;
      if (!target) return false;
      target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
      target.click();
      return true;
    }, text);
    if (ok) {
      logStep(`${stepName}_jsclicked`, { text });
    } else {
      logStep(`${stepName}_jsclick_target_missing`, { text });
    }
    return ok;
  } catch (e: any) {
    logStep(`${stepName}_jsclick_failed`, { error: e.message });
    return false;
  }
}

/**
 * Гасит блокирующие модалки лобби, появившиеся у Telemost 2026-07-22 (регресс):
 * «Включить микрофон/видео не удалось» (у бота нет медиаустройств) и «Частые вопросы».
 * Они перекрывают форму лобби и прячут поле имени → без гашения заход падает
 * `name_input_not_found`. Кликаем «Понятно»/крестик (Playwright + JS-fallback как у
 * интерстициала, там тот же React-handler баг) и добиваем Escape. Идемпотентно и
 * безопасно: если модалок нет — ничего не делает. Возвращает число погашенных.
 */
async function dismissLobbyModals(page: Page): Promise<number> {
  let dismissed = 0;
  for (const sel of telemostModalDismissSelectors) {
    try {
      const btns = await page.locator(sel).all();
      for (const b of btns) {
        if (await b.isVisible({ timeout: 300 }).catch(() => false)) {
          await b.click({ timeout: 1500 }).catch(() => {});
          dismissed++;
        }
      }
    } catch {
      // следующий селектор
    }
  }
  // JS-fallback: Playwright click в Telemost иногда не триггерит React-handler.
  if (await jsClickByText(page, "Понятно", "dismiss_modal").catch(() => false)) {
    dismissed++;
  }
  // Добиваем generic-модалку клавишей Escape (безопасно — из лобби не выкидывает).
  await page.keyboard.press("Escape").catch(() => {});
  if (dismissed > 0) logStep("lobby_modals_dismissed", { count: dismissed });
  return dismissed;
}

async function fillNameInput(page: Page, name: string, timeoutMs = 25000, scope: Page | Frame = page): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    // Регресс 2026-07-22: новые модалки Telemost прячут поле имени — гасим каждый проход,
    // они могут появляться повторно (на каждую неудачную попытку доступа к медиа).
    // Модалки старого лобби гасим только на главной: в iframe лобби 3.0 их нет,
    // а Escape там мог бы закрыть само лобби.
    if (scope === page) await dismissLobbyModals(page).catch(() => {});
    for (const sel of telemostNameInputSelectors) {
      try {
        const loc = scope.locator(sel).first();
        if (await loc.isVisible({ timeout: 500 })) {
          await loc.click({ timeout: 2000 });
          await page.keyboard.press("Control+A").catch(() => {});
          await page.keyboard.press("Meta+A").catch(() => {});
          await page.keyboard.press("Delete").catch(() => {});
          await loc.fill(name, { timeout: 5000 });
          logStep("name_filled", { selector: sel, name, waited_ms: Date.now() - start });
          return true;
        }
      } catch {
        continue;
      }
    }
    await page.waitForTimeout(500);
  }
  // Диагностика при провале: скриншот лобби с непогашенными модалками.
  try {
    await page.screenshot({ path: "/app/vexa-bot/storage/screenshots/telemost-90-name-not-found.png", fullPage: true });
  } catch {}
  logStep("name_input_not_found", { tried_selectors: telemostNameInputSelectors, waited_ms: Date.now() - start });
  return false;
}

/** Телемост 3.0: закрыть окно онбординга «Большое обновление», если оно есть (≤10 с). */
async function closeOnboarding(page: Page): Promise<void> {
  for (let i = 0; i < 20; i++) {
    for (const sel of telemostOnboardingDismissSelectors) {
      const hit = await page.evaluate((s: string) => {
        const b = document.querySelector(s) as HTMLElement | null;
        if (!b || !(b.offsetWidth || b.offsetHeight)) return false;
        b.click();
        return true;
      }, sel).catch(() => false);
      if (hit) {
        logStep("onboarding_closed", { selector: sel });
        await page.waitForTimeout(800);
        return;
      }
    }
    await page.waitForTimeout(500);
  }
  logStep("onboarding_not_shown");
}

/** Телемост 3.0: найти iframe лобби (/private-join/) с кнопкой «Подключиться». */
async function findLobbyFrame(page: Page, timeoutMs = 15000): Promise<Frame | null> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const fr = page.frames().find((f) => f.url().includes(telemostLobbyFrameUrlMarker));
    if (fr) {
      const ready = await fr.locator(telemostJoinButtonSelectors[0]).first().isVisible({ timeout: 500 }).catch(() => false);
      if (ready) {
        logStep("lobby_frame_found", { url: fr.url().split("?")[0], waited_ms: Date.now() - start });
        return fr;
      }
    }
    await page.waitForTimeout(500);
  }
  return null;
}

/** Кнопка «Подключиться» пропала (или iframe ушёл) — значит, лобби позади. */
async function waitLobbyClosed(lobby: Frame, timeoutMs: number): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (lobby.isDetached()) return true;
    const visible = await lobby.locator(telemostJoinButtonSelectors[0]).first().isVisible({ timeout: 300 }).catch(() => false);
    if (!visible) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

/** Телемост 3.0: имя, микрофон/камера и «Подключиться» — внутри iframe лобби. */
async function joinViaLobbyFrame(page: Page, lobby: Frame, botName: string): Promise<void> {
  const nameOk = await fillNameInput(page, botName, 15000, lobby);
  if (!nameOk) {
    throw new Error("Lobby: поле ввода имени не найдено в Telemost (iframe лобби)");
  }

  // Гость в 3.0 заходит с ВКЛ микрофоном и камерой. Бот — наблюдатель: гасим оба
  // одним кликом (без JS-добивки: повторный клик включил бы обратно).
  for (const tid of ["turn-off-mic-button", "turn-off-camera-button"]) {
    const sel = `[data-testid="${tid}"]`;
    try {
      const btn = lobby.locator(sel).first();
      if (await btn.isVisible({ timeout: 500 })) {
        await btn.click({ timeout: 2000 });
        await new Promise((r) => setTimeout(r, 400));
        const still = await lobby.locator(sel).first().isVisible({ timeout: 300 }).catch(() => false);
        logStep("lobby_device_off", { testid: tid, still_visible: still });
      } else {
        logStep("lobby_device_already_off", { testid: tid });
      }
    } catch (e: any) {
      logStep("lobby_device_off_failed", { testid: tid, error: e.message });
    }
  }

  const joined = await tryClickFirstVisible(lobby, telemostJoinButtonSelectors, "join_clicked", 10000);
  if (!joined) {
    throw new Error("Lobby: кнопка «Подключиться» не найдена в Telemost (iframe лобби)");
  }

  // Лобби должно закрыться. Не закрылось — добиваем JS-кликом (React-handler
  // Телемоста не всегда ловит Playwright-клик) и ждём ещё.
  let left = await waitLobbyClosed(lobby, 6000);
  if (!left) {
    await lobby.evaluate((s: string) => { (document.querySelector(s) as HTMLElement | null)?.click(); }, telemostJoinButtonSelectors[0]).catch(() => {});
    logStep("join_testid_jsclicked");
    left = await waitLobbyClosed(lobby, 10000);
  }
  if (!left) {
    try {
      await page.screenshot({ path: "/app/vexa-bot/storage/screenshots/telemost-91-still-in-lobby.png", fullPage: true });
    } catch {}
    throw new Error("Lobby: после «Подключиться» бот остался в лобби Telemost");
  }
  logStep("lobby_left");

  // Страховка: микрофон в звонке всё же включён — выключаем (виджет звонка на
  // главной странице, aria-label однозначно говорит о состоянии).
  await page.waitForTimeout(1500);
  for (const sel of telemostCallMicOnSelectors) {
    try {
      const b = page.locator(sel).first();
      if (await b.isVisible({ timeout: 500 })) {
        await b.click({ timeout: 2000 });
        logStep("call_mic_muted", { selector: sel });
      }
    } catch {}
  }

  try {
    await page.screenshot({ path: "/app/vexa-bot/storage/screenshots/telemost-02-after-join-click.png", fullPage: true });
  } catch {}
  logStep("join_done", { via: "lobby_iframe" });
}

export async function joinYandexTelemost(
  page: Page,
  meetingUrl: string,
  botName: string,
  botConfig: BotConfig
): Promise<void> {
  logStep("join_start", { url: meetingUrl, name: botName });

  // Телемост 3.0 может играть звук не через <audio>, а через WebAudio — ловим
  // входящие аудио-дорожки прямо у RTCPeerConnection (во всех фреймах, включая
  // iframe звонка). Запись (recording.ts, rescan) подмешивает их вместе с
  // дорожками из DOM; дубли отсекаются по track.id.
  await page.addInitScript(() => {
    const w = window as any;
    if (w.__vexaRtcHooked || !w.RTCPeerConnection) return;
    w.__vexaRtcHooked = true;
    w.__vexaRemoteAudioTracks = [];
    const Orig = w.RTCPeerConnection;
    const Hooked: any = function (...args: any[]) {
      const pc = new Orig(...args);
      pc.addEventListener("track", (e: any) => {
        try {
          if (e.track && e.track.kind === "audio") w.__vexaRemoteAudioTracks.push(e.track);
        } catch { /* */ }
      });
      return pc;
    };
    Hooked.prototype = Orig.prototype;
    Object.setPrototypeOf(Hooked, Orig);
    w.RTCPeerConnection = Hooked;
  }).catch((e: any) => logStep("rtc_hook_failed", { error: e.message }));

  await page.goto(meetingUrl, { waitUntil: "domcontentloaded" });
  await page.bringToFront();
  logStep("page_loaded", { url: page.url() });

  try {
    await page.screenshot({ path: "/app/vexa-bot/storage/screenshots/telemost-00-after-navigation.png", fullPage: true });
  } catch {}

  await callJoiningCallback(botConfig).catch((e: any) => log(`[telemost] joining-callback failed (non-fatal): ${e.message}`));

  // 1) Промежуточный экран «Продолжить в браузере». Не всегда появляется,
  // зависит от user-agent и наличия установленного desktop-приложения.
  await page.waitForTimeout(1500);
  // Телемост 3.0: окно онбординга перекрывает «Продолжить в браузере» — закрываем.
  await closeOnboarding(page);

  const interstitialClicked = await tryClickFirstVisible(
    page,
    telemostInterstitialContinueSelectors,
    "interstitial_continue",
    8000
  );
  if (interstitialClicked) {
    // Дополнительно дёргаем JS-клик: Playwright click в Telemost иногда
    // не триггерит React handler (видели на VPS — URL остаётся прежним).
    await jsClickByText(page, "Продолжить в браузере", "interstitial_continue");
  } else {
    logStep("interstitial_skipped", { reason: "not_visible_or_already_past" });
  }

  // 2) Lobby — ждём появления поля имени.
  await page.waitForTimeout(3000);

  // Диагностика: дамп state — URL, title, testids, видимый текст.
  try {
    const diag = await page.evaluate(() => {
      const testids = Array.from(document.querySelectorAll("[data-testid]"))
        .slice(0, 40)
        .map((el) => ({
          testid: el.getAttribute("data-testid"),
          tag: el.tagName,
          text: ((el as HTMLElement).innerText || "").trim().substring(0, 50),
        }));
      const visibleText = (document.body.innerText || "").substring(0, 600);
      return { url: location.href, title: document.title, testids, visibleText };
    });
    logStep("post_interstitial_state", { url: diag.url, title: diag.title });
    log(`${LOG_PREFIX} post_interstitial_testids ${JSON.stringify(diag.testids)}`);
    log(`${LOG_PREFIX} post_interstitial_visible_text ${JSON.stringify(diag.visibleText)}`);
  } catch (e: any) {
    logStep("post_interstitial_diag_failed", { error: e.message });
  }

  // Телемост 3.0: лобби во вложенном iframe. Нашли — заходим через него,
  // не нашли — старый путь по главной странице.
  const lobbyFrame = await findLobbyFrame(page);
  if (lobbyFrame) {
    await joinViaLobbyFrame(page, lobbyFrame, botName);
    return;
  }
  logStep("lobby_frame_not_found", { fallback: "main_page" });

  const nameOk = await fillNameInput(page, botName);
  if (!nameOk) {
    // Это критично — без имени бот не сможет идентифицировать себя в комнате.
    throw new Error("Lobby: поле ввода имени не найдено в Telemost");
  }

  // 3) Mic / camera. ВАЖНО: в Vexa-контейнере браузеру дают permissions автоматом,
  // и Telemost включает микрофон по дефолту (testid становится `turn-off-mic-button`).
  // Бот — наблюдатель, ему вход с открытым мик неприемлем. Кликаем по нему чтобы вырубить.
  // Та же React-handler проблема как у `enter-conference-button` — Playwright click может
  // не сработать, нужен JS-fallback с явной верификацией.
  // Мьютим mic, если он включён. ВАЖНО: один click (Playwright) — иначе двойной
  // click может вернуть state обратно (toggle). Если после ожидания состояние
  // не изменилось — пробуем JS-fallback одним вызовом.
  try {
    const turnOff = '[data-testid="turn-off-mic-button"]';
    const offBtn = page.locator(turnOff).first();
    if (await offBtn.isVisible({ timeout: 500 })) {
      await offBtn.click({ timeout: 2000 }).catch(() => {});
      await page.waitForTimeout(500);
      const stillOn = await page.locator(turnOff).first().isVisible({ timeout: 300 }).catch(() => false);
      if (stillOn) {
        // Playwright click не сработал — JS-fallback ровно один раз.
        await page.evaluate((sel: string) => {
          const btn = document.querySelector(sel) as HTMLElement | null;
          if (btn) {
            btn.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
            btn.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
            btn.click();
          }
        }, turnOff).catch(() => {});
        await page.waitForTimeout(500);
        const stillOnAfterJs = await page.locator(turnOff).first().isVisible({ timeout: 300 }).catch(() => false);
        logStep("mic_muted_in_lobby", { method: "js_fallback", still_unmuted: stillOnAfterJs });
      } else {
        logStep("mic_muted_in_lobby", { method: "playwright_click", still_unmuted: false });
      }
    } else {
      logStep("mic_already_off");
    }
  } catch (e: any) {
    logStep("mic_mute_failed", { error: e.message });
  }
  // Камера для лога — не кликаем (по дефолту off).
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

  // 4) Клик «Подключиться» — с JS fallback (как у интерстициала, тот же баг).
  const joined = await tryClickFirstVisible(page, telemostJoinButtonSelectors, "join_clicked", 15000);
  if (!joined) {
    throw new Error("Lobby: кнопка «Подключиться» не найдена в Telemost");
  }
  await jsClickByText(page, "Подключиться", "join_clicked");
  // Доп. подстраховка — если есть data-testid="enter-conference-button", дёргаем явно через JS.
  try {
    await page.evaluate(() => {
      const btn = document.querySelector('[data-testid="enter-conference-button"]') as HTMLElement | null;
      if (btn) {
        btn.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        btn.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
        btn.click();
      }
    });
    logStep("join_testid_jsclicked");
  } catch (e: any) {
    logStep("join_testid_jsclick_failed", { error: e.message });
  }

  try {
    await page.screenshot({ path: "/app/vexa-bot/storage/screenshots/telemost-02-after-join-click.png", fullPage: true });
  } catch {}

  logStep("join_done");
}
