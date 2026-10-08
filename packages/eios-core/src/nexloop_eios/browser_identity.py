"""Canonical browser identity reads through a dedicated restricted operator pool."""
from contextlib import contextmanager
import json
from datetime import datetime
from eios.identity.errors import IdentityUnavailable
from eios.identity.models import Subject,TenantMembership,LocalAccount,EncodedPasswordHash
from eios.adapters.postgres.database import create_pool
from eios.persistence.settings import StorageSettings
from nexloop_eios.bootstrap import verify


def verify_identity_role(c):
    row=c.execute('''select current_user,session_user,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,
        exists(select 1 from pg_roles elevated where elevated.rolname<>current_user
         and (elevated.rolsuper or elevated.rolbypassrls or elevated.rolcreatedb or elevated.rolcreaterole or elevated.rolreplication or elevated.rolname='nexloop_owner')
         and pg_has_role(session_user,elevated.oid,'MEMBER')) from pg_roles where rolname=current_user''').fetchone()
    if not row or row[:2]!=('nexloop_identity','nexloop_identity') or any(row[2:]):raise IdentityUnavailable('identity service is unavailable')


@contextmanager
def open_browser_identity(database_url):
    pool=create_pool(StorageSettings(database_url=database_url),open_pool=True)
    try:
        pool.wait(timeout=10)
        with pool.connection() as c,c.transaction():
            c.execute('set transaction read only');verify_identity_role(c);verify(c)
        yield pool
    finally:pool.close()


class PostgresBrowserIdentityReader:
    def __init__(self,pool,*,tenant_id,application_id):
        if any(type(value) is not str or not 0<len(value)<=320 or value!=value.strip() for value in (tenant_id,application_id)):raise ValueError('trusted realm binding required')
        self.pool=pool;self.tenant_id=tenant_id;self.application_id=application_id

    def _read(self,kind,key,model):
        try:
            if type(key) is not str or not 0<len(key)<=320:raise ValueError()
            with self.pool.connection() as c,c.transaction():
                c.execute('set transaction read only');verify_identity_role(c)
                value=c.execute('select control.nexloop_read_browser_identity(%s,%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,'nexloop_identity',kind,key)).fetchone()[0]
                if value is None:return None
                if model is LocalAccount:return decode_local_account(value)
                return model.model_validate_json(json.dumps(value))
        except Exception:raise IdentityUnavailable('identity service is unavailable') from None

    def get_subject(self,subject_id):return self._read('subject',subject_id,Subject)
    def get_membership(self,tenant_id,principal_id):
        if tenant_id!=self.tenant_id:raise IdentityUnavailable('identity service is unavailable')
        return self._read('membership',principal_id,TenantMembership)
    def get_local_account(self,tenant_id,account_id):
        if tenant_id!=self.tenant_id:raise IdentityUnavailable('identity service is unavailable')
        return self._read('account',account_id,LocalAccount)
    def find_by_username(self,tenant_id,username):
        if tenant_id!=self.tenant_id:raise IdentityUnavailable('identity service is unavailable')
        return self._read('account_by_username',username,LocalAccount)


def decode_local_account(value):
    value=dict(value)
    value['password_hash']=None if value['password_hash'] is None else EncodedPasswordHash(value['password_hash'])
    value['password_history']=tuple(EncodedPasswordHash(v) for v in value['password_history'])
    for name in ('created_at','updated_at','locked_until'):
        value[name]=None if value[name] is None else datetime.fromisoformat(value[name])
    return LocalAccount.model_validate(value)


class PostgresBrowserLocalAccountRepository(PostgresBrowserIdentityReader):
    def current_time(self):
        try:
            with self.pool.connection() as c,c.transaction():
                c.execute('set transaction read only');verify_identity_role(c)
                return c.execute('select clock_timestamp()').fetchone()[0]
        except Exception:raise IdentityUnavailable('identity service is unavailable') from None

    def record_authentication_failure(self,command,*,operator):
        from eios.identity.ports import RecordLocalAccountFailureCommand,TrustedIdentityOperator
        try:
            if type(command) is not RecordLocalAccountFailureCommand or type(operator) is not TrustedIdentityOperator:raise ValueError()
            command=RecordLocalAccountFailureCommand.model_validate(command)
            operator=TrustedIdentityOperator.model_validate(operator)
            if command.tenant_id!=self.tenant_id or operator.operator_principal_id!='nexloop_identity':raise ValueError()
            with self.pool.connection() as c,c.transaction():
                verify_identity_role(c)
                value=c.execute('select control.nexloop_record_browser_account_failure(%s,%s,%s,%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,operator.operator_principal_id,command.local_account_id,command.failed_at,operator.request_id,operator.trace_id)).fetchone()[0]
                result=decode_local_account(value)
            return result
        except Exception:raise IdentityUnavailable('identity service is unavailable') from None


    def verify_failure_result(self,before,after,failed_at,operator):
        from eios.identity.ports import TrustedIdentityOperator
        from psycopg.types.json import Jsonb
        try:
            if type(before) is not LocalAccount or type(after) is not LocalAccount or type(operator) is not TrustedIdentityOperator:raise ValueError()
            before=LocalAccount.model_validate(before);after=LocalAccount.model_validate(after)
            operator=TrustedIdentityOperator.model_validate(operator)
            if before.tenant_id!=self.tenant_id or after.tenant_id!=self.tenant_id or operator.operator_principal_id!='nexloop_identity':raise ValueError()
            for name in ('local_account_id','subject_id','username','verified_email','password_hash','password_history','status','session_epoch','must_change_password','created_at'):
                if getattr(before,name)!=getattr(after,name):return False
            if after.revision<before.revision:return False
            snapshot=after.model_dump(mode='json',exclude={'password_hash','password_history'})
            with self.pool.connection() as c,c.transaction():
                verify_identity_role(c)
                return c.execute('select control.nexloop_verify_browser_account_failure(%s,%s,%s,%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,operator.operator_principal_id,operator.request_id,operator.trace_id,failed_at,Jsonb(snapshot))).fetchone()[0] is True
        except Exception:raise IdentityUnavailable('identity service is unavailable') from None

    def save_local_account(self,command,*,operator):
        from eios.identity.ports import SaveLocalAccountCommand,TrustedIdentityOperator
        from psycopg.types.json import Jsonb
        try:
            if type(command) is not SaveLocalAccountCommand or type(operator) is not TrustedIdentityOperator:raise ValueError()
            command=SaveLocalAccountCommand.model_validate(command)
            operator=TrustedIdentityOperator.model_validate(operator)
            account=LocalAccount.model_validate(command.local_account)
            if account.tenant_id!=self.tenant_id or operator.operator_principal_id!='nexloop_identity' or command.expected_revision is None or account.password_hash is None:raise ValueError()
            body=account.model_dump(mode='json',exclude={'password_hash','password_history'})
            with self.pool.connection() as c,c.transaction():
                verify_identity_role(c)
                value=c.execute('select control.nexloop_save_browser_account_password(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,operator.operator_principal_id,command.expected_revision,Jsonb(body),account.password_hash.get_secret_value(),[v.get_secret_value() for v in account.password_history],operator.request_id,operator.trace_id)).fetchone()[0]
                result=decode_local_account(value)
                if result!=account.model_copy(update={'revision':command.expected_revision+1}):raise ValueError()
            return result
        except Exception:raise IdentityUnavailable('identity service is unavailable') from None
