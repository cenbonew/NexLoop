"""pytest plugin (measurement only): server-side PL/pgSQL function times and wait sampling.

After the test's own bootstrap it enables `track_functions='pl'` on the disposable
test cluster and starts a 20 ms pg_stat_activity sampler (wait_event_type/event of
active NexLoop application backends). After the `admin` fixture is finalized (the
test's pools are closed, so backends have flushed their stats) it records
pg_stat_user_functions and the samples to <out>/pg-<test>.json.
"""
import json,os,re,threading,time
from pathlib import Path
import psycopg
import pytest

OUT=Path(os.environ.get('NEXLOOP_PERF_OUT') or (Path(__file__).resolve().parent.parent/'out'/'.active').read_text().strip())
_state={}
TIMELINE=os.environ.get('NEXLOOP_PERF_TIMELINE')=='1'


def pytest_configure(config):
    import nexloop_eios.bootstrap as module
    original=module.bootstrap
    def bootstrap(admin,*a,**k):
        result=original(admin,*a,**k)
        if 'dsn' not in _state:
            admin.execute("alter system set track_functions='pl'")
            if TIMELINE:
                # NX-049: slow statements and lock waits with timestamps in the server log; deadlock_timeout
                # (which gates log_lock_waits) is left at its default so lock behaviour is unchanged.
                admin.execute("alter system set log_min_duration_statement='200ms'");admin.execute("alter system set log_lock_waits=on")
                admin.execute("alter system set log_line_prefix='%m [%p] %u '");admin.execute("alter system set log_timezone='UTC'")
                # Bound parameter values (signed claims, test keys) never reach the log.
                admin.execute("alter system set log_parameter_max_length=0");admin.execute("alter system set log_parameter_max_length_on_error=0")
                _state['log']=admin.execute("select setting from pg_settings where name='data_directory'").fetchone()[0]
            admin.execute('select pg_reload_conf()')
            admin.execute('select pg_stat_reset()')
            if (OUT.parent/'.trace_read_assert').exists():_trace_read_assert(admin)
            _state['dsn']=admin.info.dsn;_state['samples']=[];_state['stop']=threading.Event()
            thread=threading.Thread(target=_sample,daemon=True);_state['thread']=thread;thread.start()
        return result
    module.bootstrap=bootstrap


def _trace_read_assert(admin):
    """Measurement only, disposable test cluster: log each assert_read_authority call
    (backend pid, statement start, claims digest, target) to the server log via RAISE LOG,
    which also works inside read-only transactions. Wraps the current function in place."""
    acl=admin.execute("""select coalesce(array_agg(distinct grantee::regrole::text),'{}') from pg_proc p,
        aclexplode(p.proacl) a where p.oid='authz.nexloop_assert_read_authority(text,text,jsonb)'::regprocedure and a.privilege_type='EXECUTE'
        and a.grantee<>0 and a.grantee::regrole::text<>'nexloop_owner'""").fetchone()[0]
    admin.execute('alter function authz.nexloop_assert_read_authority(text,text,jsonb) rename to nexloop_assert_read_authority_perf_orig')
    admin.execute("""create function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
        language plpgsql security definer set search_path=pg_catalog as $f$
        begin
         -- O5b phase marking: visible-state digest and locks held at this assertion.
         raise log 'perfra|%|%|%|%|%|%|%',pg_backend_pid(),statement_timestamp(),md5(p_claims::text),p_claims->>'target_resource',
          md5(pg_current_snapshot()::text),
          (select count(*) from pg_locks l where l.pid=pg_backend_pid() and l.granted and l.locktype='advisory'),
          (select count(*) from pg_locks l where l.pid=pg_backend_pid() and l.granted);
         return authz.nexloop_assert_read_authority_perf_orig(p_digest,p_world,p_claims);
        end $f$""")
    admin.execute('alter function authz.nexloop_assert_read_authority(text,text,jsonb) owner to nexloop_owner')
    admin.execute('revoke all on function authz.nexloop_assert_read_authority(text,text,jsonb) from public')
    for role in acl:admin.execute(f'grant execute on function authz.nexloop_assert_read_authority(text,text,jsonb) to {role}')
    _state['log']=admin.execute("select setting from pg_settings where name='data_directory'").fetchone()[0]


