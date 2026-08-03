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
  incident_count: number;
  active_incident_count: number;
  payload_size: number;
}

export interface HttpHistoryExportFilter {
  roomId?: number;
  since?: string;
  until?: string;
}

export interface HttpIncidentSummary {
  incident_id: string;
  room_id: number;
  kind: string;
  first_at: string;
  last_at: string;
  occurrence_count: number;
  status: string;
  record_count: number;
  payload_size: number;
  partial: boolean;
}

@Injectable({ providedIn: 'root' })
export class HttpHistoryService {
  constructor(
    private http: HttpClient,
    private url: UrlService,
  ) {}

  getStatus(): Observable<HttpHistoryStatus> {
    return this.http.get<HttpHistoryStatus>(
      this.url.makeApiUrl('/api/v1/http-history/status'),
    );
  }

  exportHistory(
    filter: HttpHistoryExportFilter,
  ): Observable<HttpResponse<Blob>> {
    return this.http.get(this.url.makeApiUrl('/api/v1/http-history/export'), {
      params: this.filterParams(filter),
      observe: 'response',
      responseType: 'blob',
    });
  }

  listIncidents(
    filter: HttpHistoryExportFilter,
  ): Observable<HttpIncidentSummary[]> {
    return this.http.get<HttpIncidentSummary[]>(
      this.url.makeApiUrl('/api/v1/http-history/incidents'),
      { params: this.filterParams(filter) },
    );
  }

  exportIncident(incidentId: string): Observable<HttpResponse<Blob>> {
    return this.http.get(
      this.url.makeApiUrl(
        `/api/v1/http-history/incidents/${encodeURIComponent(incidentId)}/export`,
      ),
      { observe: 'response', responseType: 'blob' },
    );
  }

  clear(): Observable<ResponseMessage> {
    return this.http.delete<ResponseMessage>(
      this.url.makeApiUrl('/api/v1/http-history'),
    );
  }

  private filterParams(filter: HttpHistoryExportFilter): HttpParams {
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
    return params;
  }
}
