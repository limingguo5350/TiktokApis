import json
from types import SimpleNamespace

import pytest

from mstoken_verify import inspect_response, prepare_context


class Response:
    def __init__(self, data, *, token='new-token', status=200):
        self.status_code = status
        self.request = SimpleNamespace(url='https://www.tiktok.com/api/user/playlist/?msToken=' + token)
        self.content = data if isinstance(data, bytes) else json.dumps(data).encode()

    def json(self):
        return json.loads(self.content)


@pytest.mark.parametrize('body', [b'', {'statusCode': 0, 'playList': []},
    {'statusCode': 1, 'playList': [{'name': 'Example'}]},
    {'statusCode': 0, 'status_code': 1, 'playList': [{'name': 'Example'}]},
    {'playList': [{'name': 'Example'}]}])
def test_http_200_is_not_business_success(body):
    row = inspect_response(Response(body), endpoint='/api/user/playlist/', token='new-token')
    assert row['business_success'] is False


def test_real_fields_and_exact_outgoing_token_are_required():
    response = Response({'statusCode': 0, 'playList': [{'name': 'Example', 'videoCount': 3}]})
    row = inspect_response(response, endpoint='/api/user/playlist/', token='new-token')
    assert row['business_success'] is True
    assert row['sample'] == {'name': 'Example', 'videoCount': 3}
    replaced = inspect_response(response, endpoint='/api/user/playlist/', token='another-token')
    assert replaced['business_success'] is False
    assert replaced['request_token_matches'] is False


def test_duplicate_outgoing_token_fails_verification():
    response = Response({'statusCode': 0, 'playList': [{'name': 'Example'}]},
                        token='old-token&msToken=new-token')
    assert inspect_response(response, endpoint='/api/user/playlist/', token='new-token')['business_success'] is False


def test_prepare_keeps_wire_encoding_and_removes_all_old_signatures():
    url, headers = prepare_context({
        'url': 'https://www.tiktok.com/api/user/playlist/?a=%20&a=+&msToken=old&X-Gnarly=old&z=%2F&X-Dynosaur=old&X-Bogus=1',
        'headers': {'User-Agent': 'ua', 'Referer': 'https://www.tiktok.com/', ':path': 'private', 'Content-Length': '10'},
    })
    assert url == 'https://www.tiktok.com/api/user/playlist/?a=%20&a=+&z=%2F'
    assert headers == {'user-agent': 'ua', 'referer': 'https://www.tiktok.com/'}


@pytest.mark.parametrize('url', [
    'https://www.tiktok.com/api/user/playlist/?a=1#fragment',
    'https://www.tiktok.com.evil.invalid/api/user/playlist/',
    'https://www.tiktok.com/api/post/item_list/',
])
def test_verifier_limits_targets_to_supported_read_only_gets(url):
    with pytest.raises(ValueError):
        prepare_context({'url': url, 'headers': {'User-Agent': 'ua', 'Referer': 'https://www.tiktok.com/'}})
