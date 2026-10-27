"""NX-031 slice 1: the matrix file is the design's eight cells with only C4 and C4-low as gates; the runner refuses anything
else before touching a cluster, and refuses REAL_DISPATCH_ENABLED=true."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest
from support.absence import absent

ROOT=Path(__file__).resolve().parents[1]
MATRIX=json.loads((ROOT/'deploy/load/matrix.v1.json').read_text())
spec=importlib.util.spec_from_file_location('load_matrix',ROOT/'scripts/load/matrix.py');M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)


def test_repository_matrix_is_the_design_matrix():
    M.validate_matrix(MATRIX)
    cells={c['cell']:c for c in MATRIX['cells']}
    assert [c for c,s in cells.items() if s['role']=='gate']==['C4','C4-low']
    assert {c:s['host_concurrency'] for c,s in cells.items()}=={'C1':1,'C2':2,'C4':4,'C8':8,'C4-low':4,'C4-peak':4,'C4-quiet':4,'C4-ci':4}
    assert {c:s['rate_per_s'] for c,s in cells.items() if c.startswith('C4')}=={'C4':1.0,'C4-low':0.2,'C4-peak':3.0,'C4-quiet':1.0,'C4-ci':1.0}
    assert [c for c,s in cells.items() if not s['background']]==['C4-quiet'] and [c for c,s in cells.items() if s['ci_background']]==['C4-ci']
    assert MATRIX['expected_refusals']==['NXC05','NXC06'] and MATRIX['model_latency']=={'distribution':'lognormal','median_ms':1500,'p95_ms':4000}
    assert MATRIX['timing']['steady_seconds']==600 and MATRIX['timing']['snapshot_interval_seconds']==30
    assert abs(sum(MATRIX['load']['message_mix'].values())-1)<1e-9


@pytest.mark.parametrize('mutate',[lambda m:m['cells'].pop(),lambda m:m['cells'][0].update(role='gate'),lambda m:m['cells'][2].update(role='observe'),
    lambda m:m.update(schema='other')])
def test_a_matrix_off_the_design_is_refused(mutate):
    matrix=copy.deepcopy(MATRIX);mutate(matrix)
    with pytest.raises(SystemExit):M.validate_matrix(matrix)


def test_runner_refuses_real_dispatch(monkeypatch,tmp_path):
    monkeypatch.setenv('REAL_DISPATCH_ENABLED','true')
    with pytest.raises(SystemExit,match='REAL_DISPATCH_ENABLED'):
        M.main(['--template',str(tmp_path),'--matrix',str(ROOT/'deploy/load/matrix.v1.json'),'--out',str(tmp_path/'o'),'--lane-root',str(tmp_path/'l')])


def test_the_load_never_relaxes_the_login_rate_limit():
    """Dispatcher 2026-10-11: the login rate limit is a security control. No load script or load test helper writes a rate policy
    (the template keeps its trusted configuration's), every cell aborts unless the policy it runs under is the one the template
    recorded, and the driver's warm-up waits out a 429 (or logs in from distinct loopback sources where the host can bind them)."""
    sources=[*(ROOT/'scripts/load').glob('*.py'),*(ROOT/'scripts/load').glob('*.sh'),ROOT/'tests/load_template.py',ROOT/'tests/load_chain.py']
    assert not [p.name for p in sources if 'rate_polic' in p.read_text() and p.name not in ('matrix.py','build_template_hold.py','lane_preflight.py')]
    assert 'insert into control.nexloop_browser_rate_policies' not in (ROOT/'scripts/load/lane_preflight.py').read_text().replace(r'(insert into|update)\s+','')
    for name in ('matrix.py','build_template_hold.py'):  # read-only use: the policy is selected, never inserted or updated
        text=(ROOT/'scripts/load'/name).read_text()
        assert 'from control.nexloop_browser_rate_policies' in text and 'insert into control.nexloop_browser_rate_policies' not in text
        assert 'update control.nexloop_browser_rate_policies' not in text
    driver=(ROOT/'scripts/load/driver.py').read_text()
    assert "Retry-After" in driver and 'local_address' in driver
    assert "self.abort('login_policy_mismatch')" in (ROOT/'scripts/load/matrix.py').read_text()


def test_resume_keeps_completed_cells_and_moves_incomplete_attempts_aside(tmp_path):
    out,lane=tmp_path/'run',tmp_path/'lane'
    for cell,completed in (('C4',True),('C4-low',False)):
        (out/'cells'/cell).mkdir(parents=True);(out/'cells'/cell/'cell.json').write_text(json.dumps({'completed':completed,'aborted':None if completed else 'service_start_failed'}))
        (lane/cell).mkdir(parents=True);(lane/cell/'marker').write_text(cell)
    run={'template':{'key':'k'},'commit':'c','manifest_digest':'d'}
    (out/'run.json').write_text(json.dumps(run))
    resumed=M.resume_run(out,{'template':{'key':'k'},'commit':'c','manifest_digest':'d'})
    assert M.prepare_resume(out,resumed,'C4',lane) is False and (out/'cells'/'C4'/'cell.json').is_file()  # kept as is
    assert M.prepare_resume(out,resumed,'C4-low',lane) is True
    assert not (out/'cells'/'C4-low').exists() and json.loads((out/'attempts'/'C4-low.1'/'cell.json').read_text())['aborted']=='service_start_failed'
    assert (lane/'attempts'/'C4-low.1'/'marker').read_text()=='C4-low'
    assert resumed['attempts']==[{'cell':'C4-low','attempt':1,'aborted':'service_start_failed','kept_in':'attempts/C4-low.1'}] and len(resumed['resumed_at'])==1
    with pytest.raises(SystemExit):M.resume_run(out,{'template':{'key':'other'},'commit':'c','manifest_digest':'d'})


def test_lane_preflight_is_read_only_and_checks_the_template(tmp_path, monkeypatch):
    spec2=importlib.util.spec_from_file_location('lane_preflight',ROOT/'scripts/load/lane_preflight.py');P=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(P)
    lock=tmp_path/'ci.lock';lock.write_text('')
    assert P.lock_idle(str(lock))==(True,'idle') and P.lock_idle(str(tmp_path/'missing'))==(False,'missing')
    import fcntl
    with open(lock,'rb') as held:
        fcntl.flock(held,fcntl.LOCK_EX)
        assert P.lock_idle(str(lock))==(False,'held')
    ok,details=P.deployment_premises()
    assert ok and details['runtime_worker']['guard_workers']==4 and details['runtime_worker']['dispatch_loops']==4 and all(details['chain'].values())
    assert P.login_limit_untouched()==(True,[])
    template=tmp_path/'template';(template/'pgdata').mkdir(parents=True)
    S=P.synthetic
    meta={'seed':31,'manifest_totals':{'consumers':20},'manifest_digest':S.digest(S.lines(31,20)),'commit':'abc','login_rate_policy':{'maximum_attempts':10,'window_seconds':60,'active':True}}
    (template/'template.json').write_text(json.dumps(meta))
    assert P.template_matches(template,31,20,'abc')[0] and not P.template_matches(template,31,21,'abc')[0] and not P.template_matches(template,31,20,'other')[0]
    before=sorted(p.name for p in tmp_path.rglob('*'))
    monkeypatch.setenv('NEXLOOP_CI_LOCK',str(lock));monkeypatch.setenv('LOAD_CONSUMERS','20')
    P.main(['--template',str(template)])
    assert sorted(p.name for p in tmp_path.rglob('*'))==before  # wrote nothing


def test_draft_v2_changes_only_the_gate_cells_rates():
    """matrix.v2.json (draft, pending the owner's D-a addendum): C4 0.1/s and C4-low 0.05/s; everything else as v1."""
    v2=json.loads((ROOT/'deploy/load/matrix.v2.json').read_text())
    M.validate_matrix(v2)
    assert v2['schema']=='nexloop.load-matrix.v2' and v2['version']==2
    old={c['cell']:c for c in MATRIX['cells']};new={c['cell']:c for c in v2['cells']}
    assert {c:s['rate_per_s'] for c,s in new.items() if s['role']=='gate'}=={'C4':0.1,'C4-low':0.05}
    for name,cell in new.items():
        expected=dict(old[name],rate_per_s=cell['rate_per_s']) if cell['role']=='gate' else old[name]
        assert cell==expected,name
    assert all(new[c]['role']=='observe' for c,s in old.items() if s['rate_per_s'] in (1.0,3.0) and s['role']=='observe')
    assert {k:v for k,v in v2.items() if k not in ('schema','version','decision','cells')}=={k:v for k,v in MATRIX.items() if k not in ('schema','version','decision','cells')}


def test_lane_preflight_reads_the_lane_matrix(monkeypatch):
    spec2=importlib.util.spec_from_file_location('lane_preflight_v2',ROOT/'scripts/load/lane_preflight.py');P=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(P)
    monkeypatch.setenv('LOAD_MATRIX','deploy/load/matrix.v2.json')
    ok,details=P.deployment_premises()
    assert ok and details['gate_host_concurrency']=={'C4':4,'C4-low':4}


def _cell(tmp_path, scale=1.0):
    from types import SimpleNamespace
    spec = next(c for c in MATRIX['cells'] if c['cell'] == 'C4')
    return M.Cell(spec, MATRIX, SimpleNamespace(out=str(tmp_path), lane_root=str(tmp_path / 'lane'), timing_scale=scale), {'seed': 1})


def _settle(cell, counts):
    """Fake clock: each warm(seconds) advances it; the query returns the next residue count."""
    import asyncio
    state = {'now': 0.0, 'i': 0, 'warmed': []}
    async def query():
        value = counts[min(state['i'], len(counts) - 1)]
        state['i'] += 1
        return value
    async def warm(seconds):
        state['warmed'].append(seconds)
        state['now'] += seconds
    return asyncio.run(cell.settle_residue(query, warm, clock=lambda: state['now'])), state


def test_the_template_residue_is_waited_out_before_the_steady_window(tmp_path):
    result, state = _settle(_cell(tmp_path), [{'recall-instance': 811, 'reply-due': 1}, {'recall-instance': 400}, {}])
    assert result == {'at_start': 812, 'by_feed': {'recall-instance': 811, 'reply-due': 1}, 'drained': True, 'seconds': 10.0, 'code': None,
                      'polls': [{'seconds': 0.0, 'due': 812}, {'seconds': 5.0, 'due': 400}, {'seconds': 10.0, 'due': 0}]}
    assert state['warmed'] == [5, 5]  # warm-up traffic keeps going while waiting
    result, state = _settle(_cell(tmp_path), [{}])
    assert result['drained'] is True and result['at_start'] == 0 and state['warmed'] == []


def test_a_residue_left_at_the_cap_is_recorded_and_the_cell_goes_on(tmp_path):
    result, state = _settle(_cell(tmp_path), [{'recall-instance': 811}] * 100)
    assert result['drained'] is False and result['code'] == 'warm_up_not_drained' and result['seconds'] == 300.0
    assert result['remaining'] == {'recall-instance': 811} and sum(state['warmed']) == 300
    result, _ = _settle(_cell(tmp_path, scale=0.1), [{'claim-match': 3}] * 100)
    assert result['code'] == 'warm_up_not_drained' and result['seconds'] == 30.0  # the cap scales with the timing


def test_relay_processes_is_a_bounded_runner_option_recorded_per_cell(tmp_path):
    """Dispatcher 2026-10-13: the load stack starts 1 (default) to 3 relay processes; the cell records the number."""
    import sys
    sys.path.insert(0, str(ROOT / 'scripts/load'))
    import stack as St
    for bad in (0, 4, '2'):
        with pytest.raises(St.StackFailed):
            St.Stack.start_message_chain(object(), 4, relay_processes=bad)  # refused before anything starts
    assert _cell(tmp_path).result['relay_processes'] == 1
    from types import SimpleNamespace
    spec = next(c for c in MATRIX['cells'] if c['cell'] == 'C4-low')
    cell = M.Cell(spec, MATRIX, SimpleNamespace(out=str(tmp_path), lane_root=str(tmp_path / 'lane'), timing_scale=1.0, relay_processes=2), {'seed': 1})
    assert cell.result['relay_processes'] == 2
    lane = (ROOT / 'scripts/load/run_on_lane.sh').read_text()
    assert '--relay-processes "${LOAD_RELAY_PROCESSES:-1}"' in lane and 'LOAD_RELAY_PROCESSES must be 1, 2 or 3' in lane


def test_an_aborted_cell_records_the_error_types_and_frames_without_text(tmp_path):
    """NX-052 N state (2026-10-13): a cell that aborted with runner_error left no trace. The cell now records the exception
    types along the cause chain, a SQLSTATE and file:line:function frames, never the message (D8)."""
    import psycopg
    secret = 'tenant 00000000-0000-4000-8000-00000000000a constraint secret-text'
    def inner():
        raise psycopg.errors.UndefinedObject(secret)
    try:
        try:
            inner()
        except psycopg.Error as error:
            raise RuntimeError(secret) from error
    except RuntimeError as error:
        described = M.describe_error(error)
    assert described['chain'] == [{'type': 'builtins.RuntimeError'}, {'type': 'psycopg.errors.UndefinedObject', 'sqlstate': '42704'}]
    assert any(f.startswith('test_load_matrix.py:') and f.endswith(':inner') for f in described['frames'])
    assert secret not in json.dumps(described) and absent('00000000', described)
    cell = _cell(tmp_path)
    result = cell.abort('runner_error', RuntimeError(secret))
    assert result['aborted'] == 'runner_error' and result['error']['chain'] == [{'type': 'builtins.RuntimeError'}] and secret not in json.dumps(result)


def test_the_load_api_identity_pool_follows_the_deployment_budget_unless_given(tmp_path):
    """Dispatcher 2026-10-13: the load lane's API browser identity pool is the deployment budget's (api-browser-identity) by
    default, can be set for a retest (--identity-pool-max / LOAD_IDENTITY_POOL_MAX) and is recorded per cell."""
    import sys
    sys.path.insert(0, str(ROOT / 'scripts/load'))
    import stack as St
    budget = json.loads((ROOT / 'deploy/stage/connection-budget.v1.json').read_text())
    assert St.budget_identity_pool() == next(s['pool_max'] for s in budget['services'] if s['name'] == 'api-browser-identity') == 2
    for bad in (0, 9, '3'):
        with pytest.raises(St.StackFailed):
            St.Stack.start_api(object(), identity_pool_max=bad)  # refused before anything starts
    stack_text = (ROOT / 'scripts/load/stack.py').read_text()
    assert "'--identity-pool-max', str(identity_pool_max)" in stack_text
    matrix_text = (ROOT / 'scripts/load/matrix.py').read_text()
    assert "self.result['identity_pool_max'] = stack.identity_pool_max" in matrix_text and "'--identity-pool-max'" in matrix_text
    lane = (ROOT / 'scripts/load/run_on_lane.sh').read_text()
    assert '${LOAD_IDENTITY_POOL_MAX:+--identity-pool-max "$LOAD_IDENTITY_POOL_MAX"}' in lane and 'LOAD_IDENTITY_POOL_MAX must be 1..8' in lane


def test_the_warm_up_login_peak_is_summarised_numbers_only():
    """Login peak retest (dispatcher 2026-10-13): each warm-up login request's latency and status, summarised per cell."""
    attempts = [(100.0, 200)] * 18 + [(900.0, 200), (40.0, 429), (2500.0, 503), (30000.0, 0)]
    assert M.login_summary(attempts) == {'requests': 22, 'p50_ms': 100.0, 'p95_ms': 900.0, 'max_ms': 2500.0,
                                          'statuses': {'0': 1, '200': 19, '429': 1, '503': 1}}
    assert M.login_summary([]) == {'requests': 0, 'p50_ms': None, 'p95_ms': None, 'max_ms': None, 'statuses': {}}
    driver = (ROOT / 'scripts/load/driver.py').read_text()
    assert 'await customer.login(attempts=self.login_attempts)' in driver
