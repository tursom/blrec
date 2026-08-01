"""把录制管线异常分类为可重试错误或终止当前任务的稳定状态。"""

from __future__ import annotations

import errno
from typing import Optional, TypeVar

from loguru import logger
from reactivex import Observable, abc

from blrec.bili.exceptions import LiveRoomEncrypted, LiveRoomHidden, LiveRoomLocked
from blrec.exception.helpers import format_exception
from blrec.utils import operators as utils_ops
from blrec.utils.mixins import AsyncCooperationMixin

__all__ = ('ExceptionHandler',)


_T = TypeVar('_T')


class ExceptionHandler(AsyncCooperationMixin):
    """提交所有异常；磁盘耗尽和不可访问房间正常完成，其余异常交给 retry。"""

    def __call__(self, source: Observable[_T]) -> Observable[_T]:
        return self._handle(source).pipe(utils_ops.retry(delay=1))

    def _handle(self, source: Observable[_T]) -> Observable[_T]:
        def subscribe(
            observer: abc.ObserverBase[_T],
            scheduler: Optional[abc.SchedulerBase] = None,
        ) -> abc.DisposableBase:
            def on_error(exc: Exception) -> None:
                self._submit_exception(exc)
                try:
                    raise exc
                except OSError as e:
                    logger.critical('{}\n{}', repr(exc), format_exception(exc))
                    if e.errno == errno.ENOSPC:
                        # 磁盘已满时继续重试只会产生错误风暴，因此结束当前录制流。
                        observer.on_completed()
                    else:
                        observer.on_error(exc)
                except LiveRoomHidden:
                    logger.error('The live room has been hidden!')
                    observer.on_completed()
                except LiveRoomLocked:
                    logger.error('The live room has been locked!')
                    observer.on_completed()
                except LiveRoomEncrypted:
                    logger.error('The live room has been encrypted!')
                    observer.on_completed()
                except Exception:
                    observer.on_error(exc)

            return source.subscribe(
                observer.on_next, on_error, observer.on_completed, scheduler=scheduler
            )

        return Observable(subscribe)