def _sample():
    with psycopg.connect(_state['dsn'],autocommit=True) as c:
        while not _state['stop'].wait(0.02):
            if TIMELINE:
                try:
                    # Lock waits with their blockers (wall clock, comparable with the Python/Host timeline).
                    waits=c.execute("""select a.pid,a.usename,a.wait_event_type,a.wait_event,left(a.query,100),
                        (extract(epoch from clock_timestamp()-a.query_start)*1000)::float8,pg_blocking_pids(a.pid),
                        (select coalesce(json_agg(json_build_object('pid',b.pid,'state',b.state,'query',left(b.query,100))),'[]') from pg_stat_activity b where b.pid=any(pg_blocking_pids(a.pid)))
                        from pg_stat_activity a where a.usename like 'nexloop_%' and a.wait_event_type='Lock'""").fetchall()
                    if waits:_state.setdefault('lock_waits',[]).append([time.time(),[list(w) for w in waits]])
                except Exception:pass
            try:rows=c.execute("""select usename,state,coalesce(wait_event_type,'CPU'),coalesce(wait_event,'-'),left(query,80)
                from pg_stat_activity where usename like 'nexloop_%' and state='active' and pid<>pg_backend_pid()""").fetchall()
            except Exception:return
            if rows:_state['samples'].append([time.time() if TIMELINE else time.perf_counter(),rows])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _state['test']=item.nodeid;start=time.perf_counter()
    yield
    _state['call_seconds']=time.perf_counter()-start


def pytest_fixture_post_finalizer(fixturedef,request):
    if fixturedef.argname!='admin' or 'dsn' not in _state:return
    _state['stop'].set();_state['thread'].join(2)
    time.sleep(0.5)
    rows=[]
    try:
        with psycopg.connect(_state['dsn'],autocommit=True) as c:
            rows=c.execute("""select schemaname||'.'||funcname,calls,total_time,self_time from pg_stat_user_functions
                where schemaname in ('authz','control','runtime','ontology') order by self_time desc""").fetchall()
    except Exception as error:rows=[['unavailable:'+type(error).__name__,0,0,0]]
    waits={};by_query={}
    for _,sample in _state['samples']:
        for user,state,kind,event,query in sample:
            key=f'{kind}:{event}';waits[key]=waits.get(key,0)+1
            m=re.search(r'select\s+([a-z_]+\.[a-z0-9_]+)\s*\(',query or '',re.I)
            q=(m.group(1) if m else (query or '')[:40]).lower()
            by_query.setdefault(q,{});by_query[q][key]=by_query[q].get(key,0)+1
    name=re.sub(r'[^A-Za-z0-9_.-]+','_',_state.get('test',request.node.nodeid))[-150:]+(('-it'+os.environ['NEXLOOP_PERF_ITER']) if os.environ.get('NEXLOOP_PERF_ITER') else '')
    if _state.get('log'):
        import pathlib,shutil
        log=pathlib.Path(_state['log']).parent/'postgres.log'
        if log.exists():shutil.copy(log,OUT/f'pglog-{name}.log')
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/f'pg-{name}.json').write_text(json.dumps({'test':_state.get('test'),'call_seconds':_state.get('call_seconds'),
        'functions':[{'function':f,'calls':n,'total_ms':t,'self_ms':s} for f,n,t,s in rows],
        'wait_samples_20ms':dict(sorted(waits.items(),key=lambda kv:-kv[1])),
        'lock_waits':_state.get('lock_waits',[]) if TIMELINE else [],
        'active_samples':[[w,[list(r) for r in rows]] for w,rows in _state['samples']] if TIMELINE else [],
        'wait_samples_by_statement':{q:dict(sorted(v.items(),key=lambda kv:-kv[1])) for q,v in sorted(by_query.items(),key=lambda kv:-sum(kv[1].values()))}},default=str))
    _state.clear()
