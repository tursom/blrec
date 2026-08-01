"""原始弹幕 JSONL 的等待、开播前暂存和随视频写入状态机。"""

import asyncio
import json
import os
from enum import Enum
from typing import Optional

import aiofiles
from loguru import logger

from blrec.bili.live import Live
from blrec.event.event_emitter import EventEmitter, EventListener
from blrec.exception import exception_callback, submit_exception
from blrec.logging.context import async_task_with_logger_context
from blrec.path import raw_danmaku_path
from blrec.utils.mixins import SwitchableMixin

from .raw_danmaku_receiver import RawDanmakuReceiver
from .stream_recorder import StreamRecorder

__all__ = (
    'RawDanmakuDumper',
    'RawDanmakuDumperEventListener',
    'RawDanmakuDumpingState',
)


class RawDanmakuDumperEventListener(EventListener):
    async def on_raw_danmaku_file_created(self, path: str) -> None:
        ...

    async def on_raw_danmaku_file_completed(self, path: str) -> None:
        ...


class RawDanmakuDumpingState(str, Enum):
    IDLE = 'idle'
    WAITING_DUMPING = 'waiting_dumping'
    LIVE_PRELUDE_SPOOLING = 'live_prelude_spooling'
    LIVE_DUMPING = 'live_dumping'


