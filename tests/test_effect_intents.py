"""Candidate real PG/EIOS intent admission; planner controls are synthetic seeds.

No provider dispatch or production business/planner completeness is claimed.
The Consumer business object is created only by the actual governed Action port.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC,datetime,timedelta
import copy
import json
from pathlib import Path
import secrets
import uuid

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_intents import EffectIntentConflict,EffectIntentUnavailable,ACTION
from authority_fixture import seed_authority
from test_postgres_action_claims import governance_inputs


from test_effect_contexts import plan,configure,bind


@pytest.fixture
def intents(plan):
    # Shared fixture creates formal Consumer/Goal/Step/Control via real governed
    # Actions and registers authority through the actual Backend, no context seed.
    configure(plan);registration=bind(plan)
    second=plan['second_submitter'].issue_run_credential(action_resources=[f'eios:action:{ACTION}:1'])
    other=plan['backend'].authenticate_run(second.token,world='real',run_id=second.run_id)
    assert bind(plan,run=second)==registration
    yield [plan['run'],other],[plan['issued'],second],registration['context_ref'],plan['backend'],plan['submitter']


def test_two_actual_principals_share_one_stable_intent_and_outbox(intents,admin):
    ports,_,context,_,_=intents
    with ThreadPoolExecutor(2) as executor:receipts=list(executor.map(lambda port:port.submit_effect_intent(parameters={'message':'one service'}),ports))
    assert receipts[0]==receipts[1] and receipts[0]['state']=='accepted'
    assert all(port.find_effect_receipt(intent_id=receipts[0]['intent_id'])==receipts[0] for port in ports)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.nexloop_effect_submissions').fetchone()[0]==2
    assert admin.execute('select reserved_units from control.nexloop_effect_contexts where context_id=%s',(context,)).fetchone()[0]==1
    # The existing effect Action claim was not reserved under either submitter.
    assert admin.execute('select count(*) from runtime.nexloop_action_claims where action_name=%s',(ACTION,)).fetchone()[0]==0


def test_payload_conflict_is_409_and_preserves_original(intents,admin):
    ports,_,_,_,_=intents;first=ports[0].submit_effect_intent(parameters={'message':'original'})
    with pytest.raises(EffectIntentConflict) as error:ports[1].submit_effect_intent(parameters={'message':'different'})
    assert error.value.http_status==409 and error.value.code=='intent_payload_conflict'
    assert ports[1].find_effect_receipt(intent_id=first['intent_id'])==first
    assert admin.execute('select frozen_request->\'parameters\' from runtime.nexloop_effect_intents').fetchone()[0]=={'message':'original'}
    assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()[0]==1


@pytest.mark.parametrize('case',['missing_context','denied','expired','budget','consumer_revision','schema'])
def test_missing_authority_controls_or_schema_fail_closed(intents,admin,case):
    ports,runs,context,_,_=intents;parameters={'message':'service'}
    if case=='missing_context':admin.execute('delete from control.nexloop_effect_run_contexts where run_id=%s',(runs[0].run_id,))
    elif case=='denied':admin.execute('update control.nexloop_effect_contexts set allow_effect=false where context_id=%s',(context,))
    elif case=='expired':admin.execute('update control.nexloop_effect_contexts set valid_until=clock_timestamp()-interval \'1 second\' where context_id=%s',(context,))
    elif case=='budget':admin.execute('update control.nexloop_effect_contexts set budget_units=0 where context_id=%s',(context,))
    elif case=='consumer_revision':admin.execute('update control.nexloop_effect_contexts set consumer_revision=2 where context_id=%s',(context,))
    else:parameters={'message':7}
    with pytest.raises(EffectIntentUnavailable):ports[0].submit_effect_intent(parameters=parameters)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==0
    assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()[0]==0


def test_outbox_failure_rolls_back_intent_budget_and_submission(intents,admin):
    ports,_,context,_,_=intents
    admin.execute("create function public.synthetic_effect_outbox_fail() returns trigger language plpgsql as $$ begin raise exception 'synthetic failure'; end $$")
    admin.execute('create trigger synthetic_effect_failure before insert on runtime.nexloop_effect_outbox for each row execute function public.synthetic_effect_outbox_fail()')
    with pytest.raises(EffectIntentUnavailable):ports[0].submit_effect_intent(parameters={'message':'service'})
    for table in ('nexloop_effect_intents','nexloop_effect_outbox','nexloop_effect_submissions'):
        assert admin.execute(f'select count(*) from runtime.{table}').fetchone()[0]==0
    assert admin.execute('select reserved_units from control.nexloop_effect_contexts where context_id=%s',(context,)).fetchone()[0]==0


def test_application_cannot_directly_read_or_write_authority(intents):
    _,_,_,backend,_=intents
    for table in ('control.nexloop_effect_contexts','control.nexloop_effect_run_contexts','runtime.nexloop_effect_intents','runtime.nexloop_effect_outbox'):
        with backend._pool.connection() as connection,connection.transaction():
            with pytest.raises(psycopg.errors.InsufficientPrivilege):connection.execute('select * from '+table)


def test_signed_server_cannot_bypass_sql_parameter_schema(intents,admin,monkeypatch):
    # Simulate a trusted Python validation bug; actual server signs a real EIOS
    # proof. The SQL boundary independently rejects malformed effect parameters.
    from jsonschema import Draft202012Validator
    ports,_,_,_,_=intents
    monkeypatch.setattr(Draft202012Validator,'validate',lambda self,*args,**kwargs:None)
    with pytest.raises(EffectIntentUnavailable):ports[0].submit_effect_intent(parameters={'message':7})
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==0
    assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()[0]==0


def test_actual_goal_version_change_fails_closed(intents,plan,admin):
    ports,_,_,_,_=intents
    plan['goal_editor'].edit_object(action_name='Goal.edit',action_version=1,intent_id='actual-intent-goal-close',
        type_name='Goal',object_id=plan['goal'],expected_revision=1,properties={'state':'closed'})
    assert admin.execute('select nexloop_revision from ontology.objects where object_id=%s',(plan['goal'],)).fetchone()[0]==2
    with pytest.raises(EffectIntentUnavailable):ports[0].submit_effect_intent(parameters={'message':'service'})
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==0


def test_current_run_revocation_blocks_shared_receipt_access(intents,admin):
    ports,runs,_,_,_=intents;receipt=ports[0].submit_effect_intent(parameters={'message':'service'})
    admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s",(runs[1].run_id,))
    with pytest.raises(EffectIntentUnavailable):ports[1].find_effect_receipt(intent_id=receipt['intent_id'])
    assert ports[0].find_effect_receipt(intent_id=receipt['intent_id'])==receipt


def test_non_run_service_cannot_submit_or_find_effect_intent(intents,admin):
    ports,_,_,_,root=intents
    receipt=ports[0].submit_effect_intent(parameters={'message':'service'})
    with pytest.raises(EffectIntentUnavailable):root.submit_effect_intent(parameters={'message':'service'})
    with pytest.raises(EffectIntentUnavailable):root.find_effect_receipt(intent_id=receipt['intent_id'])
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==1
