"""Regressions for the observed empty-response/token-update reply flow."""
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from builder.signer import SignerError, TiktokSigner
from api.comment_replies import CommentRepliesClient, ReplyRequestError

URL = 'https://www.tiktok.com/api/comment/list/reply/'
CONTEXT = {
    'url': URL,
    'headers': {'User-Agent': 'Mozilla/5.0 test', 'Referer': 'https://www.tiktok.com/@test/video/123'},
    'cookies': {'msToken': 'expired', 'other': 'context-cookie'},
    'params': {'item_id': '123', 'comment_id': '456', 'count': '3', 'cursor': '3',
               'referer': 'https://www.tiktok.com/zh-Hans', 'msToken': 'expired',
               'X-Dynosaur': 'captured', 'X-Gnarly': 'captured', 'X-Bogus': '1'},
}
DATA = {'status_code': 0, 'comments': [{'cid': '789', 'aweme_id': '123', 'reply_id': '456'}],
        'cursor': 6, 'has_more': 1}


def response(status=200, data=None, token='', raw=None):
    content = (b'' if data is None else b'json') if raw is None else raw
    def as_json():
        if data is None:
            raise ValueError('private body')
        return data
    return SimpleNamespace(status_code=status, content=content, json=as_json,
                           headers={'x-ms-token': token} if token else {})


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        r.request = SimpleNamespace(url=url, headers=kwargs['headers'])
        return r

    def close(self):
        self.closed = True


@pytest.mark.parametrize('status', [200, 403])
def test_empty_response_rotates_token_and_resigns_once(status):
    session = Session(response(status, token='new-token'), response(data=DATA, token='next-token'))
    original = deepcopy(CONTEXT)
    with CommentRepliesClient(CONTEXT, session=session, bootstrap=True, keep_cookies=False) as client:
        result = client.get_replies()
        assert client.ms_token == 'next-token'
    assert not session.closed
    assert CONTEXT == original
    first, second = session.calls
    q1, q2 = (parse_qs(urlsplit(call[0]).query) for call in (first, second))
    assert 'msToken' not in q1
    assert q2['msToken'] == ['new-token']
    assert q1['X-Dynosaur'] != ['captured']
    assert q1['X-Gnarly'] != ['captured']
    assert q1['X-Gnarly'] != q2['X-Gnarly']
    assert 'cookie' not in first[1]['headers']
    assert second[1]['headers']['cookie'] == 'msToken=new-token'
    assert q2['referer'] == ['https://www.tiktok.com/zh-Hans']
    assert all(call[1]['allow_redirects'] is False for call in session.calls)
    assert result['data'] == DATA
    assert result['attempts'][-1]['business_success'] is True
    assert result['attempts'][0]['business_success'] is False
    assert result['attempts'][-1]['request_token_matches'] is True
    assert 'new-token' not in str(result['attempts'])


def test_success_rotates_token_for_next_page_and_preserves_raw_query():
    context = {'url': URL + '?item_id=123&comment_id=456&a=%20&a=+&msToken=stale&X-Dynosaur=captured',
               'headers': {**CONTEXT['headers'], 'Cookie': 'msToken=stale; other=ok'}}
    session = Session(response(data=DATA, token='rotated'), response(data=DATA))
    client = CommentRepliesClient(context, session=session)
    client.get_replies()
    client.get_replies(cursor=6)
    first, second = session.calls
    assert '&a=%20&a=+&' in first[0]
    assert '&a=%20&a=+&' in second[0]
    assert parse_qs(urlsplit(second[0]).query)['msToken'] == ['rotated']
    assert parse_qs(urlsplit(second[0]).query)['cursor'] == ['6']
    assert second[1]['headers']['cookie'] == 'other=ok; msToken=rotated'
    assert client.ms_token == 'rotated'


@pytest.mark.parametrize('reply', [response(403), response(200), response(200, token='same'),
    response(200, token='bad\r\ntoken'), response(302, token='fresh'),
    response(200, token='fresh', raw=b'challenge HTML'), response(429, token='fresh')])
def test_no_blind_retry_for_missing_unchanged_invalid_tokens_or_nonempty_errors(reply):
    context = deepcopy(CONTEXT)
    context['params']['msToken'] = context['cookies']['msToken'] = 'same'
    session = Session(reply)
    with pytest.raises(ReplyRequestError) as error:
        CommentRepliesClient(context, session=session).get_replies()
    assert len(session.calls) == 1
    assert error.value.attempts[0]['business_success'] is False


