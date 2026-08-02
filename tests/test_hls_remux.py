from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from reactivex import just

# Recorder package initialization expects settings/application to be loaded first.
# isort: off
from blrec.setting import Settings  # noqa: F401
from blrec.application import Application  # noqa: F401

# isort: on
from blrec.postprocess import DeleteStrategy, Postprocessor
from blrec.postprocess.remux import RemuxingResult


class HLSRemuxFallbackTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video_path = os.path.join(self.tmp.name, 'record.m4s')
        self.playlist_path = os.path.join(self.tmp.name, 'record.m3u8')
        self.metadata_path = os.path.join(self.tmp.name, 'record.meta.json')
        Path(self.video_path).write_bytes(b'source')
        Path(self.playlist_path).write_text('#EXTM3U\n#EXT-X-ENDLIST', encoding='utf8')
        Path(self.metadata_path).write_text('{}', encoding='utf8')
        self.recorder = Mock()

    async def process(
        self,
        results: list[RemuxingResult],
        *,
        delete_source: DeleteStrategy = DeleteStrategy.NEVER,
    ) -> tuple[Postprocessor, list[dict]]:
        postprocessor = Postprocessor(
            SimpleNamespace(room_id=1),
            self.recorder,
            remux_to_mp4=True,
            delete_source=delete_source,
        )
        calls = []
        result_iter = iter(results)
        ffmpeg_metadata_path = os.path.join(self.tmp.name, 'record.m4s.meta')
        Path(ffmpeg_metadata_path).write_text('metadata', encoding='utf8')

        def remux(
            in_path: str,
            out_path: str,
            metadata_path: str,
            *,
            display_progress: bool,
            remove_filler_data: bool,
        ):
            calls.append(
                {
                    'in_path': in_path,
                    'out_path': out_path,
                    'remove_filler_data': remove_filler_data,
                }
            )
            Path(out_path).write_bytes(
                b'filtered' if remove_filler_data else b'fallback'
            )
            return just(next(result_iter))

        with (
            patch(
                'blrec.postprocess.postprocessor.make_metadata_file',
                AsyncMock(return_value=ffmpeg_metadata_path),
            ),
            patch('blrec.postprocess.postprocessor.remux_video', side_effect=remux),
        ):
            await postprocessor.start()
            await postprocessor.on_video_file_completed(self.recorder, self.video_path)
            await asyncio.wait_for(postprocessor.stop(), timeout=2)

        return postprocessor, calls

    async def test_failed_filtered_remux_retries_without_filter(self) -> None:
        postprocessor, calls = await self.process(
            [RemuxingResult(1, 'Conversion failed'), RemuxingResult(0, 'completed')]
        )

        self.assertEqual([call['remove_filler_data'] for call in calls], [True, False])
        self.assertTrue(all(call['out_path'].endswith('.part.mp4') for call in calls))
        self.assertEqual(Path(self.tmp.name, 'record.mp4').read_bytes(), b'fallback')
        self.assertFalse(Path(self.tmp.name, 'record.part.mp4').exists())
        self.assertIn(
            os.path.join(self.tmp.name, 'record.mp4'),
            list(postprocessor.get_completed_files()),
        )

    async def test_successful_filtered_remux_does_not_retry(self) -> None:
        _, calls = await self.process([RemuxingResult(0, 'completed')])

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]['remove_filler_data'])
        self.assertEqual(Path(self.tmp.name, 'record.mp4').read_bytes(), b'filtered')

    async def test_two_failed_attempts_keep_sources_and_remove_partial_output(
        self,
    ) -> None:
        postprocessor, calls = await self.process(
            [
                RemuxingResult(1, 'Conversion failed'),
                RemuxingResult(1, 'Conversion failed again'),
            ],
            delete_source=DeleteStrategy.AUTO,
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(Path(self.video_path).exists())
        self.assertTrue(Path(self.playlist_path).exists())
        self.assertFalse(Path(self.tmp.name, 'record.mp4').exists())
        self.assertFalse(Path(self.tmp.name, 'record.part.mp4').exists())
        completed_files = list(postprocessor.get_completed_files())
        self.assertIn(self.video_path, completed_files)
        self.assertNotIn(self.playlist_path, completed_files)

    async def test_delete_strategy_is_applied_after_atomic_success(self) -> None:
        for strategy, source_should_exist in (
            (DeleteStrategy.AUTO, False),
            (DeleteStrategy.SAFE, False),
            (DeleteStrategy.NEVER, True),
        ):
            with self.subTest(strategy=strategy):
                Path(self.video_path).write_bytes(b'source')
                Path(self.playlist_path).write_text(
                    '#EXTM3U\n#EXT-X-ENDLIST', encoding='utf8'
                )
                Path(self.metadata_path).write_text('{}', encoding='utf8')

                await self.process(
                    [RemuxingResult(0, 'completed')], delete_source=strategy
                )

                self.assertEqual(Path(self.video_path).exists(), source_should_exist)
                self.assertEqual(Path(self.playlist_path).exists(), source_should_exist)

    async def test_safe_strategy_keeps_sources_for_warned_remux(self) -> None:
        await self.process(
            [RemuxingResult(0, 'Non-monotonous DTS in output stream')],
            delete_source=DeleteStrategy.SAFE,
        )

        self.assertTrue(Path(self.video_path).exists())
        self.assertTrue(Path(self.playlist_path).exists())
        self.assertTrue(Path(self.tmp.name, 'record.mp4').exists())

    async def test_process_existing_files_deduplicates_and_restores_stopped_state(
        self,
    ) -> None:
        postprocessor = Postprocessor(SimpleNamespace(room_id=1), self.recorder)

        await postprocessor.process_existing_files([self.video_path, self.video_path])

        self.assertTrue(postprocessor.stopped)
        self.assertEqual(list(postprocessor.get_completed_files()), [self.video_path])
        self.recorder.add_listener.assert_called_once_with(postprocessor)
        self.recorder.remove_listener.assert_called_once_with(postprocessor)

    async def test_process_existing_files_keeps_running_postprocessor_started(
        self,
    ) -> None:
        postprocessor = Postprocessor(SimpleNamespace(room_id=1), self.recorder)
        await postprocessor.start()
        self.addAsyncCleanup(postprocessor.stop)

        await postprocessor.process_existing_files([self.video_path])

        self.assertFalse(postprocessor.stopped)
        self.recorder.remove_listener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
