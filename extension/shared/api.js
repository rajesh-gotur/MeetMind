/** Backend client. No chrome.* calls, so it runs in any extension page (including offscreen). */
export class UploadError extends Error {
}
/** Longest wait for the notes: the server tries up to three models of at most 60 s each. */
const ANALYZE_TIMEOUT_MS = 300_000;
function headers(target) {
    return { "X-MeetMind-Token": target.token, "X-MeetMind-Client": target.clientId };
}
/** True when the server answers. A sleeping Render server can take up to a minute to wake. */
export async function checkHealth(url, timeoutMs = 75_000) {
    try {
        const response = await fetch(`${url}/health`, { signal: AbortSignal.timeout(timeoutMs) });
        return response.ok;
    }
    catch {
        return false;
    }
}
/** Confirms the app token without using any transcription minutes. */
export async function checkAuth(target) {
    try {
        const response = await fetch(`${target.url}/auth/check`, {
            headers: headers(target),
            signal: AbortSignal.timeout(75_000),
        });
        if (response.status === 401)
            return { ok: false, error: "The app token in config.js does not match the server." };
        if (!response.ok)
            return { ok: false, error: await readError(response) };
        const body = (await response.json());
        return {
            ok: body.speech_to_text_ready,
            error: body.speech_to_text_ready ? undefined : "The server has no speech-to-text key (Deepgram or Groq) yet.",
            speechEngine: body.speech_engine ?? "",
            minutesLeftToday: body.minutes_left_today,
            speechToTextReady: body.speech_to_text_ready,
            summaryReady: body.summary_ready ?? false,
        };
    }
    catch {
        return { ok: false, error: "Could not reach the server." };
    }
}
/** Uploads one audio piece and returns its meeting-relative segments. Retries busy/network errors. */
export async function uploadChunk(target, chunk) {
    let lastError = "Unknown error";
    for (let attempt = 1; attempt <= chunk.attempts; attempt++) {
        const form = new FormData();
        form.append("audio", chunk.blob, `chunk-${chunk.index}-${chunk.track}.webm`);
        form.append("chunk_index", String(chunk.index));
        form.append("offset_seconds", chunk.offset.toFixed(2));
        form.append("track", chunk.track);
        if (chunk.language !== "auto")
            form.append("language", chunk.language);
        if (chunk.names.length)
            form.append("names", chunk.names.join("\n"));
        let response = null;
        try {
            response = await fetch(`${target.url}/transcribe`, { method: "POST", headers: headers(target), body: form });
            if (response.ok) {
                const body = (await response.json());
                return { segments: body.segments, engine: body.engine ?? "" };
            }
            lastError = await readError(response);
            // Bad token, too large, daily limit: retrying will not help.
            if (response.status < 500 && response.status !== 429 && response.status !== 408)
                break;
        }
        catch (error) {
            lastError = `Network error: ${error instanceof Error ? error.message : String(error)}`;
        }
        if (attempt < chunk.attempts)
            await sleep(retryDelayMs(response, attempt));
    }
    throw new UploadError(lastError);
}
/**
 * Corrected transcript plus summary, topics, decisions and verified tasks for the meeting.
 * The server needs one language-model call (usually 10-40 s). A timeout is NOT retried:
 * the server may still be working, and starting again would only double the wait.
 */
