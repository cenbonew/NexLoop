"""NX-052 v2 end-to-end measurement helpers (implementation note §11): the per-state matrix, the report comparison, the batch
names and the cell-level drop of the tenant keys on a disposable clone."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/load'))
import matrix as runner  # noqa: E402
import nx052_e2e  # noqa: E402
import tenant_fk_state  # noqa: E402


def test_state_matrix_keeps_every_cell_and_sets_only_the_measured_one(tmp_path):
    base = ROOT / 'deploy/load/matrix.v1.json'
    out = tmp_path / 'm.json'
    assert nx052_e2e.main(['matrix', '--base', str(base), '--rate', '0.1', '--out', str(out)]) == 0
    derived, original = json.loads(out.read_text()), json.loads(base.read_text())
    runner.validate_matrix(derived)  # matrix.py still accepts it
    changed = [c for c, o in zip(derived['cells'], original['cells']) if c != o]
    assert changed == [{**next(o for o in original['cells'] if o['cell'] == 'C4-low'), 'rate_per_s': 0.1, 'host_concurrency': 4}]
    assert derived['timing'] == original['timing'] and derived['nx052']['rate_per_s'] == 0.1
    with pytest.raises(ValueError):
        nx052_e2e.matrix(base, 1.5)


def report(ack, e2e, dispatch, gate='passed', failing=(), absent=()):
    items = {name: {'result': 'fail' if name in failing else 'pass'} for name in nx052_e2e.REQUIRED_GATE_ITEMS + ('idle_dispatch_p95_ms', 'ack_p95_ms', 'guard_p95_ms')
             if name not in absent}
    items.update({name: {'result': 'fail'} for name in failing})
    return {'cells': [{'cell': 'C4-low', 'metrics': {'ack_p95_ms': ack, 'idle_dispatch_p95_ms': dispatch},
                       'decomposition': {'e2e_ms': {'p95': e2e}}, 'gate': {'result': gate, 'items': items}}]}


def test_compare_judges_each_p95_against_five_percent_and_the_required_gate_items():
    ok = nx052_e2e.compare(report(100, 2000, 800), report(104, 2090, 820))
    assert ok['passed'] and all(m['within'] for m in ok['metrics'].values())
    slow = nx052_e2e.compare(report(100, 2000, 800), report(106, 2000, 800))
    assert not slow['passed'] and not slow['metrics']['ack_p95_ms']['within'] and slow['metrics']['ack_p95_ms']['ratio'] == 1.06
    missing = nx052_e2e.compare(report(100, 2000, 800), report(100, None, 800))
    assert not missing['passed'] and missing['metrics']['e2e_p95_ms']['ratio'] is None


def test_at_0_1_per_second_the_cell_gate_verdict_is_not_required_but_correctness_items_are():
    """Owner 2026-10-14 (ROADMAP §7): the idle dispatch 5 s gate is set for 0.05/s; at 0.1/s the cell's AT-060 verdict may fail on the
    latency gates, while identity, unresolved work, timeout rate, connections, critical alerts and unexpected refusals must pass."""
    latency_only = nx052_e2e.compare(report(100, 2000, 6500), report(100, 2000, 6600, gate='failed', failing=('idle_dispatch_p95_ms', 'ack_p95_ms')))
    assert latency_only['passed'] and latency_only['gate']['candidate'] == 'failed'
    assert set(latency_only['required_gate_items']) == set(nx052_e2e.REQUIRED_GATE_ITEMS)
    for name in nx052_e2e.REQUIRED_GATE_ITEMS:
        failed = nx052_e2e.compare(report(100, 2000, 800), report(100, 2000, 800, gate='failed', failing=(name,)))
        assert not failed['passed'] and failed['required_gate_items'][name] == 'fail', name
        absent = nx052_e2e.compare(report(100, 2000, 800), report(100, 2000, 800, absent=(name,)))
        assert not absent['passed'] and absent['required_gate_items'][name] == 'missing', name


def test_cli_exit_code_follows_the_verdict(tmp_path):
    a, b = tmp_path / 'a.json', tmp_path / 'b.json'
    a.write_text(json.dumps(report(100, 2000, 800)))
    b.write_text(json.dumps(report(120, 2000, 800)))
    assert nx052_e2e.main(['compare', '--baseline', str(a), '--candidate', str(b)]) == 1
    assert nx052_e2e.main(['compare', '--baseline', str(a), '--candidate', str(a)]) == 0


def test_every_nx052_foreign_key_migration_is_a_named_batch():
    files = sorted(p.name for p in (ROOT / 'packages/eios-core/src/eios/migrations').glob('*_nx052_tenant_fk_*.sql'))
    assert sorted(f[4:] for f in files) == sorted(tenant_fk_state.BATCHES.values())
    assert tenant_fk_state.expand(['all']) == list(tenant_fk_state.BATCHES)
    assert all(tenant_fk_state.constraints(b) for b in tenant_fk_state.BATCHES)
    with pytest.raises(ValueError):
        tenant_fk_state.expand(['3d'])


def test_cell_drop_removes_every_tenant_key_on_a_clone_and_nothing_else(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    count = lambda: admin.execute("select count(*) from pg_constraint where contype='f' and confrelid='control.nexloop_tenants'::regclass").fetchone()[0]  # noqa: E731
    before = count()
    dropped = tenant_fk_state.drop(admin, ['all'])
    # The keys predating NX-052 (0004, 0008, 0040, 0048, 0050) stay; every NX-052 batch key goes.
    assert len(dropped) == sum(len(tenant_fk_state.constraints(b)) for b in tenant_fk_state.BATCHES) and count() == before - len(dropped) > 0


def test_stack_drops_the_keys_on_its_clone_only_when_asked(admin, pg, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nexloop_eios.bootstrap import bootstrap
    import stack
    bootstrap(admin)
    template = tmp_path / 'template'
    (template / 'private').mkdir(parents=True)
    (template / 'private/chain.json').write_text(json.dumps({'tenant': '00000000-0000-4000-8000-00000000000a'}))
    count = lambda: admin.execute("select count(*) from pg_constraint where contype='f' and confrelid='control.nexloop_tenants'::regclass").fetchone()[0]  # noqa: E731
    before = count()
    monkeypatch.delenv('NEXLOOP_LOAD_TENANT_FK_DROP_CELL', raising=False)
    stack.Stack(SimpleNamespace(dsn=pg), template, tmp_path / 'lane-a', 'C4-low')
    assert count() == before and not (tmp_path / 'lane-a/tenant-fk-state.json').exists()
    monkeypatch.setenv('NEXLOOP_LOAD_TENANT_FK_DROP_CELL', 'all')
    stack.Stack(SimpleNamespace(dsn=pg), template, tmp_path / 'lane-b', 'C4-low')
    state = json.loads((tmp_path / 'lane-b/tenant-fk-state.json').read_text())
    assert state['dropped_batches'] == ['all'] and count() == before - state['dropped_constraints']


def test_a_mismatched_batch_drops_nothing_and_the_cell_records_what_is_missing(admin, pg, tmp_path, monkeypatch):
    """N state on a clone whose keys do not match the batch files (a key renamed, rebuilt or already dropped): nothing is
    dropped, the lane file and the cell record the missing constraint identifiers (schema names only), and the cell's error
    names the exception type instead of a bare runner_error."""
    from types import SimpleNamespace
    from nexloop_eios.bootstrap import bootstrap
    import stack
    bootstrap(admin)
    table, name = tenant_fk_state.constraints('5')[0]
    admin.execute(f'alter table {table} drop constraint {name}')
    count = lambda: admin.execute("select count(*) from pg_constraint where contype='f' and confrelid='control.nexloop_tenants'::regclass").fetchone()[0]  # noqa: E731
    before = count()
    assert tenant_fk_state.missing(admin, ['all']) == [f'{table}.{name}']
    with pytest.raises(tenant_fk_state.TenantFkMismatch):
        tenant_fk_state.drop(admin, ['all'])
    assert count() == before  # all or nothing
    template = tmp_path / 'template'
    (template / 'private').mkdir(parents=True)
    (template / 'private/chain.json').write_text(json.dumps({'tenant': '00000000-0000-4000-8000-00000000000a'}))
    monkeypatch.setenv('NEXLOOP_LOAD_TENANT_FK_DROP_CELL', 'all')
    lane = tmp_path / 'lane'
    with pytest.raises(tenant_fk_state.TenantFkMismatch) as raised:
        stack.Stack(SimpleNamespace(dsn=pg), template, lane, {'cell': 'C4-low'})
    state = json.loads((lane / 'tenant-fk-state.json').read_text())
    assert {k: v for k, v in state.items() if k != 'cluster'} == {'dropped_batches': ['all'], 'dropped_constraints': 0, 'missing': [f'{table}.{name}']}
    assert state['cluster']['database'] == 'postgres' and state['cluster']['tenant_foreign_keys'] > 0
    cell = runner.Cell(next(c for c in json.loads((ROOT / 'deploy/load/matrix.v2.json').read_text())['cells'] if c['cell'] == 'C4-low'),
                       json.loads((ROOT / 'deploy/load/matrix.v2.json').read_text()), SimpleNamespace(out=str(tmp_path / 'out'), lane_root=str(tmp_path), timing_scale=1.0), {'seed': 1})
    cell.lane = lane
    result = cell.abort('runner_error', raised.value)
    assert result['error']['chain'] == [{'type': 'tenant_fk_state.TenantFkMismatch'}] and result['tenant_fk_state']['missing'] == [f'{table}.{name}']
    monkeypatch.setenv('NEXLOOP_LOAD_TENANT_FK_DROP_CELL', 'N')  # an unknown batch name is refused and recorded too
    with pytest.raises(ValueError):
        stack.Stack(SimpleNamespace(dsn=pg), template, tmp_path / 'lane-2', {'cell': 'C4-low'})
    assert json.loads((tmp_path / 'lane-2' / 'tenant-fk-state.json').read_text())['refused'] == 'unknown_batch'


def test_the_key_check_does_not_depend_on_the_session_search_path(admin):
    """CI host 2026-10-13 (run 20261013T161753Z): with the schemas on the session's search_path (an environment setting such as
    PGOPTIONS), conrelid::regclass::text dropped the schema and every key looked missing. The check now compares schema-qualified
    names from the catalogs: the same session finds every key and drops them; a real mismatch still reports cluster facts."""
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    admin.execute("set search_path=control,ontology,runtime,authz,public")
    assert admin.execute("select 'nexloop_tenants'::regclass::text").fetchone()[0] == 'nexloop_tenants'  # the trap itself
    assert tenant_fk_state.missing(admin, ['all']) == []
    table, name = tenant_fk_state.constraints('5')[0]
    admin.execute(f'alter table {table} drop constraint {name}')
    with pytest.raises(tenant_fk_state.TenantFkMismatch) as raised:
        tenant_fk_state.drop(admin, ['all'])
    assert raised.value.missing == [f'{table}.{name}']
    facts = raised.value.cluster
    assert facts['database'] == 'postgres' and facts['search_path'].startswith('control') and facts['tenant_foreign_keys'] > 0
    assert set(facts) == {'database', 'search_path', 'server_version_num', 'tenant_foreign_keys'}
    dropped = tenant_fk_state.drop(admin, [b for b in tenant_fk_state.ALL if b != '5'])
    assert len(dropped) == sum(len(tenant_fk_state.constraints(b)) for b in tenant_fk_state.ALL if b != '5')


def test_a_template_built_without_the_keys_is_refused_with_that_cause(admin, pg, tmp_path, monkeypatch):
    """CI host N runs 2026-10-13/14 (run 20261013T191340Z): TenantFkMismatch with all 134 missing in database 'postgres' with only
    8 tenant keys, the ones predating NX-052. Reproduced: a template built with NEXLOOP_LOAD_TENANT_FK_DROP has no NX-052 keys, so
    its clones have none to drop. The cell now refuses with that cause before touching the clone; nothing is dropped."""
    from types import SimpleNamespace
    from nexloop_eios.bootstrap import bootstrap
    import stack
    bootstrap(admin)
    count = lambda: admin.execute("select count(*) from pg_constraint where contype='f' and confrelid='control.nexloop_tenants'::regclass").fetchone()[0]  # noqa: E731
    before = count()
    template = tmp_path / 'template'
    (template / 'private').mkdir(parents=True)
    (template / 'private/chain.json').write_text(json.dumps({'tenant': '00000000-0000-4000-8000-00000000000a'}))
    (template / 'template.json').write_text(json.dumps({'tenant_fk_dropped': {'batches': ['all'], 'constraints': ['x']}}))
    monkeypatch.setenv('NEXLOOP_LOAD_TENANT_FK_DROP_CELL', 'all')
    lane = tmp_path / 'lane'
    with pytest.raises(tenant_fk_state.TemplateKeysDropped):
        stack.Stack(SimpleNamespace(dsn=pg), template, lane, {'cell': 'C4-low'})
    assert json.loads((lane / 'tenant-fk-state.json').read_text()) == {'dropped_batches': ['all'], 'dropped_constraints': 0,
                                                                       'refused': 'template_keys_already_dropped', 'template_dropped_batches': ['all']}
    assert count() == before
    lane_script = (ROOT / 'scripts/load/run_on_lane.sh').read_text()
    assert 'NEXLOOP_LOAD_TENANT_FK_DROP and NEXLOOP_LOAD_TENANT_FK_DROP_CELL are alternatives' in lane_script


def test_a_database_without_the_keys_reports_its_name_and_the_right_one_drops_all(admin, pg):
    """Dispatcher 2026-10-14: checked against a database that does not hold the NX-052 keys, the mismatch names that database
    (cluster facts, no data); against the clone's database every one of the 134 keys is dropped."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    import psycopg
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    admin.execute('create database nx052_elsewhere')
    try:
        with psycopg.connect(make_conninfo(**{**conninfo_to_dict(pg), 'dbname': 'nx052_elsewhere'}), autocommit=True) as other:
            with pytest.raises(tenant_fk_state.TenantFkMismatch) as raised:
                tenant_fk_state.drop(other, ['all'])
        assert len(raised.value.missing) == sum(len(tenant_fk_state.constraints(b)) for b in tenant_fk_state.ALL)
        assert raised.value.cluster['database'] == 'nx052_elsewhere' and raised.value.cluster['tenant_foreign_keys'] == 0
    finally:
        admin.execute('drop database nx052_elsewhere')
    assert admin.execute('select current_database()').fetchone()[0] == 'postgres'
    assert len(tenant_fk_state.drop(admin, ['all'])) == sum(len(tenant_fk_state.constraints(b)) for b in tenant_fk_state.ALL) == 134


