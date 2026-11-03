"""Layer 9 — AI fixer for doubtful words (generative error correction with several hypotheses).

Only words the vote was unsure about are sent, each with the versions the
different ears heard and a little context. Research on LLM error correction shows
models fix far more when they see several hypotheses instead of one.

The AI may only choose: one of the heard versions, a glossary word or a
participant name. Anything else goes through refine.safe_fix (sound-alike check)
and is thrown away if it changes the meaning. If no AI answers, the vote's best
word simply stays.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.pipeline.fusion import FusedWord
from app.pipeline.hypothesis import norm
from app.schemas import Turn
from app.services.llm import AnalysisError, LanguageModel
from app.services.refine import safe_fix, tokens

logger = logging.getLogger(__name__)

CONTEXT_WORDS = 7
MAX_SPAN_WORDS = 4

SYSTEM = """You correct speech-recognition mistakes in a meeting transcript. Return ONLY JSON.
For each item you get: the words before, the doubtful words, the words after, and OPTIONS = the different
versions that several speech engines heard. Choose what was most likely really said.
Rules:
- Prefer one of the OPTIONS. You may instead use a GLOSSARY term or a PARTICIPANT name when it sounds like
  the options (for example "ERP next" -> "ERPNext", "prape" -> "Frappe").
- Never add new information, never rephrase, never translate. If unsure, return the first option.
JSON: {"choices": [{"id": 1, "text": "chosen words"}]}"""


@dataclass(frozen=True)
class Doubt:
    """A doubtful span, stored for Record-here meetings until the notes are written."""

    time: float  # meeting seconds
    heard: str
    options: list[str]


@dataclass(frozen=True)
class _Item:
    id: int
    before: str
    heard: str
    after: str
    options: list[str]


class AiFixer:
    def __init__(self, model: LanguageModel, terms: list[str], names: list[str], batch: int) -> None:
        self._model = model
        self._terms = terms
        self._names = names
        self._batch = batch
        self._known = {w for phrase in [*terms, *names] for w in tokens(phrase)}
        self._known_phrases = {"".join(norm(t) for t in p.split()) for p in [*terms, *names]}
        self.failed = False

    async def fix_words(self, words: list[FusedWord], doubt_below: float) -> tuple[list[FusedWord], int]:
        """Returns the words with doubtful spans resolved, and how many spans changed."""
        spans = _doubtful_spans(words, doubt_below)
        if not spans:
            return words, 0
        items = [
            _Item(
                id=i,
                before=" ".join(w.text for w in words[max(0, a - CONTEXT_WORDS) : a]),
                heard=" ".join(w.text for w in words[a:b]),
                after=" ".join(w.text for w in words[b : b + CONTEXT_WORDS]),
                options=_options(words[a:b]),
            )
            for i, (a, b) in enumerate(spans)
        ]
        choices = await self._choose(items)
        out: list[FusedWord] = []
        cursor, changed = 0, 0
        for item, (a, b) in zip(items, spans, strict=True):
            out.extend(words[cursor:a])
            chosen = choices.get(item.id)
            group = words[a:b]
            if chosen is None or chosen == item.heard:
                out.extend(group)
            else:
                out.extend(_replace(group, chosen))
                changed += 1
            cursor = b
        out.extend(words[cursor:])
        return out, changed

    async def fix_turns(self, turns: list[Turn], doubts: list[Doubt]) -> tuple[list[Turn], int]:
        """Record-here: doubts were saved while the pieces were transcribed; fix them inside the turns."""
        located: list[tuple[int, Doubt]] = []
        for doubt in doubts:
            for index, turn in enumerate(turns):
                if turn.start - 2 <= doubt.time <= turn.end + 2 and doubt.heard.lower() in turn.text.lower():
                    located.append((index, doubt))
                    break
        if not located:
            return turns, 0
        items = []
        for i, (index, doubt) in enumerate(located):
            text = turns[index].text
            at = text.lower().find(doubt.heard.lower())
            items.append(
                _Item(
                    i,
                    " ".join(text[:at].split()[-CONTEXT_WORDS:]),
                    doubt.heard,
                    " ".join(text[at + len(doubt.heard) :].split()[:CONTEXT_WORDS]),
                    doubt.options,
                )
            )
        choices = await self._choose(items)
        fixed = list(turns)
        changed = 0
        for item, (index, doubt) in zip(items, located, strict=True):
            chosen = choices.get(item.id)
            if not chosen or chosen == doubt.heard:
                continue
            turn = fixed[index]
            at = turn.text.lower().find(doubt.heard.lower())
            if at < 0:
                continue
            text = turn.text[:at] + chosen + turn.text[at + len(doubt.heard) :]
            fixed[index] = Turn(start=turn.start, end=turn.end, speaker=turn.speaker, text=text)
            changed += 1
        return fixed, changed

    async def _choose(self, items: list[_Item]) -> dict[int, str]:
        choices: dict[int, str] = {}
        for start in range(0, len(items), self._batch):
            batch = items[start : start + self._batch]
            prompt = (
                f"GLOSSARY: {', '.join(self._terms) or 'none'}\n"
                f"PARTICIPANTS: {', '.join(self._names) or 'not known'}\n\nITEMS:\n"
                + "\n".join(
                    json.dumps(
                        {"id": i.id, "before": i.before, "heard": i.heard, "after": i.after, "options": i.options},
                        ensure_ascii=False,
                    )
                    for i in batch
                )
            )
            try:
                raw = await self._model.complete_json(SYSTEM, prompt)
            except AnalysisError as exc:
                logger.warning("AI fixer skipped: %s", exc)
                self.failed = True
                return choices
            by_id = {i.id: i for i in batch}
            for entry in raw.data.get("choices") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    item = by_id[int(str(entry.get("id")))]
                except (KeyError, TypeError, ValueError):
                    continue
                text = " ".join(str(entry.get("text") or "").split())
                if text and self._allowed(item, text):
                    choices[item.id] = text
        return choices

    def _allowed(self, item: _Item, text: str) -> bool:
        key = "".join(norm(t) for t in text.split())
        if key in {"".join(norm(t) for t in o.split()) for o in item.options}:
            return True
        if key in self._known_phrases:
            return True
        return safe_fix(item.heard, text, self._known)


def _doubtful_spans(words: list[FusedWord], doubt_below: float) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(words):
        if words[i].confidence < doubt_below and (words[i].alternatives or words[i].voters <= 1):
            j = i + 1
            while j < len(words) and j - i < MAX_SPAN_WORDS and words[j].confidence < doubt_below:
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _options(group: list[FusedWord]) -> list[str]:
    heard = " ".join(w.text for w in group)
    options = [heard]
    for word in group:
        for alt in word.alternatives:
            candidate = heard.replace(word.text, alt, 1) if len(group) > 1 else alt
            if candidate and candidate not in options:
                options.append(candidate)
    return options[:5]


def _replace(group: list[FusedWord], chosen: str) -> list[FusedWord]:
    pieces = chosen.split()
    start, end = group[0].start, group[-1].end
    step = (end - start) / max(len(pieces), 1)
    return [
        FusedWord(
            text=p,
            start=round(start + i * step, 3),
            end=round(start + (i + 1) * step, 3),
            confidence=0.85,
            agreement=max(w.agreement for w in group),
            voters=max(w.voters for w in group),
            sources=sorted({s for w in group for s in w.sources}),
            alternatives=[" ".join(w.text for w in group)] if i == 0 else [],
            voice=group[0].voice,
            corrected=True,
        )
        for i, p in enumerate(pieces)
    ]
