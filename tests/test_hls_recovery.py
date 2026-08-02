from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import m3u8

from blrec.hls.recovery import recover_incomplete_hls_recordings


class HLSRecoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_recording(
        self,
        name: str = 'record',
        *,
        directory: Path | None = None,
        media_size: int = 60,
        room_id: object = '1',
        endlist: bool = False,
    ) -> tuple[Path, Path]:
        directory = directory or self.root
        directory.mkdir(parents=True, exist_ok=True)
        video_path = directory / f'{name}.m4s'
        playlist_path = directory / f'{name}.m3u8'
        video_path.write_bytes(b'x' * media_size)
        (directory / f'{name}.meta.json').write_text(
            json.dumps({'description': {'RoomId': room_id}}), encoding='utf8'
        )
        playlist_path.write_text(
            '#EXTM3U\n'
            '#EXT-X-VERSION:7\n'
            f'#EXT-X-MAP:URI="{video_path.name}",BYTERANGE="10@0"\n'
            '#EXTINF:1,\n'
            '#EXT-X-BYTERANGE:20@10\n'
            f'{video_path.name}\n'
            '#EXTINF:1,\n'
            '#EXT-X-BYTERANGE:20@30\n'
            f'{video_path.name}\n' + ('#EXT-X-ENDLIST\n' if endlist else ''),
            encoding='utf8',
        )
        return video_path, playlist_path

    def test_finalizes_complete_unfinished_playlist(self) -> None:
        video_path, playlist_path = self.make_recording()

        recovered = recover_incomplete_hls_recordings([str(self.root)], {1})

        self.assertEqual(recovered, {1: [str(video_path)]})
        playlist = m3u8.load(str(playlist_path))
        self.assertTrue(playlist.is_endlist)
        self.assertEqual(len(playlist.segments), 2)
        self.assertEqual(video_path.read_bytes(), b'x' * 60)

    def test_removes_invalid_trailing_segments_without_truncating_media(self) -> None:
        video_path, playlist_path = self.make_recording(media_size=45)
        original_media = video_path.read_bytes()

        recovered = recover_incomplete_hls_recordings([str(self.root)], {1})

        self.assertEqual(recovered, {1: [str(video_path)]})
        playlist = m3u8.load(str(playlist_path))
        self.assertTrue(playlist.is_endlist)
        self.assertEqual(len(playlist.segments), 1)
        self.assertEqual(video_path.read_bytes(), original_media)

    def test_leaves_playlist_unchanged_when_no_media_segment_is_valid(self) -> None:
        _, playlist_path = self.make_recording(media_size=15)
        original_playlist = playlist_path.read_bytes()

        recovered = recover_incomplete_hls_recordings([str(self.root)], {1})

        self.assertEqual(recovered, {})
        self.assertEqual(playlist_path.read_bytes(), original_playlist)

    def test_skips_ineligible_candidates_without_changing_them(self) -> None:
        cases = (
            'complete',
            'missing_media',
            'missing_metadata',
            'corrupt_metadata',
            'wrong_room',
            'mp4',
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                video_path, playlist_path = self.make_recording(
                    case,
                    room_id='2' if case == 'wrong_room' else '1',
                    endlist=case == 'complete',
                )
                metadata_path = playlist_path.with_suffix('.meta.json')
                if case == 'missing_media':
                    video_path.unlink()
                elif case == 'missing_metadata':
                    metadata_path.unlink()
                elif case == 'corrupt_metadata':
                    metadata_path.write_text('{', encoding='utf8')
                elif case == 'mp4':
                    playlist_path.with_suffix('.mp4').write_bytes(b'final')
                original_playlist = playlist_path.read_bytes()

                recovered = recover_incomplete_hls_recordings(
                    [str(self.root)], {100 + index}
                )

                self.assertEqual(recovered, {})
                self.assertEqual(playlist_path.read_bytes(), original_playlist)

    def test_leaves_playlist_unchanged_when_init_range_is_invalid(self) -> None:
        _, playlist_path = self.make_recording()
        playlist_path.write_text(
            playlist_path.read_text(encoding='utf8').replace(
                'BYTERANGE="10@0"', 'BYTERANGE="100@0"'
            ),
            encoding='utf8',
        )
        original_playlist = playlist_path.read_bytes()

        recovered = recover_incomplete_hls_recordings([str(self.root)], {1})

        self.assertEqual(recovered, {})
        self.assertEqual(playlist_path.read_bytes(), original_playlist)

    def test_corrupt_candidate_does_not_block_another_recording(self) -> None:
        _, corrupt_playlist = self.make_recording('corrupt')
        corrupt_playlist.write_bytes(b'\xff')
        video_path, valid_playlist = self.make_recording('valid')

        recovered = recover_incomplete_hls_recordings([str(self.root)], {1})

        self.assertEqual(recovered, {1: [str(video_path)]})
        self.assertEqual(corrupt_playlist.read_bytes(), b'\xff')
        self.assertTrue(m3u8.load(str(valid_playlist)).is_endlist)

    def test_recovery_is_idempotent_and_deduplicates_overlapping_roots(self) -> None:
        nested = self.root / 'nested'
        video_path, playlist_path = self.make_recording(directory=nested)

        first = recover_incomplete_hls_recordings(
            [str(self.root), str(nested), str(self.root)], {1}
        )
        finalized = playlist_path.read_bytes()
        second = recover_incomplete_hls_recordings([str(self.root)], {1})

        self.assertEqual(first, {1: [str(video_path)]})
        self.assertEqual(second, {})
        self.assertEqual(playlist_path.read_bytes(), finalized)
        self.assertEqual(list(nested.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
