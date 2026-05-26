// Прямой вызов Telegram Bot API из bot-контейнера на VPS.
// Используется для push о waiting room — чтобы Илья мог впустить бота
// или знал, что встреча провалена.
//
// Токен и chat-id берутся из env (.env.notary):
//   TELEGRAM_BOT_TOKEN  — токен @Ilia_claude_1_bot
//   TELEGRAM_CHAT_ID    — chat_id Ильи (личка бота)
//
// Не блокирующая отправка: ошибка не должна валить адаптер.

import { log } from "../../utils";

const TG_API_BASE = "https://api.telegram.org";

let lastSentByKey: Map<string, number> = new Map();

/**
 * Отправить сообщение в Telegram. Возвращает true при успехе.
 * dedupeKey + dedupeMs позволяет защитить от спама одинаковыми сообщениями.
 */
export async function sendTelegramMessage(
  text: string,
  opts?: { dedupeKey?: string; dedupeMs?: number }
): Promise<boolean> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;

  if (!token || !chatId) {
    log("[Telegram] TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы в env, push пропущен");
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
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
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
