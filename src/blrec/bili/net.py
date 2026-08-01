"""B 站 HTTP 客户端共享的地址族和连接池配置。"""

import os
import socket

import aiohttp
import requests

__all__ = ('create_connector', 'timeout')

USE_IPV4_ONLY = bool(os.environ.get('BLREC_IPV4'))

if not USE_IPV4_ONLY:
    family = 0
else:
    # requests 与 aiohttp 各自维护地址族选择，两处都需禁用 IPv6。
    requests.packages.urllib3.util.connection.HAS_IPV6 = False  # type: ignore
    family = socket.AF_INET

timeout = aiohttp.ClientTimeout(total=10)


def create_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(family=family, limit=200)
