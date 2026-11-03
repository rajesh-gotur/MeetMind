/** Reads config.js and the user's saved preferences (not usable from the offscreen document). */
import { MEETMIND_CONFIG } from "../config.js";
export const CONFIG = MEETMIND_CONFIG;
export const SESSION_KEY = "meetmind.session";
const PREFERENCES_KEY = "meetmind.preferences";
const CLIENT_ID_KEY = "meetmind.clientId";
const TOKEN_PLACEHOLDER = "PASTE_THE_APP_TOKEN_HERE";
export function serverUrl() {
    return CONFIG.server.url.trim().replace(/\/+$/, "");
}
/** Mistakes in config.js that an admin must fix before anyone can record. */
export function configProblems() {
    const problems = [];
    try {
        const url = new URL(serverUrl());
        if (!/^https?:$/.test(url.protocol))
            problems.push("server.url must start with http:// or https://");
    }
    catch {
        problems.push("server.url in config.js is not a valid address.");
    }
    const token = CONFIG.server.appToken.trim();
    if (!token || token === TOKEN_PLACEHOLDER)
        problems.push("server.appToken in config.js is not set.");
    return problems;
}
export async function getPreferences() {
    const stored = await chrome.storage.local.get(PREFERENCES_KEY);
    const defaults = {
        displayName: "",
        language: CONFIG.recording.defaultLanguage,
        includeMic: CONFIG.recording.includeMicByDefault,
        setupComplete: false,
        startMode: "bot",
    };
    return { ...defaults, ...stored[PREFERENCES_KEY] };
}
export async function savePreferences(update) {
    const next = { ...(await getPreferences()), ...update };
    next.displayName = next.displayName.trim();
    await chrome.storage.local.set({ [PREFERENCES_KEY]: next });
    return next;
}
/** Anonymous per-install ID used by the backend's fair-use limit. */
export async function getClientId() {
    const stored = await chrome.storage.local.get(CLIENT_ID_KEY);
    const existing = stored[CLIENT_ID_KEY];
    if (existing)
        return existing;
    const created = crypto.randomUUID();
    await chrome.storage.local.set({ [CLIENT_ID_KEY]: created });
    return created;
}
export async function backendConfig() {
    return { url: serverUrl(), token: CONFIG.server.appToken.trim(), clientId: await getClientId() };
}
export async function loadSession() {
    const stored = await chrome.storage.local.get(SESSION_KEY);
    return stored[SESSION_KEY] ?? null;
}
export async function saveSession(session) {
    if (session)
        await chrome.storage.local.set({ [SESSION_KEY]: session });
    else
        await chrome.storage.local.remove(SESSION_KEY);
}
export async function microphoneAllowed() {
    try {
        return (await navigator.permissions.query({ name: "microphone" })).state === "granted";
    }
    catch {
        return false;
    }
}
