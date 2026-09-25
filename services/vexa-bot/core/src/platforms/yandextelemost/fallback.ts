// Запасной путь входа в Телемост (ISS-60, фаза 3б плана
// plans/2026-09-23-telemost-update-resilience.md, 25.09.2026).
//
// ЗАЧЕМ. Телемост меняет вход без предупреждения раз в 1–2 месяца (23.06, 30.06,
// 22.07, 21.09). Основной путь (join.ts) знает точные метки сегодняшнего экрана;
// стоит Телемосту их поменять — основной путь падает, и до правки кода бот не
// заходит ни на одну встречу. Запасной путь ищет то же самое «по смыслу» и
// включается ТОЛЬКО если основной не прошёл (или по флагу живой проверки).
//
// КАК. Во всех окнах страницы (главная, окно звонка `/private-join/`, прочие окна
// Телемоста; чужие окна и чужие страницы не трогаются) снимаются видимые кнопки и
// поля. Что делать дальше, решает ЧИСТАЯ функция chooseFallbackAction:
//   1) всплывающее окно — закрыть кнопкой из белого списка: «Понятно» и т.п. —
//      в окне поверх экрана или плашкой в окне звонка; «Закрыть», «×», «Позже» —
//      только внутри окна поверх экрана и только если это окно не само лобби;
//   2) поле имени — вписать имя бота (не в поиск, не в чат: исключения проверяются
//      на КАЖДОМ ярусе — поле поиска чатов 3.0 носит ту же метку, что поле имени);
//   3) «Подключиться» — нажать (после имени);
//   4) «Продолжить в браузере» — нажать.
// Каждая цель ищется по ярусам: метка (data-testid) → подпись (aria-label, title,
// placeholder, <label>) → видимый текст. Кнопка, совпавшая с ЧЁРНЫМ списком
// (selectors.ts: выйти, завершить, запись, демонстрация, включить микрофон или
// камеру, пригласить, уйти со страницы, отмена…), не нажимается НИКОГДА — даже с
// текстом из белого списка. Перед самым нажатием кнопка перечитывается и
// проверяется ещё раз.
// Нажатие проверяется: экран сменился (кнопка пропала или признаки окон другие) —
// шаг засчитан; нет — ровно один JS-клик по той же кнопке и снова проверка; нет —
// эта кнопка больше не трогается.
//
// Итог — в статусе входа (`fallback_used`: что сорвалось у основного пути, какие
// шаги и каким ярусом), сторож на хосте пишет владельцу тихую строку «Телемост
// поменял вход, бот обошёл сам».
//
// Приватность: тексты страницы (там бывают имена и чат) не пишутся ни в лог, ни в
// статус — только наши собственные слова: цель, ярус, окно.

import { Frame, Page } from "playwright";
import { log } from "../../utils";
import {
  FallbackButtonTarget,
  telemostFallbackButtons,
  telemostFallbackNameInput,
  telemostFallbackBlacklist,
  telemostFallbackDialogTidPattern,
  telemostFallbackDialogClassPattern,
} from "./selectors";
import {
  FrameKind,
  ScreenState,
  collectFrameSignals,
  frameKind,
  isTelemostMeetingUrl,
  orderedFrames,
  readTelemostScreen,
} from "./screen";
import { ensureDeviceOff } from "./devices";
import { FallbackForceMode } from "./join-status";
import { stopSignalReceived } from "./stop-signal";

const LOG_PREFIX = "[adapter-telemost]";

function logStep(step: string, ctx: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString();
  log(`${LOG_PREFIX} step=${step} ts=${ts} ${Object.entries(ctx).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}`);
}

// ---------------------------------------------------------------------------
// Типы
// ---------------------------------------------------------------------------

export type FbTier = "tid" | "label" | "text";
export type FbTarget = FallbackButtonTarget | "name";

/** Видимый элемент одного окна (кнопка или поле) — только признаки, без введённых значений. */
export type FbControl = {
  /** Метка элемента на время одного прохода (атрибут data-notary-fb). */
  id: string;
  frameIdx: number;
  frame: FrameKind;
  kind: "button" | "input";
  tag: string;
  role: string | null;
  tid: string | null;
  aria: string | null;
  title: string | null;
  placeholder: string | null;
  /** Текст <label> / aria-labelledby поля. */
  label: string | null;
  /** Видимый текст рядом с полем (родитель до двух уровней вверх). */
  near: string | null;
  /** Видимый текст кнопки. */
  text: string;
  type: string | null;
  /** Окно поверх экрана, внутри которого элемент (id на один проход), или null. */
  dialog: string | null;
  disabled: boolean;
  readOnly: boolean;
  /** Поле: в нём уже имя бота. */
  isBotName: boolean;
};

export type FbMatch = { target: FbTarget; tier: FbTier; match: string };
export type FbAction = { kind: "click" | "fill"; control: FbControl; m: FbMatch };

