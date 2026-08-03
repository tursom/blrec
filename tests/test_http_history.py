import asyncio
import json
import os
import queue
import shutil
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import reactivex
import requests
from aiohttp import web
from fastapi import Depends, FastAPI

# Application imports the recorder graph, whose existing package initialization expects
# settings to have been loaded first, as it is in blrec.web.main.
# isort: off
from blrec.setting import HttpHistorySettings, Settings, SettingsIn
from blrec.application import Application

# isort: on
from blrec.bili.api import BaseApi, WebApi
from blrec.bili.exceptions import ApiRequestError
from blrec.bili.live import Live
from blrec.core.cover_downloader import CoverDownloader
from blrec.core.operators.request_exception_handler import RequestExceptionHandler
from blrec.core.operators.stream_fetcher import StreamFetcher
from blrec.hls.operators.playlist_fetcher import PlaylistFetcher
from blrec.hls.operators.segment_fetcher import SegmentFetcher
from blrec.http_history import (
    HttpHistoryStore,
    NoHistoryRecords,
    record_http_exchange,
    sanitize_record,
)
from blrec.web import security
from blrec.web.routers import http_history as http_history_router


class HttpHistorySanitizerTestCase(unittest.TestCase):
    def test_credentials_are_removed_before_serialization(self) -> None:
        record = sanitize_record(
            {
                'record_type': 'http_exchange',
                'request': {
                    'url': (
                        'https://api.live.bilibili.com/x?room_id=1&w_rid=secret-sign'
                    ),
                    'headers': {
                        'Cookie': 'SESSDATA=secret-cookie; bili_jct=secret-csrf;',
                        'Authorization': 'Bearer secret-token',
                        'User-Agent': 'blrec-test',
                    },
                },
                'response': {
                    'headers': {'Set-Cookie': 'SESSDATA=response-secret'},
                    'body': {
                        'url': 'https://cdn.example/live.flv?token=stream-secret',
                        'code': -352,
                    },
                },
                'error': 'request failed for SESSDATA=error-secret',
                'diagnostic': {'access_token': 'nested-top-level-secret'},
            }
        )

        serialized = json.dumps(record, ensure_ascii=False)
        for secret in (
            'secret-sign',
            'secret-cookie',
            'secret-csrf',
            'secret-token',
            'response-secret',
            'stream-secret',
            'error-secret',
            'nested-top-level-secret',
        ):
            self.assertNotIn(secret, serialized)

        self.assertEqual(record['request']['headers']['User-Agent'], 'blrec-test')
        self.assertEqual(record['request']['cookie_names'], ['SESSDATA', 'bili_jct'])
        self.assertEqual(record['response']['body']['code'], -352)
        self.assertIn('room_id=1', record['request']['url'])

    def test_credentials_in_error_text_and_url_userinfo_are_removed(self) -> None:
        record = sanitize_record(
            {
                'request': {
                    'url': 'https://private-user:private-pass@example.test/path'
                },
                'error': (
                    'Authorization: Bearer auth-secret; '
                    'X-API-Key=api-key-secret; Cookie: DedeUserID=cookie-secret'
                ),
            }
        )

        serialized = json.dumps(record)
        for secret in (
            'private-user',
            'private-pass',
            'auth-secret',
            'api-key-secret',
            'cookie-secret',
        ):
            self.assertNotIn(secret, serialized)

    def test_nav_response_does_not_export_login_identity(self) -> None:
        record = sanitize_record(
            {
                'record_type': 'http_exchange',
                'request': {'url': 'https://api.bilibili.com/x/web-interface/nav'},
                'response': {
                    'body': {
                        'code': 0,
                        'message': '0',
                        'data': {
                            'mid': 123,
                            'uname': 'private-account',
                            'wbi_img': {
                                'img_url': 'https://i0.hdslb.com/bfs/wbi/img-key.png',
                                'sub_url': 'https://i0.hdslb.com/bfs/wbi/sub-key.png',
                            },
                        },
                    }
                },
            }
        )

        serialized = json.dumps(record, ensure_ascii=False)
        self.assertNotIn('private-account', serialized)
        self.assertNotIn('"mid"', serialized)
        self.assertIn('wbi_img', serialized)

    def test_nested_cookie_fields_and_relative_playback_urls_are_redacted(self) -> None:
        record = sanitize_record(
            {
                'response': {
                    'body': {
                        'SESSDATA': 'sess-secret',
                        'bili_jct': 'jct-secret',
                        'buvid3': 'buvid-secret',
                        'segment': '/live/one.m4s?wsSecret=play-secret&range=0-99',
                    }
                }
            }
        )

        serialized = json.dumps(record)
        for secret in ('sess-secret', 'jct-secret', 'buvid-secret', 'play-secret'):
            self.assertNotIn(secret, serialized)
        self.assertIn('range=0-99', serialized)

    def test_multiline_playlist_relative_urls_are_redacted(self) -> None:
        record = sanitize_record(
            {
                'response': {
                    'body': (
                        '#EXTM3U\n'
                        '#EXTINF:1.0,\n'
                        'segment.m4s?wsSecret=playlist-secret&wsTime=123\n'
                    )
                }
            }
        )

        serialized = json.dumps(record)
        self.assertNotIn('playlist-secret', serialized)
        self.assertNotIn('wsTime=123', serialized)
        self.assertIn('%5BREDACTED%5D', serialized)


class HttpHistoryStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = HttpHistoryStore(
            self.tempdir.name,
            enabled=True,
            retention_days=7,
            max_size=10 * 1024 * 1024,
            segment_size=1024,
        )
        self.store.start()

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_export_filters_records_and_contains_safe_manifest(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record(
            {
                'record_type': 'http_exchange',
                'recorded_at': (now - timedelta(hours=2)).isoformat(),
                'room_id': 1,
                'request': {'url': 'https://api.bilibili.com/x?token=secret-token'},
                'response': {'status': 200, 'body': {'code': 0}},
            }
        )
        self.store.record(
            {
                'record_type': 'http_exchange',
                'recorded_at': now.isoformat(),
                'room_id': 2,
                'request': {'url': 'https://api.bilibili.com/y'},
                'response': {'status': 500, 'body': {'code': -352}},
            }
        )

        result = self.store.export(
            room_id=2,
            since=now - timedelta(minutes=5),
            until=now + timedelta(minutes=5),
        )
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))

        with zipfile.ZipFile(result.path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            lines = archive.read('records.jsonl').decode().splitlines()

        self.assertEqual(result.record_count, 1)
        self.assertEqual(manifest['schema_version'], 2)
        self.assertEqual(manifest['filters']['room_id'], 2)
        self.assertNotIn('cwd', manifest['application'])
        self.assertEqual(json.loads(lines[0])['room_id'], 2)

    def test_disabled_store_keeps_existing_records_available(self) -> None:
        self.store.record(
            {
                'record_type': 'http_exchange',
                'room_id': 1,
                'request': {'url': 'https://api.bilibili.com/one'},
            }
        )
        self.store.configure(enabled=False, retention_days=7, max_size=10 * 1024 * 1024)
        self.store.record(
            {
                'record_type': 'http_exchange',
                'room_id': 2,
                'request': {'url': 'https://api.bilibili.com/two'},
            }
        )

        status = self.store.status()
        self.assertFalse(status.enabled)
        self.assertEqual(status.record_count, 1)
        result = self.store.export()
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        self.assertEqual(result.record_count, 1)

        self.store.clear()
        self.assertEqual(self.store.status().record_count, 0)
        with self.assertRaises(NoHistoryRecords):
            self.store.export()

    def test_rotated_segments_stay_within_the_configured_limit(self) -> None:
        self.store.configure(enabled=True, retention_days=7, max_size=2500)
        for index in range(30):
            self.store.record(
                {
                    'record_type': 'http_exchange',
                    'room_id': index,
                    'request': {'url': f'https://api.bilibili.com/{index}'},
                    'response': {'body': {'padding': 'x' * 200}},
                }
            )
        self.store.flush()

        status = self.store.status()
        self.assertLessEqual(status.total_size, 2500)
        self.assertLess(status.record_count, 30)

    def test_retention_cleanup_removes_old_closed_segments(self) -> None:
        self.store.record(
            {'record_type': 'http_exchange', 'request': {'url': 'https://old.test'}}
        )
        snapshot = self.store._command('snapshot')
        self.addCleanup(shutil.rmtree, snapshot.directory, True)
        paths = snapshot.paths
        old = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        os.utime(paths[0], (old, old))

        self.store.configure(enabled=True, retention_days=7, max_size=10 * 1024 * 1024)

        self.assertEqual(self.store.status().record_count, 0)

    def test_queue_overflow_and_write_failure_are_counted_without_stopping(
        self,
    ) -> None:
        self.store.close()
        self.store = HttpHistoryStore(self.tempdir.name, max_size=10 * 1024 * 1024)
        self.store._queue = queue.Queue(maxsize=1)
        original_write = self.store._write
        write_started = threading.Event()
        release_write = threading.Event()

        def blocked_write(record: dict) -> None:
            write_started.set()
            release_write.wait(timeout=2)
            original_write(record)

        with patch.object(self.store, '_write', side_effect=blocked_write):
            self.store.start()
            self.store.record({'request': {'url': 'https://example.test/one'}})
            self.assertTrue(write_started.wait(timeout=2))
            self.store.record({'request': {'url': 'https://example.test/two'}})
            self.store.record({'request': {'url': 'https://example.test/dropped'}})
            release_write.set()
            self.store.flush()

        with patch.object(self.store, '_write', side_effect=OSError('disk full')):
            self.store.record({'request': {'url': 'https://example.test/write-error'}})
            self.store.flush()
        self.store.record({'request': {'url': 'https://example.test/recovered'}})
        self.store.flush()

        status = self.store.status()
        self.assertEqual(status.dropped_records, 1)
        self.assertIn('disk full', status.last_error or '')
        result = self.store.export()
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            records = archive.read('records.jsonl')
        self.assertIn(b'recovered', records)
        self.assertIn(b'dropped', records)

    def test_http_exchange_records_body_hash_and_truncation(self) -> None:
        record_http_exchange(
            self.store,
            room_id=7,
            category='api',
            method='GET',
            url='https://api.bilibili.com/large',
            request_headers={'Cookie': 'SESSDATA=secret'},
            response_status=200,
            response_headers={'Content-Type': 'application/json'},
            response_body={'access_token': 'large-json-secret', 'value': 'x' * 100},
            max_body_size=30,
        )
        result = self.store.export(room_id=7)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))

        with zipfile.ZipFile(result.path) as archive:
            record = json.loads(archive.read('records.jsonl').decode())
        self.assertTrue(record['response']['body_truncated'])
        self.assertEqual(len(record['response']['body_sha256']), 64)
        self.assertNotIn('secret', json.dumps(record))
        self.assertNotIn(b'large-json-secret', Path(result.path).read_bytes())

    def test_sealed_snapshot_survives_cleanup_of_source_segments(self) -> None:
        self.store.record(
            {'record_type': 'http_exchange', 'request': {'url': 'https://one.test'}}
        )
        snapshot = self.store._command('snapshot')
        self.addCleanup(shutil.rmtree, snapshot.directory, True)

        for path in Path(self.tempdir.name).glob('http-history-*.jsonl'):
            path.unlink()

        self.assertIn(b'one.test', snapshot.paths[0].read_bytes())

    def test_binary_body_is_json_serializable(self) -> None:
        record_http_exchange(
            self.store,
            room_id=7,
            category='api',
            method='GET',
            url='https://api.bilibili.com/binary',
            response_status=502,
            response_body=b'not-json',
        )

        result = self.store.export(room_id=7)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            record = json.loads(archive.read('records.jsonl').decode())
        self.assertEqual(record['response']['body'], 'not-json')

    def test_json_text_is_sanitized_before_it_is_truncated(self) -> None:
        record_http_exchange(
            self.store,
            room_id=7,
            category='api',
            method='GET',
            url='https://api.bilibili.com/error',
            response_body='{"access_token":"raw-json-secret","padding":"xxxxxxxx"}',
            max_body_size=24,
        )

        result = self.store.export(room_id=7)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        self.assertNotIn(b'raw-json-secret', Path(result.path).read_bytes())

    def test_concurrent_writes_and_restart_recover_valid_records(self) -> None:
        threads = []
        for worker in range(4):
            thread = threading.Thread(
                target=lambda offset=worker: [
                    self.store.record(
                        {
                            'record_type': 'http_exchange',
                            'room_id': offset,
                            'request': {
                                'url': f'https://example.test/{offset}/{index}'
                            },
                        }
                    )
                    for index in range(25)
                ]
            )
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        self.store.close()

        segment = sorted(Path(self.tempdir.name).glob('http-history-*.jsonl'))[-1]
        with open(segment, 'ab') as target:
            target.write(b'{partial-tail')
        self.store = HttpHistoryStore(self.tempdir.name, max_size=10 * 1024 * 1024)
        self.store.start()

        self.assertEqual(self.store.status().record_count, 100)

    def test_v1_jsonl_remains_readable_by_status_and_general_export(self) -> None:
        self.store.close()
        record = {
            'schema_version': 1,
            'recorded_at': '2026-08-01T00:00:00+00:00',
            'room_id': 41,
            'request': {'url': 'https://api.bilibili.com/v1'},
        }
        path = Path(self.tempdir.name) / 'http-history-v1.jsonl'
        path.write_text(json.dumps(record) + '\n', encoding='utf8')
        self.store = HttpHistoryStore(self.tempdir.name, max_size=10 * 1024 * 1024)
        self.store.start()

        result = self.store.export(room_id=41)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            exported = json.loads(archive.read('records.jsonl'))

        self.assertEqual(self.store.status().record_count, 1)
        self.assertEqual(exported['schema_version'], 1)
        self.assertNotIn('payload', exported.get('response', {}))

    def test_directory_switch_drains_old_store_and_uses_new_directory(self) -> None:
        second_directory = tempfile.TemporaryDirectory()
        self.addCleanup(second_directory.cleanup)
        self.store.record(
            {'record_type': 'http_exchange', 'request': {'url': 'https://old.test'}}
        )

        self.store.set_directory(second_directory.name)
        self.store.record(
            {'record_type': 'http_exchange', 'request': {'url': 'https://new.test'}}
        )
        self.store.flush()

        old_bytes = b''.join(
            path.read_bytes()
            for path in Path(self.tempdir.name).glob('http-history-*.jsonl')
        )
        new_bytes = b''.join(
            path.read_bytes()
            for path in Path(second_directory.name).glob('http-history-*.jsonl')
        )
        self.assertIn(b'old.test', old_bytes)
        self.assertNotIn(b'new.test', old_bytes)
        self.assertIn(b'new.test', new_bytes)

    def test_startup_directory_failure_does_not_raise(self) -> None:
        self.store.close()
        with patch.object(Path, 'mkdir', side_effect=OSError('read only')):
            self.store = HttpHistoryStore(self.tempdir.name)
            self.store.start()

        self.assertIsNotNone(self.store.status().last_error)

    def test_failed_file_flush_still_releases_the_current_segment(self) -> None:
        file = Mock()
        file.flush.side_effect = OSError('flush failed')
        self.store._file = file
        self.store._path = Path(self.tempdir.name) / 'current.jsonl'

        with self.assertRaises(OSError):
            self.store._close_file()

        self.assertIsNone(self.store._file)
        self.assertIsNone(self.store._path)
        file.close.assert_called_once()

    def test_export_sanitizes_untrusted_existing_record_again(self) -> None:
        self.store.flush()
        segment = sorted(Path(self.tempdir.name).glob('http-history-*.jsonl'))
        if not segment:
            self.store.record(
                {'record_type': 'http_exchange', 'request': {'url': 'https://safe'}}
            )
            self.store.flush()
            segment = sorted(Path(self.tempdir.name).glob('http-history-*.jsonl'))
        with open(segment[0], 'at', encoding='utf8') as target:
            target.write(
                json.dumps(
                    {
                        'recorded_at': datetime.now(timezone.utc).isoformat(),
                        'request': {'url': 'https://example.test/?token=export-secret'},
                    }
                )
                + '\n'
            )

        result = self.store.export()
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        self.assertNotIn(b'export-secret', Path(result.path).read_bytes())
        with zipfile.ZipFile(result.path) as archive:
            self.assertNotIn(b'export-secret', archive.read('records.jsonl'))

    def test_export_failure_is_reported_in_status(self) -> None:
        self.store.record(
            {'record_type': 'http_exchange', 'request': {'url': 'https://safe.test'}}
        )

        with patch(
            'blrec.http_history.store.tempfile.mkstemp',
            side_effect=OSError('export directory unavailable'),
        ):
            with self.assertRaises(OSError):
                self.store.export()

        self.assertIn(
            'export directory unavailable', self.store.status().last_error or ''
        )

    def test_hls_playlist_body_is_only_saved_when_it_changes(self) -> None:
        live = SimpleNamespace(room_id=31, headers={}, http_history=self.store)
        response = Mock(
            url='https://cdn.example/live.m3u8?token=playlist-secret',
            status_code=200,
            headers={'Content-Type': 'application/vnd.apple.mpegurl'},
            text='#EXTM3U\n#EXT-X-VERSION:7\n',
        )
        response.request.headers = {}
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        fetcher = PlaylistFetcher(live, session)

        fetcher._fetch_playlist(response.url)
        fetcher._fetch_playlist(response.url)

        result = self.store.export(room_id=31)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read('records.jsonl').decode().splitlines()
            ]
        self.assertIn('body', records[0]['response'])
        self.assertNotIn('body', records[1]['response'])
        self.assertTrue(records[1]['body_unchanged'])
        self.assertNotIn('playlist-secret', json.dumps(records))

    def test_hls_playlist_failure_saves_a_sanitized_response_body(self) -> None:
        live = SimpleNamespace(room_id=34, headers={}, http_history=self.store)
        response = Mock(
            status_code=503,
            headers={'Content-Type': 'text/plain'},
            text='failed url https://cdn.example/live.m3u8?token=failure-secret',
        )
        error = requests.HTTPError('playlist failed', response=response)
        session = Mock()
        session.get.side_effect = error
        fetcher = PlaylistFetcher(live, session)

        with self.assertRaises(requests.HTTPError):
            fetcher._fetch_playlist('https://cdn.example/live.m3u8')

        result = self.store.export(room_id=34)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            record = json.loads(archive.read('records.jsonl').decode())
        self.assertEqual(record['response']['status'], 503)
        self.assertIn('[REDACTED]', record['response']['body'])
        self.assertNotIn('failure-secret', json.dumps(record))

    def test_hls_playlist_network_retries_share_an_operation(self) -> None:
        live = SimpleNamespace(room_id=35, headers={}, http_history=self.store)
        response = Mock(
            url='https://cdn.example/live.m3u8',
            status_code=200,
            headers={'Content-Type': 'application/vnd.apple.mpegurl'},
            text='#EXTM3U\n',
        )
        response.request.headers = {}
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.side_effect = [requests.Timeout('temporary timeout'), response]
        fetcher = PlaylistFetcher(live, session)

        self.assertEqual(fetcher._fetch_playlist(response.url), '#EXTM3U\n')

        result = self.store.export(room_id=35)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read('records.jsonl').decode().splitlines()
            ]
        self.assertEqual([record['retry']['attempt'] for record in records], [1, 2])
        self.assertEqual(len({record['operation_id'] for record in records}), 1)

    def test_hls_segments_are_aggregated_and_validation_errors_have_no_body(
        self,
    ) -> None:
        live = SimpleNamespace(room_id=32, headers={}, http_history=self.store)
        response = Mock(
            url='https://segments.example/one.m4s?token=segment-secret',
            status_code=200,
            headers={'Content-Length': '5'},
            content=b'media',
        )
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        fetcher = SegmentFetcher(live, session, Mock())

        fetcher._fetch_segment(response.url)
        operation_id = fetcher._last_segment_operation_id
        fetcher._record_segment_validation_error(
            response.url, 'crc32', 'a', 'b', operation_id
        )
        fetcher._record_segment_success(
            response.url, len(response.content), operation_id
        )
        fetcher._flush_segment_summaries()

        result = self.store.export(room_id=32)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read('records.jsonl').decode().splitlines()
            ]
        summary = next(
            record
            for record in records
            if record['record_type'] == 'hls_segment_summary'
        )
        validation_error = next(
            record
            for record in records
            if record['record_type'] == 'hls_segment_validation_error'
        )
        self.assertEqual(summary['request_count'], 1)
        self.assertEqual(summary['response_bytes'], 5)
        self.assertEqual(summary['operation_ids'], [operation_id])
        self.assertEqual(validation_error['operation_id'], operation_id)
        self.assertEqual(validation_error['validation']['type'], 'crc32')
        self.assertNotIn('body', json.dumps(records))
        self.assertNotIn('segment-secret', json.dumps(records))

    def test_flv_handshake_records_headers_without_media_body(self) -> None:
        live = SimpleNamespace(room_id=33, headers={}, http_history=self.store)
        response = Mock(
            url='https://stream.example/live.flv?token=stream-secret',
            status_code=200,
            headers={'Content-Type': 'video/x-flv'},
            raw=Mock(),
        )
        response.request.headers = {}
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response

        async def fetch() -> object:
            return StreamFetcher(live, session)(reactivex.just(response.url)).run()

        raw = asyncio.run(fetch())

        self.assertIs(raw, response.raw)
        connection_id = live.http_history_connection_id
        resolver = SimpleNamespace(live=live, stream_url=response.url)
        handler = RequestExceptionHandler(resolver)
        handler._last_retry_time = 0
        with self.assertRaises(requests.ConnectionError):
            handler._handle(
                reactivex.throw(requests.ConnectionError('stream read failed'))
            ).run()
        result = self.store.export(room_id=33)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read('records.jsonl').decode().splitlines()
            ]
        handshake, read_error = records
        self.assertEqual(handshake['category'], 'flv_handshake')
        self.assertEqual(handshake['response']['status'], 200)
        self.assertNotIn('body', handshake['response'])
        self.assertEqual(handshake['operation_id'], connection_id)
        self.assertEqual(read_error['parent_operation_id'], connection_id)
        self.assertNotIn('stream-secret', json.dumps(records))


