"""Offline strict configuration proofs only; no substituted authority ports."""
from datetime import UTC,datetime,timedelta
import pytest
from nexloop_eios.message_relay import message_assignment_schema,validate_recipe
from nexloop_eios.message_relay_cli import _recipe,arguments


def recipe():
    return {'consumer_id':'a'*64,'control_id':'b'*64,'control_revision':1,'consumer_revision':1,
        'valid_until':(datetime.now(UTC)+timedelta(seconds=100)).isoformat(),'role_ref':'role:operations',
        'context_manifest_ref':'context:relay','runtime_profile':'pi-durable',
        'budget':{'maximum_model_turns':2,'maximum_tool_calls':4,'active_timeout_seconds':30,'maximum_cost':'0','currency':'USD'},
        'runtime_owner_epoch':1,'queue':'operations'}


def test_actual_formal_assignment_schema_and_recipe_snapshot():
    schema=message_assignment_schema()
    assert schema.only_edit_via_actions and schema.type_name=='MessageAssignment'
    config=recipe();snapshot=validate_recipe(config);config['budget']['maximum_tool_calls']=100
    assert snapshot['budget']['maximum_tool_calls']==4

@pytest.mark.parametrize('field,value',[('consumer_id','browser-choice'),('control_revision',True),
    ('runtime_owner_epoch',0),('budget',{'maximum_tool_calls':999}),('role_ref','unqualified'),('queue','../queue')])
def test_invalid_recipe_rejected_without_claim(field,value):
    config=recipe();config[field]=value
    with pytest.raises(Exception):validate_recipe(config)


def test_private_recipe_duplicate_keys_rejected(tmp_path):
    path=tmp_path/'recipe';path.write_text('{"consumer_id":"first","consumer_id":"second"}');path.chmod(0o600)
    with pytest.raises(ValueError):_recipe(path)

@pytest.mark.parametrize('value',['nan','inf','0','-1','31'])
def test_cli_tick_configuration_failclosed(value,capsys):
    argv=[]
    for name in ('database-url-file','signing-key-file','route-credential-file','source-credential-file',
        'planner-credential-file','executor-credential-file','artifact-root','vault-root','recipe-file'):
        argv.extend(['--'+name,'/synthetic/private'])
    with pytest.raises(SystemExit) as error:arguments(argv+['--tick-seconds',value])
    assert error.value.code==2
    assert capsys.readouterr().err=='Message relay configuration unavailable\n'
