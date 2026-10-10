"""NX-049 report: where the 2 s guard budget goes, per run, from scripts/perf/out/<label>.

Inputs (written by nx049_profile.sh with hooks in timeline mode): walls.jsonl, proc-*.json
(Python events + timeline), host-*.jsonl (Node preload), pg-*.json (function stats, lock-wait
and activity samples), pglog-*.log (slow statements / lock waits, parameters not logged).
Outputs summary.json and summary.md in the same directory. All clocks are wall clocks
(epoch seconds / ms) so records from the test process, the Host and PostgreSQL line up.
"""
import json,re,statistics,sys
from datetime import datetime,timezone
from pathlib import Path

LIMIT_MS=2000
GUARD_PATHS=('/internal/v1/runtime/authorize','/internal/v1/runtime/effects/submit','/internal/v1/runtime/effects/find')
CONSTRAINED=('invoke:authorize_runtime_activation','invoke:runtime_effect_tool','invoke:prepare_message_context','invoke:create_runtime_activation')


def load(out):
    walls=[json.loads(l) for l in (out/'walls.jsonl').read_text().splitlines()] if (out/'walls.jsonl').exists() else []
    procs=[json.loads(p.read_text()) for p in sorted(out.glob('proc-*.json'))]
    host=[json.loads(l) for p in sorted(out.glob('host-*.jsonl')) for l in p.read_text().splitlines() if l.strip()]
    pgs={p.stem[3:]:json.loads(p.read_text()) for p in sorted(out.glob('pg-*.json'))}
    logs={p.stem[6:]:p.read_text(errors='replace') for p in sorted(out.glob('pglog-*.log'))}
    return walls,procs,host,pgs,logs


