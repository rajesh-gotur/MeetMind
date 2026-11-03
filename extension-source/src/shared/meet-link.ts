/**
 * Reads any Google Meet link people paste or open, in every format Meet uses:
 *   https://meet.google.com/abc-defg-hij           (classic)
 *   https://meet.google.com/meet/abc-defg-hij      (newer links)
 *   meet.google.com/abc-defg-hij?authuser=1&hs=179 (no https, extra parts)
 *   https://g.co/meet/abc-defg-hij                 (short link)
 *   abc-defg-hij  or  abcdefghij                   (just the code)
 *   https://meet.google.com/lookup/team-standup    (nickname links)
 * The same rules are in backend/app/bot/links.py; change both together.
 */

export interface MeetLink {
  /** Clean link to open: https://meet.google.com/<path>, without tracking parts. */
  url: string;
  /** Short name for the meeting, e.g. "abc-defg-hij" (used in titles and file names). */
  code: string;
}

const CODE = /^([a-z]{3})-?([a-z]{4})-?([a-z]{3})$/i;
/** Meet pages that are not a meeting. */
const NOT_A_MEETING = new Set(["", "landing", "new", "about", "_meet", "linkredirect", "calling", "unsupported"]);

export function parseMeetLink(text: string | undefined | null): MeetLink | null {
  const raw = (text ?? "").trim().replace(/^<|>$/g, "");
  if (!raw) return null;

  const bare = CODE.exec(raw);
  if (bare) return fromCode(bare);

  let url: URL;
  try {
    url = new URL(/^[a-z]+:\/\//i.test(raw) ? raw : `https://${raw}`);
  } catch {
    return null;
  }
  const host = url.hostname.toLowerCase().replace(/^www\./, "");
  const parts = url.pathname.split("/").filter(Boolean);

  if (host === "g.co" && parts[0]?.toLowerCase() === "meet" && parts[1]) {
    const code = CODE.exec(parts[1]);
    return code ? fromCode(code) : { url: `https://meet.google.com/lookup/${parts[1]}`, code: parts[1] };
  }
  if (host !== "meet.google.com" || parts.length === 0 || NOT_A_MEETING.has(parts[0]!.toLowerCase())) return null;

  for (let i = parts.length - 1; i >= 0; i--) {
    const code = CODE.exec(parts[i]!);
    if (code) {
      // Keep Meet's own path (for example /meet/abc-defg-hij) with the code in its usual form.
      const path = [...parts.slice(0, i), canonical(code)].join("/");
      return { url: `https://meet.google.com/${path}`, code: canonical(code) };
    }
  }
  const last = parts[parts.length - 1]!;
  if (!/^[\w-]{3,}$/.test(last)) return null;
  return { url: `https://meet.google.com/${parts.join("/")}`, code: last.toLowerCase() };
}

function canonical(match: RegExpExecArray): string {
  return `${match[1]}-${match[2]}-${match[3]}`.toLowerCase();
}

function fromCode(match: RegExpExecArray): MeetLink {
  const code = canonical(match);
  return { url: `https://meet.google.com/${code}`, code };
}
