"""Remove credentials and account data from HTTP diagnostic records."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = ('sanitize_record', 'sanitize_url')

_REDACTED = '[REDACTED]'
_SAFE_REQUEST_HEADERS = frozenset(
    (
        'accept',
        'accept-encoding',
        'accept-language',
        'cache-control',
        'connection',
        'content-type',
        'origin',
        'pragma',
        'referer',
        'user-agent',
    )
)
_SAFE_RESPONSE_HEADERS = frozenset(
    (
        'accept-ranges',
        'age',
        'cache-control',
        'content-encoding',
        'content-length',
        'content-range',
        'content-type',
        'date',
        'etag',
        'expires',
        'last-modified',
        'location',
        'server',
        'transfer-encoding',
        'vary',
    )
)
_SENSITIVE_NAME = re.compile(
    r'(?:auth|authorization|bili[_-]?jct|buvid|cookie|credential|csrf|dedeuser|'
    r'key|pass|secret|sess|session|sign|token|w_rid)',
    re.IGNORECASE,
)
_PLAYBACK_SECRET_NAMES = frozenset(
    (
        'deadline',
        'expires',
        'oi',
        'sk',
        'trid',
        'txsecret',
        'txtime',
        'uipk',
        'upsig',
        'wssecret',
        'wstime',
    )
)
_COOKIE_VALUE = re.compile(
    r'(?i)\b([A-Za-z0-9_.-]*(?:cookie|sess|csrf|jct|buvid)[A-Za-z0-9_.-]*)=([^;\s,]+)'
)
_COOKIE_HEADER_IN_TEXT = re.compile(r'(?i)\bcookie\s*:\s*[^\r\n]+')
_CREDENTIAL_HEADER_IN_TEXT = re.compile(
    r'(?i)\b(authorization|proxy-authorization|x-api-key|api-key)\s*[:=]\s*'
    r'(?:bearer\s+)?[^\s;,]+'
)
_NAMED_CREDENTIAL_IN_TEXT = re.compile(
    r'(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|signature|token|'
    r'w_rid)\s*=\s*[^\s;,]+'
)
_URL_IN_TEXT = re.compile(
    r'https?://[^\s\]\[\)\(\}\{<>"\']+'
    r'|(?:\.?\.?/)?[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~!$&*+,;=:@%-]*)*'
    r'\?[^\s\]\[\)\(\}\{<>"\']+'
)


def _is_sensitive_name(name: str) -> bool:
    compact = name.replace('-', '').replace('_', '').casefold()
    return bool(_SENSITIVE_NAME.search(name)) or compact in _PLAYBACK_SECRET_NAMES


def sanitize_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return _sanitize_text(url)
    if not parts.query and (not parts.scheme or not parts.netloc):
        return _sanitize_text(url)

    query = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((name, _REDACTED if _is_sensitive_name(name) else value))
    netloc = parts.netloc.rsplit('@', 1)[-1]
    return urlunsplit(
        (parts.scheme, netloc, parts.path, urlencode(query, doseq=True), '')
    )


def _sanitize_text(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith(('{', '[')) and stripped.endswith(('}', ']')):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            return json.dumps(
                _sanitize_value(parsed), ensure_ascii=False, separators=(',', ':')
            )
    value = _COOKIE_HEADER_IN_TEXT.sub(f'Cookie: {_REDACTED}', value)
    value = _CREDENTIAL_HEADER_IN_TEXT.sub(
        lambda match: f'{match.group(1)}: {_REDACTED}', value
    )
    value = _NAMED_CREDENTIAL_IN_TEXT.sub(
        lambda match: f'{match.group(1)}={_REDACTED}', value
    )
    value = _COOKIE_VALUE.sub(lambda match: f'{match.group(1)}={_REDACTED}', value)
    return _URL_IN_TEXT.sub(lambda match: sanitize_url(match.group(0)), value)


def _cookie_names(headers: Mapping[str, Any]) -> Iterable[str]:
    for name, value in headers.items():
        if name.casefold() != 'cookie':
            continue
        for part in str(value).split(';'):
            cookie_name, separator, _ = part.strip().partition('=')
            if separator and cookie_name:
                yield cookie_name


def _sanitize_headers(
    headers: Mapping[str, Any], allowed_names: frozenset[str]
) -> Dict[str, str]:
    result = {}
    for name, value in headers.items():
        if name.casefold() not in allowed_names:
            continue
        string_value = str(value)
        if name.casefold() in ('referer', 'location'):
            string_value = sanitize_url(string_value)
        result[name] = _sanitize_text(string_value)
    return result


def _sanitize_value(value: Any, key: str = '') -> Any:
    if _is_sensitive_name(key) or key.casefold() == 'extra':
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(k): _sanitize_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        if '?' in value and not any(character.isspace() for character in value):
            return sanitize_url(value)
        if value.startswith(('http://', 'https://')):
            return sanitize_url(value)
        return _sanitize_text(value)
    return value


def _sanitize_nav_response(record: Dict[str, Any]) -> None:
    request = record.get('request')
    response = record.get('response')
    if not isinstance(request, Mapping) or not isinstance(response, dict):
        return
    if urlsplit(str(request.get('url', ''))).path != '/x/web-interface/nav':
        return
    body = response.get('body')
    if not isinstance(body, Mapping):
        return
    data = body.get('data')
    safe_data = {}
    if isinstance(data, Mapping) and isinstance(data.get('wbi_img'), Mapping):
        safe_data['wbi_img'] = _sanitize_value(data['wbi_img'])
    response['body'] = {
        key: _sanitize_value(body[key], key)
        for key in ('code', 'message', 'msg', 'ttl')
        if key in body
    }
    if safe_data:
        response['body']['data'] = safe_data


def sanitize_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a deep, issue-safe copy of a diagnostic record."""

    result = copy.deepcopy(dict(record))
    request = result.get('request')
    if isinstance(request, dict):
        if isinstance(request.get('url'), str):
            request['url'] = sanitize_url(request['url'])
        headers = request.get('headers')
        if isinstance(headers, Mapping):
            names = sorted(set(_cookie_names(headers)))
            cookie_present = any(
                name.casefold() == 'cookie' and bool(str(value))
                for name, value in headers.items()
            )
            request['headers'] = _sanitize_headers(headers, _SAFE_REQUEST_HEADERS)
            request['cookie_present'] = cookie_present
            request['cookie_names'] = names
        if 'body' in request:
            request['body'] = _sanitize_value(request['body'])
        for key in tuple(request):
            if key not in ('url', 'headers', 'body', 'cookie_present', 'cookie_names'):
                request[key] = _sanitize_value(request[key], key)

    response = result.get('response')
    if isinstance(response, dict):
        headers = response.get('headers')
        if isinstance(headers, Mapping):
            response['headers'] = _sanitize_headers(headers, _SAFE_RESPONSE_HEADERS)
        if 'body' in response:
            response['body'] = _sanitize_value(response['body'])
        for key in tuple(response):
            if key not in ('headers', 'body'):
                response[key] = _sanitize_value(response[key], key)

    for key in tuple(result):
        if key not in ('request', 'response'):
            result[key] = _sanitize_value(result[key], key)
    _sanitize_nav_response(result)
    return result
