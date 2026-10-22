#!/usr/bin/env python3
"""E1 grant drift (docs/design/eios-capability-gap.md): a live database's privileges against the ones its migrations give.

  expected --out FILE        bootstrap a disposable cluster from this checkout's migration catalog (as check_definer_search_path.py
                             does) and write its privilege snapshot (nexloop-grant-snapshot/1). Run it on the release's commit.
  check --database-url-file F --expected FILE [--json]
                             read the live database's privileges (one catalog query in a read-only transaction; any role can
                             read the catalog) and list what is missing and what is extra. Exit 0 = they match, 1 = drift, or the
                             live catalog revision is not the snapshot's or cannot be read (a role without SELECT on
                             control.schema_migrations, such as nexloop_metrics, needs --allow-unknown-revision), 2 = usage or
                             connection error.

Nothing is written to the database. Reviewed differences are listed in nexloop_eios.grant_drift.IGNORED with their reasons.
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


def expected(out, dsn=None):
    import psycopg
    from check_definer_search_path import disposable
    from nexloop_eios import grant_drift as G
    from nexloop_eios.bootstrap import bootstrap, catalog

    def run(url):
        with psycopg.connect(url, autocommit=True) as connection:
            if dsn is None:
                bootstrap(connection)
            return G.snapshot(connection)
    entries = run(dsn) if dsn else disposable(run)
    out.write_text(json.dumps(G.document(entries, catalog_revision=catalog()[-1].version), indent=0) + '\n')
    return len(entries)


def live_revision(connection):
    import psycopg
    try:
        with connection.transaction():
            connection.execute('set transaction read only')
            return connection.execute('select max(version) from control.schema_migrations').fetchone()[0]
    except psycopg.errors.InsufficientPrivilege:
        return None  # the reading role may not see the migration ledger: the revision is then reported as unknown


def check(database_url_file, expected_file, *, allow_unknown_revision=False):
    import psycopg
    from nexloop_eios import grant_drift as G
    from nexloop_eios.private_configuration import read_private_text
    snapshot = G.load(expected_file)
    with psycopg.connect(read_private_text(database_url_file, maximum=4096).strip(), autocommit=True, connect_timeout=5) as connection:
        revision = live_revision(connection)
        actual = G.snapshot(connection)
    result = G.compare(snapshot['entries'], actual)
    result.update(expected_revision=snapshot['catalog_revision'], live_revision=revision,
                  revision_match=revision == snapshot['catalog_revision'] or (revision is None and allow_unknown_revision),
                  counts={'expected': len(snapshot['entries']), 'live': len(actual)})
    result['passed'] = result['match'] and result['revision_match']
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    e = commands.add_parser('expected')
    e.add_argument('--out', type=pathlib.Path, required=True)
    e.add_argument('--dsn', help='an already bootstrapped disposable database (instead of a new cluster)')
    c = commands.add_parser('check')
    c.add_argument('--database-url-file', type=pathlib.Path, required=True)
    c.add_argument('--expected', type=pathlib.Path, required=True)
    c.add_argument('--json', action='store_true')
    c.add_argument('--allow-unknown-revision', action='store_true',
                   help='the reading role cannot see control.schema_migrations (the metrics role): compare without the revision check')
    args = parser.parse_args(argv)
    try:
        if args.command == 'expected':
            print(f'grant snapshot written: {expected(args.out, args.dsn)} entries')
            return 0
        result = check(args.database_url_file, args.expected, allow_unknown_revision=args.allow_unknown_revision)
    except Exception as error:
        print(f'grant drift: {type(error).__name__}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        for item in result['missing']:
            print('MISSING', item)
        for item in result['extra']:
            print('EXTRA', item)
        for item in result['ignored']:
            print('ignored', item['side'], item['entry'], '-', item['reason'])
        if not result['revision_match']:
            print(f"REVISION live {result['live_revision']} != snapshot {result['expected_revision']}")
        print(f"grant drift: {'match' if result['passed'] else 'DRIFT'} ({len(result['missing'])} missing, {len(result['extra'])} extra, "
              f"{len(result['ignored'])} ignored; {result['counts']['expected']} expected, {result['counts']['live']} live; "
              f"revision {result['live_revision'] or 'unknown'})")
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
