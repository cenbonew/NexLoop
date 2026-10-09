"""Process entries for the background services (NX-019/020/021/045) on clean catalog PostgreSQL.

Credentials come from the repository service-grants manifest applied through trusted
configuration (as deployed); each entry runs one ``--once`` tick in-process with private
files only. Outputs are counts-only; DSNs, tokens and keys never appear.
"""
import json

import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
from nexloop_eios import background_services as B
from test_service_grants_pg import TENANT,deployment,private  # noqa: F401

ROLES={'claim-extraction-scheduler':('claim_extraction_scheduler','nexloop_api'),'claim-extraction-worker':('claim_extraction_worker','nexloop_domain_worker'),
    'claim-matcher':('claim_matcher','nexloop_domain_worker'),'recall-indexer':('recall_indexer','nexloop_domain_worker')}


def argv(d,tmp_path,service,*,role=None):
    principal,database_role=ROLES[service]
    reference=next(p for p in d['manifest']['principals'] if p['role']==principal)['credential_reference']
    root=tmp_path/service
    root.mkdir(exist_ok=True)
    args=['--database-url-file',str(private(root,'database_url',make_conninfo(d['pg'],user=role or database_role))),
        '--signing-key-file',str(signing_file(root,d)),'--signing-key-id','nx048-deploy',
        '--service-credential-file',str(private(root,'service_credential',d['tokens'][reference])),
        '--artifact-root',str(tmp_path/'artifacts'),'--world','real','--once']
    if service in ('claim-extraction-worker','claim-matcher'):
        args+=['--model-env-file',str(private(root,'model.env','MODEL_PROVIDER=\n'))]
    if service=='claim-matcher':
        args+=['--match-config-file',str(private(root,'match-config.json',json.dumps({'edit_actions':{'Consumer':['Consumer.edit',1]},'create_actions':{}})))]
    if service=='recall-indexer':
        args+=['--types-file',str(private(root,'types.json',json.dumps({'Consumer':None})))]
    return args


def signing_file(root,d):
    path=root/'artifact_key';path.write_bytes(d['signer'].material);path.chmod(0o600);return path


def hidden(d):return [*d['tokens'].values(),d['paths']['signing'].read_text().strip(),d['pg']]


@pytest.mark.parametrize('service,expected',[('claim-extraction-scheduler',{'enqueued':0}),('claim-extraction-worker',{'status':'idle'}),
    ('claim-matcher',{'applied':0,'changed':0,'conversations':0,'dead_lettered':0,'glued':0,'lease_lost':0,'matched':0,'retry':0}),
    ('recall-indexer',{'changed':0,'dead_lettered':0,'indexed':0,'lease_lost':0,'removed':0,'retry':0,'skipped':0})])
def test_each_entry_runs_one_tick_with_manifest_credentials(deployment,tmp_path,capsys,service,expected):
    d=deployment;d['apply']()
    assert B.main_for(service,argv(d,tmp_path,service))==0
    out,err=capsys.readouterr()
    lines=out.strip().splitlines()
    assert lines[0]==B.SERVICES[service][0]+' ready' and json.loads(lines[1])==expected and err==''
    assert not any(secret in out+err for secret in hidden(d))


def test_recall_indexer_entry_indexes_a_marked_object(deployment,admin,tmp_path,capsys):
    d=deployment;d['apply']()
    schema=ObjectTypeDefinition(type_name='Consumer',version=1,title_property='display_name',
        properties=(PropertyDefinition(property_name='display_name',value_type=PropertyValueType.STRING),))
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',1,%s)",(TENANT,Jsonb(schema.model_dump(mode='json'))))
    admin.execute("""insert into ontology.objects(tenant_id,type_name,object_id,schema_version,properties,created_at,updated_at,world,nexloop_revision)
        values(%s,'Consumer',%s,1,'{"display_name":"张伟"}',now(),now(),'real',1)""",(TENANT,'a'*64))
    assert B.main_for('recall-indexer',argv(d,tmp_path,'recall-indexer'))==0
    summary=json.loads(capsys.readouterr().out.strip().splitlines()[1])
    assert summary['indexed']==1
    assert admin.execute("select field,body from ontology.nexloop_recall_entries where source_key=%s",('object:real/Consumer/'+'a'*64,)).fetchall()==[('title','张伟')]
    assert admin.execute('select count(*) from runtime.nexloop_work_feed').fetchone()==(0,)


def test_wrong_role_or_revoked_credential_fails_closed_with_fixed_text(deployment,admin,tmp_path,capsys):
    d=deployment;d['apply']()
    # A domain-worker service process given the API role DSN refuses to start.
    assert B.main_for('recall-indexer',argv(d,tmp_path,'recall-indexer',role='nexloop_api'))==1
    out,err=capsys.readouterr()
    assert out=='' and err=='Recall Indexer unavailable\n'
    # The scheduler principal holds no recall-instance feed authority: the tick fails, nothing leaks.
    args=argv(d,tmp_path,'recall-indexer')
    scheduler=next(p for p in d['manifest']['principals'] if p['role']=='claim_extraction_scheduler')['credential_reference']
    (tmp_path/'recall-indexer'/'service_credential').write_text(d['tokens'][scheduler])
    assert B.main_for('recall-indexer',args)==1
    out,err=capsys.readouterr()
    assert err.endswith('Recall Indexer unavailable\n') and not any(secret in out+err for secret in hidden(d))


def test_invalid_arguments_never_echo(capsys):
    with pytest.raises(SystemExit) as exit_:B.main_for('claim-matcher',['--database-url-file','postgresql://user:secret-password@host/db','--world','shadow'])
    assert exit_.value.code==2
    out,err=capsys.readouterr()
    assert err=='Claim Matcher configuration unavailable\n' and 'secret-password' not in out+err
