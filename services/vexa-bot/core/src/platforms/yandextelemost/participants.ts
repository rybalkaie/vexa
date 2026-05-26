// Yandex Telemost: сбор имён участников встречи для маппинга спикеров.
//
// Логика Ф3:
//   1) Раз в N секунд (по умолчанию 60s) открываем панель «Участники»
//      кликом по [data-testid="participants-button"].
//   2) Снимаем DOM-snapshot — пытаемся выделить имена участников по
//      нескольким эвристикам (точный селектор тайла-участника на Ф2 не
//      разведан, поэтому подход — несколько fallback'ов + дамп DOM
//      на первом прогоне).
//   3) Закрываем панель (Escape) — иначе она оверлеит meeting tiles
//      и мешает видимости конца встречи.
//   4) Merge имён в глобальный Set. Финальный список передаётся
//      post-processing скрипту через meta.json.
//
// Бот сам — участник, фильтруется по совпадению с botName.
//
// Конфиденциальность: имена пишутся в meta.json + в логи (для разведки
// на первом прогоне). НЕ являются «опасной тройкой» — это структурный
// метаданный, не реплики транскрипта.

import { Page } from "playwright";
import { log } from "../../utils";
import {
  telemostParticipantsButtonSelectors,
  telemostParticipantsPanelSelectors,
} from "./selectors";

const LOG_PREFIX = "[adapter-telemost-participants]";

const PARTICIPANTS_POLL_INTERVAL_MS = 60_000;
const PARTICIPANTS_POLL_DELAY_AFTER_CLICK_MS = 1500;

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

// Простая эвристика — выглядит ли строка как имя человека.
// Имя: 2-40 символов, начинается с буквы (кириллица/латиница), не содержит
// слов из stoplist'а интерфейса.
function looksLikeName(s: string): boolean {
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

async function openParticipantsPanel(page: Page): Promise<boolean> {
  for (const sel of telemostParticipantsButtonSelectors) {
    try {
      const loc = page.locator(sel).first();
      if (await loc.isVisible({ timeout: 500 })) {
        // Playwright click + JS-fallback (тот же React-handler-issue из Ф2).
        await loc.click({ timeout: 2000 }).catch(() => {});
        await page.evaluate((s: string) => {
          const btn = document.querySelector(s) as HTMLElement | null;
          if (btn) {
            btn.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
            btn.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
            btn.click();
          }
        }, sel).catch(() => {});
        logStep("participants_panel_opened", { selector: sel });
        return true;
      }
    } catch {}
  }
  return false;
}

async function closeParticipantsPanel(page: Page): Promise<void> {
  // Самый надёжный способ — Escape.
  try {
    await page.keyboard.press("Escape").catch(() => {});
  } catch {}
}

async function snapshotPanelDomForDiscovery(page: Page): Promise<void> {
  try {
    const dump = await page.evaluate((panelSelectors: string[]) => {
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
          text: ((el as HTMLElement).innerText || "").trim().substring(0, 80),
          aria: el.getAttribute("aria-label"),
        }));
      return {
        panel_found: panel !== null,
        url: location.href,
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

async function extractNamesFromPanel(page: Page, botName: string): Promise<string[]> {
  try {
    const result = await page.evaluate(
      (args: { panelSelectors: string[]; botName: string }) => {
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
          const all = Array.from(panel.querySelectorAll("span, div, p"));
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
      { panelSelectors: telemostParticipantsPanelSelectors, botName }
    );

    if (!result.panel_found) {
      logStep("panel_not_found_after_open");
    }

    // Фильтр на стороне Node: убираем бота, дубликаты, не-имена.
    const out: string[] = [];
    const seen = new Set<string>();
    for (const raw of result.raw_names) {
      const cleaned = raw.replace(/\s+/g, " ").trim();
      if (!cleaned) continue;
      // Бот фильтруется по подстроке.
      if (botName && cleaned.toLowerCase().includes(botName.toLowerCase().substring(0, 8))) continue;
      if (cleaned.toLowerCase().includes("протокол встречи")) continue;
      if (!looksLikeName(cleaned)) continue;
      if (seen.has(cleaned)) continue;
      seen.add(cleaned);
      out.push(cleaned);
    }
    return out;
  } catch (e: any) {
    logStep("extract_names_failed", { error: e.message });
    return [];
  }
}

/**
 * Стартует периодический polling списка участников.
 * Возвращает stopper + getNames (текущий снимок Set).
 */
export function startParticipantsPolling(
  page: Page,
  botName: string,
  onUpdate?: (names: string[]) => void
): { stop: () => void; getNames: () => string[] } {
  const collected = new Set<string>();
  let stopped = false;
  let domSnapshotDone = false;

  const poll = async () => {
    if (stopped) return;
    try {
      const opened = await openParticipantsPanel(page);
      if (!opened) {
        logStep("participants_button_not_found");
        return;
      }
      await page.waitForTimeout(PARTICIPANTS_POLL_DELAY_AFTER_CLICK_MS);

      // На первом успешном открытии — снимаем dump панели для разведки селекторов.
      if (!domSnapshotDone) {
        await snapshotPanelDomForDiscovery(page);
        domSnapshotDone = true;
      }

      const names = await extractNamesFromPanel(page, botName);
      let changed = false;
      for (const n of names) {
        if (!collected.has(n)) {
          collected.add(n);
          changed = true;
        }
      }
      logStep("participants_polled", {
        found: names.length,
        total_unique: collected.size,
        new: changed,
      });
      if (changed && onUpdate) {
        try {
          onUpdate(Array.from(collected));
        } catch {}
      }

      await closeParticipantsPanel(page);
    } catch (e: any) {
      logStep("participants_poll_failed", { error: e.message });
    }
  };

  // Первый poll — после задержки, чтобы UI устаканился после admission.
  const initialDelay = setTimeout(() => {
    if (!stopped) poll();
  }, 5000);

  const interval = setInterval(() => {
    if (!stopped) poll();
  }, PARTICIPANTS_POLL_INTERVAL_MS);

  return {
    stop: () => {
      stopped = true;
      clearTimeout(initialDelay);
      clearInterval(interval);
      logStep("participants_polling_stopped", { final_count: collected.size });
    },
    getNames: () => Array.from(collected),
  };
}
