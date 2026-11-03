"""API endpoints: /health, /auth/check, /transcribe, /analyze and /export.

/transcribe runs accuracy layers 1-8 on one Record-here piece (see app/pipeline);
/analyze runs layers 9-12 and adds the quality score.
"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import ValidationError

from app.api.deps import client_id, get_notes_writer, get_piece_memory, get_usage_limiter, verify_app_token
from app.config import Settings, get_settings
from app.exporters.bundle import FileKind, build_one
from app.exporters.common import build_view
from app.pipeline import glossary_terms, learned_terms, to_segments
from app.pipeline.ears import EarContext
from app.pipeline.glossary import GlossaryFixer
from app.pipeline.memory import PieceMemory
from app.pipeline.scores import QualityInputs
from app.pipeline.transcriber import Transcriber
from app.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    AuthCheckResponse,
    ExportRequest,
    HealthResponse,
    TranscribeResponse,
)
from app.services.notes import AnalysisError, EmptyTranscriptError, NotesExtras, NotesWriter
from app.services.stt_common import Track
from app.services.usage import UsageLimiter

router = APIRouter()

ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg", "audio/mp4", "video/webm"}


@router.get("/health", response_model=HealthResponse)
async def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(version=settings.app_version)


@router.get("/auth/check", response_model=AuthCheckResponse, dependencies=[Depends(verify_app_token)])
async def auth_check(
    settings: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[UsageLimiter, Depends(get_usage_limiter)],
    client: Annotated[str, Depends(client_id)],
) -> AuthCheckResponse:
    """Lets the extension confirm its token and see today's remaining minutes, without using quota."""
    stt = Transcriber(settings)
    await stt.aclose()
    return AuthCheckResponse(
        speech_to_text_ready=stt.ready,
        speech_engine=stt.label,
        summary_ready=bool(settings.gemini.api_key or (settings.groq.api_key and settings.groq.summary_fallback_model)),
        minutes_left_today=round(limiter.remaining(client) / 60, 1),
    )


@router.post(
    "/transcribe",
    response_model=TranscribeResponse,
    dependencies=[Depends(verify_app_token)],
)
async def transcribe(
    settings: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[UsageLimiter, Depends(get_usage_limiter)],
    memory: Annotated[PieceMemory, Depends(get_piece_memory)],
    client: Annotated[str, Depends(client_id)],
    audio: Annotated[UploadFile, File(description="One audio chunk (webm/opus)")],
    chunk_index: Annotated[int, Form(ge=0)],
    offset_seconds: Annotated[float, Form(ge=0)],
    language: Annotated[str | None, Form(max_length=8)] = None,
    track: Annotated[Track, Form()] = "mix",
    names: Annotated[str | None, Form(max_length=2000, description="Participant names, one per line")] = None,
    prompt: Annotated[str | None, Form(max_length=2000, description="Older extensions: names as free text")] = None,
) -> TranscribeResponse:
    base_type = (audio.content_type or "").split(";")[0].strip()
    if base_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"Unsupported audio type: {base_type or 'unknown'}")

    if limiter.remaining(client) <= 0:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Daily transcription limit reached. Try again tomorrow.")

    data = await audio.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Audio chunk is larger than {settings.limits.max_chunk_mb} MB."
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Audio chunk is empty.")

    people = [n.strip() for n in (names or "").splitlines() if n.strip()][:50]
    terms = glossary_terms(settings)
    context = EarContext(terms=terms, names=people, language=language or None, track=track, piece=chunk_index)
    transcriber = Transcriber(settings)
    try:
        if not transcriber.ready:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Server has no speech-to-text key.")
        hearing = await transcriber.hear(data, base_type, offset_seconds, context, terms)
        if not hearing.skipped and not any(h.words for h in hearing.hypotheses) and hearing.failures:
            # Every ear failed: the extension keeps the piece and retries.
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "; ".join(hearing.failures))
        fixer = GlossaryFixer(terms, settings.pipeline.doubt_below)
        words = transcriber.assemble([hearing], [], terms, fixer=fixer)
    finally:
        await transcriber.aclose()

    if settings.vocabulary.learn:
        learned_terms().remember(fixer.fixes)
    limiter.record(client, hearing.duration)
    memory.remember(
        client,
        track,
        words,
        settings.pipeline.doubt_below,
        seconds=hearing.duration,
        covered=hearing.duration,
        ears=hearing.ears,
        failures=hearing.failures,
    )
    return TranscribeResponse(
        chunk_index=chunk_index,
        offset_seconds=offset_seconds,
        duration_seconds=hearing.duration,
        language=next((h.language for h in hearing.hypotheses if h.language), None),
        engine=" + ".join(hearing.ears) or transcriber.label,
        segments=to_segments(words),
    )


