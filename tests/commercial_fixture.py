"""NX-027 commercial events fixture on clean catalog PostgreSQL (synthetic data only).

On top of the published Consumer fixture (bootstrap, Consumer type and Action, signer, nexloop_api pool):

* the CommercialRecord type v1 from deploy/ontology/business-object-types.v1.json and CommercialRecord.create / .edit
  compiled from deploy/configuration/business-actions.v1.json for the real and the test world (published by admin as
  configuration fixtures, the way the goal and commitment fixtures publish theirs);
* one commercial_recorder service per world on the domain-worker role with the manifest's grants (seeded) and the
  commercial_state property_access_rule (fact fixture);
* connectors and customer links configured through the actual nexloop_configurator functions (key from a private file);
* events signed with the connector key and passed to the actual intake function as the API role does.
Every record write after that goes through the recorder, the governed Actions and the SQL guard.
"""
from contextlib import ExitStack
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,time,uuid
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import business_actions,business_object_types,commercial
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.property_access import PropertyAccessRule,PropertyGroupRestriction
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action  # noqa: F401
from test_postgres_action_claims import governance_inputs

ROOT=Path(__file__).resolve().parents[1]
TENANT='synthetic-a'
SETTINGS=ROOT/'deploy/configuration/commercial.v1.json'
TYPES=['order.created','order.paid','order.cancelled','payment.pending','payment.succeeded','payment.failed','subscription.renewed','refund.succeeded']
RECORDER_TARGETS=[(r,ResourceType.ACTION,Operation.EXECUTE) for r in ('eios:action:NexLoop.feed.commercial-record:1','eios:action:nexloop.commercial.record:1',
    'eios:action:nexloop.commercial.read:1','eios:action:CommercialRecord.create:1','eios:action:CommercialRecord.edit:1')]+[
    ('eios:object_type:CommercialRecord',ResourceType.OBJECT_TYPE,Operation.READ)]


def ts(value):return value.astimezone(UTC).isoformat()


def publish_commercial_type(admin,worlds=('real','test'),tenant=TENANT):
    rows=[r for r in business_object_types.trusted_object_types(ROOT/'deploy/ontology/business-object-types.v1.json') if r['type_name']=='CommercialRecord']
    for row in rows:
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s) on conflict do nothing',
            (tenant,row['type_name'],row['version'],Jsonb(row)))
    base=governance_inputs()['capability_snapshot']
    caps={name:base.model_copy(update={'capability_name':name,'has_side_effects':True}) for name in ('ontology.object.create','ontology.object.edit')}
    compiled=business_actions.compile_actions(business_actions.load(ROOT/'deploy/configuration/business-actions.v1.json'),tenant=tenant,
        created_by='synthetic-configuration',created_at=datetime.now(UTC),object_types=rows,capabilities=caps,
        select=('CommercialRecord.create','CommercialRecord.edit'))
    for world in worlds:
        for row in compiled:
            d=row['definition']
            admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
                (tenant,world,f"eios:action:{d['stable_name']}:{d['version']}",Jsonb(d),Jsonb(row['capability'])))
    return rows


def state_rule(admin,principal,tenant=TENANT):
    value=dict(tenant_id=tenant,principal_id=principal,type_name='CommercialRecord',operations=('edit','read'),property_groups=('commercial_state',),
        include_review_published=False,basis_schema_version=1,active=True,valid_until=datetime.now(UTC)+timedelta(days=1))
    admin.execute('''insert into authz.nexloop_authority_facts values(%s,%s,%s,%s)
        on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload''',
        (tenant,'property_access_rule',[principal,'CommercialRecord'],Jsonb(PropertyAccessRule(**value).model_dump(mode='json'))))
    # Derivation needs the owner's restriction decision for the type (0084); a synthetic owner decision restricts no group.
    admin.execute('''insert into authz.nexloop_authority_facts values(%s,%s,%s,%s)
        on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload''',
        (tenant,'property_group_restriction',['CommercialRecord'],Jsonb(PropertyGroupRestriction(tenant_id=tenant,type_name='CommercialRecord',
            restricted_groups=(),decision='synthetic owner decision').model_dump(mode='json'))))