class RawDanmakuDumper(
    EventEmitter[RawDanmakuDumperEventListener],
    SwitchableMixin,
):
    """串行切换三种写入阶段，并用临时文件保护 prelude 的可见性。"""

    def __init__(
        self,
        live: Live,
        stream_recorder: StreamRecorder,
        danmaku_receiver: RawDanmakuReceiver,
    ) -> None:
        super().__init__()
        self._logger_context = {'room_id': live.room_id}
        self._logger = logger.bind(**self._logger_context)
        self._stream_recorder = stream_recorder
        self._receiver = danmaku_receiver
        self._lock = asyncio.Lock()
        self._state = RawDanmakuDumpingState.IDLE
        self._path: Optional[str] = None
        self._spool_path: Optional[str] = None
        self._prelude_path: Optional[str] = None

    def _do_enable(self) -> None:
        self._logger.debug('Enabled raw danmaku dumper')

    def _do_disable(self) -> None:
        asyncio.create_task(self.shutdown())
        self._logger.debug('Disabled raw danmaku dumper')

    @property
    def state(self) -> RawDanmakuDumpingState:
        return self._state

    async def start_waiting(self) -> None:
        async with self._lock:
            if self._state == RawDanmakuDumpingState.WAITING_DUMPING:
                return
            await self._stop_locked(finalize_prelude=False)
            self._path, _ = self._stream_recorder.make_waiting_raw_danmaku_path()
            self._create_dump_task(self._path)
            self._state = RawDanmakuDumpingState.WAITING_DUMPING

    async def stop_waiting(self) -> None:
        async with self._lock:
            if self._state != RawDanmakuDumpingState.WAITING_DUMPING:
                return
            await self._stop_dumping_task()
            self._reset_paths()
            self._state = RawDanmakuDumpingState.IDLE

    async def start_live_prelude(self) -> None:
        async with self._lock:
            if self._state == RawDanmakuDumpingState.LIVE_PRELUDE_SPOOLING:
                return
            await self._stop_locked(finalize_prelude=False)
            self._prelude_path, _ = self._stream_recorder.make_prelude_raw_danmaku_path()
            # 开播但视频文件尚未创建时写临时 spool，不对外发送文件完成事件。
            self._spool_path = self._prelude_path + '.tmp'
            self._create_dump_task(self._spool_path, emit_events=False)
            self._path = None
            self._state = RawDanmakuDumpingState.LIVE_PRELUDE_SPOOLING

    async def start_live_dumping(self, video_path: str) -> None:
        async with self._lock:
            initial_data_path = None
            if self._state == RawDanmakuDumpingState.LIVE_PRELUDE_SPOOLING:
                # 停止 spool writer 后再复制，避免读取到半条 JSONL 记录。
                await self._stop_dumping_task()
                initial_data_path = self._spool_path
            elif self._state == RawDanmakuDumpingState.WAITING_DUMPING:
                await self._stop_dumping_task()
            elif self._state == RawDanmakuDumpingState.LIVE_DUMPING:
                await self._stop_dumping_task()

            self._path = raw_danmaku_path(video_path)
            self._create_dump_task(self._path, initial_data_path=initial_data_path)
            self._spool_path = None
            self._prelude_path = None
            self._state = RawDanmakuDumpingState.LIVE_DUMPING

    async def complete_live_dumping(self) -> None:
        async with self._lock:
            if self._state != RawDanmakuDumpingState.LIVE_DUMPING:
                return
            await self._stop_dumping_task()
            self._reset_paths()
            self._state = RawDanmakuDumpingState.IDLE

    async def stop_live(self) -> None:
        async with self._lock:
            await self._stop_locked(finalize_prelude=True)

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop_locked(finalize_prelude=True)

    def _create_dump_task(
        self,
        path: str,
        *,
        emit_events: bool = True,
        initial_data_path: Optional[str] = None,
    ) -> None:
        self._dump_task = asyncio.create_task(
            self._do_dump(
                path,
                emit_events=emit_events,
                initial_data_path=initial_data_path,
            )
        )
        self._dump_task.add_done_callback(exception_callback)

    async def _cancel_dump_task(self) -> None:
        self._dump_task.cancel()
        try:
            await self._dump_task
        except asyncio.CancelledError:
            pass

    async def _stop_dumping_task(self) -> None:
        if not hasattr(self, '_dump_task'):
            return
        await self._cancel_dump_task()
        del self._dump_task  # type: ignore

    async def _stop_locked(self, *, finalize_prelude: bool) -> None:
        # finalize 用于真正结束直播；普通状态切换则丢弃尚未归属视频的临时数据。
        if self._state == RawDanmakuDumpingState.LIVE_PRELUDE_SPOOLING:
            await self._stop_dumping_task()
            if finalize_prelude:
                await self._finalize_prelude_file()
            else:
                await self._discard_spool()
        elif self._state in (
            RawDanmakuDumpingState.WAITING_DUMPING,
            RawDanmakuDumpingState.LIVE_DUMPING,
        ):
            await self._stop_dumping_task()

        self._reset_paths()
        self._state = RawDanmakuDumpingState.IDLE

    async def _discard_spool(self) -> None:
        if self._spool_path is not None and os.path.exists(self._spool_path):
            os.remove(self._spool_path)

    async def _finalize_prelude_file(self) -> None:
        if self._spool_path is None or self._prelude_path is None:
            return
        if os.path.exists(self._spool_path):
            # 原子 rename 后文件才对外可见，并成对发出 created/completed 事件。
            os.replace(self._spool_path, self._prelude_path)
            self._logger.info(f"Raw danmaku file created: '{self._prelude_path}'")
            await self._emit('raw_danmaku_file_created', self._prelude_path)
            self._logger.info(f"Raw danmaku file completed: '{self._prelude_path}'")
            await self._emit('raw_danmaku_file_completed', self._prelude_path)

    def _reset_paths(self) -> None:
        self._path = None
        self._spool_path = None
        self._prelude_path = None

    @async_task_with_logger_context
    async def _do_dump(
        self,
        path: str,
        *,
        emit_events: bool = True,
        initial_data_path: Optional[str] = None,
    ) -> None:
        self._logger.debug('Started dumping raw danmaku')
        try:
            async with aiofiles.open(path, 'wt', encoding='utf8') as f:
                if emit_events:
                    self._logger.info(f"Raw danmaku file created: '{path}'")
                    await self._emit('raw_danmaku_file_created', path)

                if initial_data_path is not None and os.path.exists(initial_data_path):
                    # prelude 必须先写入目标文件，随后才消费实时队列以保持时间顺序。
                    async with aiofiles.open(
                        initial_data_path, 'rt', encoding='utf8'
                    ) as initial_file:
                        async for line in initial_file:
                            await f.write(line)
                    os.remove(initial_data_path)

                while True:
                    danmu = await self._receiver.get_raw_danmaku()
                    json_string = json.dumps(danmu, ensure_ascii=False)
                    await f.write(json_string + '\n')
        except Exception as e:
            submit_exception(e)
            raise
        finally:
            if emit_events:
                self._logger.info(f"Raw danmaku file completed: '{path}'")
                await self._emit('raw_danmaku_file_completed', path)
            self._logger.debug('Stopped dumping raw danmaku')
