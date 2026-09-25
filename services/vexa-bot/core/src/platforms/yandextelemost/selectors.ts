// Yandex Telemost селекторы. Подтверждены живой разведкой Playwright 2026-05-26
// на https://telemost.yandex.ru/j/<id>. Все ключевые элементы lobby имеют
// стабильные data-testid из дизайн-системы Orb.
//
// Если Telemost обновит фронт — менять здесь, не размазывать по всему адаптеру.
//
// История изменений:
// - Ф3 (2026-05-26): закреплены реальные in-meeting testid'ы из DOM-dump
//   прогона Ф2 (handoff п.5).
// - 2026-09-23: «Телемост 3.0» (выкачен ~21.09). Лобби (поле имени, кнопки
//   микрофона/камеры, «Подключиться») с ПРЕЖНИМИ testid переехало во вложенный
//   iframe `telemost.yandex.ru/private-join/<id>` — на главной странице его нет,
//   там только виджет звонка («Групповой звонок / Подключение», кнопки с
//   aria-label «Завершить звонок», «Выключить микрофон»). Сам звонок (WebRTC)
//   живёт в том же iframe. Поверх всего при загрузке — окно онбординга «Большое
//   обновление в Телемосте». Гость в 3.0 по умолчанию с ВКЛ микрофоном и камерой.
//   Проверено живым заходом гостя в Т-10 изнутри образа бота 23.09.2026.

// Промежуточный экран «Подключение к звонку» (в 3.0 — со своим testid).
// ВАЖНО: `to-home-screen-button` — это кнопка «На главную» (стрелка-крест в углу),
// она НЕ ведёт в lobby. Игнорируем её.
export const telemostInterstitialContinueSelectors: string[] = [
  '[data-testid="meeting-continue-in-browser-continue"]',
  'button:has-text("Продолжить в браузере")',
];

// Телемост 3.0: окно онбординга поверх интерстициала. Перекрывает «Продолжить в
// браузере» — Playwright-клик упирается в него. Браузер бота стартует с чистым
// профилем, поэтому окно будет на каждом заходе.
export const telemostOnboardingDismissSelectors: string[] = [
  '[data-testid="telemost-3-onboarding-close"]',
  '[data-testid="telemost-3-onboarding-confirm"]',
];

// Телемост 3.0: iframe с лобби и самим звонком — ищем по куску URL.
export const telemostLobbyFrameUrlMarker = "/private-join/";

// Lobby: поле ввода имени (в 3.0 — внутри iframe лобби). В 3.0 тот же testid носит поле
// поиска чатов (placeholder «Поиск») — его исключаем, иначе бот впечатает
// своё имя в поиск.
export const telemostNameInputSelectors: string[] = [
  'input[data-testid="orb-textinput-input"]:not([placeholder="Поиск"]):not([placeholder="Search"])',
];

// Lobby: кнопка «Подключиться» (вход в комнату).
export const telemostJoinButtonSelectors: string[] = [
  '[data-testid="enter-conference-button"]',
  'button:has-text("Подключиться")',
];

// Lobby: блокирующие модалки, появившиеся у Telemost 2026-07-22.
// Регресс: Telemost стал показывать поверх формы лобби модалки
// «Включить микрофон не удалось» / «Включить видео не удалось» (у headless-бота
// нет медиаустройств) и модалку «Частые вопросы». Они перекрывают/прячут поле
// имени → `fillNameInput` не находит его и заход падает `name_input_not_found`.
// Гасим их перед вводом имени: кнопки «Понятно» + крестик модалки.
export const telemostModalDismissSelectors: string[] = [
  'button:has-text("Понятно")',
  '[data-testid="orb-button-close"]',
];

// Lobby: микрофон. ВАЖНО: по дефолту микрофон ВЫКЛЮЧЕН, кнопка ВКЛЮЧАЕТ.
// Бот никогда не кликает по ней — но используем для детекта статуса.
export const telemostMicButtonSelectors: string[] = [
  '[data-testid="turn-on-mic-button"]',
  '[data-testid="turn-off-mic-button"]',
];

