/**
 * Запасной путь входа — чистые решения (ISS-60, фаза 3б плана
 * telemost-update-resilience, 25.09.2026): белый и чёрный списки, ярусы поиска
 * (метка → подпись → текст), выбор следующего шага, итог для владельца, флаг
 * живой проверки.
 *
 * Элементы собраны по копиям экранов (screen-copies.test.ts) и по живой разведке
 * 25.09; имена и ссылки выдуманные. Браузер не нужен.
 *
 * Run (в образе): node /app/dist/platforms/yandextelemost/fallback.test.js
 */

import {
  FbControl,
  FbDecisionState,
  blacklistHit,
  chooseFallbackAction,
  controlSignature,
  fallbackStepsCode,
  fallbackSummary,
  lobbyControlsVisible,
  lobbyDialogs,
  matchButton,
  matchNameInput,
  normLabel,
  structuralNameInputs,
  MAX_POPUP_CLICKS,
} from "./fallback";
import { telemostFallbackButtons } from "./selectors";
import { fallbackForceMode } from "./join-status";

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

let n = 0;
function btn(over: Partial<FbControl> = {}): FbControl {
  return {
    id: `b${n++}`, frameIdx: 0, frame: "call", kind: "button", tag: "button", role: null, tid: null, aria: null,
    title: null, placeholder: null, label: null, near: null, text: "", type: null, dialog: null,
    disabled: false, readOnly: false, isBotName: false, ...over,
  };
}
function inp(over: Partial<FbControl> = {}): FbControl {
  return btn({ kind: "input", tag: "input", ...over });
}
function state(over: Partial<FbDecisionState> = {}): FbDecisionState {
  return { nameFilled: false, spent: new Set(), noTestid: false, popupClicks: 0, joinWithoutName: false, ...over };
}
/** Что выбрано: [действие, цель, ярус, id элемента] или null. */
function pick(controls: FbControl[], st: FbDecisionState = state()) {
  const a = chooseFallbackAction(controls, st);
  return a ? [a.kind, a.m.target, a.m.tier, a.control.id] : null;
}

// --- Элементы копий экранов ---------------------------------------------------

const MAIN = { frame: "main" as const, frameIdx: 1 };
const main30 = () => [
  btn({ ...MAIN, aria: "Завершить звонок", id: "w-end" }),
  btn({ ...MAIN, aria: "Выключить микрофон", tid: "orb-button", id: "w-mic" }),
  btn({ ...MAIN, text: "Войти", tid: "orb-global-bar-login-button", id: "login" }),
  inp({ ...MAIN, tid: "orb-textinput-input", placeholder: "Поиск", id: "search" }),
];
const onboarding = () => [
  btn({ ...MAIN, tid: "telemost-3-onboarding-close", text: "×", dialog: "d0", id: "onb-x" }),
];
const interstitial = () => [
  btn({ ...MAIN, tid: "meeting-continue-in-browser-continue", text: "Продолжить в браузере", id: "cont" }),
  btn({ ...MAIN, text: "Открыть в приложении", id: "app" }),
];
const lobby30 = (over: { name?: Partial<FbControl>; join?: Partial<FbControl> } = {}) => [
  inp({ tid: "orb-textinput-input", placeholder: "Ваше имя", id: "name", ...(over.name || {}) }),
  btn({ tid: "turn-off-mic-button", id: "mic" }),
  btn({ tid: "turn-off-camera-button", id: "cam" }),
  btn({ tid: "enter-conference-button", text: "Подключиться", id: "join", ...(over.join || {}) }),
];

