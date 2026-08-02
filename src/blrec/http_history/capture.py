"""Normalize outbound HTTP attempts into the versioned history record shape."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .sanitizer import sanitize_record
from .store import HttpHistoryStore

__all__ = ('record_http_exchange',)


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
    response_status: Optional[int] = None,
    response_headers: Optional[Mapping[str, Any]] = None,
    response_body: Any = None,
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
        record = {
            'record_type': 'http_exchange',
            'recorded_at': datetime.now(timezone.utc).isoformat(),
            'room_id': room_id,
            'category': category,
            'operation_id': operation_id,
            'parent_operation_id': parent_operation_id,
            'duration_ms': round(duration_ms, 3) if duration_ms is not None else None,
            'request': {
                'method': method,
                'url': url,
                'headers': dict(request_headers or {}),
            },
            'response': {
                'status': response_status,
                'headers': dict(response_headers or {}),
            },
            'outcome': 'error' if error is not None else 'success',
            'error': f'{type(error).__name__}: {error}' if error is not None else None,
        }
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
        store.record(record)
    except Exception as exc:
        store.report_error(exc, dropped=True)
