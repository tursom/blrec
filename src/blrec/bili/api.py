"""B 站 Web/App HTTP API 适配、签名和多域名容错。"""

import asyncio
import hashlib
import time
import uuid
from abc import ABC
from datetime import datetime
from typing import Any, Dict, Final, List, Mapping, Optional
from urllib.parse import urlencode

import aiohttp
from loguru import logger
from tenacity import retry, stop_after_delay, wait_exponential

from ..http_history import HttpHistoryStore, record_http_exchange
from . import wbi
from .exceptions import ApiRequestError
from .typing import JsonResponse, QualityNumber, ResponseData

__all__ = 'AppApi', 'WebApi'


BASE_HEADERS: Final = {
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en;q=0.3,en-US;q=0.2',  # noqa
    'Accept': 'application/json, text/plain, */*',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'Origin': 'https://live.bilibili.com',
    'Pragma': 'no-cache',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',  # noqa
}


class BaseApi(ABC):
    """共享会话与响应校验，并为备用域名提供串行/并行请求策略。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        headers: Optional[Dict[str, str]] = None,
        *,
        room_id: Optional[int] = None,
        http_history: Optional[HttpHistoryStore] = None,
    ):
        self._logger = logger.bind(room_id=room_id or '')
        self._room_id = room_id
        self._http_history = http_history

        self.base_api_urls: List[str] = ['https://api.bilibili.com']
        self.base_live_api_urls: List[str] = ['https://api.live.bilibili.com']
        self.base_play_info_api_urls: List[str] = ['https://api.live.bilibili.com']

        self._session = session
        self.headers = headers or {}
        self.timeout = 10

    @property
    def headers(self) -> Dict[str, str]:
        return self._headers

    @headers.setter
    def headers(self, value: Dict[str, str]) -> None:
        self._headers = {**BASE_HEADERS, **value}

    @staticmethod
    def _check_response(json_res: JsonResponse) -> None:
        if json_res['code'] != 0:
            raise ApiRequestError(
                json_res['code'], json_res.get('message') or json_res.get('msg') or ''
            )

    @retry(reraise=True, stop=stop_after_delay(5), wait=wait_exponential(0.1))
    async def _get_json_res(self, *args: Any, **kwds: Any) -> JsonResponse:
        should_check_response = kwds.pop('check_response', True)
        operation_id = kwds.pop('_history_operation_id', None)
        parent_operation_id = kwds.pop('_history_parent_operation_id', None)
        kwds = {
            'timeout': self.timeout,
            'headers': self.headers,
            'raise_for_status': False,
            **kwds,
        }
        started_at = time.perf_counter()
        requested_url = str(args[0]) if args else str(kwds.get('url', ''))
        response_status = None
        response_headers: Mapping[str, Any] = {}
        response_body: Any = None
        request_headers: Mapping[str, Any] = self.headers
        error: Optional[BaseException] = None
        try:
            async with self._session.get(*args, **kwds) as res:
                requested_url = str(res.request_info.real_url)
                request_headers = res.request_info.headers
                response_status = res.status
                response_headers = res.headers
                text_res = await res.text()
                response_body = text_res
                self._logger.trace('Request: {}', res.request_info)
                self._logger.trace('Response: {}', text_res)
                res.raise_for_status()
                json_res = await res.json()
                response_body = json_res
                if should_check_response:
                    self._check_response(json_res)
        except aiohttp.ContentTypeError as exc:
            response_body = text_res
            self._logger.debug(f'Response text: {text_res[:200]}')
            error = exc
            raise
        except Exception as exc:
            error = exc
            raise
        else:
            return json_res
        finally:
            record_http_exchange(
                self._http_history,
                room_id=self._room_id,
                category='api',
                method='GET',
                url=requested_url,
                request_headers=request_headers,
                response_status=response_status,
                response_headers=response_headers,
                response_body=response_body,
                error=error,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                operation_id=operation_id,
                parent_operation_id=parent_operation_id,
            )

    async def _get_json(
        self, base_urls: List[str], path: str, *args: Any, **kwds: Any
    ) -> JsonResponse:
        if not base_urls:
            raise ValueError('No base urls')
        parent_operation_id = uuid.uuid4().hex
        # 普通接口按配置顺序尝试域名，成功后立即返回，避免重复请求。
        exception = None
        for base_url in base_urls:
            url = base_url + path
            try:
                return await self._get_json_res(
                    url,
                    *args,
                    **kwds,
                    _history_operation_id=uuid.uuid4().hex,
                    _history_parent_operation_id=parent_operation_id,
                )
            except Exception as exc:
                exception = exc
                self._logger.trace('Failed to get json from {}: {}', url, repr(exc))
        else:
            assert exception is not None
            raise exception

    async def _get_jsons_concurrently(
        self, base_urls: List[str], path: str, *args: Any, **kwds: Any
    ) -> List[JsonResponse]:
        if not base_urls:
            raise ValueError('No base urls')
        parent_operation_id = uuid.uuid4().hex
        # 播放信息需要汇总多个域名的 CDN 结果，因此并发请求并保留全部成功响应。
        urls = [base_url + path for base_url in base_urls]
        aws = (
            self._get_json_res(
                url,
                *args,
                **kwds,
                _history_operation_id=uuid.uuid4().hex,
                _history_parent_operation_id=parent_operation_id,
            )
            for url in urls
        )
        results = await asyncio.gather(*aws, return_exceptions=True)
        exceptions = []
        json_responses = []
        for idx, item in enumerate(results):
            if isinstance(item, Exception):
                self._logger.trace(
                    'Failed to get json from {}: {}', urls[idx], repr(item)
                )
                exceptions.append(item)
            elif isinstance(item, dict):
                json_responses.append(item)
            else:
                self._logger.trace('{}', repr(item))
        if not json_responses:
            raise exceptions[0]
        return json_responses


class AppApi(BaseApi):
    # App 签名规则来源：
    # https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/other/API_sign.md  # noqa
    _appkey = '1d8b6e7d45233436'
    _appsec = '560c52ccd288fed045859ed18bffd973'

    _app_headers = {
        'User-Agent': 'Mozilla/5.0 BiliDroid/6.64.0 (bbcallen@gmail.com) os/android model/Unknown mobi_app/android build/6640400 channel/bili innerVer/6640400 osVer/6.0.1 network/2',  # noqa
        'Connection': 'Keep-Alive',
        'Accept-Encoding': 'gzip',
    }

    @property
    def headers(self) -> Dict[str, str]:
        return self._headers

    @headers.setter
    def headers(self, value: Dict[str, str]) -> None:
        self._headers = {**value, **self._app_headers}

    @classmethod
    def signed(cls, params: Mapping[str, Any]) -> Dict[str, Any]:
        if isinstance(params, Mapping):
            params = dict(sorted({**params, 'appkey': cls._appkey}.items()))
        else:
            raise ValueError(type(params))
        query = urlencode(params, doseq=True)
        sign = hashlib.md5((query + cls._appsec).encode()).hexdigest()
        params.update(sign=sign)
        return params

    async def get_room_play_infos(
        self,
        room_id: int,
        qn: QualityNumber = 10000,
        *,
        only_video: bool = False,
        only_audio: bool = False,
    ) -> List[ResponseData]:
        path = '/xlive/app-room/v2/index/getRoomPlayInfo'
        params = self.signed(
            {
                'actionKey': 'appkey',
                'build': '6640400',
                'channel': 'bili',
                'codec': '0,1',  # 0: avc, 1: hevc
                'device': 'android',
                'device_name': 'Unknown',
                'disable_rcmd': '0',
                'dolby': '1',
                'format': '0,1,2',  # 0: flv, 1: ts, 2: fmp4
                'free_type': '0',
                'http': '1',
                'mask': '0',
                'mobi_app': 'android',
                'need_hdr': '0',
                'no_playurl': '0',
                'only_audio': '1' if only_audio else '0',
                'only_video': '1' if only_video else '0',
                'platform': 'android',
                'play_type': '0',
                'protocol': '0,1',
                'qn': qn,
                'room_id': room_id,
                'ts': int(datetime.utcnow().timestamp()),
            }
        )
        json_responses = await self._get_jsons_concurrently(
            self.base_play_info_api_urls, path, params=params
        )
        return [r['data'] for r in json_responses]

    async def get_info_by_room(self, room_id: int) -> ResponseData:
        path = '/xlive/app-room/v1/index/getInfoByRoom'
        params = self.signed(
            {
                'actionKey': 'appkey',
                'build': '6640400',
                'channel': 'bili',
                'device': 'android',
                'mobi_app': 'android',
                'platform': 'android',
                'room_id': room_id,
                'ts': int(datetime.utcnow().timestamp()),
            }
        )
        json_res = await self._get_json(self.base_live_api_urls, path, params=params)
        return json_res['data']

    async def get_user_info(self, uid: int) -> ResponseData:
        base_api_urls = ['https://app.bilibili.com']
        path = '/x/v2/space'
        params = self.signed(
            {
                'build': '6640400',
                'channel': 'bili',
                'mobi_app': 'android',
                'platform': 'android',
                'ts': int(datetime.utcnow().timestamp()),
                'vmid': uid,
            }
        )
        json_res = await self._get_json(base_api_urls, path, params=params)
        return json_res['data']

    async def get_danmu_info(self, room_id: int) -> ResponseData:
        path = '/xlive/app-room/v1/index/getDanmuInfo'
        params = self.signed(
            {
                'actionKey': 'appkey',
                'build': '6640400',
                'channel': 'bili',
                'device': 'android',
                'mobi_app': 'android',
                'platform': 'android',
                'room_id': room_id,
                'ts': int(datetime.utcnow().timestamp()),
            }
        )
        json_res = await self._get_json(self.base_live_api_urls, path, params=params)
        return json_res['data']


class WebApi(BaseApi):
    """支持 WBI 签名并在签名失效时限频刷新密钥的 Web API 客户端。"""

    _wbi_key = wbi.make_key(
        img_key="7cd084941338484aae1ad9425b84077c",
        sub_key="4932caff0ff746eab6f01bf08b70ac45",
    )
    _wbi_key_mtime = 0.0

    @retry(reraise=True, stop=stop_after_delay(20), wait=wait_exponential(0.1))
    async def _get_json_res(
        self, url: str, with_wbi: bool = False, *args: Any, **kwds: Any
    ) -> JsonResponse:
        if with_wbi:
            key = self.__class__._wbi_key
            ts = int(datetime.now().timestamp())
            params = list(kwds.pop("params").items())
            query = wbi.build_query(key, ts, params)
            url = f'{url}?{query}'

        try:
            return await super()._get_json_res(url, *args, **kwds)
        except ApiRequestError as e:
            # -352 通常表示 WBI 风控/签名失败；一分钟内只刷新一次共享密钥。
            if e.code == -352 and time.monotonic() - self.__class__._wbi_key_mtime > 60:
                await self._update_wbi_key()
            raise

    async def room_init(self, room_id: int) -> ResponseData:
        path = '/room/v1/Room/room_init'
        params = {'id': room_id}
        json_res = await self._get_json(self.base_live_api_urls, path, params=params)
        return json_res['data']

    async def get_room_play_infos(
        self, room_id: int, qn: QualityNumber = 10000
    ) -> List[ResponseData]:
        path = '/xlive/web-room/v2/index/getRoomPlayInfo'
        params = {
            'room_id': room_id,
            'protocol': '0,1',
            'format': '0,1,2',
            'codec': '0,1',
            'qn': qn,
            'platform': 'web',
            'ptype': 8,
        }
        json_responses = await self._get_jsons_concurrently(
            self.base_play_info_api_urls, path, with_wbi=True, params=params
        )
        return [r['data'] for r in json_responses]

    async def get_info_by_room(self, room_id: int) -> ResponseData:
        path = '/xlive/web-room/v1/index/getInfoByRoom'
        params = {'room_id': room_id}
        json_res = await self._get_json(
            self.base_live_api_urls, path, with_wbi=True, params=params
        )
        return json_res['data']

    async def get_info(self, room_id: int) -> ResponseData:
        path = '/room/v1/Room/get_info'
        params = {'room_id': room_id}
        json_res = await self._get_json(self.base_live_api_urls, path, params=params)
        return json_res['data']

    async def get_timestamp(self) -> int:
        path = '/av/v1/Time/getTimestamp'
        params = {'platform': 'pc'}
        json_res = await self._get_json(self.base_live_api_urls, path, params=params)
        return json_res['data']['timestamp']

    async def get_user_info(self, uid: int) -> ResponseData:
        path = '/x/space/wbi/acc/info'
        params = {'mid': uid}
        json_res = await self._get_json(
            self.base_api_urls, path, with_wbi=True, params=params
        )
        return json_res['data']

    async def get_danmu_info(self, room_id: int) -> ResponseData:
        path = '/xlive/web-room/v1/index/getDanmuInfo'
        params = {'id': room_id}
        json_res = await self._get_json(
            self.base_live_api_urls, path, with_wbi=True, params=params
        )
        return json_res['data']

    async def get_nav(self) -> ResponseData:
        path = '/x/web-interface/nav'
        json_res = await self._get_json(self.base_api_urls, path, check_response=False)
        return json_res

    async def _update_wbi_key(self) -> None:
        nav = await self.get_nav()
        img_key = wbi.extract_key(nav['data']['wbi_img']['img_url'])
        sub_key = wbi.extract_key(nav['data']['wbi_img']['sub_url'])
        self.__class__._wbi_key = wbi.make_key(img_key, sub_key)
        self.__class__._wbi_key_mtime = time.monotonic()
