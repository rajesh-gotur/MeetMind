/** Typed messages exchanged between popup, service worker, offscreen document and content script. */

import type { BackendConfig, CaptionEntry, Language, RecordingMode, Segment, Track } from "./types.js";

/* Popup / setup page -> service worker */
export interface StartRequest {
  type: "START";
  tabId: number;
  streamId: string;
  mode: RecordingMode;
  language: Language;
  includeMic: boolean;
}
export interface StopRequest {
  type: "STOP";
}
export interface ResetRequest {
  type: "RESET";
}
export interface RetryRequest {
  type: "RETRY_FAILED";
}
export interface AnalyzeAgainRequest {
  type: "ANALYZE";
}
/** Sent by the popup while notes are being written, in case the worker was restarted meanwhile. */
export interface ResumeAnalysisRequest {
  type: "RESUME_ANALYSIS";
}

/* Service worker -> offscreen */
export interface OffscreenStart {
  target: "offscreen";
  type: "OFFSCREEN_START";
  sessionId: string;
  streamId: string;
  mode: RecordingMode;
  includeMic: boolean;
  /** You are muted in Meet right now. */
  micMuted: boolean;
  language: Language;
  backend: BackendConfig;
}
/** Follows your Meet mute button, so nothing is recorded from your mic while you are muted. */
export interface OffscreenMic {
  target: "offscreen";
  type: "OFFSCREEN_MIC";
  muted: boolean;
}
export interface OffscreenStop {
  target: "offscreen";
  type: "OFFSCREEN_STOP";
}
export interface OffscreenRetry {
  target: "offscreen";
  type: "OFFSCREEN_RETRY";
  sessionId: string;
  chunks: { index: number; offset: number; track: Track }[];
  language: Language;
  backend: BackendConfig;
}

/* Offscreen -> service worker */
export interface PromptRequest {
  type: "GET_PROMPT";
}
export interface ChunkStarted {
  type: "CHUNK_STARTED";
  index: number;
  offset: number;
  tracks: Track[];
}
export interface ChunkUploading {
  type: "CHUNK_UPLOADING";
  index: number;
  track: Track;
}
export interface ChunkResult {
  type: "CHUNK_RESULT";
  index: number;
  track: Track;
  segments: Segment[];
  engine?: string;
}
export interface ChunkFailed {
  type: "CHUNK_FAILED";
  index: number;
  track: Track;
  error: string;
}
export interface RecordingWarning {
  type: "RECORDING_WARNING";
  message: string;
}
export interface ScreenshotTaken {
  type: "SCREENSHOT_TAKEN";
  name: string;
  time: number;
}
/** The full video is ready as a blob URL owned by the offscreen document. */
export interface VideoReady {
  type: "VIDEO_READY";
  url: string;
  bytes: number;
}
export interface RecordingFinished {
  type: "RECORDING_FINISHED";
}
export interface RecordingError {
  type: "RECORDING_ERROR";
  error: string;
}

/* Offscreen -> popup (live meters; ignored by the service worker) */
export interface AudioLevel {
  target: "popup";
  type: "AUDIO_LEVEL";
  /** 0..1, or -1 when that source is not being recorded. */
  tab: number;
  mic: number;
}

/* Service worker <-> content script */
export interface CaptionsStart {
  type: "CAPTIONS_START";
  startedAt: number;
  autoEnable: boolean;
  regionSelector: string;
  turnOnButtonLabel: string;
  tileSelector: string;
  nameSelector: string;
  peopleListSelector: string;
  micButtonSelector: string;
}
export interface CaptionsStop {
  type: "CAPTIONS_STOP";
}
export interface CaptionsBatch {
  type: "CAPTIONS_BATCH";
  entries: CaptionEntry[];
  /** Everyone currently shown in the call (tiles and People list). */
  roster?: string[];
  selfName?: string;
  /** Meet's microphone button shows you as muted. */
  micMuted?: boolean;
}

/* Content script -> service worker: your own Meet tab joined or left a call */
export interface WatchOptionsRequest {
  type: "WATCH_OPTIONS";
}
export interface MeetJoined {
  type: "MEET_JOINED";
  code: string;
}
export interface MeetLeft {
  type: "MEET_LEFT";
  code: string;
}
/** What the content script needs to watch for "you left the call" (from config.js autoStop). */
export interface WatchOptions {
  enabled: boolean;
  leaveButtonSelector: string;
}

export type WorkerMessage =
  | StartRequest
  | StopRequest
  | ResetRequest
  | RetryRequest
  | AnalyzeAgainRequest
  | ResumeAnalysisRequest
  | PromptRequest
  | ChunkStarted
  | ChunkUploading
  | ChunkResult
  | ChunkFailed
  | RecordingWarning
  | ScreenshotTaken
  | VideoReady
  | RecordingFinished
  | RecordingError
  | CaptionsBatch
  | WatchOptionsRequest
  | MeetJoined
  | MeetLeft;

export type OffscreenMessage = OffscreenStart | OffscreenStop | OffscreenRetry | OffscreenMic;

export interface Ack {
  ok: boolean;
  error?: string;
  startedAt?: number;
  /** GET_PROMPT: names the speech engine should spell exactly. */
  names?: string[];
  entries?: CaptionEntry[];
  roster?: string[];
  selfName?: string;
  /** WATCH_OPTIONS: how to notice that you left the call. */
  watch?: WatchOptions;
}