export type FbDecisionState = {
  /** Имя уже вписано запасным путём (и проверено). */
  nameFilled: boolean;
  /** Подписи элементов, нажатие которых ничего не изменило, — больше не трогаем. */
  spent: Set<string>;
  /** Режим живой проверки «Телемост переименовал все метки»: ярус меток выключен. */
  noTestid: boolean;
  /** Сколько раз уже закрывали всплывашки (не больше MAX_POPUP_CLICKS). */
  popupClicks: number;
  /** Поля имени так и не видно — можно нажимать «Подключиться» без него. */
  joinWithoutName: boolean;
};

export type FallbackStep = {
  target: FbTarget;
  tier: FbTier;
  frame: FrameKind;
  method: string;
  ok: boolean;
};

export type FallbackResult = {
  ok: boolean;
  screen: ScreenState;
  steps: FallbackStep[];
  /** Чего не хватило: поля имени, кнопки «Подключиться», или нажатие не вывело из лобби. */
  missing: "name" | "join" | "lobby" | null;
  aborted: "stop_signal" | "page_closed" | "foreign_page" | null;
  /** «Подключиться» нажата, а имя бота так и не вписано (поля имени не нашлось). */
  nameless?: boolean;
};

export type FallbackRunOptions = {
  botName: string;
  /** Почему не прошёл основной путь (код причины) или null — принудительно. */
  reason: string | null;
  forced: FallbackForceMode | null;
  /** Срок всего запасного пути (по умолчанию FALLBACK_BUDGET_MS). */
  budgetMs?: number;
  /** Сколько ждать, если делать нечего (по умолчанию FALLBACK_IDLE_MS). */
  idleMs?: number;
};

// ---------------------------------------------------------------------------
// Чистые решения (тест: fallback.test.ts)
// ---------------------------------------------------------------------------

export const FALLBACK_BUDGET_MS = 75_000;
export const FALLBACK_IDLE_MS = 20_000;
const MAX_ACTIONS = 12;
export const MAX_POPUP_CLICKS = 4;
/** После «Подключиться» экран «неясно» столько — отдаём дальше (решит ожидание впуска). */
const JOIN_SETTLE_MS = 8_000;
/** Поля имени не видно столько — «Подключиться» можно жать без него. */
const NAME_GRACE_MS = 3_000;
const CHANGE_WAIT_MS = 3_000;
const SCAN_TIMEOUT_MS = 2_500;
/** Элемент мог пропасть между проходом и нажатием — не ждать его 30 с по умолчанию. */
const EVAL_TIMEOUT_MS = 1_500;

/**
 * Подпись для сравнения: регистр, ё=е, пробелы; значки и знаки препинания по краям
 * («🎥 Подключиться →», «Понятно!») отбрасываются, если после них остаются буквы или
 * цифры (голый «×» остаётся «×»).
 */
export function normLabel(s: string | null | undefined): string {
  const base = String(s ?? "")
    // Невидимые форматирующие символы (мягкий перенос U+00AD, word joiner U+2060,
    // U+200B…U+200D, BOM…) — цикл5 фазы 3: иначе «за\u00adпись» проходила бы мимо
    // чёрного списка.
    .replace(/\p{Cf}/gu, "")
    .replace(/\u00a0/g, " ")
    .toLowerCase()
    .replace(/ё/g, "е")
    .replace(/\s+/g, " ")
    .trim();
  const core = base.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "").trim();
  return core || base;
}

const LABEL_INDEX = new Map<FallbackButtonTarget, Map<string, string>>();
function labelIndex(target: FallbackButtonTarget): Map<string, string> {
  let m = LABEL_INDEX.get(target);
  if (!m) {
    m = new Map(telemostFallbackButtons[target].labels.map((l) => [normLabel(l), l] as [string, string]));
    LABEL_INDEX.set(target, m);
  }
  return m;
}

/** Совпадение с ЧЁРНЫМ списком — имя признака или null. Проверяется у каждой кнопки. */
export function blacklistHit(c: Pick<FbControl, "tid" | "aria" | "title" | "text">): string | null {
  const tid = c.tid || "";
  if (tid) {
    for (const re of telemostFallbackBlacklist.tidPatterns) if (re.test(tid)) return `tid:${re.source}`;
  }
  for (const v of [c.aria, c.title, c.text]) {
    const n = normLabel(v);
    if (!n) continue;
    for (const re of telemostFallbackBlacklist.labelPatterns) if (re.test(n)) return `label:${re.source}`;
  }
  return null;
}

/** Кнопка — цель `target` белого списка? Ярус и наше слово совпадения, или null. */
export function matchButton(c: FbControl, target: FallbackButtonTarget, noTestid = false): FbMatch | null {
  if (c.kind !== "button") return null;
  if (blacklistHit(c)) return null;
  const L = telemostFallbackButtons[target];
  if (!noTestid && c.tid) {
    if (L.tids.includes(c.tid) || L.tidPatterns.some((re) => re.test(c.tid as string))) {
      return { target, tier: "tid", match: c.tid };
    }
  }
  const idx = labelIndex(target);
  for (const v of [c.aria, c.title]) {
    const hit = idx.get(normLabel(v));
    if (hit) return { target, tier: "label", match: hit };
  }
  const hit = idx.get(normLabel(c.text));
  if (hit) return { target, tier: "text", match: hit };
  return null;
}

