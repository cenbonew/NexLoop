"""10/16 stage (release 5): every message Run's v6 pack was empty: required_source_unreadable for open_work [Consumer] and
evidence [Conversation]. The stage relay Source holds no per-object grant (stage_secrets register): its Consumer READ is derived
in SQL from its relay enrollment (0142 relay_source_read_rule) and its Message/Conversation READ from its message_read_rule
(0077/0102). Two gaps: the Context's Consumer proof and the accepted-Message derivation's Consumer READ took configured
authority only, so the 0142 derivation never counted; and the stage rule listed actor and body only, while the Conversation
READ needs conversation_id in the rule. This reproduces that authority (multi fixture: derived-only Source) with the stage rule
and collects the sources exactly as the message Context does."""
import json
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.context_engine.sources import collect
from nexloop_eios.message_relay import MessageRelayUnavailable
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


def test_a_derived_only_source_reads_every_required_context_source(multi):  # noqa: F811
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


def test_a_stage_source_s_second_message_pack_carries_the_earlier_turn_and_the_consumer(multi):  # noqa: F811
    """Through the relay (derived-only Source, the stage message_read_rule, recent_plus_required): A's messages are A1 (conversation
    0), A2 (conversation 1), A3 (conversation 0). A3's bound pack has no insufficiency, its evidence carries A1 as an earlier turn
    of the same conversation (0201) and not A3 itself (that is the user_statement), and consumer_state holds the Consumer."""
    from nexloop_eios.run_credential_vault import RunCredentialVault
    from test_context_v6_pg import STRATEGY, seed_strategy
    m = multi
    m['enroll']('A')
    stage_rule(m)
    seed_strategy(m['admin'], m['tenant'], STRATEGY)
    c = m['consumers']['A']
    with RunCredentialVault(m['vault_dir']) as vault:
        assert [m['relay'](vault, context_strategy='recent_plus_required').run_once() for _ in range(3)] == ['queued'] * 3
    packs = {}
    for text, run_id in m['admin'].execute('select pack_text,run_id::text from runtime.nexloop_context_artifact_bindings').fetchall():
        pack = json.loads(text)
        packs[pack['user_statement']['message_id']] = pack
    first, second, third = (packs[message] for message in c['messages'])
    for pack in (first, second, third):
        assert pack['insufficient'] == [], pack['insufficient']
        assert [i['ref'] for i in pack['consumer_state']] == ['eios:object:Consumer/' + c['consumer']]
    turns = [i for i in third['evidence'] if i['subsection'] == 'conversation']
    assert [(t['ref'], t['content']['speaker'], t['content']['body']) for t in turns] == [('eios:object:Message/' + c['messages'][0], 'consumer', 'A 第一条')]
    assert not [i for i in first['evidence'] if i['subsection'] == 'conversation']  # its conversation had no earlier turn


def seed_claims(m, message_id, count, kind='user_statement', state='unresolved', quote=None):
    """NX-019 statement Claims sourced on one accepted Message (the shape test_context_v6_pg.seed_claim writes)."""
    import hashlib
    import uuid
    from test_context_v6_pg import owner
    admin, tenant = m['admin'], m['tenant']
    conversation, sequence, record = admin.execute('select conversation_id,sequence,record from runtime.nexloop_conversation_messages where message_id=%s', (message_id,)).fetchone()
    consumer = admin.execute("select consumer_id from runtime.nexloop_conversations where conversation_id=%s", (conversation,)).fetchone()[0]
    body = record['body'] if quote is None else quote
    with admin.transaction():
        owner(admin, tenant)
        for n in range(count):
            claim_id = hashlib.sha256(('claim:stage-many:' + str(uuid.uuid4())).encode()).hexdigest()
            admin.execute('''insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
                predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
                extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags) values
                (%s,'real',%s,%s,%s,%s,%s,'consumer',%s,'顾客','note',%s,'consumer','affirmed','asserted','','','{}',%s,%s,0,%s,%s,%s,'nx019-extractor/2',0.8,%s,%s,'[]',%s,'[]')''',
                (tenant, claim_id, conversation, consumer, 'a' * 64, 't' + str(n), 'consumer:' + consumer, Jsonb({'type': 'text', 'value': body}),
                 message_id, sequence, len(body), hashlib.sha256(body.encode()).hexdigest(), body, kind, state, hashlib.sha256(claim_id.encode()).hexdigest()))