def test_the_template_census_reads_a_copy_and_counts_the_nx052_keys(admin, pg, tmp_path):
    """scripts/load/template_fk_census.py (CI host N runs 2026-10-14): on a copy of a template's pgdata, started on its own socket
    with the bootstrap superuser, it counts per database the *_tenant_fk keys, the keys referencing the tenant table, the NX-052
    batch keys present and the ledger's NX-052 batch versions; the copy is removed and the template never touched."""
    import glob
    import tempfile
    import load_template
    import template_fk_census
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    full = tmp_path / 'full'
    full.mkdir()
    load_template.snapshot(pg, full)
    before = sorted(glob.glob(tempfile.gettempdir() + '/nl-census-*'))
    result = template_fk_census.census(full)
    assert sorted(glob.glob(tempfile.gettempdir() + '/nl-census-*')) == before and not (full / 'pgdata/postmaster.pid').exists()
    assert result['superuser'] == result['bootstrap_role'] == 'nexloop_bootstrap'
    db = result['databases']['postgres']
    assert db['nx052_listed'] == db['nx052_present'] == 134 and db['referencing_tenants'] == 142 and db['named_tenant_fk'] >= 134
    assert db['ledger']['nx052_batches_absent'] == [] and len(db['ledger']['nx052_batches_recorded']) == len(tenant_fk_state.BATCHES)
    tenant_fk_state.drop(admin, ['5'])
    dropped = tmp_path / 'dropped'
    dropped.mkdir()
    load_template.snapshot(pg, dropped)
    db = template_fk_census.census(dropped)['databases']['postgres']
    assert db['nx052_present'] == 134 - len(tenant_fk_state.constraints('5')) and db['ledger']['nx052_batches_absent'] == []
    assert template_fk_census.main(['--template', str(dropped)]) == 0


