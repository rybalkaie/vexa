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
//
// 25.09.2026 (фаза 3а, хвост У8): сторож останавливает зависшего на входе бота
// сигналом (docker stop). По сигналу бот, пока ещё не в звонке, снимает
// аварийный снимок (stop-signal.ts → leave.ts) и выходит штатно. Вход, сорванный
// нашей же остановкой, снимок и статус не перетирает.

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
  shortMessage,
} from "./join-status";
import { joinFailureReason } from "./screen";
import { captureCrashKit } from "./crashkit";
import { armStopSignalCapture, stopSignalReceived } from "./stop-signal";

/** Сорванный вход: причина → аварийный снимок → статус failed. Не бросает. */
async function markJoinFailed(page: Page | null, step: string, error: any): Promise<void> {
  // Вход сорвала наша же остановка (сигнал): снимок уже снят по сигналу, а
  // «failed» с закрытой страницы был бы ложной причиной.
  if (stopSignalReceived()) return;
  try {
    const message = error?.message || String(error);
    // «Встречи нет» — тем же разбором экрана, что вход и ожидание впуска
    // (screen.ts → joinFailureReason; цикл5 фазы 3, В1).
    const reason = await joinFailureReason(page, message);
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
  armStopSignalCapture();
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
      // «Подключиться» нажато, ждём признаков звонка: у сторожа на эту фазу свой
      // срок (до waitingRoomTimeout + запас), чтобы не убить бота, которого ещё
      // могут впустить (цикл5 Н1).
      writeJoinStatus("admission", { admission_timeout_ms: timeoutMs });
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
      if (!ok && !stopSignalReceived()) {
        // «Вход не подтвердился» — вероятная форма следующей поломки (Телемост
        // поменял экран звонка): снимок нужен именно здесь (цикл5 У8).
        const kit = await captureCrashKit(p, "admission_false_positive").catch(() => null);
        writeJoinStatus("failed", { reason: "admission_false_positive", step: "verify", crash_kit: kit });
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
