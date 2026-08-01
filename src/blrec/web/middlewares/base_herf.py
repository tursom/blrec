"""在反向代理子路径部署时，动态改写 Angular 入口页的 base href。"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BaseHrefMiddleware:
    """仅拦截入口页响应，并在完整响应体到达后更新长度与 base 标签。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope['type'] != 'http'
            or scope.get('method', '') != 'GET'
            or scope.get('path', '') != '/'
            or scope.get('root_path', '') == ''
        ):
            await self._app(scope, receive, send)
            return

        # ASGI 把响应头和响应体分开发送；先暂存头部，待改写 body 后再一起下发。
        initial_message: Message = {}

        async def _send(msg: Message) -> None:
            nonlocal initial_message
            msg_type = msg['type']
            if msg_type == 'http.response.start':
                headers = Headers(raw=msg['headers'])
                # BrotliMiddleware 必须位于本中间件外层，否则无法直接替换 HTML 字节。
                assert 'content-encoding' not in headers
                initial_message = msg
            elif msg_type == 'http.response.body':
                body = msg.get('body', b'')
                # Angular 入口是一个非空、非流式响应；违反此约束说明中间件顺序改变。
                assert body != b''
                more_body = msg.get('more_body', False)
                assert more_body is False
                # CLI 已把 root_path 规范成带首尾斜杠的形式。
                root_path = scope.get('root_path', '') or '/'
                body = body.replace(
                    b'<base href="/">', f'<base href="{root_path}">'.encode(), 1
                )
                msg['body'] = body
                # body 长度改变后必须同步 Content-Length，否则客户端可能截断响应。
                headers = MutableHeaders(raw=initial_message['headers'])
                headers['Content-Length'] = str(len(body))
                await send(initial_message)
                await send(msg)
                # 释放暂存消息，避免错误地复用于后续 ASGI 消息。
                del initial_message

        await self._app(scope, receive, _send)
