import { Injectable } from '@angular/core';
import {
  HttpRequest,
  HttpHandler,
  HttpEvent,
  HttpInterceptor,
  HttpErrorResponse,
} from '@angular/common/http';
import { Observable } from 'rxjs';
import { AuthService } from '../services/auth.service';
import { catchError, retry } from 'rxjs/operators';

@Injectable()
/** 为 API 请求注入凭据，并在服务端拒绝凭据时重新向用户收集。 */
export class AuthInterceptor implements HttpInterceptor {
  constructor(private auth: AuthService) {}

  intercept(
    request: HttpRequest<unknown>,
    next: HttpHandler
  ): Observable<HttpEvent<unknown>> {
    return next
      .handle(
        request.clone({ setHeaders: { 'X-API-KEY': this.auth.getApiKey() } })
      )
      .pipe(
        catchError((error: HttpErrorResponse) => {
          if (error.status === 401) {
            // 401 表明已保存的凭据失效；新值供后续请求使用，当前请求仍按既有策略重试。
            if (this.auth.hasApiKey()) {
              this.auth.removeApiKey();
            }
            const apiKey = window.prompt('API Key:') ?? '';
            this.auth.setApiKey(apiKey);
          }
          throw error;
        }),
        // RxJS 的 count 表示失败后的重试次数，不包含首次请求。
        retry(3)
      );
  }
}
