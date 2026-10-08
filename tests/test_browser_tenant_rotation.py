import pytest
from psycopg.types.json import Jsonb
from eios.identity.sessions import BrowserSessionService
from eios.identity.errors import CredentialInvalid,IdentityUnavailable
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
from test_browser_session_creation import uow,create
from test_browser_identity_reads import identity
from test_browser_evidence import authenticate
from test_browser_account_failures import OP

@pytest.fixture
def target(identity,admin):
    m=identity[2].model_copy(update={'tenant_id':'synthetic-b','principal_id':'synthetic-principal-b'})
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)',('synthetic-b','synthetic-browser-app'))
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)',(m.tenant_id,m.principal_id,m.subject_id,Jsonb(m.model_dump(mode='json'))))
    return m


def switch(uow,issued):
    return BrowserSessionService(uow,uow,application_id=uow.application_id,operator=OP).switch_tenant(issued.session_token.get_secret_value(),issued.csrf_token.get_secret_value(),'synthetic-b')


def test_real_switch_preserves_original_credential_and_absolute_expiry(uow,identity,target,admin):
    old=create(uow,identity,authenticate(uow,identity).evidence)
    new=switch(uow,old)
    assert new.session.tenant_id==target.tenant_id and new.session.principal_id==target.principal_id
    assert new.session.credential_tenant_id==old.session.credential_tenant_id=='synthetic-a'
    assert new.session.absolute_expires_at==old.session.absolute_expires_at
    assert uow.get_session(old.session.session_id) is None
    foreign=PostgresBrowserSessionUnitOfWork(uow.pool,tenant_id='synthetic-b',application_id=uow.application_id)
    service=BrowserSessionService(foreign,foreign,application_id=foreign.application_id,operator=OP)
    assert service.authenticate(new.session_token.get_secret_value()).credential_tenant_id=='synthetic-a'
    back=service.switch_tenant(new.session_token.get_secret_value(),new.csrf_token.get_secret_value(),'synthetic-a')
    assert uow.get_session(back.session.session_id)==back.session
    assert admin.execute('select count(*) from control.nexloop_browser_accounts').fetchone()[0]==1


def test_commit_failure_rolls_back_both_sessions(uow,identity,target,admin):
    old=create(uow,identity,authenticate(uow,identity).evidence)
    admin.execute("create function control.synthetic_rotation_failure() returns trigger language plpgsql as $$begin raise exception 'synthetic commit failure';end$$")
    admin.execute('create constraint trigger synthetic_rotation_failure after insert on control.nexloop_browser_sessions deferrable initially deferred for each row execute function control.synthetic_rotation_failure()')
    with pytest.raises(IdentityUnavailable):switch(uow,old)
    assert uow.get_session(old.session.session_id)==old.session
    assert admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0]==1


def test_target_revoke_wrong_csrf_and_original_credential_revoke_deny(uow,identity,target,admin):
    old=create(uow,identity,authenticate(uow,identity).evidence)
    service=BrowserSessionService(uow,uow,application_id=uow.application_id,operator=OP)
    with pytest.raises(CredentialInvalid):service.switch_tenant(old.session_token.get_secret_value(),'synthetic-wrong','synthetic-b')
    payload=target.model_copy(update={'status':'revoked','revision':2}).model_dump(mode='json')
    admin.execute("update control.nexloop_browser_memberships set payload=%s where tenant_id='synthetic-b'",(Jsonb(payload),))
    with pytest.raises(CredentialInvalid):switch(uow,old)
    assert uow.get_session(old.session.session_id)==old.session
    admin.execute("update control.nexloop_browser_memberships set payload=%s where tenant_id='synthetic-b'",(Jsonb(target.model_dump(mode='json')),))
    new=switch(uow,old)
    admin.execute("update control.nexloop_browser_accounts set payload=jsonb_set(payload,'{session_epoch}','2')")
    foreign=PostgresBrowserSessionUnitOfWork(uow.pool,tenant_id='synthetic-b',application_id=uow.application_id)
    assert foreign.get_session(new.session.session_id) is None
