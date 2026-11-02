"""S-DUP-2 (docs/implementation/S-DUP-extraction-dedupe-design.md §11) on clean catalog PostgreSQL: when a correction supersedes a Claim,
its still-active duplicates (same 0412 statement key) are superseded with it and recorded by id; a Claim with another key stays active;
the 0228 preferences then follow the new statement. The governed backfill does the same for corrections made before the fix: claim matcher
service, counts only, repeatable, erasures skipped. Synthetic data only."""
import hashlib

import pytest
from psycopg.types.json import Jsonb

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.claim_duplicates import ACTION, ClaimDuplicateSupersedePort
from nexloop_eios.plan_reevaluation import PlanUnavailable
from psycopg.conninfo import make_conninfo
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_claim_extraction_jobs_pg import make_window
from test_claim_matching_pg import env  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401

OLD = '以后只接收站内消息'
NEW = '刚才说错了，还是打电话吧'


def oid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def add(admin, conversation, message, label, quote, kind, predicate, value, *, modality='asserted', polarity='affirmed', corrects=None):
    seq, body = admin.execute("select sequence,record->>'body' from runtime.nexloop_conversation_messages where message_id=%s", (message,)).fetchone()
    start = body.index(quote)
    admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
        predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
        extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags,corrects_claim_id)
        select tenant_id,world,%s,conversation_id,consumer_id,%s,%s,'consumer',consumer_id,'',%s,%s,'consumer',%s,%s,'','',%s,%s,%s,%s,%s,%s,%s,
        'nx019-extractor/4',0.9,%s,'unresolved','[]',%s,'[]',%s from runtime.nexloop_conversations where conversation_id=%s""",
        (oid(label), '0' * 64, '1' * 64, predicate, Jsonb(value), polarity, modality,
         Jsonb({'kind': 'none', 'status': 'absent', 'start': None, 'end': None, 'anchor': '2026-10-30T00:00:00+00:00', 'timezone': 'Asia/Shanghai', 'expression': ''}),
         message, seq, start, start + len(quote), hashlib.sha256(body.encode()).hexdigest(), quote, kind, oid(label + '-corr'), corrects, conversation))
    return oid(label)


@pytest.fixture
def corrected(conversations, admin):
    """Three active duplicates of one statement (other predicates), one Claim with another key on the same span, then the correction."""
    conversation, ids = make_window(conversations, [OLD, NEW], 'sdup2')
    S = lambda v: {'type': 'string', 'value': v}
    dups = [add(admin, conversation, ids[0], f'dup-{i}', OLD, 'preference', p, S(v)) for i, (p, v) in
            enumerate((('contact_channel', 'native.webchat'), ('联系渠道', 'Native.Webchat'), ('contact_channel', ' native.webchat ')))]
    other = add(admin, conversation, ids[0], 'other-key', OLD, 'preference', 'contact_channel', S('sms'), polarity='negated')
    fix = add(admin, conversation, ids[1], 'correction', NEW, 'correction', 'contact_channel', S('phone'), corrects=dups[0])
    new = add(admin, conversation, ids[1], 'new-statement', NEW, 'preference', 'contact_channel', S('phone'))
    tenant, consumer = admin.execute('select tenant_id,consumer_id from runtime.nexloop_conversations where conversation_id=%s', (conversation,)).fetchone()
    return dict(conversations=conversations, conversation=conversation, ids=ids, dups=dups, other=other, fix=fix, new=new, tenant=tenant, consumer=consumer)


def state(admin, claim):
    return admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s', (claim,)).fetchone()[0]


def channels(admin, c):
    prefs = admin.execute("select runtime.nexloop_contact_preferences(%s,'real',%s)", (c['tenant'], c['consumer'])).fetchone()[0]
    return sorted((p['value'], p['polarity']) for p in prefs if p['dimension'] == 'channel')


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_a_correction_supersedes_every_active_duplicate_and_0228_follows_the_new_statement(corrected, admin):
    c = corrected
    assert channels(admin, c) == sorted([('Native.Webchat', 'affirmed'), (' native.webchat ', 'affirmed'), ('native.webchat', 'affirmed'), ('phone', 'affirmed'), ('sms', 'negated')])
    # The usual supersede write (as the matcher's apply of the correction does).
    admin.execute("update ontology.nexloop_claims set resolution_state='superseded' where claim_id=%s", (c['dups'][0],))
    assert [state(admin, d) for d in c['dups']] == ['superseded'] * 3
    assert state(admin, c['other']) == 'unresolved'  # another statement key: untouched
    assert channels(admin, c) == [('phone', 'affirmed'), ('sms', 'negated')]
    assert sorted(admin.execute('select claim_id,corrected_claim_id,correction_claim_id,cause from ontology.nexloop_claim_supersessions').fetchall()) == sorted(
        (d, c['dups'][0], c['fix'], 'statement_duplicate') for d in c['dups'][1:])
    assert admin.execute('select count(*) from ontology.nexloop_claims where claim_id=any(%s) and superseded_at is null', (c['dups'],)).fetchone() == (0,)


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_a_supersede_without_a_correction_does_not_cascade(corrected, admin):
    c = corrected
    admin.execute("update ontology.nexloop_claims set resolution_state='superseded' where claim_id=%s", (c['dups'][1],))  # nothing corrects it
    assert [state(admin, d) for d in c['dups']] == ['unresolved', 'superseded', 'unresolved']
    assert admin.execute('select count(*) from ontology.nexloop_claim_supersessions').fetchone() == (0,)


def earlier_correction(admin, c):
    """A correction that took effect before the fix: the trigger did not run then."""
    admin.execute('alter table ontology.nexloop_claims disable trigger claim_superseded_duplicates')
    try:
        admin.execute("update ontology.nexloop_claims set resolution_state='superseded' where claim_id=%s", (c['dups'][0],))
    finally:
        admin.execute('alter table ontology.nexloop_claims enable trigger claim_superseded_duplicates')
    assert [state(admin, d) for d in c['dups']] == ['superseded', 'unresolved', 'unresolved']


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_backfill_supersedes_earlier_corrections_counts_only_and_is_repeatable(corrected, admin):
    c = corrected
    earlier_correction(admin, c)
    f = c['conversations']
    with open_core(make_conninfo(f['pg'], user='nexloop_domain_worker')) as worker:
        _, token = seed_multi_authority(admin, worker, [(ACTION, ResourceType.ACTION, Operation.EXECUTE)], identity_suffix='-claim-matcher')
        _, other = seed_multi_authority(admin, worker, [('eios:action:nexloop.claim.match:1', ResourceType.ACTION, Operation.EXECUTE)], identity_suffix='-no-supersede')
        port = lambda credential=None: ClaimDuplicateSupersedePort(worker, authenticate_service(worker, credential or token, world='real'), f['reader'].signer)
        with pytest.raises(PlanUnavailable):
            port(other).run()
        dry = port().run()
        assert dry == {'verb': 'dry_run', 'counts': {'corrected': 1, 'duplicates': 2, 'superseded': 0, 'skipped_erasure': 0, 'commitments': 0}}
        assert [state(admin, d) for d in c['dups']] == ['superseded', 'unresolved', 'unresolved']
        done = port().run(apply=True)
        assert done['counts'] == {'corrected': 1, 'duplicates': 2, 'superseded': 2, 'skipped_erasure': 0, 'commitments': 0}
        assert [state(admin, d) for d in c['dups']] == ['superseded'] * 3 and state(admin, c['other']) == 'unresolved'
        assert channels(admin, c) == [('phone', 'affirmed'), ('sms', 'negated')]
        again = port().run(apply=True)
        assert again['counts'] == {'corrected': 0, 'duplicates': 0, 'superseded': 0, 'skipped_erasure': 0, 'commitments': 0}


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_backfill_skips_a_consumer_with_an_erasure_request(corrected, admin):
    c = corrected
    earlier_correction(admin, c)
    admin.execute("insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark) "
                  "values(%s,'real','erasure-sdup2-synthetic',%s,'owner','synthetic-owner','synthetic','pending_confirmation',0)", (c['tenant'], c['consumer']))
    f = c['conversations']
    with open_core(make_conninfo(f['pg'], user='nexloop_domain_worker')) as worker:
        _, token = seed_multi_authority(admin, worker, [(ACTION, ResourceType.ACTION, Operation.EXECUTE)], identity_suffix='-claim-matcher')
        done = ClaimDuplicateSupersedePort(worker, authenticate_service(worker, token, world='real'), f['reader'].signer).run(apply=True)
    assert done['counts'] == {'corrected': 1, 'duplicates': 0, 'superseded': 0, 'skipped_erasure': 2, 'commitments': 0}
    assert [state(admin, d) for d in c['dups']] == ['superseded', 'unresolved', 'unresolved']


def copies(admin, claim, n, state='resolved'):
    """Duplicates of one Claim as the pre-0412 re-extraction left them: same Message span, kind, polarity, modality and value."""
    made = []
    for i in range(n):
        new = oid(f'{claim}-copy-{i}')
        admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
            select tenant_id,world,%s,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,predicate||%s,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,extractor_version,confidence,epistemic_kind,%s,derived_from,correlation_key,guard_flags from ontology.nexloop_claims where claim_id=%s""", (new, f'（副本{i}）', state, claim))
        made.append(new)
    return made