class HttpHistorySettingsTestCase(unittest.TestCase):
    def test_existing_settings_gain_bounded_enabled_history_defaults(self) -> None:
        settings = Settings.parse_obj({})

        self.assertTrue(settings.http_history.enabled)
        self.assertEqual(settings.http_history.retention_days, 7)
        self.assertEqual(settings.http_history.max_size, 500 * 1024 * 1024)
        self.assertEqual(
            SettingsIn(httpHistory=settings.http_history).dict(by_alias=True)[
                'httpHistory'
            ]['retentionDays'],
            7,
        )

    def test_history_limits_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            HttpHistorySettings(retentionDays=0)
        with self.assertRaises(ValueError):
            HttpHistorySettings(maxSize=1024)


class HttpHistoryApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        settings = Settings.parse_obj(
            {'logging': {'logDir': self.tempdir.name}, 'httpHistory': {'enabled': True}}
        )
        self.application = Application(settings)
        self.application._http_history.start()
        http_history_router.app = self.application

    async def asyncTearDown(self) -> None:
        self.application._http_history.close()
        self.tempdir.cleanup()

    async def test_user_can_download_and_clear_filtered_history(self) -> None:
        self.application._http_history.record(
            {
                'record_type': 'http_exchange',
                'room_id': 123,
                'request': {'url': 'https://api.bilibili.com/x'},
            }
        )

        history_status = await http_history_router.get_http_history_status()
        self.assertEqual(history_status.room_ids, [123])

        export_response = await http_history_router.export_http_history(
            room_id=123, since=None, until=None
        )
        self.addCleanup(
            lambda: os.path.exists(export_response.path)
            and os.remove(export_response.path)
        )
        self.assertIn('blrec-http-history-', export_response.filename)
        with zipfile.ZipFile(export_response.path) as archive:
            record = json.loads(archive.read('records.jsonl').decode())
        self.assertEqual(record['room_id'], 123)

        clear_response = await http_history_router.clear_http_history()
        self.assertEqual(clear_response.code, 0)
        self.assertEqual(
            (await http_history_router.get_http_history_status()).record_count, 0
        )

    async def test_export_rejects_non_utc_or_reversed_ranges(self) -> None:
        with self.assertRaises(Exception):
            await http_history_router.export_http_history(
                room_id=None, since=datetime(2026, 8, 1), until=datetime(2026, 8, 2)
            )
        with self.assertRaises(Exception):
            await http_history_router.export_http_history(
                room_id=None,
                since=datetime(2026, 8, 2, tzinfo=timezone.utc),
                until=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )

    async def test_user_can_list_filter_and_download_incidents(self) -> None:
        self.application._http_history.record(
            {
                'record_type': 'http_exchange',
                'room_id': 123,
                'recorded_at': '2026-08-02T00:00:00+00:00',
                'request': {'url': 'https://cdn.example/init.mp4'},
                'response': {'status': 200},
            },
            payload=b'init',
            payload_role='hls_init',
        )
        incident_id = self.application._http_history.mark_incident(
            room_id=123,
            kind='hls_init_unstable',
            occurred_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )

        incidents = await http_history_router.list_http_incidents(
            room_id=123,
            since=datetime(2026, 8, 1, tzinfo=timezone.utc),
            until=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].incident_id, incident_id)
        self.assertEqual(incidents[0].kind, 'hls_init_unstable')
        self.assertEqual(incidents[0].payload_size, 4)

        history_status = await http_history_router.get_http_history_status()
        self.assertEqual(history_status.incident_count, 1)
        self.assertEqual(history_status.active_incident_count, 0)
        self.assertEqual(history_status.payload_size, 4)

        response = await http_history_router.export_http_incident(incident_id)
        self.addCleanup(
            lambda: os.path.exists(response.path) and os.remove(response.path)
        )
        with zipfile.ZipFile(response.path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
        self.assertEqual(manifest['incident']['incident_id'], incident_id)

    async def test_incident_routes_reject_invalid_ranges_and_unknown_ids(self) -> None:
        with self.assertRaises(Exception):
            await http_history_router.list_http_incidents(
                room_id=None, since=datetime(2026, 8, 1), until=datetime(2026, 8, 2)
            )
        with self.assertRaises(Exception):
            await http_history_router.export_http_incident('../manifest.json')
        with self.assertRaises(Exception):
            await http_history_router.export_http_incident('0' * 32)

    async def test_cookie_validation_is_exposed_by_the_application_facade(self) -> None:
        response = {'code': 0, 'message': 'ok', 'data': {}}
        with patch(
            'blrec.application.get_nav', new=AsyncMock(return_value=response)
        ) as nav:
            self.assertEqual(
                await self.application.validate_cookie('SESSDATA=x'), response
            )

        nav.assert_awaited_once_with('SESSDATA=x', self.application._http_history)

    async def test_http_history_routes_honor_the_global_api_key(self) -> None:
        previous_key = security.api_key
        security.api_key = 'history-api-key'
        security.whitelist.clear()
        security.blacklist.clear()
        security.attempting_clients.clear()
        self.addCleanup(setattr, security, 'api_key', previous_key)
        self.addCleanup(security.whitelist.clear)
        self.addCleanup(security.blacklist.clear)
        self.addCleanup(security.attempting_clients.clear)
        protected_api = FastAPI(dependencies=[Depends(security.authenticate)])
        protected_api.include_router(http_history_router.router)

        async def request_status(headers: list[tuple[bytes, bytes]]) -> int:
            messages = []

            async def receive() -> dict:
                return {'type': 'http.request', 'body': b'', 'more_body': False}

            async def send(message: dict) -> None:
                messages.append(message)

            await protected_api(
                {
                    'type': 'http',
                    'asgi': {'version': '3.0'},
                    'http_version': '1.1',
                    'method': 'GET',
                    'scheme': 'http',
                    'path': '/api/v1/http-history/status',
                    'raw_path': b'/api/v1/http-history/status',
                    'query_string': b'',
                    'headers': headers,
                    'client': ('127.0.0.1', 12345),
                    'server': ('testserver', 80),
                    'root_path': '',
                },
                receive,
                send,
            )
            return next(
                message['status']
                for message in messages
                if message['type'] == 'http.response.start'
            )

        self.assertEqual(await request_status([]), 401)
        self.assertEqual(
            await request_status([(b'x-api-key', b'history-api-key')]), 200
        )


class HttpHistoryCaptureTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = HttpHistoryStore(self.tempdir.name)
        self.store.start()

        async def play_info(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    'code': 0,
                    'message': 'ok',
                    'data': {'playurl_info': {'playurl': {'stream': []}}},
                }
            )

        async def risk_control(request: web.Request) -> web.Response:
            return web.json_response({'code': -352, 'message': 'risk control'})

        async def non_json(request: web.Request) -> web.Response:
            return web.Response(
                text='upstream html response', content_type='text/plain'
            )

        async def slow(request: web.Request) -> web.Response:
            await asyncio.sleep(0.05)
            return web.json_response({'code': 0, 'data': {}})

        async def broken_html(request: web.Request) -> web.Response:
            return web.Response(text='<html>missing init data</html>')

        async def unavailable(request: web.Request) -> web.Response:
            return web.json_response(
                {'code': 503, 'message': 'upstream unavailable'},
                status=503,
                headers={'X-Upstream': 'test'},
            )

        async def valid_html(request: web.Request) -> web.Response:
            return web.Response(
                text=(
                    '<script>window.__NEPTUNE_IS_MY_WAIFU__ = '
                    '{"roomInitRes":{"code":0,"data":{"live_status":1}},'
                    '"roomInfoRes":{"code":0,"data":{}}}</script>'
                )
            )

        application = web.Application()
        application.router.add_get(
            '/xlive/web-room/v2/index/getRoomPlayInfo', play_info
        )
        application.router.add_get('/risk', risk_control)
        application.router.add_get('/non-json', non_json)
        application.router.add_get('/slow', slow)
        application.router.add_get('/broken-html', broken_html)
        application.router.add_get('/unavailable', unavailable)
        application.router.add_get('/valid-html', valid_html)
        self.cover_attempts = 0

        async def retrying_cover(request: web.Request) -> web.Response:
            self.cover_attempts += 1
            if self.cover_attempts < 3:
                return web.Response(status=503, text='try again')
            return web.Response(body=b'cover-data', content_type='image/jpeg')

        application.router.add_get('/cover.jpg', retrying_cover)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        sockets = self.site._server.sockets
        self.base_url = f'http://127.0.0.1:{sockets[0].getsockname()[1]}'

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()
        self.store.close()
        self.tempdir.cleanup()

    async def test_wbi_api_exchange_is_persisted_with_redacted_signature(self) -> None:
        async with aiohttp.ClientSession() as session:
            api = WebApi(session, room_id=99, http_history=self.store)
            api.base_play_info_api_urls = [self.base_url]

            result = await api.get_room_play_infos(99)

        self.assertEqual(len(result), 1)
        export = self.store.export(room_id=99)
        self.addCleanup(lambda: os.path.exists(export.path) and os.remove(export.path))
        with zipfile.ZipFile(export.path) as archive:
            record = json.loads(archive.read('records.jsonl').decode())
        self.assertEqual(record['response']['body']['code'], 0)
        self.assertIn('w_rid=%5BREDACTED%5D', record['request']['url'])
        self.assertEqual(record['operation_id'] is not None, True)
        self.assertEqual(record['retry'], {'attempt': 1, 'is_retry': False})

    async def test_exchange_without_caller_operation_id_gets_attempt_identity(
        self,
    ) -> None:
        record_http_exchange(
            self.store,
            room_id=98,
            category='connectivity',
            method='HEAD',
            url=self.base_url,
            response_status=200,
        )

        export = self.store.export(room_id=98)
        self.addCleanup(lambda: os.path.exists(export.path) and os.remove(export.path))
        with zipfile.ZipFile(export.path) as archive:
            record = json.loads(archive.read('records.jsonl'))

        self.assertIsInstance(record['operation_id'], str)
        self.assertTrue(record['operation_id'])
        self.assertEqual(record['retry'], {'attempt': 1, 'is_retry': False})

    async def test_cover_retries_share_operation_id_and_increment_attempts(
        self,
    ) -> None:
        live = SimpleNamespace(
            room_id=97,
            http_history=self.store,
            headers={'User-Agent': 'blrec-test'},
            room_info=SimpleNamespace(cover=self.base_url + '/cover.jpg'),
            update_room_info=AsyncMock(return_value=True),
        )
        downloader = CoverDownloader(live, Mock(), save_cover=True)
        video_path = os.path.join(self.tempdir.name, 'recording.m4s')

        await downloader.on_video_file_completed(video_path)

        export = self.store.export(room_id=97)
        self.addCleanup(lambda: os.path.exists(export.path) and os.remove(export.path))
        with zipfile.ZipFile(export.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read('records.jsonl').decode().splitlines()
            ]
        self.assertEqual(self.cover_attempts, 3)
        self.assertEqual(len({record['operation_id'] for record in records}), 1)
        self.assertEqual([record['retry']['attempt'] for record in records], [1, 2, 3])

    async def test_concurrent_api_candidates_are_not_mislabeled_as_retries(
        self,
    ) -> None:
        async with aiohttp.ClientSession() as session:
            api = WebApi(session, room_id=100, http_history=self.store)
            api.base_play_info_api_urls = [self.base_url, self.base_url]
            self.assertEqual(len(await api.get_room_play_infos(100)), 2)

        export = self.store.export(room_id=100)
        self.addCleanup(lambda: os.path.exists(export.path) and os.remove(export.path))
        with zipfile.ZipFile(export.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read('records.jsonl').decode().splitlines()
            ]
        self.assertEqual([record['retry']['attempt'] for record in records], [1, 1])
        self.assertEqual(len({record['parent_operation_id'] for record in records}), 1)
        self.assertEqual(len({record['operation_id'] for record in records}), 2)

    async def test_api_failures_and_retries_are_recorded_individually(self) -> None:
        async with aiohttp.ClientSession() as session:
            api = WebApi(session, room_id=77, http_history=self.store)
            request_once = BaseApi._get_json_res.__wrapped__
            for expected_error, path, timeout in (
                (ApiRequestError, '/risk', None),
                (ApiRequestError, '/risk', None),
                (aiohttp.ContentTypeError, '/non-json', None),
                (asyncio.TimeoutError, '/slow', aiohttp.ClientTimeout(total=0.001)),
            ):
                kwargs = {'_history_operation_id': 'same-operation'}
                if timeout is not None:
                    kwargs['timeout'] = timeout
                with self.assertRaises(expected_error):
                    await request_once(api, self.base_url + path, **kwargs)

        export = self.store.export(room_id=77)
        self.addCleanup(lambda: os.path.exists(export.path) and os.remove(export.path))
        with zipfile.ZipFile(export.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read('records.jsonl').decode().splitlines()
            ]
        self.assertEqual([item['retry']['attempt'] for item in records], [1, 2, 3, 4])
        self.assertEqual(records[0]['response']['body']['code'], -352)
        self.assertEqual(records[2]['response']['body'], 'upstream html response')
        self.assertIn('TimeoutError', records[3]['error'])

    async def test_http_error_keeps_status_headers_and_response_body(self) -> None:
        async with aiohttp.ClientSession(raise_for_status=True) as session:
            api = WebApi(session, room_id=78, http_history=self.store)
            request_once = BaseApi._get_json_res.__wrapped__
            with self.assertRaises(aiohttp.ClientResponseError):
                await request_once(api, self.base_url + '/unavailable')

        export = self.store.export(room_id=78)
        self.addCleanup(lambda: os.path.exists(export.path) and os.remove(export.path))
        with zipfile.ZipFile(export.path) as archive:
            record = json.loads(archive.read('records.jsonl').decode())
        self.assertEqual(record['response']['status'], 503)
        self.assertEqual(
            record['response']['headers']['Content-Type'],
            'application/json; charset=utf-8',
        )
        self.assertIn('upstream unavailable', record['response']['body'])

    async def test_html_parse_failure_records_a_bounded_diagnostic_body(self) -> None:
        live = Live(88, http_history=self.store)
        live._html_page_url = self.base_url + '/broken-html'
        try:
            with self.assertRaises(ValueError):
                await live._get_info_via_html_page()
        finally:
            await live.deinit()

        export = self.store.export(room_id=88)
        self.addCleanup(lambda: os.path.exists(export.path) and os.remove(export.path))
        with zipfile.ZipFile(export.path) as archive:
            record = json.loads(archive.read('records.jsonl').decode())
        self.assertEqual(record['category'], 'html')
        self.assertIn('missing init data', record['response']['body'])
        self.assertIn('ValueError', record['error'])

    async def test_html_live_status_saves_extracted_initialization_json(self) -> None:
        live = Live(89, http_history=self.store)
        live._html_page_url = self.base_url + '/valid-html'
        try:
            self.assertEqual(await live._get_live_status_via_html_page(), 1)
        finally:
            await live.deinit()

        export = self.store.export(room_id=89)
        self.addCleanup(lambda: os.path.exists(export.path) and os.remove(export.path))
        with zipfile.ZipFile(export.path) as archive:
            record = json.loads(archive.read('records.jsonl').decode())
        self.assertEqual(
            record['response']['body']['roomInitRes']['data']['live_status'], 1
        )


if __name__ == '__main__':
    unittest.main()
