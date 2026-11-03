/**
 * Popup controller. Two ways to record a meeting:
 *   - MeetMind bot: paste a Meet link; the server's bot joins as a guest and records it (most accurate).
 *   - Record here: this browser records the Meet tab and your microphone.
 * Both end on the same results screen with the same files.
 */

import {
  botLiveUrl,
  checkAuth,
  checkHealth,
  exportFiles,
  getBotFile,
  getBotStatus,
  renotesBot,
  resetBot,
  startBot,
  stopBot,
} from "../shared/api.js";
import { parseMeetLink } from "../shared/meet-link.js";
import { listShots } from "../shared/chunk-store.js";
import type {
  Ack,
  AnalyzeAgainRequest,
  AudioLevel,
  ResetRequest,
  ResumeAnalysisRequest,
  RetryRequest,
  StartRequest,
  StopRequest,
} from "../shared/messages.js";
import {
  SESSION_KEY,
  backendConfig,
  configProblems,
  getPreferences,
  loadSession,
  microphoneAllowed,
  savePreferences,
  serverUrl,
} from "../shared/settings.js";
import { buildExportPayload, buildTranscriptText, fileSlug } from "../shared/transcript.js";
import type {
  BotStatus,
  ExportKind,
  Language,
  MeetingSession,
  Preferences,
  RecordingMode,
  StartMode,
} from "../shared/types.js";
import { BOT_ACTIVE, botResults, renderBotProgress } from "./bot-view.js";
import { $, element, errorText, saveFile, toast } from "./dom.js";
import { buildRibbon, renderProcessing, renderRecording, sessionResults, showLevels } from "./record-view.js";
import { renderResults } from "./results.js";

type View = "config" | "setup" | "start" | "bot" | "recording" | "stopping" | "done" | "error";
type Source = "session" | "bot";
type WorkerRequest =
  | StartRequest
  | StopRequest
  | ResetRequest
  | RetryRequest
  | AnalyzeAgainRequest
  | ResumeAnalysisRequest;

const BOT_POLL_MS = 1500;

const state = {
  tab: null as chrome.tabs.Tab | null,
  preferences: null as Preferences | null,
  session: null as MeetingSession | null,
  bot: null as BotStatus | null,
  micAllowed: false,
  view: null as View | null,
  source: "session" as Source,
  ticker: null as number | null,
  busy: false,
};

document.addEventListener("DOMContentLoaded", () => void init());

async function init(): Promise<void> {
  const [[tab], preferences, session, micAllowed] = await Promise.all([
    chrome.tabs.query({ active: true, currentWindow: true }),
    getPreferences(),
    loadSession(),
    microphoneAllowed(),
  ]);
  Object.assign(state, { tab: tab ?? null, preferences, session, micAllowed });

  $<HTMLInputElement>("mic-toggle").checked = preferences.includeMic;
  $<HTMLSelectElement>("language").value = preferences.language;
  if (onMeetTab()) $<HTMLInputElement>("bot-link").value = cleanMeetLink(state.tab?.url ?? "");
  buildRibbon();
  bindEvents();
  render();
  void checkServer();
  void pollBot();
  if (session?.status === "analyzing") void request({ type: "RESUME_ANALYSIS" });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !(SESSION_KEY in changes)) return;
    state.session = (changes[SESSION_KEY]?.newValue as MeetingSession | undefined) ?? null;
    render();
  });
  chrome.runtime.onMessage.addListener((message: AudioLevel) => {
    if (message.target === "popup" && message.type === "AUDIO_LEVEL" && state.view === "recording") showLevels(message);
    return false;
  });
}

