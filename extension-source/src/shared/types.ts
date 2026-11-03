/** Shared domain types for every extension context. */

export type RecordingMode = "audio" | "audio_video";

export type Language = "auto" | "en" | "hi";

/** recording -> stopping (last uploads) -> analyzing (summary) -> done */
export type SessionStatus = "idle" | "starting" | "recording" | "stopping" | "analyzing" | "done" | "error";

/** "mic": only your own voice. "tab": everyone else in the call. */
export type Track = "mic" | "tab";

export interface Word {
  text: string;
  /** Seconds from recording start. */
  start: number;
  end: number;
  /** Voice number from the speech engine, only meaningful inside one audio piece. */
  speaker?: number | null;
  /** 0-1: how sure MeetMind is about this word after comparing every speech engine. */
  confidence?: number | null;
  /** Other spellings the engines heard (doubtful words only). */
  alternatives?: string[];
}

export interface Segment {
  /** Seconds from recording start. */
  start: number;
  end: number;
  text: string;
  /** Voice number from the speech engine (Deepgram), only meaningful inside one audio piece. */
  speaker?: number | null;
  words?: Word[];
  /** Added by the extension: which recording and which audio piece this came from. */
  track?: Track;
  chunk?: number;
}

export interface CaptionEntry {
  id: string;
  speaker: string;
  text: string;
  /** Seconds from recording start. */
  start: number;
  end: number;
}

/** recording -> uploading -> done | failed */
export type ChunkState = "recording" | "uploading" | "done" | "failed";

export interface ChunkStatus {
  index: number;
  offset: number;
  track: Track;
  state: ChunkState;
  error?: string;
}

export interface MeetingSession {
  id: string;
  tabId: number;
  meetingCode: string;
  title: string;
  mode: RecordingMode;
  language: Language;
  /** Epoch ms when audio recording actually started. */
  startedAt: number;
  endedAt?: number;
  status: SessionStatus;
  error?: string;
  warnings: string[];
  chunks: ChunkStatus[];
  segments: Segment[];
  captions: CaptionEntry[];
  /** Names seen on Meet's participant tiles and People list during the call. */
  roster: string[];
  /** Your own name as Meet shows it, if it could be read. */
  selfName?: string;
  /** Speech engine that transcribed the audio, as reported by the server. */
  engine?: string;
  /** Final transcript after the server fixed spellings and named the speakers. */
  turns?: Turn[];
  participants?: string[];
  /** Video mode: screenshots kept in IndexedDB, named shot-001.jpg ... */
  screenshots: ScreenshotMeta[];
  /** Video mode: the recording saved to Downloads. */
  videoFile?: string;
  report?: MeetingReport;
  reportError?: string;
  /** Epoch ms when writing the notes started (drives the progress shown in the popup). */
  analyzingSince?: number;
}

export interface ScreenshotMeta {
  name: string;
  /** Seconds from recording start. */
  time: number;
}

/* ---- Meeting report (mirrors backend/app/schemas.py MeetingReport) ---- */

export type Priority = "High" | "Medium" | "Low";

export interface ActionItem {
  id: string;
  task: string;
  owner: string;
  assigned_by: string;
  due_date: string | null;
  due_text: string;
  priority: Priority;
  time: number;
  evidence: string;
  verified: boolean;
}

/** How much MeetMind trusts the notes (0-100, estimated). Mirrors QualityScore in backend/app/schemas.py. */
export interface QualityScore {
  overall: number;
  capture: number;
  words: number;
  speakers: number;
  notes: number;
  ears: string[];
  fallbacks: string[];
  doubtful_words: number;
  corrected_words: number;
}

export interface MeetingReport {
  title: string;
  overview: string;
  key_points: string[];
  topics: { title: string; summary: string; start: number; end: number; speakers: string[] }[];
  decisions: { text: string; made_by: string; time: number | null }[];
  action_items: ActionItem[];
  blockers: { text: string; raised_by: string; time: number | null }[];
  next_steps: string[];
  next_meeting: string;
  generated_by: string;
  /** Misheard words fixed in the transcript. */
  corrections?: number;
  notes: string[];
  quality?: QualityScore | null;
}

export interface Turn {
  start: number;
  end: number;
  speaker: string;
  text: string;
}

export interface MeetingInfo {
  title: string;
  code: string;
  /** ISO 8601 with the user's UTC offset, e.g. 2026-10-29T10:00:00+05:30 */
  started_at: string;
  duration_seconds: number;
  recorded_by: string;
}

export type ExportKind = "zip" | "docx" | "xlsx" | "mom" | "readme" | "transcript";

/* ---- The MeetMind bot (mirrors backend/app/bot/state.py) ---- */

export type BotPhase = "idle" | "starting" | "joining" | "waiting" | "recording" | "finishing" | "writing" | "done" | "error";

export interface BotReport {
  title: string;
  overview: string;
  action_items: ActionItem[];
  decisions: number;
  corrections: number;
  people: number;
  duration: number;
  generated_by: string;
  quality?: QualityScore | null;
}

/** The bot's live self-check (every 5 s while it records). */
export interface BotHealth {
  score: number;
  in_call: boolean;
  sound: boolean;
  captions: boolean;
}

export interface BotStatus {
  phase: BotPhase;
  meeting_url: string;
  meeting_code: string;
  title: string;
  bot_name: string;
  started_at: number | null;
  ended_at: number | null;
  now: number;
  captions: { speaker: string; text: string; t: number }[];
  captions_final: boolean;
  captions_on: boolean;
  people: { name: string; words: number; speaking?: boolean; present?: boolean }[];
  tracks: number;
  pieces: { index: number; state: string; note?: string; error?: string }[];
  engine: string;
  /** Saved meeting folder; "Create notes again" works when it is set. */
  meeting_id?: string;
  health?: BotHealth | Record<string, never>;
  report: BotReport | null;
  error: string;
  warnings: string[];
  /** False when this server cannot run the bot; the reason says why. */
  available: boolean;
  unavailable_reason: string;
}

/** How the user records: send the MeetMind bot, or record in this browser. */
export type StartMode = "bot" | "record";

/** Per-user choices saved in the browser (server details live in config.js). */
export interface Preferences {
  /** Your Meet display name; replaces "You" in captions. */
  displayName: string;
  language: Language;
  includeMic: boolean;
  setupComplete: boolean;
  startMode: StartMode;
}

export interface BackendConfig {
  url: string;
  token: string;
  clientId: string;
}
