from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Recorder package initialization expects settings/application to be loaded first.
# isort: off
from blrec.setting import Settings  # noqa: F401
from blrec.application import Application  # noqa: F401

# isort: on
from blrec.task.task_manager import RecordTaskManager


class FakeRecordTask:
    events: list[tuple] = []
    fail_processing_room_id: int | None = None
    fail_monitor_once_room_id: int | None = None
    failed_monitor_rooms: set[int] = set()

    def __init__(self, room_id: int, **kwargs) -> None:
        self.room_id = room_id
        self.ready = False
        self.out_dir = ''

    async def setup(self) -> None:
        self.events.append(('setup', self.room_id))
        self.ready = True

    async def process_existing_files(self, paths) -> None:
        self.events.append(('process', self.room_id, tuple(paths)))
        if self.room_id == self.fail_processing_room_id:
            raise RuntimeError('postprocessing failed')

    async def enable_monitor(self) -> None:
        self.events.append(('monitor', self.room_id))
        if (
            self.room_id == self.fail_monitor_once_room_id
            and self.room_id not in self.failed_monitor_rooms
        ):
            self.failed_monitor_rooms.add(self.room_id)
            raise asyncio.TimeoutError

    async def enable_recorder(self) -> None:
        self.events.append(('recorder', self.room_id))

    async def destroy(self) -> None:
        self.events.append(('destroy', self.room_id))
        self.ready = False


class FakeSettingsManager:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.manager = None

    def get_settings(self, include):
        if include == {'tasks'}:
            return SimpleNamespace(tasks=self.settings)
        return SimpleNamespace(
            bili_api=SimpleNamespace(
                base_api_urls=[], base_live_api_urls=[], base_play_info_api_urls=[]
            )
        )

    async def apply_task_header_settings(self, *args, **kwargs) -> None:
        pass

    def apply_task_output_settings(self, room_id, output) -> None:
        self.manager._tasks[room_id].out_dir = output.out_dir

    def apply_task_danmaku_settings(self, *args) -> None:
        pass

    def apply_task_recorder_settings(self, *args) -> None:
        pass

    def apply_task_postprocessing_settings(self, *args) -> None:
        pass


def make_settings(
    room_id: int, *, enable_monitor: bool = True, enable_recorder: bool = True
):
    return SimpleNamespace(
        room_id=room_id,
        header=object(),
        output=SimpleNamespace(out_dir='/recordings'),
        danmaku=object(),
        recorder=object(),
        postprocessing=object(),
        enable_monitor=enable_monitor,
        enable_recorder=enable_recorder,
    )


class HLSTaskRecoveryTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeRecordTask.events = []
        FakeRecordTask.fail_processing_room_id = None
        FakeRecordTask.fail_monitor_once_room_id = None
        FakeRecordTask.failed_monitor_rooms = set()
        self.http_history = Mock()

    def make_manager(self, settings) -> RecordTaskManager:
        settings_manager = FakeSettingsManager(settings)
        manager = RecordTaskManager(settings_manager, self.http_history)
        settings_manager.manager = manager
        return manager

    async def test_persisted_tasks_recover_together_before_activation(self) -> None:
        settings = [
            make_settings(1, enable_recorder=False),
            make_settings(2, enable_monitor=False),
        ]
        manager = self.make_manager(settings)

        def recover(out_dirs, room_ids):
            FakeRecordTask.events.append(('scan', tuple(out_dirs), frozenset(room_ids)))
            return {1: ['/recordings/one.m4s'], 2: ['/recordings/two.m4s']}

        with (
            patch('blrec.task.task_manager.RecordTask', FakeRecordTask),
            patch(
                'blrec.task.task_manager.recover_incomplete_hls_recordings',
                side_effect=recover,
                create=True,
            ),
        ):
            await manager.load_all_tasks()

        self.assertEqual(
            FakeRecordTask.events,
            [
                ('setup', 1),
                ('setup', 2),
                ('scan', ('/recordings',), frozenset({1, 2})),
                ('process', 1, ('/recordings/one.m4s',)),
                ('process', 2, ('/recordings/two.m4s',)),
                ('monitor', 1),
                ('recorder', 2),
            ],
        )
        incident_calls = self.http_history.mark_incident.call_args_list
        self.assertEqual(len(incident_calls), 2)
        self.assertEqual({call.kwargs['room_id'] for call in incident_calls}, {1, 2})
        self.assertTrue(
            all(call.kwargs['kind'] == 'hls_crash_recovered' for call in incident_calls)
        )

    async def test_runtime_task_recovers_before_activation(self) -> None:
        settings = make_settings(3)
        manager = self.make_manager([])

        def recover(out_dirs, room_ids):
            FakeRecordTask.events.append(('scan', tuple(out_dirs), frozenset(room_ids)))
            return {3: ['/recordings/three.m4s']}

        with (
            patch('blrec.task.task_manager.RecordTask', FakeRecordTask),
            patch(
                'blrec.task.task_manager.recover_incomplete_hls_recordings',
                side_effect=recover,
                create=True,
            ),
        ):
            await manager.add_task(settings)

        self.assertEqual(
            FakeRecordTask.events,
            [
                ('setup', 3),
                ('scan', ('/recordings',), frozenset({3})),
                ('process', 3, ('/recordings/three.m4s',)),
                ('monitor', 3),
                ('recorder', 3),
            ],
        )

    async def test_recovery_failure_does_not_block_other_tasks(self) -> None:
        settings = [make_settings(1), make_settings(2)]
        manager = self.make_manager(settings)
        FakeRecordTask.fail_processing_room_id = 1

        with (
            patch('blrec.task.task_manager.RecordTask', FakeRecordTask),
            patch(
                'blrec.task.task_manager.recover_incomplete_hls_recordings',
                return_value={1: ['one.m4s'], 2: ['two.m4s']},
            ),
            patch('blrec.task.task_manager.submit_exception'),
        ):
            await manager.load_all_tasks()

        self.assertIn(('process', 1, ('one.m4s',)), FakeRecordTask.events)
        self.assertIn(('process', 2, ('two.m4s',)), FakeRecordTask.events)
        self.assertIn(('monitor', 1), FakeRecordTask.events)
        self.assertIn(('recorder', 1), FakeRecordTask.events)
        self.assertIn(('monitor', 2), FakeRecordTask.events)
        self.assertIn(('recorder', 2), FakeRecordTask.events)

    async def test_transient_activation_failure_reassembles_task_after_recovery(
        self,
    ) -> None:
        settings = [make_settings(1)]
        manager = self.make_manager(settings)
        FakeRecordTask.fail_monitor_once_room_id = 1

        def recover(out_dirs, room_ids):
            FakeRecordTask.events.append(('scan', tuple(out_dirs), frozenset(room_ids)))
            return {}

        with (
            patch('blrec.task.task_manager.RecordTask', FakeRecordTask),
            patch(
                'blrec.task.task_manager.recover_incomplete_hls_recordings',
                side_effect=recover,
            ),
            patch('blrec.task.task_manager.submit_exception'),
        ):
            await manager.load_all_tasks()

        self.assertEqual(
            FakeRecordTask.events,
            [
                ('setup', 1),
                ('scan', ('/recordings',), frozenset({1})),
                ('monitor', 1),
                ('destroy', 1),
                ('setup', 1),
                ('monitor', 1),
                ('recorder', 1),
            ],
        )


if __name__ == '__main__':
    unittest.main()