console.log("normLabel / blacklistHit — чёрный список сильнее белого");
expect("нормализация", normLabel("  ПОДКЛЮЧИТЬСЯ.  "), "подключиться");
expect("мягкий перенос и word joiner внутри слова убраны", normLabel("Под\u00adклю\u2060читься"), "подключиться");
expect("ё = е", normLabel("Всё ясно"), "все ясно");
expect("значки по краям отброшены", normLabel("🎥 Подключиться →"), "подключиться");
expect("вопрос в конце", normLabel("Как вас представить?"), "как вас представить");
expect("голый крестик остаётся крестиком", normLabel(" × "), "×");
expect("запятая внутри остаётся", normLabel("Нет, спасибо!"), "нет, спасибо");
for (const c of [
  { tid: "end-call-alt-button" }, { text: "Завершить звонок" }, { aria: "Выйти из звонка" }, { text: "Выйти" },
  { text: "Начать запись" }, { title: "Демонстрация экрана" }, { aria: "Включить микрофон" }, { aria: "Включить камеру" },
  { text: "Пригласить" }, { text: "Войти" }, { text: "Войти в аккаунт" }, { text: "На главную" }, { text: "Отмена" },
  { text: "Завершить для всех" }, { tid: "share-button" }, { tid: "turn-on-camera-button" }, { tid: "turn-on-mic-button" },
  { tid: "hand-up-button" }, { tid: "start-demonstration-button" }, { tid: "to-home-screen-button" },
  { text: "Открыть в приложении" }, { text: "Leave call" }, { tid: "mute-audio" }, { text: "Поднять руку" },
  { tid: "close-call" }, { tid: "meeting-close" }, { text: "Закрыть звонок" }, { text: "Отключиться" },
  // цикл5 фазы 3 (В5): невидимые символы внутри слова и новые слова
  { text: "Начать за\u00adпись" }, { aria: "Вы\u2060йти" }, { text: "Уйти со встречи" }, { text: "Начать трансляцию" },
  { text: "Добавить участника" }, { text: "Впустить всех" }, { text: "Admit all" }, { text: "Start video" },
]) {
  expect(`в чёрном списке: ${JSON.stringify(c)}`, blacklistHit({ tid: null, aria: null, title: null, text: "", ...c }) !== null, true);
}
for (const c of [
  { text: "Подключиться" }, { text: "Понятно" }, { text: "Продолжить в браузере" }, { aria: "Выключить микрофон" },
  { text: "Закрыть" }, { tid: "enter-conference-button" }, { tid: "orb-button-close" }, { tid: "feedback-close" },
  { text: "Попросить войти" },
]) {
  expect(`не в чёрном списке: ${JSON.stringify(c)}`, blacklistHit({ tid: null, aria: null, title: null, text: "", ...c }), null);
}
{
  const selfVetoed: string[] = [];
  for (const [target, L] of Object.entries(telemostFallbackButtons)) {
    for (const l of L.labels) if (blacklistHit({ tid: null, aria: null, title: null, text: l })) selfVetoed.push(`${target}:${l}`);
    for (const t of L.tids) if (blacklistHit({ tid: t, aria: null, title: null, text: "" })) selfVetoed.push(`${target}:${t}`);
  }
  expect("белый список сам себя не вычёркивает (ни одна подпись и метка не в чёрном)", selfVetoed, []);
}

console.log("matchButton — ярусы: метка → подпись → текст");
expect("метка сегодняшняя", matchButton(btn({ tid: "enter-conference-button" }), "join"), { target: "join", tier: "tid", match: "enter-conference-button" });
expect("метка по образцу (переименовали в том же смысле)", matchButton(btn({ tid: "join-call-button" }), "join")?.tier, "tid");
// Цикл5 фазы 3 (В2): образец метки — целиком. «Войти с камерой / микрофоном» — не «Подключиться».
for (const tid of ["join-call-with-video", "enter-conference-with-mic", "join-meeting-camera-on", "join-call-mic-on"]) {
  expect(`метка «${tid}» — не «Подключиться»`, matchButton(btn({ tid }), "join"), null);
  expect(`метка «${tid}» — в чёрном списке`, blacklistHit({ tid, aria: null, title: null, text: "" }) !== null, true);
}
expect("метка по образцу «join-meeting» — «Подключиться»", matchButton(btn({ tid: "join-meeting" }), "join")?.tier, "tid");
expect("метка «join-meeting-video» (кусок образца) — не «Подключиться»", matchButton(btn({ tid: "join-meeting-video" }), "join"), null);
expect("метка по образцу «enter-conference» — «Подключиться»", matchButton(btn({ tid: "enter-conference" }), "join")?.tier, "tid");
expect("подпись", matchButton(btn({ aria: "Подключиться" }), "join"), { target: "join", tier: "label", match: "Подключиться" });
expect("title — тоже подпись", matchButton(btn({ title: "Присоединиться" }), "join")?.tier, "label");
expect("текст (регистр, точка)", matchButton(btn({ text: " ПОДКЛЮЧИТЬСЯ. " }), "join"), { target: "join", tier: "text", match: "Подключиться" });
expect("текст целиком, не кусок", matchButton(btn({ text: "Подключиться без камеры" }), "join"), null);
expect("без меток: метка не в счёт", matchButton(btn({ tid: "enter-conference-button" }), "join", true), null);
expect("без меток: текст работает", matchButton(btn({ tid: "enter-conference-button", text: "Подключиться" }), "join", true)?.tier, "text");
expect("текст «Подключиться», но метка end-call — НЕТ", matchButton(btn({ tid: "end-call-alt-button", text: "Подключиться" }), "join"), null);
expect("текст «Понятно», но подпись «Начать запись» — НЕТ", matchButton(btn({ aria: "Начать запись", text: "Понятно" }), "popup_ack"), null);
expect("голое «Войти» — не вход в звонок", matchButton(btn({ text: "Войти" }), "join"), null);
expect("поле — не кнопка", matchButton(inp({ text: "Подключиться" }), "join"), null);
expect("крестик закрытия по образцу метки", matchButton(btn({ tid: "whatsnew-close" }), "popup_dismiss")?.tier, "tid");
expect("«закрыть звонок» по образцу метки — НЕ крестик всплывашки", matchButton(btn({ tid: "close-call" }), "popup_dismiss"), null);
expect("значок у текста не мешает", matchButton(btn({ text: "🎥 Подключиться" }), "join")?.tier, "text");