function bindEvents(): void {
  $("tab-bot").addEventListener("click", () => void chooseMode("bot"));
  $("tab-record").addEventListener("click", () => void chooseMode("record"));
  $("bot-paste").addEventListener("click", () => void pasteLink());
  $("bot-link").addEventListener("input", () => {
    showStartError("");
    updateLinkHint();
  });
  $("bot-link").addEventListener("blur", () => {
    const field = $<HTMLInputElement>("bot-link");
    if (parseMeetLink(field.value)) field.value = cleanMeetLink(field.value);
  });
  $("bot-link").addEventListener("keydown", (event) => {
    if (event.key === "Enter") void sendBot();
  });
  $("bot-join").addEventListener("click", () => void sendBot());
  $("bot-stop").addEventListener("click", () => void stopTheBot());
  $("bot-live").addEventListener("click", () => void openLiveView());
  $("start-btn").addEventListener("click", () => void startRecording());
  $("stop-btn").addEventListener("click", () => void stopRecording());
  $("retry-btn").addEventListener("click", () => void request({ type: "RETRY_FAILED" }));
  $("analyze-btn").addEventListener("click", () => void request({ type: "ANALYZE" }));
  $("renotes-btn").addEventListener("click", () => void renotes());
  $("new-btn").addEventListener("click", () => void startOver());
  $("reset-btn").addEventListener("click", () => void startOver());
  $("download-all").addEventListener("click", () => void download("zip", $("download-all")));
  for (const button of document.querySelectorAll<HTMLButtonElement>(".file[data-kind]")) {
    button.addEventListener("click", () => void download(button.dataset.kind as ExportKind, button));
  }
  for (const id of ["settings-btn", "open-setup"]) {
    $(id).addEventListener("click", () => void chrome.runtime.openOptionsPage());
  }
  $("mic-toggle").addEventListener("change", () => {
    void updatePreferences({ includeMic: $<HTMLInputElement>("mic-toggle").checked });
    renderMicHint();
  });
  $("language").addEventListener("change", () => {
    void updatePreferences({ language: $<HTMLSelectElement>("language").value as Language });
  });
}

/* ---------- which screen ---------- */

function currentView(): View {
  if (configProblems().length) return "config";
  if (!state.preferences?.setupComplete) return "setup";
  const session = state.session;
  const bot = state.bot;
  if (session?.status === "starting" || session?.status === "recording") return "recording";
  if (session?.status === "stopping" || session?.status === "analyzing") return "stopping";
  if (bot && BOT_ACTIVE.has(bot.phase)) return "bot";

  // Both may have finished results; show the most recent one.
  const sessionEnd = session && (session.status === "done" || session.status === "error") ? (session.endedAt ?? 0) : -1;
  const botEnd = bot && (bot.phase === "done" || bot.phase === "error") ? (bot.ended_at ?? bot.now) * 1000 : -1;
  if (sessionEnd < 0 && botEnd < 0) return "start";
  state.source = botEnd > sessionEnd ? "bot" : "session";
  if (state.source === "bot") return bot?.phase === "done" ? "done" : "error";
  return session?.status === "done" ? "done" : "error";
}

function render(): void {
  const view = currentView();
  if (view !== state.view) {
    state.view = view;
    document.body.dataset.view = view;
    for (const panel of document.querySelectorAll<HTMLElement>("[data-view]")) {
      panel.hidden = panel.dataset.view !== view;
    }
  }
  if (state.ticker !== null) clearInterval(state.ticker);
  state.ticker = null;

  const displayName = state.preferences?.displayName ?? "";
  let tick: (() => void) | null = null;
  if (view === "config") renderConfigProblems();
  else if (view === "start") renderStart();
  else if (view === "bot" && state.bot) tick = renderBotProgress(state.bot);
  else if (view === "recording" && state.session) {
    tick = renderRecording(state.session, displayName, state.preferences?.includeMic ?? true);
  } else if (view === "stopping" && state.session) tick = renderProcessing(state.session);
  else if (view === "done") renderDone(displayName);
  else if (view === "error") renderError();
  if (tick) state.ticker = window.setInterval(tick, 1000);
  renderWarnings();
}

/* ---------- start: bot or record here ---------- */

function onMeetTab(): boolean {
  return parseMeetLink(state.tab?.url) !== null;
}

