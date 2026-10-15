"""Browser-free msToken smoke check. Prints metadata, never the token itself."""
import argparse
import json
import os

from utils.mstoken import MsTokenClient, MsTokenError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default=os.getenv('HTTPS_PROXY') or os.getenv('https_proxy'))
    parser.add_argument('--region', choices=('sg', 'va'), default='sg')
    parser.add_argument('--refresh', action='store_true', help='also force one renewal')
    parser.add_argument('--profile', help='optional configured device-profile JSON')
    args = parser.parse_args()
    profile = None
    if args.profile:
        from pathlib import Path
        profile = json.loads(Path(args.profile).read_text(encoding='utf-8'))
    try:
        with MsTokenClient(proxy=args.proxy, region=args.region, profile=profile) as client:
            first = client.get_token()
            result = {'issued': True, 'token_length': len(first), 'region': args.region}
            if args.refresh:
                second = client.refresh()
                result.update(refreshed=True, refresh_length=len(second), rotated=first != second)
        print(json.dumps(result))
        return 0
    except MsTokenError as exc:
        print(json.dumps({'issued': False, 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
