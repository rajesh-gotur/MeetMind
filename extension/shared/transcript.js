/**
 * Builds the speaker-labelled transcript from the two recordings.
 *
 * - Mic track: always you (your own microphone).
 * - Tab track: the other people. Each WORD gets a speaker, so a phrase is split
 *   exactly where the voice changes. The name comes from, in order:
 *     1. Meet captions, if you turned them on (caption speaker at that moment),
 *     2. the participant list, when only one other person is in the call,
 *     3. the speech engine's voice number ("Speaker 2", "Speaker 3"); the server
 *        later replaces these with real names when the conversation makes it clear.
 * - Echo: if your mic picked up the call from your speakers, those words are
 *   already on the tab track, so the mic copy is dropped.
 */
/** Meet captions appear slightly after speech; widen each caption window a little. */
const CAPTION_PAD_SECONDS = 1.5;
const NEAREST_LIMIT_SECONDS = 5;
/** Consecutive phrases from one speaker closer than this become one turn. */
const MERGE_GAP_SECONDS = 2;
/** Share of a mic phrase's words also heard on the call track for it to count as echo. */
const ECHO_SHARE = 0.6;
export const UNKNOWN_SPEAKER = "Unknown speaker";
const REMOTE = "remote";
/* ---------- names ---------- */
function same(a, b) {
    return a.trim().toLowerCase() === b.trim().toLowerCase();
}
/** Your name in the notes: setup name, else the name Meet shows for you, else "You". */
export function selfLabel(session, displayName) {
    return displayName.trim() || session.selfName?.trim() || "You";
}
function isSelf(name, session, displayName) {
    return /^you$/i.test(name.trim()) || same(name, selfLabel(session, displayName)) || same(name, session.selfName ?? "");
}
/** Everyone else in the call: participant tiles and People list, plus caption speakers. */
export function remotePeople(session, displayName) {
    const names = [...(session.roster ?? []), ...session.captions.map((c) => c.speaker)];
    const unique = new Map();
    for (const raw of names) {
        const name = raw.trim();
        if (name && !isSelf(name, session, displayName) && !unique.has(name.toLowerCase()))
            unique.set(name.toLowerCase(), name);
    }
    return [...unique.values()].sort((a, b) => a.localeCompare(b));
}
export function participantsOf(session, displayName) {
    if (session.participants?.length)
        return session.participants;
    const self = selfLabel(session, displayName);
    const spoke = session.segments.some((s) => s.track === "mic") || session.status === "recording";
    return [...(spoke ? [self] : []), ...remotePeople(session, displayName)];
}
/* ---------- speaker per word ---------- */
function captionAt(time, captions) {
    let best = null;
    let bestGap = Number.POSITIVE_INFINITY;
    for (const caption of captions) {
        const start = caption.start - CAPTION_PAD_SECONDS;
        const end = caption.end + CAPTION_PAD_SECONDS;
        const gap = time < start ? start - time : time > end ? time - end : 0;
        const core = time < caption.start ? caption.start - time : time > caption.end ? time - caption.end : 0;
        // Inside a window wins (inside the unpadded part best); between windows, the nearest one within the limit.
        const score = gap === 0 ? -1 + core / 100 : gap;
        if (score < bestGap && gap <= NEAREST_LIMIT_SECONDS) {
            bestGap = score;
            best = caption;
        }
    }
    return best;
}
/** Display text per word: keeps the punctuated phrase text when it lines up word for word. */
function wordTexts(segment, words) {
    const tokens = segment.text.split(/\s+/).filter(Boolean);
    return tokens.length === words.length ? tokens : words.map((w) => w.text);
}
/** A single word between two runs of the same other voice is diarization jitter. */
function smooth(labels) {
    const out = [...labels];
    for (let i = 1; i < out.length - 1; i++) {
        if (out[i] !== out[i - 1] && out[i - 1] === out[i + 1])
            out[i] = out[i - 1];
    }
    return out;
}
function tabPieces(segment, captions) {
    const voice = (n) => (n === null || n === undefined ? REMOTE : `c:${segment.chunk ?? 0}:${n}`);
    const words = segment.words ?? [];
    if (!words.length) {
        const caption = captions.length ? captionAt((segment.start + segment.end) / 2, captions) : null;
        return [{ ...segment, speaker: caption?.speaker ?? voice(segment.speaker), track: "tab" }];
    }
    const labels = smooth(words.map((w) => {
        const caption = captions.length ? captionAt((w.start + w.end) / 2, captions) : null;
        return caption?.speaker ?? voice(w.speaker ?? segment.speaker);
    }));
    if (labels.every((l) => l === labels[0]))
        return [{ ...segment, speaker: labels[0], track: "tab" }];
    const texts = wordTexts(segment, words);
    const pieces = [];
    words.forEach((word, i) => {
        const last = pieces.at(-1);
        if (last && last.speaker === labels[i]) {
            last.end = word.end;
            last.text = `${last.text} ${texts[i]}`;
        }
        else {
            pieces.push({ start: word.start, end: word.end, speaker: labels[i], text: texts[i] ?? word.text, track: "tab" });
        }
    });
    return pieces;
}
/** Older sessions (one mixed track): the old caption-overlap rule. */
function mixedPiece(segment, captions, session, displayName) {
    const caption = captionAt((segment.start + segment.end) / 2, captions);
    const name = caption ? (isSelf(caption.speaker, session, displayName) ? selfLabel(session, displayName) : caption.speaker) : UNKNOWN_SPEAKER;
    return { ...segment, speaker: name, track: "mix" };
}
/* ---------- echo ---------- */
function tokens(text) {
    return text.toLowerCase().match(/[\p{L}\p{N}']+/gu) ?? [];
}
function dropEcho(pieces) {
    const tab = pieces.filter((p) => p.track === "tab");
    return pieces.filter((piece) => {
        if (piece.track !== "mic")
            return true;
        const mine = tokens(piece.text);
        if (!mine.length)
            return false;
        const heard = new Set(tab.filter((t) => t.start <= piece.end + 1 && t.end >= piece.start - 1).flatMap((t) => tokens(t.text)));
        if (!heard.size)
            return true;
        const shared = mine.filter((w) => heard.has(w)).length / mine.length;
        return shared < ECHO_SHARE;
    });
}
/* ---------- building ---------- */
function mergeTurns(pieces) {
    const turns = [];
    for (const piece of [...pieces].sort((a, b) => a.start - b.start)) {
        const last = turns.at(-1);
        if (last && last.speaker === piece.speaker && piece.start - last.end < MERGE_GAP_SECONDS) {
            last.end = Math.max(last.end, piece.end);
            last.text = `${last.text} ${piece.text}`;
        }
        else {
            turns.push({ start: piece.start, end: piece.end, speaker: piece.speaker, text: piece.text });
        }
    }
    return turns;
}
export function buildTranscript(session, displayName) {
    const self = selfLabel(session, displayName);
    const remote = remotePeople(session, displayName);
    // Your own captions ("You") never describe the call track.
    const remoteCaptions = session.captions.filter((c) => !isSelf(c.speaker, session, displayName));
    const pieces = dropEcho(session.segments.flatMap((segment) => {
        if (segment.track === "mic")
            return [{ ...segment, speaker: self, track: "mic" }];
        if (segment.track === "tab")
            return tabPieces(segment, remoteCaptions);
        return [mixedPiece(segment, session.captions, session, displayName)];
    }));
    // Turn internal keys into names or numbered placeholders, in order of first appearance.
    const labels = new Map();
    let next = 2; // "Speaker 1" would be you
    for (const piece of [...pieces].sort((a, b) => a.start - b.start)) {
        if (piece.speaker !== REMOTE && !piece.speaker.startsWith("c:"))
            continue;
        if (!labels.has(piece.speaker)) {
            if (remote.length === 1)
                labels.set(piece.speaker, remote[0]);
            else if (piece.speaker === REMOTE && remote.length > 1)
                labels.set(piece.speaker, "Participant");
            else
                labels.set(piece.speaker, `Speaker ${next++}`);
        }
        piece.speaker = labels.get(piece.speaker);
    }
    const turns = mergeTurns(pieces).map((t) => ({
        start: Math.round(t.start * 10) / 10,
        end: Math.round(t.end * 10) / 10,
        speaker: t.speaker,
        text: t.text.trim(),
    }));
    const unresolved = [...new Set(labels.values())].filter((l) => /^Speaker \d+$/.test(l));
    const spoke = new Set(turns.map((t) => t.speaker));
    const participants = [...new Set([...(spoke.has(self) ? [self] : []), ...remote, ...turns.map((t) => t.speaker)])].filter((name) => name !== UNKNOWN_SPEAKER && name !== "Participant");
    return { turns, participants, unresolved };
}
/** The transcript to show and export: the server-corrected one when the notes exist. */
export function finalTurns(session, displayName) {
    return session.turns?.length ? session.turns : buildTranscript(session, displayName).turns;
}
/* ---------- formatting ---------- */
export function formatClock(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}
export function buildTranscriptText(session, displayName) {
    const turns = finalTurns(session, displayName);
    const started = new Date(session.startedAt);
    const duration = session.endedAt ? (session.endedAt - session.startedAt) / 1000 : 0;
    const failed = session.chunks.filter((c) => c.state === "failed").length;
    const header = [
        "MeetMind transcript",
        `Meeting: ${session.title}`,
        `Meeting code: ${session.meetingCode}`,
        `Date: ${started.toLocaleString()}`,
        `Duration: ${formatClock(duration)}`,
        `Participants: ${participantsOf(session, displayName).join(", ") || "Not detected"}`,
        failed ? `Note: ${failed} audio piece(s) could not be transcribed.` : "",
    ].filter(Boolean);
    const body = turns.map((t) => `[${formatClock(t.start)}] ${t.speaker}: ${t.text}`);
    return [...header, "", ...body, ""].join("\n");
}
export function fileSlug(session) {
    const date = new Date(session.startedAt).toISOString().slice(0, 10);
    const base = (session.title || session.meetingCode || "meeting")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 40);
    return `${date}-${base || "meeting"}`;
}
/** Epoch ms -> "2026-10-29T10:00:00+05:30" in the user's own time zone. */
export function isoWithOffset(ms) {
    const date = new Date(ms);
    const pad = (n) => String(Math.trunc(Math.abs(n))).padStart(2, "0");
    const offset = -date.getTimezoneOffset();
    const sign = offset >= 0 ? "+" : "-";
    return (`${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
        `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
        `${sign}${pad(offset / 60)}:${pad(offset % 60)}`);
}
function meetingInfo(session, displayName) {
    const ended = session.endedAt ?? Date.now();
    return {
        title: session.title,
        code: session.meetingCode,
        started_at: isoWithOffset(session.startedAt),
        duration_seconds: Math.max(0, Math.round((ended - session.startedAt) / 1000)),
        recorded_by: selfLabel(session, displayName),
    };
}
/** What the server needs to correct the transcript and write the notes. */
export function buildAnalyzePayload(session, displayName) {
    const built = buildTranscript(session, displayName);
    return {
        meeting: meetingInfo(session, displayName),
        participants: built.participants,
        turns: built.turns,
        unresolved_speakers: built.unresolved,
    };
}
/** What the server needs to build the files (always the final transcript). */
export function buildExportPayload(session, displayName) {
    return {
        meeting: meetingInfo(session, displayName),
        participants: participantsOf(session, displayName),
        turns: finalTurns(session, displayName),
    };
}
