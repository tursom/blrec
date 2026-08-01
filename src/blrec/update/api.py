"""访问 PyPI 项目/版本元数据，并区分资源不存在与临时网络失败。"""

from http import HTTPStatus
from typing import Any, Final, Optional

import aiohttp
from tenacity import (
    retry,
    wait_exponential,
    stop_after_delay,
)

from .typing import JsonResponse, Metadata


__all__ = 'PypiApi',


class PypiApi:
    """复用应用 aiohttp 会话查询 PyPI JSON API。"""

    BASE_URL: Final[str] = 'https://pypi.org/pypi'

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    @classmethod
    def _make_url(cls, path: str) -> str:
        return cls.BASE_URL + path

    @retry(
        reraise=True,
        # 更新检查不应长期阻塞启动流程：在 5 秒预算内指数退避后抛出最后异常。
        stop=stop_after_delay(5),
        wait=wait_exponential(0.1),
    )
    async def _get(self, *args: Any, **kwds: Any) -> Optional[JsonResponse]:
        try:
            async with self._session.get(
                *args, raise_for_status=True, timeout=10, **kwds
            ) as res:
                return await res.json()
        except aiohttp.ClientResponseError as e:
            if e.status == HTTPStatus.NOT_FOUND:
                # 404 是“项目/版本不存在”的正常查询结果，不参与瞬时错误重试。
                return None
            else:
                raise

    async def get_project_metadata(
        self, project_name: str
    ) -> Optional[Metadata]:
        url = self._make_url(f'/{project_name}/json')
        return await self._get(url)

    async def get_release_metadata(
        self, project_name: str, version: str
    ) -> Optional[Metadata]:
        url = self._make_url(f'/{project_name}/{version}/json')
        return await self._get(url)
