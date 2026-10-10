"""NX-024 follow-up (NX-025 G3): the production Role launch of a reevaluation Run on clean catalog PostgreSQL.

The governed Role fixture (two Sources, Role definitions with execution ceilings, assignment scopes and links,
shared PlanStep, v6 strategy) supplies the plan recipe. The reevaluation worker issues the Run through the
production Role issuance (role_policies.issue_role_run: Run credential + Role policy binding in one transaction),
links it to the plan, binds the Planner effect context and activates the Role plan on Context v6. The fixture's
Role issuance hooks (issue_run_credential / accept_runtime_event wrappers for its own Runs) are not on this path:
the launcher calls role_policies.issue_role_run and role_activation.activate_role_plan directly.
Synthetic data only; admin seeds and probes.
"""
from datetime import UTC,datetime,timedelta

import json

import pytest
from nexloop_eios.context_artifacts import ContextArtifactUnavailable
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.role_context_artifacts import RoleContextV6ArtifactProducer
from nexloop_eios.plan_reevaluation import run_budget
import runtime_effect_fixture as fixture
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from nexloop_eios.plan_reevaluation import PlanPort,PlanReevaluationWorker,RoleRunLauncher
from test_context_v6_pg import STRATEGY,seed_strategy
from test_plan_reevaluation_pg import SETTINGS,make_due,publish_goal,reevaluator,services,spec,step


@pytest.fixture
def role_settings():
    """The deployment settings; actual-Host tests override this with the deterministic runtime profile."""
    return SETTINGS


@pytest.fixture
def role_planning(monkeypatch,admin,request,role_settings):
    install=fixture.install_runtime_catalog
    def installed(admin_,tenant,*args):
        out=install(admin_,tenant,*args);seed_strategy(admin_,tenant,STRATEGY);return out
    monkeypatch.setattr(fixture,'install_runtime_catalog',installed)
    plan=request.getfixturevalue('role_runtime_plan');tenant=plan['tenant']
    publish_goal(admin,tenant,0,'v1')
    session=reevaluator(plan,admin)
    principal=plan['sources'][0]._session.authentication.subject_principal_id
    selected=plan['role_selection'][principal];catalog=plan['role_catalogs'][principal]
    recipe=dict(role_id=selected['role_id'],link_id=selected['link_id'],step_id=selected['step_id'],offering_id=catalog['offering_id'],
        binding_id=catalog['binding_id'],control_id=catalog['control_id'])
    api=plan['api']
    launcher=RoleRunLauncher(source=lambda:api.authenticate(plan['source_tokens'][0],world='real'),
        queue_service=lambda:api.authenticate(plan['owner_token'],world='real'),planner=lambda:api.authenticate(plan['planner_token'],world='real'),
        executor_token=plan['executor_token'],settings=role_settings,effect_action='eios:action:'+fixture.EFFECT+':1')
    def worker():return PlanReevaluationWorker(*services(session()),settings=role_settings,launcher=launcher)
    def establish(**changes):
        s=spec(plan,recipe=recipe,**changes);PlanPort(*services(session())).establish(s);make_due(admin);return s
    yield dict(plan=plan,admin=admin,worker=worker,establish=establish,principal=principal)


