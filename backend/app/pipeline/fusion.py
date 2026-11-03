"""Layer 7 — word vote (ROVER-style fusion of every ear).

1. A timed hypothesis (Deepgram first) is the backbone: it gives each word its time.
2. Every other hypothesis is aligned to the backbone word by word (difflib).
3. Each ear votes only for moments it actually covered, so an ear that missed a
   stretch of audio cannot delete words there.
4. Per backbone word, the candidate with the highest weight wins
   (ear weight x word confidence, + a bonus for glossary words).
   Words that only some ears heard are inserted when most covering ears agree.
5. Every result word keeps a confidence (0-1), how many ears agreed, and the
   other versions heard (used by the AI fixer, layer 9).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.pipeline.hypothesis import SOURCE_WEIGHT, HWord, Hypothesis, norm
from app.pipeline.vad import Region

GLOSSARY_BONUS = 0.3
COVER_PAD_SECONDS = 1.5  # an ear "covers" the time around its own words
COVER_GAP_SECONDS = 4.0  # a longer pause in an ear's words is a gap it did not hear
BACKBONE_ORDER = ("deepgram", "groq", "local_whisper", "gemini", "indic", "captions")


@dataclass
class FusedWord:
    text: str
    start: float
    end: float
    confidence: float
    agreement: int  # ears that heard this word
    voters: int  # ears that covered this moment
    sources: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    voice: str | None = None  # Deepgram voice group of the backbone word
    corrected: bool = False  # changed by the glossary fixer or the AI fixer


@dataclass
class _Vote:
    weight: float
    display: str
    best_source_weight: float
    sources: list[str] = field(default_factory=list)


@dataclass
class _Slot:
    word: HWord
    source: str
    votes: dict[str, _Vote] = field(default_factory=dict)
    voters: set[str] = field(default_factory=set)
    covering_weight: float = 0.0


def _weight(source: str, confidence: float) -> float:
    return SOURCE_WEIGHT.get(source, 0.6) * (0.5 + 0.5 * max(0.0, min(confidence, 1.0)))


def _coverage(
    hyp: Hypothesis, span: tuple[float, float], heard: list[tuple[float, float]] | None = None
) -> list[tuple[float, float]]:
    """Time ranges this ear actually heard.

    An ear without word times cannot claim silence anywhere: it covers only where an ear WITH
    times heard sound (so when the sound is lost, the captions are not outvoted by an empty guess).
    """
    if not hyp.timed and hyp.source != "captions":
        return heard if heard is not None else [span]
    ranges: list[tuple[float, float]] = []
    for word in sorted(hyp.words, key=lambda w: w.start):
        start, end = word.start - COVER_PAD_SECONDS, word.end + COVER_PAD_SECONDS
        if ranges and start - ranges[-1][1] <= COVER_GAP_SECONDS:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def _covers(ranges: list[tuple[float, float]], t: float) -> bool:
    return any(a <= t <= b for a, b in ranges)


def choose_backbone(hyps: list[Hypothesis]) -> Hypothesis | None:
    usable = [h for h in hyps if h.words]
    if not usable:
        return None
    timed = [h for h in usable if h.timed]
    pool = timed or usable
    order = {name: i for i, name in enumerate(BACKBONE_ORDER)}
    return min(pool, key=lambda h: (order.get(h.source, 99), -len(h.words)))


def respread(hyp: Hypothesis, regions: list[Region], offset: float) -> Hypothesis:
    """Untimed text: place the words over the detected speech, in proportion to its length."""
    if hyp.timed or not regions or not hyp.words:
        return hyp
    total = sum(r.length for r in regions) or 1.0
    words: list[HWord] = []
    remaining = list(hyp.words)
    for index, region in enumerate(regions):
        count = len(remaining) if index == len(regions) - 1 else round(len(hyp.words) * region.length / total)
        chunk, remaining = remaining[:count], remaining[count:]
        step = region.length / max(len(chunk), 1)
        for i, w in enumerate(chunk):
            start = offset + region.start + i * step
            words.append(HWord(w.text, round(start, 3), round(start + step, 3), w.confidence, w.speaker))
    return Hypothesis(hyp.source, hyp.label, words, timed=False, language=hyp.language)


def fuse(
    hypotheses: Iterable[Hypothesis],
    span: tuple[float, float],
    glossary: set[str] | None = None,
) -> list[FusedWord]:
    """Fuses every ear's hypothesis for one piece into one scored word list."""
    hyps = [h for h in hypotheses if h.words]
    backbone = choose_backbone(hyps)
    if backbone is None:
        return []
    terms = glossary or set()
    slots = [_Slot(word=w, source=backbone.source) for w in backbone.words]
    inserts: dict[int, list[_Insert]] = {}  # gap index -> words the backbone missed, per ear
    covers: dict[str, list[tuple[float, float]]] = {}
    backbone_cover = _coverage(backbone, span)
    covers[backbone.source] = backbone_cover
    timed_audio = [h for h in hyps if h.timed and h.source != "captions"]
    heard = [r for h in timed_audio for r in _coverage(h, span)] if timed_audio else None
    base = backbone.tokens()

    for slot in slots:
        _vote(slot, backbone.source, slot.word.text, slot.word.confidence, terms)

    for hyp in hyps:
        if hyp is backbone:
            continue
        cover = _coverage(hyp, span, heard)
        covers[hyp.source] = cover
        other = hyp.tokens()
        ops = SequenceMatcher(None, base, other, autojunk=False).get_opcodes()
        for op, i1, i2, j1, j2 in ops:
            if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):
                for k in range(i2 - i1):
                    slot = slots[i1 + k]
                    if _covers(cover, _mid(slot.word)):
                        _vote(slot, hyp.source, hyp.words[j1 + k].text, hyp.words[j1 + k].confidence, terms)
            elif op == "replace":
                phrase = " ".join(w.text for w in hyp.words[j1:j2])
                confidence = min(w.confidence for w in hyp.words[j1:j2])
                for k in range(i1, i2):
                    slot = slots[k]
                    if _covers(cover, _mid(slot.word)):
                        _vote(slot, hyp.source, phrase if k == i1 else "", confidence, terms)
            elif op == "delete":
                for k in range(i1, i2):
                    slot = slots[k]
                    if _covers(cover, _mid(slot.word)):
                        _vote(slot, hyp.source, "", 0.8, terms)
            elif op == "insert":
                group = hyp.words[j1:j2]
                weight = _weight(hyp.source, min(w.confidence for w in group))
                # Words with real times are judged at their own moment; text-only words at the gap.
                at = sum(_mid(w) for w in group) / len(group) if hyp.timed else _gap_time(slots, i1)
                inserts.setdefault(i1, []).append(_Insert(hyp.source, group, weight, at, hyp.timed))

    return _resolve(slots, inserts, covers, terms)