/** Поле подходит по типу (текстовое <input>, не поиск) и не похоже на поиск, чат, ссылку… */
export function nameInputEligible(c: FbControl): boolean {
  if (c.kind !== "input" || c.tag !== "input") return false;
  if (c.disabled || c.readOnly) return false;
  if (!telemostFallbackNameInput.allowedTypes.includes((c.type || "").toLowerCase())) return false;
  if ((c.role || "").toLowerCase() === "searchbox") return false;
  const own = [c.aria, c.placeholder, c.title, c.label, c.tid].map(normLabel).filter(Boolean);
  return !telemostFallbackNameInput.excludePatterns.some((re) => own.some((s) => re.test(s)));
}

/**
 * Поле имени? Ярусы: метка → подпись (aria-label, placeholder, title, <label>) →
 * текст рядом (только если поле в окне единственное — `sole`).
 */
export function matchNameInput(c: FbControl, noTestid = false, sole = false): FbMatch | null {
  if (!nameInputEligible(c)) return null;
  const N = telemostFallbackNameInput;
  if (!noTestid && c.tid && N.tids.includes(c.tid)) return { target: "name", tier: "tid", match: c.tid };
  for (const v of [c.aria, c.placeholder, c.title, c.label]) {
    const n = normLabel(v);
    if (n && N.labelPatterns.some((re) => re.test(n))) return { target: "name", tier: "label", match: "подпись поля" };
  }
  if (sole) {
    const near = normLabel(c.near);
    if (near && N.labelPatterns.some((re) => re.test(near)) && !N.excludePatterns.some((re) => re.test(near))) {
      return { target: "name", tier: "text", match: "текст рядом с полем" };
    }
  }
  return null;
}

/** Подпись элемента: одна и та же кнопка на разных проходах (метки data-notary-fb меняются). */
export function controlSignature(c: FbControl): string {
  return [c.frame, c.kind, c.tid || "", normLabel(c.aria), normLabel(c.title), normLabel(c.text), normLabel(c.placeholder)].join("|");
}

const TIERS: FbTier[] = ["tid", "label", "text"];

function tiers(noTestid: boolean): FbTier[] {
  return noTestid ? TIERS.filter((t) => t !== "tid") : TIERS;
}

function dialogKey(c: FbControl): string | null {
  return c.dialog ? `${c.frameIdx}:${c.dialog}` : null;
}

/**
 * Окна поверх экрана, которые на самом деле лобби (в них поле ввода, «Подключиться»
 * или «Продолжить в браузере»): «Закрыть» / «Позже» там — отказ от входа. Считается
 * по ВСЕМ ярусам, независимо от режима: безопасность от режима не зависит.
 */
export function lobbyDialogs(controls: FbControl[]): Set<string> {
  const out = new Set<string>();
  for (const c of controls) {
    const k = dialogKey(c);
    if (!k) continue;
    if (nameInputEligible(c) || matchButton(c, "join", false) || matchButton(c, "continue", false)) {
      out.add(k);
    }
  }
  return out;
}

/** На экране видно лобби или промежуточный экран (поле имени, «Подключиться», «Продолжить в браузере»). */
export function lobbyControlsVisible(controls: FbControl[]): boolean {
  return controls.some(
    (c) => !!(matchNameInput(c, false, false) || matchButton(c, "join", false) || matchButton(c, "continue", false)),
  );
}

function soleInputs(controls: FbControl[]): Set<string> {
  const byFrame = new Map<number, FbControl[]>();
  for (const c of controls) {
    if (!nameInputEligible(c)) continue;
    const list = byFrame.get(c.frameIdx) || [];
    list.push(c);
    byFrame.set(c.frameIdx, list);
  }
  const out = new Set<string>();
  for (const list of byFrame.values()) if (list.length === 1) out.add(list[0].id);
  return out;
}

function pickButton(
  controls: FbControl[],
  target: FallbackButtonTarget,
  st: FbDecisionState,
  accept: (c: FbControl) => boolean = () => true,
): { control: FbControl; m: FbMatch } | null {
  for (const tier of tiers(st.noTestid)) {
    for (const c of controls) {
      if (!accept(c)) continue;
      const m = matchButton(c, target, st.noTestid);
      if (m && m.tier === tier) return { control: c, m };
    }
  }
  return null;
}

/**
 * Следующий шаг запасного пути — ЧИСТАЯ функция. `controls` — в порядке окон
 * (окно звонка, главное, прочие) и документа. null — делать нечего (подождать).
 */
