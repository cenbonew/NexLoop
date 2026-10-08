from datetime import UTC,datetime,timedelta
import pytest
from nexloop_eios.service_offerings import assess_request_scope,json_export_example,offering_schemas

ID='a'*64
@pytest.fixture
def offering():return json_export_example(valid_until=(datetime.now(UTC)+timedelta(minutes=5)).isoformat())

def scope(**fields):return {'offering_id':ID,'offering_revision':1,'requested_guarantees':[],'requested_discounts':[],**fields}

def test_example_has_two_real_governed_types():
    assert {s.type_name for s in offering_schemas()}=={'ServiceOffering','ConsumerServiceOffering'}
    assert all(s.only_edit_via_actions for s in offering_schemas())

@pytest.mark.parametrize('requested_scope',[scope(requested_guarantees=['guaranteed-profit']),scope(requested_discounts=['50%-discount'])])
def test_outside_terms_refused_with_explicit_delivery_range(offering,requested_scope):
    result=assess_request_scope(offering,requested_scope,offering_id=ID,revision=1)
    assert result['allowed'] is False and result['reason']=='outside_catalog_terms'
    assert result['scope']['guarantees']==[] and result['scope']['discounts']==[]
    assert result['scope']['evidence_kind']=='fsynced_json_export'

def test_statement_is_never_interpreted_as_entitlement(offering):
    result=assess_request_scope(offering,scope(),offering_id=ID,revision=1)
    assert result['allowed'] is True
    with pytest.raises(Exception):assess_request_scope(offering,scope(message='promise discount'),offering_id=ID,revision=1)

def test_stale_revision_and_paused_offering_refuse(offering):
    with pytest.raises(ValueError):assess_request_scope(offering,scope(),offering_id=ID,revision=2)
    offering['active']=False
    with pytest.raises(ValueError):assess_request_scope(offering,scope(),offering_id=ID,revision=1)
