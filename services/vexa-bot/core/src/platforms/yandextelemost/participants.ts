// Yandex Telemost: сбор имён участников встречи для маппинга спикеров.
//
// 25.09.2026 (фаза 3а, ISS-60, хвост ISS-59). В Телемосте 3.0 кнопки панели
// «Участники» бот не находит (`participants_button_not_found`), список был пуст —
// а от него зависят детекторы «оглох» (recording.ts): бот уходил с
// `no_one_joined`, имена в протокол не попадали. Живая разведка 25.09 (Т-10):
// участники видны БЕЗ панели —
//   а) имена на плитках во вложенном окне звонка `/private-join/`
//      (`[class*="participantName_"]`, свою плитку бот тоже видит);
//   б) счётчик «N участников» в мини-виджете главного окна
//      (aria-label «Открыть экран звонка»).
// Теперь это основной путь (раз в 15 с, без нажатий). Панель — запасной путь
// (старый интерфейс), только если ни плиток, ни счётчика нет, не чаще раза в
// минуту, и ищется во всех окнах, а не только на главной.
//
// Кроме накопленного списка имён (он уходит в meta.json) — «сколько людей в
// звонке сейчас, кроме бота» (getPresence): по нему recording.ts решает, «никто
// не пришёл» это или «оглох при людях», и не поднимает ложную тревогу «оглох»,
// когда люди просто ушли.
//
// Бот сам — участник, фильтруется по совпадению с botName.
//
// Конфиденциальность: имена пишутся в meta.json. В лог — только числа.
// НЕ являются «опасной тройкой» — это структурный метаданный, не реплики.

import { Frame, Page } from "playwright";
import { log } from "../../utils";
import {
  telemostParticipantsButtonSelectors,
  telemostParticipantsPanelSelectors,
  telemostParticipantNameSelectors,
  telemostCallWidgetAria,
} from "./selectors";
import { frameKind, orderedFrames, parseParticipantCount } from "./screen";

const LOG_PREFIX = "[adapter-telemost-participants]";

const TILES_POLL_INTERVAL_MS = 15_000;
const PANEL_POLL_MIN_INTERVAL_MS = 60_000;
// Панель — только если плиток и счётчика не видно давно: сразу после входа
// плитки ещё рисуются, а на миг пропавшая плитка не повод лезть в панель
// (в 3.0 кнопки панели нет — в лог шёл бы ложный `participants_button_not_found`).
const PANEL_FIRST_AFTER_MS = 30_000;
const TILES_TRUST_MS = 120_000;
const PARTICIPANTS_POLL_DELAY_AFTER_CLICK_MS = 1500;
const FIRST_POLL_DELAY_MS = 5000;

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

// Простая эвристика — выглядит ли строка как имя человека.
// Имя: 2-40 символов, начинается с буквы (кириллица/латиница), не содержит
// слов из stoplist'а интерфейса.
export function looksLikeName(s: string): boolean {
  const trimmed = s.trim();
  if (trimmed.length < 2 || trimmed.length > 40) return false;
  if (!/^[\p{L}]/u.test(trimmed)) return false;
  const lowered = trimmed.toLowerCase();
  const stopWords = [
    "участник", "участники", "ждать", "ожидание", "выйти", "пригласить",
    "поиск", "закрыть", "копировать", "ссылка", "вы", "хост",
    "микрофон", "камера", "звук", "видео", "чат", "сообщение",
    "participant", "participants", "host", "you", "search", "close",
    "mute", "unmute", "leave", "demonstration", "share",
  ];
  for (const w of stopWords) {
    if (lowered === w || lowered.startsWith(w + " ") || lowered.endsWith(" " + w)) return false;
  }
  // отсечь строки с цифрами и техническими знаками
  if (/[<>{}|\\\/\[\]=]/.test(trimmed)) return false;
  return true;
}

/**
 * Сырые строки → имена людей: без бота, без «протокол встречи», без мусора
 * интерфейса, без повторов — ЧИСТАЯ функция (тест: call-ui.test.ts).
 */
export function cleanParticipantNames(raw: string[], botName: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const botKey = (botName || "").toLowerCase().substring(0, 8);
  for (const r of raw) {
    const cleaned = String(r || "").replace(/\s+/g, " ").trim();
    if (!cleaned) continue;
    // Бот фильтруется по подстроке.
    if (botKey && cleaned.toLowerCase().includes(botKey)) continue;
    if (cleaned.toLowerCase().includes("протокол встречи")) continue;
    if (!looksLikeName(cleaned)) continue;
    if (seen.has(cleaned)) continue;
    seen.add(cleaned);
    out.push(cleaned);
  }
  return out;
}