class Commercial(dict):
    def __repr__(self):return '<synthetic NX-027 commercial configuration>'
    __str__=__repr__

    # -- configuration (actual nexloop_configurator functions) ----------------------------------------------------
    def key_file(self,name):
        path=self['tmp_path']/f'connector-key-{name}';path.write_text(secrets.token_hex(32));path.chmod(0o600)
        return path

    def connector(self,connector_id='synthetic-shop',*,world='real',data_mode='real',types=TYPES,currencies=('CNY','USD','JPY'),key_id='k1',
                  confirmation='synthetic owner confirmation',rotation_seconds=86400):
        key=self.key_file(f'{connector_id}-{key_id}')
        result=commercial.configure_connector(self['configurator_dsn'],tenant=TENANT,world=world,connector_id=connector_id,data_mode=data_mode,
            event_types=types,currencies=currencies,key_id=key_id,key_file=key,owner_confirmation=confirmation,rotation_seconds=rotation_seconds)
        self.setdefault('keys',{})[connector_id]=bytes.fromhex(key.read_text())
        return result

    def link(self,customer_ref,consumer_id,connector_id='synthetic-shop'):
        return commercial.link_customer(self['configurator_dsn'],connector_id,customer_ref,consumer_id)

    def unlink(self,customer_ref,connector_id='synthetic-shop'):
        return commercial.unlink_customer(self['configurator_dsn'],connector_id,customer_ref)

    def consumer(self,world='real'):
        """A Consumer object (admin seed of the governed Consumer.create write, as other fixtures do)."""
        object_id=hashlib.sha256(secrets.token_bytes(16)).hexdigest()
        self['admin'].execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,%s,'Consumer',%s,1,'{}','nexloop-action',%s,clock_timestamp(),clock_timestamp())''',(TENANT,world,object_id,'synthetic-consumer-'+object_id[:12]))
        return object_id

    # -- events (actual intake function, as the API calls it) -----------------------------------------------------
    def event(self,type_,external_id,*,amount=10000,currency='CNY',customer='cust-1',occurred_at=None,event_id=None,sequence=None,related=None):
        body={'event_id':event_id or 'evt-'+uuid.uuid4().hex,'type':type_,'external_id':external_id,'occurred_at':ts(occurred_at or datetime.now(UTC)),
            'amount_minor':amount,'currency':currency,'customer_ref':customer}
        if sequence is not None:body['provider_sequence']=sequence
        if related is not None:body['related_kind'],body['related_external_id']=related
        return body

    def post(self,body,*,connector_id='synthetic-shop',timestamp=None,signature=None,raw=None):
        text=raw if raw is not None else json.dumps(body)
        timestamp=str(int(time.time())) if timestamp is None else timestamp
        if signature is None:signature=commercial.sign(self['keys'][connector_id],timestamp,text)
        return commercial.ingest(self['api_pool'],connector_id,timestamp,signature,text)

    # -- services -------------------------------------------------------------------------------------------------
    def session(self,world='real'):return authenticate_service(self['worker'],self['tokens'][world],world=world)
    def recorder(self,world='real'):return commercial.CommercialRecorder(self['worker'],self.session(world),self['signer'],settings=self['settings'])
    def reader(self,world='real'):return commercial.CommercialReadPort(self['worker'],self.session(world),self['signer'])

    def tick(self,world='real',times=4):
        out=[]
        for _ in range(times):
            out.append(self.recorder(world).run_once())
            if not any(out[-1].values()):break
        return out

    # -- probes ---------------------------------------------------------------------------------------------------
    def record_key(self,kind,external_id,connector_id='synthetic-shop',world='real'):
        return self['admin'].execute('select runtime.nexloop_commercial_key(%s,%s,%s,%s,%s)',(TENANT,world,connector_id,kind,external_id)).fetchone()[0]

    def record(self,kind,external_id,connector_id='synthetic-shop',world='real'):
        key=self.record_key(kind,external_id,connector_id,world)
        row=self['admin'].execute('''select o.properties,o.nexloop_revision,o.object_id from runtime.nexloop_commercial_records r
            join ontology.objects o on o.tenant_id=r.tenant_id and o.world=r.world and o.type_name='CommercialRecord' and o.object_id=r.object_id
            where r.tenant_id=%s and r.world=%s and r.record_key=%s''',(TENANT,world,key)).fetchone()
        return None if row is None else {'properties':row[0],'revision':row[1],'object_id':row[2]}

    def exceptions(self,world='real'):
        return self['admin'].execute('select subject_ref,reason,detail from runtime.nexloop_commercial_exceptions where tenant_id=%s and world=%s order by raised_at,reason',
            (TENANT,world)).fetchall()


@pytest.fixture
def commercial_env(published_action,admin,pg,tmp_path):
    reader,_,_=published_action
    publish_commercial_type(admin)
    admin.execute('alter role nexloop_configurator login')
    dsn=tmp_path/'configurator-dsn';dsn.write_text(make_conninfo(pg,user='nexloop_configurator'));dsn.chmod(0o600)
    with ExitStack() as stack:
        worker=stack.enter_context(open_core(make_conninfo(pg,user='nexloop_domain_worker')))
        tokens={}
        for world in ('real','test'):
            _,tokens[world]=seed_multi_authority(admin,worker,RECORDER_TARGETS,identity_suffix='-commercial-recorder-'+world,world=world,tenant=TENANT)
        for principal, in admin.execute("select distinct entity_key[1] from authz.nexloop_authority_facts where fact_kind='grants' and entity_key[2]='eios:action:nexloop.commercial.record:1'").fetchall():
            state_rule(admin,principal)
        yield Commercial(admin=admin,pg=pg,tmp_path=tmp_path,worker=worker,api_pool=reader.pool,signer=reader.signer,tokens=tokens,
            configurator_dsn=dsn,settings=commercial.load_settings(SETTINGS))
