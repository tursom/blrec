"""把解析出的直播流 URL 转换为 requests 流式响应。"""

from __future__ import annotations

import io
import time
import uuid
from typing import Optional

import requests
from loguru import logger
from reactivex import Observable, abc

from blrec.bili.live import Live
from blrec.http_history import record_http_exchange
from blrec.utils.mixins import AsyncCooperationMixin

__all__ = ('StreamFetcher',)


class StreamFetcher(AsyncCooperationMixin):
    """在 Rx 调度线程中发起同步请求，把响应体交给后续解析器按需读取。"""

    def __init__(
        self,
        live: Live,
        session: requests.Session,
        *,
        read_timeout: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._live = live
        self._session = session
        self.read_timeout = read_timeout or 3
        self._operation_id: Optional[str] = None

    def __call__(self, source: Observable[str]) -> Observable[io.RawIOBase]:
        return self._fetch(source)

    def _fetch(self, source: Observable[str]) -> Observable[io.RawIOBase]:
        def subscribe(
            observer: abc.ObserverBase[io.RawIOBase],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            def on_next(url: str) -> None:
                started_at = time.perf_counter()
                if self._operation_id is None:
                    self._operation_id = uuid.uuid4().hex
                try:
                    logger.info(f'Requesting live stream... {url}')
                    response = self._session.get(
                        url,
                        stream=True,
                        headers=self._live.headers,
                        # requests 的单值 timeout 同时约束连接和相邻两次读取等待时间。
                        timeout=self.read_timeout,
                    )
                    logger.info('Response received')
                    response.raise_for_status()
                except Exception as e:
                    response = getattr(e, 'response', None)
                    record_http_exchange(
                        self._live.http_history,
                        room_id=self._live.room_id,
                        category='flv_handshake',
                        method='GET',
                        url=url,
                        request_headers=self._live.headers,
                        response_status=getattr(response, 'status_code', None),
                        response_headers=getattr(response, 'headers', None),
                        error=e,
                        duration_ms=(time.perf_counter() - started_at) * 1000,
                        operation_id=self._operation_id,
                    )
                    logger.warning(f'Failed to request live stream: {repr(e)}')
                    observer.on_error(e)
                else:
                    record_http_exchange(
                        self._live.http_history,
                        room_id=self._live.room_id,
                        category='flv_handshake',
                        method='GET',
                        url=response.url,
                        request_headers=response.request.headers,
                        response_status=response.status_code,
                        response_headers=response.headers,
                        duration_ms=(time.perf_counter() - started_at) * 1000,
                        operation_id=self._operation_id,
                    )
                    self._live.http_history_connection_id = self._operation_id
                    self._operation_id = None
                    observer.on_next(response.raw)  # urllib3.response.HTTPResponse

            return source.subscribe(
                on_next, observer.on_error, observer.on_completed, scheduler=scheduler
            )

        return Observable(subscribe)
