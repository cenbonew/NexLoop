from datetime import UTC,datetime
import secrets
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from argon2 import PasswordHasher
from eios.identity.models import Subject,SubjectKind,TenantMembership,MembershipKind,LocalAccount,EncodedPasswordHash
from eios.identity.errors import IdentityUnavailable
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.browser_identity import open_browser_identity,PostgresBrowserIdentityReader

@pytest.fixture
def identity(admin,pg):
    bootstrap(admin);now=datetime.now(UTC);password=secrets.token_urlsafe(32);hasher=PasswordHasher()
    subject=Subject(subject_id='synthetic-human-a',kind=SubjectKind.HUMAN,status='active',created_at=now,updated_at=now,revision=1)
    membership=TenantMembership(tenant_id='synthetic-a',principal_id='synthetic-principal-a',subject_id=subject.subject_id,kind=MembershipKind.HOME,status='active',valid_from=now,valid_until=None,revision=1)
    account=LocalAccount(local_account_id='synthetic-account-a',tenant_id='synthetic-a',subject_id=subject.subject_id,username='synthetic-user',verified_email='synthetic@example.invalid',password_hash=EncodedPasswordHash(hasher.hash(password)),status='active',failed_attempts=0,lockout_level=0,locked_until=None,must_change_password=False,session_epoch=1,created_at=now,updated_at=now,revision=1)
    # Canonical authentication setup only, not a business object write fixture.
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)',('synthetic-a','synthetic-browser-app'))
    admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)',(subject.subject_id,Jsonb(subject.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)',(membership.tenant_id,membership.principal_id,membership.subject_id,Jsonb(membership.model_dump(mode='json'))))
    payload=account.model_dump(mode='json');payload.pop('password_hash');payload.pop('password_history')
    admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',(account.tenant_id,account.local_account_id,account.subject_id,account.username,account.password_hash.get_secret_value(),[],Jsonb(payload)))
    return make_conninfo(pg,user='nexloop_identity'),subject,membership,account,password


def test_typed_canonical_reads_and_password_hash_roundtrip(identity):
    dsn,subject,membership,account,password=identity
    with open_browser_identity(dsn) as pool:
        reader=PostgresBrowserIdentityReader(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app')
        assert reader.get_subject(subject.subject_id)==subject
        assert reader.get_membership('synthetic-a',membership.principal_id)==membership
        stored=reader.find_by_username('synthetic-a',account.username)
        assert stored==account and PasswordHasher().verify(stored.password_hash.get_secret_value(),password)
        assert reader.get_local_account('synthetic-a',account.local_account_id)==account
        assert reader.find_by_username('synthetic-a','missing') is None


def test_reopen_and_current_subject_revoke(identity,admin):
    dsn,subject,membership,account,password=identity
    with open_browser_identity(dsn) as pool:
        reader=PostgresBrowserIdentityReader(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app')
        assert reader.get_subject(subject.subject_id).status=='active'
        changed=subject.model_copy(update={'status':'disabled','revision':2})
        admin.execute('update control.nexloop_browser_subjects set payload=%s where subject_id=%s',(Jsonb(changed.model_dump(mode='json')),subject.subject_id))
        assert reader.get_subject(subject.subject_id).status=='disabled'
    with open_browser_identity(dsn) as pool:
        assert PostgresBrowserIdentityReader(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app').get_subject(subject.subject_id).revision==2


def test_identity_role_has_no_direct_auth_or_business_access(identity):
    dsn,*_=identity
    with psycopg.connect(dsn) as c:
        for statement in ['select * from control.nexloop_browser_accounts','select * from control.nexloop_browser_subjects','select * from ontology.objects',"insert into control.nexloop_browser_applications values('x','y',1,true)"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():c.execute(statement)


def test_realm_operator_and_tenant_fail_closed(identity,admin,pg):
    dsn,subject,membership,account,password=identity
    with open_browser_identity(dsn) as pool:
        reader=PostgresBrowserIdentityReader(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app')
        with pytest.raises(IdentityUnavailable):reader.get_membership('synthetic-b',membership.principal_id)
        with pytest.raises(IdentityUnavailable):PostgresBrowserIdentityReader(pool,tenant_id='synthetic-a',application_id='missing-app').get_subject(subject.subject_id)
        with pool.connection() as c,pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():
            c.execute('select control.nexloop_read_browser_identity(%s,%s,%s,%s,%s)',('synthetic-a','synthetic-browser-app','fake-operator','subject',subject.subject_id))
        admin.execute('update control.nexloop_browser_applications set active=false')
        with pytest.raises(IdentityUnavailable):reader.find_by_username('synthetic-a',account.username)
    for user in ['nexloop_api','nexloop_bootstrap']:
        with pytest.raises(IdentityUnavailable),open_browser_identity(make_conninfo(pg,user=user)):pass


def test_global_subject_cannot_cross_unrelated_tenant(identity,admin):
    dsn,subject,membership,account,password=identity
    foreign=subject.model_copy(update={'subject_id':'synthetic-human-b'})
    foreign_member=membership.model_copy(update={'tenant_id':'synthetic-b','principal_id':'synthetic-principal-b','subject_id':foreign.subject_id})
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)',('synthetic-b','synthetic-browser-app'))
    admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)',(foreign.subject_id,Jsonb(foreign.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)',(foreign_member.tenant_id,foreign_member.principal_id,foreign_member.subject_id,Jsonb(foreign_member.model_dump(mode='json'))))
    with open_browser_identity(dsn) as pool:
        a=PostgresBrowserIdentityReader(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app')
        b=PostgresBrowserIdentityReader(pool,tenant_id='synthetic-b',application_id='synthetic-browser-app')
        assert a.get_subject(foreign.subject_id) is None and b.get_subject(foreign.subject_id)==foreign
        assert b.get_subject(subject.subject_id) is None


def test_elevated_identity_membership_refused(identity,admin):
    dsn,*_=identity
    admin.execute('grant nexloop_owner to nexloop_identity')
    with pytest.raises(IdentityUnavailable),open_browser_identity(dsn):pass


def test_malformed_canonical_model_fails_closed(identity,admin):
    dsn,subject,membership,account,password=identity
    payload=subject.model_dump(mode='json');payload.pop('updated_at')
    admin.execute('update control.nexloop_browser_subjects set payload=%s where subject_id=%s',(Jsonb(payload),subject.subject_id))
    with open_browser_identity(dsn) as pool:
        with pytest.raises(IdentityUnavailable):PostgresBrowserIdentityReader(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app').get_subject(subject.subject_id)