def test_with_claims_over_the_quota_the_latest_consumer_turn_stays_first_and_tagged(multi):  # noqa: F811
    """0202: A3's conversation has A1 as its earlier turn and 30 Claims (evidence quota 12). The bound pack (verified by the
    v6 section check) lists the turn before every Claim, tags it latest_consumer_turn, and trims Claims, never the turn."""
    from nexloop_eios.run_credential_vault import RunCredentialVault
    from test_context_v6_pg import STRATEGY, seed_strategy
    m = multi
    m['enroll']('A')
    stage_rule(m)
    seed_strategy(m['admin'], m['tenant'], STRATEGY)
    c = m['consumers']['A']
    with RunCredentialVault(m['vault_dir']) as vault:
        assert m['relay'](vault, context_strategy='recent_plus_required').run_once() == 'queued'
        seed_claims(m, c['messages'][0], 30)
        assert [m['relay'](vault, context_strategy='recent_plus_required').run_once() for _ in range(2)] == ['queued'] * 2
    packs = {json.loads(t)['user_statement']['message_id']: json.loads(t) for (t,) in m['admin'].execute('select pack_text from runtime.nexloop_context_artifact_bindings')}
    third = packs[c['messages'][2]]
    assert third['insufficient'] == [], third['insufficient']
    evidence = third['evidence']
    assert evidence[0]['ref'] == 'eios:object:Message/' + c['messages'][0] and evidence[0]['tags'] == ['latest_consumer_turn']
    assert [i['subsection'] for i in evidence[1:]] == ['claim_evidence'] * (len(evidence) - 1) and len(evidence) == STRATEGY['sections']['evidence']['max_items'] + 1
    omitted = third['budget_report']['omitted']
    # 10/18: of 30 non-pinned Claims only 2 x quota are candidates; the others are not read into the Context (nor reported).
    candidates = min(30, 2 * STRATEGY['sections']['evidence']['max_items'])
    assert len(omitted) == candidates - (len(evidence) - 1) and {o['reason'] for o in omitted} == {'section_quota'}
    assert all(o['subsection'] == 'claim_evidence' for o in omitted)
    # Exactly one tagged turn, and the SQL check accepted it (the binding row exists only after 0202 verified the pack).
    assert sum('latest_consumer_turn' in i['tags'] for i in evidence) == 1


TURN_TAMPER = {'tag_dropped': 'context v6 tags mismatch', 'tag_on_claim': 'context v6 tags mismatch', 'latest_omitted': 'context v6 pinned source omitted'}


@pytest.mark.parametrize('mode', sorted(TURN_TAMPER))
def test_sql_requires_the_latest_consumer_turn_tag_on_exactly_that_turn(multi, monkeypatch, caplog, mode):  # noqa: F811
    """0202 at the bind: the tag dropped from the latest consumer turn, put on another item, or the turn reported omitted is refused."""
    from nexloop_eios.context_artifacts import ContextV6ArtifactProducer
    from nexloop_eios.run_credential_vault import RunCredentialVault
    from test_context_v6_pg import STRATEGY, _recount, seed_strategy
    m = multi
    m['enroll']('A')
    stage_rule(m)
    seed_strategy(m['admin'], m['tenant'], STRATEGY)
    c = m['consumers']['A']
    latest = 'eios:object:Message/' + c['messages'][0]
    original = ContextV6ArtifactProducer.assemble

    def assemble(self, snapshot, command):
        body, outcome, items, proofs = original(self, snapshot, command)
        evidence = body['evidence']
        turn = next((i for i in evidence if i['ref'] == latest), None)
        if turn is not None:
            if mode == 'tag_dropped':
                turn['tags'] = []
            elif mode == 'tag_on_claim':
                claim = next(i for i in evidence if i['subsection'] == 'claim_evidence')
                claim['tags'] = sorted(claim['tags'] + ['latest_consumer_turn'])
            else:
                evidence.remove(turn)
                entry = {'section': 'evidence', 'subsection': 'conversation', 'ref': latest, 'reason': 'older_conversation'}
                body['budget_report']['omitted'].append(entry)
                if outcome.report is not body['budget_report']:
                    outcome.report['omitted'].append(entry)
            _recount(body)
        return body, outcome, items, proofs
    with RunCredentialVault(m['vault_dir']) as vault:
        assert m['relay'](vault, context_strategy='recent_plus_required').run_once() == 'queued'
        seed_claims(m, c['messages'][0], 2)
        assert m['relay'](vault, context_strategy='recent_plus_required').run_once() == 'queued'
        monkeypatch.setattr(ContextV6ArtifactProducer, 'assemble', assemble)
        relay = m['relay'](vault, context_strategy='recent_plus_required')
        with pytest.raises(MessageRelayUnavailable):
            relay.run_once()
    assert relay._context_diagnostic is not None and relay._context_diagnostic[1] == TURN_TAMPER[mode], relay._context_diagnostic
    # 10/18 stage: the failed attempt leaves one log line naming the refusing check as a code.
    [line] = [r for r in caplog.records if r.name == 'nexloop.message_relay' and r.msg.startswith('message relay attempt failed')]
    assert line.args['stage'] == 'context_prepare' and line.args['code'] == TURN_TAMPER[mode].replace(' ', '_')


