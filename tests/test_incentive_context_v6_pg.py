"""AT-043 D8 on clean catalog PostgreSQL: the incentives a Consumer may still be issued in Context v6 open work (0145).

The governed Role reevaluation fixture (as the NX-026 commitment context tests) launches a Role Run on v6; each bound,
active, valid IncentiveOffering of the Consumer is a formal open-work item with its issuable range, re-derived by SQL at
bind; the budget balance is never shown. Catalog and budget are seeded as their governed writers leave them."""
import hashlib,json

import pytest
from nexloop_eios.context_artifacts import ContextArtifactUnavailable
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.role_context_artifacts import RoleContextV6ArtifactProducer
from nexloop_eios.plan_reevaluation import run_budget
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_role_launcher_pg import role_planning,role_settings,within_ceiling  # noqa: F401
from test_plan_reevaluation_pg import SETTINGS
from test_commitment_context_v6_pg import consumer_of
from test_incentive_runs_pg import seed_catalog


def test_bound_incentives_are_open_work_with_their_issuable_range_and_no_budget(role_planning):
    f=role_planning;admin=f['admin']
    s=f['establish'](steps=[within_ceiling()]);tenant=f['plan']['tenant'];consumer=consumer_of(f,s['plan_id'])
    offer=seed_catalog(admin,tenant,consumer);other=seed_catalog(admin,tenant,'e'*64)
    summary=f['worker']().run_once();assert summary['launched']==1,summary
    (run_id,)=admin.execute('select run_id::text from runtime.nexloop_plan_runs where plan_id=%s',(s['plan_id'],)).fetchone()
    pack=json.loads(admin.execute('select pack_text from runtime.nexloop_role_context_artifacts where run_id=%s',(run_id,)).fetchone()[0])
    (item,)=[i for i in pack['open_work'] if i['subsection']=='incentive']
    assert item['ref']=='eios:object:IncentiveOffering/'+offer['incentive_offering_id'] and item['evidence_kind']=='formal_object' and item['revision']=='1'
    c=item['content']
    assert c['incentive_code']=='renewal-credit' and c['currency']=='CNY' and c['binding_id']==offer['binding_id'] and c['offering_revision']==1
    assert c['issuable_minor']=={'min':'1','max':'500'}  # single-issue maximum below the per-Consumer period limit
    assert not any(key in json.dumps(c) for key in ('limit_amount','10000','remaining','used'))  # never the budget
    assert 'eios:object:IncentiveOffering/'+other['incentive_offering_id'] not in [i['ref'] for i in pack['open_work']]


def _rewritten(pack):
    (item,)=[i for i in pack['open_work'] if i['subsection']=='incentive']
    item['content']['issuable_minor']['max']='9999'
    item['content_hash']=hashlib.sha256(canonical_payload(item['content']).encode()).hexdigest()


def test_sql_refuses_a_pack_that_widens_an_incentive(role_planning,monkeypatch):
    f=role_planning;admin=f['admin']
    s=f['establish'](steps=[within_ceiling()]);seed_catalog(admin,f['plan']['tenant'],consumer_of(f,s['plan_id']))
    original=RoleContextV6ArtifactProducer.assemble;seen=[]
    def assemble(self,snapshot,command):
        body,outcome,items,proofs=original(self,snapshot,command);_rewritten(body);seen.append(self);return body,outcome,items,proofs
    monkeypatch.setattr(RoleContextV6ArtifactProducer,'assemble',assemble)
    w=f['worker']();decision=w.port.precheck(s['plan_id'],[]);budget=run_budget(SETTINGS,decision['plan_steps'])
    run=w.launcher.issue(decision,budget)
    with pytest.raises(ContextArtifactUnavailable):w.launcher.activate(run,decision,budget,[])
    assert seen[-1].last_diagnostic[1]=='context source content mismatch'
