import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
} from '@angular/core';
import { Router, NavigationEnd, NavigationStart } from '@angular/router';
import { BreakpointObserver, Breakpoints } from '@angular/cdk/layout';

import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

@Component({
  selector: 'app-root',
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
/** 协调顶层路由加载状态与响应式侧栏形态。 */
export class AppComponent implements OnDestroy {
  title = 'B 站直播录制';
  theme: 'light' | 'dark' = 'light';

  loading = false;
  collapsed = false;
  useDrawer = false;
  destroyed = new Subject<void>();

  constructor(
    router: Router,
    changeDetector: ChangeDetectorRef,
    breakpointObserver: BreakpointObserver
  ) {
    router.events.subscribe((event) => {
      if (event instanceof NavigationStart) {
        this.loading = true;
        // close the drawer
        if (this.useDrawer) {
          this.collapsed = true;
        }
      } else if (event instanceof NavigationEnd) {
        this.loading = false;
      }
    });

    // 极窄屏改用抽屉导航，并通过 destroyed 统一释放媒体查询订阅。
    breakpointObserver
      .observe(Breakpoints.XSmall)
      .pipe(takeUntil(this.destroyed))
      .subscribe((state) => {
        this.useDrawer = state.matches;
        // ensure the drawer is closed
        if (this.useDrawer) {
          this.collapsed = true;
        }
        changeDetector.markForCheck();
      });

    // 为任务卡片保留双列空间：400px * 2 + 12px 间距 + 12px * 2 内边距 + 200px 侧栏。
    breakpointObserver
      .observe('(max-width: 1036px)')
      .pipe(takeUntil(this.destroyed))
      .subscribe((state) => {
        this.collapsed = state.matches;
        changeDetector.markForCheck();
      });
  }

  ngOnDestroy() {
    this.destroyed.next();
    this.destroyed.complete();
  }
}
