import {
  Component,
  OnInit,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  OnDestroy,
} from '@angular/core';
import { ActivatedRoute, ParamMap, Router } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';

import { interval, of, Subscription, zip } from 'rxjs';
import { catchError, concatAll, switchMap } from 'rxjs/operators';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { retry } from 'src/app/shared/rx-operators';
import { TaskService } from '../shared/services/task.service';
import {
  TaskData,
  DanmakuFileDetail,
  VideoFileDetail,
} from '../shared/task.model';

@Component({
  selector: 'app-task-detail',
  templateUrl: './task-detail.component.html',
  styleUrls: ['./task-detail.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
/** 同步单个任务及其视频、弹幕文件快照，供详情页各子面板共享。 */
export class TaskDetailComponent implements OnInit, OnDestroy {
  roomId!: number;
  taskData!: TaskData;
  videoFileDetails: VideoFileDetail[] = [];
  danmakuFileDetails: DanmakuFileDetail[] = [];

  loading: boolean = true;
  private dataSubscription?: Subscription;

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private changeDetector: ChangeDetectorRef,
    private notification: NzNotificationService,
    private taskService: TaskService
  ) {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        this.syncData();
      } else {
        this.desyncData();
      }
    });
  }

  ngOnInit(): void {
    this.route.paramMap.subscribe((params: ParamMap) => {
      this.roomId = parseInt(params.get('id')!);
      this.syncData();
    });
  }

  ngOnDestroy(): void {
    this.desyncData();
  }

  private syncData(): void {
    // 三个接口组成同一轮快照；任一失败都会使该轮整体失败，避免展示不同步的数据。
    this.dataSubscription = of(of(0), interval(1000))
      .pipe(
        concatAll(),
        switchMap(() =>
          zip(
            this.taskService.getTaskData(this.roomId),
            this.taskService.getVideoFileDetails(this.roomId),
            this.taskService.getDanmakuFileDetails(this.roomId)
          )
        ),
        catchError((error: HttpErrorResponse) => {
          this.notification.error('获取任务数据出错', error.message);
          throw error;
        }),
        retry(10, 3000)
      )
      .subscribe(
        ([taskData, videoFileDetails, danmakuFileDetails]) => {
          this.loading = false;
          this.taskData = taskData;
          this.videoFileDetails = videoFileDetails;
          this.danmakuFileDetails = danmakuFileDetails;
          this.changeDetector.markForCheck();
        },
        (error: HttpErrorResponse) => {
          this.notification.error(
            '获取任务数据出错',
            '网络连接异常, 请待网络正常后刷新。',
            { nzDuration: 0 }
          );
        }
      );
  }

  private desyncData(): void {
    // 退到后台或销毁页面时，同时停止轮询和当前尚未完成的请求。
    this.dataSubscription?.unsubscribe();
  }
}
