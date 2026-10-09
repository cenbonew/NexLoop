"""Explicit Host constructor mode, not an inferred mode from arbitrary input."""
import json,shutil,subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
@pytest.mark.parametrize('extra',[
 {'context_input_protocol':None},
 {'context_input_protocol':True},
 # v3 (0063 Role) and v4 (relationship) are explicit supported wires; an unknown one is refused.
 {'context_input_protocol':'nexloop.context-pack.v9'},
 {'context_input_protocol':'nexloop.context-pack.v2','deterministic_message_from_input':False},
 {'context_input_protocol':'nexloop.context-pack.v2','deterministic_message_from_input':None},
 {'context_input_protocol':'nexloop.context-pack.v2','effect_tools':False},
 {'context_input_protocol':'nexloop.context-pack.v2','deterministic_effect_message':'legacy fixed'},
 {'context_input_protocol':'nexloop.context-pack.v2','runtime_profile':'deepseek-flash','model_configuration_file':'/unused/private-model','maximum_request_cost':'1'},
])
def test_context_mode_is_explicit_strict_and_synthetic_only(tmp_path,extra):
    config=tmp_path/'config.json';config.write_text(json.dumps({'runtime_profile':'deterministic-test','guard_url':'https://127.0.0.1:9999/internal/v1/runtime/authorize',
     'guard_ca_file':str(tmp_path/'unused-ca'),'guard_key_file':str(tmp_path/'unused-key'),'effect_tools':True,'deterministic_message_from_input':True,**extra}));config.chmod(0o600)
    script="""import {readFileSync} from 'node:fs';
import {RuntimeHost} from './apps/agent-host/dist/runtime-host.js';
try{new RuntimeHost(process.argv[2],process.argv[1],path=>readFileSync(path),()=>{});process.exit(2);}
catch(error){if(error.message!=='runtime configuration refused')process.exit(3);console.log('runtime_configuration_refused');}
"""
    result=subprocess.run([shutil.which('node'),'--disable-warning=ExperimentalWarning','--input-type=module','-e',script,str(config),str(tmp_path)],cwd=ROOT,capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and result.stdout=='runtime_configuration_refused\n' and result.stderr==''
