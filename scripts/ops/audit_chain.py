#!/usr/bin/env python3
"""NX-059 audit hash chain operations (docs/design/audit-hash-chain.md). Run on the data host with an owner-capable DSN file (the
nexloop_bootstrap socket DSN, as the other data-host tools); the seal key file is private (0600) and is never printed.

  seal          --database-url-file F --key-file K --tenant T [--tenant ...]   append the pending audit rows, then seal each head
  verify        --database-url-file F --key-file K --tenant T                  the database's verification plus every seal's MAC
  export        --database-url-file F --tenant T --out DIR                     chain.jsonl, seals.jsonl, manifest.json (new directory)
  verify-export --key-file K --dir DIR                                         offline: digests, links, rows, pruned entries, seals

Exit 0 = valid / done, 1 = a problem was found, 2 = usage or connection error. Output: codes, sequence numbers and keys only.
"""
import argparse
import json
import pathlib
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('seal', 'verify', 'export', 'verify-export'):
        c = commands.add_parser(name)
        if name != 'verify-export':
            c.add_argument('--database-url-file', type=pathlib.Path, required=True)
            c.add_argument('--tenant', action='append', required=True)
        if name != 'export':
            c.add_argument('--key-file', type=pathlib.Path, required=True)
        if name == 'export':
            c.add_argument('--out', type=pathlib.Path, required=True)
        if name == 'verify-export':
            c.add_argument('--dir', type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    from nexloop_eios import audit_chain as A
    try:
        keys = A.load_keys(args.key_file) if args.command != 'export' else None
        if args.command == 'verify-export':
            result = A.verify_export(args.dir, keys)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['valid'] else 1
        import psycopg
        from nexloop_eios.private_configuration import read_private_text
        with psycopg.connect(read_private_text(args.database_url_file, maximum=4096).strip(), autocommit=True, connect_timeout=5) as c:
            if args.command == 'seal':
                for tenant in args.tenant:
                    print(json.dumps(A.seal(c, tenant, keys) or {'tenant_id': tenant, 'seq': 0}))
                return 0
            if args.command == 'export':
                if len(args.tenant) != 1:
                    raise ValueError('one tenant per export')
                print(json.dumps(A.export(c, args.tenant[0], args.out)))
                return 0
            results = [A.verify(c, tenant, keys) for tenant in args.tenant]
    except Exception as error:
        print(f'audit chain: {type(error).__name__}', file=sys.stderr)
        return 2
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if all(r['valid'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
