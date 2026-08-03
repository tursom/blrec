"""HTTP history status, filtered issue bundle export, and cleanup endpoints."""

import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ...application import Application
from ...exception import NotFoundError
from ...http_history import NoHistoryRecords
from ..schemas import (
    HttpHistoryStatusResponse,
    HttpIncidentSummaryResponse,
    ResponseMessage,
)

app: Application = None  # type: ignore

router = APIRouter(prefix='/api/v1/http-history', tags=['http-history'])


def _validate_utc_range(since: Optional[datetime], until: Optional[datetime]) -> None:
    for name, value in (('since', since), ('until', until)):
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != timedelta(0)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f'{name} must be an RFC 3339 UTC timestamp',
            )
    if since is not None and until is not None and since >= until:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='since must be earlier than until',
        )


@router.get('/status', response_model=HttpHistoryStatusResponse)
async def get_http_history_status() -> HttpHistoryStatusResponse:
    return HttpHistoryStatusResponse(**asdict(await app.get_http_history_status()))


@router.get('/export')
async def export_http_history(
    room_id: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> FileResponse:
    _validate_utc_range(since, until)
    try:
        result = await app.export_http_history(
            room_id=room_id, since=since, until=until
        )
    except NoHistoryRecords as exc:
        raise NotFoundError(str(exc))
    return FileResponse(
        result.path,
        media_type='application/zip',
        filename=result.filename,
        background=BackgroundTask(os.remove, result.path),
    )


@router.get('/incidents', response_model=List[HttpIncidentSummaryResponse])
async def list_http_incidents(
    room_id: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> List[HttpIncidentSummaryResponse]:
    _validate_utc_range(since, until)
    incidents = await app.list_http_incidents(room_id=room_id, since=since, until=until)
    return [HttpIncidentSummaryResponse(**asdict(item)) for item in incidents]


@router.get('/incidents/{incident_id}/export')
async def export_http_incident(incident_id: str) -> FileResponse:
    if re.fullmatch(r'[0-9a-f]{32}', incident_id) is None:
        raise NotFoundError(f'Unknown HTTP incident: {incident_id}')
    try:
        result = await app.export_http_incident(incident_id)
    except KeyError as exc:
        raise NotFoundError(str(exc))
    return FileResponse(
        result.path,
        media_type='application/zip',
        filename=result.filename,
        background=BackgroundTask(os.remove, result.path),
    )


@router.delete('', response_model=ResponseMessage)
async def clear_http_history() -> ResponseMessage:
    await app.clear_http_history()
    return ResponseMessage(message='HTTP history has been cleared')
