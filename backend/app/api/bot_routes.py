"""Bot endpoints: the extension sends the bot to a meeting, follows it live, and downloads the files."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

from app.api.deps import client_id, get_bot_manager, verify_app_token
from app.bot.manager import BotManager, BotUnavailableError
from app.bot.session import BotRequest
from app.exporters.bundle import FileKind, build_one
from app.exporters.common import build_view
from app.schemas import BotStartRequest, BotStopRequest, ExportRequest

router = APIRouter(prefix="/bot", tags=["bot"])
LIVE_DIR = (Path(__file__).resolve().parent.parent / "bot" / "live").resolve()
Manager = Annotated[BotManager, Depends(get_bot_manager)]
Protected = [Depends(verify_app_token), Depends(client_id)]


@router.get("/status", dependencies=Protected)
def bot_status(manager: Manager, tail: Annotated[int, Query(ge=0, le=500)] = 0) -> dict[str, Any]:
    """Everything the popup and the live view show. ``tail`` limits the transcript lines (0 = all)."""
    data = manager.status.snapshot(captions_tail=tail or None)
    reason = manager.unavailable_reason()
    data["available"] = not reason
    data["unavailable_reason"] = reason
    return data


@router.post("/start", dependencies=[Depends(verify_app_token)])
def bot_start(body: BotStartRequest, manager: Manager, client: Annotated[str, Depends(client_id)]) -> dict[str, Any]:
    try:
        manager.start(
            BotRequest(
                meet_url=body.meet_url.strip(),
                language=body.language,
                requested_by=body.your_name.strip(),
                client_id=client,
            )
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except BotUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"ok": True}


@router.post("/stop", dependencies=Protected)
def bot_stop(manager: Manager, body: BotStopRequest | None = None) -> dict[str, bool]:
    """Stops recording and writes the notes. No body = the Stop button."""
    manager.stop((body or BotStopRequest()).reason)
    return {"ok": True}


@router.post("/renotes", dependencies=Protected)
def bot_renotes(manager: Manager) -> dict[str, bool]:
    """ "Create notes again" from the last saved meeting (the speech engines are not called again)."""
    try:
        manager.renotes()
    except BotUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"ok": True}


@router.post("/reset", dependencies=Protected)
def bot_reset(manager: Manager) -> dict[str, bool]:
    try:
        manager.reset()
    except BotUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"ok": True}


@router.get("/files", dependencies=Protected)
def bot_files(manager: Manager, kind: Annotated[FileKind, Query()] = "zip") -> Response:
    """The same files as an extension recording: zip, Word, Excel, MOM.md, README.md or transcript."""
    result = manager.result
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The bot has no finished notes yet.")
    request = ExportRequest(
        meeting=result.meeting,
        participants=result.notes.participants,
        turns=result.notes.turns,
        report=result.notes.report,
        screenshots=[],
    )
    built = build_one(build_view(request), kind, {})
    disposition = f"attachment; filename*=UTF-8''{quote(built.filename)}"
    return Response(content=built.content, media_type=built.media_type, headers={"Content-Disposition": disposition})


@router.get("/live", include_in_schema=False)
def live_view() -> FileResponse:
    """Full-page live view of the bot (the popup opens it; data still needs the app token)."""
    return FileResponse(LIVE_DIR / "index.html")


@router.get("/live/{path:path}", include_in_schema=False)
def live_asset(path: str) -> FileResponse:
    file = (LIVE_DIR / path).resolve()
    if LIVE_DIR not in file.parents or not file.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return FileResponse(file)
