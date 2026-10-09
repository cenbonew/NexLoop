#!/usr/bin/env python3
"""Fail closed on temporary-schema exposure in a bootstrapped NexLoop database.

Checks (after bootstrap of a disposable cluster, or against --dsn):
  * every application-schema routine that sets search_path lists pg_temp last;
  * every application-schema SECURITY DEFINER routine sets search_path;
  * PUBLIC and the runtime roles have no TEMPORARY privilege; nexloop_owner has it.
Extension-owned routines are out of scope.
"""
import argparse
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile

RUNTIME_ROLES = ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler',
                 'nexloop_runtime', 'nexloop_identity', 'nexloop_configurator')
ROUTINES = """
select p.oid::regprocedure::text, p.prosecdef,
       (select c from pg_catalog.unnest(p.proconfig) c where c like 'search\\_path=%%')
from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid=p.pronamespace
where n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg\\_toast%%' and n.nspname not like 'pg\\_temp%%'
  and not exists(select 1 from pg_catalog.pg_depend d where d.classid='pg_catalog.pg_proc'::regclass and d.objid=p.oid and d.deptype='e')
order by 1
"""


def problems(connection):
    found = []; summary = {'routines_with_search_path': 0, 'security_definer': 0}
    for signature, definer, setting in connection.execute(ROUTINES).fetchall():
        summary['security_definer'] += bool(definer)
        if setting is None:
            if definer:found.append(f'{signature}: SECURITY DEFINER without search_path')
            continue
        summary['routines_with_search_path'] += 1
        entries = [e.strip().strip('"') for e in setting.split('=', 1)[1].split(',')]
        if entries[-1] != 'pg_temp' or entries.count('pg_temp') != 1:
            found.append(f'{signature}: {setting} (pg_temp must be listed last)')
    database = connection.execute('select current_database()').fetchone()[0]
    acl = connection.execute('select coalesce(datacl::text[],array[]::text[]) from pg_catalog.pg_database where datname=current_database()').fetchone()[0]
    if not acl or any(entry.startswith('=') and 'T' in entry.split('=', 1)[1].split('/')[0] for entry in acl):
        found.append(f'database {database}: PUBLIC has TEMPORARY')
    for role in RUNTIME_ROLES:
        row = connection.execute("select has_database_privilege(%s,current_database(),'TEMPORARY') from pg_catalog.pg_roles where rolname=%s", (role, role)).fetchone()
        if row and row[0]:found.append(f'role {role}: has TEMPORARY')
    if not connection.execute("select has_database_privilege('nexloop_owner',current_database(),'TEMPORARY')").fetchone()[0]:
        found.append('role nexloop_owner: TEMPORARY missing')
    return found, summary


def disposable(callback):
    bindir = pathlib.Path(os.environ.get('NEXLOOP_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@18/bin'))
    root = pathlib.Path(tempfile.mkdtemp(prefix='nexloop-pgpath-')); data = root / 'data'; sock = root / 's'; sock.mkdir()
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    subprocess.run([str(bindir / 'initdb'), '-D', str(data), '-U', 'nexloop_bootstrap', '--auth=trust', '--no-locale', '-E', 'UTF8'], check=True, stdout=subprocess.DEVNULL)
    started = False
    try:
        subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-l', str(root / 'pg.log'), '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, stdout=subprocess.DEVNULL)
        started = True
        return callback(f'host={sock} port={port} dbname=postgres user=nexloop_bootstrap')
    finally:
        if started:subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'], check=True, stdout=subprocess.DEVNULL)
        assert root.name.startswith('nexloop-pgpath-') and root.parent == pathlib.Path(tempfile.gettempdir())
        shutil.rmtree(root)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--dsn', help='an already bootstrapped disposable database')
    args = parser.parse_args()
    import psycopg
    def run(dsn):
        with psycopg.connect(dsn, autocommit=True) as connection:
            if not args.dsn:
                from nexloop_eios.bootstrap import bootstrap
                bootstrap(connection)
            return problems(connection)
    found, summary = run(args.dsn) if args.dsn else disposable(run)
    for line in found:print('FAIL', line)
    print(f"definer search_path check: {summary['routines_with_search_path']} routines pin search_path, "
          f"{summary['security_definer']} SECURITY DEFINER, {len(found)} problems")
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
