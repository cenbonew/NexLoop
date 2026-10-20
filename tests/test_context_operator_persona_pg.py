"""0227 operator persona slice 1 at the message Run Context v6 bind: the pack carries the tenant's operator persona section
(persona@version, re-derived by SQL) exactly when the tenant has a usable persona for the Consumer; SQL refuses a pack that
omits, adds or rewrites it; the manifest cites operator-persona:<id>@<version> as a role source and the Consumer's assignment is
settled with an event. Same local assembled stack as tests/test_context_v6_pg.py; synthetic data; admin seeds the persona
configuration (the owner Actions are covered by tests/test_operator_persona_pg.py)."""
import json

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.context_artifacts import ContextV6ArtifactProducer
from nexloop_eios.contracts import ContextPackV6
from nexloop_eios.message_relay import MessageRelayUnavailable
from test_context_artifacts import context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_context_v6_pg import bound, owner, sources, v6  # noqa: F401

DOCUMENT = {'display_name': '小禾', 'signature': '小禾｜服务助理', 'tone': '亲切、简洁', 'values': ['不确定就说不确定'],
            'expression': {'max_chars': 200, 'emoji': 'few', 'forbidden_phrases': ['亲爱的']}, 'ai_disclosure': {'deflect_statement': '我是本店的服务助手。'}}


def seed_persona(admin, tenant):
    with admin.transaction():
        owner(admin, tenant)
        admin.execute("insert into control.nexloop_operator_personas(tenant_id,persona_id,version,document,document_hash,created_by,request_intent) "
                      "values(%s,'xiaohe',1,%s,encode(sha256(convert_to(runtime.nexloop_canonical_json(%s::jsonb),'UTF8')),'hex'),'synthetic-owner','seed-persona')",
                      (tenant, Jsonb(DOCUMENT), json.dumps(DOCUMENT)))
        admin.execute("insert into control.nexloop_operator_persona_status_events(tenant_id,persona_id,version,status,set_by,request_intent,reason) "
                      "values(%s,'xiaohe',1,'published','synthetic-owner','seed-persona','合成人设')", (tenant,))
        admin.execute("insert into control.nexloop_operator_persona_rules(tenant_id,version,default_persona_id,rules,published_by,request_intent,reason) "
                      "values(%s,1,'xiaohe','[]','synthetic-owner','seed-rules','默认人设')", (tenant,))


def test_no_persona_leaves_the_pack_unchanged(v6, admin):
    f = v6
    assert f['v6_relay']().run_once() == 'queued'
    pack, _ = bound(admin)
    assert 'operator_persona' not in pack


def test_the_pack_carries_persona_at_version_and_the_manifest_cites_it(v6, admin):
    f = v6
    tenant, consumer = f['original']['tenant'], f['f']['recipe']['consumer_id']
    seed_persona(admin, tenant)
    assert f['v6_relay']().run_once() == 'queued'
    pack, row = bound(admin)
    ContextPackV6.model_validate(pack)
    assert pack['operator_persona'] == {'persona_ref': 'operator-persona:xiaohe@1', 'persona_id': 'xiaohe', 'version': 1, 'source': 'default', 'document': DOCUMENT,
                                        'ai_disclosure': {'mode': 'none', 'when_asked': 'deflect', 'block_human_claims': False, 'content_label': False}}
    rows = sources(admin, tenant, row[3])
    assert [(r[1], r[2], r[3], r[5]) for r in rows if r[1] == 'role'] == [('role', 'operator-persona:xiaohe@1', '1', 'policy')]
    with admin.transaction():
        owner(admin, tenant)
        assert admin.execute('select previous,persona_id,source,request_intent from control.nexloop_operator_persona_assignment_events where consumer_id=%s',
                             (consumer,)).fetchall() == [(None, 'xiaohe', 'initial', 'context:' + row[3])]
        assert admin.execute('select persona_id,source from control.nexloop_operator_persona_current where consumer_id=%s', (consumer,)).fetchone() == ('xiaohe', 'default')
    job = admin.execute('select normalized_input from runtime.jobs').fetchone()[0]
    assert '"operator_persona"' in job['input'] and 'operator-persona:xiaohe@1' in job['input']  # the Run input the model reads


@pytest.mark.parametrize('mode', ['omitted', 'rewritten', 'added_without_persona'])
def test_sql_refuses_a_pack_whose_persona_section_differs(v6, admin, monkeypatch, mode):
    f = v6
    tenant = f['original']['tenant']
    if mode != 'added_without_persona':
        seed_persona(admin, tenant)
    original = ContextV6ArtifactProducer.assemble

    def assemble(self, snapshot, command):
        body, outcome, items, proofs = original(self, snapshot, command)
        if mode == 'omitted':
            body.pop('operator_persona')
        elif mode == 'rewritten':
            body['operator_persona'] = {**body['operator_persona'], 'document': {**DOCUMENT, 'tone': '强势推销'}}
        else:
            body['operator_persona'] = {'persona_ref': 'operator-persona:xiaohe@1', 'persona_id': 'xiaohe', 'version': 1, 'source': 'default', 'document': DOCUMENT,
                                        'ai_disclosure': {'mode': 'none', 'when_asked': 'deflect', 'block_human_claims': False, 'content_label': False}}
        return body, outcome, items, proofs
    monkeypatch.setattr(ContextV6ArtifactProducer, 'assemble', assemble)
    with pytest.raises(MessageRelayUnavailable):
        f['v6_relay']().run_once()
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone() == (0,)


def test_the_persona_section_carries_the_consumers_preferences(v6, admin):
    """0228: the consumer's own communication preference (a Claim of their message) rides in the section, re-derived at bind."""
    import hashlib
    import secrets
    f = v6
    tenant, consumer, message = f['original']['tenant'], f['f']['recipe']['consumer_id'], f['message']
    seed_persona(admin, tenant)
    claim_id = secrets.token_hex(32)
    with admin.transaction():
        owner(admin, tenant)
        admin.execute('''insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
            predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
            extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags) values
            (%s,'real',%s,%s,%s,%s,'tone','consumer',%s,'顾客','communication_tone',%s,'consumer','affirmed','requested','','','{}',%s,%s,0,1,%s,%s,'nx019-extractor/2',0.8,
             'preference','unresolved','[]',%s,'[]')''',
            (tenant, claim_id, message['conversation_id'], consumer, 'a' * 64, 'consumer:' + consumer, Jsonb({'type': 'string', 'value': '说简单点'}), message['id'],
             message['sequence'], hashlib.sha256(message['body'].encode()).hexdigest(), message['body'][:1], hashlib.sha256(claim_id.encode()).hexdigest()))
    assert f['v6_relay']().run_once() == 'queued'
    pack, _ = bound(admin)
    ContextPackV6.model_validate(pack)
    assert pack['operator_persona']['preferences'] == [{'claim_id': claim_id, 'dimension': 'tone', 'polarity': 'affirmed', 'value': '说简单点'}]