/**
 * Сколько людей в звонке сейчас, КРОМЕ бота — ЧИСТАЯ функция.
 * `names` — имена с плиток уже без бота; `count` — счётчик виджета (с ботом);
 * `tiles` — сколько плиток с именем в окне звонка (своя плитка бота тоже там).
 * Источники расходятся → берём большее: ошибиться в сторону «люди есть» дешевле
 * (бот подождёт звука), чем уйти с живой встречи как «никто не пришёл».
 * Источник не найден вовсе → null («не знаю», не ноль).
 */
export function presenceFromTiles(names: string[], count: number | null, sourceFound: boolean, tiles = 0): number | null {
  if (!sourceFound) return null;
  const byCount = count !== null ? Math.max(0, count - 1) : 0;
  const byTiles = Math.max(0, tiles - 1);
  return Math.max(names.length, byCount, byTiles);
}

/**
 * Пора ли заглянуть в панель «Участники» (запасной путь) — ЧИСТАЯ функция.
 * Не раньше PANEL_FIRST_AFTER_MS от старта опроса, не чаще раза в минуту и не
 * раньше чем через TILES_TRUST_MS после последних увиденных плиток/счётчика.
 */
export function panelDue(now: number, startedAt: number, lastPanelAt: number, lastTilesAt: number): boolean {
  if (now - startedAt < PANEL_FIRST_AFTER_MS) return false;
  if (lastPanelAt && now - lastPanelAt < PANEL_POLL_MIN_INTERVAL_MS) return false;
  if (lastTilesAt && now - lastTilesAt < TILES_TRUST_MS) return false;
  return true;
}

export type TileSnapshot = { rawNames: string[]; count: number | null; tilesSeen: number; sourceFound: boolean };

/** Имена с плиток (окно звонка 3.0) и счётчик виджета (главное окно). Не бросает. */
export async function collectTileParticipants(page: Page): Promise<TileSnapshot> {
  const snap: TileSnapshot = { rawNames: [], count: null, tilesSeen: 0, sourceFound: false };
  if (!page || page.isClosed()) return snap;
  for (const fr of orderedFrames(page)) {
    if (fr.isDetached()) continue;
    const isCall = frameKind(page, fr) === "call";
    try {
      const r = await Promise.race([
        fr.evaluate(
          (a: { nameSel: string; widgetAria: string; isCall: boolean }) => {
            const names: string[] = [];
            let tiles = 0;
            if (a.isCall) {
              for (const el of Array.from(document.querySelectorAll(a.nameSel))) {
                tiles += 1;
                const t = (((el as HTMLElement).innerText || el.textContent || "") + "").trim();
                if (t) names.push(t.slice(0, 120));
              }
            }
            const widgets = Array.from(document.querySelectorAll("[aria-label]"))
              .filter((e) => e.getAttribute("aria-label") === a.widgetAria)
              .map((e) => (((e as HTMLElement).innerText || e.textContent || "") + "").slice(0, 200));
            return { names: names.slice(0, 200), tiles, widgets: widgets.slice(0, 5) };
          },
          { nameSel: telemostParticipantNameSelectors[0], widgetAria: telemostCallWidgetAria, isCall },
        ),
        new Promise<null>((res) => setTimeout(() => res(null), 2500)),
      ]);
      if (!r) continue;
      if (r.tiles > 0) {
        snap.tilesSeen += r.tiles;
        snap.rawNames.push(...r.names);
        snap.sourceFound = true;
      }
      for (const w of r.widgets) {
        const n = parseParticipantCount(w);
        if (n !== null) {
          snap.count = snap.count === null ? n : Math.max(snap.count, n);
          snap.sourceFound = true;
        }
      }
    } catch {
      // окно ушло или чужой origin — дальше
    }
  }
  return snap;
}

// ---------------------------------------------------------------------------
// Запасной путь: панель «Участники» (старый интерфейс) — во всех окнах.
// ---------------------------------------------------------------------------

async function openParticipantsPanel(page: Page): Promise<Frame | null> {
  for (const fr of orderedFrames(page)) {
    if (fr.isDetached()) continue;
    for (const sel of telemostParticipantsButtonSelectors) {
      try {
        const loc = fr.locator(sel).first();
        if (await loc.isVisible({ timeout: 500 })) {
          // Playwright click + JS-fallback (тот же React-handler-issue из Ф2).
          await loc.click({ timeout: 2000 }).catch(() => {});
          await fr.evaluate((s: string) => {
            const btn = document.querySelector(s) as HTMLElement | null;
            if (btn) {
              btn.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
              btn.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
              btn.click();
            }
          }, sel).catch(() => {});
          logStep("participants_panel_opened", { selector: sel, frame: frameKind(page, fr) });
          return fr;
        }
      } catch {}
    }
  }
  return null;
}

