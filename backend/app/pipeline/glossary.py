"""Layer 8 — glossary fixer (RapidFuzz + Jellyfish, no AI guessing).

Terms come from config.yaml (vocabulary.terms), the participant names, and the
learned glossary (terms corrected in earlier meetings, saved in learned_terms.json).

Two kinds of fix:
  * exact (always safe): the same letters with other spacing/case,
    "erp next" / "Erpnext" -> "ERPNext"
  * sound-alike (only on doubtful words): close spelling AND similar sound,
    "ERPNex" -> "ERPNext", "Frapp" -> "Frappe"
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.fusion import FusedWord
from app.pipeline.hypothesis import norm

logger = logging.getLogger(__name__)

MAX_NGRAM = 3
SPELLING_SCORE = 86  # RapidFuzz ratio (0-100) for a fix on a doubtful word
SOUND_SPELLING_SCORE = 72  # lower spelling score allowed when the words also sound alike
MIN_LENGTH = 4
LEARNED_LIMIT = 150

try:  # optional at import time so the server starts without them
    import jellyfish
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - reported in the banner
    fuzz = None
    jellyfish = None


def available() -> bool:
    return fuzz is not None and jellyfish is not None


@dataclass(frozen=True)
class _Term:
    text: str
    key: str  # compact comparison form
    sound: str  # phonetic code
    words: int


class GlossaryFixer:
    def __init__(self, terms: list[str], doubt_below: float) -> None:
        self._doubt = doubt_below
        self._terms: list[_Term] = []
        seen: set[str] = set()
        for raw in terms:
            text = " ".join(str(raw).split())
            key = _compact(text)
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            self._terms.append(_Term(text, key, _sound(key), len(text.split())))
        self._by_key = {t.key: t for t in self._terms}
        self.fixes: Counter[str] = Counter()

    @property
    def keys(self) -> set[str]:
        return set(self._by_key)

    def fix(self, words: list[FusedWord]) -> list[FusedWord]:
        """Returns a new word list with glossary spellings applied."""
        if not self._terms:
            return words
        out: list[FusedWord] = []
        i = 0
        while i < len(words):
            replaced = False
            for n in range(min(MAX_NGRAM, len(words) - i), 0, -1):
                group = words[i : i + n]
                term = self._match(group)
                if term is None:
                    continue
                trail = _trailing_punctuation(group[-1].text)
                first = group[0]
                changed = " ".join(w.text for w in group).rstrip(".,!?;:") != term.text
                out.append(
                    FusedWord(
                        text=term.text + trail,
                        start=first.start,
                        end=group[-1].end,
                        confidence=max(min(w.confidence for w in group), 0.9 if changed else 0.0),
                        agreement=max(w.agreement for w in group),
                        voters=max(w.voters for w in group),
                        sources=sorted({s for w in group for s in w.sources}),
                        alternatives=[" ".join(w.text for w in group)] if changed else first.alternatives,
                        voice=first.voice,
                        corrected=changed or any(w.corrected for w in group),
                    )
                )
                if changed:
                    self.fixes[term.text] += 1
                i += n
                replaced = True
                break
            if not replaced:
                out.append(words[i])
                i += 1
        return out

    def _match(self, group: list[FusedWord]) -> _Term | None:
        key = _compact(" ".join(w.text for w in group))
        if len(key) < 2:
            return None
        exact = self._by_key.get(key)
        if exact is not None:
            return exact
        # Sound-alike fixes only touch words the ears were unsure about.
        doubtful = any(w.confidence < self._doubt for w in group)
        candidates = {_compact(a) for w in group for a in w.alternatives}
        if not available() or len(key) < MIN_LENGTH:
            return None
        best: tuple[float, _Term] | None = None
        for term in self._terms:
            if abs(term.words - len(group)) > 1 or len(term.key) < MIN_LENGTH:
                continue
            if term.key in candidates:  # another ear already heard the glossary spelling here
                return term
            if not doubtful:
                continue
            score = fuzz.ratio(key, term.key)
            sounds_alike = _sound(key) == term.sound
            if score >= SPELLING_SCORE or (sounds_alike and score >= SOUND_SPELLING_SCORE):
                if best is None or score > best[0]:
                    best = (score, term)
        return best[1] if best else None


def _compact(text: str) -> str:
    return "".join(norm(t) for t in text.split())


def _sound(key: str) -> str:
    if jellyfish is None:
        return key
    try:
        return str(jellyfish.metaphone(key))
    except Exception:  # noqa: BLE001 - non-Latin text
        return key


def _trailing_punctuation(text: str) -> str:
    match = re.search(r"[.,!?;:]+$", text)
    return match.group(0) if match else ""


class LearnedTerms:
    """Terms corrected in past meetings; given to every ear next time (backend/learned_terms.json)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> list[str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        counts = data.get("terms", {}) if isinstance(data, dict) else {}
        return [t for t, _ in sorted(counts.items(), key=lambda kv: -int(kv[1]))][:LEARNED_LIMIT]

    def remember(self, terms: Counter[str]) -> None:
        if not terms:
            return
        with self._lock:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            counts: dict[str, int] = data.get("terms", {}) if isinstance(data, dict) else {}
            for term, count in terms.items():
                counts[term] = int(counts.get(term, 0)) + int(count)
            keep = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:LEARNED_LIMIT])
            try:
                self._path.write_text(json.dumps({"terms": keep}, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not save learned terms: %s", exc)
