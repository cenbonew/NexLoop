"""NX-038 P5 route A: the synthetic fixture type is published through the governed Schema proposal path only (NX-040 F3: a frozen
proposal, published by a human reviewer), never by a direct catalog write - local test database only. add_object_type creates the
type with one marker property (0416 admits no empty type), add_optional_property adds each fixture property with its display name (F or T). The published type
has no object and no Action (A2: no change_scope binds it, so the NX-068 gate and F3 successors stay empty)."""
import pytest

from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_claim_matching_pg import env  # noqa: F401
from test_nx040_schema_publication_pg import port, proposal
from test_p5_synthetic_fixture_pg import CROWD, F, FIXTURE, T, TARGET
from test_review_decisions_pg import TENANT, review  # noqa: F401

pytestmark = pytest.mark.parametrize('env', [TENANT], indirect=True)


def test_the_fixture_is_published_through_proposals_by_a_person_and_binds_nothing(review):  # noqa: F811
    f = review
    admin, tenant = f['admin'], f['tenant']
    pid = proposal(f, 'add_object_type', FIXTURE, {'properties': [{'property_name': 'fixture_marker', 'value_type': 'string'}]})  # as s6_stage_schema FIXTURE_CHANGE
    out = port(f).publish(pid, rationale='P5 合成验收夹具（synthetic）', idempotency_key='p5-fixture-type-0001')
    assert out['published'] is True and out['version_after'] == 1 and out['successor_actions'] == []
    for i, (name, display) in enumerate([(n, F) for n in CROWD] + [(TARGET, T)]):
        pid = proposal(f, 'add_optional_property', FIXTURE, {'property_name': name, 'value_type': 'string', 'display_name': display,
                                                              'description': 'synthetic fixture (NX-038 P5)'})
        out = port(f).publish(pid, rationale='P5 合成验收夹具（synthetic）', idempotency_key=f'p5-fixture-prop-{i:04d}')
        assert out['published'] is True and out['version_after'] == i + 2 and out['successor_actions'] == []
    schema = admin.execute('select definition from ontology.object_type_versions where tenant_id=%s and type_name=%s and version=%s',
                           (tenant, FIXTURE, len(CROWD) + 2)).fetchone()[0]
    # The display names nexloop_recall_definition_rows (0067) indexes as terms.
    assert sorted(p.get('display_name') or '' for p in schema['properties'] if p['property_name'] != 'fixture_marker') == sorted([F] * len(CROWD) + [T])
    assert [r[0] for r in admin.execute('select kind from ontology.nexloop_schema_publications order by published_at')] == ['publish'] * (len(CROWD) + 2)
    assert admin.execute('select count(*) from ontology.objects where tenant_id=%s and type_name=%s', (tenant, FIXTURE)).fetchone()[0] == 0
    assert admin.execute("select count(*) from control.nexloop_action_definitions where tenant_id=%s and definition::text like %s",
                         (tenant, f'%{FIXTURE}%')).fetchone()[0] == 0
