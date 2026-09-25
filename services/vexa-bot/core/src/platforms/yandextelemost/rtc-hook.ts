// Браузерные помощники записи звука (ISS-60, фаза 3а, 25.09.2026).
//
// Обе функции САМОДОСТАТОЧНЫ: уходят в страницу как текст (Playwright
// addInitScript / evaluate), поэтому не трогают ничего снаружи себя. Это же даёт
// проверить их без браузера — подставив поддельное окно (call-ui.test.ts).
//
// 1) installRtcAudioHook — ловит входящие аудио-дорожки у RTCPeerConnection
//    (Телемост 3.0 может играть звук через WebAudio, а не <audio>). Раньше список
//    `__vexaRemoteAudioTracks` только рос: после переподключения участника в нём
//    копились мёртвые дорожки. Теперь завершённые выкидываются — и при появлении
//    новой, и по событию `ended`.
// 2) installFrameWalker — ставит в окно обходчик всех окон того же
//    происхождения вглубь (раньше запись смотрела вложенные окна только на один
//    уровень).
//
// Фаза 3б (25.09.2026): перехват ещё и помнит соединения звонка
// (`__vexaPeerConnections`, счётчик живых — `__vexaOpenPeerConnections()`). По
// нему выход из звонка (leave.ts) подтверждается независимо от вида экрана: нажали
// «Завершить звонок» — Телемост закрывает свои соединения.

/**
 * Поставить перехват RTCPeerConnection в окне `target` (в браузере — само окно).
 * Идемпотентна. Список живых входящих аудио-дорожек — `__vexaRemoteAudioTracks`,
 * чистка — `__vexaPruneRemoteAudioTracks()`; сколько соединений звонка ещё не
 * закрыто — `__vexaOpenPeerConnections()`.
 */
export function installRtcAudioHook(target?: any): void {
  const w: any = target || (globalThis as any);
  if (!w || w.__vexaRtcHooked || !w.RTCPeerConnection) return;
  w.__vexaRtcHooked = true;
  w.__vexaRemoteAudioTracks = [];
  w.__vexaPeerConnections = [];
  const MAX_TRACKS = 64;
  const MAX_PCS = 32;
  const isOpen = (pc: any): boolean => !!pc && pc.signalingState !== "closed" && pc.connectionState !== "closed";
  w.__vexaOpenPeerConnections = (): number => {
    try {
      const list: any[] = Array.isArray(w.__vexaPeerConnections) ? w.__vexaPeerConnections : [];
      return list.filter(isOpen).length;
    } catch {
      return 0;
    }
  };
  const prune = (): void => {
    try {
      const list: any[] = Array.isArray(w.__vexaRemoteAudioTracks) ? w.__vexaRemoteAudioTracks : [];
      w.__vexaRemoteAudioTracks = list.filter((t: any) => t && t.readyState !== "ended");
    } catch {
      /* окно уходит — не наше дело */
    }
  };
  w.__vexaPruneRemoteAudioTracks = prune;
  const Orig = w.RTCPeerConnection;
  const Hooked: any = function (this: any, ...args: any[]) {
    const pc = new Orig(...args);
    try {
      const pcs: any[] = Array.isArray(w.__vexaPeerConnections) ? w.__vexaPeerConnections : [];
      pcs.push(pc);
      w.__vexaPeerConnections = pcs.length > MAX_PCS ? pcs.filter(isOpen).slice(-MAX_PCS) : pcs;
    } catch {
      /* */
    }
    pc.addEventListener("track", (e: any) => {
      try {
        const t = e && e.track;
        if (!t || t.kind !== "audio") return;
        prune();
        const list: any[] = w.__vexaRemoteAudioTracks;
        if (list.indexOf(t) === -1) list.push(t);
        if (list.length > MAX_TRACKS) list.splice(0, list.length - MAX_TRACKS);
        if (typeof t.addEventListener === "function") t.addEventListener("ended", prune);
      } catch {
        /* */
      }
    });
    return pc;
  };
  Hooked.prototype = Orig.prototype;
  Object.setPrototypeOf(Hooked, Orig);
  w.RTCPeerConnection = Hooked;
}

/**
 * Поставить в окно `target` обходчик окон: `__vexaSameOriginFrameWindows(root,
 * maxDepth)` → окна того же происхождения, начиная с `root`, вглубь до
 * `maxDepth` уровней вложенности. Чужие окна (другой origin) пропускаются молча,
 * без повторов. Ставится функцией, а не строкой: `eval` в странице может
 * запретить её политика безопасности.
 */
export function installFrameWalker(target?: any): void {
  const w: any = target || (globalThis as any);
  if (!w) return;
  w.__vexaSameOriginFrameWindows = function (root: any, maxDepth?: number): any[] {
    const limit = typeof maxDepth === "number" ? maxDepth : 6;
    const out: any[] = [];
    const walk = (win: any, depth: number): void => {
      if (!win || out.indexOf(win) !== -1) return;
      let doc: any = null;
      try {
        doc = win.document;
      } catch {
        return; // чужой origin
      }
      if (!doc) return;
      out.push(win);
      if (depth >= limit) return;
      let frames: any[] = [];
      try {
        frames = Array.from(doc.querySelectorAll("iframe"));
      } catch {
        frames = [];
      }
      for (const fr of frames) {
        let cw: any = null;
        try {
          cw = fr.contentWindow;
        } catch {
          cw = null;
        }
        if (cw) walk(cw, depth + 1);
      }
    };
    walk(root, 0);
    return out;
  };
}