export function chooseFallbackAction(controls: FbControl[], st: FbDecisionState): FbAction | null {
  const live = controls.filter((c) => !c.disabled && !st.spent.has(controlSignature(c)));
  const lobby = lobbyDialogs(controls);

  // 1) Всплывашки. «Понятно» — в окне поверх экрана (не лобби) или плашкой в окне
  //    звонка (на главной 3.0 живёт чат комнаты: кнопка там могла бы что-то
  //    отправить людям); «Закрыть» / «×» / «Позже» — только в окне поверх экрана.
  if (st.popupClicks < MAX_POPUP_CLICKS) {
    for (const tier of tiers(st.noTestid)) {
      for (const c of live) {
        const k = dialogKey(c);
        if (k && lobby.has(k)) continue;
        const ack = matchButton(c, "popup_ack", st.noTestid);
        if (ack && ack.tier === tier && (k || c.frame === "call")) return { kind: "click", control: c, m: ack };
        if (k) {
          const dis = matchButton(c, "popup_dismiss", st.noTestid);
          if (dis && dis.tier === tier) return { kind: "click", control: c, m: dis };
        }
      }
    }
  }

  // 2) Поле имени.
  const sole = soleInputs(controls);
  let name: { control: FbControl; m: FbMatch } | null = null;
  for (const tier of tiers(st.noTestid)) {
    for (const c of live) {
      const m = matchNameInput(c, st.noTestid, sole.has(c.id));
      if (m && m.tier === tier) {
        name = { control: c, m };
        break;
      }
    }
    if (name) break;
  }
  // Имя вписываем, пока его нет в поле (страница могла перерисовать поле и стереть).
  if (name && !name.control.isBotName) return { kind: "fill", control: name.control, m: name.m };

  // 3) «Подключиться» — после имени; поля имени нет — только когда его не видно давно.
  const nameReady = st.nameFilled || !!(name && name.control.isBotName);
  if (nameReady || (!name && st.joinWithoutName)) {
    const nameFrame = name ? name.control.frameIdx : null;
    const join =
      (nameFrame !== null ? pickButton(live, "join", st, (c) => c.frameIdx === nameFrame) : null) ||
      pickButton(live, "join", st);
    if (join) return { kind: "click", control: join.control, m: join.m };
  }

  // 4) «Продолжить в браузере».
  const cont = pickButton(live, "continue", st);
  if (cont) return { kind: "click", control: cont.control, m: cont.m };
  return null;
}

// ---------------------------------------------------------------------------
// Итог для статуса и владельца (наши слова, без текстов страницы)
// ---------------------------------------------------------------------------

const TIER_RU: Record<FbTier, string> = { tid: "по метке", label: "по подписи", text: "по тексту" };
const PRIMARY_RU: Record<string, string> = {
  name_input_not_found: "не нашёл поле имени",
  join_button_not_found: "не нашёл кнопку «Подключиться»",
  still_in_lobby: "после «Подключиться» остался в лобби",
};

/** Коротко для владельца: что сорвалось у основного пути и чем обошёл запасной. */
export function fallbackSummary(
  reason: string | null,
  forced: FallbackForceMode | null,
  steps: FallbackStep[],
  nameless = false,
): string {
  const head = forced
    ? `принудительная проверка запасного пути${forced === "no-testid" ? " без меток" : ""}`
    : `основной путь ${PRIMARY_RU[reason || ""] || "сорвался"}`;
  const parts: string[] = [];
  for (const s of steps) {
    if (!s.ok) continue;
    const how = TIER_RU[s.tier];
    let p: string;
    if (s.target === "popup_ack" || s.target === "popup_dismiss") p = `закрыл всплывающее окно (${how})`;
    else if (s.target === "name") p = `имя — ${how}`;
    else if (s.target === "join") p = `«Подключиться» — ${how}`;
    else p = `«Продолжить в браузере» — ${how}`;
    if (parts[parts.length - 1] !== p) parts.push(p);
  }
  // Цикл5 фазы 3 (Н7/У7): имя бота — единственная подпись «идёт протокол»;
  // вошёл без него — владелец должен это увидеть первым делом.
  const warn = nameless ? "⚠️ вошёл БЕЗ ИМЕНИ — люди видят гостя без подписи «протокол встречи»; " : "";
  return `${warn}${head}; запасной: ${parts.join(", ") || "ничего не нажимал"}`.slice(0, 280);
}

/** Шаги коротко для машины: `popup_dismiss:label,name:tid,join:text`. */
export function fallbackStepsCode(steps: FallbackStep[]): string {
  return steps
    .filter((s) => s.ok)
    .map((s) => `${s.target}:${s.tier}`)
    .join(",")
    .slice(0, 200);
}

// ---------------------------------------------------------------------------
// Сбор элементов в браузере
// ---------------------------------------------------------------------------

type ScanArgs = { run: string; botName: string; dialogTid: string; dialogClass: string; maxButtons: number; maxInputs: number };
type RawControl = Omit<FbControl, "frameIdx" | "frame">;