console.log("matchNameInput — поле имени, не поиск и не чат");
expect("лобби 3.0 по метке", matchNameInput(inp({ tid: "orb-textinput-input", placeholder: "Ваше имя" })), { target: "name", tier: "tid", match: "orb-textinput-input" });
expect("поиск чатов 3.0 с той же меткой (placeholder «Поиск»)", matchNameInput(inp({ tid: "orb-textinput-input", placeholder: "Поиск" })), null);
expect("та же метка, подпись «Поиск по чатам» без placeholder", matchNameInput(inp({ tid: "orb-textinput-input", aria: "Поиск по чатам" })), null);
expect("та же метка, type=search", matchNameInput(inp({ tid: "orb-textinput-input", type: "search" })), null);
expect("та же метка, role=searchbox", matchNameInput(inp({ tid: "orb-textinput-input", role: "searchbox" })), null);
expect("подпись «Как вас представить?»", matchNameInput(inp({ tid: "name-field-v4", placeholder: "Как вас представить?" }))?.tier, "label");
expect("<label> «Ваше имя»", matchNameInput(inp({ label: "Ваше имя" }))?.tier, "label");
expect("английская подпись", matchNameInput(inp({ aria: "Your name" }))?.tier, "label");
expect("поле чата — нет", matchNameInput(inp({ placeholder: "Напишите сообщение" })), null);
expect("приглашение по почте — нет", matchNameInput(inp({ placeholder: "Пригласить по e-mail" })), null);
expect("текст рядом — только если поле в окне одно", matchNameInput(inp({ near: "Как вас зовут" })), null);
expect("текст рядом, поле одно", matchNameInput(inp({ near: "Как вас зовут" }), false, true)?.tier, "text");
expect("текст рядом про поиск — нет", matchNameInput(inp({ near: "Поиск по имени" }), false, true), null);
expect("выключенное / только чтение — нет", [matchNameInput(inp({ placeholder: "Имя", disabled: true })), matchNameInput(inp({ placeholder: "Имя", readOnly: true }))], [null, null]);
expect("textarea — нет (чат)", matchNameInput(inp({ tag: "textarea", placeholder: "Имя" })), null);
expect("без меток: одна метка — нет, подпись — да", [matchNameInput(inp({ tid: "orb-textinput-input" }), true), matchNameInput(inp({ tid: "orb-textinput-input", placeholder: "Ваше имя" }), true)?.tier], [null, "label"]);

console.log("chooseFallbackAction — следующий шаг");
expect("онбординг поверх интерстициала: сначала закрыть окно (по метке)",
  pick([...onboarding(), ...interstitial(), ...main30()]), ["click", "popup_dismiss", "tid", "onb-x"]);
expect("окно закрыто: «Продолжить в браузере», не «в приложении»",
  pick([...interstitial(), ...main30()]), ["click", "continue", "tid", "cont"]);
expect("лобби 3.0: имя — в поле лобби, не в поиск на главной",
  pick([...lobby30(), ...main30()]), ["fill", "name", "tid", "name"]);
expect("имя вписано: «Подключиться» в окне лобби",
  pick([...lobby30({ name: { isBotName: true } }), ...main30()], state({ nameFilled: true })), ["click", "join", "tid", "join"]);
