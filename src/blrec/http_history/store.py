"""Bounded JSONL persistence and issue-safe ZIP export for HTTP history."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, TextIO, Union

from loguru import logger

from blrec import __prog__, __version__

from .sanitizer import sanitize_record

__all__ = (
    'HttpHistoryExport',
    'HttpIncidentSummary',
    'HttpHistoryStatus',
    'HttpHistoryStore',
    'NoHistoryRecords',
)

SCHEMA_VERSION = 2
DEFAULT_SEGMENT_SIZE = 5 * 1024 * 1024
DEFAULT_MAX_SIZE = 500 * 1024 * 1024
_QUEUE_SIZE = 10000
_QUEUED_PAYLOAD_LIMIT = 64 * 1024 * 1024
_WARNING_INTERVAL = 60.0
_INCIDENT_PRE_SECONDS = 30
_INCIDENT_POST_SECONDS = 10
_INCIDENT_MERGE_SECONDS = 5 * 60
_INCIDENT_PAYLOAD_LIMIT = 128 * 1024 * 1024
_INCIDENT_RESOURCE_LIMIT = 128 * 1024 * 1024


class NoHistoryRecords(Exception):
    pass


@dataclass(frozen=True)
class HttpHistoryStatus:
    enabled: bool
    record_count: int
    total_size: int
    oldest_at: Optional[str]
    newest_at: Optional[str]
    room_ids: List[int]
    dropped_records: int
    last_error: Optional[str]
    incident_count: int = 0
    active_incident_count: int = 0
    payload_size: int = 0


@dataclass(frozen=True)
class HttpHistoryExport:
    path: str
    filename: str
    record_count: int


@dataclass(frozen=True)
class HttpIncidentSummary:
    incident_id: str
    room_id: int
    kind: str
    first_at: str
    last_at: str
    occurrence_count: int
    status: str
    record_count: int
    payload_size: int
    partial: bool


@dataclass(frozen=True)
class _Snapshot:
    directory: Path
    paths: List[Path]


@dataclass(frozen=True)
class _IncidentSnapshot:
    incident: Dict[str, Any]
    payload_paths: Dict[str, Path]


@dataclass
class _Command:
    name: str
    value: Any = None
    event: threading.Event = None  # type: ignore
    result: Any = None
    error: Optional[BaseException] = None

    def __post_init__(self) -> None:
        if self.event is None:
            self.event = threading.Event()


@dataclass(frozen=True)
class _RecordItem:
    record: Dict[str, Any]
    payload: Optional[bytes] = None
    payload_role: Optional[str] = None
    payload_content_type: Optional[str] = None


@dataclass(frozen=True)
class _IncidentMarker:
    incident_id: str
    room_id: int
    kind: str
    occurred_at: str
    operation_id: Optional[str]
    details: Dict[str, Any]


QueueItem = Union[_RecordItem, _IncidentMarker, _Command]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class HttpHistoryStore:
    """Serialize records on a writer thread without blocking network paths."""

    def __init__(
        self,
        directory: str,
        *,
        enabled: bool = True,
        retention_days: int = 7,
        max_size: int = DEFAULT_MAX_SIZE,
        segment_size: int = DEFAULT_SEGMENT_SIZE,
    ) -> None:
        self._directory = Path(directory)
        self._enabled = enabled
        self._retention_days = retention_days
        self._max_size = max_size
        self._segment_size = segment_size
        self._queue: queue.Queue[QueueItem] = queue.Queue(maxsize=_QUEUE_SIZE)
        self._metadata_fallback: queue.SimpleQueue[
            Union[_RecordItem, _IncidentMarker]
        ] = queue.SimpleQueue()
        self._state_lock = threading.Lock()
        self._started = False
        self._accepting = False
        self._dropped_records = 0
        self._last_error: Optional[str] = None
        self._last_warning_at = 0.0
        self._record_count = 0
        self._total_size = 0
        self._oldest_at: Optional[str] = None
        self._newest_at: Optional[str] = None
        self._room_ids: set[int] = set()
        self._incident_count = 0
        self._active_incident_count = 0
        self._payload_size = 0
        self._operation_attempts: OrderedDict[str, int] = OrderedDict()
        self._incident_lookup: Dict[tuple[int, str], tuple[str, datetime]] = {}
        self._incidents: Dict[str, Dict[str, Any]] = {}
        self._incident_record_ids: Dict[str, set[str]] = {}
        self._payload_ref_counts: Dict[str, int] = {}
        self._queued_payload_bytes = 0
        self._sequence = 0
        self._file: Optional[TextIO] = None
        self._path: Optional[Path] = None

    @property
    def directory(self) -> str:
        return str(self._directory)

    def start(self) -> None:
        if self._started:
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_error(exc)
        self._started = True
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(ready,), name='HttpHistoryWriter', daemon=True
        )
        self._thread.start()
        ready.wait()
        self._accepting = True

    def close(self) -> None:
        if not self._started:
            return
        self._accepting = False
        try:
            self._command('stop')
        except Exception:
            pass
        self._thread.join()
        self._started = False

    def set_directory(self, directory: str) -> None:
        path = Path(directory)
        if path == self._directory:
            return
        was_started = self._started
        if was_started:
            self.close()
        self._directory = path
        if was_started:
            self.start()

    def configure(self, *, enabled: bool, retention_days: int, max_size: int) -> None:
        self._command(
            'configure',
            {
                'enabled': enabled,
                'retention_days': retention_days,
                'max_size': max_size,
            },
        )

    def record(
        self,
        record: Mapping[str, Any],
        *,
        payload: Optional[bytes] = None,
        payload_role: Optional[str] = None,
        payload_content_type: Optional[str] = None,
    ) -> None:
        if not self._enabled:
            return
        if not self._accepting:
            if self._started:
                self.report_error(
                    RuntimeError('HTTP history store is switching directories'),
                    dropped=True,
                )
            return
        try:
            item = sanitize_record(record)
        except Exception as exc:
            self._set_error(exc, dropped=True)
            return
        item.setdefault('schema_version', SCHEMA_VERSION)
        item.setdefault('record_id', uuid.uuid4().hex)
        item.setdefault('recorded_at', _utc_now().isoformat())
        item.setdefault('response_completed_at', item['recorded_at'])
        operation_id = item.get('operation_id')
        attempt = 1
        if isinstance(operation_id, str) and operation_id:
            with self._state_lock:
                attempt = self._operation_attempts.pop(operation_id, 0) + 1
                self._operation_attempts[operation_id] = attempt
                if len(self._operation_attempts) > 10000:
                    self._operation_attempts.popitem(last=False)
        item.setdefault('retry', {'attempt': attempt, 'is_retry': attempt > 1})

        accepted_payload = payload
        payload_digest = (
            hashlib.sha256(payload).hexdigest() if payload is not None else None
        )
        if payload is not None:
            with self._state_lock:
                payload_limit = min(self._max_size, _INCIDENT_PAYLOAD_LIMIT)
                if (
                    len(payload) > payload_limit
                    or self._queued_payload_bytes + len(payload) > _QUEUED_PAYLOAD_LIMIT
                ):
                    accepted_payload = None
                    self._dropped_records += 1
                else:
                    self._queued_payload_bytes += len(payload)
            if accepted_payload is None:
                response = item.setdefault('response', {})
                response['payload'] = {
                    'sha256': payload_digest,
                    'size': len(payload),
                    'role': payload_role,
                    'content_type': payload_content_type,
                    'omitted': True,
                    'omitted_reason': 'capture queue or size limit exceeded',
                }
        with self._state_lock:
            self._sequence += 1
            item.setdefault('sequence', self._sequence)
        try:
            self._queue.put_nowait(
                _RecordItem(item, accepted_payload, payload_role, payload_content_type)
            )
        except queue.Full:
            if accepted_payload is not None:
                with self._state_lock:
                    self._queued_payload_bytes -= len(accepted_payload)
                response = item.setdefault('response', {})
                response['payload'] = {
                    'sha256': payload_digest,
                    'size': len(accepted_payload),
                    'role': payload_role,
                    'content_type': payload_content_type,
                    'omitted': True,
                    'omitted_reason': 'capture queue is full',
                }
                self.report_error(
                    RuntimeError('HTTP history payload queue is full'), dropped=True
                )
            self._metadata_fallback.put(_RecordItem(item))

    def mark_incident(
        self,
        *,
        room_id: int,
        kind: str,
        operation_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        occurred_at: Optional[datetime] = None,
    ) -> str:
        """异步冻结错误窗口，并立即返回稳定的事件 ID。"""

        now = (occurred_at or _utc_now()).astimezone(timezone.utc)
        key = (room_id, kind)
        with self._state_lock:
            previous = self._incident_lookup.get(key)
            if (
                previous is not None
                and (now - previous[1]).total_seconds() <= _INCIDENT_MERGE_SECONDS
            ):
                incident_id = previous[0]
            else:
                incident_id = uuid.uuid4().hex
            self._incident_lookup[key] = (incident_id, now)

        marker = _IncidentMarker(
            incident_id=incident_id,
            room_id=room_id,
            kind=kind,
            occurred_at=now.isoformat(),
            operation_id=operation_id,
            details=sanitize_record(dict(details or {})),
        )
        try:
            self._queue.put_nowait(marker)
        except queue.Full:
            self._metadata_fallback.put(marker)
        return incident_id

    def list_incidents(
        self,
        *,
        room_id: Optional[int] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[HttpIncidentSummary]:
        return self._command(
            'list_incidents', {'room_id': room_id, 'since': since, 'until': until}
        )

    def export_incident(self, incident_id: str) -> HttpHistoryExport:
        snapshot = self._command('incident_snapshot', incident_id)
        assert isinstance(snapshot, _IncidentSnapshot)
        incident = sanitize_record(snapshot.incident)
        exported_at = _utc_now()
        filename = (
            f'blrec-http-incident-{incident_id}-'
            f'{exported_at.strftime("%Y%m%dT%H%M%SZ")}.zip'
        )
        fd, path = tempfile.mkstemp(
            prefix='.http-incident-export-', suffix='.zip', dir=self._directory
        )
        os.close(fd)
        selected_record_ids = set(
            incident.get(
                'selected_record_ids',
                [record.get('record_id') for record in incident['records']],
            )
        )
        records = sorted(
            (
                sanitize_record(record)
                for record in incident['records']
                if record.get('record_id') in selected_record_ids
            ),
            key=lambda item: item['sequence'],
        )
        selected_detail_indexes = set(
            incident.get(
                'selected_detail_indexes', range(len(incident.get('details', [])))
            )
        )
        incident_manifest = {
            key: value
            for key, value in incident.items()
            if key
            not in (
                'records',
                'payloads',
                'omitted_payloads',
                'omitted_records',
                'omitted_details',
                'selected_record_ids',
                'selected_detail_indexes',
                'resource_size',
                'forced_partial',
            )
        }
        incident_manifest['details'] = [
            detail
            for index, detail in enumerate(incident.get('details', []))
            if index in selected_detail_indexes
        ]
        manifest = {
            'schema_version': SCHEMA_VERSION,
            'bundle_kind': 'incident',
            'exported_at': exported_at.isoformat(),
            'application': {
                'name': __prog__,
                'version': __version__,
                'python': platform.python_version(),
                'operating_system': platform.platform(),
            },
            'incident': incident_manifest,
            'capture': {
                'pre_seconds': _INCIDENT_PRE_SECONDS,
                'post_seconds': _INCIDENT_POST_SECONDS,
                'payload_limit': _INCIDENT_PAYLOAD_LIMIT,
                'resource_limit': _INCIDENT_RESOURCE_LIMIT,
                'resource_size': incident.get('resource_size', 0),
            },
            'payloads': list(incident['payloads'].values()),
            'omitted_details': incident.get('omitted_details', []),
            'omitted_records': incident.get('omitted_records', []),
            'omitted_payloads': incident.get('omitted_payloads', []),
        }
        readme = (
            'blrec HTTP incident bundle\n\n'
            'records.jsonl contains sanitized HTTP exchanges in completion order.\n'
            'payloads/ contains raw HLS bytes addressed by SHA256.\n'
            'Raw HLS payloads may contain copyrighted or private audio/video.\n'
        )
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2)
                )
                archive.writestr(
                    'records.jsonl',
                    ''.join(
                        json.dumps(record, ensure_ascii=False, separators=(',', ':'))
                        + '\n'
                        for record in records
                    ),
                )
                archive.writestr('README.txt', readme)
                for digest, payload_path in snapshot.payload_paths.items():
                    archive.write(payload_path, f'payloads/{digest}.bin')
        except Exception as exc:
            Path(path).unlink(missing_ok=True)
            self._set_error(exc)
            raise
        return HttpHistoryExport(
            path=path, filename=filename, record_count=len(records)
        )

    def report_error(self, exc: BaseException, *, dropped: bool = False) -> None:
        self._set_error(exc, dropped=dropped)

    def flush(self) -> None:
        self._command('flush')

    def clear(self) -> None:
        self._command('clear')

    def status(self) -> HttpHistoryStatus:
        if self._started:
            try:
                self.flush()
            except Exception:
                pass
        with self._state_lock:
            return HttpHistoryStatus(
                enabled=self._enabled,
                record_count=self._record_count,
                total_size=self._total_size,
                oldest_at=self._oldest_at,
                newest_at=self._newest_at,
                room_ids=sorted(self._room_ids),
                dropped_records=self._dropped_records,
                last_error=self._last_error,
                incident_count=self._incident_count,
                active_incident_count=self._active_incident_count,
                payload_size=self._payload_size,
            )

    def export(
        self,
        *,
        room_id: Optional[int] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> HttpHistoryExport:
        snapshot = self._command('snapshot')
        records = []
        try:
            for path in snapshot.paths:
                with open(path, 'rt', encoding='utf8') as source:
                    for line in source:
                        try:
                            record = sanitize_record(json.loads(line))
                            timestamp = _parse_datetime(record['recorded_at'])
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                            continue
                        if room_id is not None and record.get('room_id') != room_id:
                            continue
                        if since is not None and timestamp < since.astimezone(
                            timezone.utc
                        ):
                            continue
                        if until is not None and timestamp > until.astimezone(
                            timezone.utc
                        ):
                            continue
                        records.append((timestamp, record))
        finally:
            shutil.rmtree(snapshot.directory, ignore_errors=True)

        if not records:
            raise NoHistoryRecords('No HTTP history records match the filter')
        records.sort(key=lambda item: item[0])

        exported_at = _utc_now()
        filename = f'blrec-http-history-{exported_at.strftime("%Y%m%dT%H%M%SZ")}.zip'
        try:
            fd, path = tempfile.mkstemp(
                prefix='.http-history-export-', suffix='.zip', dir=self._directory
            )
        except Exception as exc:
            self._set_error(exc)
            raise
        os.close(fd)
        manifest = {
            'schema_version': SCHEMA_VERSION,
            'exported_at': exported_at.isoformat(),
            'application': {
                'name': __prog__,
                'version': __version__,
                'python': platform.python_version(),
                'operating_system': platform.platform(),
            },
            'filters': {
                'room_id': room_id,
                'since': since.isoformat() if since else None,
                'until': until.isoformat() if until else None,
            },
            'record_count': len(records),
            'dropped_records': self.status().dropped_records,
            'truncated_records': sum(
                1
                for _, record in records
                if record.get('response', {}).get('body_truncated')
            ),
        }
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2)
                )
                content = ''.join(
                    json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
                    for _, record in records
                )
                archive.writestr('records.jsonl', content)
        except Exception as exc:
            Path(path).unlink(missing_ok=True)
            self._set_error(exc)
            raise
        return HttpHistoryExport(
            path=path, filename=filename, record_count=len(records)
        )

    def _command(self, name: str, value: Any = None) -> Any:
        if not self._started:
            if name == 'stop':
                return None
            raise RuntimeError('HTTP history store is not started')
        command = _Command(name=name, value=value)
        self._queue.put(command)
        command.event.wait()
        if command.error is not None:
            raise command.error
        return command.result

    def _run(self, ready: threading.Event) -> None:
        try:
            self._load_incidents()
            self._rebuild_payload_ref_counts()
            self._cleanup()
            self._refresh_status()
        except Exception as exc:
            self._set_error(exc)
        finally:
            ready.set()

        while True:
            from_fallback = False
            try:
                item = self._metadata_fallback.get_nowait()
                from_fallback = True
            except queue.Empty:
                item = self._queue.get()
            try:
                if isinstance(item, _Command):
                    if self._handle_command(item):
                        return
                elif isinstance(item, _IncidentMarker):
                    self._mark_incident(item)
                else:
                    self._write_item(item)
            except Exception as exc:
                if isinstance(item, _Command):
                    item.error = exc
                else:
                    self._set_error(exc, dropped=True)
                    try:
                        self._close_file()
                    except Exception as close_exc:
                        self._set_error(close_exc)
            finally:
                if isinstance(item, _RecordItem) and item.payload is not None:
                    with self._state_lock:
                        self._queued_payload_bytes -= len(item.payload)
                if isinstance(item, _Command):
                    item.event.set()
                if not from_fallback:
                    self._queue.task_done()

    def _handle_command(self, command: _Command) -> bool:
        if command.name == 'stop':
            try:
                self._close_file()
            except Exception as exc:
                command.error = exc
                self._set_error(exc)
            return True
        if command.name == 'flush':
            if self._file is not None:
                self._file.flush()
            self._refresh_status()
            return False
        if command.name == 'snapshot':
            self._close_file()
            snapshot_directory = Path(
                tempfile.mkdtemp(prefix='.http-history-snapshot-', dir=self._directory)
            )
            snapshot_paths = []
            try:
                for path in self._segment_paths():
                    snapshot_path = snapshot_directory / path.name
                    try:
                        os.link(path, snapshot_path)
                    except OSError:
                        shutil.copy2(path, snapshot_path)
                    snapshot_paths.append(snapshot_path)
            except Exception:
                shutil.rmtree(snapshot_directory, ignore_errors=True)
                raise
            command.result = _Snapshot(snapshot_directory, snapshot_paths)
            return False
        if command.name == 'list_incidents':
            self._finalize_expired_incidents()
            filters = command.value
            room_id = filters['room_id']
            since = filters['since']
            until = filters['until']
            result = []
            for incident in self._incidents.values():
                timestamp = _parse_datetime(incident['last_at'])
                if room_id is not None and incident['room_id'] != room_id:
                    continue
                if since is not None and timestamp < since.astimezone(timezone.utc):
                    continue
                if until is not None and timestamp > until.astimezone(timezone.utc):
                    continue
                result.append(self._incident_summary(incident))
            command.result = sorted(result, key=lambda item: item.last_at, reverse=True)
            return False
        if command.name == 'incident_snapshot':
            self._finalize_expired_incidents()
            incident = self._incidents.get(command.value)
            if incident is None:
                raise KeyError(f'Unknown HTTP incident: {command.value}')
            payload_paths = {
                digest: self._payload_path(digest)
                for digest in incident['payloads']
                if re.fullmatch(r'[0-9a-f]{64}', digest) is not None
                and self._payload_path(digest).is_file()
            }
            command.result = _IncidentSnapshot(deepcopy(incident), payload_paths)
            return False
        if command.name == 'clear':
            self._close_file()
            for path in self._segment_paths():
                path.unlink(missing_ok=True)
            for path in self._incident_paths():
                path.unlink(missing_ok=True)
            shutil.rmtree(self._payload_directory(), ignore_errors=True)
            self._incidents.clear()
            self._incident_record_ids.clear()
            self._payload_ref_counts.clear()
            with self._state_lock:
                self._incident_lookup.clear()
            self._refresh_status()
            return False
        if command.name == 'configure':
            self._enabled = command.value['enabled']
            self._retention_days = command.value['retention_days']
            self._max_size = command.value['max_size']
            self._cleanup()
            self._refresh_status()
            return False
        raise ValueError(f'Unknown HTTP history command: {command.name}')

    def _write_item(self, item: _RecordItem) -> None:
        record = item.record
        if item.payload is not None:
            digest = hashlib.sha256(item.payload).hexdigest()
            payload_path = self._payload_path(digest)
            response = record.setdefault('response', {})
            try:
                if not payload_path.is_file():
                    self._write_payload(payload_path, item.payload)
            except Exception as exc:
                response['payload'] = {
                    'sha256': digest,
                    'size': len(item.payload),
                    'role': item.payload_role,
                    'content_type': item.payload_content_type,
                    'omitted': True,
                    'omitted_reason': (
                        f'payload write failed: {type(exc).__name__}: {exc}'
                    ),
                }
                self._set_error(exc, dropped=True)
            else:
                response['payload'] = {
                    'sha256': digest,
                    'size': len(item.payload),
                    'role': item.payload_role,
                    'content_type': item.payload_content_type,
                    'path': f'payloads/{digest}.bin',
                    'omitted': False,
                }
                self._payload_ref_counts[digest] = (
                    self._payload_ref_counts.get(digest, 0) + 1
                )

        record.setdefault('sequence', self._sequence)
        self._write(record)
        self._capture_record_for_active_incidents(record)
        self._cleanup()

    def _write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
        size = len(line.encode('utf8'))
        if self._file is None or (
            self._path is not None
            and self._path.stat().st_size + size > self._segment_size
        ):
            self._close_file()
            self._open_file()
        assert self._file is not None
        self._file.write(line)
        self._file.flush()

    def _write_payload(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='wb',
                dir=str(path.parent),
                prefix=f'.{path.name}.',
                suffix='.tmp',
                delete=False,
            ) as target:
                temp_path = target.name
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(temp_path, path)
            except FileExistsError:
                pass
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)

    def _mark_incident(self, marker: _IncidentMarker) -> None:
        occurred_at = _parse_datetime(marker.occurred_at)
        incident = self._incidents.get(marker.incident_id)
        if incident is None:
            incident = {
                'schema_version': SCHEMA_VERSION,
                'incident_id': marker.incident_id,
                'room_id': marker.room_id,
                'kind': marker.kind,
                'first_at': marker.occurred_at,
                'last_at': marker.occurred_at,
                'capture_until': (
                    occurred_at + timedelta(seconds=_INCIDENT_POST_SECONDS)
                ).isoformat(),
                'occurrence_count': 0,
                'status': 'capturing',
                'partial': False,
                'operation_ids': [],
                'details': [],
                'records': [],
                'payloads': {},
                'selected_detail_indexes': [],
                'selected_record_ids': [],
                'omitted_details': [],
                'omitted_records': [],
                'omitted_payloads': [],
                'payload_size': 0,
                'resource_size': 0,
                'forced_partial': False,
            }
            self._incidents[marker.incident_id] = incident
            self._incident_record_ids[marker.incident_id] = set()

        incident['last_at'] = marker.occurred_at
        incident['capture_until'] = (
            occurred_at + timedelta(seconds=_INCIDENT_POST_SECONDS)
        ).isoformat()
        incident['occurrence_count'] += 1
        incident['status'] = 'capturing'
        if marker.operation_id and marker.operation_id not in incident['operation_ids']:
            incident['operation_ids'].append(marker.operation_id)
        if marker.details:
            incident['details'].append(marker.details)

        start = occurred_at - timedelta(seconds=_INCIDENT_PRE_SECONDS)
        for record in self._records_between(
            room_id=marker.room_id, since=start, until=occurred_at
        ):
            self._add_record_to_incident(incident, record)
        self._select_incident_payloads(incident)
        self._persist_incident(incident)
        self._cleanup()
        self._refresh_status()

    def _capture_record_for_active_incidents(self, record: Dict[str, Any]) -> None:
        timestamp = _parse_datetime(record['recorded_at'])
        changed = []
        for incident in self._incidents.values():
            if incident['status'] != 'capturing':
                continue
            capture_until = _parse_datetime(incident['capture_until'])
            if timestamp > capture_until:
                incident['status'] = 'ready'
                changed.append(incident)
                continue
            if record.get('room_id') == incident['room_id']:
                self._add_record_to_incident(incident, record)
                changed.append(incident)
        for incident in changed:
            self._persist_incident(incident)

    def _add_record_to_incident(
        self, incident: Dict[str, Any], record: Dict[str, Any]
    ) -> None:
        record_id = str(record.get('record_id', ''))
        seen = self._incident_record_ids.setdefault(incident['incident_id'], set())
        if not record_id or record_id in seen:
            return

        incident['records'].append(deepcopy(record))
        seen.add(record_id)
        self._resolve_deduplicated_body(incident['records'][-1])
        self._select_incident_payloads(incident)

    def _resolve_deduplicated_body(self, record: Dict[str, Any]) -> None:
        if not record.get('body_unchanged'):
            return
        response = record.get('response')
        digest = record.get('body_sha256')
        sequence = record.get('sequence')
        if (
            not isinstance(response, dict)
            or 'body' in response
            or not isinstance(digest, str)
            or not isinstance(sequence, int)
        ):
            return

        latest: Optional[Dict[str, Any]] = None
        latest_sequence = -1
        if self._file is not None:
            self._file.flush()
        for path in self._segment_paths():
            try:
                with open(path, 'rt', encoding='utf8') as source:
                    for line in source:
                        try:
                            candidate = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        candidate_sequence = candidate.get('sequence')
                        candidate_response = candidate.get('response', {})
                        candidate_digest = candidate_response.get(
                            'body_sha256', candidate.get('body_sha256')
                        )
                        if (
                            not isinstance(candidate_sequence, int)
                            or candidate_sequence >= sequence
                            or candidate_sequence <= latest_sequence
                            or candidate.get('room_id') != record.get('room_id')
                            or candidate.get('category') != record.get('category')
                            or candidate_digest != digest
                            or 'body' not in candidate_response
                        ):
                            continue
                        latest = candidate
                        latest_sequence = candidate_sequence
            except FileNotFoundError:
                continue
        if latest is None:
            return
        safe_latest = sanitize_record(latest)
        response['body'] = deepcopy(safe_latest['response']['body'])
        response['body_sha256'] = digest
        record['body_resolved_from_record_id'] = safe_latest.get('record_id')

    def _select_incident_payloads(self, incident: Dict[str, Any]) -> None:
        trigger_at = _parse_datetime(incident['last_at'])
        operation_ids = set(incident.get('operation_ids', []))
        candidates: Dict[str, tuple[tuple[int, float, int], Dict[str, Any], str]] = {}
        omitted = []

        def record_sort_key(
            record: Dict[str, Any], index: int
        ) -> tuple[int, float, int]:
            payload = record.get('response', {}).get('payload')
            role = payload.get('role') if isinstance(payload, dict) else None
            status_code = record.get('response', {}).get('status')
            if record.get('operation_id') in operation_ids:
                priority = 0
            elif role == 'hls_init':
                priority = 1
            elif record.get('category') == 'hls_playlist':
                priority = 2
            elif record.get('outcome') == 'error' or (
                isinstance(status_code, int) and status_code >= 400
            ):
                priority = 3
            elif role == 'hls_media':
                priority = 4
            else:
                priority = 5
            try:
                distance = abs(
                    (
                        _parse_datetime(record['recorded_at']) - trigger_at
                    ).total_seconds()
                )
            except (KeyError, TypeError, ValueError):
                distance = float('inf')
            return priority, distance, index

        selected_detail_indexes = []
        omitted_details = []
        details = incident.get('details', [])
        resource_size = len(b'[]')
        for index in reversed(range(len(details))):
            candidate_indexes = sorted([index, *selected_detail_indexes])
            candidate_details = [details[item] for item in candidate_indexes]
            candidate_size = len(
                json.dumps(
                    candidate_details, ensure_ascii=False, separators=(',', ':')
                ).encode('utf8')
            )
            if candidate_size <= _INCIDENT_RESOURCE_LIMIT:
                selected_detail_indexes = candidate_indexes
                resource_size = candidate_size
            else:
                omitted_details.append(
                    {'index': index, 'reason': 'incident resource limit exceeded'}
                )

        ranked_records = []
        for index, record in enumerate(incident['records']):
            line = (
                json.dumps(
                    sanitize_record(record), ensure_ascii=False, separators=(',', ':')
                )
                + '\n'
            )
            ranked_records.append(
                (record_sort_key(record, index), record, len(line.encode('utf8')))
            )

        selected_record_ids = set()
        omitted_records = []
        for _, record, size in sorted(ranked_records, key=lambda item: item[0]):
            record_id = str(record.get('record_id', ''))
            if record_id and resource_size + size <= _INCIDENT_RESOURCE_LIMIT:
                selected_record_ids.add(record_id)
                resource_size += size
            else:
                omitted_records.append(
                    {
                        key: value
                        for key, value in {
                            'record_id': record_id or None,
                            'exchange_id': record.get('exchange_id'),
                            'sequence': record.get('sequence'),
                            'category': record.get('category'),
                            'reason': 'incident resource limit exceeded',
                        }.items()
                        if value is not None
                    }
                )

        for index, record in enumerate(incident['records']):
            payload = record.get('response', {}).get('payload')
            if not isinstance(payload, dict):
                continue
            record_id = str(record.get('record_id', ''))
            if record_id not in selected_record_ids:
                omitted.append(
                    {
                        key: value
                        for key, value in {
                            'sha256': payload.get('sha256'),
                            'size': payload.get('size'),
                            'role': payload.get('role'),
                            'record_id': record_id,
                            'reason': (
                                'incident resource limit omitted its exchange record'
                            ),
                        }.items()
                        if value is not None
                    }
                )
                continue
            if payload.get('omitted'):
                omitted.append(
                    {
                        key: value
                        for key, value in {
                            'sha256': payload.get('sha256'),
                            'size': payload.get('size'),
                            'role': payload.get('role'),
                            'record_id': record_id,
                            'reason': payload.get(
                                'omitted_reason', 'payload was not captured'
                            ),
                        }.items()
                        if value is not None
                    }
                )
                continue

            digest = payload.get('sha256')
            size = payload.get('size')
            if not isinstance(digest, str) or not isinstance(size, int):
                continue
            if (
                re.fullmatch(r'[0-9a-f]{64}', digest) is None
                or not self._payload_path(digest).is_file()
            ):
                omitted.append(
                    {
                        'sha256': digest,
                        'size': size,
                        'role': payload.get('role'),
                        'record_id': record_id,
                        'reason': 'content-addressed payload is missing',
                    }
                )
                continue
            sort_key = record_sort_key(record, index)
            previous = candidates.get(digest)
            if previous is None or sort_key < previous[0]:
                candidates[digest] = (sort_key, deepcopy(payload), record_id)

        selected: Dict[str, Dict[str, Any]] = {}
        payload_size = 0
        payload_budget = min(
            _INCIDENT_PAYLOAD_LIMIT, max(_INCIDENT_RESOURCE_LIMIT - resource_size, 0)
        )
        for digest, (_, payload, record_id) in sorted(
            candidates.items(), key=lambda item: item[1][0]
        ):
            size = payload['size']
            if payload_size + size <= payload_budget:
                selected[digest] = payload
                payload_size += size
            else:
                omitted.append(
                    {
                        'sha256': digest,
                        'size': size,
                        'role': payload.get('role'),
                        'record_id': record_id,
                        'reason': 'incident payload limit exceeded',
                    }
                )

        omitted = [
            payload for payload in omitted if payload.get('sha256') not in selected
        ]
        incident['payloads'] = selected
        incident['payload_size'] = payload_size
        incident['selected_detail_indexes'] = selected_detail_indexes
        incident['selected_record_ids'] = [
            str(record.get('record_id'))
            for record in incident['records']
            if str(record.get('record_id')) in selected_record_ids
        ]
        incident['omitted_details'] = list(reversed(omitted_details))
        incident['omitted_records'] = omitted_records
        incident['omitted_payloads'] = omitted
        incident['resource_size'] = resource_size + payload_size
        incident['partial'] = bool(incident.get('forced_partial')) or bool(
            omitted_details or omitted_records or omitted
        )

    def _records_between(
        self, *, room_id: int, since: datetime, until: datetime
    ) -> Iterable[Dict[str, Any]]:
        if self._file is not None:
            self._file.flush()
        for path in self._segment_paths():
            try:
                with open(path, 'rt', encoding='utf8') as source:
                    for line in source:
                        try:
                            record = json.loads(line)
                            timestamp = _parse_datetime(record['recorded_at'])
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                            continue
                        if record.get('room_id') != room_id:
                            continue
                        if since <= timestamp <= until:
                            yield record
            except FileNotFoundError:
                continue

    def _finalize_expired_incidents(self) -> None:
        now = _utc_now()
        changed = False
        for incident in self._incidents.values():
            if incident['status'] == 'capturing' and now > _parse_datetime(
                incident['capture_until']
            ):
                incident['status'] = 'ready'
                self._persist_incident(incident)
                changed = True
        if changed:
            self._cleanup()
            self._refresh_status()

    def _incident_summary(self, incident: Dict[str, Any]) -> HttpIncidentSummary:
        return HttpIncidentSummary(
            incident_id=incident['incident_id'],
            room_id=incident['room_id'],
            kind=incident['kind'],
            first_at=incident['first_at'],
            last_at=incident['last_at'],
            occurrence_count=incident['occurrence_count'],
            status=incident['status'],
            record_count=len(incident.get('selected_record_ids', incident['records'])),
            payload_size=incident['payload_size'],
            partial=incident['partial'],
        )

    def _persist_incident(self, incident: Dict[str, Any]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._incident_path(incident['incident_id'])
        temp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='wt',
                encoding='utf8',
                dir=str(self._directory),
                prefix=f'.{path.name}.',
                suffix='.tmp',
                delete=False,
            ) as target:
                temp_path = target.name
                json.dump(incident, target, ensure_ascii=False, separators=(',', ':'))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)

    def _load_incidents(self) -> None:
        self._incidents.clear()
        self._incident_record_ids.clear()
        with self._state_lock:
            self._incident_lookup.clear()
        for path in self._incident_paths():
            try:
                incident = json.loads(path.read_text(encoding='utf8'))
                incident_id = str(incident['incident_id'])
                room_id = int(incident['room_id'])
                kind = str(incident['kind'])
                last_at = _parse_datetime(incident['last_at'])
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError):
                continue
            changed = False
            if incident.get('status') == 'capturing':
                incident['status'] = 'ready'
                incident['forced_partial'] = True
                incident['partial'] = True
                changed = True
            payloads = incident.setdefault('payloads', {})
            for digest, payload in list(payloads.items()):
                if (
                    re.fullmatch(r'[0-9a-f]{64}', digest) is not None
                    and self._payload_path(digest).is_file()
                ):
                    continue
                payloads.pop(digest, None)
                size = payload.get('size') if isinstance(payload, dict) else None
                incident.setdefault('omitted_payloads', []).append(
                    {
                        key: value
                        for key, value in {
                            'sha256': digest,
                            'size': size,
                            'role': (
                                payload.get('role')
                                if isinstance(payload, dict)
                                else None
                            ),
                            'reason': 'content-addressed payload is missing',
                        }.items()
                        if value is not None
                    }
                )
                incident['forced_partial'] = True
                incident['partial'] = True
                changed = True
            incident['payload_size'] = sum(
                payload.get('size', 0)
                for payload in payloads.values()
                if isinstance(payload, dict) and isinstance(payload.get('size'), int)
            )
            if 'selected_record_ids' not in incident:
                changed = True
            self._select_incident_payloads(incident)
            if changed:
                self._persist_incident(incident)
            self._incidents[incident_id] = incident
            self._incident_record_ids[incident_id] = {
                str(record.get('record_id')) for record in incident.get('records', [])
            }
            with self._state_lock:
                key = (room_id, kind)
                current = self._incident_lookup.get(key)
                if current is None or last_at > current[1]:
                    self._incident_lookup[key] = (incident_id, last_at)

    def _payload_directory(self) -> Path:
        return self._directory / 'payloads'

    def _payload_path(self, digest: str) -> Path:
        return self._payload_directory() / f'{digest}.bin'

    def _incident_path(self, incident_id: str) -> Path:
        return self._directory / f'http-incident-{incident_id}.json'

    def _incident_paths(self) -> List[Path]:
        return sorted(self._directory.glob('http-incident-*.json'))

    def _open_file(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        timestamp = _utc_now().strftime('%Y%m%dT%H%M%S%fZ')
        self._path = (
            self._directory / f'http-history-{timestamp}-{uuid.uuid4().hex}.jsonl'
        )
        self._file = open(self._path, 'at', encoding='utf8')

    def _close_file(self) -> None:
        file = self._file
        self._file = None
        self._path = None
        if file is None:
            return
        error = None
        try:
            file.flush()
        except Exception as exc:
            error = exc
        try:
            file.close()
        except Exception as exc:
            if error is None:
                error = exc
        if error is not None:
            raise error

    def _segment_paths(self) -> List[Path]:
        return sorted(self._directory.glob('http-history-*.jsonl'))

    def _rebuild_payload_ref_counts(self) -> None:
        self._payload_ref_counts.clear()
        self._sequence = 0
        for path in self._segment_paths():
            try:
                with open(path, 'rt', encoding='utf8') as source:
                    for line in source:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        sequence = record.get('sequence')
                        if isinstance(sequence, int):
                            self._sequence = max(self._sequence, sequence)
                        digest = (
                            record.get('response', {}).get('payload', {}).get('sha256')
                        )
                        if isinstance(digest, str):
                            self._payload_ref_counts[digest] = (
                                self._payload_ref_counts.get(digest, 0) + 1
                            )
            except FileNotFoundError:
                continue

    def _delete_segment(self, path: Path) -> None:
        try:
            with open(path, 'rt', encoding='utf8') as source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    digest = record.get('response', {}).get('payload', {}).get('sha256')
                    if isinstance(digest, str):
                        remaining = self._payload_ref_counts.get(digest, 0) - 1
                        if remaining > 0:
                            self._payload_ref_counts[digest] = remaining
                        else:
                            self._payload_ref_counts.pop(digest, None)
        except FileNotFoundError:
            return
        path.unlink(missing_ok=True)

    def _incident_payload_hashes(self) -> set[str]:
        return {
            digest
            for incident in self._incidents.values()
            for digest in incident.get('payloads', {})
        }

    def _remove_orphan_payloads(self) -> None:
        pinned = self._incident_payload_hashes()
        for path in self._payload_directory().glob('*.bin'):
            if path.stem not in self._payload_ref_counts and path.stem not in pinned:
                path.unlink(missing_ok=True)

    def _stored_size(self) -> int:
        paths = [*self._segment_paths(), *self._incident_paths()]
        paths.extend(self._payload_directory().glob('*.bin'))
        return sum(path.stat().st_size for path in paths if path.is_file())

    def _cleanup(self) -> None:
        paths = self._segment_paths()
        cutoff = _utc_now() - timedelta(days=self._retention_days)
        for path in paths:
            if path == self._path:
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                self._delete_segment(path)

        for path in self._incident_paths():
            incident = self._incidents.get(path.stem.removeprefix('http-incident-'))
            if incident is None or incident.get('status') == 'capturing':
                continue
            if _parse_datetime(incident['last_at']) < cutoff:
                path.unlink(missing_ok=True)
                self._incidents.pop(incident['incident_id'], None)
                self._incident_record_ids.pop(incident['incident_id'], None)

        self._remove_orphan_payloads()

        paths = self._segment_paths()
        total_size = self._stored_size()
        if total_size > self._max_size and self._path is not None:
            self._close_file()
            paths = self._segment_paths()
        for path in paths:
            if total_size <= self._max_size:
                break
            if path == self._path:
                continue
            size = path.stat().st_size
            self._delete_segment(path)
            total_size -= size

        self._remove_orphan_payloads()
        total_size = self._stored_size()
        ready_incidents = sorted(
            (
                incident
                for incident in self._incidents.values()
                if incident.get('status') != 'capturing'
            ),
            key=lambda incident: incident['last_at'],
        )
        for incident in ready_incidents:
            if total_size <= self._max_size:
                break
            path = self._incident_path(incident['incident_id'])
            path.unlink(missing_ok=True)
            self._incidents.pop(incident['incident_id'], None)
            self._incident_record_ids.pop(incident['incident_id'], None)
            self._remove_orphan_payloads()
            total_size = self._stored_size()

    def _refresh_status(self) -> None:
        record_count = 0
        total_size = 0
        oldest_at = None
        newest_at = None
        room_ids = set()
        payload_size = sum(
            path.stat().st_size for path in self._payload_directory().glob('*.bin')
        )
        for path in self._segment_paths():
            total_size += path.stat().st_size
            try:
                with open(path, 'rt', encoding='utf8') as source:
                    for line in source:
                        try:
                            record = json.loads(line)
                            timestamp = str(record['recorded_at'])
                        except (json.JSONDecodeError, KeyError, TypeError):
                            continue
                        record_count += 1
                        oldest_at = (
                            min(oldest_at, timestamp) if oldest_at else timestamp
                        )
                        newest_at = (
                            max(newest_at, timestamp) if newest_at else timestamp
                        )
                        if isinstance(record.get('room_id'), int):
                            room_ids.add(record['room_id'])
            except FileNotFoundError:
                continue
        room_ids.update(
            incident['room_id']
            for incident in self._incidents.values()
            if isinstance(incident.get('room_id'), int)
        )
        with self._state_lock:
            self._record_count = record_count
            self._total_size = self._stored_size()
            self._oldest_at = oldest_at
            self._newest_at = newest_at
            self._room_ids = room_ids
            self._incident_count = len(self._incidents)
            self._active_incident_count = sum(
                incident.get('status') == 'capturing'
                for incident in self._incidents.values()
            )
            self._payload_size = payload_size

    def _set_error(self, exc: BaseException, *, dropped: bool = False) -> None:
        message = f'{type(exc).__name__}: {exc}'
        now = time.monotonic()
        with self._state_lock:
            self._last_error = message
            if dropped:
                self._dropped_records += 1
            should_warn = now - self._last_warning_at >= _WARNING_INTERVAL
            if should_warn:
                self._last_warning_at = now
        if should_warn:
            logger.warning('Failed to persist HTTP history: {}', message)
