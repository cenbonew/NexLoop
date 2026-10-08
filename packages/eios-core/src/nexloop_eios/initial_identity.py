"""Explicit NEW initial identity operator port; no business grants or fake Human.

Creates only deliberately supplied canonical Human/tenant membership/account.
Requires real restricted nexloop_identity role and configurator-published bounded
allowance. Actual local login verifies Argon2 later; this never issues a session.
"""
import argparse,hashlib,hmac,re,secrets,sys,uuid
from pathlib import Path
from argon2 import PasswordHasher
from eios.identity.models import LocalAccount,EncodedPasswordHash,SubjectKind
from eios.identity.ports import CreateSubjectCommand,CreateMembershipCommand,SaveLocalAccountCommand,TrustedIdentityOperator
from nexloop_eios.browser_identity import open_browser_identity,verify_identity_role,decode_local_account
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.trusted_configuration import strict_json,ConfigurationRejected


def validate_identity_manifest(value):
    try:
        if type(value) is not dict or set(value)!={'schema_version','tenant_id','application_id','request_id','operator_label','subject','membership','account'} or value['schema_version']!='1.0':raise ValueError()
        for name in ('tenant_id','application_id','operator_label'):
            if type(value[name]) is not str or not 0<len(value[name])<=320 or value[name]!=value[name].strip():raise ValueError()
        request=str(uuid.UUID(value['request_id']))
        subject=CreateSubjectCommand.model_validate_json(canonical_payload({'subject':value['subject']})).subject
        membership=CreateMembershipCommand.model_validate_json(canonical_payload({'membership':value['membership']})).membership
        if subject.kind is not SubjectKind.HUMAN or subject.status!='active' or membership.subject_id!=subject.subject_id or membership.tenant_id!=value['tenant_id'] or membership.status!='active' or dict(membership.trusted_attributes):raise ValueError()
        body=value['account']
        if type(body) is not dict or 'password_hash' in body or 'password_history' in body:raise ValueError()
        # Independent random ephemeral hash allows original active-account model
        # checks during offline validation. Never persisted/output, no password.
        account=decode_local_account({**body,'password_hash':PasswordHasher().hash(secrets.token_urlsafe(32)),'password_history':[]})
        if account.tenant_id!=value['tenant_id'] or account.subject_id!=subject.subject_id or account.status!='active' or account.revision!=1 or account.session_epoch!=1 or account.failed_attempts!=0 or account.lockout_level!=0 or account.locked_until is not None:raise ValueError()
        return {**value,'request_id':request,'subject':subject.model_dump(mode='json'),'membership':membership.model_dump(mode='json'),'account':account.model_dump(mode='json',exclude={'password_hash','password_history'})}
    except Exception:raise ConfigurationRejected() from None


def create_initial_identity(value,*,database_url_file,password_file,idempotency_key_file):
    try:
        value=validate_identity_manifest(value)
        password=read_private_text(password_file,maximum=4096)
        if len(password)<12:raise ValueError()
        seal_key=bytes.fromhex(read_private_text(idempotency_key_file,maximum=128))
        if len(seal_key)!=32:raise ValueError()
        password_fingerprint=hmac.new(seal_key,canonical_payload({'domain':'nexloop-initial-password-v1','tenant_id':value['tenant_id'],'application_id':value['application_id'],'request_id':value['request_id'],'password':password}).encode(),'sha256').hexdigest()
        account=decode_local_account({**value['account'],'password_hash':PasswordHasher().hash(password),'password_history':[]})
        command=SaveLocalAccountCommand(local_account=account,expected_revision=None)
        # Operator role is trusted composition context, never supplied by manifest.
        operator=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id=value['request_id'],trace_id=value['request_id'])
        with open_browser_identity(read_private_text(database_url_file,maximum=16384)) as pool,pool.connection() as db,db.transaction():
            verify_identity_role(db)
            result=db.execute('select control.nexloop_initial_identity_create(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s)',
                (value['tenant_id'],value['application_id'],operator.operator_principal_id,value['request_id'],value['operator_label'],
                 canonical_payload(value['subject']),canonical_payload(value['membership']),canonical_payload(value['account']),
                 command.local_account.password_hash.get_secret_value(),password_fingerprint,seal_key)).fetchone()[0]
        expected={'created','subject_id','principal_id','account_id'}
        if type(result) is not dict or set(result)!=expected or type(result['created']) is not bool or result['subject_id']!=value['subject']['subject_id'] or result['principal_id']!=value['membership']['principal_id'] or result['account_id']!=value['account']['local_account_id']:raise ValueError()
        return result
    except Exception:raise ConfigurationRejected() from None


def main():
    parser=argparse.ArgumentParser(description='Explicit initial identity operator; no session or business Grant')
    parser.add_argument('--manifest',type=Path,required=True);mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument('--check',action='store_true');mode.add_argument('--create',action='store_true')
    parser.add_argument('--identity-database-url-file',type=Path);parser.add_argument('--password-file',type=Path);parser.add_argument('--idempotency-key-file',type=Path)
    args=parser.parse_args()
    try:
        with args.manifest.open() as stream:raw=stream.read(1048577)
        if len(raw.encode())>1048576:raise ValueError()
        value=validate_identity_manifest(strict_json(raw))
        if args.create:
            if any(v is None for v in (args.identity_database_url_file,args.password_file,args.idempotency_key_file)):raise ValueError()
            result=create_initial_identity(value,database_url_file=args.identity_database_url_file,password_file=args.password_file,idempotency_key_file=args.idempotency_key_file)
            # IDs stay private; no raw credentials are generated or printed.
            print(canonical_payload({'identity_created':result['created']}))
        else:print(canonical_payload({'checked':True,'manifest_digest':hashlib.sha256(canonical_payload(value).encode()).hexdigest()}))
        return 0
    except Exception:print('configuration_rejected',file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