expect("имя стёрто перерисовкой — вписать снова",
  pick([...lobby30(), ...main30()], state({ nameFilled: true })), ["fill", "name", "tid", "name"]);
{
  const popup = [
    btn({ text: "Начать запись", dialog: "d1", id: "rec" }),
    btn({ aria: "Пригласить участников", text: "+", dialog: "d1", id: "inv" }),
    btn({ text: "Понятно", dialog: "d1", id: "ok" }),
  ];
  expect("новая всплывашка в окне лобби: «Понятно», не запись и не приглашение",
    pick([...popup, ...lobby30(), ...main30()]), ["click", "popup_ack", "text", "ok"]);
  expect(`всплывашки закрывали уже ${MAX_POPUP_CLICKS} раз — дальше к полю имени`,
    pick([...popup, ...lobby30(), ...main30()], state({ popupClicks: MAX_POPUP_CLICKS })), ["fill", "name", "tid", "name"]);
}
{
  // Само лобби — окно поверх экрана с «Закрыть» и «Позже»: это отказ от входа.
  const lobbyDialog = [
    btn({ text: "Закрыть", dialog: "d2", id: "close" }),
    btn({ text: "Позже", dialog: "d2", id: "later" }),
    ...lobby30().map((c) => ({ ...c, dialog: "d2" })),
  ];
  expect("лобби в окне поверх экрана: «Закрыть»/«Позже» не трогаем, вписываем имя",
    pick(lobbyDialog), ["fill", "name", "tid", "name"]);
  expect("окно лобби распознано", [...lobbyDialogs(lobbyDialog)], ["0:d2"]);
}
{
  const bait = [
    btn({ tid: "end-call-alt-button", text: "Подключиться", id: "b1" }),
    btn({ aria: "Начать запись", text: "Подключиться", id: "b2" }),
    btn({ title: "Демонстрация экрана", text: "Продолжить в браузере", id: "b3" }),
    btn({ aria: "Выйти из звонка", text: "Понятно", dialog: "d3", id: "b4" }),
    btn({ tid: "record-start", text: "×", dialog: "d3", id: "b5" }),
    btn({ aria: "Включить микрофон", id: "b6" }),
    inp({ placeholder: "Ваше имя", isBotName: true, id: "nm" }),
    ...main30(),
  ];
  expect("опасные кнопки с «белыми» текстами: делать нечего (ни одной не нажимаем)", pick(bait, state({ nameFilled: true })), null);
  expect("…и появилась безопасная «Присоединиться» — только её",
    pick([...bait, btn({ text: "Присоединиться", id: "safe" })], state({ nameFilled: true })), ["click", "join", "text", "safe"]);
}
expect("поля имени нет: «Подключиться» ждёт, пока поля не видно давно",
  pick([btn({ text: "Подключиться", id: "j" })]), null);
expect("поля имени так и нет — можно нажимать",
  pick([btn({ text: "Подключиться", id: "j" })], state({ joinWithoutName: true })), ["click", "join", "text", "j"]);
{
  const j = btn({ text: "Подключиться", id: "j" });
  expect("кнопка, нажатие которой ничего не дало, больше не трогается",
    pick([j], state({ joinWithoutName: true, spent: new Set([controlSignature(j)]) })), null);
}
expect("«Хорошо» на главной без окна (чат комнаты) — не нажимаем",
  pick([btn({ ...MAIN, text: "Хорошо", id: "chat-ok" })]), null);
expect("«Понятно» плашкой в окне звонка — нажимаем",
  pick([btn({ text: "Понятно", id: "tip" })]), ["click", "popup_ack", "text", "tip"]);
expect("«Закрыть» вне окна поверх экрана — не нажимаем",
  pick([btn({ text: "Закрыть", id: "x" })]), null);
expect("«Подключиться» — в том же окне, что поле имени (не карточка звонка в чате на главной)",
  pick([btn({ ...MAIN, text: "Подключиться", id: "chat-card" }), ...lobby30({ name: { isBotName: true }, join: { tid: "cta-x7" } })],
    state({ nameFilled: true })), ["click", "join", "text", "join"]);
