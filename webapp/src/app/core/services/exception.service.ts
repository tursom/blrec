import { Injectable } from '@angular/core';

import { webSocket, WebSocketSubject } from 'rxjs/webSocket';

import { Event } from '../models/event.model';
import { UrlService } from './url.service';

@Injectable({
  providedIn: 'root',
})
/** 懒加载并共享服务端异常 WebSocket，与普通业务事件通道相互隔离。 */
export class ExceptionService {
  private exceptionSubject?: WebSocketSubject<Event>;

  constructor(private url: UrlService) {}

  get exceptions() {
    if (!this.exceptionSubject) {
      // 缓存 subject，确保应用内所有异常观察者消费同一条连接。
      this.exceptionSubject = webSocket(
        this.url.makeWebSocketUrl('/ws/v1/exceptions')
      );
    }
    return this.exceptionSubject;
  }
}
