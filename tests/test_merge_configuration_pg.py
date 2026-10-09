"""Owner-approved production glue configuration (deploy/ontology/merge-config.v1.json) through trusted configuration.

The manifest is applied by nexloop_configurator only. The recall profile is
activated with the production model id/dimension by the deterministic test
provider (no endpoint call): only the profile identity matters here.
"""
import copy
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import merge_configuration as M
from nexloop_eios.assembly import open_core
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.embedding_provider import DeterministicTestEmbeddingProvider
from nexloop_eios.recall import RecallIndexer
from multi_authority_fixture import seed_multi_authority

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'deploy/ontology/merge-config.v1.json'
TENANT='0d6a9f84-0000-4000-8000-000000000046'


def test_manifest_values_are_the_real_calibration_and_ci_keeps_the_test_values():
    manifest=M.load(MANIFEST);real=json.loads((ROOT/'docs/implementation/NX-045-real-calibration.json').read_text())
    assert manifest['weights']==real['result']['weights']=={'lexical_similarity':0.4,'core_term_containment':0.1,'vector_cluster':0.1,'rule_whitelist':0.4}
    assert manifest['merge_threshold']==real['result']['merge_threshold']==0.35
    assert f"{manifest['embedding_profile']['model']}@{manifest['embedding_profile']['dimension']}"==real['embedding_profile']=='doubao-embedding-vision-251215@1024'
    c=manifest['calibration']
    assert (c['pairs'],c['precision'],c['recall'],c['false_positive'],c['evidence_class'])==(42,1.0,0.6667,0,'real')
    ci=json.loads((ROOT/'tests/data/nx045_merge_config.json').read_text())
    assert ci['config_version']!=manifest['config_version'] and ci['calibration']['embedding_profile']=='nexloop-test-ngram-v1@64'


@pytest.mark.parametrize('change',[lambda m:m['weights'].update(rule_whitelist=0.2,lexical_similarity=0.6),
    lambda m:m['weights'].update(lexical_similarity=0.5),lambda m:m['calibration'].update(false_positive=1),
    lambda m:m.update(feature_version='other'),lambda m:m.update(extra=True),lambda m:m['embedding_profile'].update(dimension='1024')])
def test_invalid_manifests_are_refused(change):
    manifest=json.loads(MANIFEST.read_text());change(manifest)
    with pytest.raises(M.MergeConfigurationRejected):M.validate(manifest)


@pytest.fixture
def deployment(pg,admin,tmp_path):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')",(TENANT,))
    admin.execute('alter role nexloop_configurator login')  # deployment step, as in NX-048
    dsn=tmp_path/'configurator-dsn';dsn.write_text(make_conninfo(pg,user='nexloop_configurator'));dsn.chmod(0o600)
    def activate(model,dimension):
        with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as worker:
            session,_=seed_multi_authority(admin,worker,[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,Operation.READ)],identity_suffix=f'-idx-{dimension}',tenant=TENANT)
            return RecallIndexer(worker,session,provider=DeterministicTestEmbeddingProvider(dimension,model=model),expected_dimension=dimension).activate_profile()
    return dict(dsn=dsn,activate=activate,admin=admin,pg=pg,tmp=tmp_path)


def test_refused_until_the_calibrated_embedding_profile_is_active(deployment):
    d=deployment;manifest=M.load(MANIFEST)
    with pytest.raises(psycopg.Error,match='active recall profile'):M.apply(manifest,TENANT,database_url_file=d['dsn'])
    d['activate']('nexloop-test-ngram-v1',64)
    with pytest.raises(psycopg.Error,match='active recall profile'):M.apply(manifest,TENANT,database_url_file=d['dsn'])
    report=M.doctor(manifest,TENANT,database_url_file=d['dsn'])
    assert not report['in_sync'] and {f['kind'] for f in report['findings']}=={'embedding_profile_mismatch','no_active_configuration'}


