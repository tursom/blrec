"""HTTP history status, filtered issue bundle export, and cleanup endpoints."""

import os
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ...application import Application
from ...exception import NotFoundError
from ...http_history import NoHistoryRecords
from ..schemas import HttpHistoryStatusResponse, ResponseMessage

app: Application = None  # type: ignore

router = APIRouter(prefix='/api/v1/http-history', tags=['http-history'])


@router.get('/status', response_model=HttpHistoryStatusResponse)
async def get_http_history_status() -> HttpHistoryStatusResponse:
    return HttpHistoryStatusResponse(**asdict(await app.get_http_history_status()))


@router.get('/export')
async def export_http_history(
    room_id: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> FileResponse:
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


@router.delete('', response_model=ResponseMessage)
async def clear_http_history() -> ResponseMessage:
    await app.clear_http_history()
    return ResponseMessage(message='HTTP history has been cleared')