async function closeParticipantsPanel(page: Page): Promise<void> {
  // Самый надёжный способ — Escape.
  try {
    await page.keyboard.press("Escape").catch(() => {});
  } catch {}
}

async function snapshotPanelDomForDiscovery(fr: Frame): Promise<void> {
  try {
    const dump = await fr.evaluate((panelSelectors: string[]) => {
      // Пробуем найти открытую панель по гипотезам.
      let panel: Element | null = null;
      for (const sel of panelSelectors) {
        const found = Array.from(document.querySelectorAll(sel)).find((el) => {
          const r = (el as HTMLElement).getBoundingClientRect();
          const cs = getComputedStyle(el as HTMLElement);
          return r.width > 200 && r.height > 100 && cs.display !== "none" && cs.visibility !== "hidden";
        });
        if (found) {
          panel = found;
          break;
        }
      }
      // Если панель не нашлась — дампим все видимые testid'ы из правой части экрана
      // (Orb обычно ставит панели справа).
      const scope: Element = panel || document.body;
      const items = Array.from(scope.querySelectorAll("[data-testid]"))
        .slice(0, 60)
        .map((el) => ({
          testid: el.getAttribute("data-testid"),
          tag: el.tagName,
          aria: el.getAttribute("aria-label"),
        }));
      return {
        panel_found: panel !== null,
        items,
      };
    }, telemostParticipantsPanelSelectors);

    logStep("panel_dom_snapshot", {
      panel_found: dump.panel_found,
      item_count: dump.items.length,
    });
    const batches: typeof dump.items[] = [];
    for (let i = 0; i < dump.items.length; i += 10) batches.push(dump.items.slice(i, i + 10));
    batches.forEach((batch, idx) => {
      log(`${LOG_PREFIX} panel_dom_batch=${idx} items=${JSON.stringify(batch)}`);
    });
  } catch (e: any) {
    logStep("panel_dom_snapshot_failed", { error: e.message });
  }
}

async function extractNamesFromPanel(fr: Frame, botName: string): Promise<string[]> {
  try {
    const result = await fr.evaluate(
      (args: { panelSelectors: string[] }) => {
        const panelSelectors = args.panelSelectors;
        let panel: Element | null = null;
        for (const sel of panelSelectors) {
          const found = Array.from(document.querySelectorAll(sel)).find((el) => {
            const r = (el as HTMLElement).getBoundingClientRect();
            const cs = getComputedStyle(el as HTMLElement);
            return r.width > 200 && r.height > 100 && cs.display !== "none" && cs.visibility !== "hidden";
          });
          if (found) {
            panel = found;
            break;
          }
        }
        const scope: Element = panel || document.body;

        const names: string[] = [];

        // Подход 1: точные testid'ы для участника-в-списке.
        const itemSelectors = [
          '[data-testid*="participant-item"]',
          '[data-testid*="participant-row"]',
          '[data-testid*="participant-name"]',
          '[data-testid*="user-item"]',
          '[data-testid*="member-item"]',
        ];
        for (const sel of itemSelectors) {
          const els = Array.from(scope.querySelectorAll(sel));
          for (const el of els) {
            const t = ((el as HTMLElement).innerText || "").trim();
            if (t) names.push(t);
          }
          if (names.length > 0) break;
        }

        // Подход 2: role-based (Orb может использовать ARIA).
        if (names.length === 0) {
          const els = Array.from(scope.querySelectorAll('[role="listitem"], [role="option"]'));
          for (const el of els) {
            const t = ((el as HTMLElement).innerText || "").trim();
            if (t) names.push(t);
          }
        }

        // Подход 3: fallback — собрать все короткие текст-узлы внутри панели.
        // Имена обычно лежат в <span> / <div> уровне 2-3 в глубину от panel.
        if (names.length === 0 && panel) {
          const all = Array.from((panel as Element).querySelectorAll("span, div, p"));
          const seen = new Set<string>();
          for (const el of all) {
            // Берём узлы без дочерних span/div (листы) с коротким текстом.
            const children = (el as HTMLElement).children;
            const isLeaf = Array.from(children).every((c) => !["SPAN", "DIV", "P"].includes(c.tagName));
            if (!isLeaf) continue;
            const t = ((el as HTMLElement).innerText || "").trim();
            if (!t || t.length < 2 || t.length > 60) continue;
            if (seen.has(t)) continue;
            seen.add(t);
            names.push(t);
          }
        }

        return { panel_found: panel !== null, raw_names: names };
      },
      { panelSelectors: telemostParticipantsPanelSelectors }
    );

    if (!result.panel_found) {
      logStep("panel_not_found_after_open");
    }
    return cleanParticipantNames(result.raw_names, botName);
  } catch (e: any) {
    logStep("extract_names_failed", { error: e.message });
    return [];
  }
}

