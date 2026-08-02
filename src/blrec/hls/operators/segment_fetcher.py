"""下载并校验 fMP4 初始化段和媒体分片。"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Union
from urllib.parse import urlsplit

import attr
import m3u8
import requests
import urllib3
from loguru import logger
from reactivex import Observable, abc
from reactivex import operators as ops
from reactivex.disposable import CompositeDisposable, Disposable, SerialDisposable
from tenacity import (
    retry,
    retry_all,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_delay,
    wait_exponential,
)

from blrec.bili.live import Live
from blrec.core import operators as core_ops
from blrec.exception.helpers import format_exception
from blrec.http_history import record_http_exchange
from blrec.utils import operators as utils_ops
from blrec.utils.hash import cksum

from ..exceptions import FetchSegmentError, SegmentDataCorrupted

__all__ = ('SegmentFetcher', 'InitSectionData', 'SegmentData')


@attr.s(auto_attribs=True, slots=True, frozen=True)
class InitSectionData:
    segment: m3u8.Segment
    payload: bytes
    offset: int = 0

    def __len__(self) -> int:
        return len(self.payload)


@attr.s(auto_attribs=True, slots=True, frozen=True)
class SegmentData:
    segment: m3u8.Segment
    payload: bytes
    offset: int = 0

    def __len__(self) -> int:
        return len(self.payload)


class SegmentFetcher:
    _MAX_INIT_SECTION_DOWNLOADS = 3

    def __init__(
        self,
        live: Live,
        session: requests.Session,
        stream_url_resolver: core_ops.StreamURLResolver,
    ) -> None:
        self._live = live
        self._session = session
        self._stream_url_resolver = stream_url_resolver
        self._segment_summaries = {}
        self._last_segment_operation_id: Optional[str] = None

    def __call__(
        self, source: Observable[m3u8.Segment]
    ) -> Observable[Union[InitSectionData, SegmentData]]:
        return self._fetch(source).pipe(  # type: ignore
            ops.do_action(on_error=self._before_retry),
            utils_ops.retry(should_retry=self._should_retry),
        )

    def _fetch(
        self, source: Observable[m3u8.Segment]
    ) -> Observable[Union[InitSectionData, SegmentData]]:
        def subscribe(
            observer: abc.ObserverBase[Union[InitSectionData, SegmentData]],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            disposed = False
            subscription = SerialDisposable()

            attempts: int = 0
            last_segment: Optional[m3u8.Segment] = None

            def on_next(seg: m3u8.Segment) -> None:
                nonlocal attempts, last_segment
                url: str = ''

                try:
                    if hasattr(seg, 'init_section') and (
                        (
                            last_segment is None
                            or seg.init_section != last_segment.init_section
                        )
                    ):
                        url = seg.init_section.absolute_uri
                        data = self._fetch_segment(url)
                        data_operation_id = self._last_segment_operation_id
                        # 初始化段没有服务端校验值，最多下载三次并要求相邻两次相同。
                        for _ in range(self._MAX_INIT_SECTION_DOWNLOADS - 1):
                            time.sleep(1)
                            if (_data := self._fetch_segment(url)) == data:
                                self._record_segment_success(
                                    url, len(data), data_operation_id
                                )
                                self._record_segment_success(
                                    url, len(_data), self._last_segment_operation_id
                                )
                                logger.debug(
                                    'Init section checked: '
                                    f'crc32 of previous data: {cksum(data)}, '
                                    f'crc32 of current data: {cksum(_data)}, '
                                    f'init section url: {url}'
                                )
                                break
                            else:
                                logger.debug(
                                    'Init section corrupted: '
                                    f'crc32 of previous data: {cksum(data)}, '
                                    f'crc32 of current data: {cksum(_data)}, '
                                    f'init section url: {url}'
                                )
                                data = _data
                                data_operation_id = self._last_segment_operation_id
                        else:
                            raise SegmentDataCorrupted(url)
                        observer.on_next(InitSectionData(segment=seg, payload=data))
                    last_segment = seg

                    url = seg.absolute_uri
                    hex_size, crc32, *_ = seg.title.split('|')
                    # B 站把预期十六进制大小与 CRC32 放在 EXTINF title 中。
                    size = int(hex_size, 16)
                    for _ in range(3):
                        data = self._fetch_segment(url)
                        operation_id = self._last_segment_operation_id
                        if len(data) != size:
                            self._record_segment_validation_error(
                                url, 'length', size, len(data), operation_id
                            )
                            logger.debug(
                                'Segment data incomplete: '
                                f'size expected: {size}, '
                                f'size fetched: {len(data)}, '
                                f'segment url: {url}'
                            )
                            continue
                        crc32_of_data = cksum(data)
                        if crc32_of_data != crc32:
                            self._record_segment_validation_error(
                                url, 'crc32', crc32, crc32_of_data, operation_id
                            )
                            logger.debug(
                                'Segment data corrupted: '
                                f'correct crc32: {crc32}, '
                                f'crc32 of segment data: {crc32_of_data}, '
                                f'segment url: {url}'
                            )
                            continue
                        self._record_segment_success(url, len(data), operation_id)
                        break
                    else:
                        raise SegmentDataCorrupted(url)
                except Exception as exc:
                    logger.warning(
                        'Failed to fetch segment: {}\n{}', url, format_exception(exc)
                    )
                    attempts += 1
                    if attempts > 3:
                        attempts = 0
                        observer.on_error(FetchSegmentError(exc))
                else:
                    observer.on_next(SegmentData(segment=seg, payload=data))
                    attempts = 0

            def dispose() -> None:
                nonlocal disposed
                nonlocal last_segment
                disposed = True
                last_segment = None
                self._flush_segment_summaries()

            subscription.disposable = source.subscribe(
                on_next, observer.on_error, observer.on_completed, scheduler=scheduler
            )

            return CompositeDisposable(subscription, Disposable(dispose))

        return Observable(subscribe)

    def _fetch_segment(self, url: str) -> bytes:
        operation_id = uuid.uuid4().hex
        data = self._fetch_segment_with_retry(url, operation_id)
        self._last_segment_operation_id = operation_id
        return data

    @retry(
        reraise=True,
        retry=retry_all(
            retry_if_exception_type(
                (requests.exceptions.RequestException, urllib3.exceptions.HTTPError)
            ),
            retry_if_not_exception_type(requests.exceptions.HTTPError),
        ),
        wait=wait_exponential(max=10),
        stop=stop_after_delay(60),
    )
    def _fetch_segment_with_retry(self, url: str, operation_id: str) -> bytes:
        started_at = time.perf_counter()
        try:
            response = self._session.get(url, headers=self._live.headers, timeout=5)
            response.raise_for_status()
        except Exception as e:
            failed_response = getattr(e, 'response', None)
            record_http_exchange(
                self._live.http_history,
                room_id=self._live.room_id,
                category='hls_segment',
                method='GET',
                url=url,
                request_headers=self._live.headers,
                response_status=getattr(failed_response, 'status_code', None),
                response_headers=getattr(failed_response, 'headers', None),
                error=e,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                operation_id=operation_id,
            )
            logger.debug(f'Failed to fetch segment {url}: {repr(e)}')
            raise
        else:
            return response.content

    def _record_segment_success(
        self, url: str, size: int, operation_id: Optional[str] = None
    ) -> None:
        host = urlsplit(url).hostname or ''
        now = time.monotonic()
        summary = self._segment_summaries.setdefault(
            host,
            {
                'started_at': datetime.now(timezone.utc).isoformat(),
                'started_monotonic': now,
                'count': 0,
                'bytes': 0,
                'url': url,
                'operation_ids': [],
            },
        )
        summary['count'] += 1
        summary['bytes'] += size
        if operation_id is not None:
            summary['operation_ids'].append(operation_id)
        if now - summary['started_monotonic'] >= 60:
            self._flush_segment_summaries(host)

    def _flush_segment_summaries(self, host: Optional[str] = None) -> None:
        hosts = [host] if host is not None else list(self._segment_summaries)
        for item_host in hosts:
            summary = self._segment_summaries.pop(item_host, None)
            if not summary or not summary['count']:
                continue
            if self._live.http_history is not None:
                self._live.http_history.record(
                    {
                        'record_type': 'hls_segment_summary',
                        'room_id': self._live.room_id,
                        'category': 'hls_segment',
                        'request': {'method': 'GET', 'url': summary['url']},
                        'started_at': summary['started_at'],
                        'ended_at': datetime.now(timezone.utc).isoformat(),
                        'request_count': summary['count'],
                        'response_bytes': summary['bytes'],
                        'operation_ids': summary['operation_ids'],
                    }
                )

    def _record_segment_validation_error(
        self,
        url: str,
        error_type: str,
        expected: Union[str, int],
        actual: Union[str, int],
        operation_id: Optional[str] = None,
    ) -> None:
        if self._live.http_history is None:
            return
        self._live.http_history.record(
            {
                'record_type': 'hls_segment_validation_error',
                'room_id': self._live.room_id,
                'category': 'hls_segment',
                'operation_id': operation_id,
                'request': {'method': 'GET', 'url': url},
                'validation': {
                    'type': error_type,
                    'expected': expected,
                    'actual': actual,
                },
            }
        )

    def _should_retry(self, exc: Exception) -> bool:
        if isinstance(exc, FetchSegmentError):
            return True
        else:
            return False

    def _before_retry(self, exc: Exception) -> None:
        if not isinstance(exc, FetchSegmentError):
            return
        logger.warning(
            'Fetch segments failed continuously, trying to update the stream url.'
        )
        self._stream_url_resolver.reset()
        self._stream_url_resolver.rotate_routes()
