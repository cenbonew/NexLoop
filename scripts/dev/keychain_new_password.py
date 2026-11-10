#!/usr/bin/env python3
"""Generate a new password straight into the macOS Keychain (run-nx058-browser-walkthrough K1; owner rule: passwords live only in the
Keychain). The value never appears in argv, a file, a heredoc, a pipe to head, or the output: it is drawn with `secrets`, checked for
length, and handed to `security -i` on its stdin. An existing item is refused (delete it yourself first, per your Keychain rule).

    python3 -I scripts/dev/keychain_new_password.py --service nexloop/stage/walk-op-20261110 --account walk-op-20261110

Prints only {"stored": true, "service": ..., "account": ...}. Exit 1 on refusal."""
import argparse
import json
import re
import secrets
import subprocess
import sys

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/@-]{0,127}')
LENGTH = 24  # >= the workbench minimum of 12 characters


def new_password():
    value = secrets.token_urlsafe(LENGTH)  # [A-Za-z0-9_-] only: no quoting inside the security -i command line
    if len(value) < LENGTH or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise SystemExit('generated value failed its own check')
    return value


def store(service, account, run=subprocess.run):
    if not NAME.fullmatch(service or '') or not NAME.fullmatch(account or ''):
        return {'stored': False, 'reason': 'bad_name'}
    if run(['security', 'find-generic-password', '-s', service, '-a', account], capture_output=True).returncode == 0:
        return {'stored': False, 'reason': 'exists'}
    command = f'add-generic-password -s {service} -a {account} -w {new_password()}\n'
    done = run(['security', '-i'], input=command, text=True, capture_output=True)
    del command
    if done.returncode != 0:
        return {'stored': False, 'reason': 'security_failed'}
    if run(['security', 'find-generic-password', '-s', service, '-a', account], capture_output=True).returncode != 0:
        return {'stored': False, 'reason': 'not_found_after_store'}
    return {'stored': True, 'service': service, 'account': account}


def main(argv=None, run=subprocess.run):
    p = argparse.ArgumentParser()
    p.add_argument('--service', required=True)
    p.add_argument('--account', required=True)
    a = p.parse_args(argv)
    result = store(a.service, a.account, run=run)
    print(json.dumps(result))
    return 0 if result['stored'] else 1


if __name__ == '__main__':
    sys.exit(main())