def test_retry_is_bounded_even_when_every_empty_response_rotates():
    session = Session(response(token='one'), response(token='two'))
    with pytest.raises(ReplyRequestError) as error:
        CommentRepliesClient(CONTEXT, session=session, bootstrap=True).get_replies()
    assert len(session.calls) == 2
    assert len(error.value.attempts) == 2


@pytest.mark.parametrize('data', [
    {**DATA, 'status_code': 5}, {**DATA, 'comments': []},
    {**DATA, 'comments': [{'aweme_id': 'wrong', 'reply_id': '456'}]},
    {**DATA, 'comments': [{'aweme_id': '123', 'reply_id': 'wrong'}]},
    {**DATA, 'comments': [DATA['comments'][0], DATA['comments'][0]]}, [],
])
def test_success_requires_correct_reply_data(data):
    session = Session(response(data=data, token='fresh'))
    with pytest.raises(ReplyRequestError):
        CommentRepliesClient(CONTEXT, session=session).get_replies()
    assert len(session.calls) == 1


def test_error_sanitizes_transport_details():
    with pytest.raises(ReplyRequestError) as error:
        CommentRepliesClient(CONTEXT, session=Session(RuntimeError('secret-full-url'))).get_replies()
    assert 'secret-full-url' not in str(error.value)


@pytest.mark.parametrize('target', ['https://evil.invalid/api/comment/list/reply/',
    'http://www.tiktok.com/api/comment/list/reply/',
    'https://www.tiktok.com/api/comment/publish/', URL + '#fragment',
    'https://user:password@www.tiktok.com/api/comment/list/reply/'])
def test_client_only_accepts_exact_public_read_endpoint(target):
    with pytest.raises(ValueError):
        CommentRepliesClient({**CONTEXT, 'url': target}, session=Session())


def test_no_token_signing_is_opt_in_and_scoped_to_replies_get():
    headers = {'user-agent': 'test'}
    signer = TiktokSigner()
    with pytest.raises(SignerError):
        signer.sign(url=URL + '?a=1', headers=headers, user_agent='test')
    result = signer.sign(url=URL + '?a=1', headers=headers, user_agent='test',
                         allow_missing_ms_token=True)
    assert 'msToken' not in result['values']
    assert set(result['values']) == {'X-Dynosaur', 'X-Bogus', 'X-Gnarly'}
    for url, method in [(URL, 'POST'), (URL.replace('/reply/', '/'), 'GET'),
                        (URL.replace('www.tiktok.com', 'evil.invalid'), 'GET')]:
        with pytest.raises(SignerError):
            signer.sign(url=url, method=method, body='', headers=headers, user_agent='test',
                        allow_missing_ms_token=True)


@pytest.mark.parametrize('mode', ['replaced_query', 'duplicate_query', 'replaced_cookie', 'duplicate_cookie'])
def test_verifier_rejects_silent_wire_token_changes(mode):
    class ChangedWire(Session):
        def get(self, *args, **kwargs):
            r = super().get(*args, **kwargs)
            if mode == 'replaced_cookie':
                r.request.headers = {**r.request.headers, 'cookie': 'msToken=unexpected'}
            elif mode == 'duplicate_cookie':
                r.request.headers['cookie'] += '; msToken=expired'
            elif mode == 'duplicate_query':
                r.request.url += '&msToken=expired'
            else:
                r.request.url += '&msToken=unexpected'
            return r
    with pytest.raises(ReplyRequestError):
        CommentRepliesClient(CONTEXT, session=ChangedWire(response(data=DATA))).get_replies()


def test_bootstrap_flag_cannot_be_used_for_creator_dispatch():
    with pytest.raises(SignerError):
        TiktokSigner().sign(url='https://www.tiktok.com/tiktok/web/project/post/v1/',
                            method='POST', body='', headers={'user-agent': 'test'},
                            user_agent='test', allow_missing_ms_token=True)


def test_json_valued_request_cookie_does_not_hide_later_token():
    context = deepcopy(CONTEXT)
    context['cookies'] = {}
    context['headers']['Cookie'] = 'g_state={"i_l":0,"i_ll":123}; msToken=expired; other=ok'
    session = Session(response(data=DATA))
    result = CommentRepliesClient(context, session=session).get_replies()
    assert result['attempts'][0]['cookie_token_matches'] is True
    assert 'g_state={"i_l":0,"i_ll":123}' in session.calls[0][1]['headers']['cookie']


def test_json_cookie_in_context_does_not_break_wire_token_validation():
    context = deepcopy(CONTEXT)
    context['cookies'] = {'g_state': '{"i_l":0}', 'msToken': 'expired'}
    result = CommentRepliesClient(context, session=Session(response(data=DATA))).get_replies()
    assert result['attempts'][0]['cookie_token_matches'] is True
