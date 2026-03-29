import asyncio
import json
import os
import sys
import tempfile
import types
import unittest

sys.modules.setdefault('humanize', types.SimpleNamespace(intcomma=lambda value: str(value)))

from blrec.core.raw_danmaku_dumper import RawDanmakuDumper, RawDanmakuDumpingState
from blrec.core.recorder import Recorder


class _FakeLive:
    room_id = 1


class _FakeStreamRecorder:
    def __init__(self, directory: str) -> None:
        self._directory = directory
        self._waiting_index = 0
        self._prelude_index = 0

    def make_waiting_raw_danmaku_path(self, timestamp=None):
        path = os.path.join(
            self._directory, f'waiting_{self._waiting_index}.waiting.jsonl'
        )
        self._waiting_index += 1
        return path, 0

    def make_prelude_raw_danmaku_path(self, timestamp=None):
        path = os.path.join(
            self._directory, f'prelude_{self._prelude_index}.prelude.jsonl'
        )
        self._prelude_index += 1
        return path, 0


class _FakeReceiver:
    def __init__(self) -> None:
        self.queue = asyncio.Queue()
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.started = True
        self.start_calls += 1

    def stop(self) -> None:
        self.started = False
        self.stop_calls += 1

    async def get_raw_danmaku(self):
        return await self.queue.get()

    async def put(self, danmu) -> None:
        await self.queue.put(danmu)


class _Listener:
    def __init__(self) -> None:
        self.created = []
        self.completed = []

    async def on_raw_danmaku_file_created(self, path: str) -> None:
        self.created.append(path)

    async def on_raw_danmaku_file_completed(self, path: str) -> None:
        self.completed.append(path)


class RawDanmakuDumperTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.receiver = _FakeReceiver()
        self.stream_recorder = _FakeStreamRecorder(self.tempdir.name)
        self.dumper = RawDanmakuDumper(
            _FakeLive(), self.stream_recorder, self.receiver
        )
        self.listener = _Listener()
        self.dumper.add_listener(self.listener)
        self.dumper.enable()

    async def asyncTearDown(self) -> None:
        await self.dumper.shutdown()
        self.tempdir.cleanup()

    async def test_waiting_file_is_written_and_closed(self) -> None:
        await self.dumper.start_waiting()
        await asyncio.sleep(0.05)
        self.assertEqual(self.dumper.state, RawDanmakuDumpingState.WAITING_DUMPING)

        await self.receiver.put({'cmd': 'DANMU_MSG', 'text': 'waiting'})
        await asyncio.sleep(0.05)

        waiting_path = self.listener.created[0]
        await self.dumper.stop_waiting()

        with open(waiting_path, 'rt', encoding='utf8') as file:
            lines = file.read().splitlines()

        self.assertEqual([json.loads(line)['text'] for line in lines], ['waiting'])
        self.assertEqual(self.listener.created, [waiting_path])
        self.assertEqual(self.listener.completed, [waiting_path])
        self.assertEqual(self.dumper.state, RawDanmakuDumpingState.IDLE)

    async def test_prelude_messages_are_flushed_into_live_file(self) -> None:
        video_path = os.path.join(self.tempdir.name, 'segment.flv')

        await self.dumper.start_live_prelude()
        await asyncio.sleep(0.05)
        await self.receiver.put({'cmd': 'DANMU_MSG', 'text': 'prelude'})
        await asyncio.sleep(0.05)

        await self.dumper.start_live_dumping(video_path)
        await asyncio.sleep(0.05)
        await self.receiver.put({'cmd': 'DANMU_MSG', 'text': 'live'})
        await asyncio.sleep(0.05)
        await self.dumper.complete_live_dumping()

        live_path = os.path.join(self.tempdir.name, 'segment.jsonl')
        with open(live_path, 'rt', encoding='utf8') as file:
            lines = file.read().splitlines()

        self.assertEqual(
            [json.loads(line)['text'] for line in lines], ['prelude', 'live']
        )
        self.assertEqual(self.listener.created, [live_path])
        self.assertEqual(self.listener.completed, [live_path])
        self.assertFalse(os.path.exists(os.path.join(self.tempdir.name, 'prelude_0.prelude.jsonl')))

    async def test_prelude_is_finalized_when_live_stops_before_first_segment(self) -> None:
        await self.dumper.start_live_prelude()
        await asyncio.sleep(0.05)
        await self.receiver.put({'cmd': 'DANMU_MSG', 'text': 'prelude-only'})
        await asyncio.sleep(0.05)

        await self.dumper.stop_live()

        prelude_path = os.path.join(self.tempdir.name, 'prelude_0.prelude.jsonl')
        with open(prelude_path, 'rt', encoding='utf8') as file:
            lines = file.read().splitlines()

        self.assertEqual(
            [json.loads(line)['text'] for line in lines], ['prelude-only']
        )
        self.assertEqual(self.listener.created, [prelude_path])
        self.assertEqual(self.listener.completed, [prelude_path])


class _RecorderDumper:
    def __init__(self) -> None:
        self.started_waiting = 0
        self.stopped_waiting = 0
        self.shutdown_calls = 0

    async def start_waiting(self) -> None:
        self.started_waiting += 1

    async def stop_waiting(self) -> None:
        self.stopped_waiting += 1

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class RecorderRawDanmakuStateTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_mode_starts_receiver_and_waiting_dump(self) -> None:
        recorder = object.__new__(Recorder)
        recorder._stopped = False
        recorder._recording = False
        recorder._save_raw_danmaku = True
        recorder._record_raw_danmaku_during_waiting = True
        recorder._raw_danmaku_coordinator_lock = asyncio.Lock()
        recorder._raw_danmaku_receiver = _FakeReceiver()
        recorder._raw_danmaku_dumper = _RecorderDumper()

        await Recorder._sync_raw_danmaku_state(recorder)

        self.assertTrue(recorder._raw_danmaku_receiver.started)
        self.assertEqual(recorder._raw_danmaku_receiver.start_calls, 1)
        self.assertEqual(recorder._raw_danmaku_dumper.started_waiting, 1)
        self.assertEqual(recorder._raw_danmaku_dumper.stopped_waiting, 0)

    async def test_force_stop_shuts_down_waiting_chain(self) -> None:
        recorder = object.__new__(Recorder)
        recorder._stopped = False
        recorder._recording = False
        recorder._save_raw_danmaku = True
        recorder._record_raw_danmaku_during_waiting = True
        recorder._raw_danmaku_coordinator_lock = asyncio.Lock()
        recorder._raw_danmaku_receiver = _FakeReceiver()
        recorder._raw_danmaku_dumper = _RecorderDumper()

        await Recorder._sync_raw_danmaku_state(recorder, force_stop=True)

        self.assertFalse(recorder._raw_danmaku_receiver.started)
        self.assertEqual(recorder._raw_danmaku_receiver.stop_calls, 1)
        self.assertEqual(recorder._raw_danmaku_dumper.shutdown_calls, 1)


if __name__ == '__main__':
    unittest.main()
