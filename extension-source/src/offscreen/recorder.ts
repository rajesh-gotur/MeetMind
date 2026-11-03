/**
 * Offscreen recorder: captures the Meet tab audio and your microphone as two
 * SEPARATE recordings, keeps the call audible, and uploads self-contained audio
 * pieces for transcription.
 *
 * Why two recordings: everything on the mic track is you, and everything on the
 * tab track is the other people (Meet never plays your own voice back). This
 * makes "who said what" exact for you, and leaves only the other voices to tell
 * apart. In "Audio + Video" mode it also records the tab video and takes screenshots.
 *
 * Offscreen documents can only use chrome.runtime (no chrome.storage), so
 * per-session details arrive in messages; fixed settings come from config.js.
 */

import { MEETMIND_CONFIG as CONFIG } from "../config.js";
import { uploadChunk } from "../shared/api.js";
import { deleteChunk, getChunk, putChunk, putShot } from "../shared/chunk-store.js";
import type { Ack, AudioLevel, OffscreenMessage, OffscreenRetry, OffscreenStart, WorkerMessage } from "../shared/messages.js";
import type { BackendConfig, Language, Track } from "../shared/types.js";
import { ScreenshotWatcher, VideoRecording } from "./video.js";

const MIME_TYPE = "audio/webm;codecs=opus";
const MIN_CHUNK_BYTES = 2_000;
const LEVEL_INTERVAL_MS = 200;
/** A piece whose loudest moment stays under this level is silence: nothing to transcribe. */
const SILENCE_PEAK = 0.06;

interface Recording {
  sessionId: string;
  language: Language;
  backend: BackendConfig;
  tabStream: MediaStream;
  micStream: MediaStream | null;
  audioContext: AudioContext;
  destinations: Map<Track, MediaStreamAudioDestinationNode>;
  mix: MediaStreamAudioDestinationNode;
  meters: Map<Track, AnalyserNode>;
  micGain: GainNode | null;
  startedAtMs: number;
  recorders: Map<Track, MediaRecorder>;
  /** Loudest level seen per track in the piece being recorded now. */
  peaks: Map<Track, number>;
  rotateTimer: number | null;
  levelTimer: number | null;
  nextIndex: number;
  uploads: Promise<void>[];
  stopping: Promise<void> | null;
  video: VideoRecording | null;
  screenshots: ScreenshotWatcher | null;
}

let recording: Recording | null = null;

chrome.runtime.onMessage.addListener((message: OffscreenMessage, _sender, sendResponse) => {
  if (message.target !== "offscreen") return false;
  let work: Promise<Ack>;
  if (message.type === "OFFSCREEN_START") work = start(message);
  else if (message.type === "OFFSCREEN_RETRY") work = retry(message);
  else if (message.type === "OFFSCREEN_MIC") work = Promise.resolve(setMicMuted(message.muted));
  else work = stop().then(() => ({ ok: true }));
  work.then(sendResponse).catch((error: unknown) => sendResponse({ ok: false, error: errorText(error) } satisfies Ack));
  return true;
});

/* ---------- recording ---------- */

