"""Reads any Google Meet link, in every format Meet uses.

    https://meet.google.com/abc-defg-hij            (classic)
    https://meet.google.com/meet/abc-defg-hij       (newer links)
    meet.google.com/abc-defg-hij?authuser=1&hs=179  (no https, extra parts)
    https://g.co/meet/abc-defg-hij                  (short link)
    abc-defg-hij  or  abcdefghij                    (just the code)
    https://meet.google.com/lookup/team-standup     (nickname links)

The same rules are in extension-source/src/shared/meet-link.ts; change both together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

CODE = re.compile(r"^([a-z]{3})-?([a-z]{4})-?([a-z]{3})$", re.IGNORECASE)
NOT_A_MEETING = {"", "landing", "new", "about", "_meet", "linkredirect", "calling", "unsupported"}
NICKNAME = re.compile(r"^[\w-]{3,}$")


@dataclass(frozen=True)
class MeetLink:
    url: str  # clean link to open, without tracking parts
    code: str  # short name for the meeting, e.g. "abc-defg-hij"


def _canonical(match: re.Match[str]) -> str:
    return f"{match[1]}-{match[2]}-{match[3]}".lower()


def parse_meet_link(text: str | None) -> MeetLink | None:
    raw = (text or "").strip().strip("<>")
    if not raw:
        return None
    if bare := CODE.match(raw):
        code = _canonical(bare)
        return MeetLink(f"https://meet.google.com/{code}", code)

    parts_of = urlsplit(raw if re.match(r"^[a-z]+://", raw, re.IGNORECASE) else f"https://{raw}")
    host = (parts_of.hostname or "").lower().removeprefix("www.")
    parts = [p for p in parts_of.path.split("/") if p]

    if host == "g.co" and len(parts) >= 2 and parts[0].lower() == "meet":
        if short := CODE.match(parts[1]):
            code = _canonical(short)
            return MeetLink(f"https://meet.google.com/{code}", code)
        return MeetLink(f"https://meet.google.com/lookup/{parts[1]}", parts[1])
    if host != "meet.google.com" or not parts or parts[0].lower() in NOT_A_MEETING:
        return None

    for i in range(len(parts) - 1, -1, -1):
        if found := CODE.match(parts[i]):
            code = _canonical(found)
            return MeetLink(f"https://meet.google.com/{'/'.join([*parts[:i], code])}", code)
    if not NICKNAME.match(parts[-1]):
        return None
    return MeetLink(f"https://meet.google.com/{'/'.join(parts)}", parts[-1].lower())
