"""NX-031 slice 1: the matrix file is the design's eight cells with only C4 and C4-low as gates; the runner refuses anything
else before touching a cluster, and refuses REAL_DISPATCH_ENABLED=true."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

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
