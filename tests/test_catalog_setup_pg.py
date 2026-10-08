"""Actual restricted CLI/Governor catalog registration; no automatic READ grant."""
from datetime import UTC,datetime,timedelta
from pathlib import Path
import json,os,subprocess,sys,uuid
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from effect_execution_fixture import execution_plan
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def cli(execution_plan,admin,pg,tmp_path):
    plan=execution_plan
    _,token=seed_multi_authority(admin,plan['backend']._pool,[('eios:action:'+name+'.create:1',ResourceType.ACTION,Operation.EXECUTE) for name in ('ServiceOffering','ConsumerServiceOffering')],identity_suffix='-catalog-cli-maintainer')
    def private(name,text):
        path=tmp_path/name;path.write_text(text);path.chmod(0o600);return path
    paths={'dsn':private('catalog-dsn',make_conninfo(pg,user='nexloop_api')),'keeper':private('catalog-keeper',token),'source':private('catalog-source',plan['submitter_token'])}
    recipe={'schema_version':'1.0','request_id':str(uuid.uuid4()),'consumer_id':plan['consumer'],'valid_until':(datetime.now(UTC)+timedelta(minutes=3)).isoformat()}
    recipe_path=private('catalog-recipe',json.dumps(recipe))
    def run(**change):
        args=[sys.executable,'-m','nexloop_eios.catalog_setup','--database-url-file',str(paths['dsn']),'--signing-key-file',str(plan['key']),'--signing-key-id','synthetic-plan','--maintainer-credential-file',str(change.get('keeper',paths['keeper'])),'--source-credential-file',str(change.get('source',paths['source'])),'--recipe-file',str(recipe_path),'--artifact-root',str(tmp_path/'catalog-cli-artifacts')]
        result=subprocess.run(args,env={'PATH':os.environ['PATH'],'PYTHONPATH':str(ROOT/'packages/eios-core/src')+os.pathsep+str(ROOT/'tests')},capture_output=True,text=True,timeout=15)
        assert token not in result.stdout+result.stderr and plan['submitter_token'] not in result.stdout+result.stderr
        return result
    return {'run':run,'recipe':recipe,'recipe_path':recipe_path,'paths':paths,'consumer':plan['consumer']}

def test_actual_cli_same_consumer_stable_replay_no_dispatch_claim(cli,admin):
    first=cli['run']();assert first.returncode==0,first.stderr
    receipt=json.loads(first.stdout);assert receipt['consumer_id']==cli['consumer'] and receipt['dispatch_authorized'] is False
    replay=cli['run']();assert replay.returncode==0 and json.loads(replay.stdout)==receipt
    assert admin.execute('select count(*) from ontology.objects where object_id in (%s,%s)',(receipt['offering_id'],receipt['binding_id'])).fetchone()==(2,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)

def test_changed_recipe_payload_does_not_overwrite_stable_catalog(cli,admin):
    first=cli['run']();assert first.returncode==0
    old=json.loads(first.stdout);cli['recipe']['valid_until']=(datetime.now(UTC)+timedelta(minutes=4)).isoformat();cli['recipe_path'].write_text(json.dumps(cli['recipe']))
    changed=cli['run']();assert changed.returncode==1 and changed.stdout=='' and changed.stderr.strip()=='Catalog setup unavailable'
    assert admin.execute('select nexloop_revision from ontology.objects where object_id=%s',(old['offering_id'],)).fetchone()==(1,)

def test_source_cannot_be_maintainer_or_create_catalog(cli,admin):
    before=admin.execute("select count(*) from ontology.objects where type_name in ('ServiceOffering','ConsumerServiceOffering')").fetchone()
    same=cli['run'](keeper=cli['paths']['source']);assert same.returncode==1
    distinct=cli['run'](keeper=cli['paths']['source'],source=cli['paths']['keeper']);assert distinct.returncode==1
    assert admin.execute("select count(*) from ontology.objects where type_name in ('ServiceOffering','ConsumerServiceOffering')").fetchone()==before

def test_wrong_consumer_registration_remains_not_a_dispatch_permit(cli,admin):
    cli['recipe']['consumer_id']='f'*64;cli['recipe_path'].write_text(json.dumps(cli['recipe']))
    result=cli['run']();assert result.returncode==0
    value=json.loads(result.stdout);assert value['consumer_id']=='f'*64 and value['dispatch_authorized'] is False
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
