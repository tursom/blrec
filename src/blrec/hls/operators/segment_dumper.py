"""把 fMP4 init/media 数据顺序写入单个 m4s 文件并记录 byte range。"""

import io
from pathlib import PurePath
from typing import Any, Callable, Dict, Optional, Tuple, Union

import attr
from loguru import logger
from reactivex import Observable, Subject, abc
from reactivex.disposable import CompositeDisposable, Disposable, SerialDisposable

from blrec.http_history import HttpHistoryStore, mark_http_incident
from blrec.utils.ffprobe import StreamProfile, ffprobe

from .segment_fetcher import InitSectionData, SegmentData

__all__ = ('SegmentDumper',)


class SegmentDumper:
    """初始化段变化或上游 split 标志会关闭当前文件并创建新文件。"""

    def __init__(
        self,
        path_provider: Callable[[Optional[int]], Tuple[str, int]],
        *,
        http_history: Optional[HttpHistoryStore] = None,
        room_id: Optional[int] = None,
    ) -> None:
        self._path_provider = path_provider
        self._http_history = http_history
        self._room_id = room_id
        self._file_opened: Subject[Tuple[str, int]] = Subject()
        self._file_closed: Subject[str] = Subject()
        self._reset()

    def _reset(self) -> None:
        self._path: str = ''
        self._file: Optional[io.BufferedWriter] = None
        self._filesize: int = 0

    @property
    def path(self) -> str:
        return self._path

    @property
    def filesize(self) -> int:
        return self._filesize

    @property
    def file_opened(self) -> Observable[Tuple[str, int]]:
        return self._file_opened

    @property
    def file_closed(self) -> Observable[str]:
        return self._file_closed

    def __call__(
        self, source: Observable[Union[InitSectionData, SegmentData]]
    ) -> Observable[Union[InitSectionData, SegmentData]]:
        return self._dump(source)

    def _open_file(self) -> None:
        path, timestamp = self._path_provider()
        self._path = str(PurePath(path).with_suffix('.m4s'))
        self._file = open(self._path, 'wb')  # type: ignore
        logger.debug(f'Opened file: {self._path}')
        self._file_opened.on_next((self._path, timestamp))

    def _close_file(self) -> None:
        if self._file is not None and not self._file.closed:
            self._file.close()
            logger.debug(f'Closed file: {self._path}')
            self._file_closed.on_next(self._path)

    def _write_data(self, item: Union[InitSectionData, SegmentData]) -> Tuple[int, int]:
        assert self._file is not None
        offset = self._file.tell()
        size = self._file.write(item.payload)
        assert size == len(item)
        return offset, size

    def _update_filesize(self, size: int) -> None:
        self._filesize += size

    def _is_redundant(
        self, prev_init_item: Optional[InitSectionData], curr_init_item: InitSectionData
    ) -> bool:
        return (
            prev_init_item is not None
            and curr_init_item.payload == prev_init_item.payload
        )

    def _must_split_file(
        self, prev_init_item: Optional[InitSectionData], curr_init_item: InitSectionData
    ) -> bool:
        if prev_init_item is None:
            try:
                curr_profile = ffprobe(curr_init_item.payload)
            except Exception as e:
                logger.warning(f'Failed to probe current init section: {repr(e)}')
                self._report_incident('hls_init_probe_failed', {'error': repr(e)})
            else:
                logger.debug(f'current init section profile: {curr_profile}')
            return True

        try:
            prev_profile = ffprobe(prev_init_item.payload)
            curr_profile = ffprobe(curr_init_item.payload)
        except Exception as e:
            logger.warning(
                f'Failed to compare init section profiles, splitting file: {repr(e)}'
            )
            self._report_incident('hls_init_probe_failed', {'error': repr(e)})
            return True

        logger.debug(f'previous init section profile: {prev_profile}')
        logger.debug(f'current init section profile: {curr_profile}')
        prev_fingerprint = self._profile_fingerprint(prev_profile)
        curr_fingerprint = self._profile_fingerprint(curr_profile)
        if prev_fingerprint is None or curr_fingerprint is None:
            logger.warning('Incomplete init section profile, splitting file')
            self._report_incident('hls_init_profile_incomplete')
            return True
        if prev_fingerprint != curr_fingerprint:
            logger.warning('Init section track parameters changed')
            self._report_incident('hls_init_incompatible')
            return True

        logger.debug('Init section changed but track parameters remain compatible')
        return False

    def _report_incident(
        self, kind: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        if self._room_id is None:
            return
        mark_http_incident(
            self._http_history, room_id=self._room_id, kind=kind, details=details
        )

    def _profile_fingerprint(
        self, profile: StreamProfile
    ) -> Optional[Tuple[Tuple[Any, ...], ...]]:
        streams = profile.get('streams', [])
        if not streams:
            return None

        fingerprints = []
        for stream in streams:
            fingerprint = self._stream_fingerprint(stream)
            if fingerprint is None:
                return None
            fingerprints.append(fingerprint)
        return tuple(sorted(fingerprints))

    def _stream_fingerprint(self, stream: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
        codec_type = stream.get('codec_type')
        common_fields = (
            'id',
            'codec_name',
            'codec_tag_string',
            'profile',
            'time_base',
            'extradata_hash',
        )
        if codec_type == 'video':
            specific_fields = (
                'level',
                'width',
                'height',
                'coded_width',
                'coded_height',
            )
        elif codec_type == 'audio':
            specific_fields = ('sample_rate', 'channels', 'channel_layout')
        else:
            return None

        fields = common_fields + specific_fields
        if any(stream.get(field) is None for field in fields):
            return None
        return (codec_type, *(stream[field] for field in fields))

    def _need_split_file(self, item: Union[InitSectionData, SegmentData]) -> bool:
        return item.segment.custom_parser_values.get('split', False)

    def _dump(
        self, source: Observable[Union[InitSectionData, SegmentData]]
    ) -> Observable[Union[InitSectionData, SegmentData]]:
        def subscribe(
            observer: abc.ObserverBase[Union[InitSectionData, SegmentData]],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            disposed = False
            subscription = SerialDisposable()
            last_init_item: Optional[InitSectionData] = None

            def on_next(item: Union[InitSectionData, SegmentData]) -> None:
                nonlocal last_init_item
                split_file = False

                if isinstance(item, InitSectionData):
                    if self._is_redundant(last_init_item, item):
                        return
                    split_file = self._must_split_file(last_init_item, item)
                    last_init_item = item

                if not split_file:
                    split_file = self._need_split_file(item)

                if split_file:
                    self._close_file()
                    self._reset()
                    self._open_file()

                try:
                    if split_file and not isinstance(item, InitSectionData):
                        # 人工切片落在媒体段上时，新文件必须先补写最近的 init section。
                        assert last_init_item is not None
                        offset, size = self._write_data(last_init_item)
                        self._update_filesize(size)
                        observer.on_next(attr.evolve(last_init_item, offset=offset))

                    offset, size = self._write_data(item)
                    self._update_filesize(size)
                    observer.on_next(attr.evolve(item, offset=offset))
                except Exception as e:
                    logger.error(f'Failed to write data: {repr(e)}')
                    self._close_file()
                    self._reset()
                    observer.on_error(e)

            def on_completed() -> None:
                self._close_file()
                self._reset()
                observer.on_completed()

            def on_error(e: Exception) -> None:
                self._close_file()
                self._reset()
                observer.on_error(e)

            def dispose() -> None:
                nonlocal disposed
                nonlocal last_init_item
                disposed = True
                last_init_item = None
                self._close_file()
                self._reset()

            subscription.disposable = source.subscribe(
                on_next, on_error, on_completed, scheduler=scheduler
            )

            return CompositeDisposable(subscription, Disposable(dispose))

        return Observable(subscribe)
