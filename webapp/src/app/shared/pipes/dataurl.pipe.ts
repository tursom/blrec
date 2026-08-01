import { Pipe, PipeTransform } from '@angular/core';
import { SafeUrl, DomSanitizer } from '@angular/platform-browser';

import { from, fromEvent, Observable, of } from 'rxjs';
import { catchError, map, switchMap, tap } from 'rxjs/operators';

const dataUrlCache = new Map<string, SafeUrl>();
const objectUrlCache = new Map<string, SafeUrl>();

@Pipe({
  name: 'dataurl',
})
/**
 * 下载外部图片并转换成 Angular 可绑定的本地 URL。
 *
 * 两类缓存都以原始 URL 为键并在应用生命周期内复用，避免列表刷新时重复下载和转换。
 */
export class DataurlPipe implements PipeTransform {
  constructor(private domSanitizer: DomSanitizer) {}

  transform(
    url: string,
    type: 'object' | 'data' = 'object'
  ): Observable<SafeUrl> {
    if (type === 'object') {
      if (objectUrlCache.has(url)) {
        return of(objectUrlCache.get(url)!);
      }
      return from(this.fetchImage(url)).pipe(
        // Object URL 不复制 Blob 内容，适合常规展示；缓存期间必须保持 URL 有效。
        map((data) => URL.createObjectURL(data)),
        map((objectUrl) => this.domSanitizer.bypassSecurityTrustUrl(objectUrl)),
        tap((objectSafeUrl) => objectUrlCache.set(url, objectSafeUrl)),
        catchError(() => of(this.domSanitizer.bypassSecurityTrustUrl('')))
      );
    } else {
      if (dataUrlCache.has(url)) {
        return of(dataUrlCache.get(url)!);
      }
      return from(this.fetchImage(url)).pipe(
        // Data URL 会把内容编码进字符串，适合需要自包含地址的调用方。
        switchMap((data) => this.createDataURL(data)),
        tap((dataUrl) => dataUrlCache.set(url, dataUrl)),
        catchError(() => of(this.domSanitizer.bypassSecurityTrustUrl('')))
      );
    }
  }

  private async fetchImage(url: string): Promise<Blob> {
    // 清空 referrer，避免向第三方图片源泄露当前管理页面地址。
    const res = await fetch(url, { referrer: '' });
    return await res.blob();
  }

  private createDataURL(data: Blob): Observable<SafeUrl> {
    const reader = new FileReader();
    const observable = fromEvent(reader, 'load').pipe(
      map(() =>
        this.domSanitizer.bypassSecurityTrustUrl(reader.result as string)
      )
    );
    reader.readAsDataURL(data);
    return observable;
  }
}
