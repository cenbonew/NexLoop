"""follow_latest_version end to end: a real human review publication, then trusted-configuration apply."""
from datetime import UTC,datetime,timedelta
import json
import secrets

import pytest
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import service_grants as G
from nexloop_eios.authorization import authenticate_service,authorization_service
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.review_actions import uncovered_actions
from test_browser_session_reads import authenticate,create
from test_review_decisions_pg import MANIFEST_PATH,human,pending,review  # noqa: F401
from test_claim_matching_pg import env  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_review_http import grant_human

TENANT='synthetic-a'
pytestmark=pytest.mark.parametrize('env',[TENANT],indirect=True)


def allowed(pool,session,resource):
    try:return authorization_service(pool,session).decide(session.query(resource_id=resource,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)).allowed
    except (AuthorizationUnavailable,F.AuthorizationFactDenied):return False


def test_declared_service_grant_follows_reviewed_successor_idempotently(review):
    f=review;admin=f['admin']
    _,cid,_,_=pending(f,'fl1','常用付款方式','花呗','我一般用花呗付款','payment_method')
    revision=admin.execute('select revision from ontology.nexloop_candidate_definitions where candidate_id=%s',(cid,)).fetchone()[0]
    published=human(f).decide(candidate_id=cid,decision='approve',expected_revision=revision,rationale='新增付款方式属性',idempotency_key='synthetic-follow-approve-01')
    assert published['outcome']=='published' and 'eios:action:Consumer.edit:2' in published['publication']['published_refs']
    admin.execute('alter role nexloop_configurator login')
    manifest=G.load(MANIFEST_PATH)
    def private(name,value):
        path=f['tmp']/name;path.write_text(value);path.chmod(0o600);return path
    dsn=private('configurator-dsn',make_conninfo(f['pg'],user='nexloop_configurator'))
    tokens={p['credential_reference']:secrets.token_urlsafe(48) for p in manifest['principals']}
    secrets_file=private('service-secrets',json.dumps(tokens));signing=private('signing-key',f['signer'].material.hex())
    def apply():
        return G.apply(manifest,TENANT,database_url_file=dsn,signing_key_file=signing,signing_key_id=f['signer'].key_id,
            service_secrets_file=secrets_file,credential_expires_at=datetime.now(UTC)+timedelta(hours=2))
    # Read-only checks before apply: the successor is announced as auto-covered, not as a gap.
    report=uncovered_actions(manifest,TENANT,database_url_file=dsn)
    assert report['covered'] is True and [i['resource_id'] for i in report['will_be_auto_covered']]==['eios:action:Consumer.edit:2']
    doctor=G.doctor(manifest,TENANT,database_url_file=dsn)
    assert doctor['in_sync'] is False and [g['resource_id'] for g in doctor['follow_latest']['derived']]==['eios:action:Consumer.edit:2']
    applied=apply()
    assert applied['changed'] and {(g['resource_id'],g['operation']) for g in applied['grants_added'] if g['resource_id'].startswith('eios:action:Consumer.edit')}=={
        ('eios:action:Consumer.edit:1','execute'),('eios:action:Consumer.edit:2','execute')}
    assert applied['follow_latest']['refused']==[]
    assert admin.execute("select count(*) from control.nexloop_configuration_publications where operator_role='nexloop_configurator'").fetchone()[0]>=1
    principal=next(p for p in manifest['principals'] if p['role']=='claim_matcher')
    service=authenticate_service(f['worker'],tokens[principal['credential_reference']],world='real')
    assert allowed(f['worker'],service,'eios:action:Consumer.edit:2') and allowed(f['worker'],service,'eios:action:Consumer.edit:1')
    # Other services are not extended (only the declared principal/Action).
    relay=next(p for p in manifest['principals'] if p['role']=='message_relay')
    pool=f['api'] if relay['database_role']=='nexloop_api' else f['worker']
    assert not allowed(pool,authenticate_service(pool,tokens[relay['credential_reference']],world='real'),'eios:action:Consumer.edit:2')
    # Idempotent: a second apply writes nothing; doctor in sync.
    again=apply()
    assert again['changed'] is False and again['facts_written']==[],again['facts_written']
    after=G.doctor(manifest,TENANT,database_url_file=dsn)
    # Fixture principals outside the manifest stay reported as extra grants; nothing of the manifest is missing or drifting.
    assert after['missing_grants']==[] and after['fact_drift']==[] and after['follow_latest']['refused']==[]
    assert all(not g['manifest_principal'] for g in after['extra_grants'])
    # A Human holding Consumer.edit:1 does not follow to :2 (humans are never in the manifest).
    grant_human(admin,f['uow'],f['identity'],'eios:action:Consumer.edit:1')
    issued=create(f['uow'],f['identity'],authenticate(f['uow'],f['identity']).evidence)
    person=authenticate_browser_business(f['api'],issued.session,world='real')
    assert allowed(f['api'],person,'eios:action:Consumer.edit:1') and not allowed(f['api'],person,'eios:action:Consumer.edit:2')
    assert apply()['changed'] is False


def test_human_principal_in_manifest_is_still_rejected(review):
    f=review;admin=f['admin']
    admin.execute('alter role nexloop_configurator login')
    dsn=f['tmp']/'configurator-dsn';dsn.write_text(make_conninfo(f['pg'],user='nexloop_configurator'));dsn.chmod(0o600)
    manifest=G.load(MANIFEST_PATH)
    principal=next(p for p in manifest['principals'] if p['role']=='claim_matcher')
    principal['principal_id']=f['identity'][2].principal_id  # the browser Human's principal
    with pytest.raises(G.ServiceGrantsRejected,match='human_principal'):G.doctor(manifest,TENANT,database_url_file=dsn)
