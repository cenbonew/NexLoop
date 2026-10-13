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


def report(ack, e2e, dispatch, gate='passed'):
    return {'cells': [{'cell': 'C4-low', 'metrics': {'ack_p95_ms': ack, 'idle_dispatch_p95_ms': dispatch},
                       'decomposition': {'e2e_ms': {'p95': e2e}}, 'gate': {'result': gate}}]}


def test_compare_judges_each_p95_against_five_percent_and_the_candidate_gate():
    ok = nx052_e2e.compare(report(100, 2000, 800), report(104, 2090, 820))
    assert ok['passed'] and all(m['within'] for m in ok['metrics'].values())
    slow = nx052_e2e.compare(report(100, 2000, 800), report(106, 2000, 800))
    assert not slow['passed'] and not slow['metrics']['ack_p95_ms']['within'] and slow['metrics']['ack_p95_ms']['ratio'] == 1.06
    assert not nx052_e2e.compare(report(100, 2000, 800), report(100, 2000, 800, gate='failed'))['passed']
    missing = nx052_e2e.compare(report(100, 2000, 800), report(100, None, 800))
    assert not missing['passed'] and missing['metrics']['e2e_p95_ms']['ratio'] is None


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
