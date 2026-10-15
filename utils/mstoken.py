"""Browser-free WebMssdk report encoding and server-issued msToken lifecycle.

The token is opaque and issued online. No browser, Node or captured token is
used here. Fingerprints describe a configured client profile, not local hardware.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Sequence

from signing.pure import _custom_b64, _gnarly_xor

_ALPHABET = 'Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe='


def encode_strdata(data: bytes, *, key_words: Sequence[int] | None = None,
                   compressed: bool = False) -> str:
    """Encode SDK s1 data with fresh 384-bit key material.

    ``compressed=True`` accepts bytes already compressed by the SDK; it does
    not run a compressor. Runtime reports use the SDK's uncompressed format.
    """
    words = tuple(key_words) if key_words is not None else tuple(secrets.randbits(32) for _ in range(12))
    if len(words) != 12 or any(type(w) is not int or not 0 <= w <= 0xffffffff for w in words):
        raise ValueError('key_words must contain twelve uint32 values')
    rounds = (sum(word & 15 for word in words) & 15) + 5
    encrypted = _gnarly_xor(data, words, rounds)
    key = b''.join(word.to_bytes(4, 'little') for word in words)
    insertion = (sum(key) + sum(encrypted)) % (len(encrypted) + 1)
    flag = 76 if compressed else 75
    return _custom_b64(bytes([flag]) + encrypted[:insertion] + key + encrypted[insertion:], _ALPHABET)


def build_report(fingerprint: dict, *, timestamp_ms: int | None = None) -> dict:
    plaintext = json.dumps(fingerprint, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return {'magic': 538969122, 'version': 1, 'dataType': 8,
            'strData': encode_strdata(plaintext),
            'tspFromClient': int(time.time() * 1000) if timestamp_ms is None else timestamp_ms}


class MsTokenError(RuntimeError):
    """A report failed; no replacement token was accepted."""


def build_fingerprint(*, user_agent: str, token: str = '', sequence: int = 1,
                      profile: dict | None = None, timestamp_ms: int | None = None) -> dict:
    """Build a fresh report from a configured, non-secret device template.

    Callers should pass their coherent device profile for API traffic. This
    template is a protocol bootstrap default, not a browser fingerprint probe.
    """
    if profile is None:
        profile = json.loads(Path(__file__).with_name('mstoken_profile.json').read_text(encoding='utf-8'))
    data = json.loads(json.dumps(profile))
    now = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    nav = data.setdefault('navigator', {})
    nav['userAgent'] = user_agent
    nav['appVersion'] = user_agent.removeprefix('Mozilla/')
    nav['platform'] = 'Win32' if 'Windows' in user_agent else ('MacIntel' if 'Macintosh' in user_agent else 'Linux x86_64')
    wid = data.setdefault('wID', {})
    wid.update(timestamp=str(now), seq=[sequence, sequence], aid=1988,
               sdkVersion='5.3.2', scmVersion='1.0.0.417', msgType=1)
    data['tokenList'] = [token] if token else []
    return data


class MsTokenClient:
    """Per-session token acquisition/refresh with explicit proxy support.

    A cache lifetime is a caller policy; it is not the token's server expiry.
    Use the same user agent and proxy for subsequent API requests.
    """

    DEFAULT_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/155.0.0.0 Safari/537.36')
    HOSTS = {'sg': 'https://mssdk-sg.tiktok.com', 'va': 'https://mssdk-va.tiktok.com'}

    def __init__(self, *, user_agent: str = DEFAULT_UA, proxy: str | None = None,
                 region: str = 'sg', timeout: float = 25, cache_seconds: float = 300,
                 profile: dict | None = None, session=None):
        from threading import RLock
        if region not in self.HOSTS:
            raise ValueError('region must be sg or va')
        if timeout <= 0 or cache_seconds < 0:
            raise ValueError('timeout must be positive and cache_seconds nonnegative')
        self.user_agent = user_agent
        self.endpoint = self.HOSTS[region] + '/web/report'
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.profile = profile
        if session is not None and proxy is not None:
            raise ValueError('configure proxy on the supplied session itself')
        self._owns_session = session is None
        if session is None:
            from curl_cffi import requests
            session = requests.Session(impersonate='chrome', proxy=proxy)
        self.session = session
        self._token = ''
        self._fetched_at = 0.0
        self._sequence = 0
        self._closed = False
        self._lock = RLock()

    def get_token(self, *, force: bool = False, previous_token: str = '') -> str:
        with self._lock:
            if self._closed:
                raise MsTokenError('msToken client is closed')
            if not force and self._token and time.monotonic() - self._fetched_at < self.cache_seconds:
                return self._token
            previous = previous_token or self._token
            if previous and not self._valid_token(previous):
                raise ValueError('previous_token is not an ASCII cookie token')
            sequence = self._sequence + 1
            fingerprint = build_fingerprint(user_agent=self.user_agent, token=previous,
                                            sequence=sequence, profile=self.profile)
            report = build_report(fingerprint)
            headers = {'user-agent': self.user_agent, 'origin': 'https://www.tiktok.com',
                       'referer': 'https://www.tiktok.com/', 'accept': '*/*',
                       'content-type': 'text/plain;charset=UTF-8',
                       'cookie': f'msToken={previous}' if previous else ''}
            try:
                response = self.session.post(self.endpoint, headers=headers,
                    data=json.dumps(report, separators=(',', ':')), timeout=self.timeout,
                    allow_redirects=False)
            except Exception as exc:
                # Transport errors may contain URLs/cookies; keep them out of logs.
                raise MsTokenError(f'msToken transport failed ({type(exc).__name__})') from None
            if response.status_code != 200:
                raise MsTokenError(f'msToken report HTTP {response.status_code}')
            try:
                ack = response.json()
            except (ValueError, TypeError):
                raise MsTokenError('msToken report response is not JSON') from None
            # The spelling "maigc" is present in the real report acknowledgement.
            if not isinstance(ack, dict) or any(ack.get(k) != v for k, v in
                    {'maigc': 538051346, 'version': 1, 'dataType': 8}.items()) or ack.get('code', 0) != 0:
                raise MsTokenError('msToken report was not acknowledged by the server')
            token = response.headers.get('x-ms-token', '')
            if not token:
                from http.cookies import SimpleCookie
                try:
                    values = response.headers.get_list('set-cookie')
                except AttributeError:
                    values = [response.headers.get('set-cookie', '')]
                for value in values:
                    cookies = SimpleCookie()
                    try:
                        cookies.load(value)
                    except Exception:
                        continue
                    if 'msToken' in cookies:
                        token = cookies['msToken'].value
            if not self._valid_token(token):
                raise MsTokenError('acknowledged msToken report has no valid server token')
            self._token = token
            self._sequence = sequence
            self._fetched_at = time.monotonic()
            return token

    @staticmethod
    def _valid_token(value: str) -> bool:
        return isinstance(value, str) and bool(value) and all(33 <= ord(c) <= 126 and c not in ';,"\\' for c in value)

    def refresh(self, *, previous_token: str = '') -> str:
        return self.get_token(force=True, previous_token=previous_token)

    def close(self):
        with self._lock:
            if not self._closed and self._owns_session:
                self.session.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
