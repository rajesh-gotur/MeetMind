"""Layer 11a — facts first (FRAME method, EMNLP 2025).

The transcript is read in sections (long meetings lose nothing in the middle).
For each section the AI lists facts: decisions, tasks, insights, questions,
blockers and context, each with an exact quote, the time, and an importance score
(1-10). Every quote is then checked against the transcript with RapidFuzz; facts
nobody actually said are dropped before they can reach the notes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.schemas import AnalyzeRequest, Turn
from app.services.llm import AnalysisError, LanguageModel, format_transcript

logger = logging.getLogger(__name__)

QUOTE_MATCH = 80  # RapidFuzz partial ratio (0-100) a quote needs inside one transcript line
TYPES = {"decision", "task", "insight", "question", "blocker", "context"}

SYSTEM = """You extract FACTS from one section of a meeting transcript. Return ONLY JSON.
A fact is something a reader of the meeting notes must know. Types:
  "task"      work someone must do after the meeting (commitments, assignments, requests)
  "decision"  something the group agreed or chose
  "blocker"   a problem stopping progress
  "question"  an open question nobody answered
  "insight"   an important number, result, status or opinion
  "context"   background needed to understand the above
Rules: use only what is said; every fact needs "quote" = exact words (max 25) copied from ONE line, and
"time" = that line's seconds. For tasks, "owner" = who must do it, "assigned_by" = who asked, "due_text" = the
deadline words if any. "importance" 1-10 (10 = must be in the notes). Write "text" in clear simple English.
JSON: {"facts": [{"type": "task", "text": "...", "quote": "...", "time": 42, "speaker": "...",
                  "owner": "", "assigned_by": "", "due_text": "", "importance": 8}]}"""


@dataclass
class Fact:
    id: str
    type: str
    text: str
    quote: str
    time: float
    speaker: str
    owner: str
    assigned_by: str
    due_text: str
    importance: int
    verified: bool = False


def sections(turns: list[Turn], minutes: float) -> list[list[Turn]]:
    """Splits the transcript into sections of about `minutes`, at turn boundaries."""
    limit = minutes * 60
    out: list[list[Turn]] = []
    for turn in turns:
        if not out or turn.start - out[-1][0].start >= limit:
            out.append([])
        out[-1].append(turn)
    return out


async def extract_facts(
    model: LanguageModel, request: AnalyzeRequest, glossary: list[str], minutes: float
) -> list[Fact]:
    parts = sections(request.turns, minutes)
    results = await asyncio.gather(
        *(_section(model, request, glossary, part, i, len(parts)) for i, part in enumerate(parts)),
        return_exceptions=True,
    )
    facts: list[Fact] = []
    for result in results:
        if isinstance(result, list):
            facts.extend(result)
        else:
            logger.warning("Facts for one section could not be extracted: %s", result)
    verify_quotes(facts, request.turns)
    kept = [f for f in facts if f.verified]
    for index, fact in enumerate(kept, start=1):
        fact.id = f"F{index}"
    logger.info("Facts: %s extracted, %s verified against the transcript", len(facts), len(kept))
    return kept


async def _section(
    model: LanguageModel, request: AnalyzeRequest, glossary: list[str], part: list[Turn], index: int, total: int
) -> list[Fact]:
    meeting = request.meeting
    prompt = (
        f"MEETING: {meeting.title} on {meeting.started_at.date().isoformat()} ({meeting.started_at.strftime('%A')})\n"
        f"PARTICIPANTS: {', '.join(request.participants) or 'not known'}\n"
        f"GLOSSARY: {', '.join(glossary) or 'none'}\n"
        f"SECTION {index + 1} of {total}. TRANSCRIPT (each line: [mm:ss | seconds] Speaker: words):\n"
        f"{format_transcript(part)}"
    )
    try:
        raw = await model.complete_json(SYSTEM, prompt)
    except AnalysisError as exc:
        raise RuntimeError(str(exc)) from exc
    facts: list[Fact] = []
    for entry in raw.data.get("facts") or []:
        if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
            continue
        kind = str(entry.get("type") or "").strip().lower()
        try:
            importance = max(1, min(10, int(entry.get("importance") or 5)))
            time = float(entry.get("time") or 0)
        except (TypeError, ValueError):
            importance, time = 5, 0.0
        facts.append(
            Fact(
                id="",
                type=kind if kind in TYPES else "context",
                text=str(entry["text"]).strip()[:500],
                quote=str(entry.get("quote") or "").strip()[:400],
                time=time,
                speaker=str(entry.get("speaker") or "").strip()[:120],
                owner=str(entry.get("owner") or "").strip()[:120],
                assigned_by=str(entry.get("assigned_by") or "").strip()[:120],
                due_text=str(entry.get("due_text") or "").strip()[:120],
                importance=importance,
            )
        )
    return facts


def verify_quotes(facts: list[Fact], turns: list[Turn]) -> None:
    """A fact is verified when its quote is found in one transcript line; its time comes from that line."""
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover
        fuzz = None
    lowered = [t.text.lower() for t in turns]
    for fact in facts:
        quote = fact.quote.lower()
        if not quote:
            continue
        best, best_index = 0.0, -1
        for index, line in enumerate(lowered):
            score = fuzz.partial_ratio(quote, line) if fuzz else (100.0 if quote in line else 0.0)
            if score > best:
                best, best_index = score, index
        if best >= QUOTE_MATCH and best_index >= 0:
            fact.verified = True
            fact.time = round(turns[best_index].start, 1)
            fact.speaker = fact.speaker or turns[best_index].speaker


def facts_block(facts: list[Fact]) -> str:
    """Facts as text for the writer and the reviewer."""
    ordered = sorted(facts, key=lambda f: (-f.importance, f.time))
    lines = []
    for f in ordered:
        extra = ""
        if f.type == "task":
            extra = f" | owner: {f.owner or '?'} | asked by: {f.assigned_by or '?'} | due: {f.due_text or '-'}"
        lines.append(
            f'{f.id} [{f.type}, importance {f.importance}, {int(f.time)}s] {f.text} | quote: "{f.quote}"{extra}'
        )
    return "\n".join(lines)
