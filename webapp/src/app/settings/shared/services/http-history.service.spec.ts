import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';

import { HttpHistoryService } from './http-history.service';

describe('HttpHistoryService', () => {
  let http: HttpTestingController;
  let service: HttpHistoryService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        HttpHistoryService,
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    service = TestBed.inject(HttpHistoryService);
  });

  afterEach(() => http.verify());

  it('exports the selected room and UTC range as a blob', () => {
    service
      .exportHistory({
        roomId: 42,
        since: '2026-08-01T00:00:00.000Z',
        until: '2026-08-02T00:00:00.000Z',
      })
      .subscribe((response) =>
        expect(response.body).toEqual(new Blob(['zip'])),
      );

    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/v1/http-history/export' &&
        candidate.params.get('room_id') === '42' &&
        candidate.params.get('since') === '2026-08-01T00:00:00.000Z' &&
        candidate.params.get('until') === '2026-08-02T00:00:00.000Z',
    );
    expect(request.request.responseType).toBe('blob');
    request.flush(new Blob(['zip']));
  });

  it('omits unset filters for an all-history export', () => {
    service.exportHistory({}).subscribe();

    const request = http.expectOne('/api/v1/http-history/export');
    expect(request.request.params.keys()).toEqual([]);
    request.flush(new Blob(['zip']));
  });

  it('lists incidents for the selected room and UTC range', () => {
    service
      .listIncidents({
        roomId: 42,
        since: '2026-08-01T00:00:00.000Z',
        until: '2026-08-02T00:00:00.000Z',
      })
      .subscribe((incidents) => expect(incidents[0].incident_id).toBe('abc'));

    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/v1/http-history/incidents' &&
        candidate.params.get('room_id') === '42' &&
        candidate.params.get('since') === '2026-08-01T00:00:00.000Z' &&
        candidate.params.get('until') === '2026-08-02T00:00:00.000Z',
    );
    request.flush([{ incident_id: 'abc' }]);
  });

  it('exports one incident as a blob', () => {
    service
      .exportIncident('abc')
      .subscribe((response) =>
        expect(response.body).toEqual(new Blob(['zip'])),
      );

    const request = http.expectOne('/api/v1/http-history/incidents/abc/export');
    expect(request.request.responseType).toBe('blob');
    request.flush(new Blob(['zip']));
  });
});