// Lobby: камера. По дефолту ВЫКЛЮЧЕНА. Бот не кликает.
export const telemostCameraButtonSelectors: string[] = [
  '[data-testid="turn-on-camera-button"]',
  '[data-testid="turn-off-camera-button"]',
];

// Waiting room (ожидание admit от хоста). Конкретный селектор/текст
// предстоит подтвердить на первом прогоне — список обновим из логов.
export const telemostWaitingRoomIndicators: string[] = [
  'text="Ожидание организатора"',
  'text*="ожидайте"',
  'text*="впустит"',
  'text*="Дождитесь"',
  '[data-testid*="waiting"]',
  '[data-testid*="lobby"]',
];

// Невалидная / закрытая встреча.
export const telemostRejectionIndicators: string[] = [
  'text="Такой встречи не существует"',
  'text*="встречи не существует"',
  'text*="встреча завершена"',
  'text*="отказано"',
];

// In-meeting: панель/тулбар встречи (виден когда мы в комнате).
// Источник: DOM-dump прогона Ф2 (2026-05-26); первая строка — Телемост 3.0.
// «Завершить звонок» в 3.0 виден и в лобби (виджет на главной) — поэтому
// join.ts сам проверяет, что лобби закрылось, прежде чем вернуть управление.
export const telemostInMeetingIndicators: string[] = [
  'button[aria-label="Завершить звонок"]',
  '[data-testid="end-call-alt-button"]',
  '[data-testid="mute-audio"]',
  '[aria-label*="Выйти из встречи"]',
  '[aria-label*="Выйти"]',
];

// In-meeting: кнопка выхода. Список уходит в document.querySelector внутри
// страницы (leave.ts) — только чистый CSS, без Playwright-расширений вроде :has-text.
// В 3.0 «Завершить звонок» уводит из звонка сразу, без окна подтверждения.
export const telemostLeaveButtonSelectors: string[] = [
  'button[aria-label="Завершить звонок"]',
  '[data-testid="end-call-alt-button"]',
  'button[aria-label*="Выйти из встречи"]',
  'button[aria-label*="Выйти"]',
];

// Телемост 3.0: микрофон и камера в звонке. «Выключить …» видна, когда устройство
// ВКЛЮЧЕНО. Гость в 3.0 заходит с включённым микрофоном (проверено изнутри образа),
// бот — наблюдатель, поэтому выключает сам.
export const telemostCallMicOnSelectors: string[] = [
  'button[aria-label="Выключить микрофон"]',
];
export const telemostCallMicOffSelectors: string[] = [
  'button[aria-label="Включить микрофон"]',
];
export const telemostCallCameraOnSelectors: string[] = [
  'button[aria-label="Выключить камеру"]',
];

// In-meeting: кнопки управления mic/camera в meeting (отличаются от lobby).
export const telemostInMeetingMicButtonSelectors: string[] = [
  '[data-testid="mute-audio"]',
  '[data-testid="turn-on-mic-button"]',
  '[data-testid="turn-off-mic-button"]',
];

export const telemostInMeetingCameraButtonSelectors: string[] = [
  '[data-testid="turn-on-camera-button"]',
  '[data-testid="turn-off-camera-button"]',
];

// Кнопка «Поднять руку» — пока не используется, но подтверждена в DOM-dump.
export const telemostRaiseHandButtonSelectors: string[] = [
  '[data-testid="hand-up-button"]',
];

// Кнопка «Демонстрация» — пока не используется.
export const telemostShareScreenButtonSelectors: string[] = [
  '[data-testid="start-demonstration-button"]',
];

// Кнопка открытия панели участников — ВХОД ДЛЯ МАППИНГА ИМЁН В Ф3.
// Клик откроет панель, далее polling её DOM для списка имён.
export const telemostParticipantsButtonSelectors: string[] = [
  '[data-testid="participants-button"]',
  'button[aria-label*="частник"]',
  'button[aria-label*="part"]',
];

// Кнопка чата — пока не используется.
export const telemostChatButtonSelectors: string[] = [
  '[data-testid="chat-alt-button"]',
];

