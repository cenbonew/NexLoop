"""Pure typed configuration tests; not a governed business success substitute."""
from datetime import UTC,datetime,timedelta
import json,uuid
import pytest
from nexloop_eios.catalog_setup import strict_recipe,CatalogSetupUnavailable,main

def recipe():return {'schema_version':'1.0','request_id':str(uuid.uuid4()),'consumer_id':'a'*64,'valid_until':(datetime.now(UTC)+timedelta(minutes=5)).isoformat()}
def test_exact_recipe_retains_stable_inputs():
    value=recipe();assert strict_recipe(json.dumps(value))==value
@pytest.mark.parametrize('kind',['extra','missing','uuid','bool','consumer','world','expired','offset','nan','date','duplicate','bytes'])
def test_rejects_malformed_recipe(kind):
    value=recipe()
    if kind=='extra':value['grant']=True
    elif kind=='missing':del value['consumer_id']
    elif kind=='uuid':value['request_id']=uuid.uuid4().hex
    elif kind=='bool':value['consumer_id']=True
    elif kind=='consumer':value['consumer_id']='a'*63
    elif kind=='world':value['world']='shadow'
    elif kind=='expired':value['valid_until']='2000-01-01T00:00:00Z'
    elif kind=='offset':value['valid_until']='2030-01-01T00:00:00+08:00'
    elif kind=='date':value['valid_until']='2030-02-31T00:00:00Z'
    raw=json.dumps(value)
    if kind=='nan':raw=raw.replace('"1.0"','NaN')
    elif kind=='duplicate':raw=raw[:-1]+',"consumer_id":"'+'b'*64+'"}'
    elif kind=='bytes':raw+=' '*32768
    with pytest.raises(CatalogSetupUnavailable):strict_recipe(raw)
def test_argparse_never_repeats_unknown_credential(capsys):
    with pytest.raises(SystemExit):main(['--SYNTHETIC_PRIVATE_CREDENTIAL'])
    assert 'SYNTHETIC_PRIVATE_CREDENTIAL' not in capsys.readouterr().err
