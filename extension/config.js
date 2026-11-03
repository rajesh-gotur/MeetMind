/*
 * =====================================================================
 *  MeetMind extension - CONFIG
 *  This is the only file you edit before sharing the extension.
 *
 *  1. server.url       -> your backend address (Render URL or http://localhost:8000)
 *  2. server.appToken  -> the SAME value as server.app_token in backend/config.yaml
 *                         (or APP_TOKEN on Render)
 *
 *  After editing, open chrome://extensions and click the reload icon on MeetMind.
 * =====================================================================
 */

export const MEETMIND_CONFIG = Object.freeze({
  server: {
    url: "http://localhost:8000",
    appToken: "",
  },

  recording: {
    // Audio is uploaded in pieces of this many seconds while you talk.
    // Your mic and the call audio are recorded as two separate pieces each time.
    // 900 (15 minutes) keeps each piece about 3.6 MB, far under Groq's 25 MB limit,
    // and long pieces help the speech engine keep voices apart.
    chunkSeconds: 900,
    // 32 kbps Opus is clear for speech and keeps uploads small.
    audioBitsPerSecond: 32000,
    includeMicByDefault: true,
    // While you are muted in Meet, your mic track records silence.
    followMeetMute: true,
    // "auto" works for English and Hinglish. Other choices: "en", "hi".
    defaultLanguage: "auto",
    // How many times one audio piece is re-sent if the server is busy or asleep.
    uploadAttempts: 5,
  },

  video: {
    // "Audio + Video" mode: the Meet tab is also recorded and saved to your Downloads.
    // 1.5 Mbps at up to 15 frames per second keeps a 1-hour meeting near 700 MB.
    videoBitsPerSecond: 1500000,
    maxFrameRate: 15,
    maxWidth: 1920,
  },

  screenshots: {
    // A screenshot is taken whenever the shared screen changes (for example a new slide).
    checkEverySeconds: 2,
    // Share of the screen that must change (0.12 = 12%) before a new screenshot is kept.
    changeThreshold: 0.12,
    // Never keep two screenshots closer together than this.
    minGapSeconds: 8,
    maxPerMeeting: 60,
    // Saved width in pixels and JPEG quality (0-1).
    width: 1280,
    quality: 0.82,
  },

  report: {
    // Create the summary, MOM, Word report and Excel tracker automatically after Stop.
    autoGenerate: true,
  },

  captions: {
    // MeetMind never turns Meet captions on by itself (false). If YOU turn them on,
    // MeetMind reads them as an extra hint for who is speaking. Set to true to turn them on automatically.
    autoEnable: false,
    // Where Meet shows captions. Only change these if Google redesigns Meet.
    regionSelector: '[role="region"][aria-label*="caption" i]',
    turnOnButtonLabel: "turn on captions",
  },

  participants: {
    // How MeetMind finds the names of the people in the call. Only change these
    // if Google redesigns Meet and the "People in this call" list stays empty.
    tileSelector: "[data-participant-id]",
    nameSelector: ".notranslate",
    peopleListSelector: '[role="list"] [role="listitem"][aria-label]',
    micButtonSelector: 'button[aria-label*="microphone" i]',
  },

  downloads: {
    // Sub-folder inside your Downloads folder.
    folder: "MeetMind",
  },
});
