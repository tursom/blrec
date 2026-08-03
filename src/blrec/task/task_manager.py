"""按真实房间号管理 RecordTask，并在设置层与任务聚合之间传递配置。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional, Tuple

import aiohttp
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_exponential

from blrec.utils.libc import malloc_trim

from ..bili.exceptions import ApiRequestError
from ..core.typing import MetaData
from ..exception import NotFoundError, submit_exception
from ..flv.operators import StreamProfile
from ..hls.recovery import recover_incomplete_hls_recordings
from ..http_history import mark_http_incident
from .models import DanmakuFileDetail, TaskData, TaskParam, VideoFileDetail
from .task import RecordTask

if TYPE_CHECKING:
    from ..setting import SettingsManager
    from ..http_history import HttpHistoryStore

from loguru import logger

from ..setting import (
    BiliApiSettings,
    DanmakuSettings,
    HeaderSettings,
    OutputSettings,
    PostprocessingSettings,
    RecorderSettings,
    TaskSettings,
)

__all__ = ('RecordTaskManager',)


_retry_task_operation = retry(
    reraise=True,
    retry=retry_if_exception_type(
        (asyncio.TimeoutError, aiohttp.ClientError, ApiRequestError)
    ),
    wait=wait_exponential(max=10),
    stop=stop_after_delay(60),
)


class RecordTaskManager:
    """拥有所有房间任务对象，并协调任务的装配、启停和销毁。"""

    def __init__(
        self, settings_manager: SettingsManager, http_history: HttpHistoryStore
    ) -> None:
        self._settings_manager = settings_manager
        self._http_history = http_history
        self._tasks: Dict[int, RecordTask] = {}

    async def load_all_tasks(self) -> None:
        """从持久化设置恢复任务；单个房间失败不会阻止其余房间加载。"""

        logger.info('Loading all tasks...')

        settings_list = self._settings_manager.get_settings({'tasks'}).tasks
        assert settings_list is not None

        assembled: List[Tuple[TaskSettings, RecordTask]] = []
        for settings in settings_list:
            logger.info(f'Adding task {settings.room_id}...')
            try:
                task = await self._assemble_task_with_retry(settings)
            except Exception as e:
                submit_exception(e)

            else:
                assembled.append((settings, task))

        await self._recover_tasks(
            {settings.room_id: task for settings, task in assembled}
        )

        for settings, _task in assembled:
            try:
                await self._activate_loaded_task_with_retry(settings)
            except Exception as e:
                submit_exception(e)
            else:
                logger.info(f'Successfully added task {settings.room_id}')

        logger.info('Load all tasks complete')

    async def destroy_all_tasks(self) -> None:
        logger.debug('Destroying all tasks...')
        for task in self._tasks.values():
            if not task.ready:
                continue
            await task.destroy()
        self._tasks.clear()
        malloc_trim(0)
        logger.debug('Successfully destroyed all task')

    def has_task(self, room_id: int) -> bool:
        return room_id in self._tasks

    @_retry_task_operation
    async def add_task(self, settings: TaskSettings) -> None:
        logger.info(f'Adding task {settings.room_id}...')

        task = await self._assemble_task(settings)
        try:
            await self._recover_tasks({settings.room_id: task})
            await self._activate_task(task, settings)
        except BaseException as e:
            await self._discard_failed_task(settings.room_id, task, e)
            raise

        logger.info(f'Successfully added task {settings.room_id}')

    @_retry_task_operation
    async def _assemble_task_with_retry(
        self, settings: TaskSettings
    ) -> RecordTask:
        return await self._assemble_task(settings)

    async def _assemble_task(self, settings: TaskSettings) -> RecordTask:
        # 先注册占位任务，使并发查询能识别“存在但尚未 ready”的状态。
        task = RecordTask(settings.room_id, http_history=self._http_history)
        self._tasks[settings.room_id] = task

        try:
            bili_api = self._settings_manager.get_settings({'bili_api'}).bili_api
            assert bili_api is not None
            self.apply_task_bili_api_settings(settings.room_id, bili_api)
            await self._settings_manager.apply_task_header_settings(
                settings.room_id, settings.header, restart_danmaku_client=False
            )
            await task.setup()

            # setup 完成后再应用其余配置，确保底层 recorder/client 已经创建。
            self._settings_manager.apply_task_output_settings(
                settings.room_id, settings.output
            )
            self._settings_manager.apply_task_danmaku_settings(
                settings.room_id, settings.danmaku
            )
            self._settings_manager.apply_task_recorder_settings(
                settings.room_id, settings.recorder
            )
            self._settings_manager.apply_task_postprocessing_settings(
                settings.room_id, settings.postprocessing
            )
        except BaseException as exc:
            await self._discard_failed_task(settings.room_id, task, exc)
            raise

        return task

    async def _recover_tasks(self, tasks: Dict[int, RecordTask]) -> None:
        if not tasks:
            return

        out_dirs = sorted({task.out_dir for task in tasks.values()})
        loop = asyncio.get_running_loop()
        try:
            recovered = await loop.run_in_executor(
                None,
                partial(
                    recover_incomplete_hls_recordings, out_dirs, set(tasks.keys())
                ),
            )
        except Exception as exc:
            logger.warning(f'Failed to scan incomplete HLS recordings: {exc!r}')
            submit_exception(exc)
            return

        for room_id, paths in recovered.items():
            task = tasks.get(room_id)
            if task is None:
                continue
            mtimes = []
            for path in paths:
                try:
                    mtimes.append(os.path.getmtime(path))
                except OSError:
                    pass
            occurred_at = (
                datetime.fromtimestamp(max(mtimes), timezone.utc) if mtimes else None
            )
            mark_http_incident(
                self._http_history,
                room_id=room_id,
                kind='hls_crash_recovered',
                details={'paths': paths},
                occurred_at=occurred_at,
            )
            try:
                await task.process_existing_files(paths)
            except Exception as exc:
                logger.warning(
                    f'Failed to postprocess recovered HLS files for room '
                    f'{room_id}: {exc!r}'
                )
                submit_exception(exc)

    async def _activate_task(
        self, task: RecordTask, settings: TaskSettings
    ) -> None:
        if settings.enable_monitor:
            await task.enable_monitor()
        if settings.enable_recorder:
            await task.enable_recorder()

    @_retry_task_operation
    async def _activate_loaded_task_with_retry(
        self, settings: TaskSettings
    ) -> None:
        task = self._tasks.get(settings.room_id)
        if task is None:
            task = await self._assemble_task(settings)
        try:
            await self._activate_task(task, settings)
        except BaseException as exc:
            await self._discard_failed_task(settings.room_id, task, exc)
            raise

    async def _discard_failed_task(
        self, room_id: int, task: RecordTask, exc: BaseException
    ) -> None:
        logger.error(f'Failed to add task {room_id} due to: {repr(exc)}')
        # 装配必须具备事务性：任何阶段失败都清理已创建组件并撤销索引。
        await task.destroy()
        self._tasks.pop(room_id, None)

    async def remove_task(self, room_id: int) -> None:
        logger.debug(f'Removing task {room_id}...')
        task = self._get_task(room_id, check_ready=True)
        # 删除任务不等待后处理排空；强制停止后再拆除 monitor 和聚合对象。
        await task.disable_recorder(force=True)
        await task.disable_monitor()
        await task.destroy()
        del self._tasks[room_id]
        malloc_trim(0)
        logger.debug(f'Removed task {room_id}')

    async def remove_all_tasks(self) -> None:
        logger.debug('Removing all tasks...')
        for room_id, task in self._tasks.copy().items():
            if not task.ready:
                continue
            await self.remove_task(room_id)
        malloc_trim(0)
        logger.debug('Removed all tasks')

    async def start_task(self, room_id: int) -> None:
        logger.debug(f'Starting task {room_id}...')
        task = self._get_task(room_id, check_ready=True)
        await task.update_info()
        await task.enable_monitor()
        await task.enable_recorder()
        logger.debug(f'Started task {room_id}')

    async def stop_task(self, room_id: int, force: bool = False) -> None:
        logger.debug(f'Stopping task {room_id}...')
        task = self._get_task(room_id, check_ready=True)
        await task.disable_recorder(force)
        await task.disable_monitor()
        logger.debug(f'Stopped task {room_id}')

    async def start_all_tasks(self) -> None:
        logger.debug('Starting all tasks...')
        for room_id, task in self._tasks.items():
            if not task.ready:
                continue
            await self.start_task(room_id)
        logger.debug('Started all tasks')

    async def stop_all_tasks(self, force: bool = False) -> None:
        logger.debug('Stopping all tasks...')
        for room_id, task in self._tasks.items():
            if not task.ready:
                continue
            await self.stop_task(room_id, force=force)
        logger.debug('Stopped all tasks')

    async def enable_task_monitor(self, room_id: int) -> None:
        logger.debug(f'Enabling live monitor for task {room_id}...')
        task = self._get_task(room_id, check_ready=True)
        await task.enable_monitor()
        logger.debug(f'Enabled live monitor for task {room_id}')

    async def disable_task_monitor(self, room_id: int) -> None:
        logger.debug(f'Disabling live monitor for task {room_id}...')
        task = self._get_task(room_id, check_ready=True)
        await task.disable_monitor()
        logger.debug(f'Disabled live monitor for task {room_id}')

    async def enable_all_task_monitors(self) -> None:
        logger.debug('Enabling live monitor for all tasks...')
        for room_id, task in self._tasks.items():
            if not task.ready:
                continue
            await self.enable_task_monitor(room_id)
        logger.debug('Enabled live monitor for all tasks')

    async def disable_all_task_monitors(self) -> None:
        logger.debug('Disabling live monitor for all tasks...')
        for room_id, task in self._tasks.items():
            if not task.ready:
                continue
            await self.disable_task_monitor(room_id)
        logger.debug('Disabled live monitor for all tasks')

    async def enable_task_recorder(self, room_id: int) -> None:
        logger.debug(f'Enabling recorder for task {room_id}...')
        task = self._get_task(room_id, check_ready=True)
        await task.enable_recorder()
        logger.debug(f'Enabled recorder for task {room_id}')

    async def disable_task_recorder(self, room_id: int, force: bool = False) -> None:
        logger.debug(f'Disabling recorder for task {room_id}...')
        task = self._get_task(room_id, check_ready=True)
        await task.disable_recorder(force)
        logger.debug(f'Disabled recorder for task {room_id}')

    async def enable_all_task_recorders(self) -> None:
        logger.debug('Enabling recorder for all tasks...')
        for room_id, task in self._tasks.items():
            if not task.ready:
                continue
            await self.enable_task_recorder(room_id)
        logger.debug('Enabled recorder for all tasks')

    async def disable_all_task_recorders(self, force: bool = False) -> None:
        logger.debug('Disabling recorder for all tasks...')
        for room_id, task in self._tasks.items():
            if not task.ready:
                continue
            await self.disable_task_recorder(room_id, force=force)
        logger.debug('Disabled recorder for all tasks')

    def get_task_data(self, room_id: int) -> TaskData:
        task = self._get_task(room_id, check_ready=True)
        return self._make_task_data(task)

    def get_all_task_data(self) -> Iterator[TaskData]:
        for task in filter(lambda t: t.ready, self._tasks.values()):
            yield self._make_task_data(task)

    def get_task_param(self, room_id: int) -> TaskParam:
        task = self._get_task(room_id, check_ready=True)
        return self._make_task_param(task)

    def get_task_metadata(self, room_id: int) -> Optional[MetaData]:
        task = self._get_task(room_id, check_ready=True)
        return task.metadata

    def get_task_stream_profile(self, room_id: int) -> StreamProfile:
        task = self._get_task(room_id, check_ready=True)
        return task.stream_profile

    def get_task_video_file_details(self, room_id: int) -> Iterator[VideoFileDetail]:
        task = self._get_task(room_id, check_ready=True)
        yield from task.video_file_details

    def get_task_danmaku_file_details(
        self, room_id: int
    ) -> Iterator[DanmakuFileDetail]:
        task = self._get_task(room_id, check_ready=True)
        yield from task.danmaku_file_details

    def can_cut_stream(self, room_id: int) -> bool:
        task = self._get_task(room_id, check_ready=True)
        return task.can_cut_stream()

    def cut_stream(self, room_id: int) -> bool:
        task = self._get_task(room_id, check_ready=True)
        return task.cut_stream()

    async def update_task_info(self, room_id: int) -> None:
        logger.debug(f'Updating info for task {room_id}...')
        task = self._get_task(room_id, check_ready=True)
        await task.update_info(raise_exception=True)
        logger.debug(f'Updated info for task {room_id}')

    async def update_all_task_infos(self) -> None:
        logger.debug('Updating info for all tasks...')
        for room_id, task in self._tasks.items():
            if not task.ready:
                continue
            await self.update_task_info(room_id)
        logger.debug('Updated info for all tasks')

    def apply_task_bili_api_settings(
        self, room_id: int, settings: BiliApiSettings
    ) -> None:
        task = self._get_task(room_id)
        task.base_api_urls = settings.base_api_urls
        task.base_live_api_urls = settings.base_live_api_urls
        task.base_play_info_api_urls = settings.base_play_info_api_urls

    async def apply_task_header_settings(
        self,
        room_id: int,
        settings: HeaderSettings,
        *,
        restart_danmaku_client: bool = True,
    ) -> None:
        task = self._get_task(room_id)
        changed = False
        if task.user_agent != settings.user_agent:
            task.user_agent = settings.user_agent
            changed = True
        if task.cookie != settings.cookie:
            task.cookie = settings.cookie
            changed = True
        # 已建立的弹幕 WebSocket 不会自动采用新 Header，必须显式重连。
        if changed and restart_danmaku_client:
            await task.restart_danmaku_client()

    def apply_task_output_settings(
        self, room_id: int, settings: OutputSettings
    ) -> None:
        task = self._get_task(room_id)
        task.out_dir = settings.out_dir
        task.path_template = settings.path_template
        task.filesize_limit = settings.filesize_limit
        task.duration_limit = settings.duration_limit

    def apply_task_danmaku_settings(
        self, room_id: int, settings: DanmakuSettings
    ) -> None:
        task = self._get_task(room_id)
        task.danmu_uname = settings.danmu_uname
        task.record_gift_send = settings.record_gift_send
        task.record_free_gifts = settings.record_free_gifts
        task.record_guard_buy = settings.record_guard_buy
        task.record_super_chat = settings.record_super_chat
        task.save_raw_danmaku = settings.save_raw_danmaku
        task.record_raw_danmaku_during_waiting = (
            settings.record_raw_danmaku_during_waiting
        )

    def apply_task_recorder_settings(
        self, room_id: int, settings: RecorderSettings
    ) -> None:
        task = self._get_task(room_id)
        task.stream_format = settings.stream_format
        task.recording_mode = settings.recording_mode
        task.quality_number = settings.quality_number
        task.fmp4_stream_timeout = settings.fmp4_stream_timeout
        task.read_timeout = settings.read_timeout
        task.disconnection_timeout = settings.disconnection_timeout
        task.buffer_size = settings.buffer_size
        task.save_cover = settings.save_cover
        task.cover_save_strategy = settings.cover_save_strategy

    def apply_task_postprocessing_settings(
        self, room_id: int, settings: PostprocessingSettings
    ) -> None:
        task = self._get_task(room_id)
        task.remux_to_mp4 = settings.remux_to_mp4
        task.inject_extra_metadata = settings.inject_extra_metadata
        task.delete_source = settings.delete_source

    def _get_task(self, room_id: int, check_ready: bool = False) -> RecordTask:
        """查找任务；对操作类请求可额外拒绝仍在异步装配中的任务。"""

        try:
            task = self._tasks[room_id]
        except KeyError:
            raise NotFoundError(f'no task for the room {room_id}')
        else:
            if check_ready and not task.ready:
                raise NotFoundError(f'the task {room_id} is not ready yet')
            return task

    def _make_task_param(self, task: RecordTask) -> TaskParam:
        return TaskParam(
            out_dir=task.out_dir,
            path_template=task.path_template,
            filesize_limit=task.filesize_limit,
            duration_limit=task.duration_limit,
            base_api_urls=task.base_api_urls,
            base_live_api_urls=task.base_live_api_urls,
            base_play_info_api_urls=task.base_play_info_api_urls,
            user_agent=task.user_agent,
            cookie=task.cookie,
            danmu_uname=task.danmu_uname,
            record_gift_send=task.record_gift_send,
            record_free_gifts=task.record_free_gifts,
            record_guard_buy=task.record_guard_buy,
            record_super_chat=task.record_super_chat,
            save_cover=task.save_cover,
            cover_save_strategy=task.cover_save_strategy,
            save_raw_danmaku=task.save_raw_danmaku,
            record_raw_danmaku_during_waiting=(
                task.record_raw_danmaku_during_waiting
            ),
            stream_format=task.stream_format,
            recording_mode=task.recording_mode,
            quality_number=task.quality_number,
            fmp4_stream_timeout=task.fmp4_stream_timeout,
            read_timeout=task.read_timeout,
            disconnection_timeout=task.disconnection_timeout,
            buffer_size=task.buffer_size,
            remux_to_mp4=task.remux_to_mp4,
            inject_extra_metadata=task.inject_extra_metadata,
            delete_source=task.delete_source,
        )

    def _make_task_data(self, task: RecordTask) -> TaskData:
        return TaskData(
            user_info=task.user_info, room_info=task.room_info, task_status=task.status
        )
