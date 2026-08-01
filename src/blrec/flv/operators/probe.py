"""从每个 FLV 分段开头采样少量标签供 ffprobe 识别媒体参数。"""

from __future__ import annotations

import io
from typing import List, Optional, cast

from loguru import logger
from reactivex import Observable, Subject, abc
from reactivex.disposable import CompositeDisposable, Disposable, SerialDisposable

from ...utils.ffprobe import StreamProfile, ffprobe_on
from ..common import find_aac_header_tag, find_avc_header_tag
from ..io import FlvWriter
from ..models import FlvHeader, FlvTag
from .typing import FLVStream, FLVStreamItem

__all__ = ('Prober', 'StreamProfile')


class Prober:
    """旁路探测媒体信息且不改写标签；ffprobe 在当前 Rx 调度线程同步执行。"""

    def __init__(self) -> None:
        self._profiles: Subject[StreamProfile] = Subject()

    def _reset(self) -> None:
        self._gathering: bool = False
        self._gathered_items: List[FLVStreamItem] = []

    @property
    def profiles(self) -> Observable[StreamProfile]:
        return self._profiles

    def __call__(self, source: FLVStream) -> FLVStream:
        return self._probe(source)

    def _probe(self, source: FLVStream) -> FLVStream:
        def subscribe(
            observer: abc.ObserverBase[FLVStreamItem],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            disposed = False
            subscription = SerialDisposable()

            self._reset()

            def on_next(item: FLVStreamItem) -> None:
                if isinstance(item, FlvHeader):
                    self._gathered_items.clear()
                    self._gathering = True

                if self._gathering:
                    self._gathered_items.append(item)
                    # 十个标签通常足以覆盖 FLV Header、音视频序列头和首批媒体数据。
                    if len(self._gathered_items) >= 10:
                        try:
                            self._do_probe()
                        except Exception as e:
                            logger.warning(f'Failed to probe stream: {repr(e)}')
                        finally:
                            self._gathered_items.clear()
                            self._gathering = False

                observer.on_next(item)

            def dispose() -> None:
                nonlocal disposed
                disposed = True
                self._reset()

            subscription.disposable = source.subscribe(
                on_next, observer.on_error, observer.on_completed, scheduler=scheduler
            )

            return CompositeDisposable(subscription, Disposable(dispose))

        return Observable(subscribe)

    def _do_probe(self) -> None:
        bytes_io = io.BytesIO()
        writer = FlvWriter(bytes_io)
        writer.write_header(cast(FlvHeader, self._gathered_items[0]))
        gathered_tags = cast(List[FlvTag], self._gathered_items[1:])

        avc_sequence_header = find_avc_header_tag(gathered_tags)
        aac_sequence_header = find_aac_header_tag(gathered_tags)
        if avc_sequence_header is not None and aac_sequence_header is not None:
            index_of_avc_sequence_header = gathered_tags.index(avc_sequence_header)
            index_of_aac_sequence_header = gathered_tags.index(aac_sequence_header)
            if index_of_avc_sequence_header > index_of_aac_sequence_header:
                # ffprobe 对初始化标签顺序敏感，临时样本统一改为 AVC 在 AAC 之前。
                gathered_tags[index_of_aac_sequence_header] = avc_sequence_header
                gathered_tags[index_of_avc_sequence_header] = aac_sequence_header

        for tag in gathered_tags:
            writer.write_tag(tag)

        def on_next(profile: StreamProfile) -> None:
            self._profiles.on_next(profile)

        def on_error(e: Exception) -> None:
            logger.warning(f'Failed to probe stream by ffprobe: {repr(e)}')

        ffprobe_on(bytes_io.getvalue()).subscribe(on_next, on_error)
