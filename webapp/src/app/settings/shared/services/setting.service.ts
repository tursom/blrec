import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  Settings,
  TaskOptions,
  TaskOptionsIn,
  SettingsIn,
  SettingsOut,
} from '../setting.model';

@Injectable({
  providedIn: 'root',
})
export class SettingService {
  constructor(private http: HttpClient, private url: UrlService) {}

  getSettings(
    include: Array<keyof Settings> | null = null,
    exclude: Array<keyof Settings> | null = null
  ): Observable<Settings> {
    const url = this.url.makeApiUrl(`/api/v1/settings`);
    return this.http.get<Settings>(url, {
      params: {
        include: include ?? [],
        exclude: exclude ?? [],
      },
    });
  }

  /**
   * 修改应用全局设置。
   *
   * 修改输出目录会触发应用重启；修改网络请求头会重连全部弹幕客户端。
   * @param settings 仅包含待修改字段的局部设置
   * @returns 服务端确认后的应用设置
   */
  changeSettings(settings: SettingsIn): Observable<SettingsOut> {
    const url = this.url.makeApiUrl(`/api/v1/settings`);
    return this.http.patch<SettingsOut>(url, settings);
  }

  getTaskOptions(roomId: number): Observable<TaskOptions> {
    const url = this.url.makeApiUrl(`/api/v1/settings/tasks/${roomId}`);
    return this.http.get<TaskOptions>(url);
  }

  /**
   * 修改任务级设置。非 `null` 值覆盖对应全局设置，显式传入 `null` 则解除覆盖。
   *
   * 修改网络请求头会重连该任务的弹幕客户端。
   * @param roomId 任务的真实房间号
   * @param options 仅包含待修改字段的局部设置
   * @returns 服务端保存后的任务级覆盖值
   */
  changeTaskOptions(
    roomId: number,
    options: TaskOptionsIn
  ): Observable<TaskOptions> {
    const url = this.url.makeApiUrl(`/api/v1/settings/tasks/${roomId}`);
    return this.http.patch<TaskOptions>(url, options);
  }
}
