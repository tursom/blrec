
"""把上下文管理器和后台 Future 中的异常提交到 ExceptionCenter。"""

import asyncio

from .exception_center import ExceptionCenter


__all__ = (
    'ExceptionSubmitter',
    'submit_exception',
    'exception_callback',
)


class ExceptionSubmitter:
    """捕获 listener 异常并返回 True，避免一个消费者中断整个事件链。"""

    def __enter__(self):  # type: ignore
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):  # type: ignore
        if exc_val is not None:
            submit_exception(exc_val)
        return True


def submit_exception(exc: BaseException) -> None:
    ExceptionCenter.get_instance().submit(exc)


def exception_callback(future: asyncio.Future) -> None:  # type: ignore
    # CancelledError 属于生命周期控制，不作为业务异常上报。
    if not future.done() or future.cancelled():
        return
    if (exc := future.exception()):
        submit_exception(exc)
