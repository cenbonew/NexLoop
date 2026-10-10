"""NX-029 slice 1: prompt text of recorded model requests expires (30 days, never longer than the message text).

A real v6 message Run records its model request and prompt through the guard (NX-023); the retention keeper of that
tenant redacts the prompt text once it is older than its class. The request manifest (digests, usage) stays.
Time passes by explicit injection (admin back-dates the request with triggers suspended). Synthetic data only.
"""
from datetime import UTC,datetime,timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import retention
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from multi_authority_fixture import seed_multi_authority
from test_context_v6_pg import v6,owner,request_snapshot
from test_context_artifacts import context_message,source_declarations,active_worker
from local_message_assembly_fixture import assembled_message,business_plan,configured


def test_prompt_text_is_redacted_after_its_class_and_the_manifest_stays(v6,admin,tmp_path):
    f=v6;o=f['original'];tenant=o['tenant']
    assert f['v6_relay']().run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model',request_snapshot=request_snapshot(1,text))
    # The keeper is provisioned afterwards (seeding moves the authority directory the relay and worker authenticated under).
    with open_core(make_conninfo(o['pg'],user='nexloop_domain_worker')) as pool:
        _,token=seed_multi_authority(admin,pool,[('eios:action:nexloop.retention.execute:1',ResourceType.ACTION,Operation.EXECUTE)],
            identity_suffix='-retention-keeper',tenant=tenant)
        prompt=lambda:admin.execute('select p.request_text,r.request_digest from runtime.nexloop_model_request_prompts p join runtime.nexloop_model_requests r using(tenant_id,world,run_id,call_sequence)').fetchone()
        before,digest=prompt();assert before and len(digest)==64
        port=lambda:retention.RetentionPort(pool,authenticate_service(pool,token,world='real'),f['backend']._signer)
        # Not yet expired: nothing happens.
        assert port().sweep('prompt_text','sweep:22222222-2222-2222-2222-222222222222')['processed']==0
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute('update runtime.nexloop_model_requests set requested_at=%s',(datetime.now(UTC)-timedelta(days=31),))
        assert port().sweep('prompt_text','sweep:33333333-3333-3333-3333-333333333333')['counts']=={'prompts':1}
        after,same=prompt();assert after=='' and same==digest
        # Still append-only outside a sweep, for the owner too.
        with admin.transaction():
            owner(admin,tenant)
            with pytest.raises(psycopg.errors.InvalidParameterValue,match='append-only'),admin.transaction():
                admin.execute("update runtime.nexloop_model_request_prompts set request_text=''")
        assert admin.execute("select action,counts from runtime.nexloop_erasure_tombstones where item_class='prompt_text'").fetchall()==[('redacted',{'prompts':1})]
