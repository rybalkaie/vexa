/**
 * Экран Телемоста: классификатор, признаки конца встречи, счётчик участников,
 * сверка ссылки (ISS-60, фаза 3а плана telemost-update-resilience, 25.09.2026).
 *
 * Признаки окон собраны по фактам живой разведки 25.09 (Т-10, Телемост 3.0) и
 * по старым селекторам; имена и ссылки — выдуманные.
 *
 * Run (в образе): node /app/dist/platforms/yandextelemost/screen.test.js
 */

import {
  FrameKind,
  FrameSignals,
  classifyTelemostScreen,
  removalEvidence,
  parseParticipantCount,
  isTelemostMeetingUrl,
  isCallFrameUrl,
  meetingIdFromUrl,
} from "./screen";

let passed = 0;
let failed = 0;

function expect(name: string, actual: any, expected: any) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    console.log(`  PASS  ${name}`);
    passed++;
  } else {
    console.log(`  FAIL  ${name}\n        expected: ${e}\n        actual:   ${a}`);
    failed++;
  }
}

function frame(kind: FrameKind, over: Partial<FrameSignals> = {}): FrameSignals {
  return {
    kind, lines: [], testids: [], buttons: [], arias: [], widgets: [], tiles: 0,
    nameInput: false, joinButton: false, ...over,
  };
}
const btn = (tid: string | null, aria: string | null = null, text = "") => ({ tid, aria, text });

// --- Копии экранов (признаки) ------------------------------------------------

// Главное окно 3.0 в лобби: виджет «Групповой звонок / Подключение» с кнопками
// «Завершить звонок» и «Выключить микрофон» (они видны УЖЕ в лобби) + поиск чатов.
const main30Lobby = frame("main", {
  lines: ["Телемост", "Групповой звонок", "Подключение"],
  buttons: [btn(null, "Завершить звонок"), btn(null, "Выключить микрофон")],
  arias: ["Завершить звонок", "Выключить микрофон"],
  testids: ["orb-textinput-input"],
});
// Главное окно 3.0 в звонке: мини-виджет «Открыть экран звонка» со счётчиком.
const main30Call = (n: number) => frame("main", {
  lines: ["Телемост", "10", `${n} участник${n === 1 ? "" : "а"}`, "•", "00:36"],
  arias: ["Открыть экран звонка"],
  widgets: [`10 ${n} участник${n === 1 ? "" : "а"} • 00:36`],
});
const call30Lobby = frame("call", {
  lines: ["Подключение к звонку", "Ваше имя", "Подключиться"],
  testids: ["orb-textinput-input", "turn-off-mic-button", "turn-off-camera-button", "enter-conference-button"],
  buttons: [btn("turn-off-mic-button"), btn("turn-off-camera-button"), btn("enter-conference-button", null, "Подключиться")],
  nameInput: true, joinButton: true,
});
const call30Toolbar = ["end-call-alt-button", "more-popup-alt-button", "mute-audio", "turn-off-mic-button",
  "turn-on-camera-button", "settings-button", "share-button", "participant-video-placeholder"];
const call30InCall = (names: string[]) => frame("call", {
  lines: names,
  testids: call30Toolbar,
  buttons: [btn("end-call-alt-button"), btn("mute-audio", "Выключить микрофон"), btn("turn-on-camera-button")],
  tiles: names.length,
});

console.log("classifyTelemostScreen — Телемост 3.0");
expect("лобби", classifyTelemostScreen([main30Lobby, call30Lobby]).state, "lobby");
expect("одна плитка (бот один) — в звонке", classifyTelemostScreen([main30Call(1), call30InCall(["Бот — протокол встречи"])]),
  { state: "in_call", ui: "3.0", evidence: "call:end-call-alt-button" });
expect("две плитки — в звонке", classifyTelemostScreen([main30Call(2), call30InCall(["Бот — протокол встречи", "Тест-диктор"])]).state, "in_call");
expect("зал ожидания во вложенном окне — раньше «в звонке» (кнопки звонка уже видны)",
  classifyTelemostScreen([main30Lobby, frame("call", {
    lines: ["Ожидание организатора", "Организатор скоро впустит вас в звонок"],
    testids: ["end-call-alt-button", "mute-audio"],
    buttons: [btn("end-call-alt-button"), btn("mute-audio", "Выключить микрофон")],
  })]), { state: "waiting_room", ui: "3.0", evidence: "call:text_waiting" });
expect("зал ожидания по точной метке во вложенном окне",
  classifyTelemostScreen([main30Lobby, frame("call", { testids: ["waiting-room-screen", "end-call-alt-button"] })]).state, "waiting_room");
