"""NX-052: read-only census of the tenant foreign keys inside a load template (CI host N runs, 2026-10-14).

Copies <template>/pgdata to a fresh temporary directory, starts that copy on its own socket (no TCP), connects as the template
cluster's bootstrap superuser (the build's initdb -U nexloop_bootstrap with local trust; overridable with --user), and prints one
JSON object: per database the number of foreign keys named *_tenant_fk, the foreign keys referencing control.nexloop_tenants,
how many of the NX-052 batch constraints (scripts/load/tenant_fk_state.py, schema-qualified) are present, and the migration
ledger (control.schema_migrations: count, head, which NX-052 batch versions are recorded). The copy is stopped and removed in
every case; the template itself is never started or written. Identifiers and numbers only.

    uv run --frozen python scripts/load/template_fk_census.py --template .ci-results/load/<run>/template
"""
import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tenant_fk_state  # noqa: E402

PG_BIN = Path(os.environ.get('NEXLOOP_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@18/bin'))
SUPERUSERS = ('nexloop_bootstrap', 'postgres')


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def batch_versions():
    """The migration version of each NX-052 batch file in this checkout ({batch: version})."""
    return {batch: next(tenant_fk_state.MIGRATIONS.glob('*' + suffix)).name.split('_', 1)[0] for batch, suffix in tenant_fk_state.BATCHES.items()}


def database_census(db, listed):
    present = {(t, n) for t, n in db.execute(tenant_fk_state.FOREIGN_KEYS).fetchall()}
    out = {'foreign_keys': len(present),
           'named_tenant_fk': db.execute(r"select count(*) from pg_constraint where contype='f' and conname like '%\_tenant\_fk'").fetchone()[0],
           'referencing_tenants': db.execute("select count(*) from pg_constraint where contype='f' and confrelid=to_regclass('control.nexloop_tenants')").fetchone()[0],
           'nx052_listed': len(listed), 'nx052_present': sum(1 for key in listed if key in present)}
    if db.execute("select to_regclass('control.schema_migrations')").fetchone()[0] is None:
        out['ledger'] = None
        return out
    versions = [v for v, in db.execute('select version from control.schema_migrations order by version').fetchall()]
    wanted = batch_versions()
    out['ledger'] = {'migrations': len(versions), 'head': versions[-1] if versions else None,
                     'nx052_batches_recorded': sorted(b for b, v in wanted.items() if v in versions),
                     'nx052_batches_absent': sorted(b for b, v in wanted.items() if v not in versions)}
    return out


def census(template, user=None, pg_bin=PG_BIN):
    import psycopg
    source = Path(template) / 'pgdata'
    if not (source / 'PG_VERSION').is_file():
        raise SystemExit(f'not a template data directory: {source}')
    root = Path(tempfile.mkdtemp(prefix='nl-census-'))
    data, sock = root / 'data', Path(tempfile.mkdtemp(prefix='nl-cs-'))
    started = False
    try:
        shutil.copytree(source, data, symlinks=True)
        data.chmod(0o700)
        (data / 'postmaster.pid').unlink(missing_ok=True)
        port = _free_port()
        subprocess.run([str(pg_bin / 'pg_ctl'), '-D', str(data), '-l', str(root / 'postgres.log'), '-o',
                        f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'], check=True, capture_output=True)
        started = True
        tried, connected = [], None
        for candidate in [u for u in (user, *SUPERUSERS, getpass.getuser()) if u]:
            if candidate in tried:
                continue
            tried.append(candidate)
            try:
                connected = psycopg.connect(f'host={sock} port={port} dbname=postgres user={candidate}', autocommit=True)
                break
            except psycopg.OperationalError:
                continue
        if connected is None:
            raise SystemExit(f'no superuser login on the copy (tried {tried}); pass --user')
        listed = [(t, n) for b in tenant_fk_state.ALL for t, n in tenant_fk_state.constraints(b)]
        login = connected.info.user
        with connected as admin:
            bootstrap_role = admin.execute('select rolname from pg_roles where oid=10').fetchone()[0]
            names = [d for d, in admin.execute('select datname from pg_database where datallowconn order by datname').fetchall()]
            version = int(admin.execute("select current_setting('server_version_num')").fetchone()[0])
            databases = {}
            for name in names:
                with psycopg.connect(f'host={sock} port={port} dbname={name} user={login}', autocommit=True) as db:
                    databases[name] = database_census(db, listed)
        return {'template': str(Path(template)), 'superuser': login, 'bootstrap_role': bootstrap_role, 'server_version_num': version,
                'checkout_nx052_batch_versions': batch_versions(), 'databases': databases}
    finally:
        if started:
            subprocess.run([str(pg_bin / 'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(sock, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--template', required=True, help='a load template directory (contains pgdata/)')
    parser.add_argument('--user', default=None, help='superuser of the template cluster (default: nexloop_bootstrap, then postgres, then the OS user)')
    args = parser.parse_args(argv)
    print(json.dumps(census(args.template, args.user), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