/** Выполняется ВНУТРИ окна страницы — только собственные переменные. Значения полей наружу не отдаёт. */
function scanControlsInPage(a: ScanArgs): RawControl[] {
  const vis = (el: Element): boolean => {
    const h = el as HTMLElement;
    if (!(h.offsetWidth || h.offsetHeight || h.getClientRects().length)) return false;
    const cs = getComputedStyle(h);
    return cs.visibility !== "hidden" && cs.display !== "none";
  };
  const cut = (s: string | null | undefined, n: number): string => (s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const attr = (el: Element, name: string): string | null => {
    const v = el.getAttribute(name);
    return v === null ? null : cut(v, 120) || null;
  };
  for (const old of Array.from(document.querySelectorAll("[data-notary-fb]"))) old.removeAttribute("data-notary-fb");
  const dTid = new RegExp(a.dialogTid, "i");
  const dCls = new RegExp(a.dialogClass, "i");
  const overlayish = (n: Element): boolean => {
    const cs = getComputedStyle(n as HTMLElement);
    return cs.position === "fixed" || cs.position === "absolute";
  };
  const isDialog = (n: Element): boolean => {
    const role = n.getAttribute("role");
    // Меню (role=menu) — тоже всплывающее окно: после «Завершить звонок» Телемост
    // может показать не диалог, а меню «Выйти из звонка / Завершить для всех».
    if (role === "dialog" || role === "alertdialog" || role === "menu") return true;
    if (n.getAttribute("aria-modal") === "true") return true;
    if (n.tagName === "DIALOG" && n.hasAttribute("open")) return true;
    const tid = n.getAttribute("data-testid");
    if (tid && dTid.test(tid) && overlayish(n)) return true;
    const cls = n.getAttribute("class") || "";
    return !!cls && dCls.test(cls) && overlayish(n);
  };
  const dialogs = new Map<Element, string>();
  const dialogOf = (el: Element): string | null => {
    let n: Element | null = el.parentElement;
    for (let depth = 0; n && depth < 40; depth++, n = n.parentElement) {
      if (isDialog(n)) {
        let id = dialogs.get(n);
        if (!id) {
          id = `d${dialogs.size}`;
          dialogs.set(n, id);
        }
        return id;
      }
    }
    return null;
  };
  let i = 0;
  const mark = (el: Element): string => {
    const id = `${a.run}-${i++}`;
    el.setAttribute("data-notary-fb", id);
    return id;
  };
  const out: RawControl[] = [];
  const BTN = "button, [role='button'], [role='menuitem'], input[type='button'], input[type='submit'], a[role='button']";
  const buttons = Array.from(document.querySelectorAll(BTN)).filter(vis).slice(0, a.maxButtons);
  for (const el of buttons) {
    const isInput = el.tagName === "INPUT";
    out.push({
      id: mark(el),
      kind: "button",
      tag: el.tagName.toLowerCase(),
      role: attr(el, "role"),
      tid: attr(el, "data-testid"),
      aria: attr(el, "aria-label"),
      title: attr(el, "title"),
      placeholder: null,
      label: null,
      near: null,
      text: isInput ? cut((el as HTMLInputElement).value, 80) : cut((el as HTMLElement).innerText, 80),
      type: attr(el, "type"),
      dialog: dialogOf(el),
      disabled: (el as HTMLButtonElement).disabled === true || el.getAttribute("aria-disabled") === "true",
      readOnly: false,
      isBotName: false,
    });
  }
  const labelOf = (el: HTMLInputElement): string | null => {
    let t = "";
    try {
      if (el.id) {
        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`) as HTMLElement | null;
        if (l) t = l.innerText;
      }
    } catch {
      t = "";
    }
    if (!t) {
      const l = el.closest("label") as HTMLElement | null;
      if (l) t = l.innerText;
    }
    if (!t) {
      const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
      t = ids.map((x) => (document.getElementById(x) as HTMLElement | null)?.innerText || "").join(" ");
    }
    return cut(t, 80) || null;
  };
  const nearOf = (el: Element): string | null => {
    let n: Element | null = el.parentElement;
    for (let k = 0; k < 2 && n; k++, n = n.parentElement) {
      const t = cut((n as HTMLElement).innerText, 160);
      if (t) return t;
    }
    return null;
  };
  const inputs = Array.from(document.querySelectorAll("input"))
    .filter((el) => !el.matches(BTN))
    .filter(vis)
    .slice(0, a.maxInputs);
  for (const el of inputs) {
    out.push({
      id: mark(el),
      kind: "input",
      tag: "input",
      role: attr(el, "role"),
      tid: attr(el, "data-testid"),
      aria: attr(el, "aria-label"),
      title: attr(el, "title"),
      placeholder: attr(el, "placeholder"),
      label: labelOf(el),
      near: nearOf(el),
      text: "",
      type: attr(el, "type"),
      dialog: dialogOf(el),
      disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true",
      readOnly: el.readOnly === true || el.getAttribute("aria-readonly") === "true",
      isBotName: (el.value || "").trim() === a.botName,
    });
  }
  return out;
}

/** Перечитать один помеченный элемент прямо перед нажатием (кнопка могла смениться). */
function describeMarkedInPage(el: Element): { tid: string | null; aria: string | null; title: string | null; text: string; disabled: boolean; visible: boolean } {
  const h = el as HTMLElement;
  const cs = getComputedStyle(h);
  const visible = !!(h.offsetWidth || h.offsetHeight || h.getClientRects().length) && cs.visibility !== "hidden" && cs.display !== "none";
  const cut = (s: string | null | undefined, n: number): string => (s || "").replace(/\s+/g, " ").trim().slice(0, n);
  return {
    tid: el.getAttribute("data-testid"),
    aria: el.getAttribute("aria-label"),
    title: el.getAttribute("title"),
    text: el.tagName === "INPUT" ? cut((el as HTMLInputElement).value, 80) : cut(h.innerText, 80),
    disabled: (el as HTMLButtonElement).disabled === true || el.getAttribute("aria-disabled") === "true",
    visible,
  };
}

export type FbScan = { controls: FbControl[]; frames: Frame[] };

function safeUrl(fr: Frame): string {
  try {
    return fr.url();
  } catch {
    return "";
  }
}

/** Видимые кнопки и поля во всех окнах Телемоста (окно звонка первым). Никогда не бросает. */
export async function scanControls(page: Page, run: string, botName: string): Promise<FbScan> {
  const frames: Frame[] = [];
  const controls: FbControl[] = [];
  if (!page || page.isClosed()) return { controls, frames };
  const pageOk = isTelemostMeetingUrl(page.url());
  for (const fr of orderedFrames(page)) {
    if (fr.isDetached()) continue;
    const kind = frameKind(page, fr);
    // Чужие окна (реклама, метрика, вход в аккаунт) и чужая страница — не трогаем.
    if (kind === "main" ? !pageOk : !isTelemostMeetingUrl(safeUrl(fr))) continue;
    const idx = frames.length;
    frames.push(fr);
    const args: ScanArgs = {
      run: `${run}f${idx}`,
      botName,
      dialogTid: telemostFallbackDialogTidPattern.source,
      dialogClass: telemostFallbackDialogClassPattern.source,
      maxButtons: 300,
      maxInputs: 60,
    };
    const raw = await Promise.race([
      fr.evaluate(scanControlsInPage, args).catch(() => null),
      new Promise<null>((r) => setTimeout(() => r(null), SCAN_TIMEOUT_MS)),
    ]);
    if (!raw) continue;
    for (const r of raw) controls.push({ ...r, frameIdx: idx, frame: kind });
  }
  return { controls, frames };
}

// ---------------------------------------------------------------------------
// Нажатие и ввод с проверкой
// ---------------------------------------------------------------------------

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Отпечаток экрана без текста и цифр (часы и счётчики не должны «менять экран»). */
async function screenFingerprint(page: Page): Promise<string> {
  try {
    const frames = await collectFrameSignals(page);
    return JSON.stringify(
      frames.map((f) => [
        f.kind,
        [...f.testids].sort(),
        f.buttons.map((b) => `${b.tid || ""}|${b.aria || ""}|${(b.text || "").replace(/\d+/g, "#")}`).sort(),
        f.nameInput,
        f.joinButton,
        f.tiles > 0,
      ]),
    );
  } catch {
    return "";
  }
}

function markedSelector(id: string): string {
  return `[data-notary-fb="${id.replace(/["\\]/g, "")}"]`;
}

/** JS-нажатие с полной последовательностью событий (React-обработчики Телемоста). */
async function jsClick(fr: Frame, sel: string): Promise<boolean> {
  return fr
    .evaluate((s: string) => {
      const el = document.querySelector(s) as HTMLElement | null;
      if (!el) return false;
      const o = { bubbles: true, cancelable: true, composed: true, view: window };
      try {
        el.dispatchEvent(new PointerEvent("pointerdown", o));
      } catch {
        /* нет PointerEvent */
      }
      el.dispatchEvent(new MouseEvent("mousedown", o));
      try {
        el.dispatchEvent(new PointerEvent("pointerup", o));
      } catch {
        /* нет PointerEvent */
      }
      el.dispatchEvent(new MouseEvent("mouseup", o));
      el.click();
      return true;
    }, sel)
    .catch(() => false);
}

async function waitChanged(page: Page, fr: Frame, sel: string, before: string, ms: number): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < ms) {
    await sleep(300);
    if (page.isClosed() || fr.isDetached()) return true;
    const stillThere = await fr.locator(sel).first().isVisible().catch(() => false);
    if (!stillThere) return true;
    if ((await screenFingerprint(page)) !== before) return true;
  }
  return false;
}

