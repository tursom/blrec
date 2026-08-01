"""组件内部的强类型监听器列表和顺序异步事件分发。"""

from __future__ import annotations

from abc import ABC
from contextlib import suppress
from typing import Any, Generic, List, TypeVar

from ..exception import ExceptionSubmitter

__all__ = 'EventListener', 'EventEmitter'


class EventListener(ABC):
    ...


_T = TypeVar('_T', bound=EventListener)


class EventEmitter(Generic[_T]):
    """按注册顺序等待每个 listener，并隔离单个 listener 的异常。"""

    def __init__(self) -> None:
        super().__init__()
        self._listeners: List[_T] = []

    def add_listener(self, listener: _T) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: _T) -> None:
        with suppress(ValueError):
            self._listeners.remove(listener)

    # 事件名通过 on_<name> 动态映射回调；当前 Python 类型系统无法表达这种模板
    # 字面量约束，因此由各 EventListener 接口和调用点共同保证名称正确。
    # 相关讨论：https://github.com/python/typing/issues/685
    async def _emit(self, name: str, *args: Any, **kwds: Any) -> None:
        # 不并发调用 listener，确保文件 created/completed 等事件保持因果顺序。
        for listener in self._listeners:
            with ExceptionSubmitter():
                coro = getattr(listener, 'on_' + name)
                await coro(*args, **kwds)