def seed_unknown_replies(m, c, count):
    """The 10/16 smoke left two service.request intents unknown with their outbound replies unknown (customer-1 on stage)."""
    import uuid
    admin, tenant = m['admin'], m['tenant']
    context = admin.execute('select context_id from control.nexloop_effect_contexts where tenant_id=%s limit 1', (tenant,)).fetchone()[0]
    run = admin.execute('select run_id from runtime.nexloop_message_routes where tenant_id=%s and run_id is not null limit 1', (tenant,)).fetchone()[0]
    intents = []
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # test data only: the ledger rows as the stuck smoke left them
        for n in range(count):
            conversation, message = admin.execute('select conversation_id,message_id from runtime.nexloop_conversation_messages where message_id=%s', (c['messages'][n],)).fetchone()
            intent = uuid.uuid4()
            admin.execute('''insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,plan_step_identity,slot_identity,
                action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,capability,executor_principal,control_revision,created_at,state,origin_run_id)
                values(%s,%s,%s,%s,'real',%s,'g','s',%s,'nexloop.service.request',1,%s,%s,'{}','{}','{}','x',1,clock_timestamp()-interval '2 days','unknown',%s)''',
                (intent, uuid.uuid4(), context, tenant, c['consumer'], 'slot-' + str(n), 'a' * 64, 'b' * 64, run))
            admin.execute('''insert into runtime.nexloop_outbound_messages(tenant_id,world,intent_id,receipt_id,conversation_id,trigger_message_id,run_id,sender_kind,
                sender_principal,role_ref,body_digest,delivery_state) values(%s,'real',%s,%s,%s,%s,%s,'agent','x','role',%s,'unknown')''',
                (tenant, intent, uuid.uuid4(), conversation, message, run, 'c' * 64))
            intents.append(str(intent))
    return intents


def test_a_consumer_with_a_long_history_still_gets_a_context_within_the_bound_size(multi):  # noqa: F811
    """10/18 stage, trial e80e360c: customer-1 (about 200 extracted Claims, 20 pinned contact limits, two unknown replies from the
    10/16 smoke) got ContextArtifactUnavailable on every relay attempt and on its fallback: context_pack_too_large (the token
    budget does not bound bytes; the omission report alone listed every unkept Claim). Its next message now binds a pack within
    64 KiB with every pinned source (contact limits, unknown intents and replies, the latest consumer turn)."""
    from nexloop_eios.context_engine.pack import MAX_PACK_BYTES
    from nexloop_eios.run_credential_vault import RunCredentialVault
    from test_context_v6_pg import STRATEGY, seed_strategy
    m = multi
    m['enroll']('A')
    stage_rule(m)
    seed_strategy(m['admin'], m['tenant'], STRATEGY)
    c = m['consumers']['A']
    with RunCredentialVault(m['vault_dir']) as vault:
        assert m['relay'](vault, context_strategy='recent_plus_required').run_once() == 'queued'
        unknown = seed_unknown_replies(m, c, 2)
        for kind, state, n in (('intent', 'needs_resolution', 101), ('need_problem', 'needs_resolution', 30), ('user_statement', 'awaiting_definition', 34),
                               ('constraint', 'awaiting_definition', 17), ('need_problem', 'awaiting_definition', 7), ('user_statement', 'needs_resolution', 3),
                               ('constraint', 'needs_resolution', 3)):
            seed_claims(m, c['messages'][0], n, kind, state, quote=('顾客原话很长' * 60) if kind == 'constraint' else None)
        assert [m['relay'](vault, context_strategy='recent_plus_required').run_once() for _ in range(2)] == ['queued'] * 2
    (text,) = m['admin'].execute('select pack_text from runtime.nexloop_context_artifact_bindings where message_id=%s', (c['messages'][2],)).fetchone()
    pack = json.loads(text)
    assert len(text.encode()) <= MAX_PACK_BYTES and pack['insufficient'] == []
    refs = {i['ref'] for section in ('open_work', 'evidence') for i in pack[section]}
    assert {'nexloop:intent:' + i for i in unknown} | {'nexloop:outbound:' + i for i in unknown} <= refs
    assert sum(1 for i in pack['evidence'] if 'contact_limit' in i['tags']) == 20
    assert any(i['tags'] == ['latest_consumer_turn'] for i in pack['evidence'])
    assert len(pack['budget_report']['omitted']) <= 2 * STRATEGY['sections']['evidence']['max_items']
