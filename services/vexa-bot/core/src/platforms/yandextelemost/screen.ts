// Экран Телемоста: что сейчас видит бот — во ВСЕХ окнах страницы.
// (ISS-60, фаза 3а плана plans/2026-09-23-telemost-update-resilience.md, 25.09.2026)
//
// ЗАЧЕМ. В Телемосте 3.0 лобби и сам звонок живут во вложенном окне
// `/private-join/`, а на главной странице — виджет звонка, где «Завершить звонок»
// виден уже в лобби. Старые проверки смотрели только главное окно и проверяли
// «в звонке» раньше «зала ожидания»: бот в зале ожидания считал себя в звонке,
// отказ и конец встречи в 3.0 не видел вовсе, а при неясном экране объявлял себя
// в звонке «методом исключения».
//
// ЧТО ДЕЛАЕТ. collectFrameSignals снимает с каждого окна лёгкие признаки
// (видимые строки текста, data-testid, подписи кнопок, поле имени, кнопка
// «Подключиться», виджет звонка), а ЧИСТАЯ функция classifyTelemostScreen
// решает в строгом порядке:
//   1. встречи нет / отказали          — строки текста в любом окне без панели
//                                        звонка (в окне с панелью это чат);
//   2. зал ожидания                    — строки текста в любом окне, точные метки;
//      (цикл5 фазы 3: в 1–2 текст не решает, если в окне звонка видны плитки
//      ДРУГИХ людей — это уже звонок, а строки — реплики и имена; главное окно 3.0
//      с мини-виджетом звонка — чат комнаты, его строки не читаются);
//   3. лобби                           — видна «Подключиться», или поле имени в
//                                        окне без признаков звонка (в 3.0 — только
//                                        в окне звонка: на главной чат и поиск);
//   4. в звонке                        — 3.0: кнопки звонка или плитки участников
//                                        во вложенном окне, либо счётчик «N
//                                        участников» в виджете (НЕ «Завершить
//                                        звонок» на главной — он виден уже в
//                                        лобби); старый интерфейс: кнопки звонка
//                                        на главной;
//   5. зал ожидания по старым широким меткам (`*waiting*`, `*lobby*`) — только
//      если признаков звонка нет: они не подтверждены вживую и не имеют права
//      перебить «в звонке»;
//   6. «неясно» — и это НЕ «в звонке».
//
// Приватность: строки экрана (там могут быть имена и реплики чата) не пишутся
// в лог и не покидают процесс — наружу уходит только итог и имя признака.

import { Frame, Page } from "playwright";
import { classifyJoinError, isMeetingGoneText } from "./join-status";
import {
  telemostLobbyFrameUrlMarker,
  telemostNameInputSelectors,
  telemostCallWidgetAria,
  telemostParticipantNameSelectors,
} from "./selectors";

export type FrameKind = "main" | "call" | "other";
export type ScreenButton = { tid: string | null; aria: string | null; text: string };

export type FrameSignals = {
  kind: FrameKind;
  /** Видимые строки текста окна (обрезаны, без пустых). */
  lines: string[];
  /** Видимые data-testid. */
  testids: string[];
  /** Видимые кнопки: testid, подпись (aria-label), текст. */
  buttons: ScreenButton[];
  /** Подписи (aria-label) всех видимых элементов. */
  arias: string[];
  /** Текст виджета звонка на главной («Открыть экран звонка»). */
  widgets: string[];
  /** Сколько плиток с именем участника (`participantName_*`) есть в окне. */
  tiles: number;
  /** Видно поле ввода имени лобби (не поиск чатов). */
  nameInput: boolean;
  /** Видна кнопка «Подключиться». */
  joinButton: boolean;
};

export type ScreenState = "gone" | "rejected" | "waiting_room" | "lobby" | "in_call" | "unknown";
export type ScreenVerdict = { state: ScreenState; ui: "3.0" | "old"; evidence: string };

// ---------------------------------------------------------------------------
// Ссылка встречи (хвост С10): бот открывает только Телемост. Та же проверка на
// хосте — scripts/notary/lib/telemost_url.py; в дикторе самопроверки — своя копия.
// ---------------------------------------------------------------------------

const TELEMOST_HOST_RE = /^(?:[a-z0-9-]+\.)*telemost(?:\.360)?\.yandex\.ru$/i;