async function start(message: OffscreenStart): Promise<Ack> {
  if (recording) return { ok: false, error: "A recording is already running." };
  if (!MediaRecorder.isTypeSupported(MIME_TYPE)) return { ok: false, error: "This browser cannot record Opus audio." };

  // One stream ID can be used once, so audio and (in video mode) video are captured together.
  const source = { chromeMediaSource: "tab", chromeMediaSourceId: message.streamId };
  const withVideo = message.mode === "audio_video";
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: source },
    video: withVideo
      ? {
          mandatory: {
            ...source,
            maxWidth: CONFIG.video.maxWidth,
            maxHeight: Math.round((CONFIG.video.maxWidth * 9) / 16),
            maxFrameRate: CONFIG.video.maxFrameRate,
          },
        }
      : false,
  } as unknown as MediaStreamConstraints);

  const audioContext = new AudioContext();
  const tabSource = audioContext.createMediaStreamSource(tabStream);
  // Capturing a tab mutes it for the user; route it back to the speakers.
  tabSource.connect(audioContext.destination);

  const mix = audioContext.createMediaStreamDestination(); // only for the video file
  const tabOnly = audioContext.createMediaStreamDestination();
  tabSource.connect(mix);
  tabSource.connect(tabOnly);
  const destinations = new Map<Track, MediaStreamAudioDestinationNode>([["tab", tabOnly]]);
  const meters = new Map<Track, AnalyserNode>([["tab", meter(audioContext, tabSource)]]);

  let micStream: MediaStream | null = null;
  let micGain: GainNode | null = null;
  if (message.includeMic) {
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        // Echo cancellation keeps the call's sound (from your speakers) off your mic track.
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
      const micSource = audioContext.createMediaStreamSource(micStream);
      micGain = audioContext.createGain(); // set to 0 while you are muted in Meet
      micSource.connect(micGain);
      const micOnly = audioContext.createMediaStreamDestination();
      micGain.connect(micOnly);
      micGain.connect(mix);
      destinations.set("mic", micOnly);
      meters.set("mic", meter(audioContext, micGain));
    } catch (error) {
      const blocked = error instanceof DOMException && error.name === "NotAllowedError";
      notify({
        type: "RECORDING_WARNING",
        message: blocked
          ? "Your microphone is not allowed, so your own voice is not recorded. Open MeetMind setup to allow it."
          : `Your microphone could not be opened (${errorText(error)}), so your own voice is not recorded.`,
      });
    }
  }

  const current: Recording = {
    sessionId: message.sessionId,
    language: message.language,
    backend: message.backend,
    tabStream,
    micStream,
    audioContext,
    destinations,
    mix,
    meters,
    micGain,
    startedAtMs: Date.now(),
    recorders: new Map(),
    peaks: new Map(),
    rotateTimer: null,
    levelTimer: null,
    nextIndex: 0,
    uploads: [],
    stopping: null,
    video: null,
    screenshots: null,
  };
  recording = current;
  if (message.micMuted) setMicMuted(true);

  const videoTrack = tabStream.getVideoTracks()[0];
  if (videoTrack) {
    current.video = new VideoRecording(videoTrack, mix.stream.getAudioTracks()[0]);
    current.screenshots = new ScreenshotWatcher(videoTrack, current.startedAtMs, async (shot) => {
      const name = `shot-${String(shot.number).padStart(3, "0")}.jpg`;
      await putShot({ sessionId: current.sessionId, name, time: shot.time, blob: shot.blob });
      notify({ type: "SCREENSHOT_TAKEN", name, time: shot.time });
    });
  }

  // The tab stream ends when the Meet tab closes or navigates away.
  tabStream.getAudioTracks()[0]?.addEventListener("ended", () => void stop());
  current.levelTimer = self.setInterval(() => sendLevels(current), LEVEL_INTERVAL_MS);

  startChunk(current);
  return { ok: true, startedAt: current.startedAtMs };
}

/** While you are muted in Meet, your mic track records silence (privacy, and no side talk in the notes). */
function setMicMuted(muted: boolean): Ack {
  const gain = recording?.micGain;
  if (gain && recording) gain.gain.setTargetAtTime(muted ? 0 : 1, recording.audioContext.currentTime, 0.05);
  return { ok: true };
}

/** Starts the next recorders before stopping the previous ones, so no audio is lost between pieces. */
function startChunk(current: Recording): void {
  const index = current.nextIndex++;
  const offset = (Date.now() - current.startedAtMs) / 1000;
  const previous = new Map(current.recorders);
  // Each piece has its own peak record; sendLevels writes into the newest one.
  const peaks = new Map<Track, number>();
  current.peaks = peaks;

  for (const [track, destination] of current.destinations) {
    const parts: Blob[] = [];
    const recorder = new MediaRecorder(destination.stream, {
      mimeType: MIME_TYPE,
      audioBitsPerSecond: CONFIG.recording.audioBitsPerSecond,
    });
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) parts.push(event.data);
    };
    recorder.onstop = () => {
      const blob = new Blob(parts, { type: MIME_TYPE });
      current.uploads.push(saveAndUpload(current, index, offset, track, blob, peaks.get(track) ?? 0));
    };
    recorder.start();
    current.recorders.set(track, recorder);
  }
  notify({ type: "CHUNK_STARTED", index, offset, tracks: [...current.destinations.keys()] });
  for (const recorder of previous.values()) if (recorder.state !== "inactive") recorder.stop();

  current.rotateTimer = self.setTimeout(() => {
    if (!current.stopping) startChunk(current);
  }, CONFIG.recording.chunkSeconds * 1000);
}

async function stop(): Promise<void> {
  const current = recording;
  if (!current) return;
  if (current.stopping) return current.stopping;

  current.stopping = (async () => {
    if (current.rotateTimer !== null) clearTimeout(current.rotateTimer);
    if (current.levelTimer !== null) clearInterval(current.levelTimer);
    await Promise.all(
      [...current.recorders.values()]
        .filter((recorder) => recorder.state !== "inactive")
        .map(
          (recorder) =>
            new Promise<void>((resolve) => {
              recorder.addEventListener("stop", () => resolve(), { once: true });
              recorder.stop();
            }),
        ),
    );
    await current.screenshots?.stop();
    const video = await current.video?.finish();
    for (const track of [...current.tabStream.getTracks(), ...(current.micStream?.getTracks() ?? [])]) track.stop();
    await current.audioContext.close();
    // Wait for the onstop handlers to queue the last uploads, then for every upload.
    await Promise.resolve();
    await Promise.allSettled(current.uploads);
    if (video) await handOverVideo(video);
    recording = null;
    notify({ type: "RECORDING_FINISHED" });
  })();
  return current.stopping;
}

