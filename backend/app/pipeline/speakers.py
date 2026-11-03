"""Layer 10 — who said each word (speaker vote).

Signals, per word:
  captions   the caption shown at that moment carries Meet's exact display name (strongest)
  timeline   who Meet highlighted as speaking at that moment (when the page shows it)
  voice      Deepgram's voice group, turned into a person by overlap with the two
             signals above ("voice 1 matches Rajesh in 92% of its words -> Rajesh")

The name with the most weight wins. When signals point to different people with
similar weight (crosstalk) the word is marked "Unclear" instead of guessing a
wrong name. A placeholder "Speaker N" is used only when nothing names a voice;
the notes step may still name it from the conversation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from app.pipeline.fusion import FusedWord
from app.schemas import Turn

CAPTION_WEIGHT = 0.6
TIMELINE_WEIGHT = 0.3
VOICE_WEIGHT = 0.35
CAPTION_PAD_SECONDS = 1.5
CAPTION_REACH_SECONDS = 4.0
VOICE_MATCH_SHARE = 0.6
VOICE_MATCH_WORDS = 5
UNCLEAR_MARGIN = 0.12
MERGE_GAP_SECONDS = 2.0
UNCLEAR = "Unclear"


@dataclass(frozen=True)
class Attributed:
    turns: list[Turn]
    participants: list[str]
    unresolved: list[str]  # "Speaker N" labels the notes step may still name
    confirmed: float  # share of words named by Meet's captions or by 2+ agreeing signals (0-1)


def attribute(
    words: list[FusedWord],
    captions: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    roster: list[str],
    bot_name: str,
) -> Attributed:
    bot = bot_name.strip().lower()
    people = sorted({n.strip() for n in roster if n.strip() and n.strip().lower() != bot})
    heard = [c for c in captions if str(c.get("speaker", "")).strip().lower() not in {"", bot}]
    talks = [t for t in timeline if str(t.get("name", "")).strip().lower() not in {"", bot}]

    direct = [_direct(w, heard, talks) for w in words]
    voices = _map_voices(words, [votes for votes, _ in direct])

    labels: list[str] = []
    confirmed = 0
    placeholder: dict[str, str] = {}
    for word, (votes, signals) in zip(words, direct, strict=True):
        captioned = {name for name, weight in votes.items() if weight >= CAPTION_WEIGHT}
        scores = dict(votes)
        signals = dict(signals)
        mapped = voices.get(word.voice or "")
        if mapped:
            scores[mapped] = scores.get(mapped, 0.0) + VOICE_WEIGHT
            signals[mapped] = signals.get(mapped, 0) + 1
        if not scores:
            if len(people) == 1:
                labels.append(people[0])
            else:
                key = word.voice or "unknown"
                placeholder.setdefault(key, f"Speaker {len(placeholder) + 1}")
                labels.append(placeholder[key])
            continue
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < UNCLEAR_MARGIN:
            labels.append(UNCLEAR)
            continue
        winner = ranked[0][0]
        labels.append(winner)
        # Confirmed: nobody else is named, and Meet's own caption or two independent signals name this person.
        if len(scores) == 1 and (signals.get(winner, 0) >= 2 or winner in captioned):
            confirmed += 1

    labels = _smooth(labels)
    turns = _turns(words, labels)
    spoken = [t.speaker for t in turns if t.speaker != UNCLEAR]
    unresolved = sorted({label for label in labels if label.startswith("Speaker ")})
    participants = list(dict.fromkeys([*people, *(s for s in spoken if not s.startswith("Speaker "))]))
    return Attributed(turns, participants, unresolved, confirmed / len(words) if words else 0.0)


def _direct(
    word: FusedWord, captions: list[dict[str, Any]], timeline: list[dict[str, Any]]
) -> tuple[dict[str, float], dict[str, int]]:
    """Votes (weights) and how many independent signals name each person, at the middle of the word."""
    t = (word.start + word.end) / 2
    votes: dict[str, float] = defaultdict(float)
    signals: dict[str, int] = defaultdict(int)
    # Every caption on screen at this moment votes (two at once = people talking over each other).
    showing = {str(c["speaker"]).strip() for c in captions if _showing(c, t)}
    if not showing:
        caption = _caption_at(t, captions)
        showing = {str(caption["speaker"]).strip()} if caption is not None else set()
    for name in showing:
        votes[name] += CAPTION_WEIGHT
        signals[name] += 1
    talking = [talk for talk in timeline if float(talk["start"]) - 0.3 <= t <= float(talk["end"]) + 0.3]
    for talk in talking:
        name = str(talk["name"]).strip()
        votes[name] += TIMELINE_WEIGHT / len(talking)
        signals[name] += 1
    return dict(votes), dict(signals)


def _showing(caption: dict[str, Any], t: float) -> bool:
    start, end = float(caption["start"]), float(caption.get("end") or caption["start"])
    return start - 0.3 <= t <= end + 0.3


def _caption_at(t: float, captions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The caption shown when a word was spoken: inside a caption wins (nearest core first), else the nearest."""
    best, best_score = None, float("inf")
    for caption in captions:
        start, end = float(caption["start"]), float(caption.get("end") or caption["start"])
        padded_start, padded_end = start - CAPTION_PAD_SECONDS, end + CAPTION_PAD_SECONDS
        gap = padded_start - t if t < padded_start else t - padded_end if t > padded_end else 0.0
        if gap > CAPTION_REACH_SECONDS:
            continue
        core = start - t if t < start else max(0.0, t - end)
        score = -1 + core / 100 if gap == 0 else gap
        if score < best_score:
            best, best_score = caption, score
    return best


def _map_voices(words: list[FusedWord], direct: list[dict[str, float]]) -> dict[str, str]:
    """Voice group -> person, when most of the group's words are named by captions/timeline."""
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for word, votes in zip(words, direct, strict=True):
        if word.voice and votes:
            counts[word.voice][max(votes.items(), key=lambda kv: kv[1])[0]] += 1
    mapped: dict[str, str] = {}
    for voice, names in counts.items():
        name, hits = names.most_common(1)[0]
        if hits >= VOICE_MATCH_WORDS and hits / sum(names.values()) >= VOICE_MATCH_SHARE:
            mapped[voice] = name
    return mapped


def _smooth(labels: list[str]) -> list[str]:
    """One word between two runs of the same speaker is jitter."""
    out = list(labels)
    for i in range(1, len(out) - 1):
        if out[i] != out[i - 1] and out[i - 1] == out[i + 1]:
            out[i] = out[i - 1]
    return out


def _turns(words: list[FusedWord], labels: list[str]) -> list[Turn]:
    turns: list[Turn] = []
    for word, label in zip(words, labels, strict=True):
        last = turns[-1] if turns else None
        if last and last.speaker == label and word.start - last.end < MERGE_GAP_SECONDS:
            turns[-1] = Turn(
                start=last.start, end=max(last.end, word.end), speaker=label, text=f"{last.text} {word.text}"
            )
        else:
            turns.append(Turn(start=round(word.start, 1), end=round(word.end, 1), speaker=label, text=word.text))
    return [Turn(start=t.start, end=round(t.end, 1), speaker=t.speaker, text=t.text.strip()) for t in turns]