/** Ссылка ведёт в Яндекс Телемост: хост целиком (или поддомен), не подстрока. */
export function isTelemostMeetingUrl(raw: string): boolean {
  const s = String(raw || "").trim();
  // Обратная косая черта, пробелы и управляющие символы: браузер и проверка
  // могли бы понять адрес по-разному.
  if (!s || /[\s\\\u0000-\u001f\u007f]/.test(s)) return false;
  let u: URL;
  try {
    u = new URL(s);
  } catch {
    return false;
  }
  if (u.protocol !== "https:" && u.protocol !== "http:") return false;
  if (u.username || u.password) return false;
  if (u.port) return false; // стандартный порт URL сам убирает; любой другой — не Телемост
  return TELEMOST_HOST_RE.test(u.hostname.replace(/\.$/, ""));
}

// ---------------------------------------------------------------------------
// Признаки (чистые). Строки проверяются ПО ОТДЕЛЬНОСТИ и только короткие: длинные
// пояснения и справка не должны превращать экран в «отказ» или «зал ожидания».
// ---------------------------------------------------------------------------

// Отказ во входе. Старый признак — «отказано». Строки про доступ к устройствам
// («доступ к микрофону запрещён / отказано») отказом во входе не считаются.
const REJECTED_RE =
  /(отказано|вам отказали|организатор (отклонил|не впустил)|отклонил[аи]? (ваш )?(запрос|заявку)|запрос на (вход|подключение) отклон[её]н|вас не впустили|access denied|request (was )?(denied|declined|rejected)|host (denied|declined|rejected))/i;
const DEVICE_LINE_RE = /(микрофон|камер|динамик|устройств|microphone|camera|device)/i;

// Зал ожидания — точные формулировки (старые признаки + вероятные в 3.0).
const WAITING_RE =
  /(ожидание организатора|зал[еу]? ожидания|ожидайте|дождитесь|впустит|скоро (вас )?(впустят|пустят|подключат)|ждём,? (пока|когда) организатор|waiting room|wait(ing)? for the (host|organizer)|host will (let you in|admit you)|asking to join)/i;
// Точные метки зала ожидания — сильный признак (в отличие от широких `*waiting*`).
const WAITING_TID_STRONG_RE = /(waiting[-_]?room|wait[-_]?room|lobby[-_]?wait|waiting[-_]?for[-_]?(host|organizer|admission)|knock)/i;
// Старые широкие метки (`[data-testid*="waiting"]`, `[data-testid*="lobby"]`) — гипотеза Ф2.
const WAITING_TID_WEAK_RE = /(waiting|lobby)/i;

// Нас убрали / встреча кончилась — для сторожа выброса (removal.ts).
const REMOVED_RE =
  /(вы вышли из (встречи|звонка|конференции)|вас (удалили|исключили|отключили)|организатор (завершил|закончил) (встречу|звонок)|звонок (завершён|завершен|окончен|закончен)|встреча (завершена|окончена|закончена)|you (were|have been) removed|you left the (meeting|call))/i;
const REMOVED_TID_RE = /(meeting[-_]?ended|call[-_]?ended|conference[-_]?ended)/i;

// Признаки идущего звонка 3.0 во вложенном окне (разведка 25.09): кнопка выхода,
// кнопка микрофона панели, заглушка видео участника. Панель звонка (выход,
// микрофон) — сильнее: заглушка видео и плитка с именем могут оказаться и в
// предпросмотре лобби, поэтому поле имени перебивают только кнопки панели.
const CALL_CONTROL_TIDS = ["end-call-alt-button", "mute-audio"];
const CALL_UI_TIDS = [...CALL_CONTROL_TIDS, "participant-video-placeholder"];

// Счётчик в виджете звонка: «10\n2 участника\n•\n00:36» → 2.
const PARTICIPANT_COUNT_RE = /(\d{1,4})\s*(участник|participant)/i;

/** «N участник(а/ов)» → N; нет счётчика → null. */
export function parseParticipantCount(text: string): number | null {
  const m = String(text || "").match(PARTICIPANT_COUNT_RE);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isInteger(n) && n >= 0 && n <= 5000 ? n : null;
}

function findLine(
  frames: FrameSignals[],
  re: RegExp,
  opts: { maxLen: number; allowQuestion: boolean; exclude?: RegExp },
): string | null {
  for (const f of frames) {
    for (const raw of f.lines) {
      const line = raw.replace(/\s+/g, " ").trim();
      if (!line || line.length > opts.maxLen) continue;
      if (!opts.allowQuestion && line.includes("?")) continue;
      if (opts.exclude && opts.exclude.test(line)) continue;
      if (re.test(line)) return f.kind;
    }
  }
  return null;
}

