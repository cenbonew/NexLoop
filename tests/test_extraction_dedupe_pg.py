"""S-DUP (docs/implementation/S-DUP-extraction-dedupe-design.md) on clean catalog PostgreSQL: windows split into targets and context
(0411), the recorder writes the same statement once (0412: span, kind, polarity, modality, condition presence, normalized value), a
correction may point at a current Claim recorded outside the window (C1), and no Claim may cite a context Message (SQL guard).
claim_id is unchanged. Synthetic data only."""
import json

import pytest

from nexloop_eios import claim_store
from nexloop_eios.claim_store import ConversationClaimExtractor
from nexloop_eios.conversation_extraction import EXTRACTOR_VERSION, SYSTEM_PROMPT
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_claim_store_pg import source_targets
from test_conversation_messages import conversations  # noqa: F401

FILLER = [f'第{i}条闲聊' for i in range(1, 12)]
BODIES = ['只接收站内消息可以吗？', '只接收站内消息', '我的会员号是 AB 12', '我买了 3 件', '我的会员号是A123', *FILLER, '刚才说错了，会员号是A124']


class Scripted:
    """The model's answers in order; model_id varies per extractor so each run has its own input digest."""
    provider = 'test'

    def __init__(self, model_id, responses):
        self.model_id, self.responses, self.calls = model_id, list(responses), 0

    def complete(self, system_prompt, user_payload):
        assert system_prompt == SYSTEM_PROMPT
        self.calls += 1
        self.last_payload = json.loads(user_payload)
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


def claim(ref, quote, kind, predicate, value, **extra):
    return {'topic_index': 0, 'message_ref': ref, 'quote': quote, 'kind': kind, 'explicit': True, 'subject': {'kind': 'consumer', 'text': ''},
            'predicate': predicate, 'value': value, **extra}


def answer(refs, *claims):
    return {'topics': [{'topic': '测试', 'conversation_summary': '合成', 'user_valid_reply': True, 'message_refs': refs}], 'claims': list(claims)}


@pytest.fixture
def chat(conversations, admin):
    from test_claim_extraction_jobs_pg import make_window
    conversation, ids = make_window(conversations, BODIES, 'sdup')
    created = {'id': conversation}
    reader = conversations['reader']
    session, _ = seed_multi_authority(admin, reader.pool, source_targets(created['id'], ids), identity_suffix='-sdup-extractor')
    seq = dict(admin.execute('select message_id,sequence from runtime.nexloop_conversation_messages where conversation_id=%s', (created['id'],)).fetchall())

    def run(model_id, targets, response, context=()):
        provider = Scripted(model_id, [response])
        result = ConversationClaimExtractor(reader.pool, session, reader.signer, provider, timezone='Asia/Shanghai').extract(
            conversation_id=created['id'], message_ids=[ids[i] for i in targets], context_message_ids=[ids[i] for i in context])
        return result, provider
    return {'conversation': created['id'], 'ids': ids, 'seq': seq, 'run': run}


