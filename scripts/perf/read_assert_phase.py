"""O5b phase classification of duplicate assert_read_authority calls (measurement only).

Input: pglog-*.log produced by perf_plugin's read-assert trace (.trace_read_assert).
For every assertion that repeats an identical claims digest earlier in the SAME SQL
statement (same backend pid + statement start), compare with the previous identical
assertion:
  same_snapshot     visible database state unchanged -> result can only differ by the
                    clock (expiry); dedupable if expires_at is re-checked.
  same_snapshot_lock  as above, but this backend acquired new locks in between (no
                    commit became visible, so a wait, if any, changed nothing).
  new_snapshot_lock another transaction committed in between AND this backend acquired
                    new locks in between (advisory or any granted lock) -> the post-lock
                    wait re-check L1 requires; must be redone.
  new_snapshot      another transaction committed in between, no new lock observed ->
                    must be redone (visible data may have changed).
Usage: read_assert_phase.py scripts/perf/out/<label>"""
import collections,re,sys
from pathlib import Path
LINE=re.compile(r'perfra\|(\d+)\|([^|]+)\|([0-9a-f]{32})\|([^|]*)\|([0-9a-f]{32})\|(\d+)\|(\d+)')
def kind(target):
    t=re.sub(r'[0-9a-f]{64}','<id>',target)
    return t.split('/')[0] if not t.startswith('eios:property') else t.split('/')[0]+'/'+t.split('/')[-1]
for log in sorted(Path(sys.argv[1]).glob('pglog-*.log')):
    stmts=collections.defaultdict(list)
    for line in log.read_text(errors='replace').splitlines():
        m=LINE.search(line)
        if m:stmts[(m.group(1),m.group(2))].append((m.group(3),m.group(4),m.group(5),int(m.group(6)),int(m.group(7))))
    total=sum(len(v) for v in stmts.values());cls=collections.Counter();by_kind=collections.defaultdict(collections.Counter)
    for calls in stmts.values():
        last={}
        for claims,target,snap,adv,locks in calls:
            if claims in last:
                psnap,padv,plocks=last[claims]
                if snap==psnap:c='same_snapshot_lock' if (adv>padv or locks>plocks) else 'same_snapshot'
                elif adv>padv or locks>plocks:c='new_snapshot_lock'
                else:c='new_snapshot'
            else:c='first'
            cls[c]+=1;by_kind[kind(target)][c]+=1
            last[claims]=(snap,adv,locks)
    dup=total-cls['first']
    print(f'== {log.name[6:90]}')
    print(f'statements {len(stmts)}  assertions {total}  first {cls["first"]}  duplicates {dup} ({100*dup/max(total,1):.1f}%)')
    for c in ('same_snapshot','same_snapshot_lock','new_snapshot_lock','new_snapshot'):
        print(f'  {c:18s} {cls[c]:7d}  {100*cls[c]/max(dup,1):5.1f}% of duplicates  {100*cls[c]/max(total,1):5.1f}% of all')
    print('  top targets (first/same/same_lock/new_lock/new):')
    for k,c in sorted(by_kind.items(),key=lambda kv:-sum(kv[1].values()))[:8]:
        print(f'    {sum(c.values()):6d} {k:55s} {c["first"]}/{c["same_snapshot"]}/{c["same_snapshot_lock"]}/{c["new_snapshot_lock"]}/{c["new_snapshot"]}')