/** Окна, где у данного интерфейса живёт экран звонка. */
function callScope(frames: FrameSignals[], ui: "3.0" | "old"): FrameSignals[] {
  return frames.filter((f) => (ui === "3.0" ? f.kind === "call" : f.kind === "main"));
}

/** Счётчик мини-виджета звонка «N участников» (главное окно 3.0) или null. */
function widgetCount(frames: FrameSignals[]): number | null {
  let best: number | null = null;
  for (const f of frames) {
    for (const w of f.widgets) {
      const n = parseParticipantCount(w);
      if (n !== null) best = best === null ? n : Math.max(best, n);
    }
  }
  return best;
}

/**
 * В звонке видны ДРУГИЕ люди (цикл5 фазы 3, В1): в окне звонка 3.0 две плитки с
 * именами и больше (плитки считаются по людям — разведка 25.09). Из зала ожидания
 * и лобби чужих плиток не видно (там максимум своя плитка предпросмотра) — значит,
 * бот в звонке, и строки «дождитесь / встреча завершена / отказано» в окнах — это
 * реплики чата или имена людей, а не экран. Счётчик виджета здесь не берём: в зале
 * ожидания 3.0 (вживую не видели) он мог бы показывать тех, кто уже в звонке.
 */
function othersVisible(frames: FrameSignals[], ui: "3.0" | "old"): boolean {
  return ui === "3.0" && frames.some((f) => f.kind === "call" && f.tiles >= 2);
}

/**
 * Окна, чей ТЕКСТ говорит о состоянии входа (цикл5 фазы 3, В1). В 3.0 главное
 * окно — чат комнаты (README адаптера): когда на нём мини-виджет звонка со
 * счётчиком, бот уже в звонке, и строки главного окна — реплики людей, а не экран.
 */
function stateTextFrames(frames: FrameSignals[], ui: "3.0" | "old"): FrameSignals[] {
  if (ui !== "3.0" || widgetCount(frames) === null) return frames;
  return frames.filter((f) => f.kind !== "main");
}

/**
 * Что на экране — ЧИСТАЯ функция (тест: screen.test.ts). Порядок — в шапке файла.
 * «unknown» не означает «в звонке».
 */
