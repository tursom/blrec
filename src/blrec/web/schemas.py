from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

__all__ = (
    'ResponseMessage',
    'HttpHistoryStatusResponse',
    'HttpIncidentSummaryResponse',
    'DataSelection',
    'AliasKeyOfSettings',
)


class ResponseMessage(BaseModel):
    code: int = 0
    message: str = ''
    data: Optional[Dict[str, Any]] = None


class HttpHistoryStatusResponse(BaseModel):
    enabled: bool
    record_count: int
    total_size: int
    oldest_at: Optional[datetime]
    newest_at: Optional[datetime]
    room_ids: List[int]
    dropped_records: int
    last_error: Optional[str]
    incident_count: int
    active_incident_count: int
    payload_size: int


class HttpIncidentSummaryResponse(BaseModel):
    incident_id: str
    room_id: int
    kind: str
    first_at: datetime
    last_at: datetime
    occurrence_count: int
    status: str
    record_count: int
    payload_size: int
    partial: bool


class DataSelection(str, Enum):
    ALL = 'all'

    # live status
    PREPARING = 'preparing'
    LIVING = 'living'
    ROUNDING = 'rounding'

    # task status
    MONITOR_ENABLED = 'monitor_enabled'
    MONITOR_DISABLED = 'monitor_disabled'
    RECORDER_ENABLED = 'recorder_enabled'
    RECORDER_DISABLED = 'recorder_disabled'

    # task running status
    STOPPED = 'stopped'
    WAITTING = 'waitting'
    RECORDING = 'recording'
    REMUXING = 'remuxing'
    INJECTING = 'injecting'


AliasKeyOfSettings = Literal[
    'version',
    'tasks',
    'output',
    'logging',
    'httpHistory',
    'biliApi',
    'header',
    'danmaku',
    'recorder',
    'postprocessing',
    'space',
    'emailNotification',
    'serverchanNotification',
    'pushdeerNotification',
    'pushplusNotification',
    'telegramNotification',
    'barkNotification',
    'webhooks',
]