/** The clean link when the text is any kind of Meet link, else the text as typed. */
function cleanMeetLink(text: string): string {
  return parseMeetLink(text)?.url ?? text.trim();
}

/** Checks the link while it is typed or pasted and says which meeting it found. */
function updateLinkHint(): void {
  const hint = $("bot-link-hint");
  const unavailable = state.bot && !state.bot.available ? state.bot.unavailable_reason : "";
  const text = $<HTMLInputElement>("bot-link").value.trim();
  const link = parseMeetLink(text);
  hint.classList.toggle("field__hint--warn", Boolean(unavailable) || (Boolean(text) && !link));
  hint.classList.toggle("field__hint--ok", !unavailable && Boolean(link));
  if (unavailable) hint.textContent = `${unavailable} Until then, use Record here.`;
  else if (link) hint.textContent = `Meeting ${link.code} found. The bot joins as a guest; someone clicks Admit.`;
  else if (text) hint.textContent = "This does not look like a Google Meet link yet.";
  else hint.textContent = "Paste any Google Meet link. The bot joins as a guest; someone clicks Admit.";
}

function renderStart(): void {
  const mode = state.preferences?.startMode ?? "bot";
  const onMeet = onMeetTab();
  const code = parseMeetLink(state.tab?.url)?.code ?? "";
  const title = (state.tab?.title ?? "").replace(/^Meet\s*[-–:]\s*/i, "").trim();

  $("start-tag").classList.toggle("live-tag--off", !onMeet);
  $("start-tag-text").textContent = onMeet ? "In a call" : "No call in this tab";
  $("start-code").hidden = !onMeet;
  $("start-code").textContent = code;
  $("start-title").textContent = onMeet ? (title && title !== code ? title : "Google Meet call") : "Record a Google Meet";
  $("start-note").textContent =
    mode === "bot"
      ? "The MeetMind bot joins the call and records it for you."
      : "This browser records the Meet tab and your microphone.";

  for (const [id, tabMode] of [
    ["tab-bot", "bot"],
    ["tab-record", "record"],
  ] as const) {
    $(id).setAttribute("aria-selected", String(mode === tabMode));
  }
  $("panel-bot").hidden = mode !== "bot";
  $("panel-record").hidden = mode !== "record";
  $("mic-row").hidden = mode !== "record";
  $("bot-join").hidden = mode !== "bot";
  $("start-btn").hidden = mode !== "record";

  const unavailable = state.bot && !state.bot.available ? state.bot.unavailable_reason : "";
  $<HTMLButtonElement>("bot-join").disabled = state.busy || Boolean(unavailable);
  const link = $<HTMLInputElement>("bot-link");
  if (!link.value && state.bot?.meeting_url) link.value = state.bot.meeting_url; // after "Start again"
  updateLinkHint();

  $("record-offmeet").hidden = onMeet;
  $<HTMLButtonElement>("start-btn").disabled = state.busy || !onMeet;
  renderMicHint();
}

function renderMicHint(): void {
  const hint = $("mic-hint");
  const blocked = $<HTMLInputElement>("mic-toggle").checked && !state.micAllowed;
  hint.textContent = blocked ? "Not allowed yet. Open setup to allow it." : "Records your own voice too";
  hint.className = blocked ? "warn-text" : "muted";
}

async function chooseMode(startMode: StartMode): Promise<void> {
  showStartError("");
  await updatePreferences({ startMode });
  render();
}

async function pasteLink(): Promise<void> {
  try {
    const text = await navigator.clipboard.readText();
    $<HTMLInputElement>("bot-link").value = cleanMeetLink(text);
    showStartError("");
    updateLinkHint();
  } catch {
    $<HTMLInputElement>("bot-link").focus();
    toast("Press Ctrl+V to paste the link.");
  }
}

/* ---------- the bot ---------- */