export function classifyTelemostScreen(frames: FrameSignals[]): ScreenVerdict {
  const ui: "3.0" | "old" = frames.some((f) => f.kind === "call") ? "3.0" : "old";
  const v = (state: ScreenState, evidence: string): ScreenVerdict => ({ state, ui, evidence });

  // 1. Встречи нет / отказали — в любом окне, где НЕТ панели звонка. Окно с
  //    панелью звонка — это идущий звонок: строка «Звонок завершён» / «отказано»
  //    там — сообщение чата (в 3.0 звонок живёт в чате комнаты, и в нём может
  //    висеть «Звонок завершён» от прошлой встречи). Ошибиться в эту сторону
  //    дешевле: ложное «в звонке» кончится тишиной, ложное «встречи нет» —
  //    потерянной встречей без повтора.
  //    Цикл5 фазы 3 (В1): текст решает экран, только пока чужих людей в звонке
  //    не видно, и без главного окна 3.0, когда там уже виджет звонка (чат комнаты).
  const others = othersVisible(frames, ui);
  const textFrames = others ? [] : stateTextFrames(frames, ui);
  const noControls = textFrames.filter((f) => !callControlsPresent(f));
  for (const f of noControls) {
    if (isMeetingGoneText(f.lines.join("\n"))) return v("gone", `${f.kind}:text_meeting_gone`);
  }
  const rej = findLine(noControls, REJECTED_RE, { maxLen: 120, allowQuestion: false, exclude: DEVICE_LINE_RE });
  if (rej) return v("rejected", `${rej}:text_rejected`);

  // 2. Зал ожидания — текстом в любом окне или точной меткой (раньше «в звонке»).
  //    Текстом — только пока чужих людей в звонке не видно (см. шаг 1): реплика
  //    «дождитесь Ивана» или участник по имени «Ожидайте» не выгоняют бота.
  const wait = findLine(textFrames, WAITING_RE, { maxLen: 160, allowQuestion: true });
  if (wait) return v("waiting_room", `${wait}:text_waiting`);
  for (const f of callScope(frames, ui)) {
    const tid = f.testids.find((t) => WAITING_TID_STRONG_RE.test(t));
    if (tid) return v("waiting_room", `${f.kind}:tid_${tid}`);
  }

  // 3. Лобби: «Подключиться» ещё видна; или видно поле имени, а панели звонка
  //    (выход, микрофон) в этом окне нет (поле ввода рядом с идущим звонком — не
  //    лобби, а, скорее, поле чата; плитка предпросмотра лобби не в счёт).
  //    В 3.0 лобби — только в окне звонка (цикл5 фазы 3): на главной (чат комнаты)
  //    то же `data-testid` носит поиск чатов, а «Подключиться» может оказаться
  //    карточкой звонка в чате — смена подсказки поиска или такая карточка не
  //    должны превращать идущий звонок в «лобби».
  for (const f of frames) {
    if (ui === "3.0" && f.kind !== "call") continue;
    if (f.joinButton) return v("lobby", `${f.kind}:join_button`);
    if (f.nameInput && !callControlsPresent(f)) return v("lobby", `${f.kind}:name_input`);
  }

  // 4. В звонке.
  if (ui === "3.0") {
    for (const f of frames.filter((x) => x.kind === "call")) {
      for (const tid of CALL_UI_TIDS) {
        if (f.testids.includes(tid)) return v("in_call", `call:${tid}`);
      }
      if (f.tiles > 0) return v("in_call", "call:participant_tiles");
    }
    // «Завершить звонок» на главной в 3.0 — НЕ признак: он виден уже в лобби.
    for (const f of frames) {
      if (f.widgets.some((w) => parseParticipantCount(w) !== null)) return v("in_call", `${f.kind}:call_widget_count`);
    }
  } else {
    for (const f of frames.filter((x) => x.kind === "main")) {
      if (f.buttons.some((b) => b.aria === "Завершить звонок")) return v("in_call", "main:aria_end_call");
      for (const tid of ["end-call-alt-button", "mute-audio"]) {
        if (f.testids.includes(tid)) return v("in_call", `main:${tid}`);
      }
      if (f.arias.some((a) => a.includes("Выйти из встречи") || a.includes("Выйти"))) return v("in_call", "main:aria_leave");
    }
  }

  // 5. Старые широкие метки зала ожидания — только если звонка не видно.
  for (const f of callScope(frames, ui)) {
    const tid = f.testids.find((t) => WAITING_TID_WEAK_RE.test(t));
    if (tid) return v("waiting_room", `${f.kind}:tid_weak_${tid}`);
  }

  return v("unknown", "none");
}

/** В окне видна панель звонка: выход или микрофон панели. */
export function callControlsPresent(f: FrameSignals): boolean {
  return CALL_CONTROL_TIDS.some((t) => f.testids.includes(t)) || f.buttons.some((b) => b.aria === "Завершить звонок");
}

/** В окне идёт звонок: видна панель (выход, микрофон), заглушка видео или плитки участников. */
export function callUiPresent(f: FrameSignals): boolean {
  return callControlsPresent(f) || CALL_UI_TIDS.some((t) => f.testids.includes(t)) || f.tiles > 0;
}

/**
 * Признаки «нас убрали / встреча кончилась» (для removal.ts) — ЧИСТАЯ функция.
 * Только короткие отдельные строки и точные метки. Пока хоть в одном окне видна
 * панель звонка или плитки участников, встреча идёт: строка «встреча завершена»
 * тогда — скорее реплика чата или превью другого звонка на главной, а ложный
 * выход с живой встречи хуже позднего (конец встречи всё равно поймают тишина
 * и адрес страницы). Возвращает имя признака или null.
 */
