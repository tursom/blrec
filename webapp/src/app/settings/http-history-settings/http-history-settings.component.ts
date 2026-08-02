import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnChanges,
  OnInit,
} from '@angular/core';
import { FormBuilder, FormControl, FormGroup } from '@angular/forms';

import mapValues from 'lodash-es/mapValues';
import { Observable } from 'rxjs';
import { finalize } from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';

import { SYNC_FAILED_WARNING_TIP } from '../shared/constants/form';
import { HttpHistorySettings } from '../shared/setting.model';
import {
  HttpHistoryService,
  HttpHistoryStatus,
} from '../shared/services/http-history.service';
import {
  calcSyncStatus,
  SettingsSyncService,
  SyncStatus,
} from '../shared/services/settings-sync.service';

@Component({
  selector: 'app-http-history-settings',
  templateUrl: './http-history-settings.component.html',
  styleUrls: ['./http-history-settings.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class HttpHistorySettingsComponent implements OnInit, OnChanges {
  @Input() settings!: HttpHistorySettings;

  readonly settingsForm: FormGroup;
  readonly syncFailedWarningTip = SYNC_FAILED_WARNING_TIP;
  readonly retentionOptions = [1, 3, 7, 14, 30, 90].map((days) => ({
    label: `${days} 天`,
    value: days,
  }));
  readonly sizeOptions = [10, 50, 100, 250, 500, 1024].map((mib) => ({
    label: `${mib} MiB`,
    value: mib * 1024 ** 2,
  }));

  syncStatus!: SyncStatus<HttpHistorySettings>;
  historyStatus: HttpHistoryStatus | null = null;
  selectedRoomId: number | null = null;
  selectedRange: Date[] = this.defaultRange();
  loading = false;
  downloading = false;

  constructor(
    formBuilder: FormBuilder,
    private changeDetector: ChangeDetectorRef,
    private historyService: HttpHistoryService,
    private message: NzMessageService,
    private modal: NzModalService,
    private settingsSyncService: SettingsSyncService
  ) {
    this.settingsForm = formBuilder.group({
      enabled: [''],
      retentionDays: [''],
      maxSize: [''],
    });
  }

  get enabledControl(): FormControl {
    return this.settingsForm.get('enabled') as FormControl;
  }

  get retentionDaysControl(): FormControl {
    return this.settingsForm.get('retentionDays') as FormControl;
  }

  get maxSizeControl(): FormControl {
    return this.settingsForm.get('maxSize') as FormControl;
  }

  ngOnChanges(): void {
    this.syncStatus = mapValues(this.settings, () => true);
    this.settingsForm.setValue(this.settings);
  }

  ngOnInit(): void {
    this.settingsSyncService
      .syncSettings(
        'httpHistory',
        this.settings,
        this.settingsForm.valueChanges as Observable<HttpHistorySettings>
      )
      .subscribe((detail) => {
        this.syncStatus = { ...this.syncStatus, ...calcSyncStatus(detail) };
        this.changeDetector.markForCheck();
      });
    this.refreshStatus();
  }

  refreshStatus(): void {
    this.loading = true;
    this.historyService
      .getStatus()
      .pipe(
        finalize(() => {
          this.loading = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: (status) => (this.historyStatus = status),
        error: (error) => this.message.error(`读取请求历史状态失败: ${error.message}`),
      });
  }

  downloadHistory(): void {
    const [since, until] = this.selectedRange || [];
    this.downloading = true;
    this.historyService
      .exportHistory({
        roomId: this.selectedRoomId ?? undefined,
        since: since?.toISOString(),
        until: until?.toISOString(),
      })
      .pipe(
        finalize(() => {
          this.downloading = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: (response) => {
          if (!response.body) {
            return;
          }
          const objectUrl = URL.createObjectURL(response.body);
          const anchor = document.createElement('a');
          anchor.href = objectUrl;
          anchor.download = this.filenameFromHeader(
            response.headers.get('content-disposition')
          );
          anchor.click();
          URL.revokeObjectURL(objectUrl);
        },
        error: (error) => this.message.error(`下载请求历史失败: ${error.message}`),
      });
  }

  confirmClear(): void {
    this.modal.confirm({
      nzTitle: '清空请求历史？',
      nzContent: '已保存的请求记录将被永久删除。',
      nzOkDanger: true,
      nzOkText: '清空',
      nzCancelText: '取消',
      nzOnOk: () =>
        new Promise<void>((resolve, reject) => {
          this.historyService.clear().subscribe({
            next: () => {
              this.message.success('请求历史已清空');
              this.refreshStatus();
              resolve();
            },
            error: (error) => {
              this.message.error(`清空请求历史失败: ${error.message}`);
              reject(error);
            },
          });
        }),
    });
  }

  private defaultRange(): Date[] {
    const until = new Date();
    return [new Date(until.getTime() - 24 * 60 * 60 * 1000), until];
  }

  private filenameFromHeader(contentDisposition: string | null): string {
    const match = contentDisposition?.match(/filename="?([^";]+)"?/i);
    return match?.[1] || 'blrec-http-history.zip';
  }
}