@router.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(verify_app_token)])
async def analyze(
    request: AnalyzeRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    writer: Annotated[NotesWriter, Depends(get_notes_writer)],
    memory: Annotated[PieceMemory, Depends(get_piece_memory)],
    client: Annotated[str, Depends(client_id)],
) -> AnalyzeResponse:
    """Layers 9-12: doubtful words, facts first, notes, second-opinion check, quality score."""
    since = request.meeting.started_at.timestamp() - 120
    recall = memory.recall(client, since)
    duration = request.meeting.duration_seconds or recall.meeting_seconds
    extras = NotesExtras(
        doubts=recall.doubts,
        glossary=glossary_terms(settings),
        quality=QualityInputs(
            meeting_seconds=duration,
            covered_seconds=min(recall.covered_seconds, duration) if recall.meeting_seconds else duration,
            ears=recall.ears,
            fallbacks=_condense(recall.failures),
            word_counts=(recall.confident, recall.words) if recall.words else None,
            doubtful=len(recall.doubts),
            corrected=recall.corrected,
        ),
    )
    try:
        return await writer.write(request, extras)
    except EmptyTranscriptError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except AnalysisError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


def _condense(failures: list[str]) -> list[str]:
    """ "Gemini could not transcribe a piece (...)" x5 -> one line per engine."""
    engines = list(dict.fromkeys(f.split(" could not")[0] for f in failures))
    return [f"{name} failed on some pieces; the other ears covered them." for name in engines]


JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG"
MAX_SCREENSHOT_BYTES = 3 * 1024 * 1024


@router.post("/export", dependencies=[Depends(verify_app_token), Depends(client_id)])
async def export(
    settings: Annotated[Settings, Depends(get_settings)],
    payload: Annotated[str, Form(description="ExportRequest as JSON")],
    screenshots: Annotated[list[UploadFile] | None, File()] = None,
    kind: Annotated[FileKind, Query()] = "zip",
) -> Response:
    """Builds the Word report, Excel tracker, MOM.md, README.md and transcript (one file or a zip)."""
    try:
        request = ExportRequest.model_validate_json(payload)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid export data: {exc.errors()[0]['msg']}"
        ) from exc

    files = screenshots or []
    if len(files) > settings.limits.max_screenshots:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"At most {settings.limits.max_screenshots} screenshots."
        )
    expected = {shot.name for shot in request.screenshots}
    images: dict[str, bytes] = {}
    for upload in files:
        data = await upload.read(MAX_SCREENSHOT_BYTES + 1)
        name = upload.filename or ""
        if name not in expected or len(data) > MAX_SCREENSHOT_BYTES:
            continue  # unknown or oversized file: skip it rather than fail the whole report
        if data.startswith(JPEG_MAGIC) or data.startswith(PNG_MAGIC):
            images[name] = data
    request.screenshots = [shot for shot in request.screenshots if shot.name in images]

    built = build_one(build_view(request), kind, images)
    disposition = f"attachment; filename*=UTF-8''{quote(built.filename)}"
    return Response(content=built.content, media_type=built.media_type, headers={"Content-Disposition": disposition})