def rows(admin, message, kind=None):
    return admin.execute('select predicate,modality,value,claim_id from ontology.nexloop_claims where source_message_id=%s'
                         + (' and epistemic_kind=%s' if kind else '') + ' order by recorded_at', (message, kind) if kind else (message,)).fetchall()


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_the_same_statement_is_written_once_and_real_differences_are_kept(chat, admin):
    c, S = chat, (lambda v: {'type': 'string', 'value': v})
    q = '只接收站内消息'
    first, _ = c['run']('m1', [1], answer([2], claim(2, q, 'preference', '联系方式', S('站内消息'))))
    again, _ = c['run']('m2', [1], answer([2], claim(2, q, 'preference', '通知方式偏好', S(' 站内消息 '))))  # other words, same statement
    assert len(rows(admin, c['ids'][1])) == 1 and again['claim_ids'] == first['claim_ids']
    assert admin.execute('select count(*) from ontology.nexloop_extraction_run_claims where claim_id=%s', (first['claim_ids'][0],)).fetchone() == (2,)
    c['run']('m3', [1], answer([2], claim(2, q, 'preference', '联系方式', S('短信'))))  # a different value is a new statement
    assert len(rows(admin, c['ids'][1])) == 2
    # Normalization: whitespace and case only for strings ('AB 12' = 'ab12'); a string '3' is not the number 3.
    c['run']('m4', [2], answer([3], claim(3, '我的会员号是 AB 12', 'user_statement', '会员号', S('AB 12'))))
    c['run']('m5', [2], answer([3], claim(3, '我的会员号是 AB 12', 'user_statement', '会员编号', S('ab12'))))
    assert len(rows(admin, c['ids'][2])) == 1
    c['run']('m6', [3], answer([4], claim(4, '我买了 3 件', 'user_statement', '购买件数', S('3'))))
    c['run']('m7', [3], answer([4], claim(4, '我买了 3 件', 'user_statement', '购买件数', {'type': 'number', 'value': 3})))
    assert len(rows(admin, c['ids'][3])) == 2


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_a_requested_then_asserted_statement_is_two_claims_and_the_later_one_counts(chat, admin):
    c, S = chat, (lambda v: {'type': 'string', 'value': v})
    q = '只接收站内消息'
    c['run']('r1', [1], answer([2], claim(2, q, 'preference', 'contact_channel', S('native.webchat'), modality='requested')))
    c['run']('r2', [1], answer([2], claim(2, q, 'preference', 'contact_channel', S('native.webchat'), modality='asserted')))
    assert [r[1] for r in rows(admin, c['ids'][1])] == ['requested', 'asserted']
    tenant, consumer = admin.execute('select tenant_id,consumer_id from runtime.nexloop_conversations where conversation_id=%s', (c['conversation'],)).fetchone()
    prefs = admin.execute("select runtime.nexloop_contact_preferences(%s,'real',%s)", (tenant, consumer)).fetchone()[0]
    assert rows(admin, c['ids'][1])[1][3] in {p['claim_id'] for p in prefs}


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_context_is_never_extracted_and_a_correction_reaches_a_claim_outside_the_window(chat, admin):
    c, S = chat, (lambda v: {'type': 'string', 'value': v})
    first, _ = c['run']('c1', [4], answer([5], claim(5, '我的会员号是A123', 'user_statement', '会员号', S('A123'))))
    old = first['claim_ids'][0]
    last = len(BODIES) - 1
    context = list(range(last - 10, last))  # the ten Messages before the target: the corrected one is older than all of them
    assert 4 not in context
    seq = c['seq']
    ref_of = lambda i: seq[c['ids'][i]]
    result, provider = c['run']('c2', [last], answer([ref_of(context[-1]), ref_of(last)],
        claim(ref_of(context[-1]), FILLER[-1], 'user_statement', '闲聊', S('闲聊')),  # cites a context Message: dropped
        claim(ref_of(last), '刚才说错了，会员号是A124', 'correction', '会员号', S('A124'), corrects_existing='e1')), context=context)
    payload = provider.last_payload
    assert [r.get('context', False) for r in payload['conversation_data']] == [True] * 10 + [False]
    assert payload['existing_claims']['user_statement'][0]['ref'] == 'e1'
    (new,) = result['claim_ids']
    assert admin.execute('select epistemic_kind,corrects_claim_id,extractor_version from ontology.nexloop_claims where claim_id=%s', (new,)).fetchone() == (
        'correction', old, EXTRACTOR_VERSION)
    assert admin.execute('select count(*) from ontology.nexloop_claims where source_message_id=any(%s)', ([c['ids'][i] for i in context],)).fetchone() == (0,)
    rejected = admin.execute('select rejected from ontology.nexloop_extraction_runs where model_id=%s', ('c2',)).fetchone()[0]
    assert [r['reason'] for r in rejected] == ['context_message']


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_sql_refuses_a_claim_citing_a_context_message_even_past_the_client_guard(chat, admin, monkeypatch):
    c, S = chat, (lambda v: {'type': 'string', 'value': v})
    real = claim_store.normalize

    def blind(raw, messages, context):  # the client guard skipped: context flags dropped before normalizing
        import dataclasses
        return real(raw, [dataclasses.replace(m, context=False) for m in messages], context)
    monkeypatch.setattr(claim_store, 'normalize', blind)
    with pytest.raises(claim_store.ClaimExtractionContextCited):  # its own code, never a denial (claim_store._raise)
        c['run']('g1', [1], answer([1, 2], claim(1, '只接收站内消息可以吗？', 'preference', '联系方式', S('站内消息'))), context=[0])
    assert admin.execute('select count(*) from ontology.nexloop_claims where source_message_id=%s', (c['ids'][0],)).fetchone() == (0,)


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_due_returns_the_pending_targets_and_up_to_ten_earlier_messages_as_context(conversations, admin):
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    from nexloop_eios.authorization import authenticate_service
    from nexloop_eios.claim_extraction_jobs import QUEUE, ClaimFeed
    from test_claim_extraction_jobs_pg import make_window
    conversation, ids = make_window(conversations, BODIES, 'sdup-due')
    # The first 14 Messages were extracted earlier (their feed rows enqueued); the last three are pending.
    admin.execute("select set_config('eios.tenant_id',(select tenant_id from runtime.nexloop_conversations where conversation_id=%s),false)", (conversation,))
    admin.execute("update runtime.nexloop_claim_extraction_feed set task_id='sdup-earlier',enqueued_at=clock_timestamp() where message_id=any(%s)", (ids[:14],))
    api = conversations['reader'].pool
    _, token = seed_multi_authority(admin, api, [('eios:action:nexloop.claim.extract:1', ResourceType.ACTION, Operation.EXECUTE),
                                                 ('eios:action:NexLoop.queue.' + QUEUE + ':1', ResourceType.ACTION, Operation.EXECUTE)], identity_suffix='-sdup-scheduler')
    feed = ClaimFeed(api, authenticate_service(api, token, world='real'), conversations['reader'].signer)
    (item,) = feed.due(quiet_seconds=0, limit=10, window=50)
    assert item['message_ids'] == ids[14:] and item['context_message_ids'] == ids[4:14]
    (narrow,) = feed.due(quiet_seconds=0, limit=10, window=2)  # window bounds the targets; context ends before the first target
    assert narrow['message_ids'] == ids[15:] and narrow['context_message_ids'] == ids[5:15]


