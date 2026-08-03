from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests

from blrec.http_history import (
    HttpHistoryStore,
    record_http_exchange,
    redirects_from_response,
)


class _HlsHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect.m3u8":
            self.send_response(302)
            self.send_header("Location", "/index.m3u8")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required for the incident smoke test",
)
class HttpIncidentReplaySmokeTestCase(unittest.TestCase):
    def test_exported_bundle_reconstructs_real_fragmented_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fragmented_mp4 = root / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=160x90:r=10",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:sample_rate=48000",
                    "-t",
                    "1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "frag_keyframe+empty_moov+default_base_moof",
                    str(fragmented_mp4),
                ],
                check=True,
            )
            source = fragmented_mp4.read_bytes()
            moof_offset = source.index(b"moof") - 4
            init = source[:moof_offset]
            media = source[moof_offset:]
            (root / "init.mp4").write_bytes(init)
            (root / "media.m4s").write_bytes(media)
            (root / "corrupt.m4s").write_bytes(media[: len(media) // 2])
            (root / "index.m3u8").write_text(
                '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-MAP:URI="init.mp4"\n'
                "#EXTINF:1.0,\nmedia.m4s\n#EXT-X-ENDLIST\n",
                encoding="utf8",
            )

            handler = functools.partial(_HlsHandler, directory=directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base_url = f"http://127.0.0.1:{server.server_port}"

            store = HttpHistoryStore(str(root / "history"))
            store.start()
            self.addCleanup(store.close)
            session = requests.Session()
            for category, path, role in (
                ("hls_playlist", "/redirect.m3u8", None),
                ("hls_init", "/init.mp4", "hls_init"),
                ("hls_media", "/media.m4s", "hls_media"),
                ("hls_media", "/corrupt.m4s", "hls_media"),
            ):
                response = session.get(base_url + path, timeout=3)
                response.raise_for_status()
                record_http_exchange(
                    store,
                    room_id=88,
                    category=category,
                    method="GET",
                    url=response.url,
                    request_headers=response.request.headers,
                    response_status=response.status_code,
                    response_headers=response.headers,
                    response_body=response.text if category == "hls_playlist" else None,
                    response_payload=response.content if role else None,
                    payload_role=role,
                    redirects=redirects_from_response(response),
                    operation_id=path,
                )
            occurred_at = datetime.now(timezone.utc)
            incident_id = store.mark_incident(
                room_id=88,
                kind="hls_segment_corrupted",
                operation_id="/corrupt.m4s",
                occurred_at=occurred_at,
            )
            store.record(
                {
                    "record_type": "incident_window_boundary",
                    "recorded_at": (occurred_at + timedelta(seconds=11)).isoformat(),
                    "room_id": 88,
                }
            )
            exported = store.export_incident(incident_id)
            self.addCleanup(
                lambda: os.path.exists(exported.path) and os.remove(exported.path)
            )

            with zipfile.ZipFile(exported.path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                records = [
                    json.loads(line)
                    for line in archive.read("records.jsonl").decode().splitlines()
                ]
                resources = {
                    urlsplit(record["request"]["url"]).path: archive.read(
                        f'payloads/{record["response"]["payload"]["sha256"]}.bin'
                    )
                    for record in records
                    if record.get("response", {}).get("payload", {}).get("sha256")
                }

            reconstructed = root / "reconstructed.mp4"
            reconstructed.write_bytes(resources["/init.mp4"] + resources["/media.m4s"])
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    str(reconstructed),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            codec_types = {
                stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]
            }

            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["incident"]["status"], "ready")
            self.assertIn("video", codec_types)
            self.assertIn("audio", codec_types)
            playlist = next(
                record for record in records if record.get("category") == "hls_playlist"
            )
            self.assertEqual(playlist["redirects"][0]["status"], 302)
            self.assertEqual(resources["/corrupt.m4s"], media[: len(media) // 2])


if __name__ == "__main__":
    unittest.main()
