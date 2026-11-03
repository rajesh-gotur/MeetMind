"""Large language model calls (JSON in, JSON out) with a free backup chain.

Writer order : each Gemini model (gemini.models) -> Groq backup model -> local Ollama
Reviewer order: Groq backup model -> local Ollama -> Gemini
(the reviewer deliberately starts with a DIFFERENT model than the writer, because
one model tends to miss its own mistakes).

Models only suggest; validators check every answer against the transcript.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.config import Settings
from app.schemas import AnalyzeRequest, Turn

logger = logging.getLogger(__name__)


class AnalysisError(RuntimeError):
    """No model could produce a usable answer."""


@dataclass(frozen=True)
class RawAnalysis:
    data: dict[str, Any]
    model: str


SYSTEM_PROMPT = """You are a meticulous meeting secretary. You read a meeting transcript and return ONLY a JSON object.

Strict rules:
- Use only what is said in the transcript. Never invent people, tasks, dates or numbers.
- Speaker labels are reliable: the RECORDED BY person's lines come from their own microphone.
- Names: use the exact names from the PARTICIPANTS list. If the owner of a task is unclear, use "Unassigned".
- Action items are every piece of work someone must do after the meeting:
  * commitments: "I will...", "I'll...", "let me...", "I am going to..."
  * assignments and requests: "please...", "can you...", "I want you to...", "you need to...",
    "your task / assignment is...", "make sure you...", "learn / prepare / send / fix X".
  The owner is the person who must do the work (for a request, the person being asked; when only two people
  are in the meeting, that is the other person). "assigned_by" is the person who asked.
  Split one assignment into separate tasks only when it has clearly separate deliverables.
  Each task needs "evidence": a short exact quote (max 25 words) copied word for word from ONE transcript line,
  and "time": the seconds value shown at the start of that line.
- Decisions are agreements or choices the group made. An assignment of work is an action item, not a decision.
- Due dates: only if a deadline is said. Convert to YYYY-MM-DD using the MEETING DATE (e.g. "by Friday" =
  the next Friday after the meeting date, "tomorrow" = the day after). Put the original words in "due_text".
  Otherwise due_date = null.
- Priority: "High" if urgent/blocking/today, "Low" if optional/nice-to-have, else "Medium".
- Topics: split the meeting into its real topics in time order, each with start/end seconds from the lines.
- Write the summaries in clear, simple English, even if people spoke Hindi or Hinglish.
- Spell product and project words exactly as in the GLOSSARY.

JSON shape:
{
  "title": "short meeting title (use the given title if it is meaningful)",
  "overview": "2-4 sentence summary of the whole meeting",
  "key_points": ["..."],
  "topics": [{"title": "...", "summary": "2-3 sentences", "start": 0, "end": 120, "speakers": ["..."]}],
  "decisions": [{"text": "...", "made_by": "name or empty", "time": 95}],
  "action_items": [{"task": "clear task starting with a verb", "owner": "name", "assigned_by": "name",
                    "due_date": "YYYY-MM-DD or null", "due_text": "words used or empty",
                    "priority": "High|Medium|Low", "time": 42, "evidence": "exact quote"}],
  "blockers": [{"text": "...", "raised_by": "name", "time": 300}],
  "next_steps": ["..."],
  "next_meeting": "when the next meeting is, if said, else empty"
}
Use empty lists when there is nothing for a section."""


# Added to SYSTEM_PROMPT so ONE model call both proof-reads the transcript and writes the notes
# (two calls in a row made the "Preparing your notes" step twice as slow).
CORRECTION_PROMPT = """

Also proof-read the transcript (speech recognition mishears names, product names and technical words):
- "fixes": for each line with clearly misheard words, return {"line": <L number>, "text": "full corrected line"}.
  Fix only names of PARTICIPANTS, GLOSSARY words and obvious sound-alike mistakes ("ERP next" -> "ERPNext",
  "prap" -> "Frappe"), and remove obvious junk nobody said (a stray "PARTICIPANT 1"). Do NOT rephrase,
  shorten, translate or add words. Leave unchanged lines out.
