"""NX-049: summarize cprof-*.pstats written with NEXLOOP_PERF_CPROFILE=1 (measurement only).

Usage: python scripts/perf/cprof_report.py <out-dir> [top=30]
Prints the functions under AuthorizationFactsResolver.resolve by own time (tottime) and by
inclusive time (cumtime), aggregated over every profiled resolve call in <out-dir>.
"""
import glob,json,pstats,re,sys

out=sys.argv[1];top=int(sys.argv[2]) if len(sys.argv)>2 else 30
files=sorted(glob.glob(out+'/cprof-*.pstats'))
if not files:sys.exit('no cprof-*.pstats in '+out)
stats=pstats.Stats(files[0])
for f in files[1:]:stats.add(f)
meta=[json.load(open(f)) for f in glob.glob(out+'/cprof-*.json')]
profiled=sum(m['profiled'] for m in meta);skipped=sum(m['skipped_overlapping'] for m in meta)

def name(key):
    path,line,fn=key
    return f"{re.sub(r'^.*?/(src|lib/python[0-9.]+|site-packages)/','',path)}:{line}({fn})"

rows=[(name(k),v[1],v[2],v[3]) for k,v in stats.stats.items()]  # (name, ncalls, tottime, cumtime)
roots=[r for r in rows if r[0].endswith(('(resolve)','(resolve_in_unit_of_work)')) and '_fact_resolver' in r[0]]
total=sum(r[3] for r in roots) or 1.0
print(f'profiled resolve calls: {profiled} (skipped while another was profiled: {skipped}); inclusive {1000*total:.0f} ms')
for title,col in (('own time (tottime)',2),('inclusive time (cumtime)',3)):
    print(f'\n## top {top} by {title}\n| function | calls | ms | % of resolve |\n|---|---|---|---|')
    for r in sorted(rows,key=lambda r:-r[col])[:top]:
        print(f'| `{r[0]}` | {r[1]} | {1000*r[col]:.1f} | {100*r[col]/total:.1f} |')
