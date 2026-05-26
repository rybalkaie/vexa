// Yandex Telemost адаптер — main handler.
//
// Использует общий runMeetingFlow с набором стратегий, как все остальные
// браузерные платформы. По дизайну strategy-pattern из Vexa.

import { Page } from "playwright";
import { BotConfig } from "../../types";
import { runMeetingFlow, PlatformStrategies } from "../shared/meetingFlow";

import { joinYandexTelemost } from "./join";
import { waitForYandexTelemostAdmission, checkForTelemostAdmissionSilent } from "./admission";
import { startYandexTelemostRecording } from "./recording";
import { prepareForRecording, leaveYandexTelemost } from "./leave";
import { startYandexTelemostRemovalMonitor } from "./removal";

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
      await joinYandexTelemost(p as Page, cfg.meetingUrl!, cfg.botName, cfg);
    },
    waitForAdmission: waitForYandexTelemostAdmission as any,
    checkAdmissionSilent: checkForTelemostAdmissionSilent as any,
    prepare: prepareForRecording as any,
    startRecording: startYandexTelemostRecording as any,
    startRemovalMonitor: startYandexTelemostRemovalMonitor as any,
    leave: leaveYandexTelemost,
  };

  await runMeetingFlow("yandex_telemost", botConfig, page, gracefulLeaveFunction, strategies);
}

export { leaveYandexTelemost };
