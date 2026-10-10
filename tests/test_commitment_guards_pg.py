"""NX-026 negative cases on clean catalog PostgreSQL (design §10 items 14–20).

The object guard (prepared creation only, ledger-justified transitions only, content immutable, never deleted), the
commitment reference of an effect intent, the ports' callers, what is not registered, corrections and deleted source
Messages (D7), and a tenant without the Commitment type (schema gap, then recovery). Synthetic data; admin seeds
fixtures (commitment_fixture) and probes; where admin acts as a role it says so.
"""
from datetime import UTC,datetime,timedelta
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.commitments import CommitmentPort,CommitmentReadPort
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.effect_intents import EffectIntentUnavailable
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.object_edits import GovernedObjectEditor
from nexloop_eios.plan_reevaluation import PlanUnavailable
from commitment_fixture import TENANT,commitments,deadline  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_commitments_pg import QUOTE,REPLY,fresh,registered

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)
DENIED=(Exception,)


def count(admin,sql,*args):return admin.execute(sql,args).fetchone()[0]


def test_object_guard_refuses_unjustified_edits_content_changes_deletes_and_unprepared_creates(commitments,admin):
    c=commitments;commitment,_=fresh(c);before=c.commitment(commitment)
    session=c.keeper_session()
    editor=GovernedObjectEditor(c['worker'],session,c['signer'])
    # The keeper's own governed edit, with a transition the ledger does not justify: refused by the guard.
    with pytest.raises(psycopg.errors.CheckViolation,match='not justified by its evidence'):
        editor.edit(action_name='Commitment.edit',action_version=1,intent_id='forged-fulfil',type_name='Commitment',object_id=commitment,
            expected_revision=before['revision'],properties={'status':'fulfilled','fulfillment_evidence':[{'kind':'operator_attestation','ref':'x'}]})
    # A content property is outside the derived EDIT (commitment_lifecycle only).
    with pytest.raises((ActionAuthorizationDenied,AuthorizationUnavailable,F.AuthorizationFactDenied)):
        editor.edit(action_name='Commitment.edit',action_version=1,intent_id='forged-due',type_name='Commitment',object_id=commitment,
            expected_revision=before['revision'],properties={'due_at':'2099-01-01T00:00:00.000000Z'})
    # Even the table owner cannot move the status, change content or delete it.
    for statement,refusal in (("update ontology.objects set properties=properties||'{\"status\":\"cancelled\"}' where object_id=%s",'not justified'),
                      ("update ontology.objects set properties=properties||'{\"made_to\":\"ffff\"}' where object_id=%s",'not justified'),
                      ("delete from ontology.objects where object_id=%s",'never deleted')):
        with admin.transaction():
            admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
            with pytest.raises(psycopg.Error,match=refusal),admin.transaction():admin.execute(statement,(commitment,))
    assert c.commitment(commitment)==before
    # A governed create with properties nobody prepared from evidence: refused.
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='not prepared'):
        GovernedObjectCreator(c['worker'],session,c['signer']).create(action_name='Commitment.create',action_version=1,intent_id='forged-commitment',
            type_name='Commitment',properties=before['properties'])
    assert count(admin,"select count(*) from ontology.objects where type_name='Commitment'")==1


def test_commitment_reference_must_name_a_live_commitment_of_the_same_consumer(commitments,admin):
    c=commitments;c.configure_categories()
    other='9'*64;now=datetime.now(UTC).replace(microsecond=0)
    message=c.outbound(REPLY,accepted_at=now,consumer=other)
    foreign,_=registered(c,c.claim(message,QUOTE,consumer=other,valid_time=deadline(message,now+timedelta(days=1))))
    conditional,_=fresh(c,reply='您确认后我们补发。',quote='您确认后我们补发',modality='conditional',condition='您确认后',predicate='补发')
    for ref in ('commitment:'+foreign,'commitment:'+'0'*64,'commitment:'+conditional,'Commitment/'+conditional,'commitment:'+conditional.upper()):
        with pytest.raises(EffectIntentUnavailable):c.submit({'service':'退款','commitment_ref':ref})
    assert count(admin,'select count(*) from runtime.nexloop_commitment_evidence')==0
    assert count(admin,"select count(*) from runtime.nexloop_effect_intents where frozen_request->'parameters' ? 'commitment_ref'")==0


def test_fallback_reply_run_never_carries_a_commitment_reference(commitments,admin):
    """ADR-023 §2.7: the fallback reply Run only answers; SQL refuses its submission of a commitment-bound intent."""
    c=commitments;c.configure_categories();commitment,_=fresh(c)
    receipt=c.submit({'service':'退款','commitment_ref':'commitment:'+commitment})
    run=c.issue_run()
    digest=admin.execute('select token_digest from authz.nexloop_run_credentials where run_id=%s',(run.run_id,)).fetchone()[0]
    with admin.transaction():
        # Fault injection: that second Run is registered as a fallback reply Run, then it submits the same intent.
        admin.execute('''insert into authz.nexloop_message_fallback_issuances(tenant_id,world,message_id,run_id,request_id,issuance_nonce,assignment_id,assignment_revision,
            assignment_digest,issuer_principal,run_digest,issued_at,expires_at) values(%s,'real',%s,%s,'synthetic',%s,'synthetic',1,%s,'synthetic',%s,clock_timestamp(),clock_timestamp()+interval '60 seconds')''',
            (TENANT,'7'*64,run.run_id,'a'*64,'b'*64,digest))
        with pytest.raises(psycopg.errors.InsufficientPrivilege,match='fallback reply Run cannot act on a commitment'),admin.transaction():
            admin.execute("insert into runtime.nexloop_effect_submissions values(%s,%s,'synthetic-principal',%s,clock_timestamp())",(receipt['intent_id'],run.run_id,digest))


