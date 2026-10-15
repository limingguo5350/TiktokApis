"""Public reply GETs with local signatures and bounded server-token bootstrap.

Pass a request context containing url, headers, and optional params/cookies.
Captured signatures are discarded. msToken remains an opaque server value;
bootstrap obtains it from a response, without a browser or CDP.
"""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qsl, quote, unquote_plus, urlsplit

from builder.signer import TiktokSigner
from utils.mstoken import MsTokenClient

REPLY_PATH = '/api/comment/list/reply/'
SIGNATURE_FIELDS = frozenset({'msToken', 'X-Dynosaur', 'X-Bogus', 'X-Gnarly'})


class ReplyRequestError(RuntimeError):
    """A reply request failed; attempts contain only sanitized diagnostics."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


def _cookie_pairs(header: str) -> list[tuple[str, str]]:
    # Request Cookie values may contain unquoted JSON. SimpleCookie parses
    # Set-Cookie syntax and can silently discard the rest of such a header.
    pairs = []
    for part in header.split(';'):
        key, separator, value = part.strip().partition('=')
        if separator and key:
            pairs.append((key.strip(), value.strip()))
    return pairs


def _key(pair: str) -> str:
    return unquote_plus(pair.split('=', 1)[0])


def _without_signatures(query: str) -> str:
    return '&'.join(pair for pair in query.split('&')
                    if pair and _key(pair) not in SIGNATURE_FIELDS)


def _replace_pair(query: str, key: str, value: str) -> str:
    pairs = [pair for pair in query.split('&') if pair and _key(pair) != key]
    pairs.append(quote(key, safe='') + '=' + quote(str(value), safe='-._~'))
    return '&'.join(pairs)


class CommentRepliesClient:
    """Reuse exact business parameters while signing every HTTP request anew.

    ``bootstrap=True`` discards all input token copies. ``keep_cookies=False``
    also discards other input cookies. Device/region/UA and other business
    fields still come from the supplied context. No browser is launched.
    """

    def __init__(self, context: Mapping, *, proxy: str | None = None,
                 bootstrap: bool = False, keep_cookies: bool = True,
                 timeout: float = 25, session=None, metrics: Mapping | None = None):
        parts = urlsplit(str(context['url']))
        if (parts.scheme != 'https' or parts.netloc != 'www.tiktok.com'
                or parts.path != REPLY_PATH or parts.fragment):
            raise ValueError('context must target the public HTTPS comment replies endpoint')
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        if session is not None and proxy is not None:
            raise ValueError('configure proxy on the supplied session')
        headers = {str(k).lower(): str(v) for k, v in context['headers'].items()
                   if not str(k).startswith(':') and str(k).lower() not in
                   {'host', 'content-length', 'accept-encoding'}}
        if not headers.get('user-agent') or not headers.get('referer'):
            raise ValueError('context headers require user-agent and referer')
        cookie_values = dict(_cookie_pairs(headers.pop('cookie', '')))
        cookie_values.update({str(k): str(v) for k, v in (context.get('cookies') or {}).items()})
        query = parts.query
        # Merge optional params without rewriting the raw query's existing pairs.
        for key, value in (context.get('params') or {}).items():
            query = _replace_pair(query, str(key), str(value))
        parsed_query = dict(parse_qsl(query, keep_blank_values=True))
        if not parsed_query.get('item_id') or not parsed_query.get('comment_id'):
            raise ValueError('context requires item_id and comment_id')
        self.item_id = parsed_query['item_id']
        self.comment_id = parsed_query['comment_id']
        token = parsed_query.get('msToken') or cookie_values.get('msToken', '')
        self.ms_token = '' if bootstrap else token
        if self.ms_token and not MsTokenClient._valid_token(self.ms_token):
            raise ValueError('invalid context msToken')
        self._base = parts._replace(query=_without_signatures(query))
        self._headers = headers
        self._cookies = {k: v for k, v in cookie_values.items() if k != 'msToken'} if keep_cookies else {}
        self._metrics = dict(metrics or {})
        self._signer = TiktokSigner()
        self.timeout = timeout
        self._owns_session = session is None
        if session is None:
            from curl_cffi import requests
            session = requests.Session(impersonate='chrome', proxy=proxy)
        self.session = session
        self._closed = False

    def get_replies(self, *, cursor: str | int | None = None,
                    count: str | int | None = None) -> dict:
        """Return validated data plus diagnostics; refresh/retry at most once.

        The only retry is an empty 200/403 response that supplies a different,
        syntactically valid x-ms-token. HTML challenges, rate limits, JSON
        business errors and transport failures are returned as errors.
        """
        if self._closed:
            raise ReplyRequestError('comment replies client is closed', [])
        query = self._base.query
        for key, value in (('cursor', cursor), ('count', count)):
            if value is not None:
                query = _replace_pair(query, key, str(value))
        attempts = []
        for index in range(2):
            sent_token = self.ms_token
            headers = dict(self._headers)
            cookies = dict(self._cookies)
            current_query = query
            if sent_token:
                current_query += '&msToken=' + quote(sent_token, safe='-._~')
                cookies['msToken'] = sent_token
            if cookies:
                headers['cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
            signed = self._signer.sign(
                url=self._base._replace(query=current_query).geturl(), headers=headers,
                user_agent=headers['user-agent'], referer=headers['referer'],
                metrics=self._metrics, allow_missing_ms_token=not bool(sent_token))
            # Cookies learned by curl must not resurrect a stale token behind
            # the explicit query/Cookie copies or contaminate anonymous probes.
            jar = getattr(self.session, 'cookies', None)
            if jar is not None:
                jar.clear()
            try:
                response = self.session.get(signed['url'], headers=headers,
                    timeout=self.timeout, allow_redirects=False)
            except Exception as exc:
                attempts.append({'attempt': index + 1, 'business_success': False,
                                 'error_type': type(exc).__name__})
                raise ReplyRequestError('comment replies transport failed', attempts) from None
            row, data = self._inspect(response, sent_token)
            row['attempt'] = index + 1
            row['generated_signature_lengths'] = {
                k: len(v) for k, v in signed['values'].items() if k != 'msToken'}
            attempts.append(row)
            new_token = response.headers.get('x-ms-token', '')
            token_changed = (new_token != sent_token and MsTokenClient._valid_token(new_token))
            if token_changed and response.status_code in (200, 403):
                self.ms_token = new_token
            if row['business_success']:
                return {'data': data, 'attempts': attempts}
            if (index == 0 and not response.content and response.status_code in (200, 403)
                    and token_changed):
                continue
            raise ReplyRequestError('comment replies response did not contain validated replies', attempts)
        raise AssertionError('unreachable')

    def _inspect(self, response, sent_token: str) -> tuple[dict, object]:
        wire_tokens = [value for key, value in parse_qsl(
            urlsplit(response.request.url).query, keep_blank_values=True) if key == "msToken"]
        cookie_tokens = [value for key, value in _cookie_pairs(
            response.request.headers.get('cookie', '')) if key == 'msToken']
        row = {'http_status': response.status_code, 'response_bytes': len(response.content),
               'request_token_length': len(sent_token),
               'request_token_matches': wire_tokens == ([sent_token] if sent_token else []),
               'cookie_token_matches': cookie_tokens == ([sent_token] if sent_token else []),
               'response_token_length': len(response.headers.get('x-ms-token', '')),
               'response_is_json': False, 'business_success': False}
        try:
            data = response.json()
        except (ValueError, TypeError):
            return row, None
        row['response_is_json'] = True
        if not isinstance(data, dict):
            return row, data
        comments = data.get('comments')
        row.update(status_code=data.get('status_code'), cursor=data.get('cursor'),
                   has_more=data.get('has_more'),
                   comments_count=len(comments) if isinstance(comments, list) else None)
        row['ids_match'] = (isinstance(comments, list) and bool(comments)
                           and all(isinstance(c, dict) and c.get('cid')
                                   and str(c.get('aweme_id')) == self.item_id
                                   and str(c.get('reply_id')) == self.comment_id for c in comments))
        row['unique_comment_ids'] = (row['ids_match'] and
            len({str(c['cid']) for c in comments}) == len(comments))
        row['business_success'] = bool(response.status_code == 200 and data.get('status_code') == 0
            and row['ids_match'] and row['unique_comment_ids']
            and row['request_token_matches'] and row['cookie_token_matches'])
        return row, data

    def close(self):
        if not self._closed and self._owns_session:
            self.session.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