/** Кнопка всё ещё та же цель белого списка и не из чёрного списка? */
async function stillSafe(fr: Frame, sel: string, c: FbControl, target: FallbackButtonTarget, noTestid: boolean): Promise<boolean> {
  const now = await fr
    .locator(sel)
    .first()
    .evaluate(describeMarkedInPage, undefined, { timeout: EVAL_TIMEOUT_MS })
    .catch(() => null);
  if (!now || !now.visible || now.disabled) return false;
  const fresh: FbControl = { ...c, tid: now.tid, aria: now.aria, title: now.title, text: now.text };
  return !blacklistHit(fresh) && !!matchButton(fresh, target, noTestid);
}

async function clickVerified(
  page: Page,
  fr: Frame,
  c: FbControl,
  target: FallbackButtonTarget,
  noTestid: boolean,
): Promise<{ changed: boolean; method: string }> {
  const sel = markedSelector(c.id);
  if (!(await stillSafe(fr, sel, c, target, noTestid))) return { changed: false, method: "refused" };
  const before = await screenFingerprint(page);
  const clicked = await fr
    .locator(sel)
    .first()
    .click({ timeout: 2000 })
    .then(() => true)
    .catch(() => false);
  let method = clicked ? "click" : "js_click";
  if (!clicked) {
    // Обычный клик не прошёл (кнопку накрыло) — JS-клик, после повторной проверки.
    if (!(await stillSafe(fr, sel, c, target, noTestid))) return { changed: false, method: "refused" };
    await jsClick(fr, sel);
  }
  let changed = await waitChanged(page, fr, sel, before, CHANGE_WAIT_MS);
  if (!changed && clicked && (await stillSafe(fr, sel, c, target, noTestid))) {
    // Клик «прошёл», а экран тот же (React-обработчик Телемоста его не поймал):
    // ровно один JS-клик по той же кнопке.
    method = "click+js_click";
    await jsClick(fr, sel);
    changed = await waitChanged(page, fr, sel, before, CHANGE_WAIT_MS);
  }
  return { changed, method };
}