// Цикл5 фазы 3 (У7): поле «Как к вам обращаться?» / «Никнейм» — поле имени, а не вход без имени.
for (const ph of ["Как к вам обращаться?", "Никнейм", "Ваш псевдоним"]) {
  expect(`поле «${ph}» — поле имени`, pick([inp({ tid: "fld-9", placeholder: ph, id: "nm" }), btn({ text: "Подключиться", id: "j" })]),
    ["fill", "name", "label", "nm"]);
}
{
  const renamed = [
    inp({ tid: "name-field-v4", placeholder: "Как вас представить?", id: "nm" }),
    btn({ tid: "cta-primary", text: "Присоединиться", id: "jn" }),
  ];
  expect("переименованные метки: имя по подписи", pick(renamed), ["fill", "name", "label", "nm"]);
  expect("переименованные метки: вход по тексту",
    pick([{ ...renamed[0], isBotName: true }, renamed[1]], state({ nameFilled: true })), ["click", "join", "text", "jn"]);
}
expect("без меток: имя по подписи, не по метке",
  pick([...lobby30(), ...main30()], state({ noTestid: true })), ["fill", "name", "label", "name"]);
expect("без меток: вход по тексту",
  pick([...lobby30({ name: { isBotName: true } }), ...main30()], state({ noTestid: true, nameFilled: true })), ["click", "join", "text", "join"]);
expect("без меток: крестик онбординга по тексту «×»",
  pick([...onboarding(), ...interstitial(), ...main30()], state({ noTestid: true })), ["click", "popup_dismiss", "text", "onb-x"]);
expect("звонок идёт (панель звонка): делать нечего",
  pick([btn({ tid: "end-call-alt-button", id: "e" }), btn({ tid: "mute-audio", aria: "Выключить микрофон", id: "m" }), ...main30()]), null);

console.log("поле имени «по месту» — живое лобби 3.0 без меток (самопроверка no-testid 25.09)");
// Снимок живого лобби: поле без подсказки, подписи и <label>; кнопки «На главный
// экран», «Включить камеру», «Подключиться» (только текст), «Открыть настройки».
const liveLobby = (over: { name?: Partial<FbControl>; join?: Partial<FbControl> } = {}) => [
  btn({ tid: "to-home-screen-button", aria: "На главный экран", id: "home" }),
  inp({ tid: "orb-textinput-input", type: "text", near: "Подключение к звонку", id: "nm", ...(over.name || {}) }),
  btn({ tid: "turn-on-camera-button", aria: "Включить камеру", id: "cam" }),
  btn({ tid: "enter-conference-button", text: "Подключиться", id: "join", ...(over.join || {}) }),
  btn({ tid: "open-settings-button", aria: "Открыть настройки", id: "set" }),
];
expect("без меток: единственное поле рядом с «Подключиться» — поле имени по месту",
  pick([...liveLobby(), ...main30()], state({ noTestid: true })), ["fill", "name", "form", "nm"]);
expect("…кнопка «Подключиться» неактивна, пока нет имени — всё равно поле имени",
  pick([...liveLobby({ join: { disabled: true } }), ...main30()], state({ noTestid: true })), ["fill", "name", "form", "nm"]);
expect("…имя вписано — «Подключиться» по тексту",
  pick([...liveLobby({ name: { isBotName: true } }), ...main30()], state({ noTestid: true, nameFilled: true })), ["click", "join", "text", "join"]);
expect("с метками (обычный запасной путь) — как раньше, по метке",
  pick([...liveLobby(), ...main30()]), ["fill", "name", "tid", "nm"]);
expect("два поля без подписей в окне — не угадываем (имени нет, вход без имени не разрешён)",
  pick([...liveLobby(), inp({ tid: "orb-textinput-input", type: "text", id: "nm2" }), ...main30()], state({ noTestid: true })), null);
expect("два поля — структурный ярус пуст",
  [...structuralNameInputs([...liveLobby(), inp({ type: "text", id: "nm2" })], true)], []);
expect("в окне нет «Подключиться» — не по месту",
  [...structuralNameInputs(liveLobby().filter((c) => c.id !== "join"), true)], []);
