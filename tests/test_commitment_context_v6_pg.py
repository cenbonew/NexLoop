"""NX-026 slice 3 on clean catalog PostgreSQL: live commitments in Context v6 open work (0113).

The governed Role reevaluation fixture (NX-025 G3/G4) launches a Role Run on v6 for a plan of the Consumer; a
registered commitment made to that Consumer is a pinned, read-only formal open-work item re-derived by SQL at bind.
The commitment is seeded by admin as registry row plus object with exactly the prepared properties (the object guard
accepts nothing else); its registration path is covered by test_commitments_pg. Synthetic data only.
"""
import hashlib,json,secrets

import pytest
from psycopg.types.json import Jsonb
from nexloop_eios.context_artifacts import ContextArtifactUnavailable
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.role_context_artifacts import RoleContextV6ArtifactProducer
from nexloop_eios.plan_reevaluation import run_budget
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_role_launcher_pg import role_planning,role_settings,within_ceiling  # noqa: F401
from test_plan_reevaluation_pg import SETTINGS


def seed_commitment(admin,tenant,consumer,*,status='open'):
    commitment=secrets.token_hex(32);intent='commitment-'+secrets.token_hex(32)
    props={'made_to':consumer,'made_by':{'sender_kind':'agent'},'content_ref':{'claim_id':None,'message_id':None},'promised_at':'2026-10-10T00:00:00.000000Z',
        'due_at':'2099-01-01T00:00:00.000000Z','due_precision':'exact','condition':None,'status':status,'late':False,'fulfillment_basis':'undetermined',
        'fulfillment_evidence':[],'related_goal_ref':None,'supersedes':None}
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
        admin.execute('''insert into runtime.nexloop_commitments(tenant_id,world,commitment_id,dedupe_key,origin,consumer_id,supersedes,intent_id,properties,registered_at)
            values(%s,'real',%s,%s,'extend',%s,%s,%s,%s,null)''',(tenant,commitment,'seed:'+commitment,consumer,'0'*64,intent,Jsonb(props)))
        admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','Commitment',%s,1,%s,'nexloop-action',%s,clock_timestamp(),clock_timestamp())''',(tenant,commitment,Jsonb(props),intent))
        admin.execute('alter table runtime.nexloop_commitments disable trigger nx026_registry_guard')
        admin.execute('update runtime.nexloop_commitments set registered_at=clock_timestamp() where commitment_id=%s',(commitment,))
        admin.execute('alter table runtime.nexloop_commitments enable trigger nx026_registry_guard')
    return commitment


def consumer_of(f,plan_id):
    return f['admin'].execute('select consumer_id from runtime.nexloop_plans where plan_id=%s',(plan_id,)).fetchone()[0]


def test_live_commitments_are_pinned_formal_open_work_in_v6(role_planning):
    f=role_planning;admin=f['admin']
    s=f['establish'](steps=[within_ceiling()]);tenant=f['plan']['tenant'];consumer=consumer_of(f,s['plan_id'])
    live=seed_commitment(admin,tenant,consumer);closed=seed_commitment(admin,tenant,consumer,status='cancelled')
    other=seed_commitment(admin,tenant,'e'*64)
    summary=f['worker']().run_once();assert summary['launched']==1,summary
    (run_id,)=admin.execute('select run_id::text from runtime.nexloop_plan_runs where plan_id=%s',(s['plan_id'],)).fetchone()
    pack=json.loads(admin.execute('select pack_text from runtime.nexloop_role_context_artifacts where run_id=%s',(run_id,)).fetchone()[0])
    (item,)=[i for i in pack['open_work'] if i['subsection']=='commitment']
    assert item['ref']=='eios:object:Commitment/'+live and item['evidence_kind']=='formal_object' and item['revision']=='1' and item['tags']==[]
    c=item['content']
    assert c['commitment_ref']=='commitment:'+live and c['status']=='open' and c['due_at']=='2099-01-01T00:00:00.000000Z'
    assert c['evidence']=={'requested':0,'delivered':0,'customer_confirmed':0,'problem_resolved':'unavailable'} and c['quote'] is None
    refs=[i['ref'] for i in pack['open_work']]
    assert 'eios:object:Commitment/'+closed not in refs and 'eios:object:Commitment/'+other not in refs
    source=admin.execute('select s.content_hash,s.evidence_kind,s.status from runtime.nexloop_context_sources s join runtime.nexloop_context_packs p '
        'on p.tenant_id=s.tenant_id and p.world=s.world and p.context_id=s.context_id where p.run_id=%s and s.ref=%s',(run_id,item['ref'])).fetchone()
    assert source==(item['content_hash'],'formal_object','included')


def _without(pack):
    pack['open_work']=[i for i in pack['open_work'] if i['subsection']!='commitment']
    pack['budget_report']['sections']['open_work']['included']=len(pack['open_work'])


def _rewritten(pack):
    (item,)=[i for i in pack['open_work'] if i['subsection']=='commitment']
    item['content']['status']='fulfilled'
    item['content_hash']=hashlib.sha256(canonical_payload(item['content']).encode()).hexdigest()


@pytest.mark.parametrize('change,expected',[(_without,'context v6 pinned source missing'),(_rewritten,'context source content mismatch')])
def test_sql_refuses_a_pack_that_drops_or_rewrites_a_commitment(role_planning,monkeypatch,change,expected):
    f=role_planning;admin=f['admin']
    s=f['establish'](steps=[within_ceiling()]);seed_commitment(admin,f['plan']['tenant'],consumer_of(f,s['plan_id']))
    original=RoleContextV6ArtifactProducer.assemble;seen=[]
    def assemble(self,snapshot,command):
        body,outcome,items,proofs=original(self,snapshot,command);change(body);seen.append(self);return body,outcome,items,proofs
    monkeypatch.setattr(RoleContextV6ArtifactProducer,'assemble',assemble)
    w=f['worker']();decision=w.port.precheck(s['plan_id'],[]);budget=run_budget(SETTINGS,decision['plan_steps'])
    run=w.launcher.issue(decision,budget)
    with pytest.raises(ContextArtifactUnavailable):w.launcher.activate(run,decision,budget,[])
    assert seen[-1].last_diagnostic[1]==expected
