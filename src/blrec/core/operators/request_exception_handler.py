"""对瞬时请求异常限速重试，并在特定 HTTP 状态后轮换流地址。"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, TypeVar

import aiohttp
import requests
import urllib3
from loguru import logger
from reactivex import Observable, abc
from reactivex import operators as ops

from blrec.core import operators as core_ops
from blrec.http_history import record_http_exchange
from blrec.utils import operators as utils_ops

__all__ = ('RequestExceptionHandler',)


_T = TypeVar('_T')


class RequestExceptionHandler:
    def __init__(self, stream_url_resolver: core_ops.StreamURLResolver) -> None:
        self._stream_url_resolver = stream_url_resolver
        self._last_retry_time = time.monotonic()

    def __call__(self, source: Observable[_T]) -> Observable[_T]:
        return self._handle(source).pipe(
            ops.do_action(on_error=self._before_retry),
            utils_ops.retry(should_retry=self._should_retry),
        )

    def _handle(self, source: Observable[_T]) -> Observable[_T]:
        def subscribe(
            observer: abc.ObserverBase[_T],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            def on_error(exc: Exception) -> None:
                response = getattr(exc, 'response', None)
                record_http_exchange(
                    self._stream_url_resolver.live.http_history,
                    room_id=self._stream_url_resolver.live.room_id,
                    category='stream_read',
                    method='GET',
                    url=self._stream_url_resolver.stream_url,
                    request_headers=self._stream_url_resolver.live.headers,
                    response_status=getattr(response, 'status_code', None),
                    response_headers=getattr(response, 'headers', None),
                    error=exc,
                    parent_operation_id=(
                        self._stream_url_resolver.live.http_history_connection_id
                    ),
                )
                self._stream_url_resolver.live.http_history_connection_id = None
                try:
                    raise exc
                except requests.exceptions.RequestException:  # XXX: ConnectionError
                    logger.warning(repr(exc))
                except urllib3.exceptions.HTTPError:
                    logger.warning(repr(exc))
                except asyncio.exceptions.TimeoutError:
                    logger.warning(repr(exc))
                except aiohttp.ClientError:
                    logger.warning(repr(exc))
                except Exception:
                    pass

                if self._should_retry(exc):
                    if time.monotonic() - self._last_retry_time < 1:
                        time.sleep(1)
                    self._last_retry_time = time.monotonic()

                observer.on_error(exc)

            return source.subscribe(
                observer.on_next, on_error, observer.on_completed, scheduler=scheduler
            )

        return Observable(subscribe)

    def _should_retry(self, exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                requests.exceptions.RequestException,  # XXX: ConnectionError
                urllib3.exceptions.HTTPError,
                asyncio.exceptions.TimeoutError,
                aiohttp.ClientError,
            ),
        ):
            return True
        else:
            return False

    def _before_retry(self, exc: Exception) -> None:
        if isinstance(
            exc, requests.exceptions.HTTPError
        ) and exc.response.status_code in (403, 404):
            # 当前 CDN 地址可能过期或被拒绝，重置 resolver 后从下一路由重新解析。
            self._stream_url_resolver.reset()
            self._stream_url_resolver.rotate_routes()
