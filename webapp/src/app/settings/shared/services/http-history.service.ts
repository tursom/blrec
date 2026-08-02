import { HttpClient, HttpParams, HttpResponse } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import { ResponseMessage } from 'src/app/shared/api.models';

export interface HttpHistoryStatus {
  enabled: boolean;
  record_count: number;
  total_size: number;
  oldest_at: string | null;
  newest_at: string | null;
  room_ids: number[];
  dropped_records: number;
  last_error: string | null;
}

export interface HttpHistoryExportFilter {
  roomId?: number;
  since?: string;
  until?: string;
}

@Injectable({ providedIn: 'root' })
export class HttpHistoryService {
  constructor(private http: HttpClient, private url: UrlService) {}

  getStatus(): Observable<HttpHistoryStatus> {
    return this.http.get<HttpHistoryStatus>(
      this.url.makeApiUrl('/api/v1/http-history/status')
    );
  }

  exportHistory(
    filter: HttpHistoryExportFilter
  ): Observable<HttpResponse<Blob>> {
    let params = new HttpParams();
    if (filter.roomId !== undefined) {
      params = params.set('room_id', filter.roomId);
    }
    if (filter.since) {
      params = params.set('since', filter.since);
    }
    if (filter.until) {
      params = params.set('until', filter.until);
    }
    return this.http.get(this.url.makeApiUrl('/api/v1/http-history/export'), {
      params,
      observe: 'response',
      responseType: 'blob',
    });
  }

  clear(): Observable<ResponseMessage> {
    return this.http.delete<ResponseMessage>(
      this.url.makeApiUrl('/api/v1/http-history')
    );
  }
}