- "speakers": map a PLACEHOLDER SPEAKER (for example "Speaker 2") to a PARTICIPANT name only when the transcript
  makes it clear (greeted or addressed by name, self-introduction). Never map one to the RECORDED BY person.
  If unsure, leave it out. In the notes, use the mapped names.
Add these two keys to the same JSON object:
  "fixes": [{"line": 12, "text": "..."}], "speakers": {"Speaker 2": "Exact Participant Name"}"""


# Added when layer 11a found verified facts (FRAME method: facts first, then the notes).
FACTS_PROMPT = """

VERIFIED FACTS are listed before the transcript. Every task and decision fact with importance 5 or more MUST
appear in your notes (use its quote as evidence). Build the overview and topics around the most important facts.
Do not add tasks or decisions that are neither in the facts nor clearly said in the transcript."""


def format_transcript(turns: list[Turn], numbered: bool = False) -> str:
    lines = []
    for i, turn in enumerate(turns):
        minutes, seconds = divmod(int(turn.start), 60)
        prefix = f"L{i} " if numbered else ""
        lines.append(f"{prefix}[{minutes:02d}:{seconds:02d} | {int(turn.start)}s] {turn.speaker}: {turn.text}")
    return "\n".join(lines)


def build_user_prompt(
    request: AnalyzeRequest,
    glossary: list[str] | None = None,
    placeholders: list[str] | None = None,
    correct: bool = False,
) -> str:
    meeting = request.meeting
    shape = "L<number> [mm:ss | seconds] Speaker: words" if correct else "[mm:ss | seconds] Speaker: words"
    extra = f"PLACEHOLDER SPEAKERS: {', '.join(placeholders or []) or 'none'}\n" if correct else ""
    return (
        f"MEETING TITLE: {meeting.title}\n"
        f"MEETING DATE: {meeting.started_at.date().isoformat()} ({meeting.started_at.strftime('%A')})\n"
        f"DURATION: {int(meeting.duration_seconds // 60)} minutes\n"
        f"PARTICIPANTS: {', '.join(request.participants) or 'not known'}\n"
        f"RECORDED BY: {meeting.recorded_by or 'unknown'}\n"
        f"{extra}"
        f"GLOSSARY: {', '.join(glossary or []) or 'none'}\n\n"
        f"TRANSCRIPT (each line: {shape}):\n{format_transcript(request.turns, numbered=correct)}"
    )


def parse_json(text: str) -> dict[str, Any]:
    """Accepts plain JSON or JSON wrapped in a ```json fence."""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Model returned JSON that is not an object.")
    return data


Order = Literal["writer", "reviewer"]


class LanguageModel:
    """Sends one JSON task down the backup chain and returns the first good answer."""

    _ollama_up: bool | None = None  # checked once per server run

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def ready(self) -> bool:
        return bool(
            self._settings.gemini.api_key
            or (self._settings.groq.api_key and self._settings.groq.summary_fallback_model)
            or self._settings.ollama.enabled == "on"
        )

    def _chain(self, order: Order) -> list[tuple[str, str]]:
        gemini = [("gemini", m) for m in self._settings.gemini.models] if self._settings.gemini.api_key else []
        groq = (
            [("groq", self._settings.groq.summary_fallback_model)]
            if self._settings.groq.api_key and self._settings.groq.summary_fallback_model
            else []
        )
        ollama = [("ollama", self._settings.ollama.model)] if self._settings.ollama.enabled != "off" else []
        return [*groq, *ollama, *gemini] if order == "reviewer" else [*gemini, *groq, *ollama]

    async def complete_json(self, system: str, prompt: str, order: Order = "writer") -> RawAnalysis:
        errors: list[str] = []
        # One model may take at most timeout_seconds; then the next model gets its turn.
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.gemini.timeout_seconds, connect=15)
        )
        try:
            for provider, model in self._chain(order):
                if provider == "ollama" and not await self._ollama_available(client):
                    continue
                started = time.monotonic()
                try:
                    call = {"gemini": self._gemini, "groq": self._groq, "ollama": self._ollama}[provider]
                    data = await call(client, model, system, prompt)
                    logger.info("%s %s answered in %.1fs", provider.title(), model, time.monotonic() - started)
                    return RawAnalysis(data, model)
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                    errors.append(f"{model}: {_short(exc)}")
                    logger.warning(
                        "%s %s failed after %.1fs: %s", provider.title(), model, time.monotonic() - started, _short(exc)
                    )
        finally:
            if self._client is None:
                await client.aclose()
        detail = "; ".join(errors) or "no language model is configured"
        raise AnalysisError(f"Could not get an answer from any AI model ({detail}).")

    async def _ollama_available(self, client: httpx.AsyncClient) -> bool:
        if self._settings.ollama.enabled == "on":
            return True
        if LanguageModel._ollama_up is None:
            try:
                response = await client.get(f"{self._settings.ollama.base_url}/api/tags", timeout=2)
                LanguageModel._ollama_up = response.status_code == 200
            except httpx.HTTPError:
                LanguageModel._ollama_up = False
        return LanguageModel._ollama_up

    async def _gemini(self, client: httpx.AsyncClient, model: str, system: str, prompt: str) -> dict[str, Any]:
        url = f"{self._settings.gemini.base_url}/models/{model}:generateContent"
        generation: dict[str, Any] = {"responseMimeType": "application/json", "temperature": 0.1}
        level = self._settings.gemini.thinking_level
        if level:
            # Flash models "think" before answering; a low level keeps the notes fast for this simple task.
            generation["thinkingConfig"] = {"thinkingLevel": level}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        headers = {"x-goog-api-key": self._settings.gemini.api_key}
        response = await client.post(url, headers=headers, json=body)
        if response.status_code == 400 and level and "think" in response.text.lower():
            # This model does not accept a thinking level: ask again without it.
            generation.pop("thinkingConfig", None)
            response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
        parts = payload["candidates"][0]["content"]["parts"]
        # Thinking models may return "thought" parts first; keep only the answer.
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        return parse_json(text)

    async def _groq(self, client: httpx.AsyncClient, model: str, system: str, prompt: str) -> dict[str, Any]:
        url = f"{self._settings.groq.base_url}/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        response = await client.post(url, headers={"Authorization": f"Bearer {self._settings.groq.api_key}"}, json=body)
        response.raise_for_status()
        return parse_json(response.json()["choices"][0]["message"]["content"])

    async def _ollama(self, client: httpx.AsyncClient, model: str, system: str, prompt: str) -> dict[str, Any]:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1},
        }
        response = await client.post(
            f"{self._settings.ollama.base_url}/api/chat", json=body, timeout=self._settings.ollama.timeout_seconds
        )
        response.raise_for_status()
        return parse_json(response.json()["message"]["content"])


class MeetingAnalyzer:
    """Summary, topics, decisions and tasks for one (already corrected) transcript."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self.model = LanguageModel(settings, client)

    async def analyze(
        self,
        request: AnalyzeRequest,
        placeholders: list[str] | None = None,
        facts: str = "",
        glossary: list[str] | None = None,
    ) -> RawAnalysis:
        """Writes the notes (plus spelling fixes and speaker names when vocabulary.correct_transcript is on).

        ``facts`` are the verified facts from layer 11a; the notes must cover them.
        """
        correct = self._settings.vocabulary.correct_transcript
        system = SYSTEM_PROMPT + (CORRECTION_PROMPT if correct else "") + (FACTS_PROMPT if facts else "")
        prompt = build_user_prompt(request, glossary or self._settings.vocabulary.terms, placeholders, correct)
        if facts:
            prompt = f"VERIFIED FACTS (checked against the transcript):\n{facts}\n\n{prompt}"
        return await self.model.complete_json(system, prompt)


def _short(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return f"{exc.__class__.__name__}: {str(exc)[:120]}"
