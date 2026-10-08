"""Exact independent lineage, bootstrap-only administrative entry point."""
from importlib import resources
import json
from eios.adapters.postgres.migrations import load_migrations, apply_migrations, MigrationDrift

def catalog():
    manifest = json.loads(resources.files('eios.migrations').joinpath('catalog.json').read_text())
    if manifest['lineage'] != 'nexloop-eios-v1':
        raise MigrationDrift('unknown bootstrap lineage')
    migrations = load_migrations()
    actual = [{'version':m.version, 'name':f'{m.version}_{m.name}.sql', 'sha256':m.checksum} for m in migrations]
    if actual != manifest['migrations']:
        raise MigrationDrift('bootstrap source checksum mismatch')
    return migrations

def bootstrap(connection, *, role=None):
    """For new isolated NexLoop databases only; never the original EIOS database."""
    # Upstream 0001 contains conditional grants to original EIOS roles. Those
    # roles must never receive authority in the new independent lineage.
    original_roles = connection.execute(
        "select rolname from pg_roles where rolname in ('nex_eios_app','nex_eios_runtime')"
    ).fetchall()
    if original_roles:
        raise MigrationDrift('original EIOS roles present; use an isolated NexLoop cluster')
    return apply_migrations(connection, role=role, migrations=catalog())

def verify(connection):
    expected = [(m.version, m.checksum) for m in catalog()]
    actual = connection.execute('select version, checksum from control.schema_migrations order by version').fetchall()
    if list(actual) != expected:
        raise MigrationDrift('NexLoop database lineage is not exact')
    return {'lineage':'nexloop-eios-v1', 'revision':expected[-1][0], 'migration_count':len(expected)}
