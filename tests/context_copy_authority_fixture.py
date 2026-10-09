import copy,json,pytest
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.applications import ResourceRestriction
from authority_fixture import authority_records
def configure_message_read(f,admin,*,allow,withdraw="body"):
 tenant=f['original']['tenant'];principal=f['source']._session.authentication.subject_principal_id;mid=f['message']['id']
 specs=[('eios:object:Message/'+mid,ResourceType.OBJECT),('eios:property:Message/'+mid+'/body',ResourceType.PROPERTY),('eios:property:Message/'+mid+'/actor',ResourceType.PROPERTY)]
 def change(manifest):
  source_app=f['source']._session.authentication.caller_application_id
  role=next(row['payload']['roles'][0] for row in manifest['authority_facts'] if row['kind']=='subject_authority' and row['key']==[principal])
  merged={(row['kind'],tuple(row['key'])):row for row in manifest['authority_facts']}
  configured=('grants',(principal,'eios:object:Message/'+mid)) in merged
  # Withdrawal targets configured Message authority. If the Source currently reads
  # through derivation (0073), configure first: configured authority then wins and a
  # withdrawn configured grant must not be revived by the derived path.
  if allow or not configured:
   for target,kind in specs:
    _,_,rows=authority_records(tenant,target,resource_type=kind,operation=Operation.READ,identity_suffix='-assembly-source')
    for name,key,fact in rows:
     if name not in ('resource_graph','grants','scope','controls','policies'):continue
     if name=='scope':fact=fact.model_copy(update={'catalog_scopes':f['source']._session.authentication.requested_scopes,'authorized_scopes':f['source']._session.authentication.requested_scopes})
     if name=='grants':fact=fact.model_copy(update={'grants':tuple(g.model_copy(update={'role_digest':role['digest']}) for g in fact.grants)})
     raw=fact.model_dump(mode='json');raw.pop('snapshot_digest',None);fact=type(fact).model_validate_json(json.dumps(raw))
     merged[name,tuple(key)]={'kind':name,'key':key,'payload':fact.model_dump(mode='json')}
   for row in merged.values():
    if row['kind']=='application' and row['key']==[source_app,'1']:
     raw=copy.deepcopy(row['payload']);raw.pop('snapshot_digest',None)
     raw['resources'] += [ResourceRestriction(tenant_id=tenant,resource_type=kind.value,resource_id=target).model_dump(mode='json') for target,kind in specs]
     row['payload']=F.ApplicationFacts.model_validate_json(json.dumps(raw)).model_dump(mode='json')
  if not allow:
   target='eios:object:Message/'+mid if withdraw=='object' else 'eios:property:Message/'+mid+'/'+withdraw
   row=merged['grants',(principal,target)];raw=copy.deepcopy(row['payload']);raw.pop('snapshot_digest',None);raw['grants']=[];row['payload']=F.GrantFacts.model_validate_json(json.dumps(raw)).model_dump(mode='json')
  manifest['authority_facts']=list(merged.values())
 from test_context_artifacts import reconfigure
 reconfigure(f,admin,change)

