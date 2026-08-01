"""把进程内事件总线和异常总线桥接为浏览器 WebSocket。"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger
from websockets.exceptions import ConnectionClosed

from ...application import Application
from ...event import EventCenter
from ...event.typing import Event
from ...exception import ExceptionCenter, format_exception

logging.getLogger('websockets').setLevel(logging.WARNING)

app: Application = None  # type: ignore  # bypass flake8 F821

router = APIRouter(tags=['websockets'])


@router.websocket('/ws/v1/events')
async def receive_events(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.debug('Events websocket accepted')

    # 路由协程必须保持存活；future 只在连接关闭或发送失败时结束。
    future = asyncio.Future()  # type: ignore

    async def send_event(event: Event) -> None:
        try:
            text = json.dumps(event.asdict(), ensure_ascii=False)
            await websocket.send_text(text)
        except (WebSocketDisconnect, ConnectionClosed) as e:
            logger.debug(f'Events websocket closed: {repr(e)}')
            subscription.dispose()
            future.set_result(None)
        except Exception as e:
            logger.error(f'Error occurred on events websocket: {repr(e)}')
            subscription.dispose()
            future.set_exception(e)

    def on_event(event: Event) -> None:
        # Rx 回调是同步接口，实际 WebSocket I/O 交给当前事件循环中的任务。
        asyncio.create_task(send_event(event))

    # 每个连接独占一个订阅，并由 send_event 在断连时负责 dispose。
    subscription = EventCenter.get_instance().events.subscribe(on_event)

    await future


@router.websocket('/ws/v1/exceptions')
async def receive_exception(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.debug('Exceptions websocket accepted')

    # 与事件流分开连接，避免异常文本影响结构化业务事件的序列化契约。
    future = asyncio.Future()  # type: ignore

    async def send_exception(exc: BaseException) -> None:
        try:
            await websocket.send_text(format_exception(exc))
        except (WebSocketDisconnect, ConnectionClosed) as e:
            logger.debug(f'Exceptions websocket closed: {repr(e)}')
            subscription.dispose()
            future.set_result(None)
        except Exception as e:
            logger.error(f'Error occurred on exceptions websocket: {repr(e)}')
            subscription.dispose()
            future.set_exception(e)

    def on_exception(exc: BaseException) -> None:
        asyncio.create_task(send_exception(exc))

    subscription = ExceptionCenter.get_instance().exceptions.subscribe(on_exception)

    await future