def test_the_cell_variable_drops_the_keys_in_a_matrix_cell_only_never_in_the_template_build(admin, pg, tmp_path, monkeypatch):
    """CI host N runs 2026-10-13/14: with NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all set for the whole lane run, the template build's
    history chain (a Stack on the template cluster, spec {}) dropped the keys from the template itself. With the variable set, the
    template's Stack keeps all 134 keys; a matrix cell's Stack (a spec naming its cell) drops all 134 on its clone."""
    from types import SimpleNamespace
    from nexloop_eios.bootstrap import bootstrap
    import stack
    bootstrap(admin)
    template = tmp_path / 'template'
    (template / 'private').mkdir(parents=True)
    (template / 'private/chain.json').write_text(json.dumps({'tenant': '00000000-0000-4000-8000-00000000000a'}))
    monkeypatch.setenv('NEXLOOP_LOAD_TENANT_FK_DROP_CELL', 'all')
    stack.Stack(SimpleNamespace(dsn=pg), template, tmp_path / 'history-lane', {})  # the template build's history chain
    assert tenant_fk_state.missing(admin, ['all']) == [] and not (tmp_path / 'history-lane/tenant-fk-state.json').exists()
    stack.Stack(SimpleNamespace(dsn=pg), template, tmp_path / 'cell-lane', {'cell': 'C4-low'})
    assert len(tenant_fk_state.missing(admin, ['all'])) == 134
    assert json.loads((tmp_path / 'cell-lane/tenant-fk-state.json').read_text()) == {'dropped_batches': ['all'], 'dropped_constraints': 134}


