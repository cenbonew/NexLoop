"""Actual Node exec environment/selector; no provider request or EIOS permit."""
import json,os,subprocess,sys,shutil
from pathlib import Path
import pytest

# Works both in ignored candidate and after merging under repository tests/.
ROOT=next(parent for parent in Path(__file__).resolve().parents if (parent/'versions.lock.json').is_file())
CANDIDATE=Path(__file__).resolve().parents[1]
LAUNCHER=CANDIDATE/'scripts/agent_host.py' if (CANDIDATE/'scripts/agent_host.py').is_file() else ROOT/'scripts/agent_host.py'
TRUSTED=ROOT/'apps/agent-host/dist'

@pytest.fixture
def launch(tmp_path):
    node=shutil.which('node');assert node and subprocess.check_output([node,'--version'],text=True).startswith('v24.')
    app=tmp_path/'app';(app/'scripts').mkdir(parents=True)
    shutil.copyfile(LAUNCHER,app/'scripts/agent_host.py')
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    def private(name,value):
        p=tmp_path/name;p.write_text(value);p.chmod(0o600);return p
    key=private('model-key','')
    model=private('model-configuration',json.dumps({'MODEL_CREDENTIALS_FILE':str(key),'stage':False}))
    config=private('host-config',json.dumps({'runtime_profile':'deepseek-flash','model_configuration_file':str(model)}))
    # Test-owned executable entry only inspects actual frozen selector/auth;
    # it never streams a request or supplies an authorization callback.
    entry=app/'apps/agent-host/dist/main.js'
    entry.parent.mkdir(parents=True,exist_ok=True)
    (entry.parent/'package.json').write_text('{"type":"module"}')
    entry.write_text("import {readFileSync} from 'node:fs';\n"+
      f"import {{selectTrustedModel}} from '{(TRUSTED/'trusted-model-profile.js').as_uri()}';\n"+
      f"import {{readPrivateMaterial}} from '{(TRUSTED/'private-material.js').as_uri()}';\n"+
      "const runtime=JSON.parse(readFileSync(process.argv[8],'utf8'));const config=JSON.parse(readFileSync(runtime.model_configuration_file,'utf8'));\n"+
      "const environment={...config,MODEL_API_KEY:process.env.MODEL_API_KEY};const chosen=selectTrustedModel(environment,readPrivateMaterial,config.stage);const auth=await chosen.models.getAuth('deepseek');\n"+
      "console.log(JSON.stringify({profile:chosen.runtimeProfile,source:chosen.credentialSource,hasModelEnv:Object.hasOwn(process.env,'MODEL_API_KEY'),fileWins:auth?.auth.apiKey==='dummy-private-model',envUsed:auth?.auth.apiKey==='dummy-local-model',otherSecret:Object.keys(process.env).some(k=>['DATABASE_URL','DEEPSEEK_API_KEY','CHANNEL_TOKEN','SSLKEYLOGFILE'].includes(k))}));\n")
    args=[sys.executable,str(app/'scripts/agent_host.py'),'--node',node,'--runtime-root',str(runtime),'--internal-key-file',str(key),'--port','65431','--tls-certificate-file',str(key),'--tls-key-file',str(key),'--runtime-config-file',str(config)]
    def call(*,optin=False,stage=False,material='',public=False,empty_environment=False,config_fault=None):
        key.write_text(material);model.write_text(json.dumps({'MODEL_CREDENTIALS_FILE':str(key),'stage':stage}));model.chmod(0o644 if public else 0o600)
        if config_fault=='symlink':
            target=tmp_path/'real-config';model.rename(target);model.symlink_to(target)
        elif config_fault=='hardlink':os.link(model,tmp_path/'config-link')
        elif config_fault=='duplicate':model.write_text('{"stage":false,"stage":false}')
        env={'PATH' :os.environ['PATH'],'MODEL_API_KEY':'dummy-local-model','DATABASE_URL':'dummy-db-secret','DEEPSEEK_API_KEY':'dummy-ambient-secret','CHANNEL_TOKEN':'dummy-channel-secret','SSLKEYLOGFILE':'dummy-keylog'}
        if empty_environment:env['MODEL_API_KEY']=''
        result=subprocess.run(args+(['--allow-local-model-key'] if optin else []),capture_output=True,text=True,timeout=10,env=env)
        for secret in env.values():
            if secret.startswith('dummy-'):assert secret not in result.stdout+result.stderr
        return result
    yield call


def test_explicit_local_key_reaches_actual_selector_and_no_other_secret(launch):
    result=launch(optin=True);assert result.returncode==0
    body=json.loads(result.stdout);assert body=={'profile':'deepseek-flash','source':'environment','hasModelEnv':True,'fileWins':False,'envUsed':True,'otherSecret':False}


def test_no_optin_drops_model_key_and_uses_deterministic_fallback(launch):
    result=launch();assert result.returncode==0
    body=json.loads(result.stdout);assert body['profile']=='deterministic-test' and body['hasModelEnv'] is False and body['otherSecret'] is False


def test_stage_rejects_optin_and_still_uses_private_file_without_env(launch):
    rejected=launch(optin=True,stage=True,material='dummy-private-model');assert rejected.returncode==1 and rejected.stdout==''
    result=launch(stage=True,material='dummy-private-model');assert result.returncode==0
    body=json.loads(result.stdout);assert body['fileWins'] is True and body['source']=='secret_file' and body['hasModelEnv'] is False


def test_local_private_file_wins_over_explicit_environment(launch):
    result=launch(optin=True,material='dummy-private-model');assert result.returncode==0
    body=json.loads(result.stdout);assert body['fileWins'] is True and body['envUsed'] is False


def test_local_optin_requires_owned_private_config(launch):
    result=launch(optin=True,public=True);assert result.returncode==1 and result.stdout==''


@pytest.mark.parametrize('fault',['symlink','hardlink','duplicate'])
def test_local_optin_rejects_unsafe_or_ambiguous_configuration(launch,fault):
    result=launch(optin=True,config_fault=fault)
    assert result.returncode==1 and result.stdout==''


def test_explicit_local_empty_key_uses_deterministic_fallback(launch):
    result=launch(optin=True,empty_environment=True)
    assert result.returncode==0
    body=json.loads(result.stdout)
    assert body['profile']=='deterministic-test' and body['otherSecret'] is False
