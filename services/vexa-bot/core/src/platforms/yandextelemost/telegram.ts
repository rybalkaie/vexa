// Прямой вызов Telegram Bot API из bot-контейнера на VPS.
// Используется для push о waiting room — чтобы Илья мог впустить бота
// или знал, что встреча провалена.
//
// Токен и chat-id берутся из env (.env.notary):
//   TELEGRAM_BOT_TOKEN  — токен @Ilia_claude_1_bot
//   TELEGRAM_CHAT_ID    — chat_id Ильи (личка бота) — дефолтный получатель
//
// Ф2 (ISS-28, R4b): необязательный opts.chatId позволяет адресовать сообщение
// в ДРУГОЙ чат (групповой чат серии = seriesChatId из BOT_CONFIG) — для алерта
// «в момент детекта» в группу встречи. Без chatId сохраняется дефолт — личка
// Ильи (TELEGRAM_CHAT_ID), чтобы не сломать существующие вызовы admission.ts.
//
// Не блокирующая отправка: ошибка не должна валить адаптер.

import { log } from "../../utils";

const TG_API_BASE = "https://api.telegram.org";

let lastSentByKey: Map<string, number> = new Map();

/**
 * Отправить сообщение в Telegram. Возвращает true при успехе.
 * dedupeKey + dedupeMs позволяет защитить от спама одинаковыми сообщениями.
 * opts.chatId (Ф2/R4b) — переопределить получателя (напр. групповой чат серии);
 * без него шлём в личку Ильи (TELEGRAM_CHAT_ID) — дефолт не меняется.
 */
export async function sendTelegramMessage(
  text: string,
  opts?: { dedupeKey?: string; dedupeMs?: number; chatId?: number | string }
): Promise<boolean> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  // Ф2/R4b: явный получатель (групповой чат серии) перекрывает дефолт-личку.
  const chatId = opts?.chatId != null ? String(opts.chatId) : process.env.TELEGRAM_CHAT_ID;

  if (!token || !chatId) {
    log("[Telegram] TELEGRAM_BOT_TOKEN или chat_id не заданы (env/opts), push пропущен");
    return false;
  }

  if (opts?.dedupeKey && opts?.dedupeMs) {
    const last = lastSentByKey.get(opts.dedupeKey) || 0;
    if (Date.now() - last < opts.dedupeMs) {
      log(`[Telegram] dedupe: skip push с ключом '${opts.dedupeKey}' (последний ${Math.round((Date.now() - last) / 1000)}s назад)`);
      return false;
    }
    lastSentByKey.set(opts.dedupeKey, Date.now());
  }

  try {
    const url = `${TG_API_BASE}/bot${token}/sendMessage`;
    // Жёсткий timeout: если Telegram API повис, push не должен залочить admission loop —
    // мы рискуем упустить abandon-таймаут (см. план Ф2, обработка waiting room).
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      log(`[Telegram] sendMessage упал: HTTP ${res.status} ${body.slice(0, 200)}`);
      return false;
    }
    log(`[Telegram] push отправлен: ${text.substring(0, 80)}${text.length > 80 ? "…" : ""}`);
    return true;
  } catch (err: any) {
    log(`[Telegram] ошибка отправки: ${err.message}`);
    return false;
  }
}