expect("«Подключиться» только в другом окне — не по месту",
  [...structuralNameInputs([...liveLobby().filter((c) => c.id !== "join"), btn({ ...MAIN, text: "Подключиться", id: "j2" })], true)], []);
{
  // 3.0: поле только в окне чата (главное), там же карточка «Подключиться»; лобби
  // во вложенном окне без поля — в поле чата имя не вписываем никогда.
  const chatOnly = [
    btn({ tid: "enter-conference-button", text: "Подключиться", id: "join" }),
    inp({ ...MAIN, type: "text", id: "chat-in" }),
    btn({ ...MAIN, text: "Подключиться", id: "chat-card" }),
  ];
  expect("3.0: поле только в чате главного окна — не поле имени по месту", [...structuralNameInputs(chatOnly, true)], []);
  const a = chooseFallbackAction(chatOnly, state({ noTestid: true }));
  expect("3.0: поле только в чате главного окна — не вписываем", !!(a && a.kind === "fill"), false);
}
expect("старый интерфейс (окна звонка нет): поле на главной рядом с «Подключиться» — по месту",
  [...structuralNameInputs([inp({ ...MAIN, type: "text", id: "old-nm" }), btn({ ...MAIN, text: "Подключиться", id: "old-j" })], true)], ["old-nm"]);
expect("поиск рядом с «Подключиться» — не поле имени по месту",
  [...structuralNameInputs([inp({ type: "search", id: "s" }), btn({ text: "Подключиться", id: "j" })], true)], []);
expect("строка «Сообщение» рядом с «Подключиться» — не поле имени по месту",
  [...structuralNameInputs([inp({ placeholder: "Сообщение", id: "m" }), btn({ text: "Подключиться", id: "j" })], true)], []);
expect("строка владельцу: «по месту»",
  fallbackSummary(null, "no-testid", [{ target: "name", tier: "form", frame: "call", method: "fill", ok: true }]).includes("имя — по месту"), true);
expect("шаги для машины: name:form",
  fallbackStepsCode([{ target: "name", tier: "form", frame: "call", method: "fill", ok: true }]), "name:form");

console.log("lobbyControlsVisible — «в звонке» до входа не засчитывать");
expect("виджет главной 3.0 без лобби — лобби не видно", lobbyControlsVisible(main30()), false);
expect("интерстициал — лобби видно", lobbyControlsVisible([...interstitial(), ...main30()]), true);
expect("лобби — видно", lobbyControlsVisible(lobby30()), true);

console.log("fallbackSummary / fallbackStepsCode — наши слова для владельца");
const steps = [
  { target: "popup_ack" as const, tier: "text" as const, frame: "call" as const, method: "click", ok: true },
  { target: "name" as const, tier: "label" as const, frame: "call" as const, method: "fill", ok: true },
  { target: "join" as const, tier: "text" as const, frame: "call" as const, method: "click", ok: false },
  { target: "join" as const, tier: "text" as const, frame: "call" as const, method: "click+js_click", ok: true },
];
expect("строка владельцу", fallbackSummary("name_input_not_found", null, steps),
  "основной путь не нашёл поле имени; запасной: закрыл всплывающее окно (по тексту), имя — по подписи, «Подключиться» — по тексту");
expect("принудительно, без меток", fallbackSummary(null, "no-testid", []).startsWith("принудительная проверка запасного пути без меток"), true);
expect("шаги для машины (только удачные)", fallbackStepsCode(steps), "popup_ack:text,name:label,join:text");
expect("вошёл без имени — владелец видит это первым (цикл5 фазы 3, Н7)",
  fallbackSummary("name_input_not_found", null, [steps[3]], true).startsWith("⚠️ вошёл БЕЗ ИМЕНИ"), true);
expect("с именем — без предупреждения", fallbackSummary("name_input_not_found", null, steps).includes("БЕЗ ИМЕНИ"), false);

console.log("fallbackForceMode — флаг живой проверки в сыром BOT_CONFIG");
const saved = process.env.BOT_CONFIG;
const modeWith = (cfg: any) => {
  process.env.BOT_CONFIG = JSON.stringify(cfg);
  return fallbackForceMode();
};
expect("по умолчанию выключен", modeWith({ sessionUid: "x" }), null);
expect("true — все ярусы", modeWith({ telemostForceFallback: true }), "all");
expect("\"no-testid\" — без меток", modeWith({ telemostForceFallback: "no-testid" }), "no-testid");
expect("false / мусор — выключен", [modeWith({ telemostForceFallback: false }), modeWith({ telemostForceFallback: "yes please" })], [null, null]);
process.env.BOT_CONFIG = "не json";
expect("битый BOT_CONFIG — выключен", fallbackForceMode(), null);
if (saved === undefined) delete process.env.BOT_CONFIG;
else process.env.BOT_CONFIG = saved;

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
