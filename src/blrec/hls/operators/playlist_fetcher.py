"""持续拉取 HLS playlist，并把 master playlist 解析到最高带宽变体。"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime
from typing import Optional

import m3u8
import requests
import urllib3
from loguru import logger
from reactivex import Observable, abc
from reactivex.disposable import CompositeDisposable, Disposable, SerialDisposable
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_exponential

from blrec.bili.live import Live
from blrec.http_history import (
    mark_http_incident,
    record_http_exchange,
    redirects_from_response,
)
from blrec.utils.mixins import SupportDebugMixin

__all__ = ('PlaylistFetcher',)


class PlaylistFetcher(SupportDebugMixin):
    def __init__(self, live: Live, session: requests.Session) -> None:
        super().__init__()
        self._init_for_debug(live.room_id)
        self._live = live
        self._session = session
        self._playlist_hashes = {}

    def __call__(self, source: Observable[str]) -> Observable[m3u8.M3U8]:
        return self._fetch(source)

    def _fetch(self, source: Observable[str]) -> Observable[m3u8.M3U8]:
        def subscribe(
            observer: abc.ObserverBase[m3u8.M3U8],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            if self._debug:
                path = '{}/playlist-{}-{}.m3u8'.format(
                    self._debug_dir,
                    self._live.room_id,
                    datetime.now().strftime('%Y-%m-%d-%H%M%S-%f'),
                )
                playlist_debug_file = open(path, 'wt', encoding='utf-8')

            disposed = False
            subscription = SerialDisposable()

            def on_next(url: str) -> None:
                logger.info(f'Fetching playlist... {url}')

                while not disposed:
                    try:
                        content = self._fetch_playlist(url)
                    except Exception as e:
                        logger.warning(f'Failed to fetch playlist: {repr(e)}')
                        observer.on_error(e)
                    else:
                        if self._debug:
                            playlist_debug_file.write(content + '\n')
                        try:
                            playlist = m3u8.loads(content, uri=url)
                        except Exception as exc:
                            mark_http_incident(
                                self._live.http_history,
                                room_id=self._live.room_id,
                                kind='hls_playlist_parse_failed',
                                details={'url': url, 'error': repr(exc)},
                            )
                            observer.on_error(exc)
                            return
                        if playlist.is_variant:
                            # master playlist 本身不含媒体分片，递归切换到最高带宽子清单。
                            url = self._get_best_quality_url(playlist)
                            logger.debug('Playlist changed to variant playlist')
                            on_next(url)
                        else:
                            observer.on_next(playlist)
                            time.sleep(1)

            def dispose() -> None:
                nonlocal disposed
                disposed = True
                if self._debug:
                    playlist_debug_file.close()

            subscription.disposable = source.subscribe(
                on_next, observer.on_error, observer.on_completed, scheduler=scheduler
            )

            return CompositeDisposable(subscription, Disposable(dispose))

        return Observable(subscribe)

    def _get_best_quality_url(self, playlist: m3u8.M3U8) -> str:
        sorted_playlists = sorted(
            playlist.playlists, key=lambda p: p.stream_info.bandwidth
        )
        return sorted_playlists[-1].absolute_uri

    def _fetch_playlist(self, url: str) -> str:
        operation_id = uuid.uuid4().hex
        try:
            return self._fetch_playlist_with_retry(url, operation_id)
        except Exception as exc:
            mark_http_incident(
                self._live.http_history,
                room_id=self._live.room_id,
                kind='hls_playlist_fetch_failed',
                operation_id=operation_id,
                details={'url': url, 'error': repr(exc)},
            )
            raise

    @retry(
        reraise=True,
        retry=retry_if_exception_type(
            (
                requests.exceptions.Timeout,
                urllib3.exceptions.TimeoutError,
                urllib3.exceptions.ProtocolError,
            )
        ),
        wait=wait_exponential(multiplier=0.1, max=1),
        stop=stop_after_delay(8),
    )
    def _fetch_playlist_with_retry(self, url: str, operation_id: str) -> str:
        started_at = time.perf_counter()
        try:
            response = self._session.get(url, headers=self._live.headers, timeout=3)
            response.raise_for_status()
        except Exception as e:
            failed_response = getattr(e, 'response', None)
            record_http_exchange(
                self._live.http_history,
                room_id=self._live.room_id,
                category='hls_playlist',
                method='GET',
                url=url,
                request_headers=self._live.headers,
                response_status=getattr(failed_response, 'status_code', None),
                response_headers=getattr(failed_response, 'headers', None),
                response_body=getattr(failed_response, 'text', None),
                error=e,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                operation_id=operation_id,
                max_body_size=1024 * 1024,
                redirects=redirects_from_response(failed_response),
            )
            logger.debug(f'Failed to fetch playlist: {repr(e)}')
            raise
        else:
            response.encoding = 'utf-8'
            content = response.text
            digest = hashlib.sha256(content.encode('utf8')).hexdigest()
            changed = self._playlist_hashes.get(url) != digest
            self._playlist_hashes[url] = digest
            record_http_exchange(
                self._live.http_history,
                room_id=self._live.room_id,
                category='hls_playlist',
                method='GET',
                url=response.url,
                request_headers=response.request.headers,
                response_status=response.status_code,
                response_headers=response.headers,
                response_body=content if changed else None,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                operation_id=operation_id,
                max_body_size=1024 * 1024,
                extra={'body_unchanged': not changed, 'body_sha256': digest},
                redirects=redirects_from_response(response),
            )
            return content