def test_apply_publishes_once_reapply_noop_versions_immutable_and_rollback(deployment):
    d=deployment;manifest=M.load(MANIFEST)
    d['activate']('doubao-embedding-vision-251215',1024)
    first=M.apply(manifest,TENANT,database_url_file=d['dsn'])
    assert first['changed'] and first['config_version']=='nx045-glue-v1-doubao1024'
    assert M.apply(manifest,TENANT,database_url_file=d['dsn'])=={**first,'changed':False}
    assert M.doctor(manifest,TENANT,database_url_file=d['dsn'])=={'config_version':manifest['config_version'],'in_sync':True,'findings':[],'active':manifest['config_version']}
    row=d['admin'].execute("select weights,merge_threshold::float8,dedupe_threshold::float8,reject_cooldown_seconds,whitelist,calibration->>'evidence_class',calibration->'embedding_profile',published_by,active "
        "from ontology.nexloop_merge_configurations where tenant_id=%s",(TENANT,)).fetchall()
    assert row==[(manifest['weights'],0.35,0.35,2592000,[],'real',{'model':'doubao-embedding-vision-251215','dimension':1024},'nexloop_configurator',True)]
    tampered=copy.deepcopy(manifest);tampered['merge_threshold']=0.3
    with pytest.raises(psycopg.Error,match='immutable'):M.apply(tampered,TENANT,database_url_file=d['dsn'])
    # A new version activates; re-applying v1 rolls back to it without a new row.
    v2=copy.deepcopy(manifest);v2['config_version']='nx045-glue-v2-doubao1024';v2['reject_cooldown_seconds']=604800
    assert M.apply(v2,TENANT,database_url_file=d['dsn'])['changed']
    assert M.doctor(manifest,TENANT,database_url_file=d['dsn'])['findings']==[{'kind':'other_version_active','active':'nx045-glue-v2-doubao1024'}]
    assert M.apply(manifest,TENANT,database_url_file=d['dsn'])['changed']
    assert d['admin'].execute('select config_version from ontology.nexloop_merge_configurations where tenant_id=%s and active',(TENANT,)).fetchall()==[('nx045-glue-v1-doubao1024',)]
    assert d['admin'].execute('select count(*) from ontology.nexloop_merge_configurations where tenant_id=%s',(TENANT,)).fetchone()[0]==2


def test_only_the_trusted_configuration_identity_can_publish(deployment):
    d=deployment;manifest=M.load(MANIFEST);d['activate']('doubao-embedding-vision-251215',1024)
    api=d['tmp']/'api-dsn';api.write_text(make_conninfo(d['pg'],user='nexloop_api'));api.chmod(0o600)
    with pytest.raises(ValueError):M.apply(manifest,TENANT,database_url_file=api)
    admin_dsn=d['tmp']/'admin-dsn';admin_dsn.write_text(d['pg']);admin_dsn.chmod(0o600)
    with pytest.raises(ValueError):M.apply(manifest,TENANT,database_url_file=admin_dsn)
    with psycopg.connect(make_conninfo(d['pg'],user='nexloop_api')) as c,pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("select control.nexloop_publish_merge_configuration_manifest(%s,'{}')",(TENANT,))
    assert d['admin'].execute('select count(*) from ontology.nexloop_merge_configurations').fetchone()[0]==0


def test_cli_check_and_apply(deployment,capsys):
    d=deployment;d['activate']('doubao-embedding-vision-251215',1024)
    assert M.main(['--manifest',str(MANIFEST),'--check'])==0
    assert M.main(['--manifest',str(MANIFEST),'--tenant',TENANT,'--apply','--database-url-file',str(d['dsn'])])==0
    assert M.main(['--manifest',str(MANIFEST),'--tenant',TENANT,'--doctor','--database-url-file',str(d['dsn'])])==0
    out=capsys.readouterr().out
    assert '"in_sync": true' in out and str(d['dsn']) not in out
