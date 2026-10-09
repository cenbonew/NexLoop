"""Measurement-only timing hooks for NexLoop authorization diagnosis.

Loaded only when scripts/perf/hooks is on PYTHONPATH (scripts/perf/run_profile.sh).
It monkeypatches, at import time and only inside this measuring process tree:
  * psycopg Cursor.execute (each SQL statement, keyed by called function),
  * psycopg_pool ConnectionPool.getconn (pool acquisition wait),
  * the Backend request lock (time waiting for the serialized backend),
  * named authorization/governance functions (inclusive + exclusive time).
No product file is modified. Each process writes <out>/proc-<pid>.json at exit.
"""
import atexit,importlib.abc,importlib.machinery,json,os,re,sys,threading,time
from pathlib import Path

_HERE=Path(__file__).resolve().parent
_OUT=os.environ.get('NEXLOOP_PERF_OUT') or ((_HERE.parent/'out'/'.active').read_text().strip() if (_HERE.parent/'out'/'.active').exists() else '')

if _OUT:
    _clock=time.perf_counter
    _NOCACHE=os.environ.get('NEXLOOP_PERF_NO_FACT_CACHE')=='1' or (_HERE.parent/'out'/'.nocache').exists()
    _local=threading.local()
    _stats={};_events=[];_glock=threading.Lock()
    _SQL=re.compile(r'select\s+([a-z_]+\.[a-z0-9_]+)\s*\(',re.I)

    def _record(name,total,own):
        with _glock:
            s=_stats.setdefault(name,[0,0.0,0.0,0.0])
            s[0]+=1;s[1]+=total;s[2]+=own;s[3]=max(s[3],total)

    class _Frame:
        __slots__=('name','start','child','breakdown','wall','pgfn')
        def __init__(self,name):self.name,self.start,self.child,self.breakdown,self.wall,self.pgfn=name,_clock(),0.0,{},time.time(),{}

    def _enter(name):
        stack=getattr(_local,'stack',None)
        if stack is None:stack=_local.stack=[]
        frame=_Frame(name);stack.append(frame);return frame

    def _exit(frame,top_events=('invoke:','govern','authenticate_service')):
        stack=_local.stack;stack.pop()
        total=_clock()-frame.start;_record(frame.name,total,total-frame.child)
        frame.breakdown[frame.name+' (self)']=frame.breakdown.get(frame.name+' (self)',0.0)+total-frame.child
        frame.breakdown['#'+frame.name]=frame.breakdown.get('#'+frame.name,0)+1
        keys=frame.breakdown.pop('@keys',None)
        if stack:
            parent=stack[-1];parent.child+=total
            for k,v in frame.breakdown.items():parent.breakdown[k]=parent.breakdown.get(k,0.0)+v
            if keys and not frame.name.startswith('invoke:'):parent.breakdown.setdefault('@keys',[]).extend(keys)
            for fn,v in frame.pgfn.items():
                acc=parent.pgfn.setdefault(fn,[0,0.0,0.0]);acc[0]+=v[0];acc[1]+=v[1];acc[2]+=v[2]
        if frame.name.startswith(top_events) and len(_events)<4000:
            with _glock:_events.append({'name':frame.name,'t':round(frame.start,6),'wall':round(frame.wall,6),'thread':threading.get_ident(),'total':total,
                'breakdown':{k:round(v,6) for k,v in sorted(((k,v) for k,v in frame.breakdown.items() if not k.startswith(('#','@'))),key=lambda kv:-kv[1])[:12]},
                'counts':{k[1:]:v for k,v in frame.breakdown.items() if k.startswith('#')},
                'decisions':len(keys or ()),'distinct_decisions':len(set(keys or ())),
                # NX-049: PG function calls/total/self (ms) executed inside this request (pg_stat_xact_user_functions deltas).
                'pg_functions':{k:[v[0],round(v[1],3),round(v[2],3)] for k,v in sorted(frame.pgfn.items(),key=lambda kv:-kv[1][1])[:40]},
                'probe_ms':round(1000*frame.breakdown.get('@probe',0.0),3)})

    def _wrap(fn,label):
        if getattr(fn,'_nexloop_perf',False):return fn
        def wrapper(*a,**k):
            name=label(a,k) if callable(label) else label
            frame=_enter(name)
            try:return fn(*a,**k)
            finally:_exit(frame)
        wrapper._nexloop_perf=True;wrapper.__wrapped__=fn;wrapper.__name__=getattr(fn,'__name__','wrapped')
        return wrapper

    def _sql_label(a,k):
        query=a[1] if len(a)>1 else k.get('query','')
        text=query if isinstance(query,str) else getattr(query,'as_string',lambda c=None:str(query))(None) if hasattr(query,'as_string') else str(query)
        m=_SQL.search(text or '')
        return 'sql:'+(m.group(1).lower() if m else ' '.join((text or '').split())[:48])

    TARGETS={
        'psycopg':[('Cursor','execute',_sql_label)],
        'psycopg_pool.pool':[('ConnectionPool','getconn','pool:getconn')],
        'eios.authz._fact_resolver':[('AuthorizationFactsResolver','resolve',lambda a,k:_resolve_label(a,k)),
            # O2b batch path resolves through the public resolve_in_unit_of_work; count it as a decision too.
            ('AuthorizationFactsResolver','resolve_in_unit_of_work',lambda a,k:_resolve_label(a,k))],
        'eios.authz.service':[('AuthorizationDecisionService','decide','authz:decide'),('AuthorizationDecisionService','decide_resolved','authz:decide_resolved')],
        'nexloop_eios.authorization':[(None,'resolve_authorities','authz:batch_resolve'),(None,'authenticate_service','authenticate_service'),('PostgresAuthorityUnitOfWork','_load','authz:load_fact'),
            ('PostgresAuthorityProvider','open_unit_of_work','authz:open_unit_of_work'),(None,'_identity','authz:identity_snapshot')],
        'nexloop_eios.postgres_artifacts':[(None,'canonical_payload','serialize:canonical_payload')],
        'nexloop_eios.action_definitions':[('PostgresActionDefinitionReader','get_with_schemas','governance:action_definition_read')],
        'nexloop_eios.action_governor':[(None,'govern_published_action','govern')],
        'nexloop_eios.postgres_action_claims':[('PostgresActionClaimPort','reserve','governance:claim_reserve')],
        'nexloop_eios.runtime_activation':[('RuntimeActivationPort','_proof','runtime:proof'),('RuntimeActivationPort','_signed','runtime:signed_command')],
        'nexloop_eios.backend':[('Backend','_invoke',lambda a,k:'invoke:'+str(a[2] if len(a)>2 else k.get('operation')))],
        'hmac':[(None,'new','sign:hmac')],
        # Non-Backend units of work (background jobs / readers) are tracked like requests.
        'nexloop_eios.claim_extraction_jobs':[('ClaimExtractionScheduler','run_once','invoke:job:claim_schedule'),('ClaimExtractionWorker','run_once','invoke:job:claim_extract')],
        'nexloop_eios.claim_store':[('ConversationClaimExtractor','extract','invoke:job:extract_window')],
        'nexloop_eios.claim_matching':[('ClaimMatcher','match_claim','invoke:job:match_claim'),('ClaimMatcher','apply','invoke:job:match_apply')],
        'nexloop_eios.candidate_merge':[('CandidateGluer','process','invoke:job:glue_process'),('ReviewQueueReader','pending','invoke:job:review_pending'),('ReviewQueueReader','candidate','invoke:job:review_candidate')],
        # 0077 message READ derivation and 0084 property-access derivation proofs.
        'nexloop_eios.message_read':[(None,'message_read_basis','derive:message_read_basis'),(None,'derived_message_read_envelope','derive:message_read_envelope')],
        'nexloop_eios.property_access':[(None,'property_access_basis','derive:property_access_basis'),(None,'derived_claims','derive:property_claims')],
        'nexloop_eios.role_runs':[(None,'role_envelope_for_run','envelope:role')],
        'nexloop_eios.service_offerings':[(None,'catalog_envelope_from_hint','envelope:catalog')],
        'nexloop_eios.recall':[('OntologyRecall','recall','invoke:job:recall')],
        # NX-049: Host control requests from the dispatcher and context assembly units.
        'nexloop_eios.runtime_dispatch':[('RuntimeDispatcher','run_once','invoke:dispatch:run_once')],
        'nexloop_eios.message_relay':[('MessageRelay','run_once','invoke:relay:run_once')],
    }

    # NX-049 (NEXLOOP_PERF_CPROFILE=1): cProfile of AuthorizationFactsResolver.resolve /
    # resolve_in_unit_of_work (the pure-Python part of `authz:resolve_facts`), aggregated per
    # process into <out>/cprof-<pid>.pstats; read with scripts/perf/cprof_report.py. The profiler
    # is process-wide, so only one resolve is profiled at a time (overlapping ones run unprofiled).
    _CPROF=os.environ.get('NEXLOOP_PERF_CPROFILE')=='1';_cpstats=[None,0,0]
    if _CPROF:
        import cProfile,pstats
        _cplock=threading.Lock()
        def _profiled(fn):
            def wrapper(*a,**k):
                if getattr(_local,'cprof',False) or not _cplock.acquire(blocking=False):
                    if not getattr(_local,'cprof',False):_cpstats[2]+=1
                    return fn(*a,**k)
                _local.cprof=True;profile=cProfile.Profile()
                try:
                    profile.enable()
                    try:return fn(*a,**k)
                    finally:profile.disable()
                finally:
                    _local.cprof=False;stats=pstats.Stats(profile)
                    if _cpstats[0] is None:_cpstats[0]=stats
                    else:_cpstats[0].add(stats)
                    _cpstats[1]+=1;_cplock.release()
            wrapper.__wrapped__=fn;wrapper.__name__=getattr(fn,'__name__','wrapped')
            return wrapper

    # NX-049 (timeline mode and NEXLOOP_PERF_PGFUNC=1; off by default because the probe adds about
    # 50 pg_stat queries / ~200 ms per guard request on the deploy host): after each statement that calls an
    # authz/control/runtime/ontology function inside a transaction block, read this backend's
    # pg_stat_xact_user_functions on the same connection and attribute the delta to the
    # enclosing request. The probe itself is timed as `perf:pgfunc_probe`.
    _PGFN=os.environ.get('NEXLOOP_PERF_TIMELINE')=='1' and os.environ.get('NEXLOOP_PERF_PGFUNC','0')=='1'
    _FN_SQL=re.compile(r'select\s+(authz|control|runtime|ontology)\.[a-z0-9_]+\s*\(',re.I)
    _PROBE=("select coalesce(json_object_agg(schemaname||'.'||funcname,json_build_array(calls,total_time,self_time)),'{}') "
            "from pg_stat_xact_user_functions")
    _xact={}
    def _probe(cursor):
        import psycopg
        connection=cursor.connection
        if connection.info.transaction_status!=psycopg.pq.TransactionStatus.INTRANS:return
        _local.probing=True;frame=_enter('perf:pgfunc_probe');began=_clock()
        try:
            with connection.cursor() as c:stats=c.execute(_PROBE).fetchone()[0]
        except Exception:return
        finally:
            _exit(frame);_local.probing=False
            # Inclusive probe time (statement + JSON) so readers can subtract the measurement cost.
            for f in reversed(getattr(_local,'stack',None) or []):
                if f.name.startswith('invoke:'):f.breakdown['@probe']=f.breakdown.get('@probe',0.0)+_clock()-began;break
        # Pending per-backend counters span transactions until the backend flushes its stats;
        # always diff against this connection's previous reading, and treat a drop as a flush.
        key=id(connection);base=_xact.get(key,{})
        _xact[key]=stats
        for f in reversed(getattr(_local,'stack',None) or []):
            if f.name.startswith('invoke:'):
                for name,(calls,total,own) in stats.items():
                    b=base.get(name,[0,0.0,0.0])
                    if calls<b[0]:b=[0,0.0,0.0]
                    if calls-b[0]>0:
                        acc=f.pgfn.setdefault(name,[0,0.0,0.0]);acc[0]+=calls-b[0];acc[1]+=total-b[1];acc[2]+=own-b[2]
                break

    def _note_decision(key):
        stack=getattr(_local,'stack',None) or []
        for frame in reversed(stack):
            if frame.name.startswith('invoke:'):
                frame.breakdown.setdefault('@keys',[]).append(key);return

    _HEX=re.compile(r'[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}')
    def _resolve_label(a,k):
        try:
            target=(a[1] if len(a)>1 else k['query']).target
            _record('decision_target:'+target.resource_type.value+':'+target.operation.value+':'+_HEX.sub('<id>',target.resource_id),0.0,0.0)
            q=a[1] if len(a)>1 else k['query']
            _note_decision((q.authentication.subject_principal_id,q.authentication.credential_id,q.tenant_id,q.request_attributes.get('world'),q.request_attributes.get('run_id'),target.resource_type.value,target.resource_id,target.operation.value))
        except Exception:pass
        return 'authz:resolve_facts'

    def _patch(module):
        for owner,attr,label in TARGETS.get(module.__name__,()):
            target=getattr(module,owner,None) if owner else module
            if target is None or not hasattr(target,attr):continue
            original=getattr(target,attr)
            if isinstance(original,(staticmethod,classmethod)):continue
            setattr(target,attr,_wrap(original,label))
            if _CPROF and module.__name__=='eios.authz._fact_resolver':setattr(target,attr,_profiled(getattr(target,attr)))
        if module.__name__=='psycopg' and _PGFN and not getattr(module.Cursor.execute,'_nexloop_pgfn',False):
            timed=module.Cursor.execute
            def execute(self,query,*a,**k):
                result=timed(self,query,*a,**k)
                if not getattr(_local,'probing',False) and _FN_SQL.search(query if isinstance(query,str) else str(query)):_probe(self)
                return result
            execute._nexloop_pgfn=True;execute._nexloop_perf=True
            module.Cursor.execute=execute
        if module.__name__=='nexloop_eios.authorization' and _NOCACHE and hasattr(module,'FactParseCache'):
            # Measurement baseline only: same code path with the O3 parse cache bypassed.
            module.FactParseCache.parse=lambda self,model,text:model.model_validate_json(text)
        if module.__name__=='nexloop_eios.backend':
            init=module.Backend.__init__
            def __init__(self,*a,**k):
                init(self,*a,**k)
                inner=self._lock
                class TimedLock:
                    # Measures waiting for the shared request hold; keeps exclusive() semantics.
                    def __enter__(s):
                        f=_enter('lock:backend_request_lock')
                        try:return inner.__enter__()
                        finally:_exit(f)
                    def __exit__(s,*e):return inner.__exit__(*e)
                    def acquire(s,*x,**y):return inner.acquire(*x,**y)
                    def release(s):return inner.release()
                    def exclusive(s):return inner.exclusive()
                self._lock=TimedLock()
            module.Backend.__init__=__init__

    class _Loader(importlib.abc.Loader):
        def __init__(self,loader):self.loader=loader
        def create_module(self,spec):return self.loader.create_module(spec)
        def exec_module(self,module):
            self.loader.exec_module(module);_patch(module)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self,name,path,target=None):
            if name not in TARGETS:return None
            spec=importlib.machinery.PathFinder.find_spec(name,path)
            if spec is not None and spec.loader is not None:spec.loader=_Loader(spec.loader)
            return spec

    for loaded in list(sys.modules.values()):
        if getattr(loaded,'__name__',None) in TARGETS:_patch(loaded)
    sys.meta_path.insert(0,_Finder())

    # NX-049 timeline (only with NEXLOOP_PERF_TIMELINE=1): wall-clock records comparable across
    # processes: guard HTTP handling, TLS handshakes, dispatcher→Host requests, Host spawn, and
    # the Node preload (host_timeline.mjs) injected into the Agent Host. Timings/paths only.
    _timeline=[]
    if os.environ.get('NEXLOOP_PERF_TIMELINE')=='1':
        import http.server,ssl,subprocess
        def _note(**record):
            if len(_timeline)<20000:
                with _glock:_timeline.append(record)
        handle=http.server.BaseHTTPRequestHandler.handle_one_request
        def handle_one_request(self):
            wall=time.time();start=_clock();self._perf_status=None
            try:return handle(self)
            finally:
                if getattr(self,'path',None):_note(kind='guard_request',path=self.path.split('?')[0],start_wall=wall,total_ms=1000*(_clock()-start),status=self._perf_status,thread=threading.get_ident())
        http.server.BaseHTTPRequestHandler.handle_one_request=handle_one_request
        send=http.server.BaseHTTPRequestHandler.send_response
        def send_response(self,code,message=None):
            self._perf_status=code;return send(self,code,message)
        http.server.BaseHTTPRequestHandler.send_response=send_response
        handshake=ssl.SSLSocket.do_handshake
        def do_handshake(self,*a,**k):
            wall=time.time();start=_clock();error=None
            try:return handshake(self,*a,**k)
            except Exception as e:error=type(e).__name__;raise
            finally:_note(kind='tls_handshake',server_side=bool(getattr(self,'server_side',False)),start_wall=wall,total_ms=1000*(_clock()-start),error=error,thread=threading.get_ident())
        ssl.SSLSocket.do_handshake=do_handshake
        popen=subprocess.Popen.__init__
        def popen_init(self,args,*a,**k):
            text=' '.join(map(str,args)) if isinstance(args,(list,tuple)) else str(args)
            if 'agent_host.py' in text:_note(kind='host_spawn',start_wall=time.time())
            return popen(self,args,*a,**k)
        subprocess.Popen.__init__=popen_init
        try:
            import httpx
            hsend=httpx.Client.send
            def client_send(self,request,*a,**k):
                wall=time.time();start=_clock();status=None;error=None
                try:
                    response=hsend(self,request,*a,**k);status=response.status_code;return response
                except Exception as e:error=type(e).__name__;raise
                finally:_note(kind='http_client',path=request.url.path,start_wall=wall,total_ms=1000*(_clock()-start),status=status,error=error,thread=threading.get_ident())
            httpx.Client.send=client_send
        except Exception:pass
        if sys.argv and sys.argv[0].endswith('agent_host.py'):
            # The Host launcher passes only an allow-listed environment to Node; add the preload there.
            execve=os.execve
            def traced_execve(path,argv,env):
                env=dict(env);env['NEXLOOP_PERF_OUT']=_OUT
                env['NODE_OPTIONS']=(env.get('NODE_OPTIONS','')+' --import '+(_HERE/'host_timeline.mjs').as_uri()).strip()
                return execve(path,argv,env)
            os.execve=traced_execve

    def _dump():
        Path(_OUT).mkdir(parents=True,exist_ok=True)
        cache=getattr(sys.modules.get('nexloop_eios.authorization'),'FACT_PARSE_CACHE',None)
        data={'pid':os.getpid(),'argv':sys.argv[:4],'timeline':_timeline,'fact_parse_cache':None if cache is None else {'hits':cache.hits,'misses':cache.misses,'disabled':_NOCACHE},'stats':{k:{'count':v[0],'total':v[1],'self':v[2],'max':v[3]} for k,v in _stats.items()},'events':_events}
        (Path(_OUT)/f'proc-{os.getpid()}.json').write_text(json.dumps(data))
        if _cpstats[0] is not None:
            _cpstats[0].dump_stats(str(Path(_OUT)/f'cprof-{os.getpid()}.pstats'))
            (Path(_OUT)/f'cprof-{os.getpid()}.json').write_text(json.dumps({'profiled':_cpstats[1],'skipped_overlapping':_cpstats[2]}))
    atexit.register(_dump)
