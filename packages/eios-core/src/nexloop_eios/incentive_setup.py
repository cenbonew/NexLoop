"""S77: the incentive catalog through published governed Actions only (IncentiveOffering.create/edit,
ConsumerIncentiveOffering.create/edit), run by the catalog_maintainer service principal in world real.

No schema publication, no grant and no direct SQL. 0389 is the server-side rule (shape, real world only, a binding names an
active offering at its current revision and a current Consumer, an edit is only a withdrawal); this tool checks the recipe
and the deployment ceilings (owner Q6, configurable) before any write. Every step commits on its own and replays under
stable intents; a step the server refuses stops the run and is reported by step and index, never skipped. Amounts are
minor units (0145: amount_minor)."""
import argparse,hashlib,json,logging,re,sys,uuid
from datetime import UTC,datetime,timedelta
from pathlib import Path
from eios.identity.models import SubjectKind
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text

SETUP_SCHEMA='nexloop-incentive-setup/1'
WITHDRAW_SCHEMA='nexloop-incentive-withdraw/1'
OFFERING_KEYS={'incentive_code','max_amount_minor','per_consumer_period_limit_minor','currency','title','valid_until'}
# Owner Q6 (values to be confirmed by the owner): the tool's ceilings, overridable per run.
DEFAULT_MAX_AMOUNT_MINOR=10000
DEFAULT_MAX_VALIDITY_DAYS=90
MAX_CONSUMERS=50
HEX64=re.compile('[a-f0-9]{64}')
UTC_TIME=re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)')


class IncentiveSetupRefused(RuntimeError):
    """A refusal with a stable code (and the failed step); never the recipe values."""
    def __init__(self,code,step=None,index=None):
        super().__init__(code);self.code,self.step,self.index=code,step,index

    def report(self):
        return {'configured':False,'code':self.code,**({'step':self.step} if self.step else {}),**({'index':self.index} if self.index is not None else {})}


def _strict(text):
    if type(text) is not str or len(text.encode('utf8'))>32768:raise IncentiveSetupRefused('recipe_invalid')
    def pairs(rows):
        result={}
        for key,value in rows:
            if key in result:raise ValueError()
            result[key]=value
        return result
    def constant(unused):raise ValueError()
    try:value=json.loads(text,object_pairs_hook=pairs,parse_constant=constant)
    except ValueError:raise IncentiveSetupRefused('recipe_invalid') from None
    if type(value) is not dict:raise IncentiveSetupRefused('recipe_invalid')
    return value


def _request_id(value):
    if type(value) is not str:raise IncentiveSetupRefused('recipe_invalid')
    try:
        if str(uuid.UUID(value))!=value:raise ValueError()
    except ValueError:raise IncentiveSetupRefused('recipe_invalid') from None
    return value


def _minor(value):
    if type(value) is not str or not re.fullmatch('[1-9][0-9]{0,15}',value):raise IncentiveSetupRefused('recipe_invalid')
    return int(value)


def setup_recipe(text,*,max_amount_minor=DEFAULT_MAX_AMOUNT_MINOR,max_validity_days=DEFAULT_MAX_VALIDITY_DAYS,now=None):
    value=_strict(text)
    if set(value)!={'schema_version','request_id','offering','consumers'} or value['schema_version']!=SETUP_SCHEMA:raise IncentiveSetupRefused('recipe_invalid')
    _request_id(value['request_id'])
    offering=value['offering']
    if type(offering) is not dict or set(offering)!=OFFERING_KEYS:raise IncentiveSetupRefused('recipe_invalid')
    if type(offering['incentive_code']) is not str or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,63}',offering['incentive_code']):raise IncentiveSetupRefused('recipe_invalid')
    single,period=_minor(offering['max_amount_minor']),_minor(offering['per_consumer_period_limit_minor'])
    if period<single:raise IncentiveSetupRefused('recipe_invalid')
    if single>max_amount_minor:raise IncentiveSetupRefused('amount_above_ceiling')
    if type(offering['currency']) is not str or not re.fullmatch('[A-Z]{3}',offering['currency']):raise IncentiveSetupRefused('recipe_invalid')
    if type(offering['title']) is not str or not 1<=len(offering['title'])<=256:raise IncentiveSetupRefused('recipe_invalid')
    expiry=offering['valid_until']
    if type(expiry) is not str or not UTC_TIME.fullmatch(expiry):raise IncentiveSetupRefused('recipe_invalid')
    now=now or datetime.now(UTC);until=datetime.fromisoformat(expiry.replace('Z','+00:00'))
    if until<=now:raise IncentiveSetupRefused('recipe_invalid')
    if until>now+timedelta(days=max_validity_days):raise IncentiveSetupRefused('validity_above_ceiling')
    consumers=value['consumers']
    if (type(consumers) is not list or not 1<=len(consumers)<=MAX_CONSUMERS or len(set(map(str,consumers)))!=len(consumers)
        or any(type(c) is not str or not HEX64.fullmatch(c) for c in consumers)):raise IncentiveSetupRefused('recipe_invalid')
    return value


def withdraw_recipe(text):
    value=_strict(text)
    if set(value)!={'schema_version','request_id','target','object_id','expected_revision'} or value['schema_version']!=WITHDRAW_SCHEMA:raise IncentiveSetupRefused('recipe_invalid')
    _request_id(value['request_id'])
    if value['target'] not in ('offering','binding') or type(value['object_id']) is not str or not HEX64.fullmatch(value['object_id']):raise IncentiveSetupRefused('recipe_invalid')
    if type(value['expected_revision']) is not int or value['expected_revision']<1:raise IncentiveSetupRefused('recipe_invalid')
    return value


