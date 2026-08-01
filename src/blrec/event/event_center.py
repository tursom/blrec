"""向 WebSocket、通知和 Webhook 广播业务事件的进程级 Rx 总线。"""

from reactivex import Observable, Subject

from ..utils.patterns import Singleton
from .typing import Event

__all__ = ('EventCenter',)


class EventCenter(Singleton):
    """Subject 不缓存历史事件，新订阅者只接收订阅后的事件。"""

    def __init__(self) -> None:
        super().__init__()
        self._source: Subject[Event] = Subject()

    @property
    def events(self) -> Observable[Event]:
        return self._source

    def submit(self, event: Event) -> None:
        self._source.on_next(event)
