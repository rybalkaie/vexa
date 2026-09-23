// Статус входа бота во встречу — для сторожа на хосте (ISS-60, 23.09.2026).
//
// ЗАЧЕМ. С 21 по 23.09.2026 бот падал на каждой встрече через ~40 с после
// старта, и никто этого не видел: запускатель смотрел на контейнер один раз
// через 3 с, а тревога «пустая запись» живёт от meta.json, который бот пишет
// только ПОСЛЕ входа. Сорванный вход не оставлял следов вовсе.
//
// ЧТО ДЕЛАЕТ. Бот пишет крошечный файл-статус рядом с записями:
//   <TRANSCRIPT_DIR>/.join-status/<sessionUid>.json
// фазы: joining → waiting_room → in_call, либо failed + код причины.
// sessionUid — тот, что дал запускатель (`auto-…`, сырой BOT_CONFIG): по нему
// сторож на хосте (scripts/notary/lib/join_watch.py) находит файл.
//
// Тревогу бот НЕ шлёт сам: первый срыв бывает случайным, и сообщение в группу
// до повторной попытки было бы ложным. Решает сторож: повтор → сообщение.
//
// Приватность: в файле только фаза, код причины, шаг и обрезанный текст
// исключения (наши собственные сообщения об ошибке) — ни имён, ни реплик.

import * as fs from "fs";
import * as path from "path";
import { Page } from "playwright";
import { log } from "../../utils";

const TRANSCRIPT_DIR = process.env.TELEMOST_TRANSCRIPT_DIR || "/transcripts";
export const JOIN_STATUS_DIR = path.join(TRANSCRIPT_DIR, ".join-status");

export type JoinPhase = "joining" | "waiting_room" | "in_call" | "failed";

// Коды причин — машинные, их понимает join_alert.py (тексты для людей там).
export const JOIN_REASONS = [
  "name_input_not_found",
  "join_button_not_found",
  "still_in_lobby",
  "navigation_failed",
  "meeting_not_found",
  "admission_rejected",
  "waiting_room_not_admitted",
  "admission_timeout",
  "admission_false_positive",
  "join_error",
] as const;

/** Сырой BOT_CONFIG: Zod апстрима вырезает незнакомые ключи (sessionUid в том числе). */
export function rawBotConfig(): any {
  try {
    return JSON.parse(process.env.BOT_CONFIG || "{}");
  } catch {
    return {};
  }
}

const SAFE_UID_RE = /^[A-Za-z0-9._-]{1,200}$/;

/** sessionUid запускателя (`auto-…`). Нет или странный — статус не пишем. */
export function joinSessionUid(): string | null {
  const v = rawBotConfig().sessionUid;
  return typeof v === "string" && SAFE_UID_RE.test(v) ? v : null;
}

let state: Record<string, any> = {};

/**
 * Записать фазу входа. Никогда не бросает: статус — помощник сторожа, а не
 * условие входа. Запись атомарная (tmp + rename), чтобы сторож не прочитал
 * половину файла.
 */
export function writeJoinStatus(phase: JoinPhase, extra: Record<string, unknown> = {}): void {
  const uid = joinSessionUid();
  if (!uid) return;
  try {
    fs.mkdirSync(JOIN_STATUS_DIR, { recursive: true });
    try { fs.chmodSync(JOIN_STATUS_DIR, 0o777); } catch { /* не наш каталог — не страшно */ }
    const now = new Date().toISOString();
    const history = Array.isArray(state.history) ? state.history : [];
    state = {
      ...state,
      ...extra,
      phase,
      session_uid: uid,
      image_sha: process.env.NOTARY_BOT_GIT_SHA || null,
      started_at: state.started_at || now,
      updated_at: now,
      history: [...history, { phase, at: now }].slice(-20),
    };
    const file = path.join(JOIN_STATUS_DIR, `${uid}.json`);
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(state, null, 2), "utf8");
    // 0666 — чтобы dev на хосте мог прочитать и убрать (контейнер пишет от root).
    try { fs.chmodSync(tmp, 0o666); } catch { /* */ }
    fs.renameSync(tmp, file);
    log(`[join-status] phase=${phase}${extra.reason ? ` reason=${String(extra.reason)}` : ""}`);
  } catch (e: any) {
    log(`[join-status] write failed (non-fatal): ${e?.message}`);
  }
}

/** Отметить, что вход прошёл запасным путём (Фаза 3) — не сбой, а сигнал «Телемост поменялся». */
export function noteJoinFallback(detail: Record<string, unknown>): void {
  state = { ...state, fallback_used: { ...(state.fallback_used || {}), ...detail } };
}

/** Код причины по тексту исключения из join.ts / admission.ts. */
export function classifyJoinError(message: string): string {
  const m = message || "";
  if (/поле ввода имени не найдено/i.test(m)) return "name_input_not_found";
  if (/кнопка «Подключиться» не найдена/i.test(m)) return "join_button_not_found";
  if (/остался в лобби/i.test(m)) return "still_in_lobby";
  if (/rejected by meeting admin/i.test(m)) return "admission_rejected";
  if (/not admitted from Telemost waiting room/i.test(m)) return "waiting_room_not_admitted";
  if (/admission timeout/i.test(m)) return "admission_timeout";
  if (/net::ERR_|Navigation|page\.goto|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION/i.test(m)) return "navigation_failed";
  return "join_error";
}

// «Встречи нет / завершена» — это не поломка бота, а проблема ссылки или
// встречи. Сторож шлёт такое только владельцу, группу не пугает.
const MEETING_GONE_RE =
  /(такой встречи не существует|встреч[аи] не существует|встреча (уже )?завершена|встреча закончилась|ссылка (устарела|недействительна)|meeting (not found|has ended|doesn.t exist))/i;

/** Есть ли на странице (в любом окне) надпись «встречи нет / завершена». */
export async function detectMeetingGone(page: Page | null): Promise<boolean> {
  if (!page) return false;
  for (const fr of page.frames()) {
    try {
      const txt: string = await fr.evaluate(() => (document.body && document.body.innerText) || "");
      if (MEETING_GONE_RE.test(txt)) return true;
    } catch {
      // окно ушло или чужой origin — дальше
    }
  }
  return false;
}

/** Обрезанный текст для статуса (наши собственные сообщения, без данных встречи). */
export function shortMessage(m: unknown, max = 300): string {
  const s = String(m ?? "").replace(/\s+/g, " ").trim();
  return s.length > max ? `${s.slice(0, max)}…` : s;
}
