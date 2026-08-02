"""恢复进程异常退出时未封口的本地 HLS 录制。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import m3u8
from loguru import logger

__all__ = ('recover_incomplete_hls_recordings',)


def recover_incomplete_hls_recordings(
    out_dirs: Iterable[str], room_ids: Set[int]
) -> Dict[int, List[str]]:
    """封口属于已加载房间的残留 HLS 文件，并按房间返回视频路径。"""

    recovered: Dict[int, List[str]] = {}
    seen_playlists: Set[str] = set()
    roots = {
        os.path.realpath(os.path.abspath(os.path.expanduser(path))) for path in out_dirs
    }

    for root in sorted(roots):
        if not os.path.isdir(root):
            logger.warning(f'HLS recovery output directory does not exist: {root!r}')
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for filename in sorted(filenames):
                if not filename.endswith('.m3u8'):
                    continue
                playlist_path = os.path.realpath(os.path.join(dirpath, filename))
                if playlist_path in seen_playlists:
                    continue
                seen_playlists.add(playlist_path)
                try:
                    result = _recover_playlist(Path(playlist_path), room_ids)
                except Exception as exc:
                    logger.warning(
                        f'Failed to recover HLS playlist {playlist_path!r}: {exc!r}'
                    )
                    continue
                if result is None:
                    continue
                room_id, video_path = result
                recovered.setdefault(room_id, []).append(str(video_path))

    return recovered


def _recover_playlist(
    playlist_path: Path, room_ids: Set[int]
) -> Optional[Tuple[int, Path]]:
    video_path = playlist_path.with_suffix('.m4s')
    metadata_path = playlist_path.with_suffix('.meta.json')
    final_path = playlist_path.with_suffix('.mp4')

    if final_path.exists():
        return None

    try:
        playlist = m3u8.loads(playlist_path.read_text(encoding='utf8'))
    except Exception as exc:
        logger.warning(f'Cannot parse HLS playlist {playlist_path!s}: {exc!r}')
        return None
    if playlist.is_endlist:
        return None
    if not video_path.is_file():
        logger.warning(f'HLS recovery source is missing for {playlist_path!s}')
        return None
    if not metadata_path.is_file():
        logger.warning(f'HLS recovery metadata is missing for {playlist_path!s}')
        return None

    room_id = _read_room_id(metadata_path)
    if room_id is None or room_id not in room_ids:
        logger.warning(f'HLS recovery metadata does not match {playlist_path!s}')
        return None

    media_size = video_path.stat().st_size
    valid_count = 0
    for segment in playlist.segments:
        init_section = getattr(segment, 'init_section', None)
        if (
            init_section is None
            or not _is_expected_uri(init_section.uri, video_path.name)
            or not _is_expected_uri(segment.uri, video_path.name)
            or not _is_valid_byte_range(init_section.byterange, media_size)
            or not _is_valid_byte_range(segment.byterange, media_size)
        ):
            break
        valid_count += 1

    if valid_count == 0:
        logger.warning(f'HLS recovery found no valid segments in {playlist_path!s}')
        return None

    del playlist.segments[valid_count:]
    playlist.is_endlist = True
    _atomic_write(playlist_path, playlist.dumps())
    logger.info(f'Recovered incomplete HLS recording: {video_path!s}')
    return room_id, video_path


def _read_room_id(metadata_path: Path) -> Optional[int]:
    try:
        metadata = json.loads(metadata_path.read_text(encoding='utf8'))
        room_id = metadata['description']['RoomId']
        if isinstance(room_id, bool):
            return None
        if isinstance(room_id, int):
            return room_id
        if isinstance(room_id, str) and room_id.isdigit():
            return int(room_id)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def _is_expected_uri(uri: Optional[str], filename: str) -> bool:
    if not uri:
        return False
    parsed = urlsplit(uri)
    path = PurePosixPath(parsed.path)
    return (
        not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
        and str(path.parent) == '.'
        and path.name == filename
    )


def _is_valid_byte_range(value: Optional[str], media_size: int) -> bool:
    if not value or '@' not in value:
        return False
    size_text, offset_text = value.split('@', maxsplit=1)
    try:
        size = int(size_text)
        offset = int(offset_text)
    except ValueError:
        return False
    return size > 0 and offset >= 0 and offset + size <= media_size


def _atomic_write(path: Path, content: str) -> None:
    temp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='wt',
            encoding='utf8',
            dir=str(path.parent),
            prefix=f'.{path.name}.',
            suffix='.tmp',
            delete=False,
        ) as file:
            temp_path = file.name
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            os.unlink(temp_path)