// In-meeting: participant tiles. Хайпотезы — точные селекторы будут разведаны
// на первом боевом прогоне через polling-метод.
// Известно из DOM-dump Ф2: на прогоне с одним говорящим тайлов других участников
// не было, точные тайл-селекторы не зафиксированы. Оставляем гипотезы +
// добавляем fallback на video-элементы внутри meeting-области.
export const telemostParticipantTileSelectors: string[] = [
  '[data-testid*="participant-tile"]',
  '[data-testid*="participant"]:not([data-testid="participants-button"])',
  '[data-testid*="video-tile"]',
  '[data-testid*="user-tile"]',
];

// In-meeting: панель участников после клика по participants-button.
// Имена будут где-то внутри списка — селекторы участника-в-списке тоже гипотезы.
// В participants.ts polling будет дампить DOM панели для разведки на первом прогоне.
export const telemostParticipantsPanelSelectors: string[] = [
  '[data-testid="participants-panel"]',
  '[data-testid*="participants-list"]',
  '[role="dialog"]',
  '[class*="participants"]',
];

// Indicators ошибки/удаления из встречи.
export const telemostRemovalIndicators: string[] = [
  'text="Встреча завершена"',
  'text*="завершена"',
  'text*="отключены"',
  'text*="вышли из встречи"',
  '[data-testid*="meeting-ended"]',
];

// ---------------------------------------------------------------------------
// Телемост 3.0 — живая разведка 25.09.2026 (Т-10, два тестовых участника).
// Старые списки выше не трогаем: они нужны старому интерфейсу. Строгие признаки
// экрана (зал ожидания, отказ, конец встречи, «в звонке») собраны в screen.ts
// и ищутся во ВСЕХ окнах, а не только на главной.
//
// Вложенное окно звонка `/private-join/` (testid): end-call-alt-button,
// more-popup-alt-button, mute-audio (подпись «Выключить микрофон» /
// «Включить микрофон»), turn-off-mic-button, turn-on-camera-button,
// settings-button, share-button, participant-video-placeholder; плитки —
// классы newGridItem_*, participantName_*, avatarWithNameBlock_*.
// Главное окно: мини-виджет с aria-label «Открыть экран звонка» и текстом
// «N участник(а/ов)».
// ---------------------------------------------------------------------------

// Мини-виджет звонка на главной: в его тексте счётчик «N участников».
export const telemostCallWidgetAria = "Открыть экран звонка";

// Имена на плитках во вложенном окне звонка (панель участников — запасной путь).
export const telemostParticipantNameSelectors: string[] = [
  '[class*="participantName_"]',
];

// Выход из звонка в 3.0 — кнопка во вложенном окне. Порядок важен: окно звонка
// обходится первым, внутри него первой пробуется кнопка 3.0.
export const telemostLeaveButtonSelectors3: string[] = [
  '[data-testid="end-call-alt-button"]',
  ...telemostLeaveButtonSelectors,
];

// Микрофон и камера (лобби и звонок, оба интерфейса): по подписи и testid бот
// понимает состояние — подпись «Выключить …» / testid turn-off-* видна, когда
// устройство ВКЛЮЧЕНО, — и что нажать. Решение — devices.ts (readDeviceState).
export type TelemostDeviceLabels = {
  onAria: string;
  offAria: string;
  onTid: string;
  offTid: string;
  /** Кнопка панели звонка 3.0 с меняющейся подписью. */
  toolbarTid: string | null;
};
export const telemostDeviceLabels: { mic: TelemostDeviceLabels; camera: TelemostDeviceLabels } = {
  mic: {
    onAria: "Выключить микрофон",
    offAria: "Включить микрофон",
    onTid: "turn-off-mic-button",
    offTid: "turn-on-mic-button",
    toolbarTid: "mute-audio",
  },
  camera: {
    onAria: "Выключить камеру",
    offAria: "Включить камеру",
    onTid: "turn-off-camera-button",
    offTid: "turn-on-camera-button",
    toolbarTid: null,
  },
};