def _vote(slot: _Slot, source: str, text: str, confidence: float, terms: set[str]) -> None:
    key = "".join(norm(t) for t in text.split())
    weight = _weight(source, confidence) + (GLOSSARY_BONUS if key and key in terms else 0.0)
    vote = slot.votes.get(key)
    if vote is None:
        slot.votes[key] = _Vote(weight, text, weight, [source])
    else:
        vote.weight += weight
        vote.sources.append(source)
        if weight > vote.best_source_weight:  # show the spelling of the most trusted ear
            vote.display, vote.best_source_weight = text, weight
    slot.voters.add(source)
    slot.covering_weight += weight


@dataclass
class _Insert:
    source: str
    words: list[HWord]
    weight: float
    at: float  # the moment this ear says the words were spoken
    timed: bool


def _resolve(
    slots: list[_Slot],
    inserts: dict[int, list[_Insert]],
    covers: dict[str, list[tuple[float, float]]],
    terms: set[str],
) -> list[FusedWord]:
    out: list[FusedWord] = []
    for index in range(len(slots) + 1):
        out.extend(_inserted(index, slots, inserts.get(index, []), covers))
        if index == len(slots):
            break
        slot = slots[index]
        key, vote = max(slot.votes.items(), key=lambda kv: kv[1].weight)
        if not key:  # most ears heard nothing here
            continue
        # The vote replaced what the timing ear (backbone) heard: count it as a correction.
        overruled = key != "".join(norm(t) for t in slot.word.text.split())
        total = slot.covering_weight or vote.weight
        confidence = vote.weight / total
        if len(slot.voters) == 1:  # only one ear heard this moment: trust its own score, a little less
            confidence = min(confidence, 0.8 * slot.word.confidence + (0.15 if key in terms else 0.0))
        alternatives = [v.display for k, v in slot.votes.items() if k and k != key]
        pieces = vote.display.split()
        step = (slot.word.end - slot.word.start) / max(len(pieces), 1)
        for i, piece in enumerate(pieces):
            out.append(
                FusedWord(
                    text=piece,
                    start=round(slot.word.start + i * step, 3),
                    end=round(slot.word.start + (i + 1) * step, 3),
                    confidence=round(min(confidence, 1.0), 3),
                    agreement=len(set(vote.sources)),
                    voters=len(slot.voters),
                    sources=sorted(set(vote.sources)),
                    alternatives=alternatives if i == 0 else [],
                    voice=slot.word.speaker,
                    corrected=overruled,
                )
            )
    return out


