/**
 * Video mode: records the Meet tab (video + the same mixed audio) to one file,
 * and keeps a screenshot whenever the screen really changes (a new slide, a
 * different shared window). Settings come from config.js (video, screenshots).
 */

import { MEETMIND_CONFIG as CONFIG } from "../config.js";

const SAMPLE_WIDTH = 64;
const SAMPLE_HEIGHT = 36;
const PIXEL_CHANGE = 28; // colour difference (0-255, any channel) that counts as "changed"
const STABLE_LIMIT = 0.02; // the screen is settled when under 2% changed since the last check
const VIDEO_TYPES = ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"];

/* ---------- full recording ---------- */

export class VideoRecording {
  private readonly recorder: MediaRecorder;
  private readonly parts: Blob[] = [];
  readonly mimeType: string;

  constructor(videoTrack: MediaStreamTrack, audioTrack: MediaStreamTrack | undefined) {
    this.mimeType = VIDEO_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) ?? "video/webm";
    const stream = new MediaStream(audioTrack ? [videoTrack, audioTrack] : [videoTrack]);
    this.recorder = new MediaRecorder(stream, {
      mimeType: this.mimeType,
      videoBitsPerSecond: CONFIG.video.videoBitsPerSecond,
      audioBitsPerSecond: 64_000,
    });
    this.recorder.ondataavailable = (event) => {
      if (event.data.size > 0) this.parts.push(event.data);
    };
    // Small slices keep memory steady; Chrome moves large blobs to disk by itself.
    this.recorder.start(10_000);
  }

  /** Stops recording and returns the whole video, or null if nothing was recorded. */
  async finish(): Promise<Blob | null> {
    if (this.recorder.state !== "inactive") {
      await new Promise<void>((resolve) => {
        this.recorder.addEventListener("stop", () => resolve(), { once: true });
        this.recorder.stop();
      });
    }
    return this.parts.length ? new Blob(this.parts, { type: "video/webm" }) : null;
  }
}

/* ---------- screenshots ---------- */

export interface Screenshot {
  /** 1, 2, 3... in the order the screen changes were seen. */
  number: number;
  /** Seconds from recording start. */
  time: number;
  blob: Blob;
}

export class ScreenshotWatcher {
  private readonly video = document.createElement("video");
  private readonly sample = document.createElement("canvas");
  private readonly sampleContext: CanvasRenderingContext2D;
  private timer: number | null = null;
  private previous: Uint8Array | null = null;
  private lastKept: Uint8Array | null = null;
  private lastKeptAt = Number.NEGATIVE_INFINITY;
  private kept = 0;
  private pending: Promise<void> = Promise.resolve();

  constructor(
    videoTrack: MediaStreamTrack,
    private readonly startedAtMs: number,
    private readonly onShot: (shot: Screenshot) => Promise<void>,
  ) {
    this.sample.width = SAMPLE_WIDTH;
    this.sample.height = SAMPLE_HEIGHT;
    const context = this.sample.getContext("2d", { willReadFrequently: true });
    if (!context) throw new Error("Canvas is not available.");
    this.sampleContext = context;
    this.video.muted = true;
    this.video.srcObject = new MediaStream([videoTrack]);
    void this.video.play();
    this.timer = self.setInterval(() => this.check(), CONFIG.screenshots.checkEverySeconds * 1000);
  }

  get count(): number {
    return this.kept;
  }

  async stop(): Promise<void> {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    await this.pending;
    this.video.srcObject = null;
  }

  private check(): void {
    if (this.video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !this.video.videoWidth) return;
    if (this.kept >= CONFIG.screenshots.maxPerMeeting) return;

    this.sampleContext.drawImage(this.video, 0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT);
    const current = rgb(this.sampleContext.getImageData(0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT).data);
    const settled = this.previous !== null && changed(this.previous, current) < STABLE_LIMIT;
    this.previous = current;
    if (!settled) return; // wait until slides finish animating or scrolling

    const time = (Date.now() - this.startedAtMs) / 1000;
    const isNew = this.lastKept === null || changed(this.lastKept, current) >= CONFIG.screenshots.changeThreshold;
    if (!isNew || time - this.lastKeptAt < CONFIG.screenshots.minGapSeconds) return;

    this.lastKept = current;
    this.lastKeptAt = time;
    this.kept += 1;
    const number = this.kept;
    this.pending = this.pending.then(() => this.capture(time, number)).catch(() => undefined);
  }

  private async capture(time: number, number: number): Promise<void> {
    const width = Math.min(CONFIG.screenshots.width, this.video.videoWidth);
    const height = Math.round((width / this.video.videoWidth) * this.video.videoHeight);
    const canvas = new OffscreenCanvas(width, height);
    const context = canvas.getContext("2d");
    if (!context) return;
    context.drawImage(this.video, 0, 0, width, height);
    const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: CONFIG.screenshots.quality });
    await this.onShot({ number, time: Math.round(time * 10) / 10, blob });
  }
}

/** RGBA pixels -> packed RGB (alpha dropped). Colour is kept: two slides can share a brightness. */
function rgb(rgba: Uint8ClampedArray): Uint8Array {
  const out = new Uint8Array((rgba.length / 4) * 3);
  for (let p = 0, o = 0; p < rgba.length; p += 4, o += 3) {
    out[o] = rgba[p]!;
    out[o + 1] = rgba[p + 1]!;
    out[o + 2] = rgba[p + 2]!;
  }
  return out;
}

/** Share of pixels (0-1) where any colour channel changed noticeably between two samples. */
function changed(a: Uint8Array, b: Uint8Array): number {
  let count = 0;
  for (let i = 0; i < a.length; i += 3) {
    const diff = Math.max(Math.abs(a[i]! - b[i]!), Math.abs(a[i + 1]! - b[i + 1]!), Math.abs(a[i + 2]! - b[i + 2]!));
    if (diff > PIXEL_CHANGE) count++;
  }
  return count / (a.length / 3);
}
