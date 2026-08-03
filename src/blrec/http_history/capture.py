"""Normalize outbound HTTP attempts into the versioned history record shape."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from .sanitizer import sanitize_record
from .store import HttpHistoryStore

__all__ = ('mark_http_incident', 'record_http_exchange', 'redirects_from_response')


def redirects_from_response(response: object) -> list[dict]:
    if response is None:
        return []
    result = []
    history = getattr(response, 'history', ())
    try:
        iterator = iter(history)
    except TypeError:
        return []
    for item in iterator:
        headers = getattr(item, 'headers', {})
        result.append(
            {
                'status': getattr(item, 'status_code', getattr(item, 'status', None)),
                'url': str(getattr(item, 'url', '')),
                'location': (
                    headers.get('Location') if hasattr(headers, 'get') else None
                ),
            }
        )
    return result


def mark_http_incident(
    store: Optional[HttpHistoryStore],
    *,
    room_id: int,
    kind: str,
    operation_id: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
) -> Optional[str]:
    if store is None:
        return None
    try:
        return store.mark_incident(
            room_id=room_id,
            kind=kind,
            operation_id=operation_id,
            details=details,
            occurred_at=occurred_at,
        )
    except Exception as exc:
        report_error = getattr(store, 'report_error', None)
        if callable(report_error):
            report_error(exc, dropped=True)
        return None


def _serializable_body(body: Any) -> tuple[Any, bytes]:
    if isinstance(body, bytes):
        text = body.decode('utf8', errors='replace')
        return text, body
    if isinstance(body, str):
        return body, body.encode('utf8', errors='replace')
    text = json.dumps(body, ensure_ascii=False, separators=(',', ':'), default=str)
    return json.loads(text), text.encode('utf8')


def _bounded_body(body: Any, max_size: int) -> tuple[Any, str, bool]:
    normalized, payload = _serializable_body(body)
    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) <= max_size:
        return normalized, digest, False
    text = payload[:max_size].decode('utf8', errors='replace')
    return text, digest, True


def record_http_exchange(
    store: Optional[HttpHistoryStore],
    *,
    room_id: Optional[int],
    category: str,
    method: str,
    url: str,
    request_headers: Optional[Mapping[str, Any]] = None,
    request_body: Any = None,
    response_status: Optional[int] = None,
    response_headers: Optional[Mapping[str, Any]] = None,
    response_body: Any = None,
    response_payload: Optional[bytes] = None,
    payload_role: Optional[str] = None,
    redirects: Optional[Iterable[Mapping[str, Any]]] = None,
    error: Optional[BaseException] = None,
    duration_ms: Optional[float] = None,
    operation_id: Optional[str] = None,
    parent_operation_id: Optional[str] = None,
    max_body_size: int = 2 * 1024 * 1024,
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    if store is None:
        return
    try:
        if operation_id is None:
            operation_id = uuid.uuid4().hex
        safe_request_headers = (
            dict(request_headers) if isinstance(request_headers, Mapping) else {}
        )
        safe_response_headers = (
            dict(response_headers) if isinstance(response_headers, Mapping) else {}
        )
        completed_at = datetime.now(timezone.utc)
        started_at = (
            completed_at - timedelta(milliseconds=duration_ms)
            if duration_ms is not None
            else completed_at
        )
        record = {
            'record_type': 'http_exchange',
            'recorded_at': completed_at.isoformat(),
            'exchange_id': uuid.uuid4().hex,
            'request_started_at': started_at.isoformat(),
            'response_completed_at': completed_at.isoformat(),
            'room_id': room_id,
            'category': category,
            'operation_id': operation_id,
            'parent_operation_id': parent_operation_id,
            'duration_ms': round(duration_ms, 3) if duration_ms is not None else None,
            'request': {'method': method, 'url': url, 'headers': safe_request_headers},
            'response': {'status': response_status, 'headers': safe_response_headers},
            'outcome': 'error' if error is not None else 'success',
            'error': f'{type(error).__name__}: {error}' if error is not None else None,
        }
        if request_body is not None:
            record['request']['body'] = request_body
        if redirects:
            record['redirects'] = [dict(item) for item in redirects]
        if response_body is not None:
            record['response']['body'] = response_body
        if extra:
            record.update(extra)
        record = sanitize_record(record)
        if response_body is not None:
            body, digest, truncated = _bounded_body(
                record['response']['body'], max_body_size
            )
            record['response'].update(
                body=body, body_sha256=digest, body_truncated=truncated
            )
        content_type = None
        for name, value in safe_response_headers.items():
            if name.casefold() == 'content-type':
                content_type = str(value)
                break
        store.record(
            record,
            payload=response_payload,
            payload_role=payload_role,
            payload_content_type=content_type,
        )
    except Exception as exc:
        store.report_error(exc, dropped=True)
