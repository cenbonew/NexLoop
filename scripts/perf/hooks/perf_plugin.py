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


def pytest_configure(config):
    import nexloop_eios.bootstrap as module
    original=module.bootstrap
    def bootstrap(admin,*a,**k):
        result=original(admin,*a,**k)
        if 'dsn' not in _state:
            admin.execute("alter system set track_functions='pl'");admin.execute('select pg_reload_conf()')
            admin.execute('select pg_stat_reset()')
            _state['dsn']=admin.info.dsn;_state['samples']=[];_state['stop']=threading.Event()
            thread=threading.Thread(target=_sample,daemon=True);_state['thread']=thread;thread.start()
        return result
    module.bootstrap=bootstrap


def _sample():
    with psycopg.connect(_state['dsn'],autocommit=True) as c:
        while not _state['stop'].wait(0.02):
            try:rows=c.execute("""select usename,state,coalesce(wait_event_type,'CPU'),coalesce(wait_event,'-'),left(query,80)
                from pg_stat_activity where usename like 'nexloop_%' and state='active' and pid<>pg_backend_pid()""").fetchall()
            except Exception:return
            if rows:_state['samples'].append([time.perf_counter(),rows])


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
    name=re.sub(r'[^A-Za-z0-9_.-]+','_',_state.get('test',request.node.nodeid))[-150:]
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/f'pg-{name}.json').write_text(json.dumps({'test':_state.get('test'),'call_seconds':_state.get('call_seconds'),
        'functions':[{'function':f,'calls':n,'total_ms':t,'self_ms':s} for f,n,t,s in rows],
        'wait_samples_20ms':dict(sorted(waits.items(),key=lambda kv:-kv[1])),
        'wait_samples_by_statement':{q:dict(sorted(v.items(),key=lambda kv:-kv[1])) for q,v in sorted(by_query.items(),key=lambda kv:-sum(kv[1].values()))}}))
    _state.clear()
