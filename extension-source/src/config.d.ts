/** Types for static/config.js, which stays plain JavaScript so it can be edited after building. */

import type { Language } from "./shared/types.js";

export interface MeetMindConfig {
  readonly server: { readonly url: string; readonly appToken: string };
  readonly recording: {
    readonly chunkSeconds: number;
    readonly audioBitsPerSecond: number;
    readonly includeMicByDefault: boolean;
    readonly followMeetMute: boolean;
    readonly defaultLanguage: Language;
    readonly uploadAttempts: number;
  };
  readonly video: {
    readonly videoBitsPerSecond: number;
    readonly maxFrameRate: number;
    readonly maxWidth: number;
  };
  readonly screenshots: {
    readonly checkEverySeconds: number;
    readonly changeThreshold: number;
    readonly minGapSeconds: number;
    readonly maxPerMeeting: number;
    readonly width: number;
    readonly quality: number;
  };
  readonly autoStop: {
    readonly recordHere: boolean;
    readonly botWhenILeave: boolean;
    readonly leaveButtonSelector: string;
  };
  readonly report: { readonly autoGenerate: boolean };
  readonly captions: {
    readonly autoEnable: boolean;
    readonly regionSelector: string;
    readonly turnOnButtonLabel: string;
  };
  readonly participants: {
    readonly tileSelector: string;
    readonly nameSelector: string;
    readonly peopleListSelector: string;
    readonly micButtonSelector: string;
  };
  readonly downloads: { readonly folder: string };
}

export declare const MEETMIND_CONFIG: MeetMindConfig;