export async function analyzeMeeting(target, body) {
    let lastError = "Unknown error";
    for (let attempt = 1; attempt <= 2; attempt++) {
        let response = null;
        try {
            response = await fetch(`${target.url}/analyze`, {
                method: "POST",
                headers: { ...headers(target), "Content-Type": "application/json" },
                body: JSON.stringify(body),
                signal: AbortSignal.timeout(ANALYZE_TIMEOUT_MS),
            });
            if (response.ok) {
                const result = (await response.json());
                return { report: result.report, turns: result.turns ?? body.turns, participants: result.participants ?? body.participants };
            }
            lastError = await readError(response);
            // Only a sleeping/busy server (5xx before work started, 429) is worth one more try.
            if (response.status < 500 && response.status !== 429)
                break;
        }
        catch (error) {
            if (error instanceof DOMException && error.name === "TimeoutError") {
                throw new UploadError("The notes took too long. Your transcript is safe; click Try again.");
            }
            lastError = `Network error: ${error instanceof Error ? error.message : String(error)}`;
        }
        if (attempt < 2)
            await sleep(retryDelayMs(response, attempt));
    }
    throw new UploadError(lastError);
}
/** Builds the meeting files on the server. Returns the file and the name the server chose. */
export async function exportFiles(target, kind, payload, images) {
    const form = new FormData();
    form.append("payload", JSON.stringify(payload));
    for (const image of images)
        form.append("screenshots", image.blob, image.name);
    const response = await fetch(`${target.url}/export?kind=${kind}`, {
        method: "POST",
        headers: headers(target),
        body: form,
        signal: AbortSignal.timeout(180_000),
    });
    if (!response.ok)
        throw new UploadError(await readError(response));
    return { blob: await response.blob(), filename: filenameOf(response, `meetmind-${kind}`) };
}
/** The bot's live status, or null when the server is unreachable or too old to have a bot. */
export async function getBotStatus(target, tail = 0) {
    try {
        const response = await fetch(`${target.url}/bot/status?tail=${tail}`, {
            headers: headers(target),
            cache: "no-store",
            signal: AbortSignal.timeout(4000),
        });
        return response.ok ? (await response.json()) : null;
    }
    catch {
        return null;
    }
}
async function botCommand(target, path, body) {
    let response;
    try {
        response = await fetch(`${target.url}/bot/${path}`, {
            method: "POST",
            headers: { ...headers(target), "Content-Type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
            signal: AbortSignal.timeout(15_000),
        });
    }
    catch {
        throw new UploadError("Could not reach the MeetMind server.");
    }
    if (!response.ok)
        throw new UploadError(await readError(response));
}
export const startBot = (target, body) => botCommand(target, "start", body);
export const stopBot = (target) => botCommand(target, "stop");
export const resetBot = (target) => botCommand(target, "reset");
/** "Create notes again" from the bot's saved meeting (no recording, no speech engines called again). */
export const renotesBot = (target) => botCommand(target, "renotes");
/** The bot's finished files: the same kinds as a recording made in the browser. */
export async function getBotFile(target, kind) {
    const response = await fetch(`${target.url}/bot/files?kind=${kind}`, {
        headers: headers(target),
        signal: AbortSignal.timeout(120_000),
    });
    if (!response.ok)
        throw new UploadError(await readError(response));
    return { blob: await response.blob(), filename: filenameOf(response, `meetmind-${kind}`) };
}
/** Full-page live view of the bot, served by the backend. The token travels in the #hash only. */
export function botLiveUrl(target) {
    return `${target.url}/bot/live#token=${encodeURIComponent(target.token)}&client=${encodeURIComponent(target.clientId)}`;
}
function filenameOf(response, fallback) {
    const disposition = response.headers.get("content-disposition") ?? "";
    const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    return match?.[1] ? decodeURIComponent(match[1]) : fallback;
}
async function readError(response) {
    try {
        const body = (await response.json());
        if (typeof body.detail === "string")
            return body.detail;
    }
    catch {
        // not JSON
    }
    return `Server returned ${response.status}`;
}
/** Honours Retry-After; otherwise waits 5s, 10s, 20s, 30s... so a sleeping Render server has time to wake. */
function retryDelayMs(response, attempt) {
    const seconds = Number(response?.headers.get("retry-after") ?? Number.NaN);
    if (Number.isFinite(seconds))
        return Math.min(seconds, 60) * 1000;
    return Math.min(5 * 2 ** (attempt - 1), 30) * 1000;
}
function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