async function sendBot(): Promise<void> {
  if (state.busy) return;
  const parsed = parseMeetLink($<HTMLInputElement>("bot-link").value);
  const link = parsed?.url ?? "";
  if (!parsed) {
    showStartError("Paste the link of a Google Meet call (any meet.google.com link, or just the code like abc-defg-hij).");
    $<HTMLInputElement>("bot-link").focus();
    return;
  }
  setBusy(true, "Sending the bot…");
  try {
    await startBot(await backendConfig(), {
      meet_url: link,
      language: state.preferences?.language ?? "auto",
      your_name: state.preferences?.displayName ?? "",
    });
    await refreshBot();
  } catch (error) {
    showStartError(errorText(error));
  } finally {
    setBusy(false);
  }
}

async function stopTheBot(): Promise<void> {
  const button = $<HTMLButtonElement>("bot-stop");
  button.disabled = true;
  try {
    await stopBot(await backendConfig());
    await refreshBot();
  } catch (error) {
    toast(errorText(error));
  } finally {
    button.disabled = false;
  }
}

async function openLiveView(): Promise<void> {
  await chrome.tabs.create({ url: botLiveUrl(await backendConfig()) });
}

/** Follows the bot while the popup is open. */
async function pollBot(): Promise<void> {
  await refreshBot();
  window.setTimeout(() => void pollBot(), BOT_POLL_MS);
}

async function refreshBot(): Promise<void> {
  if (configProblems().length) return;
  state.bot = await getBotStatus(await backendConfig(), 3);
  render();
}

/* ---------- record in this browser ---------- */

async function startRecording(): Promise<void> {
  const tabId = state.tab?.id;
  const preferences = state.preferences;
  if (tabId === undefined || !preferences || state.busy) return;
  setBusy(true, "Starting…");
  showStartError("");
  try {
    // Must run inside the click handler: Chrome only allows tab capture after a user gesture.
    const streamId = await getTabStreamId(tabId);
    const mode = (document.querySelector<HTMLInputElement>('input[name="mode"]:checked')?.value ??
      "audio") as RecordingMode;
    const ack = await request({
      type: "START",
      tabId,
      streamId,
      mode,
      language: preferences.language,
      includeMic: preferences.includeMic,
    });
    if (!ack.ok) showStartError(ack.error ?? "Recording could not start.");
  } catch (error) {
    showStartError(errorText(error));
  } finally {
    setBusy(false);
  }
}

async function stopRecording(): Promise<void> {
  const button = $<HTMLButtonElement>("stop-btn");
  button.disabled = true;
  const ack = await request({ type: "STOP" });
  button.disabled = false;
  if (!ack.ok) toast(ack.error ?? "Recording could not stop.");
}

function getTabStreamId(tabId: number): Promise<string> {
  return new Promise((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (streamId) => {
      const error = chrome.runtime.lastError;
      if (error || !streamId) reject(new Error(friendlyCaptureError(error?.message)));
      else resolve(streamId);
    });
  });
}

function friendlyCaptureError(message?: string): string {
  if (message && /active stream/i.test(message)) return "This tab is already being recorded. Reload the Meet tab and try again.";
  return "Chrome did not allow recording this tab. Click the MeetMind icon on the Meet tab and try again.";
}

async function request(message: WorkerRequest): Promise<Ack> {
  try {
    return ((await chrome.runtime.sendMessage(message)) as Ack | undefined) ?? { ok: false, error: "No response." };
  } catch (error) {
    return { ok: false, error: errorText(error) };
  }
}

/* ---------- results, errors, starting over ---------- */

function renderDone(displayName: string): void {
  if (state.source === "bot" && state.bot) renderResults(botResults(state.bot));
  else if (state.session) renderResults(sessionResults(state.session, displayName));
}

function renderError(): void {
  const fromBot = state.source === "bot";
  $("error-title").textContent = fromBot ? "The bot stopped" : "The recording stopped";
  $("error-text").textContent = (fromBot ? state.bot?.error : state.session?.error) || "Something went wrong.";
}

