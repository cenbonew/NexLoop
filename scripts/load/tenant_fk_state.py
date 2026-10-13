"""NX-052 full-window measurement helpers (disposable load clusters only; implementation note §11).

    drop   remove the tenant foreign keys of whole batches ('all' = every NX-052 batch) from a disposable cluster: a cell's clone
           (NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all in stack.py, the v2 measurement, note §11) or a template before its snapshot
           (NEXLOOP_LOAD_TENANT_FK_DROP in build_template_hold.py);
    pulse  hold the tenant row FOR NO KEY UPDATE (the lock configure_manifest and initial_identity_create take since 0162) for
           --hold seconds every --interval seconds, emulating a configuration publication during a cell:
           NEXLOOP_LOAD_TENANT_LOCK_PULSE=5,0.2 in stack.py.

Batches are named by the suffix of their migration file, which survives renumbering at integration.
"""
import argparse
import re
import time
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parents[2] / 'packages/eios-core/src/eios/migrations'
BATCHES = {'2': '_nx052_tenant_fk_control_config.sql', '3a': '_nx052_tenant_fk_ontology_definitions.sql',
           '3b': '_nx052_tenant_fk_claims_extraction.sql', '3c': '_nx052_tenant_fk_conversations.sql', '4': '_nx052_tenant_fk_run_effect.sql',
           '4b1': '_nx052_tenant_fk_control_events.sql', '4b2': '_nx052_tenant_fk_commercial_erasure.sql', '5': '_nx052_tenant_fk_durable_queue.sql',
           'realms': '_nx052_tenant_fk_token_realms.sql'}
ALL = tuple(BATCHES)


def expand(batches):
    """'all' stands for every NX-052 tenant foreign key batch; unknown names are refused."""
    names = [b for item in batches for b in (ALL if item == 'all' else (item,))]
    unknown = [b for b in names if b not in BATCHES]
    if unknown:
        raise ValueError(f'unknown NX-052 batch: {unknown}')
    return list(dict.fromkeys(names))


def constraints(batch):
    path = next(MIGRATIONS.glob('*' + BATCHES[batch]))
    return re.findall(r'^alter table (\S+) add constraint (\S+) foreign key', path.read_text(), re.M)


class TenantFkMismatch(Exception):
    """A constraint an NX-052 batch file adds is not in the cluster (renamed, rebuilt or already dropped): nothing is dropped."""

    def __init__(self, missing, cluster=None):
        super().__init__(f'{len(missing)} NX-052 tenant foreign keys not found')
        self.missing = missing
        self.cluster = cluster or {}


class TemplateKeysDropped(Exception):
    """The template was built with NEXLOOP_LOAD_TENANT_FK_DROP: its clones have no NX-052 tenant keys to drop."""

    def __init__(self, batches):
        super().__init__('the template was built without the NX-052 tenant foreign keys')
        self.batches = batches


# Schema-qualified from the catalogs, never conrelid::regclass::text: that text drops the schema of any table whose schema is on the
# session's search_path, so a search_path set by the environment (PGOPTIONS, a role or server setting) made every key look
# missing on the CI host (2026-10-13, run 20261013T161753Z).
FOREIGN_KEYS = ("select n.nspname||'.'||c.relname,k.conname from pg_constraint k join pg_class c on c.oid=k.conrelid "
                "join pg_namespace n on n.oid=c.relnamespace where k.contype='f'")


def missing(db, batches):
    """The batches' constraints (schema identifiers only) that the cluster does not have, as 'schema.table.constraint'."""
    present = {(t, n) for t, n in db.execute(FOREIGN_KEYS).fetchall()}
    return [f'{t}.{n}' for b in expand(batches) for t, n in constraints(b) if (t, n) not in present]


def cluster_facts(db):
    """What the check ran against (no data): the database, the session's search_path, the server version and the number of
    foreign keys referencing the tenant table."""
    database, search_path, version = db.execute("select current_database(),current_setting('search_path'),current_setting('server_version_num')").fetchone()
    tenant_keys = db.execute("select count(*) from pg_constraint where contype='f' and confrelid=to_regclass('control.nexloop_tenants')").fetchone()[0]
    return {'database': database, 'search_path': search_path, 'server_version_num': int(version), 'tenant_foreign_keys': tenant_keys}


def drop(db, batches):
    """Drop the batches' tenant foreign keys, all or none: every constraint is checked first, then all are dropped in one
    transaction."""
    absent = missing(db, batches)
    if absent:
        raise TenantFkMismatch(absent, cluster_facts(db))
    dropped = []
    with db.transaction():
        for batch in expand(batches):
            for table, name in constraints(batch):
                db.execute(f'alter table {table} drop constraint {name}')
                dropped.append(name)
    return dropped


def pulse(dsn, tenant, interval, hold, once=False):
    with psycopg.connect(dsn) as db:
        while True:
            with db.transaction():
                db.execute('select 1 from control.nexloop_tenants where tenant_id=%s for no key update', (tenant,))
                time.sleep(hold)
            if once:
                return
            time.sleep(max(0.0, interval - hold))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('pulse')
    p.add_argument('--dsn-file', required=True, type=Path)
    p.add_argument('--tenant', required=True)
    p.add_argument('--interval', type=float, default=5.0)
    p.add_argument('--hold', type=float, default=0.2)
    args = parser.parse_args()
    pulse(args.dsn_file.read_text().strip(), args.tenant, args.interval, args.hold)


if __name__ == '__main__':
    main()
