#!/usr/bin/env python3
"""Development machine only: the owner's governed workbench Actions of the AT-072 local restore drill, on the owner's own session.

    drill_workbench.py --drill-dir .dev-drill --origin https://127.0.0.1:8753 erase-consumer          # source, after the drill mark
    drill_workbench.py --drill-dir .dev-drill --origin https://127.0.0.1:8754 open-restore-scope --restore-ref drill-<id>   # restored

Signs in exactly as the workbench page does (POST /api/v1/workbench/auth/login with the owner's username and password from
<drill-dir>/credentials.json, never arguments, never printed), submits one workbench Action (the same governed entry the
page uses: owner grant, current authentication, SQL handler), prints the HTTP status and the JSON answer, and signs out.
The certificate is the drill's own loopback certificate. Synthetic accounts only.
"""
import argparse
import json
import secrets
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--drill-dir', type=Path, required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--as', dest='member', choices=('owner', 'operator'), default='owner')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('erase-consumer')
    opening = sub.add_parser('open-restore-scope')
    opening.add_argument('--restore-ref', required=True)
    a = parser.parse_args(argv)
    import httpx
    drill = a.drill_dir.resolve()
    accounts = json.loads((drill / 'credentials.json').read_text())
    if a.command == 'erase-consumer':
        operation, body = 'erase_consumer', {'consumer_id': accounts['consumer_id'], 'reason': 'AT-072 演练：恢复点之后的合成删除'}
    else:
        operation, body = 'open_restore_scope', {'restore_ref': a.restore_ref}
    headers = {'Origin': a.origin}
    with httpx.Client(base_url=a.origin, verify=str(drill / 'tls-cert.pem'), timeout=30) as client:
        login = client.post('/api/v1/workbench/auth/login', headers=headers,
                            json={'username': accounts[a.member]['username'], 'password': accounts[a.member]['password']})
        if login.status_code != 200:
            print(json.dumps({'operation': operation, 'login_status': login.status_code}))
            return 1
        csrf = login.json()['csrf_token']
        response = client.post('/api/v1/workbench/actions/' + operation, json=body,
                               headers={**headers, 'X-CSRF-Token': csrf, 'Idempotency-Key': 'at072-' + secrets.token_hex(12)})
        client.post('/api/v1/workbench/auth/logout', headers={**headers, 'X-CSRF-Token': csrf})
    print(json.dumps({'operation': operation, 'status': response.status_code, 'answer': response.json()}, ensure_ascii=False))
    return 0 if response.status_code == 200 else 1


if __name__ == '__main__':
    raise SystemExit(main())
