"""在视频片段完成后下载直播封面，并可按内容哈希去重。"""

import hashlib
import time
import uuid
from enum import Enum
from threading import Lock
from typing import Set

import aiofiles
import aiohttp
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_fixed

from blrec.bili.live import Live
from blrec.bili.net import create_connector, timeout
from blrec.event.event_emitter import EventEmitter, EventListener
from blrec.exception import submit_exception
from blrec.http_history import record_http_exchange, redirects_from_response
from blrec.path import cover_path
from blrec.utils.hash import sha1sum
from blrec.utils.mixins import SwitchableMixin

from .stream_recorder import StreamRecorder, StreamRecorderEventListener

__all__ = 'CoverDownloader', 'CoverDownloaderEventListener'


class CoverDownloaderEventListener(EventListener):
    async def on_cover_image_downloaded(self, path: str) -> None:
        pass


class CoverSaveStrategy(Enum):
    DEFAULT = 'default'
    DEDUP = 'dedup'

    def __str__(self) -> str:
        return self.value

    # workaround for value serialization
    def __repr__(self) -> str:
        return str(self)


class CoverDownloader(
    EventEmitter[CoverDownloaderEventListener],
    StreamRecorderEventListener,
    SwitchableMixin,
):
    """把封面生命周期绑定到视频文件完成事件，而不是直播状态事件。"""

    def __init__(
        self,
        live: Live,
        stream_recorder: StreamRecorder,
        *,
        save_cover: bool = False,
        cover_save_strategy: CoverSaveStrategy = CoverSaveStrategy.DEFAULT,
    ) -> None:
        super().__init__()
        self._logger_context = {'room_id': live.room_id}
        self._logger = logger.bind(**self._logger_context)
        self._live = live
        self._stream_recorder = stream_recorder
        self._lock: Lock = Lock()
        # 去重范围限定为本次 Recorder 启用周期，不跨房间也不扫描历史文件。
        self._sha1_set: Set[str] = set()
        self.save_cover = save_cover
        self.cover_save_strategy = cover_save_strategy

    def _do_enable(self) -> None:
        self._sha1_set.clear()
        self._stream_recorder.add_listener(self)
        self._logger.debug('Enabled cover downloader')

    def _do_disable(self) -> None:
        self._stream_recorder.remove_listener(self)
        self._logger.debug('Disabled cover downloader')

    async def on_video_file_completed(self, video_path: str) -> None:
        with self._lock:
            if not self.save_cover:
                return
            await self._save_cover(video_path)

    async def _save_cover(self, video_path: str) -> None:
        try:
            await self._live.update_room_info()
            cover_url = self._live.room_info.cover
            data = await self._fetch_cover(cover_url)
            sha1 = sha1sum(data)
            if (
                self.cover_save_strategy == CoverSaveStrategy.DEDUP
                and sha1 in self._sha1_set
            ):
                return
            path = cover_path(video_path, ext=cover_url.rsplit('.', 1)[-1])
            await self._save_file(path, data)
            self._sha1_set.add(sha1)
        except Exception as e:
            self._logger.error(f'Failed to save cover image: {repr(e)}')
            submit_exception(e)
        else:
            self._logger.info(f'Saved cover image: {path}')
            await self._emit('cover_image_downloaded', path)

    async def _fetch_cover(self, url: str) -> bytes:
        return await self._fetch_cover_with_retry(url, uuid.uuid4().hex)

    @retry(reraise=True, wait=wait_fixed(1), stop=stop_after_attempt(3))
    async def _fetch_cover_with_retry(self, url: str, operation_id: str) -> bytes:
        started_at = time.perf_counter()
        response = None
        async with aiohttp.ClientSession(
            connector=create_connector(),
            raise_for_status=True,
            trust_env=True,
            timeout=timeout,
        ) as session:
            try:
                async with session.get(url) as response:
                    data = await response.read()
            except Exception as exc:
                record_http_exchange(
                    self._live.http_history,
                    room_id=self._live.room_id,
                    category='cover',
                    method='GET',
                    url=url,
                    request_headers=self._live.headers,
                    response_status=getattr(response, 'status', None),
                    response_headers=getattr(response, 'headers', None),
                    error=exc,
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    operation_id=operation_id,
                    redirects=redirects_from_response(response),
                )
                raise
            else:
                record_http_exchange(
                    self._live.http_history,
                    room_id=self._live.room_id,
                    category='cover',
                    method='GET',
                    url=str(response.url),
                    request_headers=response.request_info.headers,
                    response_status=response.status,
                    response_headers=response.headers,
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    operation_id=operation_id,
                    redirects=redirects_from_response(response),
                    extra={
                        'response_size': len(data),
                        'response_sha256': hashlib.sha256(data).hexdigest(),
                    },
                )
                return data

    async def _save_file(self, path: str, data: bytes) -> None:
        async with aiofiles.open(path, 'wb') as file:
            await file.write(data)
