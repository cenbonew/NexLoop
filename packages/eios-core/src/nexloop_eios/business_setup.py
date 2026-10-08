"""Private bounded business setup through existing real governed Actions.

Every step commits independently. Stable step intents recover prior completed
work. Failure never reports a whole setup complete and never overwrites a prior
payload. Human membership is verified by the protected ownership registrar.
"""
import argparse
from datetime import UTC,datetime
import json
import logging
from pathlib import Path
import re
import sys
import uuid
from eios.identity.models import SubjectKind
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.conversation_messages import ConversationOwnershipRegistrar
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text


class BusinessSetupUnavailable(RuntimeError):
    def __init__(self):super().__init__('business_setup_unavailable')


def strict_recipe(text):
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ValueError()
            out[key]=value
        return out
    def constant(unused):raise ValueError()
    try:
        recipe=json.loads(text,object_pairs_hook=pairs,parse_constant=constant)
        if type(recipe) is not dict or set(recipe)!={'schema_version','request_id','human_principal_id','control'} or recipe['schema_version']!='1.0':raise ValueError()
        if type(recipe['request_id']) is not str or str(uuid.UUID(recipe['request_id']))!=recipe['request_id']:raise ValueError()
        human=recipe['human_principal_id']
        if type(human) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,319}',human):raise ValueError()
        control=recipe['control']
        if type(control) is not dict or set(control)!={'budget_units','valid_until'}:raise ValueError()
        if type(control['budget_units']) is not int or not 1<=control['budget_units']<=1000000:raise ValueError()
        stamp=control['valid_until']
        if type(stamp) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)',stamp):raise ValueError()
        if datetime.fromisoformat(stamp.replace('Z','+00:00'))<=datetime.now(UTC):raise ValueError()
        return recipe
    except Exception:raise BusinessSetupUnavailable() from None


def _service(backend,path):
    service=backend.authenticate(read_private_text(path,maximum=16384),world='real')
    session=service._session
    if session.world!='real' or session.run_context is not None or session.authentication.subject_kind is not SubjectKind.SERVICE:raise BusinessSetupUnavailable()
    return service


def setup_business(backend,*,owner_credential_file,executor_credential_file,recipe,verify_current=False):
    try:
        if type(verify_current) is not bool:raise ValueError()
        recipe=strict_recipe(canonical_payload(recipe))
        owner=_service(backend,owner_credential_file);executor=_service(backend,executor_credential_file)
        auth=owner._session.authentication;exe=executor._session.authentication
        if auth.tenant_id!=exe.tenant_id:raise ValueError()
        tenant,owner_principal,executor_principal=auth.tenant_id,auth.subject_principal_id,exe.subject_principal_id
        def fresh_owner():
            current=_service(backend,owner_credential_file)
            if (current._session.authentication.tenant_id,current._session.authentication.subject_principal_id)!=(tenant,owner_principal):raise ValueError()
            return current
        prefix='business-setup-'+recipe['request_id']
        consumer=fresh_owner().create_object(action_name='Consumer.create',action_version=1,
            intent_id=prefix+'-consumer',type_name='Consumer',properties={})
        if consumer.get('type_name')!='Consumer' or consumer.get('world')!='real' or not re.fullmatch('[a-f0-9]{64}',consumer['object_id']):raise ValueError()
        properties={'consumer_id':consumer['object_id'],'owner_principal':owner_principal,
            'executor_principal':executor_principal,'budget_units':recipe['control']['budget_units'],
            'allow_effect':True,'valid_until':recipe['control']['valid_until']}
        control=fresh_owner().create_object(action_name='EffectControl.create',action_version=1,
            intent_id=prefix+'-control',type_name='EffectControl',properties=properties)
        if control.get('type_name')!='EffectControl' or control.get('world')!='real' or not re.fullmatch('[a-f0-9]{64}',control['object_id']):raise ValueError()
        executor=_service(backend,executor_credential_file)
        if (executor._session.authentication.tenant_id,executor._session.authentication.subject_principal_id)!=(tenant,executor_principal):raise ValueError()
        fresh_owner().configure_effect_control(control_id=control['object_id'],control_revision=1,
            executor_token=read_private_text(executor_credential_file,maximum=16384))
        owner=fresh_owner()
        with backend._lock:
            backend._assert_open()
            ownership=ConversationOwnershipRegistrar(backend._pool,owner._session,backend._signer).register_consumer_owner(
                consumer_id=consumer['object_id'],principal_id=recipe['human_principal_id'],idempotency_key=prefix+'-ownership')
        if ownership['consumer_id']!=consumer['object_id'] or ownership['principal_id']!=recipe['human_principal_id']:raise ValueError()
        result={'configured':True,'tenant_id':tenant,'world_id':'real','consumer_id':consumer['object_id'],
            'expected_consumer_revision':1,'control_id':control['object_id'],
            'expected_control_revision':1,'owner_ref':'principal:'+owner_principal,
            'executor_ref':'principal:'+executor_principal,'ownership_id':ownership['ownership_id']}
        if verify_current:
            # Optional real READ authority, never admin SQL or an automatic grant.
            current_consumer=fresh_owner().read_object(type_name='Consumer',object_id=consumer['object_id'])
            current_control=fresh_owner().read_object(type_name='EffectControl',object_id=control['object_id'],fields=tuple(properties))
            if current_consumer['revision']!=1 or current_control['revision']!=1 or current_control['properties']!=properties:raise ValueError()
            result.update(current_consumer_revision=current_consumer['revision'],current_control_revision=current_control['revision'])
        return result
    except Exception:raise BusinessSetupUnavailable() from None


class _Parser(argparse.ArgumentParser):
    def error(self,unused):self.exit(2,'Business setup configuration unavailable\n')


def main(argv=None):
    parser=_Parser(description=__doc__)
    for name in ('database-url-file','signing-key-file','owner-credential-file','executor-credential-file','recipe-file','artifact-root'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--signing-key-id',default='active')
    parser.add_argument('--verify-current',action='store_true')
    args=parser.parse_args(argv)
    logger=logging.getLogger('psycopg.pool');disabled=logger.disabled;logger.disabled=True
    try:
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}',args.signing_key_id):raise ValueError()
        recipe=strict_recipe(read_private_text(args.recipe_file,maximum=32768))
        with open_backend(database_url=read_private_text(args.database_url_file,maximum=16384),
            artifact_root=args.artifact_root,signing_key_file=args.signing_key_file,signing_key_id=args.signing_key_id) as backend:
            with backend._pool.connection() as db:
                if verify_application_role(db)!='nexloop_api':raise ValueError()
            result=setup_business(backend,owner_credential_file=args.owner_credential_file,executor_credential_file=args.executor_credential_file,recipe=recipe,verify_current=args.verify_current)
        print(canonical_payload(result));return 0
    except Exception:
        print('Business setup unavailable',file=sys.stderr);return 1
    finally:logger.disabled=disabled

if __name__=='__main__':raise SystemExit(main())