def test_ports_refuse_other_callers(commitments,admin):
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios.authorization import authenticate_service
    c=commitments;commitment,_=fresh(c)
    # A service without the keep / read Actions.
    _,token=seed_multi_authority(admin,c['worker'],[('eios:action:NexLoop.feed.commitment-monitor:1',__import__('eios.authz.resources',fromlist=['x']).ResourceType.ACTION,
        __import__('eios.authz.operations',fromlist=['x']).Operation.EXECUTE)],identity_suffix='-no-commitment-port',tenant=TENANT)
    other=authenticate_service(c['worker'],token,world='real')
    with pytest.raises(PlanUnavailable):CommitmentPort(c['worker'],other,c['signer']).evaluate(commitment)
    with pytest.raises(PlanUnavailable):CommitmentReadPort(c['worker'],other,c['signer']).commitment(commitment)
    # A Run credential is never a keeper.
    run=c.issue_run()
    run_session=c['plan']['backend'].authenticate_run(run.token,world='real',run_id=run.run_id)._session
    with pytest.raises(PlanUnavailable):CommitmentPort(c['worker'],run_session,c['signer'])
    # Another tenant's keeper with the same grants sees nothing of this tenant.
    other_tenant=str(uuid.uuid4());admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')",(other_tenant,))
    from commitment_fixture import KEEPER_TARGETS
    _,foreign=seed_multi_authority(admin,c['worker'],KEEPER_TARGETS,identity_suffix='-foreign-keeper',tenant=other_tenant)
    foreign_session=authenticate_service(c['worker'],foreign,world='real')
    with pytest.raises(Exception):CommitmentPort(c['worker'],foreign_session,c['signer']).evaluate(commitment)
    assert CommitmentReadPort(c['worker'],foreign_session,c['signer']).commitment(commitment) is None
    assert CommitmentReadPort(c['worker'],foreign_session,c['signer']).commitments()==[]
    # Application roles have no table access.
    with psycopg.connect(__import__('psycopg.conninfo',fromlist=['x']).make_conninfo(c['pg'],user='nexloop_domain_worker')) as db:
        for table in ('runtime.nexloop_commitments','runtime.nexloop_commitment_evidence','runtime.nexloop_commitment_events','runtime.nexloop_commitment_exceptions'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select 1 from '+table)
            db.rollback()


def test_not_registered_consumer_words_tentative_negated_and_undelivered(commitments,admin):
    c=commitments;now=datetime.now(UTC).replace(microsecond=0)
    message=c.outbound(REPLY,accepted_at=now)
    claims=[c.claim(message,QUOTE,modality='tentative',predicate='a'),c.claim(message,QUOTE,polarity='negated',predicate='b'),
        c.claim(message,QUOTE,speaker='consumer',kind='intent',predicate='c')]
    # An Agent message still undelivered (outbound record persisted, no stream entry): not a promise made to the customer.
    undelivered=dict(message,message_id='8'*64)
    claims.append(c.claim(undelivered,QUOTE,predicate='d'))
    c.tick()
    assert count(admin,'select count(*) from runtime.nexloop_commitments')==0
    skipped={r[0]:r[1] for r in admin.execute("select subject_ref,detail->>'reason' from runtime.nexloop_commitment_events where kind='skipped'").fetchall()}
    assert skipped=={'claim:'+claims[0]:'not_a_commitment','claim:'+claims[1]:'not_a_commitment','claim:'+claims[3]:'source_not_delivered_outbound'}
    assert admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s',(claims[0],)).fetchone()==('unresolved',)


def test_correction_and_deleted_source_keep_the_commitment_and_hide_the_words(commitments,admin):
    """D7: a deleted source Message leaves status and audit; the words are no longer shown. A correction never cancels."""
    c=commitments;commitment,message=fresh(c)
    claim=admin.execute('select claim_id from runtime.nexloop_commitments where commitment_id=%s',(commitment,)).fetchone()[0]
    admin.execute("update ontology.nexloop_claims set resolution_state='superseded' where claim_id=%s",(claim,))
    admin.execute("delete from ontology.objects where type_name='Message' and object_id=%s",(message['message_id'],))
    c.tick()
    view=c.reader().commitment(commitment)
    assert view['quote'] is None and view['source_available'] is False and view['properties']['status']=='open'
    assert sorted(x['reason'] for x in view['exceptions'])==['source_deleted','source_superseded']
    assert [e['kind'] for e in view['events']]==['registered']


def test_schema_gap_keeps_the_claim_and_recovers_once_published(commitments,admin):
    c=commitments;now=datetime.now(UTC).replace(microsecond=0)
    admin.execute("update control.nexloop_action_definitions set active=false where resource_id='eios:action:Commitment.create:1'")
    message=c.outbound(REPLY,accepted_at=now);claim=c.claim(message,QUOTE,valid_time=deadline(message,now+timedelta(days=1)))
    summary=c.keeper().run_once();assert summary['retry']==1
    assert count(admin,"select count(*) from ontology.objects where type_name='Commitment'")==0
    assert admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s',(claim,)).fetchone()==('unresolved',)
    assert [x['reason'] for x in c.reader().exceptions()]==['schema_gap']
    admin.execute("update control.nexloop_action_definitions set active=true where resource_id='eios:action:Commitment.create:1'")
    admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp() where feed='commitment-register'")  # retry now (time injection)
    c.tick()
    assert c.by_claim(claim) and admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s',(claim,)).fetchone()==('resolved',)
