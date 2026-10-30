"""Relay dependency expiry (L6 S43/S45, migration relay_dependency_expiry, alert rules v23) on real PostgreSQL, with the
relay multi-consumer fixture: an active enrollment whose dependencies expire within 7 days warns, an expired one is critical,
a tenant without any enrollment is visible (never a silent zero), and an enrolled consumer the relay idles past is counted."""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from psycopg.types.json import Jsonb

from nexloop_eios.observability import load_rules
from test_relay_multi_consumer_pg import multi  # noqa: F401
from message_runtime_fixture import message_plan  # noqa: F401
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'


def readiness(m):
    return m['admin'].execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'relay_readiness'", (m['tenant'],)).fetchone()[0]


def fired(snapshot):
    rules = load_rules(RULES / 'alert-rules.v23.json')['rules']
    out = set()
    for r in rules:
        block, _, key = r['metric'].partition('.')
        if block == 'relay_readiness' and snapshot[key] > r['threshold']:
            out.add(r['rule_id'])
    return out


def test_v23_adds_exactly_the_five_readiness_rules():
    v22, v23 = json.loads((RULES / 'alert-rules.v22.json').read_text()), load_rules(RULES / 'alert-rules.v23.json')
    assert v23['version'] == 23 and v23['rules'][:len(v22['rules'])] == v22['rules']
    added = {r['rule_id']: r for r in v23['rules'][len(v22['rules']):]}
    assert set(added) == {'relay_dependency_expiring', 'relay_dependency_expired', 'relay_unserviceable_enrollment', 'relay_no_active_enrollment',
                          'relay_stalled_consumers'}
    assert added['relay_dependency_expired']['severity'] == added['relay_unserviceable_enrollment']['severity'] == 'critical'
    assert added['relay_dependency_expiring']['severity'] == 'warning' and all(r['metric'].startswith('relay_readiness.') for r in added.values())


def test_no_enrollment_is_visible_then_expiry_warns_and_expired_is_critical(multi):
    m = multi
    empty = readiness(m)  # nothing enrolled yet: not a silent pass
    assert empty['active_enrollments'] == 0 and empty['no_active_enrollment'] == 1 and empty['earliest_valid_until'] is None
    assert fired(empty) == {'relay_no_active_enrollment'}
    assert m['enroll']('A')['state'] == 'active'
    soon = readiness(m)  # the fixture's control and offering expire in 5 minutes
    assert soon['active_enrollments'] == 1 and soon['no_active_enrollment'] == 0 and soon['unserviceable_enrollments'] == 0
    assert soon['dependencies_expired'] == 0 and soon['dependencies_expiring_7d'] >= 3  # control, its ledger row, offering
    assert datetime.fromisoformat(soon['earliest_valid_until'].replace('Z', '+00:00')) <= datetime.now(UTC) + timedelta(minutes=5)
    assert fired(soon) == {'relay_dependency_expiring'}
    # Renewed far out (as the 10-30 rebuild): nothing fires.
    admin, c = m['admin'], m['consumers']['A']
    far = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    for object_id in (c['control'], c['offering']):
        admin.execute("update ontology.objects set properties=properties||%s where object_id=%s", (Jsonb({'valid_until': far}), object_id))
    admin.execute('update control.nexloop_effect_control_ledger set valid_until=%s where control_id=%s', (far, c['control']))
    renewed = readiness(m)
    assert renewed['dependencies_expiring_7d'] == renewed['dependencies_expired'] == 0 and 'relay_dependency_expiring' not in fired(renewed)
    # The 10-22 failure: the control expired. The profile turns null and both critical rules fire.
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    admin.execute("update ontology.objects set properties=properties||%s where object_id=%s", (Jsonb({'valid_until': past}), c['control']))
    expired = readiness(m)
    assert expired['dependencies_expired'] == 1 and expired['unserviceable_enrollments'] == 1
    assert {'relay_dependency_expired', 'relay_unserviceable_enrollment'} <= fired(expired)
    # A named dependency that no longer exists counts as expired.
    admin.execute("update ontology.objects set type_name='Gone' where object_id=%s", (c['offering'],))
    assert readiness(m)['dependencies_expired'] == 2


def test_an_enrolled_consumer_the_relay_idles_past_is_stalled(multi):
    m = multi
    assert m['enroll']('B')['state'] == 'active'
    assert readiness(m)['stalled_consumers'] == 0  # B's message is fresh
    admin, c = m['admin'], m['consumers']['B']
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    admin.execute("update runtime.nexloop_message_outbox set record=jsonb_set(record,'{accepted_at}',to_jsonb(%s::text)) where message_id=any(%s)", (old, c['messages']))
    stalled = readiness(m)
    assert stalled['stalled_consumers'] == 1 and 'relay_stalled_consumers' in fired(stalled)
    # A lease that ended long ago does not count; one taken in the last 5 minutes means the relay is working on it.
    admin.execute("insert into runtime.nexloop_message_relay_leases select tenant_id,world,message_id,%s,'p','d',1,clock_timestamp()-interval '10 minutes' "
                  "from runtime.nexloop_message_outbox where message_id=%s", (c['consumer'], c['messages'][0]))
    assert readiness(m)['stalled_consumers'] == 1
    admin.execute("update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp()+interval '30 seconds' where message_id=%s", (c['messages'][0],))
    assert readiness(m)['stalled_consumers'] == 0
