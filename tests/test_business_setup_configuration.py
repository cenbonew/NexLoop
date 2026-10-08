"""Offline production recipe validation, no fabricated business authority."""
from datetime import UTC,datetime,timedelta
import json,uuid
import pytest
from nexloop_eios.business_setup import strict_recipe,BusinessSetupUnavailable,main


def recipe():
    return {'schema_version':'1.0','request_id':str(uuid.uuid4()),'human_principal_id':'canonical-human-principal',
        'control':{'budget_units':1,'valid_until':(datetime.now(UTC)+timedelta(seconds=150)).isoformat()}}


def test_valid_explicit_bounded_recipe():
    value=recipe();assert strict_recipe(json.dumps(value))==value

@pytest.mark.parametrize('field,value',[('request_id',None),('request_id','nonuuid'),('human_principal_id','browser selected / human'),
    ('schema_version','9.0'),('human_principal_id',True),('control',None)])
def test_invalid_top_level_recipe_fixed_error(field,value):
    body=recipe();body[field]=value
    with pytest.raises(BusinessSetupUnavailable,match='^business_setup_unavailable$') as error:strict_recipe(json.dumps(body))
    assert error.value.__suppress_context__

@pytest.mark.parametrize('field,value',[('budget_units',True),('budget_units',0),('budget_units',1000001),('budget_units',1.5),
    ('valid_until',None),('valid_until','2020-01-01T00:00:00+00:00'),('valid_until','2099-01-01T00:00:00+08:00')])
def test_invalid_control_recipe(field,value):
    body=recipe();body['control'][field]=value
    with pytest.raises(BusinessSetupUnavailable):strict_recipe(json.dumps(body))

@pytest.mark.parametrize('text',['{"request_id":"first","request_id":"second"}', '{"budget_units":NaN}', '[]'])
def test_corrupt_json_no_native_details(text):
    with pytest.raises(BusinessSetupUnavailable,match='^business_setup_unavailable$'):strict_recipe(text)


def test_recipe_cannot_supply_identity_authority_or_world():
    for field,value in [('tenant_id','caller-tenant'),('world','shadow'),('owner_principal','caller-owner'),('allow_effect',False),('allowed_resources',['arbitrary'])]:
        body=recipe();body[field]=value
        with pytest.raises(BusinessSetupUnavailable):strict_recipe(json.dumps(body))


def test_cli_invalid_private_recipe_fails_before_dsn_read(tmp_path,capsys):
    path=tmp_path/'recipe';path.write_text('{"private_sentinel":"synthetic do not echo"}');path.chmod(0o600)
    argv=[]
    for name in ('database-url-file','signing-key-file','owner-credential-file','executor-credential-file','artifact-root'):
        argv+=['--'+name,str(tmp_path/'does-not-exist')]
    assert main(argv+['--recipe-file',str(path)])==1
    result=capsys.readouterr();assert result.out=='' and result.err=='Business setup unavailable\n'


def test_parser_never_echoes_unknown_synthetic_value(capsys):
    with pytest.raises(SystemExit) as error:main(['--synthetic-invalid','private-sentinel'])
    assert error.value.code==2
    assert capsys.readouterr().err=='Business setup configuration unavailable\n'
