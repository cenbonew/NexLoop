"""10/16 stage (release 5): every message Run's v6 pack was empty: required_source_unreadable for open_work [Consumer] and
evidence [Conversation]. The stage relay Source holds no per-object grant (stage_secrets register): its Consumer READ is derived
in SQL from its relay enrollment (0142 relay_source_read_rule) and its Message/Conversation READ from its message_read_rule
(0077/0102). Two gaps: the Context's Consumer proof and the accepted-Message derivation's Consumer READ took configured
authority only, so the 0142 derivation never counted; and the stage rule listed actor and body only, while the Conversation
READ needs conversation_id in the rule. This reproduces that authority (multi fixture: derived-only Source) with the stage rule
and collects the sources exactly as the message Context does."""
import json
from pathlib import Path

from psycopg.types.json import Jsonb

from nexloop_eios.context_engine.sources import collect
from test_relay_multi_consumer_pg import multi, relay_plan, receipt_plan, message_plan  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
STAGE_RULE_FIELDS = json.loads((ROOT / 'deploy/authorization/stage-principals.v1.json').read_text())['source_message_read_rules'][0]['fields']


def stage_rule(m):
    """The Source's message_read_rule with the fields the stage registers (stage-principals.v1.json)."""
    row = m['admin'].execute("select payload from authz.nexloop_authority_facts where fact_kind='message_read_rule' and tenant_id=%s", (m['tenant'],)).fetchone()[0]
    row['fields'] = sorted(STAGE_RULE_FIELDS)
    m['admin'].execute("update authz.nexloop_authority_facts set payload=%s where fact_kind='message_read_rule' and tenant_id=%s", (Jsonb(row), m['tenant']))


def conversations_of(m, c):
    return [r[0] for r in m['admin'].execute("select distinct properties->>'conversation_id' from ontology.objects where type_name='Message' and object_id=any(%s)",
                                             (c['messages'],))]


def test_the_stage_rule_lists_conversation_id():
    assert 'conversation_id' in STAGE_RULE_FIELDS and STAGE_RULE_FIELDS == sorted(set(STAGE_RULE_FIELDS))


def test_a_derived_only_source_reads_every_required_context_source(multi):
    m = multi
    m['enroll']('A')
    stage_rule(m)
    api, c = m['api'], m['consumers']['A']
    source = api.authenticate(m['source_token'], world='real')
    found = collect(api._pool, source._session, api._signer, consumer_id=c['consumer'], conversation_ids=conversations_of(m, c), strategy={'include_hypotheses': False})
    assert found.insufficient == [], found.insufficient
    # Not enrolled: nothing is derived, the gap is still declared (never guessed).
    other = m['consumers']['B']
    found = collect(api._pool, source._session, api._signer, consumer_id=other['consumer'], conversation_ids=conversations_of(m, other), strategy={'include_hypotheses': False})
    assert {i['section'] for i in found.insufficient} == {'open_work', 'evidence'}