def test_a_clone_without_any_of_the_keys_is_refused_as_a_template_lacking_them(admin, pg, tmp_path, monkeypatch):
    """Whatever removed them (the template.json record is absent here): a cell whose clone has none of the batches' keys is
    refused with TemplateKeysDropped, the template lacks the keys; nothing else is touched."""
    from types import SimpleNamespace
    from nexloop_eios.bootstrap import bootstrap
    import stack
    bootstrap(admin)
    tenant_fk_state.drop(admin, ['all'])
    template = tmp_path / 'template'
    (template / 'private').mkdir(parents=True)
    (template / 'private/chain.json').write_text(json.dumps({'tenant': '00000000-0000-4000-8000-00000000000a'}))
    monkeypatch.setenv('NEXLOOP_LOAD_TENANT_FK_DROP_CELL', 'all')
    with pytest.raises(tenant_fk_state.TemplateKeysDropped, match='lacks the NX-052 tenant foreign keys'):
        stack.Stack(SimpleNamespace(dsn=pg), template, tmp_path / 'lane', {'cell': 'C4-low'})
    state = json.loads((tmp_path / 'lane/tenant-fk-state.json').read_text())
    assert state['refused'] == 'template_keys_missing' and state['missing'] == 134 and state['dropped_constraints'] == 0


def test_the_template_build_refuses_the_cell_variable_and_the_lane_withholds_it(tmp_path):
    import os
    import subprocess
    run = subprocess.run(['bash', str(ROOT / 'scripts/load/build_template.sh'), '--out', str(tmp_path / 'template')], capture_output=True, text=True,
                         env={**os.environ, 'NEXLOOP_LOAD_TENANT_FK_DROP_CELL': 'all'}, timeout=60)
    assert run.returncode == 2 and 'applies to matrix cells only' in run.stderr and not (tmp_path / 'template').exists()
    lane = (ROOT / 'scripts/load/run_on_lane.sh').read_text()
    template_step = next(line for line in lane.splitlines() if 'step_run template' in line)
    assert 'env -u NEXLOOP_LOAD_TENANT_FK_DROP_CELL' in template_step
    assert 'env -u NEXLOOP_LOAD_TENANT_FK_DROP_CELL' not in next(line for line in lane.splitlines() if 'step_run matrix' in line)
    assert "NEXLOOP_LOAD_TENANT_FK_DROP_CELL'), 'NEXLOOP_LOAD_TENANT_FK_DROP_CELL applies to matrix cells only" in (ROOT / 'scripts/load/build_template_hold.py').read_text()
