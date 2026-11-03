/*
 * MeetMind bot: runs inside the bot's own Google Meet page, in a hidden "isolated world".
 *
 * Meet's own scripts cannot see this code or anything it creates (no changed browser
 * functions, no extra globals), so Google's bot check sees an ordinary Chrome page.
 * The world shares only the page's elements (DOM) with Meet.
 *
 * 1. Audio: once the bot is admitted, it records what the Meet tab plays (Chrome's
 *    "share this tab" audio, accepted automatically). Meet now plays voices through
 *    Web Audio, not <audio> elements, so tab audio is the only reliable source.
 *    Backup: the WebRTC tracks of any <audio>/<video> elements on the page.
 *    No microphone, no echo: just the call itself.
 * 2. Captions: shown only in the BOT's window (nobody else sees them). They tell us
 *    exactly who said each sentence.
 * 3. People and screen state: names on the tiles, and whether the bot is waiting,
 *    in the call, or the call has ended.
 *
 * Python talks to this file through window.__meetmind in that world (see browser.py).
 */
(() => {
  if (window.top !== window || window.__meetmind) return;

  const opts = {
    tileSelector: "[data-participant-id]",
    nameSelector: ".notranslate",
    peopleListSelector: '[role="list"] [role="listitem"][aria-label]',
    captionRegionSelector: '[role="region"][aria-label*="caption" i]',
    leaveButtonSelector: 'button[aria-label*="leave call" i]',
    speakingSelector: '[data-audio-level]:not([data-audio-level="0"]), [class*="speaking" i], [aria-label*="is speaking" i]',
  };
  const OVERLAP_MS = 2000; // pieces overlap so no word is cut at a boundary
  const UI_NOISE = new Set(["arrow_downward", "jump to bottom", "more_vert", "close", "settings"]);
  const NAME_SUFFIX = /\s*\((you|presenting|presentation|host|co-host|meeting host)\)\s*$/i;
  const ICON_WORD = /^[a-z0-9]+(_[a-z0-9]+)+$/;
  const NOT_A_NAME = /^(you|presentation|your presentation|meeting host|more options|pin|unpin|mute|.*'s presentation)$/i;

  const audio = { ctx: null, dest: null, analyser: null, tracks: new Map(), recorder: null, parts: [], peak: 0 };
  const captions = { live: new Map(), dirty: new Map() };
  const roster = new Set();
  let startedAt = null; // performance.now() when recording started
  let levels = [];

  /* ---------- audio ---------- */

  function ensureAudio() {
    if (audio.ctx) return;
    audio.ctx = new AudioContext();
    audio.dest = audio.ctx.createMediaStreamDestination();
    audio.analyser = audio.ctx.createAnalyser();
    audio.analyser.fftSize = 512;
    setInterval(sampleLevel, 150);
  }

  function addTrack(track) {
    if (!track || track.kind !== "audio" || audio.tracks.has(track.id)) return;
    ensureAudio();
    const stream = new MediaStream([track]);
    // Chrome only lets remote WebRTC audio flow into Web Audio while a media element plays it.
    const keep = new Audio();
    keep.muted = true;
    keep.srcObject = stream;
    keep.play().catch(() => undefined);
    const source = audio.ctx.createMediaStreamSource(stream);
    source.connect(audio.dest);
    source.connect(audio.analyser);
    audio.tracks.set(track.id, { source, keep });
    track.addEventListener("ended", () => {
      source.disconnect();
      keep.srcObject = null;
      audio.tracks.delete(track.id);
    });
  }

  /**
   * Records the audio the Meet tab plays ("share this tab", accepted automatically by Chrome).
   * Called once the bot is in the call. Returns { ok, error }.
   * silent = true: the sound is recorded but not played on the computer's speakers (no echo).
   */
  async function captureTab(silent = true) {
    if (audio.tab) return { ok: true };
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: 1, width: 320, height: 180 },
        // suppressLocalAudioPlayback: the bot records the call sound but does NOT play it on the
        // computer's speakers, so a person in the same meeting on the same PC hears no echo.
        audio: { suppressLocalAudioPlayback: Boolean(silent), echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        preferCurrentTab: true,
        selfBrowserSurface: "include",
        surfaceSwitching: "exclude",
        systemAudio: "exclude",
      });
      const track = stream.getAudioTracks()[0];
      if (!track) {
        stream.getTracks().forEach((t) => t.stop());
        return { ok: false, error: "Chrome shared the tab without its sound" };
      }
      for (const video of stream.getVideoTracks()) video.enabled = false; // only the sound is needed
      audio.tab = stream;
      track.addEventListener("ended", () => {
        audio.tab = null; // sharing was stopped: fall back to the page's media elements
      });
      addTrack(track);
      return { ok: true };
    } catch (error) {
      return { ok: false, error: String(error && error.message ? error.message : error) };
    }
  }

  /** Watchdog repair: stops the tab-sound capture so it can be started again. */
  function stopTab() {
    if (!audio.tab) return;
    for (const track of audio.tab.getTracks()) {
      const entry = audio.tracks.get(track.id);
      if (entry) {
        entry.source.disconnect();
        entry.keep.srcObject = null;
        audio.tracks.delete(track.id);
      }
      track.stop();
    }
    audio.tab = null;
  }

  /** Backup: the call audio of Meet's own <audio>/<video> elements (older Meet versions). */
  function scanMedia() {
    if (audio.tab) return; // tab audio already contains everything; avoid recording voices twice
    for (const el of document.querySelectorAll("audio, video")) {
      const stream = el.srcObject;
      if (!stream || typeof stream.getAudioTracks !== "function") continue;
      for (const track of stream.getAudioTracks()) {
        if (track.readyState === "live") addTrack(track);
      }
    }
  }
  setInterval(scanMedia, 500);

  function sampleLevel() {
    if (!audio.analyser) return;
    const samples = new Float32Array(audio.analyser.fftSize);
    audio.analyser.getFloatTimeDomainData(samples);
    let sum = 0;
    for (const s of samples) sum += s * s;
    const value = Math.min(1, Math.sqrt(Math.sqrt(sum / samples.length)) * 1.8);
    audio.peak = Math.max(audio.peak, value);
    levels.push(Math.round(value * 100) / 100);
    if (levels.length > 40) levels = levels.slice(-40);
  }

  /** Starts a new audio piece and returns the previous one (base64 webm) once it is complete. */
  async function rotate() {
    ensureAudio();
    if (audio.ctx.state === "suspended") await audio.ctx.resume().catch(() => undefined);
    if (startedAt === null) startedAt = performance.now();
    const previous = audio.recorder;
    const previousParts = audio.parts;
    const previousPeak = audio.peak;
    const offset = (performance.now() - startedAt) / 1000;

    const recorder = new MediaRecorder(audio.dest.stream, { mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: 32000 });
    const parts = [];
    recorder.ondataavailable = (e) => e.data.size && parts.push(e.data);
    recorder.start();
    audio.recorder = recorder;
    audio.parts = parts;
    audio.peak = 0;

    if (previous) await new Promise((resolve) => setTimeout(resolve, OVERLAP_MS)); // both record for a moment
    const finished = previous ? await finish(previous, previousParts, Math.max(previousPeak, audio.peak)) : null;
    return { offset, finished };
  }

  /** Stops the current piece (end of meeting). */
  async function stopAll() {
    const recorder = audio.recorder;
    audio.recorder = null;
    return recorder ? finish(recorder, audio.parts, audio.peak) : null;
  }

  function finish(recorder, parts, peak) {
    return new Promise((resolve) => {
      recorder.onstop = async () => {
        const blob = new Blob(parts, { type: "audio/webm" });
        const buffer = new Uint8Array(await blob.arrayBuffer());
        let binary = "";
        for (let i = 0; i < buffer.length; i += 0x8000) binary += String.fromCharCode(...buffer.subarray(i, i + 0x8000));
        resolve({ b64: btoa(binary), bytes: buffer.length, peak });
      };
      if (recorder.state !== "inactive") recorder.stop();
      else recorder.onstop();
    });
  }

  /* ---------- people ---------- */

  function cleanName(raw) {
    const name = String(raw ?? "").split("\n")[0].replace(NAME_SUFFIX, "").trim();
    if (ICON_WORD.test(name)) return "";
    return name.length >= 2 && name.length <= 80 && !NOT_A_NAME.test(name) ? name : "";
  }

  function isIcon(el) {
    if (el.closest('[aria-hidden="true"]')) return true;
    if (/icon|symbol/i.test(el.getAttribute("class") ?? "")) return true;
    try {
      return /material|symbols|icons/i.test(getComputedStyle(el).fontFamily);
    } catch {
      return false;
    }
  }

  /** Meet keeps hidden copies of some controls; only what is on screen counts. */
  function visible(el) {
    return Boolean(el && el.getClientRects().length);
  }

  function captionRegion() {
    return q(document, opts.captionRegionSelector).find(visible) ?? null;
  }

  function q(root, selector) {
    try {
      return [...root.querySelectorAll(selector)];
    } catch {
      return [];
    }
  }

  function readPeople() {
    const seen = new Set();
    for (const tile of q(document, opts.tileSelector).filter(visible)) {
      const name = q(tile, opts.nameSelector)
        .filter((el) => !isIcon(el))
        .map((el) => cleanName(el.innerText || el.textContent))
        .find(Boolean);
      if (name) seen.add(name);
    }
    for (const item of q(document, opts.peopleListSelector)) {
      const name = cleanName(item.getAttribute("aria-label"));
      if (name) seen.add(name);
    }
    for (const name of seen) roster.add(name);
    return seen;
  }

  /** Who-is-talking: tiles Meet highlights as speaking (best effort; Meet does not document it). */
  function readSpeaking() {
    const names = new Set();
    for (const tile of q(document, opts.tileSelector).filter(visible)) {
      if (!q(tile, opts.speakingSelector).some(visible) && !tile.matches(opts.speakingSelector)) continue;
      const name = q(tile, opts.nameSelector)
        .filter((el) => !isIcon(el))
        .map((el) => cleanName(el.innerText || el.textContent))
        .find(Boolean);
      if (name) names.add(name);
    }
    return [...names];
  }

  /* ---------- captions (bot's own view only) ---------- */

  function parseBlock(block) {
    const lines = block.innerText
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l && !UI_NOISE.has(l.toLowerCase()));
    if (lines.length < 2) return null;
    const [speaker, ...rest] = lines;
    if (!speaker || speaker.length > 80) return null;
    return { speaker: speaker.replace(NAME_SUFFIX, "").trim(), text: rest.join(" ") };
  }

  function captionBlocks(region) {
    let best = [];
    let level = [region];
    for (let depth = 0; depth < 4 && level.length; depth++) {
      for (const container of level) {
        const blocks = [...container.children].filter((c) => c instanceof HTMLElement && parseBlock(c));
        if (blocks.length && blocks.length >= best.length) best = blocks;
      }
      level = level.flatMap((el) => [...el.children].filter((c) => c instanceof HTMLElement));
    }
    return best;
  }

  function mergeText(previous, next) {
    if (next === previous || previous.includes(next)) return previous;
    if (next.includes(previous) || next.startsWith(previous.slice(0, 20))) return next;
    for (let size = Math.min(previous.length, next.length); size >= 10; size--) {
      if (previous.endsWith(next.slice(0, size))) return previous + next.slice(size);
    }
    return next.length < previous.length * 0.5 ? `${previous} ${next}` : next;
  }

  function scanCaptions() {
    if (startedAt === null) return;
    const region = captionRegion();
    const now = (performance.now() - startedAt) / 1000;
    const seen = new Set();
    if (region) {
      for (const block of captionBlocks(region)) {
        const parsed = parseBlock(block);
        if (!parsed) continue;
        seen.add(block);
        const existing = captions.live.get(block);
        if (existing && existing.speaker === parsed.speaker) {
          const merged = mergeText(existing.text, parsed.text);
          if (merged !== existing.text) {
            existing.text = merged;
            existing.end = now;
            captions.dirty.set(existing.id, existing);
          }
          continue;
        }
        if (existing) captions.dirty.set(existing.id, existing);
        const entry = { id: crypto.randomUUID(), speaker: parsed.speaker, text: parsed.text, start: now, end: now };
        captions.live.set(block, entry);
        captions.dirty.set(entry.id, entry);
      }
    }
    for (const [block, entry] of captions.live) {
      if (!seen.has(block)) {
        captions.dirty.set(entry.id, entry);
        captions.live.delete(block);
      }
    }
  }
  setInterval(scanCaptions, 500);

  /* ---------- screen state ---------- */

  function screen() {
    const text = (document.body?.innerText ?? "").toLowerCase();
    if (/you['’]ve been removed|you have been removed|the call has ended|meeting has ended|you left the meeting|call ended/.test(text)) return "ended";
    // Meet refused this browser itself (not a person): usually a guest browser it thinks is automated.
    if (/you can['’]t join this (video )?call/.test(text)) return "blocked";
    if (/check your meeting code|meeting code (is )?invalid|invalid video call name/.test(text)) return "invalid";
    if (/no one responded to your request|someone in the call denied your request|denied your request to join/.test(text)) return "denied";
    // The waiting room also shows a hang-up button, so it is checked first.
    if (/asking to be let in|asking to join|you['’]ll join the call when someone lets you in|wait until a meeting host brings you|someone will let you in soon|waiting for the host/.test(text)) return "waiting";
    if (q(document, opts.leaveButtonSelector).some(visible)) return "incall";
    return "prejoin";
  }

  /** Someone shares a screen: Meet then shows only a few tiles, so a missing name means nothing. */
  function presenting() {
    if (/is presenting|you are presenting|presenting now/i.test(document.body?.innerText ?? "")) return true;
    return q(document, opts.tileSelector).some((tile) => /\((presenting|presentation)\)|'s presentation/i.test(tile.innerText ?? ""));
  }

  window.__meetmind = {
    configure(next) {
      Object.assign(opts, next || {});
    },
    rotate,
    stopAll,
    captureTab,
    stopTab,
    /** Everything new since the last call; Python polls this twice a second. */
    snapshot(next) {
      if (next) Object.assign(opts, next);
      const present = readPeople();
      const updates = [...captions.dirty.values()].map((c) => ({ ...c }));
      captions.dirty.clear();
      const out = {
        screen: screen(),
        present: [...present],
        roster: [...roster],
        tracks: audio.tracks.size,
        tabAudio: Boolean(audio.tab),
        levels,
        captions: updates,
        captionsOn: Boolean(captionRegion()),
        speaking: startedAt === null ? [] : readSpeaking(),
        t: startedAt === null ? 0 : (performance.now() - startedAt) / 1000,
        title: document.title,
        presenting: presenting(),
      };
      levels = [];
      return out;
    },
  };
})();
