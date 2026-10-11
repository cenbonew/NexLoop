"""NX-031 slice 0: the synthetic manifest is a pure function of (seed, consumers); synthetic text only."""
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('load_synthetic',ROOT/'scripts/load/synthetic.py');S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)


def test_same_seed_same_bytes_other_seed_other_digest(tmp_path):
    a,b,c=S.lines(31,200),S.lines(31,200),S.lines(32,200)
    assert a==b and S.digest(a)==S.digest(b)!=S.digest(c)
    assert S.main(['--seed','31','--consumers','200','--out',str(tmp_path/'m.jsonl')])==0
    assert (tmp_path/'m.jsonl').read_text()=='\n'.join(a)+'\n'


def test_shape_and_proportions():
    records=[json.loads(x) for x in S.lines(31,1000)]
    *consumers,trailer=records
    assert trailer['trailer'] is True and trailer['totals']['consumers']==1000==len(consumers)
    t=trailer['totals']
    assert t['messages']==sum(len(c['messages']) for r in consumers for c in r['conversations'])
    # §2.2 proportions, within sampling noise of the fixed seed
    assert 0.05<t['restricted']/1000<0.11 and 0.16<t['commitment']/1000<0.25 and 0.25<t['active_plan']/1000<0.35
    assert 15<t['messages']/1000<25 and all(1<=len(r['conversations'])<=3 for r in consumers)
    assert len({r['username'] for r in consumers})==1000
    assert all(any(m['kind']=='commitment_ask' for m in r['conversations'][0]['messages']) for r in consumers if r['commitment'])
    restricted=[r for r in consumers if r['restricted']]
    assert all(r['conversations'][-1]['messages'][-1]['kind']=='refusal' for r in restricted)
    assert all(e['currency']=='CNY' and e['amount_minor']>0 for r in consumers for e in r['commercial_events'])
