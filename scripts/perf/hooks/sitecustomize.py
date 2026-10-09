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
        __slots__=('name','start','child','breakdown')
        def __init__(self,name):self.name,self.start,self.child,self.breakdown=name,_clock(),0.0,{}

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
        if frame.name.startswith(top_events) and len(_events)<4000:
            with _glock:_events.append({'name':frame.name,'t':round(frame.start,6),'total':total,
                'breakdown':{k:round(v,6) for k,v in sorted(((k,v) for k,v in frame.breakdown.items() if not k.startswith('#')),key=lambda kv:-kv[1])[:12]},
                'counts':{k[1:]:v for k,v in frame.breakdown.items() if k.startswith('#')},
                'decisions':len(keys or ()),'distinct_decisions':len(set(keys or ()))})

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
        'eios.authz._fact_resolver':[('AuthorizationFactsResolver','resolve',lambda a,k:_resolve_label(a,k))],
        'eios.authz.service':[('AuthorizationDecisionService','decide','authz:decide'),('AuthorizationDecisionService','decide_resolved','authz:decide_resolved')],
        'nexloop_eios.authorization':[(None,'authenticate_service','authenticate_service'),('PostgresAuthorityUnitOfWork','_load','authz:load_fact'),
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
    }

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

    def _dump():
        Path(_OUT).mkdir(parents=True,exist_ok=True)
        cache=getattr(sys.modules.get('nexloop_eios.authorization'),'FACT_PARSE_CACHE',None)
        data={'pid':os.getpid(),'argv':sys.argv[:4],'fact_parse_cache':None if cache is None else {'hits':cache.hits,'misses':cache.misses,'disabled':_NOCACHE},'stats':{k:{'count':v[0],'total':v[1],'self':v[2],'max':v[3]} for k,v in _stats.items()},'events':_events}
        (Path(_OUT)/f'proc-{os.getpid()}.json').write_text(json.dumps(data))
    atexit.register(_dump)
