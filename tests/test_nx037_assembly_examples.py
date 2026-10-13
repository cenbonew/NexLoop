"""NX-037 gap F: the public example configurations for the real tenant assembly are accepted by the validators the tools use.

Placeholders (<...>) are replaced with synthetic values here; the examples themselves carry no real value.
"""
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'deploy/examples/nx037'
HEX = 'a' * 64
TENANT = '00000000-0000-4000-8000-00000000000a'


def filled(name, **values):
    text = (EXAMPLES / name).read_text()
    for key, value in values.items():
        text = text.replace('<' + key + '>', value)
    assert '<' not in text, re.findall(r'<[A-Z_]+>', text)
    return json.loads(text)


def test_examples_hold_only_placeholders():
    for path in EXAMPLES.glob('*.json'):
        text = path.read_text()
        assert not re.search(r'\b\d{1,3}(\.\d{1,3}){3}\b', text), path.name
        assert '@' not in text.replace('<EMAIL>', ''), path.name


def test_business_and_catalog_recipes():
    from nexloop_eios.business_setup import strict_recipe as business
    from nexloop_eios.catalog_setup import strict_recipe as catalog
    end = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    assert business(json.dumps(filled('business-recipe.example.json', NEW_UUID=str(uuid.uuid4()), CONSUMER_PRINCIPAL_ID='principal-consumer',
                                      TRIAL_PERIOD_END_UTC_ISO=end)))['control']['budget_units'] == 30
    assert catalog(json.dumps(filled('catalog-recipe.example.json', NEW_UUID=str(uuid.uuid4()), CONSUMER_ID_FROM_BUSINESS_SETUP=HEX,
                                     TRIAL_PERIOD_END_UTC_ISO=end)))['consumer_id'] == HEX


def test_relay_run_profile_and_enrollment():
    from nexloop_eios.message_relay import validate_run_profile
    profile = filled('relay-run-profile.example.json', ROLE_REF='role:nx037', CONTEXT_MANIFEST_REF='artifact:nx037', PER_RUN_COST_CAP='2',
                     TRIAL_CURRENCY='CNY')
    assert validate_run_profile(profile)['runtime_profile'] == 'deepseek-flash'
    enrollment = filled('relay-enrollment.example.json', CONSUMER_ID_FROM_BUSINESS_SETUP=HEX, CONTROL_ID_FROM_BUSINESS_SETUP=HEX,
                        OFFERING_ID_FROM_CATALOG_SETUP=HEX, BINDING_ID_FROM_CATALOG_SETUP=HEX,
                        RELAY_SERVICE_PRINCIPAL_ID='relay-principal', NEW_UUID=str(uuid.uuid4()))
    assert set(enrollment) <= {'consumer_id', 'control_id', 'offering_id', 'binding_id', 'relay_principal', 'intent_id', 'role_id', 'link_id', 'step_id'}


def test_identity_manifest_and_workbench_members():
    from nexloop_eios.initial_identity import validate_identity_manifest
    from nexloop_eios import workbench_roles
    now = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    manifest = filled('identity-human.example.json', TENANT_ID=TENANT, BROWSER_APPLICATION_ID='nexloop-webchat', NEW_UUID=str(uuid.uuid4()),
                      OPERATOR_LABEL='explicit-technical-owner', SUBJECT_ID='human-1', NOW_UTC_ISO=now, PRINCIPAL_ID='principal-1',
                      LOCAL_ACCOUNT_ID='account-1', USERNAME='consumer-1', EMAIL='consumer@example.invalid')
    assert validate_identity_manifest(manifest)['membership']['principal_id'] == 'principal-1'
    roles = workbench_roles.validate_roles(json.loads((ROOT / 'deploy/authorization/workbench-roles.v1.json').read_text()))
    members = filled('workbench-members.example.json', TENANT_ID=TENANT, WORKBENCH_APPLICATION_ID='nexloop-workbench', OWNER_PRINCIPAL_ID='owner-1',
                     OWNER_SUBJECT_ID='human-owner', OPERATOR_PRINCIPAL_ID='operator-1', OPERATOR_SUBJECT_ID='human-operator')
    workbench_roles.validate_members(members, roles)


