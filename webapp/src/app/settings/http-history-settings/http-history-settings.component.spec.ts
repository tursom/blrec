import { HttpHeaders, HttpResponse } from '@angular/common/http';
import { ChangeDetectorRef } from '@angular/core';
import { FormBuilder } from '@angular/forms';

import { EMPTY, of, throwError } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';

import { HttpHistoryService } from '../shared/services/http-history.service';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { HttpHistorySettingsComponent } from './http-history-settings.component';

describe('HttpHistorySettingsComponent', () => {
  let component: HttpHistorySettingsComponent;
  let historyService: jasmine.SpyObj<HttpHistoryService>;
  let message: jasmine.SpyObj<NzMessageService>;
  let modal: jasmine.SpyObj<NzModalService>;
  let settingsSync: jasmine.SpyObj<SettingsSyncService>;

  beforeEach(() => {
    historyService = jasmine.createSpyObj('HttpHistoryService', [
      'getStatus',
      'exportHistory',
      'clear',
    ]);
    historyService.getStatus.and.returnValue(
      of({
        enabled: true,
        record_count: 1,
        total_size: 100,
        oldest_at: '2026-08-01T00:00:00Z',
        newest_at: '2026-08-02T00:00:00Z',
        room_ids: [42],
        dropped_records: 0,
        last_error: null,
      })
    );
    message = jasmine.createSpyObj('NzMessageService', [
      'error',
      'success',
    ]);
    modal = jasmine.createSpyObj('NzModalService', ['confirm']);
    settingsSync = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings']
    );
    settingsSync.syncSettings.and.returnValue(EMPTY);

    component = new HttpHistorySettingsComponent(
      new FormBuilder(),
      jasmine.createSpyObj<ChangeDetectorRef>('ChangeDetectorRef', [
        'markForCheck',
      ]),
      historyService,
      message,
      modal,
      settingsSync
    );
    component.settings = {
      enabled: true,
      retentionDays: 7,
      maxSize: 100 * 1024 ** 2,
    };
    component.ngOnChanges();
    component.ngOnInit();
  });

  it('starts with a recent 24 hour export range', () => {
    expect(
      component.selectedRange[1].getTime() -
        component.selectedRange[0].getTime()
    ).toBe(24 * 60 * 60 * 1000);
  });

  it('loads settings into the form and starts settings synchronization', () => {
    expect(component.settingsForm.value).toEqual(component.settings);
    expect(settingsSync.syncSettings).toHaveBeenCalledWith(
      'httpHistory',
      component.settings,
      jasmine.anything()
    );
  });

  it('downloads the server filename and releases the object URL', () => {
    const blob = new Blob(['zip']);
    historyService.exportHistory.and.returnValue(
      of(
        new HttpResponse({
          body: blob,
          headers: new HttpHeaders({
            'content-disposition': 'attachment; filename="history.zip"',
          }),
        })
      )
    );
    component.selectedRoomId = 42;
    const anchor = document.createElement('a');
    spyOn(document, 'createElement').and.returnValue(anchor);
    spyOn(anchor, 'click');
    spyOn(URL, 'createObjectURL').and.returnValue('blob:history');
    spyOn(URL, 'revokeObjectURL');

    component.downloadHistory();

    expect(historyService.exportHistory).toHaveBeenCalledWith(
      jasmine.objectContaining({ roomId: 42 })
    );
    expect(anchor.download).toBe('history.zip');
    expect(anchor.click).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:history');
  });

  it('shows a message when download fails', () => {
    historyService.exportHistory.and.returnValue(
      throwError(() => new Error('export failed'))
    );

    component.downloadHistory();

    expect(message.error).toHaveBeenCalledWith(
      '下载请求历史失败: export failed'
    );
  });

  it('does not clear history until confirmation is accepted', () => {
    component.confirmClear();

    expect(historyService.clear).not.toHaveBeenCalled();
  });

  it('clears history after confirmation', async () => {
    historyService.clear.and.returnValue(of({ code: 0, message: 'ok' }));
    component.confirmClear();
    const options = modal.confirm.calls.mostRecent().args[0];
    expect(options).toBeDefined();
    const onOk = options!.nzOnOk as () => Promise<void>;

    await onOk();

    expect(historyService.clear).toHaveBeenCalled();
    expect(message.success).toHaveBeenCalledWith('请求历史已清空');
  });

  it('shows a message when clearing fails', async () => {
    historyService.clear.and.returnValue(
      throwError(() => new Error('clear failed'))
    );
    component.confirmClear();
    const options = modal.confirm.calls.mostRecent().args[0];
    const onOk = options!.nzOnOk as () => Promise<void>;

    await expectAsync(onOk()).toBeRejected();

    expect(message.error).toHaveBeenCalledWith(
      '清空请求历史失败: clear failed'
    );
  });
});
