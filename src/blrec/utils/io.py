"""通过共享线程池为任意阻塞函数提供调用超时。"""

import atexit
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _TimeoutError
from typing import Callable, Any, Iterable, Mapping, TypeVar


_T = TypeVar('_T')


_executor = None


def wait_for(
    func: Callable[..., _T],
    *,
    args: Iterable[Any] = [],
    kwargs: Mapping[str, Any] = {},
    timeout: float
) -> _T:
    global _executor
    if _executor is None:
        # 延迟创建并在进程退出时回收，避免导入模块就生成大量线程。
        _executor = ThreadPoolExecutor(max_workers=200, thread_name_prefix='wait_for')
        atexit.register(_executor.shutdown)

    future = _executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except _TimeoutError:
        raise TimeoutError(timeout, func, args, kwargs) from None
