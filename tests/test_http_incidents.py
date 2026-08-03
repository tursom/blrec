from __future__ import annotations

import hashlib
import json
import os
import queue
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import m3u8
import requests
from reactivex import from_iterable

import blrec.http_history.store as history_store_module
from blrec.http_history import (
    HttpHistoryStore,
    mark_http_incident,
    record_http_exchange,
)

# Recorder package initialization expects settings/application to be loaded first.
# isort: off
from blrec.setting import Settings  # noqa: F401
from blrec.application import Application  # noqa: F401

# isort: on
from blrec.hls.operators.segment_fetcher import SegmentFetcher
from blrec.utils.hash import cksum


class HttpIncidentBundleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = HttpHistoryStore(self.tmp.name, max_size=500 * 1024**2)
        self.store.start()
        self.addCleanup(self.store.close)

    def test_incident_export_contains_ordered_records_and_deduplicated_payloads(
        self,
    ) -> None:
        payload = b"captured-init"
        self.store.record(
            {
                "record_type": "http_exchange",
                "room_id": 7,
                "category": "api",
                "request": {"method": "GET", "url": "https://api.test/room"},
                "response": {"status": 200, "body": {"code": 0}},
            }
        )
        self.store.record(
            {
                "record_type": "http_exchange",
                "room_id": 7,
                "category": "hls_init",
                "operation_id": "init-a",
                "request": {"method": "GET", "url": "https://cdn.test/init.mp4"},
                "response": {"status": 200},
            },
            payload=payload,
            payload_role="hls_init",
            payload_content_type="video/mp4",
        )
        incident_id = self.store.mark_incident(
            room_id=7,
            kind="hls_init_unstable",
            operation_id="init-a",
            details={"attempts": 3},
        )
        self.store.record(
            {
                "record_type": "http_exchange",
                "room_id": 7,
                "category": "hls_init",
                "operation_id": "init-b",
                "request": {"method": "GET", "url": "https://cdn.test/init.mp4"},
                "response": {"status": 200},
            },
            payload=payload,
            payload_role="hls_init",
            payload_content_type="video/mp4",
        )

        incidents = self.store.list_incidents(room_id=7)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].incident_id, incident_id)
        self.assertEqual(incidents[0].kind, "hls_init_unstable")
        self.assertEqual(incidents[0].occurrence_count, 1)

        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read("manifest.json"))
            records = [
                json.loads(line)
                for line in archive.read("records.jsonl").decode().splitlines()
            ]

            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["bundle_kind"], "incident")
            self.assertEqual(manifest["incident"]["incident_id"], incident_id)
            self.assertIn("README.txt", names)
            payload_names = [name for name in names if name.startswith("payloads/")]
            self.assertEqual(len(payload_names), 1)
            self.assertEqual(archive.read(payload_names[0]), payload)

        sequences = [record["sequence"] for record in records]
        self.assertEqual(sequences, sorted(sequences))
        init_records = [
            record for record in records if record["category"] == "hls_init"
        ]
        self.assertEqual(len(init_records), 2)
        self.assertEqual(
            init_records[0]["response"]["payload"]["sha256"],
            init_records[1]["response"]["payload"]["sha256"],
        )

    def test_same_room_and_kind_are_merged_within_cooldown(self) -> None:
        first_id = self.store.mark_incident(room_id=9, kind="hls_segment_corrupted")
        second_id = self.store.mark_incident(room_id=9, kind="hls_segment_corrupted")

        self.assertEqual(second_id, first_id)
        incidents = self.store.list_incidents(room_id=9)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].occurrence_count, 2)

    def test_exchange_capture_preserves_timing_redirects_and_hls_payload(self) -> None:
        record_http_exchange(
            self.store,
            room_id=11,
            category="hls_media",
            method="GET",
            url="https://cdn.test/segment.m4s?token=secret",
            request_headers={"Cookie": "SESSDATA=secret"},
            request_body={"access_token": "secret", "value": 1},
            response_status=200,
            response_headers={"Content-Type": "video/mp4"},
            response_payload=b"media-bytes",
            payload_role="hls_media",
            redirects=[
                {
                    "status": 302,
                    "url": "https://origin.test/segment?token=secret",
                    "location": "https://cdn.test/segment.m4s?token=secret",
                }
            ],
            duration_ms=12.5,
            operation_id="media-op",
        )
        incident_id = self.store.mark_incident(
            room_id=11, kind="hls_segment_corrupted", operation_id="media-op"
        )

        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            record = json.loads(archive.read("records.jsonl").decode())

        self.assertTrue(record["exchange_id"])
        self.assertIn("request_started_at", record)
        self.assertIn("response_completed_at", record)
        self.assertEqual(record["request"]["body"]["value"], 1)
        self.assertEqual(record["request"]["body"]["access_token"], "[REDACTED]")
        self.assertEqual(record["redirects"][0]["status"], 302)
        self.assertNotIn("secret", json.dumps(record))
        self.assertEqual(record["response"]["payload"]["role"], "hls_media")

    def test_segment_fetcher_captures_each_init_and_media_response(self) -> None:
        playlist = m3u8.loads(
            "#EXTM3U\n"
            '#EXT-X-MAP:URI="init.mp4"\n'
            f'#EXTINF:1.0,5|{cksum(b"media")}\n'
            "1.m4s\n",
            uri="https://cdn.test/live/index.m3u8",
        )
        responses = []
        for payload in (b"init", b"init", b"media"):
            response = Mock(
                url="https://cdn.test/resource",
                status_code=200,
                headers={"Content-Type": "video/mp4"},
                content=payload,
                history=[],
            )
            response.request.headers = {}
            response.raise_for_status.return_value = None
            responses.append(response)
        session = Mock()
        session.get.side_effect = responses
        live = SimpleNamespace(room_id=12, headers={}, http_history=self.store)
        output = []

        with patch("blrec.hls.operators.segment_fetcher.time.sleep"):
            from_iterable([playlist.segments[0]]).pipe(
                SegmentFetcher(live, session, Mock())
            ).subscribe(output.append)

        incident_id = self.store.mark_incident(room_id=12, kind="hls_capture_check")
        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read("records.jsonl").decode().splitlines()
            ]
            payloads = [
                archive.read(name)
                for name in archive.namelist()
                if name.startswith("payloads/")
            ]

        self.assertEqual([item.payload for item in output], [b"init", b"media"])
        exchanges = [
            record for record in records if record["record_type"] == "http_exchange"
        ]
        self.assertEqual(
            [record["category"] for record in exchanges],
            ["hls_init", "hls_init", "hls_media"],
        )
        self.assertCountEqual(payloads, [b"init", b"media"])

    def test_segment_fetcher_captures_a_failed_hls_response_body(self) -> None:
        playlist = m3u8.loads(
            '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n' "#EXTINF:1.0,5|00000000\n1.m4s\n",
            uri="https://cdn.test/live/index.m3u8",
        )
        init_responses = []
        for _ in range(2):
            init_response = Mock(
                url="https://cdn.test/live/init.mp4",
                status_code=200,
                headers={"Content-Type": "video/mp4"},
                content=b"init",
                history=[],
            )
            init_response.request.headers = {}
            init_response.raise_for_status.return_value = None
            init_responses.append(init_response)
        response = Mock(
            url="https://cdn.test/live/1.m4s",
            status_code=503,
            headers={"Content-Type": "application/octet-stream"},
            content=b"bad-media-response",
            history=[],
        )
        response.request.headers = {}
        response.raise_for_status.side_effect = requests.HTTPError(
            "unavailable", response=response
        )
        session = Mock()
        session.get.side_effect = [*init_responses, response]
        live = SimpleNamespace(room_id=13, headers={}, http_history=self.store)

        from_iterable([playlist.segments[0]]).pipe(
            SegmentFetcher(live, session, Mock())
        ).subscribe()
        incident_id = self.store.mark_incident(room_id=13, kind="hls_media_corrupted")

        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            payloads = [
                archive.read(name)
                for name in archive.namelist()
                if name.startswith("payloads/")
            ]
        self.assertIn(b"bad-media-response", payloads)

    def test_incident_contains_the_exact_pre_and_post_error_window(self) -> None:
        occurred_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

        def record_at(seconds: int) -> None:
            self.store.record(
                {
                    "record_type": "http_exchange",
                    "recorded_at": (
                        occurred_at + timedelta(seconds=seconds)
                    ).isoformat(),
                    "room_id": 20,
                    "request": {"url": f"https://cdn.test/{seconds}"},
                }
            )

        for seconds in (-31, -30, 0):
            record_at(seconds)
        incident_id = self.store.mark_incident(
            room_id=20, kind="hls_playlist_stalled", occurred_at=occurred_at
        )
        for seconds in (10, 11):
            record_at(seconds)

        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read("records.jsonl").decode().splitlines()
            ]

        self.assertEqual(
            [record["request"]["url"] for record in records],
            ["https://cdn.test/-30", "https://cdn.test/0", "https://cdn.test/10"],
        )

    def test_incident_resolves_a_deduplicated_playlist_to_its_latest_full_body(
        self,
    ) -> None:
        occurred_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        body = "#EXTM3U\n#EXT-X-VERSION:7\n"
        digest = hashlib.sha256(body.encode()).hexdigest()
        self.store.record(
            {
                "record_type": "http_exchange",
                "recorded_at": (occurred_at - timedelta(seconds=60)).isoformat(),
                "room_id": 26,
                "category": "hls_playlist",
                "request": {"url": "https://cdn.test/live.m3u8"},
                "response": {"status": 200, "body": body, "body_sha256": digest},
                "body_sha256": digest,
                "body_unchanged": False,
            }
        )
        self.store.record(
            {
                "record_type": "http_exchange",
                "recorded_at": occurred_at.isoformat(),
                "room_id": 26,
                "category": "hls_playlist",
                "request": {"url": "https://cdn.test/live.m3u8"},
                "response": {"status": 200},
                "body_sha256": digest,
                "body_unchanged": True,
            }
        )
        incident_id = self.store.mark_incident(
            room_id=26, kind="hls_playlist_stalled", occurred_at=occurred_at
        )

        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            records = [
                json.loads(line)
                for line in archive.read("records.jsonl").decode().splitlines()
            ]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["response"]["body"], body)
        self.assertTrue(records[0]["body_resolved_from_record_id"])

    def test_active_incident_is_recovered_as_ready_and_partial_after_restart(
        self,
    ) -> None:
        self.store.record(
            {
                "record_type": "http_exchange",
                "room_id": 21,
                "request": {"url": "https://cdn.test/init"},
                "response": {"status": 200},
            },
            payload=b"init",
            payload_role="hls_init",
        )
        incident_id = self.store.mark_incident(room_id=21, kind="hls_init_unstable")
        self.store.flush()
        self.store.close()

        self.store = HttpHistoryStore(self.tmp.name, max_size=500 * 1024**2)
        self.store.start()
        self.addCleanup(self.store.close)
        incidents = self.store.list_incidents(room_id=21)

        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].incident_id, incident_id)
        self.assertEqual(incidents[0].status, "ready")
        self.assertTrue(incidents[0].partial)
        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            self.assertEqual(
                archive.read(
                    next(
                        name
                        for name in archive.namelist()
                        if name.startswith("payloads/")
                    )
                ),
                b"init",
            )

    def test_clear_removes_records_incidents_and_payloads(self) -> None:
        self.store.record(
            {
                "record_type": "http_exchange",
                "room_id": 22,
                "request": {"url": "https://cdn.test/media"},
                "response": {"status": 200},
            },
            payload=b"media",
            payload_role="hls_media",
        )
        self.store.mark_incident(room_id=22, kind="hls_segment_corrupted")
        self.store.flush()

        self.store.clear()

        self.assertEqual(self.store.list_incidents(), [])
        self.assertEqual(self.store.status().record_count, 0)
        self.assertFalse(list(Path(self.tmp.name).glob("http-incident-*.json")))
        self.assertFalse(list((Path(self.tmp.name) / "payloads").glob("*.bin")))

    def test_omitted_payload_keeps_metadata_and_marks_incident_partial(self) -> None:
        with patch.object(history_store_module, "_QUEUED_PAYLOAD_LIMIT", 0):
            self.store.record(
                {
                    "record_type": "http_exchange",
                    "room_id": 23,
                    "request": {"url": "https://cdn.test/media"},
                    "response": {"status": 200},
                },
                payload=b"four",
                payload_role="hls_media",
            )
        incident_id = self.store.mark_incident(room_id=23, kind="hls_media_corrupted")

        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            record = json.loads(archive.read("records.jsonl"))

        self.assertTrue(manifest["incident"]["partial"])
        self.assertEqual(len(manifest["omitted_payloads"]), 1)
        self.assertTrue(record["response"]["payload"]["omitted"])
        self.assertEqual(record["response"]["payload"]["size"], 4)
        self.assertEqual(
            record["response"]["payload"]["sha256"], hashlib.sha256(b"four").hexdigest()
        )

    def test_payload_disk_failure_keeps_metadata_and_marks_incident_partial(
        self,
    ) -> None:
        with patch.object(
            self.store, "_write_payload", side_effect=OSError("disk full")
        ):
            self.store.record(
                {
                    "record_type": "http_exchange",
                    "room_id": 24,
                    "request": {"url": "https://cdn.test/init"},
                    "response": {"status": 200},
                },
                payload=b"init",
                payload_role="hls_init",
            )
            incident_id = self.store.mark_incident(room_id=24, kind="hls_init_unstable")
            result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            record = json.loads(archive.read("records.jsonl"))

        self.assertTrue(manifest["incident"]["partial"])
        self.assertIn("disk full", manifest["omitted_payloads"][0]["reason"])
        self.assertEqual(record["request"]["url"], "https://cdn.test/init")
        self.assertTrue(record["response"]["payload"]["omitted"])

    def test_incident_payload_limit_prioritizes_the_triggering_exchange(self) -> None:
        occurred_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        with patch.object(history_store_module, "_INCIDENT_PAYLOAD_LIMIT", 4):
            for seconds, operation_id, role, payload in (
                (-20, "old-media", "hls_media", b"old!"),
                (-10, "init", "hls_init", b"init"),
                (0, "trigger", "hls_media", b"fail"),
            ):
                self.store.record(
                    {
                        "record_type": "http_exchange",
                        "recorded_at": (
                            occurred_at + timedelta(seconds=seconds)
                        ).isoformat(),
                        "room_id": 25,
                        "operation_id": operation_id,
                        "request": {"url": f"https://cdn.test/{operation_id}"},
                        "response": {"status": 500 if seconds == 0 else 200},
                    },
                    payload=payload,
                    payload_role=role,
                )
            incident_id = self.store.mark_incident(
                room_id=25,
                kind="hls_media_corrupted",
                operation_id="trigger",
                occurred_at=occurred_at,
            )
            result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            payload_names = [
                name for name in archive.namelist() if name.startswith("payloads/")
            ]
            payload = archive.read(payload_names[0])

        self.assertEqual(payload, b"fail")
        self.assertTrue(manifest["incident"]["partial"])
        self.assertEqual(len(manifest["omitted_payloads"]), 2)

    def test_incident_payload_limit_prioritizes_playlist_before_other_errors(
        self,
    ) -> None:
        occurred_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        with patch.object(history_store_module, "_INCIDENT_PAYLOAD_LIMIT", 12):
            for seconds, operation_id, category, role, status, payload in (
                (-20, "init", "hls_init", "hls_init", 200, b"init"),
                (-15, "playlist", "hls_playlist", "hls_playlist", 200, b"list"),
                (-1, "other-error", "hls_media", "hls_media", 500, b"oops"),
                (0, "trigger", "hls_media", "hls_media", 500, b"fail"),
            ):
                self.store.record(
                    {
                        "record_type": "http_exchange",
                        "recorded_at": (
                            occurred_at + timedelta(seconds=seconds)
                        ).isoformat(),
                        "room_id": 38,
                        "category": category,
                        "operation_id": operation_id,
                        "request": {"url": f"https://cdn.test/{operation_id}"},
                        "response": {"status": status},
                        "outcome": "error" if status >= 400 else "success",
                    },
                    payload=payload,
                    payload_role=role,
                )
            incident_id = self.store.mark_incident(
                room_id=38,
                kind="hls_media_corrupted",
                operation_id="trigger",
                occurred_at=occurred_at,
            )
            result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))

        with zipfile.ZipFile(result.path) as archive:
            payloads = {
                archive.read(name)
                for name in archive.namelist()
                if name.startswith("payloads/")
            }

        self.assertEqual(payloads, {b"fail", b"init", b"list"})

    def test_incident_resource_limit_omits_lower_priority_records_from_export(
        self,
    ) -> None:
        occurred_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        with patch.object(history_store_module, "_INCIDENT_RESOURCE_LIMIT", 1100):
            for seconds, operation_id, payload in (
                (-10, "context", b"context"),
                (0, "trigger", None),
            ):
                self.store.record(
                    {
                        "record_type": "http_exchange",
                        "recorded_at": (
                            occurred_at + timedelta(seconds=seconds)
                        ).isoformat(),
                        "room_id": 39,
                        "category": "hls_media",
                        "operation_id": operation_id,
                        "request": {"url": f"https://cdn.test/{operation_id}"},
                        "response": {
                            "status": 500 if seconds == 0 else 200,
                            "body": "x" * 400,
                        },
                        "outcome": "error" if seconds == 0 else "success",
                    },
                    payload=payload,
                    payload_role="hls_media" if payload is not None else None,
                )
            incident_id = self.store.mark_incident(
                room_id=39,
                kind="hls_media_corrupted",
                operation_id="trigger",
                occurred_at=occurred_at,
            )
            result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))

        with zipfile.ZipFile(result.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            records = [
                json.loads(line)
                for line in archive.read("records.jsonl").decode().splitlines()
            ]

        self.assertEqual([record["operation_id"] for record in records], ["trigger"])
        self.assertEqual(manifest["capture"]["resource_limit"], 1100)
        self.assertLessEqual(manifest["capture"]["resource_size"], 1100)
        self.assertEqual(len(manifest["omitted_records"]), 1)
        self.assertEqual(len(manifest["omitted_payloads"]), 1)
        self.assertEqual(
            manifest["omitted_payloads"][0]["sha256"],
            hashlib.sha256(b"context").hexdigest(),
        )
        self.assertTrue(manifest["incident"]["partial"])

    def test_total_quota_evicts_the_oldest_ready_incident_and_orphan_payload(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        incident_ids = []
        for room_id, minutes, payload in ((30, -60, b"a" * 600), (31, -30, b"b" * 600)):
            occurred_at = now + timedelta(minutes=minutes)
            self.store.record(
                {
                    "record_type": "http_exchange",
                    "recorded_at": occurred_at.isoformat(),
                    "room_id": room_id,
                    "request": {"url": f"https://cdn.test/{room_id}"},
                    "response": {"status": 500},
                },
                payload=payload,
                payload_role="hls_media",
            )
            incident_ids.append(
                self.store.mark_incident(
                    room_id=room_id, kind="hls_media_corrupted", occurred_at=occurred_at
                )
            )
            self.store.list_incidents()

        self.store.configure(enabled=True, retention_days=7, max_size=2500)

        incidents = self.store.list_incidents()
        self.assertEqual([item.incident_id for item in incidents], [incident_ids[1]])
        status = self.store.status()
        self.assertLessEqual(status.total_size, 2500)
        self.assertIn(31, status.room_ids)
        payloads = list((Path(self.tmp.name) / "payloads").glob("*.bin"))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].read_bytes(), b"b" * 600)

    def test_incident_helper_does_not_raise_when_error_reporter_is_unavailable(
        self,
    ) -> None:
        store = Mock(spec=["mark_incident"])
        store.mark_incident.side_effect = RuntimeError("not ready")

        result = mark_http_incident(store, room_id=32, kind="hls_probe_failed")

        self.assertIsNone(result)

    def test_incident_marker_survives_a_full_writer_queue(self) -> None:
        self.store.close()
        self.store = HttpHistoryStore(self.tmp.name, max_size=500 * 1024**2)
        self.store._queue = queue.Queue(maxsize=1)
        original_write = self.store._write
        write_started = threading.Event()
        release_write = threading.Event()

        def blocked_write(record: dict) -> None:
            write_started.set()
            release_write.wait(timeout=2)
            original_write(record)

        with patch.object(self.store, "_write", side_effect=blocked_write):
            self.store.start()
            self.addCleanup(self.store.close)
            self.store.record(
                {"room_id": 33, "request": {"url": "https://cdn.test/one"}}
            )
            self.assertTrue(write_started.wait(timeout=2))
            self.store.record(
                {"room_id": 33, "request": {"url": "https://cdn.test/two"}}
            )
            incident_id = self.store.mark_incident(
                room_id=33, kind="hls_playlist_stalled"
            )
            release_write.set()
            incidents = self.store.list_incidents(room_id=33)

        self.assertEqual([item.incident_id for item in incidents], [incident_id])

    def test_completed_incident_is_evicted_as_soon_as_its_window_closes(self) -> None:
        self.store.close()
        self.store = HttpHistoryStore(self.tmp.name, max_size=1000)
        self.store.start()
        self.addCleanup(self.store.close)
        occurred_at = datetime.now(timezone.utc) - timedelta(hours=1)
        incident_id = self.store.mark_incident(
            room_id=34, kind="hls_media_corrupted", occurred_at=occurred_at
        )
        self.store.record(
            {
                "record_type": "http_exchange",
                "recorded_at": occurred_at.isoformat(),
                "room_id": 34,
                "operation_id": incident_id,
                "request": {"url": "https://cdn.test/media"},
                "response": {"status": 500},
            },
            payload=b"x" * 600,
            payload_role="hls_media",
        )

        incidents = self.store.list_incidents(room_id=34)

        self.assertEqual(incidents, [])
        self.assertLessEqual(self.store.status().total_size, 1000)

    def test_incident_export_sanitizes_persisted_text_again(self) -> None:
        self.store.record(
            {
                "record_type": "http_exchange",
                "room_id": 35,
                "request": {"url": "https://cdn.test/safe"},
            }
        )
        incident_id = self.store.mark_incident(room_id=35, kind="hls_playlist_stalled")
        self.store.flush()
        incident_path = Path(self.tmp.name) / f"http-incident-{incident_id}.json"
        incident = json.loads(incident_path.read_text())
        incident["details"] = [{"Cookie": "SESSDATA=incident-secret"}]
        incident["records"][0]["request"][
            "url"
        ] = "https://cdn.test/live.m3u8?token=record-secret"
        incident_path.write_text(json.dumps(incident))
        self.store.close()
        self.store = HttpHistoryStore(self.tmp.name, max_size=500 * 1024**2)
        self.store.start()
        self.addCleanup(self.store.close)

        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))

        with zipfile.ZipFile(result.path) as archive:
            exported_text = archive.read("manifest.json") + archive.read(
                "records.jsonl"
            )
        self.assertNotIn(b"incident-secret", exported_text)
        self.assertNotIn(b"record-secret", exported_text)

    def test_restart_marks_a_missing_content_addressed_payload_partial(self) -> None:
        occurred_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.store.record(
            {
                "record_type": "http_exchange",
                "recorded_at": occurred_at.isoformat(),
                "room_id": 36,
                "request": {"url": "https://cdn.test/init"},
                "response": {"status": 200},
            },
            payload=b"init",
            payload_role="hls_init",
        )
        incident_id = self.store.mark_incident(
            room_id=36, kind="hls_init_unstable", occurred_at=occurred_at
        )
        self.store.list_incidents()
        self.store.close()
        for path in (Path(self.tmp.name) / "payloads").glob("*.bin"):
            path.unlink()
        self.store = HttpHistoryStore(self.tmp.name, max_size=500 * 1024**2)
        self.store.start()
        self.addCleanup(self.store.close)

        incidents = self.store.list_incidents(room_id=36)
        result = self.store.export_incident(incident_id)
        self.addCleanup(lambda: os.path.exists(result.path) and os.remove(result.path))
        with zipfile.ZipFile(result.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))

        self.assertTrue(incidents[0].partial)
        self.assertEqual(manifest["payloads"], [])
        self.assertEqual(
            manifest["omitted_payloads"][0]["reason"],
            "content-addressed payload is missing",
        )

    def test_restart_merges_into_the_most_recent_incident_for_a_room_and_kind(
        self,
    ) -> None:
        self.store.close()
        first_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        recent_id = "0" * 32
        older_id = "f" * 32
        for incident_id, occurred_at in (
            (recent_id, first_at + timedelta(minutes=6)),
            (older_id, first_at),
        ):
            incident = {
                "schema_version": 2,
                "incident_id": incident_id,
                "room_id": 37,
                "kind": "hls_playlist_stalled",
                "first_at": occurred_at.isoformat(),
                "last_at": occurred_at.isoformat(),
                "capture_until": (occurred_at + timedelta(seconds=10)).isoformat(),
                "occurrence_count": 1,
                "status": "ready",
                "partial": False,
                "operation_ids": [],
                "details": [],
                "records": [],
                "payloads": {},
                "omitted_payloads": [],
                "payload_size": 0,
            }
            (Path(self.tmp.name) / f"http-incident-{incident_id}.json").write_text(
                json.dumps(incident), encoding="utf8"
            )
        self.store = HttpHistoryStore(self.tmp.name, max_size=500 * 1024**2)
        self.store.start()
        self.addCleanup(self.store.close)

        merged_id = self.store.mark_incident(
            room_id=37,
            kind="hls_playlist_stalled",
            occurred_at=first_at + timedelta(minutes=7),
        )

        self.assertEqual(merged_id, recent_id)


if __name__ == "__main__":
    unittest.main()
