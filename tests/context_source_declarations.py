"""Typed context Source configuration declarations; never authorization callbacks."""
import json
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.applications import ResourceRestriction,OperationRestriction
from authority_fixture import authority_records
from nexloop_eios.context_artifacts import ACTION

def source_declarations(tenant, catalog_targets=(), *, identity_suffix='-assembly-source'):
    specs=[('eios:action:nexloop.service.request:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:'+ACTION+':1',ResourceType.ACTION,Operation.EXECUTE),
     ('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.CREATE),('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.READ)]
    specs.extend((target,kind,Operation.READ) for target,kind in catalog_targets)
    records={};scopes={kind.value+'.'+op.value for _,kind,op in specs}
    for target,kind,op in specs:
        binding,expiry,rows=authority_records(tenant,target,operation=op,resource_type=kind,operations=(Operation.CREATE,Operation.READ) if kind is ResourceType.ARTIFACT else None,identity_suffix=identity_suffix)
        for name,key,fact in rows:records[name,tuple(key)]=(name,key,fact)
    app=next(row[2] for row in records.values() if row[0]=='application')
    resources=[ResourceRestriction(tenant_id=tenant,resource_type=kind.value,resource_id=target) for target,kind in sorted(set((t,k) for t,k,_ in specs),key=lambda p:p[0])]
    digest=F.canonical_authority_digest({'resources':[r.model_dump(mode='json') for r in resources]})
    app=app.model_copy(update={'application_id':app.application_id+(':catalog' if catalog_targets else ':context'),'resources':tuple(resources),'operations':tuple(OperationRestriction(operation=op) for op in (Operation.EXECUTE,Operation.CREATE,Operation.READ)),'version_digest':digest,'record_digest':digest})
    binding=binding.model_copy(update={'credential_id':binding.credential_id+(':catalog' if catalog_targets else ':context'),'caller_application_id':app.application_id,'caller_application_digest':digest,'requested_scopes':frozenset(scopes)})
    role=next(row[2].roles[0] for row in records.values() if row[0]=='subject_authority')
    for index,(name,key,fact) in list(records.items()):
        if name=='application':fact=app
        if name=='authentication':fact=F.CredentialAuthenticationFacts(**binding.model_dump(),status='active',expires_at=expiry,repository_witness=fact.repository_witness)
        if name=='scope':fact=fact.model_copy(update={'catalog_scopes':frozenset(scopes),'authorized_scopes':frozenset(scopes)})
        if name=='grants':fact=fact.model_copy(update={'grants':tuple(g.model_copy(update={'role_digest':role.digest}) for g in fact.grants)})
        body=fact.model_dump(mode='json');body.pop('snapshot_digest',None)
        if name=='application':key=[app.application_id,'1']
        if name=='authentication':key=[binding.credential_id]
        records[index]=(name,key,type(fact).model_validate_json(json.dumps(body)))
    return binding,[{'kind':name,'key':key,'payload':fact.model_dump(mode='json')} for name,key,fact in records.values()]