expect("отказ во вложенном окне — раньше всего",
  classifyTelemostScreen([main30Lobby, frame("call", { lines: ["Вам отказано во входе", "На главную"], tiles: 1 })]),
  { state: "rejected", ui: "3.0", evidence: "call:text_rejected" });
expect("«отказано» строкой в окне с панелью звонка (чат) — не отказ",
  classifyTelemostScreen([main30Call(2), { ...call30InCall(["Тест-диктор"]), lines: ["Тест-диктор", "Вам отказано"] }]).state, "in_call");
expect("отказ: «Организатор отклонил ваш запрос»",
  classifyTelemostScreen([main30Lobby, frame("call", { lines: ["Организатор отклонил ваш запрос"] })]).state, "rejected");
expect("встреча завершена во вложенном окне",
  classifyTelemostScreen([main30Lobby, frame("call", { lines: ["Встреча завершена"], testids: ["participant-video-placeholder"] })]).state, "gone");
expect("«Звонок завершён» от прошлой встречи в чате идущего звонка — в звонке, не «встречи нет»",
  classifyTelemostScreen([main30Call(2), { ...call30InCall(["Тест-диктор"]), lines: ["Тест-диктор", "Звонок завершён"] }]).state, "in_call");
expect("«Такого звонка нет» на главной (живой экран 24.09)",
  classifyTelemostScreen([frame("main", { lines: ["Телемост", "Войдите в аккаунт", "Такого звонка нет", "Возможно, в ссылке опечатка", "На главную"] })]).state, "gone");
expect("«Завершить звонок» только на главной, окно звонка пустое — НЕ в звонке",
  classifyTelemostScreen([main30Lobby, frame("call", { lines: ["Загрузка"] })]),
  { state: "unknown", ui: "3.0", evidence: "none" });
expect("счётчик «N участников» в виджете — в звонке",
  classifyTelemostScreen([main30Call(3), frame("call")]).evidence, "main:call_widget_count");
expect("только плитки участников (панель спрятана) — в звонке",
  classifyTelemostScreen([main30Lobby, frame("call", { tiles: 2 })]).evidence, "call:participant_tiles");
expect("поле ввода рядом с идущим звонком (чат) — не лобби",
  classifyTelemostScreen([main30Call(2), { ...call30InCall(["Тест-диктор"]), nameInput: true }]).state, "in_call");
expect("поле имени + плитка и заглушка видео предпросмотра, панели звонка нет — лобби, не звонок",
  classifyTelemostScreen([main30Lobby, frame("call", {
    lines: ["Бот — протокол встречи"], testids: ["orb-textinput-input", "participant-video-placeholder"],
    tiles: 1, nameInput: true,
  })]), { state: "lobby", ui: "3.0", evidence: "call:name_input" });
expect("кнопка «Подключиться» видна — лобби, даже если есть кнопки звонка",
  classifyTelemostScreen([main30Lobby, { ...call30InCall([]), joinButton: true }]).state, "lobby");
expect("широкая старая метка *lobby* не перебивает «в звонке»",
  classifyTelemostScreen([main30Call(2), frame("call", { testids: ["lobby-container", ...call30Toolbar] })]).state, "in_call");
expect("широкая старая метка *waiting* без звонка — зал ожидания",
  classifyTelemostScreen([main30Lobby, frame("call", { testids: ["waiting-placeholder"] })]).evidence, "call:tid_weak_waiting-placeholder");
console.log("classifyTelemostScreen — текст людей не решает экран (цикл5 фазы 3, В1)");
// Главная 3.0 — чат комнаты: когда на ней мини-виджет звонка со счётчиком, её строки — реплики.
const main30Chat = (n: number, chat: string[]) => ({ ...main30Call(n), lines: [...main30Call(n).lines, ...chat] });
for (const [line, what] of [
  ["Дождитесь Ивана, начнём через 5 минут", "зал ожидания"],
  ["Кто впустит Сергея?", "зал ожидания"],
  ["Звонок завершён", "встречи нет"],
  ["Вам отказали", "отказ"],
] as const) {
  expect(`чат на главной «${line}», бот один в звонке — в звонке, не ${what}`,
    classifyTelemostScreen([main30Chat(1, [line]), call30InCall(["Бот — протокол встречи"])]).state, "in_call");
}
expect("реплика «Дождитесь Ивана» в окне звонка, людей видно (две плитки) — в звонке",
  classifyTelemostScreen([main30Call(2), { ...call30InCall(["Бот — протокол встречи", "Тест-диктор"]), lines: ["Бот — протокол встречи", "Тест-диктор", "Дождитесь Ивана"] }]).state, "in_call");