def soon():return (datetime.now(UTC)+timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00','Z')


def within_ceiling():
    # The Role ceiling of the fixture: 8 turns / 8 tool calls / 60 s / 1.0 USD.
    return step(reassess_at=soon(),budget={'maximum_model_turns':6,'maximum_tool_calls':8,'active_timeout_seconds':60})


def test_due_plan_launches_a_governed_role_run_on_v6(role_planning):
    f=role_planning;admin=f['admin']
    s=f['establish'](steps=[within_ceiling()])
    summary=f['worker']().run_once()
    assert summary['launched']==1,(summary,admin.execute("select status,last_code from runtime.nexloop_work_feed where feed='plan-reevaluate'").fetchall())
    (run_id,version),=admin.execute('select run_id::text,version from runtime.nexloop_plan_runs where plan_id=%s',(s['plan_id'],)).fetchall()
    assert version==1
    # Production Role issuance: the Run credential and its Role policy binding exist together, with the reevaluation budget.
    assert admin.execute('select allowed_resources from authz.nexloop_run_credentials where run_id=%s',(run_id,)).fetchone()[0]==['eios:action:'+fixture.EFFECT+':1']
    assert admin.execute('select count(*) from authz.nexloop_role_policy_bindings where run_id=%s',(run_id,)).fetchone()==(1,)
    assert admin.execute('select count(*) from authz.nexloop_role_run_bindings where run_id=%s',(run_id,)).fetchone()==(1,)
    # Activated on Context v6 with the configured strategy, accepted into the durable queue with the same budget.
    (task_id,)=admin.execute('select task_id from authz.nexloop_runtime_run_bindings where run_id=%s',(run_id,)).fetchone()
    (payload,)=admin.execute('select normalized_input from runtime.jobs where job_id::text=%s',(task_id,)).fetchone()
    command=payload['run_command']
    assert command['budget']=={'maximum_model_turns':6,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'}
    assert command['goal_version_ref'].startswith('goal:'+f['plan']['goal']+':revision:1:') and command['runtime_profile']==SETTINGS['runtime_profile']
    assert admin.execute('select count(*) from runtime.nexloop_context_packs where run_id=%s',(run_id,)).fetchone()==(1,)
    # G4: the Run's v6 Context carries its plan (current version, read-only policy item), recorded in the source manifest.
    pack=json.loads(admin.execute('select pack_text from runtime.nexloop_role_context_artifacts where run_id=%s',(run_id,)).fetchone()[0])
    (item,)=[i for i in pack['open_work'] if i['subsection']=='plan']
    assert item['ref']==f"nexloop:plan:{s['plan_id']}@1" and item['evidence_kind']=='policy' and item['revision']=='1' and item['tags']==[]
    content=item['content']
    assert content['plan_ref']==f"plan:{s['plan_id']}@1" and content['strategy']==s['strategy']['content'] and content['goal_version_ref']=='goal:renewal-q4@1'
    assert [x['step_key'] for x in content['steps']]==['confirm-renewal'] and content['steps'][0]['expected_result']==s['steps'][0]['expected_result']
    source=admin.execute('select s.revision,s.content_hash,s.evidence_kind,s.status from runtime.nexloop_context_sources s join runtime.nexloop_context_packs c '
        'on c.tenant_id=s.tenant_id and c.world=s.world and c.context_id=s.context_id where c.run_id=%s and s.ref=%s',(run_id,item['ref'])).fetchone()
    assert source==('1',item['content_hash'],'policy','included')


def test_budget_above_the_role_ceiling_issues_nothing_and_retries(role_planning):
    """The Role ceiling bounds the reevaluation Run: no credential, no link, no job; the plan stays due (retry)."""
    f=role_planning;admin=f['admin']
    before=admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]
    s=f['establish'](steps=[step(reassess_at=soon(),budget={'maximum_model_turns':6,'maximum_tool_calls':20,'active_timeout_seconds':120})])
    summary=f['worker']().run_once()
    assert summary['retry']==1 and summary['launched']==0,summary
    assert admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]==before
    assert admin.execute('select count(*) from runtime.nexloop_plan_runs where plan_id=%s',(s['plan_id'],)).fetchone()==(0,)
    assert admin.execute("select status,last_code from runtime.nexloop_work_feed where feed='plan-reevaluate'").fetchone()==('pending','reevaluation_failed')


def _without_plan(pack):
    pack['open_work']=[i for i in pack['open_work'] if i['subsection']!='plan']
    pack['budget_report']['sections']['open_work']['included']=len(pack['open_work'])


def _rewritten_plan(pack):
    import hashlib
    (item,)=[i for i in pack['open_work'] if i['subsection']=='plan']
    item['content']['steps'][0]['expected_result']='直接催促续费'
    item['content_hash']=hashlib.sha256(canonical_payload(item['content']).encode()).hexdigest()


@pytest.mark.parametrize('change,expected',[(_without_plan,'context v6 pinned source missing'),(_rewritten_plan,'context source content mismatch')])
def test_sql_refuses_a_pack_that_drops_or_rewrites_the_plan(role_planning,monkeypatch,change,expected):
    """G4: the plan item is re-derived by SQL at bind and pinned; a producer cannot drop or rewrite it."""
    f=role_planning;admin=f['admin']
    s=f['establish'](steps=[within_ceiling()])
    original=RoleContextV6ArtifactProducer.assemble;seen=[]
    def assemble(self,snapshot,command):
        body,outcome,items,proofs=original(self,snapshot,command);change(body);seen.append(self);return body,outcome,items,proofs
    monkeypatch.setattr(RoleContextV6ArtifactProducer,'assemble',assemble)
    w=f['worker']();decision=w.port.precheck(s['plan_id'],[]);budget=run_budget(SETTINGS,decision['plan_steps'])
    run=w.launcher.issue(decision,budget)
    with pytest.raises(ContextArtifactUnavailable):w.launcher.activate(run,decision,budget,[])
    assert seen[-1].last_diagnostic[1]==expected
    assert admin.execute('select count(*) from runtime.nexloop_context_packs where run_id=%s',(run.run_id,)).fetchone()==(0,)
