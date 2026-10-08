"""Multi-resource synthetic authority configuration; no business writes."""
import hashlib
import secrets
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.applications import ResourceRestriction,OperationRestriction
from authority_fixture import authority_records
from nexloop_eios.authorization import authenticate_service


def seed_multi_authority(admin,pool,targets,*,identity_suffix,world='real',extra_scopes=()):
    scopes=frozenset(kind.value+'.'+op.value for _,kind,op in targets)|frozenset(extra_scopes)
    records={}; apps=[]
    for target,kind,op in targets:
        auth,expiry,rows=authority_records('synthetic-a',target,world=world,resource_type=kind,operation=op,identity_suffix=identity_suffix)
        for k,key,fact in rows:
            if k=='application':apps.append(fact)
            previous=records.get((k,tuple(key)))
            if k=='grants' and previous is not None:
                fact=fact.model_copy(update={'grants':tuple(dict.fromkeys(previous[2].grants+tuple(g.model_copy(update={'grant_id':g.grant_id+'-'+op.value}) for g in fact.grants)))})
            records[(k,tuple(key))]=(k,key,fact)
    app=apps[0].model_copy(update={'resources':tuple(ResourceRestriction(tenant_id='synthetic-a',resource_type=kind.value,resource_id=target) for target,kind in dict.fromkeys((target,kind) for target,kind,op in targets)),
        'operations':tuple(OperationRestriction(operation=op) for op in sorted({op for _,_,op in targets},key=lambda op:op.value))})
    auth=auth.model_copy(update={'requested_scopes':scopes,'caller_application_digest':app.version_digest})
    for mapkey,(kind,key,fact) in list(records.items()):
        if kind=='application':fact=app
        if kind=='scope':fact=fact.model_copy(update={'catalog_scopes':scopes,'authorized_scopes':scopes})
        if kind=='authentication':fact=F.CredentialAuthenticationFacts(**auth.model_dump(),status='active',expires_at=expiry,repository_witness=fact.repository_witness)
        if kind=='grants':
            role=next(v[2].roles[0] for v in records.values() if v[0]=='subject_authority')
            fact=fact.model_copy(update={'grants':tuple(g.model_copy(update={'role_digest':role.digest}) for g in fact.grants)})
        raw=fact.model_dump(mode='json');raw.pop('snapshot_digest',None)
        fact=type(fact).model_validate_json(__import__("json").dumps(raw))
        records[mapkey]=(kind,key,fact)
    token=secrets.token_urlsafe(48)
    admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
        (hashlib.sha256(token.encode()).hexdigest(),'synthetic-a',auth.credential_id,Jsonb(auth.model_dump(mode='json')),[world],'nexloop-core','active',expiry))
    for kind,key,fact in records.values():
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
            ('synthetic-a',kind,key,Jsonb(fact.model_dump(mode='json'))))
    session=authenticate_service(pool,token,world=world)
    return session,token