expect("участник по имени «Ожидайте» — в звонке",
  classifyTelemostScreen([main30Call(2), call30InCall(["Бот — протокол встречи", "Ожидайте"])]).state, "in_call");
expect("«Встреча завершена» строкой при двух плитках (панель спрятана) — не «встречи нет»",
  classifyTelemostScreen([main30Call(2), frame("call", { lines: ["Анна", "Бот — протокол встречи", "Встреча завершена"], tiles: 2 })]).state, "in_call");
expect("зал ожидания во вложенном окне + своя плитка предпросмотра — всё ещё зал ожидания",
  classifyTelemostScreen([main30Lobby, frame("call", { lines: ["Бот — протокол встречи", "Ожидание организатора"], tiles: 1, testids: call30Toolbar })]).state, "waiting_room");
expect("без виджета со счётчиком главная читается (зал ожидания текстом на главной)",
  classifyTelemostScreen([{ ...main30Lobby, lines: [...main30Lobby.lines, "Ожидание организатора"] }, frame("call", { lines: ["Загрузка"] })]).state, "waiting_room");
expect("поле поиска на главной 3.0 с новой подсказкой — не лобби, звонок виден",
  classifyTelemostScreen([{ ...main30Call(2), nameInput: true }, call30InCall(["Бот — протокол встречи", "Тест-диктор"])]).state, "in_call");
expect("поле имени во вложенном окне — лобби (как было)",
  classifyTelemostScreen([main30Lobby, frame("call", { nameInput: true })]).state, "lobby");
expect("«Подключиться» карточкой звонка в чате главной 3.0 при идущем звонке — в звонке",
  classifyTelemostScreen([{ ...main30Call(2), joinButton: true }, call30InCall(["Бот — протокол встречи", "Тест-диктор"])]).state, "in_call");
expect("старый интерфейс: «Подключиться» на главной — лобби (как было)",
  classifyTelemostScreen([frame("main", { joinButton: true, buttons: [btn(null, "Завершить звонок")] })]).state, "lobby");

expect("строка про доступ к микрофону — не отказ во входе",
  classifyTelemostScreen([main30Lobby, frame("call", { lines: ["Доступ к микрофону отказано"] })]).state, "unknown");
expect("справка с вопросом — не «встреча завершена»",
  classifyTelemostScreen([main30Lobby, frame("call", { lines: ["Что делать, если встреча завершена?"] })]).state, "unknown");
expect("длинный абзац со словом «впустит» — не зал ожидания",
  classifyTelemostScreen([main30Lobby, frame("call", { lines: ["Если организатор включил зал ожидания, он впустит вас, когда будет готов; пока можно проверить камеру и микрофон, выбрать фон, настроить звук и изображение, а также прочитать правила встречи и подготовить вопросы заранее."] })]).state, "unknown");
expect("пусто — неясно", classifyTelemostScreen([]).state, "unknown");

console.log("classifyTelemostScreen — старый интерфейс (без вложенного окна)");
expect("лобби на главной", classifyTelemostScreen([frame("main", { nameInput: true, joinButton: true })]).state, "lobby");
expect("в звонке по «Завершить звонок» на главной",
  classifyTelemostScreen([frame("main", { buttons: [btn(null, "Завершить звонок")], arias: ["Завершить звонок"] })]),
  { state: "in_call", ui: "old", evidence: "main:aria_end_call" });
expect("в звонке по mute-audio на главной",
  classifyTelemostScreen([frame("main", { testids: ["mute-audio"] })]).evidence, "main:mute-audio");
expect("в звонке по «Выйти из встречи»",
  classifyTelemostScreen([frame("main", { arias: ["Выйти из встречи"] })]).evidence, "main:aria_leave");
expect("зал ожидания по старой фразе «Дождитесь…»",
  classifyTelemostScreen([frame("main", { lines: ["Дождитесь, пока организатор впустит вас"], buttons: [btn(null, "Завершить звонок")] })]).state, "waiting_room");
expect("«Такой встречи не существует»",
  classifyTelemostScreen([frame("main", { lines: ["Такой встречи не существует"] })]).state, "gone");

console.log("removalEvidence — конец встречи во всех окнах");
expect("обычный звонок — нет", removalEvidence([main30Call(2), call30InCall(["Тест-диктор"])]), null);
expect("реплика чата «Встреча завершена» при идущем звонке — нет",
  removalEvidence([main30Call(2), { ...call30InCall(["Тест-диктор"]), lines: ["Тест-диктор", "Встреча завершена"] }]), null);
expect("экран «Встреча завершена» во вложенном окне без панели звонка",
  removalEvidence([main30Call(2), frame("call", { lines: ["Встреча завершена", "Вернуться"] })]), "call:text_meeting_gone");