def offering_properties(offering):
    """The offering exactly as the 0145 admission and 0389 accept it: kind, eligibility and active are fixed here."""
    return {**{k:offering[k] for k in sorted(OFFERING_KEYS)},'kind':'fixed_amount','eligibility':'bound_consumer','active':True}


def _maintainer(backend,path):
    service=backend.authenticate(read_private_text(path,maximum=16384),world='real')
    session=service._session
    if session.world!='real' or session.run_context is not None or session.authentication.subject_kind is not SubjectKind.SERVICE:
        raise IncentiveSetupRefused('maintainer_unavailable')
    return service


def setup_incentives(backend,*,maintainer_credential_file,recipe):
    maintainer=_maintainer(backend,maintainer_credential_file);keeper=maintainer._session.authentication
    base='incentive-setup-'+recipe['request_id']
    try:
        offered=maintainer.create_object(action_name='IncentiveOffering.create',action_version=1,intent_id=base+'-offering',
            type_name='IncentiveOffering',properties=offering_properties(recipe['offering']))
    except Exception:raise IncentiveSetupRefused('refused','offering') from None
    bindings=[]
    for index,consumer in enumerate(recipe['consumers']):
        try:
            link=maintainer.create_object(action_name='ConsumerIncentiveOffering.create',action_version=1,
                intent_id=base+'-binding-'+hashlib.sha256(consumer.encode()).hexdigest()[:16],type_name='ConsumerIncentiveOffering',
                properties={'consumer_id':consumer,'offering_id':offered['object_id'],'offering_revision':1,
                            'source_principal':keeper.subject_principal_id,'active':True})
        except Exception:raise IncentiveSetupRefused('refused','binding',index) from None
        bindings.append({'consumer_id':consumer,'binding_id':link['object_id']})
    return {'configured':True,'tenant_id':keeper.tenant_id,'world_id':'real','offering_id':offered['object_id'],'bindings':bindings,
        'expected_offering_revision':1,'max_amount_minor':recipe['offering']['max_amount_minor'],'currency':recipe['offering']['currency'],
        'source_ref':'principal:'+keeper.subject_principal_id,'dispatch_authorized':False}


def withdraw(backend,*,maintainer_credential_file,recipe):
    maintainer=_maintainer(backend,maintainer_credential_file)
    type_name='IncentiveOffering' if recipe['target']=='offering' else 'ConsumerIncentiveOffering'
    try:
        changed=maintainer.edit_object(action_name=type_name+'.edit',action_version=1,intent_id='incentive-withdraw-'+recipe['request_id'],
            type_name=type_name,object_id=recipe['object_id'],expected_revision=recipe['expected_revision'],properties={'active':False})
    except Exception:raise IncentiveSetupRefused('refused','withdraw') from None
    return {'withdrawn':True,'target':recipe['target'],'object_id':recipe['object_id'],'revision':changed['revision']}


class _Parser(argparse.ArgumentParser):
    def error(self,unused):self.exit(2,'Incentive setup configuration unavailable\n')


def main(argv=None):
    parser=_Parser(description=__doc__)
    parser.add_argument('command',choices=('setup','withdraw'))
    for name in ('database-url-file','signing-key-file','maintainer-credential-file','recipe-file','artifact-root'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--signing-key-id',default='active')
    parser.add_argument('--max-amount-minor',type=int,default=DEFAULT_MAX_AMOUNT_MINOR)
    parser.add_argument('--max-validity-days',type=int,default=DEFAULT_MAX_VALIDITY_DAYS)
    parser.add_argument('--dry-run',action='store_true',help='check the recipe and the ceilings only; no connection')
    args=parser.parse_args(argv)
    logger=logging.getLogger('psycopg.pool');disabled=logger.disabled;logger.disabled=True
    try:
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}',args.signing_key_id) or not 1<=args.max_amount_minor<=10**16 or not 1<=args.max_validity_days<=366:
            raise IncentiveSetupRefused('configuration_invalid')
        text=read_private_text(args.recipe_file,maximum=32768)
        if args.command=='setup':recipe=setup_recipe(text,max_amount_minor=args.max_amount_minor,max_validity_days=args.max_validity_days)
        else:recipe=withdraw_recipe(text)
        if args.dry_run:
            print(canonical_payload({'dry_run':True,'command':args.command,**({'consumers':len(recipe['consumers'])} if args.command=='setup' else {'target':recipe['target']})}))
            return 0
        with open_backend(database_url=read_private_text(args.database_url_file,maximum=16384),artifact_root=args.artifact_root,signing_key_file=args.signing_key_file,signing_key_id=args.signing_key_id) as backend:
            with backend._pool.connection() as db:
                if verify_application_role(db)!='nexloop_api':raise IncentiveSetupRefused('database_role')
            if args.command=='setup':result=setup_incentives(backend,maintainer_credential_file=args.maintainer_credential_file,recipe=recipe)
            else:result=withdraw(backend,maintainer_credential_file=args.maintainer_credential_file,recipe=recipe)
        print(canonical_payload(result));return 0
    except IncentiveSetupRefused as refused:print(canonical_payload(refused.report()));return 1
    except Exception:print(canonical_payload({'configured':False,'code':'unavailable'}));return 1
    finally:logger.disabled=disabled

if __name__=='__main__':raise SystemExit(main())
