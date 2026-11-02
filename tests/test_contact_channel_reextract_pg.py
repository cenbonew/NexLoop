"""Contact channel re-extraction (plan docs/implementation/claim-to-consumer-property-plan.md §8.4), on clean catalog PostgreSQL. A channel
statement recorded by an earlier extractor version with a free-text predicate gives the 0228 preferences nothing; the claim extraction
scheduler, holding nexloop.claim.reextract_contact, gets one window per such Conversation (message ids and counts only) and enqueues it under
a re-extraction event id on its own queue; the worker extracts it with the current version, and the consumer's channel preference appears in
runtime.nexloop_contact_preferences (the source of the workbench "这位客户自己说过的沟通偏好"). Repeatable; simulated personas and erasures are
skipped. Synthetic data only."""
import hashlib
import json

import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.claim_extraction_jobs import QUEUE, REEXTRACT_ACTION, ClaimExtractionWorker, reextract_contact_channels
from nexloop_eios.conversation_extraction import EXTRACTOR_VERSION, SYSTEM_PROMPT
from nexloop_eios.plan_reevaluation import PlanUnavailable
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_claim_extraction_jobs_pg import make_window
from test_claim_store_pg import source_targets
from test_conversation_messages import conversations  # noqa: F401

QUEUE_TARGET = ('eios:action:NexLoop.queue.' + QUEUE + ':1', ResourceType.ACTION, Operation.EXECUTE)
BODY = '以后我只想通过站内消息收到通知'


class Fixed:
    """The model's answer for the re-extraction window: one channel statement, already in the 0228 vocabulary."""
    provider = 'test'
    model_id = 'fixed-contact-channel'

    def __init__(self):
        self.calls = 0

    def complete(self, system_prompt, user_payload):
        assert system_prompt == SYSTEM_PROMPT
        self.calls += 1
        return json.dumps({'topics': [{'topic': '通知渠道', 'conversation_summary': '只要站内消息', 'user_valid_reply': True, 'message_refs': [1]}],
                           'claims': [{'topic_index': 0, 'message_ref': 1, 'quote': BODY, 'kind': 'preference', 'explicit': True,
                                       'subject': {'kind': 'consumer', 'text': ''}, 'predicate': '通知方式', 'value': {'type': 'string', 'value': '站内消息'}}]},
                          ensure_ascii=False)


