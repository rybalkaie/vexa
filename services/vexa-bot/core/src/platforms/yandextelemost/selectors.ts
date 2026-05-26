// Yandex Telemost селекторы. Подтверждены живой разведкой Playwright 2026-05-26
// на https://telemost.yandex.ru/j/<id>. Все ключевые элементы lobby имеют
// стабильные data-testid из дизайн-системы Orb.
//
// Если Telemost обновит фронт — менять здесь, не размазывать по всему адаптеру.

// Промежуточный экран «Вы подключаетесь к видеовстрече» с одной кнопкой.
export const telemostInterstitialContinueSelectors: string[] = [
  '[data-testid="to-home-screen-button"]',
  'button:has-text("Продолжить в браузере")',
];

// Lobby: поле ввода имени.
export const telemostNameInputSelectors: string[] = [
  '[data-testid="orb-textinput-input"]',
  'input[data-testid="orb-textinput-input"]',
];

// Lobby: кнопка «Подключиться» (вход в комнату).
export const telemostJoinButtonSelectors: string[] = [
  '[data-testid="enter-conference-button"]',
  'button:has-text("Подключиться")',
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
// Предположение основано на Orb naming convention — уточним из логов.
export const telemostInMeetingIndicators: string[] = [
  '[data-testid*="leave-conference"]',
  '[data-testid*="leave-call"]',
  '[data-testid*="meeting-toolbar"]',
  '[data-testid*="conference-toolbar"]',
  '[aria-label*="Выйти"]',
  'button[aria-label*="ыйти"]',
];

// In-meeting: кнопка выхода. Логировать все потенциальные совпадения.
export const telemostLeaveButtonSelectors: string[] = [
  '[data-testid="leave-conference-button"]',
  '[data-testid="leave-call-button"]',
  '[data-testid*="leave"]',
  'button[aria-label*="Выйти из встречи"]',
  'button[aria-label*="Выйти"]',
  'button:has-text("Выйти")',
];

// In-meeting: участники / тайлы видео. Гипотеза — уточним из логов.
export const telemostParticipantSelectors: string[] = [
  '[data-testid*="participant-tile"]',
  '[data-testid*="participant"]',
  '[data-testid*="video-tile"]',
  '[data-testid*="user-tile"]',
];

// Indicators ошибки/удаления из встречи.
export const telemostRemovalIndicators: string[] = [
  'text="Встреча завершена"',
  'text*="завершена"',
  'text*="отключены"',
  'text*="вышли из встречи"',
  '[data-testid*="meeting-ended"]',
];