def _inserted(
    gap: int,
    slots: list[_Slot],
    candidates: list[_Insert],
    covers: dict[str, list[tuple[float, float]]],
) -> list[FusedWord]:
    """Words the backbone missed: kept when most ears that were listening at that moment agree."""
    if not candidates:
        return []
    groups: dict[str, list[_Insert]] = {}
    for candidate in candidates:
        groups.setdefault(" ".join(norm(w.text) for w in candidate.words), []).append(candidate)
    _key, group = max(groups.items(), key=lambda kv: sum(c.weight for c in kv[1]))
    at = sum(c.at for c in group) / len(group)
    support = sum(c.weight for c in group)
    # Every ear listening at that moment votes; the ones that heard nothing vote "no words here".
    listening = {source: _weight(source, 0.9) for source, ranges in covers.items() if _covers(ranges, at)}
    for c in group:
        listening[c.source] = max(listening.get(c.source, 0.0), c.weight)
    total = sum(listening.values()) or 1.0
    if support <= total * 0.5:
        return []
    best = max(group, key=lambda c: c.weight)
    timed = next((c for c in group if c.timed), None)
    before = slots[gap - 1].word.end if gap > 0 else float("-inf")
    after = slots[gap].word.start if gap < len(slots) else float("inf")
    if timed is not None and before - 0.5 <= timed.words[0].start and timed.words[-1].end <= after + 0.5:
        times = [(w.start, w.end) for w in timed.words]  # the ear's own times fit: keep them
        if len(times) != len(best.words):
            times = _spread(times[0][0], times[-1][1], len(best.words))
    else:
        start = before if before != float("-inf") else (after - 0.4 * len(best.words) if after != float("inf") else 0.0)
        end = after if after != float("inf") else start + 0.4 * len(best.words)
        times = _spread(max(0.0, start), max(end, start + 0.2 * len(best.words)), len(best.words))
    sources = sorted({c.source for c in group})
    confidence = round(min(support / total, 1.0) * (0.85 if len(group) == 1 else 1.0), 3)
    return [
        FusedWord(
            text=w.text,
            start=round(a, 3),
            end=round(b, 3),
            confidence=confidence,
            agreement=len(sources),
            voters=max(len(listening), len(group)),
            sources=sources,
            voice=w.speaker if w.speaker and w.speaker.startswith("dg:") else None,
        )
        for w, (a, b) in zip(best.words, times, strict=True)
    ]


def _spread(start: float, end: float, count: int) -> list[tuple[float, float]]:
    step = (end - start) / max(count, 1)
    return [(start + i * step, start + (i + 1) * step) for i in range(count)]


def _mid(word: HWord) -> float:
    return (word.start + word.end) / 2


def _gap_time(slots: list[_Slot], gap: int) -> float:
    if not slots:
        return 0.0
    if gap == 0:
        return slots[0].word.start
    if gap == len(slots):
        return slots[-1].word.end
    return (slots[gap - 1].word.end + slots[gap].word.start) / 2
