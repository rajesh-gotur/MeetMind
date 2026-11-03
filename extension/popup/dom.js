/** Small DOM and formatting helpers shared by the popup's screens. */
import { CONFIG } from "../shared/settings.js";
const AVATAR_TINTS = ["#CDEBDD", "#FCE6C2", "#F9D5D1", "#DCE8D2", "#EADFC8", "#D3E6E0"];
export function $(id) {
    const node = document.getElementById(id);
    if (!node)
        throw new Error(`Missing #${id}`);
    return node;
}
export function element(tag, text = "", className = "") {
    const node = document.createElement(tag);
    if (text)
        node.textContent = text;
    if (className)
        node.className = className;
    return node;
}
/** "Suresh Kumar (#:Mr.S_HåCK)" -> "SK": letters only, first and last word. */
export function initials(name) {
    const parts = name.split(/\s+/).filter((part) => /^\p{L}/u.test(part));
    return ((parts[0]?.[0] ?? "") + (parts.length > 1 ? (parts.at(-1)?.[0] ?? "") : "")).toUpperCase() || "?";
}
export function avatar(name) {
    const node = element("span", initials(name), "avatar");
    let hash = 0;
    for (const char of name)
        hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
    node.style.background = AVATAR_TINTS[hash % AVATAR_TINTS.length] ?? AVATAR_TINTS[0];
    node.setAttribute("aria-hidden", "true");
    return node;
}
export function personChip(name, isYou = false) {
    const chip = element("span", "", isYou ? "person person--you" : "person");
    chip.append(avatar(name), isYou ? `${name} (you)` : name);
    return chip;
}
export function formatDuration(seconds) {
    const minutes = Math.round(seconds / 60);
    if (minutes < 1)
        return `${Math.max(1, Math.round(seconds))} s`;
    return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} m`;
}
export function formatDue(item) {
    if (!item.due_date)
        return item.due_text;
    return new Date(`${item.due_date}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
export function errorText(error) {
    return error instanceof Error ? error.message : String(error);
}
/** Saves a file into Downloads/<config folder>. */
export async function saveFile(blob, filename) {
    const url = URL.createObjectURL(blob);
    try {
        await chrome.downloads.download({ url, filename: `${CONFIG.downloads.folder}/${filename}`, saveAs: false });
        toast(`Saved to Downloads/${CONFIG.downloads.folder}`);
    }
    finally {
        setTimeout(() => URL.revokeObjectURL(url), 30_000);
    }
}
let toastTimer = null;
export function toast(message) {
    const box = $("toast");
    box.textContent = message;
    box.hidden = false;
    if (toastTimer !== null)
        clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => (box.hidden = true), 2800);
}