@pytest.mark.parametrize('value', [
    {'type': 'string', 'value': ' 站内 消息 '}, {'type': 'string', 'value': 'AB　12'}, {'type': 'string', 'value': 'Ab C'},
    {'type': 'string', 'value': '3'}, {'type': 'number', 'value': 3}, {'type': 'number', 'value': 3.5}, {'type': 'boolean', 'value': False},
    {'type': 'none', 'value': None}, {'type': 'money', 'value': {'currency': 'CNY', 'amount': 12.5}}])
@pytest.mark.parametrize('condition', ['', '解决后'])
def test_python_and_sql_statement_keys_are_the_same_text(admin, value, condition):
    """The one key: conversation_extraction.statement_key (Python, K5 folding) equals ontology.nexloop_claim_statement_key (0412 dedupe)."""
    from nexloop_eios.bootstrap import bootstrap
    from nexloop_eios.conversation_extraction import statement_key
    from psycopg.types.json import Jsonb
    bootstrap(admin)
    args = dict(source_message_id='a' * 64, span_start=0, span_end=5, kind='preference', polarity='affirmed', modality='requested', condition=condition, value=value)
    sql = admin.execute('select ontology.nexloop_claim_statement_key(%s,%s,%s,%s,%s,%s,%s,%s)',
                        (args['source_message_id'], 0, 5, 'preference', 'affirmed', 'requested', condition, Jsonb(value))).fetchone()[0]
    assert statement_key(**args) == sql
