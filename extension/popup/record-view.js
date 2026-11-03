/** Screens for a recording made in this browser: live recording, processing, and its results model. */
import { getShot } from "../shared/chunk-store.js";
import { CONFIG } from "../shared/settings.js";
import { formatClock, participantsOf, selfLabel } from "../shared/transcript.js";
import { $, element, personChip } from "./dom.js";
const RIBBON_BARS = 44;
const RIBBON_HALF = 36; // px: tallest bar on each side of the centre line
const RING_LENGTH = 326.7; // 2 * PI * r (r = 52)
const ribbon = Array.from({ length: RIBBON_BARS }, () => ({ mic: 0, tab: 0 }));
let thumbName = "";
let thumbUrl = "";
export function buildRibbon() {
    $("ribbon").replaceChildren(...Array.from({ length: RIBBON_BARS }, () => {
        const bar = element("i");
        bar.append(element("b"), element("b"));
        return bar;
    }));
}
/** Mirrored ribbon: your mic grows upward in marigold, the call grows downward in mint. */
export function showLevels(levels) {
    ribbon.push({ mic: Math.max(levels.mic, 0), tab: Math.max(levels.tab, 0) });
    ribbon.shift();
    const bars = $("ribbon").children;
    ribbon.forEach((level, i) => {
        const [up, down] = (bars[i]?.children ?? []);
        if (!up || !down)
            return;
        up.style.height = `${Math.max(2, Math.round(level.mic * RIBBON_HALF))}px`;
        down.style.height = `${Math.max(2, Math.round(level.tab * RIBBON_HALF))}px`;
    });
}
/** Returns a ticker that updates the timer once a second. */
export function renderRecording(session, displayName, includeMic) {
    const video = session.mode === "audio_video";
    $("rec-title").textContent = session.title;
    $("mode-tag").textContent = video ? "Audio and video" : "Audio";
    $("meter-mic-row").classList.toggle("meter--off", !includeMic);
    const self = selfLabel(session, displayName);
    const names = participantsOf(session, displayName);
    $("speakers").replaceChildren(...names.map((name) => personChip(name, name === self)), ...(names.length > 1 ? [] : [element("span", "Waiting for the others to appear…", "person person--waiting")]));
    $("shots-block").hidden = !video;
    if (video)
        void renderLatestShot(session);
    renderPieces(session);
    const tick = () => {
        const elapsed = (Date.now() - session.startedAt) / 1000;
        $("timer").textContent = formatClock(elapsed);
        const current = session.chunks.find((c) => c.state === "recording");
        const nextIn = current ? CONFIG.recording.chunkSeconds - (elapsed - current.offset) : 0;
        $("next-upload").textContent = nextIn > 0 ? `next in ${formatClock(nextIn).slice(3)}` : "";
    };
    tick();
    return tick;
}
async function renderLatestShot(session) {
    const count = session.screenshots.length;
    $("shot-count").textContent = count ? `${count} screenshot${count > 1 ? "s" : ""} taken` : "No screenshots yet";
    const latest = session.screenshots.at(-1);
    if (!latest || latest.name === thumbName)
        return;
    const stored = await getShot(session.id, latest.name).catch(() => undefined);
    if (!stored)
        return;
    if (thumbUrl)
        URL.revokeObjectURL(thumbUrl);
    thumbName = latest.name;
    thumbUrl = URL.createObjectURL(stored.blob);
    $("shot-thumb").src = thumbUrl;
}
/** One column per audio piece: the top sliver is your mic, the bottom one the call. */
function renderPieces(session) {
    const indexes = [...new Set(session.chunks.map((c) => c.index))].sort((a, b) => a - b);
    $("piece-dots").replaceChildren(...indexes.map((index) => {
        const column = element("span", "", "piece");
        const parts = session.chunks.filter((c) => c.index === index).sort((a) => (a.track === "mic" ? -1 : 1));
        column.title = `Piece ${index + 1}: ${parts.map((c) => `${c.track === "mic" ? "your mic" : "the call"} ${c.state}`).join(", ")}`;
        for (const part of parts) {
            const sliver = element("span");
            sliver.dataset.state = part.state;
            sliver.dataset.track = part.track;
            column.append(sliver);
        }
        return column;
    }));
}
/** Returns a ticker while the notes are being written (the ring keeps moving). */
export function renderProcessing(session) {
    const chunks = session.chunks;
    const finished = chunks.filter((c) => c.state === "done" || c.state === "failed").length;
    const analyzing = session.status === "analyzing";
    $("step-save-video").hidden = session.mode !== "audio_video";
    const saving = !analyzing && chunks.some((c) => c.state === "recording");
    const transcribing = !analyzing && !saving && chunks.some((c) => c.state === "uploading");
    setStep("step-save", saving ? "active" : "done");
    setStep("step-transcribe", saving ? "waiting" : transcribing ? "active" : "done");
    // One server request fixes spellings, names speakers and writes the notes.
    setStep("step-names", analyzing ? "active" : saving || transcribing ? "waiting" : "done");
    setStep("step-summary", analyzing ? "active" : "waiting");
    const tick = () => {
        // Transcription fills the ring to 70%; writing the notes (usually 5-40 s) eases it towards 99%.
        const waited = analyzing && session.analyzingSince ? (Date.now() - session.analyzingSince) / 1000 : 0;
        const ratio = analyzing
            ? 0.7 + 0.29 * (1 - Math.exp(-waited / 25))
            : 0.7 * (chunks.length ? finished / chunks.length : 0);
        $("ring-bar").style.strokeDashoffset = String(RING_LENGTH * (1 - ratio));
        $("ring-text").textContent = `${Math.round(ratio * 100)}%`;
        $("processing-hint").textContent = !analyzing
            ? "You can close this window. MeetMind keeps working."
            : waited < 60
                ? `Writing your notes… ${Math.round(waited)} s. You can close this window.`
                : `Still writing (${Math.floor(waited / 60)} min ${Math.round(waited % 60)} s). The AI service is slow right now; MeetMind tries a backup model.`;
    };
    tick();
    return tick;
}
function setStep(id, value) {
    $(id).dataset.state = value;
}
export function sessionResults(session, displayName) {
    const report = session.report;
    const failed = session.chunks.filter((c) => c.state === "failed");
    const noSpeech = session.segments.length === 0;
    const shots = session.screenshots.length;
    return {
        title: report?.title || session.title,
        durationSeconds: ((session.endedAt ?? Date.now()) - session.startedAt) / 1000,
        people: participantsOf(session, displayName).length,
        overview: report ? report.overview : null,
        tasks: report?.action_items ?? [],
        corrections: report?.corrections ?? 0,
        engine: session.engine ?? "",
        failed: failed.length
            ? {
                count: failed.length,
                message: `${failed.length} audio piece${failed.length > 1 ? "s were" : " was"} not transcribed. ${friendlyUploadError(failed[0]?.error)}`,
            }
            : null,
        reportProblem: noSpeech
            ? "No speech was transcribed, so there are no notes to create."
            : session.reportError
                ? "The summary could not be created. Your transcript is safe."
                : null,
        canAnalyzeAgain: !noSpeech && Boolean(session.reportError),
        transcriptOnly: !noSpeech,
        videoNote: session.mode !== "audio_video"
            ? null
            : session.videoFile
                ? `Video saved to Downloads/${session.videoFile}. ${shots} screenshot${shots === 1 ? " is" : "s are"} in the Word report.`
                : "The video was not saved. See the note below.",
        quality: report?.quality ?? null,
        canRenotes: false, // browser recordings use "Try again" (the audio is kept in this browser)
    };
}
/** Turns server errors into a next step people can act on. */
function friendlyUploadError(error = "") {
    if (/daily .*limit/i.test(error))
        return "Today's transcription limit is used up. Retry tomorrow.";
    if (/token/i.test(error))
        return "The app token was rejected. Ask your admin to check config.js.";
    if (/network|reach|fetch/i.test(error))
        return "The connection dropped. The audio is saved, so click Retry.";
    return "The server was busy. The audio is saved, so click Retry.";
}
