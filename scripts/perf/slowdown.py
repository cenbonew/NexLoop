"""Per-request latency vs the 2 s guard/tool HTTP limit under a CPU slowdown factor k.

Usage: python scripts/perf/slowdown.py scripts/perf/out/<label> [limit_ms]
A request exceeds the limit on a k-times slower host when k*latency > limit
(uniform scaling assumption: CPU-bound Python + PL/pgSQL; lock waits scale with
the holder's CPU time, so they scale too)."""
import json,sys
from pathlib import Path
out=Path(sys.argv[1]);limit=float(sys.argv[2]) if len(sys.argv)>2 else 2000.0
events={}
for p in out.glob('proc-*.json'):
    for e in json.loads(p.read_text())['events']:
        if e['name'].startswith('invoke:'):events.setdefault(e['name'],[]).append(1000*e['total'])
print(f'|request|n|max ms|break-even k (limit/max)|>limit at k=1.5|k=2|k=2.5|k=3|\n|---|---|---|---|---|---|---|---|')
for name,v in sorted(events.items(),key=lambda kv:-max(kv[1])):
    if max(v)<100:continue
    row=[sum(1 for x in v if x*k>limit) for k in (1.5,2,2.5,3)]
    print(f'|{name}|{len(v)}|{max(v):.0f}|{limit/max(v):.2f}|'+'|'.join(str(r) for r in row)+'|')
