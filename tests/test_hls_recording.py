from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import m3u8
from reactivex import Subject, from_iterable

# Recorder package initialization expects settings/application to be loaded first.
# isort: off
from blrec.setting import Settings  # noqa: F401
from blrec.application import Application  # noqa: F401

# isort: on
from blrec.hls.operators.analyser import Analyser
from blrec.hls.operators.playlist_dumper import PlaylistDumper
from blrec.hls.operators.segment_dumper import SegmentDumper
from blrec.hls.operators.segment_fetcher import (
    InitSectionData,
    SegmentData,
    SegmentFetcher,
)
from blrec.utils.hash import cksum


def make_segment(
    sequence: int = 1, *, init_uri: str = 'https://cdn.example/init.mp4'
) -> m3u8.Segment:
    playlist = m3u8.loads(
        '#EXTM3U\n'
        '#EXT-X-VERSION:7\n'
        f'#EXT-X-MAP:URI="{init_uri}"\n'
        f'#EXTINF:1.0,5|{cksum(b"media")}\n'
        f'{sequence}.m4s\n',
        uri='https://cdn.example/index.m3u8',
    )
    segment = playlist.segments[0]
    segment.custom_parser_values['playlist'] = playlist
    return segment


def video_profile(
    *, track_id: str = '0x1', extradata_hash: str = 'video', time_base: str = '1/90000'
) -> dict:
    return {
        'codec_type': 'video',
        'codec_name': 'h264',
        'codec_tag_string': 'avc1',
        'profile': 'High',
        'level': 42,
        'id': track_id,
        'time_base': time_base,
        'extradata_hash': extradata_hash,
        'width': 1920,
        'height': 1080,
        'coded_width': 1920,
        'coded_height': 1080,
    }


def audio_profile() -> dict:
    return {
        'codec_type': 'audio',
        'codec_name': 'aac',
        'codec_tag_string': 'mp4a',
        'profile': 'LC',
        'id': '0x2',
        'time_base': '1/48000',
        'extradata_hash': 'audio',
        'sample_rate': '48000',
        'channels': 2,
        'channel_layout': 'stereo',
    }


class HLSStreamProfileTestCase(unittest.TestCase):
    def test_video_first_profile_updates_video_dimensions(self) -> None:
        profiles = Subject()
        analyser = Analyser(
            Mock(duration=0), Mock(filesize=0), SimpleNamespace(profiles=profiles)
        )

        profiles.on_next({'streams': [video_profile(), audio_profile()]})

        metadata = analyser.make_metadata()
        self.assertEqual((metadata.width, metadata.height), (1920, 1080))

    def test_audio_first_profile_updates_video_dimensions(self) -> None:
        profiles = Subject()
        analyser = Analyser(
            Mock(duration=0), Mock(filesize=0), SimpleNamespace(profiles=profiles)
        )

        profiles.on_next({'streams': [audio_profile(), video_profile()]})

        metadata = analyser.make_metadata()
        self.assertEqual((metadata.width, metadata.height), (1920, 1080))

    def test_profile_without_video_keeps_zero_dimensions(self) -> None:
        profiles = Subject()
        analyser = Analyser(
            Mock(duration=0), Mock(filesize=0), SimpleNamespace(profiles=profiles)
        )

        profiles.on_next({'streams': [audio_profile()]})

        metadata = analyser.make_metadata()
        self.assertEqual((metadata.width, metadata.height), (0, 0))

    def test_video_only_profile_updates_video_dimensions(self) -> None:
        profiles = Subject()
        analyser = Analyser(
            Mock(duration=0), Mock(filesize=0), SimpleNamespace(profiles=profiles)
        )

        profiles.on_next({'streams': [video_profile()]})

        metadata = analyser.make_metadata()
        self.assertEqual((metadata.width, metadata.height), (1920, 1080))


class HLSSegmentDumperTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = iter(
            [
                (os.path.join(self.tmp.name, 'first.m4s'), 1),
                (os.path.join(self.tmp.name, 'second.m4s'), 2),
            ]
        )

    def dump(self, items: list, profiles: list[dict]) -> list:
        dumper = SegmentDumper(lambda: next(self.paths))
        output = []
        errors = []
        with patch('blrec.hls.operators.segment_dumper.ffprobe', side_effect=profiles):
            from_iterable(items).pipe(dumper).subscribe(output.append, errors.append)
        self.assertEqual(errors, [])
        return output

    def test_semantically_compatible_audio_first_init_stays_in_one_file(self) -> None:
        first = make_segment(1)
        second = make_segment(2, init_uri='https://cdn.example/init-2.mp4')
        profile = {'streams': [audio_profile(), video_profile()]}

        output = self.dump(
            [
                InitSectionData(first, b'init-a'),
                SegmentData(first, b'media-a'),
                InitSectionData(second, b'init-b'),
                SegmentData(second, b'media-b'),
            ],
            [profile, profile, profile],
        )

        self.assertEqual(len(output), 4)
        self.assertEqual(
            Path(self.tmp.name, 'first.m4s').read_bytes(), b'init-amedia-ainit-bmedia-b'
        )
        self.assertFalse(Path(self.tmp.name, 'second.m4s').exists())

    def test_incompatible_track_id_splits_the_file(self) -> None:
        first = make_segment(1)
        second = make_segment(2, init_uri='https://cdn.example/init-2.mp4')
        initial = {'streams': [audio_profile(), video_profile()]}
        changed = {'streams': [audio_profile(), video_profile(track_id='0x3')]}

        self.dump(
            [
                InitSectionData(first, b'init-a'),
                SegmentData(first, b'media-a'),
                InitSectionData(second, b'init-b'),
                SegmentData(second, b'media-b'),
            ],
            [initial, initial, changed],
        )

        self.assertEqual(
            Path(self.tmp.name, 'first.m4s').read_bytes(), b'init-amedia-a'
        )
        self.assertEqual(
            Path(self.tmp.name, 'second.m4s').read_bytes(), b'init-bmedia-b'
        )

    def test_incompatible_extradata_hash_splits_the_file(self) -> None:
        first = make_segment(1)
        second = make_segment(2, init_uri='https://cdn.example/init-2.mp4')
        initial = {'streams': [audio_profile(), video_profile()]}
        changed = {
            'streams': [audio_profile(), video_profile(extradata_hash='changed')]
        }

        self.dump(
            [
                InitSectionData(first, b'init-a'),
                SegmentData(first, b'media-a'),
                InitSectionData(second, b'init-b'),
                SegmentData(second, b'media-b'),
            ],
            [initial, initial, changed],
        )

        self.assertTrue(Path(self.tmp.name, 'second.m4s').exists())

    def test_incompatible_time_base_splits_the_file(self) -> None:
        first = make_segment(1)
        second = make_segment(2, init_uri='https://cdn.example/init-2.mp4')
        initial = {'streams': [audio_profile(), video_profile()]}
        changed = {'streams': [audio_profile(), video_profile(time_base='1/1000')]}

        self.dump(
            [
                InitSectionData(first, b'init-a'),
                SegmentData(first, b'media-a'),
                InitSectionData(second, b'init-b'),
                SegmentData(second, b'media-b'),
            ],
            [initial, initial, changed],
        )

        self.assertTrue(Path(self.tmp.name, 'second.m4s').exists())

    def test_changed_track_count_splits_the_file(self) -> None:
        first = make_segment(1)
        second = make_segment(2, init_uri='https://cdn.example/init-2.mp4')
        initial = {'streams': [audio_profile(), video_profile()]}
        changed = {'streams': [video_profile()]}

        self.dump(
            [
                InitSectionData(first, b'init-a'),
                SegmentData(first, b'media-a'),
                InitSectionData(second, b'init-b'),
                SegmentData(second, b'media-b'),
            ],
            [initial, initial, changed],
        )

        self.assertTrue(Path(self.tmp.name, 'second.m4s').exists())

    def test_compatible_init_updates_playlist_byte_range(self) -> None:
        first = make_segment(1)
        second = make_segment(2, init_uri='https://cdn.example/init-2.mp4')
        initial = {'streams': [video_profile(), audio_profile()]}
        reordered = {'streams': [audio_profile(), video_profile()]}
        segment_dumper = SegmentDumper(lambda: next(self.paths))
        playlist_dumper = PlaylistDumper(segment_dumper)
        errors = []

        with patch(
            'blrec.hls.operators.segment_dumper.ffprobe',
            side_effect=[initial, initial, reordered],
        ):
            from_iterable(
                [
                    InitSectionData(first, b'init-a'),
                    SegmentData(first, b'media-a'),
                    InitSectionData(second, b'init-b'),
                    SegmentData(second, b'media-b'),
                ]
            ).pipe(segment_dumper, playlist_dumper).subscribe(on_error=errors.append)

        self.assertEqual(errors, [])
        playlist_text = Path(self.tmp.name, 'first.m3u8').read_text(encoding='utf8')
        self.assertIn('BYTERANGE="6@0"', playlist_text)
        self.assertIn('BYTERANGE="6@13"', playlist_text)
        self.assertEqual(playlist_text.count('#EXT-X-MAP:'), 2)
        self.assertTrue(playlist_text.endswith('#EXT-X-ENDLIST'))

    def test_missing_fingerprint_field_splits_conservatively(self) -> None:
        first = make_segment(1)
        second = make_segment(2, init_uri='https://cdn.example/init-2.mp4')
        initial = {'streams': [video_profile(), audio_profile()]}
        incomplete_video = video_profile()
        del incomplete_video['extradata_hash']
        changed = {'streams': [incomplete_video, audio_profile()]}

        self.dump(
            [
                InitSectionData(first, b'init-a'),
                SegmentData(first, b'media-a'),
                InitSectionData(second, b'init-b'),
                SegmentData(second, b'media-b'),
            ],
            [initial, initial, changed],
        )

        self.assertTrue(Path(self.tmp.name, 'second.m4s').exists())

    def test_probe_failure_splits_conservatively_without_stopping(self) -> None:
        first = make_segment(1)
        second = make_segment(2, init_uri='https://cdn.example/init-2.mp4')
        profile = {'streams': [video_profile(), audio_profile()]}

        output = self.dump(
            [
                InitSectionData(first, b'init-a'),
                SegmentData(first, b'media-a'),
                InitSectionData(second, b'init-b'),
                SegmentData(second, b'media-b'),
            ],
            [profile, profile, ValueError('invalid init')],
        )

        self.assertEqual(len(output), 4)
        self.assertTrue(Path(self.tmp.name, 'second.m4s').exists())


