from role_tail_fixture import role_tail_plan
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan

def test_append_normal_role_runtime_and_artifact(role_tail_plan):
    plan=role_tail_plan
    for index,command in enumerate(plan['commands']):
        assert plan['worker'].authorize_runtime_activation(activation_ref=plan['activations'][index],command=command,operation='model')['authorized'] is True
        pack=plan['context_packs'][command['run_id']]
        assert plan['sources'][index].read_artifact(pack['artifact_ref'].removeprefix('artifact:'))==pack['input'].encode()

import pytest
@pytest.mark.parametrize('deadline_kind',['source_read','run_proof','role_validity'])
def test_actual_artifact_fact_lock_crosses_previous_deadline(request,monkeypatch,admin,pg,deadline_kind):
    import concurrent.futures,time,json,hmac
    from datetime import UTC,datetime,timedelta
    import psycopg
    from nexloop_eios.runtime_activation import RuntimeActivationPort
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.role_mapping import RoleMappingPort
    from eios.authz.errors import AuthorizationUnavailable
    deadline=[]
    if deadline_kind=='role_validity':
        create=RoleMappingPort.create_role
        def shorter(self,**kwargs):
            kwargs['valid_until']=(datetime.now(UTC)+timedelta(seconds=22)).isoformat()
            result=create(self,**kwargs)
            if not deadline:deadline.append(datetime.fromisoformat(kwargs['valid_until']))
            return result
        monkeypatch.setattr(RoleMappingPort,'create_role',shorter)
    plan=request.getfixturevalue('role_tail_plan')
    failures=[]
    original=RuntimeActivationPort._execute
    def execute(self,db,envelope,**kwargs):
        text,signature,payload=envelope;params=json.loads(payload)
        if deadline_kind=='role_validity':
            # Owned test only: permit a real natural role deadline to cross
            # the last lock; this is not a product timeout change.
            db.execute("set local lock_timeout='30000ms'; set local statement_timeout='30000ms'")
        if params.get('verb')=='authorize' and deadline_kind!='role_validity':
            expiry=datetime.now(UTC)+timedelta(seconds=2);deadline[:]=[expiry]
            claims=json.loads(text)
            if deadline_kind=='run_proof':
                params['run_proofs'][0]['expires_at']=expiry.isoformat();payload=canonical_payload(params);claims['parameters_digest']=__import__('hashlib').sha256(payload.encode()).hexdigest()
            else:
                nested=claims['context_role_envelope'];body=json.loads(nested['payload'])
                read=json.loads(body['reads']['role']['text']);read['expires_at']=expiry.isoformat()
                rtext=canonical_payload(read);body['reads']['role']={'text':rtext,'signature':hmac.new(self.signer.material,('nexloop-object-read-v1:'+rtext).encode(),'sha256').hexdigest()}
                innerpayload=canonical_payload(body);inner=json.loads(nested['text']);inner['parameters_digest']=__import__('hashlib').sha256(innerpayload.encode()).hexdigest()
                innertext=canonical_payload(inner);claims['context_role_envelope']={'payload':innerpayload,'text':innertext,'signature':hmac.new(self.signer.material,('nexloop-role-run-v1:'+innertext).encode(),'sha256').hexdigest()}
            text=canonical_payload(claims);signature=hmac.new(self.signer.material,(claims['protocol']+':'+text).encode(),'sha256').hexdigest();envelope=text,signature,payload
        try:return original(self,db,envelope,**kwargs)
        except psycopg.Error as error:
            failures.append(error.diag.message_primary);raise
    monkeypatch.setattr(RuntimeActivationPort,'_execute',execute)
    principal=plan['sources'][0]._session.authentication.subject_principal_id
    with psycopg.connect(pg) as blocker:
        assert blocker.execute("select 1 from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key=%s for update",(plan['tenant'],[principal,'eios:artifact:local_real'])).fetchone()==(1,)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(plan['worker'].authorize_runtime_activation,activation_ref=plan['activations'][0],command=plan['commands'][0],operation='model')
            timeout=time.monotonic()+8;observed=False
            while time.monotonic()<timeout:
                if admin.execute("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'select authz.nexloop_runtime_activation_command%%'").fetchone()[0]>0:observed=True;break
                if future.done():break
                time.sleep(.02)
            assert observed,'real Runtime SQL never waited on Artifact fact lock'
            assert deadline
            remaining=(deadline[0]-datetime.now(UTC)).total_seconds()
            if remaining>0:time.sleep(remaining+.08)
            blocker.commit()
            with pytest.raises(AuthorizationUnavailable):future.result(timeout=10)
            assert datetime.now(UTC)>deadline[0]
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)

