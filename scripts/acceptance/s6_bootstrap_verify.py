#!/usr/bin/env python3
"""NX-040 stage: bootstrap.verify on its own, for the check after the owner publishes an S6-R8 proposal with schema_proposal.py.

    s6_bootstrap_verify.py --config PRIVATE.json

Uses the same verify connection and the same summary as s6_stage_schema.py r8's catalog_and_verify_unchanged step (roles.verify.dsn_file
of the S6 configuration; one read-only transaction): prints one JSON line {revision, migration_count, digest}. Compare it with the
`before` of r8's result. Never prints the DSN. Exit: 0 printed; 1 not reachable or not configured; 2 configuration refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from s6_common import Refused, connect_from, load_config  # noqa: E402
from s6_stage_schema import Ports  # noqa: E402


def main(argv=None, *, connect=None, out=sys.stdout):
    parser = argparse.ArgumentParser(prog='s6_bootstrap_verify.py')
    parser.add_argument('--config', required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        config = load_config(args.config)
        if connect is None:
            v = (config.get('roles') or {}).get('verify') or {}
            if not v.get('dsn_file'):
                raise Refused('roles.verify.dsn_file required')
            connect = connect_from(v['dsn_file'])
        print(json.dumps(Ports(verify_connect=connect).verify(), sort_keys=True), file=out)
        return 0
    except Refused as error:
        print(json.dumps({'ok': False, 'refused': str(error)}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - the type alone, never a message
        print(json.dumps({'ok': False, 'error': type(error).__name__}), file=out)
        return 1


if __name__ == '__main__':
    sys.exit(main())
