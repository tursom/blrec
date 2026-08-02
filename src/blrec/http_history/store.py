"""Bounded JSONL persistence and issue-safe ZIP export for HTTP history."""

from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, TextIO, Union

from loguru import logger

from blrec import __prog__, __version__

from .sanitizer import sanitize_record

__all__ = (
    'HttpHistoryExport',
    'HttpHistoryStatus',
    'HttpHistoryStore',
    'NoHistoryRecords',
)

SCHEMA_VERSION = 1
DEFAULT_SEGMENT_SIZE = 5 * 1024 * 1024
_QUEUE_SIZE = 10000
_WARNING_INTERVAL = 60.0


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


@dataclass(frozen=True)
class HttpHistoryExport:
    path: str
    filename: str
    record_count: int


@dataclass(frozen=True)
class _Snapshot:
    directory: Path
    paths: List[Path]


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


QueueItem = Union[Dict[str, Any], _Command]


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
        max_size: int = 100 * 1024 * 1024,
        segment_size: int = DEFAULT_SEGMENT_SIZE,
    ) -> None:
        self._directory = Path(directory)
        self._enabled = enabled
        self._retention_days = retention_days
        self._max_size = max_size
        self._segment_size = segment_size
        self._queue: queue.Queue[QueueItem] = queue.Queue(maxsize=_QUEUE_SIZE)
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
        self._operation_attempts: OrderedDict[str, int] = OrderedDict()
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

    def record(self, record: Mapping[str, Any]) -> None:
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
        operation_id = item.get('operation_id')
        attempt = 1
        if isinstance(operation_id, str) and operation_id:
            with self._state_lock:
                attempt = self._operation_attempts.pop(operation_id, 0) + 1
                self._operation_attempts[operation_id] = attempt
                if len(self._operation_attempts) > 10000:
                    self._operation_attempts.popitem(last=False)
        item.setdefault('retry', {'attempt': attempt, 'is_retry': attempt > 1})
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.report_error(RuntimeError('HTTP history queue is full'), dropped=True)

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
            self._cleanup()
            self._refresh_status()
        except Exception as exc:
            self._set_error(exc)
        finally:
            ready.set()

        while True:
            item = self._queue.get()
            try:
                if isinstance(item, _Command):
                    if self._handle_command(item):
                        return
                else:
                    self._write(item)
            except Exception as exc:
                self._set_error(exc, dropped=not isinstance(item, _Command))
                if isinstance(item, _Command):
                    item.error = exc
                else:
                    try:
                        self._close_file()
                    except Exception as close_exc:
                        self._set_error(close_exc)
            finally:
                if isinstance(item, _Command):
                    item.event.set()
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
        if command.name == 'clear':
            self._close_file()
            for path in self._segment_paths():
                path.unlink(missing_ok=True)
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
        self._cleanup()

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

    def _cleanup(self) -> None:
        paths = self._segment_paths()
        cutoff = _utc_now() - timedelta(days=self._retention_days)
        for path in paths:
            if path == self._path:
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                path.unlink(missing_ok=True)

        paths = self._segment_paths()
        total_size = sum(path.stat().st_size for path in paths)
        for path in paths:
            if total_size <= self._max_size:
                break
            if path == self._path:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total_size -= size

    def _refresh_status(self) -> None:
        record_count = 0
        total_size = 0
        oldest_at = None
        newest_at = None
        room_ids = set()
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
        with self._state_lock:
            self._record_count = record_count
            self._total_size = total_size
            self._oldest_at = oldest_at
            self._newest_at = newest_at
            self._room_ids = room_ids

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
