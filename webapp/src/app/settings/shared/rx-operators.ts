import { AbstractControl } from '@angular/forms';

import { pipe } from 'rxjs';
import {
  debounceTime,
  distinctUntilChanged,
  filter,
  map,
  tap,
} from 'rxjs/operators';
import isEqual from 'lodash-es/isEqual';
import isString from 'lodash-es/isString';
import transform from 'lodash-es/transform';

export function filterValueChanges<T extends object>(control: AbstractControl) {
  // 只同步通过校验且稳定 300 ms 的值，并用深比较抑制结构相同的重复对象。
  return pipe(
    filter<T>(() => control.valid),
    debounceTime(300),
    trimString(),
    distinctUntilChanged<T>(isEqual)
  );
}

export function trimString<T extends object>() {
  // 表单既可能直接发出字符串，也可能发出一层对象；这里只清理当前层级。
  return pipe(
    map((object: T) => {
      if (isString(object)) {
        return object.trim() as unknown as T;
      }
      return transform(
        object,
        (result, value: any, prop) => {
          result[prop] = isString(value) ? value.trim() : value;
        },
        {} as T
      ) as T;
    })
  );
}

export function debugValueChanges<T>() {
  return pipe(
    tap((value: T) => {
      console.debug('value change:', value);
    })
  );
}