def test_end_to_end_through_the_matcher_apply_path(env):
    """The real path: the matcher applies a correction (authz.nexloop_record_claim_match, proposal applied, then supersede)."""
    from datetime import timedelta
    from test_claim_matching_pg import T0, decision, matcher, proposals, resolution
    f = env
    c = f['claims']
    first = c.add('e2e-1', '喜欢的运动', '网球', quote='喜欢网球', at=T0 + timedelta(days=1))
    m, _ = matcher(f, {first: decision('eios:property:Consumer/favorite_sport', '网球')})
    m.process_conversation(f['conversation'])
    dups = copies(f['admin'], first, 2)
    fix = c.add('e2e-2', '喜欢的运动', '羽毛球', kind='correction', quote='说错了，是羽毛球', at=T0)
    m.provider.decisions[fix] = decision('eios:property:Consumer/favorite_sport', '羽毛球')
    m.process_conversation(f['conversation'])
    assert proposals(f['admin'])[fix][8] == first and resolution(f['admin'], first) == 'superseded'
    assert [resolution(f['admin'], d) for d in dups] == ['superseded', 'superseded']
    assert sorted(f['admin'].execute('select claim_id,corrected_claim_id,correction_claim_id from ontology.nexloop_claim_supersessions').fetchall()) == sorted(
        (d, first, fix) for d in dups)


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_end_to_end_through_the_extraction_recorder_then_a_later_supersede(conversations, admin):
    """The correction is recorded by authz.nexloop_record_claim_extraction (corrects_claim_id set after the insert, same transaction);
    the corrected Claim is superseded later in its own statement; its duplicates follow at commit."""
    import json as _json
    from nexloop_eios.claim_store import ConversationClaimExtractor
    from nexloop_eios.conversation_extraction import SYSTEM_PROMPT
    from test_claim_store_pg import source_targets
    conversation, ids = make_window(conversations, ['我的会员号是A123', '刚才说错了，会员号是A124'], 'sdup2-e2e')
    reader = conversations['reader']
    session, _ = seed_multi_authority(admin, reader.pool, source_targets(conversation, ids), identity_suffix='-sdup2-extractor')

    class Fixed:
        provider = 'test'

        def __init__(self, model_id, response):
            self.model_id, self.response = model_id, response

        def complete(self, system_prompt, user_payload):
            assert system_prompt == SYSTEM_PROMPT
            return _json.dumps(self.response, ensure_ascii=False)

    def run(model_id, targets, context, claims):
        refs = [admin.execute('select sequence from runtime.nexloop_conversation_messages where message_id=%s', (ids[i],)).fetchone()[0] for i in context + targets]
        response = {'topics': [{'topic': '会员号', 'conversation_summary': '合成', 'user_valid_reply': True, 'message_refs': refs}], 'claims': claims}
        return ConversationClaimExtractor(reader.pool, session, reader.signer, Fixed(model_id, response), timezone='Asia/Shanghai').extract(
            conversation_id=conversation, message_ids=[ids[i] for i in targets], context_message_ids=[ids[i] for i in context])

    seq = lambda i: admin.execute('select sequence from runtime.nexloop_conversation_messages where message_id=%s', (ids[i],)).fetchone()[0]
    S = lambda v: {'type': 'string', 'value': v}
    (first,) = run('e1', [0], [], [{'topic_index': 0, 'message_ref': seq(0), 'quote': '我的会员号是A123', 'kind': 'user_statement', 'explicit': True,
                                   'subject': {'kind': 'consumer', 'text': ''}, 'predicate': '会员号', 'value': S('A123')}])['claim_ids']
    dups = copies(admin, first, 2, state='unresolved')
    result = run('e2', [1], [0], [{'topic_index': 0, 'message_ref': seq(1), 'quote': '刚才说错了，会员号是A124', 'kind': 'correction', 'explicit': True,
                                   'subject': {'kind': 'consumer', 'text': ''}, 'predicate': '会员号', 'value': S('A124'), 'corrects_existing': 'e1'}])
    (fix,) = result['claim_ids']
    target = admin.execute('select corrects_claim_id from ontology.nexloop_claims where claim_id=%s', (fix,)).fetchone()[0]
    assert target in [first, *dups]
    admin.execute("update ontology.nexloop_claims set resolution_state='superseded' where claim_id=%s", (target,))
    assert {state(admin, x) for x in [first, *dups]} == {'superseded'}
    assert admin.execute('select count(*) from ontology.nexloop_claim_supersessions where correction_claim_id=%s', (fix,)).fetchone() == (2,)


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_a_correction_linked_after_the_supersede_still_reaches_the_duplicates(corrected, admin):
    """Order does not matter: the corrected Claim is superseded first (no correction yet), then the correction is linked."""
    c = corrected
    admin.execute("update ontology.nexloop_claims set corrects_claim_id=null where claim_id=%s", (c['fix'],))
    admin.execute("update ontology.nexloop_claims set resolution_state='superseded' where claim_id=%s", (c['dups'][0],))
    assert [state(admin, d) for d in c['dups']] == ['superseded', 'unresolved', 'unresolved']
    admin.execute("update ontology.nexloop_claims set corrects_claim_id=%s where claim_id=%s", (c['dups'][0], c['fix']))
    assert [state(admin, d) for d in c['dups']] == ['superseded'] * 3
