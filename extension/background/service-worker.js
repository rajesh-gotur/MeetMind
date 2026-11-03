/**
 * Service worker: owns the recording state machine and coordinates
 * the offscreen recorder and the Meet caption scraper.
 */
import { analyzeMeeting } from "../shared/api.js";
import { clearAll } from "../shared/chunk-store.js";
import { parseMeetLink } from "../shared/meet-link.js";
import { CONFIG, backendConfig, configProblems, getPreferences, loadSession, saveSession } from "../shared/settings.js";
import { buildAnalyzePayload, fileSlug, remotePeople, selfLabel } from "../shared/transcript.js";
const OFFSCREEN_URL = "offscreen/offscreen.html";
const BADGE_RED = "#DC2626";
const BADGE_GREY = "#64748B";
/* ---------- lifecycle ---------- */
chrome.runtime.onInstalled.addListener((details) => {
    // First install: open the 1-minute setup in a new tab.
    if (details.reason === chrome.runtime.OnInstalledReason.INSTALL) {
        void chrome.tabs.create({ url: chrome.runtime.getURL("options/options.html") });
    }
});
chrome.runtime.onStartup.addListener(() => void recoverAfterRestart());
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.target)
        return false; // meant for the offscreen document or the popup
    handle(message, sender)
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: errorText(error) }));
    return true; // keep the channel open for the async response
});
chrome.tabs.onRemoved.addListener((tabId) => {
    void loadSession().then((session) => {
        if (session?.tabId === tabId && session.status === "recording")
            void stopRecording();
    });
});
async function handle(message, sender) {
    switch (message.type) {
        case "START":
            return startRecording(message);
        case "STOP":
            return stopRecording();
        case "RETRY_FAILED":
            return retryFailed();
        case "ANALYZE":
            return analyzeAgain();
        case "RESUME_ANALYSIS":
            return resumeAnalysis();
        case "SCREENSHOT_TAKEN":
            await updateSession((s) => {
                if (!s.screenshots.some((shot) => shot.name === message.name)) {
                    s.screenshots.push({ name: message.name, time: message.time });
                }
            });
            return { ok: true };
        case "VIDEO_READY":
            return saveVideo(message.url);
        case "RESET":
            await saveSession(null);
            await clearAll().catch(() => undefined);
            await setBadge("");
            return { ok: true };
        case "GET_PROMPT":
            return { ok: true, names: await knownNames() };
        case "CHUNK_STARTED":
            await updateSession((s) => {
                for (const chunk of s.chunks)
                    if (chunk.state === "recording")
                        chunk.state = "uploading";
                for (const track of message.tracks) {
                    s.chunks.push({ index: message.index, offset: message.offset, track, state: "recording" });
                }
            });
            return { ok: true };
        case "CHUNK_UPLOADING":
            await updateSession((s) => setChunkState(s, message.index, message.track, "uploading"));
            return { ok: true };
        case "CHUNK_RESULT":
            await updateSession((s) => {
                setChunkState(s, message.index, message.track, "done");
                if (message.engine)
                    s.engine = message.engine;
                // A retried piece replaces anything already stored for the same piece.
                s.segments = s.segments.filter((seg) => !(seg.chunk === message.index && seg.track === message.track));
                s.segments.push(...message.segments.map((seg) => ({ ...seg, track: message.track, chunk: message.index })));
                s.segments.sort((a, b) => a.start - b.start);
            });
            return { ok: true };
        case "CHUNK_FAILED":
            await updateSession((s) => setChunkState(s, message.index, message.track, "failed", message.error));
            return { ok: true };
        case "RECORDING_WARNING":
            await updateSession((s) => {
                if (!s.warnings.includes(message.message))
                    s.warnings.push(message.message);
            });
            return { ok: true };
        case "CAPTIONS_BATCH":
            if (sender.tab?.id !== undefined) {
                await mergeCaptions(sender.tab.id, message.entries, message.roster, message.selfName);
                if (message.micMuted !== undefined)
                    await followMute(sender.tab.id, message.micMuted);
            }
            return { ok: true };
        case "RECORDING_FINISHED":
            await finish();
            return { ok: true };
        case "RECORDING_ERROR":
            await fail(message.error);
            return { ok: true };
    }
}
/* ---------- recording ---------- */
async function startRecording(request) {
    const problems = configProblems();
    if (problems.length)
        return { ok: false, error: problems[0] };
    const current = await loadSession();
    if (current && ["starting", "recording", "stopping"].includes(current.status)) {
        if (await offscreenOpen())
            return { ok: false, error: "A recording is already in progress." };
        // The recorder is gone (Chrome crashed or was closed): keep what was saved, then start fresh.
        await finish();
    }
    await clearAll().catch(() => undefined); // audio and screenshots kept from an older meeting
    const tab = await chrome.tabs.get(request.tabId);
    const meetingCode = parseMeetLink(tab.url)?.code ?? "meeting";
    const session = {
        id: crypto.randomUUID(),
        tabId: request.tabId,
        meetingCode,
        title: cleanTitle(tab.title, meetingCode),
        mode: request.mode,
        language: request.language,
        startedAt: Date.now(),
        status: "starting",
        warnings: [],
        chunks: [],
        segments: [],
        captions: [],
        roster: [],
        screenshots: [],
    };
    await saveSession(session);
    await ensureOffscreen();
    const startMessage = {
        target: "offscreen",
        type: "OFFSCREEN_START",
        sessionId: session.id,
        streamId: request.streamId,
        mode: request.mode,
        includeMic: request.includeMic,
        micMuted: false,
        language: request.language,
        backend: await backendConfig(),
    };
    const ack = (await chrome.runtime.sendMessage(startMessage));
    if (!ack?.ok || !ack.startedAt) {
        const error = ack?.error ?? "Could not start the recorder.";
        await fail(error);
        return { ok: false, error };
    }
    const startedAt = ack.startedAt;
    await updateSession((s) => {
        s.startedAt = startedAt;
        s.status = "recording";
    });
    await startCaptions(request.tabId, startedAt);
    await setBadge("REC");
    return { ok: true, startedAt };
}
async function stopRecording() {
    const session = await loadSession();
    if (!session || session.status !== "recording")
        return { ok: false, error: "Nothing is recording." };
    await updateSession((s) => {
        s.status = "stopping";
        s.endedAt = Date.now();
    });
    await setBadge("…");
    // Pull the last caption lines before the recorder finalises.
    try {
        const ack = (await chrome.tabs.sendMessage(session.tabId, { type: "CAPTIONS_STOP" }));
        await mergeCaptions(session.tabId, ack?.entries ?? [], ack?.roster, ack?.selfName);
    }
    catch {
        // Tab closed or navigated away: captions already sent in batches are kept.
    }
    const stop = { target: "offscreen", type: "OFFSCREEN_STOP" };
    try {
        await chrome.runtime.sendMessage(stop);
    }
    catch {
        await finish(); // offscreen document already gone
    }
    return { ok: true };
}
async function retryFailed() {
    const session = await loadSession();
    if (!session || session.status !== "done")
        return { ok: false, error: "Nothing to retry." };
    const failed = session.chunks.filter((c) => c.state === "failed");
    if (!failed.length)
        return { ok: false, error: "No failed audio pieces." };
    await updateSession((s) => {
        s.status = "stopping";
        for (const chunk of s.chunks) {
            if (chunk.state === "failed") {
                chunk.state = "uploading";
                delete chunk.error;
            }
        }
    });
    await ensureOffscreen();
    const retry = {
        target: "offscreen",
        type: "OFFSCREEN_RETRY",
        sessionId: session.id,
        chunks: failed.map(({ index, offset, track }) => ({ index, offset, track })),
        language: session.language,
        backend: await backendConfig(),
    };
    chrome.runtime.sendMessage(retry).catch(() => void finish());
    return { ok: true };
}
async function finish() {
    let analyze = false;
    await updateSession((s) => {
        if (s.status === "error")
            return;
        s.endedAt ??= Date.now();
        for (const chunk of s.chunks) {
            if (chunk.state === "uploading" || chunk.state === "recording") {
                chunk.state = "failed";
                chunk.error = "Upload did not finish.";
            }
        }
        // The transcript changed, so any older summary is out of date.
        delete s.report;
        delete s.reportError;
        delete s.turns;
        delete s.participants;
        analyze = CONFIG.report.autoGenerate && s.segments.length > 0;
        s.status = analyze ? "analyzing" : "done";
        if (analyze)
            s.analyzingSince = Date.now();
    });
    await setBadge("");
    await closeOffscreen();
    if (analyze)
        await runAnalysis();
}
/* ---------- meeting summary ---------- */
/** True only inside this service-worker instance; false again if Chrome restarted the worker. */
let analysisRunning = false;
async function runAnalysis() {
    const [session, preferences] = await Promise.all([loadSession(), getPreferences()]);
    if (!session || analysisRunning)
        return;
    analysisRunning = true;
    // Chrome stops an idle extension worker after about 30 s, which would silently drop the
    // pending request and leave the popup at "Preparing your notes" forever. A light API call
    // every 20 s keeps the worker alive until the answer arrives.
    const keepAlive = setInterval(() => void chrome.runtime.getPlatformInfo(), 20_000);
    try {
        const result = await analyzeMeeting(await backendConfig(), buildAnalyzePayload(session, preferences.displayName));
        await updateSession((s) => {
            if (s.id !== session.id)
                return;
            s.report = result.report;
            s.turns = result.turns;
            s.participants = result.participants;
            delete s.reportError;
            s.status = "done";
        });
    }
    catch (error) {
        await updateSession((s) => {
            if (s.id !== session.id)
                return;
            s.reportError = errorText(error);
            s.status = "done";
        });
    }
    finally {
        clearInterval(keepAlive);
        analysisRunning = false;
    }
}
/** Popup opened while notes are "being written": if this worker never started them (it was restarted), start again. */
async function resumeAnalysis() {
    const session = await loadSession();
    if (session?.status === "analyzing" && !analysisRunning) {
        await updateSession((s) => {
            s.analyzingSince = Date.now();
        });
        void runAnalysis();
    }
    return { ok: true };
}
async function analyzeAgain() {
    const session = await loadSession();
    if (!session || session.status !== "done" || session.segments.length === 0) {
        return { ok: false, error: "There is no transcript to summarise yet." };
    }
    await updateSession((s) => {
        s.status = "analyzing";
        s.analyzingSince = Date.now();
        delete s.reportError;
    });
    void runAnalysis();
    return { ok: true };
}
/* ---------- video ---------- */
/** Saves the video from the offscreen document's blob URL and answers once it is on disk. */
async function saveVideo(url) {
    const session = await loadSession();
    if (!session)
        return { ok: false, error: "No session." };
    const filename = `${CONFIG.downloads.folder}/${fileSlug(session)}-recording.webm`;
    try {
        const id = await chrome.downloads.download({ url, filename, saveAs: false, conflictAction: "uniquify" });
        const item = await waitForDownload(id);
        if (item?.state !== "complete")
            throw new Error(item?.error ?? "The download did not finish.");
        await updateSession((s) => {
            s.videoFile = item.filename.split(/[\\/]/).slice(-2).join("/");
        });
        return { ok: true };
    }
    catch (error) {
        await updateSession((s) => s.warnings.push(`The video could not be saved: ${errorText(error)}`));
        return { ok: false, error: errorText(error) };
    }
}
function waitForDownload(id, timeoutMs = 600_000) {
    return new Promise((resolve) => {
        const done = async () => {
            chrome.downloads.onChanged.removeListener(listener);
            clearTimeout(timer);
            const [item] = await chrome.downloads.search({ id });
            resolve(item);
        };
        const listener = (delta) => {
            if (delta.id === id && (delta.state?.current === "complete" || delta.state?.current === "interrupted"))
                void done();
        };
        const timer = setTimeout(() => void done(), timeoutMs);
        chrome.downloads.onChanged.addListener(listener);
        // It may already have finished before the listener was added.
        void chrome.downloads.search({ id }).then(([item]) => {
            if (item && item.state !== "in_progress")
                void done();
        });
    });
}
async function fail(error) {
    await updateSession((s) => {
        s.status = "error";
        s.error = error;
    });
    await setBadge("");
    await closeOffscreen();
}
/** If Chrome restarted mid-recording, the recorder is gone: close the session cleanly. */
async function recoverAfterRestart() {
    const session = await loadSession();
    if (session?.status === "analyzing") {
        void resumeAnalysis(); // the transcript is complete; only the notes were interrupted
        return;
    }
    if (session && ["starting", "recording", "stopping"].includes(session.status)) {
        await updateSession((s) => s.warnings.push("Chrome closed during the recording, so it stopped early."));
        await finish();
    }
}
/* ---------- captions ---------- */
async function startCaptions(tabId, startedAt) {
    const message = {
        type: "CAPTIONS_START",
        startedAt,
        autoEnable: CONFIG.captions.autoEnable,
        regionSelector: CONFIG.captions.regionSelector,
        turnOnButtonLabel: CONFIG.captions.turnOnButtonLabel,
        tileSelector: CONFIG.participants.tileSelector,
        nameSelector: CONFIG.participants.nameSelector,
        peopleListSelector: CONFIG.participants.peopleListSelector,
        micButtonSelector: CONFIG.participants.micButtonSelector,
    };
    let ack;
    try {
        ack = (await chrome.tabs.sendMessage(tabId, message));
    }
    catch {
        // The Meet tab was open before the extension was installed: inject the page reader now.
        try {
            await chrome.scripting.executeScript({ target: { tabId }, files: ["content/meet-scraper.js"] });
            ack = (await chrome.tabs.sendMessage(tabId, message));
        }
        catch {
            await updateSession((s) => s.warnings.push("Could not read the Meet page, so other people's names may be missing. Reload the Meet tab next time."));
        }
    }
    if (ack)
        await mergeCaptions(tabId, [], ack.roster, ack.selfName);
}
async function mergeCaptions(tabId, entries, roster, selfName) {
    await updateSession((s) => {
        if (s.tabId !== tabId)
            return;
        if (entries.length) {
            const byId = new Map(s.captions.map((c) => [c.id, c]));
            for (const entry of entries)
                byId.set(entry.id, entry);
            s.captions = [...byId.values()].sort((a, b) => a.start - b.start);
        }
        // People who joined at any time during the call stay in the list.
        if (roster?.length)
            s.roster = [...new Set([...(s.roster ?? []), ...roster])];
        if (selfName)
            s.selfName = selfName;
    });
}
/** Your mic track records silence while Meet shows you as muted. */
async function followMute(tabId, muted) {
    if (!CONFIG.recording.followMeetMute)
        return;
    const session = await loadSession();
    if (session?.tabId !== tabId || !["starting", "recording"].includes(session.status))
        return;
    const message = { target: "offscreen", type: "OFFSCREEN_MIC", muted };
    await chrome.runtime.sendMessage(message).catch(() => undefined);
}
/** The speech engine spells names correctly when it is told them first (project words are added by the server). */
async function knownNames() {
    const [session, preferences] = await Promise.all([loadSession(), getPreferences()]);
    if (!session)
        return preferences.displayName ? [preferences.displayName] : [];
    return [selfLabel(session, preferences.displayName), ...remotePeople(session, preferences.displayName)].filter((name) => name !== "You");
}
/* ---------- helpers ---------- */
let writeQueue = Promise.resolve();
/** Serialises read-modify-write updates so concurrent events never overwrite each other. */
function updateSession(mutate) {
    writeQueue = writeQueue
        .then(async () => {
        const session = await loadSession();
        if (!session)
            return;
        mutate(session);
        await saveSession(session);
    })
        .catch((error) => console.error("[MeetMind] session update failed", error));
    return writeQueue;
}
function setChunkState(session, index, track, state, error) {
    const chunk = session.chunks.find((c) => c.index === index && c.track === track);
    if (!chunk)
        return;
    chunk.state = state;
    if (error)
        chunk.error = error;
    else
        delete chunk.error;
}
async function offscreenOpen() {
    const contexts = await chrome.runtime.getContexts({ contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT] });
    return contexts.length > 0;
}
async function ensureOffscreen() {
    if (await offscreenOpen())
        return;
    await chrome.offscreen.createDocument({
        url: OFFSCREEN_URL,
        reasons: [chrome.offscreen.Reason.USER_MEDIA],
        justification: "Record Google Meet tab audio and microphone for transcription.",
    });
}
async function closeOffscreen() {
    const contexts = await chrome.runtime.getContexts({ contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT] });
    if (contexts.length > 0)
        await chrome.offscreen.closeDocument();
}
async function setBadge(text) {
    await chrome.action.setBadgeText({ text });
    if (text)
        await chrome.action.setBadgeBackgroundColor({ color: text === "REC" ? BADGE_RED : BADGE_GREY });
}
function cleanTitle(title, meetingCode) {
    const cleaned = (title ?? "").replace(/^Meet\s*[-–:]\s*/i, "").trim();
    return cleaned && cleaned !== meetingCode ? cleaned : `Meeting ${meetingCode}`;
}
function errorText(error) {
    return error instanceof Error ? error.message : String(error);
}
