"""Summarize scripts/perf/out/<label> into Markdown (stdout)."""
import json,statistics,sys
from pathlib import Path

out=Path(sys.argv[1])
procs=[json.loads(p.read_text()) for p in sorted(out.glob('proc-*.json'))]
pgs=[json.loads(p.read_text()) for p in sorted(out.glob('pg-*.json'))]
walls=[json.loads(l) for l in (out/'walls.jsonl').read_text().splitlines()] if (out/'walls.jsonl').exists() else []

def q(values,p):
    values=sorted(values)
    return values[min(len(values)-1,int(round(p*(len(values)-1))))] if values else 0

print(f'# perf summary: {out.name}\n')
print('## tests\n\n|test|wall s|result|\n|---|---|---|')
for w in walls:
    lines=(out/w['log']).read_text().strip().splitlines() if w.get('log') and (out/w['log']).exists() else ['?']
    tail=lines[-1]
    print(f"|{w['node']}|{w['wall_seconds']:.1f}|{tail}|")
fc=[p.get('fact_parse_cache') for p in procs if p.get('fact_parse_cache')]
if fc:print(f"\nfact parse cache: hits {sum(c['hits'] for c in fc)}, misses {sum(c['misses'] for c in fc)}, disabled={any(c['disabled'] for c in fc)}")
print(f'\nprocesses with hooks: {len(procs)} ({", ".join(" ".join(p["argv"])[-40:] for p in procs)})\n')

stats={}
for p in procs:
    for name,s in p['stats'].items():
        t=stats.setdefault(name,{'count':0,'total':0.0,'self':0.0,'max':0.0})
        t['count']+=s['count'];t['total']+=s['total'];t['self']+=s['self'];t['max']=max(t['max'],s['max'])
print('## Python-side inclusive/exclusive time (all hooked processes)\n\n|name|count|total s|self s|mean ms|max ms|\n|---|---|---|---|---|---|')
import re
RUNTIME=re.compile(r'sql:(authz|control|runtime|ontology)\.[a-z0-9_]+$')
setup={'count':0,'total':0.0,'self':0.0,'max':0.0}
for name in [n for n in stats if n.startswith('sql:') and not RUNTIME.match(n)]:
    s=stats.pop(name)
    for k in ('count','total','self'):setup[k]+=s[k]
    setup['max']=max(setup['max'],s['max'])
stats['sql:other (bootstrap DDL, fixture seeding, sampler)']=setup
for name,s in sorted(stats.items(),key=lambda kv:-kv[1]['self'])[:40]:
    print(f"|{name}|{s['count']}|{s['total']:.3f}|{s['self']:.3f}|{1000*s['total']/s['count']:.1f}|{1000*s['max']:.1f}|")

events={}
for p in procs:
    for e in p['events']:events.setdefault(e['name'],[]).append(e)
print('\n## top-level request latency (per call)\n\n|request|n|p50 ms|p95 ms|max ms|mean breakdown (ms, top 8)|\n|---|---|---|---|---|---|')
for name,items in sorted(events.items(),key=lambda kv:-sum(e['total'] for e in kv[1])):
    totals=[e['total'] for e in items];agg={}
    for e in items:
        for k,v in e['breakdown'].items():agg[k]=agg.get(k,0.0)+v
    top=', '.join(f'{k} {1000*v/len(items):.0f}' for k,v in sorted(agg.items(),key=lambda kv:-kv[1])[:8])
    counts={}
    for e in items:
        for k,v in e.get('counts',{}).items():counts[k]=counts.get(k,0)+v
    dec=[e for e in items if 'decisions' in e]
    dup=f"; decisions/req {sum(e['decisions'] for e in dec)/len(dec):.1f}, distinct {sum(e['distinct_decisions'] for e in dec)/len(dec):.1f}" if dec and sum(e['decisions'] for e in dec) else ''
    calls=', '.join(f'{k} ×{v/len(items):.1f}' for k,v in sorted(counts.items(),key=lambda kv:-kv[1])[:6] if k!=name)
    print(f'|{name}|{len(items)}|{1000*q(totals,.5):.0f}|{1000*q(totals,.95):.0f}|{1000*max(totals):.0f}|{top}{dup}{"; calls/req: "+calls if calls else ""}|')

funcs={}
for pg in pgs:
    for f in pg['functions']:
        t=funcs.setdefault(f['function'],[0,0.0,0.0]);t[0]+=f['calls'];t[1]+=f['total_ms'];t[2]+=f['self_ms']
print('\n## PostgreSQL PL/pgSQL functions, all tests (track_functions=pl)\n\n|function|calls|total ms|self ms|mean total ms|\n|---|---|---|---|---|')
for f,(n,t,s_) in sorted(funcs.items(),key=lambda kv:-kv[1][2])[:25]:
    print(f'|{f}|{n}|{t:.0f}|{s_:.0f}|{t/n if n else 0:.1f}|')
for pg in pgs:
    print(f"\n## PostgreSQL functions: {pg['test']} (call {pg['call_seconds'] or 0:.1f}s)\n\n|function|calls|total ms|self ms|mean total ms|\n|---|---|---|---|---|")
    for f in pg['functions'][:15]:
        print(f"|{f['function']}|{f['calls']}|{f['total_ms']:.0f}|{f['self_ms']:.0f}|{(f['total_ms']/f['calls'] if f['calls'] else 0):.1f}|")
    print('\nactive-backend wait samples (20 ms):',', '.join(f'{k}={v}' for k,v in list(pg['wait_samples_20ms'].items())[:10]) or 'none')
    for q,v in list(pg.get('wait_samples_by_statement',{}).items())[:6]:
        print(f'- `{q}`: '+', '.join(f'{k}={n}' for k,n in v.items()))
