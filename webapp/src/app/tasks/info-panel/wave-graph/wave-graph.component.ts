import {
  Component,
  OnInit,
  ChangeDetectionStrategy,
  Input,
  ChangeDetectorRef,
  OnDestroy,
} from '@angular/core';

import { interval, Subscription } from 'rxjs';

interface Point {
  x: number;
  y: number;
}

@Component({
  selector: 'app-wave-graph',
  templateUrl: './wave-graph.component.svg',
  styleUrls: ['./wave-graph.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
/** 将最近一段时间的采样值归一化为固定尺寸的 SVG 折线。 */
export class WaveGraphComponent implements OnInit, OnDestroy {
  @Input() value: number = 0;
  @Input() width: number = 200;
  @Input() height: number = 16;
  @Input() stroke: string = 'white';

  private data: number[] = [];
  private points: Point[] = [];
  private subscription?: Subscription;

  constructor(private changeDetector: ChangeDetectorRef) {
    // 横坐标步长固定为 2 px，因此数组长度同时决定图表保留的历史窗口。
    for (let x = 0; x <= this.width; x += 2) {
      this.data.push(0);
      this.points.push({ x: x, y: this.height });
    }
  }

  get polylinePoints(): string {
    return this.points.map((p) => `${p.x},${p.y}`).join(' ');
  }

  ngOnInit(): void {
    this.subscription = interval(1000).subscribe(() => {
      this.data.push(this.value || 0);
      this.data.shift();

      let maximum = Math.max(...this.data);
      // 每个窗口按自身峰值缩放；全零窗口用 1 作分母以保持坐标有限。
      this.points = this.data.map((value, index) => ({
        x: Math.min(index * 2, this.width),
        y: (1 - value / (maximum || 1)) * this.height,
      }));

      this.changeDetector.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.subscription?.unsubscribe();
  }
}
