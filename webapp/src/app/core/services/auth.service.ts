import { Injectable } from '@angular/core';
import { StorageService } from './storage.service';

const API_KEY_STORAGE_KEY = 'app-api-key';

@Injectable({
  providedIn: 'root',
})
/** 管理浏览器本地保存的 API Key，供所有 HTTP 请求共享。 */
export class AuthService {
  constructor(private storage: StorageService) {}

  hasApiKey(): boolean {
    return this.storage.hasData(API_KEY_STORAGE_KEY);
  }

  getApiKey(): string {
    // 未配置时仍返回空字符串，使拦截器始终可以生成结构一致的请求头。
    return this.storage.getData(API_KEY_STORAGE_KEY) ?? '';
  }

  setApiKey(value: string): void {
    this.storage.setData(API_KEY_STORAGE_KEY, value);
  }

  removeApiKey() {
    this.storage.removeData(API_KEY_STORAGE_KEY);
  }
}
