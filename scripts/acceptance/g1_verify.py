#!/usr/bin/env python3
"""Restricted G1, fixed query only. Exit 0 clear, 1 nonzero, 2 unavailable/invalid.

No provisioning, tenant input, role switch, writes or fallback identity. Deployment
and temporary enablement of the NOLOGIN identity require separate authorization.
"""
import argparse
import json

import psycopg
from nexloop_eios.private_configuration import read_private_text


def verdict(value):
    keys = {'status', 'tenants_checked', 'suspended_checked', 'effect_actions', 'active_admissions'}
    if type(value) is not dict or set(value) != keys:
        raise ValueError()
    if any(type(value[k]) is not int or value[k] < 0 for k in keys - {'status'}):
        raise ValueError()
    if value['tenants_checked'] == 0 or value['effect_actions'] == 0 or value['suspended_checked'] > value['tenants_checked']:
        raise ValueError()
    expected = 'clear' if value['active_admissions'] == 0 else 'blocked_nonzero'
    if value['status'] != expected:
        raise ValueError()
    return 0 if expected == 'clear' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url-file', required=True)
    args = parser.parse_args(argv)
    try:
        dsn = read_private_text(args.database_url_file, maximum=4096).strip()
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as db:
            try:
                db.execute('begin isolation level repeatable read read only')
                db.execute("set local statement_timeout='10s'")
                db.execute("set local lock_timeout='1s'")
                value = db.execute('select nexloop_maintenance.nexloop_g1_verify()').fetchone()[0]
                code = verdict(value)
            finally:
                # No summary is released unless ROLLBACK returned successfully.
                db.execute('rollback')
        print(json.dumps(value, sort_keys=True))
        return code
    except Exception:
        print(json.dumps({'status': 'blocked', 'reason': 'verification_or_rollback_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