async function fillVerified(fr: Frame, c: FbControl, botName: string): Promise<{ ok: boolean; method: string }> {
  const loc = fr.locator(markedSelector(c.id)).first();
  // Перед вводом — ещё раз: это всё то же текстовое поле, не поиск и не чат.
  const fresh = await loc
    .evaluate((el: Element) => ({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute("type"),
      role: el.getAttribute("role"),
      tid: el.getAttribute("data-testid"),
      aria: el.getAttribute("aria-label"),
      title: el.getAttribute("title"),
      placeholder: el.getAttribute("placeholder"),
      disabled: (el as HTMLInputElement).disabled === true,
      readOnly: (el as HTMLInputElement).readOnly === true,
    }), undefined, { timeout: EVAL_TIMEOUT_MS })
    .catch(() => null);
  if (!fresh || !nameInputEligible({ ...c, ...fresh })) return { ok: false, method: "refused" };
  const has = () =>
    loc
      .evaluate((el: Element, name: string) => ((el as HTMLInputElement).value || "").trim() === name, botName, { timeout: EVAL_TIMEOUT_MS })
      .catch(() => false);
  let method = "fill";
  await loc.fill(botName, { timeout: 3000 }).catch(() => undefined);
  if (!(await has())) {
    // fill не прошёл — значение через родной сеттер + события ввода (React видит).
    method = "js_value";
    await loc
      .evaluate((el: Element, name: string) => {
        const inp = el as HTMLInputElement;
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
        if (setter) setter.call(inp, name);
        else inp.value = name;
        inp.dispatchEvent(new Event("input", { bubbles: true }));
        inp.dispatchEvent(new Event("change", { bubbles: true }));
      }, botName, { timeout: EVAL_TIMEOUT_MS })
      .catch(() => undefined);
  }
  return { ok: await has(), method };
}

// ---------------------------------------------------------------------------
// Запасной путь целиком
// ---------------------------------------------------------------------------

/**
 * Пройти вход запасным путём. Никогда не бросает. ok — бот в звонке, в зале
 * ожидания или «Подключиться» нажата и экран сменился (дальше решает ожидание
 * впуска, как после основного пути).
 */
