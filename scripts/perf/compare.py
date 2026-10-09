"""Compare two perf runs: per-request latency and in-request decisions. Usage: compare.py <before> <after>"""
import json,sys
from pathlib import Path
def load(d):
    ev={}
    for p in Path(d).glob('proc-*.json'):
        for e in json.loads(p.read_text())['events']:
            if e['name'].startswith('invoke:'):ev.setdefault(e['name'],[]).append(e)
    walls={json.loads(l)['node']:json.loads(l) for l in (Path(d)/'walls.jsonl').read_text().splitlines()}
    return ev,walls
q=lambda v,p:sorted(v)[min(len(v)-1,int(round(p*(len(v)-1))))]
(b,wb),(a,wa)=load(sys.argv[1]),load(sys.argv[2])
print('|request|n before/after|decisions/req before→after|p50 ms|p95 ms|max ms|\n|---|---|---|---|---|---|')
for k in sorted(set(b)&set(a),key=lambda k:-max(e['total'] for e in b[k])):
    tb=[1000*e['total'] for e in b[k]];ta=[1000*e['total'] for e in a[k]]
    if max(tb)<150:continue
    db=sum(e.get('decisions',0) for e in b[k])/len(b[k]);da=sum(e.get('decisions',0) for e in a[k])/len(a[k])
    print(f"|{k}|{len(tb)}/{len(ta)}|{db:.1f}→{da:.1f}|{q(tb,.5):.0f}→{q(ta,.5):.0f}|{q(tb,.95):.0f}→{q(ta,.95):.0f}|{max(tb):.0f}→{max(ta):.0f}|")
print('\n|test|before|after|\n|---|---|---|')
for n in wb:
    x,y=wb[n],wa.get(n,{})
    print(f"|{n.split('::')[-1][:70]}|{x['wall_seconds']:.1f}s rc={x['rc']}|{y.get('wall_seconds',0):.1f}s rc={y.get('rc')}|")