/** Bot meetings are saved on the server: write the notes again without recording again. */
async function renotes(): Promise<void> {
  const button = $<HTMLButtonElement>("renotes-btn");
  button.disabled = true;
  try {
    await renotesBot(await backendConfig());
    await refreshBot();
  } catch (error) {
    toast(errorText(error));
  } finally {
    button.disabled = false;
  }
}

async function startOver(): Promise<void> {
  if (state.source === "bot") {
    try {
      await resetBot(await backendConfig());
      await refreshBot();
    } catch (error) {
      toast(errorText(error));
    }
    return;
  }
  await request({ type: "RESET" });
}

function renderWarnings(): void {
  const box = $("warnings");
  const visible = state.view === "recording" || (state.view === "done" && state.source === "session");
  const warnings = visible ? (state.session?.warnings ?? []) : [];
  box.hidden = warnings.length === 0;
  box.textContent = warnings.join(" ");
}

function renderConfigProblems(): void {
  $("config-problems").replaceChildren(...configProblems().map((text) => element("li", text)));
}

/* ---------- downloads (same files for both sources) ---------- */

async function download(kind: ExportKind, button: HTMLElement): Promise<void> {
  if (button.getAttribute("aria-busy") === "true") return;
  button.setAttribute("aria-busy", "true");
  const isAll = button.id === "download-all";
  if (isAll) $("download-all-label").textContent = "Preparing your files…";
  try {
    const { blob, filename } = state.source === "bot" ? await getBotFile(await backendConfig(), kind) : await sessionFile(kind);
    await saveFile(blob, filename);
  } catch (error) {
    toast(`Download failed: ${errorText(error)}`);
  } finally {
    button.removeAttribute("aria-busy");
    if (isAll) $("download-all-label").textContent = "Download all files";
  }
}

async function sessionFile(kind: ExportKind): Promise<{ blob: Blob; filename: string }> {
  const session = state.session;
  if (!session) throw new Error("No recording.");
  const displayName = state.preferences?.displayName ?? "";
  if (!session.report) {
    // Without notes, the transcript is still built here in the browser.
    const text = buildTranscriptText(session, displayName);
    return { blob: new Blob([text], { type: "text/plain;charset=utf-8" }), filename: `${fileSlug(session)}_Transcript.txt` };
  }
  const shots = await listShots(session.id).catch(() => []);
  return exportFiles(
    await backendConfig(),
    kind,
    {
      ...buildExportPayload(session, displayName),
      report: session.report,
      screenshots: shots.map((s) => ({ name: s.name, time: s.time })),
    },
    shots.map((s) => ({ name: s.name, blob: s.blob })),
  );
}

/* ---------- server status ---------- */

async function checkServer(): Promise<void> {
  if (configProblems().length) return setServer("down", "Not set up");
  setServer("waking", "Connecting");
  // Render's free plan sleeps; this request also wakes it before you press Start.
  if (!(await checkHealth(serverUrl()))) return setServer("down", "Server offline");
  const auth = await checkAuth(await backendConfig());
  if (!auth.ok) {
    setServer("down", "Check setup");
    if (state.view === "start") showStartError(auth.error ?? "The server did not accept this extension.");
    return;
  }
  setServer("ok", "Ready");
  if (auth.minutesLeftToday !== undefined) {
    $("minutes-left").textContent = `${Math.floor(auth.minutesLeftToday)} min left today`;
  }
}

function setServer(value: "waking" | "ok" | "down", text: string): void {
  $("server-status").dataset.state = value;
  $("server-text").textContent = text;
}

/* ---------- small helpers ---------- */

async function updatePreferences(update: Partial<Preferences>): Promise<void> {
  state.preferences = await savePreferences(update);
}

function setBusy(busy: boolean, label = ""): void {
  state.busy = busy;
  $("bot-join-label").textContent = busy && label ? label : "Join meeting via bot";
  $("start-label").textContent = busy && label ? label : "Start recording";
  render();
}

function showStartError(message: string): void {
  const box = $("start-error");
  box.hidden = !message;
  box.textContent = message;
}
