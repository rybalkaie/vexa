// Снимок экрана по сигналу остановки (хвост У8, фаза 3а плана
// plans/2026-09-23-telemost-update-resilience.md, 25.09.2026).
//
// ЗАЧЕМ. Сторож на хосте (scripts/notary/lib/join_watch.py) останавливает
// зависшего на входе бота (`docker stop -t 20`). Раньше SIGTERM до node не
// доходил вовсе (первым процессом в образе был bash без exec), через 20 с
// docker добивал бота, и от зависания оставался только лог — без экрана.
// Теперь entrypoint.sh запускает node через exec, сигнал доходит, и бот успевает
// снять аварийный снимок (crashkit.ts), а потом выходит штатно.
//
// КАК. Слушатель сигнала ставится ПЕРВЫМ (prependListener) и лишь запоминает
// момент — синхронно. Сам снимок снимает leaveYandexTelemost (leave.ts): его
// вызывает штатный выход index.ts раньше, чем закрывает страницу, так что снимок
// и выход идут по очереди, а не наперегонки. На снимок — не больше
// STOP_SNAPSHOT_BUDGET_MS: у docker на всё 20 с, выйти из встречи важнее.
//
// Только пока бот ещё НЕ был в звонке (входит / ждёт впуска / в зале ожидания):
// снимок живого звонка — это лица и имена людей, для разбора не нужен. Бот,
// который уже был в звонке и перезаходит после выброса, тоже не снимается.

import { Page } from "playwright";
import { log } from "../../utils";
import { captureCrashKit } from "./crashkit";
import { currentJoinPhase, joinPhaseSeen, writeJoinStatus, JoinPhase } from "./join-status";

const STOP_SNAPSHOT_BUDGET_MS = 12_000;

/**
 * Параметры запуска браузера бота (index.ts, Телемост): сигналы остановки ловит
 * сам бот. По умолчанию Playwright на SIGTERM/SIGINT тут же закрывает браузер
 * сам — раньше, чем штатный выход успеет снять снимок и нажать «выйти»
 * (проверено тестом screen-copies.test.ts: без этого снимка и выхода нет).
 * Браузер всё равно закроется: штатный выход закрывает его сам, а при выходе
 * процесса Playwright добивает браузер своим обработчиком `exit`.
 */
export const BROWSER_SIGNAL_OPTIONS = { handleSIGTERM: false, handleSIGINT: false } as const;

let stopAt: number | null = null;
let armed = false;
let captured = false;

/** Нужен ли снимок по сигналу остановки — ЧИСТАЯ функция. */
export function shouldCaptureOnStop(phase: string | null, everInCall = false): boolean {
  if (everInCall) return false;
  return phase === "joining" || phase === "admission" || phase === "waiting_room";
}

/** Поставить слушатель сигналов остановки (один раз на процесс). */
export function armStopSignalCapture(): void {
  if (armed) return;
  armed = true;
  const mark = (): void => {
    if (stopAt === null) stopAt = Date.now();
  };
  process.prependListener("SIGTERM", mark);
  process.prependListener("SIGINT", mark);
}

/** Пришёл ли сигнал остановки. */
export function stopSignalReceived(): boolean {
  return stopAt !== null;
}

/**
 * Снять аварийный снимок, если пришёл сигнал остановки и бот ещё не был в
 * звонке. Один раз на процесс. Путь к снимку дописывается в статус входа (фаза
 * та же) — сторож прочитает его после остановки. Никогда не бросает.
 */
export async function captureStopSnapshot(page: Page | null): Promise<string | null> {
  if (stopAt === null || captured) return null;
  captured = true;
  try {
    const phase = currentJoinPhase();
    if (!shouldCaptureOnStop(phase, joinPhaseSeen("in_call"))) {
      log(`[crash-kit] stop signal: phase=${phase ?? "none"} — снимок не нужен`);
      return null;
    }
    const kit = await Promise.race([
      captureCrashKit(page, `stop_signal_${phase}`),
      new Promise<null>((r) => setTimeout(() => r(null), STOP_SNAPSHOT_BUDGET_MS)),
    ]);
    if (kit) writeJoinStatus(phase as JoinPhase, { crash_kit: kit, stop_signal: true });
    else log("[crash-kit] stop snapshot: не успел за отведённое время — выхожу без него");
    return kit;
  } catch (e: any) {
    log(`[crash-kit] stop snapshot failed (non-fatal): ${e?.message}`);
    return null;
  }
}
