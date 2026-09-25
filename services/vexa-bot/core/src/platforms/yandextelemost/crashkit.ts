// Аварийный снимок экрана при сорванном входе (23.09.2026, после ISS-59).
//
// ЗАЧЕМ. 21–23.09 разбор «почему бот не входит» занял часы: логи видели только
// главную страницу, а лобби Телемоста 3.0 переехало во вложенное окно. Теперь
// при любом сорванном входе бот сам снимает, ЧТО было на экране — во всех окнах
// страницы сразу: адреса окон, видимые кнопки (метка, подпись, текст), поля
// ввода (метка, тип, подсказка — БЕЗ введённых значений), диалоги, вложенные
// окна; плюс скриншот. Кладёт в
//   <TRANSCRIPT_DIR>/.crash-kits/<sessionUid>/{kit.json, screen.png}
// — это папка на хосте, она переживает удаление контейнера.
//
// Снимок никуда не отправляется: он лежит на сервере для разбора. Сравнение
// с последним зелёным снимком самопроверки делает canary.py.

import * as fs from "fs";
import * as path from "path";
import { Page } from "playwright";
import { log } from "../../utils";
import { joinSessionUid } from "./join-status";

const TRANSCRIPT_DIR = process.env.TELEMOST_TRANSCRIPT_DIR || "/transcripts";
export const CRASH_KITS_DIR = path.join(TRANSCRIPT_DIR, ".crash-kits");

export type FrameSnapshot = {
  url: string;
  title: string;
  buttons: Array<{ tid: string | null; aria: string | null; text: string; disabled: boolean }>;
  inputs: Array<{ tid: string | null; type: string | null; placeholder: string | null; aria: string | null }>;
  dialogs: Array<{ aria: string | null; buttons: string[] }>;
  iframes: string[];
  testids: string[];
  headings: string[];
};

// На одно окно — не дольше этого: зависшее окно не должно съесть весь снимок
// (по сигналу остановки на всё про всё у бота 20 с — stop-signal.ts).
const FRAME_SNAPSHOT_TIMEOUT_MS = 2500;

const EMPTY_FRAME = (url: string): FrameSnapshot => ({
  url, title: "", buttons: [], inputs: [], dialogs: [], iframes: [], testids: [], headings: [],
});

/** Снять видимую структуру ВСЕХ окон страницы. Никогда не бросает. */
export async function snapshotFrames(page: Page): Promise<FrameSnapshot[]> {
  const out: FrameSnapshot[] = [];
  for (const fr of page.frames()) {
    try {
      const pending: Promise<FrameSnapshot> = fr.evaluate(() => {
        const vis = (el: Element) => {
          const h = el as HTMLElement;
          return !!(h.offsetWidth || h.offsetHeight || h.getClientRects().length);
        };
        const cut = (s: string | null | undefined, n = 40) =>
          (s || "").replace(/\s+/g, " ").trim().slice(0, n);
        const strip = (u: string) => {
          try {
            const x = new URL(u, location.href);
            return x.origin + x.pathname;
          } catch {
            return "";
          }
        };
        const buttons = Array.from(document.querySelectorAll("button, [role='button'], a[role='button']"))
          .filter(vis)
          .slice(0, 80)
          .map((b) => ({
            tid: b.getAttribute("data-testid"),
            aria: b.getAttribute("aria-label"),
            text: cut((b as HTMLElement).innerText),
            disabled: (b as HTMLButtonElement).disabled === true || b.getAttribute("aria-disabled") === "true",
          }));
        const inputs = Array.from(document.querySelectorAll("input, textarea, [contenteditable='true']"))
          .filter(vis)
          .slice(0, 30)
          .map((i) => ({
            tid: i.getAttribute("data-testid"),
            type: i.getAttribute("type"),
            placeholder: i.getAttribute("placeholder"),
            aria: i.getAttribute("aria-label"),
          }));
        const dialogs = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true']"))
          .filter(vis)
          .slice(0, 10)
          .map((d) => ({
            aria: d.getAttribute("aria-label"),
            buttons: Array.from(d.querySelectorAll("button"))
              .filter(vis)
              .slice(0, 8)
              .map((b) => cut(b.getAttribute("aria-label") || (b as HTMLElement).innerText, 30)),
          }));
        const iframes = Array.from(document.querySelectorAll("iframe")).map((f) => strip((f as HTMLIFrameElement).src));
        const testids = Array.from(
          new Set(
            Array.from(document.querySelectorAll("[data-testid]"))
              .filter(vis)
              .map((e) => e.getAttribute("data-testid") || ""),
          ),
        )
          .filter(Boolean)
          .slice(0, 200);
        // Заголовки экрана («Такого звонка нет», «Подключение к звонку») — по
        // ним причину видно без скриншота.
        const headings = Array.from(document.querySelectorAll("h1, h2, h3, [role='heading']"))
          .filter(vis)
          .slice(0, 10)
          .map((h) => cut((h as HTMLElement).innerText, 60))
          .filter(Boolean);
        return { url: strip(location.href), title: cut(document.title, 80), buttons, inputs, dialogs, iframes, testids, headings };
      });
      const snap = await Promise.race([
        pending,
        new Promise<null>((r) => setTimeout(() => r(null), FRAME_SNAPSHOT_TIMEOUT_MS)),
      ]);
      out.push(snap || EMPTY_FRAME("(не ответило)"));
    } catch {
      out.push(EMPTY_FRAME("(недоступно)"));
    }
  }
  return out;
}

/**
 * Снять аварийный комплект. Возвращает путь к папке (как видит его контейнер)
 * или null. Никогда не бросает — снимок не должен мешать выходу бота.
 */
export async function captureCrashKit(page: Page | null, reason: string): Promise<string | null> {
  const uid = joinSessionUid();
  if (!page || !uid) return null;
  const dir = path.join(CRASH_KITS_DIR, uid);
  try {
    fs.mkdirSync(dir, { recursive: true });
    try {
      fs.chmodSync(CRASH_KITS_DIR, 0o777);
      fs.chmodSync(dir, 0o777);
    } catch { /* */ }
    const frames = await snapshotFrames(page);
    const kit = {
      reason,
      at: new Date().toISOString(),
      image_sha: process.env.NOTARY_BOT_GIT_SHA || null,
      frames,
    };
    fs.writeFileSync(path.join(dir, "kit.json"), JSON.stringify(kit, null, 2), "utf8");
    try { fs.chmodSync(path.join(dir, "kit.json"), 0o666); } catch { /* */ }
    try {
      await page.screenshot({ path: path.join(dir, "screen.png"), fullPage: true, timeout: 8000 });
      try { fs.chmodSync(path.join(dir, "screen.png"), 0o666); } catch { /* */ }
    } catch (e: any) {
      log(`[crash-kit] screenshot failed (non-fatal): ${e?.message}`);
    }
    log(`[crash-kit] saved reason=${reason} frames=${frames.length} dir=${dir}`);
    return dir;
  } catch (e: any) {
    log(`[crash-kit] failed (non-fatal): ${e?.message}`);
    return null;
  }
}
