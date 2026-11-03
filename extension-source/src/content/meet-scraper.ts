/**
 * Meet page reader (content script).
 *
 * While MeetMind records, it reads three things from the Meet page:
 * 1. Who is in the call: names on the participant tiles and in the People list.
 * 2. Whether you are muted (Meet's microphone button), so your mic track stays silent then.
 * 3. Live captions, ONLY if you turned them on yourself (MeetMind never turns them on
 *    unless captions.autoEnable is true in config.js). They are an extra hint for names.
 *
 * Everything is sent to the service worker as small batches every few seconds.
 *
 * It also watches YOUR call (always, even when MeetMind is not recording): when you leave
 * (red button, "You left the meeting", another page), it tells the service worker, which
 * stops "Record here" and/or the MeetMind bot in the same meeting and writes the notes.
 * Selectors use roles, ARIA labels and data attributes, not class names, because
 * Meet's class names change often; all of them can be changed in config.js.
 *
 * This file is a classic script (content scripts cannot be ES modules),
 * so it has no imports and runs inside an IIFE.
 */

(() => {
  interface Caption {
    id: string;
    speaker: string;
    text: string;
    start: number;
    end: number;
  }

  interface StartOptions {
    startedAt: number;
    autoEnable: boolean;
    regionSelector: string;
    turnOnButtonLabel: string;
    tileSelector: string;
    nameSelector: string;
    peopleListSelector: string;
    micButtonSelector: string;
  }

  const FLAG = "__meetmindScraperLoaded";
  const scope = window as unknown as Record<string, boolean>;
  if (scope[FLAG]) return;
  scope[FLAG] = true;

  const FLUSH_MS = 3000;
  const SCAN_MS = 1000;
  const UI_NOISE = new Set(["arrow_downward", "jump to bottom", "more_vert", "close", "settings"]);
  /** Labels Meet adds after a name; the name itself may contain brackets, so only these are removed. */
  const NAME_SUFFIX = /\s*\((you|presenting|presentation|host|co-host|meeting host)\)\s*$/i;
  const ICON_WORD = /^[a-z0-9]+(_[a-z0-9]+)+$/;
  const NOT_A_NAME = /^(you|presentation|your presentation|meeting host|more options|pin|unpin|mute|.*'s presentation)$/i;

  let startedAt: number | null = null;
  let options: StartOptions | null = null;
  let observer: MutationObserver | null = null;
  let flushTimer: number | null = null;
  let scanTimer: number | null = null;
  const live = new Map<Element, Caption>();
  const dirty = new Map<string, Caption>();
  const roster = new Set<string>();
  let rosterSent = "";
  let selfName = "";
  let micMuted: boolean | null = null;

  chrome.runtime.onMessage.addListener((message: { type?: string } & Partial<StartOptions>, _sender, sendResponse) => {
    if (message.type === "CAPTIONS_START" && typeof message.startedAt === "number") {
      start({
        startedAt: message.startedAt,
        autoEnable: message.autoEnable ?? false,
        regionSelector: message.regionSelector ?? '[role="region"][aria-label*="caption" i]',
        turnOnButtonLabel: message.turnOnButtonLabel ?? "turn on captions",
        tileSelector: message.tileSelector ?? "[data-participant-id]",
        nameSelector: message.nameSelector ?? ".notranslate",
        peopleListSelector: message.peopleListSelector ?? '[role="list"] [role="listitem"][aria-label]',
        micButtonSelector: message.micButtonSelector ?? 'button[aria-label*="microphone" i]',
      });
      readPeople();
      readMic();
      sendResponse({ ok: true, roster: [...roster], selfName, micMuted: micMuted ?? false });
    } else if (message.type === "CAPTIONS_STOP") {
      const entries = stop();
      sendResponse({ ok: true, entries, roster: [...roster], selfName });
    }
    return false;
  });

  function start(next: StartOptions): void {
    stop();
    options = next;
    startedAt = next.startedAt;
    if (next.autoEnable) void enableCaptions(next.turnOnButtonLabel.toLowerCase());
    observer = new MutationObserver(() => scan());
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    // Polling backs up the observer when Meet replaces large parts of the DOM.
    scanTimer = window.setInterval(() => {
      scan();
      readPeople();
      readMic();
    }, SCAN_MS);
    flushTimer = window.setInterval(flush, FLUSH_MS);
  }

  function stop(): Caption[] {
    observer?.disconnect();
    observer = null;
    if (flushTimer !== null) clearInterval(flushTimer);
    if (scanTimer !== null) clearInterval(scanTimer);
    flushTimer = scanTimer = null;
    scan();
    readPeople();
    const remaining = [...dirty.values(), ...live.values()];
    dirty.clear();
    live.clear();
    startedAt = null;
    return dedupe(remaining);
  }

  /* ---------- people in the call ---------- */

  function cleanName(raw: string | null | undefined): string {
    const name = (raw ?? "").split("\n")[0]?.replace(NAME_SUFFIX, "").trim() ?? "";
    if (ICON_WORD.test(name)) return ""; // "frame_person", "more_vert": icon names, not people
    return name.length >= 2 && name.length <= 80 && !NOT_A_NAME.test(name) ? name : "";
  }

  /**
   * Meet draws its icons with a font: the element's text is the icon name ("devices",
   * "frame_person") and it also carries the "notranslate" class. Such elements are never names.
   */
  function isIcon(el: Element): boolean {
    if (el.closest('[aria-hidden="true"]')) return true;
    if (/icon|symbol/i.test(el.getAttribute("class") ?? "")) return true;
    try {
      return /material|symbols|icons/i.test(getComputedStyle(el).fontFamily);
    } catch {
      return false;
    }
  }

  function query(root: ParentNode, selector: string): Element[] {
    try {
      return [...root.querySelectorAll(selector)];
    } catch {
      return []; // invalid selector in config.js
    }
  }

  /** Names from the video tiles and, when it is open, the People list. */
  function readPeople(): void {
    if (!options) return;
    for (const tile of query(document, options.tileSelector)) {
      const self = tile.querySelector("[data-self-name]");
      if (self) {
        const own = cleanName(self.getAttribute("data-self-name"));
        if (own) selfName = own;
      }
      const label = query(tile, options.nameSelector)
        .filter((el) => !isIcon(el))
        .map((el) => cleanName((el as HTMLElement).innerText || el.textContent))
        .find(Boolean);
      if (label) roster.add(label);
    }
    for (const item of query(document, options.peopleListSelector)) {
      const name = cleanName(item.getAttribute("aria-label"));
      if (name) roster.add(name);
    }
  }

  /** Meet's mic button: data-is-muted="true", or a label that offers to turn the mic on. */
  function readMic(): void {
    if (!options) return;
    const button = query(document, options.micButtonSelector)[0];
    if (!button) return;
    const flag = button.getAttribute("data-is-muted");
    const label = (button.getAttribute("aria-label") ?? "").toLowerCase();
    const muted = flag !== null ? flag === "true" : label.startsWith("turn on");
    if (muted !== micMuted) {
      micMuted = muted;
      chrome.runtime.sendMessage({ type: "CAPTIONS_BATCH", entries: [], micMuted: muted }).catch(() => undefined);
    }
  }

  /* ---------- captions (only when they are on) ---------- */

  /** Clicks Meet's "Turn on captions" button. Used only when captions.autoEnable is true. */
  async function enableCaptions(label: string): Promise<void> {
    for (let attempt = 0; attempt < 10 && startedAt !== null; attempt++) {
      if (findCaptionRegion()) return;
      const button = [...document.querySelectorAll<HTMLButtonElement>("button[aria-label]")].find((b) =>
        (b.getAttribute("aria-label") ?? "").toLowerCase().includes(label),
      );
      if (button) {
        button.click();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }

  function findCaptionRegion(): HTMLElement | null {
    if (!options) return null;
    try {
      return document.querySelector<HTMLElement>(options.regionSelector);
    } catch {
      return null; // invalid selector in config.js
    }
  }

  /**
   * Finds the element whose children are the caption blocks (one block = name + words).
   * Checks the region and a few levels below it and picks the level where the most
   * children parse as "speaker + text"; on a tie the deeper level wins, so a lone
   * block is still found and several blocks are never merged into one.
   */
  function captionBlocks(region: HTMLElement): HTMLElement[] {
    let best: HTMLElement[] = [];
    let level: HTMLElement[] = [region];
    for (let depth = 0; depth < 4 && level.length; depth++) {
      for (const container of level) {
        const children = [...container.children].filter((el): el is HTMLElement => el instanceof HTMLElement);
        const blocks = children.filter((child) => parseBlock(child) !== null);
        if (blocks.length && blocks.length >= best.length) best = blocks;
      }
      level = level.flatMap((el) => [...el.children].filter((c): c is HTMLElement => c instanceof HTMLElement));
    }
    return best;
  }

  function parseBlock(block: HTMLElement): { speaker: string; text: string } | null {
    const lines = block.innerText
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !UI_NOISE.has(line.toLowerCase()));
    if (lines.length < 2) return null;
    const [speaker, ...rest] = lines;
    if (!speaker || speaker.length > 80) return null;
    return { speaker, text: rest.join(" ") };
  }

  function scan(): void {
    if (startedAt === null) return;
    const region = findCaptionRegion();
    const seen = new Set<Element>();
    const now = (Date.now() - startedAt) / 1000;

    if (region) {
      for (const block of captionBlocks(region)) {
        const parsed = parseBlock(block);
        if (!parsed) continue;
        seen.add(block);
        const existing = live.get(block);

        if (existing && existing.speaker === parsed.speaker) {
          const merged = mergeText(existing.text, parsed.text);
          if (merged !== existing.text) {
            existing.text = merged;
            existing.end = now;
            dirty.set(existing.id, existing);
          }
          continue;
        }
        if (existing) dirty.set(existing.id, existing); // block reused for a new speaker
        const caption: Caption = { id: crypto.randomUUID(), speaker: parsed.speaker, text: parsed.text, start: now, end: now };
        live.set(block, caption);
        dirty.set(caption.id, caption);
      }
    }

    // Blocks that left the screen are final.
    for (const [block, caption] of live) {
      if (!seen.has(block)) {
        dirty.set(caption.id, caption);
        live.delete(block);
      }
    }
  }

  /**
   * Meet edits captions in place and trims old lines from long blocks.
   * Keep the full text: replace when the new text contains the old one,
   * append only the new tail when they overlap.
   */
  function mergeText(previous: string, next: string): string {
    if (next === previous || previous.includes(next)) return previous;
    if (next.includes(previous) || next.startsWith(previous.slice(0, 20))) return next;
    const maxOverlap = Math.min(previous.length, next.length);
    for (let size = maxOverlap; size >= 10; size--) {
      if (previous.endsWith(next.slice(0, size))) return `${previous}${next.slice(size)}`;
    }
    return next.length < previous.length * 0.5 ? `${previous} ${next}` : next;
  }

  /* ---------- sending ---------- */

  function flush(): void {
    const names = [...roster].sort();
    const rosterKey = `${names.join("|")}#${selfName}`;
    if (dirty.size === 0 && rosterKey === rosterSent) return;
    const entries = dedupe([...dirty.values()]);
    dirty.clear();
    chrome.runtime
      .sendMessage({ type: "CAPTIONS_BATCH", entries, roster: names, selfName })
      .then(() => (rosterSent = rosterKey))
      .catch(() => {
        for (const entry of entries) dirty.set(entry.id, entry); // retry on next flush
      });
  }

  function dedupe(entries: Caption[]): Caption[] {
    return [...new Map(entries.map((e) => [e.id, { ...e }])).values()];
  }

  /* ---------- did you leave the call? ---------- */

  const WATCH_MS = 1000;
  const JOIN_CHECKS = 2; // the call toolbar must be seen twice in a row
  const LEFT_CHECKS = 2; // "you left" must be seen twice in a row (a redraw is not a leave)
  const GONE_CHECKS = 10; // toolbar missing this long with no "left" text also counts as left
  const CODE = /\/([a-z]{3}-[a-z]{4}-[a-z]{3})(?=$|[/?#])/i;
  const LEFT_TEXT =
    /you left the meeting|you['’]ve left the meeting|you have left the meeting|you['’]ve been removed|you have been removed|the call has ended|meeting has ended|ended the meeting for everyone|call ended/i;
  const WAITING_TEXT = /asking to be let in|asking to join|you['’]ll join the call when someone lets you in|waiting for the host/i;

  let joinedCode = "";
  let inCallSeen = 0;
  let leftSeen = 0;
  let goneSeen = 0;

  void chrome.runtime
    .sendMessage({ type: "WATCH_OPTIONS" })
    .then((ack: { watch?: { enabled: boolean; leaveButtonSelector: string } } | undefined) => {
      const watch = ack?.watch;
      if (!watch?.enabled) return;
      window.setInterval(() => watchCall(watch.leaveButtonSelector), WATCH_MS);
      // Tab closed or another site opened while in the call (the service worker also checks the tab).
      window.addEventListener("pagehide", () => joinedCode && report("MEET_LEFT", joinedCode));
    })
    .catch(() => undefined); // extension reloaded: this old copy of the script just stays idle

  function watchCall(leaveSelector: string): void {
    const code = CODE.exec(location.pathname)?.[1]?.toLowerCase() ?? "";
    // In the DOM, not "visible": Meet hides its toolbar when the mouse is still.
    const toolbar = query(document, leaveSelector).length > 0;
    if (!joinedCode) {
      const inCall = Boolean(code) && toolbar && !WAITING_TEXT.test(document.body?.innerText ?? "");
      inCallSeen = inCall ? inCallSeen + 1 : 0;
      if (inCallSeen >= JOIN_CHECKS) {
        joinedCode = code;
        leftSeen = goneSeen = 0;
        report("MEET_JOINED", code);
      }
      return;
    }
    if (code !== joinedCode) return left(); // moved to another page or meeting
    if (toolbar) {
      leftSeen = goneSeen = 0;
      return;
    }
    goneSeen += 1;
    // "Left" text counts only once the toolbar is gone, so a chat line saying "call ended" never stops anything.
    leftSeen = LEFT_TEXT.test(document.body?.innerText ?? "") ? leftSeen + 1 : 0;
    if (leftSeen >= LEFT_CHECKS || goneSeen >= GONE_CHECKS) left();
  }

  function left(): void {
    const code = joinedCode;
    joinedCode = "";
    inCallSeen = leftSeen = goneSeen = 0; // "Rejoin" is watched again like a new call
    report("MEET_LEFT", code);
  }

  function report(type: "MEET_JOINED" | "MEET_LEFT", code: string): void {
    chrome.runtime.sendMessage({ type, code }).catch(() => undefined);
  }
})();