/**
 * Only extension pages can save downloads, so the service worker saves the video
 * from a blob URL owned by this document. It answers once the file is on disk.
 */
async function handOverVideo(video: Blob): Promise<void> {
  const url = URL.createObjectURL(video);
  try {
    await chrome.runtime.sendMessage({ type: "VIDEO_READY", url, bytes: video.size } satisfies WorkerMessage);
  } catch {
    // The service worker reports the problem in the session warnings.
  } finally {
    URL.revokeObjectURL(url);
  }
}

/* ---------- uploads ---------- */

async function saveAndUpload(
  current: Recording,
  index: number,
  offset: number,
  track: Track,
  blob: Blob,
  peak: number,
): Promise<void> {
  // Silence (for example you were muted the whole time) costs credit and can make Whisper invent words.
  if (blob.size < MIN_CHUNK_BYTES || peak < SILENCE_PEAK) {
    notify({ type: "CHUNK_RESULT", index, track, segments: [] });
    return;
  }
  // Keep the audio until the server confirms it, so a failed piece can be retried later.
  try {
    await putChunk({ sessionId: current.sessionId, index, offset, track, blob });
  } catch {
    // Storage full or blocked: still try to upload.
  }
  await send(current.sessionId, current.language, current.backend, index, offset, track, blob);
}

async function send(
  sessionId: string,
  language: Language,
  backend: BackendConfig,
  index: number,
  offset: number,
  track: Track,
  blob: Blob,
): Promise<void> {
  notify({ type: "CHUNK_UPLOADING", index, track });
  try {
    const result = await uploadChunk(backend, {
      index,
      offset,
      track,
      blob,
      language,
      names: await currentNames(),
      attempts: CONFIG.recording.uploadAttempts,
    });
    await deleteChunk(sessionId, index, track).catch(() => undefined);
    notify({ type: "CHUNK_RESULT", index, track, segments: result.segments, engine: result.engine });
  } catch (error) {
    notify({ type: "CHUNK_FAILED", index, track, error: errorText(error) });
  }
}

/** Re-sends pieces that failed earlier, using the audio kept in IndexedDB. */
async function retry(message: OffscreenRetry): Promise<Ack> {
  for (const chunk of message.chunks) {
    const stored = await getChunk(message.sessionId, chunk.index, chunk.track);
    if (!stored) {
      notify({ type: "CHUNK_FAILED", index: chunk.index, track: chunk.track, error: "This audio piece is no longer saved." });
      continue;
    }
    await send(message.sessionId, message.language, message.backend, chunk.index, chunk.offset, chunk.track, stored.blob);
  }
  notify({ type: "RECORDING_FINISHED" });
  return { ok: true };
}

/** Asks the service worker for the names seen in the call, so the speech engine spells them right. */
async function currentNames(): Promise<string[]> {
  try {
    const ack = (await chrome.runtime.sendMessage({ type: "GET_PROMPT" } satisfies WorkerMessage)) as Ack | undefined;
    return ack?.names ?? [];
  } catch {
    return [];
  }
}

/* ---------- live levels for the popup ---------- */

function meter(context: AudioContext, source: AudioNode): AnalyserNode {
  const analyser = context.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  return analyser;
}

function level(analyser: AnalyserNode | undefined): number {
  if (!analyser) return -1; // not recording this source
  const samples = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(samples);
  let sum = 0;
  for (const sample of samples) sum += sample * sample;
  const rms = Math.sqrt(sum / samples.length);
  return Math.min(1, Math.sqrt(rms) * 1.8); // square root makes quiet speech visible on a 0..1 meter
}

function sendLevels(current: Recording): void {
  const tab = level(current.meters.get("tab"));
  const mic = level(current.meters.get("mic"));
  current.peaks.set("tab", Math.max(current.peaks.get("tab") ?? 0, tab));
  if (mic >= 0) current.peaks.set("mic", Math.max(current.peaks.get("mic") ?? 0, mic));
  const message: AudioLevel = { target: "popup", type: "AUDIO_LEVEL", tab, mic };
  chrome.runtime.sendMessage(message).catch(() => undefined); // popup closed: nothing listens
}

/* ---------- helpers ---------- */

function notify(message: WorkerMessage): void {
  chrome.runtime.sendMessage(message).catch(() => {
    // Service worker restarting; Chrome wakes it on the next message.
  });
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
