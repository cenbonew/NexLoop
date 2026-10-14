"""NX-037 real-mode start guards (scripts/nx037/trial_guards.py): each guard passes on a good setup and refuses each
counter-example. D3 consent, the trial window, the enterprise endpoint conformance, the WebChat reply delivery and the outbound
recorder backlog (0480, clean catalog PostgreSQL); and the counted cost (the tenant's actual increase over an interaction).
Synthetic data; the reference endpoint stands for both HTTP services (it speaks the same protocol)."""
import hashlib
import json
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from test_reference_endpoint import private, running
from test_local_json_delivery_pg import delivery_plan  # noqa: F401
from test_business_setup_pg import business_plan  # noqa: F401
from test_trusted_configuration_pg import configured  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import trial_guards as G  # noqa: E402

A, B = 'consumer:' + 'a' * 64, 'consumer:' + 'b' * 64


def consent_setup(tmp_path):
    files, participants = {}, []
    for ref in (A, B):
        signed = private(tmp_path / f'consent-{ref[-4:]}.pdf', f'synthetic signed consent {secrets.token_hex(8)}')
        files[ref] = str(signed)
        participants.append({'consumer_ref': ref, 'consent_sha256': hashlib.sha256(signed.read_bytes()).hexdigest()})
    mapping = private(tmp_path / 'consent-files.json', json.dumps(files))
    return {'participants': participants}, {'consumers': [A, B]}, mapping, files


def test_consent_passes_with_every_planned_consumer_registered_private_and_matching(tmp_path):
    trial, plan, mapping, _ = consent_setup(tmp_path)
    failed, recorded = G.consent(trial, plan, mapping)
    assert failed == [] and recorded == [{'consumer_id': 'a' * 64, 'consent_sha256': trial['participants'][0]['consent_sha256']},
                                         {'consumer_id': 'b' * 64, 'consent_sha256': trial['participants'][1]['consent_sha256']}]
    out = tmp_path / 'trial'
    G.record_consent(out, recorded)
    assert json.loads((out / 'consent.json').read_text()) == {'participants': recorded} and (out / 'consent.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('case', ['no_files_option', 'files_map_not_private', 'file_absent', 'file_not_private', 'digest_differs',
                                  'consumer_not_registered', 'plan_without_consumers', 'trial_without_participants', 'not_a_consumer_ref'])
def test_consent_refuses_each_missing_item(tmp_path, case):
    trial, plan, mapping, files = consent_setup(tmp_path)
    if case == 'no_files_option':
        mapping = None
    elif case == 'files_map_not_private':
        mapping.chmod(0o644)
    elif case == 'file_absent':
        Path(files[B]).unlink()
    elif case == 'file_not_private':
        Path(files[B]).chmod(0o640)
    elif case == 'digest_differs':
        trial['participants'][1]['consent_sha256'] = 'c' * 64
    elif case == 'consumer_not_registered':
        trial['participants'] = trial['participants'][:1]
    elif case == 'plan_without_consumers':
        plan = {'consumers': None}
    elif case == 'trial_without_participants':
        trial = {}
    elif case == 'not_a_consumer_ref':
        plan = {'consumers': ['owner-principal-synthetic']}
    assert G.consent(trial, plan, mapping) == (['consent_missing'], [])


def test_the_trial_window_is_local_time_and_refuses_outside_it():
    trial = {'plan': {'window': {'start': '10:00', 'end': '18:00'}}}
    shanghai = lambda h, m=0: datetime(2026, 10, 14, h, m, tzinfo=UTC) - timedelta(hours=8)  # noqa: E731 - local Asia/Shanghai
    assert G.window(trial, shanghai(10)) == [] and G.window(trial, shanghai(17, 59)) == []
    assert G.window(trial, shanghai(9, 59)) == ['outside_trial_window'] and G.window(trial, shanghai(18)) == ['outside_trial_window']
    assert G.window({'plan': {'window': {'start': '10:00', 'end': '18:00', 'timezone': 'UTC'}}}, datetime(2026, 10, 14, 11, tzinfo=UTC)) == []
    assert G.window({'plan': {}}, shanghai(12)) == ['outside_trial_window']  # no window configured: never open


def provider_file(tmp_path, e, credential=None, name='provider.json'):
    credential = credential or e['credential']
    return private(tmp_path / name, json.dumps({'origin': e['origin'], 'credential_file': str(credential), 'ca_file': str(e['cert']), 'timeout': 3}))


def test_enterprise_endpoint_conformance_passes_and_refuses_a_failing_endpoint(tmp_path):
    with running(tmp_path) as e:
        out = tmp_path / 'trial'
        assert G.enterprise_endpoint(provider_file(tmp_path, e), None, out) == []
        assert json.loads((out / 'preflight/endpoint-conformance.json').read_text())['ok'] is True
        wrong = private(tmp_path / 'wrong-credential', secrets.token_urlsafe(32))
        assert G.enterprise_endpoint(provider_file(tmp_path, e, wrong, 'wrong.json'), None, out) == ['enterprise_endpoint_nonconformant']
    assert G.enterprise_endpoint(tmp_path / 'absent.json', None, tmp_path / 'trial') == ['enterprise_endpoint_nonconformant']
    (tmp_path / 'down').mkdir()
    with running(tmp_path / 'down') as e:
        stopped = provider_file(tmp_path, e, name='stopped.json')
    assert G.enterprise_endpoint(stopped, None, tmp_path / 'trial') == ['enterprise_endpoint_nonconformant']  # not listening any more


