"""Read-only online msToken check using an anonymous request context.

The context JSON contains {"url": "https://www.tiktok.com/api/user/playlist/?...",
"headers": {...}} from your own session. No browser is used by this command.
Full URLs, tokens, cookies and response bodies are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

from builder.signer import TiktokSigner
from utils.mstoken import MsTokenClient, MsTokenError

ENDPOINTS = {
    '/api/user/playlist/': 'playList',
    '/api/user/collection_list/': 'collectionList',
}
SIGNATURE_KEYS = ('msToken=', 'X-Dynosaur=', 'X-Bogus=', 'X-Gnarly=')


def prepare_context(context: dict) -> tuple[str, dict]:
    parts = urlsplit(context['url'])
    if (parts.scheme != 'https' or parts.hostname != 'www.tiktok.com'
            or parts.port not in (None, 443) or parts.username or parts.password
            or parts.path not in ENDPOINTS or parts.fragment):
        raise ValueError('context must target a supported public TikTok HTTPS GET endpoint')
    # Preserve exact ordering and escaping of every non-signature query pair.
    query = '&'.join(x for x in parts.query.split('&') if x and not x.startswith(SIGNATURE_KEYS))
    unsigned = urlunsplit(parts._replace(query=query))
    headers = {str(k).lower(): str(v) for k, v in context['headers'].items()
               if not str(k).startswith(':') and str(k).lower() not in
               ('host', 'content-length', 'accept-encoding')}
    if not headers.get('user-agent') or not headers.get('referer'):
        raise ValueError('context headers require user-agent and referer')
    return unsigned, headers


def inspect_response(response, *, endpoint: str, token: str) -> dict:
    sent = parse_qs(urlsplit(response.request.url).query).get('msToken', [])
    row = {'http_status': response.status_code, 'response_bytes': len(response.content),
           'request_token_matches': sent == ([token] if token else []),
           'request_token_length': len(token), 'response_is_json': False,
           'business_success': False}
    try:
        data = response.json()
    except ValueError:
        return row
    row['response_is_json'] = True
    if not isinstance(data, dict):
        return row
    codes = [data[k] for k in ('statusCode', 'status_code') if k in data]
    row['status_codes'] = codes
    field = ENDPOINTS[endpoint]
    items = data.get(field)
    row['data_field'] = field
    row['item_count'] = len(items) if isinstance(items, list) else 0
    if isinstance(items, list) and items and isinstance(items[0], dict):
        row['sample'] = {k: items[0][k] for k in ('name', 'mixName', 'videoCount', 'total')
                         if k in items[0]}
    row['business_success'] = bool(response.status_code == 200 and codes
                                   and all(code == 0 for code in codes)
                                   and isinstance(items, list) and items
                                   and all(isinstance(item, dict) for item in items)
                                   and row['request_token_matches'])
    return row


def verify(context: dict, *, proxy: str | None = None, region: str = 'sg') -> dict:
    unsigned, headers = prepare_context(context)
    endpoint = urlsplit(unsigned).path
    with MsTokenClient(user_agent=headers['user-agent'], proxy=proxy, region=region) as client:
        token = client.get_token()
        # An obviously unissued value checks whether this endpoint enforces tokens.
        dummy = secrets.token_urlsafe(len(token))[:len(token)]
        rows = []
        from curl_cffi import requests
        for label, value in (('issued_token', token), ('unissued_control', dummy),
                             ('unsigned_no_token_control', '')):
            current_headers = dict(headers)
            cookies = [pair.strip() for pair in current_headers.get('cookie', '').split(';')
                       if pair.strip() and pair.strip().split('=', 1)[0] != 'msToken']
            if value:
                cookies.append('msToken=' + value)
            current_headers['cookie'] = '; '.join(cookies)
            url = unsigned
            if value:
                url = TiktokSigner().sign(
                    url=unsigned + ('&' if urlsplit(unsigned).query else '?')
                        + 'msToken=' + quote(value, safe='-._~='),
                    headers=current_headers, user_agent=headers['user-agent'],
                    referer=headers['referer'])['url']
            # Isolate response cookie changes between controls. The same configured
            # proxy, TLS impersonation, identity parameters and headers are used.
            with requests.Session(impersonate='chrome', proxy=proxy) as session:
                try:
                    response = session.get(url, headers=current_headers, timeout=30,
                                           allow_redirects=False)
                    row = inspect_response(response, endpoint=endpoint, token=value)
                except Exception as exc:
                    row = {'business_success': False, 'error_type': type(exc).__name__}
            rows.append({'mode': label, **row})
    return {'endpoint': endpoint, 'token_issued': True, 'token_length': len(token),
            'browser_used_by_verifier': False,
            'request_context_source': 'caller-supplied anonymous request',
            'results': rows,
            'issued_token_business_success': rows[0]['business_success'],
            'controls_also_return_data': any(r['business_success'] for r in rows[1:]),
            'strict_token_validation': 'not_established'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True, help='private JSON: url and headers')
    parser.add_argument('--proxy', default=os.getenv('HTTPS_PROXY') or os.getenv('https_proxy'))
    parser.add_argument('--region', choices=('sg', 'va'), default='sg')
    args = parser.parse_args()
    try:
        result = verify(json.loads(Path(args.context).read_text()), proxy=args.proxy, region=args.region)
    except (MsTokenError, ValueError, KeyError, OSError) as exc:
        print(json.dumps({'verified': False, 'error_type': type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['issued_token_business_success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
