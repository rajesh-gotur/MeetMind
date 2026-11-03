"""Transcript correction: fixes misheard words and names placeholder speakers.

The fixes come back in the same model answer as the notes (one call, see llm.py).
The language model only *suggests* changes. Every suggestion is checked here:
a fix may only swap a few words for sound-alike words, glossary words or
participant names. Anything that rewrites the meaning is thrown away, so the
transcript stays a faithful record of what was said.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.schemas import Turn
from app.services.validator import NameMatcher, is_placeholder

logger = logging.getLogger(__name__)

# A replaced word must sound/look like the word it replaces, unless it is a known word.
SOUND_ALIKE = 0.45
MERGE_GAP_SECONDS = 2.0

_TOKEN = re.compile(r"[\w'’]+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


@dataclass
class Refined:
    turns: list[Turn]
    participants: list[str]
    recorder: str = ""
    placeholders: list[str] = field(default_factory=list)
    corrections: int = 0
    named: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def safe_fix(original: str, proposed: str, known_words: set[str]) -> bool:
    """True when ``proposed`` only corrects misheard words in ``original``."""
    before, after = tokens(original), tokens(proposed)
    if not after or not before:
        return False
    if abs(len(after) - len(before)) > max(3, round(len(before) * 0.2)):
        return False
    deleted = 0
    for op, i1, i2, j1, j2 in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        old, new = before[i1:i2], after[j1:j2]
        deleted += max(0, len(old) - len(new))
        old_joined = "".join(old)
        for word in new:
            # Known words (glossary, participant names) are always allowed, plurals too ("doctypes").
            if word in known_words or word.rstrip("s") in known_words:
                continue
            # "erpnex" -> "erpnext", "prape" -> "frappe": the new word must resemble what was heard.
            if (
                not old_joined
                or SequenceMatcher(None, word, old_joined).ratio() < SOUND_ALIKE
                and not any(SequenceMatcher(None, word, o).ratio() >= SOUND_ALIKE for o in old)
            ):
                return False
    return deleted <= max(3, round(len(before) * 0.15))


def merge_turns(turns: list[Turn]) -> list[Turn]:
    merged: list[Turn] = []
    for turn in sorted(turns, key=lambda t: t.start):
        last = merged[-1] if merged else None
        if last and last.speaker == turn.speaker and turn.start - last.end <= MERGE_GAP_SECONDS:
            merged[-1] = Turn(
                start=last.start, end=max(last.end, turn.end), speaker=last.speaker, text=f"{last.text} {turn.text}"
            )
        else:
            merged.append(turn)
    return merged


class TranscriptRefiner:
    """prepare() before the model call, apply() with its answer, finish() for the final transcript."""

    def __init__(self, terms: list[str]) -> None:
        self._terms = terms

    def prepare(self, turns: list[Turn], participants: list[str], recorded_by: str, unresolved: list[str]) -> Refined:
        recorder = recorded_by.strip()
        placeholders = sorted({t.speaker for t in turns if t.speaker in set(unresolved) or is_placeholder(t.speaker)})
        people = [p for p in dict.fromkeys(participants) if p.strip() and not is_placeholder(p)]
        others = [p for p in people if p.lower() != recorder.lower()]
        result = Refined(turns=list(turns), participants=people, recorder=recorder, placeholders=placeholders)
        # Only one other person is in the call: every unknown voice is theirs. No model needed.
        if placeholders and len(others) == 1:
            result.named = dict.fromkeys(placeholders, others[0])
            result.turns = self._rename(result)
        return result

    def open_placeholders(self, result: Refined) -> list[str]:
        """Placeholders the model may still name ("Participant" is a group, never one person)."""
        return [p for p in result.placeholders if p not in result.named and p.startswith("Speaker ")]

    def apply(self, data: dict, result: Refined) -> None:
        self._apply(data, result, result.participants, result.recorder, result.placeholders, self._terms)
        result.turns = self._rename(result)

    def finish(self, result: Refined) -> Refined:
        result.turns = merge_turns(result.turns)
        spoken = {t.speaker for t in result.turns}
        result.participants = [
            p
            for p in dict.fromkeys([*result.participants, *(t.speaker for t in result.turns)])
            if p in spoken or p in result.participants
        ]
        return result

    @staticmethod
    def _rename(result: Refined) -> list[Turn]:
        return [
            Turn(start=t.start, end=t.end, speaker=result.named.get(t.speaker, t.speaker), text=t.text)
            for t in result.turns
        ]

    def _apply(
        self,
        data: dict,
        result: Refined,
        people: list[str],
        recorder: str,
        placeholders: list[str],
        terms: list[str],
    ) -> None:
        known = {w for phrase in [*terms, *people] for w in tokens(phrase)}
        fixed = list(result.turns)
        for entry in data.get("fixes") or []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(str(entry.get("line", "")).lstrip("Ll"))
            except ValueError:
                continue
            text = " ".join(str(entry.get("text") or "").split())
            if not 0 <= index < len(fixed) or not text or text == fixed[index].text:
                continue
            if safe_fix(fixed[index].text, text, known):
                old = fixed[index]
                fixed[index] = Turn(start=old.start, end=old.end, speaker=old.speaker, text=text)
                result.corrections += 1
            else:
                logger.info("Rejected unsafe correction on line %s", index)
        result.turns = fixed

        names = NameMatcher(people, recorder)
        speakers = data.get("speakers") or {}
        if isinstance(speakers, dict):
            for label, name in speakers.items():
                # "Participant" means several unknown voices together, so it is never given one name.
                if label not in placeholders or label in result.named or not label.startswith("Speaker "):
                    continue
                matched = names.match(str(name or ""), default="")
                # The recorder's own voice is on its own track, so a placeholder is never the recorder.
                if matched and matched.lower() != recorder.lower():
                    result.named[label] = matched
