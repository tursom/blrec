import { Injectable } from '@angular/core';

import { webSocket, WebSocketSubject } from 'rxjs/webSocket';

import { Event } from '../models/event.model';
import { UrlService } from './url.service';

@Injectable({
  providedIn: 'root',
})
/** 懒加载并共享服务端事件 WebSocket，避免每个订阅者各建一条连接。 */
export class EventService {
  private eventSubject?: WebSocketSubject<Event>;

  constructor(private url: UrlService) {}

  get events() {
    if (!this.eventSubject) {
      // WebSocketSubject 在首个订阅时连接，并在订阅者之间复用收发通道。
      this.eventSubject = webSocket(this.url.makeWebSocketUrl('/ws/v1/events'));
    }
    return this.eventSubject;
  }
}
