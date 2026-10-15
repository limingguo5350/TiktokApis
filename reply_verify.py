"""Verify locally generated signatures against the public comment replies GET.

Only sanitized diagnostics are printed/written. A context file holds the exact
url, headers and optional params/cookies from your request. Bootstrap ignores
all supplied msToken/signature values and, by default, all supplied cookies.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from api.comment_replies import CommentRepliesClient, ReplyRequestError
from utils.mstoken import MsTokenClient, MsTokenError


def verify(context: dict, *, proxy: str | None = None, token_source: str = 'bootstrap',
           keep_cookies: bool = False, region: str = 'sg') -> dict:
    if token_source not in {'bootstrap', 'context', 'report'}:
        raise ValueError('invalid token_source')
    evidence = {'at_utc': datetime.now(timezone.utc).isoformat(),
                'endpoint': '/api/comment/list/reply/', 'token_source': token_source,
                'input_cookies_used': keep_cookies, 'business_success': False,
                'browser_used': False, 'attempts': []}
    with CommentRepliesClient(context, proxy=proxy, bootstrap=token_source != 'context',
                              keep_cookies=keep_cookies) as client:
        if token_source == 'report':
            headers = {str(k).lower(): str(v) for k, v in context['headers'].items()}
            try:
                with MsTokenClient(user_agent=headers['user-agent'], proxy=proxy,
                                   region=region) as issuer:
                    client.ms_token = issuer.get_token()
            except MsTokenError:
                evidence['error'] = 'msToken report issuance failed'
                return evidence
        try:
            result = client.get_replies()
        except ReplyRequestError as error:
            evidence.update(attempts=error.attempts, error=str(error))
        else:
            evidence.update(attempts=result['attempts'], business_success=True)
    return evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--proxy')
    parser.add_argument('--token-source', choices=('bootstrap', 'context', 'report'),
                        default='bootstrap')
    parser.add_argument('--keep-cookies', action='store_true')
    parser.add_argument('--region', choices=('sg', 'va'), default='sg')
    parser.add_argument('--output', type=Path, help='sanitized evidence JSON only')
    args = parser.parse_args(argv)
    try:
        context = json.loads(args.context.read_text())
        evidence = verify(context, proxy=args.proxy, token_source=args.token_source,
                          keep_cookies=args.keep_cookies, region=args.region)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        # Parsing errors can contain input values/paths. Keep them out of stdout.
        evidence = {'business_success': False, 'error_type': type(exc).__name__}
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + '\n')
    return 0 if evidence['business_success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
