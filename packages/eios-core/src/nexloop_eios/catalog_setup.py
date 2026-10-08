"""Private catalog bootstrap using already published governed CREATE Actions.

No schema publication or grant creation. Maintainer and delivery Source are
separate real services; Source receives no CREATE permission here. Each step
commits independently and reuses stable governed intents on identical replay.
"""
import argparse,json,logging,re,sys,uuid
from datetime import UTC,datetime
from pathlib import Path
from eios.identity.models import SubjectKind
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text

class CatalogSetupUnavailable(RuntimeError):
    def __init__(self):super().__init__('catalog_setup_unavailable')


def strict_recipe(text):
    try:
        if type(text) is not str or len(text.encode('utf8'))>32768:raise ValueError()
        def pairs(rows):
            result={}
            for key,value in rows:
                if key in result:raise ValueError()
                result[key]=value
            return result
        def constant(unused):raise ValueError()
        value=json.loads(text,object_pairs_hook=pairs,parse_constant=constant)
        if type(value) is not dict or set(value)!={'schema_version','request_id','consumer_id','valid_until'} or value['schema_version']!='1.0':raise ValueError()
        if type(value['request_id']) is not str or str(uuid.UUID(value['request_id']))!=value['request_id']:raise ValueError()
        if type(value['consumer_id']) is not str or not re.fullmatch('[a-f0-9]{64}',value['consumer_id']):raise ValueError()
        expiry=value['valid_until']
        if type(expiry) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)',expiry):raise ValueError()
        if datetime.fromisoformat(expiry.replace('Z','+00:00'))<=datetime.now(UTC):raise ValueError()
        return value
    except Exception:raise CatalogSetupUnavailable() from None


def _service(backend,path):
    service=backend.authenticate(read_private_text(path,maximum=16384),world='real')
    session=service._session
    if session.world!='real' or session.run_context is not None or session.authentication.subject_kind is not SubjectKind.SERVICE:raise ValueError()
    return service


def setup_catalog(backend,*,maintainer_credential_file,source_credential_file,recipe):
    try:
        from nexloop_eios.service_offerings import governed_create_catalog
        recipe=strict_recipe(canonical_payload(recipe))
        maintainer=_service(backend,maintainer_credential_file);source=_service(backend,source_credential_file)
        keeper=maintainer._session.authentication;delivery=source._session.authentication
        if keeper.tenant_id!=delivery.tenant_id or keeper.subject_principal_id==delivery.subject_principal_id:raise ValueError()
        result=governed_create_catalog(maintainer,delivery_source=source,intent_id='catalog-setup-'+recipe['request_id'],consumer_id=recipe['consumer_id'],valid_until=recipe['valid_until'])
        if type(result) is not dict or set(result)!={'consumer_id','offering_id','binding_id'} or result['consumer_id']!=recipe['consumer_id'] or any(type(result[k]) is not str or not re.fullmatch('[a-f0-9]{64}',result[k]) for k in result):raise ValueError()
        return {'configured':True,'tenant_id':keeper.tenant_id,'world_id':'real',**result,'expected_offering_revision':1,'expected_binding_revision':1,'source_ref':'principal:'+delivery.subject_principal_id,'scope':'catalog_registration','dispatch_authorized':False}
    except Exception:raise CatalogSetupUnavailable() from None

class _Parser(argparse.ArgumentParser):
    def error(self,unused):self.exit(2,'Catalog setup configuration unavailable\n')


def main(argv=None):
    parser=_Parser(description=__doc__)
    for name in ('database-url-file','signing-key-file','maintainer-credential-file','source-credential-file','recipe-file','artifact-root'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--signing-key-id',default='active');args=parser.parse_args(argv)
    logger=logging.getLogger('psycopg.pool');disabled=logger.disabled;logger.disabled=True
    try:
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}',args.signing_key_id):raise ValueError()
        recipe=strict_recipe(read_private_text(args.recipe_file,maximum=32768))
        with open_backend(database_url=read_private_text(args.database_url_file,maximum=16384),artifact_root=args.artifact_root,signing_key_file=args.signing_key_file,signing_key_id=args.signing_key_id) as backend:
            with backend._pool.connection() as db:
                if verify_application_role(db)!='nexloop_api':raise ValueError()
            result=setup_catalog(backend,maintainer_credential_file=args.maintainer_credential_file,source_credential_file=args.source_credential_file,recipe=recipe)
        print(canonical_payload(result));return 0
    except Exception:print('Catalog setup unavailable',file=sys.stderr);return 1
    finally:logger.disabled=disabled

if __name__=='__main__':raise SystemExit(main())