def delivery_provider_file(tmp_path, config, credential=None, name='webchat-provider.json'):
    return private(tmp_path / name, json.dumps({'origin': config.origin, 'credential_file': str(credential or config.credential_file),
                                                'ca_file': str(config.certificate_file), 'timeout': 3}))


def test_webchat_delivery_health_through_the_actual_delivery_service(delivery_plan, tmp_path):  # noqa: F811
    """The reply delivery (local_json_delivery) answers GET /v1/health 200 {state: ok} with its executor authority current."""
    from test_local_json_delivery_pg import live_delivery
    f = delivery_plan
    with live_delivery(f):
        assert G.webchat_delivery(delivery_provider_file(tmp_path, f['server_config'])) == []
        wrong = private(tmp_path / 'wrong-credential', secrets.token_urlsafe(32))
        assert G.webchat_delivery(delivery_provider_file(tmp_path, f['server_config'], wrong, 'wrong.json')) == ['webchat_delivery_unavailable']
        public = tmp_path / 'public.json'
        public.write_text(delivery_provider_file(tmp_path, f['server_config'], name='p.json').read_text())
        public.chmod(0o644)
        assert G.webchat_delivery(public) == ['webchat_delivery_unavailable']  # the provider file must be private
    assert G.webchat_delivery(delivery_provider_file(tmp_path, f['server_config'], name='stopped.json')) == ['webchat_delivery_unavailable']


def test_webchat_delivery_health_refuses_a_service_without_its_backend(tmp_path):
    """TLS and the bearer accepted, but no executor authority behind it (no database): 503, refused."""
    from test_local_json_delivery_transport import delivery_transport
    import json as _json
    with delivery_transport(tmp_path) as (client, key, root):
        origin = str(client.base_url).rstrip('/')
        provider = private(tmp_path / 'provider.json', _json.dumps({'origin': origin, 'credential_file': str(key), 'ca_file': str(tmp_path / 'host-cert.pem'), 'timeout': 3}))
        assert G.webchat_delivery(provider) == ['webchat_delivery_unavailable']
        assert client.get('/v1/health', headers={'Authorization': 'Bearer ' + key.read_text()}).status_code == 503 and list(root.iterdir()) == []


def test_a_counted_cost_takes_the_actual_increase_over_the_interaction():
    """Fallback, plan and role Runs spend outside the trigger Run: the tenant's month cost rises more than the trigger Run's cost.
    The record keeps both; the cumulative cap counts the larger, so a trial pauses on what was actually spent."""
    from nexloop_eios import controlled_interactions as X
    from nexloop_eios.interaction_drivers import SyntheticCollector, SyntheticDriver
    import tempfile
    trial = X.load_plan(ROOT / 'deploy/configuration/nx037-trial.v2.json')
    order = json.loads((ROOT / 'deploy/configuration/nx037-interaction-plan.v2.json').read_text())['order']
    month = {'value': Decimal(0)}

    def month_cost():
        month['value'] += Decimal('20')  # read before and after each interaction: +40 per interaction, all outside the trigger Run
        return month['value']
    driver = SyntheticDriver(tenant='00000000-0000-4000-8000-00000000000a', other_tenant='00000000-0000-4000-8000-00000000000c')
    with tempfile.TemporaryDirectory() as out:
        state = X.run(trial, driver, SyntheticCollector(driver), out, trial_id=str(uuid.uuid4()), order=order, month_cost=month_cost)
        records = X.read_records(out)
    assert records[0]['cost']['period_increase'] == '20' and records[0]['cost']['counted'] == '20' and Decimal(records[0]['cost']['total']) < 20
    # 80 % of the 100 CNY cumulative cap is reached at the 4th interaction (4 x 20), although every trigger Run cost far less.
    assert state['status'] == 'paused' and state['rules'] == ['cumulative_cost'] and state['at_seq'] == 4 and Decimal(state['cumulative_cost']) == 80
    assert X.counted_cost({'cost': {'total': '1.5'}}) == Decimal('1.5')  # records without the increase count the trigger Run


def test_the_outbound_recorder_guard_refuses_a_stalled_backlog(commitments, admin):  # noqa: F811
    from commitment_fixture import TENANT
    c = commitments
    assert G.outbound_recorder(admin, TENANT) == []
    entry = c.outbound('合成：已投递但未写入会话')
    def age(seconds, unrecorded=False):
        # Test seed of the ledger state (the recorder has not written this delivered reply yet); the transition guard is bypassed
        # for this seed only.
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute("update runtime.nexloop_outbound_messages set delivery_changed_at=clock_timestamp()-make_interval(secs=>%s)"
                          + (",message_id=null,sequence=null,materialized_at=null" if unrecorded else "") + " where intent_id=%s",
                          (seconds, entry['intent_id']))
    age(30, unrecorded=True)
    assert G.outbound_recorder(admin, TENANT) == []  # waiting 30 s: still within the recorder's pace
    age(90)
    assert G.outbound_recorder(admin, TENANT) == ['outbound_recorder_stalled']
    backlog = admin.execute("select authz.nexloop_nx037_outbound_backlog(%s,'real')", (TENANT,)).fetchone()[0]
    assert backlog['pending'] == 1 and backlog['stalled'] == 1 and backlog['oldest_age_seconds'] >= 90


from commitment_fixture import commitments  # noqa: E402,F401
from effect_execution_fixture import execution_plan, governed_effect_executor  # noqa: E402,F401
test_the_outbound_recorder_guard_refuses_a_stalled_backlog = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)(
    test_the_outbound_recorder_guard_refuses_a_stalled_backlog)
