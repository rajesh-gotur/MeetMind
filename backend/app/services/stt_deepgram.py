"""Deepgram Nova-3 request settings: custom key terms, word times and speaker detection.

The request itself is made by the Deepgram ear (app/pipeline/ears.py).
"""

from __future__ import annotations

from app.config import Settings
from app.services.stt_common import SpeechRequest, unique_terms

# Deepgram accepts up to 100 key terms per request.
MAX_KEYTERMS = 100


def query_params(settings: Settings, request: SpeechRequest) -> list[tuple[str, str]]:
    deepgram = settings.deepgram
    language = request.language or deepgram.auto_language
    params: list[tuple[str, str]] = [
        ("model", deepgram.model),
        ("language", language),
        ("smart_format", "true"),
        ("punctuate", "true"),
        ("utterances", "true"),
        # Only the call audio can hold several voices; the mic track is always the recorder.
        ("diarize", "false" if request.track == "mic" else "true"),
    ]
    for term in unique_terms(request.names, request.terms, limit=MAX_KEYTERMS):
        params.append(("keyterm", term))
    return params
