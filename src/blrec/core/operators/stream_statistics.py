"""通过包装可读流统计实际被下游消费的字节数和速率。"""

from __future__ import annotations

import io
from typing import Any, Optional

from reactivex import Observable, Subject, abc

from ..statistics import Statistics

__all__ = ('StreamStatistics',)


class StreamStatistics:
    """为每条上游字节流安装计数包装，并在流完成时冻结统计快照。"""

    def __init__(self) -> None:
        self._statistics = Statistics()

    @property
    def count(self) -> int:
        return self._statistics.count

    @property
    def rate(self) -> float:
        return self._statistics.rate

    @property
    def elapsed(self) -> float:
        return self._statistics.elapsed

    def freeze(self) -> None:
        self._statistics.freeze()

    def reset(self) -> None:
        self._statistics.reset()

    def __call__(self, source: Observable[io.RawIOBase]) -> Observable[io.RawIOBase]:
        self.reset()
        return self._calc(source)

    def _calc(self, source: Observable[io.RawIOBase]) -> Observable[io.RawIOBase]:
        def subscribe(
            observer: abc.ObserverBase[io.RawIOBase],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            def on_next(stream: io.RawIOBase) -> None:
                calculable_stream = CalculableStream(stream)
                calculable_stream.size_updates.subscribe(self._statistics.submit)
                observer.on_next(calculable_stream)

            def on_completed() -> None:
                self._statistics.freeze()
                observer.on_completed()

            return source.subscribe(
                on_next, observer.on_error, on_completed, scheduler=scheduler
            )

        return Observable(subscribe)


class CalculableStream(io.RawIOBase):
    """透传 read/readinto，同时发布每次成功读取的字节数。"""

    def __init__(self, stream: io.RawIOBase) -> None:
        self._stream = stream
        self._offset: int = 0
        self._size_updates: Subject[int] = Subject()

    @property
    def size_updates(self) -> Observable[int]:
        return self._size_updates

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        assert data is not None
        self._offset += len(data)
        self._size_updates.on_next(len(data))
        return data

    def readinto(self, b: Any) -> int:
        n = self._stream.readinto(b)
        assert n is not None
        self._offset += n
        self._size_updates.on_next(n)
        return n

    def tell(self) -> int:
        # 这里返回包装器累计读取量，不依赖底层流是否支持 seek/tell。
        return self._offset
