// Yandex Telemost адаптер — main handler.
//
// Использует общий runMeetingFlow с набором стратегий, как все остальные
// браузерные платформы. По дизайну strategy-pattern из Vexa.
//
// 23.09.2026 (ISS-60): каждая стратегия входа обёрнута записью статуса
// (join-status.ts) — joining → waiting_room → in_call / failed + причина, а при
// сорванном входе бот снимает аварийный комплект (crashkit.ts). По статусу
// сторож на хосте (scripts/notary/lib/join_watch.py) решает: повторить вход или
// написать людям. Логика самого входа не меняется.

import { Page } from "playwright";
import { BotConfig } from "../../types";
import { runMeetingFlow, PlatformStrategies } from "../shared/meetingFlow";

import { joinYandexTelemost } from "./join";
import { waitForYandexTelemostAdmission, checkForTelemostAdmissionSilent } from "./admission";
import { startYandexTelemostRecording } from "./recording";
import { prepareForRecording, leaveYandexTelemost } from "./leave";
import { startYandexTelemostRemovalMonitor } from "./removal";
import {
  writeJoinStatus,
  classifyJoinError,
  detectMeetingGone,
  shortMessage,
} from "./join-status";
import { captureCrashKit } from "./crashkit";

/** Сорванный вход: причина → аварийный снимок → статус failed. Не бросает. */
async function markJoinFailed(page: Page | null, step: string, error: any): Promise<void> {
  try {
    const message = error?.message || String(error);
    let reason = classifyJoinError(message);
    if (reason !== "admission_rejected" && (await detectMeetingGone(page))) {
      reason = "meeting_not_found";
    }
    const kit = await captureCrashKit(page, reason);
    writeJoinStatus("failed", { reason, step, message: shortMessage(message), crash_kit: kit });
  } catch {
    // статус — помощник сторожа; его сбой не должен менять ход выхода бота
  }
}

export async function handleYandexTelemost(
  botConfig: BotConfig,
  page: Page,
  gracefulLeaveFunction: (
    page: Page | null,
    exitCode: number,
    reason: string,
    errorDetails?: any
  ) => Promise<void>
): Promise<void> {
  const strategies: PlatformStrategies = {
    join: async (p: Page | null, cfg: BotConfig) => {
      writeJoinStatus("joining");
      try {
        await joinYandexTelemost(p as Page, cfg.meetingUrl!, cfg.botName, cfg);
      } catch (error: any) {
        await markJoinFailed(p, "join", error);
        throw error;
      }
    },
    waitForAdmission: (async (p: Page, timeoutMs: number, cfg: BotConfig) => {
      try {
        const admitted = await waitForYandexTelemostAdmission(p, timeoutMs, cfg);
        if (admitted) writeJoinStatus("in_call");
        return admitted;
      } catch (error: any) {
        await markJoinFailed(p, "admission", error);
        throw error;
      }
    }) as any,
    checkAdmissionSilent: (async (p: Page) => {
      const ok = await checkForTelemostAdmissionSilent(p);
      if (!ok) {
        writeJoinStatus("failed", { reason: "admission_false_positive", step: "verify" });
      }
      return ok;
    }) as any,
    prepare: prepareForRecording as any,
    startRecording: startYandexTelemostRecording as any,
    startRemovalMonitor: startYandexTelemostRemovalMonitor as any,
    leave: leaveYandexTelemost,
  };

  await runMeetingFlow("yandex_telemost", botConfig, page, gracefulLeaveFunction, strategies);
}

export { leaveYandexTelemost };
