"""Duplicate analysis of assert_read_authority per SQL statement from perf_plugin traces.
Usage: read_assert_dup.py scripts/perf/out/<label>"""
import collections,re,sys
from pathlib import Path
for log in sorted(Path(sys.argv[1]).glob('pglog-*.log')):
    stmts=collections.defaultdict(list)
    for line in log.read_text(errors='replace').splitlines():
        m=re.search(r'perfra\|(\d+)\|([^|]+)\|([0-9a-f]{32})\|(.*)$',line)
        if m:stmts[(m.group(1),m.group(2))].append((m.group(3),m.group(4)))
    calls=sum(len(v) for v in stmts.values());distinct=sum(len(set(c for c,_ in v)) for v in stmts.values())
    targets=collections.Counter(re.sub(r'[0-9a-f]{64}','<id>',t).split('/')[0]+'/'+(re.sub(r'[0-9a-f]{64}','<id>',t).split('/')[-1] if t.startswith('eios:property') else '') for v in stmts.values() for _,t in v)
    per=sorted(len(v) for v in stmts.values())
    print(f'== {log.name[6:80]}\nstatements {len(stmts)} asserts {calls} distinct-within-statement {distinct} duplicate {100*(calls-distinct)/max(calls,1):.1f}% '
          f'max/statement {per[-1] if per else 0} p50 {per[len(per)//2] if per else 0}')
    for t,n in targets.most_common(8):print(f'   {n:6d} {t}')