def test_enterprise_provider_example_matches_the_effect_worker_shape():
    from nexloop_eios.effect_provider import EffectProviderConfiguration
    body = filled('enterprise-provider.example.json', ENTERPRISE_ENDPOINT_HOST='endpoint.example.invalid', PORT='8443',
                  ABSOLUTE_PRIVATE_CREDENTIAL_FILE='/private/credential', ABSOLUTE_PRIVATE_CA_FILE='/private/ca.pem',
                  ENTERPRISE_ENDPOINT_IP_OR_DRILL_RELAY_LOOPBACK='127.0.0.1')
    assert set(body) == {'origin', 'credential_file', 'ca_file', 'timeout', 'connect_address'}
    assert EffectProviderConfiguration(**body).timeout == 2.0


def test_enterprise_slot_examples():
    """Enterprise delivery for one consumer: offering, control and the enrollment with the enterprise slot (ADR-030)."""
    from nexloop_eios import relay_enrollment, service_offerings
    end = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    offering = filled('enterprise-offering.example.json', SERVICE_CODE_SUFFIX='repair-visit', OFFERING_TITLE='上门维修预约',
                      CNY_PRICE_AMOUNT='0', TRIAL_PERIOD_END_UTC_ISO=end)
    assert re.fullmatch(r'enterprise\.[a-z0-9][a-z0-9.-]{0,62}', offering['service_code'])
    assert re.fullmatch(r'(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?', offering['price_amount'])
    example = getattr(service_offerings, 'enterprise_delivery_example', None)
    if example is not None:  # present once the enterprise Action (L2) is merged: the example is exactly its arguments
        assert service_offerings.delivery_scope(example(**offering))['delivery_action'] == 'nexloop.enterprise.deliver:1'
    control = filled('enterprise-control.example.json', CONSUMER_ID_FROM_BUSINESS_SETUP=HEX, EFFECT_EXECUTOR_PRINCIPAL_ID='executor',
                     TRIAL_PERIOD_END_UTC_ISO=end)
    assert set(control) == {'consumer_id', 'executor_principal', 'budget_units', 'allow_effect', 'valid_until'}
    assert type(control['budget_units']) is int and control['budget_units'] > 0 and control['allow_effect'] is True
    other = 'b' * 64
    enrollment = filled('relay-enrollment-enterprise.example.json', CONSUMER_ID_FROM_BUSINESS_SETUP=HEX, CONTROL_ID_FROM_BUSINESS_SETUP=HEX,
                        OFFERING_ID_FROM_CATALOG_SETUP=HEX, BINDING_ID_FROM_CATALOG_SETUP=HEX, RELAY_SERVICE_PRINCIPAL_ID='relay-principal',
                        NEW_UUID=str(uuid.uuid4()), ENTERPRISE_CONTROL_ID=other, ENTERPRISE_OFFERING_ID=other, ENTERPRISE_BINDING_ID=other)
    enterprise = enrollment.pop('enterprise')
    assert set(enterprise) == {'control_id', 'offering_id', 'binding_id'}
    relay_enrollment._ids(**{'enterprise_' + key: value for key, value in enterprise.items()})
    relay_enrollment._ids(**{key: enrollment[key] for key in ('consumer_id', 'control_id', 'offering_id', 'binding_id')})
    assert set(enrollment) == set(filled('relay-enrollment.example.json', CONSUMER_ID_FROM_BUSINESS_SETUP=HEX, CONTROL_ID_FROM_BUSINESS_SETUP=HEX,
                                         OFFERING_ID_FROM_CATALOG_SETUP=HEX, BINDING_ID_FROM_CATALOG_SETUP=HEX,
                                         RELAY_SERVICE_PRINCIPAL_ID='relay-principal', NEW_UUID=str(uuid.uuid4())))
    assert enterprise['control_id'] != enrollment['control_id']  # never the reply control
