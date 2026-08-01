import { pipe, timer } from 'rxjs';
import { delayWhen, retryWhen } from 'rxjs/operators';

/**
 * 按固定间隔重新订阅失败的 Observable。
 * @param count 失败后的重试次数；`Number.POSITIVE_INFINITY` 表示无限重试
 * @param delay 每次重试前等待的毫秒数
 */
export function retry<T>(count: number, delay: number) {
  return pipe(
    retryWhen<T>((errors) =>
      errors.pipe(
        delayWhen((value, index) => {
          // index 从 0 开始计数第一次失败；达到上限后透传原始错误。
          if (count !== Number.POSITIVE_INFINITY && index >= count) {
            throw value;
          }
          return timer(delay);
        })
      )
    )
  );
}
