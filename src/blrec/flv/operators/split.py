"""在 FLV 音视频参数变化时插入新的逻辑流边界。"""

from typing import Callable, Optional

from loguru import logger
from reactivex import Observable, abc
from reactivex.disposable import CompositeDisposable, Disposable, SerialDisposable

from ..common import (
    is_audio_sequence_header,
    is_metadata_tag,
    is_video_sequence_header,
    parse_metadata,
)
from ..models import AudioTag, FlvHeader, ScriptTag, VideoTag
from .typing import FLVStream, FLVStreamItem

__all__ = ('split',)


def split() -> Callable[[FLVStream], FLVStream]:
    def _split(source: FLVStream) -> FLVStream:
        """检测重复的序列头；参数改变后从下一数据标签开始新分段。"""

        def subscribe(
            observer: abc.ObserverBase[FLVStreamItem],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            disposed = False
            subscription = SerialDisposable()

            changed: bool = False
            last_flv_header: Optional[FlvHeader] = None
            last_metadata_tag: Optional[ScriptTag] = None
            last_audio_sequence_header: Optional[AudioTag] = None
            last_video_sequence_header: Optional[VideoTag] = None

            def reset() -> None:
                nonlocal changed
                nonlocal last_flv_header
                nonlocal last_metadata_tag
                nonlocal last_audio_sequence_header, last_video_sequence_header
                changed = False
                last_flv_header = None
                last_metadata_tag = None
                last_audio_sequence_header = last_video_sequence_header = None

            def insert_header_and_tags() -> None:
                # 新分段必须重放容器头、元数据及最新序列头，才能独立解码。
                assert last_flv_header is not None
                observer.on_next(last_flv_header)
                if last_metadata_tag is not None:
                    observer.on_next(last_metadata_tag)
                if last_video_sequence_header is not None:
                    observer.on_next(last_video_sequence_header)
                if last_audio_sequence_header is not None:
                    observer.on_next(last_audio_sequence_header)

            def on_next(item: FLVStreamItem) -> None:
                nonlocal changed
                nonlocal last_flv_header
                nonlocal last_metadata_tag
                nonlocal last_audio_sequence_header, last_video_sequence_header

                if isinstance(item, FlvHeader):
                    reset()
                    last_flv_header = item
                    observer.on_next(item)
                    return

                tag = item

                if is_metadata_tag(tag):
                    metadata = parse_metadata(tag)
                    logger.debug(f'Metadata tag: {tag}, metadata: {metadata}')
                    if last_metadata_tag is not None:
                        last_metadata_tag = tag
                        return
                    last_metadata_tag = tag
                elif is_audio_sequence_header(tag):
                    logger.debug(f'Audio sequence header: {tag}')
                    if last_audio_sequence_header is not None:
                        if not tag.is_the_same_as(last_audio_sequence_header):
                            logger.warning('Audio parameters changed')
                            changed = True
                        last_audio_sequence_header = tag
                        return
                    last_audio_sequence_header = tag
                elif is_video_sequence_header(tag):
                    logger.debug(f'Video sequence header: {tag}')
                    if last_video_sequence_header is not None:
                        if not tag.is_the_same_as(last_video_sequence_header):
                            logger.warning('Video parameters changed')
                            changed = True
                        last_video_sequence_header = tag
                        return
                    last_video_sequence_header = tag
                else:
                    if changed:
                        # 等序列头收集完成后再切，避免新文件从半套初始化参数开始。
                        logger.debug('Splitting stream...')
                        changed = False
                        insert_header_and_tags()
                        logger.debug('Splitted stream')

                observer.on_next(tag)

            def dispose() -> None:
                nonlocal disposed
                disposed = True
                reset()

            subscription.disposable = source.subscribe(
                on_next, observer.on_error, observer.on_completed, scheduler=scheduler
            )

            return CompositeDisposable(subscription, Disposable(dispose))

        return Observable(subscribe)

    return _split