export function removalEvidence(frames: FrameSignals[]): string | null {
  if (frames.some(callUiPresent)) return null;
  for (const f of frames) {
    if (isMeetingGoneText(f.lines.join("\n"))) return `${f.kind}:text_meeting_gone`;
    if (findLine([f], REMOVED_RE, { maxLen: 60, allowQuestion: false })) return `${f.kind}:text_removed`;
    const tid = f.testids.find((t) => REMOVED_TID_RE.test(t));
    if (tid) return `${f.kind}:tid_${tid}`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Сбор признаков в браузере
// ---------------------------------------------------------------------------

/** Подсказки полей, которые НЕ поле имени лобби: поиск, строка сообщения чата. */
const NOT_NAME_INPUT_RE = /(поиск|найти|search|find|сообщени|написать|напишите|message)/i;

type SnapshotArgs = { nameSel: string; notNameRe: string; joinSel: string; joinText: string; widgetAria: string; tileSel: string };
type RawSignals = Omit<FrameSignals, "kind">;

/** Выполняется ВНУТРИ окна страницы — только собственные переменные. */
function snapshotSignalsInPage(a: SnapshotArgs): RawSignals {
  const vis = (el: Element): boolean => {
    const h = el as HTMLElement;
    if (!(h.offsetWidth || h.offsetHeight || h.getClientRects().length)) return false;
    const cs = getComputedStyle(h);
    return cs.visibility !== "hidden" && cs.display !== "none";
  };
  const cut = (s: string | null | undefined, n: number) => (s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const body = document.body;
  const text = body ? body.innerText || "" : "";
  const lines = text
    .split(/\n+/)
    .map((l) => l.replace(/\s+/g, " ").trim())
    .filter((l) => l.length > 0 && l.length <= 240)
    .slice(0, 400);
  const testids = Array.from(
    new Set(
      Array.from(document.querySelectorAll("[data-testid]"))
        .filter(vis)
        .map((e) => e.getAttribute("data-testid") || ""),
    ),
  )
    .filter(Boolean)
    .slice(0, 300);
  // Кнопки с меткой или подписью — первыми: на большой встрече безымянные
  // кнопки плиток не должны вытеснить из списка панель звонка.
  const allButtons = Array.from(document.querySelectorAll("button, [role='button']")).filter(vis);
  const labelledFirst = [
    ...allButtons.filter((b) => b.hasAttribute("data-testid") || b.hasAttribute("aria-label")),
    ...allButtons.filter((b) => !b.hasAttribute("data-testid") && !b.hasAttribute("aria-label")),
  ];
  const buttons = labelledFirst
    .slice(0, 400)
    .map((b) => ({
      tid: b.getAttribute("data-testid"),
      aria: b.getAttribute("aria-label"),
      text: cut((b as HTMLElement).innerText, 60),
    }));
  const labelled = Array.from(document.querySelectorAll("[aria-label]")).filter(vis);
  const arias = labelled.map((e) => cut(e.getAttribute("aria-label"), 80)).filter(Boolean).slice(0, 300);
  const widgets = labelled
    .filter((e) => e.getAttribute("aria-label") === a.widgetAria)
    .map((e) => cut((e as HTMLElement).innerText || e.textContent, 160))
    .slice(0, 5);
  let nameInput = false;
  try {
    // Поиск и строка сообщения с той же меткой — не поле имени (подсказка поиска
    // может смениться с «Поиск» на «Найти чат» — цикл5 фазы 3).
    const notName = new RegExp(a.notNameRe, "i");
    const nameLike = (el: Element): boolean => {
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (type === "search" || (el.getAttribute("role") || "").toLowerCase() === "searchbox") return false;
      const own = ["placeholder", "aria-label", "title"].map((k) => el.getAttribute(k) || "").join(" ");
      return !notName.test(own);
    };
    nameInput = Array.from(document.querySelectorAll(a.nameSel)).some((el) => vis(el) && nameLike(el));
  } catch {
    nameInput = false;
  }
  let joinButton = false;
  try {
    joinButton = Array.from(document.querySelectorAll(a.joinSel)).some(vis);
  } catch {
    joinButton = false;
  }
  if (!joinButton) joinButton = buttons.some((b) => b.text === a.joinText);
  let tiles = 0;
  try {
    tiles = document.querySelectorAll(a.tileSel).length;
  } catch {
    tiles = 0;
  }
  return { lines, testids, buttons, arias, widgets, nameInput, joinButton, tiles };
}

function safeFrameUrl(fr: Frame): string {
  try {
    return fr.url();
  } catch {
    return "";
  }
}

/** Номер встречи из адреса главной (`/j/<цифры>`, иначе первая длинная серия цифр пути) или null. */
export function meetingIdFromUrl(raw: string): string | null {
  let path = "";
  try {
    path = new URL(raw).pathname;
  } catch {
    return null;
  }
  const m = path.match(/\/j\/(\d{6,})/) || path.match(/(\d{8,})/);
  return m ? m[1] : null;
}

/**
 * Окно звонка 3.0 — ЧИСТАЯ функция. Сегодня — по куску адреса `/private-join/`.
 * Цикл5 фазы 3 (У3): если такого окна на странице нет вовсе, окном звонка
 * считается окно Телемоста с номером встречи в адресе — поломка 21.09 была именно
 * переездом лобби в новое вложенное окно, и следующая может переименовать его путь:
 * запасной путь вход пройдёт, а без этого бот не узнал бы звонок и ушёл. Пока окно
 * `/private-join/` есть, правило прежнее, байт в байт.
 */
export function isCallFrameUrl(url: string, mainUrl: string, markerSeen: boolean): boolean {
  if (url.includes(telemostLobbyFrameUrlMarker)) return true;
  if (markerSeen) return false;
  const id = meetingIdFromUrl(mainUrl);
  return !!id && isTelemostMeetingUrl(url) && url.includes(id);
}

export function frameKind(page: Page, fr: Frame): FrameKind {
  if (fr === page.mainFrame()) return "main";
  const url = safeFrameUrl(fr);
  if (url.includes(telemostLobbyFrameUrlMarker)) return "call";
  let markerSeen = false;
  let mainUrl = "";
  try {
    markerSeen = page.frames().some((f) => safeFrameUrl(f).includes(telemostLobbyFrameUrlMarker));
    mainUrl = page.url();
  } catch {
    return "other";
  }
  return isCallFrameUrl(url, mainUrl, markerSeen) ? "call" : "other";
}

/** Окна страницы: сначала окна звонка 3.0, потом главное, потом прочие. */
export function orderedFrames(page: Page): Frame[] {
  const frames = page.frames();
  const rank = (f: Frame) => {
    const k = frameKind(page, f);
    return k === "call" ? 0 : k === "main" ? 1 : 2;
  };
  return [...frames].sort((a, b) => rank(a) - rank(b));
}

const FRAME_EVAL_TIMEOUT_MS = 2500;

/** Признаки одного окна. Окно ушло / чужое / зависло → null. Никогда не бросает. */
export async function frameSignals(page: Page, fr: Frame): Promise<FrameSignals | null> {
  const args: SnapshotArgs = {
    nameSel: telemostNameInputSelectors[0],
    notNameRe: NOT_NAME_INPUT_RE.source,
    joinSel: '[data-testid="enter-conference-button"]',
    joinText: "Подключиться",
    widgetAria: telemostCallWidgetAria,
    tileSel: telemostParticipantNameSelectors[0],
  };
  try {
    const raw = await Promise.race([
      fr.evaluate(snapshotSignalsInPage, args),
      new Promise<null>((r) => setTimeout(() => r(null), FRAME_EVAL_TIMEOUT_MS)),
    ]);
    if (!raw) return null;
    return { kind: frameKind(page, fr), ...raw };
  } catch {
    return null;
  }
}

/** Признаки всех окон страницы (Playwright видит вложенные окна любой глубины). */
export async function collectFrameSignals(page: Page): Promise<FrameSignals[]> {
  const out: FrameSignals[] = [];
  if (!page || page.isClosed()) return out;
  for (const fr of orderedFrames(page)) {
    if (fr.isDetached()) continue;
    const s = await frameSignals(page, fr);
    if (s) out.push(s);
  }
  return out;
}

/** Снять признаки и классифицировать экран. Никогда не бросает. */
export async function readTelemostScreen(page: Page): Promise<ScreenVerdict> {
  try {
    return classifyTelemostScreen(await collectFrameSignals(page));
  } catch {
    return { state: "unknown", ui: "old", evidence: "collect_failed" };
  }
}

/**
 * Код причины сорванного входа (index.ts → статус для сторожа): по тексту нашей
 * ошибки, а «встречи нет» — по экрану, ТЕМ ЖЕ разбором, что вход и ожидание
 * впуска. Раньше смотрелся сырой текст всех окон (`detectMeetingGone`), и реплика
 * чата «встреча закончилась» или «Звонок завершён» прошлой встречи рядом с идущим
 * звонком превращала любой срыв в «встречи нет» — без повтора и без сообщения
 * людям (цикл5 фазы 3, В1/Н1). Никогда не бросает.
 */
export async function joinFailureReason(page: Page | null, message: string): Promise<string> {
  const reason = classifyJoinError(message);
  if (reason === "admission_rejected" || reason === "meeting_not_found" || !page || page.isClosed()) return reason;
  return (await readTelemostScreen(page)).state === "gone" ? "meeting_not_found" : reason;
}