class HLSInitFetchTestCase(unittest.TestCase):
    def fetch_one_segment(self, init_payloads: list[bytes]) -> tuple[list, int]:
        resolver = Mock()
        live = SimpleNamespace(room_id=1, headers={}, http_history=None)
        fetcher = SegmentFetcher(live, Mock(), resolver)
        responses = iter([*init_payloads, b'media'])
        output = []
        with (
            patch.object(
                fetcher, '_fetch_segment', side_effect=lambda _: next(responses)
            ),
            patch('blrec.hls.operators.segment_fetcher.time.sleep'),
        ):
            from_iterable([make_segment()]).pipe(fetcher).subscribe(output.append)
        return output, len(init_payloads)

    def test_init_accepts_first_matching_pair(self) -> None:
        output, init_calls = self.fetch_one_segment([b'A', b'A'])

        self.assertEqual(init_calls, 2)
        self.assertEqual([item.payload for item in output], [b'A', b'media'])

    def test_init_accepts_second_matching_pair(self) -> None:
        output, init_calls = self.fetch_one_segment([b'A', b'B', b'B'])

        self.assertEqual(init_calls, 3)
        self.assertEqual([item.payload for item in output], [b'B', b'media'])

    def test_unstable_init_is_bounded_and_rotates_routes(self) -> None:
        resolver = Mock()
        live = SimpleNamespace(room_id=1, headers={}, http_history=None)
        fetcher = SegmentFetcher(live, Mock(), resolver)
        segments = [make_segment(index) for index in range(1, 5)]
        init_calls = 0

        def fetch(url: str) -> bytes:
            nonlocal init_calls
            if url.endswith('init.mp4'):
                init_calls += 1
                if init_calls <= 12:
                    return (b'A', b'B', b'C')[(init_calls - 1) % 3]
                return b'stable'
            return b'media'

        output = []
        errors = []
        with (
            patch.object(fetcher, '_fetch_segment', side_effect=fetch),
            patch('blrec.hls.operators.segment_fetcher.time.sleep'),
        ):
            from_iterable(segments).pipe(fetcher).subscribe(
                output.append, errors.append
            )

        self.assertEqual(errors, [])
        self.assertEqual(init_calls, 14)
        self.assertEqual(len(output), 5)
        resolver.reset.assert_called_once_with()
        resolver.rotate_routes.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