export async function runFallbackJoin(page: Page, o: FallbackRunOptions): Promise<FallbackResult> {
  const start = Date.now();
  const budget = o.budgetMs ?? FALLBACK_BUDGET_MS;
  const idleLimit = o.idleMs ?? FALLBACK_IDLE_MS;
  const noTestid = o.forced === "no-testid";
  const steps: FallbackStep[] = [];
  const clicksBySig = new Map<string, number>();
  const st: FbDecisionState = { nameFilled: false, spent: new Set(), noTestid, popupClicks: 0, joinWithoutName: false };
  let lastAction = Date.now();
  let joinClickedAt: number | null = null;
  let joinTried = false;
  let sawName = false;
  let nameless = false;
  let scanNo = 0;
  logStep("fallback_start", { reason: o.reason, forced: o.forced });

  const finish = (ok: boolean, screen: ScreenState, extra: Partial<FallbackResult> = {}): FallbackResult => {
    const res: FallbackResult = { ok, screen, steps, missing: null, aborted: null, nameless, ...extra };
    logStep("fallback_done", {
      ok,
      screen,
      steps: fallbackStepsCode(steps),
      nameless,
      missing: res.missing,
      aborted: res.aborted,
      elapsed_ms: Date.now() - start,
    });
    return res;
  };

  while (Date.now() - start < budget) {
    if (stopSignalReceived()) return finish(false, "unknown", { aborted: "stop_signal" });
    if (!page || page.isClosed()) return finish(false, "unknown", { aborted: "page_closed" });
    // Чужая страница (увело со встречи): по тексту здесь не нажимаем ничего.
    if (!isTelemostMeetingUrl(page.url())) return finish(false, "unknown", { aborted: "foreign_page" });

    const scan = await scanControls(page, `fb${scanNo++}`, o.botName);
    const v = await readTelemostScreen(page);
    if (v.state === "gone" || v.state === "rejected") return finish(false, v.state);
    // «В звонке / зал ожидания» до нажатия «Подключиться» засчитываем, только если
    // лобби не видно: в 3.0 на главной «Завершить звонок» виден уже до входа.
    if ((v.state === "in_call" || v.state === "waiting_room") && (joinClickedAt !== null || !lobbyControlsVisible(scan.controls))) {
      return finish(true, v.state);
    }
    if (joinClickedAt !== null && v.state === "unknown" && Date.now() - joinClickedAt >= JOIN_SETTLE_MS) {
      return finish(true, "unknown");
    }
    if (steps.length >= MAX_ACTIONS) break;

    if (scan.controls.some((c) => matchNameInput(c, false, false))) sawName = true;
    st.joinWithoutName = !sawName && Date.now() - start >= NAME_GRACE_MS;
    const action = chooseFallbackAction(scan.controls, st);
    if (!action) {
      if (Date.now() - lastAction >= idleLimit) break;
      await sleep(1000);
      continue;
    }
    lastAction = Date.now();
    const c = action.control;
    const fr = scan.frames[c.frameIdx];
    const sig = controlSignature(c);

    if (action.kind === "fill") {
      const r = await fillVerified(fr, c, o.botName);
      steps.push({ target: "name", tier: action.m.tier, frame: c.frame, method: r.method, ok: r.ok });
      logStep("fallback_step", { target: "name", tier: action.m.tier, frame: c.frame, method: r.method, ok: r.ok });
      if (!r.ok) {
        st.spent.add(sig);
        continue;
      }
      st.nameFilled = true;
      // Бот — наблюдатель: микрофон и камеру в этом окне — выключить с проверкой.
      for (const device of ["mic", "camera"] as const) {
        await ensureDeviceOff(page, device, { onlyFrame: fr, scope: "lobby_fallback" }).catch(() => null);
      }
      continue;
    }

    const target = action.m.target as FallbackButtonTarget;
    // «Подключиться» без имени (поля имени так и не нашлось): бот — наблюдатель,
    // микрофон и камеру в окне кнопки выключить ДО входа — в 3.0 гость входит с
    // включёнными (цикл5 фазы 3, Н7). С именем это уже сделано после ввода имени.
    const namelessJoin = target === "join" && !st.nameFilled &&
      !scan.controls.some((x) => x.kind === "input" && x.isBotName);
    if (namelessJoin) {
      for (const device of ["mic", "camera"] as const) {
        await ensureDeviceOff(page, device, { onlyFrame: fr, scope: "lobby_fallback_nameless" }).catch(() => null);
      }
    }
    const r = await clickVerified(page, fr, c, target, noTestid);
    steps.push({ target, tier: action.m.tier, frame: c.frame, method: r.method, ok: r.changed });
    logStep("fallback_step", { target, tier: action.m.tier, match: action.m.match, frame: c.frame, method: r.method, ok: r.changed });
    if (target === "popup_ack" || target === "popup_dismiss") st.popupClicks++;
    if (target === "join" && r.method !== "refused") joinTried = true;
    const n = (clicksBySig.get(sig) || 0) + 1;
    clicksBySig.set(sig, n);
    if (!r.changed || n >= 2) st.spent.add(sig);
    if (r.changed && target === "join") {
      joinClickedAt = Date.now();
      if (namelessJoin) nameless = true;
    }
  }

  const v = await readTelemostScreen(page);
  if (v.state === "gone" || v.state === "rejected") return finish(false, v.state);
  if (joinClickedAt !== null && (v.state === "in_call" || v.state === "waiting_room" || v.state === "unknown")) {
    return finish(true, v.state);
  }
  // Чего не хватило: нажимали «Подключиться» — не вывело из лобби; не нажимали —
  // сначала поле имени (не нашли или не вписалось), потом сама кнопка.
  const missing: FallbackResult["missing"] = joinTried ? "lobby" : !st.nameFilled ? "name" : "join";
  return finish(false, v.state, { missing });
}
