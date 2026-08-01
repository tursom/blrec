import { Injectable } from '@angular/core';

import { StorageService } from 'src/app/core/services/storage.service';

export interface TaskSettings {
  showInfoPanel?: boolean;
}

@Injectable({
  providedIn: 'root',
})
/** 保存仅属于当前浏览器的任务卡片展示偏好，不会写入后端任务设置。 */
export class TaskSettingsService {
  constructor(private storage: StorageService) {}

  getSettings(roomId: number): TaskSettings {
    const settingsString = this.storage.getData(this.getStorageKey(roomId));
    if (settingsString) {
      return JSON.parse(settingsString) ?? {};
    } else {
      return {};
    }
  }

  updateSettings(roomId: number, settings: TaskSettings): void {
    // 合并局部更新，避免切换一个展示项时覆盖同房间的其他本地偏好。
    settings = Object.assign(this.getSettings(roomId), settings);
    const settingsString = JSON.stringify(settings);
    this.storage.setData(this.getStorageKey(roomId), settingsString);
  }

  private getStorageKey(roomId: number): string {
    return `app-tasks-${roomId}`;
  }
}