def test_actual_private_helpers_denied_all_application_roles(role_tail_plan,admin,pg):
    import psycopg
    from psycopg.conninfo import make_conninfo
    private=admin.execute("select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='authz' and (p.proname like 'nexloop_role_ttl_%%' or p.proname='nexloop_role_business_deadline' or p.proname like '%%before_role_ttl_%%') order by p.proname").fetchall()
    assert len(private)>=12
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_scheduler','nexloop_action_worker','nexloop_identity'):
        assert all(not admin.execute('select has_function_privilege(%s,%s,\'EXECUTE\')',(role,name)).fetchone()[0] for (name,) in private)
        with psycopg.connect(make_conninfo(pg,user=role),autocommit=True) as conn:
            from psycopg import sql
            for (signature,) in private:
                qualified,arguments=signature.split('(',1);namespace,name=qualified.split('.',1)
                types=arguments.rstrip(')').split(',')
                assert all(kind in ('text','uuid','jsonb') for kind in types)
                statement=sql.SQL('select {}.{}({})').format(sql.Identifier(namespace),sql.Identifier(name),sql.SQL(',').join(sql.SQL('null::'+kind) for kind in types))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):conn.execute(statement)
    public={'nexloop_role_run_bind':['nexloop_api'],'nexloop_role_context_command':['nexloop_api'],'nexloop_runtime_activation_command':['nexloop_api','nexloop_domain_worker','nexloop_scheduler'],'nexloop_effect_intent_command':['nexloop_api','nexloop_domain_worker'],'nexloop_effect_execution_command':['nexloop_action_worker']}
    for name,allowed in public.items():
        signature='authz.'+name+'(text,text,text,text,text)'
        for role in ('nexloop_api','nexloop_domain_worker','nexloop_scheduler','nexloop_action_worker','nexloop_identity'):
            assert admin.execute("select has_function_privilege(%s,%s,'EXECUTE')",(role,signature)).fetchone()[0] is (role in allowed)

def test_context_bind_lock_expiry_rolls_back_binding_and_job(request,monkeypatch,admin,pg):
    import json,hmac,time,threading,psycopg
    from datetime import UTC,datetime,timedelta
    from nexloop_eios import role_runs,role_context_artifacts
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.context_artifacts import ContextArtifactUnavailable
    original=role_context_artifacts.RoleContextArtifactProducer._call
    read=role_runs._read_envelope
    deadline=[];observed=[];threads=[]
    def shorter(source,kind,ref,fields):
        env=read(source,kind,ref,fields)
        if kind=='RoleDefinition':
            claims=json.loads(env['text']);expiry=datetime.now(UTC)+timedelta(seconds=2);deadline[:]=[expiry];claims['expires_at']=expiry.isoformat()
            text=canonical_payload(claims);env={'text':text,'signature':hmac.new(source._backend._signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()}
        return env
    def call(self,parameters,run,definition,capability):
        if parameters['verb']!='bind':return original(self,parameters,run,definition,capability)
        blocker=psycopg.connect(pg)
        key=[self.session.authentication.subject_principal_id,'eios:artifact:local_real']
        assert blocker.execute("select 1 from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key=%s for update",(self.session.authentication.tenant_id,key)).fetchone()==(1,)
        monkeypatch.setattr(role_runs,'_read_envelope',shorter)
        def release():
            try:
                with psycopg.connect(pg,autocommit=True) as observer:
                    limit=time.monotonic()+8
                    while time.monotonic()<limit:
                        if observer.execute("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'select authz.nexloop_role_context_command%%'").fetchone()[0]>0:observed.append(True);break
                        time.sleep(.02)
                    if deadline:
                        delay=(deadline[0]-datetime.now(UTC)).total_seconds()
                        if delay>0:time.sleep(delay+.08)
            finally:blocker.commit();blocker.close()
        thread=threading.Thread(target=release);threads.append(thread);thread.start()
        return original(self,parameters,run,definition,capability)
    monkeypatch.setattr(role_context_artifacts.RoleContextArtifactProducer,'_call',call)
    try:
        with pytest.raises(ContextArtifactUnavailable):request.getfixturevalue('role_tail_plan')
    finally:
        for thread in threads:thread.join(12);assert not thread.is_alive()
    assert observed==[True] and deadline
    assert admin.execute('select count(*) from runtime.nexloop_role_context_artifacts').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)