export type Presence = { others: number | null; at: number | null; source: "tiles" | "panel" | null };

/**
 * Стартует периодический polling списка участников.
 * Возвращает stopper, getNames (накопленные имена за встречу) и getPresence
 * (сколько людей кроме бота видно сейчас — по последнему удачному опросу).
 */
export function startParticipantsPolling(
  page: Page,
  botName: string,
  onUpdate?: (names: string[]) => void
): { stop: () => void; getNames: () => string[]; getPresence: () => Presence } {
  const collected = new Set<string>();
  let stopped = false;
  let domSnapshotDone = false;
  const startedAt = Date.now();
  let lastPanelAt = 0;
  let lastTilesAt = 0;
  let running = false;
  let presence: Presence = { others: null, at: null, source: null };
  let lastLogged = "";

  const merge = (names: string[]): boolean => {
    let changed = false;
    for (const n of names) {
      if (!collected.has(n)) {
        collected.add(n);
        changed = true;
      }
    }
    if (changed && onUpdate) {
      try {
        onUpdate(Array.from(collected));
      } catch {}
    }
    return changed;
  };

  const pollPanel = async (): Promise<void> => {
    lastPanelAt = Date.now();
    const fr = await openParticipantsPanel(page);
    if (!fr) {
      logStep("participants_button_not_found");
      return;
    }
    // На любом шаге между awaits встреча могла закончиться → страница закрылась.
    // Проверяем перед каждым page-op'ом — иначе unhandled rejection ("page closed").
    if (stopped || page.isClosed()) return;
    await page.waitForTimeout(PARTICIPANTS_POLL_DELAY_AFTER_CLICK_MS);
    if (stopped || page.isClosed()) return;
    if (!domSnapshotDone) {
      await snapshotPanelDomForDiscovery(fr);
      domSnapshotDone = true;
    }
    if (stopped || page.isClosed()) return;
    const names = await extractNamesFromPanel(fr, botName);
    const changed = merge(names);
    presence = { others: names.length, at: Date.now(), source: "panel" };
    logStep("participants_polled", { source: "panel", found: names.length, total_unique: collected.size, new: changed });
    if (stopped || page.isClosed()) return;
    await closeParticipantsPanel(page);
  };

  const poll = async () => {
    if (stopped || page.isClosed() || running) return;
    running = true;
    try {
      const snap = await collectTileParticipants(page);
      if (stopped || page.isClosed()) return;
      if (snap.sourceFound) {
        lastTilesAt = Date.now();
        const names = cleanParticipantNames(snap.rawNames, botName);
        const changed = merge(names);
        const others = presenceFromTiles(names, snap.count, true, snap.tilesSeen);
        presence = { others, at: Date.now(), source: "tiles" };
        // В лог — только числа и только когда что-то поменялось.
        const key = `${names.length}|${snap.count}|${snap.tilesSeen}|${collected.size}`;
        if (changed || key !== lastLogged) {
          lastLogged = key;
          logStep("participants_polled", {
            source: "tiles", found: names.length, tiles: snap.tilesSeen, count: snap.count,
            others, total_unique: collected.size, new: changed,
          });
        }
        return;
      }
      // Плиток и счётчика нет (старый интерфейс) — панель, не чаще раза в минуту.
      if (panelDue(Date.now(), startedAt, lastPanelAt, lastTilesAt)) {
        await pollPanel();
      }
    } catch (e: any) {
      // page closed mid-poll — типично, не fatal. Логируем для диагностики.
      logStep("participants_poll_failed", { error: e.message });
    } finally {
      running = false;
    }
  };

  // Первый poll — после задержки, чтобы UI устаканился после admission.
  const initialDelay = setTimeout(() => {
    if (!stopped) poll();
  }, FIRST_POLL_DELAY_MS);

  const interval = setInterval(() => {
    if (!stopped) poll();
  }, TILES_POLL_INTERVAL_MS);

  return {
    stop: () => {
      stopped = true;
      clearTimeout(initialDelay);
      clearInterval(interval);
      logStep("participants_polling_stopped", { final_count: collected.size });
    },
    getNames: () => Array.from(collected),
    getPresence: () => presence,
  };
}