def old_claim(admin, f, conversation, message):
    """The free-text Claim an earlier extractor version recorded for that message."""
    seq = admin.execute('select sequence from runtime.nexloop_conversation_messages where message_id=%s', (message,)).fetchone()[0]
    admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
        predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
        extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
        select tenant_id,world,%s,conversation_id,consumer_id,%s,%s,'consumer',consumer_id,'','通知方式',%s,'consumer','affirmed','requested','','',%s,%s,%s,0,%s,%s,%s,
        'nx019-extractor/3',0.9,'preference','needs_resolution','[]',%s,'[]' from runtime.nexloop_conversations where conversation_id=%s""",
        (hashlib.sha256(b'old-channel-claim').hexdigest(), '0' * 64, '1' * 64, Jsonb({'type': 'string', 'value': '站内消息'}),
         Jsonb({'kind': 'none', 'status': 'absent', 'start': None, 'end': None, 'anchor': '2026-10-30T00:00:00+00:00', 'timezone': 'Asia/Shanghai', 'expression': ''}),
         message, seq, len(BODY), hashlib.sha256(BODY.encode()).hexdigest(), BODY, hashlib.sha256(b'old-corr').hexdigest(), conversation))


def preferences(admin, f):
    tenant, consumer = admin.execute('select tenant_id,consumer_id from runtime.nexloop_conversations where consumer_id=%s limit 1', (f['consumer'],)).fetchone()
    return admin.execute("select runtime.nexloop_contact_preferences(%s,'real',%s)", (tenant, consumer)).fetchone()[0]


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_reextraction_brings_the_channel_preference_to_0228_counts_only_and_repeatable(conversations, admin):
    f = conversations
    conversation, ids = make_window(f, [BODY, '好的'], 'channel')
    old_claim(admin, f, conversation, ids[0])
    assert preferences(admin, f) == []  # free-text predicate: 0228 reads no dimension
    api = f['reader'].pool
    signer = f['reader'].signer
    with open_core(make_conninfo(f['pg'], user='nexloop_domain_worker')) as worker_pool:
        _, scheduler_token = seed_multi_authority(admin, api, [(REEXTRACT_ACTION, ResourceType.ACTION, Operation.EXECUTE), QUEUE_TARGET],
                                                  identity_suffix='-claim-scheduler')
        _, plain_token = seed_multi_authority(admin, api, [QUEUE_TARGET], identity_suffix='-no-reextract')
        _, worker_token = seed_multi_authority(admin, worker_pool, [QUEUE_TARGET] + source_targets(conversation, ids), identity_suffix='-claim-worker')
        scheduler = lambda token=scheduler_token: authenticate_service(api, token, world='real')
        with pytest.raises(PlanUnavailable):
            reextract_contact_channels(api, scheduler(plain_token), signer)
        dry = reextract_contact_channels(api, scheduler(), signer)
        assert dry == {'extractor_version': EXTRACTOR_VERSION, 'apply': False,
                       'counts': {'candidate_claims': 1, 'skipped_erasure': 0, 'skipped_persona': 0, 'consumers': 1, 'messages': 1, 'windows': 1, 'accepted': 0}}
        done = reextract_contact_channels(api, scheduler(), signer, apply=True)
        assert done['counts']['accepted'] == 1
        again = reextract_contact_channels(api, scheduler(), signer, apply=True)  # same event id: the queue replays the same task
        assert again['counts']['windows'] == 1
        provider = Fixed()
        worker = ClaimExtractionWorker(worker_pool, authenticate_service(worker_pool, worker_token, world='real'), signer, provider, timezone='Asia/Shanghai')
        assert worker.run_once() == 'succeeded' and worker.run_once() == 'idle' and provider.calls == 1
        (pref,) = preferences(admin, f)
        assert (pref['dimension'], pref['polarity'], pref['value'], pref['dismissed']) == ('channel', 'affirmed', 'native.webchat', False)
        new = admin.execute('select predicate,extractor_version,guard_flags from ontology.nexloop_claims where claim_id=%s', (pref['claim_id'],)).fetchone()
        assert new[0] == 'contact_channel' and new[1] == EXTRACTOR_VERSION and 'contact_channel_normalized' in new[2]
        # Nothing left to re-extract under this version (the old Claim stays as evidence).
        after = reextract_contact_channels(api, scheduler(), signer)
        assert after['counts']['windows'] == 0 and after['counts']['candidate_claims'] == 0
        assert admin.execute("select count(*) from ontology.nexloop_claims where extractor_version='nx019-extractor/3'").fetchone() == (1,)


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_simulated_personas_and_erasures_are_skipped(conversations, admin):
    f = conversations
    conversation, ids = make_window(f, [BODY], 'channel-skip')
    old_claim(admin, f, conversation, ids[0])
    api = f['reader'].pool
    _, token = seed_multi_authority(admin, api, [(REEXTRACT_ACTION, ResourceType.ACTION, Operation.EXECUTE), QUEUE_TARGET], identity_suffix='-claim-scheduler')
    run = lambda: reextract_contact_channels(api, authenticate_service(api, token, world='real'), f['reader'].signer, apply=True)['counts']
    admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) "
                  "select tenant_id,world,consumer_id,'simulated_agent',1,'synthetic-owner' from runtime.nexloop_conversations where conversation_id=%s", (conversation,))
    assert run() == {'candidate_claims': 1, 'skipped_erasure': 0, 'skipped_persona': 1, 'consumers': 0, 'messages': 0, 'windows': 0, 'accepted': 0}
    admin.execute("update control.nexloop_consumer_personas set persona='real',revision=2 where consumer_id=%s", (f['consumer'],))
    admin.execute("insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark) "
                  "select tenant_id,world,'erasure-channel-synthetic',consumer_id,'owner','synthetic-owner','synthetic','pending_confirmation',0 "
                  "from runtime.nexloop_conversations where conversation_id=%s", (conversation,))
    assert run() == {'candidate_claims': 1, 'skipped_erasure': 1, 'skipped_persona': 0, 'consumers': 0, 'messages': 0, 'windows': 0, 'accepted': 0}
