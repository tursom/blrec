"""丢弃短于阈值、无法形成可用媒体段的 FLV 碎片。"""

from typing import Callable, List, Optional

from loguru import logger
from reactivex import Observable, abc
from reactivex.disposable import CompositeDisposable, Disposable, SerialDisposable

from ..models import FlvHeader
from .typing import FLVStream, FLVStreamItem

__all__ = ('defragment',)


def defragment(min_tags: int = 10) -> Callable[[FLVStream], FLVStream]:
    def _defragment(source: FLVStream) -> FLVStream:
        """缓存每个新流的开头，标签数超过阈值后才整体放行。"""

        def subscribe(
            observer: abc.ObserverBase[FLVStreamItem],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            disposed = False
            subscription = SerialDisposable()

            gathering: bool = False
            gathered_items: List[FLVStreamItem] = []

            def reset() -> None:
                nonlocal gathering, gathered_items
                gathering = False
                gathered_items = []

            def on_next(item: FLVStreamItem) -> None:
                nonlocal gathering

                if isinstance(item, FlvHeader):
                    # 新 Header 到达时仍未放行的上一段即被视为碎片并整体丢弃。
                    if gathered_items:
                        logger.debug(
                            'Discarded {} items, total size: {}'.format(
                                len(gathered_items), sum(len(t) for t in gathered_items)
                            )
                        )
                        gathered_items.clear()
                    gathering = True
                    logger.debug('Gathering items...')

                if gathering:
                    gathered_items.append(item)
                    if len(gathered_items) > min_tags:
                        for item in gathered_items:
                            observer.on_next(item)
                        gathered_items.clear()
                        gathering = False
                        logger.debug('Not a fragmented stream, stopped the gathering')
                else:
                    observer.on_next(item)

            def dispose() -> None:
                nonlocal disposed
                disposed = True
                reset()

            subscription.disposable = source.subscribe(
                on_next, observer.on_error, observer.on_completed, scheduler=scheduler
            )

            return CompositeDisposable(subscription, Disposable(dispose))

        return Observable(subscribe)

    return _defragment
