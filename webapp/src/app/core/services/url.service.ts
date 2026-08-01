import { Injectable } from '@angular/core';
import { Location } from '@angular/common';

import { environment } from 'src/environments/environment';

const API_BASE_URL = environment.apiBaseUrl;
const WEB_SOCKET_BASE_URL = environment.webSocketBaseUrl;

@Injectable({
  providedIn: 'root',
})
/** 在部署前缀下构造 HTTP 与 WebSocket 地址。 */
export class UrlService {
  constructor(private location: Location) {}

  makeApiUrl(uri: string): string {
    // prepareExternalUrl 会带上 Angular 的 base href，支持部署在站点子路径。
    return API_BASE_URL + this.location.prepareExternalUrl(uri);
  }

  makeWebSocketUrl(uri: string): string {
    // WebSocket 与 HTTP 使用相同的 base href，但协议/主机前缀由环境配置独立提供。
    return WEB_SOCKET_BASE_URL + this.location.prepareExternalUrl(uri);
  }
}
