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
    """Enterprise delivery for one consumer (ADR-030): the nexloop-enterprise-setup recipe and the enrollment file with the
    enterprise keys, parsed by the commands' own parsers."""
    from nexloop_eios import enterprise_setup, relay_enrollment
    end = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    recipe = filled('enterprise-setup-recipe.example.json', NEW_UUID=str(uuid.uuid4()), CONSUMER_ID_FROM_BUSINESS_SETUP=HEX,
                    SERVICE_CODE_SUFFIX='repair-visit', OFFERING_TITLE='上门维修预约', CNY_PRICE_AMOUNT='0', TRIAL_PERIOD_END_UTC_ISO=end)
    parsed = enterprise_setup.strict_recipe(json.dumps(recipe))
    assert parsed['offering']['service_code'] == 'enterprise.repair-visit' and parsed['control']['budget_units'] == 30
    other = 'b' * 64
    enrollment = filled('relay-enrollment-enterprise.example.json', CONSUMER_ID_FROM_BUSINESS_SETUP=HEX, CONTROL_ID_FROM_BUSINESS_SETUP=HEX,
                        OFFERING_ID_FROM_CATALOG_SETUP=HEX, BINDING_ID_FROM_CATALOG_SETUP=HEX, RELAY_SERVICE_PRINCIPAL_ID='relay-principal',
                        NEW_UUID=str(uuid.uuid4()), ENTERPRISE_CONTROL_ID_FROM_ENTERPRISE_SETUP=other,
                        ENTERPRISE_OFFERING_ID_FROM_ENTERPRISE_SETUP=other, ENTERPRISE_BINDING_ID_FROM_ENTERPRISE_SETUP=other)
    slot = {relay_enrollment.ENTERPRISE_FILE_KEYS[key]: enrollment.pop(key) for key in relay_enrollment.ENTERPRISE_FILE_KEYS}
    relay_enrollment._ids(**{'enterprise_' + key: value for key, value in slot.items()})
    relay_enrollment._ids(**{key: enrollment[key] for key in ('consumer_id', 'control_id', 'offering_id', 'binding_id')})
    assert relay_enrollment._INTENT.fullmatch(enrollment['intent_id'])
    assert slot['control_id'] != enrollment['control_id']  # never the reply control
    # Without the enterprise keys it is exactly the plain enrollment file: the clearing file.
    assert set(enrollment) == set(filled('relay-enrollment.example.json', CONSUMER_ID_FROM_BUSINESS_SETUP=HEX, CONTROL_ID_FROM_BUSINESS_SETUP=HEX,
                                         OFFERING_ID_FROM_CATALOG_SETUP=HEX, BINDING_ID_FROM_CATALOG_SETUP=HEX,
                                         RELAY_SERVICE_PRINCIPAL_ID='relay-principal', NEW_UUID=str(uuid.uuid4())))


def test_trial_consent_examples_fit_the_start_guard(tmp_path):
    """D3 (owner 2026-10-14): the owner's authorization, the agent participants and the owner as the only staff member, and the
    plan's consumers, filled with synthetic values, pass the run entry's guard."""
    import hashlib
    import sys
    sys.path.insert(0, str(ROOT / 'scripts/nx037'))
    import trial_guards
    authorization = tmp_path / 'authorization'
    authorization.write_text('synthetic owner authorization')
    authorization.chmod(0o600)
    digest = hashlib.sha256(authorization.read_bytes()).hexdigest()
    ids = {f'{n}_ID': hashlib.sha256(n.encode()).hexdigest() for n in ('CUSTOMER_1', 'CUSTOMER_2', 'CUSTOMER_3', 'CUSTOMER_4', 'REFUSAL_1', 'REFUSAL_2')}
    trial = filled('trial-participants.example.json', AUTHORIZATION_SHA256=digest, AGENT_TOOL='Claude Code via browser', **ids)
    assert sum(p['kind'] == 'human' and p == {'kind': 'human', 'role': 'staff'} for p in trial['participants']) == 1
    # The v3 plan with its placeholders replaced by the same ids passes the guard: every planned account is an agent participant.
    plan_text = (ROOT / 'deploy/configuration/nx037-interaction-plan.v3.json').read_text()
    for name, value in ids.items():
        plan_text = plan_text.replace('<' + name.removesuffix('_ID') + '>', value)
    plan = json.loads(plan_text)
    failed, recorded = trial_guards.consent(trial, {'consumers': sorted({i['consumer'] for i in plan['order']})}, authorization)
    assert failed == [] and len(recorded) == 6 and {r['persona'] for r in recorded} == {'simulated_agent'}
    assert filled('interaction-plan-consumers.example.json', CONSUMER_ID=HEX) == {'note': filled('interaction-plan-consumers.example.json', CONSUMER_ID=HEX)['note'],
                                                                                'consumers': ['consumer:' + HEX]}


def test_the_operator_authorization_template_names_the_scope_and_holds_only_placeholders():
    text = (EXAMPLES / 'operator-authorization.example.txt').read_text()
    for point in ('nexloop.nx037-interaction-plan.v3', 'nexloop.nx037-trial.v3', '累计 100 CNY', '单 Run 2 CNY', '扮演客户', '客户为模拟、系统为真实'):
        assert point in text, point
    assert not re.search(r'\b\d{1,3}(\.\d{1,3}){3}\b', text) and '@' not in text
    assert set(re.findall(r'<([A-Z0-9_]+)>', text)) == {'OWNER_REFERENCE', 'TENANT_ID', 'TRIAL_UUID', 'PERIOD_START', 'PERIOD_END', 'AGENT_TOOLS', 'PLAN_SHA256', 'DATE'}
