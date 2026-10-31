"""Semantic read health (I29 / L6 S60, migration semantic_read_health, alert rules v24) on real PostgreSQL: a semantic access
whose caller could read no definition type is counted in the metrics snapshot and fires semantic_read_no_readable_types; a
read with readable types, or one older than an hour, does not."""
import json
from pathlib import Path

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.context_engine.semantic import SemanticAccessRecorder
from nexloop_eios.observability import load_rules
from test_recall_pg import A
from test_semantic_access_record_pg import recall_db,semantic  # noqa: F401  (fixtures)
from test_semantic_terms_pg import SENTENCE,adapter

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'


def health(admin):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'semantic_reads'", (A,)).fetchone()[0]


def fired(snapshot):
    return {r['rule_id'] for r in load_rules(RULES / 'alert-rules.v24.json')['rules']
            if r['metric'].partition('.')[0] == 'semantic_reads' and snapshot[r['metric'].partition('.')[2]] > r['threshold']}


def test_v24_adds_exactly_the_no_readable_types_rule():
    v23, v24 = json.loads((RULES / 'alert-rules.v23.json').read_text()), load_rules(RULES / 'alert-rules.v24.json')
    assert v24['version'] == 24 and len(v24['rules']) == 87 and v24['rules'][:len(v23['rules'])] == v23['rules']
    [added] = v24['rules'][len(v23['rules']):]
    assert added['rule_id'] == 'semantic_read_no_readable_types' and added['metric'] == 'semantic_reads.no_readable_types_1h'
    assert added['severity'] == 'warning' and added['threshold'] == 0


def test_a_read_with_no_readable_types_fires_and_a_good_one_does_not(semantic):
    f, session, signer = semantic; admin = f['admin']
    assert health(admin) == {'accesses_1h': 0, 'no_readable_types_1h': 0} and fired(health(admin)) == set()  # visible zero
    adapter(f, session, signer, SemanticAccessRecorder(f['worker'], session, signer)).browse(SENTENCE, extract_terms=True)
    assert health(admin) == {'accesses_1h': 1, 'no_readable_types_1h': 0} and fired(health(admin)) == set()
    blind, _ = seed_multi_authority(admin, f['worker'], [('eios:object_type:Elsewhere', ResourceType.OBJECT_TYPE, Operation.READ)],
                                    identity_suffix='-blind-health', world='real', tenant=A)
    adapter(f, blind, signer, SemanticAccessRecorder(f['worker'], blind, signer)).browse(SENTENCE, extract_terms=True)
    assert health(admin) == {'accesses_1h': 2, 'no_readable_types_1h': 1} and fired(health(admin)) == {'semantic_read_no_readable_types'}
    later = admin.execute("select runtime.nexloop_semantic_read_health(%s,'real',clock_timestamp()+interval '2 hours')", (A,)).fetchone()[0]
    assert later == {'accesses_1h': 0, 'no_readable_types_1h': 0}  # an hour later the window is clear (records are append-only)
    # Other worlds carry no block.
    assert admin.execute("select runtime.nexloop_metrics_snapshot(%s,'test') ? 'semantic_reads'", (A,)).fetchone()[0] is False