_LOG=re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) UTC \[(\d+)\] (\S*) (LOG|ERROR|DETAIL|STATEMENT):\s+(.*)$')
def log_records(text):
    out=[]
    for line in text.splitlines():
        m=_LOG.match(line)
        if not m:continue
        wall=datetime.strptime(m.group(1),'%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc).timestamp()
        message=m.group(5)
        d=re.match(r'duration: ([\d.]+) ms\s+(?:statement|execute [^:]*): (.*)',message)
        if d:
            fn=re.search(r'([a-z_]+\.[a-z0-9_]+)\s*\(',d.group(2))
            out.append({'kind':'slow_statement','end_wall':wall,'ms':float(d.group(1)),'pid':int(m.group(2)),'statement':(fn.group(1) if fn else d.group(2)[:80])})
        elif 'still waiting for' in message or 'acquired' in message or 'deadlock' in message.lower():
            out.append({'kind':'lock_log','wall':wall,'pid':int(m.group(2)),'message':message[:200]})
    return out


def q(values,p):
    values=sorted(values)
    return round(values[min(len(values)-1,int(round(p*(len(values)-1))))],1) if values else None


def within(wall,start,end):return start<=wall<=end


def analyse_run(run,procs,host,pg,log):
    start,end=run['start_wall'],run['end_wall']
    events=[dict(e,pid=p['pid']) for p in procs for e in p.get('events',[]) if 'wall' in e and within(e['wall'],start,end)]
    timeline=[dict(r,pid=p['pid']) for p in procs for r in p.get('timeline',[]) if within(r.get('start_wall',0),start,end)]
    host_records=[r for r in host if within((r.get('start_wall_ms') or r.get('wall_ms') or r.get('origin_wall_ms') or 0)/1000,start-5,end)]
    clients=[r for r in host_records if r['kind']=='client' and r['path'] in GUARD_PATHS]
    guard_requests=[r for r in timeline if r['kind']=='guard_request']
    handshakes=[r for r in timeline if r['kind']=='tls_handshake']
    dispatch=[r for r in timeline if r['kind']=='http_client']
    def ms(values):return {'n':len(values),'p50':q(values,.5),'p95':q(values,.95),'max':round(max(values),1) if values else None}
    per_path={}
    for r in clients:per_path.setdefault(r['path'],[]).append(r['total_ms'])
    invokes={}
    for e in events:
        if e['name'] in CONSTRAINED or e['name'].startswith(('invoke:dispatch','invoke:relay')):invokes.setdefault(e['name'],[]).append(1000*e['total'])
    # O3 fact parse cache: processes that produced any record inside this run's window.
    pids={e['pid'] for e in events}|{r['pid'] for r in timeline}
    caches=[p['fact_parse_cache'] for p in procs if p['pid'] in pids and p.get('fact_parse_cache')]
    hits=sum(c['hits'] for c in caches);misses=sum(c['misses'] for c in caches)
    # PG functions per constrained request (pg_stat_xact_user_functions deltas, ms).
    per_fn={};requests=[e for e in events if e['name'] in CONSTRAINED and e.get('pg_functions')]
    for e in requests:
        for name,(calls,total,own) in e['pg_functions'].items():
            acc=per_fn.setdefault(name,[0,0.0,0.0]);acc[0]+=calls;acc[1]+=total;acc[2]+=own
    # Concurrency inside one guard process: constrained requests in the same pid whose intervals
    # overlap by more than 30% of the shorter one (back-to-back pipelining is not counted).
    # `python_ms` is request time outside SQL round trips (`sql:*` items) and needs the GIL;
    # `sql_ms` is SQL round-trip time. Bursts are connected groups of overlapping requests.
    def split(e):
        sql=sum(v for k,v in e['breakdown'].items() if k.startswith('sql:'))
        return 1000*e['total'],1000*(e['total']-sql),1000*sql
    timed=sorted((e for e in events if e['name'] in CONSTRAINED),key=lambda e:e['wall']-e['total'])
    span=lambda e:(e['wall']-e['total'],e['wall'])
    overlaps=lambda x,y:x['pid']==y['pid'] and min(span(x)[1],span(y)[1])-max(span(x)[0],span(y)[0])>0.3*min(x['total'],y['total'])
    solo=[e for e in timed if not any(o is not e and overlaps(e,o) for o in timed)];shared=[e for e in timed if e not in solo]
    def medians(group):
        parts=[split(e) for e in group]
        return {'n':len(group),**{k:round(statistics.median(x[i] for x in parts),1) if parts else None for i,k in enumerate(('total_ms','python_ms','sql_ms'))}}
    bursts=[]
    for e in shared:
        for burst in bursts:
            if any(overlaps(e,o) for o in burst):burst.append(e);break
        else:bursts.append([e])
    # Multi-process guard: overlap with any constrained request regardless of process.
    anywhere=lambda x,y:min(span(x)[1],span(y)[1])-max(span(x)[0],span(y)[0])>0.3*min(x['total'],y['total'])
    shared_any=[e for e in timed if any(o is not e and anywhere(e,o) for o in timed)]
    concurrency={'solo':medians(solo),'overlapped':medians(shared),'overlapped_any_process':medians(shared_any),
        'processes':len({e['pid'] for e in timed}),
        'bursts':[{'requests':len(g),'start_wall':round(min(span(x)[0] for x in g),3),'window_ms':round(1000*(max(span(x)[1] for x in g)-min(span(x)[0] for x in g)),1),
            'max_total_ms':round(max(1000*x['total'] for x in g),1),'totals_ms':[round(1000*x['total']) for x in g]} for g in bursts]}
    result={'node':run['node'],'iteration':run['iteration'],'rc':run['rc'],'wall_s':round(end-start,1),'load':run.get('load'),
        'host_guard_requests':{p:ms(v) for p,v in per_path.items()},
        'host_guard_failures':[{'path':r['path'],'total_ms':round(r['total_ms'],1),'error':r.get('error'),'status':r.get('status')} for r in clients if r.get('error') or r.get('status') not in (200,None)],
        'python_requests':{k:ms(v) for k,v in invokes.items()},
        'guard_server':ms([r['total_ms'] for r in guard_requests]),
        'tls_handshakes':{'server':ms([r['total_ms'] for r in handshakes if r['server_side']]),'client':ms([r['total_ms'] for r in handshakes if not r['server_side']])},
        'dispatcher_to_host':[{'path':r['path'],'ms':round(r['total_ms'],1),'status':r.get('status'),'error':r.get('error')} for r in dispatch],
        'host_startup':[],
        'concurrency':concurrency,
        'lock_wait_samples':len(pg.get('lock_waits',[])) if pg else None,
        'fact_parse_cache':{'processes':len(caches),'hits':hits,'misses':misses,'hit_rate':round(hits/(hits+misses),3) if hits+misses else None,
            'disabled':any(c.get('disabled') for c in caches)},
        'pg_functions_per_request':{'requests':len(requests),'top_by_total_ms':[{'function':k,'calls':round(v[0]/len(requests),1),'total_ms':round(v[1]/len(requests),1),'self_ms':round(v[2]/len(requests),1)}
            for k,v in sorted(per_fn.items(),key=lambda kv:-kv[1][1])[:15]] if requests else []},
        'pg_wait_samples':(pg or {}).get('wait_samples_20ms',{}),
        'pg_top_functions_self_ms':[(f['function'],f['calls'],round(f['self_ms'],1)) for f in (pg or {}).get('functions',[])[:12]],
        'slow_statements':sorted(({'statement':s['statement'],'ms':s['ms']} for s in log if s['kind']=='slow_statement'),key=lambda s:-s['ms'])[:15],
        'lock_log':[s['message'] for s in log if s['kind']=='lock_log'][:10]}
    spawns=sorted(r['start_wall'] for r in timeline if r['kind']=='host_spawn')
    for r in host_records:
        if r['kind']=='listening':
            spawn=max((s for s in spawns if s<=r['wall_ms']/1000),default=None)
            result['host_startup'].append({'node_start_to_listening_ms':round(r['since_process_start_ms'],1),
                'spawn_to_listening_ms':None if spawn is None else round(r['wall_ms']-1000*spawn,1)})
    # First guard request the Host saw exceed 2 s (or fail): full decomposition.
    over=sorted((r for r in clients if r['total_ms']>=LIMIT_MS or r.get('error')),key=lambda r:r['start_wall_ms'])
    result['first_over_limit']=decompose(over[0],guard_requests,events,handshakes,pg,log) if over else None
    slowest=max(clients,key=lambda r:r['total_ms'],default=None)
    result['slowest_guard_request']=decompose(slowest,guard_requests,events,handshakes,pg,log) if slowest and not over else None
    return result


def decompose(client,guard_requests,events,handshakes,pg,log):
    s=client['start_wall_ms']/1000;e=s+client['total_ms']/1000
    server=min((g for g in guard_requests if g['path']==client['path'] and s-0.05<=g['start_wall']<=e),key=lambda g:abs(g['start_wall']-s),default=None)
    invoke=None
    if server:
        candidates=[x for x in events if x['thread']==server['thread'] and server['start_wall']-0.01<=x['wall']<=server['start_wall']+server['total_ms']/1000 and x['name'].startswith('invoke:')]
        invoke=max(candidates,key=lambda x:x['total'],default=None)
    window=lambda wall:s-0.05<=wall<=e+0.05
    locks=[[round(w,3),[{'waiting':row[4],'wait_ms':round(row[5] or 0,1),'event':row[3],'blockers':row[7]} for row in rows]] for w,rows in (pg or {}).get('lock_waits',[]) if window(w)]
    activity={}
    for w,rows in (pg or {}).get('active_samples',[]):
        if window(w):
            for row in rows:activity[f'{row[2]}:{row[3]}']=activity.get(f'{row[2]}:{row[3]}',0)+1
    return {'host_request':{'path':client['path'],'start_wall':round(s,3),'total_ms':round(client['total_ms'],1),'status':client.get('status'),'error':client.get('error'),
            'phases_ms':{k:round(v,1) for k,v in client.get('phases',{}).items()}},
        'guard_server':None if not server else {'total_ms':round(server['total_ms'],1),'status':server.get('status'),
            'queued_before_handler_ms':round(1000*(server['start_wall']-s),1)},
        'tls_server_handshake_ms':[round(h['total_ms'],1) for h in handshakes if h['server_side'] and window(h['start_wall'])],
        'python_request':None if not invoke else {'name':invoke['name'],'total_ms':round(1000*invoke['total'],1),'measurement_probe_ms':invoke.get('probe_ms'),
            'breakdown_ms':{k:round(1000*v,1) for k,v in invoke['breakdown'].items()},'calls':invoke.get('counts',{}),
            'pg_functions_by_total':[{'function':k,'calls':v[0],'total_ms':v[1],'self_ms':v[2]} for k,v in sorted((invoke.get('pg_functions') or {}).items(),key=lambda kv:-kv[1][1])[:15]],
            'pg_functions_by_self':[{'function':k,'calls':v[0],'total_ms':v[1],'self_ms':v[2]} for k,v in sorted((invoke.get('pg_functions') or {}).items(),key=lambda kv:-kv[1][2])[:15]]},
        'pg_lock_waits':locks,'pg_activity_samples_20ms':dict(sorted(activity.items(),key=lambda kv:-kv[1])),
        'pg_slow_statements':[{'statement':x['statement'],'ms':x['ms'],'end_wall':round(x['end_wall'],3)} for x in log if x['kind']=='slow_statement' and window(x['end_wall'])]}


def markdown(summary):
    lines=[f"# NX-049 profile: {summary['label']}",'',f"limit {LIMIT_MS} ms; runs {len(summary['runs'])}",'']
    lines+=['|test|it|rc|wall s|guard authorize p50/p95/max ms|effect submit p50/max|O3 hit rate (hits/misses)|solo / overlapped p50 ms (python+sql)|first >2 s|','|---|---|---|---|---|---|---|---|---|']
    for r in summary['runs']:
        a=r['host_guard_requests'].get('/internal/v1/runtime/authorize',{});sb=r['host_guard_requests'].get('/internal/v1/runtime/effects/submit',{})
        f=r['first_over_limit'];first='—' if not f else f"{f['host_request']['path'].rsplit('/',1)[1]} {f['host_request']['total_ms']} ms {f['host_request']['error'] or ''}"
        c=r['fact_parse_cache'];cache=f"{c['hit_rate']} ({c['hits']}/{c['misses']})"+(' disabled' if c['disabled'] else '')
        so,ov=r['concurrency']['solo'],r['concurrency']['overlapped'];conc=f"{so['total_ms']} ({so['python_ms']}+{so['sql_ms']}) / {ov['total_ms']} ({ov['python_ms']}+{ov['sql_ms']})"
        lines.append(f"|{r['node'].split('::')[1][:40]}|{r['iteration']}|{r['rc']}|{r['wall_s']}|{a.get('p50')}/{a.get('p95')}/{a.get('max')}|{sb.get('p50')}/{sb.get('max')}|{cache}|{conc}|{first}|")
    for r in summary['runs']:
        f=r['first_over_limit'] or r['slowest_guard_request']
        if not f:continue
        lines+=['',f"## {r['node'].split('::')[1][:50]} it{r['iteration']}: {'first over limit' if r['first_over_limit'] else 'slowest guard request'}",'',
            '```json',json.dumps(f,ensure_ascii=False,indent=1)[:6000],'```']
        lines+=['',f"PG functions per constrained request (mean ms, top 8 by total): {r['pg_functions_per_request']['top_by_total_ms'][:8]}",
            f"Bursts of overlapping guard requests (same process): {r['concurrency']['bursts'][:6]}",
            f"Host startup: {r['host_startup']}",f"TLS handshakes: {r['tls_handshakes']}",f"slow statements: {r['slow_statements'][:5]}"]
    return '\n'.join(lines)+'\n'


def main(out):
    walls,procs,host,pgs,logs=load(out)
    runs=[]
    for run in walls:
        key=re.sub(r'[^A-Za-z0-9_.-]+','_',run['node'])[-150:]+f"-it{run['iteration']}"
        runs.append(analyse_run(run,procs,host,pgs.get(key),log_records(logs.get(key,''))))
    summary={'label':out.name,'host':(out/'host.txt').read_text() if (out/'host.txt').exists() else None,'runs':runs}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=1))
    (out/'summary.md').write_text(markdown(summary))
    print(markdown(summary)[:4000])


if __name__=='__main__':main(Path(sys.argv[1]).resolve())
