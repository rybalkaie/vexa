/**
 * Тест распознавания причин сорванного входа (ISS-60, цикл5 фазы 1: У14).
 * Формулировку Телемоста 3.0 «Такого звонка нет» нашли только живой проверкой
 * 24.09.2026 — этот тест поймал бы её за минуту.
 *
 * Run: npx tsx services/vexa-bot/core/src/platforms/yandextelemost/join-status.test.ts
 */

import {
  classifyJoinError,
  isMeetingGoneText,
  MEETING_GONE_ERROR,
  ADMISSION_UNCLEAR_ERROR,
  MEETING_URL_REJECTED_ERROR,
} from "./join-status";

let passed = 0;
let failed = 0;

function expect(name: string, actual: any, expected: any) {
  if (actual === expected) {
    console.log(`  PASS  ${name}`);
    passed++;
  } else {
    console.log(`  FAIL  ${name}\n        expected: ${JSON.stringify(expected)}\n        actual:   ${JSON.stringify(actual)}`);
    failed++;
  }
}

console.log("classifyJoinError — тексты исключений из join.ts / admission.ts");
expect("поле имени (главная)", classifyJoinError("Lobby: поле ввода имени не найдено в Telemost"), "name_input_not_found");
expect("поле имени (iframe)", classifyJoinError("Lobby: поле ввода имени не найдено в Telemost (iframe лобби)"), "name_input_not_found");
expect("кнопка", classifyJoinError("Lobby: кнопка «Подключиться» не найдена в Telemost (iframe лобби)"), "join_button_not_found");
expect("остался в лобби", classifyJoinError("Lobby: после «Подключиться» бот остался в лобби Telemost"), "still_in_lobby");
expect("отклонили", classifyJoinError("Bot admission was rejected by meeting admin (telemost rejection indicator)"), "admission_rejected");
expect("зал ожидания", classifyJoinError("Bot was not admitted from Telemost waiting room within 2 minutes"), "waiting_room_not_admitted");
expect("таймаут впуска", classifyJoinError("Telemost admission timeout"), "admission_timeout");
expect("сеть", classifyJoinError("page.goto: net::ERR_NAME_NOT_RESOLVED at https://x"), "navigation_failed");
expect("прочее", classifyJoinError("something else"), "join_error");
// Фаза 3а (25.09.2026): свои тексты исключений admission.ts / join.ts.
expect("встреча кончилась во время впуска", classifyJoinError(MEETING_GONE_ERROR), "meeting_not_found");
expect("экран звонка не распознан ≠ в звонке", classifyJoinError(ADMISSION_UNCLEAR_ERROR), "admission_unclear");
expect("ссылка не на Телемост (С10)", classifyJoinError(MEETING_URL_REJECTED_ERROR), "meeting_url_rejected");

console.log("isMeetingGoneText — «встречи нет» на экране");
expect("Телемост 3.0 (живой экран 24.09)", isMeetingGoneText("Телемост\nВойдите в аккаунт\nТакого звонка нет\nВозможно, в ссылке опечатка\nНа главную"), true);
expect("старая формулировка", isMeetingGoneText("Такой встречи не существует"), true);
expect("встреча завершена", isMeetingGoneText("Встреча завершена"), true);
expect("обычное лобби", isMeetingGoneText("Подключение к звонку\nВаше имя\nПодключиться"), false);
expect("справка с вопросом не считается", isMeetingGoneText("Что делать, если ссылка недействительна?"), false);
expect("длинное пояснение не считается", isMeetingGoneText("Запись будет доступна организатору, когда встреча завершена и обработана сервисом"), false);
expect("пусто", isMeetingGoneText(""), false);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
