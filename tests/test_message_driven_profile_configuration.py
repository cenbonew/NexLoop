"""Actual candidate compiled Host rejects unsafe opt-in before any provider IO."""
import json,shutil,subprocess
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('extra',[
    {'deterministic_message_from_input':False},
    {'deterministic_message_from_input':'true'},
    {'deterministic_message_from_input':1},
    {'deterministic_message_from_input':None},
    {'deterministic_message_from_input':True,'effect_tools':False},
    {'deterministic_message_from_input':True,'deterministic_effect_message':'private fixed synthetic message'},
    {'deterministic_message_from_input':True,'runtime_profile':'deepseek-flash','model_configuration_file':'/unused/private','maximum_request_cost':'1'},
])
def test_opt_in_is_true_test_only_tool_only_and_exclusive(tmp_path,extra):
    config=tmp_path/'configuration.json'
    config.write_text(json.dumps({'runtime_profile':'deterministic-test','guard_url':'https://127.0.0.1:9999/internal/v1/runtime/authorize',
        'guard_ca_file':str(tmp_path/'unused-ca'),'guard_key_file':str(tmp_path/'unused-key'),'effect_tools':True,**extra}));config.chmod(0o600)
    program='''import {readFileSync} from 'node:fs';
import {RuntimeHost} from './apps/agent-host/dist/runtime-host.js';
try{new RuntimeHost(process.argv[2],process.argv[1],path=>readFileSync(path),()=>{});process.exit(2);}
catch(error){if(!(error instanceof Error)||error.message!=='runtime configuration refused')process.exit(3);console.log('runtime_configuration_refused');}
'''
    node=shutil.which('node');assert node
    result=subprocess.run([node,'--disable-warning=ExperimentalWarning','--input-type=module','-e',program,str(config),str(tmp_path)],cwd=ROOT,capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and result.stdout=='runtime_configuration_refused\n' and result.stderr==''
