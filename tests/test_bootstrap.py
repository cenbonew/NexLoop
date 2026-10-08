from support.release_metadata import BOOTSTRAP_REVISION
import pytest
from eios.adapters.postgres.migrations import MigrationDrift
from eios.persistence.settings import StorageSettings, StorageConfigurationError
from nexloop_eios.bootstrap import bootstrap, verify, catalog

def test_clean_bootstrap_and_exact_reopen(admin):
    assert bootstrap(admin)==[f'{revision:04d}' for revision in range(1,int(BOOTSTRAP_REVISION)+1)]
    assert verify(admin)=={'lineage':'nexloop-eios-v1','revision':BOOTSTRAP_REVISION,'migration_count':int(BOOTSTRAP_REVISION)}
    assert bootstrap(admin)==[]
    tables=admin.execute("select tablename from pg_tables where schemaname='ontology'").fetchall()
    assert {'objects','relations','object_type_versions'} <= {r[0] for r in tables}
    assert admin.execute("select count(*) from ontology.objects").fetchone()[0]==0

def test_source_catalog_requires_published_checksums():
    assert len(catalog())==int(BOOTSTRAP_REVISION)

def test_missing_dsn_and_memory_fail_closed():
    with pytest.raises(StorageConfigurationError):StorageSettings()
    with pytest.raises(StorageConfigurationError):StorageSettings(mode='memory')
    with pytest.raises(StorageConfigurationError):StorageSettings(mode='postgres',database_url='')


def test_original_eios_roles_abort_before_schema_or_grants(admin):
    admin.execute('create role nex_eios_runtime nologin')
    with pytest.raises(MigrationDrift,match='isolated NexLoop cluster'):
        bootstrap(admin)
    assert admin.execute("select count(*) from pg_namespace where nspname in ('ontology','runtime','control')").fetchone()[0]==0
