"""通过有界队列把 Rx observer 通知切换到一个专用线程。"""

from queue import Queue
from threading import Thread, current_thread
from typing import Any, Callable, Dict, Optional, TypeVar

from loguru import logger
from reactivex import Observable, abc
from reactivex.disposable import CompositeDisposable, Disposable, SerialDisposable

_T = TypeVar('_T')


def observe_on_new_thread(
    queue_size: Optional[int] = None,
    thread_name: Optional[str] = None,
    logger_context: Optional[Dict[str, Any]] = None,
) -> Callable[[Observable[_T]], Observable[_T]]:
    def observe_on(source: Observable[_T]) -> Observable[_T]:
        def subscribe(
            observer: abc.ObserverBase[_T],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            disposed = False
            subscription = SerialDisposable()
            # 有界队列提供显式反压：生产者过快时阻塞，而不是无限增长内存。
            queue: Queue[Callable[..., Any]] = Queue(maxsize=queue_size or 0)

            def run() -> None:
                with logger.contextualize(**(logger_context or {})):
                    while not disposed:
                        queue.get()()

            thread = Thread(target=run, name=thread_name, daemon=True)
            thread.start()

            def on_next(value: _T) -> None:
                queue.put(lambda: observer.on_next(value))

            def on_error(exc: Exception) -> None:
                queue.put(lambda: observer.on_error(exc))

            def on_completed() -> None:
                queue.put(lambda: observer.on_completed)

            def dispose() -> None:
                nonlocal disposed
                disposed = True
                # 哨兵唤醒可能阻塞在 queue.get 的 worker，再等待线程完整退出。
                queue.put(lambda: None)
                if thread is not current_thread():
                    thread.join()

            subscription.disposable = source.subscribe(
                on_next, on_error, on_completed, scheduler=scheduler
            )

            return CompositeDisposable(subscription, Disposable(dispose))

        return Observable(subscribe)

    return observe_on