expect("«Вы вышли из звонка»",
  removalEvidence([frame("main"), frame("call", { lines: ["Вы вышли из звонка"] })]), "call:text_removed");
expect("метка meeting-ended",
  removalEvidence([frame("main"), frame("call", { testids: ["meeting-ended-screen"] })]), "call:tid_meeting-ended-screen");
expect("«Такого звонка нет» на главной",
  removalEvidence([frame("main", { lines: ["Такого звонка нет"] })]), "main:text_meeting_gone");
expect("на главной «Звонок завершён» (превью другого звонка), а в окне звонка идёт встреча — нет",
  removalEvidence([frame("main", { lines: ["Звонок завершён"] }), call30InCall(["Тест-диктор"])]), null);
expect("плитки ещё на экране (панель спрятана) — «Встреча завершена» строкой не считается",
  removalEvidence([frame("main"), frame("call", { lines: ["Встреча завершена"], tiles: 2 })]), null);
expect("длинная строка с «завершена» — нет",
  removalEvidence([frame("call", { lines: ["Напоминаю: когда встреча завершена, протокол придёт в чат серии в течение получаса"] })]), null);

console.log("parseParticipantCount — счётчик виджета");
expect("живой текст 25.09", parseParticipantCount("10\n2 участника\n•\n00:36"), 2);
expect("1 участник", parseParticipantCount("1 участник"), 1);
expect("5 участников", parseParticipantCount("5 участников"), 5);
expect("english", parseParticipantCount("12 participants"), 12);
expect("без счётчика", parseParticipantCount("Открыть экран звонка"), null);
expect("пусто", parseParticipantCount(""), null);

console.log("isTelemostMeetingUrl — хост целиком (хвост С10)");
expect("настоящая", isTelemostMeetingUrl("https://telemost.yandex.ru/j/00000000000000"), true);
expect("360", isTelemostMeetingUrl("https://telemost.360.yandex.ru/j/00000000000000"), true);
expect("http тоже Телемост", isTelemostMeetingUrl("http://telemost.yandex.ru/j/0"), true);
expect("регистр", isTelemostMeetingUrl("https://TELEMOST.YANDEX.RU/j/0"), true);
expect("подстрока в пути (сам С10)", isTelemostMeetingUrl("https://evil.example/telemost.yandex.ru/j/0"), false);
expect("хост лишь начинается так", isTelemostMeetingUrl("https://telemost.yandex.ru.evil.example/j/0"), false);
expect("логин перед чужим хостом", isTelemostMeetingUrl("https://telemost.yandex.ru@evil.example/j/0"), false);
expect("обратная косая", isTelemostMeetingUrl("https://evil.example\\@telemost.yandex.ru/j/0"), false);
expect("чужой порт", isTelemostMeetingUrl("https://telemost.yandex.ru:8443/j/0"), false);
expect("похожий хост", isTelemostMeetingUrl("https://xtelemost.yandex.ru/j/0"), false);
expect("javascript:", isTelemostMeetingUrl("javascript:alert(1)//telemost.yandex.ru"), false);
expect("пусто", isTelemostMeetingUrl(""), false);

console.log("isCallFrameUrl — окно звонка 3.0 (цикл5 фазы 3, У3: путь окна могут переименовать)");
const MAIN_J = "https://telemost.yandex.ru/j/00000000000000";
expect("номер встречи из /j/", meetingIdFromUrl(MAIN_J), "00000000000000");
expect("номера нет — null", meetingIdFromUrl("https://telemost.yandex.ru/"), null);
expect("не адрес — null", meetingIdFromUrl("не адрес"), null);
expect("сегодняшнее окно /private-join/", isCallFrameUrl("https://telemost.yandex.ru/private-join/00000000000000?embed=1", MAIN_J, true), true);
expect("путь переименован, окна /private-join/ нет — окно Телемоста с номером встречи",
  isCallFrameUrl("https://telemost.yandex.ru/embed-call/00000000000000?embed=1", MAIN_J, false), true);
expect("окно /private-join/ есть — правило прежнее, другое окно с номером не звонок",
  isCallFrameUrl("https://telemost.yandex.ru/embed-call/00000000000000", MAIN_J, true), false);
expect("чужой хост с номером встречи — не звонок", isCallFrameUrl("https://evil.example/00000000000000", MAIN_J, false), false);
expect("окно Телемоста без номера — не звонок", isCallFrameUrl("https://telemost.yandex.ru/metrics", MAIN_J, false), false);
expect("номер другой встречи — не звонок", isCallFrameUrl("https://telemost.yandex.ru/embed-call/11111111111111", MAIN_J, false), false);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
