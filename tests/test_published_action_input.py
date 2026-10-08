"""Actual published contracts must reject nested invalid inputs before PG claims."""
import pytest
from test_action_definitions import published_action
from test_postgres_action_claims import governance_inputs
from nexloop_eios.action_governor import govern_published_action
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from eios.actions.models import canonical_request_digest

@pytest.mark.parametrize('published_action',['strict-input'],indirect=True)
@pytest.mark.parametrize('invalid',[
 {'preference':'synthetic-private-rejected-value'}, {'preference':42},
 {}, {'preference':'service','unknown':'synthetic-private-rejected-value'},
])
def test_nested_published_input_denied_without_claim_or_business_write(published_action,admin,invalid):
    reader,definition,_=published_action
    payload={'request_id':'synthetic-nested-input','type_name':'Consumer','properties':invalid}
    command=governance_inputs()['claim_request']
    command=command.model_copy(update={'binding':command.binding.model_copy(update={
      'action_reference':definition.reference(),'request_digest':canonical_request_digest(payload)})})
    with pytest.raises(ActionAuthorizationDenied,match='^published Action input rejected$') as error:
        govern_published_action(reader.pool,reader.session,reader.signer,claim_request=command,request=payload)
    assert 'synthetic-private-rejected-value' not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==0
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0

@pytest.mark.parametrize('published_action',['strict-input'],indirect=True)
def test_valid_nested_contract_real_create_and_replay(published_action,admin):
    reader,_,_=published_action
    creator=GovernedObjectCreator(reader.pool,reader.session,reader.signer)
    args=dict(action_name='Consumer.create',action_version=1,intent_id='synthetic-valid-nested',type_name='Consumer',properties={'preference':'service'})
    receipt=creator.create(**args)
    assert creator.create(**args)==receipt
    assert admin.execute('select properties from ontology.objects').fetchone()[0]=={'preference':'service'}
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==1

@pytest.mark.parametrize('published_action',['external-input'],indirect=True)
def test_external_schema_reference_fails_closed_without_network(published_action,admin,monkeypatch):
    import urllib.request
    attempted=[]
    def forbidden(*args,**kwargs):
        attempted.append(True)
        raise AssertionError('schema network retrieval forbidden')
    monkeypatch.setattr(urllib.request,'urlopen',forbidden)
    reader,definition,_=published_action;inputs=governance_inputs();command=inputs['claim_request']
    command=command.model_copy(update={'binding':command.binding.model_copy(update={'action_reference':definition.reference()})})
    with pytest.raises(ActionAuthorizationDenied,match='^published Action input rejected$'):
        govern_published_action(reader.pool,reader.session,reader.signer,claim_request=command,request=inputs['request'])
    assert attempted==[]
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==0
