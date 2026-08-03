import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnChanges,
  OnInit,
} from '@angular/core';
import { HttpResponse } from '@angular/common/http';
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
  HttpIncidentSummary,
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
  readonly incidentKindLabels: Record<string, string> = {
    hls_init_unstable: '初始化段持续变化',
    hls_segment_corrupted: '媒体分片损坏',
    hls_playlist_fetch_failed: '播放列表获取失败',
    hls_playlist_parse_failed: '播放列表解析失败',
    hls_playlist_stalled: '播放列表停滞',
    hls_video_stream_missing: '未发现视频流',
    hls_video_dimensions_missing: '视频尺寸缺失',
    hls_stream_probe_failed: '媒体探测失败',
    hls_init_profile_incomplete: '初始化段信息不完整',
    hls_init_incompatible: '初始化段不兼容',
    hls_remux_failed: 'HLS 转封装失败',
    hls_crash_recovered: '恢复崩溃残留',
  };

  syncStatus!: SyncStatus<HttpHistorySettings>;
  historyStatus: HttpHistoryStatus | null = null;
  incidents: HttpIncidentSummary[] = [];
  incidentError: string | null = null;
  selectedRoomId: number | null = null;
  selectedRange: Date[] = this.defaultRange();
  loading = false;
  downloading = false;
  incidentsLoading = false;
  incidentDownloadingId: string | null = null;

  constructor(
    formBuilder: FormBuilder,
    private changeDetector: ChangeDetectorRef,
    private historyService: HttpHistoryService,
    private message: NzMessageService,
    private modal: NzModalService,
    private settingsSyncService: SettingsSyncService,
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
        this.settingsForm.valueChanges as Observable<HttpHistorySettings>,
      )
      .subscribe((detail) => {
        this.syncStatus = { ...this.syncStatus, ...calcSyncStatus(detail) };
        this.changeDetector.markForCheck();
      });
    this.refreshStatus();
    this.refreshIncidents();
  }

  refreshStatus(): void {
    this.loading = true;
    this.historyService
      .getStatus()
      .pipe(
        finalize(() => {
          this.loading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (status) => (this.historyStatus = status),
        error: (error) =>
          this.message.error(`读取请求历史状态失败: ${error.message}`),
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
        }),
      )
      .subscribe({
        next: (response) => {
          this.saveResponse(response, 'blrec-http-history.zip');
        },
        error: (error) =>
          this.message.error(`下载请求历史失败: ${error.message}`),
      });
  }

  refreshIncidents(): void {
    const [since, until] = this.selectedRange || [];
    this.incidentsLoading = true;
    this.incidentError = null;
    this.historyService
      .listIncidents({
        roomId: this.selectedRoomId ?? undefined,
        since: since?.toISOString(),
        until: until?.toISOString(),
      })
      .pipe(
        finalize(() => {
          this.incidentsLoading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (incidents) => (this.incidents = incidents),
        error: (error) => {
          this.incidentError = error.message;
          this.message.error(`读取错误现场失败: ${error.message}`);
        },
      });
  }

  confirmIncidentDownload(incidentId: string): void {
    this.modal.confirm({
      nzTitle: '下载错误现场包？',
      nzContent: '现场包可能包含直播音视频，请仅向可信的开发人员提供。',
      nzOkText: '下载',
      nzCancelText: '取消',
      nzOnOk: () => this.downloadIncident(incidentId),
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
              this.refreshIncidents();
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

  private downloadIncident(incidentId: string): Promise<void> {
    this.incidentDownloadingId = incidentId;
    this.changeDetector.markForCheck();
    return new Promise<void>((resolve, reject) => {
      this.historyService
        .exportIncident(incidentId)
        .pipe(
          finalize(() => {
            this.incidentDownloadingId = null;
            this.changeDetector.markForCheck();
          }),
        )
        .subscribe({
          next: (response) => {
            this.saveResponse(
              response,
              `blrec-http-incident-${incidentId}.zip`,
            );
            resolve();
          },
          error: (error) => {
            this.message.error(`下载错误现场失败: ${error.message}`);
            reject(error);
          },
        });
    });
  }

  private saveResponse(
    response: HttpResponse<Blob>,
    fallbackFilename: string,
  ): void {
    if (!response.body) {
      return;
    }
    const objectUrl = URL.createObjectURL(response.body);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = this.filenameFromHeader(
      response.headers.get('content-disposition'),
      fallbackFilename,
    );
    anchor.click();
    URL.revokeObjectURL(objectUrl);
  }

  private filenameFromHeader(
    contentDisposition: string | null,
    fallbackFilename: string,
  ): string {
    const match = contentDisposition?.match(/filename="?([^";]+)"?/i);
    return match?.[1] || fallbackFilename;
  }
}
