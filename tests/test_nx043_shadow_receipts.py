"""A synthetic four-file S9 chain, consumed by the normal offline entry."""
import hashlib
import io
import json

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/acceptance"))

from nx043_stage import CHAIN, main, save_state
from s6_common import CONFIG_SCHEMA, RESULT_SCHEMA


@pytest.mark.parametrize('fault', ['missing_guard', 'wrong_release', 'zero_decisions', 'early_end', 'missing_scope'])
def test_incomplete_shadow_observation_never_passes_merge(tmp_path, fault):
    cfg = dict(schema=CONFIG_SCHEMA, release='fixture', executor='fixture', tenant_id='tenant-demo',
               results_dir=str(tmp_path))
    config = tmp_path / 'config'
    config.write_text(json.dumps(cfg))
    config.chmod(0o600)
    state = dict(run='abcdefgh', ends_at='2026-01-01T00:00:00Z', shadow_world='shadow:fixture',
                 experiments={'shadow':'fixture'},strategies={'candidate':{'strategy_id':'candidate','version':1}})
    save_state(cfg, state)
    binding = hashlib.sha256(json.dumps([cfg['tenant_id'], state['shadow_world'], state['experiments']['shadow'],
        state['strategies']['candidate'], state['ends_at']],sort_keys=True).encode()).hexdigest()
    names = [['real_side_quiet','real_digest_before'],['shadow_events_copied'],['shadow_decisions_recorded'],
             ['guard_stops_at_the_end','shadow_sends_nothing','shadow_decisions_landed',
              'real_dispatch_switches_unchanged','real_alerts_in_window','real_side_unchanged']]
    args = ['merge','--config',str(config)]
    for i, part in enumerate(CHAIN):
        steps = [dict(name=n,passed=True,observed={'decisions':1,'copied':1,'sent':0}) for n in names[i]]
        doc = dict(schema=RESULT_SCHEMA,check=part,dry_run=False,world='simulation',release='fixture',
                   passed=True,run_nonce=state['run'],steps=steps,blocked=[],shadow_binding=binding,
                   executed_at=f'2026-01-01T00:00:0{i}+00:00')
        if i==3:
            if fault=='missing_guard':
                steps.pop(0)
            if fault=='wrong_release':
                doc['release']='other'
            if fault=='missing_scope':
                del doc['shadow_binding']
        if fault=='early_end':
            doc['executed_at']=f'2025-12-31T23:59:0{i}+00:00'
        if i==2 and fault=='zero_decisions':
            steps[0]['observed']['decisions']=0
        path=tmp_path / part
        path.write_text(json.dumps(doc))
        args.extend(['--from',str(path)])
    assert main(args,out=io.StringIO()) != 0
